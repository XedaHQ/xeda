"""test Intel Quartus flow"""

import logging
import tempfile
from pathlib import Path

from xeda import Design
from xeda.flow import FPGA
from xeda.flow_runner import DefaultRunner
from xeda.flows import Quartus
from xeda.flows.quartus import parse_csv, try_num
from xeda.tool import Tool

from .tool_utils import fake_calls, use_fake_tools

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
        },
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
    design = Design.from_file(root_dir / "pipelined_adder.yaml")
    run_path = root_dir / "xeda_run/pipelined_adder/quartus"
    settings = Quartus.Settings(fpga={"part": "10CL016YU256C6G"}, clock={"period": 15})  # type: ignore
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


def test_quartus_records_bitstream_as_artifact(tmp_path, monkeypatch) -> None:
    """`execute_flow -compile` runs the assembler, which writes `<project>.sof` into the project
    directory; `Quartus` never declared it. The project is `<design name>`, created in the run
    directory with no output directory of its own."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    settings = dict(fpga=FPGA("10CL016YU256C6G"), clock=dict(period=6.0))
    flow = DefaultRunner(tmp_path / "run").run_flow(Quartus, design, settings)
    assert flow is not None and flow.succeeded
    calls = fake_calls(flow.run_path)
    assert ["project_new", design.name, "-overwrite"] in calls
    assert ["execute_flow", "-compile"] in calls
    assert not any("PROJECT_OUTPUT_DIRECTORY" in arg for call in calls for arg in call)
    assert (flow.run_path / "fake_quartus_sh.calls").exists(), "the project is in the run directory"
    assert flow.artifacts.bitstream == f"{design.name}.sof"


def _quartus_processes(tmp_path, monkeypatch, **settings) -> list:
    """The processes a launch of `quartus` starts, as (program, arguments): every start is
    recorded and none is made, so what is judged is where the flow sends its tool."""
    started: list = []

    def record(executable, args=(), **kwargs):
        started.append((executable, [str(a) for a in args]))
        return ""

    monkeypatch.setattr("xeda.tool.run_process", record)
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
        Quartus, design, dict(fpga=FPGA("10CL016YU256C6G"), clock=dict(period=6.0), **settings)
    )
    assert flow is not None and flow.quartus_sh is not None
    return started


def test_quartus_runs_its_tool_in_the_container_when_the_flow_is_dockerized(
    tmp_path, monkeypatch
) -> None:
    """`dockerized` reaches `quartus_sh`: the tool is the flow's, made for its settings."""
    started = _quartus_processes(tmp_path, monkeypatch, dockerized=True)
    assert not [s for s in started if s[0] == "quartus_sh"], "quartus_sh ran natively"
    in_container = [
        args for program, args in started if program == "docker" and "quartus_wrapper" in args
    ]
    assert in_container and all("alterafpga/quartuspro-v25.3:20.1.0" in a for a in in_container)
    assert any("-t" in args for args in in_container)


def test_quartus_runs_its_tool_natively_when_the_flow_is_not_dockerized(
    tmp_path, monkeypatch
) -> None:
    started = _quartus_processes(tmp_path, monkeypatch, dockerized=False)
    assert [args for program, args in started if program == "quartus_sh"]
    assert not [s for s in started if s[0] == "docker"]


def test_every_launch_of_quartus_has_its_own_tool(tmp_path, monkeypatch) -> None:
    """A tool holds what it learned about the program (its version, a container's CPU count): one
    shared by every launch of the process would carry that from launch to launch."""
    assert not isinstance(vars(Quartus).get("quartus_sh"), Tool)
    monkeypatch.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    settings = dict(fpga=FPGA("10CL016YU256C6G"), clock=dict(period=6.0))
    first = DefaultRunner(tmp_path / "one", display_results=False).launch_flow(
        Quartus, design, settings
    )
    second = DefaultRunner(tmp_path / "two", display_results=False).launch_flow(
        Quartus, design, settings
    )
    assert first.quartus_sh is not second.quartus_sh


def test_quartus_synth_py(monkeypatch) -> None:
    path = RESOURCES_DIR / "design0/design0.toml"
    use_fake_tools(monkeypatch)
    assert path.exists()
    design = Design.from_file(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.yaml")
    settings = dict(fpga=FPGA("10CL016YU256C6G"), clock={"period": 6}, dockerized=False)
    with tempfile.TemporaryDirectory() as run_dir:
        print("Xeda run dir: ", run_dir)
        xeda_runner = DefaultRunner(run_dir, debug=True)
        flow = xeda_runner.run_flow(Quartus, design, settings)
        assert flow is not None, "run_flow returned None"
        settings_json = flow.run_path / "settings.json"
        results_json = flow.run_path / "results.json"
        assert settings_json.exists()
        assert results_json.exists()
        assert flow.succeeded


if __name__ == "__main__":
    _test_parse_reports()
    # test_quartus_synth_py()
