"""The generated sources that a generator writes into its design tree, and their copies.

A tree entry of `rtl.sources` (`design.storage_name` is None for it) is a path or a pattern below
the design root that the generator writes. Xeda reads the files that the tree entries match just
before and just after the generator runs (`observe`). It takes the files that the run wrote
(`written_names`) and copies each into the storage of the generator (`copy_tree_file`). The flows
read only these copies, never the tree.

A file counts as written by the run when the state of the file at its path (device, inode, size,
modification and change times) or its content changed during the run. A file that the run did not
write is never used, so a tool that skips an output it finds up to date must be made to write it.

The walk opens every directory by descriptor, relative to the one above it, and follows no
symbolic link. A link at a matched name, or at a directory on the way to one, is a
`GenerationError`. The walk needs a POSIX system.

The matching rule is that of `generation.matches_in`, so each copy matches its declaration again
in the storage: `*` is the only pattern character and it stays in one part. A pattern part matches
no name that starts with `.` unless the pattern part starts with `.` too. `?`, `[` and `]` are
literal characters. A part without `*`, `?` or `[` names one entry, as the file system compares
names. Only regular files match.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path

from .generation import GenerationError, escaped_pattern
from .run_dir import OutputState
from .utils import copy_fd, replacing_file

__all__ = ["Observed", "copy_tree_file", "observe", "part_matches", "written_names"]

#: The state of the file at a tree entry when Xeda read it, and the digest of its content.
Observed = tuple[OutputState, str]

#: A seam for tests: when set, `copy_tree_file` calls it with the path of the file it copies, after
#: it opened the file and checked its state, and before it copies the content.
copy_hook: Callable[[Path], None] | None = None

_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
# O_NONBLOCK: a FIFO put in place of a file after the check must not block the open.
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CHUNK = 1 << 20


def part_matches(pattern: str, name: str) -> bool:
    """Return whether `name`, one part of a path, fits `pattern`, one part of a declaration.

    The rule is that of `generation.matches_in` (a glob): `*` is the only pattern character, and a
    name that starts with `.` fits only a pattern that starts with `.`."""
    if name.startswith(".") and not pattern.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, escaped_pattern(pattern))


def _is_pattern(part: str) -> bool:
    # A glob lists the directory for a part with one of these characters, and looks a part
    # without one up by name.
    return any(mark in part for mark in "*?[")


def _require_posix() -> None:
    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise GenerationError(
            "a generated entry in the design tree needs a POSIX system; write "
            "`$XEDA_GENERATED/<name>`"
        )


def _open(name: str | Path, flags: int, dir_fd: int | None = None) -> int:
    return os.open(name, flags, dir_fd=dir_fd)  # read only: no flag of this module writes


def _link(path: Path) -> GenerationError:
    return GenerationError(
        f"`{path}` is a symbolic link: Xeda follows no link below the design root to a generated "
        "source. Make it a directory or file, or let the generator write into `$XEDA_GENERATED`"
    )


def _unreadable(path: Path, error: OSError) -> GenerationError:
    return GenerationError(f"cannot read `{path}`: {error.strerror or error}")


def _changed(path: Path) -> GenerationError:
    return GenerationError(f"`{path}` changed while Xeda copied it: another program writes it")


def _state(found: os.stat_result) -> OutputState:
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


def _digest(fd: int) -> str:
    """Return the digest of the whole content of the open file `fd`, whatever its offset is."""
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, _CHUNK, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _open_at(fd: int, name: str, flags: int, path: Path) -> int:
    """Open `name` relative to the directory `fd` with `flags`, which follow no link. `path` is
    its whole path, for the messages."""
    try:
        return _open(name, flags, fd)
    except OSError as error:
        try:
            found = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            found = None
        if found is not None and stat.S_ISLNK(found.st_mode):
            raise _link(path) from error
        raise _unreadable(path, error) from error


def _open_root(root: Path) -> int:
    _require_posix()
    try:
        return _open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise _unreadable(root, error) from error


def _visit(
    root: Path,
    fd: int,
    parts: Sequence[str],
    done: tuple[str, ...],
    visit: Callable[[int, str, str], None],
) -> None:
    """Call `visit(directory, name, relative)` for each regular file below the directory `fd` (at
    `done` below `root`) that `parts` match. `name` lies in the open directory `directory`, and
    `relative` is its path below `root`."""
    part, rest = parts[0], parts[1:]
    if _is_pattern(part):
        try:
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries if part_matches(part, entry.name))
        except OSError as error:
            raise _unreadable(root.joinpath(*done), error) from error
    else:
        names = [part]
    for name in names:
        place = (*done, name)
        path = root.joinpath(*place)
        try:
            found = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise _unreadable(path, error) from error
        if stat.S_ISLNK(found.st_mode):
            raise _link(path)
        if not rest:
            if stat.S_ISREG(found.st_mode):
                visit(fd, name, "/".join(place))
        elif stat.S_ISDIR(found.st_mode):
            child = _open_at(fd, name, _DIRECTORY, path)
            try:
                _visit(root, child, rest, place, visit)
            finally:
                os.close(child)


def _read(fd: int, name: str, path: Path) -> Observed:
    file = _open_at(fd, name, _FILE, path)
    try:
        found = os.fstat(file)
        if not stat.S_ISREG(found.st_mode):
            raise GenerationError(f"`{path}` changed while Xeda read it: another program writes it")
        return _state(found), _digest(file)
    finally:
        os.close(file)


def observe(root: Path, patterns: Sequence[str]) -> dict[str, Observed]:
    """Return the state and the content digest of each file below `root` that one of `patterns`
    (tree entries, POSIX paths relative to `root`) matches, by its path relative to `root`.

    The function writes nothing. A link at a matched name or at a directory on the way to one, or
    a directory it cannot read, is a `GenerationError`. A directory that is not there matches
    nothing."""
    found: dict[str, Observed] = {}

    def visit(fd: int, name: str, relative: str) -> None:
        if relative not in found:
            found[relative] = _read(fd, name, root / relative)

    fd = _open_root(root)
    try:
        for pattern in patterns:
            _visit(root, fd, pattern.split("/"), (), visit)
    finally:
        os.close(fd)
    return found


def written_names(before: Mapping[str, Observed], after: Mapping[str, Observed]) -> list[str]:
    """Return the names of `after` that the run wrote, sorted: each is absent from `before`, or
    has another state or another content digest there.

    The digest catches a rewrite that leaves the state as it was (the same size within one tick of
    a coarse clock). It can only make a file count as written when its bytes did change."""
    return sorted(name for name, seen in after.items() if before.get(name) != seen)


def copy_tree_file(root: Path, name: str, observed: Observed, destination: Path) -> None:
    """Copy the file `name` below `root` to `destination`, if it is still what `observed` says.

    Every part of `name` is opened relative to the one above it, following no link. The state of
    the open file must equal the observed state before and after the copy, and the digest of the
    copy must equal the observed digest. Otherwise the call is a `GenerationError` and the
    destination is not made. The copy replaces what is at `destination` and never writes through
    it (`utils.replacing_file`). The caller has located `destination` (`RunDirectory.writable`)."""
    path = root / name
    *directories, leaf = name.split("/")
    with ExitStack() as stack:
        fd = _open_root(root)
        stack.callback(os.close, fd)
        done = root
        for part in directories:
            done = done / part
            fd = _open_at(fd, part, _DIRECTORY, done)
            stack.callback(os.close, fd)
        file = _open_at(fd, leaf, _FILE, path)
        stack.callback(os.close, file)
        if _state(os.fstat(file)) != observed[0]:
            raise _changed(path)
        if copy_hook is not None:
            copy_hook(path)
        with replacing_file(destination, "wb") as stream:
            copy_fd(file, stream.fileno())
            # The temporary file of `replacing_file` is open for reading too (`mkstemp`).
            if _state(os.fstat(file)) != observed[0] or _digest(stream.fileno()) != observed[1]:
                raise _changed(path)
