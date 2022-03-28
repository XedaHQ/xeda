# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from pathlib import Path
from typing import Any, Literal, Optional
from pydantic.fields import Field
import csv
import logging
from ..flow import FpgaSynthFlow
from ...tool import Tool

log = logging.getLogger(__name__)


def parse_csv(
    path,
    id_field: Optional[str],
    field_parser=(lambda x: x),
    id_parser=(lambda x: x),
    interesting_fields=None,
):
    """Parse TCL-generated CSV file"""
    data = {}

    with open(path, newline="") as csvfile:
        if id_field:
            # with header, and some rows of data indexed by id_field
            reader = csv.DictReader(csvfile)
            for row in reader:
                if interesting_fields is None:
                    interesting_fields = row.keys()
                id = id_parser(row[id_field])
                data[id] = {
                    k: field_parser(row[k]) for k in interesting_fields if k in row
                }
        else:
            # no header, key/value pairs on each line
            for lrow in csv.reader(csvfile):
                if len(lrow) == 2:
                    data[id_parser(lrow[0])] = field_parser(lrow[1])
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
        router_timing_optimization_level: Literal[
            "Normal", "Maximum", "Minimum"
        ] = "Maximum"
        final_placement_optimization: Literal[
            "ALWAYS", "AUTOMATICALLY", "NEVER"
        ] = "ALWAYS"

    def init(self) -> None:
        self.reports = {
            "summary": self.reports_dir / "Flow_Summary.csv",
            "utilization": self.reports_dir
            / "Fitter"
            / "Resource_Section"
            / "Fitter_Resource_Utilization_by_Entity.csv",
            "timing_dir": self.reports_dir / "Timing_Analyzer",
            "timing.multicorner_summary": self.reports_dir
            / "Timing_Analyzer"
            / "Multicorner_Timing_Analysis_Summary.csv",
        }
        self.quartus_sh = Tool("quartus_sh")

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

        clock_sdc_path = self.copy_from_template(f"clock.sdc")
        script_path = self.copy_from_template(
            f"create_project.tcl",
            sdc_files=[clock_sdc_path],
            project_settings=project_settings,
            **kwargs,
        )
        self.quartus_sh.run("-t", script_path)

        # self.run_process('quartus_sh',
        #                  ['--dse', '-project', self.settings.design['name'], '-nogui', '-concurrent-compiles', '8', '-exploration-space',
        #                   "Extra Effort Space", '-optimization-goal', "Optimize for Speed", '-report-all-resource-usage', '-ignore-failed-base'],
        #                  stdout_logfile='dse_stdout.log'
        #                  )

    # def __init__(self, settings, args, logger):
    #     # def supported_quartus_generic(k, v, sim):
    #     #     if sim:
    #     #         return True
    #     #     if isinstance(v, int):
    #     #         return True
    #     #     if isinstance(v, bool):
    #     #         return True
    #     #     v = str(v)
    #     #     return (v.isnumeric() or (v.strip().lower() in {'true', 'false'}))

    #     # def quartus_gen_convert(k, x, sim):
    #     #     if sim:
    #     #         if isinstance(x, dict) and "file" in x:
    #     #             p = x["file"]
    #     #             assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
    #     #             x = self.conv_to_relative_path(p.strip())
    #     #             self.logger.info(f'Converting generic `{k}` marked as `file`: {p} -> {x}')
    #     #     xl = str(x).strip().lower()
    #     #     if xl == 'false':
    #     #         return "1\\'b0"
    #     #     if xl == 'true':
    #     #         return "1\\'b1"
    #     #     return x

    #     # def quartus_generics(kvdict, sim):
    #     #     return ' '.join([f"-generic {k}={quartus_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_quartus_generic(k, v, sim)])

    #     super().__init__(settings, args, logger)

    #     # self.settings.flow['generics_options'] = quartus_generics(self.settings.design["generics"], sim=False)
    #     # self.settings.flow['tb_generics_options'] = quartus_generics(self.settings.design["tb_generics"], sim=True)

    # self.run_process('quartus_eda', [prj_name, '--simulation', '--functional', '--tool=modelsim_oem', '--format=verilog'],
    #                         stdout_logfile='eda_1_stdout.log'
    #                         )

    def run(self) -> None:
        self.create_project()
        script_path = self.copy_from_template(
            f"compile.tcl", reports_dir=self.reports_dir
        )
        self.quartus_sh.run("-t", script_path)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        failed = False
        reports = self.reports
        resources = parse_csv(reports["summary"], id_field=None)
        resources = parse_csv(
            reports["utilization"],
            id_field="Compilation Hierarchy Node",
            field_parser=lambda s: try_num(s.split()[0]),
            id_parser=lambda s: s.strip().lstrip("|"),
            interesting_fields=[
                "Logic Cells",
                "LUT-Only LCs",
                "Register-Only LCs",
                "LUT/Register LCs",
                "Dedicated Logic Registers",
                "ALMs needed [=A-B+C]",
                "Combinational ALUTs",
                "ALMs used for memory",
                "Memory Bits",
                "M10Ks",
                "M9Ks",
                "DSP Elements",
                "DSP Blocks",
                "Block Memory Bits",
                "Pins",
                "I/O Registers",
            ],
        )

        top_resources = resources.get(self.design.rtl.top, {})
        if top_resources:
            top_resources.setdefault(0)
            r0 = top_resources.get("LUT-Only LCs")
            if r0:
                top_resources["lut"] = r0 + top_resources.get("LUT/Register LCs", 0)
            r0 = top_resources.get("Register-Only LCs")
            if r0:
                top_resources["ff"] = r0 + top_resources.get("LUT/Register LCs", 0)

        self.results.update(top_resources)

        # TODO reference for why this timing report is chosen

        mc_report = reports["timing.multicorner_summary"]

        slacks = parse_csv(
            mc_report,
            id_field="Clock",
            field_parser=try_float,
            id_parser=lambda s: s.strip(),
            interesting_fields=["Setup", "Hold"],
        )
        worst_slacks = slacks["Worst-case Slack"]
        wns = worst_slacks["Setup"]
        whs = worst_slacks["Hold"]
        self.results["wns"] = wns
        self.results["whs"] = whs

        if isinstance(wns, float) or isinstance(wns, int):
            failed |= wns < 0
        if isinstance(whs, float) or isinstance(whs, int):
            failed |= whs < 0

        timing_reports_folder = Path(reports["timing_dir"])
        max_fmax = 0.0
        for fmax_report in timing_reports_folder.glob(
            "Slow_*_Model/Slow_*_Model_Fmax_Summary.csv"
        ):
            log.info(f"Parsing timing report: {fmax_report}")
            fmax = parse_csv(
                fmax_report,
                id_field="Clock Name",
                field_parser=lambda s: s.strip().split(),
                id_parser=lambda s: s.strip(),
                interesting_fields=["Fmax"],
            )
            conditions = (
                fmax_report.parent.name.lstrip("Slow_").rstrip("_Model").split("_")
            )
            for clock in self.settings.clocks.keys():
                flst = fmax.get(clock, {}).get("Fmax")
                assert len(flst) == 2
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


# class QuartusPower(QuartusSynth, SimFlow):
#     def run(self):
#         self.create_project(vcd=self.settings.flow.get('vcd'))
#         script_path = self.copy_from_template(f'compile.tcl')
#         self.run_process('quartus_sh',
#                          ['-t', str(script_path)],
#                          stdout_logfile='compile_stdout.log'
#                          )

#     def parse_reports(self):
#         failed = False

#         resources = parse_csv(
#             self.reports_dir / 'Fitter' / 'Resource_Section' / 'Fitter_Resource_Utilization_by_Entity.csv',
#             id_field='Compilation Hierarchy Node',
#             field_parser=lambda s: int(s.split()[0]),
#             id_parser=lambda s: s.strip()[1:],
#             interesting_fields=['Logic Cells', 'Memory Bits', 'M9Ks', 'DSP Elements',
#                                 'LUT-Only LCs',	'Register-Only LCs', 'LUT/Register LCs']
#         )

#         top_resources = resources[self.settings.design['top']]

#         top_resources['lut'] = top_resources['LUT-Only LCs'] + top_resources['LUT/Register LCs']
#         top_resources['ff'] = top_resources['Register-Only LCs'] + top_resources['LUT/Register LCs']

#         self.results.update(top_resources)

#         # TODO is this the most reliable timing report?
#         slacks = parse_csv(
#             self.reports_dir / 'Timing_Analyzer' / 'Multicorner_Timing_Analysis_Summary.csv',
#             id_field='Clock',
#             field_parser=lambda s: float(s.strip()),
#             id_parser=lambda s: s.strip(),
#             interesting_fields=['Setup', 'Hold']
#         )
#         worst_slacks = slacks['Worst-case Slack']
#         wns = worst_slacks['Setup']
#         whs = worst_slacks['Hold']
#         self.results['wns'] = wns
#         self.results['whs'] = whs

#         failed |= wns < 0 or whs < 0

#         for temp in ['85C', '0C']:
#             fmax = parse_csv(
#                 self.reports_dir / 'Timing_Analyzer' /
#                 f'Slow_1200mV_{temp}_Model' / f'Slow_1200mV_{temp}_Model_Fmax_Summary.csv',
#                 id_field='Clock Name',
#                 field_parser=lambda s: s.strip().split(),
#                 id_parser=lambda s: s.strip(),
#                 interesting_fields=['Fmax']
#             )
#             self.results[f'fmax_{temp}'] = fmax['clock']['Fmax']

#         return not failed


# class QuartusDse(QuartusSynth, DseFlow):
#     def run(self):
#         self.create_project()
#         # 'explore': Exploration flow to use, if not specified in --config
#         #   configuration file. Valid flows: timing_aggressive,
#         #   all_optimization_modes, timing_high_effort, seed,
#         #   area_aggressive, power_high_effort, power_aggressive
#         # 'compile_flow':  'full_compile', 'fit_sta' and 'fit_sta_asm'.
#         # 'timeout': Limit the amount of time a compute node is allowed to run. Format: hh:mm:ss
#         if 'dse' not in self.settings.flow:
#             self.fatal('`flows.quartus.dse` settings are missing!')

#         dse = self.settings.flow['dse']
#         if 'nproc' not in dse or not dse['nproc']:
#             dse['nproc'] = self.nthreads

#         script_path = self.copy_from_template(f'settings.dse',
#                                               dse=dse
#                                               )
#         self.run_process('quartus_dse',
#                          ['--use-dse-file', script_path, self.settings.design['name']],
#                          stdout_logfile='dse_stdout.log',
#                          initial_step="Running Quartus DSE",
#                          )

#     def parse_reports(self):
#         'quartus_dse_report.json'
#         pass


# DES:

# Available exploration spaces for this family are:
# "Seed Sweep"
# "Extra Effort Space"
# "Extra Effort Space for Quartus Prime Integrated Synthesis Projects"
# "Area Optimization Space"
# "Signature: Placement Effort Multiplier"
# "Custom Space"

# Valid optimization-goal options are:
# "Optimize for Speed"
# "Optimize for Area"
# "Optimize for Power"
# "Optimize for Negative Slack and Failing Paths"
# "Optimize for Average Period"
# "Optimize for Quality of Fit"


# -run-power ?

#
