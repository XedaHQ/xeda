from . import design, flow_runner, flows
from .cocotb import Cocotb
from .design import Design
from .flow import FPGA, Flow, SimFlow, SynthFlow
from .flow_runner import DefaultRunner, Dse, FlowRunner
from .tool import Tool
from .version import __version__

# What a remote must understand of this side's requests (`flow_runner.remote`, which describes
# protocol 1); no released xeda before it carried a marker. Raised together with
# `REMOTE_PROTOCOL_MIN_VERSION` once per release cycle, never between releases. Keep this on the
# imported package so the probe cannot mistake another distribution's version metadata for this
# checkout's capabilities.
REMOTE_PROTOCOL_VERSION = 1

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
