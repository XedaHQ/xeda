"""The fake OpenROAD (`tests/fake_tools/openroad`) runs the Tcl that Xeda renders under `tclsh`.

It stands in for OpenROAD in tests that render a driver script and need a run to end. It knows
exactly the commands of `tool_utils.OPENROAD_COMMANDS`: a command outside that list is a Tcl
error, so a typo, or a command that OpenROAD lacks, fails a test and never passes silently.

The recorded metrics in `tests/resources/openroad/metrics/` are the metrics of a real OpenROAD
run of the gcd design on nangate45, split by stage prefix: one file for each driver script.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.tool import VERSION_REGEXP2

from . import tool_utils
from .tool_utils import OPENROAD_COMMANDS, fake_calls, require_tclsh, use_fake_openroad

FAKE = tool_utils.FAKE_TOOLS_DIR / "openroad"
PRELUDE = tool_utils.FAKE_TOOLS_DIR / "fake_openroad_prelude.tcl"
METRICS_DIR = Path(__file__).parent / "resources" / "openroad" / "metrics"
DRIVERS = ("openroad_place", "openroad_cts", "openroad_grt", "openroad_route", "openroad_final")


def run_fake(
    work: Path,
    script: str,
    *,
    driver: str = "openroad_place",
    metrics: str | None = "metrics.json",
) -> subprocess.CompletedProcess[str]:
    """Run the fake the way the flows do, in `work`, on the script `<driver>.tcl`."""
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{driver}.tcl").write_text(script)
    command = [sys.executable, str(FAKE), "-no_init", "-no_splash", "-threads", "2", "-exit"]
    if metrics is not None:
        command += ["-metrics", metrics]
    command.append(f"{driver}.tcl")
    return subprocess.run(command, cwd=work, capture_output=True, text=True, check=False)


def tree(root: Path) -> list[str]:
    """Every path under `root`, relative to it."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_the_command_list_has_no_repeats():
    assert len(set(OPENROAD_COMMANDS)) == len(OPENROAD_COMMANDS)
    assert all(re.fullmatch(r"[a-z_0-9]+(::[A-Za-z_0-9]+)*", name) for name in OPENROAD_COMMANDS)
    # the commands that need a namespace, and the ones the templates compute with
    for name in (
        "utl::set_metrics_stage",
        "gpl::get_global_placement_uniform_density",
        "ord::get_db",
        "ord::get_db_block",
        "odb::dbObstruction_destroy",
        "grt::have_routes",
        "ord::openroad_gui_compiled",
    ):
        assert name in OPENROAD_COMMANDS


def test_the_prelude_defines_every_command(tmp_path):
    require_tclsh()
    script = (
        "set missing {}\n"
        f"foreach name {{{' '.join(OPENROAD_COMMANDS)}}} {{\n"
        "  if {[llength [info commands $name]] == 0} { lappend missing $name }\n"
        "}\n"
        "set f [open missing.txt w]; puts $f $missing; close $f\n"
        # the standard `unknown` of tclsh would let a script run a program of the same name
        "set f [open unknown.txt w]; puts $f [llength [info commands unknown]]; close $f\n"
    )
    result = run_fake(tmp_path / "run", script)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "run" / "missing.txt").read_text().strip() == ""
    assert (tmp_path / "run" / "unknown.txt").read_text().strip() == "0"


def test_a_typo_is_a_tcl_error(tmp_path):
    require_tclsh()
    run = tmp_path / "run"
    result = run_fake(run, "read_db a.odb\nglobal_placment -density 0.5\nwrite_db late.odb\n")
    assert result.returncode != 0
    assert 'invalid command name "global_placment"' in result.stderr
    assert not (run / "late.odb").exists()
    log = (run / "fake_openroad.calls").read_text()
    assert re.search(r'^TCL-ERROR invalid command name "global_placment"$', log, re.M)
    with pytest.raises(pytest.fail.Exception, match="global_placment"):
        fake_calls(run)


def test_a_script_is_recorded_and_its_files_are_written(tmp_path):
    require_tclsh()
    base = tmp_path / "base"
    run = base / "run"
    script = """
utl::set_metrics_stage "floorplan__{}"
set db [ord::get_db]
foreach lib [$db getLibs] { foreach m [$lib getMasters] { $m getType; $m getName } }
foreach o [[ord::get_db_block] getObstructions] { odb::dbObstruction_destroy $o }
write_db place.odb
write_sdc -no_timestamp place.sdc
write_verilog final.v -remove_cells [list FILL_X1 TAP_X1]
write_def -version 5.8 final.def
write_spef final.spef
write_guides route.guide
write_cdl -masters masters.cdl final.cdl
write_sdf -divider / -digits 4 final.sdf
write_abstract_lef block.lef
write_timing_model -library_name lib block.lib
set f [open results.txt w]
puts $f [all_pins_placed],[design_is_routed],[check_antennas],[repair_antennas]
puts $f [grt::have_routes],[ord::openroad_gui_compiled],[gpl::get_global_placement_uniform_density -pad_left 0]
puts $f [expr {[gpl::get_global_placement_uniform_density] + 0.25}]
close $f
"""
    result = run_fake(run, script)
    assert result.returncode == 0, result.stderr
    assert fake_calls(run) == [
        ["utl::set_metrics_stage", "floorplan__{}"],
        ["ord::get_db"],
        ["ord::get_db_block"],
        ["write_db", "place.odb"],
        ["write_sdc", "-no_timestamp", "place.sdc"],
        ["write_verilog", "final.v", "-remove_cells", "FILL_X1 TAP_X1"],
        ["write_def", "-version", "5.8", "final.def"],
        ["write_spef", "final.spef"],
        ["write_guides", "route.guide"],
        ["write_cdl", "-masters", "masters.cdl", "final.cdl"],
        ["write_sdf", "-divider", "/", "-digits", "4", "final.sdf"],
        ["write_abstract_lef", "block.lef"],
        ["write_timing_model", "-library_name", "lib", "block.lib"],
        ["all_pins_placed"],
        ["design_is_routed"],
        ["check_antennas"],
        ["repair_antennas"],
        ["grt::have_routes"],
        ["ord::openroad_gui_compiled"],
        ["gpl::get_global_placement_uniform_density", "-pad_left", "0"],
        ["gpl::get_global_placement_uniform_density"],
    ]
    # the file of each writer is the file the command names, with its words and nothing else
    expect = {
        "place.odb": "write_db place.odb",
        "place.sdc": "write_sdc -no_timestamp place.sdc",
        "final.v": "write_verilog final.v -remove_cells {FILL_X1 TAP_X1}",
        "final.def": "write_def -version 5.8 final.def",
        "final.spef": "write_spef final.spef",
        "route.guide": "write_guides route.guide",
        "final.cdl": "write_cdl -masters masters.cdl final.cdl",
        "final.sdf": "write_sdf -divider / -digits 4 final.sdf",
        "block.lef": "write_abstract_lef block.lef",
        "block.lib": "write_timing_model -library_name lib block.lib",
    }
    for name, text in expect.items():
        assert (run / name).read_text() == text + "\n", name
    assert (run / "results.txt").read_text().splitlines() == ["0,1,0,0", "1,1,0.5", "0.75"]
    assert (run / "metrics.json").read_bytes() == (METRICS_DIR / "openroad_place.json").read_bytes()
    # nothing outside the working directory, and nothing the script did not ask for
    in_run = sorted([*expect, "results.txt", "metrics.json", "openroad_place.tcl"])
    assert tree(run) == sorted([*in_run, "fake_openroad.calls"])
    assert tree(tmp_path) == sorted(["base", "base/run", *[f"base/run/{n}" for n in tree(run)]])


def test_the_files_do_not_depend_on_the_directory(tmp_path):
    require_tclsh()
    script = "write_db [file join [pwd] a.odb]\nwrite_sdc -no_timestamp [pwd]/a.sdc\n"
    first, second = tmp_path / "first", tmp_path / "second" / "deeper"
    assert run_fake(first, script).returncode == 0
    assert run_fake(second, script).returncode == 0
    for name, text in (("a.odb", "write_db ./a.odb"), ("a.sdc", "write_sdc -no_timestamp ./a.sdc")):
        assert (first / name).read_text() == (second / name).read_text() == text + "\n"


def test_a_file_name_through_an_alias_of_the_directory(tmp_path):
    """On macOS the temporary directory is `/private/var/...` and also `/var/...`."""
    require_tclsh()
    run = tmp_path / "run"
    run.mkdir()
    real = str(run.resolve())
    alias = real.removeprefix("/private")
    if alias == real:
        pytest.skip("this system has no alias of the temporary directory")
    assert run_fake(run, f"write_db {alias}/a.odb\nwrite_db {real}/b.odb\n").returncode == 0
    assert (run / "a.odb").read_text() == "write_db ./a.odb\n"
    assert (run / "b.odb").read_text() == "write_db ./b.odb\n"


def test_a_program_is_not_a_command(tmp_path):
    require_tclsh()
    result = run_fake(tmp_path / "run", "ls\n")
    assert result.returncode != 0 and 'invalid command name "ls"' in result.stderr


def test_a_writer_does_not_create_a_directory(tmp_path):
    require_tclsh()
    result = run_fake(tmp_path / "run", "write_db missing/place.odb\n")
    assert result.returncode != 0
    assert "missing/place.odb" in result.stderr
    assert not (tmp_path / "run" / "missing").exists()


def test_a_failing_command_raises_after_it_is_recorded(tmp_path, monkeypatch):
    require_tclsh()
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "clock_tree_synthesis")
    run = tmp_path / "run"
    script = """
write_db before.odb
if {[catch { clock_tree_synthesis -sink_clustering_enable } message options]} {
  catch { write_db failed.odb }
  return -options $options $message
}
write_db after.odb
"""
    result = run_fake(run, script, driver="openroad_cts")
    assert result.returncode != 0
    assert "clock_tree_synthesis failed" in result.stderr
    assert (run / "before.odb").is_file() and (run / "failed.odb").is_file()
    assert not (run / "after.odb").exists()
    log = (run / "fake_openroad.calls").read_text()
    assert "ARG clock_tree_synthesis\nARG -sink_clustering_enable\n" in log
    assert "TCL-ERROR clock_tree_synthesis failed" in log
    # the metrics file is written when a script fails, as OpenROAD writes it when it exits
    assert (run / "metrics.json").read_bytes() == (METRICS_DIR / "openroad_cts.json").read_bytes()


def test_another_directory_of_metrics(tmp_path, monkeypatch):
    require_tclsh()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "openroad_cts.json").write_text('{"cts__made_up": 1}\n')
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(elsewhere))
    run = tmp_path / "run"
    assert run_fake(run, "write_db a.odb\n", driver="openroad_cts").returncode == 0
    assert (run / "metrics.json").read_text() == '{"cts__made_up": 1}\n'
    # a driver with no recorded file writes no metrics file
    other = tmp_path / "other"
    assert run_fake(other, "write_db a.odb\n", driver="openroad_grt").returncode == 0
    assert not (other / "metrics.json").exists()


def test_no_metrics_file_without_the_option(tmp_path):
    require_tclsh()
    run = tmp_path / "run"
    assert run_fake(run, "write_db a.odb\n", metrics=None).returncode == 0
    assert not list(run.glob("*.json"))


def test_return_values_and_overrides(tmp_path, monkeypatch):
    require_tclsh()
    script = """
set f [open out.txt w]
puts $f "[get_lib_cell INV_X1]|[get_name [get_property [lindex [get_lib_cell INV_X1] 0] library]]"
puts $f "[all_pins_placed]|[repair_antennas]|[get_clocks *]|[estimate_parasitics -placement]"
foreach lib [[ord::get_db] getLibs] {
  foreach m [$lib getMasters] { puts $f "[$m getName] [$m getType]" }
}
puts $f [llength [[ord::get_db_block] getObstructions]]
close $f
"""
    plain = run_fake(tmp_path / "plain", script)
    assert plain.returncode == 0, plain.stderr
    lines = (tmp_path / "plain" / "out.txt").read_text().splitlines()
    assert lines[1] == "0|0||" and lines[2:] == ["0"]
    # the value of the library name builds `<lib>/<cell>/<port>` as the templates do
    assert re.fullmatch(r"\S+\|\S+", lines[0])

    tool_utils.fake_returns(
        monkeypatch,
        {
            ("all_pins_placed",): "1",
            ("getLibs",): "::fake_lib",
            ("getMasters",): "::fake_master",
            ("getObstructions",): "::fake_obstruction",
        },
    )
    changed = run_fake(tmp_path / "changed", script)
    assert changed.returncode == 0, changed.stderr
    lines = (tmp_path / "changed" / "out.txt").read_text().splitlines()
    assert lines[1].startswith("1|")
    assert lines[2] == "FAKE_MASTER CORE" and lines[3] == "1"


def test_the_fake_is_found_through_the_path(tmp_path, monkeypatch):
    use_fake_openroad(monkeypatch)
    found = shutil.which("openroad")
    assert found is not None and Path(found).resolve() == (FAKE.parent / "fake_tool.py").resolve()
    probed = subprocess.run(
        ["openroad", "-version"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert VERSION_REGEXP2.search(probed), probed
    assert tree(tmp_path) == []


def test_the_fake_does_not_accept_an_option_the_flows_must_not_pass(tmp_path):
    require_tclsh()
    (tmp_path / "x.tcl").write_text("")
    result = subprocess.run(
        [sys.executable, str(FAKE), "-no_init", "-log", "x.log", "x.tcl"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "x.log").exists()


@pytest.mark.parametrize("driver", DRIVERS)
def test_the_recorded_metrics(driver):
    metrics = json.loads((METRICS_DIR / f"{driver}.json").read_text())
    assert isinstance(metrics, dict) and metrics
    stages = {
        "openroad_place": {
            "synth",
            "floorplan",
            "globalplace_skip_io",
            "io_placement",
            "globalplace",
            "placeopt",
            "detailedplace",
        },
        "openroad_cts": {"cts"},
        "openroad_grt": {"globalroute"},
        "openroad_route": {"detailedroute", "fillcell"},
        "openroad_final": {"finish"},
    }[driver]
    assert {key.split("__")[0] for key in metrics} <= stages | {"run"}
    assert any(key.startswith("run__") for key in metrics)
    assert all(isinstance(value, (int, float, str)) for value in metrics.values())
    text = (METRICS_DIR / f"{driver}.json").read_text()
    assert text == json.dumps(metrics, indent=2, sort_keys=True) + "\n"


def test_the_recorded_metrics_hold_the_values_of_the_real_run():
    def load(driver: str) -> dict:
        return json.loads((METRICS_DIR / f"{driver}.json").read_text())

    assert load("openroad_final")["finish__timing__setup__ws"] == -0.158984
    assert load("openroad_final")["finish__design__instance__area__stdcell"] == 907.592
    # the stages split the run: no key but the run's own is in two files
    keys = [set(load(driver)) for driver in DRIVERS]
    shared = set.intersection(*keys)
    assert shared and all(key.startswith("run__") for key in shared)
    assert sum(len(k) for k in keys) - len(shared) * (len(keys) - 1) == len(set().union(*keys))
    assert os.path.isfile(PRELUDE)
