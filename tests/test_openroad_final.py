"""The last OpenROAD flow, `openroad`: the first process, which restarts from the globally routed
database, routes the design in detail, repairs the antenna violations of the route with diodes, and
places the filler cells.

The tests render the driver script of the process for the `tiny` platform and compare it with a
golden file. They run the rendered script under the fake OpenROAD, so a command that the OpenROAD
command list does not know fails the test. The flow has no `run()` yet, so it is not registered:
the tests run it through a class that calls `run_route()`.
"""

import functools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowSettingsException, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import Openroad, OpenroadStage, base, final
from xeda.flows.openroad.final import EffectiveFinal, effective
from xeda.flows.openroad.final_groups import ViaInPin
from xeda.platforms.model import CellPort
from xeda.run_dir import RunDirectory
from xeda.tool import NonZeroExitCode, Tool

from .flow_registry import preserved_flow_registry
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
TEMPLATES = Path(base.__file__).parent / "templates"


# ------------------------------------------------------------------------------ helpers


@functools.cache
def runnable_openroad() -> type[Openroad]:
    """Return a class of the flow that runs the first process, under the name `openroad`.

    The flow has no `run()` yet, so Xeda keeps it out of the registry and no test can build it.
    The class registers while it is made, and the registry is as it was after."""
    with preserved_flow_registry():

        class Openroad(final.Openroad):  # type: ignore[no-redef]
            """The first process of the last flow."""

            def run(self) -> None:
                self.run_route()

    return Openroad


def route_flow(
    tmp_path: Path,
    settings: dict[str, Any] | None = None,
    *,
    model: dict[str, Any] | None = None,
    launched: bool = False,
) -> Openroad:
    """Return the flow on the `tiny` platform, ready to render: its run directory is
    `tmp_path/work`, its database and SDC file are in `tmp_path/inputs`, and `prepare_inputs()`
    has run.

    `settings` are nested settings (a group is a dictionary). `model` replaces fields of the
    platform model after `prepare_inputs()`. A flow built without a launcher deletes nothing in
    its run directory; with `launched`, the run directory is one that a launcher chose, under
    `tmp_path/xeda_run`."""
    store = tiny_store(tmp_path, "store")
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), **(settings or {})}
    run_path = tmp_path / "work"
    run_directory = None
    if launched:
        run_root = tmp_path / "xeda_run"
        run_path = run_root / "counter" / "openroad"
        run_directory = RunDirectory.claimed(run_path, run_root)
    run_path.mkdir(parents=True, exist_ok=True)
    cls = runnable_openroad()
    flow = cls(
        cls.Settings.from_input(given, design_root=design.design_root, runner_cwd=tmp_path),
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


def render(flow: Openroad) -> str:
    return render_driver(flow, "openroad_route")


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
        '-verbose 1 -droute_end_iter "64" -drc_report_iter_step "5"]',
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
        '-verbose 1 -droute_end_iter "32" -or_seed "3" -or_k "2" -drc_report_iter_step "0"]'
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
        {"detailed_route": {"extra_args": ["-via_access_layer", "m2", "-clean_patches"]}},
    )
    (route,) = [line for line in found if line.startswith("set route_args")]
    assert route.endswith('-drc_report_iter_step "5" "-via_access_layer" "m2" "-clean_patches"]')


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

    def execute_in_run_directory(self: Tool, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cwd", flow.run_path)
        return execute(self, *args, **kwargs)

    monkeypatch.setattr(Tool, "execute", execute_in_run_directory)


def run_route(
    tmp_path: Path, monkeypatch, settings: dict[str, Any] | None = None, **kwargs: Any
) -> Openroad:
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path, settings, **kwargs)
    in_run_directory(flow, monkeypatch)
    flow.run()
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
    flow.run()
    assert sorted(ready) == sorted(
        [
            "metrics_route.json",
            "route.odb",
            "route.sdc",
            "reports/drc.rpt",
            "reports/maze.log",
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
    flow.run()
    assert not (flow.run_path / "openroad-failed.odb").exists()


def test_the_failure_of_the_process_names_the_flow_the_script_and_the_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "detailed_route")
    require_tclsh()
    use_fake_openroad(monkeypatch)
    flow = route_flow(tmp_path)
    in_run_directory(flow, monkeypatch)
    with pytest.raises(NonZeroExitCode) as error:
        flow.run()
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
        {"detailed_route": {"extra_args": ["-via_access_layer", "m2"]}},
        {"detailed_route": {"extra_args": ["-min_access_points", "3", "-save_guide_updates"]}},
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
        "antennas": "reports/antennas.rpt",
    }
    assert Openroad.metrics_files == ("metrics_route.json",)
    assert Openroad.working_files == {"odb": "route.odb", "sdc": "route.sdc"}
    for name in (*Openroad.reports.values(), *Openroad.working_files.values()):
        assert not Path(name).is_absolute() and ".." not in Path(name).parts


def test_the_flow_is_not_registered_until_it_can_run_every_process():
    assert "openroad" not in registered_flows and "Openroad" not in registered_flows
    assert issubclass(Openroad, OpenroadStage)


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
    return " ".join(str(error.value).split())


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
