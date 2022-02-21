# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
"""
Xeda application
"""

import inspect
import multiprocessing
import os
from pathlib import Path
import sys
import argparse
import logging
from typing import Sequence
import toml
import json
import shtab
from argparse_formatter import FlexiFormatter
from pydantic.error_wrappers import ValidationError, display_errors

from .flows.flow import Flow, SimFlow, SynthFlow
from .utils import camelcase_to_snakecase, load_class
from .debug import DebugLevel
from .flow_runner import FlowRunner, DefaultRunner, merge_overrides, get_flow_class, get_settings_schema
from .flows.design import Design, DesignError
from .flows.flow import Flow, FlowSettingsError

from pprint import pprint

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions


class FlowSettingsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            print(f"flow={namespace.flow}")
            schema = get_settings_schema(namespace.flow, "xeda.flows")
            self.print_flow_settings(schema)
        finally:
            exit(0)

    @staticmethod
    def print_flow_settings(schema):
        def get_type(field):
            typ = field.get("type")
            type_def = None
            ref = field.get("$ref")
            if typ is None and ref:
                typ = ref.split("/")[-1]
                type_def = schema.get('definitions', {}).get(typ)
            additional = field.get("additionalProperties")
            if typ == 'object' and additional:
                typ = f"Dict[string, {additional.get('type')}]"
            allof = field.get("allOf")
            if typ is None and allof:
                typs = [get_type(t) for t in allof]
                typ = '+'.join([t[0] for t in typs])
                type_def = typs[0][1]
            return typ, type_def

        for name, field in schema.get('properties', {}).items():
            required = name in schema.get('required', [])
            desc = field.get("description", "")
            typ, type_def = get_type(field)
            default = field.get("default")
            req_or_def = f"default: {default}" if default is not None else "required" if required else ""
            print(f"{name} [{typ}] ({req_or_def}): {desc}")
            if type_def:
                td_desc = type_def.get('description')
                td_props = type_def.get('properties')
                if td_desc:
                    print(td_desc)
                if td_props:
                    pprint(td_props)


class ListDesignsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            toml_path = Path(namespace.xedaproject)
            xp = load_xeda(toml_path)
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
        except Exception as e:
            raise e from None
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
        formatter_class=FlexiFormatter,
    )

    parser.add_argument(
        '--debug',
        type=int,
        metavar='DEBUG_LEVEL',
        default=DebugLevel.NONE,
        help=f"""
            Set debug level. DEBUG_LEVEL corresponds to: 
            {', '.join([str(l) for l in DebugLevel])}
        """
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
    # parser.add_argument(
    #     '--force-run-dir',
    #     help='USE ONLY FOR DEBUG PURPOSES.',
    #     # default=None
    # )
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
        '--max-cpus',
        default=max(1, multiprocessing.cpu_count()),
        type=int,
        help="""
        Maximum number of threads or CPU cores to use.
        """
    )
    parser.add_argument(
        '--help-settings',
        nargs=0,
        action=FlowSettingsAction,
        help="""
        List flow settings information
        """
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

    parser.add_argument(
        'flow',
        metavar='[RUNNER_NAME:]FLOW_NAME',
        action=CommandAction,
        help=f"""Flow name optionally prepended by flow-runner.

If runner is not specified 'default' runner is used.

Available flows:
    {', '.join(registered_flows)}
Available runners:
    {', '.join([camelcase_to_snakecase(n) for n, _ in runner_classes])}
"""
    )
    parser.add_argument(
        '--design',
        nargs='?',
        help='Specify design.name in case multiple designs are available in the Xeda project.'
    )
    # parser.add_argument(
    #     'design',
    #     nargs='?',
    #     help='Specify design.name in case multiple designs are available in the Xeda project.'
    # )
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
    parser.add_argument(
        '--design-file',
        type=argparse.FileType('r'),
        help='Path to Xeda design file, containing description of a single design.'
    )
    parser.add_argument(
        '--flow-settings',
        action="extend",
        nargs="+",
        type=str,
        help="""
            Override setting values for the main flow. Use <key hierarchy>=<value> format.
             examples: 
                - xeda vivado_sim --flow-settings stop_time=100us
                - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock_period=2.345
        """
    )
    parser.add_argument(
        '--version',  action='version', version=f'%(prog)s {__version__}', help='Print version information and exit',
    )

    shtab.add_argument_to(
        parser,
        option_string="--completion",
        help="""
        Print zsh/bash shell completion to stdout
        example usage:
            - xeda --completion zsh > ~/.oh-my-zsh/functions/_xeda
            - xeda --completion bash > ~/.local/share/bash-completion/xeda
        """
    )
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


def load_xeda(file: Path):
    with open(file) as f:
        ext = file.suffix.lower()
        if ext == '.json':
            return json.load(f)
        elif ext == '.toml':
            return sanitize_toml(toml.load(f))
        else:
            exit(
                f"File {file} has unknown extension {ext}. Currently supported formats are TOML (.toml) and JSON (.json)")


def validate_design(design_dict: dict) -> Design:
    try:
        return Design(**design_dict)
    except ValidationError as e:
        errors = e.errors()
        raise DesignError(
            f"{len(errors)} error(s) validating design settings:\n\n{display_errors(errors)}\n"
        ) from None


def load_design_from_toml(design_file) -> Design:
    design_dict = sanitize_toml(toml.load(design_file))
    return validate_design(design_dict)


def run(args=None):
    parsed_args = get_main_argparser().parse_args(args)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if parsed_args.debug:
        logger.setLevel(logging.DEBUG)

    runner_cls = parsed_args.flow_runner

    design = {}
    flows = {}
    rundir = None
    if parsed_args.design_file:
        design = load_design_from_toml(parsed_args.design_file)
    else:
        toml_path = Path(parsed_args.xedaproject)
        print(f"toml_path={toml_path}")
        try:
            xeda_project = load_xeda(toml_path)
        except FileNotFoundError as e:
            try:
                xeda_project = load_xeda(
                    toml_path.parent / (toml_path.stem + ".json"))
            except:
                exit(
                    f'Cannot open project file: {toml_path}. Please run from the project directory with xedaproject.toml or specify the correct path using the --xedaproject flag')

        flows = xeda_project.get('flows', {})
        designs = xeda_project['design']
        rundir = xeda_project.get('xeda_run_dir')
        if not isinstance(designs, Sequence):
            designs = [designs]
        design_name = parsed_args.design
        if len(designs) == 1:
            design = validate_design(designs[0])
        elif design_name:
            for x in designs:
                print(x['name'])
                if x['name'] == design_name:
                    design = validate_design(x)
            if not design:
                logger.critical(
                    f'Design "{design_name}" not found in the current project.')
                exit(1)
    xeda_run_dir = parsed_args.xeda_run_dir
    if xeda_run_dir is None:
        if not rundir:
            rundir = os.environ.get('XEDA_RUN_DIR', 'xeda_run')
        xeda_run_dir = rundir

    flow_name = parsed_args.flow
    force_run = parsed_args.force_rerun
    if force_run:
        logger.info(f"Forced re-run of {flow_name}")

    flow_overrides = merge_overrides(
        parsed_args.flow_settings, flows.get(flow_name, {}))
    if parsed_args.flow_settings:
        assert len(flow_overrides) >= 1

    runner: FlowRunner = runner_cls(xeda_run_dir)

    flow_class = get_flow_class(flow_name, "xeda.flows", __package__)

    try:
        runner.run_flow(flow_class, design, flow_overrides)
    except ValidationError as e:
        errors = e.errors()
        raise FlowSettingsError(
            f"{len(errors)} error(s) validating flow settings for {flow_name}:\n\n{display_errors(errors)}\n"
        ) from None
