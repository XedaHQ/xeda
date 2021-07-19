from datetime import datetime
import coloredlogs
import os
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence
from pathlib import Path
from datetime import datetime
import logging

from pydantic.error_wrappers import ValidationError, display_errors

from ..flows.flow_gen import FlowGen
from ..flows.design import Design, DesignError
from ..flows.flow import Flow

logger = logging.getLogger()



class FlowRunner:
    def __init__(self, args: SimpleNamespace, xeda_project: Dict[str, Any]) -> None:
        self.consolidate_settings(args, xeda_project)

        self.selected_design = args.design

        if args.xeda_run_dir is None:
            rundir = xeda_project.get('xeda_run_dir')
            if rundir is None or not isinstance(rundir, str):
                project = xeda_project.get('project')
                if project:
                    rundir = project.get('xeda_run_dir')
                if not rundir:
                    rundir = os.environ.get('XEDA_RUN_DIR')
                if not rundir:
                    rundir = 'xeda_run'
            args.xeda_run_dir = rundir

        xeda_run_dir = Path(args.xeda_run_dir).resolve()
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
        logger.addHandler(fileHandler)

        coloredlogs.install(
            'INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

        logger.info(f"Running using FlowRunner: {self.__class__.__name__}")

    def fatal(self, msg=None, exception=None):
        if msg:
            logger.critical(msg)
        if exception:
            raise exception
        else:
            raise Exception(msg)

    def get_design(self, design_name: str) -> Dict[str, Any]:
        designs = self.designs
        if design_name:

            for x in designs:
                if x['name'] == design_name:
                    return x
            logger.critical(
                f'Design "{design_name}" not found in the current project.')
        elif len(designs) == 1:
            return designs[0]
        else:
            raise Exception(
                f'Please specify target design using --design. Available designs: {", ".join([x["name"] for x in designs])}')

        return {}

    def consolidate_settings(self, args: SimpleNamespace, xeda_project: Dict[str, Any]):
        # settings = dict_merge(settings, design_settings)

        # settings = merge_overrides(args.override_settings, settings)
        # flow_settings = settings['flows'].get(args.flow, dict())
        # settings['flows'][args.flow] = merge_overrides(
        #     args.override_flow_settings, flow_settings)

        # settings['design']['xeda_version'] = xeda_project['xeda_version']
        designs = xeda_project['design']
        if not isinstance(designs, Sequence):
            designs = [designs]
        self.designs = designs
        self.flows = xeda_project.get('flows', {})


    def launch(self, flow_name: str, force_run: bool) -> List[Flow]:
        """
        runs the flow and returns the completed flow object
        """

        if force_run:
            logger.info(f"Forced re-run of {flow_name}")

        design_settings = self.get_design(self.selected_design)

        logger.info(f"design_settings={design_settings}")

        flow_gen = FlowGen(self.flows)

        try:
            design: Design = Design(**design_settings)
        except ValidationError as e:
            errors = e.errors()
            raise DesignError(f"{len(errors)} errors while parsing `design` settings:\n\n{display_errors(errors)}\n") from None

        completed_dependencies: List[Flow] = []

        flow: Flow = flow_gen.generate(
            flow_name, "xeda.flows", design, self.xeda_run_dir, completed_dependencies=completed_dependencies)
        flow.run()
        flow.parse_reports()
        flow.dump_results()
        flow.print_results()
        flow.create_symlink_to_latest_run();
        
        completed_dependencies.append(flow)

        return completed_dependencies

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
        # Run post-run hooks
        # for hook in flow.post_run_hooks:
        #     logger.info(
        #         f"Running post-run hook from from {hook.__class__.__name__}")
        #     hook(flow)

        # flow.reports_dir = flow.flow_run_dir / flow.reports_subdir_name
        # if not flow.reports_dir.exists():
        #     flow.reports_dir.mkdir(parents=True)

        # flow.parse_reports()
        # flow.results['timestamp'] = flow.timestamp
        # flow.results['design.name'] = flow.settings.design['name']
        # flow.results['flow.name'] = flow.name
        # flow.results['flow.run_hash'] = flow.xedahash

        # if print_failed or flow.results.get('success'):
        #     flow.print_results()
        # flow.dump_results()

        # # Run post-results hooks
        # for hook in flow.post_results_hooks:
        #     logger.info(
        #         f"Running post-results hook from {hook.__class__.__name__}")
        #     hook(flow)
