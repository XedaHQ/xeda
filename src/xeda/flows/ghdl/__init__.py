from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import Field
import logging
import platform

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal  # type: ignore


from ..flow import Flow, SimFlow, SynthFlow
from ...design import Design, Tuple012, DesignSource, VhdlSettings
from ...tool import DockerToolSettings, Tool
from ...utils import SDF

log = logging.getLogger(__name__)


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


class GhdlTool(Tool):
    """GHDL VHDL simulation, synthesis, and linting tool: https://ghdl.readthedocs.io"""

    docker = DockerToolSettings(image_name="hdlc/sim:osvb")
    executable = "ghdl"

    def get_info(self) -> Dict[str, str]:
        out = self.run_get_stdout(
            self.executable,
            ["--version"],
        )
        lines = [line.strip() for line in out.splitlines()]
        return {"compiler": lines[1], "backend": lines[2]}


class Ghdl(Flow):
    ghdl = GhdlTool()

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
            return [f"-g{k}={v}" for k, v in generics.items()]

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
            if stage == "import" or stage == "analyze":
                return analysis_flags + warn_flags
            elif stage == "make" or stage == "elaborate":
                return elab_flags + warn_flags
            elif stage == "find-top":
                return find_top_flags
            else:
                assert False, "unknown stage!"

    def elaborate(
        self, sources: List[DesignSource], top: Tuple012, vhdl: VhdlSettings
    ) -> Tuple012:
        """returns top(s) as a list"""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
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
                if self.ghdl._info.get("backend", "").lower().startswith("llvm"):
                    if platform.system() == "Darwin" and platform.machine() == "arm64":
                        args += ["-Wl,-Wl,-no_compact_unwind"]
                print(args)
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
                    log.info(f"find-top: top-module was set to {top}")
                else:
                    log.warning(f"find-top: unable to determine the top-module")
            else:
                self.ghdl.run(step, *args)
        return top


class GhdlSynth(Ghdl, SynthFlow):
    """
    Convert a VHDL design using 'ghdl --synth'
     (Please see `YosysSynth` (or other synthesis flows) for general VHDL, Verilog, or mixed-language synthesis)
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
        if ss.vendor_library:
            flags.append(f"--vendor-library={ss.vendor_library}")
        if ss.out:
            flags.append(f"--out={ss.out}")
        if ss.no_formal:
            flags.append(f"--no-formal")
        if ss.no_assert_cover:
            flags.append(f"--no-assert-cover")
        if ss.assert_assumes:
            flags.append(f"--assert-assumes")
        if ss.assume_asserts:
            flags.append(f"--assume-asserts")

        flags.extend(ss.generics_flags(design.rtl.generics))
        if one_shot_elab:
            flags += [str(v) for v in design.rtl.sources if v.type == "vhdl"]
            flags.append("-e")
        if not top:
            top = design.rtl.top
        flags.extend(list(top))
        return flags


class GhdlLint(Ghdl, Flow):
    """Lint VHDL sources using GHDL"""

    class Settings(Ghdl.Settings):
        pass


class GhdlSim(Ghdl, SimFlow):
    """Simulate a VHDL design using GHDL"""

    cocotb_sim_name = "ghdl"

    class Settings(Ghdl.Settings, SimFlow.Settings):
        run_flags: List[str] = ["--ieee-asserts=disable-at-0"]
        optimization_flags: List[str] = Field(
            ["-O3"], description="Simulation optimization flags"
        )
        sdf: SDF = Field(
            SDF(),
            description="Do VITAL annotation using SDF files(s). A single string is interpreted as a MAX SDF file.",
        )
        wave: Union[bool, None, str] = Field(
            None, description="Write the waveforms into a GHDL Waveform (GHW) file."
        )
        stop_delta: Optional[str] = Field(
            None,
            description="Stop the simulation after N delta cycles in the same current time.",
        )
        # TODO workdir?

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
        if ss.vcd:
            run_flags.append(f"--vcd={ss.vcd}")
            log.warning(
                f"Dumping VCD to {self.run_path.relative_to(Path.cwd()) / ss.vcd}"
            )
        elif ss.wave:
            wave = ss.wave
            if isinstance(wave, bool):
                wave = design.name
            if not wave.endswith(".ghw"):
                wave += ".ghw"
            run_flags.append(f"--wave={wave}")
            log.warning(
                f"Dumping GHW to {self.run_path.relative_to(Path.cwd()) / wave}"
            )
        vpi = None
        # TODO factor out cocotb handling
        if design.tb.cocotb and self.cocotb:
            vpi = self.cocotb.vpi_path()
            # tb_generics = list(design.tb.generics)  # TODO pass to cocotb?
            design.tb.generics = design.rtl.generics

        if vpi:
            run_flags.append(f"--vpi={vpi}")
        if ss.debug:
            run_flags.extend(
                [
                    "--trace-processes",
                    "--checks",
                ]
            )
        if ss.stop_time:
            run_flags.append(f"--stop-time={ss.stop_time}")
        if ss.stop_delta:
            run_flags.append(f"--stop-delta={ss.stop_delta}")

        run_flags.extend(ss.generics_flags(design.tb.generics))
        x = self.elaborate(design.sim_sources, design.tb.top, design.language.vhdl)
        design.tb.top = x
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
        if self.cocotb and self.design.tb and self.design.tb.cocotb:
            success &= self.cocotb.parse_results()
        return success
