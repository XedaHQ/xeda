"""Validation must never edit data the caller still owns.

v1 handed every `pre=True` validator a fresh mapping and re-validated (and so copied) nested
models. v2 passes the caller's own objects straight through, so the widespread "normalize by
writing back into `values`" pattern silently rewrote a design's `flow[...]` section, a
`Settings` kwargs dict, or a settings instance another flow was still using. The fix lives in
`xeda.dataclass` (plus copy-before-normalize in a few validators that hold nested models).
"""

import copy

import pytest

from xeda.dataclass import ConfigDict, XedaBaseModel, model_validator
from xeda.flow.synth import PhysicalClock
from xeda.flows.openroad import Openroad
from xeda.flows.yosys.yosys_fpga import YosysFpga

# ---------------------------------------------------------------------------------------------
# A `mode="before"` validator must not write into the caller's own mapping.
# v1 handed it a fresh dict; v2 passes the caller's object straight through.
# ---------------------------------------------------------------------------------------------


def test_before_validator_does_not_mutate_the_caller_mapping():
    payload = {"clocks": {"main_clock": {"freq": "200 MHz"}}}
    before = copy.deepcopy(payload)
    Openroad.Settings(platform="asap7", **payload)
    assert payload == before, "constructing settings rewrote the design's own clock mapping"


def test_before_validator_still_normalizes_its_own_copy():
    """The copy must not defeat the normalization it protects."""
    clock = PhysicalClock(freq="200 MHz")
    assert (clock.freq, clock.period) == (200.0, 5.0)


def test_shim_copies_before_validator_input():
    class M(XedaBaseModel):
        model_config = ConfigDict(extra="allow")

        @model_validator(mode="before")
        @classmethod
        def _norm(cls, values):
            values["added"] = True
            return values

    payload = {"a": 1}
    M(**payload)
    assert payload == {"a": 1}


def test_shim_copies_nested_before_validator_input():
    class M(XedaBaseModel):
        @model_validator(mode="before")
        @classmethod
        def _norm(cls, values):
            values["nested"]["added"] = True
            return values

        nested: dict

    payload = {"nested": {"original": True}}
    M(**payload)
    assert payload == {"nested": {"original": True}}


def test_shim_copies_input_during_nested_model_construction():
    """A nested model gets the containing field name even though this is not assignment."""

    class Child(XedaBaseModel):
        @model_validator(mode="before")
        @classmethod
        def _norm(cls, values):
            values["nested"]["added"] = True
            return values

        nested: dict

    class Parent(XedaBaseModel):
        child: Child

    payload = {"nested": {"original": True}}
    Parent(child=payload)
    assert payload == {"nested": {"original": True}}

    replacement = {"nested": {"replacement": True}}
    parent = Parent(child={"nested": {"initial": True}})
    parent.child = replacement
    assert replacement == {"nested": {"replacement": True}}


def test_before_model_validator_receives_assignment_context():
    seen_fields = []

    class M(XedaBaseModel):
        value: int

        @model_validator(mode="before")
        @classmethod
        def _record_context(cls, values, info):
            seen_fields.append(info.field_name)
            return values

    model = M(value=1)
    model.value = 2
    assert seen_fields == [None, "value"]


def test_nested_rtl_parameter_normalization_does_not_mutate_input(tmp_path, monkeypatch):
    """Nested rtl parameter normalization does not mutate input."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "abc.mem").write_text("00\n")  # a `file` parameter must exist
    payload = {"sources": [], "parameters": {"rom": {"file": "abc.mem"}}}
    before = copy.deepcopy(payload)

    from xeda.design import RtlSettings

    RtlSettings(**payload)
    assert payload == before


def test_rtl_parameter_assignment_does_not_mutate_input(tmp_path, monkeypatch):
    """Rtl parameter assignment does not mutate input."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "abc.mem").write_text("00\n")  # a `file` parameter must exist
    from xeda.design import RtlSettings

    rtl = RtlSettings(sources=[])
    parameters = {"rom": {"file": "abc.mem"}}
    before = copy.deepcopy(parameters)

    rtl.parameters = parameters

    assert parameters == before
    assert rtl.parameters is not parameters
    assert rtl.parameters["rom"].endswith("abc.mem")


def test_clock_shorthand_normalization_does_not_mutate_input():
    payload = {"clock": {"name": "c", "freq": 100}}
    before = copy.deepcopy(payload)

    settings = YosysFpga.Settings(**payload)
    assert payload == before
    assert settings.main_clock is not None and settings.main_clock.freq == 100


def test_clock_period_and_clock_combination_fails_without_mutating_input():
    payload = {"clock": {"name": "c", "freq": 100}, "clock_period": 5.0}
    before = copy.deepcopy(payload)

    from xeda.dataclass import ValidationError

    with pytest.raises(ValidationError, match="cannot be combined"):
        YosysFpga.Settings(**payload)
    assert payload == before


def test_asic_corner_normalization_does_not_mutate_input():
    from xeda.platforms.asics import AsicsPlatform

    payload = {
        "root_dir": ".",
        "sc_lef": "cells.lef",
        "corner": {"tt": {}},
        "lib_files": ["cells.lib"],
    }
    before = copy.deepcopy(payload)

    AsicsPlatform(**payload)
    assert payload == before


def test_unrelated_assignment_preserves_nested_field_identities():
    """Revalidating one field must not replace unrelated models or containers."""
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clock_period=10.0,
    )
    fpga = settings.fpga
    clock = settings.main_clock
    clocks = settings.clocks
    plugins = settings.plugins
    assert clock is not None

    settings.verbose = 1

    assert settings.fpga is fpga
    assert settings.main_clock is clock
    assert settings.clocks is clocks
    assert settings.plugins is plugins
    fpga.part = "LFE5U-45F-6BG381C"
    clock.period = 7.0
    plugins.append("plugin.so")
    assert settings.fpga.part == "LFE5U-45F-6BG381C"
    assert settings.main_clock.period == 7.0
    assert settings.plugins == ["plugin.so"]


# ---------------------------------------------------------------------------------------------
# A validator must not mutate a nested *model instance* the caller still owns.
# v1 re-validated (and so copied) nested models; v2 preserves their identity.
# ---------------------------------------------------------------------------------------------


def test_run_options_steps_are_not_expanded_in_the_caller_object():
    from xeda.flows.vivado.vivado_alt_synth import VivadoAltSynth
    from xeda.flows.vivado.vivado_synth import RunOptions

    run_options = RunOptions(strategy="Debug")
    before = dict(run_options.steps)

    settings = VivadoAltSynth.Settings(
        fpga="xc7a100tftg256-2L", synth=run_options, clock_period=5.0
    )

    assert dict(run_options.steps) == before, "caller's RunOptions.steps was expanded in place"
    assert settings.synth.steps, "the flow's own copy should still be expanded"


def test_docker_model_and_nested_containers_are_isolated_from_the_caller(tmp_path):
    from xeda.tool import Docker, Tool

    docker = Docker(
        image="img",
        command=["echo"],
        mounts={"caller": "container"},
        default_env={"A": "B"},
    )
    before = copy.deepcopy(docker.model_dump())
    tool = Tool(executable="echo", docker=docker, design_root_=tmp_path)

    assert docker.model_dump() == before
    assert tool.docker is not docker
    assert tool.docker.command is not docker.command
    assert tool.docker.mounts is not docker.mounts
    assert tool.docker.default_env is not docker.default_env
    assert tool.docker.command == ["echo"]
    assert tool.docker.mounts[str(tmp_path)] == str(tmp_path)

    tool.docker.command.append("arg")
    tool.docker.mounts["new"] = "mount"
    tool.docker.default_env["C"] = "D"
    assert docker.model_dump() == before


def test_derived_tool_does_not_share_docker_state_with_its_source():
    from xeda.tool import Docker, Tool

    source = Tool(
        executable="first",
        docker=Docker(
            image="img",
            command=["first"],
            mounts={"caller": "container"},
            default_env={"A": "B"},
        ),
    )

    derived = source.derive("second")

    assert source.docker.command == ["first"]
    assert derived.docker.command == ["second"]
    assert derived.docker is not source.docker
    assert derived.docker.mounts is not source.docker.mounts
    assert derived.docker.default_env is not source.docker.default_env

    derived.docker.mounts["new"] = "mount"
    derived.docker.default_env["C"] = "D"
    assert source.docker.mounts == {"caller": "container"}
    assert source.docker.default_env == {"A": "B"}


def test_netlist_flags_are_derived_when_used_not_stored():
    """The `netlist_*` switches imply `write_verilog` flags. Deriving them into the stored lists at
    construction meant a switch set later -- `Yosys.init` sets `netlist_expr` for a liberty
    netlist -- never reached the script, and re-validation had to guard against duplicates."""
    flags, unset_attributes = ["custom"], ["keep"]
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        netlist_verilog_flags=flags,
        netlist_unset_attributes=unset_attributes,
    )

    assert (settings.netlist_verilog_flags, settings.netlist_unset_attributes) == (
        flags,
        unset_attributes,
    )
    assert settings.write_verilog_flags() == ["custom", "-nodec"]
    assert settings.attributes_to_unset() == ["keep", "src"]

    settings.netlist_expr = False
    settings.netlist_src_attrs = True
    assert settings.write_verilog_flags() == ["custom", "-nodec", "-noexpr"]
    assert settings.attributes_to_unset() == ["keep"]


def test_openroad_corner_selection_uses_an_isolated_platform_copy():
    from xeda.platforms.asics import AsicsPlatform

    platform = AsicsPlatform.from_resource("asap7")
    original_corner = platform.default_corner
    original_vdd = platform.pwr_nets_voltages["VDD"]
    settings = Openroad.Settings(platform=platform, corner="FF", clock_period=5.0)

    assert settings.platform is not platform
    assert platform.default_corner == original_corner
    assert platform.pwr_nets_voltages["VDD"] == original_vdd
    assert settings.platform.default_corner == "FF"
    assert settings.platform.pwr_nets_voltages["VDD"] == 0.77

    selected_platform = settings.platform
    selected_voltages = settings.platform.pwr_nets_voltages
    settings.exit = False
    assert settings.platform is selected_platform
    assert settings.platform.pwr_nets_voltages is selected_voltages

    settings.corner = ["SS"]
    assert settings.platform is selected_platform
    assert settings.platform.default_corner == "SS"
    assert settings.platform.pwr_nets_voltages["VDD"] == 0.63
