"""The lock of a storage directory: one writer or many readers, and a way for a test to wait.

A storage is a directory under the run root that one launch writes and many launches read (the
output of a design generator). Its lock is a file beside it, as for a run directory.
"""

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from xeda.flow_runner import run_lock
from xeda.flow_runner.run_lock import lock_file, storage_lock
from xeda.run_dir import RunDirectoryError

from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no storage locks")

#: What another process can take of a lock right now, without waiting.
PROBE = textwrap.dedent("""
    import fcntl, sys
    mode = fcntl.LOCK_EX if sys.argv[2] == "exclusive" else fcntl.LOCK_SH
    with open(sys.argv[1], "a") as f:
        try:
            fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            print("blocked")
        else:
            print("free")
    """)


def _probe(storage: Path, mode: str = "exclusive") -> str:
    """Whether another process could lock `storage` in `mode` now: "free" or "blocked"."""
    found = subprocess.run(
        [sys.executable, "-c", PROBE, str(lock_file(storage)), mode],
        capture_output=True,
        text=True,
        check=True,
    )
    return found.stdout.strip()


def _storage(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "xeda_run"
    return run_root, run_root / ".generated" / "design" / "0123456789abcdef"


def test_the_lock_file_lies_beside_the_storage(tmp_path):
    run_root, storage = _storage(tmp_path)
    with storage_lock(storage, exclusive=False, run_root=run_root):
        assert lock_file(storage).parent == storage.parent
        assert lock_file(storage).name == "0123456789abcdef.lock"
        assert lock_file(storage).exists()
    assert not storage.exists(), "the lock makes no storage"


def test_a_writer_keeps_everyone_else_out(tmp_path):
    run_root, storage = _storage(tmp_path)
    with storage_lock(storage, exclusive=True, run_root=run_root):
        assert _probe(storage, "exclusive") == "blocked"
        assert _probe(storage, "shared") == "blocked"
    assert _probe(storage, "exclusive") == "free"


def test_readers_share_the_storage_and_keep_a_writer_out(tmp_path):
    run_root, storage = _storage(tmp_path)
    with storage_lock(storage, exclusive=False, run_root=run_root):
        with storage_lock(storage, exclusive=False, run_root=run_root):
            assert _probe(storage, "shared") == "free"
            assert _probe(storage, "exclusive") == "blocked"
        assert _probe(storage, "exclusive") == "blocked", "the outer reader still holds it"
    assert _probe(storage, "exclusive") == "free"


def test_a_reader_cannot_become_a_writer(tmp_path):
    run_root, storage = _storage(tmp_path)
    with storage_lock(storage, exclusive=False, run_root=run_root):
        with pytest.raises(RunDirectoryError, match="shared"):
            with storage_lock(storage, exclusive=True, run_root=run_root):
                pytest.fail("a shared lease cannot authorize a writer")


def test_a_writer_may_read_what_it_wrote(tmp_path):
    run_root, storage = _storage(tmp_path)
    with storage_lock(storage, exclusive=True, run_root=run_root):
        with storage_lock(storage, exclusive=False, run_root=run_root):
            assert _probe(storage, "shared") == "blocked", "the writer keeps its exclusive lock"
    assert _probe(storage, "exclusive") == "free"


def test_a_storage_outside_the_run_root_is_refused(tmp_path):
    run_root, _ = _storage(tmp_path)
    with pytest.raises(RunDirectoryError, match="run root"):
        with storage_lock(tmp_path / "elsewhere" / "key", exclusive=True, run_root=run_root):
            pytest.fail("a lock outside the run root is not taken")
    assert not (tmp_path / "elsewhere").exists()


def test_a_storage_reached_through_a_link_out_of_the_run_root_is_refused(tmp_path):
    run_root, _ = _storage(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_root / ".generated").mkdir(parents=True)
    (run_root / ".generated" / "design").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunDirectoryError):
        with storage_lock(
            run_root / ".generated" / "design" / "key", exclusive=True, run_root=run_root
        ):
            pytest.fail("a lock behind a link out of the run root is not taken")
    assert list(outside.iterdir()) == []


def test_the_wait_hook_is_called_when_the_lock_is_held_elsewhere(tmp_path, monkeypatch):
    run_root, storage = _storage(tmp_path)
    waiting = threading.Event()
    release = threading.Event()
    holding = threading.Event()
    seen: list[Path] = []

    def hook(path: Path) -> None:
        seen.append(path)
        waiting.set()

    def hold() -> None:
        with storage_lock(storage, exclusive=True, run_root=run_root):
            holding.set()
            assert release.wait(HANG_GUARD)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(HANG_GUARD)
    monkeypatch.setattr(run_lock, "wait_hook", hook)
    took = threading.Event()

    def take() -> None:
        with storage_lock(storage, exclusive=False, run_root=run_root):
            took.set()

    reader = threading.Thread(target=take)
    reader.start()
    try:
        assert waiting.wait(HANG_GUARD), "the reader never reached the lock"
        assert not took.is_set(), "the reader got the lock while the writer held it"
    finally:
        release.set()
        holder.join(HANG_GUARD)
        reader.join(HANG_GUARD)
    assert took.is_set()
    assert seen == [lock_file(storage)]


def test_the_wait_hook_is_not_called_when_nothing_is_held(tmp_path, monkeypatch):
    run_root, storage = _storage(tmp_path)
    seen: list[Path] = []
    monkeypatch.setattr(run_lock, "wait_hook", seen.append)
    with storage_lock(storage, exclusive=True, run_root=run_root):
        pass
    with storage_lock(storage, exclusive=False, run_root=run_root):
        pass
    assert seen == []
