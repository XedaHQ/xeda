"""The `yosys` flow's declared result contract against a real `stat -json` report.

`results_description` is what `xeda list-results yosys` promises a script will find in
`results.json`. `cells` and `sequential_cells` were both advertised while `parse_reports`
wrote neither, so the machine-readable contract named values that never appeared. These tests
pin the contract to what the parser actually delivers, using a report captured verbatim from
yosys rather than one hand-written to match the parser.
"""

import json
import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda.flows import Yosys
from xeda.introspect import results_info

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"

#: A liberty-mapped `stat -json` report, as the ASIC `yosys` flow produces it.
STAT_REPORT = RESOURCES_DIR / "yosys" / "stat_liberty.json"


@pytest.fixture
def parsed_flow(tmp_path: Path) -> Yosys:
    report = tmp_path / "reports" / "utilization.json"
    report.parent.mkdir(parents=True)
    shutil.copy(STAT_REPORT, report)
    design = Design.from_file(RESOURCES_DIR / "design0" / "design0.toml")
    flow = Yosys(Yosys.Settings(clock_period=10.0), design, tmp_path)
    flow.init()
    # `get_utilization` opens the artifact path as given, so make it absolute rather than
    # depending on the process working directory.
    flow.artifacts.utilization_report = str(report.resolve())
    assert flow.parse_reports()
    return flow


def test_every_documented_key_is_actually_reported(parsed_flow: Yosys):
    """A key in `results_description` that `parse_reports` never writes is a false promise."""
    # `common` marks the keys the runner fills in (success, runtime, run_path, ...); everything
    # else is what this flow's own parse_reports() promised.
    flow_specific = [k["name"] for k in results_info(Yosys)["keys"] if not k["common"]]
    undelivered = sorted(k for k in flow_specific if parsed_flow.results.get(k) is None)
    assert not undelivered, (
        f"`xeda list-results yosys` advertises {undelivered}, but parse_reports() does not "
        "write them to results.json."
    )


def test_cells_comes_from_the_report(parsed_flow: Yosys):
    expected = json.loads(STAT_REPORT.read_text())["design"]["num_cells"]
    assert parsed_flow.results.get("cells") == expected


def test_area_comes_from_the_report(parsed_flow: Yosys):
    expected = json.loads(STAT_REPORT.read_text())["design"]["area"]
    assert parsed_flow.results.get("area") == pytest.approx(expected)


def test_per_cell_type_counts_are_reported(parsed_flow: Yosys):
    """`num_cells_by_type` is splatted into the results, keyed by liberty cell name."""
    by_type = json.loads(STAT_REPORT.read_text())["design"]["num_cells_by_type"]
    for cell, count in by_type.items():
        assert parsed_flow.results.get(cell) == count, cell


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"LUT1": 2, "LUT2": 3, "LUT6": 5, "LUT6_2": 7}, (24, 24, 0, 0)),
        ({"RAM32M": 2, "SRL16E": 3, "SRLC32E": 4}, (15, 0, 8, 7)),
        # every distributed RAM, not RAM32M alone: a 6-input LUT holds 64 x 1 or 32 x 2 bits,
        # and each further read port of a dual-port memory is another copy
        ({"RAM32X1D": 1, "RAM64X1S": 1, "RAM64M": 1}, (7, 0, 7, 0)),
        ({"RAM16X1S_1": 1, "RAM32X2S": 1, "RAM32X8S": 1, "RAM128X1D": 1}, (10, 0, 10, 0)),
        ({"RAM256X1S": 1, "RAM64X1D_1": 1, "RAM64X2S": 1, "CFGLUT5": 2}, (10, 2, 8, 0)),
        # a distributed ROM is a LUT holding an INIT value: logic, as Vivado reports it
        ({"ROM16X1": 1, "ROM32X1": 1, "ROM64X1": 1, "ROM128X1": 1, "ROM256X1": 1}, (9, 9, 0, 0)),
        ({"ROM256X1": 2, "RAM64X1S": 1, "LUT4": 1}, (10, 9, 1, 0)),
        # an INV is a LUT1, whether the design instantiates it or abc9 makes it of an inversion
        ({"INV": 2, "LUT2": 1}, (3, 3, 0, 0)),
        # the cells of `tests/test_openxc7_real.py`'s `prims` design as the real yosys maps it
        # (the real-tool layer pins the same four numbers against the installed toolchain)
        (
            {"LUT6_2": 1, "INV": 1, "RAM32X1D": 1, "SRL16E": 1, "CARRY4": 1, "FDRE": 4},
            (6, 3, 2, 1),
        ),
        ({}, (0, 0, 0, 0)),
    ],
)
def test_xilinx_lut_resource_footprint(counts, expected, tmp_path):
    """Yosys reports mapped primitive LUT-equivalent units, including dual-output and memories."""
    from xeda.flow import FPGA
    from xeda.flows import YosysFpga

    report = tmp_path / "utilization.json"
    report.write_text(json.dumps({"design": {"num_cells_by_type": counts}, "modules": {}}))
    design = Design.from_file(RESOURCES_DIR / "design0" / "design0.toml")
    flow = YosysFpga(YosysFpga.Settings(fpga=FPGA(part="xc7a35tcpg236-1")), design, tmp_path)
    flow.init()
    flow.artifacts.utilization_report = report
    assert flow.parse_reports()
    lut, logic, ram, srl = expected
    assert flow.results.get("LUT") == lut
    assert flow.results.get("LUT:LOGIC", 0) == logic
    assert flow.results.get("LUT:RAM", 0) == ram
    assert flow.results.get("LUT:SRL", 0) == srl
    assert flow.results.get("LUT:STAGE") == "mapped"
    assert flow.results.get("LUT:METHOD") == "primitive-footprint estimate"


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("ROM16X1", ("logic", 1)),
        ("ROM32X1", ("logic", 1)),
        ("ROM64X1", ("logic", 1)),
        ("ROM128X1", ("logic", 2)),
        ("ROM256X1", ("logic", 4)),
        # the same arithmetic, still memory for a RAM
        ("RAM64X1S", ("ram", 1)),
        ("RAM256X1S", ("ram", 4)),
        ("RAM32X1D", ("ram", 2)),
    ],
)
def test_xilinx_distributed_rom_is_logic_footprint(cell, expected):
    from xeda.flows.yosys.yosys_fpga import xilinx_lut_footprint

    assert xilinx_lut_footprint(cell) == expected


def test_xilinx_lut_footprint_uses_design_totals_once_with_hierarchy(tmp_path):
    from xeda.flow import FPGA
    from xeda.flows import YosysFpga

    report = tmp_path / "utilization.json"
    report.write_text(
        json.dumps(
            {
                "design": {"num_cells_by_type": {"LUT1": 3, "RAM32M": 1}},
                "modules": {
                    "top": {"num_cells_by_type": {"LUT1": 3, "RAM32M": 1}},
                    "child": {"num_cells_by_type": {"LUT1": 3, "RAM32M": 1}},
                },
            }
        )
    )
    design = Design.from_file(RESOURCES_DIR / "design0" / "design0.toml")
    flow = YosysFpga(YosysFpga.Settings(fpga=FPGA(part="xc7a35tcpg236-1")), design, tmp_path)
    flow.init()
    flow.artifacts.utilization_report = report
    assert flow.parse_reports()
    assert flow.results["LUT"] == 7
    assert flow.results["LUT:LOGIC"] == 3
    assert flow.results["LUT:RAM"] == 4


#: Every module of the installed yosys's `xilinx/cells_sim.v` that `xilinx_lut_footprint`
#: deliberately reports no footprint for, and the resource each one really occupies. The sweep
#: below enumerates the whole library and requires every cell to be classified either here or in
#: `XILINX_LUT_FOOTPRINT`, so a LUT-based family nobody thought of fails the sweep instead of
#: being silently counted as no LUT at all.
#:
#: Reviewed against yosys 0.69 (OSS CAD Suite) and its
#: `$(yosys-config --datdir)/xilinx/cells_sim.v`. Every primitive the library declares is in
#: `XILINX_LUT_FOOTPRINT` or here, and none is in both; the sweep below checks exactly that, so
#: no tally of either table is kept in this comment to go stale. A newer yosys that ships a new
#: primitive fails the sweep, which says what to do; one that drops a primitive does not, and its
#: entry here may simply be deleted. Each entry is a claim about a
#: real Xilinx primitive, read from that primitive's ports, parameters and attributes in the
#: library and checked against the Xilinx libraries guide (UG953) and the 7-series CLB user guide
#: (UG474) -- not a transcript of what the classifier happens to reject today.
XILINX_NON_LUT_PRIMITIVES = {
    # Constant drivers: a tie-off, no CLB resource at all.
    "GND": "tie-off to the global logic-0 net",
    "VCC": "tie-off to the global logic-1 net",
    # I/O buffers: they occupy an IOB, not a slice.
    "IBUF": "input buffer in an IOB",
    "IBUFG": "clock-capable input buffer in an IOB",
    "OBUF": "output buffer in an IOB",
    "OBUFT": "three-state output buffer in an IOB",
    "IOBUF": "bidirectional buffer in an IOB",
    # Clock network: dedicated buffers.
    "BUFG": "global clock buffer in the clock network",
    "BUFGCTRL": "global clock multiplexer/buffer in the clock network",
    "BUFHCE": "horizontal clock buffer in the clock network",
    # The slice's dedicated arithmetic and wide-function logic, beside its LUTs.
    "CARRY4": "the slice's dedicated 4-bit carry chain",
    "CARRY8": "the slice's dedicated 8-bit carry chain",
    "MUXCY": "one multiplexer of the dedicated carry chain",
    "XORCY": "the dedicated carry-chain XOR gate",
    "ORCY": "the dedicated carry-chain OR gate (wide OR on Virtex-4/5)",
    "MULT_AND": "the slice's dedicated AND gate feeding the carry chain",
    "MUXF5": "the dedicated F5MUX joining two LUT outputs",
    "MUXF6": "the dedicated F6MUX joining two MUXF5 outputs",
    "MUXF7": "the dedicated F7MUX joining two LUT6 outputs",
    "MUXF8": "the dedicated F8MUX joining two MUXF7 outputs",
    "MUXF9": "the dedicated F9MUX joining two MUXF8 outputs",
    # The slice's latch used as a gate. UG474 (7 Series CLB User Guide, chapter 6, "Using the
    # Latch Function as Logic"): "Because the latch function is level-sensitive, it can be used as
    # the equivalent of a logic gate. The primitives to specify this function are AND2B1L ... and
    # OR2L"; "The AND2B1L and OR2L two-input gates save LUT resources". "Generally, the latch data
    # input comes from the output of a LUT within the same slice" (that LUT is its own cell); `DI`
    # is that input and `SRI` the slice's SR input. UG974 (UltraScale) titles them "implemented in place of a
    # CLB Latch" and files them under its CLB LATCH subgroup. `synth_xilinx` emits neither: this
    # applies only to a cell a design instantiates itself.
    # https://docs.amd.com/r/en-US/ug474_7Series_CLB/Using-the-Latch-Function-as-Logic
    # https://docs.amd.com/r/2021.1-English/ug974-vivado-ultrascale-libraries/AND2B1L
    "AND2B1L": "two-input AND gate in a CLB latch site, not a LUT",
    "OR2L": "two-input OR gate in a CLB latch site, not a LUT",
    # Flip-flops: slice registers, counted as `FF` in the same report.
    "FDCE": "slice flip-flop (clock enable, asynchronous clear)",
    "FDCE_1": "slice flip-flop (clock enable, asynchronous clear, negative edge)",
    "FDPE": "slice flip-flop (clock enable, asynchronous preset)",
    "FDPE_1": "slice flip-flop (clock enable, asynchronous preset, negative edge)",
    "FDRE": "slice flip-flop (clock enable, synchronous reset)",
    "FDRE_1": "slice flip-flop (clock enable, synchronous reset, negative edge)",
    "FDSE": "slice flip-flop (clock enable, synchronous set)",
    "FDSE_1": "slice flip-flop (clock enable, synchronous set, negative edge)",
    "FDRSE": "slice flip-flop (clock enable, synchronous reset and set)",
    "FDRSE_1": "slice flip-flop (clock enable, synchronous reset and set, negative edge)",
    "FDCPE": "slice flip-flop (asynchronous clear and preset)",
    "FDCPE_1": "slice flip-flop (asynchronous clear and preset, negative edge)",
    # Latches: the same storage elements, configured transparent; counted as `LATCH`.
    "LDCE": "slice storage element as a latch (asynchronous clear)",
    "LDPE": "slice storage element as a latch (asynchronous preset)",
    "LDCPE": "slice storage element as a latch (asynchronous clear and preset)",
    # Block RAM: a dedicated memory block, counted as `RAMB18`/`RAMB36`.
    "RAMB18E1": "18 Kb block RAM",
    "RAMB36E1": "36 Kb block RAM",
    # Dedicated arithmetic blocks, counted as `DSP`.
    "DSP48": "DSP slice (Virtex-4)",
    "DSP48A": "DSP slice (Spartan-3A DSP)",
    "DSP48A1": "DSP slice (Spartan-6)",
    "DSP48E1": "DSP slice (7 series)",
    "MULT18X18": "dedicated 18x18 multiplier block",
    "MULT18X18S": "dedicated 18x18 multiplier block, registered",
    "MULT18X18SIO": "dedicated 18x18 multiplier block, cascadable",
}

#: Primitives the sweep's enumeration must find: one of every family the classification covers,
#: so a pattern that silently stops matching a whole family fails here rather than narrowing the
#: sweep. The distributed ROMs are among them because they are what the old prefix allowlist had
#: to be told about by hand, after a reviewer found the gap.
_LIBRARY_SENTINELS = frozenset(
    {"LUT6_2", "CFGLUT5", "RAM16X1S", "RAM512X1S", "ROM256X1", "RAMB36E1", "DSP48E1"}
)


def test_every_lut_based_primitive_of_the_installed_yosys_has_a_footprint():
    """A distributed RAM, ROM or shift register the count does not know is counted as no LUT at
    all (RAM32X1D was: found on the real openXC7 build of a design instantiating one). Every
    module of the installed library is classified either by `XILINX_LUT_FOOTPRINT` or by
    `XILINX_NON_LUT_PRIMITIVES`, so a family nobody thought of fails rather than passing: the
    ROM family was missed while this sweep looked for families it already knew by name."""
    import re
    import subprocess

    from .tool_utils import require_yosys, require_yosys_config

    from xeda.flows.yosys.yosys_fpga import xilinx_lut_footprint

    require_yosys()
    require_yosys_config()
    version = subprocess.run(
        ["yosys", "-V"], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()
    datdir = subprocess.run(
        ["yosys-config", "--datdir"], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()
    library = Path(datdir) / "xilinx" / "cells_sim.v"
    # The library comments out two superseded declarations, so read the code, not the comments.
    code = re.sub(r"/\*.*?\*/", "", library.read_text(), flags=re.DOTALL)
    code = "\n".join(line.split("//")[0] for line in code.splitlines())
    # Unanchored: today every declaration starts its line, but one indented or prefixed with an
    # attribute must count too, and the `endmodule` tally is what proves none was missed.
    declared = re.findall(r"\bmodule\s+(\\?[\w$]+)", code)
    closed = re.findall(r"\bendmodule\b", code)
    assert len(declared) == len(closed), (
        f"{library} did not read as expected: {len(declared)} `module` declarations against "
        f"{len(closed)} `endmodule`s. Whatever the pattern cannot see, this sweep cannot "
        "classify, which is the blind spot it exists to close."
    )
    # `\$__ABC9_LUT7` and the like are yosys' own internal cells, never a mapped primitive.
    cells = {name.removeprefix("\\") for name in declared}
    cells = {name for name in cells if not name.startswith("$")}
    assert _LIBRARY_SENTINELS <= cells, (
        f"{library} yielded {len(cells)} primitives but not "
        f"{sorted(_LIBRARY_SENTINELS - cells)}, which it does declare."
    )
    unreviewed = sorted(
        cell
        for cell in cells
        if xilinx_lut_footprint(cell) is None and cell not in XILINX_NON_LUT_PRIMITIVES
    )
    assert not unreviewed, (
        f"{library}\n(installed yosys: {version})\nships {unreviewed}, which "
        "`xilinx_lut_footprint` reports no footprint for and nobody has reviewed. Decide for "
        "each one and record the decision:\n"
        "  - LUT-based (a LUT variant, a distributed RAM or ROM, a shift register): give it a "
        "footprint in `XILINX_LUT_FOOTPRINT` (src/xeda/flows/yosys/yosys_fpga.py) and a case "
        "in `test_xilinx_lut_resource_footprint`. Left unclassified it counts as no LUT at "
        "all, so every design using it under-reports its LUT count.\n"
        "  - not LUT-based (a flip-flop, carry chain, block RAM, DSP, I/O or clock resource): "
        "add it to `XILINX_NON_LUT_PRIMITIVES` in this file, with the resource it really "
        "occupies.\n"
        "The Xilinx libraries guide (UG953) and the CLB user guide (UG474) say which it is."
    )
    claimed_both_ways = sorted(
        cell for cell in XILINX_NON_LUT_PRIMITIVES if xilinx_lut_footprint(cell) is not None
    )
    assert not claimed_both_ways, (
        f"{claimed_both_ways} are claimed both ways: `XILINX_LUT_FOOTPRINT` gives a LUT "
        "footprint while `XILINX_NON_LUT_PRIMITIVES` in this file says the primitive occupies "
        "no LUTs. Remove each from whichever of the two is wrong."
    )
