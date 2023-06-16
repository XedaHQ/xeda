"""Xilinx ISE Synthesis flow"""
from functools import cached_property
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from pydantic import validator

from ...tool import Docker, OptionalBoolOrPath, OptionalPath, Tool
from ...utils import try_convert_to_primitives
from ...flow import FpgaSynthFlow

logger = logging.getLogger(__name__)

OptionValueType = Union[str, int, bool, float]
OptionsType = Mapping[str, OptionValueType]


class XTclSh(Tool):
    class XTclShDocker(Docker):
        command: List[str] = ["bash"]

        def run(
            self,
            executable,
            *args: Any,
            env: Optional[Dict[str, Any]] = None,
            stdout: OptionalBoolOrPath = None,
            check: bool = True,
            root_dir: OptionalPath = None,
            print_command: bool = True,
        ) -> Union[None, str]:
            XILINX = "/opt/Xilinx/14.7/ISE_DS"
            args_str = " ".join(str(a) for a in args)
            executable = "bash"
            new_args = [
                "-c",
                f"source {XILINX}/settings64.sh && {XILINX}/ISE/bin/lin64/xtclsh {args_str}",
            ]
            return super().run(
                executable,
                *new_args,
                env=env,
                stdout=stdout,
                check=check,
                root_dir=root_dir,
                print_command=print_command,
            )

    executable: str = "xtclsh"
    docker: Docker = XTclShDocker(
        image="fpramme/xilinxise:centos6",
        platform="linux/amd64",
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
        xcf_file: Union[None, Path, str] = None
        ucf_files: List[Union[Path, str]] = []

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

    def init(self):
        logger.info("Deleting previous artifacts as ISE needs to run in a clean direcotry.")
        self.clean()

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        if self.settings.xcf_file is None:
            self.settings.xcf_file = self.copy_from_template("constraints.xcf")
        self.settings.ucf_files.append(self.copy_from_template("constraints.ucf"))

        self.add_template_global_func(format_value)

        script_path = self.copy_from_template("ise_synth.tcl")
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
