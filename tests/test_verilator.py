#!/usr/bin/env python3
import tempfile
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Verilator

from .tool_utils import require_cocotb, require_verilator

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_verilator_sim_py() -> None:
    require_verilator()
    require_cocotb()
    design_paths = [
        EXAMPLES_DIR / "sv" / "fifo" / "fifo.xeda.yaml",
        EXAMPLES_DIR / "sv" / "fifo" / "fifo_cocotb.xeda.yaml",
    ]
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        for design in design_paths:
            xeda_runner = DefaultRunner(run_dir, debug=debug)
            flow = xeda_runner.run(
                Verilator, design, flow_overrides=dict(debug=debug, verbose=debug)
            )
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
        "    await Timer(1, unit='ns')\n"
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


if __name__ == "__main__":
    test_verilator_sim_py()
