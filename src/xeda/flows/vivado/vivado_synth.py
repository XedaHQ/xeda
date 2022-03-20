import logging
from typing import Any, Dict, Optional, List
from pydantic import validator, Field, NoneStr
from pydantic.main import Extra

from ..flow import FPGA, FpgaSynthFlow, XedaBaseModel
from . import Vivado

log = logging.getLogger(__name__)

# curated options based on experiments and:
# see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug901-vivado-synthesis.pdf
xeda_strategies: Dict[str, Dict[str, Any]] = {
    "Debug": {
        "synth": {"-assert": None, "-debug_log": None,
                  "-flatten_hierarchy": "none", "-keep_equivalent_registers": None,
                  "-no_lc": None, "-fsm_extraction": "off", "-directive": "RuntimeOptimized"},
        "opt": {"-directive": "RuntimeOptimized"},
        "place": {"-directive": "RuntimeOptimized"},
        "place_opt": {},
        "route": {"-directive": "RuntimeOptimized"},
        "phys_opt": {"-directive": "RuntimeOptimized"}
    },

    "Runtime": {
        "synth": {"-directive": "RuntimeOptimized"},
        "opt": {"-directive": "RuntimeOptimized"},
        "place": {"-directive": "RuntimeOptimized"},
        "place_opt": {},
        # with -ultrathreads results are not reproducible!
        # OR "-no_timing_driven -ultrathreads",
        "route": {"-directive": "RuntimeOptimized"},
        "phys_opt": {"-directive": "RuntimeOptimized"}
    },

    "Default": {
        "synth": {"-flatten_hierarchy": "rebuilt", "-directive": "Default"},
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "Default"},
        "place_opt": {},
        "route": {"-directive": "Default"},
        "phys_opt": {"-directive": "Default"}
    },

    "Timing": {
        # -mode: default, out_of_context
        # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
        # -no_lc: When checked, this option turns off LUT combining
        # -keep_equivalent_registers -no_lc
        "synth": {"-flatten_hierarchy": "rebuilt",
                  "-retiming": None,
                  "-directive": "PerformanceOptimized",
                  "-fsm_extraction": "one_hot",
                  #   "-resource_sharing off",
                  #   "-no_lc",
                  "-shreg_min_size": "5",
                  #   "-keep_equivalent_registers "
                  },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraPostPlacementOpt"},
        "place_opt": {'-retarget': None, '-propconst': None, '-sweep': None, '-aggressive_remap': None, '-shift_register_opt': None},
        "phys_opt": {"-directive": "AggressiveExplore"},
        "place_opt2": {"-directive": "Explore"},
        # "route": "-directive NoTimingRelaxation",
        "route": {"-directive": "AggressiveExplore"},
    },
    # "ExtraTimingCongestion": {
    #     "synth": ["-flatten_hierarchy": "full",
    #               "-retiming",
    #               "-directive": "PerformanceOptimized",
    #               "-fsm_extraction": "one_hot",
    #               "-resource_sharing off",
    #               "-shreg_min_size 10",
    #               "-keep_equivalent_registers",
    #               ],
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive AltSpreadLogic_high"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-remap', '-muxf_remap', '-aggressive_remap', '-shift_register_opt'],
    #     "place_opt2": ["-directive Explore"],
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     "route": ["-directive AlternateCLBRouting"],
    # },
    "ExtraTiming": {
        "synth": {"-flatten_hierarchy": "full",
                  "-retiming": None,
                  "-directive": "PerformanceOptimized",
                  "-fsm_extraction": "one_hot",
                  "-resource_sharing": "off",
                  #   "-no_lc",
                  "-shreg_min_size": "10",
                  "-keep_equivalent_registers": None,
                  },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraTimingOpt"},
        "place_opt": {'-retarget': None, '-propconst': None, '-sweep': None, '-muxf_remap': None, '-aggressive_remap': None, '-shift_register_opt': None},
        "place_opt2": {"-directive": "Explore"},
        "phys_opt": {"-directive": "AggressiveExplore"},
        "route": {"-directive": "NoTimingRelaxation"},
    },
    # "ExtraTimingAltRouting": {
    #     # -mode: default, out_of_context
    #     # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
    #     # -no_lc: When checked, this option turns off LUT combining
    #     # -keep_equivalent_registers -no_lc
    #     "synth": ["-flatten_hierarchy full",
    #               "-retiming",
    #               "-directive PerformanceOptimized",
    #               "-fsm_extraction one_hot",
    #               #   "-resource_sharing off",
    #               #   "-no_lc",
    #               "-shreg_min_size 5",
    #               "-keep_equivalent_registers "
    #               ],
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive ExtraTimingOpt"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt'],
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     # "route": "-directive NoTimingRelaxation",
    #     "route": ["-directive AlternateCLBRouting"],
    # },
    # "Area": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-directive AreaOptimized_medium"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaHigh": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing on", "-directive AreaOptimized_high"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaPower": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-gated_clock_conversion auto", "-directive AreaOptimized_medium"],
    #     "opt": ["-directive ExploreArea"],
    #     "place": "-directive Default",
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     "place_opt2": ["-directive ExploreArea"],
    #     # FIXME!!! This is the only option that results in correct post-impl timing sim! Why??!
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     "route": ["-directive Explore"],
    # },
    # "AreaTiming": {
    #     "synth": ["-flatten_hierarchy rebuilt", "-retiming"],
    #     # if no directive: -resynth_seq_area
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive ExtraPostPlacementOpt"],
    #     # "place_opt": ["-directive ExploreArea"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     "place_opt2": ["-directive ExploreArea"],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive AggressiveExplore",
    #     "route": "-directive Explore",
    # },
    # "AreaExploreWithRemap": {
    #     "synth": ["-flatten_hierarchy full", "-retiming"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreWithRemap",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreWithRemap",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaExploreWithRemap2": {
    #     "synth": [],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreWithRemap",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaExplore": {
    #     "synth": ["-flatten_hierarchy full"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "Power": {
    #     "synth": ["-flatten_hierarchy full", "-gated_clock_conversion auto", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreSequentialArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreSequentialArea",
    #     # ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #   '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # }
}


StepsValType = Optional[Dict[str, Any]]


def get_steps(run: str) -> List[str]:
    if run == 'synth':
        return ['synth', 'opt']
    else:
        return ['place', 'place_opt', 'place_opt2', 'phys_opt', 'phys_opt2', 'route']


def xeda_steps(strategy: str, s: str):
    return {step: xeda_strategies[strategy].get(step) for step in get_steps(s)}


class RunOptions(XedaBaseModel):
    strategy: NoneStr = None
    steps: Dict[str, StepsValType] = {}


class VivadoSynth(Vivado, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using a custom flow"""
    class BaseSettings(FpgaSynthFlow.Settings):
        fail_critical_warning = False
        fail_timing = True
        optimize_power = False
        optimize_power_postplace = False
        blacklisted_resources: List[str] = Field(
            ['latch'],
            description="list of FPGA resources which are not allowed to be inferred or exist in the results. Valid values: latch, dsp, bram"
        )
        input_delay: Optional[float] = None
        output_delay: Optional[float] = None
        out_of_context = False
        write_checkpoint = False
        write_netlist = False
        write_bitstream = False
        synth: RunOptions = RunOptions()
        impl: RunOptions = RunOptions()

        @validator('fpga')
        def validate_fpga(cls, v: FPGA):
            if not v.part:
                if not v.speed:
                    v.speed = "-1"
                if v.device and v.package:
                    v.part = (v.device + v.package + v.speed).lower()
            assert v.part, "fpga.part is not known. Please specify more details."
            return v

    class Settings(BaseSettings):
        synth_output_dir = 'output'
        checkpoints_dir = 'checkpoints'
        synth: RunOptions = RunOptions(
            strategy='Default', steps=xeda_steps('Default', 'synth'))
        impl: RunOptions = RunOptions(
            strategy='Default', steps=xeda_steps('Default', 'impl'))

        @validator('synth')
        def validate_synth(cls, v: RunOptions):
            if v.strategy and not v.steps:
                v.steps = xeda_steps(v.strategy, 'synth')
            return v

        @validator('impl', always=True)
        def validate_impl(cls, v: RunOptions, values):
            if not v.strategy:
                v.strategy = values.get('synth', {}).get('strategy')
            if v.strategy and not v.steps:
                v.steps = xeda_steps(v.strategy, 'impl')
            return v

    def run(self):
        clock_xdc_path = self.copy_from_template(f'clock.xdc')
        settings = self.settings

        if settings.out_of_context:
            settings.synth.steps['synth'] += {"-mode": "out_of_context"}

        blacklisted_resources = self.settings.blacklisted_resources
        if 'bram' in blacklisted_resources and 'bram_tile' not in blacklisted_resources:
            blacklisted_resources.append('bram_tile')

        self.blacklisted_resources = blacklisted_resources
        log.info(f"blacklisted_resources: {blacklisted_resources}")

        # if 'bram_tile' in blacklisted_resources:
        #     # FIXME also add -max_uram 0 for ultrascale+
        #     settings.synth.steps['synth'].append('-max_bram 0')
        # if 'dsp' in blacklisted_resources:
        #     settings.synth.steps['synth'].append('-max_dsp 0')

        self.add_template_filter('flatten_dict',
                                 lambda d: ' '.join(
                                     [f"{k} {v}" if v else k for k, v in d.items()])
                                 )

        script_path = self.copy_from_template(f'{self.name}.tcl',
                                              xdc_files=[clock_xdc_path],
                                              )
        return self.run_vivado(script_path)

    def parse_timing_report(self, reports_dir) -> bool:
        failed = False
        assert isinstance(self.settings, self.Settings)
        if self.design.rtl.clock_port and self.settings.clock_period:
            failed |= not self.parse_report_regex(reports_dir / 'timing_summary.rpt',
                                                  r'Timing\s+Summary[\s\|\-]+WNS\(ns\)\s+TNS\(ns\)\s+' +
                                                  r'TNS Failing Endpoints\s+TNS Total Endpoints\s+WHS\(ns\)\s+THS\(ns\)\s+' +
                                                  r'THS Failing Endpoints\s+THS Total Endpoints\s+WPWS\(ns\)\s+TPWS\(ns\)\s+' +
                                                  r'TPWS Failing Endpoints\s+TPWS Total Endpoints\s*' +
                                                  r'\s*(?:\-+\s+)+' +
                                                  r'(?P<wns>\-?\d+(?:\.\d+)?)\s+(?P<_tns>\-?\d+(?:\.\d+)?)\s+(?P<_failing_endpoints>\d+)\s+(?P<_tns_total_endpoints>\d+)\s+' +
                                                  r'(?P<whs>\-?\d+(?:\.\d+)?)\s+(?P<_ths>\-?\d+(?:\.\d+)?)\s+(?P<_ths_failing_endpoints>\d+)\s+(?P<_ths_total_endpoints>\d+)\s+',
                                                  r'Clock Summary[\s\|\-]+^\s*Clock\s+.*$[^\w]+(\w*)\s+(\{.*\})\s+(?P<clock_period>\d+(?:\.\d+)?)\s+(?P<clock_frequency>\d+(?:\.\d+)?)', required=True

                                                  )
            if not failed:
                wns = self.results.get('wns')
                if isinstance(wns, float) or isinstance(wns, int):
                    if wns < 0:
                        failed = True
                    # see https://support.xilinx.com/s/article/57304?language=en_US
                    # Fmax in Megahertz
                    # Here Fmax refers to: "The maximum frequency a design can run on Hardware in a given implementation
                    # Fmax = 1/(T-WNS), with WNS positive or negative, where T is the target clock period."
                    clock_period = self.results['clock_period']
                    assert isinstance(clock_period, float) or isinstance(
                        clock_period, int), f"clock_period: {clock_period} is not a number"
                    self.results['Fmax'] = 1000.0 / (clock_period - wns)

        return not failed

    def parse_reports(self):
        report_stage = 'post_route'
        reports_dir = self.reports_dir / report_stage

        failed = not self.parse_timing_report(reports_dir)

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
                    log.debug(
                        f"{path} not found in the utilization report {report_file}.")

        self.results['_utilization'] = utilization

        if not failed:
            for res in self.settings.blacklisted_resources:
                res_util = self.results.get(res)
                if res_util is not None:
                    try:
                        res_util = int(res_util)
                        if res_util > 0:
                            log.critical(
                                f'{report_stage} utilization report lists {res_util} use(s) of blacklisted resource `{res}`.')
                            failed = True
                    except:
                        log.warning(
                            f'Unknown utilization value: `{res_util}` for blacklisted resource `{res}`. Optimistically assuming results are not violating the blacklist criteria.')

            # TODO better fail analysis for vivado
            if 'wns' in self.results:
                failed |= (self.results['wns'] < 0)
            if 'whs' in self.results:
                failed |= (self.results['whs'] < 0)
            if '_failing_endpoints' in self.results:
                failed |= self.results['_failing_endpoints'] != 0

        self.results['success'] = not failed
