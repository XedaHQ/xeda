from importlib.metadata import PackageNotFoundError, version

from pkg_resources import DistributionNotFound, get_distribution

try:
    __version__ = version("xeda")
except PackageNotFoundError:
    # package is not installed
    try:
        __version__ = get_distribution("xeda").version
    except DistributionNotFound:
        # package is not installed
        pass
