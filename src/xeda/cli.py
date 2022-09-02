# © 2022 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
"""Xeda Command-line interface"""
from __future__ import annotations

import inspect
import logging
import multiprocessing
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

import click
import coloredlogs
from click.shell_completion import get_completion_class
from rich import box
from rich.style import Style
from rich.table import Table
from simple_term_menu import TerminalMenu
from typeguard.importhook import install_import_hook

from .cli_utils import (
    ClickMutex,
    OptionEatAll,
    XedaHelpGroup,
    XedaOptions,
    discover_flow_class,
    print_flow_settings,
    settings_to_dict,
)
from .console import console
from .design import Design, DesignValidationError
from .flow_runner import DefaultRunner
from .flows.flow import (
    Flow,
    FlowException,
    FlowFatalError,
    FlowSettingsError,
    registered_flows,
)
from .tool import ExecutableNotFound, NonZeroExitCode
from .xedaproject import XedaProject

install_import_hook("xeda")

log = logging.getLogger(__name__)


def load_design_from_toml(design_file) -> Design:
    return Design.from_toml(design_file)


def get_available_flows():
    """alternative method using inspect"""
    mod = "xeda.flows"
    fc = inspect.getmembers(
        sys.modules[mod],
        lambda cls: inspect.isclass(cls)
        and issubclass(cls, Flow)
        and not inspect.isabstract(cls),
    )
    return {n: (mod, cls) for n, cls in fc}


CONTEXT_SETTINGS = dict(
    auto_envvar_prefix="XEDA",
    help_option_names=["--help", "-h"],
    max_content_width=console.width,
)


def log_to_file(logdir: Path):
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
    logdir.mkdir(exist_ok=True, parents=True)
    logfile = logdir / f"xeda_{timestamp}.log"
    log.info("Logging to %s", logfile)
    fileHandler = logging.FileHandler(logfile)
    logFormatter = logging.Formatter(
        "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s"
    )
    fileHandler.setFormatter(logFormatter)
    log.root.addHandler(fileHandler)


@click.group(cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@click.option("--verbose", is_flag=True, help="Enables verbose mode.")
@click.option("--quiet", is_flag=True, help="Enable quiet mode.")
@click.option("--debug", show_envvar=True, is_flag=True)
@click.version_option(message="Xeda v%(version)s")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)
    log_level = (
        logging.WARNING
        if ctx.obj.quiet
        else logging.DEBUG
        if ctx.obj.debug
        else logging.INFO
    )
    print("log_level:", logging.getLevelName(log_level))
    log.root.setLevel(log_level)

    coloredlogs.install(
        None, fmt="%(asctime)s %(levelname)s %(message)s", logger=log.root
    )


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help="Run the flow identified by FLOW_NAME. A snake_case styled FLOW_NAME (e.g. ghdl_sim) is converted to a CamelCase class name (e.g. GhdlSim).",
    no_args_is_help=False,
)
@click.argument(
    "flow", metavar="FLOW_NAME", type=click.Choice(sorted(list(registered_flows)))
)
@click.option(
    "--xeda-run-dir",
    type=click.Path(
        file_okay=False,
        dir_okay=True,
        writable=True,
        readable=True,
        resolve_path=True,
        allow_dash=True,
        path_type=Path,
    ),
    envvar="XEDA_RUN_DIR",
    help="Parent folder for execution of xeda commands.",
    default="xeda_run",
    show_default=True,
    show_envvar=True,
)
@click.option(
    "--cached-dependencies",
    default=True,
    help="Don't run dependency flows if a previous successfull run on the same design and flow settings exists. Generated directory names will contain a hash of design and/or flow settings.",
)
@click.option(
    "--run_in_existing_dir",
    is_flag=True,
    help="DO NOT USE!",
    hidden=True,
)
# @click.option(
#     "--force-run",
#     is_flag=True,
#     help="Force re-run of flow and all dependencies, even if they are already up-to-date",
# )
@click.option(
    "--xedaproject",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        writable=False,
        readable=True,
        resolve_path=True,
        allow_dash=False,
        path_type=Path,
    ),
    # cls=ClickMutex,
    # mutually_exclusive_with=["design_file"],
    help="Path to Xeda project file.",
)
@click.option(
    "--design-name",
    # cls=ClickMutex,
    # mutually_exclusive_with=["design_file"],
    help="Specify design.name in case multiple designs are available in a xedaproject.",
)
@click.option(
    "--design-file",
    "--design",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        writable=False,
        readable=True,
        resolve_path=True,
        allow_dash=False,
        path_type=Path,
    ),
    # cls=ClickMutex,
    # mutually_exclusive_with=["xedaproject"],
    help="Path to Xeda design file containing the description of a single design.",
)
@click.option(
    "--flow-settings",
    "--settings",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE overrides with commas. KEY can be a hierarchical name using dot notation.
    Example: --settings clock_period=2.345 impl.strategy=Debug
    """
    #  examples: # FIXME move to docs
    # - xeda vivado_sim --flow-settings stop_time=100us
    # - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock_period=2.345
)  # pylint: disable=C0116:missing-function-docstring
@click.pass_context
def run(
    ctx: click.Context,
    flow: str,
    cached_dependencies: bool,
    run_in_existing_dir: bool = False,
    # force_run: bool = False,
    xeda_run_dir: Optional[Path] = None,
    xedaproject: Optional[str] = None,
    design_name: Optional[str] = None,
    design_file: Optional[str] = None,
    flow_settings: Optional[Tuple[str, ...]] = None,
):
    assert ctx
    options: XedaOptions = ctx.obj or XedaOptions()
    assert xeda_run_dir
    log_to_file(xeda_run_dir / "Logs")
    # get default flow configs from xedaproject even if a design-file is specified
    xeda_project = None
    flows_config = {}
    if not xedaproject:
        xedaproject = "xedaproject.toml"
    if Path(xedaproject).exists():
        try:
            xeda_project = XedaProject.from_file(xedaproject)
        except DesignValidationError as e:
            log.critical("%s", e)
            sys.exit(1)
        except FileNotFoundError:
            sys.exit(
                f"Cannot open project file: {xedaproject}. Try specifing the correct path using the --xedaproject <path-to-file>."
            )
        flows_config = xeda_project.flows
    if design_file:
        try:
            design = Design.from_toml(design_file)
        except DesignValidationError as e:
            log.critical("%s", e)
            sys.exit(1)
    else:
        if not xeda_project:
            sys.exit(
                "No design file or project files were specified and no `xedaproject.toml` was found in the working directory."
            )
        designs = xeda_project.designs
        assert isinstance(xeda_project.design_names, list)  # type checker
        log.info(
            "Available designs in xedaproject: %s",
            ", ".join(xeda_project.design_names),
        )
        design = xeda_project.get_design(design_name)
        if not design:
            if design_name:
                log.critical(
                    'Design "%s" not found in %s. Available designs are: %s',
                    design_name,
                    xedaproject,
                    ", ".join(xeda_project.design_names),
                )
                sys.exit(1)
            else:
                if console.is_interactive:
                    terminal_menu = TerminalMenu(
                        xeda_project.design_names, title="Please select a design: "
                    )
                    idx = terminal_menu.show()
                    if idx is None or not isinstance(idx, int) or idx < 0:
                        sys.exit("Invalid design choice!")
                    design = designs[idx]
                else:
                    design_name = click.prompt(
                        "Please enter design name: ",
                        type=click.Choice(xeda_project.design_names),
                    )
                    if not design_name or design_name not in xeda_project.design_names:
                        sys.exit("Invalid design name!")
                    design = xeda_project.get_design(design_name)
            if not design:
                sys.exit("[ERROR] design is empty?!")

    flow_overrides = settings_to_dict(flow_settings)
    log.debug("flow_overrides: %s", flow_overrides)
    flow_overrides = {**flows_config.get(flow, {}), **flow_overrides}
    flow_class = discover_flow_class(flow)
    runner = DefaultRunner(
        xeda_run_dir,
        debug=options.debug,
        cached_dependencies=cached_dependencies,
        run_in_existing_dir=run_in_existing_dir,
    )
    try:
        runner.run_flow(flow_class, design, flow_overrides)
    except FlowFatalError as e:
        log.critical(
            "Flow %s failed: FlowFatalException %s",
            flow,
            " ".join(str(a) for a in e.args),
        )
        sys.exit(1)
    except NonZeroExitCode as e:
        log.critical(
            "Flow %s failed: NonZeroExitCode %s", flow, " ".join(str(a) for a in e.args)
        )
        sys.exit(1)
    except ExecutableNotFound as e:
        log.critical(
            "Executable '%s' was not found! (tool:%s, flow:%s, PATH:%s)",
            e.exec,
            e.tool,
            flow,
            e.path,
        )
        sys.exit(1)
    except FlowSettingsError as e:
        log.critical("%s", e)
        if options.debug:
            raise e from None
        sys.exit(1)
    except FlowException as e:  # any flow exception
        log.critical("%s", e)
        if options.debug:
            raise e from None
        sys.exit(1)


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List available flows.")
def list_flows():
    table = Table(
        title="Available flows",
        show_header=True,
        header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    table.add_column("Flow", header_style="bold green", style="bold")
    table.add_column("Description")
    table.add_column("Class", style="dim")
    super_flow_doc = inspect.getdoc(Flow)
    for cls_name, (_mod, cls) in sorted(registered_flows.items()):
        doc = inspect.getdoc(cls)
        if doc == super_flow_doc:
            doc = "<no description>"
        table.add_row(
            cls_name,
            doc,
            str(cls.__module__) + "." + cls.__name__,
        )
    console.print(table)


@cli.command(
    context_settings=CONTEXT_SETTINGS, short_help="List flow settings information"
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=click.Choice(sorted(list(registered_flows))),
    required=True,
)
@click.pass_context
def list_settings(ctx: click.Context, flow):
    print_flow_settings(flow, options=ctx.obj)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Design-space exploration: run several instances of a flow to find optimal parameters and results",
)
@click.argument("flow")
@click.option(
    "--max-cpus",
    default=max(1, multiprocessing.cpu_count()),
    type=int,
    help="Maximum total number of logical CPU cores to use.",
    show_default=True,
    show_envvar=True,
)
@click.pass_context
def dse(ctx, flow, max_cpus):  # pylint: disable=unused-argument
    """Design-space exploration (e.g. fmax)"""


SHELLS: dict[str, dict[str, Any]] = {
    "bash": {
        "eval_file": "~/.bashrc",
        "eval": 'eval "$(_XEDA_COMPLETE=bash_source xeda)"',
        "completion_file": None,
    },
    "zsh": {
        "eval_file": "~/.zshrc",
        "eval": 'eval "$(_XEDA_COMPLETE=zsh_source xeda)"',
        "completion_file": None,
    },
    "fish": {
        "eval_file": "~/.config/fish/completions/xeda.fish",
        "eval": "eval (env _XEDA_COMPLETE=fish_source xeda)",
        "completion_file": None,
    },
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
    "--stdout",
    is_flag=True,
    cls=ClickMutex,
    help="""Produce the shell completion script and output it to the standard output.\n
    Example usage:\n
         $ xeda completion zsh --stdout  > $HOME/.zsh/completion/_xeda\n
         $ xeda completion bash --stdout > $HOME/.local/share/bash-completion/xeda
    """,
)
@click.pass_context
def completion(_ctx: click.Context, stdout, shell=None):
    """Xeda shell auto-completion"""
    os_default_shell_name = None
    os_default_shell = os.environ.get("SHELL")
    if os_default_shell:
        os_default_shell_name = os_default_shell.split(os.sep)[-1]
    if os_default_shell_name:
        if not shell:
            shell = os_default_shell_name
        elif os_default_shell_name != shell and not stdout:
            console.print(
                f"[yellow]WARNING:[/] Current default shell ([bold]{os_default_shell}[/]) is different from the specified shell [bold]{shell}[/b]"
            )
    assert shell is not None
    if stdout:
        completion_class = get_completion_class(shell)
        if completion_class:
            complete = completion_class(
                cli=cli, ctx_args={}, prog_name=__package__, complete_var="source_xeda"
            )
            print(complete.source())
    else:
        shell_desc = SHELLS.get(shell, {})
        console.print(
            f"""
    Make sure 'xeda' executable is properly installed and is accessible through the shell's PATH.
        [yellow]$ xeda --version[/]
    Add the following line to [magenta underline]{shell_desc.get('eval_file')}[/]:
        {shell_desc.get('eval')}
            """,
            highlight=False,
        )


@cli.command()
@click.argument("subcommand", required=False)
@click.pass_context
def help(ctx: click.Context, subcommand=None):  # pylint: disable=redefined-builtin
    if subcommand:
        subcommand_obj = cli.get_command(ctx, subcommand)
        if subcommand_obj is None:
            click.echo("I don't know that command.")
        else:
            click.echo(subcommand_obj.get_help(ctx))
    else:
        click.echo(cli.get_usage(ctx))
