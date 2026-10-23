"""`yosys` owns its ASIC configuration.

The whole configuration of a standard-cell synthesis -- the liberty files, the mapping cells, the
abc script and constraints, the netlist format -- follows from a platform:
`xeda run yosys -s platform=<name>` alone maps to the cells of that platform and needs nothing
else. `abc.strategy`, `abc.driver_cell` and `abc.load` are settings of `yosys`, which acts on
them. These tests use the `tiny` platform (`tests/tiny_platform.py`): its files are in a store
under the test directory, so no PDK needs to be installed. `tests/test_yosys_platform.py` has the
tests of the platform model itself.

The script maps in the order of OpenROAD-flow-scripts. It reads every Liberty file as it is and
hands the cells that the flow must not use to `dfflibmap` and `abc` as `-dont_use` words. It
writes no patched or merged Liberty file.
"""

import contextlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, get_args

import pytest

from xeda import Design
from xeda.dataclass import ValidationError
from xeda.design import SourceType
from xeda.flow import FlowException, FlowSettingsError, FlowSettingsException
from xeda.flow.io import declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga
from xeda.flows import yosys as yosys_flows
from xeda.flows.yosys.asic_groups import AbcGroup
from xeda.tool import ExecutableNotFound

from .flow_registry import preserved_flow_registry
from .tiny_platform import TINY, TINY_RESIDUE, tiny_settings, tiny_store
from .test_yosys import _yosys_has_tcl
from .test_yosys_templates import TCL_HARNESS
from .tool_utils import require_tclsh, require_yosys
from .tool_utils import HANG_GUARD

MAC = (
    "module mul8(input [7:0] a, b, output [15:0] p);\n"
    "  assign p = a * b;\n"
    "endmodule\n"
    "module mac(input clk, input rst, input [7:0] a, b, output reg [19:0] q);\n"
    "  wire [15:0] p;\n"
    "  mul8 m(.a(a), .b(b), .p(p));\n"
    "  always @(posedge clk) if (rst) q <= 0; else q <= q + p;\n"
    "endmodule\n"
)


def _design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mac.v").write_text(MAC)
    return Design(
        name="mac",
        design_root=root,
        rtl={"sources": ["mac.v"], "top": "mac", "clock": {"port": "clk"}},
    )


def _scripted_launch(tmp_path: Path, monkeypatch, flow, settings, sections=None) -> Path:
    """Launch `flow` with every tool command recorded instead of run, and return the run
    directory of its `yosys` (itself, or its dependency): what yosys was configured with and
    handed is written there before any tool runs. The run fails once yosys, having produced
    nothing, is asked for its results."""

    def record(executable, args=None, **kwargs):
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            flow, _design(tmp_path / "design"), settings, all_flows_settings=sections
        )
    (settings_json,) = (tmp_path / "xeda_run").glob("mac/yosys/settings.json")
    return settings_json.parent


def _script(run_dir: Path, script_format: str = "ys") -> str:
    return (run_dir / f"yosys_synth.{script_format}").read_text()


def _commands(run_dir: Path, script_format: str, tmp_path: Path) -> list[list[str]]:
    """The commands yosys is handed by the script, each as its list of words: `log` and `echo`
    lines left out. A `.ys` script is split at whitespace, as yosys splits it. A `.tcl` script
    runs under `tclsh` with every command recording itself, which gives the words after Tcl
    has read them."""
    text = _script(run_dir, script_format)
    if script_format == "ys":
        calls = [
            line.split()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        require_tclsh()
        harness = tmp_path / "harness.tcl"
        harness.write_text(TCL_HARNESS)
        result = subprocess.run(
            ["tclsh", str(harness), str(run_dir / "yosys_synth.tcl")],
            capture_output=True,
            text=True,
            timeout=HANG_GUARD,
        )
        assert result.returncode == 0, f"{result.stderr}\n{text}"
        calls = [
            line.split("\x1f")[1:]
            for line in result.stdout.splitlines()
            if line.startswith("CALL\x1f")
        ]
    return [call for call in calls if call[0] not in ("log", "echo", "-import")]


def _words(commands: list[list[str]], first: str) -> list[list[str]]:
    """The commands that start with `first`."""
    return [call for call in commands if call[0] == first]


def _effective(run_dir: Path) -> dict:
    return json.loads((run_dir / "settings.json").read_text())["effective_flow_settings"]


def _tiny(tmp_path: Path, **given: Any) -> dict[str, Any]:
    """Settings that select the `tiny` platform, in a store under `tmp_path`, and a 2 ns clock."""
    return tiny_settings(tiny_store(tmp_path), clock={"period": "2 ns"}, **given)


# ------------------------------------------------------------------------------ the output


def test_yosys_declares_its_verilog_netlist():
    """The gate-level netlist a place-and-route flow reads is a declared output, switched on by
    the setting
    that names it, whose default writes it."""
    netlist = declared_outputs(Yosys)["netlist"]
    assert netlist.types == (SourceType.VerilogNetlist,)
    assert netlist.enabled_by == "netlist_verilog"
    assert Yosys.Settings().netlist_verilog == Path("netlist.v")


def test_stop_after_rtl_with_a_netlist_is_refused_before_anything_runs(tmp_path):
    """`stop_after: rtl` writes no netlist, so a request that also asks for one (the default)
    cannot succeed: it is refused when planned, saying what to write instead. The Verilog
    netlist is the declared output and the JSON netlist a plain artifact, and both are refused
    (`tests/test_yosys_stop_after.py` has the rule for every flow that stops)."""
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsError, match="netlist_verilog") as refused:
        runner.plan(Yosys, _design(tmp_path / "design"), flow_settings=["stop_after=rtl"])
    assert "netlist_json" in str(refused.value)
    assert runner.plan(
        Yosys,
        _design(tmp_path / "other"),
        flow_settings=["stop_after=rtl", "netlist_verilog=", "netlist_json="],
    )


# ------------------------------------------------------------------------------ the settings


def test_abc_driver_cell_is_a_cell_name():
    """A cell name is text: `BUF_X1` validates, and a number is not a cell name."""
    assert Yosys.Settings(abc={"driver_cell": "BUF_X1"}).abc.driver_cell == "BUF_X1"
    with pytest.raises(ValidationError):
        Yosys.Settings(abc={"driver_cell": 1})


def test_corner_needs_a_platform_and_one_of_its_corners(tmp_path):
    with pytest.raises(FlowSettingsException, match="give `platform` or `platform_file` too"):
        Yosys.check_settings_supported(Yosys.Settings(corner="tt"))
    with pytest.raises(FlowSettingsException, match="unknown corner 'nonexistent'"):
        Yosys.check_settings_supported(Yosys.Settings(platform="nangate45", corner="nonexistent"))
    Yosys.check_settings_supported(Yosys.Settings(platform="nangate45", corner="tt"))


# ------------------------------------------------------------------- what a platform derives


def _stage_platform(root: Path, dont_use_cells: list[str]) -> dict[str, Any]:
    """A platform whose liberty set is two files, each holding one cell, and whose own
    dont-use list is `dont_use_cells`: `sky130hs`'s shape when that list is empty. Return the
    settings that select it, in the store `root`."""
    pdk = root / "store" / "tiny"
    pdk.mkdir(parents=True)
    for name in ("a", "b"):
        (pdk / f"{name}.lib").write_text(
            f"library ({name}) {{\n  cell (CELL_{name.upper()}) {{\n    area : 1;\n  }}\n}}\n"
        )
    (pdk / "cells.lef").write_text("")
    values = {"abc_driver_cell": "CELL_A", "abc_load_in_ff": 1.5}
    if dont_use_cells:
        values["dont_use_cells"] = dont_use_cells
    residue = root / "tiny.yaml"
    residue.write_text(
        json.dumps(
            {
                "name": "tiny",
                "providers": {"default": "directory", "directory": {"layout": "orfs"}},
                "corners": {"default": "tt"},
                "files": {"liberty": {"tt": ["a.lib", "b.lib"]}, "tech_lef": "cells.lef"},
                "values": values,
            }
        )
    )
    return {"platform_file": str(residue), "pdk_root": str(root / "store")}


SCRIPT_FORMATS = pytest.mark.parametrize("script_format", ["ys", "tcl"])


@SCRIPT_FORMATS
@pytest.mark.parametrize("own", [[], ["CELL_B"]], ids=["platform-only", "both"])
@pytest.mark.parametrize("platform_s", [[], ["CELL_A"]], ids=["none", "platform-s"])
def test_every_liberty_file_is_read_and_mapped_to_and_the_dont_use_cells_are_words(
    tmp_path, monkeypatch, own, platform_s, script_format
):
    """A platform with several Liberty files: the script reads each file as it is, and hands
    each to `dfflibmap` and to `abc`. The cells that the flow must not use, the platform's first
    and then the setting's, are one `-dont_use` word each on both commands. No library is
    patched, merged or written."""
    settings = {
        **_stage_platform(tmp_path, platform_s),
        "clock": {"period": "2 ns"},
        "dont_use_cells": own,
    }
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    libs = sorted(str(p) for p in (tmp_path / "store" / "tiny").glob("*.lib"))
    assert len(libs) == 2
    liberty = [word for lib in libs for word in ("-liberty", lib)]
    dont_use = [word for cell in platform_s + own for word in ("-dont_use", cell)]
    commands = _commands(run_dir, script_format, tmp_path)
    assert _words(commands, "read_liberty") == [
        ["read_liberty", "-overwrite", "-setattr", "liberty_cell", "-lib", lib] for lib in libs
    ]
    assert _words(commands, "dfflibmap") == [["dfflibmap", *liberty, *dont_use]]
    (abc,) = _words(commands, "abc")
    assert "-D" not in abc
    assert abc[-len(liberty + dont_use) - 2 :] == [*liberty, *dont_use, "-constr", "abc.constr"]
    assert not list(run_dir.rglob("*.lib")) and not list(tmp_path.rglob("*merged*"))


def test_a_platform_alone_configures_everything_a_standard_cell_run_needs(tmp_path, monkeypatch):
    """Everything a standard-cell synthesis needs follows from the platform alone: the
    Liberty files, the mapping files, tie and buffer cells, flattening, the abc script `abc.strategy`
    selects, the abc constraints from the driver cell and the load of the platform, and a netlist
    without attributes. Post-synthesis optimization stays off. The settings of the groups stay as
    the user gave them: the platform's values are in the script only."""
    given = _tiny(tmp_path)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    pdk = Path(given["pdk_root"]) / "tiny"
    effective = _effective(run_dir)
    script = _script(run_dir)
    assert effective["flatten"] is True and "flatten" in script.split()
    assert effective["liberty"] == [str(pdk / "tiny.lib")]
    assert f"techmap -map {pdk / 'cells_adders.v'}" in script
    assert f"read_verilog -defer {pdk / 'cells_clkgate.v'}" in script
    assert f"techmap -map {pdk / 'cells_latch.v'}" in script
    assert "hilomap -singleton -hicell TIEHI_SMALL Z -locell TIELO_X1 Z" in script
    assert "insbuf -buf BUF_X1 A Z" in script
    assert effective["sequential"] == Yosys.Settings().sequential.model_dump(mode="json")
    assert effective["abc"] == Yosys.Settings().abc.model_dump(mode="json")
    assert effective["finish"] == Yosys.Settings().finish.model_dump(mode="json")
    assert effective["post_synth_opt"] is False and "opt -full" not in script
    assert yosys_flows.abc_strategy_script("area") in script
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell BUF_X1\nset_load 3.5\n"
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    for flag in ("-noattr", "-nohex", "-nodec", "-noexpr"):
        assert flag in write.split(), flag
    # the Liberty files are read where they are: the run writes no library of its own
    assert not list(run_dir.glob("*.lib"))


def test_explicit_settings_win_over_what_the_platform_derives(tmp_path, monkeypatch):
    settings = _tiny(
        tmp_path,
        abc={"strategy": "speed", "driver_cell": "INV_X1", "load": "2.5 fF"},
        flatten=False,
        netlist_attrs=True,
    )
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = _script(run_dir)
    assert yosys_flows.abc_strategy_script("speed") in script
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell INV_X1\nset_load 2.5\n"
    assert "flatten" not in script.split()
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    assert "-noattr" not in write.split()


def test_without_a_liberty_library_nothing_is_derived(tmp_path, monkeypatch):
    """A generic-gate run is what it was: no abc script, no constraints, no flattening, the
    netlist's attributes kept -- the group settings act on a liberty mapping."""
    run_dir = _scripted_launch(
        tmp_path, monkeypatch, Yosys, {"gates": ["AND", "OR", "NOT"], "clock": {"period": "2 ns"}}
    )
    script = _script(run_dir)
    assert "-script" not in script and "-constr" not in script
    assert "opt -full -purge -sat" not in script
    assert "flatten" not in script.split()
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    assert "-noattr" not in write.split() and "-nohex" not in write.split()
    assert not (run_dir / "abc.constr").exists()


# ------------------------------------------------------------------ with the real yosys


def test_yosys_with_a_platform_alone_maps_to_its_cells(tmp_path):
    """`xeda run yosys -s platform_file=... pdk_root=...`, alone: the cells of the platform, one
    run directory, and the declared netlist recorded. The run writes no Liberty file."""
    require_yosys()
    design = tmp_path / "design"
    _design(design)
    (design / "design.yaml").write_text(
        'name: mac\nrtl:\n  sources: ["mac.v"]\n  top: mac\n  clock:\n    port: clk\n'
    )
    run_root = tmp_path / "xeda_run"
    store = tiny_store(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "yosys",
            "design.yaml",
            "--run-root",
            str(run_root),
            "-s",
            f"platform_file={TINY_RESIDUE}",
            f"pdk_root={store}",
            "clock.period=2ns",
            "--json",
        ],
        cwd=design,
        capture_output=True,
        text=True,
        timeout=600,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:]
    assert sorted(p.parent.name for p in run_root.rglob("settings.json")) == ["yosys"]
    run_dir = run_root / "mac" / "yosys"
    results = json.loads((run_dir / "results.json").read_text())
    netlist = Path(results["outputs"]["netlist"]["path"])
    assert netlist == run_dir / "netlist.v"
    text = netlist.read_text()
    assert "_X1 " in text, "no cells of the platform in the netlist"
    assert not re.search(r"^\s*\\\$_", text, re.M), "a generic cell is left in the netlist"
    assert "1'bx" not in text, "an undefined constant is left in the netlist"
    assert not list(run_dir.glob("*.lib"))


# ------------------------------------------------------------------ post_synth_opt


@pytest.mark.parametrize("given", [None, False])
@pytest.mark.parametrize("strategy", ["area", "yosys"])
def test_a_liberty_mapping_never_runs_the_post_synthesis_optimization(
    tmp_path, monkeypatch, given, strategy
):
    """The liberty mapping ends with the tail of OpenROAD-flow-scripts, so `abc.strategy` does not
    turn post-synthesis optimization on: unset, `post_synth_opt` is false, and no `opt -full`
    is in the script. `abc.strategy: yosys` keeps the default mapping script of yosys."""
    settings = _tiny(tmp_path, abc={"strategy": strategy})
    if given is not None:
        settings["post_synth_opt"] = given
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    assert "opt -full" not in _script(run_dir)
    assert _effective(run_dir)["post_synth_opt"] is False
    recorded = json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    assert recorded["post_synth_opt"] is given
    # reloaded from `settings.json`, the settings are the same again, the unset value included
    reloaded = Yosys.Settings.from_input(recorded)
    again = Yosys.Settings.from_input(json.loads(json.dumps(reloaded.model_dump(mode="json"))))
    assert again.model_dump() == reloaded.model_dump()
    assert reloaded.post_synth_opt is given and again.post_synth_opt is given


@pytest.mark.parametrize("given", [None, True, False])
def test_the_gate_mapping_keeps_its_post_synthesis_optimization_setting(
    tmp_path, monkeypatch, given
):
    """Mapping to gates is not the liberty mapping: it runs the optimization after synthesis
    when `post_synth_opt` is true, and not otherwise."""
    settings: dict[str, Any] = {"gates": ["AND", "OR", "NOT"], "clock": {"period": "2 ns"}}
    if given is not None:
        settings["post_synth_opt"] = given
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    assert ("opt -full -purge -sat" in _script(run_dir)) is bool(given)


# ------------------------------------------------------------------------ the synthesis options


STRATEGY_CHOICES = list(get_args(AbcGroup.model_fields["strategy"].annotation))
SCRIPT_STRATEGIES = [choice for choice in STRATEGY_CHOICES if choice != "yosys"]


def test_every_strategy_choice_is_covered():
    """Every strategy choice is covered."""
    assert set(STRATEGY_CHOICES) == {
        *("area", "speed", "speed_roundtrip", "yosys"),
        *("area_0", "area_1", "area_2", "delay_0", "delay_1", "delay_2", "delay_3"),
    }


@pytest.mark.parametrize("strategy", STRATEGY_CHOICES)
def test_strategy_selects_the_abc_script(strategy, tmp_path, monkeypatch):
    """What yosys rendered into its script, given `abc.strategy`: the abc script that the
    strategy selects, or none for `yosys`. It selects no post-synthesis optimization, and the
    flat `abc_script` setting stays unset: the script is a value of the run, not a setting.

    No tool runs: this is about the settings and the script, so every command is recorded
    instead, and the run stops once yosys, having produced nothing, fails.
    """
    commands = []

    def record(executable, args=None, **kwargs):
        commands.append([str(executable), *map(str, args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    settings = _tiny(tmp_path, abc={"strategy": strategy})
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, _design(tmp_path), settings)
    (settings_json,) = tmp_path.glob("xeda_run/mac/yosys/settings.json")
    yosys_settings = json.loads(settings_json.read_text())["effective_flow_settings"]
    script = (settings_json.parent / "yosys_synth.ys").read_text()
    assert any(Path(cmd[0]).name == "yosys" for cmd in commands), "yosys was never launched"

    assert yosys_settings["abc"]["strategy"] == strategy
    assert yosys_settings["abc_script"] is None
    assert yosys_settings["post_synth_opt"] is False
    assert "opt -full" not in script
    (abc,) = [line for line in script.splitlines() if line.startswith("abc ")]
    expected = yosys_flows.abc_strategy_script(strategy)
    if expected is None:
        assert "-script" not in abc
    else:
        assert f" -script {expected} " in abc


def test_a_strategy_is_a_setting_of_the_flow_that_acts_on_it():
    """`abc.strategy` acts on yosys's mapping, so it is yosys's setting."""
    assert "abc" in Yosys.Settings.model_fields
    assert "abc" not in YosysFpga.Settings.model_fields


@pytest.mark.parametrize("strategy", SCRIPT_STRATEGIES)
def test_a_strategy_selects_an_abc_script_yosys_accepts(strategy):
    """A script must be the text `Yosys.Settings.abc_script` declares, which a list of commands
    is not: commands separated by `;`, words by `,`, after a `+`."""
    script = yosys_flows.abc_strategy_script(strategy)
    assert isinstance(script, str) and script.startswith("+") and " " not in script
    assert Yosys.Settings(abc_script=script).abc_script == script


def test_the_strategies_select_different_scripts():
    """Each strategy has a script of its own, and `yosys` has none."""
    scripts = [yosys_flows.abc_strategy_script(strategy) for strategy in STRATEGY_CHOICES]
    assert len(set(scripts)) == len(STRATEGY_CHOICES)
    assert yosys_flows.abc_strategy_script("yosys") is None


def test_area_plus_speed_is_no_strategy():
    """It selected exactly the area script, so it was "area" under another name."""
    with pytest.raises(ValidationError):
        Yosys.Settings(abc={"strategy": "area+speed"})


def _contains_run(sequence, run):
    """Check whether a generated script invokes a command."""
    return any(sequence[i : i + len(run)] == run for i in range(len(sequence) - len(run) + 1))


@pytest.mark.parametrize("strategy", SCRIPT_STRATEGIES)
def test_the_yosys_flow_synthesizes_with_the_selected_script(strategy, tmp_path):
    """End to end, with its own template: abc executes exactly the commands the selected script
    names, in order -- `upsize`/`dnsize` included, which yosys's default mapping never runs."""
    require_yosys()
    design = _design(tmp_path)
    with contextlib.suppress(ExecutableNotFound):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Yosys, design, _tiny(tmp_path, abc={"strategy": strategy})
        )

    (yosys_results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    yosys_run = yosys_results.parent
    assert json.loads((yosys_run / "results.json").read_text())["success"] is True
    executed = re.findall(r"^ABC: \+ (.+?)\s*$", (yosys_run / "yosys.log").read_text(), re.M)
    script = yosys_flows.abc_strategy_script(strategy)
    assert script is not None
    selected = [command.replace(",", " ") for command in script[1:].split(";")]
    assert _contains_run(executed, selected), f"abc did not run the {strategy} script"


@pytest.mark.parametrize("strategy", SCRIPT_STRATEGIES)
def test_real_yosys_maps_with_the_selected_script(strategy, tmp_path):
    """The script as the yosys template passes it (`abc -script "<abc_script>"`), run for real
    against the library of the `tiny` test platform."""
    require_yosys()
    lib = tmp_path / "tiny.lib"
    shutil.copy(TINY / "tiny.lib", lib)
    (tmp_path / "abc.constr").write_text("set_driving_cell BUF_X1\nset_load 3.5\n")
    (tmp_path / "top.v").write_text(
        "module top(input clk, input [7:0] a, b, output reg [15:0] q);\n"
        "  always @(posedge clk) q <= q + a * b;\n"
        "endmodule\n"
    )
    script = yosys_flows.abc_strategy_script(strategy)
    commands = (
        "read_verilog top.v; synth -top top -flatten; dfflibmap -liberty tiny.lib; "
        f'abc -D 1000 -script "{script}" -liberty tiny.lib -constr abc.constr; '
        "opt_clean -purge; write_verilog -noattr netlist.v"
    )
    proc = subprocess.run(
        ["yosys", "-q", "-p", commands], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    netlist = (tmp_path / "netlist.v").read_text()
    assert "_X1 " in netlist, "no cells of the library in the mapped netlist"


def test_a_second_launch_of_yosys_is_fresh(tmp_path):
    """Real yosys synthesis: an unchanged relaunch starts no tools and rewrites no run output,
    even byte-identically."""
    from .tool_utils import launch_until_fresh, run_outputs_state

    require_yosys()
    settings = _tiny(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design = _design(tmp_path)
    first = runner.launch_flow(Yosys, design, settings)
    assert first.succeeded and not first.reused
    again = launch_until_fresh(runner, lambda: runner.launch_flow(Yosys, design, settings))
    before = run_outputs_state(again.run_path)
    last = len(runner.launched)
    fresh = runner.launch_flow(Yosys, design, settings)
    assert fresh.reused and all(flow.reused for flow in runner.launched[last:])
    assert run_outputs_state(again.run_path) == before


def test_the_cli_hands_the_synthesis_settings_to_real_yosys(tmp_path):
    """`xeda run yosys` with its settings on the command line: the script and the abc constraints
    follow them (each differs from what the platform gives), and the netlist is written where the
    flow says. The `speed` target is not set here: on the `tiny` library its abc script fails
    together with a black box or without post-synthesis optimization (an abc assertion),
    though `test_the_yosys_flow_synthesizes_with_the_selected_script` runs it alone."""
    require_yosys()
    _design(tmp_path)
    (tmp_path / "design.yaml").write_text(
        'name: mac\nrtl:\n  sources: ["mac.v"]\n  top: mac\n  clock:\n    port: clk\n'
    )
    root = tmp_path / "runs"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "yosys",
            str(tmp_path / "design.yaml"),
            "--run-root",
            str(root),
            "--json",
            "-s",
            f"platform_file={TINY_RESIDUE}",
            f"pdk_root={tiny_store(tmp_path)}",
            "clock.period=2ns",
            "abc.driver_cell=INV_X1",
            "abc.load=2.5 fF",
            "black_box=mul8",
            "post_synth_opt=false",
        ],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:] or document
    producer = root / "mac" / "yosys"
    settings = json.loads((producer / "settings.json").read_text())["effective_flow_settings"]
    assert settings["abc"]["driver_cell"] == "INV_X1"
    assert settings["abc"]["load"] == "2.5 fF"
    assert settings["black_box"] == ["mul8"]
    assert settings["post_synth_opt"] is False
    script = (producer / "yosys_synth.ys").read_text()
    assert "map,-B,0.9" in script and "blackbox mul8" in script
    assert "opt -full -purge -sat" not in script
    assert (producer / "abc.constr").read_text() == "set_driving_cell INV_X1\nset_load 2.5\n"


def test_init_and_prepare_inputs_write_nothing(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    run = tmp_path / "run"
    run.mkdir()
    flow = Yosys(tiny_settings(tiny_store(tmp_path)), design, run)
    flow.init()
    flow.prepare_inputs()
    assert list(run.iterdir()) == []


# ------------------------------------------------------------------------ the ABC script file


def _flow_with_script_file(tmp_path: Path, cls=Yosys):
    script = tmp_path / "map.abc"
    script.write_text("strash; map\n")
    run = tmp_path / "run"
    run.mkdir()
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    return cls({"abc_script": str(script), "gates": ["AND"]}, design, run), script


def test_init_registers_the_abc_script_file_and_no_other_prepared_input(tmp_path):
    """`init()` takes the ABC script from the script setting alone. A flow whose prepared inputs
    include other files (the files of a platform) must not hand one of them to `init()` as the
    ABC script."""

    with preserved_flow_registry():

        class _WithOtherInputs(Yosys):
            def locate_prepared_inputs(self):
                return [*(super().locate_prepared_inputs() or []), tmp_path / "other.lib"]

    flow, script = _flow_with_script_file(tmp_path, _WithOtherInputs)
    flow.init()
    assert flow.settings.abc_script == str(script)
    assert flow.implicit_inputs == [script]


def test_a_relative_liberty_is_relative_to_the_design_root(tmp_path, monkeypatch):
    """The design root is not the working directory, and yosys runs in the run directory: a
    relative `liberty` or `sequential.dff_liberty` names a file under the design root."""
    from .tool_utils import use_fake_asic_tools

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    design = _design(tmp_path / "design")
    (tmp_path / "design" / "lib").mkdir()
    shutil.copy(TINY / "tiny.lib", tmp_path / "design" / "lib" / "x.lib")
    shutil.copy(TINY / "tiny.lib", tmp_path / "design" / "lib" / "dff.lib")
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys,
        design,
        {
            "liberty": ["lib/x.lib"],
            "sequential": {"dff_liberty": "lib/dff.lib"},
            "clock": {"period": "2 ns"},
        },
    )
    assert flow.succeeded
    script = _script(flow.run_path)
    assert str(tmp_path / "design" / "lib" / "dff.lib") in script
    effective = _effective(flow.run_path)
    assert effective["liberty"] == [str(tmp_path / "design" / "lib" / "x.lib")]
    assert f"-liberty {tmp_path / 'design' / 'lib' / 'x.lib'}" in script


# ------------------------------------------------------------- the order of the liberty mapping

#: The cells that the `tiny` platform lists as unusable: the library marks `INV_X4` and `NAND2_X4`.
TINY_DONT_USE = ["INV_X4", "NAND2_X4"]


def _expected_order(
    given: dict[str, Any], design: Path, strategy: str, dont_use: list[str]
) -> list[list[str]]:
    """The commands of the liberty mapping for the `tiny` platform and the `mac` design, from
    `logger` to the check of the mapped design, as the words of each command."""
    pdk = Path(given["pdk_root"]) / "tiny"
    liberty = ["-liberty", str(pdk / "tiny.lib")]
    unusable = [word for cell in dont_use for word in ("-dont_use", cell)]
    script = yosys_flows.abc_strategy_script(strategy)
    abc_script = ["-script", script] if script is not None else []
    return [
        ["logger", "-notime"],
        ["read_liberty", "-overwrite", "-setattr", "liberty_cell", "-lib", str(pdk / "tiny.lib")],
        ["read_verilog", "-defer", "-noautowire", "-sv", str(design / "mac.v")],
        ["read_verilog", "-defer", str(pdk / "cells_clkgate.v")],
        ["hierarchy", "-check", "-top", "mac"],
        ["check", "-initdrv"],
        ["proc"],
        ["flatten"],
        ["synth", "-flatten", "-run", ":fine"],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc"],
        ["chformal", "-remove"],
        ["delete", "t:$print"],
        ["rename", "-wire"],
        ["opt", "-purge"],
        ["extract_fa"],
        ["techmap", "-map", str(pdk / "cells_adders.v")],
        ["techmap"],
        ["opt", "-fast", "-purge"],
        ["dfflegalize", "-cell", "$_DLATCH_P_", "x", "-cell", "$_DLATCH_N_", "x", "t:$_DLATCH_*"],
        ["techmap", "-map", str(pdk / "cells_latch.v")],
        ["dfflibmap", *liberty, *unusable],
        ["opt"],
        ["setundef", "-zero"],
        ["abc", *abc_script, *liberty, *unusable, "-constr", "abc.constr"],
        ["splitnets"],
        ["opt_clean", "-purge"],
        ["hilomap", "-singleton", "-hicell", "TIEHI_SMALL", "Z", "-locell", "TIELO_X1", "Z"],
        ["insbuf", "-buf", "BUF_X1", "A", "Z"],
        ["check", "-assert", "-mapped"],
    ]


@SCRIPT_FORMATS
@pytest.mark.parametrize("strategy", STRATEGY_CHOICES)
def test_the_liberty_mapping_runs_the_commands_in_the_order_of_openroad_flow_scripts(
    tmp_path, monkeypatch, strategy, script_format
):
    """Both script formats hand yosys the same commands in the same order: the read of the
    Liberty files, the design, the coarse and the fine part of `synth` without `abc`, the
    mapping of adders and latches, `dfflibmap`, `abc` with the script `abc.strategy` selects, and the
    check that every cell is mapped. Then the netlist is written."""
    given = _tiny(tmp_path, abc={"strategy": strategy})
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    commands = _commands(run_dir, script_format, tmp_path)
    expected = _expected_order(given, tmp_path / "design", strategy, TINY_DONT_USE)
    assert commands[: len(expected)] == expected
    # then the netlist: its reports and its writers, with `check` and `stat` after the mapping
    rest = commands[len(expected) :]
    assert rest[0][:3] == ["tee", "-o", "final_check.log"]
    assert rest[-1][0] == "write_verilog" and rest[-1][-1] == "netlist.v"


@SCRIPT_FORMATS
def test_the_liberty_mapping_reads_the_liberty_files_once_and_before_the_design(
    tmp_path, monkeypatch, script_format
):
    """The Liberty files are read as library cells before the sources, with the attribute that
    marks them, and the generic read of the reader template is not repeated. `sequential.dff_liberty`, when
    given, is read the same way."""
    shutil.copy(TINY / "tiny.lib", tmp_path / "dff.lib")
    given = _tiny(tmp_path, sequential={"dff_liberty": str(tmp_path / "dff.lib")})
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    commands = _commands(run_dir, script_format, tmp_path)
    names = [call[0] for call in commands]
    reads = _words(commands, "read_liberty")
    assert [call[-1] for call in reads] == [
        str(Path(given["pdk_root"]) / "tiny" / "tiny.lib"),
        str(tmp_path / "dff.lib"),
    ]
    assert all(
        call[:5] == ["read_liberty", "-overwrite", "-setattr", "liberty_cell", "-lib"]
        for call in reads
    )
    assert max(i for i, name in enumerate(names) if name == "read_liberty") < names.index(
        "read_verilog"
    )
    # the flip-flops come from the file given for them, and the logic from the Liberty files
    (dfflibmap,) = _words(commands, "dfflibmap")
    assert dfflibmap[:3] == ["dfflibmap", "-liberty", str(tmp_path / "dff.lib")]
    (abc,) = _words(commands, "abc")
    assert str(tmp_path / "dff.lib") not in abc


# ----------------------------------------------------------------------------- the -dont_use words

DONT_USE_NAMES = ["USER_X1", "LP_*", "CELL?_X4", "SEL[12]_X1"]


@SCRIPT_FORMATS
def test_a_dont_use_name_reaches_both_commands_as_one_word_with_its_pattern_characters(
    tmp_path, monkeypatch, script_format
):
    """The platform's cells come first and the cells of `dont_use_cells` follow, each once on
    `dfflibmap` and on `abc`. Yosys reads `*`, `?` and `[...]` in a word as a pattern, so the
    word keeps them as given. In a Tcl script they reach yosys as the same word."""
    given = _tiny(tmp_path, dont_use_cells=[*DONT_USE_NAMES, TINY_DONT_USE[0]])
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    commands = _commands(run_dir, script_format, tmp_path)
    for command in ("dfflibmap", "abc"):
        (call,) = _words(commands, command)
        used = [call[i + 1] for i, word in enumerate(call) if word == "-dont_use"]
        assert used == [*TINY_DONT_USE, *DONT_USE_NAMES], command
        assert call.count("-dont_use") == len(used)


@pytest.mark.parametrize(
    "pattern, used, unused",
    [
        ("NAND2_X*", "NOR2_X1", "NAND2_X1"),
        ("NOR2_X?", "NAND2_X1", "NOR2_X1"),
        ("NAND2_X??", "NAND2_X1", "NOR2_X1"),
    ],
    ids=["star", "question-mark", "two-question-marks-match-no-cell"],
)
def test_a_pattern_in_a_dont_use_name_matches_the_cells_it_names_in_dfflibmap_and_abc(
    tmp_path, pattern, used, unused
):
    """Real yosys: `-dont_use` takes the pattern that the flow hands it. `*` stands for any
    characters and `?` for one, so `NAND2_X??` matches no cell of the library: `NAND2_X1` has one
    character after the `X`. The library has two gates that map `~(a & b)`; with one of them
    unusable, the other maps it."""
    require_yosys()
    lib = tmp_path / "tiny.lib"
    shutil.copy(TINY / "tiny.lib", lib)
    (tmp_path / "top.v").write_text(
        "module top(input clk, input a, b, output reg q);\n"
        "  always @(posedge clk) q <= ~(a & b);\n"
        "endmodule\n"
    )
    words = f"-dont_use {pattern}"
    commands = (
        "read_verilog top.v; synth -top top -flatten; "
        f"dfflibmap -liberty tiny.lib {words}; abc -liberty tiny.lib {words}; "
        "opt_clean -purge; write_verilog -noattr netlist.v"
    )
    proc = subprocess.run(
        ["yosys", "-q", "-p", commands], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    netlist = (tmp_path / "netlist.v").read_text()
    assert "DFF_X1" in netlist
    assert re.search(rf"^\s*{used}\b", netlist, re.M), netlist
    assert not re.search(rf"^\s*{unused}\b", netlist, re.M), netlist


# ------------------------------------------------------------------------------------- no merge


def test_a_run_writes_no_patched_merged_or_processed_library(tmp_path, monkeypatch):
    """The run reads the Liberty files where they are. It makes no merged library, and no
    directory of processed libraries, in the run directory or anywhere under the test directory."""
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path))
    assert not [p for p in tmp_path.rglob("*") if re.search(r"merged|processed", p.name)]
    assert not list(run_dir.rglob("*.lib"))
    assert "merged" not in _script(run_dir) and "processed" not in _script(run_dir)


def test_merge_libs_to_was_removed_and_says_so():
    with pytest.raises(FlowSettingsError, match="`merge_libs_to` was removed: .*; delete it"):
        Yosys.Settings.from_input({"merge_libs_to": "merged"})


# ------------------------------------------------------------------------------------ the refusals

LIBERTY_ONLY = {"liberty": ["cells.lib"]}


@pytest.mark.parametrize(
    "given, mentions",
    [
        ({"abc_dff": True}, ["abc_dff", "no effect", "dfflibmap"]),
        ({"post_synth_opt": True}, ["post_synth_opt", "no effect", "coarse.opt_dff_sat"]),
        ({"nosynth": True}, ["nosynth", "needs `synth`", "reduce the design to gates"]),
        ({"noabc": True}, ["noabc"]),
    ],
    ids=["abc_dff", "post_synth_opt", "nosynth", "noabc"],
)
def test_a_setting_the_liberty_mapping_cannot_honor_is_refused_when_planned(given, mentions):
    """The liberty mapping runs its own elaboration, synthesis and mapping with `abc`, ends with
    the tail of OpenROAD-flow-scripts, and maps the flip-flops before `abc` runs. A setting that
    asks for something else is refused with its cause, and only with a liberty mapping."""
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(Yosys.Settings(**LIBERTY_ONLY, **given))
    message = str(refused.value)
    for word in mentions:
        assert word in message, message
    # the same settings are fine when yosys maps to gates or to LUTs
    for mapping in ({"gates": ["AND", "OR", "NOT"]}, {"lut": "4"}):
        Yosys.check_settings_supported(Yosys.Settings(**mapping, **given))


@pytest.mark.parametrize(
    "given", [{"abc_dff": False}, {"post_synth_opt": False}, {"prep": ["-nomem"]}, {}]
)
def test_the_settings_that_agree_with_the_liberty_mapping_are_accepted(given):
    Yosys.check_settings_supported(Yosys.Settings(**LIBERTY_ONLY, **given))


# ----------------------------------------------------------- abc_dff and splitnets are derived


@pytest.mark.parametrize(
    "mapping, abc_dff, splitnets",
    [
        ({"liberty": ["cells.lib"]}, False, True),
        ({"gates": ["AND"]}, True, False),
        ({"lut": "4"}, True, False),
    ],
    ids=["liberty", "gates", "lut"],
)
def test_abc_dff_and_splitnets_follow_the_mapping_unless_set(
    tmp_path, monkeypatch, mapping, abc_dff, splitnets
):
    """Unset, `abc_dff` is false and `splitnets` true for a liberty mapping, as OpenROAD-flow-
    scripts runs it, and `abc_dff` true and `splitnets` false for gates and LUTs, as before. A
    value that the user gives wins where the mapping allows it."""
    cells = tmp_path / "design" / "cells.lib"
    cells.parent.mkdir(parents=True)
    shutil.copy(TINY / "tiny.lib", cells)
    settings: dict[str, Any] = {
        **{k: ([str(cells)] if k == "liberty" else v) for k, v in mapping.items()},
        "clock": {"period": "2 ns"},
    }
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = _script(run_dir)
    (abc,) = [line for line in script.splitlines() if line.startswith("abc ")]
    assert ("-dff" in abc.split()) is abc_dff
    assert ("splitnets" in script.split()) is splitnets
    for given in (False, True):
        different = {**settings, "splitnets": given}
        other = tmp_path / f"other-{given}"
        other.mkdir()
        run_dir = _scripted_launch(other, monkeypatch, Yosys, different)
        assert ("splitnets" in _script(run_dir).split()) is given


def test_abc_dff_false_keeps_dff_off_the_abc_command_of_a_liberty_mapping(tmp_path, monkeypatch):
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path, abc_dff=False))
    (abc,) = [line for line in _script(run_dir).splitlines() if line.startswith("abc ")]
    assert "-dff" not in abc.split()


def test_the_description_of_abc_dff_says_why_a_liberty_mapping_refuses_true():
    """`dfflibmap` maps the flip-flops before `abc` runs, so `-dff` finds none: the description
    says that, and does not blame a crash of ABC."""
    description = Yosys.Settings.model_fields["abc_dff"].description
    assert description is not None
    for words in ("no effect", "liberty", "dfflibmap"):
        assert words in description
    for words in ("buffer -c", "Abc_NtkRedirectCiCo", "crash"):
        assert words not in description


def test_the_fpga_flows_see_plain_booleans_for_abc_dff_and_splitnets():
    for name in ("abc_dff", "splitnets"):
        assert YosysFpga.Settings.model_fields[name].annotation is bool
        assert Yosys.Settings.model_fields[name].annotation == Optional[bool]


def test_no_synth_flag_is_added_twice(tmp_path, monkeypatch):
    """The script gives the liberty mapping's `synth -flatten` itself: the setting `flatten` does
    not also add `-flatten` to `synth_flags`, and a flag that the user gives goes on both lines."""
    given = _tiny(tmp_path, synth_flags=["-relativeshare"])
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    synths = [line.split() for line in _script(run_dir).splitlines() if line.startswith("synth")]
    assert synths == [
        ["synth", "-flatten", "-run", ":fine", "-relativeshare"],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc", "-relativeshare"],
    ]
    assert _effective(run_dir)["synth_flags"] == ["-relativeshare"]


# ----------------------------------------------------------------- the other settings keep a place


def test_the_other_settings_of_the_liberty_mapping_keep_their_places(tmp_path, monkeypatch):
    """`abc_flags` follow the constraints on `abc`, `rmports` follows `insbuf`, `check_assert:
    false` weakens only the check of mapped cells, `splitnets_ports` and `splitnets_driver` add
    their flags, and `black_box`, `post_synth_rename` and `stop_after` act where they did."""
    given = _tiny(
        tmp_path,
        abc_flags=["-fast"],
        rmports=True,
        check_assert=False,
        splitnets_ports=True,
        splitnets_driver=True,
        post_synth_rename=["-hide"],
    )
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    commands = _commands(run_dir, "ys", tmp_path)
    (abc,) = _words(commands, "abc")
    assert abc[-3:] == ["-constr", "abc.constr", "-fast"]
    names = [call[0] for call in commands]
    assert commands[names.index("splitnets")] == ["splitnets", "-driver", "-ports"]
    after_insbuf = commands[names.index("insbuf") + 1 :]
    assert after_insbuf[:2] == [["rmports"], ["check", "-mapped"]]
    assert commands.index(["rename", "-hide"]) > commands.index(["check", "-mapped"])


def test_an_explicit_false_splitnets_leaves_the_split_out(tmp_path, monkeypatch):
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path, splitnets=False))
    assert "splitnets" not in _script(run_dir).split()


# ------------------------------------------------------------------ with the real yosys, on tiny

COUNTER = (
    "module counter(input clk, rst, input [7:0] step, input en, a, output reg [7:0] count,\n"
    "               output one, zero, dc);\n"
    "  always @(posedge clk) if (rst) count <= 0; else count <= count + step;\n"
    "  assign one = 1'b1;\n"
    "  assign zero = 1'b0;\n"
    "  assign dc = en ? a : 1'bx;\n"
    "endmodule\n"
)


def _counter_design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "counter.v").write_text(COUNTER)
    return Design(
        name="counter",
        design_root=root,
        rtl={"sources": ["counter.v"], "top": "counter", "clock": {"port": "clk"}},
    )


def _split_liberty(source: Path, first: Path, second: Path, names: tuple[str, ...]) -> None:
    """Write the cells of `source` named in `names` to `second` and the others to `first`, each
    file with the header of the library."""
    lines = source.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"  cell \(", line)]
    header, tail = lines[: starts[0]], lines[-1:]
    cells = {
        re.match(r"  cell \((\w+)\)", lines[a]).group(1): lines[a:b]  # type: ignore[union-attr]
        for a, b in zip(starts, [*starts[1:], len(lines) - 1])
    }
    assert set(names) <= set(cells)
    for path, wanted in ((first, False), (second, True)):
        body = [line for name, cell in cells.items() if (name in names) is wanted for line in cell]
        path.write_text("".join(header + body + tail))


@SCRIPT_FORMATS
@pytest.mark.parametrize("strategy", STRATEGY_CHOICES)
def test_real_yosys_maps_a_counter_with_an_adder_and_constant_outputs(
    tmp_path, strategy, script_format
):
    """The order of the liberty mapping on the `tiny` platform, run for real: the counter maps,
    the netlist holds cells of the library only, no undefined constant is left in it, and the
    script never passes `-dff` to `abc`, which would find no flip-flop there."""
    require_yosys()
    if script_format == "tcl" and not _yosys_has_tcl():
        pytest.skip("this yosys has no TCL support")  # oss-cad-suite has none
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys,
        _counter_design(tmp_path / "design"),
        _tiny(tmp_path, abc={"strategy": strategy}),
    )
    assert flow.succeeded
    netlist = (flow.run_path / "netlist.v").read_text()
    assert "_X1 " in netlist
    assert "1'bx" not in netlist and "x'" not in netlist
    assert not re.search(r"^\s*\\\$_", netlist, re.M), "a generic cell is left in the netlist"
    assert "TIEHI_SMALL" in netlist and "TIELO_X1" in netlist
    script = _script(flow.run_path, script_format)
    assert "abc -dff" not in script and "-dff" not in script.split()
    assert "opt -full" not in script


def test_real_yosys_uses_the_cells_of_every_liberty_file(tmp_path):
    """Two Liberty files: the flip-flop is in one and the logic gates are in the other. The
    mapped counter uses cells of both, so both files reach `dfflibmap`, `abc` and the check that
    every cell is mapped."""
    require_yosys()
    flops, gates = tmp_path / "flops.lib", tmp_path / "gates.lib"
    _split_liberty(
        TINY / "tiny.lib",
        gates,
        flops,
        names=("DFF_X1",),
    )
    assert "DFF_X1" not in gates.read_text() and "NAND2_X1" not in flops.read_text()
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys,
        _counter_design(tmp_path / "design"),
        _tiny(tmp_path, liberty=[str(gates), str(flops)]),
    )
    assert flow.succeeded
    netlist = (flow.run_path / "netlist.v").read_text()
    assert re.search(r"^\s*DFF_X1\b", netlist, re.M), "no cell of the second file"
    assert re.search(r"^\s*(INV|NAND2|NOR2|BUF)_X\d\b", netlist, re.M), "no cell of the first file"
    script = _script(flow.run_path)
    assert script.count("read_liberty") == 2
    (dfflibmap,) = [line for line in script.splitlines() if line.startswith("dfflibmap ")]
    assert dfflibmap.count("-liberty") == 2


def test_a_liberty_file_that_includes_another_is_refused_before_yosys_starts(tmp_path, monkeypatch):
    """The Liberty reader of yosys does not read `include_file`, so the flow refuses it, in the
    launch and in the dry run, for the files of `liberty`, for the files of a platform and for
    `sequential.dff_liberty`. It refuses before the script of yosys is written."""
    from xeda.flow import FlowException
    from .tool_utils import use_fake_asic_tools

    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    design = _design(tmp_path / "design")
    units = tmp_path / "units.lib"
    units.write_text('time_unit : "1ns";\n')
    own = tmp_path / "own.lib"
    own.write_text(f"library (t) {{ include_file({units}); }}\n")
    clean = tmp_path / "clean.lib"
    shutil.copy(TINY / "tiny.lib", clean)
    store_settings = _tiny(tmp_path)
    platform_lib = tmp_path / "store" / "tiny" / "tiny.lib"
    platform_lib.write_text(
        platform_lib.read_text().replace('  time_unit : "1ns";\n', f"  include_file({units});\n")
    )
    cases = {
        "liberty": ({"liberty": [str(own)], "clock": {"period": "2 ns"}}, str(own)),
        "dff_liberty": (
            {
                "liberty": [str(clean)],
                "sequential": {"dff_liberty": str(own)},
                "clock": {"period": "2 ns"},
            },
            str(own),
        ),
        "platform": (store_settings, "tiny.lib"),
    }
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    for name, (settings, named) in cases.items():
        with pytest.raises(FlowException, match="Liberty reader of yosys does not read") as refused:
            runner.launch_flow(Yosys, design, settings)
        assert named in str(refused.value), name
        assert not list((tmp_path / "xeda_run").glob("mac/yosys*/yosys_synth.ys")), name
        _plan, states = runner.plan_with_freshness("yosys", design, flow_settings=settings)
        assert states["yosys"].state == "unknown", name
        assert "Liberty reader of yosys" in states["yosys"].reason, name
