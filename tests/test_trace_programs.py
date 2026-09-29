"""A trace names every program a run started, fingerprinted by where PATH finds it."""

import subprocess
import sys
from pathlib import Path

import xeda.tool
from xeda.flow_runner import trace
from xeda.flow_runner.trace import ProgramRecord, probe_program
from xeda.proc_utils import DOCKER_IMAGE_PREFIX, note_program, recording_programs, run_process
from xeda.tool import Docker


def test_run_process_records_what_it_starts():
    with recording_programs() as names:
        run_process(sys.executable, ["-c", "pass"])
        run_process(sys.executable, ["-c", "pass"])
    assert names == [sys.executable]


def test_nothing_is_recorded_outside_a_recording():
    note_program("anything")  # no recording active: silently ignored
    with recording_programs() as names:
        pass
    assert names == []


def test_a_program_is_fingerprinted_by_its_resolved_path_size_and_mtime():
    record = probe_program(sys.executable)
    resolved = Path(sys.executable).resolve()
    assert record is not None
    assert (record.path, record.size, record.mtime_ns) == (
        str(resolved),
        resolved.stat().st_size,
        resolved.stat().st_mtime_ns,
    )


def test_a_program_that_is_not_found_has_no_fingerprint():
    assert probe_program("no-such-program-for-xeda-tests") is None


def _docker_inspect(monkeypatch, returncode=0, stdout="", raises=None):
    """Stand in for `docker image inspect`, recording what it was asked."""
    asked = []

    def run(args, **kwargs):
        asked.append(list(args))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(trace.subprocess, "run", run)
    return asked


def test_a_container_image_is_fingerprinted_by_its_id(monkeypatch):
    asked = _docker_inspect(monkeypatch, stdout="sha256:0123abcd\n")
    record = probe_program(f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys")
    assert record == ProgramRecord(path="sha256:0123abcd")
    assert asked == [["docker", "image", "inspect", "--format", "{{.Id}}", "hdlc/ghdl:yosys"]]


def test_a_missing_image_or_docker_has_no_fingerprint(monkeypatch):
    _docker_inspect(monkeypatch, returncode=1)
    assert probe_program(f"{DOCKER_IMAGE_PREFIX}no/such:image") is None
    _docker_inspect(monkeypatch, raises=FileNotFoundError("docker"))
    assert probe_program(f"{DOCKER_IMAGE_PREFIX}no/such:image") is None


def test_running_in_a_container_records_the_image(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(xeda.tool, "run_process", lambda *args, **kwargs: None)
    with recording_programs() as names:
        Docker(image="hdlc/ghdl", tag="yosys").run("ghdl", "--version")
        Docker(image="alpine", tag=None).run("true")
    assert names == [f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys", f"{DOCKER_IMAGE_PREFIX}alpine:latest"]
