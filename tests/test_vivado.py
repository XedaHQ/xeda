import json
import os
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth
from xeda.flow import FPGA
from xeda.flows.vivado.vivado_synth import parse_hier_util, vivado_synth_generics

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
        "vivado_synth.tcl",
        xdc_files=[],
        tcl_files=[],
        generics=vivado_synth_generics(design.rtl.parameters),
    )
    with open(run_dir / tcl_file) as f:
        vivado_tcl = f.read()
    expected_lines = [
        """set_property generic {G_IN_WIDTH=32 G_ITERATIVE=1'b1 G_STR=\\"abcd\\" G_BITVECTOR=7'b0101001} [current_fileset]""",
    ]
    for line in expected_lines:
        assert line in vivado_tcl


def test_vivado_synth_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    # Append to PATH so if the actual tool exists, would take precedences.
    os.environ["PATH"] = os.path.join(TESTS_DIR, "fake_tools") + os.pathsep + os.environ.get("PATH", "")
    assert path.exists()
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(VivadoSynth, design, settings)
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded
        assert 0.3 < flow.results.runtime  # type: ignore


def test_parse_hier_util() -> None:
    d = parse_hier_util("tests/resources/vivado_synth/hierarchical_utilization.xml")
    # print(json.dumps(d, indent=2))
    with open("hier.json", "w") as f:
        json.dump(d, f, indent=4)
    assert d


if __name__ == "__main__":
    # test_vivado_synth_py()
    test_parse_hier_util()
