"""A simulation of a design whose testbench is written in a hardware description language needs
`tb.top`: without it the simulator would silently simulate `rtl.top`, which has no stimulus, and
report a run that drained.

The rule is `SimFlow.check_design_supported`, made at planning (before anything is set up) and for
a flow constructed directly. It keys on the testbench's sources in those languages
(`design.LANGUAGE_TYPES`): cocotb's, a design's own C++ driver and a design with no testbench at
all name no such top to be missed. A simulator that knows what to run without `tb.top`
(`SimFlow.runs_without_testbench_top`) is not refused: GHDL finds a VHDL testbench's top, and
Verilator and `yosys_sim` run a C++ driver of the design's own, whatever HDL sources the testbench
also has (a bound checker, a model).
"""

import re
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.design import LANGUAGE_TYPES
from xeda.flow import FlowException, SimFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flows import Verilator

SIM_FLOW_NAMES = sorted(
    {
        cls.name
        for _, cls in registered_flows.values()
        if issubclass(cls, SimFlow) and cls.__module__.startswith("xeda.flows")
    }
)

#: a testbench source of each language a design can be written in (`design.LANGUAGE_TYPES`)
HDL_TESTBENCHES = {
    "verilog": ("tb.v", "module tb; endmodule\n"),
    "systemverilog": ("tb.sv", "module tb; endmodule\n"),
    "vhdl": ("tb.vhd", "entity tb is end entity;\n"),
    "bluespec": ("tb.bsv", "package tb; endpackage\n"),
    "chisel": ("tb.sc", "object Tb\n"),
}
#: a design's own C++ driver
DRIVER = ("main.cpp", "int main() { return 0; }\n")


def _design(tmp_path: Path, *testbench: str, **tb) -> Design:
    """A design whose testbench sources are the named `HDL_TESTBENCHES` and, for "driver", a C++
    driver; none: no testbench sources."""
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    sources = []
    for kind in testbench:
        name, text = DRIVER if kind == "driver" else HDL_TESTBENCHES[kind]
        (tmp_path / name).write_text(text)
        sources.append(name)
    return Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={**({"sources": sources} if sources else {}), **tb},
    )


def _in_new_directory(tmp_path: Path, name: str) -> Path:
    (tmp_path / name).mkdir()
    return tmp_path / name


def test_the_sweep_covers_every_language(tmp_path):
    """A language added to `LANGUAGE_TYPES` needs a testbench source here."""
    types = set()
    for language in HDL_TESTBENCHES:
        design = _design(_in_new_directory(tmp_path, language), language)
        types.update(src.type for src in design.tb.sources)
    assert types == LANGUAGE_TYPES


def test_the_sweep_covers_the_simulators():
    assert {"ghdl_sim", "nvc", "verilator", "modelsim", "vcs", "yosys_sim", "bsc_sim"} <= set(
        SIM_FLOW_NAMES
    )


@pytest.mark.parametrize("language", sorted(HDL_TESTBENCHES))
def test_the_simulators_that_run_without_a_testbench_top(language, tmp_path):
    """Pinned, so that another one is a decision: GHDL finds the top of a VHDL testbench itself,
    and a design's own C++ driver runs the model for Verilator and `yosys_sim`."""

    def running(*testbench: str) -> set[str]:
        design = _design(_in_new_directory(tmp_path, "-".join(testbench)), *testbench)
        return {
            name
            for name in SIM_FLOW_NAMES
            if get_flow_class(name).runs_without_testbench_top(design)
        }

    assert running(language) == ({"ghdl_sim"} if language == "vhdl" else set())
    with_driver = {"verilator", "yosys_sim"} | ({"ghdl_sim"} if language == "vhdl" else set())
    assert running(language, "driver") == with_driver


@pytest.mark.parametrize("flow_name", SIM_FLOW_NAMES, ids=str)
@pytest.mark.parametrize("language", sorted(HDL_TESTBENCHES))
@pytest.mark.parametrize("driver", [False, True], ids=["no_driver", "own_driver"])
def test_every_simulator_refuses_an_hdl_testbench_without_a_top(
    flow_name, language, driver, tmp_path
):
    flow_class = get_flow_class(flow_name)
    design = _design(tmp_path, language, *(["driver"] if driver else []))
    assert not design.tb.top
    if flow_class.runs_without_testbench_top(design):
        flow_class.check_design_supported(design)
        return
    with pytest.raises(FlowException, match=re.escape("`tb.top`")):
        flow_class.check_design_supported(design)


@pytest.mark.parametrize("flow_name", SIM_FLOW_NAMES, ids=str)
def test_every_simulator_accepts_what_names_no_hdl_top_to_miss(flow_name, tmp_path):
    flow_class = get_flow_class(flow_name)
    # the testbench's top is named
    named = _design(_in_new_directory(tmp_path, "named"), "systemverilog", top="tb")
    flow_class.check_design_supported(named)
    # no testbench at all: the RTL top is simulated by design
    flow_class.check_design_supported(_design(_in_new_directory(tmp_path, "bare")))
    # a design's own C++ driver, which has no top
    flow_class.check_design_supported(_design(_in_new_directory(tmp_path, "cpp"), "driver"))
    if flow_class.cocotb_sim_name:
        # a cocotb testbench drives the RTL top unless it names another toplevel
        cocotb = _design(_in_new_directory(tmp_path, "cocotb"), "systemverilog", cocotb=True)
        flow_class.check_design_supported(cocotb)


def test_a_refused_launch_sets_up_no_run_directory(tmp_path):
    """Refused at launch, before anything is set up: not a run directory, not even the run root
    (created lazily, on first use)."""
    design = _design(tmp_path, "systemverilog")
    run_dir = tmp_path / "xeda_run"
    with pytest.raises(FlowException, match=re.escape("`tb.top`")):
        DefaultRunner(run_dir, display_results=False).run_flow(Verilator, design)
    assert not run_dir.exists()


def test_the_refusal_names_the_flow_and_what_to_write(tmp_path):
    design = _design(tmp_path, "systemverilog")
    with pytest.raises(FlowException) as raised:
        Verilator.check_design_supported(design)
    message = str(raised.value)
    assert "verilator" in message
    assert "tb.sources" in message
    assert "tb.top" in message
