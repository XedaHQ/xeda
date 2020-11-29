# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from collections import abc
import copy
import json
import logging
import os
import math
import csv
from pathlib import Path
from typing import Union
from xml.etree import ElementTree
import html
from ...utils import unique_list
from ..flow import DesignSource, SimFlow, Flow, SynthFlow, DebugLevel

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
    return ' '.join([f"-generic{'_top' if sim else ''} {k}={vivado_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_vivado_generic(k, v, sim)])


class Vivado(Flow):
    reports_subdir_name = 'reports'

    def run_vivado(self, script_path, stdout_logfile=None):
        if stdout_logfile is None:
            stdout_logfile = f'{self.name}_stdout.log'
        debug = self.args.debug
        vivado_args = ['-nojournal', '-mode', 'tcl' if debug >=
                       DebugLevel.HIGHEST else 'batch', '-source', str(script_path)]
        # if not debug:
        #     vivado_args.append('-notrace')
        return self.run_process('vivado', vivado_args, initial_step='Starting vivado',
                                stdout_logfile=stdout_logfile)


class VivadoSynth(Vivado, SynthFlow):
    default_settings = {**SynthFlow.default_settings,
                        'fail_critical_warning': False, 'fail_timing': False}

    required_settings = {'clock_period': Union[str, int]}

    # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
    # and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug901-vivado-synthesis.pdf
    strategy_options = {
        "Debug": {
            "synth": ["-assert", "-debug_log",
                      "-flatten_hierarchy none", "-no_timing_driven", "-keep_equivalent_registers",
                      "-no_lc", "-fsm_extraction off", "-directive RuntimeOptimized"],
            "opt": "-directive RuntimeOptimized",
            "place": "-directive RuntimeOptimized",
            "place_opt": [],
            "route": "-directive RuntimeOptimized",
            "phys_opt": "-directive RuntimeOptimized"
        },

        "Runtime": {
            "synth": ["-no_timing_driven", "-directive RuntimeOptimized"],
            "opt": "-directive RuntimeOptimized",
            "place": "-directive RuntimeOptimized",
            "place_opt": [],
            # with -ultrathreads results are not reproducible!
            # OR "-no_timing_driven -ultrathreads",
            "route": ["-directive RuntimeOptimized"],
            "phys_opt": "-directive RuntimeOptimized"
        },

        "Default": {
            "synth": ["-flatten_hierarchy rebuilt", "-directive Default"],
            "opt": ["-directive ExploreWithRemap"],
            "place": ["-directive Default"],
            "place_opt": [],
            "route": ["-directive Default"],
            "phys_opt": ["-directive Default"]
        },

        "Timing": {
            # or ExtraTimingOpt, ExtraPostPlacementOpt, Explore
            # very slow: AggressiveExplore
            # -mode: default, out_of_context
            # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
            # -no_lc: When checked, this option turns off LUT combining
            # -keep_equivalent_registers -no_lc
            "synth": ["-flatten_hierarchy full",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      "-fsm_extraction one_hot",
                      "-resource_sharing off",
                      #   "-no_lc",
                      "-shreg_min_size 5",
                      #   "-keep_equivalent_registers "
                      ],
            "opt": ["-directive ExploreWithRemap"],
            # "place": "-directive ExtraTimingOpt",
            "place": ["-directive ExtraPostPlacementOpt"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
                          '-dsp_register_opt', '-bram_power_opt'],
            # "route": "-directive NoTimingRelaxation",
            "route": ["-directive AggressiveExplore"],
            # if no directive: -placement_opt
            "phys_opt": ["-directive AggressiveExplore"]
        },
        "Timing2": {
            # or ExtraTimingOpt, ExtraPostPlacementOpt, Explore
            # very slow: AggressiveExplore
            # -mode: default, out_of_context
            # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
            # -no_lc: When checked, this option turns off LUT combining
            # -keep_equivalent_registers -no_lc
            "synth": ["-flatten_hierarchy rebuilt",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      #   "-fsm_extraction one_hot",
                      #   "-resource_sharing off",
                      #   "-no_lc",
                      "-shreg_min_size 5",
                      "-keep_equivalent_registers ",
                      ],
            "opt": ["-directive ExploreWithRemap"],
            # "place": "-directive ExtraTimingOpt",
            "place": ["-directive ExtraPostPlacementOpt"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
                          '-dsp_register_opt', '-bram_power_opt'],
            # "route": "-directive NoTimingRelaxation",
            "route": ["-directive AggressiveExplore"],
            # if no directive: -placement_opt
            "phys_opt": ["-directive AggressiveExplore"]
        },

        "Area": {
            "synth": ["-flatten_hierarchy full", "-directive AreaOptimized_high"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Explore",
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
                          '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            "route": "-directive Explore",
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore"
        }
    }
    results_dir = 'results'
    checkpoints_dir = 'checkpoints'

    def run(self):
        rtl_settings = self.settings.design["rtl"]
        flow_settings = self.settings.flow
        generics_options = vivado_generics(
            rtl_settings.get("generics", {}), sim=False)

        input_delay = flow_settings.get('input_delay', 0)
        output_delay = flow_settings.get('output_delay', 0)

        clock_xdc_path = self.copy_from_template(f'clock.xdc',
                                                 input_delay=input_delay, output_delay=output_delay,
                                                 )

        strategy = flow_settings.get('strategy', 'Default')
        if isinstance(strategy, abc.Mapping):
            options = copy.deepcopy(strategy)
        else:
            logger.info(f'Using synthesis strategy: {strategy}')
            if strategy not in self.strategy_options.keys():
                self.fatal(f'Unknown strategy: {strategy}')
            options = copy.deepcopy(self.strategy_options[strategy])
        for k, v in options.items():
            if isinstance(v, str):
                options[k] = v.split()
        if not self.settings.flow.get('allow_brams', True):
            # -max_uram 0 for ultrascale+
            options['synth'].append('-max_bram 0')
        if not flow_settings.get('allow_dsps', True):
            options['synth'].append('-max_dsp 0')

        # to strings
        for k, v in options.items():
            options[k] = ' '.join(v)
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

        report_stage = 'post_route'
        reports_dir = reports_dir / report_stage

        fields = {'lut': 'Slice LUTs', 'ff': 'Register as Flip Flop',
                  'latch': 'Register as Latch'}
        hrule_pat = r'^\s*(?:\+\-+)+\+\s*$'
        slice_logic_pat = r'^\S*\d+\.\s*Slice Logic\s*\-+\s*' + \
            hrule_pat + r'.*' + hrule_pat + r'.*'
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + \
                r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'

        slice_logic_pat += hrule_pat + r".*" + \
            r'^\S*\d+\.\s*Slice\s+Logic\s+Distribution\s*\-+\s*' + \
            hrule_pat + r'.*' + hrule_pat + r'.*'

        fields = {'slice': 'Slices?', 'lut_logic': 'LUT as Logic ',
                  'lut_mem': 'LUT as Memory'}
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + \
                r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'

        slice_logic_pat += hrule_pat + r".*" + r'^\S*\d+\.\s*Memory\s*\-+\s*' + \
            hrule_pat + r'.*' + hrule_pat + r'.*'
        fields = {'bram_tile': 'Block RAM Tile',
                  'bram_RAMB36': 'RAMB36[^\|]+', 'bram_RAMB18': 'RAMB18'}
        for fname, fregex in fields.items():
            slice_logic_pat += r'^\s*\|\s*' + fregex + \
                r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'
        slice_logic_pat += hrule_pat + r".*" + r'^\S*\d+\.\s*DSP\s*\-+\s*' + \
            hrule_pat + r'.*' + hrule_pat + r'.*'

        fname, fregex = ('dsp', 'DSPs')
        slice_logic_pat += r'^\s*\|\s*' + fregex + \
            r'\s*\|\s*' + f'(?P<{fname}>\\d+)' + r'\s*\|.*'
        self.parse_report(reports_dir / 'utilization.rpt', slice_logic_pat)

        self.parse_report(reports_dir / 'timing_summary.rpt',
                          r'Design\s+Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+TPWS Failing Endpoints\s+TPWS Total Endpoints\s*' +
                          r'\s*(?:\-+\s+)+' +
                          r'(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<_tns>\-?\d+(?:\.\d+)?)\s+(?P<_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<_tns_total_endpoints>\-?\d+(?:\.\d+)?)\s+'
                          r'(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<_ths>\-?\d+(?:\.\d+)?)\s+(?P<_ths_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<_ths_total_endpoints>\-?\d+(?:\.\d+)?)\s+',
                          r'Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<clock_frequency>\d+(?:\.\d+)?)'
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
        flow_settings = self.settings.flow
        tb_settings = self.settings.design["tb"]
        generics = tb_settings.get("generics", {})
        saif = flow_settings.get('saif')

        elab_flags = flow_settings.get('elab_flags')
        if not elab_flags:
            elab_flags = ['-nospecify',
                          '-notimingchecks', '-relax', '-maxdelay']

        elab_debug = flow_settings.get('elab_debug')
        run_configs = flow_settings.get('run_configs')
        if not elab_debug and (self.args.debug or saif or self.vcd):
            elab_debug = "typical"
        if elab_debug:
            elab_flags.append(f'-debug {elab_debug}')
        
        if not run_configs:
            run_configs = [dict(saif=saif, generics=generics)]
        else:
            for rc in run_configs:
                # merge
                rc['generics'] = {**generics, **rc['generics']}
                if saif and not 'saif' in rc:
                    rc['saif'] = saif

        tb_uut = tb_settings.get('uut')
        sdf = flow_settings.get('sdf')
        if sdf:
            if not isinstance(sdf, list):
                sdf = [sdf]
            for s in sdf:
                if isinstance(s, str):
                    s = {"file": s}
                root = s.get("root", tb_uut)
                assert root, "neither SDF root nor tb.uut are provided"
                elab_flags.append(
                    f'-sdf{s.get("delay", "max")} {root}={s["file"]}')

        libraries = flow_settings.get('libraries')
        if libraries:
            elab_flags.extend([f'-L {l}' for l in libraries])

        elab_optimize = flow_settings.get('elab_optimize', '-O3')
        if elab_optimize and elab_optimize not in elab_flags: # FIXME none of -Ox in elab_flags
            elab_flags.append(elab_optimize)

        sim_tops = tb_settings['top']
        tb_top = sim_tops
        if isinstance(sim_tops, list):
            tb_top = sim_tops[0]
        else:
            sim_tops = [sim_tops]

        script_path = self.copy_from_template(f'vivado_sim.tcl',
                                              analyze_flags=' '.join(flow_settings.get('analyze_flags', ['-relax'])),
                                              elab_flags=' '.join(unique_list(elab_flags)),
                                              run_configs=run_configs,
                                              sim_flags=' '.join(flow_settings.get('sim_flags', [])),
                                              initialize_zeros=False,
                                              vcd=self.vcd,
                                              sim_tops=sim_tops,
                                              tb_top=tb_top,
                                              lib_name='work',
                                              sim_sources=self.sim_sources,
                                              debug_traces=self.args.debug >= DebugLevel.HIGHEST or self.settings.flow.get(
                                                  'debug_traces')
                                              )
        return self.run_vivado(script_path)


class VivadoPostsynthSim(VivadoSim):
    depends_on = {VivadoSynth: {'rtl.sources': ['results/impl_timesim.v']}}

    def run(self):
        design_settings = self.settings.design
        tb_settings = design_settings['tb']
        flow_settings = self.settings.flow
        top = tb_settings['top']
        if isinstance(top, str):
            top = [top]
        if not 'glbl' in top:
            top.append('glbl')
        tb_settings['top'] = top

        if 'libraries' not in flow_settings:
            flow_settings['libraries'] = []
        if 'simprims_ver' not in flow_settings['libraries']:
            flow_settings['libraries'].append('simprims_ver')

        netlist_base = os.path.splitext(
            str(design_settings['rtl']['sources'][0]))[0]
        flow_settings['sdf'] = {'file': netlist_base + '.sdf'}

        clock_period_ps_generic = tb_settings.get('clock_period_ps_generic', 'G_PERIOD_PS') ### FIXME
        if clock_period_ps_generic:
            clock_ps = math.floor(self.settings.flow_depends['vivado_synth']['clock_period'] * 1000)
            tb_settings['generics'][clock_period_ps_generic] = clock_ps
            for rc in flow_settings.get('run_configs', []):
                rc['generics'][clock_period_ps_generic] = clock_ps

        flow_settings['elab_flags'] = ['-relax', '-maxdelay', '-transport_int_delays',
                                       '-pulse_r 0', '-pulse_int_r 0', '-pulse_e 0', '-pulse_int_e 0']

        VivadoSim.run(self)


class VivadoPower(VivadoPostsynthSim):
    # depends_on = {VivadoPostsynthSim: {'rtl.sources': ['results/impl_timesim.v']}}
    def run(self):
        flow_settings = self.settings.flow

        if flow_settings.get('prerun_time') is None:
            flow_settings['prerun_time'] = 100 + math.floor(self.settings.flow_depends['vivado_synth']['clock_period'] * 4) - 1
        saif_file = 'impl_timing.saif'
        self.power_report_filename = 'power_impl_timing.xml'
        flow_settings['saif'] = saif_file
        VivadoPostsynthSim.run(self) # run simulation FIXME implement through flow dependency system

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                        run_configs=[dict(saif=saif_file, report=self.power_report_filename)],
                                        checkpoint=os.path.relpath(str(self.run_path / 'vivado_synth' / VivadoSynth.checkpoints_dir / 'post_route.dcp'), Path(self.flow_run_dir))
                                        )

        return self.run_vivado(script_path, stdout_logfile='vivado_postsynth_power_stdout.log')

    def parse_power_report(self, report_xml):
        tree = ElementTree.parse(report_xml)

        results = {}
        components = {}

        for tablerow in tree.findall("./section[@title='Summary']/table/tablerow"):
            tablecells = tablerow.findall('tablecell')
            key, value = (html.unescape(x.attrib['contents']).strip() for x in tablecells)
            results[key] = value


        for tablerow in tree.findall("./section[@title='Summary']/section[@title='On-Chip Components']/table/tablerow"):
            tablecells = tablerow.findall('tablecell')
            if len(tablecells) >= 2:
                contents = [html.unescape(x.attrib['contents']).strip() for x in tablecells]
                key = contents[0]
                value = contents[1]
                components[key] = value
        results['Components Power'] = components

        return results

    def parse_reports(self):
        report_xml = self.flow_run_dir / self.power_report_filename

        results = self.parse_power_report(report_xml)

        self.results['success'] = True
        self.results.update(**results)


# TODO move to an LWC plugin that hooks into VivadoPower
class VivadoPowerLwc(VivadoPower):
    def run(self):
        flow_settings = self.settings.flow
        tb_settings = self.settings.design['tb']
        lwc_settings = self.settings.design.get('lwc')

        if flow_settings.get('prerun_time') is None:
            flow_settings['prerun_time'] = 100 + math.floor(self.settings.flow_depends['vivado_synth']['clock_period'] * 4) - 1
        
        power_tvs = flow_settings.get('power_tvs')
        if not power_tvs:
            power_tvs = ['enc_16_0', 'enc_0_16', 'enc_1536_0', 'enc_0_1536', 'dec_16_0', 'dec_0_16', 'dec_1536_0', 'dec_0_1536']
            if lwc_settings and lwc_settings.get('supports_hash'):
                power_tvs.extend(['hash_16', 'hash_1536'])

        lwc_settings = self.settings.design.get('lwc')
        lwc_variant = 'v1'
        if lwc_settings:
            lwc_variant = lwc_settings.get('variant', lwc_variant)
        power_tvs_root = os.path.join('KAT_GMU'/ lwc_variant)

        def pow_tv_run_config(tv_sub):
            tv_generics = copy.deepcopy(tb_settings.get('generics', {}))
            tv_generics['G_MAX_FAILURES'] = 1
            tv_generics['G_TEST_MODE'] = 0
            for t in ['pdi', 'sdi', 'do']:
                tv_generics[f'G_FNAME_{t.upper()}'] = DesignSource(os.path.join(power_tvs_root, tv_sub, f'{t}.txt'))
            return dict(generics=tv_generics, saif=f'{tv_sub}.saif', report=f'{tv_sub}.xml', name=tv_sub)


        run_configs = [pow_tv_run_config(tv) for tv in  power_tvs]
        flow_settings['run_configs'] = run_configs
        flow_settings['elab_debug'] = 'typical'

        if not flow_settings.get('skip_simulation'):
            VivadoPostsynthSim.run(self) # run simulation FIXME implement through dependency system

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                        run_configs=run_configs,
                                        checkpoint=os.path.relpath(str(self.run_path / 'vivado_synth' / VivadoSynth.checkpoints_dir / 'post_route.dcp'), Path(self.flow_run_dir))
                                        )

        self.run_vivado(script_path, stdout_logfile='vivado_postsynth_power_stdout.log')

    def parse_reports(self):
        design_name = self.settings.design['name']
                
        clock_period = self.settings.flow_depends['vivado_synth']['clock_period']
        freq = math.floor(1000 / clock_period)
        fields = {'Design': design_name, 'Frequency (MHz)': freq, 'Static': None}

        for rc in self.settings.flow['run_configs']:
            name = rc['name']
            report_xml = self.flow_run_dir / rc['report']

            results = self.parse_power_report(report_xml)

            assert results['Design Nets Matched'].startswith('100%')
            assert results['Confidence Level'] == 'High'
            static = results['Device Static (W)']
            if fields['Static'] is None:
                fields['Static'] = static
            else:
                if fields['Static'] != static:
                    logger.warning(f"Static power for {name} {static} is different from previous test vectors ({fields['Static']})")
                    fields[f'Static:{name}'] = static
            fields[name] = results['Dynamic (W)']
            self.results[name] = results

        # copy synthesis results for reference
        # FIXME dependency results/paths
        with open(self.run_path / 'vivado_synth' / 'vivado_synth_results.json') as synth_results_file:
            synth_results = json.load(synth_results_file)

        fields['LUT'] = synth_results['lut']
        fields['FF'] = synth_results['ff']
        fields['Slice'] = synth_results['slice']
        fields['LUT RAM'] = synth_results['lut_mem']

        csv_path = Path.cwd() / f"VivadoPowerLwc_{design_name}_{freq}MHz.csv"
        with open(csv_path, "w") as csv_file:
            writer = csv.DictWriter(csv_file, fields.keys())
            writer.writeheader()
            writer.writerow(fields)
        
        logger.info(f"Power results written to {csv_path}")

        self.results['success'] = True
