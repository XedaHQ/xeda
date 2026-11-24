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

import xeda._internal.tool
from xeda._internal.flow import MetricSpec
from xeda import Design
from xeda._internal import digest
from xeda._internal.flow import Flow, registered_flows
from xeda._internal.flow_runner import DefaultRunner, trace
from xeda._internal.flow_runner.trace import locate_program
from xeda._internal.proc_utils import (
    DOCKER_IMAGE_PREFIX,
    note_program,
    recording_programs,
    run_process,
)
from xeda._internal.tool import Docker, Tool
from .scoping import node_settings

pytestmark = pytest.mark.python_compat


def test_run_process_records_what_it_starts():
    with recording_programs() as names:
        run_process(sys.executable, ["-c", "pass"])
        run_process(sys.executable, ["-c", "pass"])
    assert names == [sys.executable]


def test_a_relative_program_is_recorded_where_the_process_starts(tmp_path):
    """`./simv` runs from the directory the process starts in, not from the working directory of
    Xeda, so it is recorded there."""
    program = tmp_path / "simv"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    with recording_programs() as names:
        run_process("./simv", [], cwd=tmp_path)
    assert names == [str(program)]
    assert names.before[str(program)] is not None


def test_the_program_that_the_child_path_finds_is_recorded(tmp_path, monkeypatch):
    """Keep the requested name and record its state through the child's PATH."""
    parent_bin, child_bin = tmp_path / "parent", tmp_path / "child"
    for directory in (parent_bin, child_bin):
        directory.mkdir()
        program = directory / "twin-tool"
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{parent_bin}{os.pathsep}{os.environ['PATH']}")
    with recording_programs() as names:
        run_process("twin-tool", [], cwd=tmp_path, env={"PATH": str(child_bin)})
    assert names == ["twin-tool"]
    child = (child_bin / "twin-tool").stat()
    assert names.before["twin-tool"] == (
        child.st_dev,
        child.st_ino,
        child.st_size,
        child.st_mtime_ns,
        child.st_ctime_ns,
    )


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
    monkeypatch.setattr(xeda._internal.tool, "run_process", lambda *args, **kwargs: None)
    with recording_programs() as names:
        Docker(image="hdlc/ghdl", tag="yosys").run("ghdl", "--version", cwd=tmp_path)
        Docker(image="alpine", tag=None).run("true", cwd=tmp_path)
    assert names == [f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys", f"{DOCKER_IMAGE_PREFIX}alpine:latest"]


def test_an_image_with_a_tag_or_digest_keeps_it_in_the_record_and_the_command(
    monkeypatch, tmp_path
):
    commands = []
    monkeypatch.setattr(
        xeda._internal.tool, "run_process", lambda _cli, cmd, **kwargs: commands.append(cmd)
    )
    images = [
        "hdlc/sim:osvb",
        "localhost:5000/sim",
        "localhost:5000/sim:v2",
        "sim@sha256:0123",
    ]
    with recording_programs() as names:
        for image in images:
            Docker(image=image).run("true", cwd=tmp_path)
    assert names == [
        f"{DOCKER_IMAGE_PREFIX}hdlc/sim:osvb",
        f"{DOCKER_IMAGE_PREFIX}localhost:5000/sim:latest",
        f"{DOCKER_IMAGE_PREFIX}localhost:5000/sim:v2",
        f"{DOCKER_IMAGE_PREFIX}sim@sha256:0123",
    ]
    assert [cmd[-2] for cmd in commands] == [
        "hdlc/sim:osvb",
        "localhost:5000/sim:latest",
        "localhost:5000/sim:v2",
        "sim@sha256:0123",
    ]


def test_stopping_a_container_uses_the_environment_of_the_docker_client(monkeypatch, tmp_path):
    calls = []

    def fake_run_process(cli, args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "run":
            kwargs["on_stop"]()

    monkeypatch.setattr(xeda._internal.tool, "run_process", fake_run_process)
    client_env = {"DOCKER_HOST": "ssh://elsewhere", "PATH": "/bin"}
    Docker(image="alpine").run("true", cwd=tmp_path, client_env=client_env)
    killed = [kwargs for args, kwargs in calls if args[0] == "kill"]
    assert [k["env"] for k in killed] == [client_env]


class TRunsTool(Flow):
    """Runs `probe-tool`, whatever PATH finds."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    def run(self) -> None:
        Tool("probe-tool").run(stdout=True)


@pytest.mark.parametrize("same_initial_match", [False, True])
def test_an_earlier_match_on_the_child_path_makes_the_node_stale(
    tmp_path, probe_tool, same_initial_match
):
    """An unchanged PATH can select a new program after an earlier match is installed."""
    preferred = tmp_path / "preferred"
    preferred.mkdir()
    fallback = probe_tool.parent
    if not same_initial_match:
        fallback = tmp_path / "fallback"
        fallback.mkdir()
        program = fallback / "probe-tool"
        program.write_text("#!/bin/sh\necho child\n")
        program.chmod(0o755)
    settings = {"env": {"PATH": f"{preferred}{os.pathsep}{fallback}"}}
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    runner.launch_flow(TRunsTool, design, settings)
    assert runner.launch_flow(TRunsTool, design, settings).reused
    _, states = runner.plan_with_freshness(
        TRunsTool, design, flow_settings=node_settings(TRunsTool, settings)
    )
    assert states[TRunsTool.name].state == "fresh"

    program = preferred / "probe-tool"
    program.write_text("#!/bin/sh\necho preferred\n")
    program.chmod(0o755)
    _, states = runner.plan_with_freshness(
        TRunsTool, design, flow_settings=node_settings(TRunsTool, settings)
    )
    assert states[TRunsTool.name].state == "stale"
    assert states[TRunsTool.name].reason == "probe-tool changed"
    again = runner.launch_flow(TRunsTool, design, settings)
    assert not again.reused and again.stale_reason == "probe-tool changed"
    assert runner.launch_flow(TRunsTool, design, settings).reused
    recorded = trace.read_trace(again.run_path)[0].programs
    assert set(recorded) == {"probe-tool"}
    assert recorded["probe-tool"].path == str(program.resolve())


def test_a_program_edited_with_its_size_and_mtime_put_back_makes_the_run_stale(
    tmp_path, monkeypatch
):
    """A program is recorded as a file, under the trust rule, not by size and mtime."""
    program = tmp_path / "bin" / "probe-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\necho one\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    try:
        runner.launch_flow(TRunsTool, design, {})
        assert runner.launch_flow(TRunsTool, design, {}).reused
        st = program.stat()
        program.write_text("#!/bin/sh\necho two\n")  # the same size
        os.utime(program, ns=(st.st_atime_ns, st.st_mtime_ns))
        again = runner.launch_flow(TRunsTool, design, {})
        assert not again.reused and again.stale_reason == "probe-tool changed"
    finally:
        for name in (TRunsTool.name, TRunsTool.__name__):
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
    """`probe-tool` first on PATH, the flow `TRunsTool` registered by name, and records that are
    conclusive at once, as they are two seconds later: no wait."""
    program = tmp_path / "bin" / "probe-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\necho one\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(digest, "RACY_NS", 0)
    yield program.resolve()
    for name in (TRunsTool.name, TRunsTool.__name__):
        registered_flows.pop(name, None)


def _launch(tmp_path, **settings) -> Flow:
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)
    return runner.launch_flow(TRunsTool, design, {})


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


class TInContainer(Flow):
    """Runs a program in a container of the fake client `fake-container-cli`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}
    cli: ClassVar[str] = "fake-container-cli"

    def run(self) -> None:
        tool = Tool("true", docker=Docker(image="img:tag", cli=self.cli))
        tool.dockerized = True
        tool.run(stdout=True)


class TInPodman(TInContainer):
    """Runs the same program with a client that is not `docker`."""

    cli: ClassVar[str] = "fake-podman"


_FAKE_CLI = "fake-container-cli"


@pytest.fixture
def container_daemons(tmp_path, monkeypatch):
    """A fake container client whose image ID depends on `DOCKER_HOST`: the ID of `img:tag` is
    the content of the file named for the host in `ids/`. Return a function that sets it."""
    ids = tmp_path / "ids"
    ids.mkdir()
    program = tmp_path / "bin" / _FAKE_CLI
    program.parent.mkdir()
    program.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = image ]; then\n'
        f'  cat "{ids}/${{DOCKER_HOST:-local}}" 2>/dev/null || exit 1\n'
        "fi\n"
    )
    program.chmod(0o755)
    (program.parent / "fake-podman").symlink_to(program)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(digest, "RACY_NS", 0)

    def set_id(host: str, image_id: str) -> None:
        (ids / host).write_text(image_id + "\n")

    yield set_id
    for flow_class in (TInContainer, TInPodman):
        for name in (flow_class.name, flow_class.__name__):
            registered_flows.pop(name, None)


def test_a_container_image_is_checked_on_the_daemon_the_run_used(
    tmp_path, container_daemons, monkeypatch
):
    """A change of the image on the selected daemon runs the node again. A change on another
    daemon does not."""
    container_daemons("tcp-a", "sha256:a1")
    container_daemons("tcp-b", "sha256:b1")
    monkeypatch.setenv("DOCKER_HOST", "tcp-a")
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    settings = {"inherit_env": True}
    first = runner.launch_flow(TInContainer, design, settings)
    recorded = trace.read_trace(first.run_path)[0].programs["docker-image:img:tag"]
    assert recorded.path == "sha256:a1"
    assert recorded.client.cli == _FAKE_CLI
    assert recorded.client.env["DOCKER_HOST"] == "tcp-a"
    assert runner.launch_flow(TInContainer, design, settings).reused
    container_daemons("tcp-b", "sha256:b2")
    assert runner.launch_flow(TInContainer, design, settings).reused
    container_daemons("tcp-a", "sha256:a2")
    again = runner.launch_flow(TInContainer, design, settings)
    assert not again.reused and again.stale_reason == "docker-image:img:tag changed"
    assert runner.launch_flow(TInContainer, design, settings).reused


def test_a_container_image_of_another_client_is_reused_and_checked(tmp_path, container_daemons):
    """The image of a `podman` run is read through `podman`, so a run that nothing changed is
    reused, and a changed image runs again."""
    container_daemons("local", "sha256:p1")
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    first = runner.launch_flow(TInPodman, design, {})
    recorded = trace.read_trace(first.run_path)[0].programs["docker-image:img:tag"]
    assert (recorded.path, recorded.client.cli) == ("sha256:p1", "fake-podman")
    assert runner.launch_flow(TInPodman, design, {}).reused
    container_daemons("local", "sha256:p2")
    assert not runner.launch_flow(TInPodman, design, {}).reused


def test_a_trace_of_an_image_without_a_client_runs_the_node_once(tmp_path, container_daemons):
    container_daemons("local", "sha256:l1")
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    first = runner.launch_flow(TInContainer, design, {})
    document = json.loads((first.run_path / "trace.json").read_text())
    del document["programs"]["docker-image:img:tag"]["client"]
    (first.run_path / "trace.json").write_text(json.dumps(document))
    again = runner.launch_flow(TInContainer, design, {})
    assert not again.reused
    assert again.stale_reason == "docker-image:img:tag was recorded without its client"
    assert runner.launch_flow(TInContainer, design, {}).reused


def test_a_container_image_is_checked_with_the_environment_of_the_node(
    tmp_path, container_daemons, monkeypatch
):
    """The client finds its configuration through `HOME`. The check uses the `HOME` of the node
    (its `env`), not the one of the process that checks."""
    for name in ("a", "b"):
        config = tmp_path / name / ".docker"
        config.mkdir(parents=True)
        (config / "config.json").write_text(f"sha256:{name}1\n")
    program = tmp_path / "bin" / "fake-home-cli"
    program.write_text('#!/bin/sh\n[ "$1" = image ] && cat "$HOME/.docker/config.json"\nexit 0\n')
    program.chmod(0o755)
    monkeypatch.setattr(TInContainer, "cli", "fake-home-cli")
    monkeypatch.setenv("HOME", str(tmp_path / "b"))
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    settings = {"env": {"HOME": str(tmp_path / "a")}}
    first = runner.launch_flow(TInContainer, design, settings)
    recorded = trace.read_trace(first.run_path)[0].programs["docker-image:img:tag"]
    assert recorded.path == "sha256:a1"
    assert runner.launch_flow(TInContainer, design, settings).reused
    (tmp_path / "a" / ".docker" / "config.json").write_text("sha256:a2\n")
    assert not runner.launch_flow(TInContainer, design, settings).reused
