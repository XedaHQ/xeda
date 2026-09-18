"""Clock specification, including the CLI's `-s clock_period=...` path.

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
    """A non-positive legacy `clock_period` is still validated when used alone."""
    # a settings-level failure surfaces as FlowSettingsError, not the raw pydantic error
    with pytest.raises((FlowSettingsError, ValidationError, ValueError)) as excinfo:
        VivadoSynth.Settings(
            fpga=FPGA("xc7a12tcsg325-1"),
            clock_period=period,
        )
    assert "positive" in str(excinfo.value)


def test_legacy_clock_period_cannot_be_combined_with_canonical_clocks():
    with pytest.raises(
        (FlowSettingsError, ValidationError, ValueError), match="cannot be combined"
    ):
        VivadoSynth.Settings(
            fpga=FPGA("xc7a12tcsg325-1"),
            clock_period=5.0,
            clocks={"main_clock": {"period": 5.0}},
        )


def test_clock_period_property_is_derived_from_canonical_clocks():
    """The legacy API property remains available when only canonical clocks are stored."""
    settings = VivadoSynth.Settings(
        fpga=FPGA("xc7a12tcsg325-1"), clocks={"main_clock": {"period": 5.0}}
    )
    assert settings.clock_period == pytest.approx(5.0)


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
        {"clock_period": "5.5"},
        {"clocks": {"main_clock": {"period": "5.5"}}},
        {"clocks": {"main_clock": {"freq": "181.818MHz"}}},
    ],
)
def test_synth_flow_settings_accept_string_clock_overrides(overrides):
    """`xeda run vivado_synth <design> -s clock_period=5.5` hands settings raw strings."""
    settings = VivadoSynth.Settings(fpga="xc7a100tftg256-2L", **overrides)  # type: ignore[arg-type]
    assert settings.clock_period == pytest.approx(5.5, abs=1e-3)
    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(5.5, abs=1e-3)


def test_clock_period_setter_targets_main_clock_with_multiple_unnamed_clocks():
    """`clock_period` assignment must agree with the `main_clock`/`clock_period` getters.

    With several clocks and none named ``main_clock``, both `main_clock` and `clock_period`
    already fall back to the first declared clock -- and so does the settings-layer merge for
    `-s clock_period=...`. The setter used to reject this case as "ambiguous" instead of
    matching that fallback.
    """
    settings = VivadoSynth.Settings.from_input(
        {
            "fpga": "xc7a12tcsg325-1",
            "clocks": {"clk_a": {"period": 10.0}, "clk_b": {"period": 20.0}},
        }
    )
    assert settings.main_clock is settings.clocks["clk_a"]

    settings.clock_period = 5.0

    assert settings.clock_period == pytest.approx(5.0)
    assert settings.clocks["clk_a"].period == pytest.approx(5.0)
    assert settings.clocks["clk_b"].period == pytest.approx(20.0)
