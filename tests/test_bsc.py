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
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda._internal.design import SourceType
from xeda._internal.flow import Flow, FlowFatalError, FlowSettingsError, FlowSettingsException
from xeda._internal.flow_runner import DefaultRunner
from xeda._internal.flow_runner.launch_result import launch_result
from xeda._internal.flows import Bsc, BscSim
from xeda._internal.flows import bsc as bsc_module
from xeda._internal.flows.bsc import (
    DIAGNOSTICS_SHOWN,
    LINK_LOG,
    MIN_BSC_VERSION,
    BscFlow,
    BscTool,
    _macro_flags,
    _parse_haskell_strings,
    diagnostic_lines,
)
from xeda._internal.proc_utils import ProcessTimeout
from xeda._internal.run_dir import RunDirectory
from xeda._internal.spec import Spec
from xeda._internal.utils import NonZeroExitCode

from .bsc_outputs import collected_verilog
from .design_edits import with_updates
from .tool_utils import (
    require_bluesim,
    require_bsc,
    require_c_toolchain,
    require_iverilog,
    require_verilator,
    run_report,
)

pytestmark = pytest.mark.usefixtures("tool_output_on_terminal")

# ---------------------------------------------------------------------------------------------
# small designs
# ---------------------------------------------------------------------------------------------

#: A synthesized submodule in a package of its own: its Verilog is generated while compiling the
#: package that imports it.
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

#: Finishes by itself only after a million cycles: `max_cycles` ends it long before.
ENDLESS_TB = """package Endless;
(* synthesize *)
module mkEndless(Empty);
  Reg#(UInt#(32)) n <- mkReg(0);
  rule tick; n <= n + 1; endrule
  rule stop (n == 1000000);
    $display("RAN TO THE END");
    $finish(0);
  endrule
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
    return Design(name="accum", root=root, **fields)


def _flow(flow_class: type[Flow], design: Design, run_path: Path, **settings: Any) -> Any:
    """A flow in `run_path`, a run directory xeda manages (under the run root its parent)."""
    run_path.mkdir(parents=True, exist_ok=True)
    run_directory = RunDirectory.claimed(run_path, run_path.parent)
    return flow_class(settings, design, run_path, run_directory=run_directory)


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
    assert _pairs(flags, "-promote-warnings") == ["S0091"]
    assert "-cross-info" not in flags and "-no-show-timestamps" in flags


def test_debug_does_not_change_the_generated_hardware(tmp_path):
    """`debug` adds no flag that changes the generated hardware (`-keep-fires`,
    `-remove-unused-modules`, `-cross-info`): a debug run generates the same design."""
    design = _accum_design(tmp_path / "d")
    plain = _flow(Bsc, design, tmp_path / "a")._compile_flags("verilog")
    debug = _flow(Bsc, design, tmp_path / "b", verbosity="debug")._compile_flags("verilog")
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
        nthreads=3,
    )
    bluesim = flow._link_flags(True)
    assert _pairs(bluesim, "-reset-prefix") == ["RST"]
    assert _pairs(bluesim, "-parallel-sim-link") == ["3"]
    assert _pairs(bluesim, "-Xc++") == ["-O3", "-O1"]
    assert _pairs(bluesim, "-l") == ["m"]
    assert _pairs(bluesim, "-I") == [str(design.root / "inc")]  # an input: the design's
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
        (Bsc, {"opt_undetermined_vals": False}, "requires `opt_undetermined_vals: true`"),
        (BscSim, {"unspecified_to": "X", "opt_undetermined_vals": True}, "two-valued"),
        (BscSim, {"stop_time": "100 ns"}, "max_cycles"),
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
    design = Design(name="t", root=tmp_path, rtl={"sources": ["Top.bsv"]})
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
        root=tmp_path,
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
        tb={"sources": ["test_top.py"], "cocotb": True},
    )
    with pytest.raises(FlowSettingsException, match="cocotb"):
        _run(BscSim, design, tmp_path / "run")


def test_a_bh_suffix_is_rejected_by_name(tmp_path):
    """bsc compiles BH only from `.bs`; xeda also calls `.bh` Bluespec, which bsc refuses."""
    _write(tmp_path, {"Blink.bh": BH_PKG})
    design = Design(name="t", root=tmp_path, rtl={"sources": ["Blink.bh"], "top": "mkBlink"})
    flow = _flow(Bsc, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException, match=r"rename: .*Blink\.bh"):
        flow._bsc_sources(tb=False)


@pytest.mark.parametrize("flow", ["bsc", "bsc_sim"])
def test_a_design_with_no_source_the_flow_reads_is_refused_when_planned(flow, tmp_path):
    """bsc reads Bluespec and Verilog; a netlist of another format is passed to no command."""
    _write(tmp_path, {"top.edf": "(edif top)\n"})
    design = Design(name="d", root=tmp_path, rtl={"sources": ["top.edf"], "top": "top"})
    with pytest.raises(FlowSettingsException, match=rf"{flow} reads none of the design's sources"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(flow, design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


def _design_with_an_unread_source(tmp_path, part, unread):
    _write(tmp_path, {"Top.bsv": TOP_PKG, "Tb.bsv": TB_PKG, unread: "-- not for bsc\n"})
    sources = {"rtl": ["Top.bsv"], "tb": ["Tb.bsv"]}
    sources[part].append(unread)
    return Design(
        name="d",
        root=tmp_path,
        rtl={"sources": sources["rtl"], "top": "mkTop"},
        tb={"sources": sources["tb"], "top": "mkTb"},
    )


@pytest.mark.parametrize("part", ["rtl", "tb"])
@pytest.mark.parametrize(
    ("unread", "language"), [("dut.vhd", "Vhdl"), ("dut.sc", "Chisel")], ids=["vhdl", "chisel"]
)
def test_a_language_bsc_sim_cannot_read_is_refused_by_name_when_planned(
    part, unread, language, tmp_path
):
    """A VHDL source beside Bluespec ones would be passed over, and bsc would simulate a design
    without it. The testbench counts as the RTL does."""
    design = _design_with_an_unread_source(tmp_path, part, unread)
    with pytest.raises(
        FlowSettingsException,
        match=rf"bsc_sim cannot read the design's {language} source\(s\) .*{unread}",
    ):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan("bsc_sim", design)
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


@pytest.mark.parametrize("part", ["rtl", "tb"])
@pytest.mark.parametrize("unread", ["dut.vhd", "dut.sc"], ids=["vhdl", "chisel"])
def test_bsc_plans_a_design_with_a_source_in_a_language_it_passes_on(part, unread, tmp_path):
    """`bsc` converts the Bluespec sources and passes every other source through, so it names no
    language it cannot read."""
    design = _design_with_an_unread_source(tmp_path, part, unread)
    plan = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan("bsc", design)
    assert [node.name for node in plan.nodes] == ["bsc"]
    assert not (tmp_path / "xeda_run").exists(), "a plan creates nothing"


def test_a_bluespec_design_with_imported_verilog_and_foreign_code_is_planned(tmp_path):
    """The imported Verilog module, the C++ function a testbench calls and the archive it links
    are sources `bsc_sim` reads beside the Bluespec ones."""
    names = ["pipe_mul.v", "Top.bsv", "mac.cpp", "lib.a", "kernel.c", "ops.o"]
    _write(tmp_path, {name: "// a source\n" for name in names})
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": names, "top": "mkTop"},
        tb={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    planned = DefaultRunner(tmp_path / "xeda_run", display_results=False).plan("bsc_sim", design)
    assert [node.name for node in planned.nodes] == ["bsc_sim"]


def test_the_foreign_code_bsc_links_is_chosen_by_source_type_not_by_suffix(tmp_path):
    """A source is what its `type` says: a C++ file named `.cu` is linked, an object file the
    design calls data is not. Inferred types agree with the suffixes bsc takes, so a design
    that gives no `type` links what it linked before."""
    names = ["mac.cpp", "kernel.cu", "lib.a", "ops.o", "plain.c", "notes.cc", "Dpi.bsv"]
    _write(tmp_path, {name: "// a source\n" for name in names})
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": []},
        tb={
            "sources": [
                "mac.cpp",
                {"file": "kernel.cu", "type": "Cpp"},
                "lib.a",
                {"file": "ops.o", "type": "Data"},
                "plain.c",
                {"file": "notes.cc", "type": "Data"},
                "Dpi.bsv",
            ],
            "top": "mkDpi",
        },
    )
    flow = _flow(BscSim, design, tmp_path / "run")
    assert [p.name for p in flow._foreign_sources()] == ["mac.cpp", "kernel.cu", "lib.a", "plain.c"]


def test_a_verilog_search_path_with_a_colon_is_rejected(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run", verilog_search_paths=["/a:b"])
    with pytest.raises(FlowSettingsException, match="':'"):
        flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)


def test_a_search_path_with_a_colon_is_staged_under_a_name_without_one(tmp_path):
    """bsc gets `bsc_search/0`, never the folder, so a colon in the folder name is no problem."""
    root = tmp_path / "d"
    design = _accum_design(root)
    _write(root, {"a:b/Extra.bsv": "package Extra; endpackage\n"})
    flow = _flow(Bsc, design, tmp_path / "run", search_paths=["a:b"])
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert flags[flags.index("-p") + 1] == "bsc_sources:bsc_search/0:%/Libraries"
    flow._stage(tb=False)
    assert (
        flow.run_path / "bsc_search" / "0" / "Extra.bsv"
    ).resolve() == root / "a:b" / "Extra.bsv"


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
        root=root,
        rtl={"sources": ["rtl/ext.v", "rtl/Top.bsv"], "top": "mkTop"},
    )
    out = tmp_path / "run" / "gen_rtl"  # the flow's own, inside its run directory
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
        collected = flow._collect_verilog("mkTop", root / "rtl/Top.bsv", out, [out, lib])
    modules, files = collected.modules, collected.files
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


def test_the_modules_a_design_verilog_source_instantiates_are_collected(tmp_path):
    """A Verilog wrapper the design imports may instantiate a library module, and a module bsc
    generated: both belong to the file set, which no `.use` file says."""
    root = tmp_path / "d"
    wrap = (
        "module wrap(input CLK);\n"
        "  mkInner inner (.CLK(CLK));\n"
        "  SizedFIFO #(.p1width(1)) f (.CLK(CLK));\n"
        "endmodule\n"
    )
    _write(root, {"rtl/Top.bsv": TOP_PKG, "rtl/wrap.v": wrap})
    design = Design(
        name="t",
        root=root,
        rtl={"sources": ["rtl/wrap.v", "rtl/Top.bsv"], "top": "mkTop"},
    )
    out = tmp_path / "run" / "gen_rtl"  # the flow's own, inside its run directory
    lib = tmp_path / "lib"
    _write(
        out,
        {
            "mkTop.v": "module mkTop(); endmodule\n",
            "mkTop.use": "wrap\n",
            "mkInner.v": "module mkInner(); endmodule\n",
            "mkInner.use": "",
        },
    )
    _write(lib, {"SizedFIFO.v": "module SizedFIFO(); endmodule\n"})
    flow = _flow(Bsc, design, tmp_path / "run", positive_reset=False)
    collected = flow._collect_verilog("mkTop", root / "rtl/Top.bsv", out, [out, lib])
    modules, files = collected.modules, collected.files
    assert modules == ["mkTop", "wrap", "mkInner", "SizedFIFO"]
    assert files == [out / "SizedFIFO.v", root / "rtl/wrap.v", out / "mkInner.v", out / "mkTop.v"]


def test_collect_verilog_reads_a_relative_search_path_in_the_run_directory(tmp_path):
    """A relative entry of the Verilog search path (a `-vsearch` among `extra_flags`) is relative
    to the run directory, where bsc runs, whatever the working directory of the process."""
    root = tmp_path / "d"
    _write(root, {"rtl/Top.bsv": TOP_PKG})
    design = Design(name="t", root=root, rtl={"sources": ["rtl/Top.bsv"], "top": "mkTop"})
    run = tmp_path / "run"
    out = run / "gen_rtl"
    _write(out, {"mkTop.v": "module mkTop(); endmodule\n", "mkTop.use": "FIFO2\n"})
    _write(run / "vlib", {"FIFO2.v": "module FIFO2(); endmodule\n"})
    flow = _flow(Bsc, design, run)
    collected = flow._collect_verilog("mkTop", root / "rtl/Top.bsv", out, [out, Path("vlib")])
    assert collected.modules == ["mkTop", "FIFO2"]
    assert (out / "FIFO2.v").read_text().endswith("module FIFO2(); endmodule\n")


def test_collect_verilog_needs_the_top(tmp_path):
    root = tmp_path / "d"
    _write(root, {"rtl/Top.bsv": TOP_PKG})
    design = Design(name="t", root=root, rtl={"sources": ["rtl/Top.bsv"], "top": "mkTop"})
    flow = _flow(Bsc, design, tmp_path / "run")
    (tmp_path / "out").mkdir()
    with pytest.raises(FlowSettingsException, match=r"last Bluespec source, Top\.bsv"):
        flow._collect_verilog("mkNope", root / "rtl/Top.bsv", tmp_path / "out", [])


def test_an_earlier_runs_output_is_removed_before_compiling(tmp_path):
    """Runs of a design share their directory. A module an earlier run generated -- which the
    design may since have replaced by Verilog of its own -- must not be found again, nor a
    package compiled with other flags. Every `.v`, `.use` and VPI wrapper file of the output
    folder goes, also a bare `.v` that no `.use` file marks; other files stay."""
    design = _accum_design(tmp_path / "d")
    run = tmp_path / "run"
    flow = _flow(Bsc, design, run)
    out = run / "gen_rtl"
    _write(
        out,
        {
            "mkOld.v": "module mkOld(); endmodule\n",
            "mkOld.use": "FIFO2\n",
            "mkBare.v": "module mkBare(); endmodule\n",
            "mkGone.use": "",  # its module removed by hand
            "FIFO2.v": "",
            "vpi_wrapper_f.c": "",
            "vpi_wrapper_f.h": "",
            "notes.txt": "the user's\n",
        },
    )
    _write(run / "bobjs", {"Old.bo": "", "Old.ba": ""})
    flow._prepare_dirs(out)
    left = {p.name for p in out.iterdir()} | {p.name for p in (run / "bobjs").iterdir()}
    assert left == {"notes.txt"}


def test_a_module_both_generated_and_a_design_source_is_an_error(tmp_path):
    """The file set would define it twice, which no downstream tool accepts."""
    root = tmp_path / "d"
    _write(root, {"rtl/Top.bsv": TOP_PKG, "rtl/mkSub.v": "module mkSub(); endmodule\n"})
    design = Design(
        name="t", root=root, rtl={"sources": ["rtl/mkSub.v", "rtl/Top.bsv"], "top": "mkTop"}
    )
    out = tmp_path / "run" / "gen_rtl"  # the flow's own, inside its run directory
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
XEDA_BOOLEANS = {"positive_reset"}


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
        (
            {"promote_warnings": ["G0010", "G0020"]},
            "promoteWarnings",
            'SomeMsgs ["G0010","G0020","S0091"]',
        ),
        ({"promote_warnings": ["ALL"]}, "promoteWarnings", "AllMsgs"),
        ({"promote_warnings": ["NONE"]}, "promoteWarnings", 'SomeMsgs ["S0091"]'),
        ({"promote_warnings": []}, "promoteWarnings", 'SomeMsgs ["S0091"]'),
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
    # bsc leaves a folder that does not exist out of the path it records.
    for folder in ("bobjs", "bsc_sources", "bsc_verilog", "bsc_search/0"):
        (tmp_path / "run" / folder).mkdir(parents=True)
    (tmp_path / "out").mkdir()
    record = _bsc_record(
        *flow._path_flags("verilog", tmp_path / "out", design.rtl.sources), cwd=tmp_path / "run"
    )
    # Neither the source folders nor the run directory (`+`, `.`) is on the package path.
    ifc_path = _parse_haskell_strings(record["ifcPath"])
    assert ifc_path[:3] == ["bobjs", "bsc_sources", "bsc_search/0"]
    assert len(ifc_path) == 4 and ifc_path[3].endswith("/Libraries")
    v_path = _parse_haskell_strings(record["vPath"])
    assert v_path[0] == str(tmp_path / "out")
    assert v_path[1].endswith("/Verilog.Vivado")
    assert v_path[2:4] == ["bsc_verilog", str(tmp_path / "vlib")]
    assert v_path[4].endswith("/Libraries") and v_path[5].endswith("/Verilog")
    assert len(v_path) == 6
    assert record["fdir"] == f'Just "{tmp_path / "files"}"'
    assert record["infoDir"] == f'Just "{tmp_path / "info"}"'


def test_bluesim_is_handed_no_verilog_search_path(tmp_path, capfd):
    """Bluesim reads no Verilog, and without `-vdir` bsc warns (S0073) about a `-vsearch`
    directory its default path already holds: that of a Bluespec source with Verilog beside it."""
    require_bluesim()
    root = tmp_path / "d"
    _write(root, {"rtl/extra.v": "module extra(); endmodule\n"})
    rtl = {"sources": ["rtl/extra.v", "lib/Accum.bsv", "rtl/Top.bsv"], "top": "mkTop"}
    design = _accum_design(root, rtl=rtl)
    flow = _run(BscSim, design, tmp_path / "run", promote_warnings=["ALL"])
    assert flow.succeeded, run_report(flow, capfd.readouterr())
    assert "-vsearch" not in flow._path_flags("sim", tmp_path / "out", design.rtl.sources)


@pytest.mark.parametrize("simulator", ["bluesim", "verilator", "iverilog"])
def test_the_path_flags_of_each_simulator(simulator, tmp_path):
    """`-fdir` reaches every compile; `-vsearch` only a Verilog one (Bluesim reads no Verilog);
    the output directory is `-simdir` for Bluesim and `-vdir` otherwise."""
    design = _accum_design(tmp_path / "d")
    (tmp_path / "d" / "files").mkdir()
    flow = _flow(BscSim, design, tmp_path / "run", simulator=simulator, fdir="files")
    backend = "sim" if simulator == "bluesim" else "verilog"
    flags = flow._path_flags(backend, tmp_path / "out", design.rtl.sources)
    assert flags[flags.index("-fdir") + 1] == str(tmp_path / "d" / "files")
    assert ("-vsearch" in flags) == (simulator != "bluesim")
    assert flags[flags.index("-simdir" if backend == "sim" else "-vdir") + 1] == str(
        tmp_path / "out"
    )


@pytest.mark.parametrize("simulator", ["verilator", "iverilog"])
def test_verilog_search_paths_reach_the_verilog_link(simulator, tmp_path, capfd):
    """An imported module found only on `verilog_search_paths`, as `<module>.v`, is linked."""
    _require_simulator(simulator)
    _write(tmp_path, {"vlib/add3.v": OPS_V, "Ops.bsv": OPS_PKG})
    design = Design(
        name="ops",
        root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["Ops.bsv"], "top": "mkOpsTb"},
    )
    flow = _run(
        BscSim, design, tmp_path / "run", simulator=simulator, verilog_search_paths=["vlib"]
    )
    captured = capfd.readouterr()
    out = captured.out
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in out


def test_extra_flags_reach_the_verilog_search_path_xeda_reads(tmp_path):
    """A `-vsearch` among `extra_flags` changes where bsc looks for Verilog, and so where the
    flow collects it from."""
    require_bsc()
    _write(tmp_path, {"vlib/add3.v": OPS_V, "Ops.bsv": OPS_PKG})
    design = Design(name="ops", root=tmp_path, rtl={"sources": ["Ops.bsv"], "top": "mkOpsTb"})
    vlib = tmp_path / "vlib"
    flow = _run(Bsc, design, tmp_path / "run", extra_flags=["-vsearch", f"{vlib}:+"])
    assert flow is not None and flow.succeeded
    assert "add3" in flow.results["modules"]
    assert any(Path(f).name == "add3.v" for f in collected_verilog(flow))


def test_a_link_path_with_whitespace_is_rejected_before_compiling(tmp_path):
    """bsc runs its link step through a shell, unquoted: a path with whitespace would fail
    there, obscurely, after the whole compilation."""
    design = _accum_design(tmp_path / "d")
    run = tmp_path / "run dir"
    flow = _flow(BscSim, design, run)
    with pytest.raises(FlowSettingsException, match="whitespace"):
        flow.run()
    assert not (run / "bobjs").exists() or not any((run / "bobjs").iterdir())  # nothing compiled


def test_verilog_filters_need_an_output_directory_without_whitespace(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run", verilog_filters=["true"])
    with pytest.raises(FlowSettingsException, match="whitespace"):
        flow._prepare_dirs(tmp_path / "gen rtl")


def test_bsc_sim_simulates_one_top(tmp_path):
    design = _accum_design(
        tmp_path / "d", tb={"sources": ["tb/Tb.bsv"], "top": ["mkTb", "mkOther"]}
    )
    with pytest.raises(FlowSettingsException, match="one top module"):
        _flow(BscSim, design, tmp_path / "run")


def test_the_define_is_prepended_to_verilog_in_any_encoding(tmp_path):
    """A library or imported module's file need not be UTF-8."""
    path = tmp_path / "ip.v"
    original = b"// Copyright Andr\xe9\nmodule ip(); endmodule\n"
    path.write_bytes(original)
    for _ in range(2):  # once only
        bsc_module._prepend_define(path, "BSV_POSITIVE_RESET")
    assert path.read_bytes() == b"`define BSV_POSITIVE_RESET\n" + original


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
    files = [Path(f) for f in collected_verilog(flow)]
    assert [f.name for f in files] == ["bsv_defines.v", "FIFO2.v", "mkAccum.v", "mkTop.v"]
    assert all(f.read_text().startswith("`define BSV_POSITIVE_RESET\n") for f in files)
    bsc = next(tool for tool in flow.results.tools if tool["executable"] == "bsc")
    assert BscTool._version_is_gte(tuple(bsc["version"].split(".")), MIN_BSC_VERSION)
    subprocess.run(
        ["iverilog", "-o", str(tmp_path / "a.out"), "-s", "mkTop", *map(str, files)], check=True
    )


def test_a_macro_change_is_compiled(tmp_path):
    """bsc's `-u` recompiles only when a source is newer than its `.bo`, so a second run in the
    same directory with another macro reused the first run's packages. Every run empties `bobjs`,
    so the second run compiles what it was given."""
    require_bsc()
    design = _accum_design(tmp_path / "d")
    run_paths = set()  # xeda reuses a flow's run directory, whatever the design's hash
    for step in (1, 5):
        flow = _run(Bsc, with_updates(design, rtl={"defines": {"STEP": step}}), tmp_path / "run")
        assert flow is not None and flow.succeeded
        run_paths.add(flow.run_path)
        top = next(Path(f) for f in collected_verilog(flow) if f.endswith("mkTop.v"))
        assert f"8'd{step}" in top.read_text()
    assert len(run_paths) == 1  # the second run met the first one's output


def test_a_testbench_change_is_simulated(tmp_path, capfd):
    """The same for `bsc_sim`: a second run in the same directory, with the testbench's bug
    planted by a macro, runs what it was given -- and fails."""
    require_bluesim()
    design = _accum_design(tmp_path / "d")
    flow = _run(BscSim, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert "PASS" in capfd.readouterr().out
    design = with_updates(design, tb={"defines": {"XEDA_INJECT_BUG": True}})
    second = _run(BscSim, design, tmp_path / "run")
    assert second is not None and second.run_path == flow.run_path
    assert not second.succeeded
    assert "FAIL" in capfd.readouterr().out


def test_bsc_compiles_bh(tmp_path):
    require_bsc()
    _write(tmp_path, {"Blink.bs": BH_PKG})
    design = Design(name="t", root=tmp_path, rtl={"sources": ["Blink.bs"], "top": "mkBlink"})
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert [Path(f).name for f in collected_verilog(flow)] == ["bsv_defines.v", "mkBlink.v"]


def test_a_compile_error_fails_the_run(tmp_path):
    require_bsc()
    _write(
        tmp_path,
        {"Bad.bsv": "package Bad;\nmodule mkBad(Empty); nonsense; endmodule\nendpackage\n"},
    )
    design = Design(name="t", root=tmp_path, rtl={"sources": ["Bad.bsv"], "top": "mkBad"})
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
def test_bsc_sim_passes_and_fails(simulator, tmp_path, capfd):
    """A passing testbench passes, and a `$fatal` fails the run, on every simulator."""
    _require_simulator(simulator)
    design = _accum_design(tmp_path / "d")
    # the waveform's directory is created: no simulator creates it
    flow = _run(BscSim, design, tmp_path / "pass", simulator=simulator, vcd="waves/pass.vcd")
    assert flow is not None and flow.succeeded
    assert "PASS" in capfd.readouterr().out
    assert flow.results["simulator"] == simulator
    assert Path(flow.artifacts.executable).is_file()
    assert (flow.run_path / "waves" / "pass.vcd").is_file()
    # the testbench's macros reach every package it is compiled with, the RTL's too
    stepped = with_updates(design, rtl={"defines": {"STEP": 2}}, tb={"defines": {"STEP": 3}})
    flow = _run(BscSim, stepped, tmp_path / "step", simulator=simulator)
    assert flow is not None and flow.succeeded
    # a testbench that finds a mismatch fails the run -- and its waveform, the one most wanted,
    # is recorded all the same
    buggy = with_updates(stepped, tb={"defines": {"XEDA_INJECT_BUG": True}})
    flow = _run(BscSim, buggy, tmp_path / "fail", simulator=simulator, vcd="fail.vcd")
    assert flow is not None and not flow.succeeded
    assert flow.results["simulator"] == simulator
    assert (
        Path(flow.artifacts.waveform) == Path("fail.vcd") and (flow.run_path / "fail.vcd").is_file()
    )


def _link_command(out: str) -> list[str]:
    """The bsc link command (`-e`) a flow printed."""
    commands = [line for line in out.splitlines() if line.startswith("Running `bsc ")]
    links = [c.strip("`").split()[1:] for c in commands if " -e " in c]
    assert len(links) == 1, commands
    return links[0]


@pytest.mark.parametrize("simulator", ["verilator", "iverilog"])
@pytest.mark.parametrize("positive_reset", [True, False])
def test_reset_name_and_polarity_reach_the_verilog_simulation(
    simulator, positive_reset, tmp_path, capfd
):
    """bsc's clock and reset driver must reset the port `reset_prefix` names, with the polarity
    `positive_reset` gives; otherwise the testbench stays in reset and never finishes."""
    _require_simulator(simulator)
    _write(tmp_path, {"ResetTb.bsv": RESET_TB})
    design = Design(
        name="t",
        root=tmp_path,
        tb={"sources": ["ResetTb.bsv"], "top": "mkResetTb"},
        rtl={"sources": []},
    )
    flow = _run(
        BscSim,
        design,
        tmp_path / "run",
        simulator=simulator,
        reset_prefix="RST",
        positive_reset=positive_reset,
    )
    captured = capfd.readouterr()
    out = captured.out
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    verilog = (flow.run_path / "sim_build" / "mkResetTb.v").read_text()
    assert re.search(r"^\s*input\s+RST;", verilog, re.M)
    # the driver takes its polarity from the define the link step is given, the design from
    # the compile's: both flip together, so the simulation alone cannot tell
    link = _link_command(out)
    assert ("BSV_POSITIVE_RESET" in link) == positive_reset
    assert link[link.index("-reset-prefix") + 1] == "RST"


def test_max_cycles_ends_a_bluesim_run(tmp_path, capfd):
    require_bluesim()
    _write(tmp_path, {"Endless.bsv": ENDLESS_TB})
    design = Design(name="t", root=tmp_path, rtl={"sources": ["Endless.bsv"], "top": "mkEndless"})
    flow = _run(BscSim, design, tmp_path / "run", max_cycles=100)
    assert flow is not None and flow.succeeded
    assert "RAN TO THE END" not in capfd.readouterr().out


#: The header comment of the Verilog module of the GCD example in the bsc user guide.
GCD_VERILOG_HEADER = """\
// Generated by Bluespec Compiler, version 2013.12.beta1 (build 32830, 2013-12-02)
//
// On Mon Dec 16 12:42:09 EST 2013
//
//
// Ports:
// Name                         I/O  size props
// RDY_start                      O     1
// result                         O    51 reg
// RDY_result                     O     1
// CLK                            I     1 clock
// RST_N                          I     1 reset
// start_num1                     I    51
// start_num2                     I    51
// EN_start                       I     1
//
// No combinational paths from inputs to outputs
//
//
module mkGCD(CLK, RST_N);
"""

EMPTY_VERILOG_HEADER = """\
// Ports:
// Name                         I/O  size props
// CLK                            I     1 clock
// RST_N                          I     1 reset
//
// No combinational paths from inputs to outputs
//
module mkTb(CLK, RST_N);
"""


def test_the_ports_of_a_verilog_module_with_methods_are_found():
    assert bsc_module._verilog_method_ports(GCD_VERILOG_HEADER) == [
        "RDY_start",
        "result",
        "RDY_result",
        "start_num1",
        "start_num2",
        "EN_start",
    ]


@pytest.mark.parametrize(
    "ports",
    [
        "// CLK                            I     1 clock\n// RST_N                          I     1 reset\n",
        # a clock and a reset that nothing uses carry no property but the default names
        "// CLK                            I     1 unused\n// RST_N                          I     1 unused\n",
        # a gate, a custom clock and reset, and a second clock of a module
        "// CLK                            I     1 clock\n// CLK_GATE                       I     1\n"
        "// CK                             I     1 clock\n// RESET                          I     1 reset\n"
        "// CLK_other                      I     1 clock\n// RST_N_other                    I     1 reset\n",
        # a module with no clock and no reset
        "",
    ],
)
def test_a_verilog_module_with_only_clock_and_reset_ports_has_no_methods(ports):
    header = f"// Ports:\n// Name                         I/O  size props\n{ports}//\n// No combinational\n"
    assert bsc_module._verilog_method_ports(header) == []


def test_the_ports_stop_at_the_end_of_the_header_comment():
    text = EMPTY_VERILOG_HEADER + "  input  extra;\n// Ports:\n"
    assert bsc_module._verilog_method_ports(text) == []


@pytest.mark.parametrize(
    "text",
    ["", "module mkTb(CLK);\n", "// Ports:\n// something else\n", "// Name  I/O  size props\n"],
    ids=["empty", "no header", "no table", "no ports line"],
)
def test_a_verilog_module_without_the_port_table_is_not_judged(text):
    assert bsc_module._verilog_method_ports(text) is None


BLUESIM_METHODS_HEADER = """\
class MOD_mkIfc : public Module {
 public:
  tUInt8 PORT_RST_N;
  tUInt8 PORT_EN_inc;
  tUInt8 METH_read();
  tUInt8 METH_RDY_read();
  void METH_inc();
  tUInt8 METH_RDY_inc();
  void reset_RST_N(tUInt8 ARG_rst_in);
};
"""

BLUESIM_EMPTY_HEADER = """\
class MOD_mkTb : public Module {
 public:
  tUInt8 PORT_RST_N;
  void reset_RST_N(tUInt8 ARG_rst_in);
};
"""


def test_the_methods_of_a_bluesim_module_are_found():
    assert bsc_module._bluesim_methods(BLUESIM_METHODS_HEADER, "mkIfc") == ["read", "inc"]
    assert bsc_module._bluesim_methods(BLUESIM_EMPTY_HEADER, "mkTb") == []


def test_a_bluesim_header_of_another_module_is_not_judged():
    assert bsc_module._bluesim_methods(BLUESIM_EMPTY_HEADER, "mkIfc") is None
    assert bsc_module._bluesim_methods("", "mkTb") is None


#: A module with two methods, which nothing calls, so a simulation of it never ends.
METHODS_PKG = """package Methods;
interface Ifc;
  method Bit#(8) read;
  method Action inc;
endinterface
(* synthesize *)
module mkIfc(Ifc);
  Reg#(Bit#(8)) r <- mkReg(0);
  method read = r;
  method Action inc; r <= r + 1; endmethod
endmodule
endpackage
"""

#: A testbench that has `mkIfc` as its design under test and nothing to stop the simulation.
METHODS_TB_PKG = """package MethodsTb;
import Methods::*;
(* synthesize *)
module mkMethodsTb(Ifc);
  Ifc dut <- mkIfc;
  return dut;
endmodule
endpackage
"""


def _methods_design(root: Path, *, testbench: bool) -> Design:
    _write(root, {"Methods.bsv": METHODS_PKG, "MethodsTb.bsv": METHODS_TB_PKG})
    if testbench:
        return Design(
            name="t",
            root=root,
            rtl={"sources": ["Methods.bsv"], "top": "mkIfc"},
            tb={"sources": ["MethodsTb.bsv"], "top": "mkMethodsTb"},
        )
    return Design(name="t", root=root, rtl={"sources": ["Methods.bsv"], "top": "mkIfc"})


@pytest.mark.parametrize("testbench", [False, True], ids=["rtl.top", "tb.top"])
@pytest.mark.parametrize("simulator", ["bluesim", "iverilog"])
def test_a_top_with_methods_fails_before_the_simulation(simulator, testbench, tmp_path):
    """bsc drives the clock and the reset until the module calls `$finish`. Nothing calls a
    method, so the run fails before the simulation of a module with methods."""
    _require_simulator(simulator)
    design = _methods_design(tmp_path / "d", testbench=testbench)
    # a regression would hang: the time limit turns that into a failure with another code
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    settings = {"simulator": simulator, "timeout": "60 s"}
    result = launch_result(
        runner,
        Spec(BscSim.name, design),
        lambda: runner.run_flow(BscSim, design, settings),
    )
    assert result.outcome == "failed"
    error = result.error
    assert error is not None and error.code == "reported_failure"
    top = "mkMethodsTb" if testbench else "mkIfc"
    assert f"bsc_sim cannot simulate `{top}`" in error.message
    assert "set `tb.top`" in error.message
    assert ("set `max_cycles`" in error.message) is (simulator == "bluesim")
    assert "until the module calls `$finish`" in error.message
    assert ("The design has no testbench" in error.message) is (not testbench)
    names = ["read", "inc"] if simulator == "bluesim" else ["RDY_read", "EN_inc"]
    assert all(name in error.message for name in names)
    # the simulation never started
    run_path = Path(result.nodes[BscSim.name].run_dir)
    simulator_log = "vvp.log" if simulator == "iverilog" else f"{top}.log"
    for name in ("sim.log", "xeda_end.json", "bluesim_events.jsonl", "bluesim_end.json"):
        assert not (run_path / name).exists()
    assert not (run_path / "logs" / simulator_log).exists()


def test_max_cycles_bounds_bluesim_run_of_a_top_with_methods(tmp_path, capfd):
    """The limit ends the run, so the module needs no `$finish`: it is not refused."""
    require_bluesim()
    design = _methods_design(tmp_path / "d", testbench=False)
    flow = _run(BscSim, design, tmp_path / "run", max_cycles=100, timeout="60 s")
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)


def test_the_check_names_a_file_this_run_did_not_write(tmp_path, monkeypatch):
    design = _methods_design(tmp_path / "d", testbench=False)
    flow = _flow(BscSim, design, tmp_path / "run")
    monkeypatch.setattr(BscSim, "report_file", lambda self, path, **kw: None)
    with pytest.raises(FlowFatalError, match="this run's bsc did not write .*mkIfc.h"):
        flow._require_empty_interface("mkIfc", tmp_path / "run" / "sim_build", True)


#: An `Empty` testbench that uses neither a reset (`mkRegU`) nor a method.
NO_RESET_TB = """package NoReset;
(* synthesize *)
module mkNoReset(Empty);
  Reg#(UInt#(8)) n <- mkRegU;
  rule tick; n <= n + 1; endrule
  rule stop (n == 5);
    $display("PASS");
    $finish(0);
  endrule
endmodule
endpackage
"""


def _port_table(*rows: str) -> str:
    return "// Ports:\n// Name                         I/O  size props\n" + "".join(
        f"// {row}\n" for row in rows
    )


def test_a_reset_prefix_names_the_reset_port_that_is_not_a_method():
    header = _port_table(
        "CLK                            I     1 clock",
        "RST                            I     1",
        "RST_other                      I     1",
    )
    assert bsc_module._verilog_method_ports(header) == ["RST", "RST_other"]
    assert bsc_module._verilog_method_ports(header, "RST") == []


def test_an_unused_input_with_a_name_the_design_chose_is_not_a_method_port():
    header = _port_table(
        "CK                             I     1 unused",
        "RESET_B                        I     1 unused",
    )
    assert bsc_module._verilog_method_ports(header) == []


def test_every_output_and_an_input_without_a_clock_property_is_a_method_port():
    # a value method that is always ready has an output row only
    assert bsc_module._verilog_method_ports(
        _port_table("value                          O     8 const", "CLK  I     1 unused")
    ) == ["value"]
    # an output of a clock or a reset is a member of the interface too
    assert bsc_module._verilog_method_ports(
        _port_table("CLK_out                        O     1 clock", "RST_N_out  O  1 reset")
    ) == ["CLK_out", "RST_N_out"]
    # an input argument that nothing says is a clock
    assert bsc_module._verilog_method_ports(
        _port_table("CLK                            I     1 clock", "start_num1  I  51")
    ) == ["start_num1"]
    assert bsc_module._verilog_method_ports(_port_table("data  IO  8")) == ["data"]
    # a Verilog name may hold `$`, for example where a BH name has a prime
    assert bsc_module._verilog_method_ports(_port_table("get$x  O  8 reg")) == ["get$x"]


@pytest.mark.parametrize("simulator", ["bluesim", "iverilog"])
def test_an_empty_testbench_with_an_unused_reset_runs_again_in_its_run_directory(
    simulator, tmp_path, capfd
):
    """A reset that nothing uses has no property in the port table, the name `reset_prefix`
    gives it included. A second run, in a run directory that holds the first run's files, finds
    that bsc kept its up-to-date outputs: the check still reads them."""
    _require_simulator(simulator)
    _write(tmp_path, {"NoReset.bsv": NO_RESET_TB})
    design = Design(name="t", root=tmp_path, rtl={"sources": ["NoReset.bsv"], "top": "mkNoReset"})
    settings = {"simulator": simulator, "reset_prefix": "RST", "timeout": "60 s"}
    for sim_args in ([], ["+again"]):
        flow = _run(BscSim, design, tmp_path / "run", sim_args=sim_args, **settings)
        captured = capfd.readouterr()
        assert flow is not None and flow.succeeded, run_report(flow, captured)
        assert "PASS" in captured.out


def test_a_verilog_filter_that_removes_the_port_table_skips_the_check(tmp_path, caplog, capfd):
    """Without a port table the check cannot tell: it warns and lets the run go on."""
    require_bsc()
    require_iverilog()
    strip = tmp_path / "strip.sh"
    strip.write_text('#!/bin/sh\ngrep -v "^//" "$1" > "$1.new" && mv "$1.new" "$1"\n')
    strip.chmod(0o755)
    _write(tmp_path, {"NoReset.bsv": NO_RESET_TB})
    design = Design(name="t", root=tmp_path, rtl={"sources": ["NoReset.bsv"], "top": "mkNoReset"})
    with caplog.at_level(logging.WARNING):
        flow = _run(
            BscSim,
            design,
            tmp_path / "run",
            simulator="iverilog",
            timeout="60 s",
            verilog_filters=[str(strip)],
        )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "the generated Verilog of `mkNoReset` has no port table" in caplog.text


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
    require_c_toolchain()
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.cpp": MAC_CPP})
    design = Design(
        name="dpi",
        root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.cpp", "Dpi.bsv"], "top": "mkDpi"},
    )
    flow = _run(
        BscSim, design, tmp_path / "run", simulator=simulator, use_dpi=simulator == "verilator"
    )
    captured = capfd.readouterr()
    out = captured.out
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in out


def test_without_positive_reset_the_verilog_is_left_as_generated(tmp_path):
    require_bsc()
    flow = _run(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run", positive_reset=False)
    assert flow is not None and flow.succeeded
    assert collected_verilog(flow)
    for path in collected_verilog(flow):
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
        for path in collected_verilog(flow)
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
            root=tmp_path,
            rtl={"sources": ["Knob.bs"], "top": "mkKnob", "defines": defines},
        )
        flow = _run(Bsc, design, tmp_path / f"run_{reset}", cpp=True)
        assert flow is not None and flow.succeeded
        verilog = Path(collected_verilog(flow)[-1]).read_text()
        assert f"r <= `BSV_ASSIGNMENT_DELAY {reset}" in verilog


def test_macros_that_cannot_reach_bh_sources_are_reported(tmp_path, caplog):
    require_bsc()
    _write(tmp_path, {"Blink.bs": BH_PKG})
    design = Design(
        name="blink",
        root=tmp_path,
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
    design = Design(name="rst", root=tmp_path, rtl={"sources": ["RstTop.bsv"], "top": "mkRstTop"})
    flow = _run(Bsc, design, tmp_path / "run")
    assert flow is not None and flow.succeeded
    assert {"MakeResetA", "SyncResetA"} <= set(flow.results["modules"])
    files = list(collected_verilog(flow))
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
        root=tmp_path,
        rtl={"sources": ["ops.v"]},
        tb={"sources": ["Ops.bsv"], "top": "mkOpsTb"},
    )
    flow = _run(BscSim, design, tmp_path / "run", simulator=simulator)
    captured = capfd.readouterr()
    out = captured.out
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in out


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_a_plain_c_function_is_linked_into_the_simulation(simulator, tmp_path, capfd):
    """bsc links `.c` sources too (type `C`), as it links `.cpp` ones, on every simulator."""
    _require_simulator(simulator)
    require_c_toolchain()
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.c": MAC_CPP.replace('extern "C" ', "")})
    design = Design(
        name="dpi_c",
        root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.c", "Dpi.bsv"], "top": "mkDpi"},
    )
    flow = _run(
        BscSim, design, tmp_path / "run", simulator=simulator, use_dpi=simulator == "verilator"
    )
    captured = capfd.readouterr()
    out = captured.out
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in out


#: C functions for `import "BDPI"` from two sources of one name in two folders, each including a
#: header of one name beside it, from a C++ source of the same stem, and from a prebuilt object
#: beside one of them under its name.
BDPI_FOLDERS_TB = """package Dpi;
import "BDPI" function Bit#(32) xeda_mul(Bit#(32) a);
import "BDPI" function Bit#(32) xeda_add(Bit#(32) a);
import "BDPI" function Bit#(32) xeda_pre(Bit#(32) a);
import "BDPI" function Bit#(32) xeda_dec(Bit#(32) a);
(* synthesize *)
module mkDpi(Empty);
  rule check;
    if (xeda_mul(2) == 6 && xeda_add(2) == 102 && xeda_pre(2) == 7 && xeda_dec(2) == 1)
      $display("PASS");
    else $display("FAIL: %0d %0d %0d %0d", xeda_mul(2), xeda_add(2), xeda_pre(2), xeda_dec(2));
    $finish(0);
  endrule
endmodule
endpackage
"""


@pytest.mark.parametrize("simulator", SIMULATORS)
def test_the_link_compiles_c_sources_in_the_run_directory(simulator, tmp_path, capfd):
    """bsc writes the object of a C source beside the name it is given, so the source gets a
    wrapper in the run directory: the design's folders stay as they were, a prebuilt object beside
    a source of its name included, and each source still includes the header beside it."""
    _require_simulator(simulator)
    require_c_toolchain()
    root = tmp_path / "design"
    _write(
        root,
        {
            "Dpi.bsv": BDPI_FOLDERS_TB,
            "mul/ops.h": "#define K 3\n",
            "mul/ops.c": '#include "ops.h"\nunsigned xeda_mul(unsigned a) { return a * K; }\n',
            "mul/ops.cpp": 'extern "C" unsigned xeda_dec(unsigned a) { return a - 1; }\n',
            "add/ops.h": "#define K 100\n",
            "add/ops.c": '#include "ops.h"\nunsigned xeda_add(unsigned a) { return a + K; }\n',
        },
    )
    (tmp_path / "pre.c").write_text("unsigned int xeda_pre(unsigned int a) { return a + 5; }\n")
    compiler = os.environ.get("CC") or shutil.which("cc") or "clang"
    subprocess.run(
        [compiler, "-c", "-fPIC", "pre.c", "-o", str(root / "add" / "ops.o")],
        cwd=tmp_path,
        check=True,
    )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    design = Design(
        name="dpi_folders",
        root=root,
        rtl={"sources": []},
        tb={
            "sources": ["mul/ops.c", "mul/ops.cpp", "add/ops.c", "add/ops.o", "Dpi.bsv"],
            "top": "mkDpi",
        },
    )
    flow = _run(
        BscSim, design, tmp_path / "run", simulator=simulator, use_dpi=simulator == "verilator"
    )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in captured.out
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_verilator_needs_the_dpi_for_imported_c_functions(tmp_path, monkeypatch):
    """Rejected before anything runs, rather than inside bsc's Verilator link script."""
    monkeypatch.setenv("PATH", "")
    _write(tmp_path, {"Dpi.bsv": BDPI_TB, "mac.cpp": MAC_CPP})
    design = Design(
        name="dpi",
        root=tmp_path,
        rtl={"sources": []},
        tb={"sources": ["mac.cpp", "Dpi.bsv"], "top": "mkDpi"},
    )
    with pytest.raises(FlowSettingsException, match="use_dpi: true"):
        _run(BscSim, design, tmp_path / "run", simulator="verilator")


def test_bsc_sim_time_limit_stops_a_testbench_that_never_finishes(tmp_path):
    import time

    require_bsc()
    (tmp_path / "Tb.bsv").write_text(
        "package Tb;\nmodule mkTb(Empty);\n  Reg#(UInt#(32)) n <- mkReg(0);\n"
        "  rule tick; n <= n + 1; endrule\nendmodule\nendpackage\n"
    )
    design = Design(
        name="tb",
        root=tmp_path,
        rtl={"sources": ["Tb.bsv"], "top": "mkTb"},
        tb={"sources": ["Tb.bsv"], "top": "mkTb"},
    )
    start = time.monotonic()
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        BscSim, design, {"timeout": "5 s"}
    )
    assert flow is not None and not flow.succeeded
    assert time.monotonic() - start < 60


# ---------------------------------------------------------------------------------------------
# a failed bsc run says why
# ---------------------------------------------------------------------------------------------

#: A parallel `make` that fails: the compiler's error is far from the last line of the output.
LINK_FAILURE = """\
Verilator C++ model generation...
g++ -c VmkTbAscon___024root__Slow.cpp
<command-line>: fatal error: VmkTbAscon__pch.h.slow: No such file or directory
compilation terminated.
make: *** [VmkTbAscon.mk:73: VmkTbAscon___024root__Slow.o] Error 1
<command-line>: fatal error: VmkTbAscon__pch.h.slow: No such file or directory
compilation terminated.
make: *** Waiting for unfinished jobs....
make: Leaving directory '/run/verilator'
"""


def test_the_diagnostic_lines_of_a_failed_link_are_the_errors_each_once():
    assert diagnostic_lines(LINK_FAILURE) == [
        "<command-line>: fatal error: VmkTbAscon__pch.h.slow: No such file or directory",
        "make: *** [VmkTbAscon.mk:73: VmkTbAscon___024root__Slow.o] Error 1",
    ]


def test_the_diagnostic_lines_cover_each_tool_bsc_runs():
    text = "\n".join(
        [
            'Warning: "A.bsv", line 3, column 1: (G0010) a warning',
            "\x1b[1mfoo.cpp:12:5: error: 'x' was not declared in this scope\x1b[0m",
            "foo.cpp:12:5: warning: unused variable 'y'",
            "ld: foo.o: in function `main': undefined reference to `missing'",
            "collect2: error: ld returned 1 exit status",
            "make[2]: *** [Makefile:9: all] Error 2",
            'Error: "B.bsv", line 5, column 3: (T0043)',
            "  Cannot synthesize `mkB'",
            "  because of an earlier error.",
            "Compilation failed",
            "%Error: top.v:3:4: syntax error, unexpected ')'",
            "%Error-UNSUPPORTED: top.v:9:1: Unsupported: a construct",
            "                   : ... note: In instance 'top'",
            "%Error: Exiting due to 2 error(s)",
        ]
    )
    assert diagnostic_lines(text) == [
        "foo.cpp:12:5: error: 'x' was not declared in this scope",
        "ld: foo.o: in function `main': undefined reference to `missing'",
        "collect2: error: ld returned 1 exit status",
        "make[2]: *** [Makefile:9: all] Error 2",
        'Error: "B.bsv", line 5, column 3: (T0043) Cannot synthesize `mkB\' because of an earlier'
        " error.",
        "%Error: top.v:3:4: syntax error, unexpected ')'",
        "%Error-UNSUPPORTED: top.v:9:1: Unsupported: a construct : ... note: In instance 'top'",
    ]


def test_a_long_diagnostic_is_shortened():
    (line,) = diagnostic_lines('Error: "A.bsv", line 1, column 1: (T0020)\n' + "  word" * 200)
    assert len(line) == 300 and line.endswith("...")


def test_an_output_with_no_error_has_no_diagnostic_lines():
    assert diagnostic_lines("Warning: something\ncompilation terminated.\n") == []
    assert diagnostic_lines("") == []


class _FailingBsc:
    """A stand-in for the `bsc` tool whose run writes `output` to its log and fails."""

    def __init__(self, output: str, error: NonZeroExitCode) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run(self, *args: Any, env: Any, tee: Path, merge_stderr: bool) -> None:
        self.calls.append({"args": args, "env": env, "tee": tee, "merge_stderr": merge_stderr})
        tee.write_text(self.output)
        raise self.error


def _failing_flow(tmp_path, monkeypatch, output: str, error: NonZeroExitCode):
    flow = _flow(BscSim, _accum_design(tmp_path / "d"), tmp_path / "run")
    stub = _FailingBsc(output, error)
    monkeypatch.setitem(flow.__dict__, "bsc", stub)
    return flow, stub


def test_a_failed_bsc_run_reports_the_first_errors_of_its_log(tmp_path, monkeypatch):
    error = NonZeroExitCode(["bsc", "-verilog", "-e", "mkTb"], 2)
    flow, stub = _failing_flow(tmp_path, monkeypatch, LINK_FAILURE, error)
    with pytest.raises(NonZeroExitCode) as raised:
        flow._run_bsc(LINK_LOG, "-verilog", "-e", "mkTb")
    failure = raised.value
    assert type(failure) is NonZeroExitCode
    assert failure.exit_code == 2
    assert failure.command_args == "bsc -verilog -e mkTb"
    message = str(failure)
    assert "VmkTbAscon__pch.h.slow: No such file or directory" in message
    assert "Error 1" in message
    assert "more in" not in message
    assert failure.__cause__ is error
    # the output was copied to the log in the run directory, with bsc's stderr in it
    (call,) = stub.calls
    assert call["tee"] == flow.run_path / LINK_LOG and call["merge_stderr"] is True
    assert (flow.run_path / LINK_LOG).read_text() == LINK_FAILURE


def test_a_failed_bsc_run_with_many_errors_names_the_log(tmp_path, monkeypatch):
    output = "".join(f"c{n}.cpp:1:1: error: problem {n}\n" for n in range(DIAGNOSTICS_SHOWN + 3))
    flow, _ = _failing_flow(tmp_path, monkeypatch, output, NonZeroExitCode(["bsc"], 1))
    with pytest.raises(NonZeroExitCode) as raised:
        flow._run_bsc(LINK_LOG)
    message = str(raised.value)
    assert "problem 0" in message and f"problem {DIAGNOSTICS_SHOWN - 1}" in message
    assert f"problem {DIAGNOSTICS_SHOWN}" not in message
    assert f"(and 3 more in {LINK_LOG})" in message


@pytest.mark.parametrize(
    "error",
    [NonZeroExitCode(["bsc"], 3), ProcessTimeout(["bsc"], 5)],
    ids=["no-error-line", "timeout"],
)
def test_a_failed_bsc_run_keeps_its_own_failure_unless_the_log_names_an_error(
    error, tmp_path, monkeypatch
):
    output = "all quiet\n" if type(error) is NonZeroExitCode else LINK_FAILURE
    flow, _ = _failing_flow(tmp_path, monkeypatch, output, error)
    with pytest.raises(NonZeroExitCode) as raised:
        flow._run_bsc(LINK_LOG)
    assert raised.value is error


# ---------------------------------------------------------------------------------------------
# a Bluespec include file is a declared source
# ---------------------------------------------------------------------------------------------

#: A testbench whose cycle count comes from the include file `Limit.bsvi`.
INCLUDE_TB = """package IncTb;
`include "Limit.bsvi"
(* synthesize *)
module mkIncTb(Empty);
  Reg#(UInt#(8)) cycle <- mkReg(0);
  rule tick; cycle <= cycle + 1; endrule
  rule check (cycle == `LIMIT);
    $display("PASS after %0d", cycle);
    $finish(0);
  endrule
endmodule
endpackage
"""


def _include_design(root: Path, limit: int) -> Design:
    _write(root, {"IncTb.bsv": INCLUDE_TB, "Limit.bsvi": f"`define LIMIT {limit}\n"})
    return Design(
        name="inc",
        root=root,
        rtl={"sources": ["IncTb.bsv", "Limit.bsvi"], "top": "mkIncTb"},
    )


def test_a_listed_include_file_is_a_bluespec_header_and_is_never_compiled(tmp_path):
    design = _include_design(tmp_path, 5)
    header = design.rtl.sources[1]
    assert header.type is SourceType.BluespecHeader
    for flow_class in (Bsc, BscSim):
        assert SourceType.BluespecHeader in flow_class.reads_sources
        flow = _flow(flow_class, design, tmp_path / f"run_{flow_class.name}")
        assert [src.file.name for src in flow._bsc_sources(tb=False)] == ["IncTb.bsv"]


def test_an_include_file_runs_the_flow_again_when_it_changes(tmp_path, capfd):
    require_bluesim()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    settings = {"simulator": "bluesim", "timeout": "60 s"}
    dispositions = []
    for limit in (5, 5, 6):
        design = _include_design(tmp_path / "d", limit)
        result = launch_result(
            runner,
            Spec(BscSim.name, design),
            lambda design=design: runner.run_flow(BscSim, design, settings),
        )
        captured = capfd.readouterr()
        assert result.outcome == "passed", captured.out + captured.err
        dispositions.append(result.nodes[BscSim.name].disposition)
    assert dispositions == ["ran", "reused", "ran"]


def test_the_derived_design_of_bsc_holds_no_bluespec_header(tmp_path, capfd):
    require_bsc()
    design = _include_design(tmp_path / "d", 5)
    flow = _run(Bsc, design, tmp_path / "run")
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    record = flow.results["outputs"]["design"]
    assert {item["type"] for item in record["sources"]} == {"Verilog"}
    assert not any("source" in item for item in record["sources"]), "nothing passes through"


# ---------------------------------------------------------------------------------------------
# bsc reads only the files that the design lists
# ---------------------------------------------------------------------------------------------


def _compile_package(source: Path, into: Path, *search: Path) -> None:
    """Compile the package `source` with bsc into `into`: a foreign `.bo` file for a test."""
    into.mkdir(parents=True, exist_ok=True)
    path = ":".join([*map(str, search), "+"])
    subprocess.run(
        ["bsc", "-u", "-verilog", "-bdir", str(into), "-vdir", str(into), "-p", path, str(source)],
        check=True,
        capture_output=True,
        cwd=into,
    )


def _helper_pkg(constant: int) -> str:
    return f"""package Helper;
function Bit#(8) bump(Bit#(8) x) = x + {constant};
endpackage
"""


def _helper_top(root: Path, helper: str = "Helper") -> None:
    _write(
        root,
        {
            "Top.bsv": f"""package Top;
import {helper}::*;
interface Top_IFC;
  method Bit#(8) out;
endinterface
(* synthesize *)
module mkTop(Top_IFC);
  method out = bump(8'd10);
endmodule
endpackage
""",
        },
    )


def _run_directly(flow_class: type[Flow], design: Design, run: Path, **settings: Any) -> Any:
    """Run a flow in `run` without the launcher, so a test can plant files there first."""
    flow = _flow(flow_class, design, run, **settings)
    flow.init()
    flow.run()
    return flow


def _failure_text(run: Path, error: BaseException) -> str:
    log_file = run / bsc_module.COMPILE_LOG
    return f"{error}\n{log_file.read_text() if log_file.exists() else ''}"


def test_a_stray_compiled_package_beside_a_listed_source_is_not_used(tmp_path):
    """bsc compares a source with a `Helper.bo` beside it and takes the newer. A listed
    function-only package has no `.bo` of its own, so a foreign one that is newer than the source
    replaced it on a clean run. The stage folder holds links to the listed sources only."""
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"Helper.bsv": _helper_pkg(1)})
    _compile_package(root / "Helper.bsv", root)  # a stray `Helper.bo`, with another body
    _write(root, {"Helper.bsv": _helper_pkg(3)})
    old = (root / "Helper.bo").stat().st_mtime - 100
    os.utime(root / "Helper.bsv", (old, old))
    design = Design(name="g", root=root, rtl={"sources": ["Helper.bsv", "Top.bsv"], "top": "mkTop"})
    flow = _run_directly(Bsc, design, tmp_path / "run")
    verilog = (flow.run_path / "gen_rtl" / "mkTop.v").read_text()
    assert "8'd13" in verilog and "8'd11" not in verilog


def test_an_unlisted_package_beside_a_listed_source_is_not_found(tmp_path):
    """A package that lies beside a listed source but is not listed is no part of the design:
    bsc fails with S0000, where it compiled the file through `-u` before."""
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"Helper.bsv": _helper_pkg(1)})
    design = Design(name="d", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    with pytest.raises(NonZeroExitCode) as error:
        _run_directly(Bsc, design, tmp_path / "run")
    assert "S0000" in _failure_text(tmp_path / "run", error.value)


def test_a_compiled_package_without_a_source_beside_a_listed_source_is_not_imported(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"scratch/Helper.bsv": _helper_pkg(1)})
    _compile_package(root / "scratch" / "Helper.bsv", root)  # `Helper.bo` beside `Top.bsv`
    design = Design(name="c", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    with pytest.raises(NonZeroExitCode) as error:
        _run_directly(Bsc, design, tmp_path / "run")
    assert "S0000" in _failure_text(tmp_path / "run", error.value)


def test_a_stray_package_in_the_run_directory_is_ignored(tmp_path):
    """The run directory is not on bsc's package path: a `Helper.bo` or a `mkAccum.ba` that lies
    in it is never read."""
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"scratch/Helper.bsv": _helper_pkg(1)})
    run = tmp_path / "run"
    run.mkdir()
    _compile_package(root / "scratch" / "Helper.bsv", run)
    design = Design(name="p", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    with pytest.raises(NonZeroExitCode) as error:
        _run_directly(Bsc, design, run)
    assert "S0000" in _failure_text(run, error.value)

    accum = _accum_design(tmp_path / "e")
    run = tmp_path / "run2"
    run.mkdir()
    (run / "mkAccum.ba").write_text("not a bsc file\n")
    (run / "Accum.bo").write_text("not a bsc file\n")
    flow = _run_directly(Bsc, accum, run)
    assert "mkAccum" in flow.results["modules"]


def _search_flow(tmp_path: Path, files: dict[str, str], search: list[str], **fields: Any) -> Any:
    root = tmp_path / "d"
    _write(root, files)
    design = Design(name="s", root=root, **fields)
    return _flow(Bsc, design, tmp_path / "run", search_paths=search)


def _search_names(flow: Any, index: int = 0) -> list[str]:
    folder = flow.run_path / "bsc_search" / str(index)
    return sorted(p.name for p in folder.iterdir())


def test_a_search_folder_is_staged_as_links_less_what_bsc_must_not_read(tmp_path, caplog):
    stub = "package X; endpackage\n"
    flow = _search_flow(
        tmp_path,
        {
            "Top.bsv": "package Top; endpackage\n",
            "lib/Pure.bsv": stub,
            "lib/Pure.bo": "",  # a compiled file beside its source
            "lib/Old.bs": stub,
            "lib/Old.bo": "",
            "lib/Lone.bo": "",  # no source beside it: stays
            "lib/Top.bsv": stub,  # defines a listed package
            "lib/Top.bo": "",
            "lib/sub/x.bsvi": "\n",
            "lib/.git/config": "",
            "lib/notes.txt": "",
        },
        ["lib"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    with caplog.at_level(logging.INFO):
        flow._stage(tb=False)
    assert _search_names(flow) == ["Lone.bo", "Old.bs", "Pure.bsv", "notes.txt", "sub"]
    link = flow.run_path / "bsc_search" / "0" / "sub"
    assert link.is_symlink() and link.resolve() == tmp_path / "d" / "lib" / "sub"
    assert os.path.isabs(os.readlink(link))
    (message,) = [r.getMessage() for r in caplog.records if "does not see" in r.getMessage()]
    for name in ("Pure.bo", "Old.bo", "Top.bsv", "Top.bo", ".git"):
        assert name in message
    assert "Lone.bo" not in message


def test_the_stage_of_the_design_root_leaves_out_the_run_root(tmp_path):
    """`search_paths: [.]` with the run root inside the design root: the stage never links the
    run root, or any folder that holds the run directory, into itself."""
    root = tmp_path / "d"
    _write(root, {"Top.bsv": "package Top; endpackage\n", "Other.bsv": "package O; endpackage\n"})
    design = Design(name="s", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    run = root / "xeda_run" / "s" / "bsc"
    run.mkdir(parents=True)
    flow = Bsc(
        {"search_paths": ["."]},
        design,
        run,
        run_directory=RunDirectory.claimed(run, root / "xeda_run"),
    )
    flow._stage(tb=False)
    assert _search_names(flow) == ["Other.bsv"]


def test_a_stale_search_stage_is_replaced(tmp_path):
    flow = _search_flow(
        tmp_path,
        {
            "Top.bsv": "package Top; endpackage\n",
            "lib/A.bsv": "package A; endpackage\n",
            "lib2/B.bsv": "package B; endpackage\n",
        },
        ["lib", "lib2"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    flow._stage(tb=False)
    (flow.run_path / "bsc_search" / "1" / "stale").symlink_to(tmp_path)
    (flow.run_path / "bsc_search" / "7").mkdir()
    flow._stage(tb=False)
    assert sorted(p.name for p in (flow.run_path / "bsc_search").iterdir()) == ["0", "1"]
    assert _search_names(flow, 1) == ["B.bsv"]


def test_a_search_path_that_is_no_folder_fails_the_run(tmp_path):
    """A `search_paths` entry that names no folder (a typo, for one) fails the run and names
    the entry, rather than leaving bsc a search folder that holds nothing."""
    flow = _search_flow(
        tmp_path,
        {"Top.bsv": "package Top; endpackage\n", "lib/A.bsv": "package A; endpackage\n"},
        ["lib", "libz"],
        rtl={"sources": ["Top.bsv"], "top": "mkTop"},
    )
    with pytest.raises(FlowSettingsException, match="`search_paths` names 'libz'"):
        flow._stage(tb=False)


def _mkTop_verilog(flow: Any) -> str:
    return (flow.run_path / "gen_rtl" / "mkTop.v").read_text()


def test_a_stray_compiled_package_in_a_search_folder_is_not_used(tmp_path):
    """Helper is only functions, so it has no `.bo` of its own. A foreign `Helper.bo` that is
    newer than the unlisted `Helper.bsv` beside it won before (bsc takes the newer one)."""
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"lib/Helper.bsv": _helper_pkg(1)})
    _compile_package(root / "lib" / "Helper.bsv", root / "lib")  # a stray `Helper.bo`, body 1
    _write(root, {"lib/Helper.bsv": _helper_pkg(3)})
    old = (root / "lib" / "Helper.bo").stat().st_mtime - 100
    os.utime(root / "lib" / "Helper.bsv", (old, old))
    design = Design(name="g", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    flow = _run_directly(Bsc, design, tmp_path / "run", search_paths=["lib"])
    assert "8'd13" in _mkTop_verilog(flow) and "8'd11" not in _mkTop_verilog(flow)


def test_a_listed_source_in_a_search_folder_compiles_once(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"Helper.bsv": _helper_pkg(1)})
    design = Design(name="g", root=root, rtl={"sources": ["Helper.bsv", "Top.bsv"], "top": "mkTop"})
    flow = _run_directly(Bsc, design, tmp_path / "run", search_paths=["."])
    assert "8'd11" in _mkTop_verilog(flow)
    logs = [p.read_text() for p in flow.run_path.rglob("*.log")]
    assert logs and not any("S0089" in text for text in logs)
    # The search folder holds no second copy of a listed package.
    assert _search_names(flow) == []


def test_a_listed_bh_source_wins_over_an_unlisted_bsv_package_in_a_search_folder(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(
        root,
        {
            "bh/Helper.bs": "package Helper where\nbump :: Bit 8 -> Bit 8\nbump x = x + 5\n",
            "lib/Helper.bsv": _helper_pkg(1),
        },
    )
    design = Design(
        name="g", root=root, rtl={"sources": ["bh/Helper.bs", "Top.bsv"], "top": "mkTop"}
    )
    flow = _run_directly(Bsc, design, tmp_path / "run", search_paths=["lib"])
    assert "8'd15" in _mkTop_verilog(flow) and "8'd11" not in _mkTop_verilog(flow)


def test_an_include_with_a_folder_part_resolves_from_a_search_folder(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _write(
        root,
        {
            "Top.bsv": """package Top;
`include "sub/k.bsvi"
interface Top_IFC;
  method Bit#(8) out;
endinterface
(* synthesize *)
module mkTop(Top_IFC);
  method out = `K;
endmodule
endpackage
""",
            "lib/sub/k.bsvi": "`define K 8'd42\n",
        },
    )
    design = Design(name="i", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    flow = _run_directly(Bsc, design, tmp_path / "run", search_paths=["lib"])
    assert "8'd42" in _mkTop_verilog(flow)


def test_the_first_search_folder_that_defines_a_package_wins(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"a/Helper.bsv": _helper_pkg(1), "b/Helper.bsv": _helper_pkg(2)})
    design = Design(name="g", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    flow = _run_directly(Bsc, design, tmp_path / "run", search_paths=["a", "b"])
    assert "8'd11" in _mkTop_verilog(flow) and "8'd12" not in _mkTop_verilog(flow)


def _dispositions(runner: DefaultRunner, design: Design, settings: dict, times: int) -> list[str]:
    found = []
    for _ in range(times):
        result = launch_result(
            runner, Spec(Bsc.name, design), lambda: runner.run_flow(Bsc, design, settings)
        )
        assert result.outcome == "passed"
        found.append(result.nodes[Bsc.name].disposition)
    return found


def test_a_stray_compiled_package_added_to_a_search_folder_runs_the_flow_again(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"lib/Helper.bsv": _helper_pkg(1)})
    design = Design(name="g", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    settings = {"search_paths": ["lib"]}
    assert _dispositions(runner, design, settings, 2) == ["ran", "reused"]
    _write(tmp_path / "scratch", {"Helper.bsv": _helper_pkg(7)})
    _compile_package(tmp_path / "scratch" / "Helper.bsv", root / "lib")  # a stray, newer `.bo`
    assert _dispositions(runner, design, settings, 1) == ["ran"]
    (run,) = (tmp_path / "run").rglob("mkTop.v")
    assert "8'd11" in run.read_text() and "8'd17" not in run.read_text()


def test_the_design_root_as_a_search_folder_with_the_run_root_inside_it(tmp_path):
    require_bsc()
    root = tmp_path / "d"
    _helper_top(root)
    _write(root, {"Helper.bsv": _helper_pkg(1)})
    design = Design(name="g", root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"})
    runner = DefaultRunner(root / "xeda_run", display_results=False)
    assert _dispositions(runner, design, {"search_paths": ["."]}, 2) == ["ran", "reused"]


def test_an_old_package_and_an_old_module_are_gone_before_the_compile(tmp_path):
    require_bsc()
    design = _accum_design(tmp_path / "d")
    run = tmp_path / "run"
    run.mkdir()
    _write(
        run,
        {"bobjs/Old.bo": "", "bobjs/mkOld.ba": "", "gen_rtl/mkBare.v": "module x(); endmodule\n"},
    )
    flow = _run_directly(Bsc, design, run)
    assert not (run / "bobjs" / "Old.bo").exists() and not (run / "bobjs" / "mkOld.ba").exists()
    assert not (run / "gen_rtl" / "mkBare.v").exists()
    assert "mkBare" not in flow.results["modules"]


def test_an_unlisted_verilog_module_beside_a_listed_one_fails_the_simulator_link(tmp_path, capfd):
    """An imported `add3.v` that is not listed is no longer found beside another listed Verilog
    source, as bsc's `-vsearch` held its folder before."""
    _require_simulator("iverilog")
    _write(tmp_path, {"hw/add3.v": OPS_V, "hw/other.v": "module other(); endmodule\n"})
    _write(tmp_path, {"Ops.bsv": OPS_PKG})
    design = Design(
        name="ops",
        root=tmp_path,
        rtl={"sources": ["hw/other.v"]},
        tb={"sources": ["Ops.bsv"], "top": "mkOpsTb"},
    )
    flow = _run(BscSim, design, tmp_path / "run", simulator="iverilog")
    assert flow is not None and not flow.succeeded


def test_a_second_run_removes_a_stale_link(tmp_path):
    require_bsc()
    design = _accum_design(tmp_path / "d")
    run = tmp_path / "run"
    first = _run_directly(Bsc, design, run)
    (first.run_path / "bsc_sources" / "Gone.bsv").symlink_to(tmp_path / "nowhere")
    (first.run_path / "bsc_verilog" / "gone.v").symlink_to(tmp_path / "nowhere")
    _run_directly(Bsc, design, run)
    assert sorted(p.name for p in (run / "bsc_sources").iterdir()) == ["Accum.bsv", "Top.bsv"]
    assert list((run / "bsc_verilog").iterdir()) == []


def test_the_path_flags_name_no_source_folder_and_no_current_directory(tmp_path):
    design = _accum_design(tmp_path / "d", rtl={"sources": ["lib/Accum.bsv", "rtl/Top.bsv"]})
    flow = _flow(Bsc, design, tmp_path / "run", search_paths=["libs"])
    flags = flow._path_flags("verilog", tmp_path / "run" / "gen_rtl", design.rtl.sources)
    assert flags[flags.index("-p") + 1] == "bsc_sources:bsc_search/0:%/Libraries"
    assert flags[flags.index("-vsearch") + 1] == "bsc_verilog:%/Libraries:%/Verilog"
    assert flags[flags.index("-bdir") + 1] == "bobjs"
    assert flags[flags.index("-vdir") + 1] == "gen_rtl"
    for flag in ("-p", "-vsearch"):
        entries = flags[flags.index(flag) + 1].split(":")
        assert "+" not in entries and "." not in entries


def test_the_stage_holds_exactly_the_listed_files_of_the_parts_the_flow_reads(tmp_path):
    root = tmp_path / "d"
    _write(
        root,
        {
            "Top.bsv": "package Top; endpackage\n",
            "Tb.bsv": "package Tb; endpackage\n",
            "Limit.bsvi": "\n",
            "vlib/m.v": "module m(); endmodule\n",
            "vlib/unlisted.v": "module u(); endmodule\n",
            "other/Unlisted.bsv": "package Unlisted; endpackage\n",
        },
    )
    design = Design(
        name="s",
        root=root,
        rtl={"sources": ["Limit.bsvi", "Top.bsv", "Top.bsv", "vlib/m.v"], "top": "mkTop"},
        tb={"sources": ["Tb.bsv"], "top": "mkTb"},
    )
    rtl_only = _flow(Bsc, design, tmp_path / "run1")._stage_entries(tb=False)
    assert {str(link) for link in rtl_only} == {
        "bsc_sources/Limit.bsvi",
        "bsc_sources/Top.bsv",
        "bsc_verilog/m.v",
    }
    both = _flow(BscSim, design, tmp_path / "run2")._stage_entries(tb=True)
    assert {str(link) for link in both} == {*map(str, rtl_only), "bsc_sources/Tb.bsv"}
    assert rtl_only[Path("bsc_sources/Top.bsv")] == root / "Top.bsv"
    assert bsc_module.staged_name(design.rtl.sources[1]) == Path("bsc_sources/Top.bsv")


def test_two_listed_files_of_one_name_cannot_share_a_stage_folder(tmp_path):
    root = tmp_path / "d"
    _write(root, {"a/P.bsv": "package P; endpackage\n", "b/P.bsv": "package P; endpackage\n"})
    design = Design(name="s", root=root, rtl={"sources": ["a/P.bsv", "b/P.bsv"], "top": "mkP"})
    with pytest.raises(FlowSettingsException, match="two listed sources have the file name P.bsv"):
        _flow(Bsc, design, tmp_path / "run")._stage(tb=False)


def test_a_run_root_with_whitespace_fails_with_a_clean_message_or_runs(tmp_path):
    """`bsc` has no link step, so a run directory with whitespace works. `bsc_sim` refuses it
    before it runs (`check_run_directory`), as its link step needs whitespace-free paths."""
    require_bsc()
    design = _accum_design(tmp_path / "d")
    flow = _run_directly(Bsc, design, tmp_path / "a run root" / "run")
    assert "mkTop" in flow.results["modules"]
    with pytest.raises(FlowSettingsException, match="whitespace"):
        BscSim.check_run_directory(BscSim.Settings(), tmp_path / "a run root" / "run")


# ---------------------------------------------------------------------------------------------
# installed_libraries, and the warning about a missing search folder
# ---------------------------------------------------------------------------------------------


def test_installed_libraries_come_after_the_search_paths_and_before_bsc_libraries(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(
        Bsc,
        design,
        tmp_path / "run",
        search_paths=["libs", "more"],
        installed_libraries=["FPGA/Xilinx", "TLM3"],
    )
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert _pairs(flags, "-p") == [
        "bsc_sources:bsc_search/0:bsc_search/1:%/Libraries/FPGA/Xilinx:%/Libraries/TLM3"
        ":%/Libraries"
    ]
    sim = flow._path_flags("sim", tmp_path / "out", design.rtl.sources)
    assert _pairs(sim, "-p") == _pairs(flags, "-p")


def test_no_installed_library_leaves_the_path_as_it_was(tmp_path):
    design = _accum_design(tmp_path / "d")
    flow = _flow(Bsc, design, tmp_path / "run")
    flags = flow._path_flags("verilog", tmp_path / "out", design.rtl.sources)
    assert _pairs(flags, "-p") == ["bsc_sources:%/Libraries"]


@pytest.mark.parametrize(
    "entry, message",
    [
        ("", "is empty"),
        ("/opt/lib", "is absolute"),
        ("../Other", 'empty, "." or ".." component'),
        ("FPGA/../../x", 'empty, "." or ".." component'),
        ("FPGA//Xilinx", 'empty, "." or ".." component'),
        ("FPGA/", 'empty, "." or ".." component'),
        (".", 'empty, "." or ".." component'),
        ("A:B", 'has a ":"'),
        ("%/Libraries/A", 'has a "%"'),
        ("FPGA Xilinx", "has whitespace"),
        ("A\tB", "has whitespace"),
    ],
)
def test_an_installed_library_must_be_a_folder_below_the_libraries_folder(entry, message):
    with pytest.raises(FlowSettingsError, match=rf"installed_libraries entry .* {message}"):
        Bsc.Settings.from_input({"installed_libraries": [entry]})


def test_a_nested_installed_library_is_accepted_and_stays_as_given():
    ss = Bsc.Settings(installed_libraries=["FPGA/Xilinx", "TLM3"])
    assert ss.installed_libraries == ["FPGA/Xilinx", "TLM3"]
    assert Bsc.Settings(**ss.model_dump()).installed_libraries == ss.installed_libraries


def test_installed_libraries_are_part_of_the_identity():
    assert Bsc.Settings().model_dump() != Bsc.Settings(installed_libraries=["TLM3"]).model_dump()


@pytest.mark.parametrize(
    "settings, expected",
    [
        ({}, [("-promote-warnings", "S0091")]),
        (
            {"promote_warnings": ["G0010", "G0117"]},
            [("-promote-warnings", "G0010:G0117"), ("-promote-warnings", "S0091")],
        ),
        # `NONE` clears the tags before it, so S0091 comes after it.
        (
            {"promote_warnings": ["NONE"]},
            [("-promote-warnings", "NONE"), ("-promote-warnings", "S0091")],
        ),
        (
            {"suppress_warnings": ["S0091"], "demote_errors": ["G0004"]},
            [
                ("-promote-warnings", "S0091"),
                ("-suppress-warnings", "S0091"),
                ("-demote-errors", "G0004"),
            ],
        ),
    ],
)
def test_the_message_flags_promote_a_missing_folder_after_the_settings_own(
    settings, expected, tmp_path
):
    flow = _flow(Bsc, _accum_design(tmp_path / "d"), tmp_path / "run", **settings)
    flags = flow._message_flags()
    assert list(zip(flags[::2], flags[1::2])) == expected
    compile_flags = flow._compile_flags("verilog")
    start = compile_flags.index(flags[0])
    assert compile_flags[start : start + len(flags)] == flags


def _folder_check(tmp_path: Path, name: str = "Top.bsv") -> Design:
    _write(tmp_path, {"Top.bsv": "package Top;\nmodule mkTop(Empty); endmodule\nendpackage\n"})
    return Design(name="m", root=tmp_path, rtl={"sources": [name], "top": "mkTop"})


def test_a_missing_installed_library_folder_fails_the_run(tmp_path):
    require_bsc()
    design = _folder_check(tmp_path)
    with pytest.raises(NonZeroExitCode) as error:
        _run_directly(Bsc, design, tmp_path / "run", installed_libraries=["No/Such/Folder"])
    text = _failure_text(tmp_path / "run", error.value)
    assert "S0091" in text and "No/Such/Folder" in text


def test_a_missing_verilog_search_folder_fails_the_run(tmp_path):
    require_bsc()
    design = _folder_check(tmp_path)
    with pytest.raises(NonZeroExitCode) as error:
        _run_directly(Bsc, design, tmp_path / "run", verilog_search_paths=["nowhere"])
    assert "S0091" in _failure_text(tmp_path / "run", error.value)


def test_a_run_without_a_missing_folder_passes_with_the_promotion(tmp_path):
    require_bsc()
    design = _folder_check(tmp_path)
    flow = _run_directly(Bsc, design, tmp_path / "run", promote_warnings=["NONE"])
    assert (flow.run_path / "gen_rtl" / "mkTop.v").exists()


def _bsc_outcome(tmp_path: Path, *flags: str) -> tuple[int, str]:
    """Compile a package in a clean copy with `-p` naming a missing folder, plus `flags`."""
    root = tmp_path / "t"
    root.mkdir(exist_ok=True)
    for name in ("b", "o"):
        (root / name).mkdir(exist_ok=True)
    (root / "T.bsv").write_text("package T;\nmodule mkT(Empty); endmodule\nendpackage\n")
    proc = subprocess.run(
        ["bsc", "-u", "-verilog", "-bdir", "b", "-vdir", "o", "-p", ".:%/Libraries/No:%/Libraries"]
        + list(flags)
        + ["T.bsv"],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    shutil.rmtree(root)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "flags, fails",
    [
        (["-promote-warnings", "S0091"], True),
        (["-promote-warnings", "G0010", "-promote-warnings", "S0091"], True),
        (["-promote-warnings", "S0091", "-promote-warnings", "G0010"], True),
        (["-promote-warnings", "ALL", "-promote-warnings", "S0091"], True),
        (["-promote-warnings", "NONE", "-promote-warnings", "S0091"], True),
        # `NONE` after the tag clears it: the order is what the flow relies on.
        (["-promote-warnings", "S0091", "-promote-warnings", "NONE"], False),
        # a suppressed or demoted S0091 stays a warning (or is not reported), in any order
        (["-promote-warnings", "S0091", "-suppress-warnings", "S0091"], False),
        (["-suppress-warnings", "S0091", "-promote-warnings", "S0091"], False),
        (["-promote-warnings", "S0091", "-suppress-warnings", "ALL"], False),
        (["-promote-warnings", "S0091", "-demote-errors", "S0091"], False),
        (["-demote-errors", "S0091", "-promote-warnings", "S0091"], False),
    ],
)
def test_bsc_combines_promote_warnings_flags_as_the_flow_assumes(flags, fails, tmp_path):
    require_bsc()
    code, text = _bsc_outcome(tmp_path, *flags)
    assert (code != 0) == fails, text
