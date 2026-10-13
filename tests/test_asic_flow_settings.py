"""Settings of the OpenROAD and Design Compiler flows that their scripts must honor.

OpenROAD is not runnable on every machine and Design Compiler is commercial, so these tests
check the scripts the flows render (OpenROAD: under the fake ASIC tools; Design Compiler: under
the fake `dc_shell`, which runs the script with `tclsh` and records the tool's commands).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Dc, Openroad
from xeda.utils import WorkingDirectory

from .tool_utils import fake_calls, use_fake_asic_tools, use_fake_tools

needs_tclsh = pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh runs the TCL script")


# ------------------------------------------------------------------------------------ openroad


def _mac_design(root: Path) -> Design:
    (root / "mac.v").write_text(
        "module mac(input clk, input [7:0] a, output reg [7:0] q);\n"
        "  always @(posedge clk) q <= a;\nendmodule\n"
    )
    return Design(
        name="mac",
        design_root=root,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
    )


def _launch_openroad(tmp_path: Path, monkeypatch, **settings):
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.launch_flow(
        Openroad,
        _mac_design(tmp_path),
        {"platform": "nangate45", "clock": {"period": 2.0}, **settings},
    )
    assert flow.succeeded
    return flow


def test_post_pdn_tcl_is_sourced_after_the_power_grid_is_built(tmp_path, monkeypatch) -> None:
    """The script sourced `$::env(POST_PDN_TCL)`, which nothing set: OpenROAD would stop on an
    unknown variable, or the user's script never ran."""
    hook = tmp_path / "post pdn.tcl"
    hook.write_text("puts hello\n")
    flow = _launch_openroad(tmp_path, monkeypatch, post_pdn_tcl=str(hook))
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    assert "POST_PDN_TCL" not in script
    after_pdn = script.split("pdngen", 1)[1]
    assert f'source "{hook}"' in after_pdn
    assert after_pdn.index(f'source "{hook}"') < after_pdn.index("Check supply")


def test_no_post_pdn_script_is_sourced_unless_one_is_given(tmp_path, monkeypatch) -> None:
    flow = _launch_openroad(tmp_path, monkeypatch)
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    assert "POST_PDN_TCL" not in script
    assert re.search(r"pdngen\s+# Check all supply nets", script)


def test_explicit_core_and_die_areas_replace_the_core_utilization(tmp_path, monkeypatch) -> None:
    """With the default `core_utilization` the areas were ignored without a word."""
    areas = {"core_area": [10, 10, 100, 100], "die_area": [0, 0, 110, 110]}
    flow = _launch_openroad(tmp_path, monkeypatch, **areas)
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    assert "initialize_floorplan -die_area {0 0 110 110}" in script
    assert "-core_area {10 10 100 100}" in script
    assert "-utilization" not in script


def test_core_utilization_sizes_the_floorplan_when_no_areas_are_given(
    tmp_path, monkeypatch
) -> None:
    flow = _launch_openroad(tmp_path, monkeypatch, core_utilization=55)
    script = (flow.run_path / "orflow_0_11.tcl").read_text()
    assert "initialize_floorplan -utilization 55.0" in script
    assert "-die_area" not in script


@pytest.mark.parametrize(
    "given, missing",
    [
        ({"core_area": [10, 10, 100, 100]}, "die_area"),
        ({"die_area": [0, 0, 110, 110]}, "core_area"),
    ],
)
def test_one_area_without_the_other_is_refused_at_planning(given, missing) -> None:
    settings = Openroad.Settings(platform="nangate45", **given)
    with pytest.raises(FlowSettingsError, match=f"`{missing}`"):
        Openroad.check_settings_supported(settings)


def test_both_areas_or_neither_are_accepted() -> None:
    both = {"core_area": [10, 10, 100, 100], "die_area": [0, 0, 110, 110]}
    for given in (both, {}):
        Openroad.check_settings_supported(Openroad.Settings(platform="nangate45", **given))


def _sdc_delays(flow) -> dict[str, str]:
    """The delay value that `clocks.sdc` gives to `set_input_delay` and `set_output_delay`."""
    delays = {}
    for line in (flow.run_path / "clocks.sdc").read_text().splitlines():
        words = line.split()
        if words and words[0] in ("set_input_delay", "set_output_delay"):
            delays[words[0]] = words[1]
    return delays


def test_delays_in_picoseconds_are_converted_to_the_time_unit_of_the_platform(
    tmp_path, monkeypatch
) -> None:
    """`input_delay` and `output_delay` are in picoseconds. nangate45's time unit is 1ns, and the
    constraints wrote the number as it was: 100 ps became 100 ns."""
    flow = _launch_openroad(tmp_path, monkeypatch, input_delay=100, output_delay="0.25ns")
    assert flow.settings.platform.time_unit == "1ns"
    assert _sdc_delays(flow) == {"set_input_delay": "0.100", "set_output_delay": "0.250"}


@pytest.mark.parametrize(
    "platform, unit, per_picosecond",
    [("asap7", "1ps", 1.0), ("nangate45", "1ns", 0.001), ("sky130hd", "1ns", 0.001)],
)
def test_the_conversion_follows_the_time_unit_of_each_bundled_platform(
    platform, unit, per_picosecond
) -> None:
    settings = Openroad.Settings(platform=platform)
    assert settings.platform is not None and settings.platform.time_unit == unit
    assert settings.delay_in_platform_unit(200) == pytest.approx(200 * per_picosecond)


# ------------------------------------------------------------------------- design compiler


def _dc_design(root: Path) -> Design:
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.v").write_text(
        "module top(input clk, input a, input b, output y, output z);\n"
        "  assign y = a & b; assign z = a | b;\nendmodule\n"
    )
    return Design(
        name="dcdesign",
        design_root=root,
        rtl={"sources": ["rtl/top.v"], "top": "top", "clock": {"port": "clk"}},
    )


def _run_dc(tmp_path: Path, monkeypatch, **settings):
    use_fake_tools(monkeypatch)
    lib = tmp_path / "pdk" / "cells.db"
    lib.parent.mkdir()
    lib.write_text("")
    given = {"target_libraries": [str(lib)], "clock": {"period": 5.0}, **settings}
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(Dc.Settings.from_input(given), _dc_design(tmp_path / "design"), run_path)
    flow.init()
    with WorkingDirectory(run_path):
        flow.run()
    return flow, run_path


def _app_vars(run_path: Path) -> dict[str, str]:
    return {c[1]: c[2] for c in fake_calls(run_path) if c[:1] == ["set_app_var"]}


@needs_tclsh
def test_dc_names_the_upf_variable_in_the_table_that_holds_it(tmp_path, monkeypatch) -> None:
    """`hdlin_enable_upf_compatible_naming` is an `hdlin` variable. The check looked in the
    `compile` table and so replaced the user's value in `hdlin` with `true`."""
    _, run_path = _run_dc(tmp_path, monkeypatch)
    assert _app_vars(run_path)["hdlin_enable_upf_compatible_naming"] == "true"


@needs_tclsh
def test_dc_keeps_the_upf_naming_the_user_chose(tmp_path, monkeypatch) -> None:
    flow, run_path = _run_dc(tmp_path, monkeypatch, hdlin={"enable_upf_compatible_naming": "false"})
    assert flow.settings.hdlin["enable_upf_compatible_naming"] == "false"
    assert _app_vars(run_path)["hdlin_enable_upf_compatible_naming"] == "false"


@needs_tclsh
def test_dc_constrains_each_port_once_and_names_it(tmp_path, monkeypatch) -> None:
    """Run the generated `constraints.sdc` with stand-ins for the collection commands (the fake
    `dc_shell` records a `foreach_in_collection` and does not run its body). Each port gets its own
    `set_input_delay` or `set_output_delay`, and each message names the port it is about."""
    _, run_path = _run_dc(
        tmp_path, monkeypatch, default_max_input_delay=1.5, default_max_output_delay=2.5
    )
    prelude = """
proc all_inputs {args} { return {a b} }
proc all_outputs {args} { return {y z} }
proc get_ports {args} { return $args }
proc get_object_name {port} { return $port }
proc foreach_in_collection {var collection body} {
    upvar 1 $var port
    foreach port $collection { uplevel 1 $body }
}
foreach command {create_clock set_input_delay set_output_delay} {
    proc $command {args} "puts \\"$command \\$args\\""
}
source constraints.sdc
"""
    (run_path / "stand_ins.tcl").write_text(prelude)
    out = subprocess.run(
        [shutil.which("tclsh"), "stand_ins.tcl"],
        cwd=run_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert [line for line in out if line.startswith("set_")] == [
        "set_input_delay -clock main_clock -max 1.5 a",
        "set_input_delay -clock main_clock -max 1.5 b",
        "set_output_delay -clock main_clock -max 2.5 y",
        "set_output_delay -clock main_clock -max 2.5 z",
    ]
    assert [line for line in out if line.startswith("Settings")] == [
        "Settings MAX input delay of port a to 1.5",
        "Settings MAX input delay of port b to 1.5",
        "Settings MAX output delay of port y to 2.5",
        "Settings MAX output delay of port z to 2.5",
    ]


@needs_tclsh
def test_dc_sets_the_compile_variables_it_is_given(tmp_path, monkeypatch) -> None:
    """Nothing rendered the `compile` table, so a value in it did nothing."""
    _, run_path = _run_dc(tmp_path, monkeypatch, compile={"seqmap_honor_sync_set_reset": "true"})
    calls = [c for c in fake_calls(run_path) if c[:1] == ["set_app_var"]]
    assert ["set_app_var", "compile_seqmap_honor_sync_set_reset", "true"] in calls
    names = [c[1] for c in calls]
    assert names.index("hdlin_infer_mux") < names.index("compile_seqmap_honor_sync_set_reset")


@needs_tclsh
def test_dc_sets_no_compile_variable_unless_asked(tmp_path, monkeypatch) -> None:
    _, run_path = _run_dc(tmp_path, monkeypatch)
    names = _app_vars(run_path)
    assert "compile_seqmap_honor_sync_set_reset" not in names
    assert "compile_optimize_unloaded_seq_logic_with_no_bound_opt" not in names


@needs_tclsh
def test_dc_sets_hdlin_infer_multibit_once_from_the_setting(tmp_path, monkeypatch) -> None:
    """The script set `hdlin_infer_multibit default_all` after the `hdlin` loop, so the
    `infer_multibit` setting had no effect."""
    _, run_path = _run_dc(tmp_path, monkeypatch)
    sets = [c for c in fake_calls(run_path) if c[:2] == ["set_app_var", "hdlin_infer_multibit"]]
    assert sets == [["set_app_var", "hdlin_infer_multibit", "default_all"]]


@needs_tclsh
def test_dc_keeps_the_infer_multibit_the_user_chose(tmp_path, monkeypatch) -> None:
    _, run_path = _run_dc(tmp_path, monkeypatch, infer_multibit="never")
    sets = [c for c in fake_calls(run_path) if c[:2] == ["set_app_var", "hdlin_infer_multibit"]]
    assert sets == [["set_app_var", "hdlin_infer_multibit", "never"]]


@pytest.mark.parametrize(
    "table, key, setting",
    [
        ("hdlin", "infer_multibit", "infer_multibit"),
        ("hdlin", "vhdl_preserve_case", "vhdl_preserve_case"),
    ],
)
def test_a_variable_that_has_a_setting_is_refused_in_the_table_of_variables(
    table, key, setting
) -> None:
    """`hdlin: {infer_multibit: x}` and `infer_multibit: x` are two spellings of one variable."""
    settings = Dc.Settings(**{table: {key: "true"}})
    with pytest.raises(FlowSettingsError, match=rf"`{table}\.{key}`.*`{setting}`"):
        Dc.check_settings_supported(settings)


def test_every_variable_with_a_setting_of_its_own_is_in_the_table() -> None:
    """The settings whose value `init()` writes into `hdlin` or `compile`."""
    assert Dc.Settings.variable_settings == {
        ("hdlin", "infer_multibit"): "infer_multibit",
        ("hdlin", "vhdl_preserve_case"): "vhdl_preserve_case",
    }
    for setting in Dc.Settings.variable_settings.values():
        assert setting in Dc.Settings.model_fields


def test_the_other_variables_stay_settable_in_their_tables() -> None:
    settings = Dc.Settings(
        hdlin={"enable_upf_compatible_naming": "false", "vhdl_std": "2008"},
        compile={"seqmap_honor_sync_set_reset": "true"},
    )
    Dc.check_settings_supported(settings)
