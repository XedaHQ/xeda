import logging
import re
from pathlib import Path
from typing import List, Literal, Mapping, Optional, Union

from box import Box
import colorama

from ...dataclass import Field, validator
from ...flow import AsicSynthFlow
from ...platforms import AsicsPlatform
from ...tool import Tool
from ...utils import try_convert_to_primitives, try_convert

log = logging.getLogger(__name__)


def get_hier(dct, dotted_path, default=None):
    splitted = dotted_path.split(".")
    merged_leaves = {}
    for i, key in enumerate(splitted):
        try:
            for k, v in dct.items():
                if not isinstance(v, Mapping):
                    merged_leaves[k] = v
            dct = dct[key]
        except KeyError:
            print(f'Key {key} not found in {".".join(splitted[:i])}!')
            return default

    for k, v in dct.items():
        merged_leaves[k] = v
    return Box(merged_leaves)


class Dc(AsicSynthFlow):
    dc_shell = Tool(
        executable="dc_shell",
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
            r"^(====[=]+\()(.*)(\)[=]+====)$": colorama.Fore.BLUE
            + r"\g<1>"
            + colorama.Fore.CYAN
            + r"\g<2>"
            + colorama.Fore.BLUE
            + r"\g<3>",
        },
    )  # pyright: ignore

    class Settings(AsicSynthFlow.Settings):
        topographical_mode: bool = False
        sdc_files: List[Path] = Field([], description="List of user SDC constraint files.")
        optimization: Literal["area", "speed", "power", "none"] = Field(
            "area", description="Optimization goal for synthesis."
        )
        compile_command: str = Field(
            "compile_ultra",
            description="Synthesis command to run. Supported commands: 'compile', 'compile_ultra'",
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
        hooks: Mapping[str, Optional[Union[str, Path]]] = Field(
            {
                "pre_elab": None,
                "post_elab": None,
                "post_link": None,
                "finalize": None,
            },
            description="Custom TCL hooks to be run at the specified point in the flow.",
        )
        platform: Optional[AsicsPlatform] = None
        hdlin: Mapping[str, str] = Field(
            {
                "infer_mux": "default",
                "dont_infer_mux_for_resource_sharing": "true",
            },
            description="Set hdlin_<key> variables. See the DC documentation for details.",
        )
        compile: Mapping[str, str] = Field(
            {
                "seqmap_honor_sync_set_reset": "true",
                "optimize_unloaded_seq_logic_with_no_bound_opt": "true",  # allow unconnected registers to be removed
            },
            description="Set compile_<key> variables. See the DC documentation for details.",
        )
        target_libraries: List[Union[Path, str]] = Field(description="Target library or libraries")
        extra_link_libraries: List[Path] = Field([], description="Additional link libraries")
        tluplus_map: Optional[str] = None
        tluplus_max: Optional[str] = None
        tluplus_min: Optional[str] = None
        mw_ref_lib: Optional[str] = None
        mw_tf: Optional[str] = None
        alib_dir: Optional[str] = None
        additional_search_path: Optional[str] = None
        default_max_input_delay: Optional[float] = Field(
            0.0, description="Default max delay to set on all non-clock input ports"
        )
        default_max_output_delay: Optional[float] = Field(
            0.0, description="Default max delay to set on all output ports"
        )
        clean: bool = Field(
            True,
            description="Delete all the existing files in run_dir before running synthesis.",
        )
        sdf_version: Optional[str] = Field(
            "1.0",
            description="SDF version to use for the SDF output. If not set (=None), the default version (2.1) will be used.",
        )
        sdf_inst_name: Optional[str] = Field(
            None,
            description="Instance name to use for the SDF output.",
        )

        @validator("platform", pre=True, always=True)
        def _validate_platform(cls, value):
            if isinstance(value, str) and not value.endswith(".toml"):
                return AsicsPlatform.from_resource(value)
            elif isinstance(value, (str, Path)):
                return AsicsPlatform.from_toml(value)
            return value

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
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

    def clean(self):
        # completely erase the content of the run directory
        self.purge_run_path()

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        # if ss.platform:
        #     if not ss.target_libraries:
        #         ss.liberty = ss.platform.default_corner_settings.lib_files

        # prepend the generated constraints.sdc file to the list of user SDC files
        # if you don't want to use the generated clock and I/O constraints, simply don't set
        #   any PhysicalClock settings (clock.freq, etc) in the flow settings
        ss.sdc_files.insert(0, self.copy_from_template("constraints.sdc"))
        script_path = self.copy_from_template("dc_script.tcl")
        cmd = [
            "-64bit",
        ]
        if self.settings.topographical_mode:
            cmd.append("-topographical_mode")
        if self.settings.gui:
            cmd.append("-gui")
        cmd += [
            "-f",
            str(script_path),
        ]

        self.dc_shell.run(*cmd)

    def parse_timing_reports(self) -> bool:
        failed = False
        slack_pattern = re.compile(
            r"^\s*slack\s*\(\s*(?P<status>\w+)\s*\)\s+(?P<slack>[\+-]?\d+\.\d+)\s*$"
        )
        clock_pattern = re.compile(
            r"^\s*clock\s*(?P<clock_name>\w+)\s+\(\w+ edge\)\s+(?P<clock_time>\d+\.\d+)\s+(?P<clock_period>\d+\.\d+)\s*$"
        )
        max_report_path = self.settings.reports_dir / "mapped.timing.max.rpt"
        if max_report_path.exists():
            with open(max_report_path) as f:
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
                "Timing report %s was not found. Please check the synthesis run for errors.",
                max_report_path,
            )
        min_report_path = self.settings.reports_dir / "mapped.timing.min.rpt"
        if min_report_path.exists():
            with open(min_report_path) as f:
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
                "Timing report %s was not found. Please check the synthesis run for errors.",
                min_report_path,
            )
        if "clock_period" in self.results and "setup_wns" in self.results:
            clock_period = self.results["clock_period"]
            assert isinstance(clock_period, float)
            wns = self.results["setup_wns"]
            if isinstance(wns, float):
                self.results["Fmax"] = 1000.0 / (clock_period - wns)

        return not failed

    def parse_reports(self) -> bool:
        reports_dir = self.settings.reports_dir
        failed = not self.parse_timing_reports()

        self.parse_report_regex(
            reports_dir / f"mapped.area.rpt",
            r"Number of ports:\s*(?P<num_ports>\d+)",
            r"Number of nets:\s*(?P<num_nets>\d+)",
            r"Number of cells:\s*(?P<num_cells>\d+)",
            r"Number of combinational cells:\s*(?P<num_cells_combinational>\d+)",
            r"Number of sequential cells:\s*(?P<num_cells_sequentual>\d+)",
            r"Number of macros/black boxes:\s*(?P<num_macro_bbox>\d+)",
            r"Number of buf/inv:\s*(?P<num_buf_inv>\d+)",
            r"Number of references:\s*(?P<num_refs>\d+)",
            r"Combinational area:\s*(?P<area_combinational>\d+(?:\.\d+)?)",
            r"Buf/Inv area:\s*(?P<area_buf_inv>\d+(?:\.\d+)?)",
            r"Noncombinational area:\s*(?P<area_noncombinational>\d+(?:\.\d+)?)",
            r"Macro/Black Box area:\s*(?P<area_macro_bbox>\d+(?:\.\d+)?)",
            r"Net Interconnect area:\s*(?P<area_interconnect>\S+.*$)",
            r"Total cell area:\s*(?P<area_cell_total>\d+(?:\.\d+)?)",
            r"Total area:\s*(?P<area_macro_bbox>\w+)",
            r"Core Area:\s*(?P<area_core>\d+(?:\.\d+)?)",
            r"Aspect Ratio:\s*(?P<aspect_ratio>\d+(?:\.\d+)?)",
            r"Utilization Ratio:\s*(?P<utilization_ratio>\d+(?:\.\d+)?)",
            dotall=False,
        )

        reportfile_path = reports_dir / f"mapped.qor.rpt"

        def parse_kvs(kvs):
            kvs = re.split(r"\s*\n\s*", kvs)
            kvs = [re.split(r"\s*:\s*", s.strip()) for s in kvs if s.strip()]
            return {s[0].strip(): try_convert_to_primitives(s[1]) for s in kvs}

        path_group_re = re.compile(
            r"^\s*Timing Path Group\s+'(?P<path_group_name>\w+)'\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)",
            re.MULTILINE,
        )

        area_re = re.compile(
            r"^\s*Area\s*\n\s*\-+\s*\n(?P<kv1>(?:^.*\n)+)\s*\-+\s*\n(?P<kv2>(?:^.*\n)+)",
            re.MULTILINE,
        )
        drc_re = re.compile(r"^\s*Design Rules\s*\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)", re.MULTILINE)
        wns_re = re.compile(
            r"^\s*Design\s+WNS:\s+(?P<wns>\d+\.\d+)\s+TNS:\s+(?P<tns>\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )
        hold_wns_re = re.compile(
            r"^\s*Design\s+\(Hold\)\s+WNS:\s+(?P<wns>\d+\.\d+)\s+TNS:\s+(?P<tns>\d+\.\d+)\s+Number of Violating Paths:\s*(?P<nvp>\d+)"
        )

        # placeholder for ordering
        self.results["path_groups"] = None

        with open(reportfile_path) as rpt_file:
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
                            drc = parse_kvs(match.group("kv"))
                            self.results["drc"] = drc
                            if drc["Nets With Violations"] != 0:
                                print(f"Nets With DRC Violations: {drc['Nets With Violations']}")
                        else:
                            match = wns_re.match(sec)
                            if match:
                                self.results["_wns"] = float(match.group("wns"))
                                self.results["_tns"] = float(match.group("tns"))
                                self.results["num_violating_paths"] = int(match.group("nvp"))
                                if self.results["num_violating_paths"] != 0:
                                    failed = True
                            else:
                                match = hold_wns_re.match(sec)
                                if match:
                                    self.results["_hold_wns"] = float(match.group("wns"))
                                    self.results["_hold_tns"] = float(match.group("tns"))
                                    self.results["hold_num_violating_paths"] = int(
                                        match.group("nvp")
                                    )
                                    if self.results["hold_num_violating_paths"] != 0:
                                        failed = True
            self.results["path_groups"] = path_groups

        return not failed
