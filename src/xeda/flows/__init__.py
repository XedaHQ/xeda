# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

# all Flow classes imported here can be used from FlowRunners and will be reported on the command-line help
from .vivado.vivado_project import VivadoPrjSynth
from .vivado.vivado_synth import VivadoSynth
from .vivado.vivado_sim import VivadoSim
from .ise import IseSynth
# from .quartus import QuartusSynth
# from .diamond import DiamondSynth
from .ghdl import GhdlSim
# from .modelsim import Modelsim
# from .dc import Dc
# from .yosys import Yosys, NextPnr, OpenFpgaLoader
from .yosys import Yosys