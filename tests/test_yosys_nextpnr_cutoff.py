"""The early cutoff on the real tools: `yosys_fpga+nextpnr+fpga_pack` on an ECP5 and a Xilinx board.

An edit to the source that does not change the design re-runs synthesis and nothing after it:
a comment at the end of a line leaves the netlist byte-identical, and a comment line above the
logic moves every `src` attribute of the netlist, which `nextpnr` does not read. An edit to the
logic runs all three.

The ECP5 case uses the OSS CAD Suite's tools and skips without them (`XEDA_TESTS_REQUIRE_TOOLS=1`
makes that a failure). The Xilinx case is opt-in, as in `test_openxc7_real.py`
(`XEDA_TESTS_OPENXC7=1`, openXC7's `bin` first on `PATH`), and shares the run root, and so
the chip database, of `test_fpga_chains.py`.
"""

import os
import shutil
from pathlib import Path
from typing import NamedTuple

import pytest

from xeda.flow_runner import DefaultRunner

from . import tool_utils
from .tool_utils import UNRECORDED_BEFORE_RUN_REASON

RESOURCES = Path(__file__).parent / "resources" / "chains"
REQUEST = "yosys_fpga+nextpnr+fpga_pack"
STAGES = ("yosys_fpga", "nextpnr", "fpga_pack")


class Board(NamedTuple):
    file: str
    source: str
    #: a line of the source and its replacement, an edit of the logic
    logic: tuple[str, str]
    #: the line that gets a comment at its end
    commented: str


BOARDS = {
    "ulx3s": Board(
        "ulx3s.yaml",
        "ulx3s_blinky.v",
        ("led <= 1 << shift_val;", "led <= 2 << shift_val;"),
        "    localparam ctr_width = 32;",
    ),
    "arty": Board(
        "arty.yaml",
        "arty_blinky.v",
        ("assign led = r_count[24];", "assign led = r_count[23];"),
        "    reg [24:0] r_count = 0;",
    ),
}


class Build:
    """The chain, launched as `xeda run` launches it, from one run root."""

    def __init__(self, board: Board, root: Path, run_root: Path) -> None:
        self.board = board
        self.design = root / board.file
        self.source = root / board.source
        self.run_root = run_root

    def launch(self):
        self.runner = DefaultRunner(self.run_root, display_results=False)
        flow = self.runner.run(REQUEST, self.design)
        assert flow is not None and flow.succeeded
        return flow

    def ran(self) -> tuple[str, ...]:
        """The stages of the last launch that ran, in order."""
        launched = {f.name: f for f in self.runner.launched}
        return tuple(name for name in STAGES if not launched[name].reused)

    def stage(self, name: str):
        return next(f for f in self.runner.launched if f.name == name)


@pytest.fixture(params=sorted(BOARDS))
def build(request, tmp_path, tmp_path_factory, monkeypatch) -> Build:
    board = BOARDS[request.param]
    if request.param == "ulx3s":
        tool_utils.require_nextpnr_ecp5()
        tool_utils._require_command("ecppack", ["ecppack", "--help"])
        run_root = tmp_path / "xeda_run"
    else:
        tool_utils.require_openxc7()
        base = tmp_path_factory.getbasetemp()
        common = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
        run_root = common / "openxc7-run-root"  # the one chip database of the Arty's fabric
    shutil.copytree(RESOURCES, tmp_path / "design", ignore=shutil.ignore_patterns("__pycache__"))
    # a design of its own name, so that this run root holds a run directory for it alone
    design = tmp_path / "design" / board.file
    design.write_text(
        design.read_text().replace("name: chain_", f"name: cutoff_{tmp_path.name[-24:]}_", 1)
    )
    monkeypatch.chdir(tmp_path)
    build = Build(board, tmp_path / "design", run_root)
    build.launch()
    assert build.ran() == STAGES
    # a tool that reports a file of another file system is judged once more (the documented
    # extra run): settle the whole graph, and say why any later run ran
    for _ in range(2):
        build.launch()
        if build.ran() == ():
            break
        reasons = [f.stale_reason for f in build.runner.launched if not f.reused]
        assert all(r and r.startswith(UNRECORDED_BEFORE_RUN_REASON) for r in reasons), reasons
    assert build.ran() == ()
    return build


def test_a_comment_at_the_end_of_a_line_runs_synthesis_and_nothing_after_it(build):
    run_id = build.stage("nextpnr").run_id
    text = build.source.read_text()
    assert build.board.commented in text
    build.source.write_text(
        text.replace(build.board.commented, build.board.commented + "  // c", 1)
    )
    build.launch()
    assert build.ran() == ("yosys_fpga",), build.ran()
    assert build.stage("nextpnr").run_id == run_id


def test_a_comment_line_above_the_logic_runs_synthesis_and_nothing_after_it(build, caplog):
    caplog.set_level("INFO")
    build.source.write_text("// a line above the logic\n" + build.source.read_text())
    build.launch()
    assert build.ran() == ("yosys_fpga",), build.ran()
    assert "differs from the one it consumed only in what its type ignores" in caplog.text


def test_an_edit_of_the_logic_runs_all_three(build):
    old, new = build.board.logic
    text = build.source.read_text()
    assert old in text
    build.source.write_text(text.replace(old, new))
    build.launch()
    assert build.ran() == STAGES, build.ran()


def test_the_dry_run_agrees_with_the_run_on_the_real_tools(build):
    """Programs, a depfile of yosys's own files and the declared outputs of three real flows:
    the dry run says fresh, then, after an edit, which flow runs and why; the run agrees."""
    runner = build.runner.__class__(build.run_root, display_results=False)
    plan, states = runner.plan_with_freshness(REQUEST, build.design)
    assert {s.state for s in states.values()} == {"fresh"}, states
    old = "// a comment"
    build.source.write_text(build.source.read_text() + f"\n{old}\n")
    runner = build.runner.__class__(build.run_root, display_results=False)
    plan, states = runner.plan_with_freshness(REQUEST, build.design)
    assert states["yosys_fpga"].state == "stale"
    assert states["yosys_fpga"].reason.startswith("input changed: ")
    assert states["nextpnr"].state == states["fpga_pack"].state == "unknown"
    assert states["nextpnr"].reason == "depends on yosys_fpga"
    build.launch()
    assert build.ran() == ("yosys_fpga",)
    assert build.stage("yosys_fpga").stale_reason == states["yosys_fpga"].reason
