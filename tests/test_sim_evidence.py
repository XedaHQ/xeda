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
    "ghdl_sim": "P1b: GHDL's end and assertion levels",
    "nvc": "P1b: nvc's end",
    "modelsim": "P1b: ModelSim's TESTSTATUS and end",
    "vcs": "P1b: VCS's end",
    "vivado_sim": "P1b: xsim's end",
    "vivado_postsynth_sim": "P1b: xsim's end (a vivado_sim on the netlist)",
    "vivado_power": "P1b: xsim's end (activity simulation)",
    "yosys_sim": "P1b: the CXXRTL driver reports no end",
}
#: Converted for some backends only: the rest are P1b.
PARTLY_CONVERTED = {"bsc_sim": "Verilator converted in P1; Bluesim, Icarus and the others in P1b"}


def _sim_flows():
    """xeda's own simulator flows (a test module may register flows of its own)."""
    return {
        cls.name
        for _, cls in registered_flows.values()
        if issubclass(cls, SimFlow) and cls.__module__.startswith("xeda.flows")
    }


@pytest.mark.xfail(strict=True, reason="Task 7 converts verilator")
def test_every_simulator_flow_is_converted_or_listed_with_a_reason():
    converted = {"verilator"}
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
    assert judge_evidence(stub, SimEvidence(ended_by="max_cycles", time=100), "error") is True
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
