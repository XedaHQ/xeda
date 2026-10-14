"""The last OpenROAD flow: the detailed route, the antenna repair, the filler cells and the final
outputs.

`effective` is the one function that turns the settings and the platform into the values that the
templates read as `P`. It reads nothing else.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, Optional

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import In, Out
from ...platforms.model import PlatformModel
from .base import EffectiveBase, OpenroadStage, base_values
from .final_groups import (
    DetailedRouteGroup,
    ExtractionGroup,
    FillGroup,
    FinalAntennaGroup,
    GdsGroup,
    IrDropGroup,
    ViaInPin,
)

__all__ = ["EffectiveFinal", "Openroad", "drc_iteration_reports", "effective"]


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


def effective(settings: "Openroad.Settings", model: PlatformModel) -> EffectiveFinal:
    """Return the values of the templates of `openroad` for `settings` and the platform `model`.
    The function reads only its arguments and changes neither."""
    cells = settings.fill.cells
    return EffectiveFinal(
        **base_values(settings, model),
        diode_cell=model.antenna_cell.cell if model.antenna_cell is not None else None,
        diode_usable=model.diode_usable is not False,
        fill_cells=tuple(model.fill_cells if cells is None else cells),
        via_in_pin=settings.detailed_route.via_in_pin,
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

    results_description = {}

    metrics_files = ("metrics_route.json",)
    reports = {
        "drc": "reports/drc.rpt",
        "maze": "reports/maze.log",
        "antennas": "reports/antennas.rpt",
    }
    #: The files that the first process writes for the process after it, by role: the database and
    #: its SDC file. They are no declared outputs; the flow writes its outputs at the end.
    working_files: ClassVar[Dict[str, str]] = {"odb": "route.odb", "sdc": "route.sdc"}

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
            "directory. It needs an OpenROAD build that includes the GUI.",
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

    # --- the values of the templates ----------------------------------------------------------

    def add_template_helpers(self) -> None:
        """Add the helpers of every OpenROAD flow, and `working_files`, the names of the files
        that the first process writes for the second."""
        super().add_template_helpers()
        self.jinja_env.globals.update(working_files=dict(self.working_files))

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
