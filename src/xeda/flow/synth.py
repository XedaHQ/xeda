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
    period: float = Field(
        description="Clock period (ns). Either (and only one of) 'period' OR 'freq' have to be specified."
    )
    freq: float = Field(
        description="Clock frequency (MHz). Either (and only one of) 'period' OR 'freq' have to be specified."
    )
    rise: float = Field(0.0, description="Rising time of clock (ns)")
    duty_cycle: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(0.5, description="Duty cycle (0.0..1.0)")  # type: ignore
    uncertainty: Optional[float] = Field(None, description="Clock uncertainty")
    skew: Optional[float] = Field(None, description="skew")
    port: Optional[str] = Field(None, description="associated design port")

    @field_validator("freq", mode="before")
    @classmethod
    def freq_validator(cls, value):
        return convert_unit(value, "MHz")

    @field_validator("period", "rise", "duty_cycle", "uncertainty", "skew", mode="before")
    @classmethod
    def time_validator(cls, value):
        if value is not None:
            return convert_unit(value, "nanosecond")
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
    def period_ps(self) -> float:
        if not self.period:
            return 0
        return convert_unit(self.period, to_unit="picosecond", from_unit="nanosecond")

    @period_ps.setter
    def period_ps(self, period):
        self.period = convert_unit(period, to_unit="picosecond", from_unit=None)

    def period_unit(self, unit: str) -> float:
        unit = unit.strip()
        if not unit:
            return self.period
        return convert_unit(self.period, to_unit=unit, from_unit="nanosecond")

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
        if assignment_field is not None and assignment_field not in ("freq", "period"):
            # Assignment validation re-runs this whole-model validator. Only the two correlated
            # fields may reconcile one another; re-deriving 300 MHz from its rounded 3.333 ns
            # period while assigning `rise` changed it to 300.030003... MHz.
            return values
        freq = values.get("freq")
        if freq is not None:
            freq = convert_unit(freq, "MHz")
        period = values.get("period")
        if assignment_field == "freq":
            if freq is None:
                raise ValueError("Clock frequency must be specified")
            freq = float(freq)
            if freq <= 0:
                raise ValueError(f"Clock frequency must be positive, got {freq}")
            values["period"] = round(1000.0 / freq, 3)
            values["freq"] = freq
        elif period is not None:
            period = convert_unit(period, "nanosecond")
            if period <= 0:
                raise ValueError(f"Clock period must be positive, got {period}")
            if freq is not None and abs(float(freq) * period - 1000.0) >= 0.001:
                log.debug(
                    "Mismatching 'freq' and 'period' values were specified. Setting 'freq' from 'period' value."
                )
            values["period"] = period
            values["freq"] = 1000.0 / period
        else:
            if freq is not None:
                freq = float(freq)
                if freq <= 0:
                    raise ValueError(f"Clock frequency must be positive, got {freq}")
                values["period"] = round(1000.0 / freq, 3)
                values["freq"] = freq
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

        clock_period: Optional[float] = Field(
            None, description="target clock period in nanoseconds"
        )
        clocks: Dict[str, PhysicalClock] = Field({}, description="Design clocks")

        @field_validator("clocks", mode="before")
        @classmethod
        def _clocks_validate(cls, value, info):  # pylint: disable=no-self-argument
            values = info.data if isinstance(info.data, dict) else {}
            clock_period = values.get("clock_period")
            if not value and clock_period:
                value = {
                    "main_clock": PhysicalClock(name="main_clock", period=clock_period)  # type: ignore
                }
            return value

        @field_validator("clock_period", mode="before")
        @classmethod
        def _clock_period_validate(cls, value, info):  # pylint: disable=no-self-argument
            values = info.data if isinstance(info.data, dict) else {}
            if value is not None:
                # Validated here rather than left to `PhysicalClock`: a `clock_period` that never
                # reaches a clock (because `clocks` was given too) would otherwise be accepted
                # however non-positive it is. `is not None`, not truthiness -- 0 is a value the
                # user supplied, not an omission.
                period = convert_unit(value, "nanosecond")
                if period <= 0:
                    raise ValueError(f"Clock period must be positive, got {period}")
                return period
            clocks = values.get("clocks")
            if clocks:
                clk = clocks.get("main_clock") or first_value(clocks)
                if clk:
                    value = clk.period
            return value

        @model_validator(mode="before")
        @classmethod
        def _synthflow_settings_root_validator(cls, values, info):
            """
            if we only have 1 clock OR a clock named main_clock:
                clock_period value takes priority for that particular value and overrides that clock's period
            """
            assignment_field = info.field_name if info.data is None else None
            clocks = values.get("clocks")
            # main_clock_name = "main_clock"
            clock = values.pop("clock", None)
            clock_period = values.get("clock_period")
            if (not clocks) and (clock or clock_period):
                if not clock:
                    clock = {"period": clock_period}
                if not isinstance(clock, PhysicalClock):
                    assert isinstance(
                        clock, dict
                    ), "clock should be a dictionary or PhysicalClock instance"
                    if clock_period:  # overrides the period value
                        clock["period"] = clock_period
                    clock = PhysicalClock(**clock)
                # if not clock.name:
                #     clock.name = main_clock_name
                clocks = {clock.name: clock}
            #     if clocks and (len(clocks) == 1 or main_clock_name in clocks):
            #         if main_clock_name in clocks:
            #             main_clock = clocks[main_clock_name]
            #         else:
            #             main_clock = list(clocks.values())[0]
            #             main_clock_name = list(clocks.keys())[0]
            #         if isinstance(main_clock, PhysicalClock):
            #             main_clock = dict(main_clock)
            #         if clock_period:
            #             log.debug("Setting main_clock period to %s", clock_period)
            #             main_clock["period"] = clock_period
            #         clocks[main_clock_name] = PhysicalClock(**main_clock)
            if clocks and not isinstance(clocks, dict):
                # e.g. `clocks = ["clk"]`: leave it for the `clocks` field to reject with a proper
                # "valid dictionary" error rather than crash on `.get` below.
                return values
            if clocks:
                values["clocks"] = clocks
                main_name = "main_clock" if "main_clock" in clocks else first_key(clocks)
                main = clocks.get(main_name) if main_name is not None else None
                validated_main = main
                if isinstance(main, dict):
                    try:
                        validated_main = PhysicalClock(**main)
                    except (ValueError, TypeError):
                        validated_main = None  # reported by `clocks`' own validation

                if assignment_field == "clocks":
                    # A newly assigned clock is authoritative; keep the compatibility scalar in
                    # sync rather than retaining the previously selected period.
                    if isinstance(validated_main, PhysicalClock):
                        values["clock_period"] = validated_main.period
                elif assignment_field in (None, "clock_period"):
                    if clock_period is not None:
                        # During construction, or when `clock_period` itself is assigned, the
                        # scalar shorthand is authoritative as documented. This is correlated
                        # state, not unrelated-field detachment.
                        if isinstance(main, PhysicalClock):
                            main.period = clock_period
                        elif isinstance(main, dict) and main_name is not None:
                            main["period"] = clock_period
                            clocks[main_name] = main
                    elif isinstance(validated_main, PhysicalClock):
                        # Back-fill `clock_period` from the main clock. The field-level validator
                        # cannot do this because it is declared before `clocks`.
                        values["clock_period"] = validated_main.period
                # Any other assignment leaves the clock/period pair alone: this validator runs on
                # *every* assignment, and re-imposing `clock_period` there would silently revert
                # a direct edit of `settings.clocks[...].period`.
            return values

        @property
        def main_clock(self) -> Optional[PhysicalClock]:
            return self.clocks.get("main_clock") or first_value(self.clocks)

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


class FpgaSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of all FPGA synthesis flows"""

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
