"""Current-run evidence readers for bsc's linked simulation backends."""

import logging
import re

from ...flow import Flow
from ...flow.sim import SimEvidence, SimEvent, time_in_fs
from ...flow.sim_evidence import read_sim_evidence, read_sim_log

log = logging.getLogger(__name__)


def bluesim_evidence(flow: Flow) -> SimEvidence | None:
    """Merge task hooks with the owned script's measured time and rising-edge count."""
    evidence = read_sim_evidence(flow, "bluesim_end.json", events_path="bluesim_events.jsonl")
    if evidence is None:
        return None
    if any(event.kind == "fatal" for event in evidence.events):
        evidence.ended_by = "fatal"
    elif evidence.ended_by != "fatal":
        finish = next((e for e in reversed(evidence.events) if e.kind == "finish"), None)
        if finish is not None:
            if finish.time != evidence.time:
                return None
            evidence.ended_by = "finish"
        elif any(e.kind == "stop" for e in evidence.events):
            evidence.ended_by = "unknown"
    return evidence


def verilator_evidence(flow: Flow) -> SimEvidence | None:
    """bsc owns the main; only an observed finish can normalize the generic exit fallback."""
    from ..verilator import parse_end_record

    path = flow.report_file("xeda_end.json")
    text = read_sim_log(flow, "sim.log")
    if path is None or text is None:
        return None
    try:
        evidence = parse_end_record(path)
        if evidence.time_unit is None or time_in_fs(1, evidence.time_unit) <= 0:
            return None
        if evidence.ended_by == "exit":
            finish = next((e for e in reversed(evidence.events) if e.kind == "finish"), None)
            if finish is not None and finish.time is None:
                return None
            evidence.ended_by = "finish" if finish else "unknown"
            evidence.time = finish.time if finish else None
        if evidence.time is not None and evidence.time < 0:
            return None
    except ValueError as exc:
        log.error("Invalid bsc Verilator end record: %s", exc)
        return None
    for line in text.splitlines():
        warning = re.search(r"\[(\d+)\] %Warning(?:-[A-Z0-9_]+)?: (.*)", line)
        if warning:
            evidence.events.append(
                SimEvent(kind="warning", time=int(warning[1]), message=warning[2])
            )
    return evidence


def iverilog_evidence(flow: Flow) -> SimEvidence | None:
    """Quiet finish/stop comes from task registration; native runtime diagnostics supply severity."""
    evidence = read_sim_evidence(flow, "xeda_end.json")
    text = read_sim_log(flow, "sim.log")
    if evidence is None or text is None or "XEDA_ICARUS_RUNTIME_START\n" not in text:
        return None
    if (
        evidence.time is None
        or evidence.time_unit is None
        or len(evidence.events) != 1
        or evidence.events[0].time != evidence.time
        or (evidence.ended_by, evidence.events[0].kind)
        not in (("finish", "finish"), ("unknown", "stop"))
    ):
        return None
    runtime = text.split("XEDA_ICARUS_RUNTIME_START\n", 1)[1]
    for line in runtime.splitlines():
        diagnostic = re.search(r"(WARNING|ERROR|FATAL): (.+):([0-9]+): ?(.*)", line)
        if diagnostic:
            kind = {"WARNING": "warning", "ERROR": "error", "FATAL": "fatal"}[diagnostic[1]]
            evidence.events.append(
                SimEvent(
                    kind=(
                        "warning" if kind == "warning" else "error" if kind == "error" else "fatal"
                    ),
                    location=diagnostic[2] + ":" + diagnostic[3],
                    message=diagnostic[4],
                )
            )
    return evidence
