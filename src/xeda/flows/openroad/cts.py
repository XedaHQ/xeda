"""The clock tree synthesis flow.

`openroad_cts` reads the placed database and its SDC file, builds the clock tree, legalizes the
placement and repairs the timing with propagated clocks. It writes the database `cts.odb` and
`cts.sdc`. The SDC file has the propagated clocks, so the flows after this one read it, and no
earlier one.

`effective` is the one function that turns the settings and the platform into the values that
the templates read as `P`. It reads nothing else.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import In, Out, describe_results
from ...platforms.model import PlatformModel
from .base import EffectiveBase, OpenroadStage, base_values, platform_time_unit
from .cts_groups import (
    CtsBuffersGroup,
    CtsTreeGroup,
    RepairClockNetsGroup,
    SinkClusteringGroup,
)
from .groups import Legalize, RepairTiming
from .metrics import MetricsError, stage_results

__all__ = ["EffectiveCts", "OpenroadCts", "effective"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveCts(EffectiveBase):
    """The values of the platform and the settings that the stage of `openroad_cts` reads as
    `P`."""

    #: Whether the repair of the timing swaps a cell only for a cell with the same footprint, when
    #: the setting `repair.match_cell_footprint` is unset. The platform decides.
    match_cell_footprint: bool


def effective(settings: "OpenroadCts.Settings", model: PlatformModel) -> EffectiveCts:
    """Return the values of the templates of `openroad_cts` for `settings` and the platform
    `model`. The function reads only its arguments and changes neither."""
    return EffectiveCts(
        **base_values(settings, model),
        match_cell_footprint=bool(model.match_cell_footprint),
    )


class OpenroadCts(OpenroadStage):
    """Build the clock tree of a placed design with OpenROAD and repair its timing.

    One OpenROAD process loads the Liberty files of the platform, the placed database and its SDC
    file. It repairs the clock inverters, builds the clock tree, legalizes the placement and
    repairs the timing with the clocks propagated. It writes the database `cts.odb` and the
    constraints `cts.sdc`. The clock tree changes the constraints: the clocks are propagated in
    `cts.sdc`.

    The wire RC and the cells that the resizer must not use are no part of the database. The
    process sets them again from the platform and the settings `wire_rc` and `dont_use_cells`."""

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "Fmax",
        "utilization",
        "power",
        area="Area of the standard cells after the clock tree and its repair, in square "
        "micrometers.",
        hold_tns="Total negative hold slack (ns) after the clock tree, with propagated clocks and "
        "the parasitics that OpenROAD estimates from the placement.",
        clock_skew_setup="Clock skew (ns) of the setup check after the clock tree.",
        clock_skew_hold="Clock skew (ns) of the hold check after the clock tree.",
        platform="The platform that the design is placed on: `name`, the PDK `provider` and "
        "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or user) "
        "that gave each value of the platform model.",
    )

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

    # --- the values of the templates ----------------------------------------------------------

    def effective(self) -> EffectiveCts:
        """Return the values that the templates read as `P`. It runs after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        settings = self.settings
        assert isinstance(settings, self.Settings)
        return effective(settings, model)

    # --- running OpenROAD ---------------------------------------------------------------------

    def run(self) -> None:
        """Run the process that builds the clock tree and writes the database and the SDC file."""
        odb, sdc = self.run_path / "cts.odb", self.run_path / "cts.sdc"
        self.outputs.odb, self.outputs.sdc = odb, sdc  # type: ignore[attr-defined]
        self.run_openroad("openroad_cts", "metrics.json", [odb, sdc, self.failed_odb])

    # --- the results --------------------------------------------------------------------------

    def parse_reports(self) -> bool:
        """Read the metrics of the clock tree stage: the timing, the area, the power and the clock
        skew. Return False if the metrics file is missing or holds a value that is no number."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
        metrics = self.read_metrics("metrics.json")
        if metrics is None:
            log.error(
                "OpenROAD wrote no usable metrics file: %s",
                Path(self.run_path) / "metrics.json",
            )
            return False
        try:
            found = stage_results(
                metrics,
                "cts",
                time_unit=platform_time_unit(model),
                file=Path(self.run_path) / "metrics.json",
            )
        except MetricsError as error:
            log.error("%s", error)
            return False
        self.results.update(found)
        return True
