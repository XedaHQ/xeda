"""The xeda agent on the remote host: `python -m xeda.remote.agent session|check|clean ...`.

The local xeda starts the agent through the SSH connection, with the code snapshot on
`PYTHONPATH`. The agent writes `MAGIC` first, and after it only frames (`xeda.remote.frames`).
Text that the login shell printed before `MAGIC` is noise. Whatever else the agent or a library
prints goes to stderr.

`session <root> <workspace key> [--lease SECONDS]` serves one launch. `<root>` is the remote
root `R`, and the workspace is `R/v1/ws/<key>/` with the directories `inputs/<design>/`,
`xeda_run/` and `launches/<id>/`, and the file `lock`. The frames after `MAGIC`, in order:

1. agent: `J {"hello": {"xeda": version, "pid": n}}`
2. client: `J {"sync": {"workspace": key, "design": name, "entries": [...], "id": launch id}}`.
   The `id` is optional. The agent takes the workspace lock (and sends `L` lines every 10 s
   while it waits), compares the entries with `inputs/<design>/` and answers
   `J {"need": [paths]}`. The client sends the tar of these files as `DATA` frames and an `END`
   frame (also when `need` is empty). The agent answers
   `J {"synced": {"written": n, "removed": m}}`.
3. client: `J {"launch": {<LaunchRequest fields>}, "id": launch id, "setup": [lines]}`. The agent
   starts the launch in `launches/<id>/` and streams its log as `L` frames, one for each line. When
   the launch ends, the agent sends `J {"result": <result.json or null>, "canceled": bool,
   "exit": n, "survivors": n}`. `survivors` counts the processes that SIGKILL did not remove.
   While the launch runs, the client sends `H` frames, and `C` to cancel it.
4. client: `J {"fetch": {"fetch_all": bool}}` (or none, for a dry run). The agent answers
   `J {"files": [entries]}` and then the tar of these files as `DATA` frames and an `END` frame.
5. client: `J {"bye": true}`. The agent releases the lock and exits with status 0.

A launch with `"keep_running": true` in the `launch` frame survives the session. The child
holds the workspace lock and keeps it until it ends. When the client is gone, or the agent
gets a signal, the agent leaves the launch running and exits with status 0. Only a `C` frame
cancels it. The `sync` frame can carry a `"digest"` of the request. If a launch that keeps
running has that digest, the agent answers the `sync` with `J {"attached": {...}}` and no `need`: it
sends the log of that launch from its start, then the `result` as in step 3, and the session
goes on with step 4. Two more first frames, in place of `sync`:

- `J {"attach": {"workspace": key, "id": launch id or null}}`: the same as an `attached` answer,
  for the launch with this id, else the newest running launch, else the newest launch. The agent
  refuses to fetch the files of a launch if a later launch has used the same inputs.
- `J {"cancel": {"workspace": key, "id": launch id or null}}`: cancel the running launch with this
  id, else the newest running one, as for a `C` frame. The agent answers
  `J {"stopped": {"id": launch id, "survivors": n}}` and exits.

At any time the agent can send `J {"error": {"type": name, "message": text}}` and exit with
status 1. If the client cancels (`C`) or goes away before the launch starts, the agent sends
`J {"canceled": true, "reason": text}` where it can, and exits with status 0.

The client is gone when its stream ends, when it sends no frame for the lease after it began to
send `H` frames, when it sends no frame for the lease at all (for a running launch), and when it
does not read: one write of a frame to it takes longer than the lease. A thread writes all frames,
and the agent never waits for a write while a launch runs. So output cannot delay a cancel, the
lease or the signals.

The agent cancels a running launch in that case, as for a `C` frame: SIGINT to the process group of
the launch, SIGTERM to it and to its descendants after the first grace period, SIGKILL to what
is left after the second. The periods run on the clock of the agent. The result is sent only after all of them
are gone.

An agent process (every command) that runs from an environment and a code copy in `R/v1/env/` and
`R/v1/code/` takes a shared lock on the file `.xeda-lock` of each before it does anything else
(before `hello`, for a session), and holds both until it ends. Every launch holds both locks and
the workspace lock until it ends, also when the agent has died. The agent does not pass them to the
login shell of the launch, since a program that the login profile or a `setup` line leaves in the
background would keep them for ever. It sends them through a Unix socket, and the launch entry
(`xeda.remote.launch_entry`) takes them from it as its first action. If the directory is being
removed or was removed (by `clean`), the agent reports an `error` (a session sends an `error`
frame) and exits with status 1.

`check [--json] <executables...>` and `clean <root> [--older-than DAYS] [--all] [--list]
[--only PATH]...` write `MAGIC` and then one JSON document and a newline. With `--list`, `clean`
removes nothing and reports what it would remove as `removable`, in place of `removed`. With
`--only`, it looks at the listed paths only, and each one still goes through every check.
"""

import argparse
import errno
import fcntl
import hashlib
import io
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional, Protocol, cast

import psutil

from ..utils import json_encodable, replacing_file
from ..version import __version__
from .constants import CANCEL_GRACE, LAYOUT, LEASE
from .errors import RemoteError, RemoteProtocolError
from .frames import (
    BEAT,
    CANCEL,
    DATA,
    END,
    JSON,
    LOG,
    MAGIC,
    FrameWriter,
    decode_json,
    read_frame,
)
from .launch_entry import (
    HAND_OVER_MAX,
    HAND_OVER_SIZE,
    REQUEST_FILE,
    RESULT_FILE,
    LaunchRequest,
)
from .manifest import Entry, apply_sync, build_manifest, plan_sync, safe_member_path, write_tar

#: Seconds between two `L` lines that say the agent waits for the workspace lock.
LOCK_NOTICE = 10.0

#: Seconds between two looks at the child and the client while a launch runs.
_TICK = 0.05

#: Seconds to wait for processes to be gone after SIGKILL.
_KILL_WAIT = 10.0

#: A launch id and a workspace key: one plain name, never a path.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: Seconds between two looks at the descendants of a launch.
_SEEN_EVERY = 0.25

#: The values of `state` once a cancel was asked for.
_CANCELED = ("canceling", "canceled")

#: A SHA-256 digest of a request, as the client writes it.
_DIGEST = re.compile(r"[0-9a-f]{64}")

#: The `clean` default: remove what nobody used for this many days.
_DEFAULT_DAYS = 30.0

_CHUNK = 1 << 20

#: The size of a `DATA` frame that the agent sends. A write of one frame to a slow client counts as
#: a stall when it takes longer than the lease, so a frame is small.
_DATA_FRAME = 1 << 16

#: The most log bytes that the agent sends in one look at the log (one tick).
_PUMP_BYTES = 1 << 18

#: The frames that wait for the writer thread.
_QUEUE_DEPTH = 32

#: Seconds that the agent waits to queue a closing message (an error, or a cancel notice).
_TELL_WAIT = 2.0


class _SessionEnded(Exception):
    """The session ends before a launch started: the client canceled, or the agent got a signal,
    or the connection ended. `client_gone` tells that nobody is there to be told."""

    def __init__(self, reason: str, client_gone: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.client_gone = client_gone


# ---------------------------------------------------------------------------------------------
# The client's frames


class _Client:
    """Read the frames of the client in one thread, so that a heartbeat, a cancel and the end of
    the stream are seen while the main thread waits for a lock, a file or a child.

    `H` and `C` frames are consumed here. Every other frame goes to `messages`, in order, and the
    end of the stream goes there as `None`. Every frame counts as a sign of life."""

    def __init__(self, stream: BinaryIO) -> None:
        self.messages: "queue.Queue[Optional[tuple[bytes, bytes]]]" = queue.Queue(maxsize=16)
        self.canceled = threading.Event()
        self.gone = threading.Event()
        self.beating = False
        self.last_seen = time.monotonic()
        self.error: Optional[BaseException] = None
        self._stream = stream
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="client-frames", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _put(self, item: Optional[tuple[bytes, bytes]]) -> None:
        while not self._stop.is_set():
            try:
                self.messages.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _run(self) -> None:
        try:
            while frame := read_frame(self._stream):
                self.last_seen = time.monotonic()
                kind, payload = frame
                if kind == BEAT:
                    self.beating = True
                elif kind == CANCEL:
                    self.canceled.set()
                else:
                    self._put(frame)
        except BaseException as error:  # pylint: disable=broad-except
            self.error = error
        finally:
            self.gone.set()
            self._put(None)


class _TarSource(io.RawIOBase):
    """The `DATA` frames of one stream from the client, up to its `END` frame, as a file."""

    def __init__(self, take: Callable[[], tuple[bytes, bytes]]) -> None:
        super().__init__()
        self._take = take
        self._pending = memoryview(b"")
        self._ended = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        target = memoryview(buffer).cast("B")
        while not self._pending and not self._ended and target:
            kind, payload = self._take()
            if kind == DATA:
                self._pending = memoryview(payload)
            elif kind == END:
                self._ended = True
            else:
                raise RemoteProtocolError(f"frame {kind!r} inside a data stream")
        count = min(len(target), len(self._pending))
        target[:count] = self._pending[:count]
        self._pending = self._pending[count:]
        return count


class _FrameSink(io.RawIOBase):
    """A file that sends what is written to it as `DATA` frames."""

    def __init__(self, send: Callable[[bytes, bytes], None]) -> None:
        super().__init__()
        self._send = send

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        for start in range(0, len(view), _DATA_FRAME):
            self._send(DATA, bytes(view[start : start + _DATA_FRAME]))
        return len(view)


class _FdWriter:
    """A binary file over a file descriptor. `write` writes every byte. It has no buffer and no
    Python lock, so that a thread blocked in a write cannot stop the interpreter at exit."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            view = view[os.write(self._descriptor, view) :]
        return len(data)

    def flush(self) -> None:
        pass


class _Output:
    """Send frames from one thread, so that a client that does not read blocks that thread only.

    The session queues frames and never waits for the writer, except where it has nothing else to
    do (`Session.send`). The output has failed when a write raised, or when one write has taken
    longer than `stall` seconds. A failed output drops every frame."""

    def __init__(self, writer: FrameWriter, stall: float) -> None:
        self._writer = writer
        self._stall = stall
        self._queue: "queue.Queue[Optional[tuple[bytes, bytes]]]" = queue.Queue(
            maxsize=_QUEUE_DEPTH
        )
        self._busy_since: Optional[float] = None
        self._broken = False
        self._thread = threading.Thread(target=self._run, name="frame-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def failed(self) -> bool:
        since = self._busy_since
        if since is not None and time.monotonic() - since > self._stall:
            self._broken = True
        return self._broken

    def offer(self, kind: bytes, payload: bytes = b"") -> bool:
        """Queue a frame without waiting. Return `False` if the queue is full. A failed output
        drops the frame and returns `True`."""
        if self.failed():
            return True
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            return False
        return True

    def close(self) -> None:
        """Let the writer send what is queued, as long as it makes progress."""
        while not self.failed():
            try:
                self._queue.put(None, timeout=_TICK)
                break
            except queue.Full:
                continue
        while self._thread.is_alive() and not self.failed():
            self._thread.join(_TICK)

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            if self._broken:
                continue
            self._busy_since = time.monotonic()
            try:
                self._writer.send(*item)
            except BaseException:  # pylint: disable=broad-except
                self._broken = True
            finally:
                self._busy_since = None


# ---------------------------------------------------------------------------------------------
# The processes of a launch


def _running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _group_members(pgid: int) -> list[psutil.Process]:
    """The running processes in the process group `pgid`. The launch child starts its own
    session, so its group holds the launch and every process that did not leave the group. A
    tool leads a group of its own: the launch stops that group itself when it gets SIGINT or
    SIGTERM, and the agent finds the tool as a descendant."""
    found = []
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid and _running(process):
                found.append(process)
        except (OSError, psutil.Error):
            continue
    return found


def _signal_process(process: psutil.Process, number: int) -> None:
    try:
        process.send_signal(number)  # psutil checks that the process is still the same one
    except psutil.Error:
        pass


class _LogTail:
    """Send the lines of a growing log file as `L` frames, a bounded amount at a time."""

    def __init__(self, path: Path, offer: Callable[[bytes, bytes], bool]) -> None:
        self._file = path.open("rb")
        self._offer = offer
        self._lines: "deque[bytes]" = deque()
        self._rest = b""
        self._at_end = False

    def pump(self, final: bool = False) -> int:
        """Send the complete lines that are there now, at most `_PUMP_BYTES` bytes, and never
        wait for room. A line that finds no room stays for the next call. With `final`, the
        incomplete last line goes too. Return the number of bytes sent."""
        sent = 0
        while sent < _PUMP_BYTES:
            if not self._lines:
                data = self._file.read(1 << 16)
                self._at_end = not data
                if not data:
                    if final and self._rest:
                        self._lines.append(self._rest)
                        self._rest = b""
                        continue
                    break
                *lines, self._rest = (self._rest + data).split(b"\n")
                self._lines.extend(lines)
                if len(self._rest) > _CHUNK:
                    self._lines.append(self._rest)
                    self._rest = b""
                continue
            if not self._offer(LOG, self._lines[0]):
                break
            sent += len(self._lines.popleft()) + 1
        return sent

    def finished(self) -> bool:
        """Whether `pump(final=True)` has sent everything."""
        return self._at_end and not self._lines and not self._rest

    def close(self) -> None:
        self._file.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, document: Any) -> None:
    with replacing_file(path, encoding="utf-8") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")


def _read_json(path: Path) -> Any:
    """The JSON document in the regular file `path`, or `None` if there is none or it is not
    JSON. A link is not followed."""
    try:
        if not os.path.isfile(path) or os.path.islink(path):
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_result(launch_dir: Path) -> Any:
    """The `result.json` of a launch: `None` if the launch wrote none. A file that is not a JSON
    object raises `RemoteProtocolError`."""
    path = launch_dir / RESULT_FILE
    if not os.path.lexists(path):
        return None
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("it is not a regular file")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("it is not a JSON object")
    except (OSError, ValueError) as error:
        raise RemoteProtocolError(f"{RESULT_FILE} of the launch is damaged: {error}") from error
    return document


def _alive(pid: int, created: float) -> bool:
    """Whether the process `pid` runs and has the start time `created` (a reused process id is
    another process)."""
    try:
        process = psutil.Process(pid)
        return _running(process) and abs(process.create_time() - created) < 1.0
    except psutil.Error:
        return False


#: The values of `state` in `state.json` while the launch process may run.
_ACTIVE = ("running", "canceling")


def _launch_running(state: Any) -> bool:
    """Whether the launch that `state.json` describes still runs: its process is alive and has
    the start time that was recorded."""
    if not isinstance(state, dict) or state.get("state") not in _ACTIVE:
        return False
    try:
        return _alive(int(state["pid"]), float(state["pid_created"]))
    except (KeyError, TypeError, ValueError):
        return False


class _Child(Protocol):
    """What `Session.stop_launch` needs of the launch process."""

    pid: int

    def poll(self) -> Optional[int]: ...


class _ProcessRef:
    """The process of a launch that this agent did not start (or that it no longer owns), found
    by the process id and start time in `state.json`. `poll` returns `None` while it runs."""

    def __init__(self, state: dict[str, Any]) -> None:
        try:
            self.pid = int(state["pid"])
            self._created = float(state["pid_created"])
        except (KeyError, TypeError, ValueError):
            raise RemoteProtocolError("the record of the launch has no process") from None

    def poll(self) -> Optional[int]:
        return None if _alive(self.pid, self._created) else 0


def _launch_label(state: dict[str, Any], launch_dir: Path) -> str:
    """The state of a launch for people: `running`, `canceled`, `finished`, or `lost` (the process
    is gone and left no result)."""
    if _launch_running(state):
        return "running"
    if state.get("state") in ("canceling", "canceled"):
        return "canceled"
    return "finished" if os.path.lexists(launch_dir / RESULT_FILE) else "lost"


def _update_state(launch_dir: Path, **changes: Any) -> dict[str, Any]:
    """Change keys of `state.json` of a launch, and return the new record."""
    state = _read_json(launch_dir / "state.json")
    state = dict(state) if isinstance(state, dict) else {}
    state.update(changes)
    _write_json(launch_dir / "state.json", state)
    return state


def _is_file_at(descriptor: int, path: Path) -> bool:
    """Whether the open file `descriptor` is the file that is at `path` now."""
    try:
        opened, named = os.fstat(descriptor), os.lstat(path)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


def _order(state: dict[str, Any]) -> tuple[int, str]:
    """The sort key of a launch: the order in which the launches ran in the workspace. The id does
    not give it, because a client makes the id before it waits for the lock. A record without
    a sequence number comes first, ordered by its id."""
    sequence = state.get("sequence")
    return (sequence if isinstance(sequence, int) else 0, state["id"])


#: The lock file of an environment or a code copy (`bootstrap.sh` makes it). A session holds a
#: shared lock on it while the session runs from the directory, and so does every launch, which
#: takes it through the hand-over socket. `clean` removes the directory only while it holds the
#: exclusive lock.
USE_LOCK = ".xeda-lock"


def _runtime_directories() -> list[Path]:
    """The environment and the code copy that this process runs from, if they are in the layout
    `<root>/v1/env/<key>` and `<root>/v1/code/<key>`. An agent that runs from a checkout or from
    another environment has none."""
    own = (("env", Path(sys.prefix)), ("code", Path(os.path.abspath(__file__)).parents[2]))
    return [
        directory
        for kind, directory in own
        if directory.parent.name == kind and directory.parent.parent.name == LAYOUT
    ]


def _hold_use_locks() -> list[int]:
    """Take the shared locks on the environment and the code copy that this process runs from.
    The process holds them until it ends, and every launch takes them through the hand-over
    socket."""
    held: list[int] = []
    try:
        for directory in _runtime_directories():
            held.append(_take_use_lock(directory))
    except BaseException:
        for descriptor in held:
            os.close(descriptor)
        raise
    return held


def _take_use_lock(directory: Path) -> int:
    """Take a shared lock on the lock file of an environment or a code copy, and return the open
    file. Raise `RemoteError` if `clean` removes the directory now or removed it before the lock.
    The lock file of a directory that `clean` renamed is not at its path, so the check after the
    lock catches the removal that began between the open and the lock."""
    path = directory / USE_LOCK
    removed = RemoteError(
        f"{directory} was removed by `xeda remote clean` while the agent started. Run the "
        "command again."
    )
    try:
        # A link is refused; a FIFO does not block the open.
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except FileNotFoundError:
        raise removed from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RemoteProtocolError(f"the lock {path} is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RemoteError(
                f"`xeda remote clean` removes {directory} now. Run the command again."
            ) from None
        if not _is_file_at(descriptor, path):
            raise removed
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _launch_states(workspace: Path) -> list[Any]:
    """The `state.json` documents of the launches of a workspace. The `launches` directory and
    each launch directory must be real directories: a link is not followed."""
    states: list[Any] = []
    launches = workspace / "launches"
    try:
        if not stat.S_ISDIR(os.lstat(launches).st_mode):
            return states
        with os.scandir(launches) as listing:
            directories = sorted(e.path for e in listing if e.is_dir(follow_symlinks=False))
    except OSError:
        return states
    for directory in directories:
        state = _read_json(Path(directory) / "state.json")
        if state is not None:
            states.append(state)
    return states


# ---------------------------------------------------------------------------------------------


def _limit(text: str, limit: float, what: str) -> float:
    try:
        number = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{what}: {text!r} is not a number") from None
    if not 0 <= number <= limit:
        raise argparse.ArgumentTypeError(f"{what}: {text} is not between 0 and {limit:g}")
    return number


def _grace(text: str) -> tuple[float, float]:
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--grace needs two numbers, for example 1,1")
    return (
        _limit(parts[0], CANCEL_GRACE[0], "--grace (SIGINT to SIGTERM)"),
        _limit(parts[1], CANCEL_GRACE[1], "--grace (SIGTERM to SIGKILL)"),
    )


class Session:
    """One session with the local xeda: sync, launch, fetch."""

    def __init__(
        self,
        root: Path,
        key: str,
        client: _Client,
        out: _Output,
        *,
        lease: float = LEASE,
        grace: tuple[float, float] = CANCEL_GRACE,
        lock_notice: float = LOCK_NOTICE,
        use_locks: Sequence[int] = (),
    ) -> None:
        if not _NAME.fullmatch(key):
            raise RemoteProtocolError(f"{key!r} is not a workspace key")
        self.workspace = root / LAYOUT / "ws" / key
        self.key = key
        self.client = client
        self.out = out
        self.lease = lease
        self.grace = grace
        self.lock_notice = lock_notice
        self.signal_name: Optional[str] = None
        self.request: Optional[LaunchRequest] = None
        self.design = ""
        self.result: Any = None
        #: The launch of this session survives it (see the module text).
        self.keep_running = False
        #: The descendants of the launch that were seen while it ran, so that a process that its
        #: leader left behind is still known when the launch is canceled.
        self._seen: set[psutil.Process] = set()
        self._seen_at = 0.0
        #: The digest of the request that `sync` named, if it named one.
        self.digest: Optional[str] = None
        self._lock_fd: Optional[int] = None
        #: The shared locks on the environment and the code copy of this agent. The process owns
        #: them (`_hold_use_locks`); the session hands them over to every launch (`launch`).
        self._use_fds = tuple(use_locks)
        self._child: Optional["subprocess.Popen[bytes]"] = None
        #: The end of the lock hand-over socket (`launch`) that the launch reads. The session
        #: keeps it until the launch has ended (`release_hand_over`).
        self._hand_over: Optional[socket.socket] = None

    # -- sending

    def offer(self, kind: bytes, payload: bytes = b"") -> bool:
        """Queue a frame without waiting. Return `False` if there is no room now."""
        return self.out.offer(kind, payload)

    def send(self, kind: bytes, payload: bytes = b"") -> None:
        """Queue a frame, and wait for room while the client reads. If the client does not read,
        or has gone, raise `_SessionEnded`. Only use it where the session has nothing else to
        watch: a launch that runs is supervised without it."""
        while not self.offer(kind, payload) or self.out.failed():
            if self.out.failed() or self.client.gone.is_set():
                raise _SessionEnded("the connection to the client failed", client_gone=True)
            time.sleep(_TICK)

    def send_json(self, document: Any) -> None:
        text = json.dumps(document, separators=(",", ":"), default=json_encodable)
        self.send(JSON, text.encode("utf-8"))

    def tell(self, document: Any) -> None:
        """Queue a closing message if there is room soon. Never raise."""
        text = json.dumps(document, separators=(",", ":"), default=json_encodable)
        deadline = time.monotonic() + _TELL_WAIT
        while not self.offer(JSON, text.encode("utf-8")) and time.monotonic() < deadline:
            time.sleep(_TICK)

    def say(self, text: str) -> None:
        self.offer(LOG, f"xeda agent: {text}".encode())

    # -- receiving

    def lost(self, launching: bool) -> Optional[str]:
        """The reason the client is lost, or `None`. The lease counts when a launch runs, or
        after the client sent its first heartbeat."""
        if self.signal_name:
            return f"the agent got {self.signal_name}"
        if self.client.canceled.is_set():
            return "the client asked to cancel"
        silent = time.monotonic() - self.client.last_seen
        if (launching or self.client.beating) and silent > self.lease:
            return f"the client sent nothing for {silent:.0f} s (the lease is {self.lease:g} s)"
        if self.out.failed():
            return "the client does not read the output of the agent"
        if self.client.gone.is_set():
            return "the connection to the client ended"
        return None

    def take(self) -> tuple[bytes, bytes]:
        """The next frame of the client that is not a heartbeat or a cancel request."""
        while True:
            reason = self.lost(launching=False)
            if reason and not self.client.gone.is_set():
                raise _SessionEnded(reason, client_gone=self.out.failed())
            try:
                item = self.client.messages.get(timeout=_TICK)
            except queue.Empty:
                if reason:
                    raise _SessionEnded(reason, client_gone=True) from None
                continue
            if item is None:
                if self.client.error is not None:
                    raise RemoteProtocolError(
                        f"the stream of the client is damaged: {self.client.error}"
                    )
                raise _SessionEnded("the connection to the client ended", client_gone=True)
            return item

    def take_json(self) -> dict[str, Any]:
        kind, payload = self.take()
        if kind != JSON:
            raise RemoteProtocolError(f"expected a JSON frame, got {kind!r}")
        document = decode_json(payload)
        if not isinstance(document, dict):
            raise RemoteProtocolError("a JSON frame of the client is not an object")
        return document

    # -- the session

    def run(self) -> int:
        try:
            self.send_json({"hello": {"xeda": __version__, "pid": os.getpid()}})
            message = self.take_json()
            if "bye" in message:
                return 0
            if "cancel" in message:
                self.cancel(message["cancel"])
                return 0
            if "attach" in message:
                self.attach(message["attach"])
            elif running := self.sync(message):
                self.follow(running)
            else:
                message = self.take_json()
                if "bye" in message:
                    return 0
                if "launch" not in message:
                    raise RemoteProtocolError(f"expected `launch`, got {sorted(message)}")
                self.launch(message)
            while True:
                message = self.take_json()
                if "fetch" in message:
                    self.fetch(message["fetch"])
                elif "bye" in message:
                    return 0
                else:
                    raise RemoteProtocolError(f"expected `fetch` or `bye`, got {sorted(message)}")
        except _SessionEnded as ended:
            if self.result is None and not ended.client_gone:
                self.tell({"canceled": True, "reason": ended.reason})
            return 0
        except Exception as error:  # pylint: disable=broad-except
            self.tell({"error": {"type": type(error).__name__, "message": str(error)}})
            print(f"xeda agent: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        finally:
            self.close()

    def close(self) -> None:
        """Stop a launch that still runs, and release the lock."""
        try:
            child = self._child
            if child is not None and child.poll() is None and not self.keep_running:
                self.stop_launch(child, None, "the session ends")
        finally:
            self.release_hand_over()
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    # -- step 2: the lock and the sync

    def holder(self) -> str:
        """Who holds the workspace lock, as far as the launch records say."""
        try:
            for state in _launch_states(self.workspace):
                if _launch_running(state):
                    return f"launch {state['id']} (since {state['started']})"
        except OSError:
            pass
        return "another session"

    def take_lock(self) -> None:
        """Take the lock of the workspace, and wait for it while the client is there. A lock file
        that was removed or replaced while the agent waited (by `clean`) is not the lock of the
        workspace any more: the agent opens the new file and waits for that one."""
        path = self.workspace / "lock"
        next_notice = 0.0
        while True:
            self.workspace.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RemoteProtocolError(f"the lock {path} is not a regular file")
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                    if reason := self.lost(launching=False):
                        raise _SessionEnded(reason, client_gone=self.out.failed())
                    if time.monotonic() >= next_notice:
                        self.offer(
                            LOG, f"waiting for the workspace lock held by {self.holder()}".encode()
                        )
                        next_notice = time.monotonic() + self.lock_notice
                    time.sleep(_TICK)
                if _is_file_at(descriptor, path):
                    os.utime(descriptor)  # the age of the workspace, for `clean`
                    self._lock_fd = descriptor
                    return
            except BaseException:
                os.close(descriptor)
                raise
            os.close(descriptor)

    def sync(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Take the lock and bring the inputs up to date. Return the record of a running
        launch with the digest of the request, if the client named one and there is such a launch:
        then nothing is synced, and the lock is not taken."""
        body = message.get("sync")
        if not isinstance(body, dict):
            raise RemoteProtocolError(f"expected `sync`, got {sorted(message)}")
        if body.get("workspace") != self.key:
            raise RemoteProtocolError(
                f"the client names the workspace {body.get('workspace')!r}, and this agent "
                f"serves {self.key!r}"
            )
        design = body.get("design")
        if not isinstance(design, str) or "/" in design or design.startswith("."):
            raise RemoteProtocolError(f"{design!r} is not a design name")
        safe_member_path(design)
        raw = body.get("entries")
        if not isinstance(raw, list):
            raise RemoteProtocolError("`entries` of `sync` is not a list")
        entries = [Entry.from_json_value(item) for item in raw]
        directories = body.get("directories", [])
        if not isinstance(directories, list):
            raise RemoteProtocolError("`directories` of `sync` is not a list")
        for directory in directories:
            safe_member_path(directory)
        self.design = design
        digest = body.get("digest")
        if digest is not None and not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
            raise RemoteProtocolError("`digest` of `sync` is not a SHA-256 digest")
        self.digest = digest
        if digest is not None:
            for state in reversed(_launch_states(self.workspace)):
                if (
                    _launch_running(state)
                    and state.get("keep_running")
                    and state.get("digest") == digest
                ):
                    return cast(dict[str, Any], state)
        self.take_lock()
        root = self.workspace / "inputs" / design
        plan = plan_sync(root, entries)
        self.send_json({"need": plan.missing})
        source = io.BufferedReader(_TarSource(self.take))
        apply_sync(root, entries, plan, source, directories=directories)
        self.send_json({"synced": {"written": len(plan.missing), "removed": len(plan.stale)}})
        return None

    # -- step 3: the launch

    def check_paths(self, request: LaunchRequest) -> None:
        """Refuse a launch outside the workspace that the session holds: its inputs must be in the
        design directory that the sync filled, its run root must be the one of the workspace, and
        its design file must be a plain relative path."""
        for name, value in (("inputs_dir", request.inputs_dir), ("run_root", request.run_root)):
            if not os.path.isabs(value):
                raise RemoteProtocolError(f"{name} {value!r} is not an absolute path")
        safe_member_path(request.design_file)
        real = os.path.realpath
        if not self.design or not Path(real(request.inputs_dir)).is_relative_to(
            real(self.workspace / "inputs" / self.design)
        ):
            raise RemoteProtocolError(
                f"inputs_dir {request.inputs_dir} is not in the inputs of the synced design"
            )
        if real(request.run_root) != real(self.workspace / "xeda_run"):
            raise RemoteProtocolError(
                f"run_root {request.run_root} is not the run root of the workspace"
            )

    def _lock_fds(self) -> list[int]:
        locks = [] if self._lock_fd is None else [self._lock_fd]
        return [*locks, *self._use_fds]

    def release_hand_over(self) -> None:
        """Close the end of the hand-over socket that the session kept for the launch. If the
        launch has ended and its entry never took the locks (the launch was canceled while the
        profile ran), take them from the socket and close them: a program that the profile
        left in the background has the socket, and so it would hold them. While the launch runs,
        its entry may still read the socket, so the locks stay in it."""
        receiver, self._hand_over = self._hand_over, None
        if receiver is None:
            return
        try:
            if self._child is not None and self._child.poll() is not None:
                receiver.setblocking(False)
                try:
                    _data, left, _flags, _address = socket.recv_fds(
                        receiver, HAND_OVER_SIZE, HAND_OVER_MAX
                    )
                except OSError:  # nothing in the socket
                    left = []
                for descriptor in left:
                    os.close(descriptor)
        finally:
            receiver.close()

    def launch(self, message: dict[str, Any]) -> None:
        launch_id = message.get("id")
        if not isinstance(launch_id, str) or not _NAME.fullmatch(launch_id):
            raise RemoteProtocolError(f"{launch_id!r} is not a launch id")
        setup = message.get("setup", [])
        if not isinstance(setup, list) or not all(
            isinstance(line, str) and "\0" not in line for line in setup
        ):
            raise RemoteProtocolError("`setup` of `launch` is not a list of text lines")
        try:
            request = LaunchRequest.model_validate(message["launch"])
        except (KeyError, ValueError) as error:
            raise RemoteProtocolError(f"`launch` is not a launch request: {error}") from None
        keep_running = message.get("keep_running", False)
        if not isinstance(keep_running, bool):
            raise RemoteProtocolError("`keep_running` of `launch` is not true or false")
        self.check_paths(request)
        bash = shutil.which("bash")
        if bash is None:
            raise RemoteError(
                "bash was not found on the PATH of the remote host. Xeda starts a launch with "
                "`bash -lc`, so that the setup lines of the host profile and the login profile "
                "apply. Install bash there."
            )
        self.request = request
        launch_dir = self.workspace / "launches" / launch_id
        # The workspace lock is held, so no other launch takes a number meanwhile.
        sequence = 1 + max((_order(state)[0] for state in self.records()), default=0)
        launch_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            launch_dir.mkdir()
        except FileExistsError:
            raise RemoteProtocolError(f"the launch {launch_id} exists already") from None
        _write_json(launch_dir / REQUEST_FILE, request.model_dump(mode="json"))
        code = Path(__file__).resolve().parent.parent.parent
        log = launch_dir / "log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        # The launch must hold the workspace lock and the locks on its environment and code copy
        # until it ends, also when this agent has exited or died. But the login profile and the
        # `setup` lines run first, and a program that they leave in the background would inherit
        # the locks and hold them for ever. So the locks go into a Unix socket, and they stay in
        # flight there (the open files, and so the locks, stay alive) until the launch entry
        # takes them as its first action. The shell gets only the end of the socket that the entry
        # reads, and the number of that descriptor is an argument of the entry, so that no profile
        # can change it. The agent keeps its copy of that end until the launch has ended.
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            locks = self._lock_fds()
            socket.send_fds(sender, [str(len(locks)).encode()], locks)
            sender.close()
            script = "".join(f"{line}\n" for line in setup) + (
                f"PYTHONPATH={shlex.quote(str(code))} exec {shlex.quote(sys.executable)} "
                f"-m xeda.remote.launch_entry {shlex.quote(str(launch_dir))} {receiver.fileno()}"
            )
            child = subprocess.Popen(
                [bash, "-lc", script],
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=subprocess.STDOUT,
                cwd=launch_dir,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
                # Its tools do not inherit the locks: the entry makes them not inheritable, and
                # the launcher starts its processes with `close_fds`.
                pass_fds=(receiver.fileno(),),
            )
        except BaseException:
            receiver.close()
            raise
        finally:
            sender.close()
            os.close(descriptor)
        self._hand_over = receiver
        self._child = child
        self.keep_running = keep_running
        state: dict[str, Any] = {
            "id": launch_id,
            "sequence": sequence,
            "pid": child.pid,
            "pid_created": psutil.Process(child.pid).create_time(),
            "started": _now(),
            "state": "running",
            "keep_running": keep_running,
            "digest": self.digest,
        }
        _write_json(launch_dir / "state.json", state)
        tail = _LogTail(log, self.offer)
        try:
            canceled, survivors = self.supervise(child, tail)
            self.flush_log(tail)
        finally:
            tail.close()
        self.release_hand_over()  # a session that ends early does it in `close`
        # `xeda remote cancel` may have stopped the launch from another session.
        recorded = _read_json(launch_dir / "state.json")
        canceled = canceled or (isinstance(recorded, dict) and recorded.get("state") in _CANCELED)
        state.update(state="canceled" if canceled else "finished", exit=child.returncode)
        _write_json(launch_dir / "state.json", state)
        result = _read_result(launch_dir)
        self.result = result or {}
        self.send_json(
            {
                "result": result,
                "canceled": canceled,
                "exit": child.returncode,
                "survivors": survivors,
            }
        )

    def watch_descendants(self, child: _Child) -> None:
        """Remember the descendants of the launch process. Called at each tick of the loops that
        watch a launch; the look itself runs four times a second at most."""
        now = time.monotonic()
        if now - self._seen_at < _SEEN_EVERY:
            return
        self._seen_at = now
        try:
            self._seen.update(psutil.Process(child.pid).children(recursive=True))
        except psutil.Error:
            pass

    def supervise(self, child: "subprocess.Popen[bytes]", tail: _LogTail) -> tuple[bool, int]:
        """Stream the log until the child ends. Return whether the launch was canceled, and how
        many processes of it survived SIGKILL. Nothing here waits for the client to read."""
        while child.poll() is None:
            tail.pump()
            self.watch_descendants(child)
            if (reason := self.lost(launching=True)) is not None:
                if self.keep_running and not self.client.canceled.is_set():
                    raise _SessionEnded(reason, client_gone=True)  # the launch runs on
                return True, self.stop_launch(child, tail, reason)
            time.sleep(_TICK)
        return False, 0

    def flush_log(self, tail: _LogTail) -> None:
        """Send the rest of the log, while the client reads it."""
        while True:
            sent = tail.pump(final=True)
            if tail.finished() or self.out.failed():
                return
            if not sent:
                time.sleep(_TICK)

    def stop_launch(self, child: _Child, tail: Optional[_LogTail], reason: str) -> int:
        """Stop the child and every process that it started: SIGINT to the process group of the
        child, as Ctrl-C at a terminal does; after the first grace period SIGTERM to all of them; after the second SIGKILL to what is left. "All of
        them" is the process group of the child (it starts its own session), the descendants that
        were seen while it ran, and the descendants now. So a tool that its leader left behind
        is stopped too. The periods run on the clock of the agent: no send of a frame waits for
        them or delays them. Return the number of processes that are still there after SIGKILL
        and `_KILL_WAIT` seconds."""
        self.say(f"canceling the launch: {reason}")
        known = self._seen
        pgid = child.pid
        try:
            leader = psutil.Process(child.pid)
        except psutil.Error:
            leader = None

        def look() -> None:
            if leader is not None and child.poll() is None:
                try:
                    known.update(leader.children(recursive=True))
                except psutil.Error:
                    pass
            known.update(_group_members(pgid))

        def alive() -> list[psutil.Process]:
            found = {p for p in known if _running(p)}
            if leader is not None and child.poll() is None:
                found.add(leader)
            return list(found)

        def send(number: int) -> None:
            look()
            members = _group_members(pgid)
            if members:  # a group that has no member may be another one by now
                try:
                    os.killpg(pgid, number)
                except OSError:  # ProcessLookupError, or a group that is not ours
                    pass
            grouped = {process.pid for process in members}
            for process in alive():
                if process.pid not in grouped:  # the group has had its signal
                    _signal_process(process, number)

        def wait(seconds: float) -> bool:
            """Wait until nothing is left. Return whether that happened."""
            deadline = time.monotonic() + seconds
            while True:
                look()
                if tail is not None:
                    tail.pump()
                child.poll()
                if not alive():
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_TICK)

        look()
        if child.poll() is None:
            try:
                # As Ctrl-C at a terminal: xeda gets SIGINT, and stops its tools (each leads a
                # group of its own) as it stops them in a local run.
                os.killpg(pgid, signal.SIGINT)
            except OSError:
                pass
        else:
            send(signal.SIGINT)  # the leader is gone: nobody stops what it left
        if wait(self.grace[0]):
            return 0
        send(signal.SIGTERM)
        if wait(self.grace[1]):
            return 0
        send(signal.SIGKILL)
        if wait(_KILL_WAIT):
            return 0
        survivors = len(alive())
        self.say(f"{survivors} processes of the launch did not stop after SIGKILL")
        return survivors

    # -- attach and cancel: a launch that this session did not start

    def records(self) -> list[dict[str, Any]]:
        """The launch records of the workspace, in the order in which the launches ran: the
        oldest first."""
        return sorted(
            (
                state
                for state in _launch_states(self.workspace)
                if isinstance(state, dict) and isinstance(state.get("id"), str)
            ),
            key=_order,
        )

    def check_workspace(self, body: Any, what: str) -> Optional[str]:
        """Check the body of `attach` or `cancel`. Return the launch id that it names, or `None`."""
        if not isinstance(body, dict):
            raise RemoteProtocolError(f"`{what}` is not an object")
        if body.get("workspace") != self.key:
            raise RemoteProtocolError(
                f"the client names the workspace {body.get('workspace')!r}, and this agent "
                f"serves {self.key!r}"
            )
        wanted = body.get("id")
        if wanted is not None and not (isinstance(wanted, str) and _NAME.fullmatch(wanted)):
            raise RemoteProtocolError(f"{wanted!r} is not a launch id")
        return wanted

    def attach(self, body: Any) -> None:
        wanted = self.check_workspace(body, "attach")
        records = self.records()
        if wanted is not None:
            found = [state for state in records if state["id"] == wanted]
            if not found:
                raise RemoteError(f"This workspace has no launch {wanted}.")
            chosen = found[0]
        else:
            running = [state for state in records if _launch_running(state)]
            if not (running or records):
                raise RemoteError("This workspace has no launch.")
            chosen = (running or records)[-1]
        self.follow(chosen)

    def cancel(self, body: Any) -> None:
        wanted = self.check_workspace(body, "cancel")
        running = [
            state
            for state in self.records()
            if _launch_running(state) and wanted in (None, state["id"])
        ]
        if not running:
            raise RemoteError(
                f"Launch {wanted} does not run in this workspace."
                if wanted
                else "No launch runs in this workspace."
            )
        state = running[-1]
        launch_dir = self.workspace / "launches" / state["id"]
        # The session that supervises the launch, if it is still there, reads this mark.
        _update_state(launch_dir, state="canceling")
        self.send_json({"canceling": {"id": state["id"]}})
        survivors = self.stop_launch(_ProcessRef(state), None, "xeda remote cancel")
        _update_state(launch_dir, state="canceled")
        self.send_json({"stopped": {"id": state["id"], "survivors": survivors}})

    def follow(self, state: dict[str, Any]) -> None:
        """Send the log of a launch from its start, wait until the launch ends, and send its
        result, as the end of step 3 does. The launch is not a child of this session. A client
        that is gone leaves it running. A `C` frame cancels it. The lock is taken after the
        launch has ended, for the fetch."""
        launch_id = state["id"]
        launch_dir = self.workspace / "launches" / launch_id
        child = _ProcessRef(state)
        self.send_json(
            {
                "attached": {
                    "id": launch_id,
                    "started": state.get("started"),
                    "state": _launch_label(state, launch_dir),
                    "running": child.poll() is None,
                }
            }
        )
        request = self.read_request(launch_dir)
        try:
            tail = _LogTail(launch_dir / "log", self.offer)
        except OSError as error:
            raise RemoteError(f"The log of launch {launch_id} cannot be read: {error}") from None
        canceled, survivors = False, 0
        try:
            while child.poll() is None:
                tail.pump()
                self.watch_descendants(child)
                if (reason := self.lost(launching=True)) is not None:
                    if not self.client.canceled.is_set():
                        raise _SessionEnded(reason, client_gone=True)  # the launch runs on
                    _update_state(launch_dir, state="canceling")
                    canceled, survivors = True, self.stop_launch(child, tail, reason)
                    break
                time.sleep(_TICK)
            self.flush_log(tail)
        finally:
            tail.close()
        self.check_not_replaced(launch_id, request)
        if self._lock_fd is None:
            self.take_lock()
            self.check_not_replaced(launch_id, request)
        final = _read_json(launch_dir / "state.json")
        final = final if isinstance(final, dict) else {}
        canceled = canceled or final.get("state") in _CANCELED
        if canceled:
            _update_state(launch_dir, state="canceled")
        result = _read_result(launch_dir)
        self.request = request
        self.result = result or {}
        self.send_json(
            {
                "result": result,
                "canceled": canceled,
                "exit": final.get("exit"),
                "survivors": survivors,
            }
        )

    @staticmethod
    def read_request(launch_dir: Path) -> LaunchRequest:
        document = _read_json(launch_dir / REQUEST_FILE)
        try:
            return LaunchRequest.model_validate(document)
        except ValueError as error:
            raise RemoteProtocolError(f"{REQUEST_FILE} of the launch is damaged: {error}") from None

    def check_not_replaced(self, launch_id: str, request: LaunchRequest) -> None:
        """Refuse to fetch the files of a launch if a later launch ran in the same inputs: the run
        directories may hold the files of the later launch now."""
        records = self.records()
        mine = next((state for state in records if state["id"] == launch_id), None)
        for state in records:
            if mine is None or _order(state) <= _order(mine):
                continue
            later = _read_json(self.workspace / "launches" / state["id"] / REQUEST_FILE)
            if isinstance(later, dict) and later.get("inputs_dir") == request.inputs_dir:
                raise RemoteError(
                    f"Launch {state['id']} used the same inputs after launch {launch_id}, so the "
                    f"files of launch {launch_id} may be gone. Run the original command again: "
                    "it reuses what is still valid."
                )

    # -- step 4: the fetch

    def fetch(self, body: Any) -> None:
        from .fetch import NodeResult, select_fetch

        if not isinstance(body, dict) or not isinstance(body.get("fetch_all", False), bool):
            raise RemoteProtocolError("`fetch` is not an object with a boolean `fetch_all`")
        document = self.result if isinstance(self.result, dict) else {}
        request = self.request
        files: dict[str, Path] = {}
        skipped: list[Any] = []
        if request is not None and document.get("nodes"):
            named = {element.get("node") for element in document.get("request") or []}
            written = document.get("written") or {}
            nodes = []
            for node in document["nodes"]:
                name = node["node"]
                run_dir = Path(node["run_path"])
                results = _read_json(run_dir / "results.json")
                nodes.append(
                    NodeResult(
                        name,
                        run_dir,
                        results if isinstance(results, dict) else {},
                        name in named,
                        frozenset(written[name]) if name in written else None,
                    )
                )
            selection = select_fetch(
                Path(request.run_root), nodes, fetch_all=bool(body.get("fetch_all", False))
            )
            files, skipped = selection.files, selection.skipped
        entries = build_manifest(files)
        self.send_json(
            {
                "files": [entry.as_json_value() for entry in entries],
                "skipped": [source.as_json_value() for source in skipped],
            }
        )
        sink = io.BufferedWriter(_FrameSink(self.send), buffer_size=_DATA_FRAME)
        write_tar(entries, files, {}, sink)
        sink.flush()
        self.send(END)


# ---------------------------------------------------------------------------------------------
# check and clean


def _kill_user_processes() -> str:
    """The `KillUserProcesses` setting of systemd-logind for this user: `yes`, `no` or
    `unknown`. A session that ends kills even a launch that keeps running when it is `yes`."""
    try:
        done = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "--property=KillUserProcesses", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip() in ("yes", "no"):
            return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    value = "unknown"
    files = [Path("/etc/systemd/logind.conf")]
    for directory in ("/etc/systemd/logind.conf.d", "/usr/lib/systemd/logind.conf.d"):
        try:
            files += sorted(Path(directory).glob("*.conf"))
        except OSError:
            pass
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                name, _, setting = line.partition("=")
                if name.strip() == "KillUserProcesses" and setting.strip() in ("yes", "no"):
                    value = setting.strip()
        except OSError:
            continue
    return value


def _where_found(executables: Sequence[str]) -> tuple[Optional[str], dict[str, Optional[str]]]:
    """Ask a login shell where each executable is. Return the shell and the paths."""
    found: dict[str, Optional[str]] = {name: None for name in executables}
    bash = shutil.which("bash")
    if bash is None or not executables:
        return bash, found
    script = 'for n in "$@"; do p=$(command -v -- "$n") && printf "XEDA-FOUND\\t%s\\t%s\\n" "$n" "$p"; done'
    try:
        done = subprocess.run(
            [bash, "-lc", script, "xeda", *executables],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return bash, found
    for line in done.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0] == "XEDA-FOUND" and parts[1] in found:
            found[parts[1]] = parts[2]
    return bash, found


def check(executables: Sequence[str]) -> dict[str, Any]:
    """Describe the remote side: the Python, the environment, the disk, `KillUserProcesses` and
    where a login shell finds each executable."""
    prefix = Path(sys.prefix)
    lock: list[str] = []
    try:
        lock = (prefix / "xeda-lock.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    layout = len(prefix.parents) > 2 and prefix.parent.name == "env"
    root = prefix.parents[2] if layout and prefix.parents[1].name == LAYOUT else None
    where = root if root is not None and root.is_dir() else Path.home()
    usage = shutil.disk_usage(where)
    bash, found = _where_found(executables)
    return {
        "xeda": __version__,
        "python": {
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "executable": sys.executable,
        },
        "environment": {"prefix": str(prefix), "lock": lock},
        "disk": {"path": str(where), "free": usage.free, "total": usage.total},
        "kill_user_processes": _kill_user_processes(),
        "bash": bash,
        "executables": found,
    }


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


#: The name prefix of a directory that `clean` took out of use and is removing.
_REMOVING = ".removing-"


def _host_tag() -> str:
    """A short name of this host for the name of a directory that is being removed."""
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]


def _remove_directory(path: Path) -> None:
    """Remove an environment, a code copy or a workspace whose lock this process holds. First
    rename it to a hidden name, which no session uses: a session that waits for the lock of a
    workspace then finds that its lock file is not the one of the workspace, and starts a new
    workspace. Only then delete the tree. If the deletion fails, the hidden directory stays, and
    the next `clean` removes it."""
    hidden = path.with_name(f"{_REMOVING}{os.getpid()}-{_host_tag()}-{path.name}")
    os.rename(path, hidden)
    _remove_tree(hidden)


def _remove_leftovers(directory: Path) -> None:
    """Remove the directories that an earlier `clean` renamed and did not finish to delete, if
    that `clean` ran on this host and its process is gone. A `directory` that is a link is not
    entered: what it leads to is not Xeda's."""
    try:
        if stat.S_ISLNK(directory.lstat().st_mode):
            return
        names = [n for n in os.listdir(directory) if n.startswith(_REMOVING)]
    except OSError:
        return
    for name in names:
        parts = name[len(_REMOVING) :].split("-", 2)
        try:
            pid, host = int(parts[0]), parts[1]
        except (IndexError, ValueError):
            continue
        path = directory / name
        if host != _host_tag() or psutil.pid_exists(pid) or path.is_symlink():
            continue
        try:
            _remove_tree(path)
        except OSError:
            continue


def _newest(paths: Iterable[Path]) -> float:
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest


def _provisioning(root: Path) -> bool:
    """Whether a provisioning lock of this host has a live owner. The `owner` file of a lock
    holds the host name and the process id."""
    try:
        owners = list((root / LAYOUT / "locks").glob("*/owner"))
    except OSError:
        return False
    for owner in owners:
        try:
            host, pid = owner.read_text(encoding="utf-8").split()[:2]
            if host == socket.gethostname() and psutil.pid_exists(int(pid)):
                return True
        except (OSError, ValueError):
            continue
    return False


def clean(
    root: Path,
    older_than: float,
    everything: bool,
    remove: bool = True,
    only: Optional[Collection[str]] = None,
) -> dict[str, Any]:
    """Remove environments, code copies and workspaces that nobody uses.

    One rule serves all three kinds: a directory is in use while a process holds its lock file
    (`USE_LOCK` for an environment and a code copy, `lock` for a workspace; a workspace is also in
    use while a launch of it runs). `clean` takes the exclusive lock, and keeps it while it
    removes the directory, so that no session starts in a directory that goes. Every agent process
    holds the locks of its own environment and code copy (`_hold_use_locks`), `clean` included, so
    the environment that `clean` runs from is in use. Without `everything`, only what nobody
    touched for `older_than` days goes.

    A link is never entered: not as `env`, `code` or `ws`, and not as an entry. A lock that is a
    link or no regular file is never opened for more than a look. A directory without a lock file
    is kept: a workspace directory without `lock` is not a workspace yet (a session makes the
    directory and then the lock), and nobody can say that an environment or a code copy without
    one is unused. `clean` never makes a lock file. What cannot be examined or removed (an
    `OSError` or another error for one candidate) is listed in `kept` with the reason, and the
    rest goes on.

    Without `remove`, nothing is removed. The document then lists what a call with `remove` would
    remove under `removable`, in place of `removed`. Both calls examine every candidate in the
    same way.

    With `only`, the call looks at the entries with these paths and no others. A path in `only`
    goes through the same checks, so an entry that was used since a listing stays."""
    removed: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    base = root / LAYOUT
    limit = time.time() - older_than * 86400

    def keep(kind: str, path: Path, reason: str) -> None:
        kept.append({"kind": kind, "path": str(path), "reason": reason})

    def candidates_in(kind: str, directory: Path) -> list[Path]:
        try:
            if stat.S_ISLNK(directory.lstat().st_mode):
                keep(kind, directory, "the directory is a link")
                return []
            # A name that starts with a dot is a directory that provisioning fills (`code/.<key>
            # .<host>.<pid>`) or that a `clean` removes (`_REMOVING`): neither is a candidate.
            names = sorted(p for p in directory.iterdir() if not p.name.startswith("."))
            return [p for p in names if only is None or str(p) in only]
        except FileNotFoundError:
            return []
        except OSError as error:
            keep(kind, directory, f"cannot be listed: {error}")
            return []

    def old_enough(newest: float) -> bool:
        return everything or newest < limit

    def lock_exclusively(
        kind: str, path: Path, name: str, held: str, missing: str
    ) -> Optional[int]:
        """Take the exclusive lock on the lock file `name` of the candidate `path`, and return
        the open file. Return `None`, after the reason is recorded, if the candidate stays: the
        reason is `held` for a lock that another process holds, and `missing` for no lock file."""
        descriptor: Optional[int] = None
        try:
            try:
                # Not `O_CREAT`; a link is refused; a FIFO does not block the open. Open for
                # writing: on Linux NFS, an exclusive flock needs it.
                descriptor = os.open(path / name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                keep(kind, path, missing)
                return None
            except OSError as error:
                if error.errno != errno.ELOOP:
                    raise
                keep(kind, path, "the lock is a link")
                return None
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                keep(kind, path, "the lock is not a regular file")
            else:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    keep(kind, path, held)
                else:
                    # Another `clean` may have removed the directory, and provisioning made it
                    # again, since the open: then this is the lock of a file that is gone.
                    if _is_file_at(descriptor, path / name):
                        return descriptor
                    keep(kind, path, "it changed while clean looked at it")
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        os.close(descriptor)
        return None

    if remove:
        for kind in ("env", "code", "ws"):
            _remove_leftovers(base / kind)
    busy = _provisioning(root)
    for kind in ("env", "code"):
        for path in candidates_in(kind, base / kind):
            descriptor: Optional[int] = None
            try:
                if path.is_symlink() or not path.is_dir():
                    continue
                if busy:
                    keep(kind, path, "provisioning runs")
                    continue
                descriptor = lock_exclusively(
                    kind, path, USE_LOCK, "in use", "it has no lock file, so it may be in use"
                )
                if descriptor is None:
                    continue
                if not old_enough(_newest([path, path / "last-use"])):
                    keep(kind, path, "recently used")
                else:
                    if remove:
                        _remove_directory(path)
                    removed.append({"kind": kind, "path": str(path)})
            except Exception as error:  # pylint: disable=broad-except
                keep(kind, path, f"error: {type(error).__name__}: {error}")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    for path in candidates_in("workspace", base / "ws"):
        descriptor = None
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            descriptor = lock_exclusively(
                "workspace", path, "lock", "locked", "it has no lock file, so it is not a workspace"
            )
            if descriptor is None:
                continue
            if any(_launch_running(state) for state in _launch_states(path)):
                keep("workspace", path, "a launch runs")
            elif not old_enough(
                _newest(
                    [path, path / "lock", path / "inputs", path / "launches", path / "xeda_run"]
                )
            ):
                keep("workspace", path, "recently used")
            else:
                if remove:
                    _remove_directory(path)
                removed.append({"kind": "workspace", "path": str(path)})
        except Exception as error:  # pylint: disable=broad-except
            keep("workspace", path, f"error: {type(error).__name__}: {error}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return {"removed" if remove else "removable": removed, "kept": kept}


# ---------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m xeda.remote.agent")
    commands = parser.add_subparsers(dest="command", required=True)
    session = commands.add_parser("session", help="serve one launch over stdin and stdout")
    session.add_argument("root")
    session.add_argument("key")
    # Test-only: shorter periods than the constants. A longer one is refused.
    session.add_argument(
        "--lease", type=lambda t: _limit(t, LEASE, "--lease"), default=LEASE, help=argparse.SUPPRESS
    )
    session.add_argument("--grace", type=_grace, default=CANCEL_GRACE, help=argparse.SUPPRESS)
    session.add_argument(
        "--lock-notice",
        type=lambda t: _limit(t, LOCK_NOTICE, "--lock-notice"),
        default=LOCK_NOTICE,
        help=argparse.SUPPRESS,
    )
    check_command = commands.add_parser("check", help="describe the remote side")
    check_command.add_argument("--json", action="store_true", help="accepted; the output is JSON")
    check_command.add_argument("executables", nargs="*")
    clean_command = commands.add_parser("clean", help="remove what nobody uses")
    clean_command.add_argument("root")
    clean_command.add_argument("--older-than", type=float, metavar="DAYS", default=_DEFAULT_DAYS)
    clean_command.add_argument("--all", action="store_true", dest="everything")
    clean_command.add_argument(
        "--only",
        action="append",
        metavar="PATH",
        help="look at this entry and no other (repeat for more); it still must pass every check",
    )
    clean_command.add_argument(
        "--list", action="store_true", dest="list_only", help="remove nothing; list what would go"
    )
    return parser


def _announce() -> BinaryIO:
    """Write `MAGIC` to the real stdout, and send everything else that is printed to stderr.
    Return the real stdout."""
    protocol = sys.stdout.buffer
    protocol.write(MAGIC)
    protocol.flush()
    sys.stdout = sys.stderr
    return protocol


def _error_document(error: BaseException) -> dict[str, Any]:
    return {"type": type(error).__name__, "message": str(error)}


def _refuse(arguments: argparse.Namespace, protocol: BinaryIO, error: BaseException) -> int:
    """Report that the agent cannot hold the locks of its environment and code copy, in the form
    of the command, and return the exit status."""
    print(f"xeda agent: {type(error).__name__}: {error}", file=sys.stderr)
    document: dict[str, Any] = {"error": _error_document(error)}
    if arguments.command == "session":
        FrameWriter(cast(BinaryIO, _FdWriter(protocol.fileno()))).send_json(document)
        return 1
    if arguments.command == "clean":
        key = "removable" if arguments.list_only else "removed"
        document = {key: [], "kept": [], **document}
    protocol.write(json.dumps(document).encode("utf-8") + b"\n")
    protocol.flush()
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    protocol = _announce()
    try:
        use_locks = _hold_use_locks()
    except Exception as refused:  # pylint: disable=broad-except
        return _refuse(arguments, protocol, refused)
    status = 0
    if arguments.command == "check":
        document: Any = check(arguments.executables)
    elif arguments.command == "clean":
        key = "removable" if arguments.list_only else "removed"
        if not os.path.isabs(arguments.root):
            error = RemoteProtocolError("the root must be an absolute path")
            print(f"xeda agent: {error}", file=sys.stderr)
            document, status = {key: [], "kept": [], "error": _error_document(error)}, 2
        else:
            try:
                document = clean(
                    Path(arguments.root),
                    arguments.older_than,
                    arguments.everything,
                    not arguments.list_only,
                    arguments.only,
                )
            except Exception as error:  # pylint: disable=broad-except
                print(f"xeda agent: {type(error).__name__}: {error}", file=sys.stderr)
                document, status = {key: [], "kept": [], "error": _error_document(error)}, 1
    else:
        return _serve(arguments, protocol, use_locks)
    protocol.write(json.dumps(document).encode("utf-8") + b"\n")
    protocol.flush()
    return status


def _serve(arguments: argparse.Namespace, protocol: BinaryIO, use_locks: Sequence[int]) -> int:
    writer = FrameWriter(cast(BinaryIO, _FdWriter(protocol.fileno())))
    if not os.path.isabs(arguments.root):
        error = RemoteProtocolError(f"the root {arguments.root!r} is not an absolute path")
        writer.send_json({"error": _error_document(error)})
        print(f"xeda agent: {error}", file=sys.stderr)
        return 2
    # An unbuffered file: the reader thread may be blocked in a read when the process exits, and a
    # buffered reader that a thread holds makes the interpreter abort at shutdown.
    client = _Client(cast(BinaryIO, io.FileIO(sys.stdin.fileno(), "rb", closefd=False)))
    out = _Output(writer, arguments.lease)
    try:
        session = Session(
            Path(arguments.root),
            arguments.key,
            client,
            out,
            lease=arguments.lease,
            grace=arguments.grace,
            lock_notice=arguments.lock_notice,
            use_locks=use_locks,
        )
    except RemoteProtocolError as error:
        writer.send_json({"error": _error_document(error)})
        print(f"xeda agent: {error}", file=sys.stderr)
        return 1

    def on_signal(number: int, _frame: Any) -> None:
        session.signal_name = signal.Signals(number).name

    # Handlers (not SIG_IGN) also make the child start with these signals at their default.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, on_signal)
    out.start()
    client.start()
    try:
        return session.run()
    finally:
        out.close()
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
