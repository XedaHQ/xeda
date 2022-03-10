import logging
from typing import Dict, List
from pydantic.types import NoneStr

from ..design import DefineType
from ..flow import SimFlow, XedaBaseModel
from ..vivado import Vivado

log = logging.getLogger(__name__)


class RunConfig(XedaBaseModel, arbitrary_types_allowed=True):
    name: NoneStr = None
    saif: NoneStr = None
    vcd: NoneStr = None
    generics: Dict[str, DefineType] = {}


class VivadoSim(Vivado, SimFlow):
    """
    xsim flow
    Can run multiple configurations (a.k.a testvectors) in a single run of Vivado through "run_configs"
    """

    class Settings(SimFlow.Settings, Vivado.Settings):
        saif: NoneStr = None
        elab_flags: List[str] = ['-relax']
        analyze_flags: List[str] = ['-relax']
        sim_flags: List[str] = []
        elab_debug: NoneStr = None  # TODO choices: "typical", ...
        multirun_configs: List[RunConfig] = []
        sdf: NoneStr = None
        optimization_flags: List[str] = ['-O3']
        debug_traces: bool = False
        prerun_time: NoneStr = None

    def run(self):
        saif = self.settings.saif

        elab_flags = self.settings.elab_flags
        elab_flags.append(
            f'-mt {"off" if self.settings.debug else self.settings.nthreads}')

        elab_debug = self.settings.elab_debug
        multirun_configs = self.settings.multirun_configs
        if not elab_debug and (self.settings.debug or saif or self.vcd):
            elab_debug = "typical"
        if elab_debug:
            elab_flags.append(f'-debug {elab_debug}')

        generics = self.design.tb.generics
        if not multirun_configs:
            multirun_configs = [RunConfig(saif=saif, generics=generics,
                                     vcd=self.vcd, name='default')]
            if self.vcd:
                log.info(f"Dumping VCD to {self.run_path / self.vcd}")
        else:
            for idx, rc in enumerate(multirun_configs):
                # merge
                rc.generics = {**generics, **rc.generics}
                if not rc.saif:
                    rc.saif = saif
                if not rc.name:
                    rc.name = f'run_{idx}'
                if not rc.vcd:
                    rc.vcd = (rc.name + '_' +
                                 self.vcd) if self.vcd else None

        tb_uut = self.design.tb.uut
        sdf = self.settings.sdf
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

        libraries = self.settings.lib_paths
        if libraries:
            elab_flags.extend([f'-L {l}' for l in libraries])

        for ox in self.settings.optimization_flags:
            if ox not in elab_flags:
                if ox.startswith("-O") and any(map(lambda s: s.startswith("-O"), elab_flags)):
                    continue
                elab_flags.append(ox)

        script_path = self.copy_from_template(f'vivado_sim.tcl',
                                              multirun_configs=multirun_configs,
                                              initialize_zeros=False,
                                              sim_tops=self.design.sim_tops,
                                              tb_top=self.design.tb.top,
                                              lib_name='work',
                                              sim_sources=self.design.sim_sources
                                              )
        return self.run_vivado(script_path)


# class VivadoPostsynthSim(VivadoSim):
#     """
#     Synthesize/implement design and run post-synth/impl simulation on the generated netlist
#     Depends on VivadoSynth
#     """
#     class Settings(VivadoSim.Settings, VivadoSynth.Settings):
#         pass

#     @classmethod
#     def prerequisite_flows(cls, flow_settings: 'VivadoPostsynthSim'.Settings, design: Design):
#         synth_overrides = dict(constrain_io=True)
#         period = flow_settings.clock_period
#         if period:
#             synth_overrides.clock_period = period

#         opt_power = flow_settings.optimize_power
#         if opt_power is not None:
#             synth_overrides.optimize_power = opt_power

#         return {VivadoSynth: (synth_overrides, {})}

#     def __init__(self, settings: Settings, args: SimpleNamespace, completed_dependencies: List[Flow]):
#         self.synth_flow: VivadoSynth = completed_dependencies[0]
#         self.synth_settings = self.synth_flow.settings.flow
#         self.synth_results = self.synth_flow.results

#         settings.design['rtl']['sources'] = [DesignSource(
#             self.synth_flow.flow_run_dir / VivadoSynth.synth_output_dir / 'impl_timesim.v')]

#         design_settings = settings.design
#         tb_settings = design_settings['tb']
#         flow_settings = settings.flow
#         top = tb_settings['top']
#         if isinstance(top, str):
#             top = [top]
#         if not 'glbl' in top:
#             top.append('glbl')
#         tb_settings['top'] = top

#         if 'libraries' not in flow_settings:
#             flow_settings['libraries'] = []
#         if 'simprims_ver' not in flow_settings['libraries']:
#             flow_settings['libraries'].append('simprims_ver')

#         netlist_base = os.path.splitext(
#             str(design_settings['rtl']['sources'][0]))[0]

#         timing_sim = bool(try_convert(flow_settings.get('timing_sim', True)))

#         if timing_sim:
#             if not flow_settings.get('sdf'):
#                 flow_settings['sdf'] = {'file': netlist_base + '.sdf'}
#             logger.info(f"Timing simulation using SDF {flow_settings['sdf']}")

#         clock_period_ps_generic = tb_settings.get(
#             'clock_period_ps_generic', 'G_PERIOD_PS')  # FIXME
#         tb_settings['generics'] = tb_settings.get(
#             'generics', {})  # optional key, create if not exists
#         if clock_period_ps_generic:
#             clock_ps = math.floor(
#                 self.synth_settings['clock_period'] * 1000)
#             tb_settings['generics'][clock_period_ps_generic] = clock_ps
#             for rc in flow_settings.get('run_configs', []):
#                 rc['generics'] = rc.get('generics', {})  # create if not exists
#                 rc['generics'][clock_period_ps_generic] = clock_ps

#         flow_settings['elab_flags'] = ['-relax', '-maxdelay', '-transport_int_delays',
#                                        '-pulse_r 0', '-pulse_int_r 0', '-pulse_e 0', '-pulse_int_e 0']

#         VivadoSim.__init__(self, settings, args, completed_dependencies)
