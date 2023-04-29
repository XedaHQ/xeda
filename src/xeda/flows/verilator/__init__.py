import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import Field

from ...design import SourceType
from ...flow import SimFlow
from ...tool import Tool

log = logging.getLogger(__name__)


class Verilator(SimFlow):
    cocotb_sim_name = "verilator"

    class Settings(SimFlow.Settings):
        sim_dir: str = "sim_build"
        compile_args: List[str] = []
        cflags: List[str] = []
        warn_flags: List[str] = [
            "-Wall",
        ]
        warnings_fatal: bool = False
        include_dirs: List[str] = []
        optimize: bool = True
        timing: bool = False
        plus_args: List[str] = []
        verilog_libs: List[str] = []
        build: bool = True
        vpi: bool = False
        no_deps: bool = True
        generate_systemc: bool = False
        generate_executable: bool = True
        compiler: Optional[str] = None
        random_init: bool = True
        x_initial: str = "unique"
        x_assign: str = "unique"
        fst: Union[None, str, Path] = None
        threads: int = Field(
            0,
            description="0: not thread-safe, 1: thread-safe single thread, 2+: multithreaded",
        )
        trace_underscore: bool = False
        trace_threads: Optional[int] = None
        trace_max_width: Optional[int] = 512
        trace_max_array: Optional[int] = None

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        verilator = Tool(
            "verilator",
            docker="xeda-verilator",
        )

        top = self.design.sim_tops[0] if self.design.sim_tops else None
        verilated_bin = os.path.join(ss.sim_dir, top or "top")

        compile_args = ss.compile_args
        parameters = self.design.tb.parameters
        defines: Dict[str, Any] = dict()

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
                "--build-jobs",
                0,  # auto
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

        args.append("--public-flat-rw")

        if top:
            args += ["--top-module", top]
        else:
            args += ["--prefix", "top"]

        args += [
            "-o",
            top or "top",
        ]

        if self.cocotb or ss.vpi:
            args.append("--vpi")

        if verilator.version_gte(5):
            if ss.timing:
                args.append("--timing")
            else:
                args.append("--no-timing")
        if not ss.timing:
            args += [
                "-Wno-STMTDLY",
                "-Wno-INITIALDLY",
            ]

        if ss.threads:
            args += ["--threads", ss.threads]
        if ss.trace_threads:
            args += ["--trace-threads", ss.trace_threads]

        if ss.optimize:
            args += ["-O3"]

        args += [
            "--x-initial",
            ss.x_initial,
            "--x-assign",
            ss.x_assign,
        ]

        cflags = list(ss.cflags)  # copy

        if self.cocotb:
            if verilator.docker is not None:
                self.cocotb.docker = verilator.docker.copy(
                    update=dict(command=[self.cocotb.executable]),
                )

        if ss.vcd:
            args += [
                "--trace",
            ]
        elif ss.fst:
            args += [
                "--trace-fst",
                "--trace-structs",
                # "--trace-underscore",
            ]

        if ss.vcd or ss.fst:
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

        args += compile_args
        args += [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in defines.items()]
        args += [f"-I{dir}" for dir in ss.include_dirs]
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
            cocotb_cpp = self.copy_from_template("cocotb_verilator.cpp", top=top or "top")
            sources.append(cocotb_cpp)

        verilator.run(*args, *sources)
        plus_args = list(ss.plus_args)
        if ss.random_init:
            random_seed = (
                1 if ss.debug else 0
            )  # 0 = choose value from system random number generator
            plus_args += [f"+verilator+seed+{random_seed}", "+verilator+rand+reset+2"]
        model = verilator.derive(verilated_bin)
        model.run(*ss.plus_args, env=env)
