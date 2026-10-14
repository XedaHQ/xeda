"""The clock tree synthesis flow."""

from pathlib import Path

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage
from .cts_groups import (
    CtsBuffersGroup,
    CtsTreeGroup,
    RepairClockNetsGroup,
    SinkClusteringGroup,
)
from .groups import Legalize, RepairTiming

__all__ = ["OpenroadCts"]


class OpenroadCts(OpenroadStage):
    """Build the clock tree of a placed design with OpenROAD and repair its timing."""

    results_description = {}

    class Settings(OpenroadStage.Settings):
        """The settings of the clock tree flow."""

        repair_clock_inverters: bool = Field(
            True,
            description="Clone each inverter of the clock network that drives several loads, so "
            "that each clone drives one load (`repair_clock_inverters`, run before the clock "
            "tree). OpenROAD-flow-scripts runs it.",
            json_schema_extra=tier("advanced"),
        )
        buffers: CtsBuffersGroup = Field(
            default_factory=CtsBuffersGroup,
            description="The buffers that the clock tree may use.",
        )
        sink_clustering: SinkClusteringGroup = Field(
            default_factory=SinkClusteringGroup,
            description="The grouping of the clock sinks under one buffer each.",
        )
        tree: CtsTreeGroup = Field(
            default_factory=CtsTreeGroup,
            description="The shape of the clock tree.",
        )
        repair_clock_nets: RepairClockNetsGroup = Field(
            default_factory=RepairClockNetsGroup,
            description="The buffering of the long wire from the clock port to the root buffer.",
        )
        legalize: Legalize = Field(
            default_factory=Legalize,
            description="The legalization of the placement after the clock tree and after the "
            "repair.",
        )
        repair: RepairTiming = Field(
            default_factory=RepairTiming,
            description="The repair of the timing after the clock tree (`repair_timing`), with "
            "propagated clocks. It repairs setup and hold.",
        )

    class Inputs(OpenroadStage.Inputs):
        odb: Path = In(
            SourceType.OpenroadDbPlaced,
            producer="openroad_place",
            output="odb",
            description="The OpenROAD database after placement.",
        )
        sdc: Path = In(
            SourceType.OpenroadSdcPlaced,
            producer="openroad_place",
            output="sdc",
            same_producer_as="odb",
            paired=True,
            description="The SDC file written with the placed database. It comes from the same "
            "source as the database.",
        )

    class Outputs(OpenroadStage.Outputs):
        odb: Path = Out(
            SourceType.OpenroadDbCts,
            description="The OpenROAD database after clock tree synthesis.",
        )
        sdc: Path = Out(
            SourceType.OpenroadSdcCts,
            description="The SDC file after clock tree synthesis, with propagated clocks.",
        )
