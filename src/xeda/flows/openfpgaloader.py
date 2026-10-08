import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, List, Literal, Optional, Tuple, Union

from ..board import WithFpgaBoardSettings
from ..dataclass import Field, model_validator
from ..design import SourceType
from ..flow import FPGA, Flow, FpgaSynthFlow, In
from ..tool import Tool

__all__ = ["Openfpgaloader"]

log = logging.getLogger(__name__)

#: What the loader printed, in its run directory: all there is to read when programming fails.
LOADER_LOG = "openfpgaloader.log"

#: How to give the loader its device, which programming the flash needs.
FLASH_NEEDS_THE_DEVICE = (
    "the target FPGA device, since `write_flash` programs the flash through a bridge made for "
    "the part: give its part number with `-s fpga.part=<part>`, or a board that has one with "
    "`-s board=<name>` (see `xeda list-boards`); in the design file, as `fpga.part` or `board` "
    "in its `[flows.{flow}]` section"
)


def _loader_part(fpga: FPGA) -> str:
    """The part `--fpga-part` takes: a Xilinx part without its speed grade, any other as it is.

    openFPGALoader uses the option as written to name the bridge bitstream it loads to write a
    flash (`spiOverJtag_<device><package>.bit.gz`), and that name has no speed grade.
    """
    part = fpga.part or ""
    # the part keeps the case it was written in, and its speed grade is upper case (`-2l` is `-2L`)
    if fpga.vendor == "xilinx" and fpga.speed and part.upper().endswith(fpga.speed.upper()):
        return part[: -len(fpga.speed)]
    return part


# The verdict on a run. openFPGALoader exits with status 0 after several failures, so the flow
# reads what the loader printed as well. What the loader does, from its source at the tag v1.1.1
# (commit 85be4fa; the lines below are at that tag):
#
# * `main` returns status 1 when `program()` throws (main.cpp:583-590). A `program()` that returns
#   ends the program with status 0, since `main` has no `return` at its end (main.cpp:651-653).
# * Xilinx (xilinx.cpp). `program_mem` loads the SRAM and returns nothing. When the startup
#   sequence has run, it reads the state of the FPGA and always prints `ir: <n> isc_done <d>
#   isc_ena <e> init <i> done <d>` (lines 987-989). With `done 0` it also prints the status
#   register, one `<field> <value>` line for each field (lines 991-993, 1043-1067, 1195-1225). It
#   throws nothing, so the exit status is 0. For a file that it cannot read or parse, `program`
#   prints `FAIL` and returns (lines 628-643). `program_spi`, which writes a flash, ignores the
#   result of `SPIInterface::write` (line 849), as `program` ignores the result of the
#   programmers of the CPLDs and of the platform flash (`flow_program`, `xc2c_flow_program`,
#   `xcf_program`). These writers print why a step failed and return false. So a flash write that
#   fails ends with status 0, whatever the cause: a flash that does not answer, a write that the
#   chip refuses, an image that does not fit, a bridge that cannot be chosen, a verify that
#   differs. `FAILURE_PREFIXES` and `FAILURE_ENDINGS` hold what the loader prints for each, with
#   the place in its source. (A BPI flash whose bridge cannot be chosen ends with status 1:
#   `program_bpi` throws.) One failure prints nothing that tells it: `SPIFlash::global_unlock`
#   (spiFlash.cpp:1199-1217, for the SST26VF) returns false when a sector stays locked after the
#   unlock. Its write enable and its wait print signs; only the final check of the lock registers
#   is silent. A flash write on a Xilinx FPGA therefore has to report that it finished (below).
# * Lattice (ECP5, lattice.cpp). `program` throws when a step failed (lines 1073-1082): status 1.
# * Gowin and iCE40 print `FAIL` or `Fail` for a failed step and return (gowin.cpp:385-426,
#   ice40.cpp:104-149): status 0.
# * `printError` writes to stderr. `printInfo`, `printWarn`, `printSuccess`, `printf` and
#   `std::cout` write to stdout (display.cpp:23-65). The flow merges the two into one log.
#
# The verdict looks for signs of failure. The loader is at least release 0.13.1
# (`MIN_OPENFPGALOADER_VERSION`), the first one that prints the readback of the DONE signal and
# says so in its version. It requires one sign of success, of a flash write on a Xilinx FPGA, and
# of nothing else: the other families print other words or nothing, so a required marker would
# fail good runs of other families and of releases that change a line. A load that fails without
# one of the signs below passes. Its whole output is in the log in the run directory.
#
# The sign of success. `SPIFlash::erase_and_prog` makes its `Writing` progress bar after
# everything that can stop it without a message, `prepare_flash` and so `global_unlock` among
# them (spiFlash.cpp:392-395, 531-535 and, for an `.mcs` file, 483-500; v0.13.1: 364 and 437), and
# ends the bar with `done()` only when no page write returned -1 (550, 518; v0.13.1: 452; a wait
# that times out returns another value, and prints `wait: Error`, a sign above). `done()` prints
# the bar at its end and `Done`: `Writing: [<50 columns>] 100.00%`, then `Done` (progressBar.cpp:
# 21-60; `Writing: Done` when `_verbose < 0`). A bar that an update shows at 100.00% before
# `done()` is not followed by `Done` at once. The progress bar code is the same in v0.13.1,
# v1.0.0, v1.1.0 and v1.1.1, except that a pipe gets a line end after a percent from v1.0.0 on,
# which the split into lines drops. The BPI flash writer of v1.1.0 and later uses the same label
# (bpiFlash.cpp:449). The loader never sets a locale, so the point of `100.00%` is the same
# everywhere. A later release that prints another bar fails every Xilinx flash write, loudly: the
# run says what it looked for and where the log is. `SPIInterface::write` verifies only after a
# write that succeeded (`if (_spif_verify && ret)`, spiInterface.cpp:219; v0.13.1: 192), so
# `verify` does not report a write that stopped.

#: The state of a Xilinx FPGA after a load (xilinx.cpp:988). `done` is the DONE signal.
_DONE_READBACK = re.compile(
    r"ir: [0-9a-f]+ isc_done [0-9a-f]+ isc_ena [0-9a-f]+ init [0-9a-f]+ done ([0-9a-f]+)"
)
#: Fields of the status register that the loader prints after the readback (xilinx.cpp:1043-1067).
_REGISTER_DONE = re.compile(r"Done\s+0x[0-9a-f]+")
_REGISTER_ID_ERROR = re.compile(r"ID Error\s+ID error")
_REGISTER_CRC_ERROR = re.compile(r"CRC Error\s+CRC error")
#: The warning for a `.bit` file whose header declares less data than the file holds: the loader
#: sends only what the header declares (bitparser.cpp:115).
_SHORT_HEADER = re.compile(
    r"File is longer than bitstream length declared in the header: (\d+) vs (\d+)"
)
#: A step that failed: its label, if any, and then the word that `printError` writes.
_FAILED_STEP = re.compile(r"(?:^|\s)(?:FAIL|Fail)$")
#: Failure messages that start a line, each with the place in the source of v1.1.1 where the loader
#: prints it. The rest of the line is a reason, an address or a name. The same words inside a line
#: are the text of something else, such as a field of a register dump.
FAILURE_PREFIXES: dict[str, str] = {
    "Error: ": "the loader's own prefix, such as spiFlash.cpp:454, :868, :875 and xilinx.cpp:1584",
    "Can't program ": "xilinx.cpp:723 (SPI bridge), :759 (BPI bridge); the same words are in "
    "altera.cpp:196 and efinix.cpp:435",
    "FAIL: ": "main.cpp:817 (the direct SPI mode), efinix.cpp:190",
    "Verification failed at ": "spiFlash.cpp:589 (the address that differs)",
}
#: Failure messages that end a line, each with the place where the loader prints it. They are not
#: required to start one: in a terminal the progress bar has no line end, and the message that
#: follows it is glued to its percent.
FAILURE_ENDINGS: dict[str, str] = {
    "Read ID failed": "spiFlash.cpp:628, printed by spiInterface.cpp:222",
    "Failed to read flash": "spiFlash.cpp:354, :581",
    "flash overflow": "spiFlash.cpp:399",
    "disable protection failed": "spiFlash.cpp:848, :881",
    "Unknown Top/Bottom register": "spiFlash.cpp:1112",
    "write en: Error": "spiFlash.cpp:810",
    "wait: Error": "xilinx.cpp:2259, and the wait of every other family",
    "PDI programing failed": "xilinx.cpp:952",
    "Only jed file and flash mode supported for XC95 CPLD": "xilinx.cpp:618",
    "flow erase failed": "xilinx.cpp:1727",
    "Erase: fails to verify blank check": "xilinx.cpp:2011",
}
#: What the loader prints when it cannot name the bridge that writes a flash, since it knows no
#: part: a board that its own list gives no part (`kc705`, board.hpp:197) and no `--fpga-part`
#: (xilinx.cpp:722-728; main.cpp:216-219 takes the part from the board only when it has one).
_NO_PART_FOR_THE_BRIDGE = "missing device-package information"
_ESCAPE_CODES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: The most lines that a message quotes for the failures that the loader reports as a step.
MAX_REPORTED_LINES = 6


@dataclass(frozen=True)
class LoaderFailure:
    """A failure that the output of the loader shows and its exit status does not.

    `summary` holds the sentences that say what happened, one for each line of the message.
    `evidence` holds the lines of the output that show it, as the loader printed them."""

    summary: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    evidence_heading: str = "The loader printed:"

    def message(self, log_file: Optional[Path] = None) -> str:
        """The text for a person: what happened, then the lines that show it."""
        text = list(self.summary)
        if self.evidence:
            text.append(self.evidence_heading)
            text.extend(f"    {line}" for line in self.evidence)
        if log_file is not None:
            text.append(f"Its whole output is in {log_file}.")
        return "\n".join(text)


def _output_lines(text: str) -> list[str]:
    """The lines of the output as a person reads them: without the escape codes of a terminal's
    colors, with a carriage return (the redrawn progress bar) as the end of a line, and without
    blank lines or the space around a line."""
    plain = _ESCAPE_CODES.sub("", text).replace("\r", "\n")
    return [line for line in (raw.strip() for raw in plain.split("\n")) if line]


def _unfinished_load(lines: list[str], target: Optional[str]) -> tuple[list[str], list[str]]:
    """The sentences and the lines that tell that a Xilinx FPGA did not finish its configuration:
    the readback of its state shows DONE low. Nothing if they do not."""
    for index, line in enumerate(lines):
        found = _DONE_READBACK.fullmatch(line)
        if found and found.group(1) == "0":
            break
    else:
        return [], []
    sentences = [
        "openFPGALoader exited with status 0, but the FPGA did not finish configuration: "
        "its DONE signal stayed low."
    ]
    evidence = [line]
    # what the loader warned about before it loaded, and printed after the readback
    warned = [found for found in map(_SHORT_HEADER.search, lines) if found]
    if warned:
        held, declared = warned[0].groups()
        sentences.append(
            f"The loader sent {declared} bytes, the length that the header of the bitstream "
            f"declares, but the file holds {held} bytes of data."
        )
        sentences.append("The program that wrote the bitstream wrote a wrong header.")
        evidence.insert(0, warned[0].string)
    after = lines[index + 1 :]
    done_row = next((row for row in after if _REGISTER_DONE.fullmatch(row)), None)
    id_error = next((row for row in after if _REGISTER_ID_ERROR.fullmatch(row)), None)
    crc_error = next((row for row in after if _REGISTER_CRC_ERROR.fullmatch(row)), None)
    evidence.extend(row for row in (done_row, id_error, crc_error) if row)
    if id_error:
        sentences.append(
            "The configuration logic reports an ID error: the ID code in the bitstream is not "
            "the ID code of the FPGA on the cable."
        )
        which = (
            f"the one this flow is set up to program ({target})"
            if target
            else "the one the bitstream is for"
        )
        sentences.append(f"Check that the board on the cable is {which}.")
        # the loader opens the first cable that matches, unless the cable index names another
        # (ftdipp_mpsse.cpp:50-53, main.cpp:289); it never compares the FPGA it finds with the
        # part of the board it was given (main.cpp:216-219 uses the part for the flash only)
        sentences.append(
            "The loader uses the first cable that matches unless the `cable_index` or "
            "`usb_serial_num` setting names another, so with several boards connected it can "
            "program the wrong one."
        )
    if crc_error:
        sentences.append(
            "The configuration logic reports a CRC error: the bitstream is damaged or incomplete."
        )
    return sentences, evidence


def _reported_failures(lines: list[str]) -> list[str]:
    """The lines where the loader reports a failure itself, in the order it printed them: a step
    that failed (with the line before a bare `FAIL`, which says which step), an error message,
    a flash that does not answer."""
    reported: list[str] = []
    for index, line in enumerate(lines):
        if (
            _FAILED_STEP.search(line)
            or line.startswith(tuple(FAILURE_PREFIXES))
            or line.endswith(tuple(FAILURE_ENDINGS))
        ):
            if index and re.fullmatch(r"FAIL|Fail", line):
                reported.append(lines[index - 1])
            reported.append(line)
    return list(dict.fromkeys(reported))


#: The progress bar of a flash write at its end (progressBar.cpp:21-51); `done()` follows it with
#: a line `Done`. With quiet bars (`verbose_level: -1`) `done()` prints `Done` after `Writing: `.
_WRITE_BAR_AT_ITS_END = re.compile(r"Writing: \[=+\] 100\.00%")
_WRITE_DONE_QUIET = re.compile(r"(?:^|\s)Writing: Done$")
#: The most lines of the end of the output that a message quotes for a flash that was not written.
MAX_LAST_LINES = 3


def _finished_flash_writes(lines: list[str]) -> int:
    """How many flash writes the lines report as finished: a `Writing` bar at its end that `Done`
    follows at once, or `Writing: Done` (see the comment above)."""
    finished = 0
    for index, line in enumerate(lines):
        if _WRITE_DONE_QUIET.search(line):
            finished += 1
        elif _WRITE_BAR_AT_ITS_END.fullmatch(line) and lines[index + 1 : index + 2] == ["Done"]:
            finished += 1
    return finished


def _unwritten_flash(lines: list[str], finished: int, expected: int, verify: bool) -> LoaderFailure:
    """The failure of a flash write that the output does not report as finished: the sentences and
    the end of the output, which shows where the loader stopped."""
    ends = "a progress bar `Writing: [...] 100.00%` and the word `Done`"
    sentences = [
        "openFPGALoader exited with status 0, but it did not report that it wrote the flash."
    ]
    if expected == 1:
        sentences.append(f"A flash write ends with {ends}. The log has no such lines.")
    else:
        sentences.append(
            f"It has to write {expected} flash chips, and each write ends with {ends}. "
            f"The log shows {finished} finished write{'' if finished == 1 else 's'}."
        )
    sentences.append(
        "The loader can stop before it writes, and print nothing, when the flash stays locked "
        "after its unlock. A flash of the SST26VF family does this."
    )
    sentences.append("The flash may hold its old data, or only part of the file.")
    if not verify:
        sentences.append(
            "Set `verify: true` as well. After a write that finished, the loader then reads the "
            "flash back and compares it with the file."
        )
    return LoaderFailure(
        tuple(sentences),
        tuple(lines[-MAX_LAST_LINES:]),
        evidence_heading="The last lines it printed:",
    )


def loader_failure(
    text: str,
    target: Optional[str] = None,
    *,
    flash_writes: int = 0,
    verify: bool = False,
) -> Optional[LoaderFailure]:
    """The failure that the output `text` of the loader shows (see above), or None if it shows
    none. `target`, when known, names the board or part that the flow is set up to program.
    `flash_writes` is the number of flash writes that the loader has to report as finished (a
    flash write on a Xilinx FPGA: one for each flash chip), and `verify` tells whether it was
    asked to read the flash back."""
    lines = _output_lines(text)
    sentences, evidence = _unfinished_load(lines, target)
    reported = [line for line in _reported_failures(lines) if line not in evidence]
    if not sentences and not reported:
        finished = _finished_flash_writes(lines)
        if finished >= flash_writes:
            return None
        return _unwritten_flash(lines, finished, flash_writes, verify)
    if not sentences:
        sentences = ["openFPGALoader exited with status 0, but it reported a failure."]
    if any(_NO_PART_FOR_THE_BRIDGE in line for line in reported):
        sentences.extend(
            [
                "To write a flash, the loader loads a bridge that it names after the part of "
                "the FPGA.",
                "It knows no part: the board name that the flow gave it has none in its own "
                "list of boards.",
                "Set `cable` as well. The flow then gives the loader the part of the FPGA "
                "(`--fpga-part`) in place of the board name.",
                "`openFPGALoader --list-boards` shows the cable of each board.",
            ]
        )
    return LoaderFailure(tuple(sentences), tuple(evidence + reported[:MAX_REPORTED_LINES]))


#: The first release of openFPGALoader that prints the state of a Xilinx FPGA after a load
#: (`done` of the readback, and the status register with `done 0`) and says so in its version:
#: v0.12.1 has no such line, so with an older loader a load that left DONE low would pass. The
#: tag v0.13.0 has the line, but its CMakeLists still names the version 0.12.1 (`-V` prints
#: `v${PROJECT_VERSION}`), so `-V` cannot tell it from the release before it. v0.13.1 names
#: itself. Every release since v0.3 prints `openFPGALoader v<version>` for `-V`, which `Tool` reads.
MIN_OPENFPGALOADER_VERSION = (0, 13, 1)


class OpenfpgaloaderTool(Tool):
    """openFPGALoader, whose version flag is spelled with a capital V.

    `openFPGALoader --help` lists `-V, --Version   Print program version`, and the conventional
    `--version` is rejected (`Error parsing options: Option 'version' does not exist`), so the
    generic `Tool` flag would record an empty version. The query prints one line on stdout,
    `openFPGALoader v1.1.1`, which `Tool`'s default patterns do not match (the fallback would
    keep the leading `v`). Asking for the version touches no device.

    It asks for a terminal (`pseudo_terminal`). Its progress bar redraws one line with a carriage
    return only when its output is a terminal, and its colors need one (display.cpp:23-65,
    progressBar.cpp:43-50); through a pipe it prints a line for each update. In a terminal, the
    user then sees the output as when running the loader by hand.
    """

    pseudo_terminal: ClassVar[bool] = True
    minimum_version_reason: ClassVar[str] = (
        "Older loaders do not report whether a Xilinx FPGA finished its configuration. "
        "Release 0.13.0 does, but it prints 0.12.1 as its version, so Xeda cannot tell it apart."
    )
    minimum_version: Optional[Tuple[Union[int, str], ...]] = MIN_OPENFPGALOADER_VERSION
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
    database), when it has one. The FPGA part is given when the board is not named and the part is
    known: a Xilinx part without its speed grade, any other as it is. Without a part,
    openFPGALoader detects the device; programming the flash (`write_flash`) needs the part. The
    loader's output is kept in `openfpgaloader.log` in the run directory. It needs openFPGALoader
    0.13.1 or newer, and refuses an older loader before it builds or programs anything. The run
    fails when the loader exits with a nonzero status, and also when its output shows that the
    device was not programmed although the status is 0: DONE low after a Xilinx load (with the ID
    or CRC error the FPGA reports), a step that printed FAIL, or an error message, such as a flash
    that does not answer. A flash write on a Xilinx FPGA also fails unless the loader reports that
    it wrote the flash. The flow always runs, since it changes a device rather than a file, and it
    is the only flow here that touches hardware.
    """

    #: the device only to program the flash (`required_settings_for`)
    required_settings: dict[str, str] = {}

    #: Static, so a chain can refuse this flow anywhere but last without constructing it.
    action_reason = "it programs a device"

    # This flow reports no results beyond the keys every flow reports; declaring this
    # explicitly keeps `xeda list-results` from guessing.
    results_description: dict = {}

    #: The loader's tool, made in `init()`.
    loader: OpenfpgaloaderTool

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
        write_flash: bool = Field(
            False,
            description="Program nonvolatile flash (`--write-flash`). Needs the FPGA device "
            "(`fpga.part`, or a `board` that has one): the flash is programmed through a bridge "
            "made for the part.",
        )
        verify: bool = Field(
            False, description="Verify SPI flash after programming; needs `write_flash`."
        )
        freq: Optional[int] = Field(None, gt=0, description="JTAG clock frequency in Hz.")
        offset: Optional[int] = Field(None, ge=0, description="Flash start address in bytes.")
        cable_index: Optional[int] = Field(None, ge=0, description="Probe index.")
        usb_serial_num: Optional[str] = Field(
            None,
            description="Serial number of the FTDI probe to use (`--ftdi-serial`). Selects one "
            "of several probes of the same kind.",
        )
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

    @classmethod
    def required_settings_for(cls, settings: Flow.Settings) -> Mapping[str, str]:
        """The device, to program the flash: openFPGALoader programs it through a bridge made for
        the part. Loading SRAM needs none: the loader detects the device."""
        if getattr(settings, "write_flash", False):
            return {"fpga": FLASH_NEEDS_THE_DEVICE}
        return {}

    def init(self) -> None:
        """Make the loader's tool, which asks the program for its version, before any producer
        runs: a loader that is too old (`MIN_OPENFPGALOADER_VERSION`) is refused here, so neither
        synthesis, placement nor packing runs first. The query touches no device. The tool is made
        in the flow, so it takes the flow's settings and is listed in the results with the
        version it reports."""
        super().init()
        self.loader = OpenfpgaloaderTool()

    def always_runs(self) -> Optional[str]:
        return super().always_runs() or self.action_reason

    def _target(self) -> Optional[str]:
        """The board and part that the flow is set up to program, as words for a message."""
        assert isinstance(self.settings, self.Settings)
        part = self.settings.fpga.part if self.settings.fpga is not None else None
        named = [
            f"{kind} {value}"
            for kind, value in (("board", self.settings.board), ("part", part))
            if value
        ]
        return ", ".join(named) or None

    def _flash_writes(self) -> int:
        """How many flash writes the loader has to report as finished: one for each flash chip it
        writes on a Xilinx FPGA, two with `target_flash: both` (`loader_failure`). Other families
        and SRAM loads have none to report."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if not ss.write_flash or ss.fpga is None or ss.fpga.vendor != "xilinx":
            return 0
        return 2 if ss.target_flash == "both" else 1

    def parse_reports(self) -> bool:
        """The verdict: a nonzero exit status failed the run already (`run()` raised it). A run
        also fails when this run's log shows a failure (`loader_failure`), or when there is no log
        of this run, which leaves no evidence that the device was programmed. The failure is the
        error of the run, in the words of a person, with the lines that show it."""
        if self.results.get("error"):
            return False
        log_file = self.run_path / LOADER_LOG
        report = self.report_file(log_file)
        if report is None:
            failure: Optional[LoaderFailure] = LoaderFailure(
                (
                    "openFPGALoader exited with status 0, but it left no log of this run "
                    f"({LOADER_LOG}).",
                    "There is no evidence that it programmed the FPGA.",
                )
            )
        else:
            text = report.read_text(encoding="utf-8", errors="replace")
            assert isinstance(self.settings, self.Settings)
            failure = loader_failure(
                text,
                target=self._target(),
                flash_writes=self._flash_writes(),
                verify=self.settings.verify,
            )
        if failure is None:
            return True
        message = failure.message(log_file)
        log.error("%s", message)
        self.results["error"] = {"type": "ReportedFailure", "message": message}
        return False

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
        args = ["--bitstream", self.inputs.bitstream]
        if ss.cable:
            args.extend(["--cable", ss.cable])
        elif board_name:
            args.extend(["--board", board_name])
        # `--board` gives openFPGALoader the board's own part, and `--fpga-part` would replace
        # it; with no part known, openFPGALoader detects the device (`required_settings_for`)
        if ss.fpga is not None and ss.fpga.part and (ss.cable or not board_name):
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
        # each setting with the option of openFPGALoader that it is (main.cpp:895-1022): the
        # serial number is the loader's `--ftdi-serial`, and only an FTDI probe has one
        for name, option in (
            ("freq", "--freq"),
            ("offset", "--offset"),
            ("cable_index", "--cable-index"),
            ("usb_serial_num", "--ftdi-serial"),
            ("index_chain", "--index-chain"),
            ("file_type", "--file-type"),
            ("target_flash", "--target-flash"),
            ("verbose_level", "--verbose-level"),
        ):
            value = getattr(ss, name)
            if value is not None:
                args.extend([option, str(value)])
        args.extend(ss.extra_args)
        # the loader is started twice: `init` asked it for its version, and this program call
        # follows. Its output goes to the log, made anew before the loader starts.
        self.loader.run(*args, tee=self.run_directory.writable(LOADER_LOG), merge_stderr=True)
