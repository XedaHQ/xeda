import os
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth
from xeda.flows.flow import FPGA

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


def test_vivado_synth_template() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    assert path.exists()
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = VivadoSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5)  # type: ignore
    run_dir = Path.cwd() / "vivado_synth_run"
    run_dir.mkdir(exist_ok=True)
    flow = VivadoSynth(settings, design, run_dir)  # type: ignore
    tcl_file = flow.copy_from_template(
        "vivado_synth.tcl", xdc_files=[], reports_tcl="reports_tcl"
    )
    with open(run_dir / tcl_file) as f:
        vivado_tcl = f.read()
    expected_lines = [
        "set_property generic {G_IN_WIDTH=32} [current_fileset]",
        "set_property generic {G_ITERATIVE=1'b1} [current_fileset]",
        'set_property generic {G_STR=\\"abcd\\"} [current_fileset]',
        "set_property generic {G_BITVECTOR=7'b0101001} [current_fileset]",
    ]
    for line in expected_lines:
        assert line in vivado_tcl


def test_vivado_synth_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    # Append to PATH so if the actual tool exists, would take precedences.
    os.environ["PATH"] += os.pathsep + os.path.join(TESTS_DIR, "fake_tools")
    assert path.exists()
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(VivadoSynth, design, settings)
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded
        assert 0.3 < flow.results.runtime  # type: ignore


if __name__ == "__main__":
    test_vivado_synth_py()
