"""Durable run-directory locks and read-only verification of a completed producer generation."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ..digest import FileRecord, record_file, unknown_record
from ..listing import directory_files
from ..run_dir import RunDirectoryError

if sys.platform != "win32":
    import fcntl

__all__ = ["lock_file", "run_dir_lock", "run_dir_read_lock", "CompletedRun"]


@dataclass
class _Hold:
    file: TextIO
    exclusive: bool
    readers: int = 0
    writers: int = 0


# Authorization belongs to this process and execution thread, never to another thread/worker.
_held: dict[tuple[int, int, Path], _Hold] = {}


def _after_fork() -> None:
    # Closing our inherited copy releases no parent lock. LOCK_UN would release it for both.
    for hold in _held.values():
        hold.file.close()
    _held.clear()


if sys.platform != "win32":
    os.register_at_fork(after_in_child=_after_fork)


def lock_file(run_path: Path) -> Path:
    """The durable lock beside the resolved directory, shared by ordinary in-root aliases."""
    run_path = run_path.resolve()
    return run_path.parent / f"{run_path.name}.lock"


@contextlib.contextmanager
def _lock(run_path: Path, exclusive: bool) -> Iterator[None]:
    if sys.platform == "win32":
        yield
        return
    path = lock_file(run_path)
    key = (os.getpid(), threading.get_ident(), path)
    hold = _held.get(key)
    if hold is not None:
        if exclusive and not hold.exclusive:
            raise RunDirectoryError(f"{run_path} is held by a shared read lease; cannot write it")
        hold.writers += int(exclusive)
        hold.readers += int(not exclusive)
        try:
            yield
        finally:
            hold.writers -= int(exclusive)
            hold.readers -= int(not exclusive)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            if lock_file(run_path) != path:
                raise RunDirectoryError(f"{run_path} changed while acquiring its lock")
            _held[key] = _Hold(f, exclusive, int(not exclusive), int(exclusive))
            try:
                yield
            finally:
                del _held[key]
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def run_dir_lock(run_path: Path) -> contextlib.AbstractContextManager[None]:
    """Wait for an exclusive writer lock. Reenter only within the owning writer execution."""
    return _lock(run_path, exclusive=True)


def run_dir_read_lock(run_path: Path) -> contextlib.AbstractContextManager[None]:
    """Hold a shared producer lease; an enclosing writer retains its exclusive OS lock."""
    return _lock(run_path, exclusive=False)


@dataclass(frozen=True)
class CompletedRun:
    """Files and bookkeeping captured before releasing the producer's exclusive lock.

    Use the trace's existing records where their metadata is conclusive. Include the trace
    itself, success/results/settings and every run entry, including unregistered outputs, so
    an untraced producer or byte-identical declared output cannot hide a changed generation.
    Verification uses the same walker and file records, without clocks, refreshes or writes.
    """

    run_path: Path
    extra_files: tuple[Path, ...]
    files: Mapping[Path, tuple[int, FileRecord]]
    trusted_before_ns: int | None

    @classmethod
    def capture(
        cls,
        run_path: Path,
        extra_files: Sequence[Path] = (),
        previous: Mapping[str, FileRecord] | None = None,
        trusted_before_ns: int | None = None,
    ) -> CompletedRun:
        root = run_path.resolve()
        extras = tuple(extra_files)
        files = {}
        for path in sorted({root, *directory_files(root), *extras}):
            st = path.lstat()
            try:
                record = record_file(
                    path,
                    (previous or {}).get(str(path)),
                    trusted_before_ns,
                    follow_symlinks=False,
                )
            except OSError:
                record = unknown_record(path)
            # record_file hashes a link's target too; the lease also pins the link's own inode.
            files[path] = (st.st_dev, FileRecord.of(st, record.sha))
        return cls(root, extras, files, trusted_before_ns)

    def verify(self) -> None:
        """Refuse missing, changed or uncertain completion evidence under a shared lease."""
        if any(record.unknown for _dev, record in self.files.values()):
            raise RunDirectoryError(
                f"{self.run_path} has uncertain files; cannot verify its read lease"
            )
        current = self.capture(
            self.run_path,
            self.extra_files,
            {str(path): record for path, (_dev, record) in self.files.items()},
            self.trusted_before_ns,
        )
        if current.files != self.files:
            raise RunDirectoryError(f"{self.run_path} changed before acquiring its read lease")
