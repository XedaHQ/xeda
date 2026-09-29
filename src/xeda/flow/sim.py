"""simulation flow"""

from __future__ import annotations

import logging
from abc import ABCMeta
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..cocotb import Cocotb, CocotbSettings
from ..dataclass import Field, field_validator
from ..design import Design
from .flow import Flow, FlowSettingsException, registered_flows

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
            description="Write a waveform to this file. `true` writes `dump.vcd`; a name without "
            "an extension gets `.vcd`; `false` or an empty name writes none.",
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
        def _validate_vcd(cls, vcd):
            if vcd is True:
                return "dump.vcd"
            if vcd is False or vcd == "":
                return None
            if isinstance(vcd, (str, Path)) and not Path(vcd).suffix:
                return f"{vcd}.vcd"
            return vcd

    @classmethod
    def check_design_supported(cls, design: Design) -> None:
        """A cocotb testbench needs a simulator xeda drives cocotb on (`cocotb_sim_name`): run on
        any other, the design would be simulated without it and its tests would never run."""
        super().check_design_supported(design)
        if design.tb.cocotb and not cls.cocotb_sim_name:
            supported = sorted(
                {
                    flow_class.name
                    for _, flow_class in registered_flows.values()
                    if issubclass(flow_class, SimFlow) and flow_class.cocotb_sim_name
                }
            )
            raise FlowSettingsException(
                f"{cls.name} cannot run cocotb tests; use one of: {', '.join(supported)}"
            )

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
        # launched, the flow was checked already; constructed directly, it is checked here
        self.check_design_supported(self.design)
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

    def check_results(self) -> bool:
        """Include the cocotb verdict for every simulator that ran a cocotb testbench."""
        if self.cocotb:
            return self.cocotb.add_results(self.results)
        return True

    def always_runs(self) -> Optional[str]:
        if self.cocotb is not None and self.cocotb.random_seed == "random":
            return "it draws a new random seed"
        return super().always_runs()
