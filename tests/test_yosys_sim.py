"""Real CXXRTL smoke test and translation of CXXRTL backend options."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows.yosys.cxx_rtl import YosysSim

from .tool_utils import require_c_toolchain, require_yosys


def test_yosys_sim_runs_the_cxxrtl_example(tmp_path):
    require_yosys()
    require_c_toolchain()
    example = Path(__file__).parent.parent / "examples/mixed_language/blink/blinky.xeda.toml"
    design = Design.from_file(example)
    flow = DefaultRunner(tmp_path).run_flow(YosysSim, design, {"cxxrtl": {"filename": None}})
    assert flow is not None and flow.succeeded
    assert (flow.run_path / "blink.cpp").is_file()
    assert (flow.run_path / "blink.h").is_file()
    assert (flow.run_path / "blink").is_file()
    assert flow.artifacts["cxxrtl_cpp"] == Path("blink.cpp")
    assert flow.settings.cxxrtl.filename is None
    assert "netlist_json" not in flow.artifacts


def test_cxxrtl_backend_settings_are_rendered(tmp_path):
    design = Design(name="d", rtl={"sources": [], "top": "d"})
    settings = YosysSim.Settings(
        cxxrtl={
            "filename": "sim.cpp",
            "header": True,
            "flatten": False,
            "hierarchy": False,
            "proc": False,
            "debug": 1,
            "opt": 3,
            "namespace": "sim",
        }
    )
    flow = YosysSim(settings, design, tmp_path)
    flow.init()
    script = flow.copy_from_template(
        "yosys_sim.ys",
        ghdl_args=[],
        parameters={},
        defines=[],
        cxxrtl_filename=settings.cxxrtl.filename,
        lstrip_blocks=True,
        trim_blocks=False,
    )
    text = (tmp_path / script).read_text()
    assert (
        "write_cxxrtl -header -noflatten -nohierarchy -noproc -g1 -O3 -namespace sim sim.cpp"
        in text
    )


def test_flatten_is_a_separate_command_in_both_script_formats(tmp_path):
    design = Design(name="d", rtl={"sources": [], "top": "d"})
    flow = YosysSim(YosysSim.Settings(flatten=True), design, tmp_path)
    flow.init()
    for template, command in (
        ("yosys_sim.ys", "flatten"),
        ("yosys_sim.tcl", "yosys flatten"),
    ):
        script = flow.copy_from_template(
            template,
            ghdl_args=[],
            parameters={},
            defines=[],
            cxxrtl_filename="d.cpp",
            lstrip_blocks=True,
            trim_blocks=False,
        )
        lines = [line.strip() for line in (tmp_path / script).read_text().splitlines()]
        assert command in lines
        assert any(line.startswith(command.replace("flatten", "check")) for line in lines)
        assert f"{command.replace('flatten', 'check')} -initdrv -assert" in lines
        assert any(line.endswith(" d.cpp") and "write_cxxrtl" in line for line in lines)


@pytest.mark.parametrize("check_assert", [True, False])
def test_invalid_init_driver_fails_before_cxxrtl(tmp_path, check_assert):
    require_yosys()
    (tmp_path / "bad.v").write_text(
        "module top(input a, output b); "
        "(* init = 1'b0 *) wire w; assign w = a; assign b = w; endmodule\n"
    )
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n")
    design = Design(
        name="bad_init_driver",
        design_root=tmp_path,
        rtl={"sources": ["bad.v"], "top": "top"},
        tb={"sources": ["main.cpp"]},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        YosysSim, design, {"check_assert": check_assert, "cxxrtl": {"filename": "sim.cpp"}}
    )
    assert flow is not None and not flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert "has 'init' attribute and is not driven by an FF cell" in log
    assert "ERROR: Found 1 problems in 'check -assert'" in log
    assert not (flow.run_path / "sim.cpp").exists()


def test_simulation_top_and_hdl_testbench_source_are_used(tmp_path, capfd):
    require_yosys()
    require_c_toolchain()
    (tmp_path / "dut.v").write_text("module dut(input a, output y); assign y = a; endmodule\n")
    (tmp_path / "sim_top.v").write_text(
        "module sim_top(input a, output y); dut u(.a(a), .y(y)); endmodule\n"
    )
    # The general Yosys include root is needed by older generated CXXRTL models and can also
    # be used by testbenches; the current generated model exercises the newer runtime root.
    (tmp_path / "sim_main.cpp").write_text(
        '#include <libs/sha1/sha1.h>\n#include "model.h"\n'
        "int main() { cxxrtl_design::p_sim__top top; return 0; }\n"
    )
    design = Design(
        name="sim_top_example",
        design_root=tmp_path,
        rtl={"sources": ["dut.v"], "top": "dut"},
        tb={"sources": ["sim_top.v", "sim_main.cpp"], "top": "sim_top"},
    )
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        YosysSim, design, {"log_file": None, "cxxrtl": {"filename": "model.cpp"}}
    )
    assert flow is not None and flow.succeeded
    assert "struct p_sim__top : public module" in (flow.run_path / "model.h").read_text()
    assert "Executing CHECK pass" in capfd.readouterr().out
    assert not (flow.run_path / "yosys.log").exists()
