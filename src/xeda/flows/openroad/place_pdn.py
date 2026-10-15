"""The power grid of the placement flow: the grid of the platform and the options of the settings.

`pdn.grid` of `openroad_place` has the shape of the grid in the platform model (`Pdn`). It adds
the options of the OpenROAD commands that the platform does not give: more options for a stripe,
the rings around the core, and options for the via connections between two layers. A leaf that
the settings give replaces the same leaf of the platform. The platform gives its distances as
plain numbers of micrometers. The flow turns the rails, the stripes and the connections of the
platform into these classes, which hold lengths with units, so a template reads one shape.
"""

from typing import Annotated, Literal, Optional, Union

from ...dataclass import Field, XedaBaseModel
from ...platforms.model import Pdn
from ...platforms.model import PdnRail as PlatformRail
from ...platforms.model import PdnStrap
from ...units import Bounds, Length, count

__all__ = [
    "PdnConnect",
    "PdnGrid",
    "PdnRail",
    "PdnRing",
    "PdnStripe",
    "connect_of",
    "grid_of",
    "rail_of",
    "stripe_of",
]

#: A distance or a size that is larger than 0.
Positive = Annotated[Length, Bounds(gt="0 um")]
#: A distance that is not negative.
Distance = Annotated[Length, Bounds(ge="0 um")]
#: A value for the first and the second layer of a pair, or one value for both.
OneOrTwo = Union[Positive, tuple[Positive, Positive]]

Polarity = Literal["power", "ground"]


class PdnRail(XedaBaseModel):
    """A rail of the power grid that follows the rows of cells."""

    layer: str = Field(description="Layer of the rail.")
    width: Positive = Field(description="Width of the rail, a length with a unit.")
    pitch: Optional[Positive] = Field(None, description="Pitch of the rail, a length with a unit.")
    offset: Optional[Length] = Field(None, description="Offset of the rail, a length with a unit.")


class PdnStripe(XedaBaseModel):
    """A stripe of the power grid. The offset counts from the lower-left corner of the core."""

    layer: str = Field(description="Layer of the stripe.")
    width: Positive = Field(description="Width of the stripe, a length with a unit.")
    pitch: Positive = Field(description="Pitch of the stripe, a length with a unit.")
    offset: Distance = Field(description="Offset of the stripe, a length with a unit.")
    spacing: Optional[Positive] = Field(
        None,
        description="Space between the power stripe and the ground stripe of one pitch, a "
        "length with a unit (`-spacing`). Unset: half the pitch.",
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
        description="Extend the stripes this far beyond where they would stop, a length with a "
        "unit (`-extend_by`). They never go beyond the boundary of the grid.",
    )
    snap_to_grid: bool = Field(
        False, description="Snap the stripes to the routing tracks (`-snap_to_grid`)."
    )
    number_of_straps: Optional[int] = Field(
        None,
        ge=1,
        description="The number of pairs of a power stripe and a ground stripe "
        "(`-number_of_straps`). Unset: as many as fit.",
        json_schema_extra=count(),
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
        description="Width of the stripes, a length with a unit: one value, or one for each "
        "layer (`-widths`)."
    )
    spacings: OneOrTwo = Field(
        description="Space between the power stripe and the ground stripe, a length with a "
        "unit: one value, or one for each layer (`-spacings`)."
    )
    core_offset: Union[Distance, tuple[Distance, Distance, Distance, Distance]] = Field(
        description="Distance between the ring and the core, a length with a unit: one value "
        "for all sides, or four: left, bottom, right, top (`-core_offsets`)."
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
        description="The distance between via cuts when the two layers run in parallel, a "
        "length with a unit: one value, or x and y (`-cut_pitch`).",
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
        None,
        ge=1,
        description="The most rows of an array of vias (`-max_rows`).",
        json_schema_extra=count(),
    )
    max_columns: Optional[int] = Field(
        None,
        ge=1,
        description="The most columns of an array of vias (`-max_columns`).",
        json_schema_extra=count(),
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
        description="Layers that use split cuts, each with its pitch, a length with a unit "
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

    rails: list[PdnRail] = Field(  # type: ignore[assignment]
        default_factory=list,
        description="Rails that follow the rows of cells. Each has a layer and a width, and may "
        "have a pitch and an offset, as lengths with units.",
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


def rail_of(rail: Union[PlatformRail, PdnRail]) -> PdnRail:
    """Return a rail of the platform, whose distances are plain micrometers, as a rail with
    lengths, or `rail` itself if it is one."""
    if isinstance(rail, PdnRail):
        return rail
    return PdnRail(
        layer=rail.layer,
        width=Length.of(rail.width, "um"),
        pitch=None if rail.pitch is None else Length.of(rail.pitch, "um"),
        offset=None if rail.offset is None else Length.of(rail.offset, "um"),
    )


def stripe_of(strap: Union[PdnStrap, PdnStripe]) -> PdnStripe:
    """Return a stripe of the platform, whose distances are plain micrometers, as a stripe with
    lengths and the options unset, or `strap` itself if it is a stripe."""
    if isinstance(strap, PdnStripe):
        return strap
    return PdnStripe(
        layer=strap.layer,
        width=Length.of(strap.width, "um"),
        pitch=Length.of(strap.pitch, "um"),
        offset=Length.of(strap.offset, "um"),
    )


def grid_of(platform: Pdn) -> PdnGrid:
    """Return the power grid of the platform with its rails and stripes as lengths."""
    values = {
        name: getattr(platform, name)
        for name in Pdn.model_fields
        if name not in ("rails", "straps")
    }
    return PdnGrid(
        **values,
        rails=[rail_of(rail) for rail in platform.rails],
        straps=[stripe_of(strap) for strap in platform.straps],
    )


def connect_of(connect: Union[tuple[str, str], PdnConnect]) -> PdnConnect:
    """Return a pair of layers as a connection with the options unset."""
    if isinstance(connect, PdnConnect):
        return connect
    return PdnConnect(layers=connect)
