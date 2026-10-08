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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from .tool_utils import require_yosys

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

#: A net of the flattened design: the path of an instance (the names of the cells that lead to it
#: from the top module) and a net number of that instance's module.
Net = tuple[tuple[str, ...], Any]
#: A cell that is not an instance of a module: the path of its instance, its name, and the cell.
Leaf = tuple[tuple[str, ...], str, dict[str, Any]]

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
#: nextpnr makes the IO buffers after it has imported every cell, on the nets of the flattened
#: design (`import_module`), so the `$_TBUF_` may sit in any instance (see `_flatten`):
#: https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L269-L303
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


def _flatten(modules: dict[str, Any], top: str) -> tuple[list[Leaf], Callable[[Net], Net]]:
    """The leaf cells below `top` as nextpnr's reader imports them, and the nets they sit on.

    The walk goes into every instance of a module that is not a box, and a module used twice is
    two instances, as in nextpnr. Every other cell is a leaf cell. `root` gives the same net for
    all the nets that nextpnr joins into one:

    - `import_submodule_cell` connects each port bit of an instance to the net of the parent that
      the instance connects it to, and `import_port_connections` makes the net of that bit in the
      module the same net. A constant or unconnected bit joins no other net: nextpnr gives a
      constant a net of its own, and skips a port that the instance does not connect.
    - A net that is on two ports of a module joins the two nets of the parent (`merge_nets`).

    https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L509-L550
    https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L692-L728
    https://github.com/YosysHQ/nextpnr/blob/ad8527f8f46eb1e512f2dcffd8eb42ca20c7b1b5/frontend/frontend_base.h#L730-L775
    """
    leaves: list[Leaf] = []
    parent: dict[Net, Net] = {}

    def root(net: Net) -> Net:
        while parent.setdefault(net, net) != net:
            parent[net] = parent[parent[net]]
            net = parent[net]
        return net

    def instantiate(module: str, path: tuple[str, ...]) -> None:
        for name, cell in modules[module]["cells"].items():
            cell_type = cell["type"]
            if cell_type not in modules or _is_box(modules[cell_type]):
                leaves.append((path, name, cell))
                continue
            inner = (*path, name)
            ports = modules[cell_type]["ports"]
            for port, nets in cell["connections"].items():
                for inside, outside in zip(ports[port]["bits"], nets):
                    if isinstance(inside, int) and isinstance(outside, int):
                        parent[root((inner, inside))] = root((path, outside))
            instantiate(cell_type, inner)

    instantiate(top, ())
    return leaves, root


def unplaceable_cells(netlist: dict[str, Any], target: str) -> dict[str, list[str]]:
    """The generic cells that nextpnr would import from `netlist` and could not place, for the
    synthesis `target` (`YosysFpga.Settings.synthesis_target()`).

    The result maps each such cell type to the names that nextpnr gives its cells: the name of the
    cell in the top module, or `instance.cell` below an instance.

    The walk follows nextpnr's reader (`_flatten`). It starts at the top module, goes into every
    instance of a module that is not a box, and takes each other cell as a leaf cell of its type.
    It does not read inside a box, and it does not read a module that the top module does not
    reach. The file holds `$` cells in those places that nextpnr never sees: every library model
    carries some, such as `$specify2`.

    A leaf cell is no concern when its type is in `NEXTPNR_SKIPPED_CELLS`. A `$_TBUF_` is no
    concern either, on a target in `PAD_TBUF_TARGETS`, when its output net is the net of a pad: a
    bit of an output or inout port of the top module, once the hierarchy is flattened.
    """
    modules = netlist["modules"]
    top = _top_module(modules)
    leaves, root = _flatten(modules, top)
    pads = {
        root(((), bit))
        for port in modules[top]["ports"].values()
        if port["direction"] in ("output", "inout")
        for bit in port["bits"]
        if isinstance(bit, int)
    }
    found: dict[str, list[str]] = {}
    for path, name, cell in leaves:
        cell_type = cell["type"]
        if not cell_type.startswith("$") or cell_type in NEXTPNR_SKIPPED_CELLS:
            continue
        if (
            cell_type == "$_TBUF_"
            and target in PAD_TBUF_TARGETS
            and root((path, cell["connections"]["Y"][0])) in pads
        ):
            continue
        found.setdefault(cell_type, []).append(".".join((*path, name)))
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


def _instance(module: str, **nets: list[Any]) -> dict[str, Any]:
    """A cell that instantiates `module`. The keywords give the nets that its ports connect to."""
    return {"type": module, "connections": nets}


def _driver_in_sub(driven: int, connected: Any = 5) -> dict[str, Any]:
    """A netlist whose top module has a port on net 5. Instance `u` of `sub` connects its output
    `y` to `connected` (a net of the top module, a constant, or nothing), and `sub` holds a
    `$_TBUF_` that drives net `driven` of `sub`. Net 9 of `sub` is its port `y`."""
    cells = {"u": _instance("sub") if connected is None else _instance("sub", y=[connected])}
    return {
        "modules": {
            "top": _module(cells, "top", ports=[("pad", "inout", [5])]),
            "sub": _module({"drv": _tbuf(driven)}, ports=[("y", "output", [9])]),
        }
    }


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
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["buf"], "$_AND_": ["u.gate"]}


@pytest.mark.parametrize("cell_type", sorted(NEXTPNR_SKIPPED_CELLS))
def test_the_check_accepts_the_generic_cells_nextpnr_skips(cell_type):
    netlist = {"modules": {"top": _module({"c": cell_type}, "top")}}
    assert unplaceable_cells(netlist, "ecp5") == {}


@pytest.mark.parametrize(
    "cell_type", ["$buf", "$lut", "$_AND_", "$mem_v2", "$dff", "$paramod\\FDRE\\INIT=1'0"]
)
def test_the_check_reports_every_other_generic_cell(cell_type):
    netlist = {"modules": {"top": _module({"c": cell_type}, "top")}}
    assert unplaceable_cells(netlist, "ecp5") == {cell_type: ["c"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
@pytest.mark.parametrize("direction", ["output", "inout"])
def test_a_tristate_buffer_that_drives_a_pad_is_merged_into_its_io_cell(target, direction):
    assert unplaceable_cells(_pad(5, direction), target) == {}


@pytest.mark.parametrize("target", ["xilinx", "gowin", "nexus"])
def test_a_target_without_the_merge_cannot_place_a_tristate_buffer_even_on_a_pad(target):
    assert unplaceable_cells(_pad(5), target) == {"$_TBUF_": ["drv"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
@pytest.mark.parametrize(
    "netlist",
    [pytest.param(_pad(7), id="internal-net"), pytest.param(_pad(5, "input"), id="input-port")],
)
def test_any_other_tristate_buffer_is_a_cell_that_no_architecture_can_place(target, netlist):
    assert unplaceable_cells(netlist, target) == {"$_TBUF_": ["drv"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
def test_a_tristate_buffer_of_a_submodule_is_merged_when_its_net_reaches_a_pad(target):
    assert unplaceable_cells(_driver_in_sub(9), target) == {}


@pytest.mark.parametrize("target", ["xilinx", "gowin", "nexus"])
def test_a_target_without_the_merge_cannot_place_a_tristate_buffer_of_a_submodule(target):
    assert unplaceable_cells(_driver_in_sub(9), target) == {"$_TBUF_": ["u.drv"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
@pytest.mark.parametrize(
    "netlist",
    [
        # a net inside `sub`, which is not its port
        pytest.param(_driver_in_sub(8), id="net-inside-the-submodule"),
        # net numbers belong to one module: net 5 of `sub` is not the pad of `top`
        pytest.param(_driver_in_sub(5), id="number-of-the-pad"),
        pytest.param(_driver_in_sub(9, connected=6), id="port-on-an-internal-net"),
        pytest.param(_driver_in_sub(9, connected="0"), id="port-on-a-constant"),
        pytest.param(_driver_in_sub(9, connected="x"), id="port-unconnected-bit"),
        pytest.param(_driver_in_sub(9, connected=None), id="port-not-connected"),
    ],
)
def test_a_tristate_buffer_of_a_submodule_that_reaches_no_pad_is_refused(target, netlist):
    assert unplaceable_cells(netlist, target) == {"$_TBUF_": ["u.drv"]}


@pytest.mark.parametrize("target", sorted(PAD_TBUF_TARGETS))
def test_a_module_used_twice_is_two_instances(target):
    netlist = {
        "modules": {
            "top": _module(
                {"a": _instance("sub", y=[5]), "b": _instance("sub", y=[6])},
                "top",
                ports=[("pad", "inout", [5])],
            ),
            "sub": _module({"drv": _tbuf(9), "buf": "$buf"}, ports=[("y", "output", [9])]),
        }
    }
    # the instance on the pad merges its `$_TBUF_`, the other one does not. nextpnr imports every
    # other cell once for each instance.
    assert unplaceable_cells(netlist, target) == {"$_TBUF_": ["b.drv"], "$buf": ["a.buf", "b.buf"]}


@pytest.mark.parametrize(
    "target, middle, expected",
    [
        ("ecp5", 3, {}),
        ("ice40", 3, {}),
        ("xilinx", 3, {"$_TBUF_": ["m.s.drv"]}),
        # the port of `sub` is on a net of `mid` that is not the port `z` of `mid`
        ("ecp5", 4, {"$_TBUF_": ["m.s.drv"]}),
        ("ice40", 4, {"$_TBUF_": ["m.s.drv"]}),
    ],
)
def test_a_chain_of_ports_leads_a_tristate_buffer_to_the_pad(target, middle, expected):
    netlist = {
        "modules": {
            "top": _module({"m": _instance("mid", z=[5])}, "top", ports=[("pad", "inout", [5])]),
            "mid": _module({"s": _instance("sub", y=[middle])}, ports=[("z", "output", [3])]),
            "sub": _module({"drv": _tbuf(9)}, ports=[("y", "output", [9])]),
        }
    }
    assert unplaceable_cells(netlist, target) == expected


def test_ports_that_share_a_net_inside_a_module_join_the_nets_they_connect():
    # `through` connects its ports `a` and `b` inside. nextpnr merges the two nets of the parent
    # that they are connected to (`merge_nets`), so the `$_TBUF_` on net 7 drives the pad on net 5.
    netlist = {
        "modules": {
            "top": _module(
                {"u": _instance("through", a=[5], b=[7]), "drv": _tbuf(7)},
                "top",
                ports=[("pad", "inout", [5])],
            ),
            "through": _module({}, ports=[("a", "input", [3]), ("b", "output", [3])]),
        }
    }
    assert unplaceable_cells(netlist, "ecp5") == {}


def test_an_unconnected_bit_joins_no_nets():
    # In `alias`, the ports `y` and `z` are one net. Instance `a` puts `y` on the pad and leaves `z`
    # unconnected. Instance `b` leaves `y` unconnected too, and must not end up on the pad with it.
    netlist = {
        "modules": {
            "top": _module(
                {"a": _instance("alias", y=[5], z=["x"]), "b": _instance("alias", y=["x"])},
                "top",
                ports=[("pad", "inout", [5])],
            ),
            "alias": _module({"drv": _tbuf(9)}, ports=[("y", "output", [9]), ("z", "output", [9])]),
        }
    }
    assert unplaceable_cells(netlist, "ecp5") == {"$_TBUF_": ["b.drv"]}


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
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["buf"]}


def test_without_a_marked_module_the_top_module_is_the_one_nothing_instantiates():
    netlist = {
        "modules": {
            "top": _module({"u": "sub"}),
            "sub": _module({"buf": "$buf"}),
            "FDRE": _module({}, "blackbox"),
        }
    }
    assert unplaceable_cells(netlist, "xilinx") == {"$buf": ["u.buf"]}


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
