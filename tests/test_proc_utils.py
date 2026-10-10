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

import contextlib
import os
import pty
import select
import signal
import stat
import subprocess
import sys
import termios
from pathlib import Path

import psutil
import pytest

from xeda import utils as xeda_utils
from xeda.proc_utils import run_process
from xeda.utils import NonZeroExitCode, live_log

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
    assert f"--volume={design_root}:{design_root}:ro" in volumes
    assert f"--volume={design_root / 'rtl'}:{design_root / 'rtl'}:ro" in volumes
    assert f"--volume={run_dir}:{run_dir}" in volumes
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
    assert f"--volume={design}:{design}:ro" in commands[0]


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

#: Time limits, in seconds, for a test whose tool must get somewhere (print a line, start a
#: child) before the limit expires. The limit starts when the tool starts, so the test cannot wait
#: for the tool first. The first limit is enough on an idle machine. A busy machine gets the next
#: one when the tool was too slow.
TIME_LIMITS = (0.5, 2, 8, 32)

#: How long after its limit (or after an interrupt) a watchdog may take to stop a process. The
#: processes of these tests sleep for 60 s. A watchdog that stopped nothing would let the call
#: wait until they end by themselves, and the call would raise what the test expects all the
#: same. The margin is far above any stop, even on a busy machine, and far below the sleep.
STOP_MARGIN = 20


@contextlib.contextmanager
def _stopped_within(limit: float = 0):
    """The block, which waits for a process that sleeps for 60 s, ends soon after `limit` s."""
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        assert elapsed < limit + STOP_MARGIN, (
            f"the process lived {elapsed:.0f} s, past its limit of {limit} s and the margin of "
            f"{STOP_MARGIN} s: it ended by itself, nothing stopped it"
        )


def test_a_process_past_its_time_limit_is_stopped_and_reported():
    with pytest.raises(ProcessTimeout) as info, _stopped_within(0.5):
        run_process(sys.executable, ["-c", "import time; time.sleep(60)"], timeout=0.5)
    assert info.value.timeout == 0.5
    assert "0.5" in str(info.value)


def test_a_process_within_its_time_limit_is_unaffected():
    assert run_process(sys.executable, ["-c", "print('ok')"], stdout=True, timeout=30) == "ok"


def test_a_captured_process_past_its_time_limit_is_stopped(tmp_path):
    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
        run_process(sys.executable, ["-c", "import time; time.sleep(60)"], stdout=True, timeout=0.5)
    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
        run_process(
            sys.executable,
            ["-c", "import time; time.sleep(60)"],
            stdout=tmp_path / "out.txt",
            timeout=0.5,
        )


def test_a_highlighted_process_past_its_time_limit_is_stopped():
    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
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


#: The two ways to ask `run_process` for a tool's log: copy the output to a file while showing it
#: (`tee`), or send it to a file alone (`stdout` given a path, `Tool.redirect_stdout`). One rule
#: holds for both.
LOG_ROUTES = pytest.mark.parametrize("route", ["tee", "stdout"])


@LOG_ROUTES
def test_a_tool_log_grows_while_the_process_runs(tmp_path, route):
    """`tail -F` on a log shows a long run as it goes: the log has its own name from the start."""
    log = tmp_path / "sim.log"
    go = tmp_path / "go"
    child = (
        "import pathlib, time\n"
        "print('first', flush=True)\n"
        f"go = pathlib.Path({str(go)!r})\n"
        "end = time.monotonic() + 30\n"
        "while not go.exists() and time.monotonic() < end:\n"
        "    time.sleep(0.05)\n"
        "print('last', flush=True)\n"
    )
    done = threading.Thread(
        target=run_process, args=(sys.executable, ["-c", child]), kwargs={route: log}
    )
    done.start()
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end and not (log.exists() and "first" in log.read_text()):
            time.sleep(0.05)
        seen = log.read_text() if log.exists() else None
    finally:
        go.write_text("")
        done.join(timeout=30)
    assert seen is not None and seen.splitlines() == ["first"]
    assert log.read_text().splitlines() == ["first", "last"]
    assert [p.name for p in tmp_path.iterdir() if p.name not in ("sim.log", "go")] == []


@LOG_ROUTES
def test_a_tool_that_is_stopped_leaves_the_log_it_had_written(tmp_path, route):
    """The partial log of a tool that ran out of time is its diagnostic."""
    log = tmp_path / "sim.log"
    child = "import time; print('so far', flush=True); time.sleep(60)"
    for limit in TIME_LIMITS:
        with pytest.raises(ProcessTimeout), _stopped_within(limit):
            run_process(sys.executable, ["-c", child], timeout=limit, **{route: log})
        if log.read_text():  # the tool wrote its line before the limit expired
            break
    assert log.read_text().splitlines() == ["so far"]


@LOG_ROUTES
@pytest.mark.skipif(os.name != "posix", reason="needs symbolic links")
@pytest.mark.parametrize("dangling", [False, True], ids=["to a file", "to nothing"])
def test_a_link_at_the_name_of_a_tool_log_is_never_followed(tmp_path, route, dangling):
    """A tool may leave a symbolic link at the log's name. The link is replaced as itself by the
    new log: the file it names is not written, and a name it leads to is not created."""
    run, outside = tmp_path / "run", tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    target = outside / "precious"
    if not dangling:
        target.write_text("keep\n")
    log = run / "sim.log"
    log.symlink_to(target)
    run_process(sys.executable, ["-c", "print('out')"], **{route: log})
    if dangling:
        assert not target.exists()
    else:
        assert target.read_text() == "keep\n"
    assert not log.is_symlink()
    assert log.read_text().splitlines() == ["out"]


@LOG_ROUTES
@pytest.mark.skipif(os.name != "posix", reason="needs hard links")
@pytest.mark.parametrize("fails", [False, True], ids=["passing tool", "failing tool"])
def test_a_hard_link_at_the_name_of_a_tool_log_is_never_written_through(tmp_path, route, fails):
    """A hard link shares its inode with a file that may lie outside the run directory, such as
    the user's own. The log is a new file at that name: writing to it, or to a failed tool's
    partial log, leaves the other file as it was."""
    run, outside = tmp_path / "run", tmp_path / "outside"
    run.mkdir()
    outside.mkdir()
    precious = outside / "precious"
    precious.write_text("keep\n")
    log = run / "sim.log"
    os.link(precious, log)
    assert precious.stat().st_nlink == 2
    code = "print('out'); raise SystemExit(3)" if fails else "print('out')"
    if fails:
        with pytest.raises(NonZeroExitCode):
            run_process(sys.executable, ["-c", code], **{route: log})
    else:
        run_process(sys.executable, ["-c", code], **{route: log})
    assert precious.read_text() == "keep\n"
    assert precious.stat().st_nlink == 1
    assert log.read_text().splitlines() == ["out"]
    assert not os.path.samestat(log.stat(), precious.stat())


@pytest.mark.skipif(os.name != "posix", reason="needs links")
@pytest.mark.parametrize("planted", ["symbolic link", "dangling symbolic link", "hard link"])
def test_a_link_made_while_the_log_is_created_is_replaced_not_followed(
    tmp_path, monkeypatch, planted
):
    """The log is a new file put at its name by one rename. Whatever appears at the name just
    before that rename is replaced as a name, and nothing xeda writes reaches what it led to."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "precious"
    if planted != "dangling symbolic link":
        target.write_text("keep\n")
    log = tmp_path / "sim.log"
    real_replace = os.replace
    made = []

    def replace_after_a_link_appears(source, destination, *args, **kwargs):
        if Path(destination) == log:
            if planted == "hard link":
                os.link(target, log)
            else:
                log.symlink_to(target)
            made.append(planted)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_after_a_link_appears)
    with live_log(log) as f:
        f.write("out\n")
    assert made == [planted]
    if planted == "dangling symbolic link":
        assert not target.exists()
    else:
        assert target.read_text() == "keep\n"
    assert not log.is_symlink()
    assert log.read_text() == "out\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["outside", "sim.log"]


@LOG_ROUTES
def test_a_second_run_replaces_the_log_of_the_first(tmp_path, route):
    log = tmp_path / "sim.log"
    run_process(sys.executable, ["-c", "print('first run'); print('more')"], **{route: log})
    run_process(sys.executable, ["-c", "print('second run')"], **{route: log})
    assert log.read_text().splitlines() == ["second run"]
    assert [p.name for p in tmp_path.iterdir()] == ["sim.log"]


@LOG_ROUTES
def test_a_tool_log_has_the_permissions_of_a_file_opened_for_writing(tmp_path, route):
    log = tmp_path / "sim.log"
    run_process(sys.executable, ["-c", "print('out')"], **{route: log})
    assert stat.S_IMODE(log.stat().st_mode) == xeda_utils._CREATE_MODE


@LOG_ROUTES
@pytest.mark.parametrize("obstacle", ["no directory", "a directory at the name"])
def test_a_tool_is_not_started_when_its_log_cannot_be_created(
    tmp_path, monkeypatch, route, obstacle
):
    """A log that cannot be created stops the run before the tool starts, and leaves nothing of
    its own behind."""
    started = []
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):  # type: ignore[type-arg, misc]
        def __init__(self, *args, **kwargs):
            started.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    if obstacle == "no directory":
        log = tmp_path / "missing" / "sim.log"
    else:
        log = tmp_path / "sim.log"
        log.mkdir()
    with pytest.raises(OSError):
        run_process(sys.executable, ["-c", "print('out')"], **{route: log})
    assert started == []
    assert sorted(p.name for p in tmp_path.iterdir()) == (
        [] if obstacle == "no directory" else ["sim.log"]
    )


# ---- a process tree to stop ----------------------------------------------------------------
#: Starts a program with SIGINT at its default. A process inherits a SIGINT that its starter
#: ignores, and a non-interactive shell starts every background job that way (`pytest &`). Without
#: this line, a tool would sleep through the interrupt that a test sends it.
DEFAULT_SIGINT = "import signal; signal.signal(signal.SIGINT, signal.SIG_DFL)\n"


def _tree(started: Path) -> str:
    """A program for a tool that starts a tool: a leader that runs one child and waits for it.
    The child writes its process id to `started` when it runs, and then sleeps. Both stop at
    SIGINT."""
    child = (
        f"{DEFAULT_SIGINT}"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    leader = f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {child!r}])\n"
    return DEFAULT_SIGINT + leader


def _inheriting_sigint(disposition: str, argv: list[str]) -> list[str]:
    """The command line that starts `argv` with SIGINT set to `disposition` ("SIG_DFL" or
    "SIG_IGN"), as a starter that set it before would. A non-interactive shell does the second for
    a background job."""
    return [
        sys.executable,
        "-c",
        "import os, signal, sys\n"
        "signal.signal(signal.SIGINT, getattr(signal, sys.argv[1]))\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n",
        disposition,
        *argv,
    ]


def _read_pid(path: Path) -> int | None:
    """The process id that a tree's child wrote to `path`, or None before it did."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    return int(text) if text.isdigit() else None


def _is_running(pid: int) -> bool:
    """Whether the process runs. One that ended and waits for its parent to reap it does not: the
    parent can be init, which may reap late, or never (a container without an init process)."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:  # the zombie was reaped meanwhile
        return False


def _kill(pid: int | None) -> None:
    """Clean up after a test that failed: stop the process if it still runs."""
    if pid is None:
        return
    try:
        psutil.Process(pid).kill()
    except psutil.NoSuchProcess:
        pass


def _wait_until(condition, what: str, deadline: float = 30.0):
    """Poll until `condition()` is true, and return what it returned. The deadline only ends a
    hang: a condition that is going to hold is seen within a few polls, however busy the machine."""
    end = time.monotonic() + deadline
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= end:
            pytest.fail(f"gave up waiting for {what} after {deadline} s")
        time.sleep(0.01)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX stops the whole process group; Windows only the process"
)
def test_the_time_limit_stops_the_process_tree(tmp_path):
    """A simulator started through a script: the child is stopped with it."""
    child = None
    try:
        for attempt, limit in enumerate(TIME_LIMITS):
            started = tmp_path / f"started-{attempt}"
            with pytest.raises(ProcessTimeout), _stopped_within(limit):
                run_process(sys.executable, ["-c", _tree(started)], timeout=limit)
            child = _read_pid(started)
            if child is not None:  # the child ran when the limit expired
                break
        assert child is not None, f"the child did not run within {limit} s"
        _wait_until(lambda: not _is_running(child), "the child to stop")
    except BaseException:
        _kill(child)  # a failed test leaves no sleeping process behind
        raise


# ---- the watchdog's lifecycle ---------------------------------------------------
import subprocess  # noqa: E402
import threading  # noqa: E402

import xeda.tool as xeda_tool  # noqa: E402
from xeda.proc_utils import _Deadline  # noqa: E402

pytestmark = pytest.mark.python_compat


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
        timer = deadline._timer
        assert timer is not None
        timer.join(30)  # the timer fires, finds the process ended, and returns
        assert not timer.is_alive()
    assert not deadline.expired
    assert not proc.killed


def test_a_cancelled_deadline_never_signals(monkeypatch):
    proc = _Finished()
    with _Deadline(proc, 0.2) as deadline:  # type: ignore[arg-type]
        deadline.cancel()
        timer = deadline._timer
        assert timer is not None
        assert not timer.is_alive()  # `cancel` joins the timer thread: nothing signals later
    assert not deadline.expired and not proc.killed


def test_a_process_that_exits_within_its_limit_is_never_a_timeout():
    for _ in range(5):
        run_process(sys.executable, ["-c", "pass"], timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("inherited", ["SIG_DFL", "SIG_IGN"], ids=["default", "ignored"])
def test_an_interrupt_stops_the_whole_process_tree(tmp_path, inherited):
    """An interrupt stops the leader and its child. Their starter leaves SIGINT at its default, or
    ignores it, as a test run started with `pytest &` does. Both programs set it to default."""
    started = tmp_path / "started"
    leader = [sys.executable, "-c", _tree(started)]
    proc = subprocess.Popen(_inheriting_sigint(inherited, leader), start_new_session=True)
    child = None
    try:
        child = _wait_until(lambda: _read_pid(started), "the child to run")
        with pytest.raises(KeyboardInterrupt):
            with _Deadline(proc, 60, group=True):
                raise KeyboardInterrupt
        # The leader died of the SIGINT. One that ignored it would live until the grace period
        # ended, and die of SIGKILL.
        assert proc.returncode == -signal.SIGINT
        _wait_until(lambda: not _is_running(child), "the child to stop")
    except BaseException:
        _kill(child)  # a failed test leaves no sleeping process behind
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_an_interrupt_while_copying_output_stops_the_process(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.print", interrupted)
    with pytest.raises(KeyboardInterrupt), _stopped_within():
        run_process(
            sys.executable,
            ["-c", f"{DEFAULT_SIGINT}import time; print('x', flush=True); time.sleep(60)"],
            highlight_rules={"x": ""},
            timeout=60,
        )


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_process_without_a_time_limit_stays_in_the_callers_group(tmp_path, monkeypatch):
    """Both output paths preserve the caller's process group when no timeout is set."""
    import xeda.proc_utils as pu

    groups = []
    real_popen = subprocess.Popen

    def started(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        groups.append(os.getpgid(proc.pid))
        return proc

    monkeypatch.setattr(pu.subprocess, "Popen", started)
    run_process(sys.executable, ["-c", "print('ok')"], stdout=True)
    run_process(sys.executable, ["-c", "print('ok')"], tee=tmp_path / "log")
    # A terminal's SIGINT/SIGHUP must reach the tool and its descendants.
    assert groups == [os.getpgrp(), os.getpgrp()]


@pytest.mark.parametrize("stop", ["timeout", "interrupt"])
def test_non_group_cleanup_needs_no_sigkill(monkeypatch, stop):
    """Windows has Popen.kill(), but no signal.SIGKILL; test both forced-stop paths."""
    from types import SimpleNamespace

    import xeda.proc_utils as pu

    class Running:
        args = ["stubborn"]
        pid = 12345

        def __init__(self):
            self.killed = False
            self.reaped = False

        def poll(self):
            return None

        def terminate(self):
            pass

        def send_signal(self, sig):
            pass

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.reaped = True
            return -1

    proc = Running()
    monkeypatch.setattr(pu, "signal", SimpleNamespace(SIGTERM=pu.signal.SIGTERM))
    deadline = _Deadline(proc, None)  # type: ignore[arg-type]
    if stop == "timeout":
        deadline._expire()
        deadline.__exit__(None, None, None)
        assert deadline.expired
    else:
        deadline.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert proc.killed and proc.reaped


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_interruption_signals_the_group_before_reaping_the_leader(monkeypatch):
    """The recycled-group hazard applies to interruption cleanup as well as a timeout."""
    script = f"{DEFAULT_SIGINT}import time; print('ready', flush=True); time.sleep(60)"
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, start_new_session=True
    )
    real_killpg = os.killpg
    signals = []

    def killpg(pid, sig):
        signals.append((sig, proc.returncode))
        real_killpg(pid, sig)

    try:
        assert proc.stdout.readline() == b"ready\n"
        monkeypatch.setattr(os, "killpg", killpg)
        with pytest.raises(KeyboardInterrupt):
            with _Deadline(proc, 60, group=True):
                raise KeyboardInterrupt
        assert signals and all(returncode is None for _, returncode in signals), signals
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_exception_cleanup_allows_a_graceful_exit(tmp_path, monkeypatch, error):
    """Forward the terminal's SIGINT on Ctrl-C, or SIGTERM on another exception, and let
    the tool flush its output before killing surviving group members."""
    import signal

    import xeda.proc_utils as pu

    # A bound, not a wait: the cleanup returns when the tool exits, which takes about 0.15 s
    monkeypatch.setattr(pu, "PROCESS_STOP_GRACE", 30.0)
    # CPython on macOS lacks os.waitid before 3.13: the grace period must not depend on it
    monkeypatch.delattr(os, "waitid", raising=False)
    marker = tmp_path / "graceful"
    script = (
        "import signal, time\n"
        "def stopped(sig, frame):\n"
        "    time.sleep(0.15)\n"
        f"    open({str(marker)!r}, 'w').write(str(sig))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, stopped)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        assert proc.stdout.readline() == b"ready\n"
        with pytest.raises(error):
            with _Deadline(proc, 60, group=True):
                raise error
        expected = signal.SIGINT if error is KeyboardInterrupt else signal.SIGTERM
        assert marker.read_text() == str(expected)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_exception_cleanup_kills_a_tool_after_the_grace_period(monkeypatch, error):
    """A tool ignoring the graceful signal still gets stopped after the bounded grace."""
    import signal

    import xeda.proc_utils as pu

    default, grace = pu.PROCESS_STOP_GRACE, 0.25
    monkeypatch.setattr(pu, "PROCESS_STOP_GRACE", grace)
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        assert proc.stdout.readline() == b"ready\n"
        started = time.monotonic()
        with pytest.raises(error):
            with _Deadline(proc, 60, group=True):
                raise error
        elapsed = time.monotonic() - started
        assert grace <= elapsed < default  # the cleanup waited for the shortened grace, no more
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


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


# ---- the stop hook --------------------------------------------------------------
_SLEEPER = ["-c", "import time; time.sleep(60)"]


def test_a_timeout_calls_the_stop_hook_once():
    calls = []
    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
        run_process(sys.executable, _SLEEPER, timeout=0.5, on_stop=lambda: calls.append(1))
    assert calls == [1]


def test_a_failing_stop_hook_does_not_replace_the_timeout():
    def broken():
        raise RuntimeError("docker is gone")

    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
        run_process(sys.executable, _SLEEPER, timeout=0.5, on_stop=broken)


def test_a_failing_stop_hook_does_not_replace_an_interrupt(monkeypatch):
    def broken():
        raise RuntimeError("docker is gone")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.print", interrupted)
    with pytest.raises(KeyboardInterrupt), _stopped_within():
        run_process(
            sys.executable,
            ["-c", "import time; print('x', flush=True); time.sleep(60)"],
            highlight_rules={"x": ""},
            on_stop=broken,
        )


def test_the_stop_hook_runs_on_the_calling_thread_after_a_timeout():
    seen = []
    with pytest.raises(ProcessTimeout), _stopped_within(0.5):
        run_process(
            sys.executable,
            _SLEEPER,
            timeout=0.5,
            on_stop=lambda: seen.append(threading.current_thread()),
        )
    assert seen == [threading.main_thread()]


def test_the_stop_hook_runs_on_the_calling_thread_after_an_interrupt(monkeypatch):
    seen = []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.print", interrupted)
    with pytest.raises(KeyboardInterrupt), _stopped_within():
        run_process(
            sys.executable,
            ["-c", "import time; print('x', flush=True); time.sleep(60)"],
            highlight_rules={"x": ""},
            on_stop=lambda: seen.append(threading.current_thread()),
        )
    assert seen == [threading.main_thread()]


def test_a_slow_stop_hook_never_delays_the_stopping_of_the_process():
    """The timer thread only kills: the hook runs afterwards, so the process is gone by then."""
    states = []
    holder = {}

    def hook():
        states.append(holder["proc"].poll())

    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        holder["proc"] = real_popen(*args, **kwargs)
        return holder["proc"]

    import xeda.proc_utils as pu

    pu.subprocess.Popen = recording_popen  # type: ignore[misc]
    try:
        with pytest.raises(ProcessTimeout), _stopped_within(0.3):
            run_process(sys.executable, _SLEEPER, timeout=0.3, on_stop=hook)
    finally:
        pu.subprocess.Popen = real_popen  # type: ignore[misc]
    assert states and states[0] is not None  # reaped before the hook ran


def test_the_docker_stop_hook_is_time_limited(monkeypatch, tmp_path):
    calls = []

    def fake_run_process(executable, args=None, **kwargs):
        calls.append((list(args or []), kwargs))
        if kwargs.get("on_stop"):
            kwargs["on_stop"]()
        return None

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    monkeypatch.chdir(tmp_path)
    tool = Tool(executable="some-tool", docker=Docker(image="img"), dockerized=True)
    tool.run("arg", timeout=3)
    kill_kwargs = calls[1][1]
    assert calls[1][0][0] == "kill"
    assert kill_kwargs["timeout"] == xeda_tool.DOCKER_KILL_TIMEOUT > 0
    assert "on_stop" not in kill_kwargs


def test_a_docker_stop_hook_that_times_out_is_only_logged(monkeypatch, tmp_path):
    def fake_run_process(executable, args=None, **kwargs):
        if kwargs.get("on_stop"):
            kwargs["on_stop"]()
            return None
        raise ProcessTimeout(["docker", "kill"], kwargs["timeout"])

    monkeypatch.setattr("xeda.tool.run_process", fake_run_process)
    monkeypatch.chdir(tmp_path)
    tool = Tool(executable="some-tool", docker=Docker(image="img"), dockerized=True)
    tool.run("arg", timeout=3)  # the hook's ProcessTimeout does not escape


@pytest.mark.parametrize("fails", [False, True])
def test_tee_captures_stderr_only_diagnostics(tmp_path, capsys, fails):
    transcript = tmp_path / "runtime.log"
    command = (
        "import sys; print('FINISH 5ns', file=sys.stderr); print('ERROR bad', file=sys.stderr); sys.exit("
        + str(int(fails))
        + ")"
    )
    if fails:
        with pytest.raises(NonZeroExitCode):
            run_process(sys.executable, ["-c", command], tee=transcript, merge_stderr=True)
    else:
        run_process(sys.executable, ["-c", command], tee=transcript, merge_stderr=True)
    assert transcript.read_text() == "FINISH 5ns\nERROR bad\n"
    assert "FINISH 5ns" in capsys.readouterr().out
