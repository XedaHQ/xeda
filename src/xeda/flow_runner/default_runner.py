"""Launch execution of flows"""

from __future__ import annotations

import difflib
import importlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from pprint import PrettyPrinter
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

import yaml
from box import Box
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..artifacts import drop_unwritten_artifacts, iter_artifact_paths
from ..console import console
from ..dataclass import WORKING_ROLE, XedaBaseModel, model_validator
from ..deliver import (
    OUTPUTS_TO,
    Conflict,
    Deliveries,
    ReadInputs,
    deliverable_setting_names,
    outputs_to_deliveries,
    recorded_artifacts,
    split_deliveries,
)
from ..design import (
    DESIGN_NAME,
    Design,
    cloning_dependencies_into,
    names_a_design_file,
    refusing_load_side_effects,
)
from ..flow import Flow, FlowDependencyFailure, FlowFatalError, FlowSettingsError, registered_flows
from ..flow.io import declared_inputs, is_declared, selected_types
from ..flow import flowrun_hash as flow_run_hash
from ..flow.flow import WrittenLeaf, map_written_leaves
from ..proc_utils import ProcessTimeout, recording_programs
from ..run_dir import RunDirectory, RunDirectoryError
from ..run_root import DEFAULT_RUN_ROOT, ensure_run_root
from ..tool import NonZeroExitCode
from ..utils import (
    WorkingDirectory,
    XedaException,
    backup_existing,
    dump_json,
    json_encodable,
    replacing_copy,
    semantic_hash,
    settings_to_dict,
    snakecase_to_camelcase,
    unique,
    with_json_keys,
)
from ..version import __version__
from ..xedaproject import PROJECT_FILE_NAMES, ProjectFileError, XedaProject, resolve_project_file
from .outputs import declared_output_files, handed_over, record_outputs
from .resolver import Plan, PlanNode, check_launchable, resolve as resolve_plan
from .run_lock import CompletedRun, run_dir_lock, run_dir_read_lock
from .settings_layers import (
    command_line_sections,
    compose_flow_settings,
    dependency_settings,
    merge_flow_sections,
    settings_in_context,
    suggest_dependency_node,
    transitive_dependencies,
)
from .trace import (
    as_recorded,
    DeclaredInputRecord,
    check_trace,
    locate_program,
    previous_trace,
    remove_trace,
    settings_difference,
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

DIR_NAME_HASH_LEN = 16


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


class FlowNotFoundError(XedaException):
    def __init__(self, flow_name: Optional[str] = None, suggestions: Iterable[str] = ()) -> None:
        self.flow_name = flow_name
        self.suggestions = list(suggestions)
        msg = f"Flow '{flow_name}' was not found." if flow_name else "Flow was not found."
        if self.suggestions:
            msg += " Did you mean: " + ", ".join(self.suggestions) + "?"
        msg += " Run `xeda list-flows` to see all available flows."
        super().__init__(msg)


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


def _flow_name_suggestions(flow_name: str, limit: int = 3) -> List[str]:
    """Canonical names of registered flows most similar to `flow_name`."""
    canonical = unique([cls.name for _mod, cls in registered_flows.values()])
    return difflib.get_close_matches(flow_name, canonical, n=limit, cutoff=0.6)


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__ or "xeda"
) -> Type[Flow]:
    flow_name = flow_name.strip().replace("-", "_")
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


def scrub_runs(
    flow_name: str,
    dir: Path,
    exclude: Sequence[Path] = (),
    run_root: Optional[Path] = None,
) -> bool:
    """Find and (with confirmation) remove `flow_name`'s run directories under `dir`.

    A run directory of the flow is named `flow_name`, optionally followed by an underscore and
    a `DIR_NAME_HASH_LEN`-char `[a-z0-9]` `flowrun_hash` (the unhashed form is what every default
    run creates; the hashed form only appears with `hashed_run_dirs=True`). Matched against
    `dir`'s children rather than a `f"{flow_name}_*"` glob, so the unhashed directory -- which
    that glob can never match -- is included too. Each is removed as a run directory claimed
    under `run_root` (default: `dir`'s parent), the real run root, so none that leads out of it
    is ever removed.
    """
    if run_root is None:
        run_root = dir.parent
    regex = re.compile(f"^{re.escape(flow_name)}(_" + (r"[a-z0-9]" * DIR_NAME_HASH_LEN) + r")?$")
    xr = dir.resolve()
    if not dir.exists() or not xr.is_dir():
        return False
    dirs_to_rm = unique(
        [
            p
            for p in dir.iterdir()
            if p.is_dir()
            and regex.match(p.name)
            and all(not ex.exists() or not p.samefile(ex) for ex in exclude)
            and xr in p.resolve().parents
            and RunDirectory.lies_under(p, run_root)
        ]
    )
    if dirs_to_rm:
        console.print(
            f"[red]This will action will remove all of the following {len(dirs_to_rm)} subfolders:[/red]"
        )
        for p in dirs_to_rm:
            console.print(p)
        confirmation = console.input("Type 'yes' if you're sure you want to continue: ")
        if confirmation.lower() == "yes":
            log.warning(
                "Removing the following directories: %s", " ".join(str(p) for p in dirs_to_rm)
            )
            for p in dirs_to_rm:
                with run_dir_lock(p):
                    RunDirectory.claimed(p, run_root).delete()
                    if p.is_symlink():  # a run directory reached through a link in the run root
                        p.unlink()  # the link itself, whose target, xeda's, is gone
            console.print(f"{len(dirs_to_rm)} folders removed.")
            return True
        else:
            console.print("Not confirmed. No files or folders were removed.")
    return False


def _refuse_inputs_inside(run_path: Path, flow_name: str, files: Iterable[Path]) -> None:
    """Rule R5: xeda empties and rewrites a flow's run directory, so no file of the design, and no
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
        "--outputs-to copies; give one a location for something to deliver",
        outputs_to,
        flow.name,
        which,
    )


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
        # remove flow_run folder and all of its contents _after_ running the flow:
        post_cleanup_purge: bool = False
        # remove previous flow directories _before_ running the flow:
        scrub_old_runs: bool = False
        #: copy the requested flow's artifacts into this directory once it succeeded (D21)
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
        #: in the current launch, each run directory's configuration: its `flowrun_hash`, which
        #: flow asked for it, and its input settings `as_recorded`
        self._claims: Dict[Path, Tuple[str, str, Any]] = {}
        #: asked, at an interactive terminal, whether to replace files in the way of named
        #: outputs (`xeda.deliver.Deliveries.check`); None: only `overwrite_outputs` counts
        self.confirm_overwrite: Optional[Callable[[Sequence[Conflict]], bool]] = None
        #: the design and project file `run()` was given: never an output's destination
        self._launch_inputs: List[Path] = []
        #: every file the flows of the current launch read (`xeda.deliver.ReadInputs`)
        self._read_inputs = ReadInputs()
        #: the deliveries of the current launch's flows, made when it has finished
        self._pending_deliveries: List[Tuple[Flow, Deliveries]] = []
        self._request_context: _Request | None = None
        self._plans: dict[int, tuple[Plan, Any, Any]] = {}
        self._planned_completed: dict[tuple[int, str], Flow] = {}
        self._completed_runs: dict[Path, tuple[Flow, CompletedRun]] = {}

    @property
    def run_root(self) -> Path:
        """The run root (`xeda.run_root`), resolved: created and marked when first used."""
        if not self._run_root_ready:
            root = ensure_run_root(self._run_root, start=self._start)
            assert root is not None  # created when absent
            self._run_root, self._run_root_ready = root, True
        return self._run_root

    @property
    def xeda_run_dir(self) -> Path:
        """Removed: the run root is `run_root`."""
        raise AttributeError("`xeda_run_dir` was removed: use run_root")

    def run_path_of(self, design_name: str, node_name: str, identity: Optional[str] = None) -> Path:
        """`<run root>/<design>/<node>`, or `<node>_<identity>` with hashed run directories:
        strictly inside the run root, resolved -- a `RunDirectoryError` otherwise, before anything
        (the lock beside it included) is written."""
        subdir = node_name
        if self.settings.hashed_run_dirs and identity:
            subdir += f"_{identity[:DIR_NAME_HASH_LEN]}"
        if not DESIGN_NAME.fullmatch(design_name):
            raise RunDirectoryError(f"{design_name!r} is not a design name")
        ensure_run_root(self._run_root, start=self._start, create=False)
        run_path = self._run_root / design_name / subdir
        if not RunDirectory.lies_under(run_path, self._run_root):
            raise RunDirectoryError(
                f"{run_path} leads out of the run root {self._run_root} (through a symbolic "
                "link): xeda runs only inside its run root; remove the link"
            )
        return run_path

    def get_flow_run_path(
        self, design_name: str, node_name: str, identity: str | None = None
    ) -> Path:
        """Create/mark the root, then revalidate the execution path at operation time."""
        self.run_root
        return self.run_path_of(design_name, node_name, identity)

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
    ) -> Plan:
        """Resolve one request without constructing a flow, probing tools or writing files."""
        plan = resolve_plan(
            flow_class,
            design,
            flow_settings,
            all_flows_settings,
            runner_cwd=Path.cwd(),
            run_root=self._run_root,
            hashed_run_dirs=self.settings.hashed_run_dirs,
            run_path=self.run_path_of,
            origins=origins,
            command_line=command_line,
            api_overrides=api_overrides,
            debug=self.settings.debug,
        )
        # The resolver validates final agreed settings. The original request may contain
        # partial shared values that become valid only along a declared edge.
        self._plans[id(plan)] = (
            plan,
            as_recorded(flow_settings or {}),
            as_recorded(all_flows_settings or {}),
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
        )

    def _validate_plan(
        self,
        plan: Plan,
        flow_class: type[Flow],
        design: Design,
        settings: Mapping[str, Any] | Flow.Settings | None,
        sections: Mapping[str, Any] | None,
    ) -> PlanNode:
        """Validate a plan minted by this launcher; external plans are deferred (R3)."""
        captured = self._plans.get(id(plan))
        context = plan.context
        design_hash = semantic_hash(dict(rtl_hash=design.rtl_hash, tb_hash=design.tb_hash))
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
            )
        ):
            raise FlowFatalError("The plan does not match this request's context")
        if flow_class.name not in plan:
            raise FlowFatalError(f"The plan has no node for this request: {flow_class.name}")
        node = plan.node(flow_class.name)
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
        if node.flowrun_hash != flow_run_hash(node.name, node.settings, design.name) or (
            node.run_path != self.run_path_of(design.name, node.name, node.flowrun_hash)
        ):
            raise FlowFatalError("The plan does not match this request's identity or path")
        return node

    def launch_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        all_flows_settings: Union[Dict, None] = None,
        *,
        plan: Plan | None = None,
    ) -> Flow:
        """Launch `flow_class` on `design`: the one procedure every flow run, and every
        dependency run, goes through -- make's order: bring every prerequisite up to date, then
        judge this flow against them. Its stages, in order:

        1. **input** (`_input_settings`): validate the settings in their context (design root,
           start directory) and apply the launcher's ``--debug``. The result is the run's input,
           never modified afterwards. A missing required setting, or a design the flow cannot
           run, fails the launch here.
        2. **identity** (`_run_identity`): the design's hash and the settings' `flowrun_hash`;
           the run directory, `<design>/<flow>` (``hashed_run_dirs``: `<flow>_<flowrun_hash>`),
           locked from here until the run's trace is written (`run_lock`). With ``clean``, the
           directory is emptied now (or backed up, with ``backups``).
        3. **prepare**: construct the flow with its own copy of the input and call its `init()`,
           which registers the flow's dependencies. `init()` runs for a flow that turns out to be
           fresh too, so it must not change a file in the run directory: every file there is an
           output of the last run.
        4. **dependencies** (`_run_dependencies`): launch each through this same procedure, in a
           sibling run directory, with settings composed by `dependency_settings`.
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

        Within one launch, a run directory holds one configuration: a second launch into it with
        other settings is a `FlowSettingsError`, since it would overwrite what the first one
        produced; with the same settings, the second reuses the first's run. Every flow launched
        is appended to `launched` as it completes, whether it succeeded, failed or raised.

        Outputs the user named (D21, `xeda.deliver`) are checked before any tool of their flow
        runs, noted when it succeeded or was found up to date, and delivered once the whole
        launch has finished -- only when the requested flow succeeded or was found up to date: a
        launch that raised, or whose requested flow reports failure, delivers nothing, not even
        a successful dependency's outputs (`_finish_launch`).
        """
        top_level = self._launch_depth == 0
        if top_level:
            self._claims = {}
            self._planned_completed = {}
            self._completed_runs = {}
            # every file a flow of this launch reads, registered as each flow is launched
            self._read_inputs = ReadInputs(self._launch_inputs)
            self._pending_deliveries = []
        self._launch_depth += 1
        try:
            flow = self._launch(
                flow_class,
                design,
                flow_settings,
                depender,
                copy_resources,
                all_flows_settings,
                plan=plan,
            )
            if plan is not None:
                self._planned_completed[(id(plan), flow.name)] = flow
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
        which may remove a file that is delivered. Each on its own, a failure logged and the next
        going on. The first failure, if any."""
        self._claims = {}
        deliveries, self._pending_deliveries = self._pending_deliveries, []
        first_error: Optional[Exception] = None
        for flow, delivery in deliveries if deliver else []:
            try:
                with run_dir_lock(flow.run_path):
                    flow.deliveries = delivery.deliver()
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
                with run_dir_lock(flow.run_path):
                    self._clean_up(flow, settings_json, results_json, policy)
            except Exception as e:  # noqa: BLE001 - every clean-up gets its turn
                log.error("Cleaning up %s failed: %s", flow.run_path, e, exc_info=True)
                first_error = first_error or e
        return first_error

    def _claim_run_dir(
        self,
        flow_class: Type[Flow],
        run_path: Path,
        flowrun_hash: str,
        input_settings: Flow.Settings,
        depender: Optional[Flow],
    ) -> bool:
        """Record which configuration `run_path` holds in this launch. True if this launch already
        claimed it with the same settings, whose run is then reused; a `FlowSettingsError` if it
        claimed it with other settings."""
        requester = depender.name if depender is not None else "the requested flow"
        key = Path(os.path.abspath(run_path))
        claim = self._claims.get(key)
        if claim is None:
            self._claims[key] = (flowrun_hash, requester, as_recorded(input_settings))
            return False
        claimed_hash, first, first_settings = claim
        if claimed_hash == flowrun_hash:
            return True
        difference = settings_difference(first_settings, as_recorded(input_settings))
        message = (
            f"{flow_class.name} would run twice in {run_path}, with different settings "
            f"(differing in {difference}): for {first} and for {requester}. The second run would "
            "overwrite what the first produced; --hashed-run-dirs (API hashed_run_dirs=True) "
            "gives each its own directory"
        )
        raise FlowSettingsError(
            [(str(run_path), message, None, "run_directory_conflict")], flow_class.Settings
        )

    def _launch(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        depender: Optional[Flow],
        copy_resources: List[str],
        all_flows_settings: Union[Dict, None],
        *,
        plan: Plan | None = None,
    ) -> Flow:
        """`launch_flow`'s stages, for one flow."""
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        runner_cwd = Path.cwd()
        node = None
        if plan is None and is_declared(flow_class):
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
            )
        if plan is not None:
            node = self._validate_plan(plan, flow_class, design, flow_settings, all_flows_settings)
            flow_settings = node.settings
        input_settings = self._input_settings(
            flow_class, flow_settings, design, runner_cwd, depender
        )
        # D21: a deliverable given as a location becomes its conventional name here, before the
        # identity; the tools write that, and a delivery copies it to the location
        deliveries = split_deliveries(input_settings, design.name)
        copy_resources = [res for res in copy_resources if os.path.isfile(res)]
        design_hash, flowrun_hash, run_path = self._run_identity(flow_name, design, input_settings)
        # gpt-6-sol's final (c): what this flow reads -- the settings of a dependency nested in
        # its own included, and every file under a directory one names, as its trace lists it --
        # is an input no delivery of the launch may replace, nor land beside in such a directory:
        # registered before any of this flow's deliveries, or its dependencies', is checked
        self._read_inputs.add(design_files(design))
        register_read_settings(self._read_inputs, input_settings, run_path, self.run_root)
        _refuse_inputs_inside(
            run_path, flow_name, [*design_files(design), *setting_files(input_settings)]
        )
        policy = self._run_dir_policy()
        revisit = self._claim_run_dir(flow_class, run_path, flowrun_hash, input_settings, depender)
        if revisit:
            completed = self._completed_runs.get(run_path.resolve())
            if completed is not None:
                producer, _token = completed
                if producer.design_hash != design_hash or producer.flow_hash != flowrun_hash:
                    raise FlowDependencyFailure(f"{flow_name} has a conflicting completed run")
                with self._producer_read_lease(producer):
                    reused = copy(producer)
                    reused.settings = producer.settings.model_copy(deep=True)
                    reused.design = producer.design.model_copy(deep=True)
                    reused.results = deepcopy(producer.results)
                    reused.completed_dependencies = list(producer.completed_dependencies)
                    reused.reused = True
                    reused.stale_reason = None
                    outputs_to = self.settings.outputs_to if depender is None else None
                    delivery = Deliveries(
                        run_path,
                        self.run_root,
                        deliveries,
                        inputs=self._read_inputs,
                        overwrite=self.settings.overwrite_outputs,
                        confirm=self.confirm_overwrite,
                    )
                    delivery.check_outputs_to(outputs_to)
                    delivery.check(
                        outputs_to_deliveries(
                            recorded_artifacts(run_path / "results.json"), run_path, outputs_to
                        )
                    )
                    self._defer_delivery(reused, delivery, outputs_to)
                    self.launched.append(reused)
                    return reused
            # already run in this launch, with these settings: reused, not emptied again
            policy = replace(policy, clean=False, scrub_old_runs=False)
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        # Scrub siblings before taking our own lock: concurrent hashed variants must not
        # each hold their directory while waiting to delete the other one.
        if policy.scrub_old_runs:
            scrub_runs(flow_name, run_path.parent, [run_path], run_root=self.run_root)
        with run_dir_lock(run_path), ExitStack() as read_leases:
            run_path.mkdir(parents=True, exist_ok=True)
            run_directory = RunDirectory.claimed(run_path, self.run_root)
            # the deliveries, checked before `--clean` and before any tool of this flow runs
            outputs_to = self.settings.outputs_to if depender is None else None
            delivery = Deliveries(
                run_path,
                self.run_root,
                deliveries,
                inputs=self._read_inputs,  # the launch's, completed as its flows are launched
                overwrite=self.settings.overwrite_outputs,
                confirm=self.confirm_overwrite,
            )
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
            try:
                flow.design_hash = design_hash
                flow.flow_hash = flowrun_hash
                flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                # the flow's run time includes init() and the execution of its dependencies
                flow.init_time = time.monotonic()
                try:
                    with WorkingDirectory(run_path):
                        flow.init()
                    if node is not None and node.declared:
                        if flow.dependencies:
                            raise FlowFatalError(
                                f"Declared flow {flow.name} may not add a dependency in init()"
                            )
                        assert plan is not None
                        self._run_producers(
                            flow, design, node, plan, all_flows_settings, read_leases
                        )
                    else:
                        self._run_dependencies(flow, design, all_flows_settings, read_leases)
                    flow.prepare_inputs()
                    prepared = registered_input_files(flow)
                    self._read_inputs.add(prepared)
                    _refuse_inputs_inside(flow.run_path, flow.name, prepared)
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    # a dependency failed or raised: this flow did not run: its directory no longer vouches for a success
                    remove_trace(run_path)
                    run_directory.remove(results_json)
                    self._record_identity(flow, run_path)
                    flow.results.success = False
                    flow.results["error"] = {"type": type(e).__name__, "message": str(e)}
                    self._report(flow, design, results_json, record=True)
                    raise

                # only now: what the flow consumes includes its dependencies' outputs and runs
                always = flow.always_runs()
                expected = (
                    None
                    if always is not None
                    else expectation(flow, design, input_settings, self.run_root)
                )
                if expected is None:
                    flow.stale_reason = always
                    log.info("Running %s: %s", flow.name, always)
                elif (not self.settings.rebuild_all or revisit) and not policy.clean:
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
                    # R50 j: the reports the last run read are not this run's, whatever their
                    # mtime (R49's rule, `Flow.report_file`, stays as a second line)
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
                    deliveries=[
                        {"setting": d.key, "name": str(d.name), "to": str(d.destination)}
                        for d in deliveries
                    ],
                    outputs_to=outputs_to,
                )
                if self.settings.dump_settings_json:
                    log.info("writing prepared settings to %s", settings_json)
                    dump_json(all_settings, settings_json, backup=self.settings.backups)
                copied_res_dir = run_path / flow.copied_resources_dir
                for res in copy_resources:
                    log.info("Copying %s to %s", str(res), str(copied_res_dir))
                    copied_res_dir.mkdir(parents=True, exist_ok=True)
                    # inside the run directory, as a complete file replacing a link at its
                    # name rather than writing through it
                    replacing_copy(res, run_directory.writable(copied_res_dir / Path(res).name))
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
                    with recording_programs() as programs:
                        self._execute(flow, run_path, input_settings)
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
                    trace = build_trace(expected, flow, programs, snapshot, input_settings)
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
        run_dir = Path(os.path.abspath(flow.run_path))
        for earlier_flow, earlier in self._pending_deliveries:
            if Path(os.path.abspath(earlier_flow.run_path)) == run_dir:
                earlier.merge(delivery)  # the same run directory, entered again in this launch
                return
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
        # a path the flow writes that leads out of its run directory (rule R4), a setting the
        # flow cannot run without, and a design it cannot run are reported now, before anything
        # is set up for the run
        check_launchable(flow_class, settings, design)
        return settings

    @staticmethod
    def _suggest_dependency_node(flow_class: type[Flow], error: FlowSettingsError) -> None:
        """Tell a setting that belongs to a dependency where it goes: `-s flows.<node>.key`."""
        suggest_dependency_node(flow_class, error)

    def _run_identity(
        self, flow_name: str, design: Design, settings: Flow.Settings
    ) -> tuple[str, str, Path]:
        """Stage 2: `(design_hash, flowrun_hash, run_path)`."""
        # GOTCHA: design contains tb settings even for simulation flows
        # OTOH removing tb from hash for sim flows creates a mismatch for different flows of the same design
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )
        flowrun_hash = flow_run_hash(flow_name, settings, design.name)
        run_path = self.get_flow_run_path(design.name, flow_name, flowrun_hash)
        return design_hash, flowrun_hash, run_path

    def _run_dir_policy(self) -> RunDirPolicy:
        """What this launch does with its run directory: the launcher's settings, for every
        flow alike, dependencies included."""
        return RunDirPolicy(
            clean=self.settings.clean,
            scrub_old_runs=self.settings.scrub_old_runs,
            post_cleanup=self.settings.post_cleanup,
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

    def _run_dependencies(
        self,
        flow: Flow,
        design: Design,
        all_flows_settings: Dict | None,
        read_leases: ExitStack,
    ) -> None:
        """Stage 4: launch every dependency `flow.init()` registered, through `launch_flow`, each
        in its own run directory beside `flow`'s."""
        for dep_cls, dep_settings, dep_resources in flow.dependencies:
            if isinstance(dep_cls, str):
                dep_cls = get_flow_class(dep_cls)
            dep_settings = dependency_settings(
                dep_cls, dep_settings, flow.settings, all_flows_settings
            )
            log.info(
                "Running dependency: %s (%s.%s)",
                dep_cls.name,
                dep_cls.__module__,
                dep_cls.__qualname__,
            )
            resources: list[str] = []
            for res in dep_resources:
                if not os.path.isabs(res):
                    res_path = os.path.join(flow.run_path.absolute(), res)
                    resources += glob(res_path)
            completed_dep = self.launch_flow(
                dep_cls,
                design,
                dep_settings,
                depender=flow,
                copy_resources=resources,
                all_flows_settings=all_flows_settings,
            )
            if not completed_dep.succeeded:
                log.critical("Dependency flow: %s failed!", dep_cls.name)
                raise FlowDependencyFailure(
                    f"dependency {dep_cls.name} failed: see "
                    f"{completed_dep.run_path.absolute() / 'results.json'}"
                )
            read_leases.enter_context(self._producer_read_lease(completed_dep))
            flow.completed_dependencies.append(completed_dep)

    @contextmanager
    def _producer_read_lease(self, producer: Flow):
        """Verify the completed generation under SH before any consumer can read its files."""
        with ExitStack() as lease:
            try:
                lease.enter_context(run_dir_read_lock(producer.run_path))
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
    ) -> None:
        """Materialize exactly the planned inputs; never choose another producer here."""
        records = []
        declarations = declared_inputs(type(flow))
        for selected in node.inputs:
            declaration = declarations[selected.name]
            producer = None
            paths = list(selected.sources)
            if selected.origin == "producer":
                assert selected.producer is not None and selected.output is not None
                producer_node = plan.node(selected.producer)
                key = (id(plan), producer_node.name)
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
                        )
                    except Exception as error:
                        raise FlowDependencyFailure(
                            f"dependency {producer_node.name} failed: {error}; see "
                            f"{producer_node.run_path / 'results.json'}"
                        ) from error
                    self._planned_completed[key] = producer
                if not producer.succeeded:
                    raise FlowDependencyFailure(
                        f"dependency {producer.name} failed: {producer.results.get('error', '')}; "
                        f"see {producer.run_path / 'results.json'}"
                    )
                if producer not in flow.completed_dependencies:
                    read_leases.enter_context(self._producer_read_lease(producer))
                    flow.completed_dependencies.append(producer)
                accepted = selected_types(type(flow), node.settings, selected.name)
                produced = selected_types(
                    producer_node.flow_class, producer_node.settings, selected.output, output=True
                )
                if not produced or not set(produced) <= set(accepted):
                    raise FlowDependencyFailure(
                        f"{flow.name}.{selected.name}: {producer.name}.{selected.output} "
                        "has no compatible output for the selected target"
                    )
                paths = handed_over(producer, selected.output)
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
                )
            )
        flow.declared_input_records = tuple(records)
        selected_files = declared_input_files(flow)
        self._read_inputs.add(selected_files)
        _refuse_inputs_inside(flow.run_path, flow.name, selected_files)

    @staticmethod
    def _record_identity(flow: Flow, run_path: Path) -> None:
        """What every results document says of the run, whether it succeeded or not."""
        flow.results["design"] = flow.design.name
        flow.results["design_hash"] = flow.design_hash
        flow.results["flow"] = flow.name
        flow.results["flow_hash"] = flow.flow_hash
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
            if policy.post_cleanup_purge:
                log.warning("Deleting flow run path %s", flow.run_path)
                flow.run_directory.delete()
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
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        all_flows_settings: Union[Dict, None] = None,
        *,
        plan: Plan | None = None,
    ) -> Optional[Flow]:
        """Launch `flow_class` on `design`. `all_flows_settings` (`flows` sections) are composed
        into `flow_settings` exactly as `run()` composes the design's and project's sections
        (`compose_flow_settings`); a `Flow.Settings` instance is taken as final."""
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
            copy_resources=copy_resources,
            all_flows_settings=all_flows_settings,
            plan=plan,
        )

    @staticmethod
    def _design_from_project(
        xeda_project: XedaProject | None,
        xedaproject: str,
        name: Any,
        select_design_in_project=None,
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
            selected = xeda_project.get_design(str(name))
            if selected is None:
                raise DesignNotFoundError(
                    f"Design '{name}' is not in the project file \"{xedaproject}\". "
                    f"Its designs are: {', '.join(names)}."
                )
        elif len(xeda_project.designs) == 1:
            selected = xeda_project.get_design()
        elif select_design_in_project:
            selected = select_design_in_project(xeda_project, name)
        if selected is None:
            raise DesignNotFoundError(
                f'No design was given or selected among those of "{xedaproject}": '
                f"{', '.join(names)}."
            )
        return selected

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
    ) -> Optional[Flow]:
        """Load and compose a request, then execute its resolved declared graph."""
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
        )
        plan = self._resolve_request(request) if is_declared(request.flow_class) else None
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
    ) -> Plan:
        """Plan what run() would execute, refusing side-effecting design loading (R2)."""
        return self._resolve_request(
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
                _planning=True,
            )
        )

    def _request(
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
        *,
        _planning: bool = False,
    ) -> _Request:
        """
        Flexible API for launching flows.
        """
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
        # A git dependency without a directory of its own is cloned into the run root, which
        # is asked for only then: a design that fails to load leaves no run root behind.
        with (
            cloning_dependencies_into(lambda: self.run_root / ".dependencies"),
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
                    )

                elif isinstance(design, dict):
                    design = dict(design)
                    if "design_root" not in design:
                        design["design_root"] = Path.cwd()
                    design = Design(**design)
            else:
                design = self._design_from_project(
                    xeda_project, xedaproject, design, select_design_in_project
                )
        if isinstance(flow_settings, Flow.Settings):
            flow_settings = flow_settings.model_dump()
        else:
            assert isinstance(
                flow_settings, (list, tuple, Mapping)
            ), "flow_settings should be a list, tuple or dict"
            flow_settings = settings_to_dict(flow_settings)
        if not isinstance(flow_overrides, dict):
            flow_overrides = settings_to_dict(flow_overrides)
        assert isinstance(
            flow_settings, dict
        ), f"flow_settings should be a dict at this stage, but was {type(flow_settings)}"
        assert isinstance(
            flow_overrides, dict
        ), f"flow_overrides should be a dict at this stage, but was {type(flow_overrides)}"
        if isinstance(flow, str):
            flow = flow.replace("-", "_")
            flow_class = get_flow_class(flow)
        else:
            flow_class = flow

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
        project_sections = merge_flow_sections(
            flows_settings, flow_class_for=_get_flow_class_if_known
        )
        design_sections = merge_flow_sections(design.flow, flow_class_for=_get_flow_class_if_known)
        # the command line's `-s flows.<node>.key` is the third origin; it may only name a flow
        # of this run, and `-s key` is the requested flow's own leaf
        cli_sections, flow_settings = command_line_sections(
            flow_settings, flow_class, flow_class_for=_get_flow_class_if_known
        )
        origins = [project_sections, design_sections, cli_sections]  # the three origins, in order
        # dependencies read their own merged section (`dependency_settings`), under the
        # depender's resolved nested value
        all_sections = merge_flow_sections(*origins, flow_class_for=_get_flow_class_if_known)
        # `-s` wins over the design and project files, as documented; see `settings_layers`.
        final_flow_settings = compose_flow_settings(
            flow_class, origins, flow_settings, flow_overrides
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
                    (str(Path(xedaproject).absolute()), project_sections),
                    (
                        str(given_file.absolute()) if given_file else f"the design {design.name}",
                        design_sections,
                    ),
                )
            ),
            merge_flow_sections(
                cli_sections,
                {flow_class.name: flow_settings},
                flow_class_for=_get_flow_class_if_known,
            ),
            {flow_class.name: deepcopy(flow_overrides)},
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
