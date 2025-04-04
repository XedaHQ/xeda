import itertools
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ...dataclass import Field, XedaBaseModel
from ...utils import HierDict, parse_xml
from ...flow import FpgaSynthFlow
from ..vivado import Vivado
from ..vivado.vivado_synth import VivadoSynth
from ..vivado.vivado_sim import VivadoSim

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

    class Settings(VivadoSynth.Settings, VivadoSim.Settings):
        """Settings for Vivado synthesis and simulation in project mode"""

        pass

    def run(self):
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
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
