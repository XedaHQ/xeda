"""A tool starts in an explicit working directory, whatever the working directory of Xeda is.

A tool that belongs to a flow runs in the run path of the flow. A probe runs in a scratch
directory. Any other tool runs in the current directory, read once when the process starts.
Xeda writes the files of the tool (`env.sh`, the Docker environment file) there too. The tests
below run with the working directory of the test process, which is never the run path.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, List, Tuple

import pytest

import xeda.tool
from xeda import Design
from xeda.flows import Yosys
from xeda.run_dir import RunDirectoryError
from xeda.tool import Docker, Tool

Call = Tuple[str, List[str], dict]


@pytest.fixture
def calls(monkeypatch) -> List[Call]:
    """Record each `run_process` of `xeda.tool`; a capture (`stdout=True`) returns no text."""
    recorded: List[Call] = []

    def fake(executable, args=None, **kwargs):
        recorded.append((str(executable), [str(a) for a in args or []], kwargs))
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr(xeda.tool, "run_process", fake)
    return recorded


def _flow(tmp_path: Path, name: str = "run", *, linked: bool = False) -> Yosys:
    """A flow built directly, as a test does, with its run path under `tmp_path`."""
    design_root = tmp_path / "design"
    design_root.mkdir(exist_ok=True)
    run_path = tmp_path / name
    if linked:
        real = tmp_path / f"{name}-real"
        real.mkdir()
        run_path.symlink_to(real, target_is_directory=True)
    else:
        run_path.mkdir()
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=design_root)
    return Yosys({}, design, run_path)


def _tool(flow: Yosys | None = None, **kwargs: Any) -> Tool:
    kwargs.setdefault("version_flag", None)
    return Tool(executable="some-tool", flow=flow, **kwargs)


def test_a_tool_of_a_flow_starts_in_the_run_path_of_the_flow(tmp_path, calls):
    flow = _flow(tmp_path)
    _tool(flow).run("arg")
    ((_, _, kwargs),) = calls
    assert kwargs["cwd"] == flow.run_path
    assert Path.cwd() != flow.run_path


def test_a_tool_of_a_flow_starts_in_the_run_path_as_given_not_in_its_resolved_target(
    tmp_path, calls
):
    """The run path may be a link into the run root. The paths that the tool sees stay as they
    are."""
    flow = _flow(tmp_path, linked=True)
    _tool(flow).run("arg")
    ((_, _, kwargs),) = calls
    assert kwargs["cwd"] == flow.run_path
    assert kwargs["cwd"] != flow.run_path.resolve()


def test_a_derived_tool_starts_in_the_run_path_too(tmp_path, calls):
    flow = _flow(tmp_path)
    _tool(flow).derive("other").run("arg")
    ((_, _, kwargs),) = calls
    assert kwargs["cwd"] == flow.run_path


def test_a_tool_made_in_a_flow_method_called_by_another_flow_belongs_to_the_inner_flow(
    tmp_path, calls, monkeypatch
):
    """The flow whose method makes the tool owns it, not a flow further out on the stack."""
    outer = _flow(tmp_path, "outer")
    inner = _flow(tmp_path, "inner")

    def make_the_tool(self):
        return _tool()

    def call_the_other(self, other):
        return other.make_the_tool()

    monkeypatch.setattr(Yosys, "make_the_tool", make_the_tool, raising=False)
    monkeypatch.setattr(Yosys, "call_the_other", call_the_other, raising=False)
    tool = outer.call_the_other(inner)  # type: ignore[attr-defined]
    tool.run("arg")
    ((_, _, kwargs),) = calls
    assert kwargs["cwd"] == inner.run_path
    assert kwargs["cwd"] != outer.run_path


def test_a_tool_outside_a_flow_starts_in_the_current_directory_as_an_absolute_path(calls):
    _tool().run("arg")
    ((_, _, kwargs),) = calls
    assert kwargs["cwd"] == Path(os.getcwd())
    assert Path(kwargs["cwd"]).is_absolute()


def test_a_relative_executable_is_found_in_the_working_directory_of_the_tool(tmp_path):
    """A tool named by a relative path (`./simv`, which the flow builds) is the file in its run
    directory, whatever the working directory of the process."""
    flow = _flow(tmp_path)
    program = flow.run_path / "simv"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    assert Tool(executable="./simv", flow=flow, version_flag=None).executable_path() == (
        program.resolve()
    )
    assert Tool(executable="./missing", flow=flow, version_flag=None).executable_path() is None


def test_the_environment_file_goes_to_the_run_path(tmp_path, calls):
    flow = _flow(tmp_path)
    _tool(flow).run("arg", env={"ONE": "1", "SKIPPED": None})
    assert (flow.run_path / "env.sh").read_text() == 'export ONE="1"'
    assert not (Path.cwd() / "env.sh").exists()


def test_a_relative_log_file_goes_to_the_run_path(tmp_path, calls):
    flow = _flow(tmp_path)
    tool = _tool(flow)
    tool.run("arg", stdout=Path("tool.out"))
    tool.run("arg", tee=Path("tool.log"))
    tool.redirect_stdout = Path("redirected.out")
    tool.run("arg")
    assert [kwargs.get("stdout") for _, _, kwargs in calls] == [
        flow.run_path / "tool.out",
        None,
        flow.run_path / "redirected.out",
    ]
    assert calls[1][2]["tee"] == flow.run_path / "tool.log"


def test_an_absolute_log_file_stays(tmp_path, calls):
    flow = _flow(tmp_path)
    elsewhere = tmp_path / "elsewhere.log"
    _tool(flow).run("arg", stdout=elsewhere)
    ((_, _, kwargs),) = calls
    assert kwargs["stdout"] == elsewhere


def test_a_query_runs_in_a_scratch_directory_that_is_removed(tmp_path, calls, monkeypatch):
    flow = _flow(tmp_path)
    seen: List[Path] = []
    original = xeda.tool.run_process

    def look(executable, args=None, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        assert seen[-1].is_dir()
        return original(executable, args, **kwargs)

    monkeypatch.setattr(xeda.tool, "run_process", look)
    _tool(flow).probe_stdout("--version")
    (scratch,) = seen
    assert scratch.name.startswith("xeda-probe-")
    assert scratch not in (flow.run_path, Path.cwd())
    assert not scratch.exists()
    assert list(flow.run_path.iterdir()) == []


def test_a_query_that_makes_a_file_leaves_it_in_the_scratch_directory(tmp_path, monkeypatch):
    flow = _flow(tmp_path)
    written: List[Tuple[Path, str]] = []

    def look(executable, args=None, **kwargs):
        # The file exists when the process starts, in the directory the process gets.
        scratch = Path(kwargs["cwd"])
        written.append((scratch, (scratch / "env.sh").read_text()))
        return ""

    monkeypatch.setattr(xeda.tool, "run_process", look)
    _tool(flow).probe_stdout("--version", env={"ONE": "1"})
    ((scratch, text),) = written
    assert text == 'export ONE="1"'
    assert scratch.name.startswith("xeda-probe-")
    assert scratch not in (flow.run_path, Path.cwd())
    assert list(flow.run_path.iterdir()) == []
    assert not (Path.cwd() / "env.sh").exists()


def test_the_environment_file_of_a_start_outside_the_run_directory_is_not_checked(tmp_path, calls):
    flow = _flow(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _tool(flow).run("arg", env={"ONE": "1"}, cwd=elsewhere)
    assert (elsewhere / "env.sh").read_text() == 'export ONE="1"'
    assert list(flow.run_path.iterdir()) == []


def test_the_environment_file_of_a_start_below_the_run_directory_goes_there(tmp_path, calls):
    flow = _flow(tmp_path)
    (flow.run_path / "sub").mkdir()
    _tool(flow).run("arg", env={"ONE": "1"}, cwd=Path("sub"))
    assert (flow.run_path / "sub" / "env.sh").read_text() == 'export ONE="1"'
    assert calls[0][2]["cwd"] == flow.run_path / "sub"


def test_a_start_in_a_link_that_leads_out_of_the_run_directory_is_refused(tmp_path, calls):
    """A tool may leave a link in its run directory. Xeda writes nothing through it."""
    flow = _flow(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (flow.run_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunDirectoryError, match="symbolic link"):
        _tool(flow).run("arg", env={"ONE": "1"}, cwd=Path("link"))
    assert list(outside.iterdir()) == []
    assert calls == []


# ----------------------------------------------------------------------------- Docker


def _docker_tool(flow: Yosys | None, **docker: Any) -> Tool:
    tool = _tool(flow, docker=Docker(image="img", **docker))
    tool.dockerized = True
    return tool


def _docker_command(calls: List[Call]) -> List[str]:
    ((cli, command, _),) = calls
    assert cli == "docker"
    return command


def test_a_container_works_in_the_run_path_and_mounts_it(tmp_path, calls):
    flow = _flow(tmp_path)
    _docker_tool(flow).run("arg", env={"ONE": "1"})
    command = _docker_command(calls)
    run_path = flow.run_path
    assert f"--workdir={run_path}" in command
    assert f"--volume={run_path}:{run_path}" in command
    env_file = run_path / ".some-tool_docker.env"
    assert command[command.index("--env-file") + 1] == str(env_file)
    assert "ONE=1" in env_file.read_text()
    assert (run_path / "env.sh").is_file()
    assert calls[0][2]["cwd"] == run_path
    here = str(Path.cwd())
    assert not [a for a in command if here in a], command


def test_the_cpu_information_file_goes_to_the_run_path(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(Docker, "cpuinfo", property(lambda self: [["Features : fp"]]))
    flow = _flow(tmp_path)
    _docker_tool(flow, fix_cpuinfo=True).run("arg")
    command = _docker_command(calls)
    cpuinfo = flow.run_path / ".cpuinfo"
    assert "Features : fp sse sse2" in cpuinfo.read_text()
    assert f"--volume={cpuinfo}:/proc/cpuinfo" in command
    assert not (Path.cwd() / ".cpuinfo").exists()


def test_a_container_started_with_a_directory_works_there(tmp_path, calls):
    here = tmp_path / "here"
    here.mkdir()
    Docker(image="img").run("tool", cwd=here)
    command = _docker_command(calls)
    assert f"--workdir={here}" in command
    assert f"--volume={here}:{here}" in command
    assert calls[0][2]["cwd"] == here


def test_the_cpu_information_is_asked_in_a_scratch_directory(tmp_path, calls):
    """The query of a machine property does not mount the directory of the user."""
    assert Docker(image="img").cpuinfo is not None
    ((_, command, kwargs),) = calls
    workdir = next(a for a in command if a.startswith("--workdir="))
    assert Path(workdir.removeprefix("--workdir=")).name.startswith("xeda-probe-")
    assert Path(kwargs["cwd"]).name.startswith("xeda-probe-")


def test_a_container_works_in_the_resolved_directory_of_a_linked_run_path(tmp_path, calls):
    """A container sees the physical directory: the work directory, the writable mount and the
    read-only mounts all use the resolved path, not the link into the run root."""
    flow = _flow(tmp_path, linked=True)
    real = flow.run_path.resolve()
    inside = real / "sub"
    inside.mkdir()
    flow.implicit_inputs.append(inside / "x")
    _docker_tool(flow).run("arg", env={"ONE": "1"}, stdout=Path("out.txt"))
    command = _docker_command(calls)
    link = str(flow.run_path)
    assert f"--workdir={real}" in command
    assert f"--volume={real}:{real}" in command
    assert not [a for a in command if f"={link}:" in a or a.endswith(f":{link}")], command
    assert f"--volume={inside}:{inside}:ro" not in command
    assert command[command.index("--env-file") + 1] == str(real / ".some-tool_docker.env")
    assert calls[0][2]["cwd"] == real
    assert Path(calls[0][2]["stdout"]).resolve() == real / "out.txt"


def test_the_read_only_mounts_leave_out_the_directory_of_the_tool(tmp_path, calls):
    flow = _flow(tmp_path)
    tool = _docker_tool(flow)
    assert flow.run_path not in tool._read_only_directories(flow.run_path)
    # a directory inside the run path of the flow needs no mount of its own
    inside = flow.run_path / "sub"
    inside.mkdir()
    flow.implicit_inputs.append(inside / "x")
    assert inside not in tool._read_only_directories(flow.run_path)


# ----------------------------------------------------------------------------- a real process

CHILD = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path

    changes = []
    sys.addaudithook(lambda event, args: changes.append(event) if event == "os.chdir" else None)
    from xeda import Design
    from xeda.flows import Yosys
    from xeda.tool import Tool

    root = Path(sys.argv[1])
    (root / "run").mkdir(parents=True)
    (root / "design").mkdir()
    design = Design(name="d", rtl={"sources": [], "top": "d"}, design_root=root / "design")
    flow = Yosys({}, design, root / "run")
    tool = Tool(executable=sys.executable, flow=flow, version_flag=None, print_command=False)
    show = "import os; print(os.getcwd())"
    reached = {"start": os.getcwd()}
    reached["run"] = tool.run("-c", show, stdout=True, env={"ONE": "1"}).strip()
    reached["run_files"] = sorted(p.name for p in (root / "run").iterdir())
    reached["probe"] = tool.probe_stdout("-c", show).strip()
    reached["probe_exists"] = os.path.exists(reached["probe"])
    reached["run_files_after_probe"] = sorted(p.name for p in (root / "run").iterdir())
    reached["changes"] = changes
    Path(sys.argv[2]).write_text(json.dumps(reached))
    """)


def test_real_processes_start_where_they_are_told_and_nothing_changes_directory(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = tmp_path / "result.json"
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path / "world"), str(result)],
        cwd=elsewhere,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    reached = json.loads(result.read_text())
    run_path = (tmp_path / "world" / "run").resolve()
    assert reached["changes"] == []
    assert Path(reached["start"]) == elsewhere.resolve()
    assert Path(reached["run"]) == run_path
    assert reached["run_files"] == ["env.sh"]
    assert Path(reached["probe"]).name.startswith("xeda-probe-")
    assert not reached["probe_exists"]
    assert reached["run_files_after_probe"] == ["env.sh"]
    assert list(elsewhere.iterdir()) == []
