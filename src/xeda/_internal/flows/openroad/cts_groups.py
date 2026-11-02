"""The settings groups of the clock tree flow.

The groups follow the options of `clock_tree_synthesis` (`buffers`, `sink_clustering`,
`macro_clustering`, `tree`, `balance`, `ndr`), the configuration of the characterization of the
buffers, the repair of the clock nets, and the repair of the timing that follows the clock tree.
A leaf that is `None` takes the choice of the tool, unless its description says otherwise. The
flow has no free-text list of flags: a flag that the tool does not need to change is not a
setting. The flags `-balance_levels`, `-obstruction_aware` and `-post_cts_disable` are obsolete in
the tool, which warns and ignores them, so no setting passes them.

Rules that the tool leaves to itself, and that the groups do not check:

* A root, sink or tree buffer need not be in the list of `buffers.cells`. The tool does not
  require it.
* The NDR strategy needs rules in the technology. The tool applies the rules that it finds and
  warns when it finds none.
* The characterization uses `max_slew` and `max_cap` only when both are set (see the description
  of the group).
"""

from typing import Annotated, ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, extras, field_validator, tier
from ...flow.settings_group import Flag, SettingsGroup
from ...units import Bounds, Capacitance, Length, Time, count, ratio
from .groups import whole_micrometers

__all__ = [
    "BalanceGroup",
    "CharacterizationGroup",
    "CtsBuffersGroup",
    "CtsTreeGroup",
    "MacroClusteringGroup",
    "CtsNdrGroup",
    "RepairClockNetsGroup",
    "SinkClusteringGroup",
    "WordName",
]

#: A number that is at least zero and finite.
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
#: A length above zero.
PositiveLength = Annotated[Length, Bounds(gt="0 um")]
#: A length that is at least zero.
NonNegativeLength = Annotated[Length, Bounds(ge="0 um")]
#: A name without white space, for a list that the tool splits at white space.
WordName = Annotated[str, Field(pattern=r"^\S+$")]


class CtsBuffersGroup(SettingsGroup):
    """The buffers that the clock tree may use."""

    #: The flow prints `-buf_list` and `-library` itself, and `set_cts_config` the footprint and
    #: the name, since `select` decides which of them the tool gets.
    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("root", "-root_buf", "value"),
        Flag("tree", "-tree_buf", "value"),
        Flag("sink", "-sink_clustering_buffer", "value"),
    )

    select: Literal["inferred", "list", "library", "footprint", "name"] = Field(
        "inferred",
        description="How the tool finds the clock buffers. `inferred` takes them from the "
        "Liberty files, without the cells that the platform and `dont_use_cells` exclude. `list` "
        "takes the cells of `cells` (`-buf_list`). `library` takes the buffers of the Liberty "
        "library `library` (`-library`). `footprint` takes the cells whose footprint in the "
        "Liberty file contains `footprint` (`set_cts_config -clock_buffer_footprint`). `name` "
        "takes the cells whose name contains `name_pattern` (`set_cts_config "
        "-clock_buffer_string`).",
        json_schema_extra=tier("common"),
    )
    cells: list[str] = Field(
        [],
        description="The buffer cells that the clock tree may use. Needs `select: list`.",
        json_schema_extra=tier("common"),
    )
    root: Optional[str] = Field(
        None,
        min_length=1,
        description="The buffer at the root of the clock tree (`-root_buf`). Unset: the tool "
        "chooses it, the first cell of `cells` when there is a list.",
        json_schema_extra=tier("advanced"),
    )
    tree: Optional[str] = Field(
        None,
        min_length=1,
        description="The buffer inside the tree (`-tree_buf`). Unset: the tool chooses it.",
        json_schema_extra=tier("expert"),
    )
    sink: Optional[str] = Field(
        None,
        min_length=1,
        description="The buffer that drives each cluster of sinks (`-sink_clustering_buffer`). "
        "Unset: the tool chooses it.",
        json_schema_extra=tier("expert"),
    )
    library: Optional[str] = Field(
        None,
        min_length=1,
        description="The name of the Liberty library that the buffers come from. Needs `select: "
        "library`.",
        json_schema_extra=tier("advanced"),
    )
    footprint: Optional[str] = Field(
        None,
        min_length=1,
        description="The text that the footprint of a clock buffer contains. Needs `select: "
        "footprint`. The tool also uses it to choose the buffers of the repair of the clock "
        "nets.",
        json_schema_extra=tier("expert"),
    )
    name_pattern: Optional[str] = Field(
        None,
        min_length=1,
        description="The text that the name of a clock buffer contains. Needs `select: name`. "
        "The tool's own default is `CLKBUF`.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a list of cells, a library, a footprint or a name that `select` does not use,
        and a `select` that has nothing to select from."""
        found: list[tuple[str, str]] = []
        for leaf, mode, present in (
            ("cells", "list", bool(self.cells)),
            ("library", "library", self.library is not None),
            ("footprint", "footprint", self.footprint is not None),
            ("name_pattern", "name", self.name_pattern is not None),
        ):
            if present and self.select != mode:
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.{leaf}` has no effect with `{key}.select: {self.select}`: set "
                        f"`{key}.select: {mode}` or delete `{key}.{leaf}`",
                    )
                )
            if not present and self.select == mode:
                found.append(
                    (
                        f"{key}.{leaf}",
                        f"`{key}.select: {mode}` takes the buffers from `{key}.{leaf}`, and "
                        f"that is not set: give `{key}.{leaf}` or set `{key}.select: inferred`",
                    )
                )
        return found


class SinkClusteringGroup(SettingsGroup):
    """The grouping of the clock sinks under one buffer each."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("size", "-sink_clustering_size", "value"),
        Flag("levels", "-sink_clustering_levels", "value"),
        Flag("buffer_cap_derate", "-sink_buffer_max_cap_derate", "value"),
    )

    mode: Literal["off", "auto", "explicit"] = Field(
        "auto",
        description="`off` builds no clusters. `auto` builds clusters with the size and the "
        "diameter that the tool derives from the buffers (`-sink_clustering_enable`). "
        "`explicit` builds them with `size`, `max_diameter` or both.",
        json_schema_extra=tier("common"),
    )
    size: Optional[int] = Field(
        None,
        ge=1,
        description="The most sinks in a cluster (`-sink_clustering_size`). Needs `mode: "
        "explicit`.",
        json_schema_extra=extras(tier("common"), count()),
    )
    max_diameter: Optional[PositiveLength] = Field(
        None,
        description="The largest diameter of a cluster, a length with a unit "
        "(`-sink_clustering_max_diameter`). Needs `mode: explicit`.",
        json_schema_extra=tier("common"),
    )
    levels: Optional[int] = Field(
        None,
        ge=1,
        description="Cluster the clusters again, with this many extra levels, when the design "
        "has more than 400 clusters (`-sink_clustering_levels`). Needs clustering: not with "
        "`mode: off`. Unset: no second level.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    buffer_cap_derate: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The share of the maximum capacitance of a buffer that the tool counts when "
        "it chooses the root and sink buffers itself, from 0 to 1 (`-sink_buffer_max_cap_derate`)"
        ". A small value favors strong buffers, and 1 applies no derating. Unset: the default of "
        "the tool, 0.01.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a size or a diameter outside the explicit mode, the levels without clustering,
        and the explicit mode with neither size nor diameter."""
        found: list[tuple[str, str]] = []
        if self.mode != "explicit":
            for leaf in ("size", "max_diameter"):
                if getattr(self, leaf) is not None:
                    found.append(
                        (
                            f"{key}.{leaf}",
                            f"`{key}.{leaf}` has no effect with `{key}.mode: {self.mode}`: set "
                            f"`{key}.mode: explicit` or delete `{key}.{leaf}`",
                        )
                    )
        elif self.size is None and self.max_diameter is None:
            found.append(
                (
                    f"{key}.mode",
                    f"`{key}.mode: explicit` needs `{key}.size`, `{key}.max_diameter` or both: "
                    f"give one of them or set `{key}.mode: auto`",
                )
            )
        if self.mode == "off" and self.levels is not None:
            found.append(
                (
                    f"{key}.levels",
                    f"`{key}.levels` has no effect with `{key}.mode: off`: set "
                    f"`{key}.mode: auto` or delete `{key}.levels`",
                )
            )
        return found


class MacroClusteringGroup(SettingsGroup):
    """The grouping of the macro sinks, which the tool clusters on their own."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("size", "-macro_clustering_size", "value"),)

    size: Optional[int] = Field(
        None,
        ge=1,
        description="The most macro sinks in a cluster (`-macro_clustering_size`). Unset: the "
        "default of the tool, 4.",
        json_schema_extra=extras(tier("advanced"), count()),
    )
    max_diameter: Optional[PositiveLength] = Field(
        None,
        description="The largest diameter of a cluster of macro sinks, a length with a unit "
        "(`-macro_clustering_max_diameter`). Unset: the default of the tool, 50.",
        json_schema_extra=tier("advanced"),
    )


class CtsTreeGroup(SettingsGroup):
    """The shape of the clock tree."""

    #: The flow prints `-distance_between_buffers` itself, since `style` decides whether the
    #: command gets it.
    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("clustering_exponent", "-clustering_exponent", "value"),
        Flag("clustering_unbalance_ratio", "-clustering_unbalance_ratio", "value"),
        Flag("static_layers", "-num_static_layers", "value"),
        Flag("obstruction_aware", "-no_obstruction_aware", "skip"),
    )

    style: Literal["lut", "simple_segments"] = Field(
        "lut",
        description="`lut` builds the tree with the lookup table of the characterization, and "
        "the tool decides where the buffers go. `simple_segments` puts a buffer every "
        "`buffer_distance`.",
        json_schema_extra=tier("advanced"),
    )
    buffer_distance: Optional[PositiveLength] = Field(
        None,
        description="The distance between the buffers of a wire, a length with a unit "
        "(`-distance_between_buffers`). Needs `style: simple_segments`.",
        json_schema_extra=tier("common"),
    )
    branch_buffer_distance: Optional[PositiveLength] = Field(
        None,
        description="A branch at least this long gets a buffer at its end point, a length with "
        "a unit (`-branching_point_buffers_distance`). Needs `style: simple_segments`. "
        "Unset: no buffer at the end of a branch.",
        json_schema_extra=tier("expert"),
    )
    wire_unit: Optional[PositiveLength] = Field(
        None,
        description="The shortest wire segment that the characterization tests, a length with "
        "a unit and a whole number of micrometers (`-wire_unit`). Unset: the tool derives it "
        "from the height of the buffer that it characterizes.",
        json_schema_extra=tier("expert"),
    )

    @field_validator("wire_unit")
    @classmethod
    def _wire_unit_in_whole_micrometers(cls, value: Optional[Length]) -> Optional[Length]:
        """Refuse a wire unit that is not a whole number of micrometers."""
        return None if value is None else whole_micrometers(value)

    clustering_exponent: Optional[int] = Field(
        None,
        ge=0,
        description="The power that the clustering of the sinks (CKMeans) puts on the distance "
        "between a sink and the mean of its cluster (`-clustering_exponent`). Unset: the "
        "default of the tool, 4.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    clustering_unbalance_ratio: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="The largest share of the sinks that one cluster may take in the clustering "
        "of the sinks, from 0 to 1 (`-clustering_unbalance_ratio`). 0.5 splits the sinks of a "
        "region in two equal halves. Unset: the default of the tool, 0.6.",
        json_schema_extra=extras(tier("expert"), ratio()),
    )
    static_layers: Optional[int] = Field(
        None,
        ge=0,
        description="The number of top levels of the tree that have fixed buffers "
        "(`-num_static_layers`). Unset: the default of the tool, 0.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    obstruction_aware: bool = Field(
        True,
        description="Keep the clock buffers off blockages and macros. False passes "
        "`-no_obstruction_aware`. The default is the one of the tool.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a distance with the lookup table, and the simple segments with no distance."""
        found: list[tuple[str, str]] = []
        if self.style == "lut":
            for leaf in ("buffer_distance", "branch_buffer_distance"):
                if getattr(self, leaf) is not None:
                    found.append(
                        (
                            f"{key}.{leaf}",
                            f"`{key}.{leaf}` has no effect with `{key}.style: lut`: set "
                            f"`{key}.style: simple_segments` or delete `{key}.{leaf}`",
                        )
                    )
        elif self.buffer_distance is None:
            found.append(
                (
                    f"{key}.buffer_distance",
                    f"`{key}.style: simple_segments` needs `{key}.buffer_distance`: give it or "
                    f"set `{key}.style: lut`",
                )
            )
        return found


class BalanceGroup(SettingsGroup):
    """The balance of the clock latencies between macros and registers, and of the loads at the
    leaves."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("delay_buffer_derate", "-delay_buffer_derate", "value"),
        Flag("insertion_delay", "-no_insertion_delay", "skip"),
        Flag("dummy_load", "-dont_use_dummy_load", "skip"),
    )

    delay_buffer_derate: Optional[NonNegative] = Field(
        None,
        description="The share of the delay buffers that the tool inserts to balance the "
        "latencies of macros and registers (`-delay_buffer_derate`). 1 inserts all of them, and "
        "0 inserts none. Needs `insertion_delay`. Unset: the default of the tool, 1.",
        json_schema_extra=extras(tier("advanced"), ratio()),
    )
    insertion_delay: bool = Field(
        True,
        description="Take the insertion delay of the sinks (`min_clock_tree_path` and "
        "`max_clock_tree_path` in the Liberty file of a macro) into account when the tool builds "
        "and balances the tree. False passes `-no_insertion_delay`, and the tool then does not "
        "balance the latencies of macros and registers. The default is the one of the tool.",
        json_schema_extra=tier("advanced"),
    )
    dummy_load: bool = Field(
        True,
        description="Add dummy loads (`clkload` cells) at the leaves of the tree to balance the "
        "loads. False passes `-dont_use_dummy_load`. The default is the one of the tool.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a share of delay buffers when the balance of the latencies is off."""
        if self.delay_buffer_derate is not None and not self.insertion_delay:
            return [
                (
                    f"{key}.delay_buffer_derate",
                    f"`{key}.delay_buffer_derate` has no effect with `{key}.insertion_delay: "
                    f"false`: set `{key}.insertion_delay: true` or delete "
                    f"`{key}.delay_buffer_derate`",
                )
            ]
        return []


class CtsNdrGroup(SettingsGroup):
    """The non-default routing rule (NDR) of the clock nets."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("strategy", "-apply_ndr", "value"),)

    strategy: Optional[Literal["none", "root_only", "half", "full"]] = Field(
        None,
        description="The clock nets that get the NDR with twice the default spacing "
        "(`-apply_ndr`). The leaf nets never get it. `none` applies no rule, `root_only` applies "
        "it to the trunk at the root, `half` to the first half of the levels of the tree, and "
        "`full` to every level. Unset: the default of the tool, `half`.",
        json_schema_extra=tier("advanced"),
    )


class CharacterizationGroup(SettingsGroup):
    """The characterization of the clock buffers, which `clock_tree_synthesis` runs first and
    builds its lookup table from (`configure_cts_characterization`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("slew_steps", "-slew_steps", "value"),
        Flag("cap_steps", "-cap_steps", "value"),
    )

    max_slew: Optional[Annotated[Time, Bounds(gt="0 ns")]] = Field(
        None,
        description="The largest slew that the characterization tests, a time with a unit, such "
        "as `250 ps` (`-max_slew`). The flow converts it to seconds, which the command takes. Set it with "
        "`max_cap`: the tool uses the limits only when both are set, and takes both from the "
        "Liberty files when one is missing. It ignores a single limit without a message "
        "(`TechChar.cpp`, seen with OpenROAD v2.0-26132-g6b305a712).",
        json_schema_extra=tier("expert"),
    )
    max_cap: Optional[Annotated[Capacitance, Bounds(gt="0 fF")]] = Field(
        None,
        description="The largest capacitance that the characterization tests, a capacitance "
        "with a unit, such as `0.1 pF` (`-max_cap`). The flow converts it to farads, which the command takes. Set it with "
        "`max_slew`: see there.",
        json_schema_extra=tier("expert"),
    )
    slew_steps: Optional[int] = Field(
        None,
        ge=1,
        description="The number of steps on the slew axis of the characterization "
        "(`-slew_steps`). Unset: the default of the tool, 7. The README of OpenROAD says 12.",
        json_schema_extra=extras(tier("expert"), count()),
    )
    cap_steps: Optional[int] = Field(
        None,
        ge=1,
        description="The number of steps on the capacitance axis of the characterization "
        "(`-cap_steps`). Unset: the default of the tool, 20. The README of OpenROAD says 34.",
        json_schema_extra=extras(tier("expert"), count()),
    )


class RepairClockNetsGroup(SettingsGroup):
    """The buffering of the long wire from the clock port to the root buffer."""

    when: Literal["off", "in_cts", "after"] = Field(
        "in_cts",
        description="`off` leaves the wire as it is. `in_cts` repairs it inside "
        "`clock_tree_synthesis` (`-repair_clock_nets`), before the tool balances the latencies. "
        "`after` runs `repair_clock_nets` after the clock tree. The default is the one of "
        "OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    max_wire_length: Optional[NonNegativeLength] = Field(
        None,
        description="Buffer each clock wire that is longer than this length, a length with a "
        "unit (`repair_clock_nets -max_wire_length`). Needs `when: after`: the repair inside "
        "`clock_tree_synthesis` takes no length. Unset or 0: the length that gives the least "
        "wire delay for the wire RC.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a wire length when the standalone repair does not run."""
        if self.max_wire_length is not None and self.when != "after":
            return [
                (
                    f"{key}.max_wire_length",
                    f"`{key}.max_wire_length` has no effect with `{key}.when: {self.when}`: set "
                    f"`{key}.when: after` or delete `{key}.max_wire_length`",
                )
            ]
        return []
