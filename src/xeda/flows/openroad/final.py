"""The detail routing flow, which also writes the final outputs."""

from pathlib import Path

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage
from .final_groups import (
    DetailedRouteGroup,
    ExtractionGroup,
    FillGroup,
    FinalAntennaGroup,
    GdsGroup,
    IrDropGroup,
)

__all__ = ["Openroad"]


class Openroad(OpenroadStage):
    """Detail-route a globally routed design with OpenROAD, then write the final outputs."""

    results_description = {}

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
