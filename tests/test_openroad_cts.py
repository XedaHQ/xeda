"""The clock tree flow, `openroad_cts`: the restart from the placed database, the clock tree, the
legalization and the repair of the timing.

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
from xeda.flows.openroad import OpenroadCts, base
from xeda.flows.openroad.cts import EffectiveCts, effective

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    render_driver,
    run_fake_openroad,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import fake_calls, require_tclsh, use_fake_openroad

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics"
GOLDEN = Path(__file__).parent / "resources" / "openroad" / "cts_defaults.tcl"
TEMPLATES = Path(base.__file__).parent / "templates"


# ------------------------------------------------------------------------------ helpers


def cts_flow(
    tmp_path: Path,
    settings: dict[str, Any] | None = None,
    *,
    model: dict[str, Any] | None = None,
) -> OpenroadCts:
    """Return the flow on the `tiny` platform, ready to render: its run directory is
    `tmp_path/work`, its database and SDC file are in `tmp_path/inputs`, its outputs are set and
    `prepare_inputs()` has run.

    `settings` are nested settings (a group is a dictionary). `model` replaces fields of the
    platform model after `prepare_inputs()`."""
    store = tiny_store(tmp_path, "store")
    design = Design.from_file(openroad_design(tmp_path / "design"))
    given = {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), **(settings or {})}
    run_path = tmp_path / "work"
    run_path.mkdir(exist_ok=True)
    flow = OpenroadCts(
        OpenroadCts.Settings.from_input(given, design_root=design.design_root, runner_cwd=tmp_path),
        design,
        run_path,
    )
    flow.init()
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "place.odb").write_text("odb\n")
    (inputs / "place.sdc").write_text("create_clock -name clk -period 5 [get_ports clk]\n")
    flow.inputs.odb = inputs / "place.odb"
    flow.inputs.sdc = inputs / "place.sdc"
    flow.outputs.odb = run_path / "cts.odb"
    flow.outputs.sdc = run_path / "cts.sdc"
    flow.prepare_inputs()
    if model:
        flow.platform_model = flow.platform_model.model_copy(update=model)
    return flow


def render(flow: OpenroadCts) -> str:
    return render_driver(flow, "openroad_cts")


def run_script(flow: OpenroadCts, script: str | None = None) -> list[list[str]]:
    """Run the driver of `flow` under the fake OpenROAD in its run directory; return the commands
    that the fake recorded, one list of words for each."""
    require_tclsh()
    text = render(flow) if script is None else script
    done = run_fake_openroad(flow.run_path, text, driver="openroad_cts")
    assert done.returncode == 0, done.stderr + done.stdout
    return fake_calls(flow.run_path)


def lines(script: str) -> list[str]:
    """The lines of a script without comments, blank lines and indentation."""
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def stage(script: str, name: str = "xeda_cts") -> list[str]:
    """The lines of the body of the procedure `name` in a rendered script, without comments and
    indentation. The body ends at the first line that holds only `}` and is not indented."""
    raw = script.splitlines()
    start = raw.index(f"proc {name} {{}} {{")
    end = raw.index("}", start)
    return lines("\n".join(raw[start + 1 : end]))


def body(tmp_path: Path, settings: dict[str, Any] | None = None, **kwargs: Any) -> list[str]:
    return stage(render(cts_flow(tmp_path, settings, **kwargs)))


def command(lines_: list[str], name: str) -> str:
    """The one line of the stage that starts with the command `name`."""
    (found,) = [line for line in lines_ if line.split()[0] == name]
    return found


def names(calls: list[list[str]], *wanted: str) -> list[str]:
    return [call[0] for call in calls if call[0] in wanted]


def plan(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None, request="openroad_cts"):
    """Plan `openroad_cts` on the tiny platform. `texts` are `KEY=VALUE` settings as the command
    line gives them, and `typed` gives the leaves that are no text, by dotted key."""
    store = tiny_store(tmp_path)
    given = dict(text.split("=", 1) for text in [*openroad_settings(store), *texts])
    given |= typed or {}
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        request, openroad_design(tmp_path / "design"), flow_settings=given
    )


def refusal(tmp_path: Path, *texts: str, typed: dict[str, Any] | None = None) -> str:
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, *texts, typed=typed)
    return " ".join(str(error.value).split())


# ------------------------------------------------------------------------------ the golden


def tokens(tmp_path: Path, text: str) -> str:
    """Replace the places that differ from test to test by tokens."""
    for root in sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True):
        text = text.replace(f"{root}/store", "@STORE@").replace(f"{root}/inputs", "@INPUTS@")
        text = text.replace(f"{root}/work", "@RUN@")
    return text


def test_the_defaults_render_the_golden_script(tmp_path):
    script = tokens(tmp_path, render(cts_flow(tmp_path)))
    assert script == GOLDEN.read_text()


def test_the_defaults_build_the_clock_tree_of_openroad_flow_scripts(tmp_path):
    """The commands and the flags of the default stage, line by line."""
    assert body(tmp_path) == [
        "xeda_stage cts",
        "repair_clock_inverters",
        "clock_tree_synthesis -sink_clustering_enable -repair_clock_nets",
        "estimate_parasitics -placement",
        "set_placement_padding -global -left 0 -right 0",
        "detailed_placement",
        "estimate_parasitics -placement",
        'repair_timing -setup_margin 0.0 -hold_margin 0.0 -repair_tns "100.0" -verbose',
        "detailed_placement",
        "check_placement -verbose",
        "estimate_parasitics -placement",
        "xeda_metrics 1 1",
    ]


def test_the_defaults_run_the_commands_of_the_flow_in_order(tmp_path):
    calls = run_script(cts_flow(tmp_path))
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
        "repair_clock_inverters",
        "clock_tree_synthesis",
        "estimate_parasitics",
        "set_placement_padding",
        "detailed_placement",
        "estimate_parasitics",
        "repair_timing",
        "detailed_placement",
        "check_placement",
        "estimate_parasitics",
        "write_db",
        "write_sdc",
    ]


def test_the_stage_measures_the_clock_skew_for_setup_and_hold(tmp_path):
    calls = run_script(cts_flow(tmp_path))
    assert [call for call in calls if call[0] == "report_clock_skew_metric"] == [
        ["report_clock_skew_metric"],
        ["report_clock_skew_metric", "-hold"],
    ]


# ------------------------------------------------------------------------------ the restart


def test_the_process_starts_from_the_database_and_the_sdc_file_of_the_flow_before(tmp_path):
    script = render(cts_flow(tmp_path))
    found = lines(script)
    assert f'read_db "{tmp_path / "inputs" / "place.odb"}"' in found
    assert f'read_sdc "{tmp_path / "inputs" / "place.sdc"}"' in found
    assert not [line for line in found if line.split()[0] in ("read_verilog", "link_design")]
    assert not [line for line in found if line.split()[0] == "read_lef"]
    assert [line for line in found if line.startswith("read_sdc")] == [
        f'read_sdc "{tmp_path / "inputs" / "place.sdc"}"'
    ]


def test_the_hierarchy_reaches_the_read_of_the_database(tmp_path):
    flow = cts_flow(tmp_path, {"hierarchical": True})
    assert f'read_db -hier "{tmp_path / "inputs" / "place.odb"}"' in lines(render(flow))


def test_the_wire_rc_and_the_cells_not_to_use_are_set_before_the_clock_tree(tmp_path):
    calls = run_script(cts_flow(tmp_path))
    order = [call[0] for call in calls]
    tree = order.index("clock_tree_synthesis")
    for name in ("set_layer_rc", "set_wire_rc", "set_dont_use", "read_sdc", "read_db"):
        assert max(i for i, call in enumerate(order) if call == name) < tree, name
    wire = [call for call in calls if call[:2] == ["set_wire_rc", "-clock"]]
    assert wire == [["set_wire_rc", "-clock", "-layer", "m3"]]
    (dont_use,) = [call for call in calls if call[0] == "set_dont_use"]
    assert dont_use[1:] == ["INV_X4 NAND2_X4"]


def test_the_wire_rc_of_the_settings_replaces_the_wire_rc_of_the_platform(tmp_path):
    flow = cts_flow(tmp_path, {"wire_rc": {"clock_layer": "m4"}})
    found = lines(render(flow))
    assert 'set_wire_rc -clock -layer "m4"' in found
    assert 'set_wire_rc -signal -layer "m2"' in found


def test_the_outputs_are_written_at_the_end(tmp_path):
    found = lines(render(cts_flow(tmp_path)))
    assert found[-2:] == [
        f'write_db "{tmp_path / "work" / "cts.odb"}"',
        f'write_sdc -no_timestamp "{tmp_path / "work" / "cts.sdc"}"',
    ]


def test_a_failed_stage_writes_the_failed_database_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "clock_tree_synthesis")
    flow = cts_flow(tmp_path)
    require_tclsh()
    done = run_fake_openroad(flow.run_path, render(flow), driver="openroad_cts")
    assert done.returncode != 0
    assert (flow.run_path / "openroad_cts-failed.odb").is_file()
    assert not (flow.run_path / "cts.odb").exists()


# ------------------------------------------------------------------------------ the options


def test_the_clock_inverters_are_repaired_before_the_tree_unless_switched_off(tmp_path):
    found = body(tmp_path)
    assert found.index("repair_clock_inverters") < found.index(
        command(found, "clock_tree_synthesis")
    )
    off = body(tmp_path / "off", {"repair_clock_inverters": False})
    assert "repair_clock_inverters" not in off


@pytest.mark.parametrize(
    "sink_clustering, flags",
    [
        ({"mode": "auto"}, "-sink_clustering_enable"),
        ({"mode": "off"}, ""),
        (
            {"mode": "explicit", "size": 20, "max_diameter": 50},
            '-sink_clustering_enable -sink_clustering_size "20" -sink_clustering_max_diameter "50.0"',
        ),
        ({"mode": "explicit", "size": 10}, '-sink_clustering_enable -sink_clustering_size "10"'),
        (
            {"mode": "explicit", "max_diameter": 75.5},
            '-sink_clustering_enable -sink_clustering_max_diameter "75.5"',
        ),
    ],
)
def test_the_sink_clustering_modes(tmp_path, sink_clustering, flags):
    line = command(body(tmp_path, {"sink_clustering": sink_clustering}), "clock_tree_synthesis")
    assert line == " ".join(["clock_tree_synthesis", flags, "-repair_clock_nets"]).replace(
        "  ", " "
    )


@pytest.mark.parametrize(
    "buffers, flags",
    [
        ({"select": "inferred"}, ""),
        ({"select": "list", "cells": ["BUF_A", "BUF_B"]}, '-buf_list [list "BUF_A" "BUF_B"]'),
        (
            {"select": "list", "cells": ["BUF_A", "BUF_B"], "root": "BUF_B"},
            '-buf_list [list "BUF_A" "BUF_B"] -root_buf "BUF_B"',
        ),
        ({"select": "inferred", "root": "BUF_A"}, '-root_buf "BUF_A"'),
        ({"select": "library", "library": "tiny"}, '-library "tiny"'),
    ],
)
def test_the_buffer_selection_modes(tmp_path, buffers, flags):
    line = command(body(tmp_path, {"buffers": buffers}), "clock_tree_synthesis")
    assert line == " ".join(
        ["clock_tree_synthesis", flags, "-sink_clustering_enable", "-repair_clock_nets"]
    ).replace("  ", " ")


def test_the_names_of_the_buffers_are_quoted_for_tcl(tmp_path):
    cells = ["BUF A", "BUF[1]", "$x", 'q"q']
    flow = cts_flow(tmp_path, {"buffers": {"select": "list", "cells": cells}})
    line = command(stage(render(flow)), "clock_tree_synthesis")
    assert '-buf_list [list "BUF A" "BUF\\[1\\]" "\\$x" "q\\"q"]' in line
    (tree,) = [call for call in run_script(flow) if call[0] == "clock_tree_synthesis"]
    assert tree[1] == "-buf_list"


@pytest.mark.parametrize(
    "tree, flags",
    [
        ({"style": "lut"}, ""),
        ({"style": "simple_segments", "buffer_distance": 60}, '-distance_between_buffers "60.0"'),
    ],
)
def test_the_tree_styles(tmp_path, tree, flags):
    line = command(body(tmp_path, {"tree": tree}), "clock_tree_synthesis")
    assert line == " ".join(
        ["clock_tree_synthesis", "-sink_clustering_enable", flags, "-repair_clock_nets"]
    ).replace("  ", " ")


@pytest.mark.parametrize(
    "when, in_tree, after",
    [("in_cts", True, False), ("after", False, True), ("off", False, False)],
)
def test_the_repair_of_the_clock_nets_runs_where_the_settings_say(tmp_path, when, in_tree, after):
    found = body(tmp_path, {"repair_clock_nets": {"when": when}})
    tree = found.index(command(found, "clock_tree_synthesis"))
    assert ("-repair_clock_nets" in found[tree].split()) == in_tree
    assert ("repair_clock_nets" in found) == after
    if after:
        index = found.index("repair_clock_nets")
        assert index == tree + 2 and found[index - 1] == "estimate_parasitics -placement"
        assert found[index + 1] == "estimate_parasitics -placement"


def test_the_placement_is_legalized_with_the_padding_of_the_settings(tmp_path):
    found = body(tmp_path, {"legalize": {"cell_padding": 2}})
    assert "set_placement_padding -global -left 2 -right 2" in found
    assert found.index("set_placement_padding -global -left 2 -right 2") < found.index(
        "detailed_placement"
    )


def test_the_parasitics_are_estimated_after_the_tree_and_before_the_repair(tmp_path):
    """The legalization after the tree is followed by an estimate, since the repair of the timing
    and the metrics read the parasitics. The check after the second legalization is followed by
    the last estimate."""
    found = body(tmp_path)
    tree = found.index(command(found, "clock_tree_synthesis"))
    assert found[tree + 1] == "estimate_parasitics -placement"
    first = found.index("detailed_placement")
    assert found[first + 1] == "estimate_parasitics -placement"
    assert found[first + 2].startswith("repair_timing")
    assert found[-2:] == ["estimate_parasitics -placement", "xeda_metrics 1 1"]


def test_the_repair_of_the_timing_is_followed_by_the_legalization_and_a_check(tmp_path):
    found = body(tmp_path)
    repair = found.index(command(found, "repair_timing"))
    assert found[repair - 1] == "estimate_parasitics -placement"
    assert found[repair + 1 : repair + 3] == ["detailed_placement", "check_placement -verbose"]


def test_without_the_repair_of_the_timing_the_stage_checks_the_placement_once(tmp_path):
    found = body(tmp_path, {"repair": {"enabled": False}})
    assert not [line for line in found if line.startswith("repair_timing")]
    assert found.count("detailed_placement") == 1
    assert found[-3:] == [
        "check_placement -verbose",
        "estimate_parasitics -placement",
        "xeda_metrics 1 1",
    ]


def test_the_repair_follows_its_settings(tmp_path):
    repair = {
        "fix": "hold",
        "hold_margin": 0.02,
        "match_cell_footprint": True,
    }
    line = command(body(tmp_path, {"repair": repair}), "repair_timing")
    assert line == (
        'repair_timing -hold -setup_margin 0.0 -hold_margin 0.02 -repair_tns "100.0" '
        "-match_cell_footprint -verbose"
    )
    setup = {
        "fix": "setup",
        "tns_end_percent": 40,
        "sequence": ["sizeup", "swap"],
        "last_gasp": False,
        "moves": {"pin_swap": False},
    }
    line = command(body(tmp_path / "setup", {"repair": setup}), "repair_timing")
    assert line == (
        'repair_timing -setup -setup_margin 0.0 -hold_margin 0.0 -sequence "sizeup,swap" '
        '-repair_tns "40.0" -skip_last_gasp -skip_pin_swap -verbose'
    )


@pytest.mark.parametrize("unit, setup, hold", [("1ns", "0.05", "0.02"), ("1ps", "50.0", "20.0")])
def test_the_repair_margins_are_written_in_the_time_unit_of_the_platform(
    tmp_path, unit, setup, hold
):
    repair = {"setup_margin": 0.05, "hold_margin": 0.02}
    flow = cts_flow(tmp_path, {"repair": repair}, model={"time_unit": unit})
    line = command(stage(render(flow)), "repair_timing")
    assert f"-setup_margin {setup} -hold_margin {hold}" in line


def test_the_footprint_of_the_platform_applies_unless_the_settings_say_otherwise(tmp_path):
    flow = cts_flow(tmp_path, model={"match_cell_footprint": True})
    assert "-match_cell_footprint" in command(stage(render(flow)), "repair_timing")
    flow = cts_flow(
        tmp_path / "off",
        {"repair": {"match_cell_footprint": False}},
        model={"match_cell_footprint": True},
    )
    assert "-match_cell_footprint" not in command(stage(render(flow)), "repair_timing")


@pytest.mark.parametrize(
    "settings",
    [
        {"repair_clock_inverters": False},
        {"buffers": {"select": "inferred"}},
        {"buffers": {"select": "inferred", "root": "BUF_A"}},
        {"buffers": {"select": "list", "cells": ["BUF_A"]}},
        {"buffers": {"select": "library", "library": "tiny"}},
        {"sink_clustering": {"mode": "off"}},
        {"sink_clustering": {"mode": "auto"}},
        {"sink_clustering": {"mode": "explicit", "size": 20}},
        {"sink_clustering": {"mode": "explicit", "max_diameter": 40}},
        {"tree": {"style": "lut"}},
        {"tree": {"style": "simple_segments", "buffer_distance": 80}},
        {"repair_clock_nets": {"when": "off"}},
        {"repair_clock_nets": {"when": "in_cts"}},
        {"repair_clock_nets": {"when": "after"}},
        {"legalize": {"cell_padding": 3}},
        {"repair": {"enabled": False}},
        {"repair": {"fix": "setup"}},
        {"repair": {"fix": "hold"}},
        {"repair": {"fix": "setup_and_hold"}},
    ],
    ids=lambda settings: "-".join(
        f"{group}.{leaf}={value}" if isinstance(value, dict) is False else f"{group}"
        for group, value in settings.items()
        for leaf, value in (value.items() if isinstance(value, dict) else [(group, value)])
    ),
)
def test_every_choice_of_the_clock_tree_groups_renders_and_runs(tmp_path, settings):
    calls = run_script(cts_flow(tmp_path, settings))
    assert names(calls, "clock_tree_synthesis") == ["clock_tree_synthesis"]
    assert names(calls, "write_db", "write_sdc") == ["write_db", "write_sdc"]


# ------------------------------------------------------------------------------ the templates


def test_the_driver_lists_its_stages_by_literal_names():
    driver = (TEMPLATES / "drivers" / "openroad_cts.tcl.j2").read_text()
    includes = re.findall(r"\{%\s*include\s+(.*?)\s*%\}", driver)
    assert includes and all(re.fullmatch(r'"[\w./]+"', include) for include in includes)
    for include in includes:
        assert (TEMPLATES / include.strip('"')).is_file()


def test_the_stage_template_writes_no_user_text_without_a_filter():
    text = (TEMPLATES / "stages" / "cts.tcl.j2").read_text()
    for expression in re.findall(r"\{\{(.*?)\}\}", text):
        expression = expression.strip()
        assert "|" in expression or expression.startswith("render_flags("), expression


# ------------------------------------------------------------------------------ the effective values


def test_the_effective_values_are_a_pure_function_of_the_settings_and_the_model(tmp_path):
    flow = cts_flow(tmp_path)
    settings, model = flow.settings, flow.platform_model
    before = (settings.model_dump_json(), model.model_dump_json())
    values = effective(settings, model)
    assert isinstance(values, EffectiveCts)
    assert values == effective(settings, model) == flow.effective()
    assert (settings.model_dump_json(), model.model_dump_json()) == before


def test_the_footprint_comes_from_the_platform(tmp_path):
    flow = cts_flow(tmp_path)
    assert effective(flow.settings, flow.platform_model).match_cell_footprint is False
    model = flow.platform_model.model_copy(update={"match_cell_footprint": True})
    assert effective(flow.settings, model).match_cell_footprint is True


# ------------------------------------------------------------------------------ planning


def test_the_flow_reads_the_placed_database_and_no_source():
    assert OpenroadCts.design_parts == frozenset({"rtl"})
    assert OpenroadCts.reads_sources is None


def test_the_outputs_have_the_types_of_the_clock_tree():
    from xeda.flow.io import declared_outputs

    types = {name: out.types for name, out in declared_outputs(OpenroadCts).items()}
    assert types == {
        "odb": (SourceType.OpenroadDbCts,),
        "sdc": (SourceType.OpenroadSdcCts,),
    }


def test_the_prepared_inputs_are_the_files_that_the_dry_run_locates(tmp_path):
    flow = cts_flow(tmp_path)
    assert flow.locate_prepared_inputs() == flow.implicit_inputs


def test_a_valid_set_of_settings_plans_the_placement_and_the_clock_tree(tmp_path):
    planned = plan(tmp_path, "sink_clustering.mode=explicit", "sink_clustering.size=20")
    assert [node.name for node in planned.nodes] == ["openroad_place", "openroad_cts"]


def test_a_netlist_source_of_the_design_displaces_the_synthesis(tmp_path):
    planned = plan(tmp_path)
    assert [node.name for node in planned.nodes] == ["openroad_place", "openroad_cts"]
    cts = planned.nodes[-1]
    assert [item.describe() for item in cts.inputs] == [
        "odb <- openroad_place.odb",
        "sdc <- openroad_place.sdc",
    ]


@pytest.mark.parametrize(
    "texts, expected",
    [
        (
            ["buffers.cells=BUF_A"],
            "`buffers.cells` has no effect with `buffers.select: inferred`",
        ),
        (
            ["buffers.select=list"],
            "`buffers.select: list` takes the buffers from `buffers.cells`, and that is not set",
        ),
        (
            ["buffers.library=tiny"],
            "`buffers.library` has no effect with `buffers.select: inferred`",
        ),
        (
            ["buffers.select=library"],
            "`buffers.select: library` takes the buffers from `buffers.library`",
        ),
        (
            ["sink_clustering.size=20"],
            "`sink_clustering.size` has no effect with `sink_clustering.mode: auto`",
        ),
        (
            ["sink_clustering.mode=explicit"],
            "`sink_clustering.mode: explicit` needs `sink_clustering.size`",
        ),
        (
            ["tree.buffer_distance=60"],
            "`tree.buffer_distance` has no effect with `tree.style: lut`",
        ),
        (
            ["tree.style=simple_segments"],
            "`tree.style: simple_segments` needs `tree.buffer_distance`",
        ),
        (
            ["repair.enabled=false", "repair.setup_margin=0.1"],
            "`repair.setup_margin` has no effect while `repair.enabled` is false",
        ),
        (
            ["repair.sequence=reroute"],
            "`reroute` needs the parasitics of a global route, and this call runs before global "
            "routing",
        ),
        (
            ["repair.phases=REROUTE"],
            "`REROUTE` needs the parasitics of a global route, and this call runs before global "
            "routing",
        ),
        (
            ["repair.fix=hold", "repair.last_gasp=false"],
            "`repair.last_gasp` repairs setup, and `repair.fix` is `hold`",
        ),
    ],
)
def test_the_refusals_come_at_planning_with_the_way_out(tmp_path, texts, expected):
    assert expected in refusal(tmp_path, *texts)


def test_a_root_buffer_outside_the_list_is_no_contradiction(tmp_path):
    """OpenROAD does not need the root buffer to be in the list, so xeda does not refuse it."""
    planned = plan(
        tmp_path,
        "buffers.select=list",
        "buffers.cells=BUF_A",
        "buffers.root=BUF_B",
    )
    assert planned.nodes[-1].name == "openroad_cts"


# ------------------------------------------------------------------------------ launches

LAUNCH = {"flows.openroad_place.clocks.clk.period": "5"}


def launch_settings(tmp_path: Path, **given: Any) -> tuple[Path, list[str]]:
    store = tiny_store(tmp_path)
    return store, openroad_settings(store, **{**LAUNCH, **given})


def launch(tmp_path, monkeypatch, request="openroad_cts", **given):
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


def test_a_launch_runs_the_placement_and_then_the_clock_tree(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    assert run_dir.name == "openroad_cts" and run_dir.parent.name == "counter"
    for name in ("openroad_cts.tcl", "openroad_cts.log", "metrics.json", "cts.odb", "cts.sdc"):
        assert (run_dir / name).is_file(), name
    place = run_dir.parent / "openroad_place"
    assert (place / "place.odb").is_file() and (place / "place.sdc").is_file()
    assert flow.outputs.odb == run_dir / "cts.odb"
    assert flow.outputs.sdc == run_dir / "cts.sdc"
    assert flow.inputs.odb == place / "place.odb"
    assert flow.inputs.sdc == place / "place.sdc"
    assert not (run_dir / "openroad_cts-failed.odb").exists()


def test_the_chain_of_the_two_flows_runs_the_same_way(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch, request="openroad_place+openroad_cts")
    assert flow.name == "openroad_cts" and flow.succeeded


def test_the_clock_tree_reads_the_files_of_the_placement(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    run_dir = Path(flow.run_path)
    script = (run_dir / "openroad_cts.tcl").read_text()
    place = run_dir.parent / "openroad_place"
    assert f'read_db "{place / "place.odb"}"' in script
    assert f'read_sdc "{place / "place.sdc"}"' in script
    assert [call[0] for call in fake_calls(run_dir)].count("clock_tree_synthesis") == 1


def test_a_launch_reports_the_recorded_metrics_of_the_clock_tree(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    results = json.loads((Path(flow.run_path) / "results.json").read_text())
    assert results["success"] is True
    assert results["worst_slack"] == pytest.approx(-0.156732)
    assert results["wns"] == pytest.approx(-0.156732)
    assert results["tns"] == pytest.approx(-6.82817)
    assert results["whs"] == pytest.approx(0.0808695)
    assert results["hold_tns"] == 0
    assert results["Fmax"] == pytest.approx(2458.62)
    assert results["area"] == pytest.approx(880.194)
    assert results["utilization"] == pytest.approx(0.812822)
    assert results["power"] == pytest.approx(0.00656573)
    assert results["clock_skew_setup"] == pytest.approx(0.00135155)
    assert results["clock_skew_hold"] == pytest.approx(0.00135155)
    assert results["platform"]["name"] == "tiny"


def test_the_results_are_described():
    from xeda.flow.flow import COMMON_RESULT_DESCRIPTIONS

    described = OpenroadCts.results_description
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
        "clock_skew_setup",
        "clock_skew_hold",
        "platform",
    ):
        assert described[key], key
    for key in ("wns", "tns", "whs", "worst_slack", "Fmax", "utilization", "power"):
        assert described[key] == COMMON_RESULT_DESCRIPTIONS[key]


def test_a_second_launch_is_fresh(tmp_path, monkeypatch):
    runner, design, settings, first = launch(tmp_path, monkeypatch)
    log = Path(first.run_path) / "openroad_cts.log"
    written = log.stat().st_mtime_ns
    again = runner.run("openroad_cts", design, flow_settings=settings)
    assert again is not None and again.succeeded and again.reused
    assert not first.reused
    assert log.stat().st_mtime_ns == written


def test_a_change_of_a_clock_tree_setting_runs_the_clock_tree_again_and_not_the_placement(
    tmp_path, monkeypatch
):
    runner, design, settings, _ = launch(tmp_path, monkeypatch)
    given = dict(s.split("=", 1) for s in settings) | {"sink_clustering.mode": "off"}
    _, states = runner.plan_with_freshness(
        "openroad_cts", Design.from_file(design), flow_settings=given
    )
    assert states["openroad_place"].state == "fresh"
    assert states["openroad_cts"].state != "fresh"


def test_a_missing_metrics_file_fails_the_reading_of_the_results(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    recorded = tmp_path / "metrics"
    recorded.mkdir()
    (recorded / "openroad_place.json").write_bytes((RECORDED / "openroad_place.json").read_bytes())
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(recorded))
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("openroad_cts", openroad_design(tmp_path / "design"), flow_settings=settings)
    assert flow is None or not flow.succeeded
    (run_dir,) = (tmp_path / "xeda_run").rglob("openroad_cts")
    assert not (run_dir / "trace.json").exists()


def test_a_metric_that_is_no_number_fails_the_reading_of_the_results(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    broken = tmp_path / "metrics"
    broken.mkdir()
    (broken / "openroad_place.json").write_bytes((RECORDED / "openroad_place.json").read_bytes())
    recorded = json.loads((RECORDED / "openroad_cts.json").read_text())
    recorded["cts__clock__skew__setup"] = "unknown"
    (broken / "openroad_cts.json").write_text(json.dumps(recorded))
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(broken))
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run("openroad_cts", openroad_design(tmp_path / "design"), flow_settings=settings)
    assert flow is None or not flow.succeeded


def test_a_failed_cts_stage_leaves_a_failed_database_and_no_trace(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design")
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "clock_tree_synthesis")
        failed = runner.run("openroad_cts", design, flow_settings=settings)
    assert failed is None or not failed.succeeded
    (cts_dir,) = (tmp_path / "xeda_run").rglob("openroad_cts")
    place_dir = cts_dir.parent / "openroad_place"
    assert (cts_dir / "openroad_cts-failed.odb").is_file()
    assert not (cts_dir / "trace.json").exists() and not (cts_dir / "cts.odb").exists()
    results = json.loads((cts_dir / "results.json").read_text())
    assert results["success"] is False
    assert "openroad" in results["error"]["message"] and "code 1" in results["error"]["message"]
    assert "clock_tree_synthesis" in (cts_dir / "openroad_cts.log").read_text()
    assert (place_dir / "trace.json").is_file()
    # the next launch keeps the placement and runs the clock tree
    _, states = runner.plan_with_freshness(
        "openroad_cts",
        Design.from_file(design),
        flow_settings=dict(s.split("=", 1) for s in settings),
    )
    assert states["openroad_place"].state == "fresh"
    assert states["openroad_cts"].state != "fresh"
    flow = runner.run("openroad_cts", design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    assert not (cts_dir / "openroad_cts-failed.odb").exists()
    assert (cts_dir / "trace.json").is_file() and (cts_dir / "cts.odb").is_file()


def test_the_placement_is_not_run_again_after_a_failed_clock_tree(tmp_path, monkeypatch):
    require_tclsh()
    use_fake_openroad(monkeypatch)
    _, settings = launch_settings(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = openroad_design(tmp_path / "design")
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "clock_tree_synthesis")
        runner.run("openroad_cts", design, flow_settings=settings)
    (place_dir,) = (tmp_path / "xeda_run").rglob("openroad_place")
    placed = (place_dir / "place.odb").stat().st_mtime_ns
    flow = runner.run("openroad_cts", design, flow_settings=settings)
    assert flow is not None and flow.succeeded
    assert (place_dir / "place.odb").stat().st_mtime_ns == placed


def test_a_dry_run_judges_the_files_of_the_placement(tmp_path, monkeypatch):
    runner, design_file, settings, _ = launch(tmp_path, monkeypatch)
    design = Design.from_file(design_file)
    given = dict(s.split("=", 1) for s in settings)
    _, states = runner.plan_with_freshness("openroad_cts", design, flow_settings=given)
    assert {name: state.state for name, state in states.items()} == {
        "openroad_place": "fresh",
        "openroad_cts": "fresh",
    }
    store = Path(given["pdk_root"])
    library = store / "tiny" / "tiny.lib"
    library.write_text(library.read_text() + "\n/* edited */\n")
    _, states = runner.plan_with_freshness("openroad_cts", design, flow_settings=given)
    assert states["openroad_place"].state == "stale"
    assert states["openroad_cts"].state == "stale"


def test_a_typed_netlist_chain_runs_without_synthesis(tmp_path, monkeypatch):
    _, _, _, flow = launch(tmp_path, monkeypatch)
    assert not list((tmp_path / "xeda_run").rglob("yosys"))
    assert flow.succeeded
