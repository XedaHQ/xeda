"""Launches that run at the same time, in threads or in processes, do not disturb each other.

A launch never changes the working directory of its process: paths resolve against explicit
roots, and every tool starts in its own directory. If a launch did change it, two threads of one
process would lose files to each other's directory, and a thread would fail when another removed
the directory it stood in.

Each test starts two launches on one design and one run root at a single barrier. One launch
runs the flow. If both launch the same flow, the second waits for the run directory lock and then
reuses the result or runs again. Either way, both must pass and agree with a launch that had the
machine to itself, and each run directory must hold what that launch alone writes.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.digest import TIME_MARKER_PREFIX
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Nvc
from xeda.run_root import ensure_run_root

from .tool_utils import require_ghdl, require_nvc_evidence

pytestmark = pytest.mark.real_tool

TESTBENCH = """\
entity tb is end;
architecture rtl of tb is
begin
  process begin
    wait for 5 ns;
    report "DONE";
    std.env.finish;
    wait;
  end process;
end;
"""

DESIGN = """\
name: concurrent
language.vhdl.standard: 2008
rtl:
  sources:
    - tb.vhdl
  top: tb
tb:
  top: tb
"""

#: The results that a simulation of `TESTBENCH` reports, whatever the simulator.
RESULT_KEYS = ("sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings")

#: The flows of a pair: the same flow twice, or two flows of one design.
PAIRS = {"same_flow": ("ghdl_sim", "ghdl_sim"), "different_flows": ("ghdl_sim", "nvc")}
FLOWS = {"ghdl_sim": GhdlSim, "nvc": Nvc}

TIMEOUT = 120.0


def _require_tools(pair: tuple[str, str]) -> None:
    require_ghdl()
    if "nvc" in pair:
        require_nvc_evidence()


def _write_design(root: Path) -> Path:
    root.mkdir()
    (root / "tb.vhdl").write_text(TESTBENCH)
    design_file = root / "concurrent.yaml"
    design_file.write_text(DESIGN)
    return design_file


def _files(directory: Path) -> list[str]:
    """List the files below `directory`, except the clock markers that a freshness check
    creates and removes. List only when no launch is running."""
    return sorted(
        os.fspath(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and not path.name.startswith(TIME_MARKER_PREFIX)
    )


def _summary(results: Any) -> dict[str, Any]:
    return {key: results[key] for key in RESULT_KEYS}


class _Alone:
    """What a launch that has the machine to itself reports and writes, per flow name."""

    def __init__(self, design_file: Path, run_root: Path) -> None:
        self.results: dict[str, dict[str, Any]] = {}
        self.files: dict[str, list[str]] = {}
        runner = DefaultRunner(run_root, display_results=False)
        for name, flow_class in FLOWS.items():
            flow = runner.run_flow(flow_class, Design.from_file(design_file), {})
            assert flow is not None and flow.succeeded
            self.results[name] = _summary(flow.results)
            self.files[name] = _files(flow.run_path)


@pytest.fixture(scope="module")
def alone(tmp_path_factory: pytest.TempPathFactory) -> _Alone:
    """Run both flows once, before any concurrent launch of the module."""
    require_ghdl()
    require_nvc_evidence()
    base = tmp_path_factory.mktemp("alone")
    return _Alone(_write_design(base / "design"), base / "run")


def _check_launch(saved: dict[str, Any], files: list[str], flow_name: str, alone: _Alone) -> None:
    """Check the saved results and files of a finished launch against the same flow run alone."""
    assert saved["success"] is True, saved
    assert _summary(saved) == alone.results[flow_name]
    assert files == alone.files[flow_name]


def _check_run_root(run_root: Path, design_root: Path, names: set[str]) -> None:
    """Nothing but the run directories and their lock files exists, and the design tree is
    unchanged."""
    expected = names | {f"{name}.lock" for name in names}
    assert {path.name for path in (run_root / "concurrent").iterdir()} == expected
    assert sorted(path.name for path in design_root.iterdir()) == ["concurrent.yaml", "tb.vhdl"]


class _WorkingDirectoryWatch:
    """A thread that reads the working directory of the process over and over."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.errors: list[str] = []
        self.reads = 0
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="working-directory-watch")

    def _poll(self) -> None:
        self._started.set()
        while not self._stop.is_set():
            try:
                self.seen.add(os.getcwd())
            except OSError as e:  # the directory the process stands in is gone
                self.errors.append(repr(e))
            self.reads += 1
            time.sleep(0.0005)

    def __enter__(self) -> "_WorkingDirectoryWatch":
        self._thread.start()
        assert self._started.wait(TIMEOUT)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(TIMEOUT)


@pytest.mark.parametrize("pair", PAIRS.values(), ids=PAIRS.keys())
def test_two_threads_launch_on_one_design_at_once(tmp_path, alone, pair):
    _require_tools(pair)
    design_root = tmp_path / "design"
    design_file = _write_design(design_root)
    run_root = tmp_path / "run"
    # The run root exists before the launches start. Creating it is not what this test covers.
    ensure_run_root(run_root)
    start = os.getcwd()
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    outcomes: list[Any] = [None, None]

    def launch(index: int) -> None:
        try:
            runner = DefaultRunner(run_root, display_results=False)
            design = Design.from_file(design_file)
            barrier.wait()
            flow = runner.run_flow(FLOWS[pair[index]], design, {})
            assert flow is not None
            outcomes[index] = (flow.succeeded, _summary(flow.results), flow.run_path)
        except BaseException as e:  # pylint: disable=broad-except
            outcomes[index] = e

    threads = [threading.Thread(target=launch, args=(i,), name=f"launch-{i}") for i in range(2)]
    with _WorkingDirectoryWatch() as watch:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(TIMEOUT)
    assert not any(thread.is_alive() for thread in threads)

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), repr(outcome)
    assert watch.errors == []
    assert watch.reads > 0
    assert watch.seen == {start}
    assert os.getcwd() == start
    for index, flow_name in enumerate(pair):
        succeeded, results, run_path = outcomes[index]
        assert succeeded
        assert results == alone.results[flow_name]
        assert run_path == run_root / "concurrent" / flow_name
        saved = json.loads((run_path / "results.json").read_text())
        _check_launch(saved, _files(run_path), flow_name, alone)
    _check_run_root(run_root, design_root, set(pair))


CHILD = """\
import json
import os
import sys
import time
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Nvc

design_file, run_root, flow_name, sync, index, out = sys.argv[1:]
flows = {"ghdl_sim": GhdlSim, "nvc": Nvc}
design = Design.from_file(Path(design_file))
runner = DefaultRunner(Path(run_root), display_results=False)
(Path(sync) / f"ready-{index}").write_text("")
deadline = time.monotonic() + 60
while len(list(Path(sync).glob("ready-*"))) < 2:
    if time.monotonic() > deadline:
        sys.exit("the other launch never became ready")
    time.sleep(0.001)
flow = runner.run_flow(flows[flow_name], design, {})
assert flow is not None
Path(out).write_text(json.dumps({
    "succeeded": flow.succeeded,
    "run_path": os.fspath(flow.run_path),
    "cwd": os.getcwd(),
}))
"""


@pytest.mark.parametrize("pair", PAIRS.values(), ids=PAIRS.keys())
def test_two_processes_launch_on_one_design_at_once(tmp_path, alone, pair):
    _require_tools(pair)
    design_root = tmp_path / "design"
    design_file = _write_design(design_root)
    run_root = tmp_path / "run"
    ensure_run_root(run_root)
    sync = tmp_path / "sync"
    sync.mkdir()
    children = []
    for index, flow_name in enumerate(pair):
        start = tmp_path / f"start-{index}"
        start.mkdir()
        argv = [
            sys.executable,
            "-c",
            CHILD,
            os.fspath(design_file),
            os.fspath(run_root),
            flow_name,
            os.fspath(sync),
            str(index),
            os.fspath(tmp_path / f"out-{index}.json"),
        ]
        children.append(
            subprocess.Popen(
                argv, cwd=start, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        )
    outputs = []
    try:
        for child in children:
            outputs.append(child.communicate(timeout=TIMEOUT)[0])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    for child, output in zip(children, outputs):
        assert child.returncode == 0, output

    for index, flow_name in enumerate(pair):
        reported = json.loads((tmp_path / f"out-{index}.json").read_text())
        assert reported["succeeded"] is True
        assert reported["cwd"] == os.fspath((tmp_path / f"start-{index}").resolve())
        assert list((tmp_path / f"start-{index}").iterdir()) == []
        run_path = run_root / "concurrent" / flow_name
        saved = json.loads((run_path / "results.json").read_text())
        _check_launch(saved, _files(run_path), flow_name, alone)
    _check_run_root(run_root, design_root, set(pair))
