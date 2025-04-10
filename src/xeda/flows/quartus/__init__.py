"""Intel Quartus flows"""

import csv
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Set

from ...dataclass import Field
from ...flow import FpgaSynthFlow
from ...tool import Docker, Tool
from ...types import PathLike
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
    """Parse TCL-generated CSV file"""
    data: Dict[str, Any] = {}
    with open(path, newline="") as csvfile:
        if id_field:
            # with header, and some rows of data indexed by id_field
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
            # no header, key/value pairs on each line
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
    """FPGA synthesis using Intel Quartus"""

    quartus_sh = Tool(
        executable="quartus_sh",
        docker=Docker(
            command=["quartus_wrapper", "quartus_sh"],
            image="chriz2600/quartus-lite",
            tag="20.1.0",
            platform="linux/amd64",
        ),  # pyright: ignore
    )

    class Settings(FpgaSynthFlow.Settings):
        # part number (fpga.part) formats are quite complicated.
        # See: https://www.intel.com/content/dam/www/central-libraries/us/en/documents/product-catalog.pdf
        seed: Optional[int] = Field(None, description="Seed")
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
            description="""
            see https://www.intel.com/content/www/us/en/programmable/documentation/zpr1513988353912.html
            https://www.intel.com/content/www/us/en/programmable/quartushelp/current/index.htm
        """,
        )
        remove_redundant_logic: bool = True
        auto_resource_sharing: bool = True
        retiming: bool = True
        register_duplication: bool = True
        packed_registers: bool = False
        gated_clock_conversion: bool = False
        dsp_recognition: bool = True
        ram_recognition: bool = True
        rom_recognition: bool = True
        synthesis_effort: Literal["auto", "fast"] = "auto"
        fitter_effort: Literal["STANDARD FIT", "AUTO FIT", "FAST_FIT"] = "AUTO FIT"
        optimization_technique: Literal["AREA", "SPEED", "BALANCED"] = "SPEED"
        placement_effort_multiplier: float = Field(
            2.0,
            description="""
        A logic option that controls how much time the Fitter spends in placement.
        The default value is 1.0 and legal values must be greater than 0 and can be non-integer values.
        Values between 0 and 1 can reduce fitting time, but also can reduce placement quality and design performance.
        Values greater than 1 increase placement time and placement quality, but may reduce routing time for designs with routing congestion.
        For example, a value of 4 increases fitting time by approximately 2 to 4 times, but may improve quality.""",
        )
        router_timing_optimization_level: Literal["Normal", "Maximum", "Minimum"] = "Maximum"
        final_placement_optimization: Literal["ALWAYS", "AUTOMATICALLY", "NEVER"] = "ALWAYS"

    def init(self) -> None:
        # FIXME only a proof of concept. Tool instantiation/invocation needs to change!
        if self.quartus_sh.docker:
            # FIXME
            for src in self.design.rtl.sources:
                p = str(src.file.parent.resolve())
                self.quartus_sh.docker.mounts[p] = p
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
            # faster:
            "FITTER_EFFORT": ss.fitter_effort,
            # AREA, SPEED, BALANCED
            "STRATIX_OPTIMIZATION_TECHNIQUE": ss.optimization_technique,
            "CYCLONE_OPTIMIZATION_TECHNIQUE": ss.optimization_technique,
            # see https://www.intel.com/content/www/us/en/programmable/documentation/rbb1513988527943.html
            "PLACEMENT_EFFORT_MULTIPLIER": ss.placement_effort_multiplier,
            "ROUTER_TIMING_OPTIMIZATION_LEVEL": ss.router_timing_optimization_level,
            "FINAL_PLACEMENT_OPTIMIZATION": ss.final_placement_optimization,
            # "PHYSICAL_SYNTHESIS_COMBO_LOGIC_FOR_AREA": True,
            # ?
            # "ADV_NETLIST_OPT_SYNTH_GATE_RETIME": True,
            # ?
            # "ADV_NETLIST_OPT_SYNTH_WYSIWYG_REMAP": True,
            "AUTO_PACKED_REGISTERS_STRATIX": ss.packed_registers,
            "AUTO_PACKED_REGISTERS_CYCLONE": ss.packed_registers,
            "PHYSICAL_SYNTHESIS_COMBO_LOGIC": True,
            "PHYSICAL_SYNTHESIS_REGISTER_DUPLICATION": ss.register_duplication,
            "PHYSICAL_SYNTHESIS_REGISTER_RETIMING": ss.retiming,
            # "PHYSICAL_SYNTHESIS_EFFORT": "EXTRA",
            # NORMAL, OFF, EXTRA_EFFORT
            # "OPTIMIZE_POWER_DURING_SYNTHESIS": "NORMAL",
            # SYNTH_CRITICAL_CLOCK: ON, OFF : Speed Optimization Technique for Clock Domains}
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
        self.quartus_sh.run("-t", script_path)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        failed = False
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
        resources = parse_csv(reports["summary"], id_field=None)
        self.results.update(resources)
        utilization_report = Path(reports["utilization"])
        assert utilization_report.exists()
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

        # TODO reference for why this timing report is chosen

        mc_report = reports["timing.multicorner_summary"]

        slacks = parse_csv(
            mc_report,
            id_field="Clock",
            field_parser=try_float,
            id_parser=lambda s: s.strip(),
            interesting_fields={"Setup", "Hold"},
        )
        print("slacks:", slacks)
        worst_slacks = slacks.get("Worst-case Slack")
        print("worst_slacks:", worst_slacks)
        if worst_slacks:
            wns = worst_slacks.get("Setup")
            print("wns:", wns)
            whs = worst_slacks.get("Hold")
            if wns is not None:
                wns = try_convert_to_primitives(wns)
                self.results["wns"] = wns
                if isinstance(wns, (int, float)):
                    failed |= wns < 0
            if whs is not None:
                whs = try_convert_to_primitives(whs)
                self.results["whs"] = whs
                if isinstance(whs, (int, float)):
                    failed |= whs < 0

        timing_reports_folder = Path(reports["timing_dir"])
        max_fmax = 0.0
        for fmax_report in timing_reports_folder.glob("Slow_*_Model/Slow_*_Model_Fmax_Summary.csv"):
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

        return not failed
