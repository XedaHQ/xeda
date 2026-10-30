"""The client of a remote run: `run_remote` runs one `Spec` on a Linux host over SSH.

The steps, in order:

1. Resolve the destination. Plan the request with the normal launcher
   (`FlowLauncher.prepare_request`), and refuse what a remote run cannot do. Nothing is
   connected yet, so a mistake in the request shows before any connection exists.
2. Open one SSH master connection, probe the host, and provision the environment and the code
   snapshot (`provision`).
3. Start the agent (`xeda.remote.agent`) and sync the inputs by digest.
4. Launch under the agent. The client sends heartbeats, shows the log, and sends a cancel request
   on Ctrl-C. A second Ctrl-C closes the connection.
5. Fetch the selected files into the mirror `<run root>/@<host>/...`, rewrite the paths of the
   result to the mirror, deliver `outputs_to`, and say goodbye to the agent.

The files of the mirror are kept as the remote wrote them and as their digests check, except the
records of each run directory. `results.json` and `settings.json` are written again with the paths
of this side: a path in the mirror, or the file of the local design for a path in the inputs of the
remote. So a user who opens the mirror sees no path of the remote host there.

`run_remote` returns a `Result` for every launch that started on the host, a failure of this side
after that too: it is a launch-level error of the `Result`. A failure before the launch started
raises a public error: a `SpecError` for a request that is invalid, and a `RemoteError` when the
host is unreachable or cannot be set up. `plan_remote` asks the host to plan the request and
returns a `Plan`. It writes no mirror.
"""

import copy
import hashlib
import io
import json
import logging
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import contextvars
import threading
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Deque, Dict, List, Optional, Tuple, cast

from pydantic import ValidationError

from ..deliver import (
    Conflict,
    Delivered,
    Deliveries,
    DeliveryError,
    deliverable_locations,
    outputs_to_deliveries,
    overwrite_question,
    recorded_artifacts,
    split_deliveries,
)
from ..design import generator_of
from ..plan import Plan, PlanDelivery, delivery_key, plan_from_dict, planned_destination
from ..flow_runner import DefaultRunner, launch_generation
from ..flow_runner.dependencies import DEPENDENCIES_DIRECTORY
from ..flow_runner.chains import parse_request
from ..errors import RecordError, SettingsError, SpecError
from ..errors import RemoteError as PublicRemoteError
from ..events import Event
from ..flow_runner.default_runner import PreparedRequest
from ..flow_runner.launch_options import LaunchOptions, WorkspaceOptions
from ..flow_runner.launch_result import _Progress  # pylint: disable=protected-access
from ..flow_runner.run_lock import LockRequest, LockSet, acquire_all
from ..frozen import thaw
from ..outcomes import (
    Disposition,
    NodeResult,
    Outcome,
    Result,
    aggregate,
    node_document,
    result_from_dict,
    run_document,
    spec_document,
)
from ..cancel import CancelToken, launch_canceled, never_canceled, observing
from ..proc_utils import CANCEL_POLL, TOOL_OUTPUT, tool_output_stream
from ..records import CANCELED, ErrorInfo, decode_record
from ..run_dir import RunDirectory, refuse_link_on_way
from ..remote_value import Remote
from ..spec import Spec
from ..utils import Canceled, logs_are_detailed
from .constants import HEARTBEAT, LAYOUT
from .environment import CodeSnapshot, DependencySpec, code_snapshot, dependency_spec
from .errors import (
    RemoteCanceled,
    RemoteConfigError,
    RemoteConnectionError,
    RemoteDetached,
    RemoteError,
    RemoteFetchError,
    RemoteInterrupted,
    RemoteProtocolError,
    RemoteRefused,
)
from .fetch import (
    SkippedSource,
    check_fetched_outputs,
    deliver_fetched,
    only_fetched_artifacts,
    publish_mirror,
    remote_read_inputs,
    rewrite_records,
    translate_paths,
)
from .frames import (
    BEAT,
    CANCEL,
    JSON,
    LOG,
    DataReader,
    FrameWriter,
    decode_json,
    read_frame,
    skip_to_magic,
)
from .launch_entry import LaunchRequest
from .launch_record import LaunchRecord, write_record
from .manifest import Entry, build_manifest, write_tar
from .packaging import (
    InputPackage,
    package_inputs,
    refuse_unsendable_declared,
    refuse_unsendable_generator,
)
from .profile import HostProfile
from .provision import Probe, Provisioned, agent_command, ensure_environment, probe
from .ssh import Destination, SshSession, resolve_destination

log = logging.getLogger(__name__)

#: Seconds that a finished session may take to end its agent before the client stops it.
_BYE_WAIT = 30.0

#: Seconds to wait for the status of a client that lost its connection.
_STATUS_WAIT = 10.0

#: Lines of the standard error of the agent that go into an error message.
_STDERR_LINES = 15

#: The directories of the inputs that hold copies which xeda made, and no file of the design.
_SYNTHETIC_INPUTS = frozenset({"_setting_inputs", "_liberty_inputs"})

#: The steps after the launch has finished, as a message names them.
_FETCHING = "fetching the results"
_DELIVERING = "delivering the outputs"

#: The words of a message of `read_frame` that tell the stream ended in a frame.
_ENDED_INSIDE = "the stream ended inside"


# --- Names ------------------------------------------------------------------------------------


def machine_id() -> str:
    """The identity of this machine: `/etc/machine-id`, else the hardware UUID of a Mac, else
    the host name."""
    try:
        text = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        if text:
            return text
    except (OSError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            done = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                check=False,
            )
            for line in done.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[1].strip().strip('"')
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return socket.gethostname()


def workspace_key(local_run_root: Path, destination: Destination) -> str:
    """The key of the workspace on the remote: 24 hex characters over the machine id, the
    resolved local run root and `user@hostname:port` as `ssh -G` gives them."""
    text = "\0".join(
        [
            machine_id(),
            os.path.realpath(local_run_root),
            f"{destination.user}@{destination.hostname}:{destination.port}",
        ]
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def new_launch_id() -> str:
    """A launch id: the UTC time as `yymmddHHMMSS`, a dash and 8 random hex characters."""
    return datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + "-" + secrets.token_hex(4)


def _is_windows() -> bool:
    return sys.platform == "win32"


# --- The agent --------------------------------------------------------------------------------


class _Agent:
    """The agent process on the remote host, as the client sees it: frames in and out.

    The heartbeat thread sends `H` frames, and also `C` when `interrupt` was called. The thread
    does this, and the signal handler does not, because the handler runs in the main thread, and
    the main thread may hold the lock of the frame writer when the signal comes."""

    def __init__(self, session: SshSession, command: str) -> None:
        self.host = session.destination.text
        self._session = session
        self.process = session.popen(command)
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.stdout = cast(BinaryIO, self.process.stdout)
        self.writer = FrameWriter(cast(BinaryIO, self.process.stdin))
        #: Whether a launch runs: log lines are tool output, and a lost connection is a cancel.
        self.launching = False
        #: Set by the first Ctrl-C during the launch.
        self.canceled = threading.Event()
        #: Whether a `CancelToken`, and not Ctrl-C, canceled the launch.
        self.by_request = False
        #: Set by the second Ctrl-C: the connection is closed.
        self.closed = False
        #: Whether the launch keeps running: Ctrl-C and a lost connection only stop the local
        #: follow, and the launch runs on.
        self.keep_running = False
        #: The launch that runs, for the messages of a run with `--keep-running`.
        self.launch_id = ""
        #: The text after `xeda remote attach HOST` that names the run root, if it is not the
        #: default one.
        self.attach_options = ""
        self._stderr: Deque[str] = deque(maxlen=_STDERR_LINES)
        self._drain_thread = threading.Thread(
            target=contextvars.copy_context().run, args=(self._drain,), daemon=True
        )
        self._drain_thread.start()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._beats: Optional[threading.Thread] = None

    def _drain(self) -> None:
        assert self.process.stderr is not None
        for raw in self.process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            log.debug("remote stderr: %s", line)
            if line:
                self._stderr.append(line)

    def stderr_text(self) -> str:
        return "\n".join(self._stderr)

    # -- heartbeat and cancel

    def start_heartbeat(self) -> None:
        self._beats = threading.Thread(
            target=contextvars.copy_context().run, args=(self._beat,), name="heartbeat", daemon=True
        )
        self._beats.start()

    def _beat(self) -> None:
        cancel_sent = False
        while not self._stop.is_set():
            try:
                if self.canceled.is_set() and not cancel_sent:
                    self.writer.send(CANCEL)
                    cancel_sent = True
                else:
                    self.writer.send(BEAT)
            except (OSError, RemoteProtocolError, ValueError):
                return  # the main thread finds the lost connection when it reads
            self._wake.wait(HEARTBEAT)
            self._wake.clear()

    def interrupt(self, *, quiet: bool = False) -> None:
        """The reaction to Ctrl-C during the launch, and to a `CancelToken` (`quiet`: the library
        says nothing). It runs in a signal handler: it takes no lock that the main thread may
        hold, and it does not log."""
        if self.keep_running:
            if not self.closed:
                self.closed = True
                if not quiet:
                    os.write(2, f"\nLeaving the launch on {self.host} running.\n".encode())
                try:
                    self.process.kill()  # the agent sees the end of its input and leaves
                except OSError:
                    pass
        elif not self.canceled.is_set():
            self.canceled.set()
            self._wake.set()
            self.by_request = quiet
            if not quiet:
                os.write(
                    2,
                    f"\nCanceling the launch on {self.host}. Press Ctrl-C again to close the "
                    "connection.\n".encode(),
                )
        elif not self.closed:
            self.closed = True
            try:
                self.process.kill()
            except OSError:
                pass

    def drop(self) -> None:
        """Close the connection now: a read that waits for the agent ends. It is safe in any
        thread."""
        if not self.closed:
            self.closed = True
            try:
                self.process.kill()
            except OSError:
                pass

    # -- receiving

    def emit(self, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace")
        if self.launching and TOOL_OUTPUT.get() == "terminal":
            stream = tool_output_stream()
            stream.write(text + "\n")
            stream.flush()
        else:
            log.info("remote: %s", text)

    def lost(self, step: str) -> RemoteError:
        """The error for a connection that ended while the client waited for `step`."""
        try:
            status: Optional[int] = self.process.wait(timeout=_STATUS_WAIT)
        except subprocess.TimeoutExpired:
            status = None
        self._drain_thread.join(timeout=2)
        detail = f"\nOutput of the remote host:\n{self.stderr_text()}" if self._stderr else ""
        code = f" (ssh exit status {status})" if status is not None else ""
        if self.keep_running and self.launching:
            return self.detached_error(step, code, detail)
        if self.closed:
            return RemoteCanceled(
                f"The connection to {self.host} was closed on request while {step}. The agent "
                "on the remote stops the launch."
            )
        if self.canceled.is_set():
            return RemoteCanceled(
                f"The launch on {self.host} was canceled, and the connection ended while {step}"
                f"{code}.{detail}"
            )
        if self.launching:
            return RemoteCanceled(
                f"The connection to {self.host} was lost while {step}{code}. The agent on the "
                f"remote stops the launch when it sees this.{detail}"
            )
        return RemoteConnectionError(
            f"The connection to {self.host} ended while {step}{code}.{detail}"
        )

    def detached_error(self, step: str, code: str = "", detail: str = "") -> RemoteDetached:
        """The error for a launch that keeps running and that this side does not follow any more."""
        number, host = self.launch_id, self.host
        how = (
            f"Stopped following the launch {number} on {host}."
            if self.closed
            else f"The connection to {host} was lost while {step}{code}."
        )
        return RemoteDetached(
            f"{how} The launch keeps running there. Follow it with `xeda remote attach {host}"
            f"{self.attach_options} {number}` and cancel it with `xeda remote cancel {host}"
            f"{self.attach_options} {number}`.{detail}",
            number,
            host,
        )

    def frame(self, step: str) -> tuple[bytes, bytes]:
        """The next frame of the agent. Raise the error of a lost connection at its end."""
        try:
            frame = read_frame(self.stdout)
        except RemoteProtocolError as error:
            if _ENDED_INSIDE in str(error):
                raise self.lost(step) from None
            raise RemoteProtocolError(f"{error} while {step}") from None
        except (OSError, ValueError):
            raise self.lost(step) from None
        if frame is None:
            raise self.lost(step)
        return frame

    def message(self, step: str) -> Dict[str, Any]:
        """The next JSON document of the agent. Log lines on the way are shown. An error or a
        cancel message of the agent is raised."""
        while True:
            kind, payload = self.frame(step)
            if kind == LOG:
                self.emit(payload)
                continue
            if kind != JSON:
                raise RemoteProtocolError(
                    f"The agent on {self.host} sent a frame of kind {kind!r} while {step}."
                )
            document = decode_json(payload)
            if not isinstance(document, dict):
                raise RemoteProtocolError(
                    f"The agent on {self.host} sent a JSON frame that is not an object while "
                    f"{step}."
                )
            if "error" in document:
                error = document["error"] if isinstance(document["error"], dict) else {}
                raise RemoteError(
                    f"The agent on {self.host} failed while {step}: "
                    f"{error.get('type', 'error')}: {error.get('message', document['error'])}"
                )
            if "canceled" in document and "result" not in document:
                raise RemoteCanceled(
                    f"The agent on {self.host} canceled the session while {step}: "
                    f"{document.get('reason', 'no reason given')}"
                )
            return document

    def expect(self, key: str, step: str) -> Any:
        document = self.message(step)
        if key not in document:
            raise RemoteProtocolError(
                f"The agent on {self.host} sent {sorted(document)} while {step}, and not "
                f"`{key}`."
            )
        return document[key]

    # -- sending

    def send(self, document: Any, step: str) -> None:
        try:
            self.writer.send_json(document)
        except (OSError, RemoteProtocolError, ValueError):
            raise self.after_write_failure(step) from None

    def upload(self, source: BinaryIO, step: str) -> None:
        try:
            self.writer.send_stream(source)
        except (OSError, RemoteProtocolError, ValueError):
            raise self.after_write_failure(step) from None

    def after_write_failure(self, step: str) -> RemoteError:
        """The error for a write that failed. The agent may have said why before it ended."""
        try:
            while True:
                self.message(step)  # raises the error of the agent, or the lost connection
        except RemoteError as error:
            return error
        except Exception as error:  # pylint: disable=broad-except
            return RemoteProtocolError(f"The agent on {self.host} failed while {step}: {error}")

    # -- the end

    def close(self, wait: float = _BYE_WAIT) -> None:
        """Stop the heartbeat and end the agent process. The agent exits at once after `bye` or at
        the end of its input. If it does not, the client stops after `wait` seconds."""
        self._stop.set()
        self._wake.set()
        try:
            self.process.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            self.process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._drain_thread.join(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        if self._beats is not None:
            self._beats.join(timeout=5)


@contextmanager
def _cancel_on_interrupt(agent: _Agent) -> Iterator[None]:
    """Send a cancel request to the agent on Ctrl-C, for the time of the block. A second Ctrl-C
    closes the connection. Outside the main thread no handler is installed. The request of the
    launch to stop (`cancel.launch_canceled`) does the same as the first Ctrl-C, in any thread."""
    done = threading.Event()

    def watch() -> None:
        while not done.wait(CANCEL_POLL):
            if launch_canceled():
                agent.interrupt(quiet=True)
                return

    watcher = threading.Thread(target=contextvars.copy_context().run, args=(watch,), daemon=True)
    watcher.start()
    try:
        with _interrupt_handler(agent):
            yield
    finally:
        done.set()
        watcher.join()


@contextmanager
def _cancel_while_fetching(
    agent: _Agent, run: "_Run", destination_text: str, mirror_root: Path
) -> Iterator[None]:
    """Stop the fetch and the delivery of the results when the launch is asked to stop
    (`cancel.launch_canceled`), in any thread, for the time of the block. The launch on the host
    has finished, so the watch closes the connection: a read that waits for the agent ends. The
    waits for the locks of the mirror observe the request too. Either way the block raises
    `RemoteInterrupted`, as Ctrl-C does at this point."""
    done = threading.Event()
    closed = threading.Event()

    def watch() -> None:
        while not done.wait(CANCEL_POLL):
            if launch_canceled():
                closed.set()
                agent.drop()
                return

    watcher = threading.Thread(target=contextvars.copy_context().run, args=(watch,), daemon=True)
    watcher.start()
    try:
        yield
    except Exception as error:
        if closed.is_set() or isinstance(error, Canceled):
            raise _interrupted(run, destination_text, mirror_root, "on request") from None
        raise
    finally:
        done.set()
        watcher.join()
    if closed.is_set():
        # the block ended without a read of the closed connection, as a local delivery does
        raise _interrupted(run, destination_text, mirror_root, "on request")


def _interrupted(
    run: "_Run", destination_text: str, mirror_root: Path, by: str
) -> RemoteInterrupted:
    """The error for a stop `by` Ctrl-C or on request, after the launch on the host had finished."""
    if run.phase == _DELIVERING:
        return RemoteInterrupted(
            f"Canceled {by} while {run.phase}. The launch on {destination_text} had "
            f"finished, and its results are in the mirror {mirror_root}. The files in "
            "--outputs-to may be partly written. Run the request again to deliver them."
        )
    return RemoteInterrupted(
        f"Canceled {by} while {run.phase}. The launch on {destination_text} had "
        "finished, and its results are on the host. Run the request again to fetch them."
    )


@contextmanager
def _interrupt_handler(agent: _Agent) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_number: int, _frame: Any) -> None:
        agent.interrupt()

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(
            signal.SIGINT, previous if previous is not None else signal.default_int_handler
        )


@contextmanager
def _during(step: str) -> Iterator[None]:
    """Name the step in the message of a transport error. Other errors pass unchanged."""
    try:
        yield
    except RemoteCanceled:
        raise
    except RemoteConnectionError as error:
        raise RemoteConnectionError(f"{error} (while {step})") from None
    except (BrokenPipeError, ConnectionError) as error:
        raise RemoteConnectionError(f"The connection broke while {step}: {error}") from None


# --- The request ------------------------------------------------------------------------------


@dataclass
class _Run:
    """What `run_remote` knows about the request, for the documents."""

    chain: str
    flow: str
    #: The file that the design was loaded from, if any.
    design: Optional[str]
    #: The target of the design; the plan replaces it with the target it selected.
    target: Optional[str]
    host: str
    debug: bool
    launcher: Optional[DefaultRunner] = None
    mirror_root: Optional[Path] = None
    local_root: Optional[Path] = None
    request: List[Dict[str, Any]] = field(default_factory=list)
    #: The name of the design, once it is loaded.
    design_name: Optional[str] = None
    #: The settings of the request, as a `flows` table.
    settings: Dict[str, Any] = field(default_factory=dict)
    #: The request of the caller. A launch that an attach follows has none: its record does.
    spec: Optional[Spec] = None
    #: Whether the launch was sent to the host (or found there). A failure before is no launch.
    started: bool = False
    #: The nodes of the local plan: the name, the flow and the run directory of each.
    nodes: List[Tuple[str, str, Path]] = field(default_factory=list)
    #: The id of the launch on the host, once it has one.
    launch_id: Optional[str] = None
    #: What the run does after its launch finished (`_AFTER_THE_LAUNCH`), or `None` before.
    phase: Optional[str] = None
    #: Makes the document of the result of the remote launch, with the paths of the mirror, or
    #: `None` before the launch ended. A failure after the launch builds its document from it.
    localize: Optional[Callable[[], Dict[str, Any]]] = None


@dataclass
class _Prepared:
    profile: HostProfile
    destination: Destination
    launcher: DefaultRunner
    prepared: PreparedRequest
    flow_class: Any
    requested: str
    deliveries: Optional[Deliveries]
    #: The deliveries that `--outputs-to` expects, as a plan lists them.
    planned_deliveries: tuple[PlanDelivery, ...] = ()

    def local(self) -> "_Local":
        return _Local(
            self.launcher,
            self.requested,
            self.prepared.design.name,
            [Path(node.run_path) for node in self.prepared.plan.nodes],
            self.deliveries,
        )


@dataclass
class _Local:
    """What the fetch needs of the local side: the launcher (for its run root), the requested
    node, the design name, the run directories of the local plan, and the deliveries."""

    launcher: DefaultRunner
    requested: str
    design_name: str
    run_paths: List[Path]
    deliveries: Optional[Deliveries]


def _refuse_programmers(prepared: PreparedRequest, host: str) -> None:
    for node in prepared.plan.nodes:
        reason = node.flow_class.action_reason
        if reason:
            raise SpecError(
                f"The flow {node.flow_class.name} ({reason}) cannot run with --remote, because "
                f"the device is attached to this machine and not to {host}. Run the flows "
                "before it with --remote. Then run this flow here, with a binding to the "
                "fetched file.",
                code="unsupported_with_remote",
            )


def _check_deliverables(prepared: PreparedRequest, design_name: str) -> None:
    """Refuse a deliverable setting with a location, as the old remote runner did: the remote
    would deliver to a place on the remote. The user receives the outputs with `--outputs-to`."""
    plan = prepared.plan
    for node in plan.nodes:
        located = deliverable_locations(node.settings)
        if located:
            key, path = located[0]
            raise DeliveryError(
                f"`{key}` of {node.name} names a location ({path}): with --remote, leave it a "
                "name in the run directory (or unset) and receive the outputs with "
                "--outputs-to DIR",
                before_run=True,
            )
    # The copies of generated sources go to a place in the local design tree, which the host does
    # not reach: the host would write them where the same path leads on the host.
    for _owner, unit in launch_generation.generating_units(prepared.design):
        generator = generator_of(unit.rtl, root=unit.root)
        if generator is not None and generator.outputs_to is not None:
            raise DeliveryError(
                f"`rtl.generator.outputs_to` of {unit.name} names a location "
                f"({generator.outputs_to}): with --remote, the host would deliver there, on the "
                "host. Remove it for a remote run, and run the generator here to get the copies",
                before_run=True,
            )
    # The output names, checked as a local launch checks them.
    split_deliveries(plan.node(plan.requested).settings, design_name)


def _local_run_root(run_root: Path) -> Path:
    return Path(os.path.realpath(run_root))


def _plan(
    run: _Run,
    spec: Spec,
    *,
    workspace: WorkspaceOptions,
    options: LaunchOptions,
    local_root: Path,
    destination: Destination,
    profile: HostProfile,
    confirm_overwrite: Optional[Callable[[Sequence[Conflict]], bool]],
    dry_run: bool = False,
) -> _Prepared:
    """Step 1: the checks that need no connection. A dry run asks no question."""
    destination_text = destination.text
    run.mirror_root = local_root / f"@{destination.label()}"
    launcher = DefaultRunner(
        workspace.run_root,
        rebuild_all=options.rebuild_all,
        hashed_run_dirs=workspace.hashed_run_dirs,
        clean=options.clean,
        display_results=False,
    )
    run.launcher = launcher
    prepared = launcher.prepare_request(
        spec.flow,
        design=spec.design,
        flow_settings=thaw(spec.settings),
        host_flows=profile.flows,
        host=destination_text,
        local_settings=workspace.local_settings,
        # Refused before a Git dependency is fetched: it needs nothing that one supplies.
        before_fetch=refuse_unsendable_declared,
    )
    run.target = prepared.target
    run.design_name = prepared.design.name
    run.local_root = local_root
    run.nodes = [(n.name, n.flow_class.name, Path(n.run_path)) for n in prepared.plan.nodes]
    _refuse_programmers(prepared, destination_text)
    refuse_unsendable_generator(prepared.design)
    plan = prepared.plan
    node = plan.node(plan.requested)
    _check_deliverables(prepared, prepared.design.name)
    deliveries: Optional[Deliveries] = None
    planned: tuple[PlanDelivery, ...] = ()
    if options.outputs_to is not None:
        deliveries, planned = _check_outputs_to(
            launcher,
            prepared,
            run,
            outputs_to=options.outputs_to,
            overwrite_outputs=options.overwrite_outputs,
            confirm_overwrite=confirm_overwrite,
            dry_run=dry_run,
        )
    return _Prepared(
        profile,
        destination,
        launcher,
        prepared,
        node.flow_class,
        plan.requested,
        deliveries,
        planned,
    )


def _mirror_of(mirror_root: Path, local_run_root: Path, run_path: Path) -> Path:
    """The mirror directory of a run directory (local or remote): the same place below the
    mirror root as the run directory has below its run root."""
    return mirror_root / Path(os.path.relpath(run_path, local_run_root))


def _check_outputs_to(
    launcher: DefaultRunner,
    prepared: PreparedRequest,
    run: _Run,
    *,
    outputs_to: Path,
    overwrite_outputs: bool,
    confirm_overwrite: Optional[Callable[[Sequence[Conflict]], bool]],
    dry_run: bool = False,
) -> tuple[Deliveries, tuple[PlanDelivery, ...]]:
    """Check `--outputs-to` as a local launch does, before anything is connected or shipped. The
    deliveries protect every file that this side can name as an input.

    Return the deliveries and the planned ones. A dry run, as a plan, asks no question: it
    refuses what no answer could allow and reports a file in the way as `replaces`."""
    plan = prepared.plan
    node = plan.node(plan.requested)
    local_root = _local_run_root(launcher._run_root)  # pylint: disable=protected-access
    assert run.mirror_root is not None
    mirror_dir = _mirror_of(run.mirror_root, local_root, Path(node.run_path))
    # The files that the request was given, as the launcher noted them (`prepare_request`).
    launch_inputs = list(launcher._launch_inputs)  # pylint: disable=protected-access
    deliveries = Deliveries(
        mirror_dir,
        local_root,
        inputs=remote_read_inputs(
            prepared.design,
            node.settings,
            prepared.sections,
            launch_inputs,
            flow_class=node.flow_class,
            run_path=mirror_dir,
            run_root=local_root,
        ),
        overwrite=overwrite_outputs,
        confirm=confirm_overwrite,
    )
    # The directory itself, and the files that the last fetch would deliver again.
    deliveries.check_outputs_to(outputs_to)
    previous = recorded_artifacts(mirror_dir, local_root)
    expected = outputs_to_deliveries(previous, mirror_dir, outputs_to) if previous else []
    if not dry_run:
        if previous:
            deliveries.check(expected)
        return deliveries, ()
    deliveries.refuse(expected)
    replaced = {id(d) for d in deliveries.replaced(expected)}
    return deliveries, tuple(
        PlanDelivery(delivery_key(d.key), planned_destination(d.destination), id(d) in replaced)
        for d in expected
    )


# --- The steps with the connection ------------------------------------------------------------


def _report_step(text: str) -> None:
    log.info("%s", text)


def _provision(
    session: SshSession,
    profile: HostProfile,
    snapshot: CodeSnapshot,
    spec: DependencySpec,
) -> tuple[Probe, Provisioned]:
    host = session.destination.text
    with _during("reading the facts of the host"):
        facts = probe(session, profile)
    log.info("Remote host %s: %s %s", host, facts.platform.system, facts.platform.machine)
    with _during("making the environment"):
        provisioned = ensure_environment(session, facts, profile, snapshot, spec, _report_step)
    return facts, provisioned


def _start_agent(
    session: SshSession, provisioned: Provisioned, key: str, step: str = "starting the agent"
) -> _Agent:
    host = session.destination.text
    try:
        command = agent_command(provisioned, "session", provisioned.root, key)
    except ValueError:
        raise RemoteConfigError(
            f"The remote root {provisioned.root!r} has a character that xeda cannot put in a "
            "remote command line. Set `root` in the host profile to another path."
        ) from None
    with _during(step):
        agent = _Agent(session, command)
    try:
        try:
            noise = skip_to_magic(agent.stdout)
        except RemoteProtocolError as error:
            detail = f"\nOutput of the remote host:\n{agent.stderr_text()}" if agent._stderr else ""
            raise RemoteProtocolError(
                f"The agent did not start on {host}: {error}.{detail}"
            ) from None
        if noise:
            log.debug("The login shell of %s printed %d bytes before the agent", host, len(noise))
        agent.expect("hello", step)
        agent.start_heartbeat()
    except BaseException:
        agent.close(wait=5)
        raise
    return agent


def _sync(
    agent: _Agent,
    package: InputPackage,
    design_name: str,
    key: str,
    launch_id: str,
    request: Optional[LaunchRequest] = None,
) -> tuple[list[str], Optional[Dict[str, Any]]]:
    """Send the manifest and the files that the agent lacks. Return the paths that were sent.

    With a `request` (not for a dry run), the agent looks for a launch of the same request that
    keeps running. If it finds one, it syncs nothing and answers with that launch. Then the
    result is the empty list and the record of the launch."""
    step = "syncing the inputs"
    entries = build_manifest(package.files, package.generated)
    body: Dict[str, Any] = {
        "workspace": key,
        "design": design_name,
        "entries": [entry.as_json_value() for entry in entries],
        "directories": package.directories,
        "id": launch_id,
    }
    if request is not None:
        body["digest"] = request_digest(request, entries)
    agent.send({"sync": body}, step)
    answer = agent.message(step)
    if "attached" in answer:
        record = answer["attached"]
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise RemoteProtocolError(f"The agent on {agent.host} sent a bad `attached` record.")
        return [], record
    if "need" not in answer:
        raise RemoteProtocolError(
            f"The agent on {agent.host} sent {sorted(answer)} while {step}, and not `need`."
        )
    need = answer["need"]
    if not isinstance(need, list) or not all(isinstance(path, str) for path in need):
        raise RemoteProtocolError(f"The agent on {agent.host} sent a bad `need` list.")
    known = {entry.path for entry in entries}
    unknown = sorted(set(need) - known)
    if unknown:
        raise RemoteProtocolError(
            f"The agent on {agent.host} asked for files that are not in the manifest: {unknown}"
        )
    wanted = [entry for entry in entries if entry.path in set(need)]
    with tempfile.SpooledTemporaryFile(max_size=64 << 20) as archive:
        write_tar(wanted, package.files, package.generated, cast(BinaryIO, archive))
        archive.seek(0)
        agent.upload(cast(BinaryIO, archive), step)
    synced = agent.expect("synced", step)
    log.info(
        "Synced the inputs to %s: %s file(s) written, %s removed",
        agent.host,
        synced.get("written") if isinstance(synced, dict) else "?",
        synced.get("removed") if isinstance(synced, dict) else "?",
    )
    return [entry.path for entry in wanted], None


def request_digest(request: LaunchRequest, entries: Sequence[Entry]) -> str:
    """The identity of a request for `--keep-running`: the launch request and the digests of the
    inputs. The verbosity of the log is not part of it."""
    document = request.model_dump(mode="json")
    for name in ("log_level", "detailed_logs"):
        del document[name]
    text = json.dumps(
        {"request": document, "inputs": [entry.as_json_value() for entry in entries]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _launch(
    agent: _Agent, request: LaunchRequest, launch_id: str, setup: Sequence[str]
) -> Dict[str, Any]:
    """Run the launch on the remote. Return the result document of the launch."""
    step = "running the launch"
    with _cancel_on_interrupt(agent):
        agent.launching = True
        agent.launch_id = launch_id
        try:
            agent.send(
                {
                    "launch": request.model_dump(mode="json"),
                    "id": launch_id,
                    "setup": list(setup),
                    "keep_running": agent.keep_running,
                },
                step,
            )
            document = agent.message(step)
        finally:
            agent.launching = False
    return _launch_result(agent, document)


def _follow(agent: _Agent, launch_id: str) -> Dict[str, Any]:
    """Show the log of a launch that runs on the host (or ran), from its start, and return its
    result document. Ctrl-C and a lost connection stop the follow and leave the launch running."""
    step = "following the launch"
    agent.keep_running = True
    with _cancel_on_interrupt(agent):
        agent.launching = True
        agent.launch_id = launch_id
        try:
            document = agent.message(step)
        finally:
            agent.launching = False
    return _launch_result(agent, document)


def _launch_result(agent: _Agent, document: Dict[str, Any]) -> Dict[str, Any]:
    if "result" not in document:
        raise RemoteProtocolError(
            f"The agent on {agent.host} sent {sorted(document)} when the launch ended, and not "
            "`result`."
        )
    if document.get("canceled"):
        left = document.get("survivors")
        warning = (
            f" {left} process(es) of the launch did not stop after SIGKILL and may still run on "
            f"{agent.host}."
            if left
            else ""
        )
        if agent.canceled.is_set():
            by = "" if agent.by_request else " by Ctrl-C"
            raise RemoteCanceled(f"The launch on {agent.host} was canceled{by}.{warning}")
        if agent.keep_running:
            raise RemoteCanceled(f"The launch on {agent.host} was canceled.{warning}")
        raise RemoteCanceled(
            f"The launch on {agent.host} was canceled, because the agent lost the connection or "
            f"its heartbeat.{warning}"
        )
    result = document["result"]
    if not isinstance(result, dict):
        raise RemoteError(
            f"The launch on {agent.host} ended with exit status {document.get('exit')} and left "
            "no result. The log above may tell why."
            + (
                " A logout can stop a launch that keeps running: see KillUserProcesses in "
                "`xeda remote check`."
                if agent.keep_running
                else ""
            )
        )
    if "outcome" in result and result["outcome"] is None:
        # The launch on the host started nothing: it could not read its request, or it refused it.
        error = result.get("error") or {}
        message = error.get("message", "no reason given")
        if isinstance(error.get("code"), str) and error["code"] != "remote_failed":
            raise RemoteRefused(error["code"], message, error.get("node"))
        raise RemoteError(f"The launch on {agent.host} did not start: {message}")
    return result


def _fetch(agent: _Agent, fetch_all: bool) -> tuple[list[Entry], list[SkippedSource], DataReader]:
    step = "fetching the results"
    agent.send({"fetch": {"fetch_all": fetch_all}}, step)
    document = agent.message(step)
    listing, skipped = document.get("files"), document.get("skipped")
    if set(document) != {"files", "skipped"} or not isinstance(listing, list):
        raise RemoteProtocolError(
            f"The agent on {agent.host} sent {sorted(document)} while {step}, and not a `files` "
            "list with a `skipped` list."
        )
    if not isinstance(skipped, list):
        raise RemoteProtocolError(f"The agent on {agent.host} sent a bad `skipped` list.")
    entries = [Entry.from_json_value(item) for item in listing]
    sources = [SkippedSource.from_json_value(item) for item in skipped]

    def other(kind: bytes, payload: bytes) -> None:
        if kind == LOG:
            agent.emit(payload)
        else:
            raise RemoteProtocolError(f"frame {kind!r} inside the fetched data")

    return entries, sources, DataReader(agent.stdout, on_frame=other)


def _follow_running(agent: _Agent, running: Dict[str, Any]) -> Dict[str, Any]:
    """The same request runs on the host in a launch that keeps running: follow that one."""
    launch_id = running["id"]
    log.info(
        "The same request runs on %s as the launch %s (since %s). Following it: Ctrl-C stops "
        "following and leaves it running; `xeda remote cancel %s%s %s` cancels it.",
        agent.host,
        launch_id,
        running.get("started"),
        agent.host,
        agent.attach_options,
        launch_id,
    )
    return _follow(agent, launch_id)


def _announce_keep_running(agent: _Agent, kill_user_processes: str, launch_id: str) -> None:
    host = agent.host
    log.info(
        "The launch %s keeps running on %s when this connection ends. Ctrl-C stops "
        "following it. `xeda remote attach %s%s` follows it again, and `xeda remote cancel "
        "%s%s` cancels it.",
        launch_id,
        host,
        host,
        agent.attach_options,
        host,
        agent.attach_options,
    )
    if kill_user_processes == "yes":
        log.warning(
            "systemd-logind on %s kills the processes of a user when the last session ends "
            "(KillUserProcesses=yes). A logout there may still stop this launch. Ask the "
            "administrator to enable lingering for the user (`loginctl enable-linger`).",
            host,
        )


def _record_launch(
    prepared: _Prepared,
    run: _Run,
    launch_id: str,
    *,
    local_root: Path,
    mirror_root: Path,
    remote_run_root: PurePosixPath,
    inputs_dir: PurePosixPath,
    package: InputPackage,
    design_root: Path,
    fetch_all: bool,
    delivers: bool,
) -> None:
    """Write the local record that `xeda remote attach` needs (see `launch_record`)."""
    prepared.launcher.run_root  # pylint: disable=pointless-statement
    design = run.design
    write_record(
        mirror_root,
        LaunchRecord(
            id=launch_id,
            host=run.host,
            chain=run.chain,
            flow=run.flow,
            requested=prepared.requested,
            design_name=prepared.prepared.design.name,
            design=str(design) if isinstance(design, (str, Path)) else None,
            target=run.target if isinstance(run.target, str) else None,
            request=run.request,
            local_root=str(local_root),
            settings=run.settings,
            nodes=[[name, flow] for name, flow, _path in run.nodes],
            run_paths=[str(node.run_path) for node in prepared.prepared.plan.nodes],
            remote_run_root=str(remote_run_root),
            inputs_dir=str(inputs_dir),
            files={name: str(path) for name, path in package.files.items()},
            generated=sorted(package.generated),
            design_root=str(design_root),
            fetch_all=fetch_all,
            delivers=delivers,
            debug=run.debug,
        ),
    )


# --- The documents ----------------------------------------------------------------------------


def _place_below(root: PurePosixPath, path: Any, what: str) -> PurePosixPath:
    """The place of `path` below `root`, for a path that a result of the remote names.

    The path must be an absolute path strictly below `root`, with no `..` part. A result with
    another path could make the client clear or write files outside the mirror."""
    try:
        place = PurePosixPath(path).relative_to(root)
    except (TypeError, ValueError):
        place = None
    if place is None or not place.parts or ".." in place.parts:
        raise RemoteProtocolError(f"The result names {what} {path!r}, which is not in {root}.")
    return place


def _mirror_place(mirror_root: Path, place: PurePosixPath | Path) -> Path:
    """The mirror directory for the relative `place`. Raise `ValueError` if it is not strictly
    below `mirror_root`."""
    if place.is_absolute() or not place.parts or ".." in place.parts:
        raise ValueError(f"{place} is not a place below the mirror")
    return mirror_root / place


def _node_dirs(
    document: Mapping[str, Any], remote_run_root: PurePosixPath
) -> dict[str, PurePosixPath]:
    """The run directory of every node of the result, below the remote run root, by node name."""
    found: dict[str, PurePosixPath] = {}
    for node in document.get("nodes") or []:
        try:
            name, path = node["name"], node["run_dir"]
        except (KeyError, TypeError):
            raise RemoteProtocolError(f"The result has a bad node: {node!r}") from None
        found[name] = _place_below(remote_run_root, path, "a run directory")
    return found


def _spec_fields(run: _Run) -> Dict[str, Any]:
    """The `spec` of a document of this side: what the user asked for, not the packaged copy on
    the remote."""
    return spec_document(
        flow=run.chain,
        design=run.design_name,
        design_file=run.design,
        target=run.target,
        settings=run.settings,
    )


def _localized(
    result: Mapping[str, Any],
    run: _Run,
    *,
    mirror_root: Path,
    remote_run_root: PurePosixPath,
    translate: Callable[[str], Optional[str]],
) -> Dict[str, Any]:
    """The document of the remote launch `result`, with the paths of this side.

    The run directory of a node is its place in the mirror, and its record names the paths of
    this side, as the record in the mirror does. The `spec` is the one of this side, and the
    `launch_id` is the id of the launch on the host."""
    document = copy.deepcopy(dict(result))
    document.pop("written", None)
    document.pop("unreadable", None)
    document.pop("named", None)
    document["launch_id"] = run.launch_id
    document["spec"] = _spec_fields(run)
    for node in document.get("nodes") or []:
        directory = mirror_root / _place_below(remote_run_root, node["run_dir"], "a run directory")
        node["run_dir"] = str(directory)
        if node.get("record") is not None:
            node["record"] = only_fetched_artifacts(
                translate_paths(node["record"], translate), directory
            )
    return document


def _planned_mirrors(local: "_Local", mirror_root: Path, local_root: Path) -> set[Path]:
    """The mirror directories of the nodes of the local plan."""
    found: set[Path] = set()
    for run_path in local.run_paths:
        place = Path(os.path.relpath(run_path, local_root))
        try:
            found.add(_mirror_place(mirror_root, place))
        except ValueError:
            continue  # a plan outside the run root has no mirror
    return found


def _discard_mirrors(
    local: "_Local",
    mirror_root: Path,
    local_root: Path,
    nodes: Optional[Mapping[str, PurePosixPath]] = None,
    held: Optional[LockSet] = None,
) -> None:
    """Empty the mirror directories of the planned nodes (and of `nodes`, the places that a
    result names), under their locks. Call it when the launch or the fetch failed: the records
    of an earlier run there would say that this run passed. A directory that is not there stays
    absent. A failure here is logged and does not hide the error that called it.

    The locks of all the directories are taken in one acquisition. A caller that holds them
    already passes that set as `held`, and the directories are emptied under it."""
    directories = _planned_mirrors(local, mirror_root, local_root)
    directories |= {mirror_root / place for place in (nodes or {}).values()}
    try:
        with ExitStack() as locked:
            existing = sorted(d for d in directories if d.is_dir() and not d.is_symlink())
            if held is None:
                locked.enter_context(
                    acquire_all(
                        [LockRequest("run_dir", d, True) for d in existing],
                        run_root=local_root,
                        canceled=never_canceled,  # it cleans up after a cancel
                    )
                )
            for directory in existing:
                RunDirectory.claimed(directory, local_root).clear()
    except Exception as error:  # pylint: disable=broad-except
        log.warning("Cannot empty the mirror of an unfinished run: %s", error)


def _mirror_dry_run(
    document: Dict[str, Any],
    mirror_root: Path,
    remote_run_root: PurePosixPath,
    translate: Callable[[str], Optional[str]],
) -> None:
    """Give a dry-run document the paths of this side, as the document of a run has them.

    A dry run has no mirror, but the run does: a run directory is its place in the mirror. The
    sources, settings and deliveries of a node name the files here, not the copies that the
    remote reads."""
    for node in document.get("nodes") or []:
        if not isinstance(node, dict) or "run_dir" not in node:
            continue
        place = _place_below(remote_run_root, node["run_dir"], "a run directory")
        node["run_dir"] = str(mirror_root / place)
        for key in ("settings", "inputs", "deliveries"):
            if key in node:
                node[key] = translate_paths(node[key], translate)


def _translation(
    *,
    remote_run_root: PurePosixPath,
    mirror_root: Path,
    inputs_dir: PurePosixPath,
    files: Mapping[str, str],
    generated: Collection[str],
    design_root: Path,
    local_root: Path,
) -> Callable[[str], Optional[str]]:
    """Map a path on the remote to the path of the same file here, or to `None`.

    A path in the Git dependency checkouts of the remote goes to the same checkout in the local run
    root, which planning here fetched under the same key. Any other path in the run root of the
    remote goes to the same place in the mirror. A path in the
    inputs directory goes to the local file that was sent there, or else to the same place below
    the design root: a place that the remote makes for an output (`{path: out/trace.txt}`) has
    the same name there. The inputs directory itself goes to the design root. A file that xeda
    made for the remote (the design document, a Liberty file with renamed includes) has no such
    file here, and its path stays. So does a path elsewhere (a tool, a store of the remote)."""
    sent = {(inputs_dir / name).as_posix(): local for name, local in files.items()}
    made = {(inputs_dir / name).as_posix() for name in generated}
    checkouts = remote_run_root / DEPENDENCIES_DIRECTORY

    def translate(text: str) -> Optional[str]:
        if not text.startswith("/"):
            return None
        path = PurePosixPath(text)
        if ".." in path.parts:
            return None
        if path.is_relative_to(checkouts):
            return str(local_root / DEPENDENCIES_DIRECTORY / path.relative_to(checkouts))
        if path == remote_run_root or path.is_relative_to(remote_run_root):
            return str(mirror_root / path.relative_to(remote_run_root))
        if path == inputs_dir:
            return str(design_root)
        if not path.is_relative_to(inputs_dir):
            return None
        if path.as_posix() in sent:
            return sent[path.as_posix()]
        relative = path.relative_to(inputs_dir)
        if path.as_posix() in made or relative.parts[0] in _SYNTHETIC_INPUTS:
            return None
        return str(design_root / relative)

    return translate


def _plan_options(options: LaunchOptions) -> Dict[str, Any]:
    """The `options` of a plan document: the options of the caller. The host knows only some of
    them, since it delivers nothing."""
    document = options.as_dict()
    if options.outputs_to is not None:
        document["outputs_to"] = str(options.outputs_to)
    return document


def _plan_deliveries(document: Dict[str, Any], planned: Sequence[PlanDelivery]) -> None:
    """Put the planned deliveries into the plan document of the host, in the requested node. The
    host delivers nothing, so only this side knows them."""
    roots = document.get("roots") or []
    for node in document.get("nodes") or []:
        if node.get("name") in roots:
            node["deliveries"] = [
                {"key": d.key, "destination": str(d.destination), "replaces": d.replaces}
                for d in planned
            ]


def _print_dry_run(host: str, uploaded: Sequence[str]) -> None:
    """The report of a dry run on the log: what was uploaded."""
    if uploaded:
        log.info("Uploaded %d input file(s) to %s: %s", len(uploaded), host, ", ".join(uploaded))
    else:
        log.info("Uploaded no input file to %s: it had them all", host)


# --- run_remote -------------------------------------------------------------------------------


def _refuse_unsupported(options: LaunchOptions) -> None:
    """Refuse the options that remove run directories: they are on the host, and a remote launch
    cannot ask the user to confirm the removal."""
    for name in ("prune_variants", "post_cleanup", "post_cleanup_purge"):
        if getattr(options, name):
            raise SpecError(
                f"`{name}` removes run directories, and with a remote host they are on the host: "
                "use `xeda remote clean`",
                code="unsupported_with_remote",
            )


def _remote_of(workspace: WorkspaceOptions) -> Remote:
    remote = workspace.remote
    if not isinstance(remote, Remote):
        raise TypeError(f"the workspace has no `Remote`: {remote!r}")
    return remote


def _profile_of(remote: Remote) -> HostProfile:
    """The profile of the host as the `Remote` holds it: the default profile without one."""
    if remote.profile is None:
        return HostProfile()
    try:
        return HostProfile.model_validate(thaw(remote.profile))
    except ValidationError as error:
        raise RemoteConfigError(f"The profile of {remote.host} is invalid: {error}") from None


def _request_run(spec: Spec, host: str, log_level: int) -> _Run:
    """The record of one request for the documents."""
    design = spec.design
    file = design._source_file  # pylint: disable=protected-access
    return _Run(
        chain=spec.flow,
        flow=spec.flow,
        design=None if file is None else os.path.abspath(file),
        target=design.target,
        host=host,
        debug=log_level <= logging.DEBUG,
        design_name=design.name,
        settings=thaw(spec.settings),
        spec=spec,
    )


def _log_level(log_level: Optional[int]) -> int:
    """The level of the logs that the host sends: the one of the `xeda` logger, unless given."""
    return logging.getLogger("xeda").getEffectiveLevel() if log_level is None else log_level


def _detailed_logs(detailed_logs: Optional[bool]) -> bool:
    """Whether the logs that the host sends are detailed: as the logging of this process is,
    unless given (`logs_are_detailed`)."""
    return logs_are_detailed() if detailed_logs is None else detailed_logs


def _asked(workspace: WorkspaceOptions) -> Optional[Callable[[Sequence[Conflict]], bool]]:
    """The question about the files in the way of `outputs_to`, as the workspace answers it."""
    answer = workspace.confirm
    if answer is None:
        return None
    return lambda conflicts: answer(overwrite_question(conflicts))


def run_remote(
    spec: Spec,
    *,
    workspace: WorkspaceOptions,
    options: LaunchOptions,
    cancel: Optional[CancelToken] = None,
    on_event: Optional[Callable[[Event], None]] = None,
    log_level: Optional[int] = None,
    detailed_logs: Optional[bool] = None,
) -> Result:
    """Run `spec` on the host of `workspace.remote` and return what the launch did to every node.

    The host is `workspace.remote` (a `Remote`, which holds its profile, `fetch_all` and
    `keep_running`). `log_level` is the level of the logs that the host sends: the level of the
    `xeda` logger without it. `detailed_logs` is whether they are detailed: as the logging of this
    process is without it (`logs_are_detailed`).

    A failure after the launch was sent to the host is a launch-level error of the `Result`
    (`remote_failed`, `remote_fetch_failed`, `delivery_failed`, `canceled`, or `remote_detached`
    with the outcome `detached`). A failure before raises: a `SpecError` for a request that the
    host or this side refuses, a `RemoteError` (`remote_unreachable`, `remote_setup_failed`).
    `cancel` stops the launch, and `on_event` gets its events. The host does the work, so the
    events of the nodes come when the launch ends. `launch_started` comes once the host has
    accepted the launch, with the id of the launch that runs there: the one that this call sent, or
    an identical one that keeps running, which this call then follows. An exception of `on_event`
    cancels the launch and is raised again."""
    level = _log_level(log_level)
    detailed = _detailed_logs(detailed_logs)
    remote = _remote_of(workspace)
    run = _request_run(spec, remote.host, level)
    progress = _Progress("", cancel, on_event)
    with observing(progress.canceled):
        try:
            _refuse_unsupported(options)
            request = parse_request(spec.flow)
            run.flow = request.requested.name
            run.request = request.as_elements()
            result = cast(
                Result,
                _run(
                    run,
                    spec,
                    workspace=workspace,
                    options=options,
                    remote=remote,
                    log_level=level,
                    detailed_logs=detailed,
                    dry_run=False,
                    progress=progress,
                ),
            )
        except Exception as error:  # pylint: disable=broad-except
            result = _failed(run, error)
        _tell(progress, result)
    if progress.raised is not None:
        raise progress.raised
    return result


def plan_remote(
    spec: Spec,
    *,
    workspace: WorkspaceOptions,
    options: LaunchOptions,
    log_level: Optional[int] = None,
    detailed_logs: Optional[bool] = None,
) -> Plan:
    """Ask the host of `workspace.remote` to plan `spec`, and return its `Plan`.

    The plan has the spec of the caller, and the run directories of the local mirror. It writes
    no mirror and starts no launch. The host writes nothing in its run root but the storage of
    its Git dependencies. Every failure raises, as a failure of `run_remote` before its launch
    started does."""
    level = _log_level(log_level)
    detailed = _detailed_logs(detailed_logs)
    remote = _remote_of(workspace)
    run = _request_run(spec, remote.host, level)
    try:
        _refuse_unsupported(options)
        run.request = parse_request(spec.flow).as_elements()
        document = _run(
            run,
            spec,
            workspace=workspace,
            options=options,
            remote=remote,
            log_level=level,
            detailed_logs=detailed,
            dry_run=True,
            progress=None,
        )
        try:
            return plan_from_dict(cast(Dict[str, Any], document), spec=spec, workspace=workspace)
        except ValueError as error:
            raise RemoteProtocolError(
                f"The host {remote.host} sent a plan that cannot be read: {error}"
            ) from None
    except Exception as error:  # pylint: disable=broad-except
        raise _not_started(run, error) from error


def _tell(progress: _Progress, result: Result) -> None:
    """Tell `on_event` what a finished launch did: the nodes that it started, then its end. The
    host ran the nodes, so they come together."""
    progress.launch_id = result.launch_id
    progress.emit("launch_started")
    for node in result.nodes.values():
        if node.disposition is not Disposition.NOT_RUN:
            progress.emit("node_started", node.name)
            progress.emit("node_finished", node.name, node.outcome, node.disposition)
    progress.emit("launch_finished", None, result.outcome)


def _launch_error_code(run: _Run, error: Exception) -> str:
    """The code of the launch-level error that `error` is, for a failure of this side."""
    if isinstance(error, RemoteDetached):
        return "remote_detached"
    if isinstance(error, (RemoteCanceled, RemoteInterrupted)):
        return "canceled"
    if run.phase == _DELIVERING:
        return "delivery_failed"
    if run.phase == _FETCHING or isinstance(error, RemoteFetchError):
        return "remote_fetch_failed"
    return "remote_failed"


def _unreached(run: _Run, outcome: Outcome) -> Dict[str, NodeResult]:
    """The nodes of the local plan as a launch that did not reach them leaves them: `outcome` is
    `detached` for a launch that keeps running on the host, else `skipped`. Their run directories
    are the places in the mirror."""
    found: Dict[str, NodeResult] = {}
    for name, flow, run_path in run.nodes:
        assert run.mirror_root is not None and run.local_root is not None
        found[name] = NodeResult(
            name=name,
            outcome=outcome,
            disposition=Disposition.NOT_RUN,
            reason=(
                "the launch keeps running on the host"
                if outcome is Outcome.DETACHED
                else "the launch stopped before its turn"
            ),
            blocked_by=None,
            elapsed=0.0,
            record=None,
            error=None,
            deliveries=(),
            run_dir=_mirror_of(run.mirror_root, run.local_root, run_path),
            flow=flow,
        )
    return found


def _not_started(run: _Run, error: Exception) -> Exception:
    """The public error for a failure before the launch started."""
    from ..flow_runner.error_codes import public_error

    if isinstance(error, RemoteRefused):
        # The host refused the request as a local planning does.
        if error.node is not None:
            return SettingsError(str(error), code=error.code, node=error.node)
        return SpecError(str(error), code=error.code)
    public = public_error(error)
    if isinstance(public, PublicRemoteError):
        return PublicRemoteError(public.message, code=public.code, host=public.host or run.host)
    if isinstance(error, RemoteError):
        # A canceled, detached or interrupted run has no launch to report. It is a setup failure.
        return PublicRemoteError(str(error), code="remote_setup_failed", host=run.host)
    return public


def _result_of(document: Dict[str, Any], run: _Run) -> Result:
    """The `Result` of the document of a launch, with the `spec` of the request if it has one."""
    try:
        result = result_from_dict(document)
    except (ValueError, RecordError) as error:
        raise RemoteProtocolError(
            f"The result of the launch on {run.host} cannot be read: {error}"
        ) from None
    return result if run.spec is None else replace(result, spec=run.spec)


def _failed(run: _Run, error: Exception) -> Result:
    """The result of a run that raised `error`, or the public error if no launch had started.

    The nodes are those of the remote result when the launch had ended, else those of the local
    plan. An error of this side after the launch started is a launch-level error."""
    from ..flow_runner.error_codes import request_error_info

    log.debug("The remote run failed", exc_info=True)
    # An attach that failed before it read its record has no request to report.
    spec = _spec_fields(run) if run.chain else None
    if isinstance(error, Canceled) and not run.started:
        document = run_document(
            outcome=Outcome.CANCELED,
            error=ErrorInfo(CANCELED, "The launch was canceled.", None, None, None),
            launch_id=run.launch_id or new_launch_id(),
            spec=spec,
            roots=(),
            nodes=(),
        )
        return _result_of(document, run)
    if isinstance(error, RemoteRefused) or not run.started:
        raise _not_started(run, error) from error
    # An error of the whole launch names no node.
    info = replace(request_error_info(error, code=_launch_error_code(run, error)), node=None)
    launch_id = run.launch_id or getattr(error, "launch_id", None)
    remote: Optional[Dict[str, Any]] = None
    if run.localize is not None:
        try:
            remote = run.localize()
        except Exception:  # pylint: disable=broad-except
            log.debug("The result of the remote launch cannot be shown", exc_info=True)
    if remote is not None:
        parsed = result_from_dict(remote)
        nodes, roots = dict(parsed.nodes), parsed.roots
        node_documents = remote["nodes"]
    else:
        unreached = _unreached(
            run, Outcome.DETACHED if isinstance(error, RemoteDetached) else Outcome.SKIPPED
        )
        nodes, roots = unreached, (tuple(unreached)[-1:] if unreached else ())
        node_documents = [node_document(node) for node in unreached.values()]
    outcome, cause = aggregate(nodes, roots, info)
    document = run_document(
        outcome=outcome,
        error=cause,
        launch_id=launch_id,
        spec=spec,
        roots=roots,
        nodes=node_documents,
    )
    return _result_of(document, run)


def _run(
    run: _Run,
    spec: Spec,
    *,
    workspace: WorkspaceOptions,
    options: LaunchOptions,
    remote: Remote,
    log_level: int,
    detailed_logs: bool,
    dry_run: bool,
    progress: Optional[_Progress],
) -> Result | Dict[str, Any]:
    """Run the request, or only plan it (`dry_run`). A run returns its `Result`, and a dry run the
    document of the plan, with the paths of this side."""
    if _is_windows():
        raise RemoteError(
            "--remote does not work on native Windows. Use WSL, or run xeda on a Linux or macOS "
            "machine."
        )
    destination_text = remote.host
    profile = _profile_of(remote)
    destination = resolve_destination(destination_text)
    local_root = _local_run_root(workspace.run_root)
    prepared = _plan(
        run,
        spec,
        workspace=workspace,
        options=options,
        local_root=local_root,
        destination=destination,
        profile=profile,
        confirm_overwrite=_asked(workspace),
        dry_run=dry_run,
    )
    mirror_root = local_root / f"@{destination.label()}"
    design = prepared.prepared.design
    plan = prepared.prepared.plan
    snapshot = code_snapshot()
    dependencies = dependency_spec(design.tb.python_packages)
    key = workspace_key(local_root, destination)
    launch_id = new_launch_id()
    run.launch_id = launch_id
    local = prepared.local()
    # The mirror of a node lies below the run root like the node: no link on the way to it
    # (`@host` included) is followed, so refuse one before the host runs anything.
    for directory in sorted(_planned_mirrors(local, mirror_root, local_root)):
        refuse_link_on_way(directory.parent, local_root)
    if launch_canceled():
        raise Canceled("the launch was canceled before it was sent to the host")
    log.info("Connecting to %s...", destination_text)
    try:
        with SshSession(destination) as session:
            facts, provisioned = _provision(session, profile, snapshot, dependencies)
            workspace_dir = PurePosixPath(facts.root) / LAYOUT / "ws" / key
            inputs_dir = workspace_dir / "inputs" / design.name
            remote_run_root = workspace_dir / "xeda_run"
            # The host plans too, and merges the dependencies at the same point: the package holds
            # the design as it was given, a Git dependency as its reference (the host fetches it
            # into its own run root) and a local dependency as a design document with its files.
            package = package_inputs(
                prepared.prepared.declared,
                prepared.prepared.sections,
                {node.name for node in plan.nodes},
                run_root=local_root,
                remote_inputs=inputs_dir,
                host_flows=profile.flows,
                bindings=plan.bindings,
                request=plan.request,
                resolved=prepared.prepared.design.resolved_dependencies,
            )
            launch = LaunchRequest(
                chain=run.chain,
                design_file=package.design_file,
                inputs_dir=str(inputs_dir),
                run_root=str(remote_run_root),
                path_identities=package.path_identities,
                dependency_commits=package.dependency_commits,
                hashed_run_dirs=workspace.hashed_run_dirs,
                clean=options.clean,
                rebuild_all=options.rebuild_all,
                scrub=options.prune_variants,
                dry_run=dry_run,
                log_level=log_level,
                detailed_logs=detailed_logs,
                expected={node.name: node.flowrun_hash for node in plan.nodes},
            )
            agent = _start_agent(session, provisioned, key)
            # A dry run has no launch to keep: it announces nothing and writes no record.
            keep_running = remote.keep_running and not dry_run
            agent.keep_running = keep_running
            if local_root != _local_run_root(Path.cwd() / "xeda_run"):
                agent.attach_options = f" --run-root {shlex.quote(str(local_root))}"
            try:
                uploaded, running = _sync(
                    agent, package, design.name, key, launch_id, None if dry_run else launch
                )
                try:
                    if progress is not None:
                        # An identical launch that keeps running has the id of that launch.
                        progress.launch_id = launch_id if running is None else running["id"]
                        progress.emit("launch_started")
                    if running is not None:
                        run.launch_id = running["id"]
                        run.started = True
                        result = _follow_running(agent, running)
                    else:
                        if keep_running:
                            _announce_keep_running(agent, facts.kill_user_processes, launch_id)
                            _record_launch(
                                prepared,
                                run,
                                launch_id,
                                local_root=local_root,
                                mirror_root=mirror_root,
                                remote_run_root=remote_run_root,
                                inputs_dir=inputs_dir,
                                package=package,
                                design_root=design.root,
                                fetch_all=remote.fetch_all,
                                delivers=options.outputs_to is not None,
                            )
                        run.started = not dry_run
                        result = _launch(agent, launch, launch_id, profile.setup)
                except BaseException:
                    if not dry_run:
                        _discard_mirrors(local, mirror_root, local_root)
                    raise
                run.phase = None if dry_run else _FETCHING
                translate = _translation(
                    remote_run_root=remote_run_root,
                    mirror_root=mirror_root,
                    inputs_dir=inputs_dir,
                    files={name: str(path) for name, path in package.files.items()},
                    generated=package.generated,
                    design_root=design.root,
                    local_root=local_root,
                )
                outcome: Result | Dict[str, Any]
                if dry_run:
                    if "nodes" not in result or result.get("error") is not None:
                        message = (result.get("error") or {}).get("message", "no reason given")
                        raise RemoteError(f"The host {destination_text} cannot plan: {message}")
                    outcome = dict(result)
                    _mirror_dry_run(outcome, mirror_root, remote_run_root, translate)
                    outcome["options"] = _plan_options(options)
                    if options.outputs_to is not None:
                        _plan_deliveries(outcome, prepared.planned_deliveries)
                    _print_dry_run(destination_text, uploaded)
                else:
                    run.localize = lambda: _localized(
                        result,
                        run,
                        mirror_root=mirror_root,
                        remote_run_root=remote_run_root,
                        translate=translate,
                    )
                    with _cancel_while_fetching(agent, run, destination_text, mirror_root):
                        outcome = _finish(
                            agent,
                            local,
                            run,
                            result,
                            mirror_root=mirror_root,
                            local_root=local_root,
                            remote_run_root=remote_run_root,
                            translate=translate,
                            fetch_all=remote.fetch_all,
                            outputs_to=options.outputs_to,
                        )
                agent.send({"bye": True}, "ending the session")
            except BaseException:
                agent.close(wait=5)
                raise
            agent.close()
            return outcome
    except KeyboardInterrupt:
        if run.phase is not None:
            raise _interrupted(run, destination_text, mirror_root, "by Ctrl-C") from None
        if run.started:
            raise RemoteCanceled(
                f"Canceled by Ctrl-C. The launch on {destination_text} is stopped or was never "
                "started."
            ) from None
        raise


def _note_deliveries(
    document: Dict[str, Any],
    requested: str,
    planned: Sequence[Any],
    delivered: Sequence[Delivered],
) -> None:
    """Put the deliveries of the requested node into `document`, as a local launch reports them
    (`launch_result.delivery_records`): every planned one, delivered or not, and the skipped
    ones."""
    from ..flow_runner.launch_result import delivery_records

    for node in document.get("nodes") or []:
        if node["name"] == requested:
            node["deliveries"] = [
                {
                    "key": one.key,
                    "destination": str(one.destination),
                    "delivered": one.delivered,
                }
                for one in delivery_records(planned, delivered)
            ]


def _finish(
    agent: _Agent,
    local: _Local,
    run: _Run,
    result: Dict[str, Any],
    *,
    mirror_root: Path,
    local_root: Path,
    remote_run_root: PurePosixPath,
    translate: Callable[[str], Optional[str]],
    fetch_all: bool,
    outputs_to: Optional[Path],
) -> Result:
    """Fetch the files of the run into the mirror, rewrite the records and deliver. Return the
    document of the launch, with the paths of the mirror."""
    host = agent.host
    # The run root is made and marked as Xeda's before the mirror goes into it.
    local.launcher.run_root  # pylint: disable=pointless-statement
    nodes = _node_dirs(result, remote_run_root)
    unreadable = result.get("unreadable") or {}
    for name, places in unreadable.items():
        log.warning(
            "On %s, the run directory of %s has unreadable directories: %s. The files below them "
            "are not fetched for a failed run.",
            host,
            name,
            ", ".join(places),
        )
    try:
        entries, skipped, reader = _fetch(agent, fetch_all)
    except BaseException:
        _discard_mirrors(local, mirror_root, local_root, nodes)
        raise
    directories = {name: mirror_root / place for name, place in nodes.items()}
    # The mirror of every planned node is replaced, also of a node that the launch never reached
    # (it failed before any node ran, or a producer failed): an earlier run's records there
    # would say that this node passed.
    replaced = set(directories.values()) | _planned_mirrors(local, mirror_root, local_root)
    # The locks of the whole mirror are taken in one acquisition, in the order of every launch, and
    # held through the publication, the rewrite of the records and the local delivery.
    with acquire_all(
        [LockRequest("run_dir", directory, True) for directory in sorted(replaced)],
        run_root=local_root,
    ) as locked:
        try:
            for directory in sorted(replaced):
                if directory.is_dir() and not directory.is_symlink():
                    RunDirectory.claimed(directory, local_root).clear()
            log.info("Fetching %d file(s) from %s into %s", len(entries), host, mirror_root)
            publish_mirror(
                mirror_root,
                entries,
                cast(BinaryIO, io.BufferedReader(reader)),
                remote_run_root=remote_run_root,
            )
            reader.drain()
            requested_skips = [source for source in skipped if source.node == local.requested]
            if requested_skips:
                log.warning(
                    "On %s, these artifacts of %s lead out of the run directory through a symbolic "
                    "link and were not fetched: %s",
                    host,
                    local.requested,
                    ", ".join(sorted(source.place for source in requested_skips)),
                )
            # The records of the mirror are whole before anything is delivered: a refused delivery
            # leaves them as they are.
            for directory in sorted(set(directories.values())):
                rewrite_records(directory, local_root, translate)
            assert run.localize is not None
            document = run.localize()
            record = None
            requested = next((n for n in document["nodes"] if n["name"] == local.requested), None)
            if requested is not None and requested.get("record") is not None:
                # The record of the document, not the file of the mirror: a rejected run that was
                # reused wrote nothing, so its file did not come back.
                try:
                    # The run root of a record in the mirror is the mirror, as for
                    # `read_record` and `result_from_dict`: the run directories of its producers
                    # are directories of the mirror too.
                    record = decode_record(
                        requested["record"],
                        run_dir=Path(requested["run_dir"]),
                        run_root=mirror_root,
                    )
                except RecordError as problem:
                    raise RemoteFetchError(f"cannot read the fetched record: {problem}") from None
                check_fetched_outputs(record)
        except BaseException:
            # A cut in the transfer, or a failure while the records are rewritten, leaves some files
            # of this run beside the records of an earlier one. The discard runs under the locks
            # that are held.
            _discard_mirrors(local, mirror_root, local_root, nodes, held=locked)
            raise
        if record is not None:
            if outputs_to is not None and local.deliveries is not None:
                run.phase = _DELIVERING
                planned: list[Any] = []
                try:
                    delivered = deliver_fetched(
                        local.deliveries,
                        record,
                        outputs_to=outputs_to,
                        skipped=requested_skips,
                        planned=planned,
                    )
                except BaseException:
                    # Ctrl-C too: the document of the launch lists what reached its destination.
                    _note_deliveries(document, local.requested, planned, local.deliveries.delivered)
                    run.localize = lambda: document
                    raise
                _note_deliveries(document, local.requested, planned, delivered)
    return _result_of(document, run)


__all__ = [
    "machine_id",
    "new_launch_id",
    "plan_remote",
    "run_remote",
    "workspace_key",
]
