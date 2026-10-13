"""VCS runtime observations that the `vcs` and `bsc_sim` flows share.

Contract sources: Synopsys UCLI T-2022.06 (`run`, `stop -command`, `senv`, `config endofsim`)
and the VCS 2019 guide, sections 14-43 and 14-118 (native SV task diagnostics).
These interfaces come from the documentation. No licensed VCS release was tested.
A quiet `$finish(0)` and VHDL termination have no certified finish diagnostic, so they fail
closed. A UCLI return, a CPU summary or an end callback never counts as HDL completion.
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
    """Write the owned UCLI wrapper script and return its path in the run directory.

    The checkpoints observe simulation time, not the HDL finish. The callback of a native
    time breakpoint shows that the requested bound triggered. Equal `senv time` alone cannot
    tell that from an unrelated break or a drained event queue. The caller refuses a bound
    together with arbitrary user or GUI control before it calls this function.
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
        # A user script often ends in `quit`. The wrapper observes the time before the exit.
        # It keeps the vendor command and its arguments, so the process status stays the vendor's.
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
    """Parse a UCLI time as integral femtoseconds; UCLI prints uppercase units."""
    return parse_sim_time(text.lower().replace("sec", "s"))


def parse_vcs_evidence(flow: Flow, path: str | Path = RUNTIME_LOG) -> SimEvidence | None:
    """Read the runtime section that this invocation owns, and the native diagnostic forms.

    A missing or malformed checkpoint fails closed. The diagnostics of a timed-out run stay
    available, but they cannot supply a passing end. A native severity overrides a finish.
    A raw numeric task time has no known unit, so the settings do not convert it.
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
        # SV severity tasks (2019 guide 14-43) and tagged VCS runtime errors. The tagged form
        # also covers VHDL SIMERR diagnostics. It does not infer a VHDL end.
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
        # Keep the events of a failed or timed-out invocation, but accept no finish.
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
                "VCS reported no observable HDL finish and reached no stop limit; a quiet "
                "$finish(0) and VHDL completion need a native finish diagnostic."
            )
    return evidence
