# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from ..suite import Suite


class Diamond(Suite):
    name = 'diamond'
    executable = 'diamondc'
    supported_flows = ['synth']
    reports_subdir_name = ['impl_folder']

    def __init__(self, settings, args, logger):
        super().__init__(settings, args, logger,
                         impl_folder='diamond_impl',
                         impl_name='Implementation0'
                         )

    # run steps of tools and finally set self.reports_dir
    def __runflow_impl__(self, subflow):
        script_path = self.copy_from_template(f'{subflow}.tcl')
        self.run_process(self.executable, [str(script_path)])

    def parse_reports(self, flow):
        if flow == 'synth':
            self.parse_reports_synth()

    def parse_reports_synth(self):
        self.results = dict()
        reports_dir = self.reports_dir
        self.results["_reports_path"] = str(reports_dir)
        design_name = self.settings.design['name']
        impl_name = self.settings.flow['impl_name']

        period_pat = r'''^\s*Preference:\s+PERIOD\s+PORT\s+\"(?P<clock_port>\w+)\"\s+(?P<clock_period>\d+\.\d+)\s+ns.*HIGH\s+\d+\.\d+\s+ns\s*;\s*
\s*\d+\s+items\s+\S+\s+(?P<_timing_errors>\d+)\s+timing\s+errors'''
        freq_pat = r'''^\s*Preference:\s+FREQUENCY\s+PORT\s+\"(?P<clock_port>\w+)\"\s+(?P<clock_frequency>\d+\.\d+)\s+MHz\s*;\s*
\s*\d+\s+items\s+\S+\s+(?P<_timing_errors>\d+)\s+timing\s+errors'''
        self.parse_report(reports_dir / f'{design_name}_{impl_name}.twr', [period_pat, freq_pat])

        if 'clock_frequency' in self.results:
            frequency = self.results['clock_frequency']
            period = 1000.0/frequency
            self.results['clock_period'] = period

        else:
            period = self.results['clock_period']
            frequency = 1000.0/period
            self.results['clock_frequency'] = frequency

        slice_pat = r'^Device\s+utilization\s+summary:\s*.*^\s+SLICE\s+(?P<slice>\d+)\/(?P<slice_total>\d+).*^Number\s+of\s+Signals'
        time_pat = r'''Level/\s+Number\s+Worst\s+Timing\s+Worst\s+Timing\s+Run\s+NCD\s*
\s*Cost\s+\[ncd\]\s+Unrouted\s+Slack\s+Score\s+Slack\(hold\)\s+Score\(hold\)\s+Time\s+Status\s*
(\s*\-+){8}\s*
\s*(?P<_lvl_cost>\S+)\s+(?P<_ncd>\S+)\s+(?P<_num_unrouted>\d+)\s+(?P<wns>\-?\d+\.\d+)\s+(?P<_setup_score>\d+)\s+(?P<whs>\-?\d+\.\d+)\s+(?P<_hold_score>\d+)\s+(?P<_runtime>\d+(?:\:\d*)?)\s+(?P<_status>\w+)\s*$'''
        self.parse_report(reports_dir / f'{design_name}_{impl_name}.par', slice_pat, time_pat)

        #   1. Total number of LUT4s = (Number of logic LUT4s) + 2*(Number of distributed RAMs) + 2*(Number of ripple logic)
        #   2. Number of logic LUT4s does not include count of distributed RAM and ripple logic.
        mrp_pattern = r'''Design Summary\s*\-+\s*Number\s+of\s+registers:\s*(?P<ff>\d+)\s+out\s+of\s*(?P<total_ff>\d+).*
\s*Number\s+of\s+SLICEs:\s*(?P<slice_map>\d+)\s*out\s+of\s*(?P<slice_total>\d+).*
\s+SLICEs\s+as\s+RAM:\s*(?P<slice_ram>\d+)\s*out\s+of\s*(?P<slice_ram_total>\d+).*
\s+SLICEs\s+as\s+Carry:\s*(?P<slice_carry>\d+)\s+out\s+of\s+(?P<slice_carry_total>\d+).*
\s*Number\s+of\s+LUT4s:\s*(?P<lut>\d+)\s+out of\s+(?P<lut_total>\d+).*
\s+Number\s+used\s+as\s+logic\s+LUTs:\s*(?P<lut_logic>\d+)\s*
\s+Number\s+used\s+as\s+distributed\s+RAM:\s*(?P<lut_dram>\d+)\s*
\s+Number\s+used\s+as\s+ripple\s+logic:\s*(?P<lut_ripple>\d+)\s*
\s+Number\s+used\s+as\s+shift\s+registers:\s*(?P<lut_shift>\d+)\s*.*
\s*Number\s+of\s+block\s+RAMs:\s*(?P<bram>\d+)\s+out\s+of\s+(?P<bram_total>\d+).*
\s*Number\s+Of\s+Mapped\s+DSP\s+Components:\s*\-+\s*
\s+MULT18X18D\s+(?P<dsp_MULT18X18D>\d+)\s*.*
\s+MULT9X9D\s+(?P<dsp_MULT9X9D>\d+)\s*.*'''

        self.parse_report(reports_dir / f'{design_name}_{impl_name}.mrp', mrp_pattern)

        failed = False
        forbidden_resources = ['dsp_MULT18X18D', 'dsp_MULT9X9D', 'bram']
        for res in forbidden_resources:
            if (self.results[res] != 0):
                self.logger.critical(f'Map report shows {self.results[res]} use(s) of forbidden resource {res}.')
                failed = True

        failed = failed or (self.results['wns'] < 0) or (self.results['whs'] < 0) or (
            self.results['_num_unrouted'] != 0) or (self.results['_status'].lower() != 'completed') or (self.results['_timing_errors'] != 0)

        self.results['success'] = not failed
