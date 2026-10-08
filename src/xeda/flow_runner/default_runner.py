"""Launch execution of flows"""

from __future__ import annotations

import difflib
import importlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from pprint import PrettyPrinter
from typing import Any, ClassVar, Dict, List, NamedTuple, Optional, Tuple, Type, TypeVar, Union

import yaml
from box import Box
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..artifacts import drop_unwritten_artifacts, iter_artifact_paths
from ..console import console
from ..dataclass import DELIVERABLE_ROLE, WORKING_ROLE, XedaBaseModel, model_validator, written_role
from ..deliver import (
    OUTPUTS_TO,
    Conflict,
    ConfirmedReplacements,
    DeliveredFiles,
    Deliveries,
    Delivery,
    DeliveryError,
    ReadInputs,
    deliverable_setting_names,
    outputs_to_deliveries,
    recorded_artifacts,
    refuse_shared_destinations,
    split_deliveries,
)
from ..design import (
    DESIGN_NAME,
    DESIGN_PARTS,
    Design,
    loading_in_run_root,
    names_a_design_file,
    refusing_load_side_effects,
    target_name_problem,
)
from ..flow import (
    Flow,
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
    registered_flows,
)
from ..flow.io import declared_inputs, declared_outputs, selected_types
from ..flow import flowrun_hash as flow_run_hash
from ..flow.flow import WrittenLeaf, map_written_leaves
from ..proc_utils import ProcessTimeout, recording_programs
from ..run_dir import (
    DIR_NAME_HASH_LEN,
    OutputState,
    RunDirectory,
    RunDirectoryError,
    record_output_state,
    run_directory_name,
    run_directory_problem,
)
from ..run_root import DEFAULT_RUN_ROOT, ensure_run_root
from ..tool import NonZeroExitCode
from ..utils import (
    LOCATION_FORMS,
    WorkingDirectory,
    XedaException,
    backup_existing,
    dump_json,
    json_encodable,
    settings_to_dict,
    snakecase_to_camelcase,
    unique,
    with_json_keys,
)
from ..version import __version__
from ..xedaproject import PROJECT_FILE_NAMES, ProjectFileError, XedaProject, resolve_project_file
from .bindings import (
    LOCAL_REQUESTS_ONLY,
    BindingLayer,
    NodeKey,
    default_nodes,
    effective_bindings,
    input_origins,
    node_identity,
    require_no_bindings,
    split_bindings,
)
from .chains import ChainElement, FlowRequest, parse_request
from .outputs import declared_output_files, handed_over, record_outputs
from .resolver import Plan, PlanNode, check_launchable, resolve as resolve_plan
from .run_lock import CompletedRun, run_dir_lock, run_dir_read_lock
from .settings_layers import (
    API_ORIGIN,
    COMMAND_LINE_ORIGIN,
    SUPPLIED_SECTIONS_ORIGIN,
    FlowNotFoundError,
    check_not_removed,
    command_line_sections,
    compose_flow_settings,
    merge_flow_sections,
    settings_in_context,
    split_flow_sections,
    suggest_dependency_node,
    transitive_dependencies,
)
from .trace import (
    as_recorded,
    BoundProducer,
    DeclaredInputRecord,
    check_trace,
    locate_program,
    previous_trace,
    remove_trace,
    write_trace,
)
from .trace_inputs import (
    artifact_files,
    build_trace,
    design_files,
    declared_input_files,
    expectation,
    register_read_settings,
    registered_input_files,
    setting_files,
    snapshot_inputs,
)

__all__ = [
    "DIR_NAME_HASH_LEN",
    "DefaultRunner",
    "DesignNotFoundError",
    "FlowNotFoundError",
    "FlowRunner",
    "ProjectFileError",
    "add_file_logger",
    "get_flow_class",
    "print_results",
]

log = logging.getLogger(__name__)


def print_results(
    flow: Optional[Flow] = None,
    results: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    subset: Optional[Iterable[str]] = None,
    skip_if_false: Union[bool, Iterable[str], None] = None,
) -> None:
    """Display selected flow results in a formatted table."""
    if results is None and flow:
        results = flow.results
    assert results is not None, "results is None"
    # The table shows what `results.json` holds: keys as that file writes them, so a key it can
    # write (a tuple, a `Path`) cannot crash the table printed after the run.
    results = with_json_keys(dict(results))
    console.print()
    table = Table(
        title=title,
        title_style=Style(frame=True, bold=True),
        show_header=False,
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column(style="bold", min_width=8, no_wrap=True)
    table.add_column(justify="right", min_width=8)
    skip_fields = [
        "timestamp",
        "design",
        "design_hash",
        "flow",
        "flow_hash",
        "settings_hash",
        "tools",
        "run_path",
        "artifacts",
        "outputs",
    ]
    for k, v in results.items():
        skipable = skip_if_false and (isinstance(skip_if_false, bool) or k in skip_if_false)
        if skipable and not v:
            continue
        if v is not None and not str(k).startswith("_"):
            if k == "success":
                table.add_row("Status", "[green]OK[/green]" if v else "[red]FAILED[/red]")
                continue
            if (subset and k not in subset) or k in skip_fields:
                continue
            if k == "runtime" and isinstance(v, (float, int)):
                table.add_row(
                    "Run time",
                    str(timedelta(seconds=round(v))),
                    style=Style(dim=True),
                )
                continue
            if isinstance(v, (dict,)):
                table.add_row(f"{k}:", "", style=Style(bold=True))
                for xk, xv in v.items():
                    if isinstance(xv, dict):
                        xv = json.dumps(xv, indent=1, default=json_encodable)
                    else:
                        xv = str(xv)
                    table.add_row(Text(f" {xk}"), str(xv))
                continue
            if isinstance(v, float):
                v = f"{v:,.3f}"
            elif isinstance(v, int):
                v = f"{v:,}"
            table.add_row(str(k), str(v))
    console.print(table)


class DesignNotFoundError(XedaException):
    """The design to run cannot be determined: none was given and there is no project to take
    one from, the project has no designs, or it has none of the given name."""


#: Launcher settings that no longer exist, with what replaced them. Giving one is an error that
#: says so, in the words `Flow.Settings.removed_settings` uses for flow settings.
_REMOVED_LAUNCHER_SETTINGS = {
    "xeda_run_dir": "run_root",
    "cached_dependencies": "the default, which reuses unchanged runs, and hashed_run_dirs=True to "
    "keep settings variants side by side",
    "skip_if_previous_run_exists": "the default, which reuses unchanged runs",
    "incremental": "clean=True to empty run directories before running (they are otherwise "
    "always reused)",
    "cleanup_before_run": "clean=True",
    "run_path": "run_root to choose where runs go (a flow always runs in a directory xeda "
    "creates under it), and outputs_to to receive its outputs elsewhere",
}


@dataclass(frozen=True)
class RunDirPolicy:
    """What one launch does with its run directory (see `FlowLauncher._run_dir_policy`).

    Decided per launch, never by writing to the launcher's settings: every launch of a launcher
    shares those, each of its dependency launches included.
    """

    clean: bool
    scrub_old_runs: bool
    post_cleanup: bool
    post_cleanup_purge: bool

    @property
    def purges(self) -> bool:
        """Whether the clean-up after the launch deletes the run directory (`_clean_up`)."""
        return self.post_cleanup and self.post_cleanup_purge


@dataclass(frozen=True)
class _Request:
    """Captured request layers. Workers transport these contributions, not model defaults."""

    flow_class: type[Flow]
    design: Design
    settings: dict[str, Any]
    sections: dict[str, Any]
    origins: tuple[tuple[str, Mapping[str, Any]], ...]
    command_line: dict[str, Any]
    api_overrides: dict[str, Any]
    flow_request: FlowRequest
    binding_layers: tuple[BindingLayer, ...]


def _flow_name_suggestions(flow_name: str, limit: int = 3) -> List[str]:
    """Canonical names of registered flows most similar to `flow_name`."""
    canonical = unique([cls.name for _mod, cls in registered_flows.values()])
    return difflib.get_close_matches(flow_name, canonical, n=limit, cutoff=0.6)


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__ or "xeda"
) -> Type[Flow]:
    flow_name = flow_name.strip().replace("-", "_")
    # before any lookup or import: a removed name is neither a flow nor a near miss of one
    check_not_removed(flow_name)
    _mod, flow_class = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        # canonical names, class names and aliases are all registered, but the user (or an agent)
        # may have used a different casing.
        lowered = flow_name.lower()
        for name, (_m, cls) in registered_flows.items():
            if name.lower() == lowered:
                return cls
    if flow_class is None:
        log.debug(
            "Flow %s was not found in registered flows. Trying to load using `importlib`.",
            flow_name,
        )
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise FlowNotFoundError(flow_name, _flow_name_suggestions(flow_name)) from e
        flow_class_name = snakecase_to_camelcase(flow_name)
        if module:
            try:
                flow_class = getattr(module, flow_class_name)
            except AttributeError:
                pass
        if not flow_class or not issubclass(flow_class, Flow):
            raise FlowNotFoundError(flow_name, _flow_name_suggestions(flow_name))
    return flow_class


def _get_flow_class_if_known(flow_name: str) -> Type[Flow] | None:
    """Resolve a flow-section key without rejecting sections for unavailable plugin flows."""
    try:
        return get_flow_class(flow_name)
    except FlowNotFoundError:
        return None


#: What a launch writes in every run directory it enters (the trace only after a success): a
#: directory holding one is a run directory, whatever it is called, and never a target's.
LAUNCH_DOCUMENTS = ("settings.json", "results.json", "trace.json")

#: The run records: what a launch writes when its run ends, as against `settings.json`, which it
#: writes when the run starts. A launch that finds its run fresh may write `trace.json` again, a
#: refresh, once its records have settled. A directory where these have changed was written to by a
#: launch since they were seen.
COMPLETION_DOCUMENTS = ("results.json", "trace.json")


class ScrubResult(NamedTuple):
    """What `scrub_design` did: the directories it searched, the run directories it removed
    (none if it found none or the removal was not confirmed), the ones it kept because their run
    records changed after the listing, the ones that were gone already when their turn came, and
    the links it did not list because they are no run directory (`run_directory_problem`). The
    removed, the kept and the gone are three lists: none is in another, and no skipped link is in
    any of them. A directory that has several names (links to it) is listed once, under the first
    name, and removed under all of them."""

    scanned: list[Path]
    removed: list[Path]
    kept: list[Path]
    gone: list[Path]
    skipped: list[Path]


class _Removal(NamedTuple):
    """What `_remove_confirmed` did with its candidates."""

    removed: list[Path]
    kept: list[Path]
    gone: list[Path]


def _distinct(paths: Iterable[Path]) -> list[Path]:
    """`paths` without a second name of a directory already in them (a link inside the design's
    directory), in order: what is searched or removed is each directory once."""
    first: dict[str, Path] = {}
    for p in paths:
        first.setdefault(os.path.realpath(p), p)
    return list(first.values())


class _Skipped(NamedTuple):
    """A link named like a run directory of the flow that is none (`run_directory_problem`), and
    why. Scrub neither lists it nor follows it."""

    link: Path
    problem: str


class _Scan(NamedTuple):
    """What `_run_directories_in` found in one directory: the run directories it lists, and the
    links named like one that it does not."""

    candidates: list[Path]
    skipped: list[_Skipped]


def _run_directories_in(
    flow_name: str, directory: Path, exclude: Sequence[Path], run_root: Path
) -> _Scan:
    """`flow_name`'s run directories among the children of `directory`, except `exclude`: a
    launch leaves out its own. A child is a run directory by `run_directory_problem`, the rule a
    launch applies too. A link named like one that fails the rule is not listed, and is returned
    with the reason. Nothing is listed in a directory that is not under `run_root`. The children
    are matched rather than a `f"{flow_name}_*"` glob, so the unhashed directory -- which that
    glob can never match -- is included too. A directory is excluded by where it resolves to,
    never by whether it exists now or has the same inode: a sibling launch's scrub may remove a
    launch's own directory while this lists, and it is still not a candidate."""
    if not directory.is_dir():
        return _Scan([], [])
    xr = directory.resolve()
    if not xr.is_relative_to(os.path.realpath(run_root)):
        return _Scan([], [])
    excluded = {Path(os.path.realpath(ex)) for ex in exclude}
    named = run_directory_name(flow_name)
    candidates: list[Path] = []
    skipped: list[_Skipped] = []
    for p in sorted(directory.iterdir()):
        if named.match(p.name) is None:
            continue
        problem = run_directory_problem(p, flow_name, xr)
        if problem is None:
            if Path(os.path.realpath(p)) not in excluded:
                candidates.append(p)
        elif p.is_symlink():
            skipped.append(_Skipped(p, problem))
    return _Scan(unique(candidates), skipped)


def run_directory_names(path: Path, flow_name: str, run_root: Path) -> list[Path]:
    """Every name of `flow_name`'s run directory `path` in its directory: `path` itself, and the
    links beside it that are run directories of the flow (`run_directory_problem`) and lead where
    it leads -- the names scrub lists it under (`_listed`). A purge removes all of them with the
    directory (`RunDirectory.delete`), so no name is left leading nowhere."""
    leads_to = os.path.realpath(path)
    beside = _run_directories_in(flow_name, path.parent, (), run_root).candidates
    return unique([path, *(p for p in beside if os.path.realpath(p) == leads_to)])


def _say_skipped(skipped: Iterable[_Skipped]) -> None:
    """Say, one line each, the links scrub did not list, and why: on the console, and in the log
    at info level, because `xeda scrub` configures no logging."""
    for link, problem in skipped:
        log.info("Not scrubbing %s: %s", link, problem)
        _say(f"skipped {link}: {problem}")


def _target_parents(design_dir: Path, run_root: Path) -> list[Path]:
    """The directories below `design_dir` that can hold a target's run directories: those that
    could be named as a target (`target_name_problem`: a name that is no flow's) and are no run
    directory (a launch writes `LAUNCH_DOCUMENTS` in every one), found as they are on disk --
    a target no design file names any more included -- and never searched deeper. A link
    leading out of the run root, or out of `design_dir`, is not followed."""
    if not design_dir.is_dir():
        return []
    xr = design_dir.resolve()
    return [
        p
        for p in sorted(design_dir.iterdir())
        if p.is_dir()
        and target_name_problem(p.name) is None
        and not any((p / name).exists() for name in LAUNCH_DOCUMENTS)
        and xr in p.resolve().parents
        and RunDirectory.lies_under(p, run_root)
    ]


class _Listed(NamedTuple):
    """A run directory as listed for removal: the path, the flow it is a run directory of, the
    resolved directory it was listed in and what the listing saw of its run records
    (`_completion_records`). Scrub judges the path again once it holds the lock
    (`_problem_now`), by what is there then, and compares the records to find a directory that a
    launch wrote its records to after the listing. `aliases` are the other names the listing
    found for the same directory (links to it, or the directory itself when `path` is a link):
    scrub removes the directory under every one of its names."""

    path: Path
    flow_name: str
    parent: Path
    records: tuple[Optional[OutputState], ...]
    aliases: tuple[Path, ...] = ()


def _completion_records(path: Path) -> tuple[Optional[OutputState], ...]:
    """What the run directory `path` holds of its run records (`COMPLETION_DOCUMENTS`): the state
    of each (`record_output_state`: identity, size and times), or None where it is not there. A
    launch that ends a run writes each one anew, made whole and renamed into place. A launch that
    finds its run fresh may write the trace again, once its records have settled. So the records
    differ when a launch has written them since they were last seen, and not when it only added
    other files to the directory. A record that was not there and is, counts as well."""
    return tuple(record_output_state(path / name) for name in COMPLETION_DOCUMENTS)


def _listed(flow_name: str, paths: Iterable[Path]) -> list[_Listed]:
    """`paths` as run directories to remove: one for each directory they lead to, under the first
    of its names, with the others as its `aliases`. A directory and a link to it are one
    candidate, so the link is removed with the directory and never left leading nowhere."""
    names: dict[str, list[Path]] = {}
    for p in paths:
        names.setdefault(os.path.realpath(p), []).append(p)
    return [
        _Listed(
            first,
            flow_name,
            Path(os.path.realpath(first.parent)),
            _completion_records(first),
            tuple(others),
        )
        for first, *others in names.values()
    ]


def _lock_path(path: Path, run_root: Path) -> Path:
    """What a candidate is locked by. A link to a directory inside the run root is locked by that
    directory: it stays one lock when another scrub, which held it, has removed the link and then
    the directory. Anything else is locked by its own path. That includes a link out of the run
    root or to nowhere: the lock refuses it, before it makes anything. A link may change while
    scrub waits for the lock, so scrub asks again once it holds the lock."""
    if path.is_symlink() and path.is_dir() and RunDirectory.lies_under(path, run_root):
        return Path(os.path.realpath(path))
    return path


def _refuse_a_change(path: Path, c: _Listed, locked: Path, leads_to: Path) -> None:
    """Under the lock of `locked`, judge a name of the directory again, as it is now: it still
    leads to the directory whose lock is held (`leads_to`, where `locked` leads), and is still
    a run directory of the flow where it was listed. Otherwise a `RunDirectoryError`, and
    nothing is removed: a name retargeted while scrub waited, or replaced, is not what the user
    confirmed, and scrub would remove a directory whose lock it does not hold."""
    if Path(os.path.realpath(path)) != leads_to:
        raise RunDirectoryError(
            f"{path} changed while scrub waited for its lock. It no longer leads to "
            f"{locked}, the directory whose lock scrub holds. It was not removed."
        )
    problem = _problem_now(c._replace(path=path))
    if problem is not None:
        raise RunDirectoryError(
            f"{path} is no longer a run directory of {c.flow_name} in {c.parent}: "
            f"{problem}. It was not removed."
        )


def _is_gone(path: Path) -> bool:
    """Whether nothing is at `path`: its name, or a directory above it, does not exist. A link is
    something, wherever it leads. An error that is no `FileNotFoundError` says nothing, so it is
    not "gone"."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        pass  # it cannot be told: the judgment that follows refuses it
    return False


def _problem_now(listed: _Listed) -> Optional[str]:
    """Why `listed.path` is no run directory of its flow in the directory it was listed in
    (`run_directory_problem`, the rule that listed it), or None if it is. Scrub asks once it
    holds the lock, so no launch is running in it.

    What the directory held when it was listed does not matter, and nor does whether it is the
    same directory. A launch writes in its run directory, and its inode change time moves with
    every file it adds or removes. Whether it wrote the run records there since the listing is
    another question, which `_completion_records` answers."""
    try:
        return run_directory_problem(listed.path, listed.flow_name, listed.parent)
    except OSError as error:
        return f"it cannot be examined ({error.strerror or error})"


def _say(message: str) -> None:
    """One line of what scrub did, as it is: not folded inside a path, and no part of it taken
    for markup."""
    console.print(message, markup=False, highlight=False, soft_wrap=True)


def _remove_confirmed(candidates: Sequence[_Listed], run_root: Path) -> _Removal:
    """List `candidates`, ask once, and remove them if confirmed: each as a run directory
    claimed under `run_root`, the real run root, under its own lock. The lock is waited for, so
    scrub never removes a directory while a launch runs in it. It is refused for a directory
    reached through a link out of the run root.

    Scrub removes the runs it listed, which are the runs that were confirmed. Once it holds a
    candidate's lock it looks at what is at the path. Nothing there any more (`_is_gone`: another
    scrub, or a purge, removed it while this one waited) is skipped, and said: the scrub wanted
    it gone. A candidate that no longer leads to the directory whose lock is held (`_lock_path`
    asked again: a link retargeted, or replaced by a directory, while scrub waited) is not
    removed, and the scrub fails (`RunDirectoryError`): scrub would remove a directory whose lock
    it does not hold. What is removed is the locked directory itself, never what a link leads to
    by then. Anything else that is no run directory of the flow by `run_directory_problem` -- no
    directory, or a link that now leads anywhere but to a run directory of the flow beside it --
    is not removed either, and the scrub fails the same way. A run directory whose run records
    are not what the listing saw (`_completion_records`) was written to by a launch after the
    listing: a run ended there, or a launch found its run fresh and refreshed the trace. Scrub
    cannot tell which, and what is there is no longer what was confirmed: it is kept, and said.
    The rest are removed. The three lists are the directories removed, kept and gone."""
    if not candidates:
        return _Removal([], [], [])
    console.print(
        f"[red]This will remove all of the following {len(candidates)} run directories:[/red]"
    )
    for c in candidates:
        console.print(c.path)
        for alias in c.aliases:
            _say(f"{alias} (another name of the same directory)")
    confirmation = console.input("Type 'yes' if you're sure you want to continue: ")
    if confirmation.lower() != "yes":
        console.print("Not confirmed. No files or folders were removed.")
        return _Removal([], [], [])
    log.warning(
        "Removing the following directories: %s",
        " ".join(str(name) for c in candidates for name in (c.path, *c.aliases)),
    )
    done = _Removal([], [], [])
    for c in candidates:
        p = c.path
        locked = _lock_path(p, run_root)
        leads_to = Path(os.path.realpath(locked))
        with run_dir_lock(locked, run_root):
            if _is_gone(p):
                log.info("Not removing %s: it is gone already", p)
                _say(f"{p} is gone already")
                done.gone.append(p)
                continue
            _refuse_a_change(p, c, locked, leads_to)
            if _completion_records(p) != c.records:
                log.info("Not removing %s: its run records changed after the listing", p)
                _say(
                    f"kept {p}: its run records changed after the listing "
                    "(a run finished or refreshed there)"
                )
                done.kept.append(p)
                continue
            for alias in c.aliases:  # each name of the directory, judged as the first is
                if not _is_gone(alias):
                    _refuse_a_change(alias, c, locked, leads_to)
            # The links first, as themselves: another scrub that listed one then finds it gone,
            # and a link to a directory that is gone is a link to nowhere, which its lock
            # refuses. Then the directory whose lock is held, never what a link leads to by now.
            RunDirectory.claimed(locked, run_root).delete(p, *c.aliases)
        done.removed.append(p)
    summary = f"{len(done.removed)} folders removed"
    if done.kept:
        summary += f", {len(done.kept)} kept"
    if done.gone:
        summary += f", {len(done.gone)} gone already"
    _say(summary + ".")
    return done


def scrub_runs(
    flow_name: str,
    dir: Path,
    exclude: Sequence[Path] = (),
    run_root: Optional[Path] = None,
) -> bool:
    """Find and (with confirmation) remove `flow_name`'s run directories directly in `dir`,
    the ones named `flow_name` or `flow_name_<hash>` (`_run_directories_in`), except `exclude`.
    A launch's `--scrub` scrubs its own directory this way, so it never reaches another
    target's. Each is removed as a run directory claimed under `run_root` (default: `dir`'s
    parent), the real run root, so none that leads out of it is ever removed, together with the
    links to it that the listing found (`_listed`). A link named like a run directory that is
    none is said, and left alone."""
    if run_root is None:
        run_root = dir.parent
    scan = _run_directories_in(flow_name, dir, exclude, run_root)
    _say_skipped(scan.skipped)
    listed = _listed(flow_name, scan.candidates)
    return bool(_remove_confirmed(listed, run_root).removed)


def scrub_design(
    flow_name: str, design_dir: Path, *, run_root: Path, target: str | None = None
) -> ScrubResult:
    """Find and (with one confirmation) remove `flow_name`'s run directories of the design whose
    directory is `design_dir`, under `run_root`: without `target`, those directly in it (the
    ones made before targets existed) and in every target's directory below it; with one, only
    those in `<design_dir>/<target>`. What is on disk decides (`_target_parents`); no design
    file is read, so a target the design no longer names is found, and one that was never built
    is nothing to do. A link named like a run directory that is none (`run_directory_problem`)
    is not listed: it is said before the question, and returned as `skipped`. A `target` that is
    no target name is a `RunDirectoryError`, before anything is looked at."""
    if target is not None:
        problem = target_name_problem(target)
        if problem is not None:
            raise RunDirectoryError(problem)
        parent = design_dir / target
        xr = design_dir.resolve()
        found = (
            [parent]
            if parent.is_dir()
            and xr in parent.resolve().parents
            and RunDirectory.lies_under(parent, run_root)
            else []
        )
    else:
        found = [design_dir, *_target_parents(design_dir, run_root)] if design_dir.is_dir() else []
    parents = _distinct(found)
    scans = [_run_directories_in(flow_name, parent, (), run_root) for parent in parents]
    skipped = unique([s for scan in scans for s in scan.skipped])
    candidates = [p for scan in scans for p in scan.candidates]
    _say_skipped(skipped)
    done = _remove_confirmed(_listed(flow_name, candidates), run_root)
    return ScrubResult(parents, done.removed, done.kept, done.gone, [s.link for s in skipped])


def _refuse_inputs_inside(run_path: Path, flow_name: str, files: Iterable[Path]) -> None:
    """xeda empties and rewrites a flow's run directory, so no file of the design, and no
    file a setting reads, may lie in the run directory of the flow that reads it (in another
    run's directory it is that run's output, read by content)."""
    run_dir = Path(os.path.realpath(run_path))
    inside = sorted({f for f in files if Path(os.path.realpath(f)).is_relative_to(run_dir)})
    if inside:
        raise RunDirectoryError(
            f"{inside[0]} lies in {run_path}, {flow_name}'s own run directory, which xeda "
            "empties and rewrites: keep your files in your design, outside the run root"
        )


def _free_working_locations(flow: Flow) -> None:
    """Before the run: each working location of `flow` (a setting with the `WORKING` role, a name
    inside the run directory) is where its tools work, never through a symbolic link out of the
    run directory. A tool may leave its working location's own name a link, anywhere: that link
    is removed as itself -- never what it points to -- and the tool makes the location anew. One
    reached through a link that leads out is refused, naming the link (`RunDirectory.inside`),
    before anything runs there."""
    run_directory = flow.run_directory

    def free(written: WrittenLeaf) -> Any:
        leaf = written.value
        if written.role != WORKING_ROLE or not isinstance(leaf, (str, os.PathLike)):
            return leaf
        if os.path.normpath(os.fspath(leaf)) in ("", "."):
            return leaf  # the run directory itself
        located = run_directory.inside(leaf)
        if located.is_symlink() and not RunDirectory.lies_under(located, run_directory.path):
            log.info(
                "Removing %s, a link left where `%s` works: it leads out of the run directory",
                located,
                written.key,
            )
            run_directory.remove(located)
        return leaf

    map_written_leaves(flow.settings, free)


def _warn_outputs_to_delivered_nothing(flow: Flow, outputs_to: Path) -> None:
    """`--outputs-to` copies only artifacts a successful, requested flow reported inside its run
    directory: a flow that reported none there, or names no setting deliverable, delivers nothing
    and nothing else says so. Name the flow and, where it has any, the deliverable settings of its
    own a location could be given instead (`vcd` for a simulator), so a bare run is not mistaken
    for xeda silently dropping a file."""
    names = deliverable_setting_names(type(flow.settings))
    which = ", ".join(names) if names else "none"
    log.warning(
        "--outputs-to %s: %s delivered nothing there -- its deliverable settings (%s) are what "
        "--outputs-to copies; give one a location (%s) for something to deliver",
        outputs_to,
        flow.name,
        which,
        LOCATION_FORMS,
    )


def _setting_naming(flow_class: type[Flow], output: str) -> str | None:
    """The deliverable setting of `flow_class` that names its output `output`: the one that
    switches it on (`enabled_by`), else a setting of the output's own name."""
    declaration = declared_outputs(flow_class).get(output)
    for name in (declaration.enabled_by if declaration else None, output):
        if name and written_role(flow_class.Settings, name) == DELIVERABLE_ROLE:
            return name
    return None


def _refuse_outputs_to_a_programmer(
    plan: Plan, node: PlanNode, outputs_to: Path, *, remote: bool = False
) -> None:
    """`--outputs-to` copies what the requested flow writes. A flow that programs a device
    (`Flow.action_reason`) writes no outputs, so there is nothing to deliver: a `DeliveryError`
    before anything runs, a dry run alike, naming what would deliver the file it reads -- the
    setting of the flow that writes it in this plan, given a location (`LOCATION_FORMS`); on a
    `remote` run, where that setting would deliver on the
    remote host, the request of that flow, whose own outputs come back -- or the design source it
    is."""
    flow_class = node.flow_class
    if flow_class.action_reason is None:
        return
    producers, settings, sources = [], [], []
    for selected in node.inputs:
        sources += [str(path) for path in selected.sources]
        for reference in selected.references:
            producer = plan.node(reference.node)
            producers.append(producer.name)
            setting = _setting_naming(producer.flow_class, reference.output)
            if setting is not None:
                settings.append(f"-s flows.{producer.name}.{setting}=$PWD/<file>")
    message = (
        f"--outputs-to {outputs_to}: {flow_class.name} writes no outputs "
        f"({flow_class.action_reason}), so there is nothing to deliver"
    )
    if remote and producers:
        message += ". To receive the file it reads, request the flow that writes it instead: " + (
            ", ".join(
                f"xeda run --remote {name} ... --outputs-to {outputs_to}" for name in producers
            )
        )
    elif settings:
        message += (
            ". To receive the file it reads, give the setting that writes it a location "
            f"({LOCATION_FORMS}) instead: " + ", ".join(settings)
        )
    if sources:
        message += ". The file it reads is a design source: " + ", ".join(sources)
    raise DeliveryError(message, before_run=True)


FlowLauncherType = TypeVar("FlowLauncherType", bound="FlowLauncher")


def _artifact_rows(artifacts: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """Flatten an artifacts mapping into `(label, path, end_section)` "Artifacts:" table rows.

    Each label's paths are found with `iter_artifact_paths`, so a value nested through any mix
    of mappings, lists and tuples is flattened the same way as everywhere else artifacts are
    walked. The label is shown once, on the row of its first path; a label with no path leaves
    (`None`, `""`, an empty list/dict, ...) gets no rows at all.
    """
    rows: list[tuple[str, str, bool]] = []
    for label, value in artifacts.items():
        paths = [str(path) for path in iter_artifact_paths(value)]
        for i, path in enumerate(paths):
            rows.append((label if i == 0 else "", path, i == len(paths) - 1))
    return rows


def _drop_unwritten_artifacts(flow: Flow) -> None:
    """Report only the artifacts a failed run actually wrote (`drop_unwritten_artifacts`)."""
    flow.results.artifacts = Box(
        drop_unwritten_artifacts(
            flow.results.artifacts,
            flow.wrote_output,  # only a file whose recorded prior state changed
            flow.name,
        )
    )


class FlowLauncher:
    """
    Manage running flows and their dependencies, make-like: see `launch_flow`.
    """

    #: whether `run` takes a chain or a request with explicit input bindings; a launcher that
    #: hands requests to workers (`Dse`) refuses them before anything is submitted
    accepts_bindings: ClassVar[bool] = True

    class Settings(XedaBaseModel):
        """Settings for FlowLaunchers"""

        debug: bool = False
        dump_settings_json: bool = True
        display_results: bool = True
        dump_results_json: bool = True
        #: run every flow, even one whose trace still matches; without it, make-like
        rebuild_all: bool = False
        #: a run directory per settings variant, <design>/<flow>_<settings hash>, instead of
        #: one per flow, <design>/<flow>
        hashed_run_dirs: bool = False
        #: empty each flow's run directory before it runs, and run every flow (implies
        #: `rebuild_all`)
        clean: bool = False
        backups: bool = False
        # remove flow files except settings.json, results.json, and artifacts _after_ run:
        post_cleanup: bool = False
        # remove flow_run folder and all of its contents _after_ running the flow (it needs no
        # `post_cleanup`: `_run_dir_policy` makes one imply the other):
        post_cleanup_purge: bool = False
        # remove previous flow directories _before_ running the flow:
        scrub_old_runs: bool = False
        #: copy the requested flow's artifacts into this directory once it succeeded
        outputs_to: Optional[Path] = None
        #: replace a file at an output path the user named that is not xeda's own earlier,
        #: unchanged copy (`xeda.deliver`): never an input, a directory or a run root
        overwrite_outputs: bool = False

        @model_validator(mode="before")
        @classmethod
        def _removed_settings(cls, values):
            if isinstance(values, dict):
                for old, replacement in _REMOVED_LAUNCHER_SETTINGS.items():
                    if old in values:
                        raise ValueError(f"`{old}` was removed: use {replacement}")
            return values

        def __setattr__(self, name: str, value: Any) -> None:
            if name in _REMOVED_LAUNCHER_SETTINGS:
                raise ValueError(f"`{name}` was removed: use {_REMOVED_LAUNCHER_SETTINGS[name]}")
            super().__setattr__(name, value)

    def __init__(self, run_root: Union[str, Path, None] = None, **kwargs) -> None:
        self.settings = self.Settings(**kwargs)
        if self.settings.outputs_to is not None:
            self.settings.outputs_to = Path(os.path.abspath(self.settings.outputs_to))
        # Refused here, before anything runs, if it is not xeda's; created when a flow first
        # needs it (`run_root`), so a launch that fails at its input leaves none behind.
        self._start = Path.cwd()
        self._run_root = Path(run_root or DEFAULT_RUN_ROOT).resolve()
        self._run_root_ready = (
            ensure_run_root(self._run_root, start=self._start, create=False) is not None
        )
        log.debug("%s run_root=%s", self.__class__.__name__, self._run_root)
        if self.settings.debug:
            log.setLevel(logging.DEBUG)
            log.root.setLevel(logging.DEBUG)
        self.debug = self.settings.debug
        #: every flow launched through this launcher, dependencies included, in completion order
        self.launched: List[Flow] = []
        self._launch_depth = 0
        #: post-run clean-ups waiting for the flow the current launch was asked for to complete
        self._pending_clean_ups: List[Tuple[Flow, Path, Path, RunDirPolicy]] = []
        #: the run directories the current launch has entered
        self._claims: set[Path] = set()
        #: how many flows `launched` held when the current launch began
        self._launched_before = 0
        #: the replacements the user confirmed in the current launch, each as the file was then
        self._confirmed_replacements = ConfirmedReplacements()
        #: the deliveries of the current launch's producers, checked when it started, by node:
        #: each producer checks again, at its turn, with its own
        self._deliveries_ahead: dict[NodeKey, Deliveries] = {}
        #: asked, at an interactive terminal, whether to replace files in the way of named
        #: outputs (`xeda.deliver.Deliveries.check`); None: only `overwrite_outputs` counts
        self.confirm_overwrite: Optional[Callable[[Sequence[Conflict]], bool]] = None
        #: the design and project file `run()` was given: never an output's destination
        self._launch_inputs: List[Path] = []
        #: every file the flows of the current launch read (`xeda.deliver.ReadInputs`)
        self._read_inputs = ReadInputs()
        self._delivered_files = DeliveredFiles()
        #: the deliveries of the current launch's flows, made when it has finished
        self._pending_deliveries: List[Tuple[Flow, Deliveries]] = []
        self._request_context: _Request | None = None
        #: the selected target and the name of the design last loaded for a request, for
        #: documents to report; None until a request has loaded one
        self.target: str | None = None
        self.design_name: str | None = None
        self._plans: dict[int, tuple[Plan, Any, Any]] = {}
        self._planned_completed: dict[tuple[int, NodeKey], Flow] = {}
        self.last_plan: Plan | None = None
        self._completed_runs: dict[Path, tuple[Flow, CompletedRun]] = {}

    @property
    def run_root(self) -> Path:
        """The run root (`xeda.run_root`), resolved: created and marked when first used."""
        if not self._run_root_ready:
            root = ensure_run_root(self._run_root, start=self._start)
            assert root is not None  # created when absent
            self._run_root, self._run_root_ready = root, True
        return self._run_root

    def load_run_root(self, create: bool = True) -> Optional[Path]:
        """The run root a design load keeps its own things in (`design.loading_in_run_root`):
        with `create`, made and marked as `run_root` does; without it -- a pure plan, a lookup
        that must create nothing -- only one that is already marked, else None."""
        if create:
            return self.run_root
        if self._run_root_ready:
            return self._run_root
        return ensure_run_root(self._run_root, start=self._start, create=False)

    @property
    def xeda_run_dir(self) -> Path:
        """Removed: the run root is `run_root`."""
        raise AttributeError("`xeda_run_dir` was removed: use run_root")

    def run_path_of(
        self,
        design_name: str,
        node_name: str,
        identity: Optional[str] = None,
        *,
        target: str | None = None,
    ) -> Path:
        """`<run root>/<design>[/<target>]/<node>`, or `<node>_<identity>` with hashed run
        directories: strictly inside the run root, resolved -- a `RunDirectoryError` otherwise,
        before anything (the lock beside it included) is written. A link at that name is used
        only if it is a run directory of the node by `run_directory_problem`, the rule scrub
        lists run directories by: it leads to a directory named like one beside it. A design
        selected as a target runs in that target's directory, as its dependencies do; one
        without, as it always has. `target` is the design's own (`Design.target`), never read
        from the launcher, which may be reused for another design."""
        subdir = node_name
        if self.settings.hashed_run_dirs and identity:
            subdir += f"_{identity[:DIR_NAME_HASH_LEN]}"
        if not DESIGN_NAME.fullmatch(design_name):
            raise RunDirectoryError(f"{design_name!r} is not a design name")
        parent = Path(design_name)
        if target is not None:
            problem = target_name_problem(target)
            if problem is not None:
                raise RunDirectoryError(problem)
            parent /= target
        ensure_run_root(self._run_root, start=self._start, create=False)
        run_path = self._run_root / parent / subdir
        if not RunDirectory.lies_under(run_path, self._run_root):
            raise RunDirectoryError(
                f"{run_path} leads out of the run root {self._run_root} (through a symbolic "
                "link): xeda runs only inside its run root; remove the link"
            )
        if run_path.is_symlink():  # the rule `xeda scrub` lists a run directory by
            beside = Path(os.path.realpath(run_path.parent))
            problem = run_directory_problem(run_path, node_name, beside)
            if problem is not None:
                raise RunDirectoryError(
                    f"{run_path} cannot be the run directory of {node_name}: {problem}. Remove "
                    f"the link, or make it lead to a run directory of {node_name} beside it."
                )
        return run_path

    def get_flow_run_path(
        self,
        design_name: str,
        node_name: str,
        identity: str | None = None,
        *,
        target: str | None = None,
    ) -> Path:
        """Create/mark the root, then revalidate the execution path at operation time. A name
        that is refused (a design's, a target's) is refused before the root is touched."""
        self.run_path_of(design_name, node_name, identity, target=target)
        self.run_root
        return self.run_path_of(design_name, node_name, identity, target=target)

    def resolve(
        self,
        flow_class: type[Flow],
        design: Design,
        flow_settings: Mapping[str, Any] | Flow.Settings | None,
        all_flows_settings: Mapping[str, Any] | None = None,
        *,
        origins: Sequence[tuple[str, Mapping[str, Any]]] = (),
        command_line: Mapping[str, Mapping[str, Any]] | None = None,
        api_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        flow_request: FlowRequest | None = None,
        binding_layers: Sequence[BindingLayer] = (),
    ) -> Plan:
        """Resolve one request without constructing a flow, probing tools or writing files.

        A request composes its files itself and passes them as `origins`, lowest precedence
        first (`_request`: the project's sections, then the design's; the remote runner the
        same). A launch that is only handed a built design (`run_flow`, `launch_flow`, a direct
        call) composes none, and then the design's own `flows` sections are its file origin, the
        sections it was handed come after them, and the settings after those. So planning a
        design and launching it from the same arguments resolve the same plan."""
        recorded_settings = as_recorded(flow_settings or {})
        recorded_sections = as_recorded(all_flows_settings or {})
        direct = not origins
        design_label = f"the design {design.name}"
        # Capture reserved keys before settings composition: `inputs` is wiring the resolver
        # selects edges by, never a setting.
        layers: list[BindingLayer] = []
        clean_origins = []
        if direct:
            clean, bindings = split_bindings(deepcopy(design.flow), location=design_label)
            clean_origins.append((design_label, clean))
            layers.append(bindings)
        layers.extend(binding_layers)
        for location, values in origins:
            clean, bindings = split_bindings(values, location=location)
            clean_origins.append((location, clean))
            layers.append(bindings)
        all_flows_settings, bindings = split_bindings(
            all_flows_settings or {}, location=SUPPLIED_SECTIONS_ORIGIN
        )
        if bindings.entries or bindings.invalid_inputs or (not origins and not binding_layers):
            layers.append(bindings)
        if direct:
            clean_origins.append((SUPPLIED_SECTIONS_ORIGIN, all_flows_settings))
        clean_cli, bindings = split_bindings(
            command_line or {}, location=COMMAND_LINE_ORIGIN, kind="cli"
        )
        layers.append(bindings)
        clean_api, bindings = split_bindings(api_overrides or {}, location=API_ORIGIN, kind="api")
        layers.append(bindings)
        clean_root, bindings = split_bindings(
            {flow_class.name: flow_settings or {}}, location=API_ORIGIN, kind="api"
        )
        layers.append(bindings)
        # Preserve the context/full value of a Settings instance on ordinary direct calls.
        if not isinstance(flow_settings, Flow.Settings):
            flow_settings = clean_root[flow_class.name]
        plan = resolve_plan(
            flow_class,
            design,
            flow_settings,
            all_flows_settings,
            runner_cwd=Path.cwd(),
            run_root=self._run_root,
            hashed_run_dirs=self.settings.hashed_run_dirs,
            run_path=self.run_path_of,
            origins=clean_origins,
            command_line=clean_cli,
            api_overrides=clean_api,
            debug=self.settings.debug,
            flow_request=flow_request,
            binding_layers=layers,
        )
        # The resolver validates final agreed settings. The original request may contain
        # partial shared values that become valid only along a declared edge.
        self._plans[id(plan)] = (
            plan,
            recorded_settings,
            recorded_sections,
        )
        return plan

    def _resolve_request(self, request: _Request) -> Plan:
        return self.resolve(
            request.flow_class,
            request.design,
            request.settings,
            request.sections,
            origins=request.origins,
            command_line=request.command_line,
            api_overrides=request.api_overrides,
            flow_request=request.flow_request,
            binding_layers=request.binding_layers,
        )

    def _validate_plan(
        self,
        plan: Plan,
        flow_class: type[Flow],
        design: Design,
        settings: Mapping[str, Any] | Flow.Settings | None,
        sections: Mapping[str, Any] | None,
        node_key: NodeKey | None = None,
    ) -> PlanNode:
        """Validate a plan minted by this launcher; external plans are not supported."""
        captured = self._plans.get(id(plan))
        context = plan.context
        # the whole design: the plan is of this request, not of one node's identity
        design_hash = design.parts_hash(DESIGN_PARTS)
        if (
            captured is None
            or captured[0] is not plan
            or (
                context.design_hash != design_hash
                or context.design_root != design.root_path
                or context.runner_cwd != Path.cwd()
                or context.run_root != self._run_root
                or context.hashed_run_dirs != self.settings.hashed_run_dirs
                or context.debug != self.settings.debug
                or context.target != design.target
            )
        ):
            raise FlowFatalError("The plan does not match this request's context")
        node_key = node_key or NodeKey(flow_class.name)
        if node_key not in plan:
            raise FlowFatalError(f"The plan has no node for this request: {node_key.label}")
        node = plan.node(node_key)
        supplied = as_recorded(settings or {})
        supplied_context = settings.context if isinstance(settings, Flow.Settings) else {}
        if (
            node.flow_class is not flow_class
            or any(
                value is not None and node.settings.context.get(key) != value
                for key, value in supplied_context.items()
            )
            or (
                supplied != as_recorded(node.settings)
                and (node.name != plan.requested or supplied != captured[1])
            )
            or as_recorded(sections or {}) != captured[2]
        ):
            raise FlowFatalError("The plan does not match this request's settings")
        # Every node's identity is recomputed from its settings and its ordered input origins,
        # producers first, by the one helper the resolver froze it with.
        identities: dict[str, str] = {}
        for planned in plan.nodes:
            try:
                origins = input_origins(planned.inputs, identities.__getitem__)
            except KeyError as error:
                raise FlowFatalError(
                    f"The plan's node {planned.name} reads a node that does not precede it"
                ) from error
            settings_hash = flow_run_hash(planned.flow_class.name, planned.settings, design.name)
            identities[planned.name] = node_identity(settings_hash, origins)
            if (
                planned.settings_hash != settings_hash
                or planned.origins != origins
                or planned.flowrun_hash != identities[planned.name]
                or planned.run_path
                != self.run_path_of(
                    design.name, planned.name, planned.flowrun_hash, target=design.target
                )
            ):
                raise FlowFatalError("The plan does not match this request's identity or path")
        return node

    def launch_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        *,
        depender: Optional[Flow] = None,
        all_flows_settings: Union[Dict, None] = None,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
    ) -> Flow:
        """Launch `flow_class` on `design`: the one procedure every flow run, and every
        dependency run, goes through -- make's order: bring every prerequisite up to date, then
        judge this flow against them. Its stages, in order:

        1. **input** (`_input_settings`): validate the settings in their context (design root,
           start directory) and apply the launcher's ``--debug``. The result is the run's input,
           never modified afterwards. A missing required setting, or a design the flow cannot
           run, fails the launch here.
        2. **identity** (`_run_identity`): the design's hash and the settings' `flowrun_hash`;
           the run directory, `<design>[/<target>]/<flow>` (``hashed_run_dirs``:
           `<flow>_<flowrun_hash>`), which the flow must be able to work in
           (`Flow.check_run_directory`, judged on the path before anything is created),
           locked from here until the run's trace is written (`run_lock`). With ``clean``, the
           directory is emptied now (or backed up, with ``backups``).
        3. **prepare**: construct the flow with its own copy of the input and call its `init()`.
           `init()` runs for a flow that turns out to be fresh too, so it must not change a file
           in the run directory: every file there is an output of the last run.
        4. **producers** (`_run_producers`): launch each producer the plan names through this
           same procedure, in a sibling run directory, with the settings the plan holds for it.
        5. **freshness**: without ``rebuild_all`` or ``clean`` (the default), a flow whose trace
           still matches what it would consume now (`trace.check_trace`) is not run again: its
           recorded results are reused. Otherwise `flow.stale_reason` says why it runs. A flow that can
           never be reused (`Flow.always_runs`: it programs a device, draws a new random seed,
           ...) always runs, with that reason, and no trace records it.
        6. **run**: delete the trace and the reports the last run read (`Trace.reports`: a
           previous run's report is never this run's), then record every expected input as the
           run finds it
           (`trace_inputs.snapshot_inputs`: a file edited while the run goes on then no longer
           matches its trace), record `settings.json`, and execute (`_execute`: `run()`,
           `parse_reports()`, `check_results()`, results; a failed run's artifacts only as far as
           it wrote them, `Flow.wrote_output`).
        7. **report** (`_report`): artifacts, `results.json`; then, for a successful run, the
           trace (`trace_inputs.build_trace`: the snapshot's inputs; the files the run's
           depfiles and settings name, an input unless the run's directory holds it; and the
           run's outputs -- every file of its run directory).
           The post-run clean-up (`_clean_up`) of every flow that ran waits until the flow this
           launch was asked for has completed: until then, a flow may still read files its
           dependencies wrote without declaring them.

        A launch enters each run directory once: the plan has one node per flow, and a second
        entry is a `FlowFatalError`. Every flow launched is appended to `launched` as it
        completes, whether it succeeded, failed or raised. A flow that did not run, because the
        delivery of one of its producers was refused before the producer's tool ran, is not.

        Outputs the user named (`xeda.deliver`) are checked before any tool of the launch runs.
        The requested flow checks the deliveries of every flow of the plan when the launch
        starts (`_check_deliveries_ahead`): first what no answer could allow, then the
        questions, the requested flow's own last. All of it comes before the launch scrubs older
        runs (`scrub_old_runs`) and before it takes the lock of its run directory. Deliveries are
        noted when their flow succeeded or was found up to date, and
        delivered once the whole launch has finished -- only when the requested flow succeeded
        or was found up to date: a launch that raised, or whose requested flow reports failure,
        delivers nothing, not even a successful dependency's outputs (`_finish_launch`).
        """
        top_level = self._launch_depth == 0
        if top_level:
            self._claims = set()
            self._launched_before = len(self.launched)
            self._confirmed_replacements = ConfirmedReplacements()
            self._deliveries_ahead = {}
            self._planned_completed = {}
            self._completed_runs = {}
            # every file the flows of this launch read: the requested flow registers the
            # settings of the whole plan, and each flow the files it prepares
            self._read_inputs = ReadInputs(self._launch_inputs)
            self._delivered_files = DeliveredFiles()
            self._pending_deliveries = []
        self._launch_depth += 1
        try:
            flow = self._launch(
                flow_class,
                design,
                flow_settings,
                depender,
                all_flows_settings,
                plan=plan,
                plan_node=plan_node,
            )
            if plan is not None:
                self._planned_completed[(id(plan), plan_node or NodeKey(flow.name))] = flow
        except BaseException:
            self._launch_depth -= 1
            if top_level:
                # a launch that failed delivers nothing; its clean-ups' failures are logged, and
                # the launch's error propagates
                self._finish_launch(deliver=False)
            raise
        self._launch_depth -= 1
        if top_level:
            # only a requested flow that succeeded, or was found up to date, delivers: one whose
            # results report failure returns here, and delivers nothing, not even what its
            # dependencies noted
            error = self._finish_launch(deliver=flow.succeeded)
            if error is not None:
                raise error
            if (
                self.settings.outputs_to is not None
                and flow.succeeded
                and not any(d.key == OUTPUTS_TO for d in flow.deliveries)
            ):
                _warn_outputs_to_delivered_nothing(flow, self.settings.outputs_to)
        return flow

    def _finish_launch(self, deliver: bool) -> Optional[Exception]:
        """At the end of a top-level launch: with `deliver` (it completed, and its requested
        flow succeeded or was up to date), the deliveries noted for its flows (`_defer_delivery`),
        each under its run directory's lock, in completion order; then the deferred clean-ups,
        which may remove a file that is delivered. A flow whose run directory a clean-up deletes
        delivers by moving its files. Each on its own, a failure logged and the next
        going on. The first failure, if any."""
        self._claims = set()
        self._deliveries_ahead = {}
        deliveries, self._pending_deliveries = self._pending_deliveries, []
        # A flow whose run directory the clean-ups below delete gives its files away by rename. A
        # flow found up to date has no clean-up pending, so it is copied from and keeps its files.
        purged = {
            id(flow)
            for flow, _settings, _results, policy in self._pending_clean_ups
            if policy.purges
        }
        first_error: Optional[Exception] = None
        for flow, delivery in deliveries if deliver else []:
            try:
                with run_dir_lock(flow.run_path, self.run_root):
                    flow.deliveries = delivery.deliver(move=id(flow) in purged)
            except Exception as e:  # noqa: BLE001 - every delivery gets its turn
                # `deliver()` records, and reports through `delivery.delivered`, whatever it
                # copied before raising (an `OSError` partway through), so a `--json` reader still
                # sees the copies that actually reached disk
                flow.deliveries = delivery.delivered
                log.error("Delivering the outputs of %s failed: %s", flow.name, e)
                first_error = first_error or e
        pending, self._pending_clean_ups = self._pending_clean_ups, []
        for flow, settings_json, results_json, policy in pending:
            try:
                with run_dir_lock(flow.run_path, self.run_root):
                    self._clean_up(flow, settings_json, results_json, policy)
            except Exception as e:  # noqa: BLE001 - every clean-up gets its turn
                log.error("Cleaning up %s failed: %s", flow.run_path, e, exc_info=True)
                first_error = first_error or e
        return first_error

    def _confirm_replacing(self, conflicts: Sequence[Conflict]) -> bool:
        """Ask `confirm_overwrite` whether to replace the files in the way. A file the user said
        yes to is asked about once in a launch, while it stays as it was: a producer's deliveries
        are checked ahead and again at its turn."""
        if self.confirm_overwrite is None:
            return False
        return self._confirmed_replacements.confirm(self.confirm_overwrite, conflicts)

    def _check_deliveries_ahead(
        self,
        plan: Plan,
        requested: PlanNode,
        design: Design,
        deliveries: Sequence[Delivery],
        run_path: Path,
    ) -> None:
        """Make now the checks that every flow of the plan makes of its deliveries when its turn
        comes (`Deliveries.check`), so that a refusal, or a question, never follows the run of an
        earlier flow. First comes what no answer could allow (a file a flow reads that lies in
        its own run directory, `--outputs-to`, and `Deliveries.refuse`), for every flow, the
        requested flow included -- and a destination that two deliveries name, or one inside
        another (`refuse_shared_destinations`: every destination known before the run, which is
        every named delivery of the launch and the files `--outputs-to` is expected to deliver
        from the requested flow's last run; the files that only this run reveals are compared
        once it has run); then the questions of the producers, in the order they run, each asked
        once, and last the requested flow's
        own, which runs last. So the launch has asked everything before any tool runs, before it
        scrubs older runs (`--scrub`) and before it takes the lock of its run directory: a launch
        the user declines has removed nothing, and a question that waits for the user holds up no
        other launch or scrub of that directory. Each flow keeps the object it checked, and
        checks again with it at its turn: that finds the record this check anchored, so the
        destination is read once in the launch, and asks again only about a file that changed
        meanwhile. At its turn, under its lock, the flow reads the delivery record again if
        another launch wrote it meanwhile."""
        # What no answer could allow comes first, for every flow of the plan: a file a flow reads
        # that lies in its own run directory, which xeda empties and rewrites. A flow checks it
        # again at its turn, for a file made since.
        for planned in plan.nodes:
            _refuse_inputs_inside(
                planned.run_path,
                planned.name,
                [*design_files(design), *setting_files(planned.settings)],
            )
        outputs_to = self.settings.outputs_to
        own = self._deliveries_of(run_path, deliveries, requested.name)
        own.check_outputs_to(outputs_to)
        predicted = (
            outputs_to_deliveries(
                recorded_artifacts(run_path / "results.json"), run_path, outputs_to
            )
            if outputs_to is not None
            else []
        )
        own.refuse(predicted)
        ahead: dict[NodeKey, Deliveries] = {}
        copies: list[tuple[str, Delivery, Path]] = []  # in the order the flows run
        for planned in plan.nodes:
            if planned.node_key == requested.node_key:
                known = [*deliveries, *predicted]
            else:
                known = split_deliveries(planned.settings, design.name)
                if known:
                    ahead[planned.node_key] = self._deliveries_of(
                        planned.run_path, known, planned.name
                    )
            copies += [(planned.name, d, d.destination) for d in known]
        for producer in ahead.values():
            producer.refuse()
        refuse_shared_destinations(copies, before_run=True)
        for producer in ahead.values():
            producer.check()
        own.check(predicted)
        ahead[requested.node_key] = own
        self._deliveries_ahead = ahead

    def _deliveries_of(self, run_path: Path, named: Sequence[Delivery], owner: str) -> Deliveries:
        """The deliveries `named` of the flow `owner` that runs in `run_path`, for this launch."""
        return Deliveries(
            run_path,
            self.run_root,
            named,
            inputs=self._read_inputs,  # the launch's
            overwrite=self.settings.overwrite_outputs,
            confirm=self._confirm_replacing,
            owner=owner,
            files=self._delivered_files,  # the launch's
        )

    def _entered(self, run_path: Path) -> bool:
        """Whether this launch built a flow for `run_path`, so that the `results.json` there is
        its own and not one an earlier launch left."""
        where = Path(os.path.abspath(run_path))
        return any(
            Path(os.path.abspath(f.run_path)) == where
            for f in self.launched[self._launched_before :]
        )

    def _claim_run_dir(self, run_path: Path) -> None:
        """Record that this launch entered `run_path`. The plan has one node per flow, so a
        directory is entered once; a second entry would run over what the first produced."""
        key = Path(os.path.abspath(run_path))
        if key in self._claims:
            raise FlowFatalError(f"{run_path} is entered twice in one launch")
        self._claims.add(key)

    def _launch(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        depender: Optional[Flow],
        all_flows_settings: Union[Dict, None],
        *,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
    ) -> Flow:
        """`launch_flow`'s stages, for one flow."""
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        runner_cwd = Path.cwd()
        if plan is None:
            request = self._request_context
            run_flows = {flow_name, *transitive_dependencies(flow_class)}
            plan = self.resolve(
                flow_class,
                design,
                flow_settings,
                all_flows_settings,
                origins=request.origins if request else (),
                command_line=(
                    {
                        name: values
                        for name, values in request.command_line.items()
                        if name in run_flows
                    }
                    if request
                    else None
                ),
                api_overrides=request.api_overrides if request else None,
                binding_layers=request.binding_layers if request else (),
            )
        node = self._validate_plan(
            plan, flow_class, design, flow_settings, all_flows_settings, plan_node
        )
        if depender is None and self.settings.outputs_to is not None:
            # before the run root, a lock or a producer
            _refuse_outputs_to_a_programmer(plan, node, self.settings.outputs_to)
        flow_settings = node.settings
        input_settings = self._input_settings(
            flow_class, flow_settings, design, runner_cwd, depender
        )
        # a deliverable given as a location becomes its conventional name here, before the
        # identity; the tools write that, and a delivery copies it to the location
        deliveries = split_deliveries(input_settings, design.name)
        design_hash, flowrun_hash, run_path, settings_hash = self._run_identity(
            flow_class, flow_name, design, input_settings, node
        )
        # What the flows read -- every file their settings name, and every file under a
        # directory one names, as the trace lists it -- is an input no delivery of the launch
        # may replace, nor land beside in such a directory. The requested flow registers the
        # reads of the whole plan, and makes the checks of every flow's deliveries, before any
        # flow is entered: a refusal never comes after the tool of an earlier flow ran.
        self._read_inputs.add(design_files(design))
        if plan_node is None:
            for planned in plan.nodes:
                register_read_settings(
                    self._read_inputs, planned.settings, planned.run_path, self.run_root
                )
            self._check_deliveries_ahead(plan, node, design, deliveries, run_path)
        _refuse_inputs_inside(
            run_path, flow_name, [*design_files(design), *setting_files(input_settings)]
        )
        policy = self._run_dir_policy()
        self._claim_run_dir(run_path)
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        # Scrub siblings before taking our own lock: concurrent hashed variants must not
        # each hold their directory while waiting to delete the other one.
        if policy.scrub_old_runs:
            scrub_runs(flow_name, run_path.parent, [run_path], run_root=self.run_root)
        with run_dir_lock(run_path, self.run_root), ExitStack() as read_leases:
            run_path.mkdir(parents=True, exist_ok=True)
            run_directory = RunDirectory.claimed(run_path, self.run_root)
            # the deliveries, checked again before `--clean`, before this flow's `init()` and
            # before any producer is launched, so before any tool of the plan runs. Each flow,
            # the requested one included, was asked about its files before the scrub above and
            # before this lock was taken (`_check_deliveries_ahead`): a file that changed since
            # is asked about again
            outputs_to = self.settings.outputs_to if depender is None else None
            delivery = self._deliveries_ahead.pop(node.node_key, None)
            if delivery is None:
                delivery = self._deliveries_of(run_path, deliveries, flow_name)
            else:
                # made ready before this lock was taken: the record may have been written since
                delivery.reread_record()
            # the directory itself, not only a file predicted from the last run's artifacts: a
            # location becomes a concrete `Delivery` only once its tool has run and reported an
            # artifact, so without this a run root or an input named by `--outputs-to` is refused
            # only after the requested flow's tools (its dependencies' included) have already run
            delivery.check_outputs_to(outputs_to)
            delivery.check(
                outputs_to_deliveries(recorded_artifacts(results_json), run_path, outputs_to)
            )
            if policy.clean:
                if self.settings.backups:
                    backup_existing(run_path)
                    run_path.mkdir(parents=True, exist_ok=True)
                else:
                    run_directory.clear()

            with WorkingDirectory(run_path):
                log.debug("Instantiating flow from %s", flow_class)
                # A copy of each, the flow's own to complete: the design, like the settings, has
                # been hashed already, is recorded beside that hash, and goes on to the flow's
                # dependencies, none of which may see what the flow does to it.
                flow = flow_class(
                    input_settings.model_copy(deep=True),
                    design.model_copy(deep=True),
                    run_path,
                    runner_cwd=runner_cwd,
                    run_directory=run_directory,
                )
            untouched = False
            try:
                flow.design_hash = design_hash
                flow.flow_hash = flowrun_hash
                flow.settings_hash = settings_hash
                flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                # the flow's run time includes init() and the execution of its dependencies
                flow.init_time = time.monotonic()
                try:
                    with WorkingDirectory(run_path):
                        flow.init()
                    assert plan is not None and node is not None
                    producers = self._run_producers(
                        flow, design, node, plan, all_flows_settings, read_leases
                    )
                    # Producers have their own program records. Preparation belongs to this
                    # consumer and happens before its execution recording scope/snapshot.
                    with recording_programs() as programs:
                        flow.prepare_inputs()
                    prepared = registered_input_files(flow)
                    self._read_inputs.add(prepared)
                    _refuse_inputs_inside(flow.run_path, flow.name, prepared)
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    if isinstance(e, DeliveryError) and e.before_run:
                        # a producer's delivery was refused before its tool ran: this flow's
                        # directory still describes its last run, and an earlier producer that
                        # ran meanwhile makes the next launch stale through its new run id. The
                        # flow did not run, and the launch does not list it as launched
                        untouched = True
                        raise
                    # a dependency failed or raised: this flow did not run: its directory no longer vouches for a success
                    remove_trace(run_path)
                    run_directory.remove(results_json)
                    self._record_identity(flow, run_path)
                    flow.results.success = False
                    flow.results["error"] = {"type": type(e).__name__, "message": str(e)}
                    self._report(flow, design, results_json, record=True)
                    raise

                # only now: what the flow consumes includes its producers' outputs and runs
                always = flow.always_runs()
                expected = (
                    None
                    if always is not None
                    else expectation(flow, design, input_settings, self.run_root, producers)
                )
                if expected is None:
                    flow.stale_reason = always
                    log.info("Running %s: %s", flow.name, always)
                elif not self.settings.rebuild_all and not policy.clean:
                    # A second consumer may be acquiring a lease on this same generation.
                    # Reuse must not change its trace or directory through a clock marker.
                    freshness = check_trace(
                        run_path, expected, locate_program, refresh=depender is None
                    )
                    if freshness.fresh and self._reuse_results(flow, results_json):
                        if freshness.refreshed is not None:
                            write_trace(run_path, freshness.refreshed)
                        flow.run_id = freshness.run_id
                        log.info("%s is up to date (%s)", flow.name, run_path)
                        self._report(flow, design, results_json, record=False)
                        self._defer_delivery(flow, delivery, outputs_to)
                        return flow
                    flow.stale_reason = (
                        freshness.reason or f"no successful results in {results_json}"
                    )
                    log.info("Running %s: %s", flow.name, flow.stale_reason)

                previous = previous_trace(run_path, flow.name)
                # xeda's own records never write through a link at their names (`writable`)
                if self.settings.dump_settings_json:
                    run_directory.writable(settings_json)
                # the last run's results are never this run's: gone before this one starts, like
                # the trace, so a run that fails in any way cannot leave them behind
                run_directory.remove(results_json)
                run_directory.writable(results_json)
                # from here until a new trace is written, nothing vouches for this directory
                remove_trace(run_path)
                if previous is not None:
                    # the reports the last run read are not this run's, whatever their mtime
                    # (`Flow.report_file` is a second line of defense)
                    run_directory.remove(*(p for p in previous.reports if run_directory.holds(p)))
                # The run starts now: its inputs are recorded as it finds them, so that a file
                # edited while it runs no longer matches what its trace says it consumed.
                snapshot = (
                    None if expected is None else snapshot_inputs(expected, previous, run_path)
                )
                all_settings = dict(
                    design=design,
                    design_hash=design_hash,
                    rtl_fingerprint=design.rtl_fingerprint,
                    rtl_hash=design.rtl_hash,
                    flow_name=flow.name,
                    flow_settings=input_settings,
                    effective_flow_settings=flow.settings,
                    xeda_version=__version__,
                    flowrun_hash=flowrun_hash,
                    settings_hash=settings_hash,
                    deliveries=[
                        {"setting": d.key, "name": str(d.name), "to": str(d.destination)}
                        for d in deliveries
                    ],
                    outputs_to=outputs_to,
                )
                if self.settings.dump_settings_json:
                    log.info("writing prepared settings to %s", settings_json)
                    dump_json(all_settings, settings_json, backup=self.settings.backups)
                # a tool does not make the directory of a file it is told to write: the
                # conventional names', inside the run directory (relative, never `..`)
                for d in deliveries:
                    (run_path / d.name).parent.mkdir(parents=True, exist_ok=True)
                # never through a link a tool left leading out of the run directory
                _free_working_locations(flow)
                # From now on, what the run directory holds is this run's own only if the run
                # writes it (`Flow.wrote_output`, by identity): a report left from before is a
                # previous run's (`Flow.report_file`).
                flow.start_run()
                try:
                    with recording_programs() as execution_programs:
                        self._execute(flow, run_path, input_settings)
                    for name in execution_programs:
                        if name not in programs:
                            programs.append(name)
                            if name in execution_programs.before:
                                programs.before[name] = execution_programs.before[name]
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    flow.results.success = False
                    flow.results["error"] = {"type": type(e).__name__, "message": str(e)}
                    if flow.init_time is not None:
                        flow.results.runtime = time.monotonic() - flow.init_time
                    for k, v in flow.artifacts.items():
                        flow.results.artifacts.setdefault(k, v)
                    _drop_unwritten_artifacts(flow)
                    self._report(flow, design, results_json, record=True)
                    raise
                if self.settings.dump_settings_json:
                    # `run()` may finish resolving generated files or derived switches. Keep the
                    # pre-run write above so a crash still leaves useful diagnostics, then replace
                    # its effective snapshot after a completed execution without backing up that
                    # transient snapshot.
                    dump_json(all_settings, settings_json, backup=False)
                self._report(flow, design, results_json, record=True)
                if flow.succeeded and expected is not None and snapshot is not None:
                    # Recorded once the run has written everything, `results.json` included, and
                    # before the clean-up, which may remove files the trace names (a depfile).
                    # The clean-up only removes files: the trace is the last thing a run writes.
                    trace = build_trace(
                        expected, flow, programs, snapshot, input_settings, previous
                    )
                    write_trace(run_path, trace)
                    flow.run_id = trace.run_id
                else:
                    # no trace records this run, so nothing can have consumed it before
                    flow.run_id = uuid.uuid4().hex
                if flow.succeeded:
                    self._defer_delivery(flow, delivery, outputs_to)
                if policy.post_cleanup:
                    self._pending_clean_ups.append((flow, settings_json, results_json, policy))
            finally:
                if flow.succeeded and depender is not None:
                    # Only producers need a hand-over token. A requested run may succeed with
                    # unknown output evidence, whose trace correctly requires another run.
                    recorded = previous_trace(run_path, flow.name)
                    token = CompletedRun.capture(
                        run_path,
                        [
                            p
                            for p in artifact_files(flow)
                            if not p.is_relative_to(run_path.resolve())
                        ],
                        recorded.outputs if recorded else None,
                        recorded.outputs_recorded_ns if recorded else None,
                    )
                    self._completed_runs[run_path.resolve()] = (flow, token)
                if not untouched:
                    self.launched.append(flow)
        return flow

    def _defer_delivery(self, flow: Flow, delivery: Deliveries, outputs_to: Optional[Path]) -> None:
        """Note what `flow` delivers -- its named outputs, and its artifacts for `--outputs-to` --
        file by file as its run left them (`Deliveries.collect`, under its run directory's lock).
        The copies are made when the launch has finished (`_finish_launch`), once every flow of
        it has read its inputs."""
        artifacts = [
            flow.results.get("artifacts") or flow.artifacts,
            declared_output_files(flow.results),
        ]
        delivery.collect(flow.run_path, outputs_to_deliveries(artifacts, flow.run_path, outputs_to))
        if not delivery.pending:
            return
        # what only a run reveals -- `--outputs-to`'s artifacts, the files of a directory output --
        # is compared with every copy noted so far, before any copy is made
        refuse_shared_destinations(
            (
                (other.name, named, destination)
                for other, noted in [*self._pending_deliveries, (flow, delivery)]
                for named, _source, destination, _sha in noted.pending
            ),
            before_run=False,
        )
        self._pending_deliveries.append((flow, delivery))

    def _input_settings(
        self,
        flow_class: type[Flow],
        flow_settings: dict[str, Any] | Flow.Settings | None,
        design: Design,
        runner_cwd: Path,
        depender: Flow | None = None,
    ) -> Flow.Settings:
        """Stage 1: the run's input settings, validated in context and owned by the launcher.

        Never modified afterwards: the flow works on a copy of its own (`flow.settings`), which
        its `__init__`/`init()`/`run()` may complete with derived values, resolved paths and
        outputs without any of that leaking into the run's identity.
        """
        try:
            settings = settings_in_context(flow_class, flow_settings, design.root_path, runner_cwd)
        except FlowSettingsError as error:
            if depender is None:
                suggest_dependency_node(flow_class, error)
            raise
        if self.debug:
            log.debug(
                "Flow '%s' settings: %s",
                flow_class.name,
                settings.model_dump_json(exclude_unset=True, indent=2),
            )
            settings.debug = True  # the launcher's `--debug` is part of the input
        # a path the flow writes that leads out of its run directory, a setting the
        # flow cannot run without, and a design it cannot run are reported now, before anything
        # is set up for the run
        check_launchable(flow_class, settings, design)
        return settings

    @staticmethod
    def _suggest_dependency_node(flow_class: type[Flow], error: FlowSettingsError) -> None:
        """Tell a setting that belongs to a dependency where it goes: `-s flows.<node>.key`."""
        suggest_dependency_node(flow_class, error)

    def _run_identity(
        self,
        flow_class: type[Flow],
        flow_name: str,
        design: Design,
        settings: Flow.Settings,
        node: PlanNode,
    ) -> tuple[str, str, Path, str]:
        """Stage 2: `(design_hash, flowrun_hash, run_path, settings_hash)`. The run's hash is
        its node's identity (`bindings.node_identity`): its settings and the ordered origins of
        its inputs. The design
        counts by the parts the flow reads (`Flow.design_parts`), so an edit to a testbench the
        flow does not read changes nothing of it."""
        design_hash = design.parts_hash(flow_class.design_parts)
        settings_hash = flow_run_hash(flow_name, settings, design.name)
        flowrun_hash = node_identity(settings_hash, node.origins)
        # a directory the flow cannot work in is refused on the path alone: nothing is created yet
        # (`get_flow_run_path` creates the run root)
        flow_class.check_run_directory(
            settings,
            self.run_path_of(design.name, node.name, flowrun_hash, target=design.target),
        )
        run_path = self.get_flow_run_path(
            design.name, node.name, flowrun_hash, target=design.target
        )
        return design_hash, flowrun_hash, run_path, settings_hash

    def _run_dir_policy(self) -> RunDirPolicy:
        """What this launch does with its run directory: the launcher's settings, for every
        flow alike, dependencies included. Purging is a clean-up after the run, so
        `post_cleanup_purge` alone makes `post_cleanup` true here -- the one place where the two
        settings meet, which `_clean_up`, the pending clean-ups and the delivery by move follow."""
        return RunDirPolicy(
            clean=self.settings.clean,
            scrub_old_runs=self.settings.scrub_old_runs,
            post_cleanup=self.settings.post_cleanup or self.settings.post_cleanup_purge,
            post_cleanup_purge=self.settings.post_cleanup_purge,
        )

    @staticmethod
    def _reuse_results(flow: Flow, results_json: Path) -> bool:
        """Adopt the recorded results of a fresh run; False if they cannot be read."""
        try:
            previous = Box(json.loads(results_json.read_text()))
        except (OSError, ValueError):
            return False
        if not previous.get("success"):
            return False
        flow.results.update(**previous)
        flow.artifacts = previous.get("artifacts", Box())
        flow.reused = True
        return True

    @contextmanager
    def _producer_read_lease(self, producer: Flow):
        """Verify the completed generation under SH before any consumer can read its files."""
        with ExitStack() as lease:
            try:
                lease.enter_context(run_dir_read_lock(producer.run_path, self.run_root))
            except (OSError, RunDirectoryError) as error:
                raise FlowDependencyFailure(
                    f"Cannot acquire shared read lock for producer {producer.name} "
                    f"at {producer.run_path}: {error}"
                ) from error
            try:
                _completed, token = self._completed_runs[producer.run_path.resolve()]
                token.verify()
            except (KeyError, OSError, ValueError, RunDirectoryError) as error:
                raise FlowDependencyFailure(
                    f"{producer.name} changed before acquiring its read lease: {error}"
                ) from error
            yield producer

    def _run_producers(
        self,
        flow: Flow,
        design: Design,
        node: PlanNode,
        plan: Plan,
        sections: dict[str, Any] | None,
        read_leases: ExitStack,
    ) -> list[Flow]:
        """Materialize exactly the planned inputs; never choose another producer here. The
        producers whose files the flow was handed, each once, in the order they completed."""
        consumed_from: list[Flow] = []
        records = []
        declarations = declared_inputs(type(flow))
        for selected in node.inputs:
            declaration = declarations[selected.name]
            producers: list[Flow] = []
            paths = list(selected.sources)
            # the ordered references of the plan: each producer launched once per plan,
            # its files handed over by the frozen output key, concatenated in reference order
            for reference in selected.references:
                producer_node = plan.node(reference.node)
                key = (id(plan), producer_node.node_key)
                producer = self._planned_completed.get(key)
                if producer is None:
                    try:
                        producer = self.launch_flow(
                            producer_node.flow_class,
                            design,
                            producer_node.settings,
                            depender=flow,
                            all_flows_settings=sections,
                            plan=plan,
                            plan_node=producer_node.node_key,
                        )
                    except Exception as error:
                        if isinstance(error, DeliveryError) and error.before_run:
                            raise  # refused before the producer's tool ran: not its failure
                        raise FlowDependencyFailure(
                            f"dependency {producer_node.name} failed: {error}"
                            + (
                                f"; see {producer_node.run_path / 'results.json'}"
                                if self._entered(producer_node.run_path)
                                else ""
                            )
                        ) from error
                    self._planned_completed[key] = producer
                if not producer.succeeded:
                    raise FlowDependencyFailure(
                        f"dependency {producer.name} failed: {producer.results.get('error', '')}; "
                        f"see {producer.run_path / 'results.json'}"
                    )
                if producer not in consumed_from:
                    read_leases.enter_context(self._producer_read_lease(producer))
                    consumed_from.append(producer)
                accepted = selected_types(type(flow), node.settings, selected.name)
                produced = selected_types(
                    producer_node.flow_class, producer_node.settings, reference.output, output=True
                )
                if not produced or not set(produced) <= set(accepted):
                    raise FlowDependencyFailure(
                        f"{flow.name}.{selected.name}: {producer.name}.{reference.output} "
                        "has no compatible output for the selected target"
                    )
                paths.extend(handed_over(producer, reference.output))
                producers.append(producer)
            producer = producers[0] if producers else None
            bound_producers = tuple(
                BoundProducer(
                    producer=reference.node,
                    output=reference.output,
                    producer_hash=made_by.flow_hash,
                    producer_path=str(made_by.run_path),
                )
                for reference, made_by in zip(selected.references, producers)
            )
            if (declaration.required and not paths) or (
                declaration.cardinality != "many" and len(paths) > 1
            ):
                raise FlowDependencyFailure(
                    f"{flow.name}'s input `{selected.name}` has {len(paths)} paths; "
                    f"expected {declaration.cardinality}"
                )
            for path in paths:
                if not path.is_file():
                    raise FlowDependencyFailure(
                        f"{flow.name}'s input `{selected.name}` is missing: {path}"
                    )
            value = paths if declaration.cardinality == "many" else (paths[0] if paths else None)
            setattr(flow.inputs, selected.name, value)
            records.append(
                DeclaredInputRecord(
                    name=selected.name,
                    origin=selected.origin,
                    producer=selected.producer,
                    output=selected.output,
                    producer_hash=producer.flow_hash if producer else None,
                    producer_path=str(producer.run_path) if producer else None,
                    paths=tuple(map(str, paths)),
                    references=bound_producers,
                    binding_origin=selected.binding_origin,
                    binding_location=selected.binding_location,
                )
            )
        flow.declared_input_records = tuple(records)
        selected_files = declared_input_files(flow)
        self._read_inputs.add(selected_files)
        _refuse_inputs_inside(flow.run_path, flow.name, selected_files)
        return consumed_from

    @staticmethod
    def _record_identity(flow: Flow, run_path: Path) -> None:
        """What every results document says of the run, whether it succeeded or not."""
        flow.results["design"] = flow.design.name
        flow.results["design_hash"] = flow.design_hash
        flow.results["flow"] = flow.name
        flow.results["flow_hash"] = flow.flow_hash
        flow.results["settings_hash"] = flow.settings_hash
        flow.results["run_path"] = run_path.absolute()
        flow.results.timestamp = flow.timestamp

    def _execute(self, flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
        """Stage 6: `run()`, `parse_reports()`, and the run's results, artifacts included."""
        self._record_identity(flow, run_path)

        success = True

        with WorkingDirectory(run_path):
            if flow.settings.reports_dir:
                flow.settings.reports_dir.mkdir(exist_ok=True, parents=True)
            try:
                flow.run()
            except NonZeroExitCode as e:
                if isinstance(e, ProcessTimeout):
                    log.error("%s", e)
                else:
                    log.error(
                        "Execution of '%s' returned %d",
                        (
                            " ".join(e.command_args)
                            if isinstance(e.command_args, (list, tuple))
                            else e.command_args
                        ),
                        e.exit_code,
                    )
                flow.results["error"] = {"type": type(e).__name__, "message": str(e)}
                success = False
            if flow.init_time is not None:
                flow.results.runtime = time.monotonic() - flow.init_time
            try:
                success &= flow.parse_reports()
                success &= flow.check_results()
            except Exception as e:  # pylint: disable=broad-except
                log.critical("parse_reports or check_results threw an exception: %s", e)
                if success:  # if so far so good this is a bug!
                    raise e
            flow.add_canonical_result_aliases()
            if success:
                # Consumers use these integrity records for fresh and reused producers alike.
                problems = record_outputs(flow)
                for problem in problems:
                    log.error("%s", problem)
                if problems:
                    success = False
                    flow.results["error"] = {
                        "type": "MissingOutput",
                        "message": "; ".join(problems),
                    }
            if not success and not input_settings.is_quiet:
                log.debug("Failure was reported in the parsed results.")
            if not success and not flow.results.get("error"):
                # no exception and no exit status: the reports or checks said so; a failure
                # document always names its error (and a depender quotes it). This is a node's
                # cause, `ReportedFailure`; `FlowFailed` is the verdict the top-level `--json`
                # document of `xeda run` gives whatever the cause, so the names must differ.
                flow.results["error"] = {
                    "type": "ReportedFailure",
                    "message": (
                        f"`{flow.name}` reported failure: its reports or checks did not pass "
                        "(see the log and its run directory)"
                    ),
                }
            flow.results.success = success
        for k, v in flow.artifacts.items():
            if not flow.results.artifacts.get(k):
                flow.results.artifacts[k] = v
        if not flow.succeeded:
            _drop_unwritten_artifacts(flow)

    def _report(self, flow: Flow, design: Design, results_json: Path, record: bool) -> None:
        """Stage 7: show the results and, for a run that just executed (`record`), write them to
        `results.json`. A reused run's directory is left exactly as it was."""
        if self.settings.display_results and flow.artifacts and flow.succeeded:
            table = Table(
                box=box.SIMPLE,
                show_header=True,
                show_edge=False,
                show_footer=True,
                collapse_padding=True,
                pad_edge=False,
            )
            table.add_column(
                "Artifacts:", justify="left", style="cyan", header_style="blue", no_wrap=False
            )
            table.add_column("", justify="left", style="green", no_wrap=False)

            for label, artifact, end_section in _artifact_rows(flow.artifacts):
                table.add_row(label, artifact, end_section=end_section)

            console.print("")
            console.print(table)
            console.print("")

        if record and self.settings.dump_results_json:
            dump_json(flow.results, results_json, backup=self.settings.backups)
            log.info("Results written to %s", results_json)

        if self.settings.display_results:
            print_results(
                flow,
                title=f"Results of flow:{flow.name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

    def _clean_up(
        self, flow: Flow, settings_json: Path, results_json: Path, policy: RunDirPolicy
    ) -> None:
        """After a run: purge its directory, or prune it to what the run reports and records
        (`settings.json`, `results.json`, artifacts), as `policy` says.

        Pruning removes the trace first: until every flow declares every output it writes, a
        pruned directory may lack a file its depender reads, so the run must not be reused --
        not even if the pruning is interrupted halfway."""
        if policy.post_cleanup:
            remove_trace(flow.run_path)
            if policy.purges:
                log.warning("Deleting flow run path %s", flow.run_path)
                # with every name of it, the link the launch named it by included, so none is
                # left leading nowhere
                flow.run_directory.delete(
                    *run_directory_names(flow.run_path, flow.name, self.run_root)
                )
            else:
                log.warning("Cleaning up %s", flow.run_path)
                named_run_path = Path(os.path.abspath(flow.run_path))
                run_path = flow.run_path.resolve()
                kept: set[Path] = set()
                for raw_path in (
                    settings_json,
                    results_json,
                    *iter_artifact_paths(flow.results.artifacts),
                    *declared_output_files(flow.results),
                ):
                    path = Path(os.path.abspath(named_run_path / raw_path))
                    if path.is_relative_to(named_run_path):
                        # Resolve the run-directory alias without resolving an artifact's
                        # own symlink: both that link and its internal target must survive.
                        path = run_path / path.relative_to(named_run_path)
                    target = path.resolve()
                    if path.is_relative_to(run_path):
                        kept.add(path)
                        # A retained symlink to an internal file needs its target as well.
                        if target.is_relative_to(run_path):
                            kept.add(target)
                    elif target.is_relative_to(run_path):
                        # Absolute paths may spell the run path through a symlink.
                        kept.add(target)

                ancestors = {
                    parent for path in kept for parent in path.parents if parent != run_path
                }
                removed: list[Path] = []

                def prune(directory: Path) -> None:
                    for path in directory.iterdir():
                        if path in kept:
                            continue
                        if path in ancestors and path.is_symlink():
                            continue
                        if path in ancestors and path.is_dir() and not path.is_symlink():
                            prune(path)
                        else:
                            removed.extend(flow.run_directory.remove(path))

                if run_path.is_relative_to(self.run_root) and run_path not in kept:
                    prune(run_path)
                log.warning("Removed the following files: %s", " ".join(str(p) for p in removed))

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None] = None,
        *,
        depender: Optional[Flow] = None,
        all_flows_settings: Union[Dict, None] = None,
        plan: Plan | None = None,
    ) -> Optional[Flow]:
        """Launch `flow_class` on `design`, which the caller has built. Its settings come from the
        layers `run()` composes, lowest first: the design's own `flows` sections, then
        `all_flows_settings` (more `flows` sections, composed into `flow_settings` by
        `compose_flow_settings`), then `flow_settings`; a `Flow.Settings` instance is taken as
        final. `run()` and `plan()` also read a project file. A built design names none, so hand
        its sections in as `all_flows_settings`."""
        if plan is None and depender is None:
            flow_cls = get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class
            sections, section_bindings = split_bindings(
                all_flows_settings or {}, location=SUPPLIED_SECTIONS_ORIGIN
            )
            own, own_bindings = split_bindings(
                {flow_cls.name: flow_settings or {}}, location=API_ORIGIN, kind="api"
            )
            layers = (section_bindings, own_bindings)
            # a flow that declares no inputs has none to bind: an `inputs` key is an error
            effective_bindings(layers, default_nodes([flow_cls]))
            all_flows_settings = sections
            if not isinstance(flow_settings, Flow.Settings):
                flow_settings = own[flow_cls.name]
            if any(layer.entries or layer.invalid_inputs for layer in layers):
                plan = self.resolve(
                    flow_cls, design, flow_settings, sections, binding_layers=layers
                )
        if (
            plan is None
            and all_flows_settings
            and depender is None
            and not isinstance(flow_settings, Flow.Settings)
        ):
            flow_cls = get_flow_class(flow_class) if isinstance(flow_class, str) else flow_class
            sections = merge_flow_sections(
                all_flows_settings, flow_class_for=_get_flow_class_if_known
            )
            flow_settings = compose_flow_settings(flow_cls, [sections], flow_settings or {})
            all_flows_settings = sections
        return self.launch_flow(
            flow_class,
            design,
            flow_settings,
            depender=depender,
            all_flows_settings=all_flows_settings,
            plan=plan,
        )

    @staticmethod
    def _design_from_project(
        xeda_project: XedaProject | None,
        xedaproject: str,
        name: Any,
        select_design_in_project=None,
        target: str | None = None,
    ) -> Design:
        """The design called `name` in the project, or its only design (or the one the user
        selects) when no name is given. A `DesignNotFoundError` saying why there is none."""
        if not xeda_project:
            given = f"'{name}' is not a design file, and" if name else "No design was given, and"
            raise DesignNotFoundError(
                f"{given} there is no project file ({', '.join(PROJECT_FILE_NAMES)}) in "
                f"{Path.cwd()} to take a design from. Give a design file (.yaml, .yml, .toml "
                "or .json)."
            )
        names = xeda_project.design_names
        if not xeda_project.designs:
            raise DesignNotFoundError(
                f'The project file "{xedaproject}" has no designs. Give a design file instead.'
            )
        log.info("Available designs in xedaproject: %s", ", ".join(names))
        selected: Design | None = None
        if name:
            selected = xeda_project.get_design(str(name), target)
            if selected is None:
                raise DesignNotFoundError(
                    f"Design '{name}' is not in the project file \"{xedaproject}\". "
                    f"Its designs are: {', '.join(names)}."
                )
        elif len(xeda_project.designs) == 1:
            selected = xeda_project.get_design(target=target)
        elif select_design_in_project:
            selected = select_design_in_project(xeda_project, name, target)
        if selected is None:
            raise DesignNotFoundError(
                f'No design was given or selected among those of "{xedaproject}": '
                f"{', '.join(names)}."
            )
        return selected

    def _refuse_unaccepted_request(
        self,
        flow: Union[Type[Flow], str, FlowRequest],
        flow_settings: Any,
        flow_overrides: Any,
        xedaproject: str | Path | None,
    ) -> None:
        """Refuse, before anything is loaded, what a launcher that takes no requests (`Dse`,
        `RemoteRunner.run_remote`) refuses and that needs no design: a chain, and an input
        binding of the requested node given on the command line, through the API or in the
        project file (`LOCAL_REQUESTS_ONLY`). Loading a design may clone a git dependency into
        the run root, so a refusal that came after it would leave the clone behind.

        What does need the loaded design, and is refused later with the same error: a binding of
        the requested node saved in the design file, and a binding of any other node (whether the
        request reaches that node depends on its resolved graph, so an unrelated saved binding is
        no refusal). The requested node itself is reached by every request.
        `plan` and `run_flow` do not call this: `plan` refuses side-effecting loading outright
        (`refusing_load_side_effects`) and `run_flow` takes a design that is already built."""
        if isinstance(flow, FlowRequest):
            if len(flow.elements) > 1:
                raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
            requested = flow.requested
        elif isinstance(flow, str):
            if "+" in flow:
                raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
            requested = parse_request(flow).requested
        else:
            requested = flow
        from .resolver import _explicit

        # the layers a request reads bindings from, taken out as `_request` takes them
        explicit = (
            _explicit(flow_settings)
            if isinstance(flow_settings, Flow.Settings)
            else settings_to_dict(flow_settings)
        )
        cli_sections, cli_own = split_flow_sections(
            explicit, requested.name, flow_class_for=_get_flow_class_if_known
        )
        cli_sections = merge_flow_sections(
            cli_sections, {requested.name: cli_own}, flow_class_for=_get_flow_class_if_known
        )
        layers = [
            split_bindings(cli_sections, location=COMMAND_LINE_ORIGIN, kind="cli")[1],
            split_bindings(
                {
                    requested.name: (
                        flow_overrides
                        if isinstance(flow_overrides, dict)
                        else settings_to_dict(flow_overrides)
                    )
                },
                location=API_ORIGIN,
                kind="api",
            )[1],
        ]
        project_path = resolve_project_file(xedaproject)
        if project_path is not None:
            try:
                project = XedaProject.from_file(project_path, skip_designs=True)
            except (OSError, ValueError, yaml.YAMLError):
                project = None  # `_request` reports it, before it loads any design
            if project is not None:
                layers.append(
                    split_bindings(project.flows, location=f'the project file "{project_path}"')[1]
                )
        require_no_bindings(layers, [NodeKey(requested.name)])

    def run(
        self,
        flow: Union[Type[Flow], str],
        design: Union[str, Path, Design, Dict[str, Any], None] = None,
        xedaproject: Optional[str] = None,
        flow_settings: Union[  # the command line's `-s`: overrides the design and project
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
            Flow.Settings,
        ] = [],
        flow_overrides: Union[
            List[str],
            Tuple[str, ...],
            Mapping[str, Any],
        ] = [],
        select_design_in_project=None,
        design_overrides: Union[Iterable[str], Dict[str, Any], None] = None,
        design_allow_extra: bool = False,
        design_remove_fields: List[str] = [],
        target: str | None = None,
    ) -> Optional[Flow]:
        """Load and compose a request, then execute its resolved declared graph. `target`
        selects one of the design's `targets`, as `Design.from_file` does."""
        # Where requests are not taken (`Dse`), refuse before loading what needs no design.
        # What does need it is listed there, and refused below, once the request is resolved.
        if not self.accepts_bindings:
            self._refuse_unaccepted_request(flow, flow_settings, flow_overrides, xedaproject)
        request = self._request(
            flow,
            design,
            xedaproject,
            flow_settings,
            flow_overrides,
            select_design_in_project,
            design_overrides,
            design_allow_extra,
            design_remove_fields,
            target=target,
        )
        plan = self._resolve_request(request)
        if not self.accepts_bindings and (
            len(request.flow_request.elements) > 1
            or any(i.binding_origin for n in plan.nodes for i in n.inputs)
        ):
            raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
        #: the plan this run follows, for reporting
        self.last_plan = plan
        previous, self._request_context = self._request_context, request
        try:
            return self.run_flow(
                request.flow_class,
                request.design,
                request.settings,
                all_flows_settings=request.sections,
                plan=plan,
            )
        finally:
            self._request_context = previous

    def plan(
        self,
        flow: type[Flow] | str,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any] = [],
        select_design_in_project=None,
        design_overrides: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        design_remove_fields: list[str] = [],
        target: str | None = None,
    ) -> Plan:
        """Plan what run() would execute, refusing side-effecting design loading, and what
        run() would refuse before anything runs (a run directory a flow refuses, `--outputs-to`
        a programmer)."""
        plan = self._resolve_request(
            self._request(
                flow,
                design,
                xedaproject,
                flow_settings,
                flow_overrides,
                select_design_in_project,
                design_overrides,
                design_allow_extra,
                design_remove_fields,
                target=target,
                _planning=True,
            )
        )
        # the launch refuses these too, where it decides the directory (`_run_identity`)
        for node in plan.nodes:
            node.flow_class.check_run_directory(node.settings, node.run_path)
        if self.settings.outputs_to is not None:
            _refuse_outputs_to_a_programmer(
                plan, plan.node(plan.requested), self.settings.outputs_to
            )
        return plan

    def _request(
        self,
        flow: type[Flow] | str | FlowRequest,
        design: str | Path | Design | dict[str, Any] | None = None,
        xedaproject: str | None = None,
        flow_settings: list[str] | tuple[str, ...] | Mapping[str, Any] | Flow.Settings = [],
        flow_overrides: list[str] | tuple[str, ...] | Mapping[str, Any] = [],
        select_design_in_project=None,
        design_overrides: Iterable[str] | dict[str, Any] | None = None,
        design_allow_extra: bool = False,
        design_remove_fields: list[str] = [],
        *,
        target: str | None = None,
        _planning: bool = False,
    ) -> _Request:
        """
        Flexible API for launching flows.
        """
        # nothing is loaded yet: a request that fails before its design loads reports none
        self.target = None
        self.design_name = None
        # The request is read from the flow alone, before anything is loaded: a design with a git
        # dependency is cloned into the run root as it loads, and the command line parses the
        # request first too.
        if isinstance(flow, FlowRequest):
            flow_request = flow
            flow_class = flow.requested
        elif isinstance(flow, str):
            # one flow name, or a chain `a+b.out+c`: the same parser as the command line's
            flow_request = parse_request(flow)
            flow_class = flow_request.requested
        else:
            flow_class = flow
            flow_request = FlowRequest((ChainElement(flow_class),))
        # get default flow configs from xedaproject even if a design-file is specified
        xeda_project = None
        flows_settings: Dict[str, Any] = {}
        if not design_overrides:
            design_overrides = {}
        if not isinstance(design_overrides, dict):
            design_overrides = list(design_overrides)
            design_overrides = settings_to_dict(design_overrides)
        design_not_in_project = False
        project_path = resolve_project_file(xedaproject)
        # the name messages use when there is no project file
        xedaproject = str(project_path) if project_path is not None else PROJECT_FILE_NAMES[0]
        given_file = (
            Path(design)
            if isinstance(design, (str, Path)) and names_a_design_file(design)
            else None
        )
        if design is not None:
            if isinstance(design, (Design, dict, Path)):
                design_not_in_project = True
            elif names_a_design_file(design):
                # a design file, whether or not it exists: `Design.from_file` says what is wrong
                # with it, rather than a project reporting a design of that name missing
                design_not_in_project = True
                design = Path(design)
        # A git dependency without a directory of its own is cloned into the run root, and a
        # generator's record is kept there; both ask for it only then, so a design that fails to
        # load leaves no run root behind. `--rebuild-all`/`--clean` regenerates too.
        with (
            loading_in_run_root(
                self.load_run_root, self.settings.rebuild_all or self.settings.clean
            ),
            refusing_load_side_effects() if _planning else nullcontext(),
        ):
            if Path(xedaproject).exists():
                try:
                    xeda_project = XedaProject.from_file(
                        xedaproject,
                        skip_designs=design_not_in_project,
                        design_overrides=design_overrides,
                        design_allow_extra=design_allow_extra,
                        design_remove_extra=design_remove_fields,
                    )
                except (OSError, ValueError, yaml.YAMLError) as e:
                    # unreadable, not TOML/JSON/YAML (the decode errors are `ValueError`s), or not
                    # a project's contents
                    raise ProjectFileError(
                        f'Cannot load project file "{Path(xedaproject).absolute()}": {e}'
                    ) from e
                flows_settings = xeda_project.flows
            if design and design_not_in_project:
                if isinstance(design, (str, Path)):
                    # An invalid design file raises, as a design from a project does: the caller
                    # reports it (`xeda run --json` names the error instead of "did not complete").
                    design = Design.from_file(
                        design,
                        overrides=design_overrides,
                        allow_extra=design_allow_extra,
                        remove_extra=design_remove_fields,
                        target=target,
                    )

                elif isinstance(design, dict):
                    design = Design.target_selected(design, target, design_overrides)
                    if "design_root" not in design:
                        design["design_root"] = Path.cwd()
                    design = Design(**design)
                elif target is not None:
                    raise ValueError(
                        f"target {target!r} was asked for, but the design is already built: "
                        "select the target where the design is loaded (`Design.from_file(..., "
                        "target=...)`)"
                    )
            else:
                design = self._design_from_project(
                    xeda_project, xedaproject, design, select_design_in_project, target
                )
        # Recover supplied contributions without treating model-created nested defaults as
        # explicit producer settings. Actual nested edits still count.
        from .resolver import _explicit

        settings_instance = isinstance(flow_settings, Flow.Settings)
        self.target = design.target if isinstance(design, Design) else None
        self.design_name = design.name if isinstance(design, Design) else None
        if isinstance(flow_settings, Flow.Settings):
            explicit_flow_settings = _explicit(flow_settings)
            flow_settings = flow_settings.model_dump()
        else:
            assert isinstance(
                flow_settings, (list, tuple, Mapping)
            ), "flow_settings should be a list, tuple or dict"
            flow_settings = settings_to_dict(flow_settings)
            explicit_flow_settings = flow_settings
        if not isinstance(flow_overrides, dict):
            flow_overrides = settings_to_dict(flow_overrides)
        assert isinstance(
            flow_settings, dict
        ), f"flow_settings should be a dict at this stage, but was {type(flow_settings)}"
        assert isinstance(
            flow_overrides, dict
        ), f"flow_overrides should be a dict at this stage, but was {type(flow_overrides)}"
        if not design or not flow_class:
            log.critical("Failed to parse design and/or flow")
            raise ValueError(f"design={design} flow_class={flow_class}")
        assert isinstance(
            design, Design
        ), f"BUG: design should be of type Design but was {type(design)}"

        # Canonicalize flow-section aliases and merge an embedded/project-selected design's own
        # settings too. Previously only an explicitly supplied design file reached this merge;
        # `[design.flows.*]` inside xedaproject.toml was silently ignored.
        # One origin per file, each normalized on its own: origin decides first, nesting only
        # within one origin (`compose_flow_settings`).
        project_label = str(Path(xedaproject).absolute())
        design_label = str(given_file.absolute()) if given_file else f"the design {design.name}"
        if xeda_project is not None and not design_not_in_project:
            design_label = f"{project_label}: design {design.name}"
        project_sections, project_bindings = split_bindings(flows_settings, location=project_label)
        design_sections, design_bindings = split_bindings(design.flow, location=design_label)
        # the command line's `-s flows.<node>.key` is the third origin; it may only name a flow
        # of this run, and `-s key` is the requested flow's own leaf
        cli_sections, cli_own = split_flow_sections(
            explicit_flow_settings, flow_class.name, flow_class_for=_get_flow_class_if_known
        )
        cli_sections = merge_flow_sections(
            cli_sections, {flow_class.name: cli_own}, flow_class_for=_get_flow_class_if_known
        )
        cli_sections, cli_bindings = split_bindings(
            cli_sections, location=COMMAND_LINE_ORIGIN, kind="cli"
        )
        api_sections, api_bindings = split_bindings(
            {flow_class.name: flow_overrides}, location=API_ORIGIN, kind="api"
        )
        binding_layers = (project_bindings, design_bindings, cli_bindings, api_bindings)
        effective_bindings(binding_layers, default_nodes([flow_class]), request=flow_request)
        # Ordinary calls keep the early CLI addressing check. A bound request is checked by
        # the resolver against its graph: an alternate producer can be outside the default one.
        if (
            not any(layer.entries or layer.invalid_inputs for layer in binding_layers)
            and len(flow_request.elements) == 1
        ):
            command_line_sections(
                explicit_flow_settings, flow_class, flow_class_for=_get_flow_class_if_known
            )
        origins = [project_sections, design_sections, cli_sections, api_sections]
        all_sections = merge_flow_sections(*origins, flow_class_for=_get_flow_class_if_known)
        # `-s` wins over the design and project files, as documented; see `settings_layers`.
        final_flow_settings = compose_flow_settings(flow_class, origins)
        if settings_instance:
            # Preserve the final-value model API while the captured CLI origin records only
            # supplied contributions for nested-default diagnostics and shared agreement.
            final_flow_settings = compose_flow_settings(
                flow_class, origins[:2], flow_settings, api_sections[flow_class.name]
            )
        if self.settings.debug:
            log.info("design: %s" % PrettyPrinter().pformat(design.model_dump()))
        # the files this launch was given: never an output's destination
        self._launch_inputs = [
            path.resolve()
            for path in (given_file, Path(xedaproject))
            if path is not None and path.is_file()
        ]
        return _Request(
            flow_class,
            design,
            deepcopy(final_flow_settings),
            deepcopy(all_sections),
            tuple(
                (label, deepcopy(section))
                for label, section in (
                    (project_label, project_sections),
                    (design_label, design_sections),
                )
            ),
            deepcopy(cli_sections),
            deepcopy(api_sections),
            flow_request,
            binding_layers,
        )


class FlowRunner(FlowLauncher):
    """alias for FlowLauncher"""


class DefaultRunner(FlowRunner):
    """Executes a flow and its dependencies and then reports selected results"""


def add_file_logger(logdir: Union[Path, str], timestamp: Union[str, datetime, None] = None):
    if timestamp is None:
        timestamp = datetime.now()
    if not isinstance(timestamp, str):
        timestamp = timestamp.strftime("%Y-%m-%d-%H%M%S%f")[:-3]
    if not isinstance(logdir, Path):
        logdir = Path(logdir)
    logdir.mkdir(exist_ok=True, parents=True)
    logfile = logdir / f"xeda_{timestamp}.log"
    log.info("Logging to %s", logfile)
    fileHandler = logging.FileHandler(logfile)
    logFormatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
    )
    fileHandler.setFormatter(logFormatter)
    log.root.addHandler(fileHandler)


class XedaOptions(XedaBaseModel):
    verbose: bool = False
    quiet: bool = False
    debug: bool = False
    detailed_logs: bool = True
