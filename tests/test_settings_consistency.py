"""Settings that describe one thing must keep agreeing, after construction *and* assignment.

`clock`/`clocks` and the legacy `clock_period` input, `generics` and `parameters`, and a platform's selected corner and
its supply voltages are each correlated state. (A flow's settings and its dependency's are not:
they are combined only when the dependency is launched; see `test_dependency_settings.py`.) With
`validate_assignment`, pydantic 2 re-runs a `mode="before"` model validator on every assignment:
the assigned field keeps its raw value, and every *other* field the validator rewrites is written
back. So a validator has to treat the field being assigned as authoritative, and must not
re-impose anything on an assignment that does not concern it.
"""

import copy
import json
from pathlib import Path

import pytest

from xeda.dataclass import ValidationError
from xeda.design import Design, RtlSettings
from xeda.flow.synth import PhysicalClock
from xeda.flows.yosys.yosys_fpga import YosysFpga

# ---------------------------------------------------------------------------------------------
# Clocks: `clock`/`clocks` are canonical; `clock_period` is input-only compatibility syntax.
# ---------------------------------------------------------------------------------------------


def test_canonical_clock_is_stored_once_and_legacy_period_is_derived():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clock={"name": "main_clock", "period": 10.0},
    )
    assert settings.main_clock is not None
    assert settings.clock is settings.main_clock
    assert settings.clock_period == settings.main_clock.period == 10.0
    assert "clock" not in settings.model_dump()
    assert "clock_period" not in settings.model_dump()

    settings.clock_period = 4.0
    assert (settings.clock_period, settings.main_clock.period) == (4.0, 4.0)

    clocks = {"main_clock": {"period": 8.0}}
    before = copy.deepcopy(clocks)
    settings.clocks = clocks
    assert clocks == before
    assert settings.main_clock is not None
    assert (settings.clock_period, settings.main_clock.period) == (8.0, 8.0)


def test_legacy_clock_period_cannot_be_combined_with_canonical_clock():
    with pytest.raises(ValidationError, match="cannot be combined"):
        YosysFpga.Settings(
            fpga={"part": "LFE5U-25F-6BG381C"},
            clock={"name": "main_clock", "freq": 100.0},
            clock_period=5.0,
        )


def test_main_clock_falls_back_deterministically_and_period_assignment_targets_it():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"clk_a": {"freq": 100.0}, "clk_b": {"freq": 50.0}},
    )

    assert settings.main_clock is settings.clocks["clk_a"]
    assert settings.clock is settings.clocks["clk_a"]
    assert settings.clock_period == pytest.approx(10.0)
    settings.clock_period = 5.0
    assert settings.clock_period == pytest.approx(5.0)
    assert settings.clocks["clk_a"].period == pytest.approx(5.0)
    assert settings.clocks["clk_b"].period == pytest.approx(20.0)
    assert set(settings.clocks) == {"clk_a", "clk_b"}


def test_assigning_none_to_legacy_clock_period_preserves_clocks():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"clk_a": {"freq": 100.0}, "clk_b": {"freq": 50.0}},
    )

    settings.clock_period = None

    assert set(settings.clocks) == {"clk_a", "clk_b"}


def test_physical_clock_assignment_keeps_frequency_and_period_consistent():
    clock = PhysicalClock(period=10.0)

    clock.freq = 200.0
    assert (clock.freq, clock.period) == (200.0, 5.0)

    clock.period = 4.0
    assert (clock.freq, clock.period) == (250.0, 4.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "clk"),
        ("rise", 0.5),
        ("duty_cycle", 0.4),
        ("uncertainty", 0.1),
        ("skew", 0.2),
        ("port", "clk_i"),
    ],
)
def test_unrelated_clock_assignment_does_not_reconcile_rounded_frequency(field, value):
    clock = PhysicalClock(freq=300.0)
    pair = (clock.freq, clock.period)

    setattr(clock, field, value)

    assert (clock.freq, clock.period) == pair


def test_a_directly_edited_clock_is_not_reverted_by_an_unrelated_assignment():
    """Editing canonical clock state must not be reverted by unrelated assignment.

    The settings-wide `mode="before"` validator runs on *every* assignment, so re-imposing
    `clock_period` there silently undid an edit made through `settings.clocks`.
    """
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"main_clock": {"period": 10.0}},
    )
    assert settings.main_clock is not None
    settings.main_clock.period = 7.0

    settings.verbose = 1

    assert settings.main_clock.period == 7.0


def test_rtl_clock_list_is_not_replaced_by_an_unrelated_assignment():
    rtl = RtlSettings(sources=[], clocks=[{"name": "clk", "port": "clk_i"}])
    clocks = rtl.clocks
    clock = rtl.clock

    rtl.top = "top"

    assert rtl.clocks is clocks
    assert rtl.clock is clock


def test_rtl_clock_is_stored_once_and_compatibility_values_are_derived():
    rtl = RtlSettings(sources=[], clock_port="clk_i")

    assert rtl.clock is rtl.clocks[0]
    assert rtl.clock_port == "clk_i"
    assert set(rtl.model_dump()).isdisjoint({"clock", "clock_port"})

    rtl.clocks = [{"name": "replacement", "port": "clk_r"}]
    assert rtl.clock is rtl.clocks[0]
    assert rtl.clock_port == "clk_r"


def test_empty_legacy_rtl_clock_port_still_means_no_declared_clock():
    rtl = RtlSettings(sources=[], clock_port="")

    assert rtl.clocks == []
    assert rtl.clock is None
    assert rtl.clock_port is None


@pytest.mark.parametrize(
    "values",
    [
        {"clock": {"port": "a"}, "clock_port": "b"},
        {"clock": {"port": "a"}, "clocks": [{"port": "b"}]},
        {"clock_port": "a", "clocks": [{"port": "b"}]},
    ],
)
def test_rtl_clock_spellings_in_one_input_are_rejected(values):
    with pytest.raises(ValidationError, match="Specify only one"):
        RtlSettings(sources=[], **values)


def test_rtl_single_clock_compatibility_accessors_use_the_first_clock():
    rtl = RtlSettings(
        sources=[],
        clocks=[{"name": "a", "port": "clk_a"}, {"name": "b", "port": "clk_b"}],
    )

    assert rtl.clock is not None
    assert rtl.clock.port == "clk_a"
    assert rtl.clock_port == "clk_a"

    rtl.clock_port = "x"
    assert len(rtl.clocks) == 1
    assert rtl.clocks[0].port == "x"


@pytest.mark.parametrize(
    ("field", "value", "port"),
    [
        ("clock", {"name": "replacement", "port": "clk_a"}, "clk_a"),
        ("clock_port", "clk_b", "clk_b"),
    ],
)
def test_assigning_rtl_clock_shorthand_updates_the_clock_list(field, value, port):
    rtl = RtlSettings(sources=[], clocks=[{"name": "old", "port": "old_clk"}])

    setattr(rtl, field, value)

    assert rtl.clock is not None
    assert rtl.clock.port == port
    assert len(rtl.clocks) == 1
    assert rtl.clocks[0].port == port


# ---------------------------------------------------------------------------------------------
# ASIC platforms: supply voltages written as `$(VOLTAGE)` follow the selected corner.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rebuild",
    [
        pytest.param(lambda p: type(p).model_validate(p.model_dump()), id="model_validate"),
        pytest.param(lambda p: p.model_copy(deep=True), id="model_copy"),
        pytest.param(
            lambda p: type(p).model_validate(json.loads(p.model_dump_json())), id="json_round_trip"
        ),
        pytest.param(lambda p: p.with_absolute_paths(), id="with_absolute_paths"),
    ],
)
def test_corner_dependent_voltages_survive_a_platform_rebuild(rebuild):
    """ASAP7 spells VDD as `$(VOLTAGE)`, which only the selected corner can resolve.

    The source expression has to outlive every way a platform gets reconstructed -- reloaded
    from `settings.json`, deep-copied into flow settings, or rebuilt by `with_absolute_paths` --
    or `select_corner` quietly keeps the voltage of whichever corner happened to load first.
    """
    from xeda.platforms.asics import AsicsPlatform

    platform = rebuild(AsicsPlatform.from_resource("asap7"))
    assert platform.pwr_nets_voltages["VDD"] == 0.7  # TT, the platform default

    platform.select_corner("FF")

    assert platform.default_corner == "FF"
    assert platform.pwr_nets_voltages["VDD"] == 0.77


def test_select_corner_walks_every_bundled_asap7_corner():
    from xeda.platforms.asics import AsicsPlatform

    platform = AsicsPlatform.from_resource("asap7")
    for corner, voltage in (("FF", 0.77), ("SS", 0.63), ("TT", 0.7), ("FF", 0.77)):
        platform.select_corner(corner)
        assert (platform.default_corner, platform.pwr_nets_voltages["VDD"]) == (corner, voltage)


def test_select_corner_names_the_available_corners_when_asked_for_a_missing_one():
    from xeda.platforms.asics import AsicsPlatform

    platform = AsicsPlatform.from_resource("asap7")
    with pytest.raises(ValueError, match="Unknown platform corner: typo"):
        platform.select_corner("typo")


def test_a_corner_free_platform_keeps_its_literal_voltages():
    """nangate45 writes `pwr_nets_voltages = "VDD 1.1"`: nothing to re-evaluate."""
    from xeda.platforms.asics import AsicsPlatform

    platform = AsicsPlatform.from_resource("nangate45")

    assert platform.voltage_expressions_ == {}
    assert platform.pwr_nets_voltages == {"VDD": 1.1}


# ---------------------------------------------------------------------------------------------
# `generics` and `parameters` are two names of one setting, stored once (`parameters`).
# ---------------------------------------------------------------------------------------------


def test_generics_is_the_same_setting_stored_once_as_parameters():
    rtl = RtlSettings(sources=[], generics={"WIDTH": 8})

    assert rtl.parameters == rtl.generics == {"WIDTH": 8}
    assert "generics" not in rtl.model_dump(), "one setting, one stored value"


def test_giving_both_names_at_once_is_an_error_not_a_silent_choice():
    with pytest.raises(ValidationError):
        RtlSettings(sources=[], generics={"WIDTH": 8}, parameters={"WIDTH": 16})


def test_the_design_schema_accepts_both_names():
    from xeda.introspect import design_schema

    schema = design_schema()
    for section in ("RtlSettings", "TbSettings"):
        properties = schema["$defs"][section]["properties"]
        assert {"parameters", "generics"} <= set(properties), section
    assert {"parameters", "generics"} <= set(schema["properties"]), "flat design form"


def test_flat_design_input_accepts_either_parameters_name(tmp_path):
    for spelling in ("parameters", "generics"):
        design = Design(
            name="d",
            design_root=tmp_path,
            sources=[],
            top="top",
            **{spelling: {"WIDTH": 8}},
        )
        assert design.rtl.parameters == {"WIDTH": 8}


def test_flat_design_input_rejects_both_parameters_names(tmp_path):
    from xeda.design import DesignValidationError

    with pytest.raises(DesignValidationError, match="generics"):
        Design(
            name="d",
            design_root=tmp_path,
            sources=[],
            top="top",
            parameters={"WIDTH": 8},
            generics={"WIDTH": 16},
        )


def test_design_construction_does_not_modify_nested_input(tmp_path):
    data = {
        "name": "d",
        "rtl": {
            "sources": [{"path": "generated.v", "type": "Verilog"}],
            "top": "top",
            "parameters": {"MEMORY": {"path": "memory.hex"}},
        },
    }
    before = copy.deepcopy(data)

    Design(design_root=tmp_path, **data)

    assert data == before


@pytest.mark.parametrize("spelling", ["generics", "parameters"])
def test_assigning_either_spelling_updates_both(spelling):
    from xeda.design import RtlSettings

    rtl = RtlSettings(sources=[], parameters={"WIDTH": 8})

    setattr(rtl, spelling, {"WIDTH": 16})
    assert (rtl.generics, rtl.parameters) == ({"WIDTH": 16}, {"WIDTH": 16})

    setattr(rtl, spelling, {})
    assert (rtl.generics, rtl.parameters) == ({}, {})


def test_copying_generics_between_rtl_and_tb_reaches_the_parameters_spelling():
    """What `GhdlSim` and `Nvc` do for a cocotb testbench.

    `nvc` builds its `-g` elaboration flags from `tb.parameters`, while both flows assign
    `tb.generics`, so leaving the sibling stale dropped every generic from the elaboration.
    """
    design = Design.from_toml(
        Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.toml"
    )
    design.rtl.parameters = {"G_IN_WIDTH": 32}
    design.tb.parameters = {"G_IN_WIDTH": 8}  # the testbench's own, about to be superseded

    design.tb.generics = design.rtl.generics

    assert design.tb.generics == {"G_IN_WIDTH": 32}
    assert design.tb.parameters == {"G_IN_WIDTH": 32}


# ---------------------------------------------------------------------------------------------
# `cached_property` stores into a pydantic model's `__dict__`, so a copy inherits stale values.
# ---------------------------------------------------------------------------------------------


def test_a_shared_docker_image_is_named_after_each_tool_that_borrows_it():
    from xeda.tool import Docker, Tool

    shared = Docker(image="img")  # type: ignore[call-arg]
    assert shared.name == "???"  # caches the placeholder, since there is no command yet

    tool = Tool(executable="ghdl", docker=shared)  # type: ignore[call-arg]

    assert tool.docker is not None and tool.docker.name == "ghdl"
    assert shared.command == [], "the caller's own Docker was edited"


def test_deriving_a_sibling_tool_renames_its_container():
    from xeda.tool import Docker, Tool

    tool = Tool(  # type: ignore[call-arg]
        executable="ghdl",
        docker=Docker(image="img", command=["/opt/oss-cad-suite/bin/ghdl"]),  # type: ignore[call-arg]
    )
    assert tool.docker is not None and tool.docker.name == "ghdl"

    derived = tool.derive("nvc")

    assert derived.docker is not None and derived.docker.name == "nvc"
