"""Xilinx ISE Synthesis flow"""
from functools import cached_property
import logging
from typing import Mapping, Tuple, Union

from pydantic import validator

from ...tool import Docker, Tool
from ...utils import try_convert_to_primitives
from ...flow import FpgaSynthFlow

logger = logging.getLogger(__name__)

OptionValueType = Union[str, int, bool, float]
OptionsType = Mapping[str, OptionValueType]


class XTclSh(Tool):
    executable: str = "xtclsh"
    docker: Docker = Docker(
        image="goreganesh/xilinx:latest",
        platform="linux/amd64",
        command="/opt/Xilinx/14.7/ISE_DS/ISE/bin/lin64/xtclsh",
    )

    @cached_property
    def version(self) -> Tuple[str, ...]:
        return ("14", "7")


def format_value(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


class IseSynth(FpgaSynthFlow):
    """FPGA synthesis using Xilinx ISE"""

    class Settings(FpgaSynthFlow.Settings):
        # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx14_7/devref.pdf
        synthesis_options: OptionsType = {
            "Optimization Effort": "High",
            "Global Optimization Goal": "AllClockNets",  # "AllClockNets", "Inpad To Outpad", "Offset In Before", "Offset Out After", "Maximum Delay"
            "Optimization Goal": "Speed",
            "Keep Hierarchy": "Soft",  # "No", "Yes", "Soft"
            "Optimize Instantiated Primitives": True,
            "Register Balancing": "NO",
            "Safe Implementation": "NO",
        }
        map_options: OptionsType = {
            # "Map Effort Level": "High", # "Standard", "High" # (S3/A/E/V4 only)
            "LUT Combining": "Auto",  # "Off", "Auto", "Area" (S6/V5/V6/7-series/Zynq only)
            "Placer Effort Level": "High",  # (S6/V5/V6/7-series/Zynq only)
            "Allow Logic Optimization Across Hierarchy": True,
            # "Perform Timing-Driven Packing and Placement": True, # (S3/A/E/V4 only()
            "Combinatorial Logic Optimization": True,
        }
        pnr_options: OptionsType = {
            "Place & Route Effort Level (Overall)": "High",
        }
        translate_options: OptionsType = {}
        trace_options: OptionsType = {
            "Report Type": "Verbose Report",
        }

        @validator(
            "synthesis_options",
            "map_options",
            "pnr_options",
            "trace_options",
            pre=True,
            always=True,
        )
        def _pre_process_values(cls, value):
            if isinstance(value, dict):
                for k, v in value.items():
                    value[k] = format_value(v)
            return value

    def run(self) -> None:
        xcf_file = self.copy_from_template("constraints.xcf")
        ucf_file = self.copy_from_template("constraints.ucf")

        self.add_template_global_func(format_value)

        script_path = self.copy_from_template(
            "ise_synth.tcl",
            xcf_file=xcf_file,
            ucf_file=ucf_file,
        )
        xtclsh = XTclSh()  # type: ignore
        xtclsh.run(script_path)

    def parse_reports(self) -> bool:
        top = self.design.rtl.top
        assert top
        # self.parse_report_regex(self.design.name + ".twr", r'(?P<wns>\-?\d+')
        fail = not self.parse_report_regex(
            top + "_par.xrpt",
            r'stringID="PAR_SLICES" value="(?P<slice>\-?\d+)"',
            r'stringID="PAR_SLICE_REGISTERS" value="(?P<ff>\-?\d+)"',
            r'stringID="PAR_SLICE_LUTS" value="(?P<lut>\-?\d+)"',
        )
        fail |= not self.parse_report_regex(
            top + ".syr",
            r"Minimum period:\s+(?P<minimum_period>\-?\d+(?:\.\d+)?)ns\s+\(Maximum Frequency: (?P<maximum_frequency>\-?\d+(?:\.\d+)?)MHz\)",
            r"Slack:\s+(?P<wns>\-?\d+(?:\.\d+)?)ns",
        )
        if "wns" in self.results:
            wns = try_convert_to_primitives(self.results["wns"])
            fail |= not isinstance(wns, (float, int)) or wns < 0
        return not fail
