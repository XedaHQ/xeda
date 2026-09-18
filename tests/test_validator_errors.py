"""A mistake in a design or settings file must be reported as a validation error.

pydantic v1 treated a `TypeError` raised inside a validator like a `ValueError`; v2 lets it escape
`model_validate` untouched, so a plain user mistake (`sources = 123`) surfaced as a raw traceback
on the CLI and under the wrong error class in `--json` output. The fix lives in `xeda.dataclass`
and applies to every validator, so these tests are grouped by hazard rather than by module.
"""

import pytest

from xeda.dataclass import ValidationError, XedaBaseModel, field_validator
from xeda.design import Design, DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flow.synth import PhysicalClock
from xeda.flows.yosys.yosys_fpga import YosysFpga

BAD_VALUES = [123, 4.5, True, object()]


# ---------------------------------------------------------------------------------------------
# A TypeError raised inside a validator must surface as a validation error, not a traceback.
# v1 treated TypeError like ValueError; v2 lets it escape `model_validate` untouched.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_VALUES, ids=lambda v: type(v).__name__)
def test_scalar_sources_is_a_validation_error(bad):
    """`rtl.sources = 123` used to escape as `TypeError: 'int' object is not iterable`."""
    with pytest.raises(DesignValidationError):
        Design(name="d", rtl={"sources": bad, "top": "t"})


def test_scalar_sources_message_names_the_field_and_the_type():
    with pytest.raises(DesignValidationError, match=r"'sources' must be a list"):
        Design(name="d", rtl={"sources": 123, "top": "t"})


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
    ],
    ids=repr,
)
def test_non_numeric_clock_is_a_validation_error(bad):
    """`PhysicalClock(freq=[])` used to escape as a raw `TypeError` from `float()`."""
    with pytest.raises((ValueError, FlowSettingsError)):
        PhysicalClock(freq=bad)


@pytest.mark.parametrize("bad", ["x", "5 furlongs", "abc"], ids=repr)
def test_unparseable_unit_is_a_validation_error(bad):
    """pint raises `UndefinedUnitError` (an `AttributeError`) and `DimensionalityError` (a
    `TypeError`); neither is a validation failure to pydantic v2, so a bad unit in a design file
    reached the user as a raw traceback."""
    with pytest.raises((ValueError, FlowSettingsError)):
        PhysicalClock(freq=bad)


def test_valid_units_still_convert():
    clock = PhysicalClock(freq="200 MHz")
    assert (clock.freq, clock.period) == (200.0, 5.0)
    assert PhysicalClock(period="5.5ns").period == 5.5


@pytest.mark.parametrize("bad", BAD_VALUES, ids=lambda v: type(v).__name__)
def test_scalar_verilog_lib_is_a_validation_error(bad):
    with pytest.raises((FlowSettingsError, ValueError)):
        YosysFpga.Settings(verilog_lib=bad)


def test_type_error_in_any_validator_becomes_a_validation_error():
    """The shim-level safety net, exercised directly."""

    class M(XedaBaseModel):
        x: int = 0

        @field_validator("x", mode="before")
        @classmethod
        def _boom(cls, value):
            raise TypeError("deliberate")

    with pytest.raises(ValueError, match="deliberate"):
        M(x=1)


# ---------------------------------------------------------------------------------------------
# A validator that reads another field must not assume that field validated: `info.data` holds only
# the fields that did, so `values["x"]` turned x's own error into a bare `KeyError`.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [{"init_freq_high": 100.0}, {"init_freq_low": "fast", "init_freq_high": 100.0}],
    ids=["missing", "non_numeric"],
)
def test_a_bad_init_freq_low_is_reported_as_itself(settings):
    """`xeda dse` crashed with `KeyError: 'init_freq_low'` instead of naming the setting."""
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    with pytest.raises(ValidationError) as excinfo:
        FmaxOptimizer.Settings(**settings)
    assert [e["loc"] for e in excinfo.value.errors()] == [("init_freq_low",)]


def test_init_freq_high_must_exceed_init_freq_low():
    """A real check, not an `assert` that `python -O` would skip."""
    from xeda.flow_runner.dse.fmax import FmaxOptimizer

    with pytest.raises(ValidationError, match="must be greater than init_freq_low"):
        FmaxOptimizer.Settings(init_freq_low=200.0, init_freq_high=100.0)


def test_a_bad_pll_module_name_is_reported_as_itself():
    from xeda.flows.nextpnr import EcpPLL

    with pytest.raises(ValidationError) as excinfo:
        EcpPLL(module=None, clkouts=[50.0])  # type: ignore[call-arg]
    assert [e["loc"] for e in excinfo.value.errors()] == [("module",)]
    assert EcpPLL(clkouts=[50.0]).file == "ecp5pll.v"  # type: ignore[call-arg]
