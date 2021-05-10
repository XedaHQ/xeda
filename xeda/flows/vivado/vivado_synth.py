
# Xeda Vivado Synthtesis flow
# ©2021 Kamyar Mohajerani and contributors

from collections import abc
import copy
import logging
from typing import Union
from ..flow import SynthFlow
from .vivado import Vivado, vivado_generics

logger = logging.getLogger()


class VivadoSynth(Vivado, SynthFlow):
    default_settings = {**SynthFlow.default_settings, 'nthreads': 4,
                        'fail_critical_warning': False, 'fail_timing': False,
                        'optimize_power': False, 'optimize_power_postplace': False}

    required_settings = {'clock_period': Union[str, int]}

    synth_output_dir = 'output'
    checkpoints_dir = 'checkpoints'

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
            # -mode: default, out_of_context
            # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
            # -no_lc: When checked, this option turns off LUT combining
            # -keep_equivalent_registers -no_lc
            "synth": ["-flatten_hierarchy rebuilt",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      "-fsm_extraction one_hot",
                      #   "-resource_sharing off",
                      #   "-no_lc",
                      "-shreg_min_size 5",
                      #   "-keep_equivalent_registers "
                      ],
            "opt": ["-directive ExploreWithRemap"],
            "place": ["-directive ExtraPostPlacementOpt"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt'],
            "phys_opt": ["-directive AggressiveExplore"],
            "place_opt2": ["-directive Explore"],
            # "route": "-directive NoTimingRelaxation",
            "route": ["-directive AggressiveExplore"],
        },
        "ExtraTimingCongestion": {
            "synth": ["-flatten_hierarchy full",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      "-fsm_extraction one_hot",
                      "-resource_sharing off",
                      "-shreg_min_size 10",
                      "-keep_equivalent_registers",
                      ],
            "opt": ["-directive ExploreWithRemap"],
            "place": ["-directive AltSpreadLogic_high"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-remap', '-muxf_remap', '-aggressive_remap', '-shift_register_opt'],
            "place_opt2": ["-directive Explore"],
            "phys_opt": ["-directive AggressiveExplore"],
            "route": ["-directive AlternateCLBRouting"],
        },
        "ExtraTiming": {
            "synth": ["-flatten_hierarchy full",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      "-fsm_extraction one_hot",
                      "-resource_sharing off",
                      #   "-no_lc",
                      "-shreg_min_size 10",
                      "-keep_equivalent_registers",
                      ],
            "opt": ["-directive ExploreWithRemap"],
            "place": "-directive ExtraTimingOpt",
            "place_opt": ['-retarget', '-propconst', '-sweep', '-muxf_remap', '-aggressive_remap', '-shift_register_opt'],
            "place_opt2": ["-directive Explore"],
            "phys_opt": ["-directive AggressiveExplore"],
            "route": ["-directive NoTimingRelaxation"],
        },
        "ExtraTimingAltRouting": {
            # -mode: default, out_of_context
            # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
            # -no_lc: When checked, this option turns off LUT combining
            # -keep_equivalent_registers -no_lc
            "synth": ["-flatten_hierarchy full",
                      "-retiming",
                      "-directive PerformanceOptimized",
                      "-fsm_extraction one_hot",
                      #   "-resource_sharing off",
                      #   "-no_lc",
                      "-shreg_min_size 5",
                      "-keep_equivalent_registers "
                      ],
            "opt": ["-directive ExploreWithRemap"],
            "place": ["-directive ExtraTimingOpt"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt'],
            "phys_opt": ["-directive AggressiveExplore"],
            # "route": "-directive NoTimingRelaxation",
            "route": ["-directive AlternateCLBRouting"],
        },
        "Area": {
            # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
            "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-directive AreaOptimized_medium"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Default",
            "place_opt": "-directive ExploreArea",
            # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        },
        "AreaHigh": {
            # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
            "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing on", "-directive AreaOptimized_high"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Default",
            "place_opt": "-directive ExploreArea",
            # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        },
        "AreaPower": {
            # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
            "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-gated_clock_conversion auto", "-directive AreaOptimized_medium"],
            "opt": ["-directive ExploreArea"],
            "place": "-directive Default",
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            "place_opt2": ["-directive ExploreArea"],
            # FIXME!!! This is the only option that results in correct post-impl timing sim! Why??!
            "phys_opt": ["-directive AggressiveExplore"],
            "route": ["-directive Explore"],
        },
        "AreaTiming": {
            "synth": ["-flatten_hierarchy rebuilt", "-retiming"],
            # if no directive: -resynth_seq_area
            "opt": ["-directive ExploreWithRemap"],
            "place": ["-directive ExtraPostPlacementOpt"],
            # "place_opt": ["-directive ExploreArea"],
            "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            "place_opt2": ["-directive ExploreArea"],
            # if no directive: -placement_opt
            "phys_opt": "-directive AggressiveExplore",
            "route": "-directive Explore",
        },
        "AreaExploreWithRemap": {
            "synth": ["-flatten_hierarchy full", "-retiming"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreWithRemap",
            "place": "-directive Default",
            "place_opt": "-directive ExploreWithRemap",
            # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        },
        "AreaExploreWithRemap2": {
            "synth": [],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Default",
            "place_opt": "-directive ExploreWithRemap",
            # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        },
        "AreaExplore": {
            "synth": ["-flatten_hierarchy full"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreArea",
            "place": "-directive Default",
            "place_opt": "-directive ExploreArea",
            # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        },
        "Power": {
            "synth": ["-flatten_hierarchy full", "-gated_clock_conversion auto", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto"],
            # if no directive: -resynth_seq_area
            "opt": "-directive ExploreSequentialArea",
            "place": "-directive Default",
            "place_opt": "-directive ExploreSequentialArea",
            # ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
            #   '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
            # if no directive: -placement_opt
            "phys_opt": "-directive Explore",
            "route": "-directive Explore",
        }
    }

    def run(self):
        rtl_settings = self.settings.design["rtl"]
        flow_settings = self.settings.flow
        generics_options = vivado_generics(
            rtl_settings.get("generics", {}), sim=False)

        input_delay = flow_settings.get('input_delay', 0)
        output_delay = flow_settings.get('output_delay', 0)
        constrain_io = flow_settings.get('constrain_io', False)
        out_of_context = flow_settings.get('out_of_context', False)

        clock_xdc_path = self.copy_from_template(f'clock.xdc',
                                                 constrain_io=constrain_io,
                                                 input_delay=input_delay,
                                                 output_delay=output_delay,
                                                 )

        strategy = flow_settings.get('strategy', 'Default')
        if isinstance(strategy, abc.Mapping):
            options = copy.deepcopy(strategy)
        else:
            logger.info(f'Using synthesis strategy: {strategy}')
            if strategy not in self.strategy_options.keys():
                self.fatal(f'Unknown strategy: {strategy}')
            options = copy.deepcopy(self.strategy_options[strategy])

        if 'place_opt2' not in options:
            options['place_opt2'] = None

        if out_of_context:
            options['synth'].extend(["-mode", "out_of_context"])

        for k, v in options.items():
            if isinstance(v, str):
                options[k] = v.split()

        default_blacklisted_resources = ['latch']
        # backward compatibility
        if not self.settings.flow.get('allow_brams', True):
            default_blacklisted_resources.append('bram_tile')

        if not flow_settings.get('allow_dsps', True):
            default_blacklisted_resources.append('dsp')

        blacklisted_resources = flow_settings.get(
            'blacklisted_resources', default_blacklisted_resources)
        if 'bram' in blacklisted_resources and 'bram_tile' not in blacklisted_resources:
            blacklisted_resources.append('bram_tile')

        self.blacklisted_resources = blacklisted_resources
        logger.info(f"blacklisted_resources: {blacklisted_resources}")

        if 'bram_tile' in blacklisted_resources:
            # FIXME also add -max_uram 0 for ultrascale+
            options['synth'].append('-max_bram 0')
        if 'dsp' in blacklisted_resources:
            options['synth'].append('-max_dsp 0')

        # to strings
        for k, v in options.items():
            options[k] = ' '.join(v) if v is not None else None

        script_path = self.copy_from_template(f'{self.name}.tcl',
                                              xdc_files=[clock_xdc_path],
                                              options=options,
                                              generics_options=generics_options,
                                              synth_output_dir=self.synth_output_dir,
                                              checkpoints_dir=self.checkpoints_dir
                                              )
        return self.run_vivado(script_path)

    def parse_reports(self):
        report_stage = 'post_route'
        reports_dir = self.reports_dir / report_stage

        failed = False

        failed |= not self.parse_report_regex(reports_dir / 'timing_summary.rpt',
                                              r'Design\s+Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+TPWS Failing Endpoints\s+TPWS Total Endpoints\s*' +
                                              r'\s*(?:\-+\s+)+' +
                                              r'(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<_tns>\-?\d+(?:\.\d+)?)\s+(?P<_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<_tns_total_endpoints>\-?\d+(?:\.\d+)?)\s+'
                                              r'(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<_ths>\-?\d+(?:\.\d+)?)\s+(?P<_ths_failing_endpoints>\-?\d+(?:\.\d+)?)\s+(?P<_ths_total_endpoints>\-?\d+(?:\.\d+)?)\s+',
                                              r'Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<clock_frequency>\d+(?:\.\d+)?)'
                                              )

        # failed |= not self.parse_report(reports_dir / 'power.rpt',
        #                                 r'^\s*\|\s*Total\s+On-Chip\s+Power\s+\((?P<power_onchip_unit>\w+)\)\s*\|\s*(?P<power_onchip>[\-\.\w]+)\s*\|.*' +
        #                                 r'^\s*\|\s*Dynamic\s*\((?P<power_dynamic_unit>\w+)\)\s*\|\s*(?P<power_dynamic> [\-\.\w]+)\s*\|.*' +
        #                                 r'^\s*\|\s*Device\s+Static\s+\((?P<power_static_unit>\w+)\)\s*\|\s*(?P<power_static>[\-\.\w]+)\s*\|.*' +
        #                                 r'^\s*\|\s*Confidence\s+Level\s*\|\s*(?P<power_confidence_level>[\-\.\w]+)\s*\|.*' +
        #                                 r'^\s*\|\s*Design\s+Nets\s+Matched\s*\|\s*(?P<power_nets_matched>[\-\.\w]+)\s*\|.*'
        #                                 )

        report_file = reports_dir / 'utilization.xml'
        utilization = self.parse_xml_report(report_file)
        # ordered dict
        fields = [
            ('slice', ['Slice Logic Distribution', 'Slice']),
            ('slice', ['CLB Logic Distribution', 'CLB']),  # Ultrascale+
            ('lut', ['Slice Logic', 'Slice LUTs']),
            ('lut', ['Slice Logic', 'Slice LUTs*']),
            ('lut', ['CLB Logic', 'CLB LUTs']),
            ('lut', ['CLB Logic', 'CLB LUTs*']),
            ('lut_logic', ['Slice Logic', 'LUT as Logic']),
            ('lut_logic', ['CLB Logic', 'LUT as Logic']),
            ('lut_mem', ['Slice Logic', 'LUT as Memory']),
            ('lut_mem', ['CLB Logic', 'LUT as Memory']),
            ('ff', ['Slice Logic', 'Register as Flip Flop']),
            ('ff', ['CLB Logic', 'CLB Registers']),
            ('latch', ['Slice Logic', 'Register as Latch']),
            ('latch', ['CLB Logic', 'Register as Latch']),
            ('bram_tile', ['Memory', 'Block RAM Tile']),
            ('bram_tile', ["BLOCKRAM", "Block RAM Tile"]),
            ('bram_RAMB36', ['Memory', 'RAMB36/FIFO*']),
            ('bram_RAMB36', ["BLOCKRAM", "RAMB36/FIFO*"]),
            ('bram_RAMB18', ['Memory', 'RAMB18']),
            ('bram_RAMB18', ["BLOCKRAM", "RAMB18"]),
            ('dsp', ['DSP', 'DSPs']),
            ('dsp', ["ARITHMETIC", "DSPs"]),
        ]
        for k, path in fields:
            if self.results.get(k) is None:
                path.append("Used")
                try:
                    self.results[k] = self.get_from_path(utilization, path)
                except:
                    logger.info(
                        f"{path} not found in the utilization report {report_file}.")

        self.results['_utilization'] = utilization

        if not failed:
            for res in self.blacklisted_resources:
                res_util = self.results.get(res)
                if res_util is not None:
                    try:
                        res_util = int(res_util)
                        if res_util > 0:
                            logger.critical(
                                f'{report_stage} utilization report lists {res_util} use(s) of blacklisted resource `{res}`.')
                            failed = True
                    except:
                        logger.warn(
                            f'Unknown utilization value: `{res_util}` for blacklisted resource `{res}`. Optimistically assuming results are not violating the blacklist criteria.')

            # TODO better fail analysis for vivado
            failed |= (self.results['wns'] < 0) or (self.results['whs'] < 0) or (
                self.results['_failing_endpoints'] != 0)

        self.results['success'] = not failed
