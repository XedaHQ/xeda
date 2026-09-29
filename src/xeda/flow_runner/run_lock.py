"""A lock per run directory, so that launches in concurrent processes (parallel DSE variants that
share a dependency) take turns with a directory instead of cleaning and running it at once."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Dict, Iterator

if sys.platform != "win32":
    import fcntl

__all__ = ["CWD_LOCK", "lock_file", "run_dir_lock"]

#: the lock file inside a run directory that is not xeda's (the one `--cwd` names)
CWD_LOCK = ".xeda.lock"

#: the lock files this process holds, with how many nested launches hold each
_held: Dict[Path, int] = {}


def lock_file(run_path: Path, inside: bool = False) -> Path:
    """`<run_path>.lock`, beside the directory: emptying or removing it never deletes the lock.
    With `inside`, `<run_path>/.xeda.lock`: for the directory `--cwd` names, which xeda never
    empties, and whose parent is none of xeda's business."""
    if inside:
        return run_path / CWD_LOCK
    return run_path.parent / f"{run_path.name}.lock"


@contextlib.contextmanager
def run_dir_lock(run_path: Path, inside: bool = False) -> Iterator[None]:
    """Hold an exclusive lock on `run_path`, waiting for any other process holding it.

    POSIX `flock` on `lock_file(run_path, inside)`, which is released when this process exits
    however it exits. Reentrant within a process: a launch nested in another one for the same
    directory does not wait for itself. Where `fcntl` is unavailable (Windows), there is no lock.
    """
    if sys.platform == "win32":
        yield
        return
    path = lock_file(run_path, inside)
    key = Path(os.path.abspath(path))
    if key in _held:
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        _held[key] = 1
        try:
            yield
        finally:
            del _held[key]
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
