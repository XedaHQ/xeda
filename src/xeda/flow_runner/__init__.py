from datetime import datetime
import coloredlogs
import time
import logging
from pathlib import Path, PosixPath
from datetime import datetime, timedelta
from typing import Mapping, Optional, Type, Any, Union
import importlib
import hashlib
import re
from pathvalidate import sanitize_filename
from rich import box, print_json
from rich.table import Table
from rich.style import Style
from rich.text import Text

from ..console import console
from ..flows.flow import Flow, Design, registered_flows
from ..tool import NonZeroExitCode
from ..flows.design import Design
from ..flows.flow import Flow
from ..utils import dict_merge, snakecase_to_camelcase, dump_json, try_convert, backup_existing

from .._version import get_versions
__version__ = get_versions()['version']
del get_versions

log = logging.getLogger(__name__)


def print_results(flow: Flow, results=None):
    if results is None:
        results = flow.results
    console.print()
    table = Table(title="Results", title_style=Style(frame=True, bold=True),
                  show_header=False,
                  box=box.ROUNDED,
                  show_lines=True,
                  )
    table.add_column(style="bold", no_wrap=True)
    table.add_column(justify="right")
    for k, v in results.items():
        if v is not None and not k.startswith('_'):
            if k == 'success':
                text = "OK :heavy_check_mark-text:" if v else "FAILED :cross_mark-text:"
                color = 'green' if v else 'red'
                table.add_row("Status", text, style=Style(color=color))
                continue
            if k == 'design':
                table.add_row("Design Name", Text(v), style=Style(dim=True))
                continue
            if k == 'flow':
                table.add_row("Flow Name", Text(v), style=Style(dim=True))
                continue
            if k == 'runtime':
                table.add_row("Running Time", str(timedelta(seconds=int(v))), style=Style(dim=True))
                continue
            if isinstance(v, float):
                v = f'{v:.3f}'
            table.add_row(k, str(v))
    console.print(table)


def get_flow_class(flow_name: str, module_name: str, package: str) -> Type[Flow]:
    (mod, flow_class) = registered_flows.get(flow_name, (None, None))
    if flow_class is None:
        log.warning(
            f"Flow {flow_name} was not found in registered flows. Trying to load using importlib.import_module")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            log.critical(
                f"Unable to import {module_name} from {package}")
            raise e from None
        assert module is not None, f"Failed to load module {module_name} from package {package}"
        flow_class_name = snakecase_to_camelcase(flow_name)
        try:
            flow_class = getattr(module, flow_class_name)
        except AttributeError as e:
            log.critical(
                f"Unable to find class {flow_class_name} in module {module}"
            )
            raise e from None
    assert flow_class is not None and issubclass(flow_class, Flow)
    return flow_class


def merge_overrides(overrides, settings):
    if overrides:
        if isinstance(overrides, str):
            overrides = re.split(r'\s*,\s*', overrides)

        if isinstance(overrides, list):
            for override in overrides:
                key, val = override.split('=')
                hier = key.split('.')
                patch_dict = dict()
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
            overrides, dict), f"overrides is of type {type(overrides)}"
        for k, v in overrides.items():
            settings[k] = v
    return settings


def semantic_hash(data: Any) -> str:
    def get_digest(b: bytes):
        return hashlib.sha1(b).hexdigest()[:16]

    # data: JsonType, not adding type as Pylance does not seem to like recursive types :/
    def sorted_dict_str(data):
        if isinstance(data, Mapping):
            return {k: sorted_dict_str(data[k]) for k in sorted(data.keys())}
        elif isinstance(data, list):
            return [sorted_dict_str(val) for val in data]
        elif hasattr(data, '__dict__'):
            return sorted_dict_str(data.__dict__)
        else:
            return str(data)

    return get_digest(bytes(repr(sorted_dict_str(data)), 'UTF-8'))


def get_settings_schema(flow_name: str, module_name: str, package: str = __package__):
    flow_class = get_flow_class(flow_name, module_name, package)
    return flow_class.Settings.schema(by_alias=False)


def generate(flow_class, design: Design, xeda_run_dir: Path, override_settings: Union[Mapping[str, Any], Flow.Settings]) -> Flow:
    flow_name = flow_class.name
    if isinstance(override_settings, Flow.Settings):
        override_settings = override_settings.dict()
    flow_settings = flow_class.Settings(**override_settings)
    design_hash = semantic_hash(design)
    flowrun_hash = semantic_hash(dict(
        flow_name=flow_name, flow_settings=flow_settings, xeda_version=__version__
    ))

    results_dir = xeda_run_dir / 'Results' / flow_name
    results_dir.mkdir(exist_ok=True, parents=True)
    design_subdir = f"{design.name}"
    flow_subdir = flow_name
    if flow_settings.unique_rundir:
        design_subdir += f'_{design_hash}'
        flow_subdir += f'_{flowrun_hash}'

    run_path: Path = xeda_run_dir / \
        sanitize_filename(design_subdir) / flow_subdir
    if run_path.exists():
        # run_path.rename()
        backup_existing(run_path)
    run_path.mkdir(parents=True, exist_ok=True)

    settings_json_path = run_path / f'settings.json'
    log.info(f'dumping effective settings to {settings_json_path}')
    all_settings = dict(
        design=design,
        design_hash=design_hash,
        flow_name=flow_name,
        flow_settings=flow_settings,
        xeda_version=__version__,
        flowrun_hash=flowrun_hash
    )
    dump_json(all_settings, settings_json_path)

    reports_dir = run_path / flow_settings.reports_subdir_name
    reports_dir.mkdir(exist_ok=True)

    flow = flow_class(flow_settings, design, run_path)

    flow.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    flow.init_time = time.monotonic()
    flow.design_hash = design_hash
    flow.flow_hash = flowrun_hash

    return flow


class FlowRunner:
    def __init__(self, _xeda_run_dir='xeda_run') -> None:
        xeda_run_dir = Path(_xeda_run_dir).resolve()
        xeda_run_dir.mkdir(exist_ok=True, parents=True)
        self.xeda_run_dir = xeda_run_dir
        self.install_file_logger(self.xeda_run_dir / 'Logs')

    def install_file_logger(self, logdir: Path):
        logdir.mkdir(exist_ok=True, parents=True)

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        logFormatter = logging.Formatter(
            "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")

        logfile = logdir / f"xeda_{timestamp}.log"
        print(f"Logging to {logfile}")

        fileHandler = logging.FileHandler(logfile)
        fileHandler.setFormatter(logFormatter)
        root_logger = logging.getLogger()
        root_logger.addHandler(fileHandler)

        coloredlogs.install(
            'INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=root_logger)

        log.info(f"Running using FlowRunner: {self.__class__.__name__}")

    def fatal(self, msg=None, exception=None):
        if msg:
            log.critical(msg)
        if exception:
            raise exception
        else:
            raise Exception(msg)

    def run_flow(self, flow_class: Type[Flow], design: Design, setting_overrides: Mapping[str, Any] = {}) -> Optional[Flow]:
        log.debug(f"run_flow {flow_class}")

        design.check()

        flow: Flow = generate(
            flow_class, design, self.xeda_run_dir, setting_overrides
        )

        failed = False

        flow.results['design'] = flow.design.name
        flow.results['flow'] = flow.name

        flow.init()
        # print(flow.dependencies)
        for dep_cls, dep_settings in flow.dependencies:
            # merge with existing self.flows[dep].settings
            completed_dep = self.run_flow(dep_cls, design, dep_settings)
            if not completed_dep or not completed_dep.results['success']:
                log.critical(f"Dependency flow {dep_cls.name} failed")
                raise Exception()  # TODO
            flow.completed_dependencies.append(completed_dep)
        try:
            flow.run()
        except NonZeroExitCode as e:
            log.critical(
                f"Execution of {e.command_args[0]} returned {e.exit_code}")
            failed = True
            raise e from None

        if flow.init_time is not None:
            flow.results['runtime'] = time.monotonic() - flow.init_time

        if not failed:
            flow.parse_reports()
            if not flow.results['success']:
                log.error(f"Failure was reported in the parsed results.")
                failed = True

        def default_encoder(x):
            if isinstance(x, PosixPath):
                return str(x.relative_to(flow.run_path))
            return str(x)

        if flow.artifacts:
            print(f"Generated artifacts in {flow.run_path}:")  # FIXME
            print_json(data=flow.artifacts, default=default_encoder)  # FIXME

        if failed:
            flow.results['success'] = False
            # set success=false if execution failed
            log.critical(f"{flow.name} failed!")

        path = flow.run_path / f'results.json'
        dump_json(flow.results, path)
        log.info(f"Results written to {path}")

        print_results(flow)

        return flow

        # flow.parse_reports()
        # flow.results['timestamp'] = flow.timestamp
        # flow.results['design.name'] = flow.settings.design['name']
        # flow.results['flow.name'] = flow.name
        # flow.results['flow.run_hash'] = flow.xedahash

        # if print_failed or flow.results.get('success'):
        #     flow.print_results()
        # flow.dump_results()

        # design_settings = dict(
        #     design=get_design(xeda_project['design']),
        #     flows=xeda_project.get('flows', {})
        # )

    # should not override

    def post_run(self, flow: Flow, print_failed=True):
        pass


class DefaultRunner(FlowRunner):
    pass
