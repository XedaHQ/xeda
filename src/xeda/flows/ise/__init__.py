# Xeda ISE Synthesis flow
# ©2021 Kamyar Mohajerani and contributors

import logging
from typing import Mapping, Union
from ..flow import FpgaSynthFlow
from ...tool import Tool

logger = logging.getLogger(__name__)

OptionValueType = Union[str, int, bool, float]
OptionsType = Mapping[str, OptionValueType]


class IseSynth(FpgaSynthFlow):
    """FPGA synthesis using Xilinx ISE"""

    class Settings(FpgaSynthFlow.Settings):
        # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx14_7/devref.pdf
        synthesis_options: OptionsType = {
            "Optimization Effort": "High",
            "Optimization Goal": "Speed",
            "Keep Hierarchy": "No",
        }
        map_options: OptionsType = {
            "Global Optimization": "Speed",
            "Map Effort Level": "High",
            "Placer Effort Level": "High",
            "Perform Timing-Driven Packing and Placement": True,
            "Combinatorial Logic Optimization": True,
        }
        pnr_options: OptionsType = {
            "Place & Route Effort Level (Overall)": "High",
        }
        trace_options: OptionsType = {
            "Report Type": "Verbose Report",
        }

    def run(self) -> None:
        xcf_file = self.copy_from_template(f"constraints.xcf")
        ucf_file = self.copy_from_template(f"constraints.ucf")

        self.add_template_filter(
            "quote_str", lambda v: f'"{v}"' if isinstance(v, str) else v
        )

        script_path = self.copy_from_template(
            "ise_synth.tcl",
            xcf_file=xcf_file,
            ucf_file=ucf_file,
        )
        xtclsh = Tool("xtclsh")
        xtclsh.run(script_path)

    def parse_reports(self) -> bool:
        # self.parse_report_regex(self.design.name + ".twr", r'(?P<wns>\-?\d+')
        fail = not self.parse_report_regex(
            self.design.rtl.primary_top + "_par.xrpt",
            r'stringID="PAR_SLICES" value="(?P<slice>\-?\d+)"',
            r'stringID="PAR_SLICE_REGISTERS" value="(?P<ff>\-?\d+)"',
            r'stringID="PAR_SLICE_LUTS" value="(?P<lut>\-?\d+)"',
        )
        fail |= not self.parse_report_regex(
            self.design.rtl.primary_top + ".syr",
            r"Minimum period:\s+(?P<minimum_period>\-?\d+(?:\.\d+)?)ns\s+\(Maximum Frequency: (?P<maximum_frequency>\-?\d+(?:\.\d+)?)MHz\)",
            r"Slack:\s+(?P<wns>\-?\d+(?:\.\d+)?)ns",
        )

        return not fail and self.results["wns"] > 0