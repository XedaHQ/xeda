import hashlib
import importlib
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path, PosixPath
from typing import Any, Dict, List, Mapping, Optional, Set, Type, Union

from pathvalidate import sanitize_filename  # type: ignore # pyright: reportPrivateImportUsage=none
from rich import box, print_json
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..console import console
from ..dataclass import asdict, ValidationError, validation_errors
from ..design import Design
from ..flows.flow import (
    Flow,
    FlowDependencyFailure,
    registered_flows,
    FlowFatalException,
    FlowSettingsError,
)
from ..tool import NonZeroExitCode
from ..utils import (
    backup_existing,
    dict_merge,
    dump_json,
    snakecase_to_camelcase,
    try_convert,
    WorkingDirectory,
)
from ..version import __version__

log = logging.getLogger(__name__)


def print_results(
    flow: Flow,
    results: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    subset: Optional[Set[str]] = None,
    skip_if_empty: Optional[Set[str]] = {"artifacts", "reports"},
) -> None:
    if results is None:
        results = flow.results
    console.print()
    table = Table(
        title=title,
        title_style=Style(frame=True, bold=True),
        show_header=False,
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column(style="bold", no_wrap=True)
    table.add_column(justify="right")
    for k, v in results.items():
        if skip_if_empty and k in skip_if_empty and not v:
            continue
        elif v is not None and not k.startswith("_"):
            if k == "success":
                text = "OK :heavy_check_mark-text:" if v else "FAILED :cross_mark-text:"
                color = "green" if v else "red"
                table.add_row("Status", text, style=Style(color=color))
                continue
            if subset and k not in subset:
                continue
            if k == "design":
                table.add_row("Design Name", Text(v), style=Style(dim=True))
                continue
            if k == "flow":
                table.add_row("Flow Name", Text(v), style=Style(dim=True))
                continue
            if k == "runtime":
                table.add_row(
                    "Running Time",
                    str(timedelta(seconds=round(v))),
                    style=Style(dim=True),
                )
                continue
            if isinstance(v, float):
                v = f"{v:.3f}"
            table.add_row(k, str(v))
    console.print(table)


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__
) -> Type[Flow]:
    (mod, flow_class) = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        log.warning(
            f"Flow {flow_name} was not found in registered flows. Trying to load using importlib.import_module"
        )
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            log.critical(f"Unable to import {module_name} from {package}")
            raise e from None
        assert (
            module is not None
        ), f"Failed to load module {module_name} from package {package}"
        flow_class_name = snakecase_to_camelcase(flow_name)
        try:
            flow_class = getattr(module, flow_class_name)
        except AttributeError as e:
            log.critical(f"Unable to find class {flow_class_name} in module {module}")
            raise e from None
    assert flow_class is not None and issubclass(flow_class, Flow)
    return flow_class


def merge_overrides(
    overrides: Union[str, List[str], Flow.Settings, Dict[str, Any]],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    if overrides:
        if isinstance(overrides, str):
            overrides = re.split(r"\s*,\s*", overrides)
        if isinstance(overrides, list):
            for override in overrides:
                key, val = override.split("=")
                hier = key.split(".")
                patch_dict: Dict[str, Any] = dict()
                tmp = patch_dict
                for field in hier[:-1]:
                    tmp[field] = dict()
                    tmp = tmp[field]
                tmp[hier[-1]] = try_convert(val, convert_lists=True)
                settings = dict_merge(settings, patch_dict, True)
            return settings
        if isinstance(overrides, Flow.Settings):
            overrides = overrides.__dict__
        assert isinstance(
            overrides, dict
        ), f"overrides is of unsupported type: {type(overrides)}"
        log.info(f"Overriding the following flow settings: {overrides}")
        for k, v in overrides.items():
            settings[k] = v
    return settings


def semantic_hash(data: Any) -> str:
    def sorted_dict_str(data: Any) -> Any:
        if isinstance(data, Mapping):
            return {k: sorted_dict_str(data[k]) for k in sorted(data.keys())}
        elif isinstance(data, list):
            return [sorted_dict_str(val) for val in data]
        elif hasattr(data, "__dict__"):
            return sorted_dict_str(data.__dict__)
        else:
            return str(data)

    def get_digest(b: bytes) -> str:
        return hashlib.sha1(b).hexdigest()[:16]

    return get_digest(bytes(repr(sorted_dict_str(data)), "UTF-8"))


class FlowRunner:
    def __init__(
        self,
        xeda_run_dir: Union[str, os.PathLike[Any]] = "xeda_run",
        unique_rundir: bool = False,  # FIXME: rename + test + doc
        debug: bool = False,
        dump_settings_json: bool = True,
        print_results: bool = True,
        dump_results_json: bool = True,
        run_in_existing_dir: bool = False,  # Not recommended!
    ) -> None:
        if debug:
            log.setLevel(logging.DEBUG)
        self.debug = debug
        log.debug("%s xeda_run_dir=%s", self.__class__.__name__, xeda_run_dir)
        xeda_run_dir = Path(xeda_run_dir).resolve()
        xeda_run_dir.mkdir(exist_ok=True, parents=True)
        self.xeda_run_dir: Path = xeda_run_dir
        self.unique_rundir: bool = unique_rundir
        self.print_results: bool = print_results
        self.dump_results_json: bool = dump_results_json
        self.dump_settings_json: bool = dump_settings_json
        self.run_in_existing_dir: bool = run_in_existing_dir

    def _get_flow_run_path(
        self,
        design_name: str,
        flow_name: str,
        design_hash: Optional[str] = None,
        flowrun_hash: Optional[str] = None,
    ) -> Path:
        design_subdir = design_name
        flow_subdir = flow_name
        if self.unique_rundir:
            if design_hash:
                design_subdir += f"_{design_hash}"
            if flowrun_hash:
                flow_subdir += f"_{flowrun_hash}"

        run_path: Path = (
            self.xeda_run_dir / sanitize_filename(design_subdir) / flow_subdir
        )
        if not self.run_in_existing_dir and run_path.exists():
            backup_existing(run_path)
        run_path.mkdir(parents=True, exist_ok=True)
        return run_path

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
    ) -> Flow:

        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        flow_class = flow_class
        if flow_settings is None:
            flow_settings = {}
        elif isinstance(flow_settings, Flow.Settings):
            flow_settings = asdict(flow_settings)
        try:
            flow_settings = flow_class.Settings(**flow_settings)
        except ValidationError as e:
            raise FlowSettingsError(
                flow_class, validation_errors(e.errors()), e.model
            ) from None
        flow_name = flow_class.name

        # TODO is this needed anymore?
        design.check()

        design_hash = semantic_hash(design)
        flowrun_hash = semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                xeda_version=__version__,
            )
        )
        run_path = self._get_flow_run_path(
            design.name,
            flow_name,
            design_hash,
            flowrun_hash,
        )
        if self.dump_settings_json:
            settings_json_path = run_path / "settings.json"
            log.info(f"dumping effective settings to {settings_json_path}")
            all_settings = dict(
                design=design,
                design_hash=design_hash,
                flow_name=flow_name,
                flow_settings=flow_settings,
                xeda_version=__version__,
                flowrun_hash=flowrun_hash,
            )
            dump_json(all_settings, settings_json_path)

        with WorkingDirectory(run_path):
            log.debug("Instantiating flow from %s", flow_class)
            flow = flow_class(flow_settings, design, run_path)
            flow.design_hash = design_hash
            flow.flow_hash = flowrun_hash
            flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            try:
                flow.init()
            except ValidationError as e:
                raise FlowSettingsError(
                    flow_class, validation_errors(e.errors()), e.model
                ) from None

        if flow.dependencies:
            log.debug(
                "%s dependencies: %s",
                flow.name,
                ", ".join(f.name for f, _ in flow.dependencies),
            )
            for dep_cls, dep_settings in flow.dependencies:
                # merge with existing self.flows[dep].settings
                # NOTE this allows dependency flow to make changes to 'design'
                completed_dep = self.run_flow(dep_cls, design, dep_settings)
                if not completed_dep:
                    log.critical(f"Dependency flow {dep_cls.name} failed")
                    raise FlowDependencyFailure()  # TODO
                flow.completed_dependencies.append(completed_dep)

        flow.init_time = time.monotonic()
        flow.results["design"] = flow.design.name
        flow.results["flow"] = flow.name

        success = True
        with WorkingDirectory(run_path):
            try:
                flow.run()
            except NonZeroExitCode as e:
                log.critical(f"Execution of {e.command_args[0]} returned {e.exit_code}")
                success = False
            if flow.init_time is not None:
                flow.results.runtime = time.monotonic() - flow.init_time
            if success:
                flow.results.success = flow.parse_reports()
                success &= flow.results.success
                if not success:
                    log.error(f"Failure was reported in the parsed results.")
        if flow.artifacts:

            def default_encoder(x: Any) -> str:
                if isinstance(x, PosixPath):
                    return str(x.relative_to(flow.run_path))
                return str(x)

            print(f"Generated artifacts in {flow.run_path}:")  # FIXME
            print_json(data=flow.artifacts, default=default_encoder)  # FIXME

        if not success:
            flow.results.success = False
            # set success=false if execution failed
            log.critical(f"{flow.name} failed!")

        if self.dump_results_json:
            results_path = run_path / "results.json"
            dump_json(flow.results, results_path)
            log.info(f"Results written to {results_path}")

        if self.print_results:
            print_results(flow, title=f"{flow.name} Results")
        return flow


class DefaultRunner(FlowRunner):
    """Executes a flow and its dependencies and then reports selected results"""
