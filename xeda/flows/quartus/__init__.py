# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
import sys
from xeda import design
from ..suite import Suite
from .. import parse_csv
import csv


class Quartus(Suite):
    name = 'quartus'
    supported_flows = ['synth', 'dse']
    required_settings = {'synth': {'clock_period': float, 'fpga_part': str}}

    def __init__(self, settings, args, logger):
        # def supported_quartus_generic(k, v, sim):
        #     if sim:
        #         return True
        #     if isinstance(v, int):
        #         return True
        #     if isinstance(v, bool):
        #         return True
        #     v = str(v)
        #     return (v.isnumeric() or (v.strip().lower() in {'true', 'false'}))

        # def quartus_gen_convert(k, x, sim):
        #     if sim:
        #         if isinstance(x, dict) and "file" in x:
        #             p = x["file"]
        #             assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
        #             x = self.conv_to_relative_path(p.strip())
        #             self.logger.info(f'Converting generic `{k}` marked as `file`: {p} -> {x}')
        #     xl = str(x).strip().lower()
        #     if xl == 'false':
        #         return "1\\'b0"
        #     if xl == 'true':
        #         return "1\\'b1"
        #     return x

        # def quartus_generics(kvdict, sim):
        #     return ' '.join([f"-generic {k}={quartus_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_quartus_generic(k, v, sim)])

        super().__init__(settings, args, logger,
                         fail_critical_warning=args.command != "fmax",
                         fail_timing=False
                         )

        # self.settings.flow['generics_options'] = quartus_generics(self.settings.design["generics"], sim=False)
        # self.settings.flow['tb_generics_options'] = quartus_generics(self.settings.design["tb_generics"], sim=True)

    def __runflow_impl__(self, flow):
        project_settings = None
        if 'project_settings' in self.settings.flow:
            project_settings = self.settings.flow['project_settings']
        # TODO manage settings
        if not project_settings:
            project_settings = {
                # see https://www.intel.com/content/www/us/en/programmable/documentation/zpr1513988353912.html
                # https://www.intel.com/content/www/us/en/programmable/quartushelp/current/index.htm
                # BALANCED "HIGH PERFORMANCE EFFORT" AGGRESSIVE PERFORMANCE
                # "High Performance with Maximum Placement Effort"
                # "Superior Performance"
                # "Superior Performance with Maximum Placement Effort"
                # "Aggressive Area"
                # "High Placement Routability Effort"
                # "High Packing Routability Effort"
                # "Optimize Netlist for Routability
                # "High Power Effort"
                "OPTIMIZATION_MODE": "HIGH PERFORMANCE EFFORT",
                "REMOVE_REDUNDANT_LOGIC_CELLS": "ON",
                "AUTO_RESOURCE_SHARING": "ON",
                "ALLOW_REGISTER_RETIMING": "ON",

                "SYNTH_GATED_CLOCK_CONVERSION": "ON",


                # faster: AUTO FIT, fastest: FAST_FIT
                "FITTER_EFFORT": "STANDARD FIT",

                # AREA, SPEED, BALANCED
                "STRATIX_OPTIMIZATION_TECHNIQUE": "SPEED",
                "CYCLONE_OPTIMIZATION_TECHNIQUE": "SPEED",

                # see https://www.intel.com/content/www/us/en/programmable/documentation/rbb1513988527943.html
                # The Router Effort Multiplier controls how quickly the router tries to find a valid solution. The default value is 1.0 and legal values must be greater than 0.
                # Numbers higher than 1 help designs that are difficult to route by increasing the routing effort.
                # Numbers closer to 0 (for example, 0.1) can reduce router runtime, but usually reduce routing quality slightly.
                # Experimental evidence shows that a multiplier of 3.0 reduces overall wire usage by approximately 2%. Using a Router Effort Multiplier higher than the default value can benefit designs with complex datapaths with more than five levels of logic. However, congestion in a design is primarily due to placement, and increasing the Router Effort Multiplier does not necessarily reduce congestion.
                # Note: Any Router Effort Multiplier value greater than 4 only increases by 10% for every additional 1. For example, a value of 10 is actually 4.6.
                "PLACEMENT_EFFORT_MULTIPLIER": 3.0,
                "ROUTER_EFFORT_MULTIPLIER": 3.0,

                # NORMAL, MINIMUM,MAXIMUM
                "ROUTER_TIMING_OPTIMIZATION_LEVEL": "MAXIMUM",

                # ALWAYS, AUTOMATICALLY, NEVER
                "FINAL_PLACEMENT_OPTIMIZATION": "ALWAYS",


                # "PHYSICAL_SYNTHESIS_COMBO_LOGIC_FOR_AREA": "ON",

                # ?
                # "ADV_NETLIST_OPT_SYNTH_GATE_RETIME": "ON",
                # ?
                # "ADV_NETLIST_OPT_SYNTH_WYSIWYG_REMAP": "ON",

                "AUTO_PACKED_REGISTERS_STRATIX": "OFF",
                "AUTO_PACKED_REGISTERS_CYCLONE": "OFF",
                "PHYSICAL_SYNTHESIS_COMBO_LOGIC": "ON",
                "PHYSICAL_SYNTHESIS_REGISTER_DUPLICATION": "ON",
                "PHYSICAL_SYNTHESIS_REGISTER_RETIMING": "ON",
                "PHYSICAL_SYNTHESIS_EFFORT": "EXTRA",
                "AUTO_DSP_RECOGNITION": "OFF",

                #NORMAL, OFF, EXTRA_EFFORT
                # "OPTIMIZE_POWER_DURING_SYNTHESIS": "NORMAL",

                # Used during placement. Use of a higher value increases compilation time, but may increase the quality of placement.
                "INNER_NUM": 8,
            }

        clock_sdc_path = self.copy_from_template(f'clock.sdc')
        script_path = self.copy_from_template(
            f'create_project.tcl',
            sdc_files=[clock_sdc_path],
            project_settings=project_settings
        )
        self.run_process('quartus_sh', ['-t', str(script_path)], stdout_logfile='create_project_stdout.log')

        # self.run_process('quartus_sh',
        #                  ['--dse', '-project', self.settings.design['name'], '-nogui', '-concurrent-compiles', '8', '-exploration-space',
        #                   "Extra Effort Space", '-optimization-goal', "Optimize for Speed", '-report-all-resource-usage', '-ignore-failed-base'],
        #                  stdout_logfile='dse_stdout.log'
        #                  )

        if flow == 'dse':
            # TODO Check correspondance of settings hash vs desgin settings
            # 'explore': Exploration flow to use, if not specified in --config
            #   configuration file. Valid flows: timing_aggressive,
            #   all_optimization_modes, timing_high_effort, seed,
            #   area_aggressive, power_high_effort, power_aggressive
            # 'compile_flow':  'full_compile', 'fit_sta' and 'fit_sta_asm'.
            # 'timeout': Limit the amount of time a compute node is allowed to run. Format: hh:mm:ss
            if 'dse' not in self.settings.flow:
                self.fatal('`flows.quartus.dse` settings are missing!')

            dse = self.settings.flow['dse']
            if 'nproc' not in dse or not dse['nproc']:
                dse['nproc'] = self.nthreads

            script_path = self.copy_from_template(f'settings.dse',
                                                  dse=dse
                                                  )
            self.run_process('quartus_dse',
                             ['--use-dse-file', script_path, self.settings.design['name']],
                             stdout_logfile='dse_stdout.log',
                             initial_step="Running Quartus DSE",
                             )
        elif flow == 'synth':

            script_path = self.copy_from_template(
                f'compile.tcl',
            )
            self.run_process('quartus_sh',
                             ['-t', str(script_path)],
                             stdout_logfile='compile_stdout.log'
                             )
        else:
            sys.exit('unsupported flow')

    def parse_reports(self, flow):
        if flow == 'synth':
            self.parse_synth_reports()
        if flow == 'dse':
            self.parse_dse_reports()
        # if flow == 'sim':
        #     self.parse_sim_reports()

    def parse_dse_reports(self):
        'quartus_dse_report.json'
        pass

    def parse_synth_reports(self):
        failed = False

        resources = parse_csv(
            self.reports_dir / 'Fitter.Resource_Section.Fitter_Resource_Utilization_by_Entity.csv',
            id_field='Compilation Hierarchy Node',
            field_parser=lambda s: int(s.split()[0]),
            id_parser=lambda s: s.strip()[1:],
            interesting_fields=['Logic Cells', 'Memory Bits', 'M9Ks', 'DSP Elements',
                                'LUT-Only LCs',	'Register-Only LCs', 'LUT/Register LCs']
        )

        top_resources = resources[self.settings.design['top']]

        top_resources['lut'] = top_resources['LUT-Only LCs'] + top_resources['LUT/Register LCs']
        top_resources['ff'] = top_resources['Register-Only LCs'] + top_resources['LUT/Register LCs']

        self.results.update(top_resources)

        # TODO is this the most reliable timing report?
        slacks = parse_csv(
            self.reports_dir / 'Timing_Analyzer.Multicorner_Timing_Analysis_Summary.csv',
            id_field='Clock',
            field_parser=lambda s: float(s.strip()),
            id_parser=lambda s: s.strip(),
            interesting_fields=['Setup', 'Hold']
        )
        worst_slacks = slacks['Worst-case Slack']
        wns = worst_slacks['Setup']
        whs = worst_slacks['Hold']
        self.results['wns'] = wns
        self.results['whs'] = whs

        failed |= wns < 0 or whs < 0

        for temp in ['85C', '0C']:
            fmax = parse_csv(
                self.reports_dir /
                f'Timing_Analyzer.Slow_1200mV_{temp}_Model.Slow_1200mV_{temp}_Model_Fmax_Summary.csv',
                id_field='Clock Name',
                field_parser=lambda s: s.strip().split(),
                id_parser=lambda s: s.strip(),
                interesting_fields=['Fmax']
            )
            self.results[f'fmax_{temp}'] = fmax['clock']['Fmax']

        self.results['success'] = not failed


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
