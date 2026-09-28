"""The Bluespec flows: `bsc` generates Verilog, `bsc_sim` simulates a Bluespec testbench.

Three layers. The first needs no tool: how settings become bsc flags, the checks that reject
what bsc would refuse, and how the Verilog files a top module needs are collected. The second
hands every setting's flags to the installed bsc and reads back the flag record bsc builds from
them (`-print-flags-raw`), so a setting that maps to the wrong flag, a flag bsc no longer knows
(`-cross-info`, removed in 2026.07), or a description claiming a bsc default that is not bsc's
default fails here. The third runs the flows end to end on small designs written for the case
at hand; `test_bsc_examples.py` runs the example designs.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim
from xeda.flows.bsc import MIN_BSC_VERSION, BscFlow, BscTool, _macro_flags, _parse_haskell_strings
from xeda.utils import WorkingDirectory

from .tool_utils import require_bluesim, require_bsc, require_iverilog, require_verilator

# ---------------------------------------------------------------------------------------------
# small designs
# ---------------------------------------------------------------------------------------------

#: A synthesized submodule in a package of its own: its Verilog is generated while compiling the
#: package that imports it, which is what the old flow missed.
ADDER_PKG = """package Accum;
interface Accum_IFC;
  method Action add(Bit#(8) d);
  method Bit#(8) value;
endinterface
(* synthesize *)
module mkAccum(Accum_IFC);
  Reg#(Bit#(8)) acc <- mkReg(0);
  method Action add(Bit#(8) d); acc <= acc + d; endmethod
  method value = acc;
endmodule
endpackage
"""

TOP_PKG = """package Top;
import Accum::*;
import FIFO::*;
interface Top_IFC;
  method Bit#(8) out;
endinterface
(* synthesize *)
module mkTop(Top_IFC);
`ifdef STEP
  Bit#(8) step = `STEP;
`else
  Bit#(8) step = 1;
`endif
  Accum_IFC acc <- mkAccum;
  FIFO#(Bit#(8)) fifo <- mkFIFO;
  rule feed; acc.add(step); fifo.enq(acc.value); endrule
  rule drain; fifo.deq; endrule
  method out = fifo.first;
endmodule
endpackage
"""

#: Checks the top after 10 cycles: `out` is 9 * STEP then, on every simulator.
TB_PKG = """package Tb;
import Top::*;
(* synthesize *)
module mkTb(Empty);
`ifdef STEP
  Bit#(8) step = `STEP;
`else
  Bit#(8) step = 1;
`endif
  Top_IFC dut <- mkTop;
  Reg#(UInt#(8)) cycle <- mkReg(0);
  rule tick; cycle <= cycle + 1; endrule
  Bit#(8) expected = 9 * step;
`ifdef XEDA_INJECT_BUG
  expected = expected + 1;
`endif
  rule check (cycle == 10);
    $display("out=%0d", dut.out);
    if (dut.out != expected) $fatal(1, "FAIL: expected %0d", expected);
    $display("PASS");
    $finish(0);
  endrule
endmodule
endpackage
"""

#: Never finishes by itself: only `max_cycles` ends it.
ENDLESS_TB = """package Endless;
(* synthesize *)
module mkEndless(Empty);
  Reg#(UInt#(32)) n <- mkReg(0);
  rule tick; n <= n + 1; endrule
endmodule
endpackage
"""

#: A reset that is still asserted after reset would keep `seen` at 0.
RESET_TB = """package ResetTb;
(* synthesize *)
module mkResetTb(Empty);
  Reg#(UInt#(8)) cycle <- mkReg(0);
  rule tick; cycle <= cycle + 1; endrule
  rule check (cycle == 5);
    $display("PASS");
    $finish(0);
  endrule
  rule watchdog (cycle == 0);
    $display("out of reset");
  endrule
endmodule
endpackage
"""

BH_PKG = """package Blink where

interface Blink_IFC =
    led :: Bit 1

{-# verilog mkBlink #-}
mkBlink :: Module Blink_IFC
mkBlink = module
    r :: Reg (Bit 1) <- mkReg 0
    rules
      "toggle": when True ==> r := invert r
    interface
      led = r
"""


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _accum_design(root: Path, **extra: Any) -> Design:
    """`mkTop` with its submodule in another directory, and the `mkTb` testbench."""
    _write(root, {"lib/Accum.bsv": ADDER_PKG, "rtl/Top.bsv": TOP_PKG, "tb/Tb.bsv": TB_PKG})
    fields: dict[str, Any] = {
        "rtl": {"sources": ["lib/Accum.bsv", "rtl/Top.bsv"], "top": "mkTop"},
        "tb": {"sources": ["tb/Tb.bsv"], "top": "mkTb"},
    }
    fields.update(extra)
    return Design(name="accum", design_root=root, **fields)


def _flow(flow_class: type[Flow], design: Design, run_path: Path, **settings: Any) -> Any:
    run_path.mkdir(parents=True, exist_ok=True)
    return flow_class(settings, design, run_path)


def _run(flow_class: type[Flow], design: Design, run_dir: Path, **settings: Any) -> Any:
    return DefaultRunner(run_dir, display_results=False).run_flow(flow_class, design, settings)


# ---------------------------------------------------------------------------------------------
# settings to flags, without bsc
# ---------------------------------------------------------------------------------------------


def _pairs(flags: list[str], flag: str) -> list[str]:
    """The arguments `flag` is given with in `flags`."""
    return [flags[i + 1] for i, f in enumerate(flags) if f == flag and i + 1 < len(flags)]


def test_split_if_comes_before_aggressive_conditions(tmp_path):
    """In bsc, `-split-if` / `-no-split-if` also sets aggressive conditions, so it comes before
    `-aggressive-conditions`, which then holds whatever `split_if` is."""
    flow = _flow(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run")
    flags = flow._compile_flags("verilog")
    assert flags.index("-no-split-if") < flags.index("-aggressive-conditions")
    assert "-sched-conditions" in flags


def test_default_bsc_flags(tmp_path):
    """What the `bsc` flow asks for by default: synthesis-oriented don't-cares, bsc's own
    warnings and optimizations (`-O` can take minutes on large designs), and no flag bsc 2026.07
    removed."""
    flow = _flow(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run")
    flags = flow._compile_flags("verilog")
    assert _pairs(flags, "-unspecified-to") == ["X"]
    assert "-opt-undetermined-vals" in flags and "-O" not in flags
    assert "-remove-dollar" in flags and "-remove-unused-modules" in flags
    assert "-show-module-use" in flags and "-check-assert" in flags
    assert "-promote-warnings" not in flags
    assert "-cross-info" not in flags and "-no-show-timestamps" in flags


def test_debug_does_not_change_the_generated_hardware(tmp_path):
    """`debug` used to switch on `-keep-fires` and off `-remove-unused-modules` (and pass the
    removed `-cross-info`): the Verilog a debug run generated was another design."""
    design = _accum_design(tmp_path / "d")
    plain = _flow(Bsc, design, tmp_path / "a")._compile_flags("verilog")
    debug = _flow(Bsc, design, tmp_path / "b", debug=True)._compile_flags("verilog")
    assert plain == debug


def test_bluesim_compiles_without_remove_dollar(tmp_path):
    """bsc refuses `-remove-dollar` for Bluesim and for every link step."""
    design = _accum_design(tmp_path / "d")
    flow = _flow(BscSim, design, tmp_path / "run")
    assert not any("remove-dollar" in f for f in flow._compile_flags("sim"))
    assert "-remove-dollar" in flow._compile_flags("verilog")
    for bluesim in (True, False):
        assert not any("remove-dollar" in f for f in flow._link_flags(bluesim))


def test_bsc_sim_defaults_suit_simulation(tmp_path):
    """Two-valued don't-cares Bluesim accepts, and `$fatal` kept for Verilog simulators."""
    flow = _flow(BscSim, _accum_design(tmp_path / "d"), tmp_path / "run")
    flags = flow._compile_flags("verilog")
    assert _pairs(flags, "-unspecified-to") == ["A"]
    assert "-system-verilog-tasks" in flags
    assert "-no-remove-unused-modules" in flags and "-O" not in flags


def test_link_flags(tmp_path):
    """The simulator, reset name and C/C++ options reach the link step."""
    design = _accum_design(tmp_path / "d")
    flow = _flow(
        BscSim,
        design,
        tmp_path / "run",
        reset_prefix="RST",
        cxx_flags=["-O1"],
        libraries=["m"],
        include_dirs=["inc"],
        optimization_flags=["-O3"],
        ncpus=3,
    )
    bluesim = flow._link_flags(True)
    assert _pairs(bluesim, "-reset-prefix") == ["RST"]
    assert _pairs(bluesim, "-parallel-sim-link") == ["3"]
    assert _pairs(bluesim, "-Xc++") == ["-O3", "-O1"]
    assert _pairs(bluesim, "-l") == ["m"]
    assert _pairs(bluesim, "-I") == [str(design.root_path / "inc")]  # an input: the design's
    flow.settings.simulator = "verilator"
    verilog = flow._link_flags(False)
    assert _pairs(verilog, "-vsim") == ["verilator"] and _pairs(verilog, "-Xv") == ["-O3"]
    assert "-parallel-sim-link" not in verilog


def test_macros(tmp_path):
    """`rtl.defines` and `rtl.parameters` are macros; `True` defines one bare, `False` none."""
    assert _macro_flags({"A": None, "B": True, "C": False, "D": 8, "E": "x"}, "m") == [
        "-D",
        "A",
        "-D",
        "B",
        "-D",
        "D=8",
        "-D",
        "E=x",
    ]
    with pytest.raises(FlowSettingsException, match="whitespace"):
        _macro_flags({"A": "two words"}, "m")
    with pytest.raises(FlowSettingsException, match="macro name"):
        _macro_flags({"1A": 1}, "m")


def test_the_testbench_macros_override_the_rtl_ones(tmp_path):
    design = _accum_design(
        tmp_path / "d",
        rtl={
            "sources": ["lib/Accum.bsv", "rtl/Top.bsv"],
            "top": "mkTop",
            "defines": {"STEP": 1, "X": 1},
            "parameters": {"STEP": 2},
        },
        tb={"sources": ["tb/Tb.bsv"], "top": "mkTb", "defines": {"STEP": 3}},
    )
    bsc = _flow(Bsc, design, tmp_path / "a")
    assert bsc._macros(tb=False) == {"STEP": 2, "X": 1, "BSV_POSITIVE_RESET": None}
    sim = _flow(BscSim, design, tmp_path / "b")
    assert sim._macros(tb=True) == {"STEP": 3, "X": 1}
    assert design.rtl.parameters == {"STEP": 2}  # the design is not ours to change


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"reset_prefix": "RST N"}, "Verilog port name"),
        ({"promote_warnings": ["G10"]}, "message tags"),
        ({"suppress_warnings": "G0010,oops"}, "message tags"),
        ({"haskell_runtime_flags": ["H1G"]}, "not flags"),
        ({"steps_warn_interval": 0}, "greater than 0"),
        ({"unspecified_to": "x"}, "unspecified_to"),
        ({"resource_scheduling": "on"}, "resource_scheduling"),
    ],
)
def test_invalid_settings_are_settings_errors(settings, message):
    with pytest.raises(FlowSettingsError, match=message):
        Bsc.Settings.from_input(settings)


def test_a_comma_separated_message_list_is_a_list():
    assert Bsc.Settings.from_input({"suppress_warnings": "G0010,G0021"}).suppress_warnings == [
        "G0010",
        "G0021",
    ]


def test_bsc_sim_rejects_an_unknown_simulator():
    with pytest.raises(FlowSettingsError, match="simulator"):
        BscSim.Settings.from_input({"simulator": "ghdl"})


@pytest.mark.parametrize(
    "flow_class, settings, message",
    [
        (Bsc, {"opt_undetermined_vals": False}, "requires opt_undetermined_vals"),
        (BscSim, {"unspecified_to": "X", "opt_undetermined_vals": True}, "two-valued"),
        (BscSim, {"stop_time": 100}, "max_cycles"),
        (BscSim, {"simulator": "verilator", "max_cycles": 10}, "Bluesim simulations only"),
    ],
)
def test_combinations_bsc_refuses_are_rejected_before_running(
    flow_class, settings, message, tmp_path, monkeypatch
):
    """Checked by `init()`, naming the settings, rather than by bsc after other work."""
    monkeypatch.setenv("PATH", "")  # nothing may run
    design = _accum_design(tmp_path / "d")
    with pytest.raises(FlowSettingsException, match=message):
        _run(flow_class, design, tmp_path / "run", **settings)


def test_bsc_needs_a_top(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    _write(tmp_path, {"Top.bsv": TOP_PKG})
    design = Design(name="t", design_root=tmp_path, rtl={"sources": ["Top.bsv"]})
    with pytest.raises(FlowSettingsException, match=r"rtl\.top"):
        _run(Bsc, design, tmp_path / "run")


def test_bsc_sim_needs_a_testbench_top(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    design = _accum_design(tmp_path / "d", tb={"sources": ["tb/Tb.bsv"]})
    with pytest.raises(FlowSettingsException, match=r"tb\.top"):
        _run(BscSim, design, tmp_path / "run")


def test_bsc_sim_rejects_a_cocotb_testbench(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    _write(tmp_path, {"Top.bsv": TOP_PKG, "test_top.py": "import cocotb\n"})
    design = Design(
        name="t",
        design_root=tmp_path,
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
        tb={"sources": ["test_top.py"], "cocotb": True},
    )
    with pytest.raises(FlowSettingsException, match="cocotb"):
        _run(BscSim, design, tmp_path / "run")


def test_a_bh_suffix_is_rejected_by_name(tmp_path):
    """bsc compiles BH only from `.bs`; xeda also calls `.bh` Bluespec, which bsc refuses."""
    _write(tmp_path, {"Blink.bh": BH_PKG})
    design = Design(name="t", design_root=tmp_path, rtl={"sources": ["Blink.bh"], "top": "mkBlink"})
    flow = _flow(Bsc, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException, match=r"rename: .*Blink\.bh"):
        flow._bsc_sources(tb=False)


def test_a_search_path_with_a_colon_is_rejected(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run", search_paths=["/a:b"])
    with pytest.raises(FlowSettingsException, match="':'"):
        flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)


def test_haskell_string_lists():
    """`-print-flags-raw` shows paths as Haskell strings."""
    assert _parse_haskell_strings(r'["/a b","c\"d","e\\f","\233t\233","x\1234\&5"]') == [
        "/a b",
        'c"d',
        "e\\f",
        "été",
        "xӒ5",
    ]


@pytest.mark.parametrize(
    "banner, supported",
    [
        ("Bluespec Compiler, version 2026.07.1 (build 63665af1)", True),
        ("Bluespec Compiler, version 2027.01 (build 0123abcd)", True),
        ("Bluespec Compiler, version 2026.07 (build 083dcb67)", False),
        ("Bluespec Compiler, version 2025.07 (build 282e82e9)", False),
    ],
)
def test_bsc_version(banner, supported):
    """2026.07 has no patch number and must compare below 2026.07.1, not equal to it."""
    tool = BscTool.model_construct(executable="bsc")
    version = tool.process_version_output(banner + "\nThis is free software\n")
    assert BscTool._version_is_gte(version, MIN_BSC_VERSION) is supported


# ---------------------------------------------------------------------------------------------
# collecting the Verilog files of the top, without bsc
# ---------------------------------------------------------------------------------------------


def test_collect_verilog(tmp_path, caplog):
    """Generated modules come with a `.use` file; library modules are copied from the search
    path (never taken from the output directory, which may hold an earlier run's copies);
    modules the design's Verilog defines stay where they are; a module found nowhere is left to
    the downstream tool with a warning."""
    root = tmp_path / "d"
    _write(
        root,
        {
            "rtl/Top.bsv": TOP_PKG,
            "rtl/ext.v": "module ExtA(); endmodule\nmodule ExtB();\nendmodule\n",
        },
    )
    design = Design(
        name="t",
        design_root=root,
        rtl={"sources": ["rtl/ext.v", "rtl/Top.bsv"], "top": "mkTop"},
    )
    out = tmp_path / "out"
    lib = tmp_path / "lib"
    _write(
        out,
        {
            "mkTop.v": "module mkTop(); endmodule\n",
            "mkTop.use": "mkSub\nFIFO2\nExtB\n",
            "mkSub.v": "module mkSub(); endmodule\n",
            "mkSub.use": "FIFO2\nVendorPrim\n\n",
            "FIFO2.v": "stale copy from an earlier run\n",
        },
    )
    _write(lib, {"FIFO2.v": "module FIFO2(); endmodule\n"})
    flow = _flow(Bsc, design, tmp_path / "run", positive_reset=True)
    with caplog.at_level(logging.WARNING):
        modules, files = flow._collect_verilog("mkTop", root / "rtl/Top.bsv", out, [out, lib])
    assert modules == ["mkTop", "mkSub", "FIFO2", "ExtB"]
    defines = out / "bsv_defines.v"
    assert files == [defines, out / "FIFO2.v", root / "rtl/ext.v", out / "mkSub.v", out / "mkTop.v"]
    assert defines.read_text().startswith("`define BSV_POSITIVE_RESET\n")
    assert (
        out / "FIFO2.v"
    ).read_text() == "`define BSV_POSITIVE_RESET\nmodule FIFO2(); endmodule\n"
    assert (out / "mkTop.v").read_text().startswith("`define BSV_POSITIVE_RESET\n")
    assert "`define" not in (root / "rtl/ext.v").read_text()  # the design's own is passed on
    assert "VendorPrim" in caplog.text


def test_collect_verilog_needs_the_top(tmp_path):
    root = tmp_path / "d"
    _write(root, {"rtl/Top.bsv": TOP_PKG})
    design = Design(name="t", design_root=root, rtl={"sources": ["rtl/Top.bsv"], "top": "mkTop"})
    flow = _flow(Bsc, design, tmp_path / "run")
    (tmp_path / "out").mkdir()
    with pytest.raises(FlowSettingsException, match=r"last Bluespec source, Top\.bsv"):
        flow._collect_verilog("mkNope", root / "rtl/Top.bsv", tmp_path / "out", [])


@pytest.mark.parametrize("cleanup", [True, False])
def test_an_earlier_runs_output_is_removed_before_compiling(cleanup, tmp_path):
    """Runs of a design share their directory. A module an earlier run generated -- which the
    design may since have replaced by Verilog of its own -- must not be found again, nor a
    package compiled with other flags; library copies and anything else stay."""
    design = _accum_design(tmp_path / "d")
    run = tmp_path / "run"
    flow = _flow(Bsc, design, run, cleanup_bobjs=cleanup)
    out = run / "gen_rtl"
    _write(
        out,
        {"mkOld.v": "", "mkOld.use": "FIFO2\n", "FIFO2.v": "", "notes.txt": "the user's\n"},
    )
    _write(run / "bobjs", {"Old.bo": "", "Old.ba": ""})
    with WorkingDirectory(run):
        flow._prepare_dirs(out.absolute())
    left = {p.name for p in out.iterdir()} | {p.name for p in (run / "bobjs").iterdir()}
    expected = {"FIFO2.v", "notes.txt"}
    if not cleanup:
        expected |= {"mkOld.v", "mkOld.use", "Old.bo", "Old.ba"}
    assert left == expected


def test_a_module_both_generated_and_a_design_source_is_an_error(tmp_path):
    """The file set would define it twice, which no downstream tool accepts."""
    root = tmp_path / "d"
    _write(root, {"rtl/Top.bsv": TOP_PKG, "rtl/mkSub.v": "module mkSub(); endmodule\n"})
    design = Design(
        name="t", design_root=root, rtl={"sources": ["rtl/mkSub.v", "rtl/Top.bsv"], "top": "mkTop"}
    )
    out = tmp_path / "out"
    _write(out, {"mkTop.v": "", "mkTop.use": "mkSub\n", "mkSub.v": "", "mkSub.use": ""})
    flow = _flow(Bsc, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException, match=r"mkSub is both generated by bsc"):
        flow._collect_verilog("mkTop", root / "rtl/Top.bsv", out, [])


# ---------------------------------------------------------------------------------------------
# every setting, as the installed bsc reads it
# ---------------------------------------------------------------------------------------------


def _bsc_record(*flags: str, cwd: Path) -> dict[str, str]:
    """The flag record bsc builds from `flags`, as `name -> value` text."""
    proc = subprocess.run(
        ["bsc", *flags, "-print-flags-raw"], capture_output=True, text=True, cwd=cwd, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # an error line, not the record's `demoteErrors` field
    assert not re.search(r"^Error", proc.stdout + proc.stderr, re.M), proc.stdout + proc.stderr
    record = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"^\s+(\w+) = (.*?),?$", line)
        if match:
            record[match.group(1)] = match.group(2)
    assert record, proc.stdout
    return record


#: Each boolean setting and the field of bsc's flag record it sets.
BOOLEAN_SETTINGS = {
    "aggressive_conditions": "aggImpConds",
    "sched_conditions": "schedConds",
    "split_if": "expandIf",
    "lift": "ifLift",
    "let_gen": "letGen",
    "check_assert": "testAssert",
    "opt_undetermined_vals": "optUndet",
    "optimize": "optBool",
    "synthesize_to_boolean": "synthesize",
    "remove_false_rules": "removeFalseRules",
    "remove_empty_rules": "removeEmptyRules",
    "remove_starved_rules": "removeStarvedRules",
    "remove_unused_modules": "removeUnusedMods",
    "keep_fires": "keepFires",
    "keep_inlined_boundaries": "keepInlined",
    "keep_method_conds": "methodConditions",
    "readable_mux": "readableMux",
    "remove_dollar": "removeVerilogDollar",
    "v95": "v95",
    "use_dpi": "useDPI",
    "system_verilog_tasks": "systemVerilogTasks",
    "elab": "genABin",
    "show_schedule": "showSchedule",
    "sched_dot": "schedDOT",
    "show_method_conf": "methodConf",
    "show_method_bvi": "methodBVI",
    "show_range_conflict": "showRangeConflict",
    "show_stats": "showStats",
    "show_elab_progress": "showElabProgress",
    "show_version": "showVersion",
    "show_timestamps": "timeStamps",
    "warn_method_urgency": "warnMethodUrgency",
    "warn_action_shadowing": "warnActionShadowing",
    "warn_undetermined_predicate": "warnUndetPred",
    "continue_after_errors": "enablePoisonPills",
    "cpp": "cpp",
}

#: Boolean settings that are not a bsc flag of their own.
XEDA_BOOLEANS = {"cleanup_bobjs", "positive_reset"}


def _own_boolean_settings(settings_class: type[Flow.Settings]) -> set[str]:
    return {
        name
        for name, field in settings_class.model_fields.items()
        if field.annotation is bool and name not in Flow.Settings.model_fields
    }


def test_every_boolean_setting_is_checked():
    """A new boolean setting needs a row in `BOOLEAN_SETTINGS`, or it is not tested."""
    for settings_class in (Bsc.Settings, BscSim.Settings):
        unchecked = _own_boolean_settings(settings_class) - set(BOOLEAN_SETTINGS) - XEDA_BOOLEANS
        assert not unchecked


@pytest.mark.parametrize("flow_class", [Bsc, BscSim], ids=lambda cls: cls.name)
@pytest.mark.parametrize("setting", sorted(BOOLEAN_SETTINGS))
def test_each_boolean_setting_sets_its_bsc_flag(flow_class, setting, tmp_path):
    """Both values of every boolean setting, as bsc reads the flags the flow passes."""
    require_bsc()
    design = _accum_design(tmp_path / "d")
    backend = "verilog"
    for value in (True, False):
        flow = _flow(flow_class, design, tmp_path / "run", **{setting: value})
        record = _bsc_record(*flow._compile_flags(backend), cwd=tmp_path)
        assert record[BOOLEAN_SETTINGS[setting]] == str(value), setting


@pytest.mark.parametrize(
    "settings, field, value",
    [
        ({"unspecified_to": "0"}, "unSpecTo", '"0"'),
        ({"unspecified_to": "Z"}, "unSpecTo", '"Z"'),
        ({"resource_scheduling": "simple"}, "resource", "RFsimple"),
        ({"sat_solver": "stp"}, "satBackend", "SAT_STP"),
        ({"steps_warn_interval": 7}, "redStepsWarnInterval", "7"),
        ({"steps_max_intervals": 9}, "redStepsMaxIntervals", "9"),
        ({"reset_prefix": "RST"}, "resetName", '"RST"'),
        ({"promote_warnings": ["G0010", "G0020"]}, "promoteWarnings", 'SomeMsgs ["G0010","G0020"]'),
        ({"promote_warnings": ["ALL"]}, "promoteWarnings", "AllMsgs"),
        ({"promote_warnings": []}, "promoteWarnings", "SomeMsgs []"),
        ({"suppress_warnings": ["G0021"]}, "suppressWarnings", 'SomeMsgs ["G0021"]'),
        ({"demote_errors": ["G0004"]}, "demoteErrors", 'SomeMsgs ["G0004"]'),
        ({"show_rule_rel": [("r1", "r2")]}, "schedQueries", '[("r1","r2")]'),
        ({"verilog_filters": ["true"]}, "verilogFilter", '["true"]'),
        ({"cpp_flags": ["-DX"]}, "cppFlags", '["-DX"]'),
        ({"extra_optimize_flags": ["-opt-if-mux"]}, "optIfMux", "True"),
        # `-no-synthesize` also clears it, so it must not come after the optimization flags
        ({"extra_optimize_flags": ["-opt-bit-const"]}, "optBitConst", "True"),
        ({"extra_flags": ["-show-stats"]}, "showStats", "True"),
    ],
)
def test_each_setting_sets_its_bsc_flag(settings, field, value, tmp_path):
    require_bsc()
    flow = _flow(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run", **settings)
    flags = flow._compile_flags("verilog") + flow.settings.extra_flags
    assert _bsc_record(*flags, cwd=tmp_path)[field] == value


def test_the_path_settings_reach_bsc(tmp_path, monkeypatch):
    require_bsc()
    design = _accum_design(tmp_path / "d")
    for d in ("pkgs", "vlib", "files", "info"):
        (tmp_path / d).mkdir()
    flow = _flow(
        Bsc,
        design,
        tmp_path / "run",
        search_paths=[str(tmp_path / "pkgs")],
        verilog_search_paths=[str(tmp_path / "vlib")],
        fdir=str(tmp_path / "files"),
        info_dir=str(tmp_path / "info"),
        verilog_primitives="vivado",
    )
    (tmp_path / "run" / "bobjs").mkdir()
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path / "run")  # where the flow runs bsc
    record = _bsc_record(
        *flow._path_flags("verilog", tmp_path / "out", design.rtl.sources), cwd=tmp_path / "run"
    )
    ifc_path = _parse_haskell_strings(record["ifcPath"])
    assert ifc_path[1:3] == [str(tmp_path / "d" / "lib"), str(tmp_path / "d" / "rtl")]
    assert ifc_path[3] == str(tmp_path / "pkgs") and ifc_path[-1].endswith("/Libraries")
    v_path = _parse_haskell_strings(record["vPath"])
    assert v_path[0] == str(tmp_path / "out")
    assert v_path[1].endswith("/Verilog.Vivado") and v_path[2] == str(tmp_path / "vlib")
    assert v_path[-1].endswith("/Verilog")
    assert record["fdir"] == f'Just "{tmp_path / "files"}"'
    assert record["infoDir"] == f'Just "{tmp_path / "info"}"'


def test_the_link_settings_reach_bsc(tmp_path):
    require_bsc()
    flow = _flow(
        BscSim,
        _accum_design(tmp_path / "d"),
        tmp_path / "run",
        parallel_sim_link=4,
        c_flags=["-O2"],
        cxx_flags=["-g"],
        link_flags=["-s"],
        verilog_link_flags=["-Wall"],
        library_dirs=[str(tmp_path)],
        libraries=["m"],
        use_dpi=True,
        keep_fires=True,
    )
    record = _bsc_record(*flow._link_flags(True), cwd=tmp_path)
    assert record["parallelSimLink"] == "4"
    assert record["cFlags"] == '["-O2"]' and record["cxxFlags"] == '["-g"]'
    assert record["linkFlags"] == '["-s"]' and record["vFlags"] == '["-Wall"]'
    assert record["cLibPath"] == f'["{tmp_path}"]' and record["cLibs"] == '["m"]'
    assert record["useDPI"] == "True" and record["keepFires"] == "True"
    flow.settings.simulator = "iverilog"
    assert _bsc_record(*flow._link_flags(False), cwd=tmp_path)["vsim"] == 'Just "iverilog"'


#: Settings whose default is documented as bsc's own, and the record field that holds bsc's.
BSC_DEFAULTS = {
    "aggressive_conditions": "aggImpConds",
    "sched_conditions": "schedConds",
    "split_if": "expandIf",
    "lift": "ifLift",
    "let_gen": "letGen",
    "remove_false_rules": "removeFalseRules",
    "remove_empty_rules": "removeEmptyRules",
    "keep_fires": "keepFires",
    "keep_inlined_boundaries": "keepInlined",
    "keep_method_conds": "methodConditions",
    "readable_mux": "readableMux",
    "v95": "v95",
    "use_dpi": "useDPI",
    "show_version": "showVersion",
    "warn_method_urgency": "warnMethodUrgency",
    "warn_action_shadowing": "warnActionShadowing",
    "continue_after_errors": "enablePoisonPills",
    "cpp": "cpp",
    "synthesize_to_boolean": "synthesize",
}


def test_documented_bsc_defaults_are_the_installed_bscs(tmp_path):
    """Where a setting's default is bsc's own, it matches what bsc 2026.07 does unasked --
    notably `aggressive_conditions` and `sched_conditions`, on since 2026.07."""
    require_bsc()
    record = _bsc_record(cwd=tmp_path)
    defaults = BscFlow.Settings()
    for setting, field in BSC_DEFAULTS.items():
        assert str(getattr(defaults, setting)) == record[field], setting
    assert f'"{defaults.unspecified_to}"' == record["unSpecTo"]
    assert defaults.resource_scheduling == "off" and record["resource"] == "RFoff"
    assert defaults.sat_solver == "yices" and record["satBackend"] == "SAT_Yices"


# ---------------------------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------------------------


def test_bsc_generates_every_module_the_top_needs(tmp_path):
    """The submodule of another package and the bsc library's FIFO2 are in the artifacts,
    which elaborate on their own; the version is recorded; macros reach the source."""
    require_bsc()
    require_iverilog()
    design = _accum_design(tmp_path / "d")
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert flow.results["modules"] == ["mkTop", "mkAccum", "FIFO2"]
    files = [Path(f) for f in flow.artifacts.verilog]
    assert [f.name for f in files] == ["bsv_defines.v", "FIFO2.v", "mkAccum.v", "mkTop.v"]
    assert all(f.read_text().startswith("`define BSV_POSITIVE_RESET\n") for f in files)
    bsc = next(tool for tool in flow.results.tools if tool["executable"] == "bsc")
    assert BscTool._version_is_gte(tuple(bsc["version"].split(".")), MIN_BSC_VERSION)
    subprocess.run(
        ["iverilog", "-o", str(tmp_path / "a.out"), "-s", "mkTop", *map(str, files)], check=True
    )


def test_a_macro_change_is_compiled(tmp_path):
    """bsc's `-u` recompiles only when a source is newer than its `.bo`, so a second run in the
    same directory with another macro reused the first run's packages. `cleanup_bobjs` (on by
    default) makes the second run compile what it was given."""
    require_bsc()
    design = _accum_design(tmp_path / "d")
    run_dir = tmp_path / "run"
    for step in (1, 5):
        design.rtl.defines = {"STEP": step}
        flow = _run(Bsc, design, run_dir)
        assert flow is not None and flow.succeeded
        top = next(Path(f) for f in flow.artifacts.verilog if f.endswith("mkTop.v"))
        assert f"8'd{step}" in top.read_text()


def test_bsc_compiles_bh(tmp_path):
    require_bsc()
    _write(tmp_path, {"Blink.bs": BH_PKG})
    design = Design(name="t", design_root=tmp_path, rtl={"sources": ["Blink.bs"], "top": "mkBlink"})
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert [Path(f).name for f in flow.artifacts.verilog] == ["bsv_defines.v", "mkBlink.v"]


def test_a_compile_error_fails_the_run(tmp_path):
    require_bsc()
    _write(
        tmp_path,
        {"Bad.bsv": "package Bad;\nmodule mkBad(Empty); nonsense; endmodule\nendpackage\n"},
    )
    design = Design(name="t", design_root=tmp_path, rtl={"sources": ["Bad.bsv"], "top": "mkBad"})
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and not flow.succeeded


SIMULATORS = ["bluesim", "verilator", "iverilog"]


def _require_simulator(simulator: str) -> None:
    if simulator == "bluesim":
        require_bluesim()
    else:
        require_bsc()
        (require_verilator if simulator == "verilator" else require_iverilog)()


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_bsc_sim_passes_and_fails(simulator, tmp_path):
    """A passing testbench passes, and a `$fatal` fails the run, on every simulator."""
    _require_simulator(simulator)
    design = _accum_design(tmp_path / "d")
    flow = _run(BscSim, design, tmp_path / "pass", simulator=simulator, vcd="waves.vcd")
    assert flow is not None and flow.succeeded
    assert flow.results["simulator"] == simulator
    assert Path(flow.artifacts.executable).is_file()
    assert (flow.run_path / "waves.vcd").is_file()
    # the testbench's macros reach every package it is compiled with, the RTL's too
    design.rtl.defines = {"STEP": 2}
    design.tb.defines = {"STEP": 3}
    flow = _run(BscSim, design, tmp_path / "step", simulator=simulator)
    assert flow is not None and flow.succeeded
    # a testbench that finds a mismatch fails the run -- and its waveform, the one most wanted,
    # is recorded all the same
    design.tb.defines = {"XEDA_INJECT_BUG": True}
    flow = _run(BscSim, design, tmp_path / "fail", simulator=simulator, vcd="fail.vcd")
    assert flow is not None and not flow.succeeded
    assert flow.results["simulator"] == simulator
    assert Path(flow.artifacts.vcd) == Path("fail.vcd") and (flow.run_path / "fail.vcd").is_file()


@pytest.mark.parametrize("simulator", ["verilator", "iverilog"])
def test_reset_name_and_polarity_reach_the_verilog_simulation(simulator, tmp_path):
    """bsc's clock and reset driver must reset the port `reset_prefix` names, with the polarity
    `positive_reset` gives; otherwise the testbench stays in reset and never finishes."""
    _require_simulator(simulator)
    _write(tmp_path, {"ResetTb.bsv": RESET_TB})
    design = Design(
        name="t",
        design_root=tmp_path,
        tb={"sources": ["ResetTb.bsv"], "top": "mkResetTb"},
        rtl={"sources": []},
    )
    flow = _run(
        BscSim,
        design,
        tmp_path / "run",
        simulator=simulator,
        reset_prefix="RST",
        positive_reset=True,
    )
    assert flow is not None and flow.succeeded
    verilog = (flow.run_path / "sim_build" / "mkResetTb.v").read_text()
    assert re.search(r"^\s*input\s+RST;", verilog, re.M)


def test_max_cycles_ends_a_bluesim_run(tmp_path):
    require_bluesim()
    _write(tmp_path, {"Endless.bsv": ENDLESS_TB})
    design = Design(
        name="t", design_root=tmp_path, rtl={"sources": ["Endless.bsv"], "top": "mkEndless"}
    )
    flow = _run(BscSim, design, tmp_path / "run", max_cycles=100)
    assert flow is not None and flow.succeeded


#: A testbench calling a C function through `import "BDPI"`.
BDPI_TB = """package Dpi;
import "BDPI" function Bit#(32) xeda_mac(Bit#(32) a, Bit#(32) b);
(* synthesize *)
module mkDpi(Empty);
  Reg#(Bit#(32)) n <- mkReg(0);
  rule check;
    Bit#(32) got = xeda_mac(n, 7);
    if (got != n * 3 + 7) $fatal(1, "FAIL: xeda_mac(%0d, 7) = %0d", n, got);
    n <= n + 1;
    if (n == 20) begin
      $display("PASS");
      $finish(0);
    end
  endrule
endmodule
endpackage
"""

MAC_CPP = 'extern "C" unsigned int xeda_mac(unsigned int a, unsigned int b) { return a * 3 + b; }\n'


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_an_imported_c_function_is_linked_into_the_simulation(simulator, tmp_path, capfd):
    """The design's C/C++ sources reach the link step: a testbench calling a C function
    (`import "BDPI"`) runs on every simulator -- through VPI, or the DPI for Verilator."""
    _require_simulator(simulator)
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.cpp": MAC_CPP})
    design = Design(
        name="dpi",
        design_root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.cpp", "Dpi.bsv"], "top": "mkDpi"},
    )
    flow = _run(
        BscSim, design, tmp_path / "run", simulator=simulator, use_dpi=simulator == "verilator"
    )
    out = capfd.readouterr().out
    assert flow is not None and flow.succeeded, out[-2000:]
    assert "PASS" in out


def test_without_positive_reset_the_verilog_is_left_as_generated(tmp_path):
    require_bsc()
    flow = _run(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run", positive_reset=False)
    assert flow is not None and flow.succeeded
    assert flow.artifacts.verilog
    for path in flow.artifacts.verilog:
        assert "`define BSV_POSITIVE_RESET" not in Path(path).read_text()


def test_verilog_filters_run_on_each_generated_file(tmp_path):
    """bsc runs each `verilog_filters` command on every Verilog file it generates, with the
    file's name as its argument; the library modules copied beside them are not generated."""
    require_bsc()
    stamp = tmp_path / "stamp.sh"
    stamp.write_text('#!/bin/sh\necho "// stamped by a verilog filter" >> "$1"\n')
    stamp.chmod(0o755)
    flow = _run(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run", verilog_filters=[str(stamp)])
    assert flow is not None and flow.succeeded
    stamped = {
        Path(path).name: "stamped by a verilog filter" in Path(path).read_text()
        for path in flow.artifacts.verilog
    }
    assert stamped == {"bsv_defines.v": False, "FIFO2.v": False, "mkAccum.v": True, "mkTop.v": True}


#: A BH package whose reset value depends on a macro: `#ifdef`, the C preprocessor's.
BH_MACRO_PKG = """package Knob where

interface Knob_IFC =
    value :: Bit 4

{-# verilog mkKnob #-}
mkKnob :: Module Knob_IFC
mkKnob = module
#ifdef KNOB
    r :: Reg (Bit 4) <- mkReg 5
#else
    r :: Reg (Bit 4) <- mkReg 0
#endif
    rules
      "tick": when True ==> r := r + 1
    interface
      value = r
"""


def test_cpp_hands_the_design_macros_to_bh_sources(tmp_path):
    """bsc's own preprocessor reads BSV only, so `-D` never reaches a BH source; the C
    preprocessor does, when the flow also defines the macros for it (`cpp`)."""
    require_bsc()
    _write(tmp_path, {"Knob.bs": BH_MACRO_PKG})
    for defines, reset in (({"KNOB": True}, "4'd5"), ({}, "4'd0")):
        design = Design(
            name="knob",
            design_root=tmp_path,
            rtl={"sources": ["Knob.bs"], "top": "mkKnob", "defines": defines},
        )
        flow = _run(Bsc, design, tmp_path / f"run_{reset}", cpp=True)
        assert flow is not None and flow.succeeded
        verilog = Path(flow.artifacts.verilog[-1]).read_text()
        assert f"r <= `BSV_ASSIGNMENT_DELAY {reset}" in verilog


def test_macros_that_cannot_reach_bh_sources_are_reported(tmp_path, caplog):
    require_bsc()
    _write(tmp_path, {"Blink.bs": BH_PKG})
    design = Design(
        name="blink",
        design_root=tmp_path,
        rtl={"sources": ["Blink.bs"], "top": "mkBlink", "defines": {"KNOB": True}},
    )
    with caplog.at_level(logging.WARNING):
        flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert "(KNOB) reach none of its sources, all of them BH (Blink.bs)" in caplog.text


#: A reset generated with `Clocks::mkReset`, whose library module `MakeResetA` instantiates
#: another library module, `SyncResetA`, that no `.use` file lists.
RESET_GEN_PKG = """package RstTop;
import Clocks::*;
interface RstTop_IFC;
  method Bit#(4) out;
endinterface
(* synthesize *)
module mkRstTop(RstTop_IFC);
  Clock clk <- exposeCurrentClock;
  MakeResetIfc rst <- mkReset(2, True, clk);
  Reg#(Bit#(4)) r <- mkReg(0, reset_by rst.new_rst);
  rule tick; r <= r + 1; endrule
  method out = r;
endmodule
endpackage
"""


def test_the_submodules_of_library_modules_are_collected(tmp_path):
    """A library module's own submodules are part of the file set: `MakeResetA` needs
    `SyncResetA`, which iverilog cannot elaborate the top without."""
    require_bsc()
    require_iverilog()
    _write(tmp_path, {"RstTop.bsv": RESET_GEN_PKG})
    design = Design(
        name="rst", design_root=tmp_path, rtl={"sources": ["RstTop.bsv"], "top": "mkRstTop"}
    )
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert {"MakeResetA", "SyncResetA"} <= set(flow.results["modules"])
    files = list(flow.artifacts.verilog)
    subprocess.run(
        ["iverilog", "-g2005", "-o", str(tmp_path / "a.out"), "-s", "mkRstTop", *files],
        check=True,
    )


#: A Verilog module in a file named otherwise, imported with `import "BVI"`, and a testbench.
OPS_V = "module add3(input CLK, input RST_N, input [7:0] a, output [7:0] y);\n  assign y = a + 8'd3;\nendmodule\n"

OPS_PKG = """package Ops;
interface Add3_IFC;
  method Bit#(8) calc(Bit#(8) a);
endinterface
import "BVI" add3 = module mkAdd3(Add3_IFC);
  default_clock clk(CLK, (*unused*) clk_gate);
  default_reset rst(RST_N);
  method y calc(a);
  schedule (calc) CF (calc);
endmodule
(* synthesize *)
module mkOpsTb(Empty);
  Add3_IFC dut <- mkAdd3;
  Reg#(Bit#(8)) i <- mkReg(0);
  rule check;
    if (dut.calc(i) != i + 3) $fatal(1, "FAIL: add3(%0d) = %0d", i, dut.calc(i));
    i <= i + 1;
    if (i == 10) begin
      $display("PASS");
      $finish(0);
    end
  endrule
endmodule
endpackage
"""


@pytest.mark.parametrize("simulator", ["verilator", "iverilog"])
def test_imported_verilog_in_a_file_named_otherwise_is_simulated(simulator, tmp_path, capfd):
    """bsc's Verilog link finds a module on its search path only in a file named after it; the
    design's Verilog sources are handed to it as files, so their names do not matter."""
    _require_simulator(simulator)
    _write(tmp_path, {"ops.v": OPS_V, "Ops.bsv": OPS_PKG})
    design = Design(
        name="ops",
        design_root=tmp_path,
        rtl={"sources": ["ops.v"]},
        tb={"sources": ["Ops.bsv"], "top": "mkOpsTb"},
    )
    flow = _run(BscSim, design, tmp_path / "run", simulator=simulator)
    out = capfd.readouterr().out
    assert flow is not None and flow.succeeded, out[-2000:]
    assert "PASS" in out


def test_a_plain_c_function_is_linked_into_bluesim(tmp_path, capfd):
    """bsc links `.c` sources too, which xeda gives no source type."""
    require_bluesim()
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.c": MAC_CPP.replace('extern "C" ', "")})
    design = Design(
        name="dpi_c",
        design_root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.c", "Dpi.bsv"], "top": "mkDpi"},
    )
    flow = _run(BscSim, design, tmp_path / "run")
    out = capfd.readouterr().out
    assert flow is not None and flow.succeeded, out[-2000:]
    assert "PASS" in out


def test_verilator_needs_the_dpi_for_imported_c_functions(tmp_path, monkeypatch):
    """Rejected before anything runs, rather than inside bsc's Verilator link script."""
    monkeypatch.setenv("PATH", "")
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.cpp": MAC_CPP})
    design = Design(
        name="dpi",
        design_root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.cpp", "Dpi.bsv"], "top": "mkDpi"},
    )
    with pytest.raises(FlowSettingsException, match="use_dpi = true"):
        _run(BscSim, design, tmp_path / "run", simulator="verilator")
