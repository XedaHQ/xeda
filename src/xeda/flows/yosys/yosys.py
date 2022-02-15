
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Tuple, Type
import pkg_resources
from pydantic.fields import Field
import toml
from pydantic import NoneStr, root_validator, validator
import os

from ...flows.ghdl import GhdlSynth
from ...tool import Tool
from ..flow import FPGA, FpgaSynthFlow, SynthFlow

logger = logging.getLogger(__name__)


def get_board_data(board):
    board_toml = pkg_resources.resource_string(
        'xeda.data.boards.' + board, 'board.toml')
    assert board_toml
    board_toml = board_toml.decode('utf-8')
    return toml.loads(board_toml)


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
    class Settings(Yosys.Settings, SynthFlow.Settings):
        log_file: Optional[str] = 'yosys.log'
        flatten: bool = Field(False, description="flatten design")
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
            0, description="enable inference of hard multiplexer resources for muxes at or above this number of inputs (minimum value 2, recommended value >= 5 or disabled = 0)")
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
            False, description="run additional optimization steps after synthesis if complete"
        )
        verilog_lib: List[str] = []
        splitnets: Optional[List[str]] = None  # ['-driver']
        set_attributes: Dict[str, Dict[str, Any]] = {}
        stop_after: Optional[Literal['rtl']]

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
                            logger.critical(
                                f"Decoding of JSON file {attr_file} failed: {e.args}")
                            exit(1)
                        except TypeError as e:
                            logger.critical(f"JSON TypeError: {e.args}")
                            exit(1)
                        except Exception as e:
                            logger.critical(f"Exception: {e.msg}")
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
            # assert 'fpga' in values or 'tech' in values, "ERROR in flows.yosys.settings: No targets specified! Either 'fpga' or 'tech' must be specified."
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
            'netlist': {
            },
            'rtl': {
            },
            'diagrams': {
            },
            'reports': {

                'utilization': self.stat_report,
                'timing': self.timing_report,
            },
        }
        # TODO in runner?
        self.artifacts = SimpleNamespace(**self.artifacts)

        if ss.rtl_json:
            self.artifacts.rtl['json'] = ss.rtl_json
        if ss.rtl_vhdl:
            self.artifacts.rtl['vhdl'] = ss.rtl_vhdl
        if ss.rtl_verilog:
            self.artifacts.rtl['verilog'] = ss.rtl_verilog
        if not ss.stop_after:
            if ss.netlist_json:
                self.artifacts.netlist['json'] = ss.netlist_json
            if ss.netlist_vhdl:
                self.artifacts.netlist['vhdl'] = ss.netlist_vhdl
            if ss.netlist_verilog:
                self.artifacts.netlist['verilog'] = ss.netlist_verilog


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
        logger.info(f"Yosys script: {self.run_path / script_path}")
        # args = ['-s', script_path]
        args = ['-c', script_path]
        if ss.log_file:
            args.extend(['-L', ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(['-T', '-Q', '-q'])
        self.results['_tool'] = self.info  # TODO where should this go?
        logger.info(f"Logging yosys output to {ss.log_file}")
        self.run_tool(self.default_executable, args)
        netlistsvg = True
        skin_file = None
        elk_layout = None
        if netlistsvg:
            rtl_json = self.artifacts.rtl.get('json')
            if rtl_json:
                svg_file = 'rtl.svg'
                self.artifacts.diagrams['netlistsvg_rtl'] = svg_file
                args = [rtl_json, '-o', svg_file]
                if skin_file:
                    args.extend(['--skin', skin_file])
                if elk_layout:
                    args.extend(['--layout', elk_layout])
                self.run_tool('netlistsvg', args, check=True) # ??
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
                    self.artifacts.reports.utilization, r"FDRE\s*(?P<FFs>\d+)", r"number of LCs:\s*(?P<Estimated_LCs>\d+)")
            if self.settings.fpga.family == 'ecp5':
                self.parse_report_regex(
                    self.artifacts.reports.utilization, r"TRELLIS_FF\s+(?P<FFs>\d+)", r"LUT4\s+(?P<LUT4>\d+)")
        self.results['success'] = True  # FIXME
        return True


class NextPnr(FpgaSynthFlow):
    class Settings(FpgaSynthFlow.Settings):
        board: NoneStr = None
        lpf_cfg: Optional[str] = None
        clock_period: float
        seed: Optional[int] = None

    def init(self):
        ss = self.settings
        # board = ss.get('board')
        # board_data = get_board_data(board)
        # print(board_data)
        # fpga_part = board_data['fpga']['part']
        self.add_dependency(
            YosysSynth,
            YosysSynth.Settings(
                fpga=ss.fpga,
                # board=board,  # FIXME
                clock_period=ss.clock_period
            )
        )

    def run(self):
        ss = self.settings
        yosys_flow: YosysSynth = self.completed_dependencies[0]
        netlist = yosys_flow.settings.netlist_json
        if os.path.isabs(netlist):
            netlist_json = Path(netlist)
        else:
            netlist_json = yosys_flow.run_path / netlist

        assert netlist_json.exists(), "netlist json does not exist"

        board = ss.board
        fpga = ss.fpga
        lpf_cfg = ss.lpf_cfg

        if board:
            fpga = FPGA(get_board_data(board)['fpga']['part'])
            # TODO from toml
            lpf_cfg = pkg_resources.resource_filename(
                'xeda.data.boards.' + board, f'board.lpf')
            assert lpf_cfg

        if not isinstance(fpga, FPGA):
            fpga = FPGA(fpga)

        top = self.design.rtl.top

        pnr_tool = f'nextpnr-{fpga.family}'

        freq_mhz = 1000 / ss.clock_period

        pnr_opts = ['-q', '-l', f'{pnr_tool}.log',
                    '--json', netlist_json,
                    '--top', top,
                    '--freq', freq_mhz,
                    '--sdf', f'{top}.sdf',
                    #   '--routed-svg', 'routed.svg',
                    # '--seed'
                    ]
        if ss.seed is not None:
            pnr_opts.extend(['--seed', ss.seed])
        if fpga.capacity:
            pnr_opts.extend([f'--{fpga.capacity}'])
        if not fpga.package:  # TODO
            fpga.package = 'CABGA381'
        pnr_opts.extend(['--package', fpga.package])
        if fpga.speed:
            pnr_opts.extend(['--speed', fpga.speed])

        if lpf_cfg:
            # FIXME check what to do if no board
            pnr_opts += ['--lpf', lpf_cfg, '--textcfg', f'config.txt']

        self.run_process(pnr_tool, pnr_opts)

    def parse_reports(self):
        self.results['success'] = True  # FIXME
        return True


class OpenFpgaLoader(FpgaSynthFlow):
    class Settings(FpgaSynthFlow.Settings):
        board: str
        lpf_cfg: Optional[str] = None
        clock_period: float

    def init(self):
        ss = self.settings
        self.add_dependency(
            NextPnr,
            NextPnr.Settings(
                fpga=ss.fpga,
                board=ss.board,
                clock_period=ss.clock_period
            )
        )

    def run(self):
        board = self.settings.flow['board']
        board_data = get_board_data(board)
        fpga = FPGA(board_data['fpga']['part'])
        bitstream = f'{board}.bit'
        text_cfg = self.completed_dependencies[0].flow_run_dir / 'config.txt'
        assert text_cfg.exists()

        packer = None

        if fpga.family == 'ecp5':  # FIXME from fpga/board
            packer = 'ecppack'

        if packer:
            self.run_process(packer, [str(text_cfg), bitstream])

        self.run_process('openFPGALoader',
                         ['--board', board, '--bitstream', bitstream],
                         nolog=True
                         )

    def parse_reports(self):
        self.results['success'] = True  # FIXME
        return True

# class PllWrapperGen(SynthFlow):
#     def run(self):
#         flow_settings = self.settings.flow

#         board_data = get_board_data(flow_settings.get('board'))

#         freq_mhz = 1000 / flow_settings['clock_period']

#         board_freq = list(board_data['clocks'].values())[0]  # FIXME

#         pll_module = f'__GEN__PLL'
#         pll_verilog_filename = f'{pll_module}.v'

#         self.run_process('ecppll', ['-n', pll_module, '--clkin_name', 'in_clk', '--clkin', board_freq,
#                                     '--clkout0_name', 'out_clk', '--clkout0', freq_mhz, '--file', pll_verilog_filename])

#         self.results['generated_design'] = {'rtl': {'top': 'board_top', 'sources': [self.flow_run_dir / pll_verilog_filename] }}

#         self.results['success'] = True
