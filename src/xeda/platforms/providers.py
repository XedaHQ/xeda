"""The PDK providers: find the files of a platform in a store, and plan how to install them.

A provider maps the role names of a residue (`liberty`, `tech_lef`, ...) to files by path
arithmetic on a store root and the pinned version. Finding the files uses `stat` only: it starts
no process, opens no connection, writes nothing and creates no directory. So planning and
`--dry-run` can run it. Only the install command, which is separate, uses the network and writes
the store.

Three providers serve a platform. A residue lists the ones that serve it in `providers`:

| provider | the directory of the platform | the version |
| --- | --- | --- |
| `orfs` | `<root>/xeda/orfs/<commit>/<directory>`, a sparse checkout of OpenROAD-flow-scripts | the commit |
| `ciel` | `<root>/ciel/<family>/versions/<hash>/<variant>`, as `ciel fetch` writes it | the hash |
| `directory` | `<root>/<platform>` (layout `orfs`: `<root>` is `flow/platforms` of a checkout) or `<root>/<variant>` (layout `open_pdks`) | `pdk_version` as given, else `unpinned` |

The root is the setting `pdk_root`, and the default is `~/.ciel`. No code here reads the
environment except `HOME`, through `Path.home()`.
"""

import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from xeda.flow import FlowFatalError
from xeda.platforms.model import Located
from xeda.platforms.residue import COMMIT, Residue

__all__ = [
    "CIEL_VERSION",
    "ORFS_URL",
    "PROVIDERS",
    "UNPINNED",
    "PdkInstallerMissing",
    "PdkNotInstalled",
    "PdkVersionMismatch",
    "default_root",
    "install_plan",
    "locate",
    "orfs_checkout_directory",
    "platform_directory",
    "verify_pin",
]

#: The providers, in the order that messages list them.
PROVIDERS = ("orfs", "ciel", "directory")
#: The version of a `directory` that the user gives no pin for.
UNPINNED = "unpinned"
#: The version of `ciel` that the installer runs. It has nothing to do with the pin of any PDK.
CIEL_VERSION = "3.0.1"
#: The repository that the `orfs` provider fetches.
ORFS_URL = "https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts"

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


def default_root() -> Path:
    """Return the default root of a PDK store: `~/.ciel`, with `~` from `HOME`."""
    return Path.home() / ".ciel"


def orfs_checkout_directory(root: Path, commit: str) -> Path:
    """Return the directory that holds the sparse checkout of an ORFS commit."""
    return root / "xeda" / "orfs" / commit


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
    if name == "orfs":
        directory = orfs_checkout_directory(root, pinned) / entry.directory
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


def _first_line(residue: Residue, target: _Target) -> str:
    entry = target.entry
    if target.provider == "orfs":
        what, source = entry.directory, f"orfs {target.version[:SHORT_HASH]}"
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


def _not_installed(
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
    the residue (`orfs`, `ciel`) or `unpinned` (`directory`). `corner` goes through the corner
    resolution of the residue, and `Located.files["liberty"]` holds the files of that corner.
    `platform_file` is the user file that the residue came from, if it came from one. It only
    changes the install command in the refusal, which then names the file and not the platform.

    The function checks with `stat` that every file exists and raises `PdkNotInstalled` if one is
    missing. It does not read the version marker of a `directory` (see `verify_pin`). It starts
    no process, uses no network, writes nothing and creates no directory. A relative `root` is
    made absolute with the current directory.

    Raise `ValueError` for a provider that does not serve the platform, a version that is no
    commit hash (`orfs`, `ciel`) and an unknown corner."""
    token = residue.resolve_corner(corner)
    target = _target(residue, provider, version, root)
    missing = [
        path for path in _relative_files(residue, token) if not (target.directory / path).is_file()
    ]
    if missing:
        raise _not_installed(residue, target, missing, platform_file)
    files = {
        role: [target.directory / path for path in paths]
        for role, paths in residue.files.role_paths(token).items()
    }
    upstream = [] if residue.upstream is None else residue.upstream.files
    return Located(
        provider=target.provider,
        version=target.version,
        root=target.root,
        directory=target.directory,
        corner=token,
        files=files,
        upstream_files=[target.directory / path for path in upstream],
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
    * `orfs`: the pin is the name of the directory, so there is nothing to read.

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


def _orfs_plan(target: _Target, partial: str) -> list[list[str]]:
    work = str(target.root / "xeda" / f".partial-{partial}")
    git = ["git", "-C", work]
    return [
        ["git", "init", "--quiet", work],
        [*git, "remote", "add", "origin", ORFS_URL],
        [*git, "sparse-checkout", "set", "--cone", target.entry.directory],
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
) -> list[list[str]]:
    """Return the argument vectors that install the files of the platform, in the order to run.
    Nothing here starts a process or writes.

    * `ciel`: one vector. The first of `uv tool run`, `pipx run` and a `ciel` on `PATH` that
      `shutil.which` finds. Whether that `ciel` is version 3 is for the installer to probe.
      Raise `PdkInstallerMissing` if there is none.
    * `orfs`: the Git commands that fetch the platform directory of the commit into
      `<root>/xeda/.partial-<partial>`. The first command creates that directory. `partial` is
      required, and the installer chooses it, so that the plan is deterministic. After the
      last command, the installer moves the directory to `orfs_checkout_directory(root, commit)`.
    * `directory`: nothing to install. Raise `ValueError`."""
    target = _target(residue, provider, version, root)
    if target.provider == "directory":
        raise ValueError(
            f"the directory provider has nothing to install: the directory must hold the files "
            f"of platform '{residue.name}'"
        )
    if target.provider == "ciel":
        return _ciel_plan(residue, target)
    if partial is None:
        raise ValueError("the plan of provider 'orfs' needs the name of its partial directory")
    return _orfs_plan(target, partial)
