import html
import logging
from typing import Any, Dict
from xml.etree import ElementTree

from ...dataclass import Field
from .vivado_postsynthsim import VivadoPostsynthSim
from .vivado_sim import VivadoSim
from .vivado_synth import VivadoSynth

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
        saif: str = Field(
            "activity.saif", description="SAIF file the netlist simulation writes activity to."
        )
        postsynthsim: VivadoPostsynthSim.Settings = Field(
            description="Settings for the `vivado_postsynth_sim` dependency that produces the "
            "switching activity."
        )
        dependency_settings = {"postsynthsim": ("timing_sim", "elab_debug", "saif")}
        power_report_xml: str = Field(
            "power_impl_timing.xml", description="File the XML power report is written to."
        )

    def init(self) -> None:
        assert self.design.tb, "A testbench is required for power estimation"
        ss = self.settings
        assert isinstance(ss, self.Settings)
        self.add_dependency(VivadoPostsynthSim, ss.resolve_dependency("postsynthsim"))

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)

        postsynth_sim_flow = self.pop_dependency(VivadoPostsynthSim)
        synth_flow = postsynth_sim_flow.pop_dependency(VivadoSynth)

        checkpoint = str(
            synth_flow.run_path
            / f"{self.design.name}.runs"
            / "impl_1"
            / f"{self.design.rtl.top}_routed.dcp"
        )
        saif_file = str(postsynth_sim_flow.run_path / self.settings.saif)

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
        report_xml = self.run_path / self.settings.power_report_xml
        results = self.parse_power_report(report_xml)
        self.results.update(**results)
        return True
