import contextlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import zipfile
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, Dict, Literal, Optional, Tuple, Union

import execnet
from fabric import Connection
from fabric.transfer import Transfer

from ..artifacts import (
    ArtifactPath,
    iter_artifact_paths,
    map_artifact_paths,
)
from ..deliver import (
    Deliveries,
    DeliveryError,
    deliverable_locations,
    outputs_to_deliveries,
    recorded_artifacts,
    split_deliveries,
)
from ..design import (
    Design,
    deferring_load_side_effects,
    loading_in_run_root,
    names_a_design_file,
)
from ..flow import Flow, FlowSettingsError
from ..flow.flow import written_path_problems
from ..proc_utils import tool_output_stream
from ..utils import (
    XedaException,
    dump_json,
    settings_to_dict,
)
from ..version import __version__
from ..xedaproject import PROJECT_FILE_NAMES, XedaProject, resolve_project_file
from ..remote.fetch import (
    deliver_fetched,
    keep_vouched,
    mirror_relative,
    remote_read_inputs,
)
from ..remote.packaging import package_inputs
from .bindings import require_no_bindings, split_bindings
from .default_runner import (
    FlowLauncher,
    FlowNotFoundError,
    get_flow_class,
    print_results,
    with_debug_logging,
)
from .launch_input import refuse_outputs_to_a_programmer
from .resolver import Plan, planned_design_of
from .run_lock import run_dir_lock
from .settings_layers import (
    COMMAND_LINE_ORIGIN,
    command_line_sections,
    compose_flow_settings,
    merge_flow_sections,
)
from .trace import remove_trace
from .outputs import declared_output_files
from ..flow.derived import recorded_files
from .trace import as_recorded
from ..digest import content_digest

log = logging.getLogger(__name__)


def _transfer_artifacts(
    conn: Connection,
    artifacts: Any,
    remote_run_path: str,
    local_dir: Path,
    succeeded: bool = True,
    flow_name: str = "the remote flow",
    written_on_remote: Optional[Collection[str]] = None,
) -> Any:
    """Fetch path leaves and return the same artifact tree with local path strings.

    A missing artifact of a successful run is an error. Of a failed run, only the artifacts
    that the remote vouched its run wrote are fetched and kept (`keep_vouched`)."""
    remote_root = os.path.normpath(remote_run_path)
    if not succeeded:
        artifacts = keep_vouched(artifacts, written_on_remote, flow_name)

    local_dir.mkdir(exist_ok=True, parents=True)
    local_root = local_dir.resolve()
    remote_to_local: dict[str, str] = {}
    destinations: dict[Path, str] = {}

    def remote_path_of(artifact: ArtifactPath) -> str:
        named_path = os.fspath(artifact)
        return os.path.normpath(
            named_path if os.path.isabs(named_path) else os.path.join(remote_root, named_path)
        )

    for artifact in iter_artifact_paths(artifacts):
        named_path = os.fspath(artifact)
        remote_path = remote_path_of(artifact)
        if remote_path in remote_to_local:
            continue
        local_path = local_dir / mirror_relative(remote_path, remote_root)
        if local_path in destinations and destinations[local_path] != remote_path:
            raise ValueError(f"Artifacts have conflicting local destination: {local_path}")
        if not local_path.resolve().is_relative_to(local_root):
            raise ValueError(f"Artifact destination is outside {local_dir}: {local_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        result = conn.get(remote_path, str(local_path))
        log.debug("Transferred artifact %s to %s", named_path, result.local)
        remote_to_local[remote_path] = str(local_path)
        destinations[local_path] = remote_path

    if remote_to_local:
        log.info("Transferred %d artifact(s) to %s", len(remote_to_local), local_dir)

    def rewrite_path(artifact: ArtifactPath) -> str:
        return remote_to_local[remote_path_of(artifact)]

    return map_artifact_paths(artifacts, rewrite_path)


REMOTE_PYTHON_MIN_VERSION = (3, 11, 0)


class RemoteIncompatible(XedaException):
    """The remote host cannot run what this side would send it.

    It is reported before anything is shipped, as the user error it is and not as a
    traceback."""


def check_remote_python(version_info: tuple[Any, ...]) -> None:
    """Fail early and clearly when the worker cannot run this xeda package."""
    if version_info[:3] < REMOTE_PYTHON_MIN_VERSION:
        required = ".".join(str(part) for part in REMOTE_PYTHON_MIN_VERSION)
        found = ".".join(str(part) for part in version_info)
        raise RemoteIncompatible(
            f"Python {required} or newer is required on the remote, but found {found}"
        )


#: The release line a remote must run, and the remote protocol it must speak. A Xeda without a
#: protocol marker speaks protocol 0. Protocol 1 is: canonical resolved settings, relocated read
#: inputs with their path identities, declared output records with checked hand-over,
#: current-run evidence for simulations, the FPGA build graph (`fpga_pack`, a programming-only
#: `openfpgaloader`), the identity rule (a node's `flow_hash` is its settings plus its ordered
#: resolved input origins, which this side names the mirror by and compares with the remote's),
#: the declared Vivado outputs, and `yosys`'s declared netlist with its ASIC configuration, a
#: bundled platform counted relative to the installation of Xeda. Raised together with
#: `xeda.REMOTE_PROTOCOL_VERSION` once per release cycle; development builds are not supported
#: remotes.
REMOTE_XEDA_MIN_VERSION = (0, 4, 4)
REMOTE_PROTOCOL_MIN_VERSION = 1

# Runs before shipping anything. Inspect the package this interpreter actually imports: installed
# distribution metadata alone can describe a different Xeda shadowed by a stale checkout. A
# missing or broken import is reported as an incompatible install, not an execnet traceback.
REMOTE_PROBE = """
import sys
installed = None
location = None
protocol = 0
try:
    import xeda
    installed = xeda.__version__
    location = xeda.__file__
    protocol = getattr(xeda, "REMOTE_PROTOCOL_VERSION", 0)
except Exception:
    pass
channel.send((
    sys.platform,
    tuple(sys.version_info),
    sys.executable,
    installed,
    location,
    protocol,
))
"""


def release_tuple(version: str) -> tuple[int, ...]:
    """Return the release that a version string names.

    For example, `"0.4.1.dev0+g9d1eb61"` gives `(0, 4, 1)`."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def check_remote_xeda(
    found: str | None, location: str | None, python: str, protocol: int = 0
) -> None:
    """Fail early and clearly when the Xeda on the remote cannot run what this side sends.

    `found` is the version of the Xeda that `python` on the remote imports (None when it imports
    none), and `location` is where that package lives. Without this check, a version mismatch
    surfaces as whatever the loader on the remote chokes on first. A 0.2 remote, for example,
    reports `rtl.sources: unhashable type: 'dict'`.

    The remote needs the release line `REMOTE_XEDA_MIN_VERSION` and protocol
    `REMOTE_PROTOCOL_MIN_VERSION` (a Xeda without a marker reports 0). One that resolves the
    same request otherwise reports another `flow_hash`, so it is refused here and does not fail
    on a hash mismatch after the run. The version alone cannot tell a release from a checkout.
    A compatible remote on another release line draws a warning about differences in settings.
    """
    required = ".".join(str(part) for part in REMOTE_XEDA_MIN_VERSION)
    if found is None:
        raise RemoteIncompatible(
            f"{python} on the remote imports no Xeda that can be used: upgrade the remote Xeda "
            f"to a build with remote protocol support (Xeda {required}, including development "
            f"builds, or newer; remote protocol {REMOTE_PROTOCOL_MIN_VERSION} or newer) for that "
            "interpreter (it is started by a non-login shell, so it may not be the one on your "
            "login PATH)"
        )
    if (
        release_tuple(found) < REMOTE_XEDA_MIN_VERSION
        or type(protocol) is not int
        or protocol < REMOTE_PROTOCOL_MIN_VERSION
    ):
        raise RemoteIncompatible(
            f"{python} on the remote imports Xeda {found} from {location} (remote protocol "
            f"{protocol}), which cannot run the request of this Xeda: upgrade the remote Xeda to "
            f"a build with remote protocol support (Xeda {required}, including development "
            f"builds, or newer; remote protocol "
            f"{REMOTE_PROTOCOL_MIN_VERSION} or newer). Remove that install if it shadows a newer "
            "install (it is started by a non-login shell, so it may not be the one on your login "
            "PATH)"
        )
    if release_tuple(found)[:2] != release_tuple(__version__)[:2]:
        log.warning(
            "The remote runs Xeda %s and this side %s: a setting only one of them knows will be "
            "rejected on the remote",
            found,
            __version__,
        )


def send_design(
    design: Design,
    conn,
    remote_path: str,
    all_flows_settings: Mapping[str, Any] | None = None,
    *,
    run_root: Path | None = None,
    path_identities: dict[str, str] | None = None,
    planned_flows: Collection[str] | None = None,
) -> Tuple[str, str]:
    """Archive the design (`package_inputs`) and transfer it to the remote host."""
    assert isinstance(conn, Connection)

    sections = dict(all_flows_settings) if all_flows_settings is not None else design.flow
    package = package_inputs(
        design,
        sections,
        set(sections) if planned_flows is None else planned_flows,
        run_root=run_root or Path(os.devnull),
        remote_inputs=PurePosixPath(remote_path),
    )
    if path_identities is not None:
        path_identities.update(package.path_identities)
    # The old runner reports the name of the target on this side and plans nothing there by it.
    document = json.loads(package.generated[package.design_file])
    if document.pop("target", None) is not None:
        package.generated[package.design_file] = json.dumps(document).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmpdirname:
        zip_file = Path(tmpdirname) / f"{design.name}.zip"
        log.info("Preparing design archive: %s", zip_file)
        with zipfile.ZipFile(zip_file, mode="w") as archive:
            for place, local in package.files.items():
                archive.write(local, arcname=place)
            for place, content in package.generated.items():
                archive.writestr(place, content)
            for place in package.directories:
                archive.mkdir(place)
        if log.isEnabledFor(logging.DEBUG):
            with zipfile.ZipFile(zip_file, mode="r") as archive:
                for info in archive.infolist():
                    log.debug("Archive entry: %s (%d bytes)", info.filename, info.file_size)
        log.info("Transferring design to %s in %s", conn.host, remote_path)
        conn.put(zip_file, remote=remote_path)
        return zip_file.name, package.design_file


def remote_runner(
    channel, remote_path, zip_file, flow, design_file, flow_settings, env=None, path_identities=None
):
    # pylint: disable=import-outside-toplevel,reimported,redefined-outer-name
    """Unpack the design archive and run the flow in the remote Python process."""
    import json
    import os
    import zipfile
    from pathlib import Path

    try:
        from xeda.flow_runner import DefaultRunner
        from xeda.flow_runner.outputs import handed_over
        from xeda.flow.flow import using_path_identities
    except ImportError:
        # FIXME: create or use a virtual environment, or change the remote approach.
        raise Exception("The xeda Python package was not found. Install it with pip.")

    os.chdir(remote_path)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    if channel.isclosed():
        return

    with zipfile.ZipFile(zip_file, mode="r") as archive:
        archive.extractall(path=remote_path)

    # What the remote directory holds before the run: the identity, size and times of every
    # file and directory, on the own file system of the remote. With it, the artifacts of a
    # failed run can be told from files that were already here, if a worker lacks its
    # write-reporting method (`Flow.wrote_output`). The directory is new, so this is the files
    # of the design archive. A directory that could not be read proves nothing about what lies
    # under it.
    remote_root = os.path.realpath(remote_path)
    before: dict = {}
    unread: list = []
    pending_dirs: list = [remote_root]
    while pending_dirs:
        top = pending_dirs.pop()
        try:
            st = os.stat(top)
            entries = list(os.scandir(top))
        except FileNotFoundError:
            continue
        except OSError:
            unread.append(top)
            continue
        before[(st.st_dev, st.st_ino)] = (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
        )
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending_dirs.append(entry.path)
                    continue
                st = os.stat(entry.path)
            except OSError:
                continue
            before[(st.st_dev, st.st_ino)] = (
                st.st_dev,
                st.st_ino,
                st.st_size,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )

    # A run root of its own: the directory that the archive was unpacked into holds its files.
    run_root = str(Path.cwd() / "xeda_run")
    # The protocol floor guarantees these settings: every run starts clean, in a directory named
    # by its run hash (`clean` implies `rebuild_all`).
    launcher = DefaultRunner(
        run_root,
        backups=False,
        post_cleanup=False,
        display_results=False,
        hashed_run_dirs=True,
        clean=True,
    )
    # NOTE: the source of this function is shipped to the remote host and executed there
    # against a Xeda that satisfies the protocol floor. Live output streaming is set up
    # separately, at the file-descriptor level, by `STREAM_OUTPUT_SETUP` (pure standard
    # library, with no Xeda involved). See `RemoteRunner.run_remote`.
    try:
        with using_path_identities(path_identities or {}):
            f = launcher.run(
                flow,
                design=design_file,
                design_allow_extra=True,
                flow_settings=flow_settings,
            )
    except Exception:
        # The launcher records setup and producer failures before it raises again. Return that
        # document too. Failures before a requested flow was constructed still surface as remote
        # errors.
        f = launcher.launched[-1] if launcher.launched else None
        if f is None or f.name != flow or f.results.get("success") is not False:
            raise
    if f is not None and f.results.get("success"):
        # Validate on the remote file system before any transfer can follow a link of a tool.
        for output in f.results.get("outputs", {}):
            handed_over(f, output)
    # `json` raises on a key it cannot write (a tuple, a `Path`), which lost a run's results
    # entirely. This runs against the Xeda of the remote, which may predate `utils.with_json_keys`,
    # so the same rule is spelled out here: an enum key is its value, any other key json cannot
    # write is its text, and keys that would share JSON text keep both values. In plain loops
    # over locals: execnet ships only this function's source, and refuses one with nested
    # functions (a closure is a non-builtin global to it).
    from enum import Enum

    root: list = [{"success": False} if f is None else f.results.to_dict()]
    pending: list = [(root, 0)]
    while pending:
        parent, slot = pending.pop()
        node = parent[slot]
        if isinstance(node, dict):
            fixed = {}
            for key, item in node.items():
                key_type = type(key).__name__
                while isinstance(key, Enum):
                    key = key.value
                if not (key is None or isinstance(key, (str, int, float, bool))):
                    key = str(key)
                name = key if isinstance(key, str) else json.dumps(key)
                if name in fixed:
                    stem = f"{name} ({key_type})"
                    name = stem
                    suffix = 2
                    while name in fixed:
                        name = f"{stem} {suffix}"
                        suffix += 1
                fixed[name] = item
            parent[slot] = fixed
            for key in list(fixed):
                pending.append((fixed, key))
        elif isinstance(node, (list, tuple)):
            items = list(node)
            parent[slot] = items
            for index in range(len(items)):
                pending.append((items, index))
    results = json.dumps(root[0], default=str, indent=1)
    channel.send(results)

    # Which of the artifacts of a failed run the run wrote. This is judged here, on the own
    # file system of the remote, because this side never compares its clock or its files with
    # the remote's. The own record of the remote flow (`wrote_output`) decides where its Xeda
    # has one. Otherwise an artifact counts when it lies under the remote directory and its
    # identity was not held before the run, or its state changed since. Anything else (an
    # artifact outside the directory, or under a directory that the snapshot could not read)
    # is unknown and not vouched for, and this side drops it. The names are those in the
    # results.
    written = None
    if f is not None and not f.results.get("success"):
        written = []
        wrote_output = getattr(f, "wrote_output", None)
        run_dir = str(f.run_path)
        pending_leaves: list = [f.results.get("artifacts")]
        while pending_leaves:
            node = pending_leaves.pop()
            if isinstance(node, dict):
                pending_leaves.extend(node.values())
                continue
            if isinstance(node, (list, tuple)):
                pending_leaves.extend(node)
                continue
            if not isinstance(node, (str, os.PathLike)) or not os.fspath(node):
                continue
            name = os.fspath(node)
            full = name if os.path.isabs(name) else os.path.join(run_dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if wrote_output is not None:
                try:
                    vouched = bool(wrote_output(node))
                except Exception:  # pylint: disable=broad-except
                    vouched = False
            else:
                real = os.path.realpath(full)
                vouched = real.startswith(remote_root + os.sep)
                for directory in unread:
                    if real == directory or real.startswith(directory + os.sep):
                        vouched = False
                state = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                if before.get((st.st_dev, st.st_ino)) == state:
                    vouched = False
            if vouched and name not in written:
                written.append(name)
    channel.send(json.dumps(written))


def get_env_var(conn, var):
    result = conn.run(f"echo ${var}", hide=True, pty=False)
    assert result.ok
    return result.stdout.strip()


def get_login_env(conn: Connection) -> Dict[str, str]:
    try:
        result = conn.run("$SHELL -l -c env", hide=True, pty=False)
    except socket.gaierror as e:
        raise XedaException(f"Error connecting to {conn.host}: {e}")

    assert result.ok

    lines_split = [line.split("=") for line in result.stdout.strip().split("\n")]
    return {kv[0]: kv[1] for kv in lines_split if len(kv) == 2}


class RemoteLogger:
    """A logger that echoes data received from a remote output-forwarding execnet channel.

    It writes the data to a local stream, live, as each message arrives."""

    def __init__(self, stream: IO[str], label: str = "remote"):
        self.stream = stream
        self.label = label

    def cb(self, data: Optional[str]) -> None:
        if data is None:
            log.debug("Remote %s channel closed.", self.label)
            return
        self.stream.write(data)
        self.stream.flush()


# Executed on the remote host, in the gateway (worker) process, to stream the
# flow's output back live.
#
# It takes over the file descriptors of the worker's stdout and stderr (1 and 2),
# and not the Python-level `sys.stdout` and `sys.stderr` objects, because that is
# what a spawned EDA tool actually inherits. The own bootstrap of execnet
# (`execnet.gateway_base.init_popen_io`) has already `dup`ed the real fd 1 aside
# for its wire protocol and pointed fd 1 at /dev/null, so fds 1 and 2 are free to
# claim. Anything the flow writes to them, directly or from any subprocess it
# spawns, then lands in the pty and is forwarded here line by line as it is
# produced.
#
# IMPORTANT: this runs against whatever Xeda happens to be installed on the
# remote host, which may be older than the local one. It therefore uses only
# the standard library and must stay free of any xeda import, so that live
# output streaming never depends on the version of Xeda on the remote.
STREAM_OUTPUT_SETUP = r"""
import os
import sys
import threading


def _open_stream_pair():
    # A pty (rather than a pipe) so that tools which only line-buffer when
    # attached to a terminal keep doing so, and their output arrives here
    # incrementally instead of in big blocks. Returns (read_fd, write_fd).
    try:
        import pty
        import termios
    except ImportError:  # non-POSIX remote: fall back to a plain pipe
        return os.pipe()
    read_fd, write_fd = pty.openpty()
    try:
        # Turn off output post-processing entirely, so the pty cannot rewrite
        # what the tool wrote (by default it would at least turn every '\n'
        # into '\r\n'). We want it to look like a terminal purely so tools keep
        # line-buffering. The bytes themselves must come through untouched,
        # so that what you see locally is what the tool actually printed.
        attrs = termios.tcgetattr(write_fd)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(write_fd, termios.TCSANOW, attrs)
    except Exception:
        pass  # the pump drops any redundant CR that slips through anyway
    return read_fd, write_fd


def _pump(read_fd, out_channel):
    # A carriage return immediately next to a newline carries no information:
    # after a '\n' the cursor is already at the start of a line. Drop those, so
    # each line of remote output renders as exactly one line here.
    #
    # This has to happen on this side of the wire and not in the Xeda of the
    # remote: the remote may run an older Xeda whose `run_process` prints
    # each already-newline-terminated line with `end="\r"`, emitting "\n\r" per
    # line, which made every line of tool output come out with a
    # blank line after it. The Xeda installed over there cannot be patched, but
    # this code is shipped from here, so it works against any remote version.
    #
    # A LONE '\r' is left untouched: tools use it to redraw a progress line in
    # place, and dropping it would turn a progress bar into a wall of lines.
    after_newline = False
    while True:
        try:
            data = os.read(read_fd, 4096)
        except OSError:
            break  # EIO: last writer of the pty slave closed == EOF
        if not data:
            break
        text = data.decode("utf-8", "replace")
        # ...also when the '\n' ended the previous chunk and the '\r' starts
        # this one. Only one flag of state is needed, so no output is ever held
        # back waiting for more input, and streaming stays live.
        if after_newline:
            text = text.lstrip("\r")
        after_newline = text.endswith("\n")
        text = text.replace("\n\r", "\n").replace("\r\n", "\n")
        if text:
            out_channel.send(text)
    out_channel.close()


outchan = channel.gateway.newchannel()
errchan = channel.gateway.newchannel()

out_r, out_w = _open_stream_pair()
err_r, err_w = _open_stream_pair()

# keep the worker's original fds so they can be restored on teardown
saved_out, saved_err = os.dup(1), os.dup(2)

sys.stdout.flush()
sys.stderr.flush()
os.dup2(out_w, 1)
os.dup2(err_w, 2)
os.close(out_w)
os.close(err_w)

pumps = [
    threading.Thread(target=_pump, args=(out_r, outchan), daemon=True),
    threading.Thread(target=_pump, args=(err_r, errchan), daemon=True),
]
for pump in pumps:
    pump.start()

channel.send((outchan, errchan))

channel.receive()  # blocks until the local side signals the run is over

# Restore the original fds. That drops the last writer of each pty slave, so
# the pumps see EOF and drain whatever is still buffered before exiting.
sys.stdout.flush()
sys.stderr.flush()
os.dup2(saved_out, 1)
os.dup2(saved_err, 2)
os.close(saved_out)
os.close(saved_err)
for pump in pumps:
    pump.join(10)
os.close(out_r)
os.close(err_r)

channel.send("stopped")  # local side waits for this, so no output is lost
"""


class RemoteRunner(FlowLauncher):
    class Settings(FlowLauncher.Settings):
        clean: bool = True
        backups: bool = False
        #: Always true. The results of each run hash are mirrored in a directory of their own
        #: (`<design>/<flow>_<flowrun_hash>`), which no remote run with other settings shares,
        #: and no local run in the default layout.
        hashed_run_dirs: Literal[True] = True

    @with_debug_logging
    def run_remote(
        self,
        design: Union[str, Path, Design],
        flow_name: str,
        host: str,
        user: Optional[str] = None,
        port: Optional[int] = None,
        flow_settings=None,
        xedaproject: str | Path | None = None,
        design_overrides: Iterable[str] | Mapping[str, Any] | None = None,
        design_allow_extra: bool = False,
        target: str | None = None,
    ):
        """Run a flow of a design remotely and return its results.

        `target` selects one of the `targets` of the design here, before anything ships: the
        remote is sent the design that the target yields."""
        # Nothing is loaded yet: a request that fails before its design loads reports none.
        self.target = None
        self.design_name = None
        # A chain, and an input binding of the requested node, are local requests. They are
        # refused first, as `dse` refuses them, before the design is loaded, which may clone a
        # Git dependency into the run root or fail on a design file that is missing.
        self._refuse_unaccepted_request(flow_name, flow_settings or [], {}, xedaproject)
        if target is not None and isinstance(design, Design):
            raise ValueError(
                f"target {target!r} was asked for, but the design is already built: select "
                "the target where the design is loaded (`Design.from_file(..., target=...)`)"
            )
        # The design file given, when `design` names one: never the destination of an output.
        given_file = Path(design) if isinstance(design, (str, Path)) else None
        if design_overrides is None:
            design_overrides = {}
        elif not isinstance(design_overrides, Mapping):
            design_overrides = settings_to_dict(list(design_overrides))

        def load(design: Union[str, Path, Design]) -> tuple[Design, Optional[Path], Any]:
            """Return the design and the flow sections of the project.

            They are loaded as a local launch loads them."""
            project_path: Optional[Path] = None
            project_flow_settings: Mapping[str, Any] | None = None
            # As a local launch does: a Git dependency without a directory of its own is cloned
            # into the run root, which is asked for only then.
            # A remote flow always runs from a clean directory on the remote (`clean=True`),
            # but the local design generator is loaded before shipping and follows the explicit
            # rebuild request of the caller. Do not let the default `clean` setting of
            # RemoteRunner force it on every remote invocation.
            with loading_in_run_root(self.load_run_root, self.settings.rebuild_all):
                if isinstance(design, (str, Path)):
                    design_path = Path(design)
                    # The local runner's rule: a design-file suffix means a file, which then
                    # loads or is reported as it is, never a design name to look up in a project.
                    standalone = names_a_design_file(design_path)
                    project_path = resolve_project_file(xedaproject)
                    project = None
                    if project_path is not None:
                        project = XedaProject.from_file(
                            project_path,
                            skip_designs=standalone,
                            design_overrides=dict(design_overrides),
                            design_allow_extra=design_allow_extra,
                        )
                        project_flow_settings = project.flows

                    if standalone:
                        design = Design.from_file(
                            design_path,
                            overrides=dict(design_overrides),
                            allow_extra=design_allow_extra,
                            target=target,
                        )
                    elif project is not None:
                        selected = project.get_design(str(design), target)
                        if selected is None:
                            raise ValueError(
                                f"Design {str(design)!r} not found in {project_path}. "
                                f"Available designs: {', '.join(project.design_names)}"
                            )
                        design = selected
                    else:
                        raise FileNotFoundError(
                            f"Design file {design_path} does not exist and no project file was "
                            "found"
                        )
                else:
                    project_path = resolve_project_file(xedaproject)
                    if project_path is not None:
                        project = XedaProject.from_file(project_path, skip_designs=True)
                        project_flow_settings = project.flows
            assert isinstance(design, Design)
            return design, project_path, project_flow_settings

        def preflight(
            design: Design,
            project_path: Optional[Path],
            project_flow_settings: Any,
            flow_name: str,
            flow_settings: Any,
            identify_design: bool = True,
        ) -> tuple[type[Flow], str, Plan, Flow.Settings, dict[str, Any]]:
            """Compose, resolve and check the request before anything is shipped."""
            self.target = design.target
            self.design_name = design.name
            # Where a project's settings come from, named in messages even when there is none.
            project_label = project_path or Path(PROJECT_FILE_NAMES[0])
            flow_class = get_flow_class(flow_name)
            flow_name = flow_class.name

            def flow_class_if_known(name: str):
                try:
                    return get_flow_class(name)
                except FlowNotFoundError:
                    return None

            # The same layering as a local run: the command line wins over the design file.
            cli_sections, flow_settings = command_line_sections(
                flow_settings, flow_class, flow_class_for=flow_class_if_known
            )
            # Bindings are taken out of every origin exactly as a local run does, before Settings
            # sees them. A remote run cannot carry them yet: one the request reaches is refused
            # below, once its graph is known; one saved for another flow is simply not used.
            project_sections, project_bindings = split_bindings(
                project_flow_settings or {}, location="the project file"
            )
            design_sections, design_bindings = split_bindings(design.flow, location="the design")
            command_line, cli_bindings = split_bindings(
                merge_flow_sections(
                    cli_sections, {flow_name: flow_settings}, flow_class_for=flow_class_if_known
                ),
                location=COMMAND_LINE_ORIGIN,
                kind="cli",
            )
            binding_layers = [project_bindings, design_bindings, cli_bindings]
            cli_sections, _cli_only = split_bindings(cli_sections, location=COMMAND_LINE_ORIGIN)
            flow_settings = {key: value for key, value in flow_settings.items() if key != "inputs"}
            origins = [project_sections, design_sections, cli_sections]
            sections = merge_flow_sections(*origins, flow_class_for=flow_class_if_known)
            flow_settings = compose_flow_settings(flow_class, origins, flow_settings)
            # Agree and validate before identity, input/delivery preflight and shipping.
            plan = self.resolve(
                flow_class,
                design,
                flow_settings,
                sections,
                origins=[
                    (str(project_label.absolute()), origins[0]),
                    (str(given_file.absolute()) if given_file else "the design", origins[1]),
                ],
                command_line=command_line,
                identify_design=identify_design,
            )
            require_no_bindings(binding_layers, [node.node_key for node in plan.nodes])
            input_settings = plan.node(flow_name).settings
            sections = {
                **sections,
                **{node.name: as_recorded(node.settings) for node in plan.nodes},
            }
            flow_settings = as_recorded(input_settings)
            # and not on the remote: a path that the flow writes and that leads out of its run
            # directory, a missing setting, and a design that the flow cannot run are known
            # before anything is shipped, and a remote on an older release may not check them.
            problems = written_path_problems(input_settings)
            if problems:
                raise FlowSettingsError(
                    [(key, message, None, "value_error") for key, message in problems],
                    flow_class.Settings,
                )
            flow_class.check_required_settings(input_settings)
            # The design the node is planned to read: a Bluespec design that a converter turns
            # into Verilog on the remote is checked as the Verilog it will be.
            flow_class.check_design_supported(
                planned_design_of(plan, plan.node(flow_name), design, self.run_path_of),
                input_settings,
            )
            located = deliverable_locations(input_settings)
            if located:
                key, path = located[0]
                raise DeliveryError(
                    f"`{key}` names a location ({path}): with --remote, leave it a name in the run "
                    "directory (or unset) and receive the outputs with --outputs-to DIR"
                )
            # The output names, checked as a local launch checks them: none that Xeda keeps (the
            # `results.json` of the remote itself), no two outputs under one name. No location is
            # left to split.
            split_deliveries(input_settings, design.name)
            outputs_to = self.settings.outputs_to
            if outputs_to is not None:
                # Before the mirror, the run root or a connection, as a local launch refuses it.
                refuse_outputs_to_a_programmer(plan, plan.node(flow_name), outputs_to, remote=True)
            return flow_class, flow_name, plan, input_settings, sections

        # What needs only the design's declarations is refused before a generator runs, as a
        # local launch refuses it (`FlowLauncher._judged_request`): the declared design is
        # checked first when the load would run a generator.
        loaded: tuple[Design, Optional[Path], Any] | None = None
        with deferring_load_side_effects() as deferred:
            try:
                loaded = load(design)
            except Exception as error:
                if not deferred.failure_may_depend_on_deferred_output(error):
                    raise
        if deferred.deferred:
            if loaded is not None and deferred.complete:
                preflight(*loaded, flow_name, flow_settings, identify_design=False)
            loaded = load(design)
        assert loaded is not None
        design, project_path, project_flow_settings = loaded
        flow_class, flow_name, plan, input_settings, sections = preflight(
            design, project_path, project_flow_settings, flow_name, flow_settings
        )
        # The mirror is named by the requested node's identity, as a local hashed run
        # directory is: the plan's, which counts its settings and where its inputs come from
        # (so two configurations of a producer are two mirrors). The remote resolves the same
        # request, and its `flow_hash` is compared with this one.
        flowrun_hash = plan.node(flow_name).flowrun_hash
        settings_hash = plan.node(flow_name).settings_hash
        # As a local run counts the design: by the parts and source types that the flow reads.
        design_hash = design.parts_hash(
            flow_class.design_parts, flow_class.design_scope(plan.node(flow_name).settings)
        )
        outputs_to = self.settings.outputs_to
        # The local mirror: always `<design>[/<target>]/<flow>_<flowrun_hash>`
        # (`Settings.hashed_run_dirs`), so its delivery record belongs to this run hash.
        run_path = self.get_flow_run_path(
            design.name, flow_name, flowrun_hash, target=design.target
        )
        # Every input that this side can name, as a local launch has them: never a destination.
        launch_inputs = [
            path.resolve()
            for path in (given_file, project_path)
            if path is not None and path.is_file()
        ]
        delivery = Deliveries(
            run_path,
            self.run_root,
            inputs=remote_read_inputs(
                design,
                input_settings,
                sections,
                launch_inputs,
                flow_class=flow_class,
                run_path=run_path,
                run_root=self.run_root,
            ),
            overwrite=self.settings.overwrite_outputs,
            confirm=self.confirm_overwrite,
        )
        previous = recorded_artifacts(run_path / "results.json")
        # The directory itself, not only a file predicted from the artifacts of the last run.
        # As on a local launch, a location becomes a concrete `Delivery` only once its tool has
        # run and reported an artifact. Without this check, a run root or an input named by
        # `--outputs-to` is refused only after the remote run has already completed.
        delivery.check_outputs_to(outputs_to)
        delivery.check(outputs_to_deliveries(previous, run_path / "artifacts", outputs_to))

        host_split = host.split(":")
        if port is None and len(host_split) == 2 and host_split[1].isnumeric():
            host = host_split[0]
            port = int(host_split[1])
        log.info(
            "Connecting to %s%s%s...", f"{user}@" if user else "", host, f":{port}" if port else ""
        )
        conn = Connection(host=host, user=user, port=port)
        with contextlib.ExitStack() as connected:
            # However the run ends, be it a refused delivery or a failed fetch, the gateway is
            # closed, then the connection.
            connected.callback(conn.close)
            log.info("logging in...")
            remote_env = get_login_env(conn)
            remote_env_path = remote_env.get("PATH", "")
            remote_home = remote_env.get("HOME", ".")
            log.info("Remote PATH=%s HOME=%s", remote_env_path, remote_home)

            remote_xeda = Path(remote_home) / ".xeda"
            if not Transfer(conn).is_remote_dir(str(remote_xeda)):

                conn.sftp().mkdir(str(remote_xeda))
            remote_xeda_run = remote_xeda / "remote_run"
            if not Transfer(conn).is_remote_dir(str(remote_xeda_run)):
                conn.sftp().mkdir(str(remote_xeda_run))
            # Use a timestamped subdirectory to avoid any race conditions and also have the
            # chronology clear.
            remote_path = str(remote_xeda_run / datetime.now().strftime("%y%m%d%H%M%S%f"))
            if not Transfer(conn).is_remote_dir(remote_path):
                conn.sftp().mkdir(remote_path)
            assert Transfer(conn).is_remote_dir(remote_path)
            conn.sftp().chdir(remote_path)

            ssh_opt = f"{host}"
            if user:
                ssh_opt = f"{user}@{ssh_opt}"
            if port:
                ssh_opt += f" -p {port}"

            python_exec = "python3"

            spec = {
                "ssh": ssh_opt,
                "chdir": remote_path,
                "env:PATH": remote_env_path,
                "python": python_exec,
            }
            gw = execnet.makegateway("//".join([f"{k}={v}" for k, v in spec.items()]))
            connected.callback(gw.exit)
            # `python` is resolved by the non-login shell of the remote. `env:PATH` above is
            # applied only once that interpreter runs. So which Xeda answers is not a given: ask.
            (
                platform,
                version_info,
                remote_python,
                remote_xeda,
                remote_xeda_at,
                remote_protocol,
            ) = gw.remote_exec(REMOTE_PROBE).receive()
            version_info_str = ".".join(str(v) for v in version_info)
            log.info("Remote host:%s (%s python:%s)", host, platform, version_info_str)
            log.info(
                "Remote Xeda: %s at %s (%s), protocol %s",
                remote_xeda,
                remote_xeda_at,
                remote_python,
                remote_protocol,
            )
            # The worker that runs remotely is this installed xeda package, so its interpreter
            # must satisfy the same floor as pyproject.toml, and not a floor for transport only.
            check_remote_python(version_info)
            check_remote_xeda(remote_xeda, remote_xeda_at, remote_python, remote_protocol)

            # Only now that the remote can read it.
            sections = {**sections, flow_name: as_recorded(input_settings)}
            path_identities: dict[str, str] = {}
            zip_file, design_file = send_design(
                design,
                conn,
                remote_path,
                all_flows_settings=sections,
                run_root=self._run_root,
                path_identities=path_identities,
                planned_flows={node.name for node in plan.nodes},
            )

            # From the first write to the mirror to its delivery, the mirror is this run's
            # alone: a local `--hashed-run-dirs` launch of the same settings, or another remote
            # run, takes the same lock (`run_dir_lock`), so no two of them mix their files.
            connected.enter_context(run_dir_lock(run_path, self.run_root))
            run_path.mkdir(parents=True, exist_ok=True)
            # The local mirror holds the remote run's records, never a local run's: no trace left
            # there by a local run may vouch for them.
            remove_trace(run_path)

            settings_json = run_path / "settings.json"
            results_json_path = run_path / "results.json"

            log.info("dumping input settings to %s", settings_json)
            all_settings = dict(
                design=design,
                design_hash=design_hash,
                rtl_fingerprint=design.rtl_fingerprint,
                rtl_hash=design.rtl_hash,
                flow_name=flow_name,
                flow_settings=input_settings,
                effective_flow_settings=input_settings,
                xeda_version=__version__,
                flowrun_hash=flowrun_hash,
                settings_hash=settings_hash,
            )
            dump_json(all_settings, settings_json, backup=self.settings.backups)
            results = None
            written_on_remote = None

            # Stream the output of the remote flow (its own, and that of every tool it
            # spawns) back to this terminal live, line by line, as it is produced.
            stream_channel = gw.remote_exec(STREAM_OUTPUT_SETUP)
            outchan, errchan = stream_channel.receive()
            # The remote flow's stdout is tool output: it must follow the same redirection as a
            # local tool's, or it corrupts a `--json` document.
            outchan.setcallback(
                RemoteLogger(tool_output_stream(), label="stdout").cb, endmarker=None
            )
            errchan.setcallback(RemoteLogger(sys.stderr, label="stderr").cb, endmarker=None)

            try:
                results_channel = gw.remote_exec(
                    remote_runner,
                    remote_path=remote_path,
                    zip_file=zip_file,
                    flow=flow_name,
                    design_file=design_file,
                    flow_settings={},
                    path_identities=path_identities,
                )
                if not results_channel.isclosed():
                    results_str = results_channel.receive()
                    if results_str:
                        results = json.loads(results_str)
                    # Then which of the artifacts of a failed run the remote vouches its run wrote.
                    written_on_remote = json.loads(results_channel.receive())
                results_channel.waitclose()
            except execnet.gateway_base.RemoteError as e:
                log.critical("Remote exception: %s", e.formatted)
            finally:
                # Tear the redirection down and wait for the remote pumps to drain, so that
                # trailing output cannot be lost, and cannot land in the middle of the results
                # table printed below. This is best effort: if the remote died, the teardown
                # fails too, and that must not mask the actual failure.
                try:
                    stream_channel.send("stop")
                    stream_channel.receive()
                    stream_channel.waitclose()
                except (EOFError, OSError, execnet.gateway_base.RemoteError) as e:
                    log.debug("Could not cleanly stop remote output streaming: %s", e)

            if results:
                if (
                    results.get("success")
                    # The `flow_hash` of the remote is the identity of its node. Another one means
                    # that it resolved another configuration of this flow or of one of its
                    # producers.
                    and results.get("flow_hash") != flowrun_hash
                ):
                    raise RemoteIncompatible("The remote resolved a different request identity")
                print_results(
                    results=results,
                    title=f"Results of flow:{flow_name} design:{design.name}",
                    skip_if_false={"artifacts", "reports"},
                )

                artifacts = results.get("artifacts")
                remote_run_path = results.get("run_path")
                if not results.get("success"):
                    # Partial records from MissingOutput are not a successful hand-over. The
                    # diagnostics of a failed run still travel through the vouched artifact path.
                    results.pop("outputs", None)
                declared = results.get("outputs") or {}
                output_paths = [str(path) for path in declared_output_files(results)]
                remote_deliverables = (
                    {"artifacts": artifacts or {}, "outputs": output_paths}
                    if output_paths
                    else artifacts
                )
                for recorded in declared.values():
                    for entry, key in recorded_files(recorded):
                        remote_output = Path(entry[key])
                        if (
                            not remote_run_path
                            or not remote_output.is_absolute()
                            or ".." in remote_output.parts
                            or not remote_output.is_relative_to(Path(remote_run_path))
                        ):
                            raise DeliveryError(
                                "A declared remote output is outside its run directory"
                            )

                # Keep the local settings document in the same shape as a local run. Its input
                # and design stay local (and therefore re-runnable here); only the effective
                # settings, which can be known only after the remote flow's `init()`, come back
                # from the remote run directory. Older remote Xeda versions recorded those under
                # `flow_settings`.
                if remote_run_path:
                    try:
                        remote_settings_path = str(Path(remote_run_path) / "settings.json")
                        with conn.sftp().open(remote_settings_path, "r") as remote_settings_file:
                            remote_settings = json.load(remote_settings_file)
                        effective_settings = remote_settings.get(
                            "effective_flow_settings", remote_settings.get("flow_settings")
                        )
                        if effective_settings is not None:
                            all_settings["effective_flow_settings"] = effective_settings
                            dump_json(all_settings, settings_json, backup=False)
                    except (OSError, ValueError, TypeError) as e:
                        log.warning(
                            "Could not read effective settings from remote run %s: %s",
                            remote_run_path,
                            e,
                        )

                local_artifacts_dir = run_path / "artifacts"

                if remote_run_path and (artifacts or output_paths):
                    assert isinstance(remote_run_path, str)
                    fetched = _transfer_artifacts(
                        conn,
                        remote_deliverables,
                        remote_run_path,
                        local_artifacts_dir,
                        succeeded=bool(results.get("success")),
                        flow_name=flow_name,
                        written_on_remote=written_on_remote,
                    )
                    results["artifacts"] = fetched["artifacts"] if output_paths else fetched
                    rewrites = dict(zip(output_paths, fetched["outputs"])) if output_paths else {}
                    for recorded in declared.values():
                        for entry, key in recorded_files(recorded):
                            remote_output = Path(entry[key])
                            local_output = Path(rewrites[entry[key]])
                            if content_digest(local_output) != entry["sha"]:
                                raise DeliveryError(
                                    f"Declared remote output {remote_output} changed during transfer"
                                )
                            entry[key] = str(local_output)

                # The remote run_path does not exist locally. Report the path of the local copy
                # instead.
                if "run_path" in results:
                    results["run_path"] = str(run_path)

                dump_json(results, results_json_path, backup=True)
                log.info("Results written to %s", results_json_path)
                # After the records of the mirror are whole: a refused delivery leaves them as they
                # are.
                self._deliver_fetched(
                    delivery, results, remote_deliverables, remote_run_path, local_artifacts_dir
                )
            return results

    def _deliver_fetched(
        self,
        delivery: Deliveries,
        results: Dict[str, Any],
        remote_artifacts: Any,
        remote_run_path: Optional[str],
        local_artifacts_dir: Path,
    ) -> None:
        """Deliver `--outputs-to` from the artifacts fetched into `local_artifacts_dir`."""
        deliver_fetched(
            delivery,
            results,
            remote_artifacts,
            remote_run_path,
            local_artifacts_dir,
            outputs_to=self.settings.outputs_to,
        )
