"""The remote launch entry runs a packaged request and records its result.

Each test runs `python -m xeda.remote.launch_entry` as a subprocess of the test interpreter, as
the remote agent does. The subprocess inherits the PATH with the fake tools.
"""

import fcntl
import json
import os
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

import pytest

from xeda.flow_runner import DefaultRunner
from xeda.remote.launch_entry import LaunchRequest, RunDirectorySnapshot, check_identity
from xeda.remote.errors import RemoteIncompatible

from .tool_utils import use_fake_fpga_tools

pytestmark = pytest.mark.python_compat

#: A part for which the fake `nextpnr-ecp5` reports 250 MHz.
PART = "LFE5U-25F-6BG381C"


def run_entry(
    command: Sequence[str],
    *,
    locks: Sequence[int] = (),
    message: Optional[bytes] = None,
    **keywords: Any,
) -> subprocess.CompletedProcess:
    """Run the entry `command` with the socket from which it takes its locks, as the agent does:
    the number of `locks` is the message that the agent sends (unless `message` is given), and
    the descriptors go with it. With `message` and no locks, the socket holds no descriptor."""
    sender, receiver = socket.socketpair()
    with sender, receiver:
        socket.send_fds(sender, [str(len(locks)).encode() if message is None else message], locks)
        sender.close()
        return subprocess.run(
            [*command, str(receiver.fileno())], pass_fds=(receiver.fileno(),), **keywords
        )


def make_inputs(tmp_path: Path, frequency: str = "100 MHz") -> Path:
    """Write a design with one Verilog source in the inputs directory and return its file."""
    inputs = tmp_path / "remote" / "inputs" / "top"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = inputs / "top.yaml"
    design.write_text(
        "name: top\n"
        "rtl:\n  sources: [top.v]\n  top: top\n  clock.port: clk\n"
        f"flows:\n  nextpnr:\n    fpga.part: {PART}\n    clock.freq: {frequency}\n"
    )
    return design


def expected_hashes(tmp_path: Path, design: Path) -> dict[str, str]:
    """Return what the local plan says of the request: the identity of every node."""
    plan = DefaultRunner(tmp_path / "local_root").plan("nextpnr", design=design)
    return {node.name: node.flowrun_hash for node in plan.nodes}


def launch(
    tmp_path: Path,
    design: Path,
    *,
    expected: Optional[dict[str, str]] = None,
    name: str = "launch",
    env: Optional[dict[str, str]] = None,
    **fields: Any,
) -> tuple[subprocess.CompletedProcess, dict[str, Any]]:
    """Run the entry on `design` and return the process and its `result.json`."""
    launch_dir = tmp_path / "remote" / "launches" / name
    launch_dir.mkdir(parents=True, exist_ok=True)
    request = LaunchRequest(
        chain="nextpnr",
        design_file=design.name,
        inputs_dir=str(design.parent),
        run_root=str(tmp_path / "remote" / "xeda_run"),
        path_identities={},
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        log_level=20,
        detailed_logs=False,
        expected=expected_hashes(tmp_path, design) if expected is None else expected,
    ).model_copy(update=fields)
    (launch_dir / "request.json").write_text(request.model_dump_json())
    process = run_entry(
        [sys.executable, "-m", "xeda.remote.launch_entry", str(launch_dir)],
        cwd=tmp_path,
        env=dict(os.environ, **(env or {})),
        stdin=subprocess.DEVNULL,  # the agent starts the entry without a terminal
        capture_output=True,
        text=True,
        timeout=600,
    )
    result = launch_dir / "result.json"
    assert result.is_file(), process.stderr
    return process, json.loads(result.read_text())


@pytest.fixture
def fake_fpga(tmp_path, monkeypatch):
    use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")


def test_result_is_the_json_document_of_a_local_run(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    process, result = launch(tmp_path, design)
    assert process.returncode == 0, process.stderr
    local = subprocess.run(
        [
            sys.executable,
            "-m",
            "xeda",
            "run",
            "nextpnr",
            str(design),
            "--run-root",
            str(tmp_path / "local_root"),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=600,
    )
    assert local.returncode == 0, local.stderr
    document = json.loads(local.stdout)
    assert result["outcome"] == "passed" and document["outcome"] == "passed"
    assert set(result) == set(document) | {"written", "unreadable", "named"}
    assert result["roots"] == document["roots"]
    assert result["spec"]["flow"] == document["spec"]["flow"] == "nextpnr"
    assert result["spec"]["design"] == document["spec"]["design"]
    assert result["spec"]["target"] == document["spec"]["target"]

    def shown(nodes):
        """The nodes without what differs between two launches: the places and the time."""
        return [
            {k: v for k, v in node.items() if k not in ("run_dir", "record", "elapsed")}
            for node in nodes
        ]

    assert shown(result["nodes"]) == shown(document["nodes"])
    assert [node["disposition"] for node in result["nodes"]] == ["ran", "ran"]
    for remote_node, local_node in zip(result["nodes"], document["nodes"]):
        for key in ("identity", "metrics", "outputs"):
            assert remote_node["record"][key] == local_node["record"][key], key
        assert Path(remote_node["run_dir"]).is_relative_to(tmp_path / "remote" / "xeda_run")
    # Each node that ran lists the files it wrote, relative to its run directory.
    assert set(result["written"]) == {node["name"] for node in result["nodes"]}
    assert "results.json" in result["written"]["nextpnr"]
    assert all(
        not Path(name).is_absolute() for names in result["written"].values() for name in names
    )


def test_identity_mismatch_is_refused_before_anything_runs(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    expected = expected_hashes(tmp_path, design)
    expected["nextpnr"] = "0" * len(expected["nextpnr"])
    process, result = launch(tmp_path, design, expected=expected)
    assert process.returncode == 1
    assert result["outcome"] == "failed" and result["error"]["code"] == "remote_failed"
    assert result["error"]["message"].startswith("The remote resolved another plan")
    assert "`nextpnr`" in result["error"]["message"]
    # nothing ran: every planned node is skipped
    assert result["written"] == {}
    assert {node["outcome"] for node in result["nodes"]} == {"skipped"}
    assert not (tmp_path / "remote" / "xeda_run").exists()


def test_a_design_moved_to_the_inputs_directory_keeps_its_identity(tmp_path, fake_fpga):
    local = make_inputs(tmp_path / "local")
    expected = expected_hashes(tmp_path, local)
    moved = make_inputs(tmp_path / "other")
    process, result = launch(tmp_path, moved, expected=expected)
    assert process.returncode == 0, process.stderr
    assert result["outcome"] == "passed"


def test_a_node_missing_from_the_expected_plan_is_refused(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    expected = expected_hashes(tmp_path, design)
    del expected["nextpnr"]
    _process, result = launch(tmp_path, design, expected=expected)
    assert result["error"]["code"] == "remote_failed"
    assert "the remote plan has the node `nextpnr`" in result["error"]["message"]
    assert not (tmp_path / "remote" / "xeda_run").exists()


def test_failed_node_records_only_what_this_run_wrote(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    first_process, first = launch(tmp_path, design, name="first")
    assert first["outcome"] == "passed", first_process.stderr
    run_dir = Path(first["nodes"][-1]["run_dir"])
    outputs = {p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file()}
    assert len(outputs) > 3
    # Another clock makes both nodes stale. The fake `nextpnr` fails when it starts, so its run
    # writes its own records and none of the outputs of the first run.
    changed = make_inputs(tmp_path, "120 MHz")
    _process, second = launch(
        tmp_path,
        changed,
        name="second",
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"},
    )
    assert second["outcome"] == "failed"
    assert Path(second["nodes"][-1]["run_dir"]) == run_dir
    written = set(second["written"]["nextpnr"])
    assert "results.json" in written
    assert json.loads((run_dir / "results.json").read_text())["outcome"] == "failed"
    # The outputs of the first run that this run did not write are not in the record.
    assert {"config.txt", "report.json", "nextpnr.log"} <= outputs
    assert not written & {"config.txt", "report.json", "nextpnr.log"}
    assert written <= {"env.sh", "fake_fpga.calls.jsonl", "results.json", "settings.json"}
    assert set(second["written"]) == {"yosys_fpga", "nextpnr"}


def test_a_blocked_consumer_writes_nothing_while_the_failed_producer_run_rewrites(
    tmp_path, fake_fpga
):
    design = make_inputs(tmp_path)
    _process, first = launch(tmp_path, design, name="first")
    run_dir = Path(first["nodes"][-1]["run_dir"])
    assert json.loads((run_dir / "results.json").read_text())["outcome"] == "passed"
    changed = make_inputs(tmp_path, "120 MHz")
    _process, second = launch(
        tmp_path,
        changed,
        name="second",
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "yosys"},
    )
    assert second["outcome"] == "failed"
    states = {node["flow"]: node["outcome"] for node in second["nodes"]}
    assert states["nextpnr"] == "blocked"
    # The consumer did not run: its record is the first run's, and the launch wrote no file of it.
    assert json.loads((run_dir / "results.json").read_text())["outcome"] == "passed"
    assert second["written"]["nextpnr"] == []
    assert "results.json" in second["written"]["yosys_fpga"]


def test_second_launch_reuses_a_fresh_node(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    _process, first = launch(tmp_path, design, name="first")
    assert [node["disposition"] for node in first["nodes"]] == ["ran", "ran"]
    _process, second = launch(tmp_path, design, name="second")
    assert second["outcome"] == "passed"
    assert [node["disposition"] for node in second["nodes"]] == ["reused", "reused"]
    assert second["written"] == {}


def test_scrub_on_the_host_removes_nothing_and_says_so(tmp_path, fake_fpga):
    """The host has no terminal and the protocol has no question, so nobody confirms the removal
    of the older runs: the launch succeeds, keeps them and logs that."""
    old_design = make_inputs(tmp_path, "100 MHz")
    _process, first = launch(tmp_path, old_design, name="first", hashed_run_dirs=True)
    old_runs = [Path(node["run_dir"]) for node in first["nodes"]]
    assert all(run.is_dir() for run in old_runs)
    design = make_inputs(tmp_path, "200 MHz")
    process, second = launch(tmp_path, design, name="second", hashed_run_dirs=True, scrub=True)
    assert process.returncode == 0, process.stderr
    assert second["outcome"] == "passed"
    assert all(run.is_dir() for run in old_runs)
    assert "Not removing" in process.stdout + process.stderr
    assert "EOFError" not in process.stdout + process.stderr


def test_dry_run_document(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    process, result = launch(tmp_path, design, dry_run=True)
    assert process.returncode == 0, process.stderr
    assert result["success"] is True and result["dry_run"] is True
    assert "written" not in result
    assert [node["flowrun_hash"] for node in result["plan"]["nodes"]] == list(
        expected_hashes(tmp_path, design).values()
    )
    assert {node["state"] for node in result["plan"]["nodes"]} == {"stale"}
    assert not (tmp_path / "remote" / "xeda_run").exists()


def test_unexpected_exception_still_writes_a_failure_document(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    # The inputs directory is missing: the entry cannot change into it.
    process, result = launch(
        tmp_path, design, inputs_dir=str(tmp_path / "remote" / "inputs" / "nothing")
    )
    assert process.returncode == 1
    assert result["outcome"] == "failed"
    assert result["error"]["code"] == "internal_error"
    assert "nothing" in result["error"]["message"]
    assert result["spec"]["flow"] == "nextpnr"
    assert result["nodes"] == [] and result["written"] == {}


def test_a_request_file_that_is_not_a_request_gets_a_failure_document(tmp_path):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    (launch_dir / "request.json").write_text('{"chain": "nextpnr"}')
    process = run_entry(
        [sys.executable, "-m", "xeda.remote.launch_entry", str(launch_dir)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert process.returncode == 1
    result = json.loads((launch_dir / "result.json").read_text())
    assert result["outcome"] == "failed" and result["error"]["code"] == "remote_failed"
    assert "request.json is not a launch request" in result["error"]["message"]


def test_check_identity_names_every_difference():
    check_identity({"a": "1", "b": "2"}, {"b": "2", "a": "1"})
    with pytest.raises(RemoteIncompatible) as caught:
        check_identity({"a": "1", "c": "3"}, {"a": "9", "b": "2"})
    message = str(caught.value)
    assert "`b`" in message and "`c`" in message and "`a` has the identity 1" in message


def test_snapshot_tells_new_changed_and_unchanged_files(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "sub").mkdir(parents=True)
    (run_dir / "old.txt").write_text("old")
    (run_dir / "sub" / "same.txt").write_text("same")
    (run_dir / "link").symlink_to(tmp_path)
    snapshot = RunDirectorySnapshot([run_dir])
    (run_dir / "sub" / "new.txt").write_text("new")
    (run_dir / "old.txt").write_text("different size")
    (run_dir / "moved.txt").write_text("m")
    assert snapshot.written_under(run_dir) == (["moved.txt", "old.txt", "sub/new.txt"], [])
    assert RunDirectorySnapshot([tmp_path / "absent"]).written_under(tmp_path / "absent") == (
        [],
        [],
    )


def test_snapshot_never_scans_through_a_link_to_the_run_directory(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file.txt").write_text("x")
    run_dir = tmp_path / "run"
    run_dir.symlink_to(elsewhere, target_is_directory=True)
    snapshot = RunDirectorySnapshot([run_dir])
    (elsewhere / "later.txt").write_text("y")
    assert snapshot.states == {}
    assert snapshot.written_under(run_dir) == ([], [])


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a directory that denies")
def test_snapshot_names_a_directory_that_became_unreadable(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "closed").mkdir(parents=True)
    (run_dir / "open").mkdir()
    (run_dir / "closed" / "a.txt").write_text("a")
    snapshot = RunDirectorySnapshot([run_dir])
    (run_dir / "open" / "new.txt").write_text("n")
    (run_dir / "closed" / "b.txt").write_text("b")
    (run_dir / "closed").chmod(0)
    try:
        written, unreadable = snapshot.written_under(run_dir)
    finally:
        (run_dir / "closed").chmod(0o700)
    assert written == ["open/new.txt"]
    assert unreadable == ["closed"]


#: Starts the entry in a process whose launcher raises `exception` instead of running. It leaves a
#: file in the run directory of the requested node first, as a launch that did some work would.
RAISING_LAUNCH = """
import sys
from xeda.flow_runner import DefaultRunner
from xeda.remote.launch_entry import main

def run(self, *args, **kwargs):
    raise {exception}

DefaultRunner.run = run
sys.exit(main(sys.argv[1:]))
"""


def launch_raising(tmp_path: Path, exception: str) -> tuple[subprocess.CompletedProcess, dict]:
    design = make_inputs(tmp_path)
    launch_dir = tmp_path / "remote" / "launches" / "raising"
    launch_dir.mkdir(parents=True)
    request = LaunchRequest(
        chain="nextpnr",
        design_file=design.name,
        inputs_dir=str(design.parent),
        run_root=str(tmp_path / "remote" / "xeda_run"),
        path_identities={},
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        log_level=20,
        detailed_logs=False,
        expected=expected_hashes(tmp_path, design),
    )
    (launch_dir / "request.json").write_text(request.model_dump_json())
    process = run_entry(
        [sys.executable, "-c", RAISING_LAUNCH.format(exception=exception), str(launch_dir)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return process, json.loads((launch_dir / "result.json").read_text())


def test_an_interrupt_gives_a_failure_document_and_status_130(tmp_path):
    process, result = launch_raising(tmp_path, "KeyboardInterrupt()")
    assert process.returncode == 130, process.stderr
    assert result["outcome"] == "canceled" and result["error"]["code"] == "canceled"
    assert result["spec"]["flow"] == "nextpnr"
    assert result["written"] == {} and result["unreadable"] == {}


def test_a_system_exit_gives_a_failure_document_and_is_raised_again(tmp_path):
    process, result = launch_raising(tmp_path, "SystemExit(3)")
    assert process.returncode == 3, process.stderr
    assert result["outcome"] == "failed" and result["error"]["code"] == "internal_error"
    assert "SystemExit" in result["error"]["message"]
    assert "written" in result and "unreadable" in result


def tree(root: Path) -> set[Path]:
    return {Path(dirpath, name) for dirpath, dirs, files in os.walk(root) for name in dirs + files}


def test_the_entry_writes_only_in_the_run_root_and_the_launch_directory(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    expected = expected_hashes(tmp_path, design)
    (tmp_path / "remote" / "launches" / "launch").mkdir(parents=True)
    before = tree(tmp_path)
    process, result = launch(tmp_path, design, expected=expected)
    assert process.returncode == 0, process.stderr
    new = tree(tmp_path) - before
    run_root, launch_dir = (
        tmp_path / "remote" / "xeda_run",
        tmp_path / "remote" / "launches" / "launch",
    )
    outside = sorted(p for p in new if not p.is_relative_to(run_root) and p.parent != launch_dir)
    assert new and not outside, outside
    assert (tmp_path / "remote" / "launches" / "launch" / "result.json") in new
    # The launch directory holds the request (written by `launch` above), the result, and nothing
    # left over from the atomic write of the result.
    assert {p.name for p in new if p.parent == launch_dir} == {"request.json", "result.json"}


# --- The hand-over of the locks ------------------------------------------------------------------


def entry_command(launch_dir: Path) -> list[str]:
    return [sys.executable, "-m", "xeda.remote.launch_entry", str(launch_dir)]


def failed_before_anything_ran(tmp_path: Path, process: subprocess.CompletedProcess) -> str:
    """Check that the entry stopped at the hand-over, with a failure document. Return the message
    of the failure."""
    launch_dir = tmp_path / "launch"
    assert process.returncode == 1, process.stderr
    result = json.loads((launch_dir / "result.json").read_text())
    assert result["outcome"] == "failed" and result["nodes"] == [] and result["spec"] is None
    message = result["error"]["message"]
    assert message in process.stderr
    assert not (tmp_path / "remote" / "xeda_run").exists()  # no run
    return message


@pytest.fixture
def waiting_launch(tmp_path):
    """A launch directory whose request is complete and valid: the entry would run it if the
    hand-over worked."""
    design = make_inputs(tmp_path)
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    request = LaunchRequest(
        chain="nextpnr",
        design_file=design.name,
        inputs_dir=str(design.parent),
        run_root=str(tmp_path / "remote" / "xeda_run"),
        path_identities={},
        hashed_run_dirs=False,
        clean=False,
        rebuild_all=False,
        scrub=False,
        dry_run=False,
        log_level=20,
        detailed_logs=False,
        expected={},
    )
    (launch_dir / "request.json").write_text(request.model_dump_json())
    return launch_dir


def test_an_empty_hand_over_socket_fails_the_launch_and_runs_nothing(tmp_path, waiting_launch):
    process = run_entry(
        entry_command(waiting_launch), message=b"", capture_output=True, text=True, timeout=600
    )
    assert "closed with no message" in failed_before_anything_ran(tmp_path, process)


def test_fewer_locks_than_the_agent_sent_fail_the_launch_and_run_nothing(tmp_path, waiting_launch):
    with open(tmp_path / "some-file", "w") as stream:
        process = run_entry(
            entry_command(waiting_launch),
            locks=[stream.fileno()],
            message=b"3",
            capture_output=True,
            text=True,
            timeout=600,
        )
    message = failed_before_anything_ran(tmp_path, process)
    assert "has 1 locks, and the agent sent 3" in message


def test_a_hand_over_that_is_no_socket_fails_the_launch_and_runs_nothing(tmp_path, waiting_launch):
    process = subprocess.run(
        [*entry_command(waiting_launch), "97"], capture_output=True, text=True, timeout=600
    )
    assert "did not get its locks" in failed_before_anything_ran(tmp_path, process)


def test_a_launch_without_the_hand_over_argument_is_a_usage_error(tmp_path, waiting_launch):
    process = subprocess.run(
        entry_command(waiting_launch), capture_output=True, text=True, timeout=600
    )
    assert process.returncode == 2 and "usage:" in process.stderr
    assert not (waiting_launch / "result.json").exists()


TAKING_LOCKS = """
import json, os, sys
from xeda.remote.launch_entry import take_locks

descriptors = take_locks(int(sys.argv[1]))
try:
    os.fstat(int(sys.argv[1]))
    socket_open = True
except OSError:
    socket_open = False
print(json.dumps({
    "count": len(descriptors),
    "inheritable": [os.get_inheritable(d) for d in descriptors],
    "open": [os.fstat(d).st_size >= 0 for d in descriptors],
    "socket_open": socket_open,
}))
"""


def test_the_entry_takes_the_locks_keeps_them_and_closes_the_socket(tmp_path):
    first = (tmp_path / "first").open("wb")
    second = (tmp_path / "second").open("wb")
    with first, second:
        fcntl.flock(first, fcntl.LOCK_EX)
        process = run_entry(
            [sys.executable, "-c", TAKING_LOCKS],
            locks=[first.fileno(), second.fileno()],
            capture_output=True,
            text=True,
            timeout=600,
        )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        "count": 2,
        "inheritable": [False, False],
        "open": [True, True],
        "socket_open": False,
    }
