"""File records: metadata decides whether to look, content decides whether a file changed."""

import os
import time
from pathlib import Path

from xeda.digest import RACY_NS, FileRecord, content_digest, record_file


def _write(path: Path, text: str, mtime_ns: int) -> Path:
    path.write_text(text)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_content_digest_matches_design_source_hashing(tmp_path):
    import hashlib

    f = _write(tmp_path / "a.v", "module a; endmodule\n", 10**18)
    assert content_digest(f) == hashlib.sha3_256(f.read_bytes()).hexdigest()[:32]


def _recorded(path: Path, sha: str = "recorded") -> FileRecord:
    """A record of `path` as it is now, with the digest `sha`."""
    st = path.stat()
    return FileRecord(
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        ctime_ns=st.st_ctime_ns,
        inode=st.st_ino,
        sha=sha,
    )


def _long_after(path: Path) -> int:
    """A time long after `path` last changed, its inode change time included."""
    st = path.stat()
    return max(st.st_mtime_ns, st.st_ctime_ns) + 10 * RACY_NS


def test_a_new_record_hashes_the_content(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    assert record_file(f) == _recorded(f, content_digest(f))


def test_unchanged_metadata_well_before_the_trace_is_trusted_without_reading(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    # the record was taken long after the file last changed: metadata is conclusive
    assert record_file(f, _recorded(f), trusted_before_ns=_long_after(f)).sha == "recorded"


def test_a_racy_file_is_hashed_even_when_metadata_matches(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    # its inode changed within the timestamp granularity of the record: it may have changed
    # after hashing
    recent = f.stat().st_ctime_ns + RACY_NS // 2
    assert record_file(f, _recorded(f), trusted_before_ns=recent).sha == content_digest(f)


def test_changed_metadata_with_the_same_content_keeps_the_hash(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    before = record_file(f)
    os.utime(f, ns=(10**18 + 5, 10**18 + 5))  # `touch`
    after = record_file(f, before, trusted_before_ns=_long_after(f))
    assert after.sha == before.sha and after.mtime_ns == 10**18 + 5


def test_changed_content_changes_the_hash(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    before = record_file(f)
    # same size and mtime restored: the inode change time tells. Where that clock is coarse,
    # rewrite until it has advanced
    for _ in range(200):
        _write(f, "y", 10**18)
        if f.stat().st_ctime_ns != before.ctime_ns:
            break
        time.sleep(0.01)
    assert f.stat().st_ctime_ns != before.ctime_ns
    assert record_file(f, before, trusted_before_ns=_long_after(f)).sha != before.sha


def test_the_same_metadata_on_another_inode_is_hashed(tmp_path):
    """A copy that keeps size and mtime (`cp -p`) is another file: it is read."""
    f = _write(tmp_path / "a.v", "x", 10**18)
    previous = _recorded(f)
    moved = previous.model_copy(update={"inode": previous.inode + 1})
    assert record_file(f, moved, trusted_before_ns=_long_after(f)).sha == content_digest(f)
