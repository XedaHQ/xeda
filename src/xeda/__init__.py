from . import _version
from . import cli
from .flows.design import Design
from .flows.flow import Flow, Tool, SimFlow, SynthFlow, FPGA

__all_ = [
    cli,
    Design,
    Flow, Tool, SimFlow, SynthFlow, FPGA
]

__project__ = 'xeda'
__author__ = 'Kamyar Mohajerani'
__package__ = 'xeda'

__version__ = _version.get_versions()['version']
