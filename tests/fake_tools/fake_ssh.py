#!/usr/bin/env python3
"""A fake OpenSSH client for the tests of remote runs: it never opens a network connection.

The remote host is this machine. The "remote" command runs as `/bin/sh -c <command>`, with
`XEDA_FAKE_SSH_HOME` as its working directory and `HOME`, and with an environment of only `PATH`
(from `XEDA_FAKE_SSH_PATH`), `HOME`, `USER` and `LANG`. The fake needs nothing but the standard
library of Python 3.9, because it runs on whatever `python3` the caller's `PATH` finds.

Environment:
  XEDA_FAKE_SSH_HOME           the remote home directory (required, except for `-G`)
  XEDA_FAKE_SSH_PATH           the `PATH` of the remote command (default `/usr/bin:/bin`)
  XEDA_FAKE_SSH_NOISE=1        print a banner line on stdout before the command, as a login shell
                               that echoes does
  XEDA_FAKE_SSH_REPORT_SIGINT=1  print `SIGINT ignored` or `SIGINT default` on stdout (after the
                               banner, before the command) for the disposition of SIGINT in this
                               process; the remote command always starts with the default one
  XEDA_FAKE_TOOL_FAIL          a list of tool names; with `ssh` in it, the master connection
                               (`-M`) fails with status 255 and "Permission denied (publickey)."

Calls are recorded as the other fakes record theirs, so that `tool_utils.fake_calls(directory)`
reads them: the file `fake_ssh.calls` in the parent directory of `XEDA_FAKE_SSH_HOME`, one
`CALL <n>` line per call followed by one `ARG` line per argument (not the program name). A line
break in an argument is written as `\\n`. Every call is recorded, before it does anything.

Modes (the options are parsed as OpenSSH parses them, up to the destination; the rest of the
arguments, joined with one space, is the command):
  -G                   print the lines `user`, `hostname` and `port` of the destination, which is
                       `[user@]host`, `ssh://[user@]host[:port]` or a bare name (user `xeda`,
                       port 22, if the text and the options `-l`, `-p`, `-o User=`, `-o Port=`
                       name none)
  -M ... -f            the master: create the control path (`-S` or `-o ControlPath=`) as a marker
                       file and exit 0. The directory of the control path must exist.
  -S <path> -O check   exit 0 if the marker exists, else 255 with "Control socket connect(...)"
  -S <path> -O exit    remove the marker (exit 0), else 255 as for check
  -S <path> <dest> <command>
                       if the marker does not exist, exit 255 with "Control socket connect(...)";
                       otherwise run the command. (A real client may open a connection of its own
                       instead, if the master ended before it connected. The fake always fails,
                       so that a test sees the case.)
  <dest> <command>     without `-S` or a control path: run the command

The command mode keeps stdin, stdout and stderr as pipes to the command, byte for byte, as
sshd does. The command runs in a session of its own, so a signal to the process group of this
process does not reach it. When this process ends, even by SIGKILL, the command gets EOF on its
stdin and keeps running, because the only copy of the write end of that pipe is here. Like sshd,
this process ends when the command has ended and both of its output pipes are closed, and its
exit status is that of the command (128 + n for a signal n).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

#: ssh options that take a value (the next argument, or the rest of the same one)
VALUE_OPTIONS = set("BbcDEeFIiJLlmOopQRSWw")
DEFAULT_USER = "xeda"
DEFAULT_PORT = 22
#: bytes to read at once
CHUNK = 65536


def record(argv: list[str]) -> None:
    """Append this call to `fake_ssh.calls` beside the remote home."""
    home = os.environ.get("XEDA_FAKE_SSH_HOME")
    if not home:
        return
    lines = [f"CALL {len(argv)}"] + [f"ARG {a}".replace("\n", "\\n") for a in argv]
    log = os.path.join(os.path.dirname(os.path.abspath(home)), "fake_ssh.calls")
    # one write of the whole call: calls of parallel processes do not interleave
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)


class Parsed:
    def __init__(self) -> None:
        self.flags: set[str] = set()
        self.values: dict[str, list[str]] = {}
        self.config: dict[str, str] = {}  # `-o key=value`, key in lower case
        self.destination: str | None = None
        self.command: list[str] = []

    def value(self, option: str) -> str | None:
        found = self.values.get(option)
        return found[-1] if found else None


def parse(argv: list[str]) -> Parsed:
    parsed = Parsed()
    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if parsed.destination is not None:
            parsed.command.append(arg)
        elif arg.startswith("-") and len(arg) > 1:
            j = 1
            while j < len(arg):
                letter = arg[j]
                j += 1
                if letter not in VALUE_OPTIONS:
                    parsed.flags.add(letter)
                    continue
                if j < len(arg):
                    value = arg[j:]
                elif i < len(argv):
                    value = argv[i]
                    i += 1
                else:
                    fail(f"option requires an argument -- {letter}")
                parsed.values.setdefault(letter, []).append(value)
                if letter == "o":
                    key, _, setting = value.replace("=", " ", 1).partition(" ")
                    parsed.config[key.lower()] = setting.strip()
                break
        else:
            parsed.destination = arg
    return parsed


def fail(message: str, status: int = 255):
    """Print `message` and end the process with `status`."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    sys.exit(status)


def split_destination(text: str, parsed: Parsed) -> tuple[str, str, int]:
    """The user, host name and port of the destination, as `ssh -G` prints them."""
    user = parsed.value("l") or parsed.config.get("user")
    port = parsed.value("p") or parsed.config.get("port")
    rest = text
    is_url = rest.startswith("ssh://")
    if is_url:
        rest = rest[len("ssh://") :]
    if "@" in rest:
        text_user, _, rest = rest.rpartition("@")
        user = user or text_user
    if is_url and ":" in rest:
        rest, _, text_port = rest.rpartition(":")
        port = port or text_port
    return user or DEFAULT_USER, rest, int(port or DEFAULT_PORT)


def control_path(parsed: Parsed) -> str | None:
    return parsed.value("S") or parsed.config.get("controlpath")


def no_master(path: str):
    fail(f"Control socket connect({path}): No such file or directory")


def master(parsed: Parsed) -> int:
    path = control_path(parsed)
    if "ssh" in os.environ.get("XEDA_FAKE_TOOL_FAIL", "").split():
        fail("Permission denied (publickey).")
    if not path:
        fail("fake ssh: a master needs a control path")
    try:
        with open(path, "w") as marker:
            marker.write("master\n")
    except OSError as error:
        fail(f"unix_listener: cannot bind to path {path}: {error.strerror}")
    return 0


def control_command(parsed: Parsed) -> int:
    path = control_path(parsed)
    operation = parsed.value("O")
    if not path:
        fail("fake ssh: -O needs a control path")
    if operation not in ("check", "exit"):
        fail(f"fake ssh: unsupported control command {operation}")
    if not os.path.exists(path):
        no_master(path)
    if operation == "exit":
        os.remove(path)
        sys.stderr.write("Exit request sent.\n")
    else:
        sys.stderr.write(f"Master running (pid={os.getpid()})\n")
    return 0


def sigint_disposition() -> str:
    return "ignored" if signal.getsignal(signal.SIGINT) == signal.SIG_IGN else "default"


def pump(read_fd: int, write_fd: int | None, close_pipe=None) -> None:
    """Copy bytes until EOF. Without `write_fd`, or once it is closed, drop what is read."""
    while True:
        try:
            data = os.read(read_fd, CHUNK)
        except OSError:
            break
        if not data:
            break
        while data and write_fd is not None:
            try:
                data = data[os.write(write_fd, data) :]
            except OSError:  # the reader is gone
                write_fd = None
    if close_pipe is not None:
        try:
            close_pipe()
        except OSError:
            pass


def run_command(parsed: Parsed) -> int:
    home = os.environ.get("XEDA_FAKE_SSH_HOME")
    if not home:
        fail("fake ssh: XEDA_FAKE_SSH_HOME is not set")
    path = control_path(parsed)
    if path and not os.path.exists(path):
        no_master(path)
    user = split_destination(parsed.destination or "", parsed)[0]
    env = {
        "PATH": os.environ.get("XEDA_FAKE_SSH_PATH", "/usr/bin:/bin"),
        "HOME": home,
        "USER": user,
        "LANG": "C.UTF-8",
    }
    out = []
    if os.environ.get("XEDA_FAKE_SSH_NOISE") == "1":
        out.append("Welcome to the fake host (a banner that a login shell printed)\n")
    if os.environ.get("XEDA_FAKE_SSH_REPORT_SIGINT") == "1":
        out.append(f"SIGINT {sigint_disposition()}\n")
    if out:
        os.write(1, "".join(out).encode())
    child = subprocess.Popen(
        ["/bin/sh", "-c", " ".join(parsed.command)],
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        # The caller may have started this process with SIGINT ignored. sshd does not pass that
        # on to the command.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    assert child.stdin and child.stdout and child.stderr
    # Not joined: it ends with the process, or with EOF on stdin.
    threading.Thread(
        target=pump, args=(0, child.stdin.fileno(), child.stdin.close), daemon=True
    ).start()
    outputs = [
        threading.Thread(target=pump, args=(child.stdout.fileno(), 1)),
        threading.Thread(target=pump, args=(child.stderr.fileno(), 2)),
    ]
    for thread in outputs:
        thread.start()
    status = child.wait()
    for thread in outputs:
        thread.join()
    return 128 - status if status < 0 else status


def main(argv: list[str]) -> int:
    record(argv)
    try:
        parsed = parse(argv)
        if "G" in parsed.flags:
            user, host, port = split_destination(parsed.destination or "", parsed)
            sys.stdout.write(f"user {user}\nhostname {host}\nport {port}\n")
            return 0
        if parsed.destination is None:
            fail("usage: ssh [options] destination [command]")
        if "M" in parsed.flags:
            return master(parsed)
        if "O" in parsed.values:
            return control_command(parsed)
        if not parsed.command:
            fail("fake ssh: an interactive session is not supported")
        return run_command(parsed)
    finally:
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    sys.stderr.flush()
    # not `sys.exit`: the thread that reads stdin may be blocked in `read`
    os._exit(code)
