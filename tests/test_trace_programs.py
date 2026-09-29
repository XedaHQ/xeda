"""A trace names every program a run started: where PATH finds it, and the file there
(`digest.record_file`, under the R38 trust rule); a container image by its ID."""

import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import xeda.tool
from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner, trace
from xeda.flow_runner.trace import locate_program
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


def test_a_program_is_located_by_its_resolved_path():
    assert locate_program(sys.executable) == str(Path(sys.executable).resolve())


def test_a_program_that_is_not_found_has_no_location():
    assert locate_program("no-such-program-for-xeda-tests") is None


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


def test_a_container_image_is_located_by_its_id(monkeypatch):
    asked = _docker_inspect(monkeypatch, stdout="sha256:0123abcd\n")
    assert locate_program(f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys") == "sha256:0123abcd"
    assert asked == [["docker", "image", "inspect", "--format", "{{.Id}}", "hdlc/ghdl:yosys"]]


def test_a_missing_image_or_docker_has_no_location(monkeypatch):
    _docker_inspect(monkeypatch, returncode=1)
    assert locate_program(f"{DOCKER_IMAGE_PREFIX}no/such:image") is None
    _docker_inspect(monkeypatch, raises=FileNotFoundError("docker"))
    assert locate_program(f"{DOCKER_IMAGE_PREFIX}no/such:image") is None


def test_running_in_a_container_records_the_image(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(xeda.tool, "run_process", lambda *args, **kwargs: None)
    with recording_programs() as names:
        Docker(image="hdlc/ghdl", tag="yosys").run("ghdl", "--version")
        Docker(image="alpine", tag=None).run("true")
    assert names == [f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys", f"{DOCKER_IMAGE_PREFIX}alpine:latest"]


class _RunsTool(Flow):
    """Runs `probe-tool`, whatever PATH finds."""

    results_description: ClassVar[dict[str, str]] = {}

    def run(self) -> None:
        run_process("probe-tool", [], stdout=True)


def test_a_program_edited_with_its_size_and_mtime_put_back_makes_the_run_stale(
    tmp_path, monkeypatch
):
    """R50 e: a program is recorded as a file, under the R38 rule, not by size and mtime."""
    program = tmp_path / "bin" / "probe-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\necho one\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    try:
        runner.launch_flow(_RunsTool, design, {})
        assert runner.launch_flow(_RunsTool, design, {}).reused
        st = program.stat()
        program.write_text("#!/bin/sh\necho two\n")  # the same size
        os.utime(program, ns=(st.st_atime_ns, st.st_mtime_ns))
        again = runner.launch_flow(_RunsTool, design, {})
        assert not again.reused and again.stale_reason == "probe-tool changed"
    finally:
        for name in (_RunsTool.name, _RunsTool.__name__):
            registered_flows.pop(name, None)
