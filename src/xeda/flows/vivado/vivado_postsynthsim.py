import logging
from pathlib import Path

from ...dataclass import Field
from ...design import DesignSource, RtlSettings, SourceType
from ...flow import Flow, FlowFatalError, FpgaSynthFlow, In, Out
from ...utils import SDF
from .vivado_sim import VivadoSim

log = logging.getLogger(__name__)


class VivadoPostsynthSim(VivadoSim):
    """Simulate the testbench on the routed netlist `vivado_synth` writes.

    Runs `vivado_synth` with `write_netlist` and `write_timing_netlist`, then simulates its
    functional netlist (`netlist`), or with `timing_sim` its timing netlist (`netlist_timing`)
    annotated with its slow-corner SDF (`sdf_max`).
    """

    class Settings(VivadoSim.Settings, FpgaSynthFlow.Settings):
        removed_settings = {
            **VivadoSim.Settings.removed_settings,
            "synth": "`flows.vivado_synth.<key>`",
        }
        timing_sim: bool = Field(
            False,
            description="Simulate the routed timing netlist annotated with its slow-corner SDF, "
            "instead of the routed functional netlist.",
        )

    class Inputs(VivadoSim.Inputs):
        netlist: Path | None = In(
            SourceType.FpgaNetlist,
            producer="vivado_synth",
            output="netlist",
            same_producer_as="netlist_timing",
            description="The routed functional Verilog netlist.",
        )
        netlist_timing: Path | None = In(
            SourceType.FpgaTimingNetlist,
            producer="vivado_synth",
            output="netlist_timing",
            description="The routed timing Verilog netlist, annotated with `sdf`.",
        )
        sdf: Path | None = In(
            SourceType.Sdf,
            producer="vivado_synth",
            output="sdf",
            same_producer_as="netlist_timing",
            description="The routed timing netlist's slow-corner SDF annotation.",
        )

    class Outputs(VivadoSim.Outputs):
        saif: Path | None = Out(
            SourceType.Saif,
            enabled_by="saif",
            description="Switching activity from the selected simulation.",
        )
        timing_saif: Path | None = Out(
            SourceType.Saif,
            enabled_by="timing_sim",
            description="Switching activity from a timing-annotated simulation.",
        )

    @classmethod
    def enable_output(cls, settings: Flow.Settings, name: str, *, design_name: str) -> None:
        """An activity demand names the file: `saif` has no default to switch on, so a consumer's
        demand gives it its conventional name (`outputs/<design>.saif`), the one the run writes
        when the setting names a location. Timing activity also enables timing."""
        if name not in ("saif", "timing_saif"):
            return super().enable_output(settings, name, design_name=design_name)
        assert isinstance(settings, cls.Settings)
        if settings.saif is None:
            settings.saif = cls.conventional_activity(settings, design_name)
        if name == "timing_saif":
            super().enable_output(settings, name, design_name=design_name)

    @staticmethod
    def conventional_activity(settings: Flow.Settings, design_name: str) -> Path:
        """The name the activity file is written under when nothing names it."""
        conventional = settings.conventional_output("saif", design_name)
        assert conventional is not None  # `saif` is a deliverable with a conventional name
        return Path(conventional)

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        assert isinstance(self.outputs, self.Outputs)
        synth_netlist_path = self.inputs.netlist_timing if ss.timing_sim else self.inputs.netlist
        if synth_netlist_path is None or not synth_netlist_path.is_file():
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
                if self.inputs.sdf is None:
                    raise FlowFatalError("Timing simulation requires the declared sdf input")
                ss.sdf = SDF(max=self.inputs.sdf)
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
        # A direct timing request enables timing_saif without enabling the separate saif
        # output. Supply the recorder's filename during simulation, preserving its switch.
        requested_saif = ss.saif
        recording_saif = requested_saif or (
            self.conventional_activity(ss, self.design.name) if ss.timing_sim else None
        )
        try:
            ss.saif = recording_saif
            super().run()
        finally:
            ss.saif = requested_saif
        if recording_saif:
            activity_path = self.run_path / recording_saif
            if requested_saif:
                self.outputs.saif = activity_path
            if ss.timing_sim:
                self.outputs.timing_saif = activity_path
