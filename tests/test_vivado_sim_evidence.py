"""Launched xsim evidence and protected activity-dependency contracts."""

import json
import os
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoPostsynthSim, VivadoPower, VivadoSim
from xeda.tool import Tool

from .tool_utils import fake_calls, use_fake_tools


def design_at(root: Path, vhdl=False) -> Design:
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input a, output y); assign y = ~a; endmodule\n")
    (root / "tb.sv").write_text("module tb; reg a; wire y; top dut(a, y); endmodule\n")
    if vhdl:
        (root / "tb.vhd").write_text("entity tb is end; architecture sim of tb is begin end;\n")
    return Design(
        name="sim",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top"},
        tb={"sources": ["tb.vhd" if vhdl else "tb.sv"], "top": "tb", "uut": "dut"},
    )


def launch(tmp_path, monkeypatch, state="silent", settings=None, flow_class=VivadoSim):
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", state)
    if flow_class is not VivadoSim:
        materialize_builds(monkeypatch)
    if flow_class is VivadoPostsynthSim:
        settings = {"synth": {"fpga": "xc7a12tcsg325-1"}, **(settings or {})}
    if flow_class is VivadoPower:
        settings = {"postsynthsim": {"synth": {"fpga": "xc7a12tcsg325-1"}}, **(settings or {})}
    return DefaultRunner(tmp_path / "run", rebuild_all=True).run_flow(
        flow_class, design_at(tmp_path / "design", vhdl=state.startswith("vhdl")), settings or {}
    )


def materialize_builds(monkeypatch):
    """Model exactly the outputs of successful build/report calls; runtime still executes Tcl."""
    original = Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        words = [str(arg) for arg in args]
        if Path(executable).name == "vivado" and "-source" in words:
            script = Path(words[words.index("-source") + 1])
            if script.name != "vivado_sim.tcl":
                for command in fake_calls(Path.cwd(), elements=True):
                    if command[0] in ("write_verilog", "write_sdf", "write_checkpoint"):
                        path = Path(command[-1])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("module top(input a, output y); assign y=~a; endmodule\n")
                    if command[0] == "report_power" and not os.environ.get(
                        "XEDA_FAKE_POWER_NO_OUTPUT"
                    ):
                        path = Path(command[command.index("-file") + 1])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(
                            '<report><section title="Summary"><table><tablerow><tablecell contents="Total On-Chip Power (W)"/><tablecell contents="0.5"/></tablerow></table></section></report>'
                        )
        return result

    monkeypatch.setattr(Tool, "execute", execute)


def test_vivado_silent_run_return_is_not_hdl_finish(tmp_path, monkeypatch):
    flow = launch(tmp_path, monkeypatch)
    assert flow is not None and not flow.succeeded


def test_vivado_absolute_stop_includes_prerun(tmp_path, monkeypatch):
    flow = launch(tmp_path, monkeypatch, "clock", {"prerun_time": "10ns", "stop_time": "20ns"})
    assert flow.succeeded
    assert flow.results["sim.ended_by"] == "stop_time"
    assert flow.results["sim.time"] == 20_000
    assert flow.results["sim.time_unit"] == "1000fs"


@pytest.mark.parametrize("flow_class", [VivadoSim, VivadoPostsynthSim])
@pytest.mark.parametrize(
    "state,settings,passes,ending,errors,warnings",
    [
        ("finish0", {}, True, "finish", 0, 0),
        ("finish5", {}, True, "finish", 0, 0),
        ("silent", {}, False, "unknown", 0, 0),
        ("drain5", {}, False, "unknown", 0, 0),
        ("lookalike", {}, False, "unknown", 0, 0),
        ("warning", {}, True, "finish", 0, 1),
        ("warning", {"fail_severity": "warning"}, False, "finish", 0, 1),
        ("error", {}, False, "finish", 1, 0),
        ("assertion", {}, False, "finish", 1, 0),
        ("fatal", {}, False, "fatal", 1, 0),
        ("stop", {"fail_severity": "fatal"}, False, "error", 1, 0),
        ("vhdl_finish", {}, True, "finish", 0, 0),
        ("vhdl_stop", {}, True, "finish", 0, 0),
        ("vhdl_warning", {}, True, "finish", 0, 1),
        ("vhdl_error", {}, False, "finish", 1, 0),
        ("vhdl_failure", {}, False, "fatal", 1, 0),
        ("clock", {"stop_time": "20ns"}, True, "stop_time", 0, 0),
        ("drain5", {"stop_time": "20ns"}, True, "stop_time", 0, 0),
        ("finish5", {"prerun_time": "10ns", "stop_time": "20ns"}, True, "finish", 0, 0),
        ("error", {"prerun_time": "10ns", "stop_time": "20ns"}, False, "finish", 1, 0),
    ],
)
def test_vivado_runtime_verdict(
    tmp_path, monkeypatch, flow_class, state, settings, passes, ending, errors, warnings
):
    flow = launch(tmp_path, monkeypatch, state, settings, flow_class)
    assert flow.succeeded is passes
    assert flow.results["sim.ended_by"] == ending
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.warnings"] == warnings


@pytest.mark.parametrize("prerun", ["10ns", "20ns", "30ns"])
def test_vivado_prerun_is_capped_at_absolute_bound(tmp_path, monkeypatch, prerun):
    flow = launch(tmp_path, monkeypatch, "clock", {"prerun_time": prerun, "stop_time": "20ns"})
    assert flow.succeeded
    assert flow.results["sim.time"] == 20_000


@pytest.mark.parametrize("command", ["xvlog", "xelab", "xsim", "run", "close_vcd", "close_saif"])
def test_vivado_script_errors_override_finish(tmp_path, monkeypatch, command):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", command)
    flow = launch(tmp_path, monkeypatch, "finish5", {"vcd": True, "saif": "activity.saif"})
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "NonZeroExitCode"


def test_vivado_timeout_is_applied_to_combined_process(tmp_path, monkeypatch):
    flow = launch(tmp_path, monkeypatch, "timeout", {"timeout": 2.0})
    assert not flow.succeeded
    assert flow.results["error"]["type"] == "ProcessTimeout"


@pytest.mark.parametrize("state", ["silent", "error", "fatal"])
def test_vivado_power_activity_failure_prevents_power_report(tmp_path, monkeypatch, state):
    from xeda.flow import FlowDependencyFailure

    with pytest.raises(FlowDependencyFailure):
        launch(tmp_path, monkeypatch, state, flow_class=VivadoPower)
    run_path = tmp_path / "run" / "sim" / "vivado_power"
    assert (
        json.loads((run_path / "results.json").read_text())["error"]["type"]
        == "FlowDependencyFailure"
    )
    assert not (run_path / "vivado_power.tcl").exists()


@pytest.mark.parametrize(
    "state,settings",
    [
        ("finish5", {}),
        ("warning", {"fail_severity": "error"}),
        ("clock", {"prerun_time": "10ns", "stop_time": "20ns"}),
    ],
)
def test_vivado_power_delegates_fresh_and_reused_evidence(tmp_path, monkeypatch, state, settings):
    flow = launch(tmp_path, monkeypatch, state, {"timeout": 10.0, **settings}, VivadoPower)
    assert flow.succeeded
    evidence = flow.results["sim.evidence"]
    assert flow.results["Total On-Chip Power (W)"] == "0.5"
    assert not (flow.run_path / "xsim_runtime.log").exists()
    simulation = flow.run_path.parent / "vivado_postsynth_sim"

    recorded = json.loads((simulation / "results.json").read_text())
    assert recorded["sim.evidence"] == evidence
    effective = json.loads((simulation / "settings.json").read_text())["effective_flow_settings"]
    assert effective["timeout"] == 10.0
    assert effective["fail_severity"] == settings.get("fail_severity", "error")
    if "stop_time" in settings:
        assert flow.results["sim.time"] == 20000
    before = (simulation / "fake_vivado.calls").read_text()
    # Change only the reporter's settings: it runs while its activity dependency is trace-reused.
    configured = {
        "timeout": 10.0,
        "power_report_xml": "second.xml",
        **settings,
        "postsynthsim": {"synth": {"fpga": "xc7a12tcsg325-1"}},
    }
    rerun = DefaultRunner(tmp_path / "run").run_flow(VivadoPower, flow.design, configured)
    assert rerun.succeeded
    assert rerun.results["sim.evidence"] == evidence
    assert (simulation / "fake_vivado.calls").read_text() == before


@pytest.mark.parametrize(
    "flag",
    [
        "-nolog",
        "-onfinish quit",
        "-onerror quit",
        "-downgrade_error2warning",
        "-downgrade_fatal2info",
        "-maxlogsize 1",
        "-R",
        "-quiet",
        "-tclbatch user.tcl",
        "-testplusarg value=1 -downgrade_error2warning",
        "-testplusarg value=1 -onfinish quit",
    ],
)
def test_vivado_runtime_controls_cannot_bypass_evidence(tmp_path, monkeypatch, flag):
    from xeda.flow import FlowSettingsException

    with pytest.raises(FlowSettingsException, match="runtime evidence"):
        launch(tmp_path, monkeypatch, "finish5", {"sim_flags": [flag]})


@pytest.mark.parametrize("settings", [{"no_log": True}, {"redirect_stdout": True}, {"debug": True}])
def test_vivado_owned_log_is_always_captured(tmp_path, monkeypatch, settings):
    flow = launch(tmp_path, monkeypatch, "finish5", settings)
    assert flow.succeeded
    assert "XEDA_XSIM_RUNTIME_END" in (flow.run_path / "xsim_runtime.log").read_text()


def test_vivado_previous_evidence_is_cleared_before_a_silent_tool(tmp_path, monkeypatch):
    flow = launch(tmp_path, monkeypatch, "finish5")
    assert flow.succeeded
    monkeypatch.setattr(Tool, "execute", lambda *args, **kwargs: 0)
    rerun = launch(tmp_path, monkeypatch, "silent")
    assert not rerun.succeeded
    assert not (rerun.run_path / "xsim_runtime.log").exists()


@pytest.mark.parametrize(
    "replacement",
    [
        "XEDA_XSIM_CHECKPOINT=main|bogus|1 ps",
        "XEDA_XSIM_CHECKPOINT=main|5 ns|0 ps",
        "XEDA_XSIM_CHECKPOINT=main|6 ns|1 ps",
        "XEDA_XSIM_CHECKPOINT=main|5 ns|1 ps\nXEDA_XSIM_CHECKPOINT=main|5 ns|1 ps",
        "XEDA_XSIM_RUNTIME_END\nXEDA_XSIM_CHECKPOINT=main|5 ns|1 ps",
    ],
)
def test_vivado_malformed_or_contradictory_checkpoint_fails(tmp_path, monkeypatch, replacement):
    original = Tool.execute

    def execute(tool, executable, *args, **kwargs):
        result = original(tool, executable, *args, **kwargs)
        path = Path.cwd() / "xsim_runtime.log"
        if path.is_file():
            path.write_text(
                path.read_text().replace("XEDA_XSIM_CHECKPOINT=main|5000000 fs|1 ps", replacement)
            )
        return result

    monkeypatch.setattr(Tool, "execute", execute)
    flow = launch(tmp_path, monkeypatch, "finish5")
    assert not flow.succeeded


def test_vivado_power_requires_its_own_current_power_xml(tmp_path, monkeypatch):
    flow = launch(tmp_path, monkeypatch, "finish5", flow_class=VivadoPower)
    assert flow.succeeded
    (flow.run_path / "power_impl_timing.xml").write_text("<old-report/>")
    monkeypatch.setenv("XEDA_FAKE_POWER_NO_OUTPUT", "1")
    rerun = launch(tmp_path, monkeypatch, "finish5", flow_class=VivadoPower)
    assert not rerun.succeeded
    assert rerun.results["sim.ended_by"] == "finish"


@pytest.mark.parametrize(
    "state,settings", [("warning", {"fail_severity": "warning"}), ("timeout", {"timeout": 2.0})]
)
def test_vivado_power_propagates_failing_simulation_controls(
    tmp_path, monkeypatch, state, settings
):
    from xeda.flow import FlowDependencyFailure

    with pytest.raises(FlowDependencyFailure):
        launch(tmp_path, monkeypatch, state, settings, VivadoPower)
    simulation = tmp_path / "run" / "sim" / "vivado_postsynth_sim"
    result = json.loads((simulation / "results.json").read_text())
    if state == "warning":
        assert result["sim.warnings"] == 1
    else:
        assert result["error"]["type"] == "ProcessTimeout"


@pytest.mark.parametrize("quantity", ["10", "10 ns", "1e1ns", "0ns"])
def test_vivado_quantities_use_the_common_unit_contract(tmp_path, monkeypatch, quantity):
    flow = launch(tmp_path, monkeypatch, "clock", {"stop_time": quantity})
    assert flow.succeeded
    assert flow.results["sim.time"] == (0 if quantity == "0ns" else 10000)


@pytest.mark.parametrize(
    "state,passes,errors", [("partial_error", False, 1), ("partial_finish", True, 0)]
)
def test_vivado_native_diagnostics_after_partial_output(
    tmp_path, monkeypatch, state, passes, errors
):
    flow = launch(tmp_path, monkeypatch, state, {"prerun_time": "10ns", "stop_time": "20ns"})
    assert flow.succeeded is passes
    assert flow.results["sim.errors"] == errors
    assert flow.results["sim.ended_by"] == "finish"


def test_vivado_cannot_disable_required_source_provenance(tmp_path, monkeypatch):
    from xeda.flow import FlowSettingsException

    with pytest.raises(FlowSettingsException, match="source provenance"):
        launch(tmp_path, monkeypatch, "vhdl_stop", {"elab_debug": "off"})
