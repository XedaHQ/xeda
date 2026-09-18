"""Files that have always loaded must keep loading.

TOML and YAML cannot mark a number as text, so real design and platform files spell string-valued
settings numerically (`speed = 2`, `MAX_BRAM = 0`, `compile_args = ["-j", 8]`). v1 coerced those
to `str`; v2 rejects them. `XedaBaseModel` restores the v1 coercion -- for scalars and for a
container's elements -- without disturbing annotations that genuinely discriminate on type.
"""

import pytest

from xeda.dataclass import Field, XedaBaseModel
from xeda.design import Design, DesignValidationError

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
