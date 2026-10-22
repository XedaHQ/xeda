"""Delivery of outputs to the locations that the user named.

The tools of a flow write only in its run directory. The user can name a location for an output
in two ways: a deliverable setting (`xeda.dataclass.deliverable`) whose value is absolute once
its variables are expanded (`utils.LOCATION_FORMS`), or `--outputs-to DIR` for the artifacts of
the requested flow. Xeda delivers such an output. After the launch has finished, it copies the
file to the location under the name that the user chose, for a flow that succeeded or was found
up to date. The setting itself then names the file in the run directory by its fixed
conventional name (`split_deliveries`: `outputs/<design>.<ext>`, or the default name of the
setting), and the identity of the run sees only that name (`flow.identity_settings`). Where an
output goes, and what it is called there, never changes what the tools do or what the run is.

A delivery never goes onto an input, which is any file that a flow of the launch reads
(`ReadInputs`, by file identity and resolved path). It never goes into a directory that a read
setting names (every file there is an input of the run, and so is a file delivered there), into a
run root, or over a directory. It never deletes anything and never writes through a symbolic
link: it writes a temporary file in the directory of the destination and renames it into place.

Two deliveries of one launch never go to one destination, and none goes inside another
(`refuse_shared_destinations`). The comparison uses names as the file system does, letter case
included where a directory ignores it. The launch refuses such deliveries and names both. It
refuses before any tool runs for every destination known then: the destinations of the settings,
and the files that `--outputs-to` is expected to deliver, from the last run of the requested
flow. It refuses before the first copy for the destinations that only a run reveals. Two names
that still reach one file are found by the file itself, when the second delivery meets the first
(`DeliveredFiles`).

An existing file is replaced only when it is an earlier delivery of Xeda, unchanged, as the
record says (`delivery_record`: beside the run directory, in the run root). Anything else needs
the confirmation of the user: `overwrite_outputs`, or a yes from `confirm_overwrite` of the
launcher (the prompt of the command line). The question comes before any tool of the launch runs.
The requested flow checks every flow of the plan when the launch starts (`Deliveries.check`),
after every refusal that no answer could change (`Deliveries.refuse`). A yes holds for the file
that the user was asked about, as it was then (`ConfirmedReplacements`). A file that changed since
is asked about again.

The trust rule of `digest.FileRecord` applies to the record. Xeda reads the content of a
destination only when its metadata cannot vouch for it. The check that reads it anchors the record
to the clock of the file system of the destination, read just before (`_destination_clock`), so
the next check of an unchanged delivery reads nothing (`_destination_record`).

A flow notes what it delivers, with the digest of each file, when it completes
(`Deliveries.collect`). Xeda copies the files when the launch has finished (`Deliveries.deliver`),
after every flow of the launch has read its inputs. A destination that changed after the check,
or an output that changed after its run, is never delivered. When the launch deletes the run
directory of a flow right after (`--post-cleanup-purge`), Xeda moves the file out of that
directory and does not copy it, so a large output costs no second copy on the disk.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import stat
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, Optional

from .artifacts import iter_artifact_paths
from .dataclass import DELIVERABLE_ROLE, written_role
from .digest import FileRecord, content_digest, filesystem_time_ns, record_file
from .listing import directory_files
from .flow import Flow, FlowSettingsError
from .flow.derived import recorded_files
from .flow.flow import WrittenLeaf, map_written_leaves, output_name, resolved_template
from .run_root import is_run_root
from .utils import XedaException, copy_fd, json_encodable, with_json_keys

log = logging.getLogger(__name__)

__all__ = [
    "DELIVERY_FORMAT",
    "DELIVERY_RECORD_SUFFIX",
    "OUTPUTS_TO",
    "RESERVED_NAMES",
    "Conflict",
    "ConfirmedReplacements",
    "Deliveries",
    "Delivered",
    "DeliveredFiles",
    "Delivery",
    "DeliveryError",
    "OutputExistsError",
    "ReadInputs",
    "deliverable_locations",
    "deliverable_setting_names",
    "delivery_record",
    "outputs_to_deliveries",
    "recorded_artifacts",
    "refuse_shared_destinations",
    "split_deliveries",
]

DELIVERY_RECORD_SUFFIX = ".delivered.json"
DELIVERY_FORMAT = 1
#: The key under which a delivery that `--outputs-to` asked for is reported.
OUTPUTS_TO = "--outputs-to"
#: The names that Xeda keeps for itself in a run directory. An output never takes one.
RESERVED_NAMES = frozenset({"settings.json", "results.json", "trace.json", "trace.json.tmp"})

_State = Optional[tuple[int, int, int, int]]


class DeliveryError(XedaException):
    """An output cannot be delivered where it was named.

    `before_run` is true for a refusal made before any tool of the flow of the output ran
    (`Deliveries.check_outputs_to` and `check`). Nothing of that flow has changed, so the launch
    reports the refusal as its own error and not as the failure of a flow.

    `output_problem` is true when the cause is an output of the run that is missing, leads
    nowhere or changed after the run: a problem with the declared outputs of a flow.
    """

    def __init__(
        self, *args: object, before_run: bool = False, output_problem: bool = False
    ) -> None:
        super().__init__(*args)
        self.before_run = before_run
        self.output_problem = output_problem


class OutputExistsError(DeliveryError):
    """A named output path holds a file that is not an unchanged earlier copy of Xeda.

    The user did not confirm the replacement.
    """


@dataclass(frozen=True)
class Delivery:
    """An output of a run, `name` in its run directory, to copy to `destination`.

    The setting or option `key` asked for the copy.
    """

    key: str
    name: PurePath
    destination: Path


@dataclass(frozen=True)
class Conflict:
    """A file in the way of a delivery: `destination`, located, and why replacing it needs a yes."""

    delivery: Delivery
    destination: Path
    why: str


@dataclass(frozen=True)
class Delivered:
    """A delivery that was made.

    `state` is "delivered" (written) or "unchanged" (the destination held the output already).
    """

    key: str
    source: Path
    destination: Path
    state: str

    def as_json_value(self) -> Dict[str, str]:
        return {
            "setting": self.key,
            "from": str(self.source),
            "to": str(self.destination),
            "state": self.state,
        }


def delivery_record(run_path: Path) -> Path:
    """Return `<run directory>.delivered.json`, beside the run directory, in the run root.

    The file records what Xeda delivered from the run directory and where. `--clean` and
    scrubbing leave it.
    """
    return run_path.parent / f"{run_path.name}{DELIVERY_RECORD_SUFFIX}"


def split_deliveries(settings: Flow.Settings, design: str) -> list[Delivery]:
    """Replace, in place, each deliverable setting of the flow that is given as a location.

    The setting becomes the conventional name for the design `design` (`flow.output_name`). The
    run writes that name in its run directory, whatever the location says. The function returns
    a `Delivery` of that file to the location for each such setting. The settings of a producer
    belong to its own launch. Raise a `FlowSettingsError` when two outputs would be written under
    one name, or one under a name that Xeda keeps for itself.
    """
    deliveries: list[Delivery] = []
    names: dict[PurePath, str] = {}
    problems: list[tuple[str, str]] = []

    def split(written: WrittenLeaf) -> Any:
        key, leaf = written.key, written.value
        if written.role != DELIVERABLE_ROLE or not isinstance(leaf, (str, os.PathLike)):
            return leaf
        if not os.fspath(leaf):
            return leaf
        path = Path(leaf)
        if path.is_absolute():
            conventional = output_name(written, design)
            assert conventional is not None, key  # every deliverable has a conventional name
            name = conventional
        elif (resolved := resolved_template(written, design)) is not None:
            name = resolved  # written under its own name, as for a location
        else:
            name = PurePath(path)
        if str(name) in RESERVED_NAMES:
            problems.append(
                (
                    key,
                    f"`{key}` would write {name}, a name Xeda keeps for itself in the run "
                    "directory: name the output otherwise",
                )
            )
        first = names.setdefault(name, key)
        if first != key:
            problems.append(
                (
                    key,
                    f"`{key}` and `{first}` would both be written as {name} in the run "
                    "directory: name them apart",
                )
            )
        if not path.is_absolute():
            return Path(name) if resolved_template(written, design) is not None else leaf
        if path.suffix != name.suffix:
            log.warning(
                "`%s`: the run writes %s, which is copied to %s as it is: a location chooses "
                "where the output goes, not what it is",
                key,
                name,
                path,
            )
        deliveries.append(Delivery(key, name, path))
        return Path(name)

    map_written_leaves(settings, split)
    if problems:
        raise FlowSettingsError(
            [(key, message, None, "value_error") for key, message in problems], type(settings)
        )
    return deliveries


def deliverable_locations(settings: Flow.Settings) -> list[tuple[str, Path]]:
    """Return every deliverable of `settings` given as a location, by key path.

    The function changes nothing.
    """
    found: list[tuple[str, Path]] = []

    def note(written: WrittenLeaf) -> Any:
        leaf = written.value
        if written.role == DELIVERABLE_ROLE and isinstance(leaf, (str, os.PathLike)):
            if os.fspath(leaf) and Path(leaf).is_absolute():
                found.append((written.key, Path(leaf)))
        return leaf

    map_written_leaves(settings, note)
    return found


def deliverable_setting_names(settings_cls: "type[Flow.Settings]") -> list[str]:
    """Return the name of every field that `settings_cls` itself declares deliverable.

    A field counts whatever its current value (`DELIVERABLE_ROLE`). The fields of a producer
    belong to its own launch and are not included. The warning that `--outputs-to` gives when a
    flow delivered nothing names these fields, so an unset `vcd` is not mistaken for a file that
    Xeda dropped.
    """
    return [
        name
        for name in settings_cls.model_fields
        if written_role(settings_cls, name) == DELIVERABLE_ROLE
    ]


def outputs_to_deliveries(
    artifacts: Any, run_path: Path, directory: Optional[Path]
) -> list[Delivery]:
    """Return the deliveries of `--outputs-to directory`.

    Each artifact inside the run directory `run_path` (named, or through its resolved path) goes
    to its path relative to the run directory. An artifact elsewhere is not copied, and the log
    says so.
    """
    if directory is None or not artifacts:
        return []
    bases = [Path(os.path.abspath(run_path)), Path(os.path.realpath(run_path))]
    found: dict[PurePath, Delivery] = {}
    for leaf in iter_artifact_paths(artifacts):
        path = Path(os.path.abspath(Path(leaf) if Path(leaf).is_absolute() else bases[0] / leaf))
        base = next((b for b in bases if path != b and path.is_relative_to(b)), None)
        if base is None:
            log.info(
                "%s lies outside the run directory %s: not copied to %s", path, run_path, directory
            )
            continue
        relative = PurePath(path.relative_to(base))
        found.setdefault(relative, Delivery(OUTPUTS_TO, relative, Path(directory) / relative))
    return list(found.values())


def _written_as(owner: str, delivery: Delivery) -> str:
    """Return how the user asked for `delivery`.

    That is `--outputs-to`, or the setting as written under `flows.<flow>` (by its key alone when
    the flow is not known).
    """
    if delivery.key == OUTPUTS_TO:
        return f"`{OUTPUTS_TO}` ({delivery.name})"
    return f"`flows.{owner}.{delivery.key}`" if owner else f"`{delivery.key}`"


#: The number of entries of a directory to examine for a name with letters in it, when the
#: directory is asked whether it ignores letter case.
_PROBE_ENTRIES = 64


def _one_entry(first: Path, second: Path) -> bool:
    """Return whether two names are one directory entry: the same file, with no link followed."""
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False


def _ignores_case(directory: Path) -> bool:
    """Return whether the names in `directory` are compared without regard to letter case.

    The default file systems of macOS and Windows compare names so. The function finds out by
    looking and never by writing. It takes an entry of the directory whose name has ASCII
    letters and asks for the name in the other case. The answer is that very entry, or it is not.

    A directory with no such entry is asked by its own name in its parent, when the two are on
    one file system (`st_dev`). If its own name has no letters either, the answer of its parent
    counts. The top of a file system, and a mount point with nothing to examine, are taken to
    keep case. A wrong "no" is caught when the second delivery finds the file that the first
    made (`DeliveredFiles`). A wrong "yes" would refuse two files that are two.
    """
    try:
        with os.scandir(directory) as entries:
            for entry in itertools.islice(entries, _PROBE_ENTRIES):
                swapped = entry.name.swapcase()
                if entry.name.isascii() and swapped != entry.name:
                    return _one_entry(directory / entry.name, directory / swapped)
    except OSError:
        return False
    parent = directory.parent
    if parent == directory:
        return False
    try:
        if os.stat(parent).st_dev != os.stat(directory).st_dev:
            return False  # a mount point: its parent says nothing about it
    except OSError:
        return False
    name = directory.name
    if name.isascii() and name.swapcase() != name:
        return _one_entry(directory, parent / name.swapcase())
    return _ignores_case(parent)


def _compared(path: Path, ignoring: dict[Path, Optional[bool]]) -> tuple[str, ...]:
    """Return `path`, located, as its file system compares it.

    The result holds the names, each folded to one case when the directory it lies in ignores
    letter case (`_ignores_case`). A directory that does not exist yet compares as the nearest
    existing directory, which it will be made in. `ignoring` keeps the answer for each directory
    that was examined, for the calls that follow.
    """
    parts = path.parts
    folded = [parts[0].casefold() if os.name == "nt" else parts[0]]
    ignore, here = False, Path(parts[0])
    for name in parts[1:]:
        if here not in ignoring:
            ignoring[here] = _ignores_case(here) if here.is_dir() else None
        known = ignoring[here]
        if known is not None:
            ignore = known
        folded.append(name.casefold() if ignore else name)
        here = here / name
    return tuple(folded)


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def refuse_shared_destinations(
    copies: Iterable[tuple[str, Delivery, Path]], *, before_run: bool
) -> None:
    """Raise a `DeliveryError` when two copies of one launch go to one destination.

    The error also covers a destination that lies inside another (its parent would have to be a
    file and a directory). It names everything that asked for them. A destination takes one
    output. Delivering the first output and then refusing the second as a change that nobody made
    would happen after the tools ran, and it would give a false reason and a partial delivery.

    The function compares destinations as their file system compares them (`_compared`): located,
    as `Deliveries` locates every destination, and in letter case only where the directory they
    lie in keeps it. The comparison cannot tell Unicode forms that a file system takes for one
    name, or a link made during the run. Delivery finds those in the file itself
    (`DeliveredFiles`).

    `copies` holds, in the order that the flows run, the name of a flow, one of its deliveries,
    and where that copy goes. A named delivery goes to the destination it names and is known from
    the plan, before any tool runs (`before_run`). A copy of `--outputs-to`, or of a file of a
    directory output, is known only when its flow has run. The launch then asks again, before it
    makes the first copy.
    """
    ignoring: dict[Path, Optional[bool]] = {}
    asked: dict[tuple[str, ...], list[str]] = {}
    at: dict[tuple[str, ...], Path] = {}
    for owner, delivery, destination in copies:
        located = _located(destination)
        key = _compared(located, ignoring)
        asked.setdefault(key, []).append(_written_as(owner, delivery))
        at.setdefault(key, located)
    problems = [
        f"{_listed(names)} {'both' if len(names) == 2 else 'all'} name {at[key]}"
        for key, names in asked.items()
        if len(names) > 1
    ]
    for key, names in asked.items():
        outer = next((key[:n] for n in range(len(key) - 1, 0, -1) if key[:n] in asked), None)
        if outer is not None:
            problems.append(
                f"{_listed(names)} {'name' if len(names) > 1 else 'names'} {at[key]}, which lies "
                f"inside {at[outer]}, named by {_listed(asked[outer])}"
            )
    if problems:
        raise DeliveryError(
            f"{'; '.join(problems)}: a destination takes one output; name them apart",
            before_run=before_run,
        )


def recorded_artifacts(results_json: Path) -> Any:
    """Return the artifact and declared-output paths of the last run, for checks before a run."""
    try:
        results = json.loads(results_json.read_text())
        artifacts = results.get("artifacts") or {}
        outputs = [
            entry[key]
            for recorded in (results.get("outputs") or {}).values()
            for entry, key in recorded_files(recorded)
        ]
        return {"artifacts": artifacts, "outputs": outputs} if outputs else artifacts
    except (OSError, ValueError, AttributeError):
        return {}


def _identity(path: Path, follow: bool = True) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path, follow_symlinks=follow)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _reached_files(source: Path) -> set[tuple[int, int]]:
    """Return the files that a delivery of `source` reaches: the file itself and its target."""
    return {i for i in (_identity(source, follow=False), _identity(source)) if i is not None}


class ReadInputs:
    """Every file that the flows of a launch read.

    These are the files of the design, the design file and project file that the launch was
    given, the file that each read setting of any of its flows names, and every entry under a
    directory that a read setting names, as the trace lists it
    (`trace_inputs.register_read_settings`). The class holds them by file identity (`st_dev`,
    `st_ino`) and by resolved path. It also holds those directories themselves, each with the
    setting that names it (`directory_of`).

    No delivery ever replaces one of those files or lands anywhere in one of those directories,
    with `--overwrite-outputs` or without. The `Deliveries` of a launch share one instance. The
    requested flow registers the settings of every flow of the plan when the launch starts, and
    each flow adds the files it prepares as it is launched. A delivery, made when the launch has
    finished, is therefore checked against all of them.
    """

    def __init__(self, paths: Iterable[Path] = ()) -> None:
        self._by_identity: dict[tuple[int, int], Path] = {}
        self._by_path: dict[Path, Path] = {}
        #: Each directory read (resolved), with the key of the setting that names the directory
        #: it was listed under, and that directory.
        self._directories: dict[Path, tuple[str, Path]] = {}
        self.add(paths)

    def add(self, paths: Iterable[Path]) -> None:
        for given in paths:
            self._add_file(Path(given), _identity(Path(given)))

    def _add_file(self, path: Path, identity: tuple[int, int] | None) -> None:
        if identity is not None:
            self._by_identity.setdefault(identity, path)
        self._by_path.setdefault(Path(os.path.realpath(path)), path)

    def add_directory(self, key: str, directory: Path, entries: Iterable[Path]) -> None:
        """Register `directory`, which the read setting `key` names, and `entries`.

        `entries` is everything under the directory (`trace_inputs.setting_directory_listings`).
        Each file is an input. The directory and each directory under it are places where no
        delivery goes, whether or not a file is there yet. A directory reached through a link
        counts, where the listing follows the link. A tool of the launch may read any file there,
        and nobody can know that before the tool runs.
        """
        named = Path(directory)
        for path in (named, *entries):
            try:
                st: os.stat_result | None = os.stat(path)
            except OSError:
                st = None
            if st is not None and stat.S_ISDIR(st.st_mode):
                self._directories.setdefault(Path(os.path.realpath(path)), (key, named))
            else:
                self._add_file(path, None if st is None else (st.st_dev, st.st_ino))

    def find(self, destination: Path) -> Optional[Path]:
        """Return the input that `destination` is, or None.

        The method finds the input by the file that `destination` leads to, by `destination` as
        itself, or by its resolved path (an input replaced by a new file since it was
        registered).
        """
        for identity in (_identity(destination), _identity(destination, follow=False)):
            if identity is not None and identity in self._by_identity:
                return self._by_identity[identity]
        return self._by_path.get(Path(os.path.realpath(destination)))

    def directory_of(self, destination: Path) -> tuple[str, Path] | None:
        """Return the read directory that `destination` is or lies in, or None.

        The method finds the directory as named or by its resolved path (a link to one). The
        result is the key of the setting that names the directory, and the directory it names.
        """
        for path in dict.fromkeys((Path(destination), Path(os.path.realpath(destination)))):
            for candidate in (path, *path.parents):
                if candidate in self._directories:
                    return self._directories[candidate]
        return None


class DeliveredFiles:
    """The files that the deliveries of one launch have made, by file identity (`st_dev`, `st_ino`).

    The class also records how each was asked for. A destination that is one of these files was
    named by two deliveries, whatever the names say. This tells it when the comparison before
    the run (`refuse_shared_destinations`) could not. The `Deliveries` of a launch share one
    instance.
    """

    def __init__(self) -> None:
        self._by_identity: dict[tuple[int, int], str] = {}

    def add(self, destination: Path, written_as: str) -> None:
        identity = _identity(destination, follow=False)
        if identity is not None and identity[1]:  # without inode numbers a file system tells none
            self._by_identity.setdefault(identity, written_as)

    def delivered_as(self, destination: Path) -> Optional[str]:
        """Return how the delivery that made the file at `destination` asked for it, if one did."""
        identity = _identity(destination, follow=False)
        return None if identity is None else self._by_identity.get(identity)


def _state(path: Path) -> _State:
    """Return what is at `path`, as itself: size, mtime, inode change time and inode.

    Return None if nothing is there.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


@dataclass(frozen=True)
class _ClockReading:
    """A time read from the clock of a file system, and the file system (`st_dev`) it was read on.

    A record taken at `ns` or later is conclusive by its metadata from `ns` on
    (`digest.FileRecord.trusted`), for a file that is still on `device`. The times in a record
    mean nothing against the clock of another file system.
    """

    ns: int
    device: int


def _destination_clock(destination: Path) -> Optional[_ClockReading]:
    """Return the clock of the file system that `destination` lies on, or None.

    The function reads the clock through a marker file that it makes and removes in the
    directory of the destination (`digest.filesystem_time_ns`, as a check of a run directory
    does). Delivery writes its temporary file there anyway. It never reads the clock of the
    process, because the times in a record come from the file system.

    The result is None when there is no clock to read. Either no marker can be made (a read-only
    directory, or a file system that refuses), or the directory is not on the file system of the
    destination itself (a file mounted from elsewhere). Nothing then anchors a record of the
    destination, and every later check reads its content.
    """
    try:
        directory = destination.parent
        device = os.lstat(destination).st_dev
        if directory.stat().st_dev != device:
            return None
        return _ClockReading(filesystem_time_ns(directory), device)
    except OSError:
        return None


def _recorded_anchor(entry: dict[str, Any], destination: Path) -> Optional[_ClockReading]:
    """Return the anchor that `entry` holds for its record of `destination`, or None.

    The anchor counts only if the file is still on the file system that the clock was read on.
    The result is None for an entry that Xeda wrote before it anchored records, and for a
    destination that moved to another file system. The record is then never trusted by its
    metadata.
    """
    anchored, device = entry.get("anchor_ns"), entry.get("anchor_device")
    try:
        if anchored is None or device != os.lstat(destination).st_dev:
            return None
    except OSError:
        return None
    return _ClockReading(int(anchored), int(device))


def _located(path: Path) -> Path:
    """Return `path` with its parent resolved and its last component as named, never followed."""
    return Path(os.path.realpath(path.parent)) / path.name


def _files_under(directory: Path) -> list[Path]:
    """Return the regular files under `directory`, which is what a directory output delivers.

    The listing does not follow links (`listing.directory_files`). The paths are named under
    `directory` as given.
    """
    top = directory.resolve()
    return sorted(
        directory / p.relative_to(top)
        for p in directory_files(directory)
        if p.is_file() and not p.is_symlink()
    )


def _check_link(delivery: Delivery, source: Path, run_dir: Path) -> None:
    """Refuse a delivery whose source is a symbolic link that cannot be delivered.

    A tool may leave a link. A link to a file, inside the run directory or out of it, delivers
    the content of that file. A link to a directory inside the run directory delivers its tree.
    A link to a directory outside the run directory is never expanded, because it may lead to a
    whole installation tree. A link that leads nowhere, or into a cycle, has nothing to deliver.
    The last two cases raise a `DeliveryError` that names the link.
    """
    if not source.is_symlink():
        return
    target = Path(os.path.realpath(source))
    what = f"`{delivery.key}` names {delivery.destination}, but {source} is a symbolic link"
    if not target.exists():
        raise DeliveryError(
            f"{what} that leads nowhere (to {os.readlink(source)}), or in a cycle",
            output_problem=True,
        )
    root = Path(os.path.realpath(run_dir))
    if target.is_dir() and not (target == root or target.is_relative_to(root)):
        raise DeliveryError(
            f"{what} to {target}, a directory outside the run directory, which Xeda does not "
            "copy: name the files to deliver instead",
            output_problem=True,
        )


def _read_record(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"format": DELIVERY_FORMAT, "files": {}, "directories": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or data.get("format") != DELIVERY_FORMAT:
        return empty
    if not isinstance(data.get("files"), dict) or not isinstance(data.get("directories"), list):
        return empty
    return data


def _refused(conflicts: Sequence[Conflict]) -> str:
    listed = "; ".join(f"{c.destination} ({c.why}), named by `{c.delivery.key}`" for c in conflicts)
    return (
        f"Xeda would replace {listed}: it replaces a file of yours only when you say so. Rerun "
        "with --overwrite-outputs to replace it, or name another path"
    )


class ConfirmedReplacements:
    """The files that a launch was told it may replace, each as it was when the user was asked.

    The launch checks the deliveries of a node ahead, when it starts, and again at the turn of
    the node, after the tools of the flows before it. A yes holds for the file that the user was
    asked about. Asking again about a file that is still as it was would repeat the question. A
    file whose state changed since (size, times, inode) is another file to the user, and another
    question.
    """

    def __init__(self) -> None:
        self._confirmed: dict[Path, _State] = {}

    def confirm(
        self, ask: Callable[[Sequence[Conflict]], bool], conflicts: Sequence[Conflict]
    ) -> bool:
        """Return whether every file of `conflicts` may be replaced.

        `ask` is asked about the files that are not confirmed as they are now. Each file is taken
        as it is when the question is asked, so an edit made while the answer is awaited is not
        covered by the answer. A no is a no for all files.
        """
        now = {conflict.destination: _state(conflict.destination) for conflict in conflicts}
        asking = [
            conflict
            for conflict in conflicts
            if conflict.destination not in self._confirmed
            or self._confirmed[conflict.destination] != now[conflict.destination]
        ]
        if asking and not ask(asking):
            return False
        self._confirmed.update(
            {conflict.destination: now[conflict.destination] for conflict in asking}
        )
        return True


def _discard(temporary: Path, source: Path, moved: bool) -> None:
    """Undo a delivery that did not complete.

    Remove the temporary file of a copy. A moved file is an output of the run and is never
    removed. Put it back where the run left it. If that fails, the error says where the file is.
    """
    if not moved:
        temporary.unlink(missing_ok=True)  # its own temporary file, never anything else
        return
    try:
        os.replace(temporary, source)
    except OSError as e:
        raise DeliveryError(
            f"Xeda moved {source} to {temporary} to deliver it, and the delivery failed. "
            f"Moving it back failed too ({e}). The output is in {temporary}"
        ) from e


class Deliveries:
    """The deliveries of one node.

    The class checks them before any tool of the node runs (`refuse`, `check`), notes them file
    by file once the node succeeded or was found up to date (`collect`), and makes them when the
    launch has finished (`deliver`).
    """

    def __init__(
        self,
        run_path: Path,
        run_root: Path,
        named: Sequence[Delivery] = (),
        *,
        inputs: ReadInputs,
        overwrite: bool = False,
        confirm: Optional[Callable[[Sequence[Conflict]], bool]] = None,
        owner: str = "",
        files: Optional[DeliveredFiles] = None,
    ) -> None:
        self.run_path = Path(run_path)
        self.run_root = Path(os.path.realpath(run_root))
        self.named = list(named)
        #: Every file that the flows of the launch read, shared by the launch (`ReadInputs`).
        self.inputs = inputs
        #: The name of the flow whose deliveries these are. It says how a setting was asked for.
        self.owner = owner
        #: The files that the deliveries of the launch have made, shared by the launch.
        self.launch_files = files if files is not None else DeliveredFiles()
        self.overwrite = overwrite
        self.confirm = confirm
        self.record_path = delivery_record(self.run_path)
        #: The state of the record file just before it was read (`reread_record`).
        self._record_state: _State = _state(self.record_path)
        self.record = _read_record(self.record_path)
        #: Every destination checked before the run, as it was then.
        self.checked: dict[Path, _State] = {}
        #: What the node delivers, noted when it completed (`collect`): each file, where it goes,
        #: and its content digest as the run left it.
        self.pending: list[tuple[Delivery, Path, Path, str]] = []
        #: What `deliver` has copied so far in this call, even one that raises. An `OSError`
        #: partway through must not leave the copies already made unreported.
        self.delivered: list[Delivered] = []

    def reread_record(self) -> None:
        """Read the delivery record again if its file changed since it was read.

        The lock of the run directory keeps the file from changing during the turn of a node. A
        node whose deliveries were made ready before its turn
        (`FlowLauncher._check_deliveries_ahead`) calls this once it holds the lock. Another
        launch of the run directory may have delivered and written the record meanwhile, and what
        this object read then is no longer true. When the file did not change, the record stays
        as it is. The entries that a check anchored in it still vouch for their destinations, so
        nothing is read twice.
        """
        state = _state(self.record_path)
        if state != self._record_state:
            self._record_state = state
            self.record = _read_record(self.record_path)

    def _refusal(self, destination: Path, delivery: Delivery) -> Optional[str]:
        """Return why nothing may ever be delivered to `destination` (located), or None.

        The answer holds whatever the options are.
        """
        if (
            destination == self.run_root
            or destination.is_relative_to(self.run_root)
            or any(is_run_root(d) for d in (destination, *destination.parents))
        ):
            return (
                "inside a run root of Xeda's, where only Xeda's runs live: a bare name puts an "
                "output in its run directory"
            )
        read = self.inputs.directory_of(destination)
        if read is not None:
            key, directory = read
            return (
                f"in a directory a flow of the launch reads (`{key}` names {directory}, and every "
                "file under it, a link followed, is an input of the run): an output never goes "
                "there; name another path"
            )
        found = self.inputs.find(destination)
        if found is not None:
            return (
                f"which is {found}, an input of the run: an output never replaces an input; "
                "name another path"
            )
        if (
            destination.is_dir()
            and not destination.is_symlink()
            and str(destination) not in self.record["directories"]
            and delivery in self.named
        ):
            return "a directory: name the file the output is copied to"
        return None

    def _entry(self, destination: Path) -> Optional[dict[str, Any]]:
        """Return what the record of this run directory says it delivered to `destination`.

        Return None if the record says nothing (a delivery of another settings variant, such as a
        hashed run directory or a remote mirror, is not in it) or if the entry is not a mapping.
        """
        entry = self.record["files"].get(str(destination))
        return entry if isinstance(entry, dict) else None

    def _destination_record(
        self, destination: Path, entry: dict[str, Any]
    ) -> tuple[FileRecord, FileRecord, Optional[_ClockReading]]:
        """Return what `entry` recorded of `destination`, what the file is now, and an anchor.

        The anchor is the time from which the second record is conclusive by its metadata alone.
        It is None when nothing anchors the record.

        The method reads the content of the file only when the recorded metadata cannot vouch for
        it (`FileRecord.trusted`). It reads the clock of the file system of the file just before
        that read (`before_reading` of `record_file`, as `trace.check_trace` does for a run
        directory). Call that time `T`. A record of content read after `T`, whose metadata says
        that the file last changed more than `RACY_NS` before `T`, is conclusive from `T` on. A
        later change moves the mtime or the inode change time, which nobody can set back, so the
        next check recognizes the file without reading it. The anchor is never arithmetic on the
        record already held. It is a clock read at a moment when this very content was verified.

        The anchor of `entry` counts only when the file is still on the file system that the
        clock was read on (`anchor_device`), because times from the clock of another file system
        say nothing about this one. Without an anchor (an entry that Xeda wrote before it
        anchored records, or a destination that moved), the fallback is `recorded_ns`, the change
        time of the file. The file is never settled before that time, so the method reads the
        content. It raises what `record_file` raises.
        """
        record = FileRecord.model_validate(entry["record"])
        stored = _recorded_anchor(entry, destination)
        trusted_before_ns = stored.ns if stored is not None else int(entry["recorded_ns"])
        #: The clock of the file system of the destination, read before the first read of its
        #: content.
        clock: list[Optional[_ClockReading]] = []

        def before_reading() -> None:
            if not clock:
                clock.append(_destination_clock(destination))

        now = record_file(destination, record, trusted_before_ns, False, before_reading)
        if not clock:  # the metadata vouched for the file, so the old anchor still holds
            return record, now, stored
        reading = clock[0]
        if reading is None or not now.settled_before(reading.ns):
            # No clock to anchor the record to, or the file changed within the racy window of the
            # read. The record stays fail-closed, and the next check reads the content again.
            return record, now, None
        return record, now, reading

    def _held(self, destination: Path) -> tuple[FileRecord, Optional[_ClockReading]]:
        """Return what `destination` (a regular file, not a link) holds now, with an anchor.

        The anchor is the time from which the record is conclusive by its metadata alone. It is
        None when nothing anchors the record. The method does not hash the file when the delivery
        record of this run directory vouches for it (`_destination_record`). Otherwise (nothing
        recorded for the file, or an unreadable entry) it reads the content, and nothing anchors
        that record.
        """
        entry = self._entry(destination)
        if entry is not None:
            try:
                _recorded, now, reading = self._destination_record(destination, entry)
                return now, reading
            except (KeyError, TypeError, ValueError):
                pass  # the record is unreadable: read the file
        return FileRecord.of(os.lstat(destination), content_digest(destination)), None

    def _why_not_ours(self, destination: Path) -> Optional[str]:
        """Return why replacing what is at `destination` needs a yes, or None.

        The answer is None if nothing is there, if a directory is there (the destination of a
        directory output, whose files are checked one by one), or if an unchanged earlier
        delivery of Xeda from this run directory is there. The `FileRecord` of that delivery
        (`_destination_record`) holds the same inode and the same content.

        The inode and the content decide, and the timestamps do not. A file of that inode that
        holds exactly the bytes Xeda delivered is a copy of Xeda, whatever touched it (a rewrite
        with the same bytes, or a `touch`), and replacing it cannot lose anything of the user.
        The timestamps only decide whether the content must be read. Another inode (a file put in
        its place) or other bytes is not a copy of Xeda, and the method fails closed.

        After it has read the content, it anchors the record that it took (`_refresh_entry`), so a
        later check of an unchanged delivery recognizes it by its metadata and reads nothing.
        """
        if not os.path.lexists(destination):
            return None
        if destination.is_symlink():
            return "a symbolic link Xeda did not make"
        if destination.is_dir():
            return None
        entry = self._entry(destination)
        if entry is None:
            # The record belongs to this run directory. A delivery of another settings variant
            # (a hashed run directory, a remote mirror) is not in it.
            return f"not recorded as delivered from {self.run_path.name}: yours, or another run's"
        try:
            record, now, reading = self._destination_record(destination, entry)
        except (KeyError, TypeError, ValueError, OSError):
            return "not known to be Xeda's (its record is unreadable)"
        if now.inode != record.inode or now.sha != record.sha:
            return "changed since Xeda wrote it"
        self._refresh_entry(entry, now, reading)
        return None

    @staticmethod
    def _refresh_entry(
        entry: dict[str, Any], now: FileRecord, reading: Optional[_ClockReading]
    ) -> None:
        """Keep in `entry` the record of a destination that is an unchanged copy of Xeda.

        The record gets the anchor, the time from which it is conclusive by its metadata alone.
        When nothing established an anchor, the record has none, which is the fail-closed record
        that Xeda always kept. `recorded_ns`, the change time of the file, is written either way,
        so a Xeda that knows no anchor reads the content for this record. The entry stays in
        memory for the rest of the launch of the node (`_copy` reads it back) and is written out
        when the node delivers (`_write_record`).
        """
        entry["record"] = now.model_dump(mode="json")
        entry["recorded_ns"] = max(now.mtime_ns, now.ctime_ns)
        if reading is not None and now.settled_before(reading.ns):
            entry["anchor_ns"] = reading.ns
            entry["anchor_device"] = reading.device
        else:
            entry.pop("anchor_ns", None)
            entry.pop("anchor_device", None)

    def _confirmed(self, conflicts: Sequence[Conflict]) -> bool:
        return self.overwrite or (self.confirm is not None and bool(self.confirm(conflicts)))

    def check_outputs_to(self, directory: Optional[Path]) -> None:
        """Refuse an unusable `--outputs-to` directory before any tool of the launch runs.

        Raise a `DeliveryError` for a directory that lies in a run root or in a directory that the
        launch reads, that is an input of the run, or that already exists as anything but a
        directory. The loop of `check` sees `--outputs-to` only once a location becomes a
        concrete `Delivery`: from `predicted` (the artifacts of the last run) or from the `extra`
        of `collect` (the artifacts of this run). Without this check, Xeda would refuse the
        directory itself only after the tools of the requested flow, and of every producer of the
        launch, have run.
        """
        if directory is None:
            return
        destination = _located(directory)
        refusal = self._refusal(destination, Delivery(OUTPUTS_TO, PurePath("."), destination))
        if refusal is None and os.path.lexists(destination) and not destination.is_dir():
            refusal = (
                "an existing file: --outputs-to copies outputs into a directory, not onto a file"
            )
        if refusal is not None:
            raise DeliveryError(f"--outputs-to names {destination}, {refusal}", before_run=True)

    def refuse(self, predicted: Sequence[Delivery] = ()) -> None:
        """Refuse an unusable destination before any tool of the launch runs.

        Raise a `DeliveryError` for a destination that is an input, lies in a run root or in a
        directory that the launch reads, or is a directory where a file goes. No answer could
        allow any of them, so a launch makes this check for every flow of its plan before it asks
        the first question (`check`). The check reads no content of a file. `predicted` is what
        `--outputs-to` expects to deliver, from the artifacts of the last run.
        """
        refusals: list[str] = []
        for delivery in [*self.named, *predicted]:
            destination = _located(delivery.destination)
            refusal = self._refusal(destination, delivery)
            if refusal is not None:
                refusals.append(f"`{delivery.key}` names {destination}, {refusal}")
        if refusals:
            raise DeliveryError("; ".join(refusals), before_run=True)

    def check(self, predicted: Sequence[Delivery] = ()) -> None:
        """Check the deliveries before any tool of the node runs.

        First `refuse` what no answer could allow. Then, unless the user confirmed, raise an
        `OutputExistsError` for a file that is not an unchanged earlier delivery of Xeda. A launch
        checks the deliveries of a producer ahead and again at its turn with the same object. The
        second check finds the record that the first anchored, so the launch reads a destination
        once. At its turn, the node first reads the record again if its file changed meanwhile
        (`reread_record`).

        The method takes each destination as it is when examined, and `deliver` compares with
        that state. A file edited while the question was open is not replaced on a yes given for
        the file as it was. `predicted` is what `--outputs-to` expects to deliver, from the
        artifacts of the last run.
        """
        self.refuse(predicted)
        conflicts: list[Conflict] = []
        states: dict[Path, _State] = {}
        for delivery in [*self.named, *predicted]:
            destination = _located(delivery.destination)
            why = self._why_not_ours(destination)
            if why is not None:
                conflicts.append(Conflict(delivery, destination, why))
            states[destination] = _state(destination)
        if conflicts and not self._confirmed(conflicts):
            raise OutputExistsError(_refused(conflicts), before_run=True)
        self.checked = states

    def collect(self, source_root: Path, extra: Sequence[Delivery] = ()) -> None:
        """Note each file that the node delivers, when its run is over.

        Call it under the lock of the run directory. The files come from `source_root` (the run
        directory), for the named deliveries and for the `extra` deliveries known only now (those
        of `--outputs-to`). The method notes each file with the content digest that the run left,
        for `deliver` to copy once the launch has finished. Raise a `DeliveryError` if the run
        wrote no file that a delivery names.
        """
        self.pending = []
        for delivery in [*self.named, *extra]:
            source = Path(source_root) / delivery.name
            if not os.path.lexists(source):
                raise DeliveryError(
                    f"`{delivery.key}` names {delivery.destination}, but the run wrote no "
                    f"{delivery.name} in {source_root}",
                    output_problem=True,
                )
            _check_link(delivery, source, Path(source_root))
            if source.is_dir():
                pairs = [
                    (file, delivery.destination / file.relative_to(source))
                    for file in _files_under(source)
                ]
                directory = str(_located(delivery.destination))
                if directory not in self.record["directories"]:
                    self.record["directories"].append(directory)
            else:
                pairs = [(source, delivery.destination)]
            self.pending += [(delivery, src, dest, content_digest(src)) for src, dest in pairs]

    @staticmethod
    def sources_reached(deliveries: Iterable[Deliveries]) -> Counter[tuple[int, int]]:
        """Return how many noted files, over `deliveries`, reach each file.

        `deliveries` are those of every flow of a launch. A noted file reaches the file it is and
        the file it leads to, found by device and inode, so two spellings of one name are one
        file. A file and a link to it count as one file, even when the link is in the run
        directory of another flow.
        """
        return Counter(
            file
            for delivery in deliveries
            for _delivery, src, _dest, _sha in delivery.pending
            for file in _reached_files(src)
        )

    def deliver(
        self, *, move: bool = False, reached: Optional[Counter[tuple[int, int]]] = None
    ) -> list[Delivered]:
        """Copy each file that `collect` noted to the place where it was named.

        The method refuses a destination that is an input of any flow of the launch, all known by
        now. It does not replace a destination that was checked before the run and changed since,
        and raises a `DeliveryError` once the other deliveries are made. A file found in the way
        only now needs the yes that `check` would have asked for.

        An `OSError` from `_copy` (a full disk, a permission lost during the run) can interrupt
        the method. Whatever was copied before is still recorded (`self.delivered`, and
        `_write_record` under `finally`). A later call, and `flow.deliveries` of the caller
        (reported in `nodes[].deliveries` of `--json`), therefore report all that reached the
        disk before the error.

        With `move`, the caller deletes the run directory right after this call, so a file may
        leave it by rename and not by copy (`_copy`). The caller decides this for each flow and
        never for the whole launch: a flow found up to date keeps its directory, and its trace
        vouches for its outputs there. Only a file that no other delivery names is moved
        (`_movable`). `reached` is the count of the whole launch (`sources_reached`). A file that
        the delivery of another flow reaches, for example a link to it in the run directory of
        that flow, is copied. Without `reached`, only the deliveries of this node are counted.
        """
        self.delivered = []
        movable = self._movable(reached) if move else set()
        changed: list[str] = []
        shared: list[str] = []
        late: list[tuple[Conflict, Path, _State, str]] = []
        try:
            for delivery, src, dest, sha in self.pending:
                destination = _located(dest)
                earlier = self.launch_files.delivered_as(destination)
                if earlier is not None:
                    # Another delivery of this launch made the file. Two deliveries name it,
                    # which the names did not show when they were compared.
                    shared.append(
                        f"{_written_as(self.owner, delivery)} names {destination}, which "
                        f"{earlier} delivered"
                    )
                    continue
                refusal = self._refusal(destination, delivery)
                if refusal is None and destination.is_dir() and not destination.is_symlink():
                    refusal = "a directory"
                if refusal is not None:
                    raise DeliveryError(f"`{delivery.key}` names {destination}, {refusal}")
                if destination in self.checked:
                    expected = self.checked[destination]
                    if _state(destination) != expected:
                        changed.append(str(destination))
                        continue
                else:
                    expected = _state(destination)
                    why = self._why_not_ours(destination)
                    if why is not None:
                        late.append((Conflict(delivery, destination, why), src, expected, sha))
                        continue
                made = self._copy(delivery, src, destination, expected, sha, src in movable)
                if made is None:
                    changed.append(str(destination))
                else:
                    self.delivered.append(made)
            # A refusal comes before a question. A file that two deliveries name fails the
            # launch, whatever the user would say about a file found in the way.
            if late and not shared and self._confirmed([conflict for conflict, *_rest in late]):
                for conflict, src, expected, sha in late:
                    made = self._copy(
                        conflict.delivery, src, conflict.destination, expected, sha, src in movable
                    )
                    if made is None:
                        changed.append(str(conflict.destination))
                    else:
                        self.delivered.append(made)
                late = []
        finally:
            self._write_record()
        self.pending = []
        if shared:
            raise DeliveryError(
                f"{'; '.join(shared)}: a destination takes one output; name them apart"
            )
        if late:
            raise OutputExistsError(_refused([conflict for conflict, *_rest in late]))
        if changed:
            raise DeliveryError(
                f"not delivered: {', '.join(changed)} changed while the run went on, after it "
                f"was checked; the outputs are in {self.run_path}"
            )
        return self.delivered

    def _movable(self, reached: Optional[Counter[tuple[int, int]]] = None) -> set[Path]:
        """Return the sources of the noted files that `deliver` may move.

        These are the sources that only one delivery reaches, among the deliveries of this node
        and in the whole launch. The count for this node is made now, under the lock that
        `deliver` holds, so it sees a link that another launch made to a file since the launch
        counted. The count for the whole launch is `reached` (`sources_reached`), made before the
        first delivery. A file that two deliveries reach is always copied. Moving it for one
        delivery would leave nothing for the other, whichever flow makes its delivery first. Each
        source counts as the file it is and as the file it leads to, which makes a file and a
        link to it one file.
        """
        counts = [Deliveries.sources_reached([self])]
        if reached is not None:
            counts.append(reached)
        return {
            src
            for _delivery, src, _dest, _sha in self.pending
            if (files := _reached_files(src))
            and all(count[f] == 1 for count in counts for f in files)
        }

    def _move_into(self, source: Path, temporary: Path) -> bool:
        """Rename `source` over `temporary` and return whether that happened.

        `temporary` is the placeholder that `mkstemp` made in the directory of the destination.
        Only a regular file with no other name (`st_nlink == 1`) is moved. A link is delivered as
        the file it leads to, and the link stays. A file with another hard link would share its
        inode with that name, and a later write there would change the delivered file. A file in
        a directory outside the run directory, which a link in the run directory may lead to, is
        not moved either, because that would remove a file that is not an output of the run. If
        the system refuses the rename (another file system, a permission), nothing changed and
        the file is copied.
        """
        try:
            found = os.lstat(source)
            run_path = Path(os.path.realpath(self.run_path))
            parent = Path(os.path.realpath(source.parent))
            if (
                not stat.S_ISREG(found.st_mode)
                or found.st_nlink != 1
                or not (parent == run_path or parent.is_relative_to(run_path))
            ):
                return False
            os.replace(source, temporary)
        except OSError as e:
            log.debug("%s is copied, not moved: %s", source, e)
            return False
        return True

    def _copy(
        self,
        delivery: Delivery,
        source: Path,
        destination: Path,
        expected: _State,
        sha: str,
        move: bool = False,
    ) -> Optional[Delivered]:
        """Copy `source` to `destination` (located), which was accepted as it was then (`expected`).

        The method copies into a new temporary file in the directory of the destination and
        renames it into place. Right before the rename, it checks the place again: no parent was
        swapped for a link since the destination was located, and the destination is still no
        input. It also checks that what is there is still `expected`, and otherwise returns None
        and replaces nothing. It checks that the copy is what the run left (`sha`, noted by
        `collect`), and otherwise raises a `DeliveryError`, because another launch ran in the run
        directory meanwhile. It checks the place first, because it reads the temporary file back
        by its path, which must still be where the method made it.

        A destination that already holds the output is not copied. The method reads what the
        destination holds only when the metadata of its record cannot vouch for it (`_held`), so
        an unchanged delivery costs no pass over the destination once a check has anchored its
        record.

        With `move`, the method renames the file of the run to the temporary name when it can
        (`_move_into`), and everything after that is as for a copy. If the delivery does not
        complete, the file goes back to where the run left it (`_discard`).
        """
        if destination.is_file() and not destination.is_symlink():
            try:
                now, reading = self._held(destination)
                if now.sha == sha and _state(destination) == expected:
                    self._remember(delivery, source, destination, sha, reading)
                    return Delivered(delivery.key, source, destination, "unchanged")
            except OSError:
                pass
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".xeda-delivery-", dir=destination.parent)
        temporary = Path(name)
        moved = done = False
        try:
            with os.fdopen(fd, "wb") as out:
                moved = move and self._move_into(source, temporary)
                if not moved:
                    with open(source, "rb") as data:
                        copy_fd(data.fileno(), out.fileno())
                        # by descriptor, because the name may lead elsewhere now
                        if hasattr(os, "fchmod"):
                            os.fchmod(out.fileno(), stat.S_IMODE(os.fstat(data.fileno()).st_mode))
            if (
                _located(destination) != destination
                or self._refusal(destination, delivery) is not None
                or _state(destination) != expected
            ):
                return None
            if content_digest(temporary) != sha:
                raise DeliveryError(
                    f"{source} changed after its run -- did another launch run in "
                    f"{self.run_path} meanwhile? -- so it is not delivered to {destination}",
                    output_problem=True,
                )
            os.replace(temporary, destination)
            done = True
        finally:
            if not done:
                _discard(temporary, source, moved)
        self._remember(delivery, source, destination, sha)
        log.info("%s %s to %s", "Moved" if moved else "Delivered", source, destination)
        return Delivered(delivery.key, source, destination, "delivered")

    def _remember(
        self,
        delivery: Delivery,
        source: Path,
        destination: Path,
        sha: str,
        anchored_at: Optional[_ClockReading] = None,
    ) -> None:
        """Record the file just delivered as a copy of Xeda.

        The record is a `digest.FileRecord` (size, mtime, inode change time, inode, digest) with
        `recorded_ns`, the change time of the file, before which the file is never settled. A
        record with nothing else never vouches by metadata alone, so a later check reads the
        content. A file with another inode is another file. The record fails closed.

        `anchored_at` is what a check of this very content established (`_destination_record`):
        the clock of the file system of the destination, read just before the check read the
        content. A record taken afterwards is conclusive by its metadata from then on. The method
        keeps the anchor only if this record, a fresh `lstat` taken now, is really settled before
        it. Anything that touched the file since the check falls back to the fail-closed record.
        A delivery that copied is never anchored. The file was written a moment ago, which is
        racy by construction, and the first check after the file has settled anchors it after
        reading it once.
        """
        st = os.lstat(destination)
        record = FileRecord.of(st, sha)
        entry: dict[str, Any] = {
            "record": record.model_dump(mode="json"),
            "recorded_ns": max(record.mtime_ns, record.ctime_ns),
            "setting": delivery.key,
            "source": str(source),
        }
        if (
            anchored_at is not None
            and anchored_at.device == st.st_dev
            and record.settled_before(anchored_at.ns)
        ):
            entry["anchor_ns"] = anchored_at.ns
            entry["anchor_device"] = anchored_at.device
        self.record["files"][str(destination)] = entry
        self.launch_files.add(destination, _written_as(self.owner, delivery))

    def _write_record(self) -> None:
        """Write the delivery record atomically, beside the run directory.

        The lock of the run directory serializes the write.
        """
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.record_path.with_name(self.record_path.name + ".tmp")
        temporary.write_text(
            json.dumps(with_json_keys(self.record), default=json_encodable, indent=2)
        )
        os.replace(temporary, self.record_path)
