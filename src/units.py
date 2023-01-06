from typing import Optional, Union
from pint import UnitRegistry

unit_registry = UnitRegistry()
Q_ = unit_registry.Quantity


def convert(
    value,
    target_unit,
    src_unit=None,
) -> float:
    try:
        value = float(value)
    except ValueError:
        pass
    if src_unit and isinstance(value, (float, int)):
        value = Q_(value, src_unit).to(target_unit).m
    elif isinstance(value, str):
        value = Q_(value).to(target_unit).m
    return float(value)


def convert_freq(f: Union[str, float, int], target_unit="MHz") -> float:
    return convert(f, target_unit)


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
    return convert(f, target_unit)


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
