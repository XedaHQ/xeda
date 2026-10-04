from . import design, flow_runner, flows
from .cocotb import Cocotb
from .design import Design
from .flow import FPGA, Flow, SimFlow, SynthFlow
from .flow_runner import DefaultRunner, Dse, FlowRunner
from .tool import Tool
from .version import __version__

# P2a remote archive, P1b launch contract, P2b's FPGA graph (`fpga_pack`, and an `openfpgaloader`
# that only programs) and P3's node identity (D-9: a node's identity is its settings plus its
# ordered resolved input origins). Keep this on the imported package so the probe cannot mistake
# another distribution's version metadata for this checkout's capabilities.
REMOTE_PROTOCOL_VERSION = 6

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
