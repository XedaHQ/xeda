"""Clock specification, including the CLI's `-s clock_period=...` path.

Values reaching `PhysicalClock`'s pre-root-validator are raw: a CLI override arrives as the
string "5.5". Dividing by it used to raise `unsupported operand type(s) for /: 'float' and
'str'`, which made every documented form of setting a clock period from the command line fail.
"""

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


def test_period_and_freq_are_kept_consistent():
    clock = PhysicalClock(period="4", freq="1000")  # type: ignore[call-arg]
    assert clock.period == pytest.approx(4.0)
    assert clock.freq == pytest.approx(250.0)  # period wins


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
    """A non-positive `clock_period` must be rejected even when `clocks` is also given.

    The back-fill from `clocks` tested `if not values.get("clock_period")`, so an explicit 0 was
    indistinguishable from an omitted one and was silently replaced by the main clock's period.
    A negative value escaped the other way: truthy, so never back-filled, and never checked.
    """
    # a settings-level failure surfaces as FlowSettingsError, not the raw pydantic error
    with pytest.raises((FlowSettingsError, ValidationError, ValueError)) as excinfo:
        VivadoSynth.Settings(
            fpga=FPGA("xc7a12tcsg325-1"),
            clock_period=period,
            clocks={"main_clock": {"period": 5.0}},
        )
    assert "positive" in str(excinfo.value)


def test_clock_period_is_still_back_filled_from_clocks():
    """The back-fill itself must keep working for a genuinely omitted `clock_period`."""
    settings = VivadoSynth.Settings(
        fpga=FPGA("xc7a12tcsg325-1"), clocks={"main_clock": {"period": 5.0}}
    )
    assert settings.clock_period == pytest.approx(5.0)


def test_neither_period_nor_freq_is_rejected():
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        PhysicalClock()  # type: ignore[call-arg]
    assert "Neither freq or period" in str(excinfo.value)


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
    settings = VivadoSynth.Settings(**overrides)  # type: ignore[arg-type]
    assert settings.clock_period == pytest.approx(5.5, abs=1e-3)
    assert settings.main_clock is not None
    assert settings.main_clock.period == pytest.approx(5.5, abs=1e-3)
