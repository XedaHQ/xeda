# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
import sys
from xeda import design
from ..suite import Suite
from .. import parse_csv
import csv


class Quartus(Suite):
    name = 'quartus'
    executable = 'quartus_sh'
    supported_flows = ['synth']
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

        if self.run_dir.exists():
            self.logger.warn(f"Using existing run directory: {self.run_dir}")

    # run steps of tools and finally sets self.reports_dir
    def __runflow_impl__(self, flow):
        reports_dir = 'reports'
        script_path = self.copy_from_template(f'create_project.tcl')
        self.run_process(self.executable, ['-t', str(script_path)], stdout_logfile='create_project_stdout.log')
        # self.run_process(self.executable,
        #                  ['--dse', '-project', self.settings.design['name'], '-nogui', '-concurrent-compiles', '8', '-exploration-space',
        #                   "Extra Effort Space", '-optimization-goal', "Optimize for Speed", '-report-all-resource-usage', '-ignore-failed-base'],
        #                  stdout_logfile='dse_stdout.log'
        #                  )
        script_path = self.copy_from_template(f'compile.tcl',reports_dir=reports_dir)
        self.run_process(self.executable, ['-t', str(script_path)], stdout_logfile='compile_stdout.log')

        self.reports_dir = self.run_dir / reports_dir

    def parse_reports(self, flow):
        if flow == 'synth':
            self.parse_synth_reports()
        if flow == 'sim':
            self.parse_sim_reports()

    def parse_sim_reports(self):
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
