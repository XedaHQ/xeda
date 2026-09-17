import logging
import os
import shutil
from glob import glob
from pathlib import Path
from random import randint
from typing import Any, Dict, List, Optional, Union

from ...dataclass import Field
from ...design import SourceType
from ...flow import SimFlow
from ...tool import Tool
from ...utils import unique

log = logging.getLogger(__name__)


class Verilator(SimFlow):
    """Simulate a Verilog or SystemVerilog design with Verilator.

    Verilator compiles the design to C++ (or SystemC) and builds a native executable, which
    makes it the fastest open-source simulator for large designs. Supports cocotb testbenches,
    plain C++/SystemC harnesses, and VCD/FST waveform tracing.
    """

    cocotb_sim_name = "verilator"

    class Settings(SimFlow.Settings):
        sim_dir: str = Field(
            "sim_build",
            description="Directory, relative to the run directory, where Verilator writes the "
            "generated C++ and the built model.",
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
        include_dirs: List[str] = Field(
            [], description="Directories searched for `include` files and missing modules (-I)."
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
            "event controls, needed by testbenches that use `#delay` or `wait`.",
        )
        model_args: List[str] = Field(
            default=[], description="Arguments to pass to the model executable"
        )
        verilog_libs: List[str] = Field(
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
            True,
            description="Randomize the initial value of uninitialized signals at each run, which "
            "surfaces reset bugs a zero-initialized model would hide. See `x_initial`.",
        )
        x_initial: str = Field(
            "unique",
            description='How uninitialized values are set at time 0: "unique" (random per '
            'run), "0", or "fast".',
        )
        x_assign: str = Field(
            "unique",
            description='How explicit assignments of X are resolved: "unique" (random per '
            'run), "0", "1", or "fast".',
        )
        fst: Union[None, str, Path] = Field(
            None,
            description="Write an FST waveform to this file. FST is far more compact than VCD "
            "for long simulations. See also `vcd`/`waveform`.",
        )
        saif: Union[None, str, Path] = Field(
            None,
            description="Write switching activity to this SAIF file, for power estimation.",
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
        clean_before_run: bool = Field(
            True,
            description="Remove `sim_dir` before building, so a stale generated model is never "
            "reused.",
        )

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        self.rm_dep_files()

        verilator = Tool(
            "verilator",
            docker="xeda-verilator",
        )

        top = None  # self.design.sim_tops[0] if self.design.sim_tops else None
        verilated_bin = os.path.join(ss.sim_dir, top or "top")

        compile_args = ss.compile_args
        parameters = self.design.tb.parameters
        defines: Dict[str, Any] = self.design.rtl.defines

        # use any self.design.tb.defines to override rtl defines
        defines.update(self.design.tb.defines)

        args: List[Any] = []

        if ss.generate_systemc:
            args += ["-sc"]
        else:
            args += ["-cc"]

        if ss.generate_executable:
            args += ["--exe"]

        if not self.cocotb and not self.design.sim_sources_of_type(SourceType.Cpp):
            args += ["--main"]

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

        if top:
            args += ["--top-module", top]
        else:
            args += ["--prefix", "Vtop"]

        args += [
            "-o",
            top or "top",
        ]

        if self.cocotb or ss.vpi:
            args.append("--vpi")
            args.append("--public-flat-rw")

        if verilator.version_gte(5):
            if ss.timing:
                args.append("--timing")
            else:
                args.append("--no-timing")

        # supres unhelpful warnings
        args += [
            "-Wno-DECLFILENAME",
        ]
        if not ss.timing:
            args += [
                "-Wno-STMTDLY",
                "-Wno-INITIALDLY",
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
                self.cocotb.docker = verilator.docker.model_copy(
                    update=dict(command=[self.cocotb.executable]),
                )

        model_args = ss.model_args
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

        env = None

        include_dirs = unique(
            ss.include_dirs
            + [
                str(src.path.parent)
                for src in self.design.rtl.sources
                if src.type in (SourceType.VerilogHeader, SourceType.SVHeader)
            ]
        )

        args += compile_args
        args += [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in defines.items()]
        args += [f"-I{dir}" for dir in include_dirs]
        args += [f"-G{name}={value}" for name, value in parameters.items()]

        # read verilog libs
        for vlib in ss.verilog_libs:
            args += ["-v", vlib]

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
                    cocotb_cpp = Path(
                        shutil.copy(cocotb_cpp_path, sim_dir / "cocotb_verilator.cpp")
                    )
                    assert cocotb_cpp.exists()
            if cocotb_cpp is None:
                cocotb_cpp = self.copy_from_template("cocotb_verilator.cpp", top=top or "top")
            sources.append(cocotb_cpp)

        verilator.run(*args, *sources)
        if ss.random_init:
            random_seed = (
                1 if ss.debug else randint(1, 1 << 31)
            )  # 0 = choose value from system random number generator
            model_args += [f"+verilator+seed+{random_seed}", "+verilator+rand+reset+2"]
        model = verilator.derive(verilated_bin)
        model.run(*ss.model_args, env=env)

    def rm_dep_files(self):
        assert isinstance(self.settings, self.Settings)
        log.info("Removing dependency files to trigger verilator")
        if self.settings.sim_dir and os.path.exists(self.settings.sim_dir):
            for p in glob(f"{self.settings.sim_dir}{os.sep}*.d"):
                if os.path.exists(p):
                    log.debug("Deleting %s", p)
                    os.unlink(p)

    def clean(self):
        assert isinstance(self.settings, self.Settings)
        if (
            self.settings.clean_before_run
            and self.settings.sim_dir
            and os.path.exists(self.settings.sim_dir)
        ):
            shutil.rmtree(self.settings.sim_dir)
