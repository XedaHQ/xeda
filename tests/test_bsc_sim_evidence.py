"""Observe the linked Bluespec runtime, not the compiler's exit status."""

from __future__ import annotations

import json

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.bsc import BscSim
from xeda.utils import WorkingDirectory

from .tool_utils import require_bluesim_evidence


def bsv_design(work, body):
    work.mkdir(parents=True, exist_ok=True)
    source = work / "Probe.bsv"
    source.write_text(
        "package Probe; import Assert::*; (* synthesize *) module mkProbe(Empty);\n"
        + body
        + "\nendmodule endpackage\n"
    )
    return Design(name="probe", design_root=work, rtl={"sources": [source], "top": "mkProbe"})


def launch_bsc(work, body, **settings):
    design = bsv_design(work / "design", body)
    return DefaultRunner(work / "runs", display_results=False).run_flow(
        BscSim, design, {"timeout": 10, **settings}
    )


@pytest.mark.parametrize(
    "body,ok,ending,time",
    [
        ("rule done; $finish(0); endrule", True, "finish", 0),
        (
            "Reg#(UInt#(8)) n <- mkReg(0); rule tick; n <= n+1; if(n==2) $finish(0); endrule",
            True,
            "finish",
            30,
        ),
        ('rule done; $error("PROBE_error"); $finish(0); endrule', False, "finish", 0),
        ('rule done; $warning("PROBE_warning"); $finish(0); endrule', True, "finish", 0),
        ('rule done; $fatal(1,"PROBE_fatal"); endrule', False, None, None),
        ('rule done; dynamicAssert(False,"PROBE_assert"); endrule', False, None, None),
        ("rule done; $stop(0); endrule", False, "unknown", 0),
    ],
)
def test_bluesim_native_tasks(tmp_path, body, ok, ending, time):
    require_bluesim_evidence()
    flow = launch_bsc(tmp_path, body, simulator="bluesim", check_assert=True)
    assert flow is not None and flow.succeeded is ok
    if ending:
        assert flow.results["sim.ended_by"] == ending
        assert flow.results["sim.time"] == time
        assert flow.results["sim.time_unit"] == "1us"
    assert (flow.run_path / "sim.log").is_file()


@pytest.mark.parametrize("severity", ["warning", "error", "failure", "fatal"])
@pytest.mark.parametrize("task", ["warning", "error"])
def test_bluesim_severity_threshold(tmp_path, severity, task):
    require_bluesim_evidence()
    flow = launch_bsc(
        tmp_path,
        f'rule done; ${task}("PROBE"); $finish(0); endrule',
        simulator="bluesim",
        fail_severity=severity,
    )
    assert flow is not None
    assert flow.succeeded is (
        severity in ("failure", "fatal") or (task == "warning" and severity == "error")
    )
    assert flow.results[f"sim.{task}s"] == 1


def test_bluesim_no_finish_times_out(tmp_path):
    require_bluesim_evidence()
    flow = launch_bsc(tmp_path, "", simulator="bluesim", timeout=0.2)
    assert flow is not None and not flow.succeeded
    assert flow.results.error.type == "ProcessTimeout"


@pytest.mark.parametrize("count", [1, 2, 10])
def test_bluesim_measured_cycles(tmp_path, count):
    require_bluesim_evidence()
    flow = launch_bsc(tmp_path, "", simulator="bluesim", max_cycles=count)
    assert flow is not None and flow.succeeded
    evidence = flow.results["sim.evidence"]
    assert evidence["ended_by"] == "max_cycles"
    assert evidence["cycles"] == count
    assert evidence["time"] == (count - 1) * 10
    assert evidence["time_unit"] == "1us"


def test_bluesim_early_finish_is_not_cycle_limit(tmp_path):
    require_bluesim_evidence()
    flow = launch_bsc(
        tmp_path, "rule done; $finish(0); endrule", simulator="bluesim", max_cycles=10
    )
    assert flow is not None and flow.succeeded
    assert flow.results["sim.ended_by"] == "finish"
    assert flow.results["sim.evidence"]["cycles"] == 1


@pytest.mark.parametrize("arg", ["-m", "-m10", "-c", "-csim run", "-f", "-fuser.tcl"])
def test_bluesim_owned_protocol_rejects_raw_overrides(tmp_path, arg):
    design = bsv_design(tmp_path / "design", "")
    flow = BscSim({"simulator": "bluesim", "sim_args": [arg]}, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException, match="sim_args.*max_cycles"):
        flow.init()


@pytest.mark.parametrize(
    "change", ["missing_events", "missing_cycles", "malformed", "stale", "contradictory"]
)
def test_bluesim_checkpoint_fails_closed(tmp_path, change):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    record = {"ended_by": "max_cycles", "time": 90, "time_unit": "1us", "cycles": 10, "events": []}
    if change == "missing_cycles":
        record.pop("cycles")
    if change == "contradictory":
        record["events"] = [{"kind": "finish", "time": 0}]
    (run / "bluesim_end.json").write_text(json.dumps(record))
    (run / "bluesim_events.jsonl").write_text('{"kind":"finish","time":0}\n')
    flow = BscSim({"max_cycles": 10}, design, run)
    if change != "stale":
        flow.start_run()
        (run / "bluesim_end.json").write_text(
            "bad" if change == "malformed" else json.dumps(record)
        )
        if change == "missing_events":
            (run / "bluesim_events.jsonl").unlink()
        else:
            (run / "bluesim_events.jsonl").write_text("")
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert not flow.check_results()


def test_bluesim_plusargs_and_vcd_survive_owned_script(tmp_path):
    require_bluesim_evidence()
    flow = launch_bsc(
        tmp_path,
        'rule done; let proof <- $test$plusargs("proof"); if (!proof) $fatal(1,"missing plusarg"); $finish(0); endrule',
        simulator="bluesim",
        sim_args=["+proof"],
        vcd="proof.vcd",
    )
    assert flow is not None and flow.succeeded
    assert flow.wrote_output("proof.vcd")


def test_bluesim_changed_configuration_rebuilds_objects(tmp_path):
    require_bluesim_evidence()
    design = bsv_design(
        tmp_path / "design", 'rule done; dynamicAssert(False,"PROBE"); $finish(0); endrule'
    )
    runner = DefaultRunner(tmp_path / "runs", display_results=False, rebuild_all=True)
    first = runner.run_flow(
        BscSim, design, {"timeout": 10, "check_assert": False, "cleanup_bobjs": False}
    )
    assert first is not None and first.succeeded
    second = runner.run_flow(
        BscSim, design, {"timeout": 10, "check_assert": True, "cleanup_bobjs": False}
    )
    assert second is not None and not second.succeeded


def test_bluesim_oracle_detects_suppressed_task_record(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        good = launch_case(SimCase("bsc_sim", "bluesim"), tmp_path / "good", patch, positive=True)
    assert good.succeeded
    with monkeypatch.context() as patch:
        bad = launch_case(SimCase("bsc_sim", "bluesim"), tmp_path / "bad", patch)
    assert not bad.succeeded


@pytest.mark.parametrize("backend", ["verilator", "iverilog"])
@pytest.mark.parametrize(
    "body,ok",
    [
        ("rule done; $finish(0); endrule", True),
        ("Reg#(UInt#(8)) n <- mkReg(0); rule tick; n <= n+1; if (n==2) $finish(0); endrule", True),
        ('rule done; $error("PROBE_error"); $finish(0); endrule', False),
        ('rule done; $warning("PROBE_warning"); $finish(0); endrule', True),
        ('rule done; $fatal(1,"PROBE_fatal"); endrule', False),
        ('rule done; dynamicAssert(False,"PROBE_assert"); endrule', False),
        ("rule done; $stop(0); endrule", False),
    ],
)
def test_verilator_iverilog_native_tasks(tmp_path, backend, body, ok):
    require_bsc_backend(backend)
    flow = launch_bsc(tmp_path, body, simulator=backend)
    assert flow is not None and flow.succeeded is ok
    if ok:
        assert flow.results["sim.ended_by"] == "finish"
        if backend == "verilator":
            assert flow.results["sim.time"] in (5, 25)
            assert flow.results["sim.time_unit"] == "1ps"
        else:
            from xeda.flow.sim import time_in_fs

            assert flow.results["sim.time"] >= 0
            assert time_in_fs(1, flow.results["sim.time_unit"]) > 0
    assert (flow.run_path / "sim.log").is_file()


def test_verilator_warning_after_partial_line_fails_at_warning_threshold(tmp_path):
    require_bsc_backend("verilator")
    flow = launch_bsc(
        tmp_path,
        'rule done; $write("progress: "); $warning("PROBE_warning"); $finish(0); endrule',
        simulator="verilator",
        fail_severity="warning",
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["sim.warnings"] == 1


def test_iverilog_partial_line_native_diagnostic_parser(tmp_path):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    flow = BscSim({"simulator": "iverilog"}, design, run)
    flow.start_run()
    (run / "xeda_end.json").write_text(
        json.dumps(
            {
                "ended_by": "finish",
                "time": 5000,
                "time_unit": "1ps",
                "events": [{"kind": "finish", "time": 5000}],
            }
        )
    )
    (run / "sim.log").write_text(
        "XEDA_ICARUS_RUNTIME_START\n"
        "progress: ERROR: /tmp/source.v:42: runtime PROBE\n"
        "       Time: 5000 Scope: main\n"
    )
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert not flow.check_results()
        assert flow.results["sim.errors"] == 1


def test_iverilog_parser_ignores_plain_warning_text(tmp_path):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    flow = BscSim({"simulator": "iverilog"}, design, run)
    flow.start_run()
    (run / "xeda_end.json").write_text(
        json.dumps(
            {
                "ended_by": "finish",
                "time": 5000,
                "time_unit": "1ps",
                "events": [{"kind": "finish", "time": 5000}],
            }
        )
    )
    (run / "sim.log").write_text(
        "XEDA_ICARUS_RUNTIME_START\nprogress: this text contains Warning only\n"
    )
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert flow.check_results()
        assert flow.results["sim.warnings"] == 0


def test_verilator_parser_ignores_plain_warning_text(tmp_path):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    flow = BscSim({"simulator": "verilator"}, design, run)
    flow.start_run()
    (run / "xeda_end.json").write_text(
        json.dumps(
            {
                "ended_by": "exit",
                "time": None,
                "time_unit": "1ps",
                "events": [{"kind": "finish", "time": 5000}],
            }
        )
    )
    (run / "sim.log").write_text("progress: Warning is only ordinary text\n")
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert flow.check_results()
        assert flow.results["sim.warnings"] == 0


@pytest.mark.parametrize("backend", ["verilator", "iverilog"])
@pytest.mark.parametrize("severity", ["warning", "error", "failure", "fatal"])
@pytest.mark.parametrize("task", ["warning", "error"])
def test_verilator_iverilog_severity_threshold(tmp_path, backend, severity, task):
    require_bsc_backend(backend)
    flow = launch_bsc(
        tmp_path,
        f'rule done; ${task}("PROBE"); $finish(0); endrule',
        simulator=backend,
        fail_severity=severity,
    )
    assert flow is not None
    assert flow.succeeded is (
        severity in ("failure", "fatal") or (task == "warning" and severity == "error")
    )
    assert flow.results[f"sim.{task}s"] == 1


@pytest.mark.parametrize("backend", ["verilator", "iverilog"])
def test_verilator_iverilog_no_finish_times_out(tmp_path, backend):
    require_bsc_backend(backend)
    flow = launch_bsc(tmp_path, "", simulator=backend, timeout=0.2)
    assert flow is not None and not flow.succeeded
    assert flow.results.error.type == "ProcessTimeout"


@pytest.mark.parametrize("backend", ["verilator", "iverilog"])
def test_verilator_iverilog_oracle_links_executable_and_suppresses_record(
    tmp_path, monkeypatch, backend
):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        good = launch_case(SimCase("bsc_sim", backend), tmp_path / "good", patch, positive=True)
    assert good.succeeded
    with monkeypatch.context() as patch:
        bad = launch_case(SimCase("bsc_sim", backend), tmp_path / "bad", patch)
    assert not bad.succeeded


@pytest.mark.parametrize("change", ["no_finish", "stale", "malformed", "missing_unit"])
def test_verilator_bsc_exit_record_is_not_driver_evidence(tmp_path, change):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    data = {"ended_by": "exit", "time": None, "time_unit": "1ps", "events": []}
    if change == "missing_unit":
        data["events"] = [{"kind": "finish", "time": 5}]
        data["time_unit"] = None
    (run / "xeda_end.json").write_text(json.dumps(data))
    flow = BscSim({"simulator": "verilator"}, design, run)
    if change != "stale":
        flow.start_run()
        (run / "xeda_end.json").write_text("bad" if change == "malformed" else json.dumps(data))
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert not flow.check_results()


def require_bsc_backend(backend):
    import sys
    from .tool_utils import require_bsc, require_verilator, require_iverilog

    require_bsc()
    if backend == "iverilog":
        if sys.platform != "linux":
            pytest.skip(
                "Real Icarus evidence is a required Linux CI gate; local M3 remains deferred"
            )
        require_iverilog()
    else:
        require_verilator()


@pytest.mark.parametrize("backend", ["cvc", "cver", "isim", "ncverilog", "veriwell"])
def test_uncertified_legacy_backend_is_rejected_before_compilation(tmp_path, monkeypatch, backend):
    from xeda.tool import Tool

    design = bsv_design(tmp_path / "design", "")
    flow = BscSim({"simulator": backend}, design, tmp_path / "run")
    calls = []

    def no_tool(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("legacy backend queried or launched a tool before rejection")

    def no_compile(*args, **kwargs):
        pytest.fail("legacy backend reached compilation")

    monkeypatch.setattr(flow, "_compile", no_compile)
    monkeypatch.setattr(Tool, "execute", no_tool)
    with pytest.raises(
        FlowSettingsException, match="uncertified.*bluesim.*modelsim.*questa.*vcs.*vcsi.*xsim"
    ):
        flow.init()
    assert calls == []


def test_bsc_simulator_adapter_manifest_covers_every_declared_backend(tmp_path):
    from typing import get_args

    from xeda.flows.bsc import SimulatorName

    design = bsv_design(tmp_path / "design", "")
    for backend in get_args(SimulatorName):
        flow = BscSim({"simulator": backend}, design, tmp_path / backend)
        assert flow.has_evidence_adapter() is (
            backend not in {"cvc", "cver", "isim", "ncverilog", "veriwell"}
        )


@pytest.mark.parametrize(
    "backend,option",
    [
        ("modelsim", "-do"),
        ("questa", "-do"),
        ("vcs", "-ucli"),
        ("vcsi", "-i"),
        ("xsim", "-tclbatch"),
    ],
)
def test_bsc_proprietary_sim_args_cannot_replace_owned_scripts(tmp_path, backend, option):
    design = bsv_design(tmp_path / "design", "")
    flow = BscSim({"simulator": backend, "sim_args": [option]}, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException, match="owned runtime evidence protocol"):
        flow.init()


@pytest.mark.parametrize("backend", ["modelsim", "questa", "vcs", "vcsi", "xsim"])
def test_certified_proprietary_backends_have_evidence_adapters(tmp_path, backend):
    design = bsv_design(tmp_path / "design", "")
    flow = BscSim({"simulator": backend}, design, tmp_path / "run")
    assert flow.has_evidence_adapter()


@pytest.mark.parametrize("backend", ["modelsim", "questa", "vcs", "vcsi", "xsim"])
def test_bsc_proprietary_runtime_uses_family_evidence_adapter(tmp_path, monkeypatch, backend):
    from .sim_evidence_cases import SimCase, launch_case

    flow = launch_case(
        SimCase("bsc_sim", backend),
        tmp_path / backend,
        monkeypatch,
        positive=True,
        settings_overrides={"sim_args": ["+proof"]},
    )
    assert flow.succeeded, flow.results.error if flow.results.error else flow.results
    assert flow.results["simulator"] == backend
    assert flow.results["sim.ended_by"] == "finish"
    assert flow.results["sim.evidence"]["ended_by"] == "finish"
    assert (flow.run_path / "oracle.runtime").read_text() == "runtime executed"
    assert (flow.run_path / "sim_build").is_dir()
    if backend in ("modelsim", "questa"):
        runtime_script = (flow.run_path / "bsc_modelsim_runtime.tcl").read_text()
        assert "-lib work_mkTop" in runtime_script
        assert '"-c" "main_opt"' in runtime_script and "+proof" in runtime_script
        assert (flow.run_path / "work_mkTop" / "library.dat").is_file()
    elif backend in ("vcs", "vcsi"):
        import json

        invocation = json.loads(
            (flow.run_path / "fake_vcs.invocations").read_text().splitlines()[0]
        )
        assert "+proof" in invocation[1] and "-i" in invocation[1]
    else:
        import json

        invocation = json.loads((flow.run_path / "fake_xsim.args.json").read_text())
        assert "+proof" in invocation and "-tclbatch" in invocation
        assert invocation[0] == str(flow.run_path / "sim_build" / "mkTop.xsim")


@pytest.mark.parametrize(
    "use_dpi,expected",
    [
        (False, '"-pli" "./directc_mkTop.so"'),
        (True, '"-sv_lib" "directc_mkTop"'),
    ],
)
def test_bsc_modelsim_runtime_preserves_foreign_function_loader(
    tmp_path, monkeypatch, use_dpi, expected
):
    from .sim_evidence_cases import SimCase, launch_case

    flow = launch_case(
        SimCase("bsc_sim", "modelsim"),
        tmp_path / str(use_dpi),
        monkeypatch,
        positive=True,
        foreign_source=True,
        settings_overrides={"sim_args": ["+proof"], "use_dpi": use_dpi},
    )
    assert flow.succeeded
    runtime_script = (flow.run_path / "bsc_modelsim_runtime.tcl").read_text()
    assert expected in runtime_script and "+proof" in runtime_script


@pytest.mark.parametrize(
    "kind,severity,ok",
    [
        ("WARNING", "warning", False),
        ("WARNING", "error", True),
        ("ERROR", "error", False),
        ("ERROR", "failure", True),
        ("FATAL", "failure", False),
    ],
)
def test_iverilog_native_diagnostic_parser(tmp_path, kind, severity, ok):
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "run"
    run.mkdir()
    flow = BscSim({"simulator": "iverilog", "fail_severity": severity}, design, run)
    flow.start_run()
    (run / "xeda_end.json").write_text(
        json.dumps(
            {
                "ended_by": "finish",
                "time": 5000,
                "time_unit": "1ps",
                "events": [{"kind": "finish", "time": 5000}],
            }
        )
    )
    (run / "sim.log").write_text(
        "WARNING: source.v:1: analysis only\nXEDA_ICARUS_RUNTIME_START\n"
        + f"{kind}: /tmp/source.v:42: runtime PROBE\n       Time: 5000 Scope: main\n"
    )
    with WorkingDirectory(run):
        assert flow.has_evidence_adapter()
        assert flow.check_results() is ok
        assert flow.results["sim.warnings"] == (1 if kind == "WARNING" else 0)
        assert flow.results["sim.errors"] == (0 if kind == "WARNING" else 1)


@pytest.mark.parametrize(
    "task",
    ["$finish(0)", "$finish(1)", "$finish(2)", "$finish", "$stop(0)", "$stop(1)", "$stop(2)"],
)
def test_iverilog_builtin_registration_capability(tmp_path, task):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from .tool_utils import require_iverilog, require_c_toolchain

    if sys.platform != "linux":
        pytest.skip("Linux CI must verify builtin task registration; local M3 remains deferred")
    require_iverilog()
    require_c_toolchain()
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "probe"
    run.mkdir()
    flow = BscSim({"simulator": "iverilog"}, design, run)
    with WorkingDirectory(run):
        flow._build_iverilog_monitor()
        source = Path("probe.v")
        source.write_text(
            "`timescale 1ns/1ps\nmodule probe; initial begin #5; "
            + task
            + '; $display("AFTER_TASK"); end endmodule\n'
        )
        subprocess.run(["iverilog", "-o", "probe.vvp", source], check=True, timeout=30)
        native = subprocess.run(
            ["vvp", "-n", "probe.vvp"], capture_output=True, text=True, timeout=10
        )
        hooked = subprocess.run(
            ["vvp", "-n", "-m", "./iverilog_evidence.vpi", "probe.vvp"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "XEDA_END_RECORD": "xeda_end.json"},
        )
        assert hooked.returncode == native.returncode == 0
        assert hooked.stdout.replace("XEDA_ICARUS_RUNTIME_START\n", "") == native.stdout
        assert hooked.stderr == native.stderr
        assert "AFTER_TASK" not in hooked.stdout
        assert "XEDA_ICARUS_RUNTIME_START\n" in hooked.stdout
        record = json.loads(Path("xeda_end.json").read_text())
        stop = task.startswith("$stop")
        assert record == {
            "ended_by": "unknown" if stop else "finish",
            "time": 5000,
            "time_unit": "1ps",
            "events": [{"kind": "stop" if stop else "finish", "time": 5000}],
        }
        Path("xeda_end.json").unlink()
        Path("xeda_end.json.tmp").unlink(missing_ok=True)
        source.write_text("`timescale 1ns/1ps\nmodule probe; initial #5; endmodule\n")
        subprocess.run(["iverilog", "-o", "probe.vvp", source], check=True, timeout=30)
        drained = subprocess.run(
            ["vvp", "-n", "-m", "./iverilog_evidence.vpi", "probe.vvp"],
            capture_output=True,
            timeout=10,
            env={**os.environ, "XEDA_END_RECORD": "xeda_end.json"},
        )
        assert drained.returncode == 0
        assert not Path(
            "xeda_end.json"
        ).exists(), "A drained queue must not look like a quiet finish"


def test_iverilog_helper_compiles_with_installed_headers(tmp_path):
    import shutil
    from .tool_utils import require_bsc, require_c_toolchain

    require_bsc()
    require_c_toolchain()
    if shutil.which("iverilog") is None:
        pytest.skip("Icarus headers are unavailable")
    design = bsv_design(tmp_path / "design", "")
    run = tmp_path / "probe"
    run.mkdir()
    flow = BscSim({"simulator": "iverilog"}, design, run)
    with WorkingDirectory(run):
        flow._build_iverilog_monitor()
    assert (run / "iverilog_evidence.vpi").is_file()


def test_verilator_bsc_requires_p1_hook_version_before_compiling(tmp_path, monkeypatch):
    from xeda.tool import Tool, ToolException

    design = bsv_design(tmp_path / "design", "")
    flow = BscSim({"simulator": "verilator"}, design, tmp_path / "run")
    monkeypatch.setattr(Tool, "version_gte", lambda tool, *args: tool.executable != "verilator")
    monkeypatch.setattr(Tool, "probe_stdout", lambda tool, *args, **kwargs: "Verilator 5.020\n")
    with pytest.raises(ToolException, match="Minimum version not met"):
        flow.init()
    assert not (flow.run_path / "sim_build").exists()
