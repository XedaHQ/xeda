from __future__ import annotations

import logging
from abc import ABCMeta
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, ClassVar, Dict, Optional, Union

from pydantic import TypeAdapter

from ..dataclass import Field, XedaBaseModel, model_validator
from ..design import Design
from ..units import Bounds, Frequency, QuantityError, Time, ratio
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
    period: Annotated[Time, Bounds(gt="0 ns")] = Field(
        ...,
        description="Clock period, a time with a unit, such as `10 ns`. Specify `period` or "
        "`freq`; a consistent pair is also accepted.",
    )
    rise: Time = Field(
        Time("0 ns"),
        description="Time of the rising edge of the clock, a time with a unit, such as `0.5 ns`.",
    )
    duty_cycle: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        0.5,
        description="Duty cycle, between 0.0 and 1.0 (exclusive).",
        json_schema_extra=ratio(),
    )  # type: ignore
    uncertainty: Optional[Time] = Field(
        None, description="Clock uncertainty, a time with a unit, such as `0.1 ns`."
    )
    skew: Optional[Time] = Field(
        None, description="Clock skew, a time with a unit, such as `0.05 ns`."
    )
    port: Optional[str] = Field(None, description="The design port of the clock.")

    @property
    def fall(self) -> Time:
        """The time of the falling edge of the clock."""
        period = self.period.to("ns")
        fall = self.rise.to("ns") + (period * self.duty_cycle)
        if fall >= period:
            raise ValueError("Fall time is beyond the period")
        return Time.of(fall, "ns")

    @property
    def freq(self) -> Frequency:
        """Clock frequency, derived from the one stored constraint (`period`)."""
        return Frequency.of(1000.0 / self.period.to("ns"), "MHz")

    @freq.setter
    def freq(self, value: str | Frequency) -> None:
        self.period = _period_of(_frequency(value))

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Describe the accepted input, including the computed `freq` spelling.

        Pydantic only sees the stored `period` field; the before-validator also accepts `freq`.
        """
        schema = handler(core_schema)
        properties = schema.setdefault("properties", {})
        properties["freq"] = {
            **TypeAdapter(Frequency).json_schema(),
            "description": "Clock frequency, a frequency with a unit, such as `100 MHz`. Specify "
            "`freq` or `period`; a consistent pair is also accepted.",
            "title": "Freq",
        }
        schema.pop("required", None)
        schema["anyOf"] = [{"required": ["period"]}, {"required": ["freq"]}]
        return schema

    @model_validator(mode="before")
    @classmethod
    def root_validate_phys_clock(cls, values: Dict[str, Any], info) -> Dict[str, Any]:
        # This is a `before` validator, so values are still raw here: a CLI override such as
        # `-s clock.period=5.5ns` arrives as the string "5.5ns". The field validation of `period`
        # reads it. Only `freq` is read here, because it is no stored field: it becomes `period`.
        # Test `is not None` at every step, never truthiness: a zero frequency is a value the
        # user supplied and must reach the "greater than 0 MHz" check below. Under `if freq:` it
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
        period = values.get("period")
        if freq is not None:
            freq = _frequency(freq)
            if period is None:
                values["period"] = _period_of(freq)
            else:
                try:
                    given = Time(period)
                except QuantityError:
                    given = None  # the field validation of `period` names the problem
                if given is not None and abs(given.to("ns") - _period_of(freq).to("ns")) > 0.0005:
                    raise ValueError(
                        f"Clock period ({given}) and frequency ({freq}) disagree; "
                        "specify one, or make them consistent"
                    )
        elif period is None:
            raise ValueError("Neither `freq` nor `period` was specified")
        if values.get("name") is None:
            values["name"] = ""
        return values


def _frequency(value: Any) -> Frequency:
    """Read the `freq` of a clock: a frequency greater than 0 MHz."""
    try:
        freq = Frequency(value)
    except QuantityError as error:
        raise ValueError(f"`freq`: {error}") from None
    if not freq > Frequency("0 MHz"):
        raise ValueError(f"`freq`: must be greater than 0 MHz, got {freq}")
    return freq


def _period_of(freq: Frequency) -> Time:
    """The period of a clock of frequency `freq`."""
    return Time.of(1000.0 / freq.to("MHz"), "ns")


def find_matching_clock(design_clocks: Sequence[Mapping[str, str]], name: str):
    if len(design_clocks) == 1:
        return design_clocks[0]
    for clock in design_clocks:
        if clock["name"] == name:
            return clock
    for clock in design_clocks:
        if clock["port"] == name:
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
            "clock_period": "`clocks.<name>.period` or `clocks.<name>.freq`",
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
                clock_name = design_clock["name"]
            assert clock_name is not None
            assert clock_obj is not None
            if not clock_obj.port:
                clock_obj.port = design_clock["port"]
            if not clock_obj.name:
                clock_obj.name = clock_name
            clocks[clock_name] = clock_obj
        for clock_name, physical_clock in clocks.items():
            if not physical_clock.port:
                if clock_name not in (clk["name"] for clk in design.rtl.clocks):
                    if design.rtl.clocks:
                        existing = ", ".join(
                            c["name"] for c in design.rtl.clocks if c and c["name"]
                        )
                        msg = (
                            f"Physical clock {clock_name} has no corresponding clock port in the "
                            f"design. Existing clocks: {existing}"
                        )
                    else:
                        described = f"'{clock_name}'" if clock_name else "set by `clocks`"
                        msg = (
                            f"No clock ports specified in `rtl`, while the physical clock "
                            f"{described} is set in the flow settings. Set the design clocks with "
                            "`rtl.clocks`"
                        )
                    raise FlowSettingsError([(None, msg, None, None)], settings_class)
                matched_clock = find_matching_clock(design.rtl.clocks, clock_name)
                if matched_clock:
                    physical_clock.port = matched_clock["port"]
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
                clock_name = clock["name"]
                if clock["port"] not in (c.port for c in self.settings.clocks.values()):
                    log.warning(
                        "No clock period or frequency was specified for clock: %s (design clock port: '%s')",
                        clock_name,
                        clock["port"],
                    )


#: How to give an FPGA flow its device, for `Flow.required_settings`.
FPGA_REQUIRED = (
    "the FPGA device: give its part number with `-s {flow}.fpga.part=<part>` on the command line, "
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
