"""The openXC7 1.0 toolchain for real: yosys, nextpnr-himbaechel and fpga-as, on an Arty A7-100T.

Opt-in (`XEDA_TESTS_OPENXC7=1`), since the first build generates the xc7a100t chip database:
about a minute and 3.5 GB of memory. Once opted in, a missing or broken toolchain is a failure,
not a skip. Run it with openXC7's `bin` directory first on `PATH`, from the project's own
interpreter (openXC7's `export.sh` also puts its virtual environment's `pytest` first):

    PATH=/opt/openxc7/bin:$PATH XEDA_TESTS_OPENXC7=1 python -m pytest tests/test_openxc7_real.py

Every worker of an xdist run shares one run root, under the directory their temporary
directories have in common, so the database is generated once, under the cache's own lock.
Nothing here programs a board: `openFPGALoader` is neither probed nor run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import FpgaPack

from . import tool_utils

PART = "xc7a100tcsg324-1"

# openXC7 demo-projects' blinky-digilent-arty, verbatim (its Makefile names the A7-35T part)
BLINKY = """\
`default_nettype none   //do not allow undeclared wires

module blinky (
    input  wire clk,
    output wire led
    );

    reg [24:0] r_count = 0;

    always @(posedge(clk)) r_count <= r_count + 1;

    assign led = r_count[24];
endmodule
"""

PINS = """\
set_property LOC E3 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports {clk}]

set_property LOC H5 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports {led}]
"""

# instantiated primitives: both outputs of a LUT, a distributed RAM and a shift register
PRIMS = """\
`default_nettype none
module prims (
    input  wire clk,
    output wire led
);
    reg [3:0] count = 0;
    always @(posedge clk) count <= count + 1;
    wire o5, o6, ram_out, srl_out;
    LUT6_2 #(.INIT(64'h6996_9669_8000_0001)) lut (
        .I0(count[0]), .I1(count[1]), .I2(count[2]), .I3(count[3]), .I4(1'b0), .I5(1'b1),
        .O5(o5), .O6(o6)
    );
    RAM32X1D ram (
        .WCLK(clk), .WE(o5), .D(o6),
        .A0(count[0]), .A1(count[1]), .A2(count[2]), .A3(count[3]), .A4(1'b0),
        .DPRA0(count[3]), .DPRA1(count[2]), .DPRA2(count[1]), .DPRA3(count[0]), .DPRA4(1'b0),
        .SPO(), .DPO(ram_out)
    );
    SRL16E srl (
        .CLK(clk), .CE(1'b1), .D(ram_out),
        .A0(1'b1), .A1(1'b0), .A2(1'b1), .A3(1'b0), .Q(srl_out)
    );
    assign led = srl_out;
endmodule
"""


def _tree_state(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under `root`: its type and mode, and a file's size, modification time and
    content. What a child process did to the tree shows here; an audit hook sees no child."""
    state: dict[str, tuple[Any, ...]] = {}
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            path = Path(directory, name)
            status = path.lstat()
            entry: tuple[Any, ...] = (status.st_mode,)
            if path.is_symlink():
                entry += (os.readlink(path),)
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                entry += (status.st_size, status.st_mtime_ns, digest)
            state[str(path.relative_to(root))] = entry
    return state


@pytest.fixture(scope="module")
def toolchain() -> Path:
    """The openXC7 installation prefix `PATH` selects, checked the way the flows use it."""
    return tool_utils.require_openxc7()


@pytest.fixture(scope="module")
def run_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.getbasetemp()
    # an xdist worker's base is a directory of the session's: all workers share its parent
    common = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    return common / "openxc7-run-root"


def _design(directory: Path, top: str, verilog: str) -> Design:
    directory.mkdir()
    (directory / f"{top}.v").write_text(verilog)
    (directory / f"{top}.xdc").write_text(PINS)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return Design(
        name=f"{top}_{worker}",  # workers share the run root, never a run directory
        design_root=directory,
        rtl={"sources": [f"{top}.v", f"{top}.xdc"], "top": top},
        # the upstream Makefile's `synth_xilinx -flatten -abc9 -arch xc7`
        flow={"yosys_fpga": {"fpga": {"part": PART}, "flatten": True}},
    )


@pytest.fixture(scope="module")
def built(toolchain: Path, run_root: Path, tmp_path_factory: pytest.TempPathFactory):
    """The first build of the blinky, with the installation's state before and after it."""
    design = _design(tmp_path_factory.mktemp("openxc7") / "blinky", "blinky", BLINKY)
    generators = toolchain / "share/nextpnr/himbaechel"
    before = _tree_state(generators)
    runner = DefaultRunner(run_root, display_results=False)
    flow = runner.run(FpgaPack, design)
    assert flow is not None and flow.succeeded
    return runner, design, flow, before, _tree_state(generators)


def _results(run_root: Path, design: Design, flow: str) -> dict[str, Any]:
    return json.loads((run_root / design.name / flow / "results.json").read_text())


def _chipdbs(run_root: Path) -> list[Path]:
    return sorted((run_root / ".cache/xilinx-chipdb").glob("*/*.bin"))


def test_the_arty_blinky_builds_to_a_bitstream(built, run_root):
    _runner, design, flow, _before, _after = built
    assert flow.outputs.bitstream == flow.run_path / f"outputs/{design.name}.bit"
    assert flow.outputs.bitstream.stat().st_size > 3_000_000
    assert flow.inputs.config == run_root / design.name / "nextpnr/config.fasm"
    assert flow.inputs.config.stat().st_size > 1000
    routed = _results(run_root, design, "nextpnr")
    assert routed["success"] and routed["timing_met"] is True and routed["Fmax"] > 100
    assert routed["device"] == PART and routed["fabric"] == "xc7a100t"
    assert (routed["ff"], routed["bram"], routed["dsp"], routed["io"]) == (25, 0, 0, 2)
    # a 25-bit counter: carry chains with their LUTs, counted as occupied LUT locations
    assert 25 <= routed["lut"] <= routed["SLICE_LUTX"]
    assert routed["LUT:STAGE"] == "placed and routed"
    mapped = _results(run_root, design, "yosys_fpga")
    assert mapped["LUT:STAGE"] == "mapped" and mapped["ff"] == 25
    assert len(_chipdbs(run_root)) == 1


def _configuration(fasm: Path) -> tuple[list[str], list[str]]:
    """A FASM file's features, in order, and its comments with yosys's own numbering of
    generated names (`$abc$2027$...`) taken out: xeda reads the family's cell libraries before
    the design, so the same names carry other numbers."""
    lines = fasm.read_text().splitlines()
    features = [line for line in lines if not line.startswith("#")]
    comments = [re.sub(r"\$\d+", "$N", line) for line in lines if line.startswith("#")]
    return features, comments


def test_the_configuration_is_what_the_upstream_makefile_builds(
    built, toolchain, run_root, tmp_path
):
    """openXC7 demo-projects' `openXC7.mk`, command for command, with the same yosys and
    nextpnr, chip database and (default) seed: the same FASM features in the same order, and the
    same bitstream but for the time in its header."""
    _runner, design, flow, _before, _after = built
    (tmp_path / "blinky.v").write_text(BLINKY)
    (tmp_path / "blinky.xdc").write_text(PINS)
    chipdb = _chipdbs(run_root)[0]
    database = toolchain / "share/nextpnr/prjxray-db/artix7"
    for command in (
        [
            "yosys",
            "-q",
            "-p",
            "synth_xilinx -flatten -abc9 -arch xc7 -top blinky; write_json blinky.json",
            "blinky.v",
        ],
        # the `nextpnr-xilinx` shim of openXC7 1.0, which names the device without its speed
        ["nextpnr-himbaechel", "--json", "blinky.json", "--chipdb", str(chipdb)]
        + ["--device", PART.rsplit("-", 1)[0], "-o", "xdc=blinky.xdc", "-o", "fasm=blinky.fasm"],
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=600)
    with open(tmp_path / "blinky.bit", "wb") as bitstream:
        subprocess.run(
            ["fpga-as", f"--prjxray_db_path={database}", "--part", PART, "blinky.fasm"],
            cwd=tmp_path,
            check=True,
            stdout=bitstream,
            timeout=600,
        )
    upstream = _configuration(tmp_path / "blinky.fasm")
    ours = _configuration(flow.inputs.config)
    assert len(upstream[0]) > 500
    assert ours[0] == upstream[0]
    assert ours[1] == upstream[1]
    theirs = (tmp_path / "blinky.bit").read_bytes()
    packed = flow.outputs.bitstream.read_bytes()
    assert len(packed) == len(theirs)
    # the header: design name, part, date and time fields, then the configuration
    assert packed[128:] == theirs[128:]


def test_an_unchanged_second_launch_runs_nothing(built):
    runner, design, _flow, _before, _after = built
    first = len(runner.launched)
    again = tool_utils.launch_until_fresh(runner, lambda: runner.run(FpgaPack, design))
    assert again.reused
    # the launch found fresh: its producers too, with no tool started
    assert [flow.reused for flow in runner.launched[first:]][-3:] == [True, True, True]


def test_a_second_design_reuses_the_chip_database(built, run_root, tmp_path):
    _runner, _design_, _flow, _before, _after = built
    (chipdb,) = _chipdbs(run_root)
    state = (chipdb.stat().st_ino, chipdb.stat().st_mtime_ns)
    design = _design(tmp_path / "prims", "prims", PRIMS)
    flow = DefaultRunner(run_root, display_results=False).run(FpgaPack, design)
    assert flow is not None and flow.succeeded
    assert flow.outputs.bitstream.stat().st_size > 3_000_000
    assert _chipdbs(run_root) == [chipdb]
    assert (chipdb.stat().st_ino, chipdb.stat().st_mtime_ns) == state
    trace = json.loads((run_root / design.name / "nextpnr/trace.json").read_text())
    assert "bbasm" not in json.dumps(trace["programs"])
    routed = _results(run_root, design, "nextpnr")
    assert routed["timing_met"] is True and (routed["bram"], routed["dsp"]) == (0, 0)
    assert routed["ff"] == 4 and 4 <= routed["lut"] <= routed["SLICE_LUTX"]
    mapped = _results(run_root, design, "yosys_fpga")
    # The mapped netlist holds the three primitives the design instantiates and one cell it does
    # not: the INV that abc9 makes of the counter's bit-0 complement (`$abc$...$lut$not$...`,
    # the first sum input of the CARRY4). An INV is a LUT1, and nextpnr places it in a LUT of its
    # own (B6LUT of SLICE_X89Y103 in the run that set this pin). So the footprint is LUT6_2 two
    # (logic), the dual-port RAM32X1D two (ram), the shift register one (srl), and that INV one
    # more logic LUT: (3, 2, 1). `xilinx_lut_footprint` counted no INV before "Count an
    # instantiated INV as the LUT1 it is", which made this (2, 2, 1), an undercount. The pin moves
    # again only if abc9 stops (or starts) materializing an inversion for this design.
    cells = mapped["_utilization"]
    assert (cells["INV"], cells["LUT6_2"], cells["RAM32X1D"], cells["SRL16E"]) == (1, 1, 1, 1)
    assert (mapped["LUT:LOGIC"], mapped["LUT:RAM"], mapped["LUT:SRL"]) == (3, 2, 1)
    assert mapped["lut"] == 6


def test_the_build_leaves_the_generator_tree_as_it_was(built):
    """No bytecode, no scratch file: the chip database generator and nextpnr's Python hooks
    run from the installation without writing to it."""
    _runner, _design_, _flow, before, after = built
    assert len(before) > 20
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    assert not changed


def test_the_tree_snapshot_sees_what_a_child_process_writes(tmp_path):
    """The teeth of the test above: a file a child process adds, and one it rewrites in place
    keeping its size, both show."""
    tree = tmp_path / "tree"
    (tree / "gen").mkdir(parents=True)
    (tree / "gen/tool.py").write_text("x = 1\n")
    before = _tree_state(tree)
    script = (
        "import pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "(root / 'gen/__pycache__').mkdir()\n"
        "(root / 'gen/__pycache__/tool.pyc').write_bytes(b'bytecode')\n"
        "(root / 'gen/tool.py').write_text('x = 2\\n')\n"
    )
    subprocess.run([sys.executable, "-c", script, str(tree)], check=True, timeout=60)
    after = _tree_state(tree)
    assert {name for name in after if before.get(name) != after[name]} == {
        "gen/__pycache__",
        "gen/__pycache__/tool.pyc",
        "gen/tool.py",
    }
