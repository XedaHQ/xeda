from pkg_resources import get_distribution, DistributionNotFound

from . import cli

__project__ = 'xeda'
__author__ = 'Kamyar Mohajerani'
__package__ = 'xeda'

try:
    __version__ = get_distribution(__project__).version
except DistributionNotFound:
    __version__ = '(N/A - Local package)'
