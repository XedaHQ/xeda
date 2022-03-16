# © 2022 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
"""Xeda Command-line interface"""
from dataclasses import dataclass
from functools import reduce
import multiprocessing
import os
from pathlib import Path
import sys
import logging
from typing import Sequence
import toml
import json
import re
from pydantic.error_wrappers import ValidationError, display_errors
import click
import inspect
from rich.table import Table
from rich.style import Style
from rich.text import Text
from rich import box
from click.shell_completion import get_completion_class
from click_help_colors import HelpColorsGroup

from .flows.flow import Tool, Flow, FlowSettingsError, SimFlow, SynthFlow
from .utils import camelcase_to_snakecase, load_class, sanitize_toml
from .debug import DebugLevel
from .flow_runner import FlowRunner, DefaultRunner, merge_overrides, get_flow_class
from .flows.design import Design, DesignError
from .console import console


from pprint import pprint

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions


class ConsoleLogo():
    ansi_logo: str = """
:==-+-:        :=-.-='.---==:::::::==-. .--=:::::::::=.          ,+==::::::+=-.
 .= \033[38;2;255;109;0mO\033[39;49m -=     .=--.==. *  ,...........:: *:  \033[38;2;255;109;0m........\033[39;49m  `=-      ,='            *
   :=:--=-  =-..-=.   +  .-:::::::::::' +: \033[38;2;255;109;0m=+++++++++-\033[39;49m `:+   ,=' .*########:  *
     -=.-:=-.-:=:     +  +:             +: \033[38;2;255;109;0m=+++++++++*.\033[39;49m `=- .=: :*########%:  *
       -==-.-=-       +  +: ,:::::::-,  +: \033[38;2;255;109;0m=++++++++++*:\033[39;49m := =: :*%########%:  *
      .==-.-==-       +  +: `:::::::='  +: \033[38;2;255;109;0m=++++++++++*:\033[39;49m := =: `*%#######%*'  *
     -= -:==. ==-     +  +:             +: \033[38;2;255;109;0m=++++++++++*:\033[39;49m .= =:                *
   := -==-  ==. ==-   *  `+..........-. +. \033[38;2;255;109;0m=+++++++++*:\033[39;49m .+  =: .==::::::::=.  *
 .:-.==:     .=- \033[38;2;255;109;0mO\033[39;49m =. :=+-............: *, \033[38;2;255;109;0m:++++++++'\033[39;49m .+-   =: -=          +  *
:=--==.        :=--.=: `=+:::::::::::-' `*-::::::::::+-'    `=-='          `-='
    """

    @classmethod
    def print_ansi(cls, color=True):
        logo = cls.ansi_logo
        if not color:
            logo = re.sub(r"\033\[(\d+;)+\d+m", "", logo)
        print(logo)

    @classmethod
    def print(cls):
        color_system = console.color_system
        color = False
        if color_system == "256" or color_system == "truecolor":
            color = True
        width = console.width
        if width >= 80:
            cls.print_ansi(color)


# class ListDesignsAction(argparse.Action):
#     def __call__(self, parser, namespace, values, option_string=None):
#         try:
#             toml_path = Path(namespace.xedaproject)
#             xp = load_xeda(toml_path)
#             print(f'Listing designs in `{namespace.xedaproject}`:')
#             designs = xp.get('design')
#             if designs:
#                 if not isinstance(designs, list):
#                     designs = [designs]
#                 for d in designs:
#                     dn = d.get('name')
#                     if not dn:
#                         dn = '!!!<UNKNOWN>!!!'
#                     desc = d.get('description')
#                     if desc:
#                         desc = ": " + desc
#                     else:
#                         desc = ""
#                     print(f"{' '*4}{dn:<10} {desc:.80}")
#         except Exception as e:
#             raise e from None
#         finally:
#             exit(0)


#     shtab.add_argument_to(
#         parser,
#         option_string="--completion",
#         help="""
#         Print zsh/bash shell completion to stdout
#         example usage:
#             - xeda --completion zsh > ~/.oh-my-zsh/functions/_xeda
#             - xeda --completion bash > ~/.local/share/bash-completion/xeda
#         """
#     )
#     return parser


# def gen_shell_completion():
#     print("Installing shell completion")
#     parser = get_main_argparser()
#     completion = shtab.complete(parser, shell="bash")
#     xdg_home = os.environ.get('XDG_DATA_HOME', os.path.join(
#         os.environ.get('HOME', str(Path.home())), '.local', 'share'))
#     completion_user_dir = os.environ.get(
#         'BASH_COMPLETION_USER_DIR', os.path.join(xdg_home, 'bash-completion'))

#     dir = Path(completion_user_dir)
#     if not dir.exists():
#         dir.mkdir(parents=True)
#     completion_file = dir / 'xeda'
#     with open(completion_file, 'w') as f:
#         f.write(completion)
#     eager_completion = Path.home() / '.bash_completion'
#     source_line = f'. {completion_file} # added by xeda'
#     if eager_completion.exists():
#         with open(eager_completion, "r+") as f:
#             for line in f:
#                 if source_line in line:
#                     break
#             else:  # else-for "completion clause"
#                 f.write(source_line + os.linesep)
#     else:
#         with open(eager_completion, "w") as f:
#             f.write(source_line + os.linesep)


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


def load_design_from_toml(design_file) -> Design:
    return Design.from_toml(design_file)


CONTEXT_SETTINGS = dict(auto_envvar_prefix="XEDA", show_default=True, show_envvar=True)


class Mutex(click.Option):
    def __init__(self, *args, **kwargs):
        self.not_required_if: list = kwargs.pop("not_required_if", [])
        self.required_if: list = kwargs.pop("required_if", [])
        if self.not_required_if:
            kwargs["help"] = (kwargs.get("help", "") + "Option is mutually exclusive with " + ", ".join(self.not_required_if) + ".").strip()
        if self.required_if:
            kwargs["help"] = (kwargs.get("help", "") + "Option is required only if " + " or ".join(self.not_required_if) + " is specified ").strip()
        super(Mutex, self).__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        if self.name in opts:
            for mutex_opt in self.not_required_if:
                if mutex_opt in opts:
                    raise click.UsageError(f"Option {self.name} is mutually exclusive with {mutex_opt}.")
        else:
            for dependent_opt in self.required_if:
                if dependent_opt in opts:
                    raise click.UsageError(f"Option {self.name} is required when {dependent_opt} is specified.")

        return super(Mutex, self).handle_parse_result(ctx, opts, args)


@dataclass
class XedaOptions:
    verbose: bool = False
    quiet: bool = False
    debug: bool = False


class XedaHelpGroup(HelpColorsGroup):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.help_headers_color = 'yellow'
        self.help_options_color = 'green'

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter):
        ConsoleLogo.print()
        super().format_usage(ctx, formatter)


@click.group(cls=XedaHelpGroup, no_args_is_help=True)
@click.option('--verbose', is_flag=True, help='Enables verbose mode.')
@click.option('--quiet', is_flag=True, help="Enable quiet mode.")
@click.option(
    '--debug', type=int, show_default=True,
    metavar='DEBUG_LEVEL',
    default=DebugLevel.NONE,
    help=f"""
            Set debug level. DEBUG_LEVEL corresponds to:
            {', '.join([str(l) for l in DebugLevel])}
        """
)
@click.version_option(__version__)
@click.help_option("--help", "-h", help="Print xeda help and exit.")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)


@cli.command(help="Run a flow. A snake_case styled FLOW_NAME (e.g. ghdl_sim) is converted to a CamelCase class name (e.g. GhdlSim).", no_args_is_help=True)
@click.argument(
    "flow",
    metavar='FLOW_NAME',
    # action=CommandAction,
    # help=
)
@click.option(
    '--xeda-run-dir',
    type=click.Path(file_okay=False, dir_okay=True, writable=True, readable=True,
                    resolve_path=True, allow_dash=True, path_type=Path
                    ),
    help='Parent folder for execution of xeda commands.',
    show_default=True,
    show_envvar=True,
    default=None
)
@click.option(
    '--force-run',
    is_flag=True,
    help='Force re-run of flow and all dependencies, even if they are already up-to-date',
)
@click.option(
    '--xedaproject',
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, writable=False, readable=True,
        resolve_path=True, allow_dash=False, path_type=Path
    ),
    cls=Mutex, not_required_if=["design_file"],
    help='Path to Xeda project file.'
)
@click.option(
    '--design',
    nargs='?',
    cls=Mutex,
    not_required_if=["design_file"],
    help='Specify design.name in case multiple designs are available in a xedaproject.'
)
@click.option(
    "--design-file",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, writable=False, readable=True,
        resolve_path=True, allow_dash=False, path_type=Path
    ),
    cls=Mutex,
    not_required_if=["xedaproject"],
    help="Path to Xeda design file containing the description of a single design."
)
@click.option(
    '--flow-settings',
    # action="extend",
    metavar="KEY=VALUE...",
    nargs="+",
    type=str,
    help="""Override setting values for the main flow. 
                Use <key hierarchy>=<value> format."""
    #  examples: # FIXME move to docs
    # - xeda vivado_sim --flow-settings stop_time=100us
    # - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock_period=2.345
)
@click.option(
    '--max-cpus',
    default=max(1, multiprocessing.cpu_count()),
    type=int,
    help="Maximum number of CPU cores to use."
)
# @click.option(
# "--list-flows",
# Available flows:
#     {', '.join(registered_flows)}
# Available runners:
#     {', '.join([camelcase_to_snakecase(n) for n, _ in runner_classes])}
# """
# @click.option(
#     '--list-designs',
#     nargs=0,
#     action=ListDesignsAction,
#     help='List all designs available in the Xeda project.'
# )
@click.pass_context
def run(ctx, force_run, max_cpus, flow, flow_settings, xeda_run_dir, design_file, xedaproject):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    args: XedaOptions = ctx.obj
    if args.debug:
        logger.setLevel(logging.DEBUG)

    # FIXME
    design_name = None
    runner_cls = DefaultRunner  # flow_runner #FIXME

    design = {}
    flows = {}
    rundir = None
    if design_file:
        design = Design.from_toml(design_file)
    elif xedaproject:
        toml_path = Path(xedaproject)
        try:
            xeda_project = load_xeda(toml_path)
        except FileNotFoundError as e:
            try:
                xeda_project = load_xeda(toml_path.parent / (toml_path.stem + ".json"))
            except:
                sys.exit(
                    f'Cannot open project file: {toml_path}. Please run from the project directory with xedaproject.toml or specify the correct path using the --xedaproject flag'
                )

        flows = xeda_project.get('flows', {})
        designs = xeda_project['design']
        rundir = xeda_project.get('xeda_run_dir')
        if not isinstance(designs, Sequence):
            designs = [designs]
        if len(designs) == 1:
            design_dict = designs[0]
        elif design_name:
            for x in designs:
                print(x['name'])
                if x['name'] == design_name:
                    design_dict = x
            if not design:
                logger.critical(
                    f'Design "{design_name}" not found in the current project.')
                exit(1)
        design = Design(design_root=toml_path.parent, **design_dict)
    else:
        sys.exit("No design or project specified!")

    if xeda_run_dir is None:
        if not rundir:
            rundir = os.environ.get('XEDA_RUN_DIR', 'xeda_run')
        xeda_run_dir = rundir

    flow_name = flow
    if force_run:
        logger.info(f"Forced re-run of {flow_name}")

    flow_overrides = merge_overrides(flow_settings, flows.get(flow_name, {}))

    runner: FlowRunner = runner_cls(xeda_run_dir)

    flow_class = get_flow_class(flow_name, "xeda.flows", __package__)

    try:
        runner.run_flow(flow_class, design, flow_overrides)
    except ValidationError as e:
        errors = e.errors()
        raise FlowSettingsError(
            f"{len(errors)} error(s) validating flow settings for {flow_name}:\n\n{display_errors(errors)}\n"
        ) from None


@cli.command(short_help="List available flows.")
def list_flows():
    flow_classes = inspect.getmembers(sys.modules['xeda.flows'],
                                      lambda cls: inspect.isclass(cls) and issubclass(cls, Flow) and cls != Flow and cls != SimFlow and cls != SynthFlow)
    runner_classes = inspect.getmembers(sys.modules['xeda.flow_runner'],
                                        lambda cls: inspect.isclass(cls) and issubclass(cls, FlowRunner) and cls != FlowRunner)
    table = Table(
        title="Available flows",
        show_header=True, header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD, show_lines=True
    )
    # table.add_column("Date", style="dim", width=12)
    table.add_column("Flow", header_style="bold green", style="bold")
    table.add_column("Description")
    table.add_column("Class", style="dim")
    super_flow_doc = inspect.getdoc(Flow)
    for cls_name, cls in flow_classes:
        doc = inspect.getdoc(cls)
        if doc == super_flow_doc:
            doc = "<no description>"
        table.add_row(
            camelcase_to_snakecase(cls_name), doc, str(cls.__module__) + "." + cls.__name__
        )
    console.print(table)

    console.print()
    table = Table(
        title="Available runners",
        show_header=True, header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD, show_lines=True
    )
    # table.add_column("Date", style="dim", width=12)
    table.add_column("Flow", header_style="bold green", style="bold")
    table.add_column("Description")
    table.add_column("Class", style="dim")
    for cls_name, cls in runner_classes:
        doc = inspect.getdoc(cls)
        if doc == super_flow_doc:
            doc = "<no description>"
        table.add_row(
            camelcase_to_snakecase(cls_name), doc, str(cls.__module__) + "." + cls.__name__
        )
    console.print(table)


def print_flow_settings(flow):
    flow_class = get_flow_class(flow, "xeda.flows", __package__)
    schema = flow_class.Settings.schema(by_alias=True)
    type_defs = {}

    def get_type(field):
        typ = field.get("type")
        ref = field.get("$ref")
        if typ is None and ref:
            typ = ref.split("/")[-1]
            typ_def = schema.get('definitions', {}).get(typ)
            if typ not in type_defs:
                type_defs[typ] = typ_def
            elif type_defs[typ] != typ_def:
                print(f"type definition for {typ} changed!\nPrevious def:\n {type_defs[typ]} new def:\n {typ_def}")
            return Text(typ, style="blue")
        additional = field.get("additionalProperties")
        if typ == 'object' and additional:
            return Text(f"Dict[string -> {additional.get('type')}]")
        def join_types(lst, joiner, style="red"):
            return reduce(lambda x, y: x + Text(joiner, style) + y, (get_type(t) for t in lst))
        allof = field.get("allOf")
        if allof:
            return join_types(allof, "+")
        anyOf = field.get("anyOf")
        if anyOf:
            return join_types(anyOf, " or ")
        return Text(typ)
    table = Table(
        title=f"{flow} settings", show_header=True, header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD, show_lines=True
    )
    table.add_column("Property", header_style="bold green", style="bold")
    table.add_column("Type", max_width=32)
    table.add_column("Default", max_width=42)
    table.add_column("Description")

    for name, field in schema.get('properties', {}).items():
        if name in ["results"] or name in Flow.Settings.__fields__.keys():
            continue
        required = name in schema.get('required', [])
        desc: str = field.get("description", "")
        # desc = re.sub(r'\s*\.*\s*$', '', desc)
        typ = get_type(field)
        default = field.get("default")
        req_or_def = Text("<required>", style="red") if required else Text(str(default))  # Text("default", style="red")

        # td_desc = type_def.get('description')
        # td_props = type_def.get('properties')
        # if td_desc:
        #     print("description:", td_desc)
        # if td_props:
        #     pprint(td_props)
        table.add_row(name, typ, req_or_def, desc)
    console.print(table)

    for typ_name, typ_def in list(type_defs.items()):
        for property, property_def in typ_def.get("properties").items():
            get_type(property_def)
    for typ_name, typ_def in list(type_defs.items()):
        if typ_name in ["DockerToolSettings", "RemoteToolSettings", "NativeToolSettings", "ToolSettings"]:
            continue
        table = Table(
            title=Text("Type ") + Text(typ_name, style="blue"), show_header=True, header_style="bold green",
            title_style=Style(frame=True, bold=True),
            box=box.SQUARE_DOUBLE_HEAD, show_lines=True
        )
        # console.print_json(data=typ_def)
        table.add_column("property")
        table.add_column("type")
        table.add_column("description")
        for property, property_def in typ_def.get("properties").items():
            if property_def.get("hidden_from_schema"):
                continue
            if typ_name.endswith("__Settings") and property in Flow.Settings.__fields__.keys():
                continue
            # print(property_def)
            desc = property_def.get("description", property_def.get("title"))
            desc = re.sub(r'\s*\.*\s*$', '', desc)
            table.add_row(property, get_type(property_def), desc)
        console.print(table)


@cli.command(short_help="List flow settings information")
@click.argument(
    "flow",
    metavar='FLOW_NAME',
    required=False,
)
def list_settings(flow):
    if flow:
        print_flow_settings(flow)
