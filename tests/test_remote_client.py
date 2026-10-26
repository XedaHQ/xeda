"""`run_remote` runs a request on a fake remote host over the fake `ssh`.

The fake remote provisions an environment with the fake `uv`, runs the agent and the launch entry
from a snapshot of this checkout, and runs the fake FPGA tools. Nothing is mocked in the client.
A tool that must be running when a test cancels it reports its process id through a FIFO and
blocks (`XEDA_FAKE_FPGA_READY`), so no test waits for a time to pass.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import psutil
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectoryError
from xeda.remote import client
from xeda.remote.client import new_launch_id, workspace_key
from xeda.remote.errors import (
    RemoteCanceled,
    RemoteConnectionError,
    RemoteError,
    RemoteInterrupted,
    RemoteProtocolError,
)
from xeda.remote.ssh import Destination, SshSession

from .remote_fixtures import (
    RemoteHost,
    fake_remote,
    fake_ssh,
    remote_host,
    remote_request,
)
from .test_proc_utils import DEFAULT_SIGINT

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = [pytest.mark.skipif(os.name != "posix", reason="POSIX")]

PART = "LFE5U-25F-6BG381C"
TIMEOUT = 120


# --- Helpers ----------------------------------------------------------------------------------


def _design(tmp_path: Path, sources: tuple[str, ...] = ("top.v",), freq: str = "100 MHz") -> Path:
    """A design with one Verilog file per source and the fake `nextpnr` for an ECP5."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    for source in sources:
        name = Path(source).stem
        (root / source).write_text(
            f"module {name}(input clk, output q); assign q = clk; endmodule\n"
        )
    design = root / "top.yaml"
    design.write_text(
        f"name: top\nrtl:\n  sources: [{', '.join(sources)}]\n  top: top\n  clock: {{port: clk}}\n"
        f"flows:\n  nextpnr:\n    fpga.part: {PART}\n    clock.freq: {freq}\n"
    )
    return design


def _request(tmp_path: Path, design: Path, chain: str = "nextpnr", **overrides: Any):
    return remote_request(tmp_path, design, chain, **overrides)


def _succeeded(outcome) -> dict[str, Any]:
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    return outcome.document


def _states(document: dict[str, Any]) -> dict[str, str]:
    return {node["node"]: node["state"] for node in document["nodes"]}


def _cli(tmp_path: Path, design: Path, *arguments: str):
    return CliRunner().invoke(
        cli,
        ["run", *arguments[:1], "--design", str(design), "--remote", "somewhere"]
        + ["--run-root", str(tmp_path / "local" / "xeda_run")]
        + list(arguments[1:]),
    )


def _cli_script(tmp_path: Path, design: Path, remote_host: RemoteHost, signals: bool = True) -> str:
    """A program that runs `xeda run --remote --json` in a process of its own, on the fake uv
    release of this test. With `signals`, SIGINT is at its default in the program."""
    return (
        f"{DEFAULT_SIGINT if signals else ''}import xeda.remote.environment as environment\n"
        f"environment.UV_RELEASES = {remote_host.remote.releases!r}\n"
        "from xeda.cli import cli\n"
        f"cli(['run', 'nextpnr', '--design', {str(design)!r}, '--remote', 'somewhere', "
        f"'--run-root', {str(tmp_path / 'local' / 'xeda_run')!r}, '--json'], "
        "standalone_mode=True)\n"
    )


class Ready:
    """A FIFO on which the blocking fake tool reports its process id."""

    def __init__(self, tmp_path: Path, remote_host: RemoteHost, **environment: str) -> None:
        self.fifo = tmp_path / "ready"
        os.mkfifo(self.fifo)
        remote_host.configure(
            env={
                "XEDA_FAKE_FPGA_READY": str(self.fifo),
                "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5",
                **environment,
            }
        )
        self._pid: "queue.Queue[int]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        with open(self.fifo, "rb") as stream:  # blocks until the tool opens the FIFO to write
            self._pid.put(int(stream.read()))

    def wait(
        self,
        process: Optional["subprocess.Popen[bytes]"] = None,
        stderr: Optional[Callable[[], str]] = None,
    ):
        """The process id of the tool, when it runs. If the program under test ends first, the
        test fails with its standard error (`stderr` returns it)."""
        for _ in range(TIMEOUT):
            try:
                return self._pid.get(timeout=1)
            except queue.Empty:
                if process is not None and process.poll() is not None:
                    text = stderr() if stderr is not None else ""
                    pytest.fail(f"the program ended before the tool started:\n{text}")
        pytest.fail(f"the tool did not start in {TIMEOUT} s")


def _assert_gone(pid: int) -> None:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    _gone, alive = psutil.wait_procs([process], timeout=TIMEOUT)
    alive = [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]
    for process in alive:
        process.kill()  # the test fails below; it does not leave the tool running
    assert not alive, f"the tool still runs: {alive}"


# --- A remote run is a local run on another machine ---------------------------------------------


def test_remote_run_matches_a_local_run(tmp_path, remote_host):
    design = _design(tmp_path)
    local = DefaultRunner(tmp_path / "local_only", display_results=False).run(
        "nextpnr", design=design
    )
    assert local is not None
    document = _succeeded(_request(tmp_path, design))
    results = document["results"]
    for key in ("success", "fmax", "wns", "timing_met", "lut", "ff", "clock_domains"):
        assert results[key] == local.results[key], key
    # the identity does not depend on the place of the run
    for key in ("flow_hash", "settings_hash", "design_hash"):
        assert results[key] == local.results[key], key
    run_root = tmp_path / "local" / "xeda_run"
    mirror = run_root / "@somewhere"
    assert document["remote"] == "somewhere" and document["mirror"] == str(mirror)
    assert document["run_path"] == str(mirror / "top" / "nextpnr")
    assert document["design"] == "top" and document["design_file"] == str(design)
    directory = mirror / "top" / "nextpnr"
    for name in ("results.json", "settings.json", "nextpnr.log", "config.txt", "report.json"):
        assert (directory / name).is_file(), name
    assert not list(mirror.rglob("trace.json")), "the mirror holds no trace"
    assert results["artifacts"]["textcfg"] == str(directory / "config.txt")
    assert [node["run_path"] for node in document["nodes"]] == [
        str(mirror / "top" / "yosys_fpga"),
        str(directory),
    ]
    assert "written" not in document and "unreadable" not in document
    # no local run directory: the mirror is the only place
    assert not (run_root / "top").exists()


def test_chain_and_binding_run_remotely(tmp_path, remote_host):
    design = _design(tmp_path)
    document = _succeeded(_request(tmp_path, design, chain="yosys_fpga+nextpnr"))
    assert [e["node"] for e in document["request"]] == ["yosys_fpga", "nextpnr"]
    assert _states(document) == {"yosys_fpga": "ran", "nextpnr": "ran"}
    mirror = Path(document["mirror"])
    # the declared outputs of every node that the request names come back
    assert (mirror / "top" / "yosys_fpga" / "netlist.json").is_file()
    assert (mirror / "top" / "nextpnr" / "config.txt").is_file()

    # an explicit binding reaches the remote plan, which has to agree with the local one
    bound = _succeeded(
        _request(
            tmp_path,
            design,
            flow_settings=["flows.nextpnr.inputs.netlist=yosys_fpga.netlist"],
            rebuild_all=True,
        )
    )
    assert _states(bound) == {"yosys_fpga": "ran", "nextpnr": "ran"}


def test_second_run_reuses_and_a_source_edit_reruns(tmp_path, remote_host):
    design = _design(tmp_path)
    first = _succeeded(_request(tmp_path, design))
    assert _states(first) == {"yosys_fpga": "ran", "nextpnr": "ran"}
    inputs = remote_host.workspace() / "inputs" / "top"
    unchanged = (inputs / "top.xeda.json").stat().st_ino

    second = _succeeded(_request(tmp_path, design))
    assert _states(second) == {"yosys_fpga": "fresh", "nextpnr": "fresh"}
    assert (inputs / "top.xeda.json").stat().st_ino == unchanged
    assert second["results"]["fmax"] == first["results"]["fmax"]
    # the reused result still comes back
    assert (Path(second["run_path"]) / "config.txt").is_file()

    (design.parent / "top.v").write_text(
        "module top(input clk, output q); assign q = ~clk; endmodule\n"
    )
    third = _succeeded(_request(tmp_path, design))
    assert _states(third)["yosys_fpga"] == "ran"
    assert "~clk" in (inputs / "top.v").read_text()
    assert (inputs / "top.xeda.json").stat().st_ino == unchanged


def test_removed_source_leaves_the_inputs(tmp_path, remote_host):
    design = _design(tmp_path, sources=("top.v", "extra.v"))
    _succeeded(_request(tmp_path, design))
    inputs = remote_host.workspace() / "inputs" / "top"
    assert (inputs / "extra.v").is_file()
    _design(tmp_path)  # the design lists top.v only; extra.v stays on disk here
    assert (design.parent / "extra.v").is_file()
    _succeeded(_request(tmp_path, design))
    assert (inputs / "top.v").is_file() and not (inputs / "extra.v").exists()


def test_failed_run_mirror_has_no_stale_outputs(tmp_path, remote_host):
    design = _design(tmp_path)
    ok = _succeeded(_request(tmp_path, design))
    mirror_dir = Path(ok["run_path"])
    assert (mirror_dir / "config.txt").is_file() and (mirror_dir / "report.json").is_file()
    remote_dir = remote_host.workspace() / "xeda_run" / "top" / "nextpnr"
    assert (remote_dir / "config.txt").is_file()

    # The tool fails before it writes anything. The run directory on the remote still holds the
    # outputs of the run before.
    remote_host.configure(
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"}
    )
    outcome = _request(tmp_path, design, rebuild_all=True)
    assert outcome.error is None and not outcome.success
    document = outcome.document
    assert document["error"]["type"] == "FlowFailed"
    assert document["results"]["success"] is False
    assert (remote_dir / "config.txt").is_file(), "the remote still has the old output"
    saved = json.loads((mirror_dir / "results.json").read_text())
    assert saved["outcome"] == "failed"
    assert not (mirror_dir / "config.txt").exists()
    assert not (mirror_dir / "report.json").exists()
    assert "textcfg" not in (document["results"].get("artifacts") or {})
    assert document["results"].get("outputs") is None


def test_rejected_run_reports_the_rejection(tmp_path, remote_host):
    design = _design(tmp_path, freq="400 MHz")  # the fake nextpnr reports 250 MHz
    for _launch in range(2):
        outcome = _request(tmp_path, design)
        assert outcome.error is None and not outcome.success
        assert outcome.document["results"]["error"]["code"] == "rejected"
        assert "timing not met" in outcome.document["results"]["error"]["message"]
        assert not list(Path(outcome.document["mirror"]).rglob("trace.json"))


def test_fetch_all_brings_the_whole_run_directory(tmp_path, remote_host):
    design = _design(tmp_path)
    plain = _succeeded(_request(tmp_path, design))
    directory = Path(plain["mirror"]) / "top" / "yosys_fpga"
    assert not (directory / "yosys_fpga_synth.ys").exists()
    everything = _succeeded(_request(tmp_path, design, fetch_all=True))
    assert (directory / "yosys_fpga_synth.ys").is_file()
    assert not list(Path(everything["mirror"]).rglob("trace.json"))


def test_outputs_to_delivers_from_the_mirror(tmp_path, remote_host):
    design = _design(tmp_path)
    out = tmp_path / "out"
    document = _succeeded(_request(tmp_path, design, outputs_to=out))
    assert (out / "config.txt").is_file()
    delivered = {d["to"] for d in document["results"]["deliveries"]}
    assert str(out / "config.txt") in delivered
    mirror_dir = Path(document["run_path"])
    assert (out / "config.txt").read_bytes() == (mirror_dir / "config.txt").read_bytes()
    # an earlier copy of Xeda's own is replaced
    again = _succeeded(_request(tmp_path, design, outputs_to=out))
    assert again["results"]["deliveries"]


def test_outputs_to_is_checked_before_a_connection(tmp_path, remote_host):
    design = _design(tmp_path)
    run_root = tmp_path / "local" / "xeda_run"
    outcome = _request(tmp_path, design, outputs_to=run_root / "inside")
    assert outcome.error is not None and not outcome.success
    assert outcome.document["error"]["type"] == "DeliveryError"
    assert not any("-M" in call for call in remote_host.remote.calls("ssh"))


def test_a_failed_run_delivers_nothing(tmp_path, remote_host):
    design = _design(tmp_path, freq="400 MHz")
    out = tmp_path / "out"
    outcome = _request(tmp_path, design, outputs_to=out)
    assert not outcome.success and not out.exists()


# --- Refusals, before any connection ------------------------------------------------------------


def test_programmer_flow_is_refused_with_remote(tmp_path, remote_host):
    design = _design(tmp_path)
    outcome = _request(tmp_path, design, chain="nextpnr+openfpgaloader")
    assert isinstance(outcome.error, RemoteError) and not outcome.success
    assert outcome.document["error"]["type"] == "RemoteError"
    assert "programs a device" in outcome.document["error"]["message"]
    assert not any("-M" in call for call in remote_host.remote.calls("ssh"))


def test_post_cleanup_options_are_refused_with_remote(tmp_path, remote_host):
    design = _design(tmp_path)
    for option in ("--post-cleanup", "--post-cleanup-purge"):
        result = _cli(tmp_path, design, "nextpnr", option)
        assert result.exit_code == 2, result.output
        assert "removes run directories after delivery" in result.output
        assert "xeda remote clean" in result.output


def test_scrub_is_refused_with_remote_before_any_connection(tmp_path, remote_host):
    """A remote launch cannot ask for the confirmation, so the plan refuses `--scrub`: no ssh
    call is made, with the text output and with `--json` (one document on stdout)."""
    design = _design(tmp_path)
    text = _cli(tmp_path, design, "nextpnr", "--scrub")
    assert text.exit_code == 2, text.output
    assert "`--scrub`" in text.output and "xeda remote clean" in text.output
    assert "without --remote" in text.output
    document = CliRunner().invoke(
        cli,
        ["run", "nextpnr", "--design", str(design), "--remote", "somewhere", "--scrub", "--json"]
        + ["--run-root", str(tmp_path / "local" / "xeda_run")],
    )
    assert document.exit_code != 0
    result = json.loads(document.stdout)
    assert result["success"] is False
    assert result["error"]["type"] == "UsageError" and "--scrub" in result["error"]["message"]
    dry = _cli(tmp_path, design, "nextpnr", "--scrub", "--dry-run")
    assert dry.exit_code == 2 and "`--scrub`" in dry.output
    assert not remote_host.remote.calls("ssh")
    assert not (tmp_path / "local" / "xeda_run").exists()


def test_fetch_all_needs_remote(tmp_path):
    design = _design(tmp_path)
    result = CliRunner().invoke(cli, ["run", "nextpnr", "--design", str(design), "--fetch-all"])
    assert result.exit_code == 2
    assert "--fetch-all" in result.output and "--remote" in result.output


def test_windows_is_refused(tmp_path, monkeypatch, remote_host):
    monkeypatch.setattr(client, "_is_windows", lambda: True)
    outcome = _request(tmp_path, _design(tmp_path))
    assert isinstance(outcome.error, RemoteError)
    assert "Windows" in outcome.document["error"]["message"]
    assert not remote_host.remote.calls("ssh")


def test_a_connection_that_fails_names_the_step(tmp_path, remote_host, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    outcome = _request(tmp_path, _design(tmp_path))
    assert isinstance(outcome.error, RemoteConnectionError)
    assert outcome.document["error"]["type"] == "RemoteConnectionError"
    assert "cannot connect to somewhere" in outcome.document["error"]["message"]
    assert "Permission denied" in outcome.document["error"]["message"]


def test_a_provisioning_failure_reports_its_code_in_the_json_message(tmp_path, remote_host):
    remote_host.remote.set_platform("Darwin", "arm64")
    result = _cli(tmp_path, _design(tmp_path), "nextpnr", "--json")
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)["error"]
    assert error["type"] == "RemoteProvisionError"
    assert "not-linux: " in error["message"]


# --- The login shell, the log and the provisioning -----------------------------------------------


def test_a_login_shell_that_prints_does_not_break_the_run(tmp_path, remote_host, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_SSH_NOISE", "1")
    _succeeded(_request(tmp_path, _design(tmp_path)))


def test_provisioning_and_the_log_are_reported(tmp_path, remote_host, capsys, caplog):
    caplog.set_level("INFO", logger="xeda.remote.client")
    _succeeded(_request(tmp_path, _design(tmp_path)))
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Connecting to somewhere" in messages
    assert "Creating the environment" in messages or "environment" in messages
    # the log of the launch is tool output
    assert "nextpnr-ecp5" in capsys.readouterr().out


def test_names_of_the_workspace_and_the_launch(tmp_path):
    destination = Destination("somewhere", "me", "somewhere.example", 22)
    key = workspace_key(tmp_path, destination)
    assert len(key) == 24 and int(key, 16) >= 0
    assert key == workspace_key(tmp_path, destination)
    assert key != workspace_key(tmp_path / "other", destination)
    assert key != workspace_key(tmp_path, Destination("somewhere", "me", "somewhere.example", 2222))
    first, second = new_launch_id(), new_launch_id()
    stamp, _, random = first.partition("-")
    assert len(stamp) == 12 and stamp.isdigit() and len(random) == 8 and int(random, 16) >= 0
    assert first != second


# --- Dry run ------------------------------------------------------------------------------------


def test_dry_run_syncs_but_writes_no_run_directory(tmp_path, remote_host):
    design = _design(tmp_path)
    run_root = tmp_path / "local" / "xeda_run"
    document = _succeeded(_request(tmp_path, design, dry_run=True))
    assert document["dry_run"] is True and document["remote"] == "somewhere"
    assert "top.v" in document["uploaded"] and "top.xeda.json" in document["uploaded"]
    assert [node["state"] for node in document["plan"]["nodes"]] == ["stale", "stale"]
    workspace = remote_host.workspace()
    assert (workspace / "inputs" / "top" / "top.v").is_file()
    assert (
        not any((workspace / "xeda_run").rglob("*")) if (workspace / "xeda_run").exists() else True
    )
    assert not run_root.exists(), "nothing is written locally"
    again = _succeeded(_request(tmp_path, design, dry_run=True))
    assert again["uploaded"] == []

    _succeeded(_request(tmp_path, design))
    after = _succeeded(_request(tmp_path, design, dry_run=True))
    assert [node["state"] for node in after["plan"]["nodes"]] == ["fresh", "fresh"]


def test_dry_run_through_the_command_line(tmp_path, remote_host):
    design = _design(tmp_path)
    result = _cli(tmp_path, design, "nextpnr", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Plan for nextpnr on somewhere" in result.output
    assert "Uploaded" in result.output and "top.v" in result.output
    result = _cli(tmp_path, design, "nextpnr", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["dry_run"] is True and document["remote"] == "somewhere"


# --- The JSON document --------------------------------------------------------------------------


def test_json_document_has_nodes_remote_and_mirror_paths(tmp_path, remote_host):
    design = _design(tmp_path)
    result = _cli(tmp_path, design, "yosys_fpga+nextpnr", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)  # exactly one document on stdout
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    assert document["success"] is True and document["remote"] == "somewhere"
    assert document["mirror"] == str(mirror)
    assert document["flow"] == "nextpnr"
    assert [node["node"] for node in document["nodes"]] == ["yosys_fpga", "nextpnr"]
    for node in document["nodes"]:
        assert Path(node["run_path"]).is_relative_to(mirror)
        assert (Path(node["run_path"]) / "results.json").is_file()
    assert Path(document["run_path"]).is_relative_to(mirror)
    assert Path(document["results_json"]).is_file()
    assert "nextpnr-ecp5" in result.stderr, "the log of the launch is on stderr"


def test_a_failed_remote_flow_is_a_failed_run(tmp_path, remote_host):
    design = _design(tmp_path, freq="400 MHz")
    result = _cli(tmp_path, design, "nextpnr", "--json")
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["success"] is False and document["remote"] == "somewhere"
    assert document["error"]["type"] == "FlowFailed"
    assert document["results"]["error"]["code"] == "rejected"


def test_an_error_before_the_connection_is_one_json_document(tmp_path, remote_host):
    design = _design(tmp_path)
    result = _cli(tmp_path, design, "nextpnr", "--json", "-s", "no_such_setting=1")
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["success"] is False and document["remote"] == "somewhere"
    assert document["design"] == "top"
    assert not any("-M" in call for call in remote_host.remote.calls("ssh"))


# --- Cancel and disconnect ----------------------------------------------------------------------


def _earlier_success(tmp_path: Path) -> tuple[Path, Path]:
    """Run the design once with success; then change its clock, so that the next run runs the
    tool again. Return the design file and the results of the earlier run in the mirror."""
    _succeeded(_request(tmp_path, _design(tmp_path)))
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere" / "top"
    assert json.loads((mirror / "nextpnr" / "results.json").read_text())["outcome"] == "passed"
    return _design(tmp_path, freq="90 MHz"), mirror


def _assert_mirror_emptied(mirror: Path) -> None:
    """No record of the earlier success is left in the mirror of any planned node."""
    for name in ("yosys_fpga", "nextpnr"):
        assert not (mirror / name / "results.json").exists(), name
        assert not (mirror / name / "config.txt").exists(), name


class Program:
    """The program under test: `xeda run --remote --json` in a process (and a process group) of
    its own. Its standard error is read by a thread, which tells when a text has appeared."""

    def __init__(self, tmp_path: Path, script: str) -> None:
        self.out = tmp_path / "stdout"
        self._seen = ""
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._stdout = self.out.open("wb")
        # a session of its own: a signal for the group reaches the client and every ssh process
        # of it, as Ctrl-C at a terminal does, and nothing of the test runner
        self.process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            stdout=self._stdout,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            with self._changed:
                self._seen += line.decode(errors="replace")
                self._changed.notify_all()

    @property
    def stderr(self) -> str:
        with self._lock:
            return self._seen

    def wait_for(self, text: str) -> None:
        """Return when `text` is on the standard error. Fail if the program ends or `TIMEOUT`
        seconds pass first."""
        with self._changed:
            if not self._changed.wait_for(
                lambda: text in self._seen or self.process.poll() is not None, TIMEOUT
            ):
                pytest.fail(f"{text!r} did not appear:\n{self._seen}")
            if text not in self._seen:
                pytest.fail(f"the program ended before {text!r} appeared:\n{self._seen}")

    def interrupt(self) -> None:
        """Ctrl-C: SIGINT for the whole process group."""
        os.killpg(self.process.pid, signal.SIGINT)

    def finish(self) -> int:
        try:
            return self.process.wait(timeout=TIMEOUT)
        finally:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            self._stdout.close()


def test_ctrl_c_cancels_and_prints_one_json_document(tmp_path, remote_host):
    design, mirror = _earlier_success(tmp_path)
    ready = Ready(tmp_path, remote_host)
    program = Program(tmp_path, _cli_script(tmp_path, design, remote_host))
    try:
        pid = ready.wait(program.process, lambda: program.stderr)
        program.interrupt()
        status = program.finish()
    finally:
        program.finish()
    assert status == 1, program.stderr
    document = json.loads(program.out.read_text())
    assert document["success"] is False
    assert document["error"]["type"] == "RemoteCanceled"
    assert document["remote"] == "somewhere"
    _assert_gone(pid)
    assert "Canceling the launch" in program.stderr
    _assert_mirror_emptied(mirror)


def test_a_second_ctrl_c_closes_the_connection(tmp_path, remote_host):
    design, mirror = _earlier_success(tmp_path)
    ready = Ready(tmp_path, remote_host)
    program = Program(tmp_path, _cli_script(tmp_path, design, remote_host))
    try:
        pid = ready.wait(program.process, lambda: program.stderr)
        program.interrupt()
        # The second Ctrl-C comes when the first one is known: the message is on stderr.
        program.wait_for("Press Ctrl-C again")
        program.interrupt()
        status = program.finish()
    finally:
        program.finish()
    assert status == 1
    document = json.loads(program.out.read_text())
    assert document["error"]["type"] == "RemoteCanceled"
    _assert_gone(pid)
    _assert_mirror_emptied(mirror)


def test_dropped_connection_cancels(tmp_path, remote_host, monkeypatch):
    design, mirror = _earlier_success(tmp_path)
    ready = Ready(tmp_path, remote_host)
    agents: list[Any] = []
    real_popen = SshSession.popen

    def popen(self, command: str):
        process = real_popen(self, command)
        if " agent " in command:
            agents.append(process)
        return process

    monkeypatch.setattr(SshSession, "popen", popen)
    pids: list[int] = []

    def drop() -> None:
        pids.append(ready.wait())
        agents[0].kill()  # the connection goes away while the tool runs

    thread = threading.Thread(target=drop, daemon=True)
    thread.start()
    outcome = _request(tmp_path, design)
    thread.join()
    assert isinstance(outcome.error, RemoteCanceled), outcome.document
    assert outcome.document["error"]["type"] == "RemoteCanceled"
    assert "connection" in outcome.document["error"]["message"]
    _assert_gone(pids[0])
    _assert_mirror_emptied(mirror)


def test_rerun_after_late_disconnect_fetches_the_finished_result(
    tmp_path, remote_host, monkeypatch
):
    design = _design(tmp_path)
    real_fetch = client._fetch

    def fetch_after_drop(agent, fetch_all):
        agent.process.kill()  # the connection goes away after the launch finished
        return real_fetch(agent, fetch_all)

    monkeypatch.setattr(client, "_fetch", fetch_after_drop)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RemoteConnectionError), outcome.document
    assert "fetching the results" in outcome.document["error"]["message"]
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    assert not (mirror / "top" / "nextpnr" / "results.json").exists()
    assert (remote_host.workspace() / "xeda_run" / "top" / "nextpnr" / "results.json").is_file()

    monkeypatch.setattr(client, "_fetch", real_fetch)
    document = _succeeded(_request(tmp_path, design))
    assert _states(document) == {"yosys_fpga": "fresh", "nextpnr": "fresh"}
    assert (mirror / "top" / "nextpnr" / "config.txt").is_file()


def test_agent_failure_names_the_step(tmp_path, remote_host, monkeypatch):
    """An agent that refuses a message comes back as an error that names the step."""
    real = client._launch

    def bad_launch(agent, request, launch_id, setup):
        return real(
            agent, request.model_copy(update={"run_root": "/not/the/run/root"}), launch_id, setup
        )

    monkeypatch.setattr(client, "_launch", bad_launch)
    outcome = _request(tmp_path, _design(tmp_path))
    assert isinstance(outcome.error, RemoteError) and not outcome.success
    message = outcome.document["error"]["message"]
    assert "running the launch" in message and "run root" in message


# --- A link in the mirror -----------------------------------------------------------------------


def _keep_beside_the_mirror(tmp_path: Path, *, leads: str) -> tuple[Path, Path]:
    """Return the sentinel files of two places that a link in the mirror may lead to: the run
    root's `b` and a directory outside it. Each holds the run directories of the request."""
    inside = tmp_path / "local" / "xeda_run" / "b"
    outside = tmp_path / "outside"
    for home in (inside, outside):
        for flow in ("yosys_fpga", "nextpnr"):
            (home / flow).mkdir(parents=True)
            (home / flow / "KEEP").write_text("keep\n")
    return inside, outside


def _replace_with_link(path: Path, target: Path) -> None:
    import shutil

    shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("leads", ["inside", "outside"])
@pytest.mark.parametrize("level", ["mirror", "design"])
def test_a_link_in_the_mirror_is_refused_before_the_host_runs_anything(
    tmp_path, remote_host, leads, level
):
    design, mirror = _earlier_success(tmp_path)
    inside, outside = _keep_beside_the_mirror(tmp_path, leads=leads)
    home = inside if leads == "inside" else outside
    link = mirror if level == "design" else mirror.parent
    _replace_with_link(link, home)
    connections = sum("-M" in call for call in remote_host.remote.calls("ssh"))
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RunDirectoryError), outcome.document
    assert f"{link} is a symbolic link" in outcome.document["error"]["message"]
    now = sum("-M" in call for call in remote_host.remote.calls("ssh"))
    assert now == connections, "no connection to the host was opened"
    for place in (inside, outside):
        for flow in ("yosys_fpga", "nextpnr"):
            assert (place / flow / "KEEP").read_text() == "keep\n"
            assert sorted(p.name for p in (place / flow).iterdir()) == ["KEEP"]


@pytest.mark.parametrize("leads", ["inside", "outside"])
def test_a_link_that_appears_while_the_host_runs_empties_nothing(
    tmp_path, remote_host, monkeypatch, leads
):
    """The mirror is judged again when its files arrive: the lock refuses the link before the
    mirror is emptied or written."""
    design, mirror = _earlier_success(tmp_path)
    inside, outside = _keep_beside_the_mirror(tmp_path, leads=leads)
    real_fetch = client._fetch

    def fetch_then_link(agent, fetch_all):
        fetched = real_fetch(agent, fetch_all)
        _replace_with_link(mirror, inside if leads == "inside" else outside)
        return fetched

    monkeypatch.setattr(client, "_fetch", fetch_then_link)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RunDirectoryError), outcome.document
    assert f"{mirror} is a symbolic link" in outcome.document["error"]["message"]
    for place in (inside, outside):
        for flow in ("yosys_fpga", "nextpnr"):
            assert sorted(p.name for p in (place / flow).iterdir()) == ["KEEP"]


# --- The records of the mirror -------------------------------------------------------------------


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_records_of_the_mirror_name_the_paths_of_this_side(tmp_path, remote_host):
    design = _design(tmp_path)
    document = _succeeded(_request(tmp_path, design, chain="yosys_fpga+nextpnr"))
    mirror = Path(document["mirror"])
    workspace = str(remote_host.workspace())
    for node in document["nodes"]:
        directory = Path(node["run_path"])
        for name in ("results.json", "settings.json"):
            text = (directory / name).read_text()
            assert workspace not in text, f"{directory / name} names a path of the remote"
            assert str(remote_host.home) not in text
        assert json.loads((directory / "results.json").read_text())["outcome"] == "passed"
    directory = Path(document["run_path"])
    saved = json.loads((directory / "results.json").read_text())
    # the record names its files relative to the run directory: the mirror has them where the
    # remote had them, and the document that the run printed names them in the mirror
    assert saved["artifacts"]["textcfg"] == "config.txt"
    assert saved["outputs"]["config"]["names"] == ["config.txt"]
    assert document["results"]["artifacts"]["textcfg"] == str(directory / "config.txt")
    assert document["results"]["outputs"]["config"]["path"] == str(directory / "config.txt")
    # the design of the settings is the local one: its sources are the files that were sent
    settings = json.loads((directory / "settings.json").read_text())
    assert settings["design"]["design_root"] == str(design.parent)
    sources = [item["file"] for item in settings["design"]["rtl"]["sources"]]
    assert sources == [str(design.parent / "top.v")]
    assert all(
        Path(text).is_relative_to(mirror) or Path(text).is_relative_to(design.parent)
        for text in _strings(settings)
        if text.startswith("/") and Path(text).is_relative_to(tmp_path)
    )


def test_the_recorded_results_and_the_next_delivery_agree(tmp_path, remote_host):
    """`--outputs-to` finds the copy that the last fetch delivered through the records of the
    mirror, which name paths of the mirror."""
    design = _design(tmp_path)
    out = tmp_path / "out"
    first = _succeeded(_request(tmp_path, design, outputs_to=out))
    assert first["results"]["deliveries"]
    # the copy is Xeda's own and unchanged: the next delivery replaces it without asking
    again = _succeeded(_request(tmp_path, design, outputs_to=out))
    assert again["results"]["deliveries"]
    # a file of the user's at the destination is refused, before any connection
    (out / "config.txt").write_text("the user's own file\n")
    outcome = _request(tmp_path, design, outputs_to=out)
    assert outcome.error is not None and not outcome.success
    assert "config.txt" in outcome.document["error"]["message"]
    assert len([call for call in remote_host.remote.calls("ssh") if "-M" in call]) == 2
    assert (out / "config.txt").read_text() == "the user's own file\n"


# --- Launch options mean on the remote what they mean locally ------------------------------------


def test_hashed_run_dirs_give_hashed_directories_on_the_remote_and_in_the_mirror(
    tmp_path, remote_host
):
    design = _design(tmp_path)
    document = _succeeded(_request(tmp_path, design, hashed_run_dirs=True))
    flowrun_hash = document["results"]["flow_hash"]
    name = f"nextpnr_{flowrun_hash[:16]}"
    remote_dir = remote_host.workspace() / "xeda_run" / "top" / name
    mirror_dir = tmp_path / "local" / "xeda_run" / "@somewhere" / "top" / name
    assert (remote_dir / "results.json").is_file()
    assert (mirror_dir / "results.json").is_file()
    assert Path(document["run_path"]) == mirror_dir
    assert not (remote_host.workspace() / "xeda_run" / "top" / "nextpnr").exists()
    # another setting is another directory, on the remote and in the mirror
    other = _succeeded(_request(tmp_path, _design(tmp_path, freq="90 MHz"), hashed_run_dirs=True))
    assert Path(other["run_path"]) != mirror_dir and Path(other["run_path"]).is_dir()
    assert mirror_dir.is_dir()


@pytest.mark.parametrize("option", ["rebuild_all", "clean"])
def test_rebuild_all_and_clean_run_the_remote_flows_again(tmp_path, remote_host, option):
    design = _design(tmp_path)
    assert _states(_succeeded(_request(tmp_path, design))) == {
        "yosys_fpga": "ran",
        "nextpnr": "ran",
    }
    again = _succeeded(_request(tmp_path, design, **{option: True}))
    assert _states(again) == {"yosys_fpga": "ran", "nextpnr": "ran"}


# --- A failed dry run, a failure before any node, and Ctrl-C after the launch -------------------


def _failing_dry_run(monkeypatch) -> None:
    real = client._launch

    def failed(agent, request, launch_id, setup):
        document = real(agent, request, launch_id, setup)
        return {
            **document,
            "success": False,
            "error": {"type": "FlowSettingsError", "message": "the plan is not valid"},
        }

    monkeypatch.setattr(client, "_launch", failed)


def test_a_failed_remote_dry_run_exits_with_status_1(tmp_path, remote_host, monkeypatch):
    design = _design(tmp_path)
    _failing_dry_run(monkeypatch)
    result = _cli(tmp_path, design, "nextpnr", "--dry-run")
    assert result.exit_code == 1, result.output
    assert "the plan is not valid" in result.output
    assert "Remote dry run of flow 'nextpnr' on 'somewhere' failed" in result.output
    result = _cli(tmp_path, design, "nextpnr", "--dry-run", "--json")
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["success"] is False and document["dry_run"] is True


def test_a_dry_run_document_names_the_places_of_the_mirror(tmp_path, remote_host):
    document = _succeeded(_request(tmp_path, _design(tmp_path), dry_run=True))
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    assert document["mirror"] == str(mirror)
    workspace = remote_host.workspace()
    for node in document["plan"]["nodes"]:
        name = node["name"]
        assert node["run_path"] == str(mirror / "top" / name)
        assert node["remote_run_path"] == str(workspace / "xeda_run" / "top" / name)
    assert not mirror.exists(), "a dry run writes no mirror"


def test_a_failure_before_any_node_runs_replaces_the_mirror(tmp_path, remote_host, monkeypatch):
    """The remote resolves another plan than this side: it runs no node and the launch fails.
    The records of an earlier run in the mirror must not stay and say that the run passed."""
    design = _design(tmp_path)
    ok = _succeeded(_request(tmp_path, design))
    mirror = Path(ok["mirror"])
    assert (
        json.loads((mirror / "top" / "nextpnr" / "results.json").read_text())["outcome"] == "passed"
    )
    real = client._launch

    def another_plan(agent, request, launch_id, setup):
        expected = {name: "0" * 64 for name in request.expected}
        return real(agent, request.model_copy(update={"expected": expected}), launch_id, setup)

    monkeypatch.setattr(client, "_launch", another_plan)
    outcome = _request(tmp_path, design, rebuild_all=True)
    assert not outcome.success, outcome.document
    assert outcome.document["nodes"] == [] or all(
        node["state"] != "ran" for node in outcome.document["nodes"]
    )
    for name in ("yosys_fpga", "nextpnr"):
        directory = mirror / "top" / name
        assert not (directory / "results.json").exists(), name
        assert not list(directory.glob("*")), name


def test_ctrl_c_while_fetching_says_that_the_launch_finished(tmp_path, remote_host, monkeypatch):
    design = _design(tmp_path)

    def interrupted(agent, fetch_all):
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "_fetch", interrupted)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RemoteInterrupted), outcome.document
    assert outcome.document["error"]["type"] == "RemoteInterrupted"
    message = outcome.document["error"]["message"]
    assert "while fetching the results" in message and "had finished" in message
    assert "never started" not in message
    assert (remote_host.workspace() / "xeda_run" / "top" / "nextpnr" / "results.json").is_file()


def test_ctrl_c_while_delivering_says_that_the_launch_finished(tmp_path, remote_host, monkeypatch):
    design = _design(tmp_path)

    def interrupted(*arguments, **keywords):
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "deliver_fetched", interrupted)
    outcome = _request(tmp_path, design, outputs_to=tmp_path / "out")
    assert isinstance(outcome.error, RemoteInterrupted), outcome.document
    message = outcome.document["error"]["message"]
    assert "while delivering the outputs" in message
    assert f"in the mirror {tmp_path / 'local' / 'xeda_run' / '@somewhere'}" in message
    assert "--outputs-to may be partly written" in message


def test_ctrl_c_before_the_launch_says_it_never_started(tmp_path, remote_host, monkeypatch):
    def interrupted(*arguments, **keywords):
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "_sync", interrupted)
    outcome = _request(tmp_path, _design(tmp_path))
    assert isinstance(outcome.error, RemoteCanceled), outcome.document
    assert "never started" in outcome.document["error"]["message"]


# --- The signal handler and the heartbeat ---------------------------------------------------------


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(), reason="needs the main thread"
)
def test_the_sigint_handler_is_set_during_the_launch_only(tmp_path, remote_host, monkeypatch):
    before = signal.getsignal(signal.SIGINT)
    during: list = []
    real = client._Agent.message

    def message(self, step):
        during.append(signal.getsignal(signal.SIGINT))
        return real(self, step)

    monkeypatch.setattr(client._Agent, "message", message)
    _succeeded(_request(tmp_path, _design(tmp_path)))
    assert signal.getsignal(signal.SIGINT) == before
    assert any(handler != before for handler in during), "no handler during the launch"


def test_heartbeats_start_before_the_launch_message(tmp_path, remote_host, monkeypatch):
    events: list[str] = []
    real_start, real_send = client._Agent.start_heartbeat, client._Agent.send

    def start(self):
        events.append("heartbeat")
        return real_start(self)

    def send(self, document, step):
        if isinstance(document, dict):
            events.append("launch" if "launch" in document else sorted(document)[0])
        return real_send(self, document, step)

    monkeypatch.setattr(client._Agent, "start_heartbeat", start)
    monkeypatch.setattr(client._Agent, "send", send)
    _succeeded(_request(tmp_path, _design(tmp_path)))
    assert "launch" in events and events.index("heartbeat") < events.index("launch")
    assert events.index("heartbeat") < events.index("sync")


# --- A result cannot send the client outside the mirror; an error clears the mirror --------------


def test_the_run_directories_of_a_result_must_lie_below_the_remote_run_root():
    root = PurePosixPath("/ws/xeda_run")
    assert client._node_dirs({"nodes": [{"node": "a", "run_path": "/ws/xeda_run/d/a"}]}, root) == {
        "a": PurePosixPath("d/a")
    }
    for path in (
        "/ws/xeda_run/../other",
        "/ws/xeda_run/d/../../other",
        "/ws/xeda_run",
        "/elsewhere/d/a",
        "d/a",
        None,
    ):
        with pytest.raises(RemoteProtocolError):
            client._node_dirs({"nodes": [{"node": "a", "run_path": path}]}, root)
    with pytest.raises(RemoteProtocolError):
        client._node_dirs({"nodes": [{"run_path": "/ws/xeda_run/d/a"}]}, root)
    mirror = Path("/mirror")
    for document in (
        {"nodes": [{"node": "a", "run_path": "/ws/xeda_run/../x"}]},
        {"run_path": "/ws/xeda_run/d/../../x"},
    ):
        with pytest.raises(RemoteProtocolError):
            client._mirror_document(
                document, None, host="h", mirror_root=mirror, remote_run_root=root
            )


def test_a_result_with_a_run_directory_outside_the_mirror_changes_nothing_there(
    tmp_path, remote_host, monkeypatch
):
    design = _design(tmp_path)
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    canary = tmp_path / "local" / "xeda_run" / "canary" / "results.json"
    canary.parent.mkdir(parents=True)
    (tmp_path / "local" / "xeda_run" / ".xeda-run-root").touch()
    canary.write_text("the user's own file\n")
    real = client._launch

    def crafted(agent, request, launch_id, setup):
        result = dict(real(agent, request, launch_id, setup))
        result["nodes"] = [
            {**node, "run_path": f"{request.run_root}/../canary"} for node in result["nodes"]
        ]
        return result

    monkeypatch.setattr(client, "_launch", crafted)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RemoteProtocolError), outcome.document
    assert canary.read_text() == "the user's own file\n"
    assert not mirror.exists() or not list(mirror.rglob("results.json"))


@pytest.mark.parametrize(
    "error",
    [
        RemoteProtocolError("the agent sent garbage"),
        RemoteConnectionError("the connection broke"),
        RemoteCanceled("canceled"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_an_error_after_the_launch_started_clears_the_mirror(
    tmp_path, remote_host, monkeypatch, error
):
    design, mirror = _earlier_success(tmp_path)

    def failing(agent, request, launch_id, setup):
        raise error

    monkeypatch.setattr(client, "_launch", failing)
    outcome = _request(tmp_path, design)
    assert outcome.error is error and not outcome.success
    _assert_mirror_emptied(mirror)


def test_an_error_while_fetching_clears_the_mirror(tmp_path, remote_host, monkeypatch):
    design, mirror = _earlier_success(tmp_path)

    def failing(agent, fetch_all):
        raise RemoteConnectionError("the connection broke")

    monkeypatch.setattr(client, "_fetch", failing)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RemoteConnectionError)
    _assert_mirror_emptied(mirror)


def test_a_cut_in_the_transfer_after_results_json_clears_the_mirror(
    tmp_path, remote_host, monkeypatch
):
    design, mirror = _earlier_success(tmp_path)
    published = client.publish_mirror

    def cut_after_the_files(*arguments, **keywords):
        published(*arguments, **keywords)
        assert (mirror / "nextpnr" / "results.json").is_file()  # the new files are there
        raise RemoteProtocolError("the connection ended inside a data stream")

    monkeypatch.setattr(client, "publish_mirror", cut_after_the_files)
    outcome = _request(tmp_path, design)
    assert isinstance(outcome.error, RemoteProtocolError) and not outcome.success
    _assert_mirror_emptied(mirror)
