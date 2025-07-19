"""Launch execution of flows"""

from __future__ import annotations

import importlib
import json
import logging
import os
from pprint import PrettyPrinter
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Type, TypeVar, Union

from box import Box
from pathvalidate import sanitize_filename
from rich import box
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..console import console
from ..dataclass import XedaBaseModel
from ..design import Design, DesignFileParseError, AnyDesignValidationException
from ..flow import Flow, FlowDependencyFailure, registered_flows
from ..tool import NonZeroExitCode
from ..utils import (
    WorkingDirectory,
    backup_existing,
    dump_json,
    settings_to_dict,
    snakecase_to_camelcase,
    unique,
    semantic_hash
)
from ..version import __version__
from ..xedaproject import XedaProject

__all__ = [
    "get_flow_class",
    "FlowNotFoundError",
    "FlowRunner",
    "DefaultRunner",
    "print_results",
    "add_file_logger",
]

log = logging.getLogger(__name__)

DIR_NAME_HASH_LEN = 16


def print_results(
    flow: Optional[Flow] = None,
    results: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    subset: Optional[Iterable[str]] = None,
    skip_if_false: Union[None, bool, Iterable[str]] = None,
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
    pass


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__ or "xeda"
) -> Type[Flow]:
    _mod, flow_class = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        log.debug(
            "Flow %s was not found in registered flows. Trying to load using `importlib`.",
            flow_name,
        )
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise FlowNotFoundError() from e
        flow_class_name = snakecase_to_camelcase(flow_name)
        if module:
            try:
                flow_class = getattr(module, flow_class_name)
            except AttributeError:
                pass
        if not flow_class or not issubclass(flow_class, Flow):
            raise FlowNotFoundError()
    return flow_class


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

    def __init__(self, xeda_run_dir: Union[None, str, Path] = None, **kwargs) -> None:
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
        flow_settings: Union[None, Dict[str, Any], Flow.Settings],
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        run_path: Optional[Path] = None,
        all_flows_settings: Union[None, Dict] = None,
    ) -> Flow:
        """
        Low-level interface for launching flows.
        """
        self.debug |= self.settings.debug
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_name = flow_class.name
        if flow_settings is None:
            flow_settings = {}
        runner_cwd = Path.cwd()
        if isinstance(flow_settings, dict):
            flow_settings["runner_cwd_"] = runner_cwd
            flow_settings = flow_class.Settings(**flow_settings)
        elif isinstance(flow_settings, Flow.Settings):
            flow_settings.runner_cwd_ = runner_cwd
        assert isinstance(flow_settings, Flow.Settings)
        if self.debug:
            log.debug(
                "Flow '%s' settings: %s",
                flow_name,
                flow_settings.json(exclude_unset=True, indent=2),
            )
        if self.debug:
            flow_settings.debug = True

        copy_resources = [
            res for res in copy_resources if os.path.exists(res) and os.path.isfile(res)
        ]

        # GOTCHA: design contains tb settings even for simulation flows
        # OTOH removing tb from hash for sim flows creates a mismatch for different flows of the same design
        design_hash = semantic_hash(
            dict(
                rtl_hash=design.rtl_hash,
                tb_hash=design.tb_hash,
            )
        )
        flowrun_hash = semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                # copied_resources=[FileResource(res) for res in copy_resources],
                # xeda_version=__version__,
            ),
        )
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

        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"

        previous_results = None
        if (
            (depender or self.settings.skip_if_previous_run_exists)
            and self.settings.cached_dependencies
            and run_path.exists()
            and settings_json.exists()
            and results_json.exists()
        ):
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
                    previous_results = Box(prev_results)
                else:
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

        with WorkingDirectory(run_path):
            log.debug("Instantiating flow from %s", flow_class)
            flow = flow_class(flow_settings, design, run_path, runner_cwd=runner_cwd)

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
                log.info("writing effective settings to %s", settings_json)
                all_settings = dict(
                    design=design,
                    design_hash=design_hash,
                    rtl_fingerprint=design.rtl_fingerprint,
                    rtl_hash=design.rtl_hash,
                    flow_name=flow_name,
                    flow_settings=flow_settings,
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
            for dep_cls, dep_settings, dep_resources in flow.dependencies:
                # NOTE this allows dependency flow to make changes to 'design'
                # merge with existing self.flows[dep].settings
                dep_cls_name = dep_cls if isinstance(dep_cls, str) else dep_cls.name
                if isinstance(dep_cls, str):
                    dep_cls = get_flow_class(dep_cls)
                if all_flows_settings and dep_cls_name in all_flows_settings:
                    meta_dep_settings = dep_cls.Settings(**all_flows_settings[dep_cls_name])
                    md = meta_dep_settings.dict(
                        exclude_defaults=True, exclude_unset=True
                    )  # post validation
                    if dep_settings is None:
                        dep_settings = meta_dep_settings
                    dsd = dep_settings.dict(exclude_defaults=True, exclude_unset=True)
                    for field_name, field_info in dep_settings.__fields__.items():
                        # if field is not already overriden in the dependent flow settings (dep_settings) and exists in the top level (meta_dep_settings)
                        if (
                            field_name in md
                            and field_name not in dsd
                            and field_name in meta_dep_settings.__fields__
                        ):
                            meta_val = md[field_name]
                            if field_info.default != meta_val:
                                log.warning(
                                    "Updating dependent flow %s '%s' unset setting with the value '%s' from the top level 'flows' settings",
                                    dep_cls_name,
                                    field_name,
                                    meta_val,
                                )
                                setattr(dep_settings, field_name, meta_val)
                log.info(
                    "Running dependency: %s (%s.%s)",
                    dep_cls.name,
                    dep_cls.__module__,
                    dep_cls.__qualname__,
                )
                resources: List[str] = []
                if dep_settings is None:
                    dep_settings = dep_cls.Settings()
                dep_settings.debug |= flow.settings.debug
                if not dep_settings.verbose and flow.settings.verbose > 1:
                    dep_settings.verbose = flow.settings.verbose
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
                if not success and not flow_settings.quiet:
                    log.debug("Failure was reported in the parsed results.")
                flow.results.success = success
                flow.results.timestamp = flow.timestamp

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
        return flow

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings],
        depender: Optional[Flow] = None,
        copy_resources: List[str] = [],
        run_path: Optional[Path] = None,
        all_flows_settings: Union[None, Dict] = None,
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
        flow_settings: Union[  # FIXME: this should only set defaults if not available in design/project
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
        design_overrides: Union[None, Iterable[str], Dict[str, Any]] = None,
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
                if p.suffix in [".toml", ".json"] and p.exists():
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
                if "design_root" not in design:
                    design["design_root"] = Path.cwd()
                design = Design(**design)
            flows_settings = {
                **flows_settings,
                **design.flow,
            }
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
            flow_settings = flow_settings.dict()
        else:
            assert isinstance(
                flow_settings, (list, tuple, dict)
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
            flow_name = flow
            flow_class = get_flow_class(flow)
        else:
            flow_name = flow.name
            flow_class = flow

        final_flow_settings = flows_settings.get(flow_name, {})
        if not isinstance(final_flow_settings, dict):
            final_flow_settings = settings_to_dict(final_flow_settings)
        if flow_settings:
            final_flow_settings.update(flow_settings)
        if flow_overrides:
            log.debug("flow_overrides: %s", flow_overrides)
            final_flow_settings.update(flow_overrides)
        if not design or not flow_class:
            log.critical("Failed to parse design and/or flow")
            raise ValueError(f"design={design} flow_class={flow_class}")
        assert isinstance(
            design, Design
        ), f"BUG: design should be of type Design but was {type(design)}"
        if self.settings.debug:
            log.info("design: %s" % PrettyPrinter().pformat(design.dict()))
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


def add_file_logger(logdir: Union[Path, str], timestamp: Union[None, str, datetime] = None):
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
