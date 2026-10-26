"""A remote launch that keeps running survives its connection, and `attach` and `cancel` find it
again.

The tests run the real client, agent and launch entry over the fake `ssh`, with the fake FPGA
tools. A tool that must be running at a given point reports its process id through a FIFO and
blocks (`XEDA_FAKE_FPGA_READY`). The test lets it go by opening a second FIFO
(`XEDA_FAKE_FPGA_GO`). So no test waits for a time to pass, and every test stops the processes
that it started by process id.
"""

import contextlib
import fcntl
import io
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import psutil
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.remote import client
from xeda.remote.attach import attach_launch, cancel_launch
from xeda.remote.frames import CANCEL
from xeda.remote.errors import RemoteCanceled, RemoteDetached, RemoteError
from xeda.remote.ssh import SshSession

from .remote_fixtures import RemoteHost, fake_remote, fake_ssh, remote_host, remote_request
from .test_remote_agent import (
    DESIGN,
    USE_LOCK,
    Beats,
    BlockingTool,
    file_is_unlocked,
    launch_message,
    path_setup,
    provisioned,
    run_command,
    start_agent,
    toolchain,
    workspace,
)
from .test_remote_client import TIMEOUT, _assert_gone, _design, _states, _succeeded

FIXTURES = (fake_ssh, fake_remote, remote_host, start_agent, toolchain, provisioned)

pytestmark = [pytest.mark.python_compat, pytest.mark.skipif(os.name != "posix", reason="POSIX")]


# --- Helpers ----------------------------------------------------------------------------------


class Gate:
    """The fake tool of the host blocks until the test lets it go. `pid()` returns the process id
    of the next tool that blocks. `release()` lets one blocked tool go on."""

    def __init__(self, tmp_path: Path, remote_host: RemoteHost) -> None:
        self.ready = tmp_path / "ready"
        self.go = tmp_path / "go"
        os.mkfifo(self.ready)
        os.mkfifo(self.go)
        remote_host.configure(
            env={
                "XEDA_FAKE_FPGA_READY": str(self.ready),
                "XEDA_FAKE_FPGA_GO": str(self.go),
                "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5",
            }
        )
        self.pids: list[int] = []
        self._found: "queue.Queue[int]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            with open(self.ready, "rb") as stream:  # blocks until a tool writes to the FIFO
                text = stream.read()
            if text:
                self._found.put(int(text))

    def pid(self) -> int:
        try:
            found = self._found.get(timeout=TIMEOUT)
        except queue.Empty:
            pytest.fail(f"no tool blocked in {TIMEOUT} s")
        self.pids.append(found)
        return found

    def release(self) -> None:
        with open(self.go, "wb"):  # blocks until a blocked tool reads the FIFO
            pass


class Watch(logging.Handler):
    """Tell with an event when a log message has a text."""

    def __init__(self, text: str) -> None:
        super().__init__(logging.INFO)
        self.text = text
        self.seen = threading.Event()
        self.message = ""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if self.text in message:
            self.message = message
            self.seen.set()

    def wait(self) -> str:
        if not self.seen.wait(TIMEOUT):
            pytest.fail(f"{self.text!r} did not appear in the log")
        return self.message


@pytest.fixture
def watch() -> Iterator[Callable[[str], Watch]]:
    handlers: list[Watch] = []
    logger = logging.getLogger("xeda.remote")
    previous = logger.level
    logger.setLevel(logging.INFO)

    def make(text: str) -> Watch:
        handler = Watch(text)
        logger.addHandler(handler)
        handlers.append(handler)
        return handler

    yield make
    for handler in handlers:
        logger.removeHandler(handler)
    logger.setLevel(previous)


class Connections:
    """The agent connections that the client opens, so that a test can cut one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents: list[Any] = []
        real = SshSession.popen

        def popen(session: SshSession, command: str):
            process = real(session, command)
            if " agent " in command:
                self.agents.append(process)
            return process

        monkeypatch.setattr(SshSession, "popen", popen)


def _launch_dirs(remote_host: RemoteHost) -> list[Path]:
    return sorted((remote_host.workspace() / "launches").iterdir())


def _launch_state(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "state.json").read_text())


def _agents_of(remote_host: RemoteHost) -> list[psutil.Process]:
    """The agent processes of this test: the remote root names the home of the fake host."""
    found = []
    for process in psutil.process_iter():
        try:
            line = process.cmdline()
        except psutil.Error:
            continue
        if "xeda.remote.agent" in line and str(remote_host.home) in " ".join(line):
            found.append(process)
    return found


def _wait_gone(processes: list[psutil.Process]) -> None:
    _gone, alive = psutil.wait_procs(processes, timeout=TIMEOUT)
    assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE], alive


def _lock_is_held(remote_host: RemoteHost) -> bool:
    with (remote_host.workspace() / "lock").open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


class _Processes(list):
    """The processes that a test must stop. `append(pid)` takes the process at once: psutil then
    refuses to signal another process that gets the same process id later."""

    def append(self, pid: int) -> None:  # type: ignore[override]
        try:
            super().append(psutil.Process(pid))
        except psutil.Error:
            pass


def _kill(processes: list[psutil.Process]) -> None:
    for process in processes:
        try:
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        except psutil.Error:
            continue


@pytest.fixture
def leftovers() -> Iterator[_Processes]:
    """The processes that a test names are stopped at its end, by process id."""
    processes = _Processes()
    yield processes
    _kill(processes)


@pytest.fixture
def cleanup(remote_host: RemoteHost, leftovers: _Processes) -> Iterator[_Processes]:
    """`leftovers`, and also the launches of the workspace, by their records."""
    yield leftovers
    found: list[psutil.Process] = []
    if remote_host.workspaces.is_dir():
        for workspace in remote_host.workspaces.iterdir():
            for directory in (workspace / "launches").glob("*"):
                try:
                    state = _launch_state(directory)
                    process = psutil.Process(state["pid"])
                    # a record of a launch that ended names a process id that another process may
                    # have now: stop the process only if it is the one that was recorded
                    if abs(process.create_time() - state["pid_created"]) < 1.0:
                        found.append(process)
                except (OSError, KeyError, ValueError, psutil.Error):
                    continue
    _kill(found)


def _run_in_thread(function: Callable[[], Any]) -> tuple[threading.Thread, "queue.Queue[Any]"]:
    outcome: "queue.Queue[Any]" = queue.Queue()
    thread = threading.Thread(target=lambda: outcome.put(function()), daemon=True)
    thread.start()
    return thread, outcome


def _keep_running(tmp_path: Path, design: Path, **overrides: Any):
    return remote_request(tmp_path, design, "nextpnr", keep_running=True, **overrides)


def _attach(tmp_path: Path, launch_id: Optional[str] = None):
    return attach_launch("somewhere", launch_id, run_root=tmp_path / "local" / "xeda_run")


def _cut_while_running(
    tmp_path: Path,
    design: Path,
    gate: Gate,
    connections: Connections,
    remote_host: RemoteHost,
    **overrides: Any,
) -> tuple[Any, int]:
    """Start a launch that keeps running, cut the connection when the tool runs, and return the
    outcome of the run and the process id of the tool."""
    holder: list[int] = []

    def cut() -> None:
        holder.append(gate.pid())
        connections.agents[0].kill()  # the connection goes away while the tool runs

    thread = threading.Thread(target=cut, daemon=True)
    thread.start()
    outcome = _keep_running(tmp_path, design, **overrides)
    thread.join(TIMEOUT)
    return outcome, holder[0]


def _wait_for_launch(remote_host: RemoteHost, directory: Path) -> dict[str, Any]:
    """Wait until the process of the launch has ended. Return its result."""
    try:
        psutil.Process(_launch_state(directory)["pid"]).wait(TIMEOUT)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired) as error:
        if isinstance(error, psutil.TimeoutExpired):
            pytest.fail("the launch did not end")
    return json.loads((directory / "result.json").read_text())


# --- A launch that keeps running survives -------------------------------------------------------


def test_a_launch_that_keeps_running_survives_a_killed_session_and_finishes(
    tmp_path, remote_host, monkeypatch, cleanup
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    outcome, tool = _cut_while_running(tmp_path, _design(tmp_path), gate, connections, remote_host)
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached), outcome.document
    document = outcome.document
    assert document["success"] is False and document["error"]["type"] == "RemoteDetached"
    (directory,) = _launch_dirs(remote_host)
    assert document["launch"] == {"id": directory.name, "host": "somewhere"}
    message = document["error"]["message"]
    assert "xeda remote attach somewhere" in message and directory.name in message
    assert "xeda remote cancel somewhere" in message and "--run-root" in message

    # The session is gone, and the launch and its tool run on, and hold the workspace lock.
    state = _launch_state(directory)
    assert state["keep_running"] is True and state["state"] == "running"
    assert psutil.Process(tool).is_running() and psutil.Process(state["pid"]).is_running()
    assert _lock_is_held(remote_host)

    gate.release()
    result = _wait_for_launch(remote_host, directory)
    assert result["success"] is True
    assert not _lock_is_held(remote_host)
    _assert_gone(tool)


def test_the_agent_leaves_a_launch_that_keeps_running_and_the_child_keeps_the_lock(
    tmp_path, remote_host, monkeypatch, cleanup
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    holder: list[list[psutil.Process]] = []

    def cut() -> None:
        cleanup.append(gate.pid())
        holder.append(_agents_of(remote_host))
        connections.agents[0].kill()

    thread = threading.Thread(target=cut, daemon=True)
    thread.start()
    outcome = _keep_running(tmp_path, _design(tmp_path))
    thread.join(TIMEOUT)
    assert isinstance(outcome.error, RemoteDetached)
    assert holder[0], "no agent found"
    _wait_gone(holder[0])  # the agent exits by itself, without a cancel
    assert _lock_is_held(remote_host)  # the launch child holds the lock
    (directory,) = _launch_dirs(remote_host)
    assert psutil.Process(_launch_state(directory)["pid"]).is_running()
    gate.release()
    assert _wait_for_launch(remote_host, directory)["success"] is True


# --- The agent alone ---------------------------------------------------------------------------


def _keep_running_agent(
    tmp_path,
    toolchain,
    monkeypatch,
    start_agent,
    leftovers,
    *arguments,
    beats=False,
    environment=None,
    keep_running=True,
):
    """An agent with a launch that keeps running and whose tool blocks. Return the agent, the
    state of the launch and the beats (or `None`). With `environment` (a `Provisioned`), the agent
    runs from that environment and code copy."""
    tool = BlockingTool(tmp_path, monkeypatch)
    if environment is None:
        agent = start_agent(*arguments, env=dict(os.environ))
    else:
        agent = environment.start(start_agent, *arguments)
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    beating = Beats(agent) if beats else None
    message = launch_message(tmp_path, "run-1", path_setup(toolchain))
    agent.send({**message, "keep_running": keep_running})
    agent.tool_pids.append(tool.wait(agent))
    state = json.loads((workspace(tmp_path) / "launches" / "run-1" / "state.json").read_text())
    leftovers.append(state["pid"])  # the test stops the launch if the agent did not
    return agent, state, beating


def _alive_and_holding(tmp_path: Path, state: dict[str, Any]) -> None:
    assert psutil.Process(state["pid"]).is_running()
    assert psutil.Process(state["pid"]).status() != psutil.STATUS_ZOMBIE
    with (workspace(tmp_path) / "lock").open("rb") as stream:
        with pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_launch_that_keeps_running_holds_its_environment_and_code_copy_after_the_agent_exits(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers, provisioned
):
    agent, state, _ = _keep_running_agent(
        tmp_path, toolchain, monkeypatch, start_agent, leftovers, environment=provisioned
    )
    agent.close_input()
    assert agent.process.wait(timeout=TIMEOUT) == 0
    _alive_and_holding(tmp_path, state)
    for directory in (provisioned.env, provisioned.code):
        assert provisioned.lock_is_held(directory)
    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    reasons = {Path(i["path"]).name: i["reason"] for i in report["kept"]}
    assert reasons["e0"] == "in use" and reasons["c0"] == "in use"
    assert not any(item["kind"] in ("env", "code") for item in report["removed"])
    assert (provisioned.env / USE_LOCK).exists() and (provisioned.code / USE_LOCK).exists()
    # The locks end with the launch, also while its tool runs on: the tool inherits none.
    tool = agent.tool_pids[-1]
    # One object for the launch: the system reaps it at once, and then a new object for its PID
    # raises NoSuchProcess. `wait()` of the object returns for a process that is gone.
    launch = psutil.Process(state["pid"])
    launch.kill()
    launch.wait(timeout=TIMEOUT)
    assert psutil.Process(tool).is_running()
    for directory in (provisioned.env, provisioned.code):
        assert not provisioned.lock_is_held(directory)
    with (workspace(tmp_path) / "lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_launch_that_does_not_keep_running_holds_the_locks_when_the_agent_is_killed(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers, provisioned
):
    """An agent that dies (out of memory, for example) cannot stop its launch: the launch holds
    the workspace lock and the use locks, so that `clean` leaves them alone."""
    agent, state, _ = _keep_running_agent(
        tmp_path,
        toolchain,
        monkeypatch,
        start_agent,
        leftovers,
        environment=provisioned,
        keep_running=False,
    )
    agent.process.kill()
    agent.process.wait(timeout=TIMEOUT)
    _alive_and_holding(tmp_path, state)
    for directory in (provisioned.env, provisioned.code):
        assert provisioned.lock_is_held(directory)
    launch = psutil.Process(state["pid"])
    processes = [launch, *(psutil.Process(pid) for pid in agent.tool_pids)]
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess):  # a tool can end with its launch
            process.kill()
    launch.wait(timeout=TIMEOUT)
    for directory in (provisioned.env, provisioned.code):
        assert not provisioned.lock_is_held(directory)


class SlowProfile:
    """A login profile that starts a background program and then blocks, so that the test can act
    while the launch is still in its profile, before the launch entry runs. The profile reports
    the process id of the launch shell (which becomes the launch) and of the background program
    through a FIFO, and it goes on when the test opens a second FIFO. With `detached`, the
    program is not a descendant of the launch, so that a cancel does not reach it."""

    def __init__(self, tmp_path: Path, detached: bool) -> None:
        self.ready = tmp_path / "profile-ready"
        self.go = tmp_path / "profile-go"
        os.mkfifo(self.ready)
        os.mkfifo(self.go)
        if detached:
            # The program leaves the process tree of the launch. The agent samples the descendants
            # of the launch and kills a sampled process that is outside the launch's process group
            # when a cancel ends. So the program must not exist while its ancestor is still alive:
            # the first child waits until its parent has exited, and only then forks the program.
            program = (
                "import os, sys, time\n"
                "me = os.getpid()\n"
                "if os.fork() == 0:\n"
                "    while os.getppid() == me:\n"
                "        time.sleep(0.001)\n"
                "    if os.fork() == 0:\n"
                "        os.setsid()\n"
                "        with open(sys.argv[2], 'w') as ready:\n"
                "            ready.write(f'{sys.argv[1]} {os.getpid()}')\n"
                "        time.sleep(600)\n"
            )
            start = f'{sys.executable} -c "{program}" "$$" {self.ready}\n'
        else:
            start = f'sleep 600 &\necho "$$ $!" > {self.ready}\n'  # blocks until the test reads
        (tmp_path / "home" / ".bash_profile").write_text(
            start + f"cat {self.go} > /dev/null\n"  # blocks until the test opens the FIFO
        )

    def pids(self) -> tuple[int, int]:
        """Wait until the profile blocks. Return the process ids of the launch and the program."""
        found: "queue.Queue[str]" = queue.Queue()

        def read() -> None:
            with open(self.ready, "rb") as stream:
                found.put(stream.read().decode())

        threading.Thread(target=read, daemon=True).start()
        try:
            launch, program = found.get(timeout=TIMEOUT).split()
        except queue.Empty:
            pytest.fail(f"the profile did not run in {TIMEOUT} s")
        return int(launch), int(program)

    def release(self) -> None:
        """Let the profile go on. Fail if it does not read the FIFO (its shell has died)."""

        def open_go() -> None:
            with open(self.go, "wb"):  # blocks until the profile reads the FIFO
                pass

        opener = threading.Thread(target=open_go, daemon=True)
        opener.start()
        opener.join(timeout=TIMEOUT)
        if opener.is_alive():
            pytest.fail(f"the profile did not read {self.go} in {TIMEOUT} s")


def _all_locks_held(tmp_path: Path, provisioned: Any) -> bool:
    return all(
        not file_is_unlocked(path)
        for path in (
            workspace(tmp_path) / "lock",
            provisioned.env / USE_LOCK,
            provisioned.code / USE_LOCK,
        )
    )


def _no_lock_held(tmp_path: Path, provisioned: Any) -> bool:
    return all(
        file_is_unlocked(path)
        for path in (
            workspace(tmp_path) / "lock",
            provisioned.env / USE_LOCK,
            provisioned.code / USE_LOCK,
        )
    )


@pytest.mark.parametrize("keep_running", [True, False])
def test_the_locks_of_a_launch_stay_held_when_the_agent_dies_during_its_profile(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers, provisioned, keep_running
):
    """The locks are in flight in a socket while the profile runs: they stay held without the
    agent, they pass to the launch entry, and they end with the launch, not with the program that
    the profile left in the background."""
    profile = SlowProfile(tmp_path, detached=False)
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = provisioned.start(start_agent)
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(
        {**launch_message(tmp_path, "run-1", path_setup(toolchain)), "keep_running": keep_running}
    )
    launch_pid, program_pid = profile.pids()
    leftovers.append(launch_pid)
    leftovers.append(program_pid)
    agent.process.kill()
    agent.process.wait(timeout=TIMEOUT)
    assert _all_locks_held(tmp_path, provisioned)
    profile.release()
    leftovers.append(tool._pid.get(timeout=TIMEOUT))  # the launch entry runs the tool now
    assert _all_locks_held(tmp_path, provisioned)
    launch = psutil.Process(launch_pid)
    launch.kill()
    launch.wait(timeout=TIMEOUT)
    assert psutil.Process(program_pid).is_running()
    assert _no_lock_held(tmp_path, provisioned)


def test_a_launch_canceled_in_its_profile_releases_the_locks_that_a_background_program_could_hold(
    tmp_path, toolchain, start_agent, leftovers, provisioned
):
    """The launch entry never took the locks, and a program of the profile outside the process
    group of the launch holds the end of the socket that they wait in. The agent takes them from
    the socket when the launch has ended."""
    profile = SlowProfile(tmp_path, detached=True)
    agent = provisioned.start(start_agent)
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    launch_pid, program_pid = profile.pids()
    leftovers.append(launch_pid)
    leftovers.append(program_pid)
    agent.writer.send(CANCEL)
    assert agent.get("result", whole=True)["canceled"] is True
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0
    assert psutil.Process(program_pid).is_running()
    assert _no_lock_held(tmp_path, provisioned)


def test_the_agent_leaves_a_launch_that_keeps_running_when_the_lease_ends(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers
):
    agent, state, beats = _keep_running_agent(
        tmp_path, toolchain, monkeypatch, start_agent, leftovers, "--lease", "1", beats=True
    )
    assert beats is not None
    beats.stop()  # no heartbeat any more: the lease ends
    assert agent.process.wait(timeout=TIMEOUT) == 0
    assert not any("canceling" in line for line in agent.lines)
    _alive_and_holding(tmp_path, state)
    assert state["keep_running"] is True and state["state"] == "running"


def test_the_agent_leaves_a_launch_that_keeps_running_at_the_end_of_its_input(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers
):
    agent, state, _ = _keep_running_agent(tmp_path, toolchain, monkeypatch, start_agent, leftovers)
    agent.close_input()
    assert agent.process.wait(timeout=TIMEOUT) == 0
    _alive_and_holding(tmp_path, state)


def test_a_cancel_frame_still_cancels_a_launch_that_keeps_running(
    tmp_path, toolchain, monkeypatch, start_agent, leftovers
):
    agent, state, beats = _keep_running_agent(
        tmp_path, toolchain, monkeypatch, start_agent, leftovers, beats=True
    )
    agent.writer.send(CANCEL)
    assert agent.get("result", whole=True)["canceled"] is True
    assert beats is not None
    beats.stop()
    _assert_gone(state["pid"])  # the agent has seen the launch end


# --- Stopping what a leader left behind -------------------------------------------------------


def _session(tmp_path: Path) -> Any:
    """A session object that is only used to stop a process: no client, no output."""
    from xeda.remote import agent as agent_module
    from xeda.remote.frames import FrameWriter

    client = agent_module._Client(io.BytesIO(b""))
    out = agent_module._Output(FrameWriter(io.BytesIO()), 1.0)
    return agent_module.Session(
        tmp_path / "remote", "0123456789abcdef01234567", client, out, grace=(0.2, 0.2)
    )


def _leader(program: str, leftovers: _Processes) -> tuple["subprocess.Popen[str]", int]:
    """Start `program` in a session of its own, as the agent starts a launch. It prints the
    process id of a process that it started and then does what the test needs. Return the leader
    and that process id."""
    leader = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    leftovers.append(leader.pid)
    other = int(leader.stdout.readline())
    leftovers.append(other)
    return leader, other


def test_cancel_stops_a_process_that_its_leader_left_in_the_group(tmp_path, leftovers):
    """The leader has gone when the stop begins: no child of it is left to find."""
    program = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print(p.pid, flush=True)\n"
    )
    leader, grandchild = _leader(program, leftovers)
    leader.wait(TIMEOUT)  # the leader exits right after it started the process
    assert psutil.pid_exists(grandchild) and leader.poll() == 0
    assert _session(tmp_path).stop_launch(leader, None, "test") == 0
    _assert_gone(grandchild)


def test_cancel_stops_a_process_that_was_seen_while_the_launch_ran(tmp_path, leftovers):
    """The process left the group (a tool that starts a session of its own), and its leader is
    gone: only the descendants that the agent saw while the launch ran lead to it."""
    program = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
        " start_new_session=True)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    leader, grandchild = _leader(program, leftovers)
    session = _session(tmp_path)
    session.watch_descendants(leader)  # each tick of the watch does this
    leader.kill()
    leader.wait(TIMEOUT)
    assert session.stop_launch(leader, None, "test") == 0
    _assert_gone(grandchild)


# --- Attach -------------------------------------------------------------------------------------


def test_attach_replays_the_log_waits_and_fetches(
    tmp_path, remote_host, monkeypatch, cleanup, capsys
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    outcome, tool = _cut_while_running(tmp_path, _design(tmp_path), gate, connections, remote_host)
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached)
    (directory,) = _launch_dirs(remote_host)
    capsys.readouterr()
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere" / "top"
    assert not (mirror / "nextpnr" / "results.json").exists()

    thread, attached = _run_in_thread(lambda: _attach(tmp_path))
    gate.release()  # the attach is running or about to: the tool goes on in both cases
    thread.join(TIMEOUT)
    result = attached.get(timeout=TIMEOUT)
    assert result.error is None and result.success, result.document
    assert result.document["launch"] == {"id": directory.name, "host": "somewhere"}
    assert _states(result.document)["nextpnr"] == "ran"
    assert (mirror / "nextpnr" / "config.txt").is_file()
    written = json.loads((mirror / "nextpnr" / "results.json").read_text())
    assert written["outcome"] == "passed"
    assert str(remote_host.home) not in (mirror / "nextpnr" / "results.json").read_text()
    assert "nextpnr-ecp5" in capsys.readouterr().out  # the log, from its start
    # The record names the original request: the mirror gets the local paths.
    assert (mirror.parent / ".launches" / f"{directory.name}.json").is_file()


def test_attach_without_an_id_says_which_launch_it_took(tmp_path, remote_host, caplog):
    caplog.set_level("INFO", logger="xeda.remote")
    design = _design(tmp_path)
    document = _succeeded(_keep_running(tmp_path, design))
    assert _states(document)["nextpnr"] == "ran"
    (directory,) = _launch_dirs(remote_host)

    outcome = _attach(tmp_path)

    assert outcome.error is None and outcome.success, outcome.document
    assert outcome.document["launch"]["id"] == directory.name
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"No launch runs on somewhere. Showing the newest launch, {directory.name}" in messages


def test_attach_delivers_nothing_and_names_the_original_command(
    tmp_path, remote_host, monkeypatch, cleanup, caplog
):
    caplog.set_level("INFO", logger="xeda.remote")
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    outputs = tmp_path / "out"
    outcome, tool = _cut_while_running(
        tmp_path, _design(tmp_path), gate, connections, remote_host, outputs_to=outputs
    )
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached)
    thread, attached = _run_in_thread(lambda: _attach(tmp_path))
    gate.release()
    thread.join(TIMEOUT)
    result = attached.get(timeout=TIMEOUT)
    assert result.error is None and result.success, result.document
    assert not outputs.exists()
    assert "Run the original command again to deliver" in caplog.text


def test_attach_to_a_launch_without_a_local_record_says_what_to_do(tmp_path, remote_host):
    _succeeded(_keep_running(tmp_path, _design(tmp_path)))
    records = tmp_path / "local" / "xeda_run" / "@somewhere" / ".launches"
    for record in records.glob("*.json"):
        record.unlink()
    outcome = _attach(tmp_path)
    assert outcome.error is not None and not outcome.success
    assert "no record of the launch" in outcome.document["error"]["message"]
    assert "Run the original command again" in outcome.document["error"]["message"]


def test_attach_refuses_the_files_of_a_launch_that_a_later_launch_replaced(tmp_path, remote_host):
    design = _design(tmp_path)
    _succeeded(_keep_running(tmp_path, design))
    (first,) = _launch_dirs(remote_host)
    _succeeded(_keep_running(tmp_path, _design(tmp_path, freq="90 MHz")))
    outcome = _attach(tmp_path, first.name)
    assert outcome.error is not None
    assert "used the same inputs after launch" in outcome.document["error"]["message"]


def test_the_order_of_execution_decides_replacement_and_the_newest_launch(
    tmp_path, remote_host, monkeypatch
):
    """The ids are made before the wait for the workspace lock, so an id that sorts later can
    belong to a launch that ran earlier."""
    ids = iter(["300101000000-ffffffff", "300101000000-00000000"])
    monkeypatch.setattr(client, "new_launch_id", lambda: next(ids))
    _succeeded(_keep_running(tmp_path, _design(tmp_path)))
    _succeeded(_keep_running(tmp_path, _design(tmp_path, freq="90 MHz")))

    outcome = _attach(tmp_path, "300101000000-ffffffff")
    assert outcome.error is not None
    assert "used the same inputs after launch" in outcome.document["error"]["message"]

    newest = _attach(tmp_path)
    assert newest.error is None and newest.success, newest.document
    assert newest.document["launch"]["id"] == "300101000000-00000000"


# --- A second request ---------------------------------------------------------------------------


def test_a_second_identical_request_attaches_to_the_running_launch(
    tmp_path, remote_host, monkeypatch, cleanup, watch
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    design = _design(tmp_path)
    outcome, tool = _cut_while_running(tmp_path, design, gate, connections, remote_host)
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached)
    (directory,) = _launch_dirs(remote_host)
    seen = watch("The same request runs on somewhere")

    thread, again = _run_in_thread(lambda: _keep_running(tmp_path, design))
    message = seen.wait()
    assert directory.name in message
    gate.release()
    thread.join(TIMEOUT)
    result = again.get(timeout=TIMEOUT)

    assert result.error is None and result.success, result.document
    assert _states(result.document)["nextpnr"] == "ran"
    assert _launch_dirs(remote_host) == [directory]  # no second launch
    assert gate.pids == [tool]


def test_a_different_request_waits_for_the_lock_of_the_launch_that_keeps_running(
    tmp_path, remote_host, monkeypatch, cleanup, watch
):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    outcome, tool = _cut_while_running(tmp_path, _design(tmp_path), gate, connections, remote_host)
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached)
    (first,) = _launch_dirs(remote_host)
    waiting = watch("waiting for the workspace lock")

    thread, second = _run_in_thread(
        lambda: _keep_running(tmp_path, _design(tmp_path, freq="90 MHz"))
    )
    message = waiting.wait()
    assert f"launch {first.name}" in message
    assert _launch_dirs(remote_host) == [first]  # the second request has not launched

    gate.release()  # the first tool goes on, and the first launch ends
    assert _wait_for_launch(remote_host, first)["success"] is True
    cleanup.append(gate.pid())  # the second launch starts its tool
    gate.release()
    thread.join(TIMEOUT)
    result = second.get(timeout=TIMEOUT)
    assert result.error is None and result.success, result.document
    assert len(_launch_dirs(remote_host)) == 2


# --- Cancel -------------------------------------------------------------------------------------


def test_cancel_stops_the_launch_and_its_descendants(tmp_path, remote_host, monkeypatch, cleanup):
    gate = Gate(tmp_path, remote_host)
    connections = Connections(monkeypatch)
    outcome, tool = _cut_while_running(tmp_path, _design(tmp_path), gate, connections, remote_host)
    cleanup.append(tool)
    assert isinstance(outcome.error, RemoteDetached)
    (directory,) = _launch_dirs(remote_host)
    launch = psutil.Process(_launch_state(directory)["pid"])
    assert tool in [child.pid for child in launch.children(recursive=True)]

    document = cancel_launch("somewhere", None, run_root=tmp_path / "local" / "xeda_run")

    assert document == {
        "success": True,
        "host": "somewhere",
        "launch": directory.name,
        "survivors": 0,
    }
    _assert_gone(tool)
    _assert_gone(launch.pid)
    assert _launch_state(directory)["state"] == "canceled"
    assert not _lock_is_held(remote_host)
    # An attach afterwards reports the cancel.
    again = _attach(tmp_path, directory.name)
    assert isinstance(again.error, RemoteCanceled), again.document


def test_cancel_reaches_a_session_that_still_follows(tmp_path, remote_host, cleanup, watch):
    gate = Gate(tmp_path, remote_host)
    thread, following = _run_in_thread(lambda: _keep_running(tmp_path, _design(tmp_path)))
    tool = gate.pid()
    cleanup.append(tool)
    document = cancel_launch("somewhere", None, run_root=tmp_path / "local" / "xeda_run")
    assert document["success"] is True
    thread.join(TIMEOUT)
    outcome = following.get(timeout=TIMEOUT)
    assert isinstance(outcome.error, RemoteCanceled), outcome.document
    _assert_gone(tool)


def test_cancel_without_a_running_launch_says_so(tmp_path, remote_host):
    _succeeded(_keep_running(tmp_path, _design(tmp_path)))
    result = CliRunner().invoke(
        cli, ["remote", "cancel", "somewhere", "--json", "--run-root", _run_root(tmp_path)]
    )
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["success"] is False
    assert "No launch runs in this workspace" in document["error"]["message"]


# --- The warning and the command line -----------------------------------------------------------


def _fake_loginctl(remote_host: RemoteHost, answer: str) -> None:
    """A `loginctl` that answers as the bootstrap script asks (`KillUserProcesses=yes`) and as
    the agent asks (`yes`)."""
    loginctl = remote_host.remote.bin / "loginctl"
    loginctl.write_text(
        f'#!/bin/sh\ncase "$*" in *--value*) echo {answer} ;; *) echo KillUserProcesses={answer} ;; esac\n'
    )
    loginctl.chmod(0o755)


def test_a_run_that_keeps_running_warns_when_a_logout_may_stop_it(tmp_path, remote_host, caplog):
    _fake_loginctl(remote_host, "yes")
    caplog.set_level("INFO", logger="xeda.remote")
    _succeeded(_keep_running(tmp_path, _design(tmp_path)))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("KillUserProcesses=yes" in text and "may still stop" in text for text in warnings)


def test_a_run_that_keeps_running_does_not_warn_when_logout_keeps_it(tmp_path, remote_host, caplog):
    _fake_loginctl(remote_host, "no")
    caplog.set_level("INFO", logger="xeda.remote")
    _succeeded(_keep_running(tmp_path, _design(tmp_path)))
    assert not [r for r in caplog.records if "KillUserProcesses" in r.getMessage()]


def test_a_run_without_keep_running_does_not_warn(tmp_path, remote_host, caplog):
    _fake_loginctl(remote_host, "yes")
    caplog.set_level("INFO", logger="xeda.remote")
    _succeeded(remote_request(tmp_path, _design(tmp_path), "nextpnr"))
    assert not [r for r in caplog.records if "KillUserProcesses" in r.getMessage()]
    assert not list((tmp_path / "local" / "xeda_run" / "@somewhere").glob(".launches"))


def _run_root(tmp_path: Path) -> str:
    return str(tmp_path / "local" / "xeda_run")


@pytest.mark.parametrize(
    ("extra", "text"),
    [(["--dry-run", "--remote", "somewhere"], "no launch to keep running"), ([], "needs --remote")],
)
def test_keep_running_needs_a_remote_run_and_a_real_run(tmp_path, extra, text):
    design = tmp_path / "top.yaml"
    design.write_text("name: top\n")
    result = CliRunner().invoke(cli, ["run", "nextpnr", str(design), "--keep-running", *extra])
    assert result.exit_code == 2 and text in result.output


@pytest.mark.parametrize("command", ["attach", "cancel"])
def test_the_new_commands_report_a_failure_as_one_json_document(tmp_path, remote_host, command):
    result = CliRunner().invoke(
        cli, ["remote", command, "somewhere", "--json", "--run-root", _run_root(tmp_path)]
    )
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["success"] is False and document["error"]["message"]


def test_a_run_that_keeps_running_prints_the_launch_in_its_json_document(tmp_path, remote_host):
    result = CliRunner().invoke(
        cli,
        ["run", "nextpnr", "--design", str(_design(tmp_path)), "--remote", "somewhere"]
        + ["--run-root", _run_root(tmp_path), "--keep-running", "--json"],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["success"] is True and document["remote"] == "somewhere"
    assert client  # the module under test is imported


@pytest.mark.parametrize("linked", [".launches", "mirror"])
def test_the_launch_record_is_never_written_through_a_link(tmp_path, linked):
    from xeda.remote.launch_record import LaunchRecord, write_record

    run_root = tmp_path / "xeda_run"
    mirror_root = run_root / "@host"
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked == "mirror":
        run_root.mkdir()
        mirror_root.symlink_to(outside, target_is_directory=True)
    else:
        mirror_root.mkdir(parents=True)
        (mirror_root / ".launches").symlink_to(outside, target_is_directory=True)
    record = LaunchRecord(
        id="261020035000-0123abcd",
        host="host",
        chain="yosys",
        flow="yosys",
        requested="yosys",
        design_name="d",
        design=None,
        target=None,
        request=[],
        local_root=str(run_root),
        run_paths=[],
        remote_run_root="/r/xeda_run",
        inputs_dir="/r/inputs/d",
        files={},
        generated=[],
        design_root=str(tmp_path),
        fetch_all=False,
        delivers=False,
        debug=False,
    )
    with pytest.raises(RemoteError, match="cannot be written"):
        write_record(mirror_root, record)
    assert list(outside.iterdir()) == []
