import logging
from pathlib import Path
from typing import Any, List, Optional

from ...dataclass import XedaBaseModel
from ...design import SourceType
from ...flow import SimFlow
from ...flows.ghdl import GhdlSynth
from ...tool import Docker, Tool
from .common import YosysBase

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

    class Settings(YosysBase.Settings):
        cxxrtl: CxxRtl = CxxRtl()

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys = Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # pyright: reportGeneralTypeIssues=none
        )
        ss.flatten = True
        if not ss.cxxrtl.filename:
            ss.cxxrtl.filename = (
                self.design.rtl.top if self.design.rtl.top else self.design.name + ".cpp"
            )
        script_path = self.copy_from_template(
            "yosys_sim.tcl",
            lstrip_blocks=True,
            trim_blocks=True,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
        )
        log.info("Yosys script: %s", self.run_path.relative_to(Path.cwd()) / script_path)
        # args = ['-s', script_path]
        args = ["-c", script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(["-T", "-Q"])
            if not ss.debug:
                args.append("-q")
        self.results["_tool"] = yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        yosys.run(*args)

        yosys_config = yosys.derive("yosys-config")
        yosys_include_dir = yosys_config.run_get_stdout("--datdir/include")
        cxx = yosys.derive("g++")
        assert ss.cxxrtl.filename
        cxxrtl_cpp = Path(ss.cxxrtl.filename)
        cxx_args: List[Any] = [cxxrtl_cpp] + [
            f.path for f in self.design.sim_sources_of_type(SourceType.Cpp)
        ]
        sim_bin_file = cxxrtl_cpp.with_suffix("")
        cxx_args += ["-std=c++14"]
        cxx_args += ["-o", sim_bin_file]
        cxx_args += [f"-I{yosys_include_dir}"]
        if ss.cxxrtl.header:
            cxx_args += [f"-I{cxxrtl_cpp.parent}"]
        cxx_args += ss.cxxrtl.ccflags
        cxx.run(*cxx_args)
        sim_bin = yosys.derive(executable=Path.cwd() / sim_bin_file)
        sim_bin.run()

    def parse_reports(self) -> bool:
        return True
