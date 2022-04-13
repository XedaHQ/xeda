import os
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import IseSynth
from xeda.flows.flow import FPGA

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

os.environ["PATH"] += os.pathsep + os.path.join(TESTS_DIR, "fake_tools")


def test_ise_synth_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    # Append to PATH so if the actual tool exists, would take precedences.
    assert path.exists()
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(IseSynth, design, settings)
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        # assert flow.succeeded


if __name__ == "__main__":
    test_ise_synth_py()
