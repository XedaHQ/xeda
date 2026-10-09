"""Outputs delivered where the user named them.

A flow's tools write only in its run directory. An output the user names by a location -- a
deliverable setting (`xeda.dataclass.deliverable`) whose value is absolute once its variables
are expanded (`utils.LOCATION_FORMS`), or `--outputs-to DIR` for the requested flow's
artifacts -- is **delivered**: copied there, under the name the user chose, once the launch has
finished, for a flow that succeeded or was found up to date. The setting itself then names the
file in the run directory by its fixed conventional name (`split_deliveries`:
`outputs/<design>.<ext>`, or the setting's default name), and
the run's identity sees only that name (`flow.identity_settings`): where an output goes, and what
it is called there, never changes what the tools do or what the run is.

A delivery never goes onto an input -- any file a flow of the launch reads (`ReadInputs`, by file
identity and resolved path) -- nor into a directory a read setting names (every file there is an
input of the run, one delivered there too), into a run root, or over a directory; it never
deletes anything and never writes through a symbolic link (a temporary file in the
destination's directory, renamed into place). Two deliveries of one launch never go to one
destination, nor one inside another (`refuse_shared_destinations`, which compares names as their
file system does, letter case included where a directory ignores it): the launch refuses it,
naming both, before any tool runs for every destination known then -- the settings' and the files
`--outputs-to` is expected to deliver from the requested flow's last run -- and before the first
copy for those only a run reveals. Two names that still reach one file are found by the file
itself when the second delivery meets the first's (`DeliveredFiles`). An existing file is
replaced only when it is xeda's own earlier delivery, unchanged, as its record says
(`delivery_record`: beside the run directory, in the run root); anything else needs the user's
confirmation -- `overwrite_outputs`, or a yes from the launcher's `confirm_overwrite` (the
command line's prompt) -- asked before any tool of
the launch runs (`Deliveries.check`: the requested flow makes the check of every flow of the plan
when the launch starts, after every refusal no answer could change, `Deliveries.refuse`). A yes
is for the file the user was asked about, as it was then (`ConfirmedReplacements`): a file that
changed since is asked about again. A record is checked by the trust rule like any other
(`digest.FileRecord`): the content of a destination is read only when its metadata cannot vouch
for it, and the check that reads it anchors the record it takes to the clock of the destination's
own file system, read just before (`_destination_clock`), so the next check of an unchanged
delivery reads nothing (`_destination_record`). What a flow delivers is noted with each file's
digest when it completes (`Deliveries.collect`) and copied when the launch has finished
(`Deliveries.deliver`), when every flow of it has read its inputs; a destination that changed
after it was checked, or an output that changed after its run, is never delivered. When the launch
deletes a flow's run directory right after (`--post-cleanup-purge`), the file is moved out of it
instead of copied, so a large output costs no second copy on the disk.
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
from .flow.flow import WrittenLeaf, map_written_leaves, output_name
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
#: the key a delivery `--outputs-to` asked for is reported under
OUTPUTS_TO = "--outputs-to"
#: names xeda keeps for itself in a run directory: an output never takes one
RESERVED_NAMES = frozenset({"settings.json", "results.json", "trace.json", "trace.json.tmp"})

_State = Optional[tuple[int, int, int, int]]


class DeliveryError(XedaException):
    """An output cannot be delivered where it was named.

    `before_run` is true for a refusal made before any tool of the output's flow ran
    (`Deliveries.check_outputs_to` and `check`): nothing of that flow has changed, so the launch
    reports it as its own error, not as the failure of a flow."""

    def __init__(self, *args: object, before_run: bool = False) -> None:
        super().__init__(*args)
        self.before_run = before_run


class OutputExistsError(DeliveryError):
    """A named output path holds a file that is not xeda's own earlier copy, unchanged, and
    replacing it was not confirmed."""


@dataclass(frozen=True)
class Delivery:
    """An output of a run, `name` in its run directory, to be copied to `destination`, as the
    setting (or option) `key` asked."""

    key: str
    name: PurePath
    destination: Path


@dataclass(frozen=True)
class Conflict:
    """A file in a delivery's way: `destination`, located, and why replacing it needs a yes."""

    delivery: Delivery
    destination: Path
    why: str


@dataclass(frozen=True)
class Delivered:
    """A delivery made: "delivered" (written), or "unchanged" (it held the output already)."""

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
    """`<run dir>.delivered.json`, beside the run directory, in the run root: what xeda
    delivered from it, and where. `--clean` and scrubbing leave it."""
    return run_path.parent / f"{run_path.name}{DELIVERY_RECORD_SUFFIX}"


def split_deliveries(settings: Flow.Settings, design: str) -> list[Delivery]:
    """In place: each of the flow's own deliverable settings given as a location becomes its
    conventional name for the design `design` (`flow.output_name`), which the run writes in its
    run directory whatever the location says, and a `Delivery` of that file to the location. A
    producer's settings are its own launch's. A `FlowSettingsError` when two outputs would be
    written under one name, or one under a name xeda keeps for itself."""
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
            assert conventional is not None, key  # every deliverable has one
            name = conventional
        else:
            name = PurePath(path)
        if str(name) in RESERVED_NAMES:
            problems.append(
                (
                    key,
                    f"`{key}` would write {name}, a name xeda keeps for itself in the run "
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
            return leaf
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
    """Every deliverable of `settings` given as a location, by key path; nothing is changed."""
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
    """The name of every field `settings_cls` itself declares deliverable (`DELIVERABLE_ROLE`),
    whatever its current value -- a producer's are its own launch's, so not included. Named in
    the warning `--outputs-to` gives when a flow delivered nothing, so an unset `vcd` is not
    mistaken for xeda silently dropping a file it wrote."""
    return [
        name
        for name in settings_cls.model_fields
        if written_role(settings_cls, name) == DELIVERABLE_ROLE
    ]


def outputs_to_deliveries(
    artifacts: Any, run_path: Path, directory: Optional[Path]
) -> list[Delivery]:
    """What `--outputs-to directory` delivers: each artifact inside the run directory `run_path`
    (named, or through its resolved path), at its path relative to it; one elsewhere is not
    copied, and says so."""
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
    """How the user asked for `delivery`: `--outputs-to`, or the setting as it is written under
    `flows.<flow>` (by its key alone when its flow is not known)."""
    if delivery.key == OUTPUTS_TO:
        return f"`{OUTPUTS_TO}` ({delivery.name})"
    return f"`flows.{owner}.{delivery.key}`" if owner else f"`{delivery.key}`"


#: How many entries of a directory are looked at for a name with letters in it, when the directory
#: is asked whether it ignores their case.
_PROBE_ENTRIES = 64


def _one_entry(first: Path, second: Path) -> bool:
    """Whether two names are one directory entry: the same file, a link not followed."""
    try:
        return os.path.samestat(os.lstat(first), os.lstat(second))
    except OSError:
        return False


def _ignores_case(directory: Path) -> bool:
    """Whether the names in `directory` are compared without regard to letter case, as the default
    file systems of macOS and Windows compare them. Found by looking, never by writing: an entry
    of the directory whose name has ASCII letters, asked for in the other case, is that very
    entry or it is not. A directory with no such entry is asked by its own name in its parent,
    when the two are one file system (`st_dev`), and by its parent's answer when its name has no
    letters either. The top of a file system, and a mount point with nothing to look at, are
    taken to keep case: a wrong "no" is caught when the second delivery finds the file the first
    made (`DeliveredFiles`), where a wrong "yes" would refuse two files that are two."""
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
    """`path`, located, as its file system compares it: its names, each folded to one case when
    the directory it lies in ignores letter case (`_ignores_case`). A directory that does not
    exist yet is taken to compare as the nearest one that does, which it will be made in.
    `ignoring` keeps what each directory looked at said, for the calls that follow."""
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
    """A `DeliveryError` when two copies of one launch go to one destination, or one lies inside
    another (its parent would have to be a file and a directory), naming everything that asked
    for them. A destination takes one output: delivering the first and refusing the second as a
    change nobody made, after the tools ran, gave a false reason and a partial delivery.

    Destinations are compared as their file system compares them (`_compared`): located, as
    `Deliveries` locates every destination, and in letter case only where the directory they lie
    in keeps it. What the comparison still cannot tell -- Unicode forms that a file system takes
    for one name, a link made during the run -- delivery finds in the file itself
    (`DeliveredFiles`).

    `copies`: the name of a flow, one of its deliveries, and where that copy goes, in the order
    the flows run. A named delivery goes to the destination it names, and is known from the plan,
    before any tool runs (`before_run`). A copy of `--outputs-to`, or of a file of a directory
    output, is known only when its flow has run, so the launch asks again then, before the first
    copy is made."""
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
    """The last run's artifact and declared-output paths, for delivery preflight."""
    try:
        results = json.loads(results_json.read_text())
        artifacts = results.get("artifacts") or {}
        outputs = [
            entry["path"]
            for recorded in (results.get("outputs") or {}).values()
            for entry in (recorded if isinstance(recorded, list) else [recorded])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
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
    """The files a delivery of `source` reaches: the file at that name and the file it leads to."""
    return {i for i in (_identity(source, follow=False), _identity(source)) if i is not None}


class ReadInputs:
    """Every file the flows of a launch read: the design's files, the design
    and project file the launch was given, the file each read setting of any of its flows names
    and every entry under a directory one names, as the trace lists it (`trace_inputs.register_read_settings`), by file
    identity (`st_dev`, `st_ino`) and by resolved path; and those directories themselves, each
    with the setting naming it (`directory_of`). No delivery ever replaces one of those files, or
    lands anywhere in one of those directories, `--overwrite-outputs` or not. One is shared by a
    launch's `Deliveries`. The requested flow registers the settings of every flow of the plan
    when the launch starts, and each flow adds the files it prepares as it is launched, so a
    delivery -- made when the launch has finished -- is checked against all of them."""

    def __init__(self, paths: Iterable[Path] = ()) -> None:
        self._by_identity: dict[tuple[int, int], Path] = {}
        self._by_path: dict[Path, Path] = {}
        #: each directory read (resolved), with the key of the setting naming the directory it
        #: was listed under, and that directory
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
        """`directory`, which the read setting `key` names, and `entries`, everything under it
        (`trace_inputs.setting_directory_listings`): each file an input; the directory, and each
        directory under it -- one reached through a link included, where the listing follows it
        -- a place no delivery goes, whether or not a file is there yet: a tool of the launch
        may read any file there, which cannot be known before it runs."""
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
        """The input `destination` is -- followed, as itself, or by its resolved path (an input
        replaced by a new file since it was registered) -- if any."""
        for identity in (_identity(destination), _identity(destination, follow=False)):
            if identity is not None and identity in self._by_identity:
                return self._by_identity[identity]
        return self._by_path.get(Path(os.path.realpath(destination)))

    def directory_of(self, destination: Path) -> tuple[str, Path] | None:
        """The read directory `destination` is, or lies in -- as named, or by its resolved path
        (a link to one) -- if any: the key of the setting naming it, and the directory it
        names."""
        for path in dict.fromkeys((Path(destination), Path(os.path.realpath(destination)))):
            for candidate in (path, *path.parents):
                if candidate in self._directories:
                    return self._directories[candidate]
        return None


class DeliveredFiles:
    """The files the deliveries of one launch have made, by file identity (`st_dev`, `st_ino`),
    and how each was asked for. A destination that is one of them was named by two deliveries,
    whatever the names say: this is what tells it when the comparison made before the run
    (`refuse_shared_destinations`) could not. One is shared by a launch's `Deliveries`."""

    def __init__(self) -> None:
        self._by_identity: dict[tuple[int, int], str] = {}

    def add(self, destination: Path, written_as: str) -> None:
        identity = _identity(destination, follow=False)
        if identity is not None and identity[1]:  # a file system with no inode numbers tells none
            self._by_identity.setdefault(identity, written_as)

    def delivered_as(self, destination: Path) -> Optional[str]:
        """How the delivery that made the file at `destination` asked for it, if one did."""
        identity = _identity(destination, follow=False)
        return None if identity is None else self._by_identity.get(identity)


def _state(path: Path) -> _State:
    """What is at `path`, as itself: size, mtime, inode change time, inode; None if nothing."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


@dataclass(frozen=True)
class _ClockReading:
    """A time read from a file system's own clock, and the file system (`st_dev`) it was read on:
    a record taken at `ns` or later is conclusive by its metadata from `ns` on
    (`digest.FileRecord.trusted`) -- for the file that is still on `device`, since the times a
    record holds mean nothing against another file system's clock."""

    ns: int
    device: int


def _destination_clock(destination: Path) -> Optional[_ClockReading]:
    """The clock of the file system `destination` lies on, read by a marker file made and removed
    in its own directory (`digest.filesystem_time_ns`, as a freshness check reads a run
    directory's) -- where delivery writes its temporary file anyway. None when there is no such
    clock to read: no marker can be made (a directory that is read only, a file system that
    refuses) or the directory is not on the file system the destination itself is on (a file
    mounted from elsewhere), and then nothing anchors a record of it and every later check reads
    its content. Never the process clock: the times a record holds are that file system's own."""
    try:
        directory = destination.parent
        device = os.lstat(destination).st_dev
        if directory.stat().st_dev != device:
            return None
        return _ClockReading(filesystem_time_ns(directory), device)
    except OSError:
        return None


def _recorded_anchor(entry: dict[str, Any], destination: Path) -> Optional[_ClockReading]:
    """What `entry` says anchors its record of `destination`, if the file is still on the file
    system that clock was read on; None otherwise -- an entry xeda wrote before it anchored
    records, or a destination that moved file systems -- and then the record is never trusted by
    its metadata, as it never was."""
    anchored, device = entry.get("anchor_ns"), entry.get("anchor_device")
    try:
        if anchored is None or device != os.lstat(destination).st_dev:
            return None
    except OSError:
        return None
    return _ClockReading(int(anchored), int(device))


def _located(path: Path) -> Path:
    """`path` with its parent resolved and its last component as named: never followed."""
    return Path(os.path.realpath(path.parent)) / path.name


def _files_under(directory: Path) -> list[Path]:
    """The regular files under `directory` (`listing.directory_files`: a link is not followed),
    named under `directory` as given: what a directory output delivers."""
    top = directory.resolve()
    return sorted(
        directory / p.relative_to(top)
        for p in directory_files(directory)
        if p.is_file() and not p.is_symlink()
    )


def _check_link(delivery: Delivery, source: Path, run_dir: Path) -> None:
    """A delivery whose source is a symbolic link (a tool may leave one): a link to a file,
    inside the run directory or out of it, delivers that file's content, and a link to a
    directory inside the run directory delivers its tree -- but a link to a directory outside the
    run directory is never expanded (it may lead to a whole install tree), and a link that leads
    nowhere, or into a cycle, has nothing to deliver. Either is a `DeliveryError` naming it."""
    if not source.is_symlink():
        return
    target = Path(os.path.realpath(source))
    what = f"`{delivery.key}` names {delivery.destination}, but {source} is a symbolic link"
    if not target.exists():
        raise DeliveryError(f"{what} that leads nowhere (to {os.readlink(source)}), or in a cycle")
    root = Path(os.path.realpath(run_dir))
    if target.is_dir() and not (target == root or target.is_relative_to(root)):
        raise DeliveryError(
            f"{what} to {target}, a directory outside the run directory, which xeda does not "
            "copy: name the files to deliver instead"
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
        f"xeda would replace {listed}: it replaces a file of yours only when you say so. Rerun "
        "with --overwrite-outputs to replace it, or name another path"
    )


class ConfirmedReplacements:
    """The files a launch was told it may replace, each as it was when the user was asked.

    A node's deliveries are checked ahead, when the launch starts, and again at the node's turn,
    after the tools of the flows before it. A yes holds for the file the user was asked about:
    asking again about a file that is still as it was would only repeat the question, but a file
    whose state changed since (size, times, inode) is another file to the user, and another
    question."""

    def __init__(self) -> None:
        self._confirmed: dict[Path, _State] = {}

    def confirm(
        self, ask: Callable[[Sequence[Conflict]], bool], conflicts: Sequence[Conflict]
    ) -> bool:
        """Whether every file of `conflicts` may be replaced. `ask` is asked about the files not
        confirmed as they are now, each taken as it is when the question is asked, so that an
        edit made while the answer is awaited is not covered by it. A no is a no for all."""
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
    """Undo a delivery that did not complete. A copy's temporary file is removed. A moved file is
    the run's own output, never removed: it goes back where the run left it, and if that fails
    the error says where it is."""
    if not moved:
        temporary.unlink(missing_ok=True)  # its own temporary file, never anything else
        return
    try:
        os.replace(temporary, source)
    except OSError as e:
        raise DeliveryError(
            f"xeda moved {source} to {temporary} to deliver it, and the delivery failed. "
            f"Moving it back failed too ({e}). The output is in {temporary}"
        ) from e


class Deliveries:
    """One node's deliveries: checked before any of its tools runs (`refuse`, `check`), noted
    file by file once it succeeded or was found up to date (`collect`), and made when the launch
    has finished (`deliver`)."""

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
        #: every file the launch's flows read: shared by the launch (`ReadInputs`)
        self.inputs = inputs
        #: the name of the flow whose deliveries these are, which says how a setting was asked for
        self.owner = owner
        #: the files the launch's deliveries have made, shared by the launch (`DeliveredFiles`)
        self.launch_files = files if files is not None else DeliveredFiles()
        self.overwrite = overwrite
        self.confirm = confirm
        self.record_path = delivery_record(self.run_path)
        #: the record file as it was just before it was read (`reread_record`)
        self._record_state: _State = _state(self.record_path)
        self.record = _read_record(self.record_path)
        #: every destination checked before the run, as it was then
        self.checked: dict[Path, _State] = {}
        #: what the node delivers, noted when it completed (`collect`): each file, where it goes,
        #: and its content digest as the run left it
        self.pending: list[tuple[Delivery, Path, Path, str]] = []
        #: what `deliver` has copied so far this call, even one it goes on to raise out of: an
        #: `OSError` partway through must not make the copies already made unreported
        self.delivered: list[Delivered] = []

    def reread_record(self) -> None:
        """Read the delivery record again if its file changed since it was read. The run
        directory's lock is what keeps the file from changing during a node's turn, so a node
        whose deliveries were made ready before its turn (`FlowLauncher._check_deliveries_ahead`)
        calls this once it holds the lock: another launch of the run directory may have delivered
        and written the record meanwhile, and what this object read then is no longer true. The
        record is kept as it is when its file did not change: the entries a check anchored in it
        still vouch for their destinations, so nothing is read twice."""
        state = _state(self.record_path)
        if state != self._record_state:
            self._record_state = state
            self.record = _read_record(self.record_path)

    def _refusal(self, destination: Path, delivery: Delivery) -> Optional[str]:
        """Why nothing may ever be delivered to `destination` (located), whatever the options."""
        if (
            destination == self.run_root
            or destination.is_relative_to(self.run_root)
            or any(is_run_root(d) for d in (destination, *destination.parents))
        ):
            return (
                "inside a run root of xeda's, where only xeda's runs live: a bare name puts an "
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
        """What this run directory's record says it delivered to `destination`; None if it says
        nothing (another settings variant's delivery -- a hashed run directory, a remote mirror
        -- is not in it) or the entry is not a mapping."""
        entry = self.record["files"].get(str(destination))
        return entry if isinstance(entry, dict) else None

    def _destination_record(
        self, destination: Path, entry: dict[str, Any]
    ) -> tuple[FileRecord, FileRecord, Optional[_ClockReading]]:
        """What `entry` recorded of `destination`, what the file is now, and the time from which
        that second record is conclusive by its metadata alone (None: nothing anchors it).

        The file's content is read only when the recorded metadata cannot vouch for it
        (`FileRecord.trusted`), and the clock of its own file system is read just
        **before** it is (`record_file`'s `before_reading`, as `trace.check_trace` reads a run
        directory's). A record of content read after that time `T`, whose metadata says the file
        last changed more than `RACY_NS` before `T`, is conclusive from `T` on: a change after it
        moves the mtime or the inode change time, which no one can set back, so the next check
        recognizes the file without reading it. The anchor is therefore never arithmetic on the
        record already held -- it is a clock read at a moment this very content was verified.

        `entry`'s own anchor is used only when the file is still on the file system that clock was
        read on (`anchor_device`): times from another file system's clock say nothing about this
        one's. Without one -- an entry xeda wrote before it anchored records, or a destination
        that moved -- the fall-back is `recorded_ns`, the file's own change time, before which it
        is never settled: the content is read, as it always was. Raises as `record_file` does."""
        record = FileRecord.model_validate(entry["record"])
        stored = _recorded_anchor(entry, destination)
        trusted_before_ns = stored.ns if stored is not None else int(entry["recorded_ns"])
        #: the destination file system's clock, read before the first read of its content
        clock: list[Optional[_ClockReading]] = []

        def before_reading() -> None:
            if not clock:
                clock.append(_destination_clock(destination))

        now = record_file(destination, record, trusted_before_ns, False, before_reading)
        if not clock:  # its metadata vouched for it: what anchored that record still does
            return record, now, stored
        reading = clock[0]
        if reading is None or not now.settled_before(reading.ns):
            # no clock to anchor it to, or the file changed within the racy window of the read:
            # the record stays fail-closed, and the next check reads the content again
            return record, now, None
        return record, now, reading

    def _held(self, destination: Path) -> tuple[FileRecord, Optional[_ClockReading]]:
        """What `destination` (a regular file, not a link) holds now, and the time from which
        that record is conclusive by its metadata alone (None: nothing anchors it). Read without
        hashing the file when this run directory's delivery record vouches for it
        (`_destination_record`); otherwise -- nothing recorded for it, or an unreadable entry --
        by reading the content, which nothing anchors."""
        entry = self._entry(destination)
        if entry is not None:
            try:
                _recorded, now, reading = self._destination_record(destination, entry)
                return now, reading
            except (KeyError, TypeError, ValueError):
                pass  # its record is unreadable: read the file
        return FileRecord.of(os.lstat(destination), content_digest(destination)), None

    def _why_not_ours(self, destination: Path) -> Optional[str]:
        """Why replacing what is at `destination` needs a yes; None if nothing is there, it is a
        directory (a directory output's: its files are checked one by one), or it is xeda's own
        earlier delivery from this run directory, unchanged: its `FileRecord`
        (`_destination_record`), the same inode and the same content. The inode and the content
        decide, not the timestamps: a file of that inode
        holding exactly the bytes xeda delivered is xeda's copy whatever touched it -- rewritten
        with the same bytes, or `touch`ed -- and replacing it cannot lose anything of the user's;
        the timestamps only decide whether the content must be read. Another inode (a file put in
        its place) or other bytes is not xeda's: fail closed.

        Having read the content, it anchors the record it took (`_refresh_entry`), so a later
        check of an unchanged delivery recognizes it by its metadata and reads nothing."""
        if not os.path.lexists(destination):
            return None
        if destination.is_symlink():
            return "a symbolic link xeda did not make"
        if destination.is_dir():
            return None
        entry = self._entry(destination)
        if entry is None:
            # the record is this run directory's: another settings variant's delivery (a hashed
            # run directory, a remote mirror) is not in it
            return f"not recorded as delivered from {self.run_path.name}: yours, or another run's"
        try:
            record, now, reading = self._destination_record(destination, entry)
        except (KeyError, TypeError, ValueError, OSError):
            return "not known to be xeda's (its record is unreadable)"
        if now.inode != record.inode or now.sha != record.sha:
            return "changed since xeda wrote it"
        self._refresh_entry(entry, now, reading)
        return None

    @staticmethod
    def _refresh_entry(
        entry: dict[str, Any], now: FileRecord, reading: Optional[_ClockReading]
    ) -> None:
        """Keep in `entry` the record of a destination found to be xeda's own, unchanged, with
        the time from which it is conclusive by its metadata alone -- or with no anchor at all
        when nothing established one, which is the fail-closed record xeda always kept
        (`recorded_ns`, the file's own change time, is written either way, so an xeda that knows
        no anchor reads this record's content as it always did). Held in memory for the rest of
        this node's launch (`_copy` reads it back) and written out when it delivers
        (`_write_record`)."""
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
        """Before any tool of the launch runs: refuse an `--outputs-to` directory that lies in a
        run root or in a directory the launch reads, is an input of the run, or already exists as
        anything but a directory (`DeliveryError`). `check`'s own loop only sees `--outputs-to` once a location becomes a
        concrete `Delivery` -- from `predicted`, the last run's artifacts, or `collect`'s `extra`,
        this run's -- so without this the directory itself is refused only after the requested
        flow's tools (and, for a launch with dependencies, every one of them) have already run."""
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
        """Before any tool of the launch runs: refuse a destination that is an input, lies in a
        run root or in a directory the launch reads, or is a directory where a file goes
        (`DeliveryError`). No answer could allow any of them, so a launch makes this check for
        every flow of its plan before it asks the first question (`check`). It reads no file's
        content. `predicted`: what `--outputs-to` expects to deliver, from the last run's
        artifacts."""
        refusals: list[str] = []
        for delivery in [*self.named, *predicted]:
            destination = _located(delivery.destination)
            refusal = self._refusal(destination, delivery)
            if refusal is not None:
                refusals.append(f"`{delivery.key}` names {destination}, {refusal}")
        if refusals:
            raise DeliveryError("; ".join(refusals), before_run=True)

    def check(self, predicted: Sequence[Delivery] = ()) -> None:
        """Before any tool of the node runs: `refuse` what no answer could allow; unless
        confirmed, refuse to replace a file that is not xeda's own unchanged earlier delivery
        (`OutputExistsError`). A launch checks a producer's deliveries ahead and again at its
        turn with the same object: the second check finds the record the first anchored, so a
        destination is read once for the launch. The turn first reads the record again if its
        file changed meanwhile (`reread_record`).

        Each destination is taken as it is when examined, and that is what `deliver` compares
        with: a file edited while the question was open is not replaced on a yes given for the
        file as it was. `predicted`: what `--outputs-to` expects to deliver, from the last run's
        artifacts."""
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
        """When the node's run is over, under its run directory's lock: note each file it
        delivers -- from `source_root` (the run directory), the named deliveries and the `extra`
        ones known only now (`--outputs-to`'s) -- with its content digest as the run left it, for
        `deliver` to copy once the launch has finished. A `DeliveryError` if the run wrote no
        file a delivery names."""
        self.pending = []
        for delivery in [*self.named, *extra]:
            source = Path(source_root) / delivery.name
            if not os.path.lexists(source):
                raise DeliveryError(
                    f"`{delivery.key}` names {delivery.destination}, but the run wrote no "
                    f"{delivery.name} in {source_root}"
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
        """How many noted files, over `deliveries` (those of every flow of a launch), reach each
        file. A noted file reaches the file it is and the file it leads to (device and inode, so
        two spellings of one name are one file): a file and a link to it count as one file, even
        when the link is in another flow's run directory."""
        return Counter(
            file
            for delivery in deliveries
            for _delivery, src, _dest, _sha in delivery.pending
            for file in _reached_files(src)
        )

    def deliver(
        self, *, move: bool = False, reached: Optional[Counter[tuple[int, int]]] = None
    ) -> list[Delivered]:
        """Copy each file `collect` noted to where it was named. A destination that is an input
        -- of any flow of the launch, all known by now -- is refused; one checked before the run
        that changed since is not replaced (`DeliveryError`, once the others are made); a file
        found in the way only now needs the yes `check` would have asked for. Whatever is copied
        before an `OSError` from `_copy` (a full disk, a permission lost mid-run) is still
        recorded (`self.delivered`, `_write_record` under `finally`), so a later call, and the
        caller's `flow.deliveries` (reported in `--json`'s `nodes[].deliveries`), do not
        under-report what actually reached disk before the error.

        `move`: the caller deletes the run directory right after this call, so a file may leave it
        by rename instead of being copied (`_copy`). The caller decides it for each flow, never for
        the launch: a flow found up to date keeps its directory, and its trace vouches for its
        outputs there. Only a file that no other delivery names is moved (`_movable`). `reached` is
        the count of the whole launch (`sources_reached`): a file that another flow's delivery
        reaches, a link to it in that flow's run directory, is copied. Without it, only this
        node's deliveries are counted."""
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
                    # a file another delivery of this launch made: two deliveries name it, which
                    # the names did not say when they were compared
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
            # a refusal comes before a question: a file two deliveries name fails the launch
            # whatever the user would say about a file found in the way
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
        """The sources of the noted files that `deliver` may move: those that only one delivery
        reaches, in the whole launch (`reached`, `sources_reached`) or, without it, among this
        node's. A file that two deliveries reach is copied every time: moving it for one would
        leave nothing for the other, whichever flow's delivery is made first. Each source counts by
        the file it is and by the file it leads to, which makes a file and a link to it one
        file."""
        if reached is None:
            reached = Deliveries.sources_reached([self])
        return {
            src
            for _delivery, src, _dest, _sha in self.pending
            if (files := _reached_files(src)) and all(reached[f] == 1 for f in files)
        }

    def _move_into(self, source: Path, temporary: Path) -> bool:
        """Rename `source` over `temporary`, the placeholder `mkstemp` made in the destination's
        directory; whether it did. Only a regular file with no other name (`st_nlink == 1`) is
        moved. A link is delivered as the file it leads to, and stays. A file with another hard
        link would share its inode with that name, and a later write there would change the
        delivered file. Nor is a file moved out of a directory outside the run directory, which a
        link in the run directory may lead to: that would remove a file that is not the run's.
        A rename the system refuses (another file system, a permission) changed nothing, and the
        file is copied."""
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
        """Copy `source` to `destination` (located), accepted as it was then (`expected`): into a
        new temporary file in its directory, renamed into place -- checked again right before the
        rename: the same place (no parent swapped for a link since it was located), still no
        input, what is there still `expected` (else None, and nothing replaced), and the copy
        what the run left (`sha`, noted by `collect`; else a `DeliveryError`: another launch ran
        in its run directory meanwhile). The place is re-checked first: the temporary file is
        read back by its path, which must still be where it was made.

        A destination that already holds the output is not copied at all, and what it holds is
        read only when its record's metadata cannot vouch for it (`_held`): an unchanged delivery
        costs no pass over the destination once a check has anchored its record.

        With `move` the run's file itself is renamed to the temporary name when it can be
        (`_move_into`), and everything after is as for a copy. If the delivery does not complete,
        the file goes back to where the run left it (`_discard`)."""
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
                        # by the descriptor: its name may lead elsewhere now
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
                    f"{self.run_path} meanwhile? -- so it is not delivered to {destination}"
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
        """Record the file just delivered as xeda's: a `digest.FileRecord` (size, mtime, inode
        change time, inode, digest) with `recorded_ns`, the file's own change time, before which
        it is never settled -- so a record with nothing else to it never vouches by metadata
        alone and a later check reads the content, a file of another inode being another
        file: failing closed.

        `anchored_at` is what a check of this very content established (`_destination_record`):
        the destination file system's clock, read just before that content was read, so a record
        of it taken afterwards is conclusive by its metadata from then on. It is kept only when
        this record -- a fresh `lstat`, taken now -- is really settled before it, so anything that
        touched the file since the check falls back to the fail-closed record. A delivery that
        copied is never anchored: the file was written here a moment ago, which is racy by
        construction, and the first check after it has settled anchors it after reading it once."""
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
        """Atomically, beside the run directory (the run directory's lock serializes it)."""
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.record_path.with_name(self.record_path.name + ".tmp")
        temporary.write_text(
            json.dumps(with_json_keys(self.record), default=json_encodable, indent=2)
        )
        os.replace(temporary, self.record_path)
