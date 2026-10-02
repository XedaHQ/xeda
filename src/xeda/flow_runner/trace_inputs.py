"""What a flow run consumed and produced: the files and code its trace records.

**Where inputs are recorded.** Every input the launcher can name before a run -- the design's
files, files its settings name and every file under a directory they name, its dependencies'
outputs, the files the flow registered in its `init()`, and the files the previous run's
depfiles named -- is recorded just before the run starts (`snapshot_inputs`), and the trace keeps
that record: a file edited while a long run is still going no longer matches it, so the next
launch runs again. Files known only after the run (what a depfile names for the first time, what
a flow reads on its own, the programs it started) are recorded afterwards; one written while the
run was going on is recorded as unknown (`digest.unknown_record`), which never matches.

**Whose a file is.** Every run directory is xeda's (D21): a file inside the run's own directory
is the run's; anywhere else, a file the run's settings, depfiles or design name stays an input,
recorded as unknown if it changed during the run, so the next launch runs again.

**What a run produced.** Every file the run left in its run directory is an output (`output_files`), declared as an artifact or not: a depender may read any of them by
path, so a hand edit anywhere in the directory -- or a file added there -- makes the run stale,
and its dependers follow through its new `run_id`. They are hashed once, after the run; a check
trusts their metadata (`digest.FileRecord.trusted`) as it does an input's.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Type

from pydantic import BaseModel

from ..artifacts import iter_artifact_paths
from ..design import Design, FileResource
from ..digest import (
    MODIFIED_DURING_RUN,
    UNRECORDED_BEFORE_RUN,
    FileRecord,
    content_digest,
    filesystem_time_ns,
    record_file,
    unknown_record,
    written_since,
)
from ..flow import Flow
from ..flow.io import declared_inputs
from ..dataclass import written_role
from ..deliver import ReadInputs
from ..flow.flow import _annotation_contains_path, map_keyed_path_leaves
from ..listing import VCS_METADATA, directory_files
from ..proc_utils import DOCKER_IMAGE_PREFIX, program_state
from ..version import __version__
from .run_lock import lock_file
from .trace import (
    RESERVED_FILES,
    Expectation,
    ProgramRecord,
    Trace,
    as_recorded,
    expected_inputs,
    locate_program,
    run_directory_files,
)

log = logging.getLogger(__name__)

#: xeda's own files in a run directory: never inputs; `results.json` is an output.
BOOKKEEPING_FILES = ("settings.json", "results.json", *RESERVED_FILES)


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


def setting_path_leaves(
    settings: Flow.Settings, *, written: Optional[bool] = None, dependencies: bool = False
) -> list[tuple[str, Any]]:
    """Every path-typed leaf of `settings`, by its key path (`lib_paths[0][1]`, `platform.
    tech_lef`).

    Nested models are walked too (an ASIC `platform`, the ghdl plugin's settings inside yosys's),
    except the fields holding a dependency's settings (`dependency_settings`): that
    dependency's own run records them -- walked too with `dependencies`, by their key path
    (`synth.xdc_files[0]`). Each field is walked by `map_keyed_path_leaves`, the
    traversal path expansion uses, so a container is read according to its *declared* shape
    rather than by value alone: in `lib_paths`, only the path half of each tuple is ever
    visited, never the library name. With `written`, only the leaves of fields the flow writes
    (True: a role, `xeda.dataclass.written_role`) or reads (False).
    """
    leaves: list[tuple[str, Any]] = []

    def collect(key: str, leaf: Any) -> Any:
        if isinstance(leaf, (str, os.PathLike)) and os.fspath(leaf):
            leaves.append((key, leaf))
        return leaf

    def walk(model: BaseModel, prefix: str) -> None:
        skipped = (
            model.dependency_settings
            if isinstance(model, Flow.Settings) and not dependencies
            else {}
        )
        for name, field in type(model).model_fields.items():
            if name in skipped:
                continue  # a dependency's settings: its own run records them
            value = getattr(model, name)
            role = written_role(type(model), name)
            if _annotation_contains_path(field.annotation) and (
                written is None or written is (role is not None)
            ):
                map_keyed_path_leaves(value, field.annotation, collect, prefix + name)
            nested(value, prefix + name)

    def nested(value: Any, key: str) -> None:
        if isinstance(value, BaseModel):
            walk(value, key + ".")
        elif isinstance(value, Mapping):
            for name, item in value.items():
                nested(item, f"{key}[{name}]")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                nested(item, f"{key}[{index}]")

    walk(settings, "")
    return leaves


def _setting_roots(settings: Flow.Settings) -> list[Path]:
    """What a relative path a setting names is looked up under: the design root and the start
    directory."""
    return [
        Path(root)
        for root in (settings.context.get("design_root"), settings.context.get("runner_cwd"))
        if root is not None
    ]


def _candidates(leaf: Any, roots: Sequence[Path]) -> list[Path]:
    path = Path(leaf)
    return [path] if path.is_absolute() else [root / path for root in roots]


def setting_file_keys(
    settings: Flow.Settings, *, dependencies: bool = False
) -> dict[Path, set[str]]:
    """Every existing file named by a path-typed setting the flow reads (`setting_path_leaves`),
    resolved, with the key path of each setting naming it. A relative path is looked up under
    the design root and under the start directory; each that exists counts. A setting naming
    what the flow writes (a role, `xeda.dataclass.written_role`) names no input. With
    `dependencies`, the read settings of the dependencies' settings nested in `settings` too,
    their relative paths looked up under the same roots (they are their depender's)."""
    roots = _setting_roots(settings)
    found: dict[Path, set[str]] = {}
    for key, leaf in setting_path_leaves(settings, written=False, dependencies=dependencies):
        for candidate in _candidates(leaf, roots):
            if candidate.is_file():
                found.setdefault(candidate.resolve(), set()).add(key)
    return found


def setting_files(settings: Flow.Settings, *, dependencies: bool = False) -> List[Path]:
    """Every existing file named by a path-typed setting the flow reads (`setting_file_keys`),
    resolved; with `dependencies`, those of the dependencies' settings nested in it too."""
    return list(setting_file_keys(settings, dependencies=dependencies))


#: A directory a setting names whose listing holds more files than this, or takes longer than
#: `SLOW_LISTING_S` to list, is reported: every file in it is an input, recorded before each run
#: (hashed when its metadata cannot vouch for it) and checked by its metadata at each launch.
#: There is no cap: a file past one would go unnoticed.
LARGE_LISTING_FILES = 10_000
SLOW_LISTING_S = 2.0


def setting_directories(
    settings: Flow.Settings, run_path: Path, *, dependencies: bool = False
) -> list[tuple[str, Path]]:
    """Every existing directory a path-typed setting the flow reads names (`setting_path_leaves`),
    with the key path of the first setting naming it, resolved: an absolute path as itself, a
    relative one under the design root and under the start directory (as `setting_files`) --
    but the run directory and every directory in it, whose files are outputs of the run already.
    A directory the flow writes (`reports_dir`, `sim_dir`: a role,
    `xeda.dataclass.written_role`) is no input. With `dependencies`, those the dependencies'
    settings nested in `settings` name too (as `setting_file_keys`)."""
    roots = _setting_roots(settings)
    run_dir = run_path.resolve()
    found: dict[Path, str] = {}
    for key, leaf in setting_path_leaves(settings, written=False, dependencies=dependencies):
        for candidate in _candidates(leaf, roots):
            if candidate.is_dir():
                resolved = candidate.resolve()
                if resolved.is_relative_to(run_dir):
                    continue
                found.setdefault(resolved, key)
    return [(key, directory) for directory, key in found.items()]


def setting_directory_listings(
    settings: Flow.Settings,
    run_path: Path,
    run_root: Path | None = None,
    *,
    dependencies: bool = False,
) -> list[tuple[str, Path, list[Path]]]:
    """Each directory a setting names (`setting_directories`), with the key path of the setting
    naming it and every entry under it: its recursive listing (`listing.directory_files`: a
    symbolic link as itself, and a link to a directory followed too, its entries by their path
    through it; a subdirectory and a special file as entries). The run directory and the run
    root `run_root` are not entered where a named directory holds them, however it reaches them:
    their files are the runs' own; nor is version control's metadata (`VCS_METADATA`), which no
    tool reads. A large or slow listing is reported, never capped. The one listing of what a
    flow reads through a directory: the trace records it (`setting_directory_files`), and the
    delivery guard never delivers into it (`register_read_settings`)."""
    prune = [run_path] + ([run_root] if run_root is not None else [])
    listings: list[tuple[str, Path, list[Path]]] = []
    for key, directory in setting_directories(settings, run_path, dependencies=dependencies):
        started = time.monotonic()
        listed = directory_files(directory, prune, VCS_METADATA, follow_links=True)
        elapsed = time.monotonic() - started
        if len(listed) > LARGE_LISTING_FILES or elapsed > SLOW_LISTING_S:
            log.warning(
                "%s names %s: its %d files are inputs of the run (listed in %.1f s), each checked "
                "at every launch and hashed when changed",
                key,
                directory,
                len(listed),
                elapsed,
            )
        listings.append((key, directory, listed))
    return listings


def setting_directory_files(
    settings: Flow.Settings, run_path: Path, run_root: Path | None = None
) -> list[Path]:
    """Every entry under each directory a setting names (`setting_directory_listings`), so that
    a file edited, added or removed there -- a library recompiled in place, an empty directory
    added, an edit beneath a linked directory -- makes the run stale."""
    return [
        path
        for _key, _directory, listed in setting_directory_listings(settings, run_path, run_root)
        for path in listed
    ]


def register_read_settings(
    inputs: ReadInputs, settings: Flow.Settings, run_path: Path, run_root: Path
) -> None:
    """Register with a launch's delivery guard (`xeda.deliver.ReadInputs`) everything the read
    settings of `settings` -- a dependency's settings nested in them included -- make an input,
    as the trace records it: every file one names (`setting_files`), and every directory one
    names with each entry under it (`setting_directory_listings`, the trace's own listing), so
    that no delivery lands on a file the launch read, nor anywhere in a directory it reads."""
    inputs.add(setting_files(settings, dependencies=True))
    for key, directory, listed in setting_directory_listings(
        settings, run_path, run_root, dependencies=True
    ):
        inputs.add_directory(key, directory, listed)


def setting_locations(settings: Flow.Settings, run_path: Path) -> dict[str, list[str]]:
    """Where each path-typed setting the flow reads points, by key path (`lib_paths[0][1]`); a
    setting naming what it writes is bound by no location.

    The run's identity is location-free: `$PWD/libs` and `$DESIGN_ROOT/libs` count as written,
    so a launch from another start directory, or of another design tree with the same text,
    has the same identity. A file a setting names is bound by its record, an input; a directory
    (a compiled library, an include directory) or a path that does not exist is bound by
    nothing else, so the trace records where each setting points, and a launch where one points
    elsewhere is stale.

    An absolute path (a `$PWD`/`$DESIGN_ROOT` path is absolute once expanded) is its own
    location, resolved, whatever it names -- a file, a directory, nothing yet -- so that a run
    creating it changes nothing. A relative one points at each existing path it is looked up as,
    under the design root and under the start directory (`setting_files`), except inside the run
    directory `run_path`, whose place is the trace's own; none, if there is no such path."""
    roots = _setting_roots(settings)
    run_dir = run_path.resolve()
    locations: dict[str, list[str]] = {}
    for key, leaf in setting_path_leaves(settings, written=False):
        found: list[str] = []
        for candidate in _candidates(leaf, roots):
            resolved = candidate.resolve()
            if not Path(leaf).is_absolute() and (
                not candidate.exists() or resolved.is_relative_to(run_dir)
            ):
                continue
            if str(resolved) not in found:
                found.append(str(resolved))
        locations[key] = found
    return locations


def artifact_files(flow: Flow) -> list[Path]:
    """Every file among the flow's artifacts (a directory stands for the regular files under it,
    `listing.directory_files`: a link in it is not followed out), and its `results.json`,
    resolved."""
    files = set()
    for leaf in iter_artifact_paths(flow.results.get("artifacts") or flow.artifacts):
        path = Path(leaf) if Path(leaf).is_absolute() else flow.run_path / leaf
        if path.is_file():
            files.add(path.resolve())
        elif path.is_dir():
            files.update(
                p.resolve() for p in directory_files(path) if p.is_file() and not p.is_symlink()
            )
    results_json = flow.run_path / "results.json"
    if results_json.is_file():
        files.add(results_json.resolve())
    run_dir = flow.run_path.resolve()
    return sorted(files - {run_dir / name for name in RESERVED_FILES})


def output_files(flow: Flow) -> list[Path]:
    """The files the trace of `flow`'s run records as its own: every entry in its run directory
    (`run_directory_files`), since a depender may read any of them, and the artifacts outside
    it."""
    run_dir = flow.run_path.resolve()
    outside = [path for path in artifact_files(flow) if not path.is_relative_to(run_dir)]
    return sorted({*run_directory_files(flow.run_path), *outside})


def dependency_outputs(flow: Flow) -> List[Path]:
    """The declared outputs of `flow`'s dependencies, which it consumes as inputs. A file of
    theirs it reads by path is covered by their own traces: a change to it makes the dependency
    run again, and `flow` with it (`dependency_runs`)."""
    return sorted({p for dep in flow.completed_dependencies for p in artifact_files(dep)})


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


def _existing_files(paths: Sequence[Path], run_path: Path) -> list[Path]:
    """The files among `paths` that exist, resolved; a relative one is under `run_path`."""
    files = set()
    for path in paths:
        path = Path(path) if Path(path).is_absolute() else run_path / path
        if path.is_file():
            files.add(path.resolve())
    return sorted(files)


def registered_input_files(flow: Flow) -> list[Path]:
    """Every existing file `flow` has registered in `Flow.implicit_inputs` so far. Called before
    the run, these are the files its `init()` or `prepare_inputs()` resolved (an ABC script, expanded against the start
    directory or the environment): known before the run, they are expected inputs like a
    setting's files, so a launch that resolves another file finds a new input, not a match."""
    return _existing_files(flow.implicit_inputs, flow.run_path)


def installation_prefixes(programs: Sequence[str]) -> list[Path]:
    """The installation prefix of each host program among `programs`: the directory above the
    `bin/` its resolved executable lies in. A container image has none (its paths are not host
    paths), nor has a program outside a `bin/` directory or one found nowhere."""
    prefixes = []
    for name in programs:
        if name.startswith(DOCKER_IMAGE_PREFIX):
            continue
        where = locate_program(name)
        if where is None:
            continue
        parent = Path(where).parent
        if parent.name == "bin" and parent.parent != parent.parent.parent:
            prefixes.append(parent.parent)
    return prefixes


def implicit_input_files(flow: Flow, programs: Sequence[str] = ()) -> list[Path]:
    """Every existing file the flow read that is known only after its run: those a depfile its
    tools wrote (`yosys -E`, ...) names, except the tool's own installation's files (under the
    prefix of a program in `programs`: internal to the tool, and xeda stays agnostic to how
    tools are installed), and those it registered in `Flow.implicit_inputs` (including the ones
    its `init()` registered, which are expected inputs already). Where a file lies decides
    whether it is an input or the run's own (`build_trace`)."""
    prefixes = installation_prefixes(programs)
    named = [
        dep
        for depfile in flow.depfiles
        if depfile.is_file()
        for dep in parse_depfile(depfile, flow.run_path)
        if not any(_resolved(dep, flow.run_path).is_relative_to(prefix) for prefix in prefixes)
    ]
    return _existing_files([*named, *flow.implicit_inputs], flow.run_path)


def _resolved(path: Path, run_path: Path) -> Path:
    """`path` (relative ones under `run_path`) with symbolic links resolved, existing or not."""
    return (path if path.is_absolute() else run_path / path).resolve()


def _package_files(directory: Path) -> List[Path]:
    """The files that make up a Python package directory (`listing.directory_files`, resolved):
    everything under it but compiled bytecode (`__pycache__`, `.pyc`) and hidden files, which
    Python and editors create."""
    top = directory.resolve()
    return sorted(
        p
        for p in directory_files(top, skip=frozenset({"__pycache__"}))
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo")
        and not any(part.startswith(".") for part in p.relative_to(top).parts)
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
            files.update(p.resolve() for p in directory_files(templates) if p.is_file())
    if not files:
        return ""
    return _digest_files(sorted(files), Path("/"))


def candidate_inputs(
    flow: Flow, design: Design, input_settings: Flow.Settings, run_root: Optional[Path] = None
) -> List[Path]:
    """Every existing file the run would consume that can be named before it runs: the
    design's files, the files its settings name and every file under a directory they name
    (`setting_directory_files`), its dependencies' outputs, and the files the flow registered in
    `init()` or `prepare_inputs()` (`registered_input_files`) -- xeda's own bookkeeping files in its run directory
    excepted."""
    bookkeeping = bookkeeping_files(flow.run_path)
    files = {
        *design_files(design),
        *setting_files(input_settings),
        *setting_directory_files(input_settings, flow.run_path, run_root),
        *dependency_outputs(flow),
        *registered_input_files(flow),
        *declared_input_files(flow),
    }
    return sorted(files - bookkeeping)


def declared_input_files(flow: Flow) -> list[Path]:
    """The selected declared paths, in declaration and list order."""
    files = []
    for name in declared_inputs(type(flow)):
        value = getattr(flow.inputs, name)
        files.extend(value if isinstance(value, list) else ([] if value is None else [value]))
    return files


def expectation(
    flow: Flow, design: Design, input_settings: Flow.Settings, run_root: Path
) -> Expectation:
    """What `flow` would consume now. `design` and `input_settings` are the launcher's own
    (never modified by the flow), so the same inputs are found before and after the run;
    `run_root` is the launcher's run directory root, which dependency runs are named under.

    Includes `dependency_outputs(flow)` and `dependency_runs(flow)`, so the result is only
    stable once `flow`'s dependencies have completed (`flow.completed_dependencies` populated)
    -- call this after they have run, as the launcher does -- and the files `flow` registered
    (`registered_input_files`), so call it before `flow` runs, while those are what its `init()`
    registered, including those resolved by `prepare_inputs` after hand-over.
    """
    assert flow.flow_hash is not None and flow.design_hash is not None
    return Expectation(
        flow=flow.name,
        flowrun_hash=flow.flow_hash,
        design_hash=flow.design_hash,
        xeda_version=__version__,
        xeda_code=xeda_code_digest(),
        flow_code=flow_code_digest(type(flow)),
        inputs=tuple(candidate_inputs(flow, design, input_settings, run_root)),
        settings=as_recorded(input_settings),
        setting_locations=setting_locations(input_settings, flow.run_path),
        dependency_runs=dependency_runs(flow, run_root),
        dependency_flows={
            run_dir_key(dep.run_path, run_root): dep.name for dep in flow.completed_dependencies
        },
        declared_inputs=getattr(flow, "declared_input_records", ()),
    )


@dataclass(frozen=True)
class InputSnapshot:
    """The inputs of a run as they were just before it started (`snapshot_inputs`)."""

    #: when the snapshot was begun: the run's start, from which on a written file is the run's
    started_ns: int
    #: when the input records were taken, also read from the run directory's filesystem clock
    inputs_recorded_ns: int
    #: the inputs the run is expected to consume (the `inputs` its trace will record)
    expected: Tuple[Path, ...]
    #: the record of each expected input, and of each file the previous run's depfiles named,
    #: that existed when the run started
    records: Mapping[str, FileRecord]


def snapshot_inputs(
    expected: Expectation, previous: Optional[Trace], run_path: Path
) -> InputSnapshot:
    """Record every input the run is expected to consume, and every file the `previous` run's
    depfiles named (the likely ones this run's name again), just before the run starts -- each
    as itself (a symbolic link by its target and the content it points to), and by the
    `previous` run's record where the file's metadata vouches for it (`FileRecord.trusted`), as
    a check does: an unchanged library directory is not read again at every run."""
    started_ns = filesystem_time_ns(run_path)
    inputs = tuple(expected_inputs(expected.inputs, previous, run_path))
    known = list(inputs)
    prior: Dict[str, FileRecord] = {}
    trusted_before_ns: Optional[int] = None
    if previous is not None:
        known += [Path(p) for p in previous.implicit_inputs]
        prior = {**previous.implicit_inputs, **previous.inputs}
        trusted_before_ns = previous.inputs_recorded_ns
    records: Dict[str, FileRecord] = {}
    for path in known:
        before = prior.get(str(path))
        try:
            records[str(path)] = record_file(
                path,
                None if before is None or before.unknown else before,
                trusted_before_ns,
                follow_symlinks=False,
            )
        except OSError:
            pass  # absent: if it appears, the next check finds a new input
    inputs_recorded_ns = filesystem_time_ns(run_path)
    return InputSnapshot(
        started_ns=started_ns,
        inputs_recorded_ns=inputs_recorded_ns,
        expected=inputs,
        records=records,
    )


def _programs(names: Sequence[str]) -> Dict[str, Optional[ProgramRecord]]:
    """Each program as it is after the run, its file recorded (`record_file`, after the run's
    output clock was read); one whose file changed since it was started -- its identity or
    metadata differs from what `proc_utils.note_program` recorded then (`StartedPrograms.before`),
    never a clock -- is recorded unknown, which never matches. A container image by its ID
    alone."""
    before = getattr(names, "before", {})
    programs: Dict[str, Optional[ProgramRecord]] = {}
    for name in names:
        where = locate_program(name)
        if where is None or name.startswith(DOCKER_IMAGE_PREFIX):
            programs[name] = None if where is None else ProgramRecord(path=where)
            continue
        path = Path(where)
        prior = before.get(name)
        try:
            if prior is None or program_state(where) != prior:
                file = unknown_record(path)  # not there when started, or changed since
            else:
                file = record_file(path)
        except OSError:
            file = unknown_record(path)  # gone since it was found
        programs[name] = ProgramRecord(path=where, file=file)
    return programs


def _as_recorded(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """`path`'s metadata as `digest.record_file(..., follow_symlinks=False)` records it -- a
    symbolic link to a regular file by that file's, anything else by its own -- or None if it
    is gone."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.stat(path)
            except OSError:
                target = None
            if target is not None and stat.S_ISREG(target.st_mode):
                st = target
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def changed_since(path: Path, before: FileRecord) -> bool:
    """Whether `path` changed since `before` recorded it, just before the run: its size, mtime,
    inode change time or inode differ, or it is gone -- by its own metadata, never a clock, so a
    file on a file system whose clock differs from the run directory's is judged alike."""
    return _as_recorded(path) != (before.size, before.mtime_ns, before.ctime_ns, before.inode)


def run_reports(flow: Flow) -> List[str]:
    """The reports `flow`'s run read (`Flow.reports_read`) that lie inside its run directory,
    relative to it, in POSIX form, sorted: what the launcher removes before the next run
    executes (R50 j). One named through a link out of the directory is none of them."""
    run_directory = flow.run_directory
    found = set()
    for path in flow.reports_read:
        given = path if path.is_absolute() else flow.run_path / path
        if run_directory.holds(given):
            found.add(run_directory.inside(given).relative_to(run_directory.path).as_posix())
    return sorted(found)


def build_trace(
    expected: Expectation,
    flow: Flow,
    programs: Sequence[str],
    snapshot: InputSnapshot,
    input_settings: Flow.Settings,
) -> Trace:
    """The trace of `flow`'s just-completed, successful run, under a new `run_id`: its inputs
    as `snapshot` recorded them; its outputs (`output_files`), recorded now, after the file-system
    clock is read (`outputs_recorded_ns`); and each file its settings or depfiles name that the
    snapshot did not record: the run's own inside its run directory, an input anywhere
    else -- unknown if it was written during the run (see the module docstring)."""
    started_ns = snapshot.started_ns
    run_dir = flow.run_path.resolve()
    run_device = run_dir.stat().st_dev
    bookkeeping = bookkeeping_files(flow.run_path)
    # before any output is read: from then on, the records taken of them vouch for them
    outputs_recorded_ns = filesystem_time_ns(flow.run_path)

    def written(path: Path, before: Optional[FileRecord]) -> Optional[str]:
        """Why `path` is not what the run read, or None: written during the run. Against its
        record from before the run where there is one (`changed_since`: identity and metadata,
        never a clock). Without one, by the run directory's file-system clock only on that same
        file system; on another, whose clock may differ, nothing tells (`UNRECORDED_BEFORE_RUN`).
        A file that vanished since it was listed counts as written."""
        if before is not None:
            return MODIFIED_DURING_RUN if changed_since(path, before) else None
        try:
            st = path.stat()
        except OSError:
            return MODIFIED_DURING_RUN
        if st.st_dev != run_device:
            return UNRECORDED_BEFORE_RUN
        return MODIFIED_DURING_RUN if written_since(path, started_ns) else None

    def record(path: Path) -> FileRecord:
        """The record of `path` now, as the entry it is (a symbolic link by its target); unknown
        if it vanished since it was listed."""
        try:
            return record_file(path, follow_symlinks=False)
        except OSError:
            return unknown_record(path)

    output = record

    def own(path: Path) -> bool:
        """A file is the run's own by location: inside the run directory."""
        return path.is_relative_to(run_dir)

    outputs = {str(p): output(p) for p in output_files(flow)}
    inputs: Dict[str, FileRecord] = {}
    implicit: Dict[str, FileRecord] = {}
    named = set(setting_files(input_settings)) - bookkeeping
    implicit_paths = implicit_input_files(flow, programs)
    referenced = set(snapshot.expected) | named | set(implicit_paths)
    for path in referenced:
        if not own(path):
            outputs.pop(str(path), None)

    for path in snapshot.expected:
        key = str(path)
        before = snapshot.records.get(key)
        if before is None:
            continue  # absent when the run started: a new input next time, if it appears
        why = written(path, before)
        if why is not None:
            if own(path):
                if path in named:
                    outputs[key] = output(path)
                continue
            inputs[key] = unknown_record(path, why)
        else:
            inputs[key] = before
    for path in sorted(named - set(snapshot.expected)):
        key = str(path)
        if key in inputs or key in outputs:
            continue
        # A setting may name a file that was absent when the run started: no record from before
        # the run covers it.
        why = written(path, snapshot.records.get(key))
        if why is not None:
            if own(path):
                outputs[key] = output(path)
            else:
                inputs[key] = unknown_record(path, why)
        else:
            inputs[key] = record(path)
    for path in implicit_paths:
        key = str(path)
        if key in inputs or key in outputs or path in bookkeeping:
            continue
        before = snapshot.records.get(key)
        why = written(path, before)
        if why is not None:
            if own(path):
                outputs[key] = output(path)
                continue
            # What the run read is unknown outside its run directory.
            implicit[key] = unknown_record(path, why)
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
        programs=_programs(programs),
        dependency_runs=dict(expected.dependency_runs),
        setting_locations={key: list(value) for key, value in expected.setting_locations.items()},
        inputs_recorded_ns=snapshot.inputs_recorded_ns,
        outputs_recorded_ns=outputs_recorded_ns,
        inputs=inputs,
        implicit_inputs=implicit,
        outputs=outputs,
        reports=run_reports(flow),
        declared_inputs=list(expected.declared_inputs),
    )
