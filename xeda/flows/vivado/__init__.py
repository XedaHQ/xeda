# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from ..flow import SimFlow, Flow, SynthFlow, DebugLevel


def supported_vivado_generic(k, v, sim):
    if sim:
        return True
    if isinstance(v, int):
        return True
    if isinstance(v, bool):
        return True
    v = str(v)
    return (v.isnumeric() or (v.strip().lower() in {'true', 'false'}))


def vivado_gen_convert(k, x, sim):
    if sim:
        return x
    xl = str(x).strip().lower()
    if xl == 'false':
        return "1\\'b0"
    if xl == 'true':
        return "1\\'b1"
    return x


def vivado_generics(kvdict, sim):
    return ' '.join([f"-generic {k}={vivado_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_vivado_generic(k, v, sim)])


class Vivado(Flow):
    reports_subdir_name = 'reports'

    def run_vivado(self, script_path):
        debug = self.args.debug
        vivado_args = ['-nojournal', '-mode', 'tcl' if debug >= DebugLevel.HIGHEST else 'batch', '-source', str(script_path)]
        if not debug:
            vivado_args.append('-notrace')
        return self.run_process('vivado', vivado_args, initial_step='Starting vivado',
                                stdout_logfile=self.flow_stdout_log)


class VivadoSynth(Vivado, SynthFlow):
    default_settings = {**SynthFlow.default_settings, 'fail_critical_warning': False, 'fail_timing': False}

    def run(self):
        self.settings.flow['generics_options'] = vivado_generics(self.settings.design["generics"], sim=False)
        clock_xdc_path = self.copy_from_template(f'clock.xdc')
        script_path = self.copy_from_template(f'{self.name}.tcl', xdc_files=[clock_xdc_path])
        return self.run_vivado(script_path)

    def parse_reports(self):
        reports_dir = self.reports_dir

        # TODO
        report_stage = 'post_route'
        reports_dir = reports_dir / report_stage

        fields = {'lut': 'Slice LUTs', 'ff': 'Register as Flip Flop', 'latch': 'Register as Latch'}
        hrule_pat = r'^\s*(?:\+\-+)+\+\s*$'
        slice_logic_pat = r'^\S*\d+\.\s*Slice Logic\s*\-+\s*' + hrule_pat + r'.*' + hrule_pat + r'.*'
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'

        slice_logic_pat += hrule_pat + r".*" + r'^\S*\d+\.\s*Slice\s+Logic\s+Distribution\s*\-+\s*' + hrule_pat + r'.*' + hrule_pat + r'.*'

        fields = {'slice': 'Slices?', 'lut_logic': 'LUT as Logic ', 'lut_mem': 'LUT as Memory'}
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'

        slice_logic_pat += hrule_pat + r".*" + r'^\S*\d+\.\s*Memory\s*\-+\s*' + hrule_pat + r'.*' + hrule_pat + r'.*'
        fields = {'bram_tile': 'Block RAM Tile', 'bram_RAMB36': 'RAMB36[^\|]+', 'bram_RAMB18': 'RAMB18'}
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'
        slice_logic_pat += hrule_pat + r".*" + r'^\S*\d+\.\s*DSP\s*\-+\s*' + hrule_pat + r'.*' + hrule_pat + r'.*'

        fname, fregex = ('dsp', 'DSPs')
        slice_logic_pat += r'^\s*\|\s*' + fregex + r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'
        self.parse_report(reports_dir / 'utilization.rpt', slice_logic_pat)

        self.parse_report(reports_dir / 'timing_summary.rpt',
                          r'Design\s+Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+TPWS Failing Endpoints\s+TPWS Total Endpoints\s*' +
                          r'\s*(?:\-+\s+)+' +
                          r'(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<tns>\-?\d+(?:\.\d+)?)\s+(?P<_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<tns_total_endpoints>\-?\d+(?:\.\d+)?)\s+'
                          r'(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<ths>\-?\d+(?:\.\d+)?)\s+(?P<ths_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<ths_total_endpoints>\-?\d+(?:\.\d+)?)\s+',
                          r'Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<frequency>\d+(?:\.\d+)?)'
                          )

        self.parse_report(reports_dir / 'power.rpt',
                          r'^\s*\|\s*Total\s+On-Chip\s+Power\s+\(W\)\s*\|\s*(?P<power_total>[\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*Dynamic\s*\(W\)\s*\|\s*(?P<power_dynamic> [\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*Device\s+Static\s+\(W\)\s*\|\s*(?P<power_static>[\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*Confidence\s+Level\s*\|\s*(?P<power_confidence_level>[\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*Design\s+Nets\s+Matched\s*\|\s*(?P<power_nets_matched>[\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*Clocks\s*\|\s*(?P<power_clocks>[\-\.\w]+)\s*\|.*' +
                          r'^\s*\|\s*I\/O\s*\|\s*(?P<power_io>[\-\.\w]+)\s*\|.*'
                          )

        failed = False
        forbidden_resources = ['latch', 'dsp', 'bram_tile']
        for res in forbidden_resources:
            if (self.results[res] != 0):
                self.logger.critical(
                    f'{report_stage} reports show {self.results[res]} use(s) of forbidden resource {res}.')
                failed = True

        # TODO better fail analysis for vivado
        failed = failed or (self.results['wns'] < 0) or (self.results['whs'] < 0) or (
            self.results['_failing_endpoints'] != 0)

        self.results['success'] = not failed


class VivadoSim(Vivado, SimFlow):
    def run(self):
        generics_options = vivado_generics(self.settings.design["tb_generics"], sim=True)
        saif = self.settings.flow.get('saif')
        script_path = self.copy_from_template(f'vivado_sim.tcl',
                                              generics_options=generics_options,
                                              analyze_flags='-relax',
                                              elab_flags=f'-relax -O3 -mt {self.settings.nthreads}',
                                              sim_flags='',  # '-maxdeltaid 100000 -verbose'
                                              initialize_zeros=False,
                                              vcd=self.vcd,
                                              saif=saif,
                                              debug_traces=self.args.debug >= DebugLevel.HIGHEST or self.settings.flow.get(
                                                  'debug_traces')
                                              )
        return self.run_vivado(script_path)
