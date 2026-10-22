"""The placement flow, `openroad_place`: the load, the floorplan, the tap cells and the power grid.

The tests render the driver script of the flow for the `tiny` platform and compare it with a
golden file. They run the rendered script under the fake OpenROAD, so a command that the
OpenROAD command list does not know fails the test. Planning refusals run through the runner.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, get_args

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.platforms import PdkNotInstalled
from xeda.units import Length, Time
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import OpenroadPlace, base
from xeda.flows.openroad.base import (
    fraction_of,
    to_lib_time,
    to_micrometers,
    to_nanometers,
    to_seconds,
)
from xeda.flows.openroad.place_groups import PresizeMode, SwapTarget
from xeda.flows.openroad.place import EffectivePlace, effective
from xeda.flows.openroad.place_pdn import PdnStripe
from xeda.platforms.model import CellPort, Pdn, Track

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    render_driver,
    run_fake_openroad,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import (
    fake_calls,
    fake_returns,
    require_tclsh,
    use_fake_asic_tools,
    use_fake_openroad,
)

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics"
GOLDEN = Path(__file__).parent / "resources" / "openroad" / "place_defaults.tcl"
TEMPLATES = Path(base.__file__).parent / "templates"


# ------------------------------------------------------------------------------ helpers


def place_flow(
    tmp_path: Path,
    settings: dict[str, Any] | None = None,
    *,
    model: dict[str, Any] | None = None,
    sdc: list[Path] | None = None,
) -> OpenroadPlace:
    """Return the flow on the `tiny` platform, ready to render: its run directory is
    `tmp_path/work`, its netlist and its outputs are set and `prepare_inputs()` has run.

    `settings` are nested settings (a group is a dictionary). `model` replaces fields of the
    platform model after `prepare_inputs()`. `sdc` are the SDC files of the design."""
    store = tiny_store(tmp_path, "store")
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = {
        **tiny_settings(store, residue=TINY_OPENROAD_RESIDUE),
        "clocks": {"clk": {"period": "5 ns"}},
        **(settings or {}),
    }
    run_path = tmp_path / "work"
    run_path.mkdir(exist_ok=True)
    flow = OpenroadPlace(
        OpenroadPlace.Settings.from_input(
            given, design_root=design.design_root, runner_cwd=tmp_path
        ),
        design,
        run_path,
    )
    flow.init()
    flow.inputs.netlist = design.design_root / "counter_netlist.v"
    if sdc is not None:
        flow.inputs.sdc = sdc
    flow.outputs.odb = run_path / "place.odb"
    flow.outputs.sdc = run_path / "place.sdc"
    flow.prepare_inputs()
    if model:
        flow.platform_model = flow.platform_model.model_copy(update=model)
    return flow


def render(flow: OpenroadPlace) -> str:
    return render_driver(flow, "openroad_place")


def run_script(flow: OpenroadPlace, script: str | None = None) -> list[list[str]]:
    """Run the driver of `flow` under the fake OpenROAD in its run directory; return the commands
    that the fake recorded, one list of words for each."""
    require_tclsh()
    text = render(flow) if script is None else script
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, text, driver="openroad_place")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(flow.run_path)


def floorplan_calls(calls: list[list[str]]) -> list[list[str]]:
    """The commands up to the stage of the tap cells: those of the load and the floorplan."""
    return calls[: [call[0] for call in calls].index("tapcell")]


def names(calls: list[list[str]], *wanted: str) -> list[str]:
    """The names of the recorded commands that are among `wanted`, in order."""
    return [call[0] for call in calls if call[0] in wanted]


def lines(script: str) -> list[str]:
    """The lines of a script without comments, blank lines and indentation."""
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def stage(script: str, name: str) -> list[str]:
    """The lines of the body of the procedure `name` in a rendered script, without comments and
    indentation. The body ends at the first line that holds only `}` and is not indented."""
    raw = script.splitlines()
    start = raw.index(f"proc {name} {{}} {{")
    end = raw.index("}", start)
    return lines("\n".join(raw[start + 1 : end]))


def plan(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None):
    """Plan `openroad_place` on the tiny platform. `texts` are `KEY=VALUE` settings as the command
    line gives them, and `typed` gives the leaves that are no text, by dotted key."""
    store = tiny_store(tmp_path)
    given = dict(text.split("=", 1) for text in [*openroad_settings(store), *texts])
    given |= typed or {}
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        "openroad_place", openroad_design(tmp_path / "design"), flow_settings=given
    )


def refusal(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None) -> str:
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, *texts, typed=typed)
    message = " ".join(str(error.value).split())
    assert "needs a unit" not in message, "the refusal is about a missing unit: " + message
    return message


# ------------------------------------------------------------------------------ time and numbers


@pytest.mark.parametrize(
    "unit, time, text",
    [
        ("1ns", "0.25 ns", "0.25"),
        ("1ns", "250 ps", "0.25"),
        ("1ns", "5000 ps", "5.0"),
        ("1ps", "0.25 ns", "250.0"),
        ("1ps", "2 us", "2000000.0"),
        ("10ps", "2.5 ns", "250.0"),
        ("1fs", "1 ps", "1000.0"),
    ],
)
def test_a_time_with_a_unit_is_written_in_the_unit_of_the_library(unit, time, text):
    assert to_lib_time(Time(time), unit) == text


@pytest.mark.parametrize("unit", ["1 hour", "0ns", "1e3ns", "fast"])
def test_a_unit_of_the_library_that_is_no_time_is_refused(unit):
    with pytest.raises(ValueError, match="time unit"):
        to_lib_time(Time("1 ns"), unit)


@pytest.mark.parametrize("period, fraction, text", [("5 ns", 0.2, "1.0"), ("8000 ps", 0.25, "2.0")])
def test_a_fraction_of_a_time_is_a_time(period, fraction, text):
    assert to_lib_time(fraction_of(Time(period), fraction), "1ns") == text


@pytest.mark.parametrize("value", [1.0, 5, "1.0", "5 ns", None, True, float("nan"), float("inf")])
def test_only_a_time_is_converted_as_a_time(value):
    with pytest.raises(TypeError, match="a time with a unit"):
        to_lib_time(value, "1ns")


@pytest.mark.parametrize(
    "time, text",
    [
        ("0.25 ns", "2.5e-10"),
        ("0.1 ns", "1e-10"),
        ("5 ns", "5e-09"),
        ("0 ns", "0.0"),
        ("-0.3 ns", "-3e-10"),
        ("250 ps", "2.5e-10"),
        ("0.5 us", "5e-07"),
    ],
)
def test_a_time_is_written_in_seconds_where_the_command_takes_seconds(time, text):
    assert to_seconds(Time(time)) == text


@pytest.mark.parametrize("value", [1.0, "1.0", "5 ns", None, True, float("nan"), float("inf")])
def test_only_a_time_is_converted_as_a_time_in_seconds(value):
    with pytest.raises(TypeError, match="a time with a unit"):
        to_seconds(value)


@pytest.mark.parametrize("unit, text", [("1ns", "0.25"), ("1ps", "250.0")])
def test_the_template_filter_uses_the_time_unit_of_the_platform(tmp_path, unit, text):
    flow = place_flow(tmp_path, model={"time_unit": unit})
    flow.bind_template_values()
    template = flow.jinja_env.from_string("{{ value|lib_time }}")
    assert template.render(value=Time("0.25 ns")) == text
    assert template.render(value=Time("250 ps")) == text
    with pytest.raises(TypeError, match="a time with a unit"):
        template.render(value=0.25)


def test_a_list_of_numbers_is_a_list_word_made_from_numbers(tmp_path):
    flow = place_flow(tmp_path)
    flow.bind_template_values()
    render_numbers = flow.jinja_env.from_string("{{ values|tcl_numbers }}").render
    assert render_numbers(values=(0.0, 1.5, 100.0)) == "[list 0.0 1.5 100.0]"
    with pytest.raises(TypeError):
        render_numbers(values=("0", "[exit]"))


# ------------------------------------------------------------------------------ the golden


def tokens(tmp_path: Path, text: str) -> str:
    """Replace the places that differ from test to test by tokens."""
    for root in sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True):
        text = text.replace(f"{root}/store", "@STORE@").replace(f"{root}/design", "@DESIGN@")
        text = text.replace(f"{root}/work", "@RUN@")
    return text


def test_the_defaults_render_the_golden_script(tmp_path):
    script = tokens(tmp_path, render(place_flow(tmp_path)))
    assert script == GOLDEN.read_text()


def test_the_defaults_run_the_commands_of_the_flow_in_order(tmp_path):
    calls = run_script(place_flow(tmp_path))
    interesting = {
        "read_liberty": 1,
        "read_lef": 2,
        "read_verilog": 1,
        "link_design": 1,
        "read_sdc": 1,
        "set_dont_use": 1,
        "eliminate_dead_logic": 1,
        "check_setup": 1,
        "initialize_floorplan": 1,
        "make_tracks": 3,
        "set_global_routing_layer_adjustment": 2,
        "set_routing_layers": 2,
        "repair_tie_fanout": 2,
        "repair_timing": 1,
        "tapcell": 1,
        "add_global_connection": 2,
        "global_connect": 1,
        "set_voltage_domain": 1,
        "define_pdn_grid": 1,
        "add_pdn_stripe": 3,
        "add_pdn_connect": 2,
        "pdngen": 1,
        "write_db": 1,
        "write_sdc": 1,
    }
    found = names(calls, *interesting)
    expected = [name for name, count in interesting.items() for _ in range(count)]
    assert sorted(found) == sorted(expected)
    first = {name: found.index(name) for name in interesting}
    order = [
        "read_liberty",
        "read_lef",
        "read_verilog",
        "link_design",
        "read_sdc",
        "set_dont_use",
        "eliminate_dead_logic",
        "check_setup",
        "initialize_floorplan",
        "make_tracks",
        "set_global_routing_layer_adjustment",
        "set_routing_layers",
        "repair_tie_fanout",
        "repair_timing",
        "tapcell",
        "add_global_connection",
        "global_connect",
        "set_voltage_domain",
        "define_pdn_grid",
        "add_pdn_stripe",
        "add_pdn_connect",
        "pdngen",
        "write_db",
        "write_sdc",
    ]
    assert [first[name] for name in order] == sorted(first[name] for name in order)


# ------------------------------------------------------------------------------ the floorplan


def test_the_floorplan_is_sized_from_the_utilization_by_default(tmp_path):
    script = render(place_flow(tmp_path))
    assert (
        'initialize_floorplan -utilization 50.0 -aspect_ratio 1.0 -core_space 1.0 -site "tinysite"'
        in lines(script)
    )


def test_the_floorplan_takes_the_die_and_the_core_from_the_areas(tmp_path):
    flow = place_flow(
        tmp_path,
        {
            "floorplan": {
                "sizing": "area",
                "die_area": ["0 um", "0 um", "100 um", "90 um"],
                "core_area": ["2 um", "2 um", "98 um", "88.5 um"],
            }
        },
    )
    script = render(flow)
    assert (
        "initialize_floorplan -die_area [list 0.0 0.0 100.0 90.0] "
        '-core_area [list 2.0 2.0 98.0 88.5] -site "tinysite"'
    ) in lines(script)
    assert "read_def" not in script
    run_script(flow, script)


def test_the_floorplan_takes_everything_from_a_def_file(tmp_path, monkeypatch):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})  # the file places the pins
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    flow = place_flow(
        tmp_path, {"floorplan": {"sizing": "def", "def_file": "plan.def"}}, model={"site": None}
    )
    script = render(flow)
    body = stage(script, "xeda_floorplan")
    assert f'read_def -floorplan_initialize "{tmp_path / "design" / "plan.def"}"' in body
    assert not [line for line in body if line.startswith("initialize_floorplan")]
    run_script(flow, script)


def test_a_def_file_that_does_not_exist_is_refused_naming_the_path(tmp_path):
    (tmp_path / "design").mkdir()
    store = tiny_store(tmp_path, "store")
    design = Design.from_file(openroad_design(tmp_path / "design"))
    settings = OpenroadPlace.Settings.from_input(
        {
            **tiny_settings(store, residue=TINY_OPENROAD_RESIDUE),
            "floorplan": {"sizing": "def", "def_file": "missing.def"},
        },
        design_root=design.design_root,
        runner_cwd=tmp_path,
    )
    flow = OpenroadPlace(settings, design, tmp_path / "work")
    flow.init()
    with pytest.raises(FlowSettingsException, match="missing.def"):
        flow.prepare_inputs()


def test_a_def_file_is_an_input_of_the_run(tmp_path):
    from xeda.flow_runner.trace_inputs import setting_files

    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    flow = place_flow(tmp_path, {"floorplan": {"sizing": "def", "def_file": "plan.def"}})
    assert (tmp_path / "design" / "plan.def").resolve() in setting_files(flow.settings)


def test_dead_logic_is_removed_before_the_floorplan_unless_switched_off(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_floorplan")
    assert body.index("eliminate_dead_logic") < body.index("check_setup")
    flow = place_flow(tmp_path / "off", {"design": {"eliminate_dead_logic": False}})
    assert "eliminate_dead_logic" not in render(flow)


def test_the_tracks_of_the_platform_are_made_layer_by_layer(tmp_path):
    flow = place_flow(
        tmp_path,
        model={
            "tracks": {
                "m1": Track(x=(0.095, 0.19), y=(0.07, 0.14)),
                "m2": Track(x=(0.0, 0.2)),
                "m3": Track(y=(0.1, 0.4)),
            }
        },
    )
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if line.startswith("make_tracks")] == [
        'make_tracks "m1" -x_offset 0.095 -x_pitch 0.19 -y_offset 0.07 -y_pitch 0.14',
        'make_tracks "m2" -x_offset 0.0 -x_pitch 0.2',
        'make_tracks "m3" -y_offset 0.1 -y_pitch 0.4',
    ]


def test_the_tracks_of_the_tiny_platform_come_from_its_lef_files(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_floorplan")
    assert [line.split()[1] for line in body if line.startswith("make_tracks")] == [
        '"m1"',
        '"m2"',
        '"m3"',
    ]


def test_a_platform_without_tracks_has_the_tracks_of_the_lef_files(tmp_path):
    """A bare `make_tracks` takes the pitch of each layer from the LEF files. It is not the
    grid that a platform file gives, so a platform that wants a grid lists its tracks."""
    flow = place_flow(tmp_path, model={"tracks": {}})
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if line.startswith("make_tracks")] == ["make_tracks"]


def test_the_tracks_can_come_from_the_lef_files_although_the_platform_lists_them(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"tracks": "from_tech"}})
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if line.startswith("make_tracks")] == ["make_tracks"]
    run_script(flow)


def test_the_tracks_can_be_asked_from_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"tracks": "platform"}})
    body = stage(render(flow), "xeda_floorplan")
    assert [line.split()[1] for line in body if line.startswith("make_tracks")] == [
        '"m1"',
        '"m2"',
        '"m3"',
    ]
    run_script(flow)


def test_tracks_from_a_platform_that_lists_none_are_refused_naming_the_way_out(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"tracks": "platform"}})
    with pytest.raises(FlowSettingsException, match=r"tracks.*floorplan\.tracks: from_tech"):
        effective(flow.settings, flow.platform_model.model_copy(update={"tracks": {}}))


def test_the_site_of_the_settings_replaces_the_site_of_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"site": "othersite"}})
    body = stage(render(flow), "xeda_floorplan")
    assert any(line.endswith('-site "othersite"') for line in body)
    area = place_flow(
        tmp_path / "area",
        {
            "floorplan": {
                "sizing": "area",
                "die_area": ["0 um", "0 um", "100 um", "90 um"],
                "core_area": ["2 um", "2 um", "98 um", "88 um"],
                "site": "othersite",
            }
        },
    )
    assert '-site "othersite"' in " ".join(stage(render(area), "xeda_floorplan"))


def test_a_site_in_the_settings_makes_the_floorplan_without_a_site_in_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"site": "othersite"}})
    model = flow.platform_model.model_copy(update={"site": None})
    assert effective(flow.settings, model).site == "othersite"


def test_the_rows_get_their_options(tmp_path):
    flow = place_flow(
        tmp_path,
        {
            "floorplan": {
                "additional_sites": ["s2", "s3"],
                "flip_sites": ["s2"],
                "row_gap": "6.0 um",
                "row_parity": "odd",
            }
        },
    )
    line = [l for l in stage(render(flow), "xeda_floorplan") if l.startswith("initialize")][0]
    assert line.endswith(
        '-site "tinysite" -row_parity ODD -additional_sites [list "s2" "s3"] '
        '-flip_sites [list "s2"] -gap "6.0"'
    )
    run_script(flow)


def test_the_row_options_go_to_the_floorplan_by_areas_too(tmp_path):
    flow = place_flow(
        tmp_path,
        {
            "floorplan": {
                "sizing": "area",
                "die_area": ["0 um", "0 um", "100 um", "90 um"],
                "core_area": ["2 um", "2 um", "98 um", "88 um"],
                "row_parity": "even",
            }
        },
    )
    line = [l for l in stage(render(flow), "xeda_floorplan") if l.startswith("initialize")][0]
    assert line.endswith('-site "tinysite" -row_parity EVEN')


@pytest.mark.parametrize("parity, flag", [("none", ""), ("odd", "ODD"), ("even", "EVEN")])
def test_every_row_parity_renders_and_runs(tmp_path, parity, flag):
    flow = place_flow(tmp_path, {"floorplan": {"row_parity": parity}})
    line = [l for l in stage(render(flow), "xeda_floorplan") if l.startswith("initialize")][0]
    assert ("-row_parity " + flag in line) == bool(flag)
    run_script(flow)


def test_a_row_parity_in_capitals_is_refused(tmp_path):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, "floorplan.row_parity=ODD")


@pytest.mark.parametrize(
    "leaf, value",
    [
        ("site", "x"),
        ("additional_sites", "x"),
        ("flip_sites", "x"),
        ("row_gap", "2 um"),
        ("row_parity", "odd"),
    ],
)
def test_a_floorplan_from_a_def_file_has_no_row_option(tmp_path, leaf, value):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    message = refusal(
        tmp_path, "floorplan.sizing=def", "floorplan.def_file=plan.def", f"floorplan.{leaf}={value}"
    )
    assert f"floorplan.{leaf}" in message and "sizing: utilization" in message


def test_the_margin_of_the_core_can_differ_on_each_side(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"core_margin": ["1 um", "2 um", "3 um", "4.5 um"]}})
    body = stage(render(flow), "xeda_floorplan")
    assert any("-core_space [list 1.0 2.0 3.0 4.5] -site" in line for line in body)
    run_script(flow)


@pytest.mark.parametrize(
    "margin",
    [
        ["1 um", "2 um", "3 um"],
        ["1 um", "2 um", "3 um", "4 um", "5 um"],
        ["1 um", "2 um", "3 um", "-4 um"],
        "-1 um",
        [1, 2, 3, 4],
        1,
    ],
)
def test_a_core_margin_is_one_distance_or_four(tmp_path, margin):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"floorplan.core_margin": margin})


POLYGON_DIE = [f"{n} um" for n in (0, 0, 100, 0, 100, 100, 50, 100, 50, 50, 0, 50)]
POLYGON_CORE = [f"{n} um" for n in (2, 2, 98, 2, 98, 98, 52, 98, 52, 52, 2, 52)]


def test_the_floorplan_takes_a_polygon_for_the_die_and_the_core(tmp_path):
    flow = place_flow(
        tmp_path,
        {"floorplan": {"sizing": "area", "die_area": POLYGON_DIE, "core_area": POLYGON_CORE}},
    )
    line = [l for l in stage(render(flow), "xeda_floorplan") if l.startswith("initialize")][0]
    assert "-die_area [list 0.0 0.0 100.0 0.0 100.0 100.0 50.0 100.0 50.0 50.0 0.0 50.0]" in line
    assert "-core_area [list 2.0 2.0 98.0 2.0 98.0 98.0 52.0 98.0 52.0 52.0 2.0 52.0]" in line
    run_script(flow)


@pytest.mark.parametrize("count", [5, 6, 7, 9])
def test_a_die_is_a_rectangle_or_a_polygon_with_corners(tmp_path, count):
    message = refusal(
        tmp_path,
        "floorplan.sizing=area",
        typed={
            "floorplan.die_area": [f"{n} um" for n in range(count)],
            "floorplan.core_area": ["1 um", "1 um", "9 um", "9 um"],
        },
    )
    assert "die_area" in message and "4 lengths" in message


def test_a_polygon_and_a_rectangle_are_left_to_the_tool(tmp_path):
    """OpenROAD stops with IFP-0077 on a rectangle for the die and a polygon for the core. A
    rectangle is a polygon, so xeda does not refuse the pair."""
    plan(
        tmp_path,
        "floorplan.sizing=area",
        typed={
            "floorplan.die_area": ["0 um", "0 um", "100 um", "100 um"],
            "floorplan.core_area": POLYGON_CORE,
        },
    )


def test_a_polygon_is_not_refused_with_the_power_grid_or_the_concurrent_pins(tmp_path):
    """The power grid generator assumes a rectangular design, and the concurrent placement of the
    pins does not support a polygon. The tool says so, and the descriptions name the limits."""
    plan(
        tmp_path,
        "floorplan.sizing=area",
        "io.flow=concurrent",
        typed={"floorplan.die_area": POLYGON_DIE, "floorplan.core_area": POLYGON_CORE},
    )
    from xeda.flows.openroad.place_groups import FloorplanGroup

    for leaf in ("die_area", "core_area"):
        text = FloorplanGroup.model_fields[leaf].description or ""
        assert "pdngen" in text and "place_ios" in text


def test_blockages_of_the_placement_and_of_the_routing_follow_the_tracks(tmp_path):
    flow = place_flow(
        tmp_path,
        {
            "floorplan": {
                "placement_blockages": [
                    {"region": ["1 um", "2 um", "3 um", "4 um"]},
                    {"region": ["5 um", "6 um", "7 um", "8 um"], "soft": True},
                    {"region": ["9 um", "10 um", "11 um", "12 um"], "max_density_percent": 40},
                ],
                "routing_blockages": [{"layer": "m2", "region": ["1 um", "1 um", "2 um", "2 um"]}],
            }
        },
    )
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if "blockage" in line or "obstruction" in line] == [
        "create_blockage -region [list 1.0 2.0 3.0 4.0]",
        "create_blockage -region [list 5.0 6.0 7.0 8.0] -soft",
        "create_blockage -region [list 9.0 10.0 11.0 12.0] -max_density 40.0",
        'create_obstruction -layer "m2" -region [list 1.0 1.0 2.0 2.0]',
    ]
    found = names(
        floorplan_calls(run_script(flow)),
        "make_tracks",
        "create_blockage",
        "create_obstruction",
        "repair_tie_fanout",
    )
    assert found == ["make_tracks"] * 3 + ["create_blockage"] * 3 + [
        "create_obstruction",
        "repair_tie_fanout",
        "repair_tie_fanout",
    ]


def test_a_soft_blockage_has_no_density(tmp_path):
    """OpenROAD ignores `-soft` when it gets `-max_density` (ODB-1016): the density makes the
    blockage partial, so the pair says nothing sensible."""
    message = refusal(
        tmp_path,
        typed={
            "floorplan.placement_blockages": [
                {
                    "region": ["1 um", "2 um", "3 um", "4 um"],
                    "soft": True,
                    "max_density_percent": 40,
                }
            ]
        },
    )
    assert (
        "placement_blockages" in message and "soft" in message and "max_density_percent" in message
    )


@pytest.mark.parametrize(
    "key, item",
    [
        ("placement_blockages", {"region": ["3 um", "2 um", "1 um", "4 um"]}),
        ("placement_blockages", {"region": ["1 um", "2 um", "3 um", "2 um"]}),
        ("routing_blockages", {"layer": "m2", "region": ["3 um", "2 um", "1 um", "4 um"]}),
    ],
)
def test_a_blockage_region_lists_the_lower_left_and_the_upper_right_corner(tmp_path, key, item):
    message = refusal(tmp_path, typed={f"floorplan.{key}": [item]})
    assert key in message and "lower-left" in message


@pytest.mark.parametrize(
    "key, item",
    [
        ("placement_blockages", {"region": ["1 um", "2 um", "3 um"]}),
        (
            "placement_blockages",
            {"region": ["1 um", "2 um", "3 um", "4 um"], "max_density_percent": 101},
        ),
        (
            "placement_blockages",
            {"region": ["1 um", "2 um", "3 um", "4 um"], "max_density_percent": -1},
        ),
        ("placement_blockages", {"region": ["1 um", "2 um", "3 um", "4 um"], "layer": "m1"}),
        ("routing_blockages", {"region": ["1 um", "2 um", "3 um", "4 um"]}),
        ("routing_blockages", {"layer": "", "region": ["1 um", "2 um", "3 um", "4 um"]}),
    ],
)
def test_a_malformed_blockage_is_refused(tmp_path, key, item):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={f"floorplan.{key}": [item]})


def test_the_check_of_the_setup_can_be_switched_off(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"check_setup": False}})
    assert "check_setup" not in stage(render(flow), "xeda_floorplan")
    assert not names(run_script(flow), "check_setup")


def test_the_layers_and_their_capacity_come_from_the_platform(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_floorplan")
    assert [line for line in body if "layer_adjustment" in line or "routing_layers" in line] == [
        'set_global_routing_layer_adjustment "m2-m3" 0.5',
        'set_global_routing_layer_adjustment "m4" 0.25',
        'set_routing_layers -clock "m3-m4"',
        'set_routing_layers -signal "m2-m4"',
    ]


def test_the_settings_replace_the_layers_and_the_capacity_of_the_platform(tmp_path):
    flow = place_flow(
        tmp_path,
        {
            "routing": {
                "signal_layers": "m2-m3",
                "clock_layers": "m3-m3",
                "layer_adjustments": {"m2": 0.1},
            }
        },
    )
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if "layer_adjustment" in line or "routing_layers" in line] == [
        'set_global_routing_layer_adjustment "m2" 0.1',
        'set_routing_layers -clock "m3-m3"',
        'set_routing_layers -signal "m2-m3"',
    ]


def test_one_capacity_of_the_platform_covers_the_signal_layers(tmp_path):
    from xeda.platforms.model import Routing

    routing = Routing(min_layer="m2", max_layer="m4", clock_min_layer="m3", layer_adjustment=0.4)
    body = stage(render(place_flow(tmp_path, model={"routing": routing})), "xeda_floorplan")
    assert 'set_global_routing_layer_adjustment "m2-m4" 0.4' in body


def test_a_platform_without_layers_writes_no_layer_command(tmp_path):
    from xeda.platforms.model import Routing

    body = stage(render(place_flow(tmp_path, model={"routing": Routing()})), "xeda_floorplan")
    assert not [line for line in body if "layer_adjustment" in line or "routing_layers" in line]


def test_tie_fanout_is_repaired_for_the_low_and_then_the_high_tie_cell(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_floorplan")
    assert [line for line in body if line.startswith("repair_tie_fanout")] == [
        'repair_tie_fanout -separation "0.0" [xeda_tie_pin "TIELO_X1" "Z"]',
        'repair_tie_fanout -separation "0.0" [xeda_tie_pin "TIEHI_SMALL" "Z"]',
    ]


def test_tie_fanout_comes_before_any_removal_of_buffers(tmp_path):
    """One tie cell can drive buffers that drive many outputs, so the repair needs the buffers."""
    flow = place_flow(tmp_path, {"floorplan": {"netlist_cleanup": {"mode": "remove_buffers"}}})
    calls = floorplan_calls(run_script(flow))
    found = names(calls, "repair_tie_fanout", "remove_buffers", "repair_timing")
    assert found == ["repair_tie_fanout", "repair_tie_fanout", "remove_buffers"]


def test_the_tie_separation_is_passed(tmp_path):
    body = stage(render(place_flow(tmp_path, {"tie": {"separation": "2.5 um"}})), "xeda_floorplan")
    assert all('-separation "2.5"' in line for line in body if line.startswith("repair_tie_fanout"))


def test_no_tie_fanout_is_repaired_when_it_is_switched_off(tmp_path):
    flow = place_flow(tmp_path, {"tie": {"repair_fanout": False}}, model={"tie_low": None})
    assert "repair_tie_fanout" not in " ".join(stage(render(flow), "xeda_floorplan"))
    assert not names(run_script(flow), "repair_tie_fanout")


def test_the_netlist_has_no_tie_cells_inserted_by_openroad_unless_asked(tmp_path):
    """The synthesis puts the tie cells in the netlist, so the flow runs no `insert_tiecells`
    unless `tie.insert_cells` asks for it."""
    assert "insert_tiecells" not in render(place_flow(tmp_path))


def test_inserted_tie_cells_use_the_master_and_the_port_of_the_platform(tmp_path):
    """`insert_tiecells` takes `<master>/<port>`, not the `<library>/<master>/<port>` of
    `repair_tie_fanout`. Both come before the repair, which splits the new cells."""
    flow = place_flow(tmp_path, {"tie": {"insert_cells": True}})
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if line.startswith("insert_tiecells")] == [
        'insert_tiecells "TIELO_X1/Z" -prefix TIE_ZERO_',
        'insert_tiecells "TIEHI_SMALL/Z" -prefix TIE_ONE_',
    ]
    found = names(floorplan_calls(run_script(flow)), "insert_tiecells", "repair_tie_fanout")
    assert found == ["insert_tiecells"] * 2 + ["repair_tie_fanout"] * 2


def test_tie_cells_can_be_inserted_without_repairing_their_fanout(tmp_path):
    flow = place_flow(tmp_path, {"tie": {"insert_cells": True, "repair_fanout": False}})
    assert (
        names(run_script(flow), "insert_tiecells", "repair_tie_fanout") == ["insert_tiecells"] * 2
    )


def test_inserted_tie_cells_need_the_tie_cells_of_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"tie": {"insert_cells": True, "repair_fanout": False}})
    with pytest.raises(FlowSettingsException, match=r"tie_low.*tie\.insert_cells"):
        effective(flow.settings, flow.platform_model.model_copy(update={"tie_low": None}))


def test_the_fanout_limit_of_a_tie_cell_is_passed(tmp_path):
    body = stage(render(place_flow(tmp_path, {"tie": {"max_fanout": 8}})), "xeda_floorplan")
    ties = [line for line in body if line.startswith("repair_tie_fanout")]
    assert len(ties) == 2 and all('-separation "0.0" -max_fanout "8"' in line for line in ties)


@pytest.mark.parametrize("leaf, value", [("separation", "1 um"), ("max_fanout", 4)])
def test_a_tie_option_has_no_effect_without_the_repair_of_the_fanout(tmp_path, leaf, value):
    message = refusal(tmp_path, f"tie.{leaf}={value}", "tie.repair_fanout=false")
    assert f"tie.{leaf}" in message and "no effect" in message


def test_the_tie_options_keep_their_ranges(tmp_path):
    for text in ("tie.max_fanout=0", "tie.separation=-1 um"):
        with pytest.raises(FlowSettingsException):
            plan(tmp_path / text, text)


def test_the_netlist_cleanup_repairs_setup_without_parasitics(tmp_path):
    """The repair at the floorplan runs before the placement. It repairs setup only, with the
    moves of OpenROAD-flow-scripts, and no `estimate_parasitics` comes before it."""
    flow = place_flow(tmp_path)
    script = render(flow)
    body = stage(script, "xeda_floorplan")
    (repair,) = [line for line in body if line.startswith("repair_timing")]
    assert repair == (
        "repair_timing -setup -setup_margin 0.0 -hold_margin 0.0 "
        '-sequence "unbuffer sizeup swap vt_swap" -repair_tns "100.0" -skip_last_gasp -verbose'
    )
    assert "estimate_parasitics" not in " ".join(body)
    calls = floorplan_calls(run_script(flow, script))
    assert names(calls, "estimate_parasitics") == []


def test_the_repair_margins_are_written_in_the_time_unit_of_the_platform(tmp_path):
    settings = {
        "floorplan": {
            "netlist_cleanup": {"repair": {"setup_margin": "0.05 ns", "hold_margin": "0.02 ns"}}
        }
    }
    for unit, setup, hold in (("1ns", "0.05", "0.02"), ("1ps", "50.0", "20.0")):
        flow = place_flow(tmp_path / unit, settings, model={"time_unit": unit})
        body = stage(render(flow), "xeda_floorplan")
        (repair,) = [line for line in body if line.startswith("repair_timing")]
        assert f"-setup_margin {setup} -hold_margin {hold}" in repair


def test_the_repair_follows_its_settings(tmp_path):
    repair = {
        "fix": "setup_and_hold",
        "tns_end_percent": 40,
        "sequence": ["sizeup", "swap"],
        "last_gasp": True,
        "moves": {"pin_swap": False},
        "match_cell_footprint": True,
    }
    flow = place_flow(tmp_path, {"floorplan": {"netlist_cleanup": {"repair": repair}}})
    body = stage(render(flow), "xeda_floorplan")
    (line,) = [line for line in body if line.startswith("repair_timing")]
    assert line == (
        'repair_timing -setup_margin 0.0 -hold_margin 0.0 -sequence "sizeup swap" '
        '-repair_tns "40.0" -skip_pin_swap -match_cell_footprint -verbose'
    )


def test_the_phases_of_a_repair_are_words_with_spaces_between_them(tmp_path):
    """`repair_timing -phases` does not split at a comma: `WNS,TNS` is one unknown phase name
    (RSZ-0217). The moves of `-sequence` are written the same way."""
    repair = {"sequence": ["sizeup", "swap"], "phases": ["WNS", "LAST_GASP", "TNS"]}
    flow = place_flow(
        tmp_path,
        {
            "floorplan": {"netlist_cleanup": {"repair": repair}},
            "resize": {"repair_timing": {"enabled": True, **repair}},
        },
    )
    script = render(flow)
    for name in ("xeda_floorplan", "xeda_resize"):
        (line,) = [line for line in stage(script, name) if line.startswith("repair_timing")]
        assert '-sequence "sizeup swap" -phases "WNS LAST_GASP TNS"' in line, name
        assert "," not in line, name
    run_script(flow, script)


def test_the_footprint_of_the_platform_applies_unless_the_settings_say_otherwise(tmp_path):
    flow = place_flow(tmp_path, model={"match_cell_footprint": True})
    body = stage(render(flow), "xeda_floorplan")
    assert any("-match_cell_footprint" in line for line in body)
    off = {"floorplan": {"netlist_cleanup": {"repair": {"match_cell_footprint": False}}}}
    flow = place_flow(tmp_path / "off", off, model={"match_cell_footprint": True})
    assert not any(
        "-match_cell_footprint" in line for line in stage(render(flow), "xeda_floorplan")
    )


@pytest.mark.parametrize("fix, flag", [("hold", "-hold"), ("setup_and_hold", "")])
def test_the_repair_checks_that_the_settings_name(tmp_path, fix, flag):
    cleanup = {"repair": {"fix": fix}}
    flow = place_flow(tmp_path, {"floorplan": {"netlist_cleanup": cleanup}})
    (line,) = [l for l in stage(render(flow), "xeda_floorplan") if l.startswith("repair_timing")]
    words = line.split()
    assert ("-hold" in words) == (flag == "-hold") and "-setup" not in words


def test_a_netlist_cleanup_by_buffer_removal_removes_the_buffers_only(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"netlist_cleanup": {"mode": "remove_buffers"}}})
    body = stage(render(flow), "xeda_floorplan")
    assert "remove_buffers" in body
    assert not [line for line in body if line.startswith("repair_timing")]


def test_a_floorplan_without_netlist_cleanup_does_neither(tmp_path):
    flow = place_flow(tmp_path, {"floorplan": {"netlist_cleanup": {"mode": "none"}}})
    body = stage(render(flow), "xeda_floorplan")
    assert "remove_buffers" not in body
    assert not [line for line in body if line.startswith("repair_timing")]


def test_the_instances_that_the_resizer_must_keep_are_marked_one_by_one(tmp_path):
    flow = place_flow(tmp_path, {"design": {"dont_touch": ["u1/*", "n[3]"]}})
    body = stage(render(flow), "xeda_floorplan")
    assert [line for line in body if line.startswith("set_dont_touch")] == [
        'set_dont_touch "u1/*"',
        r'set_dont_touch "n\[3\]"',
    ]
    run_script(flow)


def test_a_marked_instance_is_connected_to_the_power_nets_before_it_is_marked_again(tmp_path):
    """`global_connect` skips an instance that is marked do not touch, and that instance would
    stay without power. The floorplan marks the instances before the repair, so that the repair
    keeps them. The power grid stage unmarks them for its global connection and marks them again."""
    flow = place_flow(tmp_path, {"design": {"dont_touch": ["u1/*", "n[3]"]}})
    calls = run_script(flow)
    wanted = ("set_dont_touch", "unset_dont_touch", "global_connect", "repair_timing")
    found = [call[:2] if call[0].endswith("dont_touch") else call[:1] for call in calls]
    found = [call for call in found if call[0] in wanted]
    assert found == [
        ["set_dont_touch", "u1/*"],
        ["set_dont_touch", "n[3]"],
        ["repair_timing"],
        ["unset_dont_touch", "u1/*"],
        ["unset_dont_touch", "n[3]"],
        ["global_connect"],
        ["set_dont_touch", "u1/*"],
        ["set_dont_touch", "n[3]"],
    ]


def test_without_a_power_grid_the_marked_instances_stay_marked_all_along(tmp_path):
    flow = place_flow(tmp_path, {"design": {"dont_touch": ["u1"]}, "pdn": {"mode": "none"}})
    calls = run_script(flow)
    assert names(calls, "set_dont_touch", "unset_dont_touch") == ["set_dont_touch"]


def test_cells_not_to_use_are_set_before_any_command_that_resizes(tmp_path):
    calls = run_script(place_flow(tmp_path))
    found = names(calls, "set_dont_use", "repair_tie_fanout", "repair_timing", "tapcell")
    assert found[0] == "set_dont_use" and found.count("set_dont_use") == 1


def test_the_wire_rc_is_applied_before_the_floorplan_repairs(tmp_path):
    calls = run_script(place_flow(tmp_path))
    found = names(calls, "set_layer_rc", "set_wire_rc", "repair_timing", "initialize_floorplan")
    assert found.index("repair_timing") > max(
        i for i, name in enumerate(found) if name in ("set_layer_rc", "set_wire_rc")
    )


def test_the_floorplan_arith_swap_runs_before_the_netlist_cleanup(tmp_path):
    flow = place_flow(tmp_path, {"hierarchical": True, "arith_swap": {"enabled": True}})
    calls = floorplan_calls(run_script(flow))
    found = names(calls, "replace_arith_modules", "repair_timing", "repair_tie_fanout")
    assert found == [
        "repair_tie_fanout",
        "repair_tie_fanout",
        "replace_arith_modules",
        "repair_timing",
    ]


def test_the_swap_of_arithmetic_modules_runs_with_the_defaults_of_the_tool(tmp_path):
    flow = place_flow(tmp_path, {"hierarchical": True, "arith_swap": {"enabled": True}})
    script = render(flow)
    for name in ("xeda_floorplan", "xeda_resize"):
        assert "replace_arith_modules" in stage(script, name), name


def test_the_options_of_the_swap_reach_both_calls_of_the_command(tmp_path):
    swap = {"enabled": True, "path_count": 200, "target": "area", "slack_threshold": "0.05 ns"}
    flow = place_flow(tmp_path, {"hierarchical": True, "arith_swap": swap})
    script = render(flow)
    expected = 'replace_arith_modules -path_count "200" -target "area" -slack_threshold 0.05'
    for name in ("xeda_floorplan", "xeda_resize"):
        assert expected in stage(script, name), name
    calls = run_script(flow, script)
    assert [call for call in calls if call[0] == "replace_arith_modules"] == [
        [
            "replace_arith_modules",
            "-path_count",
            "200",
            "-target",
            "area",
            "-slack_threshold",
            "0.05",
        ]
    ] * 2


@pytest.mark.parametrize("unit, text", [("1ns", "0.05"), ("1ps", "50.0"), ("10ps", "5.0")])
def test_the_slack_threshold_of_the_swap_is_in_the_time_unit_of_the_library(tmp_path, unit, text):
    swap = {"enabled": True, "slack_threshold": "0.05 ns"}
    flow = place_flow(
        tmp_path, {"hierarchical": True, "arith_swap": swap}, model={"time_unit": unit}
    )
    assert f"replace_arith_modules -slack_threshold {text}" in stage(render(flow), "xeda_floorplan")


@pytest.mark.parametrize("target", get_args(SwapTarget))
def test_every_target_of_the_swap_renders_and_runs(tmp_path, target):
    swap = {"enabled": True, "target": target}
    flow = place_flow(tmp_path, {"hierarchical": True, "arith_swap": swap})
    assert f'replace_arith_modules -target "{target}"' in stage(render(flow), "xeda_floorplan")
    run_script(flow)


def test_a_target_that_the_tool_does_not_swap_for_yet_is_not_refused_at_planning(tmp_path):
    plan(tmp_path, "arith_swap.enabled=true", "arith_swap.target=hold", "hierarchical=true")
    description = " ".join(
        OpenroadPlace.Settings.model_fields["arith_swap"]
        .annotation.model_fields["target"]
        .description.split()
    )
    assert "RSZ-0155" in description and "`setup`" in description


def test_the_hierarchy_reaches_the_link_of_the_netlist(tmp_path):
    flow = place_flow(tmp_path, {"hierarchical": True})
    assert 'link_design -hier -omit_filename_prop "counter"' in lines(render(flow))


# ------------------------------------------------------------------------------ tap cells


def test_the_tap_cells_come_from_the_platform(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_tapcell")
    assert body == [
        "xeda_stage tapcell",
        'tapcell -distance 12.0 -tapcell_master "TAP_X1" -endcap_master "ENDCAP_X1"',
    ]


def test_the_settings_replace_the_tap_cells_of_the_platform(tmp_path):
    flow = place_flow(
        tmp_path, {"tapcell": {"distance": "30 um", "master": "T2", "endcap_master": "E2"}}
    )
    body = stage(render(flow), "xeda_tapcell")
    assert 'tapcell -distance 30.0 -tapcell_master "T2" -endcap_master "E2"' in body


def test_a_platform_without_an_end_cap_places_tap_cells_only(tmp_path):
    flow = place_flow(tmp_path, model={"endcap_cell": None})
    assert 'tapcell -distance 12.0 -tapcell_master "TAP_X1"' in stage(render(flow), "xeda_tapcell")


def test_the_rows_are_cut_when_the_platform_has_no_tap_cell(tmp_path):
    flow = place_flow(tmp_path, model={"tap_cell": None, "endcap_cell": None})
    body = stage(render(flow), "xeda_tapcell")
    assert body == ["xeda_stage tapcell", "cut_rows"]
    run_script(flow)


def test_the_rows_are_cut_with_the_end_cap_of_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"mode": "cut_rows"}})
    assert stage(render(flow), "xeda_tapcell")[1:] == ['cut_rows -endcap_master "ENDCAP_X1"']
    run_script(flow)


def test_the_tap_cell_mode_none_places_nothing(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"mode": "none"}})
    assert stage(render(flow), "xeda_tapcell") == ["xeda_stage tapcell"]
    assert not names(run_script(flow), "tapcell", "cut_rows")


def test_the_tap_cell_mode_tap_endcap_needs_a_master_and_a_distance(tmp_path):
    flow = place_flow(tmp_path)
    model = flow.platform_model.model_copy(update={"tap_cell": None})
    assert (
        effective(flow.settings, model).tapcell_mode == "cut_rows"
    )  # the platform has no tap cell
    flow.settings.tapcell.mode = "tap_endcap"
    with pytest.raises(FlowSettingsException, match=r"tapcell\.master"):
        effective(flow.settings, model)
    flow.settings.tapcell.master = "TAP_X1"
    model = model.model_copy(
        update={"tapcell": model.tapcell.model_copy(update={"distance": None})}
    )
    with pytest.raises(FlowSettingsException, match=r"tapcell\.distance"):
        effective(flow.settings, model)


def test_a_tap_distance_has_no_effect_when_the_platform_cuts_the_rows(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"distance": "20 um"}})
    model = flow.platform_model.model_copy(update={"tap_cell": None})
    with pytest.raises(FlowSettingsException, match=r"tapcell\.distance.*cut_rows"):
        effective(flow.settings, model)


def test_the_rows_of_the_tap_cells_keep_a_minimum_width_and_height(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"row_min_width": "3 um", "row_min_height": "2.5 um"}})
    assert stage(render(flow), "xeda_tapcell")[1:] == [
        'tapcell -distance 12.0 -tapcell_master "TAP_X1" -endcap_master "ENDCAP_X1" '
        '-row_min_width "3.0" -row_min_height "2.5"'
    ]
    run_script(flow)


def test_the_rows_that_are_cut_keep_a_minimum_width_and_height(tmp_path):
    flow = place_flow(
        tmp_path,
        {"tapcell": {"mode": "cut_rows", "row_min_width": "3 um", "row_min_height": "2.5 um"}},
    )
    assert stage(render(flow), "xeda_tapcell")[1:] == [
        'cut_rows -endcap_master "ENDCAP_X1" -row_min_width "3.0" -row_min_height "2.5"'
    ]
    run_script(flow)


NWELL_FLAGS = {
    "nwin2": "-tap_nwin2_master",
    "nwin3": "-tap_nwin3_master",
    "nwintie": "-tap_nwintie_master",
    "nwout2": "-tap_nwout2_master",
    "nwout3": "-tap_nwout3_master",
    "nwouttie": "-tap_nwouttie_master",
}
CORNER_FLAGS = {
    "nwin": "-cnrcap_nwin_master",
    "nwout": "-cnrcap_nwout_master",
    "inner_nwin": "-incnrcap_nwin_master",
    "inner_nwout": "-incnrcap_nwout_master",
}


@pytest.mark.parametrize(
    "group, leaf, flag",
    [("nwell_masters", leaf, flag) for leaf, flag in NWELL_FLAGS.items()]
    + [("corner_masters", leaf, flag) for leaf, flag in CORNER_FLAGS.items()],
)
def test_the_masters_by_well_and_corner_are_passed_to_the_tap_cell_command(
    tmp_path, group, leaf, flag
):
    flow = place_flow(tmp_path, {"tapcell": {group: {leaf: "M_X1"}}})
    line = stage(render(flow), "xeda_tapcell")[1]
    assert line.endswith(f'{flag} "M_X1"') and line.startswith("tapcell -distance 12.0")
    run_script(flow)


def test_the_masters_by_well_come_before_the_masters_of_the_corners(tmp_path):
    flow = place_flow(
        tmp_path,
        {"tapcell": {"corner_masters": {"nwin": "C1"}, "nwell_masters": {"nwin2": "W1"}}},
    )
    line = stage(render(flow), "xeda_tapcell")[1]
    assert line.index("-tap_nwin2_master") < line.index("-cnrcap_nwin_master")


@pytest.mark.parametrize(
    "mode, leaf",
    [
        ("none", "row_min_width"),
        ("none", "row_min_height"),
        ("none", "nwell_masters.nwin2"),
        ("none", "corner_masters.nwin"),
        ("cut_rows", "nwell_masters.nwin2"),
        ("cut_rows", "corner_masters.nwout"),
    ],
)
def test_a_tap_cell_option_that_the_mode_does_not_use_is_refused(tmp_path, mode, leaf):
    value = "2 um" if leaf.startswith("row_") else "M_X1"
    message = refusal(tmp_path, f"tapcell.mode={mode}", f"tapcell.{leaf}={value}")
    assert f"tapcell.{leaf}" in message and "no effect" in message


def test_a_master_by_well_has_no_effect_when_the_platform_cuts_the_rows(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"nwell_masters": {"nwin2": "W1"}}})
    model = flow.platform_model.model_copy(update={"tap_cell": None})
    with pytest.raises(FlowSettingsException, match=r"tapcell\.nwell_masters\.nwin2.*cut_rows"):
        effective(flow.settings, model)


def test_the_tap_cell_options_keep_their_ranges(tmp_path):
    for text in (
        "tapcell.row_min_width=-1 um",
        "tapcell.row_min_height=-1 um",
        "tapcell.nwell_masters.nwin2=",
    ):
        with pytest.raises(FlowSettingsException):
            plan(tmp_path / text, text)


# ------------------------------------------------------------------------------ power grid


PDN_LINES = [
    "xeda_stage pdn",
    'add_global_connection -net "VDD" -pin_pattern "^VDD\\$" -power',
    'add_global_connection -net "VSS" -pin_pattern "^VSS\\$" -ground',
    "global_connect",
    'set_voltage_domain -name CORE -power "VDD" -ground "VSS"',
    'define_pdn_grid -name grid -voltage_domains CORE -pins [list "m4"]',
    'add_pdn_stripe -grid grid -layer "m1" -width 0.12 -pitch 1.4 -offset 0.0 -followpins',
    'add_pdn_stripe -grid grid -layer "m3" -width 0.24 -pitch 8.0 -offset 1.0',
    'add_pdn_stripe -grid grid -layer "m4" -width 0.48 -pitch 12.0 -offset 1.0',
    'add_pdn_connect -grid grid -layers [list "m1" "m3"]',
    'add_pdn_connect -grid grid -layers [list "m3" "m4"]',
    "pdngen",
]


def test_the_power_grid_comes_from_the_platform(tmp_path):
    flow = place_flow(tmp_path)
    assert stage(render(flow), "xeda_pdn") == PDN_LINES
    run_script(flow)


def test_the_power_grid_is_not_checked(tmp_path):
    """`check_power_grid` fails on a design without pads, so the flow never calls it."""
    script = render(place_flow(tmp_path))
    assert "check_power_grid" not in script


def test_a_leaf_of_the_grid_in_the_settings_replaces_the_leaf_of_the_platform(tmp_path):
    """The pitch of the stripes must fit the die, so a small design needs its own pitch."""
    grid = {"straps": [{"layer": "m3", "width": "0.3 um", "pitch": "4 um", "offset": "2 um"}]}
    flow = place_flow(tmp_path, {"pdn": {"grid": grid}})
    body = stage(render(flow), "xeda_pdn")
    stripes = [line for line in body if line.startswith("add_pdn_stripe")]
    assert stripes == [
        'add_pdn_stripe -grid grid -layer "m1" -width 0.12 -pitch 1.4 -offset 0.0 -followpins',
        'add_pdn_stripe -grid grid -layer "m3" -width 0.3 -pitch 4.0 -offset 2.0',
    ]
    assert body[1:5] == PDN_LINES[1:5]  # the nets and the connections are the platform's
    assert body[-3:] == PDN_LINES[-3:]


def test_the_grid_of_the_settings_may_give_a_leaf_that_the_platform_leaves_empty(tmp_path):
    grid = {
        "power_net": "VPWR",
        "ground_net": "VGND",
        "rails": [{"layer": "m1", "width": "0.1 um"}],
    }
    flow = place_flow(tmp_path, {"pdn": {"grid": grid}}, model={"pdn": Pdn()})
    body = stage(render(flow), "xeda_pdn")
    assert 'set_voltage_domain -name CORE -power "VPWR" -ground "VGND"' in body
    assert not [line for line in body if line.startswith("add_global_connection")]


def test_an_empty_list_in_the_grid_keeps_the_list_of_the_platform(tmp_path):
    """A leaf that the settings leave empty is a leaf that they do not give."""
    flow = place_flow(tmp_path, {"pdn": {"grid": {"global_connects": []}}})
    body = stage(render(flow), "xeda_pdn")
    assert len([line for line in body if line.startswith("add_global_connection")]) == 2


def test_a_global_connection_without_a_kind_takes_it_from_the_net(tmp_path):
    connects = [
        {"net": "VDD", "pin_pattern": "^VDD$"},
        {"net": "VSS", "inst_pattern": "u.*", "pin_pattern": "^VSS$"},
    ]
    flow = place_flow(tmp_path, {"pdn": {"grid": {"global_connects": connects}}})
    body = stage(render(flow), "xeda_pdn")
    assert [line for line in body if line.startswith("add_global_connection")] == [
        'add_global_connection -net "VDD" -pin_pattern "^VDD\\$" -power',
        'add_global_connection -net "VSS" -inst_pattern "u.*" -pin_pattern "^VSS\\$" -ground',
    ]


def test_a_global_connection_to_a_net_of_no_kind_is_refused_before_openroad_starts(tmp_path):
    """`prepare_inputs()` computes the values of the templates, so the refusal comes before the
    run, and `place_flow` runs it."""
    connects = [{"net": "OTHER", "pin_pattern": "^X$"}]
    with pytest.raises(FlowSettingsException, match=r"OTHER.*kind"):
        place_flow(tmp_path, {"pdn": {"grid": {"global_connects": connects}}})


def test_a_leaf_of_the_grid_replaces_the_platform_leaf_in_a_planned_flow(tmp_path):
    """The planner validates the settings again, which marks every field of the grid as given.
    A leaf that the user left out still keeps the value of the platform."""
    planned = plan(
        tmp_path,
        typed={
            "pdn.grid": {
                "straps": [{"layer": "m4", "width": "1 um", "pitch": "6 um", "offset": "1 um"}]
            }
        },
    )
    settings = planned.nodes[0].settings
    assert {"power_net", "rails"} <= settings.pdn.grid.model_fields_set
    model = place_flow(tmp_path / "flow").platform_model
    grid = effective(settings, model).pdn
    assert [(strap.layer, strap.pitch) for strap in grid.straps] == [("m4", Length("6 um"))]
    assert grid.power_net == "VDD" and [rail.layer for rail in grid.rails] == ["m1"]


def grid_lines(tmp_path: Path, settings: dict[str, Any], prefix: str) -> list[str]:
    """The lines of the power grid stage that start with `prefix`, for a flow in a new directory
    of `tmp_path`."""
    flow = place_flow(Path(tempfile.mkdtemp(dir=tmp_path)), {"pdn": settings})
    return [line for line in stage(render(flow), "xeda_pdn") if line.startswith(prefix)]


def test_the_secondary_power_nets_belong_to_the_voltage_domain(tmp_path):
    lines_ = grid_lines(tmp_path, {"secondary_power": ["VDDA", "VDDB"]}, "set_voltage_domain")
    assert lines_ == [
        'set_voltage_domain -name CORE -power "VDD" -ground "VSS" '
        '-secondary_power [list "VDDA" "VDDB"]'
    ]


@pytest.mark.parametrize("polarity", ["power", "ground"])
def test_the_grid_starts_with_power_or_ground(tmp_path, polarity):
    found = grid_lines(tmp_path, {"grid": {"starts_with": polarity}}, "define_pdn_grid")
    assert found == [
        f'define_pdn_grid -name grid -voltage_domains CORE -pins [list "m4"] '
        f"-starts_with {polarity.upper()}"
    ]
    run_script(place_flow(tmp_path / polarity, {"pdn": {"grid": {"starts_with": polarity}}}))


def test_the_options_of_a_stripe_are_passed_after_its_size(tmp_path):
    strap = {
        "layer": "m4",
        "width": "0.5 um",
        "pitch": "12 um",
        "offset": "1 um",
        "spacing": "3 um",
        "offset_type": "center",
        "starts_with": "power",
        "extend": "core_ring",
        "extend_by": "2 um",
        "snap_to_grid": True,
        "number_of_straps": 4,
        "nets": ["VDD", "VSS"],
        "allow_out_of_core": True,
    }
    found = grid_lines(tmp_path, {"grid": {"straps": [strap]}}, "add_pdn_stripe")
    assert found[1] == (
        'add_pdn_stripe -grid grid -layer "m4" -width 0.5 -pitch 12.0 -offset 1.0 -spacing 3.0 '
        "-offset_type CENTER -starts_with POWER -extend_to_core_ring -extend_by 2.0 "
        '-snap_to_grid -number_of_straps 4 -nets [list "VDD" "VSS"] -allow_out_of_core'
    )
    flow = place_flow(tmp_path / "run", {"pdn": {"grid": {"straps": [strap]}}})
    run_script(flow)


@pytest.mark.parametrize(
    "kind, flag", [("core_ring", "-extend_to_core_ring"), ("boundary", "-extend_to_boundary")]
)
@pytest.mark.parametrize("offset_type", ["start", "first", "center", "last", "end"])
def test_every_choice_of_a_stripe_renders_and_runs(tmp_path, kind, flag, offset_type):
    strap = {
        "layer": "m4",
        "width": "0.5 um",
        "pitch": "12 um",
        "offset": "1 um",
        "extend": kind,
        "offset_type": offset_type,
    }
    flow = place_flow(tmp_path, {"pdn": {"grid": {"straps": [strap]}}})
    line = [l for l in stage(render(flow), "xeda_pdn") if l.startswith("add_pdn_stripe")][1]
    assert flag in line and f"-offset_type {offset_type.upper()}" in line
    run_script(flow)


def test_a_stripe_of_the_platform_has_no_option(tmp_path):
    """The stripes of the platform keep the form they have in the golden script."""
    found = grid_lines(tmp_path, {}, "add_pdn_stripe")
    assert found == [l for l in PDN_LINES if l.startswith("add_pdn_stripe")]


@pytest.mark.parametrize(
    "strap",
    [
        {"layer": "m4", "width": "0 um", "pitch": "12 um", "offset": "1 um"},
        {"layer": "m4", "width": "1 um", "pitch": "0 um", "offset": "1 um"},
        {"layer": "m4", "width": "1 um", "pitch": "12 um", "offset": "-1 um"},
        {"layer": "m4", "width": "1 um", "pitch": "12 um", "offset": "1 um", "spacing": "0 um"},
        {"layer": "m4", "width": "1 um", "pitch": "12 um", "offset": "1 um", "number_of_straps": 0},
        {"layer": "m4", "width": "1 um", "pitch": "12 um", "offset": "1 um", "extend": "pads"},
        {
            "layer": "m4",
            "width": "1 um",
            "pitch": "12 um",
            "offset": "1 um",
            "offset_type": "FIRST",
        },
        {
            "layer": "m4",
            "width": "1 um",
            "pitch": "12 um",
            "offset": "1 um",
            "starts_with": "POWER",
        },
        {"layer": "m4", "width": "1 um", "pitch": "12 um", "offset": "1 um", "spacing": "2"},
        {"layer": "m4", "width": 1, "pitch": "12 um", "offset": "1 um"},
    ],
)
def test_a_malformed_stripe_is_refused(tmp_path, strap):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"pdn.grid": {"straps": [strap]}})


RING = {"layers": ["m3", "m4"], "widths": "1 um", "spacings": "1 um", "core_offset": "0.5 um"}


def test_a_ring_surrounds_the_core(tmp_path):
    found = grid_lines(tmp_path, {"grid": {"rings": [RING]}}, "add_pdn_ring")
    assert found == [
        'add_pdn_ring -grid grid -layers [list "m3" "m4"] -widths 1.0 -spacings 1.0 '
        "-core_offsets 0.5"
    ]
    run_script(place_flow(tmp_path / "run", {"pdn": {"grid": {"rings": [RING]}}}))


def test_the_options_of_a_ring_are_passed(tmp_path):
    ring = {
        "layers": ["m3", "m4"],
        "widths": ["1 um", "2 um"],
        "spacings": ["0.5 um", "0.75 um"],
        "core_offset": ["1 um", "2 um", "3 um", "4 um"],
        "add_connect": True,
        "extend_to_boundary": True,
        "nets": ["VDD"],
        "starts_with": "ground",
        "allow_out_of_die": True,
    }
    found = grid_lines(tmp_path, {"grid": {"rings": [ring]}}, "add_pdn_ring")
    assert found == [
        'add_pdn_ring -grid grid -layers [list "m3" "m4"] -widths [list 1.0 2.0] '
        "-spacings [list 0.5 0.75] -core_offsets [list 1.0 2.0 3.0 4.0] -add_connect "
        '-extend_to_boundary -nets [list "VDD"] -starts_with GROUND -allow_out_of_die'
    ]
    run_script(place_flow(tmp_path / "run", {"pdn": {"grid": {"rings": [ring]}}}))


def test_the_rings_come_after_the_stripes_and_before_the_connections(tmp_path):
    flow = place_flow(tmp_path, {"pdn": {"grid": {"rings": [RING]}}})
    found = names(run_script(flow), "add_pdn_stripe", "add_pdn_ring", "add_pdn_connect")
    assert found == ["add_pdn_stripe"] * 3 + ["add_pdn_ring"] + ["add_pdn_connect"] * 2


@pytest.mark.parametrize(
    "ring",
    [
        {**RING, "layers": ["m3"]},
        {**RING, "layers": ["m3", "m4", "m5"]},
        {**RING, "widths": "0 um"},
        {**RING, "widths": ["1 um", "2 um", "3 um"]},
        {**RING, "spacings": "-1 um"},
        {**RING, "core_offset": ["1 um", "2 um"]},
        {**RING, "core_offset": "-1 um"},
        {k: v for k, v in RING.items() if k != "core_offset"},
        {**RING, "pad_offsets": 1},
    ],
)
def test_a_malformed_ring_is_refused(tmp_path, ring):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"pdn.grid": {"rings": [ring]}})


def test_a_connection_is_a_pair_of_layers_or_an_object_with_options(tmp_path):
    connects = [
        ["m1", "m3"],
        {
            "layers": ["m3", "m4"],
            "cut_pitch": ["0.2 um", "0.3 um"],
            "fixed_vias": ["via34"],
            "dont_use_vias": ["bad*"],
            "max_rows": 2,
            "max_columns": 3,
            "ongrid": ["m3"],
            "min_width_layers": ["m2"],
            "split_cuts": {"m3": "0.38 um", "m4": "0.5 um"},
            "split_cuts_staggered": True,
        },
        {"layers": ["m4", "m5"], "cut_pitch": "0.2 um"},
    ]
    found = grid_lines(tmp_path, {"grid": {"connects": connects}}, "add_pdn_connect")
    assert found == [
        'add_pdn_connect -grid grid -layers [list "m1" "m3"]',
        'add_pdn_connect -grid grid -layers [list "m3" "m4"] -cut_pitch [list 0.2 0.3] '
        '-fixed_vias [list "via34"] -dont_use_vias [list "bad*"] -max_rows 2 -max_columns 3 '
        '-ongrid [list "m3"] -min_width_layers [list "m2"] -split_cuts [list "m3" 0.38 "m4" 0.5] '
        "-split_cuts_staggered",
        'add_pdn_connect -grid grid -layers [list "m4" "m5"] -cut_pitch 0.2',
    ]
    run_script(place_flow(tmp_path / "run", {"pdn": {"grid": {"connects": connects}}}))


def test_the_connections_of_the_platform_have_no_option(tmp_path):
    found = grid_lines(tmp_path, {}, "add_pdn_connect")
    assert found == [l for l in PDN_LINES if l.startswith("add_pdn_connect")]


@pytest.mark.parametrize(
    "connect",
    [
        ["m1"],
        {"layers": ["m1", "m2"], "max_rows": 0},
        {"layers": ["m1", "m2"], "cut_pitch": "0 um"},
        {"layers": ["m1", "m2"], "cut_pitch": ["1 um", "2 um", "3 um"]},
        {"layers": ["m1", "m2"], "split_cuts": {"m1": "0 um"}},
        {"layers": ["m1", "m2"], "split_cuts": {"m1": 1}},
        {"layers": ["m1", "m2"], "unknown": 1},
    ],
)
def test_a_malformed_connection_is_refused(tmp_path, connect):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"pdn.grid": {"connects": [connect]}})


def test_split_cuts_are_staggered_only_when_there_are_split_cuts(tmp_path):
    message = refusal(
        tmp_path,
        typed={"pdn.grid": {"connects": [{"layers": ["m1", "m2"], "split_cuts_staggered": True}]}},
    )
    assert "split_cuts_staggered" in message and "split_cuts" in message


def test_the_trim_and_the_pins_of_the_generator_follow_the_settings(tmp_path):
    assert grid_lines(tmp_path, {}, "pdngen") == ["pdngen"]
    assert grid_lines(tmp_path, {"skip_trim": True}, "pdngen") == ["pdngen -skip_trim"]
    assert grid_lines(tmp_path, {"block_pins": False}, "pdngen") == ["pdngen -dont_add_pins"]
    both = grid_lines(tmp_path, {"skip_trim": True, "block_pins": False}, "pdngen")
    assert both == ["pdngen -skip_trim -dont_add_pins"]


def test_the_power_grid_is_not_checked_by_default(tmp_path):
    """`check_power_grid` stops on a design without pads unless it gets
    `-dont_require_terminals`, and OpenROAD-flow-scripts does not run it."""
    assert grid_lines(tmp_path, {}, "check_power_grid") == []


def test_a_failing_check_of_the_power_grid_stops_the_flow(tmp_path, monkeypatch):
    flow = place_flow(tmp_path, {"pdn": {"check": {"mode": "fail"}}})
    body = stage(render(flow), "xeda_pdn")
    assert body[-2:] == [
        'check_power_grid -net "VDD" -dont_require_terminals',
        'check_power_grid -net "VSS" -dont_require_terminals',
    ]
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "check_power_grid")
    require_tclsh()
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_place")
    assert done.returncode != 0
    assert (flow.run_path / "openroad_place-failed.odb").is_file()


def test_a_warning_check_of_the_power_grid_goes_on_after_a_failure(tmp_path, monkeypatch):
    flow = place_flow(tmp_path, {"pdn": {"check": {"mode": "warn"}}})
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "check_power_grid")
    calls = run_script(flow)
    assert names(calls, "check_power_grid") == ["check_power_grid"] * 2
    assert (flow.run_path / "place.odb").is_file()


def test_the_check_of_the_power_grid_can_require_terminals(tmp_path):
    found = grid_lines(
        tmp_path, {"check": {"mode": "warn", "require_terminals": True}}, "check_power_grid"
    )
    assert found == []  # the call is inside a catch
    body = stage(
        render(
            place_flow(tmp_path, {"pdn": {"check": {"mode": "fail", "require_terminals": True}}})
        ),
        "xeda_pdn",
    )
    assert body[-2:] == ['check_power_grid -net "VDD"', 'check_power_grid -net "VSS"']


@pytest.mark.parametrize("mode", ["off", "warn", "fail"])
def test_every_mode_of_the_check_of_the_power_grid_renders_and_runs(tmp_path, mode):
    calls = run_script(place_flow(tmp_path, {"pdn": {"check": {"mode": mode}}}))
    assert len(names(calls, "check_power_grid")) == (0 if mode == "off" else 2)


@pytest.mark.parametrize("mode", ["generated", "none"])
def test_every_mode_of_the_power_grid_renders_and_runs(tmp_path, mode):
    run_script(place_flow(tmp_path, {"pdn": {"mode": mode}}))


@pytest.mark.parametrize(
    "leaf, value",
    [
        ("secondary_power", ["VDDA"]),
        ("skip_trim", True),
        ("block_pins", False),
        ("check", {"mode": "warn"}),
    ],
)
def test_no_grid_option_has_an_effect_without_a_grid(tmp_path, leaf, value):
    message = refusal(tmp_path, "pdn.mode=none", typed={f"pdn.{leaf}": value})
    assert "no effect" in message and "pdn" in message


def test_the_terminals_of_the_check_matter_only_when_it_runs(tmp_path):
    message = refusal(tmp_path, "pdn.check.require_terminals=true")
    assert "pdn.check.require_terminals" in message and "no effect" in message


def test_a_stripe_of_the_platform_and_the_settings_have_one_shape(tmp_path):
    """The templates read the options of every stripe, also of those that the platform gives."""
    flow = place_flow(tmp_path, {"pdn": {"grid": {"rings": [RING]}}})
    grid = effective(flow.settings, flow.platform_model).pdn
    assert all(hasattr(strap, "snap_to_grid") for strap in grid.straps)
    assert all(hasattr(connect, "cut_pitch") for connect in grid.connects)


def test_the_power_grid_mode_none_builds_no_grid(tmp_path):
    flow = place_flow(tmp_path, {"pdn": {"mode": "none"}}, model={"pdn": Pdn()})
    assert stage(render(flow), "xeda_pdn") == ["xeda_stage pdn"]
    assert not names(run_script(flow), "pdngen", "define_pdn_grid")


def test_a_platform_without_a_power_grid_is_refused_naming_the_way_out(tmp_path):
    flow = place_flow(tmp_path, model={"pdn": Pdn()})
    with pytest.raises(FlowSettingsException, match=r"power_net.*pdn\.grid.*pdn\.mode: none"):
        effective(flow.settings, flow.platform_model)
    pdn = Pdn(power_net="VDD", ground_net="VSS")
    with pytest.raises(FlowSettingsException, match=r"rails or straps"):
        effective(flow.settings, flow.platform_model.model_copy(update={"pdn": pdn}))


# ------------------------------------------------------------------------------ platform data


def test_the_effective_values_are_a_pure_function_of_the_settings_and_the_model(tmp_path):
    flow = place_flow(tmp_path)
    values = effective(flow.settings, flow.platform_model)
    assert isinstance(values, EffectivePlace)
    assert values == flow.effective()
    assert values.site == "tinysite"
    assert values.tie_low == CellPort(cell="TIELO_X1", port="Z")
    assert values.tie_high == CellPort(cell="TIEHI_SMALL", port="Z")
    assert values.signal_layers == "m2-m4" and values.clock_layers == "m3-m4"
    assert values.layer_adjustments == {"m2-m3": 0.5, "m4": 0.25}
    assert (values.tapcell_mode, values.tap_distance) == ("tap_endcap", 12.0)
    assert (values.tap_master, values.endcap_master) == ("TAP_X1", "ENDCAP_X1")
    assert values.pdn.power_net == "VDD" and len(values.pdn.straps) == 2
    assert values.time_unit == "1ns"
    assert values.liberty == tuple(flow.platform_model.files["liberty"])
    assert flow.settings.pdn.grid is None  # not changed


def test_a_grid_of_the_settings_does_not_change_the_platform_or_the_settings(tmp_path):
    grid = {"straps": [{"layer": "m3", "width": "0.3 um", "pitch": "4 um", "offset": "2 um"}]}
    flow = place_flow(tmp_path, {"pdn": {"grid": grid}})
    before = flow.platform_model.pdn.model_copy(deep=True)
    values = effective(flow.settings, flow.platform_model)
    assert values.pdn.straps == [PdnStripe(layer="m3", width="0.3 um", pitch="4 um", offset="2 um")]
    assert flow.platform_model.pdn == before
    assert len(flow.settings.pdn.grid.straps) == 1


def test_a_floorplan_by_utilization_needs_the_site_of_the_platform(tmp_path):
    flow = place_flow(tmp_path)
    model = flow.platform_model.model_copy(update={"site": None})
    with pytest.raises(FlowSettingsException) as error:
        effective(flow.settings, model)
    message = " ".join(str(error.value).split())
    assert "tiny" in message and "site" in message
    assert "floorplan.sizing: def" in message
    flow.settings.floorplan.sizing = "def"
    flow.settings.floorplan.def_file = Path("plan.def")
    assert effective(flow.settings, model).site is None


def test_tie_fanout_needs_the_tie_cells_of_the_platform(tmp_path):
    flow = place_flow(tmp_path)
    model = flow.platform_model.model_copy(update={"tie_high": None})
    with pytest.raises(FlowSettingsException) as error:
        effective(flow.settings, model)
    message = " ".join(str(error.value).split())
    assert "tie_high" in message and "tie.repair_fanout: false" in message
    flow.settings.tie.repair_fanout = False
    assert effective(flow.settings, model).tie_high is None


# ------------------------------------------------------------------------------ the placement

DEFAULT_GLOBAL_PLACEMENT = (
    "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -routability_driven "
    "-timing_driven -force_center_initial_place"
)


def global_placement_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[0] == "global_placement"]


def test_the_density_is_the_density_of_the_platform_by_default(tmp_path):
    assert stage(render(place_flow(tmp_path)), "xeda_density") == ["return 0.6"]


def test_a_density_in_the_settings_replaces_the_density_of_the_platform(tmp_path):
    flow = place_flow(tmp_path, {"global_placement": {"density": {"value": 0.45}}})
    assert stage(render(flow), "xeda_density") == ["return 0.45"]


def test_a_platform_density_is_needed_for_the_fixed_density(tmp_path):
    flow = place_flow(tmp_path, model={"place_density": None})
    with pytest.raises(FlowSettingsException, match=r"place_density.*global_placement\.density"):
        flow.effective()
    flow = place_flow(tmp_path / "given", {"global_placement": {"density": {"value": 0.5}}})
    assert flow.effective().density_value == 0.5


def test_the_uniform_density_asks_the_tool_for_the_uniform_density(tmp_path):
    flow = place_flow(tmp_path, {"global_placement": {"density": {"mode": "uniform"}}})
    assert stage(render(flow), "xeda_density") == ["return uniform"]
    assert flow.effective().density_value is None


def test_the_automatic_density_is_computed_again_at_each_placement(tmp_path):
    """The netlist changes between the placements (the buffers go and come), so the density is
    a procedure that the script calls at each placement, never a number that it keeps."""
    settings = {
        "global_placement": {
            "cell_padding": 2,
            "density": {"mode": "auto", "headroom": 0.2},
        }
    }
    flow = place_flow(tmp_path, settings)
    script = render(flow)
    assert stage(script, "xeda_density")[0] == (
        "set lower_bound [gpl::get_global_placement_uniform_density -pad_left 2 -pad_right 2]"
    )
    assert "return $density" in stage(script, "xeda_density")
    calls = run_script(flow, script)
    placements = global_placement_calls(calls)
    assert len(placements) == 2
    assert len(names(calls, "gpl::get_global_placement_uniform_density")) == len(placements)
    for placement in placements:
        assert placement[placement.index("-density") + 1] == "0.61"


def test_an_automatic_density_above_one_stops_the_script(tmp_path, monkeypatch):
    require_tclsh()
    fake_returns(monkeypatch, {("gpl::get_global_placement_uniform_density",): "0.995"})
    settings = {"global_placement": {"density": {"mode": "auto", "headroom": 0.5}}}
    flow = place_flow(tmp_path, settings)
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_place")
    assert done.returncode != 0
    output = done.stderr + done.stdout
    assert "above 1.0" in output
    assert "global_placement.density.headroom" in output
    assert "floorplan.core_utilization_percent" in output


def test_the_cells_are_placed_without_the_pins_first(tmp_path):
    flow = place_flow(tmp_path)
    assert stage(render(flow), "xeda_global_place_skip_io") == [
        "xeda_stage globalplace_skip_io",
        "if {![all_pins_placed]} {",
        "global_placement -skip_io -density [xeda_density] -pad_left 0 -pad_right 0",
        "}",
    ]


def test_the_pass_without_the_pins_is_skipped_when_all_pins_are_placed(tmp_path, monkeypatch):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})
    calls = run_script(place_flow(tmp_path))
    assert len(global_placement_calls(calls)) == 1
    assert all("-skip_io" not in call for call in global_placement_calls(calls))


def test_the_seed_reaches_both_placements(tmp_path):
    flow = place_flow(tmp_path, {"global_placement": {"random_seed": 7}})
    script = render(flow)
    for name in ("xeda_global_place_skip_io", "xeda_global_place"):
        assert any(line.endswith('-random_seed "7"') for line in stage(script, name)), name


def test_the_pins_are_placed_on_the_layers_of_the_platform(tmp_path):
    flow = place_flow(tmp_path)
    script = render(flow)
    assert stage(script, "xeda_place_pins") == [
        'place_pins -hor_layers [list "m3"] -ver_layers [list "m4"]'
    ]
    assert stage(script, "xeda_io_placement") == ["xeda_stage io_placement", "xeda_place_pins"]


def test_the_settings_replace_the_layers_of_the_pins_and_add_the_distances(tmp_path):
    io = {
        "horizontal_layers": ["m2", "m4"],
        "vertical_layers": ["m3"],
        "min_distance_tracks": 4,
        "corner_avoidance": "1.5 um",
    }
    flow = place_flow(tmp_path, {"io": io})
    (line,) = stage(render(flow), "xeda_place_pins")
    assert line == (
        'place_pins -hor_layers [list "m2" "m4"] -ver_layers [list "m3"] '
        '-min_distance_in_tracks -min_distance 4 -corner_avoidance "1.5"'
    )
    run_script(flow)


def test_the_distance_of_the_pins_is_a_length_or_a_whole_number_of_tracks(tmp_path):
    um = place_flow(tmp_path, {"io": {"min_distance": "2500 nm"}})
    assert stage(render(um), "xeda_place_pins")[0].endswith("-min_distance 2.5")
    tracks = place_flow(tmp_path / "t", {"io": {"min_distance_tracks": 3}})
    assert (
        " -min_distance_in_tracks -min_distance 3 "
        in stage(render(tracks), "xeda_place_pins")[0] + " "
    )
    for number, given in enumerate(("io.min_distance_tracks=2.5", "io.min_distance_tracks=-1")):
        assert "min_distance_tracks" in refusal(tmp_path / f"r{number}", given)
    message = refusal(tmp_path / "b", "io.min_distance=2 um", "io.min_distance_tracks=3")
    assert "io.min_distance" in message and "delete one of them" in message


def test_the_pins_use_the_plural_flags_and_never_the_obsolete_random_flag(tmp_path):
    (line,) = stage(render(place_flow(tmp_path)), "xeda_place_pins")
    assert "-hor_layers" in line and "-ver_layers" in line
    assert "-random" not in line and "-hor_layer " not in line


def test_the_pins_are_placed_between_the_two_placements(tmp_path):
    names_in_order = names(
        run_script(place_flow(tmp_path)), "global_placement", "place_pins", "buffer_ports"
    )
    assert names_in_order == ["global_placement", "place_pins", "buffer_ports", "global_placement"]


def test_concurrent_placement_of_the_pins_places_them_with_the_cells(tmp_path):
    """`global_placement -place_ios` reads the layers and the spacing of the pins from
    `set_place_config` and legalizes the pins itself, so no `place_pins` follows. The
    configuration is stored in the design, so the stage removes it after the placement."""
    flow = place_flow(
        tmp_path,
        {"io": {"flow": "concurrent", "min_distance": "3 um", "corner_avoidance": "1 um"}},
    )
    script = render(flow)
    assert "xeda_place_pins" not in script
    assert stage(script, "xeda_global_place_skip_io") == ["xeda_stage globalplace_skip_io"]
    assert stage(script, "xeda_io_placement") == ["xeda_stage io_placement"]
    body = stage(script, "xeda_global_place")
    configured = body.index(
        'set_place_config -io_pin_hor_layers [list "m3"] -io_pin_ver_layers [list "m4"] '
        '-io_pin_min_distance 3.0 -io_pin_corner_avoidance "1.0"'
    )
    (placement,) = [line for line in body if " -place_ios " in line]
    assert placement.startswith("set failed [catch { global_placement -density [xeda_density] ")
    assert placement.endswith(" } message options]")
    assert "-timing_driven" in placement and "-routability_driven" in placement
    assert configured < body.index(placement) < body.index("reset_place_config")
    calls = run_script(flow, script)
    assert names(
        calls, "set_place_config", "global_placement", "reset_place_config", "place_pins"
    ) == ["set_place_config", "global_placement", "reset_place_config"]


def test_concurrent_placement_of_the_pins_gives_the_spacing_in_tracks(tmp_path):
    flow = place_flow(tmp_path, {"io": {"flow": "concurrent", "min_distance_tracks": 3}})
    (line,) = [
        line
        for line in stage(render(flow), "xeda_global_place")
        if line.startswith("set_place_config ")
    ]
    assert line.endswith("-io_pin_min_distance_in_tracks -io_pin_min_distance 3")


def fails_under_the_fake(flow: OpenroadPlace, script: str) -> str:
    """Run `script` as the driver of `flow` under the fake OpenROAD, insist that it fails and
    writes the failed database, and return its output."""
    require_tclsh()
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, script, driver="openroad_place")
    assert done.returncode != 0
    assert (flow.run_path / "openroad_place-failed.odb").is_file()
    assert not (flow.run_path / "place.odb").exists()
    return done.stdout + done.stderr


def recorded(flow: OpenroadPlace, *wanted: str) -> list[str]:
    """The names of the commands among `wanted` that the fake OpenROAD recorded before the script
    failed (`fake_calls` fails a test on the recorded error)."""
    found = []
    words = (flow.run_path / "fake_openroad.calls").read_text().splitlines()
    for line, following in zip(words, words[1:]):
        if line.startswith("CALL ") and following.startswith("ARG "):
            found.append(following[4:])
    return [name for name in found if name in wanted]


def test_concurrent_placement_of_the_pins_stops_on_an_openroad_without_the_command(tmp_path):
    flow = place_flow(tmp_path, {"io": {"flow": "concurrent"}})
    script = render(flow)
    body = stage(script, "xeda_global_place")
    check = body.index('if {[info commands set_place_config] eq ""} {')
    assert check == 1  # before the stage changes the design
    # an OpenROAD older than 26Q4-11, which has no `set_place_config`
    output = fails_under_the_fake(flow, "rename set_place_config {}\n" + script)
    assert "26Q4-11" in output and "io.flow: skip_io_pass" in output
    assert recorded(flow, "set_place_config", "global_placement") == []


def test_a_failed_concurrent_placement_removes_the_configuration_of_the_pins(tmp_path, monkeypatch):
    """`set_place_config` stores the configuration in the design, and the failed database holds
    the design, so the stage removes the configuration before it passes the error on."""
    flow = place_flow(tmp_path, {"io": {"flow": "concurrent"}})
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "global_placement")
    fails_under_the_fake(flow, render(flow))
    calls = recorded(flow, "global_placement", "reset_place_config", "write_db")
    assert calls == ["global_placement", "reset_place_config", "write_db"]


def test_the_annealing_of_the_pin_placer_is_refused_with_the_concurrent_placement(tmp_path):
    message = refusal(tmp_path, "io.flow=concurrent", "io.algorithm=annealing")
    assert "io.algorithm" in message and "io.flow: skip_io_pass" in message
    message = refusal(tmp_path / "schedule", "io.flow=concurrent", "io.annealing.temperature=3")
    assert "io.algorithm: annealing" in message and "io.flow: skip_io_pass" in message


def test_the_annealing_of_the_pin_placer_is_refused_with_a_floorplan_from_a_def_file(tmp_path):
    """No flow anneals the pins of a DEF floorplan, so the message does not offer
    `io.flow: concurrent`, which refuses the annealing too."""
    def_texts = ["floorplan.sizing=def", "floorplan.def_file=plan.def"]
    for flow in ("skip_io_pass", "concurrent"):
        message = refusal(tmp_path / flow, *def_texts, f"io.flow={flow}", "io.algorithm=annealing")
        assert "floorplan.sizing: def" in message and "delete `io.algorithm`" in message
        assert "set `io.flow" not in message


def test_concurrent_placement_of_the_pins_sets_the_rules_of_the_pins_before_it(tmp_path):
    io = {
        "flow": "concurrent",
        "groups": [{"pins": ["a", "b"]}],
        "constraints": [{"direction": "input", "region": {"edge": "top"}}],
        "mirrored_pins": [["c", "d"]],
        "pin_length": {"horizontal": "0.5 um"},
        "exclude": [{"edge": "left"}],
    }
    flow = place_flow(tmp_path, {"io": io})
    rules = ["exclude_io_pin_region", "set_io_pin_constraint", "set_pin_length"]
    calls = names(run_script(flow), *rules, "set_place_config", "global_placement")
    assert calls[-2:] == ["set_place_config", "global_placement"]
    assert set(calls[:-2]) == set(rules)


def test_the_pins_stay_where_they_are_with_the_fixed_flow(tmp_path, monkeypatch):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})
    flow = place_flow(tmp_path, {"io": {"flow": "fixed"}})
    script = render(flow)
    assert "xeda_place_pins" not in script
    calls = run_script(flow, script)
    assert names(calls, "place_pins") == []
    assert all("-skip_io" not in call for call in global_placement_calls(calls))


def test_a_floorplan_from_a_def_file_keeps_its_pins(tmp_path, monkeypatch):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    flow = place_flow(
        tmp_path, {"floorplan": {"sizing": "def", "def_file": "plan.def"}}, model={"site": None}
    )
    script = render(flow)
    assert stage(script, "xeda_global_place_skip_io") == ["xeda_stage globalplace_skip_io"]
    assert stage(script, "xeda_io_placement")[0] == "xeda_stage io_placement"
    assert names(run_script(flow, script), "place_pins") == []


def test_timing_driven_placement_removes_the_buffers_before_it_buffers_the_ports(tmp_path):
    body = stage(render(place_flow(tmp_path)), "xeda_global_place")
    assert body == [
        "xeda_stage globalplace",
        "remove_buffers",
        "buffer_ports",
        DEFAULT_GLOBAL_PLACEMENT,
        "estimate_parasitics -placement",
        "xeda_metrics 0 0",
    ]


def test_no_buffers_are_removed_without_timing_driven_placement_or_when_switched_off(tmp_path):
    off = {"global_placement": {"timing_driven": {"enabled": False}}}
    assert "remove_buffers" not in stage(render(place_flow(tmp_path, off)), "xeda_global_place")
    keep = {"resize": {"remove_buffers": False}}
    flow = place_flow(tmp_path / "keep", keep)
    assert "remove_buffers" not in stage(render(flow), "xeda_global_place")


def test_the_ports_are_not_buffered_when_switched_off(tmp_path):
    flow = place_flow(tmp_path, {"resize": {"buffer_ports": {"enabled": False}}})
    assert "buffer_ports" not in stage(render(flow), "xeda_global_place")


def test_the_buffers_are_removed_in_the_global_placement_and_not_in_the_floorplan(tmp_path):
    """The default cleanup removes the buffers with the `unbuffer` move of `repair_timing`, and
    the removal of buffers runs in the global placement. The tie cells are repaired first."""
    script = render(place_flow(tmp_path))
    assert "remove_buffers" not in stage(script, "xeda_floorplan")
    assert "remove_buffers" in stage(script, "xeda_global_place")


def test_the_placement_modes_follow_their_settings(tmp_path):
    settings = {
        "global_placement": {
            "cell_padding": 1,
            "timing_driven": {"enabled": False},
            "routability_driven": {"enabled": False},
            "initial_place": {"from_center": False},
            "nesterov": {"min_phi_coef": 0.9, "max_phi_coef": 1.1},
        }
    }
    body = stage(render(place_flow(tmp_path, settings)), "xeda_global_place")
    assert (
        "global_placement -density [xeda_density] -pad_left 1 -pad_right 1 "
        '-min_phi_coef "0.9" -max_phi_coef "1.1"'
    ) in body


def test_the_parasitics_of_the_placement_are_estimated_after_each_change(tmp_path):
    """Before the global route, every estimate is made from the placement."""
    script = render(place_flow(tmp_path))
    for name in ("xeda_global_place", "xeda_resize", "xeda_detail_place"):
        body = stage(script, name)
        assert "estimate_parasitics -placement" in body, name
    assert "-global_routing" not in script


# ------------------------------------------------------------------------------ the global placement: options


def main_placement(flow: OpenroadPlace) -> str:
    """The command of the main global placement in the rendered script."""
    (line,) = [
        line for line in stage(render(flow), "xeda_global_place") if line.startswith("global_pl")
    ]
    return line


def gp_flow(tmp_path: Path, **leaves: Any) -> OpenroadPlace:
    return place_flow(tmp_path, {"global_placement": leaves})


def test_the_timing_driven_options_follow_the_mode_flag(tmp_path):
    flow = gp_flow(
        tmp_path,
        timing_driven={
            "reweight_overflow_percent": [60, 30],
            "net_weight_max": 4.0,
            "nets_percent": 12.5,
            "keep_resize_below_overflow": 0.3,
            "repair_timing": True,
            "repair_tns_end_percent": 2.0,
            "virtual_cts": True,
            "virtual_cts_max_skew_fraction": 0.2,
        },
    )
    assert main_placement(flow) == (
        "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -routability_driven "
        '-timing_driven -timing_driven_net_reweight_overflow [list "60" "30"] '
        '-timing_driven_net_weight_max "4.0" -timing_driven_nets_percentage "12.5" '
        '-keep_resize_below_overflow "0.3" -timing_driven_repair_timing '
        '-timing_driven_repair_tns_end_percent "2.0" -virtual_cts '
        '-virtual_cts_max_skew_fraction "0.2" -force_center_initial_place'
    )
    run_script(flow)


def test_the_placement_without_pins_takes_only_the_density_the_padding_and_the_seeds(tmp_path):
    flow = gp_flow(
        tmp_path,
        cell_padding=1,
        random_seed=3,
        perturb_distance="0.4 um",
        bin_grid_count=128,
        incremental=True,
        congestion_heatmap=True,
        timing_driven={"net_weight_max": 4.0},
        nesterov={"overflow": 0.2},
    )
    assert stage(render(flow), "xeda_global_place_skip_io")[2] == (
        "global_placement -skip_io -density [xeda_density] -pad_left 1 -pad_right 1 "
        '-random_seed "3" -perturb_dist 400.0'
    )


@pytest.mark.parametrize(
    "leaves, expected",
    [
        ({"net_weight_max": 4.0}, "has no effect while `global_placement.timing_driven.enabled`"),
    ],
)
def test_a_timing_driven_option_has_no_effect_without_the_mode(tmp_path, leaves, expected):
    off = {"enabled": False, **leaves}
    message = refusal(tmp_path, typed={"global_placement.timing_driven": off})
    assert expected in message


@pytest.mark.parametrize(
    "leaf, value, needs",
    [
        ("repair_tns_end_percent", 2.0, "repair_timing"),
        ("virtual_cts_max_skew_fraction", 0.2, "virtual_cts"),
    ],
)
def test_a_timing_driven_option_needs_the_experiment_it_belongs_to(tmp_path, leaf, value, needs):
    key = f"global_placement.timing_driven.{leaf}"
    message = refusal(tmp_path, f"{key}={value}")
    assert f"`{key}`" in message and f"timing_driven.{needs}: true" in message
    plan(tmp_path / "ok", f"{key}={value}", f"global_placement.timing_driven.{needs}=true")


@pytest.mark.parametrize(
    "key, value",
    [
        ("timing_driven.reweight_overflow_percent", [0]),
        ("timing_driven.reweight_overflow_percent", [101]),
        ("timing_driven.reweight_overflow_percent", []),
        ("timing_driven.reweight_overflow_percent", [1.5]),
        ("timing_driven.net_weight_max", 0),
        ("timing_driven.nets_percent", 101),
        ("timing_driven.nets_percent", -1),
        ("timing_driven.keep_resize_below_overflow", 1.1),
        ("timing_driven.repair_tns_end_percent", 100.5),
        ("timing_driven.virtual_cts_max_skew_fraction", 1.5),
        ("routability_driven.target_rc_metric", 0),
        ("routability_driven.check_overflow", 1.5),
        ("routability_driven.snapshot_overflow", -0.1),
        ("routability_driven.max_density", 0),
        ("routability_driven.max_density", 1.5),
        ("routability_driven.inflation_ratio_coef", 0),
        ("routability_driven.max_inflation_ratio", -1),
        ("routability_driven.min_congestion_for_inflation", 2),
        ("routability_driven.max_inflation_total", 0),
        ("routability_driven.net_weight_max", 0),
        ("routability_driven.congested_nets_percent", 101),
        ("routability_driven.rc_coefficients", [1, 1, 0]),
        ("routability_driven.rc_coefficients", [1, 1, 0, -1]),
        ("routability_driven.estimator", "Grt"),
        ("initial_place.max_iter", -1),
        ("initial_place.max_fanout", 0),
        ("nesterov.init_density_penalty", 1e-7),
        ("nesterov.init_density_penalty", 1e7),
        ("nesterov.init_wirelength_coef", 0),
        ("nesterov.reference_hpwl", -1),
        ("nesterov.overflow", 1.1),
        ("bin_grid_count", 1),
        ("perturb_distance", "-1 um"),
        ("flop_clustering.tray_weight", -1),
        ("flop_clustering.timing_weight", -0.5),
        ("flop_clustering.max_split_size", -2),
        ("flop_clustering.num_paths", -1),
        ("flop_clustering.clock_power_weight", -1),
    ],
)
def test_the_options_of_the_global_placement_keep_their_ranges(tmp_path, key, value):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={f"global_placement.{key}": value})


@pytest.mark.parametrize("estimator, flag", [("rudy", ""), ("grt", " -routability_use_grt")])
def test_every_congestion_estimator_renders_and_runs(tmp_path, estimator, flag):
    flow = gp_flow(tmp_path, routability_driven={"estimator": estimator})
    assert main_placement(flow) == (
        "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 "
        f"-routability_driven{flag} -timing_driven -force_center_initial_place"
    )
    run_script(flow)


def test_the_routability_driven_options_follow_the_mode_flag(tmp_path):
    flow = gp_flow(
        tmp_path,
        routability_driven={
            "target_rc_metric": 1.02,
            "check_overflow": 0.4,
            "snapshot_overflow": 0.7,
            "max_density": 0.9,
            "inflation_ratio_coef": 2.5,
            "max_inflation_ratio": 3.5,
            "min_congestion_for_inflation": 0.9,
            "max_inflation_total": 0.2,
            "net_weight_max": 1.5,
            "congested_nets_percent": 2.0,
            "rc_coefficients": [1, 1, 0.5, 0],
        },
    )
    assert main_placement(flow) == (
        "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -routability_driven "
        '-routability_target_rc_metric "1.02" -routability_check_overflow "0.4" '
        '-routability_snapshot_overflow "0.7" -routability_max_density "0.9" '
        '-routability_inflation_ratio_coef "2.5" -routability_max_inflation_ratio "3.5" '
        '-routability_min_congestion_for_inflation "0.9" -routability_max_inflation_total "0.2" '
        '-routability_net_weight_max "1.5" -routability_congested_nets_percentage "2.0" '
        '-routability_rc_coefficients [list "1.0" "1.0" "0.5" "0.0"] -timing_driven '
        "-force_center_initial_place"
    )
    run_script(flow)


def test_a_routability_option_has_no_effect_without_the_mode(tmp_path):
    off = {"enabled": False, "estimator": "grt"}
    message = refusal(tmp_path, typed={"global_placement.routability_driven": off})
    assert "global_placement.routability_driven.estimator" in message
    assert "global_placement.routability_driven.enabled" in message


def test_the_initial_placement_and_the_solver_follow_their_settings(tmp_path):
    flow = gp_flow(
        tmp_path,
        initial_place={"from_center": False, "max_iter": 0, "max_fanout": 100},
        nesterov={
            "skip": True,
            "min_phi_coef": 0.9,
            "max_phi_coef": 1.1,
            "init_density_penalty": 1e-4,
            "init_wirelength_coef": 0.3,
            "reference_hpwl": 1000,
            "overflow": 0.07,
            "revert_on_divergence": False,
            "pin_density_adjust": False,
        },
    )
    assert main_placement(flow) == (
        "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -routability_driven "
        '-timing_driven -initial_place_max_iter "0" -initial_place_max_fanout "100" '
        '-skip_nesterov_place -min_phi_coef "0.9" -max_phi_coef "1.1" '
        '-init_density_penalty "0.0001" -init_wirelength_coef "0.3" -reference_hpwl "1000.0" '
        '-overflow "0.07" -disable_revert_if_diverge -disable_pin_density_adjust'
    )
    run_script(flow)


def test_the_solver_keeps_its_snapshots_and_the_pin_density_by_default(tmp_path):
    line = main_placement(gp_flow(tmp_path))
    assert "-disable_revert_if_diverge" not in line and "-disable_pin_density_adjust" not in line


def test_the_options_of_the_whole_placement_come_after_the_modes(tmp_path):
    flow = gp_flow(
        tmp_path,
        bin_grid_count=128,
        incremental=True,
        congestion_heatmap=True,
        random_seed=7,
        perturb_distance="0.25 um",
    )
    assert main_placement(flow).endswith(
        '-force_center_initial_place -random_seed "7" -bin_grid_count "128" -incremental '
        "-enable_routing_congestion -perturb_dist 250.0"
    )
    run_script(flow)


@pytest.mark.parametrize(
    "length, nanometers, micrometers",
    [
        ("0.4 um", "400.0", "0.4"),
        ("0 um", "0.0", "0.0"),
        ("0.0005 um", "0.5", "0.0005"),
        ("2 um", "2000.0", "2.0"),
        ("500 nm", "500.0", "0.5"),
        ("1 mm", "1000000.0", "1000.0"),
    ],
)
def test_a_length_is_written_in_the_unit_that_the_command_takes(length, nanometers, micrometers):
    assert to_nanometers(Length(length)) == nanometers
    assert to_micrometers(Length(length)) == micrometers


def test_a_list_of_lengths_is_a_list_word():
    assert to_micrometers([Length("1 um"), Length("2500 nm")]) == "[list 1.0 2.5]"
    assert to_micrometers((Length("0 um"),)) == "[list 0.0]"


@pytest.mark.parametrize("value", [1.0, 5, "1.0", "5 um", None, True, float("nan"), float("inf")])
def test_only_a_length_is_converted_as_a_length(value):
    for convert in (to_nanometers, to_micrometers):
        with pytest.raises(TypeError, match="a length with a unit"):
            convert(value)
    with pytest.raises(TypeError, match="a length with a unit"):
        to_micrometers([Length("1 um"), value])


@pytest.mark.parametrize(
    "perturbation, nanometers",
    [("0.4 um", "400.0"), ("0 um", "0.0"), ("0.0005 um", "0.5"), ("2 um", "2000.0")],
)
def test_the_perturbation_is_given_with_a_unit_and_passed_in_nanometers(
    tmp_path, perturbation, nanometers
):
    flow = gp_flow(tmp_path, perturb_distance=perturbation)
    assert main_placement(flow).endswith(f"-perturb_dist {nanometers}")
    flow.bind_template_values()
    template = flow.jinja_env.from_string("{{ value|nanometers }}|{{ value|micrometers }}")
    assert template.render(value=Length("0.4 um")) == "400.0|0.4"
    unset = gp_flow(tmp_path / "unset")
    assert "-perturb_dist" not in render(unset)


@pytest.mark.parametrize("count", [2, 64, 128, 1024])
def test_a_bin_grid_is_a_power_of_two(tmp_path, count):
    flow = gp_flow(tmp_path, bin_grid_count=count)
    assert f'-bin_grid_count "{count}"' in main_placement(flow)
    run_script(flow)


@pytest.mark.parametrize("count", [3, 100, 129])
def test_a_bin_grid_that_is_no_power_of_two_is_refused(tmp_path, count):
    message = refusal(tmp_path, f"global_placement.bin_grid_count={count}")
    assert "bin_grid_count" in message and "power of two" in message


def test_the_flops_are_clustered_after_the_placement_and_the_parasitics_are_estimated_again(
    tmp_path,
):
    flow = gp_flow(
        tmp_path,
        flop_clustering={
            "enabled": True,
            "tray_weight": 30.0,
            "timing_weight": 0.2,
            "max_split_size": -1,
            "num_paths": 5,
            "clock_power_weight": 1.5,
        },
    )
    body = stage(render(flow), "xeda_global_place")
    clustering = body.index(
        'cluster_flops -tray_weight "30.0" -timing_weight "0.2" -max_split_size "-1" '
        '-num_paths "5" -clock_power_weight "1.5"'
    )
    assert body[clustering - 1] == "estimate_parasitics -placement"
    assert body[clustering + 1 : clustering + 3] == [
        "estimate_parasitics -placement",
        "xeda_metrics 0 0",
    ]
    commands = [call[0] for call in run_script(flow)]
    last = len(commands) - 1 - commands[::-1].index("global_placement")
    assert commands[last : last + 4] == [
        "global_placement",
        "estimate_parasitics",
        "cluster_flops",
        "estimate_parasitics",
    ]


def test_the_flops_are_clustered_with_the_defaults_of_the_tool(tmp_path):
    flow = gp_flow(tmp_path, flop_clustering={"enabled": True})
    assert "cluster_flops" in stage(render(flow), "xeda_global_place")
    calls = run_script(flow)
    assert names(calls, "cluster_flops") == ["cluster_flops"]


def test_no_flops_are_clustered_by_default(tmp_path):
    assert "cluster_flops" not in render(place_flow(tmp_path))


def test_a_clustering_option_has_no_effect_when_no_flops_are_clustered(tmp_path):
    message = refusal(tmp_path, "global_placement.flop_clustering.tray_weight=30")
    assert "flop_clustering.tray_weight" in message and "flop_clustering.enabled" in message


def test_clusters_of_instances_are_set_before_the_main_placement(tmp_path):
    flow = gp_flow(tmp_path, clusters=[["u1", "u2"], ["ff*"]])
    body = stage(render(flow), "xeda_global_place")
    clusters = body.index('placement_cluster [list "u1" "u2"]')
    assert body[clusters + 1] == 'placement_cluster [list "ff*"]'
    assert body[clusters + 2].startswith("global_placement")
    assert not [
        line for line in stage(render(flow), "xeda_global_place_skip_io") if "cluster" in line
    ]
    assert names(run_script(flow), "placement_cluster", "global_placement") == [
        "global_placement",
        "placement_cluster",
        "placement_cluster",
        "global_placement",
    ]


@pytest.mark.parametrize("clusters", [[[]], [[""]], [["a"], []], "a", [["a", 1]]])
def test_a_malformed_cluster_is_refused(tmp_path, clusters):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"global_placement.clusters": clusters})


def test_the_placement_has_no_free_text_flags(tmp_path):
    """Every flag of `global_placement` that the flow does not own is a typed setting."""
    from xeda.flows.openroad.place_groups import GlobalPlacementGroup

    assert "extra_args" not in GlobalPlacementGroup.model_fields
    assert not GlobalPlacementGroup.EXTRA_FLAGS
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, "global_placement.extra_args=-skip_initial_place")


def test_the_resize_stage_repairs_the_design_after_the_placement_and_not_before(tmp_path):
    script = render(place_flow(tmp_path))
    assert not [line for line in stage(script, "xeda_floorplan") if "repair_design" in line]
    assert stage(script, "xeda_resize") == [
        "xeda_stage placeopt",
        "estimate_parasitics -placement",
        'repair_design -slew_margin "0.0" -cap_margin "0.0" -verbose',
        "report_floating_nets",
        "xeda_metrics 1 0",
    ]


def test_the_repair_of_the_design_follows_its_settings(tmp_path):
    repair = {
        "max_wire_length": "500 um",
        "slew_margin_percent": 10,
        "cap_margin_percent": 5,
        "match_cell_footprint": True,
    }
    flow = place_flow(tmp_path, {"resize": {"repair_design": repair}})
    body = stage(render(flow), "xeda_resize")
    assert (
        'repair_design -max_wire_length "500.0" -slew_margin "10.0" -cap_margin "5.0" '
        "-match_cell_footprint -verbose"
    ) in body


def test_the_repair_of_the_design_can_be_switched_off(tmp_path):
    flow = place_flow(tmp_path, {"resize": {"repair_design": {"enabled": False}}})
    assert not [line for line in stage(render(flow), "xeda_resize") if "repair_design" in line]


def test_the_repair_of_the_timing_after_the_placement_is_off_by_default_and_estimates_first(
    tmp_path,
):
    assert not [
        line
        for line in stage(render(place_flow(tmp_path)), "xeda_resize")
        if "repair_timing" in line
    ]
    flow = place_flow(
        tmp_path / "on",
        {
            "resize": {
                "repair_timing": {"enabled": True, "setup_margin": "0.05 ns", "tns_end_percent": 50}
            }
        },
        model={"time_unit": "1ps"},
    )
    body = stage(render(flow), "xeda_resize")
    repair = body.index(
        'repair_timing -setup_margin 50.0 -hold_margin 0.0 -repair_tns "50.0" -verbose'
    )
    assert body[repair - 1] == "estimate_parasitics -placement"
    design = next(i for i, line in enumerate(body) if line.startswith("repair_design"))
    assert design < repair < body.index("report_floating_nets")
    run_script(flow)


def test_the_ports_are_buffered_on_the_inputs_the_outputs_or_both(tmp_path):
    for ports, expected in (
        ({}, "buffer_ports"),
        ({"inputs": False}, "buffer_ports -outputs"),
        ({"outputs": False}, "buffer_ports -inputs"),
    ):
        flow = place_flow(
            tmp_path / expected.replace(" ", "_"), {"resize": {"buffer_ports": ports}}
        )
        body = stage(render(flow), "xeda_global_place")
        assert [line for line in body if line.startswith("buffer_ports")] == [expected]
        run_script(flow)


def test_the_buffer_of_the_ports_and_the_utilization_limit_go_to_the_command(tmp_path):
    settings = {"resize": {"max_utilization_percent": 85, "buffer_ports": {"cell": "BUF_X2"}}}
    body = stage(render(place_flow(tmp_path, settings)), "xeda_global_place")
    assert 'buffer_ports -buffer_cell "BUF_X2" -max_utilization "85.0"' in body


def test_the_utilization_limit_reaches_every_command_of_the_resizer_in_the_resize_stage(tmp_path):
    settings = {
        "resize": {"max_utilization_percent": 85, "repair_timing": {"enabled": True}},
    }
    flow = place_flow(tmp_path, settings)
    script = render(flow)
    (design,) = [l for l in stage(script, "xeda_resize") if l.startswith("repair_design")]
    (timing,) = [l for l in stage(script, "xeda_resize") if l.startswith("repair_timing")]
    assert (
        design
        == 'repair_design -slew_margin "0.0" -cap_margin "0.0" -max_utilization "85.0" -verbose'
    )
    assert timing.endswith('-max_utilization "85.0" -verbose')
    assert not [l for l in stage(script, "xeda_floorplan") if "max_utilization" in l]
    run_script(flow, script)


def test_no_repair_of_the_design_runs_before_the_placement(tmp_path):
    """OpenROAD stops with EST-0104 on `repair_design -pre_placement` after a placement, and the
    flow calls the repair after the placement only, so there is no setting for the flag."""
    message = refusal(tmp_path, "resize.repair_design.pre_placement=true")
    assert "pre_placement" in message and "Extra inputs are not permitted" in message


def test_the_repair_of_the_timing_gets_its_limits_and_switches(tmp_path):
    repair = {
        "enabled": True,
        "fix": "setup_and_hold",
        "allow_setup_violations": True,
        "crpr": False,
        "max_iterations": 4,
        "max_passes": 50,
        "max_repairs_per_pass": 2,
        "moves": {"size_down_fanout": False, "buffering": False, "buffer_to_inverters": False},
    }
    flow = place_flow(tmp_path, {"resize": {"repair_timing": repair}})
    body = stage(render(flow), "xeda_resize")
    (line,) = [l for l in body if l.startswith("repair_timing")]
    assert line == (
        "repair_timing -setup_margin 0.0 -hold_margin 0.0 -allow_setup_violations "
        '-repair_tns "100.0" -skip_crpr -max_iterations "4" -max_passes "50" '
        '-max_repairs_per_pass "2" -skip_size_down -skip_buffering -skip_buffer_to_inverters '
        "-verbose"
    )
    run_script(flow)


def test_a_power_recovery_replaces_the_repair_of_the_timing(tmp_path):
    repair = {"enabled": True, "recover_power_percent": 25}
    flow = place_flow(tmp_path, {"resize": {"repair_timing": repair}})
    (line,) = [l for l in stage(render(flow), "xeda_resize") if l.startswith("repair_timing")]
    assert line == (
        'repair_timing -setup_margin 0.0 -hold_margin 0.0 -repair_tns "100.0" '
        '-recover_power "25.0" -verbose'
    )
    run_script(flow)


def test_the_buffers_of_chosen_instances_are_removed_as_separate_arguments(tmp_path, monkeypatch):
    """`remove_buffers` takes the patterns as separate arguments: a list in one argument names an
    instance `u1 u2`, which does not exist."""
    fake_returns(monkeypatch, {("get_cells",): "u1"})
    flow = place_flow(tmp_path, {"resize": {"remove_buffers_instances": ["u1", "buf_*"]}})
    body = stage(render(flow), "xeda_global_place")
    assert 'remove_buffers "u1" "buf_*"' in body
    assert 'foreach pattern [list "u1" "buf_*"] {' in body
    calls = run_script(flow)
    assert [call for call in calls if call[0] == "remove_buffers"] == [
        ["remove_buffers", "u1", "buf_*"]
    ]
    assert names(calls, "get_cells") == ["get_cells", "get_cells"]
    plain = stage(render(place_flow(tmp_path / "all")), "xeda_global_place")
    assert "remove_buffers" in plain and not [line for line in plain if "get_cells" in line]


def test_a_pattern_that_matches_no_instance_stops_the_flow_before_the_buffers_go(tmp_path):
    """With no instance left, `remove_buffers` removes every buffer, so a typo would do that."""
    require_tclsh()
    flow = place_flow(tmp_path, {"resize": {"remove_buffers_instances": ["typo"]}})
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_place")
    assert done.returncode != 0
    output = done.stderr + done.stdout
    assert 'resize.remove_buffers_instances names "typo", which matches no instance' in output
    recorded = "".join(log.read_text() for log in flow.run_path.rglob("fake_*.calls"))
    assert "ARG remove_buffers\n" not in recorded


def test_the_instances_of_the_buffers_to_remove_need_the_timing_driven_placement(tmp_path):
    message = refusal(
        tmp_path,
        "global_placement.timing_driven.enabled=false",
        typed={"resize.remove_buffers_instances": ["u1"]},
    )
    assert "`resize.remove_buffers_instances`" in message
    assert "`global_placement.timing_driven.enabled` is false" in message
    plan(tmp_path / "ok", typed={"resize.remove_buffers_instances": ["u1"]})


def test_the_utilization_limit_needs_a_command_that_reads_it(tmp_path):
    off = [
        "resize.buffer_ports.enabled=false",
        "resize.repair_design.enabled=false",
        "resize.max_utilization_percent=80",
    ]
    message = refusal(tmp_path, *off)
    assert "`resize.max_utilization_percent` has no effect" in message
    assert "`buffer_ports`, `repair_design` and `repair_timing`" in message
    plan(tmp_path / "ok", *off[:2], "resize.repair_timing.enabled=true", off[2])


def test_the_sizing_options_are_set_before_any_command_that_sizes(tmp_path):
    settings = {
        "resize": {
            "opt_config": {
                "limit_sizing_area": 2,
                "limit_sizing_leakage": 3,
                "keep_sizing_site": True,
                "keep_sizing_vt": True,
                "early_sizing_cap_ratio": 2.5,
                "early_buffer_sizing_cap_ratio": 3.5,
                "disable_buffer_pruning": False,
            },
            "global_sizing": {
                "presize_mode": "max_size_min_vt",
                "include_clock_network": True,
                "setup_slack_margin": "0.25 ns",
                "max_iterations": 9,
                "beta": 0.5,
            },
        }
    }
    flow = place_flow(tmp_path, settings)
    body = stage(render(flow), "xeda_floorplan")
    assert body[:3] == [
        "xeda_stage synth",
        'set_opt_config -limit_sizing_area "2.0" -limit_sizing_leakage "3.0" '
        '-set_early_sizing_cap_ratio "2.5" -set_early_buffer_sizing_cap_ratio "3.5" '
        "-keep_sizing_site true -keep_sizing_vt true",
        'set_global_sizing_config -presize_mode "max_size_min_vt" -max_iterations "9" '
        '-beta "0.5" -include_clock_network true -setup_slack_margin 2.5e-10',
    ]
    calls = names(
        run_script(flow),
        "link_design",
        "set_opt_config",
        "set_global_sizing_config",
        "eliminate_dead_logic",
        "repair_timing",
        "global_placement",
    )
    assert calls[:5] == [
        "link_design",
        "set_opt_config",
        "set_global_sizing_config",
        "eliminate_dead_logic",
        "repair_timing",
    ]


@pytest.mark.parametrize("unit", ["1ns", "1ps", "10ps"])
def test_the_margin_of_the_global_sizing_is_in_seconds_whatever_the_unit_of_the_library(
    tmp_path, unit
):
    settings = {"resize": {"global_sizing": {"setup_slack_margin": "0.1 ns"}}}
    flow = place_flow(tmp_path, settings, model={"time_unit": unit})
    body = stage(render(flow), "xeda_floorplan")
    assert "set_global_sizing_config -setup_slack_margin 1e-10" in body


def test_a_boolean_of_the_sizing_options_that_is_off_sets_nothing(tmp_path):
    settings = {
        "resize": {"opt_config": {"keep_sizing_site": False, "disable_buffer_pruning": False}}
    }
    flow = place_flow(tmp_path, settings)
    assert not [l for l in stage(render(flow), "xeda_floorplan") if "set_opt_config" in l]


@pytest.mark.parametrize("mode", get_args(PresizeMode))
def test_every_presize_mode_renders_and_runs(tmp_path, mode):
    flow = place_flow(tmp_path, {"resize": {"global_sizing": {"presize_mode": mode}}})
    assert f'set_global_sizing_config -presize_mode "{mode}"' in stage(
        render(flow), "xeda_floorplan"
    )
    run_script(flow)


def test_no_sizing_option_is_set_by_default(tmp_path):
    calls = run_script(place_flow(tmp_path))
    assert names(calls, "set_opt_config", "set_global_sizing_config") == []


def test_swapped_arithmetic_modules_are_placed_again_before_the_repair(tmp_path):
    flow = place_flow(tmp_path, {"arith_swap": {"enabled": True}, "hierarchical": True})
    body = stage(render(flow), "xeda_resize")
    swap = body.index("replace_arith_modules")
    assert body[swap + 1 : swap + 3] == [
        "global_placement -incremental -density [xeda_density] -pad_left 0 -pad_right 0",
        "estimate_parasitics -placement",
    ]
    assert swap < [i for i, line in enumerate(body) if line.startswith("repair_design")][0]


def test_the_cells_are_legalized_and_optimized_after_the_resize(tmp_path):
    calls = run_script(place_flow(tmp_path))
    legal = names(
        calls,
        "set_placement_padding",
        "detailed_placement",
        "improve_placement",
        "optimize_mirroring",
        "check_placement",
    )
    assert legal == [
        "set_placement_padding",
        "detailed_placement",
        "improve_placement",
        "optimize_mirroring",
        "check_placement",
    ]


def test_the_detailed_placement_follows_its_settings(tmp_path):
    script = render(place_flow(tmp_path))
    assert stage(script, "xeda_detail_place") == [
        "xeda_stage detailedplace",
        "set_placement_padding -global -left 0 -right 0",
        "detailed_placement",
        'improve_placement -max_displacement "5 1" -enable_extra_dpl 0',
        "optimize_mirroring",
        "check_placement -verbose",
        "estimate_parasitics -placement",
        "xeda_metrics 1 0",
    ]


def test_the_displacement_of_the_optimization_is_one_list_argument(tmp_path):
    settings = {"detailed_placement": {"improve": {"max_displacement": [3, 2]}}}
    body = stage(render(place_flow(tmp_path, settings)), "xeda_detail_place")
    assert 'improve_placement -max_displacement "3 2" -enable_extra_dpl 0' in body
    calls = run_script(place_flow(tmp_path / "run", settings))
    (improve,) = [call for call in calls if call[0] == "improve_placement"]
    assert improve == ["improve_placement", "-max_displacement", "3 2", "-enable_extra_dpl", "0"]


def test_the_optimizations_of_the_detailed_placement_can_be_switched_off(tmp_path):
    settings = {
        "detailed_placement": {
            "cell_padding": 1,
            "improve": {"enabled": False},
            "mirroring": False,
            "negotiation": {"window_extension": False},
        }
    }
    body = stage(render(place_flow(tmp_path, settings)), "xeda_detail_place")
    assert "set_placement_padding -global -left 1 -right 1" in body
    assert "detailed_placement -disable_window_extension" in body
    assert not [
        line for line in body if line.startswith(("improve_placement", "optimize_mirroring"))
    ]


def test_the_legalization_follows_its_options(tmp_path):
    settings = {
        "detailed_placement": {
            "max_displacement": ["300 um", "40 um"],
            "legalizer": "negotiation",
            "negotiation": {
                "site_search_window": 30,
                "row_search_window": 0,
                "drc_penalty": 2.5,
                "window_extension": False,
            },
        }
    }
    flow = place_flow(tmp_path, settings)
    body = stage(render(flow), "xeda_detail_place")
    assert (
        'detailed_placement -max_displacement "300 40" -site_search_window "30" '
        '-row_search_window "0" -disable_window_extension -drc_penalty "2.5"'
    ) in body
    (call,) = [call for call in run_script(flow) if call[0] == "detailed_placement"]
    assert call[:3] == ["detailed_placement", "-max_displacement", "300 40"]


def test_the_diamond_legalizer_is_a_flag_of_its_own(tmp_path):
    flow = place_flow(tmp_path, {"detailed_placement": {"legalizer": "diamond"}})
    body = stage(render(flow), "xeda_detail_place")
    assert "detailed_placement -use_diamond_legalizer" in body
    run_script(flow)


def test_the_padding_of_masters_and_instances_follows_the_global_padding(tmp_path):
    overrides = [
        {"masters": ["INV_*", "BUF_X1"], "left": 1, "right": 2},
        {"instances": ["u1", "core/u*"], "right": 3},
    ]
    settings = {"detailed_placement": {"cell_padding": 1, "cell_padding_overrides": overrides}}
    flow = place_flow(tmp_path, settings)
    body = stage(render(flow), "xeda_detail_place")
    padding = [line for line in body if line.startswith("set_placement_padding")]
    assert padding == [
        "set_placement_padding -global -left 1 -right 1",
        'set_placement_padding -masters [list "INV_*" "BUF_X1"] -left 1 -right 2',
        'set_placement_padding -instances [list "u1" "core/u*"] -left 0 -right 3',
    ]
    assert body.index(padding[2]) < body.index("detailed_placement")
    run_script(flow)


def test_the_rows_are_balanced_before_the_padding(tmp_path):
    flow = place_flow(tmp_path, {"detailed_placement": {"balance_rows": True}})
    body = stage(render(flow), "xeda_detail_place")
    assert body.index("balance_row_usage") < body.index(
        "set_placement_padding -global -left 0 -right 0"
    )
    assert names(run_script(flow), "balance_row_usage") == ["balance_row_usage"]
    assert "balance_row_usage" not in stage(
        render(place_flow(tmp_path / "off")), "xeda_detail_place"
    )


def test_the_optimization_always_says_whether_it_legalizes_again(tmp_path, monkeypatch):
    """`improve_placement` reads the environment variable `ENABLE_EXTRA_DPL` when it gets no flag,
    so the flow gives the flag in every case, and the environment cannot change the result."""
    for value, text in ((False, "0"), (True, "1")):
        settings = {"detailed_placement": {"improve": {"extra_legalization": value}}}
        body = stage(render(place_flow(tmp_path / text, settings)), "xeda_detail_place")
        assert f'improve_placement -max_displacement "5 1" -enable_extra_dpl {text}' in body


def test_the_optimization_gets_its_seed_and_the_options_of_the_global_swap(tmp_path):
    swap = {
        "passes": 3,
        "tolerance": 0.02,
        "tradeoff": 0.5,
        "area_weight": 0.3,
        "pin_weight": 0.7,
        "congestion_weight": 20,
        "sampling_moves": 100,
        "normalization_interval": 500,
        "profiling_excess": 1.2,
        "budget_multipliers": [1.5, 1.2],
    }
    settings = {"detailed_placement": {"improve": {"random_seed": 7, "global_swap": swap}}}
    flow = place_flow(tmp_path, settings)
    body = stage(render(flow), "xeda_detail_place")
    words = (
        "-passes 3 -tolerance 0.02 -tradeoff 0.5 -area_weight 0.3 -pin_weight 0.7 "
        "-congestion_user_weight 20.0 -sampling_moves 100 -normalization_interval 500 "
        "-profiling_excess 1.2 -budget_multipliers \\{1.5 1.2\\}"
    )
    assert (
        'improve_placement -max_displacement "5 1" -random_seed "7" '
        f'-global_swap_args "{words}" -enable_extra_dpl 0'
    ) in body
    (call,) = [call for call in run_script(flow) if call[0] == "improve_placement"]
    pairs = call[call.index("-global_swap_args") + 1]
    assert pairs.startswith("-passes 3 ") and pairs.endswith("-budget_multipliers {1.5 1.2}")


def test_only_the_given_options_of_the_global_swap_are_passed(tmp_path):
    settings = {"detailed_placement": {"improve": {"global_swap": {"tradeoff": 0.5}}}}
    body = stage(render(place_flow(tmp_path, settings)), "xeda_detail_place")
    assert (
        'improve_placement -max_displacement "5 1" -global_swap_args "-tradeoff 0.5"'
        in " ".join(body).replace("  ", " ")
    )
    plain = " ".join(stage(render(place_flow(tmp_path / "plain")), "xeda_detail_place"))
    assert "-global_swap_args" not in plain


def test_the_legalization_has_no_free_text_flags(tmp_path):
    from xeda.flows.openroad.place_groups import DetailedPlacementGroup

    assert "extra_args" not in DetailedPlacementGroup.model_fields
    assert not DetailedPlacementGroup.EXTRA_FLAGS
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, "detailed_placement.extra_args=-use_diamond_legalizer")


def test_an_illegal_placement_stops_the_flow_unless_the_settings_say_to_report_it(
    tmp_path, monkeypatch
):
    """`check_placement` raises an error when a check fails (the installed OpenROAD reports
    DPL-0033), and OpenROAD-flow-scripts does not catch it, so the default stops the flow. With
    `check: report` the script catches the error and goes on."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "check_placement")
    report = place_flow(tmp_path / "report", {"detailed_placement": {"check": "report"}})
    calls = run_script(report)
    assert names(calls, "check_placement") == ["check_placement"]
    assert (report.run_path / "place.odb").is_file()
    body = stage(render(report), "xeda_detail_place")
    assert "if {[catch {check_placement -verbose} message]} {" in body
    stop = place_flow(tmp_path / "fail")
    body = stage(render(stop), "xeda_detail_place")
    assert "check_placement -verbose" in body and not [line for line in body if "catch" in line]
    require_tclsh()
    stop.bind_template_values()
    stop.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(stop.run_path, render(stop), driver="openroad_place")
    assert done.returncode != 0
    assert (stop.run_path / "openroad_place-failed.odb").is_file()
    assert not (stop.run_path / "place.odb").exists()


def test_the_check_of_the_legalization_defaults_to_the_one_of_openroad_flow_scripts():
    from xeda.flows.openroad.place_groups import DetailedPlacementGroup

    assert DetailedPlacementGroup().check == "fail"
    assert "OpenROAD-flow-scripts" in (
        DetailedPlacementGroup.model_fields["check"].description or ""
    )


def test_the_metrics_stages_follow_the_stages_in_order(tmp_path):
    calls = run_script(place_flow(tmp_path))
    stages = [call[1] for call in calls if call[0] == "utl::set_metrics_stage"]
    assert stages == [
        "synth__{}",
        "floorplan__{}",
        "tapcell__{}",
        "pdn__{}",
        "globalplace_skip_io__{}",
        "io_placement__{}",
        "globalplace__{}",
        "placeopt__{}",
        "detailedplace__{}",
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {"io": {"flow": "skip_io_pass"}},
        {"io": {"flow": "fixed"}},
        {"io": {"flow": "concurrent"}},
        {"io": {"min_distance": "3 um"}},
        {"io": {"min_distance_tracks": 3}},
        {"global_placement": {"density": {"mode": "fixed"}}},
        {"global_placement": {"density": {"mode": "uniform"}}},
        {"global_placement": {"density": {"mode": "auto", "headroom": 0.1}}},
        {"detailed_placement": {"check": "fail"}},
        {"detailed_placement": {"check": "report"}},
        {"resize": {"repair_timing": {"enabled": True, "fix": "setup"}}},
        {"resize": {"repair_timing": {"enabled": True, "fix": "hold"}}},
        {"resize": {"repair_timing": {"enabled": True, "fix": "setup_and_hold"}}},
    ],
    ids=lambda settings: "-".join(
        f"{group}.{leaf}={value}"
        for group, leaves in settings.items()
        for leaf, value in leaves.items()
        if not isinstance(value, dict)
    ),
)
def test_every_choice_of_the_placement_groups_renders_and_runs(tmp_path, monkeypatch, settings):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})  # for the choices that place no pins
    run_script(place_flow(tmp_path, settings))


# ------------------------------------------------------------------------------ the pins: options


def io_placement(
    tmp_path: Path, io: dict[str, Any], **more: Any
) -> tuple[OpenroadPlace, list[str]]:
    """Return the flow with the `io` settings and the lines of its stage for the pins."""
    flow = place_flow(tmp_path, {"io": io, **more})
    return flow, stage(render(flow), "xeda_io_placement")


@pytest.mark.parametrize("algorithm, flag", [("matching", ""), ("annealing", " -annealing")])
def test_the_algorithm_of_the_pins_chooses_the_flag(tmp_path, algorithm, flag):
    flow = place_flow(tmp_path, {"io": {"algorithm": algorithm}})
    (line,) = stage(render(flow), "xeda_place_pins")
    assert line == f'place_pins -hor_layers [list "m3"] -ver_layers [list "m4"]{flag}'
    run_script(flow)


def test_the_schedule_of_the_annealing_is_set_before_the_pins_are_placed(tmp_path):
    schedule = {"temperature": 2.0, "max_iterations": 300, "perturb_per_iter": 50, "alpha": 0.9}
    flow, body = io_placement(tmp_path, {"algorithm": "annealing", "annealing": schedule})
    assert body == [
        "xeda_stage io_placement",
        'set_simulated_annealing -temperature "2.0" -max_iterations "300" '
        '-perturb_per_iter "50" -alpha "0.9"',
        "xeda_place_pins",
    ]
    assert names(run_script(flow), "set_simulated_annealing", "place_pins") == [
        "set_simulated_annealing",
        "place_pins",
    ]


def test_only_the_given_leaves_of_the_annealing_schedule_are_passed(tmp_path):
    _, body = io_placement(tmp_path, {"algorithm": "annealing", "annealing": {"alpha": 0.5}})
    assert 'set_simulated_annealing -alpha "0.5"' in body
    _, body = io_placement(tmp_path / "none", {"algorithm": "annealing"})
    assert not [line for line in body if "annealing" in line]


def test_a_schedule_of_the_annealing_has_no_effect_with_the_matching_algorithm(tmp_path):
    message = refusal(tmp_path, "io.annealing.alpha=0.9")
    assert "io.annealing.alpha" in message and "io.algorithm: annealing" in message
    plan(tmp_path / "ok", "io.annealing.alpha=0.9", "io.algorithm=annealing")


@pytest.mark.parametrize(
    "leaf, value",
    [
        ("temperature", 0),
        ("temperature", -1),
        ("max_iterations", 0),
        ("perturb_per_iter", 0),
        ("alpha", 0),
        ("alpha", 1.5),
        ("alpha", -0.1),
    ],
)
def test_the_schedule_of_the_annealing_keeps_its_ranges(tmp_path, leaf, value):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, "io.algorithm=annealing", typed={f"io.annealing.{leaf}": value})


def test_an_algorithm_in_capitals_is_refused(tmp_path):
    assert "annealing" in refusal(tmp_path, "io.algorithm=Annealing")


@pytest.mark.parametrize("io_flow", ["skip_io_pass", "concurrent"])
def test_the_excluded_parts_of_the_edges_are_set_before_the_pins_are_placed(tmp_path, io_flow):
    """`place_pins -exclude` runs `exclude_io_pin_region`, and `global_placement -place_ios`
    reads only the command, so the stage gives the command for both flows."""
    exclude = [
        {"edge": "top"},
        {"edge": "right", "begin": "15 um", "end": "60.5 um"},
        {"edge": "left", "end": "2 um"},
        {"edge": "bottom", "begin": "3 um"},
    ]
    flow, body = io_placement(tmp_path, {"flow": io_flow, "exclude": exclude})
    assert body[1] == (
        'exclude_io_pin_region -region "top:*" -region "right:15-60.5" -region "left:*-2" '
        '-region "bottom:3-*"'
    )
    assert "-exclude" not in render(flow)
    calls = run_script(flow)
    placing = ("place_pins",) if io_flow == "skip_io_pass" else ("global_placement",)
    assert names(calls, "exclude_io_pin_region", *placing)[:2] == [
        "exclude_io_pin_region",
        placing[0],
    ]


@pytest.mark.parametrize(
    "begin, end, text",
    [
        ("0 um", "5 um", "0-5"),
        ("0.5 um", f"{1e-05 * 3e5} um", "0.5-3"),
        ("1250 nm", "0.1 mm", "1.25-100"),
    ],
)
def test_an_interval_is_written_in_digits_only(begin, end, text):
    from xeda.flows.openroad.place_groups import EdgeInterval

    assert EdgeInterval(edge="top", begin=begin, end=end).as_region() == f"top:{text}"


@pytest.mark.parametrize(
    "where, item",
    [
        ("exclude", {"edge": "top", "begin": "5 um", "end": "5 um"}),
        ("exclude", {"edge": "top", "begin": "6 um", "end": "5 um"}),
    ],
)
def test_an_interval_begins_before_it_ends(tmp_path, where, item):
    message = refusal(tmp_path, typed={f"io.{where}": [item]})
    assert f"io.{where}[0]" in message and "begin before it ends" in message


@pytest.mark.parametrize(
    "item",
    [
        {"edge": "up"},
        {"edge": "Top"},
        {"edge": "top", "begin": "-1 um"},
        {"edge": "top", "end": "inf um"},
        {"edge": "top", "end": 5},
        {"begin": "1 um"},
        {"edge": "top", "side": "x"},
    ],
)
def test_a_malformed_excluded_interval_is_refused(tmp_path, item):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"io.exclude": [item]})


def test_groups_of_pins_are_constraints_of_the_pin_placer_that_come_before_it(tmp_path):
    groups = [
        {"pins": ["q[0]", "q[1]"]},
        {
            "pins": ["d*"],
            "order": True,
            "region": {"edge": "right", "begin": "1 um", "end": "4 um"},
        },
    ]
    flow, body = io_placement(tmp_path, {"groups": groups})
    assert body == [
        "xeda_stage io_placement",
        'set_io_pin_constraint -pin_names [list "q\\[0\\]" "q\\[1\\]"] -group',
        'set_io_pin_constraint -pin_names [list "d*"] -group -order -region "right:1-4"',
        "xeda_place_pins",
    ]
    calls = run_script(flow)
    assert names(calls, "set_io_pin_constraint", "place_pins") == [
        "set_io_pin_constraint",
        "set_io_pin_constraint",
        "place_pins",
    ]


def test_pins_chosen_by_name_or_by_direction_are_kept_to_a_part_of_an_edge(tmp_path):
    constraints = [
        {"direction": "input", "region": {"edge": "left"}},
        {"pins": ["q*"], "region": {"edge": "right", "begin": "0 um", "end": "5 um"}},
    ]
    flow, body = io_placement(tmp_path, {"constraints": constraints})
    assert body[1:3] == [
        'set_io_pin_constraint -direction "input" -region "left:*"',
        'set_io_pin_constraint -pin_names [list "q*"] -region "right:0-5"',
    ]
    run_script(flow)


@pytest.mark.parametrize("direction", ["input", "output", "inout", "feedthru"])
def test_every_direction_of_a_constraint_renders_and_runs(tmp_path, direction):
    constraints = [{"direction": direction, "region": {"edge": "top"}}]
    flow, body = io_placement(tmp_path, {"constraints": constraints})
    assert f'set_io_pin_constraint -direction "{direction}" -region "top:*"' in body
    run_script(flow)


def test_the_spelling_of_the_documentation_for_a_direction_is_refused_naming_the_right_one(
    tmp_path,
):
    """The README of the pin placer says `feedthrough`; the tool takes `feedthru` and raises
    "Unknown io type" on the other."""
    constraints = [{"direction": "feedthrough", "region": {"edge": "top"}}]
    message = refusal(tmp_path, typed={"io.constraints": constraints})
    assert "feedthru" in message


@pytest.mark.parametrize(
    "item",
    [
        {"region": {"edge": "top"}},
        {"region": {"edge": "top"}, "pins": ["a"], "direction": "input"},
        {"region": {"edge": "top"}, "pins": []},
        {"pins": ["a"]},
        {"direction": "input"},
    ],
)
def test_a_constraint_has_a_region_and_pins_or_a_direction(tmp_path, item):
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, typed={"io.constraints": [item]})
    assert "constraints" in str(error.value)


@pytest.mark.parametrize("item", [{"pins": []}, {}, {"pins": ["a"], "order": "yes"}])
def test_a_malformed_group_is_refused(tmp_path, item):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"io.groups": [item]})


def test_pins_are_mirrored_in_pairs(tmp_path):
    flow, body = io_placement(tmp_path, {"mirrored_pins": [["d[0]", "q[0]"], ["d[1]", "q[1]"]]})
    assert (
        'set_io_pin_constraint -mirrored_pins [list "d\\[0\\]" "q\\[0\\]" ' '"d\\[1\\]" "q\\[1\\]"]'
    ) in body
    run_script(flow)


@pytest.mark.parametrize("pairs", [[["a"]], [["a", "b", "c"]], [["a", ""]]])
def test_a_mirrored_pin_comes_with_exactly_one_partner(tmp_path, pairs):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"io.mirrored_pins": pairs})


def test_the_shape_of_the_pins_is_set_before_the_pins_are_placed(tmp_path):
    io = {
        "pin_length": {"horizontal": "0.5 um", "vertical": "0.6 um"},
        "pin_extension": {"vertical": "0.1 um"},
        "pin_thickness": {"horizontal": 2, "vertical": 1.5},
    }
    flow, body = io_placement(tmp_path, io)
    assert body == [
        "xeda_stage io_placement",
        'set_pin_length -hor_length "0.5" -ver_length "0.6"',
        'set_pin_length_extension -ver_extension "0.1"',
        'set_pin_thick_multiplier -hor_multiplier "2.0" -ver_multiplier "1.5"',
        "xeda_place_pins",
    ]
    run_script(flow)


@pytest.mark.parametrize(
    "key, value",
    [
        ("io.pin_length.horizontal", "0 um"),
        ("io.pin_length.vertical", "-1 um"),
        ("io.pin_extension.horizontal", "-0.1 um"),
        ("io.pin_thickness.horizontal", 0.5),
        ("io.pin_thickness.vertical", 0),
    ],
)
def test_the_shape_of_the_pins_keeps_its_ranges(tmp_path, key, value):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={key: value})


def test_no_pin_command_is_written_when_no_setting_asks_for_it(tmp_path):
    script = render(place_flow(tmp_path))
    for command in ("set_io_pin_constraint", "set_pin_", "set_simulated_annealing", "place_pin "):
        assert command not in script


FIXED_PINS = [
    {"pin": "clk", "layer": "m3", "x": "0 um", "y": "3.5 um"},
    {
        "pin": "d[0]",
        "layer": "m4",
        "x": "1 um",
        "y": "0 um",
        "width": "0.2 um",
        "height": "0.4 um",
        "snap_to_boundary": True,
        "status": "placed",
    },
]


def test_the_pins_by_hand_are_placed_before_any_placement(tmp_path):
    flow = place_flow(tmp_path, {"io": {"fixed_pins": FIXED_PINS}})
    script = render(flow)
    assert stage(script, "xeda_fixed_pins") == [
        'place_pin -pin_name "clk" -layer "m3" -location [list 0.0 3.5]',
        'place_pin -pin_name "d\\[0\\]" -layer "m4" -location [list 1.0 0.0] '
        "-pin_size [list 0.2 0.4] -force_to_die_boundary -placed_status",
    ]
    assert names(run_script(flow, script), "place_pin", "global_placement", "place_pins") == [
        "place_pin",
        "place_pin",
        "global_placement",
        "place_pins",
        "global_placement",
    ]


@pytest.mark.parametrize("status", ["fixed", "placed"])
def test_every_status_of_a_pin_by_hand_renders_and_runs(tmp_path, status):
    pins = [{"pin": "clk", "layer": "m3", "x": "0 um", "y": "3 um", "status": status}]
    flow = place_flow(tmp_path, {"io": {"fixed_pins": pins}})
    (line,) = stage(render(flow), "xeda_fixed_pins")
    assert line.endswith("-placed_status") == (status == "placed")
    run_script(flow)


@pytest.mark.parametrize(
    "io_flow, sizing", [("fixed", "area"), ("concurrent", "utilization"), ("skip_io_pass", "def")]
)
def test_the_pins_by_hand_work_with_every_flow_and_with_a_floorplan_from_a_def_file(
    tmp_path, monkeypatch, io_flow, sizing
):
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    floorplan = {"sizing": "def", "def_file": "plan.def"} if sizing == "def" else {}
    flow = place_flow(
        tmp_path,
        {"io": {"flow": io_flow, "fixed_pins": FIXED_PINS}, "floorplan": floorplan},
        model={"site": None} if floorplan else None,
    )
    assert "xeda_fixed_pins" in render(flow)
    assert names(run_script(flow), "place_pin") == ["place_pin", "place_pin"]


def test_the_pins_by_hand_plan_with_the_pins_that_stay(tmp_path):
    plan(
        tmp_path / "def",
        "floorplan.sizing=def",
        "floorplan.def_file=plan.def",
        typed={"io.fixed_pins": FIXED_PINS},
    )
    plan(tmp_path / "fixed", "io.flow=fixed", typed={"io.fixed_pins": FIXED_PINS})


@pytest.mark.parametrize(
    "pin",
    [
        {"pin": "a", "layer": "m3", "x": "0 um", "y": "0 um", "width": "0.2 um"},
        {"pin": "a", "layer": "m3", "x": "0 um", "y": "0 um", "height": "0.2 um"},
    ],
)
def test_a_pin_by_hand_gives_both_sides_of_its_shape_or_none(tmp_path, pin):
    message = refusal(tmp_path, typed={"io.fixed_pins": [pin]})
    assert "width" in message and "height" in message


@pytest.mark.parametrize(
    "pin",
    [
        {"pin": "a", "layer": "m3", "x": "0 um"},
        {"pin": "", "layer": "m3", "x": "0 um", "y": "0 um"},
        {"pin": "a", "layer": "", "x": "0 um", "y": "0 um"},
        {"pin": "a", "layer": "m3", "x": "nan um", "y": "0 um"},
        {"pin": "a", "layer": "m3", "x": 1, "y": "0 um"},
        {"pin": "a", "layer": "m3", "x": "0 um", "y": "0 um", "width": "0 um", "height": "1 um"},
        {"pin": "a", "layer": "m3", "x": "0 um", "y": "0 um", "status": "Fixed"},
    ],
)
def test_a_malformed_pin_by_hand_is_refused(tmp_path, pin):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"io.fixed_pins": [pin]})


def test_the_pins_that_stay_need_a_def_floorplan_or_pins_by_hand(tmp_path):
    message = refusal(tmp_path, "io.flow=fixed")
    assert "io.flow: fixed" in message and "GPL-0326" in message
    assert "floorplan.sizing: def" in message and "io.fixed_pins" in message
    area = {
        "floorplan.die_area": ["0 um", "0 um", "10 um", "10 um"],
        "floorplan.core_area": ["1 um", "1 um", "9 um", "9 um"],
    }
    assert "floorplan.sizing: area" in refusal(
        tmp_path / "area", "io.flow=fixed", "floorplan.sizing=area", typed=area
    )
    plan(tmp_path / "def", "io.flow=fixed", "floorplan.sizing=def", "floorplan.def_file=a.def")
    plan(tmp_path / "hand", "io.flow=fixed", typed={"io.fixed_pins": FIXED_PINS})


def stays_flow(tmp_path: Path, *, sizing: str) -> OpenroadPlace:
    """A flow whose pins stay where they are: `io.flow: fixed`, or the pins of a DEF file."""
    if sizing == "def":
        (tmp_path / "design").mkdir(parents=True, exist_ok=True)
        (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
        floorplan = {"sizing": "def", "def_file": "plan.def"}
        return place_flow(
            tmp_path, {"floorplan": floorplan, "io": {"flow": "skip_io_pass"}}, model={"site": None}
        )
    return place_flow(tmp_path, {"io": {"flow": "fixed", "fixed_pins": FIXED_PINS}})


@pytest.mark.parametrize("sizing", ["utilization", "def"])
def test_pins_that_stay_must_all_be_placed_before_the_global_placement_needs_them(tmp_path, sizing):
    """With `io.flow: fixed`, and with the pins of a DEF file, the flow places no pins. A list of
    pins by hand or a file that places only some of them leaves the rest unplaced, and the global
    placement stops later with GPL-0326. The stage of the pins checks it first and says which
    settings place the pins."""
    flow = stays_flow(tmp_path, sizing=sizing)
    body = stage(render(flow), "xeda_io_placement")
    assert body[:3] == ["xeda_stage io_placement", "if {![all_pins_placed]} {", body[2]]
    assert body[2].startswith('error "xeda: ') and body[3] == "}"
    message = body[2]
    assert "io.fixed_pins" in message and "the pins are not all placed" in message
    assert "floorplan.sizing: def" in message and "io.flow: concurrent" in message
    assert ("io.flow is fixed" in message) == (sizing != "def")


@pytest.mark.parametrize("sizing", ["utilization", "def"])
def test_unplaced_pins_stop_the_flow_before_the_global_placement(tmp_path, monkeypatch, sizing):
    require_tclsh()
    flow = stays_flow(tmp_path / "unplaced", sizing=sizing)
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_place")
    assert done.returncode != 0
    assert "xeda: the pins are not all placed" in done.stderr + done.stdout
    assert (flow.run_path / "openroad_place-failed.odb").is_file()
    recorded = "".join(log.read_text() for log in flow.run_path.rglob("fake_*.calls"))
    assert "ARG global_placement\n" not in recorded
    fake_returns(monkeypatch, {("all_pins_placed",): "1"})
    placed = stays_flow(tmp_path / "placed", sizing=sizing)
    assert names(run_script(placed), "global_placement") == ["global_placement"]


def test_the_pins_that_the_flow_places_are_not_checked_before_the_placement(tmp_path):
    for settings in ({}, {"io": {"flow": "concurrent"}}):
        script = render(place_flow(tmp_path / str(len(settings)), settings))
        assert "all_pins_placed" not in " ".join(stage(script, "xeda_io_placement"))


@pytest.mark.parametrize(
    "leaf, value",
    [
        ("exclude", [{"edge": "top"}]),
        ("groups", [{"pins": ["a"]}]),
        ("constraints", [{"direction": "input", "region": {"edge": "top"}}]),
        ("mirrored_pins", [["a", "b"]]),
        ("pin_length.horizontal", "0.5 um"),
        ("pin_extension.vertical", "0.1 um"),
        ("pin_thickness.vertical", 2),
        ("algorithm", "annealing"),
    ],
)
def test_a_setting_of_the_pin_placer_has_no_effect_when_the_pins_stay(tmp_path, leaf, value):
    message = refusal(
        tmp_path, "io.flow=fixed", typed={f"io.{leaf}": value, "io.fixed_pins": FIXED_PINS}
    )
    assert f"io.{leaf}" in message and "no effect" in message and "io.flow: fixed" in message


# Left to the tool: a layer for the pins that is no routing layer of the right direction
# (PPL-0021, PPL-0023, PPL-0045, PPL-0046) and a pin in two groups (PPL-0104). The messages of the
# tool name the layer or the pins.


# ------------------------------------------------------------------------------ the constraints


def sdc_text(flow: OpenroadPlace) -> str:
    flow.bind_template_values()
    path = flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    return (flow.run_path / path).read_text()


def sdc_lines(flow: OpenroadPlace) -> list[str]:
    return [line for line in sdc_text(flow).splitlines() if line.strip()]


@pytest.mark.parametrize(
    "unit, period, delay",
    [("1ns", "5.0", "1.0"), ("1ps", "5000.0", "1000.0")],
)
def test_the_clock_and_the_delays_are_in_the_time_unit_of_the_platform(
    tmp_path, unit, period, delay
):
    flow = place_flow(tmp_path, model={"time_unit": unit})
    found = sdc_lines(flow)
    assert f'create_clock -period {period} -name "clk" $clk_port' in found
    assert f'set_input_delay {delay} -clock "clk" $non_clock_inputs' in found
    assert f'set_output_delay {delay} -clock "clk" $all_output_ports' in found
    assert 'set clk_port [get_ports "clk"]' in found


@pytest.mark.parametrize("period", ["5 ns", "5000 ps", "0.005 us"])
def test_the_period_of_the_clock_is_written_whatever_unit_the_setting_has(tmp_path, period):
    flow = place_flow(tmp_path, {"clocks": {"clk": {"period": period}}})
    found = sdc_lines(flow)
    assert 'create_clock -period 5.0 -name "clk" $clk_port' in found
    assert 'set_input_delay 1.0 -clock "clk" $non_clock_inputs' in found


@pytest.mark.parametrize("given", ["250 ps", "0.25 ns", "0.00025 us"])
@pytest.mark.parametrize("unit, delay", [("1ns", "0.25"), ("1ps", "250.0")])
def test_a_delay_is_converted_to_the_time_unit_of_the_platform(tmp_path, unit, delay, given):
    flow = place_flow(
        tmp_path, {"input_delay": given, "output_delay": given}, model={"time_unit": unit}
    )
    found = sdc_lines(flow)
    assert f'set_input_delay {delay} -clock "clk" $non_clock_inputs' in found
    assert f'set_output_delay {delay} -clock "clk" $all_output_ports' in found


def test_an_absolute_delay_wins_over_the_fraction_of_the_clock(tmp_path):
    flow = place_flow(
        tmp_path, {"input_delay": "100 ps", "input_delay_frac": 0.5, "output_delay_frac": 0.1}
    )
    found = sdc_lines(flow)
    assert 'set_input_delay 0.1 -clock "clk" $non_clock_inputs' in found
    assert 'set_output_delay 0.5 -clock "clk" $all_output_ports' in found


def test_no_input_delay_goes_to_the_port_of_any_clock(tmp_path):
    """With two clocks, the data inputs are the inputs without the ports of every clock. The test
    runs the constraints under `tclsh` with stubs for the commands of the constraints."""
    require_tclsh()
    flow = place_flow(
        tmp_path,
        {
            "clocks": {
                "clk_a": {"period": "5 ns", "port": "clk_a"},
                "clk_b": {"period": "8 ns", "port": "clk_b"},
            }
        },
    )
    stubs = (
        "set ::log {}\n"
        "proc all_inputs {} { return {clk_a clk_b d0 d1} }\n"
        "proc all_outputs {} { return {q0} }\n"
        "proc get_ports {names} { return $names }\n"
        "proc create_clock {args} {}\n"
        "proc set_output_delay {args} {}\n"
        "proc set_input_delay {delay flag clock ports} { lappend ::log [list $clock $ports] }\n"
    )
    script = tmp_path / "check.tcl"
    script.write_text(stubs + sdc_text(flow) + "\nforeach entry $::log { puts $entry }\n")
    done = subprocess.run(["tclsh", str(script)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["clk_a {d0 d1}", "clk_b {d0 d1}"]


def test_the_clock_file_runs_after_the_sdc_of_the_design_and_before_the_sdc_files(tmp_path):
    design_sdc = tmp_path / "design" / "design.sdc"
    flow = place_flow(tmp_path, {"sdc_files": ["extra.sdc"]}, sdc=[design_sdc])
    reads = [line for line in lines(render(flow)) if line.startswith("read_sdc")]
    assert reads == [
        f'read_sdc "{design_sdc}"',
        'read_sdc "clocks.sdc"',
        f'read_sdc "{tmp_path / "design" / "extra.sdc"}"',
    ]


def test_the_clock_file_is_the_only_sdc_without_other_files(tmp_path):
    reads = [line for line in lines(render(place_flow(tmp_path))) if line.startswith("read_sdc")]
    assert reads == ['read_sdc "clocks.sdc"']


# ------------------------------------------------------------------------------ the final files


def test_the_driver_writes_the_database_and_the_constraints(tmp_path):
    script = render(place_flow(tmp_path))
    assert lines(script)[-2:] == [
        f'write_db "{tmp_path / "work" / "place.odb"}"',
        f'write_sdc -no_timestamp "{tmp_path / "work" / "place.sdc"}"',
    ]


def test_a_failed_stage_writes_the_failed_database_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "pdngen")
    flow = place_flow(tmp_path)
    require_tclsh()
    flow.bind_template_values()
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_place")
    assert done.returncode != 0
    assert (flow.run_path / "openroad_place-failed.odb").is_file()
    assert not (flow.run_path / "place.odb").exists()


# ------------------------------------------------------------------------------ includes


def test_the_driver_lists_its_stages_by_literal_names():
    driver = (TEMPLATES / "drivers" / "openroad_place.tcl.j2").read_text()
    includes = re.findall(r"\{%\s*include\s+(.*?)\s*%\}", driver)
    assert includes and all(re.fullmatch(r'"[\w./]+"', include) for include in includes)
    for include in includes:
        assert (TEMPLATES / include.strip('"')).is_file()


def test_every_user_text_reaches_the_script_through_a_filter():
    """A name, a path or a pattern is never written raw: a `{{ }}` of a stage template names a
    filter or a number, never a bare setting."""
    for path in (TEMPLATES / "stages").glob("*.tcl.j2"):
        for expression in re.findall(r"\{\{(.*?)\}\}", path.read_text()):
            expression = expression.strip()
            choice = re.fullmatch(r'"[^"]*" if [\w.]+ == "[^"]*" else "[^"]*"', expression)
            assert (
                "|" in expression or expression.startswith("render_flags(") or choice
            ), f"{path.name}: {{{{ {expression} }}}}"


# ------------------------------------------------------------------------------ planning


def test_the_flow_is_an_asic_synthesis_flow_that_reads_a_netlist_and_sdc_files():
    from xeda.design import SourceType

    assert OpenroadPlace.design_parts == frozenset({"rtl"})
    assert OpenroadPlace.design_source_types == frozenset(
        {SourceType.Sdc, SourceType.VerilogNetlist}
    )


def test_the_prepared_inputs_are_the_files_that_the_dry_run_locates(tmp_path):
    flow = place_flow(tmp_path)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def test_a_valid_set_of_settings_plans(tmp_path):
    planned = plan(tmp_path, "floorplan.core_utilization_percent=40")
    assert [node.name for node in planned.nodes] == ["openroad_place"]


def test_arith_swap_needs_the_hierarchy(tmp_path):
    message = refusal(tmp_path, "arith_swap.enabled=true")
    assert "arith_swap.enabled" in message and "hierarchical" in message
    plan(tmp_path / "ok", "arith_swap.enabled=true", "hierarchical=true")


@pytest.mark.parametrize(
    "texts, typed, expected",
    [
        (["floorplan.sizing=area"], None, r"die_area.*core_area"),
        (["floorplan.sizing=def"], None, r"floorplan\.def_file"),
        (
            ["floorplan.core_utilization_percent=40", "floorplan.sizing=area"],
            {
                "floorplan.die_area": ["0 um", "0 um", "10 um", "10 um"],
                "floorplan.core_area": ["1 um", "1 um", "9 um", "9 um"],
            },
            r"core_utilization_percent.*sizing: utilization",
        ),
        (
            [],
            {
                "floorplan.die_area": ["0 um", "0 um", "10 um", "10 um"],
                "floorplan.core_area": ["1 um", "1 um", "9 um", "9 um"],
            },
            r"die_area.*sizing: area",
        ),
        (
            ["floorplan.sizing=area"],
            {
                "floorplan.die_area": ["0 um", "0 um", "10 um", "10 um"],
                "floorplan.core_area": ["1 um", "1 um", "20 um", "9 um"],
            },
            r"core_area.*inside.*die_area",
        ),
        (
            ["floorplan.sizing=def", "floorplan.def_file=a.def", "floorplan.aspect_ratio=2"],
            None,
            r"aspect_ratio.*sizing: utilization",
        ),
        (["tapcell.mode=none", "tapcell.distance=5 um"], None, r"tapcell\.distance.*no effect"),
        (["pdn.mode=none"], {"pdn.grid": {"power_net": "VDD"}}, r"pdn\.grid.*no effect"),
        (["design.eliminate_dead_logic=maybe"], None, r"eliminate_dead_logic"),
        (
            ["floorplan.netlist_cleanup.mode=none", "floorplan.netlist_cleanup.repair.fix=hold"],
            None,
            r"netlist_cleanup\.repair\.fix.*no effect",
        ),
    ],
)
def test_the_refusals_of_the_floorplan_groups_come_at_planning(tmp_path, texts, typed, expected):
    assert re.search(expected, refusal(tmp_path, *texts, typed=typed)), expected


# ------------------------------------------------------------------------------ a launch


def test_a_launch_writes_the_constraints_and_the_files_of_the_stage(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        OpenroadPlace,
        openroad_design(tmp_path / "design"),
        flow_settings=openroad_settings(store, **{"clock.period": "5 ns"}),
    )
    assert flow is not None and flow.succeeded
    run_dir = Path(flow.run_path)
    for name in (
        "clocks.sdc",
        "openroad_place.tcl",
        "openroad_place.log",
        "metrics.json",
        "place.odb",
        "place.sdc",
    ):
        assert (run_dir / name).is_file(), name
    assert flow.outputs.odb == run_dir / "place.odb"
    assert flow.outputs.sdc == run_dir / "place.sdc"
    assert (
        'create_clock -period 5.0 -name "main_clock" $clk_port'
        in (run_dir / "clocks.sdc").read_text()
    )
    commands = [call[0] for call in fake_calls(run_dir)]
    assert commands.index("initialize_floorplan") < commands.index("pdngen")
    assert not (run_dir / "openroad_place-failed.odb").exists()


def test_a_launch_removes_the_failed_database_of_an_earlier_run(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    settings = openroad_settings(store, **{"clock.period": "5 ns"})
    design = openroad_design(tmp_path / "design")
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "pdngen")
        failed_flow = runner.run(OpenroadPlace, design, flow_settings=settings)
        assert failed_flow is None or not failed_flow.succeeded
    (failed,) = (tmp_path / "xeda_run").rglob("openroad_place-failed.odb")
    flow = runner.run(OpenroadPlace, design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    assert not failed.exists()


# ------------------------------------------------------------------------------ the pins and the density at planning


@pytest.mark.parametrize(
    "leaf",
    [
        "io.horizontal_layers=m2",
        "io.vertical_layers=m3",
        "io.min_distance=3 um",
        "io.corner_avoidance=1 um",
        "io.algorithm=annealing",
    ],
)
def test_a_setting_for_the_placement_of_the_pins_has_no_effect_when_the_pins_stay(tmp_path, leaf):
    message = refusal(tmp_path, leaf, "io.flow=fixed")
    key = leaf.split("=")[0]
    assert f"`{key}`" in message and "io.flow" in message and "fixed" in message
    plan(tmp_path / "ok", leaf)


def test_a_floorplan_from_a_def_file_refuses_a_setting_for_the_pins(tmp_path):
    def_texts = ["floorplan.sizing=def", "floorplan.def_file=plan.def"]
    message = refusal(tmp_path, *def_texts, "io.min_distance=3 um")
    assert "io.min_distance" in message and "floorplan.sizing: def" in message
    plan(
        tmp_path / "again",
        *def_texts,
        "io.min_distance=3 um",
        "io.flow=concurrent",
    )
    plan(tmp_path / "plain", *def_texts)


def test_the_concurrent_placement_of_the_pins_plans_with_the_default_placement_modes(tmp_path):
    """`global_placement -place_ios` takes `-timing_driven` and `-routability_driven`, which are
    on by default."""
    plan(tmp_path, "io.flow=concurrent")


def test_the_pins_need_the_layers_of_the_platform_unless_they_stay(tmp_path):
    residue = tmp_path / "no_io.yaml"
    residue.write_text(
        "\n".join(
            line
            for line in TINY_OPENROAD_RESIDUE.read_text().splitlines()
            if not line.startswith("  io_layers")
        )
    )
    no_io = f"platform_file={residue}"
    message = refusal(tmp_path, no_io)
    assert "io_layers.horizontal" in message and "io.flow: fixed" in message
    plan(tmp_path / "given", no_io, "io.horizontal_layers=m3", "io.vertical_layers=m4")
    plan(tmp_path / "fixed", no_io, "io.flow=fixed", typed={"io.fixed_pins": FIXED_PINS})


def test_a_fixed_density_needs_a_density_in_the_residue_files_or_the_settings(tmp_path):
    residue = tmp_path / "no_density.yaml"
    residue.write_text(
        "\n".join(
            line
            for line in TINY_OPENROAD_RESIDUE.read_text().splitlines()
            if "place_density" not in line
        )
    )
    no_density = f"platform_file={residue}"
    message = refusal(tmp_path, no_density)
    assert "place_density" in message and "global_placement.density.value" in message
    plan(tmp_path / "value", no_density, "global_placement.density.value=0.5")
    plan(tmp_path / "uniform", no_density, "global_placement.density.mode=uniform")


def test_a_platform_that_reads_the_pdk_configuration_may_give_the_density_later(tmp_path):
    """The bundled `nangate45` has `PLACE_DENSITY` in the configuration of its PDK, which only
    the files tell. So planning refuses nothing there."""
    given = {"platform": "nangate45", "clocks": {"clk": {"period": "5 ns"}}}
    settings = OpenroadPlace.Settings.from_input(given, design_root=tmp_path, runner_cwd=tmp_path)
    OpenroadPlace.check_settings_supported(settings)


def test_the_density_rules_of_the_settings_come_at_planning(tmp_path):
    assert "value" in refusal(
        tmp_path, "global_placement.density.mode=uniform", "global_placement.density.value=0.5"
    )
    assert "headroom" in refusal(tmp_path / "a", "global_placement.density.headroom=0.2")
    assert "headroom" in refusal(tmp_path / "b", "global_placement.density.mode=auto")
    assert "min_phi_coef" in refusal(
        tmp_path / "c",
        "global_placement.nesterov.min_phi_coef=1.2",
        "global_placement.nesterov.max_phi_coef=1.1",
    )


def test_a_detailed_padding_above_the_global_padding_is_a_warning(tmp_path, monkeypatch, caplog):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    settings = openroad_settings(
        store, **{"clock.period": "5 ns", "detailed_placement.cell_padding": "2"}
    )
    with caplog.at_level("WARNING"):
        flow = runner.run(
            OpenroadPlace, openroad_design(tmp_path / "design"), flow_settings=settings
        )
    assert flow is not None and flow.succeeded
    assert any(
        "detailed_placement.cell_padding (2) is above global_placement.cell_padding (0)"
        in record.getMessage()
        for record in caplog.records
    )


# ------------------------------------------------------------------------------ the results

EXPECTED_COMMANDS = [
    "read_liberty",
    "read_lef",
    "read_lef",
    "read_verilog",
    "link_design",
    "read_sdc",
    "set_layer_rc",
    "set_layer_rc",
    "set_layer_rc",
    "set_layer_rc",
    "set_layer_rc",
    "set_layer_rc",
    "set_layer_rc",
    "set_wire_rc",
    "set_wire_rc",
    "set_dont_use",
    "eliminate_dead_logic",
    "check_setup",
    "initialize_floorplan",
    "make_tracks",
    "make_tracks",
    "make_tracks",
    "set_global_routing_layer_adjustment",
    "set_global_routing_layer_adjustment",
    "set_routing_layers",
    "set_routing_layers",
    "repair_tie_fanout",
    "repair_tie_fanout",
    "repair_timing",
    "tapcell",
    "add_global_connection",
    "add_global_connection",
    "global_connect",
    "set_voltage_domain",
    "define_pdn_grid",
    "add_pdn_stripe",
    "add_pdn_stripe",
    "add_pdn_stripe",
    "add_pdn_connect",
    "add_pdn_connect",
    "pdngen",
    "all_pins_placed",
    "global_placement",
    "place_pins",
    "remove_buffers",
    "buffer_ports",
    "global_placement",
    "estimate_parasitics",
    "estimate_parasitics",
    "repair_design",
    "report_floating_nets",
    "set_placement_padding",
    "detailed_placement",
    "improve_placement",
    "optimize_mirroring",
    "check_placement",
    "estimate_parasitics",
    "write_db",
    "write_sdc",
]
#: The commands that the stages call to write metrics and to measure the design. They are no
#: part of the order that the flow decides.
MEASURING_COMMANDS = (
    "report_design_area",
    "report_design_area_metrics",
    "report_units_metric",
    "report_tns_metric",
    "report_worst_slack_metric",
    "report_fmax_metric",
    "report_erc_metrics",
    "report_power_metric",
    "utl::set_metrics_stage",
    "get_lib_cell",
    "get_property",
    "get_name",
)


def launch(tmp_path, monkeypatch):
    """Launch `openroad_place` on the tiny design with the fake OpenROAD. Return the runner, the
    design, the settings and the flow."""
    require_tclsh()
    use_fake_openroad(monkeypatch)
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design")
    settings = openroad_settings(store, **{"clock.period": "5 ns"})
    flow = runner.run(OpenroadPlace, design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    return runner, design, settings, flow


def test_a_launch_runs_the_commands_in_order_and_reports_the_recorded_metrics(
    tmp_path, monkeypatch
):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    commands = [call[0] for call in fake_calls(run_dir) if call[0] not in MEASURING_COMMANDS]
    assert commands == EXPECTED_COMMANDS
    assert (run_dir / "place.odb").is_file() and (run_dir / "place.sdc").is_file()
    results = json.loads((run_dir / "results.json").read_text())
    assert results["success"] is True
    assert results["worst_slack"] == pytest.approx(-0.1835)
    assert results["wns"] == pytest.approx(-0.1835)
    assert results["tns"] == pytest.approx(-7.7916)
    assert results["whs"] == pytest.approx(0.0846772)
    assert results["hold_tns"] == 0
    assert results["Fmax"] == pytest.approx(2306.81)
    assert results["area"] == pytest.approx(657.02)
    assert results["utilization"] == pytest.approx(0.753968)
    assert results["power"] == pytest.approx(0.00388374)
    assert results["platform"]["name"] == "tiny"


def test_a_second_launch_reuses_the_run(tmp_path, monkeypatch):
    runner, design, settings, first = launch(tmp_path, monkeypatch)
    again = runner.launch_flow(
        OpenroadPlace, Design.from_file(design), dict(s.split("=", 1) for s in settings)
    )
    assert again.succeeded and again.reused
    assert not first.reused


def test_a_missing_metrics_file_fails_the_reading_of_the_results(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    empty = tmp_path / "metrics"
    empty.mkdir()
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(empty))
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        OpenroadPlace,
        openroad_design(tmp_path / "design"),
        flow_settings=openroad_settings(store, **{"clock.period": "5 ns"}),
    )
    assert flow is None or not flow.succeeded
    (run_dir,) = (tmp_path / "xeda_run").rglob("openroad_place")
    assert not (run_dir / "trace.json").exists()


def test_a_metric_that_is_no_number_fails_the_reading_of_the_results(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    recorded = json.loads((RECORDED / "openroad_place.json").read_text())
    recorded["detailedplace__timing__setup__ws"] = "unknown"
    broken = tmp_path / "metrics"
    broken.mkdir()
    (broken / "openroad_place.json").write_text(json.dumps(recorded))
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(broken))
    store = tiny_store(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(
        OpenroadPlace,
        openroad_design(tmp_path / "design"),
        flow_settings=openroad_settings(store, **{"clock.period": "5 ns"}),
    )
    assert flow is None or not flow.succeeded


def test_the_results_are_described():
    from xeda.flow import COMMON_METRICS

    described = OpenroadPlace.results_description
    for key in (
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "Fmax",
        "utilization",
        "power",
        "area",
        "hold_tns",
        "platform",
    ):
        assert described[key].description, key
    for key in ("utilization", "power"):
        assert key in COMMON_METRICS


# ------------------------------------------------------------------------------ the dry run


def test_a_dry_run_judges_the_platform_files_and_the_netlist(tmp_path, monkeypatch):
    runner, design_file, settings, _ = launch(tmp_path, monkeypatch)
    design = Design.from_file(design_file)
    given = dict(s.split("=", 1) for s in settings)
    store = Path(given["pdk_root"])
    _, states = runner.plan_with_freshness(OpenroadPlace, design, flow_settings=given)
    assert states["openroad_place"].state == "fresh"
    library = store / "tiny" / "tiny.lib"
    library.write_text(library.read_text() + "\n/* edited */\n")
    _, states = runner.plan_with_freshness(OpenroadPlace, design, flow_settings=given)
    assert states["openroad_place"].state == "stale"
    assert states["openroad_place"].reason == f"input changed: {library.resolve()}"
    library.write_text(library.read_text().replace("\n/* edited */\n", ""))
    netlist = design.design_root / "counter_netlist.v"
    netlist.write_text(netlist.read_text() + "// edited\n")
    _, states = runner.plan_with_freshness(OpenroadPlace, design, flow_settings=given)
    assert states["openroad_place"].state == "stale"
    assert states["openroad_place"].reason == f"input changed: {netlist.resolve()}"


def test_a_dry_run_shows_a_missing_store_as_unknown_with_the_message_of_the_launch(
    tmp_path, monkeypatch
):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = dict(s.split("=", 1) for s in openroad_settings(empty, **{"clock.period": "5 ns"}))
    _, states = runner.plan_with_freshness(OpenroadPlace, design, flow_settings=given)
    assert states["openroad_place"].state == "unknown"
    with pytest.raises(PdkNotInstalled) as refused:
        runner.launch_flow(OpenroadPlace, design, given)
    assert str(refused.value) in states["openroad_place"].reason
    assert "tiny" in str(refused.value)
    assert not list((tmp_path / "xeda_run").rglob("fake_*.calls"))


# ------------------------------------------------------------------------------ chains


def test_a_synthesized_netlist_chains_into_the_placement(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    store = tiny_store(tmp_path)
    given = dict(
        text.split("=", 1) for text in openroad_settings(store, **{"clock.period": "5 ns"})
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design", rtl=True)
    planned = runner.plan("yosys+openroad_place", design, flow_settings=given)
    assert [node.name for node in planned.nodes] == ["yosys", "openroad_place"]
    flow = runner.run("yosys+openroad_place", design, flow_settings=given)
    assert flow is not None and flow.succeeded
    assert Path(flow.inputs.netlist).parent.name == "yosys"


def test_a_netlist_source_of_the_design_displaces_the_synthesis(tmp_path):
    planned = plan(tmp_path, "floorplan.core_utilization_percent=40")
    assert [node.name for node in planned.nodes] == ["openroad_place"]
