import logging
import re
from typing import Any, Dict, Literal, Optional
from pydantic import root_validator
import pkg_resources

from ...flows.yosys import Yosys
from ..flow import FPGA, FpgaSynthFlow
from ...utils import toml_loads
from ...tool import Tool

log = logging.getLogger(__name__)


def get_board_data(board: str) -> Optional[Dict[str, Any]]:
    board_toml_bytes = pkg_resources.resource_string(
        "xeda.data.boards." + board, "board.toml"
    )
    if board_toml_bytes:
        board_toml = board_toml_bytes.decode("utf-8")
        return toml_loads(board_toml)
    return None


class WithFpgaBoardSettings(FpgaSynthFlow.Settings):
    board: Optional[str] = None

    @root_validator(pre=True)
    def fpga_validate(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        board_name = values.get("board")
        fpga = values.get("fpga")
        if not fpga and board_name:
            board_data = get_board_data(board_name)
            if board_data:
                board_fpga = board_data.get("fpga")
                log.info("FPGA info for board %s: %s", board_name, str(board_fpga))
                if board_fpga:
                    values["fpga"] = FPGA(**board_fpga)
        return values


class NextPnr(FpgaSynthFlow):
    class Settings(WithFpgaBoardSettings):
        lpf_cfg: Optional[str] = None
        clock_period: float
        seed: Optional[int] = None

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.add_dependency(
            Yosys,
            Yosys.Settings(
                fpga=ss.fpga,
                clock_period=ss.clock_period,
            ),
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_flow = self.completed_dependencies[0]
        assert isinstance(yosys_flow, Yosys)
        assert isinstance(yosys_flow.settings, Yosys.Settings)
        assert yosys_flow.settings.netlist_json
        netlist_json = yosys_flow.run_path / yosys_flow.settings.netlist_json
        fpga_family = ss.fpga.family if ss.fpga.family else "generic"
        # FIXME:
        # ["generic", "ecp5", "ice40", "nexus", "gowin", "fpga-interchange", "xilinx"]
        next_pnr = Tool(
            executable=f"nextpnr-{fpga_family}"
        )  # pyright: reportGeneralTypeIssues=none

        assert (
            netlist_json.exists()
        ), f"netlist json file {netlist_json} does not exist!"

        lpf_cfg = ss.lpf_cfg
        if not lpf_cfg and ss.board:
            # TODO from toml
            lpf_cfg = pkg_resources.resource_filename(
                "xeda.data.boards." + ss.board, "board.lpf"
            )
        assert lpf_cfg

        freq_mhz = 1000 / ss.clock_period

        args = [
            "-q",
            "-l",
            "next_pnr.log",
            "--json",
            netlist_json,
            "--freq",
            freq_mhz,
            "--sdf",
            f"nextpnr.sdf",
            #   '--routed-svg', 'routed.svg',
            # '--seed'
        ]
        if self.design.rtl.top:
            args.extend(["--top", self.design.rtl.top[0]])
        if ss.seed is not None:
            args.extend(["--seed", ss.seed])

        package = ss.fpga.package
        speed = ss.fpga.speed
        device_type = None
        if speed:
            args.extend(["--speed", speed])
        if ss.fpga.part:
            part = ss.fpga.part.strip().upper().split("-")
            if len(part) >= 2 and part[0].startswith("LFE5U"):
                if not ss.fpga.family:
                    ss.fpga.family = "ecp5"
                assert ss.fpga.family == "ecp5"
                if not ss.fpga.capacity and re.match(r"\d\dF", part[1]):
                    ss.fpga.capacity = part[1][:2] + "k"
                if part[0] == "LFE5UM":
                    device_type = "um"
                elif part[0] == "LFE5UM5G":
                    device_type = "um5g"
            if not package and len(part) >= 3:  # FIXME
                if part[2][1:-1] == "BG381":
                    package = "CABGA381"
        if ss.fpga.capacity:
            if device_type:
                args.append(f"--{device_type}-{ss.fpga.capacity}")
            else:
                args.append(f"--{ss.fpga.capacity}")
        if package:
            args.extend(["--package", package])
        if lpf_cfg:
            # FIXME check what to do if no board
            args += ["--lpf", lpf_cfg, "--textcfg", "config.txt"]

        next_pnr.run(*args)


class OpenFpgaLoader(FpgaSynthFlow):
    ofpga_loader = Tool(executable="openFPGALoader")

    class Settings(WithFpgaBoardSettings):
        board: str
        lpf_cfg: Optional[str] = None
        clock_period: float
        reset: bool = False

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.add_dependency(
            NextPnr,
            NextPnr.Settings(
                fpga=ss.fpga, board=ss.board, clock_period=ss.clock_period
            ),
        )

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        board_name = ss.board
        bitstream = f"{board_name}.bit"
        next_pnr = self.completed_dependencies[0]
        assert isinstance(next_pnr, NextPnr)
        text_cfg = self.completed_dependencies[0].run_path / "config.txt"
        assert text_cfg.exists()

        packer = None

        if ss.fpga.family == "ecp5":  # FIXME from fpga/board
            packer = Tool(executable="ecppack")

        if packer:
            packer.run(text_cfg, bitstream)
        args = ["--board", board_name, "--bitstream", bitstream]
        if ss.fpga.part:
            args.extend(["--fpga-part", ss.fpga.part])
        if ss.reset:
            args.append("--reset")
        self.ofpga_loader.run(*args)


#         ('ecppll', ['-n', pll_module, '--clkin_name', 'in_clk', '--clkin', board_freq,
#                                     '--clkout0_name', 'out_clk', '--clkout0', freq_mhz, '--file', pll_verilog_filename])
