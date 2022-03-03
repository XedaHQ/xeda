from . import _version
from . import cli
from .xeda_app import load_design_from_toml
from .flows.design import Design
from .flows.flow import Flow, Tool, SimFlow, SynthFlow, FPGA

__all_ = [
    cli,
    load_design_from_toml, Design,
    Flow, Tool, SimFlow, SynthFlow, FPGA
]

__project__ = 'xeda'
__author__ = 'Kamyar Mohajerani'
__package__ = 'xeda'

__version__ = _version.get_versions()['version']
