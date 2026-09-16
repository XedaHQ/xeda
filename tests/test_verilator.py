#!/usr/bin/env python3
import tempfile
from pathlib import Path

from xeda.flow_runner import DefaultRunner
from xeda.flows import Verilator

from .tool_utils import require_verilator

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_verilator_sim_py() -> None:
    require_verilator()
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


if __name__ == "__main__":
    test_verilator_sim_py()
