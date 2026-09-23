from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, Union

from ..dataclass import Field, XedaBaseModel, field_validator, model_validator
from ..design import Clock, Design
from ..units import convert_unit
from ..utils import first_key, first_value
from .flow import Flow, FlowSettingsError
from .fpga import FPGA

log = logging.getLogger(__name__)

__all__ = [
    "AsicSynthFlow",
    "FpgaSynthFlow",
    "PhysicalClock",
    "SynthFlow",
]


class PhysicalClock(XedaBaseModel):
    name: Optional[str] = None
    period: Optional[float] = Field(
        None,
        description="Clock period in ns. Specify `period` or `freq`; a consistent pair is also accepted.",
    )
    rise: float = Field(0.0, description="Rising time of clock (ns)")
    duty_cycle: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(0.5, description="Duty cycle (0.0..1.0)")  # type: ignore
    uncertainty: Optional[float] = Field(None, description="Clock uncertainty")
    skew: Optional[float] = Field(None, description="skew")
    port: Optional[str] = Field(None, description="associated design port")

    @field_validator(
        "period",
        "rise",
        "uncertainty",
        "skew",
        mode="before",
        json_schema_input_type=float | str | None,
    )
    @classmethod
    def time_validator(cls, value):
        if value is not None:
            return convert_unit(value, "nanosecond")
        return value

    @field_validator("duty_cycle", mode="before", json_schema_input_type=float | str)
    @classmethod
    def duty_cycle_validator(cls, value):
        """Validate the duty cycle as a unitless fraction.

        Unlike the other clock timing fields, ``duty_cycle`` is a ratio rather than a
        duration.  In particular, passing it through :func:`convert_unit` accidentally
        made values such as ``"0.5ns"`` look valid while rejecting the more useful
        percentage spelling with an opaque dimensionality error.  Keep numeric strings
        accepted because command-line overrides arrive as strings, but reject strings
        carrying a unit (and other non-numeric values) before Pydantic applies the
        ``0 < duty_cycle < 1`` bounds.
        """
        if isinstance(value, bool):
            raise ValueError("duty_cycle must be a unitless number between 0 and 1")
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ValueError(
                    "duty_cycle must be a unitless number between 0 and 1; do not specify a unit"
                ) from exc
        # Leave every other numeric representation (for example Decimal or Fraction) to
        # pydantic's ordinary float validation instead of inventing a narrower number protocol.
        return value

    @property
    def fall(self) -> float:
        if not self.period:
            return 0
        assert isinstance(self.duty_cycle, float)
        f = self.rise + (self.period * self.duty_cycle)
        if f >= self.period:
            raise ValueError("Fall time is beyond the period")
        return f

    @property
    def freq_mhz(self) -> float:
        if not self.period:
            return 0
        return 1000.0 / self.period

    @property
    def freq(self) -> float:
        """Clock frequency in MHz, derived from the one stored constraint (`period`)."""
        return self.freq_mhz

    @freq.setter
    def freq(self, value: Any) -> None:
        value = float(convert_unit(value, "MHz"))
        if value <= 0:
            raise ValueError(f"Clock frequency must be positive, got {value}")
        self.period = 1000.0 / value

    @property
    def period_ps(self) -> float:
        if not self.period:
            return 0
        return convert_unit(self.period, to_unit="picosecond", from_unit="nanosecond")

    @period_ps.setter
    def period_ps(self, period):
        self.period = convert_unit(period, to_unit="nanosecond", from_unit="picosecond")

    def period_unit(self, unit: str) -> float:
        unit = unit.strip()
        assert self.period is not None
        if not unit:
            return self.period
        return convert_unit(self.period, to_unit=unit, from_unit="nanosecond")

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Describe the accepted input, including the computed `freq` spelling.

        Pydantic only sees the stored `period` field; the before-validator also accepts `freq`.
        """
        schema = handler(core_schema)
        properties = schema.setdefault("properties", {})
        period_schema = properties.get("period", {})
        period_schema.pop("default", None)
        if "anyOf" in period_schema:
            period_schema["anyOf"] = [
                branch for branch in period_schema["anyOf"] if branch.get("type") != "null"
            ]
        properties["freq"] = {
            "anyOf": [{"type": "number"}, {"type": "string"}],
            "description": "Clock frequency in MHz. A number or a string with a frequency unit.",
            "title": "Freq",
        }
        schema["anyOf"] = [{"required": ["period"]}, {"required": ["freq"]}]
        return schema

    @model_validator(mode="before")
    @classmethod
    def root_validate_phys_clock(cls, values: Dict[str, Any], info) -> Dict[str, Any]:
        # This is a pre=True validator, so values are still raw here: a CLI override such as
        # `-s clock_period=5.5` arrives as the string "5.5". Normalize before any arithmetic --
        # dividing by the raw value used to fail with "unsupported operand type(s) for /".
        # `is not None`, not truthiness, at every step: a zero frequency is a value the user
        # supplied and must reach the "must be positive" check below. Under `if freq:` it looked
        # indistinguishable from an omitted `freq` and produced "Neither freq or period were
        # specified", which names the wrong problem. Same reasoning as the `period` test.
        #
        # `values` is already isolated by `xeda.dataclass.model_validator`: construction input is
        # copied in full, while assignment copies only the new field so unrelated model state
        # keeps its identity.
        if not isinstance(values, dict):
            return values
        assignment_field = info.field_name if info.data is None else None
        if assignment_field is not None and assignment_field != "period":
            # Assignment validation re-runs this whole-model validator. Only the stored period
            # concerns it; `freq = ...` is a property assignment that writes `period`.
            return values
        freq = values.pop("freq", None)
        if freq is not None:
            freq = float(convert_unit(freq, "MHz"))
            if freq <= 0:
                raise ValueError(f"Clock frequency must be positive, got {freq}")
        period = values.get("period")
        if period is not None:
            period = convert_unit(period, "nanosecond")
            if period <= 0:
                raise ValueError(f"Clock period must be positive, got {period}")
            if freq is not None and abs(period - 1000.0 / freq) > 0.0005:
                raise ValueError(
                    f"Clock period ({period} ns) and frequency ({freq} MHz) disagree; "
                    "specify one, or make them consistent"
                )
            values["period"] = period
        elif freq is not None:
            values["period"] = 1000.0 / freq
        else:
            raise ValueError("Neither freq or period were specified")
        if not values.get("name"):
            values["name"] = ""
        return values


def find_matching_clock(design_clocks: list[Clock], name: str):
    if len(design_clocks) == 1:
        return design_clocks[0]
    for clock in design_clocks:
        if clock.name == name:
            return clock
    for clock in design_clocks:
        if clock.port == name:
            return clock
    return None


class SynthFlow(Flow, metaclass=ABCMeta):
    """Superclass of synthesis flows"""

    requires_physical_clocks = (
        True  # physical clocks are required for each clock port in the design
    )

    class Settings(Flow.Settings):
        """base Synthesis flow settings"""

        clocks: Dict[str, PhysicalClock] = Field({}, description="Design clocks")

        @classmethod
        def __get_pydantic_json_schema__(cls, core_schema, handler):
            """Advertise the two single-clock input shorthands that normalize into `clocks`."""
            schema = handler(core_schema)
            properties = schema.setdefault("properties", {})
            clock_schema = PhysicalClock.model_json_schema()
            clock_schema["description"] = "Single design clock. Use `clock.period` or `clock.freq`."
            clock_schema["x-xeda-input-only"] = True
            properties["clock"] = clock_schema
            properties["clock_period"] = {
                "anyOf": [{"type": "number"}, {"type": "string"}],
                "deprecated": True,
                "description": "Compatibility shorthand for `clock.period` in nanoseconds. "
                "Cannot be combined with `clock` or `clocks`; prefer `clock.period`.",
                "x-xeda-input-only": True,
            }
            return schema

        @model_validator(mode="before")
        @classmethod
        def _synthflow_settings_root_validator(cls, values, info):
            """Normalize single-clock compatibility inputs into the one stored `clocks` value."""
            if info.field_name is not None and info.data is None:
                return values  # assignment to a real field; do not rebuild unrelated containers

            has_clock = "clock" in values
            has_clock_period = "clock_period" in values
            has_clocks = "clocks" in values
            clock = values.pop("clock", None)
            clock_period = values.pop("clock_period", None)
            if has_clock and has_clocks:
                raise ValueError("Specify `clock` for one clock or `clocks` for several, not both")
            if has_clock_period and (has_clock or has_clocks):
                raise ValueError(
                    "`clock_period` is a compatibility input and cannot be combined with "
                    "`clock` or `clocks`; use `clock.period` or `clock.freq`"
                )
            if clock is not None:
                if isinstance(clock, PhysicalClock):
                    clock = clock.model_copy(deep=True)
                else:
                    clock = PhysicalClock.model_validate(clock)
                if not clock.name:
                    clock.name = "main_clock"
                values["clocks"] = {clock.name: clock}
            elif clock_period is not None:
                clock = PhysicalClock(name="main_clock", period=clock_period)  # type: ignore[arg-type]
                values["clocks"] = {clock.name: clock}
            return values

        @property
        def main_clock(self) -> Optional[PhysicalClock]:
            named = self.clocks.get("main_clock")
            if named is not None:
                return named
            # Keep the historical, deterministic fallback for consumers that support one
            # timing target only (Yosys/nextpnr/OpenXC7).  A named ``main_clock`` remains the
            # explicit way to choose one; otherwise insertion order identifies the legacy main.
            return first_value(self.clocks)

        @property
        def clock(self) -> Optional[PhysicalClock]:
            """The single/main clock shorthand, derived from the stored `clocks` mapping."""
            return self.main_clock

        @clock.setter
        def clock(self, value: PhysicalClock | Dict[str, Any] | None) -> None:
            if value is None:
                self.clocks = {}
                return
            if not isinstance(value, PhysicalClock):
                value = PhysicalClock.model_validate(value)
            else:
                value = value.model_copy(deep=True)
            if not value.name:
                value.name = "main_clock"
            self.clocks = {value.name: value}

        @property
        def clock_period(self) -> Optional[float]:
            """Legacy API spelling, derived from `clock.period` and never stored separately."""
            clock = self.main_clock
            return clock.period if clock is not None else None

        @clock_period.setter
        def clock_period(self, value: Any) -> None:
            if value is None:
                # ``None`` is an absent compatibility override, not a request to destroy the
                # canonical clock topology.
                return
            if self.main_clock is None:
                self.clock = {"period": value}
            else:
                self.main_clock.period = value

    def __init__(
        self,
        settings: Union[Settings, Dict],
        design: Union[Design, Dict],
        run_path: Optional[Path] = None,
        **kwargs,
    ):
        super().__init__(settings, design, run_path, **kwargs)
        assert isinstance(
            self.settings, self.Settings
        ), "self.settings is not an instance of self.Settings class"
        # shorthand for single clock specification
        if len(self.settings.clocks) == 1 and len(self.design.rtl.clocks) == 1:
            clock_name = first_key(self.settings.clocks)
            assert clock_name is not None
            clock_obj = self.settings.clocks.pop(clock_name)
            design_clock = self.design.rtl.clocks[0]
            assert design_clock is not None
            if not clock_name:
                clock_name = design_clock.name
            assert clock_name is not None
            assert clock_obj is not None
            if not clock_obj.port:
                clock_obj.port = design_clock.port
            if not clock_obj.name:
                clock_obj.name = clock_name
            self.settings.clocks[clock_name] = clock_obj
        for clock_name, physical_clock in self.settings.clocks.items():
            if not physical_clock.port:
                if clock_name not in (clk.name for clk in self.design.rtl.clocks):
                    if self.design.rtl.clocks:
                        msg = f"Physical clock {clock_name} has no corresponding clock port in design. Existing clocks: {', '.join(c.name for c in self.design.rtl.clocks if c and c.name)}"
                    else:
                        described = f"'{clock_name}'" if clock_name else "set by `clock_period`"
                        msg = f"No clock ports specified in 'design.rtl', while the physical clock {described} is set in flow settings. Set corresponding design clocks via 'design.rtl.clocks' (for multiple clocks) or 'design.rtl.clock.port' (for a single clock)"
                    raise FlowSettingsError(
                        [
                            (
                                None,
                                msg,
                                None,
                                None,
                            )
                        ],
                        self.Settings,
                    )
                matched_clock = find_matching_clock(self.design.rtl.clocks, clock_name)
                if matched_clock:
                    physical_clock.port = matched_clock.port
                self.settings.clocks[clock_name] = physical_clock
        if self.requires_physical_clocks:
            for clock in self.design.rtl.clocks:
                clock_name = clock.name
                if clock.port not in (c.port for c in self.settings.clocks.values()):
                    log.warning(
                        "No clock period or frequency was specified for clock: %s (design clock port: '%s')",
                        clock_name,
                        clock.port,
                    )


#: How to give an FPGA flow its device, for `Flow.required_settings`.
FPGA_REQUIRED = (
    "the target FPGA device: give its part number with `-s fpga.part=<part>` on the command line, "
    'or as `fpga.part = "<part>"` in the design file\'s `[flows.{flow}]` section'
)


class FpgaSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of all FPGA synthesis flows"""

    required_settings = {"fpga": FPGA_REQUIRED}

    class Settings(SynthFlow.Settings):
        """base FPGA Synthesis flow settings"""

        fpga: Optional[FPGA] = Field(
            None,
            description="Target FPGA device. Accepts a full part identifier as a string "
            '(e.g. "xc7a100tftg256-2L") or a mapping of the FPGA fields.',
        )


class AsicSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of ASIC synthesis flows"""

    class Settings(SynthFlow.Settings):
        """base ASIC Synthesis flow settings"""


class DseFlow(Flow, metaclass=ABCMeta):
    """Superclass of design-space exploration flows"""
