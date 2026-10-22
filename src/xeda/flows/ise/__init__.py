"""Flow for FPGA implementation with Xilinx ISE."""

import logging
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ...dataclass import Field
from ...design import SOURCE_SUFFIXES, Design, SourceType
from ...flow import Flow, FlowFatalError, FlowSettingsException, FpgaSynthFlow, describe_results
from ...tool import Docker, OptionalBoolOrPath, Tool
from ...utils import replacing_copy, tcl_word, try_convert_to_primitives

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
            cwd: Path | None = None,
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
                # Forward every keyword of `Docker.run`. A keyword that this method accepts and
                # does not pass on is ignored without a message at the call site.
                merge_stderr=merge_stderr,
                timeout=timeout,
                tee=tee,
                cwd=cwd,
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

    The templates apply it when they render. A validator must not apply it, because quoting is
    not idempotent: each validation of the settings (every attribute assignment and every
    `settings.json` reload) would turn `"High"` into `""High""`.
    """
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return tcl_word(v)  # one literal word: `$` and `[` in it are never substituted
    return str(v)


class IseSynth(FpgaSynthFlow):
    """Synthesize, implement and generate a bitstream for a design with Xilinx ISE.

    Run XST synthesis, translate, map and place and route ("Implement Design"), then bitgen
    ("Generate Programming File"), in an ISE project. Report resource utilization and timing.
    The bitstream, `<top>.bit`, is recorded as the `bitstream` artifact.
    """

    executables = ("xtclsh",)

    results_description = describe_results(
        "minimum_period",
        "fmax",
        "wns",
        "lut",
        "ff",
        "slice",
    )

    reads_sources = frozenset(
        {SourceType.Verilog, SourceType.VerilogHeader, SourceType.Vhdl, SourceType.Ucf}
    )

    class Settings(FpgaSynthFlow.Settings):
        # The keys are the names ISE gives its project properties (`Keep Hierarchy`), so a dot
        # in one is part of the name.
        opaque_key_fields = (
            "synthesis_options",
            "map_options",
            "pnr_options",
            "translate_options",
            "trace_options",
        )

        # See https://www.xilinx.com/support/documentation/sw_manuals/xilinx14_7/devref.pdf
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
                # "Perform Timing-Driven Packing and Placement": True, # (S3/A/E/V4 only)
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

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design without a top module: ISE names its outputs after it (`<top>.bit`)."""
        super().check_design_supported(design, settings)
        if not design.rtl.top:
            raise FlowSettingsException(f"{cls.name} needs the design's top module: set `rtl.top`.")

    def init(self) -> None:
        # A flow built directly is not planned, so check here.
        self.check_design_supported(self.design, self.settings)

    def project_outputs(self) -> dict[str, Path]:
        """Return the files of the ISE project that this flow records, by artifact label. ISE
        names them after the top."""
        top = self.design.rtl.top
        return {
            "place_route_report": self.run_path / f"{top}_par.xrpt",
            "synthesis_report": self.run_path / f"{top}.syr",
            "bitstream": self.run_path / f"{top}.bit",
        }

    def run(self) -> None:
        # Delete here, not in `init()`: `init()` also runs for a flow that is up to date, and
        # its outputs are reused. ISE reopens a project that it finds in the directory, with the
        # old sources and outputs, and an old output must not pass for this run's.
        logger.info("Deleting previous artifacts: ISE needs a clean directory.")
        self.purge_run_path()
        assert isinstance(self.settings, self.Settings)
        if self.settings.xcf_file is None:
            self.settings.xcf_file = self.copy_from_template("constraints.xcf")
        self.settings.ucf_files.append(self.copy_from_template("constraints.ucf"))

        self.add_template_global_func(format_value)

        # `xfile` cannot override a file type. A source whose extension does not match its type
        # gets a typed copy in the run directory. The design and its source identities stay
        # unchanged.
        extensions = {
            SourceType.Verilog: ".v",
            SourceType.Vhdl: ".vhd",
            SourceType.VerilogHeader: ".vh",
            SourceType.Ucf: ".ucf",
        }
        source_files = []
        include_dirs = []
        for source in self.sources_read():
            path = source.path
            inferred = SOURCE_SUFFIXES.get(path.suffix.lstrip("."))
            if inferred is not source.type:
                copied = self.run_directory.writable(
                    Path("typed_sources")
                    / self.design.source_artifact_name(source, extensions[source.type])
                )
                copied.parent.mkdir(parents=True, exist_ok=True)
                replacing_copy(path, copied)
                path = copied
            source_files.append(path)
            if source.type in (SourceType.Verilog, SourceType.VerilogHeader):
                if source.path.parent not in include_dirs:
                    include_dirs.append(source.path.parent)
        script_path = self.copy_from_template(
            "ise_synth.tcl", source_files=source_files, source_include_dirs=include_dirs
        )
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
            r"Minimum period:\s+(?P<minimum_period>\-?\d+(?:\.\d+)?)ns\s+\(Maximum Frequency: (?P<fmax>\-?\d+(?:\.\d+)?)MHz\)",
            r"Slack:\s+(?P<wns>\-?\d+(?:\.\d+)?)ns",
        )
        if "wns" in self.results:
            wns = try_convert_to_primitives(self.results["wns"])
            fail |= not isinstance(wns, (float, int))
        return not fail

    def check_results(self) -> bool:
        """Reject the run if the slack that the synthesis report shows is negative."""
        ok = super().check_results()
        wns = try_convert_to_primitives(self.results.get("wns"))
        if isinstance(wns, (float, int)) and not isinstance(wns, bool) and wns < 0:
            self.reject(f"timing not met: negative slack (WNS {wns} ns)")
            return False
        return ok
