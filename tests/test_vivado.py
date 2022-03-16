from pathlib import Path
from xeda import Design
from xeda.flows import VivadoPrjSynth
from xeda.flows.flow import FPGA
from . import RESOURCES_DIR


def test_vivado_prj_template():
    path = RESOURCES_DIR / "design0/design0.toml"
    assert path.exists()
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    settings = VivadoPrjSynth.Settings(fpga=FPGA(part="abcd"), clock_period=5.5)
    run_dir = Path.cwd() / "vivado_prj_run"
    run_dir.mkdir(exist_ok=True)
    flow = VivadoPrjSynth(settings, design, run_dir)
    tcl_file = flow.copy_from_template("vivado_project.tcl", xdc_files=[], reports_tcl='reports_tcl')
    with open(run_dir / tcl_file) as f:
        vivado_tcl = f.read()
    expected_lines = [
        "set_property generic {G_IN_WIDTH=32} [current_fileset]",
        "set_property generic {G_ITERATIVE=1'b1} [current_fileset]",
        "set_property generic {G_STR=\\\"abcd\\\"} [current_fileset]",
        "set_property generic {G_BITVECTOR=7'b0101001} [current_fileset]"
    ]
    for l in expected_lines:
        assert l in vivado_tcl
