"""`Workspace.plan` plans a `Spec` and returns a `Plan`: what a launch would do, and nothing it did."""

import json
import os
import pickle
import subprocess
from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.errors import RunRootError, SpecError
from xeda.local_settings import LocalSettings
from xeda.plan import Plan, PlanDelivery, PlanInput, PlanNode
from xeda.project import Project
from xeda.spec import Spec
from xeda.workspace import Workspace

from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow
from .test_dry_run_freshness import RejectedProducerGraph, toys  # noqa: F401  (the toy flows)
from .tool_utils import require_git

PART = "LFE5U-25F-6BG381C"
BLINK = "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
SEVEN_OPTIONS = {
    "rebuild_all",
    "clean",
    "prune_variants",
    "outputs_to",
    "overwrite_outputs",
    "post_cleanup",
    "post_cleanup_purge",
}


def _design(root: Path) -> Design:
    root.mkdir(exist_ok=True)
    (root / "blink.v").write_text(BLINK)
    return Design(name="blink", root=root, rtl={"sources": ["blink.v"], "top": "blink"})


def _spec(root: Path, flow: str = "nextpnr") -> Spec:
    return Spec(flow, _design(root), settings={"nextpnr": {"fpga": {"part": PART}}})


def _tree(root: Path) -> set[Path]:
    return set(root.rglob("*"))


def test_plan_writes_nothing(tmp_path):
    spec = _spec(tmp_path / "design")
    before = _tree(tmp_path)
    plan = Workspace(tmp_path / "runs").plan(spec)
    assert _tree(tmp_path) == before
    assert list(plan.nodes) == ["yosys_fpga", "nextpnr"]
    assert plan.roots == ("nextpnr",)


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_plan_writes_only_dependency_storage_for_a_git_dependency(tmp_path, monkeypatch):
    require_git()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=work)
    (work / "a.v").write_text("module a; endmodule\n")
    (work / "dep.yaml").write_text(
        yaml.safe_dump({"name": "dep", "rtl": {"sources": ["a.v"], "top": "a"}})
    )
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "first", cwd=work)
    commit = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--quiet", "--bare", str(work), str(bare), cwd=tmp_path)
    design = tmp_path / "design"
    design.mkdir()
    (design / "top.v").write_text("module top; endmodule\n")
    (design / "d.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "clocks": ["clk"]},
                "dependencies": [
                    {"uri": f"git+{bare.as_uri()}?commit={commit}#dep.yaml", "rtl": {"pos": -1}}
                ],
            }
        )
    )
    root = tmp_path / "runs"
    root.mkdir()
    (root / ".xeda-run-root").write_text("{}")  # the marker is no part of the dependency storage
    before = _tree(tmp_path)
    spec = Spec(
        "yosys_fpga",
        Design.load(design / "d.yaml"),
        settings={"yosys_fpga": {"fpga": {"part": "xc7a12tcsg325-1"}}},
    )
    Workspace(root).plan(spec)
    added = _tree(tmp_path) - before
    assert added
    for path in added:
        assert path.parts[len(root.parts)] in (".dependencies", ".locks"), path


def test_plan_to_dict_is_the_dry_run_document(tmp_path):
    root = tmp_path / "design"
    spec = _spec(root)
    (root / "blink.yaml").write_text(
        yaml.safe_dump({"name": "blink", "rtl": {"sources": ["blink.v"], "top": "blink"}})
    )
    spec = Spec(
        "nextpnr", Design.load(root / "blink.yaml"), settings={"nextpnr": {"fpga": {"part": PART}}}
    )
    plan = Workspace(tmp_path / "xeda_run").plan(spec)
    document = plan.to_dict()
    assert set(document) == {"schema_version", "spec", "roots", "options", "nodes"}
    assert document["schema_version"] == 1
    assert document["spec"]["design_file"] == str(root / "blink.yaml")
    node = document["nodes"][1]
    assert set(node) == {
        "name",
        "flow",
        "run_dir",
        "identity",
        "state",
        "reason",
        "action",
        "settings",
        "inputs",
        "outputs_enabled",
        "input_types",
        "output_types",
        "deliveries",
    }
    assert set(node["inputs"][0]) == {"name", "origin", "producer", "output", "sources", "reason"}
    assert json.loads(json.dumps(document)) == document
    proc = subprocess.run(
        [
            "xeda",
            "run",
            "nextpnr",
            str(root / "blink.yaml"),
            "-s",
            f"nextpnr.fpga.part={PART}",
            "--dry-run",
            "--json",
            "--run-root",
            str(tmp_path / "xeda_run"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout) == document


def test_a_plan_is_immutable_and_can_be_pickled(tmp_path):
    plan = Workspace(tmp_path / "runs").plan(_spec(tmp_path / "design"))
    assert isinstance(plan, Plan) and isinstance(plan.nodes["nextpnr"], PlanNode)
    with pytest.raises(TypeError):
        plan.nodes["x"] = plan.nodes["nextpnr"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        plan.nodes["nextpnr"].state = "fresh"  # type: ignore[misc]
    again = pickle.loads(pickle.dumps(plan))
    assert again == plan and again.to_dict() == plan.to_dict()


def test_a_node_says_where_it_reads_from_and_what_it_enables(tmp_path):
    plan = Workspace(tmp_path / "runs").plan(_spec(tmp_path / "design"))
    producer, consumer = plan.nodes.values()
    assert producer.outputs_enabled == () and producer.inputs == ()
    assert producer.run_dir == tmp_path / "runs" / "blink" / "yosys_fpga"
    netlist = next(item for item in consumer.inputs if item.name == "netlist")
    assert (netlist.origin, netlist.producer, netlist.output) == (
        "producer",
        "yosys_fpga",
        "netlist",
    )
    assert isinstance(netlist, PlanInput) and netlist.reason
    assert consumer.action is False and consumer.settings["fpga"]["part"] == PART
    assert (producer.state, producer.reason) == ("stale", "no completed previous run")


@pytest.mark.usefixtures("toys")
def test_a_reused_rejection_is_rejected_and_its_consumer_blocked(tmp_path, monkeypatch):
    with preserved_flow_registry():
        graph = RejectedProducerGraph(tmp_path, monkeypatch)
        graph.settle()
        spec = Spec(
            graph.flow,
            graph.design(),
            settings={"toy_producer": {"reject": True}},
        )
        plan = Workspace(graph.run_root).plan(spec)
    producer, consumer = plan.nodes["toy_producer"], plan.nodes["toy_consumer"]
    assert producer.state == "rejected" and producer.reason == graph.message()
    assert consumer.state == "blocked" and "toy_producer" in consumer.reason


def test_rebuild_all_makes_every_node_stale_with_its_reason(tmp_path):
    plan = Workspace(tmp_path / "runs").plan(_spec(tmp_path / "design"), rebuild_all=True)
    assert {(n.state, n.reason) for n in plan.nodes.values()} == {("stale", "rebuild_all")}
    plan = Workspace(tmp_path / "runs").plan(_spec(tmp_path / "design"), clean=True)
    assert {(n.state, n.reason) for n in plan.nodes.values()} == {("stale", "clean")}


def test_plan_options_has_the_seven_launch_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace(tmp_path / "runs")
    spec = _spec(tmp_path / "design")
    plan = workspace.plan(spec)
    assert set(plan.options) == SEVEN_OPTIONS and not any(plan.options.values())
    plan = workspace.plan(spec, outputs_to="out", prune_variants=True, post_cleanup=True)
    assert plan.options["outputs_to"] == tmp_path / "out"
    assert plan.options["prune_variants"] and plan.options["post_cleanup"]
    assert plan.to_dict()["options"]["outputs_to"] == str(tmp_path / "out")


def test_a_delivery_that_no_answer_allows_raises_delivery_refused(tmp_path):
    root = tmp_path / "runs"
    with pytest.raises(SpecError) as raised:
        Workspace(root).plan(_spec(tmp_path / "design"), outputs_to=root / "copies")
    assert raised.value.code == "delivery_refused"
    assert not root.exists()


def test_a_delivery_is_planned_and_says_whether_it_replaces_a_file(tmp_path):
    destination = tmp_path / "out" / "blink.bit"
    spec = Spec(
        "fpga_pack",
        _design(tmp_path / "design"),
        settings={
            "nextpnr": {"fpga": {"part": PART}},
            "fpga_pack": {"bitstream": str(destination)},
        },
    )
    workspace = Workspace(tmp_path / "runs")
    planned = workspace.plan(spec).nodes["fpga_pack"].deliveries
    assert planned == (PlanDelivery("bitstream", destination, False),)
    destination.parent.mkdir()
    destination.write_text("yours\n")
    (replaced,) = workspace.plan(spec).nodes["fpga_pack"].deliveries
    assert replaced.destination == destination and replaced.replaces is True


def test_a_project_design_in_the_default_run_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blink.v").write_text(BLINK)
    (tmp_path / "xedaproject.yaml").write_text(
        yaml.safe_dump(
            {
                "run_root": "build/runs",
                "designs": [{"name": "d", "rtl": {"sources": ["blink.v"], "top": "blink"}}],
            }
        )
    )
    project = Project.load(tmp_path / "xedaproject.yaml")
    spec = Spec("nextpnr", project.design("d"), settings={"nextpnr": {"fpga": {"part": PART}}})
    with pytest.raises(SpecError) as raised:
        Workspace().plan(spec)
    assert raised.value.code == "run_root_mismatch"
    assert "Workspace(project.run_root)" in str(raised.value)
    # a given run root, one from the private files and the project's own are all used as given
    assert Workspace(project.run_root).plan(spec).nodes
    assert Workspace(local_settings=LocalSettings(run_root=tmp_path / "other")).plan(spec).nodes
    assert Workspace(tmp_path / "xeda_run").plan(spec).nodes


def test_the_default_run_root_is_the_start_directory_s_and_is_resolved_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace()
    assert workspace.run_root == (tmp_path / "xeda_run").resolve()
    monkeypatch.chdir(tmp_path / "..")
    assert workspace.plan(_spec(tmp_path / "design")).nodes["nextpnr"].run_dir == (
        (tmp_path / "xeda_run").resolve() / "blink" / "nextpnr"
    )
    assert not (tmp_path / "xeda_run").exists()


def test_the_run_root_comes_from_local_settings_when_not_given(tmp_path):
    local = LocalSettings(run_root=tmp_path / "private")
    assert Workspace(local_settings=local).run_root == (tmp_path / "private").resolve()
    assert Workspace(tmp_path / "given", local_settings=local).run_root == tmp_path / "given"


def test_a_run_root_that_xeda_cannot_take_is_refused_when_the_workspace_is_made(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(RunRootError) as raised:
        Workspace(tmp_path / "file")
    assert raised.value.code == "invalid_run_root"
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "notes.txt").write_text("x")
    with pytest.raises(RunRootError):
        Workspace(tmp_path / "mine")


def test_a_workspace_compares_by_its_options(tmp_path):
    def answer(_text: str) -> bool:
        return True

    first = Workspace(tmp_path / "a", confirm=answer)._options()
    assert first == Workspace(tmp_path / "a", confirm=answer)._options()
    assert first != Workspace(tmp_path / "a", confirm=lambda _t: True)._options()
    assert first != Workspace(tmp_path / "b", confirm=answer)._options()
    assert first != Workspace(tmp_path / "a", confirm=answer, hashed_run_dirs=True)._options()
    plan = Workspace(tmp_path / "a", confirm=answer).plan(_spec(tmp_path / "design"))
    assert plan._workspace == first and "workspace" not in plan.to_dict()


def test_a_generator_design_plans_unknown_until_generated(tmp_path):
    with preserved_flow_registry():
        flow = reader_flow()
        world = World(tmp_path)
        workspace = Workspace(world.run_root)
        plan = workspace.plan(Spec(flow.name, world.load()))
        node = plan.nodes[flow.name]
        assert (node.state, node.reason) == ("unknown", "the generator has not run")
        assert (
            node.identity
            and node.identity
            == workspace.plan(Spec(flow.name, world.load())).nodes[flow.name].identity
        )
        assert world.runs == 0
