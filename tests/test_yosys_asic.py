"""`yosys` owns its ASIC configuration.

The whole configuration of a standard-cell synthesis -- the liberty set and its merge, the
mapping cells, the abc script and constraints, the netlist format -- follows from a platform:
`xeda run yosys -s platform=<name>` alone maps to the cells of that platform and needs nothing
else. `optimize`, `abc_driver_cell` and `abc_load_in_ff` are settings of `yosys`, which acts on
them. These tests use the `tiny` platform (`tests/tiny_platform.py`): its files are in a store
under the test directory, so no PDK needs to be installed. `tests/test_yosys_platform.py` has the
tests of the platform model itself.

Two liberty bugs are pinned here: a platform's liberty set is always merged into
one library, named `<platform>_merged`, and the platform's own `dont_use_cells` are marked
`dont_use` in it beside the setting's.
"""

import contextlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import pytest

from xeda import Design
from xeda.dataclass import ValidationError
from xeda.design import SourceType
from xeda.flow import FlowException, FlowSettingsError, FlowSettingsException
from xeda.flow.io import declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga
from xeda.flows import yosys as yosys_flows
from xeda.utils import WorkingDirectory
from xeda.tool import ExecutableNotFound

from .flow_registry import preserved_flow_registry
from .tiny_platform import TINY, TINY_RESIDUE, tiny_settings, tiny_store
from .tool_utils import require_yosys

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


def _script(run_dir: Path) -> str:
    return (run_dir / "yosys_synth.ys").read_text()


def _effective(run_dir: Path) -> dict:
    return json.loads((run_dir / "settings.json").read_text())["effective_flow_settings"]


def _tiny(tmp_path: Path, **given: Any) -> dict[str, Any]:
    """Settings that select the `tiny` platform, in a store under `tmp_path`, and a 2 ns clock."""
    return tiny_settings(tiny_store(tmp_path), clock={"period": 2.0}, **given)


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
    assert Yosys.Settings(abc_driver_cell="BUF_X1").abc_driver_cell == "BUF_X1"
    with pytest.raises(ValidationError):
        Yosys.Settings(abc_driver_cell=1)


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


@pytest.mark.parametrize("own", [[], ["CELL_B"]], ids=["platform-only", "both"])
@pytest.mark.parametrize("platform_s", [[], ["CELL_A"]], ids=["none", "platform-s"])
def test_a_platform_s_liberty_set_is_always_merged_with_both_dont_use_lists(
    tmp_path, monkeypatch, own, platform_s
):
    """Two bugs: yosys merged a platform's liberty files only when `dont_use_cells` was
    set, so a platform with an empty list (`sky130hs`) reached abc as several files, of which the
    script hands abc the first; and it ignored the platform's own dont-use list, which its
    description promised."""
    settings = {
        **_stage_platform(tmp_path, platform_s),
        "clock": {"period": 2.0},
        "dont_use_cells": own,
    }
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    (liberty,) = _effective(run_dir)["liberty"]
    merged = run_dir / liberty
    assert merged.parent == run_dir, "merged in yosys's own run directory"
    text = merged.read_text()
    assert "library (tiny_merged)" in text
    assert "CELL_A" in text and "CELL_B" in text
    for cell in ("CELL_A", "CELL_B"):
        marked = f"cell (CELL_{cell[-1]}) {{\n    dont_use : true;" in text
        assert marked is (cell in platform_s + own), cell
    script = _script(run_dir)
    assert f"-liberty {merged}" in script or f"-liberty {merged.name}" in script


def test_a_platform_alone_configures_everything_a_standard_cell_run_needs(tmp_path, monkeypatch):
    """Everything a standard-cell synthesis needs follows from the platform alone: the
    merged library, the mapping files, tie and buffer cells, flattening, the abc script
    `optimize` selects with post-synthesis optimization, the abc constraints from the driver cell
    and the load of the platform, and a netlist without attributes."""
    given = _tiny(tmp_path)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, given)
    pdk = Path(given["pdk_root"]) / "tiny"
    effective = _effective(run_dir)
    script = _script(run_dir)
    assert effective["flatten"] is True and "flatten" in script.split()
    assert effective["adder_map"] == str(pdk / "cells_adders.v")
    assert effective["clockgate_map"] == str(pdk / "cells_clkgate.v")
    assert effective["other_maps"] == [str(pdk / "cells_latch.v")]
    assert effective["hilomap"]["hi"] == ["TIEHI_SMALL", "Z"]
    assert effective["hilomap"]["lo"] == ["TIELO_X1", "Z"]
    assert effective["insbuf"] == ["BUF_X1", "A", "Z"]
    assert effective["dff_liberty"] is None
    assert "opt -full -purge -sat" in script
    assert yosys_flows.abc_opt_script("area").replace(" ", ",") in script
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell BUF_X1\nset_load 3.5\n"
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    for flag in ("-noattr", "-nohex", "-nodec", "-noexpr"):
        assert flag in write.split(), flag
    # the merged library is the one the platform's liberty files give: the same bytes
    merged = run_dir / effective["liberty"][0]
    reference = tmp_path / "reference.lib"
    yosys_flows.preproc_libs([pdk / "tiny.lib"], reference, ["INV_X4", "NAND2_X4"], "tiny_merged")
    assert merged.read_bytes() == reference.read_bytes()


def test_explicit_settings_win_over_what_the_platform_derives(tmp_path, monkeypatch):
    settings = _tiny(
        tmp_path,
        optimize="speed",
        abc_driver_cell="INV_X1",
        abc_load_in_ff=2.5,
        flatten=False,
        netlist_attrs=True,
    )
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = _script(run_dir)
    assert yosys_flows.abc_opt_script("speed").replace(" ", ",") in script
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell INV_X1\nset_load 2.5\n"
    assert "flatten" not in script.split()
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    assert "-noattr" not in write.split()


def test_without_a_liberty_library_nothing_is_derived(tmp_path, monkeypatch):
    """A generic-gate run is what it was: no abc script, no constraints, no flattening, the
    netlist's attributes kept -- `optimize` and the cell settings act on a liberty mapping."""
    run_dir = _scripted_launch(
        tmp_path, monkeypatch, Yosys, {"gates": ["AND", "OR", "NOT"], "clock": {"period": 2.0}}
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
    """`xeda run yosys -s platform_file=... pdk_root=...`, alone: the cells of the platform,
    merged in its own run directory, one run directory, and the declared netlist recorded."""
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
            "clock.period=2.0",
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
    assert "$_" not in text, "an unmapped generic cell is left in the netlist"
    (merged,) = [p for p in run_dir.glob("*.lib")]
    assert "library (tiny_merged)" in merged.read_text()


# ------------------------------------------------------------------ post_synth_opt


@pytest.mark.parametrize("given", [None, True, False])
@pytest.mark.parametrize("optimize", ["area", None])
def test_an_explicit_post_synth_opt_wins_over_what_optimize_derives(
    tmp_path, monkeypatch, given, optimize
):
    """`optimize` turns post-synthesis optimization on only where `post_synth_opt` is left unset:
    `post_synth_opt: false` with the default `optimize: area` keeps the abc script and drops the
    extra optimization, and `true` adds it with `optimize: null`."""
    settings = _tiny(tmp_path, optimize=optimize)
    if given is not None:
        settings["post_synth_opt"] = given
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    expected = (optimize is not None) if given is None else given
    assert ("opt -full -purge -sat" in _script(run_dir)) is expected
    assert _effective(run_dir)["post_synth_opt"] is expected
    recorded = json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    assert recorded["post_synth_opt"] is given
    # reloaded from `settings.json`, the settings are the same again, the unset value included
    reloaded = Yosys.Settings.from_input(recorded)
    again = Yosys.Settings.from_input(json.loads(json.dumps(reloaded.model_dump(mode="json"))))
    assert again.model_dump() == reloaded.model_dump()
    assert reloaded.post_synth_opt is given and again.post_synth_opt is given


# ------------------------------------------------------------------ other_maps


@pytest.mark.parametrize("given", [None, [], ["extra.v"]])
def test_an_explicit_other_maps_wins_over_the_platform_s_latch_map(tmp_path, monkeypatch, given):
    """`other_maps` is derived from the platform only where it is left unset: an explicit empty
    list (`-s other_maps=`) means no extra mapping, not "the platform's latch map"."""
    settings = _tiny(tmp_path)
    if given is not None:
        (tmp_path / "extra.v").write_text("module _unused(); endmodule\n")
        settings["other_maps"] = [str(tmp_path / "extra.v") for _ in given]
    latch = str(Path(settings["pdk_root"]) / "tiny" / "cells_latch.v")
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    expected = [latch] if given is None else [str(tmp_path / "extra.v") for _ in given]
    effective = _effective(run_dir)["other_maps"]
    assert effective == expected
    assert (latch in _script(run_dir)) is (given is None)
    recorded = json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    assert recorded["other_maps"] == (None if given is None else settings["other_maps"])
    # reloaded from `settings.json`, the settings are the same again, the unset value included
    reloaded = Yosys.Settings.from_input(recorded)
    again = Yosys.Settings.from_input(json.loads(json.dumps(reloaded.model_dump(mode="json"))))
    assert again.model_dump() == reloaded.model_dump()
    assert reloaded.other_maps == again.other_maps


# ------------------------------------------------------------------------ the synthesis options


OPTIMIZE_CHOICES = [
    c for a in get_args(Yosys.Settings.model_fields["optimize"].annotation) for c in get_args(a)
]


def test_every_optimize_choice_is_covered():
    """Every optimize choice is covered."""
    assert set(OPTIMIZE_CHOICES) == {"speed", "area"}


# One command only each target's abc script runs, to tell the two apart.
DISTINCTIVE_ABC_COMMAND = {"area": "map -B 0.9", "speed": "&if -g -K 6"}


def test_every_optimize_target_has_a_distinctive_abc_command():
    """Every optimize target has a distinctive abc command."""
    assert set(DISTINCTIVE_ABC_COMMAND) == set(OPTIMIZE_CHOICES)


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_optimize_selects_a_script_and_post_synthesis_optimization_together(
    optimize, tmp_path, monkeypatch
):
    """What yosys rendered into its script, given `optimize`: the abc script `optimize` selects --
    told apart by a command only that target's script runs -- together with post-synthesis
    optimization.

    No tool runs: this is about the settings and the script, so every command is recorded
    instead, and the run stops once yosys, having produced nothing, fails.
    """
    commands = []

    def record(executable, args=None, **kwargs):
        commands.append([str(executable), *map(str, args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    settings = _tiny(tmp_path, optimize=optimize)
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, _design(tmp_path), settings)
    (settings_json,) = tmp_path.glob("xeda_run/mac/yosys/settings.json")
    yosys_settings = json.loads(settings_json.read_text())["effective_flow_settings"]
    script = (settings_json.parent / "yosys_synth.ys").read_text()
    assert any(Path(cmd[0]).name == "yosys" for cmd in commands), "yosys was never launched"

    assert yosys_settings["optimize"] == optimize
    assert yosys_settings["post_synth_opt"] is True
    assert "opt -full -purge -sat" in script
    (abc,) = [line for line in script.splitlines() if line.startswith("abc ")]
    for target, command in DISTINCTIVE_ABC_COMMAND.items():
        selected = target == optimize
        # `Yosys.Settings` writes a script's commands comma-separated, as `abc -script` wants
        assert (command.replace(" ", ",") in (yosys_settings["abc_script"] or "")) is selected
        assert (command.replace(" ", ",") in abc) is selected
    assert "-script" in abc


def test_optimize_is_a_setting_of_the_flow_that_acts_on_it(tmp_path, monkeypatch):
    """`optimize` acts on yosys's mapping, so it is yosys's setting, and no setting is inert:
    yosys's is read where its script is rendered -- each target renders its own abc script."""
    assert "optimize" in Yosys.Settings.model_fields
    assert "optimize" not in YosysFpga.Settings.model_fields

    def record(executable, args=None, **kwargs):
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    abc_lines = {}
    given = _tiny(tmp_path)
    for optimize in OPTIMIZE_CHOICES:
        run_root = tmp_path / optimize
        with contextlib.suppress(FlowException):
            DefaultRunner(run_root).run_flow(
                Yosys, _design(tmp_path), {**given, "optimize": optimize}
            )
        script = (run_root / "mac" / "yosys" / "yosys_synth.ys").read_text()
        (abc_lines[optimize],) = [line for line in script.splitlines() if line.startswith("abc ")]
        assert DISTINCTIVE_ABC_COMMAND[optimize].replace(" ", ",") in abc_lines[optimize]
    assert len(set(abc_lines.values())) == len(OPTIMIZE_CHOICES)


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_an_optimize_target_selects_an_abc_script_yosys_accepts(optimize):
    """`abc_opt_script` returned nothing at all, so `optimize` never reached abc; and a script
    must be the text `Yosys.Settings.abc_script` declares, which a list of commands is not."""
    script = yosys_flows.abc_opt_script(optimize)
    assert isinstance(script, str) and script.startswith("+")
    assert Yosys.Settings(abc_script=script).abc_script


def test_area_and_speed_select_different_scripts():
    """Area and speed select different scripts."""
    assert yosys_flows.abc_opt_script("area") != yosys_flows.abc_opt_script("speed")


def test_area_plus_speed_is_no_longer_a_target():
    """It selected exactly the area script, so it was "area" under another name."""
    with pytest.raises(ValidationError):
        Yosys.Settings(optimize="area+speed")


def _contains_run(sequence, run):
    """Check whether a generated script invokes a command."""
    return any(sequence[i : i + len(run)] == run for i in range(len(sequence) - len(run) + 1))


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_the_yosys_flow_synthesizes_with_the_selected_script(optimize, tmp_path):
    """End to end, with its own template: abc executes exactly the commands the selected script
    names, in order -- `upsize`/`dnsize` included, which yosys's default mapping never runs."""
    require_yosys()
    design = _design(tmp_path)
    with contextlib.suppress(ExecutableNotFound):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Yosys, design, _tiny(tmp_path, optimize=optimize)
        )

    (yosys_results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    yosys_run = yosys_results.parent
    assert json.loads((yosys_run / "results.json").read_text())["success"] is True
    executed = re.findall(r"^ABC: \+ (.+?)\s*$", (yosys_run / "yosys.log").read_text(), re.M)
    script = Yosys.Settings(abc_script=yosys_flows.abc_opt_script(optimize)).abc_script
    assert script is not None
    selected = [command.replace(",", " ") for command in script[1:].split(";")]
    assert _contains_run(executed, selected), f"abc did not run the {optimize} script"


@pytest.mark.parametrize("optimize", OPTIMIZE_CHOICES)
def test_real_yosys_maps_with_the_selected_script(optimize, tmp_path):
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
    script = Yosys.Settings(abc_script=yosys_flows.abc_opt_script(optimize)).abc_script
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
            "clock.period=2.0",
            "abc_driver_cell=INV_X1",
            "abc_load_in_ff=2.5",
            "black_box=mul8",
            "post_synth_opt=false",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 0 and document["success"], proc.stderr[-3000:] or document
    producer = root / "mac" / "yosys"
    settings = json.loads((producer / "settings.json").read_text())["effective_flow_settings"]
    assert settings["abc_driver_cell"] == "INV_X1"
    assert settings["abc_load_in_ff"] == 2.5
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
    with WorkingDirectory(run):
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
    with WorkingDirectory(tmp_path / "run"):
        flow.init()
    assert flow.settings.abc_script == str(script)
    assert flow.implicit_inputs == [script]
