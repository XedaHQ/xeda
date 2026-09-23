"""A flow must not change the design it runs on.

Every flow of a run -- the flow itself and each of its dependencies -- shares one `Design`,
whose hash is taken before any of them starts. A flow that edits it (clearing parameters,
merging testbench defines into the RTL's, recording a discovered top unit, shortening a VHDL
standard to how its tool spells it) leaves `settings.json` describing a design that does not
match the run's `design_hash`, and hands every flow after it a different design than the file
describes. Each case runs a real flow down the code paths that used to do that.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, GhdlSim, GhdlSynth, Verilator, Yosys

from .tool_utils import require_ghdl, require_verilator, require_yosys_ghdl_plugin

VHDL_INV = (
    "library ieee; use ieee.std_logic_1164.all;\n"
    "entity inv is generic(W: positive := 2);\n"
    "  port(a: in std_logic_vector(W-1 downto 0); y: out std_logic_vector(W-1 downto 0));\n"
    "end;\n"
    "architecture rtl of inv is begin y <= not a; end;\n"
)
VHDL_TB = (
    "library ieee; use ieee.std_logic_1164.all;\n"
    "entity tb is generic(N: positive := 1); end;\n"
    "architecture sim of tb is\n"
    "  signal a, y: std_logic_vector(3 downto 0) := (others => '0');\n"
    "begin\n"
    "  uut: entity work.inv generic map(W => 4) port map(a, y);\n"
    '  process begin wait for 1 ns; assert y = x"F" severity failure; wait; end process;\n'
    "end;\n"
)
VERILOG_TOP = "module top(input [3:0] a, output [3:0] y); assign y = ~a + `INC; endmodule\n"
VERILOG_TB = (
    "module tb;\n"
    "  reg [3:0] a = 0; wire [3:0] y;\n"
    "  top uut(.a(a), .y(y));\n"
    '  initial begin #1; if (y !== 4\'hF + `INC) $stop; $display("TB_DEFINE=%0d", `TB_DEFINE);'
    " $finish; end\n"
    "endmodule\n"
)
BSV_TOP = (
    "package Top;\n"
    "interface Top_IFC; method Bit#(4) out; endinterface\n"
    "(* synthesize *)\n"
    "module mkTop(Top_IFC);\n"
    "  Reg#(Bit#(4)) r <- mkReg(0);\n"
    "  rule inc; r <= r + 1; endrule\n"
    "  method out = r;\n"
    "endmodule\n"
    "endpackage\n"
)


def _require_bsc() -> None:
    # bsc is not part of the tool set CI provides, so it is optional here even under
    # XEDA_TESTS_REQUIRE_TOOLS.
    if not shutil.which("bsc"):
        pytest.skip("bsc is not installed")


def _files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).write_text(text)


# flow, tool guard, sources, design fields (besides name/root), flow settings
CASES: dict[str, Any] = {
    # `common_flags` shortened the design's VHDL standard to GHDL's spelling ("2008" -> "08")
    "ghdl_synth": (
        GhdlSynth,
        require_ghdl,
        {"inv.vhd": VHDL_INV},
        {
            "rtl": {"sources": ["inv.vhd"], "top": "inv", "parameters": {"W": 3}},
            "language": {"vhdl": "2008"},
        },
        {"verilog_output": "inv.v"},
    ),
    # `run` recorded the top unit `find-top` discovered as the design's `tb.top`
    "ghdl_sim": (
        GhdlSim,
        require_ghdl,
        {"inv.vhd": VHDL_INV, "tb.vhd": VHDL_TB},
        {
            "rtl": {"sources": ["inv.vhd"], "top": "inv"},
            "tb": {"sources": ["tb.vhd"], "parameters": {"N": 2}},
            "language": {"vhdl": "2008"},
        },
        {},
    ),
    # `init` emptied `rtl.parameters` of a VHDL top
    "yosys": (
        Yosys,
        require_yosys_ghdl_plugin,
        {"inv.vhd": VHDL_INV},
        {"rtl": {"sources": ["inv.vhd"], "top": "inv", "parameters": {"W": 3}}},
        {},
    ),
    # `run` merged the testbench's defines into the design's RTL defines
    "verilator": (
        Verilator,
        require_verilator,
        {"top.v": VERILOG_TOP, "tb.v": VERILOG_TB},
        {
            "rtl": {"sources": ["top.v"], "top": "top", "defines": {"INC": 0}},
            "tb": {"sources": ["tb.v"], "top": "tb", "defines": {"TB_DEFINE": 7}},
        },
        {},
    ),
    # `run` added BSV_POSITIVE_RESET to the design's own `rtl.parameters`
    "bsc": (
        Bsc,
        _require_bsc,
        {"Top.bsv": BSV_TOP},
        {"rtl": {"sources": ["Top.bsv"], "top": "mkTop", "parameters": {"DEPTH": 4}}},
        {"positive_reset": True},
    ),
}


def _snapshot(design: Design) -> Any:
    return (
        design.model_dump(mode="json"),
        design.rtl_fingerprint,
        design.tb_fingerprint,
        design.rtl_hash,
        design.tb_hash,
    )


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_flow_leaves_the_design_it_runs_on_unchanged(case: str, tmp_path: Path) -> None:
    flow_class, require_tool, files, fields, settings = CASES[case]
    guard: Callable[[], None] = require_tool
    guard()
    _files(tmp_path, files)
    design = Design(name=case, design_root=tmp_path, **fields)
    before = _snapshot(design)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(flow_class, design, settings)
    assert flow is not None and flow.succeeded
    assert _snapshot(design) == before
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["rtl_hash"] == design.rtl_hash
    assert recorded["rtl_fingerprint"] == json.loads(json.dumps(design.rtl_fingerprint))
