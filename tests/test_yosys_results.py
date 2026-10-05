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


def test_every_lut_based_primitive_of_the_installed_yosys_has_a_footprint():
    """A distributed RAM, ROM or shift register the count does not know is counted as no LUT at all
    (RAM32X1D was: found on the real openXC7 build of a design instantiating one; the ROM family
    was missed because this pattern could not match it)."""
    import re
    import subprocess

    from .tool_utils import require_yosys, require_yosys_config

    from xeda.flows.yosys.yosys_fpga import xilinx_lut_footprint

    require_yosys()
    require_yosys_config()
    datdir = subprocess.run(
        ["yosys-config", "--datdir"], capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()
    cells = re.findall(
        r"^module\s+((?:LUT|RAM\d+[XM]|ROM\d+X|SRL|CFGLUT)\w*)",
        (Path(datdir) / "xilinx" / "cells_sim.v").read_text(),
        flags=re.MULTILINE,
    )
    assert len(cells) > 40
    assert not [cell for cell in cells if xilinx_lut_footprint(cell) is None]
    assert xilinx_lut_footprint("RAMB36E1") is None and xilinx_lut_footprint("FDRE") is None
