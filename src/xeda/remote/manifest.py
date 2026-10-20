"""Digest-based file sync, for the inputs that go to the remote host and the files that come back.

A manifest lists files by relative path, size and SHA-256. The receiver compares it with the files
it has (`plan_sync`) and asks only for what is missing or different. The sender writes those files
as a tar stream (`write_tar`). The receiver writes a file only after its SHA-256 matches the
manifest (`apply_sync`), and it leaves an unchanged file untouched, so that its inode stays.

This module runs on both machines and is the trust boundary for the files that cross the
connection. It reads and writes only regular files and real directories. It never follows a
symbolic link in the destination: every step down from the destination root opens its directory
with `O_NOFOLLOW`, relative to the descriptor of the directory above. It writes outside any run
directory, so it does not use `RunDirectory`.
"""

import errno
import hashlib
import io
import os
import re
import secrets
import stat
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO

from .errors import RemoteError, RemoteFetchError, RemoteProtocolError

_CHUNK = 1 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}")


def safe_member_path(name: str) -> PurePosixPath:
    """Return `name` as a relative path that stays below its root, or raise
    `RemoteProtocolError`. A name is refused if it is empty, absolute or has a NUL character, or
    if one of its parts is empty, `.` or `..`."""
    if not isinstance(name, str) or not name:
        raise RemoteProtocolError("a file name is empty")
    if "\0" in name:
        raise RemoteProtocolError(f"the file name {name!r} has a NUL character")
    if name.startswith("/"):
        raise RemoteProtocolError(f"the file name {name!r} is absolute")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise RemoteProtocolError(f"the file name {name!r} has an empty, '.' or '..' part")
    return PurePosixPath(name)


@dataclass(frozen=True)
class Entry:
    """One file of a manifest. `path` is relative and uses `/`."""

    path: str
    size: int
    sha256: str
    executable: bool

    def __post_init__(self) -> None:
        safe_member_path(self.path)
        if type(self.size) is not int or self.size < 0:
            raise RemoteProtocolError(f"{self.path}: the size {self.size!r} is not a count")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise RemoteProtocolError(f"{self.path}: {self.sha256!r} is not a SHA-256 digest")
        if type(self.executable) is not bool:
            raise RemoteProtocolError(f"{self.path}: 'executable' is not true or false")

    def as_json_value(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }

    @classmethod
    def from_json_value(cls, value: Any) -> "Entry":
        """Read an entry that arrived as JSON. Raise `RemoteProtocolError` if it is not one."""
        keys = {"path", "size", "sha256", "executable"}
        if not isinstance(value, dict) or set(value) != keys:
            raise RemoteProtocolError(f"a manifest entry must have exactly the keys {sorted(keys)}")
        return cls(value["path"], value["size"], value["sha256"], value["executable"])


@dataclass(frozen=True)
class SyncPlan:
    """What a sync must do: the paths of the entries to receive, and the paths of the files below
    the root that are not in the manifest and must go."""

    missing: list[str]
    stale: list[str]


def _check_entries(entries: Sequence[Entry]) -> None:
    """Refuse a manifest with a path twice, or with a file at the place of a directory."""
    paths = {entry.path for entry in entries}
    if len(paths) != len(entries):
        raise RemoteProtocolError("the manifest names a path twice")
    for path in paths:
        for parent in PurePosixPath(path).parents:
            if str(parent) in paths:
                raise RemoteProtocolError(
                    f"the manifest has {str(parent)!r} as file and as directory"
                )


def build_manifest(files: Mapping[str, Path], generated: Mapping[str, bytes] = {}) -> list[Entry]:
    """Describe local files and generated content. The keys are the relative paths in the
    manifest. A source file is followed if it is a link, and must be a regular file. The entries
    are sorted by path."""
    if clash := files.keys() & generated.keys():
        raise ValueError(f"both a file and generated content for {sorted(clash)}")
    entries = [Entry(path, *_describe(Path(source))) for path, source in files.items()]
    for path, content in generated.items():
        entries.append(Entry(path, len(content), hashlib.sha256(content).hexdigest(), False))
    entries.sort(key=lambda entry: entry.path)
    _check_entries(entries)
    return entries


def _describe(source: Path) -> tuple[int, str, bool]:
    status = source.stat()  # before the open, which would wait for the writer of a pipe
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"{source} is not a regular file")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest(), bool(status.st_mode & stat.S_IXUSR)


class _HashingReader:
    """Pass reads through and hash what goes by."""

    def __init__(self, stream: BinaryIO) -> None:
        self.digest = hashlib.sha256()
        self.count = 0
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def write_tar(
    entries: Sequence[Entry],
    files: Mapping[str, Path],
    generated: Mapping[str, bytes],
    out: BinaryIO,
) -> None:
    """Write the content of `entries`, in this order, as a tar stream. Each member is a regular
    file with the entry's path, a fixed owner and time, and the mode 0644 or 0755. The stream
    is a valid tar archive also when `entries` is empty. If a source file changed since
    `build_manifest`, raise `RemoteError`."""
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            if entry.path in generated:
                source: BinaryIO = io.BytesIO(generated[entry.path])
            elif entry.path in files:
                source = Path(files[entry.path]).open("rb")
            else:
                raise ValueError(f"no file or content for {entry.path!r}")
            with source:
                member = tarfile.TarInfo(entry.path)
                member.size = entry.size
                member.mode = 0o755 if entry.executable else 0o644
                reader = _HashingReader(source)
                try:
                    archive.addfile(member, reader)  # type: ignore[arg-type]
                except OSError:
                    if reader.count < entry.size:  # tarfile found the file shorter than its size
                        raise RemoteError(f"{entry.path} changed while it was packed") from None
                    raise
            if reader.digest.hexdigest() != entry.sha256:
                raise RemoteError(f"{entry.path} changed while it was packed")


def copy_verified(entry: Entry, source: IO[bytes], destination: IO[bytes]) -> None:
    """Copy exactly `entry.size` bytes from `source` to `destination` and check their SHA-256.
    Raise `RemoteProtocolError` if `source` ends early and `RemoteFetchError` if the digest is not
    the one in `entry`. The caller must not publish what it wrote before this returns."""
    digest = hashlib.sha256()
    remaining = entry.size
    while remaining:
        try:
            chunk = source.read(min(_CHUNK, remaining))
        except tarfile.TarError as error:
            raise RemoteProtocolError(f"{entry.path}: {error}") from error
        if not chunk:
            raise RemoteProtocolError(f"{entry.path}: the data ended before its size")
        digest.update(chunk)
        destination.write(chunk)
        remaining -= len(chunk)
    if digest.hexdigest() != entry.sha256:
        raise RemoteFetchError(f"{entry.path} does not match its digest")


def iter_tar(expected: Sequence[Entry], tar_stream: BinaryIO) -> Iterator[tuple[Entry, IO[bytes]]]:
    """Read a tar stream that must hold one regular file for each entry of `expected`, in any
    order, and nothing else. Yield each entry with a stream of its content, which is valid until
    the next step. Read the content with `copy_verified`. After the last member, read the rest of
    `tar_stream`, so that a stream of frames is at its end.

    Raise `RemoteProtocolError` for a member that is not a regular file (a link, a directory or a
    device), a name that is not safe, a name that is not in `expected` or comes twice, a size
    that is not the entry's, a missing member and a stream that is not a tar archive."""
    wanted = {entry.path: entry for entry in expected}
    if len(wanted) != len(expected):
        raise RemoteProtocolError("the manifest names a path twice")
    seen: set[str] = set()
    try:
        archive = tarfile.open(fileobj=tar_stream, mode="r|")
    except tarfile.TarError as error:
        raise RemoteProtocolError(f"the data is not a tar archive: {error}") from error
    with archive:
        members = iter(archive)
        while True:
            try:
                member = next(members, None)
            except tarfile.TarError as error:
                raise RemoteProtocolError(f"the tar archive is damaged: {error}") from error
            if member is None:
                break
            name = str(safe_member_path(member.name))
            if not member.isreg() or member.issparse():
                raise RemoteProtocolError(f"the tar member {name!r} is not a regular file")
            if name not in wanted:
                raise RemoteProtocolError(f"the tar member {name!r} was not asked for")
            if name in seen:
                raise RemoteProtocolError(f"the tar member {name!r} comes twice")
            entry = wanted[name]
            if member.size != entry.size:
                raise RemoteProtocolError(f"the tar member {name!r} has the wrong size")
            content = archive.extractfile(member)
            if content is None:
                raise RemoteProtocolError(f"the tar member {name!r} has no content")
            seen.add(name)
            yield entry, content
    if missing := wanted.keys() - seen:
        raise RemoteProtocolError(f"the tar archive lacks {sorted(missing)}")
    while tar_stream.read(_CHUNK):
        pass


# ---------------------------------------------------------------------------------------------
# Directory descriptors: every step is relative to the descriptor above and never follows a link.


def _open_directory(name: str, parent: int | None = None) -> int:
    """Open a real directory. A link, even to a directory, is an error (`O_NOFOLLOW`)."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _open_root(root: Path, create: bool) -> int | None:
    """Open the root of a sync. Return `None` if it is not there and `create` is false."""
    if create:
        root.mkdir(parents=True, exist_ok=True)
    try:
        return _open_directory(os.fspath(root))
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RemoteError(f"{root} is not a real directory") from error
        raise


def _descend(root_fd: int, parts: Sequence[str], create: bool) -> int | None:
    """Open the directory `parts` below `root_fd` and return its descriptor (the caller closes
    it). If a part is missing or is not a real directory, return `None` when `create` is false.
    When `create` is true, remove a link or file at that place and make the directory."""
    current = os.dup(root_fd)
    for part in parts:
        try:
            following = _step(current, part, create)
        finally:
            os.close(current)
        if following is None:
            return None
        current = following
    return current


def _step(parent: int, name: str, create: bool) -> int | None:
    try:
        return _open_directory(name, parent)
    except FileNotFoundError:
        pass
    except OSError as error:
        if error.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise
        if not create:
            return None
        _remove_entry(parent, name)  # a link or a file where a directory belongs
    if not create:
        return None
    os.mkdir(name, 0o755, dir_fd=parent)
    return _open_directory(name, parent)


def _remove_entry(parent: int, name: str) -> None:
    """Remove a file, a link (itself), or a directory with everything in it."""
    try:
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(status.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    child = _open_directory(name, parent)
    try:
        with os.scandir(child) as listing:
            names = [item.name for item in listing]
        for item in names:
            _remove_entry(child, item)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def _prune(directory: int) -> bool:
    """Remove the empty directories below `directory`. Return whether it is empty now."""
    with os.scandir(directory) as listing:
        items = [(item.name, item.is_dir(follow_symlinks=False)) for item in listing]
    remaining = 0
    for name, is_directory in items:
        if is_directory:
            child = _open_directory(name, directory)
            try:
                empty = _prune(child)
            finally:
                os.close(child)
            if empty:
                _remove_entry(directory, name)
                continue
        remaining += 1
    return remaining == 0


# ---------------------------------------------------------------------------------------------


def _matches(parent: int, name: str, entry: Entry) -> bool:
    """Whether the regular file `name` (never a link) in `parent` is what `entry` says."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:  # missing, or a link
        return False
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size != entry.size:
            return False
        if bool(status.st_mode & stat.S_IXUSR) != entry.executable:
            return False
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest() == entry.sha256


def _is_current(root_fd: int, entry: Entry) -> bool:
    *parents, name = entry.path.split("/")
    parent = _descend(root_fd, parents, create=False)
    if parent is None:
        return False
    try:
        return _matches(parent, name, entry)
    finally:
        os.close(parent)


def _find_stale(directory: int, prefix: str, wanted: set[str], needed: set[str]) -> list[str]:
    """The paths below `directory` of everything but directories that is not in the manifest. A
    link is such a thing, and is never entered. A path that the manifest needs as a directory is
    not stale: the sync replaces it."""
    with os.scandir(directory) as listing:
        items = sorted((item.name, item.is_dir(follow_symlinks=False)) for item in listing)
    stale: list[str] = []
    for name, is_directory in items:
        path = prefix + name
        if is_directory:
            child = _open_directory(name, directory)
            try:
                stale += _find_stale(child, path + "/", wanted, needed)
            finally:
                os.close(child)
        elif path not in wanted and path not in needed:
            stale.append(path)
    return stale


def plan_sync(root: Path, entries: Sequence[Entry]) -> SyncPlan:
    """Compare the files below `root` with the manifest, by size, SHA-256 and executable bit
    (never by time). `missing` lists the entries to receive, in manifest order. `stale` lists the
    paths below `root` that are not directories and not in the manifest."""
    _check_entries(entries)
    root_fd = _open_root(Path(root), create=False)
    if root_fd is None:
        return SyncPlan([entry.path for entry in entries], [])
    try:
        missing = [entry.path for entry in entries if not _is_current(root_fd, entry)]
        wanted = {entry.path for entry in entries}
        needed = {str(parent) for path in wanted for parent in PurePosixPath(path).parents}
        return SyncPlan(missing, _find_stale(root_fd, "", wanted, needed))
    finally:
        os.close(root_fd)


def _write_member(parent: int, name: str, entry: Entry, source: IO[bytes]) -> None:
    """Write `source` to a temporary file in `parent`, check it, and rename it to `name`."""
    temporary = f".xeda-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as out:
            copy_verified(entry, source, out)
            os.fchmod(descriptor, 0o755 if entry.executable else 0o644)
        try:
            is_directory = stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            is_directory = False
        if is_directory:
            _remove_entry(parent, name)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        _remove_entry(parent, temporary)
        raise


def apply_sync(root: Path, entries: Sequence[Entry], plan: SyncPlan, tar_stream: BinaryIO) -> None:
    """Make the files below `root` what the manifest says.

    `tar_stream` must hold exactly the entries in `plan.missing`. Each file is checked against
    its SHA-256 in a temporary file and only then renamed to its name, so a file is never
    partly written. A link where a directory belongs, and a directory where a file belongs, are
    replaced. A link at the name of a file is replaced, not written through. Then `plan.stale`
    is removed, and the directories that are empty after that."""
    _check_entries(entries)
    by_path = {entry.path: entry for entry in entries}
    if len(set(plan.missing)) != len(plan.missing) or not set(plan.missing) <= by_path.keys():
        raise RemoteProtocolError("the plan asks for a path that is not in the manifest")
    for path in plan.stale:
        safe_member_path(path)
        if path in by_path:
            raise RemoteProtocolError(f"the plan removes {path!r}, which is in the manifest")
    root_fd = _open_root(Path(root), create=True)
    assert root_fd is not None
    try:
        for entry, source in iter_tar([by_path[path] for path in plan.missing], tar_stream):
            *parents, name = entry.path.split("/")
            parent = _descend(root_fd, parents, create=True)
            assert parent is not None
            try:
                _write_member(parent, name, entry, source)
            finally:
                os.close(parent)
        for path in plan.stale:
            *parents, name = path.split("/")
            if (parent := _descend(root_fd, parents, create=False)) is not None:
                try:
                    _remove_entry(parent, name)
                finally:
                    os.close(parent)
        _prune(root_fd)
    finally:
        os.close(root_fd)
