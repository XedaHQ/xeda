import html
import logging
from pathlib import Path
from typing import Any, Dict
from xml.etree import ElementTree

from ...dataclass import Field, deliverable
from .vivado_postsynthsim import VivadoPostsynthSim
from .vivado_sim import VivadoSim
from .vivado_synth import CHECKPOINT_ROUTE, VivadoSynth, artifact_path

logger = logging.getLogger(__name__)


class VivadoPower(VivadoSim):
    """Estimate post-implementation power from real switching activity.

    Runs `vivado_postsynth_sim` (which itself runs `vivado_synth`) to produce a SAIF activity
    file from a timing-annotated netlist simulation of the testbench, then reports power against
    the routed checkpoint. Unlike a vectorless estimate, the result reflects the actual
    testvectors, so a representative testbench matters.
    """

    # Keys are taken verbatim from the labels in Vivado's XML power report, so the exact set
    # depends on the device and design. These are the ones Vivado always emits.
    results_description = {
        "Total On-Chip Power (W)": "Total on-chip power in watts.",
        "Dynamic (W)": "Dynamic (switching) power in watts, driven by the SAIF activity.",
        "Device Static (W)": "Static (leakage) power in watts.",
        "Effective TJA (C/W)": "Effective junction-to-ambient thermal resistance.",
        "Junction Temperature (C)": "Estimated junction temperature in degrees Celsius.",
        "Thermal Margin (C)": "Margin between the estimated junction temperature and the limit.",
        "Confidence Level": 'Vivado\'s confidence in the estimate, e.g. "High".',
        "Component Power: <component>": "Per-component on-chip power in watts, one key per "
        "component Vivado reports (Clocks, Slice Logic, Signals, Block RAM, DSP, I/O, ...).",
    }

    class Settings(VivadoSim.Settings):
        timing_sim: bool = Field(
            True,
            description="Gather switching activity from a timing-annotated netlist simulation. "
            "More accurate than a functional simulation, and much slower.",
        )
        elab_debug: str = Field(
            "typical", description="Debug level passed to `xelab -debug` for the activity run."
        )
        saif: Path = Field(
            Path("activity.saif"),
            description="SAIF file the netlist simulation writes activity to.",
        )
        postsynthsim: VivadoPostsynthSim.Settings = Field(
            description="Settings for the `vivado_postsynth_sim` dependency that produces the "
            "switching activity. Its `synth.write_checkpoint` is forced on: power is reported "
            "against the routed checkpoint."
        )
        dependency_settings = {"postsynthsim": ("timing_sim", "elab_debug", "saif")}
        power_report_xml: Path = Field(
            Path("power_impl_timing.xml"),
            description="File the XML power report is written to.",
            json_schema_extra=deliverable(),
        )

    def init(self) -> None:
        super().init()
        assert self.design.tb, "A testbench is required for power estimation"
        ss = self.settings
        assert isinstance(ss, self.Settings)
        postsynthsim = ss.resolve_dependency("postsynthsim")
        postsynthsim.synth.write_checkpoint = True
        self.add_dependency(VivadoPostsynthSim, postsynthsim)

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)

        postsynth_sim_flow = self.pop_dependency(VivadoPostsynthSim)
        synth_flow = postsynth_sim_flow.pop_dependency(VivadoSynth)

        checkpoint = artifact_path(synth_flow, CHECKPOINT_ROUTE)
        # the dependency's recorded artifact, under the name its run wrote it: a location given
        # for it is delivered, and the run writes its conventional name (D21)
        saif_file = artifact_path(postsynth_sim_flow, "saif")

        # assert isinstance(dep_synth_flow.settings, VivadoSynth.Settings)
        script_path = self.copy_from_template(
            "vivado_power.tcl",
            checkpoint=checkpoint,
            saif_file=saif_file,
        )

        self.vivado.run("-source", script_path)

    def parse_power_report(self, report_xml) -> Dict[str, Any]:
        tree = ElementTree.parse(report_xml)
        results = {}
        for tablerow in tree.findall("./section[@title='Summary']/table/tablerow"):
            tablecells = tablerow.findall("tablecell")
            key, value = (html.unescape(x.attrib["contents"]).strip() for x in tablecells)
            results[key] = value

        for tablerow in tree.findall(
            "./section[@title='Summary']/section[@title='On-Chip Components']/table/tablerow"
        ):
            tablecells = tablerow.findall("tablecell")
            if len(tablecells) >= 2:
                contents = [html.unescape(x.attrib["contents"]).strip() for x in tablecells]
                key = contents[0]
                value = contents[1]
                results[f"Component Power: {key}"] = value

        return results

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        report_xml = self.report_file(self.run_path / self.settings.power_report_xml)
        if report_xml is None:  # none, or a previous run's
            return False
        results = self.parse_power_report(report_xml)
        self.results.update(**results)
        return True
