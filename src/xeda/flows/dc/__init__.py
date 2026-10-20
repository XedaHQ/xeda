import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Dict, List, Literal, Optional, Tuple, Union

import colorama
from box import Box

from ...dataclass import WORKING, Field, field_validator
from ...design import SourceType
from ...flow import AsicSynthFlow, Flow, FlowSettingsError, NoReplacement, describe_results
from ...tool import Tool
from ...utils import try_convert, try_convert_to_primitives

log = logging.getLogger(__name__)

#: The points of the flow a `hooks` script can be sourced at, in the order the flow reaches them.
HOOK_STAGES = ("pre_elab", "post_elab", "post_link", "finalize")


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

    executables = ("dc_shell",)

    #: `num_cells_sequentual` is a misspelling. It is the name of the regex group that parses the
    #: area report, so `results.json` has this key. Scripts read it, so the key stays. The correct
    #: spelling is added next to it, as for the canonical aliases of every flow.
    results_canonical_aliases: Dict[str, Tuple[str, ...]] = {
        **AsicSynthFlow.results_canonical_aliases,
        "num_cells_sequential": ("num_cells_sequential", "num_cells_sequentual"),
    }

    required_settings = {
        "target_libraries": "one or more target libraries (liberty `.db` files): "
        "`-s target_libraries=<file.db>`; in the design file, as "
        "`flows.{flow}.target_libraries`"
    }

    results_description = describe_results(
        "Fmax",
        "clock_name",
        "clock_period",
        "setup_wns",
        "hold_wns",
        "num_violating_paths",
        "hold_num_violating_paths",
        "aspect_ratio",
        "utilization_ratio",
        "area_combinational",
        "area_noncombinational",
        "area_macro_bbox",
        "area_cell_total",
        "area_total",
        "area_core",
        **{
            "area": "Area breakdown from the QoR report, as a mapping of the report's own labels "
            "to values.",
            "drc": "Non-zero design-rule violation counts from the QoR report, by rule.",
            "path_groups": "Per-timing-path-group QoR figures, keyed by path group name.",
            "num_ports": "Number of ports in the mapped design.",
            "num_nets": "Number of nets in the mapped design.",
            "num_cells": "Total number of cells in the mapped design.",
            "num_cells_combinational": "Number of combinational cells in the mapped design.",
            "num_cells_sequential": "Number of sequential cells in the mapped design.",
            "num_cells_sequentual": "Deprecated misspelling of `num_cells_sequential`, still "
            "reported so existing scripts keep working.",
            "num_macro_bbox": "Number of macros / black boxes in the mapped design.",
        },
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
        default_max_input_delay: Optional[float] = Field(
            0.0, description="Default maximum delay of all non-clock input ports."
        )
        default_max_output_delay: Optional[float] = Field(
            0.0, description="Default maximum delay of all output ports."
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
        )
        max_routing_layer: Optional[int] = Field(
            None,
            description="Maximum routing layer to use for topographical mode.",
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
        """Refuse a variable in `hdlin` or `compile` that has a setting of its own."""
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
        if errors:
            raise FlowSettingsError(errors, cls.Settings)

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.check_settings_supported(ss)
        ss.sdc_files = self.resolve_paths_to_design_or_cwd(ss.sdc_files)

        ss.hooks = {
            stage: self.process_path(p, subs_vars=True, resolve_to=self.design.root_path)
            for stage, p in ss.hooks.items()
            if p is not None
        }
        ss.target_libraries = [
            self.process_path(p, subs_vars=True, resolve_to=self.design.root_path)
            for p in ss.target_libraries
        ]
        if ss.infer_multibit:
            ss.hdlin["infer_multibit"] = ss.infer_multibit
        if "enable_hier_map" not in ss.hdlin:
            ss.hdlin["enable_hier_map"] = "true"
        if "enable_upf_compatible_naming" not in ss.hdlin:
            ss.hdlin["enable_upf_compatible_naming"] = "true"  # improve the SAIF annotation
        ss.hdlin["vhdl_preserve_case"] = "true" if ss.vhdl_preserve_case else "false"

        if "vhdl_std" not in ss.hdlin:
            if self.design.language.vhdl.standard in ("19", "2019"):
                ss.hdlin["vhdl_std"] = "2019"
            elif self.design.language.vhdl.standard in ("02", "2002", "08", "2008"):
                ss.hdlin["vhdl_std"] = "2008"
            elif self.design.language.vhdl.standard in ("93", "1993"):
                ss.hdlin["vhdl_std"] = "1993"

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # Put the generated `constraints.sdc` before the user SDC files. To skip the generated
        # clock and I/O constraints, set no clock (`clock.period`, `clock.freq`, ...) in the
        # flow settings.
        ss.sdc_files.insert(0, self.copy_from_template("constraints.sdc"))
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
        failed = False
        slack_pattern = re.compile(
            r"^\s*slack\s*\(\s*(?P<status>\w+)\s*\)\s+(?P<slack>[\+-]?\d+\.\d+)\s*$"
        )
        clock_pattern = re.compile(
            r"^\s*clock\s*(?P<clock_name>\w+)\s+\(\w+ edge\)\s+(?P<clock_time>\d+\.\d+)\s+(?P<clock_period>\d+\.\d+)\s*$"
        )
        max_report_path = self.settings.reports_dir / "mapped.timing.max.rpt"
        max_report = self.report_file(max_report_path)  # only a report that this run wrote
        if max_report is not None:
            with open(max_report) as f:
                for line in f.readlines():
                    if "clock_period" not in self.results:
                        matches = clock_pattern.search(line)
                        if matches:
                            clock_period = try_convert(matches.group("clock_period"), float)
                            if clock_period:
                                clock_name = matches.group("clock_name")
                                clock_time = try_convert(matches.group("clock_time"), float)
                                self.results["clock_name"] = clock_name
                                self.results["_clock_time"] = clock_time
                                self.results["clock_period"] = clock_period
                                continue
                    matches = slack_pattern.search(line)
                    if matches:
                        wns = try_convert(matches.group("slack"), float)
                        status = matches.group("status")
                        self.results["setup_wns"] = wns
                        self.results["_setup_status"] = status
                        if not isinstance(wns, float) or wns < 0 or status != "MET":
                            log.error(
                                "Setup time violation: WNS = %s, status = %s",
                                wns,
                                status,
                            )
                            failed = True
                        break
        else:
            log.warning(
                "Timing report %s was not found. Check the synthesis run for errors.",
                max_report_path,
            )
        min_report_path = self.settings.reports_dir / "mapped.timing.min.rpt"
        min_report = self.report_file(min_report_path)  # only a report that this run wrote
        if min_report is not None:
            with open(min_report) as f:
                for line in f.readlines():
                    matches = slack_pattern.search(line)
                    if matches:
                        wns = float(matches.group("slack"))
                        status = matches.group("status")
                        self.results["hold_wns"] = wns
                        self.results["_hold_status"] = status
                        if wns < 0 or status != "MET":
                            log.error(
                                "Hold time violation: WNS = %s, status = %s",
                                wns,
                                status,
                            )
                            failed = True
                        break
        else:
            log.warning(
                "Timing report %s was not found. Check the synthesis run for errors.",
                min_report_path,
            )
        if "clock_period" in self.results and "setup_wns" in self.results:
            clock_period = self.results["clock_period"]
            assert isinstance(clock_period, float)
            wns = self.results["setup_wns"]
            if isinstance(wns, float) and (clock_period - wns) > 0:
                self.results["Fmax"] = 1000.0 / (clock_period - wns)

        return not failed

    def parse_reports(self) -> bool:
        reports_dir = self.settings.reports_dir
        failed = not self.parse_timing_reports()

        self.parse_report_regex(
            reports_dir / "mapped.area.rpt",
            r"Number of ports:\s*(?P<num_ports>\d+)",
            r"Number of nets:\s*(?P<num_nets>\d+)",
            r"Number of cells:\s*(?P<num_cells>\d+)",
            r"Number of combinational cells:\s*(?P<num_cells_combinational>\d+)",
            r"Number of sequential cells:\s*(?P<num_cells_sequentual>\d+)",
            r"Number of macros/black boxes:\s*(?P<num_macro_bbox>\d+)",
            r"Number of buf/inv:\s*(?P<_num_buf_inv>\d+)",
            r"Number of references:\s*(?P<_num_refs>\d+)",
            r"Combinational area:\s*(?P<area_combinational>\d+(?:\.\d+)?)",
            r"Buf/Inv area:\s*(?P<_area_buf_inv>\d+(?:\.\d+)?)",
            r"Noncombinational area:\s*(?P<area_noncombinational>\d+(?:\.\d+)?)",
            r"Macro/Black Box area:\s*(?P<area_macro_bbox>\d+(?:\.\d+)?)",
            r"Net Interconnect area:\s*(?P<_area_interconnect>\S+.*$)",
            r"Total cell area:\s*(?P<area_cell_total>\d+(?:\.\d+)?)",
            r"Total area:\s*(?P<area_total>\d+(?:\.\d+)?)",
            r"Core Area:\s*(?P<area_core>\d+(?:\.\d+)?)",
            r"Aspect Ratio:\s*(?P<aspect_ratio>\d+(?:\.\d+)?)",
            r"Utilization Ratio:\s*(?P<utilization_ratio>\d+(?:\.\d+)?)",
            dotall=False,
        )

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

        area_re = re.compile(
            r"^\s*Area\s*\n\s*\-+\s*\n(?P<kv1>(?:^.*\n)+)\s*\-+\s*\n\s*(?P<kv2>(?:^.*\n)+)\s*",
            re.MULTILINE,
        )
        drc_re = re.compile(r"^\s*Design Rules\s*\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)", re.MULTILINE)
        wns_re = re.compile(
            r"^\s*Design\s+WNS:\s+(?P<wns>\d+\.\d+)\s+TNS:\s+(?P<tns>\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )
        hold_wns_re = re.compile(
            r"^\s*Design\s+\(Hold\)\s+WNS:\s+(?P<wns>\d+\.\d+)\s+TNS:\s+(?P<tns>\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )

        # Reserve the key's place in the order of the results.
        self.results["path_groups"] = None

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
                    path_groups[group_name] = parse_kvs(match.group("kv"))

                else:
                    match = area_re.match(sec)
                    if match:
                        kv1 = parse_kvs(match.group("kv1"))
                        kv2 = parse_kvs(match.group("kv2"))
                        self.results["area"] = {**kv1, **kv2}
                    else:
                        match = drc_re.match(sec)
                        if match:
                            drc = parse_kvs(match.group("kv"), skip_zero=True)
                            self.results["drc"] = drc
                            if drc.get("Nets With Violations"):
                                failed = True
                                log.error(
                                    "Nets With DRC Violations: %s", drc["Nets With Violations"]
                                )
                        else:
                            match = wns_re.match(sec)
                            if match:
                                self.results["_drc_wns"] = float(match.group("wns"))
                                self.results["_drc_tns"] = float(match.group("tns"))
                                self.results["num_violating_paths"] = int(match.group("nvp"))
                                if self.results["num_violating_paths"] != 0:
                                    failed = True
                            else:
                                match = hold_wns_re.match(sec)
                                if match:
                                    self.results["_drc_hold_wns"] = float(match.group("wns"))
                                    self.results["_drc_hold_tns"] = float(match.group("tns"))
                                    self.results["hold_num_violating_paths"] = int(
                                        match.group("nvp")
                                    )
                                    if self.results["hold_num_violating_paths"] != 0:
                                        failed = True
            self.results["path_groups"] = path_groups

        return not failed
