"""Simulate Verilog and SystemVerilog with Icarus Verilog.

`build_end_monitor` and `icarus_evidence` are also the Icarus backend of `bsc_sim`."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, ClassVar, List, Literal, Optional, Tuple, Union

from ...cocotb import cocotb_toplevel
from ...dataclass import Field, waveform
from ...design import Design, SourceType, language_of
from ...flow import (
    Flow,
    FlowFatalError,
    FlowSettingsException,
    SimFlow,
    describe_artifacts,
    describe_results,
)
from ...flow.metrics import COCOTB_METRICS, program_logs
from ...flow.sim import SimEvent, SimEvidence, whole_femtoseconds
from ...flow.sim_evidence import read_sim_evidence, read_sim_log
from ...tool import Tool
from ...units import Time, Unit
from ...utils import replacing_file, unique
from ..vcs import timescale_text

log = logging.getLogger(__name__)

#: The monitor's end record and the transcript of the simulation, both in the run directory.
END_RECORD = "xeda_end.json"
SIM_LOG = "sim.log"
#: The compiled design that `vvp` runs.
VVP_FILE = "sim.vvp"
#: The VPI module that `build_end_monitor` builds. `vvp` loads a name without a slash from its
#: search path only.
MONITOR = "./iverilog_evidence.vpi"
#: The `-g` generations of `iverilog` 13.0: the values of `language.verilog.standard`.
GENERATIONS = ("1995", "2001", "2001-noconfig", "2005", "2005-sv", "2009", "2012")
#: The end of a run as the monitor records it: `ended_by` and the kind of the record's one event.
RECORD_SHAPES = (("finish", "finish"), ("unknown", "stop"), ("unknown", None), ("stop_time", None))
#: The waveform formats, by the setting that selects each. Each is the `vvp` flag of its name.
WAVEFORMS = ("vcd", "fst")
#: The options that a setting owns, with that setting.
OWNED_COMPILE_FLAGS = {
    "-o": "Xeda names the compiled file",
    "-s": "`tb.top` selects the top modules",
    "-g19": "`language.verilog.standard` selects the generation",
    "-g20": "`language.verilog.standard` selects the generation",
}
#: Text that `iverilog` reads as a number of a given size. Any other text is a string.
SIZED_LITERAL = re.compile(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+")


def parameter_text(value: Any) -> str:
    """Return `value` as the value of a `-P` option."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, str) and not SIZED_LITERAL.fullmatch(value):
        return f'"{value}"'
    return str(value)


def build_end_monitor(flow: Flow, tool: Tool) -> None:
    """Build the VPI registration adapter, with local headers or the container's own."""
    flow.copy_from_template("sim_record.h")
    source = flow.copy_from_template("iverilog_evidence.c")
    if tool.dockerized:
        executable = tool.derive("sh", version_flag=None, minimum_version=None).run_get_stdout(
            "-c", 'readlink -f "$(command -v iverilog)"'
        )
        prefix = Path(executable.strip()).parent.parent if executable else None
        platform = "linux"
    else:
        executable_path = tool.derive(
            "iverilog", version_flag=None, minimum_version=None
        ).executable_path()
        prefix = executable_path.parent.parent if executable_path else None
        platform = sys.platform
    flags = (
        ["-bundle", "-undefined", "dynamic_lookup"]
        if platform == "darwin"
        else ["-shared", "-fPIC"]
    )
    if prefix is not None:
        flags += ["-I", str(prefix / "include" / "iverilog")]
    target = flow.run_directory.writable(MONITOR)
    flow.run_directory.remove(target)
    tool.derive("cc", version_flag=None, minimum_version=None).run(
        *flags, source, "-o", target, timeout=120
    )


def icarus_evidence(flow: Flow) -> SimEvidence | None:
    """Read the end record of the monitor and the native diagnostics of the simulator.

    The task registration reports a quiet `$finish` or `$stop`, and the monitor reports a reached
    `stop_time`. An end with neither is unknown: the event queue ran empty, or something else
    ended the simulator. The native runtime diagnostics supply the severity.
    """
    evidence = read_sim_evidence(flow, END_RECORD)
    text = read_sim_log(flow, SIM_LOG)
    if evidence is None or text is None or "XEDA_ICARUS_RUNTIME_START\n" not in text:
        return None
    event = evidence.events[0] if len(evidence.events) == 1 else None
    if (
        evidence.time is None
        or evidence.time_unit is None
        or len(evidence.events) > 1
        or (event is not None and event.time != evidence.time)
        or (evidence.ended_by, event.kind if event else None) not in RECORD_SHAPES
    ):
        return None
    runtime = text.split("XEDA_ICARUS_RUNTIME_START\n", 1)[1]
    for line in runtime.splitlines():
        diagnostic = re.search(r"(WARNING|ERROR|FATAL): (.+):([0-9]+): ?(.*)", line)
        if diagnostic:
            kind = {"WARNING": "warning", "ERROR": "error", "FATAL": "fatal"}[diagnostic[1]]
            evidence.events.append(
                SimEvent(
                    kind=(
                        "warning" if kind == "warning" else "error" if kind == "error" else "fatal"
                    ),
                    location=diagnostic[2] + ":" + diagnostic[3],
                    message=diagnostic[4],
                )
            )
    # `$fatal` reaches no task of the monitor: its end looks like any other unknown end.
    if evidence.ended_by == "unknown" and any(e.kind == "fatal" for e in evidence.events):
        evidence.ended_by = "fatal"
    return evidence


class IverilogTool(Tool):
    """Icarus Verilog: https://steveicarus.github.io/iverilog/"""

    executable: str = "iverilog"
    version_flag: Optional[List[str]] = ["-V"]
    version_regexps: List[Union[re.Pattern[str], str]] = [r"version (\d+\.\d+)"]
    minimum_version: Optional[Tuple[Union[int, str], ...]] = (13, 0)
    minimum_version_reason: ClassVar[str] = (
        "Xeda's end monitor and the options of this flow are verified from 13.0."
    )


class Iverilog(SimFlow):
    """Simulate a Verilog or SystemVerilog design with Icarus Verilog 13.0 or newer.

    `iverilog` compiles the design and `vvp` runs it. A run without cocotb needs a C compiler and
    the VPI headers of Icarus Verilog, for Xeda's end monitor. The monitor reports how the
    simulation ended, and it stops the simulation at `stop_time`, an option that `vvp` does not
    have. With cocotb, the results of cocotb decide the run, and `stop_time` is refused.

    The simulated top modules are `tb.top`, else `rtl.top`. With cocotb, the simulated top module
    is the one cocotb drives: `tb.cocotb.toplevel`, else `rtl.top`. The parameters are those of
    the first simulated top module: `rtl.parameters` updated by `tb.parameters` when it is the RTL
    top, and `tb.parameters` alone otherwise. Text is a string, unless it is a sized literal such
    as `8'hff`.

    A waveform setting (`vcd` or `fst`) selects the format. Without cocotb, the testbench's
    `$dumpvars` writes the waveform, and `$dumpfile`, if it is called, must keep the name
    `dump.vcd` or `dump.fst`. A run that writes no such file fails. With cocotb, Xeda adds a root
    module that dumps the simulation, and the value of the setting names the file.
    """

    executables = ("iverilog", "vvp", "cc", "sh")

    artifacts_description = describe_artifacts(
        "log",
        **program_logs("vvp", when="when the design compiled"),
        **program_logs("cc", when="when the flow builds the end monitor"),
    )

    cocotb_sim_name = "icarus"

    #: `iverilog` compiles Verilog and SystemVerilog, and finds a header through the directory it
    #: lies in (`-I`).
    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
        }
    )

    results_description = describe_results(
        "sim.evidence",
        "sim.ended_by",
        "sim.time",
        "sim.errors",
        "sim.warnings",
        *COCOTB_METRICS,
    )

    class Settings(SimFlow.Settings):
        compile_flags: List[str] = Field([], description="Extra flags passed to `iverilog`.")
        warn_flags: List[str] = Field(
            ["-Wall"],
            description="Warning flags of `iverilog`, such as `-Wall` and `-Wno-timescale`. "
            "`-Wall` leaves out `-Winfloop`.",
        )
        include_dirs: List[Path] = Field(
            [],
            description="Directories searched for `include` files (`-I`). A relative path is "
            "relative to the design root.",
        )
        verilog_libs: List[Path] = Field(
            [],
            description="Verilog library files (`-l`): their modules are elaborated only when "
            "instantiated. A relative path is relative to the design root.",
        )
        specify: bool = Field(
            False,
            description="Honor specify blocks and `$sdf_annotate` (`-gspecify`). Without it, "
            "Icarus Verilog omits them.",
        )
        delay_selection: Optional[Literal["min", "typ", "max"]] = Field(
            None,
            description="The delay of a `min:typ:max` expression that the simulation uses (`-T`). "
            "Icarus Verilog's default is `typ`, with a warning.",
        )
        time_unit: Annotated[Time, Unit("ns")] = Field(
            Time("1 ns"),
            description="Time unit of a module without a `` `timescale `` directive "
            "(`+timescale+`), a time with a unit. Icarus Verilog takes 1, 10 or 100 of s, ms, us, "
            "ns, ps or fs, such as `1 ns` or `10 ps`.",
        )
        time_resolution: Annotated[Time, Unit("ns")] = Field(
            Time("1 ps"),
            description="Time precision of a module without a `` `timescale `` directive "
            "(`+timescale+`), a time with a unit, no longer than `time_unit`. Icarus Verilog "
            "takes 1, 10 or 100 of s, ms, us, ns, ps or fs, such as `1 ps` or `100 fs`.",
        )
        run_flags: List[str] = Field(
            [], description="Extra flags passed to `vvp`, before the compiled design."
        )
        plusargs: List[str] = Field(
            [],
            description="Arguments for the simulation that `$test$plusargs` and "
            "`$value$plusargs` read, such as `+seed=3`. Each starts with `+`.",
        )
        vpi_modules: List[Path] = Field(
            [],
            description="VPI modules that `vvp` loads (`-m`), as paths of files. A relative path "
            "is relative to the design root.",
        )
        vcd: Union[str, Path, None] = Field(
            None,
            description="Write the waveform as VCD (`vvp -vcd`). `true` selects it. Without "
            "cocotb, the testbench's `$dumpvars` writes the file `dump.vcd`, and any other name or "
            "a location here is refused. With cocotb, the value is the file that Xeda's dump module "
            "writes: `true` writes `dump.vcd`, a name without an extension gets `.vcd`, and a "
            "location is delivered. Only one of `vcd` and `fst` may be set.",
            json_schema_extra=waveform("vcd"),
        )
        fst: Union[str, Path, None] = Field(
            None,
            description="Write the waveform as FST (`vvp -fst`), far more compact than VCD. Used "
            "as `vcd` is: `true` selects the format, and the file is `dump.fst`.",
            json_schema_extra=waveform("fst"),
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what Icarus Verilog cannot do, and a flag that competes with a setting."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        for name in ("time_unit", "time_resolution"):
            try:
                timescale_text(getattr(settings, name))
            except ValueError as error:
                message = str(error).replace("VCS", "Icarus Verilog")
                raise FlowSettingsException(f"`{name}`: {message}") from None
        if settings.time_resolution.to("fs") > settings.time_unit.to("fs"):
            raise FlowSettingsException(
                f"`time_resolution` is {settings.time_resolution}, which is longer than "
                f"`time_unit` {settings.time_unit}"
            )
        if settings.vcd is not None and settings.fst is not None:
            raise FlowSettingsException(
                "iverilog writes one waveform, and vcd and fst are set: keep one of them"
            )
        for library, path in settings.lib_paths:
            if path is None:
                raise FlowSettingsException(
                    f"`lib_paths` names the library {library} without a directory: iverilog finds "
                    "a library through its directory (`-y`). Give the directory too: "
                    f"({library}, <directory of {library}>)"
                )
        for flag in settings.compile_flags:
            for start, owner in OWNED_COMPILE_FLAGS.items():
                if flag.startswith(start):
                    raise FlowSettingsException(
                        f"compile_flags has {flag!r}, but {owner}: remove the flag"
                    )
        if "-s" in settings.run_flags:
            raise FlowSettingsException(
                "run_flags has `-s`, which stops the simulation at an interactive prompt: "
                "remove the flag"
            )
        for argument in settings.plusargs:
            if not argument.startswith("+"):
                raise FlowSettingsException(
                    f"plusargs has {argument!r}, which does not start with `+`: `vcd` and `fst` "
                    "select the waveform format, and `run_flags` holds the flags of `vvp`"
                )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse what Icarus Verilog cannot do with this design and these settings.

        These are a language standard without a generation, a parameter of an inner instance
        (`-P` reaches a root module only, and `iverilog` ignores the rest without an error), a
        design with no top module, `stop_time` with cocotb, whose runs do not load the monitor,
        and a waveform file name without cocotb, since the testbench names the file then. The
        check judges the names as given: a located `vcd` becomes `dump.vcd` once the run starts,
        and the check could not tell it from `true`.
        """
        super().check_design_supported(design, settings)
        standard = language_of(design).verilog.standard
        if standard is not None and standard not in GENERATIONS:
            raise FlowSettingsException(
                f"iverilog has no generation for `language.verilog.standard` {standard}: "
                f"use one of {', '.join(GENERATIONS)}"
            )
        for name, value in {**design.rtl.parameters, **design.tb.parameters}.items():
            # a dotted key of a design file is a table by now
            if "." in name or isinstance(value, Mapping):
                raise FlowSettingsException(
                    f"iverilog cannot set the parameter {name}: `-P` reaches a parameter of the "
                    "top module only. Use `defparam` in the testbench"
                )
        uses_cocotb = design.tb.cocotb is not None
        if not (cocotb_toplevel(design) if uses_cocotb else design.tb.top or design.rtl.top):
            raise FlowSettingsException("no simulation top: set `tb.top` or `rtl.top`")
        if not isinstance(settings, cls.Settings):
            return
        if design.tb.cocotb is not None:
            if settings.stop_time is not None:
                raise FlowSettingsException(
                    "iverilog cannot stop a cocotb simulation at `stop_time`, because Xeda's "
                    "monitor does not run with cocotb: end the test from cocotb, or limit the "
                    "run's wall-clock time with `timeout`"
                )
            return
        for name in WAVEFORMS:
            value = getattr(settings, name)
            if value is not None and Path(value) != Path(f"dump.{name}"):
                raise FlowSettingsException(
                    f"`{name}` names a waveform file, but without cocotb the testbench's "
                    f"`$dumpfile` and `$dumpvars` write the file. Set `{name}: true` (stored as "
                    f"`dump.{name}`, the only name accepted), and call `$dumpvars` in the "
                    "testbench"
                )

    @cached_property
    def iverilog(self) -> IverilogTool:
        return IverilogTool()  # pyright: ignore[reportCallIssue]

    def init(self) -> None:
        super().init()
        assert isinstance(self.settings, self.Settings)
        # a flow built directly skips planning, so check here
        self.check_settings_supported(self.settings)
        self.resolve_lib_paths()

    def has_evidence_adapter(self) -> bool:
        """Return whether Xeda's monitor records how the simulation ended.

        It does not run with cocotb, whose results decide the run."""
        return not self.cocotb

    def simulation_evidence(self) -> SimEvidence | None:
        return icarus_evidence(self)

    def waveform_problem(self) -> str | None:
        """Return a problem text if a run without cocotb wrote no waveform, else None.

        The testbench dumps the waveform, and `dump.<format>` is the one name that `true` and a
        location stand for."""
        assert isinstance(self.settings, self.Settings)
        for name in WAVEFORMS:
            if getattr(self.settings, name) is None:
                continue
            if self.report_file(self.run_path / f"dump.{name}") is None:
                return (
                    f"`{name}` asks for a waveform, and this run wrote no dump.{name}: the "
                    f"testbench must call $dumpvars, and $dumpfile only with dump.{name}"
                )
        return None

    def simulation_tops(self) -> tuple[str, ...]:
        """Return the simulated top modules, the roots of the compilation."""
        if self.cocotb:
            return (str(cocotb_toplevel(self.design)),)
        return tuple(self.design.tb.top) or (str(self.design.rtl.top),)

    def compile_options(self, tops: tuple[str, ...]) -> list[str]:
        """Return the options of `iverilog`, besides the sources."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        design = self.design
        flags = [
            *ss.compile_flags,
            *ss.warn_flags,
            f"-g{language_of(design).verilog.standard or '2012'}",
            "-o",
            VVP_FILE,
        ]
        if ss.specify:
            flags.append("-gspecify")
        if ss.delay_selection:
            flags.append(f"-T{ss.delay_selection}")
        flags += [f"-s{top}" for top in tops]
        # Testbench defines override RTL defines. `-DNAME` alone would define it as 1.
        defines = {**design.rtl.defines, **design.tb.defines}
        flags += [f"-D{k}=" if v is None else f"-D{k}={v}" for k, v in defines.items()]
        include_dirs = [*ss.include_dirs, *design.header_dirs(tb=True)]
        flags += [f"-I{d}" for d in unique(self.resolve_paths_to_design_or_cwd(include_dirs))]
        flags += [f"-y{path}" for _, path in ss.lib_paths]
        for library in self.resolve_paths_to_design_or_cwd(ss.verilog_libs):
            flags += ["-l", str(library)]
        # `rtl.parameters` are the RTL top's: they apply only when it is the simulated top
        top = tops[0]
        parameters = dict(design.tb.parameters)
        if top == design.rtl.top:
            parameters = {**design.rtl.parameters, **parameters}
        flags += [f"-P{top}.{k}={parameter_text(v)}" for k, v in parameters.items()]
        return flags

    def compile_design(self, tops: tuple[str, ...], dump_file: Path | None) -> None:
        """Compile the design into `VVP_FILE`, with Xeda's dump module when `dump_file` is set."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        command_file = self.run_directory.writable("xeda_iverilog.cmd")
        unit, resolution = timescale_text(ss.time_unit), timescale_text(ss.time_resolution)
        with replacing_file(command_file, encoding="utf-8") as f:
            f.write(f"+timescale+{unit}/{resolution}\n")
        sources = [
            src
            for src in self.sources_read(tb=True)
            if src.type in (SourceType.Verilog, SourceType.SystemVerilog)
        ]
        flags = self.compile_options(tops)
        dump_sources: list[Path] = []
        if dump_file is not None:
            flags.append("-siverilog_dump")
            dump_sources.append(
                self.copy_from_template(
                    "iverilog_dump.v",
                    top=tops[0],
                    dump_file=json.dumps(str(dump_file), ensure_ascii=False),
                )
            )
        compile_log = self.run_directory.writable("iverilog_compile.log")
        self.iverilog.run(
            *flags,
            "-c",
            command_file.name,
            *sources,
            *dump_sources,
            tee=compile_log,
            merge_stderr=True,
        )
        # `iverilog` keeps the default of a `-P` value that it could not read, or cuts it short,
        # and exits with 0.
        report = self.report_file(compile_log)
        for line in report.read_text(errors="replace").splitlines() if report else ():
            if line.startswith("<command line>: error:"):
                raise FlowFatalError(f"iverilog refused a parameter: {line}")

    def run(self) -> None:
        """Compile the design with `iverilog` and simulate it with `vvp`."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        tops = self.simulation_tops()
        # at most one format is set (`check_settings_supported`)
        wave = next((name for name in WAVEFORMS if getattr(ss, name) is not None), None)
        dump_file = None
        if wave is not None:
            dump_file = self.process_path(getattr(ss, wave))
            # a previous run's file (or a link a tool left at its name) is never this run's
            self.run_directory.remove(dump_file)
        if not self.cocotb:
            build_end_monitor(self, self.iverilog)
        self.compile_design(tops, dump_file if self.cocotb else None)

        args = ["-i", "-n", *ss.run_flags]
        args += [
            arg
            for module in self.resolve_paths_to_design_or_cwd(ss.vpi_modules)
            for arg in ("-m", str(module))
        ]
        if self.cocotb:
            library = self.cocotb.lib_path("vpi")
            if library is None:
                raise FlowFatalError("cocotb has no VPI library for Icarus Verilog")
            args += ["-m", library]
            env = self.cocotb.env(self.design)
            sim_log = None
        else:
            args += ["-m", MONITOR]
            self.run_directory.remove(END_RECORD, f"{END_RECORD}.tmp", SIM_LOG)
            env = {"XEDA_END_RECORD": END_RECORD}
            if ss.stop_time is not None:
                env["XEDA_STOP_TIME_FS"] = str(whole_femtoseconds(ss.stop_time))
            sim_log = self.run_directory.writable(SIM_LOG)
        extended = [*ss.plusargs, *([f"-{wave}"] if wave else [])]
        self.iverilog.derive("vvp", version_flag=None, minimum_version=None).run(
            *args,
            VVP_FILE,
            *extended,
            env=env,
            timeout=ss.timeout_in_seconds(),
            tee=sim_log,
            merge_stderr=sim_log is not None,
        )
