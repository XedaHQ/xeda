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
        ("initial begin $finish; end", {}, "finish", True),  # untimed, $finish at 0 (Q16)
        ("reg clk; initial clk = 0;", {}, "drained", False),  # nothing ever ends it
        ("initial begin #10; #10; end", {"timing": True}, "drained", False),
        (
            "initial begin #10; #10; end",
            {"timing": True, "stop_time": "15ns"},
            "stop_time",
            True,
        ),
        ('initial begin #5 $error("e"); #5 $finish; end', {"timing": True}, "fatal", False),
        pytest.param(
            'initial begin #5 $error("e"); #5 $finish; end',
            {"timing": True, "fail_severity": "failure"},
            "finish",
            True,
            marks=pytest.mark.xfail(strict=True, reason="Task 8"),
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
            marks=pytest.mark.xfail(strict=True, reason="Task 8"),
        ),
    ],
)
def test_verilator_passes_only_on_evidence(tmp_path, body, settings, ended_by, success):
    require_verilator()
    flow = _launch(tmp_path, body, settings)
    assert flow.results["sim.ended_by"] == ended_by
    assert flow.succeeded is success


def test_verilator_simulates_the_designs_testbench_top(tmp_path):
    """Two top-level candidates: the design's `tb.top` is the one simulated (M5)."""
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


if __name__ == "__main__":
    test_verilator_sim_py()
