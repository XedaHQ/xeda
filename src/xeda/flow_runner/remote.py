import json
import logging
import os
import re
import socket
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, Optional, Tuple, Union

import execnet
from fabric import Connection
from fabric.transfer import Transfer

from ..design import Design, DesignSource, DVSettings, FileResource, names_a_design_file
from ..flow import flowrun_hash as flow_run_hash
from ..proc_utils import tool_output_stream
from ..utils import (
    XedaException,
    dump_json,
    json_encodable,
    semantic_hash,
    settings_to_dict,
)
from ..version import __version__
from ..xedaproject import XedaProject
from .default_runner import FlowLauncher, FlowNotFoundError, get_flow_class, print_results
from .settings_layers import flow_settings_from_sections, merge_flow_sections, merge_layers

log = logging.getLogger(__name__)

REMOTE_PYTHON_MIN_VERSION = (3, 11, 0)


class RemoteIncompatible(XedaException):
    """The remote host cannot run what this side would send it: reported before anything is
    shipped, as the user error it is rather than as a traceback."""


def check_remote_python(version_info: tuple[Any, ...]) -> None:
    """Fail early and clearly when the worker cannot run this xeda package."""
    if version_info[:3] < REMOTE_PYTHON_MIN_VERSION:
        required = ".".join(str(part) for part in REMOTE_PYTHON_MIN_VERSION)
        found = ".".join(str(part) for part in version_info)
        raise RemoteIncompatible(
            f"Python {required} or newer is required on the remote, but found {found}"
        )


#: The oldest xeda a remote may run. The remote loads the design archive `send_design` builds
#: with *its* xeda, which forbids unknown keys, so the archive must stay loadable by this release
#: (`tests/test_remote_run.py` pins the keys it accepts).
REMOTE_XEDA_MIN_VERSION = (0, 4, 0)

# Runs on the remote first, like `STREAM_OUTPUT_SETUP`: stdlib only, and it never imports xeda,
# so a missing or broken install is reported rather than tripped over. `find_spec` locates the
# package without executing it; the location names *which* install answered, which matters when
# a stale checkout shadows the one the user upgraded.
REMOTE_PROBE = """
import sys
from importlib import metadata, util
try:
    installed = metadata.version("xeda")
except metadata.PackageNotFoundError:
    installed = None
spec = util.find_spec("xeda")
channel.send((
    sys.platform,
    tuple(sys.version_info),
    sys.executable,
    installed,
    spec.origin if spec is not None else None,
))
"""


def release_tuple(version: str) -> tuple[int, ...]:
    """The release a version string names: `"0.4.1.dev0+g9d1eb61"` -> `(0, 4, 1)`."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def check_remote_xeda(found: Optional[str], location: Optional[str], python: str) -> None:
    """Fail early and clearly when the remote's xeda cannot run what this side sends.

    `found` is the version of the xeda that `python` on the remote imports (None when it imports
    none) and `location` where that package lives. Without this check a version mismatch
    surfaces as whatever the old loader happens to choke on first -- a 0.2 remote reported
    `rtl.sources: unhashable type: 'dict'`.

    The floor is `REMOTE_XEDA_MIN_VERSION`, the oldest release that loads the archive. A remote
    on another release line than this side is only warned about: it reads the design, but a
    setting one side knows and the other does not is rejected there, with an error naming it.
    """
    required = ".".join(str(part) for part in REMOTE_XEDA_MIN_VERSION)
    if found is None:
        raise RemoteIncompatible(
            f"{python} on the remote imports no xeda: install xeda {required} or newer for that "
            "interpreter (it is started by a non-login shell, so it may not be the one on your "
            "login PATH)"
        )
    if release_tuple(found) < REMOTE_XEDA_MIN_VERSION:
        raise RemoteIncompatible(
            f"{python} on the remote imports xeda {found} from {location}, which cannot read "
            f"the design this xeda ({__version__}) sends: xeda {required} or newer is required "
            "there. Upgrade that install -- or remove it, if it is a stale one shadowing a newer "
            "install (it is started by a non-login shell, so it may not be the one on your login "
            "PATH)"
        )
    if release_tuple(found)[:2] != release_tuple(__version__)[:2]:
        log.warning(
            "The remote runs xeda %s and this side %s: a setting only one of them knows will be "
            "rejected on the remote",
            found,
            __version__,
        )


def send_design(
    design: Design,
    conn,
    remote_path: str,
    all_flows_settings: Mapping[str, Any] | None = None,
) -> Tuple[str, str]:
    """Archive the design and transfer it to the remote host."""
    assert isinstance(conn, Connection)

    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_dir = Path(tmpdirname)
        zip_file = temp_dir / f"{design.name}.zip"
        log.info("Preparing design archive: %s", zip_file)
        # `mode="json"`: the fields kept as they are (`dependencies` and its `local_cache` /
        # `clone_dir`) hold `Path`s, which a python-mode dump leaves as objects for the encoder
        # to stringify by luck rather than by rule.
        new_design: Dict[str, Any] = {
            **design.model_dump(mode="json"),
            "design_root": None,
        }
        rtl: Dict[str, Any] = {}
        tb: Dict[str, Any] = {}
        remote_sources_path = Path(design.name) / "sources"
        design_file = temp_dir / f"{design.name}.xeda.json"
        # Every place in the archive, and the local file that goes there. One table for both
        # kinds of place -- flat among the sources, or where a file lies under the design root
        # -- so neither can be handed a place the other already holds.
        archive_entries: Dict[Path, Path] = {Path(design_file.name): design_file}
        flat_places: Dict[Path, Path] = {}

        def archived(resource: FileResource) -> Path:
            """Where `resource` lands among the archived sources, adding it the first time it is
            seen.

            Two files may share a name under different directories, and this part of the archive
            is flat, so a repeat name is disambiguated by content -- the same file listed twice
            still travels once.
            """
            local = resource.file.resolve()
            place = flat_places.get(local)
            if place is not None:
                return place
            place = remote_sources_path / local.name
            if place in archive_entries:
                digest = resource.content_hash[:8]
                place = remote_sources_path / f"{local.stem}_{digest}{local.suffix}"
                suffix = 2
                while place in archive_entries:
                    place = remote_sources_path / f"{local.stem}_{digest}_{suffix}{local.suffix}"
                    suffix += 1
            archive_entries[place] = local
            flat_places[local] = place
            return place

        def archived_in_place(local: Path, relative: Path) -> bool:
            """Archive `local` at `relative`, where it lies under the design root, so that the
            remote finds it where this side does. False when that place holds another file."""
            return archive_entries.setdefault(relative, local.resolve()) == local.resolve()

        def packaged_sources(sources: Iterable[DesignSource]) -> list[Dict[str, Any]]:
            """The sources as the remote has to see them: under the design root, each at its
            own place relative to the root, as it is here -- a Verilog `include` finds the header
            beside the including file first, so a flattened layout could build another design,
            and it would hash as another (`Design._source_fingerprint`). A source outside the
            root has no place there, and travels among the flat sources."""
            packaged: list[Dict[str, Any]] = []
            for src in sources:
                relative = design.relative_to_root(src.file)
                if relative is not None and archived_in_place(src.file, relative):
                    place = relative
                else:
                    place = archived(src)
                spec: Dict[str, Any] = {"file": place.as_posix()}
                if src.type is not None:
                    spec["type"] = str(src.type)
                if src.standard is not None:
                    spec["standard"] = src.standard
                if src.variant is not None:
                    spec["variant"] = src.variant
                packaged.append(spec)
            return packaged

        def packaged_parameters(dv: DVSettings) -> Dict[str, Any]:
            """The parameters as the remote has to see them.

            A parameter is text to the tool, but text naming a path on *this* machine names
            nothing on the remote, so a path is re-rooted from its value alone:

            - under the design root, it keeps its place relative to the root. An existing file
              is archived at that same place; a path with no file yet (an output) is only a
              place for the remote to write. Counted relative to the root on both sides, the
              remote hashes the design exactly as this side did.
            - elsewhere, an existing file is archived among the sources and repointed there.

            Either way it travels as a table (`{ file = ... }`, `{ path = ... }`), which the
            remote resolves against *its* design root rather than against whatever directory it
            runs in. Anything else is a value, and travels as it is.
            """
            parameters: Dict[str, Any] = {}
            for name, value in dv.parameters.items():
                parameters[name] = value
                if not (isinstance(value, str) and os.path.isabs(value)):
                    continue
                path = Path(value)
                relative = design.relative_to_root(path)
                if relative is not None and not path.exists():
                    parameters[name] = {"path": relative.as_posix()}
                elif relative is not None and path.is_file() and archived_in_place(path, relative):
                    parameters[name] = {"file": relative.as_posix()}
                elif path.is_file():
                    parameters[name] = {"file": archived(FileResource(path)).as_posix()}
            return parameters

        rtl["sources"] = packaged_sources(design.rtl.sources)
        rtl["defines"] = design.rtl.defines
        rtl["attributes"] = design.rtl.attributes
        rtl["parameters"] = packaged_parameters(design.rtl)
        rtl["top"] = design.rtl.top
        rtl["clocks"] = [clk.model_dump() for clk in design.rtl.clocks]
        tb["sources"] = packaged_sources(design.tb.sources)
        tb["top"] = design.tb.top
        tb["cocotb"] = design.tb.cocotb.model_dump() if design.tb.cocotb is not None else None
        if design.tb.uut:
            tb["uut"] = design.tb.uut
        if design.tb.parameters:
            tb["parameters"] = packaged_parameters(design.tb)
        if design.tb.defines:
            tb["defines"] = design.tb.defines
        new_design["rtl"] = rtl
        new_design["tb"] = tb
        # The remote does not receive the local project file. Materialize its merged flow
        # sections into the shipped design so dependency flows see exactly the same settings as
        # a local run; the explicit top-flow settings still take precedence on the remote CLI.
        new_design["flow"] = (
            dict(all_flows_settings) if all_flows_settings is not None else design.flow
        )
        with open(design_file, "w") as f:
            # The same encoder `settings.json` uses: a pydantic model serializes through
            # pydantic so its fields' serializers run. The chain this replaced probed `obj.json`
            # and `obj.__json_encoder__`, both pydantic *v1* API -- in v2 `obj.json` is a
            # deprecated bound method, which the encoder would have handed to `json` as a value.
            json.dump(new_design, f, default=json_encodable)
        with zipfile.ZipFile(zip_file, mode="w") as archive:
            for place, local in archive_entries.items():
                archive.write(local, arcname=place)

        with zipfile.ZipFile(zip_file, mode="r") as archive:
            archive.printdir()

        log.info("Transfering design to %s in %s", conn.host, remote_path)
        conn.put(zip_file, remote=remote_path)
        return zip_file.name, design_file.name


def remote_runner(channel, remote_path, zip_file, flow, design_file, flow_settings, env=None):
    # pylint: disable=import-outside-toplevel,reimported,redefined-outer-name
    """Unpack and execute a flow in the remote Python process."""
    import json
    import os
    import zipfile
    from pathlib import Path

    try:
        from xeda.flow_runner import DefaultRunner
    except ImportError:
        # FIXME: we need to be able to create/use virtual env or change our remote approach
        raise Exception("XEDA Python package not found. Please install it using pip.")

    os.chdir(remote_path)
    if env:
        for k, v in env.items():
            os.environ[k] = v
    if channel.isclosed():
        return

    with zipfile.ZipFile(zip_file, mode="r") as archive:
        archive.extractall(path=remote_path)

    # xeda_run_dir = Path("remote_run").joinpath(datetime.now().strftime("%y%m%d%H%M%S%f"))
    xeda_run_dir = str(Path.cwd())
    launcher = DefaultRunner(
        xeda_run_dir,
        cached_dependencies=True,
        backups=False,
        cleanup_before_run=True,
        incremental=False,
        post_cleanup=False,
        display_results=False,
    )
    # NOTE: this function's *source* is shipped to the remote host and executed
    # there against whatever xeda version is installed remotely, so it must not
    # depend on APIs newer than that install. Live output streaming is set up
    # separately, at the file-descriptor level, by `STREAM_OUTPUT_SETUP` (pure
    # stdlib, no xeda involvement) -- see `RemoteRunner.run_remote`.
    f = launcher.run(
        flow,
        design=design_file,
        design_allow_extra=True,
        flow_settings=flow_settings,
    )
    # `json` raises on a key it cannot write (a tuple, a `Path`), which lost a run's results
    # entirely. This runs against the remote's xeda, which may predate `utils.with_json_keys`,
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
    """Echoes data received from a remote output-forwarding execnet channel to a
    local stream, live, as each message arrives."""

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
# It takes over the worker's stdout/stderr *file descriptors* (1 and 2) rather
# than the Python-level `sys.stdout`/`sys.stderr` objects, because that is what
# a spawned EDA tool actually inherits. execnet's own bootstrap
# (`execnet.gateway_base.init_popen_io`) has already `dup`ed the real fd 1 aside
# for its wire protocol and pointed fd 1 at /dev/null, so fds 1/2 are free for
# us to claim -- and anything the flow writes to them, directly or from any
# subprocess it spawns, then lands in our pty and is forwarded here line by
# line as it is produced.
#
# IMPORTANT: this runs against whatever xeda happens to be installed on the
# remote host, which may be older than the local one. It therefore uses only
# the standard library and must stay free of any xeda import, so that live
# output streaming never depends on the remote xeda version.
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
        # line-buffering -- the bytes themselves must come through untouched,
        # so what you see locally is what the tool actually printed.
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
    # This has to happen on this side of the wire rather than in the remote's
    # xeda: the remote may be running an older xeda whose `run_process` prints
    # each already-newline-terminated line with `end="\r"`, emitting "\n\r" per
    # line -- which is what made every line of tool output come out with a
    # blank line after it. We cannot patch the xeda installed over there, but
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
    ):
        """Execute a design flow remotely and return its results."""
        project_flow_settings: Mapping[str, Any] | None = None
        if design_overrides is None:
            design_overrides = {}
        elif not isinstance(design_overrides, Mapping):
            design_overrides = settings_to_dict(list(design_overrides))

        if isinstance(design, (str, Path)):
            design_path = Path(design)
            # The local runner's rule: a design-file suffix means a file, which then loads or
            # is reported as it is, never a design name to look up in a project.
            standalone = names_a_design_file(design_path)
            project_path = Path(xedaproject or "xedaproject.toml")
            project = None
            if project_path.exists():
                project = XedaProject.from_file(
                    project_path,
                    skip_designs=standalone,
                    design_overrides=dict(design_overrides),
                    design_allow_extra=design_allow_extra,
                )
                project_flow_settings = project.flows
            elif xedaproject is not None:
                raise FileNotFoundError(f"Cannot open xeda-project file: {project_path}")

            if standalone:
                design = Design.from_file(
                    design_path,
                    overrides=dict(design_overrides),
                    allow_extra=design_allow_extra,
                )
            elif project is not None:
                selected = project.get_design(str(design))
                if selected is None:
                    raise ValueError(
                        f"Design {str(design)!r} not found in {project_path}. Available designs: "
                        f"{', '.join(project.design_names)}"
                    )
                design = selected
            else:
                raise FileNotFoundError(
                    f"Design file {design_path} does not exist and no xedaproject was found"
                )
        else:
            project_path = Path(xedaproject or "xedaproject.toml")
            if project_path.exists():
                project = XedaProject.from_file(project_path, skip_designs=True)
                project_flow_settings = project.flows
            elif xedaproject is not None:
                raise FileNotFoundError(f"Cannot open xeda-project file: {project_path}")
        flow_class = get_flow_class(flow_name)
        flow_name = flow_class.name

        def flow_class_if_known(name: str):
            try:
                return get_flow_class(name)
            except FlowNotFoundError:
                return None

        # The same layering as a local run: the command line wins over the design file.
        sections = merge_flow_sections(
            project_flow_settings,
            design.flow,
            flow_class_for=flow_class_if_known,
        )
        flow_settings = merge_layers(
            flow_settings_from_sections(flow_class, sections),
            flow_settings,
            settings_cls=flow_class.Settings,
        )
        # Hashed exactly as a local run would be, from the validated settings.
        input_settings = flow_class.Settings.from_input(
            flow_settings, design_root=design.root_path, runner_cwd=Path.cwd()
        )
        # Here, not on the remote: a missing setting is known before anything is shipped.
        flow_class.check_required_settings(input_settings)
        flowrun_hash = flow_run_hash(
            flow_name,
            input_settings,
        )
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )

        host_split = host.split(":")
        if port is None and len(host_split) == 2 and host_split[1].isnumeric():
            host = host_split[0]
            port = int(host_split[1])
        log.info(
            "Connecting to %s%s%s...", f"{user}@" if user else "", host, f":{port}" if port else ""
        )
        conn = Connection(host=host, user=user, port=port)
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
        # use a timestamped subdirectory to avoid any race conditions and also have the chronology clear
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
        # `python` is resolved by the remote's *non-login* shell; `env:PATH` above is applied
        # only once that interpreter runs. So which xeda answers is not a given: ask.
        platform, version_info, remote_python, remote_xeda, remote_xeda_at = gw.remote_exec(
            REMOTE_PROBE
        ).receive()
        version_info_str = ".".join(str(v) for v in version_info)
        log.info("Remote host:%s (%s python:%s)", host, platform, version_info_str)
        log.info("Remote xeda: %s at %s (%s)", remote_xeda, remote_xeda_at, remote_python)
        # The remotely executed worker is this installed xeda package, so its interpreter must
        # satisfy the same floor as pyproject.toml rather than a historical transport-only floor.
        check_remote_python(version_info)
        check_remote_xeda(remote_xeda, remote_xeda_at, remote_python)

        # Only now that the remote can read it.
        zip_file, design_file = send_design(design, conn, remote_path, all_flows_settings=sections)

        run_path = self.get_flow_run_path(
            design.name,
            flow_name,
            design_hash,
            flowrun_hash,
        )
        run_path.mkdir(parents=True, exist_ok=True)

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
        )
        dump_json(all_settings, settings_json, backup=self.settings.backups)
        results = None

        # Stream the remote flow's output (its own, and that of every tool it
        # spawns) back to this terminal live, line by line, as it is produced.
        stream_channel = gw.remote_exec(STREAM_OUTPUT_SETUP)
        outchan, errchan = stream_channel.receive()
        # The remote flow's stdout is tool output: it must follow the same redirection as a
        # local tool's, or it corrupts a `--json` document.
        outchan.setcallback(RemoteLogger(tool_output_stream(), label="stdout").cb, endmarker=None)
        errchan.setcallback(RemoteLogger(sys.stderr, label="stderr").cb, endmarker=None)

        try:
            results_channel = gw.remote_exec(
                remote_runner,
                remote_path=remote_path,
                zip_file=zip_file,
                flow=flow_name,
                design_file=design_file,
                flow_settings=flow_settings,
            )
            if not results_channel.isclosed():
                results_str = results_channel.receive()
                if results_str:
                    results = json.loads(results_str)
            results_channel.waitclose()
        except execnet.gateway_base.RemoteError as e:
            log.critical("Remote exception: %s", e.formatted)
        finally:
            # Tear the redirection down and wait for the remote pumps to drain,
            # so trailing output can't be lost -- and so it can't land in the
            # middle of the results table printed below. Best-effort: if the
            # remote died, teardown will fail too, and that must not mask the
            # actual failure.
            try:
                stream_channel.send("stop")
                stream_channel.receive()
                stream_channel.waitclose()
            except (EOFError, OSError, execnet.gateway_base.RemoteError) as e:
                log.debug("Could not cleanly stop remote output streaming: %s", e)

        if results:
            print_results(
                results=results,
                title=f"Results of flow:{flow_name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

            artifacts = results.get("artifacts")
            artifacts_orig = artifacts
            remote_run_path = results.get("run_path")

            # Keep the local settings document in the same shape as a local run. Its input and
            # design stay local (and therefore re-runnable here); only the effective settings,
            # which can be known only after the remote flow's `init()`, come back from the remote
            # run directory. Older remote Xeda versions recorded those under `flow_settings`.
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

            if remote_run_path and artifacts:
                assert isinstance(remote_run_path, str)
                if isinstance(artifacts, (dict)):
                    artifacts = list(artifacts.values())
                local_artifacts_dir.mkdir(exist_ok=True, parents=True)
                num_transferred = 0
                # remote path -> local path, used to rewrite `results["artifacts"]` below
                remote_to_local: Dict[str, str] = {}

                # TODO: compress artifacts in a single Zip file before transfer, unzip after transfer
                # conn.sftp().chdir(remote_run_path)
                # artifacts_zipfile = run_path / "artifacts.zip"
                # with zipfile.ZipFile(artifacts_zipfile, mode="w") as archive:
                #     for f in artifacts:
                #         remote_path = f if os.path.isabs(f) else remote_run_path + "/" + f
                #         rel_path = os.path.relpath(f, remote_run_path) if os.path.isabs(f) else f
                #         archive.write(remote_path, arcname=rel_path)
                # log.info("Transferring artifacts to %s", local_artifacts_dir.relative_to(Path.cwd()))
                # conn.get(str(artifacts_zipfile), str(local_artifacts_dir / "artifacts.zip"))
                # with zipfile.ZipFile(artifacts_zipfile, mode="r") as archive:
                #     archive.extractall(path=local_artifacts_dir)
                # artifacts_zipfile.unlink()

                for f in artifacts:
                    remote_path = f if os.path.isabs(f) else remote_run_path + "/" + f
                    rel_path = os.path.relpath(f, remote_run_path) if os.path.isabs(f) else f
                    local_path = local_artifacts_dir / rel_path
                    if local_path.exists():
                        # backup = backup_existing(local_path)
                        # log.warning("Backed up exitsting artifact to %s", str(backup))
                        pass
                    elif not local_path.parent.exists():
                        local_path.parent.mkdir(parents=True)
                    assert local_path.is_relative_to(local_artifacts_dir)
                    result = conn.get(remote_path, str(local_path))
                    log.debug("Transferred artifact %s to %s", f, result.local)
                    remote_to_local[f] = str(local_path)
                    num_transferred += 1
                if num_transferred > 0:
                    # Not `relative_to(Path.cwd())`: the run directory need not be under the
                    # start directory (`--xeda-run-dir`, `XEDA_RUN_DIR`), and that raised.
                    log.info(
                        "Transferred %d artifact(s) to %s", num_transferred, local_artifacts_dir
                    )

                # rewrite reported artifact paths to point at the local copies, preserving
                # the original list/dict shape
                if isinstance(artifacts_orig, dict):
                    results["artifacts"] = {
                        k: remote_to_local.get(v, v) if isinstance(v, str) else v
                        for k, v in artifacts_orig.items()
                    }
                else:
                    results["artifacts"] = [
                        remote_to_local.get(f, f) if isinstance(f, str) else f
                        for f in artifacts_orig
                    ]

            # the remote run_path no longer exists locally; report the local copy's path instead
            if "run_path" in results:
                results["run_path"] = str(run_path)

            dump_json(results, results_json_path, backup=True)
            log.info("Results written to %s", results_json_path)
        gw.exit()
        conn.close()
        return results
