import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from pydantic.fields import Field
from pydantic import NoneStr, root_validator, validator
from munch import Munch

from ...flows.ghdl import GhdlSynth
from ...tool import Tool
from ..flow import FPGA, FpgaSynthFlow, SynthFlow

log = logging.getLogger(__name__)


class Yosys(Tool):
    """Yosys Open SYnthesis Suite: https://yosyshq.net/yosys"""
    docker_image: NoneStr = "hdlc/impl"
    default_executable: NoneStr = "yosys"

    class Settings(Tool.Settings):
        pass


def append_flag(flag_list: List[str], flag: str):
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


class YosysSynth(Yosys, SynthFlow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""
    class Settings(Yosys.Settings, SynthFlow.Settings):
        log_file: Optional[str] = 'yosys.log'
        flatten: bool = Field(True, description="flatten design")
        abc9: bool = Field(True, description="Use abc9")
        retime: bool = Field(False, description="Enable flip-flop retiming")
        nobram: bool = Field(
            False, description="Do not map to block RAM cells")
        nodsp: bool = Field(False, description="Do not use DSP resources")
        nolutram: bool = Field(False, description="Do not use LUT RAM cells")
        sta: bool = Field(
            False, description="Run a simple static timing analysis (requires `flatten`)")
        nowidelut: bool = Field(
            False, description="Do not use MUX resources to implement LUTs larger than native for the target")
        dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        widemux: int = Field(
            0,
            description="enable inference of hard multiplexer resources for muxes at or above this number of inputs"
            " (minimum value 2, recommended value >= 5 or disabled = 0)"
        )
        read_verilog_flags: List[str] = [
            "-noautowire",
        ]
        read_liberty_flags: List[str] = []
        synth_flags: List[str] = []
        abc_flags: List[str] = []
        write_vhdl_flags: List[str] = [
            # '-norename',
            # '-std08'.
            # '-noattr',
            '-renameprefix YOSYS_n',
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
        check_assert: bool = True
        netlist_verilog: Optional[str] = "netlist.v"
        netlist_vhdl: Optional[str] = None  # "netlist.vhdl"
        netlist_json: Optional[str] = "netlist.json"
        rtl_verilog: Optional[str] = "rtl.v"
        rtl_vhdl: Optional[str] = None  # "rtl.vhdl"
        rtl_json: Optional[str] = "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            '-stretch',
            '-enum',
            '-width',
        ]
        show_netlist: bool = False
        show_netlist_flags: List[str] = [
            '-stretch',
            '-enum'
        ]
        ghdl: GhdlSynth.Settings = GhdlSynth.Settings()
        post_synth_opt: bool = Field(
            True, description="run additional optimization steps after synthesis if complete"
        )
        verilog_lib: List[str] = []
        splitnets: Optional[List[str]] = None  # ['-driver']
        set_attributes: Dict[str, Dict[str, Any]] = {}
        stop_after: Optional[Literal['rtl']]
        netlistsvg: Optional[str] = Field(
            None, description="Generate a netlist SVG by runnning 'netlistsvg' (netlistsvg needs to be installed)")

        @validator('write_verilog_flags', pre=False)
        def validate_write_verilog_flags(cls, value, values):
            if values.get('debug'):
                value.append('-norename')
            return value

        @validator('verilog_lib', pre=True)
        def validate_verilog_lib(cls, value, values):
            if isinstance(value, str):
                value = [value]
            value = [str(Path(v).resolve(strict=True)) for v in value]
            return value

        @validator('splitnets', pre=True)
        def validate_splitnets(cls, value, values):
            if isinstance(value, str):
                value = [value]
            return [v if not v.strip() or v.startswith('-') else f'-{v}' for v in value]

        @validator('set_attributes', pre=True, always=True)
        def validate_set_attributes(cls, value, values):
            if value:
                if isinstance(value, str):
                    if value.endswith(".json"):
                        attr_file = Path(value)
                        try:
                            print(f"opening {attr_file}...")
                            with open(attr_file) as f:
                                print(f"loading as json")
                                value = {**json.load(f)}
                                print(
                                    f"converted{attr_file} to {type(value)} \n")
                        except json.JSONDecodeError as e:
                            # raise e from None
                            log.critical(
                                f"Decoding of JSON file {attr_file} failed: {e.args}")
                            exit(1)
                        except TypeError as e:
                            log.critical(f"JSON TypeError: {e.args}")
                            exit(1)
                        except Exception as e:
                            log.critical(f"Exception: {e.msg}")
                            exit(1)
                    else:
                        assert False
                for attr, attr_dict in value.items():
                    assert attr
                    assert attr_dict, "attr_dict must be a non-empty Dict[str, Any]"
                    for (path, v) in attr_dict.items():
                        assert path and v
                        if isinstance(path, list):
                            path = "/".join(path)
                        if isinstance(v, str):
                            v = f"\"{v}\""
                        attr_dict[path] = v
                    value[attr] = dict(attr_dict)
            return value

        @root_validator(pre=True)
        def check_target(cls, values):
            assert values.get('fpga') or values.get(
                'tech'), "ERROR in flows.yosys.settings: No targets specified! Either 'fpga' or 'tech' must be specified."
            return values

    def get_info(self):
        out = self.run_tool(
            self.default_executable,
            ["--version"],
            stdout=True,
        )
        return {'version': out.strip()}

    def init(self):
        # TODO?
        self.stat_report = 'utilization.rpt'
        self.timing_report = 'timing.rpt'

        ss = self.settings
        if ss.fpga:
            assert ss.fpga.family or ss.fpga.vendor == "xilinx"
        # TODO use pydantic or dataclass?
        self.artifacts = {
            'diagrams': {
            },
            'reports': {

                'utilization': self.stat_report,
                'timing': self.timing_report,
            },
        }
        # TODO in runner?
        ar = {}
        if ss.rtl_json:
            ar['json'] = ss.rtl_json
        if ss.rtl_vhdl:
            ar['vhdl'] = ss.rtl_vhdl
        if ss.rtl_verilog:
            ar['verilog'] = ss.rtl_verilog
        if ar:
            self.artifacts['rtl'] = ar
        if not ss.stop_after:  # FIXME
            an = {}
            if ss.netlist_json:
                an['json'] = ss.netlist_json
            if ss.netlist_vhdl:
                an['vhdl'] = ss.netlist_vhdl
            if ss.netlist_verilog:
                an['verilog'] = ss.netlist_verilog
            if an:
                self.artifacts['netlist'] = an

        self.artifacts = Munch.fromDict(self.artifacts)

    def run(self):
        ss = self.settings
        if ss.sta:
            ss.flatten = True

        if ss.flatten:
            append_flag(ss.synth_flags, '-flatten')
        if self.design.rtl.top:
            append_flag(ss.synth_flags, f'-top {self.design.rtl.top}')

        # add FPGA-specific synth_xx flags
        if ss.fpga:
            if ss.abc9:  # ABC9 is for only FPGAs?
                append_flag(ss.synth_flags, '-abc9')
            if ss.retime:
                append_flag(ss.synth_flags, '-retime')
            if ss.dff:
                append_flag(ss.synth_flags, '-dff')
            if ss.nobram:
                append_flag(ss.synth_flags, '-nobram')
            if ss.nolutram:
                append_flag(ss.synth_flags, '-nolutram')
            if ss.nodsp:
                append_flag(ss.synth_flags, '-nodsp')
            if ss.nowidelut:
                append_flag(ss.synth_flags, '-nowidelut')
            if ss.widemux:
                append_flag(ss.synth_flags, f'-widemux {ss.widemux}')
            if ss.fpga.vendor == 'xilinx':
                ss.write_vhdl_flags.append('-unisim')
        else:
            if ss.dff:
                append_flag(ss.abc_flags, '-dff')
            if ss.tech:
                if ss.tech.gates:
                    append_flag(ss.abc_flags, f'-g {ss.tech.gates}')
                elif ss.tech.liberty:
                    liberty_path = Path(ss.tech.liberty)
                    liberty_path = liberty_path.resolve().absolute()
                    assert liberty_path.exists(
                    ), f"Specified tech.liberty={liberty_path} does not exist!"
                    ss.tech.liberty = str(liberty_path)
                    append_flag(ss.abc_flags, f'-liberty {ss.tech.liberty}')
                elif ss.tech.lut:
                    append_flag(ss.abc_flags, f'-lut {ss.tech.lut}')

        script_path = self.copy_from_template('yosys.tcl',
                                              ghdl_args=GhdlSynth.synth_args(
                                                  ss.ghdl,
                                                  self.design
                                              ),
                                              artifacts=self.artifacts,
                                              )
        log.info(f"Yosys script: {self.run_path / script_path}")
        # args = ['-s', script_path]
        args = ['-c', script_path]
        if ss.log_file:
            args.extend(['-L', ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(['-T', '-Q', '-q'])
        self.results['_tool'] = self.info  # TODO where should this go?
        log.info(f"Logging yosys output to {ss.log_file}")
        self.run_tool(self.default_executable, args)
        skin_file = None
        elk_layout = None
        if ss.netlistsvg:
            rtl_json = self.artifacts.rtl.__dict__.get('json')
            if rtl_json:
                svg_file = 'rtl.svg'
                self.artifacts.diagrams.netlistsvg_rtl = svg_file
                args = [rtl_json, '-o', svg_file]
                if skin_file:
                    args.extend(['--skin', skin_file])
                if elk_layout:
                    args.extend(['--layout', elk_layout])
                self.run_tool('netlistsvg', args, check=True)  # ??
            if ss.netlist_json and False:
                svg_file = 'netlist.svg'
                self.artifacts.diagrams['netlistsvg'] = svg_file
                args = [self.artifacts.netlist.json, '-o', svg_file]
                if skin_file:
                    args.extend(['--skin', skin_file])
                if elk_layout:
                    args.extend(['--layout', elk_layout])
                self.run_tool('netlistsvg', args)
        return True

    def parse_reports(self):
        if self.settings.fpga:
            if self.settings.fpga.vendor == 'xilinx':
                self.parse_report_regex(
                    self.artifacts.reports.utilization,
                    r"=== design hierarchy ===",
                    r"FDRE\s*(?P<_FDRE>\d+)",
                    r"FDSE\s*(?P<_FDSE>\d+)",
                    r"number of LCs:\s*(?P<Estimated_LCs>\d+)",
                    sequential=True, required=False
                )
                self.results['FFs'] = int(self.results.get('_FDRE', 0)) + int(self.results.get('_FDSE', 0))
            if self.settings.fpga.family == 'ecp5':
                self.parse_report_regex(
                    self.artifacts.reports.utilization, r"TRELLIS_FF\s+(?P<FFs>\d+)", r"LUT4\s+(?P<LUT4>\d+)")
        self.results['success'] = True  # FIXME
        return True
