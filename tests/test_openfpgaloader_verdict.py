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
import subprocess
import sys
from pathlib import Path

import pytest

from xeda.flow import Flow
from xeda.flows.openfpgaloader import LOADER_LOG, loader_failure

from . import tool_utils
from .settings_samples import flow_classes
from .test_openfpgaloader import ECP5, _design, _prebuilt, _runner, assert_fake_loader

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
    assert "`cable_index`" in message and "several boards" in message
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
    ],
)
def test_the_flash_failures_the_loader_prints_and_does_not_exit_with_are_failures(output, line):
    failure = loader_failure(output)
    assert failure is not None and line in failure.evidence
    assert "reported" in failure.message()


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
