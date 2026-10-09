"""Settings that describe one thing must keep agreeing, after construction *and* assignment.

`clock`/`clocks` and the legacy `clock_period` input, `generics` and `parameters`, and a platform's selected corner and
its supply voltages are each correlated state. With
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
# Clocks: `clocks` is stored; `clock` is input syntax for the one clock, and a property.
# ---------------------------------------------------------------------------------------------


def test_canonical_clock_is_stored_once():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clock={"name": "main_clock", "period": 10.0},
    )
    assert settings.main_clock is not None
    assert settings.clock is settings.main_clock
    assert settings.clock.period == 10.0
    assert "clock" not in settings.model_dump()
    assert "clock_period" not in settings.model_dump()

    settings.clock.period = 4.0
    assert settings.main_clock.period == 4.0

    clocks = {"main_clock": {"period": 8.0}}
    before = copy.deepcopy(clocks)
    settings.clocks = clocks
    assert clocks == before
    assert settings.main_clock is not None
    assert settings.main_clock.period == 8.0


def test_clock_period_was_removed_whatever_it_is_combined_with():
    for extra in ({}, {"clock": {"name": "main_clock", "freq": 100.0}}):
        with pytest.raises(ValidationError, match="`clock_period` was removed: use `clock.period`"):
            YosysFpga.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, clock_period=5.0, **extra)


def test_main_clock_falls_back_deterministically():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"clk_a": {"freq": 100.0}, "clk_b": {"freq": 50.0}},
    )

    assert settings.main_clock is settings.clocks["clk_a"]
    assert settings.clock is settings.clocks["clk_a"]
    assert settings.clock.period == pytest.approx(10.0)
    settings.clock.period = 5.0
    assert settings.clocks["clk_a"].period == pytest.approx(5.0)
    assert settings.clocks["clk_b"].period == pytest.approx(20.0)
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
    `clock.period` there silently undid an edit made through `settings.clocks`.
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


def test_rtl_clock_is_stored_once_and_the_shorthand_is_derived():
    rtl = RtlSettings(sources=[], clock="clk_i")

    assert rtl.clock is rtl.clocks[0]
    assert rtl.clock.port == "clk_i"
    assert set(rtl.model_dump()).isdisjoint({"clock", "clock_port"})

    rtl.clocks = [{"name": "replacement", "port": "clk_r"}]
    assert rtl.clock is rtl.clocks[0]
    assert rtl.clock.port == "clk_r"


@pytest.mark.parametrize("value", ["clk", "", None, {"port": "clk"}])
def test_rtl_clock_port_was_removed(value):
    with pytest.raises(ValidationError, match="`clock_port` was removed: use `clock: <port>`"):
        RtlSettings(sources=[], clock_port=value)


def test_rtl_clock_spellings_in_one_input_are_rejected():
    with pytest.raises(ValidationError, match="Specify only one"):
        RtlSettings(sources=[], clock={"port": "a"}, clocks=[{"port": "b"}])


def test_rtl_clock_port_was_removed_also_beside_the_other_spellings():
    with pytest.raises(ValidationError, match="`clock_port` was removed"):
        RtlSettings(sources=[], clock={"port": "a"}, clock_port="b")


def test_rtl_single_clock_accessor_uses_the_first_clock():
    rtl = RtlSettings(
        sources=[],
        clocks=[{"name": "a", "port": "clk_a"}, {"name": "b", "port": "clk_b"}],
    )

    assert rtl.clock is not None
    assert rtl.clock.port == "clk_a"

    rtl.clock = "x"
    assert len(rtl.clocks) == 1
    assert rtl.clocks[0].port == "x"


@pytest.mark.parametrize(
    ("value", "port"),
    [({"name": "replacement", "port": "clk_a"}, "clk_a"), ("clk_b", "clk_b")],
)
def test_assigning_rtl_clock_shorthand_updates_the_clock_list(value, port):
    rtl = RtlSettings(sources=[], clocks=[{"name": "old", "port": "old_clk"}])

    rtl.clock = value

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
    design = Design.from_file(
        Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
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


# ---------------------------------------------------------------------------------------------
# Shared leaves: a setting two connected flows agree on is one setting, of one type.
# ---------------------------------------------------------------------------------------------


def _product_flows():
    from xeda.flow import registered_flows

    return {
        cls.name: cls
        for _, cls in registered_flows.values()
        if cls.__module__.startswith("xeda.flows.")
    }


def test_every_shared_setting_has_one_type_in_every_flow_that_declares_it():
    """`resolver.SHARED_SETTINGS` agree along declared edges and are propagated as given, so a
    value one flow accepts the other must accept too: the same annotation wherever one is
    declared. (`openroad`'s `platform` was a required `AsicsPlatform` where `yosys`'s and `dc`'s
    is optional: a setting a flow cannot run without is its `required_settings`, never required
    on the model.)"""
    from xeda.flow_runner.resolver import SHARED_SETTINGS

    assert {"platform", "corner", "dont_use_cells"} <= set(SHARED_SETTINGS)
    flows = _product_flows()
    retyped = {}
    for name in SHARED_SETTINGS:
        annotations = {
            flow: cls.Settings.model_fields[name].annotation
            for flow, cls in flows.items()
            if name in cls.Settings.model_fields
        }
        if len(set(map(str, annotations.values()))) > 1:
            retyped[name] = annotations
    assert not retyped


def test_corner_and_dont_use_cells_are_yosys_s_and_openroad_s_alone():
    """Containment: `yosys -> openroad` is the only edge on which `corner` or
    `dont_use_cells` can agree, because no other product flow declares either."""
    flows = _product_flows()
    for name in ("corner", "dont_use_cells"):
        holders = {flow for flow, cls in flows.items() if name in cls.Settings.model_fields}
        assert holders == {"yosys", "openroad"}, name
