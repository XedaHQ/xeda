"""`stop_after: rtl` is a stop the user asked for: the run elaborates the design, writes the RTL
outputs and succeeds. It writes no netlist and no report, so it lists none, and a setting that
asks for one is refused before anything runs, naming what to write instead.

The flows that have `stop_after` are `yosys` and `yosys_fpga`: a run of either used to end
"failed" after writing the RTL outputs, because the report it parses is written by the stage the
run never reaches.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.artifacts import iter_artifact_paths
from xeda.cli import cli
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner

from . import tool_utils
from .settings_samples import flow_classes

ECP5 = "LFE5U-25F-6BG381C"

#: What a launch of each flow that has `stop_after` needs besides it: the tools that stand in for
#: the real ones, the settings that make the flow runnable, and the settings that ask for what a
#: stopped run writes none of, turned off. A flow that gains `stop_after` has a recipe added.
FLOWS = {
    "yosys_fpga": dict(
        fake=tool_utils.use_fake_fpga_tools,
        formats=["ys", "tcl"],
        needs=[f"fpga={ECP5}"],
        off=["netlist_json=", "netlist_verilog="],
    ),
    "yosys": dict(  # its stand-in runs `.ys` scripts only
        fake=tool_utils.use_fake_asic_tools,
        formats=["ys"],
        needs=[],
        off=["netlist_json=", "netlist_verilog="],
    ),
}


def test_every_flow_with_stop_after_has_a_recipe() -> None:
    """The sweeps below cover each flow whose settings have `stop_after`."""
    stopping = {name for cls, name in flow_classes() if "stop_after" in cls.Settings.model_fields}
    assert stopping == set(FLOWS), "a flow with `stop_after` needs a recipe in FLOWS"


def _design(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    path = root / "top.yaml"
    path.write_text(yaml.safe_dump({"name": "top", "rtl": {"sources": ["top.v"], "top": "top"}}))
    return path


def _toolchain(tmp_path: Path, monkeypatch, flow: str) -> None:
    fake = FLOWS[flow]["fake"]
    fake(monkeypatch, tmp_path / ("tools" if fake is tool_utils.use_fake_fpga_tools else "bin"))
    monkeypatch.chdir(tmp_path)


def _xeda(*args: Any) -> tuple[Any, dict]:
    result = CliRunner().invoke(cli, [str(arg) for arg in (*args, "--json")])
    return result, json.loads(result.stdout)


@pytest.mark.parametrize(
    "flow, script_format",
    [(flow, form) for flow, recipe in FLOWS.items() for form in recipe["formats"]],
)
def test_a_stop_after_the_rtl_succeeds_with_the_outputs_it_wrote(
    flow, script_format, tmp_path, monkeypatch
):
    _toolchain(tmp_path, monkeypatch, flow)
    recipe = FLOWS[flow]
    result, document = _xeda(
        "run",
        flow,
        _design(tmp_path),
        "-s",
        *recipe["needs"],
        "stop_after=rtl",
        f"script_format={script_format}",
        "rtl_verilog=rtl.v",
        "rtl_json=rtl.json",
        *recipe["off"],
    )
    assert result.exit_code == 0, result.output
    assert document["success"] is True, document.get("error")
    run_path = Path(document["run_path"])
    assert (run_path / "rtl.v").is_file() and (run_path / "rtl.json").is_file()
    # it lists what it wrote, and nothing it did not: no netlist, no report
    artifacts = [Path(p) for p in iter_artifact_paths(document["results"]["artifacts"])]
    assert sorted(p.name for p in artifacts) == ["rtl.json", "rtl.v"], artifacts
    assert all((run_path / p).is_file() for p in artifacts)
    assert not (run_path / "reports" / "utilization.json").exists()
    assert not document["results"].get("outputs"), "no declared output was switched on"


def _plan(tmp_path: Path, flow: str, *settings: str):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    return runner.plan(
        dict((name, cls) for cls, name in flow_classes())[flow],
        Design.from_file(_design(tmp_path)),
        flow_settings=[*FLOWS[flow]["needs"], "stop_after=rtl", *settings],
    )


#: What asks for a result of the stages after the RTL ones, by the setting that names it, and how
#: the message says to turn it off.
ASKS = {
    "netlist_json": ("netlist_json=n.json", "`netlist_json` to null"),
    "netlist_verilog": ("netlist_verilog=n.v", "`netlist_verilog` to null"),
    "netlist_graph": ("netlist_graph=n.dot", "`netlist_graph` to null"),
    "write_blif": ("write_blif=n.blif", "`write_blif` to null"),
    "sta": ("sta=true", "`sta` to false"),
    "ltp": ("ltp=true", "`ltp` to false"),
}


@pytest.mark.parametrize("setting", ASKS)
@pytest.mark.parametrize("flow", FLOWS)
def test_a_setting_that_asks_for_what_a_stop_never_writes_is_refused_when_planned(
    flow, setting, tmp_path
):
    given, advice = ASKS[setting]
    off = [s for s in FLOWS[flow]["off"] if not s.startswith(setting + "=")]
    with pytest.raises(FlowSettingsError) as refused:
        _plan(tmp_path, flow, given, *off)
    message = str(refused.value)
    assert "`stop_after: rtl`" in message and advice in message, message


@pytest.mark.parametrize("flow", FLOWS)
def test_the_default_netlists_are_refused_with_the_settings_to_write(flow, tmp_path):
    """`stop_after: rtl` alone cannot succeed while the netlists are on, as they are by default:
    the refusal comes before anything runs and says what to write, for every one of them."""
    with pytest.raises(FlowSettingsError) as refused:
        _plan(tmp_path, flow)
    message = str(refused.value)
    for setting in ("netlist_json", "netlist_verilog"):
        if f"{setting}=" in " ".join(FLOWS[flow]["off"]):
            assert f"`-s {setting}=`" in message, message
    assert _plan(tmp_path / "off", flow, *FLOWS[flow]["off"])
