"""The global routing flow.

`openroad_grt` reads the database of the clock tree and its SDC file, finds the access points of
the pins, routes the design globally, and repairs the design and the timing with the parasitics
of the route. An incremental route follows each repair. Then it repairs the antenna violations,
and it writes the database `grt.odb`, the SDC file `grt.sdc` and the route guides `grt.guide`.
The database keeps the routing layers and the layer adjustments that `openroad_place` set, so
this flow sets none.

`effective` is the one function that turns the settings and the platform into the values that
the templates read as `P`. It reads nothing else.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...dataclass import Field
from ...design import SourceType
from ...flow import Flow, In, MetricSpec, Out, describe_results
from ...flow.flow import FlowSettingsError, FlowSettingsException
from ...platforms.model import PlatformModel
from .base import EffectiveBase, OpenroadStage, base_values, platform_time_unit
from .groups import Legalize
from .grt_groups import (
    AntennaGroup,
    CongestionGroup,
    GrtRepairGroup,
    PinAccessGroup,
    RouterGroup,
)
from .metrics import MetricsError, count, number, stage_results

__all__ = ["EffectiveGrt", "OpenroadGrt", "effective"]

log = logging.getLogger(__name__)

#: The results that the global route adds to the common ones: the result, the metric after the
#: stage prefix, and the reader of its value.
ROUTE_TABLE = (
    ("global_route_wirelength", "global_route__wirelength", number),
    ("global_route_vias", "global_route__vias", count),
    ("antenna_violating_nets", "antenna__violating__nets", count),
    ("antenna_violating_pins", "antenna__violating__pins", count),
    ("antenna_diodes", "antenna_diodes_count", count),
)


@dataclass(frozen=True)
class EffectiveGrt(EffectiveBase):
    """The values of the platform and the settings that the stage of `openroad_grt` reads as
    `P`."""

    #: Whether the repair swaps a cell only for a cell with the same footprint, when the setting
    #: `match_cell_footprint` of the repair is unset. The platform decides.
    match_cell_footprint: bool
    #: Whether one more setup repair follows the repairs, with the swap of threshold voltage and
    #: the reroute only. The setting `repair.wns_polish` decides, else the platform
    #: (`opt_post_grt_wns`, a switch: any value but 0 is on), else the default of
    #: OpenROAD-flow-scripts, which is on.
    wns_polish: bool
    #: The diode cell that `repair_antennas` inserts, or `None`: the tool finds one.
    diode_cell: Optional[str]
    #: Whether the router can insert a diode. A diode that the settings name is usable. The
    #: platform may say that its diode is not, and a platform that says nothing is trusted.
    diode_usable: bool


def _polish_replaces_the_power_recovery(platform: str, given: bool) -> str:
    """Return the refusal of a power recovery that the polish of the platform replaces. `given`
    says whether the platform sets `opt_post_grt_wns`, or the default of OpenROAD-flow-scripts
    (the polish is on) applies."""
    why = (
        "sets `opt_post_grt_wns` and so runs"
        if given
        else "gives no `opt_post_grt_wns`, so the flow runs, as OpenROAD-flow-scripts does,"
    )
    return (
        f"`repair.recover_power_percent` has no effect, because the platform {platform!r} {why} "
        "the polish of the worst slack, and the flow recovers power only without the polish: set "
        "`repair.wns_polish: false` or delete `repair.recover_power_percent`"
    )


def effective(settings: "OpenroadGrt.Settings", model: PlatformModel) -> EffectiveGrt:
    """Return the values of the templates of `openroad_grt` for `settings` and the platform
    `model`. The function reads only its arguments and changes neither.

    Raise `FlowSettingsException` for a power recovery that the polish replaces: planning cannot
    see a value that the platform takes from the configuration of its PDK."""
    polish = settings.repair.wns_polish
    if polish is None:
        switch = model.opt_post_grt_wns
        polish = True if switch is None else bool(switch)
        if polish and settings.repair.recover_power_percent is not None:
            raise FlowSettingsException(
                _polish_replaces_the_power_recovery(model.name, switch is not None)
            )
    given = settings.antenna.diode_cell
    platform_cell = model.antenna_cell.cell if model.antenna_cell is not None else None
    return EffectiveGrt(
        **base_values(settings, model),
        match_cell_footprint=bool(model.match_cell_footprint),
        wns_polish=polish,
        diode_cell=given or platform_cell,
        diode_usable=given is not None or model.diode_usable is not False,
    )


class OpenroadGrt(OpenroadStage):
    """Globally route a design that has a clock tree with OpenROAD and repair its timing.

    One OpenROAD process loads the Liberty files of the platform, the database of the clock tree
    and its SDC file. It finds the access points of the pins and routes the design globally. It
    repairs the design and the timing with the parasitics of the route, and routes again the nets
    that each repair changes. It repairs the antenna violations with diodes where the platform
    has a diode that the router can use. It writes the database `grt.odb`, the constraints
    `grt.sdc` and the route guides `grt.guide`.

    The routing layers and the layer adjustments stay in the database from `openroad_place`. The
    wire RC, the seed of the router and the cells that the resizer must not use are no part of the
    database. The process sets them again from the platform and the settings."""

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "fmax",
        "utilization",
        "power",
        area=MetricSpec(
            "Area of the standard cells after the global route and its repair, in square "
            "micrometers.",
            "number",
        ),
        hold_tns=MetricSpec(
            "Total negative hold slack after the global route, with propagated clocks and the "
            "parasitics that OpenROAD estimates from the route.",
            "number",
            "ns",
        ),
        clock_skew_setup=MetricSpec(
            "Clock skew of the setup check after the global route.", "number", "ns"
        ),
        clock_skew_hold=MetricSpec(
            "Clock skew of the hold check after the global route.", "number", "ns"
        ),
        global_route_wirelength=MetricSpec(
            "Total wire length of the global route.", "number", "um"
        ),
        global_route_vias=MetricSpec("Number of vias of the global route.", "integer"),
        antenna_violating_nets=MetricSpec(
            "Number of nets with an antenna violation after the global route and its repair.",
            "integer",
        ),
        antenna_violating_pins=MetricSpec(
            "Number of pins with an antenna violation after the global route and its repair.",
            "integer",
        ),
        antenna_diodes=MetricSpec(
            "Number of diodes that the repair of the antenna violations inserted.", "integer"
        ),
        platform=MetricSpec(
            "The platform that the design is routed on: `name`, the PDK `provider` and "
            "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or "
            "user) that gave each value of the platform model.",
            "object",
        ),
    )

    #: The design sources it can take as its inputs: the database and constraints of the clock tree.
    design_source_types = frozenset({SourceType.OpenroadDbCts, SourceType.OpenroadSdcCts})

    metrics_files = ("metrics.json",)
    reports = {
        "congestion": "reports/congestion.rpt",
        "post_repair_design": "reports/congestion_post_repair_design.rpt",
        "post_repair_timing": "reports/congestion_post_repair_timing.rpt",
        "post_recover_power": "reports/congestion_post_recover_power.rpt",
        "antennas": "reports/antennas.rpt",
    }

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
            description="The global router: its engine, resistance-aware routing, the random "
            "order of the nets, the weights of the Steiner trees and the nets to route.",
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

    # --- planning -----------------------------------------------------------------------------

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what the groups cannot judge alone: a power recovery that the polish replaces.
        The polish is on where the platform asks for it or gives no value. A platform that reads
        the configuration of its PDK gives its value only when the flow runs, so
        `prepare_inputs()` judges that case. This reads the settings and the residue files
        only."""
        super().check_settings_supported(settings)
        if not isinstance(settings, cls.Settings):
            return
        if settings.platform is None and settings.platform_file is None:
            return  # the missing platform is reported with the other required settings
        repair = settings.repair
        if repair.recover_power_percent is None or repair.wns_polish is not None:
            return
        residue = settings.residue()
        switch = residue.values.opt_post_grt_wns
        if switch is None and residue.upstream is not None:
            return  # the configuration of the PDK may give the value, and only a run reads it
        if switch is None or switch:
            problems: List[tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = [
                (
                    "repair.recover_power_percent",
                    _polish_replaces_the_power_recovery(residue.name, switch is not None),
                    None,
                    "value_error",
                )
            ]
            raise FlowSettingsError(problems, cls.Settings)

    def prepare_inputs(self) -> None:
        """Find the files of the platform, then compute the values of the templates. A power
        recovery that the polish of the platform replaces stops the run here, before OpenROAD
        starts."""
        super().prepare_inputs()
        self.effective()

    # --- the values of the templates ----------------------------------------------------------

    def effective(self) -> EffectiveGrt:
        """Return the values that the templates read as `P`. It runs after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        settings = self.settings
        assert isinstance(settings, self.Settings)
        return effective(settings, model)

    # --- running OpenROAD ---------------------------------------------------------------------

    def run(self) -> None:
        """Run the process that routes the design and writes the database, the SDC file and the
        route guides."""
        odb, sdc, guides = (self.run_path / name for name in ("grt.odb", "grt.sdc", "grt.guide"))
        self.outputs.odb, self.outputs.sdc = odb, sdc  # type: ignore[attr-defined]
        self.outputs.guides = guides  # type: ignore[attr-defined]
        self.run_openroad(
            "openroad_grt",
            "metrics.json",
            [odb, sdc, guides, self.failed_odb, *self.reports.values()],
        )

    # --- the results --------------------------------------------------------------------------

    def parse_reports(self) -> bool:
        """Read the metrics of the global route stage: the timing, the area, the power, the clock
        skew, the length of the route and the antenna violations. Return False if the metrics
        file is missing or holds a value that is no number."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
        metrics = self.read_metrics("metrics.json")
        file = Path(self.run_path) / "metrics.json"
        if metrics is None:
            log.error("OpenROAD wrote no usable metrics file: %s", file)
            return False
        try:
            found = stage_results(
                metrics, "globalroute", time_unit=platform_time_unit(model), file=file
            )
            for name, metric, reader in ROUTE_TABLE:
                key = f"globalroute__{metric}"
                if key in metrics:
                    found[name] = reader(metrics, key, file=file)
        except MetricsError as error:
            log.error("%s", error)
            return False
        self.results.update(found)
        return True
