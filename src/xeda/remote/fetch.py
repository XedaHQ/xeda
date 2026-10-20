"""Choose the files to fetch from a remote run, publish them in the local mirror, and deliver them.

`select_fetch` runs on the remote. It names the files of the run directories that come back.
`publish_mirror` runs on the local machine. It checks each received file against its digest and
writes it into the mirror. `rewrite_results` points the paths in a node's results at the mirror.

A mirror directory holds the files of one run directory under the names they have there. A file
that is not in the run directory (an artifact outside it) lies in the mirror directory under
`_external/<digest>/<name>`, where the digest names its remote path.
"""

import copy
import hashlib
import logging
import os
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Optional

from ..artifacts import (
    drop_unwritten_artifacts,
    iter_artifact_paths,
    map_artifact_paths,
)
from ..deliver import Deliveries, DeliveryError, ReadInputs, outputs_to_deliveries
from ..design import Design
from ..digest import content_digest
from ..flow import Flow, FlowSettingsError
from ..flow.derived import recorded_files
from ..flow_runner.default_runner import _get_flow_class_if_known
from ..flow_runner.outputs import declared_output_files
from ..flow_runner.trace_inputs import design_files, register_read_settings
from ..run_dir import RunDirectory, RunDirectoryError
from ..utils import replacing_file
from .errors import RemoteFetchError
from .manifest import Entry, copy_verified, iter_tar

log = logging.getLogger(__name__)

#: The files at the top of a run directory that always come back.
_RECORDS = ("results.json", "settings.json")
#: The record of freshness: it stays on the remote.
_TRACE = "trace.json"
_EXTERNAL = "_external"


def external_place(remote_path: str) -> PurePosixPath:
    """Return where a file outside the run directory lies in the mirror directory. The digest
    keeps same-named files of different places apart."""
    digest = hashlib.sha256(remote_path.encode()).hexdigest()[:16]
    return PurePosixPath(_EXTERNAL) / digest / PurePosixPath(remote_path).name


def mirror_relative(remote_path: str, remote_run_dir: str) -> PurePosixPath:
    """Return the place in the mirror directory of the file `remote_path`, which a result names
    as an absolute path or relative to the run directory `remote_run_dir`.

    A file below the run directory keeps its place. Any other file (an absolute path elsewhere,
    or a relative `../` path) has an external place, so that nothing leaves the mirror."""
    remote_root = os.path.normpath(remote_run_dir)
    full = os.path.normpath(
        remote_path if os.path.isabs(remote_path) else os.path.join(remote_root, remote_path)
    )
    relative = PurePosixPath(os.path.relpath(full, remote_root))
    if ".." in relative.parts:
        return external_place(full)
    return relative


def keep_vouched(
    artifacts: Any, vouched: Collection[str] | None, flow_name: str = "the remote flow"
) -> Any:
    """Return the artifacts of a failed run without the paths that the remote did not vouch for.

    `vouched` are the names, as each is named in the results, of the artifacts that the remote
    says its run wrote. A remote can tell, from its own file system, but this side cannot: a
    file that merely exists on the remote proves nothing, and with no word from the remote none
    is kept."""
    kept = frozenset(vouched or ())
    return drop_unwritten_artifacts(
        artifacts, lambda artifact: os.fspath(artifact) in kept, flow_name
    )


@dataclass(frozen=True)
class NodeResult:
    """One node of the plan, as the remote reports it.

    `run_dir` is its run directory on the remote and `results` its results. `named` tells that
    the request names the node: its declared outputs and artifacts come back. `written` is the
    set of the paths, relative to the run directory, that this run wrote. It is `None` when the
    remote gave none."""

    name: str
    run_dir: Path
    results: dict[str, Any]
    named: bool
    written: Optional[frozenset[str]] = None


def _declared_outputs_problem(results: Mapping[str, Any], run_dir: str) -> None:
    """Raise `DeliveryError` if a declared output is not an absolute path inside `run_dir`."""
    root = PurePosixPath(run_dir)
    for path in declared_output_files(dict(results)):
        output = PurePosixPath(path.as_posix())
        if not output.is_absolute() or ".." in output.parts or not output.is_relative_to(root):
            raise DeliveryError("A declared remote output is outside its run directory")


def _is_fetchable(path: str, real_run_dir: str) -> bool:
    """Whether `path` in the run directory is a regular file to fetch. A link counts only if it
    leads to a regular file that lies in the run directory too: a tool may leave a link to
    anywhere."""
    try:
        status = os.lstat(path)
        if stat.S_ISLNK(status.st_mode):
            target = os.path.realpath(path)
            return (
                os.path.isfile(target)
                and os.path.commonpath([target, real_run_dir]) == real_run_dir
            )
        return stat.S_ISREG(status.st_mode)
    except (OSError, ValueError):
        return False


def _files_below(run_dir: str, subdirectory: str = "") -> list[str]:
    """The files to fetch below `run_dir`/`subdirectory`, as paths relative to `run_dir`. The
    walk enters no linked directory."""
    real_run_dir = os.path.realpath(run_dir)
    found: list[str] = []
    for top, _dirs, names in os.walk(os.path.join(run_dir, subdirectory), followlinks=False):
        for name in sorted(names):
            path = os.path.join(top, name)
            if _is_fetchable(path, real_run_dir):
                found.append(PurePosixPath(os.path.relpath(path, run_dir)).as_posix())
    return found


def _node_prefix(run_root: Path, run_dir: Path) -> PurePosixPath:
    """The place of a run directory below the run root, with `/`."""
    for root, directory in (
        (os.path.abspath(run_root), os.path.abspath(run_dir)),
        (os.path.realpath(run_root), os.path.realpath(run_dir)),
    ):
        if directory == root or directory.startswith(root + os.sep):
            return PurePosixPath(os.path.relpath(directory, root).replace(os.sep, "/"))
    raise RemoteFetchError(f"the run directory {run_dir} is not below the run root {run_root}")


def select_fetch(
    run_root: Path, nodes: Sequence[NodeResult], *, fetch_all: bool
) -> dict[str, Path]:
    """Return the files that come back to the local machine, on the remote.

    The keys are paths relative to `run_root`, with `/`: the run directory of the node and then
    the place in it. The values are the files on the remote.

    Of every node, these come back: `results.json`, `settings.json`, the `*.log` files at the top
    of the run directory, and everything below `reports/`. Of a node that the request names, the
    paths in `results.outputs` and `results.artifacts` come back too. With `fetch_all`, the whole
    run directory comes back. `trace.json` never does.

    A node that failed (`results.success` is not true) returns only the files that its run wrote
    (`NodeResult.written`) and no declared outputs. If the remote gave no `written`, it returns
    none. A file outside the run directory is returned only for a node that succeeded, under
    `_external/<digest>/<name>` (`external_place`). A file that a succeeded node names and that is
    missing is an error."""
    selected: dict[str, Path] = {}
    for node in nodes:
        run_dir = os.path.abspath(node.run_dir)
        prefix = _node_prefix(run_root, node.run_dir)
        succeeded = bool(node.results.get("success"))
        inside: dict[str, Path] = {}  # place in the run directory -> file
        outside: dict[str, Path] = {}  # place in the mirror directory -> file
        if fetch_all:
            places = _files_below(run_dir)
        else:
            real_run_dir = os.path.realpath(run_dir)
            places = [
                name
                for name in _RECORDS
                if _is_fetchable(os.path.join(run_dir, name), real_run_dir)
            ]
            try:
                top = sorted(os.listdir(run_dir))
            except OSError:
                top = []
            places += [
                name
                for name in top
                if name.endswith(".log")
                and _is_fetchable(os.path.join(run_dir, name), real_run_dir)
            ]
            places += _files_below(run_dir, "reports")
        for place in places:
            inside[place] = Path(run_dir, place)
        if node.named:
            if succeeded:
                _declared_outputs_problem(node.results, run_dir)
            named = [os.fspath(path) for path in iter_artifact_paths(node.results.get("artifacts"))]
            if succeeded:
                named += [os.fspath(path) for path in declared_output_files(dict(node.results))]
            for given in named:
                mirrored = mirror_relative(given, run_dir)
                full = given if os.path.isabs(given) else os.path.join(run_dir, given)
                full = os.path.normpath(full)
                if not os.path.isfile(full):
                    if succeeded:
                        raise RemoteFetchError(
                            f"{node.name} names {given}, but the file is not in {run_dir}"
                        )
                    continue
                if mirrored.parts[0] == _EXTERNAL:
                    outside[mirrored.as_posix()] = Path(full)
                else:
                    inside[mirrored.as_posix()] = Path(full)
        inside.pop(_TRACE, None)
        if not succeeded:
            written = node.written or frozenset()
            inside = {place: path for place, path in inside.items() if place in written}
            outside = {}
        for place, path in {**inside, **outside}.items():
            selected[(prefix / place).as_posix() if prefix.parts else place] = path
    return selected


def publish_mirror(
    mirror_root: Path,
    entries: Sequence[Entry],
    tar_stream: BinaryIO,
    *,
    remote_run_root: PurePosixPath,
) -> dict[str, Path]:
    """Write the fetched files into the mirror, on the local machine.

    `mirror_root` is `<run root>/@<label>`, the mirror of one host. Each entry is a path relative
    to the run root of the remote (`remote_run_root`), and its file lies at that path below
    `mirror_root`. `tar_stream` holds the entries. A file is written to a temporary file, checked
    against the digest of its entry, and only then renamed to its name (`replacing_file`), so a
    file is complete or absent. Nothing is written through a link or outside `mirror_root`. A
    file that fails its digest raises `RemoteFetchError`. Files published before it stay. The
    caller holds the lock of the mirror and removes what an earlier fetch left.

    Return the local path of every published file, by the path it has below `remote_run_root`."""
    mirror_root.mkdir(parents=True, exist_ok=True)
    try:
        run_directory = RunDirectory.claimed(mirror_root, mirror_root.parent)
    except ValueError as error:
        raise RemoteFetchError(
            f"the mirror {mirror_root} is not below its run root: {error}"
        ) from None
    published: dict[str, Path] = {}
    for entry, source in iter_tar(entries, tar_stream):
        try:
            target = run_directory.writable(entry.path)
            if target.is_dir() and not target.is_symlink():
                run_directory.remove(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            run_directory.inside(target)
            with replacing_file(target, "wb") as out:
                copy_verified(entry, source, out)
                if entry.executable:
                    mode = os.fstat(out.fileno()).st_mode
                    os.fchmod(out.fileno(), mode | ((mode & 0o444) >> 2))
        except RunDirectoryError as error:
            raise RemoteFetchError(f"cannot publish {entry.path}: {error}") from None
        published[(remote_run_root / entry.path).as_posix()] = target
    return published


def rewrite_results(
    results: dict[str, Any], remote_run_dir: PurePosixPath, mirror_dir: Path
) -> dict[str, Any]:
    """Return the results of a node with the paths of the remote run directory `remote_run_dir`
    pointed at the mirror directory `mirror_dir`: `run_path` and every artifact and declared
    output.

    A failed run has no declared outputs. A declared output must lie in the run directory, and
    its file in the mirror must have the digest in its record, or `DeliveryError`."""
    rewritten = copy.deepcopy(results)
    remote_dir = str(remote_run_dir)
    if not rewritten.get("success"):
        rewritten.pop("outputs", None)
    _declared_outputs_problem(rewritten, remote_dir)

    def local_path(path: str) -> Path:
        return mirror_dir / mirror_relative(path, remote_dir)

    for recorded in (rewritten.get("outputs") or {}).values():
        for record, key in recorded_files(recorded):
            local = local_path(record[key])
            try:
                digest = content_digest(local)
            except OSError:
                raise DeliveryError(
                    f"Declared remote output {record[key]} is not in the mirror"
                ) from None
            if digest != record["sha"]:
                raise DeliveryError(f"Declared remote output {record[key]} changed during transfer")
            record[key] = str(local)
    if "artifacts" in rewritten:
        rewritten["artifacts"] = map_artifact_paths(
            rewritten["artifacts"], lambda artifact: str(local_path(os.fspath(artifact)))
        )
    if "run_path" in rewritten:
        rewritten["run_path"] = str(mirror_dir)
    return rewritten


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
    """Return the inputs that the deliveries of a remote run protect.

    They cover every file of the graph that this side can name: the files of the design, the
    design file and project file given, and what the read settings of the requested flow
    `flow_class` name, as the launch uses them. For the requested flow, `input_settings` are
    its section with the settings of the command line over it, and never its section as
    written, since no flow of the run reads what the command line replaced there. For every
    other flow, the section sent with the design counts as written (`flows.<name>`, with the
    project's merged in), since this side does not know which producers the remote launches, or
    with what. A file, or a directory with every entry under it, is registered as a local
    launch registers them (`trace_inputs.register_read_settings`, with `run_path`, the local
    mirror, and `run_root` for where the listings stop). A section that does not validate names
    nothing: the remote run of that flow fails before anything is delivered."""
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


def deliver_fetched(
    delivery: Deliveries,
    results: Dict[str, Any],
    remote_artifacts: Any,
    remote_run_path: Optional[str],
    local_artifacts_dir: Path,
    *,
    outputs_to: Optional[Path],
) -> None:
    """Deliver `--outputs-to` (`outputs_to`) from the artifacts fetched into
    `local_artifacts_dir`.

    The artifacts are named by their paths in the remote run directory. Only a run that
    succeeded delivers: the files of a failed run never replace earlier copies made by
    Xeda. What was delivered goes into `results`."""
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
        # through). So `results["deliveries"]`, which a `--json` reader sees, still lists
        # the copies that actually reached disk before the error propagates.
        results["deliveries"] = [d.as_json_value() for d in delivery.delivered]
        raise
    results["deliveries"] = [d.as_json_value() for d in delivered]
