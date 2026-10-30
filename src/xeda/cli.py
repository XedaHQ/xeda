"""The Xeda command-line interface."""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, NoReturn, Optional, Tuple, Union

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
from .types import UNSET, Question, Unset

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
        print_results,
        print_run_tables,
        resolve_format,
        scrub_document,
    )
    from .console import console
    from .design import Design, DesignFileParseError, target_name_problem
    from .errors import ProjectError, SettingsError
    from .local_settings import LocalSettings, local_settings
    from .project import Project
    from .xedaproject import PROJECT_FILE_NAMES, resolve_project_file
    from .flow_runner import (
        add_file_logger,
        get_flow_class,
    )
    from .flow_runner.chains import parse_request
    from .flow_runner.dse import Dse
    from .flow_runner.dse.dse_runner import RESULTS_SHOWN
    from .flow_runner.error_codes import public_error, request_error_info
    from .flow_runner.launch_report import results_of
    from .records import CANCELED, INTERNAL_ERROR, ErrorInfo
    from .flow_runner.scrub import ScrubResult
    from .log_context import launch_id_of
    from .flow_runner.settings_layers import command_line_settings, command_line_table
    from .introspect import (
        boards_info,
        design_info,
        design_schema,
        flow_chain_cells,
        flows_info,
        json_safe,
        optimizers_info,
        platforms_info,
        results_info,
    )
    from .errors import RemoteError, SpecError
    from .run_dir import RunDirectoryError
    from .outcomes import Outcome, run_document, spec_document
    from .plan import spec_document as spec_of
    from .cancel import CancelToken
    from .remote_value import Remote
    from .spec import Spec
    from .workspace import Workspace
    from .utils import ShortLoggerNames, XedaException, settings_to_dict

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


def _removed_option(
    replacement: str, false_replacement: Optional[str] = None, *, gone: Optional[str] = None
):
    """Return the callback of a hidden option that only exists to say what replaced it.

    `replacement` is the text for the option as given (`--foo`). For a pair whose negation
    (`--no-foo`) needs other text, `false_replacement` gives it. An option that nothing replaces
    gives `gone`, the reason, instead: "`--foo` was removed: <reason>; delete it". The message has the form of the
    messages of the launcher settings and the flow settings (`<name>` was removed: use
    <replacement>), so a removed option, launcher setting and flow setting all read the same
    way.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
        if value is not None:
            given = param.secondary_opts[0] if value is False else param.opts[0]
            if gone is not None:
                raise click.UsageError(f"`{given}` was removed: {gone}; delete it", ctx=ctx)
            text = replacement if value or false_replacement is None else false_replacement
            raise click.UsageError(f"`{given}` was removed: use {text}", ctx=ctx)

    return callback


def _removed_debug_option(ctx: click.Context, param: click.Parameter, value: Any) -> None:
    """Refuse `--debug`, `-d` and `XEDA_DEBUG`, and name the one that was given."""
    if value is not None:
        if ctx.get_parameter_source(param.name or "") is ParameterSource.ENVIRONMENT:
            given = "XEDA_DEBUG"
        else:
            given = param.opts[0]
        raise click.UsageError(
            f"`{given}` was removed: use --log-level 10 and --detailed-logs", ctx=ctx
        )


def _removed_options(
    replacement: str | None,
    *names: str,
    callback: Any = None,
    envvar: str | None = None,
    took_value: bool = False,
):
    """Return the decorator that adds a hidden option for each of the removed `names`.

    Each option says what replaced it, in the words of `_removed_option`, and names the spelling
    that was given. An option that `took_value` accepts an optional value, so that the old
    command line `--name=value` or `--name value` gets this message too."""

    def decorator(func):
        for name in reversed(names):
            # A flag, or an option whose value may be left out (`flag_value`).
            kind: dict[str, Any] = (
                {"is_flag": False, "flag_value": ""} if took_value else {"is_flag": True}
            )
            func = click.option(
                name,
                "removed_" + name.lstrip("-").replace("-", "_") + "_option",
                default=None,
                hidden=True,
                expose_value=False,
                envvar=envvar if name.startswith("--") else None,
                callback=callback or _removed_option(replacement or ""),
                **kind,
            )(func)
        return func

    return decorator


# click-extra uses the command name as prog_name, including for Click's completion protocol.
# Keep it aligned with the console entry point and its _XEDA_COMPLETE environment variable.
@click.group("xeda", cls=XedaHelpGroup, no_args_is_help=True, context_settings=CONTEXT_SETTINGS)
@_removed_options("--log-level 10 for more log messages", "--verbose", "-v")
@_removed_options("--log-level 30 for fewer log messages", "--quiet", "-q")
@_removed_options(None, "--debug", "-d", callback=_removed_debug_option, envvar="XEDA_DEBUG")
@click.version_option(message="Xeda v%(version)s")
def cli():
    pass


@cli.command(context_settings=CONTEXT_SETTINGS, short_help="List available flows.")
@output_format_option()
def list_flows(output_format: str, json_flag: bool):
    """List every flow Xeda can run, with its category, default producers and declared
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
        table.add_row(
            escape(flow["name"]),
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
    help="Include the settings accepted by every flow (nthreads, docker, lib_paths, ...).",
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
def list_settings(flow, output_format: str, json_flag: bool, common: bool, tier: str | None):
    """Show every setting of FLOW_NAME that can be given as `-s FLOW_NAME.KEY=VALUE` or in a design file.

    A setting of a group has a dotted name (`global_placement.timing_driven.enabled`)."""
    print_flow_settings(
        flow,
        output_format=resolve_format(output_format, json_flag),
        include_common=common,
        tier=tier,
    )


@cli.command(
    context_settings=CONTEXT_SETTINGS,
    short_help="List the metrics and artifacts a flow reports.",
)
@click.argument(
    "flow",
    metavar="FLOW_NAME",
    type=FlowChoice(flow_names),
    required=True,
)
@output_format_option()
def list_results(flow, output_format: str, json_flag: bool):
    """Show the metrics and the artifacts that FLOW_NAME writes in its run directory.

    The record `results.json` holds the metrics. Each metric has a path, a type and a unit. A
    metric in the units of the platform has no unit. An artifact is a file of the run directory,
    such as a log or a report. It has a key and a condition."""
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
    if info["artifacts"]:
        artifacts = info_table(
            f"{info['flow']} artifacts",
            [
                ("Key", {"header_style": "bold green", "style": "bold"}),
                ("When", {}),
                ("Description", {}),
            ],
        )
        for artifact in info["artifacts"]:
            artifacts.add_row(
                escape(artifact["key"]), escape(artifact["when"]), escape(artifact["description"])
            )
        console.print(artifacts)
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


def _yes_option(func):
    """Add `--yes`, which answers the question of a removal with yes."""
    return click.option("--yes", is_flag=True, default=False, help="Do not ask before removing.")(
        func
    )


def _option_names(message: str, command: click.Command) -> str:
    """Return `message` with each parameter of the API that it names in backticks (`post_cleanup`)
    written as the option of `command` that sets it (`--post-cleanup`)."""
    for param in command.params:
        if isinstance(param, click.Option):
            message = message.replace(f"`{param.name}`", f"`{param.opts[0]}`")
    return message


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

FLOW names one flow (for example ghdl_sim). The producers of the
flow run first. A chain such as `yosys_fpga+nextpnr+fpga_pack` runs its last flow, binding each
preceding flow's declared output to the compatible required inputs of the next flow.
`FLOW.OUTPUT` names the output when several fit. Unbound inputs come from the sources of the
design or from their default producers. Write the + inside one argument. A link may pass through the
flows between its two flows: `yosys_fpga+fpga_pack` runs `nextpnr` in between.

-s NODE.KEY=VALUE sets a setting of a flow of the run, where NODE is the name of the flow:
-s nextpnr.seed=7. To save a binding, write it under flows.NAME.inputs in a YAML design file or
project file. --dry-run shows the plan and runs no tools. With --remote HOST, the whole request
runs on a Linux host over SSH; Xeda installs what it needs there on the first run.

Example: xeda run yosys_fpga+nextpnr+fpga_pack design.yaml
"""


def _request_table(items: Iterable[str] | str | None) -> dict[str, Any]:
    """Return the `-s` items as the settings table of a request document. Items that cannot be
    read give an empty table: the launch reports them."""
    try:
        return command_line_table(list(items or ()))
    except ValueError:
        return {}


def _load_design(
    design: Path | str | None, project: Project | None, updates: Iterable[str], target: Any
) -> Design:
    """Return the design that the command selects: the file of the argument, else a design of the
    project. `updates` are the `--design-updates` items."""
    merged = settings_to_dict(list(updates)) if updates else {}
    if isinstance(design, Path):
        selected = Design.load(design, target=None if target is UNSET else target)
    elif project is None:
        given = f"'{design}' is not a design file, and" if design else "No design was given, and"
        raise ProjectError(
            f"{given} there is no project file to take a design from. Give one with "
            f"--xedaproject, or keep one ({', '.join(PROJECT_FILE_NAMES)}) in the directory "
            f"where you start Xeda ({Path.cwd()}). Or give a design file (.yaml, .yml, .toml or "
            ".json).",
            code="project_file_not_found",
        )
    else:
        selected = project.design(design if design else UNSET, target=target)
    return selected.with_updates(merged) if merged else selected


def _selected_target(target: str | None, no_target: bool) -> str | None | Unset:
    """Return what `--target` and `--no-target` select: a target, no target (`None`), or `UNSET`
    when neither was given, so that the project's `default_target` applies."""
    if target is not None and no_target:
        raise click.UsageError("`--target` and `--no-target` exclude each other.")
    return None if no_target else UNSET if target is None else target


def _selected_project(xedaproject: Any, design: Any) -> Project | None:
    """Return the project of this command, or None: the file of `--xedaproject`, else the one
    project file in the current directory. Only the command line looks there; the library reads
    the project file it is given and nothing else.

    `design` is the `DESIGN` argument. It is always a design file, with or without a suffix. A
    design file that the project lists is refused, and so is a name that is no file but is the
    name of a design of the project: select a project design with `--design-name`, so that one
    design never has two identities."""
    path = resolve_project_file(xedaproject)
    project = None if path is None else Project.load(path)
    if design is not None and project is not None:
        referenced = project.referenced_name(design)
        if referenced is None and not Path(design).exists() and str(design) in project.designs:
            referenced = str(design)
        if referenced is not None:
            raise ProjectError(
                f'"{design}" is the design {referenced!r} of the project file "{project.path}", '
                f"not a design file. Select a design of a project with --design-name {referenced}.",
                code="use_design_name",
                path=project.path,
            )
    if design is not None and not Path(design).exists():
        raise DesignFileParseError(design, "no such file", missing=True)
    return project


def _config_options(func):
    """Add `--config` and `--no-config`, which select the private settings files."""
    func = click.option(
        "--no-config",
        "no_config",
        is_flag=True,
        default=False,
        help="Read neither the user config file nor the project-local file. The host profiles "
        "of `remotes.yaml` are still read.",
    )(func)
    return click.option(
        "--config",
        "config",
        type=click.Path(dir_okay=False, resolve_path=True, path_type=Path),
        default=None,
        help="Read this file in place of the user config file, "
        "$XDG_CONFIG_HOME/xeda/config.yaml (~/.config/xeda/config.yaml). The file must exist.",
    )(func)


def _refuse_both_config_options(config: Path | None, no_config: bool) -> None:
    if config is not None and no_config:
        raise click.UsageError(
            "`--config` and `--no-config` exclude each other.", ctx=click.get_current_context()
        )


def _cli_local_settings(
    project: Project | None, design: Any, config: Path | None, no_config: bool
) -> LocalSettings:
    """Return the private settings of this command: the user file (or the file of `--config`,
    or none with `--no-config`) and, with a project, the project-local file.

    A design file given on the command line gets the user file, and no layer from the project or
    from the project-local file. The project-local `run_root` still counts for the run root,
    since the run root is a fact of the machine and not a setting of the design."""
    settings = local_settings(project, config=None if no_config else config or UNSET)
    if design is not None:
        settings = dataclasses.replace(settings, local={})
    return settings


def _cli_run_root(run_root: Path, local: LocalSettings) -> Path:
    """Return the run root of this command, highest first: `--run-root`, `XEDA_RUN_ROOT`, the
    `run_root` of the project-local file, that of the user file, that of the project (its
    `run_root` key, else `xeda_run` beside the project file), then `./xeda_run` when no project is
    used.

    A run root is a fact of the machine, not a setting of the design. A choice on the command
    line or in the environment wins, and so does the choice of this user for this machine over the
    suggestion that the project file checks in. `local.run_root` holds the last three that the
    files give, whichever design the command runs."""
    source = click.get_current_context().get_parameter_source("run_root")
    chosen = source not in (None, ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP)
    if chosen or local.run_root is None:
        return run_root
    return local.run_root


@contextmanager
def _cancel_on_interrupt(token: CancelToken | None) -> Iterator[None]:
    """Cancel the launch of `token` on the first Ctrl-C, for the time of the block. The launch
    then ends as a `Result` with the outcome `canceled`. A second Ctrl-C raises
    `KeyboardInterrupt`. Without a token, or outside the main thread, no handler is installed."""
    if token is None or threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_number: int, _frame: Any) -> None:
        token.cancel()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(
            signal.SIGINT, previous if previous is not None else signal.default_int_handler
        )


def _interactive(json_flag: bool) -> bool:
    """Return whether a question may be asked: stdin and stderr are terminals, and the output is
    not machine-readable."""
    return not json_flag and sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_question(question: str) -> bool:
    """Ask on the terminal the question of a workspace: its last line is the question, and the
    lines before it say what it is about. The default answer is no."""
    *context, ask = question.splitlines()
    if context:
        click.echo("\n".join(context), err=True)
    try:
        return click.confirm(ask, default=False, err=True)
    except click.Abort:
        return False


def _confirmation(json_flag: bool, yes: bool) -> Callable[[Question], bool]:
    """Return how `xeda run` answers the questions of a workspace (the removal of run directories
    and the replacement of a file), by one rule.

    `--yes` answers the question of a removal with yes. Any question is asked on a terminal, where
    the answer is yes only if the user types it. Without a terminal, nothing is removed or
    replaced, and the log says why and what to do. `--yes` does not answer the replacement of a
    file: `--overwrite-outputs` does."""

    def answer(question: Question) -> bool:
        if yes and question.removal:
            return True
        if _interactive(json_flag):
            return _prompt_question(question)
        log.warning(
            "%s: no terminal to ask on. Run the command in a terminal, pass %s, or call "
            "Workspace(confirm=...) from Python to answer the question.",
            question.undone,
            "--yes" if question.removal else "--overwrite-outputs",
        )
        return False

    return answer


def _prompt_removal(question: str) -> bool:
    """Show the directories that the question of a workspace lists and ask on the terminal (or on
    the input that stands for it) whether to remove them. The first line of the question says what
    the list is, and the last line is the question itself, which this prompt replaces. Only the
    word "yes" says yes. The answer is also no when there is no input to read: the input is at its
    end, closed or unreadable. The command asks even when stdin is no terminal, so a script can
    pipe "yes" to it. The listing and the question go to stderr with `--json`."""
    header, *listing, _ = question.splitlines()
    console.print(f"[red]{escape(header)}[/red]")
    for line in listing:
        _say_scrub(line)
    try:
        answer = console.input("Type 'yes' if you're sure you want to continue: ")
    except (EOFError, ValueError, OSError):
        console.print()
        _say_scrub(
            "Not confirmed. No files or folders were removed. Pass --yes to remove without "
            "asking."
        )
        return False
    if answer.lower() != "yes":
        _say_scrub("Not confirmed. No files or folders were removed.")
        return False
    return True


def _say_scrub(message: str) -> None:
    """Print one line of what scrub did, as it is: not folded inside a path, and no part of it
    taken for markup."""
    console.print(message, markup=False, highlight=False, soft_wrap=True)


def _print_scrub_result(result: ScrubResult) -> None:
    """Say what a scrub did: the links it skipped, the directories that were gone or kept, and
    how many it removed."""
    for link, reason in result.reasons.items():
        _say_scrub(f"skipped {link}: {reason}")
    for gone in result.gone:
        _say_scrub(f"{gone} is gone already")
    for kept in result.kept:
        _say_scrub(
            f"kept {kept}: its run records changed after the listing "
            "(a run finished or refreshed there)"
        )
    if result.removed or result.kept or result.gone:
        summary = f"{len(result.removed)} folders removed"
        if result.kept:
            summary += f", {len(result.kept)} kept"
        if result.gone:
            summary += f", {len(result.gone)} gone already"
        _say_scrub(summary + ".")


def _print_plan(document: Mapping[str, Any], *, host: str | None = None) -> None:
    """Print each planned node of the plan `document` (`Plan.to_dict()`): its directory, what a
    launch would do with it (`fresh`, or a state and its reason: `stale: <reason>`, `unknown:
    <reason>`, `rejected: <message>` or `blocked: <reason>`), its input origins, the outputs that a
    consumer switches on and its deliveries."""
    spec = document["spec"]
    target = f", target {spec['target']}" if spec.get("target") else ""
    where = f" on {host}" if host else ""
    click.echo(f"Plan for {document['roots'][0]}{target}{where} (a dry run: nothing runs)")
    for node in document["nodes"]:
        click.echo(f"  {node['name']}  {node['run_dir']}")
        click.echo(f"      {node['state']}" + (f": {node['reason']}" if node["reason"] else ""))
        if node["action"]:
            # Static class metadata: why the flow can only end a chain.
            click.echo(f"      always runs: {get_flow_class(node['flow']).action_reason}")
        for item in node["inputs"]:
            if item["origin"] == "producer":
                made = f"{item['producer']}.{item['output']}"
            elif item["origin"] == "source":
                made = "source " + ", ".join(item["sources"])
            else:
                made = "none"
            click.echo(f"      {item['name']} <- {made} ({item['reason']})")
        for output in node["outputs_enabled"]:
            click.echo(f"      output {output} switched on: a consumer reads it")
        for delivery in node["deliveries"]:
            replaces = " (replaces a file)" if delivery["replaces"] else ""
            click.echo(f"      delivers {delivery['key']} to {delivery['destination']}{replaces}")


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
    # A missing file is reported by `_selected_project`, which tells a name of a project design.
    type=click.Path(file_okay=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    required=False,
)
@_run_root_options
@_config_options
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
@_removed_options("the positional DESIGN argument", "--design-file", "--design", took_value=True)
@click.option(
    "--design-name",
    help="Specify design.name in case multiple designs are available in a project file.",
)
@click.option(
    "--target",
    metavar="NAME",
    help="Select one of the design's `targets` (`targets.NAME` in the design file). Without "
    "this option, a design of a project (--design-name) uses the `default_target` of the "
    "project, and any other design uses no target. For each key a target sets, its "
    "value wins over the design's and the project's. The order, lowest first, is defaults < "
    "project < design < target < -s < API.",
)
@click.option(
    "--no-target",
    is_flag=True,
    default=False,
    help="Select no target: use the design itself, even when the project has a "
    "`default_target`.",
)
@_removed_options("--design-updates", "--design-overrides", took_value=True)
@click.option(
    "--design-updates",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Update the design: merge the KEY=VALUE items over it, after the target is selected.
    KEY is a design key in dot notation, for example rtl.top or tb.parameters.W.""",
)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    is_flag=True,
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "", gone="a design file holds the keys of the design and nothing else"
    ),
)
@_removed_options("-s or --settings", "--flow-settings", took_value=True)
@click.option(
    "--settings",
    "-s",
    "flow_settings",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Override setting values. Every KEY starts with the name of the flow that it addresses: NODE.KEY=VALUE, for a flow of the run (the last flow, or one of its producers). Separate multiple items with spaces; the list ends at the next option or the first token that is not KEY=VALUE (use -- before a design path that looks like one). KEY can be a hierarchical name using dot notation.
    Example: --settings vivado_synth.clocks.main_clock.period=2.345ns vivado_synth.impl.strategy=Debug
    Another flow of the run: -s yosys_fpga.flatten=true.
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
    "--prune-variants",
    is_flag=True,
    help="Before running, remove the other run directories of every flow of the plan, the "
    "producers included, for this design and target. It asks for confirmation on a terminal, "
    "and without one it removes nothing unless you pass --yes. It cannot be used with --remote.",
)
@_yes_option
@_removed_options("--prune-variants", "--scrub")
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
@_removed_options(None, "--debug", "-d", callback=_removed_debug_option)
@_removed_options("`xeda list-settings FLOW`", "--help-settings", "--list-settings")
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
    "Run nothing, generators included. Fetch the Git dependencies of the design.",
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
    rebuild_all: bool = False,
    hashed_run_dirs: bool = False,
    flow_settings: Union[None, str, Iterable[str]] = None,
    clean: bool = False,
    run_root: Optional[Path] = None,
    config: Optional[Path] = None,
    no_config: bool = False,
    xedaproject: Optional[str] = None,
    # design: Optional[str] = None,
    design_name: Optional[str] = None,
    target: str | None = None,
    no_target: bool = False,
    design_updates: Iterable[str] = tuple(),
    log_level: Optional[int] = None,
    detailed_logs: bool = False,
    post_cleanup: bool = False,
    post_cleanup_purge: bool = False,
    prune_variants: bool = False,
    yes: bool = False,
    remote: Optional[str] = None,
    fetch_all: bool = False,
    keep_running: bool = False,
    outputs_to: Optional[Path] = None,
    overwrite_outputs: bool = False,
    dry_run: bool = False,
    json_flag: bool = False,
):
    """Run a flow or a chain."""
    assert ctx
    # FLOW is the canonical request text. The results, the settings and the exit status are
    # those of its last flow.
    _refuse_both_config_options(config, no_config)
    request = parse_request(flow)
    chain, flow = flow, request.requested.name
    if json_flag:
        # From here on, stdout belongs to the JSON document.
        machine_readable_mode()
    if run_root is None:
        run_root = Path.cwd() / "xeda_run"

    if log_level is None:
        log_level = logging.INFO
    setup_logger(log_level, detailed_logs)

    # The positional DESIGN argument is a design file, and --design-name names a design of the
    # project. Neither selects the only design of the project (`project.design()`).
    if design_file and design_name:
        raise click.UsageError("give DESIGN or --design-name, not both", ctx=ctx)
    design = Path(design_file) if design_file else design_name

    if remote == "":
        # An empty `XEDA_REMOTE` is the same as an unset one. An empty `--remote` is a mistake:
        # the option names a host.
        if ctx.get_parameter_source("remote") is not ParameterSource.ENVIRONMENT:
            raise click.UsageError(
                "`--remote` needs a host, such as `user@host`; leave it out to run locally",
                ctx=ctx,
            )
        remote = None
    selected_target = _selected_target(target, no_target)

    def request_spec() -> Dict[str, Any]:
        """The `spec` of a document of a launch that never got its `Spec`: the request text."""
        return spec_document(
            flow=chain,
            **design_info(design, None),
            target=target,
            settings=_request_table(flow_settings),
        )

    def canceled_before_start(launch_id: Optional[str] = None) -> NoReturn:
        """Report a Ctrl-C that ended the command without a result: while it loaded the design or
        planned, before the launch started, or (with a `launch_id`) as a second Ctrl-C while the
        launch ran. With `--json`, the document has no outcome and the error `canceled`; the exit
        status is 1. Without `--json`, the interrupt propagates. Call it while the
        `KeyboardInterrupt` is handled."""
        if not json_flag:
            raise  # pylint: disable=misplaced-bare-raise
        canceled = ErrorInfo(
            CANCELED,
            (
                "Ctrl-C canceled the command before the launch started."
                if launch_id is None
                else "A second Ctrl-C ended the command while the launch was running."
            ),
            None,
            None,
            None,
        )
        log.critical("%s", canceled.message)
        emit_structured(
            run_document(
                outcome=None,
                error=canceled,
                launch_id=launch_id,
                spec=request_spec(),
                roots=(),
                nodes=(),
            ),
            "json",
        )
        sys.exit(1)

    def not_started(error: Exception, spec: Optional[Spec] = None) -> NoReturn:
        """Report an exception that ended the command before the launch gave an outcome.

        A request that is invalid started no launch: with `--json`, its document has no
        outcome, and the exit status is 2. So did a request to a host that cannot be reached or
        set up. Any other exception ends a launch with a failure of its own: the document has the
        outcome `failed` and no node, and the exit status is 1. The log line keeps the class name
        of the exception."""
        public = public_error(error)
        info = request_error_info(error)
        text = _error_message(error)
        if info.code == "unsupported_with_remote":
            # The API names the parameter (`post_cleanup`). The user typed the option.
            info = dataclasses.replace(info, message=_option_names(info.message, ctx.command))
            text = _option_names(text, ctx.command)
        log.critical("%s", text)
        refused = isinstance(public, (SpecError, RemoteError))
        if json_flag:
            emit_structured(
                run_document(
                    outcome=None if refused else Outcome.FAILED,
                    error=info,
                    launch_id=(
                        None
                        if refused
                        else launch_id_of(error) or launch_id_of(error.__cause__ or error)
                    ),
                    spec=request_spec() if spec is None else spec_of(spec),
                    roots=(),
                    nodes=(),
                ),
                "json",
            )
        # A bug keeps its traceback.
        if not json_flag and (info.code == INTERNAL_ERROR or not isinstance(error, XedaException)):
            raise error
        sys.exit(2 if refused else 1)

    # The command line finds the project file (`--xedaproject`, else the one in the current
    # directory). The library discovers nothing.
    try:
        project = _selected_project(xedaproject, design_file)
        local = _cli_local_settings(project, design_file, config, no_config)
    except Exception as e:
        not_started(e)
    if not design and project is None:
        raise click.UsageError(
            "No design specified. Pass a design file as the DESIGN argument, or name one with "
            "--design-name.",
            ctx=ctx,
        )
    run_root = _cli_run_root(run_root, local)
    if fetch_all and not remote:
        raise click.UsageError("`--fetch-all` needs --remote", ctx=ctx)
    if keep_running and not remote:
        raise click.UsageError("`--keep-running` needs --remote", ctx=ctx)
    if keep_running and dry_run:
        raise click.UsageError(
            "`--keep-running` has no launch to keep running in a dry run", ctx=ctx
        )
    try:
        workspace = Workspace(
            run_root,
            hashed_run_dirs=hashed_run_dirs,
            tool_output="terminal",
            confirm=_confirmation(json_flag, yes),
            remote=(
                Remote(
                    remote,
                    profile=local.host_profiles.get(remote),
                    fetch_all=fetch_all,
                    keep_running=keep_running,
                )
                if remote
                else None
            ),
            local_settings=local,
        )
        spec = Spec(
            chain,
            _load_design(design, project, design_updates, selected_target),
            settings=command_line_settings(list(flow_settings or ()), request.requested.name),
        )
    except KeyboardInterrupt:
        canceled_before_start()
    except Exception as e:
        # An item that is no KEY=VALUE is a bad setting. Other errors are public already.
        bad_item = type(e) is ValueError
        not_started(SettingsError(str(e), code="invalid_value") if bad_item else e)
    options: Dict[str, Any] = dict(
        rebuild_all=rebuild_all,
        clean=clean,
        prune_variants=prune_variants,
        outputs_to=outputs_to,
        overwrite_outputs=overwrite_outputs,
        post_cleanup=post_cleanup,
        post_cleanup_purge=post_cleanup_purge,
    )

    if dry_run:
        try:
            plan_document = workspace.plan(spec, **options).to_dict()
        except KeyboardInterrupt:
            canceled_before_start()
        except Exception as e:
            not_started(e, spec)
        if json_flag:
            emit_structured(plan_document, "json")
        else:
            _print_plan(plan_document, host=remote)
        sys.exit(0)

    # A launch on this machine ends on the first Ctrl-C as a canceled result. A launch on a host
    # has the interrupt handling of its client.
    token = None if remote else CancelToken()
    try:
        with _cancel_on_interrupt(token):
            result = workspace.run(spec, **options, cancel=token)
    except KeyboardInterrupt as interrupt:
        canceled_before_start(launch_id_of(interrupt))
    except Exception as e:
        not_started(e, spec)
    if remote:
        if result.error is not None:
            log.critical("%s", result.error.message)
        shown = result.nodes[result.roots[0]] if result.roots else None
        if not json_flag and shown is not None and shown.record is not None:
            print_results(
                results_of(result.record),
                title=f"Results of flow:{flow} design:{spec.design.name}",
                skip_if_false={"artifacts", "reports"},
            )
        if not result.passed:
            log.critical("Remote run of flow '%s' on '%s' failed.", flow, remote)
    else:
        if not json_flag:
            print_run_tables(result, spec.design.name)
        if not result.passed and result.error is not None:
            log.critical("%s", result.error.message)
    if json_flag:
        emit_structured(result.to_dict(), "json")
    sys.exit(0 if result.passed else 1)


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
@click.argument(
    "design",
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
@_removed_options("-s or --settings", "--flow-settings", took_value=True)
@click.option(
    "--settings",
    "-s",
    "flow_settings",
    metavar="KEY=VALUE...",
    type=tuple,
    cls=OptionEatAll,
    default=tuple(),
    help="""Override setting values for the executed flow. Every KEY starts with the name of the flow: FLOW.KEY=VALUE. Separate multiple items with spaces; the list ends at the next option or the first token that is not KEY=VALUE (use -- before a design path that looks like one). KEY can be a hierarchical name using dot notation.
    Example: --settings vivado_synth.clocks.main_clock.period=2.345ns vivado_synth.impl.strategy=Debug
    """,
)
@_run_root_options
@_config_options
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
    "this option, a design of a project (--design-name) uses the `default_target` of the "
    "project, and any other design uses no target. For each key a target sets, its "
    "value wins over the design's and the project's. The order, lowest first, is defaults < "
    "project < design < target < -s < API.",
)
@click.option(
    "--no-target",
    is_flag=True,
    default=False,
    help="Select no target: use the design itself, even when the project has a "
    "`default_target`.",
)
@_removed_options("the positional DESIGN argument", "--design", "--design-file", took_value=True)
@click.option(
    "--design-allow-extra/--no-design-allow-extra",
    is_flag=True,
    default=None,
    hidden=True,
    expose_value=False,
    callback=_removed_option(
        "", gone="a design file holds the keys of the design and nothing else"
    ),
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
    "--init-freq-low",
    type=float,
)
@click.option(
    "--init-freq-high",
    type=float,
)
@_removed_options("--init-freq-low", "--init_freq_low", took_value=True)
@_removed_options("--init-freq-high", "--init_freq_high", took_value=True)
@_removed_options(None, "--debug", "-d", callback=_removed_debug_option)
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
def dse(
    flow: str,
    design: Optional[Path],
    flow_settings: Tuple[str, ...],
    optimizer: str,
    optimizer_settings: Tuple[str, ...],
    dse_settings: Tuple[str, ...],
    max_workers: Optional[int],
    init_freq_low: float,
    init_freq_high: float,
    run_root: Path,
    config: Optional[Path] = None,
    no_config: bool = False,
    xedaproject: Optional[str] = None,
    design_name: Optional[str] = None,
    target: str | None = None,
    no_target: bool = False,
    log_level: Optional[int] = None,
    detailed_logs: bool = True,
    json_flag: bool = False,
):
    """Run a design-space exploration, for example a search for the maximum frequency."""
    if json_flag:
        # From here on, stdout belongs to the JSON document.
        machine_readable_mode()

    if design and design_name:
        raise click.UsageError("give DESIGN or --design-name, not both")
    _refuse_both_config_options(config, no_config)
    dse: Dse | None = None
    selected_target = _selected_target(target, no_target)

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
        log_level = logging.INFO
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
        project = _selected_project(xedaproject, design)
        local = _cli_local_settings(project, design, config, no_config)
        dse = Dse(
            optimizer_class=optimizer,
            optimizer_settings=opt_settings,
            run_root=_cli_run_root(run_root, local),
            **dse_settings_dict,
        )
        # The tables go to stderr with --json (`machine_readable_mode`).
        dse.best_so_far = lambda best: print_results(
            best.results, title="Best so far", subset=RESULTS_SHOWN, skip_if_false=True
        )
        best = dse.run(
            flow,
            design=_load_design(design or design_name, project, (), selected_target),
            flow_settings=list(flow_settings),
            local_settings=local,
        )
    except SystemExit:
        raise
    except BaseException as e:
        dse_failure(type(e).__name__, _error_message(e), e)
        raise  # unreachable: dse_failure exits
    if best is not None:
        print_results(best.results, title="Best results", subset=RESULTS_SHOWN)
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
@_yes_option
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
def scrub(ctx: click.Context, flow, design_name, target, run_root, yes, json_flag):
    """Remove the previous run directories of FLOW_NAME for DESIGN_NAME.

    The run directories lie under <run root>/<design_name>: those directly under the design and
    those of every target (<design_name>/<target>/). With --target, only the run directories of
    that target are removed. Xeda lists the run directories and asks once for confirmation;
    --yes answers yes. It skips a link that is named like a run directory but does not lead to a
    run directory of the flow beside it, and says so."""
    if json_flag:
        machine_readable_mode()
    workspace: Workspace | None = None

    def report(error: Exception | None, result: Optional[ScrubResult] = None) -> None:
        """Print the document of the command: what the scrub did, or the error that ended it."""
        if json_flag:
            emit_structured(
                scrub_document(
                    outcome=Outcome.PASSED if error is None else Outcome.FAILED,
                    error=None if error is None else request_error_info(error),
                    flow=flow,
                    design=design_name,
                    target=target,
                    run_root=None if workspace is None else str(workspace.run_root),
                    result=result,
                ),
                "json",
            )

    try:
        if target is not None and (problem := target_name_problem(target)) is not None:
            raise RunDirectoryError(problem)
        workspace = Workspace(run_root, confirm=(lambda question: True) if yes else _prompt_removal)
        result = workspace.scrub(
            design_name=design_name, flow=flow, target=UNSET if target is None else target
        )
    except Exception as e:
        log.critical("%s", _error_message(e))
        report(e)
        if not json_flag and (
            request_error_info(e).code == INTERNAL_ERROR or not isinstance(e, XedaException)
        ):
            raise
        sys.exit(1)
    _print_scrub_result(result)
    report(None, result)


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
