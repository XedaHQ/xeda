# all Flow classes imported here can be used from FlowRunners and will be reported on the command-line help
from .dc import Dc
from .diamond import DiamondSynth
from .ghdl import GhdlSim, GhdlSynth
from .ise import IseSynth
from .modelsim import Modelsim
from .nextpnr import Nextpnr
from .openfpgaloader import Openfpgaloader
from .quartus import Quartus
from .vivado.vivado_synth import VivadoSynth
from .vivado.vivado_sim import VivadoSim
from .vivado.vivado_postsynthsim import VivadoPostsynthSim
from .vivado.vivado_power import VivadoPower
from .vivado.vivado_alt_synth import VivadoAltSynth
from .yosys import Yosys

# alias for backwards compatibility
from .yosys import Yosys as YosysSynth  # pylint: disable=reimported

__all__ = [
    "Dc",
    "DiamondSynth",
    "GhdlSim",
    "GhdlSynth",
    "IseSynth",
    "Modelsim",
    "Nextpnr",
    "Openfpgaloader",
    "Quartus",
    "VivadoAltSynth",
    "VivadoPostsynthSim",
    "VivadoPower",
    "VivadoSim",
    "VivadoSynth",
    "Yosys",
    "YosysSynth",
]
