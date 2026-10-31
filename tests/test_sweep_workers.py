"""`Workspace.sweep(max_workers=N)` with N above 1 executes the nodes in worker processes.

The workers start with `spawn` on every platform. The tests run the same fake tools as
`tests/test_sweep.py`. A test that stops running tools waits for each tool to write a file
(`XEDA_FAKE_FPGA_READY` names a directory), never for a time to pass.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from xeda import Design
from xeda.cancel import CancelToken
from xeda.dataclass import Field
from xeda.errors import SpecError
from xeda.events import Event
from xeda.flow import Flow
from xeda.flow_runner.run_lock import LockRequest, LockWaitCanceled, acquire_all
from xeda.outcomes import Outcome
from xeda.spec import Spec
from xeda.workspace import Workspace

from . import tool_utils
from .test_proc_utils import _is_running, _kill, _wait_until
from .test_sweep import ECP5, MARK, _calls, _dispositions, _seeds, _spec, _workspace

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


def _blocked_pids(ready: Path) -> list[int] | None:
    """The process ids of two tools and their children, once both tools block; else `None`."""
    files = sorted(ready.glob("*.pid"))
    if len(files) < 2:
        return None
    return [int(pid) for file in files for pid in file.read_text().split()]


def _two_independent_points(design) -> list:
    return [_spec(design, 1), _spec(design, 1, **{"yosys_fpga.nobram": True})]


def _assert_all_stopped_and_free(workspace: Workspace, specs: list, pids: list[int]) -> None:
    try:
        assert not [pid for pid in pids if _is_running(pid)]
    finally:
        for pid in pids:
            _kill(pid)
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
    parents = {
        json.loads(line)["ppid"]
        for file in workspace.run_root.rglob("fake_fpga.calls.jsonl")
        for line in file.read_text().splitlines()
    }
    assert os.getpid() not in parents and 1 <= len(parents) <= 2


def test_canceling_stops_running_workers_tools(tmp_path, design, monkeypatch):
    ready = _tools_that_block(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    token = CancelToken()
    found: list[list[int]] = []

    def cancel_when_both_block() -> None:
        found.append(_wait_until(lambda: _blocked_pids(ready), "both tools to block"))
        token.cancel()

    thread = threading.Thread(target=cancel_when_both_block, daemon=True)
    thread.start()
    begin = time.monotonic()
    specs = _two_independent_points(design)
    result = workspace.sweep(specs, max_workers=2, cancel=token)
    thread.join()
    assert time.monotonic() - begin < PROMPT
    assert result.canceled and {str(p.result.outcome) for p in result} == {"canceled"}
    assert {str(p.result.nodes["yosys_fpga"].outcome) for p in result} == {"canceled"}
    assert {str(p.result.nodes["nextpnr"].outcome) for p in result} == {"skipped"}
    (pids,) = found
    assert len(pids) == 4
    _assert_all_stopped_and_free(workspace, specs, pids)


def test_a_raising_callback_stops_running_workers_and_propagates(tmp_path, design, monkeypatch):
    ready = _tools_that_block(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    found: list[list[int]] = []
    started: list[str | None] = []

    def on_event(event: Event) -> None:
        if event.kind == "node_started":
            started.append(event.node)
            if len(started) == 2:  # both nodes went to a worker: wait until both tools run
                found.append(_wait_until(lambda: _blocked_pids(ready), "both tools to block"))
                raise RuntimeError("stop")

    specs = _two_independent_points(design)
    begin = time.monotonic()
    with pytest.raises(RuntimeError, match="stop"):
        workspace.sweep(specs, max_workers=2, on_event=on_event)
    assert time.monotonic() - begin < PROMPT
    ((*pids,),) = found
    _assert_all_stopped_and_free(workspace, specs, pids)


_SCRIPT = """
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
""" % (ECP5,)


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


def _plain_design(tmp_path: Path) -> Design:
    root = tmp_path / "d"
    root.mkdir()
    return Design(name="d", root=root, rtl={"sources": [], "top": "t"})


def test_a_flow_of_an_importable_module_runs_in_a_worker(tmp_path):
    # `tests.io_flows` registers its flows as it is imported. A spawned worker does not import it
    # by itself: it must import the module of each flow that Xeda does not have built in.
    from . import io_flows  # noqa: F401

    design = _plain_design(tmp_path)
    specs = [
        Spec("t_maker+t_taker", design, settings={"t_maker.text": f"{text}\n"})
        for text in ("one", "two")
    ]
    result = Workspace(tmp_path / "run", hashed_run_dirs=True).sweep(specs, max_workers=2)
    assert dict(result.counts()) == {Outcome.PASSED: 2}
    reads = [point.result.nodes["t_taker"].record.metric("read") for point in result]
    assert reads == ["one\n", "two\n"]


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


def test_a_module_that_a_worker_fails_to_import_fails_the_node_with_its_code(tmp_path, monkeypatch):
    (tmp_path / "importable_by_the_parent.py").write_text(
        "import multiprocessing\n"
        "from xeda.flow import Flow\n"
        "if multiprocessing.parent_process() is not None:\n"
        "    raise ImportError('only the parent imports this')\n"
        "class WorkerFlow(Flow):\n"
        '    """A flow that only the parent can import."""\n'
        "    results_description = {}\n"
        "    def run(self): pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "importable_by_the_parent", raising=False)
    import importlib

    importlib.import_module("importable_by_the_parent")
    spec = Spec("worker_flow", _plain_design(tmp_path))
    (point,) = Workspace(tmp_path / "run").sweep([spec], max_workers=2)
    node = point.result.nodes["worker_flow"]
    assert point.result.outcome is Outcome.FAILED and node.error is not None
    assert node.error.code == "flow_not_importable"
    assert "importable_by_the_parent" in node.error.message
