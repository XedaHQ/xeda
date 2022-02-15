from . import cli
from .xeda_app import load_design_from_toml

__all_ = [cli, load_design_from_toml]
__project__ = 'xeda'
__author__ = 'Kamyar Mohajerani'
__package__ = 'xeda'

from . import _version
__version__ = _version.get_versions()['version']
