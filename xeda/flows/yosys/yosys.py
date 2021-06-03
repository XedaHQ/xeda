
import logging
import pkg_resources
from ..flow import FPGA, SynthFlow
import toml
from pydantic import NoneStr

# from yowasp_yosys import run_yosys

logger = logging.getLogger()


class RecursiveNamespace:
    @classmethod
    def from_toml(cls, s: str):
        return RecursiveNamespace(**toml.load(s))

    @staticmethod
    def map_entry(entry):
        if isinstance(entry, dict):
            return RecursiveNamespace(**entry)
        return entry

    def __init__(self, **kwargs):
        for key, val in kwargs.items():
            if type(val) == dict:
                setattr(self, key, RecursiveNamespace(**val))
            elif type(val) == list:
                setattr(self, key, list(map(self.map_entry, val)))
            else:  # this is the only addition
                setattr(self, key, val)



def get_board_data(board):
    board_toml = pkg_resources.resource_string(
        'xeda.data.boards.' + board, 'board.toml')
    assert board_toml
    board_toml = board_toml.decode('utf-8')
    return toml.loads(board_toml)


class Yosys(SynthFlow):
    class Settings(SynthFlow.Settings):
        clock_period: float = -1


    def run(self):
        flow_settings = self.settings

        fpga = flow_settings.fpga
        
        print(fpga)

        synth_opts = [
            '-abc9',
            # 'nowidelut'
            #  '-dff'
            # '-abc2', '-retime'
        ]

        rtl_check_flags = [] #'-noinit'

        script_path = self.copy_from_template(
            f'yosys.ys', fpga=fpga, 
            rtl_check_flags=" ".join(rtl_check_flags),
            synth_opts=" ".join(synth_opts))
        self.run_process(
            'yosys', ['-q', '-l', 'yosys.log', script_path])

        self.results['success'] = True


class NextPnr(SynthFlow):
    class Settings(SynthFlow):
        board: NoneStr = None

    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):
        board = flow_settings.get('board')
        board_data = get_board_data(board)
        print(cls, flow_settings)
        print(board_data)
        fpga_part = board_data['fpga']['part']
        return {Yosys: (dict(fpga=fpga_part, board=board, clock_period=flow_settings.get('clock_period')), {})}

    def run(self):
        rtl_settings = self.settings.design['rtl']
        flow_settings = self.settings.flow

        yosys_flow = self.completed_dependencies[0]
        netlist_json = yosys_flow.flow_run_dir / f'netlist.json'

        assert netlist_json.exists(), "netlist json does not exist"

        board = flow_settings.get('board')
        fpga = flow_settings.get('fpga')
        lpf_cfg = flow_settings.get('lpf_cfg')

        if board:
            fpga = FPGA(get_board_data(board)['fpga']['part'])
            # TODO from toml
            lpf_cfg = pkg_resources.resource_filename(
                'xeda.data.boards.' + board, f'board.lpf')
            assert lpf_cfg

        if not isinstance(fpga, FPGA):
            fpga = FPGA(fpga)

        top = rtl_settings['top']

        pnr_tool = f'nextpnr-{fpga.family}'

        freq_mhz = 1000 / flow_settings['clock_period']

        pnr_opts = ['-q', '-l', f'{pnr_tool}.log',
                    '--json', netlist_json,
                    '--top', top,
                    f'--{fpga.capacity}',
                    '--package', fpga.package,
                    '--speed', fpga.speed,
                    '--freq', freq_mhz,
                    '--sdf', f'{top}.sdf',
                    #   '--routed-svg', 'routed.svg',
                    # '--seed'
                    ]
        if lpf_cfg:
            # FIXME check what to do if no board
            pnr_opts += ['--lpf', lpf_cfg, '--textcfg', f'config.txt']

        self.run_process(pnr_tool, pnr_opts)

        self.results['success'] = True


class OpenFpgaLoader(SynthFlow):
    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):
        print(cls, flow_settings)
        board = flow_settings.get('board')
        assert board, "board not specified!"
        return {NextPnr: (dict(board=board, clock_period=flow_settings.get('clock_period')), {})}

    def run(self):
        board = self.settings.flow['board']
        board_data = get_board_data(board)
        fpga = FPGA(board_data['fpga']['part'])
        bitstream = f'{board}.bit'
        text_cfg = self.completed_dependencies[0].flow_run_dir / 'config.txt'
        assert text_cfg.exists()

        packer = None

        if fpga.family == 'ecp5': # FIXME from fpga/board
            packer = 'ecppack'

        if packer:
            self.run_process(packer, [str(text_cfg), bitstream])

        self.run_process('openFPGALoader',
                         ['--board', board, '--bitstream', bitstream],
                         nolog=True
                         )

        self.results['success'] = True


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



