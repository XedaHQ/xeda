from importlib.metadata import version, PackageNotFoundError
from pkg_resources import get_distribution, DistributionNotFound

try:
    __version__ = version("xeda")
except PackageNotFoundError:
    # package is not installed
    try:
        __version__ = get_distribution("xeda").version
    except DistributionNotFound:
        # package is not installed
        pass
