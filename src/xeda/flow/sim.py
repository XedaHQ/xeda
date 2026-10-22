"""The base class of simulation flows, and the rule that judges how a simulation ended."""

from __future__ import annotations

import logging
import os
import re
from abc import ABCMeta
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Dict, Literal, Mapping, Optional, Union

from ..cocotb import Cocotb, CocotbSettings
from ..dataclass import (
    COMPRESSED_FORMATS,
    Field,
    XedaBaseModel,
    format_of_waveform_name,
    waveform_format_name,
    waveform,
    waveform_fields,
)
from ..design import LANGUAGE_TYPES, Design, SourceType
from ..records import REPORTED_FAILURE, ErrorInfo
from ..units import convert_unit
from ..utils import SDF
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
    """Return `time` ticks of `unit` in femtoseconds.

    A unit is a count and a time unit, such as `"1ps"`, `"100fs"` or `"10ns"`."""
    match = re.fullmatch(r"\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*", unit)
    if not match:
        raise ValueError(f"not a time unit: {unit!r}")
    return time * int(match.group(1)) * _FS_PER_UNIT[match.group(2)]


def readable_time(time: int, unit: str | None) -> str:
    """Return a simulated time in the largest unit that holds it exactly.

    An example is `80 ns`, not `80000000 1fs`. Return the time as given when the unit is missing
    or not a time unit that Xeda knows."""
    try:
        femtoseconds = time_in_fs(time, unit or "")
    except ValueError:
        return f"{time} {unit}" if unit else str(time)
    for name in ("s", "ms", "us", "ns", "ps"):
        if femtoseconds and femtoseconds % _FS_PER_UNIT[name] == 0:
            return f"{femtoseconds // _FS_PER_UNIT[name]} {name}"
    return f"{femtoseconds} fs"


def record_failure(flow: Any, message: str) -> None:
    """Record `message` as the `error` in the `results.json` of the run.

    Keep an error that the run already has (a tool's exit status). Without this, a failure that
    the checks of the flow found, with no exception, would get the general message of the
    launcher."""
    if not flow.results.get("error"):
        node = getattr(flow, "name", None)
        flow.results["error"] = ErrorInfo(
            code=REPORTED_FAILURE,
            message=f"`{node or 'the simulation'}` reported failure: {message}",
            node=node,
            exit_status=None,
            log=None,
        ).as_json_value()


def record_rejection(flow: Any, message: str) -> None:
    """Record that the checks of the run rejected its results, with `message` as the reason.

    Use it for a judgment of results that were read, not for results that are missing. The
    `error` in `results.json` is the same as `record_failure` gives it, and an error that the
    run already has (a tool's exit status) stays."""
    flow.reject(message)


def problem(cause: str, fix: str) -> str:
    """Return a failure message of a simulation in its one form: the cause, then what to do."""
    return f"{cause.rstrip('.')}. To fix it, {fix}."


LOG_ADVICE = "read the simulation log in the run directory"


def _cocotb_problem(results: Any) -> str:
    """Return why the verdict of cocotb failed, from the counts it recorded in the results."""
    tests = results.get("cocotb.tests")
    if tests is None:
        return problem("cocotb left no readable test results", LOG_ADVICE)
    errors, failures = results.get("cocotb.errors", 0), results.get("cocotb.failures", 0)
    if errors or failures:
        return problem(
            f"{failures} of {tests} cocotb test(s) failed and {errors} had an error",
            f"fix the failing tests ({LOG_ADVICE})",
        )
    return problem(
        f"cocotb ran no test: {tests} found, {results.get('cocotb.skipped', 0)} skipped",
        "make `cocotb.testcase` name a test that exists, or remove it",
    )


FINISH_ADVICE = "end the testbench with `std.env.finish` (VHDL) or `$finish` (Verilog)"


def _finish_fix(stop_time: Any) -> str:
    """Return what to do about a simulation that stopped by itself, with no finish and no stop."""
    if stop_time is None:
        return f"{FINISH_ADVICE}, or set `stop_time`"
    return f"set `stop_time` to a time before the testbench stops, or {FINISH_ADVICE}"


def _first_event(evidence: SimEvidence, kinds: tuple[str, ...]) -> str:
    """Return the first event of one of `kinds` as text, or an empty string if there is none."""
    event = next((e for e in evidence.events if e.kind in kinds), None)
    if event is None:
        return ""
    where = f" at {event.location}" if event.location else ""
    return f"; the first {event.kind} event{where}: {event.message}"


def _event_fix(rank: int) -> str:
    """Return what to do about events whose highest rank is `rank`.

    A threshold above `rank` allows them. No threshold lies above the fatal rank, so a fatal or
    failure event has to be fixed."""
    if rank >= SEVERITY_RANK["fatal"]:
        return (
            "fix the design or the testbench so that it reports no fatal or failure event; "
            "no `fail_severity` allows one"
        )
    allowed_from = "error" if rank == SEVERITY_RANK["warning"] else "failure"
    return (
        f"fix what the event reports, or set `fail_severity` to `{allowed_from}` if it is "
        "acceptable"
    )


def judge_evidence(flow: Any, evidence: SimEvidence | None, fail_severity: str) -> bool:
    """Judge how a simulation ended, and return whether it passed.

    A simulation passes only if it ended by `$finish` (at any time), by the `stop_time` or
    `max_cycles` the user asked for, or by the testbench's own driver exiting with status 0,
    and no recorded event reached `fail_severity`. An end record that is missing, or that ends
    by a drained event queue, an error or a fatal error, fails. A failure is logged. Unless the
    run already has an error (a tool's exit status), it is also recorded as the `error` in
    `results.json`, in the form of `problem`: the cause and what to do.

    A missing end record is a failure. A present record that shows a bad ending, or an event at
    `fail_severity`, is a judgment of the results: the run is rejected (`Flow.reject`)."""

    def fail(message: str, judged: bool = True) -> bool:
        log.error("%s", message[:1].upper() + message[1:])
        (record_rejection if judged else record_failure)(flow, message)
        return False

    if evidence is None:
        return fail(
            problem(
                "the simulation left no end record, so Xeda cannot tell how it ended", LOG_ADVICE
            ),
            judged=False,
        )
    flow.results["sim.evidence"] = evidence.model_dump(mode="json")
    flow.results["sim.ended_by"] = evidence.ended_by
    if evidence.time is not None and evidence.time_unit is not None:
        try:
            flow.results["sim.time"] = time_in_fs(evidence.time, evidence.time_unit)
        except ValueError:
            pass  # a unit that is no time unit: the time cannot be stated, and the verdict fails
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
        # A bare number is nanoseconds (`SimFlow.Settings.stop_time`); within one tick of the
        # record's precision, since the driver stops at the last whole tick.
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
        asked = f" before the requested stop_time {stop_time}" if stop_time is not None else ""
        stopped = _first_event(evidence, ("stop", "error", "fatal"))
        at = (
            f" (at {readable_time(evidence.time, evidence.time_unit)})"
            if evidence.time is not None
            else ""
        )
        if evidence.ended_by == "drained":
            cause, fix = (
                f"its event queue ran empty without a `$finish`{asked}",
                _finish_fix(stop_time),
            )
        elif evidence.ended_by == "stop_time":
            if stop_time is None:
                cause = "it stopped at a stop time that nobody asked for"
                fix = "set `stop_time` to that time, or remove the stop from the simulator options"
            elif evidence.time is None or evidence.time_unit is None:
                cause = (
                    f"the stop at the requested stop_time {stop_time} could not be confirmed, "
                    "because the end record gave no time or no time unit"
                )
                fix = f"{LOG_ADVICE} to see when it stopped"
            else:
                cause = f"it stopped at a time other than the requested stop_time {stop_time}"
                fix = (
                    "set `stop_time` to a time at which the design has activity, or remove it and "
                    + FINISH_ADVICE
                )
        elif evidence.ended_by == "max_cycles":
            if max_cycles is None:
                cause = "it stopped at a max_cycles limit that nobody asked for"
                fix = "set `max_cycles` to that count, or remove the limit from the testbench"
            else:
                cause = (
                    f"the stop at max_cycles {max_cycles} could not be confirmed: it observed "
                    f"{evidence.cycles} cycles, and a confirmed stop needs exactly {max_cycles} "
                    "cycles and a measured final time"
                )
                fix = (
                    f"check that the design reaches {max_cycles} cycles and that the simulator "
                    f"reports its final time ({LOG_ADVICE})"
                )
        elif evidence.ended_by == "exit":
            cause = f"the testbench's driver exited with status {evidence.exit_code}"
            fix = "make the driver exit with status 0 when the run passes"
        elif evidence.ended_by in ("error", "fatal"):
            noun = "fatal error" if evidence.ended_by == "fatal" else "error"
            article = "a" if evidence.ended_by == "fatal" else "an"
            cause = f"{article} {noun} ended it{stopped}"
            fix = f"fix that {noun} and run again ({LOG_ADVICE})"
        else:
            cause = f"it reported no `$finish`, no stop time and no driver exit{asked}"
            fix = _finish_fix(stop_time)
            if stopped:
                cause += stopped
            else:
                cause += ", so its event queue most likely ran empty"
                fix += f"; if the testbench already ends so, {LOG_ADVICE}"
        return fail(problem(f"the simulation did not end as intended{at}: {cause}", fix))
    threshold = SEVERITY_RANK[fail_severity]
    worst = [e for e in evidence.events if _EVENT_RANK.get(e.kind, 0) >= threshold]
    if worst:
        first = worst[0]
        where = f" at {first.location}" if first.location else ""
        return fail(
            problem(
                f"the simulation reported {len(worst)} event(s) at or above "
                f"fail_severity={fail_severity}, the first: {first.kind}{where}: {first.message}",
                _event_fix(max(_EVENT_RANK[e.kind] for e in worst)),
            )
        )
    return True


class SimFlow(Flow, metaclass=ABCMeta):
    """The base class of all simulation flows."""

    cocotb_sim_name: Optional[str] = None
    #: Advice that a simulator without cocotb support adds to the refusal of a cocotb testbench.
    cocotb_advice: ClassVar[str] = ""

    #: A simulation reads the testbench (`tb`) as well as the RTL.
    design_parts = frozenset({"rtl", "tb"})

    #: The waveform settings (`dataclass.waveform`) that this simulator cannot honor, each with
    #: what to do instead. A simulator writes a waveform through its own mechanism (a run option,
    #: a script command, the HDL's `$dumpfile`), or refuses the setting when planned: no waveform
    #: setting is ignored. `tests/test_waveform_settings.py` classifies every setting of every
    #: simulator.
    unsupported_waveforms: ClassVar[Mapping[str, str]] = {}

    class Settings(Flow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "waveform": "`vcd`",
        }
        timeout: float | None = Field(
            None,
            gt=0,
            description="Stop and fail each subprocess invocation that contains simulation after "
            "this many seconds of wall-clock time. An invocation can combine analysis, "
            "elaboration and simulation. The limit applies to each invocation, not to their "
            "total. None sets no limit.",
        )
        fail_severity: Literal["warning", "error", "failure", "fatal"] = Field(
            "error",
            description="The least severe runtime diagnostic that fails the simulation: "
            "warning, error, failure or fatal. Failure and fatal have the same rank. "
            "Diagnostics below the threshold are recorded. Assertions that the design disables "
            "explicitly keep the documented behavior of the simulator. A nonzero exit status "
            "always fails.",
        )
        vcd: Union[str, Path, None] = Field(
            None,
            description="Write a VCD waveform to this file. `true` writes `dump.vcd`; a name "
            "without an extension gets `.vcd`; `false` or an empty name writes none. A simulator "
            "that cannot write VCD refuses it, and names its own waveform settings.",
            json_schema_extra=waveform("vcd"),
        )
        stop_time: Union[str, int, float, None] = Field(
            None,
            description="Stop the simulation at this simulated time. Accepts a number of "
            'nanoseconds or a string with a unit, e.g. "100us".',
        )
        cocotb: CocotbSettings = Field(
            CocotbSettings(),  # type: ignore
            description="Settings for the cocotb testbench, used when `tb.cocotb` is set.",
        )

    @staticmethod
    def has_cpp_driver(design: Design) -> bool:
        """Return whether the design brings a C++ driver of its own.

        That means `Cpp` sources among its RTL and testbench sources. `yosys_sim` takes any `Cpp`
        source for a main and runs it in place of its own. (`verilator` asks its `main`
        setting.)"""
        return bool(design.sim_sources_of_type(SourceType.Cpp))

    @classmethod
    def runs_without_testbench_top(
        cls, design: Design, settings: Flow.Settings | None = None
    ) -> bool:
        """Return whether this simulator knows what to run for `design` when `tb.top` is not set.

        The question arises when the testbench has sources in a hardware description language.
        `settings` are the settings of the run, when the caller has them. No simulator knows by
        default. A simulator that finds the top of the testbench itself (GHDL's `find-top`), or
        that runs a C++ driver of the design's own (`yosys_sim`; `verilator` with `main: false`,
        which only its settings say), overrides this method."""
        return False

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a waveform setting that this simulator cannot honor (`unsupported_waveforms`).

        Also refuse a name that selects another format. The setting that gets a name decides the
        format, never the name. A name that ends in an extension of another format
        (`format_of_waveform_name`) is refused, naming the setting of that format on this
        simulator when it has one. Without this, a located `wave: x.vcd` of `ghdl_sim` would be
        written under the conventional name of `wave`, in the wrong format, and `true` would
        then be the same run as a location of another format."""
        super().check_settings_supported(settings)
        formats = waveform_fields(type(settings))
        usable = {
            extension: name
            for name, extension in formats.items()
            if name not in cls.unsupported_waveforms
        }
        for name, extension in formats.items():
            value = getattr(settings, name)
            if value is None:
                continue
            if name in cls.unsupported_waveforms:
                raise FlowSettingsException(
                    f"{cls.name} cannot write a waveform from `{name}`: "
                    f"{cls.unsupported_waveforms[name]}"
                )
            text = os.fspath(value)
            written = format_of_waveform_name(text)
            if text.lower().endswith(".gz") and not text.lower().endswith(".vcd.gz"):
                # A gzip name whose stem is a plain format is a compressed file of that format,
                # and only a setting of a compressed format writes one.
                stem = format_of_waveform_name(text[:-3])
                if stem is not None and stem[0] not in COMPRESSED_FORMATS:
                    raise FlowSettingsException(
                        f"`{name}` writes {waveform_format_name(extension)} data, and its name {value} ends in "
                        f".{stem[1]}.gz, a gzip name: {cls.name} writes {waveform_format_name(stem[0])} data "
                        "uncompressed"
                    )
            if written is not None and written[0] != extension:
                other, suffix = usable.get(written[0]), written[1]
                if other is not None:
                    advice = f"give the name to `{other}`, which writes {waveform_format_name(written[0])} data"
                elif COMPRESSED_FORMATS.get(written[0]) == extension:
                    advice = f"{cls.name} writes plain {waveform_format_name(extension)} data: drop the gzip ending"
                else:
                    advice = f"{waveform_format_name(written[0])} data is another format: use a .{extension} name"
                raise FlowSettingsException(
                    f"`{name}` writes {waveform_format_name(extension)} data, and its name {value} ends in "
                    f".{suffix}: {advice}"
                )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design that this simulator cannot run.

        A cocotb testbench needs a simulator that Xeda drives cocotb on (`cocotb_sim_name`). On
        any other, the design would be simulated without cocotb and its tests would never run.
        A testbench written in a hardware description language (`LANGUAGE_TYPES`) needs
        `tb.top`, unless the simulator knows what to run without it
        (`runs_without_testbench_top`). Otherwise the simulator would simulate `rtl.top`, which
        has no stimulus. A simulator with an `sdf` setting needs the instance that an SDF file
        annotates: `sdf.root`, or else `tb.uut`. Every package of `tb.python_packages` must be
        installed in the Python environment that runs Xeda."""
        super().check_design_supported(design, settings)
        for package in design.tb.python_packages:
            try:
                metadata.version(package)
            except metadata.PackageNotFoundError:
                raise FlowSettingsException(
                    f"`tb.python_packages` lists `{package}`, but it is not installed. Install "
                    "it in the Python environment that runs Xeda."
                ) from None
        if design.tb.cocotb and not cls.cocotb_sim_name:
            # API callers may have loaded only their own simulator. Include built-ins too.
            from importlib import import_module

            import_module("xeda.flows")
            supported = sorted(
                {
                    flow_class.name
                    for _, flow_class in registered_flows.values()
                    if issubclass(flow_class, SimFlow) and flow_class.cocotb_sim_name
                }
            )
            raise FlowSettingsException(
                f"{cls.name} cannot run cocotb tests; use one of: {', '.join(supported)}"
                + (f". {cls.cocotb_advice}" if cls.cocotb_advice else "")
            )
        if not design.tb.cocotb and not design.tb.top:
            hdl = [src for src in design.tb.sources if src.type in LANGUAGE_TYPES]
            if hdl and not cls.runs_without_testbench_top(design, settings):
                raise FlowSettingsException(
                    f"{cls.name} needs to know which module is the testbench: `tb.sources` "
                    f"holds {', '.join(src.file.name for src in hdl)} but `tb.top` is not set. "
                    "Set `tb.top` to the testbench's top module."
                )
        sdf = getattr(settings, "sdf", None)
        if isinstance(sdf, SDF) and sdf.delay_items() and not sdf.root and not design.tb.uut:
            raise FlowSettingsException(
                f"{cls.name} annotates the design with an SDF file, and neither `sdf.root` nor "
                "`tb.uut` is set: set `sdf.root`, or set `tb.uut` to the instance name of the "
                "unit under test in the testbench"
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
        # Launched, the flow was checked already; constructed directly, it is checked here.
        self.check_design_supported(self.design, self.settings)

    @property
    def uses_cocotb(self) -> bool:
        """Whether this run is a cocotb simulation, by the simulator and the design alone."""
        return bool(self.cocotb_sim_name and self.design.tb.cocotb)

    @property
    def cocotb(self) -> Optional[Cocotb]:
        """The cocotb support of a cocotb simulation.

        It is made when first used: it is a tool, and making one asks it for its version (a
        process). The constructor of a flow starts no process, because a dry run constructs
        flows."""
        if not hasattr(self, "_cocotb"):
            assert isinstance(self.settings, self.Settings)
            self._cocotb: Optional[Cocotb] = (
                Cocotb(
                    **self.settings.cocotb.model_dump(),
                    sim_name=self.cocotb_sim_name or "",
                    # pydantic-mypy does not see `Tool`'s fields through the
                    # `Cocotb(CocotbSettings, Tool)` diamond; `dockerized` is a real field.
                    dockerized=self.settings.dockerized,  # type: ignore[call-arg]
                )
                if self.uses_cocotb
                else None
            )
        return self._cocotb

    def has_evidence_adapter(self) -> bool:
        """Return whether the simulator of this run can report how it ended.

        This hook describes a capability (`simulation_evidence`). It never exempts a run from
        the evidence rule."""
        return False

    def simulation_evidence(self) -> SimEvidence | None:
        """Return the end record of this run, or None.

        A flow reads the record through `Flow.report_file`, so it takes only a record that this
        run wrote."""
        return None

    def check_results(self) -> bool:
        """Judge the run, by the verdict of cocotb or else by the evidence rule.

        A cocotb testbench is judged by the verdict of cocotb, on every simulator, and needs no
        simulation evidence. Any other run is judged by the evidence rule: missing evidence
        fails, including a flow without an adapter."""
        if self.cocotb:
            passed = self.cocotb.add_results(
                self.results, results_file=self.report_file(self.cocotb.results_xml)
            )
            if not passed:
                # Results that were read and did not pass are a judgment. A run that left no
                # readable results has nothing to judge.
                if self.results.get("cocotb.tests") is None:
                    record_failure(self, _cocotb_problem(self.results))
                else:
                    record_rejection(self, _cocotb_problem(self.results))
            return passed
        assert isinstance(self.settings, self.Settings)
        passed = judge_evidence(self, self.simulation_evidence(), self.settings.fail_severity)
        problem = self.waveform_problem() if passed else None
        if problem is not None:
            record_failure(self, problem)
            return False
        return passed

    def waveform_problem(self) -> str | None:
        """Return why a run that passed still failed to write the waveform its settings asked for.

        Return None when it wrote it. Only a simulator whose testbench names the file can write
        something else. The others make the tool write the name they were given."""
        return None

    def always_runs(self) -> Optional[str]:
        assert isinstance(self.settings, self.Settings)
        if self.uses_cocotb and self.settings.cocotb.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()
