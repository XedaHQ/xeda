"""The global routing flow."""

from pathlib import Path

from ...dataclass import Field
from ...design import SourceType
from ...flow import In, Out
from .base import OpenroadStage
from .groups import Legalize
from .grt_groups import (
    AntennaGroup,
    CongestionGroup,
    GrtRepairGroup,
    PinAccessGroup,
    RouterGroup,
)

__all__ = ["OpenroadGrt"]


class OpenroadGrt(OpenroadStage):
    """Globally route a design that has a clock tree with OpenROAD and repair its timing."""

    results_description = {}

    class Settings(OpenroadStage.Settings):
        """The settings of the global routing flow."""

        pin_access: PinAccessGroup = Field(
            default_factory=PinAccessGroup,
            description="The `pin_access` command, which finds the access points of the pins "
            "before the global route.",
        )
        congestion: CongestionGroup = Field(
            default_factory=CongestionGroup,
            description="How `global_route` removes congestion.",
        )
        router: RouterGroup = Field(
            default_factory=RouterGroup,
            description="The global router: its seed and the flags that no setting covers.",
        )
        legalize: Legalize = Field(
            default_factory=Legalize,
            description="The legalization of the placement after each repair of the route.",
        )
        repair: GrtRepairGroup = Field(
            default_factory=GrtRepairGroup,
            description="The repair of the design after the global route.",
        )
        antenna: AntennaGroup = Field(
            default_factory=AntennaGroup,
            description="The repair of the antenna violations of the global route.",
        )

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
