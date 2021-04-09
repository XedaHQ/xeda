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
import toml
import json
import shtab

logger = logging.getLogger()
logger.setLevel(logging.INFO)


try:
    __version__ = pkg_resources.get_distribution(__package__).version
except pkg_resources.DistributionNotFound:
    __version__ = '(N/A - Local package)'


class ListDesignsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            xp = load_xedaproject(namespace.xedaproject)
            print(f'Listing designs in `{namespace.xedaproject}`:')
            designs = xp.get('design')
            if designs:
                if not isinstance(designs, list):
                    designs = [designs]
                for d in designs:
                    dn = d.get('name')
                    if not dn:
                        dn = '!!!<UNKNOWN>!!!'
                    desc = d.get('description')
                    if desc:
                        desc = ": " + desc
                    else:
                        desc = ""
                    print(f"{' '*4}{dn:<10} {desc:.80}")
        finally:
            exit(0)


def get_main_argparser():

    # TODO registered plugins
    flow_classes = inspect.getmembers(sys.modules['xeda.flows'],
                                      lambda cls: inspect.isclass(cls) and issubclass(cls, Flow) and cls != Flow and cls != SimFlow and cls != SynthFlow)
    runner_classes = inspect.getmembers(sys.modules['xeda.flow_runner'],
                                        lambda cls: inspect.isclass(cls) and issubclass(cls, FlowRunner) and cls != FlowRunner)

    parser = argparse.ArgumentParser(
        prog=__package__,
        description=f'{__package__}: Cross-EDA abstraction and automation. Version: {__version__}',
        formatter_class=lambda prog: argparse.HelpFormatter(
            prog, max_help_position=35),
    )

    parser.add_argument(
        '--debug',
        type=int,
        metavar='DEBUG_LEVEL',
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
    )
    parser.add_argument(
        '--use-stale',
        action='store_true',
        default=False,
        help="Don'r run the flow if stale results already exist.",
    )
    parser.add_argument(
        '--max-cpus',
        default=max(1, multiprocessing.cpu_count()), type=int,
    )

    registered_flows = [camelcase_to_snakecase(n) for n, _ in flow_classes]

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
                        help=(f'Flow name optionally prepended by flow-runner.'
                              'If runner is not specified the default runner is used.\n'
                              f'Available flows are: {registered_flows}\n'
                              f'Available runners are: {[camelcase_to_snakecase(n) for n, _ in runner_classes]}'
                              )
                        )
    # redundant, kept for compatibility
    parser.add_argument(
        '--design',
        nargs='?',
        help='Specify design.name in case multiple designs are available in the Xeda project.'
    )
    parser.add_argument(
        'design',
        nargs='?',
        help='Specify design.name in case multiple designs are available in the Xeda project.'
    )
    parser.add_argument(
        '--list-designs',
        nargs=0,
        action=ListDesignsAction,
        help='List all designs available in the Xeda project.'
    )
    parser.add_argument(
        '--xedaproject',
        default='xedaproject.toml',
        help='Path to Xeda project file. By default will use xedaproject.toml in the current directory.'
    )
    parser.add_argument('--override-settings', nargs='+',
                        help=('Override setting value. Use <hierarchy>.key=value format'
                              'example: --override-settings flows.vivado_run.stop_time=100us'
                              )
                        )
    parser.add_argument('--override-flow-settings', nargs='+',
                        help=(
                            'Override setting values for the specified main flow. Use <hierarchy>.key=value format'
                            'example: xeda vivado_sim --override-settings stop_time=100us')
                        )
    parser.add_argument(
        '--version',  action='version', version=f'%(prog)s {__version__}', help='Print version information and exit',
    )

    shtab.add_argument_to(parser, ["--print-completion"])
    return parser


def gen_shell_completion():
    print("Installing shell completion")
    parser = get_main_argparser()
    completion = shtab.complete(parser, shell="bash")
    xdg_home = os.environ.get('XDG_DATA_HOME', os.path.join(
        os.environ.get('HOME', str(Path.home())), '.local', 'share'))
    completion_user_dir = os.environ.get(
        'BASH_COMPLETION_USER_DIR', os.path.join(xdg_home, 'bash-completion'))

    dir = Path(completion_user_dir)
    if not dir.exists():
        dir.mkdir(parents=True)
    completion_file = dir / 'xeda'
    with open(completion_file, 'w') as f:
        f.write(completion)
    eager_completion = Path.home() / '.bash_completion'
    source_line = f'. {completion_file} # added by xeda'
    if eager_completion.exists():
        with open(eager_completion, "r+") as f:
            for line in f:
                if source_line in line:
                    break
            else:  # else-for "completion clause"
                f.write(source_line + os.linesep)
    else:
        with open(eager_completion, "w") as f:
            f.write(source_line + os.linesep)


def sanitize_toml(obj):
    if isinstance(obj, (str, int, float, bool)):
        return obj
    elif isinstance(obj, list):
        return [sanitize_toml(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(sanitize_toml(list(obj)))
    elif isinstance(obj, dict):
        return {k: sanitize_toml(v) for k, v in obj.items()}
    elif hasattr(obj, '__dict__'):
        return(sanitize_toml(dict(**obj.__dict__)))
    else:
        print(
            f"ERROR in xeda_app.sanitize_toml: unhandled object of type {type(obj)}: {obj}")
        return sanitize_toml(dict(obj))

def load_xedaproject(project_file: Path):
    try:
        with open(project_file) as f:
            ext = project_file.suffix.lower()
            if ext == '.json':
                return json.load(f)
            elif ext == '.toml':
                return sanitize_toml(toml.load(f))
            else:
                exit(f"xedaproject: {project_file} has unknown extension {ext}. Currently supported formats are TOML (.toml) and JSON (.json)")
    except FileNotFoundError:
        exit(
            f'Cannot open project file: {project_file}. Please run from the project directory with xedaproject.toml or specify the correct path using the --xedaproject flag')
    except IsADirectoryError:
        exit(f'The specified xedaproject is not a regular file.')

class XedaApp:
    def main(self, args=None):
        parsed_args = get_main_argparser().parse_args(args)

        if parsed_args.debug:
            logger.setLevel(logging.DEBUG)

        runner_cls = parsed_args.flow_runner

        toml_path = Path(parsed_args.xedaproject)
        xeda_project = load_xedaproject(toml_path)

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

        logdir = xeda_run_dir / 'Logs'
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

        logger.info(f"Running using FlowRunner: {runner_cls.__name__}")

        xeda_project['xeda_version'] = __version__

        runner = runner_cls(parsed_args, xeda_project, timestamp)

        runner.launch()
