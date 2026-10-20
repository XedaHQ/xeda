"""Provisioning a remote host (`xeda.remote.provision` and `bootstrap.sh`), against the fake `ssh`
(`tests/fake_tools/fake_ssh.py`), the fake `uv` and the fake `curl`. The remote host is this
machine; its home is a directory of the test."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from xeda.remote import provision
from xeda.remote.constants import UV_VERSION
from xeda.remote.environment import (
    CodeSnapshot,
    DependencySpec,
    code_snapshot,
    dependency_key,
    python_choice,
)
from xeda.remote.errors import (
    RemoteConfigError,
    RemoteConnectionError,
    RemoteProtocolError,
    RemoteProvisionError,
)
from xeda.remote.profile import HostProfile
from xeda.remote.provision import (
    Probe,
    Provisioned,
    agent_command,
    ensure_environment,
    probe,
)
from xeda.remote.ssh import SshSession, resolve_destination, sh_command

from .tool_utils import FAKE_TOOLS_DIR
from .remote_fixtures import FAKE_TARGET, fake_remote, fake_ssh

pytestmark = pytest.mark.python_compat
FIXTURES = (fake_ssh, fake_remote)

SPEC = DependencySpec(
    requirements=("pydantic>=2", 'colorama; platform_system == "Windows"'),
    constraints=("pydantic==2.14.1",),
)
#: Poll the locks of the script at once, not once per second.
FAST = {"XEDA_BOOT_POLL": "0.1"}

AGENT = """\
import json, os, sys
import xeda
print(json.dumps({"xeda": xeda.__file__, "argv": sys.argv[1:], "environ": dict(os.environ)}))
"""


@pytest.fixture
def snapshot(tmp_path: Path) -> CodeSnapshot:
    """A snapshot of a small package named `xeda` whose agent prints what it sees."""
    package = tmp_path / "snapshot-source" / "xeda"
    (package / "remote").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "remote" / "__init__.py").write_text("")
    (package / "remote" / "agent.py").write_text(AGENT)
    return code_snapshot(package)


def provision_once(
    snapshot: CodeSnapshot,
    profile: Optional[HostProfile] = None,
    spec: DependencySpec = SPEC,
    destination: str = "venona",
):
    """Connect, probe and provision. Returns the result and the steps that the host reported."""
    profile = profile or HostProfile(env=FAST)
    steps: List[str] = []
    with SshSession(resolve_destination(destination)) as session:
        found = probe(session, profile)
        result = ensure_environment(session, found, profile, snapshot, spec, steps.append)
    return result, steps


def starts(steps: List[str]) -> List[str]:
    return [" ".join(step.split()[:2]) for step in steps]


def test_first_run_provisions_and_second_run_builds_nothing(fake_remote, snapshot):
    first, steps = provision_once(snapshot)
    assert starts(steps) == [
        "Installing uv",
        "Installing Python",
        "Creating the",
        "Uploading xeda",
    ]
    assert steps[0] == f"Installing uv {UV_VERSION}"
    assert steps[1].startswith("Installing Python 3.")
    assert (fake_remote.layout / "env" / first.deps_key / ".xeda-complete").exists()
    assert (fake_remote.layout / "code" / first.code_key / ".xeda-complete").exists()
    lock = (fake_remote.layout / "env" / first.deps_key / "xeda-lock.txt").read_text()
    assert "pydantic==2.14.1" in lock and "pydantic>=2" in lock
    assert fake_remote.venv_builds() == 1
    uv_calls = fake_remote.calls("uv")
    assert all("--no-config" in call for call in uv_calls if call[0] != "--version")
    assert any(f"ENV UV_CACHE_DIR={fake_remote.layout}/cache/uv" in call for call in uv_calls)
    assert any(
        f"ENV UV_PYTHON_INSTALL_DIR={fake_remote.layout}/python" in call for call in uv_calls
    )

    calls_before = len(fake_remote.calls("uv")), len(fake_remote.calls("curl"))
    second, steps = provision_once(snapshot)
    assert steps == []
    assert second == first
    assert (len(fake_remote.calls("uv")), len(fake_remote.calls("curl"))) == calls_before
    # nothing is left behind: no lock, no work directory, no staging directory
    assert not list((fake_remote.layout / "locks").glob("*"))
    assert not list((fake_remote.layout / "tmp").glob("*"))
    assert [p.name for p in (fake_remote.layout / "code").iterdir()] == [first.code_key]


def test_provisioned_names(fake_remote, snapshot):
    result, _ = provision_once(snapshot)
    profile = HostProfile(env=FAST)
    platform = probe_platform()
    assert result.root == str(fake_remote.root)
    assert result.boot.startswith(f"{fake_remote.layout}/bin/boot-")
    assert result.code_key == snapshot.key
    assert result.deps_key == dependency_key(SPEC, python_choice(profile.python), platform)
    assert (fake_remote.layout / "bin" / f"env-{result.env_hash}.sh").is_file()


def probe_platform():
    from xeda.remote.environment import RemotePlatform

    return RemotePlatform(system="Linux", machine="x86_64", libc="glibc", libc_version="2.39")


def test_probe_reports_the_host(fake_remote, monkeypatch):
    with SshSession(resolve_destination("venona")) as session:
        found = probe(session, HostProfile())
    assert found.platform == probe_platform()
    assert found.home == str(fake_remote.home)
    assert found.root == str(fake_remote.root)
    assert found.disk_free_kb > 0
    assert found.kill_user_processes == "unknown" or found.kill_user_processes.isalpha()
    assert isinstance(found, Probe)


def test_profile_root_and_xdg_cache_home_choose_the_root(fake_remote, snapshot, tmp_path):
    # the default root
    provision_once(snapshot, HostProfile(env=FAST))
    assert (fake_remote.layout / "bin").is_dir()
    # the XDG cache home reaches the installer through the `env` of the profile
    xdg = tmp_path / "xdg-cache"
    xdg.mkdir()
    with SshSession(resolve_destination("venona")) as session:
        found = probe(session, HostProfile(env={"XDG_CACHE_HOME": str(xdg)}))
    assert found.root == f"{xdg}/xeda"
    assert (xdg / "xeda" / "v1" / "bin").is_dir()
    # a root in the home directory, and an absolute root
    with SshSession(resolve_destination("venona")) as session:
        assert probe(session, HostProfile(root="~/scratch/x")).root == str(
            fake_remote.home / "scratch" / "x"
        )
        assert probe(session, HostProfile(root="~/")).root == str(fake_remote.home)
        absolute = tmp_path / "absolute-root"
        assert probe(session, HostProfile(root=str(absolute))).root == str(absolute)
    assert (absolute / "v1" / "bin").is_dir()


def test_not_linux_is_refused_and_leaves_nothing(fake_remote, snapshot):
    fake_remote.set_platform("Darwin", "arm64")
    with pytest.raises(RemoteProvisionError, match="needs Linux") as refused:
        provision_once(snapshot)
    assert refused.value.code == "not-linux"
    assert "Darwin" in str(refused.value)
    assert not (fake_remote.home / ".cache").exists()


def test_unsupported_platform_is_refused_before_anything_is_built(fake_remote, snapshot):
    fake_remote.set_platform("Linux", "riscv64")
    with pytest.raises(RemoteProvisionError) as refused:
        provision_once(snapshot)
    assert refused.value.code == "unsupported-platform"
    assert fake_remote.calls("uv") == []


def test_checksum_mismatch_is_refused_and_leaves_no_uv(fake_remote, snapshot, monkeypatch):
    monkeypatch.setattr(
        "xeda.remote.environment.UV_RELEASES",
        {FAKE_TARGET: (fake_remote.uv_url, "0" * 64)},
    )
    with pytest.raises(RemoteProvisionError, match="SHA-256") as refused:
        provision_once(snapshot)
    assert refused.value.code == "checksum-mismatch"
    assert not (fake_remote.layout / "uv").exists() or not list(
        (fake_remote.layout / "uv").iterdir()
    )
    assert not list((fake_remote.layout / "locks").glob("*"))
    assert not list((fake_remote.layout / "tmp").glob("*"))
    assert fake_remote.calls("uv") == []


def test_failed_download_names_the_url_and_keeps_the_output(fake_remote, snapshot, monkeypatch):
    url = "https://uv.example.test/missing.tar.gz"
    monkeypatch.setattr(
        "xeda.remote.environment.UV_RELEASES", {FAKE_TARGET: (url, fake_remote.uv_sha256)}
    )
    with pytest.raises(RemoteProvisionError) as failed:
        provision_once(snapshot)
    assert failed.value.code == "download-failed"
    assert url in str(failed.value) and "404" in str(failed.value)


#: The programs that the script and the installer use, besides the shell built-ins.
UTILITIES = (
    "sh", "cat", "chmod", "date", "df", "dirname", "getconf", "gzip", "mkdir", "mv", "rm",
    "rmdir", "sha256sum", "shasum", "sleep", "tar", "touch", "uname",
)  # fmt: skip


def test_no_curl_or_wget_names_both(fake_remote, snapshot, tmp_path):
    fake_remote.set_path(fake_remote.restricted_path(tmp_path, UTILITIES))
    with pytest.raises(RemoteProvisionError) as refused:
        provision_once(snapshot)
    assert refused.value.code == "no-downloader"
    assert "curl" in str(refused.value) and "wget" in str(refused.value)


def test_script_runs_with_only_its_programs_and_uses_wget_when_curl_is_missing(
    fake_remote, snapshot, tmp_path
):
    restricted = fake_remote.restricted_path(tmp_path, UTILITIES)
    fake_curl = Path(__file__).parent / "fake_tools" / "fake_curl.py"
    # wget -q -O <file> <url>
    (restricted / "wget").write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake_curl}" -o "$3" "$4"\n'
    )
    (restricted / "wget").chmod(0o755)
    (restricted / "curl").unlink(missing_ok=True)
    fake_remote.set_path(restricted)
    _, steps = provision_once(snapshot)
    assert steps[0].startswith("Installing uv")
    assert fake_remote.calls("curl")[0][0] == "-o"  # the fake curl, called by the wget wrapper
    assert fake_remote.venv_builds() == 1


def test_half_built_environment_is_rebuilt(fake_remote, snapshot):
    first, _ = provision_once(snapshot)
    marker = fake_remote.layout / "env" / first.deps_key / ".xeda-complete"
    marker.unlink()
    (fake_remote.layout / "env" / first.deps_key / "leftover").write_text("x")
    again, steps = provision_once(snapshot)
    assert again == first
    assert steps == ["Creating the environment"]
    assert marker.exists()
    assert not (fake_remote.layout / "env" / first.deps_key / "leftover").exists()
    assert fake_remote.venv_builds() == 2


def test_environment_with_a_wrong_marker_is_rebuilt(fake_remote, snapshot):
    first, _ = provision_once(snapshot)
    marker = fake_remote.layout / "env" / first.deps_key / ".xeda-complete"
    marker.write_text("another-key\n")
    _, steps = provision_once(snapshot)
    assert steps == ["Creating the environment"]


def test_failed_environment_is_removed_and_reported(fake_remote, snapshot):
    fake_remote.configure(uv_pip_fail="No solution found")
    with pytest.raises(RemoteProvisionError, match="requirements of xeda") as failed:
        provision_once(snapshot)
    assert failed.value.code == "env-failed"
    assert "No solution found" in str(failed.value)
    assert not list((fake_remote.layout / "env").glob("*"))
    assert not list((fake_remote.layout / "locks").glob("*"))


def test_failure_on_a_full_disk_is_reported_as_such(fake_remote, snapshot, tmp_path):
    fake_remote.configure(uv_pip_fail="No space left on device")
    (fake_remote.bin / "df").write_text(
        "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
        "echo '/dev/x 1000 990 10 99% /'\n"
    )
    (fake_remote.bin / "df").chmod(0o755)
    with pytest.raises(RemoteProvisionError, match="only 10 KB free") as failed:
        provision_once(snapshot)
    assert failed.value.code == "no-space"
    with SshSession(resolve_destination("venona")) as session:
        assert probe(session, HostProfile()).disk_free_kb == 10


def test_two_provisions_at_once_build_one_environment(fake_remote, snapshot):
    fake_remote.configure(uv_venv_delay=1.0)
    results = {}
    errors = []

    def work(name: str) -> None:
        try:
            results[name] = provision_once(snapshot)
        except BaseException as error:  # reported by the main thread
            errors.append(error)

    threads = [threading.Thread(target=work, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    assert results["a"][0] == results["b"][0]
    assert fake_remote.venv_builds() == 1
    assert sum(1 for call in fake_remote.calls("curl")) == 1
    installs = [c for c in fake_remote.calls("uv") if c[1:3] == ["python", "install"]]
    assert len(installs) == 1
    waited = [s for _, steps in results.values() for s in steps if s.startswith("Waiting")]
    assert waited, "one of the two should have waited for the lock"
    assert [p.name for p in (fake_remote.layout / "code").iterdir()] == [snapshot.key]
    assert not list((fake_remote.layout / "locks").glob("*"))


def test_noise_from_the_login_shell_is_ignored(fake_remote, snapshot, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_SSH_NOISE", "1")
    first, steps = provision_once(snapshot)
    assert starts(steps)[0] == "Installing uv"
    second, steps = provision_once(snapshot)
    assert second == first and steps == []


def test_profile_env_reaches_provisioning(fake_remote, snapshot):
    profile = HostProfile(env={**FAST, "HTTPS_PROXY": "http://proxy.example.test:3128"})
    provision_once(snapshot, profile)
    assert any(
        "ENV HTTPS_PROXY=http://proxy.example.test:3128" in call
        for call in fake_remote.calls("curl")
    )
    assert any(
        "ENV HTTPS_PROXY=http://proxy.example.test:3128" in call for call in fake_remote.calls("uv")
    )


NASTY = {
    "SPACES": "a b  c",
    "DOLLAR": "$HOME ${HOME} $(echo x) $1",
    "QUOTES": "it's \"quoted\" 'single'",
    "BACKQUOTE": "`echo hi`",
    "NEWLINE": "first\nsecond\n\nfourth",
    "BACKSLASH": "back\\slash \\n",
    "BANG": "!bang !!",
    "EDGES": "  leading and trailing  ",
    "EMPTY": "",
    "GLOB": "*.v [a-z]?",
    "SEMICOLON": "a; touch evil; b",
    "HEREDOC": "XEDA_END_x\nEOF\nEOF_LDD",
}


def agent_reply(session: SshSession, result: Provisioned, *args: str) -> dict:
    run = session.run(agent_command(result, *args))
    assert run.returncode == 0, run.stderr.decode()
    lines = [line for line in run.stdout.decode().splitlines() if line.startswith("{")]
    return json.loads(lines[-1])


def test_profile_env_values_reach_the_remote_command_unchanged(fake_remote, snapshot):
    profile = HostProfile(env={**FAST, **NASTY})
    result, _ = provision_once(snapshot, profile)
    with SshSession(resolve_destination("venona")) as session:
        seen = agent_reply(session, result)["environ"]
    for name, value in NASTY.items():
        assert seen[name] == value, name
    assert not (fake_remote.home / "evil").exists()


def test_agent_command_runs_the_snapshot_first_on_the_path(fake_remote, snapshot):
    result, _ = provision_once(snapshot)
    code_dir = fake_remote.layout / "code" / snapshot.key
    with SshSession(resolve_destination("venona")) as session:
        seen = agent_reply(session, result, "one", "two")
        assert seen["xeda"] == str(code_dir / "xeda" / "__init__.py")
        assert seen["argv"] == ["one", "two"]
        assert seen["environ"]["PYTHONPATH"] == str(code_dir)
    assert (code_dir / "last-use").exists()
    assert (fake_remote.layout / "env" / result.deps_key / "last-use").exists()
    command = agent_command(result, "k1")
    assert (
        command == f'exec sh "{result.boot}" agent {result.env_hash} {result.deps_key} '
        f"{result.code_key} k1"
    )


def test_agent_refuses_a_missing_environment(fake_remote, snapshot):
    result, _ = provision_once(snapshot)
    missing = Provisioned(result.root, result.boot, result.env_hash, "0" * 24, result.code_key)
    with SshSession(resolve_destination("venona")) as session:
        run = session.run(agent_command(missing))
    assert b"XEDA-ERROR env-failed" in run.stdout and run.returncode == 1


def test_stale_lock_of_a_dead_process_is_broken(fake_remote, snapshot):
    with SshSession(resolve_destination("venona")) as session:
        probe(session, HostProfile())
    locks = fake_remote.layout / "locks"
    locks.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    host = os.uname().nodename
    for name in (f"uv-{UV_VERSION}-{FAKE_TARGET}", f"python-{python_choice('managed').request}"):
        (locks / f"{name}.lock").mkdir()
        (locks / f"{name}.lock" / "owner").write_text(f"{host} {dead.pid} {int(time.time())}\n")
    provision_once(snapshot)
    assert not list(locks.glob("*"))


def test_lock_that_is_too_old_is_reported(fake_remote, snapshot):
    with SshSession(resolve_destination("venona")) as session:
        probe(session, HostProfile())
    lock = fake_remote.layout / "locks" / f"uv-{UV_VERSION}-{FAKE_TARGET}.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"another-host 4242 {int(time.time()) - 3 * 3600}\n")
    with pytest.raises(RemoteProvisionError, match="another-host") as failed:
        provision_once(snapshot)
    assert failed.value.code == "lock-timeout"
    assert lock.exists()  # xeda does not remove the lock of a process on another host


def test_put_code_refuses_unsafe_archives(fake_remote, snapshot, tmp_path):
    result, _ = provision_once(snapshot)
    import io
    import tarfile

    def archive(name: str, kind: str) -> bytes:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
            else:
                info.size = 1
            tar.addfile(info, io.BytesIO(b"x") if kind != "link" else None)
        return out.getvalue()

    key = "f" * 24
    with SshSession(resolve_destination("venona")) as session:
        for name, kind in (
            ("/etc/x", "file"),
            ("../x", "file"),
            ("a/../../x", "file"),
            ("x", "link"),
        ):
            run = session.run(
                sh_command("put-code", result.env_hash, key, script_path=result.boot),
                stdin=archive(name, kind),
            )
            assert b"XEDA-ERROR bad-request" in run.stdout, (name, kind, run.stdout)
            assert run.returncode == 1
    assert not (fake_remote.layout / "code" / key).exists()
    assert [p.name for p in (fake_remote.layout / "code").iterdir()] == [snapshot.key]


def test_terminated_script_releases_its_lock_and_removes_its_directories(
    fake_remote, snapshot, tmp_path
):
    result, _ = provision_once(snapshot)
    fake_remote.configure(uv_venv_delay=60.0)
    other = DependencySpec(requirements=("pydantic>=3",), constraints=())
    key = dependency_key(other, python_choice("managed"), probe_platform())
    environment = {
        "PATH": os.environ["XEDA_FAKE_SSH_PATH"],
        "HOME": str(fake_remote.home),
        "USER": "xeda",
        **FAST,
    }
    script = subprocess.Popen(
        [
            "sh",
            result.boot,
            "provision",
            result.env_hash,
            key,
            python_choice("managed").request,
            UV_VERSION,
            fake_remote.uv_url,
            fake_remote.uv_sha256,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert script.stdin is not None
    script.stdin.write(b"R pydantic>=3\n")
    script.stdin.close()
    deadline = time.time() + 30
    while not (fake_remote.layout / "env" / key).exists():
        assert time.time() < deadline, "no environment directory"
        assert script.poll() is None, script.stderr.read() if script.stderr else ""
        time.sleep(0.05)
    assert (fake_remote.layout / "locks" / f"env-{key}.lock").exists()
    script.send_signal(signal.SIGTERM)
    assert script.wait(timeout=30) == 143
    assert not (fake_remote.layout / "env" / key).exists()
    assert not list((fake_remote.layout / "locks").glob("*"))
    assert not list((fake_remote.layout / "tmp").glob("*"))
    script.stdout and script.stdout.close()
    script.stderr and script.stderr.close()


# --- The script itself ------------------------------------------------------------------------


def script_path() -> Path:
    return Path(provision.__file__).with_name("bootstrap.sh")


def test_scripts_are_posix_sh(tmp_path):
    installer = tmp_path / "installer.sh"
    installer.write_text(provision.installer_script(provision.boot_script(), "export A='b c'\n"))
    for path in (script_path(), installer):
        subprocess.run(["sh", "-n", str(path)], check=True)
        if shutil.which("dash"):
            subprocess.run(["dash", "-n", str(path)], check=True)
        if shutil.which("shellcheck"):
            subprocess.run(["shellcheck", "-s", "sh", str(path)], check=True)


@pytest.mark.skipif(not shutil.which("dash"), reason="dash is not installed")
def test_provisioning_works_under_dash(fake_remote, snapshot, tmp_path):
    shell = tmp_path / "dash-bin"
    shell.mkdir()
    (shell / "sh").symlink_to(shutil.which("dash") or "")
    fake_remote.set_path(shell, fake_remote.bin, FAKE_TOOLS_DIR, Path("/usr/bin"), Path("/bin"))
    _, steps = provision_once(snapshot)
    assert starts(steps)[0] == "Installing uv"
    assert fake_remote.venv_builds() == 1


def test_script_has_no_bashisms():
    text = script_path().read_text()
    for forbidden in ("[[", "local ", "declare ", "function ", "<<<", "$'", "${!", "=~", "(( "):
        assert forbidden not in text, forbidden
    assert text.startswith("#")


def test_low_disk_limit_is_the_same_in_both_scripts():
    assert f"LOW_DISK_KB={provision.LOW_DISK_KB}" in script_path().read_text()


def test_installer_text_for_values_that_look_like_its_own_markers():
    env = {"A": "XEDA_END_" + "0" * 12, "B": "@@BOOT_TEXT@@"}
    text = provision.installer_script(provision.boot_script(), provision.env_file_text(env))
    assert text.count("export B=@@BOOT_TEXT@@") == 2  # the env text is not replaced again
    assert provision.boot_script().rstrip("\n") in text


def test_unknown_error_code_is_a_protocol_error():
    reply = provision._Reply(errors=["made-up Something"], returncode=1)
    with pytest.raises(RemoteProtocolError, match="made-up"):
        provision._raise_failure(None, reply, "testing")  # type: ignore[arg-type]


def test_nul_in_an_environment_value_is_refused():
    with pytest.raises(RemoteConfigError, match="NUL"):
        provision.env_file_text({"A": "x\0y"})


def test_unsafe_root_is_refused(fake_remote, tmp_path):
    odd = tmp_path / "dollar$sign"
    odd.mkdir()
    with SshSession(resolve_destination("venona")) as session:
        found = None
        with pytest.raises(RemoteConfigError, match="Set `root`"):
            found = probe(session, HostProfile(env={"XDG_CACHE_HOME": str(odd)}))
        assert found is None


def test_lost_connection_during_a_step_is_a_connection_error(fake_remote, snapshot):
    with SshSession(resolve_destination("venona")) as session:
        found = probe(session, HostProfile())
        (Path(session._control)).unlink()
        with pytest.raises(RemoteConnectionError):
            ensure_environment(session, found, HostProfile(), snapshot, SPEC, lambda _: None)
