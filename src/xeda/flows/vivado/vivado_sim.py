import logging
from pathlib import Path
from typing import List, Literal, Optional

from ...dataclass import WORKING, Field, deliverable
from ...design import DesignValidationError
from ...flow import FlowSettingsException, SimFlow, describe_results
from ...flow.sim import SimEvidence
from ...units import convert_unit
from ...utils import SDF
from ..vivado import Vivado
from .sim_evidence import PROCESS_LOG, RUNTIME_LOG, parse_xsim_evidence

log = logging.getLogger(__name__)


class VivadoSim(Vivado, SimFlow):
    """Simulate using Xilinx Vivado simulator (xsim) flow"""

    results_description = describe_results(
        "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings", "sim.evidence"
    )

    # TODO change this?
    # Can run multiple configurations (a.k.a testvectors) in a single run of Vivado through "run_configs"

    class Settings(Vivado.Settings, SimFlow.Settings):
        timeout: float | None = Field(
            None,
            gt=0,
            description="Stop the simulation-containing Vivado invocation after this many seconds "
            "of wall-clock time, including analysis and elaboration. None sets no limit.",
        )
        fail_severity: Literal["warning", "error", "failure", "fatal"] = Field(
            "error",
            description="Lowest runtime diagnostic severity that fails simulation. "
            "Failure and fatal have the same rank.",
        )
        saif: Optional[Path] = Field(
            None,
            description="Write switching activity to this SAIF file, for downstream power "
            "estimation. Implies `elab_debug`.",
            json_schema_extra=deliverable("outputs/{design}.saif"),
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
            "to at least `typical` to retain native diagnostic source provenance.",
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
        xelab_log: Optional[Path] = Field(
            Path("xeda_xelab.log"),
            description="File the elaboration (`xelab`) log is written to.",
            json_schema_extra=WORKING,
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

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_xsim_evidence(self)

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.elab_debug == "off":
            raise FlowSettingsException(
                "elab_debug=off disables source provenance required by xsim runtime evidence"
            )
        controlled = {
            "-nolog",
            "-onfinish",
            "-onerror",
            "-runall",
            "-R",
            "-tclbatch",
            "-t",
            "-quiet",
            "-maxlogsize",
            "-downgrade_severity",
            "-scNoLogFile",
        }
        # Flags are rendered as Tcl words; existing settings allow several words in an
        # entry. Check every option so grouping cannot hide a runtime evidence control.
        for option in (word for flag in ss.sim_flags for word in flag.split()):
            if option in controlled or option.startswith("-downgrade_"):
                raise FlowSettingsException(
                    f"sim_flags {option} conflicts with xsim runtime evidence"
                )
        for name in ("stop_time", "prerun_time"):
            value = getattr(ss, name)
            if value is not None:
                try:
                    if convert_unit(value, "fs", from_unit="ns") < 0:
                        raise ValueError("negative duration")
                except ValueError as exc:
                    raise FlowSettingsException(
                        f"{name} is not a nonnegative simulation time: {value}"
                    ) from exc
        # The owned native log is required even when ordinary Vivado logging was disabled.
        self.vivado.default_args = [arg for arg in self.vivado.default_args if arg != "-nolog"]

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.nthreads is not None:
            ss.elab_flags.append(f"-mt {ss.nthreads}")
        elab_debug = ss.elab_debug
        # Native $stop carries its HDL source only with elaboration debug. The adapter
        # needs that provenance to distinguish VHDL std.env.stop from Verilog $stop.
        if not elab_debug:
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
                self.design.model_dump(),
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
                self.design.model_dump(),
            )
        if ss.vcd:
            log.info("Dumping VCD to %s", self.run_path / ss.vcd)
        sdf_root = ss.sdf.root
        if not sdf_root:
            sdf_root = self.design.tb.uut
        for delay_type, sdf_file in ss.sdf.delay_items():
            assert sdf_root, "neither SDF root nor tb.uut are provided"
            ss.elab_flags.append(f"-sdf{delay_type} {sdf_root}={sdf_file}")

        # The simulator's work library is made anew, the previous one removed through the run
        # directory. So is the SAIF, which `open_saif` does not replace.
        self.run_directory.remove("xsim.dir")
        if ss.saif:
            self.run_directory.remove(self.run_directory.writable(ss.saif))
        self.run_directory.remove(RUNTIME_LOG, PROCESS_LOG)
        self.run_directory.writable(RUNTIME_LOG)
        process_log = self.run_directory.writable(self.vivado.redirect_stdout or PROCESS_LOG)
        self.vivado.redirect_stdout = None
        stop_fs = (
            int(round(convert_unit(ss.stop_time, "fs", from_unit="ns")))
            if ss.stop_time is not None
            else None
        )
        prerun_fs = (
            int(round(convert_unit(ss.prerun_time, "fs", from_unit="ns")))
            if ss.prerun_time is not None
            else None
        )
        script_path = self.copy_from_template(
            "vivado_sim.tcl", stop_fs=stop_fs, prerun_fs=prerun_fs, runtime_log=RUNTIME_LOG
        )
        # `vivado_sim.tcl` writes these whenever the enabling setting is set; record them so
        # consumers (e.g. `vivado_power`) don't have to guess the path themselves.
        if ss.vcd:
            self.artifacts.vcd = ss.vcd
        if ss.saif:
            self.artifacts.saif = ss.saif
        self.vivado.run(
            "-log",
            RUNTIME_LOG,
            "-source",
            script_path,
            timeout=ss.timeout,
            tee=process_log,
            merge_stderr=True,
        )
