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
from typing import ClassVar, Dict, List, Optional

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import Flow, In, Out, describe_results
from ...flow.flow import FlowSettingsError, FlowSettingsException
from ...platforms.model import PlatformModel
from ...tool import NonZeroExitCode, Tool
from ...utils import replacing_file
from .base import (
    PLATFORM_ROLES,
    EffectiveBase,
    OpenroadStage,
    base_values,
    failure_lines,
    platform_time_unit,
)
from .final_groups import (
    DetailedRouteGroup,
    ExtractionGroup,
    FillGroup,
    FinalAntennaGroup,
    GdsGroup,
    IrDropGroup,
    ViaInPin,
)
from .groups import differing_leaves
from .metrics import MetricsError, final_results

__all__ = [
    "EffectiveFinal",
    "Openroad",
    "drc_iteration_reports",
    "effective",
    "resolve_extraction",
]

log = logging.getLogger(__name__)

#: The roles of the platform files that the merge of the layout reads: the technology file of
#: KLayout, which names how it reads the DEF and the LEF files, and the GDSII files of the cells.
GDS_ROLES = ("klayout_tech", "gds")
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


@dataclass(frozen=True)
class EffectiveFinal(EffectiveBase):
    """The values of the platform and the settings that the stages of `openroad` read as `P`."""

    #: The diode cell that `repair_antennas` inserts, or `None`: the tool finds one.
    diode_cell: Optional[str]
    #: Whether the router can insert a diode. The platform may say that its diode is not usable,
    #: and a platform that says nothing is trusted.
    diode_usable: bool
    #: The filler cells (names or glob patterns): those of the setting `fill.cells`, else those of
    #: the platform. The tuple is empty when there are none, and then the flow places no filler.
    fill_cells: tuple[str, ...]
    #: The range of layers where a via may sit inside a pin, or `None` for no range. No platform
    #: gives one, so only the setting does.
    via_in_pin: Optional[ViaInPin]
    #: How the parasitics of the final report come about: `rcx` (OpenRCX extracts them with the
    #: rules of the platform) or `estimate` (OpenROAD estimates them from the route).
    extraction: str
    #: The rules file of OpenRCX when `extraction` is `rcx`, else `None`.
    rcx_rules: Optional[Path]
    #: Whether the flow analyzes the IR drop (the setting `ir_drop.enabled`). With no supply net
    #: that has a voltage, there is nothing to analyze.
    ir_drop: bool
    #: The voltage of each supply net that the platform gives, in volts: the power nets, then the
    #: ground nets, each in the order of the platform.
    supply_voltages: tuple[tuple[str, float], ...]

    @property
    def supply_nets(self) -> tuple[str, ...]:
        """The names of the supply nets that have a voltage, in the order of `supply_voltages`."""
        return tuple(net for net, _ in self.supply_voltages)

    @property
    def ir_drop_reports(self) -> tuple[str, ...]:
        """The reports of the IR drop analysis: one for each supply net, `reports/irdrop_<n>.rpt`
        with `n` counted from 1 in the order of `supply_nets`. None if the analysis is off. The
        names do not come from the names of the nets."""
        if not self.ir_drop:
            return ()
        return tuple(f"reports/irdrop_{number}.rpt" for number, _ in enumerate(self.supply_nets, 1))


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
    rules = model.files.get("rcx_rules", [])
    extraction = resolve_extraction(
        settings.extraction.method, has_rules=bool(rules), platform=model.name
    )
    supply = model.supply
    return EffectiveFinal(
        **base_values(settings, model),
        diode_cell=model.antenna_cell.cell if model.antenna_cell is not None else None,
        diode_usable=model.diode_usable is not False,
        fill_cells=tuple(model.fill_cells if cells is None else cells),
        via_in_pin=settings.detailed_route.via_in_pin,
        extraction=extraction,
        rcx_rules=rules[0] if extraction == "rcx" else None,
        ir_drop=settings.ir_drop.enabled,
        supply_voltages=tuple(
            (net, float(volts))
            for nets in (supply.power_nets, supply.ground_nets)
            for net, volts in nets.items()
        ),
    )


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

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "Fmax",
        "setup_violations",
        "hold_violations",
        "utilization",
        "power",
        area="Area of the standard cells after the detailed route, without the filler cells, in "
        "square micrometers.",
        hold_tns="Total negative hold slack (ns) after the detailed route, from the final "
        "parasitics (see `parasitics`).",
        clock_skew_setup="Clock skew (ns) of the setup check after the detailed route.",
        clock_skew_hold="Clock skew (ns) of the hold check after the detailed route.",
        max_slew_violations="Number of max slew violations after the detailed route.",
        max_cap_violations="Number of max capacitance violations after the detailed route.",
        max_fanout_violations="Number of max fanout violations after the detailed route.",
        core_area="Area of the core, in square micrometers.",
        die_area="Area of the die, in square micrometers.",
        power_internal="Internal power of the cells in watts, as OpenROAD estimates it.",
        power_switching="Switching power of the nets in watts, as OpenROAD estimates it.",
        power_leakage="Leakage power of the cells in watts, as OpenROAD estimates it.",
        drc_errors="Number of design rule violations that the detailed route leaves.",
        antenna_violating_nets="Number of nets with an antenna violation after the detailed route "
        "and its repair.",
        antenna_violating_pins="Number of pins with an antenna violation after the detailed route "
        "and its repair.",
        antenna_diodes="Number of diodes that the repair of the antenna violations inserted. "
        "Absent if the platform has no diode that the router can use.",
        wirelength="Total wire length of the detailed route, in micrometers.",
        vias="Number of vias of the detailed route.",
        instances="Number of standard cell instances, without the filler cells.",
        fill_cells="Number of filler cell instances.",
        tap_cells="Number of tap cell instances.",
        warnings="Number of warnings that OpenROAD printed in the final report.",
        errors="Number of errors that OpenROAD printed in the final report.",
        parasitics="How the parasitics of the final timing, power and IR drop come about: `rcx` "
        "(OpenRCX extracted them with the rules of the platform) or `estimate` (OpenROAD "
        "estimated them from the route).",
        platform="The platform that the design is routed on: `name`, the PDK `provider` and "
        "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or user) "
        "that gave each value of the platform model.",
    )

    metrics_files = ("metrics_route.json", "metrics_final.json")
    reports = {
        "drc": "reports/drc.rpt",
        "maze": "reports/maze.log",
        "antennas": "reports/antennas.rpt",
    }
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
    }
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
            missing = [role for role in GDS_ROLES if not getattr(residue.files, role)]
            if missing:
                raise FlowSettingsException(
                    f"the platform {residue.name!r} gives no {' and no '.join(missing)}, which "
                    "`write_gds` needs to merge the layout: add them to `files` in its residue "
                    "file (`platform_file`), or set `write_gds: false`"
                )
        resolve_extraction(
            settings.extraction.method,
            has_rules=bool(residue.files.rcx_rules),
            platform=residue.name,
        )

    @property
    def platform_roles(self) -> tuple[str, ...]:
        """The roles of the platform files that the flow opens: those of every OpenROAD flow, the
        rules file of OpenRCX when the extraction is `rcx`, and the technology file of KLayout and
        the GDSII files with `write_gds`. It follows the settings and the residue file of the
        platform, so a dry run, which builds no platform model, gets the same roles as a run."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        residue = settings.residue()
        extraction = resolve_extraction(
            settings.extraction.method,
            has_rules=bool(residue.files.rcx_rules),
            platform=residue.name,
        )
        roles: tuple[str, ...] = PLATFORM_ROLES
        if extraction == "rcx":
            roles = (*roles, "rcx_rules")
        if settings.write_gds:
            roles = (*roles, *GDS_ROLES)
        return roles

    # --- the values of the templates ----------------------------------------------------------

    def add_template_helpers(self) -> None:
        """Add the helpers of every OpenROAD flow, `working_files`, the names of the files that
        the first process writes for the second, and `image_files`, the names of the images."""
        super().add_template_helpers()
        self.jinja_env.globals.update(
            working_files=dict(self.working_files), image_files=list(self.image_files)
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
                *self.reports.values(),
                *drc_iteration_reports(settings.detailed_route, self.reports["drc"]),
                self.failed_odb,
            ],
        )

    def set_final_outputs(self) -> None:
        """Name the declared outputs: `final.odb`, `final.sdc`, `final.def` and `final.v`, which
        the second process writes, `final.spef` when OpenRCX extracts the parasitics, and
        `final.gds` with `write_gds`, which KLayout writes. All are files of the run directory.
        `run_final()` calls it before it renders the driver."""
        run_path = Path(self.run_path)
        for name, file in self.final_files.items():
            setattr(self.outputs, name, run_path / file)
        extracted = self.effective().extraction == "rcx"
        setattr(self.outputs, "spef", run_path / self.spef_file if extracted else None)
        settings = self.settings
        assert isinstance(settings, self.Settings)
        setattr(self.outputs, "gds", run_path / self.gds_file if settings.write_gds else None)

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
        writes: list[str | Path] = [
            *(getattr(self.outputs, name) for name in self.final_files),
            *values.ir_drop_reports,
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
        LEF file and the LEF files of the cells, as absolute paths. KLayout reads the DEF file of
        the routed design with these. Raise `FlowSettingsException` if the file of the platform
        has no list of LEF files to replace."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        template = model.files["klayout_tech"][0]
        values = self.effective()
        lefs = [*([values.tech_lef] if values.tech_lef is not None else []), *values.cell_lefs]
        text, replaced = re.subn(
            r"<lef-files>.*?</lef-files>",
            lambda _: "".join(f"<lef-files>{lef}</lef-files>" for lef in lefs),
            template.read_text(),
        )
        if not replaced:
            raise FlowSettingsException(
                f"the KLayout technology file {template} of the platform {model.name!r} has no "
                "`<lef-files>` element, so KLayout cannot read the LEF files with the DEF file: "
                "add the element to the file, or set `write_gds: false`"
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
        names = {
            "design_name": top,
            "in_def": getattr(self.outputs, "def_file"),
            "in_files": "\n".join(str(path) for path in model.files["gds"]),
            "seal_file": "" if seal_ring is None else seal_ring,
            "out_file": output,
            "tech_file": technology,
            "layer_map": "",
            "allow_empty": "",
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
            report = self.report_file(log_file)
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
