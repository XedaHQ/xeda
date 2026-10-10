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
from xeda.utils import NonZeroExitCode

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
        self.stream.close()
        os.close(self.master)


@pytest.fixture
def terminal(monkeypatch):
    shown = Terminal()
    monkeypatch.setattr(proc_utils, "_tool_output", shown.stream)
    yield shown
    shown.close()


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

    class GoneTerminal:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 1

        def write(self, text: str) -> int:
            raise OSError(5, "the terminal is gone")

        def flush(self) -> None:
            pass

    monkeypatch.setattr(proc_utils, "_tool_output", GoneTerminal())
    log = tmp_path / "tool.log"
    code = "import time; print('first', flush=True); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(OSError, match="the terminal is gone"):
        run_process(sys.executable, child(code), tee=log, terminal=True)
    assert time.monotonic() - started < 30  # the tool did not run on
    assert log_lines(log) == ["first"]


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
    tool.run("--bitstream", "x.bit", tee=tmp_path / "loader.log", merge_stderr=True)
    assert asked["terminal"] is colors
    plain = Tool(executable="some-tool")
    plain.run(tee=tmp_path / "plain.log")
    assert asked["terminal"] is False
