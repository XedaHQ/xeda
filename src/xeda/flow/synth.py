from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Dict, Optional, Union

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
    rise: float = Field(0.0, description="Time of the rising edge of the clock, in ns.")
    duty_cycle: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        0.5, description="Duty cycle, between 0.0 and 1.0 (exclusive)."
    )  # type: ignore
    uncertainty: Optional[float] = Field(None, description="Clock uncertainty, in ns.")
    skew: Optional[float] = Field(None, description="Clock skew, in ns.")
    port: Optional[str] = Field(None, description="The design port of the clock.")

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

        Unlike the other clock timing fields, `duty_cycle` is a ratio and not a duration.
        Passing it through `convert_unit` would accept values such as `"0.5ns"` and reject the
        more useful percentage spelling with an opaque dimensionality error. Numeric strings stay
        accepted, because command-line overrides arrive as strings. Strings that carry a unit,
        and other non-numeric values, are rejected before pydantic applies the
        `0 < duty_cycle < 1` bounds.
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
        """Accept a period in picoseconds and store it in nanoseconds."""
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
        # This is a `before` validator, so values are still raw here: a CLI override such as
        # `-s clock.period=5.5` arrives as the string "5.5". Normalize before any arithmetic,
        # because dividing by the raw value fails with "unsupported operand type(s) for /".
        # Test `is not None` at every step, never truthiness: a zero frequency is a value the
        # user supplied and must reach the "must be positive" check below. Under `if freq:` it
        # would look the same as an omitted `freq` and produce "Neither `freq` nor `period` was
        # specified", which names the wrong problem. The same holds for `period`.
        #
        # `values` is already isolated by `xeda.dataclass.model_validator`: construction input is
        # copied in full, while assignment copies only the new field so unrelated model state
        # keeps its identity.
        if not isinstance(values, dict):
            return values
        assignment_field = info.field_name if info.data is None else None
        if assignment_field is not None and assignment_field != "period":
            # Assignment validation runs this whole-model validator again. Only the stored period
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
            raise ValueError("Neither `freq` nor `period` was specified")
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
    """The base class of synthesis flows."""

    requires_physical_clocks = (
        True  # physical clocks are required for each clock port in the design
    )

    class Settings(Flow.Settings):
        """The settings that all synthesis flows share."""

        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "clock_period": "`clock.period` or `clock.freq`",
        }

        clocks: Dict[str, PhysicalClock] = Field(
            {}, description="Clock constraints of the design, by clock name."
        )

        @classmethod
        def __get_pydantic_json_schema__(cls, core_schema, handler):
            """Advertise the single-clock input shorthand that normalizes into `clocks`."""
            schema = handler(core_schema)
            properties = schema.setdefault("properties", {})
            clock_schema = PhysicalClock.model_json_schema()
            clock_schema["description"] = "Single design clock. Use `clock.period` or `clock.freq`."
            clock_schema["x-xeda-input-only"] = True
            properties["clock"] = clock_schema
            return schema

        @model_validator(mode="before")
        @classmethod
        def _synthflow_settings_root_validator(cls, values, info):
            """Normalize the single-clock input into the one stored `clocks` value."""
            if info.field_name is not None and info.data is None:
                return values  # assignment to a real field; do not rebuild unrelated containers

            has_clock = "clock" in values
            has_clocks = "clocks" in values
            clock = values.pop("clock", None)
            if has_clock and has_clocks:
                raise ValueError("Specify `clock` for one clock or `clocks` for several, not both")
            if clock is not None:
                if isinstance(clock, PhysicalClock):
                    clock = clock.model_copy(deep=True)
                else:
                    clock = PhysicalClock.model_validate(clock)
                if not clock.name:
                    clock.name = "main_clock"
                values["clocks"] = {clock.name: clock}
            return values

        @property
        def main_clock(self) -> Optional[PhysicalClock]:
            named = self.clocks.get("main_clock")
            if named is not None:
                return named
            # Without a clock named `main_clock`, fall back to the first clock in insertion order.
            # This is deterministic, and it serves the consumers that support one clock only
            # (Yosys, nextpnr). A clock named `main_clock` is the explicit way to choose one.
            return first_value(self.clocks)

        @property
        def clock(self) -> Optional[PhysicalClock]:
            """The single-clock shorthand: the main clock, derived from the stored `clocks`."""
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

    @staticmethod
    def reconcile_clocks(
        clocks: Dict[str, PhysicalClock], design: Design, settings_class: type[Flow.Settings]
    ) -> None:
        """Tie each physical clock of the settings to a clock port of the design, in place.

        A single physical clock beside a single design clock is the same clock, whatever it is
        named. A physical clock that names no port and matches no design clock raises a
        `FlowSettingsError`. This is the one judgment of that case: planning
        (`check_design_supported`) and the constructor of the flow both make it."""
        # Shorthand for single clock specification.
        if len(clocks) == 1 and len(design.rtl.clocks) == 1:
            clock_name = first_key(clocks)
            assert clock_name is not None
            clock_obj = clocks.pop(clock_name)
            design_clock = design.rtl.clocks[0]
            assert design_clock is not None
            if not clock_name:
                clock_name = design_clock.name
            assert clock_name is not None
            assert clock_obj is not None
            if not clock_obj.port:
                clock_obj.port = design_clock.port
            if not clock_obj.name:
                clock_obj.name = clock_name
            clocks[clock_name] = clock_obj
        for clock_name, physical_clock in clocks.items():
            if not physical_clock.port:
                if clock_name not in (clk.name for clk in design.rtl.clocks):
                    if design.rtl.clocks:
                        msg = (
                            f"Physical clock {clock_name} has no corresponding clock port in the "
                            "design. Existing clocks: "
                            f"{', '.join(c.name for c in design.rtl.clocks if c and c.name)}"
                        )
                    else:
                        described = f"'{clock_name}'" if clock_name else "set by `clock`"
                        msg = (
                            f"No clock ports specified in `rtl`, while the physical clock "
                            f"{described} is set in the flow settings. Set the design clocks with "
                            "`rtl.clocks` (several clocks) or `rtl.clock.port` (one clock)"
                        )
                    raise FlowSettingsError([(None, msg, None, None)], settings_class)
                matched_clock = find_matching_clock(design.rtl.clocks, clock_name)
                if matched_clock:
                    physical_clock.port = matched_clock.port
                clocks[clock_name] = physical_clock

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a physical clock that no design clock matches, before anything runs."""
        super().check_design_supported(design, settings)
        if isinstance(settings, cls.Settings):
            # On a copy: the flow's constructor ties the clocks of its own settings.
            clocks = {name: clock.model_copy(deep=True) for name, clock in settings.clocks.items()}
            cls.reconcile_clocks(clocks, design, cls.Settings)

    def __init__(
        self,
        settings: Union[Settings, Dict],
        design: Union[Design, Dict],
        run_path: Path,
        **kwargs,
    ):
        super().__init__(settings, design, run_path, **kwargs)
        assert isinstance(
            self.settings, self.Settings
        ), "self.settings is not an instance of self.Settings class"
        self.reconcile_clocks(self.settings.clocks, self.design, self.Settings)
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
    "the FPGA device: give its part number with `-s fpga.part=<part>` on the command line, "
    "or in the design file as `flows.{flow}.fpga.part: <part>`"
)


class FpgaSynthFlow(SynthFlow, metaclass=ABCMeta):
    """The base class of all FPGA synthesis flows."""

    required_settings = {"fpga": FPGA_REQUIRED}

    class Settings(SynthFlow.Settings):
        """The settings that all FPGA synthesis flows share."""

        fpga: Optional[FPGA] = Field(
            None,
            description="FPGA device. Accepts a full part identifier as a string "
            '(e.g. "xc7a100tftg256-2L") or a mapping of the FPGA fields. A part identifier '
            "alone means `part: <identifier>` wherever layers merge. Over a mapping, it changes "
            "the part and keeps the other fields that the mapping wrote (a speed grade, for "
            "example).",
        )


class AsicSynthFlow(SynthFlow, metaclass=ABCMeta):
    """The base class of ASIC synthesis flows."""

    class Settings(SynthFlow.Settings):
        """The settings that all ASIC synthesis flows share."""


class DseFlow(Flow, metaclass=ABCMeta):
    """The base class of design-space exploration flows."""
