"""Content digests of files, and records that let a later check avoid reading them.

A record is `(size, mtime_ns, ctime_ns, inode, sha)`. A later check trusts a record by its
metadata alone only when all four match and the file last changed -- its mtime or its inode
change time -- well before the record was taken (`trusted_before_ns`): a file written within
the same timestamp tick could have changed after it was hashed without its metadata showing it.
This is git's "racy git" rule. The inode change time is what no one can set: an edit given back
its old mtime (`os.utime`, `touch -r`) still changes it, as a change of permissions or a move
does; a copy that keeps the mtime (`cp -p`) is another inode. Each of these costs a hash, never a
wrong reuse.

A record whose `sha` is `MODIFIED_DURING_RUN` stands for a file that changed while a run was
using it, so what the run read is unknown: it never matches. So does one whose `sha` is
`UNRECORDED_BEFORE_RUN`: a file the run was found to read only afterwards (a depfile's entry), on
a file system other than the run directory's, whose clock cannot tell whether it changed while the
run went on and of which no record from before the run exists.

A symbolic link can be recorded as itself (`record_file(..., follow_symlinks=False)`, how a run
directory's outputs are recorded): by its target and, when that is a regular file, by the
file's content as well (trusted by the file's metadata like any file's); a link to a directory,
or to nothing, by its target alone.

A directory is recorded by its metadata and `DIRECTORY_DIGEST`: an entry of a listing
(`listing.directory_files`), whose own entries are listed on their own -- and one that cannot be
listed cannot be recorded (`PermissionError`), since what it holds is unknown. A special file (a
FIFO, a socket, a device) by its metadata and `SPECIAL_DIGEST` + its kind, never read: reading it
could block.

A whole tree is digested by its files' names and content (`digest_files` over `package_files`),
with no metadata and no record to trust: that is how the installed xeda package
(`trace_inputs.xeda_code_digest`) is identified.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import List, Optional

from .dataclass import XedaBaseModel
from .listing import directory_files

#: Timestamp granularity assumed for the racy check: 2 s covers FAT and HFS+; on filesystems
#: with nanosecond timestamps it costs one extra hash of files written just before a record.
RACY_NS = 2_000_000_000

_CHUNK = 1 << 20

#: The digest of a file that was written while the run that read it was going on: what the run
#: read is unknown, so this record never matches the file (`FileRecord.unknown`).
MODIFIED_DURING_RUN = "unknown: modified during the run"
#: The digest of a file first found to be read after the run, with no record from before it, on
#: another file system than the run directory's: whether it changed during the run is unknown.
UNRECORDED_BEFORE_RUN = "unknown: no record from before the run"

#: How the digest of a symbolic link recorded as itself begins; a digest of its target text
#: follows, then, for a link to a regular file, ":" and that file's content digest. A content
#: digest never begins so: a link and a file are never mistaken for each other.
SYMLINK_DIGEST = "symlink:"

#: The digest of a directory: constant, since what it holds is listed entry by entry; a content
#: digest never is.
DIRECTORY_DIGEST = "directory"

#: How the digest of a special file begins, its kind (`fifo`, `socket`, `device`) following: it
#: is never read.
SPECIAL_DIGEST = "special:"

#: The name prefix of the marker `filesystem_time_ns` creates, and removes, to read a clock.
TIME_MARKER_PREFIX = ".xeda-time-"


def content_digest(path: Path) -> str:
    """SHA3-256 of the file's content, first 128 bits, as `DesignSource.content_hash`."""
    h = hashlib.sha3_256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()[:32]


class FileRecord(XedaBaseModel):
    """What was known about a file when a record was taken."""

    size: int
    mtime_ns: int
    #: the inode change time (`st_ctime_ns` on POSIX): set by every write, `utime`, `chmod`, ...
    ctime_ns: int
    #: the inode number (`st_ino`)
    inode: int
    sha: str

    @classmethod
    def of(cls, st: os.stat_result, sha: str) -> FileRecord:
        """The record of a file whose status is `st` and whose digest is `sha`."""
        return cls(
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            ctime_ns=st.st_ctime_ns,
            inode=st.st_ino,
            sha=sha,
        )

    def settled_before(self, time_ns: int) -> bool:
        """Whether the file last changed -- its mtime or its inode change time -- well before
        `time_ns`, outside the racy window: then, recorded at `time_ns`, it can be trusted by
        this metadata later."""
        return max(self.mtime_ns, self.ctime_ns) + RACY_NS < time_ns

    def trusted(self, st: os.stat_result, trusted_before_ns: int) -> bool:
        """Whether a file whose status is `st` is, without reading it, the file this record was
        taken of: the same size, mtime, inode change time and inode, and last changed well before
        `trusted_before_ns`, the time the record was taken (`settled_before`)."""
        now = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        recorded = (self.size, self.mtime_ns, self.ctime_ns, self.inode)
        return now == recorded and self.settled_before(trusted_before_ns)

    @property
    def unknown(self) -> bool:
        """The file changed while a run used it, or nothing tells whether it did: its content
        then is unknown."""
        return self.sha in (MODIFIED_DURING_RUN, UNRECORDED_BEFORE_RUN)


def written_since(path: Path, since_ns: int) -> bool:
    """Whether `path` was written (created, modified, or its times set) at or after `since_ns`.

    The inode change time counts too: setting a file's mtime back (`touch -d`, a copy that
    preserves times) still changes it, so a file edited during a run and given an old mtime is
    noticed. A change of permissions or links counts as well, which at worst costs a re-run.
    """
    st = path.stat()
    return max(st.st_mtime_ns, st.st_ctime_ns) >= since_ns


def unknown_record(path: Path, why: str = MODIFIED_DURING_RUN) -> FileRecord:
    """The record of a file that was written during the run that read it (or that vanished
    before it could be recorded) -- or, with `why` `UNRECORDED_BEFORE_RUN`, of which nothing
    tells whether it was."""
    try:
        st = path.stat()
    except OSError:
        return FileRecord(size=-1, mtime_ns=-1, ctime_ns=-1, inode=-1, sha=why)
    return FileRecord.of(st, why)


def filesystem_time_ns(directory: Path) -> int:
    """Read this filesystem's clock by touching a temporary marker, created and removed in
    `directory` (a run directory)."""
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
    """The record of `path` now. Reuses `previous`'s hash without reading the file when
    `previous.trusted` says its metadata is conclusive; otherwise hashes the content, calling
    `before_reading` first (a check reads its clock then, see `trace.check_trace`).

    Without `follow_symlinks`, a symbolic link is recorded as itself (`_record_link`). A
    directory or a special file is recorded by its metadata alone, never read (see the
    module)."""
    st = path.stat() if follow_symlinks else path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return _record_link(path, st, previous, trusted_before_ns, before_reading)
    if stat.S_ISDIR(st.st_mode):
        if not os.access(path, os.R_OK | os.X_OK):
            # what it holds was not listed: nothing vouches for it (the record is unknown)
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
    """The record of the symbolic link `path` (whose own status is `link_st`) as itself: by its
    target, read afresh every time (it is short), and -- when it points to a regular file -- by
    that file's content, with the file's metadata, so that the content is read only when that
    metadata cannot vouch for it (`FileRecord.trusted`). A link to a directory, or to nothing,
    is recorded by its target alone, with its own metadata: nothing is read."""
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
    """The files that make up a Python package directory (`listing.directory_files`, resolved):
    everything under it but compiled bytecode (`__pycache__`, `.pyc`) and hidden files, which
    Python and editors create."""
    top = directory.resolve()
    return sorted(
        p
        for p in directory_files(top, skip=frozenset({"__pycache__"}), follow_links=True)
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo")
        and not any(part.startswith(".") for part in p.relative_to(top).parts)
    )


def digest_files(files: Sequence[Path], base: Path) -> str:
    """A digest of `files`' names -- relative to `base` where they lie under it -- and content."""
    h = hashlib.sha3_256()
    for file in files:
        name = file.relative_to(base) if file.is_relative_to(base) else file
        h.update(name.as_posix().encode() + b"\0")
        h.update(content_digest(file).encode())
    return h.hexdigest()[:32]
