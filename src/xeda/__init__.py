"""Xeda's public package API."""

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

from .log_context import TaggingNullHandler
from .version import __version__

# The library writes no log output of its own. The application decides where records go. This
# handler writes nothing: it tags the records of a launch (`log_context.py`).
logging.getLogger(__name__).addHandler(TaggingNullHandler())

_LAZY_EXPORTS = {
    "FPGA": (".flow", "FPGA"),
    "Cocotb": (".cocotb", "Cocotb"),
    "DefaultRunner": (".flow_runner", "DefaultRunner"),
    "Design": (".design", "Design"),
    "Dse": (".flow_runner", "Dse"),
    "Flow": (".flow", "Flow"),
    "FlowRunner": (".flow_runner", "FlowRunner"),
    "SimFlow": (".flow", "SimFlow"),
    "SynthFlow": (".flow", "SynthFlow"),
    "Tool": (".tool", "Tool"),
    "cocotb": (".cocotb", None),
    "design": (".design", None),
    "flow": (".flow", None),
    "flow_runner": (".flow_runner", None),
    "flows": (".flows", None),
    "tool": (".tool", None),
}

__all__ = [
    "FPGA",
    "Cocotb",
    "DefaultRunner",
    "Design",
    "Dse",
    "Flow",
    "FlowRunner",
    "SimFlow",
    "SynthFlow",
    "Tool",
    "__version__",
    "design",
    "flow_runner",
    "flows",
]

if TYPE_CHECKING:
    from . import design as design
    from . import flow_runner as flow_runner
    from . import flows as flows
    from .cocotb import Cocotb as Cocotb
    from .design import Design as Design
    from .flow import FPGA as FPGA
    from .flow import Flow as Flow
    from .flow import SimFlow as SimFlow
    from .flow import SynthFlow as SynthFlow
    from .flow_runner import DefaultRunner as DefaultRunner
    from .flow_runner import Dse as Dse
    from .flow_runner import FlowRunner as FlowRunner
    from .tool import Tool as Tool


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    module = import_module(module_name, __name__)
    value = module if attribute is None else getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | set(_LAZY_EXPORTS))
