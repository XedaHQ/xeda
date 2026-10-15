"""The settings groups of the global routing flow.

The groups follow the commands of the flow: the pin access, `global_route`, the repair after the
route, and the repair of the antenna violations.
"""

from typing import Annotated, ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup
from .final_groups import ViaInPin
from .groups import RepairDesign, RepairTiming, differing_leaves

__all__ = [
    "AntennaGroup",
    "CongestionGroup",
    "GrtRepairGroup",
    "GrtRepairTiming",
    "PinAccessGroup",
    "RegionAdjustment",
    "RouterGroup",
    "SteinerAlpha",
]

#: A coordinate that is finite and not negative.
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class GrtRepairTiming(RepairTiming):
    """The call of `repair_timing` after global routing. It may use the `reroute` move and the
    `REROUTE` phase, which need the parasitics of the route."""

    ALLOWS_REROUTE: ClassVar[bool] = True


class PinAccessGroup(SettingsGroup):
    """The `pin_access` command, which finds the access points of the pins before the route.

    The layers of the route are the ones of the database, so the command gets none. Its
    `-distributed` mode needs an external service and has no setting."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("process_node", "-db_process_node", "value"),
        Flag("via_access_layer", "-via_access_layer", "value"),
        Flag("min_access_points", "-min_access_points", "value"),
        Flag("verbose", "-verbose", "value"),
    )

    enabled: bool = Field(
        True,
        description="Run `pin_access` before the global route. OpenROAD-flow-scripts runs it. "
        "`detailed_route` finds the access points again if it does not get them from here.",
        json_schema_extra=tier("advanced"),
    )
    process_node: Optional[str] = Field(
        None,
        min_length=1,
        description="The name of a process node that selects via rules of that node "
        "(`-db_process_node`). In OpenROAD v2.0-26132 only `GF14_13M_3Mx_2Cx_4Kx_2Hx_2Gx_LB` has "
        "an effect. OpenROAD-flow-scripts takes the name from the platform variable "
        "`dbProcessNode`. Unset: the flow does not pass the flag.",
        json_schema_extra=tier("expert"),
    )
    via_access_layer: Optional[str] = Field(
        None,
        min_length=1,
        description="The routing layer up to which the router reaches a pin only through vias "
        "(`-via_access_layer`). Unset: the flow does not pass the flag.",
        json_schema_extra=tier("expert"),
    )
    via_in_pin: Optional[ViaInPin] = Field(
        None,
        description="The range of layers where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`, `-via_in_pin_top_layer`). Give `detailed_route` the same "
        "range. OpenROAD-flow-scripts takes the range from its platform; no platform of the "
        "model gives one. Unset: the flow does not pass the flags.",
        json_schema_extra=tier("advanced"),
    )
    min_access_points: Optional[int] = Field(
        None,
        ge=1,
        description="The fewest access points to find for each pin (`-min_access_points`). "
        "Unset: the default of the tool.",
        json_schema_extra=tier("expert"),
    )
    verbose: Optional[int] = Field(
        None,
        ge=0,
        description="The level of the log of the command (`-verbose`). Unset: the default of "
        "the tool, 1.",
        json_schema_extra=tier("expert"),
    )


class RegionAdjustment(XedaBaseModel):
    """A cut in the routing capacity of one layer inside a region
    (`set_global_routing_region_adjustment`)."""

    region: Tuple[NonNegative, NonNegative, NonNegative, NonNegative] = Field(
        description="The region, in micrometers: lower-left x, lower-left y, upper-right x, "
        "upper-right y. It must lie inside the die."
    )
    layer: str = Field(min_length=1, description="The routing layer of the cut (`-layer`).")
    adjustment: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = Field(
        description="The share of the routing tracks that the router may not use, from 0 to 1 "
        "(`-adjustment`)."
    )


class CongestionGroup(SettingsGroup):
    """The options of `global_route` that decide how it removes congestion. The layer
    adjustments are no setting here: the database holds the ones of `openroad_place`."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("iterations", "-congestion_iterations", "value"),
        Flag("report_step", "-congestion_report_iter_step", "value"),
        Flag("critical_nets_percent", "-critical_nets_percentage", "value"),
        Flag("allow", "-allow_congestion", "switch"),
    )

    iterations: Optional[int] = Field(
        None,
        ge=1,
        description="The number of iterations that remove the overflow (`-congestion_iterations`). "
        "Unset: 30, and 10 with `router.engine: cugr`, as OpenROAD-flow-scripts does. The tool "
        "uses 50, and 5 with CUGR.",
        json_schema_extra=tier("common"),
    )
    report_step: int = Field(
        5,
        ge=0,
        description="Write the congestion report every this many iterations "
        "(`-congestion_report_iter_step`). 0 writes it at the end only. The default is the one "
        "of OpenROAD-flow-scripts. The tool uses 0.",
        json_schema_extra=tier("expert"),
    )
    allow: bool = Field(
        False,
        description="Finish the route when overflow remains, and write the result "
        "(`-allow_congestion`). The flow gives the flag to every incremental route and to "
        "`repair_antennas` too.",
        json_schema_extra=tier("common"),
    )
    critical_nets_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the nets with the worst slack that the router routes first "
        "when it removes overflow, in percent (`-critical_nets_percentage`). The router "
        "estimates the slack from the parasitics of its own route. Unset: the default of the "
        "tool.",
        json_schema_extra=tier("advanced"),
    )
    macro_extension: Optional[int] = Field(
        None,
        ge=0,
        description="The halo around each macro in which the router reduces the capacity, in "
        "global routing cells (`set_macro_extension`). Unset: the default of the tool, 0. The "
        "database does not store it.",
        json_schema_extra=tier("advanced"),
    )
    region_adjustments: list[RegionAdjustment] = Field(
        [],
        description="Cuts in the routing capacity of one layer in a region, which spread the "
        "routes away from it (`set_global_routing_region_adjustment`). The flow sets them in "
        "this process, since the database does not store them.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a region without area."""
        return [
            (
                f"{key}.region_adjustments",
                f"`{key}.region_adjustments[{number}]` has no area: the upper right corner must "
                "lie above and right of the lower left corner",
            )
            for number, cut in enumerate(self.region_adjustments)
            if not (cut.region[2] > cut.region[0] and cut.region[3] > cut.region[1])
        ]


class SteinerAlpha(XedaBaseModel):
    """One call of `set_routing_alpha`: the weight of the Steiner tree builder between the length
    of the tree (0) and the path length from the driver (1), for all nets or for some of them. A
    later call for the same nets replaces an earlier one."""

    alpha: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = Field(
        description="The weight, from 0 to 1."
    )
    nets: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="Apply the weight to these nets, by name (`-net`, once for each net). "
        "Empty: no such selector.",
    )
    min_fanout: Optional[Annotated[int, Field(ge=1)]] = Field(
        None,
        description="Apply the weight to the nets with at least this many pins (`-min_fanout`).",
    )
    min_hpwl: Optional[NonNegative] = Field(
        None,
        description="Apply the weight to the nets with at least this half-perimeter wire length, "
        "in micrometers (`-min_hpwl`).",
    )
    clock_nets: bool = Field(
        False, description="Apply the weight to the clock nets (`-clock_nets`)."
    )

    def selectors(self) -> list[str]:
        """Return the names of the selectors that this call gives."""
        return [
            name
            for name in ("nets", "min_fanout", "min_hpwl", "clock_nets")
            if getattr(self, name) not in (None, False, [])
        ]


class RouterGroup(SettingsGroup):
    """The global router. Every flag of `global_route` has a setting here, or belongs to the flow
    (the guides, the congestion report, the increments and the verbose report), or is
    `-infinite_cap`, which the tool documents as a testing flag."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("resistance_aware", "-resistance_aware", "switch"),
        Flag("res_aware_nets_percent", "-res_aware_nets_percentage", "value"),
        Flag("batch_width", "-snapshot_batched_width", "value"),
        Flag("skip_fanout_above", "-skip_large_fanout_nets", "value"),
    )

    engine: Literal["fastroute", "cugr"] = Field(
        "fastroute",
        description="The global router. `fastroute` is the default one. `cugr` is the second "
        "router of OpenROAD, which its README calls not ready for production (`-use_cugr`). The "
        "tool keeps the choice across the incremental routes.",
        json_schema_extra=tier("advanced"),
    )
    resistance_aware: bool = Field(
        False,
        description="Assign layers and route in 3D with the resistance of the wires in mind "
        "(`-resistance_aware`). The README of OpenROAD calls it not ready for production. The "
        "flow gives the flag to each route that ends an increment too. With CUGR it has an "
        "effect only when `congestion.critical_nets_percent` is not 0.",
        json_schema_extra=tier("expert"),
    )
    res_aware_nets_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the nets that the router treats resistance-aware, in percent "
        "(`-res_aware_nets_percentage`). Unset: the default of the tool, 15. Needs "
        "`resistance_aware`.",
        json_schema_extra=tier("expert"),
    )
    seed: Optional[int] = Field(
        None,
        ge=1,
        description="The seed that shuffles the order of the nets (`set_global_routing_random "
        "-seed`). Unset: the order is fixed. A seed of 0 shuffles nothing, so the setting "
        "refuses it. The database does not store the seed.",
        json_schema_extra=tier("advanced"),
    )
    capacity_perturbation_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the edges whose capacity the router changes at random, in "
        "percent (`-capacities_perturbation_percentage`). Unset: the default of the tool, 0. "
        "Needs `seed`. OpenROAD v2.0-26132 stops with signal 11 in `perturbCapacities` for 5 "
        "percent on a design of 72 nets, and runs with 1 percent. No upstream issue exists.",
        json_schema_extra=tier("expert"),
    )
    perturbation_amount: Optional[int] = Field(
        None,
        ge=1,
        description="By how much the router changes the capacity of such an edge "
        "(`-perturbation_amount`). Unset: the default of the tool, 1. Needs `seed`.",
        json_schema_extra=tier("expert"),
    )
    steiner_alphas: list[SteinerAlpha] = Field(
        [],
        description="The weights of the Steiner tree builder, one call of `set_routing_alpha` "
        "each, in order. The tool has a default for all nets. The database does not store them.",
        json_schema_extra=tier("expert"),
    )
    batch_width: Optional[int] = Field(
        None,
        ge=1,
        description="The most batches in a wave of the batched routing (`-snapshot_batched_width`). "
        "The README of OpenROAD advises it for exploration only. Unset: no batching. Only "
        "the default router uses it.",
        json_schema_extra=tier("expert"),
    )
    grid_origin: Optional[Tuple[NonNegative, NonNegative]] = Field(
        None,
        description="The origin of the routing grid, in micrometers: x, y (`-grid_origin`, which "
        "takes database units: the flow converts). The flow gives it to every route, because "
        "the tool resets the origin at each call without it, and the origin shifts the guides "
        "that the call writes. Unset: the origin of the die.",
        json_schema_extra=tier("expert"),
    )
    skip_fanout_above: Optional[int] = Field(
        None,
        ge=0,
        description="Give no route to a net with more pins than this (`-skip_large_fanout_nets`). "
        "The detailed router skips it too. The README of OpenROAD advises it for estimation "
        "flows. Unset: no limit. In OpenROAD v2.0-26132, a net without a route stops the "
        "following steps: `repair_design` ends with signal 11 in `getPinGridPositions`, and "
        "`check_antennas` stops with ANT-0008 where `repair.strategy` is `none`. It was seen "
        "with a design of 72 nets. No upstream issue exists.",
        json_schema_extra=tier("expert"),
    )
    only_nets: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="Route only these nets, by name or pattern (`set_nets_to_route`). The other "
        "nets get no guides, so the detailed router leaves them open. For debugging. Empty: "
        "every net. A net without a route stops the following steps in OpenROAD v2.0-26132: "
        "`repair_design` ends with signal 11 in `getPinGridPositions`. It was seen with a "
        "design of 72 nets. No upstream issue exists.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse what has no effect: a share of resistance-aware nets without the mode, a
        perturbation without the seed, batching with CUGR, and a weight with two selectors."""
        found: list[tuple[str, str]] = []
        if self.res_aware_nets_percent is not None and not self.resistance_aware:
            found.append(
                (
                    f"{key}.res_aware_nets_percent",
                    f"`{key}.res_aware_nets_percent` has no effect without "
                    f"`{key}.resistance_aware`, and the tool sets the share to 0: set "
                    f"`{key}.resistance_aware: true` or delete `{key}.res_aware_nets_percent`",
                )
            )
        for name in ("capacity_perturbation_percent", "perturbation_amount"):
            if getattr(self, name) is not None and self.seed is None:
                found.append(
                    (
                        f"{key}.{name}",
                        f"`{key}.{name}` has no effect without `{key}.seed`, since "
                        f"`set_global_routing_random` needs the seed: set `{key}.seed` or delete "
                        f"`{key}.{name}`",
                    )
                )
        if self.batch_width is not None and self.engine == "cugr":
            found.append(
                (
                    f"{key}.batch_width",
                    f"`{key}.batch_width` has no effect with `{key}.engine: cugr`, which does "
                    f"not batch: set `{key}.engine: fastroute` or delete `{key}.batch_width`",
                )
            )
        for number, alpha in enumerate(self.steiner_alphas):
            if len(alpha.selectors()) > 1:
                found.append(
                    (
                        f"{key}.steiner_alphas",
                        f"`{key}.steiner_alphas[{number}]` gives more than one of `nets`, "
                        "`min_fanout`, `min_hpwl` and `clock_nets`, and the command takes only "
                        "the first: give one, and a call of its own for each other selector",
                    )
                )
        return found


class GrtRepairGroup(SettingsGroup):
    """The repair of the design after the global route."""

    strategy: Literal["none", "incremental"] = Field(
        "incremental",
        description="`none` runs no repair after the route. `incremental` runs `repair_design` "
        "and `repair_timing`, with an incremental global route and a legalization around each "
        "of them. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    wns_polish: Optional[bool] = Field(
        None,
        description='After the repair, run one more `repair_timing -setup -sequence "vt_swap '
        'reroute"` on the worst paths. Unset: the value of the platform (`opt_post_grt_wns`, a '
        "switch), and on where the platform gives none, as in OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    recover_power_percent: Optional[float] = Field(
        None,
        gt=0,
        le=100,
        description="Trade positive slack for power on this share of the paths, in percent "
        "(`repair_timing -recover_power`). Unset: no power recovery. The flow runs the recovery "
        "only without the WNS polish.",
        json_schema_extra=tier("expert"),
    )
    design: RepairDesign = Field(
        default_factory=RepairDesign,
        description="The call of `repair_design` after the route.",
    )
    timing: GrtRepairTiming = Field(
        default_factory=GrtRepairTiming,
        description="The call of `repair_timing` after the route. It repairs setup and hold.",
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a leaf that no repair reads, and a power recovery that the polish replaces."""
        found: list[tuple[str, str]] = []
        if self.strategy == "none":
            leaves = [
                f"{key}.{name}"
                for name in ("wns_polish", "recover_power_percent")
                if getattr(self, name) is not None
            ]
            leaves += differing_leaves(self.design, f"{key}.design")
            leaves += differing_leaves(self.timing, f"{key}.timing")
            for leaf in leaves:
                found.append(
                    (
                        leaf,
                        f"`{leaf}` has no effect with `{key}.strategy: none`, which runs no "
                        f"repair: set `{key}.strategy: incremental` or delete `{leaf}`",
                    )
                )
        if self.wns_polish is True and self.recover_power_percent is not None:
            found.append(
                (
                    f"{key}.recover_power_percent",
                    f"`{key}.recover_power_percent` has no effect with `{key}.wns_polish: "
                    f"true`, because the flow recovers power only without the polish: set "
                    f"`{key}.wns_polish: false` or delete `{key}.recover_power_percent`",
                )
            )
        return found


class AntennaGroup(SettingsGroup):
    """The repair of the antenna violations of the global route, with diodes."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("iterations", "-iterations", "value"),)

    repair: bool = Field(
        True,
        description="Run `repair_antennas` after the route. OpenROAD-flow-scripts runs it. The "
        "flow skips the command, with a note in the log, when the platform says that the router "
        "cannot use its diode. The check of the antenna violations runs in every case.",
        json_schema_extra=tier("common"),
    )
    iterations: int = Field(
        5,
        ge=1,
        description="The most passes of `repair_antennas` (`-iterations`). The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    diode_cell: Optional[str] = Field(
        None,
        min_length=1,
        description="The diode cell that `repair_antennas` inserts. Unset: the antenna cell of "
        "the platform. A cell named here is used even where the platform says that the router "
        "cannot use its diode.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse an option of the repair when the repair does not run."""
        if self.repair:
            return []
        return [
            (
                leaf,
                f"`{leaf}` has no effect with `{key}.repair: false`: set `{key}.repair: true` "
                f"or delete `{leaf}`",
            )
            for leaf in differing_leaves(self, key)
            if leaf != f"{key}.repair"
        ]
