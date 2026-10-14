"""The `xeda pdk` commands: show which ASIC platforms are installed and where their files are.

The commands here only read. They start no process, use no network and write nothing, so a user
can run them at any time. A platform is a bundled name or a platform file of the user
(`--platform-file`, as the setting `platform_file`). The PDK files are in a store at `--root`
(default `~/.ciel`, with `~` from `HOME`). See `xeda.platforms.providers` for the layout of a
store.

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


def _other_orfs_versions(residue: Residue, root: Path) -> list[str]:
    """The platform directories of the other commits that the store holds, by path, sorted."""
    from .platforms.providers import orfs_checkout_directory
    from .platforms.residue import COMMIT

    entry = residue.providers.orfs
    assert entry is not None
    checkouts = orfs_checkout_directory(root.absolute(), entry.version).parent
    try:
        names = sorted(path.name for path in checkouts.iterdir())
    except OSError:
        return []
    found = []
    for name in names:
        if name == entry.version or not COMMIT.fullmatch(name):
            continue
        directory = orfs_checkout_directory(root.absolute(), name) / entry.directory
        if directory.is_dir():
            found.append(str(directory))
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
            table.add_row("", "", "other version", "yes", escape(other))
    console.print(table)


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
    what cannot be loaded."""
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
    return effective_residue(_bundled(user.extends, "--platform-file"), user)


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

    fmt = resolve_format(output_format, json_flag)
    residue = resolve_platform(platform, platform_file)
    try:
        located = locate(residue, provider, version, _store_root(root), corner)
    except PdkNotInstalled as error:
        _fail("PdkNotInstalled", str(error), fmt)
    except ValueError as error:
        raise click.UsageError(str(error)) from None
    if fmt == "table":
        click.echo(str(located.directory))
    else:
        emit_structured(json_safe({"platform": residue.name, **asdict(located)}), fmt)


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
