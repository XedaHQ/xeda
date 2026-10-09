"""Regression tests for Diamond's generated clock constraint templates."""

import re
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FPGA, FlowFatalError, FlowSettingsException
from xeda.flows import DiamondSynth
from .tool_utils import fake_calls, use_fake_tools


@pytest.mark.parametrize("template", ["constraints.sdc", "constraints.ldc"])
def test_diamond_clock_constraint_uses_settings_clock(tmp_path: Path, template: str) -> None:
    design = Design.from_file(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(clock={"period": 5.5}), design, tmp_path)

    generated = flow.copy_from_template(template)

    # `main_clock` is the flow's reconciled physical clock: its `port` comes from the
    # design's clock port (`clk`, see design0.toml), and its period from the flow settings.
    main_clock = flow.settings.main_clock
    assert main_clock is not None
    assert main_clock.port == "clk"
    assert main_clock.period == 5.5

    assert (
        tmp_path / generated
    ).read_text() == 'create_clock -period 5.500 -name "main_clock" [get_ports "clk"]'


def test_diamond_synth_without_clock_raises_flow_settings_exception(tmp_path: Path) -> None:
    design = Design.from_file(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(), design, tmp_path)

    assert flow.settings.main_clock is None

    with pytest.raises(FlowSettingsException, match="diamond_synth needs a clock"):
        flow.run()


def _diamond(tmp_path: Path, monkeypatch) -> DiamondSynth:
    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = Design.from_file(Path(__file__).parent / "resources/design0/design0.toml")
    return DiamondSynth(
        DiamondSynth.Settings(fpga=FPGA("LFE5U-25F-6BG256C"), clock={"period": 5.5}),
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
    assert not flow.artifacts
