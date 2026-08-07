"""Tests for `run_process`'s highlighting branch (the one vivado/vcs/dc/ise
take, via `highlight_rules`).

What matters here is the carriage return printed after each line. A tool that
allocates a tty of its own -- notably anything run through `docker -t -i`, which
is how vivado is commonly wrapped -- switches the terminal into raw mode while
it runs. In raw mode ONLCR is off, so a bare "\\n" moves the cursor down without
returning the carriage and the tool's output walks diagonally down the screen.

The CR must therefore be emitted whenever a *raw* terminal will display the
output -- including when xeda's own stdout is a pipe because a wrapper script or
program is relaying our output to its terminal -- and must NOT be emitted when
nothing but a file or a CI log is on the other end.

These cases need real ptys: `capsys`/`capfd` are not ttys, so they exercise only
the no-terminal half.
"""

import os
import pty
import select
import subprocess
import sys
import termios
from pathlib import Path

import pytest

from xeda.proc_utils import run_process
from xeda.utils import NonZeroExitCode

# a rule shaped like the real ones in the vivado/vcs/dc flows
HIGHLIGHT = {r"^(ERROR:)(.+)$": "<<" + r"\g<0>"}

TOOL_OUTPUT = "****** Vivado v2024.2\n  **** SW Build 5239630\n"
LINES = TOOL_OUTPUT.count("\n")


def _probe_script(tmp_path: Path) -> Path:
    """A script running xeda's `run_process` over a fake tool, so it can be
    launched with stdin/stdout of our choosing."""
    script = tmp_path / "probe.py"
    fake_tool = f"import sys; sys.stdout.write({TOOL_OUTPUT!r})"
    script.write_text(
        "import sys\n"
        "from xeda.proc_utils import run_process\n"
        f"run_process(sys.executable, ['-c', {fake_tool!r}], highlight_rules={HIGHLIGHT!r})\n"
    )
    return script


def _raw_pty():
    """A pty in raw mode -- ONLCR cleared, as `docker -t -i` leaves a terminal."""
    read_fd, write_fd = pty.openpty()
    attrs = termios.tcgetattr(write_fd)
    attrs[1] &= ~termios.OPOST  # clears ONLCR
    termios.tcsetattr(write_fd, termios.TCSANOW, attrs)
    return read_fd, write_fd


def _drain(fd) -> bytes:
    chunks = []
    while select.select([fd], [], [], 10)[0]:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def run_probe(tmp_path: Path, *, stdout, stdin=subprocess.DEVNULL, new_session=False):
    script = _probe_script(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=stdout,
        stdin=stdin,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )
    return proc


def test_output_to_a_raw_terminal_gets_the_carriage_return(tmp_path):
    """Regression test for vivado output staircasing down the screen when xeda
    is run straight from a terminal."""
    read_fd, write_fd = _raw_pty()
    proc = run_probe(tmp_path, stdout=write_fd)
    os.close(write_fd)
    out = _drain(read_fd).decode()
    os.close(read_fd)
    proc.wait()

    assert out == "****** Vivado v2024.2\n\r  **** SW Build 5239630\n\r", repr(out)
    assert out.count("\r") == LINES


def test_piped_stdout_gets_no_carriage_return_even_with_a_raw_terminal(tmp_path):
    """When our stdout is a pipe we must NOT add a carriage return, even though
    a terminal exists and a tool has put it in raw mode.

    Whoever is reading the pipe decides how the output reaches a terminal. A
    wrapper that reads us line by line (a shell script, `tee`, Scala's
    `ProcessOutput.Readlines`, `for line in proc.stdout`) strips our line
    endings and re-emits its own, so a CR we added could never reach the
    terminal -- and a lone '\\r' is itself a line terminator to those readers,
    so it would surface as a blank line after every line. See
    `test_output_survives_a_line_reading_wrapper`.
    """
    tty_read_fd, tty_write_fd = _raw_pty()
    proc = run_probe(tmp_path, stdout=subprocess.PIPE, stdin=tty_write_fd)
    os.close(tty_write_fd)
    assert proc.stdout is not None
    out = proc.stdout.read().decode()
    proc.wait()
    os.close(tty_read_fd)

    assert out == TOOL_OUTPUT, repr(out)
    assert "\r" not in out


def test_output_survives_a_line_reading_wrapper(tmp_path):
    """xeda is often driven by a program that reads its output line by line and
    reprints it. Our output has to come through such a wrapper unchanged -- no
    blank line injected after every line."""
    tty_read_fd, tty_write_fd = _raw_pty()
    proc = run_probe(tmp_path, stdout=subprocess.PIPE, stdin=tty_write_fd)
    os.close(tty_write_fd)
    assert proc.stdout is not None
    captured = proc.stdout.read().decode()
    proc.wait()
    os.close(tty_read_fd)

    # what a line-reading wrapper (Readlines + println, `tee`, ...) would relay
    relayed = "".join(line.rstrip("\r\n") + "\n" for line in captured.splitlines(keepends=True))
    assert relayed == TOOL_OUTPUT, repr(relayed)
    assert "\n\n" not in relayed


def test_output_to_a_cooked_terminal_needs_no_carriage_return(tmp_path):
    """A terminal in its normal mode expands '\\n' to CR+LF itself, so adding a
    CR would be pointless noise."""
    read_fd, write_fd = pty.openpty()  # left cooked: ONLCR on
    proc = run_probe(tmp_path, stdout=write_fd)
    os.close(write_fd)
    out = _drain(read_fd).decode()
    os.close(read_fd)
    proc.wait()

    # the pty itself turns each '\n' into '\r\n'; what matters is that xeda did
    # not add one of its own on top
    assert out == "****** Vivado v2024.2\r\n  **** SW Build 5239630\r\n", repr(out)


def test_redirected_output_with_no_terminal_has_no_carriage_returns(tmp_path):
    """`xeda ... > build.log` from CI: no terminal anywhere, so no raw mode to
    compensate for. A CR here would leave a stray ^M on every line of the log."""
    log = tmp_path / "build.log"
    with open(log, "wb") as f:
        # new_session detaches from any controlling terminal pytest may have,
        # so this is a genuine "no terminal at all" run
        run_probe(tmp_path, stdout=f, new_session=True).wait()

    assert log.read_bytes() == TOOL_OUTPUT.encode(), log.read_bytes()
    assert b"\r" not in log.read_bytes()


def test_highlighting_is_applied(capsys):
    run_process(
        sys.executable,
        ["-c", "import sys; sys.stdout.write('ERROR: boom\\n')"],
        highlight_rules=HIGHLIGHT,
    )
    assert capsys.readouterr().out.startswith("<<ERROR: boom")


def test_non_matching_lines_are_left_alone(capsys):
    run_process(
        sys.executable,
        ["-c", "import sys; sys.stdout.write('INFO: nothing special\\n')"],
        highlight_rules=HIGHLIGHT,
    )
    assert capsys.readouterr().out == "INFO: nothing special\n"


def test_non_zero_exit_still_raises(capsys):
    with pytest.raises(NonZeroExitCode):
        run_process(
            sys.executable,
            ["-c", "import sys; sys.stdout.write('ERROR: bad\\n'); sys.exit(2)"],
            highlight_rules=HIGHLIGHT,
            check=True,
        )
    assert "ERROR: bad" in capsys.readouterr().out
