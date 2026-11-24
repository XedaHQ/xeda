"""The OpenSSH transport of a remote run.

Xeda uses the system `ssh` program only. One launch opens one private master connection, in the
user's terminal, so that OpenSSH can ask for a password, a passphrase or a second factor once.
Every other `ssh` process of the launch uses that connection through its control socket and
never asks. Xeda sets no host key option: OpenSSH checks the host keys as the user configured.
"""

import contextvars
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Iterator, Optional, Sequence

from ..cancel import launch_canceled
from ..proc_utils import CANCEL_POLL, child_stdout
from ..utils import Canceled
from .constants import CONTROL_PERSIST, SERVER_ALIVE
from .errors import RemoteConfigError, RemoteConnectionError

log = logging.getLogger(__name__)

#: A Unix socket path is at most 103 bytes on macOS (104 with the final NUL byte). While it binds
#: the socket, OpenSSH adds `.` and 16 random characters to the control path.
_MAX_CONTROL_PATH = 103 - 17

#: The directory for the control socket when the temporary directory has a path that is too long.
_FALLBACK_DIRECTORY = "/tmp"

#: Seconds that `ssh -O check` and `ssh -O exit` may take, and that `ssh -G` may take.
_QUICK = 15

#: A word of a command that `sh_command` writes without quotes.
_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9._/:=@+-]+")
#: Characters that a double-quoted shell word would still expand or end.
_UNSAFE_IN_PATH = re.compile(r'["$`\\!\n]')


@dataclass(frozen=True)
class Destination:
    """The remote host as OpenSSH resolves it: `text` is what the user gave."""

    text: str
    user: str
    hostname: str
    port: int

    def label(self) -> str:
        """A name of the host that is safe in a file name."""
        return re.sub(r"[^A-Za-z0-9._@-]", "_", self.text)


def _ssh_program() -> str:
    program = shutil.which("ssh")
    if program is None:
        raise RemoteConnectionError(
            "the OpenSSH client `ssh` was not found on PATH; remote runs need it"
        )
    return program


def _check_text(text: str) -> None:
    if not text:
        raise RemoteConfigError("the remote host is empty")
    if text.startswith("-"):
        raise RemoteConfigError(
            f"the remote host {text!r} starts with '-', so ssh reads it as an option"
        )
    if any(c.isspace() or not c.isprintable() for c in text):
        raise RemoteConfigError(f"the remote host {text!r} has a space or a control character")


def _check_options(ssh_options: Sequence[str]) -> tuple[str, ...]:
    """Accept only pairs `-F <path>` (a configuration file of `ssh`). The options exist for the
    tests and for the end-to-end tests that use a real `sshd`; xeda has no SSH options of its
    own, and the user's `~/.ssh/config` holds the rest."""
    options = tuple(ssh_options)
    if len(options) % 2 or any(
        option != "-F" or not path for option, path in zip(options[::2], options[1::2])
    ):
        raise ValueError(
            f"the only ssh options that xeda accepts are '-F <path>' pairs: {options!r}"
        )
    return options


def resolve_destination(text: str, ssh_options: Sequence[str] = ()) -> Destination:
    """Ask `ssh -G` for the user, host name and port that OpenSSH uses for `text`.

    `text` is any OpenSSH destination: an alias of `~/.ssh/config`, `user@host` or
    `ssh://user@host:port`. `ssh_options` are more arguments of `ssh`, before the destination.
    This opens no connection."""
    _check_text(text)
    options = _check_options(ssh_options)
    try:
        result = subprocess.run(
            [_ssh_program(), "-G", *options, text],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_QUICK,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RemoteConnectionError(f"`ssh -G {text}` took longer than {_QUICK} s") from error
    if result.returncode != 0:
        raise RemoteConnectionError(
            f"ssh cannot resolve the remote host {text!r}: {_message(result.stderr)}"
        )
    found: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        found.setdefault(key, value.strip())
    try:
        return Destination(text, found["user"], found["hostname"], int(found["port"]))
    except (KeyError, ValueError) as error:
        raise RemoteConnectionError(
            f"`ssh -G {text}` did not print a user, a host name and a port"
        ) from error


def sh_command(*args: str, script_path: Optional[str] = None) -> str:
    """Write the command line that starts a POSIX shell script on the remote host.

    Without `script_path`, the shell reads the script from its standard input. Every word in
    `args` must be plain (letters, digits and `._/:=@+-`), so that no shell, whatever the user's
    login shell is, can read it as anything else. The result is one line for `ssh`.
    `script_path` is an absolute path on the remote host; it goes in double quotes."""
    for arg in args:
        if not _PLAIN_TOKEN.fullmatch(arg):
            raise ValueError(f"not a plain word for a remote command line: {arg!r}")
        if arg.startswith("="):  # zsh expands `=word` to a path
            raise ValueError(f"a word of a remote command line cannot start with '=': {arg!r}")
    if args and args[0].startswith("-"):
        raise ValueError(
            f"the first word of a remote command line cannot start with '-': {args[0]!r}"
        )
    if script_path is None:
        return " ".join(["exec sh -s --", *args])
    if not script_path.startswith("/") or _UNSAFE_IN_PATH.search(script_path):
        raise ValueError(f"not a safe script path for a remote command line: {script_path!r}")
    return " ".join(["exec sh", f'"{script_path}"', *args])


def _message(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace").strip()
    return text or "no message"


def _communicate(
    command: Sequence[str],
    *,
    stdin: Any,
    stdout: Any = None,
    stderr: Any = None,
    input: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[bytes]":
    """Run `command` as `subprocess.run` does, and stop it when the launch is asked to stop
    (`cancel.launch_canceled`): kill it and raise `Canceled`. The request is asked every
    `CANCEL_POLL` seconds. A `timeout` raises `subprocess.TimeoutExpired`, as `subprocess.run`
    does. The client is also killed on every other exception, Ctrl-C included."""
    deadline = None if timeout is None else monotonic() + timeout
    data = input
    with subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr) as process:
        try:
            while True:
                wait = CANCEL_POLL if deadline is None else min(CANCEL_POLL, deadline - monotonic())
                try:
                    out, err = process.communicate(data, timeout=max(wait, 0))
                    if launch_canceled():  # it ended between two checks: its result is no result
                        raise Canceled("the launch was canceled while it waited for ssh")
                    return subprocess.CompletedProcess(command, process.returncode, out, err)
                except subprocess.TimeoutExpired as expired:
                    data = None  # the input is sent already, or `communicate` keeps sending it
                    if launch_canceled():
                        raise Canceled("the launch was canceled while it waited for ssh") from None
                    if deadline is not None and monotonic() >= deadline:
                        assert timeout is not None
                        raise subprocess.TimeoutExpired(
                            command, timeout, output=expired.output, stderr=expired.stderr
                        ) from None
        except BaseException:
            process.kill()
            raise


@contextmanager
def stop_on_cancel(stop: Callable[[], None], doing: str) -> Iterator[None]:
    """Call `stop` when the launch is asked to stop (`cancel.launch_canceled`), in any thread,
    for the time of the block, and then raise `Canceled`.

    `stop` ends what the block waits for: it kills the ssh client of a step, so a read of its
    output ends. The request is asked every `CANCEL_POLL` seconds. `doing` names the step in the
    message: "the launch was canceled while <doing>". The block raises `Canceled` also when it
    ended without noticing the stop, and when the request came after the last check of the
    watcher. An exception of another kind that a stop did not cause is raised as it is."""
    done = threading.Event()
    stopped = threading.Event()
    message = f"the launch was canceled while {doing}"

    def watch() -> None:
        while not done.wait(CANCEL_POLL):
            if launch_canceled():
                stopped.set()
                stop()
                return

    watcher = threading.Thread(target=contextvars.copy_context().run, args=(watch,), daemon=True)
    watcher.start()
    try:
        yield
    except Exception as error:
        if stopped.is_set() and not isinstance(error, Canceled):
            raise Canceled(message) from error
        raise
    finally:
        done.set()
        watcher.join()
    if stopped.is_set() or launch_canceled():  # a request after the last check of the watcher
        raise Canceled(message)


class SshSession:
    """One master connection to a host, for the time of a `with` block.

    `run` and `popen` start `ssh` processes that use the master. They check the master first and
    raise `RemoteConnectionError` if it is gone. This is needed because a client whose master
    is gone can open a new connection of its own, and that one would ask for a password or run
    the command on a connection that xeda did not check."""

    def __init__(self, destination: Destination, ssh_options: Sequence[str] = ()) -> None:
        _check_text(destination.text)  # a `Destination` can be made by hand
        self.destination = destination
        self.ssh_options = _check_options(ssh_options)
        self._directory: Optional[tempfile.TemporaryDirectory[str]] = None
        self._control = ""

    def __enter__(self) -> "SshSession":
        self._directory = self._make_directory()
        self._control = f"{self._directory.name}/c"
        try:
            self._start_master()
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._close()

    def _make_directory(self) -> "tempfile.TemporaryDirectory[str]":
        """A private directory (mode 0700) for the control socket. Its path is short, because
        a socket path has a small limit."""
        directory = tempfile.TemporaryDirectory(prefix="xeda-ssh-")
        if len(f"{directory.name}/c".encode()) > _MAX_CONTROL_PATH:
            directory.cleanup()
            directory = tempfile.TemporaryDirectory(prefix="xeda-ssh-", dir=_FALLBACK_DIRECTORY)
        return directory

    def _start_master(self) -> None:
        interval, count = SERVER_ALIVE
        command = [
            _ssh_program(),
            "-M",
            "-N",
            "-f",
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={self._control}",
            "-o",
            f"ControlPersist={CONTROL_PERSIST}",
            "-o",
            f"ServerAliveInterval={interval}",
            "-o",
            f"ServerAliveCountMax={count}",
            *self.ssh_options,
            self.destination.text,
        ]
        # The master authenticates in the foreground, with the terminal of the user, and then
        # goes to the background. Its standard error goes to a file and not to a pipe, because
        # the background process keeps the pipe open, and then nothing sees the end of the data.
        # (OpenSSH asks for passwords and passphrases on the terminal itself.)
        with tempfile.TemporaryFile() as messages:
            status = _communicate(
                command, stdin=None, stderr=messages, stdout=child_stdout()
            ).returncode
            messages.seek(0)
            text = messages.read()
        if status != 0:
            raise RemoteConnectionError(
                f"cannot connect to {self.destination.text}: {_message(text)}"
                f" (ssh exit status {status})"
            )
        if text.strip():
            log.warning("%s", text.decode("utf-8", "replace").rstrip())

    def _control_command(self, operation: str) -> list[str]:
        return [
            _ssh_program(),
            "-S",
            self._control,
            "-O",
            operation,
            "-o",
            "BatchMode=yes",
            *self.ssh_options,
            self.destination.text,
        ]

    def check(self) -> bool:
        """Whether the master connection is open."""
        if self._directory is None:
            return False
        try:
            return (
                subprocess.run(
                    self._control_command("check"),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=_QUICK,
                    check=False,
                ).returncode
                == 0
            )
        except subprocess.TimeoutExpired:
            return False

    def _client_command(self, command: str) -> list[str]:
        """The `ssh` command line of a client of the master.

        Ctrl-C in the terminal sends SIGINT to the whole foreground process group. A client of
        the master carries the cancel message of the launch, so it must not stop on Ctrl-C. It
        cannot ignore SIGINT: a client of a control socket installs its own SIGINT handler, which
        ends it with status 255, whatever it inherited. So `popen` starts the client in a process
        group of its own, and Ctrl-C does not reach it. (`preexec_fn` runs Python code between
        `fork` and `exec`, which can deadlock when other threads of xeda run, as the heartbeat
        does. `start_new_session` also drops the controlling terminal of the client.)

        A real client can still open a connection of its own if the master ends after
        `check()` and before the client connects. It cannot ask for anything (`BatchMode=yes`),
        and OpenSSH checks the host keys as always, so this costs at most a second login without
        a prompt."""
        return [
            _ssh_program(),
            "-S",
            self._control,
            "-o",
            "ControlMaster=no",
            "-o",
            "BatchMode=yes",
            "-T",
            *self.ssh_options,
            self.destination.text,
            command,
        ]

    def _require_master(self) -> None:
        if not self.check():
            raise RemoteConnectionError(f"the SSH connection to {self.destination.text} closed")

    def run(
        self, command: str, stdin: Optional[bytes] = None, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[bytes]":
        """Run `command` on the remote host and wait. `stdin` is its input (none: it gets EOF at
        once). A non-zero status of the command is in the result, not an exception. If `timeout`
        seconds pass, the client stops and `subprocess.TimeoutExpired` is raised. If the master
        is gone, this raises `RemoteConnectionError`.

        `subprocess.run` kills its client on KeyboardInterrupt, so only the clients of `popen`
        survive Ctrl-C. This is intended: the agent session uses `popen`."""
        self._require_master()
        result = _communicate(
            self._client_command(command),
            input=stdin,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode == 255:  # ssh's own failure, or the command's status 255
            self._require_master()
        return result

    def popen(self, command: str) -> "subprocess.Popen[bytes]":
        """Start `command` on the remote host. Its standard input, output and error are binary
        pipes of the returned process. The process runs in a process group of its own, so Ctrl-C
        in the terminal does not stop it. If the master is gone, this raises
        `RemoteConnectionError`."""
        self._require_master()
        return subprocess.Popen(
            self._client_command(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            process_group=0,
        )

    def _close(self) -> None:
        directory, self._directory = self._directory, None
        if directory is None:
            return
        try:
            if self._control:
                command = self._control_command("exit")
                subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=_QUICK,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError, RemoteConnectionError):
            pass  # the master is gone already, or it ends by itself after CONTROL_PERSIST
        finally:
            directory.cleanup()
