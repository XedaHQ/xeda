"""The `xeda pdk` commands: show which ASIC platforms are installed and where their files are,
and install them.

`list` and `path` only read. They start no process, use no network and write nothing, so a user
can run them at any time. `install` is the only command that uses the network and writes the
store, and it does so only when the user runs it. A platform is a bundled name or a platform file
of the user (`--platform-file`, as the setting `platform_file`). The PDK files are in a store at
`--root` (default `~/.ciel`, with `~` from `HOME`). See `xeda.platforms.providers` for the layout
of a store.

The module imports only what `click` needs to declare the commands. The code that runs them is
imported when a command runs, so shell completion stays light.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

import click

from .cli_types import output_format_option

if TYPE_CHECKING:
    from .platforms import Residue

__all__ = ["PROVIDER_NAMES", "add_pdk_commands"]

#: The providers, as `xeda.platforms.providers.PROVIDERS` lists them. A test keeps both equal.
PROVIDER_NAMES = ("orfs", "ciel", "directory")

#: How many digits of a commit the tables show. The JSON documents give it in full.
SHORT_VERSION = 12


def _root_option(func):
    return click.option(
        "--root",
        "root",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        show_default=False,
        help="The PDK store. The default is ~/.ciel.",
    )(func)


def _store_root(root: Optional[Path]) -> Path:
    from .platforms.providers import default_root

    return default_root() if root is None else root


def _short(version: str) -> str:
    return version[:SHORT_VERSION] if len(version) == 40 else version


def _fail(error_type: str, message: str, fmt: str) -> NoReturn:
    """Report a failure as every command does: a document with `success` false in a
    machine-readable format, else the message alone on stderr. Exit with status 1."""
    from .cli_utils import emit_structured

    if fmt == "table":
        click.echo(message, err=True)
    else:
        emit_structured({"success": False, "error": {"type": error_type, "message": message}}, fmt)
    sys.exit(1)


# ---------------------------------------------------------------------------------------------
# xeda pdk list
# ---------------------------------------------------------------------------------------------


def _other_orfs_versions(residue: Residue, root: Path) -> list[dict[str, Any]]:
    """The other commits that the store holds a directory of the platform for, sorted. Each has
    its `version`, its `directory` and `installed`: whether every file of the platform is there.
    A partial directory, which an interrupted install leaves, is not installed."""
    from .platforms import PdkNotInstalled, locate
    from .platforms.providers import orfs_checkout_directory
    from .platforms.residue import COMMIT

    entry = residue.providers.orfs
    assert entry is not None
    root = root.absolute()
    checkouts = orfs_checkout_directory(root, entry.version).parent
    try:
        names = sorted(path.name for path in checkouts.iterdir())
    except OSError:
        return []
    found = []
    for name in names:
        directory = orfs_checkout_directory(root, name) / entry.directory
        if name == entry.version or not COMMIT.fullmatch(name) or not directory.is_dir():
            continue
        try:
            locate(residue, "orfs", name, root, None)
        except PdkNotInstalled:
            installed = False
        else:
            installed = True
        found.append({"version": name, "directory": str(directory), "installed": installed})
    return found


def store_rows(root: Path) -> list[dict[str, Any]]:
    """One row for each provider of each bundled platform: whether the store at `root` holds the
    pinned version, and where. An `orfs` row also lists the other versions that the store holds."""
    from .introspect import provider_status
    from .platforms import bundled_platform_names, load_residue

    rows: list[dict[str, Any]] = []
    for name in bundled_platform_names():
        residue = load_residue(name)
        for provider in PROVIDER_NAMES:
            if residue.providers.entry(provider) is None:
                continue
            status = provider_status(residue, provider, root)
            row: dict[str, Any] = {
                "platform": name,
                "provider": provider,
                "default": provider == residue.providers.default,
                "version": status["version"],
                "installed": status["installed"],
                "directory": status["directory"],
            }
            if "problem" in status:
                row["problem"] = status["problem"]
            if provider == "orfs":
                row["other_versions"] = _other_orfs_versions(residue, root)
            rows.append(row)
    return rows


@output_format_option()
@_root_option
def pdk_list(root: Optional[Path], output_format: str, json_flag: bool) -> None:
    """List the platforms with each provider that serves them. For each, show the version that
    Xeda pins and whether the PDK store holds it. For a provider `orfs`, also list the other
    versions that the store holds, by path."""
    from rich.markup import escape

    from .cli_utils import emit_structured, info_table, resolve_format
    from .console import console

    fmt = resolve_format(output_format, json_flag)
    store = _store_root(root)
    rows = store_rows(store)
    if fmt != "table":
        emit_structured(rows, fmt)
        return
    table = info_table(
        f"PDK store {store}",
        [
            ("Platform", {"header_style": "bold green", "style": "bold"}),
            ("Provider", {}),
            ("Version", {}),
            ("Installed", {}),
            ("Directory", {}),
        ],
    )
    for row in rows:
        provider = row["provider"] + (" (default)" if row["default"] else "")
        table.add_row(
            escape(row["platform"]),
            escape(provider),
            escape(_short(row["version"])),
            "yes" if row["installed"] else "no",
            escape(row["directory"]),
        )
        for other in row.get("other_versions", []):
            table.add_row(
                "",
                "",
                escape(_short(other["version"])),
                "yes" if other["installed"] else "no",
                escape(other["directory"]),
            )
    console.print(table)
    for row in rows:
        if "problem" in row:
            console.print(escape(row["problem"]), highlight=False)


# ---------------------------------------------------------------------------------------------
# xeda pdk path
# ---------------------------------------------------------------------------------------------


def _bundled(name: str, hint: str) -> Residue:
    from .platforms import load_residue
    from .platforms.residue import ResidueError

    try:
        return load_residue(name)
    except ResidueError as error:
        raise click.BadParameter(str(error), param_hint=hint) from None


def resolve_platform(platform: Optional[str], platform_file: Optional[Path]) -> Residue:
    """Return the residue that finds the files of the platform: a bundled platform, a complete
    platform file, or a platform file over the bundled platform that it extends. Raise a
    `click.UsageError` if the command line names both a platform and a file, or neither, or names
    what cannot be loaded, or a file that is wrong when it is merged with the platform that it
    extends."""
    from .platforms import load_residue
    from .platforms.residue import ResidueError, effective_residue

    if (platform is None) == (platform_file is None):
        raise click.UsageError("name either a PLATFORM or --platform-file, not both and not none")
    if platform is not None:
        return _bundled(platform, "PLATFORM")
    assert platform_file is not None
    try:
        user = load_residue(platform_file)
    except ResidueError as error:
        raise click.BadParameter(str(error), param_hint="--platform-file") from None
    if user.extends is None:
        return user
    base = _bundled(user.extends, "--platform-file")
    try:
        return effective_residue(base, user)
    except (ResidueError, ValueError) as error:
        # The file loads alone and is wrong over the platform that it extends.
        raise click.BadParameter(str(error), param_hint="--platform-file") from None


@click.argument("platform", required=False)
@click.option(
    "--platform-file",
    "platform_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A platform file of your own, in place of PLATFORM.",
)
@_root_option
@click.option(
    "--version",
    "version",
    default=None,
    help="The version of the PDK files. The default is the version that Xeda pins.",
)
@click.option(
    "--provider",
    "provider",
    type=click.Choice(PROVIDER_NAMES),
    default=None,
    help="Where the PDK files come from. The default is that of the platform.",
)
@click.option(
    "--corner",
    "corner",
    default=None,
    help="The corner of the Liberty files. The default is that of the platform.",
)
@output_format_option()
def pdk_path(
    platform: Optional[str],
    platform_file: Optional[Path],
    root: Optional[Path],
    version: Optional[str],
    provider: Optional[str],
    corner: Optional[str],
    output_format: str,
    json_flag: bool,
) -> None:
    """Print the directory of a platform in the PDK store. In a machine-readable format, give the
    files of each role too. If the PDK files are not in the store, fail and say how to install
    them."""
    from dataclasses import asdict

    from .cli_utils import emit_structured, resolve_format
    from .introspect import json_safe
    from .platforms import PdkNotInstalled, locate
    from .platforms.providers import PdkVersionMismatch, verify_pin

    fmt = resolve_format(output_format, json_flag)
    residue = resolve_platform(platform, platform_file)
    try:
        located = locate(
            residue, provider, version, _store_root(root), corner, platform_file=platform_file
        )
        verify_pin(located, residue)
    except (PdkNotInstalled, PdkVersionMismatch) as error:
        _fail(type(error).__name__, str(error), fmt)
    except ValueError as error:
        raise click.UsageError(str(error)) from None
    if fmt == "table":
        click.echo(str(located.directory))
    else:
        emit_structured(json_safe({"platform": residue.name, **asdict(located)}), fmt)


# ---------------------------------------------------------------------------------------------
# xeda pdk install
# ---------------------------------------------------------------------------------------------


def _print_plan(plan: Any) -> None:
    import shlex

    size = "unknown size" if plan.download_mib is None else f"about {plan.download_mib:g} MiB"
    click.echo(f"Install platform '{plan.platform}' from provider {plan.provider}.")
    click.echo(f"  version:    {plan.version}")
    click.echo(f"  download:   {size}")
    click.echo(f"  install to: {plan.directory}")
    click.echo("  commands:")
    for command in plan.commands:
        click.echo(f"    {shlex.join(command)}")


@click.argument("platform", required=False)
@click.option(
    "--platform-file",
    "platform_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A platform file of your own, in place of PLATFORM.",
)
@_root_option
@click.option(
    "--version",
    "version",
    default=None,
    help="The version of the PDK files. The default is the version that Xeda pins.",
)
@click.option(
    "--provider",
    "provider",
    type=click.Choice(PROVIDER_NAMES),
    default=None,
    help="Where the PDK files come from. The default is that of the platform.",
)
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Install without asking for confirmation.",
)
@output_format_option()
def pdk_install(
    platform: Optional[str],
    platform_file: Optional[Path],
    root: Optional[Path],
    version: Optional[str],
    provider: Optional[str],
    yes: bool,
    output_format: str,
    json_flag: bool,
) -> None:
    """Install the PDK files of a platform into the PDK store. This is the only command that uses
    the network and writes the store. It shows what it downloads, where it installs and the
    commands, and it asks for confirmation. `--yes` skips the question. `--json`, `--format json`,
    `--format jsonl` and `--format yaml` install without a question, because a program cannot
    answer. If the store holds the files already, the command says so and runs nothing. A failed
    `orfs` install leaves the store as it was. A failed `ciel` install may leave files of `ciel`
    in the store. Only one install at a time can use a store. The install refuses a symbolic link
    inside the store; the root itself may be one."""
    from .cli_utils import emit_structured, machine_readable_mode, resolve_format
    from .platforms.providers import (
        PdkInstallBusy,
        PdkInstallerMissing,
        PdkInstallFailed,
        install,
        plan_install,
        store_holds,
    )

    fmt = resolve_format(output_format, json_flag)
    if fmt != "table":
        machine_readable_mode()  # stdout belongs to the document; tool output goes to stderr
    residue = resolve_platform(platform, platform_file)
    # The install resolves the root, so the plan shows the paths that the install writes.
    store = _store_root(root).resolve()
    try:
        plan = plan_install(residue, provider, version, store)
        present = store_holds(residue, plan, provider, version)
        if not present and fmt == "table":
            _print_plan(plan)
            if not yes and not click.confirm("Install now?", default=False):
                click.echo("Nothing is installed.", err=True)
                sys.exit(1)
        result = install(residue, provider, version, store)
    except (PdkInstallerMissing, PdkInstallFailed, PdkInstallBusy) as error:
        _fail(type(error).__name__, str(error), fmt)
    except ValueError as error:
        raise click.UsageError(str(error)) from None
    located = result.located
    if fmt == "table":
        what = "is already installed in" if result.already_installed else "is installed in"
        click.echo(f"Platform '{residue.name}' {what} {located.directory}")
        if result.receipt is not None:
            click.echo(f"Receipt: {result.receipt}")
        return
    emit_structured(
        {
            "success": True,
            "platform": residue.name,
            "provider": located.provider,
            "version": located.version,
            "root": str(located.root),
            "directory": str(located.directory),
            "already_installed": result.already_installed,
            "receipt": None if result.receipt is None else str(result.receipt),
            "commands": result.commands,
        },
        fmt,
    )


# ---------------------------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------------------------


def add_pdk_commands(
    group: click.Group, command_class: type[click.Command], context_settings: dict[str, Any]
) -> None:
    """Add the commands of `xeda pdk` to `group`. `command_class` is the class of every command
    of Xeda that reads no variable of the environment unless it declares it."""
    group.command(
        "list",
        cls=command_class,
        context_settings=context_settings,
        short_help="List the platforms and whether their PDK files are installed.",
    )(pdk_list)
    group.command(
        "path",
        cls=command_class,
        context_settings=context_settings,
        short_help="Print the directory of a platform in the PDK store.",
    )(pdk_path)
    group.command(
        "install",
        cls=command_class,
        context_settings=context_settings,
        short_help="Download the PDK files of a platform into the PDK store.",
    )(pdk_install)
