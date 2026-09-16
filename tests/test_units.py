"""Unit-string parsing used by clock periods, frequencies and simulation stop times.

pint resolves some abbreviations ambiguously -- "ns" matches both *nanosecond* and
*nanosiemens*, and which one it picks is not deterministic -- so abbreviations are spelled out
before parsing.
"""

import pytest

from xeda.units import convert_unit, normalize_quantity


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5.5ns", "5.5 nanoseconds"),
        ("100us", "100 microseconds"),
        ("1 ms", "1 milliseconds"),
        ("5ps", "5 picoseconds"),
        ("200MHz", "200 MHz"),
        ("5.5", "5.5"),
        ("", ""),
    ],
)
def test_normalize_quantity(value, expected):
    assert normalize_quantity(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(5.5, 5.5), ("5.5", 5.5), ("5.5ns", 5.5), ("5.5 ns", 5.5), ("0.0055us", 5.5), ("5500ps", 5.5)],
)
def test_time_conversions_are_unambiguous(value, expected):
    """Repeated to catch the nondeterministic pint resolution of "ns"."""
    for _ in range(5):
        assert convert_unit(value, "nanosecond") == pytest.approx(expected)


@pytest.mark.parametrize("value", [200.0, "200", "200MHz", "0.2GHz", "200000kHz"])
def test_frequency_conversions(value):
    assert convert_unit(value, "MHz") == pytest.approx(200.0)
