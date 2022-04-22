import html
import logging
from typing import Any, Dict
from xml.etree import ElementTree

from .vivado_postsynthsim import VivadoPostsynthSim
from .vivado_alt_synth import VivadoSynth

logger = logging.getLogger(__name__)


class VivadoPower(VivadoPostsynthSim):
    class Settings(VivadoPostsynthSim.Settings):
        saif: str = "activity.saif"
        elab_debug = "typical"
        timing_sim = True
        power_report_xml: str = "power_impl_timing.xml"

    def run(self) -> None:
        super().run()

        assert isinstance(self.settings, self.Settings)
        dep_synth_flow = self.pop_dependency(VivadoSynth)
        assert self.design.tb
        # assert isinstance(dep_synth_flow.settings, VivadoSynth.Settings)
        top = self.design.rtl.top
        script_path = self.copy_from_template(
            "vivado_power.tcl",
            checkpoint=str(
                dep_synth_flow.run_path / f"{top}.runs" / "impl_1" / f"{top}_routed.dcp"
            ),
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
