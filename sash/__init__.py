from pkg_resources import get_distribution, DistributionNotFound

from . import cli

__project__ = 'sash'
__author__ = 'Kamyar Mohajerani'
__package__ = 'sash'

try:
    __version__ = get_distribution(__project__).version
except DistributionNotFound:
    __version__ = '(N/A - Local package)'
