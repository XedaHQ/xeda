import json
import logging
import os
import re
import string
from glob import escape as glob_escape
from glob import glob
from pathlib import Path
from random import randint
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Optional, Tuple, Union

from ...cocotb import cocotb_toplevel
from ...dataclass import WORKING, Field, field_validator, waveform
from ...design import Design, SourceType
from ...flow import (
    Flow,
    FlowFatalError,
    FlowSettingsException,
    SimFlow,
    describe_results,
)
from ...flow.metrics import COCOTB_METRICS
from ...flow.sim import SimEvent, SimEvidence
from ...tool import Docker, NonZeroExitCode, Tool
from ...units import count
from ...utils import replacing_copy, unique

log = logging.getLogger(__name__)

#: The end record that Xeda's hooks write in `sim_dir` (`templates/xeda_hooks.cpp`).
END_RECORD = "xeda_end.json"
#: The macros that make Verilator leave its runtime functions to Xeda's hooks (`verilated.cpp`).
HOOK_MACROS = (
    "VL_USER_FINISH",
    "VL_USER_STOP",
    "VL_USER_FATAL",
    "VL_USER_WARN",
    "VL_USER_STOP_MAYBE",
)
#: The make fragment that gives verilated.cpp, and nothing else, the declaration in
#: `xeda_hooks.h` (`templates/xeda_hooks.mk`). make reads it when the environment variable
#: MAKEFILES names it: Verilator's `--build` and bsc's link step run make themselves.
HOOKS_MAKEFILE = "xeda_hooks.mk"
#: An end record's event kinds, as `SimEvent` kinds; `stop_maybe` is classified by its `maybe`.
_RECORD_EVENT_KINDS = {
    "stop": "stop",
    "fatal": "fatal",
    "finish": "finish",
    "warning": "warning",
}


def parse_end_record(path: Path) -> SimEvidence:
    """Return the evidence in the end record that Xeda's Verilator hooks write.

    The hooks are in `templates/xeda_hooks.cpp`. A `stop_maybe` event is an error (`$error`, a
    failed assertion, `$stop`) when `maybe` is true and a fatal error (`$fatal`) when it is
    false. Raise `ValueError` for an unreadable or malformed record."""
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8", errors="replace"))
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} holds no end record: {data!r}")
    raw_events = data.get("events", [])
    if not isinstance(raw_events, list):
        raise ValueError(f"{path}: `events` is not a list")
    events = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: an event is not a mapping: {raw!r}")
        kind = raw.get("kind")
        if kind == "stop_maybe":
            maybe = raw.get("maybe")
            if not isinstance(maybe, bool):
                raise ValueError(f"{path}: a stop_maybe event without `maybe`: {raw!r}")
            kind = "error" if maybe else "fatal"
        elif kind in _RECORD_EVENT_KINDS:
            kind = _RECORD_EVENT_KINDS[kind]
        else:
            raise ValueError(f"{path}: unknown event kind {kind!r}")
        file = raw.get("file")
        events.append(
            SimEvent.model_validate(
                {
                    "kind": kind,
                    "time": raw.get("time"),
                    "location": f"{file}:{raw.get('line')}" if file else None,
                    "message": raw.get("msg") or "",
                }
            )
        )
    # a record's content is checked as the model's (pydantic's ValidationError is a ValueError)
    return SimEvidence.model_validate(
        {
            "ended_by": data.get("ended_by"),
            "time": data.get("time"),
            "time_unit": data.get("time_unit"),
            "exit_code": data.get("exit_code"),
            "events": events,
        }
    )


#: The file Verilator's own main writes with `--coverage`, in the model's working directory, and
#: the model argument that names another.
COVERAGE_FILE = "coverage.dat"
COVERAGE_FILE_ARGUMENT = "+verilator+coverage+file+"

#: The oldest Verilator the flow drives. From 5.048, the context reports that a `final` block is
#: running (`executingFinal()`), which the hooks need to tell the end of the simulation from a
#: `$finish` in a `final` block. From 5.044, the main that `--main` generates passes `--threads` to
#: the model's context.
MIN_VERILATOR_VERSION = (5, 48)

#: The levels that follow the `-O` of Verilator (`verilator --help`: -O0, -O1, -O2, -O3).
OPTIMIZATION_LEVELS = ("0", "1", "2", "3")

#: The trace formats, by the setting that selects each (Verilator's `--trace-<format>`).
TRACE_FORMATS = ("vcd", "fst", "saif")


class VerilatorTool(Tool):
    """Verilator, in the oldest release that Xeda's flows support."""

    docker: Optional[Docker] = Docker(image="xeda-verilator")  # type: ignore
    executable: str = "verilator"
    minimum_version: Optional[Tuple[Union[int, str], ...]] = MIN_VERILATOR_VERSION
    minimum_version_reason: ClassVar[str] = (
        "Before 5.048, Xeda's hooks cannot tell a `$finish` in a `final` block from the end of "
        "the simulation, so a run that drained could pass."
    )


class Verilator(SimFlow):
    """Simulate a Verilog or SystemVerilog design with Verilator 5.048 or newer.

    Verilator compiles the design to C++ or SystemC and builds a native executable. The flow
    supports cocotb testbenches, plain C++ and SystemC harnesses, and VCD, FST and SAIF
    waveform tracing.

    Three drivers run the model. By default, Verilator's own `--main` runs an HDL top (the
    `main` setting). With `main: false`, the design's own C++ `main` or `sc_main` runs it. With
    cocotb, cocotb's own main runs it, and cocotb's results decide the run.

    The simulated top module is `tb.top`, else `rtl.top`. A design with HDL testbench sources
    needs `tb.top` when Verilator supplies its main. With cocotb, the simulated top module is the
    one cocotb drives: `tb.cocotb.toplevel`, else `rtl.top`. The parameters (`-G`) are
    `rtl.parameters` updated by `tb.parameters` when the simulated top module is the RTL top,
    and `tb.parameters` alone otherwise.

    Without cocotb, a run passes only on evidence of how the simulation ended. Xeda's hooks
    record that evidence in Verilator's runtime (`xeda_end.json` in `sim_dir`). Under Verilator's
    main, the run passes when the testbench ends it with `$finish` and nothing reported reaches
    `fail_severity`. An event queue that runs empty without a `$finish` fails. Under the design's
    own main, the run passes when that main exits with status 0 and nothing reported reaches
    `fail_severity`. The flow refuses `stop_time`: end the testbench with `$finish`, or use
    `timeout`.

    A waveform setting (`vcd`, `fst` or `saif`) builds the model with that trace format. Without
    cocotb, the testbench's `$dumpfile` and `$dumpvars` (or the design's own main) write and name
    the file. Xeda opens no waveform.
    """

    executables = ("verilator", "make")

    cocotb_sim_name = "verilator"

    #: Verilator compiles the Verilog, SystemVerilog and C++ sources, and finds a header through
    #: the directory it lies in (`-I`).
    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Cpp,
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

    #: The exit status of this run's model, once it has returned (0) or failed.
    _driver_exit_code: int | None = None
    _model_log: Path | None = None

    class Settings(SimFlow.Settings):
        sim_dir: Path = Field(
            Path("sim_build"),
            description="Directory, relative to the run directory, where Verilator writes the "
            "generated C++ and the built model.",
            json_schema_extra=WORKING,
        )
        compile_args: List[str] = Field(
            [], description="Extra arguments passed to the `verilator` command itself."
        )
        cflags: List[str] = Field(
            [], description="Extra flags for the C++ compiler (`verilator -CFLAGS`)."
        )
        warn_flags: List[str] = Field(
            ["-Wall"],
            description="Verilator lint and warning flags, such as `-Wall` and `-Wno-WIDTH`. "
            "Verilator lints more strictly than most simulators.",
        )
        warnings_fatal: bool = Field(
            False,
            description="Make a Verilator warning fail the run. Without it, the flow passes "
            "`-Wno-fatal`.",
        )
        include_dirs: List[Path] = Field(
            [],
            description="Directories searched for `include` files and missing modules (`-I`). "
            "Every file under them is an input of the run.",
        )
        optimize: Union[bool, str, int] = Field(
            True,
            description="Optimization level for the generated model. `true` passes `-O3`, "
            "`false` passes no `-O` option, and a level of 0, 1, 2 or 3 passes `-O` with that "
            "level. Write the level only: `2`, not `-O2`.",
            json_schema_extra=count(),
        )

        @field_validator("optimize")
        @classmethod
        def _optimize_is_a_level(cls, value: bool | str | int) -> bool | str | int:
            """Accept `true`, `false` and a level that follows the `-O` of Verilator."""
            if isinstance(value, bool) or str(value) in OPTIMIZATION_LEVELS:
                return value
            if (
                isinstance(value, str)
                and value.startswith("-O")
                and value[2:] in OPTIMIZATION_LEVELS
            ):
                raise ValueError(
                    f"`optimize` is the level that follows `-O`: write `{value[2:]}`, not `{value}`"
                )
            levels = f"{', '.join(OPTIMIZATION_LEVELS[:-1])} or {OPTIMIZATION_LEVELS[-1]}"
            raise ValueError(
                f"`optimize` is true, false or a level of {levels} (the `-O` of Verilator), "
                f"not `{value}`"
            )

        timing: bool = Field(
            True,
            description="Enable Verilator's `--timing` support for delays and non-blocking "
            "event controls. Requires a compiler with C++20 coroutine support. Verilator's "
            "required C++ standard takes precedence over `-std=` in `cflags`. When it is off, "
            "Verilator ignores a delay and warns about it (`STMTDLY` for a statement, "
            "`ASSIGNDLY` for an assignment), so a testbench's `#100; $finish` ends at time 0.",
        )
        model_args: List[str] = Field(
            default=[], description="Arguments passed to the model executable."
        )
        verilog_libs: List[Path] = Field(
            [],
            description="Verilog library files (`-v`): their modules are elaborated only when "
            "instantiated.",
        )
        build: bool = Field(
            True,
            description="Compile and link the generated C++ into an executable model. If false, "
            "Verilator only generates the sources. To build them, run make in `sim_dir` with the "
            "environment variable `MAKEFILES=xeda_hooks.mk`, which gives verilated.cpp the "
            "declaration of Xeda's hooks.",
        )
        vpi: bool = Field(
            False,
            description="Enable VPI support in the model. VPI-based testbenches need it. Cocotb "
            "enables it without this setting.",
        )
        no_deps: bool = Field(
            True,
            description="Pass `--no-MMD`, so Verilator does not emit make dependency files.",
        )
        generate_systemc: bool = Field(
            False,
            description="Generate a SystemC model (`--sc`) instead of a plain C++ one.",
        )
        generate_executable: bool = Field(
            True,
            description="Link a standalone executable (`--exe`). Without it, Verilator builds a "
            "library to embed. The `main` setting makes Verilator generate `main()`.",
        )
        compiler: Optional[str] = Field(
            None,
            description='C++ compiler that builds the model, such as "clang++". The default is '
            "Verilator's own choice.",
        )
        random_init: bool = Field(
            False,
            description="Randomize the initial value of uninitialized signals, which surfaces "
            "reset bugs a zero-initialized model would hide. The values come from `random_seed`. "
            "Off by default, as in Verilator; turning it on can hang a testbench that relies on "
            "an uninitialized clock, such as bsc's `main.v`. See `x_initial`.",
        )
        random_seed: Union[Literal["random"], Annotated[int, Field(ge=1)]] = Field(
            1,
            description="Seed of the random initialization (`+verilator+seed+`): the same seed "
            'gives the same run. "random" draws a new seed on every run, so the flow then always '
            "runs and is never reused.",
            json_schema_extra=count(),
        )
        x_initial: str = Field(
            "0",
            description='How uninitialized values are set at time 0: "unique" (random per '
            'run), "0", or "fast".',
        )
        x_assign: str = Field(
            "0",
            description='How explicit assignments of X are resolved: "unique" (random per '
            'run), "0", "1", or "fast".',
        )
        main: Optional[bool] = Field(
            None,
            description="Let Verilator generate the `main` that runs the model (`--main`). Unset, "
            "it is true, unless the testbench uses cocotb, whose own main runs the model. Set "
            "it to false when the design's C++ sources hold `main` or `sc_main`; a C++ source "
            "with `main` unset is a helper (DPI), linked into the model. True is refused with "
            "cocotb.",
        )
        vcd: Union[str, Path, None] = Field(
            None,
            description="Build the model with VCD tracing (`--trace-vcd`). `true` selects it; "
            "without cocotb, the testbench's `$dumpfile` and `$dumpvars` (or the design's own "
            "main) write and name the file, and a name or location here is refused. With cocotb, "
            "the value is the file cocotb writes: `true` writes `dump.vcd`, a name without an "
            "extension gets `.vcd`, and a location is delivered. Only one of `vcd`, `fst` and "
            "`saif` may be set.",
            json_schema_extra=waveform("vcd"),
        )
        fst: Union[None, str, Path] = Field(
            None,
            description="Build the model with FST tracing (`--trace-fst`), far more compact than "
            "VCD for long simulations. Used as `vcd` is: `true` selects the format.",
            json_schema_extra=waveform("fst"),
        )
        saif: Union[None, str, Path] = Field(
            None,
            description="Build the model with SAIF tracing (`--trace-saif`), for power "
            "estimation. Used as `vcd` is: `true` selects the format.",
            json_schema_extra=waveform("saif"),
        )
        threads: int = Field(
            0,
            description="Number of threads of the model. 0 builds a model that is not "
            "thread-safe, 1 a thread-safe single-threaded model, and 2 or more a multithreaded "
            "model.",
            json_schema_extra=count(),
        )
        trace_underscore: bool = Field(
            False,
            description="Include signals whose names begin with an underscore in traces "
            "(`--trace-underscore`). Off, as in Verilator.",
        )
        trace_structs: bool = Field(
            False,
            description="Trace structs and packed arrays with their field names, as separate "
            "signals instead of flat vectors (`--trace-structs`). Off, as in Verilator.",
        )
        trace_threads: Optional[int] = Field(
            None,
            description="Number of threads used to write the waveform. Null leaves Verilator's "
            "default.",
            json_schema_extra=count(),
        )
        trace_max_width: Optional[int] = Field(
            None,
            description="Do not trace signals wider than this many bits. Null leaves "
            "Verilator's default.",
            json_schema_extra=count(),
        )
        trace_max_array: Optional[int] = Field(
            None,
            description="Do not trace arrays with more than this many elements. Null leaves "
            "Verilator's default.",
            json_schema_extra=count(),
        )

        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "clean_before_run": "the --clean option",
            "optimization_flags": "`optimize` for the `-O` of Verilator, or `cflags` for the C++ compiler "
            "(`-CFLAGS`)",
        }

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what Verilator cannot do in any mode.

        These are stopping at a simulated time, more than one trace format, and a trace option
        with no trace.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if settings.stop_time is not None:
            raise FlowSettingsException(
                "verilator cannot stop at a simulated time: end the testbench with `$finish`, or "
                "limit the run's wall-clock time with `timeout`"
            )
        formats = [name for name in TRACE_FORMATS if getattr(settings, name)]
        if len(formats) > 1:
            raise FlowSettingsException(
                f"verilator takes one trace format, and {', '.join(formats)} are set: "
                "keep one of them"
            )
        if not formats:
            for name in (
                "trace_underscore",
                "trace_structs",
                "trace_threads",
                "trace_max_width",
                "trace_max_array",
            ):
                if getattr(settings, name):
                    raise FlowSettingsException(
                        f"`{name}` refines the trace, and no trace format is set: "
                        "set `vcd`, `fst` or `saif`"
                    )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse what Verilator cannot do with this design and these settings.

        These are a `main` beside cocotb, whose own main runs the model; SystemC without the
        design's own `sc_main`; and a waveform file name, since without cocotb the testbench's
        `$dumpfile` (or the design's own C++ main) names the file. The check judges the trace
        names as given: a located `vcd` becomes `dump.vcd` once the run starts, and the check
        could not tell it from `true`.
        """
        super().check_design_supported(design, settings)
        if not isinstance(settings, cls.Settings):
            return
        if design.tb.cocotb:
            if settings.main:
                raise FlowSettingsException(
                    "main is true, but cocotb's own main runs the model: leave `main` unset"
                )
            return
        if settings.generate_systemc and settings.main is not False:
            raise FlowSettingsException(
                "generate_systemc needs `main: false` and the design's own `sc_main` among its "
                "C++ sources: Verilator's `--main` generates a C++ main"
            )
        for name in TRACE_FORMATS:
            value = getattr(settings, name)
            if value is not None and Path(value) != Path(f"dump.{name}"):
                raise FlowSettingsException(
                    f"`{name}` names a waveform file, but without cocotb Xeda writes no "
                    "waveform: the testbench's `$dumpfile` (or the design's own C++ main) names "
                    f"and writes the file. Set `{name}: true` (stored as `dump.{name}`, the only "
                    f"name accepted) to build the model with {name.upper()} tracing, and call "
                    "`$dumpfile` and `$dumpvars` in the testbench"
                )

    def init(self) -> None:
        super().init()
        assert isinstance(self.settings, self.Settings)
        # a flow built directly skips planning (its design was checked when it was built)
        self.check_settings_supported(self.settings)

    def waveform_problem(self) -> str | None:
        """Return a problem text if a run without cocotb wrote no waveform, else None.

        The model is built to dump a waveform. The testbench names the file, and `dump.<format>`
        is the one name that `true` and a location stand for. A model built for one format whose
        testbench dumps to another name leaves no `dump.<format>`."""
        assert isinstance(self.settings, self.Settings)
        if self.cocotb:
            return None
        for name in TRACE_FORMATS:
            if getattr(self.settings, name) is None:
                continue
            if self.report_file(self.run_path / f"dump.{name}") is None:
                return (
                    f"The model was built with {name.upper()} tracing (`{name}`), and it wrote "
                    f"no dump.{name} in this run: the testbench must call "
                    f'$dumpfile("dump.{name}") and $dumpvars'
                )
        return None

    def always_runs(self) -> Optional[str]:
        assert isinstance(self.settings, self.Settings)
        if self.settings.random_init and self.settings.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()

    def has_evidence_adapter(self) -> bool:
        """Return whether Xeda's hooks record how the simulation ended.

        They do not under cocotb, whose own main runs the model and whose results decide the
        run."""
        return not self.cocotb

    def simulation_top(self) -> str:
        """Return the simulated top module.

        With cocotb, it is the module that cocotb drives (`cocotb_toplevel`: `tb.cocotb.toplevel`,
        else the RTL top). Otherwise it is the first `tb.top`, else the RTL top."""
        if self.cocotb:
            top = cocotb_toplevel(self.design)
        else:
            top = next(iter(self.design.tb.top), None) or self.design.rtl.top
        if not top:
            raise FlowSettingsException("no simulation top: set tb.top or rtl.top")
        return top

    @classmethod
    def check_run_directory(cls, settings: Flow.Settings, run_path: Path) -> None:
        """Refuse a build directory whose path has whitespace.

        Verilator's makefile stops with "GNU Make cannot build in directories containing spaces"
        before it compiles anything, because make splits a path at whitespace. The model is built
        in `sim_dir`, a name inside the run directory. Make builds in the physical directory, so
        the check judges the run directory by what it leads to. It judges `sim_dir` by its name,
        because the launcher removes a link left at that name before the run and never follows
        it."""
        super().check_run_directory(settings, run_path)
        assert isinstance(settings, cls.Settings)
        build = os.path.normpath(os.path.join(os.path.realpath(run_path), settings.sim_dir))
        if any(char in string.whitespace for char in build):
            raise FlowSettingsException(
                f"{cls.name} cannot build in {build}: Verilator's GNU Make build cannot work in "
                "a directory whose path has whitespace. Use a run root (`--run-root`) and a "
                "`sim_dir` whose paths have none."
            )

    @classmethod
    def runs_without_testbench_top(
        cls, design: Design, settings: Flow.Settings | None = None
    ) -> bool:
        """Return whether the design's own C++ main runs the model, so no testbench top is needed.

        That is the case when `main` is false, whatever HDL the testbench also holds (a bound
        checker, a helper). Only the settings say. Verilator's own main runs `tb.top`, else
        `rtl.top`, which has no stimulus. A design checked without its settings is not exempt."""
        return isinstance(settings, cls.Settings) and settings.main is False

    def verilator_main(self) -> bool:
        """Return whether Verilator's own `--main` runs the model.

        It does unless cocotb is used (its own main runs the model) or `main` is false."""
        assert isinstance(self.settings, self.Settings)
        return not self.cocotb and self.settings.main is not False

    def user_main(self) -> bool:
        """Return whether the design's own C++ `main` or `sc_main` runs the model."""
        return not self.cocotb and not self.verilator_main()

    def simulation_evidence(self) -> SimEvidence | None:
        """Return the evidence from the end record that this run's model wrote, else None.

        Only a record of this run counts (`report_file`). If the design's own main ended the
        simulation by exiting, the evidence has the status that main exited with."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        path = self.report_file(self.run_path / self.settings.sim_dir / END_RECORD)
        if path is None:
            return None
        try:
            evidence = parse_end_record(path)
        except ValueError as e:
            log.error("The simulation's end record %s cannot be read: %s", path, e)
            return None
        if ss.fail_severity == "warning" and self._model_log is not None:
            # `$warning` reaches no hook: Verilator prints it as `[time] %Warning: ...`
            log_file = self.report_file(self._model_log, optional=True)
            if log_file is not None:
                for line in log_file.read_text(errors="replace").splitlines():
                    if re.match(r"\[\d+\] %Warning:", line):
                        evidence.events.append(SimEvent(kind="warning", message=line))
        if evidence.ended_by == "exit" and evidence.exit_code is None and self.user_main():
            evidence.exit_code = self._driver_exit_code
        return evidence

    def run(self):
        """Compile the design and run its Verilator simulation."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        self.rm_dep_files()

        verilator = VerilatorTool()  # pyright: ignore[reportCallIssue]

        top = self.simulation_top()
        sim_dir = Path(ss.sim_dir)
        verilated_bin = os.path.join(ss.sim_dir, "top")
        # Without cocotb, Xeda's hooks record how the simulation ends, under Verilator's main
        # and under the design's own main alike.
        hooked = not self.cocotb
        generated_main = self.verilator_main()

        compile_args = ss.compile_args
        # `rtl.parameters` are the RTL top's: they apply only when it is the simulated top
        if top == self.design.rtl.top:
            parameters = {**self.design.rtl.parameters, **self.design.tb.parameters}
        else:
            parameters = dict(self.design.tb.parameters)
        # Testbench defines override RTL defines. Merge them into a new mapping, because every
        # flow of the run shares the design, which is already hashed.
        defines: dict[str, Any] = {**self.design.rtl.defines, **self.design.tb.defines}

        args: List[Any] = []

        if ss.generate_systemc:
            args += ["-sc"]
        else:
            args += ["-cc"]

        if generated_main:
            args.append("--main")

        if ss.generate_executable:
            args += ["--exe"]

        if ss.build:
            args.append("--build")

        args += [
            "-j",  # Parallelism for --build-jobs/--verilate-jobs
            0,  # 0: auto
        ]

        for wf in ss.warn_flags:
            args.append(wf)

        if not ss.warnings_fatal:
            args.append("-Wno-fatal")

        if ss.compiler:
            args += [
                "--compiler",
                ss.compiler,
            ]
        if ss.no_deps:
            args += [
                "--no-MMD",
            ]

        args += ["-Mdir", ss.sim_dir]
        args += ["--top-module", top, "--prefix", "Vtop", "-o", "top"]

        if self.cocotb or ss.vpi:
            args.append("--vpi")
            args.append("--public-flat-rw")

        args.append("--timing" if ss.timing else "--no-timing")

        # suppress unhelpful warnings
        args += [
            "-Wno-DECLFILENAME",
        ]

        if ss.threads:
            args += ["--threads", ss.threads]

        if ss.optimize is True:
            args += ["-O3"]
        elif ss.optimize is not False:
            args += [f"-O{ss.optimize}"]

        args += [
            "--x-initial",
            ss.x_initial,
            "--x-assign",
            ss.x_assign,
        ]

        cflags = list(ss.cflags)  # copy

        if self.cocotb:
            if verilator.docker is not None:
                cocotb_docker = verilator.docker.model_copy(
                    deep=True,
                    update=dict(command=[self.cocotb.executable]),
                )
                # `model_copy` carries the source's `cached_property` cache over verbatim, and
                # `Docker.name` is derived from `command`, which `update` just replaced.
                cocotb_docker.invalidate_cached_properties()
                self.cocotb.docker = cocotb_docker

        model_args = [*ss.model_args]
        # at most one format is set (`check_settings_supported`)
        for name in TRACE_FORMATS:
            if getattr(ss, name):
                args.append(f"--trace-{name}")
                trace = getattr(ss, name)
                break
        else:
            trace = None
        if trace:
            if self.cocotb:
                # cocotb's own main opens the file, at the name Xeda gives it
                trace = str(self.process_path(trace, subs_vars=True))
                model_args += ["--trace", "--trace-file", trace]
                log.info("Will generate trace file %s", trace)
            else:
                log.info(
                    "The model is built with tracing: the testbench's $dumpfile and $dumpvars "
                    "write the waveform"
                )
            if ss.trace_threads:
                args += ["--trace-threads", ss.trace_threads]
            if ss.trace_underscore:
                args.append("--trace-underscore")
            if ss.trace_structs:
                args.append("--trace-structs")
            if ss.trace_max_width:
                args += [
                    "--trace-max-width",
                    ss.trace_max_width,
                ]
            if ss.trace_max_array:
                args += [
                    "--trace-max-array",
                    ss.trace_max_array,
                ]

        if cflags:
            args += ["-CFLAGS", " ".join(cflags)]

        hooks: list[Any] = []
        build_env: dict[str, str] | None = None
        if hooked:
            self.run_file(sim_dir).mkdir(parents=True, exist_ok=True)
            self.copy_from_template("xeda_hooks.h", script_filename=sim_dir / "xeda_hooks.h")
            self.copy_from_template(HOOKS_MAKEFILE, script_filename=sim_dir / HOOKS_MAKEFILE)
            hooks.append(
                self.copy_from_template(
                    "xeda_hooks.cpp", script_filename=sim_dir / "xeda_hooks.cpp"
                )
            )
            for macro in HOOK_MACROS:
                args += ["-CFLAGS", f"-D{macro}"]
            if generated_main:
                # at exit, the hooks then tell `$finish` from an event queue that ran empty
                args += ["-CFLAGS", "-DXEDA_VERILATOR_MAIN"]
            # by name: make splits MAKEFILES at a space, and runs in `sim_dir`, where the file is
            build_env = {"MAKEFILES": HOOKS_MAKEFILE}

        env: dict[str, str] | None = None

        # every header's directory, the testbench's included: Verilator compiles both
        include_dirs = unique(
            [str(d) for d in ss.include_dirs] + [str(d) for d in self.design.header_dirs(tb=True)]
        )

        args += compile_args
        args += [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in defines.items()]
        args += [f"-I{dir}" for dir in include_dirs]
        args += [f"-G{name}={value}" for name, value in parameters.items()]

        for vlib in ss.verilog_libs:
            args += ["-v", str(vlib)]

        sources: List[Any] = [
            src
            for src in self.sources_read(tb=True)
            if src.type in (SourceType.Verilog, SourceType.SystemVerilog, SourceType.Cpp)
        ]

        if self.cocotb:
            lib_dir = self.cocotb.lib_dir
            args += [
                "-LDFLAGS",
                f"-Wl,-rpath,{lib_dir} -L{lib_dir} -lcocotbvpi_verilator",
            ]
            env = self.cocotb.env(self.design)
            # cocotb's own main runs the model, and Xeda has no copy of it
            coco_share_dir = self.cocotb.share_dir
            cocotb_cpp_path = (
                Path(coco_share_dir) / "lib" / "verilator" / "verilator.cpp"
                if coco_share_dir
                else None
            )
            if cocotb_cpp_path is None:
                raise FlowFatalError("cocotb-config does not report cocotb's share directory")
            if verilator.dockerized:
                # the file is in the image, where Verilator reads it
                sources.append(cocotb_cpp_path)
            elif cocotb_cpp_path.exists():
                log.debug("Using cocotb verilator.cpp from %s", cocotb_cpp_path)
                self.run_file(sim_dir).mkdir(parents=True, exist_ok=True)
                target = self.run_directory.writable(sim_dir / "cocotb_verilator.cpp")
                sources.append(replacing_copy(cocotb_cpp_path, target))
            else:
                raise FlowFatalError(
                    f"cocotb's main for Verilator, {cocotb_cpp_path}, does not exist: this cocotb "
                    "installation does not support Verilator"
                )

        verilator.run(*args, *sources, *hooks, env=build_env)
        if ss.random_init:
            # a seed of 0 would ask the model to pick one from the system's random generator
            seed = randint(1, 1 << 31) if ss.random_seed == "random" else ss.random_seed
            model_args += [f"+verilator+seed+{seed}", "+verilator+rand+reset+2"]
        if ss.fail_severity in ("failure", "fatal"):
            model_args.append("+verilator+error+limit+2147483647")
        if hooked:
            record = self.run_path / sim_dir / END_RECORD
            # a previous run's record (or a link a tool left at its name) is never this run's
            self.run_directory.remove(record, record.with_name(record.name + ".tmp"))
            # the model takes no argument of Xeda's: the hooks read the record's path from here
            env = {**(env or {}), "XEDA_END_RECORD": str(record)}
        self._free_model_outputs(model_args)
        model = verilator.derive(verilated_bin)
        # the model's output, copied to `sim.log` (line-buffered by the hooks, so none is lost
        # when the model is stopped); cocotb's goes straight to the terminal, keeping its colors,
        # since its results decide the run and nothing reads its log
        model_log = None if self.cocotb else self.run_directory.writable(sim_dir / "sim.log")
        self._model_log = model_log
        try:
            model.run(*model_args, env=env, timeout=ss.timeout, tee=model_log)
        except NonZeroExitCode as e:
            self._driver_exit_code = e.exit_code
            raise
        self._driver_exit_code = 0

    def _free_model_outputs(self, model_args: List[str]) -> None:
        """Remove the files that Verilator's own main writes at a name of its own.

        This runs before the model. The files are `coverage.dat` (with `--coverage`) and the file
        that `+verilator+coverage+file+` names. Each is removed as itself, a symbolic link or a
        hard link at such a name included. Otherwise the model would write through the link, to a
        file that may lie outside the run directory, and the file of an earlier run would stand
        for this run's."""
        names = [COVERAGE_FILE]
        names += [
            arg.split(COVERAGE_FILE_ARGUMENT, 1)[1]
            for arg in model_args
            if arg.startswith(COVERAGE_FILE_ARGUMENT)
        ]
        for name in names:
            if ".." in Path(name).parts:
                # a `..` after a link leads elsewhere than its text does: the model and this check
                # would name different files
                raise FlowSettingsException(
                    f"the coverage file {name} has a `..` in it: name it without, so that the "
                    "file Xeda frees is the file the model writes"
                )
            within = self.run_directory.named_within(self.run_path / name)
            if within is None:
                continue  # a location the user chose outside the run directory is the user's own
            # a name in the run directory, however it is spelled: refused when a link on the way
            # leads out
            located = self.run_path / within
            if located.is_dir() and not located.is_symlink():
                # `remove` deletes a directory tree: never one that was named as a file
                raise FlowSettingsException(
                    f"the coverage file {located} is a directory: name a file in it, or another "
                    "file"
                )
            self.run_directory.remove(located)
            if os.path.islink(located) or (
                os.path.lexists(located) and os.lstat(located).st_nlink > 1
            ):
                # a directory no launcher chose (a flow built directly): Xeda deletes nothing there
                raise FlowFatalError(
                    f"{located} is a link, and Xeda deletes nothing in {self.run_path}, which no "
                    "launcher chose: the model would write through it. Remove the link"
                )

    def rm_dep_files(self):
        """Remove the make dependency files in `sim_dir`, to trigger Verilator."""
        assert isinstance(self.settings, self.Settings)
        sim_dir = self.settings.sim_dir
        if not sim_dir:
            return
        sim_path = self.run_file(sim_dir)
        if os.path.exists(sim_path):
            log.info("Removing dependency files to trigger verilator")
            self.run_directory.remove(*glob(f"{glob_escape(str(sim_path))}{os.sep}*.d"))
