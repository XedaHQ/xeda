import logging
from pathlib import Path
from typing import Any, List, Literal, Optional

from ...dataclass import Field, XedaBaseModel
from ...design import SourceType
from ...flow import FlowFatalError, SimFlow
from ...flows.ghdl import GhdlSynth
from .common import YosysBase, process_parameters

log = logging.getLogger(__name__)


class CxxRtl(XedaBaseModel):
    filename: Optional[str] = None
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

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    class Settings(YosysBase.Settings):
        systemverilog: Literal["default", "uhdm", "slang"] = Field(
            "default",
            description="SystemVerilog reader for CXXRTL; the built-in reader generates cells "
            "accepted by write_cxxrtl for common designs.",
        )
        # CXXRTL simulation writes no netlist: the synthesis flows' defaults are cleared, under
        # the same names (and aliases) as theirs.
        netlist_verilog: Optional[Path] = Field(
            None, alias="netlist", description="Unused by CXXRTL simulation."
        )
        netlist_json: Optional[Path] = Field(
            None, alias="json_netlist", description="Unused by CXXRTL simulation."
        )
        cxxrtl: CxxRtl = Field(
            CxxRtl(), description="Options for the generated CXXRTL C++ simulation model."
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys = self.yosys
        if not ss.cxxrtl.filename:
            ss.cxxrtl.filename = f"{self.design.rtl.top or self.design.name}.cpp"
        cxxrtl_cpp = Path(ss.cxxrtl.filename)
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
        )
        log.info("Yosys script: %s", self.run_path / script_path)
        args = [self.script_flag, script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        # `-T -Q` come with the tool's defaults (`yosys`) unless verbose.
        if not ss.verbose and not ss.debug and not ss.is_quiet:
            args.append("-q")
        self.results["_tool"] = yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        yosys.run(*args)

        yosys_config = yosys.derive("yosys-config")
        yosys_include_dir = yosys_config.run_get_stdout("--datdir/include")
        if not yosys_include_dir:
            raise FlowFatalError("yosys-config did not report its include directory.")
        runtime_include = Path(yosys_include_dir) / "backends" / "cxxrtl" / "runtime"
        cxx = yosys.derive("g++")
        assert ss.cxxrtl.filename
        self.artifacts["cxxrtl_cpp"] = cxxrtl_cpp
        if ss.cxxrtl.header:
            self.artifacts["cxxrtl_header"] = cxxrtl_cpp.with_suffix(".h")
        cxx_args: List[Any] = [cxxrtl_cpp] + [
            f.path for f in self.design.sim_sources_of_type(SourceType.Cpp)
        ]
        sim_bin_file = cxxrtl_cpp.with_suffix("")
        cxx_args += ["-std=c++14"]
        cxx_args += ["-o", sim_bin_file]
        cxx_args += [f"-I{runtime_include}"]
        if ss.cxxrtl.header:
            cxx_args += [f"-I{cxxrtl_cpp.parent}"]
        cxx_args += ss.cxxrtl.ccflags
        cxx.run(*cxx_args)
        self.artifacts["simulator"] = sim_bin_file
        sim_bin = yosys.derive(executable=str(Path.cwd() / sim_bin_file))
        sim_bin.run()

    def parse_reports(self) -> bool:
        return True
