"""The detail routing flow, which also writes the final outputs."""

from pathlib import Path

from ...dataclass import Field
from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage

__all__ = ["Openroad"]


class Openroad(OpenroadStage):
    """Detail-route a globally routed design with OpenROAD, then write the final outputs."""

    results_description = {}

    class Settings(OpenroadStage.Settings):
        """The settings of the last flow."""

        write_gds: bool = Field(
            False,
            description="Whether to write the GDSII layout of the design with KLayout.",
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
