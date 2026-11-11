"""The tool store: tools that Xeda installs for the user, and finds again on the PATH of a run.

Today the store holds one tool, the OSS CAD Suite of YosysHQ (`oss-cad-suite`). The layout of a
store is:

    <root>/oss-cad-suite/<date>/<platform>/receipt.json
    <root>/oss-cad-suite/<date>/<platform>/oss-cad-suite/bin/...

`<date>` is the date of the build (`2026-10-08`) and `<platform>` is `linux-x64`, `linux-arm64`,
`darwin-x64` or `darwin-arm64`. The root is `--root`, else `XEDA_TOOLS_ROOT`, else the data
directory of the user. The package pins the date that `install` fetches by default
(`xeda/_internal/data/tools.json`); CI installs the same build.

Finding an installation only reads: `installed` and `select` start no process, use no network and
write nothing. An installation counts only if its receipt is valid, agrees with its path and the
suite has `bin/yosys`. `install` is the only function that uses the network and writes the store.
It downloads the archive of the release, checks its SHA-256 against the digest that GitHub lists,
extracts it into a staging directory of the store and moves that directory to its place in one
rename, under a lock. A failed install leaves the earlier installations as they were.

All network access goes through `open_url`, so a test replaces it.
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import http.client
import json
import os
import platform as platform_module
import posixpath
import re
import secrets
import shutil
import sys
import tarfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xeda.errors import SettingsError, SpecError

__all__ = [
    "DATE",
    "TOOL",
    "TOOLS",
    "Asset",
    "InstallPlan",
    "InstallResult",
    "Installation",
    "ToolInstallError",
    "ToolNotInstalled",
    "default_root",
    "find",
    "install",
    "installed",
    "launch_selection",
    "open_url",
    "pinned_version",
    "plan_install",
    "platform_tag",
    "select",
]

#: The tool that the store holds.
TOOL = "oss-cad-suite"
#: The tools of the store. A private file may select one of them.
TOOLS = (TOOL,)
#: The date of a build, as the tag of a release writes it.
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: The environment variable that names the root of the store.
ROOT_VARIABLE = "XEDA_TOOLS_ROOT"
#: The program that tells a complete suite from a directory that only has the name.
MARKER = "yosys"

REPOSITORY = "YosysHQ/oss-cad-suite-build"
API = f"https://api.github.com/repos/{REPOSITORY}/releases"
DOWNLOADS = f"https://github.com/{REPOSITORY}/releases/download"

#: What an archive may hold: the sum of the sizes of its files, and the number of its entries.
MAX_BYTES = 20 * 2**30
MAX_ENTRIES = 300_000

CHUNK = 1 << 20
RECEIPT = "receipt.json"
LOCK = ".lock"
SCHEMA_VERSION = 1
RECEIPT_KEYS = (
    "schema_version",
    "tool",
    "version",
    "platform",
    "url",
    "asset_id",
    "sha256",
    "installed_at",
)


class ToolNotInstalled(SpecError):
    """A run or a query selects an installation that the store does not hold."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="tool_not_installed")


class ToolInstallError(Exception):
    """The install did not finish. `code` is one of `tool_platform_unsupported`,
    `tool_install_busy`, `tool_download_failed`, `tool_checksum_failed`, `tool_archive_invalid`
    and `tool_install_failed`. The store holds what it held before."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def pinned_version() -> str:
    """Return the date of the suite that Xeda installs by default: the pin in `tools.json`."""
    from .package_paths import tools_pin_file

    pins = json.loads(tools_pin_file().read_text(encoding="utf-8"))
    version = pins[TOOL]
    if not (isinstance(version, str) and DATE.fullmatch(version)):
        raise ValueError(f"{tools_pin_file()}: the pin of {TOOL} is not a date: {version!r}")
    return version


def platform_tag() -> str | None:
    """Return the platform of this machine as the store and the release name it
    (`darwin-arm64`), or `None` for a machine that has no build."""
    system = {"linux": "linux", "darwin": "darwin"}.get(sys.platform)
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform_module.machine().lower()
    )
    return f"{system}-{machine}" if system and machine else None


def default_root(environ: Mapping[str, str]) -> Path:
    """Return the root of the store for a process with the environment `environ`:
    `XEDA_TOOLS_ROOT` (an absolute path), else `$XDG_DATA_HOME/xeda/tools` (`~/.local/share`
    when the variable is empty or relative), on macOS too: `~/Library/Application Support` has a
    space, and the `verilator` of the suite does not start from a path with a space. Raise
    `SettingsError` (`invalid_value`) for a relative `XEDA_TOOLS_ROOT`."""
    given = environ.get(ROOT_VARIABLE)
    if given:
        if not os.path.isabs(given):
            raise SettingsError(
                f"{ROOT_VARIABLE} is {given!r}: it must be an absolute path",
                code="invalid_value",
                setting=ROOT_VARIABLE,
            )
        return Path(os.path.normpath(given))
    data = environ.get("XDG_DATA_HOME", "")
    base = Path(data) if data and os.path.isabs(data) else Path.home() / ".local" / "share"
    return base / "xeda" / "tools"


# ---------------------------------------------------------------------------------------------
# Finding installations
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    """A complete installation in a store."""

    tool: str
    version: str
    platform: str
    root: Path
    #: The content of its receipt.
    receipt: Mapping[str, Any]

    @property
    def location(self) -> Path:
        """The directory that holds the receipt and the suite."""
        return installation_directory(self.root, self.version, self.platform)

    @property
    def directory(self) -> Path:
        """The suite: the directory with `bin`, `lib` and the `environment` script."""
        return self.location / TOOL

    @property
    def bin(self) -> Path:
        return self.directory / "bin"

    @property
    def sha256(self) -> str:
        return str(self.receipt["sha256"])


def installation_directory(root: Path, version: str, platform: str) -> Path:
    return root / TOOL / version / platform


def _receipt_problem(receipt: Any, version: str, platform: str) -> str | None:
    if not isinstance(receipt, dict):
        return "it is not a JSON object"
    missing = [key for key in RECEIPT_KEYS if key not in receipt]
    if missing:
        return f"it lacks {', '.join(missing)}"
    if receipt["schema_version"] != SCHEMA_VERSION:
        return f"its schema_version is {receipt['schema_version']!r}, not {SCHEMA_VERSION}"
    for key, expected in (("tool", TOOL), ("version", version), ("platform", platform)):
        if receipt[key] != expected:
            return f"its {key} is {receipt[key]!r}, and the path says {expected!r}"
    texts = ("url", "installed_at")
    if not all(isinstance(receipt[key], str) for key in texts):
        return f"{' and '.join(texts)} must be text"
    if not (isinstance(receipt["asset_id"], int) and not isinstance(receipt["asset_id"], bool)):
        return "asset_id must be an integer"
    if not (isinstance(receipt["sha256"], str) and re.fullmatch("[0-9a-f]{64}", receipt["sha256"])):
        return "sha256 must be 64 lower-case hexadecimal digits"
    return None


def _admit(root: Path, version: str, platform: str) -> Installation | None:
    """Return the installation of `version` for `platform`, or `None` if the store holds none
    that is complete."""
    from .platforms.providers import link_on_the_way

    location = installation_directory(root, version, platform)
    try:
        if link_on_the_way(root, location / TOOL) is not None:
            return None
        receipt_path = location / RECEIPT
        if not receipt_path.is_file() or receipt_path.is_symlink():
            return None
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if _receipt_problem(receipt, version, platform) is not None:
            return None
        if not (location / TOOL / "bin" / MARKER).is_file():
            return None
    except (OSError, ValueError):
        return None
    return Installation(TOOL, version, platform, root, receipt)


def installed(root: Path, platform: str | None = None) -> list[Installation]:
    """Return the complete installations in the store `root` for `platform` (default: this
    machine), the oldest first. A machine with no build holds none."""
    platform = platform or platform_tag()
    if platform is None:
        return []
    try:
        names = sorted(
            entry.name for entry in os.scandir(root / TOOL) if DATE.fullmatch(entry.name)
        )
    except OSError:
        return []
    found = (_admit(root, name, platform) for name in names)
    return [installation for installation in found if installation is not None]


def find(root: Path, version: str, platform: str | None = None) -> Installation | None:
    """Return the installation of `version`, or `None`."""
    platform = platform or platform_tag()
    return None if platform is None else _admit(root, version, platform)


def select(root: Path, setting: str | None) -> Installation | None:
    """Return the installation that a run with the selection `setting` uses: none for `off`, the
    one of that date for a date, else the newest of this machine, or `None` if the store holds
    none. Raise `ToolNotInstalled` for a date that the store lacks."""
    if setting == "off":
        return None
    if setting is None:
        found = installed(root)
        return found[-1] if found else None
    chosen = find(root, setting)
    if chosen is None:
        raise ToolNotInstalled(
            f"{TOOL} {setting} is not installed in {root}. Install it with "
            f"`xeda tools install {TOOL} --version {setting} --root {root}`, "
            f"or select another build in `tools:` of your private settings."
        )
    return chosen


def launch_selection(environ: Mapping[str, str], tools: Mapping[str, str]) -> Installation | None:
    """Return the suite that a launch puts on `PATH`, for the environment `environ` of the launch
    and the `tools` selection of its private settings."""
    return select(default_root(environ), tools.get(TOOL))


# ---------------------------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------------------------


def open_url(url: str) -> Any:
    """Open `url` for reading and return the response, a context manager with `read` and
    `headers`. This is the one place of the store that uses the network."""
    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": "xeda", "Accept": "application/vnd.github+json"}
    )
    return urllib.request.urlopen(request, timeout=60)


def _get_json(url: str) -> Any:
    try:
        with open_url(url) as response:
            return json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise ToolInstallError(
            f"cannot read the release information at {url}: {error}", "tool_download_failed"
        ) from None


@dataclass(frozen=True)
class Asset:
    """The archive of a release for one platform, as GitHub lists it."""

    name: str
    url: str
    asset_id: int
    size: int | None
    sha256: str


def asset_name(version: str, platform: str) -> str:
    return f"{TOOL}-{platform}-{version.replace('-', '')}.tgz"


def _release_asset(version: str | None, platform: str) -> tuple[str, Asset]:
    """Return the date and the archive for `platform` of the release `version` (a date, or
    `latest`). The release must be published and name the date; the archive must be there and
    have a SHA-256 digest."""
    where = "latest" if version == "latest" else f"tags/{version}"
    release = _get_json(f"{API}/{where}")
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not (isinstance(tag, str) and DATE.fullmatch(tag)) or release.get("draft"):
        raise ToolInstallError(
            f"the release {where} of {REPOSITORY} is not a published build with a date as its tag",
            "tool_download_failed",
        )
    if version != "latest" and tag != version:
        raise ToolInstallError(
            f"the release {where} of {REPOSITORY} has the tag {tag!r}", "tool_download_failed"
        )
    name = asset_name(tag, platform)
    assets = release.get("assets")
    entry = next(
        (
            (a for a in assets if isinstance(a, dict) and a.get("name") == name)
            if isinstance(assets, list)
            else iter(())
        ),
        None,
    )
    if entry is None:
        raise ToolInstallError(
            f"the release {tag} of {REPOSITORY} has no archive {name} for {platform}",
            "tool_platform_unsupported",
        )
    digest = entry.get("digest")
    match = re.fullmatch("sha256:([0-9a-f]{64})", digest) if isinstance(digest, str) else None
    if match is None:
        raise ToolInstallError(
            f"GitHub lists no SHA-256 digest for {name}, so the download cannot be checked",
            "tool_checksum_failed",
        )
    asset_id, size = entry.get("id"), entry.get("size")
    if not isinstance(asset_id, int) or isinstance(asset_id, bool):
        raise ToolInstallError(f"GitHub lists no id for {name}", "tool_download_failed")
    return tag, Asset(
        name,
        f"{DOWNLOADS}/{tag}/{name}",
        asset_id,
        size if isinstance(size, int) and size > 0 else None,
        match.group(1),
    )


# ---------------------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    """What an install will do, before it does anything."""

    root: Path
    version: str
    platform: str
    asset: Asset

    @property
    def location(self) -> Path:
        return installation_directory(self.root, self.version, self.platform)


@dataclass(frozen=True)
class InstallResult:
    installation: Installation
    #: Whether the store held the installation already, so that nothing was downloaded.
    already_installed: bool


def check_version(version: str) -> None:
    """Raise `ValueError` unless `version` is `latest` or a date."""
    if version != "latest":
        try:
            datetime.date.fromisoformat(version)
        except ValueError:
            version = ""
        if not DATE.fullmatch(version):
            raise ValueError("a version is `latest` or a date such as 2026-10-08")


def plan_install(root: Path, version: str | None) -> InstallPlan:
    """Return the plan to install `version` (a date, `latest`, or `None` for the pin of the
    package) into the store `root`. It reads the release information from GitHub, so it uses the
    network. `latest` resolves here, once. Raise `ToolInstallError` for a machine with no build or
    a release that cannot be used."""
    platform = platform_tag()
    if platform is None:
        raise ToolInstallError(
            f"{TOOL} has no build for {sys.platform} on {platform_module.machine()}",
            "tool_platform_unsupported",
        )
    date, asset = _release_asset(version or pinned_version(), platform)
    return InstallPlan(root.resolve(), date, platform, asset)


def _remove_tree(path: Path) -> None:
    """Remove a directory that an install made. A failure is only logged: it must not hide the
    error that made the install remove it."""
    import logging

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        logging.getLogger(__name__).warning("cannot remove %s: %s; remove it", path, error)


def _refuse_links(plan: InstallPlan, *paths: Path) -> None:
    from .platforms.providers import link_on_the_way

    for path in paths:
        link = link_on_the_way(plan.root, path)
        if link is not None:
            raise ToolInstallError(
                f"cannot install {TOOL}: {link} is a symbolic link (to {os.readlink(link)}). The "
                "install writes only real directories inside the store, so that it never writes "
                "or deletes outside it. Replace the link with a real directory, and run the "
                "command again.",
                "tool_install_failed",
            )


def install(plan: InstallPlan, progress: Callable[[str], None] | None = None) -> InstallResult:
    """Install the plan into its store. The root may be a link; below it the install refuses a
    link on the way to anything that it writes. One install at a time uses a store
    (`tool_install_busy`). The install downloads the archive into a staging directory, checks its
    size and SHA-256 before it reads one entry, extracts it there, writes the receipt and moves
    the directory to `<root>/oss-cad-suite/<date>/<platform>` with one rename. A complete
    installation is returned as it is. A destination that exists without being complete is
    refused. Whatever fails, the store keeps neither the staging directory nor a new receipt, and
    the earlier installations are unchanged."""
    from .platforms.providers import locked_store

    say = progress or (lambda message: None)
    root = plan.root
    lock_path = root / LOCK
    staging = root / f".partial-{secrets.token_hex(6)}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        _refuse_links(plan, lock_path, staging, plan.location)

        def busy() -> ToolInstallError:
            return ToolInstallError(
                f"another install holds {lock_path}. Wait until it ends, "
                "and run the command again.",
                "tool_install_busy",
            )

        with locked_store(lock_path, busy):
            existing = find(root, plan.version, plan.platform)
            if existing is not None:
                return InstallResult(existing, True)
            if os.path.lexists(plan.location):
                raise ToolInstallError(
                    f"cannot install {TOOL} {plan.version}: {plan.location} exists, but it is not a "
                    "complete installation. Move or delete it, and run the command again.",
                    "tool_install_failed",
                )
            try:
                staging.mkdir(mode=0o700)
                payload = staging / "payload"
                payload.mkdir(mode=0o700)
                say(f"Downloading {plan.asset.name}")
                archive = staging / "archive.tgz"
                _download(plan, archive)
                say("Extracting")
                _extract(archive, payload)
                _write_receipt(plan, payload / RECEIPT)
                if not (payload / TOOL / "bin" / MARKER).is_file():
                    raise ToolInstallError(
                        f"the archive {plan.asset.name} has no {TOOL}/bin/{MARKER}",
                        "tool_archive_invalid",
                    )
                # The store can change while the archive downloads: look again before the move.
                _refuse_links(plan, staging, plan.location)
                _make_parents(plan)
                payload.rename(plan.location)
            finally:
                _remove_tree(staging)
    except OSError as error:
        raise ToolInstallError(f"cannot install {TOOL}: {error}", "tool_install_failed") from None
    installation = find(root, plan.version, plan.platform)
    if installation is None:
        raise ToolInstallError(
            f"the install of {TOOL} {plan.version} finished, but the store does not hold a "
            "complete installation",
            "tool_install_failed",
        )
    return InstallResult(installation, False)


def _make_parents(plan: InstallPlan) -> None:
    """Create the directories above the installation one level at a time from the root, and
    refuse a symbolic link on the way, before anything is created below it."""
    for directory in reversed(
        plan.location.parents[: len(plan.location.relative_to(plan.root).parts)]
    ):
        if directory == plan.root:
            continue
        _refuse_links(plan, directory)
        directory.mkdir(exist_ok=True)


def _download(plan: InstallPlan, target: Path) -> None:
    """Download the archive to `target` and check its size and SHA-256."""
    from .utils import replacing_file

    asset = plan.asset
    digest = hashlib.sha256()
    size = 0
    try:
        with open_url(asset.url) as response, replacing_file(target, "wb") as out:
            for chunk in iter(lambda: response.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
    except (OSError, http.client.HTTPException) as error:
        raise ToolInstallError(
            f"cannot download {asset.url}: {error}", "tool_download_failed"
        ) from None
    if asset.size is not None and size != asset.size:
        raise ToolInstallError(
            f"{asset.url}: {size} bytes arrived, and GitHub lists {asset.size}",
            "tool_download_failed",
        )
    if digest.hexdigest() != asset.sha256:
        raise ToolInstallError(
            f"{asset.name}: its SHA-256 is {digest.hexdigest()}, and GitHub lists {asset.sha256}. "
            "Nothing is installed.",
            "tool_checksum_failed",
        )


def _write_receipt(plan: InstallPlan, path: Path) -> None:
    from .utils import replacing_file

    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "version": plan.version,
        "platform": plan.platform,
        "url": plan.asset.url,
        "asset_id": plan.asset.asset_id,
        "sha256": plan.asset.sha256,
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    with replacing_file(path, encoding="utf-8") as out:
        json.dump(document, out, indent=2)
        out.write("\n")


# ---------------------------------------------------------------------------------------------
# Extracting
# ---------------------------------------------------------------------------------------------


def _invalid(archive: Path, member: str, why: str) -> ToolInstallError:
    return ToolInstallError(
        f"the archive {archive.name} is not safe to extract: {member!r} {why}. Nothing is installed.",
        "tool_archive_invalid",
    )


def _member_parts(archive: Path, name: str, what: str = "name") -> tuple[str, ...]:
    """Return the parts of the name of an entry, below the top directory `oss-cad-suite`. A
    leading `./` is ignored. Raise for an absolute name, a `..`, a NUL or another top."""
    if "\0" in name or name.startswith("/"):
        raise _invalid(archive, name, f"has an absolute {what} or a NUL")
    parts = tuple(part for part in name.split("/") if part not in ("", "."))
    if ".." in parts:
        raise _invalid(archive, name, f"has a {what} with ..")
    if not parts or parts[0] != TOOL:
        raise _invalid(archive, name, f"is not below {TOOL}/")
    return parts


def _link_target_inside(archive: Path, parts: tuple[str, ...], target: str) -> None:
    """Raise unless the symbolic link at `parts` with the target `target` stays inside the top
    directory."""
    if not target or "\0" in target or target.startswith("/"):
        raise _invalid(archive, "/".join(parts), f"links to {target!r}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname("/".join(parts)), target))
    if resolved != TOOL and not resolved.startswith(f"{TOOL}/"):
        raise _invalid(archive, "/".join(parts), f"links to {target!r}, outside the suite")


def _extract(archive: Path, payload: Path) -> None:
    """Extract the archive into `payload`: only the tree `oss-cad-suite/`. Refuse an entry that
    leaves it, a link that points out of it, a device or another special file, a name twice, and a
    write below a link. Keep the executable bits of a file; no mode bit of a privilege. The
    archive may hold at most `MAX_ENTRIES` entries and `MAX_BYTES` bytes of files."""
    from .platforms.providers import link_on_the_way
    from .utils import replacing_file

    made: dict[Path, int] = {}  # a directory the extraction made, and its mode
    seen: set[Path] = set()
    total = 0

    def make_parents(path: Path) -> None:
        """Create the directories above `path` that the archive did not list, and refuse a link
        on the way."""
        if link_on_the_way(payload, path.parent) is not None:
            raise _invalid(archive, str(path.relative_to(payload)), "is below a link")
        for directory in reversed(path.parents[: len(path.relative_to(payload).parts) - 1]):
            if not directory.exists():
                directory.mkdir(mode=0o700)
                made[directory] = 0o755
            elif not directory.is_dir():
                raise _invalid(archive, str(path.relative_to(payload)), "is below a file")

    try:
        with gzip.open(archive, "rb") as stream, tarfile.open(fileobj=stream, mode="r") as tar:
            for count, member in enumerate(tar, 1):
                if count > MAX_ENTRIES:
                    raise _invalid(archive, member.name, f"is entry {count}: too many entries")
                parts = _member_parts(archive, member.name)
                path = payload.joinpath(*parts)
                make_parents(path)
                implicit = member.isdir() and path in made  # listed after one of its files
                if path in seen or (os.path.lexists(path) and not implicit):
                    raise _invalid(archive, member.name, "is in the archive twice")
                seen.add(path)
                if member.isdir():
                    if not implicit:
                        path.mkdir(mode=0o700)
                    made[path] = (member.mode & 0o755) | 0o700
                elif member.isreg():
                    total += member.size
                    if total > MAX_BYTES:
                        raise _invalid(archive, member.name, "makes the archive too large")
                    source = tar.extractfile(member)
                    assert source is not None
                    with replacing_file(path, "wb") as out:
                        shutil.copyfileobj(source, out, CHUNK)
                    os.chmod(path, (member.mode & 0o755) | 0o600)
                elif member.issym():
                    _link_target_inside(archive, parts, member.linkname)
                    os.symlink(member.linkname, path)
                else:
                    raise _invalid(
                        archive, member.name, "is a hard link, a device or another special file"
                    )
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ToolInstallError(
            f"cannot read the archive {archive.name}: {error}", "tool_archive_invalid"
        ) from None
    _check_links_resolve_inside(archive, payload)
    # The directories were writable while they filled. Set their modes, the deepest first.
    for directory in sorted(made, key=lambda p: len(p.parts), reverse=True):
        os.chmod(directory, made[directory])


def _check_links_resolve_inside(archive: Path, payload: Path) -> None:
    """Raise unless every symbolic link of the extracted suite ends inside it. The check follows
    the links as the operating system does, so a chain of links that each look harmless (`up`
    to `..`, then a long path through `up`) is found. A link that points nowhere must end
    inside the suite too."""
    suite = os.path.realpath(payload / TOOL)
    for directory, names, files in os.walk(payload / TOOL):
        for name in (*names, *files):
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                continue
            ends = os.path.realpath(path)
            if ends != suite and not ends.startswith(suite + os.sep):
                shown = os.path.relpath(path, payload)
                raise _invalid(archive, shown, f"links to {os.readlink(path)!r}, outside the suite")
