"""The settings groups of the clock tree flow.

The groups follow the options of `clock_tree_synthesis` (`buffers`, `sink_clustering`, `tree`),
the repair of the clock nets, and the repair of the timing that follows the clock tree. A leaf
that is `None` takes the choice of the tool, unless its description says otherwise. The flow has
no free-text list of flags: a flag that the tool does not need to change is not a setting.
"""

from typing import ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, tier
from ...flow.settings_group import Flag, SettingsGroup

__all__ = ["CtsBuffersGroup", "CtsTreeGroup", "RepairClockNetsGroup", "SinkClusteringGroup"]


class CtsBuffersGroup(SettingsGroup):
    """The buffers that the clock tree may use."""

    #: The flow prints `-buf_list` and `-library` itself, since `select` decides which of them the
    #: command gets.
    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("root", "-root_buf", "value"),)

    select: Literal["inferred", "list", "library"] = Field(
        "inferred",
        description="How the tool finds the clock buffers. `inferred` takes them from the "
        "Liberty files, without the cells that the platform and `dont_use_cells` exclude. `list` "
        "takes the cells of `cells` (`-buf_list`). `library` takes the buffers of the Liberty "
        "library `library` (`-library`).",
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
    library: Optional[str] = Field(
        None,
        min_length=1,
        description="The name of the Liberty library that the buffers come from. Needs `select: "
        "library`.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a list of cells or a library that `select` does not use, and a `select` that
        has nothing to select from."""
        found: list[tuple[str, str]] = []
        for leaf, mode, present in (
            ("cells", "list", bool(self.cells)),
            ("library", "library", self.library is not None),
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
        Flag("max_diameter", "-sink_clustering_max_diameter", "value"),
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
        json_schema_extra=tier("common"),
    )
    max_diameter: Optional[float] = Field(
        None,
        gt=0,
        description="The largest diameter of a cluster, in micrometers "
        "(`-sink_clustering_max_diameter`). Needs `mode: explicit`.",
        json_schema_extra=tier("common"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a size or a diameter outside the explicit mode, and the explicit mode with
        neither."""
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
        return found


class CtsTreeGroup(SettingsGroup):
    """The shape of the clock tree."""

    #: The flow prints `-distance_between_buffers` itself, since `style` decides whether the
    #: command gets it.
    FLAGS: ClassVar[Tuple[Flag, ...]] = ()

    style: Literal["lut", "simple_segments"] = Field(
        "lut",
        description="`lut` builds the tree with the lookup table of the characterization, and "
        "the tool decides where the buffers go. `simple_segments` puts a buffer every "
        "`buffer_distance`.",
        json_schema_extra=tier("advanced"),
    )
    buffer_distance: Optional[float] = Field(
        None,
        gt=0,
        description="The distance between the buffers of a wire, in micrometers "
        "(`-distance_between_buffers`). Needs `style: simple_segments`.",
        json_schema_extra=tier("common"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a distance with the lookup table, and the simple segments with no distance."""
        if self.style == "lut" and self.buffer_distance is not None:
            return [
                (
                    f"{key}.buffer_distance",
                    f"`{key}.buffer_distance` has no effect with `{key}.style: lut`: set "
                    f"`{key}.style: simple_segments` or delete `{key}.buffer_distance`",
                )
            ]
        if self.style == "simple_segments" and self.buffer_distance is None:
            return [
                (
                    f"{key}.buffer_distance",
                    f"`{key}.style: simple_segments` needs `{key}.buffer_distance`: give it or "
                    f"set `{key}.style: lut`",
                )
            ]
        return []


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
