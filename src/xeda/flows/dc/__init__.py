import logging
import os
import re
from pathlib import Path
from turtle import st
from typing import Any, Dict, List, Mapping, Optional, Union

from box import Box

from ...dataclass import Field, validator
from ...tool import Tool
from ...platforms import AsicsPlatform
from ...utils import try_convert_to_primitives
from ...flow import AsicSynthFlow

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
    dc_shell = Tool(executable="dc_shell-xg-t")  # pyright: ignore

    class Settings(AsicSynthFlow.Settings):
        topographical_mode: bool = False
        sdc_files: List[Union[str, Path]] = Field(
            [], description="List of user SDC constraint files."
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
        target_libraries: List[Path] = Field(
            alias="libraries", description="Target library or libraries"
        )
        extra_link_libraries: List[Path] = Field([], description="Additional link libraries")
        tluplus_map: Optional[str] = None
        tluplus_max: Optional[str] = None
        tluplus_min: Optional[str] = None
        mw_ref_lib: Optional[str] = None
        mw_tf: Optional[str] = None
        alib_dir: Optional[str] = None
        additional_search_path: Optional[str] = None

        @validator("target_libraries", pre=True, always=True)
        def _validate_target_libraries(cls, value):
            if isinstance(value, (Path, str)):
                value = [value]
            return value

        @validator("platform", pre=True, always=True)
        def _validate_platform(cls, value):
            if isinstance(value, str) and not value.endswith(".toml"):
                return AsicsPlatform.from_resource(value)
            elif isinstance(value, (str, Path)):
                return AsicsPlatform.from_toml(value)
            return value

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        ss.hooks = {
            k: self.normalize_path_to_design_root(v) if v else None for k, v in ss.hooks.items()
        }

        if ss.platform:
            if not ss.target_libraries:
                ss.liberty = ss.platform.default_corner_settings.lib_files

        script_path = self.copy_from_template("run.tcl")
        cmd = [
            "-64bit",
        ]
        if self.settings.topographical_mode:
            cmd.append("-topographical_mode")
        cmd += [
            "-f",
            script_path,
        ]

        self.dc_shell.run(*cmd)

    def parse_reports(self) -> bool:
        reports_dir = self.settings.reports_dir

        top_name = self.design.rtl.top
        failed = False
        self.parse_report_regex(
            reports_dir / f"{top_name}.mapped.area.rpt",
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

        reportfile_path = reports_dir / f"{top_name}.mapped.qor.rpt"

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
                                self.results["wns"] = float(match.group("wns"))
                                self.results["tns"] = float(match.group("tns"))
                                self.results["num_violating_paths"] = int(match.group("nvp"))
                                if (
                                    self.results["wns"] > 0
                                    or self.results["tns"] > 0
                                    or self.results["num_violating_paths"] != 0
                                ):
                                    failed = True
                            else:
                                match = hold_wns_re.match(sec)
                                if match:
                                    self.results["hold_wns"] = float(match.group("wns"))
                                    self.results["hold_tns"] = float(match.group("tns"))
                                    self.results["hold_num_violating_paths"] = int(
                                        match.group("nvp")
                                    )
                                    if (
                                        self.results["hold_wns"] > 0
                                        or self.results["hold_tns"] > 0
                                        or self.results["hold_num_violating_paths"] != 0
                                    ):
                                        failed = True
            self.results["path_groups"] = path_groups

        return not failed
