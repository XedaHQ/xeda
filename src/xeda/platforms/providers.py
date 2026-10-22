"""The PDK providers: find the files of a platform in a store, and plan how to install them.

A provider maps the role names of a residue (`liberty`, `tech_lef`, ...) to files by path
arithmetic on a store root and the pinned version. Finding the files uses `stat` only: it starts
no process, opens no connection, writes nothing and creates no directory. So planning and
`--dry-run` can run it. Only `install`, which the command `xeda pdk install` runs, uses the
network and writes the store.

Four providers serve a platform. A residue lists the ones that serve it in `providers`:

| provider | the directory of the platform | the version |
| --- | --- | --- |
| `orfs` | `<root>/xeda/orfs/<commit>/<directory>`, the platform directory of a sparse checkout of OpenROAD-flow-scripts | the commit |
| `lambdapdk` | `<root>/xeda/lambdapdk/<commit>/<directory>`, a directory of a sparse checkout of lambdapdk | the commit |
| `ciel` | `<root>/ciel/<family>/versions/<hash>/<variant>`, as `ciel fetch` writes it | the hash |
| `directory` | `<root>/<platform>` (layout `orfs`: `<root>` is `flow/platforms` of a checkout) or `<root>/<variant>` (layout `open_pdks`) | `pdk_version` as given, else `unpinned` |

The root is the setting `pdk_root`, and the default is `~/.ciel`. No code here reads the
environment except `HOME`, through `Path.home()`, and `install`, which gives `PATH`, `HOME` and
`LANG` to `ciel` and nothing else.
"""

import json
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from xeda.flow import FlowFatalError
from xeda.platforms.model import Located
from xeda.platforms.residue import COMMIT, LambdapdkSource, Residue
from xeda.proc_utils import run_process
from xeda.utils import NonZeroExitCode, replacing_file
from xeda.version import __version__

if sys.platform != "win32":
    import fcntl

__all__ = [
    "CIEL_VERSION",
    "GIT_PROVIDERS",
    "LAMBDAPDK_URL",
    "ORFS_URL",
    "PROVIDERS",
    "UNPINNED",
    "InstallPlan",
    "InstallResult",
    "PdkInstallBusy",
    "PdkInstallFailed",
    "PdkInstallerMissing",
    "PdkNotInstalled",
    "PdkVersionMismatch",
    "default_root",
    "install",
    "installed",
    "install_plan",
    "plan_install",
    "store_holds",
    "locate",
    "git_checkout_directory",
    "orfs_checkout_directory",
    "resolve_corner",
    "platform_directory",
    "verify_pin",
]

#: The providers, in the order that messages list them.
PROVIDERS = ("orfs", "ciel", "lambdapdk", "directory")
#: The providers that a sparse Git checkout of one commit serves.
GIT_PROVIDERS = ("orfs", "lambdapdk")
#: The version of a `directory` that the user gives no pin for.
UNPINNED = "unpinned"
#: The version of `ciel` that the installer runs. It has nothing to do with the pin of any PDK.
CIEL_VERSION = "3.0.1"
#: The repository that the `orfs` provider fetches.
ORFS_URL = "https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts"
#: The repository that the `lambdapdk` provider fetches.
LAMBDAPDK_URL = "https://github.com/siliconcompiler/lambdapdk"

log = logging.getLogger(__name__)

SHORT_HASH = 12
CHOOSE_ANOTHER = "or name another location: -s pdk_root=<directory>"


class PdkNotInstalled(FlowFatalError):
    """The files of a platform are not in the store. The message has four lines: what the
    platform needs, where xeda looked and what is missing, the command that installs the files,
    and how to name another location."""


class PdkVersionMismatch(FlowFatalError):
    """A directory holds another version of the PDK than the one that the user pinned."""


class PdkInstallerMissing(FlowFatalError):
    """No program is available that can install the PDK files."""


class PdkInstallFailed(FlowFatalError):
    """The install did not finish. The message says which step failed. What the install had
    written into the store, other than its lock file, is removed."""


class PdkInstallBusy(FlowFatalError):
    """Another install holds the lock of the store."""


def default_root() -> Path:
    """Return the default root of a PDK store: `~/.ciel`, with `~` from `HOME`."""
    return Path.home() / ".ciel"


def git_checkout_directory(root: Path, provider: str, commit: str) -> Path:
    """Return the directory that holds the platform directories of a commit of a Git provider."""
    return root / "xeda" / provider / commit


def orfs_checkout_directory(root: Path, commit: str) -> Path:
    """Return the directory that holds the platform directories of an ORFS commit."""
    return git_checkout_directory(root, "orfs", commit)


def git_url(provider: str) -> str:
    """Return the repository that a Git provider fetches (read when called, so a test can patch
    the constants)."""
    return {"orfs": ORFS_URL, "lambdapdk": LAMBDAPDK_URL}[provider]


# ---------------------------------------------------------------------------------------------
# Where a platform is
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """What a provider looks for: the entry of the residue, the pin and the directory."""

    provider: str
    entry: Any
    version: str
    root: Path
    directory: Path

    @property
    def pin(self) -> Optional[str]:
        """The version that the residue pins, or `None` for a `directory`."""
        return None if self.provider == "directory" else str(self.entry.version)


def _provider_name(residue: Residue, provider: Optional[str]) -> str:
    if provider is None:
        provider = residue.providers.default
        if provider is None:
            raise ValueError(
                f"platform '{residue.name}' names no default provider: name one of "
                f"{', '.join(_serving(residue))}"
            )
    if provider not in PROVIDERS:
        raise ValueError(
            f"unknown provider '{provider}'; the providers are: {', '.join(PROVIDERS)}"
        )
    if residue.providers.entry(provider) is None:
        raise ValueError(
            f"platform '{residue.name}' is not served by provider '{provider}'; "
            f"it serves: {', '.join(_serving(residue)) or 'nothing'}"
        )
    return provider


def _serving(residue: Residue) -> list[str]:
    return [name for name in PROVIDERS if residue.providers.entry(name) is not None]


def _viewed(residue: Residue, provider: Optional[str]) -> Residue:
    """Return the residue as the provider sees it (`Residue.for_provider`). Raise `ValueError`
    for a provider that does not serve the platform."""
    return residue.for_provider(_provider_name(residue, provider))


def resolve_corner(residue: Residue, provider: Optional[str], corner: Optional[str]) -> str:
    """Return the token of the corner as the provider sees the platform. Raise `ValueError` for a
    corner that the provider does not have; for a provider with a layout of its own, the message
    says whose corners it names."""
    name = _provider_name(residue, provider)
    try:
        return residue.for_provider(name).resolve_corner(corner)
    except ValueError as error:
        if not isinstance(residue.providers.entry(name), LambdapdkSource):
            raise
        raise ValueError(f"{error}. These are the corners of provider '{name}'") from None


def _target(
    residue: Residue, provider: Optional[str], version: Optional[str], root: Path
) -> _Target:
    name = _provider_name(residue, provider)
    entry = residue.providers.entry(name)
    root = root.absolute()
    if name == "directory":
        pinned = UNPINNED if version is None else version
    else:
        pinned = entry.version if version is None else version
        if not COMMIT.fullmatch(pinned):
            raise ValueError(
                f"`{pinned}` is not a full commit hash (40 lowercase hexadecimal digits): "
                f"the version of provider '{name}' names a directory of the store"
            )
    if name in GIT_PROVIDERS:
        directory = git_checkout_directory(root, name, pinned) / entry.directory
    elif name == "ciel":
        directory = root / "ciel" / entry.family / "versions" / pinned / entry.variant
    elif entry.layout == "orfs":
        directory = root / residue.name
    else:
        directory = root / entry.variant
    return _Target(name, entry, pinned, root, directory)


def platform_directory(
    residue: Residue, provider: Optional[str], version: Optional[str], root: Path
) -> Path:
    """Return the directory of the platform in the store, without looking at the file system.
    `provider` `None` is the default provider of the residue, and `version` `None` is the pin.
    Raise `ValueError` for a provider that does not serve the platform."""
    return _target(residue, provider, version, root).directory


# ---------------------------------------------------------------------------------------------
# Finding the files
# ---------------------------------------------------------------------------------------------


def _relative_files(residue: Residue, corner: str) -> list[str]:
    """Every file that the platform needs, relative to its directory, once, in a fixed order:
    the role files, then the upstream configuration."""
    paths = [path for files in residue.files.role_paths(corner).values() for path in files]
    if residue.upstream is not None:
        paths.extend(residue.upstream.files)
    return list(dict.fromkeys(paths))


def _missing_files(residue: Residue, corner: str, directory: Path) -> list[str]:
    """The files of the platform for the corner that `directory` lacks, relative to it."""
    return [path for path in _relative_files(residue, corner) if not (directory / path).is_file()]


def _first_line(residue: Residue, target: _Target) -> str:
    entry = target.entry
    if target.provider in GIT_PROVIDERS:
        what = entry.directory
        source = f"{target.provider} {target.version[:SHORT_HASH]}"
    elif target.provider == "ciel":
        what, source = entry.library, f"ciel {target.version[:SHORT_HASH]}"
    else:
        what, source = entry.layout, f"directory, {target.version}"
    return f"platform '{residue.name}' needs the PDK files of {what} ({source})."


def _install_line(residue: Residue, target: _Target, platform_file: Optional[Path]) -> str:
    if target.provider == "directory":
        return (
            "none; the directory must hold the files of the platform in the "
            f"{target.entry.layout} layout"
        )
    command = ["xeda", "pdk", "install"]
    if platform_file is None:
        command.append(residue.name)
    else:
        command += ["--platform-file", str(platform_file.resolve())]
    if target.provider != residue.providers.default:
        command += ["--provider", target.provider]
    if target.root != default_root():
        command += ["--root", str(target.root)]
    if target.version != target.pin:
        command += ["--version", target.version]
    return shlex.join(command)


def _notinstalled(
    residue: Residue, target: _Target, missing: list[str], platform_file: Optional[Path]
) -> PdkNotInstalled:
    more = f", and {len(missing) - 1} more" if len(missing) > 1 else ""
    return PdkNotInstalled(
        "\n".join(
            [
                _first_line(residue, target),
                f"  looked in: {target.directory} (missing: {missing[0]}{more})",
                f"  install:   {_install_line(residue, target, platform_file)}",
                f"  {CHOOSE_ANOTHER}",
            ]
        )
    )


def locate(
    residue: Residue,
    provider: Optional[str],
    version: Optional[str],
    root: Path,
    corner: Optional[str],
    *,
    platform_file: Optional[Path] = None,
) -> Located:
    """Find the files of the platform in the store at `root`.

    `provider` `None` is the default provider of the residue. `version` `None` is the pin of
    the residue (`orfs`, `ciel`, `lambdapdk`) or `unpinned` (`directory`). `corner` goes through the corner
    resolution of the residue, and `Located.files["liberty"]` holds the files of that corner.
    `platform_file` is the user file that the residue came from, if it came from one. It only
    changes the install command in the refusal, which then names the file and not the platform.

    The function checks with `stat` that every file exists and raises `PdkNotInstalled` if one is
    missing. It does not read the version marker of a `directory` (see `verify_pin`). It starts
    no process, uses no network, writes nothing and creates no directory. A relative `root` is
    made absolute with the current directory.

    Raise `ValueError` for a provider that does not serve the platform, a version that is no
    commit hash (`orfs`, `ciel`, `lambdapdk`) and an unknown corner."""
    token = resolve_corner(residue, provider, corner)
    residue = _viewed(residue, provider)
    target = _target(residue, provider, version, root)
    missing = _missing_files(residue, token, target.directory)
    if missing:
        raise _notinstalled(residue, target, missing, platform_file)
    files = {
        role: [target.directory / path for path in paths]
        for role, paths in residue.files.role_paths(token).items()
    }
    # The files of xeda are in the package and not in the store. A role of the layout wins.
    files.update(
        {role: paths for role, paths in residue.shipped_paths().items() if role not in files}
    )
    upstream = [] if residue.upstream is None else residue.upstream.files
    return Located(
        provider=target.provider,
        version=target.version,
        root=target.root,
        directory=target.directory,
        corner=token,
        files=files,
        upstream_files=[target.directory / path for path in upstream],
        library=getattr(target.entry, "library", None),
    )


# ---------------------------------------------------------------------------------------------
# The version marker
# ---------------------------------------------------------------------------------------------


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


#: The source that names the version of a PDK build: its `SOURCES` line and its `nodeinfo.json`
#: key. The other sources of a build (`magic`, ...) are other programs, with other versions.
PDK_SOURCE = "open_pdks"


def _sources(path: Path) -> list[str]:
    """The hash of the `open_pdks` line of a `SOURCES` file, whose lines are `<name> <hash>`.
    The list is empty if the file has no such line."""
    text = _read(path)
    if text is None:
        return []
    lines = (line.split() for line in text.splitlines())
    return [parts[1] for parts in lines if len(parts) == 2 and parts[0] == PDK_SOURCE]


def _nodeinfo(path: Path) -> list[str]:
    """The commit of `open_pdks` in a `nodeinfo.json` file: the key `commit.open_pdks`. The list
    is empty if the file has no such text."""
    text = _read(path)
    if text is None:
        return []
    try:
        commits = json.loads(text).get("commit")
        commit = commits.get(PDK_SOURCE)
    except (ValueError, AttributeError):
        return []
    return [commit] if isinstance(commit, str) else []


def _commit_of_head(git_dir: Path) -> tuple[Path, list[str]]:
    """The file that names the commit of a checkout, and the commit. `HEAD` holds a commit, or
    names a reference: a file under `refs/`, or else a line of `packed-refs`."""
    head = git_dir / "HEAD"
    text = (_read(head) or "").strip()
    if not text.startswith("ref:"):
        return head, [text] if COMMIT.fullmatch(text) else []
    reference = text.removeprefix("ref:").strip()
    ref_file = git_dir / reference
    commit = (_read(ref_file) or "").strip()
    if COMMIT.fullmatch(commit):
        return ref_file, [commit]
    packed = git_dir / "packed-refs"
    for line in (_read(packed) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == reference and COMMIT.fullmatch(parts[0]):
            return packed, [parts[0]]
    return head, []


def _marker(located: Located, residue: Residue) -> tuple[Path, list[str]]:
    """The file that says which version a directory holds, and the versions it names."""
    directory = located.directory
    if located.provider == "ciel":
        return directory / "SOURCES", _sources(directory / "SOURCES")
    if located.provider == "directory":
        entry = residue.providers.directory
        if entry is not None and entry.layout == "orfs":
            # `root` is `flow/platforms` of a checkout: two levels below its top.
            git_dir = located.root.parent.parent / ".git"
            return _commit_of_head(git_dir) if git_dir.is_dir() else (git_dir, [])
        sources = _sources(directory / "SOURCES")
        if sources:
            return directory / "SOURCES", sources
        nodeinfo = directory / ".config" / "nodeinfo.json"
        return nodeinfo, _nodeinfo(nodeinfo)
    return directory, []


def verify_pin(located: Located, residue: Residue) -> None:
    """Check that the directory holds the version that was pinned, and raise
    `PdkVersionMismatch` naming both if it does not.

    The check reads the version marker of the layout, a few small files, and nothing else:

    * `directory`, layout `orfs`: the `.git/HEAD` of the checkout above the root, and the
      reference file that it names (or `packed-refs`);
    * `directory`, layout `open_pdks`: the `open_pdks` line of `<variant>/SOURCES` (lines
      `<name> <hash>`), or else `commit.open_pdks` of `<variant>/.config/nodeinfo.json`;
    * `ciel`: the `open_pdks` line of `<variant>/SOURCES` when it exists;
    * `orfs`, `lambdapdk`: the pin is the name of the directory, so there is nothing to read.

    The pin must equal the version that the marker names, as exact text. A hash of another
    source in the marker (`magic`, ...) does not count. The pin `unpinned` checks nothing. A
    marker that is missing, or that has no entry for the PDK, is no error: the content digests
    of the files, which the trace records, still catch a change."""
    if located.version == UNPINNED:
        return
    marker, found = _marker(located, residue)
    if not found or located.version in found:
        return
    raise PdkVersionMismatch(
        f"platform '{residue.name}' needs PDK version {located.version}, "
        f"but {marker} names {', '.join(dict.fromkeys(found))}.\n"
        f"  change the pin pdk_version, or {CHOOSE_ANOTHER}"
    )


# ---------------------------------------------------------------------------------------------
# The install plan
# ---------------------------------------------------------------------------------------------


def _ciel_plan(residue: Residue, target: _Target) -> list[list[str]]:
    entry = target.entry
    fetch = [
        "ciel",
        "fetch",
        "--pdk-root",
        str(target.root),
        "--pdk-family",
        entry.family,
        "-l",
        entry.library,
        target.version,
    ]
    pinned = f"ciel=={CIEL_VERSION}"
    if shutil.which("uv"):
        return [["uv", "tool", "run", "--from", pinned, *fetch]]
    if shutil.which("pipx"):
        return [["pipx", "run", "--spec", pinned, *fetch]]
    if shutil.which("ciel"):
        return [fetch]
    raise PdkInstallerMissing(
        f"cannot install the PDK files of platform '{residue.name}': no program can run ciel. "
        f"Install uv or pipx, which run {pinned} in their own environment, "
        f"or install {pinned} so that `ciel` is on PATH."
    )


def _git_plan(target: _Target, partial: str, url: str) -> list[list[str]]:
    work = str(target.root / "xeda" / f".partial-{partial}")
    git = ["git", "-C", work]
    entry = target.entry
    # The paths are relative to the top of the repository, where the Git commands run.
    paths = [f"{entry.directory}/{item}" for item in entry.fetch] or [entry.directory]
    return [
        ["git", "init", "--quiet", work],
        [*git, "remote", "add", "origin", url],
        [*git, "sparse-checkout", "set", "--cone", *paths],
        [*git, "fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", target.version],
        [*git, "checkout", "--quiet", "FETCH_HEAD"],
    ]


def install_plan(
    residue: Residue,
    provider: Optional[str],
    version: Optional[str],
    root: Path,
    *,
    partial: Optional[str] = None,
    url: Optional[str] = None,
) -> list[list[str]]:
    """Return the argument vectors that install the files of the platform, in the order to run.
    Nothing here starts a process or writes.

    * `ciel`: one vector. The first of `uv tool run`, `pipx run` and a `ciel` on `PATH` that
      `shutil.which` finds. Whether that `ciel` is version 3 is for the installer to probe.
      Raise `PdkInstallerMissing` if there is none.
    * `orfs`, `lambdapdk`: the Git commands that fetch the platform directory of the commit into
      `<root>/xeda/.partial-<partial>`. The first command creates that directory. `partial` is
      required, and the installer chooses it, so that the plan is deterministic. `url` is the
      repository to fetch from, and the default is that of the provider. After the last command, the
      installer moves the platform directory of the partial checkout (`directory` of the
      entry) to the same path below `git_checkout_directory(root, provider, commit)`. An entry
      with `fetch` fetches only those paths below the directory.
    * `directory`: nothing to install. Raise `ValueError`."""
    target = _target(_viewed(residue, provider), provider, version, root)
    if target.provider == "directory":
        raise ValueError(
            f"the directory provider has nothing to install: the directory must hold the files "
            f"of platform '{residue.name}'"
        )
    if target.provider == "ciel":
        return _ciel_plan(residue, target)
    if partial is None:
        raise ValueError(
            f"the plan of provider '{target.provider}' needs the name of its partial directory"
        )
    return _git_plan(target, partial, git_url(target.provider) if url is None else url)


# ---------------------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------------------

#: The variables of the environment that `ciel` gets. Others, such as `PDK_ROOT` and
#: `GITHUB_TOKEN`, change where it installs or how it reaches the network.
CIEL_ENVIRONMENT = ("PATH", "HOME", "LANG")
#: The settings of every `git` command of the install that stop its background work.
GIT_QUIET_SETTINGS = ("maintenance.auto=false", "gc.auto=0", "fetch.writeCommitGraph=false")
#: The variables of the environment that `git` gets besides its own settings: the proxies.
GIT_PASSED_ENVIRONMENT = (
    *CIEL_ENVIRONMENT,
    *(
        name if upper else name.lower()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")
        for upper in (True, False)
    ),
)


@dataclass(frozen=True)
class InstallPlan:
    """What an install will do, before it does anything."""

    platform: str
    provider: str
    #: The pin: a commit (`orfs`, `lambdapdk`) or a hash (`ciel`).
    version: str
    root: Path
    #: The directory of the platform when the install is done.
    directory: Path
    #: The estimated download in MiB, if the residue says.
    download_mib: Optional[float]
    #: The argument vectors, in the order to run.
    commands: list[list[str]]


@dataclass(frozen=True)
class InstallResult:
    """What an install did."""

    #: Where the files are, for the default corner.
    located: Located
    #: Whether every file was in the store already, so that nothing ran.
    already_installed: bool
    #: The receipt file, or `None` if there is none (a store that nobody installed into).
    receipt: Optional[Path]
    #: The argument vectors that ran. Empty if `already_installed`.
    commands: list[list[str]]


def plan_install(
    residue: Residue,
    provider: Optional[str],
    version: Optional[str],
    root: Path,
    *,
    partial: str = "<random>",
    url: Optional[str] = None,
) -> InstallPlan:
    """Return what `install` would do with the same arguments, without a file system access
    beyond the search for the programs that install. `partial` stands for the random name of the
    partial directory of a Git provider. Raise what `install_plan` raises."""
    target = _target(_viewed(residue, provider), provider, version, root)
    commands = install_plan(residue, provider, version, root, partial=partial, url=url)
    return InstallPlan(
        platform=residue.name,
        provider=target.provider,
        version=target.version,
        root=target.root,
        directory=target.directory,
        download_mib=getattr(target.entry, "download_mib", None),
        commands=commands,
    )


def _locate_everywhere(
    residue: Residue, provider: Optional[str], version: Optional[str], root: Path
) -> Located:
    """Locate the platform for every corner of the residue, and return the default corner.
    Raise `PdkNotInstalled` for the first corner that lacks a file."""
    view = _viewed(residue, provider)
    corners = list(view.files.liberty)
    found = [locate(residue, provider, version, root, corner) for corner in corners]
    default = view.resolve_corner(None)
    return next((located for located in found if located.corner == default), found[0])


def installed(
    residue: Residue, provider: Optional[str], version: Optional[str], root: Path
) -> Optional[Located]:
    """Return the platform if the store holds it at the pinned version, else `None`.

    Every file of every corner must be there, and the pin must verify. For `ciel`, the receipt
    must exist too. `ciel` unpacks straight into the store, so a `ciel` that failed or was killed
    can leave a truncated file under the right name. The receipt is written only after an install
    that finished and checked its files."""
    try:
        located = _locate_everywhere(residue, provider, version, root)
        verify_pin(located, residue)
    except (PdkNotInstalled, PdkVersionMismatch):
        return None
    if located.provider == "ciel":
        if not _receipt_path(located.root, residue.name, located.version).is_file():
            return None
    return located


def store_holds(
    residue: Residue, plan: InstallPlan, provider: Optional[str], version: Optional[str]
) -> bool:
    """Return whether the store holds the platform already, as `install` judges it. The check
    for links in the store comes first, so nothing looks into the store through a link. Raise
    `PdkInstallFailed` for a link in the store, and for a file system error, which names the
    path. `plan` is the plan of an install with a resolved root."""
    try:
        _refuse_links(plan, *_written_paths(plan))
        return installed(residue, provider, version, plan.root) is not None
    except OSError as error:
        raise PdkInstallFailed(f"cannot install platform '{plan.platform}': {error}") from None


def _already_installed(
    residue: Residue, plan: InstallPlan, provider: Optional[str], version: Optional[str]
) -> Optional[InstallResult]:
    located = installed(residue, provider, version, plan.root)
    if located is None:
        return None
    receipt = _receipt_path(plan.root, plan.platform, plan.version)
    return InstallResult(located, True, receipt if receipt.is_file() else None, [])


def _receipt_path(root: Path, platform: str, version: str) -> Path:
    return root / "xeda" / "receipts" / f"{platform}-{version}.json"


def _git_environment() -> dict[str, str]:
    """The environment of a `git` command: the variables that it needs to run and to reach the
    network, and settings that stop it from reading the configuration of the user or asking
    questions. `GIT_DIR`, `GIT_WORK_TREE` and the like are left out, so that `git` works only in
    the directory that the command names."""
    env = {name: os.environ[name] for name in GIT_PASSED_ENVIRONMENT if name in os.environ}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(command: list[str], env: Optional[dict[str, str]], plan: InstallPlan) -> None:
    if command[0] == "git":
        # No hook runs, whatever `git` reads, and no maintenance process starts in the
        # background: it could outlive the install and write into a directory that the install
        # moves or removes.
        settings = [f"core.hooksPath={os.devnull}", *GIT_QUIET_SETTINGS]
        command = [command[0], *(w for s in settings for w in ("-c", s)), *command[1:]]
        env = _git_environment()
    try:
        run_process(command[0], command[1:], env=env)
    except NonZeroExitCode as error:
        output = "".join(f"\n{line}" for line in error.args)
        raise PdkInstallFailed(
            f"cannot install platform '{plan.platform}': `{shlex.join(command)}` ended with "
            f"status {error.exit_code}. Nothing is installed.{output}"
        ) from None
    except OSError as error:
        raise PdkInstallFailed(
            f"cannot install platform '{plan.platform}': cannot run `{command[0]}` ({error}). "
            "Nothing is installed."
        ) from None


def _link_on_the_way(root: Path, path: Path) -> Optional[Path]:
    """Return the first symbolic link on the way from `root` down to `path`, or `None`. `root`
    itself is not looked at: the install resolves it first. The walk uses `lstat`, so it never
    follows a link, and it ends at the first name that does not exist."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISLNK(mode):
            return current
    return None


def _refuse_links(plan: "InstallPlan", *paths: Path) -> None:
    """Raise `PdkInstallFailed` if a symbolic link is on the way from the root of the store down
    to one of `paths`. The install writes only real directories inside the store, so that it
    never writes or deletes outside it."""
    for path in paths:
        link = _link_on_the_way(plan.root, path)
        if link is not None:
            raise PdkInstallFailed(
                f"cannot install platform '{plan.platform}': {link} is a symbolic link "
                f"(to {os.readlink(link)}). The install writes only real directories inside the "
                "store, so that it never writes or deletes outside it. Replace the link with a "
                "real directory, and run the command again."
            )


def _written_paths(plan: "InstallPlan") -> list[Path]:
    """The paths below the root of the store that the install writes, publishes or locks. A
    path stands for each directory above it too. The platform directory of provider `ciel` is
    not one: `ciel` writes it."""
    paths = [plan.root / "xeda" / ".lock", _receipt_path(plan.root, plan.platform, plan.version)]
    if plan.provider in GIT_PROVIDERS:
        paths += [plan.directory, Path(plan.commands[0][-1])]
    return paths


def _remove_tree(root: Path, path: Path) -> None:
    """Remove a directory that an install made, unless a link is on the way to it. A failure is
    only logged: it must not hide the error that made the install remove it."""
    link = _link_on_the_way(root, path)
    if link is not None:
        log.warning("not removing %s: %s is a symbolic link; check it", path, link)
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("cannot remove %s: %s; remove it", path, error)


def _make_parents(directory: Path, made: list[Path]) -> None:
    """Create `directory` and its parents one level at a time, topmost first. Each directory
    that this call creates goes into `made` as soon as it exists, so that the caller can remove
    exactly these directories, also after a failure part way."""
    for path in (*reversed(directory.parents), directory):
        if not path.exists():
            path.mkdir()
            made.append(path)


def _remove_empty(root: Path, directories: list[Path]) -> None:
    """Remove directories that an install made and that are still empty, the deepest first.
    Stop at a link on the way to one."""
    for directory in reversed(directories):
        if _link_on_the_way(root, directory) is not None:
            break
        try:
            directory.rmdir()
        except OSError:
            break


def _read_receipt(path: Path) -> Optional[bytes]:
    """The bytes of a receipt that an earlier install wrote, or `None` if there is none."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_receipt(root: Path, path: Path, earlier: Optional[bytes]) -> None:
    """Put the receipt back as it was before this attempt: the bytes of the earlier receipt, or
    no file if there was none. Do nothing if a link is on the way to it."""
    link = _link_on_the_way(root, path)
    if link is not None:
        log.warning("not restoring %s: %s is a symbolic link; check it", path, link)
        return
    try:
        if earlier is None:
            path.unlink(missing_ok=True)
        else:
            with replacing_file(path, "wb") as out:
                out.write(earlier)
    except OSError as error:
        log.warning("cannot restore %s: %s; check it", path, error)


def _install_git(residue: Residue, plan: InstallPlan) -> Path:
    """Run the Git commands in a partial directory, check that it holds every file of the
    platform for every corner, write the receipt, and move the directory of the platform to its
    place in the store. The move is the last step, and a rename is whole or not done: the store
    holds a directory of the platform only after an install that finished. Several platforms of
    one commit share `<root>/xeda/<provider>/<commit>/`, and each install adds only its own
    directory.
    The partial directory, with its `.git`, is removed at the end. Whatever goes wrong, the store
    keeps neither the partial directory, nor a new receipt (an earlier receipt is put back), nor
    the directory of the platform.
    Return the receipt."""
    entry = residue.providers.entry(plan.provider)
    assert entry is not None
    residue = residue.for_provider(plan.provider)
    if os.path.lexists(plan.directory):
        raise PdkInstallFailed(
            f"cannot install platform '{plan.platform}': {plan.directory} exists, but it lacks "
            "files of the platform. Move or delete it, and run the command again."
        )
    work = Path(plan.commands[0][-1])
    source = work / entry.directory
    try:
        for command in plan.commands:
            _run(command, None, plan)
        for corner in residue.files.liberty:
            missing = _missing_files(
                residue, residue.resolve_corner(corner), work / entry.directory
            )
            if missing:
                raise PdkInstallFailed(
                    f"cannot install platform '{plan.platform}': commit {plan.version} lacks "
                    f"{', '.join(missing)} in {entry.directory}. Nothing is installed."
                )
        receipt = _receipt_path(plan.root, plan.platform, plan.version)
        earlier = _read_receipt(receipt)
        made: list[Path] = []
        try:
            # The store can change while `git` runs: look again before each step.
            _refuse_links(plan, receipt, plan.directory, source)
            _write_receipt(plan, plan.directory)
            _refuse_links(plan, plan.directory, source)
            _make_parents(plan.directory.parent, made)
            _refuse_links(plan, plan.directory, source)
            source.rename(plan.directory)
        except BaseException:
            _restore_receipt(plan.root, receipt, earlier)
            _remove_empty(plan.root, made)
            raise
        return receipt
    finally:
        _remove_tree(plan.root, work)


def _check_ciel(plan: InstallPlan, env: dict[str, str]) -> None:
    """Check that the `ciel` of `PATH` is the version 3 that the plan was made for."""
    try:
        said = run_process("ciel", ["--version"], env=env, stdout=True, check=False)
    except OSError as error:
        raise PdkInstallFailed(f"cannot run `ciel --version` ({error})") from None
    match = re.search(r"\b(\d+)\.\d+(?:\.\d+)?\b", said or "")
    major = CIEL_VERSION.split(".")[0]
    if match is None or match.group(1) != major:
        raise PdkInstallFailed(
            f"cannot install platform '{plan.platform}': the `ciel` on PATH says "
            f"`{(said or '').strip() or 'nothing'}`, and Xeda needs ciel {major}.x. "
            f"Install uv or pipx, which run ciel=={CIEL_VERSION} in their own environment, "
            f"or install ciel=={CIEL_VERSION}."
        )


def _fetch_ciel(plan: InstallPlan) -> None:
    env = {name: os.environ[name] for name in CIEL_ENVIRONMENT if name in os.environ}
    (command,) = plan.commands
    if command[0] == "ciel":
        _check_ciel(plan, env)
    _run(command, env, plan)


def _write_receipt(plan: InstallPlan, directory: Path) -> Path:
    path = _receipt_path(plan.root, plan.platform, plan.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "platform": plan.platform,
        "provider": plan.provider,
        "version": plan.version,
        "root": str(plan.root),
        "directory": str(directory),
        "commands": plan.commands,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "xeda_version": __version__,
    }
    with replacing_file(path, encoding="utf-8") as out:
        json.dump(document, out, indent=2)
        out.write("\n")
    return path


def _install_locked(
    residue: Residue, plan: InstallPlan, provider: Optional[str], version: Optional[str]
) -> InstallResult:
    _refuse_links(plan, *_written_paths(plan))
    found = _already_installed(residue, plan, provider, version)
    if found is not None:
        return found
    receipt: Optional[Path] = None
    if plan.provider in GIT_PROVIDERS:
        receipt = _install_git(residue, plan)
    else:
        _fetch_ciel(plan)
    try:
        located = _locate_everywhere(residue, provider, version, plan.root)
        verify_pin(located, residue)
    except (PdkNotInstalled, PdkVersionMismatch) as error:
        raise PdkInstallFailed(
            f"the install of platform '{plan.platform}' finished, but the store does not hold "
            f"what the platform needs.\n{error}"
        ) from None
    if receipt is None:
        _refuse_links(plan, _receipt_path(plan.root, plan.platform, plan.version))
        receipt = _write_receipt(plan, located.directory)
    return InstallResult(located, False, receipt, plan.commands)


def install(
    residue: Residue,
    provider: Optional[str],
    version: Optional[str],
    root: Path,
    *,
    url: Optional[str] = None,
) -> InstallResult:
    """Install the files of the platform into the store at `root`, and write a receipt.

    Arguments are as for `locate`. `url` is the repository that a Git provider (`orfs`,
    `lambdapdk`) fetches from, and the default is that of the provider (`ORFS_URL`,
    `LAMBDAPDK_URL`). The root may be a symbolic link: the install resolves it first. Below it,
    the install refuses a symbolic link on the way to anything that it writes, publishes, removes
    or locks (everything under `<root>/xeda`, but not `<root>/ciel`), before it writes, and again
    before each step that publishes or rolls back. So it writes and deletes only inside the
    store. If the store holds every file already, nothing runs and nothing is written. Otherwise the install takes the lock `<root>/xeda/.lock`, runs the
    plan, checks every file of every corner with `locate`, checks the pin with `verify_pin` and
    writes `<root>/xeda/receipts/<platform>-<pin>.json`.

    * `orfs`, `lambdapdk`: the Git commands fill `<root>/xeda/.partial-<random>`. The install
      writes the receipt, and one rename then moves the directory of the platform to
      `<root>/xeda/<provider>/<commit>/<directory>`, so that the store never holds a part of a
      platform under that name. Platforms of one commit share the directory of the commit, and
      the store keeps no `.git`. If a command fails, a file is missing or the receipt cannot be written,
      the store is as it was. Every `git` command gets a controlled environment: no `GIT_DIR`,
      no configuration of the user and no hook.
    * `ciel`: the plan runs with `PATH`, `HOME` and `LANG` only. A `ciel` on `PATH` must say that
      it is version 3. `ciel` writes the store itself, and one build may hold several libraries,
      so the install never removes it: a failed install may leave files of `ciel` in the store.

    A store of `ciel` counts as installed only with its receipt (see `installed`), so a store
    that `ciel` filled but nobody checked is fetched again.

    Raise `PdkInstallBusy` if another install holds the lock, `PdkInstallFailed` if the install
    does not finish (a link in the store or a file system error too, naming the path), and what
    `install_plan` raises for a plan that cannot be made.
    """
    # The root may be a link, or below one. Resolve it, so that no link is left on the way from
    # it to what the install writes, and a link found there is one inside the store.
    root = root.resolve()
    plan = plan_install(residue, provider, version, root, partial=secrets.token_hex(6), url=url)
    try:
        _refuse_links(plan, *_written_paths(plan))
        found = _already_installed(residue, plan, provider, version)
        if found is not None:
            return found
        return _install_under_lock(residue, plan, provider, version)
    except OSError as error:
        # A root that cannot be written, a full disk: the message names the path.
        raise PdkInstallFailed(f"cannot install platform '{plan.platform}': {error}") from None


def _install_under_lock(
    residue: Residue, plan: InstallPlan, provider: Optional[str], version: Optional[str]
) -> InstallResult:
    lock_path = plan.root / "xeda" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_links(plan, lock_path)
    # `O_NOFOLLOW` (not on Windows) makes the open fail on a link that appeared since the check.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock_path, flags, 0o666), "a") as lock:
        if sys.platform != "win32":
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PdkInstallBusy(
                    f"another install holds {lock_path}. Wait until it ends, "
                    "and run the command again."
                ) from None
        return _install_locked(residue, plan, provider, version)
