import re
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

from ...dataclass import Field
from ...design import DesignValidationError
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

    Handles both RTL and gate-level netlist simulation; a netlist simulation can be annotated
    with timing from an SDF file via the `sdf` setting.
    """

    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_modelsim_evidence(self)

    class Settings(SimFlow.Settings):
        timeout: float | None = Field(
            None,
            gt=0,
            description="Stop the simulation-containing vsim invocation after this many seconds "
            "of wall-clock time and fail. None (the default) sets no limit.",
        )
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
            # `-svinputport=var`: an `input logic` port is a variable, as in other simulators;
            # ModelSim otherwise makes it a net of the default type, an error under
            # `default_nettype none`
            ["-lint", "-svinputport=var"],
            description="Flags passed to `vlog` for every Verilog and SystemVerilog source.",
        )
        vsim_flags: List[str] = Field(
            [], description="Extra flags passed to `vsim` when it loads the simulation tops."
        )
        fail_severity: Literal["warning", "error", "failure", "fatal"] = Field(
            "error",
            description="Fail the run when the simulation reports a message of this severity or "
            "higher: a failed VHDL assertion or the SystemVerilog `$warning`, `$error` or "
            "`$fatal` task. ModelSim reports VHDL `failure` and SystemVerilog `$fatal` with the "
            "same TESTSTATUS, so `fatal` uses the same threshold as `failure`.",
        )

    def run(self) -> None:
        """Compile design sources and execute the ModelSim script."""
        assert isinstance(self.settings, self.Settings)
        tb = self.design.tb
        ss = self.settings
        if not self.design.sim_tops:
            raise DesignValidationError(
                [(None, "ModelSim needs a simulation top: 'tb.top' is not specified", None, None)],
                self.design.model_dump(),
            )
        # vsim's arguments, one word each: the template writes each as a TCL word, so an SDF
        # path with a space is one argument
        vsim_opts: List[str] = list(ss.vsim_flags)
        # TODO are library paths supported?
        for lib_name, _ in ss.lib_paths:  # (name, path) pairs; `-L` takes the name
            if lib_name:
                vsim_opts += ["-L", lib_name]
        sdf_root = ss.sdf.root if ss.sdf.root else tb.uut
        for dt, f in ss.sdf.delay_items():
            assert sdf_root, "Neither settings.sdf.root or design.tb.uut are provided"
            vsim_opts.extend([f"-sdf{dt}", f"{sdf_root}={f}"])
        vsim_opts += [f"-g{k}={v}" for k, v in tb.parameters.items()]

        # The launch logfile includes build diagnostics; the adapter reads only runtime markers.
        # Clear both proof inputs before compilation too: a compile/load failure cannot reuse a preceding successful run.
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
        # vsim contains both build and runtime. Keep all output on failure/timeout, but only
        # the runtime transcript supplies evidence; a general stdout redirect cannot hide it.
        runtime = vsim.derive(vsim.executable, redirect_stdout=None)
        runtime.run(
            *modelsim_opts,
            timeout=ss.timeout,
            tee=self.run_directory.writable("modelsim_process.log"),
            merge_stderr=True,
        )
