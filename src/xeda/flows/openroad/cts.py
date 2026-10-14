"""The clock tree synthesis flow."""

from pathlib import Path

from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage

__all__ = ["OpenroadCts"]


class OpenroadCts(OpenroadStage):
    """Build the clock tree of a placed design with OpenROAD and repair its timing."""

    results_description = {}

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
