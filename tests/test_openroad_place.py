"""The placement flow, `openroad_place`: the load, the floorplan, the tap cells and the power grid.

The tests render the driver script of the flow for the `tiny` platform and compare it with a
golden file. They run the rendered script under the fake OpenROAD, so a command that the
OpenROAD command list does not know fails the test. Planning refusals run through the runner.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import OpenroadPlace, base
from xeda.flows.openroad.base import to_lib_time
from xeda.flows.openroad.place import EffectivePlace, effective
from xeda.platforms.model import CellPort, Pdn, PdnStrap, Track

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    register_openroad_flows,
    render_driver,
    run_fake_openroad,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import fake_calls, require_tclsh, use_fake_openroad

GOLDEN = Path(__file__).parent / "resources" / "openroad" / "place_defaults.tcl"
TEMPLATES = Path(base.__file__).parent / "templates"


@pytest.fixture(autouse=True)
def place_flow_registered(monkeypatch):
    """Register the flows, and let the tests build `OpenroadPlace` while it is not complete.

    The flow keeps one abstract method until its driver holds every stage. Then the class is
    concrete, and this fixture changes nothing more than the registration."""
    with register_openroad_flows():
        if OpenroadPlace.__abstractmethods__:
            monkeypatch.setattr(OpenroadPlace, "__abstractmethods__", frozenset())
        yield


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
    """The lines of the body of the procedure `name` in a rendered script."""
    found = lines(script)
    start = found.index(f"proc {name} {{}} {{")
    end = found.index("}", start)
    return found[start + 1 : end]


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


def test_the_metrics_of_each_stage_have_the_name_of_the_stage(tmp_path):
    calls = run_script(place_flow(tmp_path))
    stages = [call[1] for call in calls if call[0] == "utl::set_metrics_stage"]
    assert stages == ["synth__{}", "floorplan__{}", "tapcell__{}", "pdn__{}"]


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
    calls = run_script(flow)
    found = names(calls, "repair_tie_fanout", "remove_buffers", "repair_timing")
    assert found == ["repair_tie_fanout", "repair_tie_fanout", "remove_buffers"]


def test_the_tie_separation_is_passed(tmp_path):
    body = stage(render(place_flow(tmp_path, {"tie": {"separation": 2.5}})), "xeda_floorplan")
    assert all('-separation "2.5"' in line for line in body if line.startswith("repair_tie_fanout"))


def test_no_tie_fanout_is_repaired_when_it_is_switched_off(tmp_path):
    flow = place_flow(tmp_path, {"tie": {"repair_fanout": False}}, model={"tie_low": None})
    assert "repair_tie_fanout" not in " ".join(stage(render(flow), "xeda_floorplan"))
    assert not names(run_script(flow), "repair_tie_fanout")


def test_the_netlist_has_no_tie_cells_inserted_by_openroad(tmp_path):
    """The synthesis puts the tie cells in the netlist. The flow never runs `insert_tiecells`."""
    assert "insert_tiecells" not in render(place_flow(tmp_path))


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
    calls = run_script(flow, script)
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
    calls = run_script(flow)
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
    grid = {"power_net": "VPWR", "ground_net": "VGND", "global_connects": []}
    flow = place_flow(tmp_path, {"pdn": {"grid": grid}})
    body = stage(render(flow), "xeda_pdn")
    assert 'set_voltage_domain -name CORE -power "VPWR" -ground "VGND"' in body
    assert not [line for line in body if line.startswith("add_global_connection")]


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


def test_a_global_connection_to_a_net_of_no_kind_is_refused(tmp_path):
    connects = [{"net": "OTHER", "pin_pattern": "^X$"}]
    flow = place_flow(tmp_path, {"pdn": {"grid": {"global_connects": connects}}})
    with pytest.raises(FlowSettingsException, match=r"OTHER.*kind"):
        effective(flow.settings, flow.platform_model)


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
    assert values.pdn.straps == [PdnStrap(layer="m3", width=0.3, pitch=4.0, offset=2.0)]
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
        if path.name not in ("floorplan.tcl.j2", "tapcell.tcl.j2", "pdn.tcl.j2"):
            continue
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


def test_every_setting_of_the_stages_so_far_is_read():
    """The settings of the placement stages that follow the power grid are not read yet."""
    from .test_unread_settings import unread_settings

    groups = ("design", "floorplan", "routing", "tie", "tapcell", "pdn")
    own = ("sdc_files", "input_delay", "output_delay", "input_delay_frac", "output_delay_frac")
    unread = [
        name
        for name in unread_settings(OpenroadPlace)
        if name.split(".")[0] in groups or name in own
    ]
    assert unread == []


def test_a_valid_set_of_settings_plans(tmp_path):
    planned = plan(tmp_path, "floorplan.core_utilization=40")
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
            ["floorplan.core_utilization=40", "floorplan.sizing=area"],
            {"floorplan.die_area": [0, 0, 10, 10], "floorplan.core_area": [1, 1, 9, 9]},
            r"core_utilization.*sizing: utilization",
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
