"""`PlanNode.executable_check` says whether the main program of a node is on the `PATH` that the
node would get. It informs only: it starts nothing, refuses nothing and is part of no identity or
stale-plan comparison."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from xeda import Design
from xeda._internal.flow_runner.plan_check import plan_difference
from xeda._internal.local_settings import LocalSettings
from xeda._internal.plan import plan_from_dict
from xeda._internal.spec import Spec
from xeda._internal.workspace import Workspace

pytestmark = pytest.mark.python_compat

BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"


def _design(root: Path) -> Design:
    root.mkdir(exist_ok=True)
    (root / "blink.v").write_text(BLINK)
    return Design(name="blink", root=root, rtl={"sources": ["blink.v"], "top": "blink"})


def _spec(tmp_path: Path, flow: str, settings: dict) -> Spec:
    return Spec(flow, _design(tmp_path / "design"), settings=settings)


def _fake(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        program = directory / name
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    return directory


@pytest.fixture
def bare_path(tmp_path, monkeypatch):
    """A caller whose PATH holds no tool, and an empty tool store."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("XEDA_TOOLS_ROOT", str(tmp_path / "store"))
    (tmp_path / "store").mkdir()


def _plan(tmp_path, spec, *path: Path):
    local = LocalSettings(path=list(path)) if path else None
    return Workspace(tmp_path / "runs", local_settings=local).plan(spec)


ECP5 = {"yosys_fpga": {"fpga": {"part": "LFE5U-25F-6BG381C"}}}


def test_plan_checks_node_path(tmp_path, bare_path):
    spec = _spec(tmp_path, "yosys_fpga", ECP5)
    tools = _fake(tmp_path / "tools", "yosys")
    found = _plan(tmp_path, spec, tools).nodes["yosys_fpga"].executable_check
    assert found == {
        "executable": "yosys",
        "state": "found",
        "path": str((tools / "yosys").resolve()),
        "reason": None,
    }
    missing = _plan(tmp_path, spec).nodes["yosys_fpga"].executable_check
    assert missing == {"executable": "yosys", "state": "missing", "path": None, "reason": None}
    with pytest.raises(TypeError):
        found["state"] = "missing"  # type: ignore[index]  # the mapping is frozen
    # The check changes neither the state nor the identity of the node.
    assert _plan(tmp_path, spec, tools).nodes["yosys_fpga"].identity == (
        _plan(tmp_path, spec).nodes["yosys_fpga"].identity
    )


def test_a_flow_env_path_decides(tmp_path, bare_path):
    tools = _fake(tmp_path / "tools", "yosys")
    spec = _spec(
        tmp_path, "yosys_fpga", {"yosys_fpga": {**ECP5["yosys_fpga"], "env": {"PATH": ""}}}
    )
    check = _plan(tmp_path, spec, tools).nodes["yosys_fpga"].executable_check
    assert check is not None and check["state"] == "missing"


def test_plan_selects_nextpnr_family(tmp_path, bare_path):
    tools = _fake(tmp_path / "tools", "nextpnr-ecp5", "nextpnr-ice40")
    spec = _spec(tmp_path, "nextpnr", {"nextpnr": {"fpga": {"part": "LFE5U-25F-6BG381C"}}})
    plan = _plan(tmp_path, spec, tools)
    check = plan.nodes["nextpnr"].executable_check
    assert check is not None
    assert (check["executable"], check["state"]) == ("nextpnr-ecp5", "found")
    # Only the main program: the packer of the chain is not looked up for the node of nextpnr.
    other = _spec(tmp_path, "nextpnr", {"nextpnr": {"fpga": {"part": "iCE40HX8K-CT256"}}})
    check = _plan(tmp_path, other, tools).nodes["nextpnr"].executable_check
    assert check is not None and check["executable"] == "nextpnr-ice40"


def test_docker_check_is_unknown(tmp_path, bare_path, monkeypatch):
    import xeda._internal.proc_utils as proc

    def refuse(*args, **kwargs):
        raise AssertionError("a plan starts no process")

    monkeypatch.setattr(proc, "run_process", refuse)
    tools = _fake(tmp_path / "tools", "yosys")
    spec = _spec(tmp_path, "yosys_fpga", {"yosys_fpga": {**ECP5["yosys_fpga"], "docker": True}})
    check = _plan(tmp_path, spec, tools).nodes["yosys_fpga"].executable_check
    assert check == {
        "executable": "yosys",
        "state": "unknown",
        "path": None,
        "reason": "container not inspected",
    }


def test_plan_check_roundtrip_and_stale_comparison(tmp_path, bare_path):
    spec = _spec(tmp_path, "yosys_fpga", ECP5)
    tools = _fake(tmp_path / "tools", "yosys")
    workspace = Workspace(tmp_path / "runs", local_settings=LocalSettings(path=[tools]))
    plan = workspace.plan(spec)
    document = plan.to_dict()
    assert document["nodes"][0]["executable_check"]["state"] == "found"
    again = plan_from_dict(document, spec=spec, workspace=plan._workspace)
    assert again.nodes == plan.nodes
    # A document that lacks the property still reads.
    for node in document["nodes"]:
        del node["executable_check"]
    older = plan_from_dict(document, spec=spec, workspace=plan._workspace)
    assert older.nodes["yosys_fpga"].executable_check is None
    # A plan made again with another PATH differs in the check only: it is not stale for that.
    remade = _plan(tmp_path, spec)
    assert remade.nodes["yosys_fpga"].executable_check["state"] == "missing"  # type: ignore[index]
    assert remade != plan
    assert plan_difference(plan, remade) is None
    assert os.environ["PATH"]


def test_dry_run_prints_one_tool_line(tmp_path, bare_path, capsys):
    from xeda._internal.cli import _print_plan

    tools = _fake(tmp_path / "tools", "yosys")
    spec = _spec(tmp_path, "yosys_fpga", ECP5)
    found = str((tools / "yosys").resolve())
    lines = []
    for path, settings in (
        (tools, ECP5),
        (None, ECP5),
        (tools, {"yosys_fpga": {**ECP5["yosys_fpga"], "docker": True}}),
    ):
        spec = _spec(tmp_path, "yosys_fpga", settings)
        _print_plan(_plan(tmp_path, spec, *([path] if path else [])).to_dict())
        lines += [x for x in capsys.readouterr().out.splitlines() if "tool:" in x]
    assert lines == [
        f"      tool: yosys -> {found}",
        "      tool: yosys missing",
        "      tool: yosys unknown (container not inspected)",
    ]
