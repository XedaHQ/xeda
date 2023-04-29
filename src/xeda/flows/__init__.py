# all Flow classes imported here can be used from FlowRunners and will be reported on the command-line help
from __future__ import annotations

import pkgutil
from importlib import import_module
from inspect import isabstract, isclass
from typing import List, Type

from ..flow import Flow
from .bsc import Bsc
from .dc import Dc
from .diamond import DiamondSynth
from .ghdl import GhdlSim, GhdlSynth
from .ise import IseSynth
from .modelsim import Modelsim
from .nextpnr import Nextpnr
from .openfpgaloader import Openfpgaloader
from .openroad import Openroad
from .quartus import Quartus
from .verilator import Verilator
from .vivado.vivado_alt_synth import VivadoAltSynth
from .vivado.vivado_postsynthsim import VivadoPostsynthSim
from .vivado.vivado_power import VivadoPower
from .vivado.vivado_sim import VivadoSim
from .vivado.vivado_synth import VivadoSynth
from .yosys import CxxRtl, Yosys, YosysFpga

__builtin_flows__: List[Type[Flow]] = []

__all__ = [
    "__builtin_flows__",
    "Dc",
    "Bsc",
    "CxxRtl",
    "DiamondSynth",
    "GhdlSim",
    "GhdlSynth",
    "IseSynth",
    "Modelsim",
    "Nextpnr",
    "Openfpgaloader",
    "Openroad",
    "Quartus",
    "Verilator",
    "VivadoAltSynth",
    "VivadoPostsynthSim",
    "VivadoPower",
    "VivadoSim",
    "VivadoSynth",
    "Yosys",
    "YosysFpga",
]

for loader, module_name, is_pkg in pkgutil.walk_packages(__path__):
    module = import_module("." + module_name, __package__)
    for attribute_name in dir(module):
        cls = getattr(module, attribute_name)
        if isclass(cls) and issubclass(cls, Flow) and not isabstract(cls):
            __builtin_flows__.append(cls)
