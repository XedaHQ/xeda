"""The model of an ASIC platform: the values a flow needs about a PDK, and where each came from.

A platform model has four layers, weakest first:

* `derived`: facts that a reader finds in the PDK files (the site in the LEF, the supply voltage
  in the Liberty file);
* `upstream`: values from a configuration file of the PDK or of OpenROAD-flow-scripts;
* `residue`: the small file that xeda ships for the platform (`platform.yaml`);
* `user`: a residue file of the user.

A later layer wins at each *leaf*. A leaf key is dotted text that names one value of the model:

* a field of `PlatformModel` (`site`, `dont_use_cells`, `place_density`);
* a field of a group model (`tapcell.distance`, `routing.min_layer`, `pdn.rails`);
* one key of a table, which is a `dict` field (`tracks.metal1`, `wire_rc.layers.metal3`).

Tables merge key by key. Everything else is one whole value, a list or a small model, and a
later layer replaces it whole. A table key never contains a `.`, so a leaf key has one meaning.

This module starts no process and reads no file.
"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import UnionType
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Optional,
    Self,
    TypeGuard,
    Union,
    get_args,
    get_origin,
)

from pydantic import StrictBool, StrictFloat, StrictInt, TypeAdapter, ValidationError

from xeda.dataclass import Field, XedaBaseModel, model_validator

__all__ = [
    "CellPort",
    "CellPorts",
    "GlobalConnect",
    "Group",
    "IoLayers",
    "Layer",
    "LAYERS",
    "Located",
    "Pdn",
    "PdnRail",
    "PdnStrap",
    "PlatformModel",
    "PlatformValues",
    "Routing",
    "Supply",
    "Tapcell",
    "Track",
    "WireRc",
    "flatten_leaves",
    "leaf_annotation",
    "nest_leaves",
    "normalize_leaf",
    "NOT_VALUES",
    "Number",
]

#: The layer that gave a value.
Layer = Literal["derived", "upstream", "residue", "user"]
#: The layers in merge order: a later layer wins.
LAYERS: tuple[Layer, ...] = get_args(Layer)

#: A number that takes a number: text such as `"0.3"` is refused.
Number = StrictFloat
Count = StrictInt


def is_table(annotation: Any) -> bool:
    """Whether a field of this type is a table (a `dict`): its keys are leaves."""
    return get_origin(annotation) is dict


class _Model(XedaBaseModel):
    """The base of the value models: no unknown key, and no `.` in the key of a table."""

    #: Table fields whose keys are dotted on purpose (leaf keys).
    leaf_keyed: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _table_keys_have_no_dot(self) -> Self:
        for name, field in type(self).model_fields.items():
            if name in self.leaf_keyed or not is_table(field.annotation):
                continue
            for key in getattr(self, name):
                if "." in key:
                    raise ValueError(
                        f"the key `{key}` of `{name}` contains a `.`; a table key is a plain name, "
                        "because a dot separates the parts of a leaf key"
                    )
        return self


class Group(_Model):
    """A model that holds a group of leaves: a field of it is a leaf key `<group>.<field>`."""


# ---------------------------------------------------------------------------------------------
# Value models
# ---------------------------------------------------------------------------------------------


class CellPort(_Model):
    """A cell and one of its ports."""

    cell: str = Field(description="Name of the cell.")
    port: str = Field(description="Name of the port of the cell.")


class CellPorts(_Model):
    """A cell with its input and output port."""

    cell: str = Field(description="Name of the cell.")
    input: str = Field(description="Name of the input port.")
    output: str = Field(description="Name of the output port.")


class Track(_Model):
    """The routing tracks of one layer, as the arguments of OpenROAD's `make_tracks`."""

    x: Optional[tuple[Number, Number]] = Field(
        None, description="Offset and pitch of the vertical tracks, in micrometers."
    )
    y: Optional[tuple[Number, Number]] = Field(
        None, description="Offset and pitch of the horizontal tracks, in micrometers."
    )


class Tapcell(Group):
    """Placement of the tap cells."""

    distance: Optional[Number] = Field(
        None, description="Distance between tap cells in a row, in micrometers."
    )
    halo: Optional[Number] = Field(
        None, description="Distance in micrometers that the tap cells keep from a macro."
    )


class Supply(Group):
    """The power supply."""

    power_pin: Optional[str] = Field(
        None, description="Name of the power pin of the cells (a LEF pin with `USE POWER`)."
    )
    ground_pin: Optional[str] = Field(
        None, description="Name of the ground pin of the cells (a LEF pin with `USE GROUND`)."
    )
    voltage: Optional[Number] = Field(
        None, description="Nominal supply voltage of the corner, in volts."
    )
    power_nets: dict[str, Number] = Field(
        default_factory=dict, description="Voltage in volts of each power net, by net name."
    )
    ground_nets: dict[str, Number] = Field(
        default_factory=dict, description="Voltage in volts of each ground net, by net name."
    )


class Routing(Group):
    """The layers of global and detailed routing."""

    min_layer: Optional[str] = Field(None, description="Lowest layer that signal nets use.")
    max_layer: Optional[str] = Field(None, description="Highest layer that nets use.")
    clock_min_layer: Optional[str] = Field(None, description="Lowest layer that clock nets use.")
    layer_adjustments: dict[str, Number] = Field(
        default_factory=dict,
        description="Fraction of the routing capacity to keep free, by layer or layer range "
        "(`metal2-metal3`).",
    )
    layer_adjustment: Optional[Number] = Field(
        None, description="One fraction of the routing capacity to keep free on every layer."
    )


class IoLayers(Group):
    """The layers of the I/O pins."""

    horizontal: Optional[str] = Field(None, description="Layer of the pins on horizontal edges.")
    vertical: Optional[str] = Field(None, description="Layer of the pins on vertical edges.")


class WireRc(Group):
    """The resistance and capacitance of the wires, as OpenROAD's `set_layer_rc` takes them."""

    signal_layer: Optional[str] = Field(
        None, description="Layer whose resistance and capacitance estimate a signal net."
    )
    clock_layer: Optional[str] = Field(
        None, description="Layer whose resistance and capacitance estimate a clock net."
    )
    layers: dict[str, tuple[Number, Number]] = Field(
        default_factory=dict,
        description="Resistance and capacitance per micrometer of each layer, in the units of "
        "the Liberty file.",
    )
    vias: dict[str, Number] = Field(
        default_factory=dict, description="Resistance of each via, by via name."
    )


class GlobalConnect(_Model):
    """Connection of the power and ground pins of the instances to the power grid nets."""

    net: str = Field(description="Name of the net.")
    inst_pattern: str = Field(
        ".*", description="Regular expression for the instance names. The default matches all."
    )
    pin_pattern: str = Field(description="Regular expression for the pin names.")
    kind: Optional[Literal["power", "ground"]] = Field(
        None, description="Whether the pins are the power or the ground pins of the cells."
    )


class PdnRail(_Model):
    """A rail of the power grid that follows the rows of cells."""

    layer: str = Field(description="Layer of the rail.")
    width: Number = Field(description="Width of the rail, in micrometers.")
    pitch: Optional[Number] = Field(None, description="Pitch of the rail, in micrometers.")
    offset: Optional[Number] = Field(None, description="Offset of the rail, in micrometers.")


class PdnStrap(_Model):
    """A stripe of the power grid."""

    layer: str = Field(description="Layer of the stripe.")
    width: Number = Field(description="Width of the stripe, in micrometers.")
    pitch: Number = Field(description="Pitch of the stripe, in micrometers.")
    offset: Number = Field(description="Offset of the stripe, in micrometers.")


class Pdn(Group):
    """The power grid over the standard cells. Grids around macros are not supported."""

    power_net: Optional[str] = Field(None, description="Name of the power net.")
    ground_net: Optional[str] = Field(None, description="Name of the ground net.")
    global_connects: list[GlobalConnect] = Field(
        default_factory=list, description="Connections of the pins of the instances to the nets."
    )
    rails: list[PdnRail] = Field(default_factory=list, description="Rails that follow the rows.")
    straps: list[PdnStrap] = Field(default_factory=list, description="Stripes of the grid.")
    connects: list[tuple[str, str]] = Field(
        default_factory=list, description="Pairs of layers that the grid connects with vias."
    )
    pin_layers: list[str] = Field(
        default_factory=list, description="Layers of the pins that connect to the grid."
    )


# ---------------------------------------------------------------------------------------------
# The platform
# ---------------------------------------------------------------------------------------------


class PlatformValues(_Model):
    """The leaves that every layer but the files can give. Every field is optional or empty, and
    a residue file holds exactly these fields under `values`."""

    time_unit: Optional[str] = Field(
        None, description="Time unit of the Liberty file, written as `1ns`."
    )
    capacitance_unit: Optional[str] = Field(
        None, description="Capacitance unit of the Liberty file, written as `1ff`."
    )
    site: Optional[str] = Field(None, description="Name of the placement site of the core cells.")
    tie_high: Optional[CellPort] = Field(None, description="Cell and port that drive a constant 1.")
    tie_low: Optional[CellPort] = Field(None, description="Cell and port that drive a constant 0.")
    min_buffer: Optional[CellPorts] = Field(
        None, description="Smallest buffer, which synthesis uses to insert a buffer."
    )
    abc_driver_cell: Optional[str] = Field(
        None, description="Cell that drives the inputs during technology mapping."
    )
    abc_load_in_ff: Optional[Number] = Field(
        None, description="Load in femtofarads on the outputs during technology mapping."
    )
    dont_use_cells: list[str] = Field(
        default_factory=list,
        description="Cells that synthesis and optimization do not use. A name can use wildcards.",
    )
    fill_cells: list[str] = Field(
        default_factory=list, description="Filler cells that fill the gaps in the rows of cells."
    )
    tap_cell: Optional[str] = Field(None, description="Name of the tap cell.")
    endcap_cell: Optional[str] = Field(None, description="Name of the end cap cell.")
    tapcell: Tapcell = Field(default_factory=Tapcell, description="Placement of the tap cells.")
    antenna_cell: Optional[CellPort] = Field(
        None, description="Antenna diode cell and the port of its input."
    )
    diode_usable: Optional[bool] = Field(
        None, description="Whether the router can insert the antenna diode cell."
    )
    supply: Supply = Field(default_factory=Supply, description="The power supply.")
    routing: Routing = Field(default_factory=Routing, description="The layers of routing.")
    io_layers: IoLayers = Field(default_factory=IoLayers, description="The layers of the I/O pins.")
    tracks: dict[str, Track] = Field(
        default_factory=dict, description="Routing tracks, by layer name."
    )
    wire_rc: WireRc = Field(
        default_factory=WireRc, description="Resistance and capacitance of the wires."
    )
    pdn: Pdn = Field(default_factory=Pdn, description="The power grid over the standard cells.")
    place_density: Optional[Number] = Field(
        None, description="Target density of the global placement, from 0 to 1."
    )
    match_cell_footprint: Optional[bool] = Field(
        None, description="Whether resizing keeps the footprint of a cell."
    )
    opt_post_grt_wns: Optional[Number] = Field(
        None,
        description="Switch for the optimization after global routing (`OPT_POST_GRT_WNS` of "
        "OpenROAD-flow-scripts): any value but 0 runs the polish of the worst slack after global "
        "routing, and 0 skips it, which leaves room for the power recovery.",
    )
    synth_minimum_keep_size: Optional[Count] = Field(
        None, description="Size in cells below which synthesis flattens a module."
    )
    cts_buffers: list[str] = Field(
        default_factory=list, description="Buffer cells that clock tree synthesis uses."
    )
    ir_drop_layer: Optional[str] = Field(
        None, description="Layer on which the analysis of the IR drop measures."
    )


#: The names of `PlatformModel` that are no leaves of `PlatformValues`: no layer but the residue
#: and the user file gives them, and a residue gives them outside of `values`.
NOT_VALUES = ("name", "corner", "files", "sources")


class PlatformModel(PlatformValues):
    """The platform that a flow works with. `sources` says which layer gave each leaf."""

    leaf_keyed: ClassVar[tuple[str, ...]] = ("sources",)

    name: str = Field(description="Name of the platform.")
    corner: Optional[str] = Field(
        None, description="The corner of the PDK, written as the token of the PDK."
    )
    files: dict[str, list[Path]] = Field(
        default_factory=dict,
        description="Absolute paths of the PDK files, by role. The corner is applied.",
    )
    sources: dict[str, Layer] = Field(
        default_factory=dict,
        description="The layer that gave each leaf, by leaf key.",
    )

    def leaf_values(self) -> dict[str, Any]:
        """Return every leaf that has a value, by leaf key, as plain data. A leaf that is `None`
        or empty has none."""
        leaves: dict[str, Any] = {"name": self.name}
        if self.corner is not None:
            leaves["corner"] = self.corner
        for role, paths in self.files.items():
            if paths:
                leaves[f"files.{role}"] = [str(path) for path in paths]
        nested = self.model_dump(mode="json", exclude=set(NOT_VALUES))
        for key, value in flatten_leaves(nested).items():
            if value is not None and value != [] and value != {}:
                leaves[key] = value
        return leaves


@dataclass(frozen=True)
class Located:
    """Where the files of a platform are in a PDK store, as a provider finds them."""

    #: The provider that served the platform: `orfs`, `ciel` or `directory`.
    provider: str
    #: The effective pin of the PDK files.
    version: str
    #: The root of the store.
    root: Path
    #: The directory of the platform in the store.
    directory: Path
    #: The resolved corner, written as the token of the PDK.
    corner: str
    #: Absolute paths of the files, by role. `liberty` holds the files of the corner.
    files: dict[str, list[Path]]
    #: Absolute paths of the upstream configuration files, and of the lists that they name.
    upstream_files: list[Path]
    #: The standard cell library that the provider serves, if its entry names one.
    library: Optional[str] = None


# ---------------------------------------------------------------------------------------------
# Leaf keys
# ---------------------------------------------------------------------------------------------


def is_group(annotation: Any) -> TypeGuard[type[Group]]:
    """Whether a field of this type is a group of leaves."""
    return isinstance(annotation, type) and issubclass(annotation, Group)


def leaf_annotation(key: str) -> Any:
    """Return the type of the value that the leaf key `key` names. Raise `ValueError` for a key
    that names no leaf: an unknown field, a whole group or table, a part of a whole value, a table
    key with a `.`, or one of `NOT_VALUES`."""
    cls: type[_Model] = PlatformValues
    remaining = key
    while True:
        head, _, rest = remaining.partition(".")
        field = cls.model_fields.get(head)
        if field is None:
            raise ValueError(f"`{key}` is no leaf of the platform model: it has no `{head}`")
        annotation = field.annotation
        if is_group(annotation):
            if not rest:
                raise ValueError(f"`{key}` is a group: name one of its fields")
            cls, remaining = annotation, rest
        elif is_table(annotation):
            if not rest:
                raise ValueError(f"`{key}` is a table: name one of its keys (`{key}.<key>`)")
            if "." in rest:
                raise ValueError(f"`{key}` has a `.` in a table key; a table key is a plain name")
            return get_args(annotation)[1]
        else:
            if rest:
                raise ValueError(f"`{key}` is no leaf: `{head}` is one whole value")
            return annotation


def _strict_booleans(annotation: Any) -> Any:
    """Return the annotation with each `bool` strict, so that only `true` and `false` are
    booleans. The model refuses `1` and text for a boolean, and a leaf does the same."""
    if annotation is bool:
        return StrictBool
    arguments = get_args(annotation)
    origin = get_origin(annotation)
    if not arguments or origin is None:
        return annotation
    strict = tuple(_strict_booleans(argument) for argument in arguments)
    if origin is Union or origin is UnionType:
        return Union[strict]  # type: ignore[valid-type]
    return origin[strict]


@cache
def _adapter(annotation: Any) -> TypeAdapter[Any]:
    return TypeAdapter(_strict_booleans(annotation))


def normalize_leaf(key: str, value: Any) -> Any:
    """Return the value of the leaf `key` as plain data in one canonical form: validated as the
    type of the leaf, then written out (a tuple is a list, a model is a table with all its
    fields). Two values mean the same exactly when their normal forms are equal. Raise
    `ValueError` naming the leaf when the value is not of its type."""
    adapter = _adapter(leaf_annotation(key))
    try:
        return adapter.dump_python(adapter.validate_python(value), mode="json")
    except ValidationError as error:
        details = "; ".join(
            (
                ".".join(str(part) for part in problem["loc"]) + f": {problem['msg']}"
                if problem["loc"]
                else str(problem["msg"])
            )
            for problem in error.errors()
        )
        raise ValueError(f"`{key}` has a value of the wrong type ({details})") from None


def flatten_leaves(nested: Mapping[str, Any]) -> dict[str, Any]:
    """Return the leaves of a nested table that mirrors `PlatformValues`, by leaf key. A group is
    entered, a table gives one leaf per key, and any other value is one leaf."""

    def walk(table: Mapping[str, Any], cls: type[_Model], prefix: str) -> dict[str, Any]:
        leaves: dict[str, Any] = {}
        for name, value in table.items():
            field = cls.model_fields.get(name)
            if field is None:
                raise ValueError(f"`{prefix}{name}` is no leaf of the platform model")
            annotation = field.annotation
            if is_group(annotation):
                leaves.update(walk(value, annotation, f"{prefix}{name}."))
            elif is_table(annotation):
                leaves.update({f"{prefix}{name}.{key}": item for key, item in value.items()})
            else:
                leaves[f"{prefix}{name}"] = value
        return leaves

    return walk(nested, PlatformValues, "")


def nest_leaves(leaves: Mapping[str, Any]) -> dict[str, Any]:
    """Return the nested table that the leaves, by leaf key, stand for. It is the inverse of
    `flatten_leaves`."""
    nested: dict[str, Any] = {}
    for key, value in leaves.items():
        *parents, last = key.split(".")
        table = nested
        for part in parents:
            table = table.setdefault(part, {})
        table[last] = value
    return nested
