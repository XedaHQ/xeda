import itertools
import json
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from ...dataclass import Field, XedaBaseModel, validator
from ...design import Design
from ...utils import HierDict, parse_xml, try_convert
from ...flow import FPGA, FpgaSynthFlow, SimFlow
from ..vivado import Vivado

log = logging.getLogger(__name__)


StepsValType = Union[None, List[str], Dict[str, Any]]


def vivado_synth_generics(parameters: dict) -> List[str]:
    generics = []
    for k, v in parameters.items():
        if isinstance(v, bool):
            v = f"1'b{int(v)}"
        elif isinstance(v, str) and not re.match(r"\d+'b[01]+", v):
            v = '\\"' + v + '\\"'
        generics.append(f"{k}={v}")
    return generics


class RunOptions(XedaBaseModel):
    strategy: Optional[str] = None
    steps: Dict[str, StepsValType] = {}


class VivadoProject(Vivado, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using a project-based flow"""

    class Settings(Vivado.Settings, FpgaSynthFlow.Settings, SimFlow.Settings):
        """Settings for Vivado synthesis in project mode"""

        fpga: Optional[FPGA] = None

        # FIXME implement and verify all
        fail_critical_warning: bool = Field(
            False,
            description="flow fails if any Critical Warnings are reported by Vivado",
        )  # pyright: ignore
        fail_timing: bool = Field(
            True, description="flow fails if timing is not met"
        )  # pyright: ignore
        input_delay: Optional[float] = None
        output_delay: Optional[float] = None
        write_checkpoint: bool = False
        write_netlist: bool = False
        write_bitstream: bool = False
        extra_reports: bool = False
        qor_suggestions: bool = False
        # See https://www.xilinx.com/content/dam/xilinx/support/documents/sw_manuals/xilinx2022_1/ug901-vivado-synthesis.pdf
        synth: RunOptions = RunOptions(
            # Performance strategies: "Flow_PerfOptimized_high" (no LUT combining, fanout limit: 400), "Flow_AlternateRoutability",
            strategy="",  # Empty for Vivado Default strategy
            steps={
                "SYNTH_DESIGN": {},
                "OPT_DESIGN": {},
                "POWER_OPT_DESIGN": {},
            },
        )
        # See https://www.xilinx.com/content/dam/xilinx/support/documents/sw_manuals/xilinx2022_1/ug904-vivado-implementation.pdf
        impl: RunOptions = RunOptions(
            # Performance strategies: "Performance_ExploreWithRemap", "Flow_RunPostRoutePhysOpt",
            #   "Flow_RunPhysOpt", "Performance_ExtraTimingOpt",
            strategy="",  # Empty for Vivado Default strategy
            steps={
                "PLACE_DESIGN": {},
                "POST_PLACE_POWER_OPT_DESIGN": {},
                "PHYS_OPT_DESIGN": {},
                "ROUTE_DESIGN": {},
                "WRITE_BITSTREAM": {},
            },
        )
        # See https://www.xilinx.com/content/dam/xilinx/support/documents/sw_manuals/xilinx2022_1/ug903-vivado-using-constraints.pdf
        xdc_files: List[Union[str, Path]] = Field([], description="List of XDC constraint files.")
        suppress_msgs: List[str] = [
            "Synth 8-7080",  # "Parallel synthesis criteria is not met"
            "Vivado 12-7122",  # Auto Incremental Compile:: No reference checkpoint was found in run
        ]
        dummy_io_delay: bool = False  # set a dummy IO delay if they are not specified
        flatten_hierarchy: Optional[Literal["full", "rebuilt", "none"]] = Field("rebuilt")

    def run(self):
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
        if (
            settings.main_clock
            and settings.main_clock.period
            and settings.dummy_io_delay
            and (settings.input_delay is None)
            and (settings.output_delay is None)
        ):
            dummy_delay = max(0.001, 0.001 * settings.main_clock.period)
            settings.input_delay = dummy_delay
            settings.output_delay = dummy_delay
        if settings.write_netlist:
            for o in [
                "timesim.min.sdf",
                "timesim.max.sdf",
                "timesim.v",
                "funcsim.vhdl",
                "xdc",
            ]:
                self.artifacts[o] = os.path.join(settings.outputs_dir, o)

        settings.synth.steps = {
            **{
                "SYNTH_DESIGN": {},
                "OPT_DESIGN": {},
                "POWER_OPT_DESIGN": {},
            },
            **settings.synth.steps,
        }
        settings.impl.steps = {
            **{
                "PLACE_DESIGN": {},
                "POST_PLACE_POWER_OPT_DESIGN": {},
                "PHYS_OPT_DESIGN": {},
                "ROUTE_DESIGN": {},
                "WRITE_BITSTREAM": {},
            },
            **settings.impl.steps,
        }

        clock_xdc_path = self.copy_from_template("clock.xdc")

        if settings.synth.steps["SYNTH_DESIGN"] is None:
            settings.synth.steps["SYNTH_DESIGN"] = {}
        assert isinstance(settings.synth.steps["SYNTH_DESIGN"], dict)
        if settings.flatten_hierarchy:
            settings.synth.steps["SYNTH_DESIGN"]["flatten_hierarchy"] = settings.flatten_hierarchy

        reports_tcl = self.copy_from_template("vivado_report_helper.tcl")

        xdc_files = [p.file for p in self.design.rtl.sources if p.type == "xdc"]
        xdc_files += [self.normalize_path_to_design_root(p) for p in settings.xdc_files]
        assert clock_xdc_path not in xdc_files, f"XDC file {xdc_files} was already included."
        xdc_files.append(clock_xdc_path)

        script_path = self.copy_from_template(
            "vivado_project.tcl",
            xdc_files=xdc_files,
            reports_tcl=reports_tcl,
            generics=" ".join(vivado_synth_generics(self.design.rtl.parameters)),
        )
        self.vivado.run("-source", script_path)

    def parse_reports(self) -> bool:
        return super().parse_reports()


def parse_hier_util(
    report: Union[Path, str],
    skip_zero_or_empty=True,
    skip_headers=None,
) -> Optional[HierDict]:
    """parse hierarchical utilization report"""
    table = parse_xml(
        report,
        tags_blacklist=["class", "style", "halign", "width"],
        skip_empty_children=True,
    )
    if table is None:
        return None

    tr = table["RptDoc"]["section"]["table"]["tablerow"]  # type: ignore
    hdr = tr[0]["tableheader"]  # type: ignore
    headers: List[str] = [h["@contents"] for h in hdr]  # type: ignore
    rows: List[HierDict] = tr[1:]  # type: ignore

    select_headers: Optional[List[str]] = None
    if skip_headers is None:
        skip_headers = ["Logic LUTs"]
    skip_headers.append("Instance")
    if select_headers is None:
        select_headers = [h for h in headers if h not in skip_headers]
    assert select_headers is not None

    def leading_ws(s: str) -> int:
        return sum(1 for _ in itertools.takewhile(str.isspace, s))

    def conv_val(v):
        for t in (int, float):
            try:
                return t(v)
            except ValueError:
                continue
        return v

    def add_hier(d, cur_ws=0, i: int = 0, parent=None) -> int:
        cur_dict = d
        cur_mod = parent
        while i < len(rows):
            row = rows[i]
            cells = row["tablecell"]
            assert cells
            lcontents = [cell["@contents"] for cell in cells]  # type: ignore
            inst_name = lcontents[0]
            assert inst_name
            inst_ws = leading_ws(inst_name)

            if inst_ws > cur_ws:
                x = "@children"
                if x not in cur_dict:
                    cur_dict[x] = OrderedDict()
                i = add_hier(cur_dict[x], inst_ws, i, cur_mod)
                continue

            if inst_ws < cur_ws:
                return i  # pop

            key_name = inst_name.strip()
            if key_name not in d:
                d[key_name] = OrderedDict()
            cur_dict = d[key_name]
            cur_dict.update(
                OrderedDict(
                    (k, vv)
                    for k, v in zip(headers, lcontents)
                    if k in select_headers and ((vv := conv_val(v)) or not skip_zero_or_empty)
                )
            )
            # if parent:
            #     cur_dict["@parent"] = parent
            cur_mod = key_name
            i += 1
        return i

    util_dict: OrderedDict[str, Any] = OrderedDict()
    add_hier(util_dict)

    return util_dict
