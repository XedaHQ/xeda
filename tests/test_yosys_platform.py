"""`yosys` maps to a platform through the platform model.

The Liberty files, the maps and the cells come from the model that `prepare_inputs()` builds from
the PDK files of the platform, and every file it reads is a registered input of the run. These
tests use the `tiny` platform of the test resources: a user residue file (`platform_file`) and a
store that holds a copy of the files of the library (`tests/tiny_platform.py`). No test needs a
PDK in the home directory.

Most tests run a fake `yosys` that records its script. A few tests run the real one.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace_inputs import located_input_files, registered_input_files
from xeda.flows import Yosys
from xeda.platforms import PdkNotInstalled
from xeda.utils import WorkingDirectory

from .tiny_platform import TINY_RESIDUE, tiny_settings, tiny_store
from .tool_utils import fake_calls, require_yosys, use_fake_asic_tools

COUNTER = (
    "module top(input clk, input rst, input [3:0] a, output reg [3:0] q);\n"
    "  always @(posedge clk) if (rst) q <= 0; else q <= q + a;\n"
    "endmodule\n"
)
CLOCK = {"clock": {"period": "2 ns"}}


def _design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text(COUNTER)
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )


@pytest.fixture
def fake_yosys(tmp_path, monkeypatch) -> Path:
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    return tmp_path


def _runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False)


def _launch(tmp_path: Path, **given: Any):
    store = given.pop("store", None) or tiny_store(tmp_path)
    flow = _runner(tmp_path).launch_flow(
        Yosys, _design(tmp_path / "design"), tiny_settings(store, **CLOCK, **given)
    )
    return flow, store


def _script(flow) -> str:
    return (flow.run_path / "yosys_synth.ys").read_text()


def _effective(flow) -> dict:
    return json.loads((flow.run_path / "settings.json").read_text())["effective_flow_settings"]


def _flow(tmp_path: Path, store: Path, **given: Any) -> Yosys:
    """A flow that is built and not launched, with its run directory."""
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    settings = Yosys.Settings.from_input(
        tiny_settings(store, **given), design_root=tmp_path / "design"
    )
    return Yosys(settings, _design(tmp_path / "design"), run)


# ------------------------------------------------------------------------------ what it maps to


def test_the_script_maps_to_the_liberty_files_and_cells_of_the_platform(fake_yosys):
    flow, store = _launch(fake_yosys)
    assert flow.succeeded
    effective, script = _effective(flow), _script(flow)
    pdk = store / "tiny"
    assert effective["liberty"] == [str(pdk / "tiny.lib")], "the file of the platform, as it is"
    assert not list(flow.run_path.glob("*.lib")), "the run writes no library"
    assert f"-lib {pdk / 'tiny.lib'}\n" in script
    assert f"dfflibmap -liberty {pdk / 'tiny.lib'}" in script
    assert f"-liberty {pdk / 'tiny.lib'} -dont_use" in script
    assert "-hicell TIEHI_SMALL Z -locell TIELO_X1 Z" in script
    assert "insbuf -buf BUF_X1 A Z" in script
    assert (flow.run_path / "abc.constr").read_text() == "set_driving_cell BUF_X1\nset_load 3.5\n"
    assert f"techmap -map {pdk / 'cells_adders.v'}\n" in script
    assert f"read_verilog -defer {pdk / 'cells_clkgate.v'}\n" in script
    assert f"techmap -map {pdk / 'cells_latch.v'}\n" in script
    assert effective["sequential"]["dff_liberty"] is None, "no flip-flop library of its own"
    assert effective["sequential"]["latch_map"] == "platform"
    assert effective["sequential"]["latch_map_file"] is None, "the platform's map is not a setting"


def _dont_use(flow) -> dict[str, list[str]]:
    """The cells that `-dont_use` words name, on each command of the script that has them."""
    words: dict[str, list[str]] = {}
    for line in _script(flow).splitlines():
        tokens = line.split()
        if tokens and tokens[0] in ("dfflibmap", "abc"):
            words[tokens[0]] = [tokens[i + 1] for i, t in enumerate(tokens) if t == "-dont_use"]
    return words


def test_the_dont_use_cells_of_the_model_and_of_the_user_are_words_of_dfflibmap_and_abc(
    fake_yosys,
):
    """The model lists the cells that the Liberty file marks itself (`INV_X4`, `NAND2_X4`); they
    come first, then a cell of the `dont_use_cells` setting. Each is one `-dont_use` word on
    `dfflibmap` and on `abc`."""
    flow, _ = _launch(fake_yosys, dont_use_cells=["NOR2_X1"])
    cells = ["INV_X4", "NAND2_X4", "NOR2_X1"]
    assert _dont_use(flow) == {"dfflibmap": cells, "abc": cells}


def test_the_dont_use_cells_of_a_user_override_replace_those_of_the_liberty_file(
    fake_yosys,
):
    mine = fake_yosys / "mine.yaml"
    mine.write_text(
        TINY_RESIDUE.read_text()
        + "overrides:\n"
        + "  dont_use_cells: {value: [BUF_X1], reason: the test chooses another cell}\n"
    )
    flow, _ = _launch(fake_yosys, platform_file=str(mine))
    assert _dont_use(flow) == {"dfflibmap": ["BUF_X1"], "abc": ["BUF_X1"]}


def test_a_setting_the_user_gave_wins_over_the_model(fake_yosys):
    flow, _ = _launch(
        fake_yosys,
        abc={"driver_cell": "INV_X1", "load": "1.5 fF"},
        finish={
            "tie_cells": {"hi": ["TIEHI_BIG", "Z"], "lo": ["TIELO_X1", "Z"]},
            "buffer_cell": ["INV_X1", "A", "ZN"],
        },
    )
    script = _script(flow)
    assert (flow.run_path / "abc.constr").read_text() == "set_driving_cell INV_X1\nset_load 1.5\n"
    assert "-hicell TIEHI_BIG Z" in script and "insbuf -buf INV_X1 A ZN" in script


def test_a_liberty_file_the_user_gave_replaces_the_one_of_the_platform(fake_yosys):
    own = fake_yosys / "own.lib"
    own.write_text("library (own) {\n  cell (OWN_CELL) {\n    area : 1;\n  }\n}\n")
    flow, _ = _launch(fake_yosys, liberty=[str(own)])
    assert _effective(flow)["liberty"] == [str(own)]
    script = _script(flow)
    assert f"-lib {own}\n" in script and "tiny.lib" not in script
    assert f"dfflibmap -liberty {own}" in script and "abc -script" in script
    assert "-hicell TIEHI_SMALL Z" in script, "the cells still come from the platform"


@pytest.mark.parametrize("corner", [None, "typical", "tt", "fast", "ff"])
def test_the_corner_selects_the_liberty_files_of_that_corner(fake_yosys, corner):
    store = tiny_store(fake_yosys)
    library = store / "tiny" / "tiny.lib"
    (store / "tiny" / "tiny_fast.lib").write_text(library.read_text().replace("INV_X1", "FAST_X1"))
    residue = fake_yosys / "corners.yaml"
    residue.write_text(
        TINY_RESIDUE.read_text()
        .replace(
            "liberty: {typical: [tiny.lib]}",
            "liberty: {typical: [tiny.lib], fast: [tiny_fast.lib]}",
        )
        .replace("aliases: {tt: typical}", "aliases: {tt: typical, ff: fast}")
    )
    given = {} if corner is None else {"corner": corner}
    flow, _ = _launch(fake_yosys, store=store, platform_file=str(residue), **given)
    (liberty,) = _effective(flow)["liberty"]
    fast = corner in ("fast", "ff")
    assert Path(liberty).name == ("tiny_fast.lib" if fast else "tiny.lib")
    assert ("cell (FAST_X1)" in Path(liberty).read_text()) is fast
    results = json.loads((flow.run_path / "results.json").read_text())
    assert results["platform"]["corner"] == ("fast" if fast else "typical")
    recorded = json.loads((flow.run_path / "trace.json").read_text())["inputs"]
    assert any(Path(path).name == "tiny_fast.lib" for path in recorded) is fast


@pytest.mark.parametrize("generic", [{"gates": ["AND", "OR", "NOT"]}, {"lut": "4"}])
@pytest.mark.parametrize(
    "platform", [{"platform": "nangate45"}, {"platform_file": str(TINY_RESIDUE)}]
)
def test_a_platform_together_with_generic_cells_is_refused(platform, generic):
    """`platform` maps to the standard cells of the platform and `gates` or `lut` map to generic
    cells, so the run cannot honor both. The refusal names both settings."""
    (cells,) = generic
    (selector,) = [name for name in ("platform", "platform_file") if name in platform]
    settings = Yosys.Settings.from_input({**platform, **generic})
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    assert refused.value.errors == [
        (
            cells,
            f"`{cells}` and `{selector}` cannot both be set: `{selector}` maps to the standard "
            f"cells of the platform and `{cells}` maps to generic cells. Set only one of them",
            None,
            "value_error",
        )
    ]


@pytest.mark.parametrize("generic", [{"gates": ["AND", "OR", "NOT"]}, {"lut": "4"}])
def test_excluded_cells_together_with_generic_cells_are_refused(generic):
    """Generic cells are no library cells, so there is nothing to exclude. The refusal names both
    settings."""
    (cells,) = generic
    settings = Yosys.Settings.from_input({**generic, "dont_use_cells": ["BUF_X1"]})
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    assert refused.value.errors == [
        (
            "dont_use_cells",
            f"`dont_use_cells` and `{cells}` cannot both be set: `{cells}` maps to generic cells, "
            "which have no library cells to exclude. Set only one of them",
            None,
            "value_error",
        )
    ]


@pytest.mark.parametrize("generic", [{"gates": ["AND", "OR", "NOT"]}, {"lut": "4"}])
def test_no_excluded_cells_with_generic_cells_is_accepted(generic):
    Yosys.check_settings_supported(Yosys.Settings.from_input({**generic, "dont_use_cells": []}))


def test_generic_cells_without_a_platform_read_no_platform(fake_yosys):
    flow = _runner(fake_yosys).launch_flow(
        Yosys, _design(fake_yosys / "design"), {"gates": ["AND", "OR", "NOT"], **CLOCK}
    )
    assert flow.platform_model is None and flow.implicit_inputs == []
    assert flow.locate_prepared_inputs() == []


# ------------------------------------------------------------------------------ the inputs


def _platform_files(store: Path) -> list[Path]:
    pdk = store / "tiny"
    return [
        pdk / name
        for name in (
            "tiny.lib",
            "cells_latch.v",
            "cells_clkgate.v",
            "cells_adders.v",
            "tiny.tlef",
            "tiny.lef",
        )
    ]


def test_the_registered_inputs_are_the_located_inputs(tmp_path):
    store = tiny_store(tmp_path)
    flow = _flow(tmp_path, store)
    with WorkingDirectory(tmp_path / "run"):
        flow.init()
        flow.prepare_inputs()
    expected = [*_platform_files(store), TINY_RESIDUE]
    assert sorted(registered_input_files(flow)) == sorted(p.resolve() for p in expected)
    assert located_input_files(flow) == registered_input_files(flow)
    assert flow.implicit_inputs == flow.locate_prepared_inputs()


def test_a_flow_that_was_not_initialized_locates_the_same_inputs(tmp_path):
    """A dry run builds a flow and calls `locate_prepared_inputs()` only."""
    store = tiny_store(tmp_path)
    flow = _flow(tmp_path, store)
    located = flow.locate_prepared_inputs()
    assert located == [*_platform_files(store), TINY_RESIDUE]
    assert flow.platform_model is None and flow.implicit_inputs == []


def test_an_abc_script_file_stays_the_abc_script_and_comes_first(tmp_path):
    store = tiny_store(tmp_path)
    script = tmp_path / "map.abc"
    script.write_text("strash; map\n")
    flow = _flow(tmp_path, store, abc_script=str(script))
    with WorkingDirectory(tmp_path / "run"):
        flow.init()
        assert flow.implicit_inputs == [script]
        flow.prepare_inputs()
    assert flow.settings.abc_script == str(script)
    assert flow.implicit_inputs == [script, *_platform_files(store), TINY_RESIDUE]
    assert flow.implicit_inputs.count(script) == 1
    assert flow.locate_prepared_inputs() == flow.implicit_inputs
    flow.platform_model = None  # the platform does not change what `run()` is handed
    assert flow.settings.abc_script == str(script)


def test_a_launch_with_an_abc_script_file_runs_it(fake_yosys):
    script = fake_yosys / "map.abc"
    script.write_text("strash; map\n")
    flow, _ = _launch(fake_yosys, abc_script=str(script))
    assert flow.succeeded
    assert f"-script {script}" in _script(flow)
    recorded = json.loads((flow.run_path / "trace.json").read_text())
    assert str(script.resolve()) in recorded["inputs"]


def test_the_trace_holds_the_platform_files(fake_yosys):
    flow, store = _launch(fake_yosys)
    recorded = json.loads((flow.run_path / "trace.json").read_text())
    names = {Path(path).resolve() for path in recorded["inputs"]}
    for path in [*_platform_files(store), TINY_RESIDUE]:
        assert path.resolve() in names, path


def test_a_changed_platform_file_makes_the_run_stale_in_a_dry_run(fake_yosys):
    flow, store = _launch(fake_yosys)
    runner, design = _runner(fake_yosys), _design(fake_yosys / "design")
    given = tiny_settings(store, **CLOCK)
    _, states = runner.plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "fresh"
    library = store / "tiny" / "tiny.lib"
    library.write_text(library.read_text() + "\n/* edited */\n")
    _, states = runner.plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "stale"
    assert states["yosys"].reason == f"input changed: {library.resolve()}"
    again = _runner(fake_yosys).launch_flow(Yosys, design, given)
    assert again.succeeded and not again.reused


def test_a_map_file_of_the_platform_is_an_input_too(fake_yosys):
    flow, store = _launch(fake_yosys)
    _, states = _runner(fake_yosys).plan_with_freshness(
        Yosys, _design(fake_yosys / "design"), flow_settings=tiny_settings(store, **CLOCK)
    )
    assert states["yosys"].state == "fresh"
    adders = store / "tiny" / "cells_adders.v"
    adders.write_text(adders.read_text() + "// edited\n")
    _, states = _runner(fake_yosys).plan_with_freshness(
        Yosys, _design(fake_yosys / "design"), flow_settings=tiny_settings(store, **CLOCK)
    )
    assert states["yosys"].state == "stale"


def test_a_second_launch_reuses_the_run(fake_yosys):
    first, store = _launch(fake_yosys)
    again, _ = _launch(fake_yosys, store=store)
    assert first.succeeded and again.reused


def test_the_location_of_the_store_is_no_part_of_the_identity(fake_yosys):
    """The same files in another store make the same run. The run is not reused, since the trace
    records the files by the path it read them at."""
    first, _ = _launch(fake_yosys)
    other = tiny_store(fake_yosys, "elsewhere")
    again, _ = _launch(fake_yosys, store=other)
    assert again.flow_hash == first.flow_hash and again.settings_hash == first.settings_hash


# ------------------------------------------------------------------------------ results


def test_results_say_which_platform_the_netlist_is_mapped_to(fake_yosys):
    flow, _ = _launch(fake_yosys)
    results = json.loads((flow.run_path / "results.json").read_text())
    platform = results["platform"]
    assert {key: platform[key] for key in ("name", "provider", "version", "corner")} == {
        "name": "tiny",
        "provider": "directory",
        "version": "unpinned",
        "corner": "typical",
    }
    sources = platform["sources"]
    assert sources["tie_high"] == "derived" and sources["abc_driver_cell"] == "user"
    assert sources["files.liberty"] == "user" and sources["name"] == "user"
    assert "dont_use_cells" in sources


def test_a_run_without_a_platform_has_no_platform_result(fake_yosys):
    flow = _runner(fake_yosys).launch_flow(
        Yosys, _design(fake_yosys / "design"), {"gates": ["AND", "NOT"], **CLOCK}
    )
    assert "platform" not in json.loads((flow.run_path / "results.json").read_text())


# ------------------------------------------------------------------------------ a missing PDK


def _yosys_calls(tmp_path: Path) -> list[list[str]]:
    return [call for call in fake_calls(tmp_path / "xeda_run") if call[:1] == ["yosys"]]


def test_a_missing_pdk_is_refused_before_yosys_runs(fake_yosys):
    empty = fake_yosys / "empty"
    empty.mkdir()
    with pytest.raises(PdkNotInstalled) as refused:
        _launch(fake_yosys, store=empty)
    message = str(refused.value)
    assert "platform 'tiny' needs the PDK files" in message
    assert f"looked in: {empty / 'tiny'}" in message
    assert f"--platform-file {TINY_RESIDUE.resolve()}" in message or "directory" in message
    assert _yosys_calls(fake_yosys) == []
    run = fake_yosys / "xeda_run" / "top" / "yosys"
    results = json.loads((run / "results.json").read_text())
    assert results["success"] is False
    assert results["error"]["code"] == "reported_failure"
    assert results["error"]["message"] == message
    assert not (run / "trace.json").exists()


def test_a_dry_run_shows_a_missing_pdk_as_unknown_with_the_same_message(fake_yosys):
    empty = fake_yosys / "empty"
    empty.mkdir()
    runner = _runner(fake_yosys)
    design = _design(fake_yosys / "design")
    given = tiny_settings(empty, **CLOCK)
    _, states = runner.plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "unknown"
    with pytest.raises(PdkNotInstalled) as refused:
        runner.launch_flow(Yosys, design, given)
    assert str(refused.value) in states["yosys"].reason
    assert not (fake_yosys / "xeda_run").exists() or not list(
        (fake_yosys / "xeda_run").rglob("fake_*.calls")
    )


def test_a_bundled_platform_that_is_not_installed_names_the_install_command(
    fake_yosys, monkeypatch
):
    monkeypatch.setenv("HOME", str(fake_yosys / "home"))
    with pytest.raises(PdkNotInstalled) as refused:
        _runner(fake_yosys).launch_flow(
            Yosys, _design(fake_yosys / "design"), {"platform": "nangate45", **CLOCK}
        )
    assert "xeda pdk install nangate45" in str(refused.value)
    assert _yosys_calls(fake_yosys) == []


# ------------------------------------------------------------------------------ the settings


def test_a_platform_file_alone_passes_planning(fake_yosys):
    store = tiny_store(fake_yosys)
    plan = _runner(fake_yosys).plan(
        Yosys, _design(fake_yosys / "design"), flow_settings=tiny_settings(store, **CLOCK)
    )
    (node,) = plan.nodes
    assert node.settings.platform is None
    assert node.settings.pdk_provider == "directory" and node.settings.pdk_version == "unpinned"


def test_a_platform_file_alone_launches(fake_yosys):
    flow, _ = _launch(fake_yosys)
    assert flow.succeeded and flow.settings.platform is None


def test_the_platform_and_the_file_must_name_the_same_platform(fake_yosys):
    store = tiny_store(fake_yosys)
    with pytest.raises(Exception, match="names the platform 'tiny'"):
        _runner(fake_yosys).plan(
            Yosys,
            _design(fake_yosys / "design"),
            flow_settings=tiny_settings(store, platform="nangate45", **CLOCK),
        )


def test_a_corner_that_the_platform_lacks_is_refused_at_planning(fake_yosys):
    store = tiny_store(fake_yosys)
    with pytest.raises(Exception, match="nonexistent"):
        _runner(fake_yosys).plan(
            Yosys,
            _design(fake_yosys / "design"),
            flow_settings=tiny_settings(store, corner="nonexistent", **CLOCK),
        )


@pytest.mark.parametrize("name", ["driver_cell", "load"])
def test_an_abc_cell_setting_without_a_liberty_mapping_names_the_ways_to_map(name):
    value = "BUF_X1" if name == "driver_cell" else "1.5 fF"
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(Yosys.Settings.from_input({"abc": {name: value}}))
    for text in ("`platform`", "`platform_file`", "`liberty`"):
        assert text in str(refused.value)


@pytest.mark.parametrize(
    "given",
    [{"platform": "nangate45"}, {"platform_file": str(TINY_RESIDUE)}, {"liberty": ["a.lib"]}],
)
def test_the_abc_cell_settings_are_accepted_with_a_liberty_mapping(given):
    settings = Yosys.Settings.from_input({"abc": {"driver_cell": "BUF_X1"}, **given})
    Yosys.check_settings_supported(settings)


# ------------------------------------------------------------------------------ real yosys


def test_real_yosys_maps_a_counter_to_the_cells_of_the_platform(tmp_path):
    require_yosys()
    store = tiny_store(tmp_path)
    flow = _runner(tmp_path).launch_flow(
        Yosys, _design(tmp_path / "design"), tiny_settings(store, **CLOCK)
    )
    assert flow.succeeded
    results = json.loads((flow.run_path / "results.json").read_text())
    netlist = (flow.run_path / "netlist.v").read_text()
    cells = set(re.findall(r"^\s*(\w+)\s+\S+\s*\(", netlist, re.M)) - {"module"}
    assert cells and cells <= {"INV_X1", "NAND2_X1", "NOR2_X1", "DFF_X1", "BUF_X1"}, cells
    assert not re.search(r"^\s*\\\$_", netlist, re.M), "a generic cell is left in the netlist"
    assert not cells & {"INV_X4", "NAND2_X4"}, "a dont-use cell was used"
    assert results["platform"]["name"] == "tiny"
