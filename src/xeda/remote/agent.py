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
   "exit": n}`. While the launch runs, the client sends `H` frames, and `C` to cancel it.
4. client: `J {"fetch": {"fetch_all": bool}}` (or none, for a dry run). The agent answers
   `J {"files": [entries]}` and then the tar of these files as `DATA` frames and an `END` frame.
5. client: `J {"bye": true}`. The agent releases the lock and exits with status 0.

At any time the agent can send `J {"error": {"type": name, "message": text}}` and exit with
status 1. If the client cancels (`C`) or goes away before the launch starts, the agent sends
`J {"canceled": true, "reason": text}` where it can, and exits with status 0.

The client is gone when its stream ends, when it sends no frame for the lease after it began to
send `H` frames, and (for a running launch) when it sends no frame for the lease at all. The
agent cancels a running launch in that case, as for a `C` frame: SIGINT to the launch, SIGTERM
to it and to its descendants after the first grace period, SIGKILL to what is left after the
second. The result is sent only after all of them are gone.

`check [--json] <executables...>` and `clean <root> [--older-than DAYS] [--all]` write `MAGIC` and
then one JSON document and a newline.
"""

import argparse
import fcntl
import io
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional, cast

import psutil

from ..utils import replacing_file
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
from .launch_entry import REQUEST_FILE, RESULT_FILE, LaunchRequest
from .manifest import Entry, apply_sync, build_manifest, plan_sync, safe_member_path, write_tar

#: Seconds between two `L` lines that say the agent waits for the workspace lock.
LOCK_NOTICE = 10.0

#: Seconds between two looks at the child and the client while a launch runs.
_TICK = 0.05

#: Seconds to wait for processes to be gone after SIGKILL.
_KILL_WAIT = 10.0

#: A launch id and a workspace key: one plain name, never a path.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: The `clean` default: remove what nobody used for this many days.
_DEFAULT_DAYS = 30.0

_CHUNK = 1 << 20


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
        for start in range(0, len(view), _CHUNK):
            self._send(DATA, bytes(view[start : start + _CHUNK]))
        return len(view)


# ---------------------------------------------------------------------------------------------
# The processes of a launch


def _running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _signal_process(process: psutil.Process, number: int) -> None:
    try:
        process.send_signal(number)  # psutil checks that the process is still the same one
    except psutil.Error:
        pass


class _LogTail:
    """Send the lines of a growing log file as `L` frames."""

    def __init__(self, path: Path, send: Callable[[bytes, bytes], None]) -> None:
        self._file = path.open("rb")
        self._send = send
        self._rest = b""

    def pump(self, final: bool = False) -> None:
        """Send every line that is complete now. With `final`, send the incomplete last line too."""
        while data := self._file.read(1 << 16):
            *lines, self._rest = (self._rest + data).split(b"\n")
            for line in lines:
                self._send(LOG, line)
            if len(self._rest) > _CHUNK:
                self._send(LOG, self._rest)
                self._rest = b""
        if final and self._rest:
            self._send(LOG, self._rest)
            self._rest = b""

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


def _launch_running(state: Any) -> bool:
    """Whether the launch that `state.json` describes still runs: its process is alive and has
    the start time that was recorded (a reused process id is another process)."""
    if not isinstance(state, dict) or state.get("state") != "running":
        return False
    try:
        process = psutil.Process(int(state["pid"]))
        created = float(state["pid_created"])
        return _running(process) and abs(process.create_time() - created) < 1.0
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


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
        out: FrameWriter,
        *,
        lease: float = LEASE,
        grace: tuple[float, float] = CANCEL_GRACE,
        lock_notice: float = LOCK_NOTICE,
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
        self.broken = False
        self.request: Optional[LaunchRequest] = None
        self.design = ""
        self.result: Any = None
        self._lock_fd: Optional[int] = None
        self._child: Optional["subprocess.Popen[bytes]"] = None

    # -- sending

    def send(self, kind: bytes, payload: bytes = b"") -> None:
        """Send a frame. A write that fails marks the connection as broken: the session goes on
        to its end and sends nothing more."""
        if self.broken:
            return
        try:
            self.out.send(kind, payload)
        except (OSError, RemoteProtocolError, ValueError):
            self.broken = True

    def send_json(self, document: Any) -> None:
        if self.broken:
            return
        try:
            self.out.send_json(document)
        except (OSError, RemoteProtocolError, ValueError):
            self.broken = True

    def say(self, text: str) -> None:
        self.send(LOG, f"xeda agent: {text}".encode())

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
        if self.broken:
            return "the connection to the client failed"
        if self.client.gone.is_set():
            return "the connection to the client ended"
        return None

    def take(self) -> tuple[bytes, bytes]:
        """The next frame of the client that is not a heartbeat or a cancel request."""
        while True:
            reason = self.lost(launching=False)
            if reason and not self.client.gone.is_set():
                raise _SessionEnded(reason, client_gone=self.broken)
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
        self.send_json({"hello": {"xeda": __version__, "pid": os.getpid()}})
        try:
            message = self.take_json()
            if "bye" in message:
                return 0
            self.sync(message)
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
                self.send_json({"canceled": True, "reason": ended.reason})
            return 0
        except Exception as error:  # pylint: disable=broad-except
            self.send_json({"error": {"type": type(error).__name__, "message": str(error)}})
            print(f"xeda agent: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        finally:
            self.close()

    def close(self) -> None:
        """Stop a launch that still runs, and release the lock."""
        child = self._child
        if child is not None and child.poll() is None:
            self.stop_launch(child, None, "the session ends")
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    # -- step 2: the lock and the sync

    def holder(self) -> str:
        """Who holds the workspace lock, as far as the launch records say."""
        try:
            for state_file in sorted((self.workspace / "launches").glob("*/state.json")):
                state = _read_json(state_file)
                if _launch_running(state):
                    return f"launch {state['id']} (since {state['started']})"
        except OSError:
            pass
        return "another session"

    def take_lock(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.workspace / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._lock_fd = descriptor
        next_notice = 0.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pass
            if reason := self.lost(launching=False):
                raise _SessionEnded(reason, client_gone=self.client.gone.is_set() or self.broken)
            if time.monotonic() >= next_notice:
                self.send(LOG, f"waiting for the workspace lock held by {self.holder()}".encode())
                next_notice = time.monotonic() + self.lock_notice
            time.sleep(_TICK)
        os.utime(descriptor)  # the age of the workspace, for `clean`

    def sync(self, message: dict[str, Any]) -> None:
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
        self.take_lock()
        root = self.workspace / "inputs" / design
        plan = plan_sync(root, entries)
        self.send_json({"need": plan.missing})
        source = io.BufferedReader(_TarSource(self.take))
        apply_sync(root, entries, plan, source, directories=directories)
        self.send_json({"synced": {"written": len(plan.missing), "removed": len(plan.stale)}})

    # -- step 3: the launch

    def check_paths(self, request: LaunchRequest) -> None:
        """Refuse a launch outside the workspace that the session holds: its inputs must be in the
        design directory that the sync filled, and its run root must be the one of the workspace."""
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
        except ValueError as error:
            raise RemoteProtocolError(f"`launch` is not a launch request: {error}") from None
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
        launch_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            launch_dir.mkdir()
        except FileExistsError:
            raise RemoteProtocolError(f"the launch {launch_id} exists already") from None
        _write_json(launch_dir / REQUEST_FILE, request.model_dump(mode="json"))
        code = Path(__file__).resolve().parent.parent.parent
        script = "".join(f"{line}\n" for line in setup) + (
            f"PYTHONPATH={shlex.quote(str(code))} exec {shlex.quote(sys.executable)} "
            f"-m xeda.remote.launch_entry {shlex.quote(str(launch_dir))}"
        )
        log = launch_dir / "log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            child = subprocess.Popen(
                [bash, "-lc", script],
                stdin=subprocess.DEVNULL,
                stdout=descriptor,
                stderr=subprocess.STDOUT,
                cwd=launch_dir,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
            )
        finally:
            os.close(descriptor)
        self._child = child
        state: dict[str, Any] = {
            "id": launch_id,
            "pid": child.pid,
            "pid_created": psutil.Process(child.pid).create_time(),
            "started": _now(),
            "state": "running",
        }
        _write_json(launch_dir / "state.json", state)
        tail = _LogTail(log, self.send)
        try:
            canceled = self.supervise(child, tail)
            tail.pump(final=True)
        finally:
            tail.close()
        state.update(state="canceled" if canceled else "finished", exit=child.returncode)
        _write_json(launch_dir / "state.json", state)
        self.result = _read_json(launch_dir / RESULT_FILE) or {}
        self.send_json(
            {
                "result": _read_json(launch_dir / RESULT_FILE),
                "canceled": canceled,
                "exit": child.returncode,
            }
        )

    def supervise(self, child: "subprocess.Popen[bytes]", tail: _LogTail) -> bool:
        """Stream the log until the child ends. Return whether the launch was canceled."""
        while child.poll() is None:
            tail.pump()
            if (reason := self.lost(launching=True)) is not None:
                self.stop_launch(child, tail, reason)
                return True
            time.sleep(_TICK)
        return False

    def stop_launch(
        self, child: "subprocess.Popen[bytes]", tail: Optional[_LogTail], reason: str
    ) -> None:
        """Stop the child and every process that it started: SIGINT to the child; after the first
        grace period SIGTERM to all of them; after the second SIGKILL to what is left. Return
        when none is left."""
        self.say(f"canceling the launch: {reason}")
        known: set[psutil.Process] = set()
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

        def alive() -> list[psutil.Process]:
            found = [p for p in known if _running(p)]
            if leader is not None and child.poll() is None:
                found.append(leader)
            return found

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
                os.kill(child.pid, signal.SIGINT)
            except OSError:
                pass
        if wait(self.grace[0]):
            return
        look()
        for process in alive():
            _signal_process(process, signal.SIGTERM)
        if wait(self.grace[1]):
            return
        look()
        for process in alive():
            _signal_process(process, signal.SIGKILL)
        if not wait(_KILL_WAIT):
            self.say(f"{len(alive())} processes of the launch did not stop after SIGKILL")

    # -- step 4: the fetch

    def fetch(self, body: Any) -> None:
        from .fetch import NodeResult, select_fetch

        if not isinstance(body, dict) or not isinstance(body.get("fetch_all", False), bool):
            raise RemoteProtocolError("`fetch` is not an object with a boolean `fetch_all`")
        document = self.result if isinstance(self.result, dict) else {}
        request = self.request
        files: dict[str, Path] = {}
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
            files = select_fetch(
                Path(request.run_root), nodes, fetch_all=bool(body.get("fetch_all", False))
            )
        entries = build_manifest(files)
        self.send_json({"files": [entry.as_json_value() for entry in entries]})
        sink = io.BufferedWriter(_FrameSink(self.send), buffer_size=_CHUNK)
        write_tar(entries, files, {}, sink)
        sink.flush()
        self.send(END)


# ---------------------------------------------------------------------------------------------
# check and clean


def _kill_user_processes() -> str:
    """The `KillUserProcesses` setting of systemd-logind for this user: `yes`, `no` or
    `unknown`. A session that ends kills even a detached launch when it is `yes`."""
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


def _newest(paths: Iterable[Path]) -> float:
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest


def _strings_of_processes() -> list[str]:
    """The paths that other processes of this user are started from: their program, their
    command line and their `PYTHONPATH`. An environment or a code copy named here is in use."""
    found: list[str] = []
    for process in psutil.process_iter():
        try:
            found.append(process.exe())
            found.extend(process.cmdline())
            found.extend(process.environ().get("PYTHONPATH", "").split(os.pathsep))
        except psutil.Error:
            continue
    return [text for text in found if text]


def _uses(strings: Iterable[str], directory: Path) -> bool:
    names = {str(directory), os.path.realpath(directory)}
    return any(text == name or text.startswith(name + os.sep) for text in strings for name in names)


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


def clean(root: Path, older_than: float, everything: bool) -> dict[str, Any]:
    """Remove environments, code copies and workspaces that nobody uses.

    A workspace is in use while its lock is held or a launch of it runs. An environment or a code
    copy is in use while a process runs from it, and the ones this process runs from are always
    in use. Without `everything`, only what nobody touched for `older_than` days goes."""
    removed: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    base = root / LAYOUT
    limit = time.time() - older_than * 86400
    strings = _strings_of_processes() + [sys.prefix, *sys.path]
    own = [Path(sys.prefix), Path(__file__).resolve().parent.parent.parent]

    def old_enough(newest: float) -> bool:
        return everything or newest < limit

    busy = _provisioning(root)
    for kind in ("env", "code"):
        directory = base / kind
        try:
            candidates = sorted(directory.iterdir())
        except OSError:
            continue
        for path in candidates:
            if path.is_symlink() or not path.is_dir():
                continue
            if busy:
                kept.append({"kind": kind, "path": str(path), "reason": "provisioning runs"})
            elif any(os.path.realpath(path) == os.path.realpath(p) for p in own) or _uses(
                strings, path
            ):
                kept.append({"kind": kind, "path": str(path), "reason": "in use"})
            elif not old_enough(_newest([path, path / "last-use"])):
                kept.append({"kind": kind, "path": str(path), "reason": "recently used"})
            else:
                _remove_tree(path)
                removed.append({"kind": kind, "path": str(path)})
    workspaces = base / "ws"
    try:
        candidates = sorted(workspaces.iterdir())
    except OSError:
        candidates = []
    for path in candidates:
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            lock: Optional[BinaryIO] = open(path / "lock", "rb")
        except OSError:
            lock = None
        try:
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    kept.append({"kind": "workspace", "path": str(path), "reason": "locked"})
                    continue
            if any(
                _launch_running(_read_json(state))
                for state in (path / "launches").glob("*/state.json")
            ):
                kept.append({"kind": "workspace", "path": str(path), "reason": "a launch runs"})
            elif not old_enough(
                _newest(
                    [path, path / "lock", path / "inputs", path / "launches", path / "xeda_run"]
                )
            ):
                kept.append({"kind": "workspace", "path": str(path), "reason": "recently used"})
            else:
                _remove_tree(path)
                removed.append({"kind": "workspace", "path": str(path)})
        finally:
            if lock is not None:
                lock.close()
    return {"removed": removed, "kept": kept}


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
    return parser


def _announce() -> BinaryIO:
    """Write `MAGIC` to the real stdout, and send everything else that is printed to stderr.
    Return the real stdout."""
    protocol = sys.stdout.buffer
    protocol.write(MAGIC)
    protocol.flush()
    sys.stdout = sys.stderr
    return protocol


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    protocol = _announce()
    if arguments.command == "check":
        document: Any = check(arguments.executables)
    elif arguments.command == "clean":
        if not os.path.isabs(arguments.root):
            print("xeda agent: the root must be an absolute path", file=sys.stderr)
            return 2
        document = clean(Path(arguments.root), arguments.older_than, arguments.everything)
    else:
        return _serve(arguments, protocol)
    protocol.write(json.dumps(document).encode("utf-8") + b"\n")
    protocol.flush()
    return 0


def _serve(arguments: argparse.Namespace, protocol: BinaryIO) -> int:
    if not os.path.isabs(arguments.root):
        print("xeda agent: the root must be an absolute path", file=sys.stderr)
        return 2
    # An unbuffered file: the reader thread may be blocked in a read when the process exits, and a
    # buffered reader that a thread holds makes the interpreter abort at shutdown.
    client = _Client(cast(BinaryIO, io.FileIO(sys.stdin.fileno(), "rb", closefd=False)))
    out = FrameWriter(protocol)
    try:
        session = Session(
            Path(arguments.root),
            arguments.key,
            client,
            out,
            lease=arguments.lease,
            grace=arguments.grace,
            lock_notice=arguments.lock_notice,
        )
    except RemoteProtocolError as error:
        out.send_json({"error": {"type": type(error).__name__, "message": str(error)}})
        return 1

    def on_signal(number: int, _frame: Any) -> None:
        session.signal_name = signal.Signals(number).name

    # Handlers (not SIG_IGN) also make the child start with these signals at their default.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, on_signal)
    client.start()
    try:
        return session.run()
    finally:
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
