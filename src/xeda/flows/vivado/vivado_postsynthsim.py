import logging

from ...dataclass import Field
from ...design import DesignSource, RtlSettings
from ...flow import FlowFatalError
from ...utils import SDF
from .vivado_sim import VivadoSim
from .vivado_synth import NETLIST, NETLIST_TIMING, SDF_MAX, VivadoSynth, artifact_path

log = logging.getLogger(__name__)


class VivadoPostsynthSim(VivadoSim):
    """Simulate the testbench on the routed netlist `vivado_synth` writes.

    Runs `vivado_synth` with `write_netlist`, then simulates its functional netlist (`netlist`),
    or with `timing_sim` its timing netlist (`netlist_timing`) annotated with its slow-corner SDF
    (`sdf_max`).
    """

    class Settings(VivadoSim.Settings):
        synth: VivadoSynth.Settings = Field(
            description="Settings for the `vivado_synth` dependency that produces the netlist. "
            "`write_netlist` is forced on."
        )
        dependency_settings = {"synth": ()}  # nothing to propagate; `init` forces `write_netlist`
        timing_sim: bool = Field(
            False,
            description="Simulate the routed timing netlist annotated with its slow-corner SDF, "
            "instead of the routed functional netlist.",
        )

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)

        synth = ss.resolve_dependency("synth")
        synth.write_netlist = True
        self.add_dependency(VivadoSynth, synth)

    def run(self) -> None:
        synth_flow = self.completed_dependencies[0]
        assert isinstance(synth_flow, VivadoSynth)
        ss = self.settings
        assert isinstance(ss, self.Settings)

        synth_netlist_path = artifact_path(synth_flow, NETLIST_TIMING if ss.timing_sim else NETLIST)
        if not synth_netlist_path.exists():
            raise FlowFatalError(f"Netlist {synth_netlist_path} does not exist!")
        postsynth_sources = [DesignSource(synth_netlist_path)]
        log.info("Setting post-synthesis sources to: %s", postsynth_sources)
        # also removing top-level generics and everything else
        self.design.rtl = RtlSettings(
            top=self.design.rtl.top, sources=postsynth_sources, attributes={}
        )
        assert self.design.tb and self.design.tb.top
        self.design.tb.top = (self.design.tb.top[0], "glbl")

        # the functional netlist instantiates UNISIM primitives, the timing netlist SIMPRIM ones
        libraries = ["simprims_ver"] if ss.timing_sim else ["unisims_ver", "simprims_ver"]
        given = {name for name, _ in ss.lib_paths}
        ss.lib_paths.extend((library, None) for library in libraries if library not in given)

        if ss.timing_sim:
            if not ss.sdf.delay_items():
                ss.sdf = SDF(max=artifact_path(synth_flow, SDF_MAX))
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
