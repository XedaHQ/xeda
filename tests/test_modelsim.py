"""The ModelSim flow against the fake `vsim`, which runs the flow's script under tclsh with the
tool's commands recorded and exits as vsim does: only `exit -code N` sets the status."""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import xeda.tool
from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Modelsim
from xeda.flows.modelsim import ModelsimTool
from xeda.utils import LOCATION_FORMS

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


def _run(tmp_path: Path, monkeypatch, settings=None, design=None, *, record_calls=True):
    """Run the flow on the fake tools; the flow and every tool command its script ran."""
    use_fake_tools(monkeypatch)
    run_dir = tmp_path / "run"
    design = design or _design(tmp_path / "design")
    flow = DefaultRunner(run_dir).run_flow(Modelsim, design, settings or {})
    return flow, fake_calls(run_dir) if record_calls else []


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


@needs_tclsh
def test_outputs_to_warns_when_a_simulation_delivers_no_waveform(tmp_path, monkeypatch, caplog):
    """`--outputs-to` copies only what a flow reported as an artifact inside its run
    directory; a bare simulation (no `vcd`) reports none, so it silently delivered nothing there.
    The warning names the flow and its deliverable settings (`vcd`, for a simulator)."""
    use_fake_tools(monkeypatch)
    run_dir = tmp_path / "run"
    design = _design(tmp_path / "design")
    with caplog.at_level(logging.WARNING, logger="xeda.flow_runner.default_runner"):
        flow = DefaultRunner(run_dir, outputs_to=tmp_path / "got").run_flow(Modelsim, design, {})
    assert flow is not None and flow.succeeded and flow.deliveries == []
    assert not (tmp_path / "got").exists()
    assert "modelsim" in caplog.text and "vcd" in caplog.text and "delivered nothing" in caplog.text
    assert f"give one a location ({LOCATION_FORMS})" in caplog.text


def test_an_hdl_testbench_without_a_top_is_rejected_when_planned(tmp_path, monkeypatch) -> None:
    """With no `tb.top`, vsim was started with no design unit, simulated nothing, and passed. A
    testbench in Verilog is refused before anything is set up, as it is for every simulator."""
    use_fake_tools(monkeypatch)
    design = _design(tmp_path / "design", tb_top=None)
    with pytest.raises(FlowSettingsException, match=r"tb\.top"):
        DefaultRunner(tmp_path / "run").run_flow(Modelsim, design, {})
    assert not (tmp_path / "run").exists()


def test_a_design_without_a_testbench_has_no_simulation_top(tmp_path, monkeypatch) -> None:
    """With no testbench at all there is no `tb.top` either: the flow itself refuses to start vsim
    with no design unit."""
    use_fake_tools(monkeypatch)
    root = tmp_path / "design"
    root.mkdir()
    (root / "uut.vhd").write_text("entity uut is end;\narchitecture a of uut is begin end;\n")
    design = Design(name="d", design_root=root, rtl={"sources": ["uut.vhd"], "top": "uut"})
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


@needs_tclsh
@pytest.mark.parametrize("edition", ["modelsim", "questa"])
@pytest.mark.parametrize(
    "state, settings, passes, ending, errors, warnings",
    [
        ("finish0", {}, True, "finish", 0, 0),
        ("finish5", {}, True, "finish", 0, 0),
        ("vhdl_finish", {}, True, "finish", 0, 0),
        ("vhdl_stop", {}, True, "finish", 0, 0),
        ("silent", {}, False, "unknown", 0, 0),
        ("drain5", {}, False, "unknown", 0, 0),
        ("error_finish", {}, False, "finish", 1, 0),
        ("error_finish", {"fail_severity": "failure"}, True, "finish", 1, 0),
        ("warning_finish", {}, True, "finish", 0, 1),
        ("warning_finish", {"fail_severity": "warning"}, False, "finish", 0, 1),
        ("failure_finish", {"fail_severity": "fatal"}, False, "finish", 1, 0),
        ("fatal", {}, False, "fatal", 1, 0),
        ("verilog_stop", {"fail_severity": "fatal"}, False, "error", 1, 0),
        ("status2_finish", {}, False, "finish", 1, 0),
        ("limit10", {"stop_time": "10ns"}, True, "stop_time", 0, 0),
        ("limit5", {"stop_time": "10ns"}, False, "stop_time", 0, 0),
        ("break10", {"stop_time": "10ns"}, False, "unknown", 0, 0),
        ("drain5", {"stop_time": "10ns"}, False, "stop_time", 0, 0),
        ("finish0", {"stop_time": "10ns"}, True, "finish", 0, 0),
        ("lookalike", {}, False, "unknown", 0, 0),
    ],
)
def test_modelsim_questa_runtime_evidence(
    edition, state, settings, passes, ending, errors, warnings, tmp_path, monkeypatch
):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", state)
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_EDITION", edition)
    flow, calls = _run(tmp_path, monkeypatch, settings)
    assert flow is not None and flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ending
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.warnings"] == warnings
    assert ["runStatus", "-full"] in calls
    assert (flow.run_path / "modelsim_runtime.log").exists()
    assert flow.results["sim.time"] == (
        0
        if state in ("finish0", "silent")
        else 5000 if state == "limit5" else 10000 if state in ("limit10", "break10") else 5000
    )
    assert flow.results["sim.time_unit"] == "1ps"


@needs_tclsh
@pytest.mark.parametrize("missing", ["modelsim_runtime.log", "modelsim_end.txt"])
def test_modelsim_requires_both_current_outputs(missing, tmp_path, monkeypatch):
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        if "-do" in args:
            Path(missing).unlink(missing_ok=True)
        return result

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded


@needs_tclsh
@pytest.mark.parametrize(
    "checkpoint",
    [
        "",
        "partial\n",
        "XEDA_MODELSIM_V1\n5 ns\n1ps\n-1\nbreak simulation_stop {$finish}\n",
        "XEDA_MODELSIM_V1\n-5 ns\n1ps\n0\nbreak simulation_stop {$finish}\n",
    ],
)
def test_modelsim_rejects_malformed_checkpoints(checkpoint, tmp_path, monkeypatch):
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        if "-do" in args:
            Path("modelsim_end.txt").write_text(checkpoint)
        return result

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded


@needs_tclsh
@pytest.mark.parametrize("command", ["run", "coverage", "runStatus", "transcript"])
def test_modelsim_late_tcl_failure_overrides_finish(command, tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", command)
    flow, _ = _run(tmp_path, monkeypatch, record_calls=False)
    assert flow is not None and not flow.succeeded


@needs_tclsh
def test_modelsim_timeout_bounds_the_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", "hang")
    flow, _ = _run(tmp_path, monkeypatch, {"timeout": 5})
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"


@needs_tclsh
def test_modelsim_a_reused_directory_cannot_supply_finish(tmp_path, monkeypatch):
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and flow.succeeded
    monkeypatch.setenv("XEDA_FAKE_TOOL_NO_OUTPUT", "1")
    runner = DefaultRunner(tmp_path / "run", rebuild_all=True)
    flow = runner.run_flow(Modelsim, _design(tmp_path / "design"), {})
    assert flow is not None and not flow.succeeded


def test_modelsim_defaults_require_error_level_evidence():
    settings = Modelsim.Settings()
    assert settings.fail_severity == "error"
    assert settings.timeout is None


@needs_tclsh
@pytest.mark.parametrize("mode", ["checkpoint_reason", "transcript_reason", "precision", "time"])
def test_modelsim_checkpoint_must_match_native_observations(mode, tmp_path, monkeypatch):
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        if "-do" in args:
            record = Path("modelsim_end.txt")
            transcript = Path("modelsim_runtime.log")
            if mode == "checkpoint_reason":
                record.write_text(record.read_text().replace("{$finish}", "unknown"))
            elif mode == "transcript_reason":
                transcript.write_text(transcript.read_text().replace("{$finish}", "unknown"))
            elif mode == "precision":
                record.write_text(record.read_text().replace("1ps", "0ps"))
            else:
                record.write_text(record.read_text().replace("\n0\n", "\nnonsense\n", 1))
        return result

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded


def test_modelsim_has_an_opt_in_functional_capability_probe():
    from . import tool_utils

    assert callable(tool_utils.require_modelsim)


@needs_tclsh
def test_modelsim_excludes_compile_and_load_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_LOAD_STATUS", "2")
    flow, _ = _run(tmp_path, monkeypatch, {"redirect_stdout": True})
    assert flow is not None and flow.succeeded
    assert flow.results["sim.errors"] == 0 and flow.results["sim.warnings"] == 0
    assert "analysis/load diagnostic" in (flow.run_path / "modelsim_runtime.log").read_text()
    assert "analysis/load diagnostic" in (flow.run_path / "modelsim_process.log").read_text()


@needs_tclsh
def test_modelsim_timeout_retains_current_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", "hang")
    flow, _ = _run(tmp_path, monkeypatch, {"timeout": 5})
    assert flow is not None and not flow.succeeded
    assert "runtime waiting" in (flow.run_path / "modelsim_process.log").read_text()
    assert "runtime waiting" in (flow.run_path / "modelsim_runtime.log").read_text()


@needs_tclsh
@pytest.mark.parametrize("case, passes", [("normal", True), ("failure", False), ("fatal", False)])
def test_modelsim_measured_2020_1_status_spellings(case, passes, tmp_path, monkeypatch):
    """Replay an older measured reason/status; times and execution here remain synthetic."""
    fixture = (Path(__file__).parent / "resources/modelsim/status-2020.1.txt").read_text()
    excerpt = fixture.split(case + " exit 0\n", 1)[1].split("exit 0\n", 1)[0]
    state = next(
        line.removeprefix("RUN_STATUS=")
        for line in excerpt.splitlines()
        if line.startswith("RUN_STATUS=")
    )
    status = next(
        line.removeprefix("TEST_STATUS=")
        for line in excerpt.splitlines()
        if line.startswith("TEST_STATUS=")
    )
    # Tcl list quoting preserves the nested native {$finish} reason.
    monkeypatch.setenv(
        "XEDA_FAKE_TOOL_RETURNS",
        "{runStatus -full} {"
        + state
        + "} {coverage attribute -name TESTSTATUS -concise} "
        + status,
    )
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and flow.succeeded is passes


@needs_tclsh
@pytest.mark.parametrize("path", ["modelsim_runtime.log", "modelsim_end.txt"])
def test_modelsim_refuses_links_created_by_analysis(path, tmp_path, monkeypatch):
    from xeda.utils import tcl_word

    outside = tmp_path / "outside.log"
    outside.write_text("keep this data\n")
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        if "-do" in args:
            script = Path(args[args.index("-do") + 1].split(None, 1)[1])
            text = script.read_text()
            script.write_text(
                text.replace(
                    "transcript off",
                    f"file link -symbolic {path} {tcl_word(str(outside))}\ntranscript off",
                )
            )
        return original(tool, executable, *args, **kwargs)

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch, record_calls=False)
    assert flow is not None and not flow.succeeded
    assert outside.read_text() == "keep this data\n"


@needs_tclsh
@pytest.mark.parametrize("capture", ["none", "logfile", "ini"])
def test_fake_vsim_batch_requires_explicit_transcript(capture, tmp_path, monkeypatch):
    """Batch mode disables automatic transcript files, as ModelSim-Intel 2020.1 does."""
    use_fake_tools(monkeypatch)
    script = tmp_path / "batch.tcl"
    script.write_text(
        'transcript file runtime.log\necho "captured"\nputs "stdout only"\ntranscript file ""\nexit\n'
    )
    args = ["vsim", "-batch", "-do", "do " + str(script)]
    if capture == "logfile":
        args += ["-logfile", "runtime.log"]
    elif capture == "ini":
        ini = tmp_path / "modelsim.ini"
        ini.write_text("[vsim]\nBatchTranscriptFile = runtime.log\n")
        args += ["-modelsimini", str(ini)]
    result = subprocess.run(
        args, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    log = tmp_path / "runtime.log"
    assert log.exists() is (capture != "none")
    if log.exists():
        assert "captured" in log.read_text()
        assert "stdout only" not in log.read_text()


@needs_tclsh
@pytest.mark.parametrize("state", ["vhdl_stop", "verilog_stop", "fatal"])
def test_modelsim_launches_batch_with_a_logfile(state, tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", state)
    original = xeda.tool.Tool.execute
    launched = []

    def execute(tool, executable, *args, **kwargs):
        if "-do" in args:
            launched.append(args)
        return original(tool, executable, *args, **kwargs)

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None
    (args,) = launched
    assert args[args.index("-logfile") + 1] == "modelsim_runtime.log"
    assert "break simulation_stop unknown" in (flow.run_path / "modelsim_end.txt").read_text()
    assert flow.succeeded is (state == "vhdl_stop")


@needs_tclsh
@pytest.mark.parametrize("state", ["vhdl_stop", "verilog_stop", "silent"])
def test_modelsim_bounds_native_stop_evidence(state, tmp_path, monkeypatch):
    """Load/after-runtime stops cannot classify a runtime break or manufacture an end."""
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", state)
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        if "-do" in args:
            log = Path("modelsim_runtime.log")
            outside = (
                "# ** Note: $stop    : tb.v(3)\n"
                "# Break in Process line__1 at uut.vhd line 3\n"
                "# ** Error: outside runtime\n"
                "# XEDA_MODELSIM_RUN_STATUS=break simulation_stop {$finish}\n"
            )
            log.write_text(outside + log.read_text() + outside)
        return result

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and flow.succeeded is (state == "vhdl_stop")
    assert flow.results["sim.errors"] == (1 if state == "verilog_stop" else 0)


@needs_tclsh
@pytest.mark.parametrize("replacement", ["other.vhd", "tb.v", None])
def test_modelsim_unknown_stop_requires_a_known_vhdl_break(replacement, tmp_path, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_MODELSIM_STATE", "vhdl_stop")
    original = xeda.tool.Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        if "-do" in args:
            log = Path("modelsim_runtime.log")
            text = log.read_text()
            text = (
                text.replace("at uut.vhd", "at " + replacement)
                if replacement
                else text.replace("# Break in Process line__1 at uut.vhd line 3\n", "")
            )
            log.write_text(text)
        return result

    monkeypatch.setattr(xeda.tool.Tool, "execute", execute)
    flow, _ = _run(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded
    assert flow.results["sim.ended_by"] == "unknown"
