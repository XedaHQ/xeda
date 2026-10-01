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
