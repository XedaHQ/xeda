"""The global routing flow, `openroad_grt`: the restart from the clock tree database, the pin
access, the global route, the repair of the design and the timing with the parasitics of the
route, the repair of the antenna violations, and the route guides.

The tests render the driver script of the flow for the `tiny` platform and compare it with a
golden file. They run the rendered script under the fake OpenROAD, so a command that the
OpenROAD command list does not know fails the test. Planning refusals run through the runner.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import OpenroadGrt, base
from xeda.flows.openroad.grt import EffectiveGrt, effective
from xeda.flows.openroad.grt_groups import PinAccessGroup, RouterGroup
from xeda.platforms.model import CellPort

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    render_driver,
    run_fake_openroad,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import fake_calls, require_tclsh, use_fake_openroad

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics"
GOLDEN = Path(__file__).parent / "resources" / "openroad" / "grt_defaults.tcl"
TEMPLATES = Path(base.__file__).parent / "templates"


# ------------------------------------------------------------------------------ helpers


def residue_with(tmp_path: Path, *, upstream: bool = False, **values: Any) -> Path:
    """Return a copy of the residue file of the `tiny` platform with `values` added under
    `values`, for a test of a planning rule that reads the platform. With `upstream`, the platform
    reads the configuration file of its PDK too, whose values only a run can see."""
    text = TINY_OPENROAD_RESIDUE.read_text()
    extra = "".join(f"  {key}: {json.dumps(value)}\n" for key, value in values.items())
    text = text.replace("values:\n", "values:\n" + extra, 1)
    if upstream:
        text = "upstream: {adapter: orfs_config_mk, files: [config.mk]}\n" + text
    path = tmp_path / "residue" / "tiny_openroad.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def grt_flow(
    tmp_path: Path,
    settings: dict[str, Any] | None = None,
    *,
    model: dict[str, Any] | None = None,
    residue: Path | None = None,
    unset_polish: bool = False,
) -> OpenroadGrt:
    """Return the flow on the `tiny` platform, ready to render: its run directory is
    `tmp_path/work`, its database and SDC file are in `tmp_path/inputs`, its outputs are set and
    `prepare_inputs()` has run.

    `settings` are nested settings (a group is a dictionary). `model` replaces fields of the
    platform model after `prepare_inputs()`. The platform asks for no polish of the worst slack,
    unless `unset_polish` leaves its value unset or `residue` names another residue file."""
    if residue is None:
        residue = (
            TINY_OPENROAD_RESIDUE if unset_polish else residue_with(tmp_path, opt_post_grt_wns=0)
        )
    store = tiny_store(tmp_path, "store")
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = {**tiny_settings(store, residue=residue), **(settings or {})}
    run_path = tmp_path / "work"
    run_path.mkdir(exist_ok=True)
    flow = OpenroadGrt(
        OpenroadGrt.Settings.from_input(given, design_root=design.design_root, runner_cwd=tmp_path),
        design,
        run_path,
    )
    flow.init()
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "cts.odb").write_text("odb\n")
    (inputs / "cts.sdc").write_text("create_clock -name clk -period 5 [get_ports clk]\n")
    flow.inputs.odb = inputs / "cts.odb"
    flow.inputs.sdc = inputs / "cts.sdc"
    flow.outputs.odb = run_path / "grt.odb"
    flow.outputs.sdc = run_path / "grt.sdc"
    flow.outputs.guides = run_path / "grt.guide"
    flow.prepare_inputs()
    if model:
        flow.platform_model = flow.platform_model.model_copy(update=model)
    return flow


def render(flow: OpenroadGrt) -> str:
    return render_driver(flow, "openroad_grt")


def run_script(flow: OpenroadGrt, script: str | None = None) -> list[list[str]]:
    """Run the driver of `flow` under the fake OpenROAD in its run directory; return the commands
    that the fake recorded, one list of words for each."""
    require_tclsh()
    text = render(flow) if script is None else script
    done = run_fake_openroad(flow.run_path, text, driver="openroad_grt")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(flow.run_path)


def lines(script: str) -> list[str]:
    """The lines of a script without comments, blank lines and indentation."""
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def stage(script: str, name: str = "xeda_global_route") -> list[str]:
    """The lines of the body of the procedure `name` in a rendered script, without comments and
    indentation. The body ends at the first line that holds only `}` and is not indented."""
    raw = script.splitlines()
    start = raw.index(f"proc {name} {{}} {{")
    end = raw.index("}", start)
    return lines("\n".join(raw[start + 1 : end]))


def body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
    return stage(render(grt_flow(tmp_path, settings, **kwargs)))


def words(found: list[str], name: str) -> list[str]:
    """The lines of the stage that start with the command `name`."""
    return [line for line in found if line.split()[0] == name]


def names(calls: list[list[str]], *wanted: str) -> list[str]:
    return [call[0] for call in calls if call[0] in wanted]


def plan(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None, residue=None):
    """Plan `openroad_grt` on the tiny platform. `texts` are `KEY=VALUE` settings as the command
    line gives them, and `typed` gives the leaves that are no text, by dotted key."""
    store = tiny_store(tmp_path)
    given = tiny_settings(store, residue=residue or TINY_OPENROAD_RESIDUE)
    given = {key: str(value) for key, value in given.items()}
    given |= dict(text.split("=", 1) for text in texts)
    given |= typed or {}
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        "openroad_grt", openroad_design(tmp_path / "design"), flow_settings=given
    )


def refusal(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None, residue=None) -> str:
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, *texts, typed=typed, residue=residue)
    message = " ".join(str(error.value).split())
    assert "needs a unit" not in message, "the refusal is about a missing unit: " + message
    return message


# ------------------------------------------------------------------------------ the golden


def tokens(tmp_path: Path, text: str) -> str:
    """Replace the places that differ from test to test by tokens."""
    for root in sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True):
        text = text.replace(f"{root}/store", "@STORE@").replace(f"{root}/inputs", "@INPUTS@")
        text = text.replace(f"{root}/work", "@RUN@")
    return text


def test_the_defaults_render_the_golden_script(tmp_path):
    script = tokens(tmp_path, render(grt_flow(tmp_path, unset_polish=True)))
    assert script == GOLDEN.read_text()


def test_the_defaults_route_and_repair_as_openroad_flow_scripts_does(tmp_path):
    """The commands and the flags of the default stage, line by line. The platform gives no value
    for the polish of the worst slack, so the default of OpenROAD-flow-scripts applies: it
    polishes. The platform has a usable diode."""
    assert body(tmp_path, unset_polish=True) == [
        "xeda_stage globalroute",
        "pin_access",
        'global_route -congestion_report_file "reports/congestion.rpt" '
        '-congestion_iterations "30" -congestion_report_iter_step "5" -verbose',
        "set_placement_padding -global -left 0 -right 0",
        "set_propagated_clock [all_clocks]",
        "estimate_parasitics -global_routing",
        'repair_design -slew_margin "0.0" -cap_margin "0.0" -verbose',
        "global_route -start_incremental",
        "detailed_placement",
        "global_route -end_incremental "
        '-congestion_report_file "reports/congestion_post_repair_design.rpt"',
        "estimate_parasitics -global_routing",
        'repair_timing -setup_margin 0.0 -hold_margin 0.0 -repair_tns "100.0" -verbose',
        "global_route -start_incremental",
        "detailed_placement",
        "check_placement -verbose",
        "global_route -end_incremental "
        '-congestion_report_file "reports/congestion_post_repair_timing.rpt"',
        "estimate_parasitics -global_routing",
        'repair_timing -setup -sequence "vt_swap reroute" -skip_last_gasp -repair_tns 0 -verbose',
        'repair_antennas "ANTENNA_X1" -iterations "5"',
        "check_placement -verbose",
        'check_antennas -report_file "reports/antennas.rpt"',
        "estimate_parasitics -global_routing",
        "xeda_metrics 1 1",
        f'write_guides "{tmp_path / "work" / "grt.guide"}"',
    ]


def test_the_defaults_run_the_commands_of_the_flow_in_order(tmp_path):
    calls = run_script(grt_flow(tmp_path, unset_polish=True))
    commands = [
        call[0]
        for call in calls
        if call[0]
        not in (
            "report_design_area_metrics",
            "report_tns_metric",
            "report_worst_slack_metric",
            "report_fmax_metric",
            "report_clock_skew_metric",
            "report_erc_metrics",
            "report_power_metric",
            "utl::set_metrics_stage",
        )
    ]
    assert commands == [
        "read_liberty",
        "read_db",
        "read_sdc",
        *["set_layer_rc"] * 7,
        "set_wire_rc",
        "set_wire_rc",
        "set_dont_use",
        "pin_access",
        "global_route",
        "set_placement_padding",
        "all_clocks",
        "set_propagated_clock",
        "estimate_parasitics",
        "repair_design",
        "global_route",
        "detailed_placement",
        "global_route",
        "estimate_parasitics",
        "repair_timing",
        "global_route",
        "detailed_placement",
        "check_placement",
        "global_route",
        "estimate_parasitics",
        "repair_timing",
        "repair_antennas",
        "check_placement",
        "check_antennas",
        "estimate_parasitics",
        "write_guides",
        "write_db",
        "write_sdc",
    ]


def test_the_stage_measures_the_electrical_rules_and_the_clock_skew(tmp_path):
    calls = run_script(grt_flow(tmp_path))
    assert names(calls, "report_erc_metrics") == ["report_erc_metrics"]
    assert [call for call in calls if call[0] == "report_clock_skew_metric"] == [
        ["report_clock_skew_metric"],
        ["report_clock_skew_metric", "-hold"],
    ]


# ------------------------------------------------------------------------------ the restart


def test_the_process_starts_from_the_database_and_the_sdc_file_of_the_clock_tree(tmp_path):
    found = lines(render(grt_flow(tmp_path)))
    assert f'read_db "{tmp_path / "inputs" / "cts.odb"}"' in found
    assert [line for line in found if line.startswith("read_sdc")] == [
        f'read_sdc "{tmp_path / "inputs" / "cts.sdc"}"'
    ]
    assert not [line for line in found if line.split()[0] in ("read_verilog", "link_design")]
    assert not [line for line in found if line.split()[0] == "read_lef"]


def test_the_hierarchy_reaches_the_read_of_the_database(tmp_path):
    flow = grt_flow(tmp_path, {"hierarchical": True})
    assert f'read_db -hier "{tmp_path / "inputs" / "cts.odb"}"' in lines(render(flow))


def test_the_wire_rc_and_the_cells_not_to_use_are_set_before_the_route(tmp_path):
    calls = run_script(grt_flow(tmp_path))
    order = [call[0] for call in calls]
    route = order.index("global_route")
    for name in ("set_layer_rc", "set_wire_rc", "set_dont_use", "read_sdc", "read_db"):
        assert max(i for i, call in enumerate(order) if call == name) < route, name
    (dont_use,) = [call for call in calls if call[0] == "set_dont_use"]
    assert dont_use[1:] == ["INV_X4 NAND2_X4"]


def test_the_cells_not_to_use_are_set_once_and_before_every_repair(tmp_path):
    """The restart header sets them. The stage does not set them again."""
    found = body(tmp_path)
    assert not words(found, "set_dont_use")


def test_the_wire_rc_of_the_settings_replaces_the_wire_rc_of_the_platform(tmp_path):
    flow = grt_flow(tmp_path, {"wire_rc": {"signal_layer": "m3"}})
    found = lines(render(flow))
    assert 'set_wire_rc -signal -layer "m3"' in found
    assert 'set_wire_rc -clock -layer "m3"' in found


def test_the_stage_sets_no_option_that_the_database_stores(tmp_path):
    """The placement flow sets the routing layers and the layer adjustments. The database carries
    them, so the stage that routes does not set them again."""
    flow = grt_flow(tmp_path)
    called = names(
        run_script(flow),
        "set_routing_layers",
        "set_global_routing_layer_adjustment",
        "set_opt_config",
        "set_global_sizing_config",
        "set_dont_touch",
    )
    assert called == []


def test_the_outputs_are_written_at_the_end(tmp_path):
    found = lines(render(grt_flow(tmp_path)))
    assert found[-2:] == [
        f'write_db "{tmp_path / "work" / "grt.odb"}"',
        f'write_sdc -no_timestamp "{tmp_path / "work" / "grt.sdc"}"',
    ]
    assert found.index(f'write_guides "{tmp_path / "work" / "grt.guide"}"') < len(found) - 2


def test_the_guides_are_written_after_the_last_route_and_the_metrics(tmp_path):
    found = body(tmp_path)
    assert found[-2:] == ["xeda_metrics 1 1", f'write_guides "{tmp_path / "work" / "grt.guide"}"']


def test_a_failed_stage_writes_the_failed_database_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "global_route")
    flow = grt_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_grt")
    assert done.returncode != 0
    assert (flow.run_path / "openroad_grt-failed.odb").is_file()
    assert not (flow.run_path / "grt.odb").exists()


# ------------------------------------------------------------------------------ the options


def test_the_pin_access_runs_before_the_route_unless_switched_off(tmp_path):
    found = body(tmp_path)
    assert found.index("pin_access") < found.index(words(found, "global_route")[0])
    off = body(tmp_path / "off", {"pin_access": {"enabled": False}})
    assert not words(off, "pin_access")


def test_the_pin_access_takes_its_options(tmp_path):
    found = body(
        tmp_path,
        {
            "pin_access": {
                "process_node": "GF14_13M_3Mx_2Cx_4Kx_2Hx_2Gx_LB",
                "via_access_layer": "m2",
                "min_access_points": 3,
                "verbose": 0,
                "via_in_pin": {"bottom": "m1", "top": "m2"},
            }
        },
    )
    assert words(found, "pin_access") == [
        'pin_access -db_process_node "GF14_13M_3Mx_2Cx_4Kx_2Hx_2Gx_LB" -via_access_layer "m2" '
        '-min_access_points "3" -verbose "0" -via_in_pin_bottom_layer "m1" '
        '-via_in_pin_top_layer "m2"'
    ]


@pytest.mark.parametrize(
    "option, text",
    [
        ({"via_access_layer": "m2"}, '-via_access_layer "m2"'),
        ({"min_access_points": 2}, '-min_access_points "2"'),
        ({"process_node": "node"}, '-db_process_node "node"'),
        ({"verbose": 2}, '-verbose "2"'),
        ({"via_in_pin": {"bottom": "m1", "top": "m3"}}, '-via_in_pin_top_layer "m3"'),
    ],
)
def test_each_option_of_the_pin_access_passes_its_flag_only_when_set(tmp_path, option, text):
    (line,) = words(body(tmp_path, {"pin_access": option}), "pin_access")
    assert text in line
    assert words(body(tmp_path / "none"), "pin_access") == ["pin_access"]


def test_the_pin_access_has_no_list_of_extra_flags(tmp_path):
    """Every flag of the command has a setting, or belongs to the flow, or is not in the tool:
    the layers of the route are the ones of the database, and the distributed mode needs an
    external service."""
    assert "extra_args" not in PinAccessGroup.model_fields
    assert not PinAccessGroup.EXTRA_FLAGS


def first_route(found: list[str]) -> str:
    return words(found, "global_route")[0]


def test_the_seed_is_set_before_the_first_route_and_not_by_default(tmp_path):
    found = body(tmp_path, {"router": {"seed": 7}})
    seed = "set_global_routing_random -seed 7"
    assert seed in found
    assert found.index(seed) < found.index(first_route(found))
    assert not [line for line in body(tmp_path / "none") if "random" in line]


def test_the_perturbation_of_the_capacities_goes_with_the_seed(tmp_path):
    found = body(
        tmp_path,
        {"router": {"seed": 7, "capacity_perturbation_percent": 5, "perturbation_amount": 3}},
    )
    assert words(found, "set_global_routing_random") == [
        "set_global_routing_random -seed 7 -capacities_perturbation_percentage 5.0 "
        "-perturbation_amount 3"
    ]


def test_the_cugr_engine_is_chosen_on_the_first_route_only(tmp_path):
    """The tool keeps the engine across an increment, and a later `-use_cugr` would be ignored."""
    found = body(tmp_path, {"router": {"engine": "cugr"}})
    first, *incremental = words(found, "global_route")
    assert " -use_cugr" in first
    assert not [line for line in incremental if "cugr" in line]
    assert "cugr" not in first_route(body(tmp_path / "none"))


def test_the_default_iterations_follow_the_engine_as_openroad_flow_scripts_does(tmp_path):
    """The router of the default engine gets 30 iterations, and CUGR gets 10, which it needs less.
    A setting replaces both."""
    assert '-congestion_iterations "30"' in first_route(body(tmp_path))
    cugr = body(tmp_path / "cugr", {"router": {"engine": "cugr"}})
    assert '-congestion_iterations "10"' in first_route(cugr)
    given = body(
        tmp_path / "given", {"router": {"engine": "cugr"}, "congestion": {"iterations": 7}}
    )
    assert '-congestion_iterations "7"' in first_route(given)


def test_a_resistance_aware_route_keeps_the_flag_in_the_routes_that_end_an_increment(tmp_path):
    found = body(tmp_path, {"router": {"resistance_aware": True}})
    first, *incremental = words(found, "global_route")
    assert first.endswith("-verbose -resistance_aware")
    assert [line.split()[1] for line in incremental] == [
        "-start_incremental",
        "-end_incremental",
        "-start_incremental",
        "-end_incremental",
    ]
    assert all("-resistance_aware" in line for line in incremental if "-end_" in line)
    assert not [line for line in incremental if "-start_" in line and "-resistance_aware" in line]
    assert "-resistance_aware" not in " ".join(body(tmp_path / "none"))


def test_the_share_of_resistance_aware_nets_goes_to_the_first_route(tmp_path):
    found = body(tmp_path, {"router": {"resistance_aware": True, "res_aware_nets_percent": 20}})
    first, *incremental = words(found, "global_route")
    assert '-res_aware_nets_percentage "20.0"' in first
    assert not [line for line in incremental if "percentage" in line]


def test_the_batch_width_and_the_fanout_limit_go_to_the_first_route(tmp_path):
    found = body(tmp_path, {"router": {"batch_width": 4, "skip_fanout_above": 500}})
    first, *incremental = words(found, "global_route")
    assert '-snapshot_batched_width "4"' in first and '-skip_large_fanout_nets "500"' in first
    assert not [line for line in incremental if "batched" in line or "fanout" in line]


def test_the_grid_origin_is_given_in_micrometers_to_every_route(tmp_path):
    """The tool takes database units and resets the origin at each call without the flag, and the
    origin shifts the guides that the call saves. So every route gets it."""
    found = body(tmp_path, {"router": {"grid_origin": ["1 um", "2.5 um"]}})
    routes = words(found, "global_route")
    assert len(routes) == 5
    origin = "-grid_origin [list [ord::microns_to_dbu 1.0] [ord::microns_to_dbu 2.5]]"
    assert all(origin in line for line in routes)
    assert "-grid_origin" not in " ".join(body(tmp_path / "none"))


def test_the_steiner_alphas_are_set_in_order_before_the_first_route(tmp_path):
    found = body(
        tmp_path,
        {
            "router": {
                "steiner_alphas": [
                    {"alpha": 0.4},
                    {"alpha": 0.1, "clock_nets": True},
                    {"alpha": 0.8, "nets": ["a", "b"]},
                    {"alpha": 0.2, "min_fanout": 20},
                    {"alpha": 0.3, "min_hpwl": "150.5 um"},
                ]
            }
        },
    )
    alphas = words(found, "set_routing_alpha")
    assert alphas == [
        "set_routing_alpha 0.4",
        "set_routing_alpha 0.1 -clock_nets",
        'set_routing_alpha 0.8 -net "a" -net "b"',
        "set_routing_alpha 0.2 -min_fanout 20",
        "set_routing_alpha 0.3 -min_hpwl 150.5",
    ]
    assert found.index(alphas[-1]) < found.index(first_route(found))


def test_the_nets_to_route_are_set_before_the_first_route(tmp_path):
    found = body(tmp_path, {"router": {"only_nets": ["clk", "n*"]}})
    line = 'set_nets_to_route [list "clk" "n*"]'
    assert line in found and found.index(line) < found.index(first_route(found))
    assert not words(body(tmp_path / "none"), "set_nets_to_route")


def test_the_router_has_no_list_of_extra_flags():
    """Every flag of `global_route` has a setting, or belongs to the flow (the guide, the
    report, the increments, the verbose report), or is the testing flag `-infinite_cap`."""
    assert "extra_args" not in RouterGroup.model_fields
    assert not RouterGroup.EXTRA_FLAGS


def test_the_congestion_settings_reach_the_first_route(tmp_path):
    found = body(tmp_path, {"congestion": {"iterations": 50, "allow": True}})
    first = words(found, "global_route")[0]
    assert '-congestion_iterations "50"' in first and "-allow_congestion" in first


def test_the_report_step_and_the_share_of_critical_nets_reach_the_first_route(tmp_path):
    found = body(tmp_path, {"congestion": {"report_step": 0, "critical_nets_percent": 12.5}})
    first, *incremental = words(found, "global_route")
    assert '-congestion_report_iter_step "0"' in first
    assert '-critical_nets_percentage "12.5"' in first
    assert not [line for line in incremental if "critical" in line or "iter_step" in line]
    assert "-critical_nets_percentage" not in words(body(tmp_path / "none"), "global_route")[0]


def test_the_macro_extension_is_set_before_the_first_route_and_not_by_default(tmp_path):
    found = body(tmp_path, {"congestion": {"macro_extension": 2}})
    assert "set_macro_extension 2" in found
    assert found.index("set_macro_extension 2") < found.index(words(found, "global_route")[0])
    assert not words(body(tmp_path / "none"), "set_macro_extension")


def test_the_region_adjustments_are_set_before_the_first_route_in_order(tmp_path):
    """The database does not store them, so this flow sets them."""
    found = body(
        tmp_path,
        {
            "congestion": {
                "region_adjustments": [
                    {
                        "region": ["0 um", "0 um", "10 um", "12.5 um"],
                        "layer": "m2",
                        "adjustment": 0.5,
                    },
                    {
                        "region": ["5 um", "5 um", "20 um", "20 um"],
                        "layer": "m3",
                        "adjustment": 0.25,
                    },
                ]
            }
        },
    )
    lines_set = words(found, "set_global_routing_region_adjustment")
    assert lines_set == [
        'set_global_routing_region_adjustment [list 0.0 0.0 10.0 12.5] -layer "m2" -adjustment 0.5',
        'set_global_routing_region_adjustment [list 5.0 5.0 20.0 20.0] -layer "m3" -adjustment 0.25',
    ]
    assert found.index(lines_set[1]) < found.index(words(found, "global_route")[0])
    assert not words(body(tmp_path / "none"), "set_global_routing_region_adjustment")


@pytest.mark.parametrize(
    "adjustment",
    [
        {"region": ["0 um", "0 um", "10 um", "10 um"], "layer": "m2", "adjustment": 1.5},
        {"region": ["0 um", "0 um", "10 um", "10 um"], "layer": "m2", "adjustment": -0.1},
        {"region": ["-1 um", "0 um", "10 um", "10 um"], "layer": "m2", "adjustment": 0.5},
        {"region": ["0 um", "0 um", "10 um"], "layer": "m2", "adjustment": 0.5},
        {"region": ["0 um", "0 um", "10 um", "10 um"], "layer": "", "adjustment": 0.5},
        {"region": ["0 um", "0 um", "10 um", "10 um"], "layer": "m2"},
    ],
    ids=lambda adjustment: json.dumps(adjustment),
)
def test_a_region_adjustment_out_of_range_is_refused(adjustment):
    with pytest.raises(ValueError):
        OpenroadGrt.Settings(congestion={"region_adjustments": [adjustment]})


def test_the_decision_to_allow_congestion_reaches_every_route_and_the_antenna_repair(tmp_path):
    """A call without the flag routes a congested design harder and then fails on it."""
    found = body(tmp_path, {"congestion": {"allow": True}})
    routes = words(found, "global_route")
    assert len(routes) == 5 and all("-allow_congestion" in line for line in routes)
    (antennas,) = words(found, "repair_antennas")
    assert antennas.endswith("-allow_congestion")
    plain = body(tmp_path / "plain")
    assert not [line for line in plain if "-allow_congestion" in line]


def test_the_placement_is_legalized_with_the_padding_of_the_settings(tmp_path):
    found = body(tmp_path, {"legalize": {"cell_padding": 2}})
    assert "set_placement_padding -global -left 2 -right 2" in found


def test_every_legalization_takes_the_displacement_and_the_legalizer_of_the_settings(tmp_path):
    plain = [line for line in body(tmp_path) if line.startswith("detailed_placement")]
    assert plain == ["detailed_placement", "detailed_placement"]
    settings = {"legalize": {"max_displacement": ["10 um", "4 um"], "legalizer": "diamond"}}
    found = body(tmp_path / "set", settings)
    expected = 'detailed_placement -max_displacement "10 4" -use_diamond_legalizer'
    assert [line for line in found if line.startswith("detailed_placement")] == [expected] * 2


def test_the_parasitics_of_the_route_are_estimated_after_each_route_and_repair(tmp_path):
    found = body(tmp_path)
    for position, line in enumerate(found):
        if line.startswith("global_route -end_incremental") or line.startswith("repair_antennas"):
            assert found[position + 1].startswith(("estimate_parasitics -global", "check_"))
    assert found.index("set_propagated_clock [all_clocks]") + 1 == found.index(
        "estimate_parasitics -global_routing"
    )


def test_no_repair_runs_with_the_strategy_none(tmp_path):
    found = body(tmp_path, {"repair": {"strategy": "none"}})
    assert not words(found, "repair_design") and not words(found, "repair_timing")
    assert [line.split()[0] for line in found if line.startswith("global_route")] == [
        "global_route"
    ]
    assert not words(found, "detailed_placement")
    assert words(found, "repair_antennas") and words(found, "check_antennas")


def test_the_polish_and_the_power_recovery_do_not_run_with_the_strategy_none(tmp_path):
    found = body(tmp_path, {"repair": {"strategy": "none"}}, model={"opt_post_grt_wns": 1.0})
    assert not words(found, "repair_timing")


def test_the_repair_of_the_design_is_followed_by_an_increment_of_the_route(tmp_path):
    found = body(tmp_path, {"repair": {"timing": {"enabled": False}}})
    start = found.index(words(found, "repair_design")[0])
    assert found[start + 1 : start + 4] == [
        "global_route -start_incremental",
        "detailed_placement",
        "global_route -end_incremental "
        '-congestion_report_file "reports/congestion_post_repair_design.rpt"',
    ]
    assert not words(found, "repair_timing")


def test_the_repair_of_the_timing_is_followed_by_an_increment_and_a_check(tmp_path):
    found = body(tmp_path, {"repair": {"design": {"enabled": False}}})
    assert not words(found, "repair_design")
    start = found.index(words(found, "repair_timing")[0])
    assert found[start + 1 : start + 5] == [
        "global_route -start_incremental",
        "detailed_placement",
        "check_placement -verbose",
        "global_route -end_incremental "
        '-congestion_report_file "reports/congestion_post_repair_timing.rpt"',
    ]


def test_the_repair_of_the_timing_follows_its_settings(tmp_path):
    found = body(
        tmp_path,
        {
            "repair": {
                "timing": {
                    "fix": "setup",
                    "setup_margin": "0.1 ns",
                    "tns_end_percent": 50,
                    "last_gasp": False,
                    "sequence": ["unbuffer", "sizeup"],
                }
            }
        },
    )
    assert words(found, "repair_timing") == [
        'repair_timing -setup -setup_margin 0.1 -hold_margin 0.0 -sequence "unbuffer sizeup" '
        '-repair_tns "50.0" -skip_last_gasp -verbose'
    ]


def test_the_repair_margins_are_written_in_the_time_unit_of_the_platform(tmp_path):
    flow = grt_flow(tmp_path, {"repair": {"timing": {"setup_margin": "0.25 ns"}}})
    flow.platform_model = flow.platform_model.model_copy(update={"time_unit": "1ps"})
    found = stage(render(flow))
    assert "-setup_margin 250.0 -hold_margin 0.0" in words(found, "repair_timing")[0]


def test_the_reroute_move_and_phase_are_allowed_after_a_global_route(tmp_path):
    found = body(
        tmp_path,
        {"repair": {"timing": {"sequence": ["reroute"], "phases": ["WNS", "REROUTE"]}}},
    )
    (timing,) = words(found, "repair_timing")
    assert '-sequence "reroute"' in timing and '-phases "WNS REROUTE"' in timing


def test_the_reroute_of_the_repair_of_the_design_is_allowed_after_a_global_route(tmp_path):
    """The call runs with the parasitics of the route, which `-reroute` needs."""
    found = body(tmp_path, {"repair": {"design": {"reroute": True}}})
    (design,) = words(found, "repair_design")
    assert design.endswith("-reroute -verbose")
    assert "-reroute" not in " ".join(words(body(tmp_path / "none"), "repair_design"))


def test_the_footprint_of_the_platform_applies_unless_the_settings_say_otherwise(tmp_path):
    on = {"match_cell_footprint": True}
    found = body(tmp_path, model=on)
    assert all("-match_cell_footprint" in line for line in words(found, "repair_design"))
    assert all("-match_cell_footprint" in line for line in words(found, "repair_timing"))
    off = {"repair": {"design": {"match_cell_footprint": False}}}
    found = body(tmp_path / "off", off, model=on)
    assert "-match_cell_footprint" not in words(found, "repair_design")[0]
    assert "-match_cell_footprint" in words(found, "repair_timing")[0]
    assert "-match_cell_footprint" not in words(body(tmp_path / "plain"), "repair_timing")[0]


def test_the_polish_of_the_worst_slack_follows_the_setting_then_the_platform_then_the_default(
    tmp_path,
):
    polish = (
        'repair_timing -setup -sequence "vt_swap reroute" -skip_last_gasp -repair_tns 0 -verbose'
    )
    assert polish not in body(tmp_path)
    assert polish in body(tmp_path / "unset", unset_polish=True)
    off = {"wns_polish": False}
    assert polish not in body(tmp_path / "unset-off", {"repair": off}, unset_polish=True)
    found = body(tmp_path / "platform", model={"opt_post_grt_wns": 1.0})
    assert polish in found
    # the polish comes after the repair of the timing and its increment, and before the antennas
    assert found.index(polish) > found.index(words(found, "repair_timing")[0])
    assert found.index(polish) < found.index(words(found, "repair_antennas")[0])
    assert polish in body(tmp_path / "setting", {"repair": {"wns_polish": True}})
    off = body(tmp_path / "off", {"repair": {"wns_polish": False}}, model={"opt_post_grt_wns": 1.0})
    assert polish not in off


def test_the_polish_keeps_the_setup_margin_and_the_footprint(tmp_path):
    found = body(
        tmp_path,
        {"repair": {"wns_polish": True, "timing": {"setup_margin": "0.1 ns"}}},
        model={"match_cell_footprint": True},
    )
    assert (
        'repair_timing -setup -sequence "vt_swap reroute" -skip_last_gasp -repair_tns 0 '
        "-setup_margin 0.1 -match_cell_footprint -verbose"
    ) in found


def test_the_power_recovery_has_its_own_increment_of_the_route(tmp_path):
    found = body(tmp_path, {"repair": {"recover_power_percent": 10}})
    start = found.index("repair_timing -recover_power 10.0 -verbose")
    assert found[start - 1] == "global_route -start_incremental"
    assert found[start + 1] == (
        "global_route -end_incremental "
        '-congestion_report_file "reports/congestion_post_recover_power.rpt"'
    )
    assert found.index(words(found, "repair_timing")[0]) < start
    assert found.index(words(found, "repair_antennas")[0]) > start


def test_no_power_recovery_runs_by_default(tmp_path):
    found = body(tmp_path)
    assert not [line for line in found if "-recover_power" in line]
    assert len(words(found, "repair_timing")) == 1


def test_the_power_recovery_keeps_the_footprint_and_allows_congestion(tmp_path):
    found = body(
        tmp_path,
        {"repair": {"recover_power_percent": 10}, "congestion": {"allow": True}},
        model={"match_cell_footprint": True},
    )
    assert "repair_timing -recover_power 10.0 -match_cell_footprint -verbose" in found
    assert "global_route -start_incremental -allow_congestion" in found


def test_a_platform_without_a_usable_diode_skips_the_repair_of_the_antennas_and_says_so(tmp_path):
    found = body(tmp_path, model={"diode_usable": False})
    assert not words(found, "repair_antennas")
    assert 'puts "xeda: the platform has no usable antenna diode; antenna repair skipped"' in found
    assert words(found, "check_antennas")


def test_a_diode_of_the_settings_is_usable_whatever_the_platform_says(tmp_path):
    found = body(tmp_path, {"antenna": {"diode_cell": "DIODE_X1"}}, model={"diode_usable": False})
    assert words(found, "repair_antennas") == ['repair_antennas "DIODE_X1" -iterations "5"']


def test_the_diode_of_the_platform_goes_to_the_repair(tmp_path):
    found = body(tmp_path, model={"antenna_cell": CellPort(cell="DIODE_X2", port="I")})
    assert words(found, "repair_antennas") == ['repair_antennas "DIODE_X2" -iterations "5"']


def test_a_platform_that_names_no_diode_leaves_the_choice_to_the_router(tmp_path):
    """The command finds a diode by the class of the cell in the LEF file, as it does for
    OpenROAD-flow-scripts. If it finds none, it warns."""
    found = body(tmp_path, model={"antenna_cell": None, "diode_usable": None})
    assert words(found, "repair_antennas") == ['repair_antennas -iterations "5"']


def test_the_antenna_repair_can_be_switched_off_and_the_check_stays(tmp_path):
    found = body(tmp_path, {"antenna": {"repair": False}})
    assert not words(found, "repair_antennas")
    assert not [line for line in found if "skipped" in line]
    assert words(found, "check_antennas")


def test_the_iterations_of_the_antenna_repair_follow_the_settings(tmp_path):
    found = body(tmp_path, {"antenna": {"iterations": 2}})
    assert words(found, "repair_antennas") == ['repair_antennas "ANTENNA_X1" -iterations "2"']


@pytest.mark.parametrize(
    "method, flag",
    [("both", None), ("diode", "-diode_only"), ("jumper", "-jumper_only")],
)
def test_the_method_of_the_antenna_repair_chooses_its_flag(tmp_path, method, flag):
    found = body(tmp_path, {"antenna": {"method": method}})
    (line,) = words(found, "repair_antennas")
    others = {"-diode_only", "-jumper_only"} - {flag}
    assert (flag in line) if flag else True
    assert not others & set(line.split())


def test_the_ratio_margin_of_the_antenna_repair_follows_the_setting(tmp_path):
    found = body(tmp_path, {"antenna": {"ratio_margin_percent": 10, "method": "diode"}})
    assert words(found, "repair_antennas") == [
        'repair_antennas "ANTENNA_X1" -iterations "5" -ratio_margin "10.0" -diode_only'
    ]
    assert "-ratio_margin" not in " ".join(words(body(tmp_path / "none"), "repair_antennas"))


def test_the_method_and_the_margin_come_before_the_decision_on_congestion(tmp_path):
    found = body(
        tmp_path,
        {"congestion": {"allow": True}, "antenna": {"method": "jumper", "ratio_margin_percent": 5}},
    )
    (line,) = words(found, "repair_antennas")
    assert line.endswith('-ratio_margin "5.0" -jumper_only -allow_congestion')


def test_a_method_that_needs_no_diode_still_skips_where_the_platform_has_none(tmp_path):
    """The command reads the diffusion area of the diode to judge a violation, with every
    method, and it stops with GRT-0244 where that area is zero."""
    found = body(
        tmp_path,
        {"antenna": {"method": "jumper"}},
        model={"antenna_cell": None, "diode_usable": False},
    )
    assert not words(found, "repair_antennas")
    assert [line for line in found if "antenna repair skipped" in line]


@pytest.mark.parametrize(
    "settings",
    [
        {"pin_access": {"enabled": False}},
        {"pin_access": {"via_access_layer": "m2", "min_access_points": 2}},
        {"pin_access": {"via_in_pin": {"bottom": "m1", "top": "m2"}, "verbose": 0}},
        {"pin_access": {"process_node": "node"}},
        {"congestion": {"iterations": 10}},
        {"congestion": {"allow": True}},
        {"congestion": {"report_step": 0, "critical_nets_percent": 10, "macro_extension": 1}},
        {
            "congestion": {
                "region_adjustments": [
                    {"region": ["0 um", "0 um", "10 um", "10 um"], "layer": "m2", "adjustment": 0.3}
                ]
            }
        },
        {"router": {"seed": 3}},
        {"router": {"engine": "cugr", "resistance_aware": True, "res_aware_nets_percent": 20}},
        {"router": {"resistance_aware": True}},
        {"router": {"batch_width": 4, "skip_fanout_above": 100, "grid_origin": ["0 um", "1.5 um"]}},
        {"router": {"seed": 2, "capacity_perturbation_percent": 3, "perturbation_amount": 2}},
        {"router": {"steiner_alphas": [{"alpha": 0.5}, {"alpha": 0.2, "nets": ["a"]}]}},
        {"router": {"steiner_alphas": [{"alpha": 0.5, "clock_nets": True}]}},
        {"router": {"only_nets": ["clk"]}},
        {"legalize": {"cell_padding": 1}},
        {"repair": {"strategy": "none"}},
        {"repair": {"strategy": "incremental"}},
        {"repair": {"wns_polish": True}},
        {"repair": {"wns_polish": False, "recover_power_percent": 5}},
        {"repair": {"design": {"enabled": False}}},
        {"repair": {"design": {"max_wire_length": "100 um", "slew_margin_percent": 10}}},
        {"repair": {"design": {"reroute": True}}},
        {"repair": {"timing": {"enabled": False}}},
        {"repair": {"timing": {"fix": "hold", "hold_margin": "0.05 ns", "max_buffer_percent": 10}}},
        {"repair": {"timing": {"phases": ["WNS", "REROUTE"]}}},
        {"antenna": {"repair": False}},
        {"antenna": {"iterations": 1, "diode_cell": "DIODE_X1"}},
        {"antenna": {"method": "diode", "ratio_margin_percent": 20}},
        {"antenna": {"method": "jumper"}},
    ],
    ids=lambda settings: json.dumps(settings, sort_keys=True),
)
def test_every_choice_of_the_global_route_groups_renders_and_runs(tmp_path, settings):
    calls = run_script(grt_flow(tmp_path, settings))
    assert names(calls, "write_guides", "write_db", "write_sdc") == [
        "write_guides",
        "write_db",
        "write_sdc",
    ]


# ------------------------------------------------------------------------------ the templates


def test_the_driver_lists_its_stages_by_literal_names():
    driver = (TEMPLATES / "drivers" / "openroad_grt.tcl.j2").read_text()
    includes = re.findall(r"\{%\s*include\s+(.*?)\s*%\}", driver)
    assert includes and all(re.fullmatch(r'"[\w./]+"', include) for include in includes)
    for include in includes:
        assert (TEMPLATES / include.strip('"')).is_file()


def test_the_stage_template_writes_no_user_text_without_a_filter():
    text = (TEMPLATES / "stages" / "global_route.tcl.j2").read_text()
    for expression in re.findall(r"\{\{(.*?)\}\}", text):
        expression = expression.strip()
        assert "|" in expression or expression.startswith("render_flags("), expression


# ------------------------------------------------------------------------------ the effective values


def test_the_effective_values_are_a_pure_function_of_the_settings_and_the_model(tmp_path):
    flow = grt_flow(tmp_path)
    settings, model = flow.settings, flow.platform_model
    before = (settings.model_dump_json(), model.model_dump_json())
    values = effective(settings, model)
    assert isinstance(values, EffectiveGrt)
    assert values == effective(settings, model) == flow.effective()
    assert (settings.model_dump_json(), model.model_dump_json()) == before


def test_the_polish_comes_from_the_setting_then_the_platform_then_the_default(tmp_path):
    flow = grt_flow(tmp_path)
    assert effective(flow.settings, flow.platform_model).wns_polish is False
    unset = flow.platform_model.model_copy(update={"opt_post_grt_wns": None})
    assert effective(flow.settings, unset).wns_polish is True
    flow.settings.repair.wns_polish = False
    assert effective(flow.settings, unset).wns_polish is False
    flow.settings.repair.wns_polish = None
    model = flow.platform_model.model_copy(update={"opt_post_grt_wns": 1.0})
    assert effective(flow.settings, model).wns_polish is True
    zero = flow.platform_model.model_copy(update={"opt_post_grt_wns": 0.0})
    assert effective(flow.settings, zero).wns_polish is False
    for value in (2, 0.5):  # the platform value is a switch, and any value but 0 is on
        switch = flow.platform_model.model_copy(update={"opt_post_grt_wns": value})
        assert effective(flow.settings, switch).wns_polish is True
    flow.settings.repair.wns_polish = False
    assert effective(flow.settings, model).wns_polish is False
    flow.settings.repair.wns_polish = True
    assert effective(flow.settings, zero).wns_polish is True


def test_the_platform_value_of_the_polish_is_described_as_a_switch():
    from xeda.platforms.model import PlatformModel

    description = PlatformModel.model_fields["opt_post_grt_wns"].description
    assert description and "Switch" in description and "any value but 0" in description


def test_the_diode_comes_from_the_setting_and_else_from_the_platform(tmp_path):
    flow = grt_flow(tmp_path)
    plain = effective(flow.settings, flow.platform_model)
    assert (plain.diode_cell, plain.diode_usable) == ("ANTENNA_X1", True)
    unknown = flow.platform_model.model_copy(update={"antenna_cell": None, "diode_usable": None})
    values = effective(flow.settings, unknown)
    assert (values.diode_cell, values.diode_usable) == (None, True)
    platform = flow.platform_model.model_copy(update={"diode_usable": False})
    values = effective(flow.settings, platform)
    assert (values.diode_cell, values.diode_usable) == ("ANTENNA_X1", False)
    flow.settings.antenna.diode_cell = "DIODE_X1"
    values = effective(flow.settings, platform)
    assert (values.diode_cell, values.diode_usable) == ("DIODE_X1", True)


def test_the_footprint_comes_from_the_platform(tmp_path):
    flow = grt_flow(tmp_path)
    assert effective(flow.settings, flow.platform_model).match_cell_footprint is False
    model = flow.platform_model.model_copy(update={"match_cell_footprint": True})
    assert effective(flow.settings, model).match_cell_footprint is True


def test_a_power_recovery_that_the_platform_polish_replaces_is_refused_before_openroad_starts(
    tmp_path,
):
    """The platform may come from the configuration of its PDK, which planning does not read."""
    flow = grt_flow(tmp_path, {"repair": {"recover_power_percent": 10}})
    for value, said in ((1.0, "sets `opt_post_grt_wns`"), (None, "gives no `opt_post_grt_wns`")):
        model = flow.platform_model.model_copy(update={"opt_post_grt_wns": value})
        with pytest.raises(FlowSettingsException) as error:
            effective(flow.settings, model)
        message = " ".join(str(error.value).split())
        assert "`repair.recover_power_percent` has no effect" in message and said in message
        assert "`repair.wns_polish: false`" in message
    flow.settings.repair.wns_polish = False
    assert effective(flow.settings, model).wns_polish is False


# ------------------------------------------------------------------------------ planning


def test_the_flow_reads_the_database_of_the_clock_tree_and_no_source():
    assert OpenroadGrt.design_parts == frozenset({"rtl"})
    assert OpenroadGrt.reads_sources is None


def test_the_outputs_have_the_types_of_the_global_route():
    from xeda.flow.io import declared_outputs

    types = {name: out.types for name, out in declared_outputs(OpenroadGrt).items()}
    assert types == {
        "odb": (SourceType.OpenroadDbGlobalRouted,),
        "sdc": (SourceType.OpenroadSdcGlobalRouted,),
        "guides": (SourceType.Data,),
    }


def test_the_reports_have_fixed_names_under_the_directory_of_reports():
    assert OpenroadGrt.reports == {
        "congestion": "reports/congestion.rpt",
        "post_repair_design": "reports/congestion_post_repair_design.rpt",
        "post_repair_timing": "reports/congestion_post_repair_timing.rpt",
        "post_recover_power": "reports/congestion_post_recover_power.rpt",
        "antennas": "reports/antennas.rpt",
    }


def test_the_prepared_inputs_are_the_files_that_the_dry_run_locates(tmp_path):
    flow = grt_flow(tmp_path)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def test_a_valid_set_of_settings_plans_the_three_flows(tmp_path):
    planned = plan(tmp_path, "router.seed=3", "antenna.iterations=2")
    assert [node.name for node in planned.nodes] == [
        "openroad_place",
        "openroad_cts",
        "openroad_grt",
    ]
    grt = planned.nodes[-1]
    assert [item.describe() for item in grt.inputs] == [
        "odb <- openroad_cts.odb",
        "sdc <- openroad_cts.sdc",
    ]


@pytest.mark.parametrize(
    "texts, expected",
    [
        (
            ["repair.strategy=none", "repair.timing.setup_margin=0.1 ns"],
            "`repair.timing.setup_margin` has no effect with `repair.strategy: none`",
        ),
        (
            ["repair.strategy=none", "repair.wns_polish=true"],
            "`repair.wns_polish` has no effect with `repair.strategy: none`",
        ),
        (
            ["repair.timing.recover_power_percent=10"],
            "the global route has its own pass for it: set `repair.recover_power_percent`",
        ),
        (
            ["repair.wns_polish=true", "repair.recover_power_percent=10"],
            "`repair.recover_power_percent` has no effect with `repair.wns_polish: true`",
        ),
        (
            ["antenna.repair=false", "antenna.iterations=3"],
            "`antenna.iterations` has no effect with `antenna.repair: false`",
        ),
        (
            ["pin_access.enabled=false", "pin_access.min_access_points=2"],
            "has no effect while `pin_access.enabled` is false",
        ),
        (
            ["router.res_aware_nets_percent=20"],
            "`router.res_aware_nets_percent` has no effect without `router.resistance_aware`",
        ),
        (
            ["router.capacity_perturbation_percent=5"],
            "`router.capacity_perturbation_percent` has no effect without `router.seed`",
        ),
        (
            ["router.perturbation_amount=2"],
            "`router.perturbation_amount` has no effect without `router.seed`",
        ),
        (
            ["router.engine=cugr", "router.batch_width=4"],
            "`router.batch_width` has no effect with `router.engine: cugr`",
        ),
        (
            ["repair.timing.fix=hold", "repair.timing.last_gasp=false"],
            "`repair.timing.last_gasp` repairs setup, and `repair.timing.fix` is `hold`",
        ),
    ],
)
def test_the_refusals_come_at_planning_with_the_way_out(tmp_path, texts, expected):
    assert expected in refusal(tmp_path, *texts)


def test_a_steiner_alpha_with_two_selectors_is_refused_with_the_way_out(tmp_path):
    """The command takes the first selector that it finds and ignores the others."""
    message = refusal(
        tmp_path,
        typed={
            "router.steiner_alphas": [
                {"alpha": 0.2},
                {"alpha": 0.4, "clock_nets": True, "nets": ["a"]},
            ]
        },
    )
    assert "`router.steiner_alphas[1]` gives more than one of `nets`, `min_fanout`" in message
    assert (
        "`min_hpwl` and `clock_nets`" in message and "the command takes only the first" in message
    )


@pytest.mark.parametrize(
    "alpha",
    [
        {"alpha": 1.5},
        {"alpha": -0.1},
        {"alpha": 0.5, "min_fanout": -1},
        {"alpha": 0.5, "min_hpwl": "-1.0 um"},
    ],
    ids=lambda alpha: json.dumps(alpha),
)
def test_a_steiner_alpha_out_of_range_is_refused(alpha):
    with pytest.raises(ValueError):
        OpenroadGrt.Settings(router={"steiner_alphas": [alpha]})


def test_a_region_adjustment_without_area_is_refused_with_the_way_out(tmp_path):
    message = refusal(
        tmp_path,
        typed={
            "congestion.region_adjustments": [
                {"region": ["5 um", "5 um", "5 um", "20 um"], "layer": "m2", "adjustment": 0.5}
            ]
        },
    )
    assert "`congestion.region_adjustments[0]` has no area" in message
    assert "the upper right corner must lie above and right of the lower left corner" in message


def test_a_power_recovery_that_the_polish_of_the_platform_replaces_is_refused_at_planning(
    tmp_path,
):
    residue = residue_with(tmp_path, opt_post_grt_wns=1)
    message = refusal(tmp_path, "repair.recover_power_percent=10", residue=residue)
    assert "`repair.recover_power_percent` has no effect" in message
    assert "`opt_post_grt_wns`" in message and "`repair.wns_polish: false`" in message


def test_a_power_recovery_is_no_contradiction_where_the_platform_asks_for_no_polish(tmp_path):
    residue = residue_with(tmp_path, opt_post_grt_wns=0)
    planned = plan(tmp_path, "repair.recover_power_percent=10", residue=residue)
    assert planned.nodes[-1].name == "openroad_grt"
    # and where the setting turns the polish off, whatever the platform says
    residue = residue_with(tmp_path / "on", opt_post_grt_wns=1)
    planned = plan(
        tmp_path / "on",
        "repair.recover_power_percent=10",
        "repair.wns_polish=false",
        residue=residue,
    )
    assert planned.nodes[-1].name == "openroad_grt"


def test_a_platform_that_gives_no_value_polishes_as_openroad_flow_scripts_does(tmp_path):
    """Without a value in the platform and without the configuration of the PDK to give one, the
    default of OpenROAD-flow-scripts applies: the flow polishes, so a power recovery has no
    effect."""
    message = refusal(tmp_path, "repair.recover_power_percent=10")
    assert "`repair.recover_power_percent` has no effect" in message
    assert "gives no `opt_post_grt_wns`" in message and "`repair.wns_polish: false`" in message


def test_a_platform_value_that_is_unknown_at_planning_refuses_nothing(tmp_path):
    """The configuration of the PDK may give the value, and only a run reads it."""
    residue = residue_with(tmp_path, upstream=True)
    planned = plan(tmp_path, "repair.recover_power_percent=10", residue=residue)
    assert planned.nodes[-1].name == "openroad_grt"


def test_the_reroute_of_the_repair_of_the_design_is_no_refusal(tmp_path):
    planned = plan(tmp_path, "repair.design.reroute=true")
    assert planned.nodes[-1].name == "openroad_grt"


def test_the_reroute_move_and_phase_are_no_refusal(tmp_path):
    planned = plan(tmp_path, "repair.timing.sequence=reroute", "repair.timing.phases=WNS,REROUTE")
    assert planned.nodes[-1].name == "openroad_grt"


# ------------------------------------------------------------------------------ launches

LAUNCH = {"flows.openroad_place.clocks.clk.period": "5 ns"}


def launch_settings(tmp_path: Path, **given: Any) -> tuple[Path, list[str]]:
    store = tiny_store(tmp_path)
    return store, openroad_settings(store, **{**LAUNCH, **given})


def launch(tmp_path, monkeypatch, request="openroad_grt", **given):
    """Launch `request` on the tiny design with the fake OpenROAD. Return the runner, the design
    file, the settings and the flow."""
    require_tclsh()
    use_fake_openroad(monkeypatch)
    _, settings = launch_settings(tmp_path, **given)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design")
    flow = runner.run(request, design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    return runner, design, settings, flow


def test_a_launch_runs_the_three_flows_and_writes_the_outputs_and_the_guides(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    assert run_dir.name == "openroad_grt" and run_dir.parent.name == "counter"
    for name in (
        "openroad_grt.tcl",
        "openroad_grt.log",
        "metrics.json",
        "grt.odb",
        "grt.sdc",
        "grt.guide",
    ):
        assert (run_dir / name).is_file(), name
    cts = run_dir.parent / "openroad_cts"
    assert (cts / "cts.odb").is_file() and (cts / "cts.sdc").is_file()
    assert flow.outputs.odb == run_dir / "grt.odb"
    assert flow.outputs.sdc == run_dir / "grt.sdc"
    assert flow.outputs.guides == run_dir / "grt.guide"
    assert flow.inputs.odb == cts / "cts.odb"
    assert flow.inputs.sdc == cts / "cts.sdc"
    assert not (run_dir / "openroad_grt-failed.odb").exists()
    assert (run_dir / "reports").is_dir()


def test_the_global_route_reads_the_files_of_the_clock_tree(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    script = (run_dir / "openroad_grt.tcl").read_text()
    cts = run_dir.parent / "openroad_cts"
    assert f'read_db "{cts / "cts.odb"}"' in script
    assert f'read_sdc "{cts / "cts.sdc"}"' in script
    assert [call[0] for call in fake_calls(run_dir)].count("write_guides") == 1


def test_the_chain_of_the_three_flows_runs_the_same_way(tmp_path, monkeypatch):
    _, _, _, flow = launch(
        tmp_path, monkeypatch, request="openroad_place+openroad_cts+openroad_grt"
    )
    assert flow.name == "openroad_grt" and flow.succeeded


def test_a_launch_reports_the_recorded_metrics_of_the_global_route(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    document = json.loads((Path(flow.run_path) / "results.json").read_text())
    assert document["outcome"] == "passed"
    # the record holds the described metrics; the rest of the results is in memory
    results = {**flow.results, **document["metrics"]}
    assert results["worst_slack"] == pytest.approx(-0.247746)
    assert results["wns"] == pytest.approx(-0.247746)
    assert results["tns"] == pytest.approx(-11.083)
    assert results["whs"] == pytest.approx(0.0142931)
    assert results["hold_tns"] == 0
    assert results["fmax"] == pytest.approx(2009.06)
    assert results["area"] == pytest.approx(773.528)
    assert results["utilization"] == pytest.approx(0.887668)
    assert results["power"] == pytest.approx(0.00571903)
    assert results["clock_skew_setup"] == pytest.approx(0.00201635)
    assert results["global_route_wirelength"] == 6506
    assert results["global_route_vias"] == 1097
    assert results["antenna_violating_nets"] == 0
    assert results["antenna_violating_pins"] == 0
    # the diode of nangate45 is not usable: the recorded run skipped the repair
    assert "antenna_diodes" not in results
    assert results["platform"]["name"] == "tiny"


def test_the_results_are_described():
    from xeda.flow import COMMON_METRICS

    described = OpenroadGrt.results_description
    for key in (
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "fmax",
        "utilization",
        "power",
        "area",
        "hold_tns",
        "clock_skew_setup",
        "clock_skew_hold",
        "global_route_wirelength",
        "global_route_vias",
        "antenna_violating_nets",
        "antenna_violating_pins",
        "antenna_diodes",
        "platform",
    ):
        assert described[key].description, key
    for key in ("wns", "tns", "whs", "worst_slack", "fmax", "utilization", "power"):
        assert described[key] == COMMON_METRICS[key]


def test_a_second_launch_is_fresh(tmp_path, monkeypatch):
    runner, design, settings, first = launch(tmp_path, monkeypatch)
    log = Path(first.run_path) / "openroad_grt.log"
    written = log.stat().st_mtime_ns
    again = runner.run("openroad_grt", design, flow_settings=settings)
    assert again is not None and again.succeeded and again.reused
    assert not first.reused
    assert log.stat().st_mtime_ns == written


def test_a_change_of_a_global_route_setting_runs_the_route_again_and_not_the_flows_before(
    tmp_path, monkeypatch
):
    runner, design, settings, _ = launch(tmp_path, monkeypatch)
    given = dict(s.split("=", 1) for s in settings) | {"router.seed": "5"}
    _, states = runner.plan_with_freshness(
        "openroad_grt", Design.from_file(design), flow_settings=given
    )
    assert states["openroad_place"].state == "fresh"
    assert states["openroad_cts"].state == "fresh"
    assert states["openroad_grt"].state != "fresh"


def test_a_missing_metrics_file_fails_the_reading_of_the_results(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    recorded = tmp_path / "metrics"
    recorded.mkdir()
    for name in ("openroad_place", "openroad_cts"):
        (recorded / f"{name}.json").write_bytes((RECORDED / f"{name}.json").read_bytes())
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(recorded))
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("openroad_grt", openroad_design(tmp_path / "design"), flow_settings=settings)
    assert flow is None or not flow.succeeded
    (run_dir,) = (tmp_path / "xeda_run").rglob("openroad_grt")
    assert not (run_dir / "trace.json").exists()


@pytest.mark.parametrize(
    "key, value",
    [
        ("globalroute__timing__setup__ws", "unknown"),
        ("globalroute__global_route__wirelength", None),
        ("globalroute__antenna__violating__nets", 1.5),
    ],
)
def test_a_metric_that_is_no_number_fails_the_reading_of_the_results(
    tmp_path, monkeypatch, key, value
):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    broken = tmp_path / "metrics"
    broken.mkdir()
    for name in ("openroad_place", "openroad_cts"):
        (broken / f"{name}.json").write_bytes((RECORDED / f"{name}.json").read_bytes())
    recorded = json.loads((RECORDED / "openroad_grt.json").read_text())
    recorded[key] = value
    (broken / "openroad_grt.json").write_text(json.dumps(recorded))
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(broken))
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("openroad_grt", openroad_design(tmp_path / "design"), flow_settings=settings)
    assert flow is None or not flow.succeeded


def test_a_failed_route_leaves_a_failed_database_and_no_trace(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design")
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "global_route")
        failed = runner.run("openroad_grt", design, flow_settings=settings)
    assert failed is None or not failed.succeeded
    (grt_dir,) = (tmp_path / "xeda_run").rglob("openroad_grt")
    cts_dir = grt_dir.parent / "openroad_cts"
    assert (grt_dir / "openroad_grt-failed.odb").is_file()
    assert not (grt_dir / "trace.json").exists() and not (grt_dir / "grt.odb").exists()
    results = json.loads((grt_dir / "results.json").read_text())
    assert results["outcome"] == "failed"
    assert (cts_dir / "trace.json").is_file()
    # the next launch keeps the flows before it and runs the route
    _, states = runner.plan_with_freshness(
        "openroad_grt",
        Design.from_file(design),
        flow_settings=dict(s.split("=", 1) for s in settings),
    )
    assert states["openroad_cts"].state == "fresh"
    assert states["openroad_grt"].state != "fresh"
    flow = runner.run("openroad_grt", design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    assert not (grt_dir / "openroad_grt-failed.odb").exists()
    assert (grt_dir / "trace.json").is_file() and (grt_dir / "grt.odb").is_file()


def test_the_failure_of_the_process_names_the_flow_the_script_and_the_error(tmp_path, monkeypatch):
    """The error of the Tcl script is in the log of the tool. The failure says it."""
    require_tclsh()
    use_fake_openroad(monkeypatch)
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "global_route")
        runner.run("openroad_grt", openroad_design(tmp_path / "design"), flow_settings=settings)
    (grt_dir,) = (tmp_path / "xeda_run").rglob("openroad_grt")
    message = json.loads((grt_dir / "results.json").read_text())["error"]["message"]
    assert "openroad_grt" in message and "openroad_grt.tcl" in message
    assert "global_route failed" in message
    assert "code 1" in message


def test_a_typed_netlist_chain_runs_without_synthesis(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    assert not list((tmp_path / "xeda_run").rglob("yosys"))
    assert flow.succeeded


# ------------------------------------------------------------------------------ the failure message


def test_the_failure_lines_of_a_log_are_the_tcl_error_and_the_last_errors_of_the_tool():
    from xeda.flows.openroad.base import failure_lines

    log = "\n".join(
        [
            "[INFO GRT-0020] Min routing layer: 2",
            "[ERROR GRT-0001] first problem",
            *(f"[ERROR GRT-01{n:02d}] problem {n}" for n in range(1, 8)),
            "[WARNING GRT-0002] a warning",
            "Error: openroad_grt.tcl, 42 GRT-0116 Global routing finished with overflow.",
            "Trailing lines after the error.",
        ]
    )
    found = failure_lines(log)
    assert found[0] == "Error: openroad_grt.tcl, 42 GRT-0116 Global routing finished with overflow."
    assert found[1:] == [f"[ERROR GRT-01{n:02d}] problem {n}" for n in range(3, 8)]


def test_a_log_without_errors_gives_no_failure_lines():
    from xeda.flows.openroad.base import failure_lines

    assert failure_lines("[INFO GRT-0020] Min routing layer: 2\nall fine\n") == []
