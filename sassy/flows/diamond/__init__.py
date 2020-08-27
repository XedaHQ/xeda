# © 2020 [Kamyar Mohajerani](malto:kamyar@ieee.org)
 
from .. import Suite


class Diamond(Suite):
    name = 'diamond'
    executable = 'diamondc'
    supported_flows = ['synth']

    def __init__(self, settings, args, logger):
        super().__init__(settings, args, logger, impl_folder='diamond_impl', impl_name='Implementation0')
        # Note: self.reports_dir will be set after run

    ## run steps of tools and finally set self.reports_dir
    def __runflow_impl__(self, subflow):
        script_path = self.copy_from_template(f'{subflow}.tcl')
        self.run_process(self.executable, [str(script_path)])
        self.reports_dir = self.run_dir / self.settings.flow['impl_folder']

    def parse_reports(self):
        self.results = dict()
        reports_dir = self.reports_dir
        self.results["_reports_path"] = str(reports_dir)
        impl_name = self.settings.flow['impl_name']

        period_pat = r'''^\s*Preference:\s+PERIOD\s+PORT\s+\"(?P<clock_port>\w+)\"\s+(?P<clock_period>\d+\.\d+)\s+ns.*HIGH\s+\d+\.\d+\s+ns\s*;\s*
\s*\d+\s+items\s+\S+\s+(?P<_timing_errors>\d+)\s+timing\s+errors'''
        freq_pat = r'''^\s*Preference:\s+FREQUENCY\s+PORT\s+\"(?P<clock_port>\w+)\"\s+(?P<clock_frequency>\d+\.\d+)\s+MHz\s*;\s*
\s*\d+\s+items\s+\S+\s+(?P<_timing_errors>\d+)\s+timing\s+errors'''
        self.parse_report(reports_dir / f'xoodyak_{impl_name}.twr', [period_pat, freq_pat])

        if 'clock_frequency' in self.results:
            frequency = self.results['clock_frequency']
            period = 1000.0/frequency
            self.results['clock_period'] = period

        else:
            period = self.results['clock_period']
            frequency = 1000.0/period
            self.results['clock_frequency'] = frequency

        slice_pat = r'^Device utilization summary:\s*.*^\s+SLICE\s+(?P<slices>\d+)\/(?P<total_slices>\d+).*^Number\s+of\s+Signals'
        time_pat = r'''Level/\s+Number\s+Worst\s+Timing\s+Worst\s+Timing\s+Run\s+NCD\s*
\s*Cost\s+\[ncd\]\s+Unrouted\s+Slack\s+Score\s+Slack\(hold\)\s+Score\(hold\)\s+Time\s+Status\s*
(\s*\-+){8}\s*
\s*(?P<_lvl_cost>\S+)\s+(?P<_ncd>\S+)\s+(?P<_num_unrouted>\d+)\s+(?P<wns>\-?\d+\.\d+)\s+(?P<_setup_score>\d+)\s+(?P<wnhs>\-?\d+\.\d+)\s+(?P<_hold_score>\d+)\s+(?P<_runtime>\d+(?:\:\d*)?)\s+(?P<_status>\w+)\s*$'''
        self.parse_report(reports_dir / f'xoodyak_{impl_name}.par', slice_pat, time_pat)

        failed = (self.results['wns'] < 0) or (self.results['wnhs'] < 0) or (
            self.results['_num_unrouted'] != 0) or (self.results['_status'].lower() != 'completed') or (self.results['_timing_errors'] != 0)
        self.results['success'] = not failed


