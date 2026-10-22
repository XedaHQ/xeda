"""Utilities for the command-line interface."""

import json
import logging
import sys
from contextlib import contextmanager
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

import click
import yaml
from click_extra import Command as ColorizedCommand
from click_extra import Context as ColorizedContext
from click_extra import Group as ColorizedGroup
from click_extra import HelpFormatter, HelpTheme
from click_extra import Style as HelpStyle
from click_extra.theme import get_default_theme, set_default_theme
from rich import box
from rich.markup import escape
from rich.style import Style
from rich.table import Table
from simple_term_menu import TerminalMenu

from . import proc_utils
from .cli_types import (
    ChainChoice as ChainChoice,
    DOCUMENT_FORMATS as DOCUMENT_FORMATS,
    FlowChoice as FlowChoice,
    OptionEatAll as OptionEatAll,
    OUTPUT_FORMATS as OUTPUT_FORMATS,
    TIER_NAMES,
    _is_key_value,
    output_format_option as output_format_option,
)
from .console import console, console_target, redirect_console, restore_console
from .design import Design
from .flow import Flow
from .flow_runner import FlowNotFoundError, XedaOptions, get_flow_class
from .introspect import settings_info, type_str
from .units import is_number_text, is_quantity_unit
from .xedaproject import XedaProject

__all__ = [
    "ChainChoice",
    "HELP_FORMATTER_SETTINGS",
    "XEDA_HELP_THEME",
    "ConsoleLogo",
    "DeclaredEnvvarsCommand",
    "FlowChoice",
    "OptionEatAll",
    "XedaCommand",
    "XedaHelpGroup",
    "emit_structured",
    "info_table",
    "machine_readable_mode",
    "output_format_option",
    "print_flow_settings",
    "requested_output_format",
    "reraise_suggesting_visible",
    "unit_after_space_hint",
    "wants_machine_readable",
]

#: The help-screen palette: yellow headings and green options, to match the rich tables.
#: click-extra themes every element of the screen, so the rest of the built-in theme (metavars,
#: choices, envvars, defaults, ...) stays as it is.
XEDA_HELP_THEME: HelpTheme = get_default_theme().with_(
    heading=HelpStyle(fg="yellow", bold=True),
    option=HelpStyle(fg="green", bold=True),
)

#: The formatter settings of the help screens. They belong in `context_settings`, not on a
#: command: cloup resolves the formatter from the context, and a child context inherits the
#: settings of its parent, so the theme placed here reaches every subcommand. A
#: `formatter_settings=` passed to a command would style only that one screen.
HELP_FORMATTER_SETTINGS: Dict[str, Any] = HelpFormatter.settings(theme=XEDA_HELP_THEME)


log = logging.getLogger(__name__)


class ConsoleLogo:
    """The Xeda ASCII logo on the console."""

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
    def print(cls):
        """Print the logo if the console is wide enough."""
        if console.width >= 80:
            console.print(
                cls.logo.format(X="[dark_orange]", O="[/]"),
                highlight=False,
                emoji=False,
            )


def requested_output_format(argv: Sequence[str]) -> Optional[str]:
    """Return the machine-readable format that this invocation asked for, or `None` for human
    output.

    `--json` is shorthand for `--format json`. `--format table` is human output, so it returns
    `None` and nothing is intercepted.
    """
    args = list(argv)
    if "--json" in args:
        return "json"
    machine_formats = (*DOCUMENT_FORMATS, "jsonl")
    for i, arg in enumerate(args):
        if arg.startswith("--format="):
            requested = arg.split("=", 1)[1]
            return requested if requested in machine_formats else None
        if arg == "--format" and i + 1 < len(args):
            return args[i + 1] if args[i + 1] in machine_formats else None
    return None


def wants_machine_readable(argv: Sequence[str]) -> bool:
    """Return whether this invocation asked for machine-readable output on stdout."""
    return requested_output_format(argv) is not None


@contextmanager
def _restoring_output_streams() -> Iterator[None]:
    """Undo any output redirection that this invocation applied, however the invocation ends.

    `machine_readable_mode()` points the rich console and tool output at `sys.stderr`, and both
    are module-global. Without the restore, a `--json` command leaves every later command in the
    same process writing to a stream that belonged to it. This happens under
    `click.testing.CliRunner` and when the CLI is driven as a library. A one-shot `xeda` process
    is not affected.
    """
    console_previous = console_target()
    tool_previous = proc_utils.tool_output_redirect()
    try:
        yield
    finally:
        restore_console(console_previous)
        proc_utils.set_tool_output(tool_previous)


class _DeclaredEnvvarsContext(ColorizedContext):
    """A context with no automatic environment variables. Otherwise click derives a prefix from
    the parent (`XEDA_RUN_<OPTION>`) even when the command asks for none."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.auto_envvar_prefix = None


def reraise_suggesting_visible(
    command: click.Command, ctx: click.Context, error: click.NoSuchOption
) -> NoReturn:
    """Raise `error` again, with the close matches of the mistyped option taken from the options
    that the help of `command` lists.

    click suggests from every option that a command has. A hidden option exists only to say what
    replaced a removed one (`--xeda-run-dir`), so a suggestion must never lead to it. Every
    command and group of Xeda calls this function, so the rule holds for each of them.
    """
    if not error.possibilities:  # nothing was close, or a short option: nothing to leave out
        raise error
    # click suggests from the long names. A short name (`-s`) is not among them.
    visible = [
        name
        for param in command.get_params(ctx)
        if isinstance(param, click.Option) and not param.hidden
        for name in (*param.opts, *param.secondary_opts)
        if len(name) > 2
    ]
    raise click.NoSuchOption(
        error.option_name, error.message, possibilities=visible, ctx=error.ctx
    ) from error


_EXTRA_ARGUMENT = "Got unexpected extra argument ("


def unit_after_space_hint(message: str, args: Sequence[str]) -> Optional[str]:
    """Return the hint for a unit that a user wrote after a space, or None.

    `-s clock.period=5.5 ns` ends the list of `KEY=VALUE` items at `ns`, and click reports `ns` as
    an extra argument. `message` is the text of that error and `args` the command line. The hint
    applies when the first extra argument is exactly a unit of a quantity and the token before it
    is an item whose value is a number.
    """
    if not message.startswith(_EXTRA_ARGUMENT):
        return None
    unit = message.removeprefix(_EXTRA_ARGUMENT).split(" ", 1)[0].removesuffix(")")
    if not is_quantity_unit(unit):
        return None
    for index in range(1, len(args)):
        if args[index] != unit:
            continue
        item = args[index - 1]
        key, _, value = item.partition("=")
        if not _is_key_value(item) or not is_number_text(value.strip()):
            continue
        option = next((t for t in reversed(args[: index - 1]) if t.startswith("-")), None)
        prefix = f"{option} " if option is not None else ""
        return (
            f"Write the unit without a space ({prefix}{key}={value.strip()}{unit}), or quote the "
            f'item ({prefix}"{key}={value.strip()} {unit}").'
        )
    return None


class XedaCommand(ColorizedCommand):
    """The command class of the Xeda commands. It suggests only options that its help lists, and
    explains a unit that a user wrote after a space."""

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        command_line = list(args)  # the parser consumes `args`
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            reraise_suggesting_visible(self, ctx, error)
        except click.UsageError as error:
            hint = unit_after_space_hint(error.message, command_line)
            if hint is None:
                raise
            raise click.UsageError(f"{error.message}. {hint}", ctx=error.ctx) from error


class DeclaredEnvvarsCommand(XedaCommand):
    """A command whose options read only the environment variables that they declare (`envvar=`).

    With the `auto_envvar_prefix` of the group, every option has a hidden second name. A leftover
    `export XEDA_CLEAN=1` would then empty every run directory on every run, with nothing on the
    command line to turn it off. click-extra also registers the automatic names on the options
    themselves (to show them in the help), so both the settings of the command and its context
    drop the prefix.
    """

    context_class = _DeclaredEnvvarsContext

    def __init__(self, *args, **kwargs) -> None:
        kwargs["context_settings"] = {
            **(kwargs.get("context_settings") or {}),
            "auto_envvar_prefix": None,
        }
        super().__init__(*args, **kwargs)


class XedaHelpGroup(ColorizedGroup):
    """The command group that shows the Xeda help screens and reports errors in the requested
    output format."""

    command_class = XedaCommand

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            reraise_suggesting_visible(self, ctx, error)

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter):
        ConsoleLogo.print()
        super().format_usage(ctx, formatter)

    def main(self, args=None, **extra):  # type: ignore[override]
        """Report argument errors in the format that the invocation asked for.

        Click handles a `UsageError` itself: it prints to stderr and exits with status 2. A
        caller that asked for machine-readable output then gets empty stdout and nothing to
        parse. This method writes the error document in the requested format, so `--format yaml`
        gets YAML. It intercepts only the machine-readable path. Ordinary invocations are not
        changed.
        """
        # The `help` subcommand that click-extra injects renders its target through a plain
        # `click.Context`, which carries no `formatter_settings` and so falls back to the default
        # palette. Setting the process-wide default here keeps `xeda help run` looking like
        # `xeda run --help`. This happens in `main()` and not at import, so importing this
        # module never restyles the help screens of another click-extra application.
        set_default_theme(XEDA_HELP_THEME)
        argv = list(sys.argv[1:] if args is None else args)
        fmt = requested_output_format(argv)
        with _restoring_output_streams():
            if fmt is None:
                return super().main(args=args, **extra)
            return self._main_machine_readable(fmt, args, **extra)

    def _main_machine_readable(self, fmt: str, args=None, **extra):
        try:
            return super().main(args=args, **{**extra, "standalone_mode": False})
        except click.UsageError as e:
            if _is_run_command(e.ctx):
                from .outcomes import run_document

                # The document of a launch that did not start: the command line has no spec.
                emit_structured(
                    run_document(
                        outcome=None,
                        error=_usage_error_info(e),
                        launch_id=None,
                        spec=None,
                        roots=(),
                        nodes=(),
                    ),
                    fmt,
                )
                sys.exit(e.exit_code)
            self._report_click_error(e, fmt)
        except click.ClickException as e:
            self._report_click_error(e, fmt)
        except click.exceptions.Abort:
            emit_structured(
                {"success": False, "error": {"type": "Abort", "message": "Aborted."}}, fmt
            )
            sys.exit(1)

    @staticmethod
    def _report_click_error(e: click.ClickException, fmt: str) -> NoReturn:
        emit_structured(
            {
                "success": False,
                "error": {"type": type(e).__name__, "message": e.format_message()},
            },
            fmt,
        )
        sys.exit(e.exit_code)


#: The code of a file that a path parameter of `run` names and that does not exist.
_MISSING_FILE_CODES = {
    "design_file": "design_file_not_found",
    "design_file_opt": "design_file_not_found",
    "xedaproject": "project_file_not_found",
}


def _usage_error_info(error: click.UsageError) -> Any:
    """Return the `ErrorInfo` of a usage error of `run`.

    A parameter that Xeda converts with its own rules (a flow name, a chain) fails with the
    refusal of that rule, and the error has its code and message. A file that does not exist has
    the code of the missing file. Any other usage error is click's own: `usage_error`."""
    from .records import ErrorInfo

    cause = error.__context__
    if isinstance(cause, Exception):
        from .flow_runner.error_codes import public_error, request_error_info
        from .utils import XedaException

        if isinstance(cause, XedaException) and public_error(cause).code != "internal_error":
            return request_error_info(cause)
    param = getattr(error, "param", None)
    code = _MISSING_FILE_CODES.get(getattr(param, "name", None) or "")
    message = error.format_message()
    if code is not None and "does not exist" not in message:
        code = None
    return ErrorInfo(
        code=code or "usage_error", message=message, node=None, exit_status=None, log=None
    )


def _is_run_command(ctx: Optional[click.Context]) -> bool:
    """Return whether `ctx` is the context of `xeda run`, the command whose `--json` document is
    the document of a launch."""
    return ctx is not None and ctx.parent is not None and ctx.command.name == "run"


def info_table(title: str, columns: Iterable[Tuple[str, Dict[str, Any]]]) -> Table:
    """Return the empty table that a query command fills: the `title` and the `columns`, each a
    name with the keyword arguments of its rich column."""
    table = Table(
        title=title,
        show_header=True,
        header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    for name, kwargs in columns:
        # Fold long text instead of truncating it: a truncated name cannot be typed back into a
        # command.
        table.add_column(name, overflow="fold", **kwargs)
    return table


def resolve_format(output_format: str, json_flag: bool) -> str:
    """Return the output format, with `--json` as shorthand for `--format json`."""
    return "json" if json_flag else output_format


def emit_structured(data: Any, fmt: str, records: Optional[Sequence[Any]] = None) -> None:
    """Write `data` to stdout in `fmt`.

    This function bypasses the rich console: nothing is wrapped, styled or truncated to the
    terminal width, so identifiers stay complete when the output is piped.

    `records` supplies the stream of one object per line for `jsonl` when that stream differs
    from `data` (for example, the fields of a settings document instead of the document itself).
    """
    out = sys.stdout
    if fmt == "json":
        json.dump(data, out, indent=2, default=str)
        out.write("\n")
    elif fmt == "jsonl":
        if records is None:
            records = data if isinstance(data, list) else [data]
        for record in records:
            out.write(json.dumps(record, default=str) + "\n")
    elif fmt == "yaml":
        yaml.safe_dump(data, out, sort_keys=False, default_flow_style=False, allow_unicode=True)
    else:
        raise click.UsageError(f"Unsupported output format: {fmt!r}")
    out.flush()


def machine_readable_mode() -> None:
    """Give stdout exclusively to the machine-readable result.

    Rich output (tables, prompts, the logo) and the stdout of every tool move to stderr. Child
    processes that would otherwise inherit stdout are redirected too. Logging already goes to
    stderr.
    """
    redirect_console(sys.stderr)
    proc_utils.set_tool_output(sys.stderr)


def _fmt_default(value: Any, required: bool) -> str:
    """Render a default value for a table cell.

    Everything but the `<required>` marker is markup-escaped. Otherwise rich would read a type or
    default that contains square brackets (`array[string]`, `["a"]`) as a style tag, and the text
    would disappear from the table.
    """
    if required:
        return "[red]<required>[/red]"
    if value is None:
        return "None"
    if isinstance(value, bool):
        return escape(str(value).lower())
    if isinstance(value, str):
        return escape(f'"{value}"')
    if isinstance(value, (dict, list)):
        return escape(json.dumps(value, default=str))
    return escape(str(value))


def _settings_table(title: str, fields: Sequence[Dict[str, Any]]) -> Table:
    table = Table(
        title=title,
        show_header=True,
        header_style="bold yellow",
        title_style=Style(frame=True, bold=True),
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    # Use `overflow="fold"` and not the ellipsis that rich uses by default: a truncated setting
    # name ("set_synth_proper...") cannot be typed back into `-s KEY=VALUE`.
    table.add_column("Setting", header_style="bold green", style="bold", overflow="fold")
    table.add_column("Type", overflow="fold")
    table.add_column("Default", overflow="fold")
    table.add_column("Description", overflow="fold")
    for field in fields:
        name = escape(field["name"])
        if field.get("alias"):
            name += f"\n[dim](alias: {escape(str(field['alias']))})[/dim]"
        description = field.get("description") or ""
        if field.get("enum"):
            choices = ", ".join(str(c) for c in field["enum"])
            description = (description + f"\nOne of: {choices}").strip()
        table.add_row(
            name,
            escape(field.get("type", "any")),
            _fmt_default(field.get("default"), bool(field.get("required"))),
            escape(description),
        )
    return table


def _definitions_tables(definitions: Dict[str, Any], debug: bool = False) -> None:
    for type_name, type_def in definitions.items():
        properties = (type_def or {}).get("properties")
        if debug:
            console.print(f"Type: {type_name}")
            console.print_json(data=type_def)
        if not properties:
            enum = (type_def or {}).get("enum")
            if enum:
                console.print(
                    f"Type [blue]{type_name}[/blue]: one of "
                    + ", ".join(f"[bold]{v}[/bold]" for v in enum)
                )
            continue
        table = Table(
            title=f"Type [blue]{type_name}[/blue]",
            show_header=True,
            header_style="bold green",
            title_style=Style(frame=True, bold=True),
            box=box.SQUARE_DOUBLE_HEAD,
            show_lines=True,
        )
        table.add_column("property", overflow="fold")
        table.add_column("type", overflow="fold")
        table.add_column("description", overflow="fold")
        for prop, prop_def in properties.items():
            if prop_def.get("hidden_from_schema"):
                continue
            description = prop_def.get("description", prop_def.get("title", "-"))
            table.add_row(
                escape(prop),
                escape(type_str(prop_def, definitions)),
                escape(str(description).rstrip(" .")),
            )
        console.print(table)


def print_flow_settings(
    flow: Union[str, Type[Flow]],
    options: Optional[XedaOptions] = None,
    output_format: str = "table",
    include_common: bool = True,
    tier: Optional[str] = None,
) -> None:
    """Render the settings of a flow, as tables or as a machine-readable document.

    With `tier`, only the settings up to that tier are shown (`advanced` shows `common` and
    `advanced`). A table lists the `common` settings of the flow first, then a table for each
    further tier that has settings, then the settings that every flow accepts."""
    flow_class = discover_flow_class(flow) if isinstance(flow, str) else flow
    info = settings_info(flow_class)
    shown = TIER_NAMES if tier is None else TIER_NAMES[: TIER_NAMES.index(tier) + 1]
    fields: List[Dict[str, Any]] = [
        f
        for f in info["fields"]
        if (include_common or not f.get("common")) and f.get("tier", "common") in shown
    ]
    if output_format != "table":
        document = {**info, "fields": fields}
        emit_structured(
            document,
            output_format,
            records=[{"flow": info["flow"], **f} for f in fields],
        )
        return
    specific = [f for f in fields if not f.get("common")]
    common = [f for f in fields if f.get("common")]
    console.print(
        _settings_table(
            f"{info['flow']} settings", [f for f in specific if f["tier"] == TIER_NAMES[0]]
        )
    )
    for name in TIER_NAMES[1:]:
        in_tier = [f for f in specific if f["tier"] == name]
        if in_tier:
            console.print(_settings_table(f"{info['flow']} settings: {name}", in_tier))
    if common:
        console.print(_settings_table("Common settings (accepted by every flow)", common))
    _definitions_tables(info.get("definitions", {}), debug=bool(options and options.debug))


def discover_flow_class(flow: str) -> Type[Flow]:
    try:
        return get_flow_class(flow)
    except FlowNotFoundError:
        log.critical(
            "Flow %s is not known to Xeda. Please make sure the name is correctly specified.",
            flow,
        )
        sys.exit(1)


def select_design_in_project(
    xeda_project: XedaProject, design_name: str | None = None, target: str | None = None
) -> Optional[Design]:
    if console.is_interactive:
        terminal_menu = TerminalMenu(xeda_project.design_names, title="Please select a design: ")
        idx = terminal_menu.show()
        if idx is None or not isinstance(idx, int) or idx < 0:
            log.critical("Invalid design choice!")
            return None
        return xeda_project.get_design(idx, target)
    else:
        design_name = click.prompt(
            "Please enter design name: ",
            type=click.Choice(xeda_project.design_names),
        )
        if not design_name or design_name not in xeda_project.design_names:
            log.critical("Invalid design name!")
            return None
        return xeda_project.get_design(design_name, target)
