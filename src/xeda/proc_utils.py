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
from typing import Any, Dict, Iterator, List, Optional, TextIO, Tuple, Union

import colorama
import psutil

from .utils import ExecutableNotFound, NonZeroExitCode, live_log, replacing_file

log = logging.getLogger(__name__)


#: Stream that tool output and echoed commands are written to. `None` means `sys.stdout`.
#: The CLI's machine-readable modes (`--json`) point this at `sys.stderr` so that a parseable
#: result can own stdout; child processes that inherit our stdout are redirected too.
_tool_output: Optional[TextIO] = None

#: A container image is recorded under this prefix, beside the programs `run_process` starts.
DOCKER_IMAGE_PREFIX = "docker-image:"

#: Seconds allowed for a tool to exit gracefully after an interrupt or another exception.
PROCESS_STOP_GRACE = 5.0

#: A program file's state: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`.
ProgramState = Tuple[int, int, int, int, int]


class StartedPrograms(List[str]):
    """The programs started while `recording_programs` is active, in order and once each, and
    (`before`) the state of each one's file when it was first started: what its trace compares
    the file with afterwards, by identity and metadata, never a clock."""

    def __init__(self) -> None:
        super().__init__()
        self.before: Dict[str, Optional[ProgramState]] = {}


def program_state(name: str) -> Optional[ProgramState]:
    """The state of the file `name` runs -- found on PATH unless it is a path -- or None."""
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
    """Record `name`, and the state of its file as it is now, before it starts, if a recording
    is active."""
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
    """The stream tool output should be written to."""
    return _tool_output if _tool_output is not None else sys.stdout


def tool_output_redirect() -> Optional[TextIO]:
    """Stream a child process should inherit as its stdout, or `None` to leave ours unchanged.

    Any code that spawns a subprocess outside `run_process` (design generators, the remote
    runner) must honor this, or its output lands on fd 1 and corrupts a `--json` document.
    """
    return _tool_output


def _stream_has_fileno(stream: Optional[TextIO]) -> bool:
    """Whether `stream` can be handed to `Popen(stdout=...)` as an inheritable file descriptor.

    False for anything that only exists at the Python level -- `click.testing.CliRunner`'s
    captured streams, an `io.StringIO`, an embedding program's own redirected stdio -- which
    raise `io.UnsupportedOperation` (itself both an `OSError` and a `ValueError`) from
    `.fileno()`, or lack the method (`AttributeError`) entirely. `subprocess.Popen` calls
    `.fileno()` on whatever it is given with no such guard, so handing it one of these raises
    deep inside `subprocess` instead of here.
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
    """Watches `proc`: once `timeout` seconds pass (`None`: never) while it still runs, stops it
    and, when it leads a session of its own (`group`, POSIX), every process it started.

    Used as a context manager around everything that waits for `proc`: on leaving it, the timer
    is cancelled and its thread joined, so nothing signals afterwards; an exception (Ctrl-C
    included) stops the process and reaps it -- it is our child. With `group`, Ctrl-C forwards
    SIGINT, other exceptions send SIGTERM, and the leader gets `PROCESS_STOP_GRACE` seconds to
    exit without being reaped. The group is then sent SIGKILL before reaping the leader, so its
    group id cannot be recycled (the group's other members are not our children, so they cannot
    be reaped here). `on_stop` then runs, on the thread leaving the context, as `run_process`
    documents.
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
        """Run `on_stop`, once, in the calling thread, after the process was reaped. Nothing
        bounds it here: it bounds itself. A `KeyboardInterrupt` or `SystemExit` from it
        propagates; any other `Exception` is logged and never replaces the exception in
        flight."""
        if self._on_stop is None:
            return
        try:
            self._on_stop()
        except Exception as e:  # stopping is best effort
            log.warning("Stopping %s failed: %s", self.proc.args, e)

    def _expire(self) -> None:
        with self._lock:
            # a process that ended by itself is not a timeout; `poll` returning `None` means the
            # pid (and group) is not yet reaped, hence not recycled
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
        """Whether the leader has exited but is still ours to reap (a zombie), read without
        `wait`/`poll`/`waitpid`, which would release its pid and group id."""
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
                    # The leader is still ours, running or a zombie: signal descendants before
                    # wait() releases its pid. A second Ctrl-C still kills and reaps it.
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
    """Whether a child's stdout must be piped and copied to `tool_output_stream()` line by line
    in Python, rather than handed to `Popen` directly.

    Only with no capture requested (`stdout is None`): then either highlighting wants to inspect
    each line anyway, or `redirect` (tool output has been redirected, e.g. under `--json`) is a
    stream that cannot be passed to `Popen` as a file descriptor at all (`_stream_has_fileno`) --
    handing it one anyway raises `io.UnsupportedOperation` deep inside `subprocess` instead.
    """
    if stdout is not None:
        return False
    return (
        tee is not None
        or bool(highlight_rules)
        or (redirect is not None and not _stream_has_fileno(redirect))
    )


def _stdout_terminal_fd() -> Optional[int]:
    """The terminal we write to *directly*, or None if stdout is not one.

    Deliberately does not fall back to stdin or the controlling terminal. When
    our output goes through a pipe, some other program decides how it reaches a
    terminal -- and a wrapper that reads us line by line (a shell script, `tee`,
    Scala's `ProcessOutput.Readlines`, `for line in proc.stdout`) strips our
    line endings and re-emits its own. A carriage return we added would then
    never reach the terminal anyway, and worse, a lone '\\r' counts as a line
    terminator to those readers, so it would show up as a blank line after
    every line of output.
    """
    out = tool_output_stream()
    try:
        if out.isatty():
            return out.fileno()
    except (AttributeError, ValueError, OSError):
        pass
    return None


def _needs_explicit_carriage_return(terminal_fd: Optional[int]) -> bool:
    """True when the terminal will not turn a '\\n' into CR+LF by itself.

    A tool that takes over the terminal -- notably anything run through
    `docker -t -i`, which is how vivado is commonly wrapped -- switches it into
    raw mode while it runs. A bare '\\n' then moves the cursor down a line
    without returning the carriage, so output walks diagonally down the screen
    unless each line is followed by an explicit carriage return.
    """
    if terminal_fd is None:
        return False
    try:
        oflag = termios.tcgetattr(terminal_fd)[1]
    except (termios.error, OSError, ValueError):
        return False
    # The terminal only expands NL to CR+LF when output post-processing is on
    # *and* ONLCR is set; raw mode typically clears OPOST and leaves the ONLCR
    # bit itself untouched, so both have to be checked.
    return not (oflag & termios.OPOST and oflag & termios.ONLCR)


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
) -> Union[None, str]:
    """Run `executable`; return its captured stdout when `stdout` is True.

    `timeout`: stop the process, and those it started, after this many seconds, raising
    `ProcessTimeout`. `tee`: also write every line of the output to this file, as the
    line arrives (`utils.live_log`: a new file at the given name, which replaces whatever was
    there, a link or a hard link included, and is never written through it); the output is then
    not captured, so `tee` with a `stdout` other than `None` is a `ValueError`.

    `on_stop` stops what killing the process does not reach (a container: `Docker.run`). It is
    called at most once, and only when xeda stopped the process: its time limit expired, or an
    exception (Ctrl-C included) was raised while it still ran -- never for a process that ended
    by itself. It runs synchronously, on the thread that called `run_process`, after the
    process was stopped and reaped and, when it leads a process group of its own (POSIX, with a
    `timeout`), that group was sent SIGKILL. A `KeyboardInterrupt` (a second Ctrl-C) or
    `SystemExit` it raises propagates; any other `Exception` is logged and never replaces the
    exception in flight, nor the `ProcessTimeout`. Nothing bounds its run time from outside,
    and the caller waits for it: a hook that can block must carry its own time limit, such as a
    `run_process(..., timeout=...)` of its own (`Docker.run`'s `docker kill` is bounded by
    `tool.DOCKER_KILL_TIMEOUT`).

    `merge_stderr` folds the child's stderr into stdout while capturing (`stdout=True`)
    or copying lines (tee/highlighting), so stderr diagnostics also reach the transcript.
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
    if _needs_line_copy(stdout, highlight_rules, tool_output_redirect(), tee):
        # compile regex str keys to improve performance
        highlight_rules_re: Dict[re.Pattern, str] = {}
        for pattern, subs in (highlight_rules or {}).items():
            highlight_rules_re[re.compile(pattern)] = subs

        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else None,
            env=env,
            cwd=cwd,
            universal_newlines=True,
            bufsize=1,
            start_new_session=new_session,
        ) as proc:
            assert proc.stdout is not None, f"Popen for '{cmd_str}' failed: stdout is None!"
            terminal_fd = _stdout_terminal_fd()
            with _Deadline(proc, timeout, group=new_session, on_stop=on_stop) as deadline:
                with contextlib.ExitStack() as stack:
                    tee_file = (
                        stack.enter_context(live_log(tee, encoding="utf-8"))
                        if tee is not None
                        else None
                    )
                    proc_stdout = stack.enter_context(
                        open(proc.stdout.fileno(), errors="ignore", closefd=False)
                    )
                    for line in proc_stdout:
                        if tee_file is not None:
                            tee_file.write(line)
                        for re_pat, subs in highlight_rules_re.items():
                            line, matches = re_pat.subn(
                                subs + colorama.Style.RESET_ALL, line, count=1
                            )
                            if matches > 0:
                                break
                        # Re-checked per line rather than once up front: a tool can
                        # switch the terminal into raw mode while it runs and restore
                        # it on exit, so this is not fixed for the duration of the
                        # call. The check costs well under a microsecond.
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
            # a link at the name is replaced by the file, not written through; a failed tool's
            # output is kept, since it says why it failed
            return replacing_file(stdout, encoding="utf-8", keep_on_error=True)

        cm = cm_call
    else:
        cm = contextlib.nullcontext

    out = err = ""
    with cm() as f:
        with subprocess.Popen(
            [executable, *args],
            cwd=cwd,
            shell=False,
            # With no capture requested the child inherits our stdout, unless tool output has
            # been redirected -- otherwise it would write straight to fd 1 and corrupt a
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
    """`subprocess.Popen` yielding stdout lines acting as a TTY"""
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
                    # EIO means EOF on some systems
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
        # spl = re.split(r"\r?\n", data_str) #
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
