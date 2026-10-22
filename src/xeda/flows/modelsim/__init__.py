import re
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Union

from ...dataclass import Field
from ...design import DesignValidationError, SourceType
from ...flow import SimFlow
from ...flow.flow import describe_results
from ...flow.sim import SimEvidence
from ...units import convert_unit
from .sim_evidence import parse_modelsim_evidence
from ...tool import Docker, Tool
from ...utils import SDF

# ModelSim's TESTSTATUS groups VHDL failure and SystemVerilog $fatal at 3. This differs from
# BreakOnAssertion, where fatal is 4.
TEST_STATUS: Dict[str, int] = {"note": 0, "warning": 1, "error": 2, "failure": 3, "fatal": 3}


class ModelsimTool(Tool):
    """ModelSim's `vsim`, which also runs the `vlog`/`vcom` compilers from its TCL scripts."""

    executable: str = "vsim"
    # `vsim -version`: "Model Technology ModelSim ... vsim 2020.1 Simulator 2020.02 Feb 28 2020"
    version_flag: Optional[List[str]] = ["-version"]
    version_regexps: List[Union[re.Pattern[str], str]] = [r"\bvsim\s+(?P<version>\d+(\.\d+)+)"]
    # ModelSim-Intel FPGA Starter Edition 2020.1: free, needs no license, `vsim` on PATH, no
    # entrypoint. The image is amd64-only; the platform is explicit so an arm64 host emulates it
    # without Docker's platform-mismatch warning.
    docker: Optional[Docker] = Docker(
        image="chaseruskin/modelsim-intel:20.1.1-ubuntu-22.04",
        command=["vsim"],
        platform="linux/amd64",
    )  # pyright: ignore


class Modelsim(SimFlow):
    """Simulate a VHDL, Verilog, SystemVerilog or mixed-language design with Siemens ModelSim.

    Simulate RTL and gate-level netlists. A netlist simulation can use the timing of SDF files
    (`sdf` setting). A run passes only on evidence from this run: the native stop reason, the
    time and a TESTSTATUS checkpoint, plus the bounded runtime log file. VHDL `std.env.stop` is
    accepted when the evidence shows a native break from a known VHDL source. Verilog `$stop`
    fails at the default `fail_severity`. Verified with ModelSim-Intel Starter 2020.1.
    """

    executables = ("vsim",)

    #: `vlog` compiles Verilog and SystemVerilog, `vcom` compiles VHDL.
    reads_sources = frozenset({SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl})

    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.errors", "sim.warnings"
    )

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_modelsim_evidence(self)

    class Settings(SimFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **SimFlow.Settings.removed_settings,
            "optimization_flags": "`vsim_flags`, which reach `vsim`, where ModelSim optimizes the design "
            "(`-voptargs=...`)",
        }
        sdf: SDF = Field(
            SDF(),
            description="SDF timing-annotation files to back-annotate onto the netlist, per delay "
            "corner (min/typ/max) and optional instance root.",
        )
        modelsimini: Optional[Path] = Field(
            None,
            description="Path to a `modelsim.ini` to use instead of the tool default, e.g. one "
            "with pre-compiled vendor libraries mapped.",
        )
        vcom_flags: List[str] = Field(
            ["-lint"], description="Flags passed to `vcom` for every VHDL source."
        )
        vlog_flags: List[str] = Field(
            # `-svinputport=var`: an `input logic` port is a variable, as in other simulators.
            # Without it, ModelSim makes the port a net of the default type, which is an error
            # under `default_nettype none`.
            ["-lint", "-svinputport=var"],
            description="Flags passed to `vlog` for every Verilog and SystemVerilog source.",
        )
        vsim_flags: List[str] = Field(
            [], description="Extra flags passed to `vsim` when it loads the simulation tops."
        )

    def run(self) -> None:
        """Compile the sources and run the ModelSim script."""
        assert isinstance(self.settings, self.Settings)
        tb = self.design.tb
        ss = self.settings
        if not self.design.sim_tops:
            raise DesignValidationError(
                [(None, "ModelSim needs a simulation top module: `tb.top` is not set", None, None)],
                self.design.model_dump(),
            )
        # The arguments of `vsim`, one word each. The template writes each as a TCL word, so an
        # SDF path with a space stays one argument.
        vsim_opts: List[str] = list(ss.vsim_flags)
        # TODO: check whether library paths work.
        for lib_name, _ in ss.lib_paths:  # (name, path) pairs; `-L` takes the name
            if lib_name:
                vsim_opts += ["-L", lib_name]
        sdf_root = ss.sdf.root if ss.sdf.root else tb.uut
        for dt, f in ss.sdf.delay_items():
            vsim_opts.extend([f"-sdf{dt}", f"{sdf_root}={f}"])
        vsim_opts += [f"-g{k}={v}" for k, v in tb.parameters.items()]

        # The launch log file includes build diagnostics, but the adapter reads only the runtime
        # markers. Remove the evidence files (`modelsim_runtime.log`, `modelsim_end.txt`) and the
        # process log before compilation, so that a compile or load failure cannot reuse the
        # files of an earlier successful run.
        self.run_directory.remove(
            "modelsim_runtime.log", "modelsim_end.txt", "modelsim_process.log"
        )
        self.run_directory.writable("modelsim_runtime.log")
        self.run_directory.writable("modelsim_end.txt")
        stop_time = (
            str(convert_unit(ss.stop_time, "ps", from_unit="ns")) + "ps"
            if ss.stop_time is not None
            else None
        )
        script_path = self.copy_from_template(
            "run.tcl",
            vcom_opts=ss.vcom_flags,
            vlog_opts=ss.vlog_flags,
            vsim_opts=vsim_opts,
            fail_status=TEST_STATUS[ss.fail_severity],
            stop_time=stop_time,
        )

        modelsim_opts = ["-batch", "-logfile", "modelsim_runtime.log", "-do", f"do {script_path}"]
        if ss.modelsimini:
            modelsim_opts.extend(["-modelsimini", str(ss.modelsimini)])
        vsim = ModelsimTool()
        # One `vsim` run does both the build and the simulation. Keep all its output on failure
        # or timeout. Only the runtime transcript supplies evidence. A general stdout redirect
        # (`redirect_stdout`) could hide that transcript, so the derived tool sets none.
        runtime = vsim.derive(vsim.executable, redirect_stdout=None)
        runtime.run(
            *modelsim_opts,
            timeout=ss.timeout,
            tee=self.run_directory.writable("modelsim_process.log"),
            merge_stderr=True,
        )
