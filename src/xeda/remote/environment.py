"""The inputs of the remote environment: a snapshot of the local xeda code, the dependency spec,
and the keys that name the directories of the remote host.

Nothing here connects to a host. The functions read the installed packages of this process.
"""

import dataclasses
import hashlib
import importlib.metadata
import io
import json
import os
import re
import sys
import sysconfig
import tarfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Optional, Sequence

import xeda

from ..version import __version__
from .constants import LAYOUT, UV_RELEASES, UV_VERSION
from .errors import RemoteDependencyError, RemoteProvisionError

#: The distribution that the remote gets as a code snapshot, not from an index.
DISTRIBUTION = "xeda"

#: The first part of a requirement: the name of the distribution.
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

#: A requirement for an extra. The environment installs no extras.
_EXTRA_MARKER = re.compile(r"\bextra\s*==")

_KEY_LENGTH = 24


def _normalize(name: str) -> str:
    """The canonical form of a distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(content: object) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return _digest(text.encode("utf-8"))[:_KEY_LENGTH]


# --- The code snapshot ------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeSnapshot:
    """The files of the local xeda package.

    `key` names the snapshot by content. `files` pairs the name of each file in the archive with
    its local path, sorted by name. `contents` holds the bytes and the executable bit of each file
    as read when the key was made, in the same order. The archive comes from `contents` only, so
    it always matches the key.
    """

    key: str
    version: str
    files: tuple[tuple[str, Path], ...]
    contents: tuple[tuple[str, bytes, bool], ...] = dataclasses.field(repr=False)


def _dist_info(version: str) -> tuple[str, dict[str, bytes]]:
    """The name of the minimal dist-info directory and the content of its files."""
    metadata = f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: {version}\n"
    return f"{DISTRIBUTION}-{version}.dist-info", {
        "METADATA": metadata.encode("utf-8"),
        "INSTALLER": b"xeda-remote\n",
    }


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o100)


def _package_files(package_dir: Path) -> list[tuple[str, Path]]:
    """The files to send.

    Skipped: bytecode, backup files (`~`), symbolic links, and every file or directory whose name
    starts with a dot.
    """
    found: list[tuple[str, Path]] = []
    for directory, subdirs, names in os.walk(package_dir):
        subdirs[:] = sorted(d for d in subdirs if d != "__pycache__" and not d.startswith("."))
        for name in sorted(names):
            if name.startswith(".") or name.endswith((".pyc", ".pyo", "~")):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(package_dir).as_posix()
            found.append((f"{DISTRIBUTION}/{relative}", path))
    found.sort(key=lambda item: item[0])
    return found


@lru_cache(maxsize=1)
def _installed_snapshot() -> CodeSnapshot:
    return _snapshot_of(Path(xeda.__file__).parent)


def _snapshot_of(package_dir: Path) -> CodeSnapshot:
    files = _package_files(package_dir)
    contents = tuple((name, path.read_bytes(), _is_executable(path)) for name, path in files)
    _, dist_files = _dist_info(__version__)
    key = _key(
        {
            "version": __version__,
            "dist_info": {name: _digest(data) for name, data in sorted(dist_files.items())},
            "files": [[name, executable, _digest(data)] for name, data, executable in contents],
        }
    )
    return CodeSnapshot(key=key, version=__version__, files=tuple(files), contents=contents)


def code_snapshot(package_dir: Optional[Path] = None) -> CodeSnapshot:
    """Snapshot the installed xeda package, or the package in `package_dir`.

    The snapshot holds the bytes of the files, read once. The key covers the content and the
    executable bit of every file, and the version. It does not cover the time stamps. The snapshot
    of the installed package is made once per process.
    """
    if package_dir is None:
        return _installed_snapshot()
    return _snapshot_of(Path(package_dir))


def _member(name: str, data: bytes, executable: bool) -> tuple[tarfile.TarInfo, io.BytesIO]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o755 if executable else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info, io.BytesIO(data)


def write_code_tar(snapshot: CodeSnapshot, out: BinaryIO) -> None:
    """Write the snapshot as a tar stream. The same snapshot always gives the same bytes.

    The archive holds regular files only: the package, and a minimal dist-info directory.
    """
    dist_dir, dist_files = _dist_info(snapshot.version)
    members: dict[str, tuple[bytes, bool]] = {
        f"{dist_dir}/{name}": (data, False) for name, data in dist_files.items()
    }
    for name, data, executable in snapshot.contents:
        members[name] = (data, executable)
    with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name in sorted(members):
            data, executable = members[name]
            info, content = _member(name, data, executable)
            tar.addfile(info, content)


# --- The dependency spec ----------------------------------------------------------------------


@dataclass(frozen=True)
class DependencySpec:
    """What the remote installs: the requirements, and the local versions as constraints."""

    requirements: tuple[str, ...]
    constraints: tuple[str, ...]


def _distribution(name: str) -> Optional[importlib.metadata.Distribution]:
    try:
        return importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _name_of(requirement: str) -> str:
    match = _NAME.match(requirement)
    if match is None:
        raise RemoteDependencyError(f"Cannot read a package name from '{requirement}'.")
    return _normalize(match.group(1))


def _check_from_index(name: str, dist: importlib.metadata.Distribution) -> None:
    """Refuse a distribution that was not installed from a package index."""
    text = dist.read_text("direct_url.json")
    if not text:
        return
    try:
        direct = json.loads(text)
    except ValueError:
        direct = {}
    if "dir_info" in direct:
        kind = "an editable install" if direct["dir_info"].get("editable") else "a local directory"
    else:
        kind = "a direct URL"
    raise RemoteDependencyError(
        f"The package '{name}' is installed from {kind}, so its version number does not identify"
        " its code. Install it from a package index, then run again."
    )


def _closure(roots: Sequence[str]) -> dict[str, str]:
    """The installed version of every distribution that the roots reach, without xeda itself."""
    versions: dict[str, str] = {}
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name == DISTRIBUTION or name in versions:
            continue
        dist = _distribution(name)
        if dist is None:
            continue
        _check_from_index(name, dist)
        versions[name] = dist.version
        for requirement in dist.requires or ():
            if not _EXTRA_MARKER.search(requirement):
                pending.append(_name_of(requirement))
    return versions


def dependency_spec(extra_packages: Sequence[str] = ()) -> DependencySpec:
    """The requirements of xeda, plus `extra_packages`, with the local versions as constraints.

    A requirement of xeda for an extra is not included. An extra package must be installed here.
    The walk does not evaluate markers. A package that is installed here gets a constraint, even
    if a false marker excludes it from this environment (for example `colorama` behind
    `platform_system == "Windows"`). That is harmless: uv applies a constraint only to a package
    that the requirements need. A package that is not installed here has no constraint, so the
    remote resolves it freely.
    """
    declared = importlib.metadata.requires(DISTRIBUTION)
    if declared is None:
        raise RemoteDependencyError(
            "The package 'xeda' is not installed in this environment, so its requirements are"
            " unknown."
        )
    requirements = [r for r in declared if not _EXTRA_MARKER.search(r)]
    for package in extra_packages:
        name = _name_of(package)
        if _distribution(name) is None:
            raise RemoteDependencyError(
                f"The package '{name}' is not installed here, so its version cannot be used on"
                " the remote. Install it in this environment, then run again."
            )
        requirements.append(package.strip())
    roots = [_name_of(r) for r in requirements]
    versions = _closure(roots)
    return DependencySpec(
        requirements=tuple(sorted(set(requirements))),
        constraints=tuple(f"{name}=={version}" for name, version in sorted(versions.items())),
    )


# --- The platform, the Python and the keys ----------------------------------------------------


@dataclass(frozen=True)
class RemotePlatform:
    """The facts about the remote host that the environment depends on."""

    system: str
    machine: str
    libc: str
    libc_version: str


@dataclass(frozen=True)
class PythonChoice:
    """The Python of the remote environment.

    `request` is what uv installs (`cpython-3.12`, or `cpython-3.13+freethreaded`). `path` is the
    Python of the host to use instead of an install, or `None`.
    """

    request: str
    path: Optional[str]


def python_choice(profile_python: str) -> PythonChoice:
    """Choose the remote Python from the profile's `python` value.

    The value `managed` means that uv installs the Python that runs the local xeda.
    """
    if sys.implementation.name != "cpython":
        raise RemoteDependencyError(
            f"The local Python is '{sys.implementation.name}'. A remote run needs CPython."
        )
    request = f"cpython-{sys.version_info.major}.{sys.version_info.minor}"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        request += "+freethreaded"
    return PythonChoice(
        request=request, path=None if profile_python == "managed" else profile_python
    )


def dependency_key(spec: DependencySpec, python: PythonChoice, platform: RemotePlatform) -> str:
    """Name the dependency environment: 24 hex characters over every input of the resolution.

    That includes the uv version, which can change what uv resolves.
    """
    return _key(
        {
            "layout": LAYOUT,
            "uv": UV_VERSION,
            "requirements": list(spec.requirements),
            "constraints": list(spec.constraints),
            "python": {"request": python.request, "path": python.path},
            "platform": {
                "system": platform.system,
                "machine": platform.machine,
                "libc": platform.libc,
                "libc_version": platform.libc_version,
            },
        }
    )


def uv_release(platform: RemotePlatform) -> tuple[str, str]:
    """The download URL and the SHA-256 of the pinned uv release for the platform."""
    if platform.system != "Linux":
        raise RemoteProvisionError(
            "not-linux", f"The remote system is '{platform.system}'. A remote run needs Linux."
        )
    libc = {"glibc": "gnu", "musl": "musl"}.get(platform.libc)
    target = f"{platform.machine}-unknown-linux-{libc}"
    try:
        return UV_RELEASES[target]
    except KeyError:
        raise RemoteProvisionError(
            "unsupported-platform",
            f"No uv release is pinned for {platform.machine} with {platform.libc}."
            " Supported: x86_64 and aarch64, with glibc or musl.",
        ) from None
