import codecs
import contextlib
import errno
import logging
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Optional, TextIO, Tuple, Union

import colorama
import psutil

from .utils import ExecutableNotFound, NonZeroExitCode, live_log

log = logging.getLogger(__name__)


#: The stream that tool output and echoed commands are written to. `None` means `sys.stdout`.
#: The machine-readable modes of the CLI (`--json`) point it at `sys.stderr`, so that a
#: parseable result can own stdout. Child processes that inherit stdout are redirected too.
_tool_output: Optional[TextIO] = None

#: The prefix under which a container image is recorded, beside the programs that `run_process`
#: starts.
DOCKER_IMAGE_PREFIX = "docker-image:"

#: Seconds allowed for a tool to exit gracefully after an interrupt or another exception.
PROCESS_STOP_GRACE = 5.0

#: A program file's state: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`.
ProgramState = Tuple[int, int, int, int, int]


class StartedPrograms(List[str]):
    """The programs started while `recording_programs` is active, in order and once each.
    `before` holds the state of the file of each program when it was first started. The trace
    compares the file with this state afterwards, by identity and metadata and never by a
    clock."""

    def __init__(self) -> None:
        super().__init__()
        self.before: Dict[str, Optional[ProgramState]] = {}


def program_state(name: str) -> Optional[ProgramState]:
    """Return the state of the file that `name` runs, or None. A name that is not a path is
    looked up on PATH."""
    found = name if os.path.isabs(name) or os.sep in name else shutil.which(name)
    try:
        st = os.stat(found) if found else None
    except OSError:
        return None
    if st is None:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


_programs: ContextVar[Optional[StartedPrograms]] = ContextVar("xeda_programs", default=None)


@contextmanager
def recording_programs() -> Iterator[StartedPrograms]:
    """Collect, in order and once each, every program started while the context is active."""
    names = StartedPrograms()
    token = _programs.set(names)
    try:
        yield names
    finally:
        _programs.reset(token)


def note_program(name: str) -> None:
    """Record `name` and the state of its file as it is now, before the program starts, if a
    recording is active."""
    names = _programs.get()
    if names is not None and name not in names:
        names.append(name)
        if not name.startswith(DOCKER_IMAGE_PREFIX):
            names.before[name] = program_state(name)


def set_tool_output(stream: Optional[TextIO]) -> None:
    """Route tool output away from stdout. `None` restores the default (`sys.stdout`)."""
    global _tool_output  # pylint: disable=global-statement
    _tool_output = stream


def tool_output_stream() -> TextIO:
    """Return the stream that tool output is written to."""
    return _tool_output if _tool_output is not None else sys.stdout


def tool_output_redirect() -> Optional[TextIO]:
    """Return the stream that a child process should inherit as its stdout, or `None` to leave
    stdout unchanged.

    Any code that spawns a subprocess outside `run_process` (design generators, the remote
    runner) must honor this. Otherwise the output lands on fd 1 and corrupts a `--json`
    document.
    """
    return _tool_output


def _stream_has_fileno(stream: Optional[TextIO]) -> bool:
    """Return whether `stream` can be handed to `Popen(stdout=...)` as an inheritable file
    descriptor.

    The result is False for anything that exists only at the Python level, such as the captured
    streams of `click.testing.CliRunner`, an `io.StringIO`, or the redirected stdio of an
    embedding program. Their `.fileno()` raises `io.UnsupportedOperation` (itself both an
    `OSError` and a `ValueError`), or they lack the method (`AttributeError`). `subprocess.Popen`
    calls `.fileno()` on whatever it is given with no such guard, so handing it one of these
    raises deep inside `subprocess` and not here.
    """
    if stream is None:
        return False
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True


class ProcessTimeout(NonZeroExitCode):
    """A process ran past its time limit and was stopped."""

    def __init__(self, command_args: Any, timeout: float) -> None:
        super().__init__(command_args, -1)
        self.timeout = timeout

    def __str__(self) -> str:
        what = self.command_args.split(" ")[0] if self.command_args else "process"
        return f"{what} ran past its time limit of {self.timeout} s and was stopped"


class _Deadline:
    """A watch on `proc`. Once `timeout` seconds pass (`None`: never) while the process still
    runs, it stops the process and, when the process leads a session of its own (`group`,
    POSIX), every process that it started.

    Use it as a context manager around everything that waits for `proc`. On leaving, it cancels
    the timer and joins its thread, so nothing signals afterwards. An exception (Ctrl-C
    included) stops the process and reaps it, because the process is a child of the caller. With
    `group`, Ctrl-C forwards SIGINT, other exceptions send SIGTERM, and the leader gets
    `PROCESS_STOP_GRACE` seconds to exit without being reaped. The group then gets SIGKILL
    before the leader is reaped, so the group id cannot be recycled (the other members of the
    group are not children of the caller, so they cannot be reaped here). `on_stop` then runs,
    on the thread that leaves the context, as `run_process` documents.
    """

    def __init__(
        self,
        proc: "subprocess.Popen[Any]",
        timeout: float | None,
        *,
        group: bool = False,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.proc, self.expired = proc, False
        self._group = group and os.name == "posix"
        self._on_stop = on_stop
        self._lock = threading.Lock()
        self._cancelled = False
        self._timer = threading.Timer(timeout, self._expire) if timeout is not None else None
        if self._timer is not None:
            self._timer.daemon = True
            self._timer.start()

    def _stop(self, *, force: bool = False, interrupt: bool = False) -> None:
        try:
            if self._group:
                sig = signal.SIGKILL if force else signal.SIGINT if interrupt else signal.SIGTERM
                os.killpg(self.proc.pid, sig)
            elif force:
                self.proc.kill()
            else:
                self.proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    def _stopped(self) -> None:
        """Run `on_stop` once, in the calling thread, after the process was reaped. Nothing
        bounds it here, because it bounds itself. A `KeyboardInterrupt` or `SystemExit` from it
        propagates. Any other `Exception` is logged and never replaces the exception in
        flight."""
        if self._on_stop is None:
            return
        try:
            self._on_stop()
        except Exception as e:  # stopping is best effort
            log.warning("Stopping %s failed: %s", self.proc.args, e)

    def _expire(self) -> None:
        with self._lock:
            # A process that ended by itself is not a timeout. While `poll` returns `None`, the
            # process is not yet reaped, so its pid (and group) cannot be recycled.
            if self._cancelled or self.proc.poll() is not None:
                return
            self.expired = True
            self._stop(force=True)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        if self._timer is not None:
            self._timer.cancel()
            if self._timer is not threading.current_thread():
                self._timer.join()

    def __enter__(self) -> "_Deadline":
        return self

    def _exited_unreaped(self) -> bool:
        """Return whether the leader has exited but is still waiting to be reaped (a zombie).
        The answer comes without `wait`, `poll` or `waitpid`, which would release its pid and
        group id."""
        try:
            return psutil.Process(self.proc.pid).status() == psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return True

    def _wait_unreaped(self) -> None:
        """Give the POSIX leader its grace period without releasing its pid or group id."""
        end = time.monotonic() + PROCESS_STOP_GRACE
        while not self._exited_unreaped():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cancel()
        was_running = exc_type is not None and self.proc.poll() is None
        if was_running:
            log.debug("Stopping %s(pid=%s)", self.proc.args, self.proc.pid)
            self._stop(interrupt=issubclass(exc_type, KeyboardInterrupt))
            if self._group:
                try:
                    self._wait_unreaped()
                finally:
                    # The leader is still a child of this process, running or a zombie. Signal
                    # its descendants before `wait()` releases its pid. A second Ctrl-C still
                    # kills and reaps it.
                    self._stop(force=True)
                    self.proc.wait()
            else:
                try:
                    self.proc.wait(PROCESS_STOP_GRACE)
                except subprocess.TimeoutExpired:
                    self._stop(force=True)
        if exc_type is not None or self.expired:
            self.proc.wait()
        if was_running or self.expired:
            self._stopped()


def _needs_line_copy(
    stdout: Union[None, bool, str, os.PathLike],
    highlight_rules: Optional[Dict[str, str]],
    redirect: Optional[TextIO],
    tee: Path | None = None,
) -> bool:
    """Return whether the stdout of a child must be piped and copied to `tool_output_stream()`
    line by line in Python, and not handed to `Popen` directly.

    This applies only with no capture requested (`stdout is None`). Then the lines are copied
    when a `tee` log is wanted, when highlighting wants to inspect each line, or when `redirect`
    (tool output has been redirected, for example under `--json`) is a stream that cannot be
    passed to `Popen` as a file descriptor (`_stream_has_fileno`). Handing such a stream to
    `Popen` raises `io.UnsupportedOperation` deep inside `subprocess`.
    """
    if stdout is not None:
        return False
    return (
        tee is not None
        or bool(highlight_rules)
        or (redirect is not None and not _stream_has_fileno(redirect))
    )


def _stdout_terminal_fd() -> Optional[int]:
    """Return the file descriptor of the terminal that tool output is written to directly, or
    None if that stream is not a terminal.

    The function does not fall back to stdin or the controlling terminal. When the output goes
    through a pipe, some other program decides how it reaches a terminal. A wrapper that reads
    line by line (a shell script, `tee`, `ProcessOutput.Readlines` of Scala, a Python loop over
    `proc.stdout`) strips the line endings and emits its own. A carriage return added here would
    then never reach the terminal anyway. Worse, a lone '\\r' counts as a line terminator to
    those readers, so it would show up as a blank line after every line of output.
    """
    out = tool_output_stream()
    try:
        if out.isatty():
            return out.fileno()
    except (AttributeError, ValueError, OSError):
        pass
    return None


def _needs_explicit_carriage_return(terminal_fd: Optional[int]) -> bool:
    """Return True when the terminal will not turn a '\\n' into CR+LF by itself.

    A tool that takes over the terminal switches it into raw mode while it runs. This happens
    with anything run through `docker -t -i`, which is how Vivado is commonly wrapped. A bare
    '\\n' then moves the cursor down a line without returning the carriage, so the output walks
    diagonally down the screen unless each line is followed by an explicit carriage return.
    """
    if terminal_fd is None:
        return False
    try:
        oflag = termios.tcgetattr(terminal_fd)[1]
    except (termios.error, OSError, ValueError):
        return False
    # The terminal expands NL to CR+LF only when output post-processing is on and ONLCR is
    # set. Raw mode typically clears OPOST and leaves the ONLCR bit itself untouched, so both
    # have to be checked.
    return not (oflag & termios.OPOST and oflag & termios.ONLCR)


#: The escape sequences of a terminal (colors, moving the cursor): the text of a log has none.
_ESCAPE_SEQUENCES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: What ends a line of a terminal's output. A carriage return alone redraws the line.
_LINE_ENDS = re.compile(r"\r\n|\r|\n")


def _write_log_lines(log_file: IO[str], segments: Sequence[str]) -> None:
    """Write lines of the output of a terminal to a log, without the escape sequences and
    without the empty lines that a line end after a carriage return leaves."""
    for segment in segments:
        text = _ESCAPE_SEQUENCES.sub("", segment)
        if text.strip():
            log_file.write(text + "\n")


def _copy_from_terminal(master: int, log_file: IO[str], stream: TextIO) -> None:
    """Copy what a child writes to its pseudo-terminal, until the child closes it. `stream` (the
    terminal of Xeda) gets the text as the child wrote it, when it arrives: the colors work, and
    so does a line that the child redraws with a carriage return. The log gets the lines, one
    for each redraw, without the escape sequences."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""  # the last line, which has not ended yet
    while True:
        try:
            data = os.read(master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:  # Linux ends the output so; macOS reads nothing
                raise
            data = b""
        text = decoder.decode(data, final=not data)
        if text:
            # Write the log first, as for the lines of a pipe: it keeps what a failing terminal
            # missed.
            *ended, pending = _LINE_ENDS.split(pending + text)
            _write_log_lines(log_file, ended)
            stream.write(text)
            stream.flush()
        if not data:
            break
    _write_log_lines(log_file, [pending])


def _run_in_terminal(
    command: List[str],
    env: Optional[Dict[str, Any]],
    cwd: Union[None, str, os.PathLike],
    check: bool,
    merge_stderr: bool,
    timeout: float | None,
    tee: Path,
    on_stop: Callable[[], None] | None,
    new_session: bool,
) -> None:
    """Run a child as `run_process` does, with a pseudo-terminal in place of the pipe. Stdout and,
    with `merge_stderr`, stderr are the terminal. The time limit, the stop hook, the log and the
    exit status are those of the pipe."""
    master, slave = os.openpty()
    try:
        # The log is made before the tool starts: a log that cannot be made must not start it.
        with (
            live_log(tee, encoding="utf-8") as log_file,
            subprocess.Popen(
                command,
                stdout=slave,
                stderr=slave if merge_stderr else None,
                env=env,
                cwd=cwd,
                start_new_session=new_session,
            ) as proc,
        ):
            os.close(slave)  # the child's copy is the only one left: its end is the EOF
            slave = -1
            with _Deadline(proc, timeout, group=new_session, on_stop=on_stop) as deadline:
                _copy_from_terminal(master, log_file, tool_output_stream())
                ret = proc.wait()
            if deadline.expired:
                assert timeout is not None
                raise ProcessTimeout(command, timeout)
            if check and ret != 0:
                raise NonZeroExitCode(command, ret)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)


def proc_output(is_stderr: bool, line):
    print(
        f"{'[E] ' if is_stderr else ''}{line}",
        end="",
        file=sys.stderr if is_stderr else tool_output_stream(),
    )


def run_process(
    executable: str,
    args: Optional[Sequence[Any]] = None,
    env: Optional[Dict[str, Any]] = None,
    stdout: Union[None, bool, str, os.PathLike] = None,
    check: bool = True,
    cwd: Union[None, str, os.PathLike] = None,
    print_command: bool = False,
    highlight_rules: Optional[Dict[str, str]] = None,
    merge_stderr: bool = False,
    timeout: float | None = None,
    tee: Path | None = None,
    on_stop: Callable[[], None] | None = None,
    terminal: bool = False,
) -> Union[None, str]:
    """Run `executable`, and return its captured stdout when `stdout` is True.

    `timeout` stops the process after this many seconds and raises `ProcessTimeout`. On POSIX it
    also stops the processes that the child started, which share its process group. On other
    systems it stops only the child. `tee` also writes every line of the output to this file. The output is
    then not captured, so `tee` with a `stdout` other than `None` is a `ValueError`. A `stdout`
    that is a path sends the output to that file alone. Either file is the log of a tool. Xeda
    makes it as a new file at the given name before the tool starts and writes it as the output
    arrives (`utils.live_log`). The new file replaces whatever was there, a link or a hard link
    included, and Xeda never writes through that link. A log that cannot be made starts no tool.

    `on_stop` stops what killing the process does not reach (a container: `Docker.run`). It is
    called at most once, and only when Xeda stopped the process: its time limit expired, or an
    exception (Ctrl-C included) was raised while the process still ran. It is never called for a
    process that ended by itself. It runs synchronously, on the thread that called
    `run_process`, after the process was stopped and reaped and, when the process leads a
    process group of its own (POSIX, with a `timeout`), after that group was sent SIGKILL. A
    `KeyboardInterrupt` (a second Ctrl-C) or `SystemExit` that it raises propagates. Any other
    `Exception` is logged and never replaces the exception in flight, nor the `ProcessTimeout`.
    Nothing bounds its run time from outside, and the caller waits for it. A hook that can block
    must carry its own time limit, such as a `run_process(..., timeout=...)` of its own (the
    `docker kill` of `Docker.run` is bounded by `tool.DOCKER_KILL_TIMEOUT`).

    `merge_stderr` folds the stderr of the child into stdout while capturing (`stdout=True`) or
    copying lines (`tee` or highlighting), so stderr diagnostics also reach the transcript.

    `terminal` says that the child asks for a terminal. When the output is copied to a `tee` log
    and Xeda writes to a terminal, the child gets a pseudo-terminal in place of the pipe, for its
    stdout and, with `merge_stderr`, its stderr. It then behaves as it does when a person runs
    it: it writes colors and redraws a progress bar in place. `tool_output_stream()` gets what it
    writes as it writes it. The log gets the same text without the escape sequences, a line for
    each redraw and no empty line. In any other case (a pipe, a file, a CI log, a captured
    stream) `terminal` has no effect, and the child sees the pipe that it always saw.
    `highlight_rules` do not apply to a child with a terminal, which colors its own output.
    """
    if timeout is not None and not timeout > 0:
        raise ValueError(f"timeout must be a positive number of seconds, not {timeout!r}")
    if tee is not None and stdout is not None:
        raise ValueError("`tee` copies the output while showing it: not with `stdout` given")
    note_program(str(executable))
    if args is None:
        args = []
    args = [str(a) for a in args]
    if env is not None:
        env = {k: str(v) for k, v in env.items() if v is not None}
    command: List[str] = [str(c) for c in (executable, *args)]
    cmd_str = " ".join(map(lambda x: str(x), command))
    if print_command:
        print("Running `%s`" % cmd_str, file=tool_output_stream())
    else:
        log.debug("Running `%s`", cmd_str)
    if cwd:
        log.debug("cwd=%s", cwd)
    new_session = timeout is not None and os.name == "posix"
    if terminal and stdout is None and tee is not None and _stdout_terminal_fd() is not None:
        _run_in_terminal(command, env, cwd, check, merge_stderr, timeout, tee, on_stop, new_session)
        return None
    if _needs_line_copy(stdout, highlight_rules, tool_output_redirect(), tee):
        # Compile the regex keys once, to improve performance.
        highlight_rules_re: Dict[re.Pattern, str] = {}
        for pattern, subs in (highlight_rules or {}).items():
            highlight_rules_re[re.compile(pattern)] = subs

        # The log is made before the tool starts: a log that cannot be made must not start it.
        with (
            (
                live_log(tee, encoding="utf-8") if tee is not None else contextlib.nullcontext()
            ) as tee_file,
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else None,
                env=env,
                cwd=cwd,
                universal_newlines=True,
                bufsize=1,
                start_new_session=new_session,
            ) as proc,
        ):
            assert proc.stdout is not None, f"Popen for '{cmd_str}' failed: stdout is None!"
            terminal_fd = _stdout_terminal_fd()
            with _Deadline(proc, timeout, group=new_session, on_stop=on_stop) as deadline:
                with open(proc.stdout.fileno(), errors="ignore", closefd=False) as proc_stdout:
                    for line in proc_stdout:
                        if tee_file is not None:
                            tee_file.write(line)
                        for re_pat, subs in highlight_rules_re.items():
                            line, matches = re_pat.subn(
                                subs + colorama.Style.RESET_ALL, line, count=1
                            )
                            if matches > 0:
                                break
                        # Check on every line, not once up front. A tool can switch the
                        # terminal into raw mode while it runs and restore it on exit, so
                        # the answer can change during the call. The check costs well
                        # under a microsecond.
                        print(
                            line,
                            end="\r" if _needs_explicit_carriage_return(terminal_fd) else "",
                            file=tool_output_stream(),
                        )
                ret = proc.wait()
            if deadline.expired:
                assert timeout is not None
                raise ProcessTimeout(command, timeout)
            if check and ret != 0:
                raise NonZeroExitCode(command, ret)
            return None
    elif stdout and isinstance(stdout, (str, os.PathLike)):
        stdout = Path(stdout)

        def cm_call():
            assert stdout
            # The log of the tool, as with `tee`: a new file at the name, made before the tool
            # starts and written as it runs. The output of a failed tool stays, since it says
            # why the tool failed.
            return live_log(stdout, encoding="utf-8")

        cm = cm_call
    else:
        cm = contextlib.nullcontext

    out = err = ""
    with cm() as f:
        with subprocess.Popen(
            [executable, *args],
            cwd=cwd,
            shell=False,
            # With no capture requested, the child inherits stdout, unless tool output has been
            # redirected. Without the redirect it would write straight to fd 1 and corrupt a
            # machine-readable result.
            stdout=f if f else subprocess.PIPE if stdout else tool_output_redirect(),
            stderr=subprocess.STDOUT if (merge_stderr and stdout and not f) else None,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=new_session,
        ) as proc:
            log.debug("Started %s[%d]", executable, proc.pid)
            with _Deadline(proc, timeout, group=new_session, on_stop=on_stop) as deadline:
                if stdout:
                    if isinstance(stdout, bool):
                        out, err = proc.communicate(timeout=None)
                    else:
                        log.info(
                            "Standard output is redirected to: %s",
                            os.path.abspath(stdout),
                        )
                        proc.wait()
                else:
                    proc.wait()
        if deadline.expired:
            assert timeout is not None
            raise ProcessTimeout(command, timeout)
        if check and proc.returncode != 0:
            raise NonZeroExitCode(proc.args, proc.returncode)
        if stdout and isinstance(stdout, bool):
            if err:
                print(err, file=sys.stderr)
            return out.strip()

    return None


def _terminate_process(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        process.wait(10)
    if process.poll() is None:
        process.terminate()
        process.wait(100)
        process.kill()


def _subprocess_tty(command, env, cwd, check):
    """Run `command` with `subprocess.Popen` on pseudo-terminals, so that it line-buffers its
    output. Yield `(is_stderr, data)` for each chunk of bytes (at most 64) that it writes."""
    timeout = None
    mo, so = pty.openpty()  # provide tty to enable line-buffering
    me, se = pty.openpty()
    readable = [mo, me]
    data = None
    try:
        process = subprocess.Popen(
            command, stdout=so, stderr=se, bufsize=1, close_fds=True, env=env, cwd=cwd
        )
    except FileNotFoundError:
        path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH")
        raise ExecutableNotFound(command[0], path=path)
    for fd in [so, se]:
        os.close(fd)
    try:
        while readable:
            ready, _, _ = select.select(readable, [], [], timeout)
            for fd in ready:
                try:
                    data = os.read(fd, 64)
                except OSError as e:
                    # EIO means end of file on some systems
                    if e.errno != errno.EIO:
                        raise
                    data = None
                if data:
                    yield (fd == me, data)
                else:
                    readable.remove(fd)
    except KeyboardInterrupt:
        _terminate_process(process)
        raise
    finally:
        _terminate_process(process)
        for fd in [mo, me]:
            os.close(fd)
    if check and process.returncode != 0:
        raise NonZeroExitCode(process.args, process.returncode)


def run_capture_pty(command, env=None, cwd=None, check=True, encoding="utf-8"):
    remainder = ""
    err_remainder = ""
    for is_stderr, data in _subprocess_tty(command, env, cwd, check=check):
        if not data:
            break
        data_str = data.decode(encoding)
        if is_stderr:
            if err_remainder:
                data_str = err_remainder + data_str
                err_remainder = ""
        elif remainder:
            data_str = remainder + data_str
            remainder = ""
        spl = data_str.splitlines(keepends=True)
        if spl and not spl[-1].endswith(os.linesep) and not spl[-1].endswith("\n"):
            if is_stderr:
                err_remainder = spl[0]
            else:
                remainder = spl[0]
            spl = spl[1:]
        for line in spl:
            yield (is_stderr, line + os.linesep)
    if remainder:
        yield (False, remainder + os.linesep)
    if err_remainder:
        yield (True, err_remainder + os.linesep)
    return None
