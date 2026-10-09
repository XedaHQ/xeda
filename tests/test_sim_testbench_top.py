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

A simulator that declares the types it reads (`Flow.reads_sources`) refuses a source in another
language before it asks for `tb.top`, so each case here is a design in a language the simulator
reads: the RTL and the testbench are written in it.
"""

import re
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.design import LANGUAGE_TYPES, SourceType
from xeda.flow import FlowException, SimFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flows import Verilator, YosysSim

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
#: the `SourceType` of each language above
HDL_TYPES = {
    "verilog": SourceType.Verilog,
    "systemverilog": SourceType.SystemVerilog,
    "vhdl": SourceType.Vhdl,
    "bluespec": SourceType.Bluespec,
    "chisel": SourceType.Chisel,
}
#: a design's own C++ driver
DRIVER = ("main.cpp", "int main() { return 0; }\n")


def _languages_read_by(flow_class: type[SimFlow]) -> list[str]:
    """The `HDL_TESTBENCHES` languages `flow_class` reads: all of them for a simulator that
    declares no types, and so chooses its inputs itself."""
    reads = flow_class.reads_sources
    return [
        language for language in HDL_TESTBENCHES if reads is None or HDL_TYPES[language] in reads
    ]


#: every simulator with each language it reads
READABLE = [
    (name, language)
    for name in SIM_FLOW_NAMES
    for language in _languages_read_by(get_flow_class(name))
]


def _design(tmp_path: Path, *testbench: str, rtl: str = "systemverilog", **tb) -> Design:
    """A design whose RTL is a unit in the language `rtl`, and whose testbench sources are the
    named `HDL_TESTBENCHES` and, for "driver", a C++ driver; none: no testbench sources."""
    rtl_name, rtl_text = {
        "verilog": ("dut.v", "module dut; endmodule\n"),
        "systemverilog": ("dut.sv", "module dut; endmodule\n"),
        "vhdl": ("dut.vhd", "entity dut is end entity;\n"),
        "bluespec": ("dut.bsv", "package dut; endpackage\n"),
        "chisel": ("dut.sc", "object Dut\n"),
    }[rtl]
    (tmp_path / rtl_name).write_text(rtl_text)
    sources = []
    for kind in testbench:
        name, text = DRIVER if kind == "driver" else HDL_TESTBENCHES[kind]
        (tmp_path / name).write_text(text)
        sources.append(name)
    return Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [rtl_name], "top": "dut"},
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


def test_one_predicate_says_whether_a_design_has_a_cpp_driver(tmp_path):
    """`verilator` (for its own use of the driver, and for the rule here) and `yosys_sim` ask
    `SimFlow.has_cpp_driver`."""
    for expected in (False, True):
        design = _design(
            _in_new_directory(tmp_path, f"design-{expected}"),
            "systemverilog",
            *(["driver"] if expected else []),
            top="tb",
        )
        assert SimFlow.has_cpp_driver(design) is expected
        assert Verilator.runs_without_testbench_top(design) is expected
        assert YosysSim.runs_without_testbench_top(design) is expected
        flow = Verilator({}, design, _in_new_directory(tmp_path, f"run-{expected}"))
        assert flow.own_driver() is expected


@pytest.mark.parametrize(("flow_name", "language"), READABLE)
@pytest.mark.parametrize("driver", [False, True], ids=["no_driver", "own_driver"])
def test_every_simulator_refuses_an_hdl_testbench_without_a_top(
    flow_name, language, driver, tmp_path
):
    flow_class = get_flow_class(flow_name)
    design = _design(tmp_path, language, *(["driver"] if driver else []), rtl=language)
    assert not design.tb.top
    if flow_class.runs_without_testbench_top(design):
        flow_class.check_design_supported(design)
        return
    with pytest.raises(FlowException, match=re.escape("`tb.top`")):
        flow_class.check_design_supported(design)


@pytest.mark.parametrize("flow_name", SIM_FLOW_NAMES, ids=str)
def test_every_simulator_accepts_what_names_no_hdl_top_to_miss(flow_name, tmp_path):
    flow_class = get_flow_class(flow_name)
    language = _languages_read_by(flow_class)[0]
    # the testbench's top is named
    named = _design(_in_new_directory(tmp_path, "named"), language, rtl=language, top="tb")
    flow_class.check_design_supported(named)
    # no testbench at all: the RTL top is simulated by design
    bare = _design(_in_new_directory(tmp_path, "bare"), rtl=language)
    flow_class.check_design_supported(bare)
    # a design's own C++ driver, which has no top
    driver = _design(_in_new_directory(tmp_path, "cpp"), "driver", rtl=language)
    flow_class.check_design_supported(driver)
    if flow_class.cocotb_sim_name:
        # a cocotb testbench drives the RTL top unless it names another toplevel
        cocotb = _design(_in_new_directory(tmp_path, "cocotb"), language, rtl=language, cocotb=True)
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
