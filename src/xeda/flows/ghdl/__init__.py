from __future__ import annotations

import inspect
import logging
import os
import platform
from abc import ABCMeta
from functools import cached_property
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional, Union


from ...dataclass import Field, validator
from ...design import Design, DesignSource, SourceType, Tuple012, VhdlSettings
from ...flow import Flow, FlowSettingsError, SimFlow, SynthFlow, FlowException
from ...gtkwave import gen_gtkw
from ...tool import Docker, Tool
from ...utils import SDF, common_root, setting_flag

log = logging.getLogger(__name__)


def _get_wave_opt_signals(wave_opt_file, extra_top=None):
    signals = []
    with open(wave_opt_file, "r") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if line.startswith("/"):
                sig = line[1:].split("/")
                if extra_top:
                    sig.insert(0, extra_top)
                signals.append(sig)
    root_group = common_root(signals)
    return signals, root_group


class GhdlTool(Tool):
    """GHDL VHDL simulation, synthesis, and linting tool: https://ghdl.readthedocs.io"""

    docker: Optional[Docker] = Docker(image="hdlc/sim:osvb")  # type: ignore
    executable: str = "ghdl"
    version_regexps: List[Union[str, re.Pattern[str]]] = [
        re.compile(r, re.IGNORECASE)
        for r in (
            r"GHDL\s+(?P<version>\d+\.\d+\.\d+).*",
            r"GHDL\s+(?P<version>\d+\.\d+\.\d+).*",
        )
    ]

    @cached_property
    def info(self) -> Dict[str, Optional[str]]:
        out = self.version_output
        lines = [l for l in (line.strip() for line in out.splitlines()) if l] if out else []
        compiler = None
        backend = None
        for line in lines:
            m = re.match(r"\s*Compiled with\s+(.*)\s*$", line, re.IGNORECASE)
            if m:
                compiler = m.group(1).strip()
                break
        for line in lines:
            m = re.match(r"\s*(.*)\s+code generator\s*$", line, re.IGNORECASE)
            if m:
                backend = m.group(1).strip()
                break
        info = super().info
        if compiler is None and len(lines) >= 2:
            compiler = lines[1]
        if backend is None and compiler and len(lines) >= 3:
            backend = lines[2]
        if compiler:
            info["compiler"] = compiler
        if backend:
            info["backend"] = backend
        return info


class Ghdl(Flow, metaclass=ABCMeta):
    """VHDL simulation using GHDL"""

    class Settings(Flow.Settings):
        analysis_flags: List[str] = []
        elab_flags: List[str] = ["--syn-binding"]
        warn_flags: List[str] = [
            "--warn-binding",
            "--warn-default-binding",
            "--warn-reserved",
            "--warn-library",
            "--warn-vital-generic",
            "--warn-shared",
            "--warn-runtime-error",
            "--warn-body",
            "--warn-specs",
            "--warn-no-hide",
            "--warn-parenthesis",
            "--warn-port",  # Emit a warning on unconnected input port without defaults (in relaxed mode).
            "--warn-static",
            "--warn-port-bounds",  # Emit a warning on bounds mismatch between the actual and formal in a scalar port association
            "--warn-universal",  # Emit a warning on incorrect use of universal values.
        ]
        werror: bool = Field(
            False, alias="warn_error", description="warnings are always considered as errors"
        )
        elab_werror: bool = Field(
            False,
            alias="elab_warn_error",
            description="During elaboration, warnings are considered as errors",
        )
        relaxed: bool = Field(
            True,
            description="Slightly relax some rules to be compatible with various other simulators or synthesizers.",
        )
        clean: bool = Field(
            True,
            description="Run 'clean' before analysis. This will remove all generated files.",
        )
        diagnostics: bool = Field(
            True, description="Enable both color and source line carret diagnostics."
        )
        work: Optional[str] = Field(None, description="Set the name of the WORK library")
        expect_failure: bool = False
        synopsys: bool = False
        compiler_flags: List[str] = []
        assembler_flags: List[str] = []
        linker_flags: List[str] = []
        psl_in_comments: bool = Field(
            False, description="Parse PSL assertions within comments (for VHDL-2002 and earlier)"
        )

        def common_flags(self, vhdl: VhdlSettings) -> List[str]:
            cf: List[str] = []
            if vhdl.standard:
                if len(vhdl.standard) == 4 and vhdl.standard[:2] in ("20", "19"):
                    vhdl.standard = vhdl.standard[2:]
                cf.append(f"--std={vhdl.standard}")
            if self.synopsys:
                cf += ["-fsynopsys", "-fexplicit"]
            if self.work:
                cf.append(f"--work={self.work}")
            cf += [f"-P{p}" for p in self.lib_paths]
            return cf

        @staticmethod
        def generics_flags(generics: Optional[Dict[str, Any]]) -> List[str]:
            if not generics:
                return []

            def conv(v):
                if isinstance(v, bool):
                    return str(v).lower()
                return v

            return [f"-g{k}={conv(v)}" for k, v in generics.items()]

        def get_flags(self, vhdl: VhdlSettings, stage: str) -> List[str]:
            common = self.common_flags(vhdl)
            warn_flags: List[str] = self.warn_flags
            analysis_flags: List[str] = common + self.analysis_flags
            elab_flags: List[str] = common + self.elab_flags
            find_top_flags = common
            if stage == "common":
                return common
            if stage == "remove":
                return common
            if self.werror:
                warn_flags += ["--warn-error"]
            elif self.elab_werror:
                elab_flags += ["--warn-error"]
            if self.relaxed:
                analysis_flags.extend(["-frelaxed-rules", "-frelaxed", "--mb-comments"])
                elab_flags.extend(["-frelaxed"])
            if self.psl_in_comments:
                analysis_flags.append("--psl")
            if self.verbose:
                analysis_flags.append("-v")
                elab_flags.append("-v")
            if self.diagnostics:
                elab_flags.extend(
                    [
                        "-fcaret-diagnostics",
                        "-fcolor-diagnostics",
                        "-fdiagnostics-show-option",
                    ]
                )
            if self.work:
                elab_flags.append("-v")
            if self.expect_failure:
                elab_flags.append("--expect-failure")
            if vhdl.synopsys:
                analysis_flags += ["-fsynopsys", "-fexplicit"]
            if stage in ("import", "analyze"):
                return analysis_flags + warn_flags
            if stage in ("make", "elaborate"):
                return elab_flags + warn_flags
            if stage in ("find-top"):
                return find_top_flags
            raise ValueError("unknown stage!")

    @cached_property
    def ghdl(self):
        return GhdlTool()  # pyright: ignore[reportCallIssue]

    def find_top(self, *args, sources=None):
        out = self.ghdl.run_get_stdout("find-top", *args, raise_on_error=False)
        tops = out.strip().split() if out else []
        if not tops and sources:
            sources = sources or []
            find_out = self.ghdl.run_get_stdout(
                "-f", *args, *[str(s) for s in sources], raise_on_error=True
            )
            entities: List[str] = []
            if find_out:
                for line in find_out.split("\n"):
                    sp = line.split()
                    if len(sp) >= 2 and sp[0] == "entity":
                        entities.append(sp[1])
                log.info("discovered entities: %s", ", ".join(entities))
                if entities:
                    tops = (entities[-1],)
        return tops

    def elaborate(
        self,
        sources: List[DesignSource],
        top: Union[None, str, Tuple012],
        vhdl: VhdlSettings,
    ) -> Tuple012:
        """returns top unit(s) as a Tuple012"""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        sources = [src for src in sources if src.type == SourceType.Vhdl]
        if isinstance(top, str):
            top = (top,)
        steps = ["remove"] if ss.clean else []
        steps.extend(["analyze", "make"])
        if not top:
            # run find-top after import
            log.info("No top units were specified. Will try to discover by adding a find-top step")
            find_top_index = (
                steps.index("analyze")
                if "analyze" in steps
                else steps.index("import") if "import" in steps else -1
            )
            steps.insert(find_top_index + 1, "find-top")
        for step in steps:
            args = ss.get_flags(vhdl, step)
            if step in ("import", "analyze"):
                args += [str(s) for s in sources]
            elif step in ("make", "elaborate"):
                if isinstance(ss, SimFlow.Settings):
                    args += ss.optimization_flags
                if not top:
                    raise Exception("Unable to determine the top unit")
                backend = self.ghdl.info.get("backend", None)
                if backend:
                    log.info("GHDL backend: %s", backend)
                    backend_split = backend.split()
                    compiler = backend_split[0].lower() if backend_split else None
                    backend_version = backend_split[1].split(".") if len(backend_split) > 1 else []
                    # Workaround for annoying warnings on macOS/arm64 with earlier versions of the toolchains.
                    # Not required when using the latest versions of Xcode/CommandLineTools, LLVM, GNAT, and GHDL.
                    if compiler == "llvm" and backend_version and backend_version[0].isdigit():
                        llvm_major = int(backend_version[0])
                        link_flag = "-Wl,-no_compact_unwind"
                        if (
                            platform.system() == "Darwin"
                            and platform.machine() == "arm64"
                            and llvm_major
                            < 19  # TODO Probably need to check the GNAT version? Also, no idea about the version number.
                            and ss.linker_flags.count(link_flag) == 0
                        ):
                            log.info(
                                "Adding no_compact_unwind linker flags for macOS/arm64 %s" % backend
                            )
                            ss.linker_flags.append(link_flag)
                    if compiler in ("llvm", "gcc"):
                        args += (f"-Wc,{x}" for x in ss.compiler_flags)
                        args += (f"-Wa,{x}" for x in ss.assembler_flags)
                        args += (f"-Wl,{x}" for x in ss.linker_flags)

                args += list(top)
            if step == "find-top":
                tops = self.find_top(*args, sources=sources)
                if tops:
                    # clunky way of converting to tuple, just to be safe and also keep mypy happy
                    top = (
                        ()
                        if len(tops) == 0
                        else (tops[0],) if len(tops) == 1 else (tops[0], tops[1])
                    )
                    log.info(
                        "[ghdl:find-top] discovered top unit: %s. Set `top` explicitly if this was not the indtended top-level unit.",
                        ", ".join(top),
                    )
                else:
                    log.error(
                        inspect.cleandoc(
                            """[ghdl:find-top] Unable to determine the top unit.
                            Please specify `tb.top` (for simulation) and/or `rtl.top` (for synthesis) in the design description."""
                        )
                    )

            else:
                self.ghdl.run(step, *args)
        if top is None:
            top = tuple()
        return top


class GhdlSynth(Ghdl, SynthFlow):
    """
    Convert a VHDL design using 'ghdl --synth'
     (Please take a look at 'Yosys' flow (or other synthesis flows) for general VHDL, Verilog, or mixed-language synthesis targeting FPGAs or ASICs)
    """

    class Settings(Ghdl.Settings, SynthFlow.Settings):
        vendor_library: Optional[str] = Field(
            None, description="Any unit from this library is a black box"
        )
        no_formal: bool = Field(
            True,
            description="Neither synthesize assert nor PSL. Required for yosys+nextpnr flow.",
        )
        no_assert_cover: bool = Field(False, description="Cover PSL assertion activation")
        assert_assumes: bool = Field(False, description="Treat all PSL asserts like PSL assumes")
        assume_asserts: bool = Field(False, description="Treat all PSL assumes like PSL asserts")
        out: Optional[Literal["vhdl", "raw-vhdl", "verilog", "dot", "none", "raw", "dump"]] = Field(
            None, description="Type of output to generate"
        )
        out_file: Optional[str] = "converted.v"
        convert_files: bool = Field(False, description="Convert each VHDL source file to Verilog.")

    def run(self) -> None:
        design = self.design
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        top = self.elaborate(design.rtl.sources, design.rtl.top, design.language.vhdl)
        # flags = ss.get_flags(design.language.vhdl, "elaborate")
        flags = self.synth_args(ss, design, one_shot_elab=False)
        flags += setting_flag(ss.vendor_library, name="vendor_library")
        flags += setting_flag(ss.no_formal, name="no_formal")
        flags += setting_flag(ss.no_assert_cover, name="no_assert_cover")
        flags += setting_flag(ss.assert_assumes, name="assert_assumes")
        flags += setting_flag(ss.assume_asserts, name="assume_asserts")
        flags += ss.generics_flags(design.rtl.generics)
        flags += ["--out=verilog", "--warn-nowrite"]
        if ss.convert_files:
            self.artifacts.generated_verilog = []
            for src in design.sources_of_type(SourceType.Vhdl, rtl=True, tb=False):
                verilog = self.ghdl.run_get_stdout("synth", *flags, str(src.path), "-e")
                if not verilog:
                    raise FlowException("ghdl synthesis failed!")
                fixed_params = "\n".join(
                    [f"  parameter {k} = {v};" for k, v in design.rtl.generics.items()]
                )
                verilog = verilog.replace(");", f");\n{fixed_params}", 1)
                out_file = src.path.with_name(src.path.stem + "_ghdl_synth.v")
                # TODO FIXME add settings for what to do, default to fail
                if out_file.exists():
                    log.warning("File %s will be overwritten!", out_file)
                else:
                    log.info("Generating verilog: %s", out_file)
                with open(out_file, "w") as f:
                    f.write(verilog)
                self.artifacts.generated_verilog.append(out_file)
        else:
            if top:
                flags += [top[0]]
            self.ghdl.run("synth", *flags, stdout=ss.out_file)

    @staticmethod
    def synth_args(ss: Settings, design: Design, one_shot_elab: bool = True) -> List[str]:
        flags = ss.get_flags(design.language.vhdl, "elaborate")
        flags += setting_flag(ss.vendor_library, name="vendor_library")
        flags += setting_flag(ss.out, name="out")
        flags += setting_flag(ss.no_formal, name="no_formal")
        flags += setting_flag(ss.no_assert_cover, name="no_assert_cover")
        flags += setting_flag(ss.assert_assumes, name="assert_assumes")
        flags += setting_flag(ss.assume_asserts, name="assume_asserts")
        flags.extend(ss.generics_flags(design.rtl.generics))
        if one_shot_elab:
            flags += map(str, design.sources_of_type(SourceType.Vhdl, rtl=True, tb=False))
        return flags


# class GhdlLint(Ghdl, Flow):
#     """Lint VHDL sources using GHDL"""

#     class Settings(Ghdl.Settings):
#         pass


class GhdlSim(Ghdl, SimFlow):
    """Simulate a VHDL design using GHDL"""

    cocotb_sim_name = "ghdl"
    aliases = ["ghdl"]

    class Settings(Ghdl.Settings, SimFlow.Settings):
        run_flags: List[str] = []
        optimization_flags: List[str] = Field(["-O3"], description="Simulation optimization flags")
        asserts: Optional[Literal["disable", "disable-at-0"]] = None
        ieee_asserts: Optional[Literal["disable", "disable-at-0"]] = "disable-at-0"
        sdf: SDF = Field(
            SDF(),
            description="Do VITAL annotation using SDF files(s). A single string is interpreted as a MAX SDF file.",
        )
        wave: Optional[str] = Field(
            None, description="Write the waveforms into a GHDL Waveform (GHW) file."
        )
        read_wave_opt: Optional[str] = Field(
            None,
            description="Filter signals to be dumped to the wave file according to the wave option file provided.",
        )
        write_wave_opt: Optional[str] = Field(
            None,
            description="Creates a wave option file with all the signals of the design. Overwrites the file if it already exists.",
        )
        fst: Optional[str] = Field(None, description="Write the waveforms into an _fst_ file.")
        stop_delta: Optional[str] = Field(
            None,
            description="Stop the simulation after N delta cycles in the same current time.",
        )
        disp_tree: Optional[Literal["inst", "proc", "port"]] = Field(
            None,
            description="Display the design hierarchy as a tree of instantiated design entities. See GHDL documentation for more details.",
        )
        vpi: Union[None, str, List[str]] = Field(
            None,
            description="Load VPI library (or multiple libraries)",
        )
        # TODO workdir?

        @validator("wave", "fst", pre=True)
        def validate_wave(cls, value, field):  # pylint: disable=no-self-argument
            if value is not None:
                ext = ".ghw" if field.name == "wave" else ".fst" if field.name == "fst" else ""
                if isinstance(value, bool):
                    return "dump" + ext if value else None
                elif isinstance(value, str):
                    # if value.startswith("$PWD/"):
                    #     value = os.path.join(self.r, value[5:])
                    pass
            return value

    def run(self) -> None:
        design = self.design
        assert design.tb
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        cf = ss.common_flags(design.language.vhdl)
        run_flags = self.settings.run_flags
        sdf_root = ss.sdf.root if ss.sdf.root else design.tb.uut
        for delay_type, sdf_file in ss.sdf.delay_items():
            if sdf_file:
                assert sdf_root, "neither SDF root nor tb.uut are provided"
                run_flags.append(f"--sdf={delay_type}={sdf_root}={sdf_file}")

        def fp(s: Union[str, os.PathLike]) -> str:
            if not os.path.isabs(s):
                return str(self.run_path.relative_to(Path.cwd()) / s)
            return str(s)

        if ss.wave:
            if ss.wave.endswith(".vcd") or ss.wave.endswith(".vcdgz"):
                ss.vcd = ss.wave
                ss.wave = None
            elif ss.wave.endswith(".fst"):
                ss.fst = ss.wave
                ss.wave = None
            elif not ss.wave.endswith(".ghw"):
                ss.wave += ".ghw"

        if ss.vcd:
            if str(ss.vcd).endswith((".gz", ".vcdgz")):
                run_flags.append(f"--vcdgz={ss.vcd}")
            else:
                run_flags.append(f"--vcd={ss.vcd}")
            log.warning("Dumping VCD to %s", fp(ss.vcd))
        elif ss.fst:
            run_flags += setting_flag(ss.fst, name="fst")
            log.warning("Dumping fst to %s", fp(ss.fst))
        elif ss.wave:
            run_flags += setting_flag(ss.wave, name="wave")
            log.warning("Dumping GHW to %s", fp(ss.wave))

        if ss.wave or ss.vcd or ss.fst:
            if not ss.read_wave_opt and not ss.write_wave_opt:
                ss.write_wave_opt = "wave.opt"

        if ss.write_wave_opt:
            p = Path(ss.write_wave_opt)
            if p.exists():
                log.warning("Deleting existing wave option file: %s", p)
                p.unlink()
        run_flags += setting_flag(ss.write_wave_opt, name="write_wave_opt")
        if ss.read_wave_opt:
            if not Path(ss.read_wave_opt).exists():  # TODO move to validation
                raise FlowSettingsError(
                    [
                        (
                            "read_wave_opt",
                            f"File {ss.read_wave_opt} does not exist",
                            None,
                            None,
                        )
                    ],
                    self.Settings,
                )
        run_flags += setting_flag(ss.read_wave_opt, name="read_wave_opt")
        run_flags += setting_flag(ss.disp_tree, name="disp_tree")
        run_flags += setting_flag(ss.asserts, name="asserts")
        run_flags += setting_flag(ss.ieee_asserts, name="ieee_asserts")

        vpi = (
            []
            if ss.vpi is None
            else [ss.vpi] if not isinstance(ss.vpi, (list, tuple)) else list(ss.vpi)
        )
        # TODO factor out cocotb handling
        if design.tb.cocotb and self.cocotb:
            vpi_path = self.cocotb.lib_path()
            assert vpi_path, "cocotb VPI library for GHDL was not found"
            vpi.append(vpi_path)
            # tb_generics = list(design.tb.generics)  # TODO pass to cocotb?
            design.tb.generics = design.rtl.generics
            if not design.tb.top and design.rtl.top:
                design.tb.top = (design.rtl.top,)
        run_flags += setting_flag(vpi, name="vpi")

        if ss.debug:
            run_flags.extend(
                [
                    "--trace-processes",
                    "--trace-signals",
                    "--checks",
                    "--disp-sig-types",
                    "--stats",
                    "--dump-rti",
                ]
            )

        run_flags += setting_flag(ss.stop_time, name="stop_time")
        run_flags += setting_flag(ss.stop_delta, name="stop_delta")

        run_flags.extend(ss.generics_flags(design.tb.generics))

        design.tb.top = self.elaborate(design.sim_sources, design.tb.top, design.language.vhdl)
        self.ghdl.run(
            "run",
            *cf,
            *design.sim_tops,
            *run_flags,
            env=self.cocotb.env(design) if self.cocotb else {},
        )

    def parse_reports(self) -> bool:
        success = True
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        # TODO move
        if self.cocotb and self.design.tb and self.design.tb.cocotb:
            success &= self.cocotb.add_results(self.results)
        return success
