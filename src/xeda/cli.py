# © 2022-2025 [Kamyar Mohajerani](mailto:kammoh@gmail.com)
"""Xeda Command-line interface"""

import logging
import os
import re
import sys
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import click
import coloredlogs
from click.shell_completion import get_completion_class
from click_extra import Command as ColorizedCommand
from click_extra import HelpKeywords
from rich import box
from rich.markup import escape
from rich.style import Style
from rich.table import Table

from .agent_skill import SKILL_NAME, default_skill_dir, generate_flows_reference, install_skill
from .cli_utils import (
    DOCUMENT_FORMATS,
    HELP_FORMATTER_SETTINGS,
    ClickMutex,
    FlowChoice,
    OptionEatAll,
    XedaHelpGroup,
    emit_structured,
    machine_readable_mode,
    output_format_option,
    print_flow_settings,
    resolve_format,
    select_design_in_project,
)
from .console import console
from .design import DesignValidationError
from .flow import Flow, FlowException, FlowFatalError, FlowSettingsError, registered_flows
from .flow_runner import (
    DIR_NAME_HASH_LEN,
    DefaultRunner,
    FlowNotFoundError,
    XedaOptions,
    add_file_logger,
    get_flow_class,
    scrub_runs,
)
from .flow_runner.dse import Dse
from .flows import __builtin_flows__
from .introspect import (
    boards_info,
    design_schema,
    flows_info,
    json_safe,
    optimizers_info,
    platforms_info,
    results_info,
)
from .tool import ExecutableNotFound, NonZeroExitCode
from .utils import XedaException, removeprefix, settings_to_dict

log = logging.getLogger(__name__)

all_flows = OrderedDict(
    sorted(
        [
            (name, f)
            for f in set(__builtin_flows__).union(set(v for _, v in registered_flows.values()))
            for name in [f.name] + f.aliases
        ]
    )
)
all_flow_names = list(all_flows.keys())

CONTEXT_SETTINGS = dict(
    auto_envvar_prefix="XEDA",
    help_option_names=["--help", "-h"],
    max_content_width=console.width,
    # cloup reads the help formatter off the context and child contexts inherit these settings,
    # so the xeda palette set here reaches every subcommand's help screen.
    formatter_settings=HELP_FORMATTER_SETTINGS,
)


class LoggerContextFilter(logging.Filter):
    def filter(self, record):
        record.name = removeprefix(record.name, "xeda.")
        # Don't filter the record.
        return 1


def setup_logger(log_level, detailed_logs, log_to_file: Optional[Path] = None):
    logging.getLogger().setLevel(log_level)
    coloredlogs.install(
        level=log_level,
        fmt=(
            "[%(name)s] %(asctime)s %(levelname)s %(message)s"
            if detailed_logs
            else "%(levelname)s %(message)s"
        ),
        logger=log.root,
    )
    if detailed_logs:
        for handler in logging.getLogger().handlers:
            handler.addFilter(LoggerContextFilter())
    if log_to_file:
        add_file_logger(log_to_file)


@click.group(cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@click.option("--verbose", "-v", is_flag=True, help="Enables verbose mode.")
@click.option("--quiet", "-q", is_flag=True, help="Enable quiet mode.")
@click.option("--debug", "-d", show_envvar=True, is_flag=True)
@click.version_option(message="Xeda v%(version)s")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)


def _info_table(title: str, columns: Iterable[Tuple[str, Dict[str, Any]]]) -> Table:
    table = Table(
        title=title,
        show_header=True,
        header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    for name, kwargs in columns:
        # fold rather than ellipsize: a truncated name cannot be typed back into a command
        table.add_column(name, overflow="fold", **kwargs)
    return table


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List available flows.")
@output_format_option()
def list_flows(output_format: str, json_flag: bool):
    """List every flow xeda can run, with its aliases, category and dependencies."""
    fmt = resolve_format(output_format, json_flag)
    info = flows_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = _info_table(
        "Available flows",
        [
            ("Flow", {"header_style": "bold green", "style": "bold"}),
            ("Category", {}),
            ("Description", {}),
            ("Depends on", {}),
            ("Class", {"style": "dim"}),
        ],
    )
    for flow in info:
        name = escape(flow["name"])
        if flow["aliases"]:
            name += "\n[dim]aka " + escape(", ".join(flow["aliases"])) + "[/dim]"
        table.add_row(
            name,
            flow["category"].replace("_", " "),
            escape(flow["description"]) if flow["description"] else "[red]<no description>[/red]",
            escape(", ".join(flow["dependencies"])) or "-",
            escape(flow["qualified_name"]),
        )
    console.print(table)


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List flow settings information")
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(all_flow_names),
    required=True,
)
@output_format_option()
@click.option(
    "--common/--no-common",
    default=True,
    show_default=True,
    help="Include the settings accepted by every flow (ncpus, dockerized, lib_paths, ...).",
)
@click.pass_context
def list_settings(ctx: click.Context, flow, output_format: str, json_flag: bool, common: bool):
    """Show every setting of FLOW_NAME that can be given as `-s KEY=VALUE` or in a design file."""
    print_flow_settings(
        flow,
        options=ctx.obj,
        output_format=resolve_format(output_format, json_flag),
        include_common=common,
    )


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="List the result keys a flow reports.",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(all_flow_names),
    required=True,
)
@output_format_option()
def list_results(flow, output_format: str, json_flag: bool):
    """Show the keys FLOW_NAME writes to `results.json` in its run directory."""
    fmt = resolve_format(output_format, json_flag)
    info = results_info(flow)
    if fmt != "table":
        emit_structured(info, fmt, records=[{"flow": info["flow"], **k} for k in info["keys"]])
        return
    table = _info_table(
        f"{info['flow']} results",
        [
            ("Key", {"header_style": "bold green", "style": "bold"}),
            ("Scope", {}),
            ("Description", {}),
        ],
    )
    for key in info["keys"]:
        table.add_row(
            escape(key["name"]),
            "all flows" if key["common"] else escape(info["flow"]),
            escape(key["description"]) if key["description"] else "[dim]<undocumented>[/dim]",
        )
    console.print(table)
    console.print(f"[dim]{info['note']}[/dim]")


@cli.command(
    "design-schema",
    context_settings=CONTEXT_SETTINGS,
    short_help="Print the JSON Schema of a design description file.",
)
@output_format_option(formats=DOCUMENT_FORMATS, default="json")
def design_schema_cmd(output_format: str, json_flag: bool):
    """Emit the JSON Schema of a xeda design file (TOML/YAML/JSON).

    Useful for validating a design description, or for generating one correctly.
    """
    emit_structured(design_schema(), resolve_format(output_format, json_flag))


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List bundled FPGA boards.")
@output_format_option()
def list_boards(output_format: str, json_flag: bool):
    """List the FPGA boards xeda ships, usable as the `board` setting of FPGA flows."""
    fmt = resolve_format(output_format, json_flag)
    info = boards_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = _info_table(
        "Bundled FPGA boards",
        [
            ("Board", {"header_style": "bold green", "style": "bold"}),
            ("Name", {}),
            ("FPGA part", {}),
        ],
    )
    for board in info:
        fpga = board.get("fpga") or {}
        part = fpga.get("part") if isinstance(fpga, dict) else str(fpga)
        table.add_row(
            escape(board["board"]), escape(str(board.get("name", "-"))), escape(str(part or "-"))
        )
    console.print(table)


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List bundled ASIC platforms (PDKs).")
@output_format_option()
def list_platforms(output_format: str, json_flag: bool):
    """List the ASIC platforms available as the `platform` setting of the OpenROAD/DC flows."""
    fmt = resolve_format(output_format, json_flag)
    info = platforms_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = _info_table(
        "Bundled ASIC platforms",
        [
            ("Platform", {"header_style": "bold green", "style": "bold"}),
            ("Process", {}),
            ("Description", {}),
        ],
    )
    for platform in info:
        table.add_row(
            escape(platform["platform"]),
            escape(str(platform.get("process", "-"))),
            escape(str(platform.get("description") or platform.get("error") or "-")),
        )
    console.print(table)
    if not info:
        console.print(
            "[yellow]No platforms found.[/] Platform data is not shipped in the xeda wheel."
        )


@cli.command(
    context_settings=CONTEXT_SETTINGS, short_help="List design-space exploration optimizers."
)
@output_format_option()
def list_optimizers(output_format: str, json_flag: bool):
    """List the optimizers usable as `xeda dse --optimizer <name>`, and their settings."""
    fmt = resolve_format(output_format, json_flag)
    info = optimizers_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    for optimizer in info:
        table = _info_table(
            f"{optimizer['name']} settings",
            [
                ("Setting", {"header_style": "bold green", "style": "bold"}),
                ("Type", {}),
                ("Default", {}),
                ("Description", {}),
            ],
        )
        for setting in optimizer["settings"]:
            table.add_row(
                escape(setting["name"]),
                escape(setting["type"]),
                escape(str(setting["default"])),
                escape(setting["description"] or ""),
            )
        console.print(
            f"[bold green]{escape(optimizer['name'])}[/] - "
            f"{escape(optimizer['description'] or '')}"
        )
        console.print(table)


def _run_document(
    flow_name: str, design: Any, flow_obj: Optional[Flow], success: bool
) -> Dict[str, Any]:
    """The machine-readable summary emitted by `xeda run --json`."""
    document: Dict[str, Any] = {
        "flow": flow_name,
        "design": str(design),
        "success": success,
        "results": {},
        "run_path": None,
    }
    if flow_obj is not None:
        run_path = Path(flow_obj.run_path)
        document.update(
            flow=flow_obj.name,
            design=flow_obj.design.name,
            run_path=str(run_path),
            results_json=str(run_path / "results.json"),
            settings_json=str(run_path / "settings.json"),
            results=json_safe(dict(flow_obj.results)),
        )
    if not success:
        document["error"] = {
            "type": "FlowFailed",
            "message": f"Flow '{document['flow']}' did not complete successfully.",
        }
    return document


@cli.command(
    # The class `XedaHelpGroup` would pick anyway; naming it is what lets cloup's `command()`
    # overloads accept the `excluded_keywords` keyword below.
    cls=ColorizedCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help="Run the flow identified by FLOW_NAME. A snake_case styled FLOW_NAME (e.g. ghdl_sim) is converted to a CamelCase class name (e.g. GhdlSim).",
    no_args_is_help=False,
    # click-extra highlights command names wherever they appear in help prose. Here the command
    # is named after an ordinary English verb, so "Don't run dependency flows" and "Flows run
    # under ..." would be painted as if they referenced the command.
    excluded_keywords=HelpKeywords(cli_names={"run"}),
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(all_flow_names),
)
@click.argument(
    "design_file",
    metavar="DESIGN",
    type=click.Path(
        file_okay=True,
        dir_okay=False,
        readable=True,
        exists=True,
        path_type=Path,
    ),
    default=None,
    required=False,
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
    default=False,
    help="Don't run dependency flows if a previous successfull run on the same design and flow settings exists. Generated directory names will contain a hash of design and/or flow settings.",
)
@click.option(
    "--incremental/--no-incremental",
    default=True,
    help="Incremental build. Useful during development. Flows run under a <design_name>/<flow_name>_<flow_settings_hash> subfolder in incremental mode.",
)
@click.option(
    "--cwd",
    is_flag=True,
    help="Run incremental execution in the current working directory.",
)
@click.option(
    "--clean",
    is_flag=True,
    default=False,
    help="Run `clean` before `run`.",
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
    "--design-file",
    "--design",
    # A destination of its own: the positional DESIGN argument is already named `design_file`,
    # and two parameters sharing a name overwrite each other during parsing (click 8.5 warns
    # about it). The argument used to win unconditionally, which made this option a no-op.
    "design_file_opt",
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
    cls=ClickMutex,
    # mutually_exclusive_with=["design_name"],
    help="Path to Xeda design file containing the description of a single design.",
)
@click.option(
    "--design-name",
    cls=ClickMutex,
    # mutually_exclusive_with=["design_file"],
    help="Specify design.name in case multiple designs are available in a xedaproject.",
)
@click.option(
    "--design-overrides",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Override design attributes.""",
)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    type=bool,
    is_flag=True,
    default=False,
    help="""Allow extra properties (fields) in design description.""",
)
@click.option(
    "--flow-settings",
    "--settings",
    "-s",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE overrides with commas. KEY can be a hierarchical name using dot notation.
    Example: --settings clock_period=2.345 impl.strategy=Debug
    """,
    #  examples: # FIXME move to docs
    # - xeda vivado_sim --flow-settings stop_time=100us
    # - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock_period=2.345
)
@click.option("--detailed-logs/--no-detailed-logs", show_envvar=True, default=False)
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
@click.option(
    "--remote",
    type=str,
    show_envvar=True,
    help="Run on a remote machine with SSH access. Xeda needs to be installed on the remote machine and PATH env variable should be set correctly.",
)
@click.option(
    "--debug",
    "-d",
    is_flag=True,
    default=False,
    help="Run in debug mode.",
)
@click.option(
    "--help-settings",
    "--list-settings",
    is_flag=True,
    default=False,
    help="List flow settings. This option is an alias for `xeda list-settings <flow>`.",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help=(
        "Write a machine-readable JSON summary of the run to stdout. Tool output, log messages "
        "and the results table are written to stderr so stdout carries only the JSON document."
    ),
)
@click.pass_context
def run(
    ctx: click.Context,
    flow: str,
    design_file: Optional[str] = None,
    design_file_opt: Optional[str] = None,
    cached_dependencies: bool = True,
    flow_settings: Union[None, str, Iterable[str]] = None,
    incremental: bool = True,
    clean: bool = False,
    xeda_run_dir: Optional[Path] = None,
    xedaproject: Optional[str] = None,
    # design: Optional[str] = None,
    design_name: Optional[str] = None,
    design_overrides: Iterable[str] = tuple(),
    design_allow_extra: bool = False,
    log_level: Optional[int] = None,
    detailed_logs: bool = False,
    post_cleanup: bool = False,
    post_cleanup_purge: bool = False,
    scrub: bool = False,
    remote: Optional[str] = None,
    cwd: bool = False,
    debug: bool = False,
    help_settings: bool = False,
    json_flag: bool = False,
):
    """`run` command"""
    assert ctx
    options: XedaOptions = ctx.obj or XedaOptions()
    if json_flag:
        # stdout belongs to the JSON document from here on
        machine_readable_mode()
    if help_settings:
        print_flow_settings(flow, options=options, output_format="json" if json_flag else "table")
        sys.exit(0)
    debug |= options.debug
    if cwd and remote:
        log.critical("--cwd and --remote are mutually exclusive!")
        sys.exit(1)
    if cwd:
        xeda_run_dir = Path.cwd()
    elif xeda_run_dir is None:
        xeda_run_dir = Path.cwd() / "xeda_run"

    if log_level is None:
        log_level = logging.DEBUG if debug else logging.WARNING if options.quiet else logging.INFO
    detailed_logs |= debug
    setup_logger(log_level, detailed_logs)
    if flow_settings is not None:
        flow_settings = list(flow_settings)
    else:
        flow_settings = []

    # The positional DESIGN argument, then --design-file/--design, then --design-name.
    design = design_file or design_file_opt or design_name
    if not design:
        message = (
            "No design specified. Pass a design file as the DESIGN argument, or name one with "
            "--design-file / --design-name."
        )
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": None,
                    "success": False,
                    "results": {},
                    "error": {"type": "DesignNotSpecified", "message": message},
                },
                "json",
            )
        sys.exit(1)

    if remote:
        from .flow_runner.remote import RemoteRunner

        rl = RemoteRunner(
            xeda_run_dir,
            cached_dependencies=cached_dependencies,
        )
        assert design
        try:
            remote_results = rl.run_remote(design, flow, host=remote, flow_settings=flow_settings)
        except XedaException as e:
            log.critical("XedaException: %s", e)
            if json_flag:
                emit_structured(
                    _remote_document(flow, design, remote, None, False, error=e), "json"
                )
            if debug:
                raise e
            sys.exit(1)
        except Exception as e:
            if not json_flag:
                raise
            log.critical("%s: %s", type(e).__name__, e)
            emit_structured(_remote_document(flow, design, remote, None, False, error=e), "json")
            if debug:
                raise
            sys.exit(1)
        # The remote flow's own success decides ours; a failed flow is not a successful run.
        success = bool(remote_results and remote_results.get("success"))
        if not success:
            log.critical("Remote run of flow '%s' on '%s' failed.", flow, remote)
        if json_flag:
            emit_structured(_remote_document(flow, design, remote, remote_results, success), "json")
        sys.exit(0 if success else 1)

    def emit_failure(error_type: str, message: str, exc: Exception) -> None:
        """Report a failed run identically whether the caller wants text or JSON."""
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": str(design),
                    "success": False,
                    "results": {},
                    "error": {"type": error_type, "message": message},
                },
                "json",
            )
        if debug:
            raise exc
        sys.exit(1)

    try:
        launcher = DefaultRunner(
            xeda_run_dir,
            cached_dependencies=cached_dependencies,
        )
        launcher.settings.cleanup_before_run = clean
        if cwd:
            launcher.settings.incremental = True
            launcher.settings.run_path = Path.cwd()
            launcher.settings.dump_settings_json = False
            launcher.settings.dump_results_json = True
        else:
            launcher.settings.incremental = incremental
            launcher.settings.post_cleanup = post_cleanup
            launcher.settings.post_cleanup_purge = post_cleanup_purge
            launcher.settings.scrub_old_runs = scrub
        launcher.settings.debug = debug
        f = launcher.run(
            flow,
            xedaproject=xedaproject,
            design=design or design_name,
            flow_settings=flow_settings,
            select_design_in_project=select_design_in_project,
            design_overrides=design_overrides,
            design_allow_extra=design_allow_extra,
        )
        success = bool(f and f.results.success)
        if json_flag:
            emit_structured(_run_document(flow, design, f, success), "json")
        sys.exit(0 if success else 1)
    except FlowNotFoundError as e:
        emit_failure("FlowNotFoundError", str(e), e)
    except FlowFatalError as e:
        emit_failure(
            "FlowFatalError",
            f"Flow {flow} failed: FlowFatalException {' '.join(str(a) for a in e.args)}",
            e,
        )
    except NonZeroExitCode as e:
        emit_failure("NonZeroExitCode", f"Flow {flow} failed: {e}", e)
    except ExecutableNotFound as e:
        emit_failure(
            "ExecutableNotFound",
            f"Executable '{e.exec}' was not found in PATH. flow:{flow}, tool:{e.tool}"
            + (f", PATH:{e.path}" if debug else ""),
            e,
        )
    except FlowSettingsError as e:
        emit_failure("FlowSettingsError", str(e), e)
    except FlowException as e:  # any flow exception
        emit_failure("FlowException", str(e), e)
    except DesignValidationError as e:
        emit_failure("DesignValidationError", str(e), e)
    except XedaException as e:
        emit_failure("XedaException", str(e), e)
    except Exception as e:
        if not json_flag:
            raise
        emit_failure(type(e).__name__, f"{type(e).__name__}: {e}", e)


def _remote_document(
    flow_name: str,
    design: Any,
    host: str,
    results: Optional[Dict[str, Any]],
    success: bool,
    error: Optional[BaseException] = None,
) -> Dict[str, Any]:
    """The machine-readable summary emitted by `xeda run --remote --json`."""
    document: Dict[str, Any] = {
        "flow": flow_name,
        "design": str(design),
        "remote": host,
        "success": success,
        "results": json_safe(results or {}),
    }
    run_path = (results or {}).get("run_path")
    if run_path:
        document["run_path"] = str(run_path)
        document["results_json"] = str(Path(run_path) / "results.json")
    if error is not None:
        document["error"] = {"type": type(error).__name__, "message": str(error)}
    elif not success:
        document["error"] = {
            "type": "FlowFailed",
            "message": f"Remote flow '{flow_name}' did not complete successfully.",
        }
    return document


def _dse_best_document(best: Any) -> Optional[Dict[str, Any]]:
    """The best `FlowOutcome` of a design-space exploration, as JSON-safe data."""
    if best is None:
        return None
    settings: Any = getattr(best, "settings", None)
    if settings is not None and hasattr(settings, "dict"):
        settings = settings.model_dump()
    return {
        "results": json_safe(dict(getattr(best, "results", {}) or {})),
        "settings": json_safe(settings),
        "run_path": str(best.run_path) if getattr(best, "run_path", None) else None,
        "timestamp": getattr(best, "timestamp", None),
    }


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Execute multiple runs in parallel",
    help="Runs multiple instances of a flow in parallel to find optimum value(s) of a settings key with respect to a specified objective function.",
    no_args_is_help=False,
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(all_flow_names),
    required=True,
)
@click.option(
    "--flow-settings",
    "--settings",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
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
    # cls=ClickMutex,
    # mutually_exclusive_with=["design_file"],
    help="Specify design.name in case multiple designs are available in a xedaproject.",
)
@click.option(
    "--design",
    "--design-file",
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
    # mutually_exclusive_with=["design_name"],
    help="Path to Xeda design file containing the description of a single design.",
)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    type=bool,
    is_flag=True,
    default=True,  # by default allow for dse
    help="""Allow extra properties (fields) in design description.""",
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
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    show_envvar=True,
)
@click.option(
    "--optimizer-settings",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
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
@click.option(
    "--debug",
    "-d",
    is_flag=True,
    default=False,
    help="Run in debug mode.",
)
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help=(
        "Write a machine-readable JSON summary of the exploration to stdout; tool output and "
        "logs go to stderr."
    ),
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
    design: Optional[str] = None,
    design_name: Optional[str] = None,
    design_allow_extra: bool = True,
    log_level: Optional[int] = None,
    detailed_logs: bool = True,
    debug: bool = False,
    json_flag: bool = False,
):
    """Design-space exploration (e.g. fmax)"""
    options: XedaOptions = ctx.obj or XedaOptions()
    debug |= options.debug
    if json_flag:
        # stdout belongs to the JSON document from here on
        machine_readable_mode()

    if not xeda_run_dir:
        xeda_run_dir = Path.cwd() / ("xeda_run_" + optimizer)

    if log_level is None:
        log_level = (
            logging.WARNING if options.quiet else logging.DEBUG if options.debug else logging.INFO
        )
    if options.debug:
        detailed_logs = True
    setup_logger(log_level, detailed_logs, xeda_run_dir / "Logs")

    opt_settings = settings_to_dict(optimizer_settings, hierarchical_keys=True)
    dse_settings_dict = settings_to_dict(dse_settings, hierarchical_keys=True)
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

    def dse_failure(error_type: str, message: str, exc: Optional[BaseException] = None) -> None:
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": str(design or design_name),
                    "optimizer": optimizer,
                    "success": False,
                    "best": None,
                    "error": {"type": error_type, "message": message},
                },
                "json",
            )
        if debug and exc is not None:
            raise exc
        sys.exit(1)

    # Resolving the optimizer inside Dse() raises an AttributeError naming nothing useful, so
    # check it here where the available names are known.
    known_optimizers = {o["name"] for o in optimizers_info()}
    if optimizer not in known_optimizers and "." not in optimizer:
        dse_failure(
            "OptimizerNotFound",
            f"Unknown optimizer '{optimizer}'. Available optimizers: "
            f"{', '.join(sorted(known_optimizers))}. See `xeda list-optimizers`.",
        )

    try:
        dse = Dse(
            optimizer_class=optimizer,
            optimizer_settings=opt_settings,
            xeda_run_dir=xeda_run_dir,
            debug=options.debug,
            **dse_settings_dict,
        )
        best = dse.run(
            flow,
            xedaproject=xedaproject,
            design=design or design_name,
            flow_settings=list(flow_settings),
            select_design_in_project=select_design_in_project,
            design_allow_extra=design_allow_extra,
        )
    except SystemExit:
        raise
    except BaseException as e:
        dse_failure(type(e).__name__, f"{type(e).__name__}: {e}", e)
        raise  # unreachable: dse_failure exits
    if json_flag:
        document = {
            "flow": flow,
            "design": str(design or design_name),
            "optimizer": optimizer,
            "success": best is not None,
            "best": _dse_best_document(best),
        }
        if best is None:
            document["error"] = {
                "type": "NoSuccessfulRun",
                "message": "The exploration produced no successful run.",
            }
        emit_structured(document, "json")
    # An exploration that produced no successful run is a failure, like every other command.
    sys.exit(0 if best is not None else 1)


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="Remove all flow runs in the specified xeda_run_dir",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(all_flow_names),
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
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help=(
        "Write a machine-readable JSON summary to stdout; the confirmation prompt and all other "
        "output go to stderr."
    ),
)
@click.pass_context
def scrub(ctx: click.Context, flow, design_name, xeda_run_dir, incremental, json_flag):
    if json_flag:
        machine_readable_mode()
    xeda_run_dir = Path(xeda_run_dir).resolve()
    # just to make sure flow exists and name is canonical
    flow_class = get_flow_class(flow)

    regex = re.compile(f"^{re.escape(design_name)}_" + (r"[a-z0-9]" * DIR_NAME_HASH_LEN) + r"$")
    design_dirs = [
        p for p in xeda_run_dir.glob(f"{design_name}_*") if p.is_dir() and regex.match(p.name)
    ]
    if incremental and (xeda_run_dir / design_name).exists():
        design_dirs.append(xeda_run_dir / design_name)

    scrubbed = [dd for dd in design_dirs if scrub_runs(flow_class.name, dd)]
    if json_flag:
        emit_structured(
            {
                "success": True,
                "flow": flow_class.name,
                "design": design_name,
                "xeda_run_dir": str(xeda_run_dir),
                "scanned": [str(d) for d in design_dirs],
                "scrubbed": [str(d) for d in scrubbed],
            },
            "json",
        )


@cli.group(
    context_settings=CONTEXT_SETTINGS,
    cls=XedaHelpGroup,
    short_help="Install the Xeda skill for coding agents.",
)
def skill():
    """Instructions and a generated reference that teach a coding agent to drive Xeda.

    The flow catalog is generated from the installed version of xeda, so it cannot drift from the
    flows and settings you actually have.
    """


@skill.command("install", context_settings=CONTEXT_SETTINGS, short_help="Write the skill to disk.")
@click.option(
    "--dir",
    "dest_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Directory to install into. Defaults to ./.claude/skills",
    show_default=False,
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing installation.")
def skill_install(dest_dir: Optional[Path], force: bool):
    """Install the Xeda agent skill into DIR/xeda (default: ./.claude/skills/xeda)."""
    dest_dir = dest_dir or default_skill_dir()
    try:
        written = install_skill(dest_dir, force=force)
    except FileExistsError as e:
        raise click.ClickException(str(e)) from e
    console.print(
        f"Installed the [bold]{SKILL_NAME}[/] skill into [bold]{dest_dir / SKILL_NAME}[/]:"
    )
    for path in written:
        console.print(f"  {path}")


@skill.command(
    "reference",
    context_settings=CONTEXT_SETTINGS,
    short_help="Print the generated flow catalog.",
)
def skill_reference():
    """Print the generated flow catalog to stdout, without writing anything."""
    sys.stdout.write(generate_flows_reference())


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
                cli=cli, ctx_args={}, prog_name=__package__ or "xeda", complete_var="source_xeda"
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
