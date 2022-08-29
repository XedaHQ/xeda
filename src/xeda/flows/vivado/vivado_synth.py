import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...dataclass import Field, XedaBaseModel, validator
from ..flow import FpgaSynthFlow
from ..vivado import Vivado

log = logging.getLogger(__name__)


StepsValType = Optional[Dict[str, Any]]


class RunOptions(XedaBaseModel):
    strategy: Optional[str] = None
    steps: Dict[str, StepsValType] = {}


class VivadoSynth(Vivado, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using a project-based flow"""

    class Settings(Vivado.Settings, FpgaSynthFlow.Settings):
        """Settings for Vivado synthesis in project mode"""

        # FIXME implement and verify all
        fail_critical_warning = Field(
            False,
            description="flow fails if any Critical Warnings are reported by Vivado",
        )  # type: ignore
        fail_timing = Field(True, description="flow fails if timing is not met")  # type: ignore
        optimize_power = False
        optimize_power_postplace = False
        blacklisted_resources: List[str] = Field(  # TODO: remove
            # ["latch"],
            [],
            description="list of FPGA resources which are not allowed to be inferred or exist in the results. Valid values: latch, dsp, bram",
        )
        input_delay: Optional[float] = None
        output_delay: Optional[float] = None
        out_of_context: bool = False
        write_checkpoint: bool = False
        write_netlist: bool = True
        write_bitstream: bool = False
        qor_suggestions: bool = False
        synth: RunOptions = RunOptions(
            strategy="Flow_PerfOptimized_high",
            steps={
                "SYNTH_DESIGN": {},
                "OPT_DESIGN": {},
                "POWER_OPT_DESIGN": {},
            },
        )
        impl: RunOptions = RunOptions(
            strategy="Performance_ExploreWithRemap",
            steps={
                "PLACE_DESIGN": {},
                "POST_PLACE_POWER_OPT_DESIGN": {},
                "PHYS_OPT_DESIGN": {},
                "ROUTE_DESIGN": {},
                "WRITE_BITSTREAM": {},
            },
        )

        @validator("fpga")
        def _validate_fpga(cls, value):
            if not value or not value.part:
                raise ValueError("FPGA.part must be specified")
            return value

    def run(self):
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
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

        if not self.design.rtl.clock_port:
            log.critical(
                "No clocks specified for top RTL design. Continuing with synthesis anyways."
            )
        else:
            assert (
                settings.clock_period
            ), "`clock_period` must be specified and be positive value"
            freq = 1000 / settings.clock_period
            log.info(
                "clock.port=%s clock.frequency=%.3f MHz",
                self.design.rtl.clock_port,
                freq,
            )
        clock_xdc_path = self.copy_from_template("clock.xdc")

        if settings.blacklisted_resources:
            log.info("blacklisted_resources: %s", self.settings.blacklisted_resources)

        if settings.synth.steps["SYNTH_DESIGN"] is None:
            settings.synth.steps["SYNTH_DESIGN"] = {}
        assert settings.synth.steps["SYNTH_DESIGN"] is not None
        if "bram_tile" in settings.blacklisted_resources:
            # FIXME also add -max_uram 0 for ultrascale+
            settings.synth.steps["SYNTH_DESIGN"]["MAX_BRAM"] = 0
        if "dsp" in settings.blacklisted_resources:
            settings.synth.steps["SYNTH_DESIGN"]["MAX_DSP"] = 0

        reports_tcl = self.copy_from_template("vivado_report_helper.tcl")
        script_path = self.copy_from_template(
            "vivado_synth.tcl", xdc_files=[clock_xdc_path], reports_tcl=reports_tcl
        )
        self.vivado.run("-source", script_path)

    def parse_timing_report(self, reports_dir) -> bool:
        failed = False
        assert isinstance(self.settings, self.Settings)
        if self.design.rtl.clock_port and self.settings.clock_period:
            failed |= not self.parse_report_regex(
                reports_dir / "timing_summary.rpt",
                ##
                r"Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+"
                r"TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+"
                r"THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+"
                r"TPWS Failing Endpoints\s+TPWS Total Endpoints\s*"
                r"\s*(?:\-+\s+)+"
                r"(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<_tns>\-?\d+(?:\.\d+)?)\s+(?P<_failing_endpoints>\d+)\s+(?P<_tns_total_endpoints>\d+)\s+"
                r"(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<_ths>\-?\d+(?:\.\d+)?)\s+(?P<_ths_failing_endpoints>\d+)\s+(?P<_ths_total_endpoints>\d+)\s+",
                ##
                r"Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<clock_frequency>\d+(?:\.\d+)?)",
                required=False,
            )
            wns = self.results.get("wns")
            if wns:
                if not isinstance(wns, (float, int)):
                    log.critical("Parse value for `WNS` is %s (%s)", wns, type(wns))
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
        report_stage = "route_design"
        reports_dir = Path("reports") / report_stage

        failed = not self.parse_timing_report(reports_dir)

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
                        self.results[k] = self.get_from_path(utilization, path)
                    except KeyError as e:
                        log.debug(
                            "determining %s: property %s (in %s) was not found in the utilization report %s.",
                            k,
                            e.args[0] if len(e.args) > 0 else "?",
                            path,
                            report_file,
                        )
            self.results["_utilization"] = utilization

        assert isinstance(self.settings, self.Settings)

        if not failed:
            for res in self.settings.blacklisted_resources:
                res_util = self.results.get(res)
                if res_util is not None:
                    try:
                        res_util = int(res_util)
                        if res_util > 0:
                            log.critical(
                                "%s utilization report lists %s use(s) of blacklisted resource: %s",
                                report_stage,
                                res_util,
                                res,
                            )
                            failed = True
                    except ValueError:
                        log.warning(
                            "Unknown utilization value: %s for blacklisted resource %s. Assuming results are not violating the blacklist criteria.",
                            res_util,
                            res,
                        )

            # TODO better fail analysis for vivado
            if "wns" in self.results:
                failed |= self.results["wns"] < 0
            if "whs" in self.results:
                failed |= self.results["whs"] < 0
            if "_failing_endpoints" in self.results:
                failed |= self.results["_failing_endpoints"] != 0

        return not failed
