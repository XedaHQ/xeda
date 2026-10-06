#!/usr/bin/env python3
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Verilator

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
        (
            "initial begin #10; #10; end",
            {"timing": True, "stop_time": "15ns"},
            "stop_time",
            True,
        ),
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
        {"timing": True},
        extra_sources={"main.cpp": DIRECT_STOP_DRIVER},
    )
    assert flow.results["sim.ended_by"] == "error"
    assert [e["kind"] for e in flow.results["sim.evidence"]["events"]] == ["stop"]
    assert not flow.succeeded


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


def test_a_design_with_its_own_driver_and_hdl_sources_needs_no_testbench_top(tmp_path):
    """HDL among the testbench's sources (a checker bound into the design) does not make a design
    with a C++ driver of its own ask for `tb.top`: the driver runs the RTL top."""
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
    flow = DefaultRunner(tmp_path / "runs", display_results=False).run_flow(
        Verilator, design, {"timing": True}
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.ended_by"] == "exit"


@pytest.mark.parametrize("timescale", ["1ns/1ns", "1ns/1ps", "1ns/1fs", "1us/100ps"])
def test_verilator_stops_at_stop_time_in_the_models_precision(tmp_path, timescale):
    """The driver converts `stop_time` into ticks of the model's time precision, coarser or
    finer than the picoseconds it is given in, and the record says which precision it is."""
    require_verilator()
    flow = _launch(
        tmp_path,
        "initial begin #10ns; #10ns; end",
        {"timing": True, "stop_time": "15ns"},
        timescale=timescale,
    )
    assert flow.results["sim.ended_by"] == "stop_time"
    assert flow.results["sim.time_unit"] == timescale.split("/")[1]
    assert flow.succeeded


@pytest.mark.parametrize(
    ("settings", "ended_by", "error"),
    [
        ({"timing": True, "stop_time": "1us"}, "stop_time", None),
        ({"timing": True, "timeout": 2}, None, "ProcessTimeout"),
    ],
)
def test_verilator_ends_a_simulation_that_never_finishes(tmp_path, settings, ended_by, error):
    """A free-running clock never drains: the requested `stop_time` ends it (a pass), or else
    the wall-clock `timeout` stops the model, which leaves no end record (a failure)."""
    require_verilator()
    flow = _launch(tmp_path, "reg clk = 0; always #5 clk = ~clk;", settings)
    assert flow.results.get("sim.ended_by") == ended_by
    assert (flow.results.get("error") or {}).get("type") == error
    assert flow.succeeded is (error is None)


@pytest.mark.parametrize("own_driver", [False, True], ids=["xeda_driver", "own_driver"])
def test_a_timed_out_model_s_output_reaches_the_log(tmp_path, own_driver):
    """The model's output is copied through a pipe, which would leave the C runtime's stdout
    block-buffered: what a testbench printed before it hung would be lost when the time limit
    kills it. xeda's hooks make it line-buffered, in its own driver and a design's alike."""
    require_verilator()
    flow = _launch(
        tmp_path,
        'reg clk = 0; always #5 clk = ~clk; initial $display("printed before the hang");',
        {"timing": True, "timeout": 3},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", "0")} if own_driver else None,
    )
    assert (flow.results.get("error") or {}).get("type") == "ProcessTimeout"
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
    """A design with its own C++ driver keeps it; xeda's hooks, linked into it, record its end:
    the driver's exit (with the status it returned), or a fatal error."""
    require_verilator()
    flow = _launch(
        tmp_path,
        body,
        {"timing": True},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", str(status))},
    )
    assert flow.results["sim.ended_by"] == ended_by
    assert flow.succeeded is success


@pytest.mark.parametrize("own_driver", [False, True], ids=["xeda_driver", "own_driver"])
def test_a_failed_model_keeps_its_warning_evidence(tmp_path, own_driver):
    """A failed model's log still supplies warnings to its failure document."""
    require_verilator()
    ending = "$finish" if own_driver else '$fatal(1, "failed")'
    flow = _launch(
        tmp_path,
        f'initial begin #5 $warning("before failure"); #5 {ending}; end',
        {"timing": True, "fail_severity": "warning"},
        extra_sources={"main.cpp": OWN_DRIVER.replace("STATUS", "3")} if own_driver else None,
    )
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "NonZeroExitCode"
    assert flow.results["sim.warnings"] == 1


if __name__ == "__main__":
    test_verilator_sim_py()


def test_verilator_defaults_do_not_randomize(tmp_path):
    from xeda.flows.verilator import MIN_VERILATOR_VERSION

    ss = Verilator.Settings()
    assert ss.random_init is False and ss.x_initial == "0" and ss.x_assign == "0"
    assert ss.fail_severity == "error"
    assert MIN_VERILATOR_VERSION == (5, 24)


def test_the_model_arguments_are_not_written_into_the_settings(tmp_path):
    """`--trace`, the seed and xeda's own arguments are the run's, not the user's."""
    require_verilator()
    flow = _launch(
        tmp_path,
        "initial begin #1 $finish; end",
        {"timing": True, "vcd": "w.vcd", "random_init": True},
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


def test_stop_time_is_refused_where_it_cannot_be_enforced(tmp_path):
    from xeda.flow import FlowSettingsException

    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "tb_dut.py").write_text("")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    cases = {
        "cocotb": ({"sources": ["tb_dut.py"], "cocotb": True}, "with cocotb"),
        "cpp": ({"sources": ["main.cpp"]}, "design.s own C\\+\\+ driver"),
    }
    for name, (tb, text) in cases.items():
        design = Design(
            name=name, design_root=tmp_path, rtl={"sources": ["dut.sv"], "top": "dut"}, tb=tb
        )
        settings = Verilator.Settings.from_input({"stop_time": "10ns"}, design_root=tmp_path)
        flow = Verilator(settings, design, tmp_path / "run")
        with pytest.raises(FlowSettingsException, match=text):
            flow.init()
