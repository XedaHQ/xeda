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
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.platforms import PdkNotInstalled
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import OpenroadPlace, base
from xeda.flows.openroad.base import to_lib_time
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
        "clocks": {"clk": {"period": 5.0}},
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
    return " ".join(str(error.value).split())


# ------------------------------------------------------------------------------ time and numbers


@pytest.mark.parametrize(
    "unit, value, text",
    [
        ("1ns", 0.25, "0.25"),
        ("1ns", 5, "5.0"),
        ("1ps", 0.25, "250.0"),
        ("1ps", 0.1, "100.0"),
        ("10ps", 2.5, "250.0"),
        ("1ps", 0.0, "0.0"),
    ],
)
def test_a_time_in_nanoseconds_is_written_in_the_unit_of_the_library(unit, value, text):
    assert to_lib_time(value, unit) == text


@pytest.mark.parametrize("value", ["1.0", None, True, float("nan"), float("inf")])
def test_text_is_never_converted_as_a_time(value):
    with pytest.raises(TypeError):
        to_lib_time(value, "1ns")


@pytest.mark.parametrize("unit, text", [("1ns", "0.25"), ("1ps", "250.0")])
def test_the_template_filter_uses_the_time_unit_of_the_platform(tmp_path, unit, text):
    flow = place_flow(tmp_path, model={"time_unit": unit})
    flow.bind_template_values()
    assert flow.jinja_env.from_string("{{ 0.25|lib_time }}").render() == text


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
                "die_area": [0, 0, 100, 90],
                "core_area": [2, 2, 98, 88.5],
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


def test_the_floorplan_takes_everything_from_a_def_file(tmp_path):
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
                "die_area": [0, 0, 100, 90],
                "core_area": [2, 2, 98, 88],
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
                "row_gap": 6.0,
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
                "die_area": [0, 0, 100, 90],
                "core_area": [2, 2, 98, 88],
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
        ("row_gap", "2"),
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
    flow = place_flow(tmp_path, {"floorplan": {"core_margin": [1, 2, 3, 4.5]}})
    body = stage(render(flow), "xeda_floorplan")
    assert any("-core_space [list 1.0 2.0 3.0 4.5] -site" in line for line in body)
    run_script(flow)


@pytest.mark.parametrize("margin", [[1, 2, 3], [1, 2, 3, 4, 5], [1, 2, 3, -4], -1])
def test_a_core_margin_is_one_distance_or_four(tmp_path, margin):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"floorplan.core_margin": margin})


POLYGON_DIE = [0, 0, 100, 0, 100, 100, 50, 100, 50, 50, 0, 50]
POLYGON_CORE = [2, 2, 98, 2, 98, 98, 52, 98, 52, 52, 2, 52]


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
            "floorplan.die_area": [float(n) for n in range(count)],
            "floorplan.core_area": [1, 1, 9, 9],
        },
    )
    assert "die_area" in message and "4 numbers" in message


def test_a_polygon_and_a_rectangle_are_left_to_the_tool(tmp_path):
    """OpenROAD stops with IFP-0077 on a rectangle for the die and a polygon for the core. A
    rectangle is a polygon, so xeda does not refuse the pair."""
    plan(
        tmp_path,
        "floorplan.sizing=area",
        typed={"floorplan.die_area": [0, 0, 100, 100], "floorplan.core_area": POLYGON_CORE},
    )


def test_a_polygon_is_not_refused_with_the_power_grid_or_the_concurrent_pins(tmp_path):
    """The power grid generator assumes a rectangular design, and the concurrent placement of the
    pins does not support a polygon. The tool says so, and the descriptions name the limits."""
    plan(
        tmp_path,
        "floorplan.sizing=area",
        "io.flow=concurrent",
        "global_placement.timing_driven.enabled=false",
        "global_placement.routability_driven.enabled=false",
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
                    {"region": [1, 2, 3, 4]},
                    {"region": [5, 6, 7, 8], "soft": True},
                    {"region": [9, 10, 11, 12], "max_density_percent": 40},
                ],
                "routing_blockages": [{"layer": "m2", "region": [1, 1, 2, 2]}],
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
                {"region": [1, 2, 3, 4], "soft": True, "max_density_percent": 40}
            ]
        },
    )
    assert (
        "placement_blockages" in message and "soft" in message and "max_density_percent" in message
    )


@pytest.mark.parametrize(
    "key, item",
    [
        ("placement_blockages", {"region": [3, 2, 1, 4]}),
        ("placement_blockages", {"region": [1, 2, 3, 2]}),
        ("routing_blockages", {"layer": "m2", "region": [3, 2, 1, 4]}),
    ],
)
def test_a_blockage_region_lists_the_lower_left_and_the_upper_right_corner(tmp_path, key, item):
    message = refusal(tmp_path, typed={f"floorplan.{key}": [item]})
    assert key in message and "lower-left" in message


@pytest.mark.parametrize(
    "key, item",
    [
        ("placement_blockages", {"region": [1, 2, 3]}),
        ("placement_blockages", {"region": [1, 2, 3, 4], "max_density_percent": 101}),
        ("placement_blockages", {"region": [1, 2, 3, 4], "max_density_percent": -1}),
        ("placement_blockages", {"region": [1, 2, 3, 4], "layer": "m1"}),
        ("routing_blockages", {"region": [1, 2, 3, 4]}),
        ("routing_blockages", {"layer": "", "region": [1, 2, 3, 4]}),
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
    body = stage(render(place_flow(tmp_path, {"tie": {"separation": 2.5}})), "xeda_floorplan")
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


@pytest.mark.parametrize("leaf, value", [("separation", 1.0), ("max_fanout", 4)])
def test_a_tie_option_has_no_effect_without_the_repair_of_the_fanout(tmp_path, leaf, value):
    message = refusal(tmp_path, f"tie.{leaf}={value}", "tie.repair_fanout=false")
    assert f"tie.{leaf}" in message and "no effect" in message


def test_the_tie_options_keep_their_ranges(tmp_path):
    for text in ("tie.max_fanout=0", "tie.separation=-1"):
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
        '-sequence "unbuffer,sizeup,swap,vt_swap" -repair_tns "100.0" -skip_last_gasp -verbose'
    )
    assert "estimate_parasitics" not in " ".join(body)
    calls = floorplan_calls(run_script(flow, script))
    assert names(calls, "estimate_parasitics") == []


def test_the_repair_margins_are_written_in_the_time_unit_of_the_platform(tmp_path):
    settings = {
        "floorplan": {"netlist_cleanup": {"repair": {"setup_margin": 0.05, "hold_margin": 0.02}}}
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
        'repair_timing -setup_margin 0.0 -hold_margin 0.0 -sequence "sizeup,swap" '
        '-repair_tns "40.0" -skip_pin_swap -match_cell_footprint -verbose'
    )


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
        tmp_path, {"tapcell": {"distance": 30, "master": "T2", "endcap_master": "E2"}}
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
    flow = place_flow(tmp_path, {"tapcell": {"distance": 20}})
    model = flow.platform_model.model_copy(update={"tap_cell": None})
    with pytest.raises(FlowSettingsException, match=r"tapcell\.distance.*cut_rows"):
        effective(flow.settings, model)


def test_the_rows_of_the_tap_cells_keep_a_minimum_width_and_height(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"row_min_width": 3, "row_min_height": 2.5}})
    assert stage(render(flow), "xeda_tapcell")[1:] == [
        'tapcell -distance 12.0 -tapcell_master "TAP_X1" -endcap_master "ENDCAP_X1" '
        '-row_min_width "3.0" -row_min_height "2.5"'
    ]
    run_script(flow)


def test_the_rows_that_are_cut_keep_a_minimum_width_and_height(tmp_path):
    flow = place_flow(
        tmp_path, {"tapcell": {"mode": "cut_rows", "row_min_width": 3, "row_min_height": 2.5}}
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
    value = "2" if leaf.startswith("row_") else "M_X1"
    message = refusal(tmp_path, f"tapcell.mode={mode}", f"tapcell.{leaf}={value}")
    assert f"tapcell.{leaf}" in message and "no effect" in message


def test_a_master_by_well_has_no_effect_when_the_platform_cuts_the_rows(tmp_path):
    flow = place_flow(tmp_path, {"tapcell": {"nwell_masters": {"nwin2": "W1"}}})
    model = flow.platform_model.model_copy(update={"tap_cell": None})
    with pytest.raises(FlowSettingsException, match=r"tapcell\.nwell_masters\.nwin2.*cut_rows"):
        effective(flow.settings, model)


def test_the_tap_cell_options_keep_their_ranges(tmp_path):
    for text in (
        "tapcell.row_min_width=-1",
        "tapcell.row_min_height=-1",
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
    grid = {"straps": [{"layer": "m3", "width": 0.3, "pitch": 4, "offset": 2}]}
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
        "rails": [{"layer": "m1", "width": 0.1}],
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
        typed={"pdn.grid": {"straps": [{"layer": "m4", "width": 1, "pitch": 6, "offset": 1}]}},
    )
    settings = planned.nodes[0].settings
    assert {"power_net", "rails"} <= settings.pdn.grid.model_fields_set
    model = place_flow(tmp_path / "flow").platform_model
    grid = effective(settings, model).pdn
    assert [(strap.layer, strap.pitch) for strap in grid.straps] == [("m4", 6)]
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
        "width": 0.5,
        "pitch": 12,
        "offset": 1,
        "spacing": 3,
        "offset_type": "center",
        "starts_with": "power",
        "extend": "core_ring",
        "extend_by": 2,
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
        "width": 0.5,
        "pitch": 12,
        "offset": 1,
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
        {"layer": "m4", "width": 0, "pitch": 12, "offset": 1},
        {"layer": "m4", "width": 1, "pitch": 0, "offset": 1},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": -1},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "spacing": 0},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "number_of_straps": 0},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "extend": "pads"},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "offset_type": "FIRST"},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "starts_with": "POWER"},
        {"layer": "m4", "width": 1, "pitch": 12, "offset": 1, "spacing": "2"},
    ],
)
def test_a_malformed_stripe_is_refused(tmp_path, strap):
    with pytest.raises(FlowSettingsException):
        plan(tmp_path, typed={"pdn.grid": {"straps": [strap]}})


RING = {"layers": ["m3", "m4"], "widths": 1, "spacings": 1, "core_offset": 0.5}


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
        "widths": [1, 2],
        "spacings": [0.5, 0.75],
        "core_offset": [1, 2, 3, 4],
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
        {**RING, "widths": 0},
        {**RING, "widths": [1, 2, 3]},
        {**RING, "spacings": -1},
        {**RING, "core_offset": [1, 2]},
        {**RING, "core_offset": -1},
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
            "cut_pitch": [0.2, 0.3],
            "fixed_vias": ["via34"],
            "dont_use_vias": ["bad*"],
            "max_rows": 2,
            "max_columns": 3,
            "ongrid": ["m3"],
            "min_width_layers": ["m2"],
            "split_cuts": {"m3": 0.38, "m4": 0.5},
            "split_cuts_staggered": True,
        },
        {"layers": ["m4", "m5"], "cut_pitch": 0.2},
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
        {"layers": ["m1", "m2"], "cut_pitch": 0},
        {"layers": ["m1", "m2"], "cut_pitch": [1, 2, 3]},
        {"layers": ["m1", "m2"], "split_cuts": {"m1": 0}},
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
    grid = {"straps": [{"layer": "m3", "width": 0.3, "pitch": 4, "offset": 2}]}
    flow = place_flow(tmp_path, {"pdn": {"grid": grid}})
    before = flow.platform_model.pdn.model_copy(deep=True)
    values = effective(flow.settings, flow.platform_model)
    assert values.pdn.straps == [PdnStripe(layer="m3", width=0.3, pitch=4.0, offset=2.0)]
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
    assert "above 1.0" in done.stderr + done.stdout


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
        "min_distance": 4,
        "min_distance_unit": "tracks",
        "corner_avoidance": 1.5,
        "extra_args": ["-group_pins", "q0"],
    }
    flow = place_flow(tmp_path, {"io": io})
    (line,) = stage(render(flow), "xeda_place_pins")
    assert line == (
        'place_pins -hor_layers [list "m2" "m4"] -ver_layers [list "m3"] '
        '-min_distance_in_tracks -min_distance 4 -corner_avoidance "1.5" "-group_pins" "q0"'
    )
    run_script(flow)


def test_the_distance_of_the_pins_in_micrometers_is_a_number_and_in_tracks_a_whole_number(tmp_path):
    um = place_flow(tmp_path, {"io": {"min_distance": 2.5}})
    assert stage(render(um), "xeda_place_pins")[0].endswith("-min_distance 2.5")
    tracks = place_flow(tmp_path / "t", {"io": {"min_distance": 3, "min_distance_unit": "tracks"}})
    assert " -min_distance 3 " in stage(render(tracks), "xeda_place_pins")[0] + " "
    message = refusal(tmp_path / "r", "io.min_distance=2.5", "io.min_distance_unit=tracks")
    assert "whole number" in message and "io.min_distance" in message


def test_the_pins_use_the_plural_flags_and_never_the_obsolete_random_flag(tmp_path):
    (line,) = stage(render(place_flow(tmp_path)), "xeda_place_pins")
    assert "-hor_layers" in line and "-ver_layers" in line
    assert "-random" not in line and "-hor_layer " not in line


def test_the_pins_are_placed_between_the_two_placements(tmp_path):
    names_in_order = names(
        run_script(place_flow(tmp_path)), "global_placement", "place_pins", "buffer_ports"
    )
    assert names_in_order == ["global_placement", "place_pins", "buffer_ports", "global_placement"]


def test_concurrent_placement_of_the_pins_places_them_with_the_cells_and_then_legalizes_them(
    tmp_path,
):
    flow = place_flow(
        tmp_path,
        {
            "io": {"flow": "concurrent"},
            "global_placement": {
                "timing_driven": {"enabled": False},
                "routability_driven": {"enabled": False},
            },
        },
    )
    script = render(flow)
    assert stage(script, "xeda_global_place_skip_io") == ["xeda_stage globalplace_skip_io"]
    assert stage(script, "xeda_io_placement") == ["xeda_stage io_placement"]
    body = stage(script, "xeda_global_place")
    assert body.index(
        "global_placement -density [xeda_density] -pad_left 0 -pad_right 0 -place_ios "
        "-force_center_initial_place"
    ) < body.index("xeda_place_pins")
    calls = run_script(flow, script)
    assert names(calls, "global_placement", "place_pins") == ["global_placement", "place_pins"]


def test_the_pins_stay_where_they_are_with_the_fixed_flow(tmp_path):
    flow = place_flow(tmp_path, {"io": {"flow": "fixed"}})
    script = render(flow)
    assert "xeda_place_pins" not in script
    calls = run_script(flow, script)
    assert names(calls, "place_pins") == []
    assert all("-skip_io" not in call for call in global_placement_calls(calls))


def test_a_floorplan_from_a_def_file_keeps_its_pins(tmp_path):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "plan.def").write_text("VERSION 5.8 ;\n")
    flow = place_flow(
        tmp_path, {"floorplan": {"sizing": "def", "def_file": "plan.def"}}, model={"site": None}
    )
    script = render(flow)
    assert stage(script, "xeda_global_place_skip_io") == ["xeda_stage globalplace_skip_io"]
    assert stage(script, "xeda_io_placement") == ["xeda_stage io_placement"]
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
            "extra_args": ["-skip_initial_place"],
        }
    }
    body = stage(render(place_flow(tmp_path, settings)), "xeda_global_place")
    assert (
        "global_placement -density [xeda_density] -pad_left 1 -pad_right 1 "
        '-min_phi_coef "0.9" -max_phi_coef "1.1" "-skip_initial_place"'
    ) in body


def test_the_parasitics_of_the_placement_are_estimated_after_each_change(tmp_path):
    """Before the global route, every estimate is made from the placement."""
    script = render(place_flow(tmp_path))
    for name in ("xeda_global_place", "xeda_resize", "xeda_detail_place"):
        body = stage(script, name)
        assert "estimate_parasitics -placement" in body, name
    assert "-global_routing" not in script


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
        "max_wire_length": 500,
        "slew_margin": 10,
        "cap_margin": 5,
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
                "repair_timing": {"enabled": True, "setup_margin": 0.05, "tns_end_percent": 50}
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
        'improve_placement -max_displacement "5 1"',
        "optimize_mirroring",
        "if {[catch {check_placement -verbose} message]} {",
        'puts "xeda: check_placement found violations ($message), and the flow goes on"',
        "}",
        "estimate_parasitics -placement",
        "xeda_metrics 1 0",
    ]


def test_the_displacement_of_the_optimization_is_one_list_argument(tmp_path):
    settings = {"detailed_placement": {"improve": {"max_displacement": [3, 2]}}}
    body = stage(render(place_flow(tmp_path, settings)), "xeda_detail_place")
    assert 'improve_placement -max_displacement "3 2"' in body
    calls = run_script(place_flow(tmp_path / "run", settings))
    (improve,) = [call for call in calls if call[0] == "improve_placement"]
    assert improve == ["improve_placement", "-max_displacement", "3 2"]


def test_the_optimizations_of_the_detailed_placement_can_be_switched_off(tmp_path):
    settings = {
        "detailed_placement": {
            "cell_padding": 1,
            "improve": {"enabled": False},
            "mirroring": False,
            "extra_args": ["-disable_window_extension"],
        }
    }
    body = stage(render(place_flow(tmp_path, settings)), "xeda_detail_place")
    assert "set_placement_padding -global -left 1 -right 1" in body
    assert 'detailed_placement "-disable_window_extension"' in body
    assert not [
        line for line in body if line.startswith(("improve_placement", "optimize_mirroring"))
    ]


def test_an_illegal_placement_is_reported_or_stops_the_flow_as_the_settings_say(
    tmp_path, monkeypatch
):
    """`check_placement` raises an error when a check fails (the installed OpenROAD reports
    DPL-0033). With `check: report` the script catches it and goes on."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "check_placement")
    flow = place_flow(tmp_path / "report")
    calls = run_script(flow)
    assert names(calls, "check_placement") == ["check_placement"]
    assert (flow.run_path / "place.odb").is_file()
    stop = place_flow(tmp_path / "fail", {"detailed_placement": {"check": "fail"}})
    body = stage(render(stop), "xeda_detail_place")
    assert "check_placement -verbose" in body and not [line for line in body if "catch" in line]
    require_tclsh()
    stop.bind_template_values()
    stop.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    done = run_fake_openroad(stop.run_path, render(stop), driver="openroad_place")
    assert done.returncode != 0
    assert (stop.run_path / "openroad_place-failed.odb").is_file()
    assert not (stop.run_path / "place.odb").exists()


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
        {
            "io": {"flow": "concurrent"},
            "global_placement": {
                "timing_driven": {"enabled": False},
                "routability_driven": {"enabled": False},
            },
        },
        {"io": {"min_distance": 3, "min_distance_unit": "um"}},
        {"io": {"min_distance": 3, "min_distance_unit": "tracks"}},
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
def test_every_choice_of_the_placement_groups_renders_and_runs(tmp_path, settings):
    run_script(place_flow(tmp_path, settings))


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


@pytest.mark.parametrize("unit, delay", [("1ns", "0.25"), ("1ps", "250.0")])
def test_a_delay_in_picoseconds_is_converted_to_the_time_unit_of_the_platform(
    tmp_path, unit, delay
):
    flow = place_flow(
        tmp_path, {"input_delay": 250, "output_delay": 250}, model={"time_unit": unit}
    )
    found = sdc_lines(flow)
    assert f'set_input_delay {delay} -clock "clk" $non_clock_inputs' in found
    assert f'set_output_delay {delay} -clock "clk" $all_output_ports' in found


def test_an_absolute_delay_wins_over_the_fraction_of_the_clock(tmp_path):
    flow = place_flow(
        tmp_path, {"input_delay": 100, "input_delay_frac": 0.5, "output_delay_frac": 0.1}
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
                "clk_a": {"period": 5.0, "port": "clk_a"},
                "clk_b": {"period": 8.0, "port": "clk_b"},
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
            {"floorplan.die_area": [0, 0, 10, 10], "floorplan.core_area": [1, 1, 9, 9]},
            r"core_utilization_percent.*sizing: utilization",
        ),
        (
            [],
            {"floorplan.die_area": [0, 0, 10, 10], "floorplan.core_area": [1, 1, 9, 9]},
            r"die_area.*sizing: area",
        ),
        (
            ["floorplan.sizing=area"],
            {"floorplan.die_area": [0, 0, 10, 10], "floorplan.core_area": [1, 1, 20, 9]},
            r"core_area.*inside.*die_area",
        ),
        (
            ["floorplan.sizing=def", "floorplan.def_file=a.def", "floorplan.aspect_ratio=2"],
            None,
            r"aspect_ratio.*sizing: utilization",
        ),
        (["tapcell.mode=none", "tapcell.distance=5"], None, r"tapcell\.distance.*no effect"),
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
        flow_settings=openroad_settings(store, **{"clock.period": "5"}),
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
    settings = openroad_settings(store, **{"clock.period": "5"})
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
        "io.min_distance=3",
        "io.corner_avoidance=1",
        "io.extra_args=-annealing",
    ],
)
def test_a_setting_for_the_placement_of_the_pins_has_no_effect_when_the_pins_stay(tmp_path, leaf):
    message = refusal(tmp_path, leaf, "io.flow=fixed")
    key = leaf.split("=")[0]
    assert f"`{key}`" in message and "io.flow" in message and "fixed" in message
    plan(tmp_path / "ok", leaf)


def test_a_floorplan_from_a_def_file_refuses_a_setting_for_the_pins(tmp_path):
    def_texts = ["floorplan.sizing=def", "floorplan.def_file=plan.def"]
    message = refusal(tmp_path, *def_texts, "io.min_distance=3")
    assert "io.min_distance" in message and "floorplan.sizing: def" in message
    plan(
        tmp_path / "again",
        *def_texts,
        "io.min_distance=3",
        "io.flow=concurrent",
        "global_placement.timing_driven.enabled=false",
        "global_placement.routability_driven.enabled=false",
    )
    plan(tmp_path / "plain", *def_texts)


def test_the_concurrent_placement_of_the_pins_plans_with_the_default_placement_modes(tmp_path):
    """OpenROAD refuses `-place_ios` together with `-timing_driven` and `-routability_driven`
    (GPL-0179 and GPL-0181). The settings stay available, and the tool tells."""
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
    plan(tmp_path / "fixed", no_io, "io.flow=fixed")


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
    given = {"platform": "nangate45", "clocks": {"clk": {"period": 5}}}
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
        store, **{"clock.period": "5", "detailed_placement.cell_padding": "2"}
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
    settings = openroad_settings(store, **{"clock.period": "5"})
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
    assert results["worst_slack"] == pytest.approx(-0.154591)
    assert results["wns"] == pytest.approx(-0.154591)
    assert results["tns"] == pytest.approx(-6.68972)
    assert results["whs"] == pytest.approx(0.0846505)
    assert results["hold_tns"] == 0
    assert results["Fmax"] == pytest.approx(2471.63)
    assert results["area"] == pytest.approx(807.044)
    assert results["utilization"] == pytest.approx(0.745271)
    assert results["power"] == pytest.approx(0.00524598)
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
        flow_settings=openroad_settings(store, **{"clock.period": "5"}),
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
        flow_settings=openroad_settings(store, **{"clock.period": "5"}),
    )
    assert flow is None or not flow.succeeded


def test_the_results_are_described():
    from xeda.flow.flow import COMMON_RESULT_DESCRIPTIONS

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
        assert described[key], key
    for key in ("utilization", "power"):
        assert key in COMMON_RESULT_DESCRIPTIONS


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
    given = dict(s.split("=", 1) for s in openroad_settings(empty, **{"clock.period": "5"}))
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
    given = dict(text.split("=", 1) for text in openroad_settings(store, **{"clock.period": "5"}))
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
