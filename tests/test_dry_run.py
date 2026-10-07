"""Dry-run exposes the launcher's pure plan without running tools or side-effecting loaders."""

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner
from xeda.tool import Tool

from .test_cli_json_contract import json_stdout, run_xeda

PART = "LFE5U-25F-6BG381C"
OTHER_PART = "LFE5U-85F-6BG381C"
BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"


@pytest.fixture(autouse=True)
def worktree_imports(monkeypatch):
    # Subprocesses change cwd; a relative PYTHONPATH would select another editable checkout.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))


#: what the plan says of an input no explicit binding supplies, and of one nothing supplies
UNBOUND = {"binding_origin": None, "binding_location": None, "overridden": []}
NOTHING = {"sources": [], "references": [], **UNBOUND}


def _design_file(root: Path, yosys_part: str | None = None) -> Path:
    root.mkdir(exist_ok=True)
    (root / "blink.v").write_text(BLINK)
    text = (
        'name="blink"\n[rtl]\nsources=["blink.v"]\ntop="blink"\n'
        f'[flows.nextpnr]\nfpga.part="{PART}"\n'
    )
    if yosys_part:
        text += f'[flows.yosys_fpga]\nfpga.part="{yosys_part}"\n'
    path = root / "blink.toml"
    path.write_text(text)
    return path


def _forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run constructed a flow, launched it, or probed a tool")

    monkeypatch.setattr(Flow, "__init__", forbidden)
    monkeypatch.setattr(DefaultRunner, "launch_flow", forbidden)
    monkeypatch.setattr(Tool, "execute", forbidden)
    monkeypatch.setattr(Tool, "probe_stdout", forbidden)


def test_a_dry_run_prints_the_plan_and_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _design_file(tmp_path / "d")
    _forbid_execution(monkeypatch)
    run_root = tmp_path / "xeda_run"
    result = CliRunner().invoke(
        cli, ["run", "nextpnr", str(design), "--dry-run", "--run-root", str(run_root)]
    )
    assert result.exit_code == 0, (result.output, result.exception)
    text = click.unstyle(result.output)
    assert "Plan for nextpnr" in text
    assert text.index("  yosys_fpga  ") < text.index("  nextpnr  ")
    assert "netlist <- yosys_fpga.netlist" in text
    assert str(run_root / "blink" / "nextpnr") in text
    assert not run_root.exists()


@pytest.mark.parametrize("hashed", [False, True])
def test_json_plan_matches_the_public_api_and_materialized_design(tmp_path, monkeypatch, hashed):
    from xeda.introspect import plan_info

    monkeypatch.chdir(tmp_path)
    design = _design_file(tmp_path / "d")
    loaded = Design.from_file(design)
    run_root = tmp_path / "xeda_run"
    _forbid_execution(monkeypatch)
    runner = DefaultRunner(run_root, hashed_run_dirs=hashed)
    expected = plan_info(runner.plan("nextpnr", loaded))
    assert plan_info(runner.plan("nextpnr", design)) == expected
    args = ["--hashed-run-dirs"] if hashed else []
    proc = run_xeda(
        "run",
        "nextpnr",
        str(design),
        "--dry-run",
        "--json",
        "--run-root",
        str(run_root),
        *args,
        cwd=tmp_path,
    )
    document = json_stdout(proc)
    assert proc.returncode == 0, proc.stderr
    assert document == {
        "flow": "nextpnr",
        "design": str(design),
        "target": None,
        "success": True,
        "dry_run": True,
        "request": [{"node": "nextpnr", "flow": "nextpnr", "output": None}],
        "plan": expected,
    }
    nodes = document["plan"]["nodes"]
    assert [node["name"] for node in nodes] == ["yosys_fpga", "nextpnr"]
    assert nodes[1]["inputs"] == [
        {
            "name": "netlist",
            "origin": "producer",
            "producer": "yosys_fpga",
            "output": "netlist",
            "sources": [],
            "references": [{"node": "yosys_fpga", "output": "netlist"}],
            **UNBOUND,
        },
        {"name": "constraints", "origin": "none", "producer": None, "output": None, **NOTHING},
        {"name": "sdc", "origin": "none", "producer": None, "output": None, **NOTHING},
    ]
    for node in nodes:
        suffix = "_" + node["flowrun_hash"][:16] if hashed else ""
        assert node["run_path"] == str(run_root / "blink" / (node["name"] + suffix))
    assert not run_root.exists()


def test_a_source_supplies_the_input_and_displaces_the_producer(tmp_path):
    (tmp_path / "netlist.json").write_text("{}")
    design = tmp_path / "d.toml"
    design.write_text(
        'name="d"\n[rtl]\ntop="blink"\n'
        'sources=[{file="netlist.json", type="JsonNetlist"}]\n'
        f'[flows.nextpnr]\nfpga.part="{PART}"\n'
    )
    proc = run_xeda("run", "nextpnr", str(design), "--dry-run", "--json", cwd=tmp_path)
    document = json_stdout(proc)
    assert proc.returncode == 0, proc.stderr
    (node,) = document["plan"]["nodes"]
    assert node["name"] == "nextpnr"
    assert node["inputs"] == [
        {
            "name": "netlist",
            "origin": "source",
            "producer": None,
            "output": None,
            "sources": [str(tmp_path / "netlist.json")],
            "references": [],
            **UNBOUND,
        },
        {"name": "constraints", "origin": "none", "producer": None, "output": None, **NOTHING},
        {"name": "sdc", "origin": "none", "producer": None, "output": None, **NOTHING},
    ]
    assert not (tmp_path / "xeda_run").exists()


def test_an_impossible_plan_is_a_failure_document(tmp_path):
    design = _design_file(tmp_path / "d", yosys_part=OTHER_PART)
    proc = run_xeda("run", "nextpnr", str(design), "--dry-run", "--json", cwd=tmp_path)
    document = json_stdout(proc)
    assert proc.returncode == 1
    assert document["success"] is False
    assert document["error"]["type"] == "FlowSettingsError"
    assert OTHER_PART in document["error"]["message"]
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize("root_kind", ["empty_custom", "unmarked_default"])
@pytest.mark.parametrize("valid_plan", [True, False], ids=["success", "failure"])
def test_dry_run_leaves_existing_unmarked_roots_unchanged(tmp_path, root_kind, valid_plan):
    from .test_isolation import _state

    design = _design_file(tmp_path / "d", yosys_part=None if valid_plan else OTHER_PART)
    run_root = tmp_path / ("runs" if root_kind == "empty_custom" else "xeda_run")
    run_root.mkdir()
    if root_kind == "unmarked_default":
        (run_root / "old_run").mkdir()
        (run_root / "old_run" / "results.json").write_text('{"success": true}\n')
    before = _state(tmp_path, exclude=[])
    proc = run_xeda(
        "run",
        "nextpnr",
        str(design),
        "--dry-run",
        "--json",
        "--run-root",
        str(run_root),
        cwd=tmp_path,
    )
    document = json_stdout(proc)
    assert proc.returncode == (0 if valid_plan else 1), proc.stderr
    assert document["success"] is valid_plan
    if not valid_plan:
        assert document["error"]["type"] == "FlowSettingsError"
    assert _state(tmp_path, exclude=[]) == before


@pytest.mark.parametrize("json_flag", [False, True])
def test_a_dry_run_is_refused_for_a_remote(tmp_path, json_flag):
    design = _design_file(tmp_path / "d")
    proc = run_xeda(
        "run",
        "nextpnr",
        str(design),
        "--dry-run",
        "--remote",
        "host",
        *(["--json"] if json_flag else []),
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    message = "`--dry-run` is not supported with --remote"
    if json_flag:
        document = json_stdout(proc)
        assert document["success"] is False
        assert document["error"] == {"type": "UsageError", "message": message}
    else:
        assert message in click.unstyle(proc.stderr)
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize("kind", ["command", "arguments", "mapping", "project", "git", "local"])
def test_side_effecting_loading_is_refused_before_it_runs(tmp_path, kind):
    root = tmp_path / "d"
    root.mkdir()
    (root / "blink.v").write_text(BLINK)
    sentinel = root / "generated"
    command = "touch generated"
    rtl = {"sources": ["blink.v"], "top": "blink"}
    spec = {"name": "blink", "rtl": rtl, "flows": {"nextpnr": {"fpga": {"part": PART}}}}
    if kind in ("command", "project", "local"):
        rtl["generator"] = command
    elif kind == "arguments":
        rtl["generator"] = ["touch", "generated"]
    elif kind == "mapping":
        rtl["generator"] = {"executable": "touch", "args": ["generated"]}
    else:
        spec["dependencies"] = ["git+https://example.invalid/repo.git#d.toml"]
    design = root / "d.json"
    design.write_text(json.dumps(spec))
    args = [str(design)]
    if kind == "project":
        project = root / "xedaproject.json"
        project.write_text(json.dumps({"designs": [spec]}))
        args = ["--design-name", "blink", "--xedaproject", str(project)]
    elif kind == "local":
        outer = root / "outer.json"
        outer.write_text(
            json.dumps(
                {
                    "name": "outer",
                    "rtl": {"sources": [], "top": "blink"},
                    "dependencies": [str(design)],
                    "flows": spec["flows"],
                }
            )
        )
        args = [str(outer)]
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    proc = run_xeda(
        "run",
        "nextpnr",
        *args,
        "--dry-run",
        "--json",
        "--outputs-to",
        str(tmp_path / "out"),
        cwd=tmp_path,
    )
    document = json_stdout(proc)
    assert proc.returncode == 1, proc.stderr
    assert document["success"] is False
    expected = "Git dependency fetch" if kind == "git" else "generator"
    assert expected in document["error"]["message"]
    assert not sentinel.exists()
    assert not (tmp_path / "xeda_run").exists()
    assert not (tmp_path / "out").exists()
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_an_impossible_target_fails_without_probing_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _design_file(tmp_path / "d")
    _forbid_execution(monkeypatch)
    result = CliRunner().invoke(
        cli, ["run", "nextpnr", str(design), "--dry-run", "-s", "fpga.family=unknown"]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "unknown" in result.output
    assert not (tmp_path / "xeda_run").exists()


def test_an_escaping_run_directory_is_refused_without_writes(tmp_path):
    design = _design_file(tmp_path / "d")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "xeda_run"
    root.mkdir()
    (root / ".xeda-run-root").touch()
    (root / "blink").symlink_to(outside, target_is_directory=True)
    before = sorted(p.name for p in root.iterdir())
    proc = run_xeda(
        "run",
        "nextpnr",
        str(design),
        "--dry-run",
        "--json",
        "--run-root",
        str(root),
        cwd=tmp_path,
    )
    document = json_stdout(proc)
    assert proc.returncode == 1 and document["success"] is False
    assert "leads out" in document["error"]["message"]
    assert sorted(p.name for p in root.iterdir()) == before
    assert list(outside.iterdir()) == []


def test_a_flow_that_declares_no_io_plans_as_one_node(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _design_file(tmp_path / "d")
    _forbid_execution(monkeypatch)
    result = CliRunner().invoke(cli, ["run", "ghdl_sim", str(design), "--dry-run", "--json"])
    assert result.exit_code == 0, (result.output, result.exception)
    (node,) = json.loads(result.stdout)["plan"]["nodes"]
    assert node["flow"] == "ghdl_sim" and node["inputs"] == [] and node["switched_on"] == []
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize("json_flag", [False, True])
def test_switched_on_outputs_and_cli_shared_settings_are_reported(tmp_path, json_flag):
    design = _design_file(tmp_path / "d", yosys_part=OTHER_PART)
    proc = run_xeda(
        "run",
        "Nextpnr",
        str(design),
        "--dry-run",
        *(["--json"] if json_flag else []),
        "-s",
        f"flows.yosys_fpga.fpga.part={PART}",
        "flows.yosys_fpga.netlist_json=",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    if json_flag:
        document = json_stdout(proc)
        producer, consumer = document["plan"]["nodes"]
        assert producer["switched_on"] == ["netlist"]
        assert consumer["switched_on"] == []
    else:
        assert "output netlist switched on: a consumer reads it" in proc.stdout
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize("invalid_target", [False, True])
def test_a_subprocess_dry_run_never_invokes_tools_or_delivers(
    tmp_path, monkeypatch, invalid_target
):
    import os

    design = _design_file(tmp_path / "d")
    tools = tmp_path / "tools"
    tools.mkdir()
    sentinel = tmp_path / "tool-was-run"
    for name in ("yosys", "nextpnr-ecp5", "git"):
        executable = tools / name
        executable.write_text(f"#!/bin/sh\n/usr/bin/touch '{sentinel}'\nexit 1\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    proc = run_xeda(
        "run",
        "nextpnr",
        str(design),
        "--dry-run",
        "--json",
        "--clean",
        "--scrub",
        "--outputs-to",
        str(tmp_path / "out"),
        *(["-s", "fpga.family=unknown"] if invalid_target else []),
        cwd=tmp_path,
    )
    document = json_stdout(proc)
    assert proc.returncode == int(invalid_target), proc.stderr
    assert document["success"] is (not invalid_target)
    if invalid_target:
        assert "unknown" in document["error"]["message"]
    assert not sentinel.exists()
    assert not (tmp_path / "xeda_run").exists()
    assert not (tmp_path / "out").exists()
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
