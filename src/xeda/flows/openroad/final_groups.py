"""The settings groups of the last flow: the detailed route, the antenna repair, the fill, the
extraction and the layout files.

A leaf whose description says "Unset: the value of the platform" is `None` in the model, and the
flow resolves it from the platform.
"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup
from .groups import differing_leaves

__all__ = [
    "DetailedRouteGroup",
    "NdrGroup",
    "NdrRule",
    "ExtractionGroup",
    "FillGroup",
    "FinalAntennaGroup",
    "GdsGroup",
    "IrDropGroup",
    "ViaInPin",
]


class ViaInPin(XedaBaseModel):
    """The range of layers where a via may sit inside a pin. `pin_access` and `detailed_route`
    must get the same range."""

    bottom: str = Field(
        min_length=1,
        description="The lowest layer where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`).",
    )
    top: str = Field(
        min_length=1,
        description="The highest layer where a via may sit inside a pin "
        "(`-via_in_pin_top_layer`).",
    )


class DetailedRouteGroup(SettingsGroup):
    """The detailed router (`detailed_route`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("verbose", "-verbose", "value"),
        Flag("end_iteration", "-droute_end_iter", "value"),
        Flag("reroute_seed", "-or_seed", "value"),
        Flag("reroute_swap_factor", "-or_k", "value"),
        Flag("drc_report_step", "-drc_report_iter_step", "value"),
        Flag("via_access_layer", "-via_access_layer", "value"),
        Flag("min_access_points", "-min_access_points", "value"),
        Flag("pin_access", "-no_pin_access", "skip"),
        Flag("via_generation", "-disable_via_gen", "skip"),
        Flag("repair_pdn_vias_layer", "-repair_pdn_vias", "value"),
        Flag("clean_patches", "-clean_patches", "switch"),
        Flag("save_guide_updates", "-save_guide_updates", "switch"),
    )
    #: The flags of `detailed_route` that no setting covers. The layers of the route are the ones
    #: of the database, the files of the reports are the ones of the flow, and the cloud workers
    #: need a service that Xeda does not start.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-db_process_node"})

    end_iteration: int = Field(
        64,
        ge=1,
        le=64,
        description="The most optimization iterations (`-droute_end_iter`). The tool clamps "
        "larger values to 64. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    reroute_seed: Optional[int] = Field(
        None,
        ge=0,
        description="The seed that shuffles the nets to reroute (`-or_seed`). A developer "
        "option of the router. Unset: no shuffling.",
        json_schema_extra=tier("expert"),
    )
    reroute_swap_factor: Optional[float] = Field(
        None,
        ge=0,
        description="The factor that sets how many swaps the shuffle makes (`-or_k`): the "
        "number of swaps is the factor times the number of nets to reroute. Needs "
        "`reroute_seed`.",
        json_schema_extra=tier("expert"),
    )
    drc_report_step: int = Field(
        5,
        ge=0,
        description="Write a DRC report every this many iterations (`-drc_report_iter_step`). 0 "
        "writes no report during the route. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    via_in_pin: Optional[ViaInPin] = Field(
        None,
        description="The range of layers where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`, `-via_in_pin_top_layer`). Unset: the flow gives the "
        "router no range. No platform gives one.",
        json_schema_extra=tier("advanced"),
    )
    via_access_layer: Optional[str] = Field(
        None,
        min_length=1,
        description="A pin on this layer or below is reached by vias only "
        "(`-via_access_layer`). Unset: the router decides.",
        json_schema_extra=tier("expert"),
    )
    min_access_points: Optional[int] = Field(
        None,
        ge=1,
        description="The least number of access points that the router finds for each pin of a "
        "standard cell or a macro (`-min_access_points`). Unset: the default of the router.",
        json_schema_extra=tier("expert"),
    )
    pin_access: bool = Field(
        True,
        description="The router analyzes the access to the pins itself. False skips that "
        "analysis (`-no_pin_access`): the router then works with the access points that an "
        "earlier `pin_access` left in the database, which `openroad_grt` runs with "
        "`pin_access.enabled`.",
        json_schema_extra=tier("expert"),
    )
    via_generation: bool = Field(
        True,
        description="The router makes vias for the pins and the wide wires besides the vias of "
        "the technology. False allows only the vias of the technology (`-disable_via_gen`).",
        json_schema_extra=tier("advanced"),
    )
    repair_pdn_vias_layer: Optional[str] = Field(
        None,
        min_length=1,
        description="The layer of the vias of the power grid that the router removes where they "
        "cause design rule violations (`-repair_pdn_vias`). A platform whose first two metal "
        "layers carry the power rails in parallel needs it. Unset: the router removes none.",
        json_schema_extra=tier("advanced"),
    )
    clean_patches: bool = Field(
        False,
        description="Remove the patch wires that the router does not need (`-clean_patches`).",
        json_schema_extra=tier("expert"),
    )
    save_guide_updates: bool = Field(
        False,
        description="Save the changes that the router makes to the route guides "
        "(`-save_guide_updates`).",
        json_schema_extra=tier("expert"),
    )
    verbose: int = Field(
        1,
        ge=0,
        description="The level of the messages of the router (`-verbose`). The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("expert"),
    )
    default_vias: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The vias that the router prefers, by name, one command each "
        "(`detailed_route_set_default_via`). The command has no entry in the help of OpenROAD. "
        "Empty: the router chooses.",
        json_schema_extra=tier("expert"),
    )
    unidirectional_layers: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The layers where the router allows wires in the preferred direction only, "
        "one command each (`detailed_route_set_unidirectional_layer`). The command has no entry "
        "in the help of OpenROAD. Empty: the layers keep the direction rule of the technology.",
        json_schema_extra=tier("expert"),
    )
    require_routed: bool = Field(
        True,
        description="The flow fails when a net is unrouted after the route and the antenna "
        "repair (`design_is_routed`). OpenROAD-flow-scripts fails then. False keeps the "
        "partial route and goes on, so the later checks and the outputs describe it.",
        json_schema_extra=tier("advanced"),
    )
    snapshots: bool = Field(
        False,
        description="Save the database after each iteration that leaves violations "
        "(`set_debug_level DRT snapshot 1`) as `drt_iter<n>.odb` in the run directory, for "
        "each iteration `n` up to `end_iteration`. A database is large, so use it for a small "
        "design.",
        json_schema_extra=tier("expert"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `detailed_route`. Each flag must be `-db_process_node`, "
        "followed by its value. The router uses it to select the via rules of one process "
        "node, which the platform of the node gives.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a swap factor without a seed."""
        if self.reroute_swap_factor is not None and self.reroute_seed is None:
            return [
                (
                    f"{key}.reroute_swap_factor",
                    f"`{key}.reroute_swap_factor` has no effect without `{key}.reroute_seed`: "
                    f"give the seed or delete `{key}.reroute_swap_factor`",
                )
            ]
        return []


#: A width or spacing of a non-default rule: micrometers, or a factor of the value of the
#: technology, written `*2`.
NdrValue = Union[
    Annotated[float, Field(gt=0)],
    Annotated[str, Field(pattern=r"^\*[0-9]+(\.[0-9]+)?$")],
]


class NdrRule(XedaBaseModel):
    """A non-default routing rule: the widths and the spacings of the layers, and the vias."""

    width: dict[str, NdrValue] = Field(
        {},
        description="The width of the wires, by layer. A key is a layer name or a range "
        "`first:last` of routing layers. A value is a number in micrometers or a factor of the "
        "default width, written `*2`. A layer that no key names keeps its default width.",
    )
    spacing: dict[str, NdrValue] = Field(
        {},
        description="The spacing of the wires, by layer. A key is a layer name or a range "
        "`first:last` of routing layers. A value is a number in micrometers or a factor of the "
        "default spacing, written `*2`.",
    )
    vias: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The vias that the nets of the rule may use, by name.",
    )


class NdrGroup(SettingsGroup):
    """The non-default routing rules for the detailed route, and the nets that follow them."""

    rules: dict[Annotated[str, Field(min_length=1)], NdrRule] = Field(
        {},
        description="The rules to create, by name (`create_ndr`). The database keeps them.",
        json_schema_extra=tier("advanced"),
    )
    assignments: dict[Annotated[str, Field(min_length=1)], Annotated[str, Field(min_length=1)]] = (
        Field(
            {},
            description="The rule of each net, by net name (`assign_ndr -net`). A rule is one "
            "of `rules` or a rule that the database already holds.",
            json_schema_extra=tier("advanced"),
        )
    )
    clock_rule: Optional[str] = Field(
        None,
        min_length=1,
        description="The rule for all clock nets (`assign_ndr -all_clocks`). A rule is one of "
        "`rules` or a rule that the database already holds. Unset: no rule for the clock nets.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a rule that changes nothing."""
        return [
            (
                f"{key}.rules.{name}",
                f"the rule `{name}` in `{key}.rules` sets no width, no spacing and no via: give "
                "one of them or delete the rule",
            )
            for name, rule in self.rules.items()
            if not (rule.width or rule.spacing or rule.vias)
        ]


class FinalAntennaGroup(SettingsGroup):
    """The repair of the antenna violations of the detailed route, with diodes. The repair, the
    reroute and the check repeat until no violation is left or the iterations end."""

    repair: bool = Field(
        True,
        description="Repair the antenna violations of the detailed route. OpenROAD-flow-scripts "
        "does it.",
        json_schema_extra=tier("advanced"),
    )
    reroute_iterations: int = Field(
        5,
        ge=1,
        description="The most rounds of repair and reroute. The default is the one of "
        "OpenROAD-flow-scripts.",
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


class FillGroup(SettingsGroup):
    """The filler cells that fill the gaps in the rows."""

    cells: Optional[list[Annotated[str, Field(min_length=1)]]] = Field(
        None,
        description="The filler cells, as glob patterns of cell names (`filler_placement`). The "
        "flow places them after the last change of the route. An empty list places no filler. "
        "Unset: the value of the platform.",
        json_schema_extra=tier("common"),
    )


class ExtractionGroup(SettingsGroup):
    """The extraction of the parasitics of the routed design."""

    method: Literal["auto", "rcx", "estimate"] = Field(
        "auto",
        description="`rcx` extracts the parasitics with OpenRCX, with the rules of the platform. "
        "`estimate` estimates them from the detailed route (`estimate_parasitics`). `auto` uses "
        "`rcx` when the platform has the rules, and `estimate` otherwise.",
        json_schema_extra=tier("common"),
    )


class IrDropGroup(SettingsGroup):
    """The analysis of the IR drop of the power grid."""

    enabled: bool = Field(
        True,
        description="Analyze the IR drop of the power grid, for each supply net that the "
        "platform gives a voltage. The analysis only reports, and it never fails the run. On a "
        "platform that gives no voltage for any supply net, the flow skips the analysis, and the "
        "log says so.",
        json_schema_extra=tier("advanced"),
    )


class GdsGroup(SettingsGroup):
    """The GDSII file of the routed design, which KLayout writes when `write_gds` is true."""

    seal_ring: Optional[Path] = Field(
        None,
        description="A GDSII file with a seal ring, which KLayout merges into the layout. A "
        "relative path is relative to the design root. Needs `write_gds`.",
        json_schema_extra=tier("advanced"),
    )
