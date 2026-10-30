"""Durable locks of Xeda, and read-only verification of the completed run of a producer.

Four kinds of resource have a lock: a run directory, a storage (generation storage or dependency
storage), a design tree that a generator writes into, and a delivery destination. A lock is shared
(a reader) or exclusive (a writer). The lock of a run directory or of a storage is a file beside
it. The lock of a design tree is a file under `<run root>/.locks/trees/`, and the lock of a
destination is a file under `<run root>/.locks/deliveries/`.

A launch takes all its locks at once (`acquire_all`), in one order: run directories, then
storages, then design trees, then destinations, each kind by resource key. Except for a delivery, a thread waits only
while it holds no lock of Xeda.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from ..digest import FileRecord, record_file, unknown_record
from ..errors import LeaseHeldError
from ..listing import directory_files
from ..run_dir import RunDirectory, RunDirectoryError, refuse_link_on_way
from ..cancel import inherited
from ..utils import Canceled

if sys.platform != "win32":
    import fcntl

__all__ = [
    "LockRequest",
    "LockSet",
    "LockWaitCanceled",
    "acquire_all",
    "acquired_hook",
    "callback_guard",
    "destination_lock",
    "held_by_this_thread",
    "lock_file",
    "refuse_if_held",
    "run_dir_lock",
    "run_dir_read_lock",
    "storage_lock",
    "wait_hook",
    "CompletedRun",
]

Kind = Literal["run_dir", "storage", "tree", "destination"]

#: The order of the kinds in an acquisition.
_KIND_ORDER: dict[str, int] = {"run_dir": 0, "storage": 1, "tree": 2, "destination": 3}

#: Seams for tests. When `wait_hook` is set, Xeda calls it with the path of a lock file just before
#: it waits for that lock, that is, when another holder keeps the lock. A test waits on this call
#: instead of sleeping. `acquired_hook` is called with the path of each lock file just after Xeda
#: took that lock.
wait_hook: Callable[[Path], None] | None = None
acquired_hook: Callable[[Path], None] | None = None

#: The longest pause between two attempts to take a lock that another holder keeps.
_MAX_PAUSE = 0.05


class LockWaitCanceled(Canceled):
    """The `canceled` predicate of a wait for a lock became true. The call holds no lock."""


@dataclass(frozen=True)
class LockRequest:
    """One lock that `acquire_all` is asked for.

    `path` is the run directory, the storage, the root of the design tree or the destination file.
    The lock file follows from it and from the run root. A shared lock (`exclusive` false) is a lease of a reader.
    """

    kind: Kind
    path: Path
    exclusive: bool


@dataclass
class _Hold:
    """The open lock file of one resource, held by one thread of this process."""

    file: TextIO
    path: Path  # what the request named: a run directory, storage, design root or destination
    exclusive: bool
    holders: int = 0


#: The locks held by this process, by thread and resource key. The authorization belongs to this
#: process and execution thread, never to another thread or worker.
_held: dict[tuple[int, int, tuple[int, int, str]], _Hold] = {}

_local = threading.local()


def _after_fork() -> None:
    # Closing the inherited copy releases no lock of the parent. LOCK_UN would release it for both.
    for hold in _held.values():
        hold.file.close()
    _held.clear()


if sys.platform != "win32":
    os.register_at_fork(after_in_child=_after_fork)


def lock_file(run_path: Path, run_root: Path | None = None) -> Path:
    """Return the durable lock file beside the run directory: `<run directory>.lock` in its parent.

    The parent is resolved, so ordinary aliases of the parent inside the run root share one
    lock. The last component is resolved only when a run root is given and it is a link that
    stays inside the run root. The lock is then beside what the link leads to, so a launch
    through the name of the link and one through the real name serialize on one lock. A link
    leading out of the run root, or nowhere, is a `RunDirectoryError`, before anything is
    created. Without a run root the last component is never resolved (a link is itself, as
    `RunDirectory.inside` has it), so a run directory that has become a link cannot send the
    lock file to wherever the link leads."""
    run_path = Path(os.path.abspath(run_path))
    if run_root is not None:
        run_root = Path(run_root).resolve()
        _check_inside(run_path, run_root)
        if run_path.is_symlink():
            run_path = Path(os.path.realpath(run_path))
    return Path(os.path.realpath(run_path.parent)) / f"{run_path.name}.lock"


def _check_inside(run_path: Path, run_root: Path) -> None:
    """Refuse a run directory that is not inside `run_root`.

    Such a directory is reached through a link between the run root and its parent
    (`refuse_link_on_way`: the design, the target, the mirror of a host), or it is itself a link
    that leads out of the run root or nowhere."""
    refuse_link_on_way(run_path.parent, run_root)
    root = Path(os.path.realpath(run_root))
    parent = Path(os.path.realpath(run_path.parent))
    if parent != root and not parent.is_relative_to(root):
        raise RunDirectoryError(
            f"{run_path} is reached through a link that leads out of the run root {run_root}: "
            "it is not locked"
        )
    if run_path.is_symlink():
        leads_to = Path(os.path.realpath(run_path))
        if not leads_to.exists() or leads_to == root or not leads_to.is_relative_to(root):
            raise RunDirectoryError(
                f"{run_path} is a link that leads out of the run root {run_root}, or nowhere: "
                "it is not locked"
            )


def _folds_case(directory: Path) -> bool:
    """Return whether the file system of `directory` compares names without regard to letter case.

    The answer comes from `pathconf` (`PC_CASE_SENSITIVE`), where the system has it. Anywhere else
    the file system is taken to keep case."""
    if sys.platform == "darwin":
        name: str | int = 11  # _PC_CASE_SENSITIVE of <sys/unistd.h>: Python has no name for it
    elif "PC_CASE_SENSITIVE" in os.pathconf_names:
        name = "PC_CASE_SENSITIVE"
    else:
        return False
    try:
        return os.pathconf(directory, name) == 0
    except (OSError, ValueError):
        return False


def _leaf(name: str, directory: Path) -> str:
    """Return `name` as the file system of `directory` compares it: in the form NFC, and folded
    to one letter case where that file system ignores case."""
    text = unicodedata.normalize("NFC", name)
    return text.casefold() if _folds_case(directory) else text


def _resource_key(lock_path: Path) -> tuple[int, int, str]:
    """Return the identity of a lock file: the device and inode of its existing parent directory,
    and its name, normalized (`_leaf`).

    Two spellings of one lock file give one key. Every lock of Xeda is named, sorted, merged and
    owned by this key."""
    parent = lock_path.parent
    found = os.stat(parent)
    return (found.st_dev, found.st_ino, _leaf(lock_path.name, parent))


def _destination_lock_file(destination: Path, run_root: Path) -> Path:
    """Return the lock file of a delivery destination: `<run root>/.locks/deliveries/<name>.lock`.

    The name is a hash of the identity of the destination: the device and inode of the nearest
    existing directory above it, and the names below that directory (normalized, `_leaf`). So
    two spellings of one destination share a lock, and so does a destination that does not exist
    yet, in a directory that does not exist yet. The last component is never followed.

    The name changes when a directory above the destination is made. A delivery makes those
    directories before it takes this lock (`Deliveries.make_directories`), and a call that took
    the old name finds the change after the lock and starts again."""
    place = Path(os.path.abspath(destination))
    below = [place.name]
    directory = place.parent
    while not os.path.isdir(directory) and directory.parent != directory:
        below.append(directory.name)
        directory = directory.parent
    real = Path(os.path.realpath(directory))
    found = os.stat(real)
    names = [_leaf(name, real) for name in reversed(below)]
    digest = hashlib.sha256(repr((found.st_dev, found.st_ino, names)).encode()).hexdigest()
    return Path(os.path.realpath(run_root)) / ".locks" / "deliveries" / f"{digest[:32]}.lock"


def _tree_lock_file(root: Path, run_root: Path) -> Path:
    """Return the lock file of a design tree: `<run root>/.locks/trees/<name>.lock`.

    The name is a hash of the identity (device and inode) of the root of the tree, with every
    link resolved. So every name of one design root shares one lock, in one run root."""
    found = os.stat(os.path.realpath(root))
    digest = hashlib.sha256(repr((found.st_dev, found.st_ino)).encode()).hexdigest()
    return Path(os.path.realpath(run_root)) / ".locks" / "trees" / f"{digest[:32]}.lock"


@dataclass(frozen=True)
class _Target:
    """One lock to take: what was asked for, and the lock file and key it resolves to now."""

    request: LockRequest
    file: Path
    key: tuple[int, int, str]

    @property
    def order(self) -> tuple[int, tuple[int, int, str]]:
        return (_KIND_ORDER[self.request.kind], self.key)


def _resolve(request: LockRequest, run_root: Path | None) -> _Target:
    """Return where `request` locks now. The lock file lies in an existing directory."""
    if request.kind in ("destination", "tree"):
        if run_root is None:
            raise RunDirectoryError(f"the lock of a {request.kind} needs a run root")
        if request.kind == "tree":
            file = _tree_lock_file(request.path, run_root)
        else:
            file = _destination_lock_file(request.path, run_root)
    else:
        file = lock_file(request.path, run_root)
    if run_root is not None:
        # Before any directory is made: a link at `.locks` or below would send it elsewhere.
        RunDirectory(run_root, run_root).unlinked(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    return _Target(request, file, _resource_key(file))


def _targets(requests: Iterable[LockRequest], run_root: Path | None) -> list[_Target]:
    """Resolve the requests, merge those of one lock (an exclusive request wins), and sort them
    into the order of acquisition."""
    merged: dict[tuple[str, tuple[int, int, str]], _Target] = {}
    for request in requests:
        target = _resolve(request, run_root)
        found = merged.get((request.kind, target.key))
        if found is None or (request.exclusive and not found.request.exclusive):
            merged[(request.kind, target.key)] = target
    return sorted(merged.values(), key=lambda target: target.order)


def _open(path: Path, run_root: Path | None) -> TextIO:
    """Open the lock file `path`, which is created when it is not there."""
    if run_root is not None:
        # The lock file is Xeda's own: a link at its name would send the lock to another inode.
        RunDirectory(run_root, run_root).unlinked(path)
    # Opened without following a link, so none can replace the file between the check and here.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    return os.fdopen(descriptor, "a")


def _try_lock(file: TextIO, exclusive: bool, path: Path) -> bool:
    """Take the OS lock on `file` (the lock file `path`) if no other holder keeps it. Never wait.

    A system that cannot lock at all (no lock space left) is a `RunDirectoryError`."""
    try:
        fcntl.flock(file.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as error:
        raise RunDirectoryError(
            f"cannot take the {'exclusive' if exclusive else 'shared'} lock {path}: {error}"
        ) from error
    return True


def _wait_for(
    file: TextIO, exclusive: bool, path: Path, canceled: Callable[[], bool] | None
) -> None:
    """Take the OS lock on `file`, and wait for it with short pauses.

    The wait observes `canceled` between the attempts, and a `KeyboardInterrupt` ends it. It
    tells `wait_hook` once, when it first finds the lock kept."""
    pause = 0.001
    told = False
    while not _try_lock(file, exclusive, path):
        if not told:
            told = True
            if wait_hook is not None:
                wait_hook(path)
        if canceled is not None and canceled():
            raise LockWaitCanceled(f"canceled while waiting for the lock {path}")
        time.sleep(pause)
        pause = min(pause * 2, _MAX_PAUSE)


def _guard(path: Path) -> None:
    """Refuse a new lock to the thread that runs a callback of Xeda while it holds locks."""
    if getattr(_local, "callbacks", 0) > 0:
        raise LeaseHeldError(
            f"{path} cannot be locked here: this callback runs while Xeda holds locks, and it "
            "may only reuse the locks that are held",
            code="lease_held",
            path=path,
        )


def _owner(target: _Target) -> tuple[int, int, tuple[int, int, str]]:
    return (os.getpid(), threading.get_ident(), target.key)


class LockSet:
    """The locks that one `acquire_all` took, until `release`.

    The set is not reentrant for other threads: the locks belong to the thread that took them."""

    def __init__(
        self,
        taken: Sequence[_Target],
        run_root: Path | None,
        owner: tuple[int, int] | None,
    ) -> None:
        self._taken = list(taken)
        self._run_root = run_root
        self._owner = owner
        self._released = False

    @property
    def requests(self) -> tuple[LockRequest, ...]:
        """The locks of the set, merged, in the order they were taken."""
        return tuple(target.request for target in self._taken)

    def mode_of(self, kind: Kind, path: Path) -> Literal["exclusive", "shared"] | None:
        """Return the mode in which the set holds the lock of `path`, or None if it holds none."""
        if sys.platform == "win32":
            want = (0, 0, os.path.abspath(path))
        else:
            want = _resolve(LockRequest(kind, path, False), self._run_root).key
        for target in self._taken:
            if target.request.kind == kind and target.key == want:
                return "exclusive" if target.request.exclusive else "shared"
        return None

    def release(self) -> None:
        """Release every lock of the set. Releasing again does nothing."""
        if self._released:
            return
        self._released = True
        for target in reversed(self._taken):
            _release(target, self._owner)

    def __enter__(self) -> LockSet:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _release(target: _Target, owner: tuple[int, int] | None) -> None:
    if owner is None:
        return
    key = (*owner, target.key)
    hold = _held.get(key)
    if hold is None:
        return
    hold.holders -= 1
    if hold.holders > 0:
        return
    del _held[key]
    try:
        fcntl.flock(hold.file.fileno(), fcntl.LOCK_UN)
    finally:
        hold.file.close()


def _take(
    target: _Target,
    run_root: Path | None,
    *,
    wait: bool,
    canceled: Callable[[], bool] | None,
) -> bool:
    """Take one lock for this thread. Return False when `wait` is false and another holder keeps
    it. A lock that this thread holds already is held again, with no OS call."""
    key = _owner(target)
    request = target.request
    hold = _held.get(key)
    if hold is not None:
        if request.exclusive and not hold.exclusive:
            raise RunDirectoryError(
                f"{request.path} is held by a shared read lease; cannot write it"
            )
        hold.holders += 1
        return True
    _guard(request.path)
    file = _open(target.file, run_root)
    try:
        if not _try_lock(file, request.exclusive, target.file):
            if not wait:
                file.close()
                return False
            _wait_for(file, request.exclusive, target.file, canceled)
    except BaseException:
        file.close()
        raise
    _held[key] = _Hold(file, request.path, request.exclusive, 1)
    return True


def _wait_free(target: _Target, run_root: Path | None, canceled: Callable[[], bool] | None) -> None:
    """Wait until the lock of `target` could be taken, holding nothing, and take it for no time."""
    with _open(target.file, run_root) as file:
        _wait_for(file, target.request.exclusive, target.file, canceled)
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _still_there(target: _Target, run_root: Path | None) -> bool:
    """Return whether the request of `target` still resolves to the lock it took."""
    try:
        return _resolve(target.request, run_root).key == target.key
    except (OSError, RunDirectoryError):
        return False


def _acquire_set(
    requests: Sequence[LockRequest],
    run_root: Path | None,
    canceled: Callable[[], bool] | None,
    in_order: bool,
) -> LockSet:
    if run_root is not None:
        run_root = Path(run_root).resolve()
    if sys.platform == "win32":
        return LockSet(_targets_without_locks(requests), run_root, None)
    owner = (os.getpid(), threading.get_ident())
    while True:
        targets = _targets(requests, run_root)
        taken: list[_Target] = []
        busy: _Target | None = None
        again = False
        try:
            for index, target in enumerate(targets):
                if not _take(target, run_root, wait=in_order or index == 0, canceled=canceled):
                    busy = target
                    break
                taken.append(target)
                if not _still_there(target, run_root):
                    again = True
                    break
                if acquired_hook is not None:
                    acquired_hook(target.file)
        except BaseException:
            for target in reversed(taken):
                _release(target, owner)
            raise
        if busy is None and not again:
            return LockSet(taken, run_root, owner)
        for target in reversed(taken):
            _release(target, owner)
        if busy is not None:
            _wait_free(busy, run_root, canceled)


def _targets_without_locks(requests: Sequence[LockRequest]) -> list[_Target]:
    """The targets of a platform with no lock files: the set only remembers what was asked."""
    merged: dict[tuple[str, str], _Target] = {}
    for request in requests:
        name = os.path.abspath(request.path)
        found = merged.get((request.kind, name))
        if found is None or (request.exclusive and not found.request.exclusive):
            merged[(request.kind, name)] = _Target(request, Path(name), (0, 0, name))
    return sorted(merged.values(), key=lambda target: target.order)


@contextlib.contextmanager
def acquire_all(
    requests: Sequence[LockRequest],
    *,
    run_root: Path | None,
    canceled: Callable[[], bool] | None = None,
    wait_in_order: bool = False,
) -> Iterator[LockSet]:
    """Take every lock of `requests` or none, and release them when the block ends.

    Requests for one lock merge (an exclusive request wins). The order of acquisition is: run
    directories, then storages, then design trees, then destinations, each kind by resource key. The call waits only
    for the first lock, and holds nothing while it waits. It tries each later lock without
    waiting. When another holder keeps one, it releases every lock it took, waits until that lock
    alone is free, and starts again. After each lock, it checks that the request still resolves
    to the lock it took, and starts again if not. So two calls with the same requests, in any
    order, cannot wait for each other.

    A lock that the calling thread holds already is held again, with no wait. A shared lock
    cannot become an exclusive one: release it, then ask for the exclusive one.

    `canceled` is asked while the call waits. When it says yes, the call releases all it took and
    raises `LockWaitCanceled`. Without `canceled`, a call in a launch observes the request of the
    launch (`cancel.launch_canceled`), and `cancel.never_canceled` opts out.

    With `wait_in_order`, the call waits for each lock in turn and keeps the locks it took: this
    is for a thread that holds locks of an earlier kind already (the delivery of a launch), and
    the order makes the waits end.

    `run_root` is the run root that the locks lie in. A run directory or a storage that does not
    lie inside it, or that is reached through a link out of it, is a `RunDirectoryError` before
    anything is created."""
    locks = _acquire_set(list(requests), run_root, inherited(canceled), wait_in_order)
    try:
        yield locks
    finally:
        locks.release()


def lock_scope(
    kind: Kind, path: Path, exclusive: bool, run_root: Path | None = None
) -> contextlib.AbstractContextManager[LockSet]:
    """Return the context manager that holds one lock."""
    return acquire_all([LockRequest(kind, path, exclusive)], run_root=run_root)


def _held_path() -> Path | None:
    ident = (os.getpid(), threading.get_ident())
    return next((hold.path for key, hold in _held.items() if key[:2] == ident), None)


def held_by_this_thread() -> bool:
    """Return whether the calling thread holds any lock of Xeda."""
    return _held_path() is not None


def refuse_if_held() -> None:
    """Raise `LeaseHeldError` when the calling thread holds a lock of Xeda.

    A launch, a sweep or a scrub starts with this call: a thread that waits for a lock while it
    holds one could wait for itself."""
    path = _held_path()
    if path is not None:
        raise LeaseHeldError(
            f"this thread holds the lock of {path}: it cannot start a launch or a scrub",
            code="lease_held",
            path=path,
        )


@contextlib.contextmanager
def callback_guard() -> Iterator[None]:
    """Mark the calling thread as inside a callback that Xeda calls while it holds locks.

    In the block, a lock that the thread holds is held again at once, and any other lock raises
    `LeaseHeldError`."""
    _local.callbacks = getattr(_local, "callbacks", 0) + 1
    try:
        yield
    finally:
        _local.callbacks -= 1


def run_dir_lock(
    run_path: Path, run_root: Path | None = None
) -> contextlib.AbstractContextManager[LockSet]:
    """Wait for an exclusive writer lock, and reenter it only within the owning writer execution.

    With `run_root`, a run directory whose parent leads out of the run root is refused
    (`RunDirectoryError`) before anything is created."""
    return lock_scope("run_dir", run_path, True, run_root)


def run_dir_read_lock(
    run_path: Path, run_root: Path | None = None
) -> contextlib.AbstractContextManager[LockSet]:
    """Hold a shared lease on a producer. An enclosing writer keeps its exclusive OS lock."""
    return lock_scope("run_dir", run_path, False, run_root)


def storage_lock(
    storage: Path, *, exclusive: bool, run_root: Path
) -> contextlib.AbstractContextManager[LockSet]:
    """Hold the lock of a storage directory under the run root, shared (a reader) or exclusive.

    A storage is a directory that a launch writes once and many launches read: the output of a
    design generator, or the checkout of a Git dependency. Its lock file lies beside it, as the
    lock file of a run directory does. A writer holds the exclusive lock while it writes. Every
    reader holds the shared lock for as long as it reads a file of the storage, so nothing
    replaces or removes the storage meanwhile. The lock is reentrant in one thread, as a
    run-directory lock is, but a shared lock cannot become an exclusive one: release the first,
    then ask for the second.

    A storage that does not lie inside `run_root`, or that is reached through a link out of it,
    is a `RunDirectoryError` before anything is created."""
    return lock_scope("storage", storage, exclusive, run_root)


def destination_lock(
    destination: Path, run_root: Path, *, canceled: Callable[[], bool] | None = None
) -> contextlib.AbstractContextManager[LockSet]:
    """Hold the exclusive lock of a delivery destination, which need not exist.

    The lock file lies under the run root, so the lock serializes the launches that share that
    run root. A delivery holds it from its last check of the destination through the replacement
    and the record."""
    return acquire_all(
        [LockRequest("destination", destination, True)],
        run_root=run_root,
        canceled=canceled,
    )


@dataclass(frozen=True)
class CompletedRun:
    """Files and bookkeeping captured before the exclusive lock of the producer is released.

    The capture uses the existing records of the trace where their metadata is conclusive. It
    includes the trace itself, the results and settings files, and every run entry, unregistered
    outputs included, so that an untraced producer or a byte-identical declared output cannot
    hide a changed run. Verification uses the same walker and file records, without clocks,
    refreshes or writes.
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
