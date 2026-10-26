from __future__ import annotations

import json
import logging
import sys
from functools import cached_property
from pathlib import Path
from typing import ClassVar, List, Literal, Optional, Union

from ...dataclass import Field, XedaBaseModel, field_validator, waveform
from ...design import DesignSource, SourceType, VhdlSettings
from ...flow import Flow, FlowSettingsException, SimFlow, describe_results
from ...flow.metrics import COCOTB_METRICS
from ...flow.sim import SimEvidence
from ...flow.sim_evidence import parse_nvc_log, read_sim_log
from ...tool import Tool
from ...units import convert_unit, count
from ...utils import LOCATION_FORMS

log = logging.getLogger(__name__)


class NvcEnd(XedaBaseModel):
    """The checkpoint that the passive VHPI end monitor writes: the end time and the next event."""

    time: int = Field(ge=0, strict=True, description="Actual end time in femtoseconds.")
    time_unit: Literal["1fs"] = Field(description="NVC's native VHPI time unit.")
    next_time: int | None = Field(
        ge=0,
        strict=True,
        description="Time of the next pending event in femtoseconds, or null if none.",
    )


class NvcTool(Tool):
    """NVC, the VHDL compiler and simulator: https://github.com/nickg/nvc"""

    # docker: Optional[Docker] = None
    executable: str = "nvc"


class Nvc(SimFlow):
    """Simulate VHDL with NVC, using its native diagnostics and a passive VHPI end monitor.

    A run without cocotb needs a C++ compiler and NVC's `vhpi_user.h`, installed under its prefix
    or on the compiler's include search path. The monitor schedules no simulation events.
    """

    executables = ("nvc", "sh", "c++")

    cocotb_sim_name = "nvc"
    #: NVC analyzes VHDL.
    reads_sources = frozenset({SourceType.Vhdl})
    results_description = describe_results(
        "sim.evidence",
        "sim.ended_by",
        "sim.time",
        "sim.errors",
        "sim.warnings",
        *COCOTB_METRICS,
    )
    _sim_log: Path | None = None
    _end_record: Path | None = None

    class Settings(SimFlow.Settings):
        removed_settings: ClassVar[dict[str, str]] = {
            **SimFlow.Settings.removed_settings,
            "exit_severity": "fail_severity",
            "wave": "`fst`",
            "wave_format": "`fst` or `vcd`",
            "optimization_flags": "`optimization_level` (`nvc -e -O`), or `elab_flags` for other options of "
            "`nvc -e`",
        }
        one_shot: bool = Field(
            True,
            description="Run analysis, elaboration and execution in one `nvc` command. If false, "
            "run each step as a separate command.",
        )
        ignore_time: bool = Field(
            False,
            description="Do not check the timestamps of source files when the corresponding "
            "design unit is loaded from a library.",
        )
        heap_size: Optional[str] = Field(
            None,
            description="Maximum size of the simulation heap, in bytes. The heap holds the "
            "temporary allocations of processes and the allocations of the VHDL `new` operator. "
            "A `k`, `m` or `g` suffix selects kilobytes, megabytes or gigabytes. The default is "
            "16 megabytes.",
        )
        messages: Optional[Literal["full", "compact"]] = Field(
            None,
            description="Format of error and informational messages. The default, `full`, is "
            "easy to read. The `compact` format is easy for tools to parse.",
        )
        std_error: Optional[Literal["note", "warning", "error", "failure"]] = Field(
            None,
            description="Print messages at the selected severity or higher to stderr instead of "
            "stdout. The default sends all messages to stderr. The levels are `note`, `warning`, "
            "`error` and `failure`.",
        )
        werror: bool = Field(
            False,
            alias="warn_error",
            description="Treat the warnings of the analysis as errors (`-Werror`).",
        )
        work: Optional[str] = Field(None, description="Name of the WORK library.")
        # clean: bool = Field(False, description="Run 'clean' before elaboration")
        # Settings for analysis.
        analysis_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -a` when analyzing sources."
        )
        check_synthesis: bool = Field(
            True,
            description="Warn about common coding mistakes that can cause problems in synthesis, "
            "such as signals missing from a process sensitivity list.",
        )
        psl_in_comments: Optional[bool] = Field(
            None, description="Enable parsing of PSL directives in comments during analysis."
        )
        relaxed: bool = Field(
            False,
            description="Disable some pedantic LRM conformance checks and rules that later "
            "standards relaxed.",
        )
        # Settings for elaboration.
        elab_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -e` during elaboration."
        )
        cover: List[str] = Field([], description="Kinds of code coverage to collect (`--cover`).")
        cover_spec: Optional[Path] = Field(None, description="Coverage specification file.")
        jit: bool = Field(
            False,
            description="Defer native code generation to run time (`--jit`). Normally NVC "
            "compiles all code ahead of time during elaboration. With this setting, NVC compiles "
            "each function on a background thread after the interpreter has executed it often "
            "enough. This greatly reduces elaboration time, at the cost of more memory and CPU "
            "use while the simulation runs. It helps short simulations, where ahead-of-time "
            "compilation gains little.",
        )
        no_collapse: bool = Field(
            False,
            description="Preserve both signals when a port map directly connects signals at "
            "adjacent hierarchy levels. By default, NVC collapses them into the upper-level "
            "signal. Preserving both improves debugging at some performance cost.",
        )
        no_save: bool = Field(
            False,
            description="Do not save the elaborated design to a file (`--no-save`). Normally NVC "
            "saves the elaborated design in the work library, and the simulator loads it from "
            "there quickly. Without the file, the simulator elaborates the design again on each "
            "run. This helps when debugging elaboration.",
        )
        optimization_level: Optional[Literal[0, 1, 2, 3]] = Field(
            3,
            description="Optimization level (`-O`), from 0 to 3. Higher levels can speed up the "
            "simulation but slow down elaboration.",
            json_schema_extra=count(),
        )
        print_verbose: bool = Field(
            False, description="Print resource usage after each elaboration step (`--verbose`)."
        )
        # Settings for the run.
        run_flags: List[str] = Field(
            [], description="Extra flags passed to `nvc -r` when running the simulation."
        )
        ieee_warnings: Optional[bool] = Field(
            None,
            description="Turn the warnings of the standard IEEE packages on or off. NVC's "
            "default is on.",
        )
        fst: Union[str, Path, None] = Field(
            None,
            description="Write the waveform as an FST file: a name in the run directory, or a "
            f"location it is delivered to ({LOCATION_FORMS}). `true` writes `dump.fst`; a name "
            "without an extension gets `.fst`. NVC writes one waveform: set only one of `fst` and "
            "`vcd`.",
            json_schema_extra=waveform("fst"),
        )
        wave_arrays: Union[int, bool, None] = Field(
            2048,
            description="Include memories and nested arrays in the waveform. True includes all "
            "of them, an integer N only arrays with up to N elements, and false none. Large "
            "arrays cost performance, memory and disk space.",
            json_schema_extra=count(),
        )
        wave_include_glob: Optional[str] = Field(
            None,
            description="Include the signals that match this glob pattern in the waveform. "
            "Examples: `:top:*:x`, `*:x`, `:top:sub:*`. See "
            "https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS. Needs `vcd` or `fst`.",
        )
        wave_exclude_glob: Optional[str] = Field(
            None,
            description="Exclude the signals that match this glob pattern from the waveform. "
            "Examples: `:top:*:x`, `*:x`, `:top:sub:*`. See "
            "https://www.nickg.me.uk/nvc/manual.html#SELECTING_SIGNALS. Needs `vcd` or `fst`.",
        )
        stop_delta: Optional[int] = Field(
            None,
            description="Stop the simulation after this many delta cycles at the same time.",
            json_schema_extra=count(),
        )
        vhpi: List[Path] = Field([], description="VHPI libraries to load at startup.")
        shuffle: bool = Field(
            False,
            description="Run processes in random order. The VHDL standard does not specify the "
            "execution order of processes, and simulators differ in subtle ways. This setting "
            "helps to find code that depends on a particular order. Use it only for debugging: "
            "it slows the simulation considerably and can make the result non-deterministic.",
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
            """Return `run_flags` with the `--exit-severity` that `fail_severity` selects.

            Refuse a run flag that sets a competing severity or a stop time.
            """
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

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse run flags that compete with `fail_severity` or `stop_time`, a library in
        `lib_paths` without a directory, and waveform settings that NVC cannot honor.

        NVC writes one waveform, with `--wave` (`vcd` or `fst`). The settings that refine the
        waveform need one.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        settings.runtime_flags()
        for name, path in settings.lib_paths:
            if path is None:
                raise FlowSettingsException(
                    f"`lib_paths` names the library {name} without a directory: nvc has no way "
                    "to name a library without its directory. Give the library directory too: "
                    f"({name}, <directory of {name}>)"
                )
        if settings.vcd is not None and settings.fst is not None:
            raise FlowSettingsException(
                "nvc writes one waveform, and vcd and fst are set: keep one of them"
            )
        if settings.vcd is None and settings.fst is None:
            for name in ("wave_include_glob", "wave_exclude_glob", "wave_arrays"):
                if getattr(settings, name) != cls.Settings.model_fields[name].default:
                    raise FlowSettingsException(
                        f"`{name}` refines the waveform, and neither `vcd` nor `fst` is set"
                    )

    @cached_property
    def nvc(self):
        return NvcTool()  # pyright: ignore[reportCallIssue]

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)
        # a flow built directly skips planning, so check here
        self.check_settings_supported(ss)
        self.resolve_lib_paths()
        if ss.fst is not None:
            ss.fst = self.process_path(ss.fst)

    def global_options(self) -> List[str]:
        cf: List[str] = []
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.ignore_time:
            cf.append("--ignore-time")
        if ss.heap_size:
            cf += ["-H", ss.heap_size]
        # `--map=NAME:PATH` maps the library NAME to the library directory PATH. `-L PATH` adds the
        # directory PATH, which holds library directories, to the library search path.
        for name, path in ss.lib_paths:
            cf += [f"--map={name}:{path}"] if name else ["-L", str(path)]
        if ss.messages:
            cf.append(f"--messages={ss.messages}")
        if ss.std_error:
            cf.append(f"--stderr={ss.std_error}")

        # NVC's default standard is VHDL-2008.
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
        """Initialize the working library directory.

        This is not normally needed: other commands, such as `analyze`, create the library.
        """
        self.nvc.run("--init")

    def analyze_flags(self) -> list:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = list(ss.analysis_flags)
        if ss.werror:
            flags.append("-Werror")
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
        """Analyze `sources` into the work library (default: all sources of the design)."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if sources is None:
            sources = self.sources_read(tb=True)
        self.nvc.run(*self.global_options(), "-a", *sources, *self.analyze_flags())

    def elaborate_flags(self) -> List[str]:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        flags = list(ss.elab_flags)

        # The full dotted path to a generic overrides a generic of an internal instance.
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
        """Build the end monitor in the run directory and return the path of its library.

        The VHPI headers come from the environment where NVC runs.
        """
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
        report = (
            self.report_file(self._end_record, optional=True)
            if self._end_record is not None
            else None
        )
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
        """Elaborate the top units of the simulation, which `analyze` has analyzed."""
        self.nvc.run(*self.global_options(), "-e", *self.design.sim_tops, *self.elaborate_flags())

    def execute(self, one_shot=False) -> None:
        """Run the simulation."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        run_flags = ss.runtime_flags()

        # one waveform (`check_settings_supported`): `--format=vcd` selects VCD, else NVC writes FST
        target: Union[str, Path, None] = ss.fst
        if ss.vcd is not None:
            target = self.process_path(ss.vcd)
        if target:
            run_flags.append(f"--wave={target}")
            if ss.vcd is not None:
                run_flags.append("--format=vcd")
            if ss.wave_include_glob:
                run_flags.append(f"--include={ss.wave_include_glob}")
            if ss.wave_exclude_glob:
                run_flags.append(f"--exclude={ss.wave_exclude_glob}")
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
