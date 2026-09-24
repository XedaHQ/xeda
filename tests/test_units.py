"""Unit-string parsing used by clock periods and frequencies.

Units are case-sensitive, as in SI. A case-insensitive registry made pint choose between
homographs by hash seed ("5.5nS": nanoseconds on one run, nanosiemens on the next) and read
"100mhz" as millihertz. A clock unit in the wrong case is now an error naming the right one.

A quantity is exactly a number, optionally followed by one unit. Text used to go to pint's
expression evaluator, so anything it could compute was a clock: "5 ns * 2" was 10 ns, "5..5ns"
2.5 ns, "100.mHz" a 0.1 Hz clock (the number spelling slipped past the case check), and "5_ns"
or "5/0 ns" escaped as a `TokenError` / `ZeroDivisionError` traceback.
"""

import itertools
import math
import os
import subprocess
import sys
from decimal import Decimal
from fractions import Fraction

import pytest

from xeda.flow import FlowSettingsError
from xeda.flows import VivadoSynth
from xeda.units import CLOCK_UNITS, convert_unit

#: Magnitude of one of each clock unit, in nanoseconds or megahertz: given by hand, so the tests
#: do not trust pint to define what they check it against.
NS_PER_UNIT = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}
MHZ_PER_UNIT = {"Hz": 1e-6, "kHz": 1e-3, "MHz": 1.0, "GHz": 1e3}


def _target(unit):
    """Choose the target unit for conversion tests."""
    return ("nanosecond", NS_PER_UNIT[unit]) if unit in NS_PER_UNIT else ("MHz", MHZ_PER_UNIT[unit])


def test_every_clock_unit_has_a_known_magnitude():
    """Every clock unit has a known magnitude."""
    assert set(CLOCK_UNITS) == NS_PER_UNIT.keys() | MHZ_PER_UNIT.keys()


@pytest.mark.parametrize("unit", CLOCK_UNITS)
@pytest.mark.parametrize("space", ["", " "])
def test_a_clock_unit_spelled_right_converts(unit, space):
    """A clock unit spelled right converts."""
    to_unit, factor = _target(unit)
    assert convert_unit(f"2{space}{unit}", to_unit) == pytest.approx(2 * factor)


def _miscased():
    """Generate invalid case variants of clock units."""
    for unit in CLOCK_UNITS:
        for chars in itertools.product(*((c.lower(), c.upper()) for c in unit)):
            if (spelling := "".join(chars)) != unit:
                yield spelling, unit


@pytest.mark.parametrize("spelling,unit", list(_miscased()))
def test_a_clock_unit_in_another_case_is_rejected_with_the_right_spelling(spelling, unit):
    """Including the spellings that are valid SI for something else: "mHz" (millihertz), "Ms"
    (megaseconds), "nS" (nanosiemens) are typos for a clock, not another unit."""
    to_unit, _ = _target(unit)
    with pytest.raises(ValueError, match=f"did you mean '{unit}'"):
        convert_unit(f"2{spelling}", to_unit)


@pytest.mark.parametrize(
    "value,to_unit,expected",
    [
        ("5.5 nanoseconds", "nanosecond", 5.5),
        ("2 µs", "nanosecond", 2000.0),
        ("1 megahertz", "MHz", 1.0),
        ("1000 millihertz", "MHz", 1e-6),  # spelled out, so meant
        (5.5, "nanosecond", 5.5),
        ("5.5", "nanosecond", 5.5),
    ],
)
def test_other_spellings_and_plain_numbers(value, to_unit, expected):
    """Other spellings and plain numbers."""
    assert convert_unit(value, to_unit) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["5 furlongs", "5 MHz"])
def test_a_quantity_that_is_not_a_time_is_a_value_error(value):
    """A quantity that is not a time is a value error."""
    with pytest.raises(ValueError, match="cannot interpret"):
        convert_unit(value, "nanosecond")


def test_parsing_does_not_depend_on_the_hash_seed():
    """The seed is fixed when the interpreter starts, so each one needs its own process."""
    code = (
        "from xeda.units import convert_unit\n"
        "for v, u in [('5.5ns', 'ns'), ('10us', 'ns'), ('2ms', 'ns'), ('100MHz', 'MHz')]:\n"
        "    print(round(convert_unit(v, u), 6))\n"
        "for v in ('5.5nS', '10uS', '2mS', '100mhz'):\n"
        "    try:\n"
        "        convert_unit(v, 'ns')\n"
        "        print('accepted', v)\n"
        "    except ValueError:\n"
        "        print('rejected')\n"
    )
    outputs = set()
    for seed in ("0", "1", "3"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        )
        outputs.add(result.stdout)
    assert outputs == {"5.5\n10000.0\n2000000.0\n100.0\n" + "rejected\n" * 4}


#: Every way a number is written in a quantity: plain, trailing or leading dot, sign, exponent.
NUMBER_SPELLINGS = [("2", 2.0), ("2.", 2.0), ("2.0", 2.0), (".5", 0.5), ("+2", 2.0), ("2e0", 2.0)]
NUMBER_SPELLINGS += [("2E+0", 2.0), ("0.2e1", 2.0), ("200e-2", 2.0)]


@pytest.mark.parametrize("number,magnitude", NUMBER_SPELLINGS)
@pytest.mark.parametrize("space", ["", " "])
@pytest.mark.parametrize("unit", CLOCK_UNITS)
def test_every_number_spelling_converts_with_every_clock_unit(number, magnitude, space, unit):
    """Every number spelling converts with every clock unit."""
    to_unit, factor = _target(unit)
    assert convert_unit(f"{number}{space}{unit}", to_unit) == pytest.approx(magnitude * factor)


@pytest.mark.parametrize("number", [n for n, _ in NUMBER_SPELLINGS])
@pytest.mark.parametrize(
    "spelling,unit", [("mHz", "MHz"), ("nS", "ns"), ("Ms", "ms"), ("KHZ", "kHz")]
)
def test_the_case_check_holds_however_the_number_is_written(number, spelling, unit):
    """`"100.mHz"` was a 0.1 Hz clock: the case check only recognized some number spellings,
    and everything else went straight to pint."""
    to_unit, _ = _target(unit)
    with pytest.raises(ValueError, match=f"did you mean '{unit}'"):
        convert_unit(f"{number}{spelling}", to_unit)


#: Text that is not "<number>[<unit>]": expressions pint used to evaluate, malformed numbers,
#: and a unit with no number. Each is a `ValueError`, never another exception, never a value.
NOT_A_QUANTITY = [
    "5..5ns",  # was 2.5 ns (5. * .5)
    "5 ns * 2",  # was 10 ns
    "100 mHz * 1",  # was a 0.1 Hz clock, past the case check
    "(100)mHz",  # likewise
    "5 ns / 2",
    "2 * 5 ns",
    "5**ns",
    "1/ns",  # a frequency, written as an expression
    "5 [ns]",
    "5 = ns",
    "5 ns)",
    "5ns ns",
    "5 MHz MHz",
    "5_ns",  # was a TokenError traceback
    "(",  # was a TokenError traceback
    "5/0 ns",  # was a ZeroDivisionError traceback
    "5 +",  # was an AssertionError traceback
    "",
    " ",
    "ns",  # a unit with no number was 1 ns
    "e",
    "0x10 ns",
    "1_000",
    "1_000 ns",
    "5 ns; import os",
    "- 5 ns",
    "5e ns",
    "\u0665 ns",  # ARABIC-INDIC DIGIT FIVE: float() accepts it, a quantity does not
]


@pytest.mark.parametrize("value", NOT_A_QUANTITY)
@pytest.mark.parametrize("to_unit", ["nanosecond", "MHz"])
def test_text_that_is_not_a_number_and_a_unit_is_a_value_error(value, to_unit):
    """Text that is not a number and a unit is a value error."""
    with pytest.raises(ValueError, match="cannot interpret"):
        convert_unit(value, to_unit)


@pytest.mark.parametrize(
    "to_unit,example", [("nanosecond", "'5 ns'"), ("ps", "'5 ns'"), ("MHz", "'200 MHz'")]
)
def test_the_error_shows_the_expected_form(to_unit, example):
    """The error shows the expected form."""
    with pytest.raises(ValueError, match=f"expected a number.*e.g. {example}"):
        convert_unit("5_ns", to_unit)


NON_FINITE = ["nan", "inf", "-inf", "NaN", "Infinity", "nan ns", "inf ns", "inf MHz", "1e400 ns"]
NON_FINITE += [math.nan, math.inf, -math.inf, "1e308 s"]  # the last overflows only in ns


@pytest.mark.parametrize("value", NON_FINITE, ids=repr)
@pytest.mark.parametrize("to_unit", ["nanosecond", "MHz"])
def test_a_non_finite_value_is_a_value_error(value, to_unit):
    """A non finite value is a value error."""
    with pytest.raises(ValueError, match="cannot interpret"):
        convert_unit(value, to_unit)


@pytest.mark.parametrize("value", [True, False], ids=repr)
@pytest.mark.parametrize("from_unit", [None, "picosecond"])
def test_a_bool_is_not_a_quantity(value, from_unit):
    """`clock.period = true` was a 1 ns clock: `float(True)` is 1.0."""
    with pytest.raises(ValueError, match="cannot interpret"):
        convert_unit(value, "nanosecond", from_unit=from_unit)


@pytest.mark.parametrize("value", [None, [], {}, [5], b"5 ns", 1 + 0j], ids=repr)
def test_any_other_type_is_a_value_error(value):
    """Any other type is a value error."""
    with pytest.raises(ValueError, match="cannot interpret"):
        convert_unit(value, "nanosecond")


@pytest.mark.parametrize(
    "value,expected", [(5, 5.0), (5.5, 5.5), (Decimal("5.5"), 5.5), (Fraction(11, 2), 5.5)]
)
def test_a_number_is_taken_in_the_target_unit(value, expected):
    """A number is taken in the target unit."""
    result = convert_unit(value, "nanosecond")
    assert result == expected and type(result) is float


@pytest.mark.parametrize(
    "value,spelling", [("5 Nanoseconds", "nanoseconds"), ("200 MegaHertz", "megahertz")]
)
def test_a_spelled_out_unit_in_another_case_names_the_right_spelling(value, spelling):
    """A spelled out unit in another case names the right spelling."""
    with pytest.raises(ValueError, match=f"not defined.*did you mean '{spelling}'"):
        convert_unit(value, "MHz" if "Hertz" in value else "nanosecond")


def test_an_ambiguous_unit_is_rejected():
    """pint reads "min" as minute *or* milli-inch, and picks one with only a log warning."""
    with pytest.raises(ValueError, match="ambiguous"):
        convert_unit("5 min", "nanosecond")


# --- unit specifications: `to_unit` / `from_unit`, e.g. a PDK's `time_unit = "1ps"` ----------


@pytest.mark.parametrize(
    "value,to_unit,from_unit,expected",
    [
        (5.0, "1ps", "nanosecond", 5000.0),
        (5.0, "10ps", "nanosecond", 500.0),
        (5.0, "1 ps", "ns", 5000.0),
        (5000, "nanoseconds", "1ps", 5.0),
        (500, "nanoseconds", "10ps", 5.0),
        ("5000", "nanosecond", "picosecond", 5.0),
        (5.0, "picosecond", "0.5ns", 2500.0),
    ],
)
def test_a_unit_specification_may_carry_a_scale(value, to_unit, from_unit, expected):
    """A unit specification may carry a scale."""
    assert convert_unit(value, to_unit, from_unit=from_unit) == pytest.approx(expected)


def test_a_quantity_string_keeps_its_own_unit_whatever_from_unit_says():
    """`from_unit` is the unit of a bare number. Its scale used to be applied to a quantity
    string too, so "5 ns" from a "10ps" `from_unit` came out as 50 ns."""
    assert convert_unit("5 ns", "ns", from_unit="10ps") == pytest.approx(5.0)
    assert convert_unit("5 ns", "10ps", from_unit="1ns") == pytest.approx(500.0)


@pytest.mark.parametrize("spec", ["ns * 2", "1/ns", "ns ns", "", " ", "0ps", "-1ps", "5", "nan"])
def test_a_malformed_unit_specification_is_a_value_error(spec):
    """A scale of 0 used to be ignored, and "ns * 2" silently read as "ns"."""
    with pytest.raises(ValueError, match="unit"):
        convert_unit(5.0, spec, from_unit="nanosecond")
    with pytest.raises(ValueError, match="unit"):
        convert_unit(5.0, "nanosecond", from_unit=spec)


@pytest.mark.parametrize(
    "spec,unit,other", [("1PS", "ps", "ns"), ("1 NS", "ns", "ps"), ("MHZ", "MHz", "GHz")]
)
def test_a_unit_specification_is_case_checked(spec, unit, other):
    """A PDK's `time_unit = "1PS"` is petasiemens to pint, not picoseconds."""
    with pytest.raises(ValueError, match=f"did you mean '{unit}'"):
        convert_unit(5.0, spec, from_unit=other)
    with pytest.raises(ValueError, match=f"did you mean '{unit}'"):
        convert_unit(5.0, other, from_unit=spec)


# --- the same inputs, where a user writes them: a flow's clock settings ------------------------

FPGA_PART = {"part": "xc7a12tcpg238-1"}


@pytest.mark.parametrize(
    "clock",
    [
        {"freq": "100.mHz"},
        {"freq": "100 mHz * 1"},
        {"freq": "(100)mHz"},
        {"period": "5..5ns"},
        {"period": "5 ns * 2"},
        {"period": "5_ns"},
        {"period": "("},
        {"period": "5/0 ns"},
        {"period": True},
        {"freq": True},
        {"period": "nan ns"},
        {"period": "inf ns"},
        {"period": math.nan},
        {"freq": "inf MHz"},
    ],
    ids=repr,
)
def test_a_bad_clock_setting_is_a_flow_settings_error(clock):
    """A bad clock setting is a flow settings error."""
    with pytest.raises(FlowSettingsError):
        VivadoSynth.Settings.from_input({"fpga": FPGA_PART, "clock": clock})


@pytest.mark.parametrize(
    "clock,period",
    [
        ({"freq": "200 MHz"}, 5.0),
        ({"freq": "100MHz"}, 10.0),
        ({"freq": "0.2GHz"}, 5.0),
        ({"freq": "1e2 MHz"}, 10.0),
        ({"period": "5.5ns"}, 5.5),
        ({"period": "5.5"}, 5.5),
        ({"period": 5}, 5.0),
        ({"period": "0.0055us"}, 5.5),
        ({"period": "5500ps"}, 5.5),
    ],
    ids=repr,
)
def test_every_documented_clock_spelling_still_works(clock, period):
    """Every documented clock spelling still works."""
    settings = VivadoSynth.Settings.from_input({"fpga": FPGA_PART, "clock": clock})
    assert settings.clocks["main_clock"].period == pytest.approx(period)
