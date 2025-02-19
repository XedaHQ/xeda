import logging
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Union

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
        flow3: bool = Field(
            True, description="Use flow3, which runs the mapping several times, if abc9 is set"
        )
        retime: bool = Field(False, description="Enable flip-flop retiming")
        nobram: bool = Field(False, description="Do not map to block RAM cells")
        nodsp: bool = Field(False, description="Do not use DSP resources")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`)",
        )
        nowidelut: bool = Field(
            True,
            description="Do not use MUX resources to implement LUTs larger than native for the target",
        )
        abc_dff: bool = Field(False, description="Run abc/abc9 with -dff option")
        widemux: int = Field(
            0,
            description="enable inference of hard multiplexer resources for muxes at or above this number of inputs"
            " (minimum value 2, recommended value >= 5 or disabled = 0)",
        )
        synth_flags: List[str] = []
        pre_synth_opt: bool = Field(
            False,
            description="run additional optimization steps before synthesis",
        )
        post_synth_opt: bool = Field(
            False,
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

        script_path = self.copy_from_template(
            "yosys_fpga_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=True),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
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
                if self.settings.fpga.vendor == "xilinx":
                    self.results["LUT"] = sum_all_resources(
                        design_util, [f"LUT{i}" for i in range(2, 7)]
                    )
                    ram32m = sum_all_resources(design_util, ["RAM32M"])
                    if ram32m:
                        self.results["LUT"] += ram32m
                        self.results["LUT:RAM"] = ram32m
                    self.results["FF"] = sum_all_resources(
                        design_util,
                        [
                            "FDCE",  # D Flip-Flop with Clock Enable and Asynchronous Clear
                            "FDPE",  # D Flip-Flop with Clock Enable and Asynchronous Preset
                            "FDRE",  # D Flip-Flop with Clock Enable and Synchronous Reset
                            "FDSE",  # D Flip-Flop with Clock Enable and Synchronous Set
                        ],
                    )
                    latches = sum_all_resources(
                        design_util,
                        [
                            "LDCE",  # Transparent Data Latch with Asynchronous Clear and Gate Enable
                            "LDPE",  # Transparent Data Latch with Asynchronous Preset and Gate Enable
                        ],
                    )
                    if latches:
                        self.results["LATCH"] = latches
                    brams: float = sum_all_resources(design_util, ["RAMB36"])
                    brams_half = sum_all_resources(design_util, ["RAMB18"])
                    brams += brams_half / 2
                    if brams:
                        self.results["BRAM"] = brams
                    dsps = sum_all_resources(design_util, ["DSP48E1", "DSP48E2", "DSP48E"])
                    if dsps:
                        self.results["DSP"] = dsps
                    carry_chains = sum_all_resources(design_util, ["CARRY8", "CARRY4", "CARRY2"])
                    if carry_chains:
                        self.results["CARRY"] = carry_chains
                    muxf78 = sum_all_resources(design_util, ["MUXF7", "MUXF8"])
                    if carry_chains:
                        self.results["MUXF7/F8"] = muxf78

        # if self.settings.fpga:
        return True


def sum_all_resources(design_util: dict, lst: Iterable) -> int:
    return sum(int(design_util.get(t, 0)) for t in lst)
