
from types import SimpleNamespace
import pkg_resources
from ...utils import unique_list
from ..flow import DesignSource, SimFlow, Flow, SynthFlow, DebugLevel
import toml

from yowasp_yosys import run_yosys


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


class FPGA:
    def __init__(self, part: str, vendor=None) -> None:
        if part.startswith('LFE'):
            self.vendor = 'Lattice'
            part = part.split('-')
            assert len(part) == 3
            if part[0].startswith('LFE5U'):
                if part[0] == 'LFE5UM':
                    self.family = 'ecp5'  # With SERDES
                    self.has_serdes = True
                if part[0] == 'LFE5UM5G':
                    self.family = 'ecp5-5g'
                elif part[0] == 'LFE5U':
                    self.family = 'ecp5'
                self.capacity = part[1][:-1] + 'k'
                spg = part[2]
                self.speed = spg[0]
                package = spg[1:-1]
                if package.startswith('BG'):
                    package = 'CABGA' + package[2:]
                self.package = package
                self.grade = spg[-1]


class Yosys(SynthFlow):
    def run(self):
        name = self.settings.design['name']
        rtl_settings = self.settings.design['rtl']
        flow_settings = self.settings.flow

        board_name = flow_settings.get('board')
        if board_name:
            board_toml = pkg_resources.resource_string(
                'xeda.data.boards.' + board_name, 'board.toml')
            assert board_toml
            board_toml = board_toml.decode('utf-8')
            board_toml = toml.loads(board_toml)
            board_fpga = board_toml['fpga']  # FIXME
            fpga_part = board_fpga['part']

            # TODO from toml
            lpf_cfg = pkg_resources.resource_filename(
                'xeda.data.boards.' + board_name, f'board.lpf')

            assert lpf_cfg

        else:
            fpga_part = flow_settings.get('fpga')
            if not fpga_part:
                self.fatal(
                    "Either `board` or `fpga` flow settings must be specified.")
            lpf_cfg = None

        fpga = FPGA(fpga_part)

        rtl_settings['top'] = 'board_top'  # FIXME generate board_top wrapper

        top = rtl_settings['top']

        text_cfg = f'{board_name}_out.config'
        bitstream = f'{board_name}.bit'

        freq_mhz = 1000 / flow_settings['clock_period']

        board_freq = 25  # FIXME

        pll_module = f'__GEN_{fpga.family.upper()}_PLL'
        pll_verilog_filename = f'{pll_module}.v'

        self.run_process('ecppll', ['-n', pll_module, '--clkin_name', 'in_clk', '--clkin', board_freq,
                                    '--clkout0_name', 'out_clk', '--clkout0', freq_mhz, '--file', pll_verilog_filename])

        rtl_settings['sources'] = [
            pll_verilog_filename] + rtl_settings['sources']

        synth_opts = [
            '-abc9',
            #  '-dff'
        ]

        script_path = self.copy_from_template(
            f'yosys.ys', fpga=fpga, synth_opts=" ".join(synth_opts))
        self.run_process(
            'yosys', ['-q', '-l', 'yosys.log', script_path])

        pnr_tool = f'nextpnr-{fpga.family}'

        pnr_opts = ['-q', '-l', f'{pnr_tool}.log',
                    '--json', f'{name}.json',
                    '--top', top,
                    f'--{fpga.capacity}',
                    '--package', fpga.package,
                    '--speed', fpga.speed,
                    '--textcfg', text_cfg,
                    '--freq', freq_mhz,
                    '--sdf', f'{top}.sdf',
                    #   '--routed-svg', 'routed.svg',
                    # '--seed'
                    ]
        if lpf_cfg:
            # FIXME check what to do if no board
            pnr_opts += ['--lpf', lpf_cfg]

        self.run_process(pnr_tool, pnr_opts)

        self.run_process('ecppack', [text_cfg, bitstream])
        self.run_process('openFPGALoader',
                         [
                             '--board', board_name, '--bitstream', bitstream], nolog=True)

        self.results['success'] = True
