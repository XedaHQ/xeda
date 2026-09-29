"""Launch execution of flows"""

from __future__ import annotations

import difflib
import importlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from pprint import PrettyPrinter
from typing import Any, Dict, List, Literal, Optional, Tuple, Type, TypeVar, Union

import yaml
from box import Box
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..artifacts import drop_unwritten_artifacts, iter_artifact_paths
from ..console import console
from ..dataclass import XedaBaseModel, model_validator
from ..design import Design, names_a_design_file
from ..flow import Flow, FlowDependencyFailure, FlowSettingsError, registered_flows
from ..flow import flowrun_hash as flow_run_hash
from ..flow.run_dir import (
    RUN_DIR_MARKER,
    RunDirectoryError,
    check_inside_run_root,
    claim_run_dir,
    is_earlier_run_of,
    is_marked_run_dir,
    mark_run_dir,
    run_dir_name,
)
from ..proc_utils import recording_programs
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
from ..xedaproject import XedaProject
from .run_lock import lock_file, run_dir_lock
from .settings_layers import flow_settings_from_sections, merge_flow_sections, merge_layers
from .trace import (
    as_recorded,
    check_trace,
    previous_trace,
    probe_program,
    remove_trace,
    settings_difference,
    write_trace,
)
from .trace_inputs import build_trace, expectation, snapshot_inputs

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


class ProjectFileError(XedaException):
    """A project file (`xedaproject.toml`) that cannot be opened, parsed or used."""


#: Launcher settings that no longer exist, with what replaced them. Giving one is an error that
#: says so, in the words `Flow.Settings.removed_settings` uses for flow settings.
_REMOVED_LAUNCHER_SETTINGS = {
    "cached_dependencies": "rebuild='stale' (the default) to reuse unchanged runs, and "
    "run_dirs='hashed' to keep settings variants side by side",
    "skip_if_previous_run_exists": "rebuild='stale' (the default)",
    "incremental": "clean=True to empty run directories before running (they are otherwise "
    "always reused)",
    "cleanup_before_run": "clean=True",
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


def on_rm_error(func, path, exc_info):
    log.error("Error while removing %s: %s, %s", path, func, exc_info)


def rmtree(path):
    if os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=on_rm_error)
        else:
            shutil.rmtree(path, onerror=on_rm_error)


def scrub_runs(flow_name: str, dir: Path, exclude: List[Path] = []) -> bool:
    """Find and (with confirmation) remove `flow_name`'s run directories under `dir`.

    A run directory of the flow is named `flow_name`, optionally followed by an underscore and
    a `DIR_NAME_HASH_LEN`-char `[a-z0-9]` `flowrun_hash` (the unhashed form is what every default
    run creates; the hashed form only appears with `run_dirs="hashed"`). Matched against
    `dir`'s children rather than a `f"{flow_name}_*"` glob, so the unhashed directory -- which
    that glob can never match -- is included too.

    Only xeda's own are removed: if any of them is neither marked (`run_dir.RUN_DIR_MARKER`) nor
    an earlier run of the flow (`run_dir.is_earlier_run_of`), the scrub is refused, naming it,
    before anything is asked or removed.
    """
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
        ]
    )
    linked = [p for p in dirs_to_rm if p.is_symlink()]
    if linked:
        raise RunDirectoryError(
            f"Scrubbing {flow_name}'s run directories would remove "
            f"{', '.join(f'{p} (a symbolic link to {os.readlink(p)})' for p in linked)}: xeda "
            "removes a run directory only as the directory itself, never through a link, which "
            "would remove whatever it leads to. Nothing was removed. Remove the link yourself."
        )
    not_xedas = [
        p for p in dirs_to_rm if not is_marked_run_dir(p) and not is_earlier_run_of(p, flow_name)
    ]
    if not_xedas:
        raise RunDirectoryError(
            f"Scrubbing {flow_name}'s run directories would delete "
            f"{', '.join(str(p) for p in not_xedas)}, which xeda did not make: "
            f"{'it carries' if len(not_xedas) == 1 else 'they carry'} no {RUN_DIR_MARKER} and "
            f"{'holds' if len(not_xedas) == 1 else 'hold'} no earlier run of {flow_name}. Nothing "
            "was removed. Move your files out of it, or remove it yourself."
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
                rmtree(p)
                if not p.exists():  # its lock, beside it, only once it is gone
                    lock_file(p).unlink(missing_ok=True)
            console.print(f"{len(dirs_to_rm)} folders removed.")
            return True
        else:
            console.print("Not confirmed. No files or folders were removed.")
    return False


FlowLauncherType = TypeVar("FlowLauncherType", bound="FlowLauncher")


def dependency_settings(
    dep_cls: type[Flow],
    given: Flow.Settings | None,
    depender_settings: Flow.Settings,
    all_flows_settings: Mapping[str, Any] | None = None,
) -> Flow.Settings:
    """The settings a dependency is launched with -- composed here, and only here.

    `given` is what the depending flow passed to `add_dependency` (for a declared dependency,
    already `resolve_dependency`-d). Layers, lowest precedence first:

    1. the design's / project's own section for the dependency's flow (``[flows.yosys_fpga]``),
       merged deeply like any settings layer (`settings_layers.merge_layers`);
    2. `given`, which is more specific.

    The depending flow's diagnostics then carry over: `debug` if it is on, and a `verbose` level
    above 1 when the dependency has none of its own.
    """
    section = (all_flows_settings or {}).get(dep_cls.name)
    if section or given is None or not given.context:
        own = given.model_dump(exclude_unset=True) if given is not None else {}
        settings = dep_cls.Settings.from_input(
            merge_layers(section, own, settings_cls=dep_cls.Settings),
            **depender_settings.context,
        )
    else:
        settings = given
    settings.debug |= depender_settings.debug
    if not settings.verbose and depender_settings.verbose > 1:
        settings.verbose = depender_settings.verbose
    return settings


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
        #: "stale" (make-like): only flows whose trace no longer matches; "all": every flow.
        rebuild: Literal["all", "stale"] = "stale"
        #: "stable": <design>/<flow>; "hashed": <design>/<flow>_<settings hash>.
        run_dirs: Literal["stable", "hashed"] = "stable"
        #: empty each flow's run directory before it runs, and run every flow
        clean: bool = False
        backups: bool = False
        # remove flow files except settings.json, results.json, and artifacts _after_ run:
        post_cleanup: bool = False
        # remove flow_run folder and all of its contents _after_ running the flow:
        post_cleanup_purge: bool = False
        # remove previous flow directories _before_ running the flow:
        scrub_old_runs: bool = False
        run_path: Optional[Union[str, os.PathLike]] = None

        @model_validator(mode="before")
        @classmethod
        def _removed_settings(cls, values):
            if isinstance(values, dict):
                for old, replacement in _REMOVED_LAUNCHER_SETTINGS.items():
                    if old in values:
                        raise ValueError(f"`{old}` was removed: use {replacement}")
            return values

    def __init__(self, xeda_run_dir: Union[str, Path, None] = None, **kwargs) -> None:
        if "xeda_run_dir" in kwargs:
            xeda_run_dir = kwargs.pop("xeda_run_dir")
        if not xeda_run_dir:
            xeda_run_dir = "xeda_run"
        xeda_run_dir = Path(xeda_run_dir).resolve()
        xeda_run_dir.mkdir(exist_ok=True, parents=True)
        log.debug("%s xeda_run_dir=%s", self.__class__.__name__, xeda_run_dir)
        self.xeda_run_dir: Path = xeda_run_dir
        self.settings = self.Settings(**kwargs)
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

        if self.settings.run_path is not None:
            self.settings.post_cleanup = False
            self.settings.scrub_old_runs = False

    def get_flow_run_path(
        self, design_name: str, node_name: str, identity: Optional[str] = None
    ) -> Path:
        """`<xeda_run>/<design>/<node>`, or `<node>_<identity>` with hashed run directories:
        strictly inside the run root, or refused (`run_dir.RunDirectoryError`) -- a design named
        `..` would run, and its `clean` delete, in a directory of the user's."""
        subdir = run_dir_name(node_name, "flow")
        if self.settings.run_dirs == "hashed" and identity:
            subdir += f"_{identity[:DIR_NAME_HASH_LEN]}"
        run_path = self.xeda_run_dir / run_dir_name(design_name, "design") / subdir
        check_inside_run_root(run_path, self.xeda_run_dir)
        return run_path

    def launch_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None],
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        run_path: Optional[Path] = None,
        all_flows_settings: Union[Dict, None] = None,
    ) -> Flow:
        """Launch `flow_class` on `design`: the one procedure every flow run, and every
        dependency run, goes through -- make's order: bring every prerequisite up to date, then
        judge this flow against them. Its stages, in order:

        1. **input** (`_input_settings`): validate the settings in their context (design root,
           start directory) and apply the launcher's ``--debug``. The result is the run's input,
           never modified afterwards. A missing required setting, or a design the flow cannot
           run, fails the launch here.
        2. **identity** (`_run_identity`): the design's hash and the settings' `flowrun_hash`;
           the run directory, `<design>/<flow>` (``run_dirs="hashed"``: `<flow>_<flowrun_hash>`),
           locked from here until the run's trace is written (`run_lock`; a directory given as
           `run_path` by a lock file inside it). With ``clean``, the directory is emptied now
           (or backed up, with ``backups``).
        3. **prepare**: construct the flow with its own copy of the input and call its `init()`,
           which registers the flow's dependencies. `init()` runs for a flow that turns out to be
           fresh too, so it must not touch the flow's outputs.
        4. **dependencies** (`_run_dependencies`): launch each through this same procedure, in a
           sibling run directory, with settings composed by `dependency_settings`.
        5. **freshness**: with ``rebuild="stale"`` (the default), a flow whose trace still
           matches what it would consume now (`trace.check_trace`) is not run again: its recorded
           results are reused. Otherwise `flow.stale_reason` says why it runs. A flow that can
           never be reused (`Flow.always_runs`: it programs a device, draws a new random seed,
           ...) always runs, with that reason, and no trace records it.
        6. **run**: delete the trace, then record every expected input as the run finds it
           (`trace_inputs.snapshot_inputs`: a file edited while the run goes on then no longer
           matches its trace), record `settings.json`, and execute (`_execute`: `run()`,
           `parse_reports()`, `check_results()`, results; a failed run's artifacts only as far as
           it wrote them, `Flow.wrote_output`).
        7. **report** (`_report`): artifacts, `results.json`; then, for a successful run, the
           trace (`trace_inputs.build_trace`: the snapshot, and every file the run's depfiles
           and settings name classified by its origin -- written by the run, or read by it).
           The post-run clean-up (`_clean_up`) of every flow that ran waits until the flow this
           launch was asked for has completed: until then, a flow may still read files its
           dependencies wrote without declaring them.

        Within one launch, a run directory holds one configuration: a second launch into it with
        other settings is a `FlowSettingsError`, since it would overwrite what the first one
        produced; with the same settings, the second reuses the first's run. Every flow launched
        is appended to `launched` as it completes, whether it succeeded, failed or raised.
        """
        top_level = self._launch_depth == 0
        if top_level:
            self._claims = {}
        self._launch_depth += 1
        try:
            flow = self._launch(
                flow_class,
                design,
                flow_settings,
                depender,
                copy_resources,
                run_path,
                all_flows_settings,
            )
        except BaseException:
            self._launch_depth -= 1
            if top_level:
                self._finish_launch()  # its failures are logged; the launch's error propagates
            raise
        self._launch_depth -= 1
        if top_level:
            error = self._finish_launch()
            if error is not None:
                raise error
        return flow

    def _finish_launch(self) -> Optional[Exception]:
        """At the end of a top-level launch: run the deferred clean-ups in completion order, each
        on its own, logging a failure and going on with the next. The first failure, if any."""
        self._claims = {}
        pending, self._pending_clean_ups = self._pending_clean_ups, []
        first_error: Optional[Exception] = None
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
            "overwrite what the first produced; run_dirs='hashed' gives each its own directory"
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
        run_path: Optional[Path],
        all_flows_settings: Union[Dict, None],
    ) -> Flow:
        """`launch_flow`'s stages, for one flow."""
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        explicit = run_path is not None and depender is None
        if run_path is not None and depender is None:
            # A directory given explicitly (`--cwd`, `run_path`), before anything is created,
            # written or deleted: used only if xeda creates it, finds it empty, or marked it.
            claim_run_dir(run_path)
        runner_cwd = Path.cwd()
        input_settings = self._input_settings(flow_class, flow_settings, design, runner_cwd)
        copy_resources = [res for res in copy_resources if os.path.isfile(res)]
        # the directory `--cwd` names is the user's, not xeda's: never cleaned, nor locked from
        # beside it, outside the directory the user chose
        managed = run_path is None
        policy = self._run_dir_policy(run_path)
        design_hash, flowrun_hash, run_path = self._run_identity(
            flow_name, design, input_settings, run_path
        )
        if not explicit:
            # A directory xeda derived -- a dependency's too, a sibling of its depender's -- before
            # it is scrubbed, emptied or removed: it must resolve strictly inside the run root,
            # where a link could lead anywhere, and be xeda's.
            check_inside_run_root(run_path, self.xeda_run_dir)
            claim_run_dir(run_path, flow_name)
        revisit = self._claim_run_dir(flow_class, run_path, flowrun_hash, input_settings, depender)
        if revisit:
            # already run in this launch, with these settings: reused, not emptied again
            policy = replace(policy, clean=False, scrub_old_runs=False)
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        with run_dir_lock(run_path, inside=not managed):
            if policy.scrub_old_runs:
                scrub_runs(flow_name, run_path.parent, [run_path])
            if policy.clean and run_path.exists():
                if self.settings.backups:
                    backup_existing(run_path)
                else:
                    rmtree(run_path)
            run_path.mkdir(parents=True, exist_ok=True)
            mark_run_dir(run_path)  # re-created by `clean`, it stays xeda's

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
                )
            try:
                flow.design_hash = design_hash
                flow.flow_hash = flowrun_hash
                flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                # the flow's run time includes init() and the execution of its dependencies
                flow.init_time = time.monotonic()
                with WorkingDirectory(run_path):
                    flow.init()
                self._run_dependencies(flow, design, all_flows_settings)

                # only now: what the flow consumes includes its dependencies' outputs and runs
                always = flow.always_runs()
                expected = (
                    None
                    if always is not None
                    else expectation(flow, design, input_settings, self.xeda_run_dir)
                )
                if expected is None:
                    flow.stale_reason = always
                    log.info("Running %s: %s", flow.name, always)
                elif (self.settings.rebuild == "stale" or revisit) and not policy.clean:
                    freshness = check_trace(run_path, expected, probe_program)
                    if freshness.fresh and self._reuse_results(flow, results_json):
                        if freshness.refreshed is not None:
                            write_trace(run_path, freshness.refreshed)
                        flow.run_id = freshness.run_id
                        log.info("%s is up to date (%s)", flow.name, run_path)
                        self._report(flow, design, results_json, record=False)
                        return flow
                    flow.stale_reason = (
                        freshness.reason or f"no successful results in {results_json}"
                    )
                    log.info("Running %s: %s", flow.name, flow.stale_reason)

                previous = previous_trace(run_path, flow.name) if expected is not None else None
                # from here until a new trace is written, nothing vouches for this directory
                remove_trace(run_path)
                # The run starts now: its inputs are recorded as it finds them, so that a file
                # edited while it runs no longer matches what its trace says it consumed.
                snapshot = (
                    None
                    if expected is None
                    else snapshot_inputs(expected, previous, run_path, managed)
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
                )
                if self.settings.dump_settings_json:
                    log.info("writing prepared settings to %s", settings_json)
                    dump_json(all_settings, settings_json, backup=self.settings.backups)
                copied_res_dir = run_path / flow.copied_resources_dir
                for res in copy_resources:
                    log.info("Copying %s to %s", str(res), str(copied_res_dir))
                    copied_res_dir.mkdir(parents=True, exist_ok=True)
                    replacing_copy(res, copied_res_dir)  # a link there is replaced, not followed
                with recording_programs() as programs:
                    self._execute(flow, run_path, input_settings)
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
                if policy.post_cleanup:
                    self._pending_clean_ups.append((flow, settings_json, results_json, policy))
            finally:
                self.launched.append(flow)
        return flow

    def _input_settings(
        self,
        flow_class: type[Flow],
        flow_settings: dict[str, Any] | Flow.Settings | None,
        design: Design,
        runner_cwd: Path,
    ) -> Flow.Settings:
        """Stage 1: the run's input settings, validated in context and owned by the launcher.

        Never modified afterwards: the flow works on a copy of its own (`flow.settings`), which
        its `__init__`/`init()`/`run()` may complete with derived values, resolved paths and
        outputs without any of that leaking into the run's identity.
        """
        if flow_settings is None:
            flow_settings = {}
        if isinstance(flow_settings, dict):
            settings = flow_class.Settings.from_input(
                flow_settings, design_root=design.root_path, runner_cwd=runner_cwd
            )
        elif not flow_settings.context:
            settings = flow_class.Settings.from_input(
                # Preserve edits made inside default-created nested dependency settings. The
                # parent field is not marked "set" when only its child is assigned, so
                # `exclude_unset=True` would silently discard that caller input here.
                flow_settings.model_dump(),
                design_root=design.root_path,
                runner_cwd=runner_cwd,
            )
        else:
            settings = flow_settings.model_copy(deep=True)
        if self.debug:
            log.debug(
                "Flow '%s' settings: %s",
                flow_class.name,
                settings.model_dump_json(exclude_unset=True, indent=2),
            )
            settings.debug = True  # the launcher's `--debug` is part of the input
        # a setting the flow cannot run without, or a design it cannot run, is reported now,
        # before anything is set up for the run
        flow_class.check_required_settings(settings)
        flow_class.check_design_supported(design)
        return settings

    def _run_identity(
        self, flow_name: str, design: Design, settings: Flow.Settings, run_path: Path | None
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
        flowrun_hash = flow_run_hash(flow_name, settings)
        if run_path is None:
            run_path = self.get_flow_run_path(design.name, flow_name, flowrun_hash)
        return design_hash, flowrun_hash, run_path

    def _run_dir_policy(self, run_path: Path | None) -> RunDirPolicy:
        """What this launch does with its run directory: the launcher's settings, for every
        flow alike, dependencies included -- except for a launch into a given `run_path`, the
        directory `--cwd` names, which neither cleans nor scrubs it, nor cleans up after itself,
        since that directory is not xeda's to clean."""
        if run_path is not None:
            return RunDirPolicy(
                clean=False, scrub_old_runs=False, post_cleanup=False, post_cleanup_purge=False
            )
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
                raise FlowDependencyFailure()
            flow.completed_dependencies.append(completed_dep)

    def _execute(self, flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
        """Stage 6: `run()`, `parse_reports()`, and the run's results, artifacts included."""
        flow.results["design"] = flow.design.name
        flow.results["design_hash"] = flow.design_hash
        flow.results["flow"] = flow.name
        flow.results["flow_hash"] = flow.flow_hash
        flow.results["run_path"] = run_path.absolute()

        success = True

        with WorkingDirectory(run_path):
            if flow.settings.reports_dir:
                flow.settings.reports_dir.mkdir(exist_ok=True, parents=True)
            try:
                flow.run()
            except NonZeroExitCode as e:
                log.error(
                    "Execution of '%s' returned %d",
                    (
                        " ".join(e.command_args)
                        if isinstance(e.command_args, (list, tuple))
                        else e.command_args
                    ),
                    e.exit_code,
                )
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
            if not success and not input_settings.is_quiet:
                log.debug("Failure was reported in the parsed results.")
            flow.results.success = success
            flow.results.timestamp = flow.timestamp
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
                rmtree(flow.run_path)
            else:
                log.warning("Cleaning up %s", flow.run_path)
                named_run_path = Path(os.path.abspath(flow.run_path))
                run_path = flow.run_path.resolve()
                kept: set[Path] = {run_path / RUN_DIR_MARKER}  # it stays xeda's
                for raw_path in (
                    settings_json,
                    results_json,
                    *iter_artifact_paths(flow.results.artifacts),
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
                            removed.append(path)
                            if path.is_symlink() or path.is_file():
                                path.unlink()
                            elif path.is_dir():
                                rmtree(path)

                if run_path.is_relative_to(self.xeda_run_dir) and run_path not in kept:
                    prune(run_path)
                log.warning("Removed the following files: %s", " ".join(str(p) for p in removed))

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[Dict[str, Any], Flow.Settings, None] = None,
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        run_path: Optional[Path] = None,
        all_flows_settings: Union[Dict, None] = None,
    ) -> Optional[Flow]:
        # default run_flow() is launch_flow() but can be overridden in a subclass
        return self.launch_flow(
            flow_class,
            design,
            flow_settings,
            depender=depender,
            copy_resources=copy_resources,
            run_path=run_path,
            all_flows_settings=all_flows_settings,
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
                f'{given} there is no project file "{xedaproject}" in {Path.cwd()} to take '
                "a design from. Give a design file (.toml, .json, .yaml or .yml)."
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
        if xedaproject:
            if not Path(xedaproject).exists():
                raise ProjectFileError(f'Cannot open project file "{xedaproject}": no such file')
        else:
            xedaproject = "xedaproject.toml"
        if design is not None:
            if isinstance(design, (Design, dict, Path)):
                design_not_in_project = True
            elif names_a_design_file(design):
                # a design file, whether or not it exists: `Design.from_file` says what is wrong
                # with it, rather than a project reporting a design of that name missing
                design_not_in_project = True
                design = Path(design)
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
        flows_settings = merge_flow_sections(
            flows_settings,
            design.flow,
            flow_class_for=_get_flow_class_if_known,
        )

        # `-s` wins over the design and project files, as documented; see `settings_layers`.
        final_flow_settings = merge_layers(
            flow_settings_from_sections(flow_class, flows_settings),
            flow_settings,
            flow_overrides,
            settings_cls=flow_class.Settings,
        )
        if self.settings.debug:
            log.info("design: %s" % PrettyPrinter().pformat(design.model_dump()))
        run_path = self.settings.run_path
        if run_path is not None and not isinstance(run_path, Path):
            run_path = Path(run_path)
        return self.run_flow(
            flow_class,
            design,
            final_flow_settings,
            run_path=run_path,
            all_flows_settings=flows_settings,
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
