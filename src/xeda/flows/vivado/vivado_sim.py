import logging
from typing import List, Optional

from ...dataclass import Field
from ...design import DesignValidationError
from ...flow import SimFlow
from ...utils import SDF
from ..vivado import Vivado

log = logging.getLogger(__name__)


# FIXME: Does not return error when simulation is finished with a failure assertion


class VivadoSim(Vivado, SimFlow):
    """Simulate using Xilinx Vivado simulator (xsim) flow"""

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    # TODO change this?
    # Can run multiple configurations (a.k.a testvectors) in a single run of Vivado through "run_configs"

    class Settings(Vivado.Settings, SimFlow.Settings):
        saif: Optional[str] = Field(
            None,
            description="Write switching activity to this SAIF file, for downstream power "
            "estimation. Implies `elab_debug`.",
        )
        elab_flags: List[str] = Field(
            ["-relax"], description="Extra flags passed to `xelab` during elaboration."
        )
        analyze_flags: List[str] = Field(
            ["-relax"], description="Extra flags passed to `xvlog`/`xvhdl` during analysis."
        )
        sim_flags: List[str] = Field([], description="Extra flags passed to `xsim` at run time.")
        elab_debug: Optional[str] = Field(
            None,
            description='Debug level passed to `xelab -debug`, e.g. "typical" or "all". Set '
            "automatically when `debug`, `saif` or `vcd` is used.",
        )
        sdf: SDF = Field(
            SDF(),
            description="SDF timing-annotation files to back-annotate onto the netlist, per delay "
            "corner (min/typ/max) and optional instance root.",
        )
        optimization_flags: List[str] = Field(
            ["-O3"], description="Optimization flags passed to `xelab`."
        )
        debug_traces: bool = Field(
            False, description="Enable simulator debug tracing. Very verbose and slow."
        )
        prerun_time: Optional[str] = Field(
            None,
            description="Run the simulation for this long before waveform dumping starts, e.g. "
            '"10ns". Useful to skip an initial reset sequence.',
        )
        work_lib: str = Field("work", description="Name of the HDL working library.")
        initialize_zeros: bool = Field(False, description="Initialize all signals with zero")
        xelab_log: Optional[str] = Field(
            "xeda_xelab.log", description="File the elaboration (`xelab`) log is written to."
        )
        vcd_scope: str = Field(
            "",
            description="Hierarchical scope to dump to the VCD. Empty means the whole testbench.",
        )
        vcd_level: int = Field(
            0,
            description="How many hierarchy levels below `vcd_scope` to dump. 0 dumps every level.",
        )
        read_oneshot: bool = Field(
            False,
            description="Analyze all sources in a single tool invocation instead of one per file. "
            "Faster, but gives less precise error locations.",
        )

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.nthreads is not None:
            ss.elab_flags.append(f"-mt {ss.nthreads}")
        elab_debug = ss.elab_debug
        if not elab_debug and (ss.debug or ss.saif or ss.vcd):
            elab_debug = "typical"
        if elab_debug:
            ss.elab_flags.append(f"-debug {elab_debug}")

        if not self.design.tb:
            raise DesignValidationError(
                [
                    (
                        None,
                        "No testbench ('tb') is specified in the design",
                        None,
                        None,
                    )
                ],
                self.design.dict(),
            )
        if not self.design.sim_tops:
            raise DesignValidationError(
                [
                    (
                        None,
                        "VivadoSim requires simulation top but 'tb.top' was not specified in the design",
                        None,
                        None,
                    )
                ],
                self.design.dict(),
            )
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
