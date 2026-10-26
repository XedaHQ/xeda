"""The remote agent syncs the inputs, supervises a launch, and sends back the selected files.

Each test runs `python -m xeda.remote.agent` as a subprocess of the test interpreter over pipes,
as the local xeda runs it over SSH. The launch is the real launch entry with the fake FPGA tools.
A tool that must be running when a test cancels it reports its process id through a FIFO and
blocks (`XEDA_FAKE_FPGA_READY`), so no test waits for a time to pass. Every test stops its
processes by process id.
"""

import contextlib
import errno
import fcntl
import io
import json
import os
import queue
import subprocess
import sys
import sysconfig
import threading
import time
import venv
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import psutil
import pytest

import xeda
from xeda.flow_runner import DefaultRunner
from xeda.remote import agent as agent_module
from xeda.remote.constants import CANCEL_GRACE, LEASE
from xeda.remote.frames import (
    BEAT,
    CANCEL,
    DATA,
    END,
    JSON,
    LOG,
    FrameWriter,
    decode_json,
    read_frame,
    skip_to_magic,
)
from xeda.remote.launch_entry import LaunchRequest
from xeda.remote.manifest import Entry, build_manifest, copy_verified, iter_tar, write_tar

from .tool_utils import use_fake_fpga_tools

pytestmark = [pytest.mark.python_compat, pytest.mark.skipif(os.name != "posix", reason="POSIX")]

KEY = "0123456789abcdef01234567"
PART = "LFE5U-25F-6BG381C"
TIMEOUT = 120

DESIGN = {
    "top.v": b"module top(input clk, output q); assign q = clk; endmodule\n",
    "top.yaml": (
        "name: top\n"
        "rtl:\n  sources: [top.v]\n  top: top\n  clock.port: clk\n"
        f"flows:\n  nextpnr:\n    fpga.part: {PART}\n    clock.freq: 100 MHz\n"
    ).encode(),
}


class Agent:
    """A running agent and the client side of its pipes."""

    def __init__(
        self,
        tmp_path: Path,
        root: Path,
        *arguments: str,
        key: str = KEY,
        env: Optional[dict[str, str]] = None,
        noise: bool = False,
        name: str = "agent",
        python: str = sys.executable,
    ) -> None:
        command = [python, "-m", "xeda.remote.agent", "session", str(root), key, *arguments]
        if noise:
            command = ["sh", "-c", 'echo "Welcome to the host"; exec "$@"', "sh", *command]
        self.stderr_path = tmp_path / f"{name}.stderr"
        self.stderr = self.stderr_path.open("wb")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            env=env,
            cwd=tmp_path,
            start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.writer = FrameWriter(self.process.stdin)
        self.noise = skip_to_magic(self.process.stdout)
        self.lines: list[str] = []
        self.others: list[dict[str, Any]] = []
        self.tool_pids: list[int] = []
        self._frames: "queue.Queue[Optional[tuple[bytes, bytes]]]" = queue.Queue()
        self._gate = threading.Event()
        self.start_reading()

    def start_reading(self) -> None:
        """Take the frames of the agent in a thread. The gate stops it between frames."""
        self._gate.set()
        threading.Thread(target=self._read, daemon=True).start()

    def pause_reading(self) -> None:
        """Stop taking frames after the one that the thread has now: the agent's output fills the
        pipe."""
        self._gate.clear()

    def error(self) -> dict[str, Any]:
        """Read frames until the `error` document of the agent."""
        while True:
            kind, payload = self.frame()
            if kind == JSON and "error" in (document := decode_json(payload)):
                return document["error"]

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                self._gate.wait()
                if not (frame := read_frame(self.process.stdout)):
                    break
                self._frames.put(frame)
        except (OSError, ValueError):
            pass
        self._frames.put(None)

    def diagnostics(self) -> str:
        self.stderr.flush()
        return f"stderr of the agent:\n{self.stderr_path.read_text(errors='replace')}"

    def frame(self) -> tuple[bytes, bytes]:
        try:
            item = self._frames.get(timeout=TIMEOUT)
        except queue.Empty:
            pytest.fail(f"no frame from the agent in {TIMEOUT} s\n{self.diagnostics()}")
        if item is None:
            pytest.fail(f"the agent ended its output\n{self.diagnostics()}")
        return item

    def get(self, key: str, whole: bool = False) -> Any:
        """Read frames until the JSON document that has `key`, and return its value (or, with
        `whole`, the document). The log lines
        on the way go to `lines`, other documents to `others`."""
        while True:
            kind, payload = self.frame()
            if kind == LOG:
                self.lines.append(payload.decode(errors="replace"))
            elif kind == JSON:
                document = decode_json(payload)
                if key in document:
                    return document if whole else document[key]
                if "error" in document:
                    pytest.fail(f"error from the agent: {document['error']}\n{self.diagnostics()}")
                self.others.append(document)
            else:
                pytest.fail(f"unexpected frame {kind!r}")

    def line(self, text: str) -> str:
        """Read frames until a log line with `text` and return it."""
        for line in self.lines:
            if text in line:
                return line
        while True:
            kind, payload = self.frame()
            if kind == LOG:
                self.lines.append(payload.decode(errors="replace"))
                if text in self.lines[-1]:
                    return self.lines[-1]
            elif kind == JSON:
                self.others.append(decode_json(payload))

    def send(self, document: Any) -> None:
        self.writer.send_json(document)

    def sync(self, files: dict[str, bytes], launch_id: str, design: str = "top") -> Any:
        """Send a sync and the files that the agent needs. Return its `synced` answer."""
        entries = build_manifest({}, generated=files)
        self.send(
            {
                "sync": {
                    "workspace": KEY,
                    "design": design,
                    "entries": [entry.as_json_value() for entry in entries],
                    "id": launch_id,
                }
            }
        )
        self.send_needed(entries, files)
        return self.get("synced")

    def send_needed(self, entries: list[Entry], files: dict[str, bytes]) -> list[str]:
        need = self.get("need")
        wanted = [entry for entry in entries if entry.path in need]
        buffer = io.BytesIO()
        write_tar(wanted, {}, files, buffer)  # type: ignore[arg-type]
        buffer.seek(0)
        self.writer.send_stream(buffer)
        return need

    def close_input(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()

    def stop(self) -> None:
        """Stop the agent, everything it started and the tools, by process id."""
        pids = list(self.tool_pids)
        try:
            pids += [p.pid for p in psutil.Process(self.process.pid).children(recursive=True)]
        except psutil.Error:
            pass
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        self.process.kill()
        self.process.wait(timeout=60)
        for stream in (self.process.stdin, self.process.stdout, self.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BrokenPipeError:
                    # The close of stdin flushes what the client wrote last, for example a
                    # heartbeat, and the agent that would read it has exited.
                    pass


@pytest.fixture
def start_agent(tmp_path: Path) -> Iterator[Callable[..., Agent]]:
    agents: list[Agent] = []

    def start(*arguments: str, **keywords: Any) -> Agent:
        keywords.setdefault("name", f"agent{len(agents)}")
        agent = Agent(tmp_path, tmp_path / "remote", *arguments, **keywords)
        agents.append(agent)
        return agent

    yield start
    for agent in agents:
        agent.stop()


@pytest.fixture
def toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fake FPGA tools, first on `PATH`, and a login shell that reads nothing of the user's."""
    use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "nextpnr-ecp5")
    return tmp_path / "toolchain" / "bin"


def workspace(tmp_path: Path) -> Path:
    return tmp_path / "remote" / "v1" / "ws" / KEY


def launch_message(
    tmp_path: Path, launch_id: str, setup: list[str], **fields: Any
) -> dict[str, Any]:
    """The `launch` message for the design that a sync wrote to the workspace."""
    ws = workspace(tmp_path)
    plan = DefaultRunner(tmp_path / "local_root").plan(
        "nextpnr", design=ws / "inputs" / "top" / "top.yaml"
    )
    request = LaunchRequest(
        chain="nextpnr",
        design_file="top.yaml",
        inputs_dir=str(ws / "inputs" / "top"),
        run_root=str(ws / "xeda_run"),
        path_identities={},
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        log_level=20,
        detailed_logs=False,
        expected={node.name: node.flowrun_hash for node in plan.nodes},
    ).model_copy(update=fields)
    return {"launch": request.model_dump(mode="json"), "id": launch_id, "setup": setup}


def path_setup(toolchain: Path) -> list[str]:
    return [f"export PATH={toolchain}:$PATH"]


class BlockingTool:
    """The environment that makes the fake `nextpnr-ecp5` report its process id and block."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore: bool = False):
        self.fifo = tmp_path / "ready"
        os.mkfifo(self.fifo)
        monkeypatch.setenv("XEDA_FAKE_FPGA_READY", str(self.fifo))
        if ignore:
            monkeypatch.setenv("XEDA_FAKE_FPGA_IGNORE_SIGNALS", "1")
        self._pid: "queue.Queue[int]" = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        with open(self.fifo, "rb") as stream:  # blocks until the tool opens the FIFO to write
            self._pid.put(int(stream.read()))

    def wait(self, agent: Agent) -> int:
        """Return the process id of the tool, when it is running."""
        for _ in range(TIMEOUT):
            try:
                pid = self._pid.get(timeout=1)
            except queue.Empty:
                if agent.process.poll() is not None:
                    pytest.fail(f"the agent ended\n{agent.diagnostics()}")
                continue
            agent.tool_pids.append(pid)
            return pid
        pytest.fail(f"the tool did not start\n{agent.diagnostics()}")


class Beats:
    """Send heartbeats from a thread until stopped."""

    def __init__(self, agent: Agent, interval: float = 0.1) -> None:
        self._stop = threading.Event()

        def beat() -> None:
            while not self._stop.wait(interval):
                try:
                    agent.writer.send(BEAT)
                except (OSError, ValueError):
                    return

        threading.Thread(target=beat, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def descendants(agent: Agent) -> list[psutil.Process]:
    return psutil.Process(agent.process.pid).children(recursive=True)


def assert_gone(processes: list[psutil.Process]) -> None:
    _gone, alive = psutil.wait_procs(processes, timeout=30)
    alive = [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]
    assert not alive, f"still running: {alive}"


def fetched(
    agent: Agent, fetch_all: bool = False, skipped: Optional[list] = None
) -> dict[str, bytes]:
    """Fetch and return the content of every file that comes back. `skipped` gets the sources
    that the agent did not send, because a link leads them out of their run directory."""
    agent.send({"fetch": {"fetch_all": fetch_all}})
    listing = agent.get("files", whole=True)
    assert set(listing) == {"files", "skipped"}
    if skipped is not None:
        skipped.extend(listing["skipped"])
    entries = [Entry.from_json_value(item) for item in listing["files"]]
    pending: "queue.Queue[tuple[bytes, bytes]]" = queue.Queue()

    class Source(io.RawIOBase):
        def __init__(self) -> None:
            super().__init__()
            self.rest = memoryview(b"")
            self.done = False

        def readable(self) -> bool:
            return True

        def readinto(self, buffer: Any) -> int:
            while not self.rest and not self.done:
                kind, payload = agent.frame()
                if kind == DATA:
                    self.rest = memoryview(payload)
                elif kind == END:
                    self.done = True
                else:
                    pending.put((kind, payload))
            count = min(len(buffer), len(self.rest))
            buffer[:count] = self.rest[:count]
            self.rest = self.rest[count:]
            return count

    content: dict[str, bytes] = {}
    for entry, source in iter_tar(entries, io.BufferedReader(Source())):  # type: ignore[arg-type]
        out = io.BytesIO()
        copy_verified(entry, source, out)  # type: ignore[arg-type]
        content[entry.path] = out.getvalue()
    return content


# ---------------------------------------------------------------------------------------------


def test_session_syncs_launches_streams_and_fetches(tmp_path, toolchain, start_agent):
    agent = start_agent(env=dict(os.environ))
    hello = agent.get("hello")
    assert hello["xeda"] == xeda.__version__ and hello["pid"] == agent.process.pid
    assert agent.sync(DESIGN, "launch-1") == {"written": 2, "removed": 0}
    inputs = workspace(tmp_path) / "inputs" / "top"
    assert (inputs / "top.v").read_bytes() == DESIGN["top.v"]
    agent.send(launch_message(tmp_path, "launch-1", path_setup(toolchain)))
    result = agent.get("result", whole=True)
    assert result["canceled"] is False and result["exit"] == 0 and result["survivors"] == 0
    assert result["result"]["outcome"] == "passed"
    assert any("Running" in line or "nextpnr" in line for line in agent.lines), agent.lines
    launch_dir = workspace(tmp_path) / "launches" / "launch-1"
    state = json.loads((launch_dir / "state.json").read_text())
    assert state["state"] == "finished" and state["id"] == "launch-1" and state["exit"] == 0
    assert json.loads((launch_dir / "result.json").read_text()) == result["result"]
    assert (launch_dir / "request.json").is_file() and (launch_dir / "log").is_file()
    files = fetched(agent)
    assert "top/nextpnr/results.json" in files and "top/nextpnr/settings.json" in files
    assert not any(path.endswith("trace.json") for path in files)
    assert json.loads(files["top/nextpnr/results.json"])["outcome"] == "passed"
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_a_fetch_follows_a_link_only_inside_the_run_directory(tmp_path, toolchain, start_agent):
    """A tool may leave a link at the name of an artifact. One that leads to a file of the run
    directory brings that file back under its name; one that leads out of it brings nothing, and
    the agent says so, with where it leads."""
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "launch-1")
    agent.send(launch_message(tmp_path, "launch-1", path_setup(toolchain)))
    result = agent.get("result", whole=True)
    assert result["result"]["outcome"] == "passed"
    (node,) = [node for node in result["result"]["nodes"] if node["name"] == "nextpnr"]
    run_path = Path(node["run_dir"])
    artifacts = node["record"]["artifacts"]
    leads_out, leads_in = artifacts["log"], artifacts["report"]
    secret = tmp_path / "secret.txt"
    secret.write_text("not the run's\n")
    # The fetch takes the names from the record of the launch: a tool left links at two of them.
    (run_path / leads_out).unlink()
    (run_path / leads_out).symlink_to(secret)
    (run_path / leads_in).unlink()
    (run_path / leads_in).symlink_to(run_path / "settings.json")
    skipped: list = []
    files = fetched(agent, skipped=skipped)
    assert files[f"top/nextpnr/{leads_in}"] == (run_path / "settings.json").read_bytes()
    assert f"top/nextpnr/{leads_out}" not in files
    assert not [content for content in files.values() if b"not the run's" in content]
    assert skipped == [
        {
            "node": "nextpnr",
            "place": leads_out,
            "source": str(run_path / leads_out),
            "target": os.path.realpath(secret),
        }
    ]
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_second_session_syncs_only_what_changed(tmp_path, toolchain, start_agent):
    first = start_agent(env=dict(os.environ))
    first.get("hello")
    assert first.sync(DESIGN, "a")["written"] == 2
    first.send({"bye": True})
    assert first.process.wait(timeout=TIMEOUT) == 0
    unchanged = (workspace(tmp_path) / "inputs" / "top" / "top.yaml").stat().st_ino

    second = start_agent(env=dict(os.environ))
    second.get("hello")
    edited = {**DESIGN, "top.v": b"module top(input clk, output q); assign q = ~clk; endmodule\n"}
    assert second.sync(edited, "b") == {"written": 1, "removed": 0}
    inputs = workspace(tmp_path) / "inputs" / "top"
    assert (inputs / "top.v").read_bytes() == edited["top.v"]
    assert (inputs / "top.yaml").stat().st_ino == unchanged  # untouched
    second.send({"bye": True})
    assert second.process.wait(timeout=TIMEOUT) == 0

    third = start_agent(env=dict(os.environ))
    third.get("hello")
    assert third.sync({"top.yaml": DESIGN["top.yaml"]}, "c") == {"written": 0, "removed": 1}
    assert not (inputs / "top.v").exists()  # a file that left the manifest is gone
    third.send({"bye": True})
    assert third.process.wait(timeout=TIMEOUT) == 0


def test_cancel_stops_the_launch_and_every_descendant(
    tmp_path, toolchain, monkeypatch, start_agent
):
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool_pid = tool.wait(agent)
    found = descendants(agent)
    assert tool_pid in [p.pid for p in found] and len(found) >= 2  # the launch and the tool
    agent.writer.send(CANCEL)
    result = agent.get("result", whole=True)
    assert result["canceled"] is True
    assert result["result"]["error"]["code"] == "canceled" and result["exit"] == 130
    assert_gone(found)
    state = json.loads((workspace(tmp_path) / "launches" / "run-1" / "state.json").read_text())
    assert state["state"] == "canceled"
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_a_tool_that_ignores_signals_is_killed_after_the_grace_periods(
    tmp_path, toolchain, monkeypatch, start_agent
):
    tool = BlockingTool(tmp_path, monkeypatch, ignore=True)
    agent = start_agent("--grace", "0.5,0.5", env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool_pid = tool.wait(agent)
    found = descendants(agent)
    assert tool_pid in [p.pid for p in found]
    agent.writer.send(CANCEL)
    result = agent.get("result", whole=True)
    assert result["canceled"] is True
    assert_gone(found)


def test_eof_on_stdin_cancels(tmp_path, toolchain, monkeypatch, start_agent):
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool.wait(agent)
    found = descendants(agent)
    agent.close_input()
    result = agent.get("result", whole=True)  # the output still works: the agent says what happened
    assert result["canceled"] is True
    assert_gone(found)
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_missing_heartbeat_cancels_after_the_lease(tmp_path, toolchain, monkeypatch, start_agent):
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent("--lease", "1", env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    beats = Beats(agent)  # the start of the launch takes longer than the lease
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool.wait(agent)
    beats.stop()
    found = descendants(agent)
    result = agent.get("result", whole=True)  # no heartbeat comes after this
    assert result["canceled"] is True
    assert any("lease" in line for line in agent.lines), agent.lines
    assert_gone(found)


def test_heartbeats_keep_a_launch_alive_past_the_lease(
    tmp_path, toolchain, monkeypatch, start_agent
):
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent("--lease", "1", env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    beats = Beats(agent)
    agent.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool.wait(agent)
    try:
        time.sleep(2.5)  # more than twice the lease: the only test that waits for time to pass
        assert all(frame is not None and frame[0] == LOG for frame in list(agent._frames.queue))
        agent.writer.send(CANCEL)
        assert agent.get("result", whole=True)["canceled"] is True
    finally:
        beats.stop()


def test_second_session_waits_for_the_workspace_lock(tmp_path, toolchain, monkeypatch, start_agent):
    tool = BlockingTool(tmp_path, monkeypatch)
    first = start_agent(env=dict(os.environ))
    first.get("hello")
    first.sync(DESIGN, "run-1")
    first.send(launch_message(tmp_path, "run-1", path_setup(toolchain)))
    tool.wait(first)

    second = start_agent("--lock-notice", "0.2", env=dict(os.environ))
    second.get("hello")
    other = {**DESIGN, "top.v": b"module top(input clk, output q); assign q = 1'b0; endmodule\n"}
    entries = build_manifest({}, generated=other)
    second.send(
        {
            "sync": {
                "workspace": KEY,
                "design": "top",
                "entries": [entry.as_json_value() for entry in entries],
                "id": "run-2",
            }
        }
    )
    line = second.line("waiting for the workspace lock")
    assert "launch run-1" in line
    # The second launch changes no input while the first holds the workspace.
    inputs = workspace(tmp_path) / "inputs" / "top"
    assert (inputs / "top.v").read_bytes() == DESIGN["top.v"]
    first.writer.send(CANCEL)
    assert first.get("result", whole=True)["canceled"] is True
    first.send({"bye": True})
    assert first.process.wait(timeout=TIMEOUT) == 0
    assert second.send_needed(entries, other) == ["top.v"]
    assert second.get("synced") == {"written": 1, "removed": 0}
    assert (inputs / "top.v").read_bytes() == other["top.v"]
    second.send({"bye": True})
    assert second.process.wait(timeout=TIMEOUT) == 0


def test_setup_lines_and_profile_env_reach_the_launch(
    tmp_path, toolchain, monkeypatch, start_agent
):
    monkeypatch.setenv("XEDA_FAKE_FPGA_PROFILE", "from-the-profile-env")
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    setup = [
        *path_setup(toolchain),
        "export XEDA_FAKE_FPGA_SETUP='from the setup'",
        "echo setup-line-ran",
        "export PYTHONPATH=/does/not/exist",  # the agent sets it again after the setup lines
    ]
    agent.send(launch_message(tmp_path, "run-1", setup))
    result = agent.get("result", whole=True)
    assert result["result"]["outcome"] == "passed"
    assert "setup-line-ran" in agent.lines
    calls = list(workspace(tmp_path).glob("xeda_run/**/nextpnr/fake_fpga.calls.jsonl"))
    assert len(calls) == 1
    recorded = json.loads(calls[0].read_text().splitlines()[-1])["environment"]
    assert recorded["XEDA_FAKE_FPGA_SETUP"] == "from the setup"
    assert recorded["XEDA_FAKE_FPGA_PROFILE"] == "from-the-profile-env"
    assert Path(recorded["PYTHONPATH"]) == Path(xeda.__file__).resolve().parent.parent


@contextmanager
def background_program(tmp_path: Path) -> Iterator[Path]:
    """The file in which a shell line `sleep 600 & echo $! > <file>` records the process id of
    its background program. The block ends that program, by its process id."""
    pid_file = tmp_path / "background.pid"
    try:
        yield pid_file
    finally:
        if pid_file.is_file():
            with contextlib.suppress(psutil.Error, ValueError):
                psutil.Process(int(pid_file.read_text())).kill()


def file_is_unlocked(path: Path) -> bool:
    """Whether this process can take an exclusive lock on the file `path` now. The file is open
    for writing, as Linux NFS needs for an exclusive lock."""
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@pytest.mark.parametrize("where", ["login profile", "setup line"])
def test_a_background_program_of_the_profile_does_not_keep_the_locks(
    tmp_path, toolchain, start_agent, provisioned, where
):
    """The login profile and the setup lines run before the launch entry. A program that they
    leave in the background lives on after the launch, so it must not inherit any lock."""
    with background_program(tmp_path) as pid_file:
        start = f"sleep 600 &\necho $! > {pid_file}\n"
        setup = path_setup(toolchain)
        if where == "login profile":
            (tmp_path / "home" / ".bash_profile").write_text(start)
        else:
            setup += start.splitlines()
        agent = provisioned.start(start_agent)
        agent.get("hello")
        agent.sync(DESIGN, "run-1")
        agent.send(launch_message(tmp_path, "run-1", setup))
        assert agent.get("result", whole=True)["result"]["outcome"] == "passed"
        agent.send({"bye": True})
        assert agent.process.wait(timeout=TIMEOUT) == 0
        assert psutil.Process(int(pid_file.read_text())).is_running()
        assert file_is_unlocked(workspace(tmp_path) / "lock")
        assert file_is_unlocked(provisioned.env / USE_LOCK)
        assert file_is_unlocked(provisioned.code / USE_LOCK)


def test_a_missing_bash_fails_the_launch_and_names_it(tmp_path, toolchain, start_agent):
    empty = tmp_path / "empty"
    empty.mkdir()
    agent = start_agent(env={**os.environ, "PATH": str(empty)})
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    agent.send(launch_message(tmp_path, "run-1", []))
    error = None
    while error is None:
        kind, payload = agent.frame()
        if kind == JSON and "error" in (document := decode_json(payload)):
            error = document["error"]
    assert "bash" in error["message"]
    assert agent.process.wait(timeout=TIMEOUT) == 1
    assert not (workspace(tmp_path) / "launches" / "run-1").exists()


def test_noise_before_magic_does_not_break_the_client_reader(tmp_path, start_agent):
    agent = start_agent(noise=True, env=dict(os.environ))
    assert agent.noise == b"Welcome to the host\n"
    assert agent.get("hello")["pid"] > 0
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_a_launch_outside_the_workspace_is_refused(tmp_path, toolchain, start_agent):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    message = launch_message(tmp_path, "run-1", path_setup(toolchain))
    message["launch"]["run_root"] = str(tmp_path / "elsewhere")
    agent.send(message)
    with pytest.raises(pytest.fail.Exception, match="run_root"):
        agent.get("result", whole=True)
    assert agent.process.wait(timeout=TIMEOUT) == 1


def test_a_cancel_before_the_launch_ends_the_session(tmp_path, start_agent):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.writer.send(CANCEL)
    assert agent.get("canceled") is True
    assert agent.process.wait(timeout=TIMEOUT) == 0


@pytest.mark.parametrize(
    "arguments", [["--lease", str(LEASE + 1)], ["--grace", f"{CANCEL_GRACE[0] + 1},1"]]
)
def test_the_test_only_periods_refuse_values_above_the_constants(tmp_path, arguments):
    done = subprocess.run(
        [sys.executable, "-m", "xeda.remote.agent", "session", str(tmp_path), KEY, *arguments],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT,
        check=False,
    )
    assert done.returncode == 2 and b"not between" in done.stderr


# ---------------------------------------------------------------------------------------------
# check and clean


def run_command(
    *arguments: str,
    tmp_path: Path,
    python: str = sys.executable,
    environment: Optional[dict[str, str]] = None,
    check: bool = True,
) -> Any:
    """Run a one-shot command of the agent and return its JSON document."""
    done = subprocess.run(
        [python, "-m", "xeda.remote.agent", *arguments],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT,
        env={**os.environ, "HOME": str(tmp_path / "home"), **(environment or {})},
        check=check,
    )
    stream = io.BytesIO(done.stdout)
    skip_to_magic(stream)
    return json.loads(stream.read())


def test_check_reports_the_python_the_disk_and_the_executables(tmp_path):
    (tmp_path / "home").mkdir()
    report = run_command("check", "--json", "bash", "no-such-tool-xeda", tmp_path=tmp_path)
    assert report["python"]["executable"] == sys.executable
    assert report["xeda"] == xeda.__version__
    assert report["disk"]["free"] > 0 and report["kill_user_processes"] in ("yes", "no", "unknown")
    assert report["executables"]["no-such-tool-xeda"] is None
    assert report["executables"]["bash"] and report["bash"]


def test_clean_keeps_what_a_live_lock_or_a_running_launch_holds(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    names = ("idle", "locked", "running")
    for name in names:
        (base / "ws" / name / "inputs").mkdir(parents=True)
        (base / "ws" / name / "lock").write_text("")
        (base / "ws" / name / "inputs" / "f").write_text(name)
    for kind, name in (("env", "unused"), ("env", "used"), ("code", "unused")):
        (base / kind / name).mkdir(parents=True)
        (base / kind / name / USE_LOCK).write_text("")
    (base / "env" / "unused" / "f").write_text("x")
    (base / "ws" / "link").symlink_to(base / "ws" / "idle", target_is_directory=True)

    me = psutil.Process()
    launch = base / "ws" / "running" / "launches" / "x"
    launch.mkdir(parents=True)
    (launch / "state.json").write_text(
        json.dumps({"id": "x", "pid": me.pid, "pid_created": me.create_time(), "state": "running"})
    )
    with (
        (base / "ws" / "locked" / "lock").open("rb") as held,
        shared_lock_held_by_another_process(base / "env" / "used" / USE_LOCK),
    ):
        fcntl.flock(held, fcntl.LOCK_EX)
        report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    removed = {Path(item["path"]).relative_to(base).as_posix() for item in report["removed"]}
    assert removed == {"ws/idle", "env/unused", "code/unused"}, report
    reasons = {Path(item["path"]).name: item["reason"] for item in report["kept"]}
    assert reasons["locked"] == "locked" and reasons["running"] == "a launch runs"
    assert reasons["used"] == "in use"
    assert (base / "ws" / "locked" / "inputs" / "f").read_text() == "locked"
    assert (base / "ws" / "running" / "inputs" / "f").read_text() == "running"
    assert not (base / "ws" / "idle").exists() and (base / "ws" / "link").is_symlink()


def test_clean_by_age_keeps_what_was_used_lately(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    old, new = base / "env" / "old", base / "env" / "new"
    for directory in (old, new):
        directory.mkdir(parents=True)
        (directory / "last-use").write_text("")
        (directory / USE_LOCK).write_text("")
    long_ago = time.time() - 40 * 86400
    os.utime(old / "last-use", (long_ago, long_ago))
    os.utime(old, (long_ago, long_ago))
    report = run_command("clean", str(tmp_path / "remote"), "--older-than", "30", tmp_path=tmp_path)
    assert [Path(item["path"]).name for item in report["removed"]] == ["old"]
    assert new.exists() and not old.exists()


def test_clean_keeps_a_directory_without_a_lock_file(tmp_path):
    """A session makes the directory of a workspace and then its lock: no lock, no workspace."""
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1" / "ws"
    (base / "starting" / "inputs").mkdir(parents=True)
    (base / "done").mkdir()
    (base / "done" / "lock").write_text("")

    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)

    assert [Path(item["path"]).name for item in report["removed"]] == ["done"]
    (kept,) = report["kept"]
    assert Path(kept["path"]).name == "starting" and "no lock file" in kept["reason"]
    assert (base / "starting" / "inputs").is_dir()


@pytest.mark.parametrize("kind", ["ws", "env", "code"])
def test_clean_does_not_enter_a_kind_directory_that_is_a_link(tmp_path, kind):
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    (outside / "entry").mkdir(parents=True)
    (outside / "entry" / "lock").write_text("")
    # A directory there that has the name of what an ended `clean` of this host left behind.
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait(timeout=TIMEOUT)
    leftover = outside / f".removing-{ended.pid}-{agent_module._host_tag()}-old"
    leftover.mkdir()
    base = tmp_path / "remote" / "v1"
    base.mkdir(parents=True)
    (base / kind).symlink_to(outside, target_is_directory=True)

    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)

    assert report["removed"] == []
    (kept,) = report["kept"]
    assert (
        kept["kind"] == ("workspace" if kind == "ws" else kind)
        and kept["reason"] == "the directory is a link"
    )
    assert (outside / "entry" / "lock").exists() and leftover.is_dir()


def test_clean_takes_its_exclusive_locks_on_files_open_for_writing(tmp_path, monkeypatch):
    # On Linux NFS, flock is emulated with a lock of the whole file, and an exclusive lock needs
    # a file that is open for writing.
    base = tmp_path / "remote" / "v1"
    for kind, lock in (("env", ".xeda-lock"), ("code", ".xeda-lock"), ("ws", "lock")):
        (base / kind / "unused").mkdir(parents=True)
        (base / kind / "unused" / lock).write_text("")
    real = fcntl.flock

    def nfs_flock(descriptor: int, operation: int) -> None:
        read_only = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        if operation & fcntl.LOCK_EX and read_only:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        real(descriptor, operation)

    monkeypatch.setattr(agent_module.fcntl, "flock", nfs_flock)
    report = agent_module.clean(tmp_path / "remote", older_than=0, everything=True)

    assert report["kept"] == []
    assert sorted(entry["kind"] for entry in report["removed"]) == ["code", "env", "workspace"]


def test_clean_only_removes_the_listed_paths_and_checks_them_again(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    for name in ("listed", "used-since", "other"):
        (base / "ws" / name / "inputs").mkdir(parents=True)
        (base / "ws" / name / "lock").write_text("")
        old = time.time() - 60 * 86400
        for path in (
            (base / "ws" / name),
            (base / "ws" / name / "lock"),
            (base / "ws" / name / "inputs"),
        ):
            os.utime(path, (old, old))
    os.utime(base / "ws" / "used-since" / "inputs")  # used after the listing
    root = str(tmp_path / "remote")
    only = [str(base / "ws" / name) for name in ("listed", "used-since", "gone")]

    report = run_command(
        "clean",
        root,
        "--older-than",
        "30",
        *[a for p in only for a in ("--only", p)],
        tmp_path=tmp_path,
    )

    assert [Path(item["path"]).name for item in report["removed"]] == ["listed"]
    assert [(Path(i["path"]).name, i["reason"]) for i in report["kept"]] == [
        ("used-since", "recently used")
    ]
    assert (base / "ws" / "other").exists() and (base / "ws" / "used-since").exists()
    assert not (base / "ws" / "listed").exists()


def test_clean_list_removes_nothing_and_names_what_a_removal_would_remove(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    for name in ("idle", "locked"):
        (base / "ws" / name / "inputs").mkdir(parents=True)
        (base / "ws" / name / "lock").write_text("")
    (base / "env" / "unused").mkdir(parents=True)
    (base / "env" / "unused" / USE_LOCK).write_text("")
    root = str(tmp_path / "remote")
    with (base / "ws" / "locked" / "lock").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        listed = run_command("clean", root, "--all", "--list", tmp_path=tmp_path)
        assert {p.name for p in base.glob("*/*")} == {"idle", "locked", "unused"}
        removed = run_command("clean", root, "--all", tmp_path=tmp_path)
    assert "removed" not in listed and "removable" not in removed
    assert listed["removable"] == removed["removed"]
    assert {Path(item["path"]).name for item in listed["removable"]} == {"idle", "unused"}
    assert listed["kept"] == removed["kept"]
    assert [item["reason"] for item in listed["kept"]] == ["locked"]
    assert not (base / "ws" / "idle").exists() and (base / "ws" / "locked").exists()


USE_LOCK = ".xeda-lock"


@contextmanager
def shared_lock_held_by_another_process(path: Path) -> Iterator[None]:
    """Another process holds a shared lock on the file `path` while the block runs."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys\n"
            "fcntl.flock(os.open(sys.argv[1], os.O_RDONLY), fcntl.LOCK_SH)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == b"held\n"
        yield
    finally:
        assert holder.stdin is not None and holder.stdout is not None
        holder.stdin.close()
        holder.wait(timeout=TIMEOUT)
        holder.stdout.close()


def make_entry(base: Path, kind: str, name: str, *, lock: bool = True, age: float = 60) -> Path:
    """An environment or a code copy that provisioning made `age` days ago."""
    directory = base / kind / name
    directory.mkdir(parents=True)
    (directory / "last-use").write_text("")
    if lock:
        (directory / USE_LOCK).write_text("")
    old = time.time() - age * 86400
    for path in (directory, directory / "last-use"):
        os.utime(path, (old, old))
    return directory


def test_clean_keeps_an_environment_and_a_code_copy_that_another_process_holds(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    held = {kind: make_entry(base, kind, "held") for kind in ("env", "code")}
    for kind in ("env", "code"):
        make_entry(base, kind, "free")
    root = str(tmp_path / "remote")
    with (
        shared_lock_held_by_another_process(held["env"] / USE_LOCK),
        shared_lock_held_by_another_process(held["code"] / USE_LOCK),
    ):
        listed = run_command("clean", root, "--all", "--list", tmp_path=tmp_path)
        report = run_command("clean", root, "--all", tmp_path=tmp_path)
    assert {Path(i["path"]).relative_to(base).as_posix() for i in report["removed"]} == {
        "env/free",
        "code/free",
    }
    assert listed["removable"] == report["removed"]
    assert {Path(i["path"]).relative_to(base).as_posix(): i["reason"] for i in report["kept"]} == {
        "env/held": "in use",
        "code/held": "in use",
    }
    assert all(path.is_dir() for path in held.values())
    assert not (base / "env" / "free").exists() and not (base / "code" / "free").exists()


def test_clean_keeps_an_unused_entry_that_is_not_old_enough(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    make_entry(base, "env", "recent", age=1)
    make_entry(base, "code", "old", age=40)
    report = run_command("clean", str(tmp_path / "remote"), "--older-than", "30", tmp_path=tmp_path)
    assert [Path(i["path"]).name for i in report["removed"]] == ["old"]
    assert [(Path(i["path"]).name, i["reason"]) for i in report["kept"]] == [
        ("recent", "recently used")
    ]


@pytest.mark.parametrize("kind", ["env", "code"])
def test_clean_keeps_what_has_no_lock_file_and_does_not_make_one(tmp_path, kind):
    """No lock file: provisioning did not make the directory, and no session has used it. Nobody
    can say that it is unused. `clean` never creates a lock file in it."""
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    directory = make_entry(base, kind, "old", lock=False)
    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    assert report["removed"] == []
    (kept,) = report["kept"]
    assert kept["path"] == str(directory) and "no lock file" in kept["reason"]
    assert not (directory / USE_LOCK).exists()


@pytest.mark.parametrize("kind", ["env", "code"])
def test_clean_does_not_follow_a_link_named_as_the_lock_of_an_entry(tmp_path, kind):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    outside = tmp_path / "outside"
    outside.write_text("")
    make_entry(base, kind, "linked", lock=False)
    (base / kind / "linked" / USE_LOCK).symlink_to(outside)
    make_entry(base, kind, "fifo", lock=False)
    os.mkfifo(base / kind / "fifo" / USE_LOCK)
    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    assert report["removed"] == []
    reasons = {Path(item["path"]).name: item["reason"] for item in report["kept"]}
    assert reasons == {"linked": "the lock is a link", "fifo": "the lock is not a regular file"}


@pytest.mark.parametrize("kind", ["env", "code"])
def test_clean_holds_the_exclusive_lock_while_it_removes_an_entry(tmp_path, monkeypatch, kind):
    """A session that starts during the removal cannot take its shared lock."""
    base = tmp_path / "remote" / "v1"
    directory = make_entry(base, kind, "old")
    seen: list[str] = []
    real = agent_module._remove_tree

    def remove(path: Path) -> None:
        assert path != directory and path.name.startswith(".removing-")
        with (path / USE_LOCK).open("rb") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        seen.append(path.name)
        real(path)

    monkeypatch.setattr(agent_module, "_remove_tree", remove)
    report = agent_module.clean(tmp_path / "remote", 30, everything=False)
    assert [Path(i["path"]) for i in report["removed"]] == [directory]
    assert len(seen) == 1 and not list((base / kind).iterdir())


def test_clean_removes_the_leftover_of_a_removal_that_ended_early_in_every_kind(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait(timeout=TIMEOUT)
    for kind in ("env", "code"):
        leftover = base / kind / f".removing-{ended.pid}-{agent_module._host_tag()}-old"
        leftover.mkdir(parents=True)
        (leftover / "f").write_text("x")
    run_command("clean", str(tmp_path / "remote"), tmp_path=tmp_path)
    assert not list((base / "env").iterdir()) and not list((base / "code").iterdir())


# --- A session holds the environment and the code copy it runs from ---------------------------


class Provisioned:
    """An environment and a code copy in the layout of the remote root, with the lock files that
    provisioning makes. The Python of the environment imports the dependencies of the test
    interpreter and the code of the code copy."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path / "remote" / "v1"
        self.env = self.base / "env" / "e0"
        self.code = self.base / "code" / "c0"
        venv.EnvBuilder(symlinks=True, with_pip=False).create(self.env)
        (next((self.env / "lib").glob("python*/site-packages")) / "deps.pth").write_text(
            sysconfig.get_paths()["purelib"] + "\n"
        )
        self.code.mkdir(parents=True)
        (self.code / "xeda").symlink_to(Path(xeda.__file__).parent, target_is_directory=True)
        for directory in (self.env, self.code):
            (directory / USE_LOCK).write_text("")
        self.python = str(self.env / "bin" / "python")

    @staticmethod
    def lock_is_held(directory: Path) -> bool:
        """Whether another process holds a lock on the lock file of the directory now."""
        with (directory / USE_LOCK).open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    def command(self, *arguments: str, tmp_path: Path, check: bool = True) -> Any:
        """Run a one-shot command of the agent from the environment and the code copy."""
        return run_command(
            *arguments,
            tmp_path=tmp_path,
            python=self.python,
            environment={"PYTHONPATH": str(self.code)},
            check=check,
        )

    def start(self, start_agent: Callable[..., Agent], *arguments: str) -> Agent:
        environment = {**os.environ, "PYTHONPATH": str(self.code)}  # as the test has set it now
        return start_agent(*arguments, python=self.python, env=environment)


@pytest.fixture
def provisioned(tmp_path: Path) -> Provisioned:
    (tmp_path / "home").mkdir(exist_ok=True)
    return Provisioned(tmp_path)


def test_a_session_holds_its_environment_and_code_copy_until_it_ends(
    tmp_path, provisioned, start_agent
):
    agent = provisioned.start(start_agent)
    agent.get("hello")
    assert provisioned.lock_is_held(provisioned.env) and provisioned.lock_is_held(provisioned.code)
    root = str(tmp_path / "remote")
    report = run_command("clean", root, "--all", tmp_path=tmp_path)
    assert report["removed"] == []
    assert {Path(i["path"]).name: i["reason"] for i in report["kept"]} == {
        "e0": "in use",
        "c0": "in use",
    }
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0
    assert not provisioned.lock_is_held(provisioned.env)
    assert not provisioned.lock_is_held(provisioned.code)
    removed = run_command("clean", root, "--all", tmp_path=tmp_path)["removed"]
    assert {Path(i["path"]).name for i in removed} == {"e0", "c0"}


@pytest.mark.parametrize("option", ["--all", "--list"])
def test_clean_keeps_the_environment_and_code_copy_it_runs_from(tmp_path, provisioned, option):
    """`clean` holds the shared locks like every agent process, so its own exclusive lock on the
    environment it runs from fails: no special rule keeps that environment."""
    other = make_entry(provisioned.base, "env", "e1")
    report = provisioned.command("clean", str(tmp_path / "remote"), option, tmp_path=tmp_path)
    removable = report.get("removed", report.get("removable"))
    assert [Path(item["path"]) for item in removable] == [other]
    assert {Path(i["path"]).name: i["reason"] for i in report["kept"]} == {
        "e0": "in use",
        "c0": "in use",
    }
    assert provisioned.env.is_dir() and provisioned.code.is_dir()


@pytest.mark.parametrize("command", [("check",), ("clean", "ROOT", "--list"), ("clean", "ROOT")])
def test_every_command_refuses_to_run_from_a_directory_that_clean_is_removing(
    tmp_path, provisioned, command
):
    arguments = [str(tmp_path / "remote") if a == "ROOT" else a for a in command]
    with (provisioned.env / USE_LOCK).open("rb") as removing:
        fcntl.flock(removing, fcntl.LOCK_EX)
        report = provisioned.command(*arguments, tmp_path=tmp_path, check=False)
    assert "xeda remote clean" in report["error"]["message"]
    assert report.get("removed", []) == [] and report.get("removable", []) == []
    assert provisioned.env.is_dir()


def test_check_holds_the_locks_while_it_runs(tmp_path, provisioned):
    """`main` takes the locks before it runs the command: a command that holds none would let a
    `clean` remove the directory under it. The command here reports who holds the locks."""
    program = (
        "import fcntl, os, sys\n"
        "from xeda.remote import agent\n"
        "def check(executables):\n"
        "    held = []\n"
        "    for d in agent._runtime_directories():\n"
        f"        f = open(os.path.join(d, {USE_LOCK!r}), 'rb')\n"
        "        try:\n"
        "            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        except BlockingIOError:\n"
        "            held.append(str(d))\n"
        "    return {'held': held}\n"
        "agent.check = check\n"
        "sys.exit(agent.main(['check']))\n"
    )
    done = subprocess.run(
        [provisioned.python, "-c", program],
        capture_output=True,
        timeout=TIMEOUT,
        env={**os.environ, "HOME": str(tmp_path / "home"), "PYTHONPATH": str(provisioned.code)},
        check=True,
    )
    stream = io.BytesIO(done.stdout)
    skip_to_magic(stream)
    assert sorted(json.loads(stream.read())["held"]) == sorted(
        [str(provisioned.env), str(provisioned.code)]
    )


@pytest.mark.parametrize("kind", ["env", "code", "ws"])
def test_clean_skips_a_candidate_whose_lock_file_was_replaced_after_it_locked(
    tmp_path, monkeypatch, kind
):
    """Another `clean` removed the directory and provisioning made it again: the file that this
    `clean` opened is no longer the lock of the directory, so the lock means nothing."""
    base = tmp_path / "remote" / "v1"
    if kind == "ws":
        directory = base / "ws" / "w"
        directory.mkdir(parents=True)
        lock = directory / "lock"
        lock.write_text("")
    else:
        directory = make_entry(base, kind, "k")
        lock = directory / USE_LOCK
    real = fcntl.flock

    def flock_then_replace(descriptor: Any, operation: int) -> None:
        real(descriptor, operation)
        os.unlink(lock)
        lock.write_text("")

    monkeypatch.setattr(agent_module.fcntl, "flock", flock_then_replace)
    report = agent_module.clean(tmp_path / "remote", 30, everything=True)
    assert report["removed"] == []
    (kept,) = report["kept"]
    assert kept["path"] == str(directory) and "changed" in kept["reason"]
    assert directory.is_dir() and lock.is_file()


def test_clean_does_not_look_at_a_stage_directory_that_provisioning_fills(tmp_path):
    """`put-code` unpacks into `code/.<key>.<host>.<pid>` before it takes its lock. Bootstrap
    sweeps the stage directory of a process that is gone."""
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    stage = make_entry(base, "code", ".0123abcd.somehost.4242", lock=False)
    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    assert report["removed"] == [] and report["kept"] == []
    assert stage.is_dir()


def test_a_session_outside_the_layout_takes_no_use_lock(tmp_path, start_agent):
    """An agent that runs from a checkout or another environment has no lock to take."""
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


@pytest.mark.parametrize("kind", ["env", "code"])
def test_a_session_does_not_start_in_a_directory_that_clean_is_removing(
    tmp_path, provisioned, start_agent, kind
):
    directory = provisioned.env if kind == "env" else provisioned.code
    with (directory / USE_LOCK).open("rb") as removing:
        fcntl.flock(removing, fcntl.LOCK_EX)
        agent = provisioned.start(start_agent)
        error = agent.error()
        assert "xeda remote clean" in error["message"] and str(directory) in error["message"]
        assert agent.process.wait(timeout=TIMEOUT) == 1


def test_a_session_does_not_use_a_directory_that_was_renamed_after_it_took_the_lock(
    tmp_path, provisioned, monkeypatch
):
    """`clean` renamed the directory between the open of the lock file and the lock. The lock is
    then on a file of the directory that `clean` removes, so the session must refuse it."""
    real = fcntl.flock
    moved = provisioned.env.with_name(".removing-1-x-e0")

    def flock_then_rename(descriptor: Any, operation: int) -> None:
        real(descriptor, operation)
        os.rename(provisioned.env, moved)

    monkeypatch.setattr(agent_module.fcntl, "flock", flock_then_rename)
    with pytest.raises(agent_module.RemoteError, match="removed"):
        agent_module._take_use_lock(provisioned.env)
    assert moved.is_dir()


# ---------------------------------------------------------------------------------------------
# Output never delays a cancel or the lease


def read_line(stream: Any, timeout: float = 60) -> bytes:
    """One line of `stream`, or a failure after `timeout` seconds."""
    lines: "queue.Queue[bytes]" = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty:
        pytest.fail("no line from the process")


def test_a_flooding_tool_is_canceled_on_time_when_the_client_does_not_read(
    tmp_path, toolchain, monkeypatch, start_agent
):
    monkeypatch.setenv("XEDA_FAKE_FPGA_FLOOD", "1")
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent("--grace", "1,1", env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    message = launch_message(tmp_path, "run-1", path_setup(toolchain))
    agent.pause_reading()  # the pipe of the output fills up while the tool floods the log
    beats = Beats(agent)
    agent.send(message)
    pid = tool.wait(agent)
    found = descendants(agent)
    assert pid in [p.pid for p in found]
    log = workspace(tmp_path) / "launches" / "run-1" / "log"
    deadline = time.monotonic() + 60
    while log.stat().st_size < 2_000_000:  # the agent has more than its pipe and queue can hold
        assert time.monotonic() < deadline, "the tool did not flood the log"
        time.sleep(0.05)
    agent.writer.send(CANCEL)
    try:
        _gone, alive = psutil.wait_procs(found, timeout=30)
    finally:
        beats.stop()
    assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE], alive


def test_a_client_that_stops_reading_is_treated_as_gone(
    tmp_path, toolchain, monkeypatch, start_agent
):
    monkeypatch.setenv("XEDA_FAKE_FPGA_FLOOD", "1")
    tool = BlockingTool(tmp_path, monkeypatch)
    agent = start_agent("--lease", "2", "--grace", "1,1", env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    message = launch_message(tmp_path, "run-1", path_setup(toolchain))
    agent.pause_reading()
    beats = Beats(agent)  # the client is alive and sends heartbeats, but reads nothing
    agent.send(message)
    tool.wait(agent)
    found = descendants(agent)
    try:
        assert agent.process.wait(timeout=60) == 0  # the agent gave up on the client
    finally:
        beats.stop()
    assert_gone(found)
    state = json.loads((workspace(tmp_path) / "launches" / "run-1" / "state.json").read_text())
    assert state["state"] == "canceled"
    assert lock_is_free(tmp_path)


class StalledReader:
    """A stand-in for the pipe of a client that does not read: a write blocks for ever."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def write(self, data: bytes) -> int:
        self.release.wait()
        return len(data)

    def flush(self) -> None:
        pass


def make_session(tmp_path: Path, sink: Any, stall: float = 100, **keywords: Any):
    client = agent_module._Client(io.BytesIO())
    out = agent_module._Output(FrameWriter(sink), stall)
    out.start()
    return agent_module.Session(tmp_path / "remote", KEY, client, out, **keywords), out


LEADER = """\
import signal, subprocess, sys, time

log, label = sys.argv[1], sys.argv[2]


def note(number, _frame):
    with open(log, "a") as stream:
        stream.write(f"{label} {signal.Signals(number).name} {time.monotonic()}\\n")


signal.signal(signal.SIGINT, note)
signal.signal(signal.SIGTERM, note)
if label == "leader":
    other = subprocess.Popen(
        [sys.executable, sys.argv[0], log, "descendant"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=len(sys.argv) > 3,  # a tool that left the group
    )
    other.stdout.readline()
print("ready", flush=True)
time.sleep(600)
"""


def run_stop_launch(tmp_path, *leader_arguments):
    """Stop a leader with one descendant. Return the signals of each, in order, with the time."""
    script = tmp_path / "leader.py"
    script.write_text(LEADER)
    notes = tmp_path / "signals"
    sink = StalledReader()
    session, out = make_session(tmp_path, sink, grace=(0.5, 0.5))
    child = subprocess.Popen(
        [sys.executable, str(script), str(notes), "leader", *leader_arguments],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert read_line(child.stdout) == b"ready\n"
        descendant = psutil.Process(child.pid).children(recursive=True)
        assert len(descendant) == 1
        # Fill the queue of the output: the writer is blocked, so the session cannot send.
        while out.offer(LOG, b"x" * 100):
            pass
        started = time.monotonic()
        survivors = session.stop_launch(child, None, "a test")
        elapsed = time.monotonic() - started
        assert survivors == 0 and child.returncode == -9  # SIGKILL
        assert 0.9 < elapsed < 10, elapsed  # 0.5 s + 0.5 s on the clock of the agent
        assert_gone(descendant)
    finally:
        sink.release.set()
        child.kill()
        child.wait(timeout=60)
    events = [line.split() for line in notes.read_text().splitlines()]
    leader = [(name, float(at)) for label, name, at in events if label == "leader"]
    other = [(name, float(at)) for label, name, at in events if label == "descendant"]
    return leader, other, started + elapsed


def test_signals_come_in_order_on_the_clock_of_the_agent_even_when_output_is_blocked(tmp_path):
    leader, other, ended = run_stop_launch(tmp_path)
    # The tool in the group gets SIGINT with the leader, as at a terminal.
    assert [name for name, _ in leader] == ["SIGINT", "SIGTERM"]
    assert [name for name, _ in other] == ["SIGINT", "SIGTERM"]
    assert abs(other[0][1] - leader[0][1]) < 0.3
    assert leader[1][1] - leader[0][1] >= 0.45  # the first period
    assert other[1][1] >= leader[0][1] + 0.45
    assert ended - leader[1][1] >= 0.45  # the second period, then SIGKILL


def test_a_tool_that_left_the_group_gets_no_sigint_but_the_later_signals(tmp_path):
    leader, other, ended = run_stop_launch(tmp_path, "own-session")
    assert [name for name, _ in leader] == ["SIGINT", "SIGTERM"]
    assert [name for name, _ in other] == ["SIGTERM"]
    assert other[0][1] >= leader[0][1] + 0.45
    assert ended - leader[1][1] >= 0.45


def test_a_stalled_write_fails_the_output_and_ends_the_lease_wait(tmp_path):
    sink = StalledReader()
    session, out = make_session(tmp_path, sink, stall=0.3)
    try:
        assert out.offer(LOG, b"a line")
        assert out.offer(LOG, b"another")
        deadline = time.monotonic() + 30
        while not out.failed() and time.monotonic() < deadline:
            threading.Event().wait(0.05)
        assert out.failed() and "does not read" in (session.lost(launching=False) or "")
        assert out.offer(LOG, b"dropped")  # a failed output drops, and never blocks
    finally:
        sink.release.set()


def test_the_log_is_sent_a_bounded_amount_at_a_time_and_nothing_is_lost(tmp_path):
    log = tmp_path / "log"
    lines = [f"line {i} " + "x" * 90 for i in range(8000)]  # about 800 KB
    log.write_text("\n".join(lines) + "\n")
    accepted: list[bytes] = []
    room = {"left": 10**9}

    def offer(kind: bytes, payload: bytes) -> bool:
        if room["left"] <= 0:
            return False
        room["left"] -= 1
        accepted.append(payload)
        return True

    tail = agent_module._LogTail(log, offer)
    first = tail.pump()
    assert 0 < first <= agent_module._PUMP_BYTES + 200  # one tick sends a bounded amount
    assert len(accepted) < len(lines) and not tail.finished()
    room["left"] = 3  # no room: the next lines wait
    tail.pump()
    assert len(accepted) == len(lines[: len(accepted)]) and room["left"] == 0
    room["left"] = 10**9
    while not tail.finished():
        tail.pump(final=True)
    assert [line.decode() for line in accepted] == lines
    tail.close()


def test_a_damaged_result_is_a_protocol_error_with_a_clear_text(tmp_path):
    assert agent_module._read_result(tmp_path) is None
    (tmp_path / "result.json").write_text("{not json")
    with pytest.raises(
        agent_module.RemoteProtocolError, match="result.json of the launch is damaged"
    ):
        agent_module._read_result(tmp_path)
    (tmp_path / "result.json").write_text("[1]")
    with pytest.raises(agent_module.RemoteProtocolError, match="not a JSON object"):
        agent_module._read_result(tmp_path)
    (tmp_path / "result.json").write_text('{"success": true}')
    assert agent_module._read_result(tmp_path) == {"success": True}


# ---------------------------------------------------------------------------------------------
# The lock


def lock_is_free(tmp_path: Path) -> bool:
    """Whether nobody holds the lock of the workspace."""
    with (workspace(tmp_path) / "lock").open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def test_the_lock_is_released_when_the_client_goes_away(tmp_path, start_agent):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    assert not lock_is_free(tmp_path)  # held from the sync on
    agent.close_input()
    assert agent.process.wait(timeout=TIMEOUT) == 0
    assert lock_is_free(tmp_path)


def test_a_session_that_waits_for_a_removed_workspace_takes_the_new_lock(tmp_path, start_agent):
    ws = workspace(tmp_path)
    ws.mkdir(parents=True)
    with (ws / "lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        agent = start_agent("--lock-notice", "0.1", env=dict(os.environ))
        agent.get("hello")
        entries = build_manifest({}, generated=DESIGN)
        agent.send(
            {
                "sync": {
                    "workspace": KEY,
                    "design": "top",
                    "entries": [e.as_json_value() for e in entries],
                }
            }
        )
        agent.line("waiting for the workspace lock")  # the agent has the old file open
        agent_module._remove_directory(ws)  # what `clean` does, with the lock held
    # The old lock is free now, but its file is gone: the agent takes the lock file of the new
    # workspace, and holds that one.
    assert agent.send_needed(entries, DESIGN) == ["top.v", "top.yaml"]
    assert agent.get("synced") == {"written": 2, "removed": 0}
    assert (ws / "lock").is_file() and not lock_is_free(tmp_path)
    assert (ws / "inputs" / "top" / "top.v").read_bytes() == DESIGN["top.v"]
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0
    assert lock_is_free(tmp_path)
    assert not [p for p in ws.parent.iterdir() if p.name.startswith(".removing-")]


def test_a_session_during_a_removal_gets_a_fresh_workspace_and_keeps_its_inputs(
    tmp_path, start_agent, monkeypatch
):
    """`clean` holds the lock and has taken the workspace out of use; its deletion has not ended.
    A session that starts now must not work in the tree that is being deleted."""
    ws = workspace(tmp_path)
    (ws / "inputs" / "top").mkdir(parents=True)
    (ws / "lock").write_text("")
    (ws / "inputs" / "top" / "old.v").write_text("old")
    deleting = threading.Event()
    go = threading.Event()
    real = agent_module._remove_tree
    seen: list[Path] = []

    def slow_remove(path: Path) -> None:
        seen.append(path)
        deleting.set()
        assert go.wait(TIMEOUT)
        real(path)

    monkeypatch.setattr(agent_module, "_remove_tree", slow_remove)
    cleaning = threading.Thread(
        target=lambda: agent_module.clean(tmp_path / "remote", 30, everything=True), daemon=True
    )
    cleaning.start()
    assert deleting.wait(TIMEOUT)
    assert seen[0].name.startswith(".removing-") and not ws.exists()
    agent = start_agent(env=dict(os.environ))
    try:
        agent.get("hello")
        assert agent.sync(DESIGN, "run-1") == {"written": 2, "removed": 0}
        assert (ws / "inputs" / "top" / "top.v").read_bytes() == DESIGN["top.v"]
    finally:
        go.set()
        cleaning.join(TIMEOUT)
    assert (ws / "inputs" / "top" / "top.v").is_file() and not seen[0].exists()
    agent.send({"bye": True})
    assert agent.process.wait(timeout=TIMEOUT) == 0


def test_clean_removes_the_leftover_of_a_removal_that_ended_early(tmp_path):
    ws = tmp_path / "remote" / "v1" / "ws"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=TIMEOUT)  # its process id is free now
    tag = agent_module._host_tag()
    mine = ws / f"{agent_module._REMOVING}{dead.pid}-{tag}-abc"
    other_host = ws / f"{agent_module._REMOVING}{dead.pid}-00000000-abc"
    alive = ws / f"{agent_module._REMOVING}{os.getpid()}-{tag}-abc"
    for path in (mine, other_host, alive):
        (path / "inputs").mkdir(parents=True)
    report = agent_module.clean(tmp_path / "remote", 30, everything=True)
    assert not mine.exists() and other_host.exists() and alive.exists()
    assert report["removed"] == [] and report["kept"] == []


# ---------------------------------------------------------------------------------------------
# Refusals


def bare_launch(tmp_path: Path, launch_id: str = "run-1", **fields: Any) -> dict[str, Any]:
    """A `launch` message without a plan: the agent refuses it before it plans anything."""
    ws = workspace(tmp_path)
    request = LaunchRequest(
        chain="nextpnr",
        design_file="top.yaml",
        inputs_dir=str(ws / "inputs" / "top"),
        run_root=str(ws / "xeda_run"),
        path_identities={},
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        log_level=20,
        detailed_logs=False,
        expected={},
    ).model_copy(update=fields)
    return {"launch": request.model_dump(mode="json"), "id": launch_id, "setup": []}


LAUNCH_REFUSALS = [
    ({"id": "../evil"}, "not a launch id"),
    ({"id": ""}, "not a launch id"),
    ({"inputs_dir": "relative/top"}, "not an absolute path"),
    ({"run_root": "xeda_run"}, "not an absolute path"),
    ({"design_file": "../outside.yaml"}, "'..'"),
    ({"design_file": "/etc/passwd"}, "absolute"),
    ({"setup": "export A=1"}, "not a list of text lines"),
]


@pytest.mark.parametrize(("change", "text"), LAUNCH_REFUSALS)
def test_a_bad_launch_message_is_refused_and_releases_the_lock(tmp_path, start_agent, change, text):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.sync(DESIGN, "run-1")
    message = bare_launch(tmp_path)
    for key, value in change.items():
        if key in ("id", "setup"):
            message[key] = value
        else:
            message["launch"][key] = value
    agent.send(message)
    error = agent.error()
    assert error["type"] == "RemoteProtocolError" and text in error["message"], error
    assert agent.process.wait(timeout=TIMEOUT) == 1
    assert lock_is_free(tmp_path)  # released on an exception
    assert not (workspace(tmp_path) / "launches" / "run-1").exists()


def test_a_bad_workspace_key_is_refused(tmp_path, start_agent):
    agent = start_agent(key="../escape", env=dict(os.environ))
    error = agent.error()
    assert "not a workspace key" in error["message"]
    assert agent.process.wait(timeout=TIMEOUT) == 1


def test_a_relative_root_gets_an_error_frame(tmp_path):
    agent = Agent(tmp_path, Path("relative/root"), env=dict(os.environ))
    try:
        assert "not an absolute path" in agent.error()["message"]
        assert agent.process.wait(timeout=TIMEOUT) == 2
    finally:
        agent.stop()


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "../x", "size": 1, "sha256": "0" * 64, "executable": False},
        {"path": "/abs", "size": 1, "sha256": "0" * 64, "executable": False},
        {"path": "a", "size": 1, "sha256": "zz", "executable": False},
        {"path": "a", "size": 1},
    ],
)
def test_a_bad_manifest_entry_is_refused(tmp_path, start_agent, entry):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.send({"sync": {"workspace": KEY, "design": "top", "entries": [entry]}})
    assert agent.error()["type"] == "RemoteProtocolError"
    assert agent.process.wait(timeout=TIMEOUT) == 1


def test_a_workspace_name_in_the_sync_that_is_not_the_key_is_refused(tmp_path, start_agent):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.send({"sync": {"workspace": "other", "design": "top", "entries": []}})
    assert "names the workspace" in agent.error()["message"]
    assert agent.process.wait(timeout=TIMEOUT) == 1


def test_a_malformed_frame_ends_the_session_with_an_error(tmp_path, start_agent):
    agent = start_agent(env=dict(os.environ))
    agent.get("hello")
    agent.process.stdin.write(b"Z\x00\x00\x00\x00")  # type: ignore[union-attr]
    agent.process.stdin.flush()  # type: ignore[union-attr]
    error = agent.error()
    assert "damaged" in error["message"] and "unknown frame kind" in error["message"]
    assert agent.process.wait(timeout=TIMEOUT) == 1


# ---------------------------------------------------------------------------------------------
# clean: links, FIFOs and failures


def test_clean_does_not_follow_a_link_named_lock_and_does_not_block_on_a_fifo(tmp_path):
    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1" / "ws"
    outside = tmp_path / "outside-lock"
    outside.write_text("")
    for name in ("linked", "fifo", "plain"):
        (base / name / "inputs").mkdir(parents=True)
    (base / "linked" / "lock").symlink_to(outside)
    os.mkfifo(base / "fifo" / "lock")
    (base / "plain" / "lock").write_text("")
    # a launches directory that is a link to a directory outside is not entered
    other = tmp_path / "other-launches" / "x"
    other.mkdir(parents=True)
    (other / "state.json").write_text("{}")
    (base / "plain" / "launches").symlink_to(tmp_path / "other-launches")
    report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    reasons = {Path(item["path"]).name: item["reason"] for item in report["kept"]}
    assert reasons["linked"] == "the lock is a link"
    assert reasons["fifo"] == "the lock is not a regular file"
    assert {Path(item["path"]).name for item in report["removed"]} == {"plain"}
    assert outside.exists() and (tmp_path / "other-launches" / "x" / "state.json").exists()


def test_clean_reports_what_it_cannot_remove_and_goes_on(tmp_path, monkeypatch):
    base = tmp_path / "remote" / "v1"
    for kind, name in (("ws", "first"), ("ws", "second"), ("env", "e1")):
        (base / kind / name).mkdir(parents=True)
    for name in ("first", "second"):
        (base / "ws" / name / "lock").write_text("")
    (base / "env" / "e1" / USE_LOCK).write_text("")
    real = agent_module._remove_tree

    def remove(path: Path) -> None:
        if path.name.endswith("first"):
            raise PermissionError(13, "Permission denied", str(path))
        real(path)

    monkeypatch.setattr(agent_module, "_remove_tree", remove)
    report = agent_module.clean(tmp_path / "remote", 30, everything=True)
    assert {Path(item["path"]).name for item in report["removed"]} == {"second", "e1"}
    (failed,) = report["kept"]
    assert Path(failed["path"]).name == "first" and "Permission denied" in failed["reason"]
    # The workspace was out of use before the deletion failed: the next `clean` removes it.
    assert not (base / "ws" / "first").exists()
    assert any(p.name.startswith(".removing-") for p in (base / "ws").iterdir())


def test_clean_prints_its_document_for_a_relative_root(tmp_path):
    (tmp_path / "home").mkdir()
    done = subprocess.run(
        [sys.executable, "-m", "xeda.remote.agent", "clean", "relative"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT,
        check=False,
    )
    stream = io.BytesIO(done.stdout)
    skip_to_magic(stream)
    document = json.loads(stream.read())
    assert done.returncode == 2 and document["removed"] == [] and "absolute" in str(document)


def test_a_node_that_the_launch_did_not_run_brings_back_no_earlier_record(tmp_path):
    """A blocked node keeps the record of an earlier run that passed. The fetch takes it as a node
    of this launch that did not pass, so that record does not come back as this launch's."""
    from xeda.remote.agent import _launch_nodes

    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "results.json").write_text(json.dumps({"outcome": "passed"}))
    document = {
        "named": ["b"],
        "nodes": [
            {"name": "a", "run_dir": str(tmp_path / "a"), "record": {"outcome": "passed"}},
            {"name": "b", "run_dir": str(tmp_path / "b"), "record": None},
            {"name": "c", "run_dir": str(tmp_path / "c"), "record": None},
        ],
    }
    a, b, c = _launch_nodes(document)
    assert a.results == {"outcome": "passed"} and not a.named
    assert b.results == {} and b.named and c.results == {}
