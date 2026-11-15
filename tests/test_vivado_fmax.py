"""Vivado reports one Fmax for each clock, and `fmax` is the lowest of them."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from xeda import Design
from xeda._internal.flows import VivadoSynth
from xeda._internal.flows.vivado.vivado_synth import fmax_of_each_clock

from .tool_utils import FAKE_TOOLS_DIR

TWO_CLOCKS = """\
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints     WPWS(ns)     TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints  
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------     --------     --------  ----------------------  --------------------  
     -0.100       -0.400                      4                  100        0.006        0.000                      0                  100        0.616        0.000                       0                    50  


------------------------------------------------------------------------------------------------
| Clock Summary
| -------------
------------------------------------------------------------------------------------------------

Clock  Waveform(ns)         Period(ns)      Frequency(MHz)
-----  ------------         ----------      --------------
clk_a  {0.000 5.000}        10.000          100.000         
clk_b  {0.000 1.000}        2.000           500.000         
clk_c  {0.000 2.000}        4.000           250.000         


------------------------------------------------------------------------------------------------
| Intra Clock Table
| -----------------
------------------------------------------------------------------------------------------------

Clock             WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints     WPWS(ns)     TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints  
-----             -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------     --------     --------  ----------------------  --------------------  
clk_a               5.000        0.000                      0                   60        0.006        0.000                      0                   60        0.616        0.000                       0                    20  
clk_b              -0.100       -0.400                      4                   40        0.010        0.000                      0                   40        0.300        0.000                       0                    30  


------------------------------------------------------------------------------------------------
| Inter Clock Table
| -----------------
------------------------------------------------------------------------------------------------

From Clock    To Clock          WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints  
----------    --------          -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------  
clk_a         clk_b              -9.000      -90.000                     40                   40        0.100        0.000                      0                   40  

"""


def test_each_clock_has_its_own_period_and_slack():
    fmax = fmax_of_each_clock(TWO_CLOCKS)
    # `clk_c` has no intra-clock paths, and the inter-clock table counts for no clock.
    assert sorted(fmax) == ["clk_a", "clk_b"]
    assert fmax["clk_a"] == pytest.approx(200.0)
    assert fmax["clk_b"] == pytest.approx(1000.0 / 2.1)


def test_a_report_with_no_clock_tables_has_no_fmax():
    assert fmax_of_each_clock("| Design Timing Summary\n") == {}


def test_fmax_is_the_lowest_of_the_clocks(tmp_path: Path):
    design = Design(
        name="top",
        root=tmp_path,
        rtl={"sources": [], "top": "top", "clocks": ["clk_a", "clk_b"]},  # type: ignore[arg-type]
    )
    settings = VivadoSynth.Settings.from_input(
        {
            "fpga": {"part": "xc7a35ticsg324-1L"},
            "clocks": {"clk_a": {"period": "10 ns"}, "clk_b": {"period": "2 ns"}},
        }
    )
    flow = VivadoSynth(settings, design, tmp_path)
    flow.init()
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "timing_summary.rpt").write_text(TWO_CLOCKS)
    flow.parse_timing_report(reports)
    assert flow.results["wns"] == -0.1
    assert flow.results["clock_period"] == 10.0
    assert flow.results["fmax_by_clock"]["clk_a"] == pytest.approx(200.0)
    assert flow.results["fmax_by_clock"]["clk_b"] == pytest.approx(476.19, abs=0.01)
    assert flow.results["fmax"] == pytest.approx(200.0)


def test_the_canned_report_of_the_fake_tool_has_one_clock():
    with ZipFile(FAKE_TOOLS_DIR / "resource" / "fake_vivado_reports") as zf:
        report = zf.read("timing_summary.rpt").decode()
    fmax = fmax_of_each_clock(report)
    assert list(fmax) == ["clock"]
    assert fmax["clock"] == pytest.approx(1000.0 / (2.222 - 0.046))
