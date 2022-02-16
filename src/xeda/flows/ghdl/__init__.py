from typing import List, Optional, Union, Tuple
from pydantic import Field, NoneStr, validator
import logging
import platform
import os
try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal


from ..flow import Flow, SimFlow, SynthFlow
from ...flows.design import Design
from ...tool import Tool

logger = logging.getLogger(__name__)


def append_flag(flag_list: List[str], flag: str):
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


class Ghdl(Tool):
    """GHDL VHDL simulation, synthesis, and linting tool: https://ghdl.readthedocs.io"""
    docker_image: NoneStr = "hdlc/sim:osvb"
    default_executable: NoneStr = "ghdl"

    class GhdlSettings(Tool.Settings):
        analysis_flags: List[str] = []
        elab_flags: List[str] = [
            '--syn-binding'
        ]
        warn_flags: List[str] = [
            '--warn-binding', '--warn-default-binding', '--warn-reserved', '--warn-library',
            '--warn-vital-generic',
            '--warn-shared',
            '--warn-runtime-error', '--warn-body', '--warn-specs',
            '--warn-unused', '--warn-static',
            '--warn-parenthesis'
        ]
        werror: bool = Field(
            False, description="warnings are always considered as errors")
        elab_werror: bool = Field(
            False, description="During elaboration, warnings are considered as errors")
        relaxed: bool = True
        clean: bool = True
        diagnostics: bool = True
        work: NoneStr = Field(
            None, description="Set the name of the WORK library")
        lib_paths: Union[str, List[str]] = Field(
            [], description="Additional directories to add to the library search path")
        optimization_flags: List[str] = Field(
            [], description="Optimization flags")

        @validator('lib_paths', pre=False)
        def ghdl_settings_validator(cls, value, values):
            if isinstance(value, str):
                value = [value]
            return value

    def get_info(self):  # from Tool
        out = self.run_tool(
            self.default_executable,
            ["--version"],
            stdout=True,
        )
        lines = [l.strip() for l in out.splitlines()]
        return {
            'version': lines[0],
            'compiler': lines[1],
            'backend': lines[2]
        }

    @staticmethod
    def common_flags(ss, vhdl) -> List[str]:
        cf: List[str] = []
        if vhdl.standard:
            cf.append(f"--std={vhdl.standard}")
        if vhdl.synopsys:
            cf.append('-fsynopsys')
        if ss.work:
            cf.append(f'--work={ss.work}')
        cf += [f'-P{p}' for p in ss.lib_paths]
        return cf

    @staticmethod
    def generics_flags(generics) -> List[str]:
        if not generics:
            return []
        return [
            f"-g{k}={v}" for k, v in generics.items()
        ]

    @classmethod
    def get_flags(cls, ss, vhdl, stage: str) -> List[str]:
        common = cls.common_flags(ss, vhdl)
        warn_flags: List[str] = ss.warn_flags
        analysis_flags: List[str] = common + ss.analysis_flags
        elab_flags: List[str] = common + ss.elab_flags
        find_top_flags = common
        if stage == "common":
            return common
        if stage == "remove":
            return common
        if ss.werror:
            warn_flags += ['--warn-error']
        elif ss.elab_werror:
            elab_flags += ['--warn-error']
        if ss.relaxed:
            analysis_flags.extend(
                ['-frelaxed-rules', '-frelaxed', '--mb-comments'])
            elab_flags.extend(['-frelaxed'])
        if ss.verbose:
            analysis_flags.append('-v')
            elab_flags.append('-v')
        if ss.diagnostics:
            elab_flags.extend(['-fcaret-diagnostics', '-fcolor-diagnostics'])
        if ss.work:
            elab_flags.append('-v')
        if vhdl.synopsys:
            analysis_flags.append('--ieee=synopsys')
        if stage == "import" or stage == "analyze":
            return analysis_flags + warn_flags
        elif stage == "make" or stage == "elaborate":
            return elab_flags + warn_flags
        elif stage == "find-top":
            return find_top_flags
        else:
            assert False, "unknown stage!"

    def elaborate(self, sources, top, vhdl) -> Tuple[str, str]:
        """returns top(s) as a list"""
        steps = ["import", "make"]
        ss = self.settings
        opt_flags = ss.optimization_flags
        if ss.clean:
            steps.insert(0, "remove")
        if not top:
            # run find-top after import
            steps.insert(steps.index("import") + 1, "find-top")
        elif isinstance(top, str):
            top = [top]
        elif isinstance(top, tuple):
            top = [t for t in top if t]
        for step in steps:
            args = self.get_flags(ss, vhdl, step)
            if step in ["import", "analyze"]:
                args += opt_flags + sources
            elif step in ["make", "elaborate"]:
                if self.info.get('backend', '').lower().startswith("llvm"):
                    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
                        args += ['-Wl,-Wl,-no_compact_unwind']
                args += opt_flags + top
            if step == "find-top":
                out = self.run_tool(
                    self.default_executable,
                    [step, *args],
                    stdout=True
                )
                top = [out.strip()]
                logger.warn(f"setting top to {top}")
            else:
                self.run_tool(
                    self.default_executable,
                    [step, *args]
                )
        return (top[0], top[1]) if len(top) == 2 else top[0]


class GhdlSynth(Ghdl, SynthFlow):
    class Settings(Ghdl.GhdlSettings, SynthFlow.Settings):
        vendor_library: NoneStr = Field(
            None, description="Any unit from this library is a black box")
        no_formal: bool = Field(
            True, description="Neither synthesize assert nor PSL. Required for yosys+nextpnr flow.")
        no_assert_cover: bool = Field(
            False, description="Cover PSL assertion activation")
        assert_assumes: bool = Field(
            False, description="Treat all PSL asserts like PSL assumes")
        assume_asserts: bool = Field(
            False, description="Treat all PSL assumes like PSL asserts")
        out: Optional[Literal['vhdl', 'raw-vhdl', 'verilog', 'dot', 'none',
                              'raw', 'dump']] = Field(None, description="Type of output to generate")
        out_file: Optional[str] = None

    def run(self) -> bool:
        design = self.design
        ss = self.settings
        top = self.elaborate(design.rtl.sources,
                             design.rtl.top, design.language.vhdl)
        args = self.synth_args(ss, design, one_shot_elab=False, top=top)
        self.run_tool(
            self.default_executable,
            ["synth", *args],
            stdout=ss.out_file
        )
        self.results['success'] = True
        return True

    @classmethod
    def synth_args(cls, ss,  design: Design, one_shot_elab=True, top: Optional[Union[str, List[str]]] = None) -> List[str]:
        flags = cls.get_flags(ss, design.language.vhdl, "elaborate")
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

        flags.extend(cls.generics_flags(design.rtl.generics))
        if one_shot_elab:
            flags += [str(v) for v in design.rtl.sources if v.type == "vhdl"]
            flags.append('-e')
        if not top:
            top = design.rtl.top
        if top:
            if isinstance(top, str):
                flags.append(top)
            else:
                flags.extend(top)
        return flags

    def parse_reports(self):
        return self.results['success']


class GhdlLint(Ghdl, Flow):
    class Settings(Ghdl.GhdlSettings):
        pass


class GhdlSim(Ghdl, SimFlow):
    cocotb_sim_name = "ghdl"

    class Settings(Ghdl.GhdlSettings, SimFlow.Settings):
        run_flags: List[str] = [
            '--ieee-asserts=disable-at-0'
        ]
        optimization_flags: List[str] = Field(
            ['-O3'], description="Simulation optimization flags")
        sdf: Union[bool, None, List[str], str] = Field(
            None, description="Do VITAL annotation on PATH with SDF file.")
        wave: Union[bool, None, str] = None
        stop_delta: NoneStr = Field(
            None, description="Stop the simulation after N delta cycles in the same current time. The default is 5000.")
        debug: bool = Field(
            False, description="Enable simulation and runtime debugging flags")

    def run(self):
        design = self.design
        # TODO workdir?
        ss: GhdlSim.Settings = self.settings
        cf = self.common_flags(ss, design.language.vhdl)
        run_flags = self.settings.run_flags
        sdf = ss.sdf
        # --sdf=min=PATH=FILENAME
        # --sdf=typ=PATH=FILENAME
        # --sdf=max=PATH=FILENAME
        if sdf:
            # FIXME!
            for s in sdf:
                if isinstance(s, str):
                    s = {"file": s}
                root = s.get("root", self.design.tb.uut)
                assert root, "neither SDF root nor tb.uut are provided"
                run_flags.append(
                    f'--sdf={s.get("delay", "max")}={root}={s["file"]}')
        wave = ss.wave
        if self.vcd:
            run_flags.append(f'--vcd={self.vcd}')
            logger.warning(f"Dumping VCD to {self.vcd}")
        elif wave:
            if isinstance(wave, bool):
                wave = design.name
            if not wave.endswith('.ghw'):
                wave += '.ghw'
            run_flags.append(f'--wave={wave}')
            logger.warning(f"Dumping GHW to {wave}")
        vpi = None
        # TODO factor out cocotb handling
        if design.tb.cocotb and self.cocotb:
            vpi = self.cocotb.vpi_path()
            # tb_generics = list(design.tb.generics)  # TODO pass to cocotb?
            design.tb.generics = design.rtl.generics

        if vpi:
            run_flags.append(f"--vpi={vpi}")
        if ss.debug:
            run_flags.extend(['--trace-processes', '--checks', ])
        if ss.stop_time:
            run_flags.append(f'--stop-time={ss.stop_time}')
        if ss.stop_delta:
            run_flags.append(f'--stop-delta={ss.stop_delta}')

        run_flags.extend(self.generics_flags(design.tb.generics))
        design.tb.top = self.elaborate(
            design.sim_sources,
            design.tb.top,
            design.language.vhdl
        )
        args = cf + design.sim_tops + run_flags
        env = {}
        if design.tb.cocotb and self.cocotb:
            # assert design.tb.top, "tb.top not defined"
            coco_module = design.tb.sources[0].file.stem
            tb_top_path = design.tb.sources[0].file.parent
            ppath = []
            current_ppath = os.environ.get('PYTHONPATH')
            if current_ppath:
                ppath = current_ppath.split(os.pathsep)
            ppath.append(str(tb_top_path))
            env = {
                "MODULE": coco_module,
                "TOPLEVEL": design.tb.top[0],  # TODO
                "TOPLEVEL_LANG": "vhdl",
                "COCOTB_REDUCED_LOG_FMT": 1,
                "PYTHONPATH": os.pathsep.join(ppath),
            }
            if design.tb.testcase:
                env['TESTCASE'] = design.tb.testcase
        self.run_tool(
            self.default_executable,
            ["run", *args],
            env
        )
        self.results['success'] = True
        return True

    def parse_reports(self):
        return self.results['success']
