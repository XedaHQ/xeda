"""Current-run evidence readers for bsc's linked simulation backends."""

from ...flow import Flow
from ...flow.sim import SimEvidence
from ...flow.sim_evidence import read_sim_evidence


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
