"""The netlist `yosys_fpga` writes for nextpnr holds only cells nextpnr can place.

nextpnr reads the netlist as JSON, flattens the modules that are not library cells, and imports
every other cell by its type name. Each architecture places only its own primitives. A generic
yosys cell (`$buf`, `$lut`, `$_AND_`, `$mem_v2`, ...) that synthesis leaves in the netlist stops
the build, with an error such as "no BELs remaining to implement cell type '$buf'".

The check reads the cell types of the netlist itself, for every family that xeda synthesizes for
nextpnr. It fails for any yosys change that leaves a generic cell behind, not only for the one
that led to it. Yosys pull request 6174 ("abc9: migrate to write_xaiger2") made `synth_xilinx` and
`synth_gowin` leave a `$buf` cell in a design with a counter that has unused bits. Pull request
6267 ("opt_clean: Remove $buf cells with 'z bits on their input") fixed that. A yosys build that
has the first change and not the second fails here.

Two tests check the check, with hand-made netlists. The others run the flow with the real yosys
(`require_yosys()`). None of them runs nextpnr.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from .tool_utils import require_yosys

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

#: The generic cells that nextpnr's JSON reader skips instead of importing. Its `import_leaf_cell`
#: (`frontend/frontend_base.h`) returns early for exactly these types:
#:     if (cell_type == "$scopeinfo" || cell_type == "$print" || cell_type == "$check")
#: https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L456
#: The nextpnr of openXC7 (`nextpnr-himbaechel` for Xilinx) has the same line. nextpnr imports
#: every other generic cell, and no architecture can place it.
NEXTPNR_SKIPPED_CELLS = frozenset({"$scopeinfo", "$print", "$check"})


def _flag(attributes: dict[str, Any], name: str) -> bool:
    """An integer attribute, set when it is not zero. Yosys writes it as a string of bits."""
    value = attributes.get(name, "0")
    return (int(value, 2) if isinstance(value, str) else value) != 0


def _is_box(module: dict[str, Any]) -> bool:
    """A library cell model (`ModuleInfo::is_box` in nextpnr). nextpnr never reads inside one."""
    attributes = module["attributes"]
    return _flag(attributes, "blackbox") or _flag(attributes, "whitebox")


def unplaceable_cells(netlist: dict[str, Any]) -> dict[str, list[str]]:
    """The generic cells that nextpnr would import from `netlist` and could not place.

    The result maps each such cell type to the `module/cell` names of its cells.

    The walk follows nextpnr's reader. It starts at the module marked `top`, goes into every cell
    that instantiates a module that is not a box (nextpnr flattens those), and takes each other
    cell as a leaf cell of its type. It does not read inside a box, and it does not read a module
    that the top module does not reach. The file holds `$` cells in those places that nextpnr
    never sees: every library model carries some, such as `$specify2`.
    """
    modules = netlist["modules"]
    tops = [name for name, module in modules.items() if _flag(module["attributes"], "top")]
    assert len(tops) == 1, f"nextpnr needs one module marked `top`, the netlist marks {tops}"
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    pending = tops
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for cell_name, cell in modules[name]["cells"].items():
            cell_type = cell["type"]
            if cell_type in modules and not _is_box(modules[cell_type]):
                pending.append(cell_type)
            elif cell_type.startswith("$") and cell_type not in NEXTPNR_SKIPPED_CELLS:
                found.setdefault(cell_type, []).append(f"{name}/{cell_name}")
    return found


def _module(cells: dict[str, str], *flags: str) -> dict[str, Any]:
    """A module of a yosys JSON netlist: `cells` maps names to cell types, and `flags` names the
    integer attributes (`top`, `blackbox`, `whitebox`) that it sets."""
    return {
        "attributes": dict.fromkeys(flags, "00000000000000000000000000000001"),
        "cells": {name: {"type": cell_type} for name, cell_type in cells.items()},
    }


def test_the_check_reports_what_nextpnr_would_import_and_cannot_place():
    netlist = {
        "modules": {
            "top": _module(
                {"u": "sub", "ff": "FDRE", "lut": "LUT4", "info": "$scopeinfo", "buf": "$buf"},
                "top",
            ),
            "sub": _module({"gate": "$_AND_", "msg": "$print", "assertion": "$check"}),
            # library models: nextpnr places a cell of these types, and never reads inside them
            "FDRE": _module({"buf": "$buf", "timing": "$specify2"}, "blackbox"),
            "LUT4": _module({"buf": "$buf"}, "whitebox"),
            # no module instantiates it, so nextpnr never reads it
            "unused": _module({"buf": "$buf"}),
        }
    }
    assert unplaceable_cells(netlist) == {"$buf": ["top/buf"], "$_AND_": ["sub/gate"]}


def test_the_check_accepts_exactly_the_generic_cells_nextpnr_skips():
    for cell_type in sorted(NEXTPNR_SKIPPED_CELLS):
        assert unplaceable_cells({"modules": {"top": _module({"c": cell_type}, "top")}}) == {}
    for cell_type in ("$buf", "$lut", "$_AND_", "$mem_v2", "$paramod\\FDRE\\INIT=1'0"):
        found = unplaceable_cells({"modules": {"top": _module({"c": cell_type}, "top")}})
        assert found == {cell_type: ["top/c"]}


#: One device for each family `yosys_fpga` synthesizes for a nextpnr architecture: Xilinx 7-series
#: (`nextpnr-himbaechel` of openXC7), the three Gowin series (`nextpnr-himbaechel`), ECP5, iCE40
#: and the two Nexus families.
TARGETS = {
    "xilinx-7": {"part": "xc7a100tcsg324-1"},
    "gowin-gw1n": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"},
    "gowin-gw2a": {"vendor": "gowin", "family": "gowin", "device": "GW2A-18"},
    "gowin-gw5a": {"vendor": "gowin", "family": "gowin", "device": "GW5A-25"},
    "ecp5": {"part": "LFE5U-25F-6BG256C"},
    "ice40-hx": {"part": "iCE40HX1K-TQ144"},
    "ice40-up": {"part": "iCE40UP5K-SG48I"},
    "nexus-crosslink": {"part": "LIFCL-40-9BG400C"},
    "nexus-certus": {"part": "LFD2NX-40-7BG256C"},
}

#: The reproducer of yosys pull request 6267: a counter whose output uses two bits.
COUNTER = """\
module t1(input clk, output [1:0] o);
    reg [7:0] c = 8'd0;
    always @(posedge clk) c <= c + 1'b1;
    assign o = c[1:0];
endmodule
"""


def _counter(tmp_path: Path) -> Design:
    root = tmp_path / "counter"
    root.mkdir()
    (root / "t1.v").write_text(COUNTER)
    return Design(
        name="t1",
        design_root=root,
        rtl={"sources": ["t1.v"], "top": "t1", "clock": {"port": "clk"}},
    )


def _ulx3s_blinky(_tmp_path: Path) -> Design:
    """The ULX3S example: a 32-bit counter whose middle bits are used. It has the shape of the
    reproducer, in a design that people build."""
    return Design(
        name="blinky",
        design_root=EXAMPLES_DIR / "boards" / "ulx3s" / "blinky",
        rtl={"sources": ["blinky.v"], "top": "blinky", "clock": {"port": "clk_25mhz"}},
    )


DESIGNS = {"counter": _counter, "ulx3s-blinky": _ulx3s_blinky}


@pytest.mark.parametrize("design", list(DESIGNS), ids=list(DESIGNS))
@pytest.mark.parametrize("fpga", list(TARGETS.values()), ids=list(TARGETS))
def test_the_netlist_holds_only_cells_nextpnr_can_place(fpga, design, tmp_path):
    require_yosys()
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        YosysFpga, DESIGNS[design](tmp_path), {"fpga": fpga, "clock": {"period": 10.0}}
    )
    assert flow is not None and flow.succeeded, "yosys_fpga did not synthesize the design"
    netlist = json.loads((Path(flow.run_path) / "netlist.json").read_text())
    found = unplaceable_cells(netlist)
    assert not found, (
        f"{netlist['creator']} left generic cells in the netlist that nextpnr cannot place: "
        + ", ".join(f"{len(c)} x {t} (first: {c[0]})" for t, c in found.items())
    )
