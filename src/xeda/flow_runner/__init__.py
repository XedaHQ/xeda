"""Flow runner"""
import hashlib
import importlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path, PosixPath
from typing import Any, Dict, Mapping, Optional, Set, Type, Union
from box import Box

# fmt: off
from pathvalidate import sanitize_filename  # pyright: reportPrivateImportUsage=none

from rich import box, print_json
from rich.style import Style
from rich.table import Table
from rich.text import Text

from ..console import console
from ..dataclass import asdict
from ..design import Design
from ..flows.flow import Flow, FlowDependencyFailure, registered_flows
from ..tool import NonZeroExitCode
from ..utils import WorkingDirectory, backup_existing, dump_json, snakecase_to_camelcase
from ..version import __version__

__all__ = [
    "get_flow_class",
    "FlowNotFoundError",
    "FlowRunner",
    "DefaultRunner",
    "print_results",
]

log = logging.getLogger(__name__)


def print_results(
    flow: Flow,
    results: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    subset: Optional[Set[str]] = None,
    skip_if_empty: Optional[Set[str]] = None,
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
        if v is not None and not k.startswith("_"):
            if k == "success":
                # text = "OK :heavy_check_mark-text:" if v else "FAILED :cross_mark-text:"

                # color = "green" if v else "red"
                # table.add_row("Status", text, style=Style(color=color))
                table.add_row("Status", "[green]OK[/green]" if v else "[red]FAILED[/red]")
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


class FlowNotFoundError(Exception):
    pass


def get_flow_class(
    flow_name: str, module_name: str = "xeda.flows", package: str = __package__
) -> Type[Flow]:
    _mod, flow_class = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        log.warning(
            "Flow %s was not found in registered flows. Trying to load using importlib.import_module",
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


def _semantic_hash(data: Any) -> str:
    def _sorted_dict_str(data: Any) -> Any:
        if isinstance(data, (dict, Mapping)):
            return {k: _sorted_dict_str(data[k]) for k in sorted(data.keys())}
        if isinstance(data, list):
            return [_sorted_dict_str(val) for val in data]
        if hasattr(data, "__dict__"):
            return _sorted_dict_str(data.__dict__)
        return str(data)

    r = repr(_sorted_dict_str(data))
    return hashlib.sha3_256(bytes(r, "UTF-8")).hexdigest()


class FlowRunner:
    """
    Manage running flows and their dependencies.
    1. Instantiate instance of flow class with proper settings assigned (__init__)
    2. call Flow.init()
    3. Run all dependency flows (asked by the flow, during Flow.init)
    3. Run the flow by calling run()
    4. Run flow's parse_reports() ## TODO parse_reports will be renamed
    5. Evaluate and print the results
    """

    def __init__(
        self,
        xeda_run_dir: Union[str, os.PathLike] = "xeda_run",
        debug: bool = False,
        dump_settings_json: bool = True,
        display_results: bool = True,
        dump_results_json: bool = True,
        cached_dependencies: bool = False,  # do not run dependencies if previous run results exist. Uses flow run_dir names including design and flow.settings hashes
        run_in_existing_dir: bool = False,  # DO NOT USE! Only for development!
    ) -> None:
        if debug:
            log.setLevel(logging.DEBUG)
            log.root.setLevel(logging.DEBUG)
        self.debug = debug
        log.debug("%s xeda_run_dir=%s", self.__class__.__name__, xeda_run_dir)
        xeda_run_dir = Path(xeda_run_dir).resolve()
        xeda_run_dir.mkdir(exist_ok=True, parents=True)
        self.xeda_run_dir: Path = xeda_run_dir
        self.cached_dependencies: bool = cached_dependencies
        self.display_results: bool = display_results
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
        if self.cached_dependencies:
            if design_hash:
                design_subdir += f"_{design_hash[:16]}"
            if flowrun_hash:
                flow_subdir += f"_{flowrun_hash[:16]}"

        run_path: Path = (
            self.xeda_run_dir / sanitize_filename(design_subdir) / flow_subdir
        )
        return run_path

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
    ) -> Flow:
        return self._run_flow(flow_class, design, flow_settings, None)

    def _run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings],
        depender: Optional[Flow],
    ) -> Flow:
        if self.run_in_existing_dir:
            log.error(
                "run_in_existing_dir should only be used during Xeda's development!"
            )
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)
        if flow_settings is None:
            flow_settings = {}
        elif isinstance(flow_settings, Flow.Settings):
            flow_settings = asdict(flow_settings)
        if self.debug:
            print("flow_settings: ", flow_settings)
        flow_settings = flow_class.Settings(**flow_settings)
        if self.debug:
            flow_settings.debug = True

        flow_name = flow_class.name
        # GOTCHA: design contains tb settings even for simulation flows
        # OTOH removing tb from hash for sim flows creates a mismatch for different flows of the same design
        design_hash = _semantic_hash(
            dict(
                # design=design,
                rtl_hash=design.rtl_hash,  # TODO WHY?!!
                tb_hash=design.tb_hash
            )
        )
        flowrun_hash = _semantic_hash(
            dict(
                flow_name=flow_name,
                flow_settings=flow_settings,
                xeda_version=__version__,
            ),
        )
        run_path = self._get_flow_run_path(
            design.name,
            flow_name,
            design_hash,
            flowrun_hash,
        )

        settings_json = run_path / "settings.json"
        results_json = run_path / "results.json"

        previous_results = None
        if (
            depender
            and self.cached_dependencies
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
                        "%s does not contain the expected settings", prev_settings
                    )
            else:
                log.warning(
                    "Could not find valid results/settings from a previous run in %s",
                    run_path,
                )

        if not previous_results:
            if not self.run_in_existing_dir and run_path.exists():
                backup_existing(run_path)
            run_path.mkdir(parents=True)

            if self.dump_settings_json:
                log.info("dumping effective settings to %s", settings_json)
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
                dump_json(all_settings, settings_json)

        with WorkingDirectory(run_path):
            log.debug("Instantiating flow from %s", flow_class)
            flow = flow_class(flow_settings, design, run_path)
            flow.design_hash = design_hash
            flow.flow_hash = flowrun_hash
            flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            # flow execution time includes init() as well as execution of all its dependency flows
            flow.init_time = time.monotonic()
            flow.init()

        for dep_cls, dep_settings in flow.dependencies:
            # merge with existing self.flows[dep].settings
            # NOTE this allows dependency flow to make changes to 'design'
            log.info(
                "Running dependency: %s (%s.%s)",
                dep_cls.name,
                dep_cls.__module__,
                dep_cls.__qualname__,
            )
            completed_dep = self._run_flow(dep_cls, design, dep_settings, depender=flow)
            if not completed_dep.succeeded:
                log.critical("Dependency flow: %s failed!", dep_cls.name)
                raise FlowDependencyFailure()
            flow.completed_dependencies.append(completed_dep)

        flow.results["design"] = flow.design.name
        flow.results["flow"] = flow.name
        success = True

        if previous_results:
            log.warning("Using previous run results and artifacts from %s", run_path)
            flow.results.update(**previous_results)
        else:
            with WorkingDirectory(run_path):
                try:
                    flow.run()
                except NonZeroExitCode as e:
                    log.critical(
                        "Execution of %s returned %d", e.command_args[0], e.exit_code
                    )
                    success = False
                if flow.init_time is not None:
                    flow.results.runtime = time.monotonic() - flow.init_time
                try:
                    success &= flow.parse_reports()
                except Exception as e:  # pylint: disable=broad-except
                    log.critical("parse_reports throw an exception: %s", e)
                    if success:  # if so far so good this is a bug!
                        raise e from None
                    success = False
                if not success:
                    log.error("Failure was reported in the parsed results.")
                flow.results.success = success

        if flow.artifacts and flow.succeeded:
            def default_encoder(x: Any) -> str:
                if isinstance(x, (PosixPath, os.PathLike)):
                    return str(os.path.relpath(x, flow.run_path))
                return str(x)

            print(f"Generated artifacts in {flow.run_path}:")  # FIXME
            print_json(data=flow.artifacts, default=default_encoder)  # FIXME

        if not success:
            # set success=false if execution failed
            log.critical("%s failed!", flow.name)

        if self.dump_results_json:
            dump_json(flow.results, results_json)
            log.info("Results written to %s", results_json)

        if self.display_results:
            print_results(
                flow,
                title=f"{flow.name} Results",
                skip_if_empty={"artifacts", "reports"},
            )
        return flow


class DefaultRunner(FlowRunner):
    """Executes a flow and its dependencies and then reports selected results"""
