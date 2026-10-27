"""Content digests of files, and records that let a later check avoid reading them.

A record is `(size, mtime_ns, ctime_ns, inode, sha)`. A later check trusts a record by its
metadata alone only when size, mtime, inode change time and inode all match, and the file last
changed (its mtime or its inode change time) well before the record was taken
(`trusted_before_ns`). A file written within the same timestamp tick could have changed after it
was hashed without its metadata showing it. This is git's "racy git" rule. Nobody can set the
inode change time: an edit given back its old mtime (`os.utime`, `touch -r`) still changes it, as
a change of permissions or a move does. A copy that keeps the mtime (`cp -p`) is another inode.
Each of these costs a hash and never causes a wrong reuse.

A record whose `sha` is `MODIFIED_DURING_RUN` stands for a file that changed while a run was
using it, so what the run read is unknown. It never matches. A record whose `sha` is
`UNRECORDED_BEFORE_RUN` never matches either. It stands for a file that the run was found to read
only afterwards (a depfile entry) on a file system other than the run directory's. The clock of
that file system cannot tell whether the file changed during the run, and no record from before
the run exists.

A symbolic link can be recorded as itself (`record_file(..., follow_symlinks=False)`, how the
outputs of a run directory are recorded). The record holds its target and, when the link leads to
a regular file, also the content of that file (trusted by the file's metadata like any file's).
A link to a directory, or to nothing, is recorded by its target alone.

A directory is recorded by its metadata and `DIRECTORY_DIGEST`. It is an entry of a listing
(`listing.directory_files`), and its own entries are listed on their own. A directory that cannot
be listed cannot be recorded (`PermissionError`), since what it holds is unknown. A special file
(a FIFO, a socket, a device) is recorded by its metadata and `SPECIAL_DIGEST` plus its kind. It is
never read, because reading it could block.

A whole tree is digested by the names and content of its files (`content_pairs` and
`digest_pairs` over `package_files`). This identifies the installed `xeda` package and the code of
a plugin flow (`code_digest`), which keeps a record of each file in the process and reads a file
again only when its record cannot vouch for it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import List, Optional

from .dataclass import XedaBaseModel
from .listing import directory_files
from .run_root import RUN_ROOT_MARKER, is_run_root

#: The timestamp granularity that the racy check assumes. 2 s covers FAT and HFS+. On file systems
#: with nanosecond timestamps it costs one extra hash of files written just before a record.
RACY_NS = 2_000_000_000

_CHUNK = 1 << 20

#: The digest of a file that was written while the run that read it was going on: what the run
#: read is unknown, so this record never matches the file (`FileRecord.unknown`).
MODIFIED_DURING_RUN = "unknown: modified during the run"
#: The digest of a file first found to be read after the run, with no record from before it, on
#: another file system than the run directory's: whether it changed during the run is unknown.
UNRECORDED_BEFORE_RUN = "unknown: no record from before the run"

#: The start of the digest of a symbolic link recorded as itself. A digest of its target text
#: follows. For a link to a regular file, ":" and that file's content digest follow. A content
#: digest never begins so, which keeps a link and a file apart.
SYMLINK_DIGEST = "symlink:"

#: The digest of a directory. It is constant, since the entries of a directory are listed one by
#: one. A content digest is never equal to it.
DIRECTORY_DIGEST = "directory"

#: The start of the digest of a special file. Its kind (`fifo`, `socket`, `device`) follows. The
#: file is never read.
SPECIAL_DIGEST = "special:"

#: The name prefix of the marker that `filesystem_time_ns` creates and removes to read a clock.
TIME_MARKER_PREFIX = ".xeda-time-"


def content_digest(path: Path) -> str:
    """Return the SHA3-256 of the file content, cut to 128 bits (`Source.content_hash`)."""
    h = hashlib.sha3_256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()[:32]


class FileRecord(XedaBaseModel):
    """What was known about a file when a record was taken."""

    size: int
    mtime_ns: int
    #: The inode change time (`st_ctime_ns` on POSIX). Every write, `utime` and `chmod` sets it.
    ctime_ns: int
    #: The inode number (`st_ino`).
    inode: int
    sha: str

    @classmethod
    def of(cls, st: os.stat_result, sha: str) -> FileRecord:
        """Return the record of a file whose status is `st` and whose digest is `sha`."""
        return cls(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            ctime_ns=st.st_ctime_ns,
            inode=st.st_ino,
            sha=sha,
        )

    def settled_before(self, time_ns: int) -> bool:
        """Return whether the file last changed (its mtime or its inode change time) well before
        `time_ns`, outside the racy window.

        A file recorded at `time_ns` can then be trusted by its metadata later.
        """
        return max(self.mtime_ns, self.ctime_ns) + RACY_NS < time_ns

    def trusted(self, st: os.stat_result, trusted_before_ns: int) -> bool:
        """Return whether a file with status `st` is the file this record was taken of, without
        reading the file.

        It is when size, mtime, inode change time and inode are the same, and the file last
        changed well before `trusted_before_ns`, the time the record was taken
        (`settled_before`).
        """
        now = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        recorded = (self.size, self.mtime_ns, self.ctime_ns, self.inode)
        return now == recorded and self.settled_before(trusted_before_ns)

    @property
    def unknown(self) -> bool:
        """Whether the file changed while a run used it, or nothing tells whether it did.

        Its content is then unknown.
        """
        return self.sha in (MODIFIED_DURING_RUN, UNRECORDED_BEFORE_RUN)


def written_since(path: Path, since_ns: int) -> bool:
    """Return whether `path` was written (created, modified, or its times set) at or after
    `since_ns`.

    The inode change time counts too. Setting the mtime of a file back (`touch -d`, a copy that
    preserves times) still changes it, so a file edited during a run and given an old mtime is
    noticed. A change of permissions or links counts as well, which at worst causes another run.
    """
    st = path.stat()
    return max(st.st_mtime_ns, st.st_ctime_ns) >= since_ns


def unknown_record(path: Path, why: str = MODIFIED_DURING_RUN) -> FileRecord:
    """Return the record of a file that was written during the run that read it, or that vanished
    before it could be recorded.

    With `why` set to `UNRECORDED_BEFORE_RUN`, the record is for a file of which nothing tells
    whether it was written during the run.
    """
    try:
        st = path.stat()
    except OSError:
        return FileRecord(size=-1, mtime_ns=-1, ctime_ns=-1, inode=-1, sha=why)
    return FileRecord.of(st, why)


def filesystem_time_ns(directory: Path) -> int:
    """Return the clock of the file system of `directory` (a run directory), in nanoseconds.

    The clock is read from the mtime of a temporary marker that is created and removed in
    `directory`.
    """
    fd, name = tempfile.mkstemp(prefix=TIME_MARKER_PREFIX, dir=directory)
    os.close(fd)
    marker = Path(name)
    try:
        os.utime(marker, None)
        return marker.stat().st_mtime_ns
    finally:
        marker.unlink(missing_ok=True)


def record_file(
    path: Path,
    previous: Optional[FileRecord] = None,
    trusted_before_ns: Optional[int] = None,
    follow_symlinks: bool = True,
    before_reading: Callable[[], None] | None = None,
) -> FileRecord:
    """Return the record of `path` now.

    Reuse the hash of `previous`, without reading the file, when `previous.trusted` says that the
    metadata is conclusive. Otherwise hash the content, calling `before_reading` first (a check
    reads its clock then, see `trace.check_trace`).

    Without `follow_symlinks`, a symbolic link is recorded as itself (`_record_link`). A
    directory or a special file is recorded by its metadata alone and is never read (see the
    module docstring).
    """
    st = path.stat() if follow_symlinks else path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return _record_link(path, st, previous, trusted_before_ns, before_reading)
    if stat.S_ISDIR(st.st_mode):
        if not os.access(path, os.R_OK | os.X_OK):
            # what it holds was not listed, so nothing vouches for it
            raise PermissionError(errno.EACCES, "a directory that cannot be listed", str(path))
        return FileRecord.of(st, DIRECTORY_DIGEST)  # its entries are listed on their own
    if not stat.S_ISREG(st.st_mode):
        return FileRecord.of(st, SPECIAL_DIGEST + _special_kind(st.st_mode))  # it could block
    if (
        previous is not None
        and trusted_before_ns is not None
        and previous.trusted(st, trusted_before_ns)
    ):
        return previous
    if before_reading is not None:
        before_reading()
    return FileRecord.of(st, content_digest(path))


def _special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "device"


def _record_link(
    path: Path,
    link_st: os.stat_result,
    previous: FileRecord | None,
    trusted_before_ns: int | None,
    before_reading: Callable[[], None] | None,
) -> FileRecord:
    """Return the record of the symbolic link `path` (whose own status is `link_st`) as itself.

    The record holds the target, read afresh every time (it is short). When the link leads to a
    regular file, it also holds that file's content with the file's metadata, so the content is
    read only when that metadata cannot vouch for it (`FileRecord.trusted`). A link to a
    directory, or to nothing, is recorded by its target alone, with its own metadata. Nothing is
    read.
    """
    link = SYMLINK_DIGEST + hashlib.sha3_256(os.fsencode(os.readlink(path))).hexdigest()[:32]
    try:
        st = path.stat()
    except OSError:
        return FileRecord.of(link_st, link)  # to nothing
    if not stat.S_ISREG(st.st_mode):
        return FileRecord.of(link_st, link)
    if (
        previous is not None
        and trusted_before_ns is not None
        and previous.sha.startswith(link + ":")
        and previous.trusted(st, trusted_before_ns)
    ):
        return previous
    if before_reading is not None:
        before_reading()
    return FileRecord.of(st, f"{link}:{content_digest(path)}")


def package_files(directory: Path) -> List[Path]:
    """Return the files that make up a Python package directory (`listing.directory_files`,
    resolved).

    These are all files under it except compiled bytecode (`__pycache__`, `.pyc`), hidden files,
    which Python and editors create, and the files of a run root (`run_root.is_run_root`), which
    a launch started in the directory writes.
    """
    top = directory.resolve()
    listed = directory_files(top, skip=frozenset({"__pycache__"}), follow_links=True)
    run_roots = tuple(
        p.parent for p in listed if p.name == RUN_ROOT_MARKER and is_run_root(p.parent)
    )
    return sorted(
        p
        for p in listed
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo")
        and not any(part.startswith(".") for part in p.relative_to(top).parts)
        and not any(p.is_relative_to(root) for root in run_roots)
    )


def content_pairs(
    entries: Iterable[tuple[str, Path]], read: Callable[[Path], str] = content_digest
) -> list[tuple[str, str]]:
    """Return the `(name, content digest)` pairs of the `(name, file)` entries.

    `read` gives the content digest of a file."""
    return [(name, read(file)) for name, file in entries]


def digest_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """Return a digest of the `(name, content digest)` pairs, whatever their order.

    A pair that occurs twice counts twice."""
    h = hashlib.sha3_256()
    for name, sha in sorted(pairs):
        h.update(name.encode() + b"\0")
        h.update(sha.encode())
    return h.hexdigest()[:32]
