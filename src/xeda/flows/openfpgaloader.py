import logging
from typing import Optional

from ..board import WithFpgaBoardSettings, get_board_data
from ..dataclass import validator
from ..flow import FlowSettingsException, FpgaSynthFlow
from ..tool import Tool
from .nextpnr import Nextpnr

__all__ = ["Openfpgaloader"]

log = logging.getLogger(__name__)


class Openfpgaloader(FpgaSynthFlow):
    ofpga_loader = Tool("openFPGALoader")

    class Settings(WithFpgaBoardSettings):
        clock_period: float
        reset: bool = False
        cable: Optional[str] = None
        nextpnr: Optional[Nextpnr.Settings] = None

        @validator("nextpnr", always=True, pre=True)
        def _validate_nextpnr(cls, value, values):
            clocks = values.get("clocks")
            fpga = values.get("fpga")
            board = values.get("board")
            if value is None:
                value = {}
            if isinstance(value, Nextpnr.Settings):
                value = value.dict()
            assert isinstance(value, (dict)), f"not a dict: {value}"
            value["fpga"] = fpga
            value["board"] = board
            value["clocks"] = clocks
            return Nextpnr.Settings(**value)

    def init(self) -> None:
        self.packer: Optional[Tool] = None
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        assert ss.nextpnr is not None
        self.add_dependency(Nextpnr, ss.nextpnr)
        if ss.fpga is None:
            raise FlowSettingsException("")
        if ss.fpga.family == "ecp5":  # FIXME from fpga/board
            self.packer = Tool("ecppack")

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        board_id = ss.board
        board_name = None
        if board_id:
            board_data = get_board_data(board_id)
            if board_data:
                board_name = board_data.get("name")
        next_pnr = self.completed_dependencies[0]
        assert isinstance(next_pnr, Nextpnr)
        assert isinstance(next_pnr.settings, Nextpnr.Settings)
        assert next_pnr.settings.textcfg
        text_cfg = next_pnr.run_path / next_pnr.settings.textcfg
        assert text_cfg.exists(), f"Can't find {text_cfg} generated by Nextpnr!"
        bitstream = f"{board_name}.bit" if board_name else "bitstream.bit"
        if self.packer:
            self.packer.run(text_cfg, bitstream)
        args = ["--bitstream", bitstream]
        if ss.cable:
            args.extend(["--cable", ss.cable])
        elif board_name:
            args.extend(["--board", board_name])
        assert ss.fpga is not None
        if ss.fpga.part:
            args.extend(["--fpga-part", ss.fpga.part])
        if ss.reset:
            args.append("--reset")
        if ss.verbose:
            args.append("--verbose")
        self.ofpga_loader.run(*args)
