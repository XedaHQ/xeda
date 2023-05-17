from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import confloat

from ..dataclass import Field, XedaBaseModel, root_validator, validator
from ..design import Design
from ..units import convert_unit
from ..utils import first_value
from .flow import Flow, FlowSettingsError
from .fpga import FPGA

log = logging.getLogger(__name__)

__all__ = [
    "PhysicalClock",
    "SynthFlow",
    "FpgaSynthFlow",
    "AsicSynthFlow",
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
    duty_cycle: confloat(gt=0.0, lt=1.0) = Field(0.5, description="Duty cycle (0.0..1.0)")  # type: ignore
    uncertainty: Optional[float] = Field(None, description="Clock uncertainty")
    skew: Optional[float] = Field(None, description="skew")
    port: Optional[str] = Field(None, description="associated design port")

    @validator("freq", pre=True, always=True)
    def freq_validator(cls, value):
        return convert_unit(value, "MHz")

    @validator("period", "rise", "duty_cycle", "uncertainty", "skew", pre=True, always=True)
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

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def root_validate_phys_clock(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        freq = values.get("freq")
        if freq:
            freq = convert_unit(freq, "MHz")
        if "period" in values:
            period = float(values["period"])
            if freq and abs(float(freq) * period - 1000.0) >= 0.001:
                log.debug(
                    "Mismatching 'freq' and 'period' values were specified. Setting 'freq' from 'period' value."
                )
            values["freq"] = 1000.0 / values["period"]
        else:
            if freq:
                values["period"] = round(1000.0 / float(freq), 3)
            else:
                raise ValueError("Neither freq or period were specified")
        if not values.get("name"):
            values["name"] = "main_clock"
        return values


class SynthFlow(Flow, metaclass=ABCMeta):
    """Superclass of synthesis flows"""

    class Settings(Flow.Settings):
        """base Synthesis flow settings"""

        clock_period: Optional[float] = Field(
            None, description="target clock period in nanoseconds"
        )
        clocks: Dict[str, PhysicalClock] = Field({}, description="Design clocks")
        blacklisted_resources: List[str] = []

        @validator("clocks", pre=True, always=True)
        def _clocks_validate(cls, value, values):  # pylint: disable=no-self-argument
            clock_period = values.get("clock_period")
            if not value and clock_period:
                value = {
                    "main_clock": PhysicalClock(name="main_clock", period=clock_period)  # type: ignore
                }
            return value

        @validator("clock_period", pre=True, always=True)
        def _clock_period_validate(cls, value, values):  # pylint: disable=no-self-argument
            clocks = values.get("clocks")
            if not value and clocks:
                clk = clocks.get("main_clock") or first_value(clocks)
                if clk:
                    value = clk.period
            return value

        @root_validator(pre=True)
        def _synthflow_settings_root_validator(cls, values):
            """
            if we only have 1 clock OR a clock named main_clock:
                clock_period value takes priority for that particular value and overrides that clock's period
            """
            clocks = values.get("clocks")
            clock_period = values.get("clock_period")
            main_clock_name = "main_clock"
            if clocks is None and "clock" in values:
                clocks = {main_clock_name: values.pop("clock")}
            if clocks and (len(clocks) == 1 or main_clock_name in clocks):
                if main_clock_name in clocks:
                    main_clock = clocks[main_clock_name]
                else:
                    main_clock = list(clocks.values())[0]
                    main_clock_name = list(clocks.keys())[0]
                if isinstance(main_clock, PhysicalClock):
                    main_clock = dict(main_clock)
                if clock_period:
                    log.debug("Setting main_clock period to %s", clock_period)
                    main_clock["period"] = clock_period
                clocks[main_clock_name] = PhysicalClock(**main_clock)
            if clocks:
                values["clocks"] = clocks
            return values

        @property
        def main_clock(self) -> Optional[PhysicalClock]:
            return self.clocks.get("main_clock") or first_value(self.clocks)

    def __init__(self, flow_settings: Settings, design: Design, run_path: Path):
        for clock_name, physical_clock in flow_settings.clocks.items():
            if not physical_clock.port:
                if clock_name not in design.rtl.clocks:
                    if design.rtl.clocks:
                        msg = "Physical clock {} has no corresponding clock port in design. Existing clocks: {}".format(
                            clock_name, ", ".join(c for c in design.rtl.clocks)
                        )
                    else:
                        msg = f"No clock ports specified in 'design.rtl', while physical '{clock_name}' is set in flow settings. Set corresponding design clocks via 'design.rtl.clocks' (for multiple clocks) or 'design.rtl.clock.port' (for a single clock)"
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
                physical_clock.port = design.rtl.clocks[clock_name].port
                flow_settings.clocks[clock_name] = physical_clock
        for clock_name, clock in design.rtl.clocks.items():
            if clock_name not in flow_settings.clocks:
                log.critical(
                    "No clock period or frequency was specified for clock: %s (design clock port: '%s')",
                    clock_name,
                    clock.port,
                )
                # raise FlowSettingsError(
                #     [
                #         (
                #             None,
                #             f"No clock period or frequency was specified for clock: '{clock_name}' (clock port: '{clock.port})'",
                #             None,
                #             None,
                #         )
                #     ],
                #     self.Settings,
                # )
        super().__init__(flow_settings, design, run_path)


class FpgaSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of all FPGA synthesis flows"""

    class Settings(SynthFlow.Settings):
        """base FPGA Synthesis flow settings"""

        fpga: FPGA


class AsicSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of ASIC synthesis flows"""

    class Settings(SynthFlow.Settings):
        """base ASIC Synthesis flow settings"""


class DseFlow(Flow, metaclass=ABCMeta):
    """Superclass of design-space exploration flows"""
