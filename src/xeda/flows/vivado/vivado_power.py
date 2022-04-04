import html
import logging
from typing import Any, Dict
from xml.etree import ElementTree

from . import Vivado
from .vivado_postsynthsim import VivadoPostsynthSim
from .vivado_alt_synth import VivadoAltSynth

logger = logging.getLogger(__name__)


class VivadoPower(Vivado):
    class Settings(VivadoPostsynthSim.Settings):
        power_report_filename: str = "power_impl_timing.xml"
        elab_debug = "typical"
        timing_sim = True
        saif_filename: str = "impl_timing.saif"

    def init(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        # override/sanitize VivadoPostsynthSim dependency settings

        # FIXME!!! For reasons still unknown, not all strategies lead to correct post-impl simulation
        # ss.synth.synth.strategy = "AreaPower"
        ss.synth.optimize_power = True
        # pss.synth.clock_period
        # pss.synth.clocks
        self.add_dependency(VivadoPostsynthSim, ss.copy())

    def run(self) -> None:

        # FIXME FIND A CLEANER WAY TO SATISFY MYPY!
        assert isinstance(self.settings, self.Settings)
        postsynthsim = self.completed_dependencies[0]
        assert isinstance(postsynthsim, VivadoPostsynthSim)
        postsynthsim_settings = postsynthsim.settings
        assert isinstance(postsynthsim_settings, VivadoPostsynthSim.Settings)
        run_configs = postsynthsim_settings.multirun_configs
        dep_synth_flow = postsynthsim.completed_dependencies[0]
        assert isinstance(dep_synth_flow, VivadoAltSynth)

        # assert False, "FIXME! not implemented! not working!" # FIXME
        # def update_saif_path(rc):
        #     rc["saif"] = str(postsynthsim.run_path / rc["saif"])
        #     return rc

        # if run_configs:
        #     run_configs = [update_saif_path(rc) for rc in run_configs]
        # else:
        #     run_configs.append(
        #         RunConfig(
        #         saif=saif_filename, report=self.settings.power_report_filename
        #     )
        #     )

        assert self.design.tb
        assert isinstance(dep_synth_flow.settings, VivadoAltSynth.Settings)

        script_path = self.copy_from_template(
            f"vivado_power.tcl",
            run_configs=run_configs,
            checkpoint=str(
                dep_synth_flow.run_path
                / dep_synth_flow.settings.checkpoints_dir
                / "post_route.dcp"
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
        report_xml = self.run_path / self.settings.power_report_filename
        results = self.parse_power_report(report_xml)
        self.results.update(**results)
        return True
