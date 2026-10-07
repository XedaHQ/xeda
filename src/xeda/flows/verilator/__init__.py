import json
import logging
import os
import re
import string
from glob import escape as glob_escape
from glob import glob
from pathlib import Path
from random import randint
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Optional, Union

from ...cocotb import cocotb_toplevel
from ...dataclass import WORKING, Field, deliverable
from ...design import Design, SourceType
from ...flow import Flow, FlowSettingsException, SimFlow, describe_results
from ...flow.sim import SimEvent, SimEvidence
from ...tool import NonZeroExitCode, Tool
from ...units import convert_unit
from ...utils import replacing_copy, unique

log = logging.getLogger(__name__)

#: The end record xeda's hooks write in `sim_dir` (`templates/xeda_hooks.cpp`).
END_RECORD = "xeda_end.json"
#: Verilator's runtime functions xeda's hooks define in place of its own (verilated.cpp).
HOOK_MACROS = (
    "VL_USER_FINISH",
    "VL_USER_STOP",
    "VL_USER_FATAL",
    "VL_USER_WARN",
    "VL_USER_STOP_MAYBE",
)
#: An end record's event kinds, as `SimEvent` kinds; `stop_maybe` is classified by its `maybe`.
_RECORD_EVENT_KINDS = {
    "stop": "stop",
    "fatal": "fatal",
    "finish": "finish",
    "warning": "warning",
}


def parse_end_record(path: Path) -> SimEvidence:
    """The evidence in the end record xeda's Verilator hooks write (`templates/xeda_hooks.cpp`).
    A `stop_maybe` event is an error (`$error`, a failed assertion, `$stop`) when `maybe` is true
    and a fatal error (`$fatal`) when it is false. An unreadable or malformed record is a
    `ValueError`."""
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


#: The oldest Verilator with what xeda's driver and hooks use.
MIN_VERILATOR_VERSION = (5, 24)


class Verilator(SimFlow):
    """Simulate a Verilog or SystemVerilog design with Verilator.

    Verilator compiles the design to C++ (or SystemC) and builds a native executable, which
    makes it the fastest open-source simulator for large designs. Supports cocotb testbenches,
    plain C++/SystemC harnesses, and VCD/FST waveform tracing.

    The simulated top is the testbench's `tb.top`, or else the design's `rtl.top` (a design with
    HDL testbench sources needs `tb.top`, unless it has a C++ driver of its own); with cocotb, the
    module cocotb drives: `tb.cocotb.toplevel`, or else `rtl.top`. Its parameters (`-G`) are
    `rtl.parameters` updated by `tb.parameters` when the simulated top is the RTL top, and
    `tb.parameters` alone otherwise. Without cocotb, a run passes only on evidence of how the
    simulation ended, which xeda's hooks record in Verilator's runtime (`xeda_end.json` in
    `sim_dir`): xeda's own driver runs the model unless the design brings its own C++ driver,
    whose end the hooks record just the same, and the run passes when it ends by `$finish` or
    at the requested `stop_time` -- or, with the design's own driver, when that driver exits
    with status 0 -- and nothing reported reaches `fail_severity`. An event queue that runs
    empty without a `$finish` fails. With cocotb, cocotb's results decide the run.
    """

    cocotb_sim_name = "verilator"

    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )

    #: the exit status of this run's model, once it has returned (0) or failed
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
            [], description="Extra flags passed through to the C++ compiler (verilator -CFLAGS)."
        )
        warn_flags: List[str] = Field(
            ["-Wall"],
            description='Verilator lint/warning flags, e.g. ["-Wall", "-Wno-WIDTH"]. '
            "Verilator's linting is stricter than most simulators'.",
        )
        warnings_fatal: bool = Field(
            False,
            description="Treat Verilator warnings as errors (`-Werror`), failing the flow.",
        )
        include_dirs: List[Path] = Field(
            [],
            description="Directories searched for `include` files and missing modules (-I). "
            "Every file under them is an input of the run.",
        )
        optimize: Union[bool, str, int] = Field(
            True,
            description="Optimization level for the generated model: true for Verilator's "
            'default -O3, false to disable, or a level/flag string such as 3 or "-O2". '
            "Disabling it speeds up compilation and slows down simulation.",
        )
        timing: bool = Field(
            False,
            description="Enable Verilator's `--timing` support for delays and non-blocking "
            "event controls, needed by testbenches that use `#delay` or `wait`. When it is off, "
            "Verilator ignores a delay and warns about it (`STMTDLY`, `INITIALDLY`), so a "
            "testbench's `#100; $finish` ends at time 0.",
        )
        model_args: List[str] = Field(
            default=[], description="Arguments to pass to the model executable"
        )
        verilog_libs: List[Path] = Field(
            [],
            description="Verilog library files (`-v`): their modules are elaborated only when "
            "instantiated.",
        )
        build: bool = Field(
            True,
            description="Compile and link the generated C++ into an executable model. Disable to "
            "only generate sources.",
        )
        vpi: bool = Field(
            False,
            description="Enable VPI support in the model, required by VPI-based testbenches. Set "
            "automatically for cocotb.",
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
            description="Generate a main() and link a standalone executable (`--exe`) rather than "
            "a library to embed.",
        )
        compiler: Optional[str] = Field(
            None,
            description='C++ compiler used to build the model, e.g. "clang++". Defaults to '
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
        fst: Union[None, str, Path] = Field(
            None,
            description="Write an FST waveform to this file. FST is far more compact than VCD "
            "for long simulations. See also `vcd`/`waveform`.",
            json_schema_extra=deliverable("outputs/{design}.fst"),
        )
        saif: Union[None, str, Path] = Field(
            None,
            description="Write switching activity to this SAIF file, for power estimation.",
            json_schema_extra=deliverable("outputs/{design}.saif"),
        )
        threads: int = Field(
            0,
            description="0: not thread-safe, 1: thread-safe single thread, 2+: multithreaded",
        )
        trace_underscore: bool = Field(
            True, description="Include signals whose names begin with an underscore in traces."
        )
        trace_structs: bool = Field(
            True,
            description="Trace structs and packed arrays with their field names rather than as "
            "flat vectors.",
        )
        trace_threads: Optional[int] = Field(
            None,
            description="Number of threads used to write the waveform. Null leaves Verilator's "
            "default.",
        )
        trace_max_width: Optional[int] = Field(
            2048, description="Do not trace signals wider than this many bits."
        )
        trace_max_array: Optional[int] = Field(
            2048, description="Do not trace arrays with more than this many elements."
        )

        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "clean_before_run": "the --clean option",
        }

    def init(self) -> None:
        super().init()
        assert isinstance(self.settings, self.Settings)
        if self.settings.stop_time is not None:
            if self.cocotb:
                raise FlowSettingsException(
                    "stop_time is enforced only by xeda's own Verilator driver; with cocotb, "
                    "stop the simulation in the test"
                )
            if self.own_driver():
                raise FlowSettingsException(
                    "stop_time is enforced only by xeda's own Verilator driver; with the "
                    "design's own C++ driver, stop it there"
                )

    def always_runs(self) -> Optional[str]:
        assert isinstance(self.settings, self.Settings)
        if self.settings.random_init and self.settings.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()

    def has_evidence_adapter(self) -> bool:
        """xeda's hooks record how the simulation ended, except under cocotb, whose own driver
        runs the model and whose results decide the run."""
        return not self.cocotb

    def simulation_top(self) -> str:
        """The simulated top module: with cocotb, the one cocotb drives (`cocotb_toplevel`:
        `tb.cocotb.toplevel`, or else the RTL top); otherwise the testbench's first `tb.top`, or
        else the RTL top."""
        if self.cocotb:
            top = cocotb_toplevel(self.design)
        else:
            top = next(iter(self.design.tb.top), None) or self.design.rtl.top
        if not top:
            raise FlowSettingsException("no simulation top: set tb.top or rtl.top")
        return top

    @classmethod
    def check_run_directory(cls, settings: Flow.Settings, run_path: Path) -> None:
        """Refuse a build directory whose path has whitespace: Verilator's makefile stops with
        "GNU Make cannot build in directories containing spaces" before it compiles anything,
        because make splits a path at whitespace. The model is built in `sim_dir`, a name inside
        the run directory. Make builds in the physical directory, so the run directory is judged
        by what it leads to, but `sim_dir` by its name: the launcher removes a link left at it
        before the run, and never follows it."""
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
    def runs_without_testbench_top(cls, design: Design) -> bool:
        """A design with a C++ driver of its own runs the model, whatever HDL its testbench
        sources hold (a bound checker, a model): the driver does not need a testbench top."""
        return bool(design.sim_sources_of_type(SourceType.Cpp))

    def own_driver(self) -> bool:
        """Whether the design brings its own C++ driver (`Cpp` sources), which runs the model in
        place of xeda's."""
        return bool(self.design.sim_sources_of_type(SourceType.Cpp))

    def simulation_evidence(self) -> SimEvidence | None:
        """The end record this run's model wrote (`report_file`: this run's own only). A design's
        own driver that ended the simulation by exiting has the status it exited with."""
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
            log_file = self.report_file(self._model_log)
            if log_file is not None:
                for line in log_file.read_text(errors="replace").splitlines():
                    if re.match(r"\[\d+\] %Warning:", line):
                        evidence.events.append(SimEvent(kind="warning", message=line))
        if evidence.ended_by == "exit" and evidence.exit_code is None and self.own_driver():
            evidence.exit_code = self._driver_exit_code
        return evidence

    def run(self):
        """Compile the design and run its Verilator simulation."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        self.rm_dep_files()

        verilator = Tool(
            "verilator",
            docker="xeda-verilator",
            minimum_version=MIN_VERILATOR_VERSION,
        )

        top = self.simulation_top()
        sim_dir = Path(ss.sim_dir)
        verilated_bin = os.path.join(ss.sim_dir, "top")
        # without cocotb, xeda's hooks record how the simulation ends, in the design's own
        # driver or else in xeda's
        hooked = not self.cocotb
        xeda_driver = hooked and not self.own_driver()
        if xeda_driver and ss.generate_systemc:
            raise FlowSettingsException(
                "generate_systemc needs the design's own sc_main among its C++ sources: xeda's "
                "driver runs a C++ model"
            )

        compile_args = ss.compile_args
        # `rtl.parameters` are the RTL top's: they apply only when it is the simulated top
        if top == self.design.rtl.top:
            parameters = {**self.design.rtl.parameters, **self.design.tb.parameters}
        else:
            parameters = dict(self.design.tb.parameters)
        # tb defines override rtl defines; merged into a new mapping, as the design is shared by
        # every flow of the run and already hashed
        defines: dict[str, Any] = {**self.design.rtl.defines, **self.design.tb.defines}

        args: List[Any] = []

        if ss.generate_systemc:
            args += ["-sc"]
        else:
            args += ["-cc"]

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

        if verilator.version_gte(5):
            if ss.timing:
                args.append("--timing")
            else:
                args.append("--no-timing")

        # suppress unhelpful warnings
        args += [
            "-Wno-DECLFILENAME",
        ]

        if ss.threads:
            args += ["--threads", ss.threads]

        if ss.optimize:
            if ss.optimize is True:
                args += ["-O3"]
            elif isinstance(ss.optimize, (str, int)):
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
        if ss.vcd:
            args += [
                "--trace-vcd",
            ]
        elif ss.fst:
            args += [
                "--trace-fst",
            ]
        elif ss.saif:
            args += [
                "--trace-saif",
            ]
        trace = ss.vcd or ss.fst or ss.saif
        if trace:
            if self.cocotb:
                model_args.append("--trace")
            if self.cocotb and isinstance(trace, (str, Path)):
                trace = str(self.process_path(trace, subs_vars=True))
                model_args += ["--trace-file", trace]
                log.info("Will generate trace file %s", trace)
            else:
                log.info("Trace generation is enabled. Working directory is %s", ss.sim_dir)
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
        if hooked:
            sim_dir.mkdir(parents=True, exist_ok=True)
            header = self.copy_from_template(
                "xeda_hooks.h", script_filename=sim_dir / "xeda_hooks.h"
            )
            hooks.append(
                self.copy_from_template(
                    "xeda_hooks.cpp", script_filename=sim_dir / "xeda_hooks.cpp"
                )
            )
            if xeda_driver:
                hooks.append(
                    self.copy_from_template(
                        "verilator_main.cpp",
                        script_filename=sim_dir / "verilator_main.cpp",
                        prefix="Vtop",
                        threads=ss.threads or 1,
                    )
                )
            for macro in HOOK_MACROS:
                args += ["-CFLAGS", f"-D{macro}"]
            # by name: make splits a flag at a space, and the model is compiled in `sim_dir`
            args += ["-CFLAGS", f"-include {header.name}"]

        env = None

        # every header's directory, the testbench's included: Verilator compiles both
        include_dirs = unique(
            [str(d) for d in ss.include_dirs] + [str(d) for d in self.design.header_dirs(tb=True)]
        )

        args += compile_args
        args += [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in defines.items()]
        args += [f"-I{dir}" for dir in include_dirs]
        args += [f"-G{name}={value}" for name, value in parameters.items()]

        # read verilog libs
        for vlib in ss.verilog_libs:
            args += ["-v", str(vlib)]

        sources: List[Any] = self.design.sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, SourceType.Cpp, rtl=True, tb=True
        )

        if self.cocotb:
            lib_dir = self.cocotb.lib_dir
            args += [
                "-LDFLAGS",
                f"-Wl,-rpath,{lib_dir} -L{lib_dir} -lcocotbvpi_verilator",
            ]
            env = self.cocotb.env(self.design)
            cocotb_cpp = None
            coco_share_dir = self.cocotb.share_dir
            if coco_share_dir:
                cocotb_cpp_path = Path(coco_share_dir) / "lib" / "verilator" / "verilator.cpp"
                if cocotb_cpp_path.exists():
                    log.debug("Using cocotb verilator.cpp from %s", cocotb_cpp_path)
                    sim_dir = Path(ss.sim_dir)
                    sim_dir.mkdir(parents=True, exist_ok=True)
                    target = self.run_directory.writable(sim_dir / "cocotb_verilator.cpp")
                    cocotb_cpp = replacing_copy(cocotb_cpp_path, target)
                    assert cocotb_cpp.exists()
            if cocotb_cpp is None:
                cocotb_cpp = self.copy_from_template("cocotb_verilator.cpp", top="top")
            sources.append(cocotb_cpp)

        verilator.run(*args, *sources, *hooks)
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
            if xeda_driver:
                model_args.append(f"+xeda+end_record+{record}")
                if ss.stop_time is not None:
                    # a bare number is nanoseconds (`SimFlow.Settings.stop_time`)
                    stop_ps = round(convert_unit(ss.stop_time, "ps", from_unit="ns"))
                    model_args.append(f"+xeda+stop_time_ps+{stop_ps}")
            else:  # a design's own driver takes no arguments of xeda's
                env = {**(env or {}), "XEDA_END_RECORD": str(record)}
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

    def rm_dep_files(self):
        """Remove the make dependency files in `sim_dir`, to trigger verilator (through the run
        directory, in which `sim_dir` is a name)."""
        assert isinstance(self.settings, self.Settings)
        sim_dir = self.settings.sim_dir
        if not sim_dir:
            return
        if os.path.exists(sim_dir):
            log.info("Removing dependency files to trigger verilator")
            self.run_directory.remove(*glob(f"{glob_escape(str(sim_dir))}{os.sep}*.d"))
