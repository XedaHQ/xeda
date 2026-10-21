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

from ...dataclass import Field, XedaBaseModel, extras, tier
from ...flow.settings_group import Flag, SettingsGroup
from ...units import Bounds, Length, Time, count, library_units, percent, ratio
from .groups import Legalize, RepairDesign, RepairTiming, SetupMove, differing_leaves
from .place_pdn import PdnConnect, PdnGrid

__all__ = [
    "AnnealingGroup",
    "ArithSwapGroup",
    "BufferPortsGroup",
    "DensityGroup",
    "DesignGroup",
    "DetailedPlacementGroup",
    "EdgeInterval",
    "FixedPin",
    "FloorplanGroup",
    "FloorplanRepairTiming",
    "FlopClusteringGroup",
    "GlobalPlacementGroup",
    "GlobalSizingGroup",
    "ImproveGroup",
    "InitialPlaceGroup",
    "IoGroup",
    "NesterovGroup",
    "OptConfigGroup",
    "PdnCheckGroup",
    "NetlistCleanupGroup",
    "PdnGroup",
    "PinConstraint",
    "PinExtensionGroup",
    "PinGroup",
    "PinLengthGroup",
    "PinThicknessGroup",
    "PlaceRepairTiming",
    "PresizeMode",
    "ResizeGroup",
    "RoutabilityDrivenGroup",
    "RoutingGroup",
    "SwapTarget",
    "TapcellGroup",
    "TieGroup",
    "TimingDrivenGroup",
]

#: A range of routing layers as `set_routing_layers` takes it: `metal2-metal10`.
LayerRange = Annotated[str, Field(pattern=r"^[^\s-]+-[^\s-]+$")]
#: One routing layer or a range of them, as a key of `layer_adjustments`.
LayerKey = Annotated[str, Field(pattern=r"^\S+$")]
#: A rectangle: lower-left x, lower-left y, upper-right x, upper-right y.
Rectangle = tuple[Length, Length, Length, Length]
#: A length that is larger than 0.
PositiveLength = Annotated[Length, Bounds(gt="0 um")]


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
        description="The region, four lengths with units: lower-left x, lower-left y, "
        "upper-right x, upper-right y (`-region`).",
        json_schema_extra=tier("advanced"),
    )
    soft: bool = Field(
        False,
        description="A soft blockage keeps cells away during the first global placement only "
        "(`-soft`). The later steps may use the region. Not with `max_density_percent`.",
        json_schema_extra=tier("advanced"),
    )
    max_density_percent: Optional[Annotated[float, Field(ge=0, le=100)]] = Field(
        None,
        description="The share of the region that cells may fill, in percent (`-max_density`). "
        "It makes the blockage partial. Unset: no cell may enter. Not with `soft`.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )


class RoutingBlockage(XedaBaseModel):
    """A region where the router keeps wires out (`create_obstruction`)."""

    layer: str = Field(
        min_length=1,
        description="The routing layer of the obstruction (`-layer`).",
        json_schema_extra=tier("advanced"),
    )
    region: Rectangle = Field(
        description="The region, four lengths with units: lower-left x, lower-left y, "
        "upper-right x, upper-right y (`-region`).",
        json_schema_extra=tier("advanced"),
    )


#: An outline: a rectangle (4 lengths) or a polygon (8 or more lengths, an even count), the
#: corners as x, y pairs.
Outline = Annotated[list[Length], Field(min_length=4)]
#: A distance that is not negative.
Distance = Annotated[Length, Bounds(ge="0 um")]


class FloorplanGroup(SettingsGroup):
    """The size and the shape of the floorplan.

    The tool judges a rectangle for one outline and a polygon for the other (`IFP-0077`) and a
    `site` that the LEF files do not define, so no rule of ours repeats them."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("additional_sites", "-additional_sites", "list"),
        Flag("flip_sites", "-flip_sites", "list"),
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
        json_schema_extra=extras(tier("common"), percent()),
    )
    aspect_ratio: float = Field(
        1.0,
        gt=0,
        description="The height of the core divided by its width (`-aspect_ratio`). Needs "
        "`sizing: utilization`.",
        json_schema_extra=extras(tier("common"), ratio()),
    )
    core_margin: Union[Distance, tuple[Distance, Distance, Distance, Distance]] = Field(
        Length("1 um"),
        description="The space between the core and the die (`-core_space`), lengths with "
        "units, such as `1 um`. One distance for all sides, or four: bottom, top, left, right. "
        "The default is the one of OpenROAD-flow-scripts. Needs `sizing: utilization`.",
        json_schema_extra=tier("common"),
    )
    die_area: Optional[Outline] = Field(
        None,
        description="The die, as lengths with units: a rectangle as 4 lengths (lower-left x, "
        "lower-left y, upper-right x, upper-right y), or a polygon as 8 or more lengths, the x "
        "and y of each corner in turn (`-die_area`). Needs `sizing: area`, and `core_area` too. OpenROAD "
        "needs both outlines to be polygons or both rectangles. A polygon has limits: the "
        "documentation of the power grid generator (`pdngen`) says it assumes a rectangular "
        "design, and `global_placement -place_ios` does not support a polygon die.",
        json_schema_extra=tier("common"),
    )
    core_area: Optional[Outline] = Field(
        None,
        description="The core, written as `die_area` (`-core_area`). A rectangle "
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
    row_gap: Optional[PositiveLength] = Field(
        None,
        description="The space between the rows of two power domains, a length with a unit "
        "(`-gap`). Unset: the tool uses 6 times the least height of a site. Needs `sizing: utilization` "
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
                        f"`{key}.{leaf}` is a rectangle of 4 lengths or a polygon of 8 or more "
                        f"lengths in pairs, and it has {len(outline)}: list the x and y of each "
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
        json_schema_extra=extras(tier("advanced"), ratio()),
    )


class TieGroup(SettingsGroup):
    """The tie cells that drive the constant nets."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("max_fanout", "-max_fanout", "value"),)

    repair_fanout: bool = Field(
        True,
        description="Give each load of a tie cell its own copy of the cell "
        "(`repair_tie_fanout`). OpenROAD-flow-scripts runs it, before the removal of buffers.",
        json_schema_extra=tier("common"),
    )
    separation: Distance = Field(
        Length("0 um"),
        description="The distance between a tie cell and its load, a length with a unit "
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
        json_schema_extra=extras(tier("expert"), count()),
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

    mode: Optional[Literal["tap_endcap", "cut_rows", "none"]] = Field(
        None,
        description="`tap_endcap` places tap cells and end caps (`tapcell`). `cut_rows` cuts the "
        "rows around obstructions, without tap cells (`cut_rows`). `none` does neither. Unset: "
        "the value of the platform.",
        json_schema_extra=tier("common"),
    )
    distance: Optional[PositiveLength] = Field(
        None,
        description="The distance between the tap cells in a row, a length with a unit "
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
    row_min_width: Optional[Distance] = Field(
        None,
        description="Remove the pieces of a row that are narrower than this, a length with a "
        "unit (`-row_min_width` of `tapcell` and of `cut_rows`). They would be islands that the "
        "router cannot reach. Needs `mode: tap_endcap` or `mode: cut_rows`. Unset: the tool "
        "uses twice the width of the end cap, or 0 without an end cap.",
        json_schema_extra=tier("advanced"),
    )
    row_min_height: Optional[Distance] = Field(
        None,
        description="The least height of a stack of rows, a length with a unit "
        "(`-row_min_height` of `tapcell` and of `cut_rows`). Needs `mode: tap_endcap` or `mode: cut_rows`. Unset: the "
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


#: A finite number, positive or not.
Finite = Annotated[float, Field(allow_inf_nan=False)]
#: A finite number that is not negative.
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
#: A finite number above zero.
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
#: A pin name or a pattern of pin names, as `get_ports` takes it.
PinName = Annotated[str, Field(min_length=1)]


class EdgeInterval(XedaBaseModel):
    """A part of one edge of the die, in micrometers along the edge. The interval of the pin
    placer is `edge:begin-end`, and `*` stands for the start or for the end of the edge."""

    edge: Literal["top", "bottom", "left", "right"] = Field(
        description="The edge of the die. Along `top` and `bottom` the position is x, along "
        "`left` and `right` it is y.",
        json_schema_extra=tier("advanced"),
    )
    begin: Optional[Distance] = Field(
        None,
        description="Where the interval starts, a length with a unit. Unset: at the start of "
        "the edge.",
        json_schema_extra=tier("advanced"),
    )
    end: Optional[Distance] = Field(
        None,
        description="Where the interval ends, a length with a unit. Unset: at the end of the "
        "edge.",
        json_schema_extra=tier("advanced"),
    )

    def as_region(self) -> str:
        """Return the interval as the pin placer reads it: `top:*`, `left:*-2`, `right:15-60.5`.
        The pin placer reads digits only, so a number has no exponent."""

        def position(value: Optional[Length]) -> str:
            return "*" if value is None else f"{value.to('um'):.6f}".rstrip("0").rstrip(".")

        if self.begin is None and self.end is None:
            return f"{self.edge}:*"
        return f"{self.edge}:{position(self.begin)}-{position(self.end)}"


class PinGroup(XedaBaseModel):
    """Pins that the pin placer puts next to each other (`set_io_pin_constraint -group`)."""

    pins: list[PinName] = Field(
        min_length=1,
        description="The pins of the group, by name or by pattern (`-pin_names`). A pin belongs "
        "to one group.",
        json_schema_extra=tier("advanced"),
    )
    order: bool = Field(
        False,
        description="Place the pins in the order of the list, in ascending x or y (`-order`).",
        json_schema_extra=tier("advanced"),
    )
    region: Optional[EdgeInterval] = Field(
        None,
        description="Keep the group inside this part of an edge (`-region`). Unset: anywhere. "
        "With `algorithm: annealing`, OpenROAD v2.0-26132 stops with PPL-0107 for a group inside "
        "part of an edge, and it works with a whole edge (no `begin` and no `end`). The error "
        "was seen with a group of two pins in a die of 40 by 40 micrometers. No upstream issue "
        "exists.",
        json_schema_extra=tier("advanced"),
    )


class PinConstraint(XedaBaseModel):
    """A restriction of some pins to a part of an edge (`set_io_pin_constraint -region`)."""

    region: EdgeInterval = Field(
        description="The part of the edge that the pins keep to.",
        json_schema_extra=tier("advanced"),
    )
    pins: Optional[list[PinName]] = Field(
        None,
        min_length=1,
        description="The pins to restrict, by name or by pattern (`-pin_names`). Give this or "
        "`direction`.",
        json_schema_extra=tier("advanced"),
    )
    direction: Optional[Literal["input", "output", "inout", "feedthru"]] = Field(
        None,
        description="Restrict every pin of this direction (`-direction`). The documentation of "
        "OpenROAD spells the last value `feedthrough`, and the tool accepts only `feedthru`. "
        "Give this or `pins`.",
        json_schema_extra=tier("advanced"),
    )


class FixedPin(XedaBaseModel):
    """A pin that the flow places by hand (`place_pin`), before the pin placer runs."""

    pin: PinName = Field(description="The name of the pin.", json_schema_extra=tier("advanced"))
    layer: str = Field(
        min_length=1,
        description="The routing layer of the pin (`-layer`).",
        json_schema_extra=tier("advanced"),
    )
    x: Length = Field(
        description="The x of the center of the pin, a length with a unit (`-location`).",
        json_schema_extra=tier("advanced"),
    )
    y: Length = Field(
        description="The y of the center of the pin, a length with a unit (`-location`).",
        json_schema_extra=tier("advanced"),
    )
    width: Optional[PositiveLength] = Field(
        None,
        description="The width of the pin shape, a length with a unit (`-pin_size`). Give it with "
        "`height`. Unset: the tool makes the smallest shape that the layer allows.",
        json_schema_extra=tier("advanced"),
    )
    height: Optional[PositiveLength] = Field(
        None,
        description="The height of the pin shape, a length with a unit (`-pin_size`). Give it with "
        "`width`.",
        json_schema_extra=tier("advanced"),
    )
    snap_to_boundary: bool = Field(
        False,
        description="Move the pin to the nearest routing track next to the die boundary "
        "(`-force_to_die_boundary`).",
        json_schema_extra=tier("advanced"),
    )
    status: Literal["fixed", "placed"] = Field(
        "fixed",
        description="The placement status of the pin. `fixed` keeps the pin where it is. `placed` "
        "lets a later step move it (`-placed_status`).",
        json_schema_extra=tier("advanced"),
    )


class AnnealingGroup(SettingsGroup):
    """The schedule of the simulated annealing of the pin placer (`set_simulated_annealing`).
    The pin placer uses it with `io.algorithm: annealing`, which needs `io.flow: skip_io_pass`."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("temperature", "-temperature", "value"),
        Flag("max_iterations", "-max_iterations", "value"),
        Flag("perturb_per_iter", "-perturb_per_iter", "value"),
        Flag("alpha", "-alpha", "value"),
    )

    temperature: Optional[Positive] = Field(
        None,
        description="The start temperature (`-temperature`). Unset: the default of the tool, 1.0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    max_iterations: Optional[int] = Field(
        None,
        ge=1,
        description="The most iterations (`-max_iterations`). Unset: the default of the tool, "
        "2000.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    perturb_per_iter: Optional[int] = Field(
        None,
        ge=1,
        description="The moves that each iteration tries (`-perturb_per_iter`). Unset: the "
        "tool derives the number from the number of pins.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    alpha: Optional[float] = Field(
        None,
        gt=0,
        le=1,
        description="The factor that lowers the temperature after each iteration, above 0 and "
        "up to 1 (`-alpha`). Unset: the default of the tool, 0.985.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class PinLengthGroup(SettingsGroup):
    """The length of the pin shapes (`set_pin_length`)."""

    horizontal: Optional[PositiveLength] = Field(
        None,
        description="The length of the pins on the horizontal edges, a length with a unit "
        "(`-hor_length`). Unset: the shortest length that meets the minimum area of the layer.",
        json_schema_extra=tier("advanced"),
    )
    vertical: Optional[PositiveLength] = Field(
        None,
        description="The length of the pins on the vertical edges, a length with a unit "
        "(`-ver_length`). Unset: the shortest length that meets the minimum area of the layer.",
        json_schema_extra=tier("advanced"),
    )


class PinExtensionGroup(SettingsGroup):
    """How far the pin shapes stick out of the die (`set_pin_length_extension`). A pin may lie
    partly outside the die."""

    horizontal: Optional[Distance] = Field(
        None,
        description="The extension of the pins on the horizontal edges, a length with a unit "
        "(`-hor_extension`). Unset: the default of the tool, 0.",
        json_schema_extra=tier("expert"),
    )
    vertical: Optional[Distance] = Field(
        None,
        description="The extension of the pins on the vertical edges, a length with a unit "
        "(`-ver_extension`). Unset: the default of the tool, 0.",
        json_schema_extra=tier("expert"),
    )


class PinThicknessGroup(SettingsGroup):
    """The width of the pin shapes, as a multiple of the minimum width of the layer
    (`set_pin_thick_multiplier`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("horizontal", "-hor_multiplier", "value"),
        Flag("vertical", "-ver_multiplier", "value"),
    )

    horizontal: Optional[float] = Field(
        None,
        ge=1,
        allow_inf_nan=False,
        description="The multiplier for the pins on the horizontal edges (`-hor_multiplier`). "
        "Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    vertical: Optional[float] = Field(
        None,
        ge=1,
        allow_inf_nan=False,
        description="The multiplier for the pins on the vertical edges (`-ver_multiplier`). "
        "Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class IoGroup(SettingsGroup):
    """The placement of the pins of the design (`place_pins`, or `global_placement -place_ios`
    with `set_place_config`).

    The flow does not write the placement of the pins to a file (`-write_pin_placement`).
    `-random` and `-random_seed` of `place_pins` are obsolete in OpenROAD: the command warns
    (PPL-0113) and places nothing, so no setting prints them. A layer that is not a routing layer
    of the right direction, or has no tracks, is an error of the tool (PPL-0021, PPL-0023,
    PPL-0045 and PPL-0046), and the tool names the layer."""

    flow: Literal["skip_io_pass", "concurrent", "fixed"] = Field(
        "skip_io_pass",
        description="How the pins get their places. `skip_io_pass` places the cells without the "
        "pins first, then places the pins, then places the cells again. `concurrent` places the "
        "pins together with the cells (`global_placement -place_ios`), and the global placement "
        "legalizes them with the pin placer. `concurrent` gives the layers, `min_distance`, "
        "`min_distance_tracks` and `corner_avoidance` to `set_place_config` (the `-io_pin_` "
        "options) and needs OpenROAD 26Q4-11 or later, which has that command. It does not "
        "take `algorithm: annealing`. "
        "`fixed` keeps the pins that the floorplan already placed: it needs a DEF file with placed "
        "pins (`floorplan.sizing: def`) or `fixed_pins`, else the global placement stops with "
        "GPL-0326 on the unplaced pins. With `floorplan.sizing: def` the pins of the DEF file "
        "stay, except with `concurrent`. The default is the one of OpenROAD-flow-scripts.",
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
    min_distance: Optional[Distance] = Field(
        None,
        description="The smallest distance between two pins, a length with a unit "
        "(`-min_distance`). Not with `min_distance_tracks`. Unset: the default of the tool, 2 "
        "tracks.",
        json_schema_extra=tier("advanced"),
    )
    min_distance_tracks: Optional[Annotated[int, Field(ge=0)]] = Field(
        None,
        description="The smallest distance between two pins, in routing tracks "
        "(`-min_distance_in_tracks -min_distance`). OpenROAD takes a whole number of tracks. "
        "Not with `min_distance`. Unset: the default of the tool, 2 tracks.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    corner_avoidance: Optional[Distance] = Field(
        None,
        description="The distance that the pins keep from the corners of the die, a length with "
        "a unit (`-corner_avoidance`). Unset: the default of the tool, 0.",
        json_schema_extra=tier("advanced"),
    )
    algorithm: Literal["matching", "annealing"] = Field(
        "matching",
        description="How the pins get their slots. `matching` assigns them with the Hungarian "
        "method, which is the default of the tool. `annealing` uses simulated annealing "
        "(`-annealing`), set by `annealing`. Only `flow: skip_io_pass` takes `annealing`.",
        json_schema_extra=tier("advanced"),
    )
    annealing: AnnealingGroup = Field(
        default_factory=AnnealingGroup,
        description="The schedule of the simulated annealing. Needs `algorithm: annealing`, "
        "which needs `flow: skip_io_pass`.",
    )
    exclude: list[EdgeInterval] = Field(
        [],
        description="The parts of the edges where no pin may sit (`exclude_io_pin_region`, one "
        "`-region` for each item).",
        json_schema_extra=tier("advanced"),
    )
    groups: list[PinGroup] = Field(
        [],
        description="Pins that the pin placer puts next to each other. A group may be ordered "
        "and may keep to a part of an edge. A pin belongs to one group, else the tool stops "
        "(PPL-0104).",
        json_schema_extra=tier("advanced"),
    )
    constraints: list[PinConstraint] = Field(
        [],
        description="Pins, named or chosen by their direction, that the pin placer keeps to a "
        "part of an edge (`set_io_pin_constraint -region`).",
        json_schema_extra=tier("advanced"),
    )
    mirrored_pins: list[tuple[PinName, PinName]] = Field(
        [],
        description="Pairs of pins that the pin placer puts at mirrored places of opposite edges "
        "(`set_io_pin_constraint -mirrored_pins`). With `algorithm: matching`, OpenROAD "
        "v2.0-26132 can put another pin on the place that a mirrored pin takes, and the "
        "placement stops with PPL-0107 (seen on 3 of 4 trials on a netlist of 5 cells and 8 "
        "pins). `annealing` (only with `flow: skip_io_pass`) placed the pins in all of them. No "
        "upstream issue exists.",
        json_schema_extra=tier("advanced"),
    )
    fixed_pins: list[FixedPin] = Field(
        [],
        description="Pins that the flow places by hand (`place_pin`) before any placement. They "
        "stay where they are. This works with every `flow` and with a floorplan from a DEF file.",
        json_schema_extra=tier("advanced"),
    )
    pin_length: PinLengthGroup = Field(
        default_factory=PinLengthGroup,
        description="The length of the pin shapes.",
    )
    pin_extension: PinExtensionGroup = Field(
        default_factory=PinExtensionGroup,
        description="How far the pin shapes stick out of the die.",
    )
    pin_thickness: PinThicknessGroup = Field(
        default_factory=PinThicknessGroup,
        description="The width of the pin shapes.",
    )

    #: The leaves that act with `flow: fixed`, where no pin placer runs: the flow itself and the
    #: pins that the flow places by hand.
    WITH_FIXED_FLOW: ClassVar[frozenset[str]] = frozenset({"flow", "fixed_pins"})

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a setting for the placement of the pins when the pins stay where the floorplan
        put them, a unit without a distance, a fraction of a track, an empty list of layers, a
        schedule of annealing without annealing, and an interval, a constraint or a pin that
        makes no sense."""
        found: list[tuple[str, str]] = []
        if self.flow == "fixed":
            for leaf in differing_leaves(self, key):
                if leaf[len(key) + 1 :].split(".")[0] not in self.WITH_FIXED_FLOW:
                    found.append(
                        (
                            leaf,
                            f"`{leaf}` has no effect with `{key}.flow: fixed`, which keeps the "
                            f"pins that the floorplan placed: set `{key}.flow: skip_io_pass` or "
                            f"delete `{leaf}`",
                        )
                    )
        elif self.algorithm != "annealing":
            for leaf in differing_leaves(self.annealing, f"{key}.annealing"):
                found.append(
                    (
                        leaf,
                        f"`{leaf}` has no effect with `{key}.algorithm: {self.algorithm}`: set "
                        f"`{key}.algorithm: annealing` (with `{key}.flow: skip_io_pass` and a "
                        f"floorplan that is not a DEF file) or delete `{leaf}`",
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
        if self.min_distance is not None and self.min_distance_tracks is not None:
            found.append(
                (
                    f"{key}.min_distance_tracks",
                    f"`{key}.min_distance` and `{key}.min_distance_tracks` both give the "
                    f"smallest distance between two pins: delete one of them",
                )
            )
        intervals = [
            (f"{key}.exclude[{number}]", f"{key}.exclude", item)
            for number, item in enumerate(self.exclude)
        ]
        intervals += [
            (f"{key}.groups[{number}].region", f"{key}.groups", item.region)
            for number, item in enumerate(self.groups)
            if item.region is not None
        ]
        intervals += [
            (f"{key}.constraints[{number}].region", f"{key}.constraints", item.region)
            for number, item in enumerate(self.constraints)
        ]
        for where, leaf, interval in intervals:
            if (
                interval.begin is not None
                and interval.end is not None
                and not interval.begin < interval.end
            ):
                found.append(
                    (
                        leaf,
                        f"`{where}` begins at {interval.begin} and ends at {interval.end}: "
                        "the interval must begin before it ends",
                    )
                )
        for number, constraint in enumerate(self.constraints):
            if (constraint.pins is None) == (constraint.direction is None):
                found.append(
                    (
                        f"{key}.constraints",
                        f"`{key}.constraints[{number}]` must give `pins` or `direction`, and "
                        "not both: OpenROAD takes one of them in a call",
                    )
                )
        for number, pin in enumerate(self.fixed_pins):
            if (pin.width is None) != (pin.height is None):
                found.append(
                    (
                        f"{key}.fixed_pins",
                        f"`{key}.fixed_pins[{number}]` gives only one of `width` and `height`: "
                        "give both, or neither to take the shape of the tool",
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
        json_schema_extra=extras(tier("common"), ratio()),
    )
    headroom: Optional[float] = Field(
        None,
        ge=0,
        le=0.99,
        description="The share of the free space above the uniform density that the target "
        "adds, from 0 to 0.99. Needs `mode: auto`, and `mode: auto` needs it.",
        json_schema_extra=extras(tier("common"), ratio()),
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
    """The timing-driven mode of the global placement. The leaves other than `enabled` tune the
    mode, and each passes its flag only when you set it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("enabled", "-timing_driven", "switch"),
        Flag("reweight_overflow_percent", "-timing_driven_net_reweight_overflow", "list"),
        Flag("net_weight_max", "-timing_driven_net_weight_max", "value"),
        Flag("nets_percent", "-timing_driven_nets_percentage", "value"),
        Flag("keep_resize_below_overflow", "-keep_resize_below_overflow", "value"),
        Flag("repair_timing", "-timing_driven_repair_timing", "switch"),
        Flag("repair_tns_end_percent", "-timing_driven_repair_tns_end_percent", "value"),
        Flag("virtual_cts", "-virtual_cts", "switch"),
        Flag("virtual_cts_max_skew_fraction", "-virtual_cts_max_skew_fraction", "value"),
    )

    enabled: bool = Field(
        True,
        description="Weight the nets by their slack during the placement (`-timing_driven`). The "
        "mode needs clocks in the SDC and the wire RC. It costs run time. The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    reweight_overflow_percent: Optional[list[Annotated[int, Field(ge=1, le=100)]]] = Field(
        None,
        min_length=1,
        description="The overflows, in percent, at which the placement computes the net weights "
        "again (`-timing_driven_net_reweight_overflow`). Fewer values save run time. Unset: the "
        "default of the tool, 64 and 20.",
        json_schema_extra=extras(tier("expert"), percent()),
    )
    net_weight_max: Optional[Positive] = Field(
        None,
        description="The weight of the most critical nets, as a multiple "
        "(`-timing_driven_net_weight_max`). Unset: the default of the tool, 5.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    nets_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the nets that get a new weight, in percent "
        "(`-timing_driven_nets_percentage`). Unset: the default of the tool, 10.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    keep_resize_below_overflow: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="Below this overflow, from 0 to 1, the changes that the resizer makes inside "
        "the placement stay. Above it, the placement reverts them (`-keep_resize_below_overflow`). "
        "Unset: the default of the tool, 1, which keeps them all. OpenROAD-flow-scripts passes "
        "the value only when you set a variable for it.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    repair_timing: bool = Field(
        False,
        description="Repair the setup of the worst paths in the last timing-driven iteration, "
        "with a few moves that keep the topology (`-timing_driven_repair_timing`). The "
        "documentation of OpenROAD calls it experimental and not ready for production use.",
        json_schema_extra=tier("expert"),
    )
    repair_tns_end_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the violating endpoints that this repair targets, in percent "
        "(`-timing_driven_repair_tns_end_percent`). Needs `repair_timing`. Unset: the default of "
        "the tool, 1.",
        json_schema_extra=extras(tier("expert"), percent()),
    )
    virtual_cts: bool = Field(
        False,
        description="Build a light clock tree before each timing-driven iteration, to model the "
        "clock latency of each sink (`-virtual_cts`).",
        json_schema_extra=tier("expert"),
    )
    virtual_cts_max_skew_fraction: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The largest insertion delay of the virtual clock tree, as a fraction of "
        "the clock period (`-virtual_cts_max_skew_fraction`). Needs `virtual_cts`. Unset: the "
        "default of the tool, 0.1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a leaf of an experiment that is off."""
        return [
            (
                f"{key}.{leaf}",
                f"`{key}.{leaf}` has no effect without `{key}.{needs}: true`: set "
                f"`{key}.{needs}: true` or delete `{key}.{leaf}`",
            )
            for leaf, needs in (
                ("repair_tns_end_percent", "repair_timing"),
                ("virtual_cts_max_skew_fraction", "virtual_cts"),
            )
            if getattr(self, leaf) is not None and not getattr(self, needs)
        ]


class RoutabilityDrivenGroup(SettingsGroup):
    """The routability-driven mode of the global placement. The leaves other than `enabled` tune
    the mode, and each passes its flag only when you set it."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("enabled", "-routability_driven", "switch"),
        Flag("target_rc_metric", "-routability_target_rc_metric", "value"),
        Flag("check_overflow", "-routability_check_overflow", "value"),
        Flag("snapshot_overflow", "-routability_snapshot_overflow", "value"),
        Flag("max_density", "-routability_max_density", "value"),
        Flag("inflation_ratio_coef", "-routability_inflation_ratio_coef", "value"),
        Flag("max_inflation_ratio", "-routability_max_inflation_ratio", "value"),
        Flag("min_congestion_for_inflation", "-routability_min_congestion_for_inflation", "value"),
        Flag("max_inflation_total", "-routability_max_inflation_total", "value"),
        Flag("net_weight_max", "-routability_net_weight_max", "value"),
        Flag("congested_nets_percent", "-routability_congested_nets_percentage", "value"),
        Flag("rc_coefficients", "-routability_rc_coefficients", "list"),
    )

    enabled: bool = Field(
        True,
        description="Inflate the cells in congested regions during the placement "
        "(`-routability_driven`). The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    estimator: Literal["rudy", "grt"] = Field(
        "rudy",
        description="How the mode estimates the congestion. `rudy` is a fast estimate from the "
        "nets. `grt` runs the global router, which is more exact and costs much more run time "
        "(`-routability_use_grt`). The default is the one of the tool.",
        json_schema_extra=tier("expert"),
    )
    target_rc_metric: Optional[Positive] = Field(
        None,
        description="The congestion that the mode tries to reach (`-routability_target_rc_metric`). "
        "Raise it if the mode runs too long. Unset: the default of the tool, 1.01.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    check_overflow: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The overflow, from 0 to 1, at which the mode starts to check the congestion "
        "(`-routability_check_overflow`). Unset: the default of the tool, 0.3.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    snapshot_overflow: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The overflow, from 0 to 1, at which the mode saves a snapshot to revert to "
        "(`-routability_snapshot_overflow`). Unset: the default of the tool, 0.6.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    max_density: Optional[float] = Field(
        None,
        gt=0,
        le=1,
        description="The cap on the density in this mode, from 0 to 1 "
        "(`-routability_max_density`). Unset: the default of the tool, 0.99.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    inflation_ratio_coef: Optional[Positive] = Field(
        None,
        description="The strength of the inflation (`-routability_inflation_ratio_coef`). Unset: "
        "the default of the tool, 2.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    max_inflation_ratio: Optional[Positive] = Field(
        None,
        description="The largest inflation of one cell, as a ratio "
        "(`-routability_max_inflation_ratio`). Unset: the default of the tool, 3.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    min_congestion_for_inflation: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="A tile with less congestion than this, from 0 to 1, does not inflate "
        "(`-routability_min_congestion_for_inflation`). Unset: the default of the tool, 0.95.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    max_inflation_total: Optional[Positive] = Field(
        None,
        description="The most area that the inflation may add, as a fraction of the movable area "
        "(`-routability_max_inflation_total`). Unset: the default of the tool, 0.3.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    net_weight_max: Optional[Positive] = Field(
        None,
        description="The extra wirelength weight of the nets in congested tiles "
        "(`-routability_net_weight_max`). 1 adds none. Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    congested_nets_percent: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="The share of the nets that get that weight, in percent "
        "(`-routability_congested_nets_percentage`). Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("expert"), percent()),
    )
    rc_coefficients: Optional[list[NonNegative]] = Field(
        None,
        min_length=4,
        max_length=4,
        description="The weights of the 0.5%, 1%, 2% and 5% most congested tiles in the "
        "average congestion, four numbers (`-routability_rc_coefficients`). Unset: the default "
        "of the tool, 1, 1, 0 and 0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class InitialPlaceGroup(SettingsGroup):
    """The initial placement of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("from_center", "-force_center_initial_place", "switch"),
        Flag("max_iter", "-initial_place_max_iter", "value"),
        Flag("max_fanout", "-initial_place_max_fanout", "value"),
    )

    from_center: bool = Field(
        True,
        description="Start every cell at the center of the core, also when the database has "
        "locations (`-force_center_initial_place`). OpenROAD-flow-scripts gives the flag to the "
        "main pass of the placement.",
        json_schema_extra=tier("expert"),
    )
    max_iter: Optional[int] = Field(
        None,
        ge=0,
        description="The most iterations of the initial placement (`-initial_place_max_iter`). 0 "
        "skips the initial placement, which `-skip_initial_place` also does. Unset: the default "
        "of the tool, 20.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    max_fanout: Optional[int] = Field(
        None,
        ge=1,
        description="The initial placement ignores the nets with at least this many pins "
        "(`-initial_place_max_fanout`). Unset: the default of the tool, 200.",
        json_schema_extra=extras(tier("expert"), count()),
    )


class NesterovGroup(SettingsGroup):
    """The solver of the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("skip", "-skip_nesterov_place", "switch"),
        Flag("min_phi_coef", "-min_phi_coef", "value"),
        Flag("max_phi_coef", "-max_phi_coef", "value"),
        Flag("init_density_penalty", "-init_density_penalty", "value"),
        Flag("init_wirelength_coef", "-init_wirelength_coef", "value"),
        Flag("reference_hpwl", "-reference_hpwl", "value"),
        Flag("overflow", "-overflow", "value"),
        Flag("revert_on_divergence", "-disable_revert_if_diverge", "skip"),
        Flag("pin_density_adjust", "-disable_pin_density_adjust", "skip"),
    )

    skip: bool = Field(
        False,
        description="Stop after the initial placement and run no solver (`-skip_nesterov_place`). "
        "The tool refuses it with `io.flow: concurrent` (GPL-0182).",
        json_schema_extra=tier("expert"),
    )
    min_phi_coef: Optional[float] = Field(
        None,
        gt=0,
        description="The smallest factor of the step size of the solver (`-min_phi_coef`). "
        "Useful when the placement diverges. Unset: the default of the tool, 0.95.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    max_phi_coef: Optional[float] = Field(
        None,
        gt=0,
        description="The largest factor of the step size of the solver (`-max_phi_coef`). "
        "Unset: the default of the tool, 1.05.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    init_density_penalty: Optional[float] = Field(
        None,
        ge=1e-6,
        le=1e6,
        description="The first weight of the density penalty (`-init_density_penalty`). Tune it "
        "when you start from placed cells. Unset: the default of the tool, 8e-5.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    init_wirelength_coef: Optional[Positive] = Field(
        None,
        description="The first coefficient of the wirelength term (`-init_wirelength_coef`). "
        "Unset: the default of the tool, 0.25.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    reference_hpwl: Optional[NonNegative] = Field(
        None,
        description="The wirelength change that the controller of the density penalty measures "
        "against, in the units of the library: database units, not micrometers "
        "(`-reference_hpwl`). 0 takes it from the wirelength of the design. Unset: the default "
        "of the tool, 0. The README of OpenROAD names 466e6, which is not the value in the "
        "source.",
        json_schema_extra=extras(tier("expert"), library_units()),
    )
    overflow: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The overflow, from 0 to 1, at which the solver stops (`-overflow`). A larger "
        "value runs faster and spreads the cells less. Unset: the default of the tool, 0.1.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    revert_on_divergence: bool = Field(
        True,
        description="Save the state along the iterations, and go back to it if the solver "
        "diverges. False passes `-disable_revert_if_diverge`. The default is the one of the tool.",
        json_schema_extra=tier("expert"),
    )
    pin_density_adjust: bool = Field(
        True,
        description="Adjust the area of each cell by the density of its pins. False passes "
        "`-disable_pin_density_adjust`. The default is the one of the tool.",
        json_schema_extra=tier("expert"),
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


class FlopClusteringGroup(SettingsGroup):
    """The merge of flip-flops into multibit cells after the global placement (`cluster_flops`).
    The library needs multibit flip-flops. Without them the tool warns that it found no flop to
    cluster (GPL-0138) and changes nothing."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("tray_weight", "-tray_weight", "value"),
        Flag("timing_weight", "-timing_weight", "value"),
        Flag("max_split_size", "-max_split_size", "value"),
        Flag("num_paths", "-num_paths", "value"),
        Flag("clock_power_weight", "-clock_power_weight", "value"),
    )

    enabled: bool = Field(
        False,
        description="Cluster the flip-flops (`cluster_flops`). The command runs after the global "
        "placement, and the flow estimates the parasitics again after it. A library with "
        "multibit cells often needs their sites in `floorplan.additional_sites`. "
        "OpenROAD-flow-scripts runs it only when a variable asks for it.",
        json_schema_extra=tier("advanced"),
    )
    tray_weight: Optional[NonNegative] = Field(
        None,
        description="The weight of the tray (`-tray_weight`). Unset: the default of the tool, 32.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    timing_weight: Optional[NonNegative] = Field(
        None,
        description="The weight of the timing (`-timing_weight`). Unset: the default of the "
        "tool, 0.1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    max_split_size: Optional[int] = Field(
        None,
        ge=-1,
        description="The largest set of flops that the clustering handles at once "
        "(`-max_split_size`). -1 splits nothing. Unset: the default of the tool, 500.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    num_paths: Optional[int] = Field(
        None,
        ge=0,
        description="The number of timing paths that the clustering considers (`-num_paths`). "
        "Unset: the default of the tool, 0.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    clock_power_weight: Optional[NonNegative] = Field(
        None,
        description="The credit for the clock tree power that banking saves (`-clock_power_weight`). "
        "A larger value gives a higher banking ratio. Unset: the default of the tool, 0, which "
        "gives no credit.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class GlobalPlacementGroup(SettingsGroup):
    """The global placement of the cells (`global_placement`).

    The placement of the cells without the pins takes the density, the padding, the seed and the
    perturbation. The other leaves act on the main placement, as in OpenROAD-flow-scripts.

    Three flags are no leaves. `-skip_io` and `-place_ios` belong to the flow (`io.flow`).
    `-skip_initial_place` is `initial_place.max_iter: 0`."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("random_seed", "-random_seed", "value"),
        Flag("bin_grid_count", "-bin_grid_count", "value"),
        Flag("incremental", "-incremental", "switch"),
        Flag("congestion_heatmap", "-enable_routing_congestion", "switch"),
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
        json_schema_extra=extras(tier("advanced"), count()),
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
    bin_grid_count: Optional[int] = Field(
        None,
        ge=2,
        description="The number of bins of the density grid, in each direction "
        "(`-bin_grid_count`). It must be a power of two, such as 64 or 128, because the solver "
        "uses a Fourier transform. A grid much finer than the tool would choose can make the "
        "solver diverge (GPL-0305): 64 bins in each direction does so for a design of 400 cells "
        "on nangate45. The log of the placement shows the ideal count (GPL-0026). Unset: the "
        "tool chooses the number from the design.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    incremental: bool = Field(
        False,
        description="Place only the cells that have no location yet, and keep the others where "
        "they are (`-incremental`). OpenROAD-flow-scripts uses it after the swap of arithmetic "
        "modules. The tool refuses it with `io.flow: concurrent` (GPL-0170).",
        json_schema_extra=tier("expert"),
    )
    congestion_heatmap: bool = Field(
        False,
        description="Run the global router after the placement for the congestion heatmap of "
        "the graphical interface (`-enable_routing_congestion`). It costs run time.",
        json_schema_extra=tier("expert"),
    )
    random_seed: Optional[int] = Field(
        None,
        description="The seed that perturbs the cells at the start (`-random_seed`). Different "
        "seeds give different placements. Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    perturb_distance: Optional[Distance] = Field(
        None,
        description="The radius of the perturbation at the start, a length with a unit; 99.5% "
        "of the offsets fall inside it (`-perturb_dist`, which takes nanometers: the flow "
        "converts). 0 um switches the perturbation off. Unset: the smaller of 0.5 micrometers "
        "and one row height.",
        json_schema_extra=tier("expert"),
    )
    flop_clustering: FlopClusteringGroup = Field(
        default_factory=FlopClusteringGroup,
        description="The clustering of flip-flops into multibit cells.",
    )
    clusters: list[Annotated[list[PinName], Field(min_length=1)]] = Field(
        [],
        description="Sets of instances that the placement keeps together as one cluster "
        "(`placement_cluster`), each a list of instance names or patterns. The documentation of "
        "OpenROAD advises it for small groups of gates. The cluster stands for the instances "
        "that exist when the main placement starts, so name no buffer that the flow removes.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a bin grid that is no power of two."""
        count = self.bin_grid_count
        if count is not None and count & (count - 1):
            return [
                (
                    f"{key}.bin_grid_count",
                    f"`{key}.bin_grid_count` is {count}, and it must be a power of two (the "
                    "solver uses a Fourier transform): use 64, 128, 256 and so on",
                )
            ]
        return []


#: How the global sizing starts (`set_global_sizing_config -presize_mode`).
PresizeMode = Literal["disabled", "min_size_max_vt", "max_size_min_vt"]


class BufferPortsGroup(SettingsGroup):
    """The buffers between the ports of the design and its logic (`buffer_ports`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("cell", "-buffer_cell", "value"),)

    enabled: bool = Field(
        True,
        description="Put a buffer between each port and the cells that it drives or that "
        "drive it (`buffer_ports`). OpenROAD-flow-scripts runs it.",
        json_schema_extra=tier("common"),
    )
    inputs: bool = Field(
        True,
        description="Buffer the input ports. The command gets neither `-inputs` nor `-outputs` "
        "when both are true, and it buffers both then. The flow gives `-inputs` when only this "
        "one is true.",
        json_schema_extra=tier("common"),
    )
    outputs: bool = Field(
        True,
        description="Buffer the output ports. The flow gives `-outputs` when only this one is "
        "true.",
        json_schema_extra=tier("common"),
    )
    cell: Optional[str] = Field(
        None,
        min_length=1,
        description="The buffer that the command puts at the ports (`-buffer_cell`). The cell "
        "must be a buffer, or the tool stops with RSZ-0211. Unset: the tool chooses the buffer.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a group that is on and buffers neither the inputs nor the outputs."""
        if self.inputs or self.outputs:
            return []
        return [
            (
                f"{key}.outputs",
                f"`{key}` buffers neither the inputs nor the outputs: set "
                f"`{key}.enabled: false` to buffer no port, or set `{key}.inputs` or "
                f"`{key}.outputs` to true",
            )
        ]


class OptConfigGroup(SettingsGroup):
    """The options of the resizer that limit the sizing (`set_opt_config`).

    The database stores them, so `openroad_cts` and `openroad_grt` size under the same limits. This
    flow is the only one that sets them. The flow sets them at the start, before the first
    command that sizes.

    The flow prints the booleans itself, since the command takes `true` or `false` as their
    value, and `render_flags` prints no such value."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("limit_sizing_area", "-limit_sizing_area", "value"),
        Flag("limit_sizing_leakage", "-limit_sizing_leakage", "value"),
        Flag("early_sizing_cap_ratio", "-set_early_sizing_cap_ratio", "value"),
        Flag("early_buffer_sizing_cap_ratio", "-set_early_buffer_sizing_cap_ratio", "value"),
    )

    limit_sizing_area: Optional[Positive] = Field(
        None,
        description="Leave a cell out of the sizing when its area is above this multiple of the "
        "area of the cell it would replace (`-limit_sizing_area`). The area comes from the LEF "
        "files. Unset: no limit.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    limit_sizing_leakage: Optional[Positive] = Field(
        None,
        description="Leave a cell out of the sizing when its leakage power is above this "
        "multiple of the leakage of the cell it would replace (`-limit_sizing_leakage`). The "
        "leakage is that of the corner of the flow. Unset: no limit.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    keep_sizing_site: bool = Field(
        False,
        description="Keep the site type of a cell when the resizer sizes it, so that a short "
        "cell never becomes a tall cell and the other way round (`-keep_sizing_site`). It "
        "matters for designs with rows of different heights.",
        json_schema_extra=tier("advanced"),
    )
    keep_sizing_vt: bool = Field(
        False,
        description="Keep the threshold voltage class of a cell when the resizer sizes it "
        "(`-keep_sizing_vt`). The tool tells the classes apart only where the LEF files define "
        "the layers of the classes in their obstructions.",
        json_schema_extra=tier("advanced"),
    )
    early_sizing_cap_ratio: Optional[Positive] = Field(
        None,
        description="The ratio of the input pin capacitance to the output load that the first "
        "sizing of the gates keeps (`-set_early_sizing_cap_ratio`). OpenROAD-flow-scripts sets it "
        "from `EARLY_SIZING_CAP_RATIO`. Unset: the default of the tool.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    early_buffer_sizing_cap_ratio: Optional[Positive] = Field(
        None,
        description="The same ratio for the first sizing of the buffers "
        "(`-set_early_buffer_sizing_cap_ratio`). Unset: the default of the tool.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    disable_buffer_pruning: bool = Field(
        False,
        description="Keep all buffers as candidates, delay cells and slow buffers too, which "
        "helps the hold repair (`-disable_buffer_pruning`). Then every setup repair that may "
        "use the `vt_swap` move stops OpenROAD with a segmentation fault, also with a library "
        "that has no such cells. It was seen with the image `openroad/orfs:26Q4-132-g6b305a712` "
        "and with a local build. That includes the cleanup of the netlist at the floorplan. To "
        "avoid it, set "
        "`floorplan.netlist_cleanup.mode: none`, and `moves.vt_swap: false` in each other "
        "`repair_timing` call that repairs setup. The hold repair and `repair_design` are not "
        "affected.",
        json_schema_extra=tier("expert"),
    )


class GlobalSizingGroup(SettingsGroup):
    """The options of the global sizing (`set_global_sizing_config`), a repair that sizes all
    gates together with Lagrangian relaxation.

    They act on a `repair_timing` with the phase `GLOBAL_SIZING`, in this flow and in the flows
    after it, since the database stores them. This flow is the only one that sets them. The tool
    advises to leave all but the first two at their defaults.

    The flow prints `include_clock_network` and `setup_slack_margin` itself. The first takes
    `true` or `false` as its value. The second is a time that the command takes in seconds."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("presize_mode", "-presize_mode", "value"),
        Flag("max_iterations", "-max_iterations", "value"),
        Flag("beta", "-beta", "value"),
        Flag("mu_exponent", "-mu_exponent", "value"),
        Flag("lambda_floor", "-lambda_floor", "value"),
        Flag("timing_bias", "-timing_bias", "value"),
        Flag("budget_safety_factor", "-budget_safety_factor", "value"),
    )

    presize_mode: Optional[PresizeMode] = Field(
        None,
        description="How the global sizing starts (`-presize_mode`): `disabled` keeps the cells, "
        "`min_size_max_vt` replaces every cell with its equivalent of the smallest leakage, and "
        "`max_size_min_vt` with its equivalent of the largest leakage. Unset: `disabled`.",
        json_schema_extra=tier("expert"),
    )
    include_clock_network: bool = Field(
        False,
        description="Let the global sizing size the instances of the clock network "
        "(`-include_clock_network`). Off, it leaves them alone.",
        json_schema_extra=tier("expert"),
    )
    setup_slack_margin: Optional[Time] = Field(
        None,
        description="The worst setup slack at which the global sizing stops, a time with a "
        "unit, such as `50 ps` (`-setup_slack_margin`). The command takes seconds, and not the "
        "time unit of the Liberty files as the margins of `repair_timing` do, so the flow "
        "converts the value. Unset: 0.",
        json_schema_extra=tier("expert"),
    )
    max_iterations: Optional[int] = Field(
        None,
        ge=1,
        description="The most outer iterations of the Lagrangian relaxation "
        "(`-max_iterations`). Unset: the default of the tool, 20.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    beta: Optional[Positive] = Field(
        None,
        description="The step size of the update of the multipliers (`-beta`). Unset: the "
        "default of the tool, 0.6.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    mu_exponent: Optional[Positive] = Field(
        None,
        description="The exponent that seeds the multipliers of the endpoints "
        "(`-mu_exponent`). Unset: the default of the tool, 2.0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    lambda_floor: Optional[Positive] = Field(
        None,
        description="The lowest value of a multiplier, so that an arc that no path uses can "
        "come back (`-lambda_floor`). Unset: the default of the tool, 1e-12.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    timing_bias: Optional[Positive] = Field(
        None,
        description="The balance between the pressure of the timing and the cost of the "
        "leakage, without a unit (`-timing_bias`). Unset: the default of the tool, 64.0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    budget_safety_factor: Optional[float] = Field(
        None,
        gt=0,
        le=1,
        allow_inf_nan=False,
        description="A factor of at most 1 on the budget that each gate gets for sizing down "
        "(`-budget_safety_factor`). Unset: the default of the tool, 1.0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class ResizeGroup(SettingsGroup):
    """The optimization of the design after the global placement."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("max_utilization_percent", "-max_utilization", "value"),
    )

    remove_buffers: bool = Field(
        True,
        description="Remove the buffers of the synthesis before the global placement "
        "(`remove_buffers`), so that the repair chooses the buffering. Needs "
        "`global_placement.timing_driven.enabled`: OpenROAD-flow-scripts removes the buffers "
        "only with the timing-driven placement, and so does this flow.",
        json_schema_extra=tier("advanced"),
    )
    remove_buffers_instances: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="Remove only the buffers that these instances name (`remove_buffers`), each "
        "a name or a glob pattern. The command then removes them even when they are fixed or "
        "marked not to touch, and it never removes a buffer between an input port and an output "
        "port. A pattern that matches no instance stops the flow, since the command would "
        "remove every buffer. Empty: remove every buffer that is not fixed and not marked.",
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
    max_utilization_percent: Optional[float] = Field(
        None,
        gt=0,
        le=100,
        allow_inf_nan=False,
        description="Stop the resizer when the cells fill this share of the core area, in "
        "percent (`-max_utilization`). It acts on `buffer_ports`, `repair_design` and "
        "`repair_timing` of this stage. Unset: no limit.",
        json_schema_extra=extras(tier("advanced"), percent()),
    )
    opt_config: OptConfigGroup = Field(
        default_factory=OptConfigGroup,
        description="The limits of the sizing. The database keeps them for the later flows.",
    )
    global_sizing: GlobalSizingGroup = Field(
        default_factory=GlobalSizingGroup,
        description="The options of the global sizing. The database keeps them for the later "
        "flows.",
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse buffers to remove while the stage removes none."""
        if self.remove_buffers or not self.remove_buffers_instances:
            return []
        return [
            (
                f"{key}.remove_buffers_instances",
                f"`{key}.remove_buffers_instances` has no effect with `{key}.remove_buffers: "
                f"false`: set `{key}.remove_buffers: true` or delete "
                f"`{key}.remove_buffers_instances`",
            )
        ]


#: The goals of `replace_arith_modules -target`.
SwapTarget = Literal["setup", "hold", "power", "area"]


class ArithSwapGroup(SettingsGroup):
    """The swap of the arithmetic modules for faster or smaller variants (`replace_arith_modules`).

    The flow runs the command at the floorplan and again before the repairs after the global
    placement, with the same options. The flow prints `slack_threshold` itself: it is a time, and
    the command takes the time unit of the Liberty files."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("path_count", "-path_count", "value"),
        Flag("target", "-target", "value"),
    )

    enabled: bool = Field(
        False,
        description="Replace the wrapped arithmetic modules of the netlist (`replace_arith_modules`). "
        "It needs `hierarchical` and a netlist with wrapped operators.",
        json_schema_extra=tier("advanced"),
    )
    path_count: Optional[int] = Field(
        None,
        ge=1,
        description="The number of critical paths that the command analyzes to find the modules "
        "to swap (`-path_count`). Unset: the default of the tool, 1000.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    target: Optional[SwapTarget] = Field(
        None,
        description="The goal of the swap (`-target`). `setup` swaps for the fastest variants. "
        "The other goals are in the command, but the tool does not support them yet: it stops "
        "with RSZ-0155 as soon as it finds a module to swap, and the documentation of the tool "
        "lists them as not available yet. Unset: `setup`.",
        json_schema_extra=tier("advanced"),
    )
    slack_threshold: Optional[Time] = Field(
        None,
        description="The paths with a slack up to this value count as critical, a time with a "
        "unit, such as `50 ps` (`-slack_threshold`). The flow converts it to the time unit of "
        "the Liberty files. A positive value adds paths with a little positive slack. Unset: "
        "the default of the tool, 0.",
        json_schema_extra=tier("advanced"),
    )


class GlobalSwapGroup(SettingsGroup):
    """The options of the global swaps of the optimization (`improve_placement -global_swap_args`).

    The command takes them as one list of pairs, which the template builds. Each one passes only
    when you set it. The documentation of OpenROAD does not describe them. The
    defaults are those in the source of the tool."""

    passes: Optional[int] = Field(
        None,
        ge=1,
        description="The passes of the global swaps (`-passes`). Unset: the default of the "
        "tool, 2.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    tolerance: Optional[NonNegative] = Field(
        None,
        description="The improvement of the wirelength below which a pass ends (`-tolerance`). "
        "Unset: the default of the tool, 0.01.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    tradeoff: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        allow_inf_nan=False,
        description="The share of the random moves among the moves of a pass, from 0 to 1 "
        "(`-tradeoff`). Unset: the default of the tool, 0.4.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    area_weight: Optional[NonNegative] = Field(
        None,
        description="The weight of the cell area in the map of the utilization that steers the "
        "swaps (`-area_weight`). Unset: the default of the tool, 0.4.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    pin_weight: Optional[NonNegative] = Field(
        None,
        description="The weight of the pin count in that map (`-pin_weight`). Unset: the "
        "default of the tool, 0.6.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    congestion_weight: Optional[NonNegative] = Field(
        None,
        description="The weight of the congestion that you give (`-congestion_user_weight`). "
        "Unset: the default of the tool, 35.0.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    sampling_moves: Optional[int] = Field(
        None,
        ge=1,
        description="The number of moves that the swaps sample (`-sampling_moves`). Unset: the "
        "default of the tool, 150.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    normalization_interval: Optional[int] = Field(
        None,
        ge=1,
        description="The number of moves between two normalizations of the costs "
        "(`-normalization_interval`). Unset: the default of the tool, 1000.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    profiling_excess: Optional[Positive] = Field(
        None,
        description="The factor on the best wirelength of the profiling pass that sets the "
        "budget of the swaps (`-profiling_excess`). Unset: the default of the tool, 1.1.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    budget_multipliers: Optional[list[Positive]] = Field(
        None,
        min_length=1,
        description="The factors on the budget of the wirelength, one for each stage of the "
        "swaps (`-budget_multipliers`). Unset: the default of the tool, 1.5, 1.25, 1.1 and "
        "1.04.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class ImproveGroup(SettingsGroup):
    """The optimization of the detailed placement (`improve_placement`). The flow prints
    `extra_legalization` itself: the command reads the environment when it gets no value, so the
    flow always gives one."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("max_displacement", "-max_displacement", "list"),
        Flag("random_seed", "-random_seed", "value"),
    )

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
        description="The farthest that a cell moves in x and in y, as two integers in multiples "
        "of the row height (`-max_displacement`). 0 keeps the limit of the tool for that "
        "direction. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    random_seed: Optional[int] = Field(
        None,
        ge=0,
        le=2**31 - 1,
        description="The seed of the optimization (`-random_seed`). Unset: the default of the "
        "tool, 1.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    extra_legalization: bool = Field(
        False,
        description="Legalize once more inside the optimization (`-enable_extra_dpl`). The flow "
        "always gives the value, because the tool reads the environment variable "
        "`ENABLE_EXTRA_DPL` when it gets none.",
        json_schema_extra=tier("expert"),
    )
    global_swap: GlobalSwapGroup = Field(
        default_factory=GlobalSwapGroup,
        description="The options of the global swaps.",
    )


class NegotiationGroup(SettingsGroup):
    """The options of the negotiation legalizer, which `detailed_placement` uses unless
    `legalizer` is `diamond`."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("site_search_window", "-site_search_window", "value"),
        Flag("row_search_window", "-row_search_window", "value"),
        Flag("window_extension", "-disable_window_extension", "skip"),
        Flag("drc_penalty", "-drc_penalty", "value"),
    )

    site_search_window: Optional[int] = Field(
        None,
        ge=0,
        description="The base number of sites that a cell may move to the left or to the right "
        "(`-site_search_window`), capped by `max_displacement`. 0 allows no horizontal move. "
        "Unset: the default of the tool, 20.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    row_search_window: Optional[int] = Field(
        None,
        ge=0,
        description="The base number of rows that a cell may move up or down "
        "(`-row_search_window`), capped by `max_displacement`. 0 allows no change of row. "
        "Unset: the default of the tool, 5.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    window_extension: bool = Field(
        True,
        description="Let the search window grow past its base size, to the size of the cell and "
        "when a macro or the boundary of the core cuts it short. False fixes the window "
        "(`-disable_window_extension`).",
        json_schema_extra=tier("expert"),
    )
    drc_penalty: Optional[NonNegative] = Field(
        None,
        description="The priority of the cleanup of DRC violations, which grows with each "
        "iteration (`-drc_penalty`). A low value tolerates violations early, while overlaps go. "
        "0 switches off the growing penalty. Unset: the default of the tool, 5.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )


class PaddingOverride(XedaBaseModel):
    """Padding for some masters or some instances (`set_placement_padding -masters`,
    `-instances`). It wins over the global padding, and the padding of an instance wins over that
    of its master."""

    masters: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The masters, each a name or a glob pattern (`-masters`). Give this or "
        "`instances`.",
        json_schema_extra=tier("expert"),
    )
    instances: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The instances, each a name or a glob pattern (`-instances`). Give this or "
        "`masters`.",
        json_schema_extra=tier("expert"),
    )
    left: int = Field(
        0,
        ge=0,
        description="The empty sites on the left of each cell (`-left`).",
        json_schema_extra=extras(tier("expert"), count()),
    )
    right: int = Field(
        0,
        ge=0,
        description="The empty sites on the right of each cell (`-right`).",
        json_schema_extra=extras(tier("expert"), count()),
    )


class DetailedPlacementGroup(Legalize):
    """The legalization and the optimization of the placement.

    Every flag of `detailed_placement` that the flow can use has a setting. The others are
    internal (`-incremental`, `-quiet`), deprecated (`-disallow_one_site_gaps`) or write a file
    (`-report_file_name`). The padding, the farthest move and the legalizer are those of
    `Legalize`, which the flows after this one hold too."""

    cell_padding: int = Field(
        0,
        ge=0,
        description="The empty sites that each cell keeps on its left and on its right during "
        "the legalization (`set_placement_padding -global -left N -right N`), in sites. The "
        "default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    cell_padding_overrides: list[PaddingOverride] = Field(
        [],
        description="Other padding for some masters or some instances. A master wins over the "
        "global padding, and an instance wins over its master. Each item names `masters` or "
        "`instances`, with `left` and `right`.",
        json_schema_extra=tier("expert"),
    )
    negotiation: NegotiationGroup = Field(
        default_factory=NegotiationGroup,
        description="The options of the negotiation legalizer. The diamond legalizer has none.",
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
        "fail",
        description="What the flow does with an illegal placement (`check_placement`, which raises "
        "an error when a check fails). `fail` stops the flow. The default follows "
        "OpenROAD-flow-scripts, which does not catch the error and so stops on an illegal "
        "placement. `report` prints the violations and goes on.",
        json_schema_extra=tier("common"),
    )
    balance_rows: bool = Field(
        False,
        description="Balance the use of the rows of different types before the legalization "
        "(`balance_row_usage`). It is for designs with rows of different heights. "
        "OpenROAD-flow-scripts runs it only when `BALANCE_ROWS` says so.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the options of the negotiation legalizer with the diamond legalizer, and a
        padding that names both or neither of masters and instances."""
        found: list[tuple[str, str]] = []
        if self.legalizer == "diamond":
            for leaf in differing_leaves(self.negotiation, f"{key}.negotiation"):
                found.append(
                    (
                        leaf,
                        f"`{leaf}` has no effect with `{key}.legalizer: diamond`: set "
                        f"`{key}.legalizer: negotiation` or delete `{leaf}`",
                    )
                )
        for number, override in enumerate(self.cell_padding_overrides):
            if bool(override.masters) == bool(override.instances):
                found.append(
                    (
                        f"{key}.cell_padding_overrides",
                        f"`{key}.cell_padding_overrides[{number}]` must name `masters` or "
                        "`instances`, and not both: the command takes one of them in a call",
                    )
                )
        return found
