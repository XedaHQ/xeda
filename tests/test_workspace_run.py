"""`Workspace.run` runs a `Spec` or a `Plan`, and a plan is checked against a new planning."""

import logging
import sys
from typing import ClassVar

import pytest

from xeda import Design
from xeda.dataclass import Field
from xeda.errors import SpecError, StalePlanError
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner
from xeda.local_settings import LocalSettings
from xeda.outcomes import Result
from xeda.proc_utils import TOOL_OUTPUT
from xeda.spec import Spec
from xeda.workspace import Workspace

from .flow_registry import preserved_flow_registry
from .test_dry_run_freshness import ToyGraph, toys  # noqa: F401  (the toy flows)

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
