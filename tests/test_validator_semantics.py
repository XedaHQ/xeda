"""Validator behaviours pydantic v1 provided implicitly and v2 does not.

Each test here corresponds to a way the v2 migration silently changed behaviour. They are
grouped by hazard rather than by module, because the fixes live in `xeda.dataclass` and apply to
every validator in the codebase.
"""

import copy

import pytest

from xeda.dataclass import ConfigDict, Field, XedaBaseModel, field_validator, model_validator
from xeda.design import Design, DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flow.synth import PhysicalClock
from xeda.flows.openroad import Openroad
from xeda.flows.yosys.yosys_fpga import YosysFpga

BAD_VALUES = [123, 4.5, True, object()]


# ---------------------------------------------------------------------------------------------
# A TypeError raised inside a validator must surface as a validation error, not a traceback.
# v1 treated TypeError like ValueError; v2 lets it escape `model_validate` untouched.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES, ids=lambda v: type(v).__name__)
def test_scalar_sources_is_a_validation_error(bad):
    """`rtl.sources = 123` used to escape as `TypeError: 'int' object is not iterable`."""
    with pytest.raises(DesignValidationError):
        Design(name="d", rtl={"sources": bad, "top": "t"})


def test_scalar_sources_message_names_the_field_and_the_type():
    with pytest.raises(DesignValidationError, match=r"'sources' must be a list"):
        Design(name="d", rtl={"sources": 123, "top": "t"})


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
    ],
    ids=repr,
)
def test_non_numeric_clock_is_a_validation_error(bad):
    """`PhysicalClock(freq=[])` used to escape as a raw `TypeError` from `float()`."""
    with pytest.raises((ValueError, FlowSettingsError)):
        PhysicalClock(freq=bad)


@pytest.mark.parametrize("bad", ["x", "5 furlongs", "abc"], ids=repr)
def test_unparseable_unit_is_a_validation_error(bad):
    """pint raises `UndefinedUnitError` (an `AttributeError`) and `DimensionalityError` (a
    `TypeError`); neither is a validation failure to pydantic v2, so a bad unit in a design file
    reached the user as a raw traceback."""
    with pytest.raises((ValueError, FlowSettingsError)):
        PhysicalClock(freq=bad)


def test_valid_units_still_convert():
    clock = PhysicalClock(freq="200 MHz")
    assert (clock.freq, clock.period) == (200.0, 5.0)
    assert PhysicalClock(period="5.5ns").period == 5.5


@pytest.mark.parametrize("bad", BAD_VALUES, ids=lambda v: type(v).__name__)
def test_scalar_verilog_lib_is_a_validation_error(bad):
    with pytest.raises((FlowSettingsError, ValueError)):
        YosysFpga.Settings(verilog_lib=bad)


def test_type_error_in_any_validator_becomes_a_validation_error():
    """The shim-level safety net, exercised directly."""

    class M(XedaBaseModel):
        x: int = 0

        @field_validator("x", mode="before")
        @classmethod
        def _boom(cls, value):
            raise TypeError("deliberate")

    with pytest.raises(ValueError, match="deliberate"):
        M(x=1)


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


def test_nested_rtl_parameter_normalization_does_not_mutate_input():
    payload = {"sources": [], "parameters": {"rom": {"file": "abc.mem"}}}
    before = copy.deepcopy(payload)

    from xeda.design import RtlSettings

    RtlSettings(**payload)
    assert payload == before


def test_clock_shorthand_normalization_does_not_mutate_input():
    payload = {"clock": {"name": "c", "freq": 100}, "clock_period": 5.0}
    before = copy.deepcopy(payload)

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


def test_nested_settings_instance_is_not_mutated_by_a_dependent_flow():
    from xeda.flows.nextpnr import Nextpnr

    yosys = YosysFpga.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, clock_period=10.0)
    original = (yosys.fpga.part, yosys.clock_period)

    nextpnr = Nextpnr.Settings(yosys=yosys, fpga={"part": "LFE5U-45F-6BG381C"}, clock_period=5.0)

    assert (yosys.fpga.part, yosys.clock_period) == original, "caller's Yosys settings were edited"
    assert nextpnr.yosys is not yosys
    assert nextpnr.yosys.fpga.part == "LFE5U-45F-6BG381C"


def test_run_options_steps_are_not_expanded_in_the_caller_object():
    from xeda.flows.vivado.vivado_alt_synth import VivadoAltSynth
    from xeda.flows.vivado.vivado_synth import RunOptions

    run_options = RunOptions(strategy="Debug")
    before = dict(run_options.steps)

    settings = VivadoAltSynth.Settings(synth=run_options, clock_period=5.0)

    assert dict(run_options.steps) == before, "caller's RunOptions.steps was expanded in place"
    assert settings.synth.steps, "the flow's own copy should still be expanded"


def test_docker_command_is_not_written_into_a_caller_owned_instance():
    from xeda.tool import Docker, Tool

    docker = Docker(image="img")
    tool = Tool(executable="echo", docker=docker)

    assert not docker.command, "caller's Docker.command was populated"
    assert tool.docker.command == ["echo"]


# ---------------------------------------------------------------------------------------------
# Type coercion that v1 performed and v2 does not. TOML cannot mark a number as text, so files
# that have always loaded must keep loading.
# ---------------------------------------------------------------------------------------------


def test_numeric_fpga_speed_grade_is_accepted_as_a_string():
    """Speed grades really are written as bare numbers (`speed = 2`)."""
    from xeda.flow.fpga import FPGA

    assert FPGA(part="xc7a100t", speed=2).speed == "2"
    assert FPGA(part="xc7a100t", grade=1).grade == "1"


def test_numeric_design_name_is_accepted_as_a_string():
    assert Design(name=2024, rtl={"sources": [], "top": "t"}).name == "2024"


def test_booleans_are_not_silently_renamed():
    """`bool` is an `int` subclass; `True` is not a meaningful name."""
    with pytest.raises(DesignValidationError):
        Design(name=True, rtl={"sources": [], "top": "t"})


def test_a_union_of_str_and_number_still_discriminates():
    """The coercion must only fire where `str` is the *only* accepted scalar."""
    from xeda.flow.sim import SimFlow

    assert isinstance(SimFlow.Settings(stop_time=5).stop_time, int)


def test_fractional_platform_values_are_not_truncated():
    """`int`-typed physical quantities silently truncated PDK data under v1 and broke v2."""
    from xeda.platforms.asics import AsicsPlatform

    try:
        platform = AsicsPlatform.from_resource("nangate45")
    except (FileNotFoundError, ModuleNotFoundError):
        pytest.skip("nangate45 not available")
    assert platform.macro_place_halo == [22.4, 15.12]


# ---------------------------------------------------------------------------------------------
# The same coercion inside containers. v1 coerced each element of a `List[str]` / the values of a
# `Dict[str, str]`; v2 rejects the whole field. TOML/YAML parse a bare number as a number, and
# these fields routinely carry numeric-looking content.
# ---------------------------------------------------------------------------------------------


def test_numeric_tcl_property_values_are_accepted():
    """`set_property MAX_BRAM {0}` -- a Vivado property value is very often a bare number."""
    from xeda.flows.vivado.vivado_synth import VivadoSynth

    settings = VivadoSynth.Settings(
        set_synth_properties={"MAX_BRAM": 0, "MAX_DSP": 0}, clock_period=5.0
    )
    assert settings.set_synth_properties == {"MAX_BRAM": "0", "MAX_DSP": "0"}


def test_numeric_tool_arguments_are_accepted():
    """`compile_args = ["-j", 8]` is the natural way to write a job count in TOML."""
    from xeda.flows.verilator import Verilator

    assert Verilator.Settings(compile_args=["-j", 8]).compile_args == ["-j", "8"]


def test_numeric_list_items_are_accepted():
    from xeda.flows.vivado.vivado_synth import VivadoSynth

    assert VivadoSynth.Settings(suppress_msgs=[8, 7078], clock_period=5.0).suppress_msgs == [
        "8",
        "7078",
    ]


def test_container_coercion_leaves_discriminating_annotations_alone():
    """Only a container whose element type is *str-only* may be coerced."""

    class M(XedaBaseModel):
        str_items: list[str] = Field(default_factory=list)
        union_items: list[str | int] = Field(default_factory=list)
        str_values: dict[str, str] = Field(default_factory=dict)
        int_values: dict[str, int] = Field(default_factory=dict)
        int_items: list[int] = Field(default_factory=list)
        optional_items: list[str] | None = None

    m = M(
        str_items=[1, 2],
        union_items=[1, "x"],
        str_values={"k": 0},
        int_values={"k": 1},
        int_items=[1, 2],
        optional_items=[3],
    )
    assert m.str_items == ["1", "2"]
    assert m.union_items == [1, "x"]
    assert m.str_values == {"k": "0"}
    assert m.int_values == {"k": 1}
    assert m.int_items == [1, 2]
    assert m.optional_items == ["3"]


def test_booleans_inside_a_string_list_are_still_rejected():
    class M(XedaBaseModel):
        items: list[str] = Field(default_factory=list)

    with pytest.raises(ValueError):
        M(items=[True])
