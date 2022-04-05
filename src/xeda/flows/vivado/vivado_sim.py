import logging
from typing import List, Optional

from ...dataclass import Field
from ...utils import SDF
from ..flow import SimFlow
from ..vivado import Vivado

log = logging.getLogger(__name__)


# FIXME: Does not return error when simulation is finished with a failure assertion


class VivadoSim(Vivado, SimFlow):
    """Simulate using Xilinx Vivado simulator (xsim) flow"""

    # TODO change this?
    # Can run multiple configurations (a.k.a testvectors) in a single run of Vivado through "run_configs"

    class Settings(Vivado.Settings, SimFlow.Settings):
        saif: Optional[str] = None
        elab_flags: List[str] = ["-relax"]
        analyze_flags: List[str] = ["-relax"]
        sim_flags: List[str] = []
        elab_debug: Optional[str] = None  # TODO choices: "typical", ...
        sdf: SDF = SDF()
        optimization_flags: List[str] = []  # ["-O3"]
        debug_traces: bool = False
        prerun_time: Optional[str] = None
        work_lib: str = "work"
        initialize_zeros: bool = Field(
            False, description="Initialize all signals with zero"
        )
        xelab_log: Optional[str] = "xeda_xelab.log"

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)

        ss.elab_flags.append(f'-mt {"off" if ss.debug else ss.nthreads}')
        elab_debug = ss.elab_debug
        if not elab_debug and (ss.debug or ss.saif or ss.vcd):
            elab_debug = "typical"
        if elab_debug:
            ss.elab_flags.append(f"-debug {elab_debug}")

        assert self.design.tb
        if ss.vcd:
            log.info("Dumping VCD to %s", self.run_path / ss.vcd)
        sdf_root = ss.sdf.root
        if not sdf_root:
            sdf_root = self.design.tb.uut
        for delay_type, sdf_file in ss.sdf.delay_items():
            assert sdf_root, "neither SDF root nor tb.uut are provided"
            ss.elab_flags.append(f"-sdf{delay_type} {sdf_root}={sdf_file}")

        script_path = self.copy_from_template("vivado_sim.tcl")
        self.vivado.run("-source", script_path)
