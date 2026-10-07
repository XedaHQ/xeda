"""Xeda Command-line interface"""

import logging
import os
import sys
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple, Union

import click
import coloredlogs
from click.core import ParameterSource
from click.shell_completion import get_completion_class
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
    DeclaredEnvvarsCommand,
    ChainChoice,
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
from .deliver import Conflict
from .design import DESIGN_NAME, target_name_problem
from .flow import Flow, FlowFatalError, registered_flows
from .flow_runner import (
    DefaultRunner,
    XedaOptions,
    add_file_logger,
    scrub_design,
)
from .flow_runner.bindings import LOCAL_REQUESTS_ONLY
from .flow_runner.chains import parse_request
from .flow_runner.dse import Dse
from .flow_runner.resolver import Plan
from .flows import __builtin_flows__
from .introspect import (
    boards_info,
    design_schema,
    flow_chain_cells,
    flows_info,
    json_safe,
    optimizers_info,
    inputs_info,
    plan_info,
    request_info,
    platforms_info,
    results_info,
)
from .run_dir import RunDirectoryError
from .run_root import RunRootError, ensure_run_root
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


# click-extra uses the command name as prog_name, including for Click's completion protocol.
# Keep it aligned with the console entry point and its _XEDA_COMPLETE environment variable.
@click.group("xeda", cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
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
    """List every flow xeda can run, with its aliases, category, dependencies and file I/O.

    Takes and Makes list a declared flow's inputs and outputs as `name (Types)`: [name] is
    optional, name... is a list. "Can be followed by" names the flows that can come right
    after it in a chain (`xeda run a+b`); `(depends on the target)` marks a relation the
    target's family decides, `flow.output` one that needs that output named. A flow that
    declares no I/O runs alone.
    """
    fmt = resolve_format(output_format, json_flag)
    info = flows_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = _info_table(
        "Available flows",
        [
            # a name is never folded, so it can be typed back into a command
            ("Flow", {"header_style": "bold green", "style": "bold", "no_wrap": True}),
            ("Category", {}),
            ("Description", {}),
            ("Depends on", {}),
            ("Takes", {}),
            ("Makes", {}),
            ("Can be followed by", {}),
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
            *(escape(cell) for cell in flow_chain_cells(flow)),
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


def _error_message(exc: BaseException) -> str:
    """`exc` as the one message that reports it, in text and in `--json` alike: its text, led
    by its kind -- once. Some exceptions already lead their text with their class name
    (`DesignValidationError`, `FlowSettingsError`), and prefixing those again printed
    "DesignValidationError: DesignValidationError: ..."."""
    kind = type(exc).__name__
    text = str(exc)
    if not text:
        return kind
    if any(text.startswith(f"{name}:") for name in (kind, type(exc).__qualname__)):
        return text
    return f"{kind}: {text}"


def _removed_option(replacement: str, false_replacement: Optional[str] = None):
    """A hidden option that only exists to say what replaced it.

    `replacement` is the wording used when the flag as given (`--foo`) was passed; for a
    boolean pair whose negation (`--no-foo`) needs different wording, `false_replacement` gives
    that. Matches the launcher's and flow settings' own wording (`` `<name>` was removed: use
    <replacement>``) exactly, so a removed CLI option, launcher setting and flow setting all read
    the same way.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
        if value is not None:
            given = param.opts[0] if value else param.secondary_opts[0]
            text = replacement if value or false_replacement is None else false_replacement
            raise click.UsageError(f"`{given}` was removed: use {text}", ctx=ctx)

    return callback


def _removed_run_root_option(ctx: click.Context, param: click.Parameter, value: Any) -> None:
    """`--xeda-run-dir` and `XEDA_RUN_DIR`, released names of the run root: whichever was given
    is named. Explicit, because click would suggest `--hashed-run-dirs` for the option and
    silently ignore the variable."""
    if value is not None:
        if ctx.get_parameter_source(param.name or "") is ParameterSource.ENVIRONMENT:
            raise click.UsageError("`XEDA_RUN_DIR` was removed: use XEDA_RUN_ROOT", ctx=ctx)
        raise click.UsageError("`--xeda-run-dir` was removed: use --run-root", ctx=ctx)


def _run_root_options(func):
    """`--run-root` (`XEDA_RUN_ROOT`), and the hidden option refusing its removed names."""
    func = click.option(
        "--xeda-run-dir",
        envvar="XEDA_RUN_DIR",
        hidden=True,
        expose_value=False,
        callback=_removed_run_root_option,
    )(func)
    return click.option(
        "--run-root",
        type=click.Path(
            file_okay=False,
            dir_okay=True,
            writable=True,
            readable=True,
            resolve_path=True,
            allow_dash=True,
            path_type=Path,
        ),
        envvar="XEDA_RUN_ROOT",
        help="Directory holding every run directory. Xeda creates and marks it; keep nothing of "
        "yours there.",
        default="xeda_run",
        show_default=True,
        show_envvar=True,
    )(func)


RUN_HELP = """Run a flow, or the last flow of a chain.

FLOW names one flow (ghdl_sim, GhdlSim and ghdl-sim are the same flow): it runs with whatever
it needs first. A chain, `yosys_fpga+nextpnr+fpga_pack`, runs its last flow, binding each
preceding flow's declared output to the next flow's compatible required inputs; `FLOW.OUTPUT`
names the output when several fit. Unbound inputs use the design's sources or their default
producers. Use + within one argument; a chain does not search for missing stages.

-s KEY=VALUE sets the last flow; -s flows.NAME.KEY=VALUE sets another flow of the run. A
binding can be saved instead, under flows.NAME.inputs in a YAML design or project file. --dry-run
shows the graph and runs no tools. Chains are local requests: not for --remote.

Example: xeda run yosys_fpga+nextpnr+fpga_pack design.yaml
"""


def _node_states(flows: Iterable[Flow], plan: Optional[Plan] = None) -> List[Dict[str, Any]]:
    """One `nodes` entry per run directory the launcher entered, in completion order: what
    `run --json` reports about each node of the run, whether it was reused, ran, or failed. A
    flow that always runs (`Flow.always_runs`) is
    never `reused`, so it is reported as "ran"/"failed", never "fresh"."""
    nodes: List[Dict[str, Any]] = []
    seen = set()
    for f in flows:
        run_path = os.path.abspath(f.run_path)
        seen.add(run_path)
        planned = next(
            (n for n in (plan.nodes if plan else ()) if os.path.abspath(n.run_path) == run_path),
            None,
        )
        nodes.append(
            {
                "node": planned.name if planned else f.name,
                "flow": f.name,
                "run_path": str(f.run_path),
                "state": "fresh" if f.reused else ("ran" if f.succeeded else "failed"),
                "reason": f.stale_reason or "",
                "deliveries": [d.as_json_value() for d in getattr(f, "deliveries", [])],
                "inputs": inputs_info(planned) if planned else [],
            }
        )
    # the planned nodes the launch never entered: a producer failed before their turn
    for planned in plan.nodes if plan else ():
        if os.path.abspath(planned.run_path) not in seen:
            nodes.append(
                {
                    "node": planned.name,
                    "flow": planned.flow_class.name,
                    "run_path": str(planned.run_path),
                    "state": "not run",
                    "reason": "",
                    "deliveries": [],
                    "inputs": inputs_info(planned),
                }
            )
    return json_safe(nodes)


def _interactive(json_flag: bool) -> bool:
    """Whether a question may be asked: a terminal on both ends, no machine-readable output."""
    return not json_flag and sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_overwrite(conflicts: Sequence[Conflict]) -> bool:
    """Ask, on the terminal, whether to replace the files in the way of named outputs; no by
    default, and no when the question cannot be answered."""
    click.echo(
        f"xeda would replace {len(conflicts)} file(s) at the output paths you named:", err=True
    )
    for conflict in conflicts:
        click.echo(f"  {conflict.destination}   {conflict.why}", err=True)
    try:
        return click.confirm("Replace them?", default=False, err=True)
    except click.Abort:
        return False


def _run_document(
    flow_name: str,
    design: Any,
    flow_obj: Optional[Flow],
    success: bool,
    nodes: Iterable[Flow] = (),
    plan: Optional[Plan] = None,
    target: str | None = None,
) -> Dict[str, Any]:
    """The machine-readable summary emitted by `xeda run --json`."""
    document: Dict[str, Any] = {
        "flow": flow_name,
        "design": str(design),
        "target": target,
        "success": success,
        "results": {},
        "run_path": None,
    }
    if flow_obj is not None:
        run_path = Path(flow_obj.run_path)
        document.update(
            flow=flow_obj.name,
            design=flow_obj.design.name,
            target=flow_obj.design.target,
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
    document["nodes"] = _node_states(nodes, plan)
    return document


def _print_plan(plan: Plan) -> None:
    """Print each planned flow, its directory, input origins and switched-on outputs."""
    target = f", target {plan.context.target}" if plan.context.target else ""
    click.echo(f"Plan for {plan.requested}{target} (a dry run: nothing runs)")
    for node in plan.nodes:
        click.echo(f"  {node.name}  {node.run_path}")
        # static class metadata, never the flow's dynamic `always_runs()`
        if node.flow_class.action_reason:
            click.echo(f"      always runs: {node.flow_class.action_reason}")
        for resolved in node.inputs:
            click.echo(f"      {resolved.describe()}")
        for output in node.switched_on:
            click.echo(f"      output {output} switched on: a consumer reads it")


@cli.command(
    # Naming the class is also what lets cloup's `command()` overloads accept the
    # `excluded_keywords` keyword below.
    cls=DeclaredEnvvarsCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help=RUN_HELP,
    no_args_is_help=False,
    # click-extra highlights command names wherever they appear in help prose. Here the command
    # is named after an ordinary English verb, so "Don't run dependency flows" and "Flows run
    # under ..." would be painted as if they referenced the command.
    excluded_keywords=HelpKeywords(cli_names={"run"}),
)
@click.argument(
    "flow",
    metavar="FLOW[.OUTPUT][+FLOW[.OUTPUT]...]",
    type=ChainChoice(all_flow_names),
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
@_run_root_options
@click.option(
    "--rebuild-all",
    is_flag=True,
    default=False,
    help="Run every flow, dependencies included, even one that is up to date. Without it, a flow "
    "runs only when its sources, settings, tools or outputs changed since its last successful "
    "run.",
)
@click.option(
    "--hashed-run-dirs",
    is_flag=True,
    default=False,
    help="Give each variant of a flow (its settings and where its inputs come from) its own "
    "run directory, <design>/<flow>_<run hash>, instead of one per flow, <design>/<flow>.",
)
@click.option(
    "--cached-dependencies/--no-cached-dependencies",
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "the default, which reuses unchanged runs, and --hashed-run-dirs to keep settings "
        "variants side by side",
        "--rebuild-all to run every flow",
    ),
)
@click.option(
    "--incremental/--no-incremental",
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "the default behavior: run directories are always reused, and --clean empties them first",
        "--clean to empty run directories before running",
    ),
)
@click.option(
    "--cwd",
    is_flag=True,
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "--outputs-to . to receive the outputs here; the run itself goes under the run root "
        "(./xeda_run)"
    ),
)
@click.option(
    "--clean",
    is_flag=True,
    default=False,
    help="Empty each flow's run directory before it runs, and run every flow (implies "
    "--rebuild-all).",
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
    help="Path to Xeda project file. By default, discover one xedaproject.yaml, .yml or .toml.",
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
    "--target",
    metavar="NAME",
    help="Select one of the design's `targets` (`targets.NAME` in the design file). A design "
    "with a single target needs no selection; one with several does. The target is the "
    "design's own say for that build: for each key it writes, its value wins over the design's "
    "and the project's (defaults < project < design < target < -s < API); `-s` and the API "
    "still win over it.",
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
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE items with spaces; the list ends at the next option or the first token that is not KEY=VALUE (use -- before a design path that looks like one). KEY can be a hierarchical name using dot notation.
    Example: --settings clock.period=2.345 impl.strategy=Debug
    flows.<flow>.KEY=VALUE sets a setting of another flow of the run, e.g. -s flows.yosys_fpga.flatten=true.
    """,
    #  examples: # FIXME move to docs
    # - xeda vivado_sim --flow-settings stop_time=100us
    # - xeda vivado_synth --flow-settings impl.strategy=Debug --flow-settings clock.period=2.345
)
@click.option(
    "--detailed-logs/--no-detailed-logs",
    envvar="XEDA_DETAILED_LOGS",
    show_envvar=True,
    default=False,
)
@click.option("--log-level", envvar="XEDA_LOG_LEVEL", show_envvar=True, type=int, default=None)
@click.option(
    "--post-cleanup",
    is_flag=True,
    help="After the run, keep only settings.json, results.json and the artifacts in each run "
    "directory.",
)
@click.option(
    "--post-cleanup-purge",
    is_flag=True,
    help="After the run, remove each run directory.",
)
@click.option(
    "--scrub",
    is_flag=True,
    help="Before running, remove the flow's other run directories for this design; asks for "
    "confirmation.",
)
@click.option(
    "--remote",
    type=str,
    envvar="XEDA_REMOTE",
    show_envvar=True,
    help="Run on a remote machine with SSH access. Xeda needs to be installed on the remote machine and PATH env variable should be set correctly.",
)
@click.option(
    "--debug",
    "-d",
    is_flag=True,
    default=False,
    envvar="XEDA_DEBUG",
    show_envvar=True,
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
    "--outputs-to",
    "outputs_to",
    type=click.Path(file_okay=False, dir_okay=True, resolve_path=True, path_type=Path),
    default=None,
    help="Copy the requested flow's outputs (its artifacts, each at its path in the run "
    "directory) into DIR once it succeeded. An existing file there is replaced only if it is "
    "xeda's own earlier copy, unchanged (see --overwrite-outputs).",
)
@click.option(
    "--overwrite-outputs",
    is_flag=True,
    default=False,
    help="Replace existing files at the output paths named for this run (by settings or "
    "--outputs-to), even ones xeda did not write or that changed since it did. Never a design "
    "source or input, never a directory, never anything else.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved plan: each flow in execution order, its run directory, input "
    "origins and outputs switched on for consumers. Run nothing. Designs needing a generator "
    "or Git dependency fetch are refused.",
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
    rebuild_all: bool = False,
    hashed_run_dirs: bool = False,
    flow_settings: Union[None, str, Iterable[str]] = None,
    clean: bool = False,
    run_root: Optional[Path] = None,
    xedaproject: Optional[str] = None,
    # design: Optional[str] = None,
    design_name: Optional[str] = None,
    target: str | None = None,
    design_overrides: Iterable[str] = tuple(),
    design_allow_extra: bool = False,
    log_level: Optional[int] = None,
    detailed_logs: bool = False,
    post_cleanup: bool = False,
    post_cleanup_purge: bool = False,
    scrub: bool = False,
    remote: Optional[str] = None,
    debug: bool = False,
    help_settings: bool = False,
    outputs_to: Optional[Path] = None,
    overwrite_outputs: bool = False,
    dry_run: bool = False,
    json_flag: bool = False,
):
    """`run` command"""
    assert ctx
    options: XedaOptions = ctx.obj or XedaOptions()
    # FLOW is the canonical request text: results, settings and exit status are its last flow's
    request = parse_request(flow)
    chain, flow = flow, request.requested.name
    if json_flag:
        # stdout belongs to the JSON document from here on
        machine_readable_mode()
    if help_settings:
        print_flow_settings(flow, options=options, output_format="json" if json_flag else "table")
        sys.exit(0)
    debug |= options.debug
    if run_root is None:
        run_root = Path.cwd() / "xeda_run"

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
                    "target": target,
                    "success": False,
                    "results": {},
                    "error": {"type": "DesignNotSpecified", "message": message},
                    "nodes": [],
                },
                "json",
            )
        sys.exit(1)

    if dry_run and remote:
        raise click.UsageError("`--dry-run` is not supported with --remote", ctx=ctx)
    if remote and len(request.elements) > 1:
        raise click.UsageError(LOCAL_REQUESTS_ONLY, ctx=ctx)

    if remote:
        from .flow_runner import remote as remote_runner

        # The remote flow always runs fresh in hashed directories. `--rebuild-all` also forces
        # local generators to run before their design is shipped.
        for name, given in (
            ("--clean", clean),
            ("--hashed-run-dirs", hashed_run_dirs),
        ):
            if given:
                raise click.UsageError(
                    f"`{name}` is not supported with --remote: remote runs always run fresh, "
                    "mirrored in hashed run directories",
                    ctx=ctx,
                )
        rl = remote_runner.RemoteRunner(
            run_root,
            outputs_to=outputs_to,
            overwrite_outputs=overwrite_outputs,
            rebuild_all=rebuild_all,
        )
        if _interactive(json_flag):
            rl.confirm_overwrite = _prompt_overwrite
        assert design
        try:
            remote_results = rl.run_remote(
                design,
                flow,
                host=remote,
                flow_settings=flow_settings,
                xedaproject=xedaproject,
                design_overrides=design_overrides,
                design_allow_extra=design_allow_extra,
                target=target,
            )
        except XedaException as e:
            log.critical("%s", _error_message(e))
            if json_flag:
                emit_structured(
                    _remote_document(
                        flow, design, remote, None, False, error=e, target=rl.target or target
                    ),
                    "json",
                )
            if debug:
                raise e
            sys.exit(1)
        except Exception as e:
            if not json_flag:
                raise
            log.critical("%s", _error_message(e))
            emit_structured(
                _remote_document(
                    flow, design, remote, None, False, error=e, target=rl.target or target
                ),
                "json",
            )
            if debug:
                raise
            sys.exit(1)
        # The remote flow's own success decides ours; a failed flow is not a successful run.
        success = bool(remote_results and remote_results.get("success"))
        if not success:
            log.critical("Remote run of flow '%s' on '%s' failed.", flow, remote)
        if json_flag:
            emit_structured(
                _remote_document(flow, design, remote, remote_results, success, target=rl.target),
                "json",
            )
        sys.exit(0 if success else 1)

    launcher: Optional[DefaultRunner] = None

    def emit_failure(error_type: str, message: str, exc: Exception) -> None:
        """Report a failed run identically whether the caller wants text or JSON: with every
        node the launcher had launched when the run failed."""
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": str(design),
                    "target": (launcher.target if launcher is not None else None) or target,
                    "success": False,
                    "results": {},
                    "error": {"type": error_type, "message": message},
                    "request": request_info(request),
                    "nodes": (
                        _node_states(launcher.launched, launcher.last_plan)
                        if launcher is not None
                        else []
                    ),
                },
                "json",
            )
        if debug:
            raise exc
        sys.exit(1)

    if dry_run:
        try:
            launcher = DefaultRunner(
                run_root,
                rebuild_all=rebuild_all,
                hashed_run_dirs=hashed_run_dirs,
                clean=clean,
                debug=debug,
            )
            plan = launcher.plan(
                chain,
                xedaproject=xedaproject,
                design=design,
                flow_settings=flow_settings,
                select_design_in_project=select_design_in_project,
                design_overrides=design_overrides,
                design_allow_extra=design_allow_extra,
                target=target,
            )
        except XedaException as e:
            emit_failure(type(e).__name__, _error_message(e), e)
            raise
        except Exception as e:
            if not json_flag:
                raise
            emit_failure(type(e).__name__, _error_message(e), e)
            raise
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": str(design),
                    "target": plan.context.target,
                    "success": True,
                    "dry_run": True,
                    "request": request_info(request),
                    "plan": plan_info(plan),
                },
                "json",
            )
        else:
            _print_plan(plan)
        sys.exit(0)

    try:
        launcher = DefaultRunner(
            run_root,
            rebuild_all=rebuild_all,
            hashed_run_dirs=hashed_run_dirs,
            clean=clean,
            outputs_to=outputs_to,
            overwrite_outputs=overwrite_outputs,
        )
        if _interactive(json_flag):
            launcher.confirm_overwrite = _prompt_overwrite
        launcher.settings.post_cleanup = post_cleanup
        launcher.settings.post_cleanup_purge = post_cleanup_purge
        launcher.settings.scrub_old_runs = scrub
        launcher.settings.debug = debug
        f = launcher.run(
            chain,
            xedaproject=xedaproject,
            design=design or design_name,
            flow_settings=flow_settings,
            select_design_in_project=select_design_in_project,
            design_overrides=design_overrides,
            design_allow_extra=design_allow_extra,
            target=target,
        )
        success = bool(f and f.results.success)
        if json_flag:
            document = _run_document(
                flow,
                design,
                f,
                success,
                launcher.launched,
                launcher.last_plan,
                launcher.target,
            )
            document["request"] = request_info(request)
            emit_structured(document, "json")
        sys.exit(0 if success else 1)
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
    except XedaException as e:
        # a user error -- a design or project file that does not load, an unknown flow or
        # design, invalid settings, a failed dependency -- reported by its own class name
        emit_failure(type(e).__name__, _error_message(e), e)
    except Exception as e:
        if not json_flag:
            raise
        emit_failure(type(e).__name__, _error_message(e), e)


def _remote_document(
    flow_name: str,
    design: Any,
    host: str,
    results: Optional[Dict[str, Any]],
    success: bool,
    error: Optional[BaseException] = None,
    target: str | None = None,
) -> Dict[str, Any]:
    """The machine-readable summary emitted by `xeda run --remote --json`."""
    document: Dict[str, Any] = {
        "flow": flow_name,
        "design": str(design),
        "target": target,
        "remote": host,
        "success": success,
        "results": json_safe(results or {}),
    }
    run_path = (results or {}).get("run_path")
    if run_path:
        document["run_path"] = str(run_path)
        document["results_json"] = str(Path(run_path) / "results.json")
    if error is not None:
        document["error"] = {"type": type(error).__name__, "message": _error_message(error)}
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
    return json_safe(best.as_json_value())


@cli.command(
    cls=DeclaredEnvvarsCommand,
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
    help="""Override setting values for the executed flow. Separate multiple KEY=VALUE items with spaces; the list ends at the next option or the first token that is not KEY=VALUE (use -- before a design path that looks like one). KEY can be a hierarchical name using dot notation.
    Example: --settings clock.period=2.345 impl.strategy=Debug
    """,
)
@_run_root_options
@click.option(
    "--xedaproject",
    envvar="XEDA_XEDAPROJECT",
    show_envvar=True,
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
    help="Path to Xeda project file. By default, discover one xedaproject.yaml, .yml or .toml.",
)
@click.option(
    "--design-name",
    # cls=ClickMutex,
    # mutually_exclusive_with=["design_file"],
    help="Specify design.name in case multiple designs are available in a xedaproject.",
)
@click.option(
    "--target",
    metavar="NAME",
    help="Select one of the design's `targets` (`targets.NAME` in the design file). A design "
    "with a single target needs no selection; one with several does. The target is the "
    "design's own say for that build: for each key it writes, its value wins over the design's "
    "and the project's (defaults < project < design < target < -s < API); `-s` and the API "
    "still win over it.",
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
@click.option(
    "--detailed-logs/--no-detailed-logs",
    envvar="XEDA_DETAILED_LOGS",
    show_envvar=True,
    default=True,
)
@click.option("--log-level", envvar="XEDA_LOG_LEVEL", show_envvar=True, type=int, default=None)
@click.option(
    "--optimizer",
    envvar="XEDA_OPTIMIZER",
    show_envvar=True,
    type=str,
    default="fmax_optimizer",
    show_default=True,
)
@click.option(
    "--dse-settings",
    envvar="XEDA_DSE_SETTINGS",
    show_envvar=True,
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
)
@click.option(
    "--optimizer-settings",
    envvar="XEDA_OPTIMIZER_SETTINGS",
    show_envvar=True,
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
)
@click.option(
    "--max-workers",
    envvar="XEDA_MAX_WORKERS",
    show_envvar=True,
    type=int,
    default=None,
    help="Maximum number of concurrent flow executions.",
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
    envvar="XEDA_DEBUG",
    show_envvar=True,
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
    run_root: Path,
    xedaproject: Optional[str] = None,
    design: Optional[str] = None,
    design_name: Optional[str] = None,
    target: str | None = None,
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

    dse: Dse | None = None

    def dse_failure(error_type: str, message: str, exc: Optional[BaseException] = None) -> None:
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    "design": str(design or design_name),
                    "target": (dse.target if dse is not None else None) or target,
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

    if log_level is None:
        log_level = (
            logging.WARNING if options.quiet else logging.DEBUG if options.debug else logging.INFO
        )
    if options.debug:
        detailed_logs = True
    # No log file here: the exploration logs into its run root (`<root>/Logs`), which it makes
    # when it starts, once its settings have validated -- one log per exploration.
    setup_logger(log_level, detailed_logs)

    opt_settings = settings_to_dict(optimizer_settings, hierarchical_keys=True)
    dse_settings_dict = settings_to_dict(dse_settings, hierarchical_keys=True)
    if max_workers:
        dse_settings_dict["max_workers"] = max_workers  # overrides

    # will deprecate options and only use optimizer_settings
    opt_settings = {
        # Only what was given: an omitted option is no value, and passing `None` for one made
        # the optimizer report `input_value=None` instead of the setting it lacks.
        **{
            name: value
            for name, value in dict(
                init_freq_low=init_freq_low, init_freq_high=init_freq_high
            ).items()
            if value is not None
        },
        **opt_settings,  # optimizer_settings overrides other options
    }

    try:
        dse = Dse(
            optimizer_class=optimizer,
            optimizer_settings=opt_settings,
            run_root=run_root,
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
            target=target,
        )
    except SystemExit:
        raise
    except BaseException as e:
        dse_failure(type(e).__name__, _error_message(e), e)
        raise  # unreachable: dse_failure exits
    if json_flag:
        document = {
            "flow": flow,
            "design": str(design or design_name),
            "target": dse.target,
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
    cls=DeclaredEnvvarsCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Remove a flow's run directories for a design.",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    # canonical already; a removed flow's name too, whose run directories are still there
    type=FlowChoice(all_flow_names, removed=True),
    required=True,
)
@click.argument(
    "design_name",
    metavar="DESIGN_NAME",
    required=True,
)
@_run_root_options
@click.option(
    "--target",
    metavar="NAME",
    help="Remove only this target's run directories (<run-root>/<design_name>/NAME/), leaving "
    "the pre-target ones (<design_name>/<flow>) and every other target's. Without it, FLOW_NAME's "
    "directories under every target are removed too, as found on disk: no design file is read.",
)
@click.option(
    "--incremental/--no-incremental",
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "the default: scrub always looks in <design>/ for <flow> and <flow>_<hash> directories"
    ),
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
def scrub(ctx: click.Context, flow, design_name, target, run_root, json_flag):
    """Remove FLOW_NAME's previous run directories for DESIGN_NAME, under <run-root>/<design_name>:
    the pre-target ones and those of every target (<design_name>/<target>/), or with --target
    only that target's. They are listed, and confirmed once."""
    if json_flag:
        machine_readable_mode()

    def fail(error_type: str, message: str) -> NoReturn:
        if json_flag:
            emit_structured(
                {
                    "success": False,
                    "flow": flow,
                    "design": design_name,
                    "target": target,
                    "error": {"type": error_type, "message": message},
                },
                "json",
            )
        else:
            log.critical("%s", message)
        sys.exit(1)

    if not DESIGN_NAME.fullmatch(design_name):
        fail("RunDirectoryError", f"{design_name!r} is not a design name")
    if target is not None and (problem := target_name_problem(target)) is not None:
        fail("RunDirectoryError", problem)
    try:
        root = ensure_run_root(run_root, create=False)
    except RunRootError as e:
        fail("RunRootError", str(e))
    run_root = root if root is not None else Path(run_root).resolve()

    try:
        scanned, removed = scrub_design(
            flow, run_root / design_name, run_root=run_root, target=target
        )
    except RunDirectoryError as e:  # a run directory xeda cannot remove
        fail(type(e).__name__, _error_message(e))
    if json_flag:
        emit_structured(
            {
                "success": True,
                "flow": flow,
                "design": design_name,
                "target": target,
                "run_root": str(run_root),
                "scanned": [str(d) for d in scanned],
                "scrubbed": [str(d) for d in removed],
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
def completion(_ctx: click.Context, stdout: bool, shell: Optional[str] = None):
    """Xeda shell auto-completion"""
    os_default_shell_name = None
    os_default_shell = os.environ.get("SHELL")
    if os_default_shell:
        os_default_shell_name = os_default_shell.split(os.sep)[-1]
    if not shell:
        if not os_default_shell_name:
            raise click.UsageError("Specify a shell (bash, zsh, fish), or set SHELL.", ctx=_ctx)
        shell = click.Choice(list(SHELLS), case_sensitive=False).convert(
            os_default_shell_name, None, _ctx
        )
    elif os_default_shell_name and os_default_shell_name != shell and not stdout:
        console.print(
            f"[yellow]WARNING:[/] Current default shell ([bold]{os_default_shell}[/]) is different from the specified shell [bold]{shell}[/b]"
        )
    if stdout:
        completion_class = get_completion_class(shell)
        if completion_class is None:
            raise click.ClickException(f"No completion support registered for {shell}.")
        complete = completion_class(
            cli=cli, ctx_args={}, prog_name="xeda", complete_var="_XEDA_COMPLETE"
        )
        click.echo(complete.source(), nl=False)
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
            soft_wrap=True,
        )
