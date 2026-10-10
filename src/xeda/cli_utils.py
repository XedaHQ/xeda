"""Utilities for command line interface"""

import json
import logging
import sys
from contextlib import contextmanager
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    NoReturn,
    Optional,
    Sequence,
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
    ClickMutex as ClickMutex,
    DOCUMENT_FORMATS as DOCUMENT_FORMATS,
    FlowChoice as FlowChoice,
    OptionEatAll as OptionEatAll,
    OUTPUT_FORMATS as OUTPUT_FORMATS,
    output_format_option as output_format_option,
)
from .console import console, console_target, redirect_console, restore_console
from .design import Design
from .flow import Flow
from .flow_runner import FlowNotFoundError, XedaOptions, get_flow_class
from .introspect import settings_info, type_str
from .xedaproject import XedaProject

__all__ = [
    "ChainChoice",
    "HELP_FORMATTER_SETTINGS",
    "XEDA_HELP_THEME",
    "ClickMutex",
    "ConsoleLogo",
    "DeclaredEnvvarsCommand",
    "FlowChoice",
    "OptionEatAll",
    "XedaCommand",
    "XedaHelpGroup",
    "emit_structured",
    "machine_readable_mode",
    "output_format_option",
    "print_flow_settings",
    "requested_output_format",
    "reraise_suggesting_visible",
    "wants_machine_readable",
]

#: Help-screen palette: yellow headings and green options, to match the rich tables. click-extra
#: themes every element of the screen, so the rest of the built-in theme (metavars, choices,
#: envvars, defaults, ...) is inherited unchanged.
XEDA_HELP_THEME: HelpTheme = get_default_theme().with_(
    heading=HelpStyle(fg="yellow", bold=True),
    option=HelpStyle(fg="green", bold=True),
)

#: Belongs in `context_settings`, not on a command: cloup resolves the formatter from the
#: *context*, and a child context inherits its parent's settings, so the theme placed here reaches
#: every subcommand. A `formatter_settings=` passed to a command would style only that one screen.
HELP_FORMATTER_SETTINGS: Dict[str, Any] = HelpFormatter.settings(theme=XEDA_HELP_THEME)


log = logging.getLogger(__name__)


class ConsoleLogo:
    """Xeda ASCII logo on console"""

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
        """print the logo if console is wide enough"""
        if console.width >= 80:
            console.print(
                cls.logo.format(X="[dark_orange]", O="[/]"),
                highlight=False,
                emoji=False,
            )


def requested_output_format(argv: Sequence[str]) -> Optional[str]:
    """The machine-readable format this invocation asked for, or `None` for human output.

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
    """Whether this invocation asked for machine-readable output on stdout."""
    return requested_output_format(argv) is not None


@contextmanager
def _restoring_output_streams() -> Iterator[None]:
    """Undo any output redirection this invocation applied, however the invocation ends.

    `machine_readable_mode()` points the rich console and tool output at `sys.stderr`, and both
    are module-global. Without restoring them, a `--json` command leaves every later command in
    the same process writing to a stream that belonged to it -- which is exactly what happens
    under `click.testing.CliRunner` and when the CLI is driven as a library. A one-shot `xeda`
    process never noticed.
    """
    console_previous = console_target()
    tool_previous = proc_utils.tool_output_redirect()
    try:
        yield
    finally:
        restore_console(console_previous)
        proc_utils.set_tool_output(tool_previous)


class _DeclaredEnvvarsContext(ColorizedContext):
    """A context with no automatic environment variables: click would otherwise derive a prefix
    from the parent's (`XEDA_RUN_<OPTION>`) even when the command asks for none."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.auto_envvar_prefix = None


def reraise_suggesting_visible(
    command: click.Command, ctx: click.Context, error: click.NoSuchOption
) -> NoReturn:
    """Raise `error` again, with the close matches of the mistyped option taken from the options
    the help of `command` lists.

    click suggests from every option a command has. A hidden option exists only to say what
    replaced a removed one (`--xeda-run-dir`), so a suggestion must never lead to it. Every
    command and group of xeda calls this, so the rule holds for each of them.
    """
    if not error.possibilities:  # nothing was close, or a short option: nothing to leave out
        raise error
    # click suggests from the long names: a short one (`-s`) is not among them
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


class XedaCommand(ColorizedCommand):
    """The command class of xeda's own commands: it suggests only options its help lists."""

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        try:
            return super().parse_args(ctx, args)
        except click.NoSuchOption as error:
            reraise_suggesting_visible(self, ctx, error)


class DeclaredEnvvarsCommand(XedaCommand):
    """A command whose options read only the environment variables they declare (`envvar=`).

    With the group's `auto_envvar_prefix`, every option has a hidden second name: a leftover
    `export XEDA_CLEAN=1` would empty every run directory on every run, with nothing on the
    command line to turn it off. click-extra also registers the automatic names on the options
    themselves (to show them in the help), so both the command's settings and its context drop
    the prefix.
    """

    context_class = _DeclaredEnvvarsContext

    def __init__(self, *args, **kwargs) -> None:
        kwargs["context_settings"] = {
            **(kwargs.get("context_settings") or {}),
            "auto_envvar_prefix": None,
        }
        super().__init__(*args, **kwargs)


class XedaHelpGroup(ColorizedGroup):
    """How to display CLI help"""

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
        """Report argument errors in the format the invocation asked for.

        Click handles a `UsageError` itself: it prints to stderr and exits 2, which leaves a
        caller that asked for machine-readable output with empty stdout and nothing to parse.
        The error document is emitted in the requested format, so `--format yaml` gets YAML.
        Only the machine-readable path is intercepted; ordinary invocations are untouched.
        """
        # click-extra's auto-injected `help` subcommand renders its target through a plain
        # `click.Context`, which carries no `formatter_settings` and so falls back to the default
        # palette. Claiming the process-wide default here keeps `xeda help run` looking like
        # `xeda run --help`. Done in `main()` rather than at import so merely importing this
        # module never restyles another click-extra application's help screens.
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
        except click.ClickException as e:
            emit_structured(
                {
                    "success": False,
                    "error": {"type": type(e).__name__, "message": e.format_message()},
                },
                fmt,
            )
            sys.exit(e.exit_code)
        except click.exceptions.Abort:
            emit_structured(
                {"success": False, "error": {"type": "Abort", "message": "Aborted."}}, fmt
            )
            sys.exit(1)


def resolve_format(output_format: str, json_flag: bool) -> str:
    """`--json` is shorthand for `--format json`."""
    return "json" if json_flag else output_format


def emit_structured(data: Any, fmt: str, records: Optional[Sequence[Any]] = None) -> None:
    """Write `data` to stdout in `fmt`.

    Deliberately bypasses the rich console: nothing is wrapped, styled, or truncated to the
    terminal width, so identifiers stay complete when the output is piped.

    `records` supplies the one-object-per-line stream for `jsonl` when it differs from `data`
    (e.g. the fields of a settings document rather than the document itself).
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

    Rich output (tables, prompts, the logo) and every tool's stdout move to stderr, and child
    processes that would otherwise inherit our stdout are redirected too. Logging already goes
    to stderr.
    """
    redirect_console(sys.stderr)
    proc_utils.set_tool_output(sys.stderr)


def _fmt_default(value: Any, required: bool) -> str:
    """Render a default value for a table cell.

    Everything but the `<required>` marker is markup-escaped: a type or default containing
    square brackets (`array[string]`, `["a"]`) would otherwise be swallowed by rich as a style
    tag and silently disappear from the table.
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
    # `overflow="fold"` rather than rich's default ellipsis: a truncated setting name
    # ("set_synth_proper...") cannot be typed back into `-s KEY=VALUE`.
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
) -> None:
    """Render a flow's settings, either as tables or as a machine-readable document."""
    flow_class = discover_flow_class(flow) if isinstance(flow, str) else flow
    info = settings_info(flow_class)
    fields: List[Dict[str, Any]] = [
        f for f in info["fields"] if include_common or not f.get("common")
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
    console.print(_settings_table(f"{info['flow']} settings", specific))
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
