"""Traces: what a successful run consumed and produced, and whether that still holds.

A trace is written into a run directory after the run succeeded, last and atomically, and it is
deleted before a run starts executing. Its presence means "the last run in this directory
completed and succeeded"; its content says with which settings, programs, inputs and outputs.

Its inputs are recorded as the run found them when it started (`trace_inputs.snapshot_inputs`),
not as it left them: a file edited while the run was going on then no longer matches. Its outputs
are every entry of the run directory (`run_directory_files`: files, links, directories), which
is xeda's; an entry that appears there later -- an empty directory too -- is a change. The
times a file's times are compared with are read from the run directory's file-system clock
(`digest.filesystem_time_ns`), not the process clock.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from pydantic import ValidationError

from ..dataclass import XedaBaseModel
from ..digest import (
    TIME_MARKER_PREFIX,
    UNRECORDED_BEFORE_RUN,
    FileRecord,
    filesystem_time_ns,
    record_file,
)
from ..listing import directory_files
from ..proc_utils import DOCKER_IMAGE_PREFIX
from ..utils import json_encodable, with_json_keys

TRACE_FILE = "trace.json"
#: Raised whenever the meaning of a field changes; a trace of another format is never trusted.
#: 2: inputs recorded before the run (`inputs_recorded_ns`); `xeda_code` covers the package.
#: 3: the outputs of a run directory xeda manages are every file in it; a link is itself.
#: 4: file records carry the inode change time and the inode (`FileRecord.trusted`).
#: 5: outputs are trusted from when they were recorded (`outputs_recorded_ns`).
#: 6: an output link to a file is recorded by the file's content too.
#: 7: where each setting that names no file points (`setting_locations`).
#: 8: inputs include every file under a directory a setting names, each recorded as itself.
#: 9: in a directory xeda does not manage, every file of what the run claimed is an output.
#: 10: written paths are neither inputs nor bound by a location.
#: 11: every run directory is xeda's: its outputs are every file in it, and only the
#: trace's own names are reserved.
#: 12: programs are file records; listings hold directory entries and follow directory links;
#: the reports a run read are recorded.
#: 13: ordered declared-input bindings, including their source/producer provenance.
TRACE_FORMAT = 14

#: The names xeda reserves at the top of a run directory, never outputs: the trace and the trace
#: being written -- and the clock markers, named `digest.TIME_MARKER_PREFIX` + a random suffix.
RESERVED_FILES = (TRACE_FILE, TRACE_FILE + ".tmp")


class ProgramRecord(XedaBaseModel):
    """An executable a run started: where `PATH` found it, resolved, and the file there
    (`record_file`, taken after the run: checked under the trust rule, so an edit given back
    its size and mtime is noticed, and a `touch` costs a hash, not a re-run); a container image
    by its ID, with no file. A program written while the run went on has an unknown file
    record, which never matches."""

    path: str
    file: Optional[FileRecord] = None


class BoundProducer(XedaBaseModel):
    """One producer reference of an input: the plan node, its output key, the identity of the
    run that made it and that run's directory."""

    producer: str
    output: str
    producer_hash: str | None = None
    producer_path: str | None = None


class DeclaredInputRecord(XedaBaseModel):
    """One ordered input binding, independent of the set of input files.

    `references` are the input's producers in binding order; `producer`, `output`,
    `producer_hash` and `producer_path` are the first one's. `binding_origin` and
    `binding_location` say where an explicit binding was written (a chain, a file, the command
    line, the API): explanation only, which `semantic` leaves out, so the same wiring given
    another way is the same binding.
    """

    name: str
    origin: Literal["source", "producer", "none"]
    producer: str | None = None
    output: str | None = None
    producer_hash: str | None = None
    producer_path: str | None = None
    paths: tuple[str, ...] = ()
    references: tuple[BoundProducer, ...] = ()
    binding_origin: str | None = None
    binding_location: str | None = None

    def semantic(self) -> tuple[Any, ...]:
        """What the binding is: its name, origin, ordered producers and ordered files."""
        return (
            self.name,
            self.origin,
            tuple(
                (ref.producer, ref.output, ref.producer_hash, ref.producer_path)
                for ref in self.references
            ),
            self.paths,
        )

    def wiring(self) -> str:
        """Where the input comes from, for a stale reason."""
        if self.origin == "source":
            return "the design's sources"
        if not self.references:
            return "nothing"
        return ", ".join(f"{ref.producer}.{ref.output}" for ref in self.references)


def changed_binding(
    recorded: Sequence[DeclaredInputRecord], current: Sequence[DeclaredInputRecord]
) -> str | None:
    """The first input whose wiring differs from the recorded run's, as a reason naming it;
    else a producer that was configured or wired differently; None if the bindings are, in
    effect, the same (where a binding was written is no difference)."""
    if [r.name for r in recorded] != [r.name for r in current]:
        return "declared input bindings changed (name, order or origin)"
    for old, new in zip(recorded, current):
        if (old.origin, old.wiring()) != (new.origin, new.wiring()):
            return f"{new.name} now from {new.wiring()} (was {old.wiring()})"
    for old, new in zip(recorded, current):
        for before, now in zip(old.references, new.references):
            if before.producer_hash != now.producer_hash:
                return (
                    f"{now.producer}, which makes {new.name}, has other settings or inputs "
                    "than in the last run"
                )
    return None


class Trace(XedaBaseModel):
    """What a successful run consumed and produced."""

    format: int = TRACE_FORMAT
    flow: str
    #: this run, unique among all runs: the flows depending on it record which run they consumed
    run_id: str
    #: the run's identity (`bindings.node_identity`) and the settings-only hash it is made of
    flowrun_hash: str
    settings_hash: str = ""
    design_hash: str
    xeda_version: str
    #: a digest of every file of the installed xeda package: code, templates and data
    xeda_code: str
    #: a digest of the modules defining the flow outside the xeda package ("" if there are none)
    flow_code: str
    programs: Dict[str, Optional[ProgramRecord]] = {}
    #: the `run_id` of each dependency's run this run consumed, by the dependency's run
    #: directory relative to the run root (`xeda_run`), in POSIX form
    dependency_runs: Dict[str, str] = {}
    #: where each path-typed setting pointed, by its key path: what binds a setting that names
    #: a directory, or nothing yet (`trace_inputs.setting_locations`)
    setting_locations: Dict[str, List[str]] = {}
    #: when `inputs` were recorded, just before the run started: the racy-timestamp threshold
    #: for `inputs` and `implicit_inputs`, read from the run directory's file-system clock
    inputs_recorded_ns: int
    #: when `outputs` were recorded, just after the run: their racy-timestamp threshold
    outputs_recorded_ns: int
    #: the expected inputs, as they were when the run started
    inputs: Dict[str, FileRecord] = {}
    #: files the run read that were known only after it (depfiles, and what `run()` registered
    #: in `Flow.implicit_inputs`; what `init()` registered is among `inputs`)
    implicit_inputs: Dict[str, FileRecord] = {}
    #: the run's own files: every entry under its run directory (a link by its target, and a
    #: file it points to by content; a directory or a special file by its metadata) and the
    #: artifacts outside it (`trace_inputs.output_files`)
    outputs: Dict[str, FileRecord] = {}
    #: the reports the run read inside its run directory (`Flow.report_file`), relative to it,
    #: in POSIX form: removed before the next run executes
    reports: List[str] = []
    declared_inputs: list[DeclaredInputRecord] = []


def run_directory_files(run_path: Path) -> list[Path]:
    """Every entry of the run directory `run_path` (`listing.directory_files`, a link as
    itself), but the names xeda reserves at its top (`RESERVED_FILES`, clock markers)."""
    return directory_files(
        run_path,
        reserved=lambda name: name in RESERVED_FILES or name.startswith(TIME_MARKER_PREFIX),
    )


def own_files(trace: Trace, run_dir: Path) -> Set[str]:
    """The files `trace` records as the run's own and nothing else: its outputs that it does not
    also record as inputs, and which lie in the run directory. Location outside it never
    establishes ownership, including evidence from a previous trace."""
    root = run_dir.resolve()
    return {
        path
        for path in set(trace.outputs) - set(trace.inputs) - set(trace.implicit_inputs)
        if Path(path).is_relative_to(root)
    }


def expected_inputs(
    candidates: Iterable[Path], previous: Optional[Trace], run_dir: Path
) -> List[Path]:
    """The `candidates` (every existing file the run would consume) a launch expects as inputs:
    all but those the `previous` trace records as its own inside the run directory."""
    own = own_files(previous, run_dir) if previous is not None else set()
    return sorted(p for p in candidates if str(p) not in own)


def as_recorded(value: Any) -> Any:
    """`value` exactly as `dump_json` writes it (paths as text, tuples as lists, ...)."""
    return json.loads(json.dumps(with_json_keys(value), default=json_encodable))


def read_trace(run_dir: Path) -> Optional[Tuple[Trace, int]]:
    """The trace in `run_dir` and the mtime of its file, or None if there is no valid one."""
    path = run_dir / TRACE_FILE
    try:
        written_ns = path.stat().st_mtime_ns
        return Trace.model_validate(json.loads(path.read_text())), written_ns
    except (OSError, ValueError, ValidationError):
        return None


def _recorded_format(run_dir: Path) -> Optional[int]:
    """The `format` a trace file in `run_dir` claims, whether or not it is valid."""
    try:
        data = json.loads((run_dir / TRACE_FILE).read_text())
    except (OSError, ValueError):
        return None
    recorded = data.get("format") if isinstance(data, dict) else None
    return recorded if isinstance(recorded, int) else None


def previous_trace(run_dir: Path, flow: str) -> Optional[Trace]:
    """The valid trace `flow`'s last run in `run_dir` left, if any: what the next run's inputs
    are told apart from its own files by (`own_files`)."""
    found = read_trace(run_dir)
    if found is None or found[0].format != TRACE_FORMAT or found[0].flow != flow:
        return None
    return found[0]


def write_trace(run_dir: Path, trace: Trace) -> None:
    """Write `trace` under a temporary name -- created anew: a link or leftover at that reserved
    name is removed as itself first -- and rename it into place."""
    path = run_dir / TRACE_FILE
    temporary = run_dir / (TRACE_FILE + ".tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(as_recorded(trace.model_dump(mode="json")), indent=2))
    os.replace(temporary, path)


def remove_trace(run_dir: Path) -> None:
    (run_dir / TRACE_FILE).unlink(missing_ok=True)


def _image_id(reference: str) -> Optional[str]:
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if found.returncode != 0:
        return None
    return found.stdout.strip() or None


def locate_program(name: str) -> Optional[str]:
    """Where `name` is now: resolved through PATH (or as given, if absolute); a container image
    by its ID. None if it cannot be found."""
    if name.startswith(DOCKER_IMAGE_PREFIX):
        return _image_id(name[len(DOCKER_IMAGE_PREFIX) :])
    found = name if os.path.isabs(name) else shutil.which(name)
    if not found or not os.path.exists(found):
        return None
    return str(Path(found).resolve())


@dataclass(frozen=True)
class Expectation:
    """What a run would consume now; compared with what its trace says it consumed."""

    flow: str
    flowrun_hash: str
    design_hash: str
    xeda_version: str
    xeda_code: str
    flow_code: str
    #: every existing file the run would consume (`trace_inputs.candidate_inputs`); those the
    #: previous trace records as its run's own are then not expected (`expected_inputs`)
    inputs: Tuple[Path, ...]
    settings: Any  # the input settings `as_recorded`, to name what changed
    #: where each path-typed setting points now (as `Trace.setting_locations`)
    setting_locations: Mapping[str, List[str]]
    #: the `run_id` of each dependency's run, by its run directory (as `Trace.dependency_runs`)
    dependency_runs: Mapping[str, str]
    #: the flow of each dependency, by the same run directory, to name it in a reason
    dependency_flows: Mapping[str, str]
    declared_inputs: tuple[DeclaredInputRecord, ...] = ()
    #: the hash of the settings alone, to tell a settings change from a change of wiring
    settings_hash: str = ""


@dataclass(frozen=True)
class Freshness:
    """Whether the recorded run is still valid, and if not, the first reason why not.
    `refreshed` is the trace with updated file metadata, when a file was touched but not
    changed; the launcher writes it back so the next check need not hash it again. `run_id` is
    the fresh run's."""

    fresh: bool
    reason: str = ""
    refreshed: Optional[Trace] = None
    run_id: Optional[str] = None


def _flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, Mapping):
        flat: Dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat
    return {prefix[:-1]: value}


def settings_difference(before: Any, after: Any) -> str:
    """The (dotted) names of the settings that differ between two `as_recorded` settings."""
    old, new = _flatten(before), _flatten(after)
    keys = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    shown = ", ".join(keys[:5])
    return shown + (f" and {len(keys) - 5} more" if len(keys) > 5 else "") if keys else "(values)"


def _changed_settings(run_dir: Path, current: Any) -> str:
    try:
        previous = json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    except (OSError, ValueError, KeyError, TypeError):
        return "(the previous settings were not recorded)"
    return settings_difference(previous, current)


def _location(names: Optional[List[str]]) -> str:
    """How a reason names where a setting points (`Trace.setting_locations`)."""
    return ", ".join(names) if names else "nothing that exists"


def check_trace(
    run_dir: Path,
    expected: Expectation,
    locate: Callable[[str], Optional[str]],
    *,
    refresh: bool = True,
) -> Freshness:
    """Re-verify the trace in `run_dir` against `expected`; the first failing check is the
    reason. Files count as unchanged by their metadata when `FileRecord.trusted` says it is
    conclusive, otherwise by content -- a program's file too, found where `locate`
    (`locate_program`) says it is now.

    A file read by content is recorded afresh, and the trace is refreshed with those records
    (`Freshness.refreshed`), trusted from the time the check read its first file on: that time
    is read from the file-system clock of `run_dir`, by writing a marker, just before. If no
    marker can be written there (a read-only run directory), the check goes on and refreshes
    nothing: a refresh only spares later checks a hash, and without one they read the file
    again. With `refresh=False`, validation is read-only, including the clock probe, so reusing
    a producer does not invalidate another consumer's pending completion evidence."""
    found = read_trace(run_dir)
    if found is None:
        recorded = _recorded_format(run_dir)
        if recorded is not None and recorded != TRACE_FORMAT:
            return Freshness(False, "recorded by another xeda")
        return Freshness(False, "no successful previous run")
    trace, _written_ns = found
    if trace.format != TRACE_FORMAT:
        return Freshness(False, "recorded by another xeda")
    if trace.flow != expected.flow:
        return Freshness(False, f"recorded for another flow: {trace.flow}")
    if trace.flowrun_hash != expected.flowrun_hash:
        # The identity is settings plus input origins: say which of them moved.
        rewired = changed_binding(trace.declared_inputs, expected.declared_inputs)
        if rewired is not None and trace.settings_hash == expected.settings_hash:
            return Freshness(False, rewired)
        return Freshness(
            False, "settings changed: " + _changed_settings(run_dir, expected.settings)
        )
    if trace.xeda_version != expected.xeda_version:
        return Freshness(False, f"xeda changed: {trace.xeda_version} -> {expected.xeda_version}")
    if trace.xeda_code != expected.xeda_code:
        return Freshness(False, "xeda's code changed")
    if trace.flow_code != expected.flow_code:
        return Freshness(False, f"the {expected.flow} flow's code changed")
    #: when the check began to read files: from then on, the records it takes vouch for them;
    #: None if the clock could not be read, and then nothing is refreshed
    clock: list[int | None] = []
    #: how many files' content the check has read
    reads = [0]

    def before_reading() -> None:
        reads[0] += 1
        if not clock:
            if not refresh:
                clock.append(None)
                return
            try:
                clock.append(filesystem_time_ns(run_dir))
            except OSError:
                clock.append(None)

    refreshed = trace.model_copy(deep=True)
    #: the records taken by reading a file's content
    read: list[FileRecord] = []
    for name, program in trace.programs.items():
        where = locate(name)
        if program is None or where != program.path:
            if program is None and where is None:
                continue  # not found then, nor now
            return Freshness(False, f"{name} changed")
        if program.file is None:
            continue  # a container image: its ID is its identity
        if program.file.unknown:
            return Freshness(False, f"{name} changed during the last run")
        reads_before = reads[0]
        try:
            current = record_file(
                Path(program.path), program.file, trace.outputs_recorded_ns, True, before_reading
            )
        except OSError:
            return Freshness(False, f"{name} changed")
        if current.sha != program.file.sha:
            return Freshness(False, f"{name} changed")
        if reads[0] > reads_before:
            read.append(current)
        refreshed.programs[name] = ProgramRecord(path=program.path, file=current)
    if [r.semantic() for r in trace.declared_inputs] != [
        r.semantic() for r in expected.declared_inputs
    ]:
        return Freshness(
            False,
            changed_binding(trace.declared_inputs, expected.declared_inputs)
            or "declared input bindings changed (name, order or origin)",
        )
    # A dependency that ran again may have changed files this run read without declaring them,
    # which the comparison of its declared outputs below cannot see.
    for run in sorted(trace.dependency_runs.keys() | expected.dependency_runs.keys()):
        consumed, now_id = trace.dependency_runs.get(run), expected.dependency_runs.get(run)
        if consumed != now_id:
            dependency = expected.dependency_flows.get(run)
            if now_id is None:
                return Freshness(False, f"no longer depends on the run in {run}")
            if consumed is None:
                return Freshness(False, f"new dependency: {dependency} ({run})")
            return Freshness(False, f"{dependency} ({run}) ran again")
    # The identity is location-free: a setting naming a directory, or nothing yet, is bound by
    # where it points -- the cause to name before any file of a directory it now names (a file
    # it names, or one under a directory, is bound by its record, checked below).
    for key in sorted(trace.setting_locations.keys() | expected.setting_locations.keys()):
        was, now_names = trace.setting_locations.get(key), expected.setting_locations.get(key)
        if was != now_names:
            return Freshness(
                False, f"{key} now names {_location(now_names)} (was {_location(was)})"
            )
    now = {str(p) for p in expected_inputs(expected.inputs, trace, run_dir)}
    added = sorted(now - trace.inputs.keys())
    if added:
        return Freshness(False, f"new input: {added[0]}")
    dropped = sorted(trace.inputs.keys() - now)
    if dropped:
        return Freshness(False, f"input no longer used: {dropped[0]}")
    # The directory is xeda's alone: a file the run did not leave there is a change, which a
    # depender reading the directory may see.
    left = trace.outputs.keys()
    for entry in run_directory_files(run_dir):
        if str(entry) not in left:
            return Freshness(False, f"new file in the run directory: {entry}")
    # Every file is checked as the entry it was recorded as: a symbolic link by its target, and
    # the content of the file it points to (a directory's listing holds links, `directory_files`;
    # any other input is recorded by its resolved path, which is no link).
    for records, what, trusted_before_ns in (
        (refreshed.inputs, "input", trace.inputs_recorded_ns),
        (refreshed.implicit_inputs, "input", trace.inputs_recorded_ns),
        (refreshed.outputs, "output", trace.outputs_recorded_ns),
    ):
        for path, record in records.items():
            if record.sha == UNRECORDED_BEFORE_RUN:
                return Freshness(
                    False,
                    f"{what} first read by the last run, on another file system, with no record "
                    f"from before it: {path}",
                )
            if record.unknown:
                return Freshness(False, f"{what} modified during the last run: {path}")
            reads_before = reads[0]
            try:
                current = record_file(Path(path), record, trusted_before_ns, False, before_reading)
            except OSError:
                return Freshness(False, f"{what} missing: {path}")
            if current.sha != record.sha:
                return Freshness(False, f"{what} changed: {path}")
            if reads[0] > reads_before:
                read.append(current)
            records[path] = current
    if trace.design_hash != expected.design_hash:
        return Freshness(False, "design changed (top, parameters, defines or other metadata)")
    checked_ns = clock[0] if clock else None
    # Refreshed, a record read now is trusted by its metadata from `checked_ns` on: worth
    # writing when it changed, or when the file has settled since -- a record that was racy
    # when the run took it, and would be read at every check otherwise. Nothing was read (a
    # record that changed unread is that of a symbolic link to no file, whose target is read
    # afresh every time anyway), or the clock could not be read: nothing to refresh.
    if checked_ns is None or (
        refreshed == trace and not any(record.settled_before(checked_ns) for record in read)
    ):
        return Freshness(True, "", None, trace.run_id)
    # Every record now reflects the file as it was at `checked_ns` or later.
    refreshed.inputs_recorded_ns = checked_ns
    refreshed.outputs_recorded_ns = checked_ns
    return Freshness(True, "", refreshed, trace.run_id)
