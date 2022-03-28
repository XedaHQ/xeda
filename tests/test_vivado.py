import os
from pathlib import Path
import tempfile
from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoPrjSynth
from xeda.flows.flow import FPGA

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
# sys.path.insert(0, TESTS_DIR)


def test_vivado_prj_template() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    assert path.exists()
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = VivadoPrjSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5)  # type: ignore
    run_dir = Path.cwd() / "vivado_prj_run"
    run_dir.mkdir(exist_ok=True)
    flow = VivadoPrjSynth(settings, design, run_dir)  # type: ignore
    tcl_file = flow.copy_from_template(
        "vivado_project.tcl", xdc_files=[], reports_tcl="reports_tcl"
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


def test_vivado_prj_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    os.environ["PATH"] += os.pathsep + os.path.join(TESTS_DIR, "fake_tools")
    assert path.exists()
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = dict(fpga=FPGA(part="abcd"), clock_period=5.5)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(VivadoPrjSynth, design, settings)
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        # print(flow.results)
        assert flow.results["wns"] == 0.046
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded
        assert 0.3 < flow.results.runtime < 3


if __name__ == "__main__":
    test_vivado_prj_py()
