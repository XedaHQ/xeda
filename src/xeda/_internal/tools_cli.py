"""The `xeda tools` commands: show which tools the tool store holds and where, and install them.

`list` and `path` only read. They start no process, use no network and write nothing. `install`
is the only command that uses the network and writes the store, and it does so only when the user
runs it. The tool store holds the OSS CAD Suite (`oss-cad-suite`). A run puts the newest installed
suite on `PATH` by itself (see `xeda._internal.tool_store`).

Every command finds the store like a run does: `--root`, else `XEDA_TOOLS_ROOT` (the private
`env` first), else the data directory of the user. So every command reads the private settings.
`list` and `path` show the installation that a run would use: `tools: {oss-cad-suite: off}` or a
date there changes it.

The module imports only what `click` needs to declare the commands. The code that runs them is
imported when a command runs, so shell completion stays light.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

import click

from .cli_types import config_options, output_format_option, refuse_both_config_options

if TYPE_CHECKING:
    from .local_settings import LocalSettings
    from .tool_store import Installation

__all__ = ["add_tools_commands"]

#: The tools of the store, as `xeda._internal.tool_store.TOOLS` lists them. A test keeps both equal.
TOOL_NAMES = ("oss-cad-suite",)

ROOT_VARIABLE = "XEDA_TOOLS_ROOT"


def _root_option(func):
    return click.option(
        "--root",
        "root",
        type=click.Path(file_okay=False, path_type=Path),
        envvar=ROOT_VARIABLE,
        show_envvar=True,
        default=None,
        show_default=False,
        help="The tool store. The default is the data directory of the user: "
        "$XDG_DATA_HOME/xeda/tools on Linux, ~/Library/Application Support/xeda/tools on macOS.",
    )(func)


def _tool_argument(func):
    return click.argument("tool", type=click.Choice(TOOL_NAMES))(func)


def _fail(error: Exception, fmt: str) -> NoReturn:
    """Report a failure as every command does: a document with `success` false in a
    machine-readable format, else the message alone on stderr. Exit with status 1."""
    from .cli_utils import emit_structured, failure_document

    message = str(error)
    if fmt == "table":
        click.echo(message, err=True)
    else:
        emit_structured(failure_document(getattr(error, "code", "internal_error"), message), fmt)
    sys.exit(1)


def _private_settings(config: Optional[Path], no_config: bool) -> LocalSettings:
    """Read the private settings as `xeda run` does: the user file (`--config`, or none with
    `--no-config`) and the project-local file beside the project file of the current directory."""
    from .local_settings import local_settings
    from .project import Project
    from .types import UNSET
    from .xedaproject import resolve_project_file

    refuse_both_config_options(config, no_config)
    path = resolve_project_file(None)
    project = None if path is None else Project.load(path)
    return local_settings(project, config=None if no_config else config or UNSET)


def _store(root: Optional[Path], local: LocalSettings) -> Path:
    """Return the store of this command: `--root` on the command line, else `XEDA_TOOLS_ROOT` in
    the private `env` or the environment, else the default. A relative root of the variable is an
    error."""
    import os

    from xeda.errors import SettingsError

    from .tool_store import default_root

    context = click.get_current_context()
    if context.get_parameter_source("root") is click.core.ParameterSource.COMMANDLINE:
        assert root is not None
        return Path(os.path.abspath(root))
    try:
        return default_root({**os.environ, **local.env})
    except SettingsError as error:
        raise click.BadParameter(str(error), param_hint=ROOT_VARIABLE) from None


def _check_version(version: Optional[str], allow_latest: bool) -> None:
    from .tool_store import check_version

    if version is None:
        return
    if version == "latest" and not allow_latest:
        raise click.BadParameter("a date such as 2026-10-08", param_hint="--version")
    try:
        check_version(version)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--version") from None


def _description(installation: Installation) -> dict[str, Any]:
    """The fields that `path` and `install` give for an installation."""
    return {
        "tool": installation.tool,
        "version": installation.version,
        "platform": installation.platform,
        "root": str(installation.root),
        "directory": str(installation.directory),
        "bin": str(installation.bin),
        "receipt": dict(installation.receipt),
    }


# ---------------------------------------------------------------------------------------------
# xeda tools list
# ---------------------------------------------------------------------------------------------


def store_rows(root: Path, setting: Optional[str]) -> list[dict[str, Any]]:
    """One row for each installation of the store for this machine, oldest first, and one for the
    pin of the package if the store lacks it. `selected` marks the installation that a run with
    the selection `setting` uses."""
    from .tool_store import (
        TOOL,
        ToolNotInstalled,
        installation_directory,
        installed,
        pinned_version,
        platform_tag,
        select,
    )

    try:
        chosen = select(root, setting)
    except ToolNotInstalled:
        chosen = None
    found = installed(root)
    platform = platform_tag() or "unsupported"
    rows: list[dict[str, Any]] = []
    for installation in found:
        rows.append(
            {
                "tool": TOOL,
                "version": installation.version,
                "platform": platform,
                "installed": True,
                "selected": chosen is not None and chosen.version == installation.version,
                "directory": str(installation.directory),
                "bin": str(installation.bin),
                "receipt": dict(installation.receipt),
            }
        )
    pin = pinned_version()
    if all(row["version"] != pin for row in rows):
        directory = installation_directory(root, pin, platform) / TOOL
        rows.append(
            {
                "tool": TOOL,
                "version": pin,
                "platform": platform,
                "installed": False,
                "selected": False,
                "directory": str(directory),
                "bin": str(directory / "bin"),
                "receipt": None,
            }
        )
        rows.sort(key=lambda row: row["version"])
    return rows


@_root_option
@config_options
@output_format_option()
def tools_list(
    root: Optional[Path],
    config: Optional[Path],
    no_config: bool,
    output_format: str,
    json_flag: bool,
) -> None:
    """List the installations of the tool store for this machine, and the version that Xeda
    pins. A run puts the newest installed suite on PATH, unless the private settings select
    another date or turn the suite `off` (`tools:`). The `selected` column marks the suite that a
    run would use."""
    from rich.markup import escape

    from .cli_utils import emit_structured, info_table, resolve_format
    from .console import console
    from .tool_store import TOOL

    fmt = resolve_format(output_format, json_flag)
    local = _private_settings(config, no_config)
    store = _store(root, local)
    setting = local.tools.get(TOOL)
    rows = store_rows(store, setting)
    if fmt != "table":
        emit_structured(rows, fmt)
        return
    table = info_table(
        f"Tool store {store}",
        [
            ("Tool", {"header_style": "bold green", "style": "bold"}),
            ("Version", {}),
            ("Platform", {}),
            ("Installed", {}),
            ("Selected", {}),
            ("Directory", {}),
        ],
    )
    for row in rows:
        table.add_row(
            escape(row["tool"]),
            escape(row["version"]),
            escape(row["platform"]),
            "yes" if row["installed"] else "no",
            "yes" if row["selected"] else "no",
            escape(row["directory"]),
        )
    console.print(table)
    if setting == "off":
        console.print(f"{TOOL} is off in the private settings: no run uses a suite.")
    if not any(row["installed"] for row in rows):
        console.print(f"Install the suite with [bold]xeda tools install {TOOL}[/].")


# ---------------------------------------------------------------------------------------------
# xeda tools path
# ---------------------------------------------------------------------------------------------


@_tool_argument
@_root_option
@click.option(
    "--version",
    "version",
    default=None,
    help="The date of the build, such as 2026-10-08. The default is the installation that a run "
    "uses: the newest installed, or the date that the private settings select.",
)
@config_options
@output_format_option()
def tools_path(
    tool: str,
    root: Optional[Path],
    version: Optional[str],
    config: Optional[Path],
    no_config: bool,
    output_format: str,
    json_flag: bool,
) -> None:
    """Print the directory of an installed tool. In a machine-readable format, give the `bin`
    directory and the receipt too. If the suite is not installed, fail and say how to install it."""
    from .cli_utils import emit_structured, resolve_format
    from .tool_store import TOOL, ToolNotInstalled, find, select

    fmt = resolve_format(output_format, json_flag)
    _check_version(version, allow_latest=False)
    local = _private_settings(config, no_config)
    store = _store(root, local)
    setting = local.tools.get(TOOL)
    try:
        if version is not None:
            chosen = select(store, version)
        elif setting == "off":
            raise ToolNotInstalled(
                f"The private settings turn {TOOL} off (`tools:`), so no run uses a suite. "
                "Name an installation with --version DATE."
            )
        else:
            chosen = select(store, setting)
            if chosen is None:
                raise ToolNotInstalled(
                    f"{TOOL} is not installed in {store}. Install it with: "
                    f"xeda tools install {TOOL}"
                )
    except ToolNotInstalled as error:
        _fail(error, fmt)
    assert chosen is not None and find(store, chosen.version) is not None
    if fmt == "table":
        click.echo(str(chosen.directory))
    else:
        emit_structured(_description(chosen), fmt)


# ---------------------------------------------------------------------------------------------
# xeda tools install
# ---------------------------------------------------------------------------------------------


def _print_plan(plan: Any) -> None:
    size = "unknown size" if plan.asset.size is None else f"{plan.asset.size / 2**20:.0f} MiB"
    click.echo(f"Install {plan.asset.name[: -len('.tgz')]}.")
    click.echo(f"  version:    {plan.version}")
    click.echo(f"  platform:   {plan.platform}")
    click.echo(f"  download:   {size} from {plan.asset.url}")
    click.echo(f"  install to: {plan.location}")


@_tool_argument
@_root_option
@click.option(
    "--version",
    "version",
    default=None,
    help="The date of the build, such as 2026-10-08, or `latest`. The default is the build that "
    "Xeda pins.",
)
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Install without asking for confirmation.",
)
@config_options
@output_format_option()
def tools_install(
    tool: str,
    root: Optional[Path],
    version: Optional[str],
    yes: bool,
    config: Optional[Path],
    no_config: bool,
    output_format: str,
    json_flag: bool,
) -> None:
    """Install a tool into the tool store. This is the only command that uses the network and
    writes the store. It shows what it downloads and where it installs, and it asks for
    confirmation. `--yes` skips the question. `--json`, `--format json`, `--format jsonl` and
    `--format yaml` install without a question, because a program cannot answer. The download
    is checked against the SHA-256 digest that GitHub lists for it before the install reads it.
    If the store holds the build already, the command says so and downloads nothing. A failed
    install leaves the store as it was. Only one install at a time can use a store. The install
    refuses a symbolic link inside the store; the root itself may be one. A run uses the newest
    installed suite, so a new install is used by the next run."""
    from .cli_utils import emit_structured, machine_readable_mode, resolve_format
    from .tool_store import (
        TOOL,
        InstallResult,
        ToolInstallError,
        find,
        install,
        pinned_version,
        plan_install,
    )

    fmt = resolve_format(output_format, json_flag)
    if fmt != "table":
        machine_readable_mode()  # stdout belongs to the document; tool output goes to stderr
    _check_version(version, allow_latest=True)
    local = _private_settings(config, no_config)
    # The install resolves the root, so the plan shows the paths that the install writes.
    store = _store(root, local).resolve()
    wanted = version or pinned_version()
    result: InstallResult | None = None
    try:
        if wanted != "latest" and (found := find(store, wanted)) is not None:
            result = InstallResult(found, True)
        else:
            plan = plan_install(store, wanted)
            if (found := find(store, plan.version)) is not None:
                result = InstallResult(found, True)
            else:
                if fmt == "table":
                    _print_plan(plan)
                    if not yes and not click.confirm("Install now?", default=False):
                        click.echo("Nothing is installed.", err=True)
                        sys.exit(1)
                result = install(
                    plan,
                    progress=(lambda text: click.echo(text, err=True)) if fmt == "table" else None,
                )
    except ToolInstallError as error:
        _fail(error, fmt)
    installation = result.installation
    if fmt == "table":
        what = "is already installed in" if result.already_installed else "is installed in"
        click.echo(f"{TOOL} {installation.version} {what} {installation.directory}")
        click.echo(
            "Runs put the newest installed suite on PATH. To use this date always, write "
            f"`tools: {{{TOOL}: {installation.version}}}` in your private settings; "
            f"`{TOOL}: off` turns the suite off."
        )
        return
    emit_structured(
        {
            "success": True,
            **_description(installation),
            "already_installed": result.already_installed,
        },
        fmt,
    )


# ---------------------------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------------------------


def add_tools_commands(
    group: click.Group, command_class: type[click.Command], context_settings: dict[str, Any]
) -> None:
    """Add the commands of `xeda tools` to `group`. `command_class` is the class of every command
    of Xeda that reads no variable of the environment unless it declares it."""
    group.command(
        "list",
        cls=command_class,
        context_settings=context_settings,
        short_help="List the installed tools and the version that Xeda pins.",
    )(tools_list)
    group.command(
        "path",
        cls=command_class,
        context_settings=context_settings,
        short_help="Print the directory of an installed tool.",
    )(tools_path)
    group.command(
        "install",
        cls=command_class,
        context_settings=context_settings,
        short_help="Download a tool into the tool store.",
    )(tools_install)
