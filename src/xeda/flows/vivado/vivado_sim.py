import logging
from pathlib import Path
from typing import List, Optional

from ...dataclass import WORKING, Field, waveform
from ...design import Design, DesignValidationError, SourceType
from ...flow import Flow, FlowSettingsException, SimFlow, describe_results, NoReplacement
from ...flow.sim import SimEvidence
from ...units import convert_unit, count
from ...utils import SDF
from ..vivado import Vivado
from .sim_evidence import PROCESS_LOG, RUNTIME_LOG, parse_xsim_evidence

log = logging.getLogger(__name__)


class VivadoSim(Vivado, SimFlow):
    """Simulate a design with the Vivado simulator (`xsim`).

    `elab_debug` must keep source information, so that Xeda can tell VHDL `std.env.stop` from
    Verilog `$stop`. Xeda refuses `elab_debug: off`. `stop_time` is an absolute limit across the
    pre-run and the run, measured from the current time that `xsim` reports. Verified with
    Vivado 2024.2.
    """

    #: `xvlog` analyzes Verilog and SystemVerilog, `xvhdl` analyzes VHDL.
    reads_sources = frozenset({SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl})

    results_description = describe_results(
        "sim.ended_by", "sim.time", "sim.errors", "sim.warnings", "sim.evidence"
    )

    # TODO: Vivado can run several configurations (test vectors) in one run through `run_configs`.

    class Settings(Vivado.Settings, SimFlow.Settings):
        removed_settings = {
            **Vivado.Settings.removed_settings,
            "suppress_msgs": NoReplacement(
                "a simulation runs no synthesis, and Xeda passed it to nothing"
            ),
            "initialize_zeros": NoReplacement("Xeda passed it to nothing"),
            "read_oneshot": NoReplacement("Xeda passed it to nothing"),
        }
        saif: Optional[Path] = Field(
            None,
            description="Write the switching activity to this SAIF file, for power estimation. "
            "`true` writes `outputs/<design>.saif`, the name that the run uses when a location "
            "is given.",
            json_schema_extra=waveform("saif", "outputs/{design}.saif"),
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
            description='Debug level passed to `xelab -debug`, for example "typical" or "all". '
            'The default is "typical". Xeda refuses "off", because it needs the source '
            "information to read the diagnostics of the simulator.",
        )
        sdf: SDF = Field(
            SDF(),
            description="SDF files that annotate the netlist with delays, per delay corner "
            "(min, typ, max), and an optional instance root.",
        )
        optimization_flags: List[str] = Field(
            ["-O3"], description="Optimization flags passed to `xelab`."
        )
        debug_traces: bool = Field(
            False,
            description="Enable the debug tracing of the simulator (`ltrace` and `ptrace`). "
            "The output is very verbose and the simulation is slow.",
        )
        prerun_time: Optional[str] = Field(
            None,
            description="Run the simulation for this long before the SAIF file is opened, for "
            'example "10ns". The activity of this time, such as an initial reset sequence, is '
            "not in the SAIF file, and its duration does not include it. The VCD (`vcd`) is not "
            "affected: it records from the start of the simulation.",
        )
        work_lib: str = Field("work", description="Name of the HDL working library.")
        xelab_log: Optional[Path] = Field(
            Path("xeda_xelab.log"),
            description="File the elaboration (`xelab`) log is written to.",
            json_schema_extra=WORKING,
        )
        vcd_scope: str = Field(
            "",
            description="Hierarchical scope to dump to the VCD. Empty means the whole testbench. "
            "Needs `vcd`.",
        )
        vcd_level: int = Field(
            0,
            description="How many hierarchy levels below `vcd_scope` to dump. 0 dumps every level. "
            "Needs `vcd`.",
            json_schema_extra=count(),
        )

    @classmethod
    def check_waveforms(cls, settings: Flow.Settings) -> None:
        """Refuse `vcd_scope` and `vcd_level` without `vcd`."""
        assert isinstance(settings, cls.Settings)
        for name in ("vcd_scope", "vcd_level"):
            if getattr(settings, name) and settings.vcd is None:
                raise FlowSettingsException(f"`{name}` selects what the VCD holds: set `vcd`")

    @classmethod
    def records_activity(cls, settings: Flow.Settings) -> bool:
        """Whether the run records switching activity, which it does for the instance `tb.uut`."""
        assert isinstance(settings, cls.Settings)
        return settings.saif is not None

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design without `tb.uut` when the run records switching activity, whose scope
        is that instance. `SimFlow` judges an SDF file's root."""
        super().check_design_supported(design, settings)
        if settings is None or design.tb.uut:
            return
        assert isinstance(settings, cls.Settings)
        if cls.records_activity(settings):
            raise FlowSettingsException(
                f"{cls.name} records the switching activity of the unit under test, and "
                "`tb.uut` is not set: set `tb.uut` to the instance name of the unit under test "
                "in the testbench"
            )

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        return parse_xsim_evidence(self)

    #: The options of `xsim` that the runtime evidence protocol controls.
    CONTROLLED_SIM_FLAGS = frozenset(
        {
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
    )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse settings that break the runtime evidence protocol or give a bad time."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        cls.check_waveforms(settings)
        if settings.elab_debug == "off":
            raise FlowSettingsException(
                "`elab_debug: off` removes the source information that the xsim runtime evidence "
                "needs"
            )
        # A flag becomes Tcl words, and an entry may hold several words. Check each word, so that
        # grouping cannot hide an option that the runtime evidence protocol controls.
        for option in (word for flag in settings.sim_flags for word in flag.split()):
            if option in cls.CONTROLLED_SIM_FLAGS or option.startswith("-downgrade_"):
                raise FlowSettingsException(
                    f"sim_flags {option} conflicts with xsim runtime evidence"
                )
        for name in ("stop_time", "prerun_time"):
            value = getattr(settings, name)
            if value is not None:
                try:
                    if convert_unit(value, "fs", from_unit="ns") < 0:
                        raise ValueError("negative duration")
                except ValueError as exc:
                    raise FlowSettingsException(
                        f"{name} is not a nonnegative simulation time: {value}"
                    ) from exc

    def init(self) -> None:
        super().init()
        ss = self.settings
        assert isinstance(ss, self.Settings)
        # a flow built directly is not planned
        self.check_settings_supported(ss)
        # The runtime evidence reads Vivado's log, so keep it even when `no_log` is set.
        self.vivado.default_args = [arg for arg in self.vivado.default_args if arg != "-nolog"]

    def run(self) -> None:
        ss = self.settings
        assert isinstance(ss, self.Settings)
        if ss.nthreads is not None:
            ss.elab_flags.append(f"-mt {ss.nthreads}")
        elab_debug = ss.elab_debug
        # A native $stop carries its HDL source only with elaboration debug. The adapter needs
        # the source to tell VHDL std.env.stop from Verilog $stop.
        if not elab_debug:
            elab_debug = "typical"
        if elab_debug:
            ss.elab_flags.append(f"-debug {elab_debug}")

        if not self.design.tb:
            raise DesignValidationError(
                [
                    (
                        None,
                        "The design has no testbench: add a `tb` section",
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
                        "The simulation needs a top module: set `tb.top` in the design",
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
            ss.elab_flags.append(f"-sdf{delay_type} {sdf_root}={sdf_file}")

        # Remove the previous work library through the run directory, so that the simulator
        # makes it anew. Remove the previous SAIF file too: `open_saif` does not replace one.
        self.run_directory.remove("xsim.dir")
        if ss.saif:
            saif = self.run_directory.writable(ss.saif)
            self.run_directory.remove(saif)
            # `open_saif` makes no directory. The conventional name lies in `outputs/`, which
            # exists by now only if the setting names a location (the launcher makes it for the
            # delivery).
            saif.parent.mkdir(parents=True, exist_ok=True)
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
        # `vivado_sim.tcl` writes these files whenever their setting is set. Record them, so that
        # consumers such as `vivado_power` need not guess the path.
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
