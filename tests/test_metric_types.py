"""A value that a flow writes has the type that its descriptor declares.

The parsers run here on recorded reports and on small reports written for the purpose. The
oracle is `tests/metric_types.py`: an integer is an `int` and no `bool`, a number an `int` or a
`float`, and so on.
"""

import json
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FPGA, MetricSpec
from xeda.flow.sim import SimEvent, SimEvidence, SimFlow, judge_evidence
from xeda.flows import Dc, Nextpnr, Quartus, VivadoPower, Yosys, YosysFpga
from xeda.flows.openroad import Openroad

from .metric_types import (
    VALUE_TESTS,
    descriptor_of,
    described_keys,
    mismatches,
    undescribed_keys,
)
from .test_documentation import FLOW_CLASSES
from .test_nextpnr import load_ecp5_report, make_flow
from .test_sim_evidence import _Stub
from .test_openroad_final import parsed as parsed_openroad_final
from .test_yosys_results import RESOURCES_DIR, parsed_flow  # noqa: F401  (a fixture)

SIM_FLOW_CLASSES = [cls for cls in FLOW_CLASSES if issubclass(cls, SimFlow)]


def check(flow_class, results, minimum: int, *, all_described: bool = False) -> None:
    """Check the types of the described keys. With `all_described`, every key must be described
    (or internal)."""
    assert not mismatches(flow_class, results)
    if all_described:
        assert not undescribed_keys(flow_class, results)
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


def test_a_key_is_described_by_its_own_entry_only():
    declared = VivadoPower.results_description
    assert descriptor_of("power.total", declared) is declared["power.total"]
    assert descriptor_of("power.components.Clocks", declared) is None
    assert descriptor_of("Unknown", declared) is None


def test_the_oracle_finds_a_value_of_the_wrong_type():
    class Fake:
        results_description = {"lut": MetricSpec("LUTs.", "integer")}

    assert mismatches(Fake, {"lut": "3"}) == ["lut: declared integer, written '3'"]  # type: ignore[arg-type]
    assert not mismatches(Fake, {"lut": 3})  # type: ignore[arg-type]


def test_yosys_writes_the_declared_types(parsed_flow):  # noqa: F811
    check(Yosys, parsed_flow.results, 2, all_described=True)
    by_type = json.loads((RESOURCES_DIR / "yosys" / "stat_liberty.json").read_text())
    assert parsed_flow.results["cell_types"] == by_type["design"]["num_cells_by_type"]


def test_yosys_fpga_writes_the_declared_types(tmp_path):
    cells = {
        "LUT6": 5,
        "LUT2": 3,
        "FDRE": 7,
        "FDCE": 1,
        "LDCE": 1,
        "RAMB36E1": 1,
        "DSP48E1": 2,
        "CARRY4": 2,
    }
    report = tmp_path / "utilization.json"
    report.write_text(json.dumps({"design": {"num_cells_by_type": cells}, "modules": {}}))
    design = Design.from_file(RESOURCES_DIR / "design0" / "design0.toml")
    flow = YosysFpga(YosysFpga.Settings(fpga=FPGA(part="xc7a35tcpg236-1")), design, tmp_path)
    flow.init()
    flow.artifacts.utilization_report = report
    assert flow.parse_reports()
    check(YosysFpga, flow.results, 6, all_described=True)
    assert flow.results["latch"] == 1 and flow.results["dsp"] == 2
    assert flow.results["bram.ramb36"] == 1 and "bram.ramb18" not in flow.results
    assert flow.results["cell_types"] == cells


@pytest.mark.parametrize("flow_class", SIM_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_every_simulation_flow_writes_the_declared_types(flow_class):
    flow = _Stub()
    judge_evidence(
        flow,
        SimEvidence(
            ended_by="finish",
            time=42,
            time_unit="1ps",
            events=[SimEvent(kind="warning", message="w"), SimEvent(kind="error", message="e")],
        ),
        "fatal",
    )
    check(flow_class, flow.results, 5, all_described=True)
    assert flow.results["sim.time"] == 42_000
    assert (flow.results["sim.errors"], flow.results["sim.warnings"]) == (1, 1)


def test_nextpnr_writes_the_declared_types(tmp_path):
    flow = make_flow(tmp_path, load_ecp5_report())
    assert flow.parse_reports()
    check(Nextpnr, flow.results, 8, all_described=True)
    assert flow.results["cell_types"]["TRELLIS_COMB"] == flow.results["lut"]


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
        rtl={"sources": [], "top": "top", "clocks": ["clk"]},  # type: ignore[arg-type]
    )
    lib = tmp_path / "cells.db"
    lib.write_text("")
    settings = Dc.Settings.from_input(
        {"target_libraries": [str(lib)], "clocks": {"main_clock": {"period": "10 ns"}}}
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
    assert results["power.total"] == 0.5
    assert results["power.components"] == {"Clocks": 0.2}
    assert results["power.confidence"] == "High"
    # a bound is no number, so no value is written
    assert "power.static" not in results


def test_vivado_power_writes_no_metric_for_a_summary_row_it_does_not_know(tmp_path):
    """A summary row that another device or Vivado release adds names no documented metric, so
    it gives none: no key of the report's own spelling reaches the results."""
    report = tmp_path / "power.xml"
    report.write_text(
        POWER_REPORT.replace(
            "</table>\n<section",
            '<tablerow><tablecell contents="Foo (W)"/><tablecell contents="0.1"/></tablerow>\n'
            '<tablerow><tablecell contents="Design Nets Matched"/><tablecell contents="95%"/>'
            "</tablerow>\n</table>\n<section",
            1,
        )
    )
    flow = VivadoPower.__new__(VivadoPower)
    results = flow.parse_power_report(report)
    assert set(results) == {
        "power.total",
        "power.dynamic",
        "power.effective_tja",
        "power.junction_temperature",
        "power.thermal_margin",
        "power.confidence",
        "power.components",
    }


def test_quartus_reports_the_frequency_of_each_clock_as_one_table(tmp_path):
    design = Design(
        name="top",
        rtl={"sources": [], "top": "top", "clocks": ["clk"]},  # type: ignore[arg-type]
    )
    settings = Quartus.Settings.from_input(
        {"fpga": {"part": "10CL025YU256C8G"}, "clocks": {"main_clock": {"period": "10 ns"}}}
    )
    flow = Quartus(settings, design, tmp_path)
    flow.init()
    reports = tmp_path / flow.settings.reports_dir
    timing = reports / "Timing_Analyzer"
    for model, fmax in (("1200mV_85C", "150.5 MHz"), ("1200mV_0C", "0.2 GHz")):
        directory = timing / f"Slow_{model}_Model"
        directory.mkdir(parents=True)
        (directory / f"Slow_{model}_Model_Fmax_Summary.csv").write_text(
            f'Clock Name,Fmax,Restricted Fmax\nmain_clock,"{fmax}","{fmax}"\n'
        )
    (reports / "Fitter" / "Resource_Section").mkdir(parents=True)
    (reports / "Flow_Summary.csv").write_text("Flow Status,Successful\n")
    (
        reports / "Fitter" / "Resource_Section" / "Fitter_Resource_Utilization_by_Entity.csv"
    ).write_text(
        "Compilation Hierarchy Node,LUT-Only LCs,LUT/Register LCs,Register-Only LCs\ntop,3,2,1\n"
    )
    (timing / "Multicorner_Timing_Analysis_Summary.csv").write_text(
        "Clock,Setup,Hold\nWorst-case Slack,1.5,0.25\n"
    )
    assert flow.parse_reports()
    check(Quartus, flow.results, 5)
    assert flow.results["fmax"] == 200.0
    assert sorted(flow.results["fmax_by_clock"].items()) == [
        ("main_clock@1200mV:0C", 200.0),
        ("main_clock@1200mV:85C", 150.5),
    ]
    assert not [key for key in flow.results if "Fmax" in str(key)]


def test_openroad_final_writes_the_declared_types(tmp_path):
    flow, ok = parsed_openroad_final(tmp_path)
    assert ok
    check(Openroad, flow.results, 25)
