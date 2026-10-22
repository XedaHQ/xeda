"""The OpenSSH transport of remote runs (`xeda.remote.ssh`), against the fake `ssh`
(`tests/fake_tools/remote/fake_ssh.py`). A real master connection is not tested here."""

import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from xeda.remote.errors import RemoteConfigError, RemoteConnectionError
from xeda.remote import ssh
from xeda.remote.ssh import Destination, SshSession, resolve_destination, sh_command

from .remote_fixtures import fake_ssh, ssh_calls
from .tool_utils import terminal_output

pytestmark = pytest.mark.python_compat
#: The fixture, imported for the tests below that name it as an argument (pytest finds it in
#: this module; this line keeps the linter from reading each argument as a redefinition).
FIXTURES = (fake_ssh,)

#: Seconds that a test waits for something that must happen at once.
PATIENCE = 20


def _is_exit(call) -> bool:
    return call[call.index("-O") + 1] == "exit" if "-O" in call else False


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
    assert _is_exit(calls[-1])  # closed at the end, and by the last call
    assert not Path(_control_path(calls)).parent.exists()  # and its directory is gone


def test_master_options(fake_ssh):
    with SshSession(resolve_destination("venona")):
        pass
    calls = ssh_calls(fake_ssh)
    master = " ".join(next(c for c in calls if "-M" in c))
    for option in (
        "ControlMaster=yes",
        "ControlPersist=300",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
    ):
        assert f"-o {option} " in master + " "
    assert all("StrictHostKeyChecking" not in " ".join(c) for c in calls)
    assert master.split()[:3] == ["-M", "-N", "-f"]
    assert master.split()[-1] == "venona"
    control = Path(_control_path(calls))
    assert len(os.fsencode(control)) < 100


def test_an_exception_in_the_block_still_closes_the_master(fake_ssh):
    with pytest.raises(KeyError):
        with SshSession(resolve_destination("venona")) as s:
            s.run("true")
            raise KeyError("a failure in the block")
    calls = ssh_calls(fake_ssh)
    assert _is_exit(calls[-1])
    assert not Path(_control_path(calls)).parent.exists()


def test_every_client_uses_the_master_and_never_asks(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        s.run("true")
        s.popen("true").communicate()
    clients = [c for c in ssh_calls(fake_ssh) if "-T" in c]
    assert len(clients) == 2
    for call in clients:
        assert "-S" in call
        words = " ".join(call)
        assert "-o ControlMaster=no" in words and "-o BatchMode=yes" in words


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
    short = tmp_path / "short"
    short.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(long))
    monkeypatch.setattr(ssh, "_FALLBACK_DIRECTORY", str(short))
    with SshSession(resolve_destination("venona")) as s:
        assert Path(s._control).parent.parent == short
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
    options = ["-F", "/dev/null"]
    destination = resolve_destination("venona", options)
    with SshSession(destination, options) as s:
        s.run("true")
    calls = ssh_calls(fake_ssh)
    assert len(calls) >= 5
    assert all(c[c.index("-F") + 1] == "/dev/null" for c in calls)


@pytest.mark.parametrize(
    "options",
    [["-o", "Port=2200"], ["-F"], ["-F", ""], ["-F", "a", "-v"], ["-oProxyCommand=x", "a"]],
)
def test_only_configuration_file_options_are_accepted(fake_ssh, options):
    with pytest.raises(ValueError):
        resolve_destination("venona", options)
    with pytest.raises(ValueError):
        SshSession(Destination("venona", "u", "venona", 22), options)


def test_a_destination_made_by_hand_is_checked(fake_ssh):
    for text in ("-oProxyCommand=x", "a b", ""):
        with pytest.raises(RemoteConfigError):
            SshSession(Destination(text, "u", "h", 22))


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
    with pytest.raises(ValueError):
        sh_command("probe", "=ls")  # zsh would expand it to a path
    with pytest.raises(ValueError):
        sh_command("-s", "x")
    with pytest.raises(ValueError):
        sh_command("-")


@pytest.mark.parametrize(
    "bad", ['/a"b', "/a$b", "/a`b", "/a\\b", "/a!b", "/a\nb", "", "rel/x.sh", "x.sh"]
)
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
    with SshSession(resolve_destination("venona")) as s:
        # The remote command ends by itself after 10 s, so it cannot outlive the test for long.
        with pytest.raises(subprocess.TimeoutExpired):
            s.run("exec sleep 10", timeout=0.3)


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


#: A program that opens a session, starts a client with `popen`, and tells whether the client is
#: still there after a SIGINT for its process group (Ctrl-C at a terminal).
_CTRL_C = """
import signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
from xeda.remote.ssh import SshSession, resolve_destination, sh_command

with SshSession(resolve_destination("venona")) as session:
    client = session.popen(sh_command())
    client.stdin.write(b"echo up\\n")
    client.stdin.flush()
    assert client.stdout.readline() == b"up\\n"  # the client runs
    try:
        # Inside `try`: a SIGINT right after the ready line must land in the handler below.
        print("ready", flush=True)
        time.sleep(60)
    except KeyboardInterrupt:
        time.sleep(1)
        print("alive" if client.poll() is None else f"ended {client.returncode}", flush=True)
    client.stdin.close()
    client.wait()
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_command_client_does_not_stop_on_ctrl_c_in_the_terminal(fake_ssh):
    """A client of the master ends on SIGINT, as OpenSSH's does, even if it started with SIGINT
    ignored. So Ctrl-C must not reach it: it runs in a process group of its own."""
    program = subprocess.Popen(
        [sys.executable, "-c", _CTRL_C],
        stdout=subprocess.PIPE,
        start_new_session=True,  # a group of its own, as at a terminal
    )
    try:
        assert program.stdout.readline() == b"ready\n"
        os.killpg(program.pid, signal.SIGINT)
        assert program.stdout.readline() == b"alive\n"
        assert program.wait(timeout=PATIENCE) == 0
    finally:
        if program.poll() is None:
            os.killpg(program.pid, signal.SIGKILL)
            program.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_the_remote_command_starts_with_the_default_sigint(fake_ssh):
    with SshSession(resolve_destination("venona")) as s:
        result = s.run("kill -INT $$; echo survived")
    assert b"survived" not in result.stdout


def _wait_for_line(path: Path, text: bytes) -> None:
    """Wait until a process writes `text` to the FIFO at `path`; fail after `PATIENCE` seconds."""
    deadline = time.monotonic() + PATIENCE
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        data = b""
        while text not in data:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{text!r} did not arrive"
            # a FIFO without a writer reads as end of file at once, and select returns at once
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.05))
            chunk = os.read(fd, 100) if ready else b""
            data += chunk
            if ready and not chunk:
                select.select([], [], [], min(remaining, 0.01))
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


def test_a_redirect_without_a_file_descriptor_gives_the_child_the_null_device():
    """A stream with no `fileno` (the capture of a test runner) cannot go to a child. The child
    must not fall back to our own standard output: the redirect keeps that stream clean. In log
    mode a child never gets the standard output."""
    import io
    import sys

    from xeda import proc_utils

    assert proc_utils.child_stdout() == subprocess.DEVNULL
    with terminal_output():
        assert proc_utils.child_stdout() is None
    with terminal_output(io.StringIO()):
        assert proc_utils.child_stdout() == subprocess.DEVNULL
    with terminal_output(sys.__stderr__):
        assert proc_utils.child_stdout() is sys.__stderr__
