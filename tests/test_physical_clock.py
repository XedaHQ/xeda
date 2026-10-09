"""Clock specification, including the CLI's `-s clock.period=...` path.

Values reaching `PhysicalClock`'s pre-root-validator are raw: a CLI override arrives as the
string "5.5". Dividing by it used to raise `unsupported operand type(s) for /: 'float' and
'str'`, which made every documented form of setting a clock period from the command line fail.
"""

from decimal import Decimal

import pytest

from xeda.dataclass import ValidationError
from xeda.flow import FPGA, FlowSettingsError
from xeda.flow.synth import PhysicalClock
from xeda.flows import VivadoSynth


@pytest.mark.parametrize("period", [5.5, "5.5", "5.5ns", 5, "5"])
def test_period_accepts_raw_cli_values(period):
    clock = PhysicalClock(period=period)  # type: ignore[call-arg]
    assert clock.period == pytest.approx(float(str(period).rstrip("ns") or 0))
    assert clock.freq == pytest.approx(1000.0 / clock.period)


@pytest.mark.parametrize("freq", [200.0, "200", "200MHz", "0.2GHz"])
def test_freq_accepts_raw_cli_values(freq):
    clock = PhysicalClock(freq=freq)  # type: ignore[call-arg]
    assert clock.freq == pytest.approx(200.0)
    assert clock.period == pytest.approx(5.0)


def test_contradictory_period_and_freq_are_rejected():
    with pytest.raises(ValidationError, match="disagree"):
        PhysicalClock(period="4", freq="1000")  # type: ignore[call-arg]


def test_a_historically_rounded_period_frequency_pair_is_accepted():
    clock = PhysicalClock(period=3.333, freq=300.0)  # type: ignore[call-arg]
    assert clock.period == pytest.approx(3.333)
    assert clock.freq == pytest.approx(300.030003)


def test_frequency_input_has_one_stored_value_and_round_trips_exactly():
    clock = PhysicalClock(freq=300.0)  # type: ignore[call-arg]
    dumped = clock.model_dump()
    reloaded = PhysicalClock.model_validate(dumped)

    assert "freq" not in dumped
    assert reloaded == clock
    assert reloaded.freq == pytest.approx(300.0)


def test_physical_clock_schema_accepts_either_input_spelling():
    schema = PhysicalClock.model_json_schema()
    assert "freq" in schema["properties"]
    assert {branch.get("type") for branch in schema["properties"]["period"]["anyOf"]} == {
        "number",
        "string",
    }
    assert schema["anyOf"] == [{"required": ["period"]}, {"required": ["freq"]}]


@pytest.mark.parametrize("period", [0, "0", -1])
def test_non_positive_period_is_rejected_with_a_clear_message(period):
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        PhysicalClock(period=period)  # type: ignore[call-arg]
    assert "positive" in str(excinfo.value)


@pytest.mark.parametrize("freq", [0, 0.0, "0", "0MHz", -5, "-5"])
def test_non_positive_freq_is_rejected_with_a_clear_message(freq):
    """A zero frequency must name the real problem, not look like an omitted `freq`.

    The validator tested `if freq:` rather than `if freq is not None:`, so 0 fell through to
    the "Neither freq or period were specified" branch -- which is untrue and points the user
    at the wrong fix. Both truthiness tests mattered: a raw "0MHz" survives the first check and
    converts to 0.0, only to be swallowed by the second.
    """
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        PhysicalClock(freq=freq)  # type: ignore[call-arg]
    assert "positive" in str(excinfo.value)
    assert "Neither freq or period" not in str(excinfo.value)


@pytest.mark.parametrize("period", [0, 0.0, "0", -1, "-1"])
def test_non_positive_clock_period_setting_is_rejected(period):
    """A non-positive `clock.period` is rejected."""
    # a settings-level failure surfaces as FlowSettingsError, not the raw pydantic error
    with pytest.raises((FlowSettingsError, ValidationError, ValueError)) as excinfo:
        VivadoSynth.Settings(
            fpga=FPGA("xc7a12tcsg325-1"),
            clock={"period": period},
        )
    assert "positive" in str(excinfo.value)


@pytest.mark.parametrize("extra", [{}, {"clocks": {"main_clock": {"period": 5.0}}}])
def test_clock_period_was_removed(extra):
    with pytest.raises(
        (FlowSettingsError, ValidationError, ValueError),
        match="`clock_period` was removed: use `clock.period` or `clock.freq`",
    ):
        VivadoSynth.Settings(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.0, **extra)


def test_neither_period_nor_freq_is_rejected():
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        PhysicalClock()  # type: ignore[call-arg]
    assert "Neither freq or period" in str(excinfo.value)


@pytest.mark.parametrize("value", ["0.5ns", "50%", "half"])
def test_duty_cycle_rejects_units_and_non_numeric_values(value):
    """Duty cycle is a unitless ratio, unlike the clock timing fields."""
    with pytest.raises(ValidationError, match="unitless number"):
        PhysicalClock(period=5.0, duty_cycle=value)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", [0.5, "0.5", Decimal("0.5")])
def test_duty_cycle_accepts_unitless_numeric_values(value):
    clock = PhysicalClock(period=5.0, duty_cycle=value)  # type: ignore[call-arg]
    assert clock.duty_cycle == pytest.approx(float(value))


@pytest.mark.parametrize(
    "overrides",
    [
        {"clock": {"period": "5.5"}},
        {"clocks": {"main_clock": {"period": "5.5"}}},
        {"clocks": {"main_clock": {"freq": "181.818MHz"}}},
    ],
)
def test_synth_flow_settings_accept_string_clock_overrides(overrides):
    """`xeda run vivado_synth <design> -s clock.period=5.5` hands settings raw strings."""
    settings = VivadoSynth.Settings(fpga="xc7a100tftg256-2L", **overrides)  # type: ignore[arg-type]
    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(5.5, abs=1e-3)


def test_period_ps_setter_converts_picoseconds_to_nanoseconds():
    """`period` is stored in nanoseconds; assigning `period_ps` must convert, not just store.

    The setter used to call `convert_unit(period, to_unit="picosecond", from_unit=None)`, which
    -- for a plain number, with no `from_unit` to attach and nothing for pint to parse -- left
    the value untouched and wrote the raw picosecond number straight into the nanosecond field:
    `clk.period_ps = 2500` set `period` to 2500 (ns) instead of 2.5.
    """
    clock = PhysicalClock(period=1.0)  # type: ignore[call-arg]

    clock.period_ps = 2500

    assert clock.period == pytest.approx(2.5)
    assert clock.period_ps == pytest.approx(2500)


def test_period_ps_setter_accepts_a_string_with_its_own_unit():
    """Period ps setter accepts a string with its own unit."""
    clock = PhysicalClock(period=1.0)  # type: ignore[call-arg]

    clock.period_ps = "2.5ns"

    assert clock.period == pytest.approx(2.5)
