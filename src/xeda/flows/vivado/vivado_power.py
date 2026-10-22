import html
import logging
import re
from pathlib import Path
from typing import Any, Dict
from xml.etree import ElementTree

from ...dataclass import Field, deliverable
from ...design import SourceType
from ...flow import FpgaSynthFlow, In, MetricSpec, NoReplacement
from . import Vivado

logger = logging.getLogger(__name__)

#: A label of the summary that ends in a unit: `Dynamic (W)`, `Junction Temperature (C)`.
NUMERIC_LABEL = re.compile(r"\((?:W|C|C/W)\)$")

#: The labels of Vivado's power summary that this flow reports, each with its metric. Vivado
#: always writes these. A label that is not here keeps its text as the key.
SUMMARY_METRICS = {
    "Total On-Chip Power (W)": "power.total",
    "Dynamic (W)": "power.dynamic",
    "Device Static (W)": "power.static",
    "Effective TJA (C/W)": "power.effective_tja",
    "Junction Temperature (C)": "power.junction_temperature",
    "Thermal Margin (C)": "power.thermal_margin",
    "Confidence Level": "power.confidence",
}


class VivadoPower(Vivado, FpgaSynthFlow):
    """Estimate post-implementation power from the switching activity of a simulation.

    The producer `vivado_postsynth_sim` (which runs `vivado_synth` first) writes a SAIF activity
    file. This flow reports the power of the routed checkpoint for that activity. By default the
    activity comes from a simulation of the timing-annotated netlist with the testbench
    (`timing_saif`). A simulation of the functional netlist is faster and avoids the problems of
    timing simulation, but it is less accurate. To use it, bind the input:
    `-s flows.vivado_power.inputs.activity=vivado_postsynth_sim.saif`. A vectorless estimate
    ignores the test vectors. This result follows them, so use a representative testbench.
    """

    results_description = {
        "power.total": MetricSpec("Total on-chip power.", "number", "W"),
        "power.dynamic": MetricSpec(
            "Dynamic (switching) power, driven by the SAIF activity.", "number", "W"
        ),
        "power.static": MetricSpec("Static (leakage) power of the device.", "number", "W"),
        "power.effective_tja": MetricSpec(
            "Effective junction-to-ambient thermal resistance, in degrees Celsius per watt.",
            "number",
        ),
        "power.junction_temperature": MetricSpec(
            "Estimated junction temperature in degrees Celsius.", "number"
        ),
        "power.thermal_margin": MetricSpec(
            "Margin between the estimated junction temperature and the limit, in degrees "
            "Celsius.",
            "number",
        ),
        "power.confidence": MetricSpec(
            'Vivado\'s confidence in the estimate, e.g. "High".', "text"
        ),
        "power.components": MetricSpec(
            "On-chip power of each component that Vivado reports (Clocks, Slice Logic, Signals, "
            "Block RAM, DSP, I/O, ...), keyed by the component name. A value that Vivado prints "
            "as a bound, such as <0.001, is not reported.",
            "numbers",
            "W",
        ),
    }

    #: Power reads the routed checkpoint and the activity file that its producers hand over. It
    #: never reads the testbench. A testbench edit reaches power only through a new output of the
    #: producer.
    design_parts = frozenset({"rtl"})

    #: The source types that can supply an input: an activity file and a routed checkpoint.
    design_source_types = frozenset({SourceType.Saif, SourceType.RoutedCheckpoint})

    class Inputs(FpgaSynthFlow.Inputs):
        activity: Path = In(
            SourceType.Saif,
            producer="vivado_postsynth_sim",
            output="timing_saif",
            description="Switching activity from a successful simulation of the netlist: the "
            "timing-annotated one (`timing_saif`) by default, or the functional one (`saif`).",
        )
        checkpoint: Path = In(
            SourceType.RoutedCheckpoint,
            producer="vivado_synth",
            output="checkpoint_route",
            same_producer_as="activity",
            via="netlist_timing",
            description="The routed design checkpoint that the power report uses.",
        )

    class Settings(Vivado.Settings, FpgaSynthFlow.Settings):
        removed_settings = {
            **Vivado.Settings.removed_settings,
            "suppress_msgs": NoReplacement(
                "power reports from files, and Xeda passed it to nothing"
            ),
            "postsynthsim": "`flows.vivado_postsynth_sim.<key>`",
            "timing_sim": "`flows.vivado_power.inputs.activity=vivado_postsynth_sim.saif` for "
            "faster activity from a functional simulation; power takes timing activity "
            "(`vivado_postsynth_sim.timing_saif`) by default",
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
        """Return the values of the report by metric.

        The labels in `SUMMARY_METRICS` give the `power.*` metrics. The power of the components
        is the table `power.components`. A label that names a unit (`Dynamic (W)`) gives a
        number. A value that is no number, such as the bound `<0.001`, is left out. Any other
        value stays the text of the report, under its label."""
        tree = ElementTree.parse(report_xml)
        results: Dict[str, Any] = {}
        components: Dict[str, float] = {}

        def number(label: str, value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                logger.debug("The power report gives %r for %r, which is no number.", value, label)
                return None

        for tablerow in tree.findall("./section[@title='Summary']/table/tablerow"):
            tablecells = tablerow.findall("tablecell")
            label, value = (html.unescape(x.attrib["contents"]).strip() for x in tablecells)
            key = SUMMARY_METRICS.get(label, label)
            if NUMERIC_LABEL.search(label):
                parsed = number(label, value)
                if parsed is not None:
                    results[key] = parsed
            else:
                results[key] = value

        for tablerow in tree.findall(
            "./section[@title='Summary']/section[@title='On-Chip Components']/table/tablerow"
        ):
            tablecells = tablerow.findall("tablecell")
            if len(tablecells) >= 2:
                contents = [html.unescape(x.attrib["contents"]).strip() for x in tablecells]
                parsed = number(contents[0], contents[1])
                if parsed is not None:
                    components[contents[0]] = parsed
        if components:
            results["power.components"] = components
        return results

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        report_xml = self.report_file(self.run_path / self.settings.power_report_xml)
        if report_xml is None:  # none, or a previous run's
            return False
        results = self.parse_power_report(report_xml)
        self.results.update(**results)
        return True
