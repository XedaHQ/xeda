"""The settings groups of the placement flow, one for each step of the flow.

The flow loads the netlist, floorplans, places the tap cells and the power grid, places the pins,
places the cells globally, optimizes them, and legalizes them. A leaf whose description says
"Unset: the value of the platform" is `None` in the model, and the flow resolves it from the
platform. A leaf that is `None` otherwise takes the default of the tool.

A group that holds a flag of a command prints it through `FLAGS`. The flow prints the leaves that
choose between flags or that need the platform.
"""

from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup
from .groups import RepairDesign, RepairTiming, SetupMove, differing_leaves
from .place_pdn import PdnConnect, PdnGrid

__all__ = [
    "ArithSwapGroup",
    "BufferPortsGroup",
    "DensityGroup",
    "DesignGroup",
    "DetailedPlacementGroup",
    "FloorplanGroup",
    "FloorplanRepairTiming",
    "GlobalPlacementGroup",
    "ImproveGroup",
    "InitialPlaceGroup",
    "IoGroup",
    "NesterovGroup",
    "PdnCheckGroup",
    "NetlistCleanupGroup",
    "PdnGroup",
    "PlaceRepairTiming",
    "ResizeGroup",
    "RoutabilityDrivenGroup",
    "RoutingGroup",
    "TapcellGroup",
    "TieGroup",
    "TimingDrivenGroup",
]

#: A range of routing layers as `set_routing_layers` takes it: `metal2-metal10`.
LayerRange = Annotated[str, Field(pattern=r"^[^\s-]+-[^\s-]+$")]
#: One routing layer or a range of them, as a key of `layer_adjustments`.
LayerKey = Annotated[str, Field(pattern=r"^\S+$")]
#: A rectangle: lower-left x, lower-left y, upper-right x, upper-right y, in micrometers.
Rectangle = tuple[float, float, float, float]


class FloorplanRepairTiming(RepairTiming):
    """The `repair_timing` call that cleans the netlist at the floorplan. It repairs setup with
    the moves that OpenROAD-flow-scripts uses there, and it runs the last gasp of the setup repair
    only when `last_gasp` says so."""

    fix: Literal["setup_and_hold", "setup", "hold"] = Field(
        "setup",
        description="The checks to repair: `setup`, `hold`, or `setup_and_hold`, which repairs "
        "setup first and then hold (`-setup`, `-hold`, or neither). The cleanup of the netlist "
        "repairs setup only. It runs before the placement, without parasitics.",
        json_schema_extra=tier("common"),
    )
    sequence: list[SetupMove] = Field(
        ["unbuffer", "sizeup", "swap", "vt_swap"],
        description="The setup moves, in the order that the repair tries them (`-sequence`). "
        "Empty: the default sequence of the tool. A move that `moves` switches off must not "
        "appear. `reroute` needs the parasitics of a global route. Not with `fix: hold`. The "
        "default is the sequence of OpenROAD-flow-scripts at the floorplan.",
        json_schema_extra=tier("advanced"),
    )
    last_gasp: bool = Field(
        False,
        description="Run the last pass of the setup repair, a greedy sizing of the cells. False "
        "adds `-skip_last_gasp`, which saves run time and area. Not with `fix: hold`. The "
        "default is the one of OpenROAD-flow-scripts at the floorplan.",
        json_schema_extra=tier("advanced"),
    )


class PlaceRepairTiming(RepairTiming):
    """The `repair_timing` call after the placement. OpenROAD-flow-scripts runs it only when a
    variable asks for it, so it is off by default."""

    enabled: bool = Field(
        False,
        description="Run `repair_timing` after the placement. OpenROAD-flow-scripts does not "
        "run it there by default.",
        json_schema_extra=tier("common"),
    )


class DesignGroup(SettingsGroup):
    """The loading of the netlist."""

    eliminate_dead_logic: bool = Field(
        True,
        description="Remove the standard cells that drive no output (`eliminate_dead_logic`) "
        "after the flow reads the netlist. OpenROAD-flow-scripts runs it before it writes the "
        "first database.",
        json_schema_extra=tier("advanced"),
    )
    dont_touch: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The instances and nets that the resizer must not change "
        "(`set_dont_touch`). Each item is a name or a glob pattern. The database stores the "
        "setting, so the later flows keep it.",
        json_schema_extra=tier("advanced"),
    )


class NetlistCleanupGroup(SettingsGroup):
    """The cleanup of the netlist that the floorplan runs before the placement. A synthesis flow
    leaves buffers and cells that the sizing can improve."""

    mode: Literal["repair_timing", "remove_buffers", "none"] = Field(
        "repair_timing",
        description="`repair_timing` repairs the setup with the options of `repair`. "
        "`remove_buffers` removes the buffers of the synthesis (`remove_buffers`). `none` "
        "cleans nothing. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    repair: FloorplanRepairTiming = Field(
        default_factory=FloorplanRepairTiming,
        description="The options of the repair of the netlist. They act with `mode: "
        "repair_timing` only. To run no repair, set `mode: none`.",
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a repair that is switched off while the mode runs it, and a leaf of the repair
        while the mode runs none."""
        if self.mode == "repair_timing":
            if self.repair.enabled:
                return []
            return [
                (
                    f"{key}.repair.enabled",
                    f"`{key}.repair.enabled` is false, and `{key}.mode: repair_timing` runs the "
                    f"repair: set `{key}.mode: none` to run no repair, and delete "
                    f"`{key}.repair.enabled`",
                )
            ]
        leaves = differing_leaves(self.repair, f"{key}.repair")
        if not self.repair.enabled:
            # The group of a disabled repair reports its other leaves itself.
            leaves = [leaf for leaf in leaves if leaf == f"{key}.repair.enabled"]
        return [
            (
                leaf,
                f"`{leaf}` has no effect with `{key}.mode: {self.mode}`, which runs no "
                f"`repair_timing`: set `{key}.mode: repair_timing` or delete `{leaf}`",
            )
            for leaf in leaves
        ]


class PlacementBlockage(XedaBaseModel):
    """A region where the placement keeps cells away (`create_blockage`)."""

    region: Rectangle = Field(
        description="The region, in micrometers: lower-left x, lower-left y, upper-right x, "
        "upper-right y (`-region`)."
    )
    soft: bool = Field(
        False,
        description="A soft blockage keeps cells away during the first global placement only "
        "(`-soft`). The later steps may use the region. Not with `max_density_percent`.",
    )
    max_density_percent: Optional[Annotated[float, Field(ge=0, le=100)]] = Field(
        None,
        description="The share of the region that cells may fill, in percent (`-max_density`). "
        "It makes the blockage partial. Unset: no cell may enter. Not with `soft`.",
    )


class RoutingBlockage(XedaBaseModel):
    """A region where the router keeps wires out (`create_obstruction`)."""

    layer: str = Field(min_length=1, description="The routing layer of the obstruction (`-layer`).")
    region: Rectangle = Field(
        description="The region, in micrometers: lower-left x, lower-left y, upper-right x, "
        "upper-right y (`-region`)."
    )


#: An outline: a rectangle (4 numbers) or a polygon (8 or more numbers, an even count), the
#: corners as x, y pairs in micrometers.
Outline = Annotated[list[float], Field(min_length=4)]
#: A distance that is not negative.
Distance = Annotated[float, Field(ge=0)]


class FloorplanGroup(SettingsGroup):
    """The size and the shape of the floorplan.

    The tool judges a rectangle for one outline and a polygon for the other (`IFP-0077`) and a
    `site` that the LEF files do not define, so no rule of ours repeats them."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("additional_sites", "-additional_sites", "list"),
        Flag("flip_sites", "-flip_sites", "list"),
        Flag("row_gap", "-gap", "value"),
    )

    sizing: Literal["utilization", "area", "def"] = Field(
        "utilization",
        description="How the floorplan gets its size. `utilization` computes the core from the "
        "area of the netlist, with `core_utilization_percent`, `aspect_ratio` and `core_margin`. `area` "
        "takes the die and the core from `die_area` and `core_area`. `def` takes the die, the "
        "core, the rows and the tracks from the file `def_file`.",
        json_schema_extra=tier("common"),
    )
    core_utilization_percent: float = Field(
        50.0,
        gt=0,
        le=100,
        description="The share of the core that the cells fill, in percent (`-utilization`). "
        "OpenROAD-flow-scripts leaves it to the design. The default is a middle value, not a "
        "tuned one. Needs `sizing: utilization`.",
        json_schema_extra=tier("common"),
    )
    aspect_ratio: float = Field(
        1.0,
        gt=0,
        description="The height of the core divided by its width (`-aspect_ratio`). Needs "
        "`sizing: utilization`.",
        json_schema_extra=tier("common"),
    )
    core_margin: Union[Distance, tuple[Distance, Distance, Distance, Distance]] = Field(
        1.0,
        description="The space between the core and the die, in micrometers (`-core_space`). "
        "One distance for all sides, or four: bottom, top, left, right. The default is the one "
        "of OpenROAD-flow-scripts. Needs `sizing: utilization`.",
        json_schema_extra=tier("common"),
    )
    die_area: Optional[Outline] = Field(
        None,
        description="The die, in micrometers: a rectangle as 4 numbers (lower-left x, lower-left "
        "y, upper-right x, upper-right y), or a polygon as 8 or more numbers, the x and y of each "
        "corner in turn (`-die_area`). Needs `sizing: area`, and `core_area` too. OpenROAD "
        "needs both outlines to be polygons or both rectangles. A polygon has limits: the "
        "documentation of the power grid generator (`pdngen`) says it assumes a rectangular "
        "design, and `global_placement -place_ios` does not support a polygon die.",
        json_schema_extra=tier("common"),
    )
    core_area: Optional[Outline] = Field(
        None,
        description="The core, in micrometers, written as `die_area` (`-core_area`). A rectangle "
        "must lie inside the die. Needs `sizing: area`, and `die_area` too. A polygon has the "
        "limits of `die_area` for `pdngen` and for `global_placement -place_ios`.",
        json_schema_extra=tier("common"),
    )
    def_file: Optional[Path] = Field(
        None,
        description="The DEF file that gives the die, the core, the rows and the tracks "
        "(`read_def -floorplan_initialize`). A relative path is relative to the design root. "
        "Needs `sizing: def`.",
        json_schema_extra=tier("advanced"),
    )
    site: Optional[str] = Field(
        None,
        min_length=1,
        description="The site of the rows (`-site`). Needs `sizing: utilization` or `sizing: "
        "area`. Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    additional_sites: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="More sites to make rows for (`-additional_sites`), for example for cells of "
        "double height. Needs `sizing: utilization` or `sizing: area`.",
        json_schema_extra=tier("advanced"),
    )
    flip_sites: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The sites whose rows start flipped (`-flip_sites`), for cells of mixed "
        "height whose power pin sits on another side. No platform gives them. Needs `sizing: "
        "utilization` or `sizing: area`.",
        json_schema_extra=tier("expert"),
    )
    row_parity: Literal["none", "odd", "even"] = Field(
        "none",
        description="Force the number of rows to be odd or even (`-row_parity`). `none` leaves "
        "it free, which is the default of the tool. Needs `sizing: utilization` or `sizing: "
        "area`.",
        json_schema_extra=tier("expert"),
    )
    row_gap: Optional[float] = Field(
        None,
        gt=0,
        description="The space between the rows of two power domains, in micrometers (`-gap`). "
        "Unset: the tool uses 6 times the least height of a site. Needs `sizing: utilization` "
        "or `sizing: area`.",
        json_schema_extra=tier("expert"),
    )
    tracks: Optional[Literal["platform", "from_tech"]] = Field(
        None,
        description="Where the routing tracks come from. `platform` makes the tracks that the "
        "platform lists, layer by layer. `from_tech` runs `make_tracks`, which takes the pitch "
        "of each layer from the LEF files. Unset: the tracks of the platform if it lists any, "
        "else those of the LEF files.",
        json_schema_extra=tier("advanced"),
    )
    placement_blockages: list[PlacementBlockage] = Field(
        [],
        description="The regions where the placement keeps cells away (`create_blockage`). The "
        "database stores them.",
        json_schema_extra=tier("advanced"),
    )
    routing_blockages: list[RoutingBlockage] = Field(
        [],
        description="The regions where the router keeps wires out (`create_obstruction`). The "
        "database stores them.",
        json_schema_extra=tier("advanced"),
    )
    check_setup: bool = Field(
        True,
        description="Report the unconstrained endpoints, the loops and the missing clocks "
        "(`check_setup`) before the floorplan. OpenROAD-flow-scripts runs it. It only reports.",
        json_schema_extra=tier("advanced"),
    )
    netlist_cleanup: NetlistCleanupGroup = Field(
        default_factory=NetlistCleanupGroup,
        description="The cleanup of the netlist before the placement.",
    )

    #: The leaves that one way of sizing owns, with the ways. A leaf of another way has no effect.
    OWNERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "core_utilization_percent": ("utilization",),
        "aspect_ratio": ("utilization",),
        "core_margin": ("utilization",),
        "die_area": ("area",),
        "core_area": ("area",),
        "def_file": ("def",),
        "site": ("utilization", "area"),
        "additional_sites": ("utilization", "area"),
        "flip_sites": ("utilization", "area"),
        "row_parity": ("utilization", "area"),
        "row_gap": ("utilization", "area"),
    }

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the leaves that another way of sizing owns, a way of sizing that lacks its
        leaves, an outline that is not one, and a blockage that is not one."""
        found: list[tuple[str, str]] = []
        default = type(self)()
        sizing = self.sizing
        for leaf, owners in self.OWNERS.items():
            if sizing not in owners and getattr(self, leaf) != getattr(default, leaf):
                ways = " or ".join(f"`{key}.sizing: {owner}`" for owner in owners)
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` is a leaf of {ways}, and `{key}.sizing` is `{sizing}`: "
                        f"set {ways.replace(' or ', ' or set ')} or delete `{key}.{leaf}`",
                    )
                )
        if sizing == "area":
            for leaf in ("die_area", "core_area"):
                if getattr(self, leaf) is None:
                    found.append(
                        (
                            f"{key}.{leaf}",
                            f"`{key}.sizing: area` needs `{key}.die_area` and "
                            f"`{key}.core_area`, and `{key}.{leaf}` is not set: give both",
                        )
                    )
        if sizing == "def" and self.def_file is None:
            found.append(
                (
                    f"{key}.def_file",
                    f"`{key}.sizing: def` takes the floorplan from `{key}.def_file`, and that "
                    f"is not set: give it or set `{key}.sizing: utilization`",
                )
            )
        for leaf in ("die_area", "core_area"):
            outline = getattr(self, leaf)
            if outline is None:
                continue
            if len(outline) != 4 and (len(outline) < 8 or len(outline) % 2):
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` is a rectangle of 4 numbers or a polygon of 8 or more "
                        f"numbers in pairs, and it has {len(outline)}: list the x and y of each "
                        "corner",
                    )
                )
            elif len(outline) == 4 and not (outline[0] < outline[2] and outline[1] < outline[3]):
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` must list the lower-left corner and then the "
                        "upper-right corner, so that x and y of the second are larger than "
                        "x and y of the first",
                    )
                )
        die, core = self.die_area, self.core_area
        if (
            die is not None
            and core is not None
            and len(die) == 4
            and len(core) == 4
            and die[0] < die[2]
            and die[1] < die[3]
            and core[0] < core[2]
            and core[1] < core[3]
            and not (
                die[0] <= core[0] and die[1] <= core[1] and core[2] <= die[2] and core[3] <= die[3]
            )
        ):
            found.append(
                (
                    f"{key}.core_area",
                    f"`{key}.core_area` must lie inside `{key}.die_area`: move the core or "
                    "enlarge the die",
                )
            )
        for number, blockage in enumerate(self.placement_blockages):
            where = f"{key}.placement_blockages[{number}]"
            if blockage.soft and blockage.max_density_percent is not None:
                found.append(
                    (
                        f"{key}.placement_blockages",
                        f"`{where}` has `soft` and `max_density_percent`: OpenROAD ignores `soft` when "
                        "it gets a density, because a density makes the blockage partial. Give "
                        "one of them",
                    )
                )
        regions: list[tuple[str, int, Rectangle]] = [
            ("placement_blockages", number, blockage.region)
            for number, blockage in enumerate(self.placement_blockages)
        ] + [
            ("routing_blockages", number, blockage.region)
            for number, blockage in enumerate(self.routing_blockages)
        ]
        for name, number, region in regions:
            x0, y0, x1, y1 = region
            if not (x0 < x1 and y0 < y1):
                found.append(
                    (
                        f"{key}.{name}",
                        f"`{key}.{name}[{number}].region` must list the lower-left corner "
                        "and then the upper-right corner, so that x and y of the second "
                        "are larger than x and y of the first",
                    )
                )
        return found


class RoutingGroup(SettingsGroup):
    """The routing layers and the capacity of each layer. The database stores them, so
    `openroad_place` sets them and the later flows keep them."""

    signal_layers: Optional[LayerRange] = Field(
        None,
        description="The lowest and the highest layer for signal nets, written `min-max` "
        "(`set_routing_layers -signal`). Unset: the value of the platform.",
        json_schema_extra=tier("common"),
    )
    clock_layers: Optional[LayerRange] = Field(
        None,
        description="The lowest and the highest layer for clock nets, written `min-max` "
        "(`set_routing_layers -clock`). Unset: the value of the platform.",
        json_schema_extra=tier("common"),
    )
    layer_adjustments: Optional[dict[LayerKey, Annotated[float, Field(ge=0, le=1)]]] = Field(
        None,
        description="The share of the routing capacity to keep free, from 0 to 1, by layer or "
        "layer range such as `metal2-metal3` (`set_global_routing_layer_adjustment`). Unset: the "
        "value of the platform.",
        json_schema_extra=tier("advanced"),
    )


class TieGroup(SettingsGroup):
    """The tie cells that drive the constant nets."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("separation", "-separation", "value"),
        Flag("max_fanout", "-max_fanout", "value"),
    )

    repair_fanout: bool = Field(
        True,
        description="Give each load of a tie cell its own copy of the cell "
        "(`repair_tie_fanout`). OpenROAD-flow-scripts runs it, before the removal of buffers.",
        json_schema_extra=tier("common"),
    )
    separation: float = Field(
        0.0,
        ge=0,
        description="The distance between a tie cell and its load, in micrometers "
        "(`repair_tie_fanout -separation`). The default is the one of the tool and of "
        "OpenROAD-flow-scripts. Needs `repair_fanout`.",
        json_schema_extra=tier("advanced"),
    )
    max_fanout: Optional[int] = Field(
        None,
        ge=1,
        description="The most loads for one copy of a tie cell (`repair_tie_fanout "
        "-max_fanout`). Unset: the flag is not passed. OpenROAD v2.0-26132 reads the value and "
        "does not pass it to the repair, so it has no effect there. No upstream issue exists "
        "yet. Needs `repair_fanout`.",
        json_schema_extra=tier("expert"),
    )
    insert_cells: bool = Field(
        False,
        description="Insert a tie cell for each net that the netlist ties to a constant "
        "(`insert_tiecells`), before the repair of the fanout. The synthesis of xeda puts the "
        "tie cells in the netlist, so OpenROAD-flow-scripts does not run this.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse an option of the repair when no tie fanout is repaired."""
        if self.repair_fanout:
            return []
        return [
            (
                f"{key}.{leaf}",
                f"`{key}.{leaf}` has no effect with `{key}.repair_fanout: false`: set "
                f"`{key}.repair_fanout: true` or delete `{key}.{leaf}`",
            )
            for leaf in ("separation", "max_fanout")
            if getattr(self, leaf) != getattr(type(self)(), leaf)
        ]


def _master(flag: str, what: str) -> Any:
    """A tap cell master that a platform may name: `None` leaves the flag out."""
    return Field(
        None,
        min_length=1,
        description=f"The cell for {what} (`tapcell {flag}`). Needs `tapcell.mode: tap_endcap`. "
        "Unset: the flag is not passed.",
        json_schema_extra=tier("expert"),
    )


class NwellMastersGroup(SettingsGroup):
    """The tap cells at the top and the bottom of the core and of the macros, by the kind of
    n-well. Platforms with tap cells in several widths use them (asap7, ihp-sg13g2)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("nwin2", "-tap_nwin2_master", "value"),
        Flag("nwin3", "-tap_nwin3_master", "value"),
        Flag("nwintie", "-tap_nwintie_master", "value"),
        Flag("nwout2", "-tap_nwout2_master", "value"),
        Flag("nwout3", "-tap_nwout3_master", "value"),
        Flag("nwouttie", "-tap_nwouttie_master", "value"),
    )

    nwin2: Optional[str] = _master("-tap_nwin2_master", "a two-site tap with the n-well inside")
    nwin3: Optional[str] = _master("-tap_nwin3_master", "a three-site tap with the n-well inside")
    nwintie: Optional[str] = _master("-tap_nwintie_master", "a tie tap with the n-well inside")
    nwout2: Optional[str] = _master("-tap_nwout2_master", "a two-site tap with the n-well outside")
    nwout3: Optional[str] = _master(
        "-tap_nwout3_master", "a three-site tap with the n-well outside"
    )
    nwouttie: Optional[str] = _master("-tap_nwouttie_master", "a tie tap with the n-well outside")


class CornerMastersGroup(SettingsGroup):
    """The cells at the corners of the core and of the macros, by the kind of n-well."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("nwin", "-cnrcap_nwin_master", "value"),
        Flag("nwout", "-cnrcap_nwout_master", "value"),
        Flag("inner_nwin", "-incnrcap_nwin_master", "value"),
        Flag("inner_nwout", "-incnrcap_nwout_master", "value"),
    )

    nwin: Optional[str] = _master("-cnrcap_nwin_master", "an outer corner with the n-well inside")
    nwout: Optional[str] = _master(
        "-cnrcap_nwout_master", "an outer corner with the n-well outside"
    )
    inner_nwin: Optional[str] = _master(
        "-incnrcap_nwin_master", "an inner corner with the n-well inside"
    )
    inner_nwout: Optional[str] = _master(
        "-incnrcap_nwout_master", "an inner corner with the n-well outside"
    )


class TapcellGroup(SettingsGroup):
    """The well tap cells and the end cap cells."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("row_min_width", "-row_min_width", "value"),
        Flag("row_min_height", "-row_min_height", "value"),
    )

    mode: Optional[Literal["tap_endcap", "cut_rows", "none"]] = Field(
        None,
        description="`tap_endcap` places tap cells and end caps (`tapcell`). `cut_rows` cuts the "
        "rows around obstructions, without tap cells (`cut_rows`). `none` does neither. Unset: "
        "the value of the platform.",
        json_schema_extra=tier("common"),
    )
    distance: Optional[float] = Field(
        None,
        gt=0,
        description="The distance between the tap cells in a row, in micrometers "
        "(`tapcell -distance`). Needs `mode: tap_endcap`. Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    master: Optional[str] = Field(
        None,
        min_length=1,
        description="The tap cell (`tapcell -tapcell_master`). Needs `mode: tap_endcap`. Unset: "
        "the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    endcap_master: Optional[str] = Field(
        None,
        min_length=1,
        description="The end cap cell (`-endcap_master` of `tapcell` and of `cut_rows`). Needs "
        "`mode: tap_endcap` or `mode: cut_rows`. Unset: the value of the platform.",
        json_schema_extra=tier("advanced"),
    )
    row_min_width: Optional[float] = Field(
        None,
        ge=0,
        description="Remove the pieces of a row that are narrower than this, in micrometers "
        "(`-row_min_width` of `tapcell` and of `cut_rows`). They would be islands that the "
        "router cannot reach. Needs `mode: tap_endcap` or `mode: cut_rows`. Unset: the tool "
        "uses twice the width of the end cap, or 0 without an end cap.",
        json_schema_extra=tier("advanced"),
    )
    row_min_height: Optional[float] = Field(
        None,
        ge=0,
        description="The least height of a stack of rows, in micrometers (`-row_min_height` of "
        "`tapcell` and of `cut_rows`). Needs `mode: tap_endcap` or `mode: cut_rows`. Unset: the "
        "tool uses twice the height of the end cap plus the height of the tallest cell.",
        json_schema_extra=tier("expert"),
    )
    nwell_masters: NwellMastersGroup = Field(
        default_factory=NwellMastersGroup,
        description="The tap cells by the kind of n-well. The platform gives none of them.",
    )
    corner_masters: CornerMastersGroup = Field(
        default_factory=CornerMastersGroup,
        description="The corner cells by the kind of n-well. The platform gives none of them.",
    )

    #: The leaves that only `mode: tap_endcap` uses, and the leaves that `mode: cut_rows` uses
    #: too. `mode: none` uses none of them.
    TAP_ONLY: ClassVar[Tuple[str, ...]] = ("distance", "master")
    ROWS: ClassVar[Tuple[str, ...]] = ("endcap_master", "row_min_width", "row_min_height")

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a leaf that the mode does not use."""
        found: list[tuple[str, str]] = []
        masters = [
            *differing_leaves(self.nwell_masters, f"{key}.nwell_masters"),
            *differing_leaves(self.corner_masters, f"{key}.corner_masters"),
        ]
        default = type(self)()
        unused = {
            "none": [*self.TAP_ONLY, *self.ROWS],
            "cut_rows": list(self.TAP_ONLY),
        }.get(self.mode or "", [])
        leaves = [
            f"{key}.{leaf}" for leaf in unused if getattr(self, leaf) != getattr(default, leaf)
        ]
        if self.mode in ("none", "cut_rows"):
            leaves += masters
        for leaf in leaves:
            found.append(
                (
                    leaf,
                    f"`{leaf}` has no effect with `{key}.mode: {self.mode}`: set "
                    f"`{key}.mode: tap_endcap` or delete `{leaf}`",
                )
            )
        return found


class PdnCheckGroup(SettingsGroup):
    """The check that the power grid is connected, after the grid is built."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("require_terminals", "-dont_require_terminals", "skip"),
    )

    mode: Literal["off", "warn", "fail"] = Field(
        "off",
        description="`off` runs no check. `warn` runs `check_power_grid` on the power net and the "
        "ground net and logs a warning when it fails. `fail` stops the flow when it fails. "
        "OpenROAD-flow-scripts runs no check.",
        json_schema_extra=tier("advanced"),
    )
    require_terminals: bool = Field(
        False,
        description="Require that the grid reaches a terminal of the design (a pin or a pad). "
        "False passes `-dont_require_terminals`: a design without pads has no terminal on its "
        "power nets, and the check stops with PSM-0025 without that flag. Needs `mode` `warn` "
        "or `fail`.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the terminals of a check that does not run."""
        if self.mode == "off" and self.require_terminals:
            return [
                (
                    f"{key}.require_terminals",
                    f"`{key}.require_terminals` has no effect with `{key}.mode: off`: set "
                    f"`{key}.mode: warn` or `{key}.mode: fail`, or delete "
                    f"`{key}.require_terminals`",
                )
            ]
        return []


class PdnGroup(SettingsGroup):
    """The power grid over the standard cells."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("skip_trim", "-skip_trim", "switch"),
        Flag("block_pins", "-dont_add_pins", "skip"),
    )

    mode: Literal["generated", "none"] = Field(
        "generated",
        description="`generated` builds the grid from `grid`, or from the grid of the platform. "
        "`none` builds no grid.",
        json_schema_extra=tier("common"),
    )
    grid: Optional[PdnGrid] = Field(
        None,
        description="The grid: the supply nets, the connections of the pins to them, the rails, "
        "the straps, the rings, the pairs of layers that the vias connect, and the layers of the "
        "pins. Each leaf that you give, and that is not empty, replaces the same leaf of the "
        "grid of the platform. Needs `mode: generated`. Unset: the grid of the platform.",
        json_schema_extra=tier("common"),
    )
    secondary_power: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The secondary power nets of the voltage domain "
        "(`set_voltage_domain -secondary_power`). Needs `mode: generated`.",
        json_schema_extra=tier("advanced"),
    )
    block_pins: bool = Field(
        True,
        description="Promote the straps of the layers in `grid.pin_layers` to pins of the "
        "block. False adds `-dont_add_pins` to `pdngen`, which makes no pins at all. Needs "
        "`mode: generated`.",
        json_schema_extra=tier("advanced"),
    )
    skip_trim: bool = Field(
        False,
        description="Keep the metal stubs that the generator would remove (`pdngen "
        "-skip_trim`). Needs `mode: generated`.",
        json_schema_extra=tier("expert"),
    )
    check: PdnCheckGroup = Field(
        default_factory=PdnCheckGroup,
        description="The check of the grid after it is built.",
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a leaf when no grid is built, and a staggering of cuts that has no cuts."""
        found: list[tuple[str, str]] = []
        if self.mode == "none":
            default = type(self)()
            leaves: list[str] = []
            for name in type(self).model_fields:
                value = getattr(self, name)
                if name == "mode":
                    continue
                if isinstance(value, SettingsGroup):
                    leaves += differing_leaves(value, f"{key}.{name}")
                elif value != getattr(default, name):
                    leaves.append(f"{key}.{name}")
            found += [
                (
                    leaf,
                    f"`{leaf}` has no effect with `{key}.mode: none`: set "
                    f"`{key}.mode: generated` or delete `{leaf}`",
                )
                for leaf in leaves
            ]
        for connect in self.grid.connects if self.grid is not None else []:
            if isinstance(connect, PdnConnect) and connect.split_cuts_staggered:
                if not connect.split_cuts:
                    found.append(
                        (
                            f"{key}.grid.connects",
                            f"the connection of {connect.layers[0]} and {connect.layers[1]} has "
                            "`split_cuts_staggered`, and it has no `split_cuts`: give the "
                            "layers that use split cuts, or delete `split_cuts_staggered`",
                        )
                    )
        return found


class IoGroup(SettingsGroup):
    """The placement of the pins of the design (`place_pins`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("corner_avoidance", "-corner_avoidance", "value"),)
    #: The flags of `place_pins` that no setting covers. The layers, the distance and the corner
    #: avoidance have settings, and the flow decides the others. The flow prints `min_distance`
    #: itself: it is a whole number in tracks.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-annealing", "-exclude", "-group_pins"})

    flow: Literal["skip_io_pass", "concurrent", "fixed"] = Field(
        "skip_io_pass",
        description="How the pins get their places. `skip_io_pass` places the cells without the "
        "pins first, then places the pins, then places the cells again. `concurrent` places the "
        "pins together with the cells (`global_placement -place_ios`), then runs `place_pins` to "
        "legalize them. OpenROAD v2.0-26132 refuses `-place_ios` together with the "
        "timing-driven and the routability-driven placement (GPL-0179 and GPL-0181), so set both "
        "modes off for it. "
        "`fixed` keeps the pins that the floorplan already placed: it needs a DEF file with placed "
        "pins (`floorplan.sizing: def`), else the global placement stops with GPL-0326 on the "
        "unplaced pins. With `floorplan.sizing: def` the pins of the DEF file stay, except "
        "with `concurrent`. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    horizontal_layers: Optional[list[Annotated[str, Field(min_length=1)]]] = Field(
        None,
        description="The layers of the pins on the horizontal edges (`-hor_layers`). Unset: the "
        "value of the platform.",
        json_schema_extra=tier("common"),
    )
    vertical_layers: Optional[list[Annotated[str, Field(min_length=1)]]] = Field(
        None,
        description="The layers of the pins on the vertical edges (`-ver_layers`). Unset: the "
        "value of the platform.",
        json_schema_extra=tier("common"),
    )
    min_distance: Optional[float] = Field(
        None,
        ge=0,
        description="The smallest distance between two pins, in the unit of `min_distance_unit` "
        "(`-min_distance`). In tracks it must be a whole number, which is what OpenROAD takes. "
        "Unset: the default of the tool, 2 tracks.",
        json_schema_extra=tier("advanced"),
    )
    min_distance_unit: Literal["um", "tracks"] = Field(
        "um",
        description="The unit of `min_distance`: micrometers or routing tracks "
        "(`-min_distance_in_tracks`). Needs `min_distance`.",
        json_schema_extra=tier("advanced"),
    )
    corner_avoidance: Optional[float] = Field(
        None,
        ge=0,
        description="The distance that the pins keep from the corners of the die, in "
        "micrometers (`-corner_avoidance`). Unset: the default of the tool, 0.",
        json_schema_extra=tier("advanced"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `place_pins`. Each flag must be one of `-annealing`, "
        "`-exclude` and `-group_pins`, each followed by its value where it takes one.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a unit without a distance, a fraction of a track, an empty list of layers, and a
        setting for the placement of the pins when the pins stay where the floorplan put them."""
        found: list[tuple[str, str]] = []
        if self.flow == "fixed":
            for leaf in differing_leaves(self, key):
                if leaf != f"{key}.flow":
                    found.append(
                        (
                            leaf,
                            f"`{leaf}` has no effect with `{key}.flow: fixed`, which keeps the "
                            f"pins that the floorplan placed: set `{key}.flow: skip_io_pass` or "
                            f"delete `{leaf}`",
                        )
                    )
        for leaf in ("horizontal_layers", "vertical_layers"):
            if getattr(self, leaf) == []:
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` is empty: name at least one layer, or delete it to "
                        "take the layers of the platform",
                    )
                )
        if (
            self.min_distance_unit == "tracks"
            and self.min_distance is not None
            and self.min_distance != int(self.min_distance)
        ):
            found.append(
                (
                    f"{key}.min_distance",
                    f"`{key}.min_distance` is {self.min_distance} tracks, and OpenROAD takes a "
                    f"whole number of tracks: give a whole number or set "
                    f"`{key}.min_distance_unit: um`",
                )
            )
        if self.min_distance_unit != "um" and self.min_distance is None:
            found.append(
                (
                    f"{key}.min_distance_unit",
                    f"`{key}.min_distance_unit: {self.min_distance_unit}` is the unit of "
                    f"`{key}.min_distance`, and that is not set: give it or delete "
                    f"`{key}.min_distance_unit`",
                )
            )
        return found


class DensityGroup(SettingsGroup):
    """The target density of the global placement."""

    mode: Literal["auto", "fixed", "uniform"] = Field(
        "fixed",
        description="`fixed` uses `value`, or the density of the platform when `value` is "
        "unset (`-density`). `auto` computes the density from the design: the uniform density "
        "of the netlist, plus `headroom` of the space that is left. `uniform` uses the uniform "
        "density of the netlist (`-density uniform`). The default is the one of "
        "OpenROAD-flow-scripts without a headroom.",
        json_schema_extra=tier("common"),
    )
    value: Optional[float] = Field(
        None,
        gt=0,
        le=1,
        description="The target density, from 0 to 1. Needs `mode: fixed`. Unset: the value of "
        "the platform.",
        json_schema_extra=tier("common"),
    )
    headroom: Optional[float] = Field(
        None,
        ge=0,
        le=0.99,
        description="The share of the free space above the uniform density that the target "
        "adds, from 0 to 0.99. Needs `mode: auto`, and `mode: auto` needs it.",
        json_schema_extra=tier("common"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a value or a headroom that the mode does not use, and `auto` with no headroom."""
        found: list[tuple[str, str]] = []
        if self.mode != "fixed" and self.value is not None:
            found.append(
                (
                    f"{key}.value",
                    f"`{key}.value` has no effect with `{key}.mode: {self.mode}`: set "
                    f"`{key}.mode: fixed` or delete `{key}.value`",
                )
            )
        if self.mode != "auto" and self.headroom is not None:
            found.append(
                (
                    f"{key}.headroom",
                    f"`{key}.headroom` has no effect with `{key}.mode: {self.mode}`: set "
                    f"`{key}.mode: auto` or delete `{key}.headroom`",
                )
            )
        if self.mode == "auto" and self.headroom is None:
            found.append(
                (
                    f"{key}.headroom",
                    f"`{key}.mode: auto` needs `{key}.headroom`: give it or set "
                    f"`{key}.mode: fixed`",
                )
            )
        return found


class TimingDrivenGroup(SettingsGroup):
    """The timing-driven mode of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("enabled", "-timing_driven", "switch"),)

    enabled: bool = Field(
        True,
        description="Weight the nets by their slack during the placement (`-timing_driven`). The "
        "mode needs clocks in the SDC and the wire RC. It costs run time. The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )


class RoutabilityDrivenGroup(SettingsGroup):
    """The routability-driven mode of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("enabled", "-routability_driven", "switch"),)

    enabled: bool = Field(
        True,
        description="Inflate the cells in congested regions during the placement "
        "(`-routability_driven`). The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )


class InitialPlaceGroup(SettingsGroup):
    """The initial placement of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("from_center", "-force_center_initial_place", "switch"),
    )

    from_center: bool = Field(
        True,
        description="Start every cell at the center of the core, also when the database has "
        "locations (`-force_center_initial_place`). OpenROAD-flow-scripts gives the flag to the "
        "main pass of the placement.",
        json_schema_extra=tier("expert"),
    )


class NesterovGroup(SettingsGroup):
    """The solver of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("min_phi_coef", "-min_phi_coef", "value"),
        Flag("max_phi_coef", "-max_phi_coef", "value"),
    )

    min_phi_coef: Optional[float] = Field(
        None,
        gt=0,
        description="The smallest factor of the step size of the solver (`-min_phi_coef`). "
        "Useful when the placement diverges. Unset: the default of the tool, 0.95.",
        json_schema_extra=tier("advanced"),
    )
    max_phi_coef: Optional[float] = Field(
        None,
        gt=0,
        description="The largest factor of the step size of the solver (`-max_phi_coef`). "
        "Unset: the default of the tool, 1.05.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a smallest factor that is above the largest."""
        low, high = self.min_phi_coef, self.max_phi_coef
        if low is not None and high is not None and low > high:
            return [
                (
                    f"{key}.min_phi_coef",
                    f"`{key}.min_phi_coef` is {low}, above `{key}.max_phi_coef`, which is "
                    f"{high}: lower the first or raise the second",
                )
            ]
        return []


class GlobalPlacementGroup(SettingsGroup):
    """The global placement of the cells (`global_placement`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("random_seed", "-random_seed", "value"),)
    #: The flags of `global_placement` that no setting covers: the one that skips the initial
    #: placement, and the options of the routability mode that the tool added late.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "-skip_initial_place",
            "-routability_max_inflation_total",
            "-routability_net_weight_max",
            "-routability_congested_nets_percentage",
        }
    )

    density: DensityGroup = Field(
        default_factory=DensityGroup,
        description="The target density.",
    )
    cell_padding: int = Field(
        0,
        ge=0,
        description="The empty sites that each cell keeps on its left and on its right during "
        "the placement (`-pad_left`, `-pad_right`), in sites. The default is the one of "
        "OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    timing_driven: TimingDrivenGroup = Field(
        default_factory=TimingDrivenGroup,
        description="The timing-driven mode.",
    )
    routability_driven: RoutabilityDrivenGroup = Field(
        default_factory=RoutabilityDrivenGroup,
        description="The routability-driven mode.",
    )
    initial_place: InitialPlaceGroup = Field(
        default_factory=InitialPlaceGroup,
        description="The initial placement.",
    )
    nesterov: NesterovGroup = Field(
        default_factory=NesterovGroup,
        description="The solver of the placement.",
    )
    random_seed: Optional[int] = Field(
        None,
        description="The seed that perturbs the cells at the start (`-random_seed`). Different "
        "seeds give different placements. Unset: the default of the tool, 1.",
        json_schema_extra=tier("advanced"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `global_placement`. Each flag must be one of "
        "`-skip_initial_place`, `-routability_max_inflation_total`, "
        "`-routability_net_weight_max` and `-routability_congested_nets_percentage`, each "
        "followed by its value where it takes one.",
        json_schema_extra=tier("expert"),
    )


class BufferPortsGroup(SettingsGroup):
    """The buffers between the ports of the design and its logic."""

    enabled: bool = Field(
        True,
        description="Put a buffer between each port and the cells that it drives or that "
        "drive it (`buffer_ports`). OpenROAD-flow-scripts runs it.",
        json_schema_extra=tier("common"),
    )


class ResizeGroup(SettingsGroup):
    """The optimization of the design after the global placement."""

    remove_buffers: bool = Field(
        True,
        description="Remove the buffers of the synthesis before the global placement "
        "(`remove_buffers`), so that the repair chooses the buffering. Needs "
        "`global_placement.timing_driven.enabled`: OpenROAD-flow-scripts removes the buffers "
        "only with the timing-driven placement, and so does this flow.",
        json_schema_extra=tier("advanced"),
    )
    buffer_ports: BufferPortsGroup = Field(
        default_factory=BufferPortsGroup,
        description="The buffers between the ports and the logic.",
    )
    repair_design: RepairDesign = Field(
        default_factory=RepairDesign,
        description="The call of `repair_design` after the placement.",
    )
    repair_timing: PlaceRepairTiming = Field(
        default_factory=PlaceRepairTiming,
        description="The call of `repair_timing` after the placement. It repairs setup and "
        "hold.",
    )


class ArithSwapGroup(SettingsGroup):
    """The swap of the arithmetic modules for faster or smaller variants."""

    enabled: bool = Field(
        False,
        description="Replace the wrapped arithmetic modules of the netlist (`replace_arith_modules`). "
        "It needs `hierarchical` and a netlist with wrapped operators.",
        json_schema_extra=tier("advanced"),
    )


class ImproveGroup(SettingsGroup):
    """The optimization of the detailed placement (`improve_placement`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("max_displacement", "-max_displacement", "list"),)

    enabled: bool = Field(
        True,
        description="Run `improve_placement` after the legalization. It swaps and moves "
        "cells to shorten the wires. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    max_displacement: Annotated[
        list[Annotated[int, Field(ge=0)]], Field(min_length=2, max_length=2)
    ] = Field(
        [5, 1],
        description="The farthest that a cell moves in x and in y, as two integers "
        "(`-max_displacement`). The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )


class DetailedPlacementGroup(SettingsGroup):
    """The legalization and the optimization of the placement."""

    #: The flags of `detailed_placement` that no setting covers: the options of the legalizer,
    #: which the tool changes often.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "-use_diamond_legalizer",
            "-site_search_window",
            "-row_search_window",
            "-drc_penalty",
            "-disable_window_extension",
        }
    )

    cell_padding: int = Field(
        0,
        ge=0,
        description="The empty sites that each cell keeps on its left and on its right during "
        "the legalization (`set_placement_padding -global -left N -right N`), in sites. The "
        "default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    improve: ImproveGroup = Field(
        default_factory=ImproveGroup,
        description="The optimization of the placement after the legalization.",
    )
    mirroring: bool = Field(
        True,
        description="Flip cells about the y axis where that shortens the wires "
        "(`optimize_mirroring`). The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    check: Literal["fail", "report"] = Field(
        "report",
        description="What the flow does with an illegal placement (`check_placement`, which raises "
        "an error when a check fails). `fail` stops the flow, as OpenROAD-flow-scripts does. "
        "`report` prints the violations and goes on.",
        json_schema_extra=tier("common"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `detailed_placement`. Each flag must be one of "
        "`-use_diamond_legalizer`, `-site_search_window`, `-row_search_window`, "
        "`-drc_penalty` and `-disable_window_extension`, each followed by its value where it "
        "takes one.",
        json_schema_extra=tier("expert"),
    )
