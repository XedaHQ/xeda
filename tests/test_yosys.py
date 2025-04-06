import json
import os
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga
from xeda.flow import FPGA

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_yosys_synth_py() -> None:
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.xeda.yaml",
        EXAMPLES_DIR / "boards" / "ulx3s" / "blinky" / "blinky_vhdl.xeda.yaml",
    ]
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        for design in design_paths:
            xeda_runner = DefaultRunner(run_dir, debug=True)
            flow = xeda_runner.run(YosysFpga, design, flow_overrides=dict(debug=True, verbose=True))
            assert flow is not None, "run_flow returned None"
            settings_json = flow.run_path / "settings.json"
            results_json = flow.run_path / "results.json"
            assert settings_json.exists()
            assert flow.succeeded
            assert isinstance(flow.settings, YosysFpga.Settings)
            assert flow.settings.fpga is not None
            if flow.settings.fpga.vendor == "xilinx":
                assert flow.results.LUT > 1
                assert flow.results.FF > 1
            assert results_json.exists()


if __name__ == "__main__":
    test_yosys_synth_py()
