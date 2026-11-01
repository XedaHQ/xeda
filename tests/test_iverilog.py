"""The `iverilog` flow on a real Icarus Verilog.

The table of outcomes is the specification of the end evidence: how the simulation ended decides
the verdict, never the exit code alone.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowFatalError, FlowSettingsException
from xeda.flow_runner import DefaultRunner

from .test_cocotb import _TEST_HEADER, _cocotb_test
from .tool_utils import cocotb_safe_vvp, require_cocotb, require_iverilog_evidence  # noqa: F401

CLOCK = "reg clk = 0; always #1 clk = ~clk;"
DUMP = '$dumpfile("dump.vcd"); $dumpvars(0, tb);'


def run_tb(
    root: Path,
    body: str,
    *,
    decls: str = "",
    settings: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    rtl: dict[str, Any] | None = None,
    tb: dict[str, Any] | None = None,
    language: dict[str, Any] | None = None,
    prefix: str = "",
    extra_modules: str = "",
):
    """Run `tb.sv`, a module whose initial block holds `body`, and return the flow."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "tb.sv").write_text(
        f"{prefix}module tb;\n{decls}\ninitial begin\n{body}\nend\nendmodule\n{extra_modules}"
    )
    for name, text in (files or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    design = Design(
        name="icarus",
        root=root,
        rtl={"sources": ["tb.sv"], "top": "tb", **(rtl or {})},
        tb={"top": "tb", **(tb or {})},
        **({"language": language} if language else {}),
    )
    runner = DefaultRunner(root.parent / "runs", display_results=False)
    flow = runner.run_flow("iverilog", design, {"timeout": "10 s", **(settings or {})})
    assert flow is not None
    return flow


def log_of(flow) -> str:
    return (flow.run_path / "sim.log").read_text()


def error_code(flow) -> str | None:
    return (json.loads((flow.run_path / "results.json").read_text()).get("error") or {}).get("code")


@pytest.mark.parametrize(
    "body,clock,settings,passes,ended,time,code",
    [
        ("#5 $finish;", False, {}, True, "finish", 5_000_000, None),
        ("#5 $finish(0);", False, {}, True, "finish", 5_000_000, None),
        ("#5;", False, {}, False, "unknown", 5_000_000, "rejected"),
        ("#5 $stop;", False, {}, False, "unknown", 5_000_000, "rejected"),
        ("#5 $fatal;", False, {}, False, "fatal", 5_000_000, "nonzero_exit"),
        ('#5 $error("E"); $finish(0);', False, {}, False, "finish", 5_000_000, "rejected"),
        ("#5 assert(0); $finish(0);", False, {}, False, "finish", 5_000_000, "rejected"),
        (
            '#5 $error("E"); $finish(0);',
            False,
            {"fail_severity": "failure"},
            True,
            "finish",
            5_000_000,
            None,
        ),
        ('#5 $warning("W"); $finish(0);', False, {}, True, "finish", 5_000_000, None),
        (
            '#5 $warning("W"); $finish(0);',
            False,
            {"fail_severity": "warning"},
            False,
            "finish",
            5_000_000,
            "rejected",
        ),
        ("", True, {"stop_time": "42 ns"}, True, "stop_time", 42_000_000, None),
        ("", True, {"stop_time": "0 ns"}, True, "stop_time", 0, None),
        ("#5 $finish;", False, {"stop_time": "42 ns"}, True, "finish", 5_000_000, None),
    ],
    ids=[
        "finish",
        "finish0",
        "drain",
        "stop",
        "fatal",
        "error",
        "assert",
        "error-at-failure",
        "warning",
        "warning-threshold",
        "stop-time",
        "stop-time-0",
        "finish-before-stop-time",
    ],
)
def test_iverilog_runtime_evidence(tmp_path, body, clock, settings, passes, ended, time, code):
    require_iverilog_evidence()
    flow = run_tb(tmp_path / "design", body, decls=CLOCK if clock else "", settings=settings)
    assert flow.succeeded is passes
    assert error_code(flow) == code
    assert flow.results["sim.ended_by"] == ended
    assert flow.results["sim.time"] == time
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["metrics"]["sim"]["evidence"] == flow.results["sim.evidence"]
    assert (flow.run_path / "xeda_end.json").is_file() or ended == "fatal"


def test_iverilog_events_carry_the_native_diagnostics(tmp_path):
    require_iverilog_evidence()
    flow = run_tb(tmp_path / "design", '#5 $error("PROBE_error"); $finish(0);')
    events = flow.results["sim.evidence"]["events"]
    error = next(event for event in events if event["kind"] == "error")
    assert "PROBE_error" in error["message"] and "tb.sv" in error["location"]
    assert flow.results["sim.errors"] == 1


def test_iverilog_results_of_a_previous_run_are_not_counted(tmp_path):
    """The end record and the waveform of a run are no evidence of the next run in the same
    directory."""
    require_iverilog_evidence()
    first = run_tb(tmp_path / "design", f"{DUMP} #5 $finish;", settings={"vcd": True})
    assert first.succeeded
    assert (first.run_path / "dump.vcd").is_file()
    second = run_tb(tmp_path / "design", "#5;", settings={"vcd": True})
    assert second.run_path == first.run_path
    assert not second.succeeded
    assert second.results["sim.ended_by"] == "unknown"
    assert error_code(second) == "rejected"


@pytest.mark.parametrize(
    "decls,parameters,line",
    [
        ("parameter int N = 3;", {"N": 42}, "N=42"),
        ('parameter string S = "a";', {"S": "hello world"}, "S=hello world"),
        ("parameter [7:0] H = 0;", {"H": "8'hff"}, "H=255"),
        ("parameter bit B = 0;", {"B": True}, "B=1"),
        # text is a string, so an integer parameter takes the bytes of "1+2"
        ("parameter int N = 3;", {"N": "1+2"}, f"N={int.from_bytes(b'1+2')}"),
    ],
    ids=["int", "string-with-space", "sized-literal", "bool", "expression-text"],
)
def test_iverilog_parameters(tmp_path, decls, parameters, line):
    require_iverilog_evidence()
    name = next(iter(parameters))
    fmt = "%s" if name == "S" else "%0d"
    flow = run_tb(
        tmp_path / "design",
        f'$display("{name}={fmt}", {name}); $finish;',
        decls=decls,
        tb={"parameters": parameters},
    )
    assert flow.succeeded
    assert line in log_of(flow).splitlines()


def test_iverilog_rtl_parameters_apply_to_the_rtl_top_only(tmp_path):
    require_iverilog_evidence()
    flow = run_tb(
        tmp_path / "design",
        '$display("N=%0d M=%0d", N, M); $finish;',
        decls="parameter int N = 1; parameter int M = 1;",
        rtl={"parameters": {"N": 5, "M": 6}},
        tb={"parameters": {"M": 7}},
    )
    assert "N=5 M=7" in log_of(flow)


def test_iverilog_defines(tmp_path):
    """A define without a value is an empty macro (`-DX=`), not 1."""
    require_iverilog_evidence()
    flow = run_tb(
        tmp_path / "design",
        '$display("V=%0d W=%0d", 7`X, `Y); $finish;',
        rtl={"defines": {"X": None, "Y": 5}},
        tb={"defines": {"Y": 6}},
    )
    assert "V=7 W=6" in log_of(flow)


@pytest.mark.parametrize(
    "standard,passes", [(None, True), ("2012", True), ("2005", False)], ids=str
)
def test_iverilog_language_standard(tmp_path, standard, passes):
    """`string` needs the 2012 generation, the default."""
    require_iverilog_evidence()
    flow = run_tb(
        tmp_path / "design",
        'string s = "x"; $finish;',
        language={"verilog": {"standard": standard}} if standard else None,
    )
    assert flow.succeeded is passes


def test_iverilog_refuses_a_standard_it_has_no_generation_for(tmp_path):
    with pytest.raises(FlowSettingsException, match="no generation.*2017"):
        run_tb(tmp_path / "design", "$finish;", language={"verilog": {"standard": "2017"}})
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "settings,prefix,time",
    [
        ({}, "", 5_000_000),
        ({"time_unit": "10 ns", "time_resolution": "1 ns"}, "", 50_000_000),
        ({"time_unit": "1 us", "time_resolution": "10 ps"}, "", 5_000_000_000),
        # a `timescale directive wins over the settings
        ({"time_unit": "10 ns", "time_resolution": "1 ns"}, "`timescale 1us/1ns\n", 5_000_000_000),
    ],
    ids=["default", "override", "override-micro", "source-wins"],
)
def test_iverilog_time_scale(tmp_path, settings, prefix, time):
    require_iverilog_evidence()
    flow = run_tb(tmp_path / "design", "#5 $finish;", settings=settings, prefix=prefix)
    assert flow.succeeded
    assert flow.results["sim.time"] == time


def test_iverilog_refuses_a_resolution_longer_than_the_unit(tmp_path):
    with pytest.raises(FlowSettingsException, match="time_resolution.*longer than"):
        run_tb(
            tmp_path / "design",
            "$finish;",
            settings={"time_unit": "1 ns", "time_resolution": "10 ns"},
        )


@pytest.mark.parametrize("fmt", ["vcd", "fst"])
def test_iverilog_waveform_is_the_format_the_setting_selects(tmp_path, fmt):
    """The testbench's `$dumpvars` writes the file, and the setting picks the format of `vvp`."""
    require_iverilog_evidence()
    dump = f'$dumpfile("dump.{fmt}"); $dumpvars(0, tb);'
    flow = run_tb(tmp_path / "design", f"{dump} #5 $finish;", settings={fmt: True})
    assert flow.succeeded, flow.results.get("error")
    data = (flow.run_path / f"dump.{fmt}").read_bytes()
    assert data.startswith(b"$date") is (fmt == "vcd")
    if fmt == "fst":
        assert b"$timescale" not in data  # a binary FST block, not text


@pytest.mark.parametrize("fmt", ["vcd", "fst"])
def test_iverilog_fails_when_the_testbench_writes_no_waveform(tmp_path, fmt):
    require_iverilog_evidence()
    flow = run_tb(tmp_path / "design", "#5 $finish;", settings={fmt: True})
    assert not flow.succeeded
    assert error_code(flow) == "reported_failure"
    assert (
        f"dump.{fmt}"
        in json.loads((flow.run_path / "results.json").read_text())["error"]["message"]
    )


@pytest.mark.parametrize(
    "settings,match",
    [
        ({"vcd": True, "fst": True}, "one waveform"),
        ({"vcd": "w.vcd"}, "`vcd` names a waveform file"),
        ({"fst": "other/w.fst"}, "`fst` names a waveform file"),
        ({"plusargs": ["hello"]}, "plusargs has 'hello'.*does not start with"),
        ({"compile_flags": ["-o", "x.vvp"]}, "compile_flags has '-o'"),
        ({"compile_flags": ["-stb"]}, "compile_flags has '-stb'"),
        ({"compile_flags": ["-g2012"]}, "compile_flags has '-g2012'"),
        ({"run_flags": ["-s"]}, "run_flags has `-s`"),
        ({"lib_paths": [("leaf", None)]}, "`lib_paths` names the library leaf without"),
        ({"time_unit": "3 ns"}, "time_unit"),
    ],
    ids=[
        "vcd-and-fst",
        "vcd-name",
        "fst-location",
        "plusarg-without-plus",
        "flag-o",
        "flag-s",
        "flag-g",
        "run-flag-s",
        "library-without-directory",
        "odd-time-unit",
    ],
)
def test_iverilog_refuses_when_planned(tmp_path, settings, match):
    with pytest.raises(FlowSettingsException, match=match):
        run_tb(tmp_path / "design", "$finish;", settings=settings)
    assert not (tmp_path / "runs").exists()


def test_iverilog_refuses_a_parameter_of_an_inner_instance(tmp_path):
    with pytest.raises(FlowSettingsException, match="cannot set the parameter u"):
        run_tb(tmp_path / "design", "$finish;", tb={"parameters": {"u.N": 1}})
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("value", [[1, 2], 'a"b'], ids=["list", "embedded-quote"])
def test_iverilog_fails_on_a_parameter_value_it_cannot_read(tmp_path, value):
    """`iverilog` keeps the default of such a parameter or cuts it short, and exits with 0."""
    require_iverilog_evidence()
    with pytest.raises(FlowFatalError, match="iverilog refused a parameter"):
        run_tb(
            tmp_path / "design",
            "$finish;",
            decls="parameter N = 1;",
            tb={"parameters": {"N": value}},
        )


def test_iverilog_refuses_a_design_without_a_top(tmp_path):
    (tmp_path / "dut.v").write_text("module dut; endmodule\n")
    design = Design(name="no_top", root=tmp_path, rtl={"sources": ["dut.v"]})
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsException, match="no simulation top"):
        runner.run_flow("iverilog", design, {})


def test_iverilog_finds_headers_and_libraries_from_the_design_root(tmp_path):
    """A relative path of `include_dirs`, `verilog_libs` and `lib_paths` is the design root's,
    though `iverilog` runs in the run directory."""
    require_iverilog_evidence()
    files = {
        "inc/defs.vh": "`define K 9\n",
        "libfile/a.v": 'module lib_a; initial $display("LIB_A"); endmodule\n',
        "libdir/lib_b.v": 'module lib_b; initial $display("LIB_B"); endmodule\n',
    }
    flow = run_tb(
        tmp_path / "design",
        '$display("K=%0d", `K); $finish;',
        decls="lib_a a(); lib_b b();",
        prefix='`include "defs.vh"\n',
        files=files,
        settings={
            "include_dirs": ["inc"],
            "verilog_libs": ["libfile/a.v"],
            "lib_paths": [("lib_b", "libdir")],
        },
    )
    assert flow.succeeded, flow.results.get("error")
    assert {"K=9", "LIB_A", "LIB_B"} <= set(log_of(flow).splitlines())


def test_iverilog_plusargs_reach_the_simulation(tmp_path):
    require_iverilog_evidence()
    flow = run_tb(
        tmp_path / "design",
        'if ($test$plusargs("hello")) $display("GOT_HELLO");'
        ' if ($value$plusargs("seed=%d", seed)) $display("SEED=%0d", seed); $finish;',
        decls="integer seed;",
        settings={"plusargs": ["+hello", "+seed=3"]},
    )
    assert {"GOT_HELLO", "SEED=3"} <= set(log_of(flow).splitlines())


def test_iverilog_simulates_every_top_module(tmp_path):
    require_iverilog_evidence()
    flow = run_tb(
        tmp_path / "design",
        '$display("TB"); #5 $finish;',
        tb={"top": ["tb", "other"]},
        extra_modules='module other; initial $display("OTHER"); endmodule\n',
    )
    assert flow.succeeded
    assert {"TB", "OTHER"} <= set(log_of(flow).splitlines())


@pytest.mark.parametrize("specify", [False, True])
def test_iverilog_honors_specify_delays_only_when_asked(tmp_path, specify):
    require_iverilog_evidence()
    gate = (
        "module gate(input a, output y);\n"
        "  specify (a => y) = 10; endspecify\n"
        "  assign y = a;\nendmodule\n"
    )
    flow = run_tb(
        tmp_path / "design",
        'a = 0; #1 a = 1; #5 $display("Y=%b", y); #20 $display("Y=%b", y); $finish;',
        decls="reg a; wire y; gate u(a, y);",
        extra_modules=gate,
        settings={"specify": specify},
    )
    assert flow.succeeded
    lines = [line for line in log_of(flow).splitlines() if line.startswith("Y=")]
    assert lines == [f"Y={'x' if specify else '1'}", "Y=1"]


def test_iverilog_loads_a_vpi_module(tmp_path):
    require_iverilog_evidence()
    maker = shutil.which("iverilog-vpi")
    if maker is None:
        pytest.skip("iverilog-vpi is unavailable")
    build = tmp_path / "vpi"
    build.mkdir()
    (build / "hello.c").write_text(
        "#include <vpi_user.h>\n"
        'static void hello(void) { vpi_printf("HELLO_VPI\\n"); }\n'
        "void (*vlog_startup_routines[])(void) = { hello, 0 };\n"
    )
    made = subprocess.run([maker, "hello.c"], cwd=build, capture_output=True, timeout=120)
    if made.returncode != 0 or not (build / "hello.vpi").is_file():
        pytest.skip("iverilog-vpi cannot build a module here")
    flow = run_tb(
        tmp_path / "design", "$finish;", settings={"vpi_modules": [str(build / "hello.vpi")]}
    )
    assert flow.succeeded
    assert "HELLO_VPI" in log_of(flow)


def _dut_design(root: Path, test: str) -> Design:
    (root / "dut.sv").write_text(
        "module dut #(parameter int W = 1) (input logic [W-1:0] a, output logic [W-1:0] y);\n"
        "  assign y = ~a;\nendmodule\n"
    )
    (root / "tb_dut.py").write_text(test)
    return Design(
        name="icarus_cocotb",
        root=root,
        rtl={"sources": ["dut.sv"], "top": "dut", "parameters": {"W": 4}},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )


_WIDTH_TEST = (
    _TEST_HEADER
    + "@cocotb.test()\n"
    + "async def width(dut):\n"
    + "    dut.a.value = 0\n"
    + "    await Timer(1, 'ns')\n"
    + "    assert len(dut.a) == 4 and int(dut.y.value) == 15\n"
)


@pytest.mark.usefixtures("cocotb_safe_vvp")
@pytest.mark.parametrize(
    "settings,wave",
    [({}, None), ({"vcd": True}, "dump.vcd"), ({"fst": "w.fst"}, "w.fst")],
    ids=["plain", "vcd", "fst"],
)
def test_iverilog_cocotb(tmp_path, settings, wave):
    """cocotb decides the run, the RTL parameters reach the toplevel, and Xeda's dump module
    writes the waveform."""
    require_cocotb()
    require_iverilog_evidence()
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        "iverilog", _dut_design(tmp_path, _WIDTH_TEST), settings
    )
    assert flow is not None and flow.succeeded, flow.results.get("error")
    assert flow.results["cocotb.tests"] == 1
    assert not (flow.run_path / "xeda_end.json").exists()
    if wave:
        assert (flow.run_path / wave).is_file()


@pytest.mark.usefixtures("cocotb_safe_vvp")
def test_iverilog_cocotb_failing_test_fails_the_run(tmp_path):
    require_cocotb()
    require_iverilog_evidence()
    failing = _TEST_HEADER + _cocotb_test("passes").replace("== 1", "== 0")
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        "iverilog", _dut_design(tmp_path, failing)
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["cocotb.failures"] == 1


def test_iverilog_cocotb_refuses_stop_time(tmp_path):
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "tb_dut.py").write_text(_TEST_HEADER)
    design = Design(
        name="icarus_cocotb",
        root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )
    with pytest.raises(FlowSettingsException, match="cannot stop a cocotb simulation"):
        DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
            "iverilog", design, {"stop_time": "10 ns"}
        )
