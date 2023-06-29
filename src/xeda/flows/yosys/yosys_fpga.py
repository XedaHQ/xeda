import logging
from pathlib import Path
from typing import List, Literal, Optional, Union

from ...dataclass import Field
from ...flow import FpgaSynthFlow
from ...flows.ghdl import GhdlSynth
from .common import YosysBase, append_flag, process_parameters

log = logging.getLogger(__name__)


class YosysFpga(YosysBase, FpgaSynthFlow):
    """
    Yosys Open SYnthesis Suite: FPGA synthesis
    """

    class Settings(YosysBase.Settings, FpgaSynthFlow.Settings):
        abc9: bool = Field(True, description="Use abc9")
        retime: bool = Field(False, description="Enable flip-flop retiming")
        nobram: bool = Field(False, description="Do not map to block RAM cells")
        nodsp: bool = Field(False, description="Do not use DSP resources")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`)",
        )
        nowidelut: bool = Field(
            False,
            description="Do not use MUX resources to implement LUTs larger than native for the target",
        )
        abc_dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        widemux: int = Field(
            0,
            description="enable inference of hard multiplexer resources for muxes at or above this number of inputs"
            " (minimum value 2, recommended value >= 5 or disabled = 0)",
        )
        synth_flags: List[str] = []
        abc_flags: List[str] = []
        post_synth_opt: bool = Field(
            True,
            description="run additional optimization steps after synthesis if complete",
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area", description="Optimization target"
        )
        stop_after: Optional[Literal["rtl"]]
        black_box: List[str] = []
        adder_map: Optional[str] = None
        clockgate_map: Optional[str] = None
        other_maps: List[str] = []
        abc_constr: List[str] = []
        abc_script: Union[None, Path, List[str]] = None

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_family_name = {"artix-7": "xc7"}
        if ss.fpga:
            assert ss.fpga.family or ss.fpga.vendor == "xilinx"
            if ss.fpga.vendor == "xilinx" and ss.fpga.family:
                ss.fpga.family = yosys_family_name.get(ss.fpga.family, "xc7")
        self.artifacts.timing_report = ss.reports_dir / "timing.rpt"
        self.artifacts.utilization_report = ss.reports_dir / "utilization.json"
        # if ss.noabc:
        #     self.artifacts.utilization_report = None
        # add FPGA-specific synth_xx flags
        if ss.abc9:  # ABC9 is for only FPGAs?
            append_flag(ss.synth_flags, "-abc9")
        if ss.retime:
            append_flag(ss.synth_flags, "-retime")
        if ss.abc_dff:
            append_flag(ss.synth_flags, "-dff")
        if ss.nobram:
            append_flag(ss.synth_flags, "-nobram")
        if ss.nolutram:
            append_flag(ss.synth_flags, "-nolutram")
        if ss.nodsp:
            append_flag(ss.synth_flags, "-nodsp")
        if ss.nowidelut:
            append_flag(ss.synth_flags, "-nowidelut")
        if ss.widemux:
            append_flag(ss.synth_flags, f"-widemux {ss.widemux}")

        abc_constr_file = None
        if ss.abc_constr:
            abc_constr_file = "abc.constr"
            with open(abc_constr_file, "w") as f:
                f.write("\n".join(ss.abc_constr) + "\n")
        abc_script_file = None
        if ss.abc_script:
            if isinstance(ss.abc_script, list):
                abc_script_file = "abc.script"
                with open(abc_script_file, "w") as f:
                    f.write("\n".join(ss.abc_script) + "\n")
            else:
                abc_script_file = str(ss.abc_script)

        script_path = self.copy_from_template(
            "yosys_fpga_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            abc_script_file=abc_script_file,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = ["-c", script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args.extend(["-L", ss.log_file])
        if ss.log_file and not ss.verbose:  # reduce noise when have log_file, unless verbose
            args.extend(["-T", "-Q"])
            if not ss.debug and not ss.verbose:
                args.append("-q")
        self.yosys.run(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if not self.artifacts.utilization_report:
            return True

        if Path(self.artifacts.utilization_report).suffix == ".json":
            utilization = self.get_utilization()
            if not utilization:
                return False
            mod_util = utilization.get("modules")
            if mod_util:
                self.results["_hierarchical_utilization"] = mod_util
            design_util = utilization.get("design")
            if design_util:
                num_cells_by_type = design_util.get("num_cells_by_type")
                if num_cells_by_type:
                    design_util = {
                        **{k: v for k, v in design_util.items() if k != "num_cells_by_type"},
                        **num_cells_by_type,
                    }
                    self.results["_utilization"] = design_util
        else:
            if self.settings.fpga:
                if self.settings.fpga.vendor == "xilinx":
                    self.parse_report_regex(
                        self.artifacts.utilization_report,
                        r"=+\s*design hierarchy\s*=+",
                        r"DSP48(E\d+)?\s*(?P<DSP48>\d+)",
                        r"FDRE\s*(?P<_FDRE>\d+)",
                        r"FDSE\s*(?P<_FDSE>\d+)",
                        r"number of LCs:\s*(?P<Estimated_LCs>\d+)",
                        sequential=True,
                        required=False,
                    )
                    self.results["FFs"] = int(self.results.get("_FDRE", 0)) + int(
                        self.results.get("_FDSE", 0)
                    )
                if self.settings.fpga.family == "ecp5":
                    self.parse_report_regex(
                        self.artifacts.utilization_report,
                        r"TRELLIS_FF\s+(?P<FFs>\d+)",
                        r"LUT4\s+(?P<LUT4>\d+)",
                    )
        return True
