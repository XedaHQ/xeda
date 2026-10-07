"""simulation flow"""

from __future__ import annotations

import logging
import re
from abc import ABCMeta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from ..cocotb import Cocotb, CocotbSettings
from ..dataclass import Field, XedaBaseModel, deliverable, field_validator
from ..design import LANGUAGE_TYPES, Design, SourceType
from ..units import convert_unit
from .flow import Flow, FlowSettingsException, registered_flows

log = logging.getLogger(__name__)

__all__ = [
    "SimEvent",
    "SimEvidence",
    "SimFlow",
    "judge_evidence",
    "time_in_fs",
]


class SimEvent(XedaBaseModel):
    """Something a simulation reported while it ran."""

    kind: Literal["finish", "stop", "error", "fatal", "warning"] = Field(
        description="What happened."
    )
    time: int | None = Field(None, description="Simulated time, in the record's time unit.")
    location: str | None = Field(None, description="Source file and line, when known.")
    message: str = Field("", description="The simulator's message.")


class SimEvidence(XedaBaseModel):
    """How a simulation ended, as its simulator reported it (an end record)."""

    ended_by: Literal[
        "finish", "stop_time", "max_cycles", "exit", "drained", "error", "fatal", "unknown"
    ] = Field(
        description="What ended the simulation: `$finish`, the requested `stop_time` or "
        "`max_cycles`, the testbench's own driver exiting (`exit`), an event queue that ran "
        "empty (`drained`), an error or a fatal error, or unknown."
    )
    time: int | None = Field(None, description="Simulated time at the end, in `time_unit`.")
    time_unit: str | None = Field(None, description="The unit of `time`, e.g. `1ps`.")
    cycles: int | None = Field(
        None, ge=0, description="Clock cycles actually reached, if observed."
    )
    events: list[SimEvent] = Field([], description="Events recorded while the simulation ran.")
    exit_code: int | None = Field(None, description="The driver's exit status, for `exit`.")


SEVERITY_RANK = {"warning": 1, "error": 2, "failure": 3, "fatal": 3}
#: How severe each event kind is; kinds not listed (`finish`) never fail a run.
_EVENT_RANK = {"warning": 1, "error": 2, "stop": 2, "fatal": 3}
_FS_PER_UNIT = {"s": 10**15, "ms": 10**12, "us": 10**9, "ns": 10**6, "ps": 10**3, "fs": 1}


def time_in_fs(time: int, unit: str) -> int:
    """`time` ticks of `unit` (`"1ps"`, `"100fs"`, `"10ns"`) in femtoseconds."""
    match = re.fullmatch(r"\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*", unit)
    if not match:
        raise ValueError(f"not a time unit: {unit!r}")
    return time * int(match.group(1)) * _FS_PER_UNIT[match.group(2)]


def judge_evidence(flow: Any, evidence: SimEvidence | None, fail_severity: str) -> bool:
    """The evidence rule (design 8a, Q12, Q16): a simulation passes only if it ended by
    `$finish` (at any time), by the `stop_time` the user asked for, or by the testbench's own
    driver exiting with status 0; and no recorded event reached `fail_severity`. An end record
    that is missing, or ends by a drained event queue, an error or a fatal error, fails."""
    if evidence is None:
        log.error("The simulation left no end record: it did not report how it ended.")
        return False
    flow.results["sim.evidence"] = evidence.model_dump(mode="json")
    flow.results["sim.ended_by"] = evidence.ended_by
    flow.results["sim.time"] = evidence.time
    flow.results["sim.time_unit"] = evidence.time_unit
    flow.results["sim.errors"] = sum(e.kind in ("error", "stop", "fatal") for e in evidence.events)
    flow.results["sim.warnings"] = sum(e.kind == "warning" for e in evidence.events)
    stop_time = getattr(flow.settings, "stop_time", None)
    stop_reached = False
    if (
        evidence.ended_by == "stop_time"
        and stop_time is not None
        and evidence.time is not None
        and evidence.time_unit is not None
    ):
        # a bare number is nanoseconds (`SimFlow.Settings.stop_time`); within one tick of the
        # record's precision, since the driver stops at the last whole tick
        requested = int(round(convert_unit(stop_time, "fs", from_unit="ns")))
        tick = time_in_fs(1, evidence.time_unit)
        stop_reached = abs(time_in_fs(evidence.time, evidence.time_unit) - requested) < tick
    max_cycles = getattr(flow.settings, "max_cycles", None)
    cycles_reached = False
    if (
        max_cycles is not None
        and evidence.cycles == max_cycles
        and evidence.time is not None
        and evidence.time >= 0
        and evidence.time_unit is not None
    ):
        try:
            cycles_reached = time_in_fs(1, evidence.time_unit) > 0
        except ValueError:
            pass
    ok = {
        "finish": True,
        "stop_time": stop_reached,
        "max_cycles": cycles_reached,
        "exit": evidence.exit_code == 0,
    }.get(evidence.ended_by, False)
    if not ok:
        why = {
            "drained": "its event queue ran empty without a $finish"
            + (f" before the requested stop_time {stop_time}" if stop_time is not None else ""),
            "stop_time": (
                f"the stop could not be confirmed: the record gave no time or time unit "
                f"(requested stop_time {stop_time})"
                if stop_time is not None and (evidence.time is None or evidence.time_unit is None)
                else (
                    f"it stopped at a time other than the requested stop_time {stop_time}"
                    if stop_time is not None
                    else "it stopped at a stop_time nobody asked for"
                )
            ),
            "max_cycles": (
                f"the cycle stop could not be confirmed: observed {evidence.cycles} cycles "
                f"(requested max_cycles {max_cycles}); a measured final time and unit are required"
                if max_cycles is not None
                else "it stopped at a max_cycles nobody asked for"
            ),
            "exit": f"the testbench's driver exited with status {evidence.exit_code}",
            "error": "an error ended it",
            "fatal": "a fatal error ended it",
        }.get(evidence.ended_by, "it did not report how it ended")
        log.error(
            "The simulation did not end as intended: %s (time %s %s).",
            why,
            evidence.time,
            evidence.time_unit or "",
        )
        return False
    threshold = SEVERITY_RANK[fail_severity]
    worst = [e for e in evidence.events if _EVENT_RANK.get(e.kind, 0) >= threshold]
    if worst:
        log.error(
            "The simulation reported %d event(s) at or above fail_severity=%s, the first: %s at %s: %s",
            len(worst),
            fail_severity,
            worst[0].kind,
            worst[0].location,
            worst[0].message,
        )
        return False
    return True


class SimFlow(Flow, metaclass=ABCMeta):
    """superclass of all simulation flows"""

    cocotb_sim_name: Optional[str] = None

    #: a simulation runs the testbench
    design_parts = frozenset({"rtl", "tb"})

    class Settings(Flow.Settings):
        timeout: float | None = Field(
            None,
            gt=0,
            description="Stop and fail each subprocess invocation containing simulation after "
            "this many seconds of wall-clock time. Combined invocations include analysis and "
            "elaboration; this is not a cumulative dependency deadline. None sets no limit.",
        )
        fail_severity: Literal["warning", "error", "failure", "fatal"] = Field(
            "error",
            description="The least severe observed runtime diagnostic that fails simulation: "
            "warning, error, failure or fatal. Failure and fatal have the same rank; "
            "diagnostics below the threshold are recorded. Explicitly disabled assertions "
            "retain the simulator's documented behavior. Nonzero execution always fails.",
        )
        vcd: Union[str, Path, None] = Field(
            None,
            alias="waveform",
            description="Write a waveform to this file. `true` writes `dump.vcd`; a name without "
            "an extension gets `.vcd`; `false` or an empty name writes none.",
            json_schema_extra=deliverable("outputs/{design}.vcd"),
        )
        stop_time: Union[str, int, float, None] = Field(
            None,
            description="Stop the simulation at this simulated time. Accepts a number of "
            'nanoseconds or a string with a unit, e.g. "100us".',
        )
        cocotb: CocotbSettings = Field(
            CocotbSettings(),  # type: ignore
            description="Settings for the cocotb testbench, used when design.tb.cocotb is set.",
        )
        optimization_flags: List[str] = Field(
            [],
            description="Extra optimization flags passed to the simulator's compiler/elaborator.",
        )

        @field_validator("vcd", mode="before")
        @classmethod
        def _validate_vcd(cls, vcd):
            if vcd is True:
                return "dump.vcd"
            if vcd is False or vcd == "":
                return None
            if isinstance(vcd, (str, Path)) and not Path(vcd).suffix:
                return f"{vcd}.vcd"
            return vcd

    @staticmethod
    def has_cpp_driver(design: Design) -> bool:
        """Whether the design brings a C++ driver of its own: `Cpp` sources among the RTL's and the
        testbench's. A simulator that builds a C++ model runs it in place of its own (`verilator`,
        `yosys_sim`)."""
        return bool(design.sim_sources_of_type(SourceType.Cpp))

    @classmethod
    def runs_without_testbench_top(cls, design: Design) -> bool:
        """Whether this simulator knows what to run for `design` when `tb.top` is not set, though
        its testbench has sources in a hardware description language. No simulator does by
        default. One that finds the testbench's top itself (GHDL's `find-top`), or runs a C++
        driver of the design's own (Verilator, `yosys_sim`), says so by overriding this."""
        return False

    @classmethod
    def check_design_supported(cls, design: Design) -> None:
        """A cocotb testbench needs a simulator xeda drives cocotb on (`cocotb_sim_name`): run on
        any other, the design would be simulated without it and its tests would never run.
        A testbench written in a hardware description language (`LANGUAGE_TYPES`) needs `tb.top`,
        unless the simulator knows what to run without it (`runs_without_testbench_top`):
        otherwise the simulator would simulate `rtl.top`, which has no stimulus."""
        super().check_design_supported(design)
        if design.tb.cocotb and not cls.cocotb_sim_name:
            supported = sorted(
                {
                    flow_class.name
                    for _, flow_class in registered_flows.values()
                    if issubclass(flow_class, SimFlow) and flow_class.cocotb_sim_name
                }
            )
            raise FlowSettingsException(
                f"{cls.name} cannot run cocotb tests; use one of: {', '.join(supported)}"
            )
        if not design.tb.cocotb and not design.tb.top:
            hdl = [src for src in design.tb.sources if src.type in LANGUAGE_TYPES]
            if hdl and not cls.runs_without_testbench_top(design):
                raise FlowSettingsException(
                    f"{cls.name} needs to know which module is the testbench: `tb.sources` "
                    f"holds {', '.join(src.file.name for src in hdl)} but `tb.top` is not set. "
                    "Set `tb.top` to the testbench's top module."
                )

    def __init__(
        self,
        settings: Union[Settings, Dict],
        design: Union[Design, Dict],
        run_path: Path,
        **kwargs,
    ):
        super().__init__(settings, design, run_path, **kwargs)
        assert isinstance(
            self.settings, self.Settings
        ), "self.settings is not an instance of self.Settings class"
        # launched, the flow was checked already; constructed directly, it is checked here
        self.check_design_supported(self.design)
        self.cocotb: Optional[Cocotb] = (
            Cocotb(
                **self.settings.cocotb.model_dump(),
                sim_name=self.cocotb_sim_name,
                # pydantic-mypy does not see `Tool`'s fields through the
                # `Cocotb(CocotbSettings, Tool)` diamond; `dockerized` is a real field.
                dockerized=self.settings.dockerized,  # type: ignore[call-arg]
            )
            if self.cocotb_sim_name and self.design.tb.cocotb
            else None
        )

    def has_evidence_adapter(self) -> bool:
        """Whether this run's simulator can report how it ended (`simulation_evidence`).
        This capability hook never exempts a run from evidence judgment."""
        return False

    def simulation_evidence(self) -> SimEvidence | None:
        """This run's end record, read through `Flow.report_file` (this run's own only)."""
        return None

    def check_results(self) -> bool:
        """cocotb's verdict for a cocotb testbench (on every simulator); otherwise the evidence
        rule. Missing evidence fails, including a flow without an adapter."""
        if self.cocotb:
            return self.cocotb.add_results(
                self.results, results_file=self.report_file(self.cocotb.results_xml)
            )
        assert isinstance(self.settings, self.Settings)
        return judge_evidence(self, self.simulation_evidence(), self.settings.fail_severity)

    def always_runs(self) -> Optional[str]:
        if self.cocotb is not None and self.cocotb.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()
