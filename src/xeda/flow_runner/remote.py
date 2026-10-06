import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import zipfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, Literal, Optional, Tuple, Union

import execnet
from fabric import Connection
from fabric.transfer import Transfer

from ..artifacts import (
    ArtifactPath,
    drop_unwritten_artifacts,
    iter_artifact_paths,
    map_artifact_paths,
)
from ..deliver import (
    Deliveries,
    DeliveryError,
    ReadInputs,
    deliverable_locations,
    outputs_to_deliveries,
    recorded_artifacts,
    split_deliveries,
)
from ..design import (
    Design,
    DesignSource,
    DVSettings,
    FileResource,
    loading_in_run_root,
    names_a_design_file,
)
from ..flow import Flow, FlowSettingsError
from ..flow.flow import written_path_problems
from ..flow.flow import map_keyed_path_leaves
from ..dataclass import BaseModel, written_role
from ..listing import VCS_METADATA, directory_files
from ..proc_utils import tool_output_stream
from ..utils import (
    XedaException,
    dump_json,
    json_encodable,
    location_free,
    location_roots,
    settings_to_dict,
)
from ..version import __version__
from ..xedaproject import PROJECT_FILE_NAMES, XedaProject, resolve_project_file
from .bindings import require_no_bindings, split_bindings
from .default_runner import (
    FlowLauncher,
    FlowNotFoundError,
    _get_flow_class_if_known,
    get_flow_class,
    print_results,
)
from .run_lock import run_dir_lock
from .settings_layers import command_line_sections, compose_flow_settings, merge_flow_sections
from .trace import remove_trace
from .trace_inputs import design_files, register_read_settings
from .outputs import declared_output_files
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

    A missing artifact of a successful run is an error. Of a failed run's, only those the
    remote vouched its run wrote (`written_on_remote`, as each is named in the results) are
    fetched and kept; the rest are dropped, as the launcher drops them
    (`drop_unwritten_artifacts`). The remote may run an older xeda, which lists every artifact
    its flow recorded whether or not the run wrote it, and a file there may be an earlier run's:
    only the remote can tell, from its own file system (`remote_runner`), so a file merely
    existing there proves nothing, and with no word from the remote none is kept."""
    remote_root = os.path.normpath(remote_run_path)

    def remote_path_of(artifact: ArtifactPath) -> str:
        named_path = os.fspath(artifact)
        return os.path.normpath(
            named_path if os.path.isabs(named_path) else os.path.join(remote_root, named_path)
        )

    if not succeeded:
        vouched = frozenset(written_on_remote or ())

        def written(artifact: ArtifactPath) -> bool:
            return os.fspath(artifact) in vouched

        artifacts = drop_unwritten_artifacts(artifacts, written, flow_name)

    local_dir.mkdir(exist_ok=True, parents=True)
    local_root = local_dir.resolve()
    remote_to_local: dict[str, str] = {}
    destinations: dict[Path, str] = {}

    for artifact in iter_artifact_paths(artifacts):
        named_path = os.fspath(artifact)
        remote_path = remote_path_of(artifact)
        if remote_path in remote_to_local:
            continue
        relative = Path(os.path.relpath(remote_path, remote_root))
        if ".." in relative.parts:
            # An absolute path or a relative ../ path outside the run must not escape the
            # local artifacts directory. The digest keeps same-named external files apart.
            digest = hashlib.sha256(remote_path.encode()).hexdigest()[:16]
            relative = Path("_external") / digest / Path(remote_path).name
        local_path = local_dir / relative
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


def remote_read_inputs(
    design: Design,
    input_settings: Flow.Settings,
    sections: Mapping[str, Any],
    launch_inputs: Sequence[Path],
    *,
    flow_class: type[Flow],
    run_path: Path,
    run_root: Path,
) -> ReadInputs:
    """The inputs a remote run's deliveries protect every file of its
    graph this side can name -- the design's files, the design and project file given, and what
    the read settings name of the requested flow `flow_class`, as the launch uses them
    (`input_settings`: its section with the command line's settings over it, the dependencies'
    settings nested in it included; never its section as written, since what the command line
    replaced there no flow of the run reads), and of every other flow's
    section sent with the design (`flows.<name>`, the project's merged in) as written, since
    which dependencies the remote launches, and with what, is not known here. A file, or a
    directory with every entry under it, as a local launch registers them
    (`trace_inputs.register_read_settings`, with `run_path`, the local mirror, and `run_root`
    for where the listings stop). A section that does not validate names nothing: that flow's
    remote run fails before anything is delivered."""
    inputs = ReadInputs([*design_files(design), *launch_inputs])
    register_read_settings(inputs, input_settings, run_path, run_root)
    for name, section in sections.items():
        section_class = _get_flow_class_if_known(name)
        if section_class is None or section_class is flow_class or not isinstance(section, Mapping):
            continue
        try:
            settings = section_class.Settings.from_input(section, **input_settings.context)
        except FlowSettingsError:
            continue
        register_read_settings(inputs, settings, run_path, run_root)
    return inputs


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


#: The release line a remote must run, and the remote protocol it must speak. Protocol 1, the first
#: released one (no earlier release carried a marker), is: canonical resolved settings, relocated
#: read inputs with their path identities, declared output records with checked hand-over,
#: current-run evidence for simulations, the FPGA build graph (`fpga_pack`, a programming-only
#: `openfpgaloader`), the identity rule (a node's `flow_hash` is its settings plus its ordered
#: resolved input origins, which this side names the mirror by and compares with the remote's),
#: the declared Vivado outputs, and `yosys`'s declared netlist with its ASIC configuration, a
#: bundled platform counted relative to xeda's installation. Raised together with
#: `xeda.REMOTE_PROTOCOL_VERSION` once per release cycle; development builds are not supported
#: remotes.
REMOTE_XEDA_MIN_VERSION = (0, 4, 4)
REMOTE_PROTOCOL_MIN_VERSION = 1

# Runs before shipping anything. Inspect the package this interpreter actually imports: installed
# distribution metadata alone can describe a different xeda shadowed by a stale checkout. A
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
    """The release a version string names: `"0.4.1.dev0+g9d1eb61"` -> `(0, 4, 1)`."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def check_remote_xeda(
    found: str | None, location: str | None, python: str, protocol: int = 0
) -> None:
    """Fail early and clearly when the remote's xeda cannot run what this side sends.

    `found` is the version of the xeda that `python` on the remote imports (None when it imports
    none) and `location` where that package lives. Without this check a version mismatch
    surfaces as whatever the old loader happens to choke on first -- a 0.2 remote reported
    `rtl.sources: unhashable type: 'dict'`.

    The remote needs the release line `REMOTE_XEDA_MIN_VERSION` and protocol
    `REMOTE_PROTOCOL_MIN_VERSION` (a xeda without a marker reports 0): one that resolves the
    same request otherwise reports another `flow_hash`, so it is refused here rather than failing
    on a hash mismatch after the run. Version alone cannot tell a release from a checkout.
    A compatible remote on another release line is warned about settings differences.
    """
    required = ".".join(str(part) for part in REMOTE_XEDA_MIN_VERSION)
    if found is None:
        raise RemoteIncompatible(
            f"{python} on the remote imports no xeda that can be used: upgrade the remote xeda "
            f"to a build with remote protocol support (xeda {required}, including dev builds, or newer; remote "
            f"protocol {REMOTE_PROTOCOL_MIN_VERSION} or newer) for that "
            "interpreter (it is started by a non-login shell, so it may not be the one on your "
            "login PATH)"
        )
    if (
        release_tuple(found) < REMOTE_XEDA_MIN_VERSION
        or type(protocol) is not int
        or protocol < REMOTE_PROTOCOL_MIN_VERSION
    ):
        raise RemoteIncompatible(
            f"{python} on the remote imports xeda {found} from {location} (remote protocol "
            f"{protocol}), which cannot run this xeda's request: upgrade the remote xeda to a "
            f"build with remote protocol support (xeda {required}, including dev builds, or newer; remote protocol "
            f"{REMOTE_PROTOCOL_MIN_VERSION} or newer). Remove that install if it shadows a newer "
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
    *,
    run_root: Path | None = None,
    path_identities: dict[str, str] | None = None,
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
        # Not `target`: the remote is sent the design a target yielded, as a design file is
        # written, and the name of the target is this side's to report.
        new_design: Dict[str, Any] = {
            **design.model_dump(mode="json", exclude={"target"}),
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
        sections = dict(all_flows_settings) if all_flows_settings is not None else design.flow

        def packaged_path(value: Any) -> Any:
            if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
                return value
            path = Path(value)
            if not path.is_absolute():
                choices = [design.root_path / path, Path.cwd() / path]
                path = next((p for p in choices if p.exists()), choices[0])
            local = path.resolve()
            relative = design.relative_to_root(path)
            if relative is None:
                digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
                relative = Path("_setting_inputs") / digest / local.name
            if path_identities is not None:
                roots = location_roots(design.root_path, Path.cwd())
                path_identities[str(Path(remote_path) / relative)] = str(
                    location_free(value, roots)
                )
            if local.is_file():
                archive_entries.setdefault(relative, local)
            elif local.is_dir():
                archive_entries.setdefault(relative, local)
                for entry in directory_files(
                    local, [run_root] if run_root else [], VCS_METADATA, follow_links=True
                ):
                    archive_entries.setdefault(relative / entry.relative_to(local), entry)
            return "$DESIGN_ROOT/" + relative.as_posix()

        def packaged_value(value: Any) -> Any:
            if isinstance(value, BaseModel):
                values = {}
                for name, field in type(value).model_fields.items():
                    if name not in value.model_fields_set:
                        continue
                    item = getattr(value, name)
                    if written_role(type(value), name) is None:
                        item = map_keyed_path_leaves(
                            item, field.annotation, lambda _key, leaf: packaged_path(leaf), name
                        )
                    values[name] = packaged_value(item)
                return values
            if isinstance(value, Mapping):
                return {key: packaged_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [packaged_value(item) for item in value]
            return value

        shipped_sections = {}
        for name, section in sections.items():
            cls = _get_flow_class_if_known(name)
            if cls is None:
                shipped_sections[name] = section
                continue
            try:
                model = cls.Settings.from_input(
                    section, design_root=design.root_path, runner_cwd=Path.cwd()
                )
            except FlowSettingsError:
                shipped_sections[name] = section
            else:
                # Preserve explicitness: canonical declared sections are complete already;
                # an undeclared section still contributes only its stated keys.
                rewritten = packaged_value(model)
                shipped_sections[name] = {key: rewritten[key] for key in model.model_fields_set}
        new_design["flow"] = shipped_sections
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

        log.info("Transferring design to %s in %s", conn.host, remote_path)
        conn.put(zip_file, remote=remote_path)
        return zip_file.name, design_file.name


def remote_runner(
    channel, remote_path, zip_file, flow, design_file, flow_settings, env=None, path_identities=None
):
    # pylint: disable=import-outside-toplevel,reimported,redefined-outer-name
    """Unpack and execute a flow in the remote Python process."""
    import json
    import os
    import zipfile
    from pathlib import Path

    try:
        from xeda.flow_runner import DefaultRunner
        from xeda.flow_runner.outputs import handed_over
        from xeda.flow.flow import using_path_identities
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

    # What the remote directory holds before the run -- every file's and directory's identity,
    # size and times, on the remote's own file system -- so that a failed run's artifacts can be
    # told from files that were already here if a worker lacks its write-reporting method
    # (`Flow.wrote_output`). The directory is fresh, so this is the design
    # archive's files; one it could not read proves nothing about what lies under it.
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

    # a run root of its own: the directory the archive was unpacked into holds its files
    run_root = str(Path.cwd() / "xeda_run")
    # The protocol floor guarantees these settings: every run starts clean, in a directory named
    # by its settings (`clean` implies `rebuild_all`).
    launcher = DefaultRunner(
        run_root,
        backups=False,
        post_cleanup=False,
        display_results=False,
        hashed_run_dirs=True,
        clean=True,
    )
    # NOTE: this function's *source* is shipped to the remote host and executed
    # there against a xeda satisfying the protocol floor. Live output streaming is set up
    # separately, at the file-descriptor level, by `STREAM_OUTPUT_SETUP` (pure
    # stdlib, no xeda involvement) -- see `RemoteRunner.run_remote`.
    try:
        with using_path_identities(path_identities or {}):
            f = launcher.run(
                flow,
                design=design_file,
                design_allow_extra=True,
                flow_settings=flow_settings,
            )
    except Exception:
        # The launcher records setup/dependency failures before re-raising. Return that document too;
        # failures before a requested flow was constructed still surface as remote errors.
        f = launcher.launched[-1] if launcher.launched else None
        if f is None or f.name != flow or f.results.get("success") is not False:
            raise
    if f is not None and f.results.get("success"):
        # Validate on the remote filesystem before any transfer can follow a tool's link.
        for output in f.results.get("outputs", {}):
            handed_over(f, output)
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

    # Which of a failed run's artifacts the run wrote, judged here, on the remote's own file
    # system -- this side never compares its clock or its files with the remote's: by the
    # remote flow's own record (`wrote_output`) where its xeda has one; otherwise, one under the
    # remote directory whose identity it did not hold before the run, or whose state changed
    # since. Anything else -- one outside it, or under a directory the snapshot could not
    # read -- is unknown, and not vouched for: this side drops it. Named as in the results.
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
        #: always: each settings variant's results are mirrored in a directory of its own,
        #: `<design>/<flow>_<flowrun_hash>`, which no remote run of other settings shares, nor a
        #: local run in the default layout
        hashed_run_dirs: Literal[True] = True

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
        """Execute a design flow remotely and return its results. `target` selects one of the
        design's `targets` here, before anything ships: the remote is sent the design the
        target yields."""
        # A chain, and an input binding of the requested node, are local requests: refused first,
        # as `dse` refuses them, before the design is loaded, which may clone a git dependency
        # into the run root or fail on a design file that is missing.
        self._refuse_unaccepted_request(flow_name, flow_settings or [], {}, xedaproject)
        if target is not None and isinstance(design, Design):
            raise ValueError(
                f"target {target!r} was asked for, but the design is already built: select "
                "the target where the design is loaded (`Design.from_file(..., target=...)`)"
            )
        # the design file given, when `design` names one: never an output's destination
        given_file = Path(design) if isinstance(design, (str, Path)) else None
        project_flow_settings: Mapping[str, Any] | None = None
        if design_overrides is None:
            design_overrides = {}
        elif not isinstance(design_overrides, Mapping):
            design_overrides = settings_to_dict(list(design_overrides))

        # As a local launch does: a git dependency without a directory of its own is cloned
        # into the run root, which is asked for only then.
        # A remote flow is always executed fresh remotely (`clean=True`), but the local design
        # generator is loaded before shipping and follows the caller's explicit rebuild request.
        # Do not let RemoteRunner's default clean setting force it on every remote invocation.
        with loading_in_run_root(self.load_run_root, self.settings.rebuild_all):
            if isinstance(design, (str, Path)):
                design_path = Path(design)
                # The local runner's rule: a design-file suffix means a file, which then loads or
                # is reported as it is, never a design name to look up in a project.
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
                            f"Design {str(design)!r} not found in {project_path}. Available designs: "
                            f"{', '.join(project.design_names)}"
                        )
                    design = selected
                else:
                    raise FileNotFoundError(
                        f"Design file {design_path} does not exist and no xedaproject was found"
                    )
            else:
                project_path = resolve_project_file(xedaproject)
                if project_path is not None:
                    project = XedaProject.from_file(project_path, skip_designs=True)
                    project_flow_settings = project.flows
        assert isinstance(design, Design)
        self.target = design.target
        # where a project's settings come from, named in messages even when there is none
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
            location="the command line",
            kind="cli",
        )
        binding_layers = [project_bindings, design_bindings, cli_bindings]
        cli_sections, _cli_only = split_bindings(cli_sections, location="the command line")
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
        )
        require_no_bindings(binding_layers, [node.node_key for node in plan.nodes])
        input_settings = plan.node(flow_name).settings
        sections = {
            **sections,
            **{node.name: as_recorded(node.settings) for node in plan.nodes},
        }
        flow_settings = as_recorded(input_settings)
        # Hashed exactly as a local run would be, from the validated settings.
        # Here, not on the remote: a path the flow writes that leads out of its run directory, a
        # missing setting, and a design the flow cannot run are known before anything is
        # shipped -- and a remote on an older release may not check them.
        problems = written_path_problems(input_settings)
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in problems],
                flow_class.Settings,
            )
        flow_class.check_required_settings(input_settings)
        flow_class.check_design_supported(design)
        located = deliverable_locations(input_settings)
        if located:
            key, path = located[0]
            raise DeliveryError(
                f"`{key}` names a location ({path}): with --remote, leave it a name in the run "
                "directory (or unset) and receive the outputs with --outputs-to DIR"
            )
        # the output names, checked as a local launch checks them: none xeda keeps (the remote's
        # own `results.json`), no two outputs under one name; no location is left to split
        split_deliveries(input_settings, design.name)
        # The mirror is named by the requested node's identity, as a local hashed run
        # directory is: the plan's, which counts its settings and where its inputs come from
        # (so two configurations of a producer are two mirrors). The remote resolves the same
        # request, and its `flow_hash` is compared with this one.
        flowrun_hash = plan.node(flow_name).flowrun_hash
        settings_hash = plan.node(flow_name).settings_hash
        # as a local run counts the design: by the parts the flow reads
        design_hash = design.parts_hash(flow_class.design_parts)
        outputs_to = self.settings.outputs_to
        # the local mirror: always `<design>[/<target>]/<flow>_<flowrun_hash>`
        # (`Settings.hashed_run_dirs`), so its delivery record is this settings variant's
        run_path = self.get_flow_run_path(
            design.name, flow_name, flowrun_hash, target=design.target
        )
        # every input this side can name, as a local launch has them: never a destination
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
        # the directory itself, not only a file predicted from the last run's artifacts: as on a
        # local launch, a location becomes a concrete `Delivery` only once its tool has run and
        # reported an artifact, so without this a run root or an input named by `--outputs-to` is
        # refused only after the remote run has already completed
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
            # however the run ends -- a refused delivery, a failed fetch -- the gateway, then the
            # connection, is closed
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
            # use a timestamped subdirectory to avoid any race conditions and also have the
            # chronology clear
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
            # `python` is resolved by the remote's *non-login* shell; `env:PATH` above is applied
            # only once that interpreter runs. So which xeda answers is not a given: ask.
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
                "Remote xeda: %s at %s (%s), protocol %s",
                remote_xeda,
                remote_xeda_at,
                remote_python,
                remote_protocol,
            )
            # The remotely executed worker is this installed xeda package, so its interpreter
            # must satisfy the same floor as pyproject.toml rather than a historical
            # transport-only floor.
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

            # Stream the remote flow's output (its own, and that of every tool it
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
                    # then which of a failed run's artifacts the remote vouches its run wrote
                    written_on_remote = json.loads(results_channel.receive())
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
                if (
                    results.get("success")
                    # the remote's `flow_hash` is its node's identity: another one means it
                    # resolved another configuration of this flow or of one of its producers
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
                    # Partial records from MissingOutput are not a successful hand-over.
                    # Failed-run diagnostics still travel through the vouched artifact path.
                    results.pop("outputs", None)
                declared = results.get("outputs") or {}
                output_paths = [str(path) for path in declared_output_files(results)]
                remote_deliverables = (
                    {"artifacts": artifacts or {}, "outputs": output_paths}
                    if output_paths
                    else artifacts
                )
                for recorded in declared.values():
                    for entry in recorded if isinstance(recorded, list) else [recorded]:
                        remote_output = Path(entry["path"])
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
                        for entry in recorded if isinstance(recorded, list) else [recorded]:
                            remote_output = Path(entry["path"])
                            local_output = Path(rewrites[entry["path"]])
                            if content_digest(local_output) != entry["sha"]:
                                raise DeliveryError(
                                    f"Declared remote output {remote_output} changed during transfer"
                                )
                            entry["path"] = str(local_output)

                # the remote run_path no longer exists locally; report the local copy's path instead
                if "run_path" in results:
                    results["run_path"] = str(run_path)

                dump_json(results, results_json_path, backup=True)
                log.info("Results written to %s", results_json_path)
                # after the mirror's records are whole: a refused delivery leaves them as they are
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
        """`--outputs-to` from the artifacts fetched into `local_artifacts_dir`, named by their
        paths in the remote run directory -- only for a run that succeeded: a failed run's files
        never replace xeda's earlier copies. What was delivered goes into `results`."""
        outputs_to = self.settings.outputs_to
        if outputs_to is None or not remote_run_path or not remote_artifacts:
            return
        if not results.get("success"):
            log.warning("The remote run failed: nothing is delivered to %s", outputs_to)
            return
        extra = outputs_to_deliveries(remote_artifacts, Path(remote_run_path), outputs_to)
        delivery.collect(local_artifacts_dir, extra)
        try:
            delivered = delivery.deliver()
        except Exception:
            # As `_finish_launch` does for a local run: `deliver()` records, and reports through
            # `delivery.delivered`, whatever it copied before raising (an `OSError` partway
            # through), so `results["deliveries"]` -- what a `--json` reader sees -- still lists
            # the copies that actually reached disk before the error propagates.
            results["deliveries"] = [d.as_json_value() for d in delivery.delivered]
            raise
        results["deliveries"] = [d.as_json_value() for d in delivered]
