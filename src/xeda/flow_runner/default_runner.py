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
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from pprint import PrettyPrinter
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from box import Box
from pathvalidate import sanitize_filename
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..console import console
from ..dataclass import XedaBaseModel
from ..design import AnyDesignValidationException, Design, DesignFileParseError
from ..flow import Flow, FlowDependencyFailure, registered_flows
from ..flow import flowrun_hash as flow_run_hash
from ..tool import NonZeroExitCode
from ..utils import (
    WorkingDirectory,
    backup_existing,
    dump_json,
    semantic_hash,
    settings_to_dict,
    snakecase_to_camelcase,
    unique,
)
from ..version import __version__
from ..xedaproject import XedaProject
from .settings_layers import merge_flow_sections, merge_layers

__all__ = [
    "DefaultRunner",
    "FlowNotFoundError",
    "FlowRunner",
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
    if results is None and flow:
        results = flow.results
    assert results is not None, "results is None"
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
        if v is not None and not k.startswith("_"):
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
                table.add_row(k + ":", "", style=Style(bold=True))
                for xk, xv in v.items():
                    if isinstance(xv, dict):
                        xv = json.dumps(
                            xv,
                            indent=1,
                            default=lambda obj: (
                                obj.__json_encoder__
                                if hasattr(obj, "__json_encoder__")
                                else obj.__dict__ if hasattr(obj, "__dict__") else str(obj)
                            ),
                        )
                    else:
                        xv = str(xv)
                    table.add_row(Text(" " + xk), str(xv))
                continue
            if isinstance(v, float):
                v = f"{v:,.3f}"
            elif isinstance(v, int):
                v = f"{v:,}"
            table.add_row(k, str(v))
    console.print(table)


class FlowNotFoundError(Exception):
    def __init__(self, flow_name: Optional[str] = None, suggestions: Iterable[str] = ()) -> None:
        self.flow_name = flow_name
        self.suggestions = list(suggestions)
        msg = f"Flow '{flow_name}' was not found." if flow_name else "Flow was not found."
        if self.suggestions:
            msg += " Did you mean: " + ", ".join(self.suggestions) + "?"
        msg += " Run `xeda list-flows` to see all available flows."
        super().__init__(msg)


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
    regex = re.compile(f"^{flow_name}_" + (r"[a-z0-9]" * DIR_NAME_HASH_LEN) + r"$")
    xr = dir.resolve()
    if not dir.exists() or not xr.is_dir():
        return False
    dirs_to_rm = unique(
        [
            p
            for p in dir.glob(f"{flow_name}_*")
            if p.is_dir()
            and regex.match(p.name)
            and all(not ex.exists() or not p.samefile(ex) for ex in exclude)
            and xr in p.resolve().parents
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
                rmtree(p)
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


class FlowLauncher:
    """
    Manage running flows and their dependencies.
    1. Instantiate instance of flow class with proper settings assigned (__init__)
    2. call Flow.init()
    3. Run all dependency flows (asked by the flow, during Flow.init)
    3. Run the flow by calling run()
    4. Run flow's parse_reports() ## TODO parse_reports will be renamed
    5. Evaluate and print the results
    """

    class Settings(XedaBaseModel):
        """Settings for FlowLaunchers"""

        debug: bool = False
        dump_settings_json: bool = True
        display_results: bool = True
        dump_results_json: bool = True
        cached_dependencies: bool = True
        skip_if_previous_run_exists: bool = False
        backups: bool = False
        incremental: bool = False
        cleanup_before_run: bool = False
        # remove flow files except settings.json, results.json, and artifacts _after_ run:
        post_cleanup: bool = False
        # remove flow_run folder and all of its contents _after_ running the flow:
        post_cleanup_purge: bool = False
        # remove previous flow directories _before_ running the flow:
        scrub_old_runs: bool = False
        run_path: Optional[Union[str, os.PathLike]] = None

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

        if self.settings.run_path is not None:
            self.settings.incremental = True
            self.settings.post_cleanup = False
            self.settings.scrub_old_runs = False

    def get_flow_run_path(
        self,
        design_name: str,
        flow_name: str,
        design_hash: Optional[str] = None,
        flowrun_hash: Optional[str] = None,
    ) -> Path:
        design_subdir = design_name
        flow_subdir = flow_name
        if self.settings.cached_dependencies:
            if design_hash and not self.settings.incremental:
                design_subdir += f"_{design_hash[:DIR_NAME_HASH_LEN]}"
            if flowrun_hash:
                flow_subdir += f"_{flowrun_hash[:DIR_NAME_HASH_LEN]}"

        run_path: Path = self.xeda_run_dir / sanitize_filename(design_subdir) / flow_subdir
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
        dependency run, goes through. Its stages, in order:

        1. **input** (`_input_settings`): validate the settings in their context (design root,
           start directory) and apply the launcher's ``--debug``. The result is the run's input,
           never modified afterwards.
        2. **identity** (`_run_identity`): the design's hash (its sources' contents) and the
           settings' `flowrun_hash`, which also name the run directory.
        3. **reuse** (`_previous_results`): with ``--cached-dependencies``, a successful previous
           run with the same identity is reused instead of repeated.
        4. **prepare**: construct the flow with its own copy of the input and call its `init()`,
           which may do setup work and registers the flow's dependencies; record `settings.json`.
        5. **dependencies** (`_run_dependencies`): launch each through this same procedure, with
           settings composed by `dependency_settings`.
        6. **run** (`_execute`): `run()`, `parse_reports()`, results.
        7. **report** (`_report`): artifacts, `results.json`, clean-up.
        """
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        runner_cwd = Path.cwd()
        input_settings = self._input_settings(flow_class, flow_settings, design, runner_cwd)
        copy_resources = [
            res for res in copy_resources if os.path.exists(res) and os.path.isfile(res)
        ]
        design_hash, flowrun_hash, run_path = self._run_identity(
            flow_name, design, input_settings, run_path
        )
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        previous_results = self._previous_results(
            flow_name, run_path, design_hash, flowrun_hash, depender
        )
        self._prepare_run_path(flow_name, run_path, previous_results)

        with WorkingDirectory(run_path):
            log.debug("Instantiating flow from %s", flow_class)
            flow = flow_class(
                input_settings.model_copy(deep=True), design, run_path, runner_cwd=runner_cwd
            )
        flow.design_hash = design_hash
        flow.flow_hash = flowrun_hash
        flow.incremental = self.settings.incremental
        if flow.runner_cwd is None:  # redundant, but OK
            flow.runner_cwd = runner_cwd
        flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        # flow execution time includes init() as well as execution of all its dependency flows
        flow.init_time = time.monotonic()

        if previous_results:
            log.warning(
                "Using previous %s results and artifacts from %s (timestamp: %s)",
                flow_name,
                run_path.absolute(),
                previous_results.get("timestamp"),
            )
            flow.results.update(**previous_results)
            flow.artifacts = previous_results.artifacts
        else:
            if self.settings.cleanup_before_run:
                flow.settings.clean = True
            with WorkingDirectory(run_path):
                if flow.settings.clean:
                    flow.clean()
                flow.init()
            if self.settings.dump_settings_json:
                log.info("writing prepared settings to %s", settings_json)
                all_settings = dict(
                    design=design,
                    design_hash=design_hash,
                    rtl_fingerprint=design.rtl_fingerprint,
                    rtl_hash=design.rtl_hash,
                    flow_name=flow_name,
                    flow_settings=input_settings,
                    effective_flow_settings=flow.settings,
                    xeda_version=__version__,
                    flowrun_hash=flowrun_hash,
                )
                dump_json(all_settings, settings_json, backup=self.settings.backups)
            copied_res_dir = run_path / flow_class.copied_resources_dir
            if copy_resources:
                copied_res_dir.mkdir(parents=True, exist_ok=True)
            for res in copy_resources:
                log.info("Copying %s to %s", str(res), str(copied_res_dir))
                shutil.copy(res, copied_res_dir)
            self._run_dependencies(flow, design, run_path, all_flows_settings)
            self._execute(flow, run_path, input_settings)
            if self.settings.dump_settings_json:
                # `run()` may finish resolving generated files or derived switches. Keep the
                # pre-run write above so a crash still leaves useful diagnostics, then replace
                # its effective snapshot after a completed execution without backing up that
                # transient snapshot.
                dump_json(all_settings, settings_json, backup=False)

        self._report(flow, design, settings_json, results_json)
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
            run_path = self.get_flow_run_path(
                design.name,
                flow_name,
                design_hash,
                flowrun_hash,
            )
        else:
            self.settings.incremental = True
            self.settings.scrub_old_runs = False
            self.settings.post_cleanup_purge = False
            self.settings.post_cleanup = False
        return design_hash, flowrun_hash, run_path

    def _previous_results(
        self,
        flow_name: str,
        run_path: Path,
        design_hash: str,
        flowrun_hash: str,
        depender: Flow | None,
    ) -> Box | None:
        """Stage 3: the results of a successful previous run with the same identity, if any is to
        be reused."""
        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"
        if not (
            (depender or self.settings.skip_if_previous_run_exists)
            and self.settings.cached_dependencies
            and run_path.exists()
            and settings_json.exists()
            and results_json.exists()
        ):
            return None
        prev_results, prev_settings = None, None
        try:
            with open(settings_json) as f:
                prev_settings = json.load(f)
            with open(results_json) as f:
                prev_results = json.load(f)
        except TypeError:
            pass
        except ValueError:
            pass
        if prev_results and prev_results.get("success") and prev_settings:
            if (
                prev_settings.get("flow_name") == flow_name
                and prev_settings.get("design_hash") == design_hash
                and prev_settings.get("flowrun_hash") == flowrun_hash
            ):
                return Box(prev_results)
            log.warning(
                "%s does not contain the expected flow and/or design hash.",
                str(settings_json.absolute()),
            )
        else:
            log.warning(
                "No valid previous results found in %s. Running %s from scratch.",
                run_path,
                flow_name,
            )
        return None

    def _prepare_run_path(
        self, flow_name: str, run_path: Path, previous_results: Box | None
    ) -> None:
        if self.settings.scrub_old_runs:
            scrub_runs(flow_name, run_path.parent, [run_path])
        if not previous_results and run_path.exists():
            if not self.settings.incremental and self.settings.run_path is None:
                if self.settings.backups:
                    backup_existing(run_path)
                else:
                    rmtree(run_path)
        if not run_path.exists():
            run_path.mkdir(parents=True)

    def _run_dependencies(
        self,
        flow: Flow,
        design: Design,
        run_path: Path,
        all_flows_settings: Dict | None,
    ) -> None:
        """Stage 5: launch every dependency `flow.init()` registered, through `launch_flow`."""
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
                run_path=run_path / dep_cls.name if run_path else None,
                all_flows_settings=all_flows_settings,
            )
            if not completed_dep.succeeded:
                log.critical("Dependency flow: %s failed!", dep_cls.name)
                raise FlowDependencyFailure()
            flow.completed_dependencies.append(completed_dep)

    def _execute(self, flow: Flow, run_path: Path, input_settings: Flow.Settings) -> None:
        """Stage 6: `run()`, `parse_reports()`, and the run's results."""
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
            except Exception as e:  # pylint: disable=broad-except
                log.critical("parse_reports threw an exception: %s", e)
                if success:  # if so far so good this is a bug!
                    raise e
            flow.add_canonical_result_aliases()
            if not success and not input_settings.quiet:
                log.debug("Failure was reported in the parsed results.")
            flow.results.success = success
            flow.results.timestamp = flow.timestamp

    def _report(self, flow: Flow, design: Design, settings_json: Path, results_json: Path) -> None:
        """Stage 7: show and record the results; clean up the run directory as configured."""
        for k, v in flow.artifacts.items():
            if not flow.results.artifacts.get(k):
                flow.results.artifacts[k] = v

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

            for k, v in flow.artifacts.items():
                if isinstance(v, list) and v:
                    v = [str(i) for i in v]
                    table.add_row(v[0], end_section=len(v) == 1)
                    for vi in v[1:-1]:
                        table.add_row("", vi, end_section=False)
                    if len(v) > 1:
                        table.add_row("", v[-1], end_section=True)
                else:
                    table.add_row(str(v), end_section=True)

            console.print("")
            console.print(table)
            console.print("")

        if self.settings.dump_results_json:
            dump_json(flow.results, results_json, backup=self.settings.backups)
            log.info("Results written to %s", results_json)

        if self.settings.display_results:
            print_results(
                flow,
                title=f"Results of flow:{flow.name} design:{design.name}",
                skip_if_false={"artifacts", "reports"},
            )

        if self.settings.post_cleanup:
            if self.settings.post_cleanup_purge:
                log.warning("Deleting flow run path %s", flow.run_path)
                rmtree(flow.run_path)
            else:
                log.warning("Cleaning up %s", flow.run_path)
                exclude = [settings_json, results_json]
                exclude += [
                    Path(p) if os.path.isabs(p) else flow.run_path / p
                    for p in flow.artifacts
                    if p and isinstance(p, (str, Path))
                ]
                paths_to_rm = unique(
                    [
                        p
                        for p in flow.run_path.glob("*")
                        if p not in exclude and self.xeda_run_dir.resolve() in p.resolve().parents
                    ]
                )
                log.warning(
                    "Removing the following files: %s", " ".join(str(p) for p in paths_to_rm)
                )
                for p in paths_to_rm:
                    if os.path.isfile(p):
                        os.remove(p)
                    elif os.path.isdir(p):
                        rmtree(p)

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
                raise FileNotFoundError(f"Cannot open xeda-project file: {xedaproject}")
        else:
            xedaproject = "xedaproject.toml"
        if design is not None:
            if isinstance(design, (Design, dict, Path)):
                design_not_in_project = True
            else:
                p = Path(design)
                if p.suffix.lower() in {".toml", ".json", ".yaml", ".yml"} and p.exists():
                    design_not_in_project = True
                    design = p
        if Path(xedaproject).exists():
            try:
                xeda_project = XedaProject.from_file(
                    xedaproject,
                    skip_designs=design_not_in_project,
                    design_overrides=design_overrides,
                    design_allow_extra=design_allow_extra,
                    design_remove_extra=design_remove_fields,
                )
            except FileNotFoundError:
                log.critical(
                    f"Cannot open project file: {xedaproject}. Try specifing the correct path using the --xedaproject <path-to-file>."
                )
                return None
            flows_settings = xeda_project.flows
        if design and design_not_in_project:
            if isinstance(design, (str, Path)):
                try:
                    design = Design.from_file(
                        design,
                        overrides=design_overrides,
                        allow_extra=design_allow_extra,
                        remove_extra=design_remove_fields,
                    )
                except DesignFileParseError as e:
                    log.critical(f"Error parsing design file {design}: {e}")
                    if self.debug:
                        raise e
                    return None
                except AnyDesignValidationException as e:
                    log.critical(f"Error validating design file {design}:\n{e}")
                    if self.debug or self.settings.debug:
                        raise e
                    return None

            elif isinstance(design, dict):
                design = dict(design)
                if "design_root" not in design:
                    design["design_root"] = Path.cwd()
                design = Design(**design)
        else:
            if not xeda_project:
                log.critical(
                    "No design file or project files were specified and no `xedaproject.toml` was found in the working directory."
                )
                return None
            if not xeda_project.designs:
                log.critical(
                    "There are no designs in the xedaproject file. You can specify a single design description using `--design-file` argument."
                )
                return None
            assert isinstance(xeda_project.design_names, list)  # type checker
            log.info(
                "Available designs in xedaproject: %s",
                ", ".join(xeda_project.design_names),
            )
            if isinstance(design, str):
                design_ = xeda_project.get_design(design)
                if design_:
                    design = design_
                else:
                    if design:
                        log.critical(
                            'Design "%s" not found in %s. Available designs are: %s',
                            design,
                            xedaproject,
                            ", ".join(xeda_project.design_names),
                        )
                        raise ValueError("Invalid design name")
                    else:
                        if len(xeda_project.designs) == 1:
                            design = xeda_project.get_design()
                        elif select_design_in_project:
                            design = select_design_in_project(xeda_project, design)
                    if not design:
                        log.critical(
                            "[ERROR] no design was specified and none were automatically discovered."
                        )
                        raise ValueError("no design was specified or discovered")
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
            flow_name = flow_class.name
        else:
            flow_name = flow.name
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
            flows_settings.get(flow_name),
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
