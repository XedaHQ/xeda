import tempfile
from pathlib import Path

from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_yosys_synth_py() -> None:
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "pipeline" / "pipelined_adder.toml",
    ]
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        for design in design_paths:
            xeda_runner = DefaultRunner(run_dir, debug=debug)
            flow = xeda_runner.run(GhdlSim, design, flow_overrides=dict(debug=debug, verbose=debug))
            assert flow is not None, "run_flow returned None"
            settings_json = flow.run_path / "settings.json"
            results_json = flow.run_path / "results.json"
            assert settings_json.exists()
            assert flow.succeeded
            assert isinstance(flow.settings, GhdlSim.Settings)
            assert results_json.exists()


if __name__ == "__main__":
    test_yosys_synth_py()
