"""The remote launch entry runs a packaged request and records its result.

Each test runs `python -m xeda.remote.launch_entry` as a subprocess of the test interpreter, as
the remote agent does. The subprocess inherits the PATH with the fake tools.
"""

import json
import os
import subprocess
import sys
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
    process = subprocess.run(
        [sys.executable, "-m", "xeda.remote.launch_entry", str(launch_dir)],
        cwd=tmp_path,
        env=dict(os.environ, **(env or {})),
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
    assert result["success"] is True and document["success"] is True
    assert set(result) == set(document) | {"written", "unreadable"}
    for key in ("flow", "design", "target", "request", "success"):
        assert result[key] == document[key], key
    assert set(result["results"]) == set(document["results"])
    assert [{k: v for k, v in node.items() if k != "run_path"} for node in result["nodes"]] == [
        {k: v for k, v in node.items() if k != "run_path"} for node in document["nodes"]
    ]
    assert [node["state"] for node in result["nodes"]] == ["ran", "ran"]
    assert Path(result["run_path"]).is_relative_to(tmp_path / "remote" / "xeda_run")
    # Each node that ran lists the files it wrote, relative to its run directory.
    assert set(result["written"]) == {node["node"] for node in result["nodes"]}
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
    assert result["success"] is False
    assert result["error"]["type"] == "RemoteIncompatible"
    assert "`nextpnr`" in result["error"]["message"]
    assert result["written"] == {} and result["nodes"] == []
    assert not (tmp_path / "remote" / "xeda_run").exists()


def test_a_design_moved_to_the_inputs_directory_keeps_its_identity(tmp_path, fake_fpga):
    local = make_inputs(tmp_path / "local")
    expected = expected_hashes(tmp_path, local)
    moved = make_inputs(tmp_path / "other")
    process, result = launch(tmp_path, moved, expected=expected)
    assert process.returncode == 0, process.stderr
    assert result["success"] is True


def test_a_node_missing_from_the_expected_plan_is_refused(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    expected = expected_hashes(tmp_path, design)
    del expected["nextpnr"]
    _process, result = launch(tmp_path, design, expected=expected)
    assert result["error"]["type"] == "RemoteIncompatible"
    assert "the remote plan has the node `nextpnr`" in result["error"]["message"]
    assert not (tmp_path / "remote" / "xeda_run").exists()


def test_failed_node_records_only_what_this_run_wrote(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    first_process, first = launch(tmp_path, design, name="first")
    assert first["success"] is True, first_process.stderr
    run_dir = Path(first["run_path"])
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
    assert second["success"] is False
    assert Path(second["run_path"]) == run_dir
    written = set(second["written"]["nextpnr"])
    assert "results.json" in written
    assert json.loads((run_dir / "results.json").read_text())["success"] is False
    # The outputs of the first run that this run did not write are not in the record.
    assert {"config.txt", "report.json", "nextpnr.log"} <= outputs
    assert not written & {"config.txt", "report.json", "nextpnr.log"}
    assert written <= {"env.sh", "fake_fpga.calls.jsonl", "results.json", "settings.json"}
    assert set(second["written"]) == {"yosys_fpga", "nextpnr"}


def test_blocked_consumer_records_what_the_failed_producer_run_rewrote(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    _process, first = launch(tmp_path, design, name="first")
    run_dir = Path(first["run_path"])
    assert json.loads((run_dir / "results.json").read_text())["success"] is True
    changed = make_inputs(tmp_path, "120 MHz")
    _process, second = launch(
        tmp_path,
        changed,
        name="second",
        env={"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "yosys"},
    )
    assert second["success"] is False
    states = {node["flow"]: node["state"] for node in second["nodes"]}
    assert states["nextpnr"] == "blocked"
    # The launch replaced the results of the consumer, so they are in the record. The reports
    # of the first run are not.
    assert json.loads((run_dir / "results.json").read_text())["success"] is False
    assert second["written"]["nextpnr"] == ["results.json"]


def test_second_launch_reuses_a_fresh_node(tmp_path, fake_fpga):
    design = make_inputs(tmp_path)
    _process, first = launch(tmp_path, design, name="first")
    assert [node["state"] for node in first["nodes"]] == ["ran", "ran"]
    _process, second = launch(tmp_path, design, name="second")
    assert second["success"] is True
    assert [node["state"] for node in second["nodes"]] == ["fresh", "fresh"]
    assert second["written"] == {}


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
    assert result["success"] is False
    assert result["error"]["type"] == "FileNotFoundError"
    assert result["flow"] == "nextpnr"
    assert result["nodes"] == [] and result["written"] == {}


def test_a_request_file_that_is_not_a_request_gets_a_failure_document(tmp_path):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    (launch_dir / "request.json").write_text('{"chain": "nextpnr"}')
    process = subprocess.run(
        [sys.executable, "-m", "xeda.remote.launch_entry", str(launch_dir)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert process.returncode == 1
    result = json.loads((launch_dir / "result.json").read_text())
    assert result["success"] is False
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
sys.exit(main([sys.argv[1]]))
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
    process = subprocess.run(
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
    assert result["success"] is False
    assert result["error"]["type"] == "KeyboardInterrupt"
    assert result["flow"] == "nextpnr"
    assert result["written"] == {} and result["unreadable"] == {}


def test_a_system_exit_gives_a_failure_document_and_is_raised_again(tmp_path):
    process, result = launch_raising(tmp_path, "SystemExit(3)")
    assert process.returncode == 3, process.stderr
    assert result["success"] is False
    assert result["error"]["type"] == "SystemExit"
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
