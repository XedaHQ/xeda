"""A flow that cannot run without a setting says so when it is launched, naming the setting and
how to give it -- not from inside `run()` (`FlowFatalException FPGA target device not
specified`), from a template (`'None' has no attribute 'part'`), or from the tool.

The check belongs to the launch, not to the settings model: one section of a design file holds
only some of a flow's settings, so a model that insisted on all of them could not validate it.
"""

import shutil
from pathlib import Path
from typing import Optional

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.dataclass import Field
from xeda.flow import Flow, FlowSettingsException, FpgaSynthFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner

from .settings_samples import flow_classes

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


def test_a_producer_s_own_section_is_no_part_of_the_consumer_s_settings():
    """A producer's settings are written under its own section only: a flow's section is its
    own, and its producers' sections are theirs. The device given only for `yosys_fpga` reaches
    `nextpnr` along their declared edge, in the resolver."""
    from xeda.flow_runner.settings_layers import compose_flow_settings
    from xeda.flows import Nextpnr, Openfpgaloader

    sections = {
        "yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}, "abc9": False},
        "nextpnr": {"seed": 3},
    }
    assert compose_flow_settings(Nextpnr, [sections]) == sections["nextpnr"]
    assert compose_flow_settings(Openfpgaloader, [sections]) == {}
    assert compose_flow_settings(Nextpnr, [{}]) == {}


def test_a_device_given_only_for_the_synthesis_dependency_is_enough(design, tmp_path, monkeypatch):
    """`fpga` given only in `[flows.yosys_fpga]` reaches `nextpnr`, which has no device of its
    own: it must not die on a missing one after yosys has run."""
    from xeda.flows import Nextpnr

    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    design.flow = {"yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}}}
    with pytest.raises(Exception) as raised:
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run(
            Nextpnr, design, flow_overrides={"clock": {"period": 10.0}}
        )
    assert "`fpga`" not in str(raised.value), "it got past the check, to the missing tool"


def test_a_settings_field_without_a_default_is_refused_when_the_class_is_defined():
    """A setting a flow cannot run without is a `required_settings` entry, never a model field
    that has no default: the refusal names the field and where to declare it, and the flow is
    not registered."""
    before = dict(registered_flows)
    with pytest.raises(TypeError, match=r"`foo`.*`Flow.required_settings`") as refused:

        class _NeedsFoo(Flow):
            """Requires a field in its settings model."""

            results_description = {}

            class Settings(Flow.Settings):
                foo: str = Field(description="A setting without a default.")

            def run(self) -> None:
                pass

    assert "_NeedsFoo.Settings" in str(refused.value)
    assert registered_flows == before


def test_an_optional_setting_without_an_explicit_default_is_refused_too():
    """`Optional[X]` is no default: pydantic makes such a field required."""
    with pytest.raises(TypeError, match=r"`bar`.*`Flow.required_settings`"):

        class _NeedsBar(Flow):
            """Requires a field it meant to leave optional."""

            results_description = {}

            class Settings(Flow.Settings):
                bar: Optional[str] = Field(description="Meant to be optional.")

            def run(self) -> None:
                pass


def test_a_settings_class_that_adds_a_field_without_a_default_is_refused():
    with pytest.raises(TypeError, match=r"`extra`.*`Flow.required_settings`"):

        class _Extended(Flow.Settings):
            extra: int = Field(description="Added by a subclass of the base settings.")


def test_the_settings_of_every_flow_have_a_default_for_each_field():
    for cls, _name in flow_classes():
        required = [name for name, info in cls.Settings.model_fields.items() if info.is_required()]
        assert not required, f"{cls.name}: {required}"
