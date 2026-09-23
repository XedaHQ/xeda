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
    root = tmp_path / "design"
    root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", root)
    return Design(
        name="sqrt",
        design_root=root,
        rtl={"sources": ["sqrt.vhdl"], "top": "sqrt", "clock": {"port": "clk"}},
    )


def test_the_sweep_covers_the_fpga_flows():
    assert {"yosys_fpga", "nextpnr", "vivado_synth", "quartus", "ise_synth"} <= {
        cls.name for cls in FPGA_FLOWS
    }


@pytest.mark.parametrize("flow_class", FPGA_FLOWS, ids=lambda cls: cls.name)
def test_an_fpga_flow_launched_without_a_device_names_the_setting(
    flow_class, design, tmp_path, monkeypatch
):
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


def test_a_device_supplied_through_a_dependency_section_counts(design, tmp_path, monkeypatch):
    """`nextpnr` shares `fpga` with its `yosys` dependency (`dependency_settings`), and adopts
    one given only there -- so that satisfies it."""
    from xeda.flows import Nextpnr

    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            Nextpnr,
            design,
            {"clock": {"period": 10.0}, "yosys": {"fpga": {"part": "LFE5U-25F-6BG381C"}}},
        )
    assert "`fpga`" not in str(raised.value), "it got past the check, to the missing tool"


def test_a_dependency_s_own_section_reaches_the_flow_that_launches_it():
    """`[flows.yosys_fpga]` is the base of `nextpnr`'s `yosys` settings, which `[flows.nextpnr]`
    refines -- so a device given only for `yosys_fpga` is one `nextpnr` resolves (it shares
    `fpga` with that dependency), not one it learns of after its own `init()` already failed
    without it. Recursively: `openfpgaloader`'s `nextpnr.yosys` sits on it too."""
    from xeda.flow_runner.settings_layers import flow_settings_from_sections
    from xeda.flows import Nextpnr, Openfpgaloader

    sections = {
        "yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}, "abc9": False},
        "nextpnr": {"seed": 3, "yosys": {"abc9": True}},
    }
    composed = flow_settings_from_sections(Nextpnr, sections)
    assert composed == {
        "seed": 3,
        "yosys": {"fpga": {"part": "LFE5U-25F-6BG381C"}, "abc9": True},  # the more specific wins
    }
    assert flow_settings_from_sections(Openfpgaloader, sections)["nextpnr"]["yosys"]["fpga"] == {
        "part": "LFE5U-25F-6BG381C"
    }
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
