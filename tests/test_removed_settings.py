"""A removed setting is an error that names what replaced it, for every flow."""

import re
from typing import get_args

import pytest
from pydantic import BaseModel

from xeda.flow import Flow, FlowSettingsError, registered_flows
from xeda.flow.flow import removed_message
from xeda.flows import Openroad

FLOWS = sorted({cls for _, cls in registered_flows.values()}, key=lambda c: c.name)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_clean_is_no_longer_a_flow_setting(flow, tmp_path):
    with pytest.raises(FlowSettingsError, match="--clean"):
        flow.Settings.from_input({"clean": True}, design_root=tmp_path, runner_cwd=tmp_path)


def test_verilator_clean_before_run_names_the_option(tmp_path):
    verilator = registered_flows["verilator"][1]
    with pytest.raises(FlowSettingsError, match="--clean"):
        verilator.Settings.from_input(
            {"clean_before_run": True}, design_root=tmp_path, runner_cwd=tmp_path
        )


@pytest.mark.parametrize("flow_name", ["ghdl_sim", "ghdl_synth"])
def test_ghdl_clean_names_both_replacements(flow_name, tmp_path):
    """GHDL's old `clean` meant "run `ghdl remove` before analysis", not "empty the run
    directory": the error must point at `clean_before_analyze` as well as `--clean`, so a user
    who set `clean=False` to keep GHDL's incremental analysis is not steered into the option that
    does the opposite."""
    flow = registered_flows[flow_name][1]
    with pytest.raises(FlowSettingsError, match="clean_before_analyze") as exc_info:
        flow.Settings.from_input({"clean": False}, design_root=tmp_path, runner_cwd=tmp_path)
    assert "--clean" in str(exc_info.value)


@pytest.mark.parametrize("flow", FLOWS, ids=lambda c: c.name)
def test_no_flow_overrides_clean(flow):
    assert "clean" not in vars(flow), f"{flow.name} still defines clean()"


@pytest.mark.parametrize("kind", ["lpf", "pcf", "pdc"])
def test_nextpnr_pin_settings_name_typed_source_replacement(kind, tmp_path):
    flow = registered_flows["nextpnr"][1]
    with pytest.raises(FlowSettingsError) as exc:
        flow.Settings.from_input({f"{kind}_cfg": f"pins.{kind}"}, design_root=tmp_path)
    message = str(exc.value)
    assert "was removed" in message and "rtl.sources" in message
    assert f"{{file: pins.{kind}, type: {kind.capitalize()}}}" in message


# ------------------------------------------------------------------------------ a removed flow

REMOVED_OPEN_XC7 = "`open_xc7` was removed: use fpga_pack to build, openfpgaloader to program"


@pytest.mark.parametrize(
    "name", ["open_xc7", "openxc7", "OpenXC7", "open-xc7", "OPEN_XC7", "OpenXc7", " open_xc7 "]
)
def test_open_xc7_was_removed_and_names_what_replaced_it(name):
    from xeda.flow_runner import FlowNotFoundError, get_flow_class

    with pytest.raises(FlowNotFoundError) as error:
        get_flow_class(name)
    assert str(error.value) == REMOVED_OPEN_XC7


def test_no_open_xc7_flow_module_alias_or_tombstone_is_left():
    import importlib

    import xeda.flows
    from xeda.introspect import flows_info

    assert not [name for name in registered_flows if "xc7" in name.lower()]
    assert not [name for name in dir(xeda.flows) if "xc7" in name.lower()]
    assert not [flow["name"] for flow in flows_info() if "xc7" in flow["name"]]
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("xeda.flows.openxc7")


def test_launching_open_xc7_through_the_api_says_it_was_removed(tmp_path):
    from xeda import Design
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for launch in (runner.run, runner.plan):
        with pytest.raises(
            FlowNotFoundError, match="fpga_pack to build, openfpgaloader to program"
        ):
            launch("open_xc7", design)
    assert not (tmp_path / "run").exists()


# ------------------------------------------------- a removed flow's section in a `flows` table

_COUNTER = "module d(input wire clk, output reg q);\n  always @(posedge clk) q <= ~q;\nendmodule\n"


def _design_with_sections(tmp_path, sections):
    from xeda import Design

    (tmp_path / "d.v").write_text(_COUNTER)
    sections = {"yosys_fpga": {"fpga": {"part": "xc7a100tcsg324-1"}}, **sections}
    return Design(
        name="d", design_root=tmp_path, rtl={"sources": ["d.v"], "top": "d"}, flows=sections
    )


@pytest.mark.parametrize("name", ["open_xc7", "openxc7", "OpenXC7", "open-xc7"])
def test_a_design_s_open_xc7_section_is_an_error_naming_the_replacement(name, tmp_path):
    """A removed flow is not an unknown plugin: its section configures nothing any more, and
    saying nothing would leave the design's settings silently unused."""
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = _design_with_sections(tmp_path, {name: {"fpga": {"part": "xc7a100tcsg324-1"}}})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for launch in (runner.run, runner.plan):
        with pytest.raises(FlowNotFoundError) as error:
            launch("yosys_fpga", design)
        assert str(error.value) == REMOVED_OPEN_XC7
    assert not (tmp_path / "run").exists()


def test_a_project_s_open_xc7_section_is_an_error_naming_the_replacement(tmp_path):
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    (tmp_path / "d.v").write_text(_COUNTER)
    project = tmp_path / "xedaproject.yaml"
    project.write_text(
        "designs:\n"
        "  - name: d\n"
        "    rtl: {sources: [d.v], top: d}\n"
        "flows:\n"
        "  yosys_fpga:\n"
        "    fpga: {part: xc7a100tcsg324-1}\n"
        "  openxc7:\n"
        "    fpga: {part: xc7a100tcsg324-1}\n"
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowNotFoundError) as error:
        runner.run("yosys_fpga", xedaproject=str(project), select_design_in_project="d")
    assert str(error.value) == REMOVED_OPEN_XC7
    assert not (tmp_path / "run").exists()


def test_an_open_xc7_section_given_as_a_setting_is_an_error_naming_the_replacement(tmp_path):
    from xeda.flow_runner import DefaultRunner, FlowNotFoundError

    design = _design_with_sections(tmp_path, {})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowNotFoundError) as error:
        runner.run("yosys_fpga", design, flow_settings=["flows.open_xc7.seed=1"])
    assert str(error.value) == REMOVED_OPEN_XC7
    with pytest.raises(FlowNotFoundError) as error:
        runner.run_flow("yosys_fpga", design, all_flows_settings={"open_xc7": {"seed": 1}})
    assert str(error.value) == REMOVED_OPEN_XC7


def test_the_command_line_reports_a_design_file_s_open_xc7_section(tmp_path):
    import json
    import subprocess
    import sys

    (tmp_path / "d.v").write_text(_COUNTER)
    design_file = tmp_path / "d.yaml"
    design_file.write_text(
        "name: d\nrtl: {sources: [d.v], top: d}\nflows:\n"
        "  yosys_fpga: {fpga: {part: xc7a100tcsg324-1}}\n  open_xc7: {seed: 1}\n"
    )
    for extra in ([], ["--dry-run"]):
        proc = subprocess.run(
            [sys.executable, "-m", "xeda", "run", "yosys_fpga", str(design_file), "--json"]
            + extra
            + ["--run-root", str(tmp_path / "run")],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        document = json.loads(proc.stdout)
        assert document["outcome"] is None
        error = document["error"]
        assert error["code"] == "flow_not_found" and REMOVED_OPEN_XC7 in error["message"]


def test_a_section_of_a_flow_that_is_not_installed_is_still_tolerated(tmp_path):
    """A plugin flow that is not installed here: its section is nobody's settings."""
    from xeda.flow_runner import DefaultRunner

    design = _design_with_sections(tmp_path, {"some_plugin_flow": {"anything": 1}})
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan("yosys_fpga", design)
    assert plan is not None


#: A `flows` entry for the removed flow, in any of the formats an example may be written in:
#: the name in any spelling it had, optionally quoted, then what ends a key, a table header or a
#: dotted path (`open_xc7:`, `"open-xc7": {`, `[flows.open_xc7]`, `flows.OpenXC7.seed = 1`).
OPEN_XC7_ENTRY = re.compile(r"""open[_-]?xc7["']?\s*[:=\].]""", re.IGNORECASE)


@pytest.mark.parametrize(
    "text, found",
    [
        ("[flows.open_xc7]\nseed = 1\n", True),
        ('[flows."open-xc7"]\n', True),
        ("flows:\n  open_xc7:\n    seed: 1\n", True),
        ("flows:\n  'openxc7': {seed: 1}\n", True),
        ('{"flows": {"OpenXC7": {"seed": 1}}}', True),
        ("flows.open_xc7.seed = 1\n", True),
        ("open_xc7 = {}\n", True),
        ("[flows.nextpnr]\nfpga.part = 'xc7a100tcsg324-1'\n", False),
        ("flows:\n  nextpnr:\n    fpga: {part: xc7a35tcsg324-1}\n", False),
        ("# the open_xc7 flow was removed\n", False),
    ],
)
def test_the_open_xc7_pattern_sees_every_spelling_and_spares_the_rest(text, found):
    assert bool(OPEN_XC7_ENTRY.search(text)) is found


def test_no_example_keeps_an_open_xc7_section():
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples"
    kept = [
        str(path)
        for suffix in ("toml", "yaml", "yml", "json")
        for path in examples.rglob(f"*.{suffix}")
        if OPEN_XC7_ENTRY.search(path.read_text())
    ]
    assert not kept


@pytest.mark.parametrize("name", ["open_xc7", "OpenXC7", "openxc7"])
def test_scrub_still_removes_a_removed_flow_s_run_directories(name, tmp_path):
    """The flow is gone, the directories its runs left are not: `xeda scrub` only removes
    directories, so it takes a removed flow's name, in any spelling the flow had."""
    import json
    import subprocess
    import sys

    from xeda.run_root import ensure_run_root

    root = ensure_run_root(tmp_path / "run")
    for directory in ("open_xc7", "open_xc7_0123456789abcdef", "nextpnr"):
        (root / "blinky" / directory).mkdir(parents=True)
        (root / "blinky" / directory / "results.json").write_text("{}\n")
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "scrub", name, "blinky", "--run-root", str(root), "--json"],
        capture_output=True,
        text=True,
        input="yes\n",
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout)
    assert document["success"] is True and document["flow"] == "open_xc7"
    assert sorted(p.name for p in (root / "blinky").iterdir() if p.is_dir()) == ["nextpnr"]


def test_a_removed_flow_named_on_the_command_line_as_a_section_says_so(tmp_path):
    import json
    import subprocess
    import sys

    (tmp_path / "d.v").write_text(_COUNTER)
    design_file = tmp_path / "d.yaml"
    design_file.write_text("name: d\nrtl: {sources: [d.v], top: d}\n")
    for spelling in ("open_xc7", "openxc7", "OpenXC7"):
        proc = subprocess.run(
            [sys.executable, "-m", "xeda", "run", "yosys_fpga", str(design_file), "--json"]
            + ["--dry-run", "-s", "fpga.part=xc7a100tcsg324-1", f"flows.{spelling}.seed=1"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        document = json.loads(proc.stdout)
        assert document["outcome"] is None
        error = document["error"]
        assert error["code"] == "flow_not_found" and REMOVED_OPEN_XC7 in error["message"]


# ------------------------------------------------------------------------ `clock_period`

CLOCK_PERIOD_REMOVED = "`clock_period` was removed: use `clock.period` or `clock.freq`"


def _synth_flows():
    from xeda.flow.synth import SynthFlow

    return [flow for flow in FLOWS if issubclass(flow, SynthFlow)]


@pytest.mark.parametrize("flow", _synth_flows(), ids=lambda c: c.name)
def test_every_synthesis_flow_refuses_clock_period_and_names_the_clock(flow, tmp_path):
    with pytest.raises(FlowSettingsError, match=re.escape(CLOCK_PERIOD_REMOVED)):
        flow.Settings.from_input({"clock_period": 5.0}, design_root=tmp_path, runner_cwd=tmp_path)


@pytest.mark.parametrize("origin", ["design", "target", "project", "cli", "flows-cli", "api"])
def test_clock_period_was_removed_at_every_origin(tmp_path, monkeypatch, origin):
    import json

    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    fpga = {"part": "xc7a100tcsg324-1"}
    data: dict = {"name": "top", "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"}}
    if origin == "design":
        data["flows"] = {"vivado_synth": {"fpga": fpga, "clock_period": 5.0}}
    elif origin == "target":
        data["targets"] = {"t": {"flows": {"vivado_synth": {"fpga": fpga, "clock_period": 5.0}}}}
    design_file = tmp_path / "top.json"
    design_file.write_text(json.dumps(data))
    design = Design.from_file(design_file, target="t" if origin == "target" else None)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    base = ["fpga.part=xc7a100tcsg324-1"]
    with pytest.raises(Exception, match=re.escape(CLOCK_PERIOD_REMOVED)):
        if origin == "project":
            project = tmp_path / "project.yaml"
            project.write_text("flows:\n  vivado_synth:\n    clock_period: 5.0\n")
            runner.plan("vivado_synth", design, xedaproject=str(project), flow_settings=base)
        elif origin == "cli":
            runner.plan("vivado_synth", design, flow_settings=[*base, "clock_period=5.0"])
        elif origin == "flows-cli":
            runner.plan(
                "vivado_synth", design, flow_settings=[*base, "flows.vivado_synth.clock_period=5.0"]
            )
        elif origin == "api":
            runner.plan("vivado_synth", design, flow_settings={"fpga": fpga, "clock_period": 5.0})
        else:
            runner.plan("vivado_synth", design, flow_settings=base)
    assert not (tmp_path / "run").exists()


# ----------------------------------------------------------------------- the old `openroad`

#: Every field of the settings class of the old `openroad` flow, in its order. The new `openroad`
#: keeps `platform`, `corner`, `dont_use_cells` and `save_images`; the others are removed.
OLD_OPENROAD_FIELDS = (
    "platform",
    "corner",
    "multi_corner",
    "input_delay_frac",
    "output_delay_frac",
    "input_delay",
    "output_delay",
    "core_utilization",
    "core_aspect_ratio",
    "core_margin",
    "core_area",
    "die_area",
    "sdc_files",
    "io_constraints",
    "results_dir",
    "write_metrics",
    "exit",
    "gui",
    "copy_platform_files",
    "extra_liberty_files",
    "floorplan_def",
    "floorplan_tcl",
    "resynth_for_timing",
    "resynth_for_area",
    "rtlmp_flow",
    "io_place_random",
    "place_density",
    "place_density_lb_addon",
    "dont_use_cells",
    "place_pins_args",
    "global_placement_args",
    "min_phi_coef",
    "max_phi_coef",
    "congestion_iterations",
    "global_routing_layer_adjustment",
    "repair_antennas",
    "update_sdc_margin",
    "detailed_route_or_seed",
    "detailed_route_or_k",
    "db_process_node",
    "detailed_route_additional_args",
    "post_detailed_route_tcl",
    "gpl_routability_driven",
    "gpl_timing_driven",
    "macro_placement_file",
    "post_pdn_tcl",
    "make_tracks_tcl",
    "footprint",
    "footprint_def",
    "footprint_tcl",
    "gds_seal_file",
    "sig_map_file",
    "density_fill",
    "rtlmp_config_file",
    "macro_wrappers",
    "cdl_masters_file",
    "hold_slack_margin",
    "setup_slack_margin",
    "cts_buf_distance",
    "cts_cluster_size",
    "cts_cluster_diameter",
    "tie_separation",
    "final_irdrop_analysis",
    "disable_via_gen",
    "repair_pdn_via_layer",
    "write_cdl",
    "save_images",
    "generate_gds",
)

#: Entries of the old `openroad` class for the settings that moved to `yosys`. They are not
#: fields of its own settings class, but the old flow refused them with a message that names
#: `yosys`, so the new flow keeps that message.
OLD_OPENROAD_YOSYS_ENTRIES = ("blocks", "optimize", "abc_driver_cell", "abc_load_in_ff")

OPENROAD_FLOW = Openroad
#: The removed settings of `openroad` that this flow refuses, in the order of the old class.
OPENROAD_REMOVED_NAMES = tuple(
    name
    for name in (*OLD_OPENROAD_FIELDS, *OLD_OPENROAD_YOSYS_ENTRIES)
    if name not in OPENROAD_FLOW.Settings.model_fields
)
#: A reference to a setting of another flow or of this flow, written in backticks.
FLOW_PATH = re.compile(r"`flows\.(\w+)((?:\.\w+)+)`")


def _model_in(annotation):
    """Return the settings model in a type annotation, or None when it has none."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        model = _model_in(argument)
        if model is not None:
            return model
    return None


def _setting_exists(model, dotted: str) -> bool:
    """Return True when the dotted name is a field of the settings model, or of the group it
    names."""
    current = model
    parts = dotted.split(".")
    for position, part in enumerate(parts):
        field = current.model_fields.get(part)
        if field is None:
            return False
        if position < len(parts) - 1:
            current = _model_in(field.annotation)
            if current is None:
                return False
    return True


def test_openroad_removed_settings_are_the_old_fields_it_lost_and_the_yosys_entries():
    own = set(vars(OPENROAD_FLOW.Settings).get("removed_settings", {}))
    own -= set(Flow.Settings.removed_setting_replacements())
    lost = {name for name in OLD_OPENROAD_FIELDS if name not in OPENROAD_FLOW.Settings.model_fields}
    assert own == lost | set(OLD_OPENROAD_YOSYS_ENTRIES)


@pytest.mark.parametrize("name", OPENROAD_REMOVED_NAMES)
def test_openroad_refuses_an_old_setting_and_names_its_replacement(name, tmp_path):
    replacement = OPENROAD_FLOW.Settings.removed_setting_replacements()[name]
    with pytest.raises(FlowSettingsError) as refused:
        OPENROAD_FLOW.Settings.from_input({name: True}, design_root=tmp_path, runner_cwd=tmp_path)
    assert removed_message(name, replacement, True) in str(refused.value)


def test_openroad_replacements_name_settings_that_exist():
    missing = []
    for name in OPENROAD_REMOVED_NAMES:
        replacement = OPENROAD_FLOW.Settings.removed_setting_replacements()[name]
        for flow, dotted in FLOW_PATH.findall(replacement):
            model = registered_flows[flow][1].Settings
            if not _setting_exists(model, dotted[1:]):
                missing.append(f"{name}: flows.{flow}{dotted}")
    assert not missing


def test_openroad_input_delay_names_the_time_with_its_unit(tmp_path):
    with pytest.raises(FlowSettingsError) as refused:
        OPENROAD_FLOW.Settings.from_input({"input_delay": 100}, design_root=tmp_path)
    assert (
        "`input_delay` was removed: use `flows.openroad_place.input_delay`, a time with its unit, "
        "such as `100 ps`"
    ) in str(refused.value)


def test_openroad_core_utilization_names_the_percent_setting(tmp_path):
    with pytest.raises(FlowSettingsError) as refused:
        OPENROAD_FLOW.Settings.from_input({"core_utilization": 40.0}, design_root=tmp_path)
    assert (
        "`core_utilization` was removed: use "
        "`flows.openroad_place.floorplan.core_utilization_percent`"
    ) in str(refused.value)
