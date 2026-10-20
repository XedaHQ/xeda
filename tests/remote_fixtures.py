"""Fixtures of the tests of remote runs. A test module imports the fixtures that it uses and names
them once at module level (`FIXTURES = (fake_ssh,)`), so that the linter does not read each
argument of a test as a redefinition of the import."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
import yaml

from .tool_utils import FAKE_TOOLS_DIR, use_fake_fpga_tools, use_fake_tools


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Put the fake `ssh` (`tests/fake_tools/fake_ssh.py`, which documents it) first on `PATH`.
    The remote host is this machine. Yields the home directory of the remote user, a new
    directory under `tmp_path`; `ssh_calls` reads what the fake recorded."""
    use_fake_tools(monkeypatch)
    home = tmp_path / "fake-ssh" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("XEDA_FAKE_SSH_HOME", str(home))
    monkeypatch.setenv("XEDA_FAKE_SSH_PATH", f"{FAKE_TOOLS_DIR}{os.pathsep}/usr/bin:/bin")
    yield home


# --- The fake remote host: fake `uv`, `curl`, `uname` and `ldd` --------------------------------

#: The platform that the fake remote host reports, and the uv release target for it.
FAKE_TARGET = "x86_64-unknown-linux-gnu"
FAKE_URL = f"https://uv.example.test/releases/uv-{FAKE_TARGET}.tar.gz"


def tool_calls(home: Path, tool: str) -> List[List[str]]:
    """The arguments of every call of the fake `tool` (`ssh`, `uv`, `curl`), in order, for the
    remote `home` that the `fake_ssh` fixture yielded. A call that has `ENV` lines gets them as
    arguments `ENV NAME=value` after its own."""
    calls: List[List[str]] = []
    log = home.parent / f"fake_{tool}.calls"
    if not log.exists():
        return calls
    for line in log.read_text().splitlines():
        kind, _, value = line.partition(" ")
        if kind == "CALL":
            calls.append([])
        elif kind == "ARG":
            calls[-1].append(value)
        elif kind == "ENV":
            calls[-1].append(f"ENV {value}")
    return calls


def ssh_calls(home: Path) -> List[List[str]]:
    """The arguments of every call of the fake `ssh`, in order, for the remote `home` that the
    `fake_ssh` fixture yielded."""
    return tool_calls(home, "ssh")


def _script(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _real_uname() -> str:
    for candidate in ("/usr/bin/uname", "/bin/uname"):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("no uname")


@dataclass
class FakeRemote:
    """A remote host that is this machine, with a fake `uv` and a fake `curl`, a `uname` and a
    `ldd` that report a Linux x86_64 host with glibc, and a `python3`.

    The programs in `bin` come first on the `PATH` of the remote commands; the fake tools of
    `tests/fake_tools` come second. The settings that the fakes read are in
    `<parent of home>/fake_remote.json` (see `fake_uv.py` and `fake_curl.py`); `configure` changes
    them."""

    home: Path
    bin: Path
    uv_url: str
    uv_sha256: str
    releases: Dict[str, Tuple[str, str]]
    monkeypatch: pytest.MonkeyPatch

    @property
    def root(self) -> Path:
        """The default remote root."""
        return self.home / ".cache" / "xeda"

    @property
    def layout(self) -> Path:
        return self.root / "v1"

    def configure(self, **settings: object) -> None:
        path = self.home.parent / "fake_remote.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update(settings)
        path.write_text(json.dumps(current))

    def set_platform(self, system: str = "Linux", machine: str = "x86_64") -> None:
        (self.bin / "uname.system").write_text(system + "\n")
        (self.bin / "uname.machine").write_text(machine + "\n")

    def calls(self, tool: str) -> List[List[str]]:
        return tool_calls(self.home, tool)

    def venv_builds(self) -> int:
        """How many environments the fake uv created."""
        return sum(1 for call in self.calls("uv") if "venv" in call)

    def set_path(self, *directories: Path) -> None:
        """Replace the `PATH` of the remote commands."""
        self.monkeypatch.setenv("XEDA_FAKE_SSH_PATH", os.pathsep.join(str(d) for d in directories))

    def restricted_path(self, tmp: Path, names: Sequence[str]) -> Path:
        """A directory that holds the fake programs of `bin` and, of the programs on the local
        `PATH`, only those in `names`. A name that is not installed here is left out."""
        directory = tmp / "restricted-bin"
        directory.mkdir()
        for program in self.bin.iterdir():
            (directory / program.name).symlink_to(program)
        for name in names:
            found = shutil.which(name)
            if found and not (directory / name).exists():
                (directory / name).symlink_to(found)
        return directory


@pytest.fixture
def fake_remote(
    fake_ssh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeRemote]:
    """The fake remote host. Patches `xeda.remote.environment.UV_RELEASES` to a release of a uv
    that is the fake `uv`, for the platform that the fake host reports."""
    bin_dir = tmp_path / "fake-ssh" / "bin"
    bin_dir.mkdir()
    _script(
        bin_dir / "uname",
        f"""#!/bin/sh
here=$(dirname "$0")
case "${{1:-}}" in
    -s) cat "$here/uname.system" ;;
    -m) cat "$here/uname.machine" ;;
    *) exec {_real_uname()} "$@" ;;
esac
""",
    )
    _script(bin_dir / "ldd", '#!/bin/sh\necho "ldd (GNU libc) 2.39"\n')
    _script(bin_dir / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')

    stage = tmp_path / "fake-uv-release"
    (stage / f"uv-{FAKE_TARGET}").mkdir(parents=True)
    _script(
        stage / f"uv-{FAKE_TARGET}" / "uv",
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS_DIR / "fake_uv.py"}" "$@"\n',
    )
    _script(stage / f"uv-{FAKE_TARGET}" / "uvx", "#!/bin/sh\nexit 1\n")
    archive = tmp_path / "fake-uv.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage / f"uv-{FAKE_TARGET}", arcname=f"uv-{FAKE_TARGET}")
    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

    remote = FakeRemote(
        home=fake_ssh,
        bin=bin_dir,
        uv_url=FAKE_URL,
        uv_sha256=sha256,
        releases={FAKE_TARGET: (FAKE_URL, sha256)},
        monkeypatch=monkeypatch,
    )
    remote.set_platform()
    remote.configure(python=sys.executable, curl_files={FAKE_URL: str(archive)})
    remote.set_path(bin_dir, FAKE_TOOLS_DIR, Path("/usr/bin"), Path("/bin"))
    monkeypatch.setattr("xeda.remote.environment.UV_RELEASES", remote.releases)
    yield remote


# --- The remote host of the client tests --------------------------------------------------------


@dataclass
class RemoteHost:
    """The fake remote host with a host profile and the fake FPGA toolchain.

    The fake `ssh` hands a remote command only `PATH`, `HOME`, `USER` and `LANG`, and the launch
    runs under a login shell that may change `PATH`. So the toolchain reaches the launch through a
    `setup` line of the host profile, and an environment variable of a fake tool through its `env`
    table. `write_profile` writes the host profile file again after a test changed `env`,
    `setup` or `fields`."""

    remote: FakeRemote
    name: str
    profile_file: Path
    toolchain: Path
    env: Dict[str, str]
    setup: List[str]
    fields: Dict[str, Any]

    @property
    def home(self) -> Path:
        return self.remote.home

    @property
    def workspaces(self) -> Path:
        """The directory of the workspaces on the remote."""
        return self.remote.layout / "ws"

    def workspace(self) -> Path:
        """The only workspace on the remote."""
        (found,) = sorted(self.workspaces.iterdir())
        return found

    def write_profile(self) -> None:
        entry: Dict[str, Any] = {"setup": self.setup, **self.fields}
        if self.env:
            entry["env"] = self.env
        self.profile_file.parent.mkdir(parents=True, exist_ok=True)
        self.profile_file.write_text(yaml.safe_dump({"hosts": {self.name: entry}}))

    def configure(self, *, env: Optional[Dict[str, str]] = None, **fields: Any) -> None:
        """Add `env` variables and profile `fields`, and write the profile."""
        self.env.update(env or {})
        self.fields.update(fields)
        self.write_profile()


@pytest.fixture
def remote_host(
    fake_remote: FakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RemoteHost]:
    """The fake remote host `somewhere`, with a host profile whose `setup` puts the fake FPGA
    toolchain first on the `PATH` of the launch. The user-level directories of the local machine
    (`XDG_CONFIG_HOME`, `XDG_CACHE_HOME`) are under `tmp_path`."""
    prefix = tmp_path / "toolchain"
    use_fake_fpga_tools(monkeypatch, prefix)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    host = RemoteHost(
        remote=fake_remote,
        name="somewhere",
        profile_file=tmp_path / "config" / "xeda" / "remotes.yaml",
        toolchain=prefix / "bin",
        env={},
        setup=[f"export PATH={prefix / 'bin'}:{FAKE_TOOLS_DIR}:{fake_remote.bin}:$PATH"],
        fields={},
    )
    host.write_profile()
    yield host
