# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from datetime import datetime
import inspect
import multiprocessing
import os
from pathlib import Path
import sys
import argparse
from .flows.flow import Flow, SimFlow, SynthFlow
from .utils import camelcase_to_snakecase, load_class
import coloredlogs
import logging
import pkg_resources

from .debug import DebugLevel
from .flow_runner import DefaultRunner, FlowRunner
# from .plugins.lwc import LwcVariantsRunner
import toml

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# def tomlkit_to_popo(d):
#     try:
#         result = getattr(d, "value")
#     except AttributeError:
#         result = d

#     if isinstance(result, list):
#         result = [tomlkit_to_popo(x) for x in result]
#     elif isinstance(result, dict):
#         result = {
#             tomlkit_to_popo(key): tomlkit_to_popo(val) for key, val in result.items()
#         }
#     elif isinstance(result, tomlkit.items.Integer):
#         result = int(result)
#     elif isinstance(result, tomlkit.items.Float):
#         result = float(result)
#     elif isinstance(result, tomlkit.items.String):
#         result = str(result)
#     elif isinstance(result, tomlkit.items.Bool):
#         result = bool(result)

#     return result


try:
    __version__ = pkg_resources.get_distribution(__package__).version
except pkg_resources.DistributionNotFound:
    __version__ = '(N/A - Local package)'


class XedaApp:
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            prog=__package__,
            description=f'{__package__}: Cross-EDA abstraction and automation. Version: {__version__}')
        parsed_args = None

        # TODO registered plugins
        self.flow_classes = inspect.getmembers(sys.modules['xeda.flows'],
                                               lambda cls: inspect.isclass(cls) and issubclass(cls, Flow) and cls != Flow and cls != SimFlow and cls != SynthFlow)
        self.runner_classes = inspect.getmembers(sys.modules['xeda.flow_runner'],
                                                 lambda cls: inspect.isclass(cls) and issubclass(cls, FlowRunner) and cls != FlowRunner)

    def main(self, args = None):
        parsed_args = self.parse_args(args)

        if parsed_args.debug:
            logger.setLevel(logging.DEBUG)

        runner_cls = parsed_args.flow_runner

        toml_path = parsed_args.xeda_project if parsed_args.xeda_project else Path.cwd() / 'xedaproject.toml'
        xeda_project = {}
        try:
            with open(toml_path) as f:
                xeda_project = toml.load(f)

        except FileNotFoundError as e:
            print(f'Cannot open project file: {toml_path}. Please specify the correct path using --xeda-project', e)
            exit(1)
        except IsADirectoryError as e:
            self.fatal(f'The specified design json is not a regular file.', e)
            raise e



        if parsed_args.xeda_run_dir is None:
            rundir = None
            project = xeda_project.get('project')
            if isinstance(project, list):
                project = project[0]
            if project:
                rundir = project.get('xeda_run_dir')
            if not rundir:
                rundir = os.environ.get('xeda_run_dir')
            if not rundir:
                rundir = 'xeda_run'
            parsed_args.xeda_run_dir = rundir
            
        xeda_run_dir = Path(parsed_args.xeda_run_dir).resolve()
        xeda_run_dir.mkdir(exist_ok=True, parents=True)
        print(f"xeda_run_dir={xeda_run_dir}")

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")

        fileHandler = logging.FileHandler(
            xeda_run_dir / f"xeda_{timestamp}.log")
        fileHandler.setFormatter(logFormatter)
        logger.addHandler(fileHandler)

        coloredlogs.install(
            'INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

        logger.info(f"Running using FlowRunner: {runner_cls.__name__}")
        print(f"Running using FlowRunner: {runner_cls.__name__}")

        xeda_project['xeda_version'] = __version__

        runner = runner_cls(parsed_args, xeda_project, timestamp)

        runner.launch()

    def parse_args(self, args):
        parser = self.parser
        parser.add_argument(
            '--debug',
            type=int,
            default=DebugLevel.NONE,
            help=f'Set debug level. Values of DEBUG_LEVEL correspond to: {list(DebugLevel)}'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Be verbose. Print everything to stdout.'
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            help="Be as quiet as possible. Never print out output from command executions"
        )
        parser.add_argument(
            '--force-run-dir',
            help='USE ONLY FOR DEBUG PURPOSES.',
            # default=None
        )
        parser.add_argument(
            '--xeda-run-dir',
            help='Directory where the flows are executed and intermediate and result files reside.',
            default=None
        )
        parser.add_argument(
            '--force-rerun',
            action='store_true',
            default=False,
            help='Force re-run of flow and all dependencies, even if they are already up-to-date',
            # default=None
        )
        parser.add_argument(
            '--max-cpus',
            default=max(1, multiprocessing.cpu_count() // 2), type=int,
        )
        parser.add_argument(
            '--version',  action='version', version=f'%(prog)s {__version__}', help='Print version information and exit',
        )

        registered_flows = [camelcase_to_snakecase(
            n) for n, _ in self.flow_classes]

        class CommandAction(argparse.Action):
            def __call__(self, parser, args, value, option_string=None):
                assert value, "flow should not be empty"
                # TODO FIXME should change to be class and remove load_class from runners
                splitted = value.split(':')
                flow_name = splitted[-1]
                if len(splitted) == 2:
                    flow_runner_name = splitted[0]
                    if not flow_runner_name.endswith('_runner'):
                        flow_runner_name += '_runner'
                    try:
                        # FIXME: search plugins too
                        args.flow_runner = load_class(
                            flow_runner_name, '.flow_runner')
                    except:
                        sys.exit(f'FlowRunner {flow_runner_name} not found')
                elif len(splitted) == 1:
                    args.flow_runner = DefaultRunner
                else:
                    sys.exit(f'Use [RunnerName]:flow_name')
                # if not flow_name in registered_flows:  # FIXME check when loading
                #     sys.exit(f'Flow {flow_name} not found')
                setattr(args, self.dest, flow_name)

        parser.add_argument('flow', metavar='[RUNNER_NAME:]FLOW_NAME', action=CommandAction,
                            help=f'Flow name optionally prepended by flow-runner.' +
                            'If runner is not specified the default runner is used.\n' +
                            f'Available flows are: {registered_flows}\n' +
                            f'Available runners are: {[camelcase_to_snakecase(n) for n, _ in self.runner_classes]}'
                            )
        parser.add_argument(
            '--xeda-project',
            default=None,
            help='Path to Xeda project file. By default will use xeda.toml in the current directory.'
        )
        parser.add_argument('--override-settings', nargs='+',
                            help='Override setting value. Use <hierarchy>.key=value format'
                            'example: --override-settings flows.vivado_run.stop_time=100us')

        parser.add_argument('--override-flow-settings', nargs='+',
                            help='Override setting values for the specified main flow. Use <hierarchy>.key=value format'
                            'example: xeda vivado_sim --override-settings stop_time=100us')
                            
        parser.add_argument(
            '--design',
            nargs='+',
            help='Specify design.name in case multiple designs are available in the Xeda project.'
        )

        return parser.parse_args(args)
