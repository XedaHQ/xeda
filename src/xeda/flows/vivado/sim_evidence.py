"""Native xsim runtime observations shared by behavioral and netlist simulations.

The diagnostic forms and current_sim PRECISION were measured on Vivado 2024.2.
An owned Vivado log bounds runtime separately from compilation and Tcl echo.
A checkpoint observes time; only native HDL diagnostics establish finish.
"""

import re
from pathlib import Path
from typing import Literal

from ...design import SourceType
from ...flow import Flow
from ...flow.sim import SimEvent, SimEvidence
from ...flow.sim_evidence import parse_sim_time, read_sim_log

RUNTIME_LOG = Path("xsim_runtime.log")
PROCESS_LOG = Path("xsim_process.log")


def parse_xsim_evidence(flow: Flow) -> SimEvidence | None:
    """Read current-run native events and owned checkpoints, failing closed on ambiguity."""
    text = read_sim_log(flow, RUNTIME_LOG)
    if text is None:
        return None
    lines = text.splitlines()
    if lines.count("XEDA_XSIM_RUNTIME_START") != 1:
        return None
    lines = lines[lines.index("XEDA_XSIM_RUNTIME_START") + 1 :]
    complete = lines.count("XEDA_XSIM_RUNTIME_END") == 1
    if lines.count("XEDA_XSIM_RUNTIME_END") > 1:
        return None
    if complete:
        lines = lines[: lines.index("XEDA_XSIM_RUNTIME_END")]
    evidence = SimEvidence(ended_by="unknown")
    vhdl_files = {
        str(source.file) for source in flow.design.sim_sources if source.type is SourceType.Vhdl
    }
    # Relative native paths are interpreted in the simulator's working directory, never by suffix.
    vhdl_paths = {Path(path).resolve() for path in vhdl_files}
    finish_times = []
    checkpoints: list[tuple[str, int, int]] = []
    limit = None
    for index, line in enumerate(lines):
        checkpoint = re.fullmatch(r"XEDA_XSIM_CHECKPOINT=(prerun|main)\|([^|]+)\|([^|]+)", line)
        if checkpoint:
            actual = parse_sim_time(checkpoint[2].replace("sec", "s"))
            tick = parse_sim_time(checkpoint[3].replace("sec", "s"))
            if actual is None or tick is None or tick <= 0 or actual % tick:
                return None
            if checkpoints and (
                len(checkpoints) != 1
                or checkpoints[0][0] != "prerun"
                or checkpoint[1] != "main"
                or actual < checkpoints[-1][1]
                or tick != checkpoints[0][2]
            ):
                return None
            checkpoints.append((checkpoint[1], actual, tick))
            evidence.time, evidence.time_unit = actual // tick, f"{tick}fs"
            continue
        if line.startswith("XEDA_XSIM_CHECKPOINT="):
            return None
        hit = re.fullmatch(r"XEDA_XSIM_LIMIT=(.+)", line)
        if hit:
            if limit is not None:
                return None
            limit = parse_sim_time(hit[1].replace("sec", "s"))
            if limit is None:
                return None
        # Native records can follow partial $write output on the same physical line.
        # Match the complete suffix; severity still requires the native time/source context.
        end = re.search(
            r"\$(finish|stop) called at time : ([\d.]+\s+(?:fs|ps|ns|us|ms|sec))"
            r'(?: : File "(.+)" Line (\d+))?$',
            line,
        )
        if end:
            actual = parse_sim_time(end[2].replace("sec", "s"))
            if actual is None:
                return None
            vhdl_stop = bool(end[3] and (flow.run_path / end[3]).resolve() in vhdl_paths)
            kind: Literal["finish", "stop"] = (
                "finish" if end[1] == "finish" or vhdl_stop else "stop"
            )
            evidence.events.append(
                SimEvent(
                    kind=kind,
                    time=actual,
                    location=f"{end[3]}:{end[4]}" if end[3] else None,
                    message=line,
                )
            )
            if kind == "finish":
                finish_times.append(actual)
            else:
                evidence.ended_by = "error"
            continue
        diagnostic = re.search(r"(Warning|Error|Failure|Fatal): (.*)$", line)
        if diagnostic and index + 1 < len(lines):
            context = re.fullmatch(
                r"Time: ([\d.]+\s+(?:fs|ps|ns|us|ms|sec))  Iteration: \d+  Process: .+"
                r"  (?:Scope: .+  )?File: (.+?)(?: Line: (\d+))?",
                lines[index + 1],
            )
            if context:
                actual = parse_sim_time(context[1].replace("sec", "s"))
                kind_event: Literal["warning", "error", "fatal"] = (
                    "warning"
                    if diagnostic[1] == "Warning"
                    else "error" if diagnostic[1] == "Error" else "fatal"
                )
                evidence.events.append(
                    SimEvent(
                        kind=kind_event,
                        time=actual,
                        location=context[2] + (":" + context[3] if context[3] else ""),
                        message=diagnostic[2],
                    )
                )
                if kind_event == "fatal":
                    evidence.ended_by = "fatal"
    if checkpoints:
        # Events use the same observed precision as the final checkpoint.
        tick = checkpoints[-1][2]
        for event in evidence.events:
            if event.time is not None:
                if event.time % tick:
                    return None
                event.time //= tick
    if not complete:
        return evidence
    if not checkpoints or checkpoints[-1][0] != "main":
        return None
    actual = checkpoints[-1][1]
    if finish_times and (len(finish_times) != 1 or finish_times[0] != actual):
        return None
    if limit is not None and limit != actual:
        return None
    if evidence.ended_by not in ("error", "fatal"):
        if finish_times:
            evidence.ended_by = "finish"
        elif limit is not None:
            evidence.ended_by = "stop_time"
    return evidence
