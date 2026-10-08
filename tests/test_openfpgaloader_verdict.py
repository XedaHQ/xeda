"""A programmer run passes only if the loader's own record shows no failure.

openFPGALoader v1.1.1 exits with status 0 after several failed loads: when a Xilinx FPGA does not
finish its configuration it prints the state of the DONE signal and the status register and
returns, and it prints `FAIL` for a bitstream it cannot read and returns too. So the exit status
alone is no evidence that the device was programmed. The flow judges the loader's output as well
(`openfpgaloader.log`, this run's own).

NOTHING HERE MAY REACH A REAL PROGRAMMER. Every launch goes through the `fake_loader` fixture of
`test_openfpgaloader.py`: the fake prints the output a test gives it (the logs in
`tests/resources/openfpgaloader` are the real loader's, but for the spaces at the ends of lines)
and exits with the status the test gives.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from xeda import proc_utils
from xeda.flow import Flow
from xeda.flows.openfpgaloader import (
    FAILURE_ENDINGS,
    FAILURE_PREFIXES,
    LOADER_LOG,
    loader_failure,
)

from . import tool_utils
from .settings_samples import flow_classes
from .test_openfpgaloader import (
    ECP5,
    _calls,
    _design,
    _prebuilt,
    _runner,
    assert_fake_loader,
)
from .test_proc_utils_terminal import Terminal

RESOURCES = Path(__file__).parent / "resources/openfpgaloader"

#: The line that tells a Xilinx FPGA is configured (DONE high): what a good load ends with.
DONE_HIGH = "ir: 1 isc_done 1 isc_ena 0 init 1 done 1"
#: The same line for a load that did not finish.
DONE_LOW = "ir: 1 isc_done 0 isc_ena 0 init 1 done 0"


@pytest.fixture
def fake_loader(tmp_path, monkeypatch):
    """The fake toolchain first on `PATH`, and a check that `openFPGALoader` is the fake."""
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    assert assert_fake_loader() == prefix / "bin/openFPGALoader"
    return prefix


def owner_log(name: str) -> str:
    """A log the real openFPGALoader 1.1.1 wrote (an `xeda run` of the openXC7 demo projects)."""
    return (RESOURCES / name).read_text(encoding="utf-8")


#: The load of the Arty A7-100T demo before `fpga-as` was fixed, as the loader printed it: the
#: bitstream's header declared a length of 0. The warning, `Load SRAM: [] nan%` and `Done 0x0` are
#: the owner's report; the other lines of the register dump are the format of the loader's source.
EMPTY_BITSTREAM = f"""\
empty
Jtag frequency : requested 10.00MHz   -> real 10.00MHz
Open file DONE
Parse file File is longer than bitstream length declared in the header: 3825788 vs 0
DONE
load program

Load SRAM: [] nan%

Done
Shift IR 11
{DONE_LOW}
Register raw value: 0x0000b8c
CRC Error       No CRC error
Part Secured    0x0
MMCM lock       0x1
DCI match       0x1
EOS             0x0
GTS CFG B       0x0
GWE             0x0
GHIGH B         0x0
MODE            0x0
INIT Complete   0x1
INIT B          0x1
Release Done    0x0
Done            0x0
ID Error        No ID error
DEC Error       0x0
XADC Over temp  0x0
STARTUP State   0x0
Reserved        0x0
BUS Width       x1
Reserved        0x0
"""

#: What the loader prints for a bitstream it can read but whose header declares more than the
#: file holds: the explanation (stderr), then `FAIL` (stderr), and it still exits with status 0.
SHORT_FILE = """\
empty
Jtag frequency : requested 6.00MHz   -> real 6.00MHz
Open file DONE
Parse file File is shorter than bitstream length declared in the header: 1000 vs 2000
FAIL
"""

#: A Xilinx load into the SRAM that worked, then a flash that does not answer: the bridge is
#: configured (DONE is high), `SPIInterface::write` prints the exception and returns false, and
#: `Xilinx::program` ignores that result.
FLASH_DOES_NOT_ANSWER = f"""\
empty
Jtag frequency : requested 6.00MHz   -> real 6.00MHz
Open file DONE
Parse file DONE
Use: /usr/local/share/openFPGALoader/spiOverJtag_xc7a35tcpg236.bit.gz
load program

Load SRAM: [==================================================] 100.00%

Done
Shift IR 35
{DONE_HIGH}
jtag_chain_len: 1
SOJ version: 1.000000
Read ID failed
"""

#: A flash written through the bridge: erased and written, both ended with `Done`.
FLASH_WRITTEN = f"""\
empty
Jtag frequency : requested 6.00MHz   -> real 6.00MHz
Open file DONE
Parse file DONE
Use: /usr/local/share/openFPGALoader/spiOverJtag_xc7a35tcpg236.bit.gz
load program

Load SRAM: [==================================================] 100.00%

Done
Shift IR 35
{DONE_HIGH}
jtag_chain_len: 1
SOJ version: 1.000000
JEDEC ID: 0xef4016
Detected: winbond W25Q32 64 sectors size: 32Mb
start addr: 00000000, end_addr: 00200000

Erasing: [==================================================] 100.00%

Done

Writing: [==================================================] 100.00%

Done
"""

#: A flash write for a board that the loader lists without a part (`kc705`, board.hpp:197): the
#: loader names the SPI bridge after the part, finds none, prints why and returns false, which
#: `Xilinx::program_spi` ignores (xilinx.cpp:723, 849). The loader exits with status 0.
FLASH_WITHOUT_A_PART = """\
write to flash
empty
Jtag frequency : requested 10.00MHz   -> real 10.00MHz
Open file DONE
Parse file DONE
Can't program SPI flash: missing device-package information
"""

#: The progress bars of a Xilinx flash write are 50 columns wide; these are the bar at half and at
#: the end, and the colors the loader writes in a terminal (display.cpp:13-21).
HALF = "=" * 25 + " " * 25
FULL = "=" * 50
BLUE, GREEN, OFF = "\x1b[94m", "\x1b[32m", "\x1b[0m"

#: What a Xilinx flash write prints before it erases, once the loader has found the flash (the load
#: of its bridge is left out). The SST26VF032B has its whole flash locked at power-up, so the loader
#: unlocks it first (`SPIFlash::global_unlock`, spiFlash.cpp:1199) and prints `Non Volatile`.
FLASH_FOUND = """\
write to flash
empty
Jtag frequency : requested 6.00MHz   -> real 6.00MHz
Open file DONE
Parse file DONE
Use: /usr/local/share/openFPGALoader/spiOverJtag_xc7a35tcpg236.bit.gz
JEDEC ID: 0xbf2642
Detected: microchip SST26VF032B 64 sectors size: 32Mb
Non Volatile
"""

#: The same log when the flash stays locked after the unlock: `global_unlock` returns false and
#: prints nothing (spiFlash.cpp:1212-1215), `prepare_flash` returns false, the loader erases and
#: writes nothing, and the exit status is 0.
FLASH_LOCKED = FLASH_FOUND

#: What the erase and the write print when they finish, byte for byte, as the progress bar code of
#: the loader prints them (progressBar.cpp and display.cpp): a program that links those two files of
#: the tag and nothing else printed them to a pipe and to a terminal. No loader ran. The lines of
#: the first bar are an update after a second, the others are `done()`. A terminal turns each `\n`
#: into `\r\n` by itself, so the terminal forms here have the loader's own `\n` only. v0.13.1
#: writes no line end after a percent in a pipe (v1.0.0 and later do), and its `Done` follows at
#: once; `verbose_level: -1` makes the bars quiet.
FLASH_BARS_0_13_1_PIPE = (
    "start addr: 00000000, end_addr: 00010000\n"
    f"\rErasing: [{HALF}] 50.00%\rErasing: [{FULL}] 100.00%\nDone\n"
    f"\rWriting: [{HALF}] 50.00%\rWriting: [{FULL}] 100.00%\nDone\n"
)
FLASH_BARS_1_1_1_PIPE = (
    "start addr: 00000000, end_addr: 00010000\n"
    f"\rErasing: [{HALF}] 50.00%\n\rErasing: [{FULL}] 100.00%\n\nDone\n"
    f"\rWriting: [{HALF}] 50.00%\n\rWriting: [{FULL}] 100.00%\n\nDone\n"
)
FLASH_BARS_1_1_1_TERMINAL = (
    "start addr: 00000000, end_addr: 00010000\n"
    f"{BLUE}\rErasing: [{OFF}{HALF}{BLUE}] 50.00%{OFF}"
    f"{BLUE}\rErasing: [{OFF}{FULL}{BLUE}] 100.00%{OFF}{GREEN}\nDone{OFF}\n"
    f"{BLUE}\rWriting: [{OFF}{HALF}{BLUE}] 50.00%{OFF}"
    f"{BLUE}\rWriting: [{OFF}{FULL}{BLUE}] 100.00%{OFF}{GREEN}\nDone{OFF}\n"
)
FLASH_BARS_QUIET_PIPE = "start addr: 00000000, end_addr: 00010000\nErasing: Done\nWriting: Done\n"
FLASH_BARS_QUIET_TERMINAL = (
    "start addr: 00000000, end_addr: 00010000\n"
    f"{BLUE}Erasing: {OFF}{GREEN}Done{OFF}\n{BLUE}Writing: {OFF}{GREEN}Done{OFF}\n"
)
#: Each form, with the name of its test case.
FINISHED_FLASH_WRITES = {
    "pipe-0.13.1": FLASH_BARS_0_13_1_PIPE,
    "pipe-1.1.1": FLASH_BARS_1_1_1_PIPE,
    "terminal-1.1.1": FLASH_BARS_1_1_1_TERMINAL,
    "quiet-pipe": FLASH_BARS_QUIET_PIPE,
    "quiet-terminal": FLASH_BARS_QUIET_TERMINAL,
}

#: A load into an ECP5 (Lattice) that worked: the loader has no readback to print, and a failed
#: load is a thrown exception there, so its exit status is nonzero.
ECP5_LOADED = """\
empty
Jtag frequency : requested 6.00MHz   -> real 6.00MHz
Open file: DONE
Parse file: DONE
Enable configuration: DONE
SRAM erase: DONE

Loading: [==================================================] 100.00%

Done
Disable configuration: DONE
"""


def only_evidence(failure) -> str:
    return "\n".join(failure.evidence)


# ------------------------------------------------------------------------- the judgment of a log


def test_the_basys_3_log_is_a_failure_the_loader_did_not_exit_with():
    """The log of the report: DONE stayed low and the FPGA reports an ID error, and the loader
    exited with status 0."""
    failure = loader_failure(
        owner_log("basys3-id-error.txt"), target="board basys_3, part xc7a35tcpg236-1"
    )
    assert failure is not None
    message = failure.message()
    assert "exited with status 0" in message
    assert "did not finish configuration" in message and "DONE signal stayed low" in message
    assert "ID error" in message and "ID code in the bitstream" in message
    assert "board basys_3, part xc7a35tcpg236-1" in message  # which board the flow was told
    # two boards on the cable, the loader takes the first: say how to choose
    assert "`cable_index`" in message and "`usb_serial_num`" in message
    assert "several boards" in message
    # the decisive lines, as the loader printed them
    assert failure.evidence == (
        DONE_LOW,
        "Done            0x0",
        "ID Error        ID error",
    )
    for line in failure.evidence:
        assert line in message
    assert "CRC" not in message
    # a CRC error says nothing of boards
    crc = loader_failure(
        owner_log("basys3-id-error.txt").replace(
            "ID Error        ID error", "ID Error        No ID error"
        )
    )
    assert crc is not None and "cable_index" not in crc.message()


def test_the_arty_log_of_a_load_that_ended_with_done_high_passes():
    assert loader_failure(owner_log("arty-configured.txt")) is None


def test_a_bitstream_that_declares_no_length_is_explained_by_its_header():
    """The report from before `fpga-as` was fixed: the loader sent the length the header declares,
    and warned about it."""
    failure = loader_failure(EMPTY_BITSTREAM)
    assert failure is not None
    message = failure.message()
    assert "did not finish configuration" in message
    assert "sent 0 bytes" in message and "holds 3825788" in message
    assert "wrote a wrong header" in message
    assert "ID error" not in message and "CRC error" not in message
    assert failure.evidence == (
        "Parse file File is longer than bitstream length declared in the header: 3825788 vs 0",
        DONE_LOW,
        "Done            0x0",
    )


def test_a_crc_error_says_the_bitstream_is_damaged():
    log = owner_log("basys3-id-error.txt")
    log = log.replace("ID Error        ID error", "ID Error        No ID error")
    log = log.replace("CRC Error       No CRC error", "CRC Error       CRC error")
    failure = loader_failure(log)
    assert failure is not None
    assert "CRC error" in failure.message() and "damaged or incomplete" in failure.message()
    assert "ID error" not in failure.message()
    assert failure.evidence[-1] == "CRC Error       CRC error"


def test_done_low_without_a_register_dump_is_still_a_failure():
    """A loader that ends after the readback line (the dump is a second step)."""
    failure = loader_failure(f"load program\nShift IR 11\n{DONE_LOW}\n")
    assert failure is not None and failure.evidence == (DONE_LOW,)
    assert "DONE signal stayed low" in failure.message()


def test_a_step_that_printed_fail_is_a_failure_and_its_explanation_is_quoted_with_it():
    failure = loader_failure(SHORT_FILE)
    assert failure is not None
    assert failure.message().startswith("openFPGALoader exited with status 0, but it reported")
    assert failure.evidence == (
        "Parse file File is shorter than bitstream length declared in the header: 1000 vs 2000",
        "FAIL",
    )


@pytest.mark.parametrize(
    "output,line",
    [
        ("Erase Flash: FAIL\n", "Erase Flash: FAIL"),  # a label, then the status
        ("Wait for CDONE\nFail\n", "Fail"),  # the status the progress bar and iCE40 print
        ("Load SRAM \n\nLoad SRAM: [====] 100.00%\n\nDone\nFAIL\n", "FAIL"),  # Gowin: on stdout
        ("CRC check : FAIL\nRead: 0x00001234 checksum: 0x00004321\n", "CRC check : FAIL"),
    ],
)
def test_a_status_word_fail_ends_the_line_of_every_failed_step(output, line):
    failure = loader_failure(output)
    assert failure is not None and line in failure.evidence


@pytest.mark.parametrize(
    "output,line",
    [
        (
            "Error: block protection is set\n       can't unlock without --unprotect-flash\n",
            "Error: block protection is set",
        ),
        (FLASH_DOES_NOT_ANSWER, "Read ID failed"),
        ("start addr: 00000000, end_addr: 00010000\nwait: Error\n", "wait: Error"),
        ("write en: Error\n", "write en: Error"),
        # in a terminal the progress bar has no line end, so the next message follows its percent
        (
            "\rWriting: [==        ] 40.00%write en: Error\n",
            "Writing: [==        ] 40.00%write en: Error",
        ),
        # the flash write that `program_spi` ignores (xilinx.cpp:849): the bridge cannot be chosen
        (FLASH_WITHOUT_A_PART, "Can't program SPI flash: missing device-package information"),
        (
            "Can't program BPI flash: missing device-package information\n",
            "Can't program BPI flash: missing device-package information",
        ),
        # the image does not fit the chip; a block protection that stays; a register the loader
        # does not know; a PDI load whose status says failed
        ("Detected: winbond W25Q32 64 sectors size: 32Mb\nflash overflow\n", "flash overflow"),
        ("unlock blocks\ndisable protection failed\n", "disable protection failed"),
        ("Unknown Top/Bottom register\n", "Unknown Top/Bottom register"),
        ("PDI programing failed\n", "PDI programing failed"),
        # a failed verify prints `Fail`, then why; the cause is the line the user needs
        ("Failed to read flash\n", "Failed to read flash"),
        ("Verification failed at 4096\n", "Verification failed at 4096"),
        # the direct SPI mode prints `FAIL: ` and what the exception holds (main.cpp:817)
        ("Parse file DONE\nFAIL: flash write refused\n", "FAIL: flash write refused"),
        # the CPLD and platform flash programmers of the same family
        (
            "Only jed file and flash mode supported for XC95 CPLD\n",
            "Only jed file and flash mode supported for XC95 CPLD",
        ),
        ("flow erase failed\n", "flow erase failed"),
        ("Erase: fails to verify blank check\n", "Erase: fails to verify blank check"),
    ],
)
def test_the_flash_failures_the_loader_prints_and_does_not_exit_with_are_failures(output, line):
    failure = loader_failure(output)
    assert failure is not None and line in failure.evidence
    assert "reported" in failure.message()


@pytest.mark.parametrize("prefix", list(FAILURE_PREFIXES))
def test_every_failure_prefix_is_a_sign_at_the_start_of_a_line_only(prefix):
    """The sweep of the class: a prefix added to the table is judged here without a test of its
    own. The same words inside a line are the text of something else."""
    line = f"{prefix}some reason or address"
    failure = loader_failure(f"Parse file DONE\n{line}\n")
    assert failure is not None and line in failure.evidence
    assert loader_failure(f"design_name: {line}\n") is None


@pytest.mark.parametrize("message", list(FAILURE_ENDINGS))
def test_every_failure_ending_is_a_sign_at_the_end_of_a_line(message):
    """The message alone, after the label of a step that has no line end yet, and glued to the
    percent of a progress bar that a terminal redraws."""
    for line in (message, f"Erase Flash: {message}", f"Writing: [==    ] 40.00%{message}"):
        failure = loader_failure(f"{line}\n")
        assert failure is not None and line in failure.evidence, line
    assert loader_failure(f"{message} is what the manual calls it\n") is None


def test_every_sign_names_the_place_in_the_loader_s_source_that_prints_it():
    place = re.compile(r"\b[a-zA-Z0-9]+\.cpp:\d+")
    for table in (FAILURE_PREFIXES, FAILURE_ENDINGS):
        for message, where in table.items():
            assert place.search(where), f"{message!r} has no citation: {where!r}"
    assert not set(FAILURE_PREFIXES) & set(FAILURE_ENDINGS)


@pytest.mark.parametrize("form", FINISHED_FLASH_WRITES.values(), ids=FINISHED_FLASH_WRITES)
def test_a_flash_write_the_loader_reports_finished_meets_the_requirement(form):
    assert loader_failure(FLASH_FOUND + form, flash_writes=1) is None


def test_a_flash_that_stays_locked_is_no_finished_write():
    """The silent failure: nothing the loader prints says that it failed, and it exits with 0."""
    assert loader_failure(FLASH_LOCKED) is None  # an SRAM load, or a family that throws
    failure = loader_failure(FLASH_LOCKED, flash_writes=1)
    assert failure is not None
    message = failure.message()
    assert "exited with status 0, but it did not report that it wrote the flash" in message
    assert "Writing: [...] 100.00%" in message and "`Done`" in message
    assert "SST26VF" in message and "stays locked" in message
    assert "may hold its old data, or only part of the file" in message
    # where the output ends, as the loader printed it
    assert failure.evidence == (
        "JEDEC ID: 0xbf2642",
        "Detected: microchip SST26VF032B 64 sectors size: 32Mb",
        "Non Volatile",
    )
    # the flash is read back only after a write that finished: say so, and ask for it
    assert "Set `verify: true`" in message
    assert (
        "Set `verify: true`"
        not in loader_failure(FLASH_LOCKED, flash_writes=1, verify=True).message()
    )


@pytest.mark.parametrize(
    "tail",
    [
        f"\rWriting: [{HALF}] 50.00%\n",  # the write stopped half way
        f"\rErasing: [{FULL}] 100.00%\n\nDone\n",  # erased, never written
        f"\rWriting: [{FULL}] 100.00%\n",  # a bar at the end that `done()` did not finish
        f"\rWriting: [{FULL}] 100.00%\n\rWriting: [{HALF}] 50.00%\n\nDone\n",  # not the end
        "Writing: \n",  # quiet bars that did not finish
    ],
    ids=["half", "erase-only", "no-done", "done-after-half", "quiet-unfinished"],
)
def test_only_a_bar_at_the_end_followed_by_done_is_a_finished_write(tail):
    log = FLASH_FOUND + "start addr: 00000000, end_addr: 00010000\n" + tail
    failure = loader_failure(log, flash_writes=1)
    assert failure is not None and "did not report that it wrote the flash" in failure.message()


def test_a_flash_write_of_two_chips_needs_two_finished_writes():
    one = FLASH_FOUND + FLASH_BARS_1_1_1_PIPE
    assert loader_failure(one, flash_writes=1) is None
    assert loader_failure(one + FLASH_BARS_1_1_1_PIPE, flash_writes=2) is None
    failure = loader_failure(one, flash_writes=2)
    assert failure is not None
    assert "has to write 2 flash chips" in failure.message()
    assert "shows 1 finished write" in failure.message()


def test_a_sign_of_failure_leads_and_the_missing_report_is_not_added_to_it():
    log = FLASH_FOUND + "start addr: 00000000, end_addr: 00010000\nwait: Error\n"
    failure = loader_failure(log, flash_writes=1)
    assert failure is not None and failure.evidence == ("wait: Error",)
    assert "did not report" not in failure.message()
    # a finished write does not excuse a sign, either
    failure = loader_failure(
        FLASH_FOUND + FLASH_BARS_1_1_1_PIPE + "Read ID failed\n", flash_writes=1
    )
    assert failure is not None and "Read ID failed" in failure.evidence


def test_a_failed_verify_quotes_the_address_that_follows_the_fail_line():
    """`ProgressBar::fail` prints `Fail` (a bare word: the line before it is quoted for context),
    and the address comes after it (spiFlash.cpp:586-591)."""
    log = "Reading: [=====     ] 40.00%\n\nFail\nVerification failed at 4096\n"
    failure = loader_failure(log)
    assert failure is not None
    assert failure.evidence == (
        "Reading: [=====     ] 40.00%",
        "Fail",
        "Verification failed at 4096",
    )


@pytest.mark.parametrize(
    "output",
    [
        owner_log("arty-configured.txt"),
        FLASH_WRITTEN,
        ECP5_LOADED,
        "",  # a fake that prints nothing, or a loader run with --quiet over a cable that is silent
        # names that end in fail are no failed step; neither are the fields of a dump that is read
        # for information, nor an error code in a name
        "Use: /tmp/Fail\nuse /tmp/xFAIL\ndesign_name: myFail\nCRC Error       No CRC error\n",
        f"{DONE_HIGH}\nRegister raw value: 0x0\nID Error        No ID error\n",
        # a message that starts a line is a sign; the same words inside a line, or a message
        # that does not end the line, are the text of something else
        "Use: /tmp/Can't program x\ndesign_name: FAIL: x\nnote: Verification failed at the end\n",
        "the flash overflow check comes first\nRead ID failed is a message, not this line\n",
    ],
)
def test_a_log_without_a_sign_of_failure_passes(output):
    assert loader_failure(output) is None


def test_colors_and_carriage_returns_change_nothing():
    """What a loader attached to a terminal prints: escape codes around the text, and `\\r` to
    redraw the progress bar. The verdict reads the same lines."""
    for name, expected in (("basys3-id-error.txt", True), ("arty-configured.txt", False)):
        colored = owner_log(name)
        colored = "".join(f"\x1b[94m{line}\x1b[0m\r\n" for line in colored.splitlines())
        failure = loader_failure(colored)
        assert (failure is not None) is expected
        if failure is not None:
            assert failure.evidence[0] == DONE_LOW
    redrawn = "\rLoad SRAM: [==  ] 40.00%\rLoad SRAM: [====] 100.00%\n\x1b[32m\nDone\x1b[0m\n"
    assert loader_failure(redrawn + f"{DONE_HIGH}\n") is None
    assert loader_failure(redrawn + f"{DONE_LOW}\n") is not None


def test_an_error_prefix_counts_at_the_start_of_a_line_only():
    """The loader's own error messages start a line. The same words inside a line are the text of
    something else, such as a field of a register dump."""
    assert loader_failure("Error: block protection is set\n") is not None
    assert loader_failure("design_name: Error: of the design\n\t[24:23] ECC Error: none\n") is None


def test_a_failure_with_many_lines_quotes_the_first_few():
    log = "".join(f"step {n} FAIL\n" for n in range(40))
    failure = loader_failure(log)
    assert failure is not None
    assert failure.evidence[0] == "step 0 FAIL" and len(failure.evidence) <= 8


def test_the_message_is_one_text_with_the_lines_quoted_below_it():
    message = loader_failure(owner_log("basys3-id-error.txt")).message()
    head, _, quoted = message.partition("The loader printed:\n")
    assert head and quoted
    assert [line.strip() for line in quoted.splitlines()] == [
        DONE_LOW,
        "Done            0x0",
        "ID Error        ID error",
    ]


# --------------------------------------------------------------------------- the flow, end to end


def printing(monkeypatch, stdout="", stderr="", status=0):
    """The fake loader prints this and exits with this status when it programs."""
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_STDOUT", stdout)
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_STDERR", stderr)
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_STATUS", str(status))


def program(tmp_path, settings=None):
    """Launch the programmer on a prebuilt bitstream; the flow (a failed one too)."""
    assert_fake_loader()
    flow = _runner(tmp_path).run(
        "openfpgaloader", _prebuilt(tmp_path), flow_settings=settings or {"fpga": ECP5}
    )
    assert flow is not None
    return flow


def recorded(tmp_path) -> dict:
    return json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())


def test_a_load_that_reports_an_id_error_fails_although_the_loader_exits_with_0(
    tmp_path, fake_loader, monkeypatch
):
    printing(monkeypatch, stdout=owner_log("basys3-id-error.txt"))
    flow = program(tmp_path, {"board": "basys_3"})
    assert not flow.succeeded
    results = recorded(tmp_path)
    assert results["success"] is False
    error = results["error"]
    assert error["type"] == "ReportedFailure"  # the tool exited 0 and its report says otherwise
    assert "exited with status 0" in error["message"]
    assert "did not finish configuration" in error["message"]
    assert "ID Error        ID error" in error["message"]
    assert "board basys_3, part xc7a35tcpg236-1" in error["message"]  # the flow's target
    log = tmp_path / "run/top/openfpgaloader" / LOADER_LOG
    assert str(log) in error["message"]
    assert log.read_text() == owner_log("basys3-id-error.txt")  # the loader's whole output
    assert flow.results["error"] == error


def test_a_load_that_ends_with_done_high_passes(tmp_path, fake_loader, monkeypatch):
    printing(monkeypatch, stdout=owner_log("arty-configured.txt"))
    flow = program(tmp_path, {"board": "arty_a7_100t"})
    assert flow.succeeded
    # no terminal of xeda's own to show the loader's: it writes to a pipe
    (call,) = [
        json.loads(line)
        for line in (tmp_path / "run/top/openfpgaloader/fake_fpga.calls.jsonl")
        .read_text()
        .splitlines()
    ]
    assert not call["stdout_is_terminal"]
    results = recorded(tmp_path)
    assert results["success"] is True and "error" not in results


def test_a_failure_the_loader_prints_on_stderr_is_in_the_log_and_decides(
    tmp_path, fake_loader, monkeypatch
):
    """The loader's stderr and stdout are one log: `printError` writes to stderr."""
    printing(
        monkeypatch,
        stdout="Open file DONE\nParse file ",
        stderr="File is shorter than bitstream length declared in the header: 1 vs 2\nFAIL\n",
    )
    flow = program(tmp_path)
    assert not flow.succeeded
    error = recorded(tmp_path)["error"]
    assert error["type"] == "ReportedFailure" and "FAIL" in error["message"]
    log = (tmp_path / "run/top/openfpgaloader" / LOADER_LOG).read_text()
    assert log.splitlines() == [
        "Open file DONE",
        "Parse file File is shorter than bitstream length declared in the header: 1 vs 2",
        "FAIL",
    ]


def test_a_loader_that_prints_nothing_and_exits_with_0_passes(tmp_path, fake_loader):
    """The plain fake: no evidence of failure is no failure (a loader without a readback, as
    openFPGALoader before 0.13 for a Xilinx FPGA, prints no more)."""
    assert program(tmp_path).succeeded


@pytest.mark.parametrize("log", ["arty-configured.txt", "basys3-id-error.txt"])
def test_a_nonzero_exit_status_fails_the_run_and_keeps_its_own_error(
    tmp_path, fake_loader, monkeypatch, log
):
    printing(monkeypatch, stdout=owner_log(log), status=1)
    try:
        flow = program(tmp_path)
    except Exception as error:  # noqa: BLE001 - reported or raised, it is a failure
        assert type(error).__name__ == "NonZeroExitCode"
    else:
        assert not flow.succeeded
    results = recorded(tmp_path)
    assert results["success"] is False and results["error"]["type"] == "NonZeroExitCode"


def test_the_log_of_an_earlier_failure_does_not_decide_the_next_run(
    tmp_path, fake_loader, monkeypatch
):
    printing(monkeypatch, stdout=owner_log("basys3-id-error.txt"))
    assert not program(tmp_path).succeeded
    printing(monkeypatch, stdout=owner_log("arty-configured.txt"))
    assert program(tmp_path).succeeded
    results = recorded(tmp_path)
    assert results["success"] is True and "error" not in results
    printing(monkeypatch, stdout=owner_log("basys3-id-error.txt"))
    assert not program(tmp_path).succeeded  # and a failure again after a success


@pytest.mark.parametrize("earlier", ["arty-configured.txt", "basys3-id-error.txt"])
def test_a_log_a_previous_run_left_is_no_evidence_about_this_one(
    tmp_path, fake_loader, monkeypatch, earlier
):
    """If the loader leaves no log of its own, the run has no evidence that it programmed the
    device: neither a good log nor a bad one from an earlier run stands for it."""
    printing(monkeypatch, stdout=owner_log(earlier))
    first = program(tmp_path)
    assert first.succeeded is (earlier == "arty-configured.txt")
    log = tmp_path / "run/top/openfpgaloader" / LOADER_LOG
    before = log.read_bytes()
    monkeypatch.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")  # writes no log
    flow = program(tmp_path)
    assert not flow.succeeded
    error = recorded(tmp_path)["error"]
    assert error["type"] == "ReportedFailure"
    assert "left no log of this run" in error["message"] and LOADER_LOG in error["message"]
    assert "ID Error" not in error["message"] and DONE_LOW not in error["message"]
    assert log.read_bytes() == before  # xeda did not touch it


@pytest.mark.skipif(os.name != "posix", reason="a pseudo-terminal is POSIX")
@pytest.mark.parametrize("name", ["basys3-id-error.txt", "arty-configured.txt"])
def test_the_verdict_is_the_same_when_the_loader_runs_in_a_terminal(
    tmp_path, fake_loader, monkeypatch, name
):
    """In a terminal the loader has a terminal of its own (`OpenfpgaloaderTool.pseudo_terminal`):
    its log has the same lines but for the blank ones, so the verdict is the same."""
    terminal = Terminal()
    monkeypatch.setattr(proc_utils, "_tool_output", terminal.stream)
    monkeypatch.setattr("xeda.tool.console", Console(force_terminal=True, color_system="standard"))
    printing(monkeypatch, stdout=owner_log(name))
    try:
        flow = program(tmp_path, {"board": "basys_3"})
        log = (tmp_path / "run/top/openfpgaloader" / LOADER_LOG).read_text()
        shown = terminal.shown()
    finally:
        terminal.close()
    assert flow.succeeded is (name == "arty-configured.txt")
    (call,) = [
        json.loads(line)
        for line in (tmp_path / "run/top/openfpgaloader/fake_fpga.calls.jsonl")
        .read_text()
        .splitlines()
    ]
    assert call["stdout_is_terminal"] and call["stderr_is_terminal"]
    assert log.splitlines() == [line for line in owner_log(name).splitlines() if line.strip()]
    assert owner_log(name).splitlines()[1] in shown  # the terminal got the loader's output too


def test_a_programmer_in_a_chain_fails_the_chain_with_its_message(
    tmp_path, fake_loader, monkeypatch
):
    """The default graph: the bitstream is built by the fakes, and the programmer's log decides."""
    printing(monkeypatch, stdout=owner_log("basys3-id-error.txt"))
    assert_fake_loader()
    flow = _runner(tmp_path).run(
        "openfpgaloader", _design(tmp_path), flow_settings={"board": "basys_3"}
    )
    assert flow is not None and not flow.succeeded
    assert "ID Error        ID error" in recorded(tmp_path)["error"]["message"]
    for built in ("yosys_fpga", "nextpnr", "fpga_pack"):
        built_results = json.loads((tmp_path / f"run/top/{built}/results.json").read_text())
        assert built_results["success"] is True


#: A custom board whose name in the loader (`kc705`) has no part in the loader's own list.
KC705_BOARDS = """\
[MY_KC705]
openfpgaloader_board = "kc705"
fpga.part = "xc7k325tffg676-2"
"""


def flash_settings(tmp_path, **more) -> dict:
    """Settings that write the flash of the board above."""
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir(exist_ok=True)
    boards.write_text(KC705_BOARDS)
    return {"board": "MY_KC705", "custom_boards_file": "boards.toml", "write_flash": True, **more}


def test_a_flash_write_with_no_part_for_the_bridge_fails_and_says_how_to_give_one(
    tmp_path, fake_loader, monkeypatch
):
    """`program_spi` ignores the failure of its bridge (xilinx.cpp:849), so the loader exits with
    status 0 after writing nothing. The board name stays the whole of what the loader is told
    about the device: a board name gives no part."""
    head, _, line = FLASH_WITHOUT_A_PART.partition("Can't")
    printing(monkeypatch, stdout=head, stderr="Can't" + line)
    flow = program(tmp_path, flash_settings(tmp_path))
    assert not flow.succeeded
    error = recorded(tmp_path)["error"]
    assert error["type"] == "ReportedFailure"
    message = error["message"]
    assert "exited with status 0" in message
    assert "Can't program SPI flash: missing device-package information" in message
    # why, and what to set: a cable, with which the flow gives the part in place of the name
    assert "names after the part of the FPGA" in message
    assert "`cable`" in message and "`--fpga-part`" in message
    assert "`openFPGALoader --list-boards` shows the cable of each board" in message
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--board",
        "kc705",
        "--write-flash",
    ]


def test_a_cable_gives_the_flash_write_the_part_the_board_name_lacks(
    tmp_path, fake_loader, monkeypatch
):
    """The advice of the message above: with a `cable` the flow passes no board name and gives the
    part, without its speed grade, which names a bridge the loader ships
    (`spiOverJtag_xc7k325tffg676.bit.gz`). The loader then writes the flash and says so."""
    printing(monkeypatch, stdout=FLASH_FOUND + FLASH_BARS_1_1_1_PIPE)
    flow = program(tmp_path, flash_settings(tmp_path, cable="digilent"))
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "digilent",
        "--fpga-part",
        "xc7k325tffg676",
        "--write-flash",
    ]


# ------------------------------------------------- a Xilinx flash write has to report that it wrote


def xilinx_flash(**more) -> dict:
    """The settings of a flash write on a Xilinx board (the Basys 3 is bundled)."""
    return {"board": "basys_3", "write_flash": True, **more}


@pytest.mark.parametrize(
    "form,more",
    [
        (FLASH_BARS_0_13_1_PIPE, {}),
        (FLASH_BARS_1_1_1_PIPE, {}),
        (FLASH_BARS_QUIET_PIPE, {"verbose_level": -1}),
    ],
    ids=["pipe-0.13.1", "pipe-1.1.1", "quiet-pipe"],
)
def test_a_xilinx_flash_write_the_loader_reports_finished_passes(
    tmp_path, fake_loader, monkeypatch, form, more
):
    printing(monkeypatch, stdout=FLASH_FOUND + form)
    flow = program(tmp_path, xilinx_flash(**more))
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"][2:5] == ["--board", "basys3", "--write-flash"]
    assert not call["stdout_is_terminal"]  # the loader wrote its bars to a pipe
    results = recorded(tmp_path)
    assert results["success"] is True and "error" not in results


@pytest.mark.parametrize("verify", [False, True])
def test_a_xilinx_flash_write_that_stops_before_writing_fails_although_the_loader_exits_with_0(
    tmp_path, fake_loader, monkeypatch, verify
):
    printing(monkeypatch, stdout=FLASH_LOCKED)
    flow = program(tmp_path, xilinx_flash(verify=verify))
    assert not flow.succeeded
    error = recorded(tmp_path)["error"]
    assert error["type"] == "ReportedFailure"
    message = error["message"]
    assert "did not report that it wrote the flash" in message
    assert "Non Volatile" in message and str(tmp_path / "run/top/openfpgaloader") in message
    # the loader reads the flash back only after a write that it reports as done
    assert ("Set `verify: true`" in message) is not verify
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert ("--verify" in call["argv"]) is verify


def test_a_report_is_required_of_a_xilinx_flash_write_only(tmp_path, fake_loader, monkeypatch):
    """The log of a loader that stopped, from an SRAM load of a Xilinx board and from a flash write
    of an ECP5 board: the first has no flash to write, and the loader of the second throws when a
    step fails (lattice.cpp:1073-1082), so its status tells."""
    printing(monkeypatch, stdout=FLASH_LOCKED)
    assert program(tmp_path, {"board": "basys_3"}).succeeded
    assert program(tmp_path, {"board": "ulx3s_85f", "write_flash": True}).succeeded


def test_a_xilinx_flash_write_of_two_chips_is_reported_for_each(tmp_path, fake_loader, monkeypatch):
    """Two flash chips exist on a few UltraScale boards only, and the loader wants the image of the
    second one (xilinx.cpp:319-324, main.cpp:1186-1194): the device is one of those three, and
    `--secondary-bitstream` comes from `extra_args`."""
    both = {
        "fpga": "xcku040-ffva1156",
        "write_flash": True,
        "target_flash": "both",
        "extra_args": ["--secondary-bitstream", "second.bit"],
    }
    printing(monkeypatch, stdout=FLASH_FOUND + FLASH_BARS_1_1_1_PIPE)
    flow = program(tmp_path, both)
    assert not flow.succeeded
    assert "has to write 2 flash chips" in recorded(tmp_path)["error"]["message"]
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--fpga-part",
        "xcku040-ffva1156",
        "--write-flash",
        "--target-flash",
        "both",
        "--secondary-bitstream",
        "second.bit",
    ]
    printing(monkeypatch, stdout=FLASH_FOUND + FLASH_BARS_1_1_1_PIPE + FLASH_BARS_1_1_1_PIPE)
    assert program(tmp_path, both).succeeded
    printing(monkeypatch, stdout=FLASH_FOUND + FLASH_BARS_1_1_1_PIPE)
    assert program(tmp_path, {**both, "target_flash": "primary"}).succeeded


@pytest.mark.skipif(os.name != "posix", reason="a pseudo-terminal is POSIX")
@pytest.mark.parametrize(
    "stdout,passes",
    [
        (FLASH_FOUND + FLASH_BARS_1_1_1_TERMINAL, True),
        (FLASH_FOUND + FLASH_BARS_QUIET_TERMINAL, True),
        (FLASH_LOCKED, False),
    ],
    ids=["finished", "finished-quiet", "stopped"],
)
def test_a_xilinx_flash_write_is_judged_the_same_when_the_loader_runs_in_a_terminal(
    tmp_path, fake_loader, monkeypatch, stdout, passes
):
    """In a terminal the loader writes colors and redraws its bars with `\\r`; the log keeps the
    text, one line for each redraw, so the verdict reads the same lines."""
    terminal = Terminal()
    monkeypatch.setattr(proc_utils, "_tool_output", terminal.stream)
    monkeypatch.setattr("xeda.tool.console", Console(force_terminal=True, color_system="standard"))
    printing(monkeypatch, stdout=stdout)
    try:
        flow = program(tmp_path, xilinx_flash())
        log = (tmp_path / "run/top/openfpgaloader" / LOADER_LOG).read_text()
        shown = terminal.shown()
    finally:
        terminal.close()
    assert flow.succeeded is passes
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["stdout_is_terminal"] and call["stderr_is_terminal"]
    lines = log.splitlines()
    assert "\x1b" not in log and "\r" not in log
    if passes:
        assert lines[-2:] == [f"Writing: [{FULL}] 100.00%", "Done"] or lines[-1] == "Writing: Done"
    else:
        assert lines[-1] == "Non Volatile"
        assert "did not report that it wrote the flash" in recorded(tmp_path)["error"]["message"]
    assert "Detected: microchip SST26VF032B" in shown  # the terminal got the output too


def test_the_command_line_exits_with_1_and_the_message_in_the_json(tmp_path):
    """`xeda run openfpgaloader --json`, as a subprocess: the document says why."""
    design = tmp_path / "design"
    design.mkdir()
    (design / "given.bit").write_bytes(b"a bitstream built elsewhere")
    (design / "top.yaml").write_text(
        "name: top\n"
        "rtl:\n"
        "  top: top\n"
        "  sources:\n"
        '    - { file: given.bit, type: "Bitstream" }\n'
        "flows:\n"
        "  openfpgaloader:\n"
        "    board: basys_3\n"
    )
    env = dict(
        os.environ,
        COLUMNS="80",
        PATH=str(tool_utils.FAKE_TOOLS_DIR) + os.pathsep + os.environ.get("PATH", ""),
        XEDA_FAKE_FPGA_LOADER_STDOUT=owner_log("basys3-id-error.txt"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "run", "openfpgaloader", "top.yaml", "--json"],
        cwd=design,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert document["success"] is False and document["error"]["type"] == "FlowFailed"
    cause = document["results"]["error"]
    assert cause["type"] == "ReportedFailure" and "ID Error        ID error" in cause["message"]


# ------------------------------------------------------------------------------- the whole class


def unjudged_actions(classes) -> list[str]:
    """The flows that change the world outside their run directory (they declare an
    `action_reason`) and take their tool's exit status for the whole verdict: they judge neither
    in `parse_reports` nor in `check_results` what the tool printed or wrote."""
    return [
        cls.name
        for cls in classes
        if cls.action_reason
        and cls.parse_reports is Flow.parse_reports
        and cls.check_results is Flow.check_results
    ]


def test_every_flow_that_changes_the_world_judges_what_its_tool_reports():
    """A programmer is the case: a tool that acts on hardware may exit with status 0 and not have
    done it. A new flow of this kind has to say how it knows."""
    assert unjudged_actions(cls for cls, _ in flow_classes()) == []


def test_the_oracle_sees_an_action_that_takes_the_exit_status_for_the_verdict():
    class Silent(Flow):
        """Acts and judges nothing."""

        action_reason = "it flashes a thing"
        results_description: dict = {}

        def run(self) -> None:
            pass

    class Judging(Silent):
        """Acts and judges."""

        def parse_reports(self) -> bool:
            return True

    class Checking(Silent):
        """Acts and checks."""

        def check_results(self) -> bool:
            return True

    try:
        assert unjudged_actions([Silent, Judging, Checking]) == ["silent"]
    finally:
        from xeda.flow import registered_flows

        for cls in (Silent, Judging, Checking):
            for name in (cls.name, cls.__name__):
                registered_flows.pop(name, None)
