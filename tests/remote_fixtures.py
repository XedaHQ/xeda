"""Fixtures of the tests of remote runs. A test module imports the fixtures that it uses and names
them once at module level (`FIXTURES = (fake_ssh,)`), so that the linter does not read each
argument of a test as a redefinition of the import."""

import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from unittest import mock
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
import yaml

from xeda.design import Design
from xeda.outcomes import Outcome, Result
from xeda.project import Project
from xeda.records import RunRecord
from xeda.remote import client
from xeda.remote.environment import CodeSnapshot
from xeda.remote.profile import HostProfile

from xeda.spec import Spec
from xeda.types import UNSET
from xeda.utils import settings_to_dict

from .tool_utils import FAKE_TOOLS_DIR, use_fake_fpga_tools, use_fake_tools

#: The fake `ssh`, `uv` and `curl`. Only the remote fixtures put it on a `PATH`.
REMOTE_FAKES_DIR = FAKE_TOOLS_DIR / "remote"


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Put the fake `ssh` (`tests/fake_tools/remote/fake_ssh.py`, which documents it) first on
    `PATH`. The remote host is this machine. Yields the home directory of the remote user, a new
    directory under `tmp_path`; `ssh_calls` reads what the fake recorded."""
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("PATH", f"{REMOTE_FAKES_DIR}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "fake-ssh" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("XEDA_FAKE_SSH_HOME", str(home))
    monkeypatch.setenv(
        "XEDA_FAKE_SSH_PATH",
        os.pathsep.join([str(REMOTE_FAKES_DIR), str(FAKE_TOOLS_DIR), "/usr/bin", "/bin"]),
    )
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
    `tests/fake_tools/remote` and `tests/fake_tools` come second. The settings that the fakes read are in
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
        f'#!/bin/sh\nexec "{sys.executable}" "{REMOTE_FAKES_DIR / "fake_uv.py"}" "$@"\n',
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
    remote.set_path(bin_dir, REMOTE_FAKES_DIR, FAKE_TOOLS_DIR, Path("/usr/bin"), Path("/bin"))
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

    def expose(self, *names: str) -> None:
        """Let the remote launch use the tools `names` of this machine (`ghdl`, `yosys`, `bsc`).
        Their directories go before the fakes on the `PATH` of the launch, so the real tool wins
        over a fake of the same name. Call it before the first run."""
        fakes = {str(self.toolchain), str(FAKE_TOOLS_DIR), str(REMOTE_FAKES_DIR)}
        real = os.pathsep.join(
            entry for entry in os.environ["PATH"].split(os.pathsep) if entry not in fakes
        )
        for name in names:
            found = shutil.which(name, path=real)
            assert found, f"{name} is not installed here"
            line = f"export PATH={Path(found).parent}:$PATH"
            if line not in self.setup:
                self.setup.append(line)  # later lines win
        self.write_profile()

    def load_test_flows(self, *modules: str, after: str = "") -> None:
        """Make every launch on the remote import the test `modules` (by default
        `tests.io_flows`) when it starts, so that the remote knows the flows that the tests
        define. Call it before the first run. The statements in `after` run next, to change a
        flow.

        The launch entry in the code snapshot that the client sends gets a first statement that
        imports the modules from this checkout when the module runs as the program. (A
        `sitecustomize` module would import them before the interpreter is ready for the
        extension modules.)"""
        modules = modules or ("tests.io_flows",)
        checkout = Path(__file__).resolve().parent.parent
        hook = (
            "if __name__ == '__main__':\n"
            "    import sys\n"
            f"    sys.path.insert(0, {str(checkout)!r})\n"
            + "".join(f"    import {name}\n" for name in modules)
            + "".join(f"    {line}\n" for line in after.splitlines())
        ).encode()
        entry = "xeda/remote/launch_entry.py"
        real = client.code_snapshot

        def snapshot() -> CodeSnapshot:
            original = real()
            contents = tuple(
                (name, hook + data if name == entry else data, executable)
                for name, data, executable in original.contents
            )
            assert contents != original.contents, entry
            return dataclasses.replace(
                original,
                key=hashlib.sha256((original.key + hook.decode()).encode()).hexdigest()[:24],
                contents=contents,
            )

        self.remote.monkeypatch.setattr(client, "code_snapshot", snapshot)


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
        setup=[
            f"export PATH={prefix / 'bin'}:{REMOTE_FAKES_DIR}:{FAKE_TOOLS_DIR}:{fake_remote.bin}:$PATH"
        ],
        fields={},
    )
    host.write_profile()
    yield host


def host_profile_of(destination: str) -> HostProfile:
    """The profile of `destination` in the profile file of the user, as the command line reads
    it (`XDG_CONFIG_HOME` points to the file of the test)."""
    from xeda.remote.profile import host_profile, read_host_profiles, remotes_file

    return host_profile(destination, read_host_profiles(remotes_file()))


@dataclass
class RemoteRun:
    """What `remote_request` saw: the `result` of the call, or the `error` that it raised.

    `document` is the run document of the result. For a call that raised, it is the document of a
    request that started no launch, with the error of the exception."""

    result: Optional[Result]
    error: Optional[BaseException]
    document: Dict[str, Any]

    @property
    def success(self) -> bool:
        if self.result is None:  # a plan
            return self.error is None
        return self.result.passed

    @property
    def exit_status(self) -> int:
        """The exit status of `xeda run`: 0 for a pass, 2 for a request that started no launch."""
        if self.success:
            return 0
        return 2 if self.document["outcome"] is None else 1

    @property
    def record(self) -> Optional[RunRecord]:
        if self.result is None or self.result.nodes[self.result.roots[0]].record is None:
            return None
        return self.result.record


def run_of(call: Any) -> RemoteRun:
    """Call `call()`, which returns a `Result`, and return what it gave or raised as a `RemoteRun`."""
    from xeda.flow_runner.error_codes import public_error, request_error_info

    try:
        result = call()
    except Exception as error:
        info = request_error_info(error)
        public = public_error(error)
        document = {"outcome": None, "error": info.as_json_value(), "nodes": [], "roots": []}
        document["error"]["code"] = public.code
        return RemoteRun(None, error, document)
    return RemoteRun(result, None, result.to_dict())


def design_of(design: Any, xedaproject: Any, design_updates: Sequence[str], target: Any) -> Design:
    """Load the design that a request names: a file, or a design of the project."""
    updates = settings_to_dict(list(design_updates)) if design_updates else {}
    if design is not None and not isinstance(design, Design):
        found = Path(design)
        if xedaproject is None or found.suffix:
            return Design._load_file(
                found,
                updates=(updates,) if updates else (),
                target=None if target is UNSET else target,
            )
        design = None
    if isinstance(design, Design):
        return design
    loaded = Project.load(Path(xedaproject)).design(target=target)
    return loaded.with_updates(updates) if updates else loaded


@contextmanager
def _ssh_options(options: Sequence[str]) -> Iterator[None]:
    """Give the client the `ssh` options of a real host (`-F <file>`) for the time of a block. The
    client takes none: they exist for these tests."""
    if not options:
        yield
        return
    resolve, session = client.resolve_destination, client.SshSession
    with (
        mock.patch.object(client, "resolve_destination", lambda text: resolve(text, options)),
        mock.patch.object(client, "SshSession", lambda destination: session(destination, options)),
    ):
        yield


def remote_request(tmp_path: Path, design: Any, chain: str = "nextpnr", **overrides: Any):
    """Run `chain` for `design` on the host `somewhere` with `run_remote`, and return a
    `RemoteRun`. The run root is `tmp_path/local/xeda_run`. `overrides` replace the arguments of
    the request: the ones of `xeda run`, `destination_text`, `profile` and `local_settings`."""
    from xeda.flow_runner.error_codes import public_error, request_error_info
    from xeda.flow_runner.launch_options import LaunchOptions, WorkspaceOptions
    from xeda.flow_runner.settings_layers import command_line_table
    from xeda.outcomes import run_document, spec_document_of
    from xeda.remote.client import plan_remote, run_remote
    from xeda.remote_value import Remote

    arguments: Dict[str, Any] = dict(
        destination_text="somewhere",
        run_root=tmp_path / "local" / "xeda_run",
        flow_settings=[],
        xedaproject=None,
        design_updates=[],
        target=UNSET,
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        dry_run=False,
        fetch_all=False,
        keep_running=False,
        outputs_to=None,
        overwrite_outputs=False,
        confirm_overwrite=None,
        log_level=20,
        detailed_logs=False,
        local_settings=None,
        cancel=None,
        on_event=None,
    )
    arguments.update(overrides)
    arguments.setdefault("profile", host_profile_of(arguments["destination_text"]))
    asked = arguments["confirm_overwrite"]
    spec = None
    profile = arguments["profile"]
    workspace = WorkspaceOptions(
        run_root=Path(arguments["run_root"]),
        hashed_run_dirs=arguments["hashed_run_dirs"],
        tool_output="log",
        confirm=None if asked is None else (lambda text: asked([text])),
        remote=Remote(
            arguments["destination_text"],
            profile=profile.model_dump(mode="python"),
            fetch_all=arguments["fetch_all"],
            keep_running=arguments["keep_running"],
        ),
        local_settings=arguments["local_settings"],
    )
    options = LaunchOptions(
        rebuild_all=arguments["rebuild_all"],
        clean=arguments["clean"],
        outputs_to=arguments["outputs_to"],
        overwrite_outputs=arguments["overwrite_outputs"],
    )
    common = dict(
        workspace=workspace,
        options=options,
        log_level=arguments["log_level"],
        detailed_logs=arguments["detailed_logs"],
    )
    try:
        table = arguments["flow_settings"]
        spec = Spec(
            chain,
            design_of(
                design, arguments["xedaproject"], arguments["design_updates"], arguments["target"]
            ),
            settings=table if isinstance(table, Mapping) else command_line_table(list(table)),
        )
        with _ssh_options(arguments.get("ssh_options", ())):
            if arguments["dry_run"]:
                plan = plan_remote(spec, **common)
                return RemoteRun(None, None, plan.to_dict())
            result = run_remote(
                spec, cancel=arguments["cancel"], on_event=arguments["on_event"], **common
            )
            return RemoteRun(result, None, result.to_dict())
    except Exception as error:
        from xeda.errors import RemoteError, SpecError

        # A request that starts no launch: it is invalid, or its host cannot be reached or set up.
        refused = isinstance(public_error(error), (SpecError, RemoteError))
        document = run_document(
            outcome=None if refused else Outcome.FAILED,
            error=request_error_info(error),
            launch_id=None,
            spec=None if spec is None else spec_document_of(spec),
            roots=(),
            nodes=(),
        )
        # An exception that Xeda does not know comes as an `internal_error` from its cause.
        if getattr(error, "code", None) == "internal_error" and error.__cause__ is not None:
            error = error.__cause__  # type: ignore[assignment]
        return RemoteRun(None, error, document)


def connected(host: "RemoteHost") -> bool:
    """Whether the client opened an SSH connection to the fake host (`ssh -G` does not count)."""
    return any("-M" in call for call in host.remote.calls("ssh"))
