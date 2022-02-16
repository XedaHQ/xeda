from datetime import datetime
from pprint import pprint
import coloredlogs
import time
import logging
from types import SimpleNamespace
from typing import Dict, List
from pathlib import Path
from datetime import datetime
import logging
from pydantic.error_wrappers import ValidationError, display_errors

from ..tool import NonZeroExitCode
from ..flows.flow_gen import FlowGen, get_flow_class
from ..flows.design import Design, DesignError
from ..flows.flow import Flow, FlowSettingsError
from ..utils import dump_json

logger = logging.getLogger(__name__)


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

        coloredlogs.install('INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=root_logger)

        logger.info(f"Running using FlowRunner: {self.__class__.__name__}")

    def fatal(self, msg=None, exception=None):
        if msg:
            logger.critical(msg)
        if exception:
            raise exception
        else:
            raise Exception(msg)


    def run_flow(self, flow_class, design: Design, setting_overrides={}):
        logger.debug(f"run_flow {flow_class}")

        design.check()

        flow: Flow = FlowGen.generate(
            flow_class, design, self.xeda_run_dir, setting_overrides
        )

        failed = False

        flow.init()
        # print(flow.dependencies)
        for dep_cls, dep_settings in flow.dependencies:
            # merge with existing self.flows[dep].settings
            completed_dep = self.run_flow(dep_cls, dep_settings, design)
            if not completed_dep or not completed_dep.results['success']:
                logger.critical(f"Dependency flow {dep_cls.name} failed")
                raise Exception()  # TODO
            flow.completed_dependencies.append(completed_dep)
        try:
            flow.run()
        except NonZeroExitCode as e:
            logger.critical(
                f"Execution of {e.command_args[0]} returned {e.exit_code}")
            failed = True
            raise e from None

        if flow.init_time is not None:
            flow.results['runtime_minutes'] = (
                time.monotonic() - flow.init_time) / 60

        if not failed:
            flow.parse_reports()
            if not flow.results['success']:
                logger.error(f"Failure was reported in the parsed results.")
                failed = True
        if flow.artifacts:
            print(f"Generated artifacts in {flow.run_path}:")  # FIXME
            pprint(flow.artifacts)  # FIXME

        if failed:
            flow.results['success'] = False
            # set success=false if execution failed
            logger.critical(f"{flow.name} failed!")

        flow.results['design'] = flow.design.name
        flow.results['flow'] = flow.name

        path = flow.run_path / f'results.json'
        dump_json(flow.results, path)
        logger.info(f"Results written to {path}")

        flow.print_results()

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
