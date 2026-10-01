"""Native ModelSim/Questa runtime observations, reusable by the bsc vsim adapter.

runStatus's stop reason distinguishes quiet HDL finish from a returned Tcl command.
The command/status contract is documented in the vendor command reference (v2024.2,
runStatus and transcript file); ModelSim-Intel 2020.1's `simulation_stop {$finish}`
was measured in the PR #85 probes. Batch logfile capture and the unknown stop reason
were measured on ModelSim-Intel 2020.1 for FB2. Synthetic tests do not certify another version.
"""

import re
from pathlib import Path
from typing import Literal

from ...design import SourceType
from ...flow import Flow
from ...flow.sim import SimEvent, SimEvidence, time_in_fs
from ...flow.sim_evidence import parse_sim_time, read_sim_log


def parse_modelsim_evidence(
    flow: Flow,
    transcript: str | Path = "modelsim_runtime.log",
    checkpoint: str | Path = "modelsim_end.txt",
) -> SimEvidence | None:
    """Require matching current-run transcript and a complete native time/status checkpoint.

    Zero TESTSTATUS and Tcl return alone establish no end. A native finish stop reason
    must also appear in the owned runtime transcript. Messages and echoed source cannot
    stand in for that observation. TESTSTATUS adds a severity only when the log has not
    already recorded an event at that rank or higher.
    """
    text = read_sim_log(flow, transcript)
    record = read_sim_log(flow, checkpoint)
    if text is None or record is None:
        return None
    lines = [line.removeprefix("# ") for line in text.splitlines()]
    fields = record.splitlines()
    if len(fields) != 5 or fields[0] != "XEDA_MODELSIM_V1":
        return None
    _, now, resolution, status_text, state = fields
    if not re.fullmatch(r"[0-3]", status_text):
        return None
    if not re.fullmatch(r"(?:\d+\s*)?(?:fs|ps|ns|us|ms|s)", resolution):
        return None
    unit = resolution if resolution[0].isdigit() else "1" + resolution
    tick = time_in_fs(1, unit)
    # Native $now is an integer tick count at $resolution, not a unit-bearing time.
    actual = int(now) * tick if re.fullmatch(r"\d+", now) else parse_sim_time(now)
    if actual is None or tick <= 0 or actual % tick:
        return None
    if lines.count("XEDA_MODELSIM_RUNTIME_START") != 1:
        return None
    start = lines.index("XEDA_MODELSIM_RUNTIME_START")
    # -logfile also captures compilation/loading. Only the first status after our start
    # closes the runtime section; observations outside that section are not evidence.
    finish = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("XEDA_MODELSIM_RUN_STATUS=")
        ),
        None,
    )
    if finish is None or lines[finish] != "XEDA_MODELSIM_RUN_STATUS=" + state:
        return None
    lines = lines[start + 1 : finish]
    evidence = SimEvidence(ended_by="unknown", time=actual // tick, time_unit=unit)
    for index, line in enumerate(lines):
        diagnostic = re.fullmatch(r"\*\* (Warning|Error|Failure|Fatal): (.*)", line)
        if diagnostic is None:
            continue
        severity, message = diagnostic.groups()
        event_time = None
        if index + 1 < len(lines):
            context = re.fullmatch(r"\s+Time: (.+?)\s+Iteration: \d+\s+.*", lines[index + 1])
            if context:
                native_time = parse_sim_time(context[1])
                if native_time is not None and native_time % tick == 0:
                    event_time = native_time // tick
        kind: Literal["warning", "error", "fatal"] = (
            "fatal"
            if severity in ("Failure", "Fatal")
            else "error" if severity == "Error" else "warning"
        )
        evidence.events.append(
            SimEvent(
                kind=kind,
                time=event_time,
                message=message,
            )
        )
    status = int(status_text)
    # This native reason works even for $finish(0), which suppresses the usual Note.
    if state == "break simulation_stop {$finish}":
        evidence.ended_by = "finish"
        evidence.events.append(SimEvent(kind="finish", time=evidence.time))
    elif state in ("break simulation_stop {$stop}", "break simulation_stop unknown"):
        # QB1: std.env.stop is a VHDL batch completion. Intel 2020.1 reports unknown
        # for both stop forms; the native Note distinguishes Verilog $stop in mixed HDL.
        verilog_stop = any(re.fullmatch(r"\*\* Note: \$stop(?:\s*:\s*.+)?", line) for line in lines)
        vhdl_names = {
            source.file.name for source in flow.design.sim_sources if source.type is SourceType.Vhdl
        }
        vhdl_break = False
        for line in lines:
            native_break = re.fullmatch(r"(?:\*\* )?Break in Process .+ at (.+) line \d+", line)
            if native_break and Path(native_break[1]).name in vhdl_names:
                vhdl_break = True
        if status == 3:
            evidence.ended_by = "fatal"
        elif verilog_stop or (state == "break simulation_stop {$stop}" and not vhdl_break):
            evidence.ended_by = "error"
            evidence.events.append(SimEvent(kind="stop", time=evidence.time, message="$stop"))
        elif vhdl_break:
            evidence.ended_by = "finish"
            evidence.events.append(SimEvent(kind="finish", time=evidence.time))
    elif state == "ready end" and getattr(flow.settings, "stop_time", None) is not None:
        evidence.ended_by = "stop_time"
    elif "fatal_error" in state.split() or status == 3:
        evidence.ended_by = "fatal"
    elif state.startswith("error "):
        evidence.ended_by = "error"
    ranks = {"warning": 1, "error": 2, "fatal": 3, "stop": 2}
    if status and not any(ranks.get(event.kind, 0) >= status for event in evidence.events):
        evidence.events.append(
            SimEvent(
                kind="fatal" if status == 3 else "error" if status == 2 else "warning",
                time=evidence.time,
                message=f"Native TESTSTATUS={status}",
            )
        )
    return evidence
