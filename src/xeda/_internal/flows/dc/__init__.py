import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Dict, List, Literal, Optional, Tuple, Union

import colorama
from box import Box

from ...dataclass import WORKING, Field, field_validator
from ...design import SourceType, language_of
from ...flow import (
    AsicSynthFlow,
    Flow,
    FlowSettingsError,
    MetricSpec,
    NoReplacement,
    describe_results,
    ArtifactSpec,
    describe_artifacts,
)
from ...liberty_units import (
    from_library_time,
    liberty_time_unit,
    liberty_time_unit_label,
    to_library_time,
)
from ...tool import Tool
from ...units import Time, Unit, count
from ...utils import try_convert_to_primitives

log = logging.getLogger(__name__)

#: The points of the flow a `hooks` script can be sourced at, in the order the flow reaches them.
HOOK_STAGES = ("pre_elab", "post_elab", "post_link", "finalize")

#: The figures of a timing path group in the QoR report that are times, in the time unit of the
#: library. Xeda converts them to ns; the other figures are counts.
PATH_GROUP_TIMES = frozenset(
    {
        "Critical Path Length",
        "Critical Path Slack",
        "Critical Path Clk Period",
        "Critical Path Clk Skew",
        "Total Negative Slack",
        "Worst Hold Violation",
        "Total Hold Violation",
    }
)

#: The groups of the area report patterns (`parse_reports`) and the metric of each.
LIBRARY_AREA_GROUPS = {
    "_library_area_combinational": "library_area.combinational",
    "_library_area_sequential": "library_area.sequential",
    "_library_area_macros_blackboxes": "library_area.macros_blackboxes",
    "_library_area_cells": "library_area.cells",
    "_library_area_with_nets": "library_area.with_nets",
    "_library_area_nets": "library_area.nets",
}

#: The line of `report_units` (`mapped.units.rpt`) that states the time unit of the main library:
#: `Time_unit : 1e-09 Second`. The pattern is narrow on purpose. A report that has no such line
#: gives no check.
TIME_UNIT_LINE = re.compile(
    r"^\s*Time_unit\s*:\s*(?P<seconds>[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s+Second\s*$",
    re.MULTILINE,
)


def get_hier(dct, dotted_path, default=None):
    splitted = dotted_path.split(".")
    merged_leaves = {}
    for i, key in enumerate(splitted):
        try:
            for k, v in dct.items():
                if not isinstance(v, (Mapping, dict)):
                    merged_leaves[k] = v
            dct = dct[key]
        except KeyError:
            log.debug(f'Key {key} not found in {".".join(splitted[:i])}!')
            return default

    for k, v in dct.items():
        merged_leaves[k] = v
    return Box(merged_leaves)


class Dc(AsicSynthFlow):
    """Synthesize a design for an ASIC with Synopsys Design Compiler."""

    experimental = True
    executables = ("dc_shell",)
    locator_variables = ("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE")

    artifacts_description = describe_artifacts(
        "log",
        netlist=ArtifactSpec("The mapped netlist as Verilog.", "when the design has a top module"),
        netlist_vhdl=ArtifactSpec(
            "The mapped netlist as VHDL.", "when the design has a top module"
        ),
        checkpoint=ArtifactSpec(
            "The mapped design as a Design Compiler database (`.ddc`).",
            "when the design has a top module",
        ),
        sdf=ArtifactSpec(
            "Timing of the mapped design as an SDF file.", "when the design has a top module"
        ),
        sdc=ArtifactSpec(
            "Constraints of the mapped design as an SDC file.", "when the design has a top module"
        ),
    )

    def main_executable(self) -> str:
        """Return the program of Design Compiler: the setting `dc_shell_name`."""
        assert isinstance(self.settings, self.Settings)
        return self.settings.dc_shell_name

    required_settings = {
        "target_libraries": "one or more target libraries (liberty `.db` files): "
        "`-s {flow}.target_libraries=<file.db>`; in the design file, as "
        "`flows.{flow}.target_libraries`"
    }

    results_description = describe_results(
        "setup_violating_paths",
        "hold_violating_paths",
        "library_area.cells",
        "library_area.combinational",
        "library_area.sequential",
        "library_area.macros_blackboxes",
        "library_area.with_nets",
        "library_area.nets",
        fmax=MetricSpec(
            "Maximum clock frequency, `1000 / (clock_period - wns)`. The maximum delay "
            "report must name one clock. With more than one clock, there is no `fmax`.",
            "number",
            "MHz",
        ),
        clock_name=MetricSpec(
            "Name of the one clock of the maximum delay report. With more than one clock, there "
            "is no value.",
            "text",
        ),
        clock_period=MetricSpec(
            "Period of the one clock of the maximum delay report, read from the line of its "
            "capture edge. With more than one clock, there is no value.",
            "number",
            "ns",
        ),
        wns=MetricSpec(
            "Smallest slack over all paths of the maximum delay report: the worst path of each "
            "path group, for all clocks. Negative means setup timing is not met.",
            "number",
            "ns",
        ),
        whs=MetricSpec(
            "Smallest slack over all paths of the minimum delay report: the worst path of each "
            "path group, for all clocks. Negative means hold timing is not met.",
            "number",
            "ns",
        ),
        area_core=MetricSpec(
            "Area of the core region, as Design Compiler prints it. Its unit is not verified.",
            "number",
        ),
        aspect_ratio=MetricSpec(
            "Core height divided by core width, as Design Compiler prints it.", "number"
        ),
        utilization_ratio=MetricSpec(
            "Core area utilization, as Design Compiler prints it.", "number"
        ),
        electrical_violations=MetricSpec(
            "Non-zero design-rule violation counts from the QoR report, by rule. The table has no "
            "row for the total number of nets.",
            "counts",
        ),
        path_groups=MetricSpec(
            "Per-timing-path-group QoR figures, keyed by path group name. The times in them "
            "(`PATH_GROUP_TIMES`) are in ns.",
            "object",
            schema={"type": "object", "additionalProperties": {"type": "object"}},
        ),
        ports=MetricSpec("Number of ports in the mapped design.", "integer"),
        nets=MetricSpec("Number of nets in the mapped design.", "integer"),
        cells=MetricSpec("Total number of cells in the mapped design.", "integer"),
        combinational_cells=MetricSpec(
            "Number of combinational cells in the mapped design.", "integer"
        ),
        sequential_cells=MetricSpec("Number of sequential cells in the mapped design.", "integer"),
        macros_blackboxes=MetricSpec(
            "Number of macros and black boxes in the mapped design.", "integer"
        ),
    )

    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.Sdc,
            SourceType.Tcl,
        }
    )

    class Settings(AsicSynthFlow.Settings):
        #: The `compile_<key>` variables that `optimization` presets on DC-NXT, by goal.
        optimization_presets: ClassVar[Dict[str, Dict[str, str]]] = {
            "area": {"high_effort_area": "true", "optimize_netlist_area": "true"},
            "speed": {"timing_high_effort": "true"},
        }
        #: The `hdlin_<key>` variables that have a setting of their own: `(table, key)` -> the
        #: setting. `hdlin` refuses these keys, so a variable has one spelling.
        variable_settings: ClassVar[Dict[Tuple[str, str], str]] = {
            ("hdlin", "infer_multibit"): "infer_multibit",
            ("hdlin", "vhdl_preserve_case"): "vhdl_preserve_case",
        }

        removed_settings = {
            **AsicSynthFlow.Settings.removed_settings,
            "platform": "`target_libraries`. The flow never read a platform",
            **dict.fromkeys(
                (
                    "extra_link_libraries",
                    "mw_ref_lib",
                    "mw_tf",
                    "alib_dir",
                    "additional_search_path",
                ),
                NoReplacement(
                    "Xeda passed it to nothing (set it in a TCL script that `hooks` names)"
                ),
            ),
        }
        log_file: Optional[Path] = Field(
            Path("dc.log"),
            description="Path of the log file. When not set, the log goes only to stdout.",
            json_schema_extra=WORKING,
        )
        dc_shell_name: str = Field(
            "dc_shell",
            description="DC shell executable to run.",
        )
        no_init: bool = Field(
            True,
            description="Do not load the home and local `.synopsys` initialization files.",
        )
        sdc_files: List[Path] = Field([], description="List of user SDC constraint files.")
        library_time_unit: Annotated[Time, Unit("ns")] = Field(
            Time("1 ns"),
            description="The time unit of the libraries in `target_libraries`: 1, 10 or 100 of "
            "fs, ps, ns, us, ms or s, such as `1 ps`. Design Compiler reads the numbers of an "
            "SDC file and writes the times of its reports in this unit. Xeda writes the clocks "
            "and delays of the generated constraints in it, and converts the times that it reads "
            "from the reports to ns. Your own SDC files (`sdc_files`) must use it too. A `.db` "
            "library does not tell Xeda its unit, so set this for a library that is not in ns "
            "(ASAP7 has `1 ps`). After the run, Xeda rejects the results if `report_units` "
            "shows another unit.",
        )
        optimization: Literal["area", "speed", "power", "none"] = Field(
            "area",
            description="Optimization goal for synthesis. On DC-NXT, `area` and `speed` also set "
            "preset `compile_<key>` variables. A key in `compile` overrides the preset.",
        )
        compile_command: str = Field(
            "compile_ultra",
            description="Synthesis command to run: `compile` or `compile_ultra`.",
        )
        compile_args: List[str] = Field(
            [],
            description="Additional arguments to pass to the compile command.",
        )
        flatten: bool = Field(
            False,
            description="Flatten the design hierarchy before synthesis.",
        )
        gui: bool = Field(
            False,
            description="Run the synthesis tool in GUI mode.",
        )
        hooks: Dict[str, Optional[Union[str, Path]]] = Field(
            {stage: None for stage in HOOK_STAGES},
            description="TCL scripts to source at points of the flow, by stage: `pre_elab` "
            "(before elaboration), `post_elab` (after it, before linking), `post_link` (after "
            "linking, before the constraints are read) and `finalize` (after the netlist is "
            "written).",
        )

        def optimization_variables(self) -> Dict[str, str]:
            """The `compile_<key>` variables that `optimization` sets: its preset, less the
            keys that `compile` holds, which override the preset."""
            preset = self.optimization_presets.get(self.optimization, {})
            return {key: value for key, value in preset.items() if key not in self.compile}

        @field_validator("hooks")
        @classmethod
        def _hooks_at_known_stages(cls, value):
            """Reject Design Compiler hooks at unsupported stages."""
            unknown = sorted(set(value) - set(HOOK_STAGES))
            if unknown:
                raise ValueError(
                    f"no hook stage {', '.join(map(repr, unknown))}: the stages are "
                    + ", ".join(HOOK_STAGES)
                )
            return value

        hdlin: Dict[str, str] = Field(
            {
                "infer_mux": "default",
                "dont_infer_mux_for_resource_sharing": "true",
            },
            description="Values of the `hdlin_<key>` variables, by key. See the DC documentation. "
            "`infer_multibit` and `vhdl_preserve_case` have settings of their own and are "
            "refused here.",
        )
        infer_multibit: Optional[str] = Field(
            "default_all",
            description="Value of DC's `hdlin_infer_multibit` variable, controlling when register "
            "banks are inferred as multibit cells.",
        )
        vhdl_preserve_case: bool = Field(
            False,
            description="Preserve the case of VHDL identifiers instead of lower-casing them, which "
            "matters when mixing VHDL with case-sensitive Verilog.",
        )
        compile: Dict[str, str] = Field(
            {},
            description="Values of the `compile_<key>` variables, by key, set before the "
            "compile command runs. A key here overrides the preset of `optimization`. See the DC "
            "documentation.",
        )
        target_libraries: List[Path] = Field(
            [],
            description="Target library or libraries (liberty `.db` files). Required: the flow "
            "refuses to launch without one.",
        )
        default_max_input_delay: Optional[Annotated[Time, Unit("ns")]] = Field(
            Time("0 ns"),
            description="Default maximum delay of all non-clock input ports, a time with a "
            "unit, such as `1.5 ns`.",
        )
        default_max_output_delay: Optional[Annotated[Time, Unit("ns")]] = Field(
            Time("0 ns"),
            description="Default maximum delay of all output ports, a time with a unit, such as "
            "`1.5 ns`.",
        )
        sdf_version: Optional[str] = Field(
            None,
            description="SDF version of the SDF output. When not set, the default version (2.1) "
            "is used.",
        )
        sdf_inst_name: Optional[str] = Field(
            None,
            description="Instance name to use for the SDF output.",
        )
        # Settings for topographical mode.
        topographical_mode: bool = Field(
            False,
            description="Run synthesis in topographical mode (physical-aware synthesis). Design "
            "Compiler needs `max_tluplus` and `tluplus_map` for it.",
        )
        min_routing_layer: Optional[int] = Field(
            None,
            description="Minimum routing layer to use for topographical mode.",
            json_schema_extra=count(),
        )
        max_routing_layer: Optional[int] = Field(
            None,
            description="Maximum routing layer to use for topographical mode.",
            json_schema_extra=count(),
        )
        max_tluplus: Optional[Path] = Field(
            None,
            description="Path of the maximum TLUplus file for topographical mode. Required when "
            "`topographical_mode` is true.",
        )
        min_tluplus: Optional[Path] = Field(
            None,
            description="Path of the minimum TLUplus file for topographical mode. Optional.",
        )
        tluplus_map: Optional[Path] = Field(
            None,
            description="Path of the TLUplus (tech2itf) map file for topographical mode. Required "
            "when `topographical_mode` is true.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a variable in `hdlin` or `compile` that has a setting of its own, and a
        `library_time_unit` that no Liberty library has."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        errors: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = [
            (
                f"{table}.{key}",
                f"`{table}.{key}` is the setting `{setting}`: set `{setting}` instead",
                None,
                "value_error",
            )
            for (table, key), setting in cls.Settings.variable_settings.items()
            if key in getattr(settings, table)
        ]
        try:
            liberty_time_unit(settings.library_time_unit)
        except ValueError as error:
            errors.append(("library_time_unit", str(error), None, "value_error"))
        if errors:
            raise FlowSettingsError(errors, cls.Settings)

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.check_settings_supported(ss)

        ss.hooks = {stage: p for stage, p in ss.hooks.items() if p is not None}
        if ss.infer_multibit:
            ss.hdlin["infer_multibit"] = ss.infer_multibit
        if "enable_hier_map" not in ss.hdlin:
            ss.hdlin["enable_hier_map"] = "true"
        if "enable_upf_compatible_naming" not in ss.hdlin:
            ss.hdlin["enable_upf_compatible_naming"] = "true"  # improve the SAIF annotation
        ss.hdlin["vhdl_preserve_case"] = "true" if ss.vhdl_preserve_case else "false"

        if "vhdl_std" not in ss.hdlin:
            if language_of(self.design).vhdl.standard in ("19", "2019"):
                ss.hdlin["vhdl_std"] = "2019"
            elif language_of(self.design).vhdl.standard in ("02", "2002", "08", "2008"):
                ss.hdlin["vhdl_std"] = "2008"
            elif language_of(self.design).vhdl.standard in ("93", "1993"):
                ss.hdlin["vhdl_std"] = "1993"

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # Put the generated `constraints.sdc` before the user SDC files. To skip the generated
        # clock and I/O constraints, set no clock (`clock.period`, `clock.freq`, ...) in the
        # flow settings.
        ss.sdc_files.insert(
            0,
            self.copy_from_template(
                "constraints.sdc",
                lib_time=lambda time: to_library_time(time, ss.library_time_unit),
            ),
        )
        script_path = self.copy_from_template("dc_script.tcl")
        # `dc_script.tcl` writes these files in every run. It needs a top module and stops
        # without one.
        top = self.design.rtl.top
        if top:
            self.artifacts.netlist = ss.outputs_dir / f"{top}.mapped.v"
            self.artifacts.netlist_vhdl = ss.outputs_dir / f"{top}.mapped.vhd"
            self.artifacts.checkpoint = ss.outputs_dir / f"{top}.mapped.ddc"
            self.artifacts.sdf = ss.outputs_dir / f"{top}.mapped.sdf"
            self.artifacts.sdc = ss.outputs_dir / f"{top}.mapped.sdc"
        cmd = [
            "-64bit",
        ]
        if self.settings.topographical_mode:
            cmd.append("-topographical_mode")
        if self.settings.log_file:
            cmd += ["-output_log_file", str(self.settings.log_file)]
        if self.settings.gui:
            cmd.append("-gui")
        cmd += [
            "-f",
            str(script_path),
        ]
        dc_shell = Tool(
            executable=ss.dc_shell_name,
            flow=self,
            version_flag="-version",
            highlight_rules={
                r"^(Error:)(.+)$": colorama.Fore.RED + colorama.Style.BRIGHT + r"\g<0>",
                r"^(\[ERROR\])(.+)$": colorama.Fore.RED + colorama.Style.BRIGHT + r"\g<0>",
                r"^(Warning:)(.+)$": colorama.Fore.YELLOW
                + colorama.Style.BRIGHT
                + r"\g<1>"
                + colorama.Style.NORMAL
                + r"\g<2>",
                r"^(Information:)(.+)$": colorama.Fore.GREEN
                + colorama.Style.BRIGHT
                + r"\g<1>"
                + colorama.Style.NORMAL
                + r"\g<2>",
                r"^(====[=]+\()\s*(.*)\s*(\)[=]+====)$": colorama.Fore.BLUE
                + colorama.Style.BRIGHT
                + "Xeda: "
                + colorama.Style.NORMAL
                + r"\g<2>",
            },
        )  # pyright: ignore

        if ss.no_init:
            cmd.append("-no_home_init")
            cmd.append("-no_local_init")

        dc_shell.run(*cmd)

    def parse_timing_reports(self) -> bool:
        """Read the setup and hold slack and the clock period from the timing reports. The
        reports are in the time unit of the library (`library_time_unit`), and the results are in
        ns.

        A report lists the worst path of each path group. The worst slack is the smallest of
        them. The clock period and `fmax` need one clock: with more than one clock in the maximum
        delay report, they are not written."""
        assert isinstance(self.settings, self.Settings)
        unit = self.settings.library_time_unit

        def ns(text: str) -> float:
            return from_library_time(float(text), unit)

        failed = False
        slack_pattern = re.compile(
            r"^\s*slack\s*\(\s*(?P<status>\w+)\s*\)\s+(?P<slack>[\+-]?\d+\.\d+)\s*$"
        )
        clock_pattern = re.compile(
            r"^\s*clock\s+(?P<clock_name>\S+)\s+\((?:rise|fall) edge\)\s+(?P<clock_time>\d+\.\d+)\s+(?P<clock_period>\d+\.\d+)\s*$"
        )
        # Every clock edge line, also one that `clock_pattern` cannot read: a report with such a
        # line gives no clock and no fmax.
        any_clock_line = re.compile(r"^\s*clock\s+(?P<clock_name>.+?)\s+\((?:rise|fall) edge\)")

        def worst_slack(lines: List[str], kind: str) -> Optional[Tuple[float, str]]:
            """The smallest slack of the report and the status of its path."""
            nonlocal failed
            worst: Optional[Tuple[float, str]] = None
            for line in lines:
                matches = slack_pattern.search(line)
                if not matches:
                    continue
                slack = ns(matches.group("slack"))
                status = matches.group("status")
                if slack < 0 or status != "MET":
                    log.error("%s time violation: slack = %s, status = %s", kind, slack, status)
                    failed = True
                if worst is None or slack < worst[0]:
                    worst = (slack, status)
            return worst

        max_report_path = self.settings.reports_dir / "mapped.timing.max.rpt"
        # only a report that this run wrote
        max_report = self.report_file(max_report_path, optional=True)
        if max_report is not None:
            lines = max_report.read_text(errors="replace").splitlines()
            setup = worst_slack(lines, "Setup")
            if setup is not None:
                self.results["wns"], self.results["_setup_status"] = setup
            # the first clock line with a time is the capture edge: the launch edge is at 0
            edges = [m for m in map(clock_pattern.search, lines) if m]
            clock_lines = [m for m in map(any_clock_line.search, lines) if m]
            clocks = {m.group("clock_name") for m in clock_lines}
            captures = [m for m in edges if float(m.group("clock_period"))]
            if len(clocks) == 1 and len(edges) == len(clock_lines) and captures:
                self.results["clock_name"] = captures[0].group("clock_name")
                self.results["_clock_time"] = ns(captures[0].group("clock_time"))
                self.results["clock_period"] = ns(captures[0].group("clock_period"))
            elif len(clocks) > 1 or len(edges) != len(clock_lines):
                log.debug(
                    "The maximum delay report has the clocks %s, or a clock line that Xeda cannot "
                    "read: no clock_name, clock_period or fmax. The setup slack is the worst of "
                    "all paths.",
                    ", ".join(sorted(clocks)),
                )
        else:
            log.warning(
                "Timing report %s was not found. Check the synthesis run for errors.",
                max_report_path,
            )
        min_report_path = self.settings.reports_dir / "mapped.timing.min.rpt"
        # only a report that this run wrote
        min_report = self.report_file(min_report_path, optional=True)
        if min_report is not None:
            hold = worst_slack(min_report.read_text(errors="replace").splitlines(), "Hold")
            if hold is not None:
                self.results["whs"], self.results["_hold_status"] = hold
        else:
            log.warning(
                "Timing report %s was not found. Check the synthesis run for errors.",
                min_report_path,
            )
        if "clock_period" in self.results and "wns" in self.results:
            clock_period = self.results["clock_period"]
            assert isinstance(clock_period, float)
            wns = self.results["wns"]
            if isinstance(wns, float) and (clock_period - wns) > 0:
                self.results["fmax"] = 1000.0 / (clock_period - wns)

        return not failed

    def check_results(self) -> bool:
        """Reject the run if `report_units` shows a time unit of the main library that is not
        `library_time_unit`. Every number that Xeda wrote or read would be off by the ratio of the
        two units. A report without a time unit line gives no check."""
        ok = super().check_results()
        assert isinstance(self.settings, self.Settings)
        report = self.report_file(self.settings.reports_dir / "mapped.units.rpt", optional=True)
        if report is None:
            return ok
        found = TIME_UNIT_LINE.search(report.read_text(errors="replace"))
        if found is None:
            return ok
        reported = Time.of(float(found["seconds"]), "s")
        wanted = self.settings.library_time_unit
        if reported.to("s") > 0 and math.isclose(reported.to("s"), wanted.to("s"), rel_tol=1e-6):
            return ok
        self.reject(
            f"the time unit of the main library is {liberty_time_unit_label(reported)} "
            f"(`Time_unit : {found['seconds']} Second` in {report.name}), but `library_time_unit` "
            f"is {liberty_time_unit_label(wanted)}: set `library_time_unit` to the time unit of "
            "the libraries in `target_libraries`"
        )
        return False

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        reports_dir = self.settings.reports_dir
        unit = self.settings.library_time_unit
        failed = not self.parse_timing_reports()

        self.parse_report_regex(
            reports_dir / "mapped.area.rpt",
            r"Number of ports:\s*(?P<ports>\d+)",
            r"Number of nets:\s*(?P<nets>\d+)",
            r"Number of cells:\s*(?P<cells>\d+)",
            r"Number of combinational cells:\s*(?P<combinational_cells>\d+)",
            r"Number of sequential cells:\s*(?P<sequential_cells>\d+)",
            r"Number of macros/black boxes:\s*(?P<macros_blackboxes>\d+)",
            r"Number of buf/inv:\s*(?P<_num_buf_inv>\d+)",
            r"Number of references:\s*(?P<_num_refs>\d+)",
            r"Combinational area:\s*(?P<_library_area_combinational>\d+(?:\.\d+)?)",
            r"Buf/Inv area:\s*(?P<_area_buf_inv>\d+(?:\.\d+)?)",
            r"Noncombinational area:\s*(?P<_library_area_sequential>\d+(?:\.\d+)?)",
            r"Macro/Black Box area:\s*(?P<_library_area_macros_blackboxes>\d+(?:\.\d+)?)",
            r"Net Interconnect area:\s*(?P<_library_area_nets>\S+.*$)",
            r"Total cell area:\s*(?P<_library_area_cells>\d+(?:\.\d+)?)",
            r"Total area:\s*(?P<_library_area_with_nets>\d+(?:\.\d+)?)",
            r"Core Area:\s*(?P<area_core>\d+(?:\.\d+)?)",
            r"Aspect Ratio:\s*(?P<aspect_ratio>\d+(?:\.\d+)?)",
            r"Utilization Ratio:\s*(?P<utilization_ratio>\d+(?:\.\d+)?)",
            dotall=False,
            optional=True,
        )
        # The patterns name a group with a plain identifier. The results name the metric by its
        # path. "Net Interconnect area" is text when the library has no wire load model.
        for private, metric in LIBRARY_AREA_GROUPS.items():
            value = self.results.get(private)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.results[metric] = value

        reportfile_path = reports_dir / "mapped.qor.rpt"

        def parse_kvs(kvs, skip_zero=False):
            kvs = re.split(r"\s*\n\s*", kvs)
            kvs = [re.split(r"\s*:\s*", s.strip()) for s in kvs if s.strip()]
            ret = {s[0].strip(): try_convert_to_primitives(s[1]) for s in kvs}
            return {k: v for k, v in ret.items() if not skip_zero or v not in {0, None}}

        path_group_re = re.compile(
            r"^\s*Timing Path Group\s+'(?P<path_group_name>\w+)'\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)",
            re.MULTILINE,
        )

        drc_re = re.compile(r"^\s*Design Rules\s*\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)", re.MULTILINE)
        wns_re = re.compile(
            r"^\s*Design\s+WNS:\s+(?P<wns>[-+]?\d+\.\d+)\s+TNS:\s+(?P<tns>[-+]?\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )
        hold_wns_re = re.compile(
            r"^\s*Design\s+\(Hold\)\s+WNS:\s+(?P<wns>[-+]?\d+\.\d+)\s+TNS:\s+(?P<tns>[-+]?\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )

        qor_report = self.report_file(reportfile_path)  # only a report that this run wrote
        if qor_report is None:
            return False
        with open(qor_report) as rpt_file:
            content = rpt_file.read()
            sections = re.split(r"\n\s*\n", content)

            path_groups = {}
            for sec in sections:
                match = path_group_re.match(sec)
                if match:
                    group_name = match.group("path_group_name")
                    path_groups[group_name] = {
                        key: (
                            from_library_time(value, unit)
                            if key in PATH_GROUP_TIMES
                            and isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            else value
                        )
                        for key, value in parse_kvs(match.group("kv")).items()
                    }
                    continue
                match = drc_re.match(sec)
                if match:
                    drc = parse_kvs(match.group("kv"), skip_zero=True)
                    self.results["electrical_violations"] = {
                        name: count for name, count in drc.items() if name != "Total Number of Nets"
                    }
                    if drc.get("Nets With Violations"):
                        failed = True
                        log.error("Nets With DRC Violations: %s", drc["Nets With Violations"])
                    continue
                match = wns_re.match(sec)
                if match:
                    self.results["_drc_wns"] = from_library_time(float(match.group("wns")), unit)
                    self.results["_drc_tns"] = from_library_time(float(match.group("tns")), unit)
                    self.results["setup_violating_paths"] = int(match.group("nvp"))
                    if self.results["setup_violating_paths"] != 0:
                        failed = True
                    continue
                match = hold_wns_re.match(sec)
                if match:
                    self.results["_drc_hold_wns"] = from_library_time(
                        float(match.group("wns")), unit
                    )
                    self.results["_drc_hold_tns"] = from_library_time(
                        float(match.group("tns")), unit
                    )
                    self.results["hold_violating_paths"] = int(match.group("nvp"))
                    if self.results["hold_violating_paths"] != 0:
                        failed = True
            self.results["path_groups"] = path_groups

        return not failed
