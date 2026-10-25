"""The last OpenROAD flow, `openroad`. Its first process restarts from the globally routed
database, routes the design in detail, repairs the antenna violations of the route with diodes, and
places the filler cells. Its second process restarts from the routed database, writes the final
outputs, extracts the parasitics, analyzes the IR drop and reports the metrics. The results and the
verdicts come from the metrics of both.

The tests render the driver script of each process for the `tiny` platform and compare it with a
golden file. They run the rendered script under the fake OpenROAD, so a command that the OpenROAD
command list does not know fails the test.
"""

import contextlib
import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
import yaml

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda import tool as tool_module
from xeda.flows.openroad import Openroad, OpenroadStage, base, final
from xeda.flows.openroad.final import EffectiveFinal, effective
from xeda.flows.openroad.groups import ViaInPin
from xeda.platforms.model import CellPort
from xeda.run_dir import RunDirectory
from xeda.units import Capacitance, Power, Resistance, Voltage
from xeda.tool import NonZeroExitCode, Tool
from xeda.utils import ExecutableNotFound

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    register_openroad_flows,
    render_driver,
    run_fake_openroad,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import fake_calls, fake_returns, require_tclsh, use_fake_openroad

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics"
GOLDEN = Path(__file__).parent / "resources" / "openroad" / "route_defaults.tcl"
GOLDEN_FINAL = Path(__file__).parent / "resources" / "openroad" / "final_defaults.tcl"
TEMPLATES = Path(base.__file__).parent / "templates"


# ------------------------------------------------------------------------------ helpers


RCX_RULES = "tiny.rcx"
#: The GDSII file and the KLayout technology file of a platform that has the layout files. The
#: technology file lists one LEF file, as the file of a platform does.
GDS_FILE = "tiny.gds"
KLAYOUT_TECH = "tiny.lyt"
KLAYOUT_TECH_TEXT = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<technology>\n"
    " <name>tiny</name>\n"
    " <reader-options>\n"
    "  <lefdef>\n"
    "   <lef-files>./tiny.lef</lef-files>\n"
    "  </lefdef>\n"
    " </reader-options>\n"
    "</technology>\n"
)
#: The voltages of the supply nets of a platform that gives them: the power net and the ground net.
SUPPLY = {"power_nets": {"VDD": 1.1}, "ground_nets": {"VSS": 0.0}}


def variant_residue(
    tmp_path: Path, *, rules: bool, supply: dict[str, Any] | None, layout: bool = False
) -> Path:
    """Return a copy of the residue file of the `tiny` platform that names the OpenRCX rules file
    `tiny.rcx` (with `rules`), names the GDSII file and the KLayout technology file (with
    `layout`), and gives the voltages of the supply nets (`supply`, the leaves of `supply` of the
    platform model)."""
    data = yaml.safe_load(TINY_OPENROAD_RESIDUE.read_text())
    if rules:
        data["files"]["rcx_rules"] = RCX_RULES
    if layout:
        data["files"]["gds"] = [GDS_FILE]
        data["files"]["klayout_tech"] = KLAYOUT_TECH
    if supply:
        data["values"]["supply"] = supply
    path = tmp_path / "residue" / "tiny_openroad.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def route_flow(
    tmp_path: Path,
    settings: dict[str, Any] | None = None,
    *,
    model: dict[str, Any] | None = None,
    launched: bool = False,
    rules: bool = False,
    supply: dict[str, Any] | None = None,
    layout: bool = False,
) -> Openroad:
    """Return the flow on the `tiny` platform, ready to render: its run directory is
    `tmp_path/work`, its database and SDC file are in `tmp_path/inputs`, and `prepare_inputs()`
    has run.

    `settings` are nested settings (a group is a dictionary). `model` replaces fields of the
    platform model after `prepare_inputs()`. A flow built without a launcher deletes nothing in
    its run directory; with `launched`, the run directory is one that a launcher chose, under
    `tmp_path/xeda_run`. With `rules`, the platform names an OpenRCX rules file, with `layout`
    it names the GDSII and KLayout files, and with `supply` it gives the voltages of its supply
    nets (`variant_residue`)."""
    store = tiny_store(tmp_path, "store")
    residue = TINY_OPENROAD_RESIDUE
    if rules or supply or layout:
        residue = variant_residue(tmp_path, rules=rules, supply=supply, layout=layout)
        if rules:
            (store / "tiny" / RCX_RULES).write_text("rules\n")
        if layout:
            (store / "tiny" / GDS_FILE).write_bytes(b"gds\n")
            (store / "tiny" / KLAYOUT_TECH).write_text(KLAYOUT_TECH_TEXT)
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = {**tiny_settings(store, residue=residue), **(settings or {})}
    run_path = tmp_path / "work"
    run_directory = None
    if launched:
        run_root = tmp_path / "xeda_run"
        run_path = run_root / "counter" / "openroad"
        run_directory = RunDirectory.claimed(run_path, run_root)
    run_path.mkdir(parents=True, exist_ok=True)
    flow = Openroad(
        Openroad.Settings.from_input(given, design_root=design.design_root, runner_cwd=tmp_path),
        design,
        run_path,
        run_directory=run_directory,
    )
    flow.init()
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "grt.odb").write_text("odb\n")
    (inputs / "grt.sdc").write_text("create_clock -name clk -period 5 [get_ports clk]\n")
    flow.inputs.odb = inputs / "grt.odb"
    flow.inputs.sdc = inputs / "grt.sdc"
    flow.prepare_inputs()
    if model:
        flow.platform_model = flow.platform_model.model_copy(update=model)
    return flow


def final_flow(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> Openroad:
    """Return the flow on a platform that has OpenRCX rules and the voltages of its supply nets
    (`route_flow`), ready to render the second process."""
    return route_flow(tmp_path, settings, **{"rules": True, "supply": SUPPLY, **kwargs})


def render(flow: Openroad) -> str:
    return render_driver(flow, "openroad_route")


def render_final(flow: Openroad) -> str:
    """Render the driver of the second process. It writes into the files that the process
    declares as outputs, so they are set first."""
    flow.set_final_outputs()
    return render_driver(flow, "openroad_final")


def run_script(flow: Openroad, script: str | None = None) -> list[list[str]]:
    """Run the driver of `flow` under the fake OpenROAD in its run directory; return the commands
    that the fake recorded, one list of words for each."""
    require_tclsh()
    text = render(flow) if script is None else script
    done = run_fake_openroad(flow.run_path, text, driver="openroad_route")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(flow.run_path)


def called(run_dir: Path) -> list[str]:
    """The names of the commands that the fake OpenROAD recorded in `run_dir`, in order. Unlike
    `fake_calls`, it does not fail on a Tcl error, for a test that makes a command fail."""
    found: list[str] = []
    expect_name = False
    for line in (run_dir / "fake_openroad.calls").read_text().splitlines():
        kind, _, value = line.partition(" ")
        if kind == "CALL":
            expect_name = True
        elif kind == "ARG" and expect_name:
            found.append(value)
            expect_name = False
    return found


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


def route_body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
    return stage(render(route_flow(tmp_path, settings, **kwargs)), "xeda_detail_route")


def fill_body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
    return stage(render(route_flow(tmp_path, settings, **kwargs)), "xeda_fillcell")


def words(found: list[str], name: str) -> list[str]:
    """The lines of the stage that start with the command `name`."""
    return [line for line in found if line.split()[0] == name]


def names(calls: list[list[str]], *wanted: str) -> list[str]:
    return [call[0] for call in calls if call[0] in wanted]


# ------------------------------------------------------------------------------ the golden


def tokens(tmp_path: Path, text: str) -> str:
    """Replace the places that differ from test to test by tokens."""
    for root in sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True):
        text = text.replace(f"{root}/store", "@STORE@").replace(f"{root}/inputs", "@INPUTS@")
        text = text.replace(f"{root}/work", "@RUN@")
    return text


def test_the_defaults_render_the_golden_script(tmp_path):
    script = tokens(tmp_path, render(route_flow(tmp_path)))
    assert script == GOLDEN.read_text()


def test_the_defaults_route_repair_the_antennas_and_fill_as_openroad_flow_scripts_does(tmp_path):
    """The commands and the flags of the default stages, line by line. The platform has an
    antenna cell, and it does not say that the router cannot use it."""
    assert route_body(tmp_path) == [
        "xeda_stage detailedroute",
        "if {![grt::have_routes]} {",
        'error "xeda: the database has no global routes"',
        "}",
        "set_propagated_clock [all_clocks]",
        'set route_args [list -output_drc "reports/drc.rpt" -output_maze "reports/maze.log" '
        '-output_guide_coverage "reports/guide_coverage.csv" -verbose "1" '
        '-droute_end_iter "64" -drc_report_iter_step "5"]',
        "detailed_route {*}$route_args",
        "set pass 1",
        'if {[repair_antennas "ANTENNA_X1"]} {',
        "detailed_route {*}$route_args",
        "}",
        "while {[check_antennas] && $pass < 5} {",
        'repair_antennas "ANTENNA_X1"',
        "detailed_route {*}$route_args",
        "incr pass",
        "}",
        'check_antennas -report_file "reports/antennas.rpt"',
        "if {![design_is_routed]} {",
        'error "xeda: the design has unrouted nets"',
        "}",
        "report_design_area",
    ]
    assert fill_body(tmp_path / "fill") == [
        "xeda_stage fillcell",
        'filler_placement [list "FILL_X1"]',
        "check_placement",
    ]


def test_the_defaults_run_the_commands_of_the_flow_in_order(tmp_path):
    calls = run_script(route_flow(tmp_path))
    commands = [call[0] for call in calls if call[0] != "utl::set_metrics_stage"]
    assert commands == [
        "read_liberty",
        "read_db",
        "read_sdc",
        *["set_layer_rc"] * 7,
        "set_wire_rc",
        "set_wire_rc",
        "set_dont_use",
        "grt::have_routes",
        "all_clocks",
        "set_propagated_clock",
        "detailed_route",
        "repair_antennas",
        "check_antennas",
        "check_antennas",
        "design_is_routed",
        "report_design_area",
        "filler_placement",
        "check_placement",
        "write_db",
        "write_sdc",
    ]


# ------------------------------------------------------------------------------ the restart


def test_the_process_starts_from_the_database_and_the_sdc_file_of_the_global_route(tmp_path):
    found = lines(render(route_flow(tmp_path)))
    assert f'read_db "{tmp_path / "inputs" / "grt.odb"}"' in found
    assert [line for line in found if line.startswith("read_sdc")] == [
        f'read_sdc "{tmp_path / "inputs" / "grt.sdc"}"'
    ]
    assert not [line for line in found if line.split()[0] in ("read_verilog", "link_design")]
    assert not [line for line in found if line.split()[0] == "read_lef"]


def test_the_hierarchy_reaches_the_read_of_the_database(tmp_path):
    flow = route_flow(tmp_path, {"hierarchical": True})
    assert f'read_db -hier "{tmp_path / "inputs" / "grt.odb"}"' in lines(render(flow))


def test_the_wire_rc_and_the_cells_not_to_use_are_set_before_the_route(tmp_path):
    calls = run_script(route_flow(tmp_path))
    order = [call[0] for call in calls]
    route = order.index("detailed_route")
    for name in ("set_layer_rc", "set_wire_rc", "set_dont_use", "read_sdc", "read_db"):
        assert max(i for i, call in enumerate(order) if call == name) < route, name
    (dont_use,) = [call for call in calls if call[0] == "set_dont_use"]
    assert dont_use[1:] == ["INV_X4 NAND2_X4"]


def test_the_stage_sets_no_option_that_the_database_stores(tmp_path):
    """The placement flow sets the routing layers and the layer adjustments, and the database
    carries them. The router follows the guides of the database. So the stage sets none of them,
    and it sets no router state that Tcl commands keep."""
    flow = route_flow(tmp_path)
    called = names(
        run_script(flow),
        "set_routing_layers",
        "set_global_routing_layer_adjustment",
        "set_opt_config",
        "set_global_sizing_config",
        "set_dont_touch",
        "set_global_routing_random",
    )
    assert called == []
    assert "detailed_route_set" not in render(flow)


def test_the_cells_not_to_use_are_set_by_the_restart_header_and_not_by_the_stages(tmp_path):
    script = render(route_flow(tmp_path))
    assert not words(stage(script, "xeda_detail_route"), "set_dont_use")
    assert not words(stage(script, "xeda_fillcell"), "set_dont_use")
    assert words(lines(script), "set_dont_use")


def test_the_clocks_are_propagated_before_the_route(tmp_path):
    found = route_body(tmp_path)
    assert found.index("set_propagated_clock [all_clocks]") < found.index(
        "detailed_route {*}$route_args"
    )


# ------------------------------------------------------------------------------ the route


def test_the_precondition_of_the_route_is_a_global_route_in_the_database(tmp_path, monkeypatch):
    """Without global routes, the process stops before `detailed_route`, and the failed database
    holds the design."""
    fake_returns(monkeypatch, {("grt::have_routes",): "0"})
    flow = route_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_route")
    assert done.returncode != 0
    assert "the database has no global routes" in done.stderr + done.stdout
    assert "detailed_route" not in called(flow.run_path)
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert not (flow.run_path / "route.odb").exists()


def test_a_route_that_leaves_unrouted_nets_fails_and_keeps_the_database(tmp_path, monkeypatch):
    fake_returns(monkeypatch, {("design_is_routed",): "0"})
    flow = route_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_route")
    assert done.returncode != 0
    assert "the design has unrouted nets" in done.stderr + done.stdout
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert not (flow.run_path / "route.odb").exists()
    assert "filler_placement" not in called(flow.run_path)


def test_the_route_reports_go_to_fixed_names_under_the_directory_of_reports(tmp_path):
    found = route_body(tmp_path)
    (route,) = [line for line in found if line.startswith("set route_args")]
    assert '-output_drc "reports/drc.rpt"' in route
    assert '-output_maze "reports/maze.log"' in route
    assert 'check_antennas -report_file "reports/antennas.rpt"' in found


def test_the_flags_of_the_route_follow_the_settings(tmp_path):
    found = route_body(
        tmp_path,
        {
            "detailed_route": {
                "end_iteration": 32,
                "reroute_seed": 3,
                "reroute_swap_factor": 2,
                "drc_report_step": 0,
            }
        },
    )
    (route,) = [line for line in found if line.startswith("set route_args")]
    assert route.endswith(
        '-verbose "1" -droute_end_iter "32" -or_seed "3" -or_k "2.0" -drc_report_iter_step "0"]'
    )


def test_the_seed_and_the_swap_factor_use_the_underscore_spelling(tmp_path):
    (route,) = [
        line
        for line in route_body(
            tmp_path, {"detailed_route": {"reroute_seed": 1, "reroute_swap_factor": 1}}
        )
        if line.startswith("set route_args")
    ]
    assert "-or_seed" in route and "-or_k" in route
    assert "-or-seed" not in route and "-or-k" not in route


def test_the_via_in_pin_range_takes_the_bottom_layer_as_the_lowest_and_the_top_as_the_highest(
    tmp_path,
):
    found = route_body(tmp_path, {"detailed_route": {"via_in_pin": {"bottom": "m1", "top": "m3"}}})
    (route,) = [line for line in found if line.startswith("set route_args")]
    assert '-via_in_pin_bottom_layer "m1" -via_in_pin_top_layer "m3"' in route


def test_no_via_in_pin_flag_is_printed_without_a_range(tmp_path):
    (route,) = [line for line in route_body(tmp_path) if line.startswith("set route_args")]
    assert "via_in_pin" not in route


def test_the_router_gets_no_routing_layer_flags(tmp_path):
    """`detailed_route` of the installed OpenROAD fails on `-bottom_routing_layer` and
    `-top_routing_layer`. The layers of the route are the ones of the database."""
    text = render(
        route_flow(tmp_path, {"detailed_route": {"via_in_pin": {"bottom": "m1", "top": "m2"}}})
    )
    assert "routing_layer" not in text


def test_the_hatch_flags_come_after_the_flags_of_the_settings(tmp_path):
    found = route_body(
        tmp_path,
        {"detailed_route": {"extra_args": ["-db_process_node", "GF14"]}},
    )
    (route,) = [line for line in found if line.startswith("set route_args")]
    assert route.endswith('-drc_report_iter_step "5" "-db_process_node" "GF14"]')


def test_every_route_in_the_antenna_loop_gets_the_same_arguments(tmp_path):
    flow = route_flow(tmp_path)
    calls = run_script(flow)
    routes = [call for call in calls if call[0] == "detailed_route"]
    assert routes and all(call == routes[0] for call in routes)
    assert routes[0][1:] == [
        "-output_drc",
        "reports/drc.rpt",
        "-output_maze",
        "reports/maze.log",
        "-output_guide_coverage",
        "reports/guide_coverage.csv",
        "-verbose",
        "1",
        "-droute_end_iter",
        "64",
        "-drc_report_iter_step",
        "5",
    ]


# ------------------------------------------------------------------------------ the antenna loop


def test_the_antenna_loop_is_bounded_by_the_iterations_of_the_settings(tmp_path, monkeypatch):
    """With violations that never go, the loop repairs `reroute_iterations` times and routes again
    after each repair."""
    fake_returns(monkeypatch, {("repair_antennas",): "1", ("check_antennas",): "1"})
    for iterations in (1, 3):
        work = tmp_path / str(iterations)
        calls = run_script(
            route_flow(work, {"antenna": {"reroute_iterations": iterations}}),
        )
        assert len(names(calls, "repair_antennas")) == iterations
        assert len(names(calls, "detailed_route")) == 1 + iterations
        assert names(calls, "check_antennas")[-1] == "check_antennas"


def test_a_repair_that_changes_nothing_does_not_route_again(tmp_path):
    calls = run_script(route_flow(tmp_path))
    assert len(names(calls, "detailed_route")) == 1
    assert len(names(calls, "repair_antennas")) == 1
    assert len(names(calls, "check_antennas")) == 2  # the loop test, then the report


def test_a_repair_that_inserts_a_diode_routes_again_and_the_loop_ends_when_the_check_is_clean(
    tmp_path, monkeypatch
):
    fake_returns(monkeypatch, {("repair_antennas",): "1"})
    calls = run_script(route_flow(tmp_path))
    assert names(calls, "detailed_route", "repair_antennas", "check_antennas") == [
        "detailed_route",
        "repair_antennas",
        "detailed_route",
        "check_antennas",
        "check_antennas",
    ]


def test_the_loop_checks_the_antennas_after_the_last_route_and_writes_the_report(tmp_path):
    found = route_body(tmp_path)
    report = found.index('check_antennas -report_file "reports/antennas.rpt"')
    assert report > max(i for i, line in enumerate(found) if line.startswith("detailed_route"))
    assert found[report + 1 :].index("report_design_area") > 0


def test_the_platform_diode_goes_to_the_repair(tmp_path):
    found = route_body(tmp_path, model={"antenna_cell": CellPort(cell="DIODE_X2", port="I")})
    assert 'if {[repair_antennas "DIODE_X2"]} {' in found
    assert 'repair_antennas "DIODE_X2"' in found


def test_a_platform_that_names_no_diode_leaves_the_choice_to_the_router(tmp_path):
    found = route_body(tmp_path, model={"antenna_cell": None, "diode_usable": None})
    assert "if {[repair_antennas]} {" in found
    assert "repair_antennas" in found


def test_a_platform_without_a_usable_diode_has_no_loop_and_says_so(tmp_path):
    found = route_body(tmp_path, model={"diode_usable": False})
    assert not words(found, "repair_antennas")
    assert not [line for line in found if "check_antennas]" in line]
    assert 'puts "xeda: the platform has no usable antenna diode; antenna repair skipped"' in found
    assert 'check_antennas -report_file "reports/antennas.rpt"' in found


def test_the_antenna_repair_can_be_switched_off_and_the_check_stays(tmp_path):
    found = route_body(tmp_path, {"antenna": {"repair": False}})
    assert not words(found, "repair_antennas")
    assert not [line for line in found if line.startswith("while")]
    assert 'check_antennas -report_file "reports/antennas.rpt"' in found
    assert not [line for line in found if line.startswith("puts")]


def test_the_iterations_of_the_loop_follow_the_settings(tmp_path):
    found = route_body(tmp_path, {"antenna": {"reroute_iterations": 2}})
    assert "while {[check_antennas] && $pass < 2} {" in found


def test_the_antenna_repair_allows_no_congestion_after_the_detailed_route(tmp_path):
    """`-allow_congestion` belongs to the global route. After the detailed route it has no
    meaning, so the flow gives it to no command."""
    assert "allow_congestion" not in render(route_flow(tmp_path))


# ------------------------------------------------------------------------------ the fill


def test_the_fill_cells_of_the_platform_are_placed_after_the_last_change_of_the_route(tmp_path):
    flow = route_flow(tmp_path)
    order = [call[0] for call in run_script(flow)]
    last_route = max(i for i, name in enumerate(order) if name == "detailed_route")
    fill = order.index("filler_placement")
    assert last_route < order.index("design_is_routed") < fill
    assert order[fill + 1] == "check_placement"
    assert order.index("write_db") > fill


def test_the_fill_cells_of_the_settings_replace_those_of_the_platform(tmp_path):
    found = fill_body(tmp_path, {"fill": {"cells": ["FILL_X*", "DECAP_X1"]}})
    assert 'filler_placement [list "FILL_X*" "DECAP_X1"]' in found


def test_the_fill_cells_are_one_list_argument_of_the_command(tmp_path):
    calls = run_script(route_flow(tmp_path, {"fill": {"cells": ["FILL_X1", "FILL_X2"]}}))
    (fill,) = [call for call in calls if call[0] == "filler_placement"]
    assert len(fill) == 2 and fill[1] == "FILL_X1 FILL_X2"


def test_an_empty_list_of_fill_cells_places_no_filler_and_says_so(tmp_path):
    found = fill_body(tmp_path, {"fill": {"cells": []}})
    assert not words(found, "filler_placement")
    assert not words(found, "check_placement")
    assert 'puts "xeda: no filler cells; filler placement skipped"' in found


def test_a_platform_without_fill_cells_places_no_filler_and_says_so(tmp_path):
    found = fill_body(tmp_path, model={"fill_cells": []})
    assert not words(found, "filler_placement")
    assert 'puts "xeda: no filler cells; filler placement skipped"' in found


# ------------------------------------------------------------------------------ the outputs


def test_the_working_pair_is_written_at_the_end(tmp_path):
    found = lines(render(route_flow(tmp_path)))
    assert found[-2:] == ['write_db "route.odb"', 'write_sdc -no_timestamp "route.sdc"']
    assert found.index('write_db "route.odb"') > found.index("xeda_fillcell")


def test_a_failed_stage_writes_the_failed_database_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "detailed_route")
    flow = route_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_route")
    assert done.returncode != 0
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert not (flow.run_path / "route.odb").exists()
    assert not (flow.run_path / "route.sdc").exists()


def test_a_failed_fill_leaves_the_failed_database(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "filler_placement")
    flow = route_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_route")
    assert done.returncode != 0
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert not (flow.run_path / "route.odb").exists()


# ------------------------------------------------------------------------------ the process


def in_run_directory(flow: Openroad, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the tool of `flow` in the run directory of the flow. The launcher changes into that
    directory before it runs a flow; a test leaves its own directory as it is."""
    execute = Tool.execute
    replacing = tool_module.replacing_file

    def execute_in_run_directory(self: Tool, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cwd", flow.run_path)
        return execute(self, *args, **kwargs)

    @contextlib.contextmanager
    def replacing_in_run_directory(path: Any, *args: Any, **kwargs: Any) -> Any:
        """`Tool.run` writes `env.sh` beside the process, which is the run directory."""
        with replacing(Path(flow.run_path) / path, *args, **kwargs) as handle:
            yield handle

    monkeypatch.setattr(Tool, "execute", execute_in_run_directory)
    monkeypatch.setattr(tool_module, "replacing_file", replacing_in_run_directory)


def run_route(
    tmp_path: Path, monkeypatch, settings: dict[str, Any] | None = None, **kwargs: Any
) -> Openroad:
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path, settings, **kwargs)
    in_run_directory(flow, monkeypatch)
    flow.run_route()
    return flow


def test_the_process_writes_the_working_pair_the_metrics_and_the_reports_directory(
    tmp_path, monkeypatch
):
    flow = run_route(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    for name in ("openroad_route.tcl", "openroad_route.log", "route.odb", "route.sdc"):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "reports").is_dir()
    assert not (run_dir / "openroad-failed.odb").exists()
    assert json.loads((run_dir / "metrics_route.json").read_text()) == json.loads(
        (RECORDED / "openroad_route.json").read_text()
    )
    metrics = flow.read_metrics("metrics_route.json")
    assert metrics is not None and metrics["detailedroute__route__drc_errors"] == 0


def test_the_process_runs_the_script_of_the_driver_with_the_metrics_file(tmp_path, monkeypatch):
    flow = run_route(tmp_path, monkeypatch)
    script = (Path(flow.run_path) / "openroad_route.tcl").read_text()
    assert script == render(flow)


def test_the_process_makes_every_file_it_writes_ready_first(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    flow.run_route()
    assert sorted(ready) == sorted(
        [
            "metrics_route.json",
            "route.odb",
            "route.sdc",
            "reports/drc.rpt",
            "reports/maze.log",
            "reports/guide_coverage.csv",
            "reports/antennas.rpt",
            "openroad-failed.odb",
            *(f"reports/drc.rpt-{n}.rpt" for n in range(5, 65, 5)),
        ]
    )


@pytest.mark.parametrize(
    "step, end, expected",
    [
        (5, 64, [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]),
        (2, 6, [2, 4, 6]),
        (1, 3, [1, 2, 3]),
        (4, 3, []),
        (0, 64, []),
    ],
)
def test_the_reports_of_the_iterations_of_the_router_are_files_the_process_writes(
    step, end, expected
):
    """The router writes `<report>-<n>.rpt` after every step-th iteration. The names are fixed by
    the settings, and Xeda makes each ready like any other file that the tool writes."""
    route = final.DetailedRouteGroup(drc_report_step=step, end_iteration=end)
    assert final.drc_iteration_reports(route, "reports/drc.rpt") == [
        f"reports/drc.rpt-{n}.rpt" for n in expected
    ]


def test_the_process_removes_the_failed_database_of_an_earlier_run(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path, launched=True)
    in_run_directory(flow, monkeypatch)
    (flow.run_path / "openroad-failed.odb").write_text("old\n")
    flow.run_route()
    assert not (flow.run_path / "openroad-failed.odb").exists()


def test_the_failure_of_the_process_names_the_flow_the_script_and_the_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "detailed_route")
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    with pytest.raises(NonZeroExitCode) as error:
        flow.run_route()
    message = str(error.value)
    assert "openroad" in message and "openroad_route.tcl" in message
    assert "detailed_route failed" in message
    assert (flow.run_path / "openroad-failed.odb").is_file()


# ------------------------------------------------------------------------------ the options


@pytest.mark.parametrize(
    "settings",
    [
        {"detailed_route": {"end_iteration": 1}},
        {"detailed_route": {"end_iteration": 64, "drc_report_step": 0}},
        {"detailed_route": {"reroute_seed": 0}},
        {"detailed_route": {"reroute_seed": 7, "reroute_swap_factor": 0}},
        {"detailed_route": {"via_in_pin": {"bottom": "m1", "top": "m4"}}},
        {"detailed_route": {"extra_args": ["-db_process_node", "GF14"]}},
        {"detailed_route": {"via_access_layer": "m2", "min_access_points": 3}},
        {"detailed_route": {"clean_patches": True, "save_guide_updates": True}},
        {"antenna": {"repair": False}},
        {"antenna": {"reroute_iterations": 1}},
        {"antenna": {"reroute_iterations": 9}},
        {"fill": {"cells": []}},
        {"fill": {"cells": ["FILL_X1"]}},
        {"hierarchical": True},
        {"wire_rc": {"signal_layer": "m3"}},
    ],
    ids=lambda settings: json.dumps(settings, sort_keys=True),
)
def test_every_choice_of_the_first_process_renders_and_runs(tmp_path, settings):
    calls = run_script(route_flow(tmp_path, settings))
    assert names(calls, "detailed_route", "write_db", "write_sdc")[-2:] == ["write_db", "write_sdc"]
    assert names(calls, "detailed_route")


# ------------------------------------------------------------------------------ the templates


def test_the_driver_lists_its_stages_by_literal_names():
    driver = (TEMPLATES / "drivers" / "openroad_route.tcl.j2").read_text()
    includes = re.findall(r"\{%\s*include\s+(.*?)\s*%\}", driver)
    assert includes and all(re.fullmatch(r'"[\w./]+"', include) for include in includes)
    for include in includes:
        assert (TEMPLATES / include.strip('"')).is_file()


@pytest.mark.parametrize("template", ["detail_route", "fillcell"])
def test_a_stage_template_writes_no_user_text_without_a_filter(template):
    text = (TEMPLATES / "stages" / f"{template}.tcl.j2").read_text()
    for expression in re.findall(r"\{\{(.*?)\}\}", text):
        expression = expression.strip()
        assert "|" in expression or expression.startswith("render_flags("), expression


# ------------------------------------------------------------------------------ the effective values


def test_the_effective_values_are_a_pure_function_of_the_settings_and_the_model(tmp_path):
    flow = route_flow(tmp_path)
    settings, model = flow.settings, flow.platform_model
    before = (settings.model_dump_json(), model.model_dump_json())
    values = effective(settings, model)
    assert isinstance(values, EffectiveFinal)
    assert values == effective(settings, model) == flow.effective()
    assert (settings.model_dump_json(), model.model_dump_json()) == before


def test_the_diode_comes_from_the_platform(tmp_path):
    flow = route_flow(tmp_path)
    plain = effective(flow.settings, flow.platform_model)
    assert (plain.diode_cell, plain.diode_usable) == ("ANTENNA_X1", True)
    unknown = flow.platform_model.model_copy(update={"antenna_cell": None, "diode_usable": None})
    values = effective(flow.settings, unknown)
    assert (values.diode_cell, values.diode_usable) == (None, True)
    unusable = flow.platform_model.model_copy(update={"diode_usable": False})
    assert effective(flow.settings, unusable).diode_usable is False


def test_the_fill_cells_come_from_the_setting_and_else_from_the_platform(tmp_path):
    flow = route_flow(tmp_path)
    assert effective(flow.settings, flow.platform_model).fill_cells == ("FILL_X1",)
    flow.settings.fill.cells = ["FILL_X2", "FILL_X4"]
    assert effective(flow.settings, flow.platform_model).fill_cells == ("FILL_X2", "FILL_X4")
    flow.settings.fill.cells = []
    assert effective(flow.settings, flow.platform_model).fill_cells == ()
    flow.settings.fill.cells = None
    bare = flow.platform_model.model_copy(update={"fill_cells": []})
    assert effective(flow.settings, bare).fill_cells == ()


def test_the_via_in_pin_range_comes_from_the_setting(tmp_path):
    flow = route_flow(tmp_path)
    assert effective(flow.settings, flow.platform_model).via_in_pin is None
    flow.settings.detailed_route.via_in_pin = ViaInPin(bottom="m1", top="m2")
    assert effective(flow.settings, flow.platform_model).via_in_pin == ViaInPin(
        bottom="m1", top="m2"
    )


# ------------------------------------------------------------------------------ planning


def test_the_flow_reads_the_database_of_the_global_route_and_no_source():
    assert Openroad.design_parts == frozenset({"rtl"})
    assert Openroad.reads_sources is None
    from xeda.flow.io import declared_inputs

    types = {name: inp.types for name, inp in declared_inputs(Openroad).items()}
    assert types == {
        "odb": (SourceType.OpenroadDbGlobalRouted,),
        "sdc": (SourceType.OpenroadSdcGlobalRouted,),
    }


def test_the_reports_and_the_working_files_have_fixed_names():
    assert Openroad.reports == {
        "drc": "reports/drc.rpt",
        "maze": "reports/maze.log",
        "guide_coverage": "reports/guide_coverage.csv",
        "antennas": "reports/antennas.rpt",
        "timing": "reports/timing.rpt",
        "clock_skew": "reports/clock_skew.rpt",
        "power": "reports/power.rpt",
        "check_setup": "reports/check_setup.rpt",
    }
    assert Openroad.route_reports == ("drc", "maze", "guide_coverage", "antennas")
    assert Openroad.metrics_files == ("metrics_route.json", "metrics_final.json")
    assert Openroad.working_files == {"odb": "route.odb", "sdc": "route.sdc"}
    for name in (*Openroad.reports.values(), *Openroad.working_files.values()):
        assert not Path(name).is_absolute() and ".." not in Path(name).parts


def test_the_prepared_inputs_are_the_files_that_the_dry_run_locates(tmp_path):
    flow = route_flow(tmp_path)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def plan(tmp_path: Path, *texts: str):
    """Plan `openroad` on the tiny platform with the four flows registered. `texts` are
    `KEY=VALUE` settings as the command line gives them."""
    store = tiny_store(tmp_path)
    given = dict(text.split("=", 1) for text in [*openroad_settings(store), *texts])
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        "openroad", openroad_design(tmp_path / "design"), flow_settings=given
    )


def refusal(tmp_path: Path, *texts: str) -> str:
    with register_openroad_flows(), pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, *texts)
    message = " ".join(str(error.value).split())
    assert "needs a unit" not in message, "the refusal is about a missing unit: " + message
    return message


def test_the_settings_of_the_first_process_plan_the_four_flows(tmp_path):
    with register_openroad_flows():
        planned = plan(
            tmp_path,
            "detailed_route.reroute_seed=3",
            "detailed_route.reroute_swap_factor=2",
            "antenna.reroute_iterations=2",
            "fill.cells=FILL_X1,FILL_X2",
        )
    assert [node.name for node in planned.nodes] == [
        "openroad_place",
        "openroad_cts",
        "openroad_grt",
        "openroad",
    ]
    assert [item.describe() for item in planned.nodes[-1].inputs] == [
        "odb <- openroad_grt.odb",
        "sdc <- openroad_grt.sdc",
    ]


@pytest.mark.parametrize(
    "texts, expected",
    [
        (
            ["detailed_route.reroute_swap_factor=2"],
            "`detailed_route.reroute_swap_factor` has no effect without "
            "`detailed_route.reroute_seed`",
        ),
        (
            ["antenna.repair=false", "antenna.reroute_iterations=2"],
            "`antenna.reroute_iterations` has no effect with `antenna.repair: false`",
        ),
        (
            ["detailed_route.extra_args=-bottom_routing_layer,m2"],
            "`-bottom_routing_layer` in `detailed_route.extra_args` is not allowed",
        ),
        (
            ["extraction.method=rcx"],
            "`extraction.method: rcx` needs the rules file of OpenRCX, and the platform 'tiny' "
            "gives none (`files.rcx_rules` in its residue file): set `extraction.method` to "
            "`estimate` or `auto`",
        ),
        (
            ["detailed_route.extra_args=-droute_end_iter,3"],
            "`-droute_end_iter` in `detailed_route.extra_args` is the setting "
            "`detailed_route.end_iteration`",
        ),
    ],
)
def test_the_refusals_of_the_first_process_come_at_planning_with_the_way_out(
    tmp_path, texts, expected
):
    assert expected in refusal(tmp_path, *texts)


# ============================================================================== the second process
#
# The second process restarts from the routed database `route.odb` and its SDC file `route.sdc`,
# which the first process left in the run directory. It writes the declared outputs, extracts the
# parasitics, analyzes the IR drop and reports the metrics.


def final_body(tmp_path: Path, name: str, settings: dict[str, Any] | None = None, **kwargs):
    """The lines of the stage `name` of the second process, with the places of the test as
    tokens."""
    script = render_final(final_flow(tmp_path, settings, **kwargs))
    return [tokens(tmp_path, line) for line in stage(script, name)]


def outputs_body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any):
    return final_body(tmp_path, "xeda_final_outputs", settings, **kwargs)


def report_body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any):
    return final_body(tmp_path, "xeda_final_report", settings, **kwargs)


def run_final_script(flow: Openroad) -> list[list[str]]:
    """Run the driver of the second process under the fake OpenROAD in the run directory, where
    the first process has left its pair of files; return the commands that the fake recorded."""
    require_tclsh()
    for name in ("route.odb", "route.sdc"):
        (flow.run_path / name).write_text(f"{name}\n")
    (flow.run_path / "reports").mkdir(exist_ok=True)
    done = run_fake_openroad(flow.run_path, render_final(flow), driver="openroad_final")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(flow.run_path)


# ------------------------------------------------------------------------------ the restart


def test_the_second_process_starts_from_the_working_pair_of_the_first(tmp_path):
    script = render_final(final_flow(tmp_path))
    found = lines(script)
    assert 'read_db "route.odb"' in found
    assert [line for line in found if line.startswith("read_sdc")] == ['read_sdc "route.sdc"']
    assert not [line for line in found if line.split()[0] in ("read_verilog", "link_design")]
    assert not [line for line in found if line.split()[0] == "read_lef"]
    assert "grt.odb" not in script and "grt.sdc" not in script


def test_the_hierarchy_reaches_the_read_of_the_routed_database(tmp_path):
    flow = final_flow(tmp_path, {"hierarchical": True})
    assert 'read_db -hier "route.odb"' in lines(render_final(flow))


def restart_header(flow: Openroad, variables: str) -> list[str]:
    """The lines of the restart header for a driver that sets `variables` (Jinja statements)."""
    flow.bind_template_values()
    template = flow.jinja_env.from_string(
        '{% set restart = "database" %}'
        + variables
        + '{% set sdc_files = [] %}{% include "restart.tcl.j2" %}'
    )
    return lines(template.render(settings=flow.settings, design=flow.design))


def test_the_restart_header_reads_the_database_that_the_driver_names_and_else_its_input(tmp_path):
    from xeda.utils import tcl_word

    flow = final_flow(tmp_path)
    reads = lambda header: [line for line in header if line.startswith("read_db")]  # noqa: E731
    assert reads(restart_header(flow, "")) == [f'read_db "{tmp_path / "inputs" / "grt.odb"}"']
    named = "a b$[x].odb"
    assert reads(restart_header(flow, f'{{% set database_file = "{named}" %}}')) == [
        f"read_db {tcl_word(named)}"
    ]
    assert tcl_word(named) == '"a b\\$\\[x\\].odb"'


# ------------------------------------------------------------------------------ the extraction


@pytest.mark.parametrize(
    "rules, method, expected",
    [
        (False, "auto", "estimate"),
        (True, "auto", "rcx"),
        (True, "rcx", "rcx"),
        (True, "estimate", "estimate"),
        (False, "estimate", "estimate"),
    ],
)
def test_the_extraction_follows_the_rules_of_the_platform_and_the_method(
    tmp_path, rules, method, expected
):
    flow = route_flow(tmp_path, {"extraction": {"method": method}}, rules=rules)
    values = flow.effective()
    assert values.extraction == expected
    assert (values.rcx_rules is not None) is (expected == "rcx")
    if expected == "rcx":
        assert values.rcx_rules.name == RCX_RULES
        assert values.rcx_rules.parent == flow.platform_model.files["liberty"][0].parent


def test_rcx_on_a_platform_without_rules_is_refused_by_the_effective_values(tmp_path):
    flow = route_flow(tmp_path)
    flow.settings.extraction.method = "rcx"
    with pytest.raises(FlowSettingsException, match="extraction.method"):
        flow.effective()


@pytest.mark.parametrize("rules", [False, True])
@pytest.mark.parametrize("method", ["auto", "estimate"])
def test_the_files_that_the_flow_opens_are_those_the_dry_run_locates(tmp_path, rules, method):
    """`prepare_inputs()` registers the files of the platform that the flow opens, and
    `locate_prepared_inputs()`, which the dry run uses, returns the same files in the same order.
    The rules file is one of them exactly when the extraction is `rcx`."""
    flow = route_flow(tmp_path, {"extraction": {"method": method}}, rules=rules)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs
    names = [path.name for path in flow.implicit_inputs]
    assert (RCX_RULES in names) is (rules and method == "auto")


def test_the_rules_file_is_opened_when_the_extraction_is_rcx(tmp_path):
    flow = final_flow(tmp_path, {"extraction": {"method": "rcx"}})
    assert RCX_RULES in [path.name for path in flow.implicit_inputs]
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


# ------------------------------------------------------------------------------ the supply


def test_the_supply_nets_are_those_of_the_platform_with_the_power_nets_first(tmp_path):
    values = final_flow(tmp_path).effective()
    assert values.ir_drop is True
    assert values.supply_voltages == (("VDD", 1.1), ("VSS", 0.0))
    assert values.supply_nets == ("VDD", "VSS")
    assert values.ir_drop_reports == ("reports/irdrop_1.rpt", "reports/irdrop_2.rpt")


def test_many_supply_nets_are_numbered_from_one_in_the_order_of_the_platform(tmp_path):
    supply = {"power_nets": {"VDD1": 1.8, "VDD2": 1.2}, "ground_nets": {"VSS": 0.0}}
    values = final_flow(tmp_path, supply=supply).effective()
    assert values.supply_nets == ("VDD1", "VDD2", "VSS")
    assert values.supply_voltages == (("VDD1", 1.8), ("VDD2", 1.2), ("VSS", 0.0))
    assert values.ir_drop_reports == tuple(f"reports/irdrop_{n}.rpt" for n in (1, 2, 3))


def test_the_analysis_can_be_switched_off(tmp_path):
    values = final_flow(tmp_path, {"ir_drop": {"enabled": False}}).effective()
    assert values.ir_drop is False
    assert values.ir_drop_reports == ()
    assert values.supply_nets == ("VDD", "VSS")  # the platform still gives them


def test_a_platform_that_gives_no_voltage_has_no_supply_net_and_no_report(tmp_path):
    values = route_flow(tmp_path, rules=True).effective()
    assert values.ir_drop is True
    assert values.supply_voltages == () and values.supply_nets == ()
    assert values.ir_drop_reports == ()


def test_the_effective_values_of_the_second_process_do_not_change_the_settings_or_the_model(
    tmp_path,
):
    flow = final_flow(tmp_path)
    settings, model = flow.settings, flow.platform_model
    before = (settings.model_dump_json(), model.model_dump_json())
    values = effective(settings, model)
    assert values == effective(settings, model) == flow.effective()
    assert (settings.model_dump_json(), model.model_dump_json()) == before
    assert isinstance(values, EffectiveFinal)


# ------------------------------------------------------------------------------ the golden


def test_the_defaults_render_the_golden_script_of_the_second_process(tmp_path):
    script = tokens(tmp_path, render_final(final_flow(tmp_path)))
    assert script == GOLDEN_FINAL.read_text()


# ------------------------------------------------------------------------------ the outputs


def test_the_defaults_write_the_outputs_and_extract_the_parasitics_as_openroad_flow_scripts_does(
    tmp_path,
):
    """The order is that of the final stage of OpenROAD-flow-scripts: connect the cells that the
    repair added, write the database and the SDC file, delete the routing obstructions, then write
    the DEF file and the netlist, then extract. The reports come after the extraction."""
    assert outputs_body(tmp_path) == [
        "xeda_stage finish",
        "set_propagated_clock [all_clocks]",
        "global_connect",
        'write_db "@RUN@/final.odb"',
        'write_sdc -no_timestamp "@RUN@/final.sdc"',
        "foreach o [[ord::get_db_block] getObstructions] { odb::dbObstruction_destroy $o }",
        'write_def "@RUN@/final.def"',
        'write_verilog "@RUN@/final.v" -remove_cells [xeda_physical_only_masters]',
        "define_process_corner -ext_model_index 0 X",
        'set_extraction_rules_file "@STORE@/tiny/tiny.rcx"',
        "extract_parasitics",
        'write_spef "@RUN@/final.spef"',
        'read_spef "@RUN@/final.spef"',
        'write_sdf -no_timestamp "@RUN@/final.sdf"',
        'write_abstract_lef -bloat_occupied_layers "@RUN@/final.lef"',
    ]


def test_a_platform_without_rules_estimates_the_parasitics_from_the_route(tmp_path):
    found = outputs_body(tmp_path, rules=False)
    assert "estimate_parasitics -global_routing" in found
    assert not [line for line in found if line.split()[0] in ("write_spef", "read_spef")]
    assert not words(found, "extract_parasitics")
    assert not words(found, "set_extraction_rules_file")


def test_the_estimate_can_be_chosen_on_a_platform_that_has_rules(tmp_path):
    found = outputs_body(tmp_path, {"extraction": {"method": "estimate"}})
    assert "estimate_parasitics -global_routing" in found
    assert "extract_parasitics" not in found


def test_the_outputs_of_the_second_process_are_the_declared_files_of_the_run_directory(tmp_path):
    flow = final_flow(tmp_path)
    flow.set_final_outputs()
    run = Path(flow.run_path)
    assert flow.outputs.odb == run / "final.odb"
    assert flow.outputs.sdc == run / "final.sdc"
    assert flow.outputs.def_file == run / "final.def"
    assert flow.outputs.netlist == run / "final.v"
    assert flow.outputs.sdf == run / "final.sdf"
    assert flow.outputs.abstract_lef == run / "final.lef"
    assert flow.outputs.spef == run / "final.spef"
    assert (flow.outputs.gds, flow.outputs.cdl, flow.outputs.timing_model) == (None, None, None)


def test_no_parasitics_file_is_declared_when_the_extraction_is_an_estimate(tmp_path):
    flow = route_flow(tmp_path)
    flow.set_final_outputs()
    assert flow.outputs.spef is None
    assert flow.outputs.odb == Path(flow.run_path) / "final.odb"


def test_the_netlist_leaves_out_the_cells_that_only_occupy_the_layout(tmp_path, monkeypatch):
    """The fill and tap cells are no part of the logic. The netlist is written without the masters
    whose type is one that only occupies the layout, so a simulation or an equivalence check sees
    none of them."""
    fake_returns(
        monkeypatch,
        {("getLibs",): "fake_lib", ("getMasters",): "fake_master", ("getType",): "ENDCAP_TOP"},
    )
    calls = run_final_script(final_flow(tmp_path))
    (netlist,) = [call for call in calls if call[0] == "write_verilog"]
    assert netlist[2:] == ["-remove_cells", "FAKE_MASTER"]


def test_a_master_of_another_type_stays_in_the_netlist(tmp_path, monkeypatch):
    fake_returns(
        monkeypatch,
        {("getLibs",): "fake_lib", ("getMasters",): "fake_master", ("getType",): "CORE"},
    )
    calls = run_final_script(final_flow(tmp_path))
    (netlist,) = [call for call in calls if call[0] == "write_verilog"]
    assert netlist[2:] == ["-remove_cells", ""]


def test_the_second_process_writes_the_netlist_from_the_database_it_read(tmp_path):
    """A netlist written in the process that routed the design has the supply ports of the cells
    too. The second process reads the routed database first, so its netlist is the one of a
    reload."""
    order = [call[0] for call in run_final_script(final_flow(tmp_path))]
    assert order.index("read_db") < order.index("global_connect") < order.index("write_verilog")
    assert order.count("read_db") == 1 and "write_verilog" in order


def test_the_defaults_run_the_commands_of_the_second_process_in_order(tmp_path):
    calls = run_final_script(final_flow(tmp_path))
    commands = [call[0] for call in calls if call[0] != "utl::set_metrics_stage"]
    assert commands == [
        "read_liberty",
        "read_db",
        "read_sdc",
        *["set_layer_rc"] * 7,
        "set_wire_rc",
        "set_wire_rc",
        "set_dont_use",
        "all_clocks",
        "set_propagated_clock",
        "global_connect",
        "write_db",
        "write_sdc",
        "ord::get_db_block",
        "write_def",
        "ord::get_db",  # the masters that only occupy the layout
        "write_verilog",
        "define_process_corner",
        "set_extraction_rules_file",
        "extract_parasitics",
        "write_spef",
        "read_spef",
        "write_sdf",
        "write_abstract_lef",
        "set_pdnsim_net_voltage",
        "analyze_power_grid",
        "set_pdnsim_net_voltage",
        "analyze_power_grid",
        "report_checks",
        "report_clock_skew",
        "report_power",
        "report_cell_usage",
        "report_tns_metric",
        "report_tns_metric",
        "report_worst_slack_metric",
        "report_worst_slack_metric",
        "report_fmax_metric",
        "report_clock_skew_metric",
        "report_clock_skew_metric",
        "report_erc_metrics",
        "report_power_metric",
        "report_design_area_metrics",
    ]


def test_the_second_process_sets_the_stage_of_the_metrics_to_finish(tmp_path):
    calls = run_final_script(final_flow(tmp_path))
    assert [call[1] for call in calls if call[0] == "utl::set_metrics_stage"] == ["finish__{}"]


# ------------------------------------------------------------------------------ the report


#: The text reports that the final process writes by default, in order.
TEXT_REPORTS = [
    'report_checks -path_delay min_max -group_path_count "1" -format "full_clock_expanded" '
    '-fields [list "slew" "capacitance" "input_pins" "net" "fanout"] -unconstrained '
    '> "reports/timing.rpt"',
    'report_clock_skew > "reports/clock_skew.rpt"',
    'report_power > "reports/power.rpt"',
]


def test_the_report_analyzes_each_supply_net_then_reports_the_cells_and_the_metrics(tmp_path):
    assert report_body(tmp_path) == [
        'set_pdnsim_net_voltage -net "VDD" -voltage 1.1',
        'analyze_power_grid -net "VDD" -error_file "reports/irdrop_1.rpt"',
        'set_pdnsim_net_voltage -net "VSS" -voltage 0.0',
        'analyze_power_grid -net "VSS" -error_file "reports/irdrop_2.rpt"',
        *TEXT_REPORTS,
        "report_cell_usage",
        "xeda_metrics 1 1",
    ]


def test_the_analysis_of_the_ir_drop_can_be_switched_off(tmp_path):
    assert report_body(tmp_path, {"ir_drop": {"enabled": False}}) == [
        *TEXT_REPORTS,
        "report_cell_usage",
        "xeda_metrics 1 1",
    ]


def test_a_platform_that_gives_no_voltage_skips_the_analysis_and_says_so(tmp_path):
    assert report_body(tmp_path, supply=None) == [
        'puts "xeda: no supply net has a voltage; IR drop analysis skipped"',
        *TEXT_REPORTS,
        "report_cell_usage",
        "xeda_metrics 1 1",
    ]


def test_the_voltage_of_a_net_is_a_number_and_its_name_a_word(tmp_path):
    supply = {"power_nets": {"VDD [x]": 0.95}, "ground_nets": {}}
    found = report_body(tmp_path, supply=supply)
    assert found[0] == 'set_pdnsim_net_voltage -net "VDD \\[x\\]" -voltage 0.95'
    assert found[1] == 'analyze_power_grid -net "VDD \\[x\\]" -error_file "reports/irdrop_1.rpt"'


def test_the_report_names_do_not_come_from_the_names_of_the_nets(tmp_path):
    supply = {"power_nets": {"VDD/x": 1.0}, "ground_nets": {"a b": 0.0}}
    found = report_body(tmp_path, supply=supply)
    assert [line for line in found if "error_file" in line] == [
        'analyze_power_grid -net "VDD/x" -error_file "reports/irdrop_1.rpt"',
        'analyze_power_grid -net "a b" -error_file "reports/irdrop_2.rpt"',
    ]


def test_the_metrics_of_the_report_include_the_skew_and_the_electrical_rules(tmp_path):
    calls = run_final_script(final_flow(tmp_path))
    names = [call[0] for call in calls]
    assert "report_clock_skew_metric" in names and "report_erc_metrics" in names
    assert names.index("report_cell_usage") < names.index("report_tns_metric")


# ------------------------------------------------------------------------------ the failure


def test_a_failed_stage_of_the_second_process_writes_the_failed_database_and_stops(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "extract_parasitics")
    flow = final_flow(tmp_path)
    require_tclsh()
    flow.set_final_outputs()
    for name in ("route.odb", "route.sdc"):
        (flow.run_path / name).write_text("x\n")
    done = run_fake_openroad(flow.run_path, render_final(flow), driver="openroad_final")
    assert done.returncode != 0
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert "write_spef" not in called(flow.run_path)
    assert "report_cell_usage" not in called(flow.run_path)


def test_a_failed_report_leaves_the_failed_database(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "analyze_power_grid")
    flow = final_flow(tmp_path)
    require_tclsh()
    for name in ("route.odb", "route.sdc"):
        (flow.run_path / name).write_text("x\n")
    done = run_fake_openroad(flow.run_path, render_final(flow), driver="openroad_final")
    assert done.returncode != 0
    assert (flow.run_path / "openroad-failed.odb").is_file()
    assert "report_cell_usage" not in called(flow.run_path)


# ------------------------------------------------------------------------------ the processes


def run_both(
    tmp_path: Path, monkeypatch, settings: dict[str, Any] | None = None, **kwargs: Any
) -> Openroad:
    """Run both processes of the flow under the fake OpenROAD. The block of the design is
    `counter`, which names the file that the extraction writes."""
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    flow = final_flow(tmp_path, settings, **kwargs)
    in_run_directory(flow, monkeypatch)
    flow._run_processes()
    return flow


def test_the_processes_run_in_order_and_write_the_outputs_the_metrics_and_the_logs(
    tmp_path, monkeypatch
):
    flow = run_both(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    for name in (
        "openroad_route.tcl",
        "openroad_route.log",
        "route.odb",
        "route.sdc",
        "openroad_final.tcl",
        "openroad_final.log",
        "final.odb",
        "final.sdc",
        "final.def",
        "final.v",
        "final.spef",
    ):
        assert (run_dir / name).is_file(), name
    assert not (run_dir / "openroad-failed.odb").exists()
    for process, recorded in (("route", "route"), ("final", "final")):
        assert json.loads((run_dir / f"metrics_{process}.json").read_text()) == json.loads(
            (RECORDED / f"openroad_{recorded}.json").read_text()
        )
    order = called(run_dir)
    assert order.index("detailed_route") < order.index("filler_placement")
    assert order.index("filler_placement") < order.index("global_connect")
    assert order.index("write_db") < order.index("global_connect")  # the working pair
    assert flow.outputs.odb == run_dir / "final.odb"
    assert flow.outputs.netlist == run_dir / "final.v"
    assert flow.outputs.spef == run_dir / "final.spef"


def test_the_second_process_runs_the_script_of_its_driver(tmp_path, monkeypatch):
    flow = run_both(tmp_path, monkeypatch)
    assert (Path(flow.run_path) / "openroad_final.tcl").read_text() == render_final(flow)


def ready_files(flow: Openroad, monkeypatch) -> list[str]:
    """Run only the second process; return the files that it makes ready, relative to the run
    directory and sorted."""
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    for name in ("route.odb", "route.sdc"):
        (flow.run_path / name).write_text(f"{name}\n")
    flow.run_final()
    return sorted(ready)


def test_the_second_process_makes_every_file_it_writes_ready_first(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = final_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    assert ready_files(flow, monkeypatch) == sorted(
        [
            "metrics_final.json",
            "final.odb",
            "final.sdc",
            "final.def",
            "final.v",
            "final.sdf",
            "final.lef",
            "final.spef",
            "counter.totCap",
            "reports/irdrop_1.rpt",
            "reports/irdrop_2.rpt",
            "reports/timing.rpt",
            "reports/clock_skew.rpt",
            "reports/power.rpt",
            "openroad-failed.odb",
        ]
    )


@pytest.mark.parametrize(
    "settings, rules, supply, gone",
    [
        ({}, False, None, {"final.spef", "counter.totCap", "reports/irdrop_1.rpt"}),
        ({"ir_drop": {"enabled": False}}, True, SUPPLY, {"reports/irdrop_1.rpt"}),
        ({"extraction": {"method": "estimate"}}, True, SUPPLY, {"final.spef", "counter.totCap"}),
    ],
    ids=["estimate", "no analysis", "estimate chosen"],
)
def test_the_second_process_makes_ready_only_the_files_it_writes(
    tmp_path, monkeypatch, settings, rules, supply, gone
):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path, settings, rules=rules, supply=supply)
    in_run_directory(flow, monkeypatch)
    ready = ready_files(flow, monkeypatch)
    assert not gone & set(ready)
    assert {"final.odb", "final.v", "metrics_final.json", "openroad-failed.odb"} <= set(ready)


def test_the_file_of_the_extraction_is_removed_from_the_run_directory(tmp_path, monkeypatch):
    """OpenRCX writes `<top>.totCap` in the working directory. The flow removes it after the
    process, through the run directory, so no file is left that no output names. A flow that no
    launcher started deletes nothing, so the file of the fake is still there."""
    unlaunched = run_both(tmp_path / "unlaunched", monkeypatch)
    assert (unlaunched.run_path / "counter.totCap").is_file(), "the fake writes the file"
    launched = run_both(tmp_path / "launched", monkeypatch, launched=True)
    assert not os.path.lexists(launched.run_path / "counter.totCap")
    assert (launched.run_path / "final.spef").is_file()


def test_the_file_of_the_extraction_is_removed_when_the_process_fails_after_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "write_spef")
    with pytest.raises(NonZeroExitCode):
        run_both(tmp_path, monkeypatch, launched=True)
    assert not os.path.lexists(tmp_path / "xeda_run" / "counter" / "openroad" / "counter.totCap")


def test_a_link_at_the_name_of_the_file_of_the_extraction_is_removed_as_itself(
    tmp_path, monkeypatch
):
    """A tool may have left a link there. The process never writes through it, and the removal
    deletes the name and not what it leads to."""
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    flow = final_flow(tmp_path, launched=True)
    in_run_directory(flow, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("mine\n")
    (flow.run_path / "counter.totCap").symlink_to(outside)
    flow._run_processes()
    assert outside.read_text() == "mine\n"
    assert not os.path.lexists(flow.run_path / "counter.totCap")


def test_an_estimate_leaves_no_file_of_the_extraction_to_remove(tmp_path, monkeypatch):
    flow = run_both(tmp_path, monkeypatch, {"extraction": {"method": "estimate"}}, launched=True)
    assert not (flow.run_path / "final.spef").exists()
    assert not os.path.lexists(flow.run_path / "counter.totCap")


def test_the_second_process_does_not_run_when_the_first_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "detailed_route")
    with pytest.raises(NonZeroExitCode) as error:
        run_both(tmp_path, monkeypatch)
    assert "openroad_route.tcl" in str(error.value)
    assert not (tmp_path / "work" / "openroad_final.tcl").exists()


def test_the_failure_of_the_second_process_names_the_flow_the_script_and_the_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "write_def")
    with pytest.raises(NonZeroExitCode) as error:
        run_both(tmp_path, monkeypatch)
    message = str(error.value)
    assert "openroad" in message and "openroad_final.tcl" in message
    assert "write_def failed" in message
    assert (tmp_path / "work" / "openroad-failed.odb").is_file()
    assert (tmp_path / "work" / "route.odb").is_file()


def test_every_choice_of_the_second_process_renders_and_runs(tmp_path):
    """The extraction and the analysis, each way, run under the fake OpenROAD."""
    for number, (settings, rules, supply) in enumerate(
        [
            ({}, True, SUPPLY),
            ({}, False, SUPPLY),
            ({}, True, None),
            ({"extraction": {"method": "estimate"}}, True, SUPPLY),
            ({"extraction": {"method": "rcx"}, "ir_drop": {"enabled": False}}, True, SUPPLY),
            ({"hierarchical": True, "wire_rc": {"signal_layer": "m3"}}, True, SUPPLY),
        ]
    ):
        flow = route_flow(tmp_path / str(number), settings, rules=rules, supply=supply)
        names = [call[0] for call in run_final_script(flow)]
        assert names[-1] == "report_design_area_metrics"
        assert ("extract_parasitics" in names) is (
            rules and settings.get("extraction") != {"method": "estimate"}
        )


# ============================================================================== results and verdicts
#
# `parse_reports()` reads the two metrics files, and `check_results()` judges them. The recorded
# metrics are those of a real run of the design `gcd` on the platform `nangate45`: its worst setup
# slack is negative, so with the defaults the checks reject it.

REMOVE = object()


def metrics_text(name: str, **changes: Any) -> str:
    """The recorded metrics `name` (`route` or `final`) as JSON text, with `changes` applied: a key
    to its new value, or to `REMOVE` for no key."""
    data = json.loads((RECORDED / f"openroad_{name}.json").read_text())
    for key, value in changes.items():
        if value is REMOVE:
            del data[key]
        else:
            data[key] = value
    return json.dumps(data)


def parsed(
    tmp_path: Path,
    *,
    route: str | None = "recorded",
    final: str | None = "recorded",
    settings: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[Openroad, bool]:
    """Write the metrics files of the two processes (`recorded`, a text, or `None` for no file) in
    the run directory of the flow, and read them. Return the flow and what `parse_reports()` says.
    """
    flow = route_flow(tmp_path, settings, **kwargs)
    for process, text in (("route", route), ("final", final)):
        if text == "recorded":
            text = metrics_text(process)
        if text is not None:
            (Path(flow.run_path) / f"metrics_{process}.json").write_text(text)
    return flow, flow.parse_reports()


def judge(tmp_path: Path, settings: dict[str, Any] | None = None, **changes: Any):
    """Parse the recorded metrics with `changes` keyed by `route:<key>` or `final:<key>`, judge
    them, and return the flow and the verdict of `check_results()`."""
    final = {k.split(":", 1)[1]: v for k, v in changes.items() if k.startswith("final:")}
    route = {k.split(":", 1)[1]: v for k, v in changes.items() if k.startswith("route:")}
    flow, ok = parsed(
        tmp_path,
        route=metrics_text("route", **route),
        final=metrics_text("final", **final),
        settings=settings,
    )
    assert ok
    return flow, flow.check_results()


SETUP = "finish__timing__setup__ws"
HOLD = "finish__timing__hold__ws"
DRC = "detailedroute__route__drc_errors"
ANTENNA = "detailedroute__antenna__violating__nets"


# ------------------------------------------------------------------------------ the results


def test_the_results_are_the_recorded_values_of_the_real_run(tmp_path):
    flow, ok = parsed(tmp_path)
    assert ok
    results = flow.results
    expected = {
        "worst_slack": -0.244411,
        "wns": -0.244411,
        "tns": -10.9686,
        "whs": 0.0159159,
        "hold_tns": 0,
        "fmax": 2022.61,
        "setup_violations": 88,
        "hold_violations": 0,
        "max_slew_violations": 0,
        "max_cap_violations": 0,
        "max_fanout_violations": 0,
        "clock_skew_setup": 0.00138606,
        "clock_skew_hold": 0.00138606,
        "area": 773.528,
        "utilization": 0.887668,
        "core_area": 871.416,
        "die_area": 1019.52,
        "power": 0.00566387,
        "power_internal": 0.00339365,
        "power_switching": 0.00224974,
        "power_leakage": 2.04825e-05,
        "drc_errors": 0,
        "antenna_violating_nets": 0,
        "antenna_violating_pins": 0,
        "wirelength": 3599,
        "vias": 3073,
        "instances": 576,
        "fill_cells": 186,
        "tap_cells": 42,
        "warnings": 0,
        "errors": 0,
    }
    for name, value in expected.items():
        assert results[name] == pytest.approx(value), name
    assert results["parasitics"] == "estimate"  # the platform has no OpenRCX rules


def test_the_area_is_the_area_of_the_standard_cells_and_not_the_duplicated_total(tmp_path):
    """OpenROAD writes `finish__design__instance__area` twice, with the area of all the cells and
    then of the standard cells. The result is read from the key that is written once."""
    flow, _ = parsed(tmp_path)
    assert flow.results["area"] == 773.528
    assert flow.results["area"] != 871.416


def test_the_counts_are_whole_numbers(tmp_path):
    flow, _ = parsed(tmp_path)
    for name in (
        "setup_violations",
        "drc_errors",
        "antenna_violating_nets",
        "vias",
        "instances",
        "fill_cells",
        "tap_cells",
        "warnings",
        "errors",
    ):
        assert isinstance(flow.results[name], int), name


def test_the_results_name_the_extraction_and_the_platform(tmp_path):
    flow, _ = parsed(tmp_path, rules=True)
    assert flow.results["parasitics"] == "rcx"
    flow, _ = parsed(tmp_path / "estimate", settings={"extraction": {"method": "estimate"}})
    assert flow.results["parasitics"] == "estimate"
    assert flow.results["platform"]["name"] == "tiny"
    assert flow.results["platform"]["provider"] == "directory"
    assert set(flow.results["platform"]) == {"name", "provider", "version", "corner", "sources"}


def test_the_times_convert_from_the_liberty_unit_to_nanoseconds(tmp_path):
    flow, ok = parsed(tmp_path, model={"time_unit": "1ps"})
    assert ok
    assert flow.results["worst_slack"] == pytest.approx(-0.244411e-3)
    assert flow.results["whs"] == pytest.approx(0.0159159e-3)
    assert flow.results["tns"] == pytest.approx(-10.9686e-3)
    assert flow.results["clock_skew_setup"] == pytest.approx(0.00138606e-3)
    assert flow.results["fmax"] == pytest.approx(2022.61)  # a frequency has no Liberty unit


def test_the_wns_is_zero_when_the_slack_is_positive(tmp_path):
    flow, ok = parsed(tmp_path, final=metrics_text("final", **{SETUP: 0.3}))
    assert ok and flow.results["worst_slack"] == 0.3 and flow.results["wns"] == 0


def test_a_result_that_no_verdict_reads_and_that_is_absent_is_left_out(tmp_path):
    optional = {
        "finish__timing__fmax": "fmax",
        "finish__design__die__area": "die_area",
        "finish__design__core__area": "core_area",
        "finish__power__internal__total": "power_internal",
        "finish__timing__drv__max_slew": "max_slew_violations",
        "finish__design__instance__count__class:tap_cell": "tap_cells",
        "finish__flow__warnings__count": "warnings",
        "finish__clock__skew__setup": "clock_skew_setup",
        "finish__timing__setup__tns": "tns",
        "finish__timing__drv__setup_violation_count": "setup_violations",
    }
    flow, ok = parsed(tmp_path, final=metrics_text("final", **dict.fromkeys(optional, REMOVE)))
    assert ok
    for result in optional.values():
        assert result not in flow.results, result
    assert flow.results["worst_slack"] == -0.244411


def test_the_diodes_of_a_route_that_skipped_the_repair_are_left_out(tmp_path):
    """The recorded route skipped the repair: the diode of nangate45 is not usable."""
    flow, ok = parsed(tmp_path / "skipped")
    assert ok and "antenna_diodes" not in flow.results
    assert flow.results["antenna_violating_nets"] == 0
    repaired = metrics_text("route", detailedroute__antenna_diodes_count=3)
    flow, ok = parsed(tmp_path / "repaired", route=repaired)
    assert ok and flow.results["antenna_diodes"] == 3


def test_the_wirelength_and_the_vias_of_the_route_are_left_out_when_absent(tmp_path):
    flow, ok = parsed(
        tmp_path,
        route=metrics_text(
            "route", detailedroute__route__wirelength=REMOVE, detailedroute__route__vias=REMOVE
        ),
    )
    assert ok and "wirelength" not in flow.results and "vias" not in flow.results


@pytest.mark.parametrize(
    "key",
    [
        "finish__timing__fmax",
        "finish__design__die__area",
        "finish__power__total",
        "finish__timing__drv__max_cap",
        "finish__design__instance__count__stdcell",
        "finish__flow__errors__count",
    ],
)
@pytest.mark.parametrize("value", [None, "1", True, float("nan"), float("inf")])
def test_a_result_that_is_present_and_invalid_is_a_parse_failure(tmp_path, caplog, key, value):
    flow, ok = parsed(tmp_path, final=metrics_text("final", **{key: value}))
    assert not ok
    assert "metrics_final.json" in caplog.text and repr(key) in caplog.text
    assert repr(value) in caplog.text


@pytest.mark.parametrize("value", [1.5, -1, True])
def test_a_count_that_is_no_count_is_a_parse_failure(tmp_path, caplog, value):
    flow, ok = parsed(
        tmp_path, final=metrics_text("final", finish__design__instance__count__stdcell=value)
    )
    assert not ok and repr(value) in caplog.text


@pytest.mark.parametrize("which", ["route", "final"])
def test_a_metrics_file_that_is_missing_is_a_parse_failure(tmp_path, caplog, which):
    flow, ok = parsed(tmp_path, **{which: None})
    assert not ok
    assert f"metrics_{which}.json" in caplog.text
    assert "worst_slack" not in flow.results and "drc_errors" not in flow.results


@pytest.mark.parametrize("text", ["[1, 2]", "not json", "", "3"])
@pytest.mark.parametrize("which", ["route", "final"])
def test_a_metrics_file_that_is_no_json_object_is_a_parse_failure(tmp_path, which, text):
    flow, ok = parsed(tmp_path, **{which: text})
    assert not ok and "worst_slack" not in flow.results


# ------------------------------------------------------------------------------ the verdict values


@pytest.mark.parametrize("fail", [True, False], ids=["checks on", "checks off"])
@pytest.mark.parametrize(
    "key, file",
    [(SETUP, "final"), (HOLD, "final"), (DRC, "route"), (ANTENNA, "route")],
    ids=["setup", "hold", "drc", "antenna"],
)
def test_a_missing_verdict_value_is_a_parse_failure_with_the_checks_on_or_off(
    tmp_path, caplog, key, file, fail
):
    settings = dict.fromkeys(("fail_timing", "fail_drc", "fail_antenna"), fail)
    flow, ok = parsed(tmp_path, settings=settings, **{file: metrics_text(file, **{key: REMOVE})})
    assert not ok
    assert f"metrics_{file}.json" in caplog.text and repr(key) in caplog.text
    assert "missing" in caplog.text
    assert "drc_errors" not in flow.results or key != DRC  # never a zero for a missing value


@pytest.mark.parametrize("fail", [True, False], ids=["checks on", "checks off"])
@pytest.mark.parametrize(
    "value", [None, "0", True, float("nan"), float("inf"), float("-inf")], ids=repr
)
@pytest.mark.parametrize("key", [SETUP, HOLD])
def test_a_slack_that_is_no_finite_number_is_a_parse_failure(tmp_path, caplog, key, value, fail):
    settings = dict.fromkeys(("fail_timing", "fail_drc", "fail_antenna"), fail)
    flow, ok = parsed(tmp_path, settings=settings, final=metrics_text("final", **{key: value}))
    assert not ok
    assert "metrics_final.json" in caplog.text and repr(key) in caplog.text
    assert repr(value) in caplog.text


@pytest.mark.parametrize("fail", [True, False], ids=["checks on", "checks off"])
@pytest.mark.parametrize("value", [1.5, -1, True, None, "0"], ids=repr)
@pytest.mark.parametrize("key", [DRC, ANTENNA])
def test_a_count_of_a_verdict_that_is_no_count_is_a_parse_failure(
    tmp_path, caplog, key, value, fail
):
    settings = dict.fromkeys(("fail_timing", "fail_drc", "fail_antenna"), fail)
    flow, ok = parsed(tmp_path, settings=settings, route=metrics_text("route", **{key: value}))
    assert not ok
    assert "metrics_route.json" in caplog.text and repr(key) in caplog.text
    assert repr(value) in caplog.text


def test_each_verdict_value_is_read_from_the_file_of_the_process_that_writes_it(tmp_path):
    """The slack is a key of the second process and the counts are keys of the first. A key in
    the other file is not the value."""
    wrong_final = metrics_text("final", **{DRC: 9, ANTENNA: 9})
    wrong_route = metrics_text("route", **{SETUP: -9.0, HOLD: -9.0})
    flow, ok = parsed(tmp_path, route=wrong_route, final=wrong_final)
    assert ok
    assert flow.results["drc_errors"] == 0 and flow.results["worst_slack"] == -0.244411


# ------------------------------------------------------------------------------ the verdict


def test_a_run_that_meets_every_check_passes(tmp_path):
    flow, ok = judge(tmp_path, **{"final:" + SETUP: 0.2, "final:" + HOLD: 0.1})
    assert ok is True
    assert flow.rejection is None
    assert "error" not in flow.results or not flow.results["error"]


def test_a_negative_setup_slack_rejects_the_run_with_the_default_checks(tmp_path):
    flow, ok = judge(tmp_path)
    assert ok is False
    assert flow.rejection is not None
    assert "timing" in flow.rejection and "-0.244411" in flow.rejection
    assert flow.results["error"]["code"] == "rejected"
    assert flow.rejection in flow.results["error"]["message"]
    assert flow.results["worst_slack"] == -0.244411, "the numbers stay in the results"


def test_a_negative_hold_slack_rejects_the_run(tmp_path):
    flow, ok = judge(tmp_path, **{"final:" + SETUP: 0.2, "final:" + HOLD: -0.01})
    assert ok is False and "hold" in flow.rejection and "-0.01" in flow.rejection


def test_a_zero_slack_meets_timing(tmp_path):
    flow, ok = judge(tmp_path, **{"final:" + SETUP: 0.0, "final:" + HOLD: 0.0})
    assert ok is True and flow.rejection is None


def test_a_timing_miss_does_not_reject_the_run_when_the_check_is_off(tmp_path):
    flow, ok = judge(tmp_path, {"fail_timing": False})
    assert ok is True and flow.rejection is None
    assert flow.results["worst_slack"] == -0.244411 and flow.results["wns"] == -0.244411


def test_design_rule_violations_reject_the_run(tmp_path):
    flow, ok = judge(tmp_path, {"fail_timing": False}, **{"route:" + DRC: 3})
    assert ok is False
    assert "3" in flow.rejection and "design rule" in flow.rejection
    assert flow.results["drc_errors"] == 3


def test_design_rule_violations_do_not_reject_the_run_when_the_check_is_off(tmp_path):
    flow, ok = judge(tmp_path, {"fail_timing": False, "fail_drc": False}, **{"route:" + DRC: 3})
    assert ok is True and flow.rejection is None and flow.results["drc_errors"] == 3


def test_antenna_violations_reject_the_run(tmp_path):
    flow, ok = judge(tmp_path, {"fail_timing": False}, **{"route:" + ANTENNA: 2})
    assert ok is False
    assert "2" in flow.rejection and "antenna" in flow.rejection


def test_antenna_violations_do_not_reject_the_run_when_the_check_is_off(tmp_path):
    flow, ok = judge(
        tmp_path, {"fail_timing": False, "fail_antenna": False}, **{"route:" + ANTENNA: 2}
    )
    assert ok is True and flow.rejection is None
    assert flow.results["antenna_violating_nets"] == 2


def test_the_checks_are_independent(tmp_path):
    """Switching off one check leaves the others on."""
    changes = {"route:" + DRC: 4, "route:" + ANTENNA: 1}
    flow, ok = judge(tmp_path, {"fail_timing": False, "fail_drc": False}, **changes)
    assert ok is False and "antenna" in flow.rejection and "design rule" not in flow.rejection
    flow, ok = judge(tmp_path / "b", {"fail_timing": False, "fail_antenna": False}, **changes)
    assert ok is False and "design rule" in flow.rejection and "antenna" not in flow.rejection


def test_every_failed_judgment_is_named_in_the_one_rejection(tmp_path):
    """`reject()` keeps the first message in `results.json` and the last one in `Flow.rejection`.
    One call with every cause gives the same reason in the replay of the run as in the first
    launch."""
    flow, ok = judge(tmp_path, **{"route:" + DRC: 5, "route:" + ANTENNA: 2})
    assert ok is False
    for cause in ("-0.244411", "5", "design rule", "2", "antenna"):
        assert cause in flow.rejection, cause
    assert flow.results["error"]["message"] == f"`{flow.name}` reported failure: {flow.rejection}"


def test_the_verdict_is_made_from_the_final_slack_and_not_from_a_message_of_the_repair(tmp_path):
    """The repair of the timing ends with a warning that it could not repair every violation, and
    the flow goes on. The verdict reads the slack after all the stages."""
    ok_slack = {
        "final:" + SETUP: 0.5,
        "final:" + HOLD: 0.5,
        "final:finish__flow__warnings__count": 40,
    }
    flow, ok = judge(tmp_path, **ok_slack)
    assert ok is True and flow.rejection is None
    assert flow.results["warnings"] == 40


def test_the_verdict_reads_the_numbers_that_parse_reports_stored(tmp_path):
    flow, ok = parsed(tmp_path, final=metrics_text("final", **{SETUP: 0.5, HOLD: 0.5}))
    assert ok
    flow.results["worst_slack"] = -1.0  # what a check reads is the stored result
    assert flow.check_results() is False and "-1" in flow.rejection


# ------------------------------------------------------------------------------ planning, extraction


@pytest.mark.parametrize("method", ["auto", "estimate"])
def test_the_methods_that_need_no_rules_plan_on_a_platform_without_rules(tmp_path, method):
    with register_openroad_flows():
        planned = plan(tmp_path, f"extraction.method={method}")
    assert planned.nodes[-1].name == "openroad"


@pytest.mark.parametrize("method", ["auto", "rcx", "estimate"])
def test_every_method_plans_on_a_platform_with_rules(tmp_path, method):
    store = tiny_store(tmp_path)
    (store / "tiny" / RCX_RULES).write_text("rules\n")
    residue = variant_residue(tmp_path, rules=True, supply=SUPPLY)
    given = dict(
        text.split("=", 1)
        for text in [
            *openroad_settings(store, platform_file=str(residue)),
            f"extraction.method={method}",
        ]
    )
    with register_openroad_flows():
        planned = DefaultRunner(tmp_path / "run", display_results=False).plan(
            "openroad", openroad_design(tmp_path / "design"), flow_settings=given
        )
    assert planned.nodes[-1].name == "openroad"


def test_the_refusal_of_rcx_comes_from_the_class_without_a_platform_model(tmp_path):
    """Planning reads the residue file only: it builds no platform model and starts no tool."""
    store = tiny_store(tmp_path)
    settings = Openroad.Settings.from_input(
        {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), "extraction": {"method": "rcx"}},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    with pytest.raises(FlowSettingsException, match="extraction.method"):
        Openroad.check_settings_supported(settings)


# ============================================================================== the images
#
# The third process runs only with `save_images`. It restarts from the final database, loads the
# parasitics of the final report, and saves the images in the graphical interface.

GOLDEN_IMAGES = Path(__file__).parent / "resources" / "openroad" / "images_defaults.tcl"
IMAGES = [
    "images/final_all.webp",
    "images/final_routing.webp",
    "images/final_placement.webp",
    "images/final_clocks.webp",
    "images/final_resizer.webp",
    "images/final_congestion.webp",
    "images/final_worst_path.webp",
]


def images_flow(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> Openroad:
    return final_flow(tmp_path, {"save_images": True, **(settings or {})}, **kwargs)


def render_images(flow: Openroad) -> str:
    flow.set_final_outputs()
    return render_driver(flow, "openroad_images")


def run_images_script(flow: Openroad) -> list[list[str]]:
    """Run the image driver under the fake OpenROAD; the files it reads exist."""
    require_tclsh()
    text = render_images(flow)
    run_dir = Path(flow.run_path)
    for name in ("final.odb", "final.sdc", "final.spef"):
        (run_dir / name).write_text(f"{name}\n")
    (run_dir / "images").mkdir(exist_ok=True)
    done = run_fake_openroad(run_dir, text, driver="openroad_images")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(run_dir)


def test_the_image_driver_renders_the_golden_script(tmp_path):
    script = tokens(tmp_path, render_images(images_flow(tmp_path)))
    assert script == GOLDEN_IMAGES.read_text()


def test_the_image_driver_stops_first_when_this_openroad_has_no_gui(tmp_path):
    """The check comes before anything is read, so a build without the interface fails at once."""
    found = lines(render_images(images_flow(tmp_path)))
    assert found[:3] == [
        "if {![ord::openroad_gui_compiled]} {",
        'error "this OpenROAD has no GUI; set save_images: false"',
        "}",
    ]
    assert found[3].startswith("read_liberty ")


def test_the_image_driver_reads_the_final_database_and_the_parasitics_of_the_report(tmp_path):
    found = [tokens(tmp_path, line) for line in lines(render_images(images_flow(tmp_path)))]
    assert 'read_db "@RUN@/final.odb"' in found
    assert 'read_sdc "@RUN@/final.sdc"' in found
    assert 'read_spef "@RUN@/final.spef"' in found
    assert "estimate_parasitics -global_routing" not in found
    estimated = lines(render_images(images_flow(tmp_path / "e", rules=False)))
    assert "estimate_parasitics -global_routing" in estimated
    assert not any(line.startswith("read_spef") for line in estimated)


def test_the_images_are_saved_in_the_interface_at_fixed_names(tmp_path):
    calls = run_images_script(images_flow(tmp_path))
    assert [call for call in calls if call[0] == "gui::show"] == [
        ["gui::show", "xeda_images", "false"]
    ]
    saved = [call[-1] for call in calls if call[0] == "save_image"]
    assert saved == IMAGES
    order = [call[0] for call in calls]
    assert order.index("gui::show") < order.index("save_image")
    assert (
        order.count("gui::save_display_controls")
        == order.count("gui::restore_display_controls")
        == 1
    )


def test_an_image_name_is_fixed_and_no_name_of_the_design_is_in_it(tmp_path):
    assert list(Openroad.image_files) == IMAGES
    for name in IMAGES:
        assert re.fullmatch(r"images/[a-z_]+\.webp", name)


def test_the_image_process_runs_with_the_offscreen_platform_and_the_others_do_not(
    tmp_path, monkeypatch
):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    flow = images_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    started: list[tuple[str, Any]] = []
    execute = Tool.execute

    def record(self: Tool, executable: str, *args: Any, **kwargs: Any) -> Any:
        if executable == "openroad" and str(args[-1]).endswith(".tcl"):
            started.append((Path(args[-1]).name, kwargs.get("env")))
        return execute(self, executable, *args, **kwargs)

    monkeypatch.setattr(Tool, "execute", record)
    flow.run()
    assert started == [
        ("openroad_route.tcl", None),
        ("openroad_final.tcl", None),
        ("openroad_images.tcl", {"QT_QPA_PLATFORM": "offscreen"}),
    ]
    run_dir = Path(flow.run_path)
    assert (run_dir / "env.sh").read_text() == 'export QT_QPA_PLATFORM="offscreen"'
    for name in IMAGES:
        assert (run_dir / name).is_file(), name
    assert (run_dir / "openroad_images.log").is_file()
    assert not (run_dir / "openroad-failed.odb").exists()


def test_the_image_process_makes_every_file_it_writes_ready_first(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = images_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    run_dir = Path(flow.run_path)
    flow.set_final_outputs()
    for name in ("final.odb", "final.sdc", "final.spef"):
        (run_dir / name).write_text(f"{name}\n")
    flow.run_images()
    assert sorted(ready) == sorted(["metrics_images.json", *IMAGES])
    assert (run_dir / "images").is_dir()


def test_a_run_without_save_images_starts_no_third_process(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    flow = final_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    flow.run()
    run_dir = Path(flow.run_path)
    assert (run_dir / "final.odb").is_file()
    assert not (run_dir / "openroad_images.tcl").exists()
    assert not (run_dir / "images").exists()
    assert not (run_dir / "env.sh").exists()


def test_the_image_commands_are_in_the_command_list():
    from .tool_utils import OPENROAD_COMMANDS

    for command in ("gui::show", "save_image", "ord::openroad_gui_compiled", "select"):
        assert command in OPENROAD_COMMANDS


# ============================================================================== the GDS merge
#
# With `write_gds`, KLayout merges the routed design with the GDSII files of the cells into
# `final.gds`. A fake `klayout` records its command line.

FAKE_KLAYOUT = """#!/bin/sh
case "$1" in -v|-V) echo "KLayout 0.30.12"; exit 0;; esac
printf '%s\\0' "$@" > klayout.args
out=
while [ "$#" -gt 0 ]; do
  case "$1" in out_file=*) out="${1#out_file=}";; esac
  shift
done
if [ -n "$KLAYOUT_FAKE_ERROR" ]; then echo "$KLAYOUT_FAKE_ERROR"; exit 1; fi
if [ -n "$out" ]; then echo gds > "$out"; fi
"""


def fake_klayout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake `klayout` first on `PATH`. It records its arguments in `klayout.args` of the
    working directory and writes the file that `out_file` names. When `KLAYOUT_FAKE_ERROR` is set,
    it prints that text and fails."""
    binary = tmp_path / "klayout_bin"
    binary.mkdir(parents=True)
    script = binary / "klayout"
    script.write_text(FAKE_KLAYOUT)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))


def gds_flow(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> Openroad:
    return final_flow(tmp_path, {"write_gds": True, **(settings or {})}, layout=True, **kwargs)


def run_gds(tmp_path: Path, monkeypatch, settings: dict[str, Any] | None = None, **kwargs: Any):
    """Run the merge of a flow whose processes have run: the DEF file exists."""
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path, settings, **kwargs)
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    Path(flow.run_path, "final.def").write_text("VERSION 5.8 ;\n")
    flow.run_gds()
    return flow


def klayout_arguments(flow: Openroad) -> list[str]:
    return (Path(flow.run_path) / "klayout.args").read_text().split("\0")[:-1]


def test_the_merge_runs_klayout_with_the_names_that_the_script_reads(tmp_path, monkeypatch):
    flow = run_gds(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    gds = flow.platform_model.files["gds"][0]
    script = Path(final.__file__).parent / "data" / "def2stream.py"
    assert klayout_arguments(flow) == [
        "-zz",
        "-rd",
        "design_name=counter",
        "-rd",
        f"in_def={run_dir}/final.def",
        "-rd",
        f"in_files={gds}",
        "-rd",
        "seal_file=",
        "-rd",
        f"out_file={run_dir}/final.gds",
        "-rd",
        f"tech_file={run_dir}/klayout.lyt",
        "-rd",
        "layer_map=",
        "-rd",
        "allow_empty=",
        "-rd",
        "conflict_resolution=",
        "-rd",
        "read_lef_with_def=",
        "-rd",
        "net_properties=",
        "-rd",
        "instance_properties=",
        "-rd",
        "pin_properties=",
        "-rd",
        "write_timestamps=false",
        "-r",
        str(script),
    ]
    assert (run_dir / "final.gds").is_file()
    assert flow.outputs.gds == run_dir / "final.gds"
    assert script.is_file()


def test_the_technology_file_lists_the_lef_files_of_the_platform_as_absolute_paths(
    tmp_path, monkeypatch
):
    flow = run_gds(tmp_path, monkeypatch)
    text = (Path(flow.run_path) / "klayout.lyt").read_text()
    values = flow.effective()
    expected = "".join(
        f"<lef-files>{lef}</lef-files>" for lef in (values.tech_lef, *values.cell_lefs)
    )
    assert expected in text
    assert "./tiny.lef" not in text
    assert text.count("<lef-files>") == 1 + len(values.cell_lefs)
    assert all(Path(lef).is_absolute() for lef in (values.tech_lef, *values.cell_lefs))
    # the rest of the file is the file of the platform
    assert text.replace(expected, "<lef-files>./tiny.lef</lef-files>") == KLAYOUT_TECH_TEXT


def test_the_technology_file_escapes_the_lef_paths_as_xml_text(tmp_path, monkeypatch):
    """A path with `&` or `<` is valid on disk; the element of the technology file holds it as
    escaped text, and an XML parser reads back the path itself."""
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path)
    odd = tmp_path / "cells & <macros>" / "cells.lef"
    computed = flow.effective

    def effective_with_an_odd_path() -> Any:
        return dataclasses.replace(computed(), cell_lefs=(odd,))

    monkeypatch.setattr(flow, "effective", effective_with_an_odd_path)
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    path = flow.write_klayout_tech()
    lefs = [element.text for element in ElementTree.parse(path).iter("lef-files")]
    assert lefs[-1] == str(odd)
    assert "&amp; &lt;macros&gt;" in path.read_text()


def test_a_technology_file_with_a_lefdef_section_and_no_list_gets_the_list(tmp_path, monkeypatch):
    """The technology file of lambdapdk has a `<lefdef>` section and no `<lef-files>`."""
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path)
    flow.platform_model.files["klayout_tech"][0].write_text(
        "<technology>\n <lefdef>\n  <dbu>0.001</dbu>\n </lefdef>\n <other/>\n</technology>\n"
    )
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    path = flow.write_klayout_tech()
    values = flow.effective()
    listed = [element.text for element in ElementTree.parse(path).iter("lef-files")]
    assert listed == [str(lef) for lef in (values.tech_lef, *values.cell_lefs)]
    text = path.read_text()
    assert text.index("<dbu>") < text.index("<lef-files>") < text.index("</lefdef>")
    assert "<other/>" in text


LEFS = [Path("/a/tech.lef"), Path("/b/cells & <x>.lef")]
LISTED = "<lef-files>/a/tech.lef</lef-files><lef-files>/b/cells &amp; &lt;x&gt;.lef</lef-files>"


@pytest.mark.parametrize(
    "text, expected",
    [
        # one element, kept in its place with its indentation and line end
        (
            "<t>\n <lefdef>\n  <lef-files>old.lef</lef-files>\n  <dbu>1</dbu>\n </lefdef>\n</t>\n",
            f"<t>\n <lefdef>\n  {LISTED}\n  <dbu>1</dbu>\n </lefdef>\n</t>\n",
        ),
        # several elements: the list once, where the first was, and no entry from the input
        (
            "<lefdef>\n <a/>\n <lef-files>x.lef</lef-files>\n <b/>\n"
            " <lef-files>y.lef</lef-files>\n <lef-files>z.lef</lef-files>\n <c/>\n</lefdef>\n",
            f"<lefdef>\n <a/>\n {LISTED}\n <b/>\n <c/>\n</lefdef>\n",
        ),
        # empty elements, alone and among others
        (
            "<lefdef>\n <lef-files/>\n <b/>\n <lef-files />\n <lef-files\n />\n"
            " <lef-files>x.lef</lef-files>\n</lefdef>\n",
            f"<lefdef>\n {LISTED}\n <b/>\n</lefdef>\n",
        ),
        ("<lefdef><lef-files/></lefdef>", f"<lefdef>{LISTED}</lefdef>"),
        # elements over several lines
        (
            "<lefdef>\n <lef-files>\n  x.lef\n </lef-files>\n <b/>\n"
            " <lef-files>\n  y.lef\n  z.lef</lef-files>\n</lefdef>\n",
            f"<lefdef>\n {LISTED}\n <b/>\n</lefdef>\n",
        ),
        # no element: the list at the end of the section
        (
            "<lefdef>\n <dbu>1</dbu>\n</lefdef>\n<other/>\n",
            f"<lefdef>\n <dbu>1</dbu>\n{LISTED}</lefdef>\n<other/>\n",
        ),
        # an element on the same line as other text, and without a final newline
        ("<lefdef><lef-files>x</lef-files></lefdef>", f"<lefdef>{LISTED}</lefdef>"),
    ],
)
def test_the_list_of_lef_files_replaces_every_element_with_one_list(text, expected):
    from xeda.flows.openroad.final import klayout_tech_with_lef_files

    assert klayout_tech_with_lef_files(text, LEFS) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        # a commented example before the real list
        (
            "<t>\n <!-- e.g. <lef-files>example.lef</lef-files> -->\n <lefdef>\n"
            "  <lef-files>actual.lef</lef-files>\n </lefdef>\n</t>\n",
            f"<t>\n <!-- e.g. <lef-files>example.lef</lef-files> -->\n <lefdef>\n"
            f"  {LISTED}\n </lefdef>\n</t>\n",
        ),
        # a CDATA section that holds the text of an element
        (
            "<t><![CDATA[<lef-files>x</lef-files>]]><lefdef><lef-files>y</lef-files></lefdef></t>",
            f"<t><![CDATA[<lef-files>x</lef-files>]]><lefdef>{LISTED}</lefdef></t>",
        ),
        # a comment that opens a fragment before the real element, and a comment after a gap
        (
            "<lefdef><!-- <lef-files> -->  <lef-files>y</lef-files></lefdef>",
            f"<lefdef><!-- <lef-files> -->  {LISTED}</lefdef>",
        ),
        # a commented closing tag before the real one of a section without a list
        (
            "<lefdef>\n <!-- </lefdef> -->\n</lefdef>\n",
            f"<lefdef>\n <!-- </lefdef> -->\n{LISTED}</lefdef>\n",
        ),
    ],
)
def test_a_comment_or_a_cdata_section_is_no_element_and_stays_as_it_is(text, expected):
    from xeda.flows.openroad.final import klayout_tech_with_lef_files

    assert klayout_tech_with_lef_files(text, LEFS) == expected


def test_a_text_with_neither_an_element_nor_a_section_gets_no_list():
    from xeda.flows.openroad.final import klayout_tech_with_lef_files

    assert klayout_tech_with_lef_files("<technology/>\n", LEFS) is None


def test_a_technology_file_with_several_elements_gets_the_list_once(tmp_path, monkeypatch):
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path)
    flow.platform_model.files["klayout_tech"][0].write_text(
        "<technology>\n <lefdef>\n  <lef-files>a.lef</lef-files>\n"
        "  <lef-files>\n   b.lef\n  </lef-files>\n </lefdef>\n</technology>\n"
    )
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    values = flow.effective()
    listed = [e.text for e in ElementTree.parse(flow.write_klayout_tech()).iter("lef-files")]
    assert listed == [str(lef) for lef in (values.tech_lef, *values.cell_lefs)]


def test_a_technology_file_without_the_list_of_lef_files_is_refused_with_the_way_out(
    tmp_path, monkeypatch
):
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path)
    flow.platform_model.files["klayout_tech"][0].write_text("<technology/>\n")
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    with pytest.raises(FlowSettingsException, match="no `<lef-files>` element"):
        flow.run_gds()
    assert not (Path(flow.run_path) / "klayout.args").exists()


def test_a_seal_ring_goes_to_the_merge(tmp_path, monkeypatch):
    ring = tmp_path / "ring.gds"
    ring.write_bytes(b"ring\n")
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"seal_ring": str(ring)}})
    assert f"seal_file={ring}" in klayout_arguments(flow)


def test_the_merge_makes_every_file_it_writes_ready_first(tmp_path, monkeypatch):
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    flow.set_final_outputs()
    Path(flow.run_path, "final.def").write_text("VERSION 5.8 ;\n")
    flow.run_gds()
    assert "final.gds" in ready


def test_a_link_at_the_name_of_the_layout_is_replaced_and_not_followed(tmp_path, monkeypatch):
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path, launched=True)
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    run_dir = Path(flow.run_path)
    (run_dir / "final.def").write_text("VERSION 5.8 ;\n")
    victim = tmp_path / "victim.gds"
    victim.write_text("keep\n")
    (run_dir / "final.gds").symlink_to(victim)
    flow.run_gds()
    assert victim.read_text() == "keep\n"
    assert (run_dir / "final.gds").is_file() and not (run_dir / "final.gds").is_symlink()


def test_a_failed_merge_reports_the_errors_of_the_script(tmp_path, monkeypatch):
    monkeypatch.setenv("KLAYOUT_FAKE_ERROR", "[ERROR] LEF Cell 'X' has no matching GDS/OAS cell.")
    with pytest.raises(NonZeroExitCode) as raised:
        run_gds(tmp_path, monkeypatch)
    message = str(raised.value)
    assert "KLayout stopped while it merged the layout; see klayout.log" in message
    assert "[ERROR] LEF Cell 'X' has no matching GDS/OAS cell." in message


def test_a_missing_klayout_is_a_tool_error(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    flow = gds_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    flow.set_final_outputs()
    Path(flow.run_path, "final.def").write_text("VERSION 5.8 ;\n")
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ExecutableNotFound):
        flow.run_gds()


def test_the_run_merges_the_layout_after_the_images(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    fake_klayout(tmp_path, monkeypatch)
    flow = gds_flow(tmp_path, {"save_images": True})
    in_run_directory(flow, monkeypatch)
    flow.run()
    run_dir = Path(flow.run_path)
    assert (run_dir / "images" / "final_all.webp").is_file()
    assert (run_dir / "final.gds").is_file()
    assert (run_dir / "openroad_images.log").stat().st_mtime_ns <= (
        run_dir / "klayout.log"
    ).stat().st_mtime_ns


def test_a_run_without_write_gds_starts_no_klayout(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    fake_klayout(tmp_path, monkeypatch)
    flow = final_flow(tmp_path, layout=True)
    in_run_directory(flow, monkeypatch)
    flow.run()
    run_dir = Path(flow.run_path)
    assert flow.outputs.gds is None
    assert not (run_dir / "klayout.args").exists() and not (run_dir / "klayout.lyt").exists()


# ------------------------------------------------------------------------------ the files


@pytest.mark.parametrize("write_gds", [False, True])
@pytest.mark.parametrize("rules", [False, True])
def test_the_files_the_flow_opens_follow_the_extraction_and_write_gds(tmp_path, write_gds, rules):
    """The dry run, which builds no platform model, finds the same files as the run."""
    flow = route_flow(tmp_path, {"write_gds": write_gds}, layout=True, rules=rules)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs
    names = [path.name for path in flow.implicit_inputs]
    assert (GDS_FILE in names and KLAYOUT_TECH in names) is write_gds
    assert (RCX_RULES in names) is rules


def test_the_roles_are_a_property_of_the_flow_and_not_a_class_attribute():
    assert isinstance(OpenroadStage.__dict__["platform_roles"], property)
    assert isinstance(Openroad.__dict__["platform_roles"], property)
    assert OpenroadStage.platform_roles.fget is not Openroad.platform_roles.fget


def test_the_assets_of_the_merge_are_package_resources():
    from importlib import resources

    for name in ("def2stream.py", "NOTICE.md"):
        assert resources.files("xeda.flows.openroad").joinpath("data", name).is_file(), name


# ------------------------------------------------------------------------------ the planning


def plan_gds(tmp_path: Path, *texts: str, layout: bool = True):
    """Plan `openroad` on the platform with the layout files (without them with `layout=False`)."""
    store = tmp_path / "store"
    if not store.exists():
        tiny_store(tmp_path)
        (store / "tiny" / GDS_FILE).write_bytes(b"gds\n")
        (store / "tiny" / KLAYOUT_TECH).write_text(KLAYOUT_TECH_TEXT)
    residue = variant_residue(tmp_path, rules=False, supply=None, layout=True)
    settings = {"platform_file": str(residue)}
    given = dict(
        text.split("=", 1)
        for text in [*openroad_settings(store, **(settings if layout else {})), *texts]
    )
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        "openroad", openroad_design(tmp_path / "design"), flow_settings=given
    )


def test_a_setting_of_the_layout_file_without_write_gds_is_refused_with_the_way_out(tmp_path):
    ring = tmp_path / "ring.gds"
    ring.write_bytes(b"ring\n")
    with pytest.raises(FlowSettingsException) as raised:
        plan_gds(tmp_path, f"gds.seal_ring={ring}")
    message = " ".join(str(raised.value).split())
    assert (
        "`gds.seal_ring` has no effect with `write_gds: false`: set `write_gds: true` or delete "
        "`gds.seal_ring`" in message
    )


def test_a_setting_of_the_layout_file_with_write_gds_plans(tmp_path):
    ring = tmp_path / "ring.gds"
    ring.write_bytes(b"ring\n")
    planned = plan_gds(tmp_path, "write_gds=true", f"gds.seal_ring={ring}")
    assert planned.nodes[-1].name == "openroad"


def test_write_gds_on_a_platform_without_the_layout_files_is_refused_with_the_way_out(tmp_path):
    with pytest.raises(FlowSettingsException) as raised:
        plan_gds(tmp_path, "write_gds=true", layout=False)
    message = " ".join(str(raised.value).split())
    assert (
        "the platform 'tiny' gives no klayout_tech and no gds, which `write_gds` needs to merge "
        "the layout: add them to `files` in its residue file (`platform_file`), give "
        "`gds.cell_libraries` for the GDSII files, or set `write_gds: false`" in message
    )


def test_write_gds_changes_the_identity_of_the_last_flow_only(tmp_path):
    off = plan_gds(tmp_path)
    on = plan_gds(tmp_path, "write_gds=true")
    assert [node.name for node in on.nodes] == [node.name for node in off.nodes]
    same = [a.flowrun_hash == b.flowrun_hash for a, b in zip(off.nodes, on.nodes)]
    assert same == [True, True, True, False]


# ------------------------------------------------------------------------------ the assets


ASSETS = Path(final.__file__).parent / "data"
ORFS_COMMIT = "6b305a712966be72d8908695c04398acdbe273ca"


def test_the_merge_script_names_its_source_in_the_first_line():
    first = (ASSETS / "def2stream.py").read_text().splitlines()[0]
    assert first == (
        "# Source: https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/"
        f"{ORFS_COMMIT}/flow/util/def2stream.py; license: BSD-3-Clause (see NOTICE.md)"
    )


def test_the_notice_holds_the_license_and_names_the_file_and_the_changes():
    notice = (ASSETS / "NOTICE.md").read_text()
    assert "def2stream.py" in notice
    assert ORFS_COMMIT in notice
    assert "BSD 3-Clause" in notice
    assert "Copyright (c) 2018-2023, The Regents of the University of California" in notice
    assert "Redistribution and use in source and binary forms, with or without" in notice
    assert "THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS" in notice
    assert "OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE." in notice


def test_the_merge_script_reads_no_environment_variable():
    """Every input of the merge is a name that KLayout defines on its command line."""
    text = (ASSETS / "def2stream.py").read_text()
    assert "environ" not in text and "getenv" not in text


def test_the_packaging_selects_the_assets_of_the_merge():
    import tomllib

    repo = Path(__file__).parent.parent
    setuptools = tomllib.loads((repo / "pyproject.toml").read_text())["tool"]["setuptools"]
    include = setuptools["package-data"]["xeda.flows.openroad"]
    from fnmatch import fnmatchcase

    for relative in ("data/def2stream.py", "data/NOTICE.md"):
        assert any(fnmatchcase(relative, pattern) for pattern in include), relative


# ============================================================================== the detailed route
#
# Every option of the router that Xeda offers is a flag of `detailed_route`, or a command that
# runs before it. The antenna loop gives each route the same flags.


def route_args(tmp_path: Path, settings: dict[str, Any] | None = None) -> str:
    """The line `set route_args [list ...]` of the stage."""
    (line,) = [
        line
        for line in route_body(tmp_path, {"detailed_route": settings or {}})
        if line.startswith("set route_args")
    ]
    return line


@pytest.mark.parametrize(
    "option, flag",
    [
        ({"via_access_layer": "m2"}, '-via_access_layer "m2"'),
        ({"min_access_points": 2}, '-min_access_points "2"'),
        ({"pin_access": False}, "-no_pin_access"),
        ({"via_generation": False}, "-disable_via_gen"),
        ({"repair_pdn_vias_layer": "m1"}, '-repair_pdn_vias "m1"'),
        ({"clean_patches": True}, "-clean_patches"),
        ({"save_guide_updates": True}, "-save_guide_updates"),
        ({"verbose": 0}, '-verbose "0"'),
        ({"reroute_seed": 1, "reroute_swap_factor": 1.5}, '-or_k "1.5"'),
    ],
    ids=lambda value: json.dumps(value, sort_keys=True) if isinstance(value, dict) else value,
)
def test_each_option_of_the_router_prints_its_flag(tmp_path, option, flag):
    assert flag in route_args(tmp_path, option)
    assert flag not in route_args(tmp_path / "default")


def test_the_router_defaults_print_no_flag_beyond_those_of_openroad_flow_scripts(tmp_path):
    line = route_args(tmp_path)
    for flag in (
        "-via_access_layer",
        "-min_access_points",
        "-no_pin_access",
        "-disable_via_gen",
        "-repair_pdn_vias",
        "-clean_patches",
        "-save_guide_updates",
        "-or_seed",
        "-or_k",
        "-via_in_pin",
    ):
        assert flag not in line, flag


def test_the_swap_factor_is_a_number_because_the_router_reads_a_double(tmp_path):
    assert '-or_k "0.5"' in route_args(tmp_path, {"reroute_seed": 1, "reroute_swap_factor": 0.5})
    settings = Openroad.Settings.from_input(
        {"detailed_route": {"reroute_seed": 1, "reroute_swap_factor": 2}}, design_root=tmp_path
    )
    assert settings.detailed_route.reroute_swap_factor == 2.0
    assert isinstance(settings.detailed_route.reroute_swap_factor, float)


def test_the_guide_coverage_goes_to_a_report_of_a_fixed_name(tmp_path):
    assert '-output_guide_coverage "reports/guide_coverage.csv"' in route_args(tmp_path)


def test_the_flags_the_settings_cover_are_no_flags_of_the_hatch(tmp_path):
    for flag in (
        "-via_access_layer",
        "-min_access_points",
        "-save_guide_updates",
        "-clean_patches",
    ):
        message = refuse_settings(tmp_path, {"detailed_route": {"extra_args": [flag, "1"]}})
        assert f"`{flag}` in `detailed_route.extra_args` is the setting" in message


def test_the_hatch_takes_the_process_node(tmp_path):
    assert '"-db_process_node" "GF14"' in route_args(
        tmp_path, {"extra_args": ["-db_process_node", "GF14"]}
    )
    message = refuse_settings(
        tmp_path, {"detailed_route": {"extra_args": ["-output_maze", "x.log"]}}
    )
    assert "`-output_maze` in `detailed_route.extra_args` is not allowed" in message


def refuse_settings(tmp_path: Path, settings: dict[str, Any]) -> str:
    """The message of the refusal of `settings` (nested settings) by the planning of `openroad`."""
    store = tmp_path / "refusal_store"
    if not store.exists():
        tiny_store(tmp_path, "refusal_store")
    given = {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), **settings}
    with pytest.raises(FlowSettingsException) as raised:
        Openroad.check_settings_supported(
            Openroad.Settings.from_input(given, design_root=tmp_path, runner_cwd=tmp_path)
        )
    message = " ".join(str(raised.value).split())
    assert "needs a unit" not in message, "the refusal is about a missing unit: " + message
    return message


def test_the_default_vias_and_the_one_way_layers_are_set_before_the_route_one_command_each(
    tmp_path,
):
    found = route_body(
        tmp_path,
        {
            "detailed_route": {
                "default_vias": ["via1_4", "via2_0"],
                "unidirectional_layers": ["m1"],
            }
        },
    )
    assert found[found.index("set_propagated_clock [all_clocks]") + 1 :][:4] == [
        'detailed_route_set_default_via "via1_4"',
        'detailed_route_set_default_via "via2_0"',
        'detailed_route_set_unidirectional_layer "m1"',
        found[found.index('detailed_route_set_unidirectional_layer "m1"') + 1],
    ]
    assert found[found.index('detailed_route_set_unidirectional_layer "m1"') + 1].startswith(
        "set route_args"
    )


def test_a_via_name_with_a_dollar_reaches_the_command_as_text(tmp_path):
    found = route_body(tmp_path, {"detailed_route": {"default_vias": ["v$x [1]"]}})
    assert 'detailed_route_set_default_via "v\\$x \\[1\\]"' in found


def test_the_router_without_the_check_of_the_route_does_not_fail_on_unrouted_nets(tmp_path):
    found = route_body(tmp_path, {"detailed_route": {"require_routed": False}})
    assert "design_is_routed" not in " ".join(found)
    assert "report_design_area" in found
    assert any(line.startswith("check_antennas") for line in found)
    assert "design_is_routed" in " ".join(route_body(tmp_path / "default"))


def test_the_snapshots_set_the_debug_level_before_the_route_and_name_their_databases(
    tmp_path, monkeypatch
):
    found = route_body(tmp_path, {"detailed_route": {"snapshots": True}})
    assert found.index("set_debug_level DRT snapshot 1") < found.index(
        "detailed_route {*}$route_args"
    )
    assert "set_debug_level" not in " ".join(route_body(tmp_path / "default"))
    assert final.snapshot_files(Openroad.Settings().detailed_route) == []
    route = Openroad.Settings(detailed_route={"snapshots": True, "end_iteration": 3}).detailed_route
    assert final.snapshot_files(route) == [f"drt_iter{n}.odb" for n in range(4)]


def test_the_snapshots_are_files_the_process_makes_ready_first(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path, {"detailed_route": {"snapshots": True, "end_iteration": 2}})
    in_run_directory(flow, monkeypatch)
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    flow.run_route()
    assert {"drt_iter0.odb", "drt_iter1.odb", "drt_iter2.odb"} <= set(ready)
    assert "drt_iter3.odb" not in ready


def test_a_link_at_the_name_of_a_snapshot_is_replaced_and_not_followed(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(
        tmp_path, {"detailed_route": {"snapshots": True, "end_iteration": 1}}, launched=True
    )
    in_run_directory(flow, monkeypatch)
    victim = tmp_path / "victim.odb"
    victim.write_text("keep\n")
    (Path(flow.run_path) / "drt_iter1.odb").symlink_to(victim)
    flow.run_route()
    assert victim.read_text() == "keep\n"
    assert not (Path(flow.run_path) / "drt_iter1.odb").is_symlink()


# ------------------------------------------------------------------------------ the non-default rules


def ndr_lines(tmp_path: Path, ndr: dict[str, Any]) -> list[str]:
    found = route_body(tmp_path, {"ndr": ndr})
    return [line for line in found if line.startswith(("create_ndr", "assign_ndr"))]


def test_no_rule_is_created_by_default(tmp_path):
    assert ndr_lines(tmp_path, {}) == []


def test_a_rule_creates_the_widths_the_spacings_and_the_vias(tmp_path):
    rule = {
        "width": {"m2:m3": "*2", "m4": "0.4 um"},
        "spacing": {"m2": "0.15 um"},
        "vias": ["via23", "via34"],
    }
    assert ndr_lines(tmp_path, {"rules": {"wide": rule}}) == [
        'create_ndr -name "wide" -spacing [list "m2" "0.15"] '
        '-width [list "m2:m3" "*2" "m4" "0.4"] -via [list "via23" "via34"]'
    ]


def test_a_rule_with_only_vias_creates_only_those(tmp_path):
    assert ndr_lines(tmp_path, {"rules": {"v": {"vias": ["via23"]}}}) == [
        'create_ndr -name "v" -via [list "via23"]'
    ]


def test_the_rules_come_in_the_order_of_the_settings_and_the_assignments_follow_them(tmp_path):
    found = ndr_lines(
        tmp_path,
        {
            "rules": {"b": {"width": {"m2": "*2"}}, "a": {"spacing": {"m2": "*3"}}},
            "assignments": {"n1": "a", "n2": "b"},
            "clock_rule": "b",
        },
    )
    assert found == [
        'create_ndr -name "b" -width [list "m2" "*2"]',
        'create_ndr -name "a" -spacing [list "m2" "*3"]',
        'assign_ndr -ndr "a" -net "n1"',
        'assign_ndr -ndr "b" -net "n2"',
        'assign_ndr -ndr "b" -all_clocks',
    ]


def test_the_rules_are_made_before_the_first_route(tmp_path):
    found = route_body(tmp_path, {"ndr": {"clock_rule": "r", "rules": {"r": {"vias": ["v"]}}}})
    assert found.index('create_ndr -name "r" -via [list "v"]') < found.index(
        'assign_ndr -ndr "r" -all_clocks'
    )
    assert found.index('assign_ndr -ndr "r" -all_clocks') < found.index(
        "detailed_route {*}$route_args"
    )


def test_a_name_with_a_bracket_reaches_the_rule_as_text(tmp_path):
    found = ndr_lines(
        tmp_path, {"rules": {"r[0]": {"vias": ["v"]}}, "assignments": {"n$1": "r[0]"}}
    )
    assert found == [
        'create_ndr -name "r\\[0\\]" -via [list "v"]',
        'assign_ndr -ndr "r\\[0\\]" -net "n\\$1"',
    ]


def test_the_commands_of_the_rules_run_under_the_fake(tmp_path):
    calls = run_script(
        route_flow(
            tmp_path,
            {
                "detailed_route": {"default_vias": ["v"], "unidirectional_layers": ["m1"]},
                "ndr": {
                    "rules": {"r": {"width": {"m2": "*2"}}},
                    "assignments": {"n1": "r"},
                    "clock_rule": "r",
                },
            },
        )
    )
    assert names(
        calls,
        "detailed_route_set_default_via",
        "detailed_route_set_unidirectional_layer",
        "create_ndr",
        "assign_ndr",
        "detailed_route",
    )[:6] == [
        "detailed_route_set_default_via",
        "detailed_route_set_unidirectional_layer",
        "create_ndr",
        "assign_ndr",
        "assign_ndr",
        "detailed_route",
    ]


@pytest.mark.parametrize(
    "value, valid",
    [
        ("0.2 um", True),
        ("200 nm", True),
        ("*2", True),
        ("*1.5", True),
        ("2", False),
        (0.2, False),
        ("0 um", False),
        ("-1 um", False),
        ("x2", False),
    ],
)
def test_a_width_is_a_positive_length_or_a_factor(tmp_path, value, valid):
    build = lambda: Openroad.Settings.from_input(  # noqa: E731
        {"ndr": {"rules": {"r": {"width": {"m2": value}}}}}, design_root=tmp_path
    )
    if valid:
        build()
    else:
        with pytest.raises(Exception):
            build()


def test_a_rule_that_changes_nothing_is_refused_with_the_way_out(tmp_path):
    message = refuse_settings(tmp_path, {"ndr": {"rules": {"empty": {}}}})
    assert (
        "the rule `empty` in `ndr.rules` sets no width, no spacing and no via: give one of them "
        "or delete the rule" in message
    )


# ============================================================================== the antenna repair


def repairs(found: list[str]) -> list[str]:
    """The lines of the stage that call `repair_antennas`, with the `if` of the first call."""
    return [
        line.removeprefix("if {[").removesuffix("]} {")
        for line in found
        if "repair_antennas" in line
    ]


@pytest.mark.parametrize(
    "method, flag", [("both", ""), ("diode", " -diode_only"), ("jumper", " -jumper_only")]
)
def test_the_method_of_the_repair_chooses_its_flag_in_every_call(tmp_path, method, flag):
    found = route_body(tmp_path, {"antenna": {"method": method}})
    assert repairs(found) == [f'repair_antennas "ANTENNA_X1"{flag}'] * 2


def test_the_margin_of_the_repair_goes_to_every_call(tmp_path):
    found = route_body(tmp_path, {"antenna": {"ratio_margin_percent": 5}})
    assert repairs(found) == ['repair_antennas "ANTENNA_X1" -ratio_margin "5.0"'] * 2
    assert "-ratio_margin" not in " ".join(route_body(tmp_path / "default"))


def test_the_margin_comes_before_the_method(tmp_path):
    found = route_body(tmp_path, {"antenna": {"ratio_margin_percent": 1, "method": "diode"}})
    assert repairs(found)[0] == 'repair_antennas "ANTENNA_X1" -ratio_margin "1.0" -diode_only'


def test_the_repair_gets_no_count_of_iterations_since_the_loop_counts(tmp_path):
    assert "-iterations" not in " ".join(route_body(tmp_path, {"antenna": {"method": "diode"}}))


def test_a_diode_of_the_settings_replaces_the_diode_of_the_platform(tmp_path):
    found = route_body(tmp_path, {"antenna": {"diode_cell": "MY_DIODE"}})
    assert repairs(found) == ['repair_antennas "MY_DIODE"'] * 2


def test_a_diode_of_the_settings_is_used_where_the_platform_says_the_router_cannot(tmp_path):
    found = route_body(
        tmp_path, {"antenna": {"diode_cell": "MY_DIODE"}}, model={"diode_usable": False}
    )
    assert repairs(found) == ['repair_antennas "MY_DIODE"'] * 2
    assert "antenna repair skipped" not in " ".join(found)
    found = route_body(tmp_path / "plain", model={"diode_usable": False})
    assert repairs(found) == [] and "antenna repair skipped" in " ".join(found)


def test_the_diode_and_its_use_come_from_the_setting_and_else_from_the_platform(tmp_path):
    flow = route_flow(tmp_path, {"antenna": {"diode_cell": "MY_DIODE"}})
    unknown = flow.platform_model.model_copy(update={"antenna_cell": None, "diode_usable": False})
    values = effective(flow.settings, unknown)
    assert (values.diode_cell, values.diode_usable) == ("MY_DIODE", True)


def test_the_options_of_the_repair_run_under_the_fake(tmp_path):
    calls = run_script(
        route_flow(
            tmp_path,
            {"antenna": {"method": "jumper", "ratio_margin_percent": 2.5, "diode_cell": "D"}},
        )
    )
    first, *_ = [call for call in calls if call[0] == "repair_antennas"]
    assert first == ["repair_antennas", "D", "-ratio_margin", "2.5", "-jumper_only"]


@pytest.mark.parametrize(
    "leaf, value",
    [("method", "diode"), ("ratio_margin_percent", 1.0), ("diode_cell", "D")],
)
def test_an_option_of_the_repair_is_refused_without_the_repair(tmp_path, leaf, value):
    message = refuse_settings(tmp_path, {"antenna": {"repair": False, leaf: value}})
    assert f"`antenna.{leaf}` has no effect with `antenna.repair: false`" in message


@pytest.mark.parametrize("margin", [-1, 100, 120])
def test_a_margin_outside_the_percentages_is_refused(tmp_path, margin):
    with pytest.raises(Exception):
        Openroad.Settings.from_input(
            {"antenna": {"ratio_margin_percent": margin}}, design_root=tmp_path
        )


# ============================================================================== the filler cells


def test_the_decap_cells_come_before_the_filler_cells_in_one_command(tmp_path):
    found = fill_body(tmp_path, {"fill": {"decap_cells": ["DECAP_*"]}})
    assert words(found, "filler_placement") == ['filler_placement [list "DECAP_*" "FILL_X1"]']


def test_the_decap_cells_come_before_the_cells_of_the_settings(tmp_path):
    found = fill_body(tmp_path, {"fill": {"decap_cells": ["D"], "cells": ["F1", "F2"]}})
    assert words(found, "filler_placement") == ['filler_placement [list "D" "F1" "F2"]']


def test_decap_cells_alone_fill_when_the_filler_list_is_empty(tmp_path):
    found = fill_body(tmp_path, {"fill": {"decap_cells": ["D"], "cells": []}})
    assert words(found, "filler_placement") == ['filler_placement [list "D"]']


def test_the_prefix_comes_before_the_masters_and_is_not_printed_by_default(tmp_path):
    found = fill_body(tmp_path, {"fill": {"prefix": "PAD_"}})
    assert words(found, "filler_placement") == ['filler_placement -prefix "PAD_" [list "FILL_X1"]']
    assert "-prefix" not in " ".join(fill_body(tmp_path / "default"))


def test_the_check_after_the_filler_can_be_switched_off(tmp_path):
    found = fill_body(tmp_path, {"fill": {"check": False}})
    assert words(found, "filler_placement") and not words(found, "check_placement")
    assert words(fill_body(tmp_path / "default"), "check_placement")


def test_the_options_of_the_filler_run_under_the_fake(tmp_path):
    calls = run_script(
        route_flow(tmp_path, {"fill": {"decap_cells": ["D"], "prefix": "P_", "check": False}})
    )
    (fill,) = [call for call in calls if call[0] == "filler_placement"]
    assert fill == ["filler_placement", "-prefix", "P_", "D FILL_X1"]
    assert "check_placement" not in [call[0] for call in calls]


# ============================================================================== the extraction options


def extract_line(tmp_path: Path, extraction: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """The line of `extract_parasitics` in the stage of the final outputs."""
    found = outputs_body(
        tmp_path, {"extraction": {"method": "rcx", **(extraction or {})}}, **kwargs
    )
    (line,) = [line for line in found if line.startswith("extract_parasitics")]
    return line


def test_the_extraction_has_no_flag_by_default(tmp_path):
    assert extract_line(tmp_path, {"method": "auto"}) == "extract_parasitics"


@pytest.mark.parametrize(
    "option, flag",
    [
        ({"corner_index": 1}, '-corner "1"'),
        ({"corner_count": 2}, '-corner_cnt "2"'),
        ({"max_res": "20 ohm"}, '-max_res "20.0"'),
        ({"cc_model": 4}, '-cc_model "4"'),
        ({"context_depth": 3}, '-context_depth "3"'),
        ({"merge_via_res": False}, "-no_merge_via_res"),
        ({"skip_over_cell": True}, "-skip_over_cell"),
        ({"model_version": 2.0}, '-version "2.0"'),
        ({"lef_values": "res"}, "-lef_res"),
        ({"lef_values": "rc"}, "-lef_rc"),
    ],
    ids=lambda value: json.dumps(value) if isinstance(value, dict) else value,
)
def test_each_option_of_openrcx_prints_its_flag(tmp_path, option, flag):
    assert extract_line(tmp_path, option) == f"extract_parasitics {flag}"


def test_the_coupling_threshold_is_given_in_picofarads_and_printed_in_femtofarads(tmp_path):
    assert extract_line(tmp_path, {"coupling_threshold": "0.0002 pF"}) == (
        "extract_parasitics -coupling_threshold 0.2"
    )
    assert extract_line(tmp_path / "zero", {"coupling_threshold": "0 pF"}) == (
        "extract_parasitics -coupling_threshold 0.0"
    )


def test_the_options_come_in_one_order_the_threshold_and_the_lef_values_first(tmp_path):
    line = extract_line(
        tmp_path,
        {
            "model_version": 1.0,
            "skip_over_cell": True,
            "lef_values": "rc",
            "coupling_threshold": "0.001 pF",
            "corner_count": 1,
        },
    )
    assert line == (
        'extract_parasitics -coupling_threshold 1.0 -lef_rc -corner_cnt "1" -skip_over_cell '
        '-version "1.0"'
    )


def test_the_index_of_the_model_goes_to_the_process_corner(tmp_path):
    found = outputs_body(tmp_path, {"extraction": {"model_index": 2}})
    assert "define_process_corner -ext_model_index 2 X" in found
    assert "define_process_corner -ext_model_index 0 X" in outputs_body(tmp_path / "default")


def test_the_scale_comes_after_the_extraction_and_before_the_file_of_the_parasitics(tmp_path):
    found = outputs_body(tmp_path, {"extraction": {"scale": {"res": 1.5, "gnd": 0.8}}})
    assert 'adjust_rc -res_factor "1.5" -gndc_factor "0.8"' in found
    lines_ = [line.split()[0] for line in found]
    assert (
        lines_.index("extract_parasitics") < lines_.index("adjust_rc") < lines_.index("write_spef")
    )
    assert "adjust_rc" not in " ".join(outputs_body(tmp_path / "default"))


def test_a_scale_of_one_is_a_scale(tmp_path):
    found = outputs_body(tmp_path, {"extraction": {"scale": {"cc": 1}}})
    assert 'adjust_rc -cc_factor "1.0"' in found


def test_the_options_of_openrcx_run_under_the_fake(tmp_path):
    flow = final_flow(
        tmp_path,
        {
            "extraction": {
                "method": "rcx",
                "model_index": 1,
                "corner_index": 0,
                "coupling_threshold": "0.0003 pF",
                "lef_values": "res",
                "scale": {"res": 2},
            }
        },
    )
    calls = run_final_script(flow)
    (extract,) = [call for call in calls if call[0] == "extract_parasitics"]
    assert extract == [
        "extract_parasitics",
        "-coupling_threshold",
        "0.3",
        "-lef_res",
        "-corner",
        "0",
    ]
    (adjust,) = [call for call in calls if call[0] == "adjust_rc"]
    assert adjust == ["adjust_rc", "-res_factor", "2.0"]


def test_the_electrical_filters_of_the_base_take_quantities_and_no_plain_number(tmp_path):
    filters = final_flow(tmp_path).jinja_env.filters
    assert filters["femtofarads"](Capacitance("0.0001 pF")) == "0.1"
    assert filters["ohms"](Resistance("2 kohm")) == "2000.0"
    assert filters["volts"](Voltage("900 mV")) == "0.9"
    assert filters["watts"](Power("2 mW")) == "0.002"
    for name in ("femtofarads", "ohms", "volts", "watts"):
        for value in (True, 1.0, "1", float("nan"), None):
            with pytest.raises(TypeError):
                filters[name](value)
    with pytest.raises(TypeError, match="a resistance with a unit"):
        filters["ohms"](Capacitance("1 fF"))


# ------------------------------------------------------------------------------ the rules file


def rules_of_setting(tmp_path: Path) -> Path:
    path = tmp_path / "mine.rules"
    path.write_text("my rules\n")
    return path


def test_the_rules_file_of_the_setting_replaces_the_file_of_the_platform(tmp_path):
    mine = rules_of_setting(tmp_path)
    given = {"extraction": {"rules_file": str(mine)}}
    flow = final_flow(tmp_path, given)
    assert flow.effective().rcx_rules == mine
    assert flow.platform_model.files["rcx_rules"][0] != mine
    found = outputs_body(tmp_path / "again", given)
    assert f'set_extraction_rules_file "{mine}"' in [tokens(tmp_path, line) for line in found]


def test_a_platform_without_rules_extracts_with_the_rules_file_of_the_setting(tmp_path):
    mine = rules_of_setting(tmp_path)
    flow = route_flow(tmp_path, {"extraction": {"rules_file": str(mine)}}, rules=False)
    values = flow.effective()
    assert (values.extraction, values.rcx_rules) == ("rcx", mine)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def test_the_rules_file_of_the_platform_is_no_input_when_the_setting_gives_another(tmp_path):
    mine = rules_of_setting(tmp_path)
    flow = final_flow(tmp_path, {"extraction": {"rules_file": str(mine)}})
    assert RCX_RULES not in [path.name for path in flow.implicit_inputs]
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def test_the_rules_file_of_the_setting_is_an_input_of_the_run(tmp_path):
    from xeda.flow_runner.trace_inputs import setting_read_paths

    mine = rules_of_setting(tmp_path)
    flow = final_flow(tmp_path, {"extraction": {"rules_file": str(mine)}})
    assert mine in setting_read_paths(flow.settings, Path(flow.run_path))
    assert mine.parent in flow.input_directories()


def test_a_rules_file_that_is_no_file_stops_the_run_before_openroad_starts(tmp_path):
    missing = tmp_path / "missing.rules"
    with pytest.raises(FlowSettingsException) as raised:
        final_flow(tmp_path, {"extraction": {"rules_file": str(missing)}})
    assert (
        f"`extraction.rules_file` names {missing}, which is not a file: give the rules file of "
        "OpenRCX, or delete `extraction.rules_file` to use the file of the platform"
    ) in str(raised.value)


def test_the_rules_file_of_the_setting_makes_the_automatic_extraction_rcx_at_planning(tmp_path):
    mine = rules_of_setting(tmp_path)
    settings = Openroad.Settings.from_input(
        {
            **tiny_settings(tiny_store(tmp_path), residue=TINY_OPENROAD_RESIDUE),
            "extraction": {"rules_file": str(mine), "max_res": "20 ohm"},
        },
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    Openroad.check_settings_supported(settings)


# ------------------------------------------------------------------------------ the refusals


def test_an_option_of_openrcx_is_refused_with_an_estimate_that_the_method_chooses(tmp_path):
    message = refuse_settings(tmp_path, {"extraction": {"method": "estimate", "max_res": "20 ohm"}})
    assert (
        "`extraction.max_res` is an option of OpenRCX and has no effect with "
        "`extraction.method: estimate`: set `extraction.method: rcx` and give the rules, or "
        "delete `extraction.max_res`" in message
    )


def test_an_option_of_openrcx_is_refused_where_auto_gives_an_estimate(tmp_path):
    message = refuse_settings(tmp_path, {"extraction": {"cc_model": 4, "model_index": 1}})
    assert "`extraction.cc_model` is an option of OpenRCX" in message
    assert "`extraction.model_index` is an option of OpenRCX" in message
    assert "the platform 'tiny', which gives no rules file and so gets an estimate from" in message


def test_the_options_of_openrcx_plan_where_the_platform_has_rules(tmp_path):
    store = tiny_store(tmp_path)
    (store / "tiny" / RCX_RULES).write_text("rules\n")
    residue = variant_residue(tmp_path, rules=True, supply=SUPPLY)
    settings = Openroad.Settings.from_input(
        {**tiny_settings(store, residue=residue), "extraction": {"cc_model": 4}},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    Openroad.check_settings_supported(settings)


@pytest.mark.parametrize(
    "leaf, value",
    [
        ("corner_index", -1),
        ("corner_count", 0),
        ("max_res", "-1 ohm"),
        ("max_res", 20),
        ("coupling_threshold", "-0.1 fF"),
        ("coupling_threshold", 0.1),
        ("model_version", 0),
        ("lef_values", "all"),
    ],
)
def test_a_value_outside_the_range_of_openrcx_is_refused(tmp_path, leaf, value):
    with pytest.raises(Exception):
        Openroad.Settings.from_input({"extraction": {leaf: value}}, design_root=tmp_path)


@pytest.mark.parametrize("leaf", ["res", "cc", "gnd"])
@pytest.mark.parametrize("value", [0, -1])
def test_a_factor_of_the_scale_is_positive(tmp_path, leaf, value):
    with pytest.raises(Exception):
        Openroad.Settings.from_input({"extraction": {"scale": {leaf: value}}}, design_root=tmp_path)


# ============================================================================== more outputs


def test_the_delays_and_the_abstract_view_come_after_the_parasitics(tmp_path):
    found = outputs_body(tmp_path)
    order = [line.split()[0] for line in found]
    assert order.index("read_spef") < order.index("write_sdf") < order.index("write_abstract_lef")
    estimated = [line.split()[0] for line in outputs_body(tmp_path / "e", rules=False)]
    assert estimated.index("estimate_parasitics") < estimated.index("write_sdf")


def test_the_sdf_has_no_date_and_the_abstract_view_bloats_the_layers_it_occupies(tmp_path):
    found = outputs_body(tmp_path)
    assert 'write_sdf -no_timestamp "@RUN@/final.sdf"' in found
    assert 'write_abstract_lef -bloat_occupied_layers "@RUN@/final.lef"' in found


def test_the_netlist_has_no_power_pins_unless_asked(tmp_path):
    plain = outputs_body(tmp_path)
    assert 'write_verilog "@RUN@/final.v" -remove_cells [xeda_physical_only_masters]' in plain
    found = outputs_body(tmp_path / "pins", {"netlist_power_pins": True})
    assert (
        'write_verilog "@RUN@/final.v" -include_pwr_gnd -remove_cells [xeda_physical_only_masters]'
        in found
    )


def test_the_files_of_the_platform_make_the_circuit_description(tmp_path):
    flow = final_flow(tmp_path)
    cdl = flow.platform_model.files["liberty"][0].with_name("tiny.cdl")
    cdl.write_text("* cdl\n")
    flow.platform_model = flow.platform_model.model_copy(
        update={"files": {**flow.platform_model.files, "cdl": [cdl]}}
    )
    flow.set_final_outputs()
    assert flow.outputs.cdl == Path(flow.run_path) / "final.cdl"
    script = tokens(tmp_path, render_driver(flow, "openroad_final"))
    assert 'write_cdl -masters [list "@STORE@/tiny/tiny.cdl"] "@RUN@/final.cdl"' in [
        tokens(tmp_path, line) for line in lines(script)
    ]


def test_a_platform_without_cdl_files_writes_no_circuit_description(tmp_path):
    flow = final_flow(tmp_path)
    flow.set_final_outputs()
    assert flow.outputs.cdl is None
    assert "write_cdl" not in render_final(flow)


def test_the_timing_model_comes_after_the_reports_with_the_source_latency_at_zero(tmp_path):
    """The model needs the source latency of the clocks at 0. The reports and the verdict read the
    constraints of the design, so the model comes after them."""
    assert final_body(tmp_path, "xeda_timing_model", {"write_timing_model": True}) == [
        "set_clock_latency -source 0 [all_clocks]",
        'write_timing_model "@RUN@/final.lib"',
    ]
    body = " ".join(outputs_body(tmp_path / "outputs", {"write_timing_model": True}))
    assert "set_clock_latency" not in body and "write_timing_model" not in body
    driver = render_final(final_flow(tmp_path / "driver", {"write_timing_model": True}))
    calls = [line.strip() for line in driver.splitlines()]
    report = calls.index("xeda_final_report")
    assert calls[report + 1] == "xeda_timing_model"
    default = render_final(final_flow(tmp_path / "default"))
    assert "xeda_timing_model" not in default and "write_timing_model" not in default
    flow = final_flow(tmp_path / "flow", {"write_timing_model": True})
    flow.set_final_outputs()
    assert flow.outputs.timing_model == Path(flow.run_path) / "final.lib"


def test_the_timing_model_is_an_output_only_with_its_switch():
    from xeda.flow.io import declared_outputs

    outputs = declared_outputs(Openroad)
    assert outputs["timing_model"].enabled_by == "write_timing_model"
    assert outputs["timing_model"].types == (SourceType.Liberty,)
    assert (outputs["sdf"].types, outputs["sdf"].enabled_by) == ((SourceType.Sdf,), None)
    assert (outputs["abstract_lef"].types, outputs["abstract_lef"].enabled_by) == (
        (SourceType.Data,),
        None,
    )
    assert (outputs["cdl"].types, outputs["cdl"].enabled_by) == ((SourceType.Cdl,), None)


def test_the_new_outputs_are_made_ready_before_openroad_starts(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = final_flow(tmp_path, {"write_timing_model": True})
    in_run_directory(flow, monkeypatch)
    ready = ready_files(flow, monkeypatch)
    assert {"final.sdf", "final.lef", "final.lib"} <= set(ready)
    assert "final.cdl" not in ready


def test_the_files_the_new_outputs_name_are_written_under_the_fake(tmp_path, monkeypatch):
    flow = run_both(tmp_path, monkeypatch, {"write_timing_model": True})
    for name in ("final.sdf", "final.lef", "final.lib"):
        assert (Path(flow.run_path) / name).is_file(), name


# ============================================================================== the text reports


def test_each_report_is_written_to_a_file_of_a_fixed_name(tmp_path):
    found = report_body(tmp_path, {"reports": {"check_setup": True}})
    assert 'check_setup -verbose > "reports/check_setup.rpt"' in found
    assert found.index('report_power > "reports/power.rpt"') < found.index(
        'check_setup -verbose > "reports/check_setup.rpt"'
    )
    assert found.index('check_setup -verbose > "reports/check_setup.rpt"') < found.index(
        "report_cell_usage"
    )


def test_the_clock_skew_report_off_leaves_out_the_report_and_the_metrics_of_the_skew(tmp_path):
    found = report_body(tmp_path, {"reports": {"clock_skew": False}})
    assert 'report_clock_skew > "reports/clock_skew.rpt"' not in found
    assert found[-1] == "xeda_metrics 1 0"
    calls = run_final_script(final_flow(tmp_path / "run", {"reports": {"clock_skew": False}}))
    assert "report_clock_skew_metric" not in [call[0] for call in calls]
    assert "report_clock_skew" not in [call[0] for call in calls]


def test_the_power_report_off_keeps_the_power_metric(tmp_path):
    found = report_body(tmp_path, {"reports": {"power": False}})
    assert not any(line.startswith("report_power") for line in found)
    calls = run_final_script(final_flow(tmp_path / "run", {"reports": {"power": False}}))
    assert "report_power_metric" in [call[0] for call in calls]


def test_the_timing_report_follows_its_options(tmp_path):
    found = report_body(
        tmp_path,
        {
            "reports": {
                "timing": {
                    "paths": 5,
                    "format": "summary",
                    "fields": ["net", "fanout"],
                    "unconstrained": False,
                }
            }
        },
    )
    assert (
        'report_checks -path_delay min_max -group_path_count "5" -format "summary" '
        '-fields [list "net" "fanout"] > "reports/timing.rpt"'
    ) in found


def test_the_timing_report_off_writes_no_file(tmp_path):
    found = report_body(tmp_path, {"reports": {"timing": {"enabled": False}}})
    assert not any(line.startswith("report_checks") for line in found)
    flow = final_flow(tmp_path / "flow", {"reports": {"timing": {"enabled": False}}})
    assert "reports/timing.rpt" not in flow.final_report_files(flow.settings.reports)


def test_the_reports_the_settings_switch_on_are_the_files_the_process_makes_ready(
    tmp_path, monkeypatch
):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = final_flow(
        tmp_path,
        {"reports": {"clock_skew": False, "power": False, "check_setup": True}},
    )
    in_run_directory(flow, monkeypatch)
    ready = ready_files(flow, monkeypatch)
    assert {"reports/timing.rpt", "reports/check_setup.rpt"} <= set(ready)
    assert not {"reports/clock_skew.rpt", "reports/power.rpt"} & set(ready)


def test_the_first_process_makes_no_report_of_the_second_ready(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    ready: list[str] = []
    guard = flow._guard_write

    def record(path):
        ready.append(str(flow._relative_to_run(path)))
        return guard(path)

    monkeypatch.setattr(flow, "_guard_write", record)
    flow.run_route()
    assert not {"reports/timing.rpt", "reports/power.rpt"} & set(ready)


@pytest.mark.parametrize(
    "leaf, value",
    [("paths", 0), ("format", "table"), ("fields", ["name"])],
)
def test_a_value_outside_the_timing_report_is_refused(tmp_path, leaf, value):
    with pytest.raises(Exception):
        Openroad.Settings.from_input({"reports": {"timing": {leaf: value}}}, design_root=tmp_path)


def test_an_option_of_the_timing_report_is_refused_with_the_report_off(tmp_path):
    message = refuse_settings(tmp_path, {"reports": {"timing": {"enabled": False, "paths": 3}}})
    assert "`reports.timing.paths` has no effect while `reports.timing.enabled` is false" in message


# ============================================================================== the IR drop options


def analysis_lines(tmp_path: Path, ir_drop: dict[str, Any] | None = None, **kwargs: Any):
    """The lines of the stage of the final report that set up and run the IR drop analysis."""
    found = report_body(tmp_path, {"ir_drop": ir_drop or {}}, **kwargs)
    return [
        line
        for line in found
        if line.startswith(
            (
                "xeda_ir_setup",
                "set_pdnsim",
                "analyze_power_grid",
                "write_pg_spice",
                "puts",
            )
        )
    ]


def test_the_defaults_set_the_voltage_and_analyze_each_net_with_an_error_report_only(tmp_path):
    assert analysis_lines(tmp_path) == [
        'set_pdnsim_net_voltage -net "VDD" -voltage 1.1',
        'analyze_power_grid -net "VDD" -error_file "reports/irdrop_1.rpt"',
        'set_pdnsim_net_voltage -net "VSS" -voltage 0.0',
        'analyze_power_grid -net "VSS" -error_file "reports/irdrop_2.rpt"',
    ]
    assert "xeda_ir_setup" not in render_final(final_flow(tmp_path / "d"))


def test_the_nets_of_the_setting_replace_the_nets_of_the_platform_in_their_order(tmp_path):
    found = analysis_lines(tmp_path, {"nets": {"VSS": "0 V", "VDDA": "0.9 V"}})
    assert found == [
        'set_pdnsim_net_voltage -net "VSS" -voltage 0.0',
        'analyze_power_grid -net "VSS" -error_file "reports/irdrop_1.rpt"',
        'set_pdnsim_net_voltage -net "VDDA" -voltage 0.9',
        'analyze_power_grid -net "VDDA" -error_file "reports/irdrop_2.rpt"',
    ]
    flow = final_flow(tmp_path / "e", {"ir_drop": {"nets": {"VSS": "0 V", "VDDA": "0.9 V"}}})
    values = flow.effective()
    assert values.supply_voltages == (("VSS", 0.0), ("VDDA", 0.9))
    assert values.ir_drop_reports == ("reports/irdrop_1.rpt", "reports/irdrop_2.rpt")


def test_the_nets_of_the_setting_give_an_analysis_where_the_platform_gives_none(tmp_path):
    found = analysis_lines(tmp_path, {"nets": {"VDD": "1 V"}}, supply=None)
    assert found == [
        'set_pdnsim_net_voltage -net "VDD" -voltage 1.0',
        'analyze_power_grid -net "VDD" -error_file "reports/irdrop_1.rpt"',
    ]


def test_a_negative_voltage_is_refused(tmp_path):
    with pytest.raises(Exception):
        Openroad.Settings.from_input({"ir_drop": {"nets": {"VDD": "-1 V"}}}, design_root=tmp_path)


def test_the_model_of_the_sources_is_set_once_before_the_analysis(tmp_path):
    found = analysis_lines(
        tmp_path,
        {
            "source": {
                "bump_dx": "100 um",
                "bump_size": "50 um",
                "bump_interval": 2,
                "external_resistance": "0.5 ohm",
            }
        },
    )
    assert found[0] == "xeda_ir_setup"
    script = render_final(
        final_flow(
            tmp_path / "s",
            {"ir_drop": {"source": {"bump_dx": "100 um", "strap_track_pitch": 20}}},
        )
    )
    body = stage(script, "xeda_ir_setup")
    assert body == ['set_pdnsim_source_settings -bump_dx "100" -strap_track_pitch "20"']


def test_the_flags_of_the_model_of_the_sources_follow_the_command(tmp_path):
    flow = final_flow(
        tmp_path,
        {
            "ir_drop": {
                "source": {
                    "bump_dx": "1 um",
                    "bump_dy": "2 um",
                    "bump_size": "3 um",
                    "bump_interval": 4,
                    "strap_track_pitch": 5,
                    "external_resistance": "6 ohm",
                }
            }
        },
    )
    (setup,) = stage(render_final(flow), "xeda_ir_setup")
    assert setup == (
        'set_pdnsim_source_settings -bump_dx "1" -bump_dy "2" -bump_size "3" '
        '-bump_interval "4" -strap_track_pitch "5" -external_resistance "6.0"'
    )


def test_the_power_of_an_instance_is_set_in_watts_before_the_analysis(tmp_path):
    flow = final_flow(tmp_path, {"ir_drop": {"instance_power": {"u1": "1 mW", "u[2]": "20 uW"}}})
    assert stage(render_final(flow), "xeda_ir_setup") == [
        'set_pdnsim_inst_power -inst "u1" -power 0.001',
        'set_pdnsim_inst_power -inst "u\\[2\\]" -power 2e-05',
    ]


def test_the_power_of_a_flattened_instance_goes_to_the_tool_with_its_dotted_name(tmp_path):
    settings = {"ir_drop": {"instance_power": {"u_core": {"reg_1": "1 mW"}}}}
    flow = final_flow(tmp_path, settings)
    assert stage(render_final(flow), "xeda_ir_setup") == [
        'set_pdnsim_inst_power -inst "u_core.reg_1" -power 0.001'
    ]


@pytest.mark.parametrize("source_type", ["full", "bumps", "straps"])
def test_the_source_type_goes_to_every_analysis_in_capitals(tmp_path, source_type):
    found = analysis_lines(tmp_path, {"source_type": source_type})
    analyses = [line for line in found if line.startswith("analyze_power_grid")]
    assert len(analyses) == 2
    assert all(f'-source_type "{source_type.upper()}"' in line for line in analyses)


def test_a_file_of_voltage_sources_goes_to_the_analysis_of_its_net_only(tmp_path):
    vsrc = tmp_path / "vdd.loc"
    vsrc.write_text("loc\n")
    found = analysis_lines(tmp_path, {"vsrc_files": {"VDD": str(vsrc)}})
    analyses = [line for line in found if line.startswith("analyze_power_grid")]
    assert f'-vsrc "{vsrc}"' in analyses[0] and "-vsrc" not in analyses[1]


def test_the_voltages_of_the_instances_and_the_currents_are_numbered_files(tmp_path):
    found = analysis_lines(tmp_path, {"instance_voltages": True, "electromigration": True})
    analyses = [line for line in found if line.startswith("analyze_power_grid")]
    assert analyses[1] == (
        'analyze_power_grid -net "VSS" -error_file "reports/irdrop_2.rpt" '
        '-voltage_file "reports/irdrop_voltage_2.csv" '
        '-enable_em -em_outfile "reports/irdrop_em_2.csv"'
    )


def test_the_spice_netlist_of_each_net_is_a_numbered_file(tmp_path):
    vsrc = tmp_path / "vdd.loc"
    vsrc.write_text("loc\n")
    found = analysis_lines(
        tmp_path, {"spice": True, "source_type": "straps", "vsrc_files": {"VDD": str(vsrc)}}
    )
    spice = [line for line in found if line.startswith("write_pg_spice")]
    assert spice == [
        f'write_pg_spice -net "VDD" -vsrc "{vsrc}" -source_type "STRAPS" '
        '"reports/irdrop_spice_1.sp"',
        'write_pg_spice -net "VSS" -source_type "STRAPS" "reports/irdrop_spice_2.sp"',
    ]


def test_the_files_of_the_analysis_are_the_ones_the_process_makes_ready(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = final_flow(
        tmp_path,
        {"ir_drop": {"instance_voltages": True, "electromigration": True, "spice": True}},
    )
    in_run_directory(flow, monkeypatch)
    ready = ready_files(flow, monkeypatch)
    expected = {
        f"reports/irdrop_{kind}{n}.{ext}"
        for kind, ext in (("", "rpt"), ("voltage_", "csv"), ("em_", "csv"), ("spice_", "sp"))
        for n in (1, 2)
    }
    assert expected <= set(ready)
    assert flow.effective().ir_drop_files[:2] == ("reports/irdrop_1.rpt", "reports/irdrop_2.rpt")


def test_the_options_of_the_analysis_run_under_the_fake(tmp_path):
    flow = final_flow(
        tmp_path,
        {
            "ir_drop": {
                "source_type": "bumps",
                "source": {"bump_dx": "90 um"},
                "instance_power": {"u1": "0.5 W"},
                "instance_voltages": True,
                "spice": True,
            }
        },
    )
    names_ = [call[0] for call in run_final_script(flow)]
    assert names_.count("analyze_power_grid") == 2 and names_.count("write_pg_spice") == 2
    assert names_.index("set_pdnsim_source_settings") < names_.index("set_pdnsim_net_voltage")
    assert names_.index("set_pdnsim_inst_power") < names_.index("analyze_power_grid")


def test_an_option_of_the_analysis_is_refused_with_the_analysis_off(tmp_path):
    message = refuse_settings(tmp_path, {"ir_drop": {"enabled": False, "spice": True}})
    assert "`ir_drop.spice` has no effect while `ir_drop.enabled` is false" in message


def test_a_file_of_voltage_sources_for_a_net_the_table_leaves_out_is_refused(tmp_path):
    vsrc = tmp_path / "x.loc"
    vsrc.write_text("loc\n")
    message = refuse_settings(
        tmp_path, {"ir_drop": {"nets": {"VDD": "1 V"}, "vsrc_files": {"VSS": str(vsrc)}}}
    )
    assert (
        "`ir_drop.vsrc_files` names the net 'VSS', which `ir_drop.nets` does not list: add the "
        "net to `ir_drop.nets` or delete its file" in message
    )


def test_a_file_of_voltage_sources_for_a_net_the_platform_does_not_give_stops_the_run(tmp_path):
    vsrc = tmp_path / "x.loc"
    vsrc.write_text("loc\n")
    flow = final_flow(tmp_path, {"ir_drop": {"vsrc_files": {"VDDX": str(vsrc)}}})
    with pytest.raises(FlowSettingsException) as raised:
        flow.effective()
    assert (
        "`ir_drop.vsrc_files` names the net 'VDDX', which the analysis does not cover (the nets "
        "are VDD, VSS)" in str(raised.value)
    )


# ------------------------------------------------------------------------------ the decap cells


DECAP = {"enabled": True, "target_capacitance": 5.0, "cells": {"DECAP_X*": 0.5, "DECAP_X1": 1}}


def decap_flow(tmp_path: Path, ir_drop: dict[str, Any] | None = None, **kwargs: Any) -> Openroad:
    return route_flow(
        tmp_path, {"ir_drop": {"decap": DECAP, **(ir_drop or {})}}, supply=SUPPLY, **kwargs
    )


def decap_body(tmp_path: Path, ir_drop: dict[str, Any] | None = None) -> list[str]:
    return stage(render(decap_flow(tmp_path, ir_drop)), "xeda_ir_decap")


def test_no_decap_cell_is_inserted_and_no_stage_exists_by_default(tmp_path):
    for script in (render(route_flow(tmp_path)), render_final(final_flow(tmp_path / "f"))):
        assert "xeda_ir_decap" not in script and "insert_decap" not in script


def test_the_decap_cells_go_in_after_the_analysis_of_the_net_with_the_global_connections(tmp_path):
    body = [tokens(tmp_path, line) for line in decap_body(tmp_path)]
    assert body == [
        'set_pdnsim_net_voltage -net "VDD" -voltage 1.1',
        'analyze_power_grid -net "VDD"',
        'insert_decap -target_cap 5.0 -cells [list "DECAP_X*" "0.5" "DECAP_X1" "1.0"] -net "VDD"',
        "global_connect",
    ]


def test_the_decap_net_of_the_setting_wins_and_gets_the_model_of_the_sources(tmp_path):
    found = decap_body(
        tmp_path,
        {
            "decap": {**DECAP, "net": "VSS"},
            "source_type": "full",
            "instance_power": {"u": "1 W"},
        },
    )
    assert found[:3] == [
        "xeda_ir_setup",
        'set_pdnsim_net_voltage -net "VSS" -voltage 0.0',
        'analyze_power_grid -net "VSS" -source_type "FULL"',
    ]
    assert found[-2] == (
        'insert_decap -target_cap 5.0 -cells [list "DECAP_X*" "0.5" "DECAP_X1" "1.0"] -net "VSS"'
    )
    # no setup procedure exists where no setting needs one
    assert "xeda_ir_setup" not in render(decap_flow(tmp_path / "again", {"source_type": "full"}))


def test_the_decap_cells_are_inserted_before_the_filler_cells_and_the_outputs(tmp_path):
    calls = [call[0] for call in run_script(decap_flow(tmp_path))]
    assert calls.index("detailed_route") < calls.index("analyze_power_grid")
    assert calls.index("analyze_power_grid") < calls.index("insert_decap")
    assert calls.index("insert_decap") < calls.index("filler_placement")
    assert calls.index("filler_placement") < calls.index("write_db")


def test_the_driver_calls_the_decap_stage_between_the_route_and_the_filler(tmp_path):
    body = lines(render(decap_flow(tmp_path)))
    start = body.index("if {[catch {")
    assert body[start : start + 4] == [
        "if {[catch {",
        "xeda_detail_route",
        "xeda_ir_decap",
        "xeda_fillcell",
    ]


def test_the_decap_stage_is_in_the_first_process_only(tmp_path):
    flow = decap_flow(tmp_path)
    assert "insert_decap" not in render_final(flow)
    assert "insert_decap" in render(flow)


def test_a_decap_without_a_capacitance_or_a_cell_is_refused_with_the_way_out(tmp_path):
    message = refuse_settings(tmp_path, {"ir_drop": {"decap": {"enabled": True}}})
    assert (
        "`ir_drop.decap.enabled` needs `ir_drop.decap.target_capacitance`: give the capacitance "
        "to insert or set `ir_drop.decap.enabled: false`" in message
    )
    assert "`ir_drop.decap.enabled` needs `ir_drop.decap.cells`" in message


def test_an_option_of_the_decap_is_refused_with_the_decap_off(tmp_path):
    message = refuse_settings(tmp_path, {"ir_drop": {"decap": {"target_capacitance": 2.0}}})
    assert (
        "`ir_drop.decap.target_capacitance` has no effect while `ir_drop.decap.enabled` is false"
        in message
    )


def test_a_decap_net_that_the_table_leaves_out_is_refused(tmp_path):
    message = refuse_settings(
        tmp_path, {"ir_drop": {"nets": {"VDD": "1 V"}, "decap": {**DECAP, "net": "VSS"}}}
    )
    assert "`ir_drop.decap.net` is 'VSS', which `ir_drop.nets` does not list" in message


def test_a_decap_net_that_the_platform_does_not_give_stops_the_run(tmp_path):
    flow = decap_flow(tmp_path, {"decap": {**DECAP, "net": "VDDX"}})
    with pytest.raises(FlowSettingsException) as raised:
        flow.effective()
    assert "`ir_drop.decap` needs the net 'VDDX', which the analysis does not cover" in str(
        raised.value
    )
    flow = route_flow(tmp_path / "none", {"ir_drop": {"decap": DECAP}})
    with pytest.raises(FlowSettingsException):
        flow.effective()


def test_decap_cells_with_a_table_of_nets_need_the_net_that_gets_them(tmp_path):
    message = refuse_settings(
        tmp_path, {"ir_drop": {"nets": {"VDD": "1 V", "VSS": "0 V"}, "decap": DECAP}}
    )
    assert (
        "`ir_drop.decap.enabled` with `ir_drop.nets` needs `ir_drop.decap.net`: name the supply "
        "net that gets the cells" in message
    )
    # naming the net is all that the rule asks for
    store = tiny_store(tmp_path, "ok_store")
    Openroad.check_settings_supported(
        Openroad.Settings.from_input(
            {
                **tiny_settings(store, residue=TINY_OPENROAD_RESIDUE),
                "ir_drop": {"nets": {"VDD": "1 V"}, "decap": {**DECAP, "net": "VDD"}},
            },
            design_root=tmp_path,
            runner_cwd=tmp_path,
        )
    )


# ============================================================================== the GDS options


def merge_names(flow: Openroad) -> dict[str, str]:
    """The names that the merge defines for the script, as the recorded command line gives them."""
    words = klayout_arguments(flow)
    return dict(word.split("=", 1) for before, word in zip(words, words[1:]) if before == "-rd")


def test_the_layer_map_goes_to_the_merge(tmp_path, monkeypatch):
    layer_map = tmp_path / "layers.map"
    layer_map.write_text("map\n")
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"layer_map": str(layer_map)}})
    assert merge_names(flow)["layer_map"] == str(layer_map)


def test_the_files_of_the_platform_come_before_the_libraries_of_the_settings(tmp_path, monkeypatch):
    first, second = tmp_path / "a.gds", tmp_path / "b.oas"
    for path in (first, second):
        path.write_bytes(b"x\n")
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"cell_libraries": [str(first), str(second)]}})
    platform = flow.platform_model.files["gds"][0]
    assert merge_names(flow)["in_files"] == f"{platform}\n{first}\n{second}"


def test_a_library_of_the_settings_is_an_input_of_the_run(tmp_path):
    from xeda.flow_runner.trace_inputs import setting_read_paths

    library = tmp_path / "macro.gds"
    library.write_bytes(b"x\n")
    layer_map = tmp_path / "layers.map"
    layer_map.write_text("map\n")
    flow = gds_flow(
        tmp_path, {"gds": {"cell_libraries": [str(library)], "layer_map": str(layer_map)}}
    )
    read = setting_read_paths(flow.settings, Path(flow.run_path))
    assert library in read and layer_map in read


def test_the_regular_expression_of_the_cells_that_may_be_empty_goes_to_the_merge(
    tmp_path, monkeypatch
):
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"allow_empty": "fakeram.*|SRAM_.*"}})
    assert merge_names(flow)["allow_empty"] == "fakeram.*|SRAM_.*"


def test_text_that_is_no_regular_expression_is_refused(tmp_path):
    with pytest.raises(Exception, match="no regular expression"):
        Openroad.Settings.from_input({"gds": {"allow_empty": "("}}, design_root=tmp_path)


@pytest.mark.parametrize(
    "setting, name",
    [
        ("add_to_cell", "AddToCell"),
        ("overwrite_cell", "OverwriteCell"),
        ("rename_cell", "RenameCell"),
        ("skip_new_cell", "SkipNewCell"),
    ],
)
def test_each_policy_for_a_cell_that_is_in_already_is_a_member_of_the_klayout_enumeration(
    tmp_path, monkeypatch, setting, name
):
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"conflict_resolution": setting}})
    assert merge_names(flow)["conflict_resolution"] == name


@pytest.mark.parametrize(
    "leaf, value, names_",
    [
        ("read_lef_with_def", True, {"read_lef_with_def": "true"}),
        ("read_lef_with_def", False, {"read_lef_with_def": "false"}),
        ("net_properties", False, {"net_properties": "false"}),
        ("instance_properties", False, {"instance_properties": "false"}),
        ("pin_properties", True, {"pin_properties": "true"}),
    ],
)
def test_the_options_of_the_reader_are_unset_true_or_false(
    tmp_path, monkeypatch, leaf, value, names_
):
    flow = run_gds(tmp_path, monkeypatch, {"gds": {"lefdef_options": {leaf: value}}})
    found = merge_names(flow)
    assert {name: found[name] for name in names_} == names_
    unset = {
        name
        for name in ("read_lef_with_def", "net_properties", "instance_properties", "pin_properties")
        if name not in names_
    }
    assert all(found[name] == "" for name in unset)


def test_the_time_stamps_are_off_unless_asked_for(tmp_path, monkeypatch):
    assert merge_names(run_gds(tmp_path, monkeypatch))["write_timestamps"] == "false"
    flow = run_gds(tmp_path / "on", monkeypatch, {"gds": {"timestamps": True}})
    assert merge_names(flow)["write_timestamps"] == "true"


def test_an_option_of_the_layout_is_refused_without_write_gds(tmp_path):
    for leaf, value in (
        ("timestamps", True),
        ("conflict_resolution", "rename_cell"),
        ("allow_empty", "x.*"),
    ):
        message = refuse_settings(tmp_path / leaf, {"gds": {leaf: value}})
        assert f"`gds.{leaf}` has no effect with `write_gds: false`" in message
    message = refuse_settings(
        tmp_path / "sub", {"gds": {"lefdef_options": {"net_properties": False}}}
    )
    assert "`gds.lefdef_options.net_properties` has no effect with `write_gds: false`" in message


def test_a_platform_without_gds_files_merges_the_libraries_of_the_settings(tmp_path):
    library = tmp_path / "all.gds"
    library.write_bytes(b"x\n")
    store = tiny_store(tmp_path)
    (store / "tiny" / KLAYOUT_TECH).write_text(KLAYOUT_TECH_TEXT)
    residue = yaml.safe_load(TINY_OPENROAD_RESIDUE.read_text())
    residue["files"]["klayout_tech"] = KLAYOUT_TECH
    path = tmp_path / "no_gds.yaml"
    path.write_text(yaml.safe_dump(residue, sort_keys=False))
    given = {**tiny_settings(store, residue=path), "write_gds": True}
    with pytest.raises(FlowSettingsException) as raised:
        Openroad.check_settings_supported(
            Openroad.Settings.from_input(given, design_root=tmp_path, runner_cwd=tmp_path)
        )
    assert "gives no gds" in str(raised.value)
    Openroad.check_settings_supported(
        Openroad.Settings.from_input(
            {**given, "gds": {"cell_libraries": [str(library)]}},
            design_root=tmp_path,
            runner_cwd=tmp_path,
        )
    )


def test_the_merge_script_reads_every_name_that_the_flow_defines(tmp_path, monkeypatch):
    """The script gets its inputs as global names that KLayout defines from `-rd`. A name that
    the flow defines and the script does not read would do nothing, and a name that the script
    reads and the flow does not define would stop it."""
    import ast

    tree = ast.parse((ASSETS / "def2stream.py").read_text())
    (main,) = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
    ]
    read = {node.id for node in ast.walk(main) if isinstance(node, ast.Name)}
    defined = set(merge_names(run_gds(tmp_path, monkeypatch)))
    assert defined <= read, sorted(defined - read)
    kinds = {"pya", "merge_gds", "lefdef_options_of", "sys", "NameError"}
    assert read - kinds <= defined | {"allow_empty"}, sorted(read - kinds - defined)
