"""Real CXXRTL smoke test and translation of CXXRTL backend options."""

from pathlib import Path

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
        lstrip_blocks=True,
        trim_blocks=False,
    )
    text = (tmp_path / script).read_text()
    assert (
        "write_cxxrtl -header -noflatten -nohierarchy -noproc -g1 -O3 -namespace sim sim.cpp"
        in text
    )
