"""test Intel Quartus flow"""
import os
import tempfile
import logging
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Quartus
from xeda.flows.flow import FPGA
from xeda.flows.quartus import parse_csv, try_num

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"
EXAMPLES_DIR = TESTS_DIR.parent / "examples"


log = logging.getLogger(__name__)

log.root.setLevel(logging.DEBUG)
log.setLevel(logging.DEBUG)


def test_parse_csv():
    resources = parse_csv(
        RESOURCES_DIR / "Fitter_Resource_Utilization_by_Entity.csv",
        id_field="Compilation Hierarchy Node",
        field_parser=lambda s: try_num(s.split()[0]),
        id_parser=lambda s: s.strip().lstrip("|"),
        # interesting_fields=None
        interesting_fields={
            "Logic Cells",
            "LUT-Only LCs",
            "Register-Only LCs",
            "LUT/Register LCs",
            "Dedicated Logic Registers",
            "ALMs needed [=A-B+C]",
            "Combinational ALUTs",
            "ALMs used for memory",
            "Memory Bits",
            "M10Ks",
            "M9Ks",
            "DSP Elements",
            "DSP Blocks",
            "Block Memory Bits",
            "Pins",
            "I/O Registers",
        }
        # ['Logic Cells', 'Memory Bits', 'M10Ks', 'M9Ks', 'DSP Elements', 'ALMs needed [=A-B+C]',
        #                     'Combinational ALUTs', 'ALMs used for memory', 'DSP Blocks', 'Pins'
        #                     'LUT-Only LCs',	'Register-Only LCs', 'LUT/Register LCs', 'Block Memory Bits']
    )
    assert resources == {
        "full_adder_piped": {
            "ALMs needed [=A-B+C]": 1.5,
            "ALMs used for memory": 0.0,
            "Block Memory Bits": 0,
            "Combinational ALUTs": 3,
            # 'Compilation Hierarchy Node': '|full_adder_piped',
            "DSP Blocks": 0,
            "Dedicated Logic Registers": 2,
            # 'Entity Name': 'full_adder_piped',
            # 'Full Hierarchy Name': '|full_adder_piped',
            "I/O Registers": 0,
            # 'Library Name': 'work',
            "M10Ks": 0,
            "Pins": 7,
            # 'Virtual Pins': 0,
            # '[A] ALMs used in final placement': 1.5,
            # '[B] Estimate of ALMs recoverable by dense packing': 0.0,
            # '[C] Estimate of ALMs unavailable': 0.0
        },
    }


def _test_parse_reports():
    root_dir = Path("/Users/kamyar/src/xeda/examples/vhdl/pipeline")
    design = Design.from_toml(root_dir / "pipelined_adder.toml")
    run_path = root_dir / "xeda_run/pipelined_adder/quartus"
    settings = Quartus.Settings(fpga={"part": "10CL016YU256C6G"}, clock_period=15)  # type: ignore
    flow = Quartus(settings, design, run_path=run_path)
    flow.init()
    flow.parse_reports()
    print(flow.results)


def test_parse_csv_no_header():
    parsed = parse_csv(RESOURCES_DIR / "Flow_Summary.csv", None)
    expected = {
        "Flow Status": "Successful - Tue Mar  1 11:10:35 2022",
        "Quartus Prime Version": "21.1.0 Build 842 10/21/2021 SJ Lite Edition",
        "Revision Name": "pipelined_adder",
        "Top-level Entity Name": "full_adder_piped",
        "Family": "Cyclone V",
        "Device": "5CGXBC3B6F23C7",
        "Timing Models": "Final",
        "Total registers": "2",
        "Total pins": "7 / 222 ( 3 % )",
        "Total virtual pins": "0",
        "Total DSP Blocks": "0 / 57 ( 0 % )",
        "Total HSSI RX PCSs": "0 / 3 ( 0 % )",
        "Total HSSI PMA RX Deserializers": "0 / 3 ( 0 % )",
        "Total HSSI TX PCSs": "0 / 3 ( 0 % )",
        "Total HSSI PMA TX Serializers": "0 / 3 ( 0 % )",
        "Total PLLs": "0 / 7 ( 0 % )",
        "Total DLLs": "0 / 3 ( 0 % )",
    }

    for k, v in expected.items():
        assert parsed[k] == v


def prepend_to_path(path):
    current_path = os.environ.get("PATH", "").split(os.pathsep)
    current_path.insert(0, str(path))
    os.environ["PATH"] = os.pathsep.join(current_path)


def test_quartus_synth_py() -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    # Append to PATH so if the actual tool exists, would take precedences.
    dockerized = True
    if "PYTEST_CURRENT_TEST" in os.environ:
        prepend_to_path(TESTS_DIR / "fake_tools")
        dockerized = False
    assert path.exists()
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    settings = dict(fpga=FPGA("10CL016YU256C6G"), clock_period=6, dockerized=dockerized)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(Quartus, design, settings)
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded


if __name__ == "__main__":
    _test_parse_reports()
    # test_quartus_synth_py()
