"""What a flow run consumed and produced: the files and code its trace records.

**Where inputs are recorded.** Every input the launcher can name before a run -- the design's
files, files its settings name, its dependencies' outputs, and the files the previous run's
depfiles named -- is recorded just before the run starts (`snapshot_inputs`), and the trace keeps
that record: a file edited while a long run is still going no longer matches it, so the next
launch runs again. Files known only after the run (what a depfile names for the first time, what
a flow reads on its own, the programs it started) are recorded afterwards; one written while the
run was going on is recorded as unknown (`digest.unknown_record`), which never matches.

**Whose a file is.** A file's origin decides whether it is an input or the run's own, not its
location: a file the run wrote is recorded among its outputs and is not expected as an input
next time (`trace.own_files`). "Written during the run" is judged by the file's mtime and inode
change time against the run's start (`digest.written_since`). Such a file is the run's own when
it lies in a run directory xeda manages (no user file lives there), when it was created directly
in the directory `--cwd` names (a rendered script), or when the previous run wrote it too;
otherwise it could as well be a user's edit made during the run, and is recorded both ways -- as
an input the next check can refute, and as an output -- so an edit is noticed and a file the run
rewrites each time settles as the run's own after one more run.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Type

from pydantic import BaseModel

from ..artifacts import iter_artifact_paths
from ..design import Design, FileResource
from ..digest import FileRecord, content_digest, record_file, unknown_record, written_since
from ..flow import Flow
from ..flow.flow import _annotation_contains_path, map_path_leaves
from ..proc_utils import DOCKER_IMAGE_PREFIX
from ..version import __version__
from .run_lock import CWD_LOCK, lock_file
from .trace import (
    TRACE_FILE,
    UNKNOWN_PROGRAM_MTIME,
    Expectation,
    ProgramRecord,
    Trace,
    as_recorded,
    expected_inputs,
    probe_program,
)

#: xeda's own files in a run directory: never inputs; `results.json` is an output.
BOOKKEEPING_FILES = ("settings.json", "results.json", TRACE_FILE, TRACE_FILE + ".tmp", CWD_LOCK)


def bookkeeping_files(run_path: Path) -> Set[Path]:
    """The files xeda itself keeps for the run in `run_path`, its lock files included."""
    run_dir = run_path.resolve()
    return {run_dir / name for name in BOOKKEEPING_FILES} | {lock_file(run_path).resolve()}


def _add(found: List[Path], path: Path) -> None:
    resolved = path.resolve()
    if resolved not in found:
        found.append(resolved)


def design_files(design: Design) -> List[Path]:
    """Every file the design's RTL and testbench parts name, resolved: their sources, a
    parameter or define given as a file (`ROM = { file = "rom.mem" }`, which the design holds
    as the file's absolute path), and any other file a field of theirs refers to.

    One walker over every value of both parts: a `FileResource` is its file, and any other
    text or path counts when it is absolute and names an existing file -- relative text (a
    top-level name, a relative parameter) is not resolved, since nothing says against what.
    Counting a value that merely looks like a file errs towards a re-run, never a stale reuse.
    """
    found: List[Path] = []

    def visit(value: Any) -> None:
        if isinstance(value, FileResource):
            if value.file.is_file():
                _add(found, value.file)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                visit(getattr(value, name))
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item)
        elif isinstance(value, (str, os.PathLike)) and os.fspath(value):
            path = Path(value)
            if path.is_absolute() and path.is_file():
                _add(found, path)

    visit(design.rtl)
    visit(design.tb)
    return found


def setting_files(settings: Flow.Settings) -> List[Path]:
    """Every existing file named by a path-typed setting, resolved. A relative path is looked up
    under the design root and under the start directory; each that exists counts.

    Nested models are walked too (an ASIC `platform`, the ghdl plugin's settings inside yosys's),
    except the fields holding a dependency's settings (`dependency_settings`): that
    dependency's own run records its inputs. Each field is walked by `map_path_leaves`, the
    traversal path expansion uses, so a container is read according to its *declared* shape
    rather than by value alone: in `lib_paths`, only the path half of each tuple is ever
    visited, never the library name.
    """
    roots = [
        Path(root)
        for root in (settings.context.get("design_root"), settings.context.get("runner_cwd"))
        if root is not None
    ]
    found: List[Path] = []

    def collect(leaf: Any) -> Any:
        if isinstance(leaf, (str, os.PathLike)) and os.fspath(leaf):
            path = Path(leaf)
            for candidate in [path] if path.is_absolute() else [root / path for root in roots]:
                if candidate.is_file():
                    _add(found, candidate)
        return leaf

    def walk(model: BaseModel) -> None:
        dependencies = model.dependency_settings if isinstance(model, Flow.Settings) else {}
        for name, field in type(model).model_fields.items():
            if name in dependencies:
                continue  # a dependency's settings: its own run records its inputs
            value = getattr(model, name)
            if _annotation_contains_path(field.annotation):
                map_path_leaves(value, field.annotation, collect)
            nested(value)

    def nested(value: Any) -> None:
        if isinstance(value, BaseModel):
            walk(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                nested(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                nested(item)

    walk(settings)
    return found


def output_files(flow: Flow) -> List[Path]:
    """Every file among the flow's artifacts (a directory stands for the files under it), and
    its `results.json`."""
    files = set()
    for leaf in iter_artifact_paths(flow.results.get("artifacts") or flow.artifacts):
        path = Path(leaf) if Path(leaf).is_absolute() else flow.run_path / leaf
        if path.is_file():
            files.add(path.resolve())
        elif path.is_dir():
            files.update(p.resolve() for p in path.rglob("*") if p.is_file())
    results_json = flow.run_path / "results.json"
    if results_json.is_file():
        files.add(results_json.resolve())
    run_dir = flow.run_path.resolve()
    lock_files = {run_dir / TRACE_FILE, run_dir / (TRACE_FILE + ".tmp"), run_dir / CWD_LOCK}
    return sorted(files - lock_files)


def dependency_outputs(flow: Flow) -> List[Path]:
    return sorted({p for dep in flow.completed_dependencies for p in output_files(dep)})


def run_dir_key(run_path: Path, run_root: Path) -> str:
    """How a trace names a run directory: relative to the run root (`xeda_run`), in POSIX form;
    absolute if it lies outside it."""
    path, root = Path(os.path.abspath(run_path)), Path(os.path.abspath(run_root))
    return (path.relative_to(root) if path.is_relative_to(root) else path).as_posix()


def dependency_runs(flow: Flow, run_root: Path) -> Dict[str, str]:
    """The run of each of `flow`'s completed dependencies (`Flow.run_id`, which the launcher
    sets on every flow it completes), by its run directory: two dependencies of one flow, in two
    directories, are two entries."""
    runs: Dict[str, str] = {}
    for dep in flow.completed_dependencies:
        assert dep.run_id is not None, f"{dep.name} was not completed by a launcher"
        runs[run_dir_key(dep.run_path, run_root)] = dep.run_id
    return runs


_DEP_SEPARATOR = re.compile(r"(?<!\\)\s+")


def parse_depfile(path: Path, base: Path) -> List[Path]:
    """The prerequisites a Makefile-style depfile lists, relative ones resolved under `base`."""
    deps: List[Path] = []
    for line in path.read_text().replace("\\\n", " ").splitlines():
        _targets, separator, prerequisites = line.partition(": ")
        if not separator:
            continue
        for token in _DEP_SEPARATOR.split(prerequisites.strip()):
            if token:
                dep = Path(token.replace("\\ ", " ").replace("$$", "$"))
                deps.append(dep if dep.is_absolute() else base / dep)
    return deps


def implicit_input_files(flow: Flow) -> List[Path]:
    """Every existing file the flow read that is known only after its run: those a depfile its
    tools wrote (`yosys -E`, ...) names, and those it registered in `Flow.implicit_inputs`.
    Which of them are inputs and which the run's own is decided by origin (`build_trace`)."""
    named = [
        dep
        for depfile in flow.depfiles
        if depfile.is_file()
        for dep in parse_depfile(depfile, flow.run_path)
    ]
    files = set()
    for path in (*named, *flow.implicit_inputs):
        path = Path(path) if Path(path).is_absolute() else flow.run_path / path
        if path.is_file():
            files.add(path.resolve())
    return sorted(files)


def _package_files(directory: Path) -> List[Path]:
    """The files that make up a Python package directory: everything under it but compiled
    bytecode (`__pycache__`, `.pyc`) and hidden files, which Python and editors create."""
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix not in (".pyc", ".pyo")
        and not any(part.startswith(".") for part in p.relative_to(directory).parts)
    )


def _digest_files(files: Sequence[Path], base: Path) -> str:
    h = hashlib.sha3_256()
    for file in files:
        name = file.relative_to(base) if file.is_relative_to(base) else file
        h.update(name.as_posix().encode() + b"\0")
        h.update(content_digest(file).encode())
    return h.hexdigest()[:32]


#: the installed xeda package directory
XEDA_PACKAGE = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def xeda_code_digest() -> str:
    """A digest of every file of the installed xeda package -- code, templates, bundled data --
    computed once per process. An editable install keeps one version string across edits, so
    this, not the version, is what notices a change to a helper a flow uses."""
    return _digest_files(_package_files(XEDA_PACKAGE), XEDA_PACKAGE)


@lru_cache(maxsize=None)
def flow_code_digest(flow_class: Type[Flow]) -> str:
    """A digest of the modules defining `flow_class` and its bases outside the xeda package
    (a plugin's flow), with their templates; "" when xeda's own package defines them all,
    which `xeda_code_digest` covers."""
    files = set()
    for cls in flow_class.__mro__:
        if not (isinstance(cls, type) and issubclass(cls, Flow)):
            continue
        source = inspect.getsourcefile(cls)
        if source is None:
            continue
        module_file = Path(source).resolve()
        if module_file.is_relative_to(XEDA_PACKAGE):
            continue
        files.add(module_file)
        templates = module_file.parent / "templates"
        if templates.is_dir():
            files.update(p.resolve() for p in templates.rglob("*") if p.is_file())
    if not files:
        return ""
    return _digest_files(sorted(files), Path("/"))


def candidate_inputs(flow: Flow, design: Design, input_settings: Flow.Settings) -> List[Path]:
    """Every existing file the run would consume that can be named before it runs: the
    design's files, the files its settings name, and its dependencies' outputs -- xeda's own
    bookkeeping files in its run directory excepted."""
    bookkeeping = bookkeeping_files(flow.run_path)
    files = {*design_files(design), *setting_files(input_settings), *dependency_outputs(flow)}
    return sorted(files - bookkeeping)


def expectation(
    flow: Flow, design: Design, input_settings: Flow.Settings, run_root: Path
) -> Expectation:
    """What `flow` would consume now. `design` and `input_settings` are the launcher's own
    (never modified by the flow), so the same inputs are found before and after the run;
    `run_root` is the launcher's run directory root, which dependency runs are named under.

    Includes `dependency_outputs(flow)` and `dependency_runs(flow)`, so the result is only
    stable once `flow`'s dependencies have completed (`flow.completed_dependencies` populated)
    -- call this after they have run, as the launcher does.
    """
    assert flow.flow_hash is not None and flow.design_hash is not None
    return Expectation(
        flow=flow.name,
        flowrun_hash=flow.flow_hash,
        design_hash=flow.design_hash,
        xeda_version=__version__,
        xeda_code=xeda_code_digest(),
        flow_code=flow_code_digest(type(flow)),
        inputs=tuple(candidate_inputs(flow, design, input_settings)),
        settings=as_recorded(input_settings),
        dependency_runs=dependency_runs(flow, run_root),
        dependency_flows={
            run_dir_key(dep.run_path, run_root): dep.name for dep in flow.completed_dependencies
        },
    )


@dataclass(frozen=True)
class InputSnapshot:
    """The inputs of a run as they were just before it started (`snapshot_inputs`)."""

    #: when the snapshot was begun: the run's start, from which on a written file is the run's
    started_ns: int
    #: the inputs the run is expected to consume (the `inputs` its trace will record)
    expected: Tuple[Path, ...]
    #: the record of each expected input, and of each file the previous run's depfiles named,
    #: that existed when the run started
    records: Mapping[str, FileRecord]
    #: every file the previous run recorded as an output
    previous_outputs: FrozenSet[str]
    #: whether the run directory is xeda's (not the directory `--cwd` names): a file the run
    #: writes there is its own
    managed: bool
    #: in a directory that is not xeda's, the names directly in it when the run started: a file
    #: created there during the run is the run's own
    preexisting: FrozenSet[str]


def snapshot_inputs(
    expected: Expectation, previous: Optional[Trace], run_path: Path, managed: bool
) -> InputSnapshot:
    """Record every input the run is expected to consume, and every file the `previous` run's
    depfiles named (the likely ones this run's name again), just before the run starts."""
    started_ns = time.time_ns()
    preexisting = frozenset() if managed else frozenset(os.listdir(run_path))
    inputs = tuple(expected_inputs(expected.inputs, previous))
    known = list(inputs)
    if previous is not None:
        own = set(previous.outputs) - set(previous.inputs) - set(previous.implicit_inputs)
        known += [Path(p) for p in previous.implicit_inputs if p not in own]
    records: Dict[str, FileRecord] = {}
    for path in known:
        try:
            records[str(path)] = record_file(path)
        except OSError:
            pass  # absent: if it appears, the next check finds a new input
    return InputSnapshot(
        started_ns=started_ns,
        expected=inputs,
        records=records,
        previous_outputs=frozenset(previous.outputs) if previous is not None else frozenset(),
        managed=managed,
        preexisting=preexisting,
    )


def _programs(names: Sequence[str], started_ns: int) -> Dict[str, Optional[ProgramRecord]]:
    """Each program as it is after the run; one replaced while the run went on never matches."""
    programs: Dict[str, Optional[ProgramRecord]] = {}
    for name in names:
        record = probe_program(name)
        if record is not None and not name.startswith(DOCKER_IMAGE_PREFIX):
            try:
                replaced = written_since(Path(record.path), started_ns)
            except OSError:
                replaced = True  # gone since it was found
            if replaced:
                record = ProgramRecord(
                    path=record.path, size=record.size, mtime_ns=UNKNOWN_PROGRAM_MTIME
                )
        programs[name] = record
    return programs


def build_trace(
    expected: Expectation,
    flow: Flow,
    programs: Sequence[str],
    snapshot: InputSnapshot,
    input_settings: Flow.Settings,
) -> Trace:
    """The trace of `flow`'s just-completed, successful run, under a new `run_id`: its inputs
    as `snapshot` recorded them, and every other file classified by its origin (see the module
    docstring)."""
    started_ns = snapshot.started_ns
    run_dir = flow.run_path.resolve()
    bookkeeping = bookkeeping_files(flow.run_path)

    def written(path: Path) -> bool:
        """Written during the run; a file that vanished since it was listed counts as written."""
        try:
            return written_since(path, started_ns)
        except OSError:
            return True

    def record(path: Path) -> FileRecord:
        """The record of `path` now; unknown if it vanished since it was listed."""
        try:
            return record_file(path)
        except OSError:
            return unknown_record(path)

    def own(path: Path) -> bool:
        """Of a file written during the run: whether it is known to be the run's own."""
        if snapshot.managed:
            if path.is_relative_to(run_dir):
                return True
        elif path.parent == run_dir and path.name not in snapshot.preexisting:
            return True
        return str(path) in snapshot.previous_outputs

    outputs = {str(p): record(p) for p in output_files(flow)}
    inputs: Dict[str, FileRecord] = {}
    implicit: Dict[str, FileRecord] = {}
    named = set(setting_files(input_settings)) - bookkeeping

    for path in snapshot.expected:
        key = str(path)
        before = snapshot.records.get(key)
        if before is None:
            continue  # absent when the run started: a new input next time, if it appears
        if path in named and path.is_file() and written(path):
            outputs[key] = record(path)  # a file a setting names that the run wrote ...
            if own(path):
                continue
        inputs[key] = before  # ... or may have been edited while the run read it
    for path in sorted(named - set(snapshot.expected)):
        key = str(path)
        if key in inputs or key in outputs:
            continue
        # created during the run (it did not exist, or was the previous run's own, before it)
        if written(path):
            outputs[key] = record(path)
        else:
            inputs[key] = record(path)
    for path in implicit_input_files(flow):
        key = str(path)
        if key in inputs or key in outputs or path in bookkeeping:
            continue
        before = snapshot.records.get(key)
        if written(path):
            outputs[key] = record(path)
            if own(path):
                continue
            # what the run read is unknown, unless it was recorded before the run started
            implicit[key] = before if before is not None else unknown_record(path)
        else:
            implicit[key] = before if before is not None else record(path)
    return Trace(
        flow=expected.flow,
        run_id=uuid.uuid4().hex,
        flowrun_hash=expected.flowrun_hash,
        design_hash=expected.design_hash,
        xeda_version=expected.xeda_version,
        xeda_code=expected.xeda_code,
        flow_code=expected.flow_code,
        programs=_programs(programs, started_ns),
        dependency_runs=dict(expected.dependency_runs),
        inputs_recorded_ns=started_ns,
        inputs=inputs,
        implicit_inputs=implicit,
        outputs=outputs,
    )
