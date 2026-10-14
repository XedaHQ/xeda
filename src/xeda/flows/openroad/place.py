"""The placement flow."""

from pathlib import Path

from ...design import SourceType
from ...flow import AsicSynthFlow, In, Out
from .base import OpenroadStage

__all__ = ["OpenroadPlace"]


class OpenroadPlace(OpenroadStage, AsicSynthFlow):
    """Read a gate-level netlist, then floorplan, place and optimize it with OpenROAD."""

    results_description = {}

    class Settings(OpenroadStage.Settings, AsicSynthFlow.Settings):
        """The settings of the placement flow."""

    class Inputs(OpenroadStage.Inputs):
        netlist: Path = In(
            SourceType.VerilogNetlist,
            producer="yosys",
            output="netlist",
            description="The gate-level Verilog netlist of the design, mapped to the cells of "
            "the platform.",
        )
        sdc: list[Path] = In(
            SourceType.Sdc,
            optional=True,
            description="The SDC files that constrain the design. They follow the clocks "
            "that the flow writes from the `clocks` setting.",
        )

    class Outputs(OpenroadStage.Outputs):
        odb: Path = Out(
            SourceType.OpenroadDbPlaced,
            description="The OpenROAD database after placement.",
        )
        sdc: Path = Out(
            SourceType.OpenroadSdcPlaced,
            description="The SDC file of the placed design.",
        )
