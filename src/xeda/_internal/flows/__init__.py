# Every built-in flow is imported here, so launchers can run it and `xeda list-flows` shows it.
from __future__ import annotations

import pkgutil
from importlib import import_module
from inspect import isabstract, isclass
from typing import Dict, List, Type

from ..flow import Flow
from .bsc import Bsc, BscSim
from .dc import Dc
from .diamond import DiamondSynth
from .fpga_pack import FpgaPack
from .ghdl import GhdlSim, GhdlSynth
from .ise import IseSynth
from .iverilog import Iverilog
from .modelsim import Modelsim
from .nextpnr import Nextpnr
from .nvc import Nvc
from .openfpgaloader import Openfpgaloader
from .openroad import Openroad, OpenroadCts, OpenroadGrt, OpenroadPlace
from .quartus import Quartus
from .sby import Sby
from .vcs import Vcs
from .verilator import Verilator
from .vivado.vivado_alt_synth import VivadoAltSynth
from .vivado.vivado_impl import VivadoImpl
from .vivado.vivado_postsynthsim import VivadoPostsynthSim
from .vivado.vivado_power import VivadoPower
from .vivado.vivado_project import VivadoProject
from .vivado.vivado_sim import VivadoSim
from .vivado.vivado_synth import VivadoSynth
from .xezim import Xezim
from .yosys import Yosys, YosysFpga, YosysSim

__builtin_flows__: List[Type[Flow]] = []

__all__ = [
    "Bsc",
    "BscSim",
    "Dc",
    "DiamondSynth",
    "FpgaPack",
    "GhdlSim",
    "GhdlSynth",
    "IseSynth",
    "Iverilog",
    "Modelsim",
    "Nextpnr",
    "Nvc",
    "Openfpgaloader",
    "Openroad",
    "OpenroadCts",
    "OpenroadGrt",
    "OpenroadPlace",
    "Quartus",
    "Sby",
    "Vcs",
    "Verilator",
    "VivadoAltSynth",
    "VivadoImpl",
    "VivadoPostsynthSim",
    "VivadoPower",
    "VivadoProject",
    "VivadoSim",
    "VivadoSynth",
    "Xezim",
    "Yosys",
    "YosysFpga",
    "YosysSim",
    "__builtin_flows__",
]


def _discover_builtin_flows() -> List[Type[Flow]]:
    """Every concrete flow class that a module of this package defines, sorted by flow name.

    A class counts once, at the module that defines it, and only if that module is part of this
    package. A class that a module only imports (`Flow` itself, or a flow of another module)
    does not add a second entry. Two different classes with one flow name are an error.

    The walk starts with the full package name as `prefix`. Without it, `walk_packages` imports
    each subpackage under its bare name, which fails, and it then skips the subpackage silently.
    A module that cannot be imported raises here, since `import_module` runs on every module.
    """
    found: Dict[str, Type[Flow]] = {}
    for module_info in pkgutil.walk_packages(__path__, prefix=__name__ + "."):
        module = import_module(module_info.name)
        for value in vars(module).values():
            if not (isclass(value) and issubclass(value, Flow) and not isabstract(value)):
                continue
            if value.__module__ != __name__ and not value.__module__.startswith(__name__ + "."):
                continue
            other = found.setdefault(value.name, value)
            if other is not value:
                raise ImportError(
                    f"two built-in flows have the name {value.name!r}: "
                    f"{other.__module__}.{other.__qualname__} and "
                    f"{value.__module__}.{value.__qualname__}"
                )
    return [found[name] for name in sorted(found)]


__builtin_flows__.extend(_discover_builtin_flows())
