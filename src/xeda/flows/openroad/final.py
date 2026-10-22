"""The last OpenROAD flow: the detailed route, the antenna repair, the filler cells, the final
outputs and the report of the metrics.

`effective` is the one function that turns the settings and the platform into the values that the
templates read as `P`. It reads nothing else.
"""

import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape as xml_escape

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import Flow, In, MetricSpec, NoReplacement, Out, describe_results
from ...flow.flow import FlowSettingsError, FlowSettingsException
from ...platforms.model import PlatformModel
from ...tool import NonZeroExitCode, Tool
from ...units import Length
from ...utils import replacing_file
from .base import (
    PLATFORM_ROLES,
    EffectiveBase,
    OpenroadStage,
    base_values,
    failure_lines,
    platform_time_unit,
    tcl_number,
    to_micrometers,
)
from .final_groups import (
    DetailedRouteGroup,
    ExtractionGroup,
    FillGroup,
    FinalAntennaGroup,
    GdsGroup,
    IrDropGroup,
    NdrGroup,
    ReportsGroup,
)
from .groups import ViaInPin, differing_leaves
from .metrics import MetricsError, final_results

__all__ = [
    "EffectiveFinal",
    "Openroad",
    "drc_iteration_reports",
    "decap_words",
    "effective",
    "ndr_words",
    "resolve_extraction",
    "snapshot_files",
]

log = logging.getLogger(__name__)

#: The roles of the platform files that the merge of the layout reads: the technology file of
#: KLayout, which names how it reads the DEF and the LEF files, and the GDSII files of the cells.
GDS_ROLES = ("klayout_tech", "gds")
#: The members of `LoadLayoutOptions.CellConflictResolution` of KLayout, by setting.
CONFLICT_RESOLUTIONS = {
    "add_to_cell": "AddToCell",
    "overwrite_cell": "OverwriteCell",
    "rename_cell": "RenameCell",
    "skip_new_cell": "SkipNewCell",
}


def tri_state(value: Optional[bool]) -> str:
    """Return the text of a `-rd` name of the merge for an option that can be unset: an empty
    text for `None`, else `true` or `false`."""
    return "" if value is None else str(value).lower()


#: The files of the IR drop analysis for the net number `n`, by kind. Each name is fixed, and
#: numbered from 1 in the order of the nets, since the name of a net would reach the file system.
IR_DROP_FILES = {
    "error": "reports/irdrop_{n}.rpt",
    "voltage": "reports/irdrop_voltage_{n}.csv",
    "em": "reports/irdrop_em_{n}.csv",
    "spice": "reports/irdrop_spice_{n}.sp",
}
#: The images that the image process saves, by file name in the directory `images`. Each name is
#: fixed, since a name from a net or a clock would reach the file system from the design.
IMAGE_NAMES = (
    "final_all",
    "final_routing",
    "final_placement",
    "final_clocks",
    "final_resizer",
    "final_congestion",
    "final_worst_path",
)


#: A `<lef-files>` element of a KLayout technology file (`<lef-files>x</lef-files>`, over lines if
#: need be, or the empty `<lef-files/>`), with the indentation before it and the end of its line
#: after it.
LEF_FILES_ELEMENT = re.compile(
    r"(?P<indent>[ \t]*)<lef-files(?:\s*/>|>.*?</lef-files>)[ \t]*(?P<eol>\r?\n)?", re.DOTALL
)


#: A comment or a CDATA section of an XML text. Text inside is no element.
XML_VERBATIM = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>", re.DOTALL)


def klayout_tech_with_lef_files(text: str, lefs: Sequence[Path]) -> Optional[str]:
    """Return the text of a KLayout technology file with one list of LEF files.

    The function removes every `<lef-files>` element (an element may span lines or be empty) and
    writes the list once, where the first element was. A text with a `<lefdef>` section and no
    element gets the list at the end of the section. Each path is escaped as XML text. A comment
    or a CDATA section is left as it is: it holds no element. Return `None` if the text has
    neither an element nor a `<lefdef>` section."""
    elements = "".join(f"<lef-files>{xml_escape(str(lef))}</lef-files>" for lef in lefs)
    # Blank out the comments and the CDATA sections with a character that no pattern below matches,
    # so that the search sees only real markup. The blanked text has the same length, so a position
    # in it is a position in the text.
    markup = XML_VERBATIM.sub(lambda match: "\0" * len(match.group()), text)
    found = list(LEF_FILES_ELEMENT.finditer(markup))
    if not found:
        closing = markup.find("</lefdef>")
        if closing < 0:
            return None
        return f"{text[:closing]}{elements}{text[closing:]}"
    parts: list[str] = []
    end = 0
    for number, match in enumerate(found):
        parts.append(text[end : match.start()])
        if number == 0:
            parts.append(f"{match['indent']}{elements}{match['eol'] or ''}")
        end = match.end()
    parts.append(text[end:])
    return "".join(parts)


@dataclass(frozen=True)
class EffectiveFinal(EffectiveBase):
    """The values of the platform and the settings that the stages of `openroad` read as `P`."""

    #: The diode cell that `repair_antennas` inserts, or `None`: the tool finds one. The setting
    #: `antenna.diode_cell` wins over the antenna cell of the platform.
    diode_cell: Optional[str]
    #: Whether the router can insert a diode. A diode that the settings name is usable. The
    #: platform may say that its diode is not, and a platform that says nothing is trusted.
    diode_usable: bool
    #: The masters that the filler command gets (names or glob patterns): the decap cells of the
    #: setting `fill.decap_cells`, then the filler cells of the setting `fill.cells`, else those
    #: of the platform. The tuple is empty when there are none, and then the flow places no
    #: filler.
    fill_cells: tuple[str, ...]
    #: The range of layers where a via may sit inside a pin, or `None` for no range. No platform
    #: gives one, so only the setting does.
    via_in_pin: Optional[ViaInPin]
    #: How the parasitics of the final report come about: `rcx` (OpenRCX extracts them with the
    #: rules of the platform) or `estimate` (OpenROAD estimates them from the route).
    extraction: str
    #: The rules file of OpenRCX when `extraction` is `rcx`, else `None`: the file of the setting
    #: `extraction.rules_file`, else the file of the platform.
    rcx_rules: Optional[Path]
    #: Whether the flow analyzes the IR drop (the setting `ir_drop.enabled`). With no supply net
    #: that has a voltage, there is nothing to analyze.
    ir_drop: bool
    #: The voltage of each supply net that the analysis covers, in volts: the nets of the setting
    #: `ir_drop.nets` in its order, else the power nets and then the ground nets of the platform,
    #: each in the order of the platform.
    supply_voltages: tuple[tuple[str, float], ...]
    #: Whether the settings change the setup of the analysis, that is the model of the voltage
    #: sources or the power of an instance. Then the templates define and call the procedure
    #: `xeda_ir_setup`.
    ir_setup: bool
    #: The files that the analysis writes for each net besides the error report: `voltage` (the
    #: voltage of each instance), `em` (the current in each segment) and `spice` (the SPICE
    #: netlist of the grid), as the settings switch them on.
    ir_drop_kinds: tuple[str, ...]
    #: The supply net that gets the decap cells, or `None` when the setting `ir_drop.decap` is off.
    decap_net: Optional[str]
    #: The CDL files of the cells that the platform gives. With none, the flow writes no circuit
    #: description.
    cdl_masters: tuple[Path, ...]

    @property
    def supply_nets(self) -> tuple[str, ...]:
        """The names of the supply nets that have a voltage, in the order of `supply_voltages`."""
        return tuple(net for net, _ in self.supply_voltages)

    @staticmethod
    def ir_drop_file(kind: str, number: int) -> str:
        """Return the name of the file of `kind` (`error`, `voltage`, `em` or `spice`) for the
        net number `number`, counted from 1 in the order of `supply_nets`. The names do not come
        from the names of the nets."""
        return IR_DROP_FILES[kind].format(n=number)

    @property
    def ir_drop_reports(self) -> tuple[str, ...]:
        """The error reports of the IR drop analysis: one for each supply net,
        `reports/irdrop_<n>.rpt`. None if the analysis is off."""
        return self.ir_drop_files_of("error")

    def ir_drop_files_of(self, kind: str) -> tuple[str, ...]:
        """The files of `kind` for all the nets, or none if the analysis is off."""
        if not self.ir_drop:
            return ()
        return tuple(
            self.ir_drop_file(kind, number) for number, _ in enumerate(self.supply_nets, 1)
        )

    @property
    def ir_drop_files(self) -> tuple[str, ...]:
        """Every file of the IR drop analysis, for all the nets: the error reports first."""
        return tuple(
            name for kind in ("error", *self.ir_drop_kinds) for name in self.ir_drop_files_of(kind)
        )


def resolve_extraction(method: str, *, has_rules: bool, platform: str) -> str:
    """Return `rcx` or `estimate`, the extraction that the setting `extraction.method` asks for
    on a platform that has OpenRCX rules (`has_rules`) or none. `auto` is `rcx` where there are
    rules. Raise `FlowSettingsException` for `rcx` on a platform without rules, since OpenRCX
    cannot extract without them."""
    if method == "rcx" and not has_rules:
        raise FlowSettingsException(
            f"`extraction.method: rcx` needs the rules file of OpenRCX, and the platform "
            f"{platform!r} gives none (`files.rcx_rules` in its residue file): set "
            "`extraction.method` to `estimate` or `auto`, or add the rules file to the residue file"
        )
    if method == "auto":
        return "rcx" if has_rules else "estimate"
    return method


def effective(settings: "Openroad.Settings", model: PlatformModel) -> EffectiveFinal:
    """Return the values of the templates of `openroad` for `settings` and the platform `model`.
    The function reads only its arguments and changes neither."""
    cells = settings.fill.cells
    decap = settings.fill.decap_cells
    given_diode = settings.antenna.diode_cell
    given_rules = settings.extraction.rules_file
    rules = [given_rules] if given_rules is not None else model.files.get("rcx_rules", [])
    extraction = resolve_extraction(
        settings.extraction.method, has_rules=bool(rules), platform=model.name
    )
    supply = model.supply
    supply_voltages = (
        tuple((net, volts.to("V")) for net, volts in settings.ir_drop.nets.items())
        if settings.ir_drop.nets is not None
        else tuple(
            (net, float(volts))
            for nets in (supply.power_nets, supply.ground_nets)
            for net, volts in nets.items()
        )
    )
    covered = [net for net, _ in supply_voltages]
    for net in settings.ir_drop.vsrc_files:
        if settings.ir_drop.enabled and net not in covered:
            raise FlowSettingsException(
                f"`ir_drop.vsrc_files` names the net {net!r}, which the analysis does not cover "
                f"(the nets are {', '.join(covered) or 'none'}): name a net of the analysis, "
                "or give the nets with `ir_drop.nets`"
            )
    decap_net = None
    if settings.ir_drop.enabled and settings.ir_drop.decap.enabled:
        decap_net = settings.ir_drop.decap.net or (next(iter(supply.power_nets), None))
        if decap_net not in covered:
            raise FlowSettingsException(
                f"`ir_drop.decap` needs the net {decap_net!r}, which the analysis does not cover "
                f"(the nets are {', '.join(covered) or 'none'}): name a net of the analysis with "
                "`ir_drop.decap.net`, or give the nets with `ir_drop.nets`"
            )
    return EffectiveFinal(
        **base_values(settings, model),
        diode_cell=given_diode or (model.antenna_cell.cell if model.antenna_cell else None),
        diode_usable=given_diode is not None or model.diode_usable is not False,
        fill_cells=(*decap, *(model.fill_cells if cells is None else cells)),
        via_in_pin=settings.detailed_route.via_in_pin,
        extraction=extraction,
        rcx_rules=rules[0] if extraction == "rcx" else None,
        ir_drop=settings.ir_drop.enabled,
        supply_voltages=supply_voltages,
        ir_setup=bool(settings.ir_drop.instance_power)
        or any(
            getattr(settings.ir_drop.source, name) is not None
            for name in type(settings.ir_drop.source).model_fields
        ),
        ir_drop_kinds=tuple(
            kind
            for kind, on in (
                ("voltage", settings.ir_drop.instance_voltages),
                ("em", settings.ir_drop.electromigration),
                ("spice", settings.ir_drop.spice),
            )
            if on
        ),
        decap_net=decap_net,
        cdl_masters=tuple(model.files.get("cdl", [])),
    )


def ndr_words(values: Dict[str, Length | str]) -> list[str]:
    """Return the widths or the spacings of a non-default rule as the flat list that `create_ndr`
    reads: a layer or a range of layers, then its value, and so on. A length becomes the Tcl text
    of its number of micrometers, and a factor (`*2`) stays as it is."""
    words: list[str] = []
    for layers, value in values.items():
        words += [layers, value if isinstance(value, str) else to_micrometers(value)]
    return words


def decap_words(cells: Dict[str, float]) -> list[str]:
    """Return the decap cells as the flat list that `insert_decap -cells` reads: a cell name or a
    pattern, then its capacitance, and so on. A capacitance becomes the Tcl text of the number."""
    words: list[str] = []
    for cell, capacitance in cells.items():
        words += [cell, tcl_number(capacitance)]
    return words


def snapshot_files(route: DetailedRouteGroup) -> list[str]:
    """Return the names of the databases that the router saves with `snapshots`: `drt_iter<n>.odb`
    for each iteration `n` from 0 to `end_iteration`, in the working directory. The router saves
    one only after an iteration that leaves violations. The list is empty if the setting is off."""
    if not route.snapshots:
        return []
    return [f"drt_iter{n}.odb" for n in range(route.end_iteration + 1)]


def drc_iteration_reports(route: DetailedRouteGroup, drc_report: str) -> list[str]:
    """Return the names of the files that the detailed route writes next to the report `drc_report`
    while it works. The router writes `<report>-<n>.rpt` after every `drc_report_step`-th iteration
    (the iterations are 1 to `end_iteration`), and none if the step is 0."""
    step = route.drc_report_step
    if step == 0:
        return []
    return [f"{drc_report}-{n}.rpt" for n in range(step, route.end_iteration + 1, step)]


class Openroad(OpenroadStage):
    """Detail-route a globally routed design with OpenROAD, then write the final outputs.

    The flow runs OpenROAD in two processes, and in a third one only with `save_images`.

    The first process reads the database of the global route and its SDC file. It routes the
    design in detail, then repairs the antenna violations of the route with diodes and routes the
    changed nets again, until no violation is left or the iterations end. Last it places the filler
    cells. It writes the working database `route.odb`, its SDC file `route.sdc`, and the reports of
    the route: the design rule violations, the log of the maze router and the antenna violations.

    The second process reads these two files and writes the final database, the SDC file, the DEF
    file, the gate-level netlist without physical-only cells and, where OpenRCX extracts them, the
    parasitics. It reports the timing, the power, the area and the IR drop. The third process
    saves images of the routed design. With `write_gds`, KLayout writes the GDSII layout
    from the DEF file.

    The routing layers and the layer adjustments stay in the database from `openroad_place`. The
    wire RC and the cells that the resizer must not use are no part of the database. Each process
    sets them again from the platform and the settings."""

    executables = OpenroadStage.executables + ("klayout",)

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "fmax",
        "setup_violations",
        "hold_violations",
        "utilization",
        "power",
        area=MetricSpec(
            "Area of the standard cells after the detailed route, without the filler cells, in "
            "square micrometers.",
            "number",
        ),
        hold_tns=MetricSpec(
            "Total negative hold slack after the detailed route, from the final parasitics (see "
            "`parasitics`).",
            "number",
            "ns",
        ),
        clock_skew_setup=MetricSpec(
            "Clock skew of the setup check after the detailed route.", "number", "ns"
        ),
        clock_skew_hold=MetricSpec(
            "Clock skew of the hold check after the detailed route.", "number", "ns"
        ),
        max_slew_violations=MetricSpec(
            "Number of max slew violations after the detailed route.", "integer"
        ),
        max_cap_violations=MetricSpec(
            "Number of max capacitance violations after the detailed route.", "integer"
        ),
        max_fanout_violations=MetricSpec(
            "Number of max fanout violations after the detailed route.", "integer"
        ),
        core_area=MetricSpec("Area of the core, in square micrometers.", "number"),
        die_area=MetricSpec("Area of the die, in square micrometers.", "number"),
        power_internal=MetricSpec(
            "Internal power of the cells, as OpenROAD estimates it.", "number", "W"
        ),
        power_switching=MetricSpec(
            "Switching power of the nets, as OpenROAD estimates it.", "number", "W"
        ),
        power_leakage=MetricSpec(
            "Leakage power of the cells, as OpenROAD estimates it.", "number", "W"
        ),
        drc_errors=MetricSpec(
            "Number of design rule violations that the detailed route leaves.", "integer"
        ),
        antenna_violating_nets=MetricSpec(
            "Number of nets with an antenna violation after the detailed route and its repair.",
            "integer",
        ),
        antenna_violating_pins=MetricSpec(
            "Number of pins with an antenna violation after the detailed route and its repair.",
            "integer",
        ),
        antenna_diodes=MetricSpec(
            "Number of diodes that the repair of the antenna violations inserted. Absent if the "
            "platform has no diode that the router can use.",
            "integer",
        ),
        wirelength=MetricSpec("Total wire length of the detailed route.", "number", "um"),
        vias=MetricSpec("Number of vias of the detailed route.", "integer"),
        instances=MetricSpec(
            "Number of standard cell instances, without the filler cells.", "integer"
        ),
        fill_cells=MetricSpec("Number of filler cell instances.", "integer"),
        tap_cells=MetricSpec("Number of tap cell instances.", "integer"),
        warnings=MetricSpec(
            "Number of warnings that OpenROAD printed in the final report.", "integer"
        ),
        errors=MetricSpec("Number of errors that OpenROAD printed in the final report.", "integer"),
        parasitics=MetricSpec(
            "How the parasitics of the final timing, power and IR drop come about: `rcx` "
            "(OpenRCX extracted them with the rules of the platform) or `estimate` (OpenROAD "
            "estimated them from the route).",
            "text",
        ),
        platform=MetricSpec(
            "The platform that the design is routed on: `name`, the PDK `provider` and "
            "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or "
            "user) that gave each value of the platform model.",
            "object",
        ),
    )

    #: The design sources it can take as its inputs: the database and constraints of the global
    #: route.
    design_source_types = frozenset(
        {SourceType.OpenroadDbGlobalRouted, SourceType.OpenroadSdcGlobalRouted}
    )

    metrics_files = ("metrics_route.json", "metrics_final.json")
    reports = {
        "drc": "reports/drc.rpt",
        "maze": "reports/maze.log",
        "guide_coverage": "reports/guide_coverage.csv",
        "antennas": "reports/antennas.rpt",
        "timing": "reports/timing.rpt",
        "clock_skew": "reports/clock_skew.rpt",
        "power": "reports/power.rpt",
        "check_setup": "reports/check_setup.rpt",
    }
    #: The reports of the first process, by key of `reports`. The second process writes the others
    #: that the settings switch on.
    route_reports: ClassVar[tuple[str, ...]] = ("drc", "maze", "guide_coverage", "antennas")
    #: The files that the first process writes for the process after it, by role: the database and
    #: its SDC file. They are no declared outputs; the flow writes its outputs at the end.
    working_files: ClassVar[Dict[str, str]] = {"odb": "route.odb", "sdc": "route.sdc"}
    #: The declared outputs that every run of the second process writes, by output name, in the
    #: run directory. `spef` follows when OpenRCX extracts the parasitics.
    final_files: ClassVar[Dict[str, str]] = {
        "odb": "final.odb",
        "sdc": "final.sdc",
        "def_file": "final.def",
        "netlist": "final.v",
        "sdf": "final.sdf",
        "abstract_lef": "final.lef",
    }
    #: The files of the second process that exist only where a setting or the platform asks for
    #: them, by output name: the circuit description (the platform has CDL files) and the timing
    #: model (`write_timing_model`).
    optional_files: ClassVar[Dict[str, str]] = {"cdl": "final.cdl", "timing_model": "final.lib"}
    #: The parasitics file that the second process writes when OpenRCX extracts them.
    spef_file: ClassVar[str] = "final.spef"
    #: The images of the third process, in the directory `images` of the run directory.
    image_files: ClassVar[tuple[str, ...]] = tuple(f"images/{name}.webp" for name in IMAGE_NAMES)
    #: The GDSII file that KLayout writes with `write_gds`, and the technology file that the flow
    #: writes for it, from the technology file of the platform.
    gds_file: ClassVar[str] = "final.gds"
    klayout_tech_file: ClassVar[str] = "klayout.lyt"

    class Settings(OpenroadStage.Settings):
        """The settings of the last flow."""

        removed_settings: ClassVar[Dict[str, str]] = {
            **OpenroadStage.Settings.removed_settings,
            # refused here: the replacement, where there is one, is a setting of `openroad_place`
            "multi_corner": NoReplacement("multi-corner analysis is not supported"),
            "input_delay_frac": "`flows.openroad_place.input_delay_frac`",
            "output_delay_frac": "`flows.openroad_place.output_delay_frac`",
            "input_delay": "`flows.openroad_place.input_delay`, a time with its unit, such as "
            "`100 ps`",
            "output_delay": "`flows.openroad_place.output_delay`, a time with its unit, such as "
            "`100 ps`",
            "sdc_files": "`flows.openroad_place.sdc_files`",
            "core_utilization": "`flows.openroad_place.floorplan.core_utilization_percent`",
            "core_aspect_ratio": "`flows.openroad_place.floorplan.aspect_ratio`",
            "core_margin": "`flows.openroad_place.floorplan.core_margin`",
            "core_area": "`flows.openroad_place.floorplan.core_area`, with "
            "`flows.openroad_place.floorplan.sizing` set to `area`",
            "die_area": "`flows.openroad_place.floorplan.die_area`, with "
            "`flows.openroad_place.floorplan.sizing` set to `area`",
            "floorplan_def": "`flows.openroad_place.floorplan.def_file`, with "
            "`flows.openroad_place.floorplan.sizing` set to `def`",
            "io_constraints": "`flows.openroad_place.io.constraints`",
            "place_density": "`flows.openroad_place.global_placement.density.value`, with "
            "`flows.openroad_place.global_placement.density.mode` set to `fixed`",
            "place_density_lb_addon": "`flows.openroad_place.global_placement.density.headroom`, "
            "with `flows.openroad_place.global_placement.density.mode` set to `auto`",
            "place_pins_args": "the typed fields of `flows.openroad_place.io`, such as "
            "`flows.openroad_place.io.min_distance`",
            "global_placement_args": "the typed fields of `flows.openroad_place.global_placement`, "
            "such as `flows.openroad_place.global_placement.timing_driven`",
            "min_phi_coef": "`flows.openroad_place.global_placement.nesterov.min_phi_coef`",
            "max_phi_coef": "`flows.openroad_place.global_placement.nesterov.max_phi_coef`",
            "gpl_timing_driven": "`flows.openroad_place.global_placement.timing_driven.enabled`",
            "gpl_routability_driven": "`flows.openroad_place.global_placement.routability_driven"
            ".enabled`",
            "tie_separation": "`flows.openroad_place.tie.separation`",
            "global_routing_layer_adjustment": "`flows.openroad_place.routing.layer_adjustments`",
            "io_place_random": NoReplacement("`place_pins -random` is obsolete in OpenROAD"),
            "update_sdc_margin": NoReplacement("Xeda writes no tuning SDC"),
            "results_dir": NoReplacement(
                "each flow writes its files at fixed names in its run directory"
            ),
            "write_metrics": NoReplacement(
                "each flow writes its files at fixed names in its run directory and reads its "
                "own metrics"
            ),
            "exit": NoReplacement("the flow always exits OpenROAD when its script ends"),
            "gui": NoReplacement("the flow runs OpenROAD in batch mode"),
            "copy_platform_files": NoReplacement(
                "the flow reads the platform files where they are installed"
            ),
            "extra_liberty_files": NoReplacement("macros are not supported yet"),
            "rtlmp_flow": NoReplacement("macros are not supported yet"),
            "rtlmp_config_file": NoReplacement("macros are not supported yet"),
            "macro_placement_file": NoReplacement("macros are not supported yet"),
            "macro_wrappers": NoReplacement("macros are not supported yet"),
            "footprint": NoReplacement("pad rings are not supported yet"),
            "footprint_def": NoReplacement("pad rings are not supported yet"),
            "footprint_tcl": NoReplacement("pad rings are not supported yet"),
            "sig_map_file": NoReplacement("pad rings are not supported yet"),
            "floorplan_tcl": NoReplacement("Tcl hooks are not supported"),
            "post_pdn_tcl": NoReplacement("Tcl hooks are not supported"),
            "post_detailed_route_tcl": NoReplacement("Tcl hooks are not supported"),
            "make_tracks_tcl": NoReplacement("the platform gives the routing tracks"),
            "resynth_for_timing": NoReplacement("OpenROAD-flow-scripts removed resynthesis"),
            "resynth_for_area": NoReplacement("OpenROAD-flow-scripts removed resynthesis"),
            "db_process_node": NoReplacement("the platform gives the process node"),
            "density_fill": NoReplacement("density fill is not supported yet"),
            # refused here: the replacement is a setting of `openroad_cts`, `openroad_grt` or
            # `openroad`
            "hold_slack_margin": "`flows.openroad_cts.repair.hold_margin`, or "
            "`flows.openroad_grt.repair.timing.hold_margin`",
            "setup_slack_margin": "`flows.openroad_cts.repair.setup_margin`, or "
            "`flows.openroad_grt.repair.timing.setup_margin`",
            "cts_buf_distance": "`flows.openroad_cts.tree.buffer_distance`, with "
            "`flows.openroad_cts.tree.style` set to `simple_segments`",
            "cts_cluster_size": "`flows.openroad_cts.sink_clustering.size`, with "
            "`flows.openroad_cts.sink_clustering.mode` set to `explicit`",
            "cts_cluster_diameter": "`flows.openroad_cts.sink_clustering.max_diameter`, with "
            "`flows.openroad_cts.sink_clustering.mode` set to `explicit`",
            "congestion_iterations": "`flows.openroad_grt.congestion.iterations`",
            "repair_antennas": "`flows.openroad_grt.antenna.repair` and "
            "`flows.openroad.antenna.repair`",
            "detailed_route_or_seed": "`flows.openroad.detailed_route.reroute_seed`",
            "detailed_route_or_k": "`flows.openroad.detailed_route.reroute_swap_factor`",
            "detailed_route_additional_args": "`flows.openroad.detailed_route.extra_args`",
            "disable_via_gen": "`flows.openroad.detailed_route.via_generation`, set to `false`",
            "repair_pdn_via_layer": "`flows.openroad.detailed_route.repair_pdn_vias_layer`",
            "final_irdrop_analysis": "`flows.openroad.ir_drop.enabled`",
            "generate_gds": "`flows.openroad.write_gds`",
            "gds_seal_file": "`flows.openroad.gds.seal_ring`",
            "write_cdl": NoReplacement(
                "the flow writes `final.cdl` when the platform has CDL files"
            ),
            "cdl_masters_file": NoReplacement("the platform gives the CDL files of its cells"),
            # refused here: the replacement is a setting of `yosys`
            "blocks": "`flows.yosys.black_box`",
            "optimize": "`flows.yosys.abc.strategy`",
            "abc_driver_cell": "`flows.yosys.abc.driver_cell`",
            "abc_load_in_ff": "`flows.yosys.abc.load`",
        }

        write_gds: bool = Field(
            False,
            description="Write the GDSII layout of the routed design with KLayout. It needs the "
            "KLayout files and the GDSII files of the platform.",
            json_schema_extra=tier("common"),
        )
        save_images: bool = Field(
            False,
            description="Save images of the routed design in the directory `images` of the run "
            "directory. It needs an OpenROAD build that includes the GUI, and the Qt platform "
            "plugin `offscreen`. OpenROAD v2.0-26132-g6b305a712 on macOS has no such plugin and stops "
            "with GUI-0077.",
            json_schema_extra=tier("advanced"),
        )
        netlist_power_pins: bool = Field(
            False,
            description="Write the power and ground pins in the netlist `netlist` "
            "(`write_verilog -include_pwr_gnd`). A netlist for LVS needs them, and a simulation "
            "does not.",
            json_schema_extra=tier("advanced"),
        )
        write_timing_model: bool = Field(
            False,
            description="Write a Liberty timing model of the routed design as `timing_model` "
            "(`write_timing_model`), for the timing analysis of a design that uses the block. "
            "After the reports, the flow sets the source latency of the clocks to 0 and writes "
            "the model, so the reports and the verdict read the constraints of the design.",
            json_schema_extra=tier("advanced"),
        )
        fail_timing: bool = Field(
            True,
            description="The flow fails if the routed design does not meet timing.",
            json_schema_extra=tier("common"),
        )
        fail_drc: bool = Field(
            True,
            description="The flow fails if the detailed route leaves design rule violations.",
            json_schema_extra=tier("common"),
        )
        fail_antenna: bool = Field(
            True,
            description="The flow fails if antenna violations remain after the repair.",
            json_schema_extra=tier("common"),
        )
        detailed_route: DetailedRouteGroup = Field(
            default_factory=DetailedRouteGroup,
            description="The detailed router.",
        )
        ndr: NdrGroup = Field(
            default_factory=NdrGroup,
            description="The non-default routing rules for the detailed route.",
        )
        antenna: FinalAntennaGroup = Field(
            default_factory=FinalAntennaGroup,
            description="The repair of the antenna violations of the detailed route.",
        )
        fill: FillGroup = Field(
            default_factory=FillGroup,
            description="The filler cells that fill the gaps in the rows.",
        )
        extraction: ExtractionGroup = Field(
            default_factory=ExtractionGroup,
            description="The extraction of the parasitics of the routed design.",
        )
        reports: ReportsGroup = Field(
            default_factory=ReportsGroup,
            description="The text reports of the final process.",
        )
        ir_drop: IrDropGroup = Field(
            default_factory=IrDropGroup,
            description="The analysis of the IR drop of the power grid.",
        )
        gds: GdsGroup = Field(
            default_factory=GdsGroup,
            description="The GDSII file of the routed design.",
        )

    class Inputs(OpenroadStage.Inputs):
        odb: Path = In(
            SourceType.OpenroadDbGlobalRouted,
            producer="openroad_grt",
            output="odb",
            description="The OpenROAD database after global routing.",
        )
        sdc: Path = In(
            SourceType.OpenroadSdcGlobalRouted,
            producer="openroad_grt",
            output="sdc",
            same_producer_as="odb",
            paired=True,
            description="The SDC file written with the globally routed database. It comes from "
            "the same source as the database.",
        )

    class Outputs(OpenroadStage.Outputs):
        odb: Path = Out(
            SourceType.OpenroadDbRouted,
            description="The OpenROAD database of the routed design.",
        )
        sdc: Path = Out(
            SourceType.OpenroadSdcRouted,
            description="The SDC file of the routed design.",
        )
        def_file: Path = Out(
            SourceType.Def,
            description="The routed design in DEF format.",
        )
        netlist: Path = Out(
            SourceType.VerilogNetlist,
            description="The gate-level Verilog netlist of the routed design, without "
            "physical-only cells.",
        )
        sdf: Path = Out(
            SourceType.Sdf,
            description="The delays of the routed design in SDF format, from the final "
            "parasitics.",
        )
        abstract_lef: Path = Out(
            SourceType.Data,
            description="The abstract LEF view of the routed design, with the layers that it "
            "occupies bloated, for a design that uses the block.",
        )
        cdl: Path | None = Out(
            SourceType.Cdl,
            description="The circuit description of the routed design for LVS, written when the "
            "platform gives the CDL files of its cells.",
        )
        timing_model: Path | None = Out(
            SourceType.Liberty,
            enabled_by="write_timing_model",
            description="The Liberty timing model of the routed design.",
        )
        spef: Path | None = Out(
            SourceType.Spef,
            description="The extracted parasitics of the routed design, written when OpenRCX "
            "extracts them.",
        )
        gds: Path | None = Out(
            SourceType.Gds,
            enabled_by="write_gds",
            description="The GDSII layout of the routed design.",
        )

    # --- planning and the files of the platform -----------------------------------------------

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what the groups cannot judge alone: `extraction.method: rcx` on a platform that
        gives no OpenRCX rules, a setting of the layout file without `write_gds`, and `write_gds`
        on a platform that gives no KLayout technology file or no GDSII files. This reads the
        residue files only."""
        super().check_settings_supported(settings)
        if not isinstance(settings, cls.Settings):
            return
        problems: List[tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = []
        if not settings.write_gds:
            for leaf in differing_leaves(settings.gds, "gds"):
                problems.append(
                    (
                        leaf,
                        f"`{leaf}` has no effect with `write_gds: false`: set `write_gds: true` "
                        f"or delete `{leaf}`",
                        None,
                        "value_error",
                    )
                )
        if problems:
            raise FlowSettingsError(problems, cls.Settings)
        if settings.platform is None and settings.platform_file is None:
            return  # the missing platform is reported with the other required settings
        residue = settings.residue()
        if settings.write_gds:
            missing = [
                role
                for role in GDS_ROLES
                if not getattr(residue.files, role)
                and not (role == "gds" and settings.gds.cell_libraries)
            ]
            if missing:
                raise FlowSettingsException(
                    f"the platform {residue.name!r} gives no {' and no '.join(missing)}, which "
                    "`write_gds` needs to merge the layout: add them to `files` in its residue "
                    "file (`platform_file`), give `gds.cell_libraries` for the GDSII files, or "
                    "set `write_gds: false`"
                )
        extraction = resolve_extraction(
            settings.extraction.method,
            has_rules=bool(residue.files.rcx_rules) or settings.extraction.rules_file is not None,
            platform=residue.name,
        )
        if extraction == "estimate":
            leaves = [
                leaf
                for leaf in differing_leaves(settings.extraction, "extraction")
                if leaf != "extraction.method"
            ]
            if leaves:
                why = (
                    "`extraction.method: estimate`"
                    if settings.extraction.method == "estimate"
                    else f"the platform {residue.name!r}, which gives no rules file and so gets "
                    "an estimate from `extraction.method: auto`,"
                )
                raise FlowSettingsError(
                    [
                        (
                            leaf,
                            f"`{leaf}` is an option of OpenRCX and has no effect with {why}: set "
                            f"`extraction.method: rcx` and give the rules, or delete `{leaf}`",
                            None,
                            "value_error",
                        )
                        for leaf in leaves
                    ],
                    cls.Settings,
                )

    @property
    def platform_roles(self) -> tuple[str, ...]:
        """The roles of the platform files that the flow opens: those of every OpenROAD flow, the
        rules file of OpenRCX when the extraction is `rcx` and no setting gives another rules
        file, the CDL files of the platform, and the technology file of KLayout and the GDSII files
        with `write_gds`. It follows the settings and the residue file of the platform, so a dry
        run, which builds no platform model, gets the same roles as a run."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        residue = settings.residue()
        given_rules = settings.extraction.rules_file
        extraction = resolve_extraction(
            settings.extraction.method,
            has_rules=bool(residue.files.rcx_rules) or given_rules is not None,
            platform=residue.name,
        )
        roles: tuple[str, ...] = (*PLATFORM_ROLES, "cdl")
        if extraction == "rcx" and given_rules is None:
            roles = (*roles, "rcx_rules")
        if settings.write_gds:
            roles = (*roles, *GDS_ROLES)
        return roles

    def prepare_inputs(self) -> None:
        """Find the files of the platform, and check the rules file of the setting."""
        super().prepare_inputs()
        settings = self.settings
        assert isinstance(settings, self.Settings)
        rules = settings.extraction.rules_file
        if rules is not None and not rules.is_file():
            raise FlowSettingsException(
                f"`extraction.rules_file` names {rules}, which is not a file: give the rules "
                "file of OpenRCX, or delete `extraction.rules_file` to use the file of the "
                "platform"
            )

    # --- the values of the templates ----------------------------------------------------------

    def add_template_helpers(self) -> None:
        """Add the helpers of every OpenROAD flow, `working_files`, the names of the files that
        the first process writes for the second, and `image_files`, the names of the images."""
        super().add_template_helpers()
        self.jinja_env.globals.update(
            working_files=dict(self.working_files),
            image_files=list(self.image_files),
            ndr_words=ndr_words,
            decap_words=decap_words,
        )

    def effective(self) -> EffectiveFinal:
        """Return the values that the templates read as `P`. It runs after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        settings = self.settings
        assert isinstance(settings, self.Settings)
        return effective(settings, model)

    # --- running OpenROAD ---------------------------------------------------------------------

    def run_route(self) -> None:
        """Run the first process: the detailed route, the antenna repair and the filler cells. It
        writes `route.odb`, `route.sdc`, `metrics_route.json` and the reports of the route, among
        them the reports of the iterations of the router. A database of an earlier failure is
        removed first."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        self.run_openroad(
            "openroad_route",
            "metrics_route.json",
            [
                *self.working_files.values(),
                *(self.reports[name] for name in self.route_reports),
                *drc_iteration_reports(settings.detailed_route, self.reports["drc"]),
                *snapshot_files(settings.detailed_route),
                self.failed_odb,
            ],
        )

    def set_final_outputs(self) -> None:
        """Name the declared outputs: `final.odb`, `final.sdc`, `final.def`, `final.v`, `final.sdf`
        and `final.lef`, which the second process writes, `final.spef` when OpenRCX extracts the
        parasitics, `final.cdl` when the platform gives CDL files, `final.lib` with
        `write_timing_model`, and `final.gds` with `write_gds`, which KLayout writes. All are
        files of the run directory. `run_final()` calls it before it renders the driver."""
        run_path = Path(self.run_path)
        for name, file in self.final_files.items():
            setattr(self.outputs, name, run_path / file)
        values = self.effective()
        extracted = values.extraction == "rcx"
        setattr(self.outputs, "spef", run_path / self.spef_file if extracted else None)
        settings = self.settings
        assert isinstance(settings, self.Settings)
        setattr(self.outputs, "gds", run_path / self.gds_file if settings.write_gds else None)
        wanted = {"cdl": bool(values.cdl_masters), "timing_model": settings.write_timing_model}
        for name, file in self.optional_files.items():
            setattr(self.outputs, name, run_path / file if wanted[name] else None)

    def final_report_files(self, reports: ReportsGroup) -> list[str]:
        """Return the reports that the second process writes with these settings, in the order of
        `reports`: the timing paths, the clock skew, the power and the check of the setup."""
        wanted = {
            "timing": reports.timing.enabled,
            "clock_skew": reports.clock_skew,
            "power": reports.power,
            "check_setup": reports.check_setup,
        }
        return [self.reports[name] for name, on in wanted.items() if on]

    def run_final(self) -> None:
        """Run the second process: it reads `route.odb` and `route.sdc`, writes the final
        database, SDC file, DEF file and netlist, extracts the parasitics, analyzes the IR drop and
        reports the metrics into `metrics_final.json`. It writes a report of the IR drop for each
        supply net that has a voltage.

        OpenRCX writes `<top>.totCap` in the working directory. The flow makes that name ready
        before the process starts, and removes the file after the process, through the run
        directory, whether the process succeeded or not."""
        self.set_final_outputs()
        values = self.effective()
        settings = self.settings
        assert isinstance(settings, self.Settings)
        writes: list[str | Path] = [
            *(
                getattr(self.outputs, name)
                for name in (*self.final_files, *self.optional_files)
                if getattr(self.outputs, name) is not None
            ),
            *values.ir_drop_files,
            *self.final_report_files(settings.reports),
        ]
        extracted = values.extraction == "rcx"
        top = self.design.rtl.top
        totcap = f"{top}.totCap" if extracted and top else None
        if extracted:
            writes.append(getattr(self.outputs, "spef"))
        if totcap is not None:
            writes.append(totcap)
        try:
            self.run_openroad("openroad_final", "metrics_final.json", [*writes, self.failed_odb])
        finally:
            if totcap is not None:
                self.run_directory.remove(totcap)

    def _run_processes(self) -> None:
        """Run the first process, then the second."""
        self.run_route()
        self.run_final()

    def run_images(self) -> None:
        """Run the third process: it reads the final database and saves the images of the routed
        design in the directory `images` of the run directory. The process gets the environment
        variable `QT_QPA_PLATFORM=offscreen`, so that it needs no display, and the other processes
        do not. It stops with an error when this OpenROAD has no graphical interface, and the
        run fails then and leaves no trace."""
        self.set_final_outputs()
        self.run_openroad(
            "openroad_images",
            "metrics_images.json",
            self.image_files,
            env={"QT_QPA_PLATFORM": "offscreen"},
        )

    def klayout(self) -> Tool:
        """Return the `klayout` tool of this flow."""
        tool = getattr(self, "_klayout_tool", None)
        if tool is None:
            tool = self._klayout_tool = Tool("klayout", self, version_flag="-v")
        return tool

    def write_klayout_tech(self) -> Path:
        """Write the technology file of KLayout into the run directory and return its path.

        The file is the one of the platform with its list of LEF files replaced by the technology
        LEF file and the LEF files of the cells, as absolute paths. Xeda removes every
        `<lef-files>` element of the file (an element may span lines or be empty) and writes the
        list once, where the first element was. A file that has a `<lefdef>` section and no
        element gets the list at the end of the section. Each path is escaped as XML text, so a
        path with `&` or `<` stays one element. KLayout reads the DEF file of the routed design
        with these.
        Raise `FlowSettingsException` if the file of the platform has neither a `<lef-files>`
        element nor a `<lefdef>` section."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        template = model.files["klayout_tech"][0]
        values = self.effective()
        lefs = [*([values.tech_lef] if values.tech_lef is not None else []), *values.cell_lefs]
        text = klayout_tech_with_lef_files(template.read_text(), lefs)
        if text is None:
            raise FlowSettingsException(
                f"the KLayout technology file {template} of the platform {model.name!r} has no "
                "`<lef-files>` element and no `<lefdef>` section, so KLayout cannot read the LEF "
                "files with the DEF file: add the element to the file, or set `write_gds: false`"
            )
        path = self.run_directory.writable(self.klayout_tech_file)
        with replacing_file(path) as handle:
            handle.write(text)
        return path

    def run_gds(self) -> None:
        """Merge the routed design with the GDSII files of the cells into the layout
        `final.gds`, with KLayout and the script `data/def2stream.py`. The script reads the DEF
        file `final.def` with the technology file that `write_klayout_tech()` wrote. It fails
        when a cell of the design has no GDSII cell or a cell has no parent. Raise
        `ExecutableNotFound` when KLayout is not installed."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        self.set_final_outputs()
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        top = self.design.rtl.top
        assert top, "the design names its top module"
        seal_ring = settings.gds.seal_ring
        technology = self.write_klayout_tech()
        output = self._guard_write(self.gds_file)
        log_file = self.run_directory.writable("klayout.log")
        script = Path(str(resources.files("xeda.flows.openroad").joinpath("data", "def2stream.py")))
        libraries = [*model.files.get("gds", []), *settings.gds.cell_libraries]
        names = {
            "design_name": top,
            "in_def": getattr(self.outputs, "def_file"),
            "in_files": "\n".join(str(path) for path in libraries),
            "seal_file": "" if seal_ring is None else seal_ring,
            "out_file": output,
            "tech_file": technology,
            "layer_map": settings.gds.layer_map or "",
            "allow_empty": settings.gds.allow_empty or "",
            "conflict_resolution": CONFLICT_RESOLUTIONS.get(
                settings.gds.conflict_resolution or "", ""
            ),
            "read_lef_with_def": tri_state(settings.gds.lefdef_options.read_lef_with_def),
            "net_properties": tri_state(settings.gds.lefdef_options.net_properties),
            "instance_properties": tri_state(settings.gds.lefdef_options.instance_properties),
            "pin_properties": tri_state(settings.gds.lefdef_options.pin_properties),
            "write_timestamps": tri_state(settings.gds.timestamps),
        }
        defines: List[str] = []
        for name, value in names.items():
            defines += ["-rd", f"{name}={value}"]
        try:
            self.klayout().run(
                "-zz",
                *defines,
                "-r",
                script,
                tee=log_file,
                merge_stderr=True,
            )
        except NonZeroExitCode as error:
            report = self.report_file(log_file, optional=True)
            found = failure_lines(report.read_text(errors="replace")) if report else []
            message = f"{self.name}: KLayout stopped while it merged the layout"
            raise NonZeroExitCode(
                error.command_args,
                error.exit_code,
                f"{message}; see klayout.log" + (": " + " | ".join(found) if found else ""),
            ) from None

    def run(self) -> None:
        """Run the processes of the flow: the detailed route and the final outputs, then the
        images with `save_images`, then the merge of the layout with `write_gds`."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        self._run_processes()
        if settings.save_images:
            self.run_images()
        if settings.write_gds:
            self.run_gds()

    # --- the results --------------------------------------------------------------------------

    def parse_reports(self) -> bool:
        """Read the metrics of the two processes and fill the results. Return False if a metrics
        file is missing or holds no JSON object, or if a value that a verdict reads is missing or
        invalid, whatever the settings `fail_timing`, `fail_drc` and `fail_antenna` say. A result
        that no verdict reads is left out when its metric is absent, and a metric that is present
        and invalid fails the parse."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
        self.results["parasitics"] = self.effective().extraction
        run_path = Path(self.run_path)
        route_file, final_file = (run_path / name for name in self.metrics_files)
        route, final = self.read_metrics(route_file.name), self.read_metrics(final_file.name)
        missing = [
            file for file, found in ((route_file, route), (final_file, final)) if found is None
        ]
        for file in missing:
            log.error("OpenROAD wrote no usable metrics file: %s", file)
        if missing or route is None or final is None:
            return False
        try:
            found = final_results(
                route,
                final,
                time_unit=platform_time_unit(model),
                route_file=route_file,
                final_file=final_file,
            )
        except MetricsError as error:
            log.error("%s", error)
            return False
        self.results.update(found)
        return True

    def check_results(self) -> bool:
        """Reject the run for each failed judgment whose check is on: timing not met
        (`fail_timing`: the worst setup slack or the worst hold slack is negative), design rule
        violations left by the route (`fail_drc`), and antenna violations left after the repair
        (`fail_antenna`). One rejection names every cause. The numbers stay in the results either
        way. Only the values that `parse_reports()` stored count."""
        ok = super().check_results()
        settings = self.settings
        assert isinstance(settings, self.Settings)
        results = self.results
        causes: list[str] = []
        if settings.fail_timing:
            slacks = [
                f"worst {kind} slack {results[name]:g} ns"
                for kind, name in (("setup", "worst_slack"), ("hold", "whs"))
                if name in results and results[name] < 0
            ]
            if slacks:
                causes.append("timing not met: " + ", ".join(slacks))
        drc = results.get("drc_errors")
        if settings.fail_drc and drc:
            causes.append(f"the detailed route leaves {drc} design rule violations")
        antenna = results.get("antenna_violating_nets")
        if settings.fail_antenna and antenna:
            causes.append(f"{antenna} nets have antenna violations after the repair")
        if not causes:
            return ok
        self.reject("; ".join(causes))
        return False
