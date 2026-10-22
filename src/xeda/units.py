"""Physical quantities in settings: clock periods, frequencies, delays.

This paragraph describes the helpers (`convert_unit` and the others) that the settings without a
quantity type use (`NOT_MIGRATED_YET` in `tests/test_physical_quantities.py` lists them). They read a number, optionally followed by one unit: `5.5`, `"5.5ns"`,
`"200 MHz"`, `"1e3 kHz"`. That is the whole grammar. Text never reaches the expression evaluator
of pint. The evaluator would accept anything it can compute as a clock (`"5 ns * 2"`, `"5..5ns"`,
`"(100)mHz"`), and malformed text would escape as a traceback instead of a field error. Xeda
asks pint only to convert a number from one known unit to another.

The setting types of flows are the classes `Time`, `Frequency`, `Capacitance`, `Length`, `Voltage`,
`Resistance` and `Power`. Three more kinds, `Temperature`, `TemperatureDifference` and
`ThermalResistance`, are the units of metrics, and no setting takes them. A value of such a type
is a number with a unit, written as text (`10 ns`). A bare number is refused: it does not say what it measures. A quantity keeps its value
in the canonical unit of its kind, exactly as the text says it, so `10000 ps`, `0.01 us` and
`10 ns` are one value. It has no arithmetic and no `float()`. Code that needs a number asks for
the unit it wants: `period.to("ns")`.
"""

import decimal
import math
import numbers
import re
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any, ClassVar, NoReturn, Optional, Self, Union

from pint import PintError, UnitRegistry
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import PydanticCustomError, core_schema

#: Units are case-sensitive, as in SI: "MHz" is megahertz, "mHz" millihertz, "nS" nanosiemens.
#: (A case-insensitive registry left pint to pick among such homographs by hash seed.)
unit_registry: UnitRegistry = UnitRegistry()
Q_: Any = unit_registry.Quantity

#: The time and frequency units that clock and timing settings use. A unit in another letter case
#: is a typo. It is rejected, and the message names the right spelling, because SI reads it as
#: another unit: "mHz" is millihertz, "Ms" megaseconds. A unit spelled out ("millihertz") is taken
#: as is.
CLOCK_UNITS = ("fs", "ps", "ns", "us", "ms", "s", "Hz", "kHz", "MHz", "GHz")
_CLOCK_UNIT_BY_FOLDED_CASE = {unit.lower(): unit for unit in CLOCK_UNITS}

#: A number: optional sign, ASCII decimal digits with an optional point, optional exponent.
#: No digit separators, no `nan`/`inf`, no other scripts' digits (all of which `float()` takes).
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
#: One unit name: letters only (including the micro sign, U+00B5), so it cannot be an expression.
_UNIT = r"[^\W\d_]+"
#: Both parts are optional here; a value requires the number, a unit specification the unit.
_QUANTITY_RE = re.compile(rf"(?P<number>{_NUMBER})?\s*(?P<unit>{_UNIT})?")
#: The unit names of the kinds of metrics: `delta_degC` (two words joined by `_`) and `K/W` (a
#: ratio of two units). A value of a quantity class reads them. The helpers for settings do not.
_COMPOUND_UNIT = rf"{_UNIT}(?:_{_UNIT})?(?:/{_UNIT})?"
_VALUE_RE = re.compile(rf"(?P<number>{_NUMBER})?\s*(?P<unit>{_COMPOUND_UNIT})?")


def check_unit_case(unit: str, text: str) -> None:
    """Reject a clock unit written in another case ("mhz", "mHz", "NS"), naming the right one."""
    expected = _CLOCK_UNIT_BY_FOLDED_CASE.get(unit.lower())
    if expected is not None and unit != expected:
        raise ValueError(
            f"unit {unit!r} in {text!r}: units are case-sensitive, did you mean {expected!r}?"
        )


def _unit_name(unit: str, text: str) -> str:
    """Return the registry's name for `unit`, a single unit name as written in `text`."""
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
    """Parse a unit, optionally preceded by a positive scale.

    "ns" is (1.0, "nanosecond"), and a PDK's `time_unit = "10ps"` is (10.0, "picosecond")."""
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
    """Return an example of how to write a quantity in `unit_name`, for error messages."""
    if Q_(1.0, unit_name).is_compatible_with("Hz"):
        return "'200 MHz'"
    if Q_(1.0, unit_name).is_compatible_with("s"):
        return "'5 ns'"
    return f"'1 {unit_name}'"


def _parse_value(value: Any, to_name: str) -> tuple[float, str | None]:
    """Return the magnitude of `value` and the registry name of its unit (`None` if bare)."""
    # `bool` is an `int`: `clock.period: true` would be a 1 ns clock.
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
    """Convert `value` to a number of `to_unit`.

    `value` is a number, or text holding a number optionally followed by one unit ("5.5",
    "5.5ns", "200 MHz", "1e3 kHz"); a unit it carries is always the one it is in. A bare number
    is in `from_unit` if one is given, and otherwise already in `to_unit`. `to_unit` and
    `from_unit` are a unit optionally preceded by a positive scale ("nanosecond", "1ps").

    Every rejected input is a `ValueError`, so a bad value in a design file is a field error
    instead of a traceback. pint's own exceptions are not `ValueError`s: `UndefinedUnitError`
    derives from `AttributeError` and `DimensionalityError` from `TypeError`.
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


# ---- quantities with a required unit ---------------------------------------------------------

#: The key of the JSON Schema of a quantity that names its kind (`time`).
QUANTITY_KEY = "x-xeda-quantity"


class QuantityError(ValueError):
    """The text or number that a quantity was asked to hold is not a valid quantity of the kind.
    The message says what is wrong and what to write."""


class _OutOfRange(Exception):
    """An exact value does not fit a float. `large` tells whether it is too large or too small."""

    def __init__(self, large: bool) -> None:
        super().__init__("too large" if large else "too small")
        self.large = large


#: A value whose decimal order of magnitude is beyond this cannot be a float, whatever the unit
#: factor (the largest factor is 10**13). The exact computation is skipped for such a value, so a
#: text like `1e99999999999999999999 ns` costs nothing.
_ORDER_LIMIT = 400
_NUMBER_PARTS = re.compile(r"([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]+))?")
_Decimal3 = tuple[bool, str, int]


def _digits_of_text(number: str) -> _Decimal3:
    """Return the sign, the significant digits and the decimal exponent of a number text of the
    `_NUMBER` grammar. The value is `(-1)**sign * int(digits) * 10**exponent`."""
    m = _NUMBER_PARTS.fullmatch(number)
    assert m is not None, number
    sign, whole, fraction, exponent = m.groups()
    fraction = fraction or ""
    digits = (whole + fraction).lstrip("0")
    return sign == "-", digits, int(exponent or 0) - len(fraction)


def _digits_of_number(value: Union[int, float, Decimal]) -> _Decimal3:
    """Return the sign, digits and exponent of a finite number. A float counts as its shortest
    decimal text (`repr`), the decimal that a person would read from it."""
    exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    sign, digit_tuple, exponent = exact.as_tuple()
    assert isinstance(exponent, int)
    return bool(sign), "".join(map(str, digit_tuple)).lstrip("0"), exponent


def _exact_float(parts: _Decimal3, scale: Fraction) -> float:
    """Return `value * scale` rounded once to the nearest float, with no earlier rounding.

    `Decimal` arithmetic would round to the precision of the active context, and float
    multiplication rounds at each step. A `Fraction` is exact, and `float(Fraction)` rounds
    correctly. A zero value is `0.0`, never `-0.0`. A nonzero value that rounds to zero, and one
    that is too large for a float, raise `_OutOfRange`."""
    negative, digits, exponent = parts
    if not digits:
        return 0.0
    if not -_ORDER_LIMIT <= len(digits) + exponent <= _ORDER_LIMIT:
        raise _OutOfRange(len(digits) + exponent > 0)
    number = int(digits)
    value = Fraction(number * 10**exponent) if exponent >= 0 else Fraction(number, 10**-exponent)
    try:
        result = float((-value if negative else value) * scale)
    except OverflowError:
        raise _OutOfRange(True) from None
    if result == 0.0:
        raise _OutOfRange(False)
    return result


class Quantity:
    """A number with a unit: the value of a setting that measures something.

    A subclass names the kind (`Time`), the canonical unit and the units it accepts. The text
    that a quantity reads is one number, optional spaces and one of those units, and nothing else
    (`10 ns`, `2.5e3 ps`, `100 MHz`). Units are case-sensitive.

    The object is immutable and holds its value exactly in the canonical unit as one float. It is
    no number: it has no arithmetic, no `float()` and no truth value. Compare it with a quantity
    of the same kind, or read a number in a unit with `to`. Build a computed value with `of`.
    The text of the value in its canonical unit (`str(q)`) reads back as the same quantity.
    """

    __slots__ = ("_magnitude",)

    #: The name of the kind: "time".
    kind: ClassVar[str]
    #: The unit of `magnitude`: "ns".
    canonical_unit: ClassVar[str]
    #: The accepted units, canonical one included, in the order that messages list them.
    units: ClassVar[tuple[str, ...]]
    #: Text of a valid value of the kind, for messages: "10 ns".
    example: ClassVar[str]
    _factors: ClassVar[Mapping[str, Fraction]]
    _by_folded_case: ClassVar[Mapping[str, str]]
    _kind_of_unit: ClassVar[dict[str, type["Quantity"]]] = {}

    _magnitude: float

    def __init_subclass__(
        cls,
        *,
        kind: str,
        canonical_unit: str,
        factors: Mapping[str, Fraction],
        example: str,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        assert factors[canonical_unit] == 1
        cls.kind = kind
        cls.canonical_unit = canonical_unit
        cls.units = tuple(factors)
        cls.example = example
        cls._factors = dict(factors)
        cls._by_folded_case = {unit.casefold(): unit for unit in factors}
        assert len(cls._by_folded_case) == len(factors), f"{kind} units differ only in case"
        for unit in factors:
            assert unit not in Quantity._kind_of_unit, f"unit {unit} is in two kinds"
            Quantity._kind_of_unit[unit] = cls

    # ---- reading -------------------------------------------------------------------------

    def __init__(self, value: Union[str, Self]) -> None:
        """Read `value`, text such as `10 ns` or a quantity of this type, or raise
        `QuantityError`."""
        object.__setattr__(self, "_magnitude", type(self)._read(value))

    @classmethod
    def _need_kind(cls) -> None:
        if "kind" not in vars(cls):
            raise TypeError(f"{cls.__name__} is no kind of quantity: use Time, Frequency, ...")

    @classmethod
    def _factor(cls, unit: str, text: str) -> Fraction:
        """Return the value of one `unit` in the canonical unit. `text` is the text that held
        the unit, for the message."""
        cls._need_kind()
        factor = cls._factors.get(unit)
        if factor is not None:
            return factor
        right = cls._by_folded_case.get(unit.casefold())
        if right is not None and "\u03bc" in unit:
            # The Greek letter mu looks like the micro sign, so the message names both codes.
            plain = right.replace("\u00b5", "u")
            raise QuantityError(
                f"unit `{unit}` in `{text}` has the Greek letter mu (U+03BC): did you mean "
                f"`{right}` (the micro sign, U+00B5) or `{plain}`?"
            )
        if right is not None:
            raise QuantityError(
                f"unit `{unit}` in `{text}`: units are case-sensitive, did you mean `{right}`?"
            )
        other = Quantity._kind_of_unit.get(unit)
        if other is not None:
            raise QuantityError(f"`{text}` is a {other.kind}, not a {cls.kind}")
        raise QuantityError(
            f"unknown unit `{unit}` in `{text}`: a {cls.kind} takes one of {', '.join(cls.units)}"
        )

    @classmethod
    def _out_of_range(cls, error: _OutOfRange, text: str) -> QuantityError:
        reason = "it does not fit a floating-point number" if error.large else "it rounds to zero"
        return QuantityError(f"`{text}` is {error}: {reason}")

    @classmethod
    def _read(cls, value: Any) -> float:
        """Return the canonical magnitude of `value`: text or an instance of this type."""
        cls._need_kind()
        if isinstance(value, cls):
            return value._magnitude
        if isinstance(value, Quantity):
            raise QuantityError(f"`{value}` is a {value.kind}, not a {cls.kind}")
        if isinstance(value, (bool, int, float, Decimal)):
            raise QuantityError(
                f"`{value}` needs a unit, such as `{cls.example}`: a bare number does not mean "
                f"anything"
            )
        if not isinstance(value, str):
            raise QuantityError(
                f"expected a {cls.kind}, such as `{cls.example}`, not {type(value).__name__}"
            )
        text = value.strip()
        m = _VALUE_RE.fullmatch(text)
        if m is None or m["number"] is None:
            raise QuantityError(
                f"cannot read `{value}` as a {cls.kind}: write one number and one unit, such as "
                f"`{cls.example}`"
            )
        if m["unit"] is None:
            raise QuantityError(
                f"`{text}` needs a unit, such as `{cls.example}`: a bare number does not mean "
                f"anything"
            )
        factor = cls._factor(m["unit"], text)
        try:
            return _exact_float(_digits_of_text(m["number"]), factor)
        except _OutOfRange as e:
            raise cls._out_of_range(e, text) from None
        except ValueError:  # `int()` refuses a number of thousands of digits
            raise QuantityError(f"`{text[:40]}...` has too many digits") from None

    @classmethod
    def _from_magnitude(cls, magnitude: float) -> Self:
        obj = object.__new__(cls)
        object.__setattr__(obj, "_magnitude", magnitude)
        return obj

    @classmethod
    def of(cls, magnitude: Union[int, float, Decimal], unit: str) -> Self:
        """Return the quantity of `magnitude` in `unit`, for a computed value.

        `magnitude` is a finite `int`, `float` or `Decimal`. A float counts as the shortest
        decimal that reads back as it, so `Time.of(x, "ns").magnitude == x` for every finite `x`.
        `unit` is one of `units`. Text and a `bool` are refused."""
        cls._need_kind()
        if isinstance(magnitude, bool) or not isinstance(magnitude, (int, float, Decimal)):
            raise QuantityError(
                f"expected a number of {unit}, not {type(magnitude).__name__}; "
                f"for text write `{cls.example}`"
            )
        finite = (
            magnitude.is_finite() if isinstance(magnitude, Decimal) else math.isfinite(magnitude)
        )
        if not finite:
            raise QuantityError(f"`{magnitude}` {unit} is not a finite number")
        factor = cls._factor(unit, unit)
        shown = f"{magnitude} {unit}"
        try:
            return cls._from_magnitude(_exact_float(_digits_of_number(magnitude), factor))
        except _OutOfRange as e:
            raise cls._out_of_range(e, shown) from None
        except (ValueError, OverflowError):
            raise QuantityError(f"`{shown[:40]}...` has too many digits") from None

    # ---- reading the value ---------------------------------------------------------------

    @property
    def magnitude(self) -> float:
        """The value in the canonical unit."""
        return self._magnitude

    def to(self, unit: str) -> float:
        """Return the value in `unit`, one of `units`, rounded once from the exact value."""
        factor = self._factor(unit, unit)
        try:
            return _exact_float(_digits_of_number(self._magnitude), 1 / factor)
        except _OutOfRange as e:
            raise self._out_of_range(e, f"{self} in {unit}") from None

    def as_json_value(self) -> str:
        """Return the canonical text: the shortest text of the magnitude that reads back as it,
        a space and the canonical unit (`10 ns`, `2.5 ns`, `1e-05 ns`)."""
        number = repr(self._magnitude)
        return f"{number.removesuffix('.0')} {self.canonical_unit}"

    # ---- the value object ----------------------------------------------------------------

    def __str__(self) -> str:
        return self.as_json_value()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.as_json_value()!r})"

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"a {self.kind} is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError(f"a {self.kind} is immutable")

    def __bool__(self) -> NoReturn:
        raise TypeError(
            f"a {self.kind} has no truth value: test `is not None`, or compare it with another "
            f"{self.kind}"
        )

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, Quantity)
        return self._magnitude == other._magnitude

    def __hash__(self) -> int:
        return hash((self.kind, self._magnitude))

    def __lt__(self, other: Any) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._magnitude < other._magnitude

    def __le__(self, other: Any) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._magnitude <= other._magnitude

    def __gt__(self, other: Any) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._magnitude > other._magnitude

    def __ge__(self, other: Any) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._magnitude >= other._magnitude

    def __reduce__(self) -> tuple[Any, ...]:
        return type(self), (self.as_json_value(),)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: Any) -> Self:
        return self

    # ---- pydantic ------------------------------------------------------------------------

    @classmethod
    def _validate(cls, value: Any) -> Self:
        if isinstance(value, cls):
            return value
        try:
            return cls._from_magnitude(cls._read(value))
        except QuantityError as e:
            raise PydanticCustomError("quantity", "{message}", {"message": str(e)}) from None

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        cls._need_kind()
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                Quantity.as_json_value, when_used="json"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, Any]:
        return {
            "type": "string",
            QUANTITY_KEY: cls.kind,
            "description": (
                f"A {cls.kind} with a unit, such as `{cls.example}`. "
                f"Units: {', '.join(cls.units)}."
            ),
        }


def _factors(**units: Union[int, Fraction]) -> dict[str, Fraction]:
    return {unit: Fraction(factor) for unit, factor in units.items()}


def _power(exponent: int) -> Fraction:
    return Fraction(10) ** exponent


class Time(
    Quantity,
    kind="time",
    canonical_unit="ns",
    example="10 ns",
    factors={
        "fs": _power(-6),
        "ps": _power(-3),
        "ns": Fraction(1),
        "us": _power(3),
        "\u00b5s": _power(3),
        "ms": _power(6),
        "s": _power(9),
        "min": 60 * _power(9),
        "h": 3600 * _power(9),
    },
):
    """A duration. The canonical unit is `ns`. The micro sign spelling `\u00b5s` (U+00B5) is
    accepted too."""

    __slots__ = ()


class Frequency(
    Quantity,
    kind="frequency",
    canonical_unit="MHz",
    example="100 MHz",
    factors={"Hz": _power(-6), "kHz": _power(-3), "MHz": Fraction(1), "GHz": _power(3)},
):
    """A frequency. The canonical unit is `MHz`."""

    __slots__ = ()


class Capacitance(
    Quantity,
    kind="capacitance",
    canonical_unit="fF",
    example="10 fF",
    factors={"fF": Fraction(1), "pF": _power(3), "nF": _power(6)},
):
    """A capacitance. The canonical unit is `fF`."""

    __slots__ = ()


class Length(
    Quantity,
    kind="length",
    canonical_unit="um",
    example="5 um",
    factors={"nm": _power(-3), "um": Fraction(1), "\u00b5m": Fraction(1), "mm": _power(3)},
):
    """A length. The canonical unit is `um`. The micro sign spelling `\u00b5m` (U+00B5) is
    accepted too."""

    __slots__ = ()


class Voltage(
    Quantity,
    kind="voltage",
    canonical_unit="V",
    example="0.9 V",
    factors={"mV": _power(-3), "V": Fraction(1)},
):
    """A voltage. The canonical unit is `V`."""

    __slots__ = ()


class Resistance(
    Quantity,
    kind="resistance",
    canonical_unit="ohm",
    example="50 ohm",
    factors={"ohm": Fraction(1), "kohm": _power(3), "Mohm": _power(6)},
):
    """A resistance. The canonical unit is `ohm`."""

    __slots__ = ()


class Power(
    Quantity,
    kind="power",
    canonical_unit="W",
    example="2 mW",
    factors={
        "nW": _power(-9),
        "uW": _power(-6),
        "\u00b5W": _power(-6),
        "mW": _power(-3),
        "W": Fraction(1),
    },
):
    """A power. The canonical unit is `W`. The micro sign spelling `\u00b5W` (U+00B5) is accepted
    too."""

    __slots__ = ()


class Temperature(
    Quantity,
    kind="temperature",
    canonical_unit="degC",
    example="85 degC",
    factors={"degC": Fraction(1)},
):
    """A temperature on the Celsius scale. The canonical unit is `degC`.

    A metric has this kind. A temperature difference has its own kind, `TemperatureDifference`,
    because 5 `degC` is 278.15 K and 5 `delta_degC` is 5 K."""

    __slots__ = ()


class TemperatureDifference(
    Quantity,
    kind="temperature difference",
    canonical_unit="delta_degC",
    example="5 delta_degC",
    factors={"delta_degC": Fraction(1), "K": Fraction(1)},
):
    """A difference of two temperatures. The canonical unit is `delta_degC`, and 1 `delta_degC` is
    1 K. A metric has this kind."""

    __slots__ = ()


class ThermalResistance(
    Quantity,
    kind="thermal resistance",
    canonical_unit="K/W",
    example="2.5 K/W",
    factors={"K/W": Fraction(1)},
):
    """A thermal resistance. The canonical unit is `K/W`. A metric has this kind."""

    __slots__ = ()


class Bounds:
    """Limits on a quantity, as metadata of an annotation:

        period: Annotated[Time, Bounds(gt="0 ns")]
        delay: Union[Literal["auto"], Annotated[Time, Bounds(ge="0 ns")], None] = None

    `gt`, `ge`, `lt` and `le` are text of the annotated kind. A bound of another kind, or one
    that is no valid quantity, fails when the model class is built. (`Field(gt=...)` cannot
    bound a quantity.)"""

    __slots__ = ("limits",)

    _WORDS = {
        "gt": "greater than",
        "ge": "at least",
        "lt": "less than",
        "le": "at most",
    }

    def __init__(
        self,
        *,
        gt: Optional[str] = None,
        ge: Optional[str] = None,
        lt: Optional[str] = None,
        le: Optional[str] = None,
    ) -> None:
        limits = {"gt": gt, "ge": ge, "lt": lt, "le": le}
        self.limits = {op: text for op, text in limits.items() if text is not None}
        if not self.limits:
            raise TypeError("Bounds needs at least one of gt, ge, lt, le")
        for op, text in self.limits.items():
            if not isinstance(text, str):
                raise TypeError(f"the bound {op} is unit text such as '0 ns', not {text!r}")

    def __repr__(self) -> str:
        return "Bounds(" + ", ".join(f"{op}={text!r}" for op, text in self.limits.items()) + ")"

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        if not (isinstance(source, type) and issubclass(source, Quantity)):
            raise TypeError(f"{self!r} bounds a quantity, not {source!r}")
        limits = [(op, source(text)) for op, text in self.limits.items()]

        def check(value: Any) -> Any:
            for op, bound in limits:
                ok = {
                    "gt": value > bound,
                    "ge": value >= bound,
                    "lt": value < bound,
                    "le": value <= bound,
                }[op]
                if not ok:
                    raise PydanticCustomError(
                        "quantity",
                        "{message}",
                        {"message": f"must be {self._WORDS[op]} {bound}, got {value}"},
                    )
            return value

        return core_schema.no_info_after_validator_function(check, handler(source))


# ---- templates ------------------------------------------------------------------------------


def to_filter(value: Any, unit: str) -> float:
    """The template filter `to`: `{{ clock.period | to("ns") }}` is the number of nanoseconds.
    Only a quantity has a unit to convert."""
    if not isinstance(value, Quantity):
        raise TypeError(
            f"the `to` filter needs a quantity, such as `10 ns`, not {type(value).__name__} "
            f"`{value}`"
        )
    return value.to(unit)


def finalize_template_value(value: Any) -> Any:
    """Return `value` for a template to render. A quantity is refused: its text has a unit,
    which a script rarely expects, so the template says which unit it wants."""
    if isinstance(value, Quantity):
        raise TypeError(
            f"a template cannot render the {value.kind} `{value}` as it is: convert it with the "
            f"`to` filter, such as `{{{{ value | to('{value.canonical_unit}') }}}}`"
        )
    return value


# ---- settings that hold a plain number ------------------------------------------------------

#: The `json_schema_extra` key that says what a plain number setting measures.
MEASURE_KEY = "x-xeda-measure"
MEASURES = ("ratio", "percent", "count", "library_units", "data_size")


def ratio() -> dict[str, Any]:
    """Return the marker of a setting that is a unitless number and no count: a fraction, a
    ratio, a factor, a weight or an exponent (a utilization of 0.5, an aspect ratio, a duty
    cycle, a placer weight). Combine markers with `dataclass.extras`."""
    return {MEASURE_KEY: "ratio"}


def percent() -> dict[str, Any]:
    """Return the marker of a setting that is a percentage. Its name ends in `_percent`."""
    return {MEASURE_KEY: "percent"}


def count() -> dict[str, Any]:
    """Return the marker of a setting that is an integer count, a size in items or bits, a
    level, an index or a seed (iterations, fanout, verbosity, a layer index, a random seed)."""
    return {MEASURE_KEY: "count"}


def library_units() -> dict[str, Any]:
    """Return the marker of a setting that is a value in the units of the target library. Xeda
    passes it to the tool unconverted, and its description says "in the units of the library"."""
    return {MEASURE_KEY: "library_units"}


def data_size() -> dict[str, Any]:
    """Return the marker of a setting that is a size of data, in the unit that the description of
    the setting names (bytes, megabytes)."""
    return {MEASURE_KEY: "data_size"}


def is_quantity_unit(text: str) -> bool:
    """Return whether `text` is exactly a unit of one of the quantity kinds (`ns`, `MHz`)."""
    return text in Quantity._kind_of_unit  # pylint: disable=protected-access


def is_number_text(text: str) -> bool:
    """Return whether `text` is exactly one number in the grammar of a quantity (`5.5`, `1e3`)."""
    return re.fullmatch(_NUMBER, text) is not None


def measure_of(model: type[Any], field: str) -> Optional[str]:
    """Return what the plain number `field` of the pydantic `model` measures (one of `MEASURES`),
    or None when it has no marker."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    name = extra.get(MEASURE_KEY) if isinstance(extra, dict) else None
    return name if name in MEASURES else None


__all__ = [
    "Bounds",
    "CLOCK_UNITS",
    "Capacitance",
    "Frequency",
    "Length",
    "MEASURES",
    "MEASURE_KEY",
    "Power",
    "Quantity",
    "QuantityError",
    "QUANTITY_KEY",
    "Q_",
    "Resistance",
    "Temperature",
    "TemperatureDifference",
    "ThermalResistance",
    "Time",
    "Voltage",
    "check_unit_case",
    "convert_freq",
    "convert_time",
    "convert_unit",
    "count",
    "data_size",
    "finalize_template_value",
    "freq_mhz_optional",
    "is_number_text",
    "is_quantity_unit",
    "library_units",
    "measure_of",
    "percent",
    "ratio",
    "time_ns_optional",
    "to_filter",
    "unit_registry",
]
