"""Flow for FPGA synthesis with Intel Quartus."""

import csv
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Set

from ...dataclass import Field
from ...design import SourceType
from ...flow import FpgaSynthFlow, MetricSpec, NoReplacement, describe_results
from ...tool import Docker, Tool
from ...types import PathLike
from ...units import count, ratio
from ...utils import try_convert_to_primitives

log = logging.getLogger(__name__)


def identity(x: Any) -> Any:
    return x


def parse_csv(
    path: PathLike,
    id_field: Optional[str],
    field_parser: Callable[[str], Any] = identity,
    id_parser: Callable[[str], Any] = identity,
    interesting_fields: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Parse a CSV file that a TCL script wrote."""
    data: Dict[str, Any] = {}
    with open(path, newline="") as csvfile:
        if id_field:
            # With a header row, data rows are indexed by `id_field`.
            reader = csv.DictReader(csvfile)
            for row in reader:
                if interesting_fields is None:
                    interesting_fields = set(row)
                row_id = row.get(id_field)
                if row_id:
                    data[id_parser(row_id)] = {
                        k: field_parser(row[k]) for k in interesting_fields if k in row
                    }
                else:
                    log.critical("ID field %s not found in row (%s)", id_field, row)
        else:
            # Without a header, each line is one key,value pair.
            for lrow in csv.reader(csvfile):
                if len(lrow) >= 2:
                    data[id_parser(lrow[0])] = field_parser(",".join(lrow[1:]))
                else:
                    log.critical("Line is not in key,value format: %s", lrow)
    return data


def try_num(s: str):
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def try_float(s: str):
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return s


class Quartus(FpgaSynthFlow):
    """Compile a design for an Intel FPGA with Quartus."""

    executables = ("quartus_sh",)

    #: The `quartus_sh` tool. `init()` makes one for each flow, because a tool holds the flow's
    #: settings (`dockerized`) and what it learned about the program. Neither belongs to the class.
    quartus_sh: Tool

    results_description = describe_results(
        "Fmax",
        "wns",
        "whs",
        lut=MetricSpec("Number of combinational ALUTs / logic elements used.", "integer"),
        ff=MetricSpec("Number of dedicated logic registers used.", "integer"),
    )

    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
            SourceType.Sdc,
        }
    )

    class Settings(FpgaSynthFlow.Settings):
        removed_settings = {
            **FpgaSynthFlow.Settings.removed_settings,
            "synthesis_effort": NoReplacement("Xeda passed it to nothing"),
        }
        # Intel part numbers (`fpga.part`) come in many formats.
        # See: https://www.intel.com/content/dam/www/central-libraries/us/en/documents/product-catalog.pdf
        seed: Optional[int] = Field(
            None, description="Value of the Quartus `SEED` assignment.", json_schema_extra=count()
        )
        optimization_mode: Literal[
            "BALANCED",
            "HIGH PERFORMANCE EFFORT",
            "AGGRESSIVE PERFORMANCE",
            "High Performance with Maximum Placement Effort",
            "Superior Performance",
            "Superior Performance with Maximum Placement Effort",
            "Aggressive Area",
            "High Placement Routability Effort",
            "High Packing Routability Effort",
            "Optimize Netlist for Routability",
            "High Power Effort",
        ] = Field(
            "HIGH PERFORMANCE EFFORT",
            description="Value of the Quartus `OPTIMIZATION_MODE` assignment. See "
            "https://www.intel.com/content/www/us/en/programmable/documentation/zpr1513988353912.html "
            "and https://www.intel.com/content/www/us/en/programmable/quartushelp/current/index.htm.",
        )
        remove_redundant_logic: bool = Field(
            True,
            description="Let synthesis remove logic that cannot affect any output (the Quartus "
            "`REMOVE_REDUNDANT_LOGIC_CELLS` assignment).",
        )
        auto_resource_sharing: bool = Field(
            True,
            description="Let synthesis share arithmetic operators between mutually exclusive "
            "operations, trading some speed for area.",
        )
        retiming: bool = Field(
            True,
            description="Allow Quartus to move registers across combinational logic to balance "
            "path delays. This sets the `ALLOW_REGISTER_RETIMING` and "
            "`PHYSICAL_SYNTHESIS_REGISTER_RETIMING` assignments.",
        )
        register_duplication: bool = Field(
            True,
            description="Allow the fitter to duplicate registers to reduce fanout and routing "
            "delay, at the cost of extra registers.",
        )
        packed_registers: bool = Field(
            False,
            description="Value of the `AUTO_PACKED_REGISTERS_STRATIX` and "
            "`AUTO_PACKED_REGISTERS_CYCLONE` assignments, which control register packing.",
        )
        gated_clock_conversion: bool = Field(
            False,
            description="Convert gated clocks into clock-enable logic, which is generally safer "
            "on FPGAs than true clock gating.",
        )
        dsp_recognition: bool = Field(
            True,
            description="Infer DSP blocks from multipliers and MAC patterns. Disable to force "
            "everything into logic.",
        )
        ram_recognition: bool = Field(
            True, description="Infer block RAM from memory patterns in the HDL."
        )
        rom_recognition: bool = Field(
            True, description="Infer ROM blocks from constant memory patterns."
        )
        fitter_effort: Literal["STANDARD FIT", "AUTO FIT", "FAST_FIT"] = Field(
            "AUTO FIT",
            description='Fitter effort. "AUTO FIT" stops once timing is met, "STANDARD FIT" '
            'always optimizes fully, and "FAST_FIT" trades quality for compile time.',
        )
        optimization_technique: Literal["AREA", "SPEED", "BALANCED"] = Field(
            "SPEED", description="What synthesis optimizes for."
        )
        placement_effort_multiplier: float = Field(
            2.0,
            description="Multiplier for the time the fitter spends on placement. The Quartus "
            "default is 1.0. A value must be greater than 0 and can be fractional. A value below "
            "1 can shorten fitting time but can lower placement quality and design performance. "
            "A value above 1 increases placement time and quality, and may reduce routing time "
            "for designs with routing congestion. A value of 4 increases fitting time by about 2 "
            "to 4 times and may improve quality.",
            json_schema_extra=ratio(),
        )
        router_timing_optimization_level: Literal["Normal", "Maximum", "Minimum"] = Field(
            "Maximum",
            description="Router effort for timing optimization. Higher effort increases compile "
            "time.",
        )
        final_placement_optimization: Literal["ALWAYS", "AUTOMATICALLY", "NEVER"] = Field(
            "ALWAYS",
            description="When to run the final placement optimization pass after routing.",
        )

    def init(self) -> None:
        self.quartus_sh = Tool(
            executable="quartus_sh",
            flow=self,
            docker=Docker(
                command=["quartus_wrapper", "quartus_sh"],
                image="alterafpga/quartuspro-v25.3",
                tag="20.1.0",
                platform="linux/amd64",
            ),  # pyright: ignore
        )
        if self.quartus_sh.docker:
            # `mounts` are writable, so the design stays out of them. A tool built in a flow
            # mounts the design read-only for each run (`Tool.execute`).
            if self.settings.dockerized and self.quartus_sh.docker.nproc and self.settings.nthreads:
                self.settings.nthreads = min(self.settings.nthreads, self.quartus_sh.docker.nproc)

    def create_project(self, **kwargs: Any) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        project_settings = {
            "SEED": ss.seed,
            "OPTIMIZATION_MODE": ss.optimization_mode,
            "REMOVE_REDUNDANT_LOGIC_CELLS": ss.remove_redundant_logic,
            "AUTO_RESOURCE_SHARING": ss.auto_resource_sharing,
            "ALLOW_REGISTER_RETIMING": ss.retiming,
            "SYNTH_GATED_CLOCK_CONVERSION": ss.gated_clock_conversion,
            "FITTER_EFFORT": ss.fitter_effort,
            "STRATIX_OPTIMIZATION_TECHNIQUE": ss.optimization_technique,
            "CYCLONE_OPTIMIZATION_TECHNIQUE": ss.optimization_technique,
            # See https://www.intel.com/content/www/us/en/programmable/documentation/rbb1513988527943.html
            "PLACEMENT_EFFORT_MULTIPLIER": ss.placement_effort_multiplier,
            "ROUTER_TIMING_OPTIMIZATION_LEVEL": ss.router_timing_optimization_level,
            "FINAL_PLACEMENT_OPTIMIZATION": ss.final_placement_optimization,
            # "PHYSICAL_SYNTHESIS_COMBO_LOGIC_FOR_AREA": True,
            # "ADV_NETLIST_OPT_SYNTH_GATE_RETIME": True,
            # "ADV_NETLIST_OPT_SYNTH_WYSIWYG_REMAP": True,
            "AUTO_PACKED_REGISTERS_STRATIX": ss.packed_registers,
            "AUTO_PACKED_REGISTERS_CYCLONE": ss.packed_registers,
            "PHYSICAL_SYNTHESIS_COMBO_LOGIC": True,
            "PHYSICAL_SYNTHESIS_REGISTER_DUPLICATION": ss.register_duplication,
            "PHYSICAL_SYNTHESIS_REGISTER_RETIMING": ss.retiming,
            # "PHYSICAL_SYNTHESIS_EFFORT": "EXTRA",
            # NORMAL, OFF, EXTRA_EFFORT
            # "OPTIMIZE_POWER_DURING_SYNTHESIS": "NORMAL",
            # SYNTH_CRITICAL_CLOCK (ON, OFF): speed optimization technique for clock domains
            "AUTO_DSP_RECOGNITION": ss.dsp_recognition,
            "AUTO_RAM_RECOGNITION": ss.ram_recognition,
            "AUTO_ROM_RECOGNITION": ss.rom_recognition,
            "FLOW_ENABLE_POWER_ANALYZER": True,
        }

        clock_sdc_path = self.copy_from_template("clock.sdc")
        script_path = self.copy_from_template(
            "create_project.tcl",
            sdc_files=[clock_sdc_path],
            project_settings=project_settings,
            **kwargs,
        )
        self.quartus_sh.run("-t", script_path)

    def run(self) -> None:
        self.create_project()
        script_path = self.copy_from_template("compile.tcl")
        # `execute_flow -compile` always runs `quartus_asm`, which writes `<design_name>.sof`
        # into the project directory (the run directory).
        self.artifacts.bitstream = f"{self.design.name}.sof"
        self.quartus_sh.run("-t", script_path)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        reports = {
            "summary": self.settings.reports_dir / "Flow_Summary.csv",
            "utilization": self.settings.reports_dir
            / "Fitter"
            / "Resource_Section"
            / "Fitter_Resource_Utilization_by_Entity.csv",
            "timing_dir": self.settings.reports_dir / "Timing_Analyzer",
            "timing.multicorner_summary": self.settings.reports_dir
            / "Timing_Analyzer"
            / "Multicorner_Timing_Analysis_Summary.csv",
        }
        # Read only the reports that this run wrote (`report_file`).
        summary = self.report_file(reports["summary"])
        utilization_report = self.report_file(reports["utilization"])
        mc_report = self.report_file(reports["timing.multicorner_summary"])
        if summary is None or utilization_report is None or mc_report is None:
            return False
        resources = parse_csv(summary, id_field=None)
        self.results.update(resources)
        resources = parse_csv(
            utilization_report,
            id_field="Compilation Hierarchy Node",
            field_parser=lambda s: try_num(s.split()[0]),
            id_parser=lambda s: s.strip().lstrip("|"),
            # interesting_fields={
            #     "Logic Cells",
            #     "LUT-Only LCs",
            #     "Register-Only LCs",
            #     "LUT/Register LCs",
            #     "Dedicated Logic Registers",
            #     "ALMs needed [=A-B+C]",
            #     "Combinational ALUTs",
            #     "ALMs used for memory",
            #     "Memory Bits",
            #     "M10Ks",
            #     "M9Ks",
            #     "DSP Elements",
            #     "DSP Blocks",
            #     "DSP 9x9",
            #     "DSP 18x18"
            #     "Block Memory Bits",
            #     "Pins",
            #     "I/O Registers",
            # },
        )
        assert self.design.rtl.top
        top_resources = resources.get(self.design.rtl.top)
        if top_resources:
            top_resources.setdefault(0)
            lut_only = top_resources.get("LUT-Only LCs", 0)
            lut_reg = top_resources.get("LUT/Register LCs", 0)
            top_resources["lut"] = lut_only + lut_reg
            reg_only = top_resources.get("Register-Only LCs", 0)
            top_resources["ff"] = reg_only + lut_reg
            self.results.update(top_resources)

        # TODO: say why this timing report is chosen.

        slacks = parse_csv(
            mc_report,
            id_field="Clock",
            field_parser=try_float,
            id_parser=lambda s: s.strip(),
            interesting_fields={"Setup", "Hold"},
        )
        log.debug("slacks: %s", slacks)
        worst_slacks = slacks.get("Worst-case Slack")
        log.debug("worst_slacks: %s", worst_slacks)
        if worst_slacks:
            wns = worst_slacks.get("Setup")
            log.debug("wns: %s", wns)
            whs = worst_slacks.get("Hold")
            if wns is not None:
                wns = try_convert_to_primitives(wns)
                self.results["wns"] = wns
            if whs is not None:
                whs = try_convert_to_primitives(whs)
                self.results["whs"] = whs

        timing_reports_folder = Path(reports["timing_dir"])
        max_fmax = 0.0
        for found in timing_reports_folder.glob("Slow_*_Model/Slow_*_Model_Fmax_Summary.csv"):
            fmax_report = self.report_file(found, optional=True)
            if fmax_report is None:
                continue
            log.info("Parsing timing report: %s", fmax_report)
            fmax = parse_csv(
                fmax_report,
                id_field="Clock Name",
                field_parser=lambda s: s.strip().split(),
                id_parser=lambda s: s.strip(),
                interesting_fields={"Fmax"},
            )
            conditions = fmax_report.parent.name.lstrip("Slow_").rstrip("_Model").split("_")
            for clock in self.settings.clocks.keys():
                flst = fmax.get(clock, {}).get("Fmax")
                assert flst and len(flst) == 2
                fmhz = float(flst[0])
                if flst[1] == "GHz":
                    fmhz *= 1000.0
                else:
                    assert flst[1] == "MHz"
                if fmhz > max_fmax:
                    max_fmax = fmhz
                self.results[f'{clock} Fmax@{":".join(conditions)} (MHz)'] = fmhz
        if max_fmax:
            self.results["Fmax"] = max_fmax

        return True

    def check_results(self) -> bool:
        """Reject the run if the worst setup or hold slack that the timing report shows is
        negative."""
        ok = super().check_results()
        causes = []
        for label, key, name in (("setup", "wns", "WNS"), ("hold", "whs", "WHS")):
            slack = self.results.get(key)
            if isinstance(slack, (int, float)) and not isinstance(slack, bool) and slack < 0:
                causes.append(f"negative {label} slack ({name} {slack} ns)")
        if not causes:
            return ok
        self.reject("timing not met: " + "; ".join(causes))
        return False
