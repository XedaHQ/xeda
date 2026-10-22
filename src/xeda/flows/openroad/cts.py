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
from typing import ClassVar, Dict

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import In, MetricSpec, Out, describe_results
from ...platforms.model import PlatformModel
from .base import EffectiveBase, OpenroadStage, base_values, platform_time_unit
from .cts_groups import (
    BalanceGroup,
    CharacterizationGroup,
    CtsBuffersGroup,
    CtsTreeGroup,
    MacroClusteringGroup,
    CtsNdrGroup,
    RepairClockNetsGroup,
    SinkClusteringGroup,
    WordName,
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

    #: The design sources it can take as its inputs: the database and constraints of the placement.
    design_source_types = frozenset({SourceType.OpenroadDbPlaced, SourceType.OpenroadSdcPlaced})

    reports: ClassVar[Dict[str, str]] = {"cts": "reports/cts.rpt"}

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "Fmax",
        "utilization",
        "power",
        area=MetricSpec(
            "Area of the standard cells after the clock tree and its repair, in square "
            "micrometers.",
            "number",
        ),
        hold_tns=MetricSpec(
            "Total negative hold slack after the clock tree, with propagated clocks and the "
            "parasitics that OpenROAD estimates from the placement.",
            "number",
            "ns",
        ),
        clock_skew_setup=MetricSpec(
            "Clock skew of the setup check after the clock tree.", "number", "ns"
        ),
        clock_skew_hold=MetricSpec(
            "Clock skew of the hold check after the clock tree.", "number", "ns"
        ),
        platform=MetricSpec(
            "The platform that the design is placed on: `name`, the PDK `provider` and "
            "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or "
            "user) that gave each value of the platform model.",
            "object",
        ),
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
        macro_clustering: MacroClusteringGroup = Field(
            default_factory=MacroClusteringGroup,
            description="The grouping of the macro sinks, which the tool clusters on their own.",
        )
        tree: CtsTreeGroup = Field(
            default_factory=CtsTreeGroup,
            description="The shape of the clock tree.",
        )
        balance: BalanceGroup = Field(
            default_factory=BalanceGroup,
            description="The balance of the clock latencies between macros and registers, and of "
            "the loads at the leaves.",
        )
        ndr: CtsNdrGroup = Field(
            default_factory=CtsNdrGroup,
            description="The non-default routing rule of the clock nets.",
        )
        characterization: CharacterizationGroup = Field(
            default_factory=CharacterizationGroup,
            description="The characterization of the clock buffers, from which the tool builds "
            "its lookup table.",
        )
        clock_nets: list[WordName] = Field(
            [],
            description="The clock roots to build trees for: nets, or pins that drive a net "
            "(`-clk_nets`). A name holds no white space. Empty: the tool finds all clock roots.",
            json_schema_extra=tier("advanced"),
        )
        skip_nets: list[str] = Field(
            [],
            description="The nets that the tool leaves out: a clock root skips the whole clock, "
            "and a sub-net skips that net (`set_cts_config -skip_nets`). A name is a pattern of "
            "`get_nets`.",
            json_schema_extra=tier("expert"),
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
        self.run_openroad(
            "openroad_cts", "metrics.json", [odb, sdc, self.failed_odb, *self.reports.values()]
        )

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
