"""Regression tests for Diamond's generated clock constraint templates."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flows import DiamondSynth


@pytest.mark.parametrize("template", ["constraints.sdc", "constraints.ldc"])
def test_diamond_clock_constraint_uses_settings_clock(tmp_path: Path, template: str) -> None:
    design = Design.from_toml(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(clock_period=5.5), design, tmp_path)

    generated = flow.copy_from_template(template)

    # `main_clock` is the flow's reconciled physical clock: its `port` comes from the
    # design's clock port (`clk`, see design0.toml), and its period from the flow settings.
    main_clock = flow.settings.main_clock
    assert main_clock is not None
    assert main_clock.port == "clk"
    assert main_clock.period == 5.5

    assert (
        tmp_path / generated
    ).read_text() == "create_clock -period 5.500 -name main_clock [get_ports clk]"


def test_diamond_synth_without_clock_raises_flow_settings_exception(tmp_path: Path) -> None:
    design = Design.from_toml(Path(__file__).parent / "resources/design0/design0.toml")
    flow = DiamondSynth(DiamondSynth.Settings(), design, tmp_path)

    assert flow.settings.main_clock is None

    with pytest.raises(FlowSettingsException, match="diamond_synth needs a clock"):
        flow.run()
