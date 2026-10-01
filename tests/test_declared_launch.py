"""The launcher executes the plan (`resolver`): a flow that declares its inputs gets each one from
the producer the plan names -- launched by the launcher, never by the flow's `init()` -- or from
the design's sources, and is handed only what the producer recorded, checked (T7's mechanism).
Flows without declarations keep registering their dependencies, and may launch a declared one."""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowDependencyFailure, FlowFatalError
from xeda.flow_runner import DefaultRunner
from xeda.flow import Flow, In, registered_flows
from xeda.design import SourceType
from xeda.flow_runner.trace import TRACE_FORMAT

from .io_flows import _Maker, _Taker, _Wrapper


@pytest.fixture(autouse=True)
def private_registry():
    """Locally defined probes leave other tests' registered flows intact."""
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


@pytest.fixture
def design(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"})


def _runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False)


def test_the_launcher_runs_the_producer_and_hands_over_its_recorded_output(tmp_path, design):
    runner = _runner(tmp_path)
    taker = runner.launch_flow(_Taker, design, {})
    assert taker.succeeded and taker.results["read"] == "made\n"
    (maker,) = taker.completed_dependencies
    assert maker.name == "__maker"
    assert taker.inputs.made == Path(maker.results["outputs"]["made"]["path"])
    assert [flow.name for flow in runner.launched] == ["__maker", "__taker"]


def test_a_second_launch_reuses_both_and_hands_over_the_reused_record(tmp_path, design):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    again = _runner(tmp_path).launch_flow(_Taker, design, {})
    assert again.reused and again.completed_dependencies[0].reused
    assert again.inputs.made == first.inputs.made


def test_a_design_source_of_the_accepted_type_replaces_the_producer(tmp_path, design):
    (design.root_path / "given.dat").write_text("given\n")
    with_source = Design(
        name="d",
        design_root=design.root_path,
        rtl={"sources": [{"file": "given.dat", "type": "Data"}], "top": "t"},
    )
    runner = _runner(tmp_path)
    taker = runner.launch_flow(_Taker, with_source, {})
    assert taker.results["read"] == "given\n"
    assert [flow.name for flow in runner.launched] == ["__taker"]


def test_the_launch_runs_exactly_its_plan(tmp_path, design):
    """D16's oracle: the flows launched are the plan's nodes, with the plan's identities."""
    runner = _runner(tmp_path)
    sections = {"__maker": {"text": "planned\n"}}
    plan = runner.resolve(_Taker, design, {"verbose": 2}, sections)
    taker = runner.launch_flow(
        _Taker, design, {"verbose": 2}, all_flows_settings=sections, plan=plan
    )
    assert taker.results["read"] == "planned\n"
    launched = [(flow.name, flow.flow_hash, str(flow.run_path)) for flow in runner.launched]
    planned = [(node.name, node.flowrun_hash, str(node.run_path)) for node in plan.nodes]
    assert launched == planned
    assert plan.node("__maker").settings.verbose == 2  # carried from its consumer


def test_run_composes_a_producer_s_own_section_and_plan_launches_nothing(tmp_path, design):
    with_section = Design(
        name="d",
        design_root=design.root_path,
        rtl={"sources": [], "top": "t"},
        flow={"__maker": {"text": "from its section\n"}},
    )
    plan = _runner(tmp_path).plan(_Taker, with_section)
    assert [node.name for node in plan.nodes] == ["__maker", "__taker"]
    assert not (tmp_path / "xeda_run").exists(), "a plan writes nothing"
    taker = _runner(tmp_path).run(_Taker, with_section)
    assert taker is not None and taker.results["read"] == "from its section\n"


def test_an_undeclared_flow_launches_a_declared_one_with_its_own_plan(tmp_path, design):
    runner = _runner(tmp_path)
    wrapper = runner.launch_flow(_Wrapper, design, {})
    assert wrapper.succeeded and wrapper.results["read"] == "made\n"
    assert [flow.name for flow in runner.launched] == ["__maker", "__taker", "__wrapper"]


def test_a_legacy_parent_s_cli_context_reaches_its_declared_dependency(tmp_path, design):
    runner = _runner(tmp_path)
    wrapper = runner.run(_Wrapper, design, flow_settings={"verbose": 2})
    assert wrapper.succeeded and wrapper.results["read"] == "made\n"
    assert all(flow.settings.verbose == 2 for flow in runner.launched)


def test_a_legacy_plan_is_opaque_and_cannot_launch_an_unplanned_node(tmp_path, design, monkeypatch):
    def no_init(self):
        raise AssertionError("planning constructed a flow")

    monkeypatch.setattr(_Wrapper, "init", no_init)
    runner = _runner(tmp_path)
    plan = runner.plan(_Wrapper, design)
    assert len(plan.nodes) == 1 and not plan.nodes[0].declared
    with pytest.raises(FlowFatalError, match="plan has no node"):
        runner.launch_flow(_Taker, design, {}, plan=plan)
    assert not (tmp_path / "xeda_run").exists()


def test_pure_paths_create_nothing_and_launch_revalidates_containment(tmp_path, design):
    from xeda.run_dir import RunDirectoryError
    from xeda.run_root import ensure_run_root

    runner = _runner(tmp_path)
    assert runner.run_path_of("d", "__taker") == tmp_path / "xeda_run" / "d" / "__taker"
    assert not (tmp_path / "xeda_run").exists()
    with pytest.raises(RunDirectoryError, match="not a design name"):
        runner.run_path_of("../out", "__taker")
    ensure_run_root(tmp_path / "xeda_run", start=tmp_path)
    plan = runner.resolve(_Taker, design, {})
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "xeda_run" / "d").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunDirectoryError, match="leads out"):
        runner.launch_flow(_Taker, design, {}, plan=plan)
    assert list(outside.iterdir()) == []


def test_a_declared_flow_s_init_may_not_register_a_dependency(tmp_path, design, monkeypatch):
    monkeypatch.setattr(_Taker, "init", lambda self: self.add_dependency(_Maker, {}))
    with pytest.raises(FlowFatalError, match="may not add a dependency"):
        _runner(tmp_path).launch_flow(_Taker, design, {})


def test_an_output_changed_after_its_run_recorded_it_is_never_handed_over(
    tmp_path, design, monkeypatch
):
    launch = DefaultRunner.launch_flow

    def launch_then_rewrite(self, flow_class, *args, **kwargs):
        flow = launch(self, flow_class, *args, **kwargs)
        if flow.name == "__maker":  # another process rebuilds it before its consumer reads it
            Path(flow.results["outputs"]["made"]["path"]).write_text("rebuilt\n")
        return flow

    monkeypatch.setattr(DefaultRunner, "launch_flow", launch_then_rewrite)
    with pytest.raises(FlowDependencyFailure, match="changed before acquiring its read lease"):
        _runner(tmp_path).launch_flow(_Taker, design, {})
    results = json.loads((tmp_path / "xeda_run" / "d" / "__taker" / "results.json").read_text())
    assert results["success"] is False


@pytest.mark.parametrize("failure", ["init", "dependency"])
def test_setup_failure_invalidates_a_previous_success(tmp_path, design, monkeypatch, failure):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    if failure == "init":

        def fail(self):
            raise FlowFatalError("broken init")

    else:

        def fail(self):
            self.add_dependency(_Maker, {})

    monkeypatch.setattr(_Taker, "init", fail)
    with pytest.raises(FlowFatalError):
        _runner(tmp_path).launch_flow(_Taker, design, {})
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False
    for key in ("design", "flow", "design_hash", "flow_hash", "run_path", "timestamp"):
        assert recorded[key]
    assert not (first.run_path / "trace.json").exists()


@pytest.mark.parametrize(
    "changed", ["design", "cwd", "policy", "settings", "model_context", "sections"]
)
def test_an_internal_plan_cannot_be_replayed_for_another_request(
    tmp_path, design, monkeypatch, changed
):
    runner = _runner(tmp_path)
    plan = runner.resolve(_Taker, design, {})
    settings = {}
    sections = None
    if changed == "design":
        design = design.model_copy(update={"name": "other"})
    elif changed == "cwd":
        folder = tmp_path / "elsewhere"
        folder.mkdir()
        monkeypatch.chdir(folder)
    elif changed == "policy":
        runner.settings.hashed_run_dirs = True
    elif changed == "model_context":
        settings = _Taker.Settings.from_input({}, design_root=tmp_path, runner_cwd=tmp_path)
    elif changed == "sections":
        sections = {_Maker.name: {"text": "different\n"}}
    else:
        settings = {"verbose": 2}
    with pytest.raises(FlowFatalError, match="plan.*request|plan.*context"):
        runner.launch_flow(_Taker, design, settings, all_flows_settings=sections, plan=plan)
    assert not (tmp_path / "xeda_run").exists()


def test_two_inputs_launch_the_same_producer_once_and_keep_the_plan(tmp_path, design, monkeypatch):
    leases = []

    @contextmanager
    def read_lease(self, producer):
        leases.append("enter")
        yield producer
        leases.append("exit")

    monkeypatch.setattr(DefaultRunner, "_producer_read_lease", read_lease)

    class _Twice(Flow):
        """Reads the same producer's output twice."""

        results_description = {}

        class Inputs(Flow.Inputs):
            a: Path = In(SourceType.Data, producer="__maker", output="made", description="First.")
            b: Path = In(SourceType.Data, producer="__maker", output="made", description="Second.")

        def run(self):
            assert leases == ["enter"]
            self.results["read"] = self.inputs.a.read_text() + self.inputs.b.read_text()

    runner = _runner(tmp_path)
    plan = runner.resolve(_Twice, design, {})
    before = [(n.name, n.settings.model_dump(), n.inputs, n.switched_on) for n in plan.nodes]
    flow = runner.launch_flow(_Twice, design, {}, plan=plan)
    assert flow.results["read"] == "made\nmade\n"
    assert leases == ["enter", "exit"]
    assert [(f.name, f.flow_hash, f.run_path) for f in runner.launched] == [
        (n.name, n.flowrun_hash, n.run_path) for n in plan.nodes
    ]
    assert before == [
        (n.name, n.settings.model_dump(), n.inputs, n.switched_on) for n in plan.nodes
    ]
    trace = json.loads((flow.run_path / "trace.json").read_text())
    assert trace["format"] == TRACE_FORMAT > 12
    assert [i["name"] for i in trace["declared_inputs"]] == ["a", "b"]
    assert all(i["origin"] == "producer" for i in trace["declared_inputs"])


def test_declared_outputs_survive_cleanup_and_are_delivered_without_artifact_labels(
    tmp_path, design
):
    runner = DefaultRunner(
        tmp_path / "xeda_run", display_results=False, post_cleanup=True, outputs_to=tmp_path / "out"
    )
    maker = runner.launch_flow(_Maker, design, {})
    assert Path(maker.results["outputs"]["made"]["path"]).read_text() == "made\n"
    assert (tmp_path / "out" / "made.txt").read_text() == "made\n"


@pytest.mark.parametrize("binding", ["name", "origin", "order", "format"])
def test_changed_binding_provenance_invalidates_the_same_file_set(tmp_path, design, binding):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    trace_file = first.run_path / "trace.json"
    trace = json.loads(trace_file.read_text())
    if binding == "format":
        trace["format"] = 12
    elif binding == "order":
        trace["declared_inputs"] = [trace["declared_inputs"][0], trace["declared_inputs"][0]]
    else:
        trace["declared_inputs"][0][binding] = "different" if binding == "name" else "source"
    trace_file.write_text(json.dumps(trace))
    again = _runner(tmp_path).launch_flow(_Taker, design, {})
    assert not again.reused and again.completed_dependencies[0].reused
    assert (
        "another xeda" if binding == "format" else "declared input bindings changed"
    ) in again.stale_reason


def test_planning_refuses_generator_and_git_loading_before_any_effect(tmp_path, monkeypatch):
    from xeda.design import DesignValidationError

    monkeypatch.chdir(tmp_path)
    for spec, message in (
        ('name="d"\n[rtl]\nsources=[]\ntop="t"\ngenerator="touch generated"\n', "generator"),
        (
            'name="d"\ndependencies=["git+https://example.invalid/repo.git#d.toml"]\n[rtl]\nsources=[]\ntop="t"\n',
            "Git dependency fetch",
        ),
    ):
        path = tmp_path / "d.toml"
        path.write_text(spec)
        with pytest.raises(DesignValidationError, match=message):
            _runner(tmp_path).plan(_Taker, path)
        assert not (tmp_path / "generated").exists()
        assert not (tmp_path / "xeda_run").exists()


def test_a_failed_producer_reports_its_error_and_invalidates_its_consumer(
    tmp_path, design, monkeypatch
):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})

    def fail(self):
        raise FlowFatalError("maker broke")

    monkeypatch.setattr(_Maker, "run", fail)
    runner = DefaultRunner(
        tmp_path / "xeda_run", display_results=False, rebuild_all=True, outputs_to=tmp_path / "out"
    )
    with pytest.raises(FlowDependencyFailure, match="maker broke.*results.json"):
        runner.launch_flow(_Taker, design, {})
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False and recorded["error"]["type"] == "FlowDependencyFailure"
    assert not (first.run_path / "trace.json").exists()
    assert not (tmp_path / "out").exists()


def test_absent_optional_inputs_keep_their_cardinality(tmp_path, design):
    class _Optional(Flow):
        """Accepts absent optional inputs."""

        results_description = {}

        class Inputs(Flow.Inputs):
            one: Path | None = In(SourceType.Data, description="Optional scalar.")
            many: list[Path] = In(
                SourceType.MemoryFile, optional=True, description="Optional list."
            )

        def run(self):
            assert self.inputs.one is None and self.inputs.many == []

    runner = _runner(tmp_path)
    flow = runner.launch_flow(_Optional, design, {})
    assert flow.succeeded
    trace = json.loads((flow.run_path / "trace.json").read_text())
    assert [entry["origin"] for entry in trace["declared_inputs"]] == ["none", "none"]


@pytest.mark.parametrize("change", ["missing", "changed"])
def test_a_failed_handover_invalidates_previous_success_without_delivery(
    tmp_path, design, monkeypatch, change
):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    launch = DefaultRunner.launch_flow

    def change_output(self, flow_class, *args, **kwargs):
        flow = launch(self, flow_class, *args, **kwargs)
        if flow.name == _Maker.name:
            path = Path(flow.results["outputs"]["made"]["path"])
            if change == "missing":
                path.unlink()
            else:
                path.write_text("changed\n")
        return flow

    monkeypatch.setattr(DefaultRunner, "launch_flow", change_output)
    runner = DefaultRunner(tmp_path / "xeda_run", outputs_to=tmp_path / "out")
    with pytest.raises(FlowDependencyFailure, match="missing|changed"):
        runner.launch_flow(_Taker, design, {})
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False
    assert recorded["error"]["type"] == "FlowDependencyFailure"
    assert all(recorded[key] for key in ("design_hash", "flow_hash", "run_path", "timestamp"))
    assert not (first.run_path / "trace.json").exists()
    assert not (tmp_path / "out").exists()
