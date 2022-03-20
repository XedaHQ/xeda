import logging
from pathlib import Path
from typing import Optional
import pkg_resources
import toml
from pydantic import NoneStr
import os
from ...flows.yosys import YosysSynth
from ..flow import FPGA, FpgaSynthFlow

log = logging.getLogger(__name__)


def get_board_data(board):
    board_toml = pkg_resources.resource_string(
        "xeda.data.boards." + board, "board.toml"
    )
    assert board_toml
    board_toml = board_toml.decode("utf-8")
    return toml.loads(board_toml)


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
                clock_period=ss.clock_period,
            ),
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
            fpga = FPGA(get_board_data(board)["fpga"]["part"])
            # TODO from toml
            lpf_cfg = pkg_resources.resource_filename(
                "xeda.data.boards." + board, "board.lpf"
            )
            assert lpf_cfg

        if not isinstance(fpga, FPGA):
            fpga = FPGA(fpga)

        top = self.design.rtl.top

        pnr_tool = f"nextpnr-{fpga.family}"

        freq_mhz = 1000 / ss.clock_period

        pnr_opts = [
            "-q",
            "-l",
            f"{pnr_tool}.log",
            "--json",
            netlist_json,
            "--top",
            top,
            "--freq",
            freq_mhz,
            "--sdf",
            f"{top}.sdf",
            #   '--routed-svg', 'routed.svg',
            # '--seed'
        ]
        if ss.seed is not None:
            pnr_opts.extend(["--seed", ss.seed])
        if fpga.capacity:
            pnr_opts.extend([f"--{fpga.capacity}"])
        if not fpga.package:  # TODO
            fpga.package = "CABGA381"
        pnr_opts.extend(["--package", fpga.package])
        if fpga.speed:
            pnr_opts.extend(["--speed", fpga.speed])

        if lpf_cfg:
            # FIXME check what to do if no board
            pnr_opts += ["--lpf", lpf_cfg, "--textcfg", "config.txt"]

        self.run_process(pnr_tool, pnr_opts)

    def parse_reports(self):
        self.results["success"] = True  # FIXME
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
                fpga=ss.fpga, board=ss.board, clock_period=ss.clock_period
            ),
        )

    def run(self):
        board = self.settings.flow["board"]
        board_data = get_board_data(board)
        fpga = FPGA(board_data["fpga"]["part"])
        bitstream = f"{board}.bit"
        text_cfg = self.completed_dependencies[0].flow_run_dir / "config.txt"
        assert text_cfg.exists()

        packer = None

        if fpga.family == "ecp5":  # FIXME from fpga/board
            packer = "ecppack"

        if packer:
            self.run_process(packer, [str(text_cfg), bitstream])

        self.run_process(
            "openFPGALoader", ["--board", board, "--bitstream", bitstream], nolog=True
        )

    def parse_reports(self):
        self.results["success"] = True  # FIXME
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
