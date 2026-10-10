from typing import TYPE_CHECKING, Any

from .flow import (
    COMMON_RESULT_DESCRIPTIONS,
    Flow,
    FlowDependencyFailure,
    FlowException,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
    describe_results,
    flowrun_hash,
    is_unset,
    registered_flows,
)

# from .decorators import define_flow, sim_flow, synth_flow
from .fpga import FPGA
from ..run_dir import RunDirectoryError
from .io import In, Out
from .synth import AsicSynthFlow, FpgaSynthFlow, PhysicalClock, SynthFlow

if TYPE_CHECKING:
    from .sim import SimFlow as SimFlow

__all__ = [
    "COMMON_RESULT_DESCRIPTIONS",
    "FPGA",
    "AsicSynthFlow",
    "Flow",
    "FlowDependencyFailure",
    "FlowException",
    "FlowFatalError",
    "FlowSettingsError",
    "FlowSettingsException",
    "FpgaSynthFlow",
    "In",
    "Out",
    "PhysicalClock",
    "RunDirectoryError",
    "SimFlow",
    "SynthFlow",
    "describe_results",
    "flowrun_hash",
    "is_unset",
    "registered_flows",
]


def __getattr__(name: str) -> Any:
    # Tool and Cocotb import Flow. Load SimFlow after those modules can finish initializing.
    if name == "SimFlow":
        from .sim import SimFlow

        globals()[name] = SimFlow
        return SimFlow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
