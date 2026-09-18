import re
from typing import Any, Optional, Union

from pint import PintError, UnitRegistry

unit_registry: UnitRegistry = UnitRegistry(case_sensitive=False)
Q_: Any = unit_registry.Quantity

#: Abbreviations that are ambiguous to pint and must be spelled out before parsing.
#: "ns", for instance, resolves to both nanosecond and nanosiemens, and which one pint picks
#: is not deterministic -- so "5.5ns" would sometimes be rejected as a time.
UNIT_ALIASES = {
    "ps": "picoseconds",
    "ns": "nanoseconds",
    "us": "microseconds",
    "ms": "milliseconds",
}

_QUANTITY_RE = re.compile(r"^\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)\s*$")


def unit_maybe_scale(unit: str):
    unit = unit.strip()
    scale = None
    m = re.match(r"(\d*\.?\d*)\s*(\w+)", unit)
    if m:
        sc = m.group(1)
        if sc and sc != "1":
            scale = float(sc)
        unit = UNIT_ALIASES.get(m.group(2), m.group(2))
    return unit, scale


def normalize_quantity(value: str) -> str:
    """Spell out an ambiguous unit abbreviation in a "<number><unit>" string.

    `"5.5ns"` becomes `"5.5 nanoseconds"`; anything that is not a plain number-plus-unit string
    is returned unchanged for pint to parse as-is.
    """
    m = _QUANTITY_RE.match(value)
    if not m:
        return value
    number, unit = m.groups()
    return f"{number} {UNIT_ALIASES.get(unit, unit)}"


def convert_unit(
    value,
    to_unit,
    from_unit=None,
) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError):
        pass
    from_scale = None
    to_scale = None
    if from_unit:
        from_unit, from_scale = unit_maybe_scale(from_unit)
    to_unit, to_scale = unit_maybe_scale(to_unit)
    # pint raises its own exception types -- `UndefinedUnitError` derives from `AttributeError`
    # and `DimensionalityError` from `TypeError`, neither of which pydantic v2 treats as a
    # validation failure. A bad unit in a design file (`clock.period = "5 furlongs"`) would
    # otherwise reach the user as a raw traceback instead of a field error.
    try:
        if from_unit and isinstance(value, (float, int)):
            value = Q_(value, from_unit).to(to_unit).m
        elif isinstance(value, str):
            value = Q_(normalize_quantity(value)).to(to_unit).m
    except PintError as e:
        raise ValueError(f"cannot interpret {value!r} as a quantity in '{to_unit}': {e}") from e
    if from_scale:
        value *= from_scale
    if to_scale:
        value /= to_scale
    return float(value)


def convert_freq(f: Union[str, float, int], target_unit="MHz") -> float:
    return convert_unit(f, target_unit)


def freq_mhz_optional(
    f: Union[str, float, int, None], target_unit="MHz", require_positive=True, optional=True
) -> Optional[float]:
    if f is None:
        if optional:
            return f
        raise ValueError("Value is required")
    f = convert_freq(f, target_unit)
    if require_positive and f <= 0:
        raise ValueError("Value should be positive")
    return f


def convert_time(
    f: Union[str, float, int],
    target_unit="ns",
) -> float:
    return convert_unit(f, target_unit)


def time_ns_optional(
    f: Union[str, float, int, None],
    target_unit="ns",
    require_non_negative=True,
    require_non_zero=False,
    optional=True,
) -> Optional[float]:
    if f is None:
        if optional:
            return f
        raise ValueError("Value is required")
    f = convert_time(f, target_unit)
    if require_non_zero and f == 0:
        raise ValueError("Value should be non-zero")
    if require_non_negative and f < 0:
        raise ValueError("Value should be non-negative")
    return f
