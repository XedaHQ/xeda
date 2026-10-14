"""The power grid of the placement flow: the grid of the platform and the options of the settings.

`pdn.grid` of `openroad_place` has the shape of the grid in the platform model (`Pdn`). It adds
the options of the OpenROAD commands that the platform does not give: more options for a stripe,
the rings around the core, and options for the via connections between two layers. A leaf that
the settings give replaces the same leaf of the platform. The flow turns the stripes and the
connections of the platform into these classes, so a template reads one shape.
"""

from typing import Annotated, Literal, Optional, Union

from ...dataclass import Field, XedaBaseModel
from ...platforms.model import Number, Pdn, PdnRail, PdnStrap

__all__ = ["PdnConnect", "PdnGrid", "PdnRing", "PdnStripe", "connect_of", "stripe_of"]

#: A distance or a size in micrometers that is larger than 0.
Positive = Annotated[Number, Field(gt=0)]
#: A distance in micrometers that is not negative.
Distance = Annotated[Number, Field(ge=0)]
#: A value for the first and the second layer of a pair, or one value for both.
OneOrTwo = Union[Positive, tuple[Positive, Positive]]

Polarity = Literal["power", "ground"]


class PdnStripe(PdnStrap):
    """A stripe of the power grid. `width`, `pitch` and `offset` are in micrometers, and the
    offset counts from the lower-left corner of the core."""

    width: Positive = Field(description="Width of the stripe, in micrometers.")
    pitch: Positive = Field(description="Pitch of the stripe, in micrometers.")
    offset: Distance = Field(description="Offset of the stripe, in micrometers.")
    spacing: Optional[Positive] = Field(
        None,
        description="Space between the power stripe and the ground stripe of one pitch, in "
        "micrometers (`-spacing`). Unset: half the pitch.",
    )
    offset_type: Optional[Literal["start", "first", "center", "last", "end"]] = Field(
        None,
        description="The point of a group of stripes that `offset` places (`-offset_type`): the "
        "lower edge of the first stripe, the center of the first stripe, the center of the "
        "group, the center of the last stripe, or the upper edge of the last stripe. Unset: "
        "`first`.",
    )
    starts_with: Optional[Polarity] = Field(
        None,
        description="Whether the first stripe is power or ground (`-starts_with`). Unset: the "
        "choice of the grid.",
    )
    extend: Optional[Literal["core_ring", "boundary"]] = Field(
        None,
        description="Extend the stripes to the core ring or to the boundary of the grid "
        "(`-extend_to_core_ring`, `-extend_to_boundary`). Unset: the stripes stop at the last "
        "connection.",
    )
    extend_by: Optional[Distance] = Field(
        None,
        description="Extend the stripes this far beyond where they would stop, in micrometers "
        "(`-extend_by`). They never go beyond the boundary of the grid.",
    )
    snap_to_grid: bool = Field(
        False, description="Snap the stripes to the routing tracks (`-snap_to_grid`)."
    )
    number_of_straps: Optional[int] = Field(
        None,
        ge=1,
        description="The number of pairs of a power stripe and a ground stripe (`-number_of_straps`). "
        "Unset: as many as fit.",
    )
    nets: list[str] = Field(
        default_factory=list,
        description="Limit the stripes to these nets (`-nets`). Empty: all the nets of the grid.",
    )
    allow_out_of_core: bool = Field(
        False,
        description="Let the stripes continue beyond the core area (`-allow_out_of_core`).",
    )


class PdnRing(XedaBaseModel):
    """A ring of a power and a ground stripe around the core. It uses two layers with
    directions at right angles."""

    layers: tuple[str, str] = Field(
        description="The vertical layer and the horizontal layer of the ring (`-layers`). "
        "OpenROAD refuses two layers that run in the same direction."
    )
    widths: OneOrTwo = Field(
        description="Width of the stripes, in micrometers: one value, or one for each layer "
        "(`-widths`)."
    )
    spacings: OneOrTwo = Field(
        description="Space between the power stripe and the ground stripe, in micrometers: one "
        "value, or one for each layer (`-spacings`)."
    )
    core_offset: Union[Distance, tuple[Distance, Distance, Distance, Distance]] = Field(
        description="Distance between the ring and the core, in micrometers: one value for all "
        "sides, or four: left, bottom, right, top (`-core_offsets`)."
    )
    add_connect: bool = Field(
        False, description="Connect the two layers of the ring with vias (`-add_connect`)."
    )
    extend_to_boundary: bool = Field(
        False, description="Extend the ring to the boundary of the grid (`-extend_to_boundary`)."
    )
    nets: list[str] = Field(
        default_factory=list,
        description="Limit the ring to these nets (`-nets`). Empty: all the nets of the grid.",
    )
    starts_with: Optional[Polarity] = Field(
        None,
        description="Whether the first stripe is power or ground (`-starts_with`). Unset: the "
        "choice of the grid.",
    )
    allow_out_of_die: bool = Field(
        False,
        description="Let the ring go beyond the die (`-allow_out_of_die`). OpenROAD refuses a "
        "ring that does not fit in the die otherwise.",
    )


class PdnConnect(XedaBaseModel):
    """The vias that connect two layers of the grid where nets of the same kind overlap."""

    layers: tuple[str, str] = Field(description="The two layers to connect (`-layers`).")
    cut_pitch: Optional[OneOrTwo] = Field(
        None,
        description="The distance between via cuts when the two layers run in parallel, in "
        "micrometers: one value, or x and y (`-cut_pitch`).",
    )
    fixed_vias: list[str] = Field(
        default_factory=list,
        description="The vias of the technology to use for the stack (`-fixed_vias`). A "
        "technology without via rules needs them.",
    )
    dont_use_vias: list[str] = Field(
        default_factory=list,
        description="The vias, or patterns of vias, that the stack must not use "
        "(`-dont_use_vias`).",
    )
    max_rows: Optional[int] = Field(
        None, ge=1, description="The most rows of an array of vias (`-max_rows`)."
    )
    max_columns: Optional[int] = Field(
        None, ge=1, description="The most columns of an array of vias (`-max_columns`)."
    )
    ongrid: list[str] = Field(
        default_factory=list,
        description="The layers in the middle of a via stack to snap to the routing grid "
        "(`-ongrid`).",
    )
    min_width_layers: list[str] = Field(
        default_factory=list,
        description="The layers in the middle of a via stack to keep at their least width "
        "(`-min_width_layers`).",
    )
    split_cuts: dict[str, Positive] = Field(
        default_factory=dict,
        description="Layers that use split cuts, each with its pitch in micrometers "
        "(`-split_cuts`).",
    )
    split_cuts_staggered: bool = Field(
        False,
        description="Stagger the split cuts: the ground vias are half a pitch off "
        "(`-split_cuts_staggered`). Needs `split_cuts`.",
    )


class PdnGrid(Pdn):
    """The power grid. Each leaf that is not empty replaces the same leaf of the grid of the
    platform. `connects` takes a pair of layers, as the platform gives it, or an object with
    options."""

    rails: list[PdnRail] = Field(
        default_factory=list,
        description="Rails that follow the rows of cells. Each has a layer and a width, and may "
        "have a pitch and an offset, in micrometers.",
    )
    straps: list[PdnStripe] = Field(  # type: ignore[assignment]
        default_factory=list, description="Stripes of the grid, with their options."
    )
    rings: list[PdnRing] = Field(
        default_factory=list, description="Rings around the core. The platform gives none."
    )
    connects: list[Union[tuple[str, str], PdnConnect]] = Field(  # type: ignore[assignment]
        default_factory=list,
        description="The vias between layers: a pair of layers, or an object with the layers "
        "and the options of the vias. If you replace the layers of `rails` or `straps`, give "
        "`connects` too, since the connections of the platform name its layers.",
    )
    starts_with: Optional[Polarity] = Field(
        None,
        description="Whether the first stripe of the grid is power or ground "
        "(`define_pdn_grid -starts_with`). Unset: the tool starts with ground.",
    )


def stripe_of(strap: PdnStrap) -> PdnStripe:
    """Return `strap` as a stripe with the options unset, or `strap` itself if it is one."""
    if isinstance(strap, PdnStripe):
        return strap
    return PdnStripe.model_validate(strap.model_dump())


def connect_of(connect: Union[tuple[str, str], PdnConnect]) -> PdnConnect:
    """Return a pair of layers as a connection with the options unset."""
    if isinstance(connect, PdnConnect):
        return connect
    return PdnConnect(layers=connect)
