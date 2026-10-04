"""A flow that cannot run without a setting says so when it is launched, naming the setting and
how to give it -- not from inside `run()` (`FlowFatalException FPGA target device not
specified`), from a template (`'None' has no attribute 'part'`), or from the tool.

The check belongs to the launch, not to the settings model: the same settings also sit inside
another flow's settings as a dependency's, where the launching flow supplies what they lack
(`dependency_settings`), so a model that insisted on them could not even be nested.
"""

import shutil
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.flow import FlowSettingsException, FpgaSynthFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"

FPGA_FLOWS = sorted(
    {cls for _, cls in registered_flows.values() if issubclass(cls, FpgaSynthFlow)},
    key=lambda cls: cls.name,
)


@pytest.fixture
def design(tmp_path):
    """Provide a design for required settings tests."""
    root = tmp_path / "design"
    root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", root)
    return Design(
        name="sqrt",
        design_root=root,
        rtl={"sources": ["sqrt.vhdl"], "top": "sqrt", "clock": {"port": "clk"}},
    )


def test_the_sweep_covers_the_fpga_flows():
    """The sweep covers the fpga flows."""
    assert {"yosys_fpga", "nextpnr", "vivado_synth", "quartus", "ise_synth"} <= {
        cls.name for cls in FPGA_FLOWS
    }


@pytest.mark.parametrize("flow_class", FPGA_FLOWS, ids=lambda cls: cls.name)
def test_an_fpga_flow_launched_without_a_device_names_the_setting(
    flow_class, design, tmp_path, monkeypatch
):
    """An fpga flow launched without a device names the setting."""
    monkeypatch.setenv("PATH", "")  # a tool that ran would be `ExecutableNotFound`, not this
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "xeda_run"

    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(run_dir, display_results=False).run_flow(
            flow_class, design, {"clock": {"period": 10.0}}
        )

    message = str(raised.value)
    assert flow_class.name in message and "`fpga`" in message, message
    assert "fpga.part" in message, "it says how to give it"
    assert not list(run_dir.rglob("settings.json")), "and nothing was set up for the run"


def test_a_device_supplied_through_a_producer_section_counts(design, tmp_path, monkeypatch):
    """`nextpnr` shares `fpga` with the `yosys_fpga` that makes its netlist, along their declared
    edge, and adopts one given only in `flows.yosys_fpga` -- so that satisfies it."""
    from xeda.flows import Nextpnr

    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    sections = {"yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}}}
    plan = runner.resolve(Nextpnr, design, {"clock": {"period": 10.0}}, sections)
    assert plan.node("nextpnr").settings.fpga.part == "LFE5U-25F-6BG381C"
    with pytest.raises(Exception) as raised:
        runner.run_flow(Nextpnr, design, {"clock": {"period": 10.0}}, all_flows_settings=sections)
    assert "`fpga`" not in str(raised.value), "it got past the check, to the missing tool"
    assert "was removed" not in str(raised.value)


def test_a_dependency_s_own_section_reaches_the_flow_that_launches_it():
    """A declared producer's settings are written under its own section only (D-10): a flow's
    section is its own, and its producers' sections are theirs. The device given only for
    `yosys_fpga` reaches `nextpnr` along their declared edge, in the resolver."""
    from xeda.flow_runner.settings_layers import flow_settings_from_sections
    from xeda.flows import Nextpnr, Openfpgaloader

    sections = {
        "yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}, "abc9": False},
        "nextpnr": {"seed": 3},
    }
    assert flow_settings_from_sections(Nextpnr, sections) == sections["nextpnr"]
    assert flow_settings_from_sections(Openfpgaloader, sections) == {}
    assert flow_settings_from_sections(Nextpnr, {}) == {}


def test_a_device_given_only_for_the_synthesis_dependency_is_enough(design, tmp_path, monkeypatch):
    """The route that used to fail: `fpga` only in `[flows.yosys_fpga]`. `nextpnr` got past
    no check -- yosys ran, then `nextpnr` died on `assert ss.fpga is not None`."""
    from xeda.flows import Nextpnr

    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    design.flow = {"yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}}}
    with pytest.raises(Exception) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run(
            Nextpnr, design, flow_overrides={"clock": {"period": 10.0}}
        )
    assert "`fpga`" not in str(raised.value), "it got past the check, to the missing tool"
