"""File records: metadata decides whether to look, content decides whether a file changed."""

import os
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


def test_a_new_record_hashes_the_content(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    record = record_file(f)
    assert record == FileRecord(size=1, mtime_ns=10**18, sha=content_digest(f))


def test_unchanged_metadata_well_before_the_trace_is_trusted_without_reading(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    previous = FileRecord(size=1, mtime_ns=10**18, sha="recorded")
    # the trace was written long after the file's mtime: metadata is conclusive
    assert record_file(f, previous, trusted_before_ns=10**18 + 10 * RACY_NS).sha == "recorded"


def test_a_racy_file_is_hashed_even_when_metadata_matches(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    previous = FileRecord(size=1, mtime_ns=10**18, sha="recorded")
    # modified within the timestamp granularity of the trace: it may have changed after hashing
    assert record_file(
        f, previous, trusted_before_ns=10**18 + RACY_NS // 2
    ).sha == content_digest(f)


def test_changed_metadata_with_the_same_content_keeps_the_hash(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    before = record_file(f)
    os.utime(f, ns=(10**18 + 5, 10**18 + 5))  # `touch`
    after = record_file(f, before, trusted_before_ns=10**18 + 10 * RACY_NS)
    assert after.sha == before.sha and after.mtime_ns == 10**18 + 5


def test_changed_content_changes_the_hash(tmp_path):
    f = _write(tmp_path / "a.v", "x", 10**18)
    before = record_file(f)
    _write(f, "y", 10**18)  # same size and mtime restored: only a racy check or a hash sees it
    assert record_file(f, before, trusted_before_ns=10**18 + RACY_NS // 2).sha != before.sha
