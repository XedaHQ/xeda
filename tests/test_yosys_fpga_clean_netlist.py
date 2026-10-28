"""The netlists of `yosys_fpga` hold what synthesis made, and nothing that an analysis or a
check-only cell adds.

Two rules, both checked with the real yosys (`require_yosys()`):

- The timing analysis (`sta: true`) reads the design and changes nothing that the flow writes.
  The yosys `sta` pass derives every parametrized cell type it meets. A derived library cell has
  processes, so `write_json` refuses it, and `write_edif` writes it as a cell definition that no
  instance uses.
- The netlist holds no formal cell (`$check`, `$assert`, `$assume`, `$cover`) and no `$print`
  cell. An immediate `assert` or a `$display` in an `always_ff` block makes them. No FPGA tool
  reads them: Vivado does not read them in the EDIF netlist, and nextpnr skips them.
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from .tool_utils import require_yosys

#: A shift register (`SRL16E`), a distributed RAM (`RAM64X1D`) and an adder with a carry chain.
#: The shift register and the RAM are parametrized library cells.
MAPPED_CELLS = """\
module top (
  input logic clock, input logic we,
  input logic [5:0] wa, ra, input logic [7:0] wd, input logic [7:0] a, b,
  output logic [8:0] y, output logic [7:0] rd
);
  logic [8:0] r;
  logic [7:0] mem [0:63];
  logic [15:0] sh;
  always_ff @(posedge clock) begin
    r <= a + ~b;
    if (we) mem[wa] <= wd;
    rd <= mem[ra];
    sh <= {sh[14:0], a[0]};
  end
  assign y = r ^ sh[15];
endmodule
"""

#: A register with an immediate assertion and a `$display` in the clocked block.
CHECKED = """\
module top (input logic clock, input logic [7:0] a, input logic [7:0] b, output logic [8:0] y);
  always_ff @(posedge clock) begin
    assert (a != 8'hff);
    $display("sum %d", a + b);
    y <= a + b;
  end
endmodule
"""

XC7 = {"part": "xc7a100tfgg676-3"}
ECP5 = {"part": "LFE5U-25F-6BG256C"}

#: The cell types of yosys that only check or print.
CHECK_ONLY = ("$check", "$print", "$assert", "$assume", "$cover")


def _synthesize(tmp_path: Path, source: str, fpga: dict[str, str], **settings: Any):
    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.sv").write_text(source)
    design = Design(
        name="top",
        root=root,
        rtl={"sources": ["top.sv"], "top": "top", "clocks": [{"port": "clock"}]},
    )
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        YosysFpga,
        design,
        {"fpga": fpga, "clocks": {"main_clock": {"period": "10 ns"}}, **settings},
    )
    assert flow is not None and flow.succeeded, "yosys_fpga did not synthesize the design"
    return flow


def _netlist(flow) -> dict[str, Any]:
    return json.loads((Path(flow.run_path) / "netlist.json").read_text())


def _cells_of(netlist: dict[str, Any]) -> dict[str, Counter[str]]:
    """The module names of a JSON netlist, each with the types of its cells."""
    return {
        name: Counter(cell["type"] for cell in module["cells"].values())
        for name, module in netlist["modules"].items()
    }


def _edif_cell_definitions(flow) -> Counter[str]:
    """The names of the cells that an EDIF netlist defines."""
    text = (Path(flow.run_path) / "netlist.edif").read_text()
    return Counter(re.findall(r"^\s*\(cell\s+(\S+)", text, flags=re.MULTILINE))


def test_the_timing_analysis_changes_no_netlist(tmp_path: Path) -> None:
    require_yosys()
    plain = _synthesize(tmp_path / "plain", MAPPED_CELLS, XC7, sta=False)
    analyzed = _synthesize(tmp_path / "analyzed", MAPPED_CELLS, XC7, sta=True)
    assert (Path(analyzed.run_path) / "reports" / "timing.rpt").is_file()
    plain_cells = _cells_of(_netlist(plain))
    assert any(
        "SRL16E" in t or "RAM64X1D" in t for c in plain_cells.values() for t in c
    ), "the design should map a shift register or a distributed RAM"
    assert _cells_of(_netlist(analyzed)) == plain_cells
    plain_edif = _edif_cell_definitions(plain)
    assert plain_edif
    assert _edif_cell_definitions(analyzed) == plain_edif
    assert (Path(analyzed.run_path) / "netlist.v").read_text() == (
        Path(plain.run_path) / "netlist.v"
    ).read_text()


def test_the_longest_path_report_changes_no_netlist(tmp_path: Path) -> None:
    require_yosys()
    plain = _synthesize(tmp_path / "plain", MAPPED_CELLS, XC7)
    analyzed = _synthesize(tmp_path / "analyzed", MAPPED_CELLS, XC7, ltp=True)
    assert (Path(analyzed.run_path) / "ltp.out").is_file()
    assert _cells_of(_netlist(analyzed)) == _cells_of(_netlist(plain))
    assert _edif_cell_definitions(analyzed) == _edif_cell_definitions(plain)


def _check_only_in(netlist: dict[str, Any]) -> dict[str, list[str]]:
    return {
        module: [t for t in cells if t in CHECK_ONLY]
        for module, cells in _cells_of(netlist).items()
        if any(t in CHECK_ONLY for t in cells)
    }


@pytest.mark.parametrize("fpga", [XC7, ECP5], ids=["xilinx-7", "ecp5"])
def test_no_check_or_print_cell_reaches_the_netlist(fpga, tmp_path: Path) -> None:
    require_yosys()
    flow = _synthesize(tmp_path, CHECKED, fpga)
    assert _check_only_in(_netlist(flow)) == {}
    types = flow.results["cell_types"]
    assert not [t for t in types if t in CHECK_ONLY], f"the utilization counts {types}"


def test_no_check_or_print_text_reaches_the_xilinx_netlists(tmp_path: Path) -> None:
    require_yosys()
    flow = _synthesize(tmp_path, CHECKED, XC7)
    edif = (Path(flow.run_path) / "netlist.edif").read_text()
    verilog = (Path(flow.run_path) / "netlist.v").read_text()
    for name, text in (("netlist.edif", edif), ("netlist.v", verilog)):
        for cell_type in CHECK_ONLY:
            assert cell_type not in text, f"{name} holds a {cell_type} cell"
        assert "assert (" not in text, f"{name} holds an assertion"
        assert "$display" not in text, f"{name} holds a $display"


def test_the_formal_cells_stay_in_the_pass_only_recipe(tmp_path: Path) -> None:
    """`synth_pass_only` runs the synthesis pass of the family and adds no step, so the netlist
    is what `yosys -p synth_xilinx` writes."""
    require_yosys()
    flow = _synthesize(
        tmp_path,
        CHECKED,
        XC7,
        synth_pass_only=True,
        read_verilog_flags=[],
        systemverilog="default",
    )
    assert "$check" in {t for c in _cells_of(_netlist(flow)).values() for t in c}
