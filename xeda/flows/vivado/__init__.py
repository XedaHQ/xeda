# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
import logging
from ..flow import SimFlow, Flow, SynthFlow, DebugLevel

logger = logging.getLogger()


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
        vivado_args = ['-nojournal', '-mode', 'tcl' if debug >=
                       DebugLevel.HIGHEST else 'batch', '-source', str(script_path)]
        if not debug:
            vivado_args.append('-notrace')
        return self.run_process('vivado', vivado_args, initial_step='Starting vivado',
                                stdout_logfile=self.flow_stdout_log)


class VivadoSynth(Vivado, SynthFlow):
    default_settings = {**SynthFlow.default_settings, 'fail_critical_warning': False, 'fail_timing': False}

    # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
    strategy_options = {
        "Debug": {
            "synth": "-assert -debug_log -flatten_hierarchy none -no_timing_driven -keep_equivalent_registers -no_lc -fsm_extraction off -directive RuntimeOptimized",
            "opt": "-directive RuntimeOptimized",
            "place": "-directive RuntimeOptimized",
            "route": "-directive RuntimeOptimized",
            "phys_opt": "-directive RuntimeOptimized"
        },

        "Runtime": {
            "synth": "-directive RuntimeOptimized",
            "opt": "-directive RuntimeOptimized",
            "place": "-directive RuntimeOptimized",
            "route": "-directive RuntimeOptimized",
            "phys_opt": "-directive RuntimeOptimized"
        },

        "Default": {
            "synth": "-flatten_hierarchy rebuilt -retiming -directive Default",
            "opt": "-directive ExploreWithRemap",
            "place": "-directive Default",
            "route": "-directive Default",
            "phys_opt": "-directive Default"
        },

        "Timing": {
            # or ExtraTimingOpt, ExtraPostPlacementOpt, Explore
            # very slow: AggressiveExplore
            # -mode -> default, out_of_context
            # rebuilt == full in terms of QoR?
            # -no_lc: When checked, this option turns off LUT combining
            # -max_uram 0 for ultrascale+
            "synth": "-flatten_hierarchy rebuilt -retiming -directive PerformanceOptimized -fsm_extraction one_hot -keep_equivalent_registers -no_lc -shreg_min_size 5 -resource_sharing off",
            "opt": "-directive ExploreWithRemap",
            # "place": "-directive ExtraTimingOpt",
            "place": "-directive ExtraPostPlacementOpt",
            # "route": "-directive NoTimingRelaxation",
            "route": "-directive AggressiveExplore",
            # if no directive: -placement_opt
            "phys_opt": "-directive AggressiveExplore"
        },

        "Area": {
            "synth": "-flatten_hierarchy full -directive AreaOptimized_high",
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Explore",
            "route": "-directive Explore",
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore"
        }
    }
    results_dir = 'results'
    checkpoints_dir = 'checkpoints'

    def run(self):
        generics_options = vivado_generics(self.settings.design["generics"], sim=False)
        clock_xdc_path = self.copy_from_template(f'clock.xdc')
        strategy = self.settings.flow.get('strategy', 'Default')
        logger.info(f'Using synthesis strategy: {strategy}')
        if strategy not in self.strategy_options.keys():
            self.fatal(f'Unknow strategy: {strategy}')
        options = copy.deepcopy(self.strategy_options[strategy])
        if not self.settings.flow.get('use_bram', True):
            options['synth'] += ' -max_bram 0 '
        if not self.settings.flow.get('use_dsp', True):
            options['synth'] += ' -max_dsp 0 '
        script_path = self.copy_from_template(f'{self.name}.tcl',
                                              xdc_files=[clock_xdc_path],
                                              options=options,
                                              generics_options=generics_options,
                                              results_dir=self.results_dir,
                                              checkpoints_dir=self.checkpoints_dir
                                              )
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
                          r'^\s*\|\s*Design\s+Nets\s+Matched\s*\|\s*(?P<power_nets_matched>[\-\.\w]+)\s*\|.*'
                          )

        failed = False
        forbidden_resources = ['latch', 'dsp', 'bram_tile']
        for res in forbidden_resources:
            if (self.results[res] != 0):
                logger.critical(
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
        elab_flags = f'-relax'
        script_path = self.copy_from_template(f'vivado_sim.tcl',
                                              generics_options=generics_options,
                                              analyze_flags='-relax',
                                              elab_flags=elab_flags,
                                              sim_flags='',  # '-maxdeltaid 100000 -verbose'
                                              initialize_zeros=False,
                                              vcd=self.vcd,
                                              saif=saif,
                                              debug_traces=self.args.debug >= DebugLevel.HIGHEST or self.settings.flow.get(
                                                  'debug_traces')
                                              )
        return self.run_vivado(script_path)
