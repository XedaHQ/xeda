"""Regression tests for Diamond's generated clock constraint templates."""

import re
from pathlib import Path

import pytest

from xeda import Design
from xeda._internal.flow import FPGA, FlowFatalError, FlowSettingsException
from xeda._internal.flows import DiamondSynth
from .tool_utils import fake_calls, use_fake_tools


@pytest.mark.parametrize("template", ["constraints.sdc", "constraints.ldc"])
def test_diamond_clock_constraint_uses_settings_clock(tmp_path: Path, template: str) -> None:
    design = Design.load(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(
        DiamondSynth.Settings(clocks={"main_clock": {"period": "5.5 ns"}}), design, tmp_path
    )

    generated = flow.copy_from_template(template)

    # `main_clock` is the flow's reconciled physical clock: its `port` comes from the
    # design's clock port (`clk`, see design0.toml), and its period from the flow settings.
    main_clock = flow.settings.main_clock
    assert main_clock is not None
    assert main_clock.port == "clk"
    assert main_clock.period.to("ns") == 5.5

    assert (
        tmp_path / generated
    ).read_text() == 'create_clock -period 5.500 -name "main_clock" [get_ports "clk"]'


def test_diamond_synth_without_clock_raises_flow_settings_exception(tmp_path: Path) -> None:
    design = Design.load(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(), design, tmp_path)

    assert flow.settings.main_clock is None

    with pytest.raises(FlowSettingsException, match="diamond_synth needs a clock"):
        flow.run()


def _diamond(tmp_path: Path, monkeypatch) -> DiamondSynth:
    use_fake_tools(monkeypatch)
    design = Design.load(Path(__file__).parent / "resources/design0/design0.toml")
    return DiamondSynth(
        DiamondSynth.Settings(
            fpga=FPGA("LFE5U-25F-6BG256C"), clocks={"main_clock": {"period": "5.5 ns"}}
        ),
        design,
        tmp_path,
    )


def test_diamond_export_produces_and_records_bitstream(tmp_path: Path, monkeypatch) -> None:
    """Export asks for its Bitgen task: the default Export tasks are the device's (a JEDEC file
    for MachXO parts). The bitstream and the reports `parse_reports` reads are recorded, each a
    file -- the implementation directory was recorded whole, which no remote run can fetch."""
    flow = _diamond(tmp_path, monkeypatch)
    flow.run()

    calls = fake_calls(flow.run_path)
    assert ["prj_run", "Export", "-impl", "Implementation0", "-task", "Bitgen"] in calls
    impl = flow.run_path / "diamond_impl" / f"{flow.design.name}_Implementation0"
    expected = {
        "bitstream": Path(f"{impl}.bit"),
        "timing_report": Path(f"{impl}.twr"),
        "place_route_report": Path(f"{impl}.par"),
        "map_report": Path(f"{impl}.mrp"),
    }
    assert flow.artifacts.pop("log") == "logs/diamondc.log"
    assert flow.artifacts == expected
    assert all(path.is_file() for path in expected.values())


def test_a_diamond_run_without_its_bitstream_fails_naming_it(tmp_path: Path, monkeypatch) -> None:
    """A run whose tool reported success but wrote no bitstream fails, naming the file it
    expected, and records nothing that does not exist."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_NO_OUTPUT", "1")
    flow = _diamond(tmp_path, monkeypatch)
    expected = tmp_path / "diamond_impl" / f"{flow.design.name}_Implementation0.bit"

    with pytest.raises(FlowFatalError, match=re.escape(str(expected))):
        flow.run()
    assert ["prj_run", "Export", "-impl", "Implementation0", "-task", "Bitgen"] in fake_calls(
        tmp_path
    )
    assert set(flow.artifacts) == {"log"}  # nothing that does not exist but the log


MAP_REPORT_HEAD = """\
Design Summary
--------------

   Number of registers:    120 out of 24288 (0%)
      PFU registers:          120 out of 24288 (0%)
      PIO registers:            0 out of   197 (0%)
   Number of SLICEs:        80 out of 12144 (1%)
      SLICEs as Logic/ROM:     80 out of 12144 (1%)
      SLICEs as RAM:            0 out of  9108 (0%)
      SLICEs as Carry:         10 out of 12144 (0%)
   Number of LUT4s:        150 out of 24288 (1%)
      Number used as logic LUTs:        130
      Number used as distributed RAM:     0
      Number used as ripple logic:       20
      Number used as shift registers:     0
   Number of block RAMs:  2 out of 56 (4%)

"""


def _map_report(tmp_path: Path, dsp_lines: str) -> Path:
    # The fixture is synthetic (no Diamond run): it follows the layout of the lines that
    # `parse_map_report` reads. The DSP block lists a count for each primitive.
    path = tmp_path / "d_Implementation0.mrp"
    path.write_text(
        MAP_REPORT_HEAD
        + "Number Of Mapped DSP Components:\n--------------------------------\n"
        + dsp_lines
        + "--------------------------------\n"
    )
    return path


def test_the_dsp_metric_counts_every_dsp_primitive(tmp_path: Path) -> None:
    """The metric sums the multipliers, ALUs and pre-adders, so a multiply-accumulate counts
    its multiplier and its ALU."""
    design = Design.load(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(), design, tmp_path)
    report = _map_report(
        tmp_path,
        "   MULT18X18D          4\n   MULT9X9D            3\n   ALU54B              2\n"
        "   ALU24B              1\n   PRADD18A            5\n   PRADD9A             6\n",
    )
    flow.parse_map_report(report)
    assert flow.results["dsp"] == 21
    assert flow.results["bram"] == 2


def test_the_dsp_metric_is_zero_when_the_report_lists_zero_counts(tmp_path: Path) -> None:
    design = Design.load(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(), design, tmp_path)
    flow.parse_map_report(
        _map_report(tmp_path, "   MULT18X18D          0\n   ALU54B              0\n")
    )
    assert flow.results["dsp"] == 0
