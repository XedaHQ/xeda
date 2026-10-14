"""The global routing flow."""

from pathlib import Path

from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage

__all__ = ["OpenroadGrt"]


class OpenroadGrt(OpenroadStage):
    """Globally route a design that has a clock tree with OpenROAD and repair its timing."""

    results_description = {}

    class Inputs(OpenroadStage.Inputs):
        odb: Path = In(
            SourceType.OpenroadDbCts,
            producer="openroad_cts",
            output="odb",
            description="The OpenROAD database after clock tree synthesis.",
        )
        sdc: Path = In(
            SourceType.OpenroadSdcCts,
            producer="openroad_cts",
            output="sdc",
            same_producer_as="odb",
            paired=True,
            description="The SDC file written with the clock tree database. It comes from the "
            "same source as the database.",
        )

    class Outputs(OpenroadStage.Outputs):
        odb: Path = Out(
            SourceType.OpenroadDbGlobalRouted,
            description="The OpenROAD database after global routing.",
        )
        sdc: Path = Out(
            SourceType.OpenroadSdcGlobalRouted,
            description="The SDC file after global routing.",
        )
        guides: Path = Out(
            SourceType.Data,
            description="The route guides of the global routing.",
        )
