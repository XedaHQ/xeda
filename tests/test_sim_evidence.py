"""A simulation passes only on evidence that it ended as intended (the M1 class).

Oracle: every registered simulator flow is either converted (it reports evidence, and a run
without evidence fails) or listed below, with the reason it is not converted yet. P1b empties
the list; an entry cannot hide a flow, since the two sets must equal the registered flows.
"""

import pytest

from xeda.flow import registered_flows
from xeda.flow.sim import SimEvent, SimEvidence, SimFlow, judge_evidence

#: Simulator flows not converted yet, and why. P1b removes every entry.
NOT_YET_CONVERTED = {
    "vivado_sim": "P1b: xsim's end",
    "vivado_postsynth_sim": "P1b: xsim's end (a vivado_sim on the netlist)",
    "vivado_power": "P1b: xsim's end (activity simulation)",
}
#: Converted for some backends only: the rest are P1b.
PARTLY_CONVERTED: dict[str, str] = {
    "bsc_sim": "P1b: modelsim, questa, vcs, vcsi, xsim dispatch and "
    "cvc, cver, isim, ncverilog, veriwell rejection remain; Linux Icarus capability verification is pending",
}


def _sim_flows():
    """xeda's own simulator flows (a test module may register flows of its own)."""
    return {
        cls.name
        for _, cls in registered_flows.values()
        if issubclass(cls, SimFlow) and cls.__module__.startswith("xeda.flows")
    }


def test_every_simulator_flow_is_converted_or_listed_with_a_reason():
    converted = {"verilator", "ghdl_sim", "nvc", "yosys_sim", "modelsim", "vcs"}
    assert _sim_flows() == converted | set(NOT_YET_CONVERTED) | set(PARTLY_CONVERTED)
    # a converted flow reports evidence; a listed one does not (so the list cannot go stale)
    adapters = {
        cls.name
        for _, cls in registered_flows.values()
        if issubclass(cls, SimFlow)
        and cls.__module__.startswith("xeda.flows")
        and cls.has_evidence_adapter is not SimFlow.has_evidence_adapter
    }
    assert adapters == converted | set(PARTLY_CONVERTED)


class _Stub:
    """Just what `judge_evidence` reads from a flow."""

    def __init__(self, stop_time=None):
        self.results = {}
        self.settings = type("S", (), {"stop_time": stop_time, "max_cycles": None})()


@pytest.mark.parametrize(
    ("evidence", "stop_time", "passes"),
    [
        (SimEvidence(ended_by="finish", time=34), None, True),
        (SimEvidence(ended_by="finish", time=0), None, True),  # Q16: an explicit $finish, any time
        (SimEvidence(ended_by="drained", time=0), None, False),
        (SimEvidence(ended_by="drained", time=20), "15ns", False),  # a stop that was not reached
        (SimEvidence(ended_by="stop_time", time=15000, time_unit="1ps"), "15ns", True),
        (
            SimEvidence(ended_by="stop_time", time=12000, time_unit="1ps"),
            "15ns",
            False,
        ),  # R6: not the stop asked for
        (
            SimEvidence(ended_by="stop_time", time=15, time_unit="1ns"),
            None,
            False,
        ),  # nobody asked for a stop
        (SimEvidence(ended_by="stop_time", time=15000), "15ns", False),  # no unit: unconfirmed
        (SimEvidence(ended_by="fatal", time=5), None, False),
        (SimEvidence(ended_by="error", time=5), None, False),
        (SimEvidence(ended_by="unknown"), None, False),
        (None, None, False),  # no end record at all
    ],
)
def test_the_evidence_rule(evidence, stop_time, passes):
    assert judge_evidence(_Stub(stop_time), evidence, "error") is passes


@pytest.mark.parametrize(
    ("severity", "kinds", "passes"),
    [
        ("error", ["warning"], True),
        ("error", ["error"], False),
        ("failure", ["error", "error"], True),
        ("failure", ["fatal"], False),
        ("warning", ["warning"], False),
        ("fatal", ["error"], True),
    ],
)
def test_fail_severity_decides_from_the_recorded_events(severity, kinds, passes):
    events = [SimEvent(kind=k, time=34) for k in kinds] + [SimEvent(kind="finish", time=64)]
    evidence = SimEvidence(ended_by="finish", time=64, events=events)
    assert judge_evidence(_Stub(), evidence, severity) is passes


def test_a_requested_max_cycles_stop_counts():
    """R12: P1b's Bluesim adapter reports `max_cycles`; accepted only when it was asked for."""
    stub = _Stub()
    stub.settings.max_cycles = 100
    assert (
        judge_evidence(
            stub, SimEvidence(ended_by="max_cycles", cycles=100, time=100, time_unit="1ns"), "error"
        )
        is True
    )
    assert judge_evidence(_Stub(), SimEvidence(ended_by="max_cycles", time=100), "error") is False


def test_the_verdict_is_recorded_in_the_results():
    flow = _Stub()
    judge_evidence(flow, SimEvidence(ended_by="finish", time=34, time_unit="1ps"), "error")
    assert flow.results["sim.ended_by"] == "finish"
    assert flow.results["sim.time"] == 34
    assert flow.results["sim.time_unit"] == "1ps"


ALL_SKIPPED = """<testsuites><testsuite name="t" tests="2" skipped="2">
<testcase classname="t" name="a" sim_time_ns="0"><skipped/></testcase>
<testcase classname="t" name="b" sim_time_ns="0"><skipped/></testcase>
</testsuite></testsuites>"""


def test_an_all_skipped_cocotb_run_fails(tmp_path):
    from xeda.cocotb import TestResults

    path = tmp_path / "results.xml"
    path.write_text(ALL_SKIPPED)
    results = TestResults.parse_results(path)
    assert results.tests == 2 and results.skipped == 2
    from xeda.cocotb import cocotb_verdict

    flow_results: dict = {}
    assert cocotb_verdict(results, flow_results) is False
    assert flow_results["cocotb.skipped"] == 2


def test_verilator_reads_its_end_record(tmp_path):
    from xeda.flows.verilator import parse_end_record

    record = tmp_path / "xeda_end.json"
    record.write_text(
        '{"ended_by": "finish", "time": 64000, "time_unit": "1ps", "exit_code": null, "events": ['
        '{"kind": "stop_maybe", "maybe": true, "file": "tb.sv", "line": 6, "time": 34000, "msg": ""},'
        '{"kind": "stop_maybe", "maybe": false, "file": "tb.sv", "line": 9, "time": 44000, "msg": ""},'
        '{"kind": "finish", "file": "tb.sv", "line": 11, "time": 64000, "msg": ""}]}'
    )
    evidence = parse_end_record(record)
    assert [e.kind for e in evidence.events] == ["error", "fatal", "finish"]
    assert evidence.ended_by == "finish" and evidence.time == 64000
    assert evidence.events[0].location == "tb.sv:6"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        '{"ended_by": "finish", "events": [{"kind": "bogus"}]}',
        '{"ended_by": "nonsense"}',
        '{"ended_by": "finish", "events": [{"kind": "stop_maybe", "file": "tb.sv", "line": 1}]}',
    ],
)
def test_a_malformed_end_record_is_no_evidence(tmp_path, text):
    from xeda.flows.verilator import parse_end_record

    record = tmp_path / "xeda_end.json"
    record.write_text(text)
    with pytest.raises(ValueError):
        parse_end_record(record)


FINISHED = (
    '{"ended_by": "finish", "time": 34000, "time_unit": "1ps", "exit_code": null, "events": '
    '[{"kind": "finish", "file": "tb.sv", "line": 2, "time": 34000, "msg": ""}]}\n'
)


def test_verilator_never_takes_a_previous_runs_end_record(tmp_path, monkeypatch):
    """A passing end record, written by this run, passes; left by a previous run -- there,
    unchanged, since the run started -- it is not read, and the run does not pass."""
    from xeda import Design
    from xeda.flows import Verilator

    design = Design(
        name="tb",
        design_root=tmp_path,
        rtl={"sources": [], "top": "tb"},
        tb={"sources": [], "top": "tb"},
    )
    outcomes = {}
    for age in ("this run's", "a previous run's"):
        run_dir = tmp_path / age.replace(" ", "_").replace("'", "")
        run_dir.mkdir()
        monkeypatch.chdir(run_dir)
        flow = Verilator({}, design, run_dir)
        assert flow.has_evidence_adapter()
        record = run_dir / flow.settings.sim_dir / "xeda_end.json"
        record.parent.mkdir(parents=True)
        if age == "a previous run's":
            record.write_text(FINISHED)
        flow.start_run()
        if age == "this run's":
            record.write_text(FINISHED)
        outcomes[age] = (flow.check_results(), flow.results.get("sim.ended_by"))
    assert outcomes == {"this run's": (True, "finish"), "a previous run's": (False, None)}


@pytest.mark.parametrize(
    "record,passes",
    [
        ({"time": 100, "time_unit": "1ns"}, False),
        ({"cycles": 99, "time": 100, "time_unit": "1ns"}, False),
        ({"cycles": 100, "time_unit": "1ns"}, False),
        ({"cycles": 100, "time": 100}, False),
        ({"cycles": 100, "time": 100, "time_unit": "bogus"}, False),
        ({"cycles": 100, "time": -1, "time_unit": "1ns"}, False),
        ({"cycles": 100, "time": 100, "time_unit": "1ns"}, True),
    ],
)
def test_max_cycles_requires_measured_count_and_final_time(record, passes):
    flow = _Stub()
    flow.settings.max_cycles = 100
    assert judge_evidence(flow, SimEvidence(ended_by="max_cycles", **record), "error") is passes


def test_early_finish_with_max_cycles_passes():
    flow = _Stub()
    flow.settings.max_cycles = 100
    assert judge_evidence(flow, SimEvidence(ended_by="finish", time=0), "error")


@pytest.mark.parametrize("kind", ["error", "fatal"])
def test_persisted_evidence_preserves_failing_events(kind):
    flow = _Stub()
    evidence = SimEvidence(
        ended_by="finish",
        time=10,
        time_unit="1ns",
        events=[SimEvent(kind=kind, time=5, location="tb:4", message="bad")],
    )
    assert not judge_evidence(flow, evidence, "error")
    assert SimEvidence.model_validate(flow.results["sim.evidence"]) == evidence
    assert flow.results["sim.evidence"]["events"][0]["kind"] == kind


def _normalized_flow(tmp_path):
    from xeda import Design
    from xeda.flows import Verilator

    return Verilator(
        {}, Design(name="tb", design_root=tmp_path, rtl={"sources": [], "top": "tb"}), tmp_path
    )


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "{",
        "[]",
        "{}",
        '{"ended_by":"bogus"}',
        '{"ended_by":"finish","cycles":-1}',
        '{"ended_by":"stop_time","time":5,"time_unit":"bogus"}',
        '{"ended_by":"max_cycles","time":-1,"time_unit":"1ns"}',
        '{"ended_by":"finish","events":[{"kind":"stop_maybe"}]}',
        b"\xff",
    ],
)
def test_normalized_record_missing_or_malformed_is_no_evidence(tmp_path, content):
    from xeda.flow.sim_evidence import read_sim_evidence

    flow = _normalized_flow(tmp_path)
    path = tmp_path / "end.json"
    flow.start_run()
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content)
    assert read_sim_evidence(flow, path) is None


def test_normalized_reports_require_current_run_and_complete_events(tmp_path):
    import json
    from xeda.flow.sim_evidence import read_sim_evidence, read_sim_log

    flow = _normalized_flow(tmp_path)
    record, events, log = (tmp_path / name for name in ("end.json", "events.jsonl", "sim.log"))
    record.write_text('{"ended_by":"finish","time":5,"time_unit":"1ns"}')
    events.write_text('{"kind":"fatal","message":"bad"}\n')
    log.write_text("FINISH\n")
    flow.start_run()
    assert read_sim_evidence(flow, record, events_path=events) is None
    assert read_sim_log(flow, log) is None
    record.write_text('{"ended_by":"finish","time":6,"time_unit":"1ns"}')
    assert read_sim_evidence(flow, record, events_path=events) is None
    events.write_text(json.dumps({"kind": "error", "message": "current"}) + "\n")
    evidence = read_sim_evidence(flow, record, events_path=events)
    assert evidence is not None and evidence.events[0].kind == "error"
    events.write_text('{"kind":"fatal"}\n{')
    assert read_sim_evidence(flow, record, events_path=events) is None
    log.write_text("FINISH current\n")
    assert read_sim_log(flow, log) == "FINISH current\n"
    log.write_bytes(b"\xff")
    assert read_sim_log(flow, log) is None


@pytest.mark.parametrize(
    "quantity,expected",
    [
        ("5ns", 5000000),
        ("0ms+0", 0),
        ("1.25 ps+17", 1250),
        ("2 s", 2000000000000000),
        ("1fs", 1),
        ("1.1fs", None),
        ("5ns+bogus", None),
        ("nan ns", None),
        ("5", None),
        ("-1ns", None),
        ("5NS", None),
        ("time 5ns", None),
    ],
)
def test_normalized_native_time_excludes_delta_cycles(quantity, expected):
    from xeda.flow.sim_evidence import parse_sim_time

    assert parse_sim_time(quantity) == expected


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_normalized_native_writer_refuses_links_and_retains_events(tmp_path, language):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    from .tool_utils import require_c_toolchain, require_cxx_toolchain

    (require_cxx_toolchain if language == "cpp" else require_c_toolchain)()
    compiler = shutil.which("c++" if language == "cpp" else "cc")
    assert compiler
    header = Path(__file__).parents[1] / "src/xeda/flow/templates/sim_record.h"
    src = tmp_path / f"writer.{language}"
    src.write_text(
        '#include "sim_record.h"\n'
        "int main(int argc, char **argv) { (void)argc;\n"
        'if (!xeda_sim_append_event(argv[2], "{\\"kind\\":\\"warning\\"}")) return 2;\n'
        'return xeda_sim_write_record(argv[1], "{\\"ended_by\\":\\"finish\\"}") ? 0 : 3; }\n'
    )
    binary = tmp_path / "writer"
    subprocess.run(
        [compiler, "-Wall", "-Werror", "-I", str(header.parent), str(src), "-o", str(binary)],
        check=True,
        timeout=30,
    )
    canary = tmp_path / "canary"
    canary.write_text("untouched")
    record, events = tmp_path / "end.json", tmp_path / "events.jsonl"
    temporary = tmp_path / "end.json.tmp"
    temporary.symlink_to(canary)
    result = subprocess.run([str(binary), str(record), str(events)], timeout=10)
    assert result.returncode != 0 and canary.read_text() == "untouched"
    temporary.unlink()
    events.unlink()
    events.symlink_to(canary)
    assert subprocess.run([str(binary), str(record), str(events)], timeout=10).returncode != 0
    assert canary.read_text() == "untouched"
    events.unlink()
    assert subprocess.run([str(binary), str(record), str(events)], timeout=10).returncode == 0
    assert json.loads(record.read_text()) == {"ended_by": "finish"}
    assert json.loads(events.read_text()) == {"kind": "warning"}


def test_behavioral_manifest_covers_registry_and_backend_literal():
    from typing import get_args
    from xeda.flows.bsc import SimulatorName
    from .sim_evidence_cases import BSC_BACKENDS, SIMULATORS

    assert set(SIMULATORS) == _sim_flows()
    assert set(BSC_BACKENDS) == set(get_args(SimulatorName))


from .sim_evidence_cases import CASES


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_silent_runtime_requires_behavioral_evidence(case, tmp_path, monkeypatch):
    from .sim_evidence_cases import launch_case

    flow = launch_case(case, tmp_path / "case", monkeypatch)
    reason = NOT_YET_CONVERTED.get(case.flow) or PARTLY_CONVERTED.get(case.flow)
    if reason and flow.succeeded:
        pytest.xfail(reason)
    assert not flow.succeeded, f"{case.name} passed on silent exit 0"


def test_behavioral_oracle_detects_suppressed_positive_record(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("verilator"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "finish"
    with monkeypatch.context() as patch:
        missing = launch_case(SimCase("verilator"), tmp_path / "missing", patch, positive=False)
    assert not missing.succeeded


def test_ghdl_evidence_oracle_detects_suppressed_native_finish(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("ghdl_sim"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "finish"
    with monkeypatch.context() as patch:
        missing = launch_case(SimCase("ghdl_sim"), tmp_path / "missing", patch)
    assert not missing.succeeded


def test_nvc_evidence_oracle_detects_suppressed_native_finish(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("nvc"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "finish"
    with monkeypatch.context() as patch:
        missing = launch_case(SimCase("nvc"), tmp_path / "missing", patch)
    assert not missing.succeeded


def test_cxxrtl_driver_evidence_oracle_detects_suppressed_monitor(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("yosys_sim"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "exit"
    with monkeypatch.context() as patch:
        missing = launch_case(SimCase("yosys_sim"), tmp_path / "missing", patch)
    assert not missing.succeeded


def test_normalized_envelope_round_trip_and_unreadable_report(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from xeda.flow.sim_evidence import read_sim_evidence

    flow = _normalized_flow(tmp_path)
    record = tmp_path / "end.json"
    flow.start_run()
    evidence = SimEvidence(ended_by="max_cycles", cycles=100, time=10, time_unit="1ns")
    record.write_text(json.dumps(evidence.model_dump(mode="json")))
    assert read_sim_evidence(flow, record) == evidence

    def unreadable(*args, **kwargs):
        raise OSError("unreadable report")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert read_sim_evidence(flow, record) is None


def test_modelsim_oracle_detects_suppressed_native_state(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("modelsim"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "finish"
    with monkeypatch.context() as patch:
        missing = launch_case(SimCase("modelsim"), tmp_path / "missing", patch)
    assert not missing.succeeded


def test_vcs_oracle_detects_suppressed_native_finish(tmp_path, monkeypatch):
    from .sim_evidence_cases import SimCase, launch_case

    with monkeypatch.context() as patch:
        positive = launch_case(SimCase("vcs"), tmp_path / "positive", patch, positive=True)
    assert positive.succeeded and positive.results["sim.ended_by"] == "finish"
    with monkeypatch.context() as patch:
        silent = launch_case(SimCase("vcs"), tmp_path / "silent", patch)
    assert not silent.succeeded
    assert (silent.run_path / "oracle.runtime").is_file()
