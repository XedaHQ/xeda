"""What a flow run consumed and produced: the files and code that its trace records.

The trace records every input that the launcher can name before a run, just before the run
starts (`snapshot_inputs`). These inputs are the files of the design, the files that its
settings name and every file under a directory that they name, the outputs of its producers,
the files that the flow registered in its `init()`, and the files that the depfiles of the
previous run named. The trace keeps that record, so a file edited while a long run is still
going no longer matches it, and the next launch runs again. Files known only after the run
(what a depfile names for the first time, what a flow reads on its own, the programs it
started) are recorded afterwards. One that was written while the run was going on is recorded
as unknown (`digest.unknown_record`), which never matches. A program keeps the record of the
previous trace while its metadata vouches for it, so the executable of a tool is read when it is
first recorded or changed, and not at every run.

Every run directory belongs to Xeda. A file inside the own directory of a run is the run's.
Anywhere else, a file that the settings, depfiles or design of the run name stays an input. It is
recorded as unknown if it changed during the run, so the next launch runs again.

Every file that the run left in its run directory is an output (`output_files`), declared as an
artifact or not, because a consumer may read any of them by path. A hand edit anywhere in the
directory, or a file added there, makes the run stale. Its consumers follow only if what it hands
them changed (`trace.changed_edge`). The outputs are hashed once, after the run. A check trusts
their metadata (`digest.FileRecord.trusted`) as it does for an input.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from pydantic import BaseModel

from ..artifacts import iter_artifact_paths
from ..design import DESIGN_PARTS, Design, FileResource, SourceType
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
from ..flow.derived import recorded_files
from ..records import name_in
from ..flow.io import declared_inputs
from ..dataclass import is_locator, written_role
from ..deliver import ReadInputs
from ..flow.flow import _annotation_contains_path, map_keyed_path_leaves
from ..listing import VCS_METADATA, directory_files
from ..proc_utils import DOCKER_IMAGE_PREFIX, program_state
from ..records import ErrorInfo
from ..version import __version__
from .code_digest import CodeDigests
from .run_lock import lock_file
from .trace import (
    RESERVED_FILES,
    DeclaredInputRecord,
    Expectation,
    OutputEntry,
    ProgramRecord,
    Trace,
    as_recorded,
    expected_inputs,
    locate_program,
    run_directory_files,
)

log = logging.getLogger(__name__)

#: The files that Xeda keeps itself in a run directory. They are never inputs; `results.json` is
#: an output.
BOOKKEEPING_FILES = ("settings.json", "results.json", *RESERVED_FILES)


def bookkeeping_files(run_path: Path) -> Set[Path]:
    """Return the files that Xeda itself keeps for the run in `run_path`, lock files included."""
    run_dir = run_path.resolve()
    return {run_dir / name for name in BOOKKEEPING_FILES} | {lock_file(run_path).resolve()}


def _add(found: List[Path], path: Path) -> None:
    resolved = path.resolve()
    if resolved not in found:
        found.append(resolved)


def design_files(
    design: Design,
    parts: Iterable[str] = DESIGN_PARTS,
    source_types: Optional[Collection[SourceType]] = None,
) -> List[Path]:
    """Return every file that the `parts` of the design name, resolved.

    `parts` are the parts of the design that the flow reads (`Flow.design_parts`: the RTL, and
    the testbench for a flow that reads it; the whole design by default). The files are the
    sources of these parts (only those of `source_types`, if given: `Flow.design_source_types`),
    a parameter or define given as a file (`parameters.ROM.file: rom.mem`, which the design holds
    as the absolute path of the file), and any other file that a field of the parts refers to.

    Only the trace of a flow scopes them. What protects the files of the user from a delivery
    (the registered reads and refused inputs of the launcher, the read inputs of a remote run)
    keeps the whole design. Otherwise the delivery of an `{"rtl"}` flow could land on a
    testbench file.

    One walker visits every value of both parts. A `FileResource` is its file. Any other text or
    path counts when it is absolute and names an existing file. Relative text (a top-level name,
    a relative parameter) is not resolved, since nothing says against what. Counting a value
    that merely looks like a file errs toward another run, never toward a stale reuse.
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

    wanted = frozenset(parts)
    unknown = wanted - DESIGN_PARTS
    if unknown:
        raise ValueError(f"{', '.join(sorted(unknown))} is not a part of a design")

    def visit_part(part: BaseModel) -> None:
        for name in type(part).model_fields:
            value = getattr(part, name)
            if name == "sources" and source_types is not None:
                value = [source for source in value if source.type in source_types]
            visit(value)

    visit_part(design.rtl)  # the RTL is always read
    if "tb" in wanted:
        visit_part(design.tb)
    return found


def setting_path_leaves(
    settings: Flow.Settings, *, written: Optional[bool] = None
) -> list[tuple[str, Any]]:
    """Return every path-typed leaf of `settings`, by its key path.

    Examples of a key path are `lib_paths[0][1]` and `platform.tech_lef`. Nested models are
    walked too (an ASIC `platform`, the settings of the ghdl plugin inside those of yosys). Each
    field is walked by `map_keyed_path_leaves`, the traversal that path expansion uses, so a
    container is read by its declared shape and not by value alone. In `lib_paths`, only the
    path half of each tuple is ever visited, never the library name. With `written`, only the
    leaves of fields that the flow writes (True: a role, `xeda.dataclass.written_role`) or reads
    (False) count. A locator (`LOCATOR`) is never listed or walked: the flow registers the files
    it opens under it.
    """
    leaves: list[tuple[str, Any]] = []

    def collect(key: str, leaf: Any) -> Any:
        if isinstance(leaf, (str, os.PathLike)) and os.fspath(leaf):
            leaves.append((key, leaf))
        return leaf

    def walk(model: BaseModel, prefix: str) -> None:
        for name, field in type(model).model_fields.items():
            if is_locator(type(model), name):
                continue
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
    """Return the directories under which a relative path in a setting is looked up.

    These are the design root and the start directory."""
    return [
        Path(root)
        for root in (settings.context.get("design_root"), settings.context.get("runner_cwd"))
        if root is not None
    ]


def _candidates(leaf: Any, roots: Sequence[Path]) -> list[Path]:
    path = Path(leaf)
    return [path] if path.is_absolute() else [root / path for root in roots]


def setting_candidates(settings: Flow.Settings) -> list[tuple[str, Path]]:
    """Return every place that a path-typed setting the flow reads can name, with its key path.

    The paths are as written and not resolved (`setting_path_leaves`). An absolute path is taken
    as itself. A relative path is looked up under the design root and under the start directory.
    This is the one reading of "a path a setting names": the trace records and the mounts of a
    container both start from it."""
    roots = _setting_roots(settings)
    return [
        (key, candidate)
        for key, leaf in setting_path_leaves(settings, written=False)
        for candidate in _candidates(leaf, roots)
    ]


def setting_file_keys(settings: Flow.Settings) -> dict[Path, set[str]]:
    """Return every existing file named by a path-typed setting that the flow reads, resolved.

    Each file comes with the key paths of the settings that name it (`setting_path_leaves`). A
    relative path is looked up under the design root and under the start directory, and each
    candidate that exists counts. A setting that names what the flow writes (a role,
    `xeda.dataclass.written_role`) names no input."""
    found: dict[Path, set[str]] = {}
    for key, candidate in setting_candidates(settings):
        if candidate.is_file():
            found.setdefault(candidate.resolve(), set()).add(key)
    return found


def setting_files(settings: Flow.Settings) -> List[Path]:
    """Return every existing file named by a path-typed setting that the flow reads, resolved.

    See `setting_file_keys`."""
    return list(setting_file_keys(settings))


#: A directory a setting names whose listing holds more files than this, or takes longer than
#: `SLOW_LISTING_S` to list, is reported: every file in it is an input, recorded before each run
#: (hashed when its metadata cannot vouch for it) and checked by its metadata at each launch.
#: There is no cap: a file past one would go unnoticed.
LARGE_LISTING_FILES = 10_000
SLOW_LISTING_S = 2.0


def setting_directories(settings: Flow.Settings, run_path: Path) -> list[tuple[str, Path]]:
    """Return every existing directory that a path-typed setting the flow reads names, resolved.

    Each directory comes with the key path of the first setting that names it
    (`setting_path_leaves`). An absolute path is taken as itself, and a relative one is looked
    up under the design root and under the start directory (as `setting_files`). The run
    directory and every directory in it are left out, because their files are outputs of the
    run already. A directory that the flow writes (`reports_dir`, `sim_dir`: a role,
    `xeda.dataclass.written_role`) is no input."""
    run_dir = run_path.resolve()
    found: dict[Path, str] = {}
    for key, candidate in setting_candidates(settings):
        if candidate.is_dir():
            resolved = candidate.resolve()
            if resolved.is_relative_to(run_dir):
                continue
            found.setdefault(resolved, key)
    return [(key, directory) for directory, key in found.items()]


def setting_read_paths(settings: Flow.Settings, run_path: Path) -> list[Path]:
    """Return the existing files and directories that the path-typed settings the flow reads name.

    The paths are as written (`setting_candidates`), which is how the tool receives them. A
    directory in the run directory is left out, as in `setting_directories`."""
    run_dir = run_path.resolve()
    return [
        candidate
        for _key, candidate in setting_candidates(settings)
        if candidate.is_file()
        or (candidate.is_dir() and not candidate.resolve().is_relative_to(run_dir))
    ]


def setting_directory_listings(
    settings: Flow.Settings, run_path: Path, run_root: Path | None = None
) -> list[tuple[str, Path, list[Path]]]:
    """Return each directory that a setting names (`setting_directories`), with its entries.

    Each item holds the key path of the setting that names the directory, the directory, and
    every entry under it. The entries are the recursive listing (`listing.directory_files`): a
    symbolic link as itself, a link to a directory followed too (its entries by their path
    through it), and a subdirectory and a special file as entries. The run directory and the
    run root `run_root` are not entered where a named directory holds them, however it reaches
    them, because their files are the own files of the runs. Nor is the metadata of version
    control (`VCS_METADATA`), which no tool reads. A large or slow listing is reported, never
    capped. This is the one listing of what a flow reads through a directory. The trace records
    it (`setting_directory_files`), and the delivery guard never delivers into it
    (`register_read_settings`)."""
    prune = [run_path] + ([run_root] if run_root is not None else [])
    listings: list[tuple[str, Path, list[Path]]] = []
    for key, directory in setting_directories(settings, run_path):
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
    """Return every entry under each directory that a setting names (`setting_directory_listings`).

    A file edited, added or removed there makes the run stale: a library recompiled in place,
    an empty directory added, an edit beneath a linked directory."""
    return [
        path
        for _key, _directory, listed in setting_directory_listings(settings, run_path, run_root)
        for path in listed
    ]


def register_read_settings(
    inputs: ReadInputs, settings: Flow.Settings, run_path: Path, run_root: Path
) -> None:
    """Register with the delivery guard of a launch everything that the read settings make an input.

    The guard is `xeda.deliver.ReadInputs`. The registration follows the trace: every file that
    a setting names (`setting_files`), and every directory that a setting names, with each
    entry under it (`setting_directory_listings`, the listing of the trace). So no delivery
    lands on a file that the launch read, nor anywhere in a directory that it reads."""
    inputs.add(setting_files(settings))
    for key, directory, listed in setting_directory_listings(settings, run_path, run_root):
        inputs.add_directory(key, directory, listed)


def setting_locations(settings: Flow.Settings, run_path: Path) -> dict[str, list[str]]:
    """Return where each path-typed setting that the flow reads points, by key path.

    An example of a key path is `lib_paths[0][1]`. A setting that names what the flow writes is
    bound by no location.

    The identity of a run is location-free: `$PWD/libs` and `$DESIGN_ROOT/libs` count as
    written, so a launch from another start directory, or of another design tree with the same
    text, has the same identity. A file that a setting names is bound by its record, as an
    input. A directory (a compiled library, an include directory) or a path that does not exist
    is bound by nothing else. So the trace records where each setting points, and a launch
    where one points elsewhere is stale.

    An absolute path (a `$PWD` or `$DESIGN_ROOT` path is absolute once expanded) is its own
    location, resolved, whatever it names (a file, a directory, nothing yet), so that a run
    that creates it changes nothing. A relative path points at each existing path it is looked
    up as, under the design root and under the start directory (`setting_files`). The exception
    is a path inside the run directory `run_path`, whose place belongs to the trace itself. A
    relative path with no such path has no location."""
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
    """Return every file among the artifacts of the flow, and its `results.json`, resolved.

    A directory stands for the regular files under it (`listing.directory_files`: a link in it
    is not followed out)."""
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
    """Return the files that the trace of the run of `flow` records as its own.

    These are every entry in its run directory (`run_directory_files`), since a consumer may
    read any of them, and the artifacts outside it."""
    run_dir = flow.run_path.resolve()
    outside = [path for path in artifact_files(flow) if not path.is_relative_to(run_dir)]
    return sorted({*run_directory_files(flow.run_path), *outside})


def _output_equivalences(flow: Flow) -> Dict[str, str]:
    """Return the equivalence digest of each recorded output file that has one, by its name in
    the run directory (`Trace.output_equivalences`)."""
    found: Dict[str, str] = {}
    for recorded in (flow.results.get("outputs") or {}).values():
        for entry, key in recorded_files(recorded):
            if key == "path" and entry.get("equivalence"):
                name = name_in(flow.run_path, entry["path"])
                if name is not None:
                    found[name] = entry["equivalence"]
    return found


def run_dir_key(run_path: Path, run_root: Path) -> str:
    """Return how a trace names a run directory.

    The name is relative to the run root (`xeda_run`), in POSIX form, or absolute if the
    directory lies outside the run root."""
    path, root = Path(os.path.abspath(run_path)), Path(os.path.abspath(run_root))
    return (path.relative_to(root) if path.is_relative_to(root) else path).as_posix()


def dependency_runs(producers: Sequence[Flow], run_root: Path) -> Dict[str, str]:
    """Return the run of each of the `producers` that a flow consumed, by its run directory.

    The run is `Flow.run_id`, which the launcher sets on every flow it completes."""
    runs: Dict[str, str] = {}
    for producer in producers:
        assert producer.run_id is not None, f"{producer.name} was not completed by a launcher"
        runs[run_dir_key(producer.run_path, run_root)] = producer.run_id
    return runs


_DEP_SEPARATOR = re.compile(r"(?<!\\)\s+")


def parse_depfile(path: Path, base: Path) -> List[Path]:
    """Return the prerequisites that a Makefile-style depfile lists.

    A relative prerequisite is resolved under `base`."""
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
    """Return the files among `paths` that exist, resolved. A relative path is under `run_path`."""
    files = set()
    for path in paths:
        path = Path(path) if Path(path).is_absolute() else run_path / path
        if path.is_file():
            files.add(path.resolve())
    return sorted(files)


def registered_input_files(flow: Flow) -> list[Path]:
    """Return every existing file that `flow` has registered in `Flow.implicit_inputs` so far.

    Called before the run, these are the files that its `init()` or `prepare_inputs()` resolved
    (an ABC script, expanded against the start directory or the environment). They are known
    before the run, so they are expected inputs like the files of a setting, and a launch that
    resolves another file finds a new input, not a match."""
    return _existing_files(flow.implicit_inputs, flow.run_path)


def located_input_files(flow: Flow) -> Optional[list[Path]]:
    """Return the existing files that `flow.locate_prepared_inputs()` names.

    These are the files that its preparation registers.

    The files are resolved, in the form that `registered_input_files` gives them. Return None if
    some file is not made yet."""
    located = flow.locate_prepared_inputs()
    return None if located is None else _existing_files(located, flow.run_path)


def installation_prefixes(programs: Sequence[str]) -> list[Path]:
    """Return the installation prefix of each host program among `programs`.

    The prefix is the directory above the `bin/` that the resolved executable lies in. A
    container image has none (its paths are not host paths), and neither has a program outside
    a `bin/` directory or one found nowhere."""
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
    """Return every existing file that the flow read and that is known only after its run.

    These are the files that a depfile written by its tools (`yosys -E`, ...) names, except the
    files of the installation of the tool (under the prefix of a program in `programs`:
    internal to the tool, and Xeda stays agnostic to how tools are installed). They also
    include the files that the flow registered in `Flow.implicit_inputs` (including those that
    its `init()` registered, which are expected inputs already). Where a file lies decides
    whether it is an input or the run's own (`build_trace`). A file that a producer handed over
    is neither: the edge records it."""
    prefixes = installation_prefixes(programs)
    named = [
        dep
        for depfile in flow.depfiles
        if depfile.is_file()
        for dep in parse_depfile(depfile, flow.run_path)
        if not any(_resolved(dep, flow.run_path).is_relative_to(prefix) for prefix in prefixes)
    ]
    # A file handed over by a producer is no file record of this flow, however it came to be read.
    handed = {Path(path).resolve() for path in handed_paths(flow.declared_input_records)}
    return [
        path
        for path in _existing_files([*named, *flow.implicit_inputs], flow.run_path)
        if path not in handed
    ]


def _resolved(path: Path, run_path: Path) -> Path:
    """Return `path` with symbolic links resolved, existing or not.

    A relative path is under `run_path`."""
    return (path if path.is_absolute() else run_path / path).resolve()


def candidate_inputs(
    flow_class: type[Flow],
    run_path: Path,
    design: Design,
    input_settings: Flow.Settings,
    registered: Sequence[Path],
    records: Sequence[DeclaredInputRecord],
    run_root: Optional[Path] = None,
) -> List[Path]:
    """Return every existing file that the run would consume and that can be named before it runs.

    These are the files of the design, the files that its settings name and every file under a
    directory that they name (`setting_directory_files`), the design sources that its declared
    inputs take (`source_input_files` of the `records`), and the files that the flow registered
    in `init()` or `prepare_inputs()` (`registered`, as `registered_input_files` finds them).
    The bookkeeping files of Xeda in its run directory are excepted. What its producers handed
    over is no file of its own: the edge records it (`BoundProducer`)."""
    bookkeeping = bookkeeping_files(run_path)
    handed = {Path(path).resolve() for path in handed_paths(records)}
    files = {
        # The parts of the design this flow reads, not the whole design.
        *design_files(design, flow_class.design_parts, flow_class.design_scope(input_settings)),
        *setting_files(input_settings),
        *setting_directory_files(input_settings, run_path, run_root),
        *_existing_files(registered, run_path),
        *source_input_files(records),
    }
    return sorted(files - bookkeeping - handed)


def declared_input_files(flow: Flow) -> list[Path]:
    """Return the selected declared paths, in declaration and list order."""
    files = []
    for name in declared_inputs(type(flow)):
        value = getattr(flow.inputs, name)
        files.extend(value if isinstance(value, list) else ([] if value is None else [value]))
    return files


def handed_paths(records: Sequence[DeclaredInputRecord]) -> set[str]:
    """Return the paths that the producers handed over, as the records name them."""
    return {entry.path for record in records for ref in record.references for entry in ref.entries}


def source_input_files(records: Sequence[DeclaredInputRecord]) -> list[Path]:
    """Return the files of the declared inputs that come from the sources of the design.

    These are the paths that the `records` list, without those that a producer handed over
    (`handed_paths`). A file handed over is never a file record of the consumer."""
    handed = handed_paths(records)
    return [Path(path) for record in records for path in record.paths if path not in handed]


def expected_run(
    *,
    flow_class: type[Flow],
    name: str,
    flowrun_hash: str,
    settings_hash: str,
    design_hash: str,
    run_path: Path,
    design: Design,
    input_settings: Flow.Settings,
    run_root: Path,
    registered: Sequence[Path],
    declared: Sequence[DeclaredInputRecord],
    dependency_run_ids: Mapping[str, str],
    digests: CodeDigests,
) -> Expectation:
    """Return what a run of a flow would consume now, from explicit data.

    The launch (`expectation`) and the dry run (`plan_freshness`) both judge by this one
    function, so they cannot drift. `input_settings` are those of the launcher (never modified
    by the flow). `registered` are the files that the flow registers in `init()` and
    `prepare_inputs()` (`Flow.locate_prepared_inputs` says where they are without making them).
    `declared` are its input records with what the producers handed over, and
    `dependency_run_ids` are the runs consumed, as provenance. `digests` holds the code digests
    of the launch (`code_digest.CodeDigests`), which a launch keeps for its length and no longer."""
    return Expectation(
        flow=name,
        flowrun_hash=flowrun_hash,
        design_hash=design_hash,
        xeda_version=__version__,
        xeda_code=digests.xeda(),
        flow_code=digests.flow(flow_class),
        inputs=tuple(
            candidate_inputs(
                flow_class, run_path, design, input_settings, registered, declared, run_root
            )
        ),
        settings=as_recorded(input_settings),
        setting_locations=setting_locations(input_settings, run_path),
        dependency_runs=dict(dependency_run_ids),
        declared_inputs=tuple(declared),
        settings_hash=settings_hash,
    )


def expectation(
    flow: Flow,
    design: Design,
    input_settings: Flow.Settings,
    run_root: Path,
    producers: Sequence[Flow],
    digests: CodeDigests,
) -> Expectation:
    """Return what `flow` would consume now.

    `design` and `input_settings` are those of the launcher (never modified by the flow), so
    the same inputs are found before and after the run. `run_root` is the root of the run
    directories of the launcher, under which the runs of producers are named. `producers` are
    the completed producers from which the launcher handed the inputs of the flow.

    The result includes `dependency_runs(producers, run_root)` and, in the declared input
    records of the flow, what the producers handed over. So call this after the producers have
    run, as the launcher does. It also includes the files that `flow` registered
    (`registered_input_files`). So call it before `flow` runs, while those are what its
    `init()` registered, including those resolved by `prepare_inputs` after hand-over.
    """
    assert flow.flow_hash is not None and flow.design_hash is not None
    return expected_run(
        flow_class=type(flow),
        name=flow.name,
        flowrun_hash=flow.flow_hash,
        settings_hash=flow.settings_hash or "",
        design_hash=flow.design_hash,
        run_path=flow.run_path,
        design=design,
        input_settings=input_settings,
        run_root=run_root,
        registered=flow.implicit_inputs,
        declared=getattr(flow, "declared_input_records", ()),
        dependency_run_ids=dependency_runs(producers, run_root),
        digests=digests,
    )


@dataclass(frozen=True)
class InputSnapshot:
    """The inputs of a run as they were just before it started (`snapshot_inputs`)."""

    #: When the snapshot began: the start of the run, from which on a written file is the run's.
    started_ns: int
    #: When the input records were taken, also read from the file-system clock of the run
    #: directory.
    inputs_recorded_ns: int
    #: The inputs the run is expected to consume (the `inputs` that its trace will record).
    expected: Tuple[Path, ...]
    #: The record of each expected input, and of each file that the depfiles of the previous
    #: run named, that existed when the run started.
    records: Mapping[str, FileRecord]
    #: The record of each file that the producers handed over, taken as the run starts (None if
    #: it could not be read), with the identity of every link on the way to it (`_name_state`,
    #: which tells that a link was replaced and put back). A file that is not the one the
    #: producer recorded, or that changed while the flow ran, is unknown (`_consumed`).
    handed: Mapping[str, Optional[Tuple[FileRecord, Tuple[Tuple[int, int, int], ...]]]] = (
        dataclass_field(default_factory=dict)
    )


def _name_state(path: Path) -> Tuple[Tuple[int, int, int], ...]:
    """Return the identity of `path` as a name, and of every link it leads through to the file.

    A link is not followed here. For each, the identity is its inode, change time and mtime. A
    link replaced by another and put back leads to the same file, with the same metadata; this
    identity tells it."""
    states = []
    current = path
    for _ in range(40):  # the depth of links the system follows
        st = os.lstat(current)
        states.append((st.st_ino, st.st_ctime_ns, st.st_mtime_ns))
        if not stat.S_ISLNK(st.st_mode):
            break
        current = Path(os.path.normpath(current.parent / os.readlink(current)))
    return tuple(states)


def _handed_record(path: Path) -> Optional[Tuple[FileRecord, Tuple[Tuple[int, int, int], ...]]]:
    """Return the record of a handed file (its content, through a link) and of its name.

    Return None if the file is unreadable."""
    try:
        return FileRecord.of(path.stat(), content_digest(path)), _name_state(path)
    except OSError:
        return None


def snapshot_inputs(
    expected: Expectation, previous: Optional[Trace], run_path: Path
) -> InputSnapshot:
    """Record every input that the run is expected to consume, just before the run starts.

    Also record every file that the depfiles of the `previous` run named (the likely ones for
    this run to name again). Each is recorded as itself (a symbolic link by its target and the
    content it points to), and by the record of the `previous` run where the metadata of the
    file vouches for it (`FileRecord.trusted`), as a check does. So an unchanged library
    directory is not read again at every run."""
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
        handed={
            path: _handed_record(Path(path))
            for path in sorted(handed_paths(expected.declared_inputs))
        },
    )


def _programs(
    names: Sequence[str], previous: Optional[Trace] = None
) -> Dict[str, Optional[ProgramRecord]]:
    """Return each program as it is after the run, with its file recorded.

    The file is recorded by `record_file`, after the output clock of the run was read. A
    program whose file changed since it was started is recorded as unknown, which never
    matches. Its identity or metadata then differs from what `proc_utils.note_program` recorded
    (`StartedPrograms.before`); no clock is involved. A container image is recorded by its ID
    alone.

    The record of the same file in the `previous` trace is kept while its metadata vouches for
    it (`FileRecord.trusted`, against the time when that trace took its records, which is the
    time a check trusts it by too), as `snapshot_inputs` keeps the record of an input. So a
    program such as yosys or nextpnr is read when it is first recorded or changed, and not at
    every completed run. A record that stands for a file that changed during a run
    (`unknown`) is never kept."""
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
                file = record_file(
                    path,
                    _kept_record(previous, name, where),
                    None if previous is None else previous.outputs_recorded_ns,
                )
        except OSError:
            file = unknown_record(path)  # gone since it was found
        programs[name] = ProgramRecord(path=where, file=file)
    return programs


def _kept_record(previous: Optional[Trace], name: str, where: str) -> Optional[FileRecord]:
    """Return the record of the program `name` found at `where` in the `previous` trace.

    A new record may be taken over from it. Return None if there is none, if the program was
    found elsewhere, or if the record is of a file that changed during its run."""
    recorded = None if previous is None else previous.programs.get(name)
    if recorded is None or recorded.path != where or recorded.file is None:
        return None
    return None if recorded.file.unknown else recorded.file


def _as_recorded(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """Return the metadata of `path` as `digest.record_file(..., follow_symlinks=False)` records it.

    A symbolic link to a regular file counts by the metadata of that file, and anything else by
    its own. Return None if the path is gone."""
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
    """Return whether `path` changed since `before` recorded it, just before the run.

    It changed if its size, mtime, inode change time or inode differ, or if it is gone. The test
    uses its own metadata and never a clock, so a file on a file system whose clock differs from
    that of the run directory is judged alike."""
    return _as_recorded(path) != (before.size, before.mtime_ns, before.ctime_ns, before.inode)


def run_reports(flow: Flow) -> List[str]:
    """Return the reports read by the run of `flow` that lie inside its run directory.

    The reports come from `Flow.reports_read`. They are relative to the run directory, in POSIX
    form, sorted. The launcher removes them before the next run executes. A report named
    through a link out of the directory is none of them."""
    run_directory = flow.run_directory
    found = set()
    for path in flow.reports_read:
        given = path if path.is_absolute() else flow.run_path / path
        if run_directory.holds(given):
            found.add(run_directory.inside(given).relative_to(run_directory.path).as_posix())
    return sorted(found)


def _consumed(record: DeclaredInputRecord, snapshot: InputSnapshot) -> DeclaredInputRecord:
    """Return `record` as the run consumed it.

    An entry is marked unknown, and never matches, if its file was not the bytes that the
    producer recorded when the run started, or if it may have changed since. That is the case
    when its metadata differs (written while the flow ran, even if put back as it was), or when
    the metadata is the same but too recent to vouch for the file (`FileRecord.trusted`, as for
    every input). Then its content is read again."""
    references = tuple(
        ref.model_copy(update={"entries": tuple(_entry_consumed(e, snapshot) for e in ref.entries)})
        for ref in record.references
    )
    return record.model_copy(update={"references": references})


def _entry_consumed(entry: OutputEntry, snapshot: InputSnapshot) -> OutputEntry:
    began = snapshot.handed.get(entry.path)
    unknown = entry.model_copy(update={"unknown": True})
    if began is None or began[0].sha != entry.sha:
        return unknown
    started, name = began
    path = Path(entry.path)
    try:
        if _name_state(path) != name:
            return unknown  # a link replaced while the flow ran, even if put back
        now = path.stat()
        if (now.st_size, now.st_mtime_ns, now.st_ctime_ns, now.st_ino) != (
            started.size,
            started.mtime_ns,
            started.ctime_ns,
            started.inode,
        ):
            return unknown  # written while the flow ran, whatever it holds now
        if started.trusted(now, snapshot.inputs_recorded_ns):
            return entry
        return entry if content_digest(path) == entry.sha else unknown
    except OSError:
        return unknown


def build_trace(
    expected: Expectation,
    flow: Flow,
    programs: Sequence[str],
    snapshot: InputSnapshot,
    input_settings: Flow.Settings,
    previous: Optional[Trace] = None,
    *,
    run_id: str,
) -> Trace:
    """Return the trace of the run that `flow` just completed, under its `run_id`.

    The run passed, or its checks rejected it (`Flow.outcome`); the trace records which, and for
    a rejection the error of the results (`Trace.verdict`, `Trace.error`).

    Its inputs are those that `snapshot` recorded. Its outputs (`output_files`) are recorded
    now, after the file-system clock is read (`outputs_recorded_ns`). Each file that its
    settings or depfiles name and that the snapshot did not record is the run's own inside the
    run directory, and an input anywhere else (unknown if it was written during the run; see
    the module docstring). The programs keep the records of the `previous` trace where their
    files are unchanged (`_programs`)."""
    started_ns = snapshot.started_ns
    run_dir = flow.run_path.resolve()
    run_device = run_dir.stat().st_dev
    bookkeeping = bookkeeping_files(flow.run_path)
    # Before any output is read: from then on, the records taken of them vouch for them.
    outputs_recorded_ns = filesystem_time_ns(flow.run_path)

    def written(path: Path, before: Optional[FileRecord]) -> Optional[str]:
        """Return why `path` is not what the run read, or None.

        The reason is that the file was written during the run.

        Where there is a record from before the run, the file is judged against it
        (`changed_since`: identity and metadata, never a clock). Without one, the file-system
        clock of the run directory decides, and only on that same file system. On another file
        system, whose clock may differ, nothing tells (`UNRECORDED_BEFORE_RUN`). A file that
        vanished since it was listed counts as written."""
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
        """Return the record of `path` now, as the entry it is (a symbolic link by its target).

        The record is unknown if the file vanished since it was listed."""
        try:
            return record_file(path, follow_symlinks=False)
        except OSError:
            return unknown_record(path)

    output = record

    def own(path: Path) -> bool:
        """Return whether a file is the run's own by location: inside the run directory."""
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
    rejected = flow.outcome == "rejected"
    return Trace(
        flow=expected.flow,
        run_id=run_id,
        flowrun_hash=expected.flowrun_hash,
        settings_hash=expected.settings_hash,
        design_hash=expected.design_hash,
        xeda_version=expected.xeda_version,
        xeda_code=expected.xeda_code,
        flow_code=expected.flow_code,
        programs=_programs(programs, previous),
        dependency_runs=dict(expected.dependency_runs),
        setting_locations={key: list(value) for key, value in expected.setting_locations.items()},
        inputs_recorded_ns=snapshot.inputs_recorded_ns,
        outputs_recorded_ns=outputs_recorded_ns,
        inputs=inputs,
        implicit_inputs=implicit,
        outputs=outputs,
        reports=run_reports(flow),
        declared_inputs=[_consumed(record, snapshot) for record in expected.declared_inputs],
        output_equivalences=_output_equivalences(flow),
        verdict="rejected" if rejected else "passed",
        error=ErrorInfo.from_json(flow.results["error"]).as_json_value() if rejected else None,
    )
