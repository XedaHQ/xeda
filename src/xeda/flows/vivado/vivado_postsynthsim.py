import logging

from xeda.flows.flow import FlowFatalError

from ...design import DesignSource, RtlSettings
from ...utils import SDF
from .vivado_sim import VivadoSim
from .vivado_synth import VivadoSynth

log = logging.getLogger(__name__)


class VivadoPostsynthSim(VivadoSim):
    """
    Synthesizes & implements the design, then runs post-synthesis/post-implementation simulation on the generated netlist.
    The netlist can be optionally annotated with generated timing information (SDF).
    Depends on VivadoSynth
    """

    class Settings(VivadoSim.Settings):
        synth: VivadoSynth.Settings
        timing_sim: bool = False
        enforce_io_delay: bool = True

    def init(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.enforce_io_delay:
            # Force 0 I/O delay if no I/O delay is given
            # This seems to be required to meet timing
            if ss.synth.input_delay is None:
                ss.synth.input_delay = 0.0
            if ss.synth.output_delay is None:
                ss.synth.output_delay = 0.0
        self.add_dependency(VivadoSynth, ss.synth)

    def run(self) -> None:
        synth_flow = self.completed_dependencies[0]
        assert isinstance(synth_flow, VivadoSynth)
        ss = self.settings
        assert isinstance(ss, self.Settings)

        artifacts_path = (
            synth_flow.run_path / synth_flow.settings.outputs_dir / "route_design"
        )
        synth_netlist_path = artifacts_path / "timesim.v"
        if not synth_netlist_path.exists():
            raise FlowFatalError(f"Netlist {synth_netlist_path} does not exist!")
        postsynth_sources = [DesignSource(synth_netlist_path)]
        log.info("Setting post-synthesis sources to: %s", postsynth_sources)
        # also removing top-level generics and everything else
        self.design.rtl = RtlSettings(
            top=self.design.rtl.top, sources=postsynth_sources
        )
        assert self.design.tb and self.design.tb.top
        self.design.tb.top = (self.design.tb.top[0], "glbl")

        if "simprims_ver" not in ss.lib_paths:
            ss.lib_paths.append(("simprims_ver", None))

        if ss.timing_sim:
            if not ss.sdf.delay_items():
                ss.sdf = SDF(max=str(artifacts_path / "timesim.max.sdf"))
            if not ss.sdf.root:
                ss.sdf.root = self.design.tb.uut
            log.info("Timing simulation using SDF %s", ss.sdf)

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
