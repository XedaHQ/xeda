import re
from typing import Optional, Union
from pint import UnitRegistry

unit_registry = UnitRegistry(case_sensitive=False)
Q_ = unit_registry.Quantity


def unit_maybe_scale(unit: str):
    unit = unit.strip()
    scale = None
    m = re.match(r"(\d*\.?\d*)\s*(\w+)", unit)
    if m:
        sc = m.group(1)
        if sc and sc != "1":
            scale = float(sc)
        unit = m.group(2)
        if unit == "ps":
            unit = "picoseconds"
        if unit == "ns":
            unit = "nanoseconds"
        if unit == "ms":
            unit = "milliseconds"
        if unit == "us":
            unit = "microseconds"
    return unit, scale


def convert_unit(
    value,
    to_unit,
    from_unit=None,
) -> float:
    try:
        value = float(value)
    except ValueError:
        pass
    from_scale = None
    to_scale = None
    if from_unit:
        from_unit, from_scale = unit_maybe_scale(from_unit)
    to_unit, to_scale = unit_maybe_scale(to_unit)
    if from_unit and isinstance(value, (float, int)):
        value = Q_(value, from_unit).to(to_unit).m
    elif isinstance(value, str):
        value = Q_(value).to(to_unit).m
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
