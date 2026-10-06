import html
import logging
from pathlib import Path
from typing import Any, Dict
from xml.etree import ElementTree

from ...dataclass import Field, deliverable
from ...design import SourceType
from ...flow import FpgaSynthFlow, In
from . import Vivado

logger = logging.getLogger(__name__)


class VivadoPower(Vivado, FpgaSynthFlow):
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

    #: Power reads the routed checkpoint and the activity file its producers hand over, never the
    #: testbench: an edit to it re-runs power through its producer's new run, not its own hash.
    design_parts = frozenset({"rtl"})

    class Inputs(FpgaSynthFlow.Inputs):
        activity: Path = In(
            SourceType.Saif,
            producer="vivado_postsynth_sim",
            output="timing_saif",
            description="Switching activity from a successful timing-annotated simulation.",
        )
        checkpoint: Path = In(
            SourceType.Checkpoint,
            producer="vivado_synth",
            output="checkpoint_route",
            description="The routed design checkpoint against which power is reported.",
        )

    class Settings(Vivado.Settings, FpgaSynthFlow.Settings):
        removed_settings = {
            **Vivado.Settings.removed_settings,
            "postsynthsim": "`flows.vivado_postsynth_sim.<key>`",
            "timing_sim": "no setting: power asks for timing activity, which switches "
            "`flows.vivado_postsynth_sim.timing_sim` on itself, so remove it",
            **{
                key: f"`flows.vivado_postsynth_sim.{key}`"
                for key in (
                    "elab_debug",
                    "saif",
                    "stop_time",
                    "prerun_time",
                    "timeout",
                    "fail_severity",
                )
            },
        }
        power_report_xml: Path = Field(
            Path("power_impl_timing.xml"),
            description="File the XML power report is written to.",
            json_schema_extra=deliverable(),
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        script_path = self.copy_from_template(
            "vivado_power.tcl",
            checkpoint=self.inputs.checkpoint,
            saif_file=self.inputs.activity,
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
