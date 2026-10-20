"""The OpenSSH transport of remote runs (`xeda.remote.ssh`), against the fake `ssh`
(`tests/fake_tools/fake_ssh.py`). A real master connection is not tested here."""

import os
import select
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from xeda.remote.errors import RemoteConfigError, RemoteConnectionError
from xeda.remote.ssh import Destination, SshSession, resolve_destination, sh_command

from .remote_fixtures import fake_ssh, ssh_calls

pytestmark = pytest.mark.python_compat
#: The fixture, imported for the tests below that name it as an argument (pytest finds it in
#: this module; this line keeps the linter from reading each argument as a redefinition).
FIXTURES = (fake_ssh,)

#: Seconds that a test waits for something that must happen at once.
PATIENCE = 20


def _control_path(calls) -> str:
    master = next(c for c in calls if "-M" in c)
    option = next(a for a in master if a.startswith("ControlPath="))
    return option.split("=", 1)[1]


def test_master_then_commands_share_one_login(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        assert s.run("echo hi").stdout == b"hi\n"
        s.run("true")
    calls = ssh_calls(fake_ssh)
    assert sum("-M" in c for c in calls) == 1
    assert all("BatchMode=yes" in " ".join(c) for c in calls if "-S" in c)
    assert any(c[-2:] == ["-O", "exit"] or "exit" in c for c in calls)  # closed at the end
    assert not Path(_control_path(calls)).parent.exists()  # and its directory is gone


def test_master_options(fake_ssh):
    with SshSession(resolve_destination("venona")):
        pass
    calls = ssh_calls(fake_ssh)
    master = " ".join(next(c for c in calls if "-M" in c))
    for option in (
        "ControlMaster=yes",
        "ControlPersist=60",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
    ):
        assert f"-o {option} " in master + " "
    assert all("StrictHostKeyChecking" not in " ".join(c) for c in calls)
    assert master.split()[:3] == ["-M", "-N", "-f"]
    assert master.split()[-1] == "venona"
    control = Path(_control_path(calls))
    assert len(os.fsencode(control)) < 100


def test_control_directory_is_private(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        directory = Path(s._control).parent
        assert directory.stat().st_mode & 0o777 == 0o700
        assert Path(s._control).exists()


def test_control_path_stays_short_when_the_temporary_directory_is_long(
    fake_ssh, tmp_path, monkeypatch
):
    long = tmp_path / ("d" * 90)
    long.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(long))
    with SshSession(resolve_destination("venona")) as s:
        assert len(os.fsencode(s._control)) + 17 < 104
        assert s.check()


def test_failed_login_is_a_connection_error_with_the_ssh_message(fake_ssh, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    destination = resolve_destination("venona")  # asking `ssh -G` still works
    with pytest.raises(RemoteConnectionError, match="Permission denied"):
        SshSession(destination).__enter__()
    assert not Path(_control_path(ssh_calls(fake_ssh))).parent.exists()


def test_lost_master_is_reported_not_reconnected(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        assert s.check()
        Path(s._control).unlink()
        assert not s.check()
        with pytest.raises(RemoteConnectionError, match="the SSH connection to venona closed"):
            s.run(sh_command("true"))
        with pytest.raises(RemoteConnectionError, match="the SSH connection to venona closed"):
            s.popen(sh_command("true"))


def test_no_command_runs_after_the_session_ends(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        pass
    assert not s.check()
    with pytest.raises(RemoteConnectionError):
        s.run("true")


@pytest.mark.parametrize("bad", ["-oProxyCommand=x", "a b", "", "a\tb", "a\nb"])
def test_destination_text_is_refused(bad):
    with pytest.raises(RemoteConfigError):
        resolve_destination(bad)


@pytest.mark.parametrize(
    "text, user, hostname, port",
    [
        ("venona", "xeda", "venona", 22),
        ("kamyar@venona", "kamyar", "venona", 22),
        ("ssh://kamyar@venona:2222", "kamyar", "venona", 2222),
        ("ssh://venona", "xeda", "venona", 22),
    ],
)
def test_destination_is_resolved_by_ssh_g(fake_ssh, text, user, hostname, port):
    assert resolve_destination(text) == Destination(text, user, hostname, port)
    assert ssh_calls(fake_ssh)[-1][0] == "-G"


def test_ssh_options_reach_every_ssh_process(fake_ssh):
    options = ["-o", "Port=2200", "-F", "/dev/null"]
    destination = resolve_destination("venona", options)
    assert destination.port == 2200
    with SshSession(destination, options) as s:
        s.run(sh_command("true"))
    calls = ssh_calls(fake_ssh)
    assert len(calls) >= 5
    assert all(c[c.index("-F") + 1] == "/dev/null" for c in calls)


def test_sh_command_quotes_only_plain_tokens():
    assert sh_command("probe", "-") == "exec sh -s -- probe -"
    assert sh_command() == "exec sh -s --"
    assert (
        sh_command("agent", "k1", script_path="/h/.cache/xeda/v1/bin/boot-ab.sh")
        == 'exec sh "/h/.cache/xeda/v1/bin/boot-ab.sh" agent k1'
    )
    with pytest.raises(ValueError):
        sh_command("a;b")
    with pytest.raises(ValueError):
        sh_command("")
    with pytest.raises(ValueError):
        sh_command("a b")


@pytest.mark.parametrize("bad", ['/a"b', "/a$b", "/a`b", "/a\\b", "/a!b", "/a\nb", ""])
def test_sh_command_refuses_an_unsafe_script_path(bad):
    with pytest.raises(ValueError):
        sh_command("x", script_path=bad)


def test_destination_label():
    assert Destination("u@h:22", "u", "h", 22).label() == "u@h_22"
    assert Destination("ssh://u@h:22", "u", "h", 22).label() == "ssh___u@h_22"


def test_the_remote_command_runs_in_the_remote_home_with_a_small_environment(fake_ssh):
    with SshSession(resolve_destination("kamyar@venona")) as s:
        result = s.run("pwd; env | sort")
    lines = result.stdout.decode().splitlines()
    assert lines[0] == str(fake_ssh.resolve()) or lines[0] == str(fake_ssh)
    env = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    assert env["HOME"] == str(fake_ssh)
    assert env["USER"] == "kamyar"
    assert env["PATH"].endswith("/usr/bin:/bin")
    assert set(env) <= {"PATH", "HOME", "USER", "LANG", "PWD", "SHLVL", "_", "OLDPWD"}


def test_run_passes_stdin_stdout_and_stderr_unchanged(fake_ssh):
    data = bytes(range(256)) * 300 + b"\r\n\x00end"
    with SshSession(resolve_destination("venona")) as s:
        result = s.run("cat; cat >&2 </dev/null; echo oops >&2; exit 3", stdin=data)
    assert result.returncode == 3
    assert result.stdout == data
    assert result.stderr == b"oops\n"


def test_run_without_stdin_gives_the_command_end_of_file(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        assert s.run("cat; echo done").stdout == b"done\n"


def test_run_returns_the_status_255_of_the_command_while_the_master_lives(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        assert s.run("exit 255").returncode == 255


def test_run_times_out(fake_ssh):
    fifo = fake_ssh / "never"
    os.mkfifo(fifo)
    with SshSession(resolve_destination("venona")) as s:
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                s.run('exec cat "$HOME/never"', timeout=0.5)
        finally:  # let the orphaned `cat` read end of file
            _open_and_close(fifo)


def _open_and_close(fifo: Path) -> None:
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:  # no reader: the command is gone already
        pass


def test_login_noise_comes_first_on_stdout(fake_ssh, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_SSH_NOISE", "1")
    with SshSession(resolve_destination("venona")) as s:
        out = s.run("echo marker").stdout.decode().splitlines()
    assert len(out) == 2 and out[0] != "marker" and out[1] == "marker"


def test_popen_streams_both_ways_in_binary(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        process = s.popen("exec cat")
        for chunk in (b"\x00\xff\n", b"x" * 100_000, b"\r\n"):
            process.stdin.write(chunk)
            process.stdin.flush()
            assert process.stdout.read(len(chunk)) == chunk
        process.stdin.close()
        assert process.stdout.read() == b""
        assert process.wait(PATIENCE) == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_command_clients_ignore_sigint(fake_ssh, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_SSH_REPORT_SIGINT", "1")
    with SshSession(resolve_destination("venona")) as s:
        process = s.popen(sh_command("true"))
        out, _ = process.communicate(timeout=PATIENCE)
        assert out.decode().splitlines()[0] == "SIGINT ignored"
        # The check and the master are not clients of this kind, and the parent keeps SIGINT.
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_the_remote_command_starts_with_the_default_sigint(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        result = s.run("kill -INT $$; echo survived")
    assert b"survived" not in result.stdout


def _wait_for_line(path: Path, text: bytes) -> None:
    """Wait until a process writes `text` to the FIFO at `path`."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        data = b""
        while text not in data:
            ready, _, _ = select.select([fd], [], [], PATIENCE)
            assert ready, f"{text!r} did not arrive"
            chunk = os.read(fd, 100)
            if chunk:
                data += chunk
            else:  # no writer yet
                select.select([], [], [], 0.01)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_killing_the_ssh_client_gives_the_remote_command_end_of_file(fake_ssh):
    fifo = fake_ssh / "eof"
    os.mkfifo(fifo)
    with SshSession(resolve_destination("venona")) as s:
        process = s.popen('echo ready; cat >/dev/null; echo eof >"$HOME/eof"')
        assert process.stdout.readline() == b"ready\n"
        process.kill()
        process.wait()
        _wait_for_line(fifo, b"eof")
