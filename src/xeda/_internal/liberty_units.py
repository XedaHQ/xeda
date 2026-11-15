"""The time unit of a Liberty library, and times in that unit.

A Liberty file states its time unit as a scale of 1, 10 or 100 and a unit of time (`1ns`,
`10ps`). The tools read their commands and write their reports in this unit.
"""

import re

from .units import Time

#: The time unit of a Liberty file as OpenSTA reads it: 1, 10 or 100 and a unit, without spaces.
#: OpenSTA warns about another multiplier and keeps the unit alone, and it reads a unit with a
#: space or a fraction as one second.
_OPENSTA_TIME_UNIT = re.compile(r"(?P<scale>1|10|100)(?P<unit>fs|ps|ns|us|ms|s)", re.IGNORECASE)

#: The scales and the units of a time unit, in the order of their size.
SCALES = (1, 10, 100)
UNITS = ("fs", "ps", "ns", "us", "ms", "s")


def liberty_time_unit_seconds(unit: str) -> float:
    """Return the time unit of a Liberty file (`1ns`, `10ps`) in seconds, as OpenSTA reads it.

    Raise `ValueError` for a spelling that OpenSTA reads as another unit, such as `1000ps`
    (OpenSTA reads `1ps`), `1.0ns` or `1 ns` (OpenSTA reads one second)."""
    found = _OPENSTA_TIME_UNIT.fullmatch(unit)
    if found is None:
        raise ValueError(
            f"the time unit {unit!r} is no unit that OpenSTA reads: write 1, 10 or 100 and fs, "
            "ps, ns, us, ms or s, without spaces (`1ns`, `10ps`)"
        )
    return int(found["scale"]) * Time.of(1, found["unit"].lower()).to("s")


def liberty_time_unit(unit: Time) -> tuple[int, str]:
    """Return the time `unit` as the scale and the unit of a Liberty file: `1 ps` is `(1, "ps")`
    and `0.01 ns` is `(10, "ps")`.

    Raise `ValueError` if no Liberty file can have it."""
    for name in UNITS:
        for scale in SCALES:
            if unit.to(name) == scale:
                return scale, name
    raise ValueError(
        f"{unit} is no time unit of a Liberty library: a library has 1, 10 or 100 of fs, ps, ns, "
        "us, ms or s, such as `1 ps`, `10 ps` or `1 ns`"
    )


def liberty_time_unit_label(unit: Time) -> str:
    """Return the time unit as text: `1 ps`, `10 ps`. A time that is no unit of a Liberty library
    is the text of the quantity."""
    try:
        scale, name = liberty_time_unit(unit)
    except ValueError:
        return str(unit)
    return f"{scale} {name}"


def to_library_time(time: Time, unit: Time) -> float:
    """Return `time` as a number of the Liberty time `unit`: 5 ns is `5000.0` in `1 ps` and
    `500.0` in `10 ps`. The result has nine decimals at most."""
    scale, name = liberty_time_unit(unit)
    return round(time.to(name) / scale, 9)


def from_library_time(value: float, unit: Time) -> float:
    """Return a number of the Liberty time `unit` in nanoseconds: `5000` in `1 ps` is `5.0`."""
    scale, name = liberty_time_unit(unit)
    return Time.of(value * scale, name).to("ns")
