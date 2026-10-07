import logging
import re
from pathlib import Path
from typing import Any, List, Literal, Optional, Union

from ..board import FPGA_OR_BOARD_REQUIRED, WithFpgaBoardSettings
from ..dataclass import Field, model_validator
from ..design import SourceType
from ..flow import FPGA, FpgaSynthFlow, In
from ..tool import Tool

__all__ = ["Openfpgaloader"]

log = logging.getLogger(__name__)


def _loader_part(fpga: FPGA) -> str:
    """The part `--fpga-part` takes: a Xilinx part without its speed grade, any other as it is.

    openFPGALoader uses the option as written to name the bridge bitstream it loads to write a
    flash (`spiOverJtag_<device><package>.bit.gz`), and that name has no speed grade.
    """
    part = fpga.part or ""
    if fpga.vendor == "xilinx" and fpga.speed and part.endswith(fpga.speed):
        return part[: -len(fpga.speed)]
    return part


class OpenfpgaloaderTool(Tool):
    """openFPGALoader, whose version flag is spelled with a capital V.

    `openFPGALoader --help` lists `-V, --Version   Print program version`, and the conventional
    `--version` is rejected (`Error parsing options: Option 'version' does not exist`), so the
    generic `Tool` flag would record an empty version. The query prints one line on stdout,
    `openFPGALoader v1.1.1`, which `Tool`'s default patterns do not match (the fallback would
    keep the leading `v`). Asking for the version touches no device.
    """

    executable: str = "openFPGALoader"
    version_flag: Optional[List[str]] = ["-V"]
    version_regexps: List[Union[re.Pattern[str], str]] = [
        r"\bopenFPGALoader\s+v?(?P<version>\d+(?:\.\d+)+)"
    ]


def _switched_on(value: Any) -> bool:
    """Whether a Boolean setting's input, still as given, turns it on, as the setting itself
    reads it: a boolean, or `true` as text (how a command line writes one). `yes`, `on` and
    numbers are no boolean, so they are not on here; the setting's own validation refuses them,
    saying what to write."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _is_boolean(value: Any) -> bool:
    """Whether a Boolean setting's input, still as given, is a boolean the setting accepts."""
    return isinstance(value, bool) or (
        isinstance(value, str) and value.lower() in ("true", "false")
    )


class Openfpgaloader(FpgaSynthFlow):
    """Program a bitstream onto an FPGA board with openFPGALoader.

    Its `bitstream` input is a typed `Bitstream` design source -- a file built elsewhere, by
    any toolchain -- or, by default, the bitstream `fpga_pack` records after `yosys_fpga` ->
    `nextpnr` -> `fpga_pack`. The flow builds and packs nothing itself: the settings of those
    stages are their own sections' (`flows.nextpnr`, `flows.fpga_pack`). The device is targeted
    by `cable`, else by the board's name in openFPGALoader (`openfpgaloader_board` in the board
    database), when it has one. The FPGA part is given when the board is not named: a Xilinx part
    without its speed grade, any other as it is. It always runs, since
    it changes a device rather than a file, and it is the only flow here that touches hardware.
    """

    required_settings = {"fpga": FPGA_OR_BOARD_REQUIRED}

    #: Static, so a chain can refuse this flow anywhere but last without constructing it.
    action_reason = "it programs a device"

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    class Settings(WithFpgaBoardSettings):
        removed_settings = {
            **WithFpgaBoardSettings.removed_settings,
            "nextpnr": "the nextpnr flow's own settings: a [flows.nextpnr] section, or "
            "`-s flows.nextpnr.<setting>=<value>`",
            "packer_args": "flows.fpga_pack.packer_args",
            "bitstream_file": "flows.fpga_pack.bitstream to name or deliver the packed "
            "bitstream, or a typed source in rtl.sources to program an existing file: "
            '{ file = "top.bit", type = "Bitstream" }',
        }
        reset: bool = Field(
            False, description="Reset the FPGA after loading the bitstream (`--reset`)."
        )
        cable: Optional[str] = Field(
            None,
            description='Programming cable to use, e.g. "ft2232". Takes precedence over the '
            "cable implied by `board`.",
        )
        write_flash: bool = Field(False, description="Program nonvolatile flash (`--write-flash`).")
        verify: bool = Field(
            False, description="Verify SPI flash after programming; needs `write_flash`."
        )
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
        extra_args: List[str] = Field(
            [], description="Extra openFPGALoader command-line arguments."
        )

        @model_validator(mode="before")
        @classmethod
        def _verify_needs_flash(cls, values):
            """openFPGALoader verifies what it wrote to flash: there is nothing to verify
            after loading SRAM. Checked on the whole input -- on an assignment, the whole
            state with the assigned value -- before anything is stored, so the order the two
            settings are given or assigned in does not matter and a refused assignment leaves
            the settings as they were. Each value is read as its field will read it (`true` as
            text is true; `1` and `"yes"` are no boolean, and the field refuses them), and
            nothing is rewritten here."""
            flash = values.get("write_flash")
            if (
                _switched_on(values.get("verify"))
                and not _switched_on(flash)
                and (flash is None or _is_boolean(flash))  # else the field says what it is
            ):
                raise ValueError(
                    "verify checks the flash openFPGALoader wrote: it needs write_flash=true "
                    "as well"
                )
            return values

    class Inputs(FpgaSynthFlow.Inputs):
        bitstream: Path = In(
            SourceType.Bitstream,
            producer="fpga_pack",
            output="bitstream",
            description="The bitstream to program: a design source or fpga_pack's.",
        )

    def always_runs(self) -> Optional[str]:
        return super().always_runs() or self.action_reason

    def run(self) -> None:
        """Program exactly the bitstream handed over as the input `bitstream`."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        ss = self.settings
        board_name = None
        if ss.board:
            board_data = ss.board_data()
            if board_data:
                board_name = board_data.get("openfpgaloader_board")
        assert ss.fpga is not None
        args = ["--bitstream", self.inputs.bitstream]
        if ss.cable:
            args.extend(["--cable", ss.cable])
        elif board_name:
            args.extend(["--board", board_name])
        # `--board` gives openFPGALoader the board's own part, and `--fpga-part` would replace it
        if ss.fpga.part and (ss.cable or not board_name):
            args.extend(["--fpga-part", _loader_part(ss.fpga)])
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
        # made here, in the flow, so it takes the flow's settings and is listed in the results,
        # with the version it reports: the loader is started twice, once to ask for its version
        # and once to program
        OpenfpgaloaderTool().run(*args)
