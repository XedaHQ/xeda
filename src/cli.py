# © 2022 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
"""Xeda Command-line interface"""
import inspect
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, Union

import click
import coloredlogs
from click.shell_completion import get_completion_class
from rich import box
from rich.style import Style
from rich.table import Table

from .cli_utils import (
    ClickMutex,
    OptionEatAll,
    XedaHelpGroup,
    print_flow_settings,
    select_design_in_project,
)
from .console import console
from .flow import (
    Flow,
    FlowException,
    FlowFatalError,
    FlowSettingsError,
    registered_flows,
)
from .flow_runner import (
    DIR_NAME_HASH_LEN,
    DefaultRunner,
    XedaOptions,
    add_file_logger,
    get_flow_class,
    prepare,
    scrub_runs,
    settings_to_dict,
)
from .flow_runner.dse import Dse
from .flows import __all__ as all_flows__
from .tool import ExecutableNotFound, NonZeroExitCode
from .utils import removeprefix

log = logging.getLogger(__name__)


all_flows = sorted(list(registered_flows.keys()))

if len(all_flows__) != len(all_flows):
    print(f"FIXME: {len(all_flows__)}---{len(all_flows)}", file=sys.stderr)

CONTEXT_SETTINGS = dict(
    auto_envvar_prefix="XEDA",
    help_option_names=["--help", "-h"],
    max_content_width=console.width,
)


class LoggerContextFilter(logging.Filter):
    def filter(self, record):
        record.name = removeprefix(record.name, "xeda.")
        # Don't filter the record.
        return 1


def setup_logger(log_level, detailed_logs, write_to_file: Optional[Path] = None):
    logging.getLogger().setLevel(log_level)
    if detailed_logs:
        coloredlogs.install(
            None,
            fmt="[%(name)s] %(asctime)s %(levelname)s %(message)s",
            logger=log.root,
        )
        for handler in logging.getLogger().handlers:
            handler.addFilter(LoggerContextFilter())
    if write_to_file:
        add_file_logger(write_to_file)


@click.group(cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@click.option("--verbose", is_flag=True, help="Enables verbose mode.")
@click.option("--quiet", is_flag=True, help="Enable quiet mode.")
@click.option("--debug", show_envvar=True, is_flag=True)
@click.version_option(message="Xeda v%(version)s")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help="Run the flow identified by FLOW_NAME. A snake_case styled FLOW_NAME (e.g. ghdl_sim) is converted to a CamelCase class name (e.g. GhdlSim).",
    no_args_is_help=False,
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=click.Choice(all_flows),
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
    "--cached-dependencies/--no-cached-dependencies",
    default=True,
    help="Don't run dependency flows if a previous successfull run on the same design and flow settings exists. Generated directory names will contain a hash of design and/or flow settings.",
)
@click.option(
    "--incremental/--no-incremental",
    default=True,
    help="Incremental build. Useful during development. Flows run under a <design_name>/<flow_name>_<flow_settings_hash> subfolder in incremental mode.",
)
@click.option(
    "--incremental-fresh",
    default=False,
    help="In incremental mode, cleanup folder contents before running.",
)
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
)
@click.option("--detailed-logs/--no-detailed-logs", show_envvar=True, default=True)
@click.option("--log-level", show_envvar=True, type=int, default=None)
@click.option(
    "--post-cleanup",
    is_flag=True,
    help="Remove flow files except settings.json, results.json, and artifacts _after_ running the flow.",
)
@click.option(
    "--post-cleanup-purge",
    is_flag=True,
    help="Remove flow run_dir and all of its content _after_ running the flow.",
)
@click.option(
    "--scrub",
    is_flag=True,
    help="Remove all previous flow directories of the same flow withing the current 'xeda_run_dir' _before_ running the flow. Requires user confirmation.",
)
@click.pass_context
def run(
    ctx: click.Context,
    flow: str,
    cached_dependencies: bool,
    flow_settings: Union[None, str, Iterable[str]],
    incremental: bool = True,
    incremental_fresh: bool = False,
    xeda_run_dir: Optional[Path] = None,
    xedaproject: Optional[str] = None,
    design_name: Optional[str] = None,
    design_file: Optional[str] = None,
    log_level: Optional[int] = None,
    detailed_logs: bool = False,
    post_cleanup: bool = False,
    post_cleanup_purge: bool = False,
    scrub: bool = False,
):
    """`run` command"""
    assert ctx
    options: XedaOptions = ctx.obj or XedaOptions()
    assert xeda_run_dir
    if not xeda_run_dir:
        xeda_run_dir = Path.cwd() / "xeda_run"
    if log_level is None:
        log_level = (
            logging.WARNING if options.quiet else logging.DEBUG if options.debug else logging.INFO
        )
    setup_logger(log_level, detailed_logs, xeda_run_dir / "Logs" if detailed_logs else None)
    if isinstance(flow_settings, str):
        flow_settings = flow_settings.split(",")
    if flow_settings is not None:
        flow_settings = list(flow_settings)

    design, flow_class, accum_flow_settings = prepare(
        flow,
        xedaproject=xedaproject,
        design_name=design_name,
        design_file=design_file,
        flow_settings=flow_settings,
        select_design_in_project=select_design_in_project,
    )
    if not design or not flow_class:
        sys.exit(1)
    try:
        launcher = DefaultRunner(
            xeda_run_dir,
            cached_dependencies=cached_dependencies,
        )
        launcher.settings.incremental = incremental
        launcher.settings.incremental_fresh = incremental_fresh
        launcher.settings.post_cleanup = post_cleanup
        launcher.settings.post_cleanup_purge = post_cleanup_purge
        launcher.settings.scrub_old_runs = scrub
        launcher.settings.debug = options.debug
        launcher.run_flow(flow_class, design, accum_flow_settings)
    except FlowFatalError as e:
        log.critical(
            "Flow %s failed: FlowFatalException %s",
            flow,
            " ".join(str(a) for a in e.args),
        )
        sys.exit(1)
    except NonZeroExitCode as e:
        log.critical("Flow %s failed: NonZeroExitCode %s", flow, " ".join(str(a) for a in e.args))
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


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List flow settings information")
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=click.Choice(all_flows),
    required=True,
)
@click.pass_context
def list_settings(ctx: click.Context, flow):
    print_flow_settings(flow, options=ctx.obj)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Run DSE",
    help="Design-space exploration: run several instances of a flow to find optimal parameters and results",
    no_args_is_help=False,
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=click.Choice(all_flows),
    required=True,
)
@click.option(
    "--flow-settings",
    "--settings",
    metavar="KEY=VALUE...",
    multiple=True,
    default=tuple(),
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE overrides with commas. KEY can be a hierarchical name using dot notation.
    Example: --settings clock_period=2.345 impl.strategy=Debug
    """,
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
    show_envvar=True,
    help="Path to Xeda project file.",
)
@click.option(
    "--design-name",
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
    help="Path to Xeda design file containing the description of a single design.",
)
@click.option("--detailed-logs/--no-detailed-logs", show_envvar=True, default=True)
@click.option("--log-level", show_envvar=True, type=int, default=None)
@click.option(
    "--optimizer",
    type=str,
    default="fmax_optimizer",
    show_envvar=True,
    show_default=True,
)
@click.option(
    "--dse-settings",
    metavar="KEY=VALUE...",
    multiple=True,
    default=tuple(),
    show_envvar=True,
)
@click.option(
    "--optimizer-settings",
    metavar="KEY=VALUE...",
    multiple=True,
    default=tuple(),
    show_envvar=True,
)
@click.option(
    "--max-workers",
    type=int,
    default=None,
    help="Maximum number of concurrent flow executions.",
    show_envvar=True,
)
@click.option(
    "--init_freq_low",
    "--init-freq-low",
    type=float,
)
@click.option(
    "--init_freq_high",
    "--init-freq-high",
    type=float,
)
@click.pass_context
def dse(
    ctx: click.Context,
    flow: str,
    flow_settings: Tuple[str, ...],
    optimizer: str,
    optimizer_settings: Tuple[str, ...],
    dse_settings: Tuple[str, ...],
    max_workers: Optional[int],
    init_freq_low: float,
    init_freq_high: float,
    xeda_run_dir: Optional[Path],
    xedaproject: Optional[str] = None,
    design_name: Optional[str] = None,
    design_file: Optional[str] = None,
    log_level: Optional[int] = None,
    detailed_logs: bool = True,
):
    """Design-space exploration (e.g. fmax)"""
    options: XedaOptions = ctx.obj or XedaOptions()

    if not xeda_run_dir:
        xeda_run_dir = Path.cwd() / ("xeda_run_" + optimizer)

    if log_level is None:
        log_level = (
            logging.WARNING if options.quiet else logging.DEBUG if options.debug else logging.INFO
        )

    setup_logger(log_level, detailed_logs, xeda_run_dir / "Logs")

    design, flow_class, accum_flow_settings = prepare(
        flow,
        xedaproject=xedaproject,
        design_name=design_name,
        design_file=design_file,
        flow_settings=list(flow_settings),
        select_design_in_project=select_design_in_project,
    )
    opt_settings = settings_to_dict(optimizer_settings, expand_dict_keys=True)
    dse_settings_dict = settings_to_dict(dse_settings, expand_dict_keys=True)
    if max_workers:
        dse_settings_dict["max_workers"] = max_workers  # overrides

    # will deprecate options and only use optimizer_settings
    opt_settings = {
        **dict(
            init_freq_low=init_freq_low,
            init_freq_high=init_freq_high,
        ),
        **opt_settings,  # optimizer_settings overrides other options
    }
    if not design or not flow_class:
        sys.exit(1)
    dse = Dse(
        optimizer_class=optimizer,
        optimizer_settings=opt_settings,
        xeda_run_dir=xeda_run_dir,
        debug=options.debug,
        **dse_settings_dict,
    )
    dse.run_flow(
        flow_class,
        design,
        accum_flow_settings,
    )


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Remove all flow runs in the specified xeda_run_dir",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=click.Choice(all_flows),
    required=True,
)
@click.argument(
    "design_name",
    metavar="DESIGN_NAME",
    required=True,
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
    "--incremental/--no-incremental",
    default=True,
    help="Include incremental build directories (<design_name>/<flow_name>_<flow_settings_hash> name pattern)",
)
@click.pass_context
def scrub(ctx: click.Context, flow, design_name, xeda_run_dir, incremental):
    xeda_run_dir = Path(xeda_run_dir).resolve()
    # just to make sure flow exists and name is canonical
    flow_class = get_flow_class(flow)

    regex = re.compile(f"^{design_name}_" + (r"[a-z0-9]" * DIR_NAME_HASH_LEN) + r"$")
    design_dirs = [p for p in xeda_run_dir.glob("design_") if p.is_dir() and regex.match(p.name)]
    if incremental and (xeda_run_dir / design_name).exists():
        design_dirs.append(xeda_run_dir / design_name)

    for dd in design_dirs:
        scrub_runs(flow_class.name, dd)


SHELLS: Dict[str, Dict[str, Any]] = {
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
