import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from ...dataclass import Field
from ...design import SourceType
from ...flow import FpgaSynthFlow
from ...flows.ghdl import GhdlSynth
from ...tool import Docker, Tool
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
        rtl_verilog: Optional[str] = None  # "rtl.v"
        rtl_vhdl: Optional[str] = None  # "rtl.vhdl"
        rtl_json: Optional[str] = None  # "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            "-stretch",
            "-enum",
            "-width",
        ]
        show_netlist: bool = False
        show_netlist_flags: List[str] = ["-stretch", "-enum"]
        post_synth_opt: bool = Field(
            True,
            description="run additional optimization steps after synthesis if complete",
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area", description="Optimization target"
        )
        stop_after: Optional[Literal["rtl"]]
        defines: Dict[str, Any] = {}
        keep_hierarchy: List[str] = []
        black_box: List[str] = []
        adder_map: Optional[str] = None
        clockgate_map: Optional[str] = None
        other_maps: List[str] = []
        abc_constr: List[str] = []
        abc_script: Union[None, Path, List[str]] = None
        insbuf: Optional[Tuple[str, str, str]] = None
        top_is_vhdl: Optional[bool] = Field(
            None,
            description="set to `true` to specify top module is VHDL, or `false` to override detection based on last source.",
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys = Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # pyright: reportGeneralTypeIssues=none
        )
        self.artifacts.timing_report = "timing.rpt"
        self.artifacts.utilization_report = (
            "utilization.json" if yosys.version_gte(0, 21) else "utilization.rpt"
        )
        yosys_family_name = {"artix-7": "xc7"}
        if ss.fpga:
            assert ss.fpga.family or ss.fpga.vendor == "xilinx"
            if ss.fpga.vendor == "xilinx" and ss.fpga.family:
                ss.fpga.family = yosys_family_name.get(ss.fpga.family, "xc7")
        if ss.rtl_json:
            self.artifacts.rtl_json = ss.rtl_json
        if ss.rtl_vhdl:
            self.artifacts.rtl_vhdl = ss.rtl_vhdl
        if ss.rtl_verilog:
            self.artifacts.rtl_verilog = ss.rtl_verilog
        if not ss.stop_after:  # FIXME
            self.artifacts.netlist_verilog = "netlist.v"
            self.artifacts.netlist_json = "netlist.json"

        if ss.sta:
            ss.flatten = True
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")

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
        if ss.top_is_vhdl is True or (
            ss.top_is_vhdl is None and self.design.rtl.sources[-1].type is SourceType.Vhdl
        ):
            # generics were already handled by GHDL and the synthesized design is no longer parametric
            self.design.rtl.parameters = {}

        script_path = self.copy_from_template(
            "yosys_fpga_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=True,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            abc_script_file=abc_script_file,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = ["-c", script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(["-T", "-Q"])
            if not ss.debug and not ss.verbose:
                args.append("-q")
        self.results["_tool"] = yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        yosys.run(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if self.artifacts.utilization_report.endswith(".json"):
            try:
                with open(self.artifacts.utilization_report, "r") as f:
                    content = f.read()
                i = content.find("{")  # yosys bug (FIXED)
                if i >= 0:
                    content = content[i:]
                utilization = json.loads(content)
                mod_util = utilization.get("modules")
                if mod_util:
                    self.results["_module_utilization"] = mod_util
                design_util = utilization.get("design")
                if design_util:
                    num_cells_by_type = design_util.get("num_cells_by_type")
                    if num_cells_by_type:
                        design_util = {
                            **{k: v for k, v in design_util.items() if k != "num_cells_by_type"},
                            **num_cells_by_type,
                        }
                    self.results["design_utilization"] = design_util

            except json.decoder.JSONDecodeError as e:
                log.error("Failed to decode JSON %s: %s", self.artifacts.utilization_report, e)
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