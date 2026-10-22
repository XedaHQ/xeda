"""Choose the files to fetch from a remote run, publish them in the local mirror, and deliver them.

`select_fetch` runs on the remote. It names the files of the run directories that come back.
`publish_mirror` runs on the local machine. It checks each received file against its digest and
writes it into the mirror. `rewrite_records` writes the records of a mirror directory again with
the paths of this side. `deliver_fetched` delivers the outputs of the requested node.

A mirror directory holds the files of one run directory under the names they have there. The
record of a run (`results.json`) names its files relative to the run directory, so a fetched record
needs no rewriting. A file outside the run directory is in no record and does not come back.
"""

import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, BinaryIO, Dict, Optional

from ..deliver import (
    Delivered,
    Deliveries,
    DeliveryError,
    ReadInputs,
    artifacts_of,
    leads_out,
    outputs_to_deliveries,
)
from ..design import Design
from ..digest import content_digest
from ..errors import RecordError
from ..flow import Flow, FlowSettingsError
from ..flow_runner.default_runner import _get_flow_class_if_known
from ..flow_runner.trace_inputs import design_files, register_read_settings
from ..records import RunRecord, decode_record, stored_outputs
from ..run_dir import RunDirectory, RunDirectoryError, link_refusal
from ..utils import dump_json, replacing_file
from .errors import RemoteFetchError
from .manifest import Entry, copy_verified, iter_tar

log = logging.getLogger(__name__)

#: The files at the top of a run directory that always come back.
_RECORDS = ("results.json", "settings.json")
#: The record of freshness: it stays on the remote.
_TRACE = "trace.json"


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


def _is_regular(path: str) -> bool:
    """Whether `path` is itself a regular file. A link is not, whatever it leads to."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_regular_inside(run_dir: str, place: str) -> bool:
    """Whether `place` is a regular file of the run directory, reached through real directories
    only: no link on the way and none at the end. A tool may leave a link to anywhere, and
    Xeda follows none."""
    current = run_dir
    *parents, name = PurePosixPath(place).parts
    try:
        for part in parents:
            current = os.path.join(current, part)
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                return False
    except OSError:
        return False
    return _is_regular(os.path.join(current, name))


@dataclass(frozen=True)
class SkippedSource:
    """An artifact of node `node` that a symbolic link leads out of its run directory.

    `place` is its place in the run directory, with `/`, as the results name it. `source` is its
    path on the remote and `target` where it leads. Its content is not fetched, and a delivery of
    `--outputs-to` skips it, as for a local run (`deliver._outside`)."""

    node: str
    place: str
    source: str
    target: str

    def as_json_value(self) -> Dict[str, str]:
        return {
            "node": self.node,
            "place": self.place,
            "source": self.source,
            "target": self.target,
        }

    @classmethod
    def from_json_value(cls, value: Any) -> "SkippedSource":
        keys = ("node", "place", "source", "target")
        if (
            not isinstance(value, Mapping)
            or set(value) != set(keys)
            or not all(isinstance(value[key], str) and value[key] for key in keys)
        ):
            raise RemoteFetchError(f"a skipped source is not {{{', '.join(keys)}}}: {value!r}")
        place = PurePosixPath(value["place"])
        if place.is_absolute() or ".." in place.parts:
            raise RemoteFetchError(f"a skipped source has the place {value['place']!r}")
        return cls(*(value[key] for key in keys))


@dataclass(frozen=True)
class FetchSelection:
    """What `select_fetch` chose: the files that come back, and the artifacts that do not
    because a link leads them out of their run directory."""

    files: dict[str, Path]
    skipped: list[SkippedSource]


def _through_a_link(
    node: str, given: str, full: str, run_dir: str, declared: bool
) -> Optional[Path]:
    """Return the regular file of the run directory that the named file `full` leads to.

    The name is not a regular file reached through real directories only: a symbolic link lies
    at it or on the way to it. A link that leads to a regular file in the run directory brings
    that file back under the name. One that leads out of the run directory brings nothing back,
    and the function returns None for an artifact. A declared output that leads out, a link that
    leads nowhere, and a name that is no regular file raise `RemoteFetchError`."""
    if not os.path.exists(full):
        raise RemoteFetchError(
            f"{node} names {given}, but there is no file of that name in {run_dir}, or it is a "
            "symbolic link that leads nowhere"
        )
    target = leads_out(Path(full), Path(run_dir))
    if target is not None:
        if declared:
            raise RemoteFetchError(
                f"{node} names {given} as a declared output, but it leads to {target}, outside "
                f"its run directory {run_dir}"
            )
        return None
    resolved = Path(os.path.realpath(full))
    if not _is_regular(str(resolved)):
        raise RemoteFetchError(f"{node} names {given}, but it is no regular file in {run_dir}")
    return resolved


def _files_below(run_dir: str, subdirectory: str = "") -> list[str]:
    """The files to fetch below `run_dir`/`subdirectory`, as paths relative to `run_dir`, in
    order. The walk follows no link: a linked directory is not entered (the directory the walk
    starts in included) and a linked file is not listed."""
    found: list[str] = []

    def walk(directory: str, place: str) -> None:
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                return
            with os.scandir(directory) as listing:
                items = sorted(listing, key=lambda item: item.name)
        except OSError:
            return
        for item in items:
            name = f"{place}/{item.name}" if place else item.name
            if item.is_dir(follow_symlinks=False):
                walk(item.path, name)
            elif item.is_file(follow_symlinks=False):
                found.append(name)

    start = run_dir
    place = ""
    for part in PurePosixPath(subdirectory).parts:
        start = os.path.join(start, part)
        place = f"{place}/{part}" if place else part
    walk(start, place)
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


def _fetched_record(node: "NodeResult", run_root: Path) -> RunRecord | None:
    """Return the record that the results of `node` hold, or None if they hold none (a node that
    never ran, or a record that this Xeda cannot read)."""
    try:
        return decode_record(node.results, run_dir=Path(node.run_dir), run_root=run_root)
    except RecordError:
        return None


def select_fetch(run_root: Path, nodes: Sequence[NodeResult], *, fetch_all: bool) -> FetchSelection:
    """Return the files that come back to the local machine, on the remote.

    The keys of `files` are paths relative to `run_root`, with `/`: the run directory of the node
    and then the place in it. The values are the files on the remote.

    Of every node, these come back: `results.json`, `settings.json`, the `*.log` files at the top
    of the run directory, and everything below `reports/`. Of a node that the request names, the
    files that its record lists (its outputs and artifacts) come back too. With `fetch_all`, the
    whole run directory comes back. `trace.json` never does.

    A named file of a node that passed may be a symbolic link, or lie through one, that a tool
    left. If it leads to a regular file in the run directory, that file comes back under the
    name. If it leads out of the run directory, nothing of it comes back: an artifact is listed
    in `skipped`, and a declared output is an error. A link that leads nowhere is an error. The
    other files, whose discovery follows no link, come back only as regular files.

    A node that did not pass (its results hold no record, or a record of another outcome) returns
    only the files that its run wrote (`NodeResult.written`) and no declared outputs. If the
    remote gave no `written`, it returns none. A record names no file outside the run directory,
    so none comes back. A file that a passed node names and that is missing is an error."""
    selected: dict[str, Path] = {}
    skipped: list[SkippedSource] = []
    for node in nodes:
        run_dir = os.path.abspath(node.run_dir)
        prefix = _node_prefix(run_root, node.run_dir)
        record = _fetched_record(node, run_root)
        succeeded = record is not None and record.passed
        inside: dict[str, Path] = {}  # place in the run directory -> file
        if fetch_all:
            places = _files_below(run_dir)
        else:
            places = [name for name in _RECORDS if _is_regular_inside(run_dir, name)]
            try:
                with os.scandir(run_dir) as listing:
                    places += sorted(
                        item.name
                        for item in listing
                        if item.name.endswith(".log") and item.is_file(follow_symlinks=False)
                    )
            except OSError:
                pass
            places += _files_below(run_dir, "reports")
        for place in places:
            inside[place] = Path(run_dir, place)
        if node.named and record is not None:
            named = [(name, False) for name in record.artifacts.values()]
            if succeeded:
                named += [
                    (path.relative_to(record.run_dir).as_posix(), True)
                    for files in stored_outputs(record).values()
                    for path, _digest in files
                ]
            for given, declared in named:
                full = os.path.normpath(os.path.join(run_dir, given))
                if not _is_regular_inside(run_dir, given):
                    if not succeeded:
                        continue
                    resolved = _through_a_link(node.name, given, full, run_dir, declared)
                    if resolved is None:
                        skipped.append(
                            SkippedSource(node.name, given, full, os.path.realpath(full))
                        )
                    else:
                        inside[given] = resolved
                    continue
                inside[given] = Path(full)
        inside.pop(_TRACE, None)
        if not succeeded:
            written = node.written or frozenset()
            inside = {place: path for place, path in inside.items() if place in written}
        for place, path in inside.items():
            selected[(prefix / place).as_posix() if prefix.parts else place] = path
    return FetchSelection(selected, list(dict.fromkeys(skipped)))


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
    if mirror_root.is_symlink():
        raise RemoteFetchError(f"cannot publish into the mirror: {link_refusal(mirror_root)}")
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


def check_fetched_outputs(record: RunRecord) -> None:
    """Raise `DeliveryError` if a declared output of `record`, the record of a run that passed and
    that now lies in the mirror, is not a regular file there or does not have the digest that the
    record holds. A run that did not pass has no declared output to check."""
    if not record.passed:
        return
    for files in stored_outputs(record).values():
        for path, digest in files:
            if not _is_regular(str(path)):
                raise DeliveryError(f"Declared remote output {path.name} is not in the mirror")
            if content_digest(path) != digest:
                raise DeliveryError(f"Declared remote output {path.name} changed during transfer")


def translate_paths(document: Any, translate: Callable[[str], Optional[str]]) -> Any:
    """Return a copy of the JSON `document` in which every text that `translate` maps is replaced.

    `translate` returns the new text, or `None` to keep the text. The keys stay as they are."""
    if isinstance(document, str):
        mapped = translate(document)
        return document if mapped is None else mapped
    if isinstance(document, Mapping):
        return {key: translate_paths(value, translate) for key, value in document.items()}
    if isinstance(document, list):
        return [translate_paths(value, translate) for value in document]
    return document


def only_fetched_artifacts(document: Any, mirror_dir: Path) -> Any:
    """Return the record `document` of a run that did not pass, with the artifacts that came back
    and its declared outputs empty.

    The record of a run lists the files it wrote. A remote that lists a file it never wrote
    (`select_fetch` takes only the files that the remote vouches for) must not make the mirror
    record name a file that the mirror lacks. A run that did not pass hands over no output, and
    `select_fetch` brings back none of its outputs: each keeps its entry, with no names. A record
    of a run that passed is returned as it is: every file it names came back, or the fetch
    failed."""
    if not isinstance(document, Mapping) or document.get("outcome") == "passed":
        return document
    kept = dict(document)
    artifacts = document.get("artifacts")
    if isinstance(artifacts, Mapping):
        kept["artifacts"] = {
            key: name
            for key, name in artifacts.items()
            if isinstance(name, str) and _is_regular_inside(str(mirror_dir), name)
        }
    outputs = document.get("outputs")
    if isinstance(outputs, Mapping):
        kept["outputs"] = {
            name: (
                {**output, "names": [], "digests": [], "design": None}
                if isinstance(output, Mapping)
                else output
            )
            for name, output in outputs.items()
        }
    return kept


def rewrite_records(
    mirror_dir: Path,
    run_root: Path,
    translate: Callable[[str], Optional[str]],
) -> None:
    """Write `results.json` and `settings.json` of the mirror directory again, with the paths of
    this side: a user who opens the mirror finds no path of the remote host in them.

    `translate` maps a path on the remote to the path of the same file here (`translate_paths`).
    The caller holds the lock of the mirror directory. A record that was not fetched is left out.
    A record that is not JSON raises `RemoteFetchError`."""
    run_directory = RunDirectory.claimed(mirror_dir, run_root)
    for name in _RECORDS:
        record = mirror_dir / name
        if not _is_regular(str(record)):
            continue
        try:
            document = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RemoteFetchError(f"cannot read the fetched {record}: {error}") from None
        if name == "results.json":
            document = only_fetched_artifacts(document, mirror_dir)
        try:
            target = run_directory.writable(name)
        except RunDirectoryError as error:
            raise RemoteFetchError(f"cannot rewrite {record}: {error}") from None
        dump_json(translate_paths(document, translate), target, backup=False)


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
    record: RunRecord,
    *,
    outputs_to: Optional[Path],
    skipped: Sequence[SkippedSource] = (),
) -> list[Delivered]:
    """Deliver `--outputs-to` (`outputs_to`) from the files of the mirror directory of `record`
    (`record.run_dir`) and return what was delivered.

    Only a run that passed delivers: the files of a failed run never replace earlier copies made
    by Xeda. If the delivery raises, `delivery.delivered` holds what it copied before. An
    artifact in `skipped` leads out of the run directory on the remote, and the mirror holds no
    file of it: its delivery is skipped, as a local one is."""
    if outputs_to is None:
        return []
    if not record.passed:
        log.warning("The remote run did not pass: nothing is delivered to %s", outputs_to)
        return []
    named = artifacts_of(record)
    if not named:
        return []
    outside = {PurePath(source.place): (source.source, source.target) for source in skipped}
    extra = outputs_to_deliveries(named, record.run_dir, outputs_to)
    delivery.collect(record.run_dir, extra, outside)
    return list(delivery.deliver())
