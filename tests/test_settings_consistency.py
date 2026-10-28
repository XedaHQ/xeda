"""Settings that describe one thing must keep agreeing, after construction *and* assignment.

`clocks` and its legacy `clock_period` input, and a platform's selected corner and
its supply voltages are each correlated state. With
`validate_assignment`, pydantic 2 re-runs a `mode="before"` model validator on every assignment:
the assigned field keeps its raw value, and every *other* field the validator rewrites is written
back. So a validator has to treat the field being assigned as authoritative, and must not
re-impose anything on an assignment that does not concern it.
"""

import copy
from pathlib import Path

import pytest

from xeda.dataclass import ValidationError
from xeda.design import Design, Rtl
from xeda.flow.synth import PhysicalClock
from xeda.flows.yosys.yosys_fpga import YosysFpga
from xeda.units import Frequency, Time

from .design_edits import with_updates

pytestmark = pytest.mark.python_compat


# ---------------------------------------------------------------------------------------------
# Clocks: `clocks` is stored; `main_clock` is a property of it.
# ---------------------------------------------------------------------------------------------


def test_canonical_clock_is_stored_once():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"main_clock": {"name": "main_clock", "period": "10 ns"}},
    )
    assert settings.main_clock is not None
    assert settings.main_clock.period == Time("10 ns")
    assert "clock_period" not in settings.model_dump()

    settings.main_clock.period = "4 ns"
    assert settings.main_clock.period == Time("4 ns")

    clocks = {"main_clock": {"period": "8 ns"}}
    before = copy.deepcopy(clocks)
    settings.clocks = clocks
    assert clocks == before
    assert settings.main_clock is not None
    assert settings.main_clock.period == Time("8 ns")


def test_clock_period_was_removed_whatever_it_is_combined_with():
    for extra in ({}, {"clocks": {"main_clock": {"name": "main_clock", "freq": "100 MHz"}}}):
        with pytest.raises(
            ValidationError, match="`clock_period` was removed: use `clocks.<name>.period`"
        ):
            YosysFpga.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, clock_period=5.0, **extra)


def test_main_clock_falls_back_deterministically():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"clk_a": {"freq": "100 MHz"}, "clk_b": {"freq": "50 MHz"}},
    )

    assert settings.main_clock is settings.clocks["clk_a"]
    assert settings.main_clock.period == Time("10 ns")
    settings.main_clock.period = "5 ns"
    assert settings.clocks["clk_a"].period == Time("5 ns")
    assert settings.clocks["clk_b"].period == Time("20 ns")
    assert set(settings.clocks) == {"clk_a", "clk_b"}


def test_physical_clock_assignment_keeps_frequency_and_period_consistent():
    clock = PhysicalClock(period="10 ns")

    clock.freq = "200 MHz"
    assert (clock.freq, clock.period) == (
        Frequency("200 MHz"),
        Time("5 ns"),
    )

    clock.period = "4 ns"
    assert (clock.freq, clock.period) == (
        Frequency("250 MHz"),
        Time("4 ns"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "clk"),
        ("rise", "0.5 ns"),
        ("duty_cycle", 0.4),
        ("uncertainty", "0.1 ns"),
        ("skew", "0.2 ns"),
        ("port", "clk_i"),
    ],
)
def test_unrelated_clock_assignment_does_not_reconcile_rounded_frequency(field, value):
    clock = PhysicalClock(freq="300 MHz")
    pair = (clock.freq, clock.period)

    setattr(clock, field, value)

    assert (clock.freq, clock.period) == pair


def test_a_directly_edited_clock_is_not_reverted_by_an_unrelated_assignment():
    """Editing canonical clock state must not be reverted by unrelated assignment.

    The settings-wide `mode="before"` validator runs on *every* assignment, so re-imposing
    `clocks.main_clock.period` there silently undid an edit made through `settings.clocks`.
    """
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"main_clock": {"period": "10 ns"}},
    )
    assert settings.main_clock is not None
    settings.main_clock.period = "7 ns"

    settings.verbose = 1

    assert settings.main_clock.period == Time("7 ns")


def test_rtl_clock_is_stored_once_as_a_clock_table():
    rtl = Rtl(sources=[], clocks=["clk_i"])

    assert [dict(clock) for clock in rtl.clocks] == [{"port": "clk_i", "name": "clk_i"}]
    assert set(rtl.model_dump()).isdisjoint({"clock", "clock_port"})
    with pytest.raises(ValidationError, match="frozen"):
        rtl.clocks = ({"name": "replacement", "port": "clk_r"},)  # type: ignore[misc]


@pytest.mark.parametrize("value", ["clk", "", None, {"port": "clk"}])
def test_rtl_clock_port_was_removed(value):
    with pytest.raises(ValidationError, match="`clock_port` was removed: use `rtl.clocks`"):
        Rtl(sources=[], clock_port=value)


def test_rtl_clock_port_was_removed_also_beside_clocks():
    with pytest.raises(ValidationError, match="`clock_port` was removed"):
        Rtl(sources=[], clocks=[{"port": "a"}], clock_port="b")


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

    Design(root=tmp_path, **data)

    assert data == before


def test_parameters_are_stored_once_and_nothing_assigns_them():
    design = Design.load(Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.yaml")
    changed = with_updates(design, tb={"parameters": {"G_IN_WIDTH": 8}})

    assert changed.tb.parameters == {"G_IN_WIDTH": 8}
    with pytest.raises(ValidationError, match="frozen"):
        changed.tb.parameters = {"G_IN_WIDTH": 32}  # type: ignore[misc]


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
    declared. A setting a flow cannot run without is its `required_settings`, never required on
    the model."""
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


def test_corner_and_dont_use_cells_belong_to_the_asic_platform_flows_alone():
    """Containment: `yosys` and the OpenROAD flows (ASIC platform flows) are the only flows that
    declare `corner` or `dont_use_cells`, so no edge of the product flows can carry them to
    another flow."""
    from xeda.flows.openroad import OpenroadStage

    flows = _product_flows()
    asic = {"yosys"} | {flow for flow, cls in flows.items() if issubclass(cls, OpenroadStage)}
    assert "openroad_place" in asic
    for name in ("corner", "dont_use_cells"):
        holders = {flow for flow, cls in flows.items() if name in cls.Settings.model_fields}
        assert holders == asic, name
