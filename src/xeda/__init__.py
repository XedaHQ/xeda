from . import design, flow_runner, flows
from .cocotb import Cocotb
from .design import Design
from .flow import FPGA, Flow, SimFlow, SynthFlow
from .flow_runner import DefaultRunner, Dse, FlowRunner
from .tool import Tool
from .version import __version__

# P2a remote archive, P1b launch contract and P2b's FPGA build graph (`fpga_pack`). Keep this on
# the imported package so the probe cannot mistake another distribution's version metadata for
# this checkout's capabilities.
REMOTE_PROTOCOL_VERSION = 4

__all__ = [
    "FPGA",
    "Cocotb",
    "DefaultRunner",
    "Design",
    "Dse",
    "Flow",
    "FlowRunner",
    "REMOTE_PROTOCOL_VERSION",
    "SimFlow",
    "SynthFlow",
    "Tool",
    "__version__",
    "design",
    "flow_runner",
    "flows",
]
