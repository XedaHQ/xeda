import html
import logging
from typing import Any, Dict
from xml.etree import ElementTree

from .vivado_sim import VivadoSim
from .vivado_postsynthsim import VivadoPostsynthSim
from .vivado_synth import VivadoSynth

logger = logging.getLogger(__name__)


class VivadoPower(VivadoSim):
    class Settings(VivadoSim.Settings):
        timing_sim: bool = True
        elab_debug: str = "typical"
        saif: str = "activity.saif"
        postsynthsim: VivadoPostsynthSim.Settings
        power_report_xml: str = "power_impl_timing.xml"

    def init(self) -> None:
        assert self.design.tb, "A testbench is required for power estimation"
        ss = self.settings
        assert isinstance(ss, self.Settings)
        ss.postsynthsim.timing_sim = ss.timing_sim
        ss.postsynthsim.elab_debug = ss.elab_debug
        ss.postsynthsim.saif = ss.saif
        self.add_dependency(VivadoPostsynthSim, ss.postsynthsim)

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
            key, value = (
                html.unescape(x.attrib["contents"]).strip() for x in tablecells
            )
            results[key] = value

        for tablerow in tree.findall(
            "./section[@title='Summary']/section[@title='On-Chip Components']/table/tablerow"
        ):
            tablecells = tablerow.findall("tablecell")
            if len(tablecells) >= 2:
                contents = [
                    html.unescape(x.attrib["contents"]).strip() for x in tablecells
                ]
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
