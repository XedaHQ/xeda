from __future__ import annotations

import json
import logging
import sys
from functools import cached_property
from pathlib import Path
from typing import ClassVar, List, Literal, Optional, Union

from ...dataclass import Field, XedaBaseModel, deliverable, field_validator
from ...design import DesignSource, SourceType, VhdlSettings
from ...flow import FlowSettingsException, SimFlow, describe_results
from ...flow.sim import SimEvidence
from ...flow.sim_evidence import parse_nvc_log, read_sim_log
from ...tool import Tool
from ...units import convert_unit

log = logging.getLogger(__name__)


class NvcEnd(XedaBaseModel):
    """Passive VHPI checkpoint; pending activity is distinct from an HDL finish."""

    time: int = Field(ge=0, strict=True, description="Actual end time in femtoseconds.")
    time_unit: Literal["1fs"] = Field(description="NVC's native VHPI time unit.")
    next_time: int | None = Field(
        ge=0, strict=True, description="Next pending event in femtoseconds, or no activity."
    )


class NvcTool(Tool):
    """NVC VHDL simulation, synthesis, and linting tool: https://github.com/nickg/nvc"""

    # docker: Optional[Docker] = None
    executable: str = "nvc"


class Nvc(SimFlow):
    """Simulate VHDL using NVC, with native diagnostics and a passive VHPI end monitor.

    Non-cocotb runs require a C++ compiler and NVC's vhpi_user.h, installed under its prefix
    or on the compiler's include search path. The monitor schedules no simulation events.
    """

    cocotb_sim_name = "nvc"
    #: NVC analyzes VHDL.
    reads_sources = frozenset({SourceType.Vhdl})
    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )
    _sim_log: Path | None = None
    _end_record: Path | None = None

    class Settings(SimFlow.Settings):
        removed_settings: ClassVar[dict[str, str]] = {
            **SimFlow.Settings.removed_settings,
            "exit_severity": "fail_severity",
        }
        one_shot: bool = Field(
            True,
            description="Run the analysis, elaboration, and execution in a single command. Set to False to run each step separately, which might be helpful in.",
        )
        ignore_time: bool = Field(
            False,
            description="Do not check the timestamps of source files when the corresponding design unit is loaded from a library.",
        )
        heap_size: Optional[str] = Field(
            None,
            description="Set the maximum size in bytes of the simulation heap. This memory is "
            "used for temporary process allocations and dynamic allocations by the VHDL 'new' "
            "operator. The optional k, m, or g suffix selects kilobytes, megabytes, or gigabytes. "
            "The default is 16 megabytes.",
        )
        messages: Optional[Literal["full", "compact"]] = Field(
            None,
            description="Select the format used for printing error and informational messages. The default full message format is designed for readability whereas the compact messages can be easily parsed by tools.",
        )
        std_error: Optional[Literal["note", "warning", "error", "failure"]] = Field(
            None,
            description="Print messages at the selected severity or higher to stderr instead of "
            "stdout. The default sends all messages to stderr. Valid levels are note, warning, "
            "error, and failure.",
        )
        werror: bool = Field(
            False, alias="warn_error", description="warnings are always considered as errors"
        )
        work: Optional[str] = Field(None, description="Set the name of the WORK library")
        # clean: bool = Field(False, description="Run 'clean' before elaboration")
        ## analysis flags
        analysis_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -a` when analyzing sources."
        )
        check_synthesis: bool = Field(
            True,
            description="Issue warnings for common coding mistakes that may cause problems during synthesis such as missing signals from process sensitivity lists.",
        )
        psl_in_comments: Optional[bool] = Field(
            None, description="Enable parsing of PSL directives in comments during analysis."
        )
        relaxed: bool = Field(
            False,
            description="Disable certain pedantic LRM conformance checks or rules that were relaxed by later standards.",
        )
        ## elaboration flags
        elab_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -e` during elaboration."
        )
        cover: List[str] = Field([], description="Enable code coverage reporting ")
        cover_spec: Optional[Path] = Field(
            None, description="Specify the coverage specification file"
        )
        jit: bool = Field(
            False,
            description="""Normally nvc compiles all code ahead-of-time during elaboration. The --jit option defers native code generation until run-time where each function will be compiled separately on a background thread once it has been has been executed often enough in the interpreter to be deemed worthwhile. This dramatically reduces elaboration time at the cost of increased memory and CPU usage while the simulation is executing. This option is beneficial for short-running simulations where the performance gain from ahead-of-time compilation is not so significant.""",
        )
        no_collapse: bool = Field(
            False,
            description="Preserve both signals when a port map directly connects signals at "
            "adjacent hierarchy levels. By default, NVC collapses them into the upper-level "
            "signal. Preserving both improves debugging at some performance cost.",
        )
        no_save: bool = Field(
            False,
            description="Do not save the elaborated design to a file. Normally nvc saves the elaborated design to a file in the work library. This file is used by the simulator to load the design quickly. The --no-save option disables this saving and the simulator will have to re-elaborate the design each time it is run. This option is useful for debugging the elaboration process.",
        )
        optimization_level: Optional[Literal[0, 1, 2, 3]] = Field(
            3,
            description="Set the optimization level. The default is 0. Higher levels may improve simulation performance but may also increase elaboration time. The maximum level is 3.",
        )
        print_verbose: bool = Field(
            False, description="Prints resource usage information after each elaboration step."
        )
        ## run flags
        run_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -r` when running the simulation."
        )
        ieee_warnings: Optional[bool] = Field(
            None,
            description="Enable or disable warning messages from the standard IEEE packages. The default is warnings enabled.",
        )
        wave: Union[bool, str, Path, None] = Field(
            None,
            description="Write waveform data to a file. The default is to not write waveform data.",
            json_schema_extra=deliverable("outputs/{design}.fst"),
        )
        wave_format: Optional[Literal["vcd", "fst"]] = Field(
            None,
            description="Generate waveform data in this format. The default is FST if this option is not provided and `wave` is not a filename. If this option is None `wave` is a filename, the format is selected automatically based on the file extension.",
        )
        wave_arrays: Union[int, bool, None] = Field(
            2048,
            description="Include memories and nested arrays in the waveform data. This is disabled by default as it can have significant performance, memory, and disk space overhead. With optional argument N only arrays with up to this many elements will be dumped.",
        )
        wave_include_glob: Optional[str] = Field(
            None,
            description="""Include signals matching this glob pattern in the waveform data.
            Examples: ':top:*:x', '*:x', ':top:sub:*'
            See https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS for more details.""",
        )
        wave_exclude_glob: Optional[str] = Field(
            None,
            description="""Exclude signals matching this glob pattern from the waveform data.
            Examples: ':top:*:x', '*:x', ':top:sub:*'
            See https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS for more details.""",
        )
        stop_delta: Optional[int] = Field(
            None,
            description="Stop the simulation after N delta cycles in the same current time.",
        )
        vhpi: List[Path] = Field([], description="Specify the VHPI libraries to load at startup.")
        shuffle: bool = Field(
            False,
            description="""Run processes in random order.
            The VHDL standard does not specify the execution order of processes and different simulators may exhibit subtly different orderings.
            This option can help to find and debug code that inadvertently depends on a particular process execution order.
            This option should only be used during debug as it incurs a significant performance overhead as well as introducing potentially non-deterministic behavior.""",
        )
        stats: bool = Field(
            False,
            description="Print a summary of the time taken and memory used at the end of the run.",
        )

        @field_validator("stop_time", mode="before")
        @classmethod
        def validate_stop_time(cls, value):
            if value is not None and convert_unit(value, "fs", from_unit="ns") < 0:
                raise ValueError("stop_time must be nonnegative")
            return value

        def runtime_flags(self) -> list[str]:
            """Copy flags and reject competing severity/time-limit controls."""
            threshold = "failure" if self.fail_severity == "fatal" else self.fail_severity
            canonical = f"--exit-severity={threshold}"
            for flag in self.run_flags:
                if flag.startswith("--exit-severity") and flag != canonical:
                    raise FlowSettingsException(
                        f"run_flags {flag!r} conflicts with fail_severity={self.fail_severity}; "
                        "set fail_severity instead"
                    )
                if flag.startswith("--stop-time"):
                    raise FlowSettingsException(
                        f"run_flags {flag!r} bypasses stop_time; set stop_time instead"
                    )
            return [flag for flag in self.run_flags if flag != canonical] + [canonical]

    @cached_property
    def nvc(self):
        return NvcTool()  # pyright: ignore[reportCallIssue]

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)
        ss.runtime_flags()
        if ss.wave and isinstance(ss.wave, (str, Path)):
            ss.wave = self.process_path(ss.wave)

    def global_options(self) -> List[str]:
        cf: List[str] = []
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.ignore_time:
            cf.append("--ignore-time")
        if ss.heap_size:
            cf += ["-H", ss.heap_size]
        cf += [f"-L{p}" for p in ss.lib_paths]
        if ss.messages:
            cf.append(f"--messages={ss.messages}")
        if ss.std_error:
            cf.append(f"--stderr={ss.std_error}")

        # The default standard revision is VHDL-2008
        standard = self.design.language.vhdl.standard
        if standard:
            standard = {"93": "1993", "00": "2000", "02": "2002", "08": "2008", "19": "2019"}.get(
                standard, standard
            )
            assert standard in (
                "1993",
                "2000",
                "2002",
                "2008",
                "2019",
            ), f"Invalid VHDL standard: {standard}"
            cf.append(f"--std={standard}")
        if ss.work:
            cf.append(f"--work={ss.work}")
        return cf

    def init_lib(self) -> None:
        """Initialize the library
        Initialise the working library directory.
        This is not normally necessary as the library will be automatically created when using other commands such as `analyze`.
        """
        self.nvc.run("--init")

    def analyze_flags(self) -> list:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = list(ss.analysis_flags)
        if ss.psl_in_comments:
            flags.append("--psl")
        if ss.relaxed:
            flags.append("--relaxed")
        for k, v in self.design.tb.defines.items():
            assert v is not None
            flags += ["-D", f"{k}={v}"]
        if ss.check_synthesis:
            flags.append("--check-synthesis")
        return flags

    def analyze(self, sources=None) -> None:
        """
        Analyse one or more files into the work library
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if sources is None:
            sources = self.sources_read(tb=True)
        self.nvc.run(*self.global_options(), "-a", *sources, *self.analyze_flags())

    def elaborate_flags(self) -> List[str]:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = list(ss.elab_flags)

        # Note: Generics in internal instances can be overridden by giving the full dotted path to the generic.
        parameters = self.design.rtl.parameters if self.cocotb else self.design.tb.parameters
        for k, v in parameters.items():
            assert v is not None
            flags += ["-g", f"{k}={v}"]

        if ss.optimization_level is not None:
            flags.append(f"-O{ss.optimization_level}")
        if ss.jit:
            flags.append("--jit")
        if ss.no_collapse:
            flags.append("--no-collapse")
        if ss.no_save:
            flags.append("--no-save")
        if ss.print_verbose:
            flags.append("--verbose")
        if ss.cover:
            flags += [f"--cover={','.join(ss.cover)}"]
        if ss.cover_spec:
            flags += ["--cover-spec", str(ss.cover_spec)]
        return flags

    def build_end_monitor(self) -> str:
        """Build in the run directory, discovering headers in the execution environment."""
        self.copy_from_template("sim_record.h")
        source = self.copy_from_template("nvc_end.cpp")
        if self.nvc.dockerized:
            # Container paths must come from the container, never a host installation.
            executable = self.nvc.derive("sh", redirect_stdout=None).run_get_stdout(
                "-c", 'readlink -f "$(command -v nvc)"'
            )
            prefix = Path(executable.strip()).parent.parent if executable else None
            platform = "linux"
        else:
            executable_path = self.nvc.executable_path()
            prefix = executable_path.parent.parent if executable_path else None
            platform = sys.platform
        flags = (
            ["-dynamiclib", "-undefined", "dynamic_lookup"]
            if platform == "darwin"
            else ["-shared", "-fPIC"]
        )
        if prefix is not None:
            flags += ["-I", str(prefix / "include")]
        target = "nvc_end.so"
        self.run_directory.remove(target)
        self.run_directory.writable(target)
        self.nvc.derive("c++", redirect_stdout=None).run(*flags, source, "-o", target, timeout=120)
        return "./" + target

    def has_evidence_adapter(self) -> bool:
        return not self.cocotb

    def simulation_evidence(self) -> SimEvidence | None:
        if self._sim_log is None:
            return None
        evidence = parse_nvc_log(self, self._sim_log)
        if evidence is None:
            return None
        report = self.report_file(self._end_record) if self._end_record is not None else None
        if report is None:
            # A native FINISH/STOP still proves an end if its optional checkpoint is absent.
            return evidence
        text = read_sim_log(self, report)
        try:
            if text is None:
                return None
            end = NvcEnd.model_validate(json.loads(text))
            if evidence.time is not None and evidence.time != end.time:
                raise ValueError("native diagnostic and VHPI end times disagree")
            if end.next_time is not None and end.next_time <= end.time:
                raise ValueError("pending event is not after the end time")
        except (ValueError, TypeError) as exc:
            log.error("Invalid NVC end checkpoint %s: %s", report, exc)
            return None
        evidence.time, evidence.time_unit = end.time, end.time_unit
        if evidence.ended_by == "unknown":
            ss = self.settings
            assert isinstance(ss, self.Settings)
            stop = (
                round(convert_unit(ss.stop_time, "fs", from_unit="ns"))
                if ss.stop_time is not None
                else None
            )
            # A queue with an event beyond the bound confirms a native cutoff. Equality of
            # end time alone proves nothing; judge_evidence separately enforces exact time.
            if (
                stop is not None
                and end.next_time is not None
                and end.next_time > stop
                and end.time <= stop
            ):
                evidence.ended_by = "stop_time"
            elif end.next_time is None:
                evidence.ended_by = "drained"
        return evidence

    def elaborate(self):
        """
        Elaborate a previously analysed top level design unit.
        """
        self.nvc.run(*self.global_options(), "-e", *self.design.sim_tops, *self.elaborate_flags())

    def execute(self, one_shot=False) -> None:
        """Run the simulation"""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        run_flags = ss.runtime_flags()

        if ss.wave:
            if isinstance(ss.wave, bool):
                run_flags += ["--wave"]
            else:
                run_flags += [f"--wave={ss.wave}"]
                if not ss.wave_format and isinstance(ss.wave, (str, Path)):
                    ss.wave = Path(ss.wave)
                    if ss.wave.suffix == ".vcd":
                        ss.wave_format = "vcd"
                    elif ss.wave.suffix == ".fst":
                        ss.wave_format = "fst"
                if ss.wave_format:
                    run_flags.append(f"--format={ss.wave_format}")
            if ss.wave_arrays:
                if isinstance(ss.wave_arrays, bool):
                    run_flags.append("--dump-arrays")
                else:
                    assert isinstance(ss.wave_arrays, int)
                    if ss.wave_arrays > 0:
                        run_flags.append(f"--dump-arrays={ss.wave_arrays}")

        if ss.ieee_warnings is not None:
            run_flags.append("--ieee-warnings=" + ("on" if ss.ieee_warnings else "off"))
        if ss.stop_delta is not None:
            run_flags.append(f"--stop-delta={ss.stop_delta}")
        if ss.stop_time is not None:
            stop_fs = convert_unit(ss.stop_time, "fs", from_unit="ns")
            run_flags.append(f"--stop-time={stop_fs:.0f}fs")
        if ss.stats:
            run_flags.append("--stats")
        if ss.shuffle:
            run_flags.append("--shuffle")

        vhpi = self.resolve_paths_to_design_or_cwd(ss.vhpi)
        # TODO factor out cocotb handling
        if self.design.tb.cocotb and self.cocotb:
            vpi_path = self.cocotb.lib_path(interface="vhpi")
            assert vpi_path, "cocotb VHPI library for NVC was not found"
            vhpi.append(Path(vpi_path))
        loads = [str(path) for path in vhpi]
        if not self.cocotb:
            loads.insert(0, self.build_end_monitor())
        if loads:
            # NVC keeps only the last --load option; its argument is a comma-separated list.
            run_flags.append("--load=" + ",".join(loads))
        tops = self.design.sim_tops
        env = self.cocotb.env(self.design) if self.cocotb else {}
        runtime = self.nvc
        if not self.cocotb:
            self.run_directory.remove("sim.log", "nvc_end.json", "nvc_end.json.tmp")
            self._sim_log = self.run_directory.writable("sim.log")
            self._end_record = self.run_directory.writable("nvc_end.json")
            env["XEDA_NVC_END_RECORD"] = "nvc_end.json"
            runtime = self.nvc.derive(self.nvc.executable, redirect_stdout=None)
        options = dict(env=env, timeout=ss.timeout, tee=self._sim_log, merge_stderr=True)
        if one_shot:
            sources = self.sources_read(tb=True)
            runtime.run(
                *self.global_options(),
                "-a",
                *sources,
                *self.analyze_flags(),
                "-e",
                *tops,
                *self.elaborate_flags(),
                "-r",
                *run_flags,
                **options,
            )
        else:
            runtime.run(
                *self.global_options(),
                "-r",
                *tops,
                *run_flags,
                **options,
            )

    def gen_makefile(self, units: List[str]) -> None:
        self.nvc.run("--make", *units)

    def check_syntax(self, sources: List[DesignSource], vhdl: VhdlSettings) -> None:
        self.nvc.run("--syntax", *sources)

    def run(self) -> None:
        design = self.design
        assert design.tb
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if not ss.one_shot:
            self.analyze()
            self.elaborate()
        self.execute(one_shot=ss.one_shot)
