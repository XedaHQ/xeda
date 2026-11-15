"""Tests for the Synopsys Design Compiler (`dc`) flow."""

import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda._internal.flow import FlowSettingsError
from xeda._internal.flows import Dc

from .tool_utils import fake_calls, use_fake_tools


def _dc_design(root: Path) -> Design:
    """A minimal single-file Verilog design."""
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.v").write_text("module top(input a, output y); assign y = ~a; endmodule\n")
    return Design(
        name="dcdesign",
        root=root,
        rtl={"sources": ["rtl/top.v"], "top": "top", "clocks": [{"port": "clk"}]},
    )


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh is needed to run the TCL script")
@pytest.mark.parametrize("sdf_version", [None, "3.0"])
def test_dc_records_the_outputs_its_script_writes(sdf_version, tmp_path, monkeypatch) -> None:
    """`dc_script.tcl` writes the mapped netlist (Verilog and VHDL), the `.ddc` checkpoint, SDF
    and SDC on every run, and `Dc` declared none of them. The SDF was not even written: with
    `sdf_version` unset, the script asked for `write_sdf -version None`."""
    use_fake_tools(monkeypatch)
    lib = tmp_path / "pdk" / "cells.db"
    lib.parent.mkdir()
    lib.write_text("")
    settings = Dc.Settings.from_input(
        {
            "target_libraries": [str(lib)],
            "clocks": {"main_clock": {"period": "5 ns"}},
            "sdf_version": sdf_version,
        }
    )
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(settings, _dc_design(tmp_path / "design"), run_path)
    flow.init()
    flow.run()  # the fake `dc_shell` runs the script, its commands recorded

    written = {
        label: str(flow.settings.outputs_dir / f"top.mapped.{suffix}")
        for label, suffix in (
            ("netlist", "v"),
            ("netlist_vhdl", "vhd"),
            ("checkpoint", "ddc"),
            ("sdf", "sdf"),
            ("sdc", "sdc"),
        )
    }
    version = ["-version", sdf_version] if sdf_version else []
    calls = fake_calls(run_path)
    for call in (
        ["write", "-hierarchy", "-format", "verilog", "-output", written["netlist"]],
        ["write", "-hierarchy", "-format", "vhdl", "-output", written["netlist_vhdl"]],
        ["write", "-hierarchy", "-format", "ddc", "-compress", "gzip", "-output"]
        + [written["checkpoint"]],
        ["write_sdf", *version, written["sdf"]],
        ["write_sdc", "-nosplit", written["sdc"]],
    ):
        assert call in calls
    assert flow.artifacts.pop("log") == "logs/dc_shell.log"  # the log of the main tool
    assert {label: str(path) for label, path in flow.artifacts.items()} == written


# ---------------------------------------------------------------------------------------------
# The time unit of the library

TIMING_MAX = """\
  clock clk (rise edge)                 5000.00    5000.00
  slack (MET)                            120.00
"""
TIMING_MIN = "  slack (MET)                             15.00\n"
QOR_WNS = """\
Timing Path Group 'clk'
-----------------------------------
  Levels of Logic:               4

Design WNS: 0.00  TNS: 0.00  Number of Violating Paths: 0
"""


def _dc_settings(tmp_path: Path, **settings) -> Dc.Settings:
    lib = tmp_path / "pdk" / "cells.db"
    lib.parent.mkdir(exist_ok=True)
    lib.write_text("")
    return Dc.Settings.from_input(
        {
            "target_libraries": [str(lib)],
            "clocks": {"main_clock": {"period": "5 ns"}},
            **settings,
        }
    )


def _flow_with_reports(tmp_path: Path, unit: str, **reports: str) -> Dc:
    flow = Dc(
        _dc_settings(tmp_path, library_time_unit=unit),
        _dc_design(tmp_path / "design"),
        tmp_path / "run",
    )
    flow.run_path.mkdir()
    flow.init()
    directory = flow.run_path / flow.settings.reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in reports.items():
        (directory / f"mapped.{name.replace('_', '.')}.rpt").write_text(text)
    return flow


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh is needed to run the TCL script")
@pytest.mark.parametrize(
    "unit,period,delays",
    [
        ("1 ns", "5.000", ("1.5", "0.25")),
        ("1 ps", "5000.000", ("1500.0", "250.0")),
        ("10 ps", "500.000", ("150.0", "25.0")),
    ],
)
def test_the_generated_constraints_are_in_the_time_unit_of_the_library(
    unit, period, delays, tmp_path, monkeypatch
) -> None:
    """DC reads the numbers of an SDC file in the time unit of its library: a period of 5 ns is
    `5000` for a library in ps."""
    use_fake_tools(monkeypatch)
    settings = _dc_settings(
        tmp_path,
        library_time_unit=unit,
        default_max_input_delay="1.5 ns",
        default_max_output_delay="250 ps",
    )
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(settings, _dc_design(tmp_path / "design"), run_path)
    flow.init()
    flow.run()
    sdc = (run_path / "constraints.sdc").read_text()
    assert f"create_clock -period {period} " in sdc
    assert f"set max_input_delay {delays[0]}\n" in sdc
    assert f"set max_output_delay {delays[1]}\n" in sdc


def test_the_times_of_the_reports_are_converted_from_the_unit_of_the_library(tmp_path):
    flow = _flow_with_reports(
        tmp_path, "1 ps", timing_max=TIMING_MAX, timing_min=TIMING_MIN, qor=QOR_WNS
    )
    assert flow.parse_reports()
    results = flow.results
    assert results["clock_period"] == 5.0
    assert results["setup_wns"] == 0.12
    assert results["hold_wns"] == 0.015
    assert results["fmax"] == pytest.approx(1000.0 / (5.0 - 0.12))


def test_a_library_in_ns_gives_the_numbers_of_the_report(tmp_path):
    flow = _flow_with_reports(
        tmp_path,
        "1 ns",
        timing_max=TIMING_MAX.replace("5000.00", "5.00").replace("120.00", "0.12"),
        qor=QOR_WNS,
    )
    assert flow.parse_reports()
    assert (flow.results["clock_period"], flow.results["setup_wns"]) == (5.0, 0.12)
    assert flow.results["fmax"] == pytest.approx(1000.0 / 4.88)


def test_the_design_rule_times_are_converted_from_the_unit_of_the_library(tmp_path):
    qor = "Design WNS: 250.00  TNS: 1500.00  Number of Violating Paths: 3\n"
    flow = _flow_with_reports(tmp_path, "1 ps", qor=qor)
    flow.parse_reports()
    assert (flow.results["_drc_wns"], flow.results["_drc_tns"]) == (0.25, 1.5)


@pytest.mark.parametrize("unit", ["2 ns", "0 ns", "1 min", "1500 ps"])
def test_a_time_unit_that_no_library_has_is_refused_at_planning(unit, tmp_path):
    settings = _dc_settings(tmp_path, library_time_unit=unit)
    with pytest.raises(FlowSettingsError, match="no time unit of a Liberty"):
        Dc.check_settings_supported(settings)


@pytest.mark.parametrize("unit", ["1 fs", "10 ps", "100 ps", "1000 ps", "0.001 us", "1 ns"])
def test_a_time_unit_that_a_library_has_is_accepted(unit, tmp_path):
    Dc.check_settings_supported(_dc_settings(tmp_path, library_time_unit=unit))


UNITS_REPORT = """\
****************************************
Report : units
Design : top
****************************************

Units
-----
Time_unit                : {time}
Capacitive_load_unit     : 1 pF
"""


def test_a_units_report_with_another_time_unit_rejects_the_run(tmp_path):
    flow = _flow_with_reports(
        tmp_path,
        "1 ns",
        units=UNITS_REPORT.format(time="1e-12 Second"),
        qor=QOR_WNS,
        timing_max=TIMING_MAX,
    )
    assert flow.parse_reports()
    assert not flow.check_results()
    assert flow.rejection is not None
    for text in ("1 ps", "1 ns", "`library_time_unit`", "mapped.units.rpt"):
        assert text in flow.rejection


@pytest.mark.parametrize(
    "report",
    [
        UNITS_REPORT.format(time="1e-09 Second"),
        UNITS_REPORT.format(time="1.0e-09 Second"),
        UNITS_REPORT.format(time="unknown"),  # a line that the pattern does not know: no check
        "Report : units\n",
        None,  # no report
    ],
)
def test_a_units_report_that_agrees_or_has_no_time_unit_does_not_reject(report, tmp_path):
    reports = {"qor": QOR_WNS, "timing_max": TIMING_MAX.replace("5000.00", "5.00")}
    if report is not None:
        reports["units"] = report
    flow = _flow_with_reports(tmp_path, "1 ns", **reports)
    assert flow.parse_reports()
    assert flow.check_results()
    assert flow.rejection is None
