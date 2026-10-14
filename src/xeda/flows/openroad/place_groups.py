"""The settings groups of the placement flow, one for each step of the flow.

The flow loads the netlist, floorplans, places the tap cells and the power grid, places the pins,
places the cells globally, optimizes them, and legalizes them. A leaf whose description says
"Unset: the value of the platform" is `None` in the model, and the flow resolves it from the
platform. A leaf that is `None` otherwise takes the default of the tool.

A group that holds a flag of a command prints it through `FLAGS`. The flow prints the leaves that
choose between flags or that need the platform.
"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, tier
from ...flow.settings_group import Flag, SettingsGroup
from ...platforms.model import Pdn
from .groups import RepairDesign, RepairTiming, SetupMove, differing_leaves

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


class FloorplanGroup(SettingsGroup):
    """The size and the shape of the floorplan."""

    sizing: Literal["utilization", "area", "def"] = Field(
        "utilization",
        description="How the floorplan gets its size. `utilization` computes the core from the "
        "area of the netlist, with `core_utilization`, `aspect_ratio` and `core_margin`. `area` "
        "takes the die and the core from `die_area` and `core_area`. `def` takes the die, the "
        "core, the rows and the tracks from the file `def_file`.",
        json_schema_extra=tier("common"),
    )
    core_utilization: float = Field(
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
    core_margin: float = Field(
        1.0,
        ge=0,
        description="The space between the core and the die, in micrometers (`-core_space`). "
        "The default is the one of OpenROAD-flow-scripts. Needs `sizing: utilization`.",
        json_schema_extra=tier("common"),
    )
    die_area: Optional[Rectangle] = Field(
        None,
        description="The die as a rectangle, in micrometers: lower-left x, lower-left y, "
        "upper-right x, upper-right y (`-die_area`). Needs `sizing: area`, and `core_area` too.",
        json_schema_extra=tier("common"),
    )
    core_area: Optional[Rectangle] = Field(
        None,
        description="The core as a rectangle, in micrometers: lower-left x, lower-left y, "
        "upper-right x, upper-right y (`-core_area`). The core must lie inside the die. Needs "
        "`sizing: area`, and `die_area` too.",
        json_schema_extra=tier("common"),
    )
    def_file: Optional[Path] = Field(
        None,
        description="The DEF file that gives the die, the core, the rows and the tracks "
        "(`read_def -floorplan_initialize`). A relative path is relative to the design root. "
        "Needs `sizing: def`.",
        json_schema_extra=tier("advanced"),
    )
    netlist_cleanup: NetlistCleanupGroup = Field(
        default_factory=NetlistCleanupGroup,
        description="The cleanup of the netlist before the placement.",
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse the leaves that another way of sizing owns, a way of sizing that lacks its
        leaves, and a rectangle that is not one."""
        found: list[tuple[str, str]] = []
        default = type(self)()
        sizing = self.sizing
        owners = {
            "core_utilization": "utilization",
            "aspect_ratio": "utilization",
            "core_margin": "utilization",
            "die_area": "area",
            "core_area": "area",
            "def_file": "def",
        }
        for leaf, owner in owners.items():
            if owner != sizing and getattr(self, leaf) != getattr(default, leaf):
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` is a leaf of `{key}.sizing: {owner}`, and `{key}.sizing` "
                        f"is `{sizing}`: set `{key}.sizing: {owner}` or delete `{key}.{leaf}`",
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
            box = getattr(self, leaf)
            if box is not None and not (box[0] < box[2] and box[1] < box[3]):
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

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("separation", "-separation", "value"),)

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

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a separation when no tie fanout is repaired."""
        if not self.repair_fanout and self.separation != 0:
            return [
                (
                    f"{key}.separation",
                    f"`{key}.separation` has no effect with `{key}.repair_fanout: false`: set "
                    f"`{key}.repair_fanout: true` or delete `{key}.separation`",
                )
            ]
        return []


class TapcellGroup(SettingsGroup):
    """The well tap cells and the end cap cells."""

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

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a leaf that the mode does not use."""
        found: list[tuple[str, str]] = []
        unused = {
            "none": ("distance", "master", "endcap_master"),
            "cut_rows": ("distance", "master"),
        }.get(self.mode or "", ())
        for leaf in unused:
            if getattr(self, leaf) is not None:
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` has no effect with `{key}.mode: {self.mode}`: set "
                        f"`{key}.mode: tap_endcap` or delete `{key}.{leaf}`",
                    )
                )
        return found


class PdnGroup(SettingsGroup):
    """The power grid over the standard cells."""

    mode: Literal["generated", "none"] = Field(
        "generated",
        description="`generated` builds the grid from `grid`, or from the grid of the platform. "
        "`none` builds no grid.",
        json_schema_extra=tier("common"),
    )
    grid: Optional[Pdn] = Field(
        None,
        description="The grid: the supply nets, the connections of the pins to them, the rails, "
        "the straps, the pairs of layers that the vias connect, and the layers of the pins. "
        "Each leaf that you give, and that is not empty, replaces the same leaf of the grid of the "
        "platform. Needs "
        "`mode: generated`. Unset: the grid of the platform.",
        json_schema_extra=tier("common"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a grid when no grid is built."""
        if self.mode == "none" and self.grid is not None:
            return [
                (
                    f"{key}.grid",
                    f"`{key}.grid` has no effect with `{key}.mode: none`: set "
                    f"`{key}.mode: generated` or delete `{key}.grid`",
                )
            ]
        return []


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
        "legalize them. OpenROAD refuses `-place_ios` together with the timing-driven and the "
        "routability-driven placement (GPL-0179 and GPL-0181), so set both modes off for it. "
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
