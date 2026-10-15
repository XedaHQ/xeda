"""The OpenROAD flows against the real OpenROAD.

The layer is opt-in. `XEDA_TESTS_OPENROAD=1` asks for it, and then an `openroad` on `PATH` must
run (`require_openroad` in `tool_utils.py`). The tests that load a platform also need a PDK
store, as the platform conformance tests do (`XEDA_TESTS_PDK_ROOT`, `XEDA_TESTS_PLATFORMS`).

The first group of tests runs without OpenROAD: it checks the reader that finds the flags in a
Tcl script and the guard of the layer. The other groups skip without their variables.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad import OpenroadPlace
from xeda.run_root import ensure_run_root

from .openroad_flags import (
    Installed,
    Rendered,
    flags_of,
    group_flags,
    groups_of,
    FLOWS,
    help_flags,
    pairs_of_script,
    pairs_of_template,
    read_installed,
    render_variants,
    template_pairs,
)
from .openroad_utils import render_driver
from .tool_utils import (
    OPENROAD_COMMANDS,
    pdk_test_platforms,
    require_openroad,
    require_pdk_store,
    require_yosys,
)

# ---------------------------------------------------------------------------- the guard


def fake_openroad(directory: Path, body: str) -> None:
    """Write an `openroad` into `directory` that runs the shell script `body`."""
    directory.mkdir(exist_ok=True)
    path = directory / "openroad"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_the_layer_skips_without_its_variable(monkeypatch):
    monkeypatch.delenv("XEDA_TESTS_OPENROAD", raising=False)
    with pytest.raises(pytest.skip.Exception, match="XEDA_TESTS_OPENROAD"):
        require_openroad()


def test_the_layer_fails_when_asked_for_and_openroad_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("XEDA_TESTS_OPENROAD", "1")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(pytest.fail.Exception, match="no `openroad` is on PATH"):
        require_openroad()


@pytest.mark.parametrize(
    "body",
    [
        "exit 3",
        '[ "$1" = -version ] && exit 0; exit 1',
        '[ "$1" = -version ] && exit 3; exit 0',
    ],
)
def test_the_layer_fails_when_openroad_does_not_run(monkeypatch, tmp_path, body):
    fake_openroad(tmp_path / "bin", body)
    monkeypatch.setenv("XEDA_TESTS_OPENROAD", "1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    with pytest.raises(pytest.fail.Exception, match="does not run"):
        require_openroad()


def test_the_layer_returns_the_openroad_that_runs(monkeypatch, tmp_path):
    fake_openroad(tmp_path / "bin", "exit 0")
    monkeypatch.setenv("XEDA_TESTS_OPENROAD", "1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    assert require_openroad() == tmp_path / "bin" / "openroad"


def test_the_probe_starts_openroad_the_way_the_flows_do(monkeypatch, tmp_path):
    """The probe runs a script with `-no_init -no_splash -exit`, as `run_openroad` does."""
    log = tmp_path / "calls"
    fake_openroad(tmp_path / "bin", f'echo "$@" >> {log}')
    monkeypatch.setenv("XEDA_TESTS_OPENROAD", "1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    require_openroad()
    calls = log.read_text().splitlines()
    assert calls[0] == "-version"
    assert calls[1].startswith("-no_init -no_splash -exit ")
    assert calls[1].endswith(".tcl")


# ---------------------------------------------------------------------------- the reader


def test_the_reader_finds_the_flags_of_a_command():
    script = 'repair_timing -setup -setup_margin 0.0 -sequence "a b" -verbose\n'
    assert pairs_of_script(script) == {
        ("repair_timing", "-setup"),
        ("repair_timing", "-setup_margin"),
        ("repair_timing", "-sequence"),
        ("repair_timing", "-verbose"),
    }


def test_the_reader_joins_the_lines_of_a_command():
    script = "global_placement -density 0.5 \\\n    -pad_left 2 \\\n    -skip_io\n"
    assert pairs_of_script(script) == {
        ("global_placement", "-density"),
        ("global_placement", "-pad_left"),
        ("global_placement", "-skip_io"),
    }


def test_the_reader_leaves_out_comments_numbers_and_quoted_text():
    script = (
        "# repair_timing -not_a_flag\n"
        'set_wire_rc -signal -layer "-looks_like_a_flag"\n'
        "set_layer_rc -layer m1 -resistance -0.5\n"
        "set_cts {-also_not_a_flag}\n"
    )
    assert pairs_of_script(script) == {
        ("set_wire_rc", "-signal"),
        ("set_wire_rc", "-layer"),
        ("set_layer_rc", "-layer"),
        ("set_layer_rc", "-resistance"),
    }


def test_the_reader_gives_a_flag_inside_brackets_to_the_command_that_holds_it():
    script = 'if {[repair_antennas -iterations 3 "DIODE"]} {\n    detailed_route -verbose\n}\n'
    assert pairs_of_script(script) == {
        ("repair_antennas", "-iterations"),
        ("detailed_route", "-verbose"),
    }
    nested = "set x [get_cells -hier *] ; report_checks -path_delay max\n"
    assert pairs_of_script(nested) == {
        ("get_cells", "-hier"),
        ("report_checks", "-path_delay"),
    }


def test_the_reader_knows_the_arguments_that_a_script_keeps_in_a_list():
    script = (
        'set route_args [list -output_drc "a.rpt" -verbose 1]\n'
        "detailed_route {*}$route_args\n"
        "detailed_route -droute_end_iter 3 {*}$route_args\n"
    )
    assert pairs_of_script(script) == {
        ("detailed_route", "-output_drc"),
        ("detailed_route", "-verbose"),
        ("detailed_route", "-droute_end_iter"),
    }


def test_the_reader_ignores_the_commands_it_does_not_know():
    assert pairs_of_script("puts -nonewline x\nstring match -nocase a b\n") == set()
    assert flags_of(" -a 1 -b") == ["-a", "-b"]


def test_the_reader_reads_the_text_around_the_statements_of_a_template():
    template = (
        "{# a comment with repair_design -no #}\n"
        "{% if x %}\n"
        "    repair_design {{ render_flags(group) }} -verbose\n"
        "{% endif %}\n"
        '    global_route -guide_file {{ guide|tcl_word }} {{ ["-a", "-b"]|join(" ") }}\n'
    )
    assert pairs_of_template(template) == {
        ("repair_design", "-verbose"),
        ("global_route", "-guide_file"),
    }


def test_a_flip_flop_is_found_in_a_liberty_file(tmp_path):
    liberty = tmp_path / "cells.lib"
    liberty.write_text(
        'cell (BUF_X1) {\n  pin (Z) { direction : output; function : "A"; }\n}\n'
        "cell (DFF_X1) {\n"
        '  ff ("IQ" , "IQN") { next_state : "D"; clocked_on : "CK"; }\n'
        "  pin (D) { direction : input; }\n"
        "  pin (CK) { direction : input; clock : true; }\n"
        '  pin (QN) { direction : output; function : "IQN"; }\n'
        '  pin (Q) { direction : output; function : "IQ"; }\n'
        "}\n"
    )
    assert liberty_flop([liberty], []) == ("DFF_X1", "D", "CK", "Q")
    assert liberty_flop([liberty], ["DFF_X1"]) is None


def test_the_help_of_a_command_lists_its_flags():
    text = (
        "repair_timing [-setup] [-hold] [-recover_power percent]\n"
        "   [-sequence move_list] [-skip_gate_cloning]\n"
        "  -setup  Apply to setup checks.\n"
        "\n"
        "repair_timing_other [-other]\n"
    )
    flags = help_flags(text)
    assert flags["repair_timing"] == {
        "-setup",
        "-hold",
        "-recover_power",
        "-sequence",
        "-skip_gate_cloning",
    }
    assert flags["repair_timing_other"] == {"-other"}


def test_a_command_without_help_has_no_entry():
    assert help_flags("[WARNING STA-0160] no commands match 'gui::fit'.\n") == {}


# ---------------------------------------------------------------------------- the flags

#: Flags that `help` lists for a command and that the tool rejects when they are given. No
#: template prints them.
LISTED_BUT_REJECTED = {
    ("detailed_route", "-bottom_routing_layer"),
    ("detailed_route", "-top_routing_layer"),
    ("pin_access", "-bottom_routing_layer"),
    ("pin_access", "-top_routing_layer"),
}


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> Installed:
    """What the installed OpenROAD says about the commands, from one process."""
    openroad = require_openroad()
    return read_installed(openroad, tmp_path_factory.mktemp("help"))


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Rendered:
    """The drivers of the four flows for the defaults and the variants of every setting."""
    require_openroad()
    return render_variants(tmp_path_factory.mktemp("variants"))


def test_every_command_of_the_list_exists_in_the_tool(installed):
    missing = sorted(set(OPENROAD_COMMANDS) - installed.exists)
    assert not missing, f"the installed OpenROAD has no command {missing}"


def test_every_emitted_flag_is_in_help(installed, rendered):
    """The flags of the rendered drivers and of the text of the templates are all in the `help`
    of their command. A command that is a Tcl procedure without a usage in `help` is checked
    against the flags its body reads. A command with neither takes no flag in any template."""
    pairs = rendered.pairs | template_pairs()
    wrong, unchecked = [], []
    for command, flag in sorted(pairs):
        known = installed.flags.get(command, installed.body_flags.get(command))
        if known is None:
            unchecked.append((command, flag))
        elif flag not in known:
            where = rendered.where.get((command, flag), "a template")
            wrong.append(f"{command} {flag} (in {where})")
    assert not wrong, "flags that the installed OpenROAD does not list:\n  " + "\n  ".join(wrong)
    assert not unchecked, f"flags of commands that the tool does not describe: {unchecked}"


def test_no_template_prints_a_flag_that_the_tool_lists_and_rejects(rendered):
    printed = rendered.pairs | template_pairs()
    assert not printed & LISTED_BUT_REJECTED


def test_the_variants_reach_the_flag_of_every_typed_setting(rendered):
    """Every flag of a `FLAGS` table and every flag of an `EXTRA_FLAGS` list is in a rendered
    driver, so the check above saw it."""
    assert {
        ("detailed_route", "-droute_end_iter"),
        ("repair_timing", "-sequence"),
    } <= rendered.pairs
    seen = {flag for _, flag in rendered.pairs}
    for name, flags in group_flags().items():
        assert flags <= seen, f"{name}: {sorted(flags - seen)}"
    for name, flow in FLOWS.items():
        for path, group in groups_of(flow.Settings):
            assert group.EXTRA_FLAGS <= seen, f"{name}.{'.'.join(path)}"


def test_the_help_of_the_tool_is_read(installed):
    assert {"-setup", "-hold", "-sequence"} <= installed.flags["repair_timing"]
    assert "-no_such_flag" not in installed.flags["repair_timing"]
    assert {"-pad_left", "-pad_right"} <= installed.body_flags[
        "gpl::get_global_placement_uniform_density"
    ]
    assert "utl::set_metrics_stage" not in installed.flags


# ---------------------------------------------------------------------------- the platforms

#: A die that holds the default power grid of any platform. The cells of the netlist sit in the
#: middle of it.
DIE = ["0 um", "0 um", "250 um", "250 um"]
CORE = ["10 um", "10 um", "240 um", "240 um"]


def liberty_flop(files: list[Path], dont_use: list[str]) -> tuple[str, str, str, str] | None:
    """Return the cell, data pin, clock pin and output pin of a flip-flop in Liberty `files`, or
    `None`. The flip-flop is a plain one that is not in `dont_use`: its next state and its clock
    are pins. Of those, it is the one with the fewest pins."""
    found = []
    for liberty in files:
        text = liberty.read_text()
        parts = re.split(r"^\s*cell\s*\(\s*\"?([\w$.]+)\"?\s*\)", text, flags=re.M)
        for name, body in zip(parts[1::2], parts[2::2]):
            group = re.search(
                r'ff\s*\(\s*"?(\w+)"?\s*,\s*"?\w+"?\s*\)\s*\{[^}]*?'
                r'next_state\s*:\s*"(\w+)"\s*;\s*clocked_on\s*:\s*"(\w+)"',
                body,
            )
            if group is None or name in dont_use:
                continue
            state, data, clock = group.groups()
            pins = re.split(r"\bpin\s*\(", body)[1:]
            for pin in pins:
                named = re.match(r'\s*"?(\w+)"?\s*\)', pin)
                if named and re.search(rf'function\s*:\s*"{state}"', pin) and "output" in pin:
                    found.append((len(pins), name, data, clock, named.group(1)))
                    break
    if not found:
        return None
    _, name, data, clock, out = min(found)
    return name, data, clock, out


def four_cell_netlist(model, flop: tuple[str, str, str, str]) -> str:
    """Return a netlist of a high tie cell, a low tie cell, a buffer and a flip-flop."""
    high, low, buffer = model.tie_high, model.tie_low, model.min_buffer
    cell, data, clock, out = flop
    return (
        "module top(clk, d, q, hi, lo);\n"
        "  input clk;\n  input d;\n  output q;\n  output hi;\n  output lo;\n  wire b;\n"
        f"  {high.cell} tie_high (.{high.port}(hi));\n"
        f"  {low.cell} tie_low (.{low.port}(lo));\n"
        f"  {buffer.cell} buf0 (.{buffer.input}(d), .{buffer.output}(b));\n"
        f"  {cell} ff0 (.{data}(b), .{clock}(clk), .{out}(q));\n"
        "endmodule\n"
    )


def run_openroad(openroad: Path, work: Path, script: str, name: str) -> subprocess.CompletedProcess:
    """Run `script` in `work` as the flows start OpenROAD, and return the finished process."""
    (work / f"{name}.tcl").write_text(script)
    return subprocess.run(
        [
            str(openroad),
            "-no_init",
            "-no_splash",
            "-exit",
            "-metrics",
            f"{name}.json",
            f"{name}.tcl",
        ],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("name", pdk_test_platforms())
def test_each_platform_floorplans_a_four_cell_netlist(name, tmp_path):
    """The load, the floorplan, the tap cells and the power grid of `openroad_place` work on the
    real files of the platform, with the cells that the platform names for ties and buffers."""
    openroad = require_openroad()
    store = require_pdk_store()
    root = tmp_path / "design"
    root.mkdir()
    netlist = root / "top_netlist.v"
    netlist.write_text("module top(); endmodule\n")
    design = Design(
        name="top",
        design_root=root,
        rtl={
            "sources": [{"file": "top_netlist.v", "type": "VerilogNetlist"}],
            "top": "top",
            "clock": {"port": "clk"},
        },
    )
    settings = OpenroadPlace.Settings.from_input(
        {
            "platform": name,
            "pdk_root": str(store),
            "clocks": {"clk": {"period": "10 ns"}},
            "floorplan": {"sizing": "area", "die_area": DIE, "core_area": CORE},
        },
        design_root=root,
        runner_cwd=tmp_path,
    )
    work = tmp_path / "work"
    work.mkdir()
    flow = OpenroadPlace(settings, design, work)
    flow.init()
    flow.inputs.netlist = netlist
    flow.outputs.odb = work / "place.odb"
    flow.outputs.sdc = work / "place.sdc"
    flow.prepare_inputs()
    model = flow.platform_model
    assert model is not None
    flop = liberty_flop(model.files["liberty"], model.dont_use_cells)
    assert flop is not None, f"no plain flip-flop in the Liberty files of {name}"
    netlist.write_text(four_cell_netlist(model, flop))
    script = render_driver(flow, "openroad_place")
    start, end = script.index("    xeda_global_place_skip_io\n"), script.index("} message options]")
    assert "    xeda_pdn\n" in script[:start], "the cut leaves the floorplan, the taps and the grid"
    flow.copy_from_template("clocks.sdc", trim_blocks=True, lstrip_blocks=True)
    (work / "reports").mkdir()
    done = run_openroad(openroad, work, script[:start] + script[end:], "openroad_place")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "xeda: stage pdn" in done.stdout
    assert (work / "place.odb").stat().st_size > 0
    assert (work / "place.sdc").is_file()
    count = run_openroad(
        openroad,
        work,
        'read_db place.odb\nputs "INSTANCES [llength [[ord::get_db_block] getInsts]]"\n',
        "count",
    )
    assert count.returncode == 0, count.stdout[-2000:] + count.stderr[-2000:]
    found = re.search(r"INSTANCES (\d+)", count.stdout)
    assert found and int(found.group(1)) >= 4, count.stdout


# ---------------------------------------------------------------------------- gcd through the chain

#: The design `gcd` of OpenROAD-flow-scripts on nangate45, with the settings of its configuration.
GCD_DESIGN = Path(__file__).parent.parent / "examples" / "asic" / "gcd" / "gcd.yaml"
#: The clock period of the ORFS configuration, which these flows do not meet on gcd.
ORFS_PERIOD = "0.25ns"


def require_gcd_tools() -> Path:
    """Insist on what a gcd run needs: OpenROAD, yosys, and a PDK store that holds nangate45.
    Return the store."""
    require_openroad()
    require_yosys()
    store = require_pdk_store()
    if "nangate45" not in pdk_test_platforms():
        pytest.skip("the PDK store of XEDA_TESTS_PDK_ROOT does not hold nangate45")
    return store


def gcd_settings(store: Path, period: str | None = None) -> list[str]:
    """The settings of the command line for a gcd run: the PDK store, and the clock period of
    the two flows that take it, if `period` is given."""
    settings = [f"pdk_root={store}"]
    if period is not None:
        settings += [f"flows.{flow}.clock.period={period}" for flow in ("yosys", "openroad_place")]
    return settings


def run_gcd(
    runner: DefaultRunner, store: Path, *extra: str, period: str | None = None, flow: str = ""
) -> Flow:
    """Launch `yosys+openroad` (or `flow`) on gcd with `runner` and return the requested flow."""
    launched = runner.run(
        flow or "yosys+openroad",
        str(GCD_DESIGN),
        flow_settings=[*gcd_settings(store, period), *extra],
    )
    assert launched is not None
    return launched


def started(runner: DefaultRunner, first: int) -> list[str]:
    """The names of the flows that the launches of `runner` ran, not reused, since `first`."""
    return [flow.name for flow in runner.launched[first:] if not flow.reused]


CHAIN = ["yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad"]


def test_gcd_runs_through_the_chain(tmp_path):
    """The whole chain closes gcd at 0.65 ns on nangate45: timing met, no DRC error, no antenna
    violation, and a trace in each of the five run directories."""
    store = require_gcd_tools()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    start = time.monotonic()
    flow = run_gcd(runner, store)
    elapsed = time.monotonic() - start
    assert flow.succeeded, flow.results.get("error")
    assert started(runner, 0) == CHAIN
    results = flow.results
    assert results["wns"] == 0 and results["worst_slack"] > 0, results["worst_slack"]
    assert results["drc_errors"] == 0 and results["antenna_violating_nets"] == 0
    for name in CHAIN:
        assert (tmp_path / "run" / "gcd" / name / "trace.json").is_file(), name
    print(f"the gcd chain took {elapsed:.0f} s")


def test_gcd_at_its_orfs_clock_is_rejected_and_reported_again(tmp_path):
    """At the 0.25 ns of its ORFS configuration, `openroad` rejects gcd for its timing and
    keeps the trace. A second launch reports the rejection again and starts no tool."""
    store = require_gcd_tools()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = run_gcd(runner, store, period=ORFS_PERIOD)
    assert flow.name == "openroad" and flow.outcome == "rejected", flow.results.get("error")
    assert "timing not met" in (flow.rejection or "")
    assert (flow.run_path / "trace.json").is_file()
    logs = [flow.run_path / f"openroad_{process}.log" for process in ("route", "final")]
    stamps = [log.stat().st_mtime_ns for log in logs]
    first = len(runner.launched)
    again = run_gcd(runner, store, period=ORFS_PERIOD)
    assert again.reused and again.rejected_again and not again.succeeded
    document = json.loads((flow.run_path / "results.json").read_text())
    assert again.rejection == document["error"]["message"]
    assert (flow.rejection or "") in again.rejection
    assert started(runner, first) == []
    assert [log.stat().st_mtime_ns for log in logs] == stamps


def test_a_late_setting_reruns_only_the_later_flows(tmp_path):
    """A setting of `openroad` reruns only `openroad`. A setting of `openroad_grt` reruns
    `openroad_grt` and `openroad`, and the earlier flows are reused."""
    store = require_gcd_tools()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    assert run_gcd(runner, store).succeeded
    first = len(runner.launched)
    route = "flows.openroad.detailed_route.end_iteration=32"
    assert run_gcd(runner, store, route).succeeded
    assert started(runner, first) == ["openroad"]
    first = len(runner.launched)
    congestion = "flows.openroad_grt.congestion.iterations=40"
    assert run_gcd(runner, store, route, congestion).succeeded
    assert started(runner, first) == ["openroad_grt", "openroad"]


def write_def(openroad: Path, odb: Path, work: Path) -> bytes:
    """The DEF file that OpenROAD writes from the database `odb`."""
    work.mkdir()
    done = run_openroad(openroad, work, f"read_db {odb}\nwrite_def design.def\n", "def")
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return (work / "design.def").read_bytes()


def test_two_runs_write_the_same_database(tmp_path):
    """`openroad_place` in two run roots writes the same SDC file and the same database, or at
    least the same design: the DEF files that OpenROAD writes from the two databases are equal.
    The database never holds the path of its netlist."""
    store = require_gcd_tools()
    openroad = require_openroad()
    flows = [
        run_gcd(DefaultRunner(tmp_path / root, display_results=False), store, flow="openroad_place")
        for root in ("one", "two")
    ]
    assert all(flow.succeeded for flow in flows)
    odbs = [flow.outputs.odb.read_bytes() for flow in flows]
    sdcs = [flow.outputs.sdc.read_bytes() for flow in flows]
    assert sdcs[0] == sdcs[1]
    for flow, odb in zip(flows, odbs):
        assert flow.inputs.netlist is not None
        assert os.fsencode(flow.inputs.netlist) not in odb
        assert os.fsencode(flow.inputs.netlist.parent) not in odb
    if odbs[0] == odbs[1]:
        print("the two databases are equal")
        return
    defs = [
        write_def(openroad, flow.outputs.odb, tmp_path / f"def-{index}")
        for index, flow in enumerate(flows)
    ]
    assert defs[0] == defs[1], "the two databases hold different designs"
    print("the two databases differ, and their DEF files are equal")


def test_the_chain_writes_nothing_outside_its_run_root(tmp_path, monkeypatch):
    """The chain, an unchanged second launch and a rejected launch write nothing outside the run
    root. The audit hook sees the writes of xeda, and the listing of the tree those of the tools.
    The design, a copy of the PDK store and the home directory are read-only, but for root, who
    writes through the permissions: the CI job runs in a container as root."""
    from .test_isolation import World, _freeze, _state, watching

    store = require_gcd_tools()
    parent = tmp_path / "world"
    work = parent / "work"
    shutil.copytree(GCD_DESIGN.parent, work)
    pdks = parent / "pdks"
    shutil.copytree(store, pdks, symlinks=True)
    home = parent / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    world = World(parent, work, parent / "outside", work / "xeda_run", work / "delivered", pdks)
    ensure_run_root(world.root)
    before = _state(parent, [world.root])
    modes = {
        Path(directory, name): os.lstat(Path(directory, name)).st_mode
        for directory, directories, files in os.walk(parent)
        for name in (*directories, *files)
    }
    modes[parent] = parent.stat().st_mode
    frozen = os.geteuid() != 0
    if frozen:
        _freeze(parent, keep=world.root)
    try:
        with watching(world) as violations:
            runner = DefaultRunner(world.root, display_results=False)
            design = str(work / GCD_DESIGN.name)

            def launch(period: str | None = None) -> Flow:
                settings = gcd_settings(pdks, period)
                flow = runner.run("yosys+openroad", design, flow_settings=settings)
                assert flow is not None
                return flow

            assert launch().succeeded
            first = len(runner.launched)
            assert launch().reused and started(runner, first) == []
            assert launch(ORFS_PERIOD).outcome == "rejected"
    finally:  # the modes as they were: the store holds executable files
        for path, mode in sorted(modes.items(), key=lambda item: len(item[0].parts)):
            if frozen and not stat.S_ISLNK(mode):
                path.chmod(stat.S_IMODE(mode))
    after = _state(parent, [world.root])
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, changed
    assert not violations, violations
