"""Content digests of files, and records that let a later check avoid reading them.

A record is `(size, mtime_ns, sha)`. A later check trusts a record by its metadata alone only
when the file was last modified well before the record was taken (`trusted_before_ns`): a file
written within the same timestamp tick could have changed after it was hashed without its
metadata showing it. This is git's "racy git" rule.

A record whose `sha` is `MODIFIED_DURING_RUN` stands for a file that changed while a run was
using it, so what the run read is unknown: it never matches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from .dataclass import XedaBaseModel

#: Timestamp granularity assumed for the racy check: 2 s covers FAT and HFS+; on filesystems
#: with nanosecond timestamps it costs one extra hash of files written just before a record.
RACY_NS = 2_000_000_000

_CHUNK = 1 << 20

#: The digest of a file that was written while the run that read it was going on: what the run
#: read is unknown, so this record never matches the file (`FileRecord.unknown`).
MODIFIED_DURING_RUN = "unknown: modified during the run"


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
    sha: str

    @property
    def unknown(self) -> bool:
        """The file changed while a run used it: its content then is unknown."""
        return self.sha == MODIFIED_DURING_RUN


def written_since(path: Path, since_ns: int) -> bool:
    """Whether `path` was written (created, modified, or its times set) at or after `since_ns`.

    The inode change time counts too: setting a file's mtime back (`touch -d`, a copy that
    preserves times) still changes it, so a file edited during a run and given an old mtime is
    noticed. A change of permissions or links counts as well, which at worst costs a re-run.
    """
    st = path.stat()
    return max(st.st_mtime_ns, st.st_ctime_ns) >= since_ns


def unknown_record(path: Path) -> FileRecord:
    """The record of a file that was written during the run that read it (or that vanished
    before it could be recorded)."""
    try:
        st = path.stat()
    except OSError:
        return FileRecord(size=-1, mtime_ns=-1, sha=MODIFIED_DURING_RUN)
    return FileRecord(size=st.st_size, mtime_ns=st.st_mtime_ns, sha=MODIFIED_DURING_RUN)


def record_file(
    path: Path,
    previous: Optional[FileRecord] = None,
    trusted_before_ns: Optional[int] = None,
) -> FileRecord:
    """The record of `path` now. Reuses `previous`'s hash without reading the file when size
    and mtime are unchanged and the file is not racy; otherwise hashes the content."""
    st = path.stat()
    if (
        previous is not None
        and trusted_before_ns is not None
        and st.st_size == previous.size
        and st.st_mtime_ns == previous.mtime_ns
        and st.st_mtime_ns + RACY_NS < trusted_before_ns
    ):
        return previous
    return FileRecord(size=st.st_size, mtime_ns=st.st_mtime_ns, sha=content_digest(path))
