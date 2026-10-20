"""The remote agent syncs the inputs, supervises a launch, and sends back the selected files.

Each test runs `python -m xeda.remote.agent` as a subprocess of the test interpreter over pipes,
as the local xeda runs it over SSH. The launch is the real launch entry with the fake FPGA tools.
A tool that must be running when a test cancels it reports its process id through a FIFO and
blocks (`XEDA_FAKE_FPGA_READY`), so no test waits for a time to pass. Every test stops its
processes by process id.
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

import psutil
import pytest

import xeda
from xeda.flow_runner import DefaultRunner
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
    ) -> None:
        command = [sys.executable, "-m", "xeda.remote.agent", "session", str(root), key, *arguments]
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
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while frame := read_frame(self.process.stdout):
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
                stream.close()


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


def fetched(agent: Agent, fetch_all: bool = False) -> dict[str, bytes]:
    """Fetch and return the content of every file that comes back."""
    agent.send({"fetch": {"fetch_all": fetch_all}})
    entries = [Entry.from_json_value(item) for item in agent.get("files")]
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
    assert result["canceled"] is False and result["exit"] == 0
    assert result["result"]["success"] is True
    assert any("Running" in line or "nextpnr" in line for line in agent.lines), agent.lines
    launch_dir = workspace(tmp_path) / "launches" / "launch-1"
    state = json.loads((launch_dir / "state.json").read_text())
    assert state["state"] == "finished" and state["id"] == "launch-1" and state["exit"] == 0
    assert json.loads((launch_dir / "result.json").read_text()) == result["result"]
    assert (launch_dir / "request.json").is_file() and (launch_dir / "log").is_file()
    files = fetched(agent)
    assert "top/nextpnr/results.json" in files and "top/nextpnr/settings.json" in files
    assert not any(path.endswith("trace.json") for path in files)
    assert json.loads(files["top/nextpnr/results.json"])["success"] is True
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
    assert result["result"]["error"]["type"] == "KeyboardInterrupt" and result["exit"] == 130
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
    assert result["result"]["success"] is True
    assert "setup-line-ran" in agent.lines
    calls = list(workspace(tmp_path).glob("xeda_run/**/nextpnr/fake_fpga.calls.jsonl"))
    assert len(calls) == 1
    recorded = json.loads(calls[0].read_text().splitlines()[-1])["environment"]
    assert recorded["XEDA_FAKE_FPGA_SETUP"] == "from the setup"
    assert recorded["XEDA_FAKE_FPGA_PROFILE"] == "from-the-profile-env"
    assert Path(recorded["PYTHONPATH"]) == Path(xeda.__file__).resolve().parent.parent


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


def run_command(*arguments: str, tmp_path: Path) -> Any:
    """Run a one-shot command of the agent and return its JSON document."""
    done = subprocess.run(
        [sys.executable, "-m", "xeda.remote.agent", *arguments],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        check=True,
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
    import fcntl

    (tmp_path / "home").mkdir()
    base = tmp_path / "remote" / "v1"
    names = ("idle", "locked", "running")
    for name in names:
        (base / "ws" / name / "inputs").mkdir(parents=True)
        (base / "ws" / name / "lock").write_text("")
        (base / "ws" / name / "inputs" / "f").write_text(name)
    (base / "env" / "unused").mkdir(parents=True)
    (base / "env" / "unused" / "f").write_text("x")
    (base / "env" / "used").mkdir(parents=True)
    (base / "code" / "unused").mkdir(parents=True)
    (base / "ws" / "link").symlink_to(base / "ws" / "idle", target_is_directory=True)

    me = psutil.Process()
    launch = base / "ws" / "running" / "launches" / "x"
    launch.mkdir(parents=True)
    (launch / "state.json").write_text(
        json.dumps({"id": "x", "pid": me.pid, "pid_created": me.create_time(), "state": "running"})
    )
    user = subprocess.Popen(  # a process that runs from the "used" environment
        [
            sys.executable,
            "-c",
            "import time; time.sleep(600)",
            str(base / "env" / "used" / "marker"),
        ]
    )
    try:
        with (base / "ws" / "locked" / "lock").open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            report = run_command("clean", str(tmp_path / "remote"), "--all", tmp_path=tmp_path)
    finally:
        user.kill()
        user.wait(timeout=60)
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
    long_ago = time.time() - 40 * 86400
    os.utime(old / "last-use", (long_ago, long_ago))
    os.utime(old, (long_ago, long_ago))
    report = run_command("clean", str(tmp_path / "remote"), "--older-than", "30", tmp_path=tmp_path)
    assert [Path(item["path"]).name for item in report["removed"]] == ["old"]
    assert new.exists() and not old.exists()
