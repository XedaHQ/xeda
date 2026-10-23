"""A value that a flow writes has the type that its descriptor declares.

The parsers run here on recorded reports and on small reports written for the purpose. The
oracle is `tests/metric_types.py`: an integer is an `int` and no `bool`, a number an `int` or a
`float`, and so on.
"""

import json
from pathlib import Path


from xeda import Design
from xeda.flow import FPGA, MetricSpec
from xeda.flows import Dc, Nextpnr, VivadoPower, Yosys, YosysFpga
from xeda.flows.openroad import Openroad
from xeda.utils import WorkingDirectory

from .metric_types import VALUE_TESTS, descriptor_of, described_keys, mismatches
from .test_nextpnr import load_ecp5_report, make_flow
from .test_openroad_final import parsed as parsed_openroad_final
from .test_yosys_results import RESOURCES_DIR, parsed_flow  # noqa: F401  (a fixture)


def check(flow_class, results, minimum: int) -> None:
    assert not mismatches(flow_class, results)
    assert len(described_keys(flow_class, results)) >= minimum


def test_the_oracle_tells_each_type_from_the_others():
    assert VALUE_TESTS["integer"](3) and not VALUE_TESTS["integer"](True)
    assert not VALUE_TESTS["integer"](3.0) and not VALUE_TESTS["integer"]("3")
    assert VALUE_TESTS["number"](3) and VALUE_TESTS["number"](0.5)
    assert not VALUE_TESTS["number"](True) and not VALUE_TESTS["number"]("0.5")
    assert VALUE_TESTS["boolean"](False) and not VALUE_TESTS["boolean"](0)
    assert VALUE_TESTS["text"]("a") and not VALUE_TESTS["text"](["a"])
    assert VALUE_TESTS["text_list"](["a", "b"]) and not VALUE_TESTS["text_list"](["a", 1])
    assert VALUE_TESTS["counts"]({"a": 1}) and not VALUE_TESTS["counts"]({"a": 1.5})
    assert VALUE_TESTS["numbers"]({"a": 1.5}) and not VALUE_TESTS["numbers"]({"a": "1"})
    assert VALUE_TESTS["object"]({}) and not VALUE_TESTS["object"]([])


def test_a_key_with_a_placeholder_is_described_by_its_entry():
    declared = VivadoPower.results_description
    assert (
        descriptor_of("Component Power: Clocks", declared)
        is declared["Component Power: <component>"]
    )
    assert descriptor_of("Component Power: ", declared) is None
    assert descriptor_of("Unknown", declared) is None


def test_the_oracle_finds_a_value_of_the_wrong_type():
    class Fake:
        results_description = {"lut": MetricSpec("LUTs.", "integer")}

    assert mismatches(Fake, {"lut": "3"}) == ["lut: declared integer, written '3'"]  # type: ignore[arg-type]
    assert not mismatches(Fake, {"lut": 3})  # type: ignore[arg-type]


def test_yosys_writes_the_declared_types(parsed_flow):  # noqa: F811
    check(Yosys, parsed_flow.results, 2)


def test_yosys_fpga_writes_the_declared_types(tmp_path):
    cells = {"LUT6": 5, "LUT2": 3, "FDRE": 7, "FDCE": 1, "LDCE": 1, "RAMB36E1": 1, "DSP48E1": 2}
    report = tmp_path / "utilization.json"
    report.write_text(json.dumps({"design": {"num_cells_by_type": cells}, "modules": {}}))
    design = Design.from_file(RESOURCES_DIR / "design0" / "design0.toml")
    flow = YosysFpga(YosysFpga.Settings(fpga=FPGA(part="xc7a35tcpg236-1")), design, tmp_path)
    flow.init()
    flow.artifacts.utilization_report = report
    assert flow.parse_reports()
    check(YosysFpga, flow.results, 6)


def test_nextpnr_writes_the_declared_types(tmp_path):
    flow = make_flow(tmp_path, load_ecp5_report())
    assert flow.parse_reports()
    check(Nextpnr, flow.results, 8)


TIMING_MAX = """\
  clock clk (rise edge)                   10.00      10.00
  slack (MET)                              2.50
"""
TIMING_MIN = "  slack (MET)                              0.25\n"
AREA = """\
Number of ports:                           9
Number of nets:                           50
Number of cells:                          44
Number of combinational cells:            28
Number of sequential cells:               16
Number of macros/black boxes:              0
Combinational area:                  252.5
Noncombinational area:               434.75
Total cell area:                     687.25
Total area:                          687.25
"""
QOR = """\
Timing Path Group 'clk'
-----------------------------------
  Levels of Logic:               4
  Critical Path Length:          2.5

Area
-----------------------------------
  Combinational Area:          252.5
  Noncombinational Area:       434.75
-----------------------------------
  Net Area:                      0.0
  Cell Area:                   687.25

Design Rules
-----------------------------------
  Total Number of Nets:        50
  Nets With Violations:         0
  Max Trans Violations:         2
"""


def test_dc_writes_the_declared_types(tmp_path):
    design = Design(
        name="top",
        rtl={"sources": [], "top": "top", "clock": "clk"},  # type: ignore[arg-type]
    )
    lib = tmp_path / "cells.db"
    lib.write_text("")
    settings = Dc.Settings.from_input(
        {"target_libraries": [str(lib)], "clock": {"period": "10 ns"}}
    )
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(settings, design, run_path)
    flow.init()
    reports = Path(run_path) / flow.settings.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("mapped.timing.max.rpt", TIMING_MAX),
        ("mapped.timing.min.rpt", TIMING_MIN),
        ("mapped.area.rpt", AREA),
        ("mapped.qor.rpt", QOR),
    ):
        (reports / name).write_text(text)
    with WorkingDirectory(run_path):
        assert flow.parse_reports()
    check(Dc, flow.results, 12)
    assert isinstance(flow.results["area"], dict)


POWER_REPORT = """\
<report><section title="Summary"><table>
<tablerow><tablecell contents="Total On-Chip Power (W)"/><tablecell contents="0.5"/></tablerow>
<tablerow><tablecell contents="Dynamic (W)"/><tablecell contents="0.4"/></tablerow>
<tablerow><tablecell contents="Device Static (W)"/><tablecell contents="&lt;0.001"/></tablerow>
<tablerow><tablecell contents="Effective TJA (C/W)"/><tablecell contents="4.6"/></tablerow>
<tablerow><tablecell contents="Junction Temperature (C)"/><tablecell contents="26.3"/></tablerow>
<tablerow><tablecell contents="Thermal Margin (C)"/><tablecell contents="58.7"/></tablerow>
<tablerow><tablecell contents="Confidence Level"/><tablecell contents="High"/></tablerow>
</table>
<section title="On-Chip Components"><table>
<tablerow><tablecell contents="Clocks"/><tablecell contents="0.2"/></tablerow>
<tablerow><tablecell contents="Signals"/><tablecell contents="&lt;0.001"/></tablerow>
</table></section></section></report>
"""


def test_vivado_power_writes_the_declared_types(tmp_path):
    report = tmp_path / "power.xml"
    report.write_text(POWER_REPORT)
    flow = VivadoPower.__new__(VivadoPower)
    results = flow.parse_power_report(report)
    check(VivadoPower, results, 7)
    assert results["Total On-Chip Power (W)"] == 0.5
    assert results["Component Power: Clocks"] == 0.2
    assert results["Confidence Level"] == "High"
    # a bound is no number, so no value is written
    assert "Device Static (W)" not in results
    assert "Component Power: Signals" not in results


def test_openroad_final_writes_the_declared_types(tmp_path):
    flow, ok = parsed_openroad_final(tmp_path)
    assert ok
    check(Openroad, flow.results, 25)
