"""The ModelSim flow against the fake `vsim`, which runs the flow's script under tclsh with the
tool's commands recorded and exits as vsim does: only `exit -code N` sets the status."""

import shutil
from pathlib import Path
from typing import Optional

import pytest

import xeda.tool
from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Modelsim
from xeda.flows.modelsim import ModelsimTool

from .tool_utils import fake_calls, use_fake_tools

needs_tclsh = pytest.mark.skipif(
    not shutil.which("tclsh"), reason="tclsh is needed to run the TCL scripts"
)


def _design(root: Path, tb_top: Optional[str] = "tb") -> Design:
    """A VHDL unit and a Verilog testbench."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "uut.vhd").write_text("entity uut is end;\narchitecture a of uut is begin end;\n")
    (root / "tb.v").write_text("module tb; uut u(); endmodule\n")
    tb = {"sources": ["tb.v"], **({"top": tb_top} if tb_top else {})}
    return Design(name="d", design_root=root, rtl={"sources": ["uut.vhd"], "top": "uut"}, tb=tb)


def _run(tmp_path: Path, monkeypatch, settings=None, design=None):
    """Run the flow on the fake tools; the flow and every tool command its script ran."""
    use_fake_tools(monkeypatch)
    run_dir = tmp_path / "run"
    design = design or _design(tmp_path / "design")
    flow = DefaultRunner(run_dir).run_flow(Modelsim, design, settings or {})
    return flow, fake_calls(run_dir)


@needs_tclsh
def test_a_simulation_runs_every_source_and_the_top(tmp_path, monkeypatch) -> None:
    """Each source is compiled with its compiler, and the top is loaded so that `$finish` returns
    to the script (whose test-status check would otherwise never run)."""
    flow, calls = _run(tmp_path, monkeypatch)
    assert flow is not None and flow.succeeded
    commands = [call[0] for call in calls]
    assert commands.index("vcom") < commands.index("vlog") < commands.index("vsim")
    (vsim,) = [call for call in calls if call[0] == "vsim"]
    assert vsim[1:] == ["-t", "ps", "-onfinish", "stop", "tb"]
    assert ["coverage", "attribute", "-name", "TESTSTATUS", "-concise"] in calls


@needs_tclsh
@pytest.mark.parametrize("command", ["vcom", "vlog", "vsim"])
def test_a_failing_tool_command_fails_the_flow(command, tmp_path, monkeypatch) -> None:
    """The script's error paths used `exit 1`, which vsim exits with status 0: every compile
    error was a successful run."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", command)
    flow, calls = _run(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded
    assert ["run", "-all"] not in calls


@needs_tclsh
def test_flags_reach_their_tools_one_word_each(tmp_path, monkeypatch) -> None:
    """User flags are TCL words: a flag with a space is one argument."""
    settings = {
        "vcom_flags": ["-explicit"],
        "vlog_flags": ["+define+MSG=a b"],
        "vsim_flags": ["-voptargs=+acc"],
    }
    _, calls = _run(tmp_path, monkeypatch, settings)
    by_command = {call[0]: call for call in calls}
    assert by_command["vcom"][2:] == ["-explicit"]
    assert by_command["vlog"][2:] == ["+define+MSG=a b"]
    assert "-voptargs=+acc" in by_command["vsim"]


def test_a_design_without_a_simulation_top_is_rejected(tmp_path, monkeypatch) -> None:
    """With no `tb.top`, vsim was started with no design unit, simulated nothing, and passed."""
    use_fake_tools(monkeypatch)
    design = _design(tmp_path / "design", tb_top=None)
    with pytest.raises(DesignValidationError, match=r"tb\.top"):
        DefaultRunner(tmp_path / "run").run_flow(Modelsim, design, {})


def test_vsim_version_is_parsed() -> None:
    """`vsim -version` names the product before the version."""
    out = "Model Technology ModelSim - INTEL FPGA STARTER EDITION vsim 2020.1 Simulator 2020.02"
    assert ModelsimTool.model_construct().process_version_output(out) == ("2020", "1")


def test_the_default_image_runs_vsim_on_amd64(monkeypatch, tmp_path) -> None:
    """Not run: the `docker run` command is captured. The image is amd64-only."""
    ran = []
    monkeypatch.setattr(xeda.tool, "run_process", lambda cli, cmd, **kw: ran.append([cli, *cmd]))
    monkeypatch.chdir(tmp_path)
    tool = ModelsimTool(version_flag=None)
    tool.dockerized = True
    tool.run("-batch", "-do", "do run.tcl")
    (command,) = ran
    assert command[command.index("--platform") + 1] == "linux/amd64"
    image = "chaseruskin/modelsim-intel:20.1.1-ubuntu-22.04"
    assert command[command.index(image) + 1 :] == ["vsim", "-batch", "-do", "do run.tcl"]
