"""The settings groups of the global routing flow.

The groups follow the commands of the flow: the pin access, `global_route`, the repair after the
route, and the repair of the antenna violations.
"""

from typing import ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, tier
from ...flow.settings_group import Flag, SettingsGroup
from .final_groups import ViaInPin
from .groups import RepairDesign, RepairTiming, differing_leaves

__all__ = [
    "AntennaGroup",
    "CongestionGroup",
    "GrtRepairGroup",
    "GrtRepairTiming",
    "PinAccessGroup",
    "RouterGroup",
]


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


class CongestionGroup(SettingsGroup):
    """The options of `global_route` that decide how it removes congestion."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("iterations", "-congestion_iterations", "value"),
        Flag("allow", "-allow_congestion", "switch"),
    )

    iterations: int = Field(
        30,
        ge=1,
        description="The number of iterations that remove the overflow (`-congestion_iterations`). "
        "The default is the one of OpenROAD-flow-scripts. The tool uses 50.",
        json_schema_extra=tier("common"),
    )
    allow: bool = Field(
        False,
        description="Finish the route when overflow remains, and write the result "
        "(`-allow_congestion`). The flow gives the flag to every incremental route and to "
        "`repair_antennas` too.",
        json_schema_extra=tier("common"),
    )


class RouterGroup(SettingsGroup):
    """The global router."""

    #: The research options of `global_route`, which the tool adds and changes often.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "-use_cugr",
            "-resistance_aware",
            "-res_aware_nets_percentage",
            "-snapshot_batched_width",
        }
    )

    seed: Optional[int] = Field(
        None,
        ge=1,
        description="The seed that shuffles the order of the nets (`set_global_routing_random "
        "-seed`). Unset: the order is fixed. A seed of 0 shuffles nothing, so the setting "
        "refuses it. The database does not store the seed.",
        json_schema_extra=tier("advanced"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `global_route`. Each flag must be one of `-use_cugr`, "
        "`-resistance_aware`, `-res_aware_nets_percentage` and `-snapshot_batched_width`, each "
        "followed by its value where it takes one. The flow gives `-resistance_aware` to each "
        "route that ends an increment too.",
        json_schema_extra=tier("expert"),
    )


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
