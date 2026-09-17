"""simulation flow"""

from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..cocotb import Cocotb, CocotbSettings
from ..dataclass import Field, field_validator
from ..design import Design
from .flow import Flow

log = logging.getLogger(__name__)

__all__ = [
    "SimFlow",
]


class SimFlow(Flow, metaclass=ABCMeta):
    """superclass of all simulation flows"""

    cocotb_sim_name: Optional[str] = None

    class Settings(Flow.Settings):
        vcd: Union[str, Path, None] = Field(
            None,
            alias="waveform",
            description="Write waveform to file",
            validate_default=False,  # v1: no `always=True` -- do not run on the default
        )
        stop_time: Union[str, int, float, None] = Field(
            None,
            description="Stop the simulation at this simulated time. Accepts a number of "
            'nanoseconds or a string with a unit, e.g. "100us".',
        )
        cocotb: CocotbSettings = Field(
            CocotbSettings(),  # type: ignore
            description="Settings for the cocotb testbench, used when design.tb.cocotb is set.",
        )
        optimization_flags: List[str] = Field(
            [],
            description="Extra optimization flags passed to the simulator's compiler/elaborator.",
        )

        @field_validator("vcd", mode="before")
        @classmethod
        def _validate_vcd(cls, vcd):  # pylint: disable=no-self-argument
            if vcd is not None:
                if isinstance(vcd, bool) and vcd is True:
                    vcd = "dump.vcd"
                else:
                    if (
                        isinstance(vcd, str) and vcd[1:].count(".") == 0
                    ):  # if it doesn't have an extension
                        vcd += ".vcd"
            return vcd

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
        self.cocotb: Optional[Cocotb] = (
            Cocotb(
                **self.settings.cocotb.model_dump(),
                sim_name=self.cocotb_sim_name,
                # pydantic-mypy does not see `Tool`'s fields through the
                # `Cocotb(CocotbSettings, Tool)` diamond; `dockerized` is a real field.
                dockerized=self.settings.dockerized,  # type: ignore[call-arg]
            )
            if self.cocotb_sim_name and self.design.tb.cocotb
            else None
        )
