"""`Workspace.sweep(max_workers=N)` with N above 1 executes the nodes in worker processes.

The workers start with `spawn` on every platform. The tests run the same fake tools as
`tests/test_sweep.py`. A test that stops running tools waits for each tool to write a file
(`XEDA_FAKE_FPGA_READY` names a directory), never for a time to pass.
"""

import copy
import dataclasses
import json
import os
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda._internal.cancel import CancelToken
from xeda._internal.dataclass import Field
from xeda.errors import SpecError
from xeda._internal.events import Event
from xeda._internal.flow import Flow
from xeda._internal.flow_runner.run_lock import LockRequest, LockWaitCanceled, acquire_all
from xeda._internal.outcomes import Disposition, Outcome
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from xeda._internal import sweep
from xeda._internal.spec import Spec
from xeda._internal.sweep import NodeTask, worker_main
from xeda._internal.workspace import Workspace

from . import io_flows  # noqa: F401 - registers the declared test flows
from . import tool_utils
from .test_proc_utils import _is_running, _kill, _wait_until
from .test_sweep import ECP5, MARK, _calls, _dispositions, _seeds, _spec, _task, _workspace
from .source_layout import layout_script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")


@pytest.fixture
def design(tmp_path, monkeypatch) -> Design:
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    return Design(
        name="top", root=root, rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]}
    )


#: A sweep that nothing blocks ends within this many seconds, or a test fails.
PROMPT = 30.0


def _tools_that_block(tmp_path: Path, monkeypatch) -> Path:
    """Make every fake `yosys` write `<pid>.pid` into the returned directory and wait. The file
    holds the process id of the tool and of a child that the tool starts."""
    ready = tmp_path / "ready"
    ready.mkdir()
    monkeypatch.setenv("XEDA_FAKE_FPGA_READY", str(ready))
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "yosys")
    return ready


def _all_pids(ready: Path) -> list[int]:
    """The process ids that the blocked tools wrote, and those of their children."""
    pids: list[int] = []
    for file in sorted(ready.glob("*.pid")):
        pids += [int(pid) for pid in file.read_text().split()]
    return pids


def _blocked_pids(ready: Path) -> list[int] | None:
    """The process ids of two tools and their children, once both tools block; else `None`."""
    return _all_pids(ready) if len(list(ready.glob("*.pid"))) >= 2 else None


def _worker_pids(workspace: Workspace) -> set[int]:
    """The processes that started a tool: the workers."""
    return {
        json.loads(line)["ppid"]
        for file in workspace.run_root.rglob("fake_fpga.calls.jsonl")
        for line in file.read_text().splitlines()
    }


def _two_independent_points(design) -> list:
    return [_spec(design, 1), _spec(design, 1, **{"yosys_fpga.nobram": True})]


def _stopped_and_free(workspace: Workspace, specs: list, pids: list[int]) -> None:
    """Assert that every process in `pids` ended and that a new launch takes every lock."""
    _wait_until(lambda: not any(_is_running(pid) for pid in pids), "the processes to end", PROMPT)
    requests = [
        LockRequest("run_dir", node.run_dir, True)
        for spec in specs
        for node in workspace.plan(spec).nodes.values()
    ]
    try:
        with acquire_all(requests, run_root=workspace.run_root, canceled=lambda: True):
            pass
    except LockWaitCanceled:
        pytest.fail("a lock of the sweep is still held")


def test_two_workers_execute_each_node_once(tmp_path, design):
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design), max_workers=2)
    assert _dispositions(result, "yosys_fpga") == ["ran", "shared", "shared", "shared"]
    assert _dispositions(result, "nextpnr") == ["ran"] * 4
    assert dict(result.counts()) == {Outcome.PASSED: 4} and not result.canceled
    calls = _calls(workspace)
    assert calls["yosys"] == 1 and calls["nextpnr-ecp5"] == 4
    parents = _worker_pids(workspace)
    assert os.getpid() not in parents and 1 <= len(parents) <= 2


def test_canceling_stops_running_workers_tools(tmp_path, design, monkeypatch):
    ready = _tools_that_block(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    token = CancelToken()
    failure: list[BaseException] = []

    def cancel_when_both_block() -> None:
        try:
            _wait_until(lambda: _blocked_pids(ready), "both tools to block")
        except BaseException as error:  # noqa: BLE001 - the main thread raises it
            failure.append(error)
        finally:  # whatever happened, the sweep must not wait for ever
            token.cancel()

    thread = threading.Thread(target=cancel_when_both_block, daemon=True)
    thread.start()
    specs = _two_independent_points(design)
    try:
        begin = time.monotonic()
        result = workspace.sweep(specs, max_workers=2, cancel=token)
        thread.join()
        if failure:
            raise failure[0]
        assert time.monotonic() - begin < PROMPT
        assert result.canceled and {str(p.result.outcome) for p in result} == {"canceled"}
        assert {str(p.result.nodes["yosys_fpga"].outcome) for p in result} == {"canceled"}
        assert {str(p.result.nodes["nextpnr"].outcome) for p in result} == {"skipped"}
        assert len(_all_pids(ready)) == 4
        _stopped_and_free(workspace, specs, _all_pids(ready))
    finally:
        thread.join()
        for pid in _all_pids(ready):
            _kill(pid)


def test_a_raising_callback_stops_running_workers_and_propagates(tmp_path, design, monkeypatch):

    ready = _tools_that_block(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    # Three workers: two run the tools that block, and the third finishes `t_maker`, which
    # tells the callback while the other two run.
    specs = [*_two_independent_points(design), Spec("t_maker", design)]

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and event.node == "t_maker":
            _wait_until(lambda: _blocked_pids(ready), "both tools to block")
            raise RuntimeError("stop")

    try:
        begin = time.monotonic()
        with pytest.raises(RuntimeError, match="stop"):
            workspace.sweep(specs, max_workers=3, on_event=on_event)
        assert time.monotonic() - begin < PROMPT
        assert len(_all_pids(ready)) == 4
        _stopped_and_free(workspace, specs, _all_pids(ready))
    finally:
        for pid in _all_pids(ready):
            _kill(pid)


_SCRIPT = layout_script("""
import json
import sys
from pathlib import Path

from xeda import Design
from xeda.spec import Spec
from xeda.workspace import Workspace

def main():
    root = Path(sys.argv[1])
    design = Design(
        name="top", root=root / "design",
        rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]},
    )
    specs = [
        Spec("yosys_fpga+nextpnr", design,
             settings={"nextpnr.seed": seed, "nextpnr.fpga.part": %r})
        for seed in (1, 2)
    ]
    result = Workspace(root / "run", hashed_run_dirs=True).sweep(specs, max_workers=2)
    print(json.dumps([str(point.result.outcome) for point in result]))
""") % (ECP5,)


def _run_python(tmp_path: Path, *command: str) -> list[str]:
    """Run Python with `command` and the root of the test as its argument, in another directory.
    Return the outcomes that it printed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [sys.executable, *command, str(tmp_path)],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def test_a_guarded_script_runs_spawned_workers(tmp_path, design):
    script = tmp_path / "sweep_script.py"
    script.write_text(_SCRIPT + '\nif __name__ == "__main__":\n    main()\n')
    assert _run_python(tmp_path, str(script)) == ["passed", "passed"]


def test_a_notebook_like_main_runs_spawned_workers(tmp_path, design):
    # A notebook kernel's `__main__` has no file, and its cells have no guard. The `__main__` of
    # `python -c` has no file either. (Since Python 3.15, that of `python -` names `<stdin>`, which
    # `spawn` cannot read.)
    assert _run_python(
        tmp_path,
        "-c",
        _SCRIPT + "\nmain()\n",
    ) == ["passed", "passed"]


def test_a_script_without_a_main_guard_gives_each_point_the_cause(tmp_path, design):
    # Each worker imports the script again: its sweep is refused there, and the worker ends.
    report = (
        "    print(json.dumps([[str(p.result.outcome), p.result.error.code, p.result.error.message]"
        " for p in result]))\n"
    )
    script = tmp_path / "unguarded_script.py"
    script.write_text(
        _SCRIPT.replace(
            "    print(json.dumps([str(point.result.outcome) for point in result]))\n", report
        )
        + "\nmain()\n"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=120,
    )
    points = json.loads(done.stdout.splitlines()[-1])
    assert [outcome for outcome, _, _ in points] == ["failed", "failed"]
    assert {code for _, code, _ in points} == {"reported_failure"}
    for _, _, message in points:
        assert 'under `if __name__ == "__main__":`' in message
        assert "standard error of the worker" in message
    assert "Put the code that launches under" in done.stderr, "the worker says why it ended"


def test_events_of_workers_reach_the_callback_in_the_calling_thread(tmp_path, design):
    events: list[Event] = []
    threads: set[int] = set()

    def on_event(event: Event) -> None:
        threads.add(threading.get_ident())
        events.append(event)

    result = _workspace(tmp_path).sweep(_seeds(design, 3), max_workers=2, on_event=on_event)
    assert threads == {threading.get_ident()}
    assert len({point.result.launch_id for point in result}) == 3
    for point in result:
        mine = [(e.kind, e.node) for e in events if e.launch_id == point.result.launch_id]
        assert mine == [
            ("launch_started", None),
            ("node_started", "yosys_fpga"),
            ("node_finished", "yosys_fpga"),
            ("node_started", "nextpnr"),
            ("node_finished", "nextpnr"),
            ("launch_finished", None),
        ]


def test_workers_use_the_parent_snapshot(tmp_path, design, monkeypatch):
    monkeypatch.setenv(MARK, "OLD")
    workspace = _workspace(tmp_path)
    changed: list[str] = []

    def on_event(event: Event) -> None:
        # Workers that start after this change inherit the new variable, and must not use it.
        if event.kind == "node_finished" and not changed:
            changed.append(event.node or "")
            monkeypatch.setenv(MARK, "NEW")

    result = workspace.sweep(_seeds(design, 3), max_workers=2, on_event=on_event)
    assert changed and result.counts() == {Outcome.PASSED: 3}
    seen = {
        json.loads(line)["environment"].get(MARK)
        for file in workspace.run_root.rglob("fake_fpga.calls.jsonl")
        for line in file.read_text().splitlines()
    }
    assert seen == {"OLD"}


@pytest.mark.parametrize("workers", [1, 2])
def test_a_path_variable_expands_from_the_snapshot_of_the_sweep(
    tmp_path, design, monkeypatch, workers
):
    monkeypatch.setenv("XEDA_TEST_SUB", "reports_a")
    changed: list[str] = []

    def on_event(event: Event) -> None:
        # After the plan, before the nodes execute: a worker that starts now inherits this value.
        if event.kind == "launch_started" and not changed:
            changed.append(event.launch_id)
            monkeypatch.setenv("XEDA_TEST_SUB", "reports_b")

    specs = [_spec(design, seed, **{"yosys_fpga.reports_dir": "$XEDA_TEST_SUB"}) for seed in (1, 2)]
    result = _workspace(tmp_path).sweep(specs, max_workers=workers, on_event=on_event)
    assert changed and dict(result.counts()) == {Outcome.PASSED: 2}
    for point in result.points:
        run_dir = point.result.nodes["yosys_fpga"].run_dir
        assert (run_dir / "reports_a").is_dir() and not (run_dir / "reports_b").exists(), run_dir


def _plain_design(tmp_path: Path) -> Design:
    root = tmp_path / "d"
    root.mkdir()
    return Design(name="d", root=root, rtl={"sources": [], "top": "t"})


def test_a_flow_of_an_importable_module_runs_in_a_worker(tmp_path):
    # `tests.io_flows` registers its flows as it is imported. A spawned worker does not import it
    # by itself: it must import the module of each flow that Xeda does not have built in.

    design = _plain_design(tmp_path)
    specs = [
        Spec("t_maker+t_taker", design, settings={"t_maker.text": f"{text}\n"})
        for text in ("one", "two")
    ]
    result = Workspace(tmp_path / "run", hashed_run_dirs=True).sweep(specs, max_workers=2)
    assert dict(result.counts()) == {Outcome.PASSED: 2}
    reads = [point.result.nodes["t_taker"].record.metric("read") for point in result]
    assert reads == ["one\n", "two\n"]


_FACTORY = layout_script(
    "from xeda._internal.dataclass import Field\n"
    "from xeda.flow import Flow\n"
    "def make():\n"
    "    class RuntimeToy(Flow):\n"
    '        """A class that a function makes: no worker finds it in its module."""\n'
    "        results_description = {}\n"
    "        class Settings(Flow.Settings):\n"
    '            n: int = Field(0, description="Tells the points apart.")\n'
    "        def run(self): pass\n"
    "    return RuntimeToy\n"
)


def test_a_flow_that_its_module_makes_at_run_time_is_refused_before_anything_runs(
    tmp_path, monkeypatch
):
    _module_flow(tmp_path, monkeypatch, "factory_of_flows", _FACTORY)
    sys.modules["factory_of_flows"].make()
    design = _plain_design(tmp_path)
    specs = [Spec("runtime_toy", design, settings={"runtime_toy.n": n}) for n in (1, 2)]
    workspace = Workspace(tmp_path / "run", hashed_run_dirs=True)
    with pytest.raises(SpecError) as refused:
        workspace.sweep(specs, max_workers=2)
    assert refused.value.code == "flow_not_importable" and list(refused.value.points) == [0, 1]
    assert str(refused.value) == (
        "the flow `runtime_toy` is not at `make.<locals>.RuntimeToy` in `factory_of_flows`, so a "
        "worker process that imports the module does not make it. Define the class at the top "
        "level of the module, or sweep with max_workers=1."
    )
    assert not list(workspace.run_root.rglob("results.json"))
    assert dict(workspace.sweep(specs, max_workers=1).counts()) == {Outcome.PASSED: 2}


def test_a_worker_that_does_not_make_the_flow_fails_its_node_with_its_code(tmp_path, monkeypatch):
    # The module of a flow, as the parent plans it, holds the class. A worker imports the same
    # module and registers no flow.
    _module_flow(
        tmp_path,
        monkeypatch,
        "flow_of_the_parent",
        layout_script(
            "import multiprocessing\n"
            "from xeda.flow import Flow\n"
            "if multiprocessing.parent_process() is None:\n"
            "    class ParentOnlyFlow(Flow):\n"
            '        """A flow that only the parent makes."""\n'
            "        results_description = {}\n"
            "        def run(self): pass\n"
        ),
    )
    spec = Spec("parent_only_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    node = point.result.nodes["parent_only_flow"]
    assert point.result.outcome is Outcome.FAILED and node.error is not None
    assert node.error.code == "flow_not_importable" and node.disposition is Disposition.NOT_RUN
    assert node.error.message == (
        "a worker process does not rebuild the flow `parent_only_flow` as the sweep planned it, "
        "`ParentOnlyFlow` in `flow_of_the_parent`. It has no such flow."
    )
    assert not list((tmp_path / "run").rglob("results.json")), "no tool started"


def test_a_flow_defined_in_main_is_refused_before_anything_runs(tmp_path):
    class MyMainFlow(Flow):
        """A flow of a script: its module is `__main__`."""

        __module__ = "__main__"
        results_description: dict = {}

        class Settings(Flow.Settings):
            n: int = Field(0, description="Tells the points apart.")

        def run(self) -> None:
            pass

    design = _plain_design(tmp_path)
    specs = [Spec("my_main_flow", design, settings={"my_main_flow.n": n}) for n in (1, 2)]
    workspace = Workspace(tmp_path / "run", hashed_run_dirs=True)
    with pytest.raises(SpecError) as refused:
        workspace.sweep(specs, max_workers=2)
    assert refused.value.code == "flow_not_importable" and list(refused.value.points) == [0, 1]
    assert str(refused.value) == (
        "the flow `my_main_flow` is defined in `__main__`, so a worker process cannot import it. "
        "Move it into a module, or sweep with max_workers=1."
    )
    assert not list(workspace.run_root.rglob("results.json"))
    # One worker runs it in this process, as before.
    assert dict(workspace.sweep(specs, max_workers=1).counts()) == {Outcome.PASSED: 2}


_MESSAGE = (
    "the flow `my_flow` is defined in `{module}`, which a worker process cannot import. "
    "Move it into a module that Python can import, or sweep with max_workers=1."
)


def _flow_of_module(module: str) -> type[Flow]:
    class MyFlow(Flow):
        """A flow whose class says that it is defined in `module`."""

        __module__ = module
        results_description: dict = {}

        class Settings(Flow.Settings):
            n: int = Field(0, description="Tells the points apart.")

        def run(self) -> None:
            pass

    return MyFlow


def _refused_modules_specs(tmp_path: Path) -> tuple[Workspace, list[Spec]]:
    design = _plain_design(tmp_path)
    specs = [Spec("my_flow", design, settings={"my_flow.n": n}) for n in (1, 2)]
    return Workspace(tmp_path / "run", hashed_run_dirs=True), specs


def _assert_refused(workspace: Workspace, specs: list[Spec], module: str) -> None:
    with pytest.raises(SpecError) as refused:
        workspace.sweep(specs, max_workers=2)
    assert refused.value.code == "flow_not_importable" and list(refused.value.points) == [0, 1]
    assert str(refused.value) == _MESSAGE.format(module=module)
    assert not list(workspace.run_root.rglob("results.json"))
    # One worker runs it in this process, as before.
    assert dict(workspace.sweep(specs, max_workers=1).counts()) == {Outcome.PASSED: 2}


def test_a_flow_of_a_module_that_does_not_exist_is_refused_before_anything_runs(tmp_path):
    _flow_of_module("no_such_scripts.foo")
    workspace, specs = _refused_modules_specs(tmp_path)
    _assert_refused(workspace, specs, "no_such_scripts.foo")


def test_a_flow_of_a_module_loaded_from_a_file_under_a_name_is_refused(tmp_path, monkeypatch):
    # The module is in `sys.modules`, but no other process finds it by that name.
    import importlib.util

    source = tmp_path / "foo.py"
    source.write_text("")
    spec = importlib.util.spec_from_file_location("loaded_scripts.foo", source)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "loaded_scripts.foo", module)
    _flow_of_module("loaded_scripts.foo")
    workspace, specs = _refused_modules_specs(tmp_path)
    _assert_refused(workspace, specs, "loaded_scripts.foo")


def _module_flow(tmp_path: Path, monkeypatch, name: str, source: str) -> None:
    """Write the module `name` with `source` into `tmp_path`, make it importable by name in this
    process and in a spawned worker (which gets `sys.path`), and import it: its flow registers."""
    import importlib

    (tmp_path / f"{name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.import_module(name)


def test_a_module_that_a_worker_fails_to_import_fails_the_node_with_its_code(tmp_path, monkeypatch):
    _module_flow(
        tmp_path,
        monkeypatch,
        "importable_by_the_parent",
        layout_script(
            "import multiprocessing\n"
            "from xeda.flow import Flow\n"
            "if multiprocessing.parent_process() is not None:\n"
            "    raise ImportError('only the parent imports this')\n"
            "class WorkerFlow(Flow):\n"
            '    """A flow that only the parent can import."""\n'
            "    results_description = {}\n"
            "    def run(self): pass\n"
        ),
    )
    spec = Spec("worker_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    node = point.result.nodes["worker_flow"]
    assert point.result.outcome is Outcome.FAILED and node.error is not None
    assert node.error.code == "flow_not_importable"
    assert "importable_by_the_parent" in node.error.message


@pytest.mark.parametrize("statement", ["sys.exit(3)", "raise KeyboardInterrupt"])
def test_a_module_that_leaves_the_worker_at_import_fails_its_node_not_the_sweep(
    tmp_path, monkeypatch, statement
):
    _module_flow(
        tmp_path,
        monkeypatch,
        "module_that_leaves",
        layout_script(
            "import multiprocessing, sys\n"
            "from xeda.flow import Flow\n"
            "if multiprocessing.parent_process() is not None:\n"
            f"    {statement}\n"
            "class ModuleLeavingFlow(Flow):\n"
            '    """A flow whose module leaves a worker when the worker imports it."""\n'
            "    results_description = {}\n"
            "    def run(self): pass\n"
        ),
    )
    spec = Spec("module_leaving_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    node = point.result.nodes["module_leaving_flow"]
    assert point.result.outcome is Outcome.FAILED and node.error is not None
    assert node.error.code == "flow_not_importable"


# ------------------------------------------------------- a task of a process with other code


def _made_by_other_code(task: NodeTask, drop: tuple[str, ...], add: dict[str, Any]) -> NodeTask:
    """Return `task` as a worker gets it from a process whose code gives a task no fields `drop`
    and the fields `add`: unpickling sets the fields that the pickle holds, whatever the class in
    the worker declares."""
    other = copy.copy(task)
    for name in drop:
        del vars(other)[name]
    vars(other).update(add)
    return pickle.loads(pickle.dumps(other))


@pytest.mark.parametrize(
    "drop, add",
    [
        # Xeda from before a task named its code.
        (("code",), {}),
        # Xeda from before `flows`: a missing field must not read as its default here.
        (("flows",), {"modules": ()}),
        # A newer Xeda, whose task has one more field.
        ((), {"later": 1}),
        # Other code of Xeda, and a module that does not exist: the worker compares the code
        # before it imports the module of any flow.
        ((), {"code": {"xeda": "0" * 32}, "flows": (("no_flow", "no_such_module", "NoFlow"),)}),
    ],
    ids=["no-code", "old-field", "unknown-field", "other-digest"],
)
def test_a_worker_refuses_the_task_of_other_code_before_it_reads_it(tmp_path, design, drop, add):
    workspace = _workspace(tmp_path)
    task = _task(workspace, _spec(design, 1), "yosys_fpga", [])
    outcome = worker_main(_made_by_other_code(task, drop, add))
    error = outcome.result.error
    assert outcome.result.outcome is Outcome.FAILED and error is not None
    assert (outcome.node, error.node, error.code) == ("yosys_fpga", "yosys_fpga", "stale_runtime")
    assert outcome.result.disposition is Disposition.NOT_RUN, "a refused task did not run"
    assert error.message == (
        "The process that planned the sweep runs other Xeda code than the code on disk, which this "
        "worker process imported: the files changed after that process imported them. Restart the "
        "process that sweeps (in Jupyter, restart the kernel), and sweep again."
    )
    assert not _calls(workspace) and not list(workspace.run_root.rglob("results.json"))


def test_a_worker_refuses_a_task_whose_flow_code_differs_before_the_run_begins(tmp_path, design):
    # The code of Xeda matches, but the sweep planned with other code of the flow: the entry of
    # `run_node` refuses the task before the run of the node began.
    workspace = _workspace(tmp_path)
    task = _task(workspace, _spec(design, 1), "yosys_fpga", [])
    assert "yosys_fpga" in task.code
    outcome = worker_main(dataclasses.replace(task, code={**task.code, "yosys_fpga": "0" * 32}))
    error = outcome.result.error
    assert error is not None and error.code == "stale_runtime"
    assert "the code of the flow `yosys_fpga` differs" in error.message
    assert outcome.result.disposition is Disposition.NOT_RUN
    assert not _calls(workspace) and not list(workspace.run_root.rglob("results.json"))


@pytest.mark.parametrize(
    "error", [BrokenProcessPool("broken"), OSError(24, "Too many open files")], ids=repr
)
@pytest.mark.parametrize(
    "failing, dispositions",
    [
        # The workers start before the first node: a pool that cannot start them runs no node.
        ("warm-up", ["not_run"] * 4),
        # Every worker started, so the submit of a node starts no process. A broken pool refuses
        # the task before it queues it: no point ran the node. Any other error comes after the
        # pool queued the task, which a worker may still run: the sweep cannot tell, and counts
        # it as run once and shared.
        ("node", None),
    ],
)
def test_a_task_that_the_pool_does_not_take_is_not_run_only_when_the_pool_refused_it(
    tmp_path, design, monkeypatch, error, failing, dispositions
):
    real = ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        if failing == "warm-up" or fn is worker_main:
            raise error
        return real(self, fn, *args, **kwargs)

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design), max_workers=2)
    assert all(point.result.outcome is Outcome.FAILED for point in result)
    if dispositions is None:
        refused = isinstance(error, BrokenProcessPool)
        dispositions = ["not_run"] * 4 if refused else ["ran", "shared", "shared", "shared"]
    assert _dispositions(result, "yosys_fpga") == dispositions
    assert not _calls(workspace)


def test_the_warm_ups_start_every_worker_before_any_node(tmp_path):
    """No warm-up returns before the calling process lets them go, so no worker is idle while it
    submits them: each submit starts a process, and the pool has all its workers before the first
    node. Each warm-up reads one message, so none is left to read as a request to stop."""
    import multiprocessing
    from concurrent.futures import wait

    context = multiprocessing.get_context("spawn")
    channel = sweep.WorkerChannel(context)
    before = set(multiprocessing.active_children())
    try:
        with ProcessPoolExecutor(
            3, mp_context=context, initializer=sweep._start_worker, initargs=(channel.reader,)
        ) as pool:
            try:
                warm_ups = [pool.submit(sweep._warm_up) for _ in range(3)]
                assert len(set(multiprocessing.active_children()) - before) == 3
                done, _ = wait(warm_ups, timeout=0.5)
                assert not done, "a warm-up went on before the calling process let it"
                channel.release(3)
                for future in warm_ups:
                    future.result(timeout=60)
                assert not channel.reader.poll(), "every message was read"
            finally:
                channel.cancel()  # a warm-up that still waits ends, and the pool shuts down
    finally:
        channel.close()


def test_a_cancel_while_the_workers_start_runs_no_node(tmp_path, design, monkeypatch):
    """The warm-ups wait for a release that never comes: the cancel ends them, and the sweep runs
    no node."""
    token = CancelToken()
    monkeypatch.setattr(sweep.WorkerChannel, "release", lambda self, count: token.cancel())
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design), max_workers=2, cancel=token)
    assert result.canceled
    assert {str(n.outcome) for p in result for n in p.result.nodes.values()} == {"skipped"}
    assert not _calls(workspace)


def test_the_workers_start_before_the_first_node_and_are_no_more_than_the_nodes(tmp_path, design):
    """Three nodes (one `yosys_fpga` that both points share, and a `nextpnr` for each): the pool
    starts three workers of the eight allowed, all before the first node ends."""
    import multiprocessing

    before = set(multiprocessing.active_children())
    counted: list[int] = []

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and not counted:
            counted.append(len(set(multiprocessing.active_children()) - before))

    result = _workspace(tmp_path).sweep(_seeds(design, 2), max_workers=8, on_event=on_event)
    assert all(point.result.passed for point in result)
    assert counted == [3]


def test_a_stopped_sweep_tells_a_node_that_ended_after_one_that_never_started(
    tmp_path, monkeypatch
):
    """`t_right` ends while `t_left`, before it in the plan, never starts, and the sweep stops: the
    node that ended has its events all the same, and the nodes that never started have none."""
    token = CancelToken()
    start, collect = sweep._Sweep._start, sweep._Sweep._collect

    def start_all_but_t_left(self, unit, pool, channel, running):
        if unit.node.name != "t_left":
            start(self, unit, pool, channel, running)

    def stop_after_t_right(self, unit, future):
        collect(self, unit, future)
        if unit.node.name == "t_right":
            token.cancel()

    monkeypatch.setattr(sweep._Sweep, "_start", start_all_but_t_left)
    monkeypatch.setattr(sweep._Sweep, "_collect", stop_after_t_right)
    events: list[Event] = []
    (point,) = Workspace(tmp_path / "run").sweep(
        [Spec("t_join", _plain_design(tmp_path))],
        max_workers=2,
        cancel=token,
        on_event=events.append,
    )
    nodes = point.result.nodes
    assert {name: str(node.outcome) for name, node in nodes.items()} == {
        "t_fork": "passed",
        "t_left": "skipped",
        "t_right": "passed",
        "t_join": "skipped",
    }
    assert [(e.kind, e.node) for e in events] == [
        ("launch_started", None),
        ("node_started", "t_fork"),
        ("node_finished", "t_fork"),
        ("node_started", "t_right"),
        ("node_finished", "t_right"),
        ("launch_finished", None),
    ]


def test_a_launch_and_a_sweep_tell_the_same_nodes_of_a_diamond_whose_fork_failed(
    tmp_path, monkeypatch
):
    """`t_left` and `t_right` read `t_fork`, and `t_join` reads both. When `t_fork` fails, a
    launch never enters `t_right`, and a sweep settles the branches without executing them (the
    sweeping process settles them so with workers too). Every node has its two events all the
    same, with its outcome in the result."""

    def fail(self):
        raise RuntimeError("the fork fails")

    monkeypatch.setattr(io_flows.TFork, "run", fail)
    design = _plain_design(tmp_path)
    launched: list[Event] = []
    alone = Workspace(tmp_path / "alone").run(Spec("t_join", design), on_event=launched.append)
    swept: list[Event] = []
    (point,) = Workspace(tmp_path / "run").sweep([Spec("t_join", design)], on_event=swept.append)
    for result, events in ((alone, launched), (point.result, swept)):
        assert result.nodes["t_fork"].outcome is Outcome.FAILED
        assert {str(node.outcome) for name, node in result.nodes.items() if name != "t_fork"} == {
            "blocked"
        }
        finished = {e.node: (e.outcome, e.disposition) for e in events if e.kind == "node_finished"}
        assert finished == {n.name: (n.outcome, n.disposition) for n in result.nodes.values()}
        kinds = [(e.kind, e.node) for e in events]
        assert sorted(kinds) == sorted(
            [("launch_started", None), ("launch_finished", None)]
            + [(kind, name) for name in result.nodes for kind in ("node_started", "node_finished")]
        )
        for name in result.nodes:
            assert kinds.index(("node_started", name)) < kinds.index(("node_finished", name))


def test_a_failure_that_leaves_the_launch_of_a_node_without_a_result_is_ran(
    tmp_path, design, monkeypatch
):
    # Whether the run of the node began is unknown, so the worker reports the node as executed.
    def broken(task, **_):
        raise RuntimeError("a bug of the launch")

    monkeypatch.setattr(sweep, "run_node", broken)
    workspace = _workspace(tmp_path)
    outcome = worker_main(_task(workspace, _spec(design, 1), "yosys_fpga", []))
    assert outcome.result.error is not None and outcome.result.error.code == "internal_error"
    assert outcome.result.disposition is Disposition.RAN


def test_a_worker_reports_a_task_that_names_no_node(tmp_path, design):
    # The error reads no field that the task may lack. The process that sweeps fills in the name.
    workspace = _workspace(tmp_path)
    task = _task(workspace, _spec(design, 1), "yosys_fpga", [])
    outcome = worker_main(_made_by_other_code(task, ("node", "code"), {}))
    error = outcome.result.error
    assert error is not None and error.code == "stale_runtime"
    assert (outcome.node, error.node) == ("", None)


def test_workers_refuse_the_tasks_of_a_process_that_runs_older_code(tmp_path, design, monkeypatch):
    # The process that sweeps makes its tasks as a Xeda from before a task named its code: a
    # notebook kernel that imported Xeda before an update. Each worker imports the code on disk.
    make = sweep._Sweep._task

    def old_task(self, unit):
        task = make(self, unit)
        del vars(task)["code"]
        return task

    monkeypatch.setattr(sweep._Sweep, "_task", old_task)
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design), max_workers=2)
    for point in result:
        error = point.result.error
        assert point.result.outcome is Outcome.FAILED and error is not None
        assert (error.code, error.node) == ("stale_runtime", "yosys_fpga")
        assert error.message.startswith("The process that planned the sweep runs other Xeda code")
        assert point.result.nodes["yosys_fpga"].name == "yosys_fpga"
    # Refused in every point: no point ran it, so no point shares a run of it.
    assert _dispositions(result, "yosys_fpga") == ["not_run"] * len(result.points)
    assert not _calls(workspace) and not list(workspace.run_root.rglob("results.json"))


def test_a_worker_that_ends_after_the_code_changed_on_disk_fails_its_node_with_stale_runtime(
    tmp_path, monkeypatch
):
    # The worker imports the module of the flow, which changes on disk and ends the worker at
    # once, as a task that the code on disk cannot unpickle ends it. The process that sweeps finds
    # that its own code changed: the cause is `stale_runtime`, not a script without a main guard.
    _module_flow(
        tmp_path,
        monkeypatch,
        "module_that_changes",
        layout_script(
            "import multiprocessing, os, pathlib\n"
            "from xeda.flow import Flow\n"
            "if multiprocessing.parent_process() is not None:\n"
            "    path = pathlib.Path(__file__)\n"
            "    path.write_text(path.read_text() + '# changed\\n')\n"
            "    os._exit(1)\n"
            "class ChangingFlow(Flow):\n"
            '    """A flow whose module changes on disk when a worker imports it."""\n'
            "    results_description = {}\n"
            "    def run(self): pass\n"
        ),
    )
    spec = Spec("changing_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    error = point.result.nodes["changing_flow"].error
    assert point.result.outcome is Outcome.FAILED and error is not None
    assert error.code == "stale_runtime"
    # A worker that died leaves the sweep unable to tell whether the run began: it reports `ran`.
    assert point.result.nodes["changing_flow"].disposition is Disposition.RAN
    assert "terminated abruptly" in error.message and "__main__" not in error.message


def test_a_tool_that_a_worker_starts_does_not_ignore_ctrl_c(tmp_path):
    # An ignored signal stays ignored across `exec`, a handler does not: the worker must not leave
    # SIGINT ignored for the tools that it starts.
    script = layout_script(
        "import multiprocessing, signal, subprocess, sys\n"
        "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
        "from xeda.sweep import _start_worker\n"
        "_start_worker(multiprocessing.Pipe(duplex=False)[0])\n"
        "child = 'import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)'\n"
        "print(subprocess.run([sys.executable, '-c', child], capture_output=True, text=True,"
        " check=True).stdout.strip())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("statement", ["sys.exit(3)", "raise KeyboardInterrupt"])
def test_a_flow_that_leaves_the_worker_fails_its_node_not_the_sweep(
    tmp_path, monkeypatch, statement
):
    _module_flow(
        tmp_path,
        monkeypatch,
        "flow_that_leaves",
        layout_script(
            "import sys\n"
            "from xeda.flow import Flow\n"
            "class LeavingFlow(Flow):\n"
            '    """A flow that leaves its process."""\n'
            "    results_description = {}\n"
            f"    def run(self): {statement}\n"
        ),
    )
    spec = Spec("leaving_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    node = point.result.nodes["leaving_flow"]
    assert point.result.outcome is Outcome.FAILED and node.error is not None
    assert node.error.code == "reported_failure"
    # The run began, and the exception left it with no result: the worker reports `ran`.
    assert node.disposition is Disposition.RAN


def test_the_events_of_a_diamond_come_in_the_order_of_the_plan(tmp_path):
    # `t_left` and `t_right` run at once in two workers. Their events still come as the plan has
    # them: a `node_started` and a `node_finished` for each node, one node after the other.

    workspace = Workspace(tmp_path / "run")
    spec = Spec("t_join", _plain_design(tmp_path))
    names = list(workspace.plan(spec).nodes)
    assert names[0] == "t_fork" and names[-1] == "t_join" and len(names) == 4
    events: list[Event] = []
    (point,) = workspace.sweep([spec], max_workers=2, on_event=events.append)
    assert point.result.outcome is Outcome.PASSED
    assert [(e.kind, e.node) for e in events] == [
        ("launch_started", None),
        *[(kind, name) for name in names for kind in ("node_started", "node_finished")],
        ("launch_finished", None),
    ]


_BLOCKED_SWEEP = layout_script("""
import sys
from pathlib import Path

from xeda import Design
from xeda.spec import Spec
from xeda.workspace import Workspace

def main():
    root = Path(sys.argv[1])
    design = Design(
        name="top", root=root / "design",
        rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]},
    )
    base = {"nextpnr.seed": 1, "nextpnr.fpga.part": %r}
    specs = [
        Spec("yosys_fpga+nextpnr", design, settings=base),
        Spec("yosys_fpga+nextpnr", design, settings={**base, "yosys_fpga.nobram": True}),
    ]
    Workspace(root / "run", hashed_run_dirs=True).sweep(specs, max_workers=2)

if __name__ == "__main__":
    main()
""") % (ECP5,)


def test_killing_the_calling_process_stops_the_tools_of_the_workers(tmp_path, design, monkeypatch):
    ready = _tools_that_block(tmp_path, monkeypatch)
    script = tmp_path / "killed_sweep.py"
    script.write_text(_BLOCKED_SWEEP)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    caller = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        cwd=elsewhere,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    workspace = _workspace(tmp_path)
    try:
        _wait_until(lambda: _blocked_pids(ready), "both tools to block")
        caller.kill()  # SIGKILL: nothing in the process gets to stop its workers
        caller.wait()
        pids = _all_pids(ready)
        # The tools and their children end, the workers end, and every lock is free.
        _stopped_and_free(
            workspace, _two_independent_points(design), pids + [*_worker_pids(workspace)]
        )
    finally:
        caller.kill()
        caller.wait()
        for pid in [*_all_pids(ready), *_worker_pids(workspace)]:
            _kill(pid)


def test_a_sweep_with_workers_makes_no_temporary_directory_for_its_sockets(tmp_path, design):
    # A `multiprocessing.Manager` makes `pymp-*` with a socket in the temporary directory, which
    # is a write outside the run root. It is made by a process that this one starts, so no audit
    # hook of this process sees it: look at the directory while the sweep runs.
    import tempfile

    places = {Path(tempfile.gettempdir()), Path("/tmp")}

    def made() -> set[Path]:
        return {entry for place in places for entry in place.glob("pymp-*")}

    before = made()
    seen: list[set[Path]] = []
    result = _workspace(tmp_path).sweep(
        _seeds(design, 2), max_workers=2, on_event=lambda event: seen.append(made() - before)
    )
    assert dict(result.counts()) == {Outcome.PASSED: 2}
    assert seen and not any(seen), seen
