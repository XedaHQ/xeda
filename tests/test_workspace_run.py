"""`Workspace.run` runs a `Spec` or a `Plan`, and a plan is checked against a new planning."""

import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from xeda import Design
from xeda.dataclass import Field
from xeda.deliver import Deliveries, DeliveryError
from xeda.design import SourceType
from xeda.errors import SpecError, StalePlanError
from xeda.flow import Flow, MetricSpec, Out
from xeda.flow_runner import DefaultRunner, run_lock
from xeda.flow_runner.plan_check import plan_difference
from xeda.local_settings import LocalSettings
from xeda.outcomes import Result
from xeda.proc_utils import TOOL_OUTPUT
from xeda.run_root import ensure_run_root
from xeda.spec import Spec
from xeda.workspace import Workspace

from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow
from .test_dry_run_freshness import ToyGraph, toys  # noqa: F401  (the toy flows)
from .test_git_dependencies import _git, git_log, remote, toy  # noqa: F401  (a Git remote)
from .tool_utils import HANG_GUARD

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _spec(graph: ToyGraph, **consumer) -> Spec:
    return Spec("toy_consumer", graph.design(), settings={"toy_consumer": consumer})


@pytest.fixture
def graph(tmp_path, monkeypatch, toys):  # noqa: F811
    return ToyGraph(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "other, named",
    [
        ({"run_root": "other"}, "run root"),
        ({"hashed_run_dirs": True}, "hashed_run_dirs"),
        ({"tool_output": "terminal"}, "tool_output"),
        ({"confirm": lambda question: True}, "confirm"),
        # `Remote` does not exist yet: any value stands for it. The plan is refused before the
        # workspace would use the host, so nothing connects.
        ({"remote": object()}, "remote"),
        ({"local_settings": LocalSettings(env={"A": "b"})}, "local_settings"),
    ],
)
def test_a_plan_run_in_a_workspace_with_another_option_is_stale(graph, tmp_path, other, named):
    spec = _spec(graph)
    plan = Workspace(graph.run_root).plan(spec)
    options = {"run_root": graph.run_root, **other}
    if "run_root" in other:
        options["run_root"] = tmp_path / other["run_root"]
    with pytest.raises(StalePlanError) as raised:
        Workspace(**options).run(plan)
    assert raised.value.code == "stale_plan" and named in raised.value.difference
    assert not (graph.run_root / "d").exists(), "nothing ran"


def test_a_changed_flow_registration_changes_the_graph(graph):
    spec = _spec(graph)
    workspace = Workspace(graph.run_root)
    plan = workspace.plan(spec)

    class ToyConsumer(Flow):
        """The consumer again, without the producer."""

        results_description: ClassVar[dict] = {}

        class Settings(Flow.Settings):
            shout: bool = Field(False, description="Unused.")
            always: bool = Field(False, description="Unused.")
            reject: bool = Field(False, description="Unused.")

        def run(self) -> None:
            pass

    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert "graph" in raised.value.difference
    assert ToyConsumer.name == "toy_consumer"


def test_editing_the_design_file_after_planning_is_no_difference(graph, tmp_path):
    (tmp_path / "d.yaml").write_text(
        "name: d\nrtl: {sources: [a.v], top: a}\nflows: {toy_producer: {suffix: one}}\n"
    )
    spec = Spec("toy_consumer", Design.load(tmp_path / "d.yaml"))
    workspace = Workspace(graph.run_root)
    plan = workspace.plan(spec)
    (tmp_path / "d.yaml").write_text(
        "name: d\nrtl: {sources: [a.v], top: a}\nflows: {toy_producer: {suffix: two}}\n"
    )
    result = workspace.run(plan)
    assert result.passed
    assert (graph.produced()).read_text().endswith("one")


@pytest.mark.parametrize(
    "option, value",
    [
        ("rebuild_all", False),
        ("clean", False),
        ("prune_variants", False),
        ("outputs_to", None),
        ("overwrite_outputs", False),
        ("post_cleanup", False),
        ("post_cleanup_purge", False),
    ],
)
def test_every_launch_option_with_a_plan_raises_plan_options(graph, option, value):
    workspace = Workspace(graph.run_root)
    plan = workspace.plan(_spec(graph))
    with pytest.raises(SpecError) as raised:
        workspace.run(plan, **{option: value})
    assert raised.value.code == "plan_options" and option in str(raised.value)


def test_a_source_edit_gives_a_fresh_execution_not_an_error(graph):
    workspace = Workspace(graph.run_root)
    spec = _spec(graph)
    assert workspace.run(spec).passed
    plan = workspace.plan(spec)
    assert {n.state for n in plan.nodes.values()} == {"fresh"}
    graph.edit_source()
    result = workspace.run(plan)
    assert result.passed
    assert {n.disposition for n in result.nodes.values()} == {"ran"}


def test_a_changed_delivery_is_a_difference(graph, tmp_path):
    out = tmp_path / "out"
    confirmed = []
    workspace = Workspace(
        graph.run_root, confirm=lambda question: confirmed.append(question) or True
    )
    spec = _spec(graph)
    assert workspace.run(spec).passed
    plan = workspace.plan(spec, outputs_to=out, overwrite_outputs=True)
    (planned,) = plan.nodes["toy_consumer"].deliveries
    assert not planned.replaces
    planned.destination.parent.mkdir(parents=True)
    planned.destination.write_text("yours\n")
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert "deliveries" in raised.value.difference
    assert workspace.run(plan_again := workspace.plan(spec, outputs_to=out, overwrite_outputs=True))
    assert plan_again.nodes["toy_consumer"].deliveries[0].replaces


def test_post_cleanup_purge_leaves_a_reused_node_alone(graph):
    workspace = Workspace(graph.run_root)
    assert workspace.run(_spec(graph)).passed
    result = workspace.run(_spec(graph, shout=True), post_cleanup_purge=True)
    assert result.nodes["toy_producer"].disposition == "reused"
    assert result.nodes["toy_consumer"].disposition == "ran"
    assert graph.run_dir("toy_producer").is_dir()
    assert not graph.run_dir("toy_consumer").exists()


def test_prune_variants_keeps_the_planned_directory(graph, caplog):
    workspace = Workspace(graph.run_root, hashed_run_dirs=True, confirm=lambda question: True)
    with caplog.at_level(logging.INFO):
        first = workspace.run(_spec(graph), prune_variants=True)
    assert "no other run directory" in caplog.text
    second = workspace.run(_spec(graph, shout=True))
    old = first.nodes["toy_consumer"].run_dir
    assert old.exists() and second.nodes["toy_consumer"].run_dir != old
    third = workspace.run(_spec(graph, shout=True), prune_variants=True)
    kept = third.nodes["toy_consumer"].run_dir
    assert kept.exists() and not old.exists()


def test_prune_variants_asks_confirm_and_no_answer_removes_nothing(graph):
    hashed = Workspace(graph.run_root, hashed_run_dirs=True)
    first = hashed.run(_spec(graph))
    hashed.run(_spec(graph, shout=True), prune_variants=True)
    assert first.nodes["toy_consumer"].run_dir.exists()


def test_tool_output_reaches_the_launch_and_is_put_back(graph):
    seen = []

    class ToyWatcher(Flow):
        """Records where tool output goes."""

        results_description: ClassVar[dict] = {}
        design_source_types = frozenset()

        def run(self) -> None:
            seen.append(TOOL_OUTPUT.get())

    result = Workspace(graph.run_root, tool_output="terminal").run(
        Spec("toy_watcher", graph.design())
    )
    assert result.passed and seen == ["terminal"]
    assert TOOL_OUTPUT.get() == "log"


def test_run_returns_a_result_for_every_launch_that_started(graph):
    class ToyBroken(Flow):
        """Fails when it runs."""

        results_description: ClassVar[dict] = {}
        design_source_types = frozenset()

        def run(self) -> None:
            raise RuntimeError("broken")

    result = Workspace(graph.run_root).run(Spec("toy_broken", graph.design()))
    assert isinstance(result, Result) and result.outcome == "failed"


def test_the_old_launcher_name_of_prune_variants_is_refused(tmp_path):
    with pytest.raises(ValueError, match="scrub_old_runs.*removed.*prune_variants"):
        DefaultRunner(tmp_path / "runs", scrub_old_runs=True)


# ---------------------------------------------------- a stale plan changes nothing and asks nothing


class LocalDependency:
    """A design that reads Verilog sources of a local dependency, and the ways to edit them."""

    def __init__(self, root: Path) -> None:
        self.lib = root / "lib"
        self.lib.mkdir()
        for name in "bcd":
            (self.lib / f"{name}.v").write_text(f"module {name}; endmodule\n")
        (root / "top.v").write_text("module top; endmodule\n")
        (root / "d.yaml").write_text(
            "name: d\nrtl: {sources: [top.v], top: top}\ndependencies: [{uri: lib/lib.yaml}]\n"
        )
        self.root = root
        self.use("b")

    def use(self, names: str) -> None:
        """Make the dependency list the sources `<name>.v` for each letter of `names`."""
        sources = ", ".join(f"{name}.v" for name in names)
        (self.lib / "lib.yaml").write_text(f"name: lib\nrtl: {{sources: [{sources}]}}\n")

    def spec(self, flow: str) -> Spec:
        return Spec(flow, Design.load(self.root / "d.yaml"))


def _run_while_waiting_for_the_locks(workspace, plan, held: Path, during):
    """Run `plan` in a thread that waits for the lock of the run directory `held`, which the test
    holds. `during` runs once the launch waits. Return what the launch raised or returned."""
    waiting = threading.Event()
    outcome: list = []

    def call() -> None:
        try:
            outcome.append(workspace.run(plan))
        except BaseException as error:  # noqa: BLE001 - reported by the test thread
            outcome.append(error)
        finally:
            waiting.set()  # a launch that ended without a wait must not hang the test

    original = run_lock.wait_hook
    run_lock.wait_hook = lambda path: waiting.set()
    try:
        with run_lock.run_dir_lock(held, ensure_run_root(workspace.run_root)):
            thread = threading.Thread(target=call)
            thread.start()
            assert waiting.wait(HANG_GUARD)
            assert not outcome, f"the launch did not wait: {outcome}"
            during()
        thread.join(HANG_GUARD)
        assert not thread.is_alive()
    finally:
        run_lock.wait_hook = original
    return outcome[0]


def test_a_dependency_that_changes_while_the_launch_waits_for_its_locks_is_a_difference(
    tmp_path, toy  # noqa: F811
):
    dependency = LocalDependency(tmp_path)
    workspace = Workspace(tmp_path / "xeda_run")
    plan = workspace.plan(dependency.spec(toy.name))
    run_dir = plan.nodes[toy.name].run_dir
    outcome = _run_while_waiting_for_the_locks(
        workspace, plan, run_dir, lambda: dependency.use("bc")
    )
    assert isinstance(outcome, StalePlanError) and "changed" in outcome.difference
    assert not run_dir.exists(), "nothing ran"


def test_a_stale_plan_prunes_nothing(tmp_path, toy):  # noqa: F811
    dependency = LocalDependency(tmp_path)
    workspace = Workspace(tmp_path / "xeda_run", hashed_run_dirs=True, confirm=lambda q: True)
    old = workspace.run(dependency.spec(toy.name)).nodes[toy.name].run_dir
    dependency.use("bc")
    plan = workspace.plan(dependency.spec(toy.name), prune_variants=True)
    outcome = _run_while_waiting_for_the_locks(
        workspace, plan, plan.nodes[toy.name].run_dir, lambda: dependency.use("bcd")
    )
    assert isinstance(outcome, StalePlanError)
    assert old.is_dir(), "the old run is still there"
    assert not plan.nodes[toy.name].run_dir.exists()


def _suffixed(graph: ToyGraph, suffix: str) -> Spec:
    """A spec whose producer and consumer both change with `suffix`."""
    return Spec(
        "toy_consumer",
        graph.design(),
        settings={"toy_producer": {"suffix": suffix}, "toy_consumer": {"shout": suffix == "y"}},
    )


def test_a_plan_that_still_holds_prunes_the_other_runs_of_every_node(graph):
    questions: list[str] = []
    workspace = Workspace(
        graph.run_root,
        hashed_run_dirs=True,
        confirm=lambda question: questions.append(question) or True,
    )
    old = workspace.run(_suffixed(graph, "x")).nodes
    plan = workspace.plan(_suffixed(graph, "y"), prune_variants=True)
    result = workspace.run(plan)
    assert result.passed
    assert len(questions) == 2, "one question for each flow"
    for name, node in result.nodes.items():
        assert node.run_dir.is_dir() and not old[name].run_dir.exists()


@pytest.mark.parametrize("broken", [False, True])
def test_a_stale_plan_neither_regenerates_nor_meets_a_failing_generator(tmp_path, broken):
    world = World(tmp_path)
    reader = reader_flow()
    workspace = Workspace(world.run_root)
    spec = Spec(reader.name, world.load())
    assert workspace.run(spec).passed and world.runs == 1
    plan = workspace.plan(spec, clean=True)
    (storage,) = world.storages()
    before = {p: p.stat().st_mtime_ns for p in storage.rglob("*")}

    class TGenReader(Flow):
        """The reader again, with an output that the plan did not have."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        class Outputs(Flow.Outputs):
            extra: Path | None = Out(SourceType.Data, description="An output the plan lacks.")

        def run(self) -> None:
            pass

    assert TGenReader.name == reader.name
    if broken:
        (world.root / "fail").write_text("")
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert "graph" in raised.value.difference
    assert world.runs == 1, "the generator did not run"
    assert {p: p.stat().st_mtime_ns for p in storage.rglob("*")} == before


def test_a_destination_that_appears_after_planning_is_a_difference_and_no_question(graph, tmp_path):
    asked: list[str] = []
    workspace = Workspace(graph.run_root, confirm=lambda question: asked.append(question) or True)
    spec = _spec(graph)
    assert workspace.run(spec).passed
    plan = workspace.plan(spec, outputs_to=tmp_path / "out")
    (planned,) = plan.nodes["toy_consumer"].deliveries
    assert not planned.replaces
    planned.destination.parent.mkdir(parents=True)
    planned.destination.write_text("yours\n")
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert "deliveries" in raised.value.difference
    assert asked == []
    assert planned.destination.read_text() == "yours\n"


def test_a_delivery_that_planning_refuses_now_is_a_difference_not_a_refusal(
    graph, tmp_path, monkeypatch
):
    workspace = Workspace(graph.run_root)
    spec = _spec(graph)
    assert workspace.run(spec).passed
    plan = workspace.plan(spec, outputs_to=tmp_path / "out")

    def refuse(self, predicted=()):
        raise DeliveryError("`outputs_to` names a directory", before_run=True)

    monkeypatch.setattr(Deliveries, "refuse", refuse)
    with pytest.raises(StalePlanError) as raised:
        workspace.run(plan)
    assert "refused" in raised.value.difference


def test_two_entries_of_one_repository_at_different_commits_are_no_difference(
    tmp_path, remote, toy  # noqa: F811
):
    (remote.work / "src" / "b.v").write_text("module b; endmodule\n")
    (remote.work / "dep2.yaml").write_text(
        yaml.safe_dump({"name": "dep2", "rtl": {"sources": ["src/b.v"], "top": "b"}})
    )
    second = remote.commit("a second design file")
    _git("push", "--quiet", "origin", "main", cwd=remote.work)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    entries = [
        {"uri": f"git+{remote.url}?commit={remote.first}#dep.yaml"},
        {"uri": f"git+{remote.url}?commit={second}#dep2.yaml"},
    ]
    (tmp_path / "d.yaml").write_text(
        yaml.safe_dump(
            {"name": "d", "rtl": {"sources": ["top.v"], "clocks": ["clk"]}, "dependencies": entries}
        )
    )
    workspace = Workspace(tmp_path / "xeda_run")
    plan = workspace.plan(Spec(toy.name, Design.load(tmp_path / "d.yaml")))
    assert [entry[-1] for entry in plan._dependencies] == [remote.first, second]
    assert workspace.run(plan).passed


def test_dependencies_are_compared_by_path(tmp_path, remote, toy):  # noqa: F811
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    entry = {"uri": f"git+{remote.url}?commit={remote.first}#dep.yaml"}
    (tmp_path / "d.yaml").write_text(
        yaml.safe_dump({"name": "d", "rtl": {"sources": ["top.v"]}, "dependencies": [entry]})
    )
    plan = Workspace(tmp_path / "xeda_run").plan(Spec(toy.name, Design.load(tmp_path / "d.yaml")))
    path, repository, commit = plan._dependencies[0]
    assert plan_difference(plan, plan) is None
    assert "was removed" in plan_difference(plan, replace(plan, _dependencies=()))
    assert "was added" in plan_difference(replace(plan, _dependencies=()), plan)
    moved = replace(plan, _dependencies=((path, repository, "0" * 40),))
    assert plan_difference(plan, moved) == f"dependency {repository} now resolves to {'0' * 40}"
