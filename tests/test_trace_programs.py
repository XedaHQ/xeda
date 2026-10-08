"""A trace names every program a run started: where PATH finds it, and the file there
(`digest.record_file`, under the trust rule); a container image by its ID."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import ClassVar

import pytest

import xeda.tool
from xeda import Design, digest
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
    """A program is recorded as a file, under the trust rule, not by size and mtime."""
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


def _count_reads(monkeypatch) -> "Counter[Path]":
    """How many times each file's content is read for a record (`digest.content_digest`)."""
    reads: Counter[Path] = Counter()
    real = digest.content_digest

    def counted(path):
        reads[Path(path).resolve()] += 1
        return real(path)

    monkeypatch.setattr(digest, "content_digest", counted)
    return reads


@pytest.fixture
def probe_tool(tmp_path, monkeypatch) -> Path:
    """`probe-tool` first on PATH, the flow `_RunsTool` registered by name, and records that are
    conclusive at once, as they are two seconds later: no wait."""
    program = tmp_path / "bin" / "probe-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\necho one\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(digest, "RACY_NS", 0)
    yield program.resolve()
    for name in (_RunsTool.name, _RunsTool.__name__):
        registered_flows.pop(name, None)


def _launch(tmp_path, **settings) -> Flow:
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)
    return runner.launch_flow(_RunsTool, design, {})


def _recorded(flow: Flow) -> dict:
    """What the flow's trace says of `probe-tool`'s file."""
    document = json.loads((flow.run_path / "trace.json").read_text())
    return document["programs"]["probe-tool"]["file"]


def test_a_program_the_previous_trace_vouches_for_is_not_read_again(
    tmp_path, probe_tool, monkeypatch
):
    """A successful run records the programs it started. The record the previous trace holds is
    kept while the file's metadata vouches for it (`FileRecord.trusted`), as for an input: the
    yosys or nextpnr binary is not read at every run."""
    sha = digest.content_digest(probe_tool)
    reads = _count_reads(monkeypatch)
    first = _launch(tmp_path, rebuild_all=True)
    recorded = _recorded(first)
    assert recorded["sha"] == sha and reads[probe_tool] == 1, "no earlier record: it is read"
    reads.clear()
    for _ in range(2):
        again = _launch(tmp_path, rebuild_all=True)
        assert not again.reused, "every run executes"
    assert reads[probe_tool] == 0, "the record the trace holds was kept"
    assert _recorded(again) == recorded
    assert _launch(tmp_path).reused, "and it still vouches for the file"


def test_a_program_edited_since_the_previous_trace_is_read_again(tmp_path, probe_tool, monkeypatch):
    """An edit that gives the file its old size and mtime back moves its inode change time: the
    old record no longer vouches for it, and the next run records what the file holds now."""
    first = _recorded(_launch(tmp_path, rebuild_all=True))
    st = probe_tool.stat()
    probe_tool.write_text("#!/bin/sh\necho two\n")  # the same size
    os.utime(probe_tool, ns=(st.st_atime_ns, st.st_mtime_ns))
    reads = _count_reads(monkeypatch)
    again = _launch(tmp_path, rebuild_all=True)
    assert reads[probe_tool] == 1
    assert _recorded(again)["sha"] == digest.content_digest(probe_tool) != first["sha"]


def test_a_record_of_a_program_that_changed_during_a_run_is_not_carried_forward(
    tmp_path, probe_tool
):
    """The record of a file that changed while a run used it stands for nothing. The next run
    records the file as it is, however well the old record's metadata matches."""
    flow = _launch(tmp_path, rebuild_all=True)
    document = json.loads((flow.run_path / "trace.json").read_text())
    document["programs"]["probe-tool"]["file"]["sha"] = digest.MODIFIED_DURING_RUN
    (flow.run_path / "trace.json").write_text(json.dumps(document))
    again = _launch(tmp_path, rebuild_all=True)
    assert _recorded(again)["sha"] == digest.content_digest(probe_tool)
    assert _launch(tmp_path).reused
