import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Literal, Optional

from ..board import FPGA_OR_BOARD_REQUIRED, WithFpgaBoardSettings
from ..dataclass import Field
from ..flow import FlowFatalError, FlowSettingsException, FpgaSynthFlow
from ..tool import Tool
from .nextpnr import Nextpnr

__all__ = ["Openfpgaloader"]

log = logging.getLogger(__name__)

#: Bitstream packer for each FPGA family this flow is tested with.
PACKERS = {"ecp5": "ecppack", "ice40": "icepack"}


class Openfpgaloader(FpgaSynthFlow):
    """Build a bitstream and program it onto an FPGA board with openFPGALoader.

    Runs the full `yosys_fpga` -> `nextpnr` chain, packs the routed design into a bitstream
    with `ecppack` for ECP5 or `icepack` for iCE40, and loads it over the configured `cable` or
    `board`. Other families are rejected before place and route. This is the only flow here
    that touches real hardware.
    """

    required_settings = {"fpga": FPGA_OR_BOARD_REQUIRED}

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    ofpga_loader = Tool("openFPGALoader")

    class Settings(WithFpgaBoardSettings):
        reset: bool = Field(
            False, description="Reset the FPGA after loading the bitstream (`--reset`)."
        )
        cable: Optional[str] = Field(
            None,
            description='Programming cable to use, e.g. "ft2232". Takes precedence over the '
            "cable implied by `board`.",
        )
        bitstream_file: Optional[Path] = Field(
            None,
            description="Packed bitstream output path, relative to the run directory. Defaults "
            "to a file named after the board.",
        )
        write_flash: bool = Field(False, description="Program nonvolatile flash (`--write-flash`).")
        verify: bool = Field(False, description="Verify SPI flash after programming.")
        freq: Optional[int] = Field(None, gt=0, description="JTAG clock frequency in Hz.")
        offset: Optional[int] = Field(None, ge=0, description="Flash start address in bytes.")
        cable_index: Optional[int] = Field(None, ge=0, description="Probe index.")
        usb_serial_num: Optional[str] = Field(None, description="USB probe serial number.")
        index_chain: Optional[int] = Field(None, ge=0, description="Device index in JTAG chain.")
        file_type: Optional[str] = Field(
            None, description="Bitstream type when extension detection is insufficient."
        )
        target_flash: Optional[Literal["primary", "secondary", "both"]] = Field(
            None, description="Flash chip to program on boards with two (`--target-flash`)."
        )
        skip_reset: bool = Field(False, description="Skip reset during flash programming.")
        skip_load_bridge: bool = Field(
            False, description="Skip loading the SPI bridge during flash programming."
        )
        verbose_level: Optional[int] = Field(
            None, ge=-1, le=2, description="Loader verbosity (-1 to 2)."
        )
        packer_args: List[str] = Field([], description="Extra arguments to ecppack or icepack.")
        extra_args: List[str] = Field(
            [], description="Extra openFPGALoader command-line arguments."
        )
        nextpnr: Nextpnr.Settings = Field(
            default_factory=Nextpnr.Settings,
            description="Settings for the `nextpnr` dependency that places and routes the design.",
        )

        dependency_settings = {"nextpnr": ("fpga", "board", "custom_boards_file", "clocks")}

    def init(self) -> None:
        """Select the FPGA packer and register the nextpnr dependency."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        nextpnr = ss.resolve_dependency("nextpnr")  # adopts an `fpga` given only for nextpnr
        assert ss.fpga is not None, "checked at launch (`required_settings`)"
        family = (ss.fpga.family or "").lower()
        # Checked before the dependencies run: there is no use in placing and routing a design
        # that cannot be packed.
        if family not in PACKERS:
            raise FlowSettingsException(
                f"openfpgaloader has no tested bitstream packer for fpga.family={family or None!r}"
                f" (packers: {', '.join(f'{k}: {v}' for k, v in PACKERS.items())}); run nextpnr "
                "directly for other families."
            )
        self.packer = Tool(PACKERS[family])
        self.add_dependency(Nextpnr, nextpnr)

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        board_id = ss.board
        board_name = None
        if board_id:
            board_data = ss.board_data()
            if board_data:
                board_name = board_data.get("name")
        assert ss.fpga is not None
        family = (ss.fpga.family or "").lower()
        next_pnr = self.completed_dependencies[0]
        assert isinstance(next_pnr, Nextpnr)
        assert isinstance(next_pnr.settings, Nextpnr.Settings)
        config_name = next_pnr.settings.textcfg if family == "ecp5" else next_pnr.settings.asc
        if not config_name:
            raise FlowFatalError(f"nextpnr {family} configuration output is disabled.")
        config = next_pnr.run_path / config_name
        if not config.is_file():
            raise FlowFatalError(f"Can't find {config} generated by nextpnr.")
        extension = ".bit" if family == "ecp5" else ".bin"
        bitstream = ss.bitstream_file or Path(f"{board_name or 'bitstream'}{extension}")
        if not bitstream.is_absolute():
            bitstream = self.run_path / bitstream
        bitstream.parent.mkdir(parents=True, exist_ok=True)
        if bitstream.resolve() == config.resolve():
            raise FlowFatalError("The packed bitstream cannot overwrite the nextpnr configuration.")
        # A successful packer invocation may produce no file (for example, --help). Pack to a
        # fresh path, then publish it only after this invocation has written an output.
        with TemporaryDirectory(prefix=".xeda-pack-", dir=bitstream.parent) as temporary_dir:
            packed = Path(temporary_dir) / bitstream.name
            self.packer.run(config, packed, *ss.packer_args)
            if not packed.is_file():
                raise FlowFatalError(f"Bitstream packer did not write {packed}.")
            packed.replace(bitstream)
        self.artifacts["bitstream"] = bitstream
        args = ["--bitstream", bitstream]
        if ss.cable:
            args.extend(["--cable", ss.cable])
        elif board_name:
            args.extend(["--board", board_name])
        if ss.fpga.part:
            args.extend(["--fpga-part", ss.fpga.part])
        for enabled, flag in (
            (ss.reset, "--reset"),
            (ss.write_flash, "--write-flash"),
            (ss.verify, "--verify"),
            (ss.skip_reset, "--skip-reset"),
            (ss.skip_load_bridge, "--skip-load-bridge"),
            (ss.verbose > 0, "--verbose"),
        ):
            if enabled:
                args.append(flag)
        for name in (
            "freq",
            "offset",
            "cable_index",
            "usb_serial_num",
            "index_chain",
            "file_type",
            "target_flash",
            "verbose_level",
        ):
            value = getattr(ss, name)
            if value is not None:
                args.extend([f"--{name.replace('_', '-')}", str(value)])
        args.extend(ss.extra_args)
        self.ofpga_loader.run(*args)
