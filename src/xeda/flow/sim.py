"""simulation flow"""
from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import List, Optional, Union


from ..dataclass import (
    Field,
    validator,
)
from ..design import Design
from .flow import Flow
from ..cocotb import Cocotb, CocotbSettings

log = logging.getLogger(__name__)

__all__ = [
    "SimFlow",
]


class SimFlow(Flow, metaclass=ABCMeta):
    """superclass of all simulation flows"""

    cocotb_sim_name: Optional[str] = None

    class Settings(Flow.Settings):
        vcd: Union[None, str, Path] = None
        stop_time: Union[None, str, int, float] = None
        cocotb: CocotbSettings = CocotbSettings()  # pyright: reportGeneralTypeIssues=none
        optimization_flags: List[str] = Field([], description="Optimization flags")

        @validator("vcd", pre=True)
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

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        assert isinstance(
            self.settings, self.Settings
        ), "self.settings is not an instance of self.Settings class"
        self.cocotb: Optional[Cocotb] = (
            Cocotb(
                **self.settings.cocotb.dict(),
                sim_name=self.cocotb_sim_name,
                dockerized=self.settings.dockerized,
            )
            if self.cocotb_sim_name and self.design.tb.cocotb
            else None
        )
