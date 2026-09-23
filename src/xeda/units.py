"""Physical quantities in settings: clock periods, frequencies, delays.

A quantity is written as a number, optionally followed by one unit: ``5.5``, ``"5.5ns"``,
``"200 MHz"``, ``"1e3 kHz"``. That is the whole grammar. Text is never handed to pint's
expression evaluator, which would make anything it can compute a clock (``"5 ns * 2"``,
``"5..5ns"``, ``"(100)mHz"``) and let malformed text escape as a traceback rather than a field
error. pint is only asked to convert a number from one known unit to another.
"""

import decimal
import math
import numbers
import re
from typing import Any, Optional, Union

from pint import PintError, UnitRegistry

#: Units are case-sensitive, as in SI: "MHz" is megahertz, "mHz" millihertz, "nS" nanosiemens.
#: (A case-insensitive registry left pint to pick among such homographs by hash seed.)
unit_registry: UnitRegistry = UnitRegistry()
Q_: Any = unit_registry.Quantity

#: The time and frequency units clock and timing settings are written in. Another case of one of
#: them is a typo, and is rejected with the right spelling rather than read as whatever it spells
#: in SI: "mHz" is millihertz, "Ms" megaseconds. A unit spelled out ("millihertz") is taken as is.
CLOCK_UNITS = ("fs", "ps", "ns", "us", "ms", "s", "Hz", "kHz", "MHz", "GHz")
_CLOCK_UNIT_BY_FOLDED_CASE = {unit.lower(): unit for unit in CLOCK_UNITS}

#: A number: optional sign, ASCII decimal digits with an optional point, optional exponent.
#: No digit separators, no `nan`/`inf`, no other scripts' digits (all of which `float()` takes).
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
#: One unit name: letters only (including "µ"), so it cannot be an expression.
_UNIT = r"[^\W\d_]+"
#: Both parts are optional here; a value requires the number, a unit specification the unit.
_QUANTITY_RE = re.compile(rf"(?P<number>{_NUMBER})?\s*(?P<unit>{_UNIT})?")


def check_unit_case(unit: str, text: str) -> None:
    """Reject a clock unit written in another case ("mhz", "mHz", "NS"), naming the right one."""
    expected = _CLOCK_UNIT_BY_FOLDED_CASE.get(unit.lower())
    if expected is not None and unit != expected:
        raise ValueError(
            f"unit {unit!r} in {text!r}: units are case-sensitive, did you mean {expected!r}?"
        )


def _unit_name(unit: str, text: str) -> str:
    """The registry's name for `unit`, a single unit name as written in `text`."""
    check_unit_case(unit, text)
    candidates = {prefix + name for prefix, name, _ in unit_registry.parse_unit_name(unit)}
    if len(candidates) == 1:
        return candidates.pop()
    if candidates:
        # pint would take the first and log a warning: "min" is minute or milli-inch.
        raise ValueError(
            f"cannot interpret {text!r}: unit {unit!r} is ambiguous, it could be any of "
            f"{', '.join(sorted(candidates))}; spell it out"
        )
    hint = ""
    if unit.lower() != unit and unit_registry.parse_unit_name(unit.lower()):
        hint = f"; units are case-sensitive, did you mean {unit.lower()!r}?"
    raise ValueError(f"cannot interpret {text!r}: unit {unit!r} is not defined{hint}")


def _parse_unit_spec(spec: Any) -> tuple[float, str]:
    """A unit, optionally preceded by a positive scale: "ns" is (1.0, "nanosecond"), a PDK's
    `time_unit = "10ps"` is (10.0, "picosecond")."""
    m = _QUANTITY_RE.fullmatch(spec.strip()) if isinstance(spec, str) else None
    if m is None or m["unit"] is None:
        raise ValueError(
            f"cannot interpret {spec!r} as a unit: expected a unit name, optionally preceded by "
            "a positive scale, e.g. 'ns' or '10ps'"
        )
    scale = float(m["number"]) if m["number"] else 1.0
    if not (math.isfinite(scale) and scale > 0):
        raise ValueError(f"cannot interpret {spec!r} as a unit: its scale must be positive")
    return scale, _unit_name(m["unit"], spec)


def _example(unit_name: str) -> str:
    """How a quantity in `unit_name` is written, for error messages."""
    if Q_(1.0, unit_name).is_compatible_with("Hz"):
        return "'200 MHz'"
    if Q_(1.0, unit_name).is_compatible_with("s"):
        return "'5 ns'"
    return f"'1 {unit_name}'"


def _parse_value(value: Any, to_name: str) -> tuple[float, str | None]:
    """The magnitude of `value` and the registry name of its unit, or `None` for a bare number."""
    # `bool` is an `int`: `clock.period = true` would be a 1 ns clock.
    if isinstance(value, bool) or not isinstance(value, (str, numbers.Real, decimal.Decimal)):
        raise ValueError(
            f"cannot interpret {value!r} as a quantity: expected a number, or text such as "
            f"{_example(to_name)}, not {type(value).__name__}"
        )
    unit = None
    if isinstance(value, str):
        m = _QUANTITY_RE.fullmatch(value.strip())
        if m is None or m["number"] is None:
            raise ValueError(
                f"cannot interpret {value!r} as a quantity: expected a number, optionally "
                f"followed by a unit, e.g. {_example(to_name)}"
            )
        magnitude = float(m["number"])
        if m["unit"]:
            unit = _unit_name(m["unit"], value)
    else:
        magnitude = float(value)
    if not math.isfinite(magnitude):
        raise ValueError(f"cannot interpret {value!r} as a quantity: it is not a finite number")
    return magnitude, unit


def convert_unit(value: Any, to_unit: str, from_unit: str | None = None) -> float:
    """`value` as a number of `to_unit`.

    `value` is a number, or text holding a number optionally followed by one unit ("5.5",
    "5.5ns", "200 MHz", "1e3 kHz"); a unit it carries is always the one it is in. A bare number
    is in `from_unit` if one is given, and otherwise already in `to_unit`. `to_unit` and
    `from_unit` are a unit optionally preceded by a positive scale ("nanosecond", "1ps").

    Every rejected input is a `ValueError`, so a bad value in a design file is a field error
    rather than a traceback. pint's own exceptions are not: `UndefinedUnitError` derives from
    `AttributeError` and `DimensionalityError` from `TypeError`.
    """
    to_scale, to_name = _parse_unit_spec(to_unit)
    magnitude, unit = _parse_value(value, to_name)
    from_scale = 1.0
    if unit is None:
        if from_unit is None:
            return magnitude
        from_scale, unit = _parse_unit_spec(from_unit)
    try:
        result = float(Q_(magnitude, unit).to(to_name).m)
    except PintError as e:
        raise ValueError(f"cannot interpret {value!r} as a quantity in {to_unit!r}: {e}") from e
    result = result * from_scale / to_scale
    if not math.isfinite(result):
        raise ValueError(
            f"cannot interpret {value!r} as a quantity in {to_unit!r}: out of range ({result})"
        )
    return result


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
