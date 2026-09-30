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


# --------------------------------------------------------------- stderr-only version banners

from pathlib import Path  # noqa: E402

from xeda.tool import Docker, Tool  # noqa: E402


def test_merge_stderr_captures_a_stderr_only_banner(tmp_path):
    """Some tools (nextpnr among them) print their version banner to stderr."""
    script = tmp_path / "banner.py"
    script.write_text("import sys; print('Version tool-1.2.3', file=sys.stderr)\n")
    tool = Tool(executable=sys.executable, default_args=[str(script)], version_flag=["--version"])
    assert not (tool.run_get_stdout("--version") or "").strip()
    assert "Version tool-1.2.3" in (tool.run_get_stdout("--version", merge_stderr=True) or "")


def test_version_detection_falls_back_to_stderr(tmp_path):
    script = tmp_path / "banner.py"
    script.write_text("import sys; print('Version tool-1.2.3', file=sys.stderr)\n")
    tool = Tool(executable=sys.executable, default_args=[str(script)])
    assert tool.version_output and "tool-1.2.3" in tool.version_output


def test_merge_stderr_is_forwarded_through_the_docker_path(monkeypatch, tmp_path):
    """The Docker branch dropped the option, so a containerized tool's banner was lost."""
    recorded = {}

    def fake_run_process(executable, args=None, **kwargs):
        recorded.update(kwargs)
        return "out"

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    # Docker.run writes a `.<name>_docker.env` file into the working directory before it reaches
    # run_process, so run somewhere disposable rather than dirtying the checkout.
    monkeypatch.chdir(tmp_path)
    tool = Tool(executable="some-tool", docker=Docker(image="img"), dockerized=True)
    tool.run("--version", stdout=True, merge_stderr=True)
    assert recorded.get("merge_stderr") is True
    assert not list(Path.cwd().parent.glob("*_docker.env"))


def test_a_docker_run_mounts_the_directory_it_runs_in_not_an_earlier_one(monkeypatch, tmp_path):
    """A tool's version is probed in a temporary directory (`Tool.probe_stdout`), gone by the
    time the flow runs the tool: were that directory still mounted, Docker would create it
    again, outside the run directory. Each run mounts the directory it runs in, and only that."""
    commands = []

    def fake_run_process(executable, args=None, **kwargs):
        commands.append([str(arg) for arg in args or []])
        return "out"

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    first, second = tmp_path / "first", tmp_path / "second"
    tool = Tool(executable="some-tool", docker=Docker(image="img"), dockerized=True)
    for cwd in (first, second):
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        tool.run("arg", stdout=True)
    assert any(str(second) in arg for arg in commands[1])
    assert not any(str(first) in arg for arg in commands[1]), commands[1]


def test_docker_mounts_the_design_read_only_and_the_run_directory_read_write(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        "xeda.tool.run_process",
        lambda executable, args=None, **kwargs: commands.append([str(a) for a in args or []]),
    )
    design_root, run_dir = tmp_path / "design", tmp_path / "design" / "xeda_run" / "d" / "f"
    (design_root / "rtl").mkdir(parents=True)
    run_dir.mkdir(parents=True)
    monkeypatch.chdir(run_dir)
    tool = Tool(
        executable="some-tool",
        docker=Docker(image="img"),
        dockerized=True,
        design_root_=design_root,
        source_dirs_=[design_root / "rtl"],
    )
    tool.run("arg", stdout=True)
    volumes = [a for a in commands[0] if a.startswith("--volume=")]
    assert f"--volume={design_root}:{design_root}:ro,z" in volumes
    assert f"--volume={design_root / 'rtl'}:{design_root / 'rtl'}:ro,z" in volumes
    assert f"--volume={run_dir}:{run_dir}:z" in volumes
    assert tool.docker.mounts == {}


def _docker_run_overrides():
    """Every `Docker` subclass that overrides `run`, however deeply nested.

    Importing `xeda.flows` walks the flow subpackages, so a tool class declaring its own
    container wrapper is registered as a subclass by the time this runs.
    """
    import xeda.flows  # noqa: F401

    found, seen, stack = [], set(), list(Docker.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        if "run" in cls.__dict__:
            found.append(cls)
    return found


@pytest.mark.parametrize("docker_cls", _docker_run_overrides(), ids=lambda c: c.__qualname__)
def test_docker_run_overrides_forward_merge_stderr(docker_cls, monkeypatch, tmp_path):
    """A subclass that accepts `merge_stderr` and forgets to pass it on ignores it silently.

    `XTclShDocker` did exactly that, so `merge_stderr=True` on the dockerized ISE path arrived
    at `run_process` as False. The base-class test above cannot see this: the option is lost in
    the override, one level up.
    """
    recorded = {}

    def fake_run_process(executable, args=None, **kwargs):
        recorded.update(kwargs)
        return "out"

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    monkeypatch.chdir(tmp_path)
    docker_cls(image="img").run("some-tool", "--version", stdout=True, merge_stderr=True)
    assert recorded.get("merge_stderr") is True, (
        f"{docker_cls.__qualname__}.run accepts merge_stderr but does not forward it to "
        "Docker.run, so callers asking for stderr silently do not get it."
    )


@pytest.mark.parametrize("docker_cls", _docker_run_overrides(), ids=lambda c: c.__qualname__)
def test_docker_run_overrides_mount_the_design_read_only(docker_cls, monkeypatch, tmp_path):
    """An override that dropped `read_only` would run its tool without the design mounted."""
    commands = []
    monkeypatch.setattr(
        "xeda.tool.run_process",
        lambda executable, args=None, **kwargs: commands.append([str(a) for a in args or []]),
    )
    design = tmp_path / "design"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    docker_cls(image="img").run("some-tool", "--version", stdout=True, read_only=[design])
    assert f"--volume={design}:{design}:ro,z" in commands[0]


def test_tool_output_redirect_is_none_by_default():
    from xeda import proc_utils

    assert proc_utils.tool_output_redirect() is None
    try:
        proc_utils.set_tool_output(sys.stderr)
        assert proc_utils.tool_output_redirect() is sys.stderr
        assert proc_utils.tool_output_stream() is sys.stderr
    finally:
        proc_utils.set_tool_output(None)
    assert proc_utils.tool_output_stream() is sys.stdout


def test_run_process_pipes_output_when_the_redirect_has_no_file_descriptor():
    """`set_tool_output` may point at a stream that exists only at the Python level -- an
    `io.StringIO`, `click.testing.CliRunner`'s captured streams, an embedding program's own
    redirected stdio -- and cannot be handed to `Popen(stdout=...)` as a file descriptor.
    `run_process` used to hand it over anyway, which raised `io.UnsupportedOperation: fileno`
    deep inside `subprocess` the moment any code ran a real flow under `--json`; it must instead
    fall back to piping and copying, exactly as the highlighting path already does."""
    import io

    from xeda import proc_utils

    previous = proc_utils.tool_output_redirect()
    buffer = io.StringIO()
    proc_utils.set_tool_output(buffer)
    try:
        run_process(sys.executable, ["-c", "print('hi')"])
    finally:
        proc_utils.set_tool_output(previous)

    assert "hi" in buffer.getvalue()


# ---- a time limit and an output copy ---------------------------------------------------------
import time  # noqa: E402

from xeda.proc_utils import ProcessTimeout  # noqa: E402


def test_a_process_past_its_time_limit_is_stopped_and_reported():
    started = time.monotonic()
    with pytest.raises(ProcessTimeout) as info:
        run_process(sys.executable, ["-c", "import time; time.sleep(60)"], timeout=0.5)
    assert time.monotonic() - started < 20
    assert info.value.timeout == 0.5
    assert "0.5" in str(info.value)


def test_a_process_within_its_time_limit_is_unaffected():
    assert run_process(sys.executable, ["-c", "print('ok')"], stdout=True, timeout=30) == "ok"


def test_a_captured_process_past_its_time_limit_is_stopped(tmp_path):
    with pytest.raises(ProcessTimeout):
        run_process(sys.executable, ["-c", "import time; time.sleep(60)"], stdout=True, timeout=0.5)
    with pytest.raises(ProcessTimeout):
        run_process(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            stdout=tmp_path / "out.txt",
            timeout=0.5,
        )


def test_a_highlighted_process_past_its_time_limit_is_stopped():
    with pytest.raises(ProcessTimeout):
        run_process(
            sys.executable,
            ["-c", "import time; print('x', flush=True); time.sleep(60)"],
            highlight_rules={"x": ""},
            timeout=0.5,
        )


def test_tee_writes_the_output_to_a_file_and_still_shows_it(tmp_path, capsys):
    log = tmp_path / "sim.log"
    run_process(sys.executable, ["-c", "print('one'); print('two')"], tee=log)
    assert log.read_text().splitlines() == ["one", "two"]
    shown = capsys.readouterr().out
    assert "one" in shown and "two" in shown


def test_tee_keeps_the_output_of_a_failed_process(tmp_path):
    log = tmp_path / "sim.log"
    with pytest.raises(NonZeroExitCode):
        run_process(sys.executable, ["-c", "print('why'); raise SystemExit(3)"], tee=log)
    assert log.read_text().splitlines() == ["why"]


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX stops the whole process group; Windows only the process"
)
def test_the_time_limit_stops_the_process_tree(tmp_path):
    """A simulator started through a script: the child is stopped with it."""
    marker = tmp_path / "late"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()"
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    with pytest.raises(ProcessTimeout):
        run_process(sys.executable, ["-c", parent], timeout=0.5)
    time.sleep(4)
    assert not marker.exists()


# ---- fix round 1: the watchdog's lifecycle ---------------------------------------------------
import subprocess  # noqa: E402

from xeda.proc_utils import _Deadline  # noqa: E402


class _Finished:
    """A stand-in for a process that has already ended by itself."""

    pid = 2**22 + 12345
    killed = False

    def __init__(self):
        self.args = ["done"]

    def poll(self):
        return 0

    def send_signal(self, sig):
        self.killed = True

    def kill(self):
        self.killed = True


def test_a_process_that_ended_by_itself_is_not_a_timeout(monkeypatch):
    proc = _Finished()
    monkeypatch.setattr("os.killpg", lambda *a: setattr(proc, "killed", True))
    with _Deadline(proc, 0.05, group=True) as deadline:  # type: ignore[arg-type]
        time.sleep(0.3)
    assert not deadline.expired
    assert not proc.killed


def test_a_cancelled_deadline_never_signals(monkeypatch):
    proc = _Finished()
    with _Deadline(proc, 0.2) as deadline:  # type: ignore[arg-type]
        deadline.cancel()
        time.sleep(0.4)
    assert not deadline.expired and not proc.killed


def test_a_process_that_exits_within_its_limit_is_never_a_timeout():
    for _ in range(5):
        run_process(sys.executable, ["-c", "pass"], timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_an_interrupt_stops_the_whole_process_tree(tmp_path):
    marker = tmp_path / "late"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()"
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    proc = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
    time.sleep(0.5)
    with pytest.raises(KeyboardInterrupt):
        with _Deadline(proc, 60, group=True):
            raise KeyboardInterrupt
    assert proc.poll() is not None
    time.sleep(4)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_an_interrupt_while_copying_output_stops_the_process(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.print", interrupted)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_process(
            sys.executable,
            ["-c", "import time; print('x', flush=True); time.sleep(60)"],
            highlight_rules={"x": ""},
            timeout=60,
        )
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("bad", [0, -1, 0.0])
def test_a_non_positive_time_limit_is_refused(bad):
    with pytest.raises(ValueError, match=str(bad)):
        run_process(sys.executable, ["-c", "pass"], timeout=bad)


@pytest.mark.parametrize("stdout", [True, "out.txt"])
def test_tee_with_captured_output_is_refused(tmp_path, stdout):
    with pytest.raises(ValueError, match="tee"):
        run_process(sys.executable, ["-c", "pass"], stdout=stdout, tee=tmp_path / "log")


def test_a_docker_run_names_its_container_and_stops_it_when_stopped(monkeypatch, tmp_path):
    calls = []

    def fake_run_process(executable, args=None, **kwargs):
        calls.append((list(args or []), kwargs))
        if kwargs.get("on_stop"):
            kwargs["on_stop"]()
            raise ProcessTimeout([executable], kwargs["timeout"])
        return None

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    monkeypatch.chdir(tmp_path)
    tool = Tool(executable="some-tool", docker=Docker(image="img"), dockerized=True)
    with pytest.raises(ProcessTimeout):
        tool.run("arg", timeout=3)
    (run_args, run_kwargs), (kill_args, kill_kwargs) = calls
    name = run_args[run_args.index("--name") + 1]
    assert name.startswith("xeda-")
    assert kill_args == ["kill", name]
    assert kill_kwargs.get("check") is False
    assert run_kwargs["timeout"] == 3
