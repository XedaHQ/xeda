import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from box import Box

from ...tool import Tool
from ...utils import dict_merge, toml_load, try_convert
from ..flow import AsicSynthFlow

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
        # if not isinstance(v, Mapping):
        merged_leaves[k] = v
    return Box(merged_leaves)


class Dc(AsicSynthFlow):
    dc_shell = Tool(executable="dc_shell-xg-t")  # type: ignore

    class Settings(AsicSynthFlow.Settings):
        adk: str

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        adk_id = ss.adk
        adk_root = Path.home() / "adk"
        adk_config: Dict[str, Any] = {}
        for toml_file in adk_root.glob("*.toml"):
            adk_config = dict_merge(adk_config, toml_load(toml_file), add_new_keys=True)

        adk = get_hier(adk_config, adk_id)
        assert adk is not None
        if not os.path.isabs(adk.path):
            adk.path = os.path.join(str(adk_root), adk.path)

        log.debug("adk=%s", adk)

        script_path = self.copy_from_template(
            "run.tcl",
            adk=adk,
        )
        self.dc_shell.run("-64bit", "-topographical_mode", "-f", script_path)

    def init(self) -> None:
        self.reports_dir.mkdir(exist_ok=True)

    def parse_reports(self) -> bool:
        reports_dir = self.reports_dir

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
            return {s[0].strip(): try_convert(s[1]) for s in kvs}

        path_group_re = re.compile(
            r"^\s*Timing Path Group\s+'(?P<path_group_name>\w+)'\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)",
            re.MULTILINE,
        )

        area_re = re.compile(
            r"^\s*Area\s*\n\s*\-+\s*\n(?P<kv1>(?:^.*\n)+)\s*\-+\s*\n(?P<kv2>(?:^.*\n)+)",
            re.MULTILINE,
        )
        drc_re = re.compile(
            r"^\s*Design Rules\s*\n\s*\-+\s*\n(?P<kv>(?:^.*\n)+)", re.MULTILINE
        )
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
                                print(
                                    f"Nets With DRC Violations: {drc['Nets With Violations']}"
                                )
                        else:
                            match = wns_re.match(sec)
                            if match:
                                self.results["wns"] = float(match.group("wns"))
                                self.results["tns"] = float(match.group("tns"))
                                self.results["num_violating_paths"] = int(
                                    match.group("nvp")
                                )
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
