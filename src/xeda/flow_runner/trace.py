"""Traces: what a successful run consumed and produced, and whether that still holds.

A trace is written into a run directory after the run succeeded, last and atomically, and it is
deleted before a run starts executing. Its presence means "the last run in this directory
completed and succeeded"; its content says with which settings, programs, inputs and outputs.

Its inputs are recorded as the run found them when it started (`trace_inputs.snapshot_inputs`),
not as it left them: a file edited while the run was going on then no longer matches.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from pydantic import ValidationError

from ..dataclass import XedaBaseModel
from ..digest import FileRecord, record_file
from ..proc_utils import DOCKER_IMAGE_PREFIX
from ..utils import json_encodable, with_json_keys

TRACE_FILE = "trace.json"
#: Raised whenever the meaning of a field changes; a trace of another format is never trusted.
#: 2: inputs recorded before the run (`inputs_recorded_ns`); `xeda_code` covers the package.
TRACE_FORMAT = 2

#: The `mtime_ns` of a program that was replaced while the run went on: it never matches.
UNKNOWN_PROGRAM_MTIME = -1


class ProgramRecord(XedaBaseModel):
    """An executable a run started, as `PATH` resolved it. For a container image, `path` is the
    image ID and the size and mtime are 0."""

    path: str
    size: int = 0
    mtime_ns: int = 0


class Trace(XedaBaseModel):
    """What a successful run consumed and produced."""

    format: int = TRACE_FORMAT
    flow: str
    #: this run, unique among all runs: the flows depending on it record which run they consumed
    run_id: str
    flowrun_hash: str
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
    #: when `inputs` were recorded, just before the run started: the racy-timestamp threshold
    #: for `inputs` and `implicit_inputs` (`outputs` use the trace file's own mtime)
    inputs_recorded_ns: int
    #: the expected inputs, as they were when the run started
    inputs: Dict[str, FileRecord] = {}
    #: files the run read that were known only after it (depfiles, `Flow.implicit_inputs`)
    implicit_inputs: Dict[str, FileRecord] = {}
    #: the run's own files: its artifacts, `results.json`, and every file it wrote that a setting
    #: or a depfile names
    outputs: Dict[str, FileRecord] = {}


def own_files(trace: Trace) -> Set[str]:
    """The files `trace` records as the run's own and nothing else: its outputs that it does not
    also record as inputs. A launch does not expect them as inputs (they are checked as
    outputs), which is what keeps a file the run wrote from looking like a new input next time.
    A file the run wrote that it may also have read (one it cannot tell apart from a user's edit
    during the run) is recorded as both, and so stays an expected input."""
    return set(trace.outputs) - set(trace.inputs) - set(trace.implicit_inputs)


def expected_inputs(candidates: Iterable[Path], previous: Optional[Trace]) -> List[Path]:
    """The `candidates` (every existing file the run would consume) a launch expects as inputs:
    all but those the `previous` trace of the directory records as its run's own."""
    own = own_files(previous) if previous is not None else set()
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
    """Write `trace` under a temporary name and rename it into place."""
    path = run_dir / TRACE_FILE
    temporary = run_dir / (TRACE_FILE + ".tmp")
    temporary.write_text(json.dumps(as_recorded(trace.model_dump(mode="json")), indent=2))
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


def probe_program(name: str) -> Optional[ProgramRecord]:
    """Where `name` is now: resolved through PATH (or as given, if absolute), with size and
    mtime; a container image by its ID. None if it cannot be found."""
    if name.startswith(DOCKER_IMAGE_PREFIX):
        image_id = _image_id(name[len(DOCKER_IMAGE_PREFIX) :])
        return ProgramRecord(path=image_id) if image_id else None
    found = name if os.path.isabs(name) else shutil.which(name)
    if not found or not os.path.exists(found):
        return None
    path = Path(found).resolve()
    st = path.stat()
    return ProgramRecord(path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns)


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
    #: the `run_id` of each dependency's run, by its run directory (as `Trace.dependency_runs`)
    dependency_runs: Mapping[str, str]
    #: the flow of each dependency, by the same run directory, to name it in a reason
    dependency_flows: Mapping[str, str]


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


def check_trace(
    run_dir: Path,
    expected: Expectation,
    probe: Callable[[str], Optional[ProgramRecord]],
) -> Freshness:
    """Re-verify the trace in `run_dir` against `expected`; the first failing check is the
    reason. Files count as unchanged by size and mtime unless racy, otherwise by content."""
    found = read_trace(run_dir)
    if found is None:
        recorded = _recorded_format(run_dir)
        if recorded is not None and recorded != TRACE_FORMAT:
            return Freshness(False, "recorded by another xeda")
        return Freshness(False, "no successful previous run")
    trace, written_ns = found
    if trace.format != TRACE_FORMAT:
        return Freshness(False, "recorded by another xeda")
    if trace.flow != expected.flow:
        return Freshness(False, f"recorded for another flow: {trace.flow}")
    if trace.flowrun_hash != expected.flowrun_hash:
        return Freshness(
            False, "settings changed: " + _changed_settings(run_dir, expected.settings)
        )
    if trace.xeda_version != expected.xeda_version:
        return Freshness(False, f"xeda changed: {trace.xeda_version} -> {expected.xeda_version}")
    if trace.xeda_code != expected.xeda_code:
        return Freshness(False, "xeda's code changed")
    if trace.flow_code != expected.flow_code:
        return Freshness(False, f"the {expected.flow} flow's code changed")
    for name, program in trace.programs.items():
        if probe(name) != program:
            return Freshness(False, f"{name} changed")
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
    now = {str(p) for p in expected_inputs(expected.inputs, trace)}
    added = sorted(now - trace.inputs.keys())
    if added:
        return Freshness(False, f"new input: {added[0]}")
    dropped = sorted(trace.inputs.keys() - now)
    if dropped:
        return Freshness(False, f"input no longer used: {dropped[0]}")
    checked_ns = time.time_ns()
    refreshed = trace.model_copy(deep=True)
    for records, what, trusted_before_ns in (
        (refreshed.inputs, "input", trace.inputs_recorded_ns),
        (refreshed.implicit_inputs, "input", trace.inputs_recorded_ns),
        (refreshed.outputs, "output", written_ns),
    ):
        for path, record in records.items():
            if record.unknown:
                return Freshness(False, f"{what} modified during the last run: {path}")
            try:
                current = record_file(Path(path), record, trusted_before_ns)
            except OSError:
                return Freshness(False, f"{what} missing: {path}")
            if current.sha != record.sha:
                return Freshness(False, f"{what} changed: {path}")
            records[path] = current
    if trace.design_hash != expected.design_hash:
        return Freshness(False, "design changed (top, parameters, defines or other metadata)")
    if refreshed == trace:
        return Freshness(True, "", None, trace.run_id)
    # Every input record now reflects the file as it was at `checked_ns` or later.
    refreshed.inputs_recorded_ns = checked_ns
    return Freshness(True, "", refreshed, trace.run_id)
