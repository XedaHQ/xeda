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
from ...flow import FpgaSynthFlow
from ..vivado import Vivado

log = logging.getLogger(__name__)


StepsValType = Union[None, List[str], Dict[str, Any]]


def vivado_synth_generics(design: Design) -> List[str]:
    generics = []
    for k, v in design.rtl.parameters.items():
        if isinstance(v, bool):
            v = f"1'b{int(v)}"
        elif isinstance(v, str) and not re.match(r"\d+'b[01]+", v):
            v = '\\"' + v + '\\"'
        generics.append(f"{k}={v}")
    return generics


class RunOptions(XedaBaseModel):
    strategy: Optional[str] = None
    steps: Dict[str, StepsValType] = {}


class VivadoSynth(Vivado, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using a project-based flow"""

    class Settings(Vivado.Settings, FpgaSynthFlow.Settings):
        """Settings for Vivado synthesis in project mode"""

        # FIXME implement and verify all
        fail_critical_warning: bool = Field(
            False,
            description="flow fails if any Critical Warnings are reported by Vivado",
        )  # pyright: ignore
        fail_timing: bool = Field(
            True, description="flow fails if timing is not met"
        )  # pyright: ignore
        blacklisted_resources: List[str] = Field(  # TODO: remove
            # ["latch"],
            [],
            description="list of FPGA resources which are not allowed to be inferred or exist in the results. Valid values: latch, dsp, bram",
        )
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

        @validator("fpga")
        def _validate_fpga(cls, value):
            if not value or not value.part:
                raise ValueError("FPGA.part must be specified")
            return value

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

        if not self.design.rtl.clocks:
            log.critical(
                "No clocks specified for top RTL design. Continuing with synthesis anyways."
            )

        clock_xdc_path = self.copy_from_template("clock.xdc")

        log.debug("blacklisted_resources: %s", settings.blacklisted_resources)

        if settings.synth.steps["SYNTH_DESIGN"] is None:
            settings.synth.steps["SYNTH_DESIGN"] = {}
        assert isinstance(settings.synth.steps["SYNTH_DESIGN"], dict)
        if any(item in settings.blacklisted_resources for item in ("bram_tile", "bram")):
            # FIXME also add -max_uram 0 for ultrascale+
            settings.synth.steps["SYNTH_DESIGN"]["MAX_BRAM"] = 0
        if "dsp" in settings.blacklisted_resources:
            settings.synth.steps["SYNTH_DESIGN"]["MAX_DSP"] = 0

        reports_tcl = self.copy_from_template("vivado_report_helper.tcl")

        xdc_files = [p.file for p in self.design.rtl.sources if p.type == "xdc"]
        xdc_files += [self.normalize_path_to_design_root(p) for p in settings.xdc_files]
        assert clock_xdc_path not in xdc_files, f"XDC file {xdc_files} was already included."
        xdc_files.append(clock_xdc_path)

        script_path = self.copy_from_template(
            "vivado_synth.tcl",
            xdc_files=xdc_files,
            reports_tcl=reports_tcl,
            generics=" ".join(vivado_synth_generics(self.design)),
        )
        self.vivado.run("-source", script_path)

    def parse_timing_report(self, reports_dir) -> bool:
        assert isinstance(self.settings, self.Settings)
        if not self.design.rtl.clocks:
            log.warning(
                "Skipping parse of timing reports as no design clocks (Design.rtl.clocks) were specified "
            )
            return True
        if not self.settings.clocks:
            log.warning(
                "Skipping parse of timing reports as no flow clocks (Flow.settings.clocks) were specified."
            )
            return True
        failed = not self.parse_report_regex(
            reports_dir / "timing_summary.rpt",
            ##
            r"Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+"
            r"TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+"
            r"THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+"
            r"TPWS Failing Endpoints\s+TPWS Total Endpoints\s*"
            r"\s*(?:\-+\s+)+"
            r"(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<tns>\-?\d+(?:\.\d+)?)\s+(?P<setup_violations>\d+)\s+(?P<_tns_total_endpoints>\d+)\s+"
            r"(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<_ths>\-?\d+(?:\.\d+)?)\s+(?P<hold_violations>\d+)\s+(?P<_ths_total_endpoints>\d+)\s+",
            ##
            r"Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<clock_frequency>\d+(?:\.\d+)?)",
            required=False,
        )
        wns = self.results.get("wns")
        su_fail = self.results.get("setup_violations")
        if su_fail:
            log.error("%s setup violations. WNS: %s", su_fail, wns)
            failed = True
        hld_fail = self.results.get("hold_violations")
        if hld_fail:
            log.error("%s hold violations. WHS: %s", hld_fail, self.results.get("whs"))
            failed = True
        if wns is not None:
            if not isinstance(wns, (float, int)):
                log.critical("Parsed value for `WNS` is %s (%s)", wns, type(wns))
            else:
                if wns < 0:
                    failed = True
                # see https://support.xilinx.com/s/article/57304?language=en_US
                # Fmax in Megahertz
                # Here Fmax refers to: "The maximum frequency a design can run on Hardware in a given implementation
                # Fmax = 1/(T-WNS), with WNS positive or negative, where T is the target clock period."
                if "clock_period" in self.results:
                    clock_period = self.results["clock_period"]
                    assert isinstance(
                        clock_period, (float, int)
                    ), f"clock_period: {clock_period} is not a number"
                    self.results["Fmax"] = 1000.0 / (clock_period - wns)

        return not failed

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)

        for report_file in self.settings.reports_dir.glob("**/*"):
            if report_file.is_file():
                self.artifacts[str(report_file)] = report_file

        for log_file in self.run_path.glob("**/*.log"):
            if log_file.is_file():
                self.artifacts[str(log_file)] = log_file

        if self.settings.write_bitstream:
            for bitstream in self.run_path.glob("**/*.bit"):
                if bitstream.is_file():
                    self.artifacts["bitstream"] = bitstream
                    break

        reports_dir = self.settings.reports_dir / "route_design"
        failed = not self.parse_timing_report(reports_dir)
        hier_util = parse_hier_util(reports_dir / "hierarchical_utilization.xml")
        if hier_util:
            with open(reports_dir / "hierarchical_utilization.json", "w") as f:
                json.dump(hier_util, f)
            self.results["_hierarchical_utilization"] = hier_util
        else:
            log.error("Parsing hierarchical utilization failed!")

        report_file = reports_dir / "utilization.xml"
        utilization = self.parse_xml_report(report_file)
        # ordered dict
        fields = [
            ("slice", ["Slice Logic Distribution", "Slice"]),
            ("slice", ["CLB Logic Distribution", "CLB"]),  # Ultrascale+
            ("lut", ["Slice Logic", "Slice LUTs"]),
            ("lut", ["Slice Logic", "Slice LUTs*"]),
            ("lut", ["CLB Logic", "CLB LUTs"]),
            ("lut", ["CLB Logic", "CLB LUTs*"]),
            ("lut_logic", ["Slice Logic", "LUT as Logic"]),
            ("lut_logic", ["CLB Logic", "LUT as Logic"]),
            ("lut_mem", ["Slice Logic", "LUT as Memory"]),
            ("lut_mem", ["CLB Logic", "LUT as Memory"]),
            ("ff", ["Slice Logic", "Register as Flip Flop"]),
            ("ff", ["CLB Logic", "CLB Registers"]),
            ("latch", ["Slice Logic", "Register as Latch"]),
            ("latch", ["CLB Logic", "Register as Latch"]),
            ("bram_tile", ["Memory", "Block RAM Tile"]),
            ("bram_tile", ["BLOCKRAM", "Block RAM Tile"]),
            ("bram_RAMB36", ["Memory", "RAMB36/FIFO*"]),
            ("bram_RAMB36", ["BLOCKRAM", "RAMB36/FIFO*"]),
            ("bram_RAMB18", ["Memory", "RAMB18"]),
            ("bram_RAMB18", ["BLOCKRAM", "RAMB18"]),
            ("dsp", ["DSP", "DSPs"]),
            ("dsp", ["ARITHMETIC", "DSPs"]),
        ]
        if utilization is not None:
            for k, path in fields:
                if self.results.get(k) is None:
                    path.append("Used")
                    try:
                        util = self.get_from_path(utilization, path)
                    except KeyError as e:
                        util = None
                        log.debug(
                            "determining %s: property %s (in %s) was not found in the utilization report %s.",
                            k,
                            e.args[0] if len(e.args) > 0 else "?",
                            path,
                            report_file,
                        )
                    if util is not None:
                        r = try_convert(util, int, default=util)
                        if r:
                            self.results[k] = r
            self.results["_utilization"] = utilization
        if failed:
            return False
        for resource in self.settings.blacklisted_resources:
            res_util = self.results.get(resource)
            if res_util is not None:
                try:
                    res_util = int(res_util)
                    if res_util > 0:
                        log.critical(
                            "utilization report lists %s use(s) of blacklisted resource: %s",
                            res_util,
                            resource,
                        )
                        failed = True
                except ValueError:
                    log.warning(
                        "Unknown utilization value: %s for blacklisted resource %s. Assuming results are not violating the blacklist criteria.",
                        res_util,
                        resource,
                    )
        # TODO better fail analysis for vivado
        wns = self.results.get("wns")
        if wns is not None and isinstance(wns, (float, int, str)):
            failed |= float(wns) < 0.0
        whs = self.results.get("whs")
        if whs is not None and isinstance(whs, (float, int, str)):
            failed |= float(whs) < 0.0
        if "_failing_endpoints" in self.results:
            failed |= self.results["_failing_endpoints"] != 0
        return not failed


def parse_hier_util(
    report: Union[Path, os.PathLike, str],
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
                    if k in select_headers  # type: ignore
                    and ((vv := conv_val(v)) or not skip_zero_or_empty)
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
