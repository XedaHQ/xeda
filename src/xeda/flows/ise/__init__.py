
# Xeda ISE Synthesis flow
# ©2021 Kamyar Mohajerani and contributors

from collections import abc
import logging
from typing import Optional, Sequence, Mapping, Union
from pydantic.types import NoneStr
from pathlib import Path
from ..flow import FPGA, SynthFlow

logger = logging.getLogger(__name__)

OptionValueType = Union[str, int, bool, float]
OptionsType = Mapping[str, OptionValueType]


class IseSynth(SynthFlow):

    class Settings(SynthFlow.Settings):
        clock_period: NoneStr = None
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

    def run(self):
        constraint_file = self.copy_from_template(f'constraints.xcf')
        ucf_file = self.copy_from_template(f'constraints.ucf')

        self.add_filter(
            'quote_str', lambda v: f"\"{v}\"" if isinstance(v, str) else v
        )

        script_path = self.copy_from_template(f'ise_synth.tcl')

        self.run_process('xtclsh', [script_path], initial_step='Starting ISE',
                         stdout_logfile='xeda_ise_stdout.log')

    def parse_reports(self):
        # self.parse_report_regex(self.design.name + ".twr", r'(?P<wns>\-?\d+')
        fail = not self.parse_report_regex(self.design.rtl.top + "_par.xrpt",
                                           r'stringID="PAR_SLICES" value="(?P<slice>\-?\d+)"',
                                           r'stringID="PAR_SLICE_REGISTERS" value="(?P<ff>\-?\d+)"',
                                           r'stringID="PAR_SLICE_LUTS" value="(?P<lut>\-?\d+)"',

                                           )
        fail |= not self.parse_report_regex(self.design.rtl.top + ".syr",
                                            r'Minimum period:\s+(?P<minimum_period>\-?\d+(?:\.\d+)?)ns\s+\(Maximum Frequency: (?P<maximum_frequency>\-?\d+(?:\.\d+)?)MHz\)',
                                            r'Slack:\s+(?P<wns>\-?\d+(?:\.\d+)?)ns'
                                            )

        self.results['success'] = not fail and self.results['wns'] > 0
