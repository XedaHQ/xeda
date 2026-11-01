"""The xezim flow: its evidence, read from native transcripts, and the behavior of the real tool."""

import json
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flows import Xezim
from xeda.flows.xezim import XezimTool, parse_xezim_log
from xeda.units import Time

from .tool_utils import HANG_GUARD, require_cocotb, require_xezim

BANNER = "=== xezim 0.11.4 ===\ngit e826641 (2026-10-31T02:47:36-07:00)\n"
TIMESCALES = (
    "=== module timescales (2 modules) ===\n"
    "  sub                          10ns / 1ns\n"
    "  tb                           1ns / 100ps   (a note)\n"
    "======================================\n"
)
FINISHED = "Simulation finished at time 15 ($finish called)\n"
TIMESCALE = "`timescale 1ns/1ps\n"


def _evidence(tmp_path: Path, text: str, stop_time: Optional[str] = None):
    """Read `text` as the transcript of a run with `stop_time`."""
    transcript = tmp_path / "sim.log"
    transcript.write_text(text, encoding="utf-8")
    flow: Any = SimpleNamespace(
        report_file=lambda path: transcript,
        settings=SimpleNamespace(stop_time=None if stop_time is None else Time(stop_time)),
    )
    return parse_xezim_log(flow)


def test_the_tick_is_the_finest_precision_of_the_design(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, BANNER + TIMESCALES + FINISHED)
    assert (evidence.ended_by, evidence.time, evidence.time_unit) == ("finish", 15, "100ps")


def test_a_transcript_without_timescales_has_no_time_unit(tmp_path: Path) -> None:
    assert _evidence(tmp_path, FINISHED).time_unit is None


@pytest.mark.parametrize("text", ["", BANNER, BANNER + "Simulation error: no top\n"])
def test_a_transcript_without_an_end_line_is_no_evidence(tmp_path: Path, text: str) -> None:
    assert _evidence(tmp_path, text) is None


def test_an_empty_event_queue_is_no_finish(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, BANNER + TIMESCALES + "Simulation finished at time 15\n")
    assert evidence.ended_by == "drained"


def test_the_output_of_a_design_cannot_stand_in_for_the_end_line(tmp_path: Path) -> None:
    """The native line comes after every `final` block, so the last line decides."""
    spoofed = (
        FINISHED + "Simulation finished at time 15 ($finish called)"
        "Simulation finished at time 15\n"
    )
    assert _evidence(tmp_path, BANNER + spoofed).ended_by == "drained"


def test_output_without_a_newline_cannot_hide_the_end_line(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "no newline" + FINISHED)
    assert (evidence.ended_by, evidence.time) == ("finish", 15)


def test_diagnostics_become_events(tmp_path: Path) -> None:
    diagnostics = (
        "** Error: oops\n   Time: 5  Scope: tb.u\n"
        "** Warning\n   Time: 6  Scope: tb\n"
        "** Info: a note\n   Time: 6  Scope: tb\n"
    )
    events = _evidence(tmp_path, TIMESCALES + diagnostics + FINISHED).events
    assert [(event.kind, event.time, event.message) for event in events] == [
        ("error", 5, "oops (in tb.u)"),
        ("warning", 6, "(in tb)"),
        ("finish", 15, ""),
    ]


def test_output_without_a_newline_cannot_hide_a_diagnostic(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "text** Error: oops\n   Time: 5  Scope: tb\n" + FINISHED)
    assert [event.kind for event in evidence.events] == ["error", "finish"]


def test_a_diagnostic_needs_no_context_line(tmp_path: Path) -> None:
    """A timing violation has none, and a run with one still exits with status 0."""
    violation = (
        "** Error: $setup( d:10 ns, posedge ck:11 ns, 5 ns ) violation in t.u at time 11 ns\n"
    )
    evidence = _evidence(tmp_path, violation + FINISHED)
    assert [event.kind for event in evidence.events] == ["error", "finish"]
    assert evidence.events[0].time is None
    assert "violation in t.u" in evidence.events[0].message


def test_an_error_of_the_tool_is_an_error(tmp_path: Path) -> None:
    """xezim counts these as errors too, and a run with one exits with status 0."""
    text = "[xezim][error] $readmemh: 'a' does not name an unpacked memory (t=0)\n" + FINISHED
    event = _evidence(tmp_path, text).events[0]
    assert (event.kind, event.time) == ("error", None)
    assert "does not name an unpacked memory" in event.message


def test_text_of_a_testbench_that_only_says_error_is_no_event(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "Error: 0 mismatches\nERROR count = 0\n" + FINISHED)
    assert [event.kind for event in evidence.events] == ["finish"]


def test_a_fatal_event_decides_the_end(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "** Fatal: boom\n   Time: 5  Scope: tb\n" + FINISHED)
    assert evidence.ended_by == "fatal"


def test_a_signal_is_no_finish(tmp_path: Path) -> None:
    """xezim exits with status 0 and claims `$finish` when a signal stops it."""
    interrupted = "[xezim] interrupted at time 15 - finalizing waveform dumps\n"
    evidence = _evidence(tmp_path, interrupted + FINISHED)
    assert evidence.ended_by == "error"
    assert [event.kind for event in evidence.events] == ["error"]


def test_a_requested_stop_time_is_reached_at_exactly_that_time(tmp_path: Path) -> None:
    text = TIMESCALES + "Simulation finished at time 20\n"
    evidence = _evidence(tmp_path, text, stop_time="2 ns")
    assert (evidence.ended_by, evidence.time, evidence.time_unit) == ("stop_time", 20, "100ps")


def test_an_end_before_the_stop_time_drained(tmp_path: Path) -> None:
    text = TIMESCALES + "Simulation finished at time 10\n"
    assert _evidence(tmp_path, text, stop_time="2 ns").ended_by == "drained"


def test_a_stop_time_without_a_tick_stays_unconfirmed(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, "Simulation finished at time 20\n", stop_time="2 ns")
    assert (evidence.ended_by, evidence.time_unit) == ("stop_time", None)


def test_a_limit_that_nobody_asked_for_is_a_stop_time(tmp_path: Path) -> None:
    """`judge_evidence` fails a stop time without a `stop_time` setting."""
    text = (
        "[xezim][hang-report] simulation reached --max-time (9 ticks) without $finish\n"
        "Simulation finished at time 5\n"
    )
    assert _evidence(tmp_path, text).ended_by == "stop_time"


def _settings(**values) -> Xezim.Settings:
    return Xezim.Settings(**values)


@pytest.mark.parametrize("stop_time", ["1500 ps", "1 fs", "0 ns"])
def test_a_stop_time_that_is_no_positive_whole_nanosecond_is_refused(stop_time: str) -> None:
    with pytest.raises(FlowSettingsException, match="whole number of nanoseconds"):
        Xezim.check_settings_supported(_settings(stop_time=stop_time))


@pytest.mark.parametrize(
    "flag, setting",
    [
        ("-do", "`stop_time`"),
        ("--max-time=5ns", "`stop_time`"),
        ("--error-exit", "`fail_severity`"),
        ("+seed=3", "`random_seed`"),
        ("-l", "the log of the run"),
    ],
)
def test_an_option_of_the_flow_is_refused_in_the_flags(flag: str, setting: str) -> None:
    with pytest.raises(FlowSettingsException, match=setting):
        Xezim.check_settings_supported(_settings(xezim_flags=[flag]))


def test_other_flags_pass() -> None:
    Xezim.check_settings_supported(_settings(xezim_flags=["--relax-implicit-static", "+nospecify"]))


@pytest.mark.parametrize(
    "values, refined",
    [
        ({"sdf_type": "max"}, "sdf_file"),
        ({"x_warn_limit": 5}, "x_warn"),
        ({"code_coverage_scope": ["tb.dut"]}, "code_coverage"),
    ],
)
def test_a_refinement_needs_what_it_refines(values: Dict[str, Any], refined: str) -> None:
    with pytest.raises(FlowSettingsException, match=f"set `{refined}`"):
        Xezim.check_settings_supported(_settings(**values))


def _design(root: Path, top: str = "tb", **values: Any) -> Design:
    """A design whose testbench is the file `tb.sv`, which the caller wrote."""
    tb = {"sources": ["tb.sv"], "top": top}
    return Design(name="d", root=root, tb=tb, rtl={"sources": [], "top": None}, **values)


@pytest.mark.parametrize("standard", ["2005", "09"])
def test_a_standard_that_xezim_does_not_parse_is_refused(tmp_path: Path, standard: str) -> None:
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    design = _design(tmp_path, language={"verilog": {"standard": standard}})
    with pytest.raises(FlowSettingsException, match="1800-2017 and 1800-2023"):
        Xezim.check_design_supported(design, _settings())


def test_vcd_with_cocotb_is_refused(tmp_path: Path) -> None:
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "t.py").write_text("")
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["t.py"], "cocotb": True},
    )
    with pytest.raises(FlowSettingsException, match="use `fst`"):
        Xezim.check_design_supported(design, _settings(vcd=True))


def test_the_flow_reads_verilog_and_no_vhdl() -> None:
    flow = get_flow_class("xezim")
    assert SourceType.SystemVerilog in flow.reads_sources
    assert SourceType.Vhdl not in flow.reads_sources


def _simulate(tmp_path: Path, testbench: str, design: Optional[Design] = None, **settings: Any):
    """Run `testbench` on the real xezim."""
    require_xezim()
    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / "tb.sv").write_text(TIMESCALE + testbench, encoding="utf-8")
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    flow = runner.run_flow(Xezim, design or _design(root), settings)
    assert flow is not None
    return flow


@pytest.mark.real_tool
def test_a_finish_passes_with_the_time_in_femtoseconds(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; initial #10 $finish; endmodule\n")
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"
    assert flow.results["sim.time"] == 10_000_000


@pytest.mark.real_tool
def test_a_silent_finish_passes(tmp_path: Path) -> None:
    assert _simulate(tmp_path, "module tb; initial #10 $finish(0); endmodule\n").succeeded


@pytest.mark.real_tool
def test_a_stop_passes_because_xezim_reports_it_as_a_finish(tmp_path: Path) -> None:
    """A known limitation, documented in the flow: this test shows when upstream changes it."""
    flow = _simulate(tmp_path, "module tb; initial #10 $stop; endmodule\n")
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"


@pytest.mark.real_tool
def test_an_empty_event_queue_fails(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; initial #10 $display(0); endmodule\n")
    assert not flow.succeeded
    assert flow.results["sim.ended_by"] == "drained"


@pytest.mark.real_tool
def test_an_error_fails_at_the_default_severity(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, 'module tb; initial begin $error("e"); $finish; end endmodule\n')
    assert not flow.succeeded
    assert flow.results["sim.errors"] == 1
    assert "e (in tb)" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_an_error_below_the_severity_passes(tmp_path: Path) -> None:
    testbench = 'module tb; initial begin $error("e"); $finish; end endmodule\n'
    assert _simulate(tmp_path, testbench, fail_severity="failure").succeeded


@pytest.mark.real_tool
def test_a_warning_counts_only_at_its_severity(tmp_path: Path) -> None:
    testbench = 'module tb; initial begin $warning("w"); $finish; end endmodule\n'
    assert _simulate(tmp_path / "a", testbench).succeeded
    assert not _simulate(tmp_path / "b", testbench, fail_severity="warning").succeeded


TIMING = (
    "module ff(input d, input ck); reg n;\n"
    "  specify $setup(d, posedge ck, 5, n); endspecify\n"
    "endmodule\n"
    "module tb; reg d = 0, c = 0; ff u(d, c);\n"
    "  initial begin #10 d = 1; #1 c = 1; #1 $finish; end\n"
    "endmodule\n"
)


@pytest.mark.real_tool
def test_a_timing_violation_fails(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, TIMING)
    assert not flow.succeeded
    assert flow.results["sim.errors"] == 1
    assert "violation in tb.u" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_an_error_of_the_tool_fails(tmp_path: Path) -> None:
    testbench = 'module tb; reg a; initial begin $readmemh("m.hex", a); $finish; end endmodule\n'
    flow = _simulate(tmp_path, testbench)
    assert not flow.succeeded
    assert "does not name an unpacked memory" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_a_failed_assertion_fails(tmp_path: Path) -> None:
    testbench = (
        "module tb; reg c = 0, a = 0; always #5 c = ~c;\n"
        "  assert property (@(posedge c) a);\n"
        "  initial #7 $finish;\n"
        "endmodule\n"
    )
    assert not _simulate(tmp_path, testbench).succeeded


@pytest.mark.real_tool
def test_an_illegal_bin_fails(tmp_path: Path) -> None:
    testbench = (
        "module tb; reg c = 0; reg [1:0] v = 2;\n"
        "  covergroup g @(posedge c); coverpoint v { illegal_bins bad = {2}; } endgroup\n"
        "  g i = new;\n"
        "  initial begin #1 c = 1; #1 $finish; end\n"
        "endmodule\n"
    )
    assert not _simulate(tmp_path, testbench).succeeded


@pytest.mark.real_tool
def test_a_fatal_fails(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, 'module tb; initial $fatal(1, "f"); endmodule\n')
    assert not flow.succeeded


@pytest.mark.real_tool
def test_a_parse_error_fails(tmp_path: Path) -> None:
    assert not _simulate(tmp_path, "module tb; initial begin end\n").succeeded


CLOCK = "module tb; reg c = 0; always #5 c = ~c; endmodule\n"


@pytest.mark.real_tool
def test_a_stop_time_passes_at_exactly_that_time(tmp_path: Path) -> None:
    """The last event of this run is at 5 ns: the end is at the request, not at the last event."""
    flow = _simulate(tmp_path, CLOCK, stop_time="7 ns")
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "stop_time"
    assert flow.results["sim.time"] == 7_000_000


@pytest.mark.real_tool
def test_a_run_goes_past_the_default_limit_of_the_tool(tmp_path: Path) -> None:
    testbench = "module tb; initial #200_000_000 $finish; endmodule\n"
    flow = _simulate(tmp_path, testbench)
    assert flow.succeeded
    assert flow.results["sim.time"] == 200 * 10**12


@pytest.mark.real_tool
def test_a_timeout_fails_although_the_tool_exits_with_status_0(tmp_path: Path) -> None:
    """xeda stops the tool with a signal, which xezim takes as a clean end."""
    flow = _simulate(tmp_path, CLOCK, timeout="1 s")
    assert not flow.succeeded
    assert flow.results["error"]["code"] == "timeout"


@pytest.mark.real_tool
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_the_tool_still_hides_a_signal_in_its_exit_status(tmp_path: Path, number: int) -> None:
    """The reason for the check of the interruption line: exit 0, and a claim of `$finish`."""
    require_xezim()
    source = tmp_path / "tb.sv"
    source.write_text(
        TIMESCALE + "module tb; reg c = 0; always #5 c = ~c;\n"
        '  initial begin #1 $display("ready"); $fflush; end\n'
        "endmodule\n"
    )
    with subprocess.Popen(
        ["xezim", "-do", "run -all", "-s", "tb", str(source)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # `pytest &` in a non-interactive shell starts its children with SIGINT ignored.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    ) as process:
        assert process.stdout is not None
        watchdog = threading.Timer(HANG_GUARD, process.kill)
        watchdog.start()
        # A running testbench proves that the simulation has its handler.
        for line in process.stdout:
            if line.strip() == "ready":
                break
        process.send_signal(number)
        output = process.stdout.read()
        watchdog.cancel()
    assert process.returncode == 0
    assert "interrupted at time" in output
    assert "($finish called)" in output


@pytest.mark.real_tool
def test_parameters_reach_the_top_alone(tmp_path: Path) -> None:
    """`-G` would rewrite the value that the instance gives to `sub`."""
    root = tmp_path / "design"
    root.mkdir()
    testbench = (
        'module sub #(parameter W = 2) (); initial $display("sub W=%0d", W); endmodule\n'
        'module tb #(parameter W = 1, parameter string S = "a");\n'
        "  sub #(.W(9)) u();\n"
        '  initial begin $display("top W=%0d S=%s D=%0d", W, S, `DEPTH); $finish; end\n'
        "endmodule\n"
    )
    (root / "tb.sv").write_text(testbench)
    design = Design(
        name="d",
        root=root,
        rtl={"sources": [], "top": None},
        tb={
            "sources": ["tb.sv"],
            "top": "tb",
            "parameters": {"W": 5, "S": "text"},
            "defines": {"DEPTH": 3},
        },
    )
    flow = _simulate(tmp_path, testbench, design=design)
    assert flow.succeeded
    text = (flow.run_path / "sim.log").read_text()
    assert "top W=5 S=text D=3" in text
    assert "sub W=9" in text


@pytest.mark.real_tool
def test_parameters_reach_every_top(tmp_path: Path) -> None:
    root = tmp_path / "design"
    root.mkdir()
    testbench = (
        'module tb #(parameter W = 1); initial begin $display("tb W=%0d", W); #1 $finish; end\n'
        "endmodule\n"
        'module chk #(parameter W = 2); initial $display("chk W=%0d", W); endmodule\n'
    )
    (root / "tb.sv").write_text(testbench)
    design = Design(
        name="d",
        root=root,
        rtl={"sources": [], "top": None},
        tb={"sources": ["tb.sv"], "top": ["tb", "chk"], "parameters": {"W": 5}},
    )
    flow = _simulate(tmp_path, testbench, design=design)
    assert flow.succeeded
    text = (flow.run_path / "sim.log").read_text()
    assert "tb W=5" in text and "chk W=5" in text


@pytest.mark.real_tool
def test_a_header_is_found_through_its_directory(tmp_path: Path) -> None:
    root = tmp_path / "design"
    (root / "inc").mkdir(parents=True)
    (root / "inc" / "values.svh").write_text("`define ANSWER 42\n")
    testbench = (
        '`include "values.svh"\n'
        'module tb; initial begin $display("%0d", `ANSWER); $finish; end endmodule\n'
    )
    (root / "tb.sv").write_text(testbench)
    design = Design(
        name="d",
        root=root,
        rtl={"sources": [], "top": None},
        tb={"sources": ["tb.sv", "inc/values.svh"], "top": "tb"},
    )
    assert _simulate(tmp_path, testbench, design=design).succeeded


@pytest.mark.real_tool
def test_fst_needs_nothing_from_the_testbench(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; reg a = 0; initial #10 $finish; endmodule\n", fst=True)
    assert flow.succeeded
    assert (flow.run_path / "dump.fst").exists()


DUMPS = 'initial begin $dumpfile("dump.vcd"); $dumpvars(0, tb); #10 $finish; end'


@pytest.mark.real_tool
def test_vcd_comes_from_the_testbench(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, f"module tb; {DUMPS} endmodule\n", vcd=True)
    assert flow.succeeded
    assert (flow.run_path / "dump.vcd").exists()


@pytest.mark.real_tool
def test_vcd_takes_the_name_of_the_setting(tmp_path: Path) -> None:
    testbench = f"module tb; {DUMPS.replace('dump.vcd', 'waves.vcd')} endmodule\n"
    flow = _simulate(tmp_path, testbench, vcd="waves.vcd")
    assert flow.succeeded
    assert (flow.run_path / "waves.vcd").exists()


@pytest.mark.real_tool
def test_vcd_fails_a_testbench_that_writes_another_name(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, f"module tb; {DUMPS} endmodule\n", vcd="waves.vcd")
    assert not flow.succeeded
    assert "wrote no waves.vcd" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_vcd_fails_a_testbench_that_dumps_nothing(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; initial #10 $finish; endmodule\n", vcd=True)
    assert not flow.succeeded
    assert "wrote no dump.vcd" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_fst_reaches_its_location(tmp_path: Path) -> None:
    location = tmp_path / "out" / "w.fst"
    testbench = "module tb; reg a = 0; initial #10 $finish; endmodule\n"
    flow = _simulate(tmp_path, testbench, fst=location)
    assert flow.succeeded
    assert (flow.run_path / "dump.fst").exists()
    assert location.exists()


@pytest.mark.real_tool
def test_fst_fails_a_design_with_no_signal(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; initial #10 $finish; endmodule\n", fst=True)
    assert not flow.succeeded
    assert "no signal to dump" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_coverage_gives_results_and_an_artifact(tmp_path: Path) -> None:
    testbench = (
        "module tb; reg a = 0;\n"
        "  always @* if (a) $display(1); else $display(0);\n"
        "  initial begin #1 a = 1; #1 $finish; end\n"
        "endmodule\n"
    )
    flow = _simulate(tmp_path, testbench, code_coverage=["stmt", "branch"])
    assert flow.succeeded
    assert 0 < flow.results["coverage.statement"] <= 100
    assert 0 < flow.results["coverage.branch"] <= 100
    assert "coverage.toggle" not in flow.results
    assert flow.artifacts["coverage"] == "xezim_cov.json"
    assert json.loads((flow.run_path / "xezim_cov.json").read_text())["code_coverage"]


@pytest.mark.real_tool
def test_coverage_of_nothing_is_no_result_and_no_failure(tmp_path: Path) -> None:
    testbench = "module tb; initial $finish; endmodule\n"
    flow = _simulate(tmp_path, testbench, code_coverage=["branch"])
    assert flow.succeeded
    assert "coverage.branch" not in flow.results


GATE = (
    "module cbuf(input a, output y); assign y = a; specify (a => y) = (8, 9); endspecify endmodule\n"
    "module tb; reg a = 0; wire y; cbuf uut(.a(a), .y(y));\n"
    '  always @(y) $display("y=%b at %0t", y, $time);\n'
    "  initial begin #1 a = 1; #50 $finish; end\n"
    "endmodule\n"
)


def _sdf(path: Path, instance: str) -> Path:
    path.write_text(
        f'(DELAYFILE (SDFVERSION "3.0") (DESIGN "tb") (TIMESCALE 1ns)\n'
        f' (CELL (CELLTYPE "cbuf") (INSTANCE {instance})\n'
        "  (DELAY (ABSOLUTE (IOPATH a y (10:20:30) (10:20:30))))))\n"
    )
    return path


@pytest.mark.real_tool
@pytest.mark.parametrize("delay, arrival", [("min", 11), ("typ", 21), ("max", 31)])
def test_sdf_delays_apply(tmp_path: Path, delay: str, arrival: int) -> None:
    sdf = _sdf(tmp_path / "d.sdf", "uut")
    flow = _simulate(tmp_path, GATE, sdf_file=sdf, sdf_type=delay)
    assert flow.succeeded
    assert f"y=1 at {arrival * 1000}" in (flow.run_path / "sim.log").read_text()


@pytest.mark.real_tool
def test_an_edited_sdf_file_makes_the_run_stale(tmp_path: Path) -> None:
    sdf = _sdf(tmp_path / "d.sdf", "uut")
    first = _simulate(tmp_path, GATE, sdf_file=sdf)
    assert "y=1 at 21000" in (first.run_path / "sim.log").read_text()
    sdf.write_text(sdf.read_text().replace("10:20:30", "10:40:30"))
    second = _simulate(tmp_path, GATE, sdf_file=sdf)
    assert "y=1 at 41000" in (second.run_path / "sim.log").read_text()


@pytest.mark.real_tool
def test_sdf_that_annotates_nothing_is_rejected(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, GATE, sdf_file=_sdf(tmp_path / "d.sdf", "elsewhere"))
    assert not flow.succeeded
    assert "annotated no signal" in flow.results["error"]["message"]


@pytest.mark.real_tool
def test_x_warnings_are_in_the_log(tmp_path: Path) -> None:
    testbench = "module tb; reg b = 0; initial begin #1 b = 1'bx; #1 $finish; end endmodule\n"
    flow = _simulate(tmp_path, testbench, x_warn=True, x_warn_limit=1)
    assert flow.succeeded
    assert "X on 'b'" in (flow.run_path / "sim.log").read_text()


@pytest.mark.real_tool
def test_the_seed_gives_the_same_run(tmp_path: Path) -> None:
    testbench = 'module tb; initial begin $display("r=%0d", $random); $finish; end endmodule\n'
    runs = [_simulate(tmp_path / name, testbench, random_seed=7) for name in ("a", "b")]
    logs = [flow.run_path / "sim.log" for flow in runs]
    assert logs[0].read_text().count("r=") == 1
    line = [text for text in logs[0].read_text().splitlines() if text.startswith("r=")]
    assert line and line[0] in logs[1].read_text()


@pytest.mark.real_tool
def test_a_random_seed_reaches_the_tool(tmp_path: Path) -> None:
    flow = _simulate(tmp_path, "module tb; initial $finish; endmodule\n", random_seed="random")
    assert flow.succeeded
    assert "random seed:" in (flow.run_path / "sim.log").read_text()


def test_a_random_seed_always_runs(tmp_path: Path) -> None:
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    flow = Xezim({"random_seed": "random"}, _design(tmp_path), tmp_path / "run")
    assert flow.always_runs() == "it draws a new random seed"


@pytest.mark.real_tool
def test_a_standard_reaches_the_tool(tmp_path: Path, monkeypatch) -> None:
    arguments = []
    run = XezimTool.run

    def recording_run(self, *args, **kwargs):
        arguments.extend(args)
        return run(self, *args, **kwargs)

    monkeypatch.setattr(XezimTool, "run", recording_run)
    root = tmp_path / "design"
    root.mkdir()
    (root / "tb.sv").write_text("")
    design = _design(root, language={"verilog": {"standard": "2017"}})
    flow = _simulate(tmp_path, "module tb; initial $finish; endmodule\n", design=design)
    assert flow.succeeded
    assert "--sv2017" in arguments


@pytest.mark.real_tool
def test_cocotb_runs_on_its_stock_library(tmp_path: Path) -> None:
    require_cocotb()
    require_xezim()
    root = tmp_path / "design"
    root.mkdir()
    (root / "dut.sv").write_text(
        TIMESCALE + "module dut(input logic clk, input logic [7:0] a, output logic [7:0] q);\n"
        "  always_ff @(posedge clk) q <= a + 1;\n"
        "endmodule\n"
    )
    (root / "test_dut.py").write_text(
        "import cocotb\n"
        "from cocotb.clock import Clock\n"
        "from cocotb.triggers import ReadOnly, RisingEdge\n\n\n"
        "@cocotb.test()\n"
        "async def increments(dut):\n"
        '    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())\n'
        "    dut.a.value = 5\n"
        "    await RisingEdge(dut.clk)\n"
        "    await RisingEdge(dut.clk)\n"
        "    await ReadOnly()\n"
        "    assert dut.q.value == 6\n"
    )
    design = Design(
        name="d",
        root=root,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["test_dut.py"], "cocotb": True},
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    flow = runner.run_flow(Xezim, design, {"fst": True})
    assert flow is not None and flow.succeeded
    assert flow.results["cocotb.tests"] == 1
    assert (flow.run_path / "dump.fst").exists()
