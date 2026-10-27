"""The locks of a launch: all of them at once, in one order, and none held while it waits.

A launch locks run directories, then storages, then delivery destinations. It takes every lock or
none, and it waits only while it holds no lock. Delivery is the one phase that holds locks and
waits.
"""

import sys
import threading
import unicodedata
from pathlib import Path

import pytest

from xeda.errors import LeaseHeldError
from xeda.flow_runner import run_lock
from xeda.flow_runner.run_lock import (
    LockRequest,
    LockWaitCanceled,
    acquire_all,
    callback_guard,
    destination_lock,
    held_by_this_thread,
    lock_file,
    refuse_if_held,
    run_dir_read_lock,
    storage_lock,
)
from xeda.run_dir import RunDirectoryError

from .test_storage_locks import _probe
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no run-directory locks")


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return a run root, a run directory in it and a storage in it."""
    root = tmp_path / "xeda_run"
    run_dir = root / "design" / "flow"
    storage = root / ".generated" / "design" / "0123456789abcdef"
    return root, run_dir, storage


def _run_dir(root: Path, name: str) -> LockRequest:
    return LockRequest("run_dir", root / "design" / name, True)


def _in_thread(function):
    """Start `function` in a thread. Return the thread and a list that gets its outcome."""
    outcome: list = []

    def run() -> None:
        try:
            outcome.append(function())
        except BaseException as error:  # noqa: BLE001 - reported by the test thread
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def _join(thread: threading.Thread) -> None:
    thread.join(HANG_GUARD)
    assert not thread.is_alive(), "a thread did not finish"


# ------------------------------------------------------------------------------------ order


def test_the_requests_are_taken_in_the_order_of_their_kinds(tmp_path, monkeypatch):
    root, run_dir, storage = _tree(tmp_path)
    taken: list[Path] = []
    monkeypatch.setattr(run_lock, "acquired_hook", taken.append)
    destination = tmp_path / "out" / "result.txt"
    requests = [
        LockRequest("destination", destination, True),
        LockRequest("storage", storage, False),
        LockRequest("run_dir", run_dir, True),
    ]
    with acquire_all(requests, run_root=root) as locks:
        assert [r.kind for r in locks.requests] == ["run_dir", "storage", "destination"]
    assert taken == [
        lock_file(run_dir),
        lock_file(storage),
        root / ".locks" / "deliveries" / taken[2].name,
    ]


def test_requests_for_one_lock_merge_and_the_exclusive_request_wins(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    requests = [LockRequest("run_dir", run_dir, False), LockRequest("run_dir", run_dir, True)]
    with acquire_all(requests, run_root=root) as locks:
        assert locks.requests == (LockRequest("run_dir", run_dir, True),)
        assert locks.mode_of("run_dir", run_dir) == "exclusive"
        assert _probe(run_dir, "shared") == "blocked"
    assert _probe(run_dir) == "free"


def test_the_mode_of_a_lock_that_the_set_does_not_hold_is_none(tmp_path):
    root, run_dir, storage = _tree(tmp_path)
    with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root) as locks:
        assert locks.mode_of("run_dir", run_dir) == "shared"
        assert locks.mode_of("storage", storage) is None
        assert locks.mode_of("run_dir", run_dir.parent / "other") is None


def _insensitive(directory: Path) -> bool:
    return run_lock._folds_case(directory)


def test_two_spellings_of_one_run_directory_are_one_lock(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    if not _insensitive(tmp_path):
        pytest.skip("this file system keeps letter case")
    spelled = run_dir.parent / run_dir.name.upper()
    requests = [LockRequest("run_dir", run_dir, False), LockRequest("run_dir", spelled, True)]
    with acquire_all(requests, run_root=root) as locks:
        assert len(locks.requests) == 1
        assert locks.mode_of("run_dir", spelled) == "exclusive"


def test_two_unicode_forms_of_one_name_are_one_lock(tmp_path):
    root, _, _ = _tree(tmp_path)
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed
    requests = [
        LockRequest("run_dir", root / "design" / composed, False),
        LockRequest("run_dir", root / "design" / decomposed, True),
    ]
    with acquire_all(requests, run_root=root) as locks:
        assert len(locks.requests) == 1


def test_a_name_behind_a_link_is_not_locked(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    run_dir.parent.mkdir(parents=True)
    alias = root / "alias"
    alias.symlink_to(run_dir.parent, target_is_directory=True)
    with pytest.raises(RunDirectoryError):
        with acquire_all([LockRequest("run_dir", alias / run_dir.name, True)], run_root=root):
            pytest.fail("a name behind a link is not locked")


def test_a_lock_that_is_held_again_by_its_thread_is_not_waited_for(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
        with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root):
            assert _probe(run_dir, "shared") == "blocked"
        assert _probe(run_dir, "shared") == "blocked"
    assert _probe(run_dir) == "free"


def test_a_shared_lock_cannot_become_an_exclusive_one(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root):
        with pytest.raises(RunDirectoryError, match="shared read lease"):
            with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
                pytest.fail("an upgrade is not taken")
        assert _probe(run_dir) == "blocked"
    assert _probe(run_dir) == "free"


def test_launches_with_opposite_graph_orders_finish(tmp_path, monkeypatch):
    """Two callers ask for the same two run directories, in opposite orders. The canonical order
    makes both take the same lock first, so one waits for the other and both end."""
    root, _, _ = _tree(tmp_path)
    names = ["a", "b"]
    first_taken = threading.Event()
    second_waits = threading.Event()
    seen: dict[str, list[str]] = {"a": [], "b": []}
    owner = {}

    def hook(path: Path) -> None:
        seen[owner[threading.get_ident()]].append(path.name)
        if threading.get_ident() != first_id[0]:
            second_waits.set()

    first_id: list[int] = [0]

    def after_first(path: Path) -> None:
        if not first_taken.is_set():
            first_id[0] = threading.get_ident()
            first_taken.set()
            assert second_waits.wait(HANG_GUARD), "the second call never waited"

    monkeypatch.setattr(run_lock, "wait_hook", hook)
    monkeypatch.setattr(run_lock, "acquired_hook", after_first)

    def launch(order: list[str], label: str):
        def run() -> str:
            owner[threading.get_ident()] = label
            with acquire_all([_run_dir(root, n) for n in order], run_root=root):
                return label

        return run

    first, first_out = _in_thread(launch(names, "a"))
    assert first_taken.wait(HANG_GUARD)
    second, second_out = _in_thread(launch(names[::-1], "b"))
    _join(first)
    _join(second)
    assert first_out == ["a"] and second_out == ["b"]


def test_a_busy_later_lock_makes_the_call_release_everything_before_it_waits(tmp_path, monkeypatch):
    """The caller needs run directory R and storage S. A reader holds S. When the caller waits for
    S, it holds nothing: another thread takes R at once. Then the reader ends and the call
    takes both."""
    root, run_dir, storage = _tree(tmp_path)
    holding = threading.Event()
    release = threading.Event()
    waits = threading.Event()
    released_reader = threading.Event()

    def read() -> None:
        with storage_lock(storage, exclusive=False, run_root=root):
            holding.set()
            assert release.wait(HANG_GUARD)

    reader, _ = _in_thread(read)
    assert holding.wait(HANG_GUARD)

    def hook(path: Path) -> None:
        if path == lock_file(storage):
            waits.set()
            assert released_reader.wait(HANG_GUARD)

    monkeypatch.setattr(run_lock, "wait_hook", hook)

    def call():
        with acquire_all(
            [LockRequest("run_dir", run_dir, True), LockRequest("storage", storage, True)],
            run_root=root,
        ) as locks:
            return [r.kind for r in locks.requests]

    caller, outcome = _in_thread(call)
    try:
        assert waits.wait(HANG_GUARD)
        assert _probe(run_dir) == "free", "the call held a lock while it waited"
        with run_dir_read_lock(run_dir, root):  # taken without blocking
            pass
    finally:
        released_reader.set()
        release.set()
    _join(reader)
    _join(caller)
    assert outcome == [["run_dir", "storage"]]


def test_the_first_lock_is_waited_for_with_nothing_held(tmp_path, monkeypatch):
    root, run_dir, _ = _tree(tmp_path)
    holding = threading.Event()
    release = threading.Event()
    held_while_waiting: list[bool] = []

    def hold() -> None:
        with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
            holding.set()
            assert release.wait(HANG_GUARD)

    holder, _ = _in_thread(hold)
    assert holding.wait(HANG_GUARD)
    waiting = threading.Event()

    def hook(path: Path) -> None:
        held_while_waiting.append(held_by_this_thread())
        waiting.set()

    monkeypatch.setattr(run_lock, "wait_hook", hook)

    def call() -> str:
        with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root):
            return "taken"

    caller, outcome = _in_thread(call)
    try:
        assert waiting.wait(HANG_GUARD)
    finally:
        release.set()
    _join(holder)
    _join(caller)
    assert outcome == ["taken"] and held_while_waiting == [False]


def test_a_path_that_resolves_to_another_lock_after_the_wait_is_taken_again(tmp_path, monkeypatch):
    """A run directory is a link inside the run root. The link leads to another directory while
    the call takes its lock: the lock taken is not the lock of the run directory now."""
    root, run_dir, _ = _tree(tmp_path)
    first = root / "design" / "first_aaaaaaaaaaaaaaaa"
    second = root / "design" / "first_bbbbbbbbbbbbbbbb"
    first.mkdir(parents=True)
    second.mkdir()
    link = root / "design" / "first"
    link.symlink_to(first, target_is_directory=True)
    real = run_lock.fcntl.flock
    state = {"calls": 0}

    def flock(descriptor, mode):
        result = real(descriptor, mode)
        state["calls"] += 1
        if state["calls"] == 1:
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
        return result

    monkeypatch.setattr(run_lock.fcntl, "flock", flock)
    with acquire_all([LockRequest("run_dir", link, True)], run_root=root) as locks:
        assert locks.mode_of("run_dir", link) == "exclusive"
        assert _probe(second) == "blocked"
        assert _probe(first) == "free", "the lock of the old target was released"


# ------------------------------------------------------------------------------ cancellation


def test_cancellation_while_waiting_for_a_lock_releases_everything(tmp_path, monkeypatch):
    root, run_dir, storage = _tree(tmp_path)
    holding = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    stop = threading.Event()

    def hold() -> None:
        with storage_lock(storage, exclusive=True, run_root=root):
            holding.set()
            assert release.wait(HANG_GUARD)

    holder, _ = _in_thread(hold)
    assert holding.wait(HANG_GUARD)
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())

    def call():
        with acquire_all(
            [LockRequest("run_dir", run_dir, True), LockRequest("storage", storage, False)],
            run_root=root,
            canceled=stop.is_set,
        ):
            return "taken"

    caller, outcome = _in_thread(call)
    try:
        assert waiting.wait(HANG_GUARD)
        stop.set()
        _join(caller)
    finally:
        release.set()
        _join(holder)
    assert isinstance(outcome[0], LockWaitCanceled)
    assert _probe(run_dir) == "free"
    assert not held_by_this_thread()


def test_the_first_lock_observes_cancellation_too(tmp_path, monkeypatch):
    root, run_dir, _ = _tree(tmp_path)
    holding = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    stop = threading.Event()

    def hold() -> None:
        with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
            holding.set()
            assert release.wait(HANG_GUARD)

    holder, _ = _in_thread(hold)
    assert holding.wait(HANG_GUARD)
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    caller, outcome = _in_thread(
        lambda: acquire_all(
            [LockRequest("run_dir", run_dir, False)], run_root=root, canceled=stop.is_set
        ).__enter__()
    )
    try:
        assert waiting.wait(HANG_GUARD)
        stop.set()
        _join(caller)
    finally:
        release.set()
        _join(holder)
    assert isinstance(outcome[0], LockWaitCanceled)


def test_a_wait_for_a_lock_never_blocks_in_flock(tmp_path, monkeypatch):
    """Every attempt is nonblocking, so a wait can be canceled and a signal ends it."""
    root, run_dir, storage = _tree(tmp_path)
    modes: list[int] = []
    real = run_lock.fcntl.flock

    def spy(descriptor, mode):
        modes.append(mode)
        return real(descriptor, mode)

    monkeypatch.setattr(run_lock.fcntl, "flock", spy)
    with acquire_all(
        [LockRequest("run_dir", run_dir, True), LockRequest("storage", storage, False)],
        run_root=root,
    ):
        pass
    assert modes
    for mode in modes:
        if mode & (run_lock.fcntl.LOCK_EX | run_lock.fcntl.LOCK_SH):
            assert mode & run_lock.fcntl.LOCK_NB


# ------------------------------------------------------------------------------------ leases


def test_a_thread_that_holds_a_lock_is_refused_a_launch(tmp_path):
    root, run_dir, _ = _tree(tmp_path)
    refuse_if_held()  # nothing is held
    with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root):
        assert held_by_this_thread()
        with pytest.raises(LeaseHeldError) as caught:
            refuse_if_held()
    assert caught.value.code == "lease_held"
    assert caught.value.path == run_dir
    assert not held_by_this_thread()


def test_a_callback_may_use_a_lock_that_is_held_and_no_other(tmp_path):
    root, run_dir, storage = _tree(tmp_path)
    with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
        with callback_guard():
            with run_dir_read_lock(run_dir, root):  # held already: at once
                pass
            with pytest.raises(LeaseHeldError) as caught:
                with run_dir_read_lock(run_dir.parent / "other", root):
                    pytest.fail("a callback takes no new lock")
            assert caught.value.code == "lease_held"
            with pytest.raises(LeaseHeldError):
                with storage_lock(storage, exclusive=False, run_root=root):
                    pytest.fail("a callback takes no new lock")
        with run_dir_read_lock(run_dir.parent / "other", root):  # outside the callback
            pass


# ------------------------------------------------------------------------------ destinations


def test_the_lock_of_a_destination_lies_under_the_run_root(tmp_path):
    root, _, _ = _tree(tmp_path)
    destination = tmp_path / "user" / "new" / "result.txt"
    with destination_lock(destination, root):
        found = list((root / ".locks" / "deliveries").glob("*.lock"))
        assert len(found) == 1
    assert not destination.parent.exists(), "the lock makes nothing at the destination"


def test_two_spellings_of_one_destination_are_one_lock(tmp_path, monkeypatch):
    root, _, _ = _tree(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    (user / "result.txt").write_text("x")
    through = tmp_path / "alias"
    through.symlink_to(user, target_is_directory=True)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with destination_lock(user / "result.txt", root):
            held.set()
            assert release.wait(HANG_GUARD)

    holder, _ = _in_thread(hold)
    assert held.wait(HANG_GUARD)
    waiting = threading.Event()
    seen = []

    def waits(path: Path) -> None:
        seen.append(path)
        waiting.set()

    monkeypatch.setattr(run_lock, "wait_hook", waits)
    caller, outcome = _in_thread(lambda: destination_lock(through / "result.txt", root).__enter__())
    try:
        assert waiting.wait(HANG_GUARD), "a spelling of the destination was not locked"
    finally:
        release.set()
    _join(holder)
    _join(caller)
    outcome[0].release()


def test_a_destination_in_a_directory_that_does_not_exist_yet_is_one_lock(tmp_path):
    root, _, _ = _tree(tmp_path)
    destination = tmp_path / "later" / "deep" / "result.txt"
    first = run_lock._destination_lock_file(destination, root)
    destination.parent.mkdir(parents=True)
    second = run_lock._destination_lock_file(destination, root)
    # Once the directory exists the key names it, so the lock file differs. A call that
    # locked the old name finds, after it holds the lock, that the destination resolves to the
    # other lock, and it starts again with the new one.
    assert first != second


def test_destinations_are_awaited_in_order_by_a_thread_that_keeps_its_locks(tmp_path, monkeypatch):
    """The delivery of a launch holds its node locks and waits for destinations in order."""
    root, run_dir, _ = _tree(tmp_path)
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with destination_lock(two, root):
            holding.set()
            assert release.wait(HANG_GUARD)

    holder, _ = _in_thread(hold)
    assert holding.wait(HANG_GUARD)
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())

    def deliver():
        with acquire_all([LockRequest("run_dir", run_dir, False)], run_root=root):
            with acquire_all(
                [LockRequest("destination", p, True) for p in (one, two)],
                run_root=root,
                wait_in_order=True,
            ) as locks:
                assert _probe(run_dir, "exclusive") == "blocked"  # the node lock stayed
                return len(locks.requests)

    caller, outcome = _in_thread(deliver)
    try:
        assert waiting.wait(HANG_GUARD)
    finally:
        release.set()
    _join(holder)
    _join(caller)
    assert outcome == [2]
