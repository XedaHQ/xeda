#!/usr/bin/env python3
import json
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError, FlowSettingsError, FlowSettingsException
from xeda.flow.sim import time_in_fs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Verilator
from xeda.run_dir import RunDirectoryError

from .test_cli_structured_output import run_xeda
from .tool_utils import require_cocotb, require_verilator

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_verilator_sim_py(tmp_path: Path) -> None:
    require_verilator()
    require_cocotb()
    design_paths = [
        EXAMPLES_DIR / "sv" / "fifo" / "fifo.xeda.yaml",
        EXAMPLES_DIR / "sv" / "fifo" / "fifo_cocotb.xeda.yaml",
    ]
    run_dir = tmp_path / "xeda_run"
    for design in design_paths:
        xeda_runner = DefaultRunner(run_dir, debug=debug)
        flow = xeda_runner.run(Verilator, design, flow_overrides=dict(debug=debug, verbose=debug))
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert flow.succeeded
        if design.name == "fifo.xeda.yaml":
            # the example defines DUMP_VCD, so its testbench's $dumpfile block writes the waveform
            assert (flow.run_path / "dump.vcd").is_file()
        assert isinstance(flow.settings, Verilator.Settings)
        assert results_json.exists()


@pytest.mark.parametrize("expected,success", [(1, True), (0, False)])
def test_verilator_cocotb_verdict(tmp_path: Path, expected: int, success: bool) -> None:
    require_verilator()
    require_cocotb()
    (tmp_path / "dut.sv").write_text(
        "module dut(input logic a, output logic y); assign y = ~a; endmodule\n"
    )
    (tmp_path / "tb_dut.py").write_text(
        "import cocotb\n"
        "from cocotb.triggers import Timer\n"
        "@cocotb.test()\n"
        "async def check_output(dut):\n"
        "    dut.a.value = 0\n"
        "    await Timer(1, 'ns')\n"
        f"    assert int(dut.y.value) == {expected}\n"
    )
    design = Design(
        name="cocotb_verdict",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(Verilator, design)
    assert flow is not None
    assert flow.succeeded is success
    assert flow.results["cocotb.tests"] == 1
    assert flow.results["cocotb.failures"] == (0 if success else 1)
    # cocotb's output is not piped (it keeps its terminal and colors): its results decide
    assert not (flow.run_path / flow.settings.sim_dir / "sim.log").exists()


def _launch(
    tmp_path: Path,
    body: str,
    settings: dict,
    extra_modules: str = "",
    timescale: str = "1ns/1ps",
    extra_sources: dict | None = None,
) -> Verilator:
    """A one-file Verilog testbench `tb` whose body is `body`, simulated by the verilator flow;
    `extra_sources` (name -> text) are written beside it and added to the testbench."""
    (tmp_path / "tb.sv").write_text(
        f"`timescale {timescale}\nmodule tb; {body} endmodule\n{extra_modules}"
    )
    for name, text in (extra_sources or {}).items():
        (tmp_path / name).write_text(text)
    design = Design(
        name="tb",
        design_root=tmp_path,
        rtl={"sources": ["tb.sv"], "top": "tb"},
        tb={"sources": ["tb.sv", *(extra_sources or {})], "top": "tb"},
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Verilator, design, settings
    )
    assert flow is not None
    return flow


@pytest.mark.parametrize(
    ("body", "settings", "ended_by", "success"),
    [
        ("initial begin #34 $finish; end", {"timing": True}, "finish", True),
        ("initial begin $finish; end", {}, "finish", True),  # untimed, $finish at 0
        ("reg clk; initial clk = 0;", {}, "drained", False),  # nothing ever ends it
        ("initial begin #10; #10; end", {"timing": True}, "drained", False),
        ('initial begin #5 $error("e"); #5 $finish; end', {"timing": True}, "error", False),
        ("initial begin #5 $stop; end", {"timing": True}, "error", False),
        pytest.param(
            'initial begin #5 $error("e"); #5 $finish; end',
            {"timing": True, "fail_severity": "failure"},
            "finish",
            True,
        ),
        (
            'initial begin #5 $fatal(1, "f"); end',
            {"timing": True, "fail_severity": "failure"},
            "fatal",
            False,
        ),
        pytest.param(
            'initial begin #5 $warning("w"); #5 $finish; end',
            {"timing": True, "fail_severity": "warning"},
            "finish",
            False,
        ),
    ],
)
def test_verilator_passes_only_on_evidence(tmp_path, body, settings, ended_by, success):
    require_verilator()
    flow = _launch(tmp_path, body, settings)
    assert flow.results["sim.ended_by"] == ended_by
    assert flow.succeeded is success


@pytest.mark.parametrize(
    ("body", "ended_by", "kind"),
    [
        ('initial begin #5 $error("e"); #5 $finish; end', "error", "error"),
        ("initial begin #5 $stop; end", "error", "error"),
        ('initial begin #5 $fatal(1, "f"); end', "fatal", "fatal"),
    ],
    ids=["error", "stop", "fatal"],
)
def test_verilator_records_one_event_for_the_report_that_ended_it(tmp_path, body, ended_by, kind):
    """`$error`, `$stop` and `$fatal` each end the run with the cause they have, and
    record the report once: not as a fatal error, and not as a stop followed by a fatal error."""
    require_verilator()
    flow = _launch(tmp_path, body, {"timing": True})
    assert flow.results["sim.ended_by"] == ended_by
    events = flow.results["sim.evidence"]["events"]
    assert [event["kind"] for event in events] == [kind]
    assert flow.results["sim.errors"] == 1
    assert not flow.succeeded


DIRECT_STOP_DRIVER = """#include "verilated.h"
int main(int, char**) {
    vl_stop("direct.v", 3, "top");  // a `$stop` that does not come through `vl_stop_maybe`
    return 0;
}
"""


def test_a_stop_reaching_the_hooks_directly_is_recorded_once_as_an_error(tmp_path):
    """Verilator calls `vl_stop` itself for some stops. It is one event, `stop`, and the run
    ended by an error: not a stop followed by a fatal error."""
    require_verilator()
    flow = _launch(
        tmp_path,
        "initial begin #5 $finish; end",
        {"timing": True, "main": False},
        extra_sources={"main.cpp": DIRECT_STOP_DRIVER},
    )
    assert flow.results["sim.ended_by"] == "error"
    assert [e["kind"] for e in flow.results["sim.evidence"]["events"]] == ["stop"]
    assert not flow.succeeded


def test_the_hooks_header_reaches_make_by_name_not_by_the_run_directory_path(tmp_path):
    """make splits the compiler flags and MAKEFILES at a space: a path in them breaks a build
    whose path has one. make runs in `sim_dir`, where the hooks' files are, so it reads the
    fragment by name, and the fragment names the header by its own directory. No flag forces the
    header into the model's compiles."""
    require_verilator()
    flow = _launch(tmp_path, "initial begin #5 $finish; end", {"timing": True})
    assert flow.succeeded
    sim_dir = flow.run_path / flow.settings.sim_dir
    makefile = (sim_dir / "Vtop.mk").read_text()
    assert "xeda_hooks.h" not in makefile
    assert str(flow.run_path) not in makefile
    assert (sim_dir / "xeda_hooks.mk").is_file() and (sim_dir / "xeda_hooks.h").is_file()


def test_a_model_compiled_in_parallel_uses_its_precompiled_header_with_the_hooks(tmp_path):
    """Verilator compiles a large or split model in parallel, each file with a precompiled
    header that it forces in. GCC uses that header only when no other file is forced in before
    it, and fails on the header's missing text otherwise. The hooks' declaration therefore goes to
    verilated.cpp alone (`xeda_hooks.mk`): the model builds, and an `$error` still reaches the
    hooks through `vl_stop_maybe`."""
    require_verilator()
    split = ["--output-split", "1", "--output-split-cfuncs", "1"]
    flow = _launch(
        tmp_path,
        'initial begin #5 $error("e"); #5 $finish; end',
        {"timing": True, "compile_args": split},
    )
    sim_dir = flow.run_path / flow.settings.sim_dir
    assert "VM_PARALLEL_BUILDS = 1" in (sim_dir / "Vtop_classes.mk").read_text()
    assert list(sim_dir.glob("Vtop__pch.h.*.gch"))
    assert flow.results["sim.ended_by"] == "error"
    assert [event["kind"] for event in flow.results["sim.evidence"]["events"]] == ["error"]


def test_a_delay_verilator_ignores_is_warned_about_not_failed(tmp_path, capfd):
    """Without `timing`, Verilator ignores a delay: the testbench's `#100; $finish` ends at time
    0. The warnings say so, in the build's output: `STMTDLY` for a delay on a statement (xeda used
    to hide it) and `ASSIGNDLY` for one on an assignment (it never did). The default
    `-Wno-fatal` keeps them warnings, so the run still passes."""
    require_verilator()
    flow = _launch(
        tmp_path, "reg r; initial begin r <= #1 1'b1; #100; $finish; end", {"timing": False}
    )
    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "%Warning-STMTDLY" in output
    assert "%Warning-ASSIGNDLY" in output
    assert flow.succeeded
    assert flow.results["sim.time"] == 0


def test_verilator_enables_timing_by_default(tmp_path):
    """A delay runs in the default mode, so `$finish` is recorded at its simulation time."""
    require_verilator()
    flow = _launch(tmp_path, "initial begin #100; $finish; end", {}, timescale="1ps/1ps")
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"
    assert flow.results["sim.time"] == 100_000  # 100 ps in femtoseconds


def test_verilator_reports_an_error_at_its_default_simulation_time(tmp_path):
    """The default mode must run delayed assertions at their stated simulation time."""
    require_verilator()
    flow = _launch(
        tmp_path,
        'initial begin #10; $error("late error"); end',
        {},
        timescale="1ps/1ps",
    )
    assert not flow.succeeded
    assert flow.results["sim.ended_by"] == "error"
    assert flow.results["sim.time"] == 10_000  # 10 ps in femtoseconds


def test_verilator_simulates_the_designs_testbench_top(tmp_path):
    """Two top-level candidates: the design's `tb.top` is the one simulated."""
    require_verilator()
    flow = _launch(
        tmp_path,
        'initial begin $display("TB"); $finish; end',
        {},
        extra_modules='module other; initial begin $display("OTHER"); $finish; end endmodule\n',
    )
    log = (flow.run_path / flow.settings.sim_dir / "sim.log").read_text()
    assert "TB" in log and "OTHER" not in log
    assert flow.succeeded


def test_a_design_with_its_own_main_and_hdl_sources_needs_no_testbench_top(tmp_path):
    """HDL among the testbench's sources (a checker bound into the design) needs `tb.top`, unless
    the design's own C++ main (`main: false`) runs the model: that main runs the RTL top. The
    same design with `main` unset is refused, since Verilator's main would run no stimulus."""
    require_verilator()
    (tmp_path / "dut.sv").write_text(
        "`timescale 1ns/1ps\nmodule dut(input logic clk);\n"
        "  logic [3:0] cnt = 0;\n  initial begin #50 $finish; end\nendmodule\n"
    )
    (tmp_path / "chk.sv").write_text(
        "module chk(input logic clk, input logic [3:0] cnt);\nendmodule\n"
        "bind dut chk c(.clk(clk), .cnt(cnt));\n"
    )
    (tmp_path / "main.cpp").write_text(OWN_DRIVER.replace("STATUS", "0"))
    design = Design(
        name="dut",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["chk.sv", "main.cpp"]},
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    for settings in ({"timing": True}, {"timing": True, "main": True}):
        with pytest.raises(FlowSettingsException, match="`tb.top`"):
            runner.plan("verilator", design, flow_settings=settings)
        with pytest.raises(FlowSettingsException, match="`tb.top`"):
            runner.run_flow(Verilator, design, settings)
    assert not (tmp_path / "runs").exists()
    runner.plan("verilator", design, flow_settings={"timing": True, "main": False})
    flow = runner.run_flow(Verilator, design, {"timing": True, "main": False})
    assert flow is not None and flow.succeeded
    assert flow.results["sim.ended_by"] == "exit"


@pytest.mark.parametrize(
    ("timescale", "ticks"),
    [("1ns/1ns", 15), ("1ns/1ps", 15000), ("1ns/1fs", 15000000), ("1us/100ps", 150)],
)
def test_verilator_records_the_finish_time_in_the_models_precision(tmp_path, timescale, ticks):
    """The `$finish` is recorded at its simulated time, in ticks of the model's time precision,
    and the record says which precision it is."""
    require_verilator()
    flow = _launch(
        tmp_path, "initial begin #15ns; $finish; end", {"timing": True}, timescale=timescale
    )
    assert flow.results["sim.ended_by"] == "finish"
    precision = timescale.split("/")[1]
    assert flow.results["sim.time"] == time_in_fs(ticks, precision)
    assert "sim.time_unit" not in flow.results
    assert flow.succeeded


def test_a_drained_run_fails_and_has_no_time(tmp_path):
    """Verilator's main returns when no event is left. With no `$finish`, the hooks record
    `drained`, which fails, and the end time is unknown."""
    require_verilator()
    flow = _launch(tmp_path, "initial begin #10; #10; end", {"timing": True})
    assert flow.results["sim.ended_by"] == "drained"
    assert "sim.time" not in flow.results
    assert not flow.succeeded


def test_a_finish_in_a_final_block_does_not_end_a_drained_run(tmp_path):
    """The `final` block runs after the simulation ended. Its `$finish` is no end of the
    simulation: the run drained, and fails. The context reports a running `final` block from
    Verilator 5.048, the flow's floor."""
    require_verilator()
    flow = _launch(tmp_path, "initial begin #10; end final $finish;", {"timing": True})
    assert flow.results["sim.ended_by"] == "drained"
    assert not flow.succeeded


def test_the_wall_clock_timeout_stops_a_simulation_that_never_finishes(tmp_path):
    """A free-running clock never drains, and nothing stops it but the wall-clock `timeout`,
    which leaves no end record (a failure)."""
    require_verilator()
    flow = _launch(tmp_path, "reg clk = 0; always #5 clk = ~clk;", {"timing": True, "timeout": 2})
    assert "sim.ended_by" not in flow.results
    assert flow.results["error"]["code"] == "timeout"
    assert not flow.succeeded


@pytest.mark.parametrize("own_driver", [False, True], ids=["verilator_main", "own_main"])
def test_a_timed_out_model_s_output_reaches_the_log(tmp_path, own_driver):
    """The model's output is copied through a pipe, which would leave the C runtime's stdout
    block-buffered: what a testbench printed before it hung would be lost when the time limit
    kills it. xeda's hooks make it line-buffered, under Verilator's main and a design's alike."""
    require_verilator()
    flow = _launch(
        tmp_path,
        'reg clk = 0; always #5 clk = ~clk; initial $display("printed before the hang");',
        {"timing": True, "timeout": 3, **({"main": False} if own_driver else {})},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", "0")} if own_driver else None,
    )
    assert (flow.results.get("error") or {}).get("code") == "timeout"
    assert not flow.succeeded
    log = (flow.run_path / flow.settings.sim_dir / "sim.log").read_text()
    assert "printed before the hang" in log


OWN_DRIVER = """#include <memory>
#include "verilated.h"
#include "Vtop.h"
int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> contextp{new VerilatedContext};
    contextp->commandArgs(argc, argv);
    const std::unique_ptr<Vtop> topp{new Vtop{contextp.get(), ""}};
    while (!contextp->gotFinish()) {
        topp->eval();
        if (!topp->eventsPending()) break;
        contextp->time(topp->nextTimeSlot());
    }
    topp->final();
    return STATUS;
}
"""


@pytest.mark.parametrize(
    ("body", "status", "ended_by", "success"),
    [
        ("initial begin #10 $finish; end", 0, "exit", True),
        ("initial begin #10 $finish; end", 3, "exit", False),
        ('initial begin #5 $fatal(1, "f"); end', 0, "fatal", False),
    ],
)
def test_verilator_judges_the_designs_own_driver_by_its_exit(
    tmp_path, body, status, ended_by, success
):
    """A design with its own C++ main (`main: false`) keeps it; xeda's hooks, linked into it,
    record its end: the main's exit (with the status it returned), or a fatal error."""
    require_verilator()
    flow = _launch(
        tmp_path,
        body,
        {"timing": True, "main": False},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", str(status))},
    )
    assert flow.results["sim.ended_by"] == ended_by
    assert flow.succeeded is success


@pytest.mark.parametrize("own_driver", [False, True], ids=["verilator_main", "own_main"])
def test_a_failed_model_keeps_its_warning_evidence(tmp_path, own_driver):
    """A failed model's log still supplies warnings to its failure document."""
    require_verilator()
    ending = "$finish" if own_driver else '$fatal(1, "failed")'
    flow = _launch(
        tmp_path,
        f'initial begin #5 $warning("before failure"); #5 {ending}; end',
        {"timing": True, "fail_severity": "warning", **({"main": False} if own_driver else {})},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", "3")} if own_driver else None,
    )
    assert not flow.succeeded
    assert flow.results["error"]["code"] == "nonzero_exit"
    assert flow.results["sim.warnings"] == 1


@pytest.mark.parametrize(
    "optimize,expected",
    [
        (True, "-O3"),
        (False, None),
        (0, "-O0"),
        (1, "-O1"),
        (2, "-O2"),
        (3, "-O3"),
        ("0", "-O0"),
        ("2", "-O2"),
    ],
)
def test_optimize_is_the_level_that_follows_the_o_of_verilator(
    tmp_path, commands, optimize, expected
):
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(name="tb", design_root=tmp_path, rtl={"sources": ["tb.sv"], "top": "tb"})
    DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Verilator, design, {"optimize": optimize}
    )
    (command,) = [c for c in commands if c[0] == "verilator" and "--top-module" in c]
    assert [a for a in command if a.startswith("-O")] == ([expected] if expected else [])


@pytest.mark.parametrize(
    "optimize,message",
    [
        ("-O2", "write `2`, not `-O2`"),
        ("-O3", "write `3`, not `-O3`"),
        ("s", "true, false or a level of 0, 1, 2 or 3"),
        ("4", "true, false or a level of 0, 1, 2 or 3"),
        (4, "true, false or a level of 0, 1, 2 or 3"),
        (-1, "true, false or a level of 0, 1, 2 or 3"),
        ("", "true, false or a level of 0, 1, 2 or 3"),
        ("-O", "true, false or a level of 0, 1, 2 or 3"),
    ],
)
def test_optimize_that_verilator_would_not_take_is_refused_before_it_runs(
    tmp_path, optimize, message
):
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(name="tb", design_root=tmp_path, rtl={"sources": ["tb.sv"], "top": "tb"})
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsError, match=message):
        runner.plan("verilator", design, flow_settings={"optimize": optimize})
    assert not (tmp_path / "runs").exists()


if __name__ == "__main__":
    test_verilator_sim_py()


def test_verilator_defaults_do_not_randomize(tmp_path):
    from xeda.flows.verilator import MIN_VERILATOR_VERSION

    ss = Verilator.Settings()
    assert ss.random_init is False and ss.x_initial == "0" and ss.x_assign == "0"
    assert ss.fail_severity == "error"
    assert MIN_VERILATOR_VERSION == (5, 48)


def test_the_model_arguments_are_not_written_into_the_settings(tmp_path):
    """`--trace`, the seed and xeda's own arguments are the run's, not the user's."""
    require_verilator()
    flow = _launch(
        tmp_path,
        "initial begin #1 $finish; end",
        {"timing": True, "vcd": True, "random_init": True},
    )
    assert flow.settings.model_args == []


def test_rtl_parameters_reach_a_cocotb_top(tmp_path):
    """With cocotb the simulated top is the RTL top, so `rtl.parameters` apply to it."""
    require_verilator()
    require_cocotb()
    (tmp_path / "dut.sv").write_text(
        "module dut #(parameter W = 1) (output logic [W-1:0] y); assign y = '1; endmodule\n"
    )
    (tmp_path / "tb_dut.py").write_text(
        "import cocotb\n"
        "from cocotb.triggers import Timer\n"
        "@cocotb.test()\n"
        "async def width(dut):\n"
        "    await Timer(1, 'ns')\n"
        "    assert int(dut.y.value) == 15\n"
    )
    design = Design(
        name="dut",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut", "parameters": {"W": 4}},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(Verilator, design)
    assert flow is not None and flow.succeeded


def test_a_cocotb_toplevel_other_than_the_rtl_top_is_simulated(tmp_path):
    """cocotb drives `tb.cocotb.toplevel` (a wrapper of the RTL top here): that is the model's
    top, and `rtl.parameters` (the RTL top's) do not apply to it."""
    require_verilator()
    require_cocotb()
    (tmp_path / "dut.sv").write_text(
        "module dut #(parameter W = 1) (output logic [W-1:0] y); assign y = '1; endmodule\n"
    )
    (tmp_path / "dut_wrap.sv").write_text(
        "module dut_wrap (output logic [1:0] wrapped); dut #(.W(2)) u (.y(wrapped)); endmodule\n"
    )
    (tmp_path / "tb_wrap.py").write_text(
        "import cocotb\n"
        "from cocotb.triggers import Timer\n"
        "@cocotb.test()\n"
        "async def wrapped(dut):\n"
        "    await Timer(1, 'ns')\n"
        "    assert int(dut.wrapped.value) == 3\n"
    )
    design = Design(
        name="dut_wrap",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut", "parameters": {"W": 4}},
        tb={
            "sources": ["dut_wrap.sv", "tb_wrap.py"],
            "cocotb": {"toplevel": "dut_wrap"},
        },
    )
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(Verilator, design)
    assert flow is not None and flow.succeeded
    assert flow.results["cocotb.tests"] == 1


def _stop_time_designs(tmp_path):
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "tb_dut.py").write_text("")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    cases = {
        "hdl": ({"sources": []}, {}),
        "cpp": ({"sources": ["main.cpp"]}, {"main": False}),
        "cocotb": ({"sources": ["tb_dut.py"], "cocotb": True}, {}),
    }
    for name, (tb, settings) in cases.items():
        design = Design(
            name=name, design_root=tmp_path, rtl={"sources": ["dut.sv"], "top": "dut"}, tb=tb
        )
        yield name, design, settings


def test_stop_time_is_refused_in_every_mode_before_anything_runs(tmp_path):
    """Verilator's own main, the design's own main and cocotb's main cannot stop at a simulated
    time. The plan refuses it, and so does a flow built directly."""
    for name, design, settings in _stop_time_designs(tmp_path):
        runner = DefaultRunner(tmp_path / f"runs-{name}", display_results=False)
        with pytest.raises(FlowSettingsException, match=r"\$finish.*`timeout`"):
            runner.plan("verilator", design, flow_settings={**settings, "stop_time": "10ns"})
        assert not (tmp_path / f"runs-{name}").exists()
        flow_settings = Verilator.Settings.from_input(
            {**settings, "stop_time": "10ns"}, design_root=tmp_path
        )
        with pytest.raises(FlowSettingsException, match="cannot stop at a simulated time"):
            Verilator(flow_settings, design, tmp_path / f"run-{name}").init()


@pytest.mark.parametrize("pair", [("vcd", "fst"), ("vcd", "saif"), ("fst", "saif")])
def test_two_trace_formats_are_refused(tmp_path, pair):
    """Verilator takes one trace format; the flow no longer picks one of two in silence."""
    (tmp_path / "d.sv").write_text("module d; endmodule\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["d.sv"], "top": "d"})
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsException, match="one trace format"):
        runner.plan("verilator", design, flow_settings={name: True for name in pair})


def test_the_trace_formats_are_switches_that_default_to_verilators_own_options(tmp_path, commands):
    """`true` selects a format, YAML and command line alike, and no trace option beyond the format
    is passed unless it is set."""
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(name="tb", design_root=tmp_path, rtl={"sources": ["tb.sv"], "top": "tb"})
    for name in ("vcd", "fst", "saif"):
        commands.clear()
        DefaultRunner(tmp_path / f"runs-{name}", display_results=False).run_flow(
            Verilator, design, {name: True}
        )
        (command,) = [c for c in commands if c[0] == "verilator" and "--top-module" in c]
        assert f"--trace-{name}" in command
        assert not [a for a in command if a.startswith("--trace") and a != f"--trace-{name}"]
    commands.clear()
    DefaultRunner(tmp_path / "runs-set", display_results=False).run_flow(
        Verilator,
        design,
        {"vcd": True, "trace_underscore": True, "trace_structs": True, "trace_max_width": 64},
    )
    (command,) = [c for c in commands if c[0] == "verilator" and "--top-module" in c]
    assert {"--trace-underscore", "--trace-structs"} <= set(command)
    assert command[command.index("--trace-max-width") + 1] == "64"
    assert "--trace-max-array" not in command


def test_verilators_main_is_asked_for_unless_the_design_brings_its_own(tmp_path, commands):
    """`--main` follows the `main` setting: set unless it is false."""
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    design = Design(
        name="tb",
        design_root=tmp_path,
        rtl={"sources": ["tb.sv"], "top": "tb"},
        tb={"sources": ["main.cpp"], "top": "tb"},
    )
    for settings, expected in (({}, True), ({"main": True}, True), ({"main": False}, False)):
        commands.clear()
        DefaultRunner(
            tmp_path / f"runs-{expected}-{len(settings)}", display_results=False
        ).run_flow(Verilator, design, settings)
        (command,) = [c for c in commands if c[0] == "verilator" and "--top-module" in c]
        assert ("--main" in command) is expected
        assert ("-CFLAGS" in command and "-DXEDA_VERILATOR_MAIN" in command) is expected


def test_cocotb_has_its_own_main_and_the_systemc_model_needs_the_designs(tmp_path):
    (tmp_path / "d.sv").write_text("module d; endmodule\n")
    design = Design(
        name="d", design_root=tmp_path, rtl={"sources": ["d.sv"], "top": "d"}, tb={"top": "d"}
    )
    flow_settings = Verilator.Settings.from_input({"generate_systemc": True}, design_root=tmp_path)
    with pytest.raises(FlowSettingsException, match="`main: false`"):
        Verilator(flow_settings, design, tmp_path / "run").init()
    (tmp_path / "tb_d.py").write_text("")
    cocotb = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["d.sv"], "top": "d"},
        tb={"sources": ["tb_d.py"], "cocotb": True},
    )
    flow_settings = Verilator.Settings.from_input({"main": True}, design_root=tmp_path)
    with pytest.raises(FlowSettingsException, match="cocotb's own main"):
        Verilator(flow_settings, cocotb, tmp_path / "run-cocotb").init()
    flow_settings = Verilator.Settings.from_input({"main": False}, design_root=tmp_path)
    Verilator(flow_settings, cocotb, tmp_path / "run-cocotb-false").init()


def dumping_body(name: str = "dump.vcd") -> str:
    return (
        "reg [3:0] n = 0;\n"
        f'initial begin $dumpfile("{name}"); $dumpvars(0, tb); end\n'
        "always #5 n = n + 1;\n"
        "initial begin #50 $finish; end"
    )


DUMPING_BODY = dumping_body()


def test_the_testbench_writes_the_waveform_that_the_setting_builds_in(tmp_path):
    """`vcd: true` builds the model with VCD tracing; the testbench's `$dumpfile("dump.vcd")`
    writes the file, in the model's working directory (the run directory), and xeda opens none
    of its own."""
    require_verilator()
    flow = _launch(tmp_path, DUMPING_BODY, {"vcd": True})
    assert flow.succeeded
    wave = flow.run_path / "dump.vcd"
    assert wave.read_text().startswith("$version Generated by VerilatedVcd")
    assert "$var" in wave.read_text()


@pytest.mark.parametrize(
    "setting,dumped",
    [("vcd", "tb_wave.vcd"), ("fst", "dump.vcd"), ("vcd", "dump.fst"), ("saif", "dump.vcd")],
)
def test_a_run_that_wrote_no_waveform_of_the_built_format_fails(tmp_path, setting, dumped):
    """The model is built for one format and the testbench dumps under another name: the run
    would pass with no `dump.<format>`, or with data of the other format in `dump.vcd`."""
    require_verilator()
    flow = _launch(tmp_path, dumping_body(dumped), {setting: True})
    assert not flow.succeeded
    assert f'$dumpfile("dump.{setting}")' in flow.results["error"]["message"]
    assert not (flow.run_path / f"dump.{setting}").exists()


def test_a_waveform_of_an_earlier_run_is_not_this_runs(tmp_path):
    require_verilator()
    first = _launch(tmp_path, DUMPING_BODY, {"vcd": True})
    assert first.succeeded and (first.run_path / "dump.vcd").is_file()
    second = _launch(tmp_path, dumping_body("other.vcd"), {"vcd": True})
    assert not second.succeeded


def test_the_testbench_writes_no_waveform_when_no_format_is_set(tmp_path):
    require_verilator()
    flow = _launch(tmp_path, DUMPING_BODY, {})
    assert flow.succeeded
    assert not list(flow.run_path.rglob("*.vcd"))


@pytest.mark.parametrize("main", [True, False], ids=["verilator_main", "own_main"])
@pytest.mark.parametrize("given", ["name", "location"])
def test_a_waveform_name_or_location_is_refused_before_verilator_runs(tmp_path, main, given):
    """Without cocotb, the testbench or the design's main names the waveform file, so a name or
    a location is refused, naming `$dumpfile`, and no model is built."""
    if given == "location":
        value = str(tmp_path / "out" / "w.vcd")
    else:
        value = "w.vcd"
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    sources = ["tb.sv"]
    if not main:
        (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
        sources.append("main.cpp")
    design = Design(
        name="tb",
        design_root=tmp_path,
        rtl={"sources": ["tb.sv"], "top": "tb"},
        tb={"sources": sources, "top": "tb"},
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsException, match=r"`vcd` names a waveform file.*`\$dumpfile`"):
        runner.run_flow(Verilator, design, {"vcd": value, "main": main})
    assert not list((tmp_path / "runs").rglob("sim_build"))


def test_cocotb_names_the_waveform_and_a_location_is_delivered(tmp_path):
    """With cocotb, xeda names the file through cocotb's `--trace-file`: `true` writes
    `dump.vcd`, and a location is delivered."""
    require_verilator()
    require_cocotb()
    (tmp_path / "dut.sv").write_text(
        "module dut(input logic clk, output logic [3:0] q);\n"
        "  always_ff @(posedge clk) q <= q + 1;\n"
        "  initial q = 0;\nendmodule\n"
    )
    (tmp_path / "tb_dut.py").write_text(
        "import cocotb\n"
        "from cocotb.clock import Clock\n"
        "from cocotb.triggers import Timer\n"
        "@cocotb.test()\n"
        "async def count(dut):\n"
        "    cocotb.start_soon(Clock(dut.clk, 10, unit='ns').start())\n"
        "    await Timer(100, 'ns')\n"
    )
    design = Design(
        name="dut",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )
    flow = DefaultRunner(tmp_path / "runs-true", display_results=False).run_flow(
        Verilator, design, {"vcd": True, "timing": True}
    )
    assert flow is not None and flow.succeeded
    assert "$var" in (flow.run_path / "dump.vcd").read_text()
    delivered = tmp_path / "out" / "c.vcd"
    flow = DefaultRunner(tmp_path / "runs-located", display_results=False).run_flow(
        Verilator, design, {"vcd": str(delivered), "timing": True}
    )
    assert flow is not None and flow.succeeded
    assert "$var" in delivered.read_text()


SQRT_DESIGN = EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml"


def test_verilator_refuses_a_vhdl_design_when_planned(tmp_path):
    """Verilator reads Verilog and SystemVerilog. A design with VHDL sources used to be handed to
    it with the VHDL left out, and verilator failed with "No input Verilog file specified". Now
    the plan refuses the design, naming the VHDL source and what Verilator reads."""
    design = Design.from_file(SQRT_DESIGN)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsException) as raised:
        runner.plan("verilator", design)
    message = str(raised.value)
    assert message.startswith("verilator cannot read the design's Vhdl source(s) "), message
    assert str(SQRT_DESIGN.parent / "sqrt.vhdl") in message
    assert "SystemVerilog" in message and "Verilog" in message
    assert not (tmp_path / "xeda_run").exists()


def test_xeda_run_verilator_on_a_vhdl_design_fails_with_the_refusal_not_the_tools_error(tmp_path):
    """The command line reports the refusal, as `FlowSettingsException`, and starts no tool: no
    run directory is made. Before, verilator ran and its exit status was the error
    (`NonZeroExitCode`)."""
    require_verilator()
    run_root = tmp_path / "xeda_run"
    proc = run_xeda(
        "run", "verilator", SQRT_DESIGN.name, "--json", "--run-root", str(run_root),
        cwd=SQRT_DESIGN.parent,
    )  # fmt: skip
    document = json.loads(proc.stdout)
    assert proc.returncode != 0 and document["success"] is False
    assert document["error"]["type"] == "FlowSettingsException"
    assert "cannot read the design's Vhdl source(s)" in document["error"]["message"]
    assert not run_root.exists()


@pytest.fixture
def commands(monkeypatch) -> list[list[str]]:
    """Every command a tool would run, instead of running it."""
    recorded: list[list[str]] = []

    def record(executable, args=None, **kwargs):
        recorded.append([str(executable), *(str(a) for a in args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    return recorded


def test_verilator_is_handed_the_sources_it_reads_in_design_order(tmp_path, commands):
    """The HDL and C++ sources of the RTL, then of the testbench, are on the command line in
    design order. A header is found through its directory (`-I`), and a source of a type that
    Verilator is not given (a constraint file) is not on the command line."""
    files = {
        "inc/defs.vh": "`define W 4\n",
        "rtl/top.v": "module top; endmodule\n",
        "pkg.sv": "package p; endpackage\n",
        "pins.xdc": "# a constraint\n",
        "tb inc/tb_defs.svh": "`define N 3\n",
        "tb.sv": "module tb; endmodule\n",
        "main.cpp": "int main() { return 0; }\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["inc/defs.vh", "rtl/top.v", "pkg.sv", "pins.xdc"], "top": "top"},
        tb={"sources": ["tb inc/tb_defs.svh", "tb.sv", "main.cpp"], "top": "tb"},
    )
    DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(Verilator, design, {})
    (command,) = [c for c in commands if c[0] == "verilator" and "--top-module" in c]
    on_the_line = [a for a in command if a.startswith(str(tmp_path)) and not a.startswith("-")]
    assert on_the_line == [str(tmp_path / f) for f in ("rtl/top.v", "pkg.sv", "tb.sv", "main.cpp")]
    assert [a for a in command if a.startswith("-I")] == [
        f"-I{tmp_path / 'inc'}",
        f"-I{tmp_path / 'tb inc'}",
    ]


DPI_HELPER = """extern "C" int xeda_add(int a, int b) { return a + b; }
"""


def test_a_cpp_source_with_no_main_is_a_dpi_helper_and_links(tmp_path):
    """A `Cpp` source is not taken for a main: Verilator's own main runs the HDL testbench and
    the helper, which the testbench calls through DPI, is linked into the model."""
    require_verilator()
    flow = _launch(
        tmp_path,
        'import "DPI-C" function int xeda_add(input int a, input int b);\n'
        "initial begin\n"
        '  if (xeda_add(2, 3) != 5) $fatal(1, "DPI helper returned the wrong sum");\n'
        "  #10 $finish;\n"
        "end",
        {},
        extra_sources={"helper.cpp": DPI_HELPER},
    )
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"


def test_a_link_left_at_the_coverage_file_is_never_written_through(tmp_path):
    """`--coverage` makes Verilator's own main write `coverage.dat` in the run directory. A link
    at that name, left by an earlier tool, is removed as itself before the model runs: the
    file it leads to, outside the run directory, is not touched."""
    require_verilator()
    body = "reg r = 0; initial begin #5 r = 1; #5 $finish; end"
    settings = {"compile_args": ["--coverage"]}
    flow = _launch(tmp_path, body, settings)
    assert flow.succeeded
    coverage = flow.run_path / "coverage.dat"
    assert coverage.is_file() and not coverage.is_symlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("not xeda's")
    coverage.unlink()
    coverage.symlink_to(outside)
    flow = _launch(tmp_path, body, settings)
    assert flow.succeeded
    assert outside.read_text() == "not xeda's"
    assert coverage.is_file() and not coverage.is_symlink()


def test_a_coverage_file_named_through_a_link_that_leads_out_is_refused(tmp_path):
    """`+verilator+coverage+file+cov/results.dat` names a file in a directory of the run
    directory. When `cov` is a link left by an earlier tool that leads out, the run is refused
    before the model starts, and the file outside is not touched."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    settings = {
        "compile_args": ["--coverage"],
        "model_args": ["+verilator+coverage+file+cov/r.dat"],
    }
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "r.dat").write_text("not xeda's")
    (flow.run_path / "cov").symlink_to(outside)
    with pytest.raises(RunDirectoryError, match="symbolic link out of it"):
        _launch(tmp_path, body, settings)
    # the same file named by its full path, behind the same link
    full = str(flow.run_path / "cov" / "r.dat")
    settings["model_args"] = [f"+verilator+coverage+file+{full}"]
    with pytest.raises(RunDirectoryError, match="symbolic link out of it"):
        _launch(tmp_path, body, settings)
    assert (outside / "r.dat").read_text() == "not xeda's"


@pytest.mark.parametrize(
    "body",
    ['initial $display("done");', "reg clk = 0; always #5 clk = ~clk;"],
    ids=["nothing_to_do", "ignored_delay"],
)
def test_an_untimed_run_that_never_finishes_drains_at_once(tmp_path, body):
    """Without `timing`, Verilator's main leaves its loop when no event is left (a delay is
    ignored): the run drained and fails, and does not wait for the wall-clock `timeout`."""
    require_verilator()
    flow = _launch(tmp_path, body, {"timing": False, "timeout": 60})
    assert flow.results["sim.ended_by"] == "drained"
    assert not flow.succeeded


def test_a_coverage_file_is_found_in_the_run_directory_under_another_letter_case(tmp_path):
    """The run directory is told by file-system identity, not by how its path is spelled: on a
    volume that ignores letter case, the full path of a coverage file under another spelling of
    the run directory is still a file of it, and a link left at that name is removed as itself."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    respelled = flow.run_path.with_name(flow.run_path.name.upper())
    if not respelled.exists():
        pytest.skip("this file system distinguishes letter case")
    outside = tmp_path / "outside.txt"
    outside.write_text("not xeda's")
    (flow.run_path / "cov.dat").symlink_to(outside)
    settings = {
        "compile_args": ["--coverage"],
        "model_args": [f"+verilator+coverage+file+{respelled / 'cov.dat'}"],
    }
    flow = _launch(tmp_path, body, settings)
    assert flow.succeeded
    assert outside.read_text() == "not xeda's"
    assert (flow.run_path / "cov.dat").is_file() and not (flow.run_path / "cov.dat").is_symlink()


def test_a_coverage_file_named_through_an_alias_of_a_subdirectory_is_freed(tmp_path):
    """`/alias/result.dat`, where `alias` is a link to a directory of the run directory, is a file
    of the run directory: a link left at that name is removed as itself."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    (flow.run_path / "cov").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(flow.run_path / "cov")
    outside = tmp_path / "outside.txt"
    outside.write_text("not xeda's")
    (flow.run_path / "cov" / "result.dat").symlink_to(outside)
    settings = {
        "compile_args": ["--coverage"],
        "model_args": [f"+verilator+coverage+file+{alias / 'result.dat'}"],
    }
    flow = _launch(tmp_path, body, settings)
    assert flow.succeeded
    assert outside.read_text() == "not xeda's"
    assert (flow.run_path / "cov" / "result.dat").is_file()


def test_a_coverage_file_named_through_an_alias_in_another_letter_case_is_freed(tmp_path):
    """An alias of a directory of the run directory, made to the run directory's name in another
    letter case, still leads to the run directory by identity: a link left at the file's name is
    removed as itself. Needs a file system that ignores letter case."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    respelled = flow.run_path.with_name(flow.run_path.name.upper())
    if not respelled.exists():
        pytest.skip("this file system distinguishes letter case")
    (flow.run_path / "cov").mkdir()
    alias = tmp_path / "cov-alias"
    alias.symlink_to(respelled / "cov")
    outside = tmp_path / "outside.txt"
    outside.write_text("not xeda's")
    (flow.run_path / "cov" / "result.dat").symlink_to(outside)
    settings = {
        "compile_args": ["--coverage"],
        "model_args": [f"+verilator+coverage+file+{alias / 'result.dat'}"],
    }
    flow = _launch(tmp_path, body, settings)
    assert flow.succeeded
    assert outside.read_text() == "not xeda's"
    assert (flow.run_path / "cov" / "result.dat").is_file()


def test_a_coverage_name_with_dotdot_is_refused(tmp_path):
    """`cov/../r.dat` means a different file when `cov` is a link: the file xeda frees and the
    file the model writes would differ, so the name is refused."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    settings = {
        "compile_args": ["--coverage"],
        "model_args": ["+verilator+coverage+file+cov/../r.dat"],
    }
    with pytest.raises(FlowSettingsException, match=r"has a `\.\.` in it"):
        _launch(tmp_path, body, settings)


def test_a_flow_built_directly_refuses_a_coverage_link_it_may_not_remove(tmp_path):
    """In a directory no launcher chose, xeda deletes nothing. A link at the coverage file's name
    would then be written through by the model, so the run is refused before it starts."""
    (tmp_path / "tb.sv").write_text("module tb; endmodule\n")
    design = Design(name="tb", design_root=tmp_path, rtl={"sources": ["tb.sv"], "top": "tb"})
    run_path = tmp_path / "direct"
    run_path.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not xeda's")
    (run_path / "coverage.dat").symlink_to(outside)
    settings = Verilator.Settings.from_input({"build": False}, design_root=tmp_path)
    flow = Verilator(settings, design, run_path)
    with pytest.raises(FlowFatalError, match="deletes nothing"):
        flow._free_model_outputs([])
    assert outside.read_text() == "not xeda's"
    assert (run_path / "coverage.dat").is_symlink()


def test_a_coverage_file_behind_a_link_that_leads_out_after_an_alias_is_refused(tmp_path):
    """`/alias/out/result.dat`: `alias` is a link to a directory of the run directory, and `out`
    in it is a link to a directory outside. The name is in the run directory, and the link on the
    way leads out, so the run is refused and the file outside is not touched."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    (flow.run_path / "cov").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.dat").write_text("not xeda's")
    (flow.run_path / "cov" / "out").symlink_to(outside)
    alias = tmp_path / "cov-alias"
    alias.symlink_to(flow.run_path / "cov")
    settings = {
        "compile_args": ["--coverage"],
        "model_args": [f"+verilator+coverage+file+{alias / 'out' / 'result.dat'}"],
    }
    with pytest.raises(RunDirectoryError, match="symbolic link out of it"):
        _launch(tmp_path, body, settings)
    assert (outside / "result.dat").read_text() == "not xeda's"


def test_a_directory_named_as_the_coverage_file_is_never_deleted(tmp_path):
    """A coverage name is removed as a file, a link included. A directory of that name is refused,
    with everything in it left in place."""
    require_verilator()
    body = "initial begin #5 $finish; end"
    flow = _launch(tmp_path, body, {"compile_args": ["--coverage"]})
    assert flow.succeeded
    kept = flow.run_path / "cov" / "keep.txt"
    kept.parent.mkdir()
    kept.write_text("mine")
    settings = {
        "compile_args": ["--coverage"],
        "model_args": ["+verilator+coverage+file+cov"],
    }
    with pytest.raises(FlowSettingsException, match="is a directory"):
        _launch(tmp_path, body, settings)
    assert kept.read_text() == "mine"
