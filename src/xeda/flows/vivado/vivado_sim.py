import logging
from typing import Any, Dict, List, Optional
from pydantic import validator, Field
from math import floor

from ...design import DefineType
from ...utils import SDF
from ...dataclass import XedaBaseModeAllowExtra
from ..flow import SimFlow
from ...design import DesignSource
from ..vivado import Vivado
from ..vivado.vivado_synth import VivadoSynth

log = logging.getLogger(__name__)


class RunConfig(XedaBaseModeAllowExtra):
    name: Optional[str] = None
    saif: Optional[str] = None
    vcd: Optional[str] = None
    generics: Dict[str, DefineType] = {}


class VivadoSim(Vivado, SimFlow):
    """Simulate using Xilinx Vivado simulator (xsim) flow"""

    # TODO change this?
    # Can run multiple configurations (a.k.a testvectors) in a single run of Vivado through "run_configs"

    class Settings(SimFlow.Settings, Vivado.Settings):
        saif: Optional[str] = None
        elab_flags: List[str] = ["-relax"]
        analyze_flags: List[str] = ["-relax"]
        sim_flags: List[str] = []
        elab_debug: Optional[str] = None  # TODO choices: "typical", ...
        multirun_configs: List[RunConfig] = []
        sdf: SDF = SDF()
        optimization_flags: List[str] = ["-O3"]
        debug_traces: bool = False
        prerun_time: Optional[str] = None

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)

        saif = ss.saif

        elab_flags = ss.elab_flags
        elab_flags.append(f'-mt {"off" if ss.debug else ss.nthreads}')

        elab_debug = ss.elab_debug
        multirun_configs = ss.multirun_configs
        if not elab_debug and (ss.debug or saif or ss.vcd):
            elab_debug = "typical"
        if elab_debug:
            elab_flags.append(f"-debug {elab_debug}")

        assert self.design.tb
        generics = self.design.tb.generics
        if not multirun_configs:
            multirun_configs = [
                RunConfig(saif=saif, generics=generics, vcd=ss.vcd, name="default")
            ]
            if ss.vcd:
                log.info(f"Dumping VCD to {self.run_path / ss.vcd}")
        else:
            for idx, rc in enumerate(multirun_configs):
                # merge
                rc.generics = {**generics, **rc.generics}
                if not rc.saif:
                    rc.saif = saif
                if not rc.name:
                    rc.name = f"run_{idx}"
                if not rc.vcd:
                    rc.vcd = (rc.name + "_" + ss.vcd) if ss.vcd else None
        sdf_root = ss.sdf.root
        if not sdf_root:
            sdf_root = self.design.tb.uut
        for delay_type in ("min", "max", "typ"):
            sdf_file = ss.sdf.dict().get(delay_type)
            if sdf_file:
                assert sdf_root, "neither SDF root nor tb.uut are provided"
                elab_flags.append(f"-sdf{delay_type} {sdf_root}={sdf_file}")

        elab_flags.extend([f"-L {l}" for l in ss.lib_paths])

        for ox in ss.optimization_flags:
            if ox not in elab_flags:
                if ox.startswith("-O") and any(
                    map(lambda s: s.startswith("-O"), elab_flags)
                ):
                    continue
                elab_flags.append(ox)

        script_path = self.copy_from_template(
            f"vivado_sim.tcl",
            multirun_configs=multirun_configs,
            initialize_zeros=False,
            sim_tops=self.design.sim_tops,
            tb_top=self.design.tb.top,
            lib_name="work",
            sim_sources=self.design.sim_sources,
        )
        self.vivado.run("-source", script_path)


class VivadoPostsynthSim(VivadoSim):
    """
    Synthesize/implement design and run post-synth/impl simulation on the generated netlist
    Depends on VivadoSynth
    """

    class Settings(VivadoSim.Settings):
        synth: VivadoSynth.Settings
        tb_clock_param: Dict[str, str] = Field(
            {},
            description="""A mapping of 'clock'->'param'. Sets (and overrides) testbanch parameter/generic named 'param'
                to the value of the the clock period specified for clock named 'clock', converted to nearset smaller integer in *picoseconds*.
                 In other words 'param' will be set to floor(clock.period * 1000.0).
                Example: {'main_clock': 'G_PERIOD_PS'}
            """,
        )
        timing_sim: bool = False

        @validator("tb_clock_param", pre=True)
        def validate_tb_clock_param(cls, value: Any) -> Any:
            if isinstance(value, str):
                value = dict(main_clock=value)
            return value

    def init(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.add_dependency(VivadoSynth, ss.synth)

    def run(self) -> None:
        synth_flow = self.completed_dependencies[0]
        assert isinstance(synth_flow, VivadoSynth)
        ss = self.settings
        assert isinstance(ss, self.Settings)
        synth_settings = synth_flow.settings
        assert isinstance(synth_settings, VivadoSynth.Settings)
        if synth_settings.input_delay is None:
            synth_settings.input_delay = 0.0
        if synth_settings.output_delay is None:
            synth_settings.output_delay = 0.0

        # FIXME!!! For reasons still unknown, not all strategies lead to correct post-impl simulation
        synth_settings.synth.strategy = "AreaPower"
        synth_netlist = synth_flow.artifacts.get("netlist.impl.timesim.v")

        self.design.rtl.sources = [DesignSource(synth_flow.run_path / synth_netlist)]
        assert self.design.tb
        assert self.design.tb.top
        self.design.tb.top = (self.design.tb.primary_top, "glbl")

        if "simprims_ver" not in ss.lib_paths:
            ss.lib_paths.append("simprims_ver")

        if ss.timing_sim:
            if not ss.sdf:
                ss.sdf = SDF(max=str(synth_flow.artifacts.get("sdf.impl")))
            log.info("Timing simulation using SDF %s", ss.sdf)

        for k, v in ss.tb_clock_param.items():
            clock = synth_settings.clocks.get(k)
            if clock:
                self.design.tb.parameters[v] = floor(clock.period * 1000.0)

        ss.elab_flags.extend(
            [
                "-maxdelay",
                "-transport_int_delays",
                "-pulse_r 0",
                "-pulse_int_r 0",
                "-pulse_e 0",
                "-pulse_int_e 0",
            ]
        )
        # run VivadoSim
        super().run()
