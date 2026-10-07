"""The netlist `yosys_fpga` writes for nextpnr holds only cells nextpnr can place.

nextpnr reads the netlist as JSON, flattens the modules that are not library cells, and imports
every other cell by its type name. Each architecture places only its own primitives. A generic
yosys cell (`$buf`, `$lut`, `$_AND_`, `$mem_v2`, ...) that synthesis leaves in the netlist stops
the build, with an error such as "no BELs remaining to implement cell type '$buf'". There are two
exceptions, `NEXTPNR_SKIPPED_CELLS` and `PAD_TBUF_TARGETS`.

The check reads the cell types of the netlist itself, for every family that xeda synthesizes for
nextpnr. It fails for any yosys change that leaves a generic cell behind, not only for the one
that led to it. Yosys pull request 6174 ("abc9: migrate to write_xaiger2") made `synth_xilinx` and
`synth_gowin` leave a `$buf` cell in a design with a counter that has unused bits. Pull request
6267 ("opt_clean: Remove $buf cells with 'z bits on their input") fixed that. A yosys build that
has the first change and not the second fails here.

Some tests check the check, with hand-made netlists. The others run the flow with the real yosys
(`require_yosys()`). None of them runs nextpnr.
"""

import json
from collections.abc import Sequence
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
#: every other generic cell as a leaf cell. An architecture places it only when it has a rule for
#: that type, as iCE40 and ECP5 have for a `$_TBUF_` (see `PAD_TBUF_TARGETS`).
NEXTPNR_SKIPPED_CELLS = frozenset({"$scopeinfo", "$print", "$check"})

#: The synthesis targets (`YosysFpga.Settings.synthesis_target()`) whose nextpnr architecture
#: merges a `$_TBUF_` into the IO cell of the pad that it drives, an output or inout port. For the
#: net `donet` of the IO buffer of each such port, the iCE40 and ECP5 packers call
#: `net_driven_by(ctx, donet, <cell->type == ctx->id("$_TBUF_")>, id_Y)` and fold the `$_TBUF_`
#: that they find into the IO cell:
#: https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/ice40/cells.cc#L468
#: https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/ecp5/cells.cc#L358
#: A `$_TBUF_` that drives any other net stays a cell that no architecture can place ("cell type
#: '$_TBUF_' is unsupported"). The Nexus code and the himbaechel code (Gowin, and Xilinx in the
#: nextpnr of openXC7) have no rule for a `$_TBUF_`. The check does not judge the readers of the
#: pad net: on an output pad whose net also feeds other cells, nextpnr stops with "unsupported
#: tristate IO pattern".
PAD_TBUF_TARGETS = frozenset({"ecp5", "ice40"})


def _flag(attributes: dict[str, Any], name: str) -> bool:
    """An integer attribute, set when it is not zero. Yosys writes it as a string of bits."""
    value = attributes.get(name, "0")
    return (int(value, 2) if isinstance(value, str) else value) != 0


def _is_box(module: dict[str, Any]) -> bool:
    """A library cell model (`ModuleInfo::is_box` in nextpnr). nextpnr never reads inside one."""
    attributes = module["attributes"]
    return _flag(attributes, "blackbox") or _flag(attributes, "whitebox")


def _top_module(modules: dict[str, Any]) -> str:
    """The module that nextpnr starts from, chosen as `find_top_module` chooses it.

    It is the one module that is marked `top` and is not a box. When no module is marked, it is
    the one module that is not a box and that no module instantiates. nextpnr stops when it finds
    several marked modules, or when it does not find exactly one candidate. So does this function.
    https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L189-L219
    """
    marked = [n for n, m in modules.items() if _flag(m["attributes"], "top") and not _is_box(m)]
    assert len(marked) <= 1, f"nextpnr stops at several modules marked `top`: {marked}"
    if marked:
        return marked[0]
    candidates = {name for name, module in modules.items() if not _is_box(module)}
    for module in modules.values():
        candidates -= {cell["type"] for cell in module["cells"].values()}
    assert len(candidates) == 1, f"nextpnr cannot tell the top module among {sorted(candidates)}"
    return candidates.pop()


def unplaceable_cells(netlist: dict[str, Any], target: str) -> dict[str, list[str]]:
    """The generic cells that nextpnr would import from `netlist` and could not place, for the
    synthesis `target` (`YosysFpga.Settings.synthesis_target()`).

    The result maps each such cell type to the `module/cell` names of its cells.

    The walk follows nextpnr's reader. It starts at the top module, goes into every cell that
    instantiates a module that is not a box (nextpnr flattens those), and takes each other cell as
    a leaf cell of its type. It does not read inside a box, and it does not read a module that the
    top module does not reach. The file holds `$` cells in those places that nextpnr never sees:
    every library model carries some, such as `$specify2`.

    A leaf cell is no concern when its type is in `NEXTPNR_SKIPPED_CELLS`. A `$_TBUF_` of the top
    module is no concern either, on a target in `PAD_TBUF_TARGETS`, when its output is a bit of an
    output or inout port of the top module. Net numbers belong to one module, so a `$_TBUF_` of
    any other module does not count as driving a pad.
    """
    modules = netlist["modules"]
    top = _top_module(modules)
    pads = {
        bit
        for port in modules[top]["ports"].values()
        if port["direction"] in ("output", "inout")
        for bit in port["bits"]
        if isinstance(bit, int)
    }
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    pending = [top]
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
                if (
                    cell_type == "$_TBUF_"
                    and target in PAD_TBUF_TARGETS
                    and name == top
                    and cell["connections"]["Y"][0] in pads
                ):
                    continue
                found.setdefault(cell_type, []).append(f"{name}/{cell_name}")
    return found


def _module(
    cells: dict[str, Any],
    *flags: str,
    ports: Sequence[tuple[str, str, list[int]]] = (),
) -> dict[str, Any]:
    """A module of a yosys JSON netlist.

    `cells` maps names to cell types, or to whole cells. `flags` names the integer attributes
    (`top`, `blackbox`, `whitebox`) that the module sets. `ports` lists the `(name, direction,
    nets)` of its ports.
    """
    return {
        "attributes": dict.fromkeys(flags, "00000000000000000000000000000001"),
        "ports": {name: {"direction": d, "bits": nets} for name, d, nets in ports},
        "cells": {
            name: cell if isinstance(cell, dict) else {"type": cell, "connections": {}}
            for name, cell in cells.items()
        },
    }


def _tbuf(output: int) -> dict[str, Any]:
    """A `$_TBUF_` cell, as yosys writes it, with its output on net `output`."""
    return {"type": "$_TBUF_", "connections": {"A": [2], "E": [3], "Y": [output]}}


def _pad(driven: int, direction: str = "inout") -> dict[str, Any]:
    """A netlist whose top module has a port on net 5, and a `$_TBUF_` that drives net `driven`."""
    top = _module({"drv": _tbuf(driven)}, "top", ports=[("pad", direction, [5])])
    return {"modules": {"top": top}}


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
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["top/buf"], "$_AND_": ["sub/gate"]}


@pytest.mark.parametrize("cell_type", sorted(NEXTPNR_SKIPPED_CELLS))
def test_the_check_accepts_the_generic_cells_nextpnr_skips(cell_type):
    netlist = {"modules": {"top": _module({"c": cell_type}, "top")}}
    assert unplaceable_cells(netlist, "ecp5") == {}


@pytest.mark.parametrize(
    "cell_type", ["$buf", "$lut", "$_AND_", "$mem_v2", "$dff", "$paramod\\FDRE\\INIT=1'0"]
)
def test_the_check_reports_every_other_generic_cell(cell_type):
    netlist = {"modules": {"top": _module({"c": cell_type}, "top")}}
    assert unplaceable_cells(netlist, "ecp5") == {cell_type: ["top/c"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
@pytest.mark.parametrize("direction", ["output", "inout"])
def test_a_tristate_buffer_that_drives_a_pad_is_merged_into_its_io_cell(target, direction):
    assert unplaceable_cells(_pad(5, direction), target) == {}


@pytest.mark.parametrize("target", ["xilinx", "gowin", "nexus"])
def test_a_target_without_the_merge_cannot_place_a_tristate_buffer_even_on_a_pad(target):
    assert unplaceable_cells(_pad(5), target) == {"$_TBUF_": ["top/drv"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
@pytest.mark.parametrize(
    "netlist, where",
    [
        pytest.param(_pad(7), "top/drv", id="internal-net"),
        pytest.param(_pad(5, "input"), "top/drv", id="input-port"),
        # net numbers belong to one module: net 5 of `sub` is not the pad of `top`
        pytest.param(
            {
                "modules": {
                    "top": _module({"u": "sub"}, "top", ports=[("pad", "inout", [5])]),
                    "sub": _module({"drv": _tbuf(5)}),
                }
            },
            "sub/drv",
            id="other-module",
        ),
    ],
)
def test_any_other_tristate_buffer_is_a_cell_that_no_architecture_can_place(target, netlist, where):
    assert unplaceable_cells(netlist, target) == {"$_TBUF_": [where]}


def test_the_top_module_is_the_one_marked_top():
    netlist = {
        "modules": {
            "top": _module({"buf": "$buf"}, "top"),
            # nothing instantiates it, but a marked module settles the choice
            "other": _module({"stray": "$buf"}),
            # a box is never the top module, whatever its attributes say
            "box": _module({}, "top", "blackbox"),
        }
    }
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["top/buf"]}


def test_without_a_marked_module_the_top_module_is_the_one_nothing_instantiates():
    netlist = {
        "modules": {
            "top": _module({"u": "sub"}),
            "sub": _module({"buf": "$buf"}),
            "FDRE": _module({}, "blackbox"),
        }
    }
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["sub/buf"]}


@pytest.mark.parametrize(
    "modules, problem",
    [
        pytest.param(
            {"a": _module({}, "top"), "b": _module({}, "top")}, "several modules", id="two-marked"
        ),
        pytest.param({"a": _module({}), "b": _module({})}, "cannot tell", id="two-candidates"),
        pytest.param(
            {"a": _module({"u": "b"}), "b": _module({"u": "a"})}, "cannot tell", id="no-candidate"
        ),
    ],
)
def test_nextpnr_needs_one_top_module(modules, problem):
    with pytest.raises(AssertionError, match=problem):
        unplaceable_cells({"modules": modules}, "xilinx")


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
    found = unplaceable_cells(netlist, flow.settings.synthesis_target())
    assert not found, (
        f"{netlist['creator']} left generic cells in the netlist that nextpnr cannot place: "
        + ", ".join(f"{len(c)} x {t} (first: {c[0]})" for t, c in found.items())
    )
