import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from box import Box

from ...dataclass import Field, XedaBaseModel, root_validator, validator
from ...design import Design, SourceType
from ...flows.ghdl import GhdlSynth
from ...fpga import FPGA
from ...tool import Docker, Tool
from ..flow import AsicSynthFlow, FpgaSynthFlow, SynthFlow, SimFlow, Flow

log = logging.getLogger(__name__)


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


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


class Yosys(Flow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""

    class Settings(Flow.Settings):
        log_file: Optional[str] = "yosys.log"
        flatten: bool = Field(True, description="flatten design")
        read_verilog_flags: List[str] = [
            "-noautowire",
        ]
        read_systemverilog_flags: List[str] = []
        read_liberty_flags: List[str] = []
        check_assert: bool = True
        rtl_verilog: Optional[str] = None  # "rtl.v"
        rtl_vhdl: Optional[str] = None  # "rtl.vhdl"
        rtl_json: Optional[str] = None  # "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            "-stretch",
            "-enum",
            "-width",
        ]
        ghdl: GhdlSynth.Settings = GhdlSynth.Settings()
        verilog_lib: List[str] = []
        splitnets: Optional[List[str]] = None  # ['-driver']
        set_attributes: Dict[str, Dict[str, Any]] = {}
        plugins: List[str] = []  # ["systemverilog"]
        prep: Optional[List[str]] = None
        write_vhdl_flags: List[str] = [
            # '-norename',
            # '-std08'.
            # '-noattr',
            "-renameprefix YOSYS_n",
            # "-v",
        ]
        write_verilog_flags: List[str] = [
            # '-norename',
            # '-attr2comment',
            # '-noexpr',
            # '-siminit',
            # '-extmem',
            # '-sv',
        ]

        @validator("write_verilog_flags", pre=False)
        def validate_write_verilog_flags(cls, value, values):  # type: ignore
            if values.get("debug"):
                value.append("-norename")
            return value

        @validator("verilog_lib", pre=True)
        def validate_verilog_lib(cls, value, values):  # type: ignore
            if isinstance(value, str):
                value = [value]
            value = [str(Path(v).resolve(strict=True)) for v in value]
            return value

        @validator("splitnets", pre=True)
        def validate_splitnets(cls, value, values):  # type: ignore
            if value is not None:
                if isinstance(value, str):
                    value = [value]
                value = [
                    v if not v.strip() or v.startswith("-") else f"-{v}" for v in value
                ]
            return value

        @validator("set_attributes", pre=True, always=True)
        def validate_set_attributes(cls, value, values):  # type: ignore
            if value:
                if isinstance(value, str):
                    if value.endswith(".json"):
                        attr_file = Path(value)
                        try:
                            log.info("Parsing %s as JSON file", attr_file)
                            with open(attr_file) as f:
                                value = {**json.load(f)}
                        except json.JSONDecodeError as e:
                            raise ValueError(
                                f"Decoding of JSON file {attr_file} failed: {e.args}"
                            ) from e
                        except TypeError as e:
                            raise ValueError(f"JSON TypeError: {e.args}") from e
                    else:
                        raise ValueError(
                            f"Unsupported extension for JSON file: {value}"
                        )
                for attr, attr_dict in value.items():
                    assert attr
                    assert attr_dict, "attr_dict must be a non-empty Dict[str, Any]"
                    for (path, v) in attr_dict.items():
                        assert path and v
                        if isinstance(path, list):
                            path = "/".join(path)
                        if isinstance(v, str):
                            v = f'"{v}"'
                        attr_dict[path] = v
                    value[attr] = dict(attr_dict)
            return value

    def __init__(self, flow_settings: Settings, design: Design, run_path: Path):
        super().__init__(flow_settings, design, run_path)

        assert isinstance(self.settings, self.Settings)
        self.yosys = Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # pyright: reportGeneralTypeIssues=none
        )

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.rtl_json:
            self.artifacts.rtl.json = ss.rtl_json
        if ss.rtl_vhdl:
            self.artifacts.rtl.vhdl = ss.rtl_vhdl
        if ss.rtl_verilog:
            self.artifacts.rtl.verilog = ss.rtl_verilog


class YosysSynth(Yosys, SynthFlow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""

    class Settings(Yosys.Settings, FpgaSynthFlow.Settings, AsicSynthFlow.Settings):
        fpga: Optional[FPGA] = None  # type: ignore
        abc9: bool = Field(True, description="Use abc9")
        retime: bool = Field(False, description="Enable flip-flop retiming")
        nobram: bool = Field(False, description="Do not map to block RAM cells")
        nodsp: bool = Field(False, description="Do not use DSP resources")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`)",
        )
        nowidelut: bool = Field(
            False,
            description="Do not use MUX resources to implement LUTs larger than native for the target",
        )
        dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        widemux: int = Field(
            0,
            description="enable inference of hard multiplexer resources for muxes at or above this number of inputs"
            " (minimum value 2, recommended value >= 5 or disabled = 0)",
        )
        synth_flags: List[str] = []
        abc_flags: List[str] = []
        netlist_verilog: Optional[str] = "netlist.v"
        netlist_vhdl: Optional[str] = None  # "netlist.vhdl"
        netlist_json: Optional[str] = "netlist.json"
        rtl_verilog: Optional[str] = None  # "rtl.v"
        rtl_vhdl: Optional[str] = None  # "rtl.vhdl"
        rtl_json: Optional[str] = None  # "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            "-stretch",
            "-enum",
            "-width",
        ]
        show_netlist: bool = False
        show_netlist_flags: List[str] = ["-stretch", "-enum"]
        post_synth_opt: bool = Field(
            True,
            description="run additional optimization steps after synthesis if complete",
        )
        stop_after: Optional[Literal["rtl"]]

        @validator("write_verilog_flags", pre=False)
        def validate_write_verilog_flags(cls, value, values):  # type: ignore
            if values.get("debug"):
                value.append("-norename")
            return value

        @validator("splitnets", pre=True)
        def validate_splitnets(cls, value, values):  # type: ignore
            if value is not None:
                if isinstance(value, str):
                    value = [value]
                value = [
                    v if not v.strip() or v.startswith("-") else f"-{v}" for v in value
                ]
            return value

        @root_validator(pre=True)
        def check_target(cls, values):  # type: ignore
            # assert values.get("fpga") or values.get(
            #     "tech"
            # ), "ERROR in flows.yosys.settings: No targets specified! Either 'fpga' or 'tech' must be specified."
            return values

    def __init__(self, flow_settings: Settings, design: Design, run_path: Path):
        super().__init__(flow_settings, design, run_path)

        assert isinstance(self.settings, self.Settings)
        self.yosys = Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # pyright: reportGeneralTypeIssues=none
        )
        self.stat_report = (
            "utilization.json" if self.yosys.version_gte(0, 21) else "utilization.rpt"
        )
        self.timing_report = "timing.rpt"
        self.artifacts = Box(
            # non-plural names are preferred for keys, e.g. "digram" instead of "diagrams".
            diagram={},
            report=dict(utilization=self.stat_report, timing=self.timing_report),
            rtl={},
            netlist={},
        )

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)

        yosys_family_name = {"artix-7": "xc7"}

        ss = self.settings
        if ss.fpga:
            assert ss.fpga.family or ss.fpga.vendor == "xilinx"
            if ss.fpga.vendor == "xilinx" and ss.fpga.family:
                ss.fpga.family = yosys_family_name.get(ss.fpga.family, "xc7")
        if ss.rtl_json:
            self.artifacts.rtl.json = ss.rtl_json
        if ss.rtl_vhdl:
            self.artifacts.rtl.vhdl = ss.rtl_vhdl
        if ss.rtl_verilog:
            self.artifacts.rtl.verilog = ss.rtl_verilog
        if not ss.stop_after:  # FIXME
            if ss.netlist_json:
                self.artifacts.netlist.json = ss.netlist_json
            if ss.netlist_vhdl:
                self.artifacts.netlist.vhdl = ss.netlist_vhdl
            if ss.netlist_verilog:
                self.artifacts.netlist.verilog = ss.netlist_verilog

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.sta:
            ss.flatten = True
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")

        # add FPGA-specific synth_xx flags
        if ss.fpga:
            if ss.abc9:  # ABC9 is for only FPGAs?
                append_flag(ss.synth_flags, "-abc9")
            if ss.retime:
                append_flag(ss.synth_flags, "-retime")
            if ss.dff:
                append_flag(ss.synth_flags, "-dff")
            if ss.nobram:
                append_flag(ss.synth_flags, "-nobram")
            if ss.nolutram:
                append_flag(ss.synth_flags, "-nolutram")
            if ss.nodsp:
                append_flag(ss.synth_flags, "-nodsp")
            if ss.nowidelut:
                append_flag(ss.synth_flags, "-nowidelut")
            if ss.widemux:
                append_flag(ss.synth_flags, f"-widemux {ss.widemux}")
            if ss.fpga.vendor == "xilinx":
                ss.write_vhdl_flags.append("-unisim")
        else:
            if ss.dff:
                append_flag(ss.abc_flags, "-dff")
            if ss.tech:
                if ss.tech.gates:
                    append_flag(ss.abc_flags, f"-g {ss.tech.gates}")
                elif ss.tech.liberty:
                    liberty_path = Path(ss.tech.liberty)
                    liberty_path = liberty_path.resolve().absolute()
                    assert (
                        liberty_path.exists()
                    ), f"Specified tech.liberty={liberty_path} does not exist!"
                    ss.tech.liberty = str(liberty_path)
                    append_flag(ss.abc_flags, f"-liberty {ss.tech.liberty}")
                elif ss.tech.lut:
                    append_flag(ss.abc_flags, f"-lut {ss.tech.lut}")

        script_path = self.copy_from_template(
            "yosys_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=True,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
        )
        log.info(
            "Yosys script: %s", self.run_path.relative_to(Path.cwd()) / script_path
        )
        # args = ['-s', script_path]
        args = ["-c", script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(["-T", "-Q", "-q"])
        self.results["_tool"] = self.yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        self.yosys.run(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if self.artifacts.report.utilization.endswith(".json"):
            try:
                with open(self.artifacts.report.utilization, "r") as f:
                    content = f.read()
                i = content.find("{")  # yosys bug
                if i >= 0:
                    content = content[i:]
                utilization = json.loads(content)
                mod_util = utilization.get("modules")
                if mod_util:
                    self.results["module_utilization"] = mod_util
                design_util = utilization.get("design")
                if design_util:
                    num_cells_by_type = design_util.get("num_cells_by_type")
                    if num_cells_by_type:
                        design_util = {
                            **{
                                k: v
                                for k, v in design_util.items()
                                if k != "num_cells_by_type"
                            },
                            **num_cells_by_type,
                        }
                    self.results["design_utilization"] = design_util

            except json.decoder.JSONDecodeError as e:
                log.error(
                    "Failed to decode JSON %s: %s", self.artifacts.report.utilization, e
                )
        else:
            if self.settings.fpga:
                if self.settings.fpga.vendor == "xilinx":
                    self.parse_report_regex(
                        self.artifacts.report.utilization,
                        r"=+\s*design hierarchy\s*=+",
                        r"DSP48(E\d+)?\s*(?P<DSP48>\d+)",
                        r"FDRE\s*(?P<_FDRE>\d+)",
                        r"FDSE\s*(?P<_FDSE>\d+)",
                        r"number of LCs:\s*(?P<Estimated_LCs>\d+)",
                        sequential=True,
                        required=False,
                    )
                    self.results["FFs"] = int(self.results.get("_FDRE", 0)) + int(
                        self.results.get("_FDSE", 0)
                    )
                if self.settings.fpga.family == "ecp5":
                    self.parse_report_regex(
                        self.artifacts.report.utilization,
                        r"TRELLIS_FF\s+(?P<FFs>\d+)",
                        r"LUT4\s+(?P<LUT4>\d+)",
                    )
        return True


class YosysSim(Yosys, SimFlow):
    """Simulate with CXXRTL"""

    class Settings(Yosys.Settings):
        cxxrtl: CxxRtl = CxxRtl()

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        ss.flatten = True
        if not ss.cxxrtl.filename:
            ss.cxxrtl.filename = (
                self.design.rtl.top
                if self.design.rtl.top
                else self.design.name + ".cpp"
            )
        script_path = self.copy_from_template(
            "yosys_sim.tcl",
            lstrip_blocks=True,
            trim_blocks=True,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
        )
        log.info(
            "Yosys script: %s", self.run_path.relative_to(Path.cwd()) / script_path
        )
        # args = ['-s', script_path]
        args = ["-c", script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(["-T", "-Q", "-q"])
        self.results["_tool"] = self.yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        self.yosys.run(*args)

        yosys_config = self.yosys.update(executable="yosys-config")
        yosys_include_dir = yosys_config.run_get_stdout("--datdir/include")
        cxx = self.yosys.update(executable="g++")
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
        self.run_tool(cxx, *cxx_args)
        sim_bin = self.yosys.update(executable=Path.cwd() / sim_bin_file)
        self.run_tool(sim_bin)

    def parse_reports(self) -> bool:
        return True
