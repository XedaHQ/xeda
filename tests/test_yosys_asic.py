"""`yosys` owns its ASIC configuration.

`openroad` used to build its `yosys` dependency's whole configuration -- the liberty set and
its merge, the mapping cells, the abc script and constraints, the netlist format -- from its
own `platform`. A producer's settings must not depend on which consumer asked, so `yosys`
derives all of it from a `platform` of its own: `xeda run yosys -s platform=nangate45` alone maps
to Nangate45 cells and needs nothing from a consumer. `optimize`, `abc_driver_cell` and
`abc_load_in_ff` are settings of the flow that acts on them, `yosys`, and `openroad` no longer
has them (`test_removed_settings.py` checks the tombstones from every origin).

Two liberty bugs are pinned here: a platform's liberty set is always merged into
one library, named `<platform>_merged` as `openroad` names its own, and the platform's own
`dont_use_cells` are marked `dont_use` in it beside the setting's.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.dataclass import ValidationError
from xeda.design import SourceType
from xeda.flow import FlowException, FlowSettingsError
from xeda.flow.io import declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openroad, Yosys, YosysFpga
from xeda.flows import yosys as yosys_flows
from xeda.platforms import AsicsPlatform

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


# ------------------------------------------------------------------------------ the output


def test_yosys_declares_its_verilog_netlist():
    """The gate-level netlist `openroad` reads is a declared output, switched on by the setting
    that names it, whose default writes it."""
    netlist = declared_outputs(Yosys)["netlist"]
    assert netlist.types == (SourceType.VerilogNetlist,)
    assert netlist.enabled_by == "netlist_verilog"
    assert Yosys.Settings().netlist_verilog == Path("netlist.v")


def test_stop_after_rtl_with_a_netlist_is_refused_before_anything_runs(tmp_path):
    """`stop_after: rtl` writes no netlist, so a request that also asks for one (the default)
    cannot succeed: it is refused when planned, saying what to write instead."""
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsError, match="netlist_verilog"):
        runner.plan(Yosys, _design(tmp_path / "design"), flow_settings=["stop_after=rtl"])
    assert runner.plan(
        Yosys, _design(tmp_path / "other"), flow_settings=["stop_after=rtl", "netlist_verilog="]
    )


# ------------------------------------------------------------------------------ the settings


def test_abc_driver_cell_is_a_cell_name():
    """A cell name is text: `BUF_X1` validates, and a number is not a cell name."""
    assert Yosys.Settings(abc_driver_cell="BUF_X1").abc_driver_cell == "BUF_X1"
    with pytest.raises(ValidationError):
        Yosys.Settings(abc_driver_cell=1)


def test_the_moved_settings_are_yosys_s_and_nowhere_else():
    for name in ("optimize", "abc_driver_cell", "abc_load_in_ff"):
        assert name in Yosys.Settings.model_fields, name
        assert name not in Openroad.Settings.model_fields, name
        assert name not in YosysFpga.Settings.model_fields, name
    assert Yosys.Settings().optimize == "area"
    with pytest.raises(ValidationError):
        Yosys.Settings(optimize="area+speed")


def test_corner_selects_a_corner_of_a_private_copy_of_the_platform():
    """A validator copies a nested model before it changes it: the caller's platform keeps its
    own corner."""
    platform = AsicsPlatform.from_resource("nangate45")
    corners = list(platform.corner)
    before = platform.model_dump()
    settings = Yosys.Settings(platform=platform, corner=corners[0])
    assert settings.platform is not platform
    assert settings.platform.default_corner == corners[0]
    assert platform.model_dump() == before


def test_corner_needs_a_platform_and_one_of_its_corners():
    with pytest.raises(ValidationError, match="platform"):
        Yosys.Settings(corner="tt")
    with pytest.raises(ValidationError, match="Available corners"):
        Yosys.Settings(platform="nangate45", corner="nonexistent")


# ------------------------------------------------------------------- what a platform derives


def _stage_platform(root: Path, dont_use_cells: list[str]) -> Path:
    """A platform whose liberty set is two files, each holding one cell, and whose own
    dont-use list is `dont_use_cells`: `sky130hs`'s shape when that list is empty."""
    root.mkdir(parents=True)
    for name in ("a", "b"):
        (root / f"{name}.lib").write_text(
            f"library ({name}) {{\n  cell (CELL_{name.upper()}) {{\n    area : 1;\n  }}\n}}\n"
        )
    (root / "cells.lef").write_text("")
    (root / "config.toml").write_text(
        'name = "tiny"\nsc_lef = "cells.lef"\n'
        f"dont_use_cells = {json.dumps(dont_use_cells)}\n"
        'abc_driver_cell = "CELL_A"\nabc_load_in_ff = 1.5\n'
        '[corner.tt]\nlib_files = ["a.lib", "b.lib"]\n'
    )
    return root / "config.toml"


@pytest.mark.parametrize("own", [[], ["CELL_B"]], ids=["platform-only", "both"])
@pytest.mark.parametrize("platform_s", [[], ["CELL_A"]], ids=["none", "platform-s"])
def test_a_platform_s_liberty_set_is_always_merged_with_both_dont_use_lists(
    tmp_path, monkeypatch, own, platform_s
):
    """Two bugs: yosys merged a platform's liberty files only when `dont_use_cells` was
    set, so a platform with an empty list (`sky130hs`) reached abc as several files, of which the
    script hands abc the first; and it ignored the platform's own dont-use list, which its
    description promised."""
    config = _stage_platform(tmp_path / "tiny", platform_s)
    settings = {"platform": str(config), "clock": {"period": 2.0}, "dont_use_cells": own}
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


def test_a_platform_alone_configures_everything_openroad_imposed(tmp_path, monkeypatch):
    """Everything `openroad` imposed on its dependency follows from `platform` alone: the
    merged library and the platform's dff library, the mapping files, tie and buffer cells,
    flattening, the abc script `optimize` selects with post-synthesis optimization, the abc
    constraints from the platform's driver cell and load, and a netlist without attributes."""
    run_dir = _scripted_launch(
        tmp_path, monkeypatch, Yosys, {"platform": "nangate45", "clock": {"period": 2.0}}
    )
    platform = AsicsPlatform.from_resource("nangate45")
    corner = platform.default_corner_settings
    effective = _effective(run_dir)
    script = _script(run_dir)
    assert effective["flatten"] is True and "flatten" in script.split()
    assert effective["adder_map"] == str(platform.adder_map_file)
    assert effective["clockgate_map"] == (
        str(platform.clkgate_map_file) if platform.clkgate_map_file else None
    )
    assert effective["other_maps"] == [str(platform.latch_map_file)]
    assert effective["hilomap"]["hi"] == [platform.tiehi_cell, platform.tiehi_port]
    assert effective["hilomap"]["lo"] == [platform.tielo_cell, platform.tielo_port]
    assert effective["insbuf"] == [platform.min_buf_cell, *platform.min_buf_ports]
    assert effective["dff_liberty"] == (str(corner.dff_lib_file) if corner.dff_lib_file else None)
    assert "opt -full -purge -sat" in script
    assert yosys_flows.abc_opt_script("area").replace(" ", ",") in script
    assert (run_dir / "abc.constr").read_text() == (
        f"set_driving_cell {platform.abc_driver_cell}\nset_load {platform.abc_load_in_ff}\n"
    )
    (write,) = [line for line in script.splitlines() if line.startswith("write_verilog")]
    for flag in ("-noattr", "-nohex", "-nodec", "-noexpr"):
        assert flag in write.split(), flag
    # the merged library is the one openroad makes from the same platform: the same bytes
    merged = run_dir / effective["liberty"][0]
    reference = tmp_path / "reference.lib"
    yosys_flows.preproc_libs(
        corner.lib_files, reference, platform.dont_use_cells, "nangate45_merged"
    )
    assert merged.read_bytes() == reference.read_bytes()


def test_explicit_settings_win_over_what_the_platform_derives(tmp_path, monkeypatch):
    settings = {
        "platform": "nangate45",
        "clock": {"period": 2.0},
        "optimize": "speed",
        "abc_driver_cell": "BUF_X4",
        "abc_load_in_ff": 2.5,
        "flatten": False,
        "netlist_attrs": True,
    }
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = _script(run_dir)
    assert yosys_flows.abc_opt_script("speed").replace(" ", ",") in script
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell BUF_X4\nset_load 2.5\n"
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


# ------------------------------------------------------------------ openroad's dependency


def test_openroad_s_dependency_reads_flows_yosys_and_nothing_in_openroad_s_directory(
    tmp_path, monkeypatch
):
    """`openroad` hands its dependency its platform and its shared settings, and nothing it
    made: the moved settings, given where they now live (`flows.yosys`), reach the script, and
    the library yosys reads is its own merge, the same bytes as `openroad`'s `merged.lib`."""
    sections = {"yosys": {"optimize": "speed", "abc_driver_cell": "BUF_X4"}}
    run_dir = _scripted_launch(
        tmp_path,
        monkeypatch,
        Openroad,
        {"platform": "nangate45", "clock": {"period": 2.0}},
        sections,
    )
    script = _script(run_dir)
    assert yosys_flows.abc_opt_script("speed").replace(" ", ",") in script
    assert (run_dir / "abc.constr").read_text().startswith("set_driving_cell BUF_X4\n")
    openroad_dir = run_dir.parent / "openroad"
    assert str(openroad_dir) not in script
    (liberty,) = _effective(run_dir)["liberty"]
    assert (run_dir / liberty).parent == run_dir
    assert (run_dir / liberty).is_file()
    # A failed producer never starts OpenROAD, so no consumer library is created.
    assert not (openroad_dir / "merged.lib").exists()


# ------------------------------------------------------------------ with the real yosys


def test_yosys_with_a_platform_alone_maps_to_its_cells(tmp_path):
    """`xeda run yosys -s platform=nangate45`, alone: Nangate45 cells, merged in its own run
    directory, one run directory, and the declared netlist recorded."""
    require_yosys()
    design = tmp_path / "design"
    _design(design)
    (design / "design.yaml").write_text(
        'name: mac\nrtl:\n  sources: ["mac.v"]\n  top: mac\n  clock:\n    port: clk\n'
    )
    run_root = tmp_path / "xeda_run"
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
            "platform=nangate45",
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
    assert "_X1 " in text or "_X2 " in text, "no Nangate45 cells in the netlist"
    assert "$_" not in text, "an unmapped generic cell is left in the netlist"
    (merged,) = [p for p in run_dir.glob("*.lib")]
    assert "library (nangate45_merged)" in merged.read_text()


# ------------------------------------------------------------------ post_synth_opt


@pytest.mark.parametrize("given", [None, True, False])
@pytest.mark.parametrize("optimize", ["area", None])
def test_an_explicit_post_synth_opt_wins_over_what_optimize_derives(
    tmp_path, monkeypatch, given, optimize
):
    """`optimize` turns post-synthesis optimization on only where `post_synth_opt` is left unset:
    `post_synth_opt: false` with the default `optimize: area` keeps the abc script and drops the
    extra optimization, and `true` adds it with `optimize: null`."""
    settings = {"platform": "nangate45", "clock": {"period": 2.0}, "optimize": optimize}
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
    settings = {"platform": "nangate45", "clock": {"period": 2.0}}
    if given is not None:
        (tmp_path / "extra.v").write_text("module _unused(); endmodule\n")
        settings["other_maps"] = [str(tmp_path / "extra.v") for _ in given]
    latch = str(AsicsPlatform.from_resource("nangate45").latch_map_file)
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


# ------------------------------------------------------------------ asap7's SS corner

#: A `config.toml` of an asap7 platform with its liberty files present (the package ships the
#: description, not the libraries). Opt-in: the test below is skipped without it.
ASAP7_PLATFORM = os.environ.get("XEDA_TESTS_ASAP7_PLATFORM", "")


def _abc_failure(run_dir: Path) -> list[str]:
    """ABC's own failure lines in yosys's log, without the temporary paths they name."""
    log_text = (run_dir / "yosys.log").read_text()
    lines = [
        line
        for line in log_text.splitlines()
        if "Assertion failed" in line or line.startswith("ERROR")
    ]
    return [re.sub(r"\S*yosys-abc-\S+", "<abc>", line) for line in lines]


def test_asap7_ss_hands_abc_the_same_inputs_standalone_and_under_openroad(tmp_path):
    """The `platform=asap7 -s corner=SS` variant, with the real yosys and the real asap7
    libraries: `yosys` alone and `openroad`'s dependency hand abc identical inputs -- the merged
    library, the flip-flop library, the abc constraints and script -- and end alike.

    Exception: with this design the SS corner crashes yosys's ABC
    (`Abc_NtkCheck ... A CI/CO pair share the name`, an assertion in `abcFanio.c`) on
    `origin/main` as well, so no netlist can be compared. Identical ABC inputs and an identical
    failure are accepted as passing the comparison for this variant; the crash is recorded
    separately. If a yosys release fixes it, both sides must produce the same netlist instead."""
    require_yosys()
    if not ASAP7_PLATFORM or not Path(ASAP7_PLATFORM).is_file():
        pytest.skip("set XEDA_TESTS_ASAP7_PLATFORM to an asap7 config.toml with its libraries")
    design = _design(tmp_path / "design")
    settings = {"platform": ASAP7_PLATFORM, "clock": {"period": 2.0}, "corner": "SS"}
    runs = {}
    for flow in (Yosys, Openroad):
        run_root = tmp_path / flow.name
        with contextlib.suppress(Exception):
            DefaultRunner(run_root, display_results=False).run_flow(flow, design, dict(settings))
        runs[flow.name] = run_root / "mac" / "yosys"
    alone, under = runs["yosys"], runs["openroad"]

    def handed(run_dir: Path) -> dict[str, bytes]:
        effective = _effective(run_dir)
        (liberty,) = effective["liberty"]
        script = _script(run_dir).replace(str(run_dir.parent.parent.parent), "<RUNROOT>")
        return {
            "liberty": (run_dir / liberty).read_bytes(),
            "dff_liberty": Path(effective["dff_liberty"]).read_bytes(),
            "abc.constr": (run_dir / "abc.constr").read_bytes(),
            "script": script.encode(),
        }

    assert handed(alone) == handed(under)
    outcomes = {
        name: json.loads((run_dir / "results.json").read_text())["success"]
        for name, run_dir in runs.items()
    }
    assert outcomes["yosys"] == outcomes["openroad"], outcomes
    if outcomes["yosys"]:
        assert (alone / "netlist.v").read_bytes() == (under / "netlist.v").read_bytes()
    else:
        assert _abc_failure(alone) == _abc_failure(under) != []
