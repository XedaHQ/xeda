"""Xeda selects the format of the yosys script from the settings.

A setting that needs a Tcl procedure selects a Tcl script: `hierarchy.keep_min_cost`,
`arithmetic.wrap_operators`, `memory.max_bits` and `memory.mock_large` of `yosys`. Without one,
every yosys flow writes a script of yosys, which every yosys build runs. The selection reads
only the settings, so planning, the identity and the rendering agree, and no setting chooses
the format.
"""

import contextlib
import logging
import re
from typing import Any

import pytest

from xeda.flow import FlowSettingsError
from xeda.flows import Yosys, YosysFpga, YosysSim
from xeda.tool import NonZeroExitCode

from .test_yosys_asic import _design, _tiny
from .test_yosys_asic_tcl import ADDER, HIERARCHY_DESIGN, _launch
from .tool_utils import require_yosys

#: Each setting that needs a Tcl script, and settings that give it.
TCL_SETTINGS = [
    ("hierarchy.keep_min_cost", {"hierarchy": {"keep_min_cost": 5}}),
    ("arithmetic.wrap_operators", {"arithmetic": {"wrap_operators": True}}),
    ("memory.max_bits", {"memory": {"max_bits": 64}}),
    ("memory.mock_large", {"memory": {"max_bits": 64, "mock_large": True}}),
]


@pytest.mark.parametrize("key, given", TCL_SETTINGS, ids=[key for key, _ in TCL_SETTINGS])
def test_a_setting_that_needs_tcl_selects_the_tcl_script(key, given):
    settings = Yosys.Settings(platform="nangate45", **given)
    Yosys.check_settings_supported(settings)
    assert key in Yosys.tcl_reasons(settings)
    assert Yosys.script_format(settings) == "tcl"


@pytest.mark.parametrize(
    "given",
    [
        {},
        {"arithmetic": {"wrap_operators": False}},
        {"memory": {"mock_large": False}},
        {"abc": {"mapper": "abc_new"}},
    ],
    ids=["defaults", "no-wrapping", "no-mocking", "abc_new"],
)
def test_without_such_a_setting_yosys_writes_a_yosys_script(given):
    settings = Yosys.Settings(platform="nangate45", **given)
    assert Yosys.tcl_reasons(settings) == []
    assert Yosys.script_format(settings) == "ys"


def test_the_other_yosys_flows_always_write_a_yosys_script():
    assert YosysFpga.script_format(YosysFpga.Settings(fpga={"part": "xc7a12tcsg325-1"})) == "ys"
    assert YosysSim.script_format(YosysSim.Settings()) == "ys"


@pytest.mark.parametrize("flow", [Yosys, YosysFpga, YosysSim], ids=lambda flow: flow.name)
@pytest.mark.parametrize("value", ["ys", "tcl"])
def test_script_format_was_removed(flow, value):
    with pytest.raises(FlowSettingsError) as refused:
        flow.Settings.from_input({"script_format": value})
    assert (
        "`script_format` was removed: Xeda selects the format of the script: a Tcl script only "
        "when a setting needs one; delete it"
    ) in str(refused.value)


def test_a_failed_tcl_script_names_the_setting_that_needs_tcl(tmp_path, monkeypatch, caplog):
    """A yosys built without Tcl cannot run the Tcl script. The log of Xeda names the setting
    that selected it, and the error of the tool stays the error of the run."""
    from xeda.flow_runner import DefaultRunner
    from xeda.flows.yosys.common import YosysBase

    def fail(self, *args: Any) -> None:
        raise NonZeroExitCode(["yosys", "-c", "yosys_synth.tcl"], 1)

    monkeypatch.setattr(YosysBase, "run_yosys", fail)
    caplog.set_level(logging.ERROR)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with contextlib.suppress(Exception):
        runner.launch_flow(
            Yosys, _design(tmp_path / "design"), _tiny(tmp_path, memory={"max_bits": 64})
        )
    assert "since `memory.max_bits` needs one" in caplog.text
    (results,) = (tmp_path / "run").glob("mac/yosys/results.json")
    assert '"nonzero_exit"' in results.read_text()


def test_real_yosys_maps_a_design_elaborated_by_prep(tmp_path):
    """`prep` needs no Tcl: Xeda writes a `.ys` script, which a yosys without Tcl runs."""
    require_yosys()
    flow, netlist = _launch(tmp_path, HIERARCHY_DESIGN, "top", prep=["-nomem"])
    assert (flow.run_path / "yosys_synth.ys").is_file()
    log = (flow.run_path / "yosys.log").read_text()
    assert "Executing PREP pass" in log
    assert not re.search(r"^\s*(big|small)\s", netlist, re.M), "prep -flatten flattens the design"
    assert re.search(r"^\s*(INV|NAND2|NOR2|BUF)_X\d\b", netlist, re.M)


def test_real_yosys_maps_with_abc_new_in_a_ys_script(tmp_path):
    """`abc_new` needs no Tcl: a yosys without Tcl maps with it from a `.ys` script."""
    require_yosys()
    flow, netlist = _launch(tmp_path, ADDER, "adder", abc={"mapper": "abc_new"})
    assert (flow.run_path / "yosys_synth.ys").is_file()
    log = (flow.run_path / "yosys.log").read_text()
    assert re.search(r"ABC: \+ source \S*abc_speed_gia_only\.script", log)
    assert re.search(r"^\s*(INV|NAND2|NOR2|BUF)_X\d\b", netlist, re.M)
    assert not re.search(r"^\s*\\\$_", netlist, re.M), "a generic cell is left in the netlist"
