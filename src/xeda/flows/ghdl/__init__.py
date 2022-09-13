import logging
import os
import platform
from abc import ABCMeta
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from ...dataclass import Field, validator
from ...design import Design, DesignSource, Tuple012, VhdlSettings
from ...gtkwave import gen_gtkw
from ...tool import Docker, Tool
from ...utils import SDF, common_root, setting_flag
from ..flow import Flow, FlowSettingsError, SimFlow, SynthFlow

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

    docker = Docker(image="hdlc/sim:osvb")
    executable = "ghdl"

    @cached_property
    def info(self) -> Dict[str, str]:
        out = self.run_get_stdout(
            "--version",
        )
        lines = [line.strip() for line in out.splitlines()]
        if len(lines) < 3:
            return {}
        self._version = tuple(lines[0].split("."))

        return {
            "version": ".".join(self.version),
            "compiler": lines[1],
            "backend": lines[2],
        }


class Ghdl(Flow, metaclass=ABCMeta):
    """VHDL simulation using GHDL"""

    ghdl = GhdlTool()  # pyright: reportGeneralTypeIssues=none

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
            "--warn-unused",
            "--warn-static",
            "--warn-parenthesis",
        ]
        werror: bool = Field(
            False, description="warnings are always considered as errors"
        )
        elab_werror: bool = Field(
            False, description="During elaboration, warnings are considered as errors"
        )
        relaxed: bool = Field(
            True,
            description="Slightly relax some rules to be compatible with various other simulators or synthesizers.",
        )
        clean: bool = Field(False, description="Run 'clean' before elaboration")
        diagnostics: bool = Field(
            True, description="Enable both color and source line carret diagnostics."
        )
        work: Optional[str] = Field(
            None, description="Set the name of the WORK library"
        )
        expect_failure: bool = False

        def common_flags(self, vhdl: VhdlSettings) -> List[str]:
            cf: List[str] = []
            if vhdl.standard:
                cf.append(f"--std={vhdl.standard}")
            if vhdl.synopsys:
                cf.append("-fsynopsys")
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
                analysis_flags.append("--ieee=synopsys")
            if stage in ("import", "analyze"):
                return analysis_flags + warn_flags
            if stage in ("make", "elaborate"):
                return elab_flags + warn_flags
            if stage in ("find-top"):
                return find_top_flags
            raise ValueError("unknown stage!")

    def elaborate(
        self, sources: List[DesignSource], top: Union[str, Tuple012], vhdl: VhdlSettings
    ) -> Tuple012:
        """returns top(s) as a list"""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if isinstance(top, str):
            top = (top,)
        steps = ["import", "make"]
        if ss.clean:
            steps.insert(0, "remove")
        if not top:
            # run find-top after import
            log.warning("added find-top to steps")
            steps.insert(steps.index("import") + 1, "find-top")
        for step in steps:
            args = ss.get_flags(vhdl, step)
            if isinstance(ss, SimFlow.Settings):
                args += ss.optimization_flags
            if step in ["import", "analyze"]:
                args += [str(s) for s in sources]
            elif step in ["make", "elaborate"]:
                if self.ghdl.info.get("backend", "").lower().startswith("llvm"):
                    if platform.system() == "Darwin" and platform.machine() == "arm64":
                        args += ["-Wl,-Wl,-no_compact_unwind"]
                args += list(top)
            if step == "find-top":
                out = self.ghdl.run_get_stdout(step, *args)
                top_list = out.strip().split()
                # clunky way of converting to tuple, just to be safe and also keep mypy happy
                top = (
                    ()
                    if len(top_list) == 0
                    else (top_list[0],)
                    if len(top_list) == 1
                    else (top_list[0], top_list[1])
                )
                if top:
                    log.info("find-top: top-module was set to %s", top)
                else:
                    log.warning("find-top: unable to determine the top-module")
            else:
                self.ghdl.run(step, *args)
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
        no_assert_cover: bool = Field(
            False, description="Cover PSL assertion activation"
        )
        assert_assumes: bool = Field(
            False, description="Treat all PSL asserts like PSL assumes"
        )
        assume_asserts: bool = Field(
            False, description="Treat all PSL assumes like PSL asserts"
        )
        out: Optional[
            Literal["vhdl", "raw-vhdl", "verilog", "dot", "none", "raw", "dump"]
        ] = Field(None, description="Type of output to generate")
        out_file: Optional[str] = None

    def run(self) -> None:
        design = self.design
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        top = self.elaborate(design.rtl.sources, design.rtl.top, design.language.vhdl)
        args = self.synth_args(ss, design, one_shot_elab=False, top=top)
        self.ghdl.run_stdout_to_file("synth", *args, stdout=ss.out_file)

    @staticmethod
    def synth_args(
        ss: Settings, design: Design, one_shot_elab: bool = True, top: Tuple012 = ()
    ) -> List[str]:
        flags = ss.get_flags(design.language.vhdl, "elaborate")
        flags += setting_flag(ss.vendor_library, name="vendor_library")
        flags += setting_flag(ss.out, name="out")
        flags += setting_flag(ss.no_formal, name="no_formal")
        flags += setting_flag(ss.no_assert_cover, name="no_assert_cover")
        flags += setting_flag(ss.assert_assumes, name="assert_assumes")
        flags += setting_flag(ss.assume_asserts, name="assume_asserts")

        flags.extend(ss.generics_flags(design.rtl.generics))
        if one_shot_elab:
            flags += [str(v) for v in design.rtl.sources if v.type == "vhdl"]
            flags.append("-e")
        if (
            design.rtl.sources[-1].type == "vhdl"
        ):  # add top if last source (top source) is VHDL
            if not top:
                top = (design.rtl.top,)
            flags.extend(list(top))
        return flags


# class GhdlLint(Ghdl, Flow):
#     """Lint VHDL sources using GHDL"""

#     class Settings(Ghdl.Settings):
#         pass


class GhdlSim(Ghdl, SimFlow):
    """Simulate a VHDL design using GHDL"""

    cocotb_sim_name = "ghdl"

    class Settings(Ghdl.Settings, SimFlow.Settings):
        run_flags: List[str] = []
        optimization_flags: List[str] = Field(
            ["-O3"], description="Simulation optimization flags"
        )
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
        fst: Optional[str] = Field(
            None, description="Write the waveforms into an _fst_ file."
        )
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
                ext = (
                    ".ghw"
                    if field.name == "wave"
                    else ".fst"
                    if field.name == "fst"
                    else ""
                )
                if isinstance(value, bool):
                    value = "dump" + ext if value else None
                else:
                    if not isinstance(value, str):
                        value = str(value)
                    if not value.endswith(ext):
                        value += ext
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

        if ss.vcd:
            if ss.vcd.endswith((".gz", ".vcdgz")):
                run_flags.append(f"--vcdgz={ss.vcd}")
            else:
                run_flags.append(f"--vcd={ss.vcd}")
            log.warning("Dumping VCD to %s", fp(ss.vcd))
        if ss.fst:
            run_flags += setting_flag(ss.fst, name="fst")
            log.warning("Dumping fst to %s", fp(ss.fst))
        if ss.wave:
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
            else [ss.vpi]
            if not isinstance(ss.vpi, (list, tuple))
            else list(ss.vpi)
        )
        # TODO factor out cocotb handling
        if design.tb.cocotb and self.cocotb:
            vpi.append(self.cocotb.vpi_path())
            # tb_generics = list(design.tb.generics)  # TODO pass to cocotb?
            design.tb.generics = design.rtl.generics
            if not design.tb.top:
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

        design.tb.top = self.elaborate(
            design.sim_sources, design.tb.top, design.language.vhdl
        )
        assert self.cocotb
        self.ghdl.run(
            "run",
            *cf,
            *design.sim_tops,
            *run_flags,
            env=self.cocotb.env(design),
        )

    def parse_reports(self) -> bool:
        success = True
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        dump_file = ss.wave or ss.vcd or ss.fst
        if dump_file:
            log.debug("Generating GtkWave save-file form dump_file=%s", dump_file)
            opt_file = ss.read_wave_opt or ss.write_wave_opt
            extra_top = "top" if ss.wave else None
            signals, root_group = _get_wave_opt_signals(opt_file, extra_top)
            if dump_file == ss.fst:  # fst removes the common hierarchy
                signals = [s[len(root_group) :] for s in signals]
                root_group = []
            gen_gtkw(dump_file, signals, root_group)

        # TODO move
        if self.cocotb and self.design.tb and self.design.tb.cocotb:
            success &= self.cocotb.add_results(self.results)
        return success
