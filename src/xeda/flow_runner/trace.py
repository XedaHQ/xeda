"""Traces: what a completed run consumed and produced, and whether that still holds.

A trace is written into a run directory after a run completed and its checks passed or rejected
it (`Flow.reject`), last and atomically. It is deleted before a run starts executing. Its
presence means "the last run in this directory completed". Its `verdict` says whether its checks
passed or rejected the results. Its content says with which settings, programs, inputs and
outputs.

Its inputs are recorded as the run found them when it started (`trace_inputs.snapshot_inputs`),
not as it left them, so a file edited while the run was going on no longer matches. Its outputs
are every entry of the run directory (`run_directory_files`: files, links, directories), which
belongs to Xeda. An entry that appears there later, an empty directory too, is a change. The
times of a file are compared with times read from the file-system clock of the run directory
(`digest.filesystem_time_ns`), not with the process clock.
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
    Collection,
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
#: 3: the outputs of a run directory that Xeda manages are every file in it; a link is itself.
#: 4: file records carry the inode change time and the inode (`FileRecord.trusted`).
#: 5: outputs are trusted from when they were recorded (`outputs_recorded_ns`).
#: 6: an output link to a file is recorded by the file's content too.
#: 7: where each setting that names no file points (`setting_locations`).
#: 8: inputs include every file under a directory a setting names, each recorded as itself.
#: 9: in a directory that Xeda does not manage, every file of what the run claimed is an output.
#: 10: written paths are neither inputs nor bound by a location.
#: 11: every run directory is Xeda's: its outputs are every file in it, and only the
#: trace's own names are reserved.
#: 12: programs are file records; listings hold directory entries and follow directory links;
#: the reports a run read are recorded.
#: 13: ordered declared-input bindings, including their source/producer provenance.
#: 14: the producers' runs a consumer consumed (`dependency_runs`) decide its freshness.
#: 15: a consumer consumed what its producers handed over (`BoundProducer.entries`), not their
#: files: a producer that ran again with the same outputs leaves it up to date.
#: 16: a run whose checks rejected it keeps a trace (`verdict`, `error`); reuse requires
#: `results.json` to agree with them.
#: 17: the `error` of a trace is an error record: a stable `code`, not the name of a class.
#: 18: metrics have lower-snake names, so a `results.json` with the earlier names is not reused.
TRACE_FORMAT = 18

#: The names that Xeda reserves at the top of a run directory, never outputs: the trace and the
#: trace being written. The clock markers (`digest.TIME_MARKER_PREFIX` plus a random suffix) are
#: reserved too, but they are not in this tuple.
RESERVED_FILES = (TRACE_FILE, TRACE_FILE + ".tmp")


class ProgramRecord(XedaBaseModel):
    """An executable that a run started: where `PATH` found it, resolved, and the file there.

    The file is a `record_file` taken after the run and checked under the trust rule, so an
    edit given back its size and mtime is noticed, and a `touch` costs a hash, not another run.
    A container image is recorded by its ID, with no file. A program written while the run went
    on has an unknown file record, which never matches."""

    path: str
    file: Optional[FileRecord] = None


class OutputEntry(XedaBaseModel):
    """One file of a declared output, as the producer recorded it and as a consumer consumed it.

    It holds the path, the content digest and, for an output of a type that defines one
    (`xeda.equivalence`), the digest of what the type calls equivalent. `unknown` marks a file
    that changed while the consumer ran: it never matches."""

    path: str
    sha: str
    equivalence: str | None = None
    unknown: bool = False


class BoundProducer(XedaBaseModel):
    """One producer reference of an input.

    It holds the plan node, its output key, the identity of the run that made it, the directory
    of that run, and the files of the output as they were handed over (`entries`, in order)."""

    producer: str
    output: str
    producer_hash: str | None = None
    producer_path: str | None = None
    entries: tuple[OutputEntry, ...] = ()


class DeclaredInputRecord(XedaBaseModel):
    """One ordered input binding, independent of the set of input files.

    `references` are the producers of the input in binding order. `producer`, `output`,
    `producer_hash` and `producer_path` are those of the first one. `binding_origin` and
    `binding_location` say where an explicit binding was written (a chain, a file, the command
    line, the API). They are explanation only, which `semantic` leaves out, so the same wiring
    given another way is the same binding.
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
    #: Whether the input compares the equivalence digest of what it was handed
    #: (`In(..., equivalence=True)`), and not only its content digest.
    equivalence: bool = False

    def semantic(self) -> tuple[Any, ...]:
        """Return what the binding is: its name, origin, ordered producers and ordered own files.

        The own files are those that are not a producer's. What a producer handed over is the
        edge (`changed_edge`), judged last."""
        handed = {entry.path for ref in self.references for entry in ref.entries}
        return (
            self.name,
            self.origin,
            tuple(
                (ref.producer, ref.output, ref.producer_hash, ref.producer_path)
                for ref in self.references
            ),
            tuple(path for path in self.paths if path not in handed),
        )

    def wiring(self) -> str:
        """Return where the input comes from, for the reason that a run is stale."""
        if self.origin == "source":
            return "the design's sources"
        if not self.references:
            return "nothing"
        return ", ".join(f"{ref.producer}.{ref.output}" for ref in self.references)


def changed_binding(
    recorded: Sequence[DeclaredInputRecord], current: Sequence[DeclaredInputRecord]
) -> str | None:
    """Return a reason that names the first input whose wiring differs from the recorded run's.

    Otherwise return a reason that names a producer that was configured or wired differently.
    Return None if the bindings are, in effect, the same (where a binding was written is no
    difference)."""
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


def changed_edge(
    flow: str,
    recorded: Sequence[DeclaredInputRecord],
    current: Sequence[DeclaredInputRecord],
) -> tuple[str | None, tuple[str, ...]]:
    """Compare what the producers handed over with what the `recorded` run consumed.

    Return a pair. The first item is a reason that names the input and producer of the first
    output that differs (for a list input, the first reference that changed), else None. The
    second item lists the inputs whose files differ but are equivalent by the digest of their
    type, and so are no change for an input that compares it (`In(..., equivalence=True)`). An
    entry that the recorded run saw change while it ran never matches. This function reads no
    file: the entries are the records of the producers, which hand-over has verified."""
    equivalent: list[str] = []
    for old, new in zip(recorded, current):
        for before, now in zip(old.references, new.references):
            label = f"`{new.name}` from `{now.producer}`"
            if len(before.entries) != len(now.entries):
                return f"{label} changed", ()
            same = True
            for was, is_now in zip(before.entries, now.entries):
                if was.unknown:
                    return f"{label} changed while {flow} ran", ()
                if was.path != is_now.path:
                    return f"{label} changed", ()
                if was.sha == is_now.sha:
                    continue
                if (
                    new.equivalence
                    and was.equivalence is not None
                    and was.equivalence == is_now.equivalence
                ):
                    same = False
                    continue
                return f"{label} changed", ()
            if not same and new.name not in equivalent:
                equivalent.append(new.name)
    return None, tuple(equivalent)


def ran_again_note(records: Sequence[DeclaredInputRecord], ran_again: Collection[str]) -> str:
    """Return why a flow stayed up to date although a producer ran again in this launch.

    The note lists the producers that did (`ran_again`, their run directories), each with the
    outputs that the flow consumed of it, which are unchanged (`changed_edge`). It is empty if
    none of the producers of the flow ran again."""
    parts = []
    references = [ref for record in records for ref in record.references]
    for producer in dict.fromkeys(ref.producer for ref in references):
        if not any(
            ref.producer == producer and ref.producer_path in ran_again for ref in references
        ):
            continue
        outputs = list(
            dict.fromkeys(f"`{ref.output}`" for ref in references if ref.producer == producer)
        )
        noun, verb = ("output", "is") if len(outputs) == 1 else ("outputs", "are")
        parts.append(f"{producer} ran again; its {noun} {', '.join(outputs)} {verb} unchanged")
    return "; ".join(parts)


class Trace(XedaBaseModel):
    """What a completed run consumed and produced, and whether its checks passed it."""

    format: int = TRACE_FORMAT
    flow: str
    #: This run, unique among all runs. The consumers of its outputs record which run they
    #: consumed.
    run_id: str
    #: The identity of the run (`bindings.node_identity`) and the settings-only hash that it is
    #: made of.
    flowrun_hash: str
    settings_hash: str = ""
    design_hash: str
    xeda_version: str
    #: A digest of every file of the installed xeda package: code, templates and data.
    xeda_code: str
    #: A digest of the modules that define the flow outside the xeda package ("" if there are
    #: none).
    flow_code: str
    programs: Dict[str, Optional[ProgramRecord]] = {}
    #: The `run_id` of the run of each producer that this run consumed, by the run directory of
    #: the producer relative to the run root (`xeda_run`), in POSIX form. This is provenance: it
    #: says which run the files were taken from and decides nothing (`declared_inputs` does).
    dependency_runs: Dict[str, str] = {}
    #: Where each path-typed setting pointed, by its key path. This binds a setting that names a
    #: directory, or nothing yet (`trace_inputs.setting_locations`).
    setting_locations: Dict[str, List[str]] = {}
    #: When `inputs` were recorded, just before the run started: the racy-timestamp threshold
    #: for `inputs` and `implicit_inputs`, read from the file-system clock of the run directory.
    inputs_recorded_ns: int
    #: When `outputs` were recorded, just after the run: their racy-timestamp threshold.
    outputs_recorded_ns: int
    #: The expected inputs, as they were when the run started.
    inputs: Dict[str, FileRecord] = {}
    #: Files the run read that were known only after it (depfiles, and what `run()` registered
    #: in `Flow.implicit_inputs`; what `init()` registered is among `inputs`).
    implicit_inputs: Dict[str, FileRecord] = {}
    #: The own files of the run: every entry under its run directory (a link by its target, and
    #: a file it points to by content; a directory or a special file by its metadata) and the
    #: artifacts outside it (`trace_inputs.output_files`).
    outputs: Dict[str, FileRecord] = {}
    #: The reports the run read inside its run directory (`Flow.report_file`), relative to it,
    #: in POSIX form. They are removed before the next run executes.
    reports: List[str] = []
    declared_inputs: list[DeclaredInputRecord] = []
    #: What the checks of the run decided: "passed", or "rejected" (the run called
    #: `Flow.reject`). A launch with the same inputs and settings reports a rejection again
    #: without running.
    verdict: Literal["passed", "rejected"] = "passed"
    #: The `error` of the results of a rejected run (its `code` and `message`, and the other fields
    #: of an error record); None if it passed.
    error: Optional[Dict[str, Any]] = None


def run_directory_files(run_path: Path) -> list[Path]:
    """Return every entry of the run directory `run_path`, except the names that Xeda reserves.

    The entries come from `listing.directory_files` (a link as itself). The reserved names at
    the top of the directory are `RESERVED_FILES` and the clock markers."""
    return directory_files(
        run_path,
        reserved=lambda name: name in RESERVED_FILES or name.startswith(TIME_MARKER_PREFIX),
    )


def own_files(trace: Trace, run_dir: Path) -> Set[str]:
    """Return the files that `trace` records as the run's own and nothing else.

    These are its outputs that it does not also record as inputs, and which lie in the run
    directory. A location outside the run directory never establishes ownership, including
    evidence from a previous trace."""
    root = run_dir.resolve()
    return {
        path
        for path in set(trace.outputs) - set(trace.inputs) - set(trace.implicit_inputs)
        if Path(path).is_relative_to(root)
    }


def expected_inputs(
    candidates: Iterable[Path], previous: Optional[Trace], run_dir: Path
) -> List[Path]:
    """Return the `candidates` that a launch expects as inputs.

    The candidates are every existing file that the run would consume. A launch expects all but
    those that the `previous` trace records as its own inside the run directory."""
    own = own_files(previous, run_dir) if previous is not None else set()
    return sorted(p for p in candidates if str(p) not in own)


def as_recorded(value: Any) -> Any:
    """Return `value` exactly as `dump_json` writes it (paths as text, tuples as lists, ...)."""
    return json.loads(json.dumps(with_json_keys(value), default=json_encodable))


def read_trace(run_dir: Path) -> Optional[Tuple[Trace, int]]:
    """Return the trace in `run_dir` and the mtime of its file, or None if there is no valid one."""
    path = run_dir / TRACE_FILE
    try:
        written_ns = path.stat().st_mtime_ns
        return Trace.model_validate(json.loads(path.read_text())), written_ns
    except (OSError, ValueError, ValidationError):
        return None


def _recorded_format(run_dir: Path) -> Optional[int]:
    """Return the `format` that a trace file in `run_dir` claims, whether or not it is valid."""
    try:
        data = json.loads((run_dir / TRACE_FILE).read_text())
    except (OSError, ValueError):
        return None
    recorded = data.get("format") if isinstance(data, dict) else None
    return recorded if isinstance(recorded, int) else None


def previous_trace(run_dir: Path, flow: str) -> Optional[Trace]:
    """Return the valid trace that the last run of `flow` in `run_dir` left, if any.

    The next run uses it to tell its inputs from its own files (`own_files`)."""
    found = read_trace(run_dir)
    if found is None or found[0].format != TRACE_FORMAT or found[0].flow != flow:
        return None
    return found[0]


def write_trace(run_dir: Path, trace: Trace) -> None:
    """Write `trace` under a temporary name and rename it into place.

    The temporary file is created anew: a link or leftover at that reserved name is removed as
    itself first."""
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
    """Return where `name` is now, or None if it cannot be found.

    A program is resolved through PATH (or taken as given, if absolute). A container image is
    located by its ID."""
    if name.startswith(DOCKER_IMAGE_PREFIX):
        return _image_id(name[len(DOCKER_IMAGE_PREFIX) :])
    found = name if os.path.isabs(name) else shutil.which(name)
    if not found or not os.path.exists(found):
        return None
    return str(Path(found).resolve())


@dataclass(frozen=True)
class Expectation:
    """What a run would consume now.

    It is compared with what the trace of the run says it consumed."""

    flow: str
    flowrun_hash: str
    design_hash: str
    xeda_version: str
    xeda_code: str
    flow_code: str
    #: Every existing file the run would consume (`trace_inputs.candidate_inputs`). The files
    #: that the previous trace records as the own files of its run are not expected
    #: (`expected_inputs`).
    inputs: Tuple[Path, ...]
    settings: Any  # the input settings `as_recorded`, to name what changed
    #: Where each path-typed setting points now (as `Trace.setting_locations`).
    setting_locations: Mapping[str, List[str]]
    #: The `run_id` of the run of each producer, by its run directory (as
    #: `Trace.dependency_runs`).
    dependency_runs: Mapping[str, str]
    declared_inputs: tuple[DeclaredInputRecord, ...] = ()
    #: The hash of the settings alone, to tell a settings change from a change of wiring.
    settings_hash: str = ""


@dataclass(frozen=True)
class Freshness:
    """Whether the recorded run is still valid, and if not, the first reason why not.

    `refreshed` is the trace with updated file metadata, when a file was touched but not
    changed. The launcher writes it back, so the next check need not hash the file again.
    `run_id` is the id of the run that is up to date. `verdict` and `error` are those of its
    trace when it is up to date: a fresh trace of a rejected run says the run was rejected."""

    fresh: bool
    reason: str = ""
    refreshed: Optional[Trace] = None
    run_id: Optional[str] = None
    #: The inputs whose files differ from the ones consumed but are equivalent
    #: (`changed_edge`).
    equivalent: tuple[str, ...] = ()
    verdict: str = "passed"
    error: Optional[Dict[str, Any]] = None


def _flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, Mapping):
        flat: Dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat
    return {prefix[:-1]: value}


def settings_difference(before: Any, after: Any) -> str:
    """Return the (dotted) names of the settings that differ between two `as_recorded` settings."""
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
    """Return how a reason names where a setting points (`Trace.setting_locations`)."""
    return ", ".join(names) if names else "nothing that exists"


def check_trace(
    run_dir: Path,
    expected: Expectation,
    locate: Callable[[str], Optional[str]],
    *,
    refresh: bool = True,
    edge: bool = True,
) -> Freshness:
    """Re-verify the trace in `run_dir` against `expected`. The first failing check is the reason.

    A file counts as unchanged by its metadata when `FileRecord.trusted` says that is
    conclusive, otherwise by its content. That holds for the file of a program too, found
    where `locate` (`locate_program`) says it is now.

    A file read by content is recorded afresh, and the trace is refreshed with those records
    (`Freshness.refreshed`). They are trusted from the time when the check read its first file
    on. That time is read from the file-system clock of `run_dir`, by writing a marker, just
    before. If no marker can be written there (a read-only run directory), the check goes on
    and refreshes nothing: a refresh only spares later checks a hash, and without one they read
    the file again. With `refresh=False`, validation is read-only, the clock probe included, so
    reusing a producer does not invalidate the pending completion evidence of another
    consumer. With `edge=False`, what the producers handed over is not compared (a dry run that
    does not know it yet judges everything else)."""
    found = read_trace(run_dir)
    if found is None:
        recorded = _recorded_format(run_dir)
        if recorded is not None and recorded != TRACE_FORMAT:
            return Freshness(False, "recorded by another Xeda")
        return Freshness(False, "no completed previous run")
    trace, _written_ns = found
    if trace.format != TRACE_FORMAT:
        return Freshness(False, "recorded by another Xeda")
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
        return Freshness(False, f"Xeda changed: {trace.xeda_version} -> {expected.xeda_version}")
    if trace.xeda_code != expected.xeda_code:
        return Freshness(False, "Xeda's code changed")
    if trace.flow_code != expected.flow_code:
        return Freshness(False, f"the {expected.flow} flow's code changed")
    #: When the check began to read files: from then on, the records it takes vouch for them.
    #: None if the clock could not be read; then nothing is refreshed.
    clock: list[int | None] = []
    #: How many files' content the check has read.
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
    #: The records taken by reading a file's content.
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
    # The identity is location-free. A setting that names a directory, or nothing yet, is bound
    # by where it points. This is the cause to name before any file of a directory that it now
    # names. (A file that a setting names, or one under a directory, is bound by its record,
    # checked below.)
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
    # The directory belongs to Xeda alone: a file that the run did not leave there is a change,
    # which a consumer that reads the directory may see.
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
    # Last: everything above is known without the producers, so a flow that is stale for its
    # own reasons says so, whatever the producers did.
    equivalent: tuple[str, ...] = ()
    if edge:
        changed, equivalent = changed_edge(
            expected.flow, trace.declared_inputs, expected.declared_inputs
        )
        if changed is not None:
            return Freshness(False, changed)
    checked_ns = clock[0] if clock else None
    # Refreshed, a record read now is trusted by its metadata from `checked_ns` on. It is worth
    # writing when it changed, or when the file has settled since: a record that was racy when
    # the run took it would be read at every check otherwise. There is nothing to refresh when
    # nothing was read (a record that changed unread is that of a symbolic link to no file,
    # whose target is read afresh every time anyway), or when the clock could not be read.
    if checked_ns is None or (
        refreshed == trace and not any(record.settled_before(checked_ns) for record in read)
    ):
        return Freshness(True, "", None, trace.run_id, equivalent, trace.verdict, trace.error)
    # Every record now reflects the file as it was at `checked_ns` or later.
    refreshed.inputs_recorded_ns = checked_ns
    refreshed.outputs_recorded_ns = checked_ns
    return Freshness(True, "", refreshed, trace.run_id, equivalent, trace.verdict, trace.error)
