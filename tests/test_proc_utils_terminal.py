"""A tool that asks for a terminal gets one when xeda's own output is a terminal.

A tool that is run through a pipe sees no terminal, so it writes no colors, and a progress bar
that it redraws with a carriage return becomes a line for each update (the carriage return is a
line end for the reader of the pipe), with a blank line between. Some tools ask for a terminal
(`Tool.pseudo_terminal`, the loader of `openfpgaloader`): `run_process(terminal=True)` then gives
the tool a pseudo-terminal in place of the pipe, so the tool behaves as it does when a person runs
it, and copies what it writes to xeda's terminal as it is. The log (`tee`) gets the same text
without the escape codes and with a line for each redraw.

All of this needs real pseudo-terminals: `capsys` is not a terminal, so it only shows that a tool
gets no terminal of its own when there is none to show it on.
"""

import os
import pty
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from xeda import proc_utils
from xeda.flows.openfpgaloader import OpenfpgaloaderTool
from xeda.proc_utils import run_process
from xeda.tool import Tool
from xeda.utils import NonZeroExitCode, live_log

from .test_proc_utils import (
    LEADER_PROBE,
    TIME_LIMITS,
    _is_running,
    _kill,
    _orphaning,
    _read_pid,
    _stopped_within,
    _tree,
    _wait_until,
)

pytestmark = [
    pytest.mark.python_compat,
    pytest.mark.skipif(os.name != "posix", reason="pseudo-terminals are POSIX"),
]

#: Written to the terminal after the run, so that a test knows that everything before it arrived.
END = "\x00END\x00"

#: What a tool with a progress bar and colors prints: the bar is redrawn in place when the output
#: is a terminal, and is a line for each update when it is not (the loader's way).
PROGRESS = r"""
import os, sys, time
tty = os.isatty(1)
print("stdout is a terminal:", tty, "| stderr is a terminal:", os.isatty(2), flush=True)
for step in (33, 66, 100):
    sys.stdout.write("\rLoad SRAM: [" + "=" * (step // 2) + "] %d%%" % step + ("" if tty else "\n"))
    sys.stdout.flush()
    time.sleep(0.05)
sys.stdout.write("\n\x1b[32mDone\x1b[0m\n")
sys.stdout.flush()
sys.stderr.write("\x1b[31mFAIL\x1b[0m\n")
"""


class Terminal:
    """xeda's own terminal: the stream the tool output goes to, a pseudo-terminal in raw mode (so
    that the bytes written to it are the bytes read from it), and what was written to it."""

    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        attributes = termios.tcgetattr(slave)
        attributes[1] &= ~termios.OPOST  # no NL -> CR NL: the bytes reach the master as written
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        self.stream = os.fdopen(slave, "w", encoding="utf-8", newline="", buffering=1)
        self.chunks: list[bytes] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.chunks.append(data)

    def shown(self) -> str:
        """Everything written to the terminal so far, once it has all arrived."""
        self.stream.write(END)
        self.stream.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            text = b"".join(self.chunks).decode("utf-8", errors="replace")
            if text.endswith(END):
                return text[: -len(END)]
            time.sleep(0.01)
        raise AssertionError("the terminal did not receive what was written to it")

    def close(self) -> None:
        self.stream.close()  # the reader reads the end of the output and returns
        self.reader.join(30)  # a later terminal must not get the number of a descriptor still read
        assert not self.reader.is_alive(), "the output of the terminal did not end"
        os.close(self.master)


@pytest.fixture
def terminal(monkeypatch):
    shown = Terminal()
    monkeypatch.setattr(proc_utils, "_tool_output", shown.stream)
    yield shown
    shown.close()


class GoneTerminal:
    """xeda's own terminal when it is gone (a closed ssh session): every write raises."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1

    def write(self, text: str) -> int:
        raise OSError(5, "the terminal is gone")

    def flush(self) -> None:
        pass


def child(code: str) -> list[str]:
    return ["-c", code]


def log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_a_tool_that_asks_for_a_terminal_has_one_when_the_output_is_one(terminal, tmp_path):
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(PROGRESS), tee=log, terminal=True, merge_stderr=True)
    assert log_lines(log)[0] == "stdout is a terminal: True | stderr is a terminal: True"
    assert "stdout is a terminal: True" in terminal.shown()


def test_a_tool_that_does_not_ask_gets_the_pipe_it_always_got(terminal, tmp_path):
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(PROGRESS), tee=log)
    assert log_lines(log)[0] == "stdout is a terminal: False | stderr is a terminal: False"


def test_a_tool_that_asks_has_no_terminal_when_the_output_is_none(tmp_path, capfd):
    """Under `pytest`, a pipe or a file (a CI log, `--json` captured by an agent): the tool sees
    what it saw before, and the log is the lines it wrote."""
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(PROGRESS), tee=log, terminal=True, merge_stderr=True)
    lines = log_lines(log)
    assert lines[0] == "stdout is a terminal: False | stderr is a terminal: False"
    assert "Load SRAM: [" + "=" * 50 + "] 100%" in lines  # a line for each update, as before


def test_the_terminal_gets_what_the_tool_wrote_exactly_colors_and_redraws_included(
    terminal, tmp_path
):
    run_process(
        sys.executable,
        child(PROGRESS),
        tee=tmp_path / "tool.log",
        terminal=True,
        merge_stderr=True,
    )
    shown = terminal.shown()
    # the bar is redrawn in place: a carriage return before each update, no line end between them
    assert "\rLoad SRAM: [" + "=" * 16 + "] 33%\rLoad SRAM: [" + "=" * 33 + "] 66%\r" in shown
    assert "\x1b[32mDone\x1b[0m" in shown and "\x1b[31mFAIL\x1b[0m" in shown


def test_the_log_has_the_text_without_escape_codes_and_a_line_for_each_redraw(terminal, tmp_path):
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(PROGRESS), tee=log, terminal=True, merge_stderr=True)
    text = log.read_text(encoding="utf-8")
    assert "\x1b" not in text and "\r" not in text
    assert log_lines(log) == [
        "stdout is a terminal: True | stderr is a terminal: True",
        "Load SRAM: [" + "=" * 16 + "] 33%",
        "Load SRAM: [" + "=" * 33 + "] 66%",
        "Load SRAM: [" + "=" * 50 + "] 100%",
        "Done",
        "FAIL",  # stderr is the same terminal: one stream, in the order it was written
    ]


def test_a_failing_tool_still_fails_and_leaves_its_log(terminal, tmp_path):
    log = tmp_path / "tool.log"
    code = "import sys; print('about to fail', flush=True); sys.exit(3)"
    with pytest.raises(NonZeroExitCode) as raised:
        run_process(sys.executable, child(code), tee=log, terminal=True)
    assert raised.value.exit_code == 3
    assert log_lines(log) == ["about to fail"]


def test_a_tool_past_its_time_limit_is_stopped_as_before(terminal, tmp_path):
    code = "import time; print('started', flush=True); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(proc_utils.ProcessTimeout):
        run_process(
            sys.executable, child(code), tee=tmp_path / "tool.log", terminal=True, timeout=1
        )
    assert time.monotonic() - started < 30


def test_a_terminal_that_fails_leaves_the_log_what_the_tool_had_written(monkeypatch, tmp_path):
    """The log is written before the terminal, as the lines of a pipe are: a terminal that is gone
    (a closed ssh session) raises, the tool is stopped, and the log has what came before."""

    monkeypatch.setattr(proc_utils, "_tool_output", GoneTerminal())
    log = tmp_path / "tool.log"
    code = "import time; print('first', flush=True); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(OSError, match="the terminal is gone"):
        run_process(sys.executable, child(code), tee=log, terminal=True)
    assert time.monotonic() - started < 30  # the tool did not run on
    assert log_lines(log) == ["first"]


def test_a_terminal_that_fails_leaves_the_log_the_line_the_tool_had_not_ended(
    monkeypatch, tmp_path
):
    """The tool wrote `first` and has not ended the line, and the terminal fails on that text. The
    log has `first`, as it has the last line when the output ends. The tool writes no line end
    until it is stopped, so the terminal gets `first` alone in its first write."""

    monkeypatch.setattr(proc_utils, "_tool_output", GoneTerminal())
    log = tmp_path / "tool.log"
    code = "import sys, time; sys.stdout.write('first'); sys.stdout.flush(); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(OSError, match="the terminal is gone"):
        run_process(sys.executable, child(code), tee=log, terminal=True)
    assert time.monotonic() - started < 30  # the tool did not run on
    assert log_lines(log) == ["first"]


def test_a_terminal_that_fails_leaves_the_log_a_character_the_tool_had_not_ended(
    monkeypatch, tmp_path
):
    """The tool wrote `first` and the first byte of a character of two bytes. The terminal fails
    on that text. The log has what it has when the output ends there: `first` and a replacement
    character for the cut character."""
    monkeypatch.setattr(proc_utils, "_tool_output", GoneTerminal())
    log = tmp_path / "tool.log"
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'first\\xc3')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(OSError, match="the terminal is gone"):
        run_process(sys.executable, child(code), tee=log, terminal=True)
    assert log_lines(log) == ["first\ufffd"]


def test_the_output_that_ends_in_a_cut_character_has_the_replacement_character_once(
    terminal, tmp_path
):
    code = "import sys; sys.stdout.buffer.write(b'first\\xc3')"
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(code), tee=log, terminal=True)
    assert log_lines(log) == ["first\ufffd"]
    assert terminal.shown() == "first\ufffd"


def test_the_last_output_of_a_child_that_ended_is_not_lost(tmp_path):
    """macOS discards what the master has not read when the last slave end closes, so the copy
    of the output holds a slave end until the child has ended and its output is read. Only
    macOS tells the held slave end from a closed one; on Linux the output is kept either way."""
    master, slave = pty.openpty()
    held = [slave]  # the slave end the copy releases
    shown = tmp_path / "shown.txt"
    log = tmp_path / "tool.log"
    try:
        child_slave = os.dup(slave)
        os.write(child_slave, b"first\xc3")
        os.close(child_slave)  # the child writes and ends: only the held slave end is left
        with (
            live_log(log, encoding="utf-8") as log_file,
            shown.open("w", encoding="utf-8", newline="") as stream,
        ):
            proc_utils._copy_from_terminal(
                master, log_file, stream, lambda: True, lambda: os.close(held.pop())
            )
    finally:
        for fd in (master, *held):
            os.close(fd)
    assert log_lines(log) == ["first\ufffd"]
    assert shown.read_text(encoding="utf-8") == "first\ufffd"


def test_a_tool_that_ends_before_its_output_is_read_keeps_it(terminal, tmp_path, monkeypatch):
    """The same through `run_process`: the copy starts only after the tool has ended, so the
    output is still there only if Xeda still holds its slave end."""
    copy = proc_utils._copy_from_terminal

    def copy_after_the_end(master, log_file, stream, child_ended, release):
        deadline = time.monotonic() + 30
        while not child_ended():
            assert time.monotonic() < deadline, "the tool did not end"
            time.sleep(0.01)
        copy(master, log_file, stream, child_ended, release)

    monkeypatch.setattr(proc_utils, "_copy_from_terminal", copy_after_the_end)
    code = "import sys; sys.stdout.buffer.write(b'first\\xc3')"
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(code), tee=log, terminal=True)
    assert log_lines(log) == ["first\ufffd"]
    assert terminal.shown() == "first\ufffd"


def test_what_a_tool_writes_in_pieces_arrives_whole(terminal, tmp_path):
    """A character of more than a byte, cut in two by the tool's own writes, and a line that
    ends only after a pause: the log has them whole."""
    code = (
        "import os, time\n"
        "data = 'caf\\u00e9 \\u2713 done\\n'.encode()\n"
        "for index in range(len(data)):\n"
        "    os.write(1, data[index:index + 1]); time.sleep(0.01)\n"
    )
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(code), tee=log, terminal=True)
    assert log_lines(log) == ["café ✓ done"]
    assert "café ✓ done" in terminal.shown()


def test_a_lot_of_output_does_not_stop_the_tool(terminal, tmp_path):
    code = "for n in range(20000): print('line', n, 'x' * 40)"
    log = tmp_path / "tool.log"
    run_process(sys.executable, child(code), tee=log, terminal=True)
    lines = log_lines(log)
    assert (
        len(lines) == 20000
        and lines[0].startswith("line 0 ")
        and lines[-1].startswith("line 19999 ")
    )


def test_nothing_is_left_open(terminal, tmp_path):
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        run_process(sys.executable, child("print('x')"), tee=tmp_path / "tool.log", terminal=True)
    assert len(os.listdir("/dev/fd")) <= before


def test_a_log_that_cannot_be_made_starts_no_tool(terminal, tmp_path):
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w').close()"
    with pytest.raises(OSError):
        run_process(
            sys.executable,
            child(code),
            tee=tmp_path / "missing" / "dir" / "tool.log",
            terminal=True,
        )
    assert not marker.exists()


def test_highlighting_gives_way_to_the_terminal(terminal, tmp_path):
    """A tool that asks for a terminal colors its own output."""
    log = tmp_path / "tool.log"
    run_process(
        sys.executable,
        child("print('ERROR: nothing to see')"),
        tee=log,
        terminal=True,
        highlight_rules={r"^(ERROR:)(.+)$": "<<"},
    )
    assert log_lines(log) == ["ERROR: nothing to see"]
    assert "<<" not in terminal.shown()


# ----------------------------------------------------------------------------------- the tools


def test_only_a_tool_that_declares_a_terminal_asks_for_one():
    assert Tool.pseudo_terminal is False
    assert OpenfpgaloaderTool.pseudo_terminal is True


@pytest.mark.parametrize("colors", [True, False])
def test_a_tool_asks_for_a_terminal_if_it_declares_one_and_colors_are_wanted(
    monkeypatch, tmp_path, colors
):
    """`console_colors` is off when the user switched colors off or the console has none."""
    asked = {}

    def record(executable, args, **kwargs):
        asked.update(kwargs)

    monkeypatch.setattr("xeda.tool.run_process", record)
    tool = OpenfpgaloaderTool(console_colors=colors)
    tool.console_colors = colors
    tool.run("--bitstream", "x.bit", tee=tmp_path / "loader.log", merge_stderr=True, cwd=tmp_path)
    assert asked["terminal"] is colors
    plain = Tool(executable="some-tool")
    plain.run(tee=tmp_path / "plain.log", cwd=tmp_path)
    assert asked["terminal"] is False


def test_a_tool_with_a_terminal_leads_a_process_group_and_a_session_of_its_own(terminal, tmp_path):
    """The pseudo-terminal route starts the tool as the other routes do, with and without a time
    limit. The terminal is the output of the tool, not its controlling terminal."""
    record = tmp_path / "record"
    for timeout in (None, 60):
        run_process(
            sys.executable,
            ["-c", LEADER_PROBE, str(record)],
            tee=tmp_path / "tool.log",
            terminal=True,
            timeout=timeout,
        )
    assert record.read_text().splitlines() == ["True True"] * 2
    run_process(
        sys.executable,
        child("import os; print(os.isatty(1))"),
        tee=tmp_path / "tty.log",
        terminal=True,
    )
    assert log_lines(tmp_path / "tty.log") == ["True"]


def test_an_interrupt_stops_what_a_tool_with_a_terminal_started(terminal, tmp_path, monkeypatch):
    """Ctrl-C while the tool runs, with no time limit, ends the child that the tool started."""
    started = tmp_path / "started"

    def interrupted(*args, **kwargs):
        _wait_until(lambda: _read_pid(started), "the child to run")
        raise KeyboardInterrupt

    monkeypatch.setattr(proc_utils, "_copy_from_terminal", interrupted)
    grandchild = None
    try:
        with pytest.raises(KeyboardInterrupt), _stopped_within():
            run_process(
                sys.executable,
                child(_tree(started)),
                tee=tmp_path / "tool.log",
                terminal=True,
            )
        grandchild = _read_pid(started)
        assert grandchild is not None, "the child did not run"
        _wait_until(lambda: not _is_running(grandchild), "the child to stop")
    except BaseException:
        _kill(grandchild or _read_pid(started))  # a failed test leaves no sleeping process
        raise


def test_an_interrupt_stops_a_child_that_outlived_a_tool_with_a_terminal(
    terminal, tmp_path, monkeypatch
):
    """The tool ended and its child keeps the terminal open, so Xeda still copies the output when
    Ctrl-C comes. Xeda did not reap the tool while it watched for its end, so it still stops the
    group of the tool."""
    started = tmp_path / "started"
    trigger = "interrupt now"

    class Interrupting:
        """The terminal of Xeda, which raises `KeyboardInterrupt` as it gets `trigger`."""

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return terminal.stream.fileno()

        def write(self, text: str) -> int:
            if trigger in text:
                raise KeyboardInterrupt
            return terminal.stream.write(text)

        def flush(self) -> None:
            terminal.stream.flush()

    monkeypatch.setattr(proc_utils, "_tool_output", Interrupting())
    grandchild = None
    try:
        with pytest.raises(KeyboardInterrupt), _stopped_within():
            run_process(
                sys.executable,
                child(_orphaning(started, announce=trigger)),
                tee=tmp_path / "tool.log",
                terminal=True,
            )
        grandchild = _read_pid(started)
        assert grandchild is not None, "the child did not run"
        _wait_until(lambda: not _is_running(grandchild), "the child to stop")
    except BaseException:
        _kill(grandchild or _read_pid(started))  # a failed test leaves no sleeping process
        raise


def test_a_time_limit_stops_what_a_tool_with_a_terminal_started(terminal, tmp_path):
    grandchild = None
    try:
        for attempt, limit in enumerate(TIME_LIMITS):
            started = tmp_path / f"started-{attempt}"
            with pytest.raises(proc_utils.ProcessTimeout), _stopped_within(limit):
                run_process(
                    sys.executable,
                    child(_tree(started)),
                    tee=tmp_path / "tool.log",
                    terminal=True,
                    timeout=limit,
                )
            grandchild = _read_pid(started)
            if grandchild is not None:  # the child ran when the limit expired
                break
        assert grandchild is not None, f"the child did not run within {limit} s"
        _wait_until(lambda: not _is_running(grandchild), "the child to stop")
    except BaseException:
        _kill(grandchild)
        raise
