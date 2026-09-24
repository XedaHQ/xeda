import itertools
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ...dataclass import Field, XedaBaseModel
from ...design import SourceType
from ...flow import FpgaSynthFlow
from ...utils import HierDict, parse_xml
from ..vivado import Vivado
from ..vivado.vivado_sim import VivadoSim
from ..vivado.vivado_synth import (
    VivadoSynth,
    constraint_files,
    normalize_run_steps,
    post_step_hooks,
)

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
    """Create a Xilinx Vivado project for the design, to work on in Vivado.

    The project holds the design's sources and testbench, its constraints, the synthesis and
    implementation strategies and step settings, and the hooks that write `vivado_synth`'s
    reports after each step. Nothing is run: open the project (`artifacts.project`) in Vivado, or
    set `gui` to have the flow open it. For synthesis and implementation in batch, use
    `vivado_synth`.
    """

    # Creating a project reports nothing beyond the keys every flow reports.
    results_description: dict = {}

    class Settings(VivadoSynth.Settings, VivadoSim.Settings):
        """Settings for a Vivado project: synthesis, implementation and simulation"""

        gui: bool = Field(
            False,
            description="Open the created project in the Vivado GUI, which needs a display.",
        )

    def init(self):
        """Set up Vivado project execution and optional GUI mode."""
        super().init()
        assert isinstance(self.settings, self.Settings)
        if self.settings.gui:
            # `vivado -mode gui -source ...`: the script runs in the GUI, and the project stays
            # open there, as DC's `gui` runs `dc_shell -gui`
            args = list(self.vivado.default_args)
            args[args.index("-mode") + 1] = "gui"
            self.vivado.default_args = args

    def run(self):
        """Create the Vivado project and run its configured steps."""
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
        self.artifacts.project = f"{self.design.name}.xpr"

        normalize_run_steps(settings)
        assert isinstance(settings.synth.steps["SYNTH_DESIGN"], dict)
        if settings.flatten_hierarchy:
            settings.synth.steps["SYNTH_DESIGN"]["flatten_hierarchy"] = settings.flatten_hierarchy
        # reports are written after each major step, as `vivado_synth` writes them
        tcl_files = [self.process_path(p, subs_vars=True) for p in settings.tcl_files]
        tcl_files += post_step_hooks(self, settings)

        script_path = self.copy_from_template(
            "vivado_project.tcl",
            # constraints go to the constraint fileset, as `vivado_synth` reads them
            sources=[
                src
                for src in self.design.rtl.sources
                if src.type not in (SourceType.Xdc, SourceType.Sdc)
            ],
            xdc_files=constraint_files(self, settings),
            tcl_files=tcl_files,
            generics=" ".join(vivado_synth_generics(self.design.rtl.parameters)),
        )
        self.vivado.run("-source", script_path)

    def parse_reports(self) -> bool:
        """Check that the Vivado project file was created."""
        return (self.run_path / f"{self.design.name}.xpr").is_file()


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
