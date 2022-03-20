# © 2022 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
"""Xeda Command-line interface"""
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
import multiprocessing
import os
from pathlib import Path
import sys
import logging
import coloredlogs
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
import yaml

from ._version import get_versions
from .utils import camelcase_to_snakecase, sanitize_toml
from .debug import DebugLevel
from .flow_runner import DefaultRunner, merge_overrides, get_flow_class
from .flows.flow import Flow, FlowFatalException, registered_flows
from .tool import NonZeroExitCode
from .flows.design import Design
from .console import console

log = logging.getLogger()

__version__ = get_versions()['version']


available_flow_names = [camelcase_to_snakecase(f) for f in registered_flows.keys()]


class ConsoleLogo():
    logo: str = """
:==-+-:        :=-.-='.---==:::::::==-. .--=:::::::::=.          ,+==::::::+=-.
 .= {X}O{O} -=     .=--.==. *  ,...........:: *:  {X}........{O}  `=-      ,='            *
   :=:--=-  =-..-=.   +  .-:::::::::::' +: {X}=+++++++++-{O} `:+   ,=' .*########:  *
     -=.-:=-.-:=:     +  +:             +: {X}=+++++++++*.{O} `=- .=: :*########%:  *
       -==-.-=-       +  +: ,:::::::-,  +: {X}=++++++++++*:{O} := =: :*%########%:  *
      .==-.-==-       +  +: `:::::::='  +: {X}=++++++++++*:{O} := =: `*%#######%*'  *
     -= -:==. ==-     +  +:             +: {X}=++++++++++*:{O} .= =:                *
   := -==-  ==. ==-   *  `+..........-. +. {X}=+++++++++*:{O} .+  =: .==::::::::=.  *
 .:-.==:     .=- {X}O{O} =. :=+-............: *, {X}:++++++++'{O} .+-   =: -=          +  *
:=--==.        :=--.=: `=+:::::::::::-' `*-::::::::::+-'    `=-='          `-='
    """

    @classmethod
    def print_ansi(cls, color=True):
        logo = cls.logo
        print(logo)

    @classmethod
    def print(cls):
        color_system = console.color_system
        color = False
        if color_system == "256" or color_system == "truecolor":
            color = True
        width = console.width
        if width >= 80:
            console.print(cls.logo.format(X='[dark_orange]', O='[/]'), highlight=False, emoji=False)


def load_xeda(file: Path):
    with open(file) as f:
        ext = file.suffix.lower()
        if ext == '.json':
            return json.load(f)
        elif ext == '.toml':
            return sanitize_toml(toml.load(f))
        elif ext == '.yaml':
            return yaml.safe_load(f)
        else:
            exit(
                f"File {file} has unknown extension {ext}. Currently supported formats are TOML (.toml) and JSON (.json)")


def load_design_from_toml(design_file) -> Design:
    return Design.from_toml(design_file)


def get_available_flows():
    """alternative method using inspect"""
    mod = 'xeda.flows'
    fc = inspect.getmembers(
        sys.modules[mod],
        lambda cls: inspect.isclass(cls) and issubclass(cls, Flow) and not inspect.isabstract(cls)
    )
    return {n: (mod, cls) for n, cls in fc}


CONTEXT_SETTINGS = dict(
    auto_envvar_prefix="XEDA",
    help_option_names=["--help", "-h"],
    max_content_width=console.width,
)


class Mutex(click.Option):
    def __init__(self, *args, **kwargs):
        self.mutually_exclusive_with: list = kwargs.pop("mutually_exclusive_with", [])
        self.required_if: list = kwargs.pop("required_if", [])
        if self.mutually_exclusive_with:
            kwargs["help"] = (kwargs.get("help", "") + "Option is mutually exclusive with " + ", ".join(self.mutually_exclusive_with) + ".").strip()
        if self.required_if:
            kwargs["help"] = (kwargs.get("help", "") + "Option is required only if " + " or ".join(self.mutually_exclusive_with) + " is specified ").strip()
        super(Mutex, self).__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        if self.name in opts:
            for mutex_opt in self.mutually_exclusive_with:
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


def setup_logger(log, logdir: Path):
    coloredlogs.install(
        None, fmt='%(asctime)s %(levelname)s %(message)s', logger=log
    )
    logdir.mkdir(exist_ok=True, parents=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
    logfile = logdir / f"xeda_{timestamp}.log"
    log.info(f"Logging to {logfile}")
    logFormatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    fileHandler = logging.FileHandler(logfile)
    fileHandler.setFormatter(logFormatter)
    log.addHandler(fileHandler)


@click.group(cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@click.option('--verbose', is_flag=True, help='Enables verbose mode.')
@click.option('--quiet', is_flag=True, help="Enable quiet mode.")
@click.option(
    '--debug',
    show_default=True,
    show_envvar=True,
    metavar='DEBUG_LEVEL',
    default=DebugLevel.NONE,
    type=DebugLevel,  # click.Choice([str(l.value) for l in DebugLevel]),
    help=f"""
            Set debug level. DEBUG_LEVEL values corresponds to:
            {', '.join([f"{l.value}: {l.name}" for l in DebugLevel])}
        """
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help="Run the flow identified by FLOW_NAME. A snake_case styled FLOW_NAME (e.g. ghdl_sim) is converted to a CamelCase class name (e.g. GhdlSim).",
    no_args_is_help=False
)
@click.argument(
    "flow",
    metavar='FLOW_NAME',
    type=click.Choice(available_flow_names)
)
@ click.option(
    "--xeda-run-dir",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, readable=True,
                    resolve_path=True, allow_dash=True, path_type=Path
                    ),
    envvar="XEDA_RUN_DIR",
    help="Parent folder for execution of xeda commands.",
    default="xeda_run",
    show_default=True,
    show_envvar=True,
)
@ click.option(
    '--force-run',
    is_flag=True,
    help='Force re-run of flow and all dependencies, even if they are already up-to-date',
)
@ click.option(
    '--xedaproject',
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, writable=False, readable=True,
        resolve_path=True, allow_dash=False, path_type=Path
    ),
    cls=Mutex, mutually_exclusive_with=["design_file"],
    help='Path to Xeda project file.'
)
@ click.option(
    '--design-name',
    cls=Mutex,
    mutually_exclusive_with=["design_file"],
    help='Specify design.name in case multiple designs are available in a xedaproject.'
)
@ click.option(
    "--design-file", "--design",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, writable=False, readable=True,
        resolve_path=True, allow_dash=False, path_type=Path
    ),
    cls=Mutex,
    mutually_exclusive_with=["xedaproject"],
    help="Path to Xeda design file containing the description of a single design."
)
@ click.option(
    '--flow-settings', '--settings',
    # action="extend",
    metavar="KEY=VALUE...",
    type=str,
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE overrides with commas. KEY can be a hierarchical name using dot notation.
    Example: clock_period=2.345,impl.strategy=Debug
    """
    #  examples: # FIXME move to docs
    # - xeda vivado_sim --flow-settings stop_time=100us
    # - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock_period=2.345
)
@ click.pass_context
def run(ctx, force_run, flow, flow_settings, xeda_run_dir, design_file=None, xedaproject=None, design_name=None):
    args: XedaOptions = ctx.obj
    # Always run setup_logger with INFO log level
    log.setLevel(logging.INFO)
    setup_logger(log, xeda_run_dir / 'Logs')
    # then switch to requested level
    log.setLevel(logging.WARNING if args.quiet else logging.DEBUG if args.debug else logging.INFO)

    # FIXME
    design = {}
    flows = {}
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
                log.critical(
                    f'Design "{design_name}" not found in the current project.')
                exit(1)
        design = Design(design_root=toml_path.parent, **design_dict)
    else:
        sys.exit("No design or project specified!")
    if force_run:
        log.info(f"Forced re-run of {flow}")
    flow_overrides = merge_overrides(flow_settings, flows.get(flow, {}))
    flow_class = get_flow_class(flow, "xeda.flows", __package__)
    runner = DefaultRunner(xeda_run_dir)
    try:
        runner.run_flow(flow_class, design, flow_overrides)
    except FlowFatalException as e:
        log.critical(f"Flow {flow} failed!")
        exit(1)
    except NonZeroExitCode as e:
        log.critical(f"Flow {flow} failed!")
        exit(1)
    except ValidationError as e:
        errors = e.errors()
        log.critical(f"{len(errors)} error(s) validating flow settings for {flow}:\n\n{display_errors(errors)}")
        exit(1)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="List available flows."
)
def list_flows():
    table = Table(
        title="Available flows",
        show_header=True, header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD, show_lines=True
    )
    table.add_column("Flow", header_style="bold green", style="bold")
    table.add_column("Description")
    table.add_column("Class", style="dim")
    super_flow_doc = inspect.getdoc(Flow)
    for cls_name, (mod, cls) in registered_flows.items():
        doc = inspect.getdoc(cls)
        if doc == super_flow_doc:
            doc = "<no description>"
        table.add_row(
            camelcase_to_snakecase(cls_name), doc, str(cls.__module__) + "." + cls.__name__
        )
    console.print(table)


def print_flow_settings(flow, options: XedaOptions):
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
                log.critical(
                    "type definition for %s changed!\nPrevious def:\n %s new def:\n %s",
                    typ, type_defs[typ], typ_def
                )
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
        typ = get_type(field)
        default = field.get("default")
        req_or_def = "[red]<required>[/red]" if required else str(default)
        table.add_row(name, typ, req_or_def, desc)
    console.print(table)

    for typ_name, typ_def in list(type_defs.items()):
        for property, property_def in typ_def.get("properties").items():
            get_type(property_def)
    for typ_name, typ_def in list(type_defs.items()):
        if typ_name in ["DockerToolSettings", "RemoteToolSettings", "NativeToolSettings", "ToolSettings"]:
            continue
        c = "blue"
        table = Table(
            title=f"Type [{c}]{typ_name}[/{c}]", show_header=True, header_style="bold green",
            title_style=Style(frame=True, bold=True),
            box=box.SQUARE_DOUBLE_HEAD, show_lines=True
        )
        if options.debug:
            console.print(f"Type: {typ_name}")
            console.print_json(data=typ_def)
        table.add_column("property")
        table.add_column("type")
        table.add_column("description")
        for property, property_def in typ_def.get("properties").items():
            if property_def.get("hidden_from_schema"):
                continue
            if typ_name.endswith("__Settings") and property in Flow.Settings.__fields__.keys():
                continue
            desc = property_def.get("description", property_def.get("title"))
            desc = re.sub(r'\s*\.*\s*$', '', desc)
            table.add_row(property, get_type(property_def), desc)
        console.print(table)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="List flow settings information"
)
@click.argument(
    "flow",
    metavar='FLOW_NAME',
    required=True,
)
@click.pass_context
def list_settings(ctx: click.Context, flow):
    print_flow_settings(flow, options=ctx.obj)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Design-space exploration: run several instances of a flow to find optimal parameters and results"
)
@click.argument(
    "flow"
)
@click.option(
    '--max-cpus',
    default=max(1, multiprocessing.cpu_count()),
    type=int,
    help="Maximum total number of logical CPU cores to use.",
    show_default=True,
    show_envvar=True,
)
@click.pass_context
def dse(ctx, flow, max_cpus):
    """Design-space exploration (e.g. fmax)"""
    ...


SHELLS = {
    'bash': {
        'eval_file': '~/.bashrc',
        'eval': 'eval "$(_XEDA_COMPLETE=bash_source xeda)"',
        'completion_file': None,
    },
    'zsh': {
        'eval_file': '~/.zshrc',
        'eval': 'eval "$(_XEDA_COMPLETE=zsh_source xeda)"',
        'completion_file': None,
    },
    'fish': {
        'eval_file': '~/.config/fish/completions/xeda.fish',
        'eval': 'eval (env _XEDA_COMPLETE=fish_source xeda)',
        'completion_file': None,
    }
}


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Shell completion",
    help=f"Name of the shell. Supported shells are: {', '.join(SHELLS.keys())}",
)
@click.argument(
    "shell",
    required=False,
    type=click.Choice(list(SHELLS.keys()), case_sensitive=False),
)
@click.option(
    '--stdout',
    is_flag=True,
    cls=Mutex,
    help="""Produce the shell completion script and output it to the standard output.\n
    Example usage:\n
         $ xeda completion zsh --stdout  > $HOME/.zsh/completion/_xeda\n
         $ xeda completion bash --stdout > $HOME/.local/share/bash-completion/xeda
    """
)
@click.pass_context
def completion(ctx: click.Context, stdout, shell=None):
    """Xeda shell auto-completion"""
    # xdg_home = os.environ.get('XDG_DATA_HOME', os.path.join(
    #     os.environ.get('HOME', str(Path.home())), '.local', 'share'))
    # completion_user_dir = os.environ.get(
    #     'BASH_COMPLETION_USER_DIR', os.path.join(xdg_home, 'bash-completion'))
    # dir = Path(completion_user_dir)
    # if not dir.exists():
    #     dir.mkdir(parents=True)
    # completion_file = dir / 'xeda'
    # with open(completion_file, 'w') as f:
    #     f.write(completion)
    # eager_completion = Path.home() / '.bash_completion'
    # source_line = f'. {completion_file} # added by xeda'
    # if eager_completion.exists():
    #     with open(eager_completion, "r+") as f:
    #         for line in f:
    #             if source_line in line:
    #                 break
    #         else:  # else-for "completion clause"
    #             f.write(source_line + os.linesep)
    # else:
    #     with open(eager_completion, "w") as f:
    #         f.write(source_line + os.linesep)
    os_default_shell_name = None
    os_default_shell = os.environ.get("SHELL")
    if os_default_shell:
        os_default_shell_name = os_default_shell.split(os.sep)[-1]
    if os_default_shell_name:
        if not shell:
            shell = os_default_shell_name
        elif os_default_shell_name != shell and not stdout:
            console.print(f"[yellow]WARNING:[/] Current default shell ([bold]{os_default_shell}[/]) is different from the specified shell [bold]{shell}[/b]")
    if stdout:
        completion_class = get_completion_class(shell)
        if completion_class:
            complete = completion_class(cli=cli, ctx_args={}, prog_name=__package__, complete_var="source_xeda")
            print(complete.source())
    else:
        console.print(f"""
    Make sure 'xeda' executable is properly installed and is accessible through the shell's PATH.
        [yellow]$ xeda --version[/]
    Add the following line to [magenta underline]{SHELLS[shell]['eval_file']}[/]:
        {SHELLS[shell]['eval']}
            """, highlight=False)


@cli.command()
@click.argument(
    'subcommand',
    required=False
)
@click.pass_context
def help(ctx: click.Context, subcommand=None):
    if subcommand:
        subcommand_obj = cli.get_command(ctx, subcommand)
        if subcommand_obj is None:
            click.echo("I don't know that command.")
        else:
            click.echo(subcommand_obj.get_help(ctx))
    else:
        click.echo(cli.get_usage(ctx))
