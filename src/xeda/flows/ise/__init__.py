"""Xilinx ISE Synthesis flow"""

import logging
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ...dataclass import Field
from ...design import SourceType
from ...flow import FlowFatalError, FpgaSynthFlow, describe_results
from ...tool import Docker, OptionalBoolOrPath, Tool
from ...utils import tcl_word, try_convert_to_primitives

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
            read_only: Sequence[Path] = (),
            print_command: bool = True,
            highlight_rules: Optional[Dict[str, str]] = None,
            merge_stderr: bool = False,
            timeout: float | None = None,
            tee: Path | None = None,
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
                read_only=read_only,
                print_command=print_command,
                highlight_rules=highlight_rules,
                # Every keyword of `Docker.run` must be forwarded: one accepted here and dropped
                # on the way down is silently ignored at the call site. `merge_stderr` was, so a
                # caller asking for stderr on the dockerized ISE path never got it.
                merge_stderr=merge_stderr,
                timeout=timeout,
                tee=tee,
            )

    executable: str = "xtclsh"
    docker: Docker = XTclShDocker(
        image="fpramme/xilinxise:centos6",
        platform="linux/amd64",
    )  # pyright: ignore[reportCallIssue]

    @cached_property
    def version(self) -> Tuple[str, ...]:
        return ("14", "7")


def format_value(v) -> str:
    """Render a project property the way ISE's `project set` expects it.

    Applied by the templates at render time, never by a validator: quoting is not idempotent,
    so normalizing on the way in turned `"High"` into `""High""` every time the settings were
    re-validated -- on any attribute assignment, and on every `settings.json` round trip.
    """
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return tcl_word(v)  # one literal word: `$` and `[` in it are never substituted
    return str(v)


class IseSynth(FpgaSynthFlow):
    """FPGA synthesis, implementation and bitstream generation using Xilinx ISE.

    Runs XST synthesis, translate, map and place & route ("Implement Design"), then bitgen
    ("Generate Programming File") in an ISE project, and reports resource utilization and timing.
    The bitstream, `<top>.bit`, is recorded as the `bitstream` artifact.
    """

    results_description = describe_results(
        "minimum_period",
        "maximum_frequency",
        "Fmax",
        "wns",
        "lut",
        "ff",
        "slice",
    )

    reads_sources = frozenset(
        {SourceType.Verilog, SourceType.VerilogHeader, SourceType.Vhdl, SourceType.Ucf}
    )

    class Settings(FpgaSynthFlow.Settings):
        # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx14_7/devref.pdf
        synthesis_options: OptionsType = Field(
            {
                "Optimization Effort": "High",
                "Global Optimization Goal": "AllClockNets",  # "AllClockNets", "Inpad To Outpad", "Offset In Before", "Offset Out After", "Maximum Delay"
                "Optimization Goal": "Speed",
                "Keep Hierarchy": "Soft",  # "No", "Yes", "Soft"
                "Optimize Instantiated Primitives": True,
                "Register Balancing": "NO",
                "Safe Implementation": "NO",
            },
            description="XST synthesis properties, as ISE names them in the project file. See the "
            "ISE Development System Reference Guide for the accepted values.",
        )
        map_options: OptionsType = Field(
            {
                # "Map Effort Level": "High", # "Standard", "High" # (S3/A/E/V4 only)
                "LUT Combining": "Auto",  # "Off", "Auto", "Area" (S6/V5/V6/7-series/Zynq only)
                "Placer Effort Level": "High",  # (S6/V5/V6/7-series/Zynq only)
                "Allow Logic Optimization Across Hierarchy": True,
                # "Perform Timing-Driven Packing and Placement": True, # (S3/A/E/V4 only()
                "Combinatorial Logic Optimization": True,
            },
            description="Properties for the `map` (technology mapping and packing) step.",
        )
        pnr_options: OptionsType = Field(
            {
                "Place & Route Effort Level (Overall)": "High",
            },
            description="Properties for the `par` (place and route) step.",
        )
        translate_options: OptionsType = Field(
            {}, description="Properties for the `ngdbuild` (translate) step."
        )
        trace_options: OptionsType = Field(
            {"Report Type": "Verbose Report"},
            description="Properties for the `trce` (static timing analysis) step.",
        )
        xcf_file: Union[None, Path, str] = Field(
            None, description="XST constraint file (.xcf) applied during synthesis."
        )
        ucf_files: List[Union[Path, str]] = Field(
            [], description="User constraint files (.ucf) with pin and timing constraints."
        )

    def init(self) -> None:
        # ISE names its outputs after the top (`<top>.bit`): a design without one cannot run.
        if not self.design.rtl.top:
            raise FlowFatalError(
                f"{self.name} needs the design's top-level entity or module: set `rtl.top`."
            )

    def project_outputs(self) -> dict[str, Path]:
        """The files of the ISE project this flow records, by artifact label. ISE names them
        after the top."""
        top = self.design.rtl.top
        return {
            "place_route_report": self.run_path / f"{top}_par.xrpt",
            "synthesis_report": self.run_path / f"{top}.syr",
            "bitstream": self.run_path / f"{top}.bit",
        }

    def run(self) -> None:
        # here, not in `init()`, which also runs for a fresh flow whose outputs are reused;
        # a project left in the directory is reopened with its previous sources and outputs, and
        # a previous run's outputs must not pass for this run's
        logger.info("Deleting previous artifacts as ISE needs to run in a clean directory.")
        self.purge_run_path()
        assert isinstance(self.settings, self.Settings)
        if self.settings.xcf_file is None:
            self.settings.xcf_file = self.copy_from_template("constraints.xcf")
        self.settings.ucf_files.append(self.copy_from_template("constraints.ucf"))

        self.add_template_global_func(format_value)

        script_path = self.copy_from_template("ise_synth.tcl")
        xtclsh = XTclSh()  # type: ignore
        xtclsh.run(script_path)
        for label, path in self.project_outputs().items():
            if path.is_file() and self.written_by_this_run(path):
                self.artifacts[label] = path
        if "bitstream" not in self.artifacts:
            raise FlowFatalError(
                f"ISE's bitgen did not write the bitstream {self.project_outputs()['bitstream']}."
            )

    def parse_reports(self) -> bool:
        outputs = self.project_outputs()
        # self.parse_report_regex(self.design.name + ".twr", r'(?P<wns>\-?\d+')
        fail = not self.parse_report_regex(
            outputs["place_route_report"],
            r'stringID="PAR_SLICES" value="(?P<slice>\-?\d+)"',
            r'stringID="PAR_SLICE_REGISTERS" value="(?P<ff>\-?\d+)"',
            r'stringID="PAR_SLICE_LUTS" value="(?P<lut>\-?\d+)"',
        )
        fail |= not self.parse_report_regex(
            outputs["synthesis_report"],
            r"Minimum period:\s+(?P<minimum_period>\-?\d+(?:\.\d+)?)ns\s+\(Maximum Frequency: (?P<maximum_frequency>\-?\d+(?:\.\d+)?)MHz\)",
            r"Slack:\s+(?P<wns>\-?\d+(?:\.\d+)?)ns",
        )
        if "wns" in self.results:
            wns = try_convert_to_primitives(self.results["wns"])
            fail |= not isinstance(wns, (float, int)) or wns < 0
        return not fail
