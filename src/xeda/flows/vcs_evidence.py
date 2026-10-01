"""VCS runtime observations shared with the later bsc VCS adapter.

Contract sources: Synopsys UCLI T-2022.06 (run, stop -command, senv, config
endofsim) and VCS 2019 sections 14-43 and 14-118 (native SV task diagnostics).
These interfaces are documentation-based here; no licensed VCS release was tested.
Quiet $finish(0) and VHDL termination have no certified finish diagnostic and fail
closed. A UCLI return, CPU summary or an end callback is never HDL completion.
"""

import logging
import re
from pathlib import Path
from typing import Literal

from ..flow import Flow
from ..flow.sim import SimEvent, SimEvidence, time_in_fs
from ..flow.sim_evidence import parse_sim_time, read_sim_log
from ..utils import replacing_file, tcl_word

log = logging.getLogger(__name__)
RUNTIME_LOG = Path("vcs_runtime.log")
RUNTIME_SCRIPT = Path("vcs_runtime.tcl")


def prepare_vcs_runtime(
    flow: Flow, *, setup: list[str], user_script: Path | None, stop_time: str | None
) -> Path:
    """Write an owned UCLI wrapper; checkpoints observe time, not HDL finish.

    A native time breakpoint's callback proves that the requested bound triggered;
    equality of senv time alone cannot distinguish an unrelated break or queue drain.
    Caller rejects a bound with arbitrary user/GUI control before invoking this helper.
    """
    flow.run_directory.remove(RUNTIME_LOG)
    script = flow.run_directory.writable(RUNTIME_SCRIPT)
    lines = [
        "config endofsim noexit",
        "puts XEDA_VCS_RUNTIME_START",
        "flush stdout",
        "proc xeda_checkpoint {} {",
        '    puts "XEDA_VCS_TIME=[senv time]"',
        '    puts "XEDA_VCS_PRECISION=[senv timePrecision]"',
        "    puts XEDA_VCS_RUNTIME_END",
        "    flush stdout",
        "}",
        # A user script commonly ends in quit. Observe time before it exits, retaining
        # the vendor command and its arguments instead of replacing the process status.
        "rename quit xeda_native_quit",
        "proc quit {args} {xeda_checkpoint; xeda_native_quit {*}$args}",
        *setup,
    ]
    if stop_time is not None:
        lines += [
            f"set xeda_limit [stop -absolute {tcl_word(stop_time)}]",
            'stop -command {puts "XEDA_VCS_LIMIT=[senv time]"; flush stdout} $xeda_limit',
        ]
    lines += [f"source {tcl_word(user_script)}" if user_script else "run", "quit"]
    with replacing_file(script, encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return RUNTIME_SCRIPT


def _native_time(text: str) -> int | None:
    """UCLI prints uppercase units, unlike the shared SI quantity reader."""
    return parse_sim_time(text.lower().replace("sec", "s"))


def parse_vcs_evidence(flow: Flow, path: str | Path = RUNTIME_LOG) -> SimEvidence | None:
    """Read only this invocation's owned runtime section and native diagnostic forms.

    Missing/malformed checkpoints fail closed. Partial timeout diagnostics remain
    available, but cannot supply a passing end. Native severities override finish;
    raw numeric task times have no known unit and are not converted using settings.
    """
    text = read_sim_log(flow, path)
    if text is None:
        return None
    lines = text.splitlines()
    if lines.count("XEDA_VCS_RUNTIME_START") != 1:
        return None
    lines = lines[lines.index("XEDA_VCS_RUNTIME_START") + 1 :]
    evidence = SimEvidence(ended_by="unknown")
    ends = [i for i, line in enumerate(lines) if line == "XEDA_VCS_RUNTIME_END"]
    complete = len(ends) == 1
    tick = None
    if complete:
        end = ends[0]
        if end < 2:
            return None
        now = re.fullmatch(r"XEDA_VCS_TIME=(.+)", lines[end - 2])
        precision = re.fullmatch(r"XEDA_VCS_PRECISION=(.+)", lines[end - 1])
        actual = _native_time(now[1]) if now else None
        tick = _native_time(precision[1]) if precision else None
        if actual is None or tick is None or tick <= 0 or actual % tick:
            return None
        evidence.time, evidence.time_unit = actual // tick, f"{tick}fs"
        lines = lines[: end - 2]
    native_finish = False
    limit = None
    for index, line in enumerate(lines):
        finish = re.fullmatch(r'\$finish called from file "(.+)", line (\d+)\.', line)
        stop = re.fullmatch(r'\$stop called from file "(.+)", line (\d+)\.', line)
        if finish or stop:
            match = finish or stop
            assert match is not None
            kind: Literal["finish", "stop"] = "finish" if finish else "stop"
            evidence.events.append(SimEvent(kind=kind, location=f"{match[1]}:{match[2]}"))
            native_finish |= bool(finish)
            if stop:
                evidence.ended_by = "error"
            continue
        # SV severity tasks (2019 guide 14-43), and tagged VCS runtime errors.
        # The latter also covers VHDL SIMERR diagnostics; it does not invent VHDL end.
        task = re.search(r'(Warning|Error|Fatal): "(.+)", (\d+): .+: at time \d+\s*$', line)
        tagged = re.fullmatch(r"(Warning|Error|Fatal)-\[[\w-]+\] (.+)", line)
        if task or tagged:
            diagnostic = task or tagged
            assert diagnostic is not None
            severity = diagnostic[1]
            event_kind: Literal["warning", "error", "fatal"] = (
                "warning" if severity == "Warning" else "error" if severity == "Error" else "fatal"
            )
            evidence.events.append(
                SimEvent(
                    kind=event_kind,
                    location=f"{task[2]}:{task[3]}" if task else None,
                    message=line,
                )
            )
            if event_kind == "fatal":
                evidence.ended_by = "fatal"
        if re.fullmatch(r"RT Warning: .+", line) and index + 1 < len(lines):
            context = lines[index + 1]
            if index + 2 < len(lines) and context.endswith("at time"):
                context += " " + lines[index + 2].strip()
            rt_location = re.fullmatch(r'"(.+)", line (\d+), for .+, at time\s+\d+\.', context)
            if rt_location:
                evidence.events.append(
                    SimEvent(
                        kind="warning",
                        location=f"{rt_location[1]}:{rt_location[2]}",
                        message=line,
                    )
                )
        assertion = re.fullmatch(r'"(.+)", (\d+): .+: started at (\S+) failed at (\S+)', line)
        if assertion:
            actual_failure = _native_time(assertion[4])
            evidence.events.append(
                SimEvent(
                    kind="error",
                    time=(
                        actual_failure // tick
                        if tick is not None
                        and actual_failure is not None
                        and actual_failure % tick == 0
                        else None
                    ),
                    location=f"{assertion[1]}:{assertion[2]}",
                    message=line,
                )
            )
        hit = re.fullmatch(r"XEDA_VCS_LIMIT=(.+)", line)
        if hit:
            if limit is not None:
                return None
            limit = _native_time(hit[1])
            if limit is None:
                return None
    if not complete:
        # Preserve events from a failed/timeout invocation, without accepting finish.
        return evidence
    if evidence.ended_by not in ("error", "fatal"):
        if native_finish:
            evidence.ended_by = "finish"
        elif limit is not None:
            if time_in_fs(evidence.time or 0, evidence.time_unit or "1fs") != limit:
                return None
            evidence.ended_by = "stop_time"
        else:
            log.error(
                "VCS reported no observable HDL finish or reached limit; quiet $finish(0) "
                "and VHDL completion require a certified native diagnostic."
            )
    return evidence
