"""The placement flow."""

from pathlib import Path
from typing import Optional

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import AsicSynthFlow, In, Out
from .base import OpenroadStage
from .place_groups import (
    ArithSwapGroup,
    DesignGroup,
    DetailedPlacementGroup,
    FloorplanGroup,
    GlobalPlacementGroup,
    IoGroup,
    PdnGroup,
    ResizeGroup,
    RoutingGroup,
    TapcellGroup,
    TieGroup,
)

__all__ = ["OpenroadPlace"]


class OpenroadPlace(OpenroadStage, AsicSynthFlow):
    """Read a gate-level netlist, then floorplan, place and optimize it with OpenROAD."""

    results_description = {}

    class Settings(OpenroadStage.Settings, AsicSynthFlow.Settings):
        """The settings of the placement flow."""

        input_delay_frac: float = Field(
            0.20,
            ge=0,
            lt=1,
            description="The input delay of the generated constraints, as a fraction of the clock "
            "period, from 0 up to but not including 1. Used only when `input_delay` is unset.",
            json_schema_extra=tier("advanced"),
        )
        output_delay_frac: float = Field(
            0.20,
            ge=0,
            lt=1,
            description="The output delay of the generated constraints, as a fraction of the "
            "clock period, from 0 up to but not including 1. Used only when `output_delay` is "
            "unset.",
            json_schema_extra=tier("advanced"),
        )
        input_delay: Optional[float] = Field(
            None,
            ge=0,
            description="The input delay of the generated constraints, in picoseconds. The flow "
            "converts it to the time unit of the platform. It takes precedence over "
            "`input_delay_frac`.",
            json_schema_extra=tier("advanced"),
        )
        output_delay: Optional[float] = Field(
            None,
            ge=0,
            description="The output delay of the generated constraints, in picoseconds. The flow "
            "converts it to the time unit of the platform. It takes precedence over "
            "`output_delay_frac`.",
            json_schema_extra=tier("advanced"),
        )
        sdc_files: list[Path] = Field(
            [],
            description="More SDC files. The flow reads them after the constraints that it "
            "generates from `clocks`. A relative path is relative to the design root.",
            json_schema_extra=tier("advanced"),
        )
        design: DesignGroup = Field(
            default_factory=DesignGroup,
            description="The loading of the netlist.",
        )
        floorplan: FloorplanGroup = Field(
            default_factory=FloorplanGroup,
            description="The size and the shape of the floorplan, and the cleanup of the netlist "
            "before the placement.",
        )
        routing: RoutingGroup = Field(
            default_factory=RoutingGroup,
            description="The routing layers and the capacity of each layer. The database stores "
            "them, so the later flows keep them.",
        )
        tie: TieGroup = Field(
            default_factory=TieGroup,
            description="The tie cells that drive the constant nets.",
        )
        tapcell: TapcellGroup = Field(
            default_factory=TapcellGroup,
            description="The well tap cells and the end cap cells.",
        )
        pdn: PdnGroup = Field(
            default_factory=PdnGroup,
            description="The power grid over the standard cells.",
        )
        io: IoGroup = Field(
            default_factory=IoGroup,
            description="The placement of the pins of the design.",
        )
        global_placement: GlobalPlacementGroup = Field(
            default_factory=GlobalPlacementGroup,
            description="The global placement of the cells.",
        )
        resize: ResizeGroup = Field(
            default_factory=ResizeGroup,
            description="The optimization of the design after the global placement.",
        )
        arith_swap: ArithSwapGroup = Field(
            default_factory=ArithSwapGroup,
            description="The swap of the arithmetic modules for faster or smaller variants.",
        )
        detailed_placement: DetailedPlacementGroup = Field(
            default_factory=DetailedPlacementGroup,
            description="The legalization and the optimization of the placement.",
        )

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
