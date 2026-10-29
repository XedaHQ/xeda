"""A launch stops on a `CancelToken`, and tells `on_event` how it goes on.

Each test that stops something waits for the thing to run (a file that the program writes, a hook
of the lock), never for a time to pass.
"""

import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import xeda.cancel
from xeda.cancel import CancelToken
from xeda.errors import LeaseHeldError
from xeda.events import Event
from xeda.flow_runner import run_lock
from xeda.flow_runner.run_lock import LockRequest, LockWaitCanceled, acquire_all
from xeda.outcomes import Disposition, Outcome
from xeda.proc_utils import ProcessCanceled, run_child, run_process
from xeda.records import read_record
from xeda.run_root import ensure_run_root
from xeda.spec import Spec
from xeda.workspace import Workspace

from .cancel_flows import cut_design, cut_flows
from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow
from .test_proc_utils import DEFAULT_SIGINT, _is_running, _read_pid, _tree, _wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

#: A launch that nothing stops ends within this many seconds, or a test fails.
PROMPT = 30.0


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _free(root: Path, run_dir: Path) -> bool:
    """Whether the lock of `run_dir` is free now."""
    try:
        with acquire_all(
            [LockRequest("run_dir", run_dir, True)], run_root=root, canceled=lambda: True
        ):
            return True
    except LockWaitCanceled:
        return False


def _cancel_when_running(started: Path, token: CancelToken) -> threading.Thread:
    """Start a thread that cancels `token` once the program wrote its process id to `started`."""

    def work() -> None:
        _wait_until(lambda: _read_pid(started), "the program to run")
        token.cancel()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def test_cancel_from_another_thread_stops_a_running_tool(tmp_path):
    started = tmp_path / "started"
    cut_flows(_tree(started))
    workspace = Workspace(tmp_path / "xeda_run")
    spec = Spec("cut_consumer", cut_design(tmp_path / "design"))
    token = CancelToken()
    thread = _cancel_when_running(started, token)
    result = workspace.run(spec, cancel=token)
    thread.join()
    assert result.outcome is Outcome.CANCELED and not result.passed
    assert (
        result.error is not None and result.error.code == "canceled" and result.error.node is None
    )
    producer, consumer = result.nodes["cut_producer"], result.nodes["cut_consumer"]
    assert producer.outcome is Outcome.CANCELED and producer.disposition is Disposition.RAN
    assert producer.error is not None and producer.error.code == "canceled"
    assert consumer.outcome is Outcome.SKIPPED and consumer.disposition is Disposition.NOT_RUN
    record = read_record(producer.run_dir, run_root=workspace.run_root)
    assert record.outcome is Outcome.CANCELED
    assert not (producer.run_dir / "trace.json").exists(), "a canceled run keeps no trace"
    assert not _is_running(_read_pid(started)), "the whole group of the tool is gone"
    assert _free(workspace.run_root, producer.run_dir)
    # The next launch runs the node again.
    cut_flows(None)
    again = workspace.run(spec)
    assert again.passed and again.nodes["cut_producer"].disposition is Disposition.RAN


def test_cancel_while_waiting_for_a_lock(tmp_path, monkeypatch):
    cut_flows()
    workspace = Workspace(ensure_run_root(tmp_path / "xeda_run"))
    spec = Spec("cut_producer", cut_design(tmp_path / "design"))
    (node,) = workspace.plan(spec).nodes.values()
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    token = CancelToken()
    outcome: list = []
    with run_lock.run_dir_lock(node.run_dir, workspace.run_root):
        thread = threading.Thread(
            target=lambda: outcome.append(workspace.run(spec, cancel=token)), daemon=True
        )
        thread.start()
        assert waiting.wait(PROMPT), "the launch did not wait for the lock"
        token.cancel()
        thread.join(PROMPT)
    (result,) = outcome
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    assert {n.outcome for n in result.nodes.values()} == {Outcome.SKIPPED}
    assert not (node.run_dir / "results.json").exists(), "nothing ran"
    assert _free(workspace.run_root, node.run_dir)


def test_cancel_during_generation(tmp_path):
    world = World(tmp_path)
    started = Path(str(world.counter) + ".pid")
    (world.root / "gen.py").write_text(
        "import os, pathlib, sys, time\n"
        f"{DEFAULT_SIGINT}"
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    reader = reader_flow()
    workspace = Workspace(world.run_root)
    token = CancelToken()
    thread = _cancel_when_running(started, token)
    result = workspace.run(Spec(reader.name, world.load()), cancel=token)
    thread.join()
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    assert {n.outcome for n in result.nodes.values()} == {Outcome.SKIPPED}
    assert not _is_running(_read_pid(started))
    assert world.storages() == [], "no half-written output stays"


def test_a_token_that_is_canceled_before_the_launch_starts_nothing(tmp_path):
    cut_flows(None)
    token = CancelToken()
    token.cancel()
    workspace = Workspace(tmp_path / "xeda_run")
    result = workspace.run(Spec("cut_consumer", cut_design(tmp_path / "design")), cancel=token)
    assert result.outcome is Outcome.CANCELED
    assert {n.outcome for n in result.nodes.values()} == {Outcome.SKIPPED}
    assert [n for n in workspace.run_root.rglob("results.json")] == []


def test_the_timeout_counts_from_construction(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(xeda.cancel, "monotonic", lambda: now[0])
    token = CancelToken(timeout=5)
    now[0] = 104.9
    assert not token.canceled
    now[0] = 105.0
    assert token.canceled


def test_cancel_is_idempotent_and_a_timeout_must_be_positive():
    token = CancelToken()
    assert not token.canceled
    token.cancel()
    token.cancel()
    assert token.canceled
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            CancelToken(timeout=bad)


def test_a_timeout_stops_a_running_tool(tmp_path):
    started = tmp_path / "started"
    cut_flows(_tree(started))
    workspace = Workspace(tmp_path / "xeda_run")
    # The timeout passes while the tool runs: the token answers by the clock, with no `cancel()`.
    token = CancelToken(timeout=2)
    result = workspace.run(Spec("cut_producer", cut_design(tmp_path / "design")), cancel=token)
    assert result.outcome is Outcome.CANCELED
    assert result.nodes["cut_producer"].outcome is Outcome.CANCELED
    assert not _is_running(_read_pid(started))


def test_events_come_in_order_in_the_calling_thread(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    spec = Spec("cut_consumer", cut_design(tmp_path / "design"))
    seen: list[tuple[Event, int]] = []
    result = workspace.run(spec, on_event=lambda e: seen.append((e, threading.get_ident())))
    events = [e for e, _ in seen]
    assert [(e.kind, e.node) for e in events] == [
        ("launch_started", None),
        ("node_started", "cut_producer"),
        ("node_finished", "cut_producer"),
        ("node_started", "cut_consumer"),
        ("node_finished", "cut_consumer"),
        ("launch_finished", None),
    ]
    assert {thread for _, thread in seen} == {threading.get_ident()}
    assert {e.launch_id for e in events} == {result.launch_id}
    assert [(e.outcome, e.disposition) for e in events] == [
        (None, None),
        (None, None),
        (Outcome.PASSED, Disposition.RAN),
        (None, None),
        (Outcome.PASSED, Disposition.RAN),
        (Outcome.PASSED, None),
    ]
    assert all(e.time.tzinfo is timezone.utc for e in events)
    assert [e.time for e in events] == sorted(e.time for e in events)
    assert isinstance(events[0].time, datetime)
    # A reused node has both events, with the disposition `reused`.
    again: list[Event] = []
    workspace.run(spec, on_event=again.append)
    assert [e.kind for e in again] == [e.kind for e in events]
    assert [e.disposition for e in again if e.kind == "node_finished"] == [Disposition.REUSED] * 2


def test_a_failed_node_has_its_events_and_the_launch_finishes(tmp_path):
    producer, _ = cut_flows("raise SystemExit(3)")
    workspace = Workspace(tmp_path / "xeda_run")
    seen: list[Event] = []
    result = workspace.run(
        Spec("cut_consumer", cut_design(tmp_path / "design")), on_event=seen.append
    )
    assert result.outcome is Outcome.FAILED
    finished = {e.node: e for e in seen if e.kind == "node_finished"}
    assert finished["cut_producer"].outcome is Outcome.FAILED
    assert seen[-1].kind == "launch_finished" and seen[-1].outcome is Outcome.FAILED


def test_a_raising_callback_cancels_the_launch_and_propagates(tmp_path):
    started = tmp_path / "started"
    cut_flows(_tree(started))
    workspace = Workspace(tmp_path / "xeda_run")
    spec = Spec("cut_consumer", cut_design(tmp_path / "design"))
    boom = ValueError("the callback failed")
    kinds: list[str] = []

    def on_event(event: Event) -> None:
        kinds.append(event.kind)
        if event.kind == "node_started":
            raise boom

    with pytest.raises(ValueError) as raised:
        workspace.run(spec, on_event=on_event)
    assert raised.value is boom
    assert kinds == ["launch_started", "node_started"], "no event after the callback raised"
    run_dir = workspace.run_root / "d" / "cut_producer"
    assert _free(workspace.run_root, run_dir)
    if started.exists():
        assert not _is_running(_read_pid(started)), "the tool was stopped"


def test_a_callback_that_raises_at_the_end_propagates_after_the_result_is_built(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")

    def on_event(event: Event) -> None:
        if event.kind == "launch_finished":
            raise KeyError("at the end")

    with pytest.raises(KeyError):
        workspace.run(Spec("cut_producer", cut_design(tmp_path / "design")), on_event=on_event)


def test_a_callback_cannot_start_a_launch(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    design = cut_design(tmp_path / "design")
    other = Spec("cut_producer", design)

    def on_event(event: Event) -> None:
        if event.kind == "node_finished":
            workspace.run(other)

    with pytest.raises(LeaseHeldError) as raised:
        workspace.run(Spec("cut_consumer", design), on_event=on_event)
    assert raised.value.code == "lease_held"
    assert _free(workspace.run_root, workspace.run_root / "d" / "cut_producer")
    assert _free(workspace.run_root, workspace.run_root / "d" / "cut_consumer")


def test_a_callback_reuses_the_locks_of_the_launch_and_takes_no_other(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    design = cut_design(tmp_path / "design")
    root = workspace.run_root
    held, elsewhere = root / "d" / "cut_producer", root / "d" / "other"
    found: list[str] = []

    def on_event(event: Event) -> None:
        if event.kind != "node_finished":
            return
        with run_lock.run_dir_read_lock(held, root):
            found.append("reused")
        try:
            with run_lock.run_dir_read_lock(elsewhere, root):
                found.append("took another")
        except LeaseHeldError as error:
            found.append(error.code)

    assert workspace.run(Spec("cut_consumer", design), on_event=on_event).passed
    assert found[:2] == ["reused", "lease_held"]
    assert not elsewhere.exists() and not (root / "d" / "other.lock").exists()


def test_the_launch_without_a_callback_keeps_its_behavior(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    assert workspace.run(Spec("cut_consumer", cut_design(tmp_path / "design"))).passed


# ------------------------------------------------------------------ the processes of the library


def test_run_process_stops_a_group_when_canceled_says_yes(tmp_path):
    started = tmp_path / "started"
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: (_wait_until(lambda: _read_pid(started), "the child"), stop.set()),
        daemon=True,
    )
    thread.start()
    with pytest.raises(ProcessCanceled):
        run_process(sys.executable, ["-c", _tree(started)], canceled=stop.is_set)
    thread.join()
    assert not _is_running(_read_pid(started))


def test_run_child_stops_a_group_when_canceled_says_yes(tmp_path):
    started = tmp_path / "started"
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: (_wait_until(lambda: _read_pid(started), "the child"), stop.set()),
        daemon=True,
    )
    thread.start()
    with pytest.raises(ProcessCanceled):
        run_child([sys.executable, "-c", _tree(started)], cwd=tmp_path, canceled=stop.is_set)
    thread.join()
    assert not _is_running(_read_pid(started))


def test_a_canceled_predicate_that_stays_false_changes_nothing(tmp_path):
    out = run_process(sys.executable, ["-c", "print('hi')"], stdout=True, canceled=lambda: False)
    assert out == "hi"


# ------------------------------------------------------------------------------- Ctrl-C


CHILD = """\
import signal, sys
sys.path.insert(0, {root!r})
signal.signal(signal.SIGINT, signal.default_int_handler)
from xeda.spec import Spec
from xeda.workspace import Workspace
from tests.cancel_flows import cut_design, cut_flows

cut_flows({program!r})
workspace = Workspace({run_root!r})
try:
    workspace.run(Spec("cut_consumer", cut_design({design!r})))
except KeyboardInterrupt:
    sys.exit(130)
sys.exit(0)
"""


def test_an_interrupt_stops_the_tool_group_and_releases_the_locks(tmp_path):
    started = tmp_path / "started"
    run_root = tmp_path / "xeda_run"
    script = tmp_path / "child.py"
    script.write_text(
        CHILD.format(
            root=str(Path(__file__).parent.parent),
            program=_tree(started),
            run_root=str(run_root),
            design=str(tmp_path / "design"),
        )
    )
    child = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stderr=subprocess.PIPE,
    )
    try:
        grandchild = _wait_until(lambda: _read_pid(started), "the tool to run")
        child.send_signal(signal.SIGINT)
        _out, error = child.communicate(timeout=PROMPT)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert child.returncode == 130, error.decode()
    assert not _is_running(grandchild)
    root = Path(run_root).resolve()
    assert _free(root, root / "d" / "cut_producer")
    assert _free(root, root / "d" / "cut_consumer")
    # A second launch on the same run root takes the locks at once, and runs.
    cut_flows(None)
    again = Workspace(run_root).run(Spec("cut_consumer", cut_design(tmp_path / "design")))
    assert again.passed
