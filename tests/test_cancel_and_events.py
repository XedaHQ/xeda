"""A launch stops on a `CancelToken`, and tells `on_event` how it goes on.

Each test that stops something waits for the thing to run (a file that the program writes, a hook
of the lock), never for a time to pass.
"""

import ast
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import xeda.cancel
import xeda.flow_runner.dependencies
from xeda import Design
from xeda.cancel import CancelToken, never_canceled, observing
from xeda.errors import LeaseHeldError
from xeda.events import Event
from xeda.flow_runner import run_lock
from xeda.flow_runner.default_runner import FlowLauncher
from xeda.flow_runner.run_lock import LockRequest, LockWaitCanceled, acquire_all
from xeda.outcomes import Disposition, Outcome
from xeda.proc_utils import ProcessCanceled, run_child, run_process
from xeda.records import read_record
from xeda.run_root import ensure_run_root
from xeda.spec import Spec
from xeda.utils import Canceled
from xeda.workspace import Workspace

from .cancel_flows import cut_design, cut_flows
from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow
from .test_dry_run_freshness import ToyGraph, toys  # noqa: F401  (the toy flows)
from .test_git_dependencies import REAL_GIT, git_log, remote  # noqa: F401  (a Git remote)
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


def test_run_child_stops_its_descendants_when_canceled_says_yes(tmp_path):
    started = tmp_path / "started"
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: (_wait_until(lambda: _read_pid(started), "the child"), stop.set()),
        daemon=True,
    )
    thread.start()
    begin = time.monotonic()
    with pytest.raises(ProcessCanceled):
        run_child([sys.executable, "-c", _tree(started)], cwd=tmp_path, canceled=stop.is_set)
    thread.join()
    assert time.monotonic() - begin < PROMPT, "the call waited for the child to end on its own"
    assert not _is_running(_read_pid(started))


def test_a_canceled_predicate_that_stays_false_changes_nothing(tmp_path):
    out = run_process(sys.executable, ["-c", "print('hi')"], stdout=True, canceled=lambda: False)
    assert out == "hi"


def _leaves_a_child(started: Path, wrapper: Path) -> str:
    """A tool wrapper that starts a child, which keeps the output open, and exits at once."""
    child = (
        f"{DEFAULT_SIGINT}import os, pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(20)\n"
    )
    return (
        "import os, pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"while not pathlib.Path({str(started)!r}).exists():\n    time.sleep(0.01)\n"
        f"pathlib.Path({str(wrapper)!r}).write_text(str(os.getpid()))\n"
    )


@pytest.mark.parametrize("capture", [True, False], ids=["captured", "copied"])
def test_cancel_stops_what_an_exited_tool_left_holding_the_output(tmp_path, capture):
    started, wrapper = tmp_path / "started", tmp_path / "wrapper"
    stop = threading.Event()

    def work() -> None:
        _wait_until(lambda: _read_pid(started), "the child")
        pid = _wait_until(lambda: _read_pid(wrapper), "the wrapper")
        _wait_until(lambda: not _is_running(pid), "the wrapper to exit")
        stop.set()  # the wrapper is gone, and the child holds the pipe that Xeda reads

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProcessCanceled):
            run_process(
                sys.executable,
                ["-c", _leaves_a_child(started, wrapper)],
                stdout=True if capture else None,
                canceled=stop.is_set,
            )
        thread.join()
        assert not _is_running(_read_pid(started)), "the child is gone"
    finally:
        if (pid := _read_pid(started)) is not None and _is_running(pid):
            os.kill(pid, signal.SIGKILL)


def test_run_child_keeps_the_session_and_the_group_of_the_caller(tmp_path):
    where = tmp_path / "where"
    program = (
        f"import os, pathlib; pathlib.Path({str(where)!r}).write_text(f'{{os.getsid(0)}} "
        "{os.getpgid(0)}')"
    )
    run_child([sys.executable, "-c", program], cwd=tmp_path, check=True)
    assert where.read_text() == f"{os.getsid(0)} {os.getpgid(0)}"


def test_a_call_in_a_launch_observes_its_request_without_being_given_it(tmp_path):
    stop = threading.Event()
    for call in (run_process, run_child):
        started = tmp_path / f"started-{call.__name__}"
        stop.clear()
        thread = threading.Thread(
            target=lambda: (_wait_until(lambda: _read_pid(started), "the child"), stop.set()),
            daemon=True,
        )
        thread.start()
        begin = time.monotonic()
        with observing(stop.is_set), pytest.raises(ProcessCanceled):
            if call is run_process:
                run_process(sys.executable, ["-c", _tree(started)])
            else:
                run_child([sys.executable, "-c", _tree(started)], cwd=tmp_path)
        thread.join()
        assert time.monotonic() - begin < PROMPT, call.__name__
        assert not _is_running(_read_pid(started)), call.__name__


def test_a_lock_wait_in_a_launch_observes_its_request_without_being_given_it(tmp_path, monkeypatch):
    root = ensure_run_root(tmp_path / "xeda_run")
    run_dir = root / "d" / "node"
    stop, waiting = threading.Event(), threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    outcome: list[BaseException] = []

    def wait_for_it() -> None:
        with observing(stop.is_set):
            try:
                with acquire_all([LockRequest("run_dir", run_dir, True)], run_root=root):
                    pass
            except BaseException as error:  # noqa: BLE001 - reported by the test thread
                outcome.append(error)

    with run_lock.run_dir_lock(run_dir, root):
        thread = threading.Thread(target=wait_for_it, daemon=True)
        thread.start()
        assert waiting.wait(PROMPT)
        stop.set()
        thread.join(PROMPT)
    assert len(outcome) == 1 and isinstance(outcome[0], LockWaitCanceled)


def test_a_call_can_opt_out_of_the_request_of_a_launch(tmp_path):
    root = ensure_run_root(tmp_path / "xeda_run")
    with observing(lambda: True):
        out = run_process(
            sys.executable, ["-c", "print('done')"], stdout=True, canceled=never_canceled
        )
        with acquire_all(
            [LockRequest("run_dir", root / "d" / "node", True)],
            run_root=root,
            canceled=never_canceled,
        ):
            pass
    assert out == "done"


def _names_used(directory: Path, wanted: set[str]) -> dict[str, set[str]]:
    """Return, for each file below `directory`, which of the names `wanted` it uses."""
    found: dict[str, set[str]] = {}
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text())
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        } & wanted
        if names:
            found[str(path.relative_to(directory))] = names
    return found


def test_only_reviewed_code_opts_out_of_the_request_of_a_launch():
    """A call that opts out is a call that a canceled launch does not stop. The two reviewed ones
    run because the launch was canceled: the stop of a container, and the clean-up of a mirror."""
    root = Path(xeda.__file__).parent
    found = _names_used(root, {"never_canceled"})
    found.pop("cancel.py")
    assert found == {"remote/client.py": {"never_canceled"}, "tool.py": {"never_canceled"}}


def test_only_reviewed_code_starts_a_process_other_than_through_run_process():
    """A process that bypasses `run_process` and `run_child` does not observe the request."""
    root = Path(xeda.__file__).parent
    starters = {("subprocess", name) for name in ("run", "Popen", "call", "check_call")} | {
        ("subprocess", "check_output"),
        ("os", "system"),
        ("os", "popen"),
    }
    found = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and (node.value.id, node.attr) in starters
            ):
                found.add(str(path.relative_to(root)))
    assert sorted(found) == [
        "flow_runner/trace.py",  # `docker image inspect`, a quick query with no log
        "proc_utils.py",
        "remote/agent.py",  # runs on the host
        "remote/client.py",
        "remote/ssh.py",
    ]


# ------------------------------------------------------------------ a cancel in a callback


def _cancel_at(token: CancelToken, kind: str, node: str, seen: list[Event]):
    def on_event(event: Event) -> None:
        seen.append(event)
        if (event.kind, event.node) == (kind, node):
            token.cancel()

    return on_event


def test_a_cancel_in_the_callback_of_a_producer_stops_its_consumer(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    token, seen = CancelToken(), []
    result = workspace.run(
        Spec("cut_consumer", cut_design(tmp_path / "design")),
        cancel=token,
        on_event=_cancel_at(token, "node_finished", "cut_producer", seen),
    )
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    producer, consumer = result.nodes["cut_producer"], result.nodes["cut_consumer"]
    assert producer.outcome is Outcome.PASSED
    assert consumer.outcome is Outcome.SKIPPED and consumer.disposition is Disposition.NOT_RUN
    assert not (consumer.run_dir / "results.json").exists(), "the consumer did not run"
    assert [(e.kind, e.node) for e in seen] == [
        ("launch_started", None),
        ("node_started", "cut_producer"),
        ("node_finished", "cut_producer"),
        ("launch_finished", None),
    ]
    assert seen[-1].outcome is Outcome.CANCELED


def test_a_cancel_in_the_callback_of_the_start_of_a_node_cancels_that_node(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    token, seen = CancelToken(), []
    result = workspace.run(
        Spec("cut_consumer", cut_design(tmp_path / "design")),
        cancel=token,
        on_event=_cancel_at(token, "node_started", "cut_producer", seen),
    )
    assert result.outcome is Outcome.CANCELED
    producer = result.nodes["cut_producer"]
    assert producer.outcome is Outcome.CANCELED and producer.disposition is Disposition.NOT_RUN
    assert producer.error is not None and producer.error.code == "canceled"
    assert result.nodes["cut_consumer"].outcome is Outcome.SKIPPED
    for name in ("results.json", "trace.json"):
        assert not (producer.run_dir / name).exists(), "the flow did not run"
    assert [(e.kind, e.node) for e in seen] == [
        ("launch_started", None),
        ("node_started", "cut_producer"),
        ("node_finished", "cut_producer"),
        ("launch_finished", None),
    ]
    assert seen[2].outcome is Outcome.CANCELED


def test_a_callback_that_raises_at_the_start_of_a_node_keeps_the_flow_from_running(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    boom = ValueError("no")

    def on_event(event: Event) -> None:
        if event.kind == "node_started":
            raise boom

    with pytest.raises(ValueError) as raised:
        workspace.run(Spec("cut_producer", cut_design(tmp_path / "design")), on_event=on_event)
    assert raised.value is boom
    run_dir = workspace.run_root / "d" / "cut_producer"
    for name in ("results.json", "trace.json"):
        assert not (run_dir / name).exists(), "the flow did not run, and no trace says it passed"


def test_a_cancel_after_the_last_node_delivers_nothing(tmp_path):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    token, seen = CancelToken(), []
    out = tmp_path / "out"
    result = workspace.run(
        Spec("cut_consumer", cut_design(tmp_path / "design")),
        outputs_to=out,
        cancel=token,
        on_event=_cancel_at(token, "node_finished", "cut_consumer", seen),
    )
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    assert {n.outcome for n in result.nodes.values()} == {Outcome.PASSED}
    assert not out.exists() or list(out.rglob("*")) == [], "nothing was delivered"
    assert seen[-1].kind == "launch_finished" and seen[-1].outcome is Outcome.CANCELED


# ------------------------------------------------------------------ planning and the prune


def _stalling_git(tmp_path: Path, monkeypatch, started: Path) -> None:
    """Put a `git` on `PATH` that stops in `fetch`, with its process id in `started`."""
    shim = tmp_path / "stall"
    shim.mkdir()
    program = shim / "git"
    program.write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" fetch "*) printf %s $$ > "{started}"; exec sleep 60;; esac\n'
        f'exec "{REAL_GIT}" "$@"\n'
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")


def _dependent(tmp_path: Path, remote) -> Spec:  # noqa: F811
    from .test_git_dependencies import _design_file, _pinned

    return Spec("cut_producer", Design.load(_design_file(tmp_path, _pinned(remote))))


def _assert_nothing_planned(result, events: list[Event]) -> None:
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    assert dict(result.nodes) == {} and result.roots == ()
    assert [e.kind for e in events] == ["launch_started", "launch_finished"]
    assert events[-1].outcome is Outcome.CANCELED


def test_cancel_while_planning_fetches_a_git_dependency(
    tmp_path, remote, monkeypatch  # noqa: F811
):
    cut_flows()
    started = tmp_path / "fetching"
    _stalling_git(tmp_path, monkeypatch, started)
    workspace = Workspace(tmp_path / "xeda_run")
    token, seen = CancelToken(), []
    thread = _cancel_when_running(started, token)
    result = workspace.run(_dependent(tmp_path, remote), cancel=token, on_event=seen.append)
    thread.join()
    _assert_nothing_planned(result, seen)
    assert not _is_running(_read_pid(started)), "git was stopped"
    storage = xeda.flow_runner.dependencies.DEPENDENCIES_DIRECTORY
    assert [p.name for p in (workspace.run_root / storage).iterdir() if p.suffix != ".lock"] == []


def test_cancel_while_planning_waits_for_the_lock_of_a_dependency(
    tmp_path, remote, monkeypatch  # noqa: F811
):
    cut_flows()
    workspace = Workspace(ensure_run_root(tmp_path / "xeda_run"))
    storage = xeda.flow_runner.dependencies.dependency_storage(
        workspace.run_root, xeda.flow_runner.dependencies.dependency_key(remote.url, remote.first)
    )
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    token, seen, outcome = CancelToken(), [], []
    spec = _dependent(tmp_path, remote)
    with run_lock.storage_lock(storage, exclusive=True, run_root=workspace.run_root):
        thread = threading.Thread(
            target=lambda: outcome.append(workspace.run(spec, cancel=token, on_event=seen.append)),
            daemon=True,
        )
        thread.start()
        assert waiting.wait(PROMPT), "planning did not wait for the lock"
        token.cancel()
        thread.join(PROMPT)
    (result,) = outcome
    _assert_nothing_planned(result, seen)
    assert not storage.exists()


def test_a_token_that_is_canceled_before_a_dependency_is_read_fetches_nothing(
    tmp_path, remote, git_log  # noqa: F811
):
    cut_flows()
    token, seen = CancelToken(), []
    token.cancel()
    result = Workspace(tmp_path / "xeda_run").run(
        _dependent(tmp_path, remote), cancel=token, on_event=seen.append
    )
    _assert_nothing_planned(result, seen)
    assert git_log() == [], "no Git command started"


def test_a_cancel_while_a_plan_is_checked_under_the_locks_is_no_stale_plan(tmp_path, monkeypatch):
    cut_flows()
    workspace = Workspace(tmp_path / "xeda_run")
    plan = workspace.plan(Spec("cut_consumer", cut_design(tmp_path / "design")))
    real = FlowLauncher._judged_request
    calls: list[int] = []

    def judged(self, *arguments, **keywords):
        calls.append(1)
        if len(calls) > 1:  # the check of the plan, under the locks of the launch
            raise Canceled("canceled while the plan was planned again")
        return real(self, *arguments, **keywords)

    monkeypatch.setattr(FlowLauncher, "_judged_request", judged)
    result = workspace.run(plan)
    assert len(calls) == 2
    assert result.outcome is Outcome.CANCELED
    assert {n.outcome for n in result.nodes.values()} == {Outcome.SKIPPED}


def _prune_world(graph, tmp_path):
    """A workspace with hashed run directories, the old directory of the consumer, and the spec
    that a pruning launch runs. Return them."""
    workspace = Workspace(graph.run_root, hashed_run_dirs=True, confirm=lambda question: True)
    first = workspace.run(Spec("toy_consumer", graph.design()))
    spec = Spec("toy_consumer", graph.design(), settings={"toy_consumer": {"shout": True}})
    assert workspace.run(spec).passed
    old = first.nodes["toy_consumer"].run_dir
    assert old.is_dir()
    return workspace, spec, old


@pytest.fixture
def graph(tmp_path, monkeypatch, toys):  # noqa: F811
    return ToyGraph(tmp_path, monkeypatch)


def test_a_cancel_ends_the_wait_of_the_prune_and_removes_nothing(graph, tmp_path, monkeypatch):
    workspace, spec, old = _prune_world(graph, tmp_path)
    waiting = threading.Event()
    monkeypatch.setattr(run_lock, "wait_hook", lambda path: waiting.set())
    token, outcome = CancelToken(), []
    with run_lock.run_dir_lock(old, workspace.run_root):
        thread = threading.Thread(
            target=lambda: outcome.append(workspace.run(spec, prune_variants=True, cancel=token)),
            daemon=True,
        )
        thread.start()
        assert waiting.wait(PROMPT), "the prune did not wait for the lock"
        token.cancel()
        thread.join(PROMPT)
    (result,) = outcome
    assert result.outcome is Outcome.CANCELED
    assert old.is_dir(), "nothing was removed"


def test_a_cancel_after_the_prune_took_its_lock_removes_nothing(graph, tmp_path, monkeypatch):
    workspace, spec, old = _prune_world(graph, tmp_path)
    token = CancelToken()
    monkeypatch.setattr(
        run_lock,
        "acquired_hook",
        lambda path: token.cancel() if path == run_lock.lock_file(old) else None,
    )
    result = workspace.run(spec, prune_variants=True, cancel=token)
    assert result.outcome is Outcome.CANCELED
    assert result.error is not None and result.error.code == "canceled"
    assert old.is_dir(), "the request came after the lock, and before the removal"
    assert {n.outcome for n in result.nodes.values()} == {Outcome.SKIPPED}


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
