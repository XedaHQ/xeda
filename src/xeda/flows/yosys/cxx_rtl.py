import logging
from pathlib import Path
from typing import Any, List, Literal, Optional

from ...dataclass import Field, XedaBaseModel, deliverable
from ...design import SourceType
from ...flow import FlowFatalError, FlowSettingsException, SimFlow, describe_results
from ...flow.sim import SimEvidence
from ...flow.sim_evidence import read_sim_evidence
from ...flows.ghdl import GhdlSynth
from ...tool import NonZeroExitCode
from .common import YosysBase, process_parameters

log = logging.getLogger(__name__)


class CxxRtl(XedaBaseModel):
    filename: Optional[Path] = Field(
        None,
        description="The C++ file CXXRTL writes the simulation model to; by default "
        "`<top>.cpp`, in the run directory.",
        json_schema_extra=deliverable("outputs/{design}.cpp"),
    )
    header: bool = True
    flatten: bool = True
    hierarchy: bool = True
    proc: bool = True
    debug: Optional[int] = None
    opt: Optional[int] = None
    namespace: Optional[str] = None
    ccflags: List[str] = []


class YosysSim(YosysBase, SimFlow):
    """Simulate with CXXRTL"""

    # Before this flow used the shared tool, its direct Yosys invocation had no version floor.
    minimum_yosys = None

    results_description = describe_results(
        "sim.evidence", "sim.ended_by", "sim.time", "sim.time_unit", "sim.errors", "sim.warnings"
    )
    _end_record: Path | None = None
    _events_record: Path | None = None
    _driver_exit_code: int | None = None

    class Settings(YosysBase.Settings, SimFlow.Settings):
        systemverilog: Literal["default", "uhdm", "slang"] = Field(
            "default",
            description="SystemVerilog reader for CXXRTL; the built-in reader generates cells "
            "accepted by write_cxxrtl for common designs.",
        )
        # CXXRTL simulation writes no netlist: the synthesis flows' defaults are cleared, under
        # the same names (and aliases) as theirs.
        netlist_verilog: Optional[Path] = Field(
            None,
            alias="netlist",
            description="Unused by CXXRTL simulation.",
            json_schema_extra=deliverable("outputs/{design}_netlist.v"),
        )
        netlist_json: Optional[Path] = Field(
            None,
            alias="json_netlist",
            description="Unused by CXXRTL simulation.",
            json_schema_extra=deliverable("outputs/{design}_netlist.json"),
        )
        cxxrtl: CxxRtl = Field(
            CxxRtl(), description="Options for the generated CXXRTL C++ simulation model."
        )

    def init(self):
        assert isinstance(self.settings, self.Settings)
        if self.settings.stop_time is not None:
            raise FlowSettingsException(
                "yosys_sim cannot enforce stop_time: the user-owned C++ driver controls scheduling."
            )
        super().init()

    def has_evidence_adapter(self) -> bool:
        return True

    def simulation_evidence(self) -> SimEvidence | None:
        if self._end_record is None or self._driver_exit_code is None:
            return None
        evidence = read_sim_evidence(self, self._end_record, events_path=self._events_record)
        if evidence is None:
            return None
        # The atexit callback observes execution, but cannot know main's return value.
        # Only an exit envelope with unknown scheduling belongs to this monitor protocol.
        if evidence.ended_by != "exit" or any(
            value is not None
            for value in (evidence.time, evidence.time_unit, evidence.cycles, evidence.exit_code)
        ):
            log.error(
                "Invalid CXXRTL driver end record: expected an exit with unknown time/status."
            )
            return None
        evidence.exit_code = self._driver_exit_code
        return evidence

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.prepare_output_parents()
        yosys = self.yosys
        cxxrtl_filename = ss.cxxrtl.filename or f"{self.design.rtl.top or self.design.name}.cpp"
        cxxrtl_cpp = Path(cxxrtl_filename)
        cxxrtl_cpp.parent.mkdir(parents=True, exist_ok=True)
        simulation_top = self.design.sim_tops[0] if self.design.sim_tops else self.design.rtl.top
        ghdl_top = (
            simulation_top
            if any(src.type is SourceType.Vhdl for src in self.design.tb.sources)
            else self.design.rtl.top
        )
        script_path = self.copy_from_template(
            f"yosys_sim{self.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=False),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            read_tb_sources=True,
            hierarchy_top=simulation_top,
            ghdl_top=ghdl_top,
            cxxrtl_filename=cxxrtl_filename,
        )
        log.info("Yosys script: %s", self.run_path / script_path)
        args = [self.script_flag, script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
            log.info("Logging yosys output to %s", ss.log_file)
        # `-T -Q` come with the tool's defaults (`yosys`) unless verbose.
        if ss.log_file and not ss.verbose and not ss.debug and not ss.is_quiet:
            args.append("-q")
        depfile = self.run_path / "yosys.d"
        args += ["-E", depfile]
        self.depfiles.append(depfile)
        self.results["_tool"] = yosys.info  # TODO where should this go?
        yosys.run(*args)

        yosys_config = yosys.derive(
            "yosys-config", sibling=True, source_name="yosys", redirect_stdout=None
        )
        yosys_include_dir = yosys_config.probe_stdout("--datdir/include")
        if not yosys_include_dir:
            raise FlowFatalError("yosys-config did not report its include directory.")
        runtime_include = Path(yosys_include_dir) / "backends" / "cxxrtl" / "runtime"
        cxx = yosys.derive("g++", redirect_stdout=None)
        self.artifacts["cxxrtl_cpp"] = cxxrtl_cpp
        if ss.cxxrtl.header:
            self.artifacts["cxxrtl_header"] = cxxrtl_cpp.with_suffix(".h")
        cxx_args: List[Any] = [cxxrtl_cpp] + [
            f.path for f in self.design.sim_sources_of_type(SourceType.Cpp)
        ]
        sim_bin_file = cxxrtl_cpp.with_suffix("")
        cxx_args += ["-std=c++14"]
        cxx_args += ["-o", sim_bin_file]
        # Older Yosys CXXRTL output includes headers under backends/cxxrtl/; newer output uses
        # cxxrtl/ inside the runtime directory. Keep both installed include roots available.
        cxx_args += [f"-I{runtime_include}", f"-I{yosys_include_dir}"]
        if ss.cxxrtl.header:
            cxx_args += [f"-I{cxxrtl_cpp.parent}"]
        cxx_args += ss.cxxrtl.ccflags
        self.copy_from_template("sim_record.h")
        monitor_header = self.copy_from_template("cxxrtl_evidence.h")
        monitor = self.copy_from_template("cxxrtl_evidence.cpp")
        # Force the RTL hook into both generated code and drivers including its header.
        # The include remains run-relative under Docker; runtime headers are queried there.
        cxx_args += ["-include", monitor_header, monitor]
        cxx.run(*cxx_args)
        self.artifacts["simulator"] = sim_bin_file
        sim_bin = yosys.derive(executable=str(Path.cwd() / sim_bin_file), redirect_stdout=None)
        self._driver_exit_code = None
        self._end_record = Path("cxxrtl_end.json")
        self._events_record = Path("cxxrtl_events.jsonl")
        self.run_directory.remove(
            self._end_record, str(self._end_record) + ".tmp", self._events_record
        )
        self.run_directory.writable(self._end_record)
        self.run_directory.writable(self._events_record)
        sim_log = self.run_directory.writable("cxxrtl_sim.log")
        try:
            sim_bin.run(
                env={
                    "XEDA_CXXRTL_END_RECORD": str(self._end_record),
                    "XEDA_CXXRTL_EVENTS": str(self._events_record),
                    "XEDA_CXXRTL_FAIL_SEVERITY": ss.fail_severity,
                },
                timeout=ss.timeout,
                tee=sim_log,
                merge_stderr=True,
            )
        except NonZeroExitCode as exc:
            self._driver_exit_code = exc.exit_code
            raise
        self._driver_exit_code = 0

    def parse_reports(self) -> bool:
        return True
