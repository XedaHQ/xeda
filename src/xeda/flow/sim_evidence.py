"""Small readers shared by simulator adapters; the owning flow decides report freshness."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from .flow import Flow
from .sim import SimEvent, SimEvidence, time_in_fs

log = logging.getLogger(__name__)


def read_sim_log(flow: Flow, path: str | Path) -> str | None:
    """Read a current-run UTF-8 transcript, or return None without a parser traceback."""
    report = flow.report_file(Path(path))
    if report is None:
        return None
    try:
        return report.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        log.error("Cannot read simulation log %s: %s", report, exc)
        return None


def read_sim_evidence(
    flow: Flow, path: str | Path, *, events_path: str | Path | None = None
) -> SimEvidence | None:
    """Validate a normalized end envelope and, optionally, its required JSONL event file.

    Separate events must also belong to this invocation. An incomplete event line or competing
    embedded event list makes the whole record unusable; a missing diagnostic is not a pass.
    This format does not reinterpret Verilator's existing stop_maybe records.
    """
    text = read_sim_log(flow, path)
    if text is None:
        return None
    try:
        evidence = SimEvidence.model_validate(json.loads(text))
        if evidence.time is not None and evidence.time < 0:
            raise ValueError("negative simulated time")
        if evidence.time_unit is not None and time_in_fs(1, evidence.time_unit) <= 0:
            raise ValueError("nonpositive time unit")
        if events_path is not None:
            events = read_sim_log(flow, events_path)
            if events is None:
                return None
            if evidence.events:
                raise ValueError("events supplied in both the envelope and event file")
            evidence.events = [
                SimEvent.model_validate(json.loads(line)) for line in events.splitlines()
            ]
        return evidence
    except (ValueError, TypeError) as exc:
        log.error("Invalid simulation evidence %s: %s", path, exc)
        return None


def parse_sim_time(quantity: str) -> int | None:
    """Parse a native quantity as integral femtoseconds; ignore an explicit +delta suffix.

    The unit is required. Fractional femtoseconds cannot be represented and are rejected,
    rather than rounded into invented precision. Source text and arbitrary suffixes fail.
    """
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|ms|us|ns|ps|fs)(?:\s*\+\d+)?\s*", quantity)
    if match is None:
        return None
    value = Decimal(match[1]) * time_in_fs(1, "1" + match[2])
    return int(value) if value == value.to_integral_value() else None


def parse_ghdl_log(flow: Flow, path: str | Path) -> SimEvidence | None:
    """Translate GHDL's native runtime end and assertion diagnostics into evidence.

    Only anchored native end lines count. A report's message, echoed source and analysis
    output cannot establish completion. GHDL reports no time for queue drain, so a silent
    invocation remains unknown. VHDL std.env.stop is intentional batch completion (QB1).
    """
    text = read_sim_log(flow, path)
    if text is None:
        return None
    evidence = SimEvidence(ended_by="unknown")
    for line in text.splitlines():
        end = re.fullmatch(
            r"simulation (?:finished|stopped) @(?P<time>\S+)"
            r"(?: with status (?P<status>-?\d+))?",
            line,
        )
        limit = re.fullmatch(
            r"(?:[^\n]+:info: )?simulation stopped by --stop-time @(?P<time>\S+)",
            line,
        )
        if end or limit:
            match = end or limit
            assert match is not None
            time = parse_sim_time(match["time"])
            if time is None:
                continue
            evidence.time = time
            evidence.time_unit = "1fs"
            evidence.ended_by = "finish" if end else "stop_time"
            if end:
                evidence.events.append(SimEvent(kind="finish", time=time, message=line))
                if end["status"] is not None and int(end["status"]) != 0:
                    evidence.ended_by = "error"
            continue
        diagnostic = re.fullmatch(
            r"(?P<location>.+:\d+:\d+):@(?P<time>[^:]+):"
            r"\((?:assertion|report) (?P<severity>warning|error|failure)\): ?(?P<message>.*)",
            line,
        )
        if diagnostic:
            severity = diagnostic["severity"]
            evidence.events.append(
                SimEvent(
                    kind=(
                        "fatal"
                        if severity == "failure"
                        else "error" if severity == "error" else "warning"
                    ),
                    time=parse_sim_time(diagnostic["time"]),
                    location=diagnostic["location"],
                    message=diagnostic["message"],
                )
            )
    return evidence
