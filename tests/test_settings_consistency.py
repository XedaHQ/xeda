"""Settings that describe one thing must keep agreeing, after construction *and* assignment.

`clock_period` and `clocks`, `generics` and `parameters`, a platform's selected corner and its
supply voltages, and a flow's settings and its dependency's are each correlated state. With
`validate_assignment`, pydantic 2 re-runs a `mode="before"` model validator on every assignment:
the assigned field keeps its raw value, and every *other* field the validator rewrites is written
back. So a validator has to treat the field being assigned as authoritative, and must not
re-impose anything on an assignment that does not concern it.
"""

import copy
import json
from pathlib import Path

import pytest

from xeda.design import Design
from xeda.flow.synth import PhysicalClock
from xeda.flows.yosys.yosys_fpga import YosysFpga

# ---------------------------------------------------------------------------------------------
# Clocks: `clock_period`, `clocks`, and a clock's `freq`/`period` describe one constraint.
# ---------------------------------------------------------------------------------------------


def test_clock_period_and_main_clock_remain_consistent():
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"main_clock": {"period": 10.0}},
        clock_period=5.0,
    )
    assert settings.main_clock is not None
    assert (settings.clock_period, settings.main_clock.period) == (5.0, 5.0)

    settings.clock_period = 4.0
    assert (settings.clock_period, settings.main_clock.period) == (4.0, 4.0)

    clocks = {"main_clock": {"period": 8.0}}
    before = copy.deepcopy(clocks)
    settings.clocks = clocks
    assert clocks == before
    assert settings.main_clock is not None
    assert (settings.clock_period, settings.main_clock.period) == (8.0, 8.0)


def test_physical_clock_assignment_keeps_frequency_and_period_consistent():
    clock = PhysicalClock(period=10.0)

    clock.freq = 200.0
    assert (clock.freq, clock.period) == (200.0, 5.0)

    clock.period = 4.0
    assert (clock.freq, clock.period) == (250.0, 4.0)


def test_a_directly_edited_clock_is_not_reverted_by_an_unrelated_assignment():
    """`clock_period` is the documented shorthand, but it must not keep overwriting `clocks`.

    The settings-wide `mode="before"` validator runs on *every* assignment, so re-imposing
    `clock_period` there silently undid an edit made through `settings.clocks`.
    """
    settings = YosysFpga.Settings(
        fpga={"part": "LFE5U-25F-6BG381C"},
        clocks={"main_clock": {"period": 10.0}},
        clock_period=5.0,
    )
    assert settings.main_clock is not None
    settings.main_clock.period = 7.0

    settings.verbose = 1

    assert settings.main_clock.period == 7.0


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
# `generics` and `parameters` are two spellings of one setting and must never drift apart.
# ---------------------------------------------------------------------------------------------


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
# Settings a flow hands to its dependency have to keep following it after construction.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flow", ["nextpnr", "openxc7"])
def test_propagated_clocks_keep_nested_yosys_period_consistent(flow):
    yosys = YosysFpga.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, clock_period=10.0)
    if flow == "nextpnr":
        from xeda.flows.nextpnr import Nextpnr

        settings = Nextpnr.Settings(
            yosys=yosys,
            fpga={"part": "LFE5U-45F-6BG381C"},
            clock_period=5.0,
        )
    else:
        from xeda.flows.openxc7 import OpenXC7

        settings = OpenXC7.Settings(
            yosys=yosys,
            fpga={"part": "xc7a100tftg256-2L"},
            clock_period=5.0,
        )

    assert yosys.clock_period == 10.0
    assert yosys.main_clock is not None and yosys.main_clock.period == 10.0
    assert settings.yosys is not None and settings.yosys.main_clock is not None
    assert settings.yosys.clock_period == 5.0
    assert settings.yosys.main_clock.period == 5.0

    settings.clock_period = 4.0
    assert settings.yosys.clock_period == 4.0
    assert settings.yosys.main_clock.period == 4.0

    settings.clocks = {"main_clock": {"period": 8.0}}
    assert settings.yosys.clock_period == 8.0
    assert settings.yosys.main_clock.period == 8.0


def test_openfpgaloader_keeps_its_whole_dependency_chain_in_step():
    """`openfpgaloader` -> `nextpnr` -> `yosys_fpga`, all constrained by the same device."""
    from xeda.flows.openfpgaloader import Openfpgaloader

    settings = Openfpgaloader.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, clock_period=10.0)
    assert settings.nextpnr is not None and settings.nextpnr.yosys is not None

    settings.fpga = {"part": "LFE5U-45F-6BG381C"}
    assert settings.nextpnr.fpga is not None and settings.nextpnr.yosys.fpga is not None
    assert settings.nextpnr.fpga.part == "LFE5U-45F-6BG381C"
    assert settings.nextpnr.yosys.fpga.part == "LFE5U-45F-6BG381C"

    settings.clock_period = 4.0
    assert settings.nextpnr.clock_period == 4.0
    assert settings.nextpnr.yosys.clock_period == 4.0


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
