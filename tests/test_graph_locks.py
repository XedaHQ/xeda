"""The locks of a launch: all of them at once, in one order, and none held while it waits.

A launch locks run directories, then storages, then delivery destinations. It takes every lock or
none, and it waits only while it holds no lock. Delivery is the one phase that holds locks and
waits.
"""

import json
import os
import shutil
import sys
import threading
import unicodedata
from contextlib import ExitStack
from pathlib import Path

import pytest

from xeda import Design
from xeda.deliver import Deliveries, DeliveryError
from xeda.errors import LeaseHeldError
from xeda.flow_runner import DefaultRunner, launch_generation, launch_locks, run_lock
from xeda.flow_runner.dse.dse_runner import Dse
from xeda.flow_runner.dse.fmax import FmaxOptimizer
from xeda.flow_runner.scrub import scrub_design, scrub_generations, scrub_runs
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
from xeda.outcomes import Outcome
from xeda.run_dir import RunDirectoryError
from xeda.run_root import ensure_run_root

from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow
from .io_flows import _Maker, _Taker
from .test_storage_locks import PROBE, _probe
from .test_target_scrub import FLOW as SCRUB_FLOW
from .test_target_scrub import Tree
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no run-directory locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


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


# ------------------------------------------------------------------------------------ launches


def _design(tmp_path: Path) -> Design:
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    return Design(name="d", root=root, rtl={"sources": [], "top": "t"})


def _probe_file(lock: Path, mode: str = "exclusive") -> str:
    """Whether another process could take the lock file `lock` in `mode` now."""
    import subprocess

    found = subprocess.run(
        [sys.executable, "-c", PROBE, str(lock), mode], capture_output=True, text=True, check=True
    )
    return found.stdout.strip()


@pytest.fixture
def acquisitions(monkeypatch):
    """The requests of each acquisition that a launch makes for its graph, as
    `(kind, name, exclusive)`."""
    calls: list[list[tuple[str, str, bool]]] = []
    original = launch_generation.acquire_all

    def record(requests, **kwargs):
        calls.append([(r.kind, r.path.name, r.exclusive) for r in requests])
        return original(requests, **kwargs)

    monkeypatch.setattr(launch_generation, "acquire_all", record)
    return calls


def test_a_launch_takes_every_lock_of_its_graph_once_before_any_flow_runs(
    tmp_path, monkeypatch, acquisitions
):
    root = tmp_path / "xeda_run"
    events: list[str] = []
    monkeypatch.setattr(run_lock, "acquired_hook", lambda path: events.append(path.name))
    for flow in (_Maker, _Taker):
        run = flow.run

        def noted(self, run=run):
            events.append("run " + self.name)
            run(self)

        monkeypatch.setattr(flow, "run", noted)
    runner = DefaultRunner(root, display_results=False)
    assert runner.launch_flow(_Taker, _design(tmp_path), {}).succeeded
    assert events == [
        "__maker.lock",
        "__taker.lock",
        "run __maker",
        "run __taker",
    ], "the producer takes no lock inside the lock of its consumer"
    assert acquisitions == [[("run_dir", "__maker", True), ("run_dir", "__taker", True)]]


def test_a_node_that_is_stale_under_its_shared_lock_makes_the_launch_start_again(
    tmp_path, monkeypatch, acquisitions
):
    root = tmp_path / "xeda_run"
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Taker, design, {}).succeeded
    acquisitions.clear()
    ran: list[str] = []
    run = _Maker.run

    def noted(self):
        ran.append(self.name)
        run(self)

    monkeypatch.setattr(_Maker, "run", noted)
    hint = launch_locks.NodeLocks.hint

    def hint_then_spoil(self):
        hint(self)  # both nodes are up to date: shared
        (root / "d" / "__maker" / "results.json").unlink()  # not any more, when the locks are held

    monkeypatch.setattr(launch_locks.NodeLocks, "hint", hint_then_spoil)
    runner = DefaultRunner(root, display_results=False)
    assert runner.launch_flow(_Taker, design, {}).succeeded
    assert acquisitions == [
        [("run_dir", "__maker", False), ("run_dir", "__taker", False)],
        [("run_dir", "__maker", True), ("run_dir", "__taker", True)],
    ]
    assert ran == ["__maker"]


def test_the_launch_holds_the_storage_exclusively_to_generate_and_shared_while_flows_run(
    tmp_path, monkeypatch, acquisitions
):
    flow = reader_flow()
    world = World(tmp_path)
    events: list[tuple[str, int]] = []
    monkeypatch.setattr(run_lock, "acquired_hook", lambda p: events.append((p.name, world.runs)))
    original = flow.run
    modes: list[tuple[str, str]] = []

    def run(self):
        (storage,) = world.storages()
        modes.append((_probe(storage, "exclusive"), _probe(storage, "shared")))
        original(self)

    monkeypatch.setattr(flow, "run", run)
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    (storage,) = world.storages()
    lock = storage.name + ".lock"
    assert events == [
        (f"{flow.name}.lock", 0),
        (lock, 0),
        (f"{flow.name}.lock", 1),
        (lock, 1),
    ], "the nodes are locked before the generator runs"
    assert acquisitions == [
        [("run_dir", flow.name, True), ("storage", storage.name, True)],
        [("run_dir", flow.name, True), ("storage", storage.name, False)],
    ]
    assert modes == [("blocked", "free")], "a shared lease while the flow runs"
    assert world.runs == 1


def _busy_storage_lock(monkeypatch, world: World, busy_attempts: set[int]) -> list[int]:
    """Make the storage lock look held by another process at the attempts numbered in
    `busy_attempts` (from 1) to take it without waiting. Return the list of restarts."""
    restarts: list[int] = []
    attempts = [0]
    original = run_lock._try_lock

    def try_lock(file, exclusive, path):
        if path.parent.parent.name == ".generated":
            attempts[0] += 1
            if attempts[0] in busy_attempts:
                return False
        return original(file, exclusive, path)

    monkeypatch.setattr(run_lock, "_try_lock", try_lock)
    monkeypatch.setattr(run_lock, "_wait_free", lambda *_args: restarts.append(attempts[0]))
    return restarts


def test_a_launch_that_contention_restarts_many_times_still_completes(tmp_path, monkeypatch):
    """A restart because another process holds a lock is no failed try of the generator."""
    flow = reader_flow()
    world = World(tmp_path)
    restarts = _busy_storage_lock(monkeypatch, world, {1, 2, 3, 4, 5, 6})
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    assert len(restarts) == 6
    assert world.runs == 1


def test_a_rebuild_all_launch_generates_once_although_the_acquisition_restarts(
    tmp_path, monkeypatch
):
    flow = reader_flow()
    world = World(tmp_path)
    # The first attempt generates. The shared retake of the storage finds it held: a restart.
    restarts = _busy_storage_lock(monkeypatch, world, {2, 3})
    result = world.launch(flow, world.runner(rebuild_all=True))
    assert result.outcome is Outcome.PASSED, result.error
    assert len(restarts) == 2
    assert world.runs == 1


def test_the_rebuild_of_a_storage_is_remembered_by_its_identity_and_never_hides_an_invalid_one(
    tmp_path,
):
    world = World(tmp_path)
    design = world.load()
    run_root = ensure_run_root(world.run_root)
    state = launch_generation.GenerationState()

    def settle() -> None:
        with ExitStack() as stack:
            launch_generation.settle(
                design,
                run_root=run_root,
                environ=dict(os.environ),
                rebuild_all=True,
                state=state,
                stack=stack,
            )

    settle()
    assert world.runs == 1 and len(state.generated) == 1
    (identity,) = state.generated
    assert identity.startswith(f"{design.name}/")
    settle()
    assert world.runs == 1, "the forced rebuild happens once in a launch"
    (storage,) = world.storages()
    shutil.rmtree(storage)
    settle()
    assert world.runs == 2, "a storage that is not valid is generated again"


def test_a_storage_that_another_launch_replaced_in_the_gap_is_accepted_when_it_is_valid(
    tmp_path, monkeypatch
):
    flow = reader_flow()
    world = World(tmp_path)
    (world.root / "gen.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "with open(sys.argv[1], 'a') as counter:\n"
        "    counter.write('ran\\n')\n"
        "count = len(Path(sys.argv[1]).read_text().splitlines())\n"
        "out = Path(os.environ['XEDA_GENERATED'])\n"
        "(out / 'gen').mkdir(exist_ok=True)\n"
        "(out / 'gen' / 'top.v').write_text(f'// run {count}\\n')\n"
    )
    original = launch_generation.acquire_all
    calls = []

    def retake(requests, **kwargs):
        calls.append(requests)
        if len(calls) == 2:
            # The launch released its locks after it generated. Another launch forces a rebuild.
            assert world.launch(flow, world.runner(rebuild_all=True)).outcome is Outcome.PASSED
        return original(requests, **kwargs)

    monkeypatch.setattr(launch_generation, "acquire_all", retake)
    result = world.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    assert world.runs == 2
    assert result.nodes[flow.name].record.metrics["seen"] == "// run 2\n"


def test_a_node_reused_under_a_shared_lock_is_neither_prepared_nor_refreshed(tmp_path, monkeypatch):
    root = tmp_path / "xeda_run"
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Taker, design, {}).succeeded
    before = {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino)
        for p in sorted((root / "d").rglob("*"))
        if p.is_file() and p.suffix != ".lock"
    }
    prepared: list[str] = []
    for flow in (_Maker, _Taker):
        monkeypatch.setattr(flow, "prepare_inputs", lambda self: prepared.append(self.name))
    held: list[tuple[str, str]] = []
    place = DefaultRunner._place_generated_deliveries

    def seen_held(self, *args):
        # Called when the launch has taken its locks, before it runs anything.
        for name in ("__maker", "__taker"):
            held.append((_probe(root / "d" / name, "shared"), _probe(root / "d" / name)))
        return place(self, *args)

    monkeypatch.setattr(DefaultRunner, "_place_generated_deliveries", seen_held)
    outcome = DefaultRunner(root, display_results=False).launch_flow(_Taker, design, {})
    assert outcome.reused and prepared == []
    after = {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino)
        for p in sorted((root / "d").rglob("*"))
        if p.is_file() and p.suffix != ".lock"
    }
    assert after == before, "a shared node is read and never written"
    assert held == [("free", "blocked")] * 2, "shared: readers may join, a writer may not"


# ------------------------------------------------------------------------------- cancellation


def test_a_launch_that_waits_for_its_first_lock_observes_cancellation(tmp_path, monkeypatch):
    root = ensure_run_root(tmp_path / "xeda_run")
    design = _design(tmp_path)
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    runner = DefaultRunner(root, display_results=False)
    runner._canceled = waiting.is_set
    monkeypatch.setattr(_Maker, "run", lambda self: pytest.fail("it ran"))
    with run_lock.run_dir_lock(root / "d" / "__maker", root):
        launch, outcome = _in_thread(lambda: runner.launch_flow(_Maker, design, {}))
        _join(launch)
    assert isinstance(outcome[0], LockWaitCanceled)
    assert _probe(root / "d" / "__maker") == "free", "the launch holds nothing"


def test_a_delivery_that_waits_for_a_destination_observes_cancellation(tmp_path, monkeypatch):
    root = ensure_run_root(tmp_path / "xeda_run")
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Maker, design, {}).succeeded
    destination = tmp_path / "user"
    destination.mkdir()
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    runner = DefaultRunner(root, display_results=False, outputs_to=destination)
    runner._canceled = waiting.is_set
    with destination_lock(destination / "made.txt", root):
        launch, outcome = _in_thread(lambda: runner.launch_flow(_Maker, design, {}))
        _join(launch)
    assert isinstance(outcome[0], LockWaitCanceled)
    assert not (destination / "made.txt").exists()
    assert _probe(root / "d" / "__maker") == "free", "the launch released its node locks"


# --------------------------------------------------------------------------------- delivery


def test_a_delivery_keeps_the_node_locks_and_holds_each_destination_in_one_order(
    tmp_path, monkeypatch
):
    root = ensure_run_root(tmp_path / "xeda_run")
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Maker, design, {}).succeeded
    user = tmp_path / "user"
    user.mkdir()
    taken: list[Path] = []
    monkeypatch.setattr(run_lock, "acquired_hook", taken.append)
    held: list[tuple[str, ...]] = []
    remember = Deliveries._remember

    def checked(self, *args, **kwargs):
        locks = sorted((root / ".locks" / "deliveries").glob("*.lock"))
        held.append(
            (
                _probe(root / "d" / "__maker", "exclusive"),
                *(_probe_file(lock) for lock in locks),
            )
        )
        return remember(self, *args, **kwargs)

    monkeypatch.setattr(Deliveries, "_remember", checked)
    runner = DefaultRunner(root, display_results=False, outputs_to=user)
    assert runner.launch_flow(_Maker, design, {}).succeeded
    assert (user / "made.txt").read_text() == "made\n"
    # The node is reused: shared. Its destination and its record are exclusive.
    assert held == [("blocked", "blocked", "blocked")]
    taken = list(dict.fromkeys(taken))  # a lock held again is told again
    deliveries = [p for p in taken if p.parent.name == "deliveries"]
    assert len(deliveries) == 2
    assert [run_lock._resource_key(p) for p in deliveries] == sorted(
        run_lock._resource_key(p) for p in deliveries
    )
    assert all(p.parent.name != "deliveries" for p in taken[:-2]), "the destinations come last"


def _delivering_after_both_read_the_record(monkeypatch) -> None:
    """Make two launches read the delivery record before either one writes it."""
    barrier = threading.Barrier(2, timeout=HANG_GUARD)
    original = Deliveries.lock_requests
    seen = set()

    def requests(self):
        if threading.get_ident() not in seen:
            seen.add(threading.get_ident())
            barrier.wait()
        return original(self)

    monkeypatch.setattr(Deliveries, "lock_requests", requests)


def test_two_launches_that_reuse_one_node_write_both_deliveries_in_its_record(
    tmp_path, monkeypatch
):
    root = ensure_run_root(tmp_path / "xeda_run")
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Maker, design, {}).succeeded
    users = [tmp_path / "one", tmp_path / "two"]
    for user in users:
        user.mkdir()
    _delivering_after_both_read_the_record(monkeypatch)

    def deliver(user: Path):
        return DefaultRunner(root, display_results=False, outputs_to=user).launch_flow(
            _Maker, design, {}
        )

    threads = [_in_thread(lambda user=user: deliver(user)) for user in users]
    for thread, _outcome in threads:
        _join(thread)
    assert [out[0].succeeded for _thread, out in threads] == [True, True]
    record = json.loads((root / "d" / "__maker.delivered.json").read_text())
    assert sorted(Path(name).parent.name for name in record["files"]) == ["one", "two"]


def test_two_launches_that_deliver_one_output_to_one_destination_ask_the_second(
    tmp_path, monkeypatch
):
    """Both launches found nothing at the destination. The first copies. The second finds its
    check out of date under the lock of the destination, and replaces nothing."""
    root = ensure_run_root(tmp_path / "xeda_run")
    design = _design(tmp_path)
    assert DefaultRunner(root, display_results=False).launch_flow(_Maker, design, {}).succeeded
    user = tmp_path / "user"
    user.mkdir()
    _delivering_after_both_read_the_record(monkeypatch)
    threads = [
        _in_thread(
            lambda: DefaultRunner(root, display_results=False, outputs_to=user).launch_flow(
                _Maker, design, {}
            )
        )
        for _ in range(2)
    ]
    for thread, _outcome in threads:
        _join(thread)
    results = [out[0] for _thread, out in threads]
    refused = [r for r in results if isinstance(r, DeliveryError)]
    assert len(refused) == 1 and "changed while the run went on" in str(refused[0])
    assert [r.succeeded for r in results if not isinstance(r, BaseException)] == [True]
    assert (user / "made.txt").read_text() == "made\n"


# ------------------------------------------------------------------------------------- guards


def test_a_sweep_is_refused_by_a_thread_that_holds_a_lock(tmp_path):
    root = ensure_run_root(tmp_path / "xeda_run")
    sweep = Dse(
        FmaxOptimizer,
        {"init_freq_low": 100, "init_freq_high": 200},
        run_root=root,
        max_workers=1,
    )
    with run_lock.run_dir_read_lock(root / "d" / "flow", root):
        with pytest.raises(LeaseHeldError) as caught:
            sweep.run_flow(_Maker, _design(tmp_path), {})
    assert caught.value.code == "lease_held"


def test_a_scrub_is_refused_by_a_thread_that_holds_a_lock(tmp_path):
    tree = Tree(tmp_path)
    with run_lock.run_dir_read_lock(tree.root / "other" / "flow", tree.root):
        with pytest.raises(LeaseHeldError) as caught:
            scrub_design(SCRUB_FLOW, tree.design, run_root=tree.root, confirm=lambda _: True)
        assert caught.value.code == "lease_held"
        with pytest.raises(LeaseHeldError):
            scrub_runs(SCRUB_FLOW, tree.design, run_root=tree.root, confirm=lambda _: True)
        with pytest.raises(LeaseHeldError):
            scrub_generations("d", run_root=tree.root, confirm=lambda _: True)
    assert all(path.exists() for path in tree.everything), "a refused scrub removes nothing"


def test_a_scrub_holds_one_lock_at_a_time(tmp_path, monkeypatch):
    """Scrub waits for each lock, so it never keeps one while it waits for the next."""
    tree = Tree(tmp_path)
    me = (os.getpid(), threading.get_ident())
    most: list[int] = [0]

    def count(_path: Path) -> None:
        held = sum(1 for key in run_lock._held if key[:2] == me)
        most[0] = max(most[0], held)

    monkeypatch.setattr(run_lock, "acquired_hook", count)
    result = scrub_design(SCRUB_FLOW, tree.design, run_root=tree.root, confirm=lambda _: True)
    assert sorted(result.removed) == sorted(tree.everything)
    assert most[0] == 1
