"""Simulate Verilog and SystemVerilog with xezim: https://github.com/aionhw/xezim"""

import json
import logging
import re
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union

from ..cocotb import cocotb_toplevel
from ..dataclass import Field, waveform
from ..design import Design, SourceType, language_of
from ..flow import Flow, FlowSettingsException, SimFlow, describe_artifacts
from ..flow.flow import describe_results
from ..flow.metrics import COCOTB_METRICS, ArtifactSpec, MetricSpec
from ..flow.sim import SimEvent, SimEvidence, time_in_fs, whole_femtoseconds
from ..flow.sim_evidence import read_sim_log
from ..tool import Tool
from ..units import Time, count
from ..utils import LOCATION_FORMS

log = logging.getLogger(__name__)

#: The transcript of a run, with the native end line and diagnostics of the simulator.
SIM_LOG = "sim.log"
#: The file where xezim writes coverage. It exists only when there is coverage to write.
COVERAGE_DB = "xezim_cov.json"

_END_LINE = re.compile(r".*Simulation finished at time (\d+)( \(\$finish called\))?")
_INTERRUPTED = re.compile(r".*\[xezim\] interrupted at time \d+\b.*")
_CAP_REACHED = re.compile(r".*\[xezim\]\[hang-report\] simulation reached --max-time \(.*")
_DIAGNOSTIC = re.compile(r".*?\*\* (Error|Warning|Fatal)(?:: (.*))?")
_DIAGNOSTIC_CONTEXT = re.compile(r"   Time: (\d+)  Scope: (.*)")
_TIMESCALES_HEADER = re.compile(r"=== module timescales \(\d+ modules?\) ===")
_TIMESCALE_ENTRY = re.compile(r"  .+?\s+\d+\s*[a-z]+ / (\d+)\s*(s|ms|us|ns|ps|fs)\b.*")
_NOTHING_ANNOTATED = "[SDF] annotated 0 signals with delays"

#: The options that the flow sets itself, each with the setting to use instead.
OWNED_FLAGS: Dict[str, str] = {
    "-s": "`tb.top`",
    "-do": "`stop_time`",
    "--max-time": "`stop_time`",
    "-l": "the log of the run",
    "--log": "the log of the run",
    "-logfile": "the log of the run",
    "--tee": "the log of the run",
    "--wave": "`vcd`",
    "-vcd": "`vcd`",
    "--fst": "`fst`",
    "--parse": "another flow: `xezim` always simulates",
    "--compile": "another flow: `xezim` always simulates",
    "--no-sim": "another flow: `xezim` always simulates",
    "--error-exit": "`fail_severity`",
    "--sdf": "`sdf_file`",
    "--dump-timescales": "nothing: the flow always passes it",
    "-uvm": "`uvm`",
    "--uvm": "`uvm`",
    "--x-warn": "`x_warn`",
    "--x-warn-limit": "`x_warn_limit`",
    "--code-coverage": "`code_coverage`",
    "--code-coverage-scope": "`code_coverage_scope`",
    "+seed": "`random_seed`",
    "-sv_seed": "`random_seed`",
}
#: The settings that only refine another setting, each with the setting that it refines.
REFINEMENTS: Tuple[Tuple[str, str], ...] = (
    ("sdf_type", "sdf_file"),
    ("x_warn_limit", "x_warn"),
    ("code_coverage_scope", "code_coverage"),
)
#: The option of each value of `language.verilog.standard`.
STANDARD_FLAGS: Dict[str, str] = {
    "17": "--sv2017",
    "2017": "--sv2017",
    "23": "--sv2023",
    "2023": "--sv2023",
}


def _value_text(value: Any) -> str:
    """Return a parameter or macro value as xezim reads it: a boolean is 1 or 0."""
    return str(int(value)) if isinstance(value, bool) else str(value)


def _whole_nanoseconds(time: Time) -> Optional[int]:
    """Return `time` as a positive whole number of nanoseconds, or None if it is not one."""
    nanoseconds, remainder = divmod(whole_femtoseconds(time), 10**6)
    return nanoseconds if nanoseconds > 0 and remainder == 0 else None


def _tick(lines: List[str]) -> Optional[str]:
    """Return the simulation tick: the finest precision of `--dump-timescales`, or None."""
    start = next((i for i, line in enumerate(lines) if _TIMESCALES_HEADER.fullmatch(line)), None)
    if start is None:
        return None
    precisions = []
    for line in lines[start + 1 :]:
        entry = _TIMESCALE_ENTRY.fullmatch(line)
        if entry is None:
            break
        precisions.append(entry[1] + entry[2])
    return min(precisions, key=lambda unit: time_in_fs(1, unit), default=None)


def parse_xezim_log(flow: Flow, path: Union[str, Path] = SIM_LOG) -> Optional[SimEvidence]:
    """Translate the transcript of a run into evidence, or return None if it shows no end.

    The native end line comes after all output of the design, `final` blocks included, so only
    the last one counts: output of the design cannot stand in for it, and `$write` without a
    newline can only come before it on its line. All times are ticks of the finest precision of
    the design, which `--dump-timescales` prints before the run. xezim reports `$stop` as
    `$finish`, so a `$stop` reads as a finish here. A signal also ends a run with exit status 0
    and a claim of `$finish`, so the interruption line takes precedence over that claim."""
    text = read_sim_log(flow, path)
    if text is None:
        return None
    lines = text.splitlines()
    end = next((i for i in reversed(range(len(lines))) if _END_LINE.fullmatch(lines[i])), None)
    if end is None:
        return None
    closing = _END_LINE.fullmatch(lines[end])
    assert closing is not None
    time, finished = int(closing[1]), closing[2] is not None
    tick = _tick(lines[:end])
    evidence = SimEvidence(ended_by="unknown", time=time, time_unit=tick)
    fatal = interrupted = capped = False
    for index, line in enumerate(lines[:end]):
        interrupted = interrupted or _INTERRUPTED.fullmatch(line) is not None
        capped = capped or _CAP_REACHED.fullmatch(line) is not None
        diagnostic = _DIAGNOSTIC.fullmatch(line)
        context = _DIAGNOSTIC_CONTEXT.fullmatch(lines[index + 1]) if diagnostic else None
        if diagnostic is None or context is None:
            continue
        severity, message = diagnostic.groups()
        kind: Literal["fatal", "error", "warning"] = (
            "fatal" if severity == "Fatal" else "error" if severity == "Error" else "warning"
        )
        fatal = fatal or kind == "fatal"
        described = f"{message + ' ' if message else ''}(in {context[2]})"
        evidence.events.append(SimEvent(kind=kind, time=int(context[1]), message=described))
    stop_time = getattr(flow.settings, "stop_time", None)
    if fatal:
        evidence.ended_by = "fatal"
    elif interrupted:
        evidence.ended_by = "error"
        message = "a signal interrupted the simulation"
        evidence.events.append(SimEvent(kind="error", time=time, message=message))
    elif finished:
        evidence.ended_by = "finish"
        evidence.events.append(SimEvent(kind="finish", time=time))
    elif capped:
        evidence.ended_by = "stop_time"
    elif stop_time is not None and (
        tick is None or time_in_fs(time, tick) == whole_femtoseconds(stop_time)
    ):
        evidence.ended_by = "stop_time"
    else:
        evidence.ended_by = "drained"
    return evidence


class XezimTool(Tool):
    """The xezim simulator, which compiles and simulates in one command."""

    executable: str = "xezim"
    # `xezim -V`: "xezim version 0.11.4"
    version_flag: Optional[List[str]] = ["-V"]
    version_regexps: List[Union[re.Pattern[str], str]] = [
        r"\bxezim version (?P<version>\d+(\.\d+)+)"
    ]


class Xezim(SimFlow):
    """Simulate a Verilog or SystemVerilog design with xezim, a 4-state event-driven simulator.

    One process compiles and simulates. The flow supports cocotb testbenches, UVM, SDF timing
    annotation, code coverage and VCD and FST waveforms. A run passes on the native end line of
    this run and its diagnostics. xezim ends a run at a `$stop` as at a `$finish`, and reports
    both the same way, so a Verilog `$stop` passes here, unlike on other simulators. A
    testbench that reports a failure must use `$error` or `$fatal`. Verified with xezim 0.11.2
    and 0.11.4.
    """

    executables = ("xezim",)
    locator_variables = ("XEZIM_UVM_DIR", "XEZIM_UVM_VERSION")

    #: cocotb has no library of its own for xezim. Its library for VCS is a plain VPI client.
    cocotb_sim_name = "vcs"

    #: xezim reads Verilog and SystemVerilog, and finds a header through its directory (`-I`).
    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
        }
    )

    artifacts_description = describe_artifacts(
        "log",
        coverage=ArtifactSpec(
            "Code, functional and assertion coverage, as xezim writes it.",
            "when the setting `code_coverage` is set",
        ),
    )

    results_description = describe_results(
        "sim.evidence",
        "sim.ended_by",
        "sim.time",
        "sim.errors",
        "sim.warnings",
        *COCOTB_METRICS,
        **{
            f"coverage.{kind}": MetricSpec(
                f"Percentage of {name} that the simulation covered, within "
                "`code_coverage_scope`. Present when `code_coverage` selects it and the scope "
                "has something of this kind.",
                "number",
            )
            for kind, name in (
                ("statement", "statements"),
                ("branch", "branches"),
                ("toggle", "signal bits toggled in both directions"),
            )
        },
    )

    class Settings(SimFlow.Settings):
        fst: Optional[Union[str, Path]] = Field(
            None,
            description="Write the waveform as an FST file: a name in the run directory, or a "
            f"location it is delivered to ({LOCATION_FORMS}). `true` writes `dump.fst`; a name "
            "without an extension gets `.fst`. The testbench needs no `$dumpvars`.",
            json_schema_extra=waveform("fst"),
        )
        random_seed: Union[Literal["random"], Annotated[int, Field(ge=1)]] = Field(
            1,
            description="Seed of the simulator's random numbers (`+seed=`): the same seed gives "
            'the same run. "random" draws a new seed on every run, so the flow then always runs '
            "and is never reused.",
            json_schema_extra=count(),
        )
        uvm: bool = Field(
            False,
            description="Add the UVM library (`-uvm`) that the environment variable "
            "`XEZIM_UVM_DIR` names. `XEZIM_UVM_VERSION` picks a release of a checkout that holds "
            "several. Xeda tracks these two values, not the files of the library, so an edit in "
            "place does not make a run stale.",
        )
        sdf_file: Optional[Path] = Field(
            None,
            description="SDF file that back-annotates delays (`--sdf`). xezim has no region for "
            "it: each `INSTANCE` is a dotted path from the simulation top, such as `uut.u1`. A "
            "run that annotates no signal fails.",
        )
        sdf_type: Literal["min", "typ", "max"] = Field(
            "typ", description="SDF delay type to back-annotate. Needs `sdf_file`."
        )
        code_coverage: List[Literal["stmt", "branch", "toggle"]] = Field(
            [],
            description="Kinds of code coverage to collect (`--code-coverage`): `stmt`, `branch` "
            "and `toggle`. The percentages are the results `coverage.statement`, "
            "`coverage.branch` and `coverage.toggle`, and the artifact `coverage` holds the "
            "details.",
        )
        code_coverage_scope: List[str] = Field(
            [],
            description="Limit code coverage to these instance subtrees and packages, such as "
            "`tb.dut` (`--code-coverage-scope`). Needs `code_coverage`.",
        )
        x_warn: bool = Field(
            False,
            description="Warn when a signal that held 0 or 1 takes an X value after time 0, "
            "and name its drivers (`--x-warn`). The warnings are in the log and no result "
            "depends on them.",
        )
        x_warn_limit: Optional[int] = Field(
            None,
            ge=0,
            description="Stop reporting X warnings after this many; 0 sets no limit "
            "(`--x-warn-limit`). None keeps the default of xezim. Needs `x_warn`.",
            json_schema_extra=count(),
        )
        xezim_flags: List[str] = Field(
            [],
            description="Extra arguments for `xezim`, placed before the source files. A file "
            "that an argument names is no input of the run.",
        )

    @cached_property
    def xezim(self) -> XezimTool:
        return XezimTool()  # pyright: ignore[reportCallIssue]

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what xezim cannot take: a `stop_time` that is no positive whole number of
        nanoseconds, an option that the flow sets itself, and a setting that only refines
        another one that is not set."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if settings.stop_time is not None and _whole_nanoseconds(settings.stop_time) is None:
            raise FlowSettingsException(
                f"`stop_time` is {settings.stop_time}, and xezim stops a run only at a positive "
                "whole number of nanoseconds"
            )
        for flag in settings.xezim_flags:
            name = flag.split("=", 1)[0]
            if name in OWNED_FLAGS:
                raise FlowSettingsException(
                    f"`xezim_flags` holds `{flag}`, which the flow sets itself: use "
                    f"{OWNED_FLAGS[name]}"
                )
        for name, refined in REFINEMENTS:
            default = cls.Settings.model_fields[name].default
            if getattr(settings, name) != default and not getattr(settings, refined):
                raise FlowSettingsException(f"`{name}` refines `{refined}`: set `{refined}`")

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design that xezim cannot run: one with a Verilog standard that xezim does not
        parse, and a cocotb testbench that asks for a VCD file, which only the `$dumpfile` of an
        HDL testbench can name."""
        super().check_design_supported(design, settings)
        standard = language_of(design).verilog.standard
        if standard is not None and standard not in STANDARD_FLAGS:
            raise FlowSettingsException(
                f"xezim parses IEEE 1800-2017 and 1800-2023, and `language.verilog.standard` is "
                f"{standard}: use one of {', '.join(sorted(STANDARD_FLAGS))}"
            )
        if design.tb.cocotb is not None and getattr(settings, "vcd", None) is not None:
            raise FlowSettingsException(
                "xezim writes VCD only from the `$dumpfile` of an HDL testbench, and this "
                "testbench is cocotb's: use `fst`"
            )

    def init(self) -> None:
        super().init()
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # a flow built directly is not planned, so check here
        self.check_settings_supported(ss)
        if ss.sdf_file:
            ss.sdf_file = self.process_path(ss.sdf_file, resolve_to=self.design.root)

    def _simulated_tops(self) -> List[str]:
        """Return the top modules: the one that cocotb drives, or else those of the design."""
        if self.cocotb:
            top = cocotb_toplevel(self.design)
            return [top] if top else []
        return [top for top in self.design.sim_tops if top]

    def _parameters(self, top: str) -> Dict[str, Any]:
        """Return the parameters of `top`. `rtl.parameters` apply only when it is the RTL top."""
        tb_parameters = dict(self.design.tb.parameters)
        if top == self.design.rtl.top:
            return {**self.design.rtl.parameters, **tb_parameters}
        return tb_parameters

    def run(self) -> None:
        """Compile and simulate the design, in one `xezim` process."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        design = self.design
        self.run_directory.remove(SIM_LOG, COVERAGE_DB)
        # `run -all` lifts the default limit of 100 ms of simulated time. `--max-time` would end
        # the run at its last event, not at the requested time.
        run_for = "-all" if ss.stop_time is None else f"{_whole_nanoseconds(ss.stop_time)}ns"
        args: List[Any] = ["--dump-timescales", "-do", f"run {run_for}"]
        standard = language_of(design).verilog.standard
        if standard is not None:
            args.append(STANDARD_FLAGS[standard])
        defines = {**design.rtl.defines, **design.tb.defines}
        args += [f"-D{k}" if v is None else f"-D{k}={_value_text(v)}" for k, v in defines.items()]
        for directory in design.header_dirs(tb=True):
            args += ["-I", directory]
        for top in self._simulated_tops():
            args += ["-s", top]
            # `-g/<top>/` sets the top alone. `-G` would also rewrite the instances of the design.
            args += [
                f"-g/{top}/{name}={_value_text(value)}"
                for name, value in self._parameters(top).items()
                if value is not None
            ]
        args.append(f"+seed={ss.random_seed}")
        if ss.vcd is not None:
            args.append("--wave")
        if ss.fst is not None:
            args += ["--fst", ss.fst]
        if ss.uvm:
            args.append("-uvm")
        if ss.sdf_file:
            args += ["--sdf", ss.sdf_file, f"--sdf-{ss.sdf_type}"]
        if ss.code_coverage:
            args.append("--code-coverage=" + ",".join(ss.code_coverage))
        if ss.code_coverage_scope:
            args += ["--code-coverage-scope", ",".join(ss.code_coverage_scope)]
        if ss.x_warn:
            args.append("--x-warn")
        if ss.x_warn_limit is not None:
            args += ["--x-warn-limit", ss.x_warn_limit]
        env: Dict[str, Any] = {}
        if self.cocotb:
            library = self.cocotb.lib_path()
            assert library, "the cocotb VPI library was not found"
            args += ["--vpi-lib", library]
            env = self.cocotb.env(design)
        sources = [
            src
            for src in self.sources_read(tb=True)
            if src.type in (SourceType.Verilog, SourceType.SystemVerilog)
        ]
        self.xezim.run(
            *args,
            *ss.xezim_flags,
            *(str(src) for src in sources),
            env=env,
            timeout=ss.timeout_in_seconds(),
            tee=self.run_directory.writable(SIM_LOG),
            merge_stderr=True,
        )

    def has_evidence_adapter(self) -> bool:
        return not self.cocotb

    def simulation_evidence(self) -> Optional[SimEvidence]:
        return parse_xezim_log(self)

    def waveform_problem(self) -> Optional[str]:
        """Return a problem text if a run did not write the waveform it asked for, else None.

        Only the testbench can write a VCD file, so it must use the name of the setting. xezim
        writes no FST file for a design with no signal to dump, and goes on."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.vcd is not None and self.report_file(ss.vcd) is None:
            return (
                f"`vcd` is set, and the testbench wrote no {ss.vcd} in this run: it must call "
                f'$dumpfile("{ss.vcd}") and $dumpvars, or use `fst`, which needs neither'
            )
        if ss.fst is not None and self.report_file(ss.fst) is None:
            return f"`fst` is set, and xezim wrote no {ss.fst}: the design has no signal to dump"
        return None

    def parse_reports(self) -> bool:
        """Read the coverage percentages that the settings asked for."""
        assert isinstance(self.settings, self.Settings)
        if not self.settings.code_coverage:
            return True
        report = self.report_file(COVERAGE_DB)
        if report is None:
            return False
        try:
            coverage = json.loads(report.read_text(encoding="utf-8"))["code_coverage"]
            for kind in coverage["kinds"]:
                percent = coverage[kind]["percent"]
                if percent is not None:  # None: the scope has nothing of this kind to cover
                    self.results[f"coverage.{kind}"] = float(percent)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.error("Cannot read coverage from %s: %s", report, error)
            return False
        self.artifacts["coverage"] = COVERAGE_DB
        return True

    def check_results(self) -> bool:
        """Judge the run, and reject a run that was to annotate delays and annotated none."""
        assert isinstance(self.settings, self.Settings)
        passed = super().check_results()
        if passed and self.settings.sdf_file:
            text = read_sim_log(self, SIM_LOG)
            if text is not None and _NOTHING_ANNOTATED in text.splitlines():
                self.reject(
                    "xezim annotated no signal from `sdf_file`. To fix it, make each INSTANCE "
                    "in the SDF file a dotted path from the simulation top, such as `uut.u1`"
                )
                return False
        return passed

    def always_runs(self) -> Optional[str]:
        assert isinstance(self.settings, self.Settings)
        if self.settings.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()
