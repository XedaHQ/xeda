"""Settings of the Design Compiler flow that its script must honor.

Design Compiler is commercial, so these tests check the script the flow renders, under the fake
`dc_shell`, which runs the script with `tclsh` and records the tool's commands.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flows import Dc

from .tool_utils import fake_calls, use_fake_tools

needs_tclsh = pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh runs the TCL script")


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
    given = {"target_libraries": [str(lib)], "clock": {"period": "5 ns"}, **settings}
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(Dc.Settings.from_input(given), _dc_design(tmp_path / "design"), run_path)
    flow.init()
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


PRESETS = {
    "area": ("compile_high_effort_area", "compile_optimize_netlist_area"),
    "speed": ("compile_timing_high_effort",),
    "power": (),
    "none": (),
}


@needs_tclsh
@pytest.mark.parametrize("optimization", PRESETS)
def test_dc_applies_the_preset_of_the_optimization_goal(
    optimization, tmp_path, monkeypatch
) -> None:
    _, run_path = _run_dc(tmp_path, monkeypatch, optimization=optimization)
    values = _app_vars(run_path)
    every = {name for names in PRESETS.values() for name in names}
    assert {name: values[name] for name in every if name in values} == {
        name: "true" for name in PRESETS[optimization]
    }


@needs_tclsh
@pytest.mark.parametrize("optimization", ["area", "speed"])
def test_a_compile_key_overrides_the_preset_of_the_optimization_goal(
    optimization, tmp_path, monkeypatch
) -> None:
    """The preset set the variable after the `compile` table, so the user's value was lost."""
    names = PRESETS[optimization]
    _, run_path = _run_dc(
        tmp_path,
        monkeypatch,
        optimization=optimization,
        compile={names[0].removeprefix("compile_"): "false"},
    )
    values = _app_vars(run_path)
    assert values[names[0]] == "false"
    assert all(values[name] == "true" for name in names[1:])
