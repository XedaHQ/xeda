"""The Xeda command-line interface."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, NoReturn, Optional, Sequence, Tuple, Union

import click
from click.core import ParameterSource

from .cli_types import (
    ChainChoice,
    DOCUMENT_FORMATS,
    FlowChoice,
    OptionEatAll,
    TIER_NAMES,
    output_format_option,
)
from .flow_catalog import flow_names
from .pdk_cli import add_pdk_commands
from .remote.commands import add_remote_commands

# Click resolves completion before invoking command callbacks. Share the declarations below,
# but keep execution and help-rendering dependencies out of a completion process.
if TYPE_CHECKING or not os.environ.get("_XEDA_COMPLETE"):
    import coloredlogs
    from click_extra import HelpKeywords
    from rich.markup import escape

    from .agent_skill import SKILL_NAME, default_skill_dir, generate_flows_reference, install_skill
    from .cli_utils import (
        HELP_FORMATTER_SETTINGS,
        DeclaredEnvvarsCommand,
        XedaHelpGroup as _XedaHelpGroup,
        emit_structured,
        info_table,
        machine_readable_mode,
        print_flow_settings,
        resolve_format,
        select_design_in_project,
    )
    from .console import console
    from .deliver import Conflict
    from .design import DESIGN_NAME, target_name_problem
    from .flow_runner import (
        DefaultRunner,
        XedaOptions,
        add_file_logger,
        scrub_design,
    )
    from .flow_runner.chains import parse_request
    from .flow_runner.dse import Dse
    from .flow_runner.error_codes import public_error
    from .flow_runner.launch_result import launch_result
    from .flow_runner.resolver import Plan
    from .flow_runner.settings_layers import command_line_table
    from .introspect import (
        boards_info,
        design_info,
        design_schema,
        flow_chain_cells,
        flows_info,
        json_safe,
        optimizers_info,
        plan_info,
        request_info,
        platforms_info,
        results_info,
    )
    from .errors import SpecError
    from .records import ErrorInfo
    from .run_dir import RunDirectoryError
    from .run_root import RunRootError, ensure_run_root
    from .outcomes import run_document, spec_document, spec_document_of
    from .spec import Spec
    from .utils import XedaException, removeprefix, settings_to_dict

    from .cli_completion import dispatch_shell_completion

    class XedaHelpGroup(_XedaHelpGroup):
        """Help group with Xeda's local, lightweight completion dispatch."""

        def _main_shell_completion(
            self,
            ctx_args: MutableMapping[str, Any],
            prog_name: str,
            complete_var: str | None = None,
        ) -> None:
            dispatch_shell_completion(self, ctx_args, prog_name, complete_var)

    CONTEXT_SETTINGS: Dict[str, Any] = dict(
        auto_envvar_prefix="XEDA",
        help_option_names=["--help", "-h"],
        max_content_width=console.width,
        formatter_settings=HELP_FORMATTER_SETTINGS,
    )
    RUN_STYLE: Dict[str, Any] = {"excluded_keywords": HelpKeywords(cli_names={"run"})}
else:
    from .cli_completion import CompletionGroup as XedaHelpGroup
    from .cli_completion import CompletionCommand as DeclaredEnvvarsCommand

    CONTEXT_SETTINGS = dict(
        auto_envvar_prefix="XEDA",
        help_option_names=["--help", "-h"],
        max_content_width=120,
    )
    RUN_STYLE = {}

log = logging.getLogger(__name__)


class ShortLoggerNames(logging.Formatter):
    """A formatter that shows a log record with its logger name shortened (`xeda.flow` as `flow`).
    It formats a copy of the record. The record itself, which every other handler of the process
    also receives, keeps the name of its logger."""

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.formatter = formatter

    def format(self, record: logging.LogRecord) -> str:
        shown = logging.makeLogRecord(record.__dict__)
        shown.name = removeprefix(record.name, "xeda.")
        return self.formatter.format(shown)


def setup_logger(log_level, detailed_logs, log_to_file: Optional[Path] = None):
    root = logging.getLogger()
    root.setLevel(log_level)
    before = list(root.handlers)
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
        # Wrap only the handler installed here. The handler of another party shows names as it
        # likes.
        for handler in root.handlers:
            if handler not in before and handler.formatter is not None:
                handler.setFormatter(ShortLoggerNames(handler.formatter))
    if log_to_file:
        add_file_logger(log_to_file)


# click-extra uses the command name as prog_name, including for Click's completion protocol.
# Keep it aligned with the console entry point and its _XEDA_COMPLETE environment variable.
@click.group("xeda", cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose mode.")
@click.option("--quiet", "-q", is_flag=True, help="Enable quiet mode.")
@click.option("--debug", "-d", show_envvar=True, is_flag=True)
@click.version_option(message="Xeda v%(version)s")
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    ctx.obj = XedaOptions(**kwargs)


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List available flows.")
@output_format_option()
def list_flows(output_format: str, json_flag: bool):
    """List every flow Xeda can run, with its aliases, category, default producers and declared
    inputs and outputs.

    The "Depends on" column lists the default producers of a flow. "Takes" and "Makes" list the
    declared inputs and outputs as `name (Types)`: [name] is optional, and name... is a list.
    "Can be followed by" names the flows that can come right after this flow in a chain
    (`xeda run a+b`). `(depends on the target)` marks a relation that the target decides, and
    `flow.output` marks a relation that needs that output named. A flow that declares no inputs
    or outputs runs alone.
    """
    fmt = resolve_format(output_format, json_flag)
    info = flows_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = info_table(
        "Available flows",
        [
            # A name is never folded, so it can be typed back into a command.
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


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List the settings of a flow.")
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(flow_names),
    required=True,
)
@output_format_option()
@click.option(
    "--common/--no-common",
    default=True,
    show_default=True,
    help="Include the settings accepted by every flow (ncpus, dockerized, lib_paths, ...).",
)
@click.option(
    "--tier",
    type=click.Choice(TIER_NAMES),
    default=None,
    help=(
        "Show the settings up to this tier: `advanced` shows `common` and `advanced`. "
        "The default is every tier."
    ),
)
@click.pass_context
def list_settings(
    ctx: click.Context, flow, output_format: str, json_flag: bool, common: bool, tier: str | None
):
    """Show every setting of FLOW_NAME that can be given as `-s KEY=VALUE` or in a design file.

    A setting of a group has a dotted name (`global_placement.timing_driven.enabled`)."""
    print_flow_settings(
        flow,
        options=ctx.obj,
        output_format=resolve_format(output_format, json_flag),
        include_common=common,
        tier=tier,
    )


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="List the metrics a flow reports.",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(flow_names),
    required=True,
)
@output_format_option()
def list_results(flow, output_format: str, json_flag: bool):
    """Show the metrics that FLOW_NAME writes to `results.json` in its run directory.

    Each metric has a path, a type and a unit. A metric in the units of the platform has no unit."""
    fmt = resolve_format(output_format, json_flag)
    info = results_info(flow)
    if fmt != "table":
        emit_structured(info, fmt, records=[{"flow": info["flow"], **m} for m in info["metrics"]])
        return
    table = info_table(
        f"{info['flow']} results",
        [
            ("Path", {"header_style": "bold green", "style": "bold"}),
            ("Type", {}),
            ("Unit", {}),
            ("Description", {}),
        ],
    )
    for metric in info["metrics"]:
        table.add_row(
            escape(metric["path"]),
            escape(metric["type"]),
            escape(metric["unit"]) if metric["unit"] else "[dim]-[/dim]",
            escape(metric["description"]),
        )
    console.print(table)
    console.print(f"[dim]{info['note']}[/dim]")


@cli.command(
    "design-schema",
    context_settings=CONTEXT_SETTINGS,
    short_help="Print the JSON Schema of a design file.",
)
@output_format_option(formats=DOCUMENT_FORMATS, default="json")
def design_schema_cmd(output_format: str, json_flag: bool):
    """Print the JSON Schema of a Xeda design file (YAML, TOML or JSON).

    Use it to check the structure of a design file or to write one. The schema does not catch
    every invalid file: it accepts unknown keys, which the design loader refuses.
    """
    emit_structured(design_schema(), resolve_format(output_format, json_flag))


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List bundled FPGA boards.")
@output_format_option()
def list_boards(output_format: str, json_flag: bool):
    """List the FPGA boards that Xeda ships. A board name is a value of the `board` setting of an
    FPGA flow."""
    fmt = resolve_format(output_format, json_flag)
    info = boards_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = info_table(
        "Bundled FPGA boards",
        [
            ("Board", {"header_style": "bold green", "style": "bold"}),
            ("openFPGALoader board", {}),
            ("FPGA part", {}),
        ],
    )
    for board in info:
        fpga = board.get("fpga") or {}
        part = fpga.get("part") if isinstance(fpga, dict) else str(fpga)
        table.add_row(
            escape(board["board"]),
            escape(str(board.get("openfpgaloader_board", "-"))),
            escape(str(part or "-")),
        )
    console.print(table)


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List bundled ASIC platforms (PDKs).")
@output_format_option()
def list_platforms(output_format: str, json_flag: bool):
    """List the ASIC platforms that Xeda ships. The ASIC flows (`yosys` today) take a platform
    through the setting `platform`, or through `platform_file` for a platform file of your own.
    Each platform shows whether its PDK files are installed in the default store (`~/.ciel`). The
    command only looks at the files. `xeda pdk install` installs a platform."""
    fmt = resolve_format(output_format, json_flag)
    info = platforms_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    table = info_table(
        "Bundled ASIC platforms",
        [
            ("Platform", {"header_style": "bold green", "style": "bold"}),
            ("Description", {}),
            ("Provider", {}),
            ("Version", {}),
            ("Installed", {}),
            ("Directory", {}),
        ],
    )
    for platform in info:
        version = str(platform["version"])
        table.add_row(
            escape(platform["platform"]),
            escape(str(platform.get("description") or "-")),
            escape(platform["provider"]),
            escape(version[:12] if len(version) == 40 else version),
            "yes" if platform["installed"] else "no",
            escape(str(platform.get("directory", "-"))),
        )
    console.print(table)
    for platform in info:
        if "problem" in platform:
            console.print(escape(platform["problem"]), highlight=False)
    missing = [p["platform"] for p in info if not p["installed"]]
    if missing:
        console.print(
            f"Install a platform with [bold]xeda pdk install <platform>[/]: {escape(', '.join(missing))}."
        )


@cli.group(
    context_settings=CONTEXT_SETTINGS,
    cls=XedaHelpGroup,
    short_help="Find the PDK files of the ASIC platforms.",
)
def pdk():
    """The PDK files of the ASIC platforms.

    A platform needs the files of its PDK: Liberty, LEF and more. They live in a store, by
    default `~/.ciel`. `list` and `path` show which platforms the store holds and where the files
    are. They start no process, use no network and write nothing. `install` downloads the files
    of a platform into the store.
    """


add_pdk_commands(pdk, DeclaredEnvvarsCommand, CONTEXT_SETTINGS)


@cli.group(
    "remote",
    context_settings=CONTEXT_SETTINGS,
    cls=XedaHelpGroup,
    short_help="Check, clean and follow launches on remote hosts.",
)
def remote_group():
    """The hosts that `xeda run --remote HOST` uses.

    `check` connects to a host, makes the environment of xeda there if it is missing, and reports
    what a remote run finds. `clean` removes the environments and workspaces that nobody uses.
    `attach` follows a launch that `xeda run --remote HOST --keep-running` started, and fetches its
    result. `cancel` stops such a launch.
    """


add_remote_commands(remote_group, DeclaredEnvvarsCommand, CONTEXT_SETTINGS)


@cli.command(
    context_settings=CONTEXT_SETTINGS, short_help="List design-space exploration optimizers."
)
@output_format_option()
def list_optimizers(output_format: str, json_flag: bool):
    """List the optimizers that `xeda dse --optimizer <name>` accepts, and their settings."""
    fmt = resolve_format(output_format, json_flag)
    info = optimizers_info()
    if fmt != "table":
        emit_structured(info, fmt)
        return
    for optimizer in info:
        table = info_table(
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
    """Return the one message that reports `exc`, in text and in `--json` output: its text,
    led by its class name. Some exceptions already start their text with their class name
    (`DesignValidationError`, `FlowSettingsError`). Those keep their text, so a message never
    reads "DesignValidationError: DesignValidationError: ...". An exception with no text reports
    its class name."""
    kind = type(exc).__name__
    text = str(exc)
    if not text:
        return kind
    if any(text.startswith(f"{name}:") for name in (kind, type(exc).__qualname__)):
        return text
    return f"{kind}: {text}"


def _removed_option(replacement: str, false_replacement: Optional[str] = None):
    """Return the callback of a hidden option that only exists to say what replaced it.

    `replacement` is the text for the option as given (`--foo`). For a pair whose negation
    (`--no-foo`) needs other text, `false_replacement` gives it. The message has the form of the
    messages of the launcher settings and the flow settings (`<name>` was removed: use
    <replacement>), so a removed option, launcher setting and flow setting all read the same
    way.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
        if value is not None:
            given = param.opts[0] if value else param.secondary_opts[0]
            text = replacement if value or false_replacement is None else false_replacement
            raise click.UsageError(f"`{given}` was removed: use {text}", ctx=ctx)

    return callback


def _removed_run_root_option(ctx: click.Context, param: click.Parameter, value: Any) -> None:
    """Refuse `--xeda-run-dir` and `XEDA_RUN_DIR`, the former names of the run root, and name the
    one that was given. The check is explicit because click would suggest `--hashed-run-dirs`
    for the option and would ignore the variable without a message."""
    if value is not None:
        if ctx.get_parameter_source(param.name or "") is ParameterSource.ENVIRONMENT:
            raise click.UsageError("`XEDA_RUN_DIR` was removed: use XEDA_RUN_ROOT", ctx=ctx)
        raise click.UsageError("`--xeda-run-dir` was removed: use --run-root", ctx=ctx)


def _run_root_options(func):
    """Add `--run-root` (`XEDA_RUN_ROOT`) and the hidden option that refuses the removed names of
    the run root."""
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

FLOW names one flow (ghdl_sim, GhdlSim and ghdl-sim name the same flow). The producers of the
flow run first. A chain such as `yosys_fpga+nextpnr+fpga_pack` runs its last flow, binding each
preceding flow's declared output to the compatible required inputs of the next flow.
`FLOW.OUTPUT` names the output when several fit. Unbound inputs come from the sources of the
design or from their default producers. Write the + inside one argument. A link may pass through the
flows between its two flows: `yosys_fpga+fpga_pack` runs `nextpnr` in between.

-s KEY=VALUE sets a setting of the last flow. -s flows.NAME.KEY=VALUE sets a setting of another
flow of the run. To save a binding, write it under flows.NAME.inputs in a YAML design file or
project file. --dry-run shows the plan and runs no tools. With --remote HOST, the whole request
runs on a Linux host over SSH; Xeda installs what it needs there on the first run.

Example: xeda run yosys_fpga+nextpnr+fpga_pack design.yaml
"""


def _interactive(json_flag: bool) -> bool:
    """Return whether a question may be asked: stdin and stderr are terminals, and the output is
    not machine-readable."""
    return not json_flag and sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_overwrite(conflicts: Sequence[Conflict]) -> bool:
    """Ask on the terminal whether to replace the files that are in the way of the named outputs.
    The default answer is no, and the answer is also no when the question cannot be answered."""
    click.echo(
        f"Xeda would replace {len(conflicts)} file(s) at the output paths you named:", err=True
    )
    for conflict in conflicts:
        click.echo(f"  {conflict.destination}   {conflict.why}", err=True)
    try:
        return click.confirm("Replace them?", default=False, err=True)
    except click.Abort:
        return False


def _prompt_scrub(directories: Sequence[Path]) -> bool:
    """Ask on the terminal (or on the input that stands for it) whether to remove the run
    directories that scrub listed. Only the word "yes" says yes. The answer is also no when there
    is no input to read: the input is at its end, closed or unreadable. The commands ask even when
    stdin is no terminal, so a script can pipe "yes" to them. The question goes to stderr with
    `--json`."""
    try:
        answer = console.input("Type 'yes' if you're sure you want to continue: ")
    except (EOFError, ValueError, OSError):
        console.print()
        return False
    return answer.lower() == "yes"


def _print_plan(plan: Plan, freshness: Mapping[str, Any] | None = None) -> None:
    """Print each planned flow, its directory, what a launch would do with it (`fresh`, or a state
    and its reason: `stale: <reason>`, `unknown: <reason>`, `rejected again: <message>` or
    `blocked: <reason>`), its input origins and switched-on outputs."""
    target = f", target {plan.context.target}" if plan.context.target else ""
    click.echo(f"Plan for {plan.requested}{target} (a dry run: nothing runs)")
    for node in plan.nodes:
        click.echo(f"  {node.name}  {node.run_path}")
        if freshness is not None:
            said = freshness[node.name]
            click.echo(f"      {said.state}" + (f": {said.reason}" if said.reason else ""))
        # Static class metadata, never the dynamic `always_runs()` of the flow.
        if node.flow_class.action_reason:
            click.echo(f"      always runs: {node.flow_class.action_reason}")
        for resolved in node.inputs:
            click.echo(f"      {resolved.describe()}")
        for output in node.switched_on:
            click.echo(f"      output {output} switched on: a consumer reads it")


@cli.command(
    # Naming the class is also what lets the `command()` overloads of cloup accept the
    # `excluded_keywords` keyword below.
    cls=DeclaredEnvvarsCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a flow.",
    help=RUN_HELP,
    no_args_is_help=False,
    # click-extra highlights command names wherever they appear in help prose. This command is
    # named after an ordinary English verb, so each use of the word "run" in the help would be
    # painted as if it named the command.
    **RUN_STYLE,
)
@click.argument(
    "flow",
    metavar="FLOW[.OUTPUT][+FLOW[.OUTPUT]...]",
    type=ChainChoice(flow_names),
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
    help="Run every flow, producers included, even a flow that is up to date. Without it, a "
    "flow runs only when its sources, settings, tools or outputs changed since its last "
    "successful run.",
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
    # and two parameters that share a name overwrite each other during parsing (click 8.5
    # warns about it).
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
    help="Path to the Xeda design file of a single design.",
)
@click.option(
    "--design-name",
    help="Specify design.name in case multiple designs are available in a project file.",
)
@click.option(
    "--target",
    metavar="NAME",
    help="Select one of the design's `targets` (`targets.NAME` in the design file). Without "
    "this option, Xeda uses the design itself, with no target. For each key a target sets, its "
    "value wins over the design's and the project's. The order, lowest first, is defaults < "
    "project < design < target < -s < API.",
)
@click.option(
    "--design-overrides",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Override attributes of the design.""",
)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    type=bool,
    is_flag=True,
    default=False,
    help="""Allow extra properties (fields) in the design file.""",
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
    Example: --settings clock.period=2.345ns impl.strategy=Debug
    flows.<flow>.KEY=VALUE sets a setting of another flow of the run, e.g. -s flows.yosys_fpga.flatten=true.
    """,
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
    help="After the run, keep only these files in each run directory: settings.json, "
    "results.json, the artifacts listed in results.json and the declared output files, with "
    "their parent directories. Xeda does not reuse a run that was cleaned up.",
)
@click.option(
    "--post-cleanup-purge",
    is_flag=True,
    help="After the run, remove each run directory; this implies --post-cleanup. An output "
    "delivered to a location you named is moved there instead of copied, when it can be.",
)
@click.option(
    "--scrub",
    is_flag=True,
    help="Before running, remove the flow's other run directories for this design; asks for "
    "confirmation. It cannot be used with --remote.",
)
@click.option(
    "--remote",
    type=str,
    envvar="XEDA_REMOTE",
    show_envvar=True,
    help="Run the request on a Linux host over SSH. HOST is an SSH destination; xeda installs "
    "what it needs there on the first run.",
    metavar="HOST",
)
@click.option(
    "--fetch-all",
    is_flag=True,
    default=False,
    help="With --remote, fetch every file of the run directories, not only the results, logs, "
    "reports and final outputs.",
)
@click.option(
    "--keep-running",
    is_flag=True,
    default=False,
    help="With --remote, keep the launch running on the host when the connection ends or Ctrl-C "
    "stops the local follow. `xeda remote attach` follows it again and `xeda remote cancel` "
    "stops it. Running the same request again attaches to the running launch.",
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
    help="Copy the outputs of the requested flow (its artifacts, each at its path in the run "
    "directory) into DIR once the flow succeeds. An existing file there is replaced only if it "
    "is an earlier copy that Xeda made and that is unchanged (see --overwrite-outputs). For a "
    "flow that writes no outputs (a programmer), Xeda refuses this before anything runs and "
    "names what delivers the file the flow reads.",
)
@click.option(
    "--overwrite-outputs",
    is_flag=True,
    default=False,
    help="Replace existing files at the output paths named for this run (by settings or "
    "--outputs-to), even files that Xeda did not write or that changed since Xeda wrote them. "
    "Xeda never replaces a design source, another input or a directory.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved plan: each flow in execution order, its run directory, what a "
    "launch would do with it, its input origins and the outputs switched on for consumers. "
    "Run nothing. Refuse a design that needs a generator run or a Git dependency fetch.",
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
    fetch_all: bool = False,
    keep_running: bool = False,
    debug: bool = False,
    help_settings: bool = False,
    outputs_to: Optional[Path] = None,
    overwrite_outputs: bool = False,
    dry_run: bool = False,
    json_flag: bool = False,
):
    """Run a flow or a chain."""
    assert ctx
    options: XedaOptions = ctx.obj or XedaOptions()
    # FLOW is the canonical request text. The results, the settings and the exit status are
    # those of its last flow.
    request = parse_request(flow)
    chain, flow = flow, request.requested.name
    if json_flag:
        # From here on, stdout belongs to the JSON document.
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
        raise click.UsageError(
            "No design specified. Pass a design file as the DESIGN argument, or name one with "
            "--design-file / --design-name.",
            ctx=ctx,
        )

    if fetch_all and not remote:
        raise click.UsageError("`--fetch-all` needs --remote", ctx=ctx)
    if keep_running and not remote:
        raise click.UsageError("`--keep-running` needs --remote", ctx=ctx)
    if keep_running and dry_run:
        raise click.UsageError(
            "`--keep-running` has no launch to keep running in a dry run", ctx=ctx
        )
    if remote:
        for name, given in (
            ("--post-cleanup", post_cleanup),
            ("--post-cleanup-purge", post_cleanup_purge),
        ):
            if given:
                raise click.UsageError(
                    f"`{name}` removes run directories after delivery; with --remote, the run "
                    "directories are on the remote: use `xeda remote clean`",
                    ctx=ctx,
                )
        if scrub:
            raise click.UsageError(
                "`--scrub` asks you to confirm the removal of run directories, and a remote "
                "launch cannot ask: run without --remote, or free the space on the host with "
                "`xeda remote clean`",
                ctx=ctx,
            )
        from .remote import client as remote_client

        outcome = remote_client.run_remote(
            chain,
            design=design,
            destination_text=remote,
            run_root=run_root,
            flow_settings=flow_settings,
            xedaproject=xedaproject,
            design_overrides=design_overrides,
            design_allow_extra=design_allow_extra,
            target=target,
            select_design_in_project=select_design_in_project,
            hashed_run_dirs=hashed_run_dirs,
            clean=clean,
            rebuild_all=rebuild_all,
            scrub=scrub,
            dry_run=dry_run,
            fetch_all=fetch_all,
            outputs_to=outputs_to,
            overwrite_outputs=overwrite_outputs,
            confirm_overwrite=_prompt_overwrite if _interactive(json_flag) else None,
            log_level=log_level,
            detailed_logs=detailed_logs,
            keep_running=keep_running,
        )
        document = outcome.document
        if outcome.error is not None:
            log.critical("%s", document["error"]["message"])
            if json_flag:
                emit_structured(document, "json")
            if debug or (not json_flag and not isinstance(outcome.error, XedaException)):
                raise outcome.error
            sys.exit(outcome.exit_status)
        if dry_run:
            if json_flag:
                emit_structured(document, "json")
            else:
                _print_remote_plan(document)
            if not outcome.success:
                error = document.get("error") or {}
                if error.get("message"):
                    log.critical("%s", error["message"])
                log.critical("Remote dry run of flow '%s' on '%s' failed.", flow, remote)
            sys.exit(outcome.exit_status)
        if not outcome.success:
            log.critical("Remote run of flow '%s' on '%s' failed.", flow, remote)
        if json_flag:
            emit_structured(document, "json")
        sys.exit(outcome.exit_status)

    launcher: Optional[DefaultRunner] = None
    table = command_line_table(flow, flow_settings)

    def not_started(error: Exception, spec: Optional[Spec] = None) -> NoReturn:
        """Report a request that started no launch: log the error and, with `--json`, write the
        document of a launch that did not start. Then raise `error` when debugging, or exit with
        status 2 (the request is invalid) or 1 (any other failure)."""
        public = public_error(error)
        info = ErrorInfo(
            code=public.code,
            message=_error_message(error),
            node=getattr(public, "node", None),
            exit_status=None,
            log=None,
        )
        log.critical("%s", info.message)
        if json_flag:
            if spec is None:
                spec_fields = spec_document(
                    flow=chain,
                    **design_info(design, launcher.design_name if launcher is not None else None),
                    target=(launcher.target if launcher is not None else None) or target,
                    settings=table,
                )
            else:
                spec_fields = spec_document_of(spec)
            emit_structured(
                run_document(
                    outcome=None,
                    error=info,
                    launch_id=None,
                    spec=spec_fields,
                    roots=(),
                    nodes=(),
                ),
                "json",
            )
        if debug or (not json_flag and not isinstance(error, XedaException)):
            raise error
        sys.exit(2 if isinstance(public, SpecError) else 1)

    if dry_run:
        try:
            launcher = DefaultRunner(
                run_root,
                rebuild_all=rebuild_all,
                hashed_run_dirs=hashed_run_dirs,
                clean=clean,
                debug=debug,
                outputs_to=outputs_to,
            )
            plan, freshness = launcher.plan_with_freshness(
                chain,
                xedaproject=xedaproject,
                design=design,
                flow_settings=flow_settings,
                select_design_in_project=select_design_in_project,
                design_overrides=design_overrides,
                design_allow_extra=design_allow_extra,
                target=target,
            )
        except Exception as e:
            not_started(e)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    **design_info(design, launcher.design_name),
                    "target": plan.context.target,
                    "success": True,
                    "dry_run": True,
                    "request": request_info(request),
                    "plan": plan_info(plan, freshness),
                },
                "json",
            )
        else:
            _print_plan(plan, freshness)
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
        launcher.confirm_scrub = _prompt_scrub
        launcher.settings.debug = debug
        resolved, plan = launcher.resolve_run(
            chain,
            xedaproject=xedaproject,
            design=design or design_name,
            flow_settings=flow_settings,
            select_design_in_project=select_design_in_project,
            design_overrides=design_overrides,
            design_allow_extra=design_allow_extra,
            target=target,
        )
    except Exception as e:
        not_started(e)
    spec = Spec(chain, resolved.design, settings=table)
    try:
        result = launch_result(launcher, spec, lambda: launcher.run_resolved(resolved, plan))
    except Exception as e:
        not_started(e, spec)
    if not result.passed and result.error is not None:
        log.critical("%s", result.error.message)
    if json_flag:
        emit_structured(result.to_dict(), "json")
    sys.exit(0 if result.passed else 1)


def _print_remote_plan(document: Dict[str, Any]) -> None:
    """Print the plan of a remote dry run, as `_print_plan` prints a local one, and the input
    files that the dry run uploaded."""
    plan = document.get("plan") or {}
    target = f", target {plan['target']}" if plan.get("target") else ""
    click.echo(
        f"Plan for {plan.get('requested')}{target} on {document.get('remote')} "
        "(a dry run: nothing runs)"
    )
    for node in plan.get("nodes", []):
        click.echo(f"  {node['name']}  {node['run_path']}")
        said = node.get("state")
        if said:
            click.echo(f"      {said}" + (f": {node['reason']}" if node.get("reason") else ""))
        if node.get("action_reason"):
            click.echo(f"      always runs: {node['action_reason']}")
        for resolved in node.get("inputs", []):
            origin = resolved["origin"] + (
                f" {resolved['producer']}.{resolved['output']}" if resolved.get("producer") else ""
            )
            click.echo(f"      input {resolved['name']}: {origin}")
        for output in node.get("switched_on", []):
            click.echo(f"      output {output} switched on: a consumer reads it")
    uploaded = document.get("uploaded") or []
    if uploaded:
        click.echo(f"Uploaded {len(uploaded)} input file(s) to {document.get('remote')}:")
        for path in uploaded:
            click.echo(f"  {path}")
    else:
        click.echo(f"Uploaded no input file to {document.get('remote')}: it had them all.")


def _dse_best_document(best: Any) -> Optional[Dict[str, Any]]:
    """Return the best `FlowOutcome` of a design-space exploration as JSON-safe data."""
    if best is None:
        return None
    return json_safe(best.as_json_value())


@cli.command(
    cls=DeclaredEnvvarsCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Run a design-space exploration in parallel.",
    help="Run multiple instances of a flow in parallel to find the optimum value of one or more settings for a given objective function.",
    no_args_is_help=False,
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(flow_names),
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
    Example: --settings clock.period=2.345ns impl.strategy=Debug
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
    help="Specify design.name in case multiple designs are available in a xedaproject.",
)
@click.option(
    "--target",
    metavar="NAME",
    help="Select one of the design's `targets` (`targets.NAME` in the design file). Without "
    "this option, Xeda uses the design itself, with no target. For each key a target sets, its "
    "value wins over the design's and the project's. The order, lowest first, is defaults < "
    "project < design < target < -s < API.",
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
    help="Path to Xeda design file containing the description of a single design.",
)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    type=bool,
    is_flag=True,
    default=True,  # unlike `run`, dse allows extra properties by default
    help="""Allow extra properties (fields) in the design file.""",
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
    help="Maximum number of concurrent runs.",
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
    """Run a design-space exploration, for example a search for the maximum frequency."""
    options: XedaOptions = ctx.obj or XedaOptions()
    debug |= options.debug
    if json_flag:
        # From here on, stdout belongs to the JSON document.
        machine_readable_mode()

    dse: Dse | None = None

    def dse_failure(error_type: str, message: str, exc: Optional[BaseException] = None) -> None:
        log.critical("%s", message)
        if json_flag:
            emit_structured(
                {
                    "flow": flow,
                    **design_info(
                        design or design_name, dse.design_name if dse is not None else None
                    ),
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

    # Resolving the optimizer inside Dse() raises an AttributeError that names nothing useful,
    # so check it here, where the available names are known.
    known_optimizers = {o["name"] for o in optimizers_info()}
    if optimizer not in known_optimizers and "." not in optimizer:
        dse_failure(
            "OptimizerNotFound",
            f"Unknown optimizer '{optimizer}'. Available optimizers: "
            f"{', '.join(sorted(known_optimizers))}. See `xeda list-optimizers`.",
        )

    if log_level is None:
        log_level = logging.WARNING if options.quiet else logging.DEBUG if debug else logging.INFO
    if debug:
        detailed_logs = True
    # No log file here. The exploration logs into its run root (`<root>/Logs`), which it makes
    # when it starts, once its settings have validated. There is one log per exploration.
    setup_logger(log_level, detailed_logs)

    try:
        opt_settings = settings_to_dict(optimizer_settings, hierarchical_keys=True)
        dse_settings_dict = settings_to_dict(dse_settings, hierarchical_keys=True)
    except XedaException as e:  # a key given as a value and as a table
        dse_failure(type(e).__name__, _error_message(e), e)
        raise  # unreachable: dse_failure exits
    if max_workers:
        dse_settings_dict["max_workers"] = max_workers  # overrides

    # `optimizer_settings` sets the same options; prefer it.
    opt_settings = {
        # Only the options that were given. An omitted option has no value, and passing `None`
        # for one would make the optimizer report `input_value=None` instead of the missing
        # setting.
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
            debug=debug,
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
            **design_info(design or design_name, dse.design_name),
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
    # An exploration with no successful run is a failure.
    sys.exit(0 if best is not None else 1)


@cli.command(
    cls=DeclaredEnvvarsCommand,
    context_settings=CONTEXT_SETTINGS,
    short_help="Remove the run directories of a flow for a design.",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    # FlowChoice returns the canonical name. With `removed=True` it also accepts the name of a
    # removed flow, whose run directories can still exist.
    type=FlowChoice(flow_names, removed=True),
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
    help="Remove only the run directories of this target (<run root>/<design_name>/NAME/). The "
    "run directories directly under the design (<design_name>/<flow>) and those of every other "
    "target stay. Without this option, the run directories of FLOW_NAME under every target are "
    "removed too, as they are found on disk. No design file is read.",
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
    """Remove the previous run directories of FLOW_NAME for DESIGN_NAME.

    The run directories lie under <run root>/<design_name>: those directly under the design and
    those of every target (<design_name>/<target>/). With --target, only the run directories of
    that target are removed. Xeda lists the run directories and asks once for confirmation. It
    skips a link that is named like a run directory but does not lead to a run directory of the
    flow beside it, and says so."""
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
        result = scrub_design(
            flow,
            run_root / design_name,
            run_root=run_root,
            target=target,
            confirm=_prompt_scrub,
        )
    except RunDirectoryError as e:  # a run directory that cannot be removed
        fail(type(e).__name__, _error_message(e))
    if json_flag:
        emit_structured(
            {
                "success": True,
                "flow": flow,
                "design": design_name,
                "target": target,
                "run_root": str(run_root),
                "scanned": [str(d) for d in result.scanned],
                "scrubbed": [str(d) for d in result.removed],
                "kept": [str(d) for d in result.kept],
                "gone": [str(d) for d in result.gone],
                "skipped": [str(d) for d in result.skipped],
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

    Xeda generates the flow catalog from the installed version, so it always matches the flows
    and settings that are installed.
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
    help="Show how to enable shell completion, or print the completion script with --stdout. "
    f"The shell is one of: {', '.join(SHELLS.keys())}. The default is the shell that the SHELL environment variable names.",
)
@click.argument(
    "shell",
    required=False,
    type=click.Choice(list(SHELLS.keys()), case_sensitive=False),
)
@click.option(
    "--stdout",
    is_flag=True,
    help="""Produce the shell completion script and output it to the standard output.\n
    Example usage:\n
         $ xeda completion zsh --stdout  > $HOME/.zsh/completion/_xeda\n
         $ xeda completion bash --stdout > $HOME/.local/share/bash-completion/xeda
    """,
)
@click.pass_context
def completion(_ctx: click.Context, stdout: bool, shell: Optional[str] = None):
    """Show how to enable shell completion, or print the completion script with --stdout."""
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
        from .cli_settings_completion import completion_class as adapter_class

        completion_class = adapter_class(shell)
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
