"""Physical quantities with a required unit.

A setting that holds a measurement takes text such as `10 ns`. A bare number means nothing, so
the setting refuses it. The value keeps its canonical magnitude exactly: `10000 ps`, `0.01 us`
and `10 ns` are one value with one hash. The object has no arithmetic and no `float()`, so code
that forgets to convert fails in a test instead of handing a wrong number to a tool.
"""

import copy
import itertools
import json
import pickle
import random
import struct
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Any, Literal, Optional, Union

import jinja2
import pytest

from xeda import introspect
from xeda.dataclass import BaseModel, Field, ValidationError, XedaBaseModel, extras, tier
from xeda.flow.flow import Flow
from xeda.units import (
    Bounds,
    Capacitance,
    Frequency,
    Length,
    Quantity,
    QuantityError,
    Power,
    Resistance,
    Time,
    Voltage,
    METRIC_ONLY_UNITS,
    count,
    data_size,
    is_metric_unit,
    is_quantity_unit,
    library_units,
    measure_of,
    percent,
    ratio,
)
from xeda.utils import dump_json, json_encodable, location_free, semantic_hash

pytestmark = pytest.mark.python_compat

# For each kind: the unit table, written by hand. Each entry is the unit and the canonical
# magnitude of 2.5 of that unit.
TABLE: dict[type[Quantity], list[tuple[str, float]]] = {
    Time: [
        ("fs", 2.5e-6),
        ("ps", 2.5e-3),
        ("ns", 2.5),
        ("us", 2500.0),
        ("µs", 2500.0),
        ("ms", 2.5e6),
        ("s", 2.5e9),
        ("min", 150e9),
        ("h", 9000e9),
    ],
    Frequency: [("Hz", 2.5e-6), ("kHz", 2.5e-3), ("MHz", 2.5), ("GHz", 2500.0)],
    Capacitance: [("fF", 2.5), ("pF", 2500.0), ("nF", 2.5e6)],
    Length: [("nm", 2.5e-3), ("um", 2.5), ("µm", 2.5), ("mm", 2500.0)],
    Voltage: [("mV", 2.5e-3), ("V", 2.5)],
    Resistance: [("ohm", 2.5), ("kohm", 2500.0), ("Mohm", 2.5e6)],
    Power: [("nW", 2.5e-9), ("uW", 2.5e-6), ("µW", 2.5e-6), ("mW", 2.5e-3), ("W", 2.5)],
}
KINDS = {
    Time: ("time", "ns"),
    Frequency: ("frequency", "MHz"),
    Capacitance: ("capacitance", "fF"),
    Length: ("length", "um"),
    Voltage: ("voltage", "V"),
    Resistance: ("resistance", "ohm"),
    Power: ("power", "W"),
}
CASES = [(cls, unit, mag) for cls, rows in TABLE.items() for unit, mag in rows]


def test_every_kind_is_in_the_table() -> None:
    assert set(TABLE) == set(KINDS)
    for cls, (kind, canonical) in KINDS.items():
        assert cls.kind == kind
        assert cls.canonical_unit == canonical
        assert set(cls.units) == {unit for unit, _ in TABLE[cls]}


@pytest.mark.parametrize("space", ["", " ", "  "])
@pytest.mark.parametrize("cls,unit,magnitude", CASES)
def test_each_type_parses_every_unit_of_its_allowlist(cls, unit, magnitude, space) -> None:
    q = cls(f"2.5{space}{unit}")
    assert q.magnitude == magnitude
    assert q.to(unit) == 2.5


# ---- exactness -----------------------------------------------------------------------------


def test_equal_values_in_different_units_are_one_value() -> None:
    values = [Time(t) for t in ("10000 ps", "0.01 us", "10 ns", "1e1 ns", "10.0ns", "+10 ns")]
    assert len({v.magnitude for v in values}) == 1
    assert len({hash(v) for v in values}) == 1
    assert len({semantic_hash(v) for v in values}) == 1
    assert all(v == values[0] for v in values)


def test_a_conversion_is_exact() -> None:
    assert Time("0.3 ns").to("ps") == 300.0
    assert Time("0.1 us").to("ns") == 100.0
    digits = "1.234567890123456789012345678901234567890"
    assert len(digits.replace(".", "")) == 40
    assert Time(f"{digits} ns").magnitude == float(Fraction(Decimal(digits)))
    assert Time(f"{digits} us").magnitude == float(Fraction(Decimal(digits)) * 1000)
    assert Time(f"{digits} ps").magnitude == float(Fraction(Decimal(digits)) / 1000)


def test_minutes_and_hours() -> None:
    assert Time("1 min").magnitude == 60e9
    assert Time("1 h").magnitude == 3600e9
    assert Time("90 s").to("min") == 1.5
    assert Time("1.5 h").to("min") == 90.0


def test_the_units_of_metrics_change_nothing_a_setting_reads() -> None:
    """`degC`, `delta_degC` and `K/W` are units of metrics only. Every input that a setting
    refused before still gets the message that it got."""
    assert not any(is_quantity_unit(unit) for unit in METRIC_ONLY_UNITS)
    assert all(is_metric_unit(unit) for unit in METRIC_ONLY_UNITS)
    with pytest.raises(QuantityError) as error:
        Time("5 ns/s")
    assert str(error.value) == (
        "cannot read `5 ns/s` as a time: write one number and one unit, such as `10 ns`"
    )
    with pytest.raises(QuantityError) as error:
        Time("5 degC")
    assert str(error.value) == "unknown unit `degC` in `5 degC`: a time takes one of " + ", ".join(
        Time.units
    )
    with pytest.raises(QuantityError, match="cannot read `5 K/W` as a power"):
        Power("5 K/W")


# ---- canonical text ------------------------------------------------------------------------


def test_canonical_text() -> None:
    assert str(Time("10 ns")) == "10 ns"
    assert str(Time("10.0 ns")) == "10 ns"
    assert str(Time("2.5 ns")) == "2.5 ns"
    assert str(Time("1e-5 ns")) == "1e-05 ns"
    assert Time("2.5 ns").as_json_value() == "2.5 ns"
    assert Time("10000 ps").as_json_value() == "10 ns"
    assert repr(Time("10 ns")) == "Time('10 ns')"
    assert repr(Frequency("0.1 GHz")) == "Frequency('100 MHz')"
    assert str(Length("2 mm")) == "2000 um"


SPREAD = [
    0.0,
    1.0,
    -1.0,
    10.0,
    2.5,
    -2.5,
    0.1,
    1 / 3,
    5e-324,
    1e-300,
    1e-5,
    -1e-5,
    123456789.0,
    1e15,
    1e16,
    1e22,
    12345678901234567.0,
    1.7976931348623157e308,
    -1.7976931348623157e308,
]


@pytest.mark.parametrize("x", SPREAD)
def test_the_canonical_text_parses_back_to_the_same_magnitude(x) -> None:
    q = Time.of(x, "ns")
    assert Time(str(q)).magnitude == q.magnitude == x
    assert Time(q.as_json_value()) == q


def test_of_keeps_a_float_exactly() -> None:
    rng = random.Random(20261014)
    checked = 0
    while checked < 2000:
        (x,) = struct.unpack("<d", rng.randbytes(8))
        if x != x or x in (float("inf"), float("-inf")):
            continue
        checked += 1
        assert Time.of(x, "ns").magnitude == x
        assert Time(str(Time.of(x, "ns"))).magnitude == x


def test_of_takes_int_float_and_decimal_in_any_unit() -> None:
    assert Time.of(3, "ns").magnitude == 3.0
    assert Time.of(Decimal("0.3"), "ns").to("ps") == 300.0
    assert Time.of(1.5, "us") == Time("1500 ns")
    assert Time.of(0.01, "us") == Time("10 ns")
    assert Frequency.of(1, "GHz") == Frequency("1000 MHz")


@pytest.mark.parametrize(
    "magnitude",
    [True, "1", None, float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"), 1e400, [1]],
)
def test_of_refuses_what_is_no_finite_number(magnitude) -> None:
    with pytest.raises(QuantityError):
        Time.of(magnitude, "ns")


def test_of_refuses_a_unit_that_is_not_of_the_kind() -> None:
    with pytest.raises(QuantityError, match="frequency, not a time"):
        Time.of(1, "MHz")
    with pytest.raises(QuantityError, match="did you mean `ns`"):
        Time.of(1, "NS")
    with pytest.raises(QuantityError, match="unknown unit"):
        Time.of(1, "parsec")


def test_to_refuses_a_unit_that_is_not_of_the_kind() -> None:
    with pytest.raises(QuantityError, match="frequency, not a time"):
        Time("1 ns").to("MHz")
    with pytest.raises(QuantityError, match="did you mean `ps`"):
        Time("1 ns").to("PS")
    with pytest.raises(QuantityError, match="unknown unit"):
        Time("1 ns").to("nanoseconds")


def test_to_refuses_a_result_that_cannot_be_a_float() -> None:
    with pytest.raises(QuantityError, match="too large"):
        Time("1e308 ns").to("fs")
    with pytest.raises(QuantityError, match="too small"):
        Time.of(5e-324, "ns").to("h")


# ---- refusals ------------------------------------------------------------------------------


class Holder(XedaBaseModel):
    period: Time


class FrequencyHolder(XedaBaseModel):
    freq: Frequency


def _miscased(cls):
    """Generate the wrong-case spellings of the units of `cls`, with the right spelling."""
    for unit, _ in TABLE[cls]:
        for chars in itertools.product(*((c.lower(), c.upper()) for c in unit)):
            spelling = "".join(chars)
            if spelling != unit and spelling not in cls.units:
                yield spelling, unit


MISCASED = [(cls, s, u) for cls in TABLE for s, u in _miscased(cls)]

BAD_TIMES: list[Any] = [
    10,
    10.0,
    Decimal("10"),
    True,
    False,
    "10",
    " 10 ",
    "10.5",
    "5 MHz",
    "5 V",
    "5 nanoseconds",
    "5 parsec",
    "5 ns * 2",
    "5..5ns",
    "ns",
    "",
    " ",
    "5 ns ns",
    "nan ns",
    "inf ns",
    "-inf ns",
    "5,5 ns",
    "5_0 ns",
    "(5) ns",
    "5 ns/2",
    "1e400 ns",
    "-1e400 ns",
    "1e-400 ns",
    "1e99999999999999999999 ns",
    "1e-99999999999999999999 ns",
    "9" * 5000 + " ns",
    Frequency("5 MHz"),
    None,
    [],
    {},
    b"10 ns",
    bytearray(b"x"),
]


def _problem(value: Any, model: type[BaseModel] = Holder, field: str = "period") -> str:
    with pytest.raises(ValidationError) as caught:
        model.model_validate({field: value})
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == (field,)
    assert errors[0]["type"] == "quantity"
    return errors[0]["msg"]


@pytest.mark.parametrize("value", BAD_TIMES, ids=lambda v: repr(v)[:30])
def test_a_bad_value_is_a_field_error(value) -> None:
    _problem(value)
    with pytest.raises(QuantityError):
        Time(value)
    # a refusal is a `ValueError`, whatever the type of the value
    with pytest.raises(ValueError):
        Time(value)


@pytest.mark.parametrize("value", [10, 10.0, Decimal("10"), "10", " 10 ", "10.5"])
def test_a_bare_number_needs_a_unit(value) -> None:
    message = _problem(value)
    assert "needs a unit" in message
    assert "10 ns" in message


def test_a_bool_is_refused_with_the_example() -> None:
    message = _problem(True)
    assert "needs a unit" in message
    assert "10 ns" in message


def test_a_unit_of_another_kind_is_named() -> None:
    assert "frequency, not a time" in _problem("5 MHz")
    assert "5 MHz" in _problem("5 MHz")
    assert "time, not a frequency" in _problem("5 ns", FrequencyHolder, "freq")
    assert "voltage, not a time" in _problem("5 V")


def test_an_instance_of_another_type_is_refused() -> None:
    assert "frequency, not a time" in _problem(Frequency("5 MHz"))


@pytest.mark.parametrize("cls,spelling,unit", MISCASED)
def test_a_unit_in_another_case_is_refused_with_the_right_spelling(cls, spelling, unit) -> None:
    with pytest.raises(QuantityError, match=f"did you mean `{unit}`"):
        cls(f"2{spelling}")
    with pytest.raises(QuantityError, match=f"did you mean `{unit}`"):
        cls(f"2 {spelling}")


def test_the_greek_mu_is_refused_and_the_message_names_the_character_to_use() -> None:
    """U+03BC and U+00B5 look the same, so "did you mean `µm`?" alone would send the user in a
    circle."""
    for text in ("5 \u03bcm", "5\u03bcm"):
        with pytest.raises(QuantityError) as raised:
            Length(text)
        message = str(raised.value)
        assert "did you mean `\u00b5m` (the micro sign, U+00B5) or `um`?" in message
        assert "U+03BC" in message
    assert Length("5 \u00b5m") == Length("5 um")
    with pytest.raises(QuantityError) as raised:
        Time("5 \u03bcs")
    assert "did you mean `\u00b5s` (the micro sign, U+00B5) or `us`?" in str(raised.value)


def test_a_time_takes_the_micro_sign_as_a_length_does() -> None:
    """Each kind that takes a `u` prefix takes the micro sign too."""
    assert Time("2 \u00b5s") == Time("2 us") == Time("2000 ns")
    assert str(Time("2 \u00b5s")) == "2000 ns"
    for cls in (Time, Length):
        assert all(
            unit.replace("u", "\u00b5", 1) in cls.units
            for unit in cls.units
            if unit.startswith("u")
        )


def test_the_miscased_spellings_include_the_micro_sign_variants() -> None:
    spellings = {s for cls, s, _ in MISCASED if cls is Length}
    assert {"nM", "UM", "Μm", "ΜM"} <= spellings


def test_an_unknown_unit_names_the_units_of_the_kind() -> None:
    message = _problem("5 parsec")
    assert "parsec" in message
    assert "fs, ps, ns, us, µs, ms, s, min, h" in message
    assert "nanoseconds" in _problem("5 nanoseconds")


def test_a_value_out_of_range_is_refused() -> None:
    assert "too large" in _problem("1e400 ns")
    assert "too small" in _problem("1e-400 ns")
    # the unit moves a value over the edge too
    assert "too large" in _problem("1e305 h")
    assert _problem("not a number") is not None


def test_no_message_names_the_field() -> None:
    """The location of the error names the field already."""
    assert "period" not in _problem("10")


def test_negative_zero_is_zero() -> None:
    zero = Time("0 ns")
    for text in ("-0 ns", "-0.0 ps", "-0e5 us", "0 ns"):
        q = Time(text)
        assert q == zero
        assert hash(q) == hash(zero)
        assert str(q) == "0 ns"
        assert semantic_hash(q) == semantic_hash(0.0)
    assert str(Time.of(-0.0, "ns")) == "0 ns"
    assert Holder(period="-0 ns").period.as_json_value() == "0 ns"


def test_a_sign_is_part_of_the_number() -> None:
    assert Time("-5 ns").magnitude == -5.0
    assert Time("+5 ns").magnitude == 5.0
    assert Time(".5 ns").magnitude == 0.5
    assert Time("5. ns").magnitude == 5.0
    assert Time("5E3 ps").magnitude == 5.0


# ---- the value object ----------------------------------------------------------------------


def test_the_value_has_no_dict_and_cannot_change() -> None:
    t = Time("1 ns")
    assert not hasattr(t, "__dict__")
    with pytest.raises(AttributeError):
        t.other = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        t._magnitude = 2.0
    with pytest.raises(AttributeError):
        del t._magnitude


def test_the_value_has_no_arithmetic_and_no_number_conversion() -> None:
    t = Time("1 ns")
    for operation in (
        lambda: float(t),
        lambda: int(t),
        lambda: t + t,
        lambda: t - t,
        lambda: t * 2,
        lambda: 2 * t,
        lambda: t / 2,
        lambda: -t,
        lambda: abs(t),
        lambda: round(t),
        lambda: bool(t),
        lambda: f"{t:.3f}",
        lambda: t < 1.0,
        lambda: t >= 1,
        lambda: [0][t],  # type: ignore[index]
        lambda: sum([t, t]),
    ):
        with pytest.raises(TypeError):
            operation()


def test_the_truth_value_says_to_test_for_none() -> None:
    with pytest.raises(TypeError, match="is not None"):
        if Time("1 ns"):
            pass


def test_comparison() -> None:
    t = Time("1 ns")
    assert (t == 1.0) is False
    assert (t == "1 ns") is False
    assert (t != 1.0) is True
    assert (t == None) is False  # noqa: E711
    assert (Time("1 ns") == Frequency("1 MHz")) is False
    assert Time("1 ns") != Frequency("1 MHz")
    with pytest.raises(TypeError):
        Time("1 ns") < Frequency("1 MHz")
    assert Time("1 ns") < Time("2 ns")
    assert Time("2 ns") > Time("1 ns")
    assert Time("1 ns") <= Time("1000 ps")
    assert Time("1 ns") >= Time("1000 ps")
    assert Time("1 ps") < Time("1 ns") < Time("1 us") < Time("1 ms") < Time("1 s")
    assert sorted([Time("3 ns"), Time("1 ns"), Time("2000 ps")]) == [
        Time("1 ns"),
        Time("2 ns"),
        Time("3 ns"),
    ]
    assert hash(Time("1 ns")) == hash(Time("1000 ps"))
    assert len({Time("1 ns"), Time("1000 ps"), Time("2 ns")}) == 2
    assert {Time("1 ns"): "a"}[Time("0.001 us")] == "a"


def test_a_constructor_takes_text_or_the_same_type() -> None:
    t = Time("1 ns")
    assert Time(t) == t
    with pytest.raises(QuantityError, match="frequency, not a time"):
        Time(Frequency("1 MHz"))


def test_an_abstract_base_is_no_kind() -> None:
    with pytest.raises(TypeError):
        Quantity("1 ns")


# ---- copies --------------------------------------------------------------------------------


class Bag(XedaBaseModel):
    period: Time
    opt: Optional[Time] = None
    items: list[Time] = []


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize("cls,unit,magnitude", CASES)
def test_pickle_round_trips(cls, unit, magnitude, protocol) -> None:
    q = cls(f"2.5 {unit}")
    copied = pickle.loads(pickle.dumps(q, protocol))
    assert type(copied) is cls
    assert copied == q
    assert copied.magnitude == magnitude


def test_copies_round_trip() -> None:
    q = Time("2.5 ns")
    for copied in (copy.copy(q), copy.deepcopy(q)):
        assert copied == q
        assert type(copied) is Time


def test_copies_of_a_model_round_trip() -> None:
    bag = Bag(period="2.5 ns", opt="1 us", items=["1 ns", "2 ns"])
    for copied in (
        bag.model_copy(deep=True),
        bag.model_copy(),
        copy.deepcopy(bag),
        pickle.loads(pickle.dumps(bag)),
    ):
        assert copied == bag
        assert type(copied.period) is Time
        assert copied.items == [Time("1 ns"), Time("2 ns")]


# ---- pydantic ------------------------------------------------------------------------------


class Rich(XedaBaseModel):
    period: Time
    opt: Optional[Time] = None
    bounded: Annotated[Time, Bounds(gt="0 ns")] = Time("1 ns")
    auto: Union[Literal["auto"], Annotated[Time, Bounds(ge="0 ns")], None] = "auto"
    many: list[Time] = []
    named: dict[str, Time] = {}


def test_a_model_takes_text_and_an_instance() -> None:
    m = Rich(period="10 ns", opt=Time("1 us"), many=["1 ns", Time("2 ns")], named={"a": "3 ns"})
    assert m.period == Time("10 ns")
    assert m.opt == Time("1000 ns")
    assert m.many == [Time("1 ns"), Time("2 ns")]
    assert m.named == {"a": Time("3 ns")}
    assert m.auto == "auto"
    assert m.opt is not None and isinstance(m.opt, Time)


def test_assignment_validates() -> None:
    m = Rich(period="10 ns")
    m.period = "20000 ps"
    assert m.period == Time("20 ns")
    for bad in (5, "5", "5 MHz", None):
        with pytest.raises(ValidationError):
            m.period = bad  # type: ignore[assignment]
    assert m.period == Time("20 ns")
    m.opt = None
    m.auto = "0 ns"
    assert m.auto == Time("0 ns")
    with pytest.raises(ValidationError):
        m.auto = "-1 ns"


def test_none_is_refused_where_the_field_is_not_optional() -> None:
    assert "unit" in _problem(None) or "time" in _problem(None)


def test_validation_is_idempotent() -> None:
    m = Rich(period="10 ns", opt="3 ps", auto="2 ns", many=["1 ns"], named={"a": "1 ns"})
    again = m.model_copy(deep=True)
    again.period = m.period
    again.opt = m.opt
    again.auto = m.auto
    assert again == m
    assert Rich.model_validate(m.model_dump(mode="json")) == m
    assert Rich.model_validate(json.loads(m.model_dump_json())) == m
    assert Rich.model_validate_json(m.model_dump_json()) == m
    assert Rich.model_validate(m.model_dump()) == m


def test_the_dump_keeps_the_object_and_the_json_dump_is_the_canonical_text() -> None:
    m = Rich(period="10000 ps", opt="2.5 ns", many=["1 us"], named={"k": "1e-5 ns"})
    plain = m.model_dump()
    assert isinstance(plain["period"], Time)
    assert isinstance(plain["opt"], Time)
    assert isinstance(plain["many"][0], Time)
    assert isinstance(plain["named"]["k"], Time)
    as_json = m.model_dump(mode="json")
    assert as_json["period"] == "10 ns"
    assert as_json["opt"] == "2.5 ns"
    assert as_json["many"] == ["1000 ns"]
    assert as_json["named"] == {"k": "1e-05 ns"}
    assert as_json["bounded"] == "1 ns"
    assert as_json["auto"] == "auto"
    assert json.loads(m.model_dump_json()) == as_json


def _quantity_schemas(node: Any) -> list[dict]:
    found = []
    if isinstance(node, dict):
        if "x-xeda-quantity" in node:
            found.append(node)
        for value in node.values():
            found.extend(_quantity_schemas(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_quantity_schemas(value))
    return found


def test_the_json_schema_names_the_kind() -> None:
    schema = Rich.model_json_schema()
    props = schema["properties"]
    for name in ("period", "opt", "bounded", "auto", "many", "named"):
        found = _quantity_schemas(props[name])
        assert len(found) == 1, name
        assert found[0]["type"] == "string"
        assert found[0]["x-xeda-quantity"] == "time"
        assert "10 ns" in found[0]["description"]
        assert "fs, ps, ns, us, µs, ms, s, min, h" in found[0]["description"]
    assert props["many"]["type"] == "array"
    assert props["named"]["type"] == "object"
    assert {"type": "null"} in props["opt"]["anyOf"]
    assert {"const": "auto", "type": "string"} in props["auto"]["anyOf"] or any(
        "auto" in str(branch) for branch in props["auto"]["anyOf"]
    )


@pytest.mark.parametrize("cls", list(TABLE))
def test_every_kind_has_a_schema(cls) -> None:
    class M(XedaBaseModel):
        x: cls  # type: ignore[valid-type]

    node = M.model_json_schema()["properties"]["x"]
    assert node["x-xeda-quantity"] == KINDS[cls][0]
    assert node["type"] == "string"
    for unit, _ in TABLE[cls]:
        assert unit in node["description"]


@pytest.mark.parametrize("cls", list(TABLE))
def test_a_listing_shows_the_kind_of_a_quantity(cls) -> None:
    class M(XedaBaseModel):
        x: cls  # type: ignore[valid-type]

    schema = M.model_json_schema()
    assert introspect.type_str(schema["properties"]["x"], schema.get("$defs")) == KINDS[cls][0]


def test_a_listing_shows_the_kind_inside_optional_unions_and_containers() -> None:
    schema = Rich.model_json_schema()
    shown = {
        name: introspect.type_str(spec, schema.get("$defs"))
        for name, spec in schema["properties"].items()
    }
    assert shown["period"] == "time"
    assert shown["opt"] == "time"  # an optional setting reads as its type
    assert shown["bounded"] == "time"
    assert shown["many"] == "array[time]"
    assert shown["named"] == "dict[string, time]"
    # a union lists its branches by type, as it does for any other union: the text `auto` is a
    # string
    assert shown["auto"] == "string | time"


def test_the_settings_schema_of_a_flow_checks_the_form_of_a_quantity_and_names_its_kind() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = introspect.settings_info("yosys")["json_schema"]
    validator = jsonschema.Draft202012Validator(schema)
    assert not list(validator.iter_errors({"clock": {"period": "10 ns"}}))
    assert not list(validator.iter_errors({"abc": {"delay_target": "auto", "load": "3.9 fF"}}))
    # a bare number is not text: the schema can say so
    assert list(validator.iter_errors({"clock": {"period": 10}}))
    assert list(validator.iter_errors({"abc": {"delay_target": 500}}))
    # it cannot check the unit in a text: Xeda refuses `10` and `10 MHz` as a time, and the
    # schema accepts both
    assert not list(validator.iter_errors({"clock": {"period": "10"}}))
    assert not list(validator.iter_errors({"clock": {"period": "10 MHz"}}))
    clock = schema["$defs"]["PhysicalClock"]["properties"]
    assert (clock["period"]["type"], clock["period"]["x-xeda-quantity"]) == ("string", "time")
    assert (clock["freq"]["type"], clock["freq"]["x-xeda-quantity"]) == ("string", "frequency")
    abc = schema["$defs"]["AbcGroup"]["properties"]
    assert _quantity_schemas(abc["max_slew"])[0]["x-xeda-quantity"] == "time"
    assert _quantity_schemas(abc["load"])[0]["x-xeda-quantity"] == "capacitance"


def test_a_field_description_replaces_the_description_but_not_the_kind() -> None:
    class M(XedaBaseModel):
        x: Time = Field(Time("1 ns"), description="The clock period.")

    node = M.model_json_schema()["properties"]["x"]
    assert node["description"] == "The clock period."
    assert node["x-xeda-quantity"] == "time"


def test_a_plain_basemodel_works_too() -> None:
    class M(BaseModel):
        x: Time

    assert M(x="1000 ps").x == Time("1 ns")
    assert M(x="1 ns").model_dump(mode="json") == {"x": "1 ns"}


def test_bounds() -> None:
    with pytest.raises(ValidationError) as caught:
        Rich(period="1 ns", bounded="-1 ns")
    (error,) = caught.value.errors()
    assert error["loc"] == ("bounded",)
    assert "must be greater than 0 ns" in error["msg"]
    assert "-1 ns" in error["msg"]
    with pytest.raises(ValidationError, match="must be greater than 0 ns"):
        Rich(period="1 ns", bounded="0 ns")
    assert Rich(period="1 ns", auto="0 ns").auto == Time("0 ns")
    with pytest.raises(ValidationError, match="must be at least 0 ns"):
        Rich(period="1 ns", auto="-1 ps")
    assert Rich(period="1 ns", bounded="1 ps").bounded == Time("1 ps")


def test_bounds_with_every_comparison() -> None:
    class M(XedaBaseModel):
        lt: Annotated[Time, Bounds(lt="10 ns")] = Time("1 ns")
        le: Annotated[Time, Bounds(le="10 ns")] = Time("1 ns")
        window: Annotated[Time, Bounds(gt="1 ns", lt="1 us")] = Time("2 ns")

    assert M(lt="9999 ps", le="10 ns", window="500 ns")
    with pytest.raises(ValidationError, match="must be less than 10 ns"):
        M(lt="10 ns")
    with pytest.raises(ValidationError, match="must be at most 10 ns"):
        M(le="10001 ps")
    for bad in ("1 ns", "1 us", "0 ns", "1 ms"):
        with pytest.raises(ValidationError, match="must be (greater|less) than"):
            M(window=bad)
    # a value of the wrong form is refused by the type first
    with pytest.raises(ValidationError, match="needs a unit"):
        M(lt="5")


def test_a_bound_of_another_kind_fails_when_the_class_is_built() -> None:
    with pytest.raises(QuantityError, match="frequency, not a time"):

        class Bad(XedaBaseModel):
            x: Annotated[Time, Bounds(gt="0 MHz")] = Time("1 ns")

    with pytest.raises(QuantityError, match="needs a unit"):

        class Bad2(XedaBaseModel):
            x: Annotated[Time, Bounds(gt="0")] = Time("1 ns")

    with pytest.raises(TypeError):

        class Bad3(XedaBaseModel):
            x: Annotated[float, Bounds(gt="0 ns")] = 1.0

    with pytest.raises(TypeError):
        Bounds()


def test_the_project_config_is_in_force() -> None:
    with pytest.raises(ValidationError):
        Rich(period="1 ns", unknown="1 ns")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Rich()  # type: ignore[call-arg]
    assert Rich.model_config["validate_default"] is True


# ---- identity and JSON ---------------------------------------------------------------------

#: `semantic_hash` of a clock whose period is the float 10.0, computed before quantities existed.
OLD_CLOCK_HASH = "e0a3c98a20048a75d0d529404a564148a6cc0e9c498892a5eef323355ec18092"


def test_a_quantity_hashes_as_the_float_it_replaces() -> None:
    assert semantic_hash(Time("10 ns")) == semantic_hash(10.0)
    assert semantic_hash(Time("10000 ps")) == semantic_hash(10.0)
    assert semantic_hash(Time("10.5 ns")) == semantic_hash(10.5)
    assert semantic_hash({"a": [Time("10 ns"), {"b": Time("10 ns")}]}) == semantic_hash(
        {"a": [10.0, {"b": 10.0}]}
    )
    assert semantic_hash({"clocks": {"main_clock": {"period": Time("10 ns")}}}) == OLD_CLOCK_HASH
    assert semantic_hash({"clocks": {"main_clock": {"period": 10.0}}}) == OLD_CLOCK_HASH
    assert semantic_hash(Time("10 ns")) != semantic_hash(Time("11 ns"))


def test_the_hash_of_a_model_dump_does_not_change() -> None:
    class Clock(XedaBaseModel):
        period: Time

    assert semantic_hash({"clocks": {"main_clock": Clock(period="10 ns").model_dump()}}) == (
        OLD_CLOCK_HASH
    )


def test_location_free_passes_a_quantity_through() -> None:
    t = Time("10 ns")
    assert location_free(t, []) is t
    assert location_free({"a": [t]}, []) == {"a": [t]}


def test_json_writes_the_canonical_text() -> None:
    data = {"a": [Time("10 ns"), {"b": Frequency("0.1 GHz")}], "c": Length("1 mm")}
    text = json.dumps(data, default=json_encodable)
    assert json.loads(text) == {"a": ["10 ns", {"b": "100 MHz"}], "c": "1000 um"}
    assert json_encodable(Time("2 us")) == "2000 ns"


def test_dump_json_writes_the_canonical_text(tmp_path) -> None:
    path = tmp_path / "out.json"
    dump_json({"a": [Time("10 ns"), {"b": Frequency("100 MHz")}]}, path)
    assert json.loads(path.read_text()) == {"a": ["10 ns", {"b": "100 MHz"}]}


def test_json_safe_writes_the_canonical_text() -> None:
    assert introspect.json_safe({"a": [Time("10 ns"), {"b": Frequency("100 MHz")}]}) == {
        "a": ["10 ns", {"b": "100 MHz"}]
    }


# ---- templates -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env() -> jinja2.Environment:
    return Flow._create_jinja_env()


def _render(env: jinja2.Environment, source: str, **values: Any) -> str:
    return env.from_string(source).render(**values)


def test_the_filter_converts(env) -> None:
    t = Time("0.3 ns")
    assert _render(env, '{{ t | to("ps") }}', t=t) == "300.0"
    assert _render(env, '{{ t | to("ns") }}', t=t) == "0.3"
    assert _render(env, '{{ f | to("GHz") }}', f=Frequency("100 MHz")) == "0.1"
    assert _render(env, '{{ "%.1f" | format(t | to("ps")) }}', t=t) == "300.0"


def test_the_filter_refuses_what_is_no_quantity(env) -> None:
    for value in (3.0, 3, "3 ns", None):
        with pytest.raises(TypeError, match="quantity"):
            _render(env, '{{ x | to("ns") }}', x=value)


def test_the_filter_refuses_a_unit_of_another_kind(env) -> None:
    with pytest.raises(QuantityError, match="frequency, not a time"):
        _render(env, '{{ t | to("MHz") }}', t=Time("1 ns"))


def test_rendering_a_quantity_directly_is_refused_and_names_the_filter(env) -> None:
    with pytest.raises(TypeError, match=r"\bto\b") as caught:
        _render(env, "{{ t }}", t=Time("1 ns"))
    assert "filter" in str(caught.value)
    with pytest.raises(TypeError, match=r"\bto\b"):
        _render(env, "period {{ clock.period }} ns", clock={"period": Time("1 ns")})


@pytest.mark.parametrize(
    "value", ["text", "", 7, 0, 2.5, -1e-5, None, True, [1, "a"], {"k": 1}], ids=repr
)
def test_a_template_without_a_quantity_renders_as_before(env, value) -> None:
    plain = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    source = "a {{ x }} b {{ x | string }} c {% if x is not none %}{{ x }}{% endif %}"
    assert _render(env, source, x=value) == _render(plain, source, x=value)


def test_a_quantity_stays_out_of_a_template_unless_converted(env) -> None:
    source = "{% for t in ts %}{{ t | to('ns') }};{% endfor %}"
    assert _render(env, source, ts=[Time("1 ns"), Time("1 us")]) == "1.0;1000.0;"


# ---- markers -------------------------------------------------------------------------------


@pytest.mark.parametrize("marker,name", [(ratio, "ratio"), (percent, "percent"), (count, "count")])
def test_a_marker_is_one_key(marker, name) -> None:
    assert marker() == {"x-xeda-measure": name}


def test_the_other_markers() -> None:
    assert library_units() == {"x-xeda-measure": "library_units"}
    assert data_size() == {"x-xeda-measure": "data_size"}


def test_markers_combine_with_other_markers() -> None:
    assert extras(tier("common"), ratio()) == {
        "x-xeda-tier": "common",
        "x-xeda-measure": "ratio",
    }
    assert extras(ratio(), ratio()) == ratio()
    with pytest.raises(ValueError, match="x-xeda-measure"):
        extras(ratio(), count())


def test_measure_of_reads_the_marker() -> None:
    class M(XedaBaseModel):
        u: float = Field(0.5, json_schema_extra=extras(tier("common"), ratio()))
        n: int = Field(1, json_schema_extra=count())
        lib: float = Field(1.0, json_schema_extra=library_units())
        plain: float = 1.0
        tiered: float = Field(1.0, json_schema_extra=tier("common"))

    assert measure_of(M, "u") == "ratio"
    assert measure_of(M, "n") == "count"
    assert measure_of(M, "lib") == "library_units"
    assert measure_of(M, "plain") is None
    assert measure_of(M, "tiered") is None
    assert measure_of(M, "missing") is None
