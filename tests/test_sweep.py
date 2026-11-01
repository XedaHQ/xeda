"""`Workspace.sweep` plans every point first, then executes each node once, in this process.

The tests run the open FPGA chain `yosys_fpga+nextpnr` through the process fakes of
`tests/fake_tools`. The fakes record each call in `fake_fpga.calls.jsonl` in the run directory of
the node, so a test counts what ran.
"""

import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from xeda import Design
from xeda.cancel import CancelToken
from xeda.errors import LeaseHeldError, RecordError, RunReplacedError, SpecError, StalePlanError
from xeda.events import Event
from xeda.flow_runner import run_lock
from xeda.flow_runner.trace import previous_trace
from xeda.flow_runner.environment import EnvironmentSnapshot
from xeda.flow_runner.launch_options import LaunchOptions
from xeda.outcomes import Disposition, Outcome, Result
from xeda.records import ErrorInfo, encode_record, read_record
from xeda.spec import Spec
from xeda.sweep import CompletedNode, NodeTask, SweepResult, node_key, run_node
from xeda.utils import dump_json
from xeda.workspace import Workspace

from . import tool_utils
from .cancel_flows import cut_flows
from .flow_registry import preserved_flow_registry
from .test_cancel_and_events import _dependent
from .test_git_dependencies import REAL_GIT, git_log, remote  # noqa: F401  (a Git remote)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

ECP5 = "LFE5U-25F-6BG381C"
MARK = "XEDA_FAKE_FPGA_MARK"


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


@pytest.fixture
def design(tmp_path, monkeypatch) -> Design:
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    return Design(
        name="top", root=root, rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]}
    )


def _spec(design: Design, seed: int, **settings) -> Spec:
    table = {"nextpnr.seed": seed, "nextpnr.fpga.part": ECP5, **settings}
    return Spec("yosys_fpga+nextpnr", design, settings=table)


def _seeds(design: Design, count: int = 4) -> list[Spec]:
    return [_spec(design, seed) for seed in range(1, count + 1)]


def _workspace(tmp_path: Path, **options) -> Workspace:
    return Workspace(tmp_path / "run", hashed_run_dirs=True, **options)


def _calls(workspace: Workspace) -> Counter:
    """How often each fake tool ran, in every run directory of the workspace."""
    found: Counter = Counter()
    for file in workspace.run_root.rglob("fake_fpga.calls.jsonl"):
        for line in file.read_text().splitlines():
            found[json.loads(line)["tool"]] += 1
    return found


def _calls_of(workspace: Workspace, prefix: str) -> Counter:
    return Counter(
        {tool: count for tool, count in _calls(workspace).items() if tool.startswith(prefix)}
    )


def _dispositions(result: SweepResult, node: str) -> list[str]:
    return [str(point.result.nodes[node].disposition) for point in result]


def test_a_seed_sweep_runs_one_shared_producer(tmp_path, design):
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design))
    assert [point.index for point in result] == [0, 1, 2, 3] and len(result) == 4
    assert _dispositions(result, "yosys_fpga") == ["ran", "shared", "shared", "shared"]
    assert _dispositions(result, "nextpnr") == ["ran"] * 4
    assert dict(result.counts()) == {Outcome.PASSED: 4} and not result.canceled
    first, second = (point.result.nodes["yosys_fpga"] for point in list(result)[:2])
    assert second.outcome is first.outcome and second.record == first.record
    assert second.elapsed == 0.0 and second.reason == "shared with point 0"
    calls = _calls(workspace)
    assert calls["yosys"] == 1 and calls["nextpnr-ecp5"] == 4


def test_rebuild_all_rebuilds_a_shared_node_once(tmp_path, design):
    workspace = _workspace(tmp_path)
    workspace.sweep(_seeds(design))
    again = workspace.sweep(_seeds(design), rebuild_all=True)
    assert _dispositions(again, "yosys_fpga") == ["ran", "shared", "shared", "shared"]
    calls = _calls(workspace)
    assert calls["yosys"] == 2 and calls["nextpnr-ecp5"] == 8


def test_a_node_reused_from_an_earlier_launch_is_reused_in_every_point(tmp_path, design):
    workspace = _workspace(tmp_path)
    workspace.sweep(_seeds(design))
    again = workspace.sweep(_seeds(design))
    assert _dispositions(again, "yosys_fpga") == ["reused"] * 4
    assert _dispositions(again, "nextpnr") == ["reused"] * 4
    assert _calls(workspace)["yosys"] == 1


def _task(workspace: Workspace, spec: Spec, node: str, completed, **fields) -> NodeTask:
    return NodeTask(
        spec=spec,
        node=node,
        identity=workspace.plan(spec).nodes[node].identity,
        workspace=workspace._options(),
        launch=LaunchOptions(),
        snapshot=EnvironmentSnapshot.take({}),
        completed=tuple(completed),
        rebuild=fields.get("rebuild", False),
    )


def _completed(result) -> CompletedNode:
    node = result.nodes["yosys_fpga"]
    assert node.record is not None
    return CompletedNode("yosys_fpga", node.run_dir, node.record.run_id, node.outcome)


def test_a_consumer_never_launches_its_completed_producer(tmp_path, design):
    workspace = _workspace(tmp_path)
    spec = _spec(design, 1)
    first = workspace.run(spec)
    done = _completed(first)
    task = _task(workspace, spec, "nextpnr", [done], rebuild=True)
    outcome = run_node(task, canceled=lambda: False, emit=lambda event: None)
    assert outcome.node == "nextpnr" and outcome.result.outcome is Outcome.PASSED
    assert outcome.result.disposition is Disposition.RAN and outcome.run_id is not None
    calls = _calls(workspace)
    assert calls["yosys"] == 1 and calls["nextpnr-ecp5"] == 2
    assert outcome.result.record is not None
    (producer,) = [i for i in outcome.result.record.inputs if i.producer == "yosys_fpga"]
    assert producer.producer_run_dir == done.run_dir and producer.producer_run_id == done.run_id


def test_a_node_whose_identity_changed_since_the_plan_is_refused_before_it_runs(tmp_path, design):
    workspace = _workspace(tmp_path)
    spec = _spec(design, 1)
    task = _task(workspace, spec, "yosys_fpga", [])
    stale = replace(task, identity="0" * 64)
    with pytest.raises(StalePlanError) as raised:
        run_node(stale, canceled=lambda: False, emit=lambda event: None)
    assert raised.value.code == "stale_plan" and "yosys_fpga" in raised.value.difference
    assert not _calls(workspace), "nothing ran"
    assert not list(workspace.run_root.rglob("*.lock")), "no lock was taken"
    outcome = run_node(task, canceled=lambda: False, emit=lambda event: None)
    assert outcome.result.outcome is Outcome.PASSED


def _replace_record(directory: Path, workspace: Workspace, **changes) -> str:
    """Write the record of `directory` again, as a launch outside the sweep would: another run."""
    record = read_record(directory, run_root=workspace.run_root)
    new_id = "0123456789abcdef0123456789abcdef"
    dump_json(encode_record(replace(record, run_id=new_id, **changes)), directory / "results.json")
    return new_id


@pytest.mark.parametrize("passed", [True, False], ids=["a passed record", "a failed record"])
def test_a_replaced_producer_is_read_as_found_never_run_again(
    tmp_path, design, monkeypatch, passed
):
    workspace = _workspace(tmp_path)
    spec = _spec(design, 1)
    first = workspace.run(spec)
    done = _completed(first)
    changes: dict = {}
    if not passed:
        changes = dict(
            outcome="failed",
            error=ErrorInfo(
                code="nonzero_exit", message="again", node="yosys_fpga", exit_status=1, log=None
            ),
        )
    taken: list[str] = []
    lock = done.run_dir.parent / (done.run_dir.name + ".lock")

    def after_the_lease(path: Path) -> None:
        if path == lock and not taken:
            taken.append(_replace_record(done.run_dir, workspace, **changes))

    monkeypatch.setattr(run_lock, "acquired_hook", after_the_lease)
    outcome = run_node(
        _task(workspace, spec, "nextpnr", [done], rebuild=True),
        canceled=lambda: False,
        emit=lambda event: None,
    )
    assert taken and _calls(workspace)["yosys"] == 1, "the producer did not run again"
    if passed:
        assert outcome.result.outcome is Outcome.PASSED and outcome.result.record is not None
        (producer,) = [i for i in outcome.result.record.inputs if i.producer == "yosys_fpga"]
        assert producer.producer_run_id == taken[0]
    else:
        assert outcome.result.error is not None
        assert outcome.result.error.code == "producer_failed"
        assert outcome.result.outcome is Outcome.BLOCKED and outcome.run_id is None


def test_a_failed_shared_producer_is_shared_and_blocks_every_point(tmp_path, design, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "yosys")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design, 3))
    yosys = [point.result.nodes["yosys_fpga"] for point in result]
    assert [str(n.outcome) for n in yosys] == ["failed"] * 3
    assert [str(n.disposition) for n in yosys] == ["ran", "shared", "shared"]
    assert yosys[1].error == yosys[0].error and yosys[1].record == yosys[0].record
    for point in result:
        nextpnr = point.result.nodes["nextpnr"]
        assert nextpnr.outcome is Outcome.BLOCKED and nextpnr.blocked_by == "yosys_fpga"
        assert point.result.outcome is Outcome.FAILED
        assert point.result.error == yosys[0].error
    calls = _calls(workspace)
    assert calls["yosys"] == 1 and not calls["nextpnr-ecp5"]


def test_two_configurations_on_one_directory_are_refused_before_execution(tmp_path, design):
    workspace = Workspace(tmp_path / "run")
    with pytest.raises(SpecError) as raised:
        workspace.sweep(_seeds(design, 2))
    assert raised.value.code == "colliding_run_dirs" and raised.value.points == (0, 1)
    assert "hashed_run_dirs=True" in raised.value.message
    assert not (tmp_path / "run").exists(), "nothing was written"


def test_equal_specs_are_refused(tmp_path, design):
    workspace = _workspace(tmp_path)
    ns = _spec(design, 1, **{"nextpnr.clocks.main_clock.period": "10 ns"})
    ps = _spec(design, 1, **{"nextpnr.clocks.main_clock.period": "10000 ps"})
    other = _spec(design, 2)
    with pytest.raises(SpecError) as raised:
        workspace.sweep([other, ns, ps])
    assert raised.value.code == "equal_specs" and raised.value.points == (1, 2)
    assert not (tmp_path / "run").exists()


def test_one_destination_for_two_points_is_refused(tmp_path, design):
    destination = tmp_path / "out" / "routed.config"
    specs = [_spec(design, seed, **{"nextpnr.textcfg": str(destination)}) for seed in (1, 2)]
    with pytest.raises(SpecError) as raised:
        _workspace(tmp_path).sweep(specs)
    assert raised.value.code == "shared_destination" and raised.value.points == (0, 1)
    assert not (tmp_path / "run").exists() and not destination.exists()


def test_a_point_that_cannot_be_planned_names_itself(tmp_path, design):
    specs = [_spec(design, 1), Spec("yosys_fpga+nextpnr", design, settings={"nextpnr.nosuch": 1})]
    with pytest.raises(SpecError) as raised:
        _workspace(tmp_path).sweep(specs)
    assert raised.value.points == (1,)


def test_an_action_node_is_never_shared(tmp_path, design):
    workspace = _workspace(tmp_path)
    plan = workspace.plan(
        Spec(
            "nextpnr+fpga_pack+openfpgaloader",
            design,
            settings={"nextpnr": {"fpga": {"part": ECP5}}},
        )
    )
    action = [node for node in plan.nodes.values() if node.action]
    assert [node.name for node in action] == ["openfpgaloader"]
    for node in plan.nodes.values():
        same = node_key(0, node, tmp_path) == node_key(1, node, tmp_path)
        if not node.action:
            assert node_key(0, node, tmp_path) != node_key(0, node, tmp_path / "other")
        assert same is not node.action


def test_rows_have_the_contract_columns(tmp_path, design):
    workspace = _workspace(tmp_path)
    updated = design.with_updates({"rtl": {"parameters": {"WIDTH": 8}}})
    specs = [replace(_spec(design, seed), design=updated) for seed in (1, 2)]
    result = workspace.sweep(specs)
    rows = result.rows(metrics=("nextpnr.fmax", "yosys_fpga.lut"))
    assert list(rows[0])[:7] == [
        "index",
        "flow",
        "design",
        "target",
        "outcome",
        "error_code",
        "run_dir",
    ]
    assert [row["index"] for row in rows] == [0, 1] and rows[0]["design"] == "top"
    assert rows[0]["flow"] == "yosys_fpga+nextpnr" and rows[0]["outcome"] == "passed"
    assert rows[0]["error_code"] is None and rows[0]["target"] is None
    assert rows[0]["run_dir"] == result.points[0].result.nodes["nextpnr"].run_dir
    assert rows[0]["design.rtl.parameters.WIDTH"] == 8
    assert [row["settings.nextpnr.seed"] for row in rows] == [1, 2]
    assert rows[0]["settings.nextpnr.fpga.part"] == ECP5
    assert "nextpnr.fmax" in rows[0] and "yosys_fpga.lut" in rows[0]
    names = list(rows[0])
    assert names.index("design.rtl.parameters.WIDTH") < names.index("settings.nextpnr.seed")
    assert names.index("settings.nextpnr.seed") < names.index("nextpnr.fmax")
    # nested and dotted settings give one column
    nested = Spec(
        "yosys_fpga+nextpnr", design, settings={"nextpnr": {"seed": 1, "fpga": {"part": ECP5}}}
    )
    assert workspace.sweep([nested]).rows()[0]["settings.nextpnr.seed"] == 1


def test_a_metric_of_a_node_in_no_point_raises_key_error(tmp_path, design):
    result = _workspace(tmp_path).sweep(_seeds(design, 2))
    for text in ("fmax", "nosuchnode.fmax"):
        with pytest.raises(KeyError) as raised:
            result.rows(metrics=(text,))
        assert raised.value.args[0] == (
            f"`{text}` names no node of this sweep. Write `<node>.<metric>`, such as "
            "`nextpnr.fmax`. The nodes are: nextpnr, yosys_fpga."
        )


def test_a_metric_column_is_none_where_the_node_has_no_record(tmp_path, design, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "yosys")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    rows = _workspace(tmp_path).sweep(_seeds(design, 2)).rows(metrics=("nextpnr.fmax",))
    assert [row["nextpnr.fmax"] for row in rows] == [None, None]
    assert [row["error_code"] for row in rows] == ["nonzero_exit"] * 2


def test_every_point_uses_the_snapshot_taken_at_the_start(tmp_path, design, monkeypatch):
    monkeypatch.setenv(MARK, "OLD")
    workspace = _workspace(tmp_path)
    changed: list[str] = []

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and not changed:
            changed.append(event.node or "")
            monkeypatch.setenv(MARK, "NEW")

    result = workspace.sweep(_seeds(design, 3), on_event=on_event)
    assert changed and result.counts() == {Outcome.PASSED: 3}
    seen = {
        json.loads(line)["environment"].get(MARK)
        for file in workspace.run_root.rglob("fake_fpga.calls.jsonl")
        for line in file.read_text().splitlines()
    }
    assert seen == {"OLD"}


def _lock_of(directory: Path) -> Path:
    return directory.parent / (directory.name + ".lock")


@pytest.mark.parametrize("replaced", [False, True], ids=["purged", "replaced meanwhile"])
def test_shared_node_clean_up_retakes_the_lock_and_checks_the_run_id(
    tmp_path, design, monkeypatch, replaced
):
    workspace = _workspace(tmp_path)
    shared = workspace.plan(_spec(design, 1)).nodes["yosys_fpga"].run_dir
    swapped: list[str] = []

    def hook(path: Path) -> None:
        if path == _lock_of(shared) and not swapped:
            swapped.append(_replace_record(shared, workspace))

    finished: list[Event] = []

    def on_event(event: Event) -> None:
        finished.append(event)
        # The last point has finished: what follows is the clean-up, which takes the lock again.
        if replaced and sum(e.kind == "launch_finished" for e in finished) == 2:
            monkeypatch.setattr(run_lock, "acquired_hook", hook)

    result = workspace.sweep(_seeds(design, 2), post_cleanup_purge=True, on_event=on_event)
    nextpnr_dirs = [point.result.nodes["nextpnr"].run_dir for point in result]
    assert not any(directory.exists() for directory in nextpnr_dirs)
    record = result.points[1].result.nodes["yosys_fpga"].record
    assert record is not None
    if replaced:
        assert swapped and shared.is_dir(), "the directory of another run is left as it is"
        with pytest.raises(RunReplacedError):
            with record.open():
                pass
    else:
        assert not shared.exists(), "the shared node is purged after both points"
        with pytest.raises(RecordError) as raised:
            with record.open():
                pass
        assert raised.value.code == "missing_record"


def test_post_cleanup_keeps_the_records_and_the_artifacts_of_the_nodes_that_ran(tmp_path, design):
    workspace = _workspace(tmp_path)
    result = workspace.sweep(_seeds(design, 2), post_cleanup=True)
    for point in result:
        record = point.result.nodes["nextpnr"].record
        assert record is not None and (record.run_dir / "results.json").is_file()
        assert not (record.run_dir / "trace.json").exists(), "such a run is never reused"
        with record.open():
            pass


def test_a_sweep_callback_holds_no_lock_and_can_open_any_record(tmp_path, design):
    workspace = _workspace(tmp_path)
    opened: list[int] = []

    def on_event(event: Event) -> None:
        if event.kind != "node_finished":
            return
        for record in workspace.records():
            with record.open():
                opened.append(1)

    result = workspace.sweep(_seeds(design, 2), on_event=on_event)
    assert result.counts() == {Outcome.PASSED: 2} and len(opened) >= 4


def test_a_sweep_refuses_to_start_in_a_thread_that_holds_a_lock(tmp_path, design):
    workspace = _workspace(tmp_path)
    workspace.run(_spec(design, 1))
    (record,) = [r for r in workspace.records() if r.flow == "nextpnr"]
    with record.open():
        with pytest.raises(LeaseHeldError):
            workspace.sweep([_spec(design, 1)])


def test_unstarted_points_of_a_canceled_sweep_are_canceled_with_skipped_nodes(tmp_path, design):
    workspace = _workspace(tmp_path)
    token = CancelToken()

    def on_event(event: Event) -> None:
        if event.kind == "node_finished":
            token.cancel()

    specs = [_spec(design, 1), _spec(design, 1, **{"yosys_fpga.nobram": True})]
    result = workspace.sweep(specs, cancel=token, on_event=on_event)
    assert result.canceled
    started, unstarted = (point.result for point in result)
    assert started.nodes["yosys_fpga"].outcome is Outcome.PASSED
    assert started.nodes["nextpnr"].outcome is Outcome.SKIPPED
    assert started.outcome is Outcome.CANCELED
    assert unstarted.outcome is Outcome.CANCELED and unstarted.error.code == "canceled"
    assert {str(n.outcome) for n in unstarted.nodes.values()} == {"skipped"}
    assert {str(n.disposition) for n in unstarted.nodes.values()} == {"not_run"}
    assert not _calls_of(workspace, "nextpnr")


def test_events_of_a_sweep_come_in_order_within_each_launch(tmp_path, design):
    events: list[Event] = []
    result = _workspace(tmp_path).sweep(_seeds(design, 2), on_event=events.append)
    for point in result:
        mine = [e for e in events if e.launch_id == point.result.launch_id]
        assert [e.kind for e in mine] == [
            "launch_started",
            "node_started",
            "node_finished",
            "node_started",
            "node_finished",
            "launch_finished",
        ]
        assert [e.node for e in mine][1:5] == ["yosys_fpga", "yosys_fpga", "nextpnr", "nextpnr"]
        assert mine[-1].outcome is Outcome.PASSED
    assert len({point.result.launch_id for point in result}) == 2
    finished = [e for e in events if e.kind == "node_finished" and e.node == "yosys_fpga"]
    assert [str(e.disposition) for e in finished] == ["ran", "shared"]


def test_a_number_of_workers_that_is_not_a_positive_whole_number_is_refused(tmp_path, design):
    workspace = _workspace(tmp_path)
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_workers"):
            workspace.sweep(_seeds(design, 1), max_workers=bad)
    assert not (tmp_path / "run").exists()


def test_a_task_crosses_a_process_boundary(tmp_path, design):
    import pickle

    workspace = _workspace(tmp_path)
    task = _task(workspace, _spec(design, 1), "nextpnr", [])
    assert pickle.loads(pickle.dumps(task)) == task
    done = CompletedNode("yosys_fpga", tmp_path, "ab" * 16, Outcome.PASSED)
    assert pickle.loads(pickle.dumps(done)) == done


def test_an_empty_sweep_has_no_point(tmp_path):
    result = Workspace(tmp_path / "run").sweep([])
    assert len(result) == 0 and not result.canceled and result.rows() == []


def _edif_spec(design: Design, seed: int, destination: Path, **settings) -> Spec:
    return _spec(design, seed, **{"yosys_fpga.netlist_verilog": str(destination), **settings})


def _point_view(result: Result) -> tuple:
    """What a launch of a point shows, apart from where it ran: the verdict and each node."""
    return (
        str(result.outcome),
        None if result.error is None else (result.error.code, result.error.node),
        {n.name: (str(n.outcome), n.blocked_by) for n in result.nodes.values()},
    )


def test_a_point_delivers_when_it_ends_as_a_launch_of_that_point_alone_does(
    tmp_path, design, monkeypatch
):
    failing = {
        "nextpnr.env": {"XEDA_FAKE_FPGA_MODE": "fail", "XEDA_FAKE_FPGA_TOOL": "nextpnr-ecp5"}
    }
    out = tmp_path / "out"
    a, b, c = (out / f"{name}.v" for name in "abc")
    specs = [
        _edif_spec(design, 1, a),
        _edif_spec(design, 2, b, **failing),
        _edif_spec(design, 3, c),
    ]
    workspace = _workspace(tmp_path)
    result = workspace.sweep(specs)
    assert _dispositions(result, "yosys_fpga") == ["ran", "shared", "shared"]
    assert [str(p.result.outcome) for p in result] == ["passed", "failed", "passed"]
    assert a.is_file() and c.is_file() and not b.exists(), "a failed point delivers nothing"
    assert a.read_bytes() == c.read_bytes()
    for point, destination in zip(result, (a, b, c)):
        delivered = point.result.nodes["yosys_fpga"].deliveries
        assert [(d.key, d.destination, d.delivered) for d in delivered] == (
            [("netlist_verilog", destination, True)] if destination.is_file() else []
        )
    # Each point alone, in a workspace of its own, delivers the same files.
    alone = tmp_path / "alone"
    for spec, destination in zip(specs, (a, b, c)):
        destination.unlink(missing_ok=True)
        single = Workspace(alone, hashed_run_dirs=True).run(spec)
        assert destination.is_file() is (single.outcome is Outcome.PASSED)
        destination.unlink(missing_ok=True)


def test_a_file_in_the_way_of_a_delivery_is_refused_before_anything_runs(tmp_path, design):
    destination = tmp_path / "out" / "top.v"
    destination.parent.mkdir()
    destination.write_text("a file that is not Xeda's\n")
    specs = [_spec(design, 1), _edif_spec(design, 2, destination)]
    with pytest.raises(SpecError) as raised:
        _workspace(tmp_path).sweep(specs)
    assert raised.value.code == "delivery_refused" and raised.value.points == (1,)
    assert destination.read_text() == "a file that is not Xeda's\n"
    assert not (tmp_path / "run").exists()


def test_a_failed_delivery_is_an_error_of_the_point_as_it_is_of_a_launch(tmp_path, design):
    destination = tmp_path / "out" / "top.v"
    destination.parent.mkdir()

    def on_event(event: Event) -> None:
        # Another process puts a file there after the plan, while the sweep runs.
        if event.kind == "node_finished":
            destination.write_text("a file that is not Xeda's\n")

    swept = _workspace(tmp_path).sweep([_edif_spec(design, 1, destination)], on_event=on_event)
    (point,) = swept
    assert point.result.outcome is Outcome.FAILED
    assert point.result.error is not None and point.result.error.node is None
    assert point.result.error.code == "delivery_failed"
    assert {str(n.outcome) for n in point.result.nodes.values()} == {"passed"}
    assert destination.read_text() == "a file that is not Xeda's\n"


def test_a_generator_failure_is_an_error_of_the_point_as_it_is_of_a_launch(tmp_path):
    from .generation_world import World, reader_flow

    flow = reader_flow()
    world = World(tmp_path)
    (world.root / "fail").write_text("")
    base = world.load()
    specs = [
        Spec(flow.name, base.with_updates({"rtl": {"parameters": {"W": width}}}))
        for width in (1, 2)
    ]
    swept = Workspace(world.run_root, hashed_run_dirs=True).sweep(specs)
    for point, spec in zip(swept, specs):
        alone = Workspace(tmp_path / "alone", hashed_run_dirs=True).run(spec)
        assert alone.error is not None and alone.error.code == "generator_failed"
        assert _point_view(point.result) == _point_view(alone)
        assert point.result.error.node is None
        assert {str(n.disposition) for n in point.result.nodes.values()} == {"not_run"}
    assert {point.result.outcome for point in swept} == {Outcome.FAILED}
    assert not swept.canceled


def test_to_pandas_has_the_columns_of_rows(tmp_path, design):
    pytest.importorskip("pandas")
    result = _workspace(tmp_path).sweep(_seeds(design, 2))
    frame = result.to_pandas(metrics=("nextpnr.fmax",))
    assert list(frame.columns) == list(result.rows(metrics=("nextpnr.fmax",))[0])
    assert list(frame["settings.nextpnr.seed"]) == [1, 2] and len(frame) == 2


def test_to_pandas_without_pandas_is_a_spec_error(tmp_path, design, monkeypatch):
    result = _workspace(tmp_path).sweep(_seeds(design, 1))
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(SpecError) as raised:
        result.to_pandas()
    assert raised.value.code == "missing_dependency"


# ------------------------------------------------------------------------------ review findings


def _design_in(tmp_path: Path, directory: str, text: str) -> Design:
    root = tmp_path / directory
    root.mkdir()
    (root / "top.v").write_text(text)
    return Design(
        name="top", root=root, rtl={"sources": ["top.v"], "top": "top", "clocks": ["clk"]}
    )


def test_designs_of_one_name_in_two_roots_do_not_share_a_node(tmp_path, design):
    text = "module top(input clk, output q); assign q = {}; endmodule\n"
    first = _design_in(tmp_path, "one", text.format("clk"))
    second = _design_in(tmp_path, "two", text.format("~clk"))
    third = _design_in(tmp_path, "three", text.format("1'b0"))
    workspace = _workspace(tmp_path)
    shared = workspace.plan(_spec(first, 1)).nodes["yosys_fpga"].run_dir
    sources = (
        "{} use one run directory, " + str(shared) + ", for designs with different sources. "
        "Give each design its own name, or sweep them separately."
    )
    for specs, points, text in (
        # equal identities for `yosys_fpga`
        ([_spec(first, 1), _spec(second, 2)], (0, 1), "points 0 and 1"),
        # equal identities for the whole run
        ([_spec(first, 1), _spec(second, 1)], (0, 1), "points 0 and 1"),
        ([_spec(first, 1), _spec(third, 3), _spec(second, 2)], (0, 1, 2), "points 0, 1 and 2"),
    ):
        with pytest.raises(SpecError) as raised:
            workspace.sweep(specs)
        assert raised.value.code == "colliding_run_dirs" and raised.value.points == points
        assert raised.value.message == sources.format(text)
    assert not (tmp_path / "run").exists()


def test_a_producer_replaced_by_a_failed_run_under_the_lease_fails_the_point(
    tmp_path, design, monkeypatch
):
    workspace = _workspace(tmp_path)
    shared = workspace.plan(_spec(design, 1)).nodes["yosys_fpga"].run_dir
    failed = dict(
        outcome="failed",
        error=ErrorInfo("nonzero_exit", "again", "yosys_fpga", 1, None),
    )
    armed: list[bool] = []
    swapped: list[str] = []

    def hook(path: Path) -> None:
        if path == _lock_of(shared) and armed and not swapped:
            swapped.append(_replace_record(shared, workspace, **failed))

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and event.node == "yosys_fpga":
            armed.append(True)  # the routing node takes its lease next
            monkeypatch.setattr(run_lock, "acquired_hook", hook)

    result = workspace.sweep([_spec(design, 1)], on_event=on_event)
    assert swapped
    (point,) = result
    assert point.result.outcome is Outcome.FAILED
    assert point.result.error is not None and point.result.error.code == "producer_failed"
    assert point.result.nodes["nextpnr"].outcome is not Outcome.BLOCKED
    assert result.counts() == {Outcome.FAILED: 1}


def test_a_delivery_never_runs_a_generator_again(tmp_path):
    from .test_generator_deliveries import Delivering

    world = Delivering(tmp_path, ["gen/top.v"])
    document = json.loads(world.file.read_text())
    document["rtl"]["generator"]["always_runs"] = True
    world.file.write_text(json.dumps(document))
    result = Workspace(world.run_root).sweep([Spec(world.flow.name, world.design())])
    assert result.counts() == {Outcome.PASSED: 1}
    assert world.runs == 1, "the generator ran for the node, and not again for the delivery"
    assert (world.root / "out" / "gen" / "top.v").is_file()


def test_a_root_replaced_by_a_failed_run_before_the_delivery_fails_the_point(tmp_path, design):
    destination = tmp_path / "out" / "top.v"
    workspace = _workspace(tmp_path)
    failed = dict(outcome="failed", error=ErrorInfo("nonzero_exit", "x", "nextpnr", 1, None))

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and event.node == "nextpnr":
            directory = workspace.plan(_spec(design, 1)).nodes["nextpnr"].run_dir
            _replace_record(directory, workspace, **failed)

    result = workspace.sweep([_edif_spec(design, 1, destination)], on_event=on_event)
    (point,) = result
    assert point.result.outcome is Outcome.FAILED
    assert point.result.error is not None and point.result.error.code == "delivery_failed"
    assert point.result.error.node is None and not destination.exists()


def test_a_cancel_while_the_sweep_plans_gives_canceled_points(
    tmp_path, remote, git_log  # noqa: F811
):
    cut_flows()
    token, events = CancelToken(), []
    token.cancel()
    specs = [_dependent(tmp_path, remote)]
    result = Workspace(tmp_path / "xeda_run").sweep(specs, cancel=token, on_event=events.append)
    assert result.canceled and len(result) == 1
    (point,) = result
    assert point.result.outcome is Outcome.CANCELED and point.result.error.code == "canceled"
    assert dict(point.result.nodes) == {} and point.result.roots == ()
    assert [e.kind for e in events] == ["launch_started", "launch_finished"]
    assert git_log() == [], "no Git command started"


def test_a_cancel_after_the_last_node_cancels_the_point_as_it_does_a_launch(tmp_path, design):
    spec = Spec("yosys_fpga", design, settings={"yosys_fpga.fpga.part": ECP5})

    def cancel_at_the_end(token: CancelToken):
        return lambda event: token.cancel() if event.kind == "node_finished" else None

    token = CancelToken()
    launched = _workspace(tmp_path / "a").run(spec, cancel=token, on_event=cancel_at_the_end(token))
    token = CancelToken()
    swept = _workspace(tmp_path / "b").sweep(
        [spec], cancel=token, on_event=cancel_at_the_end(token)
    )
    assert launched.outcome is Outcome.CANCELED
    (point,) = swept
    assert swept.canceled and point.result.outcome is launched.outcome
    assert point.result.error == launched.error


def test_an_exception_of_on_event_propagates_unchanged(tmp_path, design):
    class Boom(Exception):
        pass

    raised = Boom("stop")

    def on_event(event: Event) -> None:
        if event.kind == "node_finished":
            raise raised

    with pytest.raises(Boom) as caught:
        _workspace(tmp_path).sweep(_seeds(design, 2), on_event=on_event)
    assert caught.value is raised


def test_dotted_and_nested_updates_make_one_column(tmp_path, design):
    updated = (
        design.with_updates({"rtl.parameters.WIDTH": 8})
        .with_updates({"rtl": {"parameters": {"WIDTH": 16}}})
        .with_updates({"rtl.parameters.WIDTH": 32})
    )
    result = _workspace(tmp_path).sweep([replace(_spec(design, 1), design=updated)])
    (row,) = result.rows()
    assert updated.rtl.parameters["WIDTH"] == 32
    assert row["design.rtl.parameters.WIDTH"] == 32
    assert [key for key in row if key.startswith("design.")] == ["design.rtl.parameters.WIDTH"]


# ------------------------------------------------------------------------------- generation


def _generating(tmp_path: Path, monkeypatch, **recipe):
    """A design whose generator always runs, and the fake FPGA tools."""
    from .generation_world import World

    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    return World(
        tmp_path,
        generator={"always_runs": True, **recipe},
        extra="  clocks: [clk]\n",
    )


def _generated_keys(workspace: Workspace) -> set[str | None]:
    return {
        source.generation
        for record in workspace.records()
        for item in record.inputs
        for source in item.sources
        if source.generated
    }


def test_a_sweep_generates_once_and_every_node_and_the_delivery_read_that_generation(
    tmp_path, monkeypatch
):
    world = _generating(tmp_path, monkeypatch, outputs_to=str(tmp_path / "copy"))
    workspace = Workspace(world.run_root, hashed_run_dirs=True)
    specs = [_spec(world.load(), seed) for seed in (1, 2, 3)]
    result = workspace.sweep(specs)
    assert result.counts() == {Outcome.PASSED: 3}
    assert world.runs == 1, "an always-running generator runs once for the sweep"
    assert len(world.storages()) == 1
    assert _dispositions(result, "yosys_fpga") == ["ran", "shared", "shared"]
    assert len(_generated_keys(workspace) - {None}) == 1
    (copy,) = [p for p in (tmp_path / "copy").rglob("*.v")]
    assert copy.read_text() == (world.storages()[0] / "gen" / "top.v").read_text()


def test_points_with_different_generation_keys_each_generate_once(tmp_path, monkeypatch):
    world = _generating(tmp_path, monkeypatch)
    base = world.load()
    workspace = Workspace(world.run_root, hashed_run_dirs=True)
    specs = [
        _spec(base.with_updates({"rtl": {"parameters": {"W": width}}}), seed)
        for width in (1, 2)
        for seed in (1, 2)
    ]
    result = workspace.sweep(specs)
    assert result.counts() == {Outcome.PASSED: 4}
    assert len(world.storages()) == 2 and world.runs == 2


@pytest.mark.xfail(
    strict=True,
    reason="the node identity holds no part of the generator recipe: points whose generators "
    "differ only in their arguments are one node, so the sweep shares the first generation's "
    "result (or fails with StalePlanError, or refuses equal_specs)",
)
@pytest.mark.parametrize(
    "case",
    [
        "seeds",  # one name, other seeds: the sweep shares a node that read the other generation
        "names",  # other names: the shared node runs in the other point's run directory
        "args_only",  # nothing else varies: the sweep refuses the points as one run
    ],
)
def test_points_whose_generators_differ_only_in_their_arguments_are_other_nodes(
    tmp_path, monkeypatch, case
):
    world = _generating(tmp_path, monkeypatch)
    base = world.load()
    workspace = Workspace(world.run_root, hashed_run_dirs=True)
    specs = []
    for index, width in enumerate(("8", "16")):
        updates: dict = {"rtl.generator.args": ["$DESIGN_ROOT/gen.py", str(world.counter), width]}
        if case == "names":
            updates["name"] = f"counter{width}"
        specs.append(_spec(base.with_updates(updates), index + 1 if case == "seeds" else 1))
    result = workspace.sweep(specs)
    assert result.counts() == {Outcome.PASSED: 2}
    assert _dispositions(result, "yosys_fpga") == ["ran", "ran"]
    read = [
        {
            source.generation
            for item in point.result.nodes["yosys_fpga"].record.inputs
            for source in item.sources
            if source.generated
        }
        for point in result
    ]
    assert len(read[0]) == len(read[1]) == 1 and read[0] != read[1], "each point reads its own"


def _another_generation(storage: Path) -> None:
    """Replace the generation in `storage` with another one of the same key, as a launch that ran
    the generator again would: other files, and a record that vouches for them."""
    import yaml

    from xeda.digest import content_digest

    record = storage / "generation.yaml"
    data = yaml.safe_load(record.read_text())
    for name, _digest in data["outputs"]:
        (storage / name).write_text("// the other generation\n")
    data["outputs"] = [[name, content_digest(storage / name)] for name, _ in data["outputs"]]
    record.write_text(yaml.safe_dump(data, sort_keys=True))


@pytest.mark.parametrize("after", ["yosys_fpga", "nextpnr"])
def test_a_generation_that_another_launch_replaced_is_never_read_or_delivered(
    tmp_path, monkeypatch, after
):
    copy = tmp_path / "copy"
    world = _generating(tmp_path, monkeypatch, outputs_to=str(copy))
    workspace = Workspace(world.run_root, hashed_run_dirs=True)
    swapped: list[str] = []

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and event.node == after and not swapped:
            swapped.append(after)
            _another_generation(world.storages()[0])

    result = workspace.sweep([_spec(world.load(), 1)], on_event=on_event)
    (point,) = result
    assert swapped and point.result.outcome is Outcome.FAILED
    assert point.result.error is not None and point.result.error.node is None
    if after == "yosys_fpga":
        # The routing node found another generation under its lease.
        assert point.result.error.code == "generator_failed"
        assert point.result.nodes["nextpnr"].outcome is Outcome.SKIPPED
        assert "sweep generated" in point.result.error.message
    else:
        assert point.result.error.code == "delivery_failed"
        assert {str(n.outcome) for n in point.result.nodes.values()} == {"passed"}
    assert not copy.exists() or not any(copy.rglob("*")), "nothing of the other generation"
    assert world.runs == 1


def test_cancel_at_the_start_event_runs_no_generator(tmp_path, monkeypatch):
    world = _generating(tmp_path, monkeypatch)
    token = CancelToken()

    def on_event(event: Event) -> None:
        if event.kind == "launch_started":
            token.cancel()

    result = Workspace(world.run_root, hashed_run_dirs=True).sweep(
        _seeds(world.load(), 2), cancel=token, on_event=on_event
    )
    assert world.runs == 0 and result.canceled
    assert {str(p.result.outcome) for p in result} == {"canceled"}
    assert {str(n.outcome) for p in result for n in p.result.nodes.values()} == {"skipped"}


def test_an_exception_at_the_start_event_runs_no_generator(tmp_path, monkeypatch):
    world = _generating(tmp_path, monkeypatch)
    raised = RuntimeError("stop at the start")

    def on_event(event: Event) -> None:
        if event.kind == "launch_started":
            raise raised

    with pytest.raises(RuntimeError) as caught:
        Workspace(world.run_root, hashed_run_dirs=True).sweep(
            _seeds(world.load(), 2), on_event=on_event
        )
    assert caught.value is raised and world.runs == 0


def test_a_node_task_applies_the_run_root_rule_with_the_start_directory_of_the_workspace(
    tmp_path, design, monkeypatch
):
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    root = tmp_path / "elsewhere" / "xeda_run"
    workspace = Workspace(root)  # absent now: the rule accepts it
    spec = _spec(design, 1)
    task = _task(workspace, spec, "yosys_fpga", [])
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("not Xeda's\n")  # unmarked, not empty, not the default
    # The rule of a workspace, which a task carries to the process that executes it.
    with pytest.raises(Exception) as run_error:
        workspace.run(spec)
    assert run_error.value.code == "invalid_run_root"
    with pytest.raises(Exception) as node_error:
        run_node(task, canceled=lambda: False, emit=lambda event: None)
    assert node_error.value.code == "invalid_run_root"
    assert not (root / ".xeda-run-root").exists(), "the node must not adopt the directory"


def test_a_consumer_vouches_for_the_unchanged_outputs_of_its_producer_by_metadata(
    tmp_path, design, monkeypatch
):
    from xeda import digest

    monkeypatch.setattr(digest, "RACY_NS", 0)  # a file of this test is settled at once
    workspace = _workspace(tmp_path)
    spec = _spec(design, 1)
    first = workspace.run(spec)
    done = _completed(first)
    recorded = previous_trace(done.run_dir, "yosys_fpga")
    assert recorded is not None and recorded.outputs
    outputs = {Path(name) for name in recorded.outputs}
    read: list[Path] = []
    real, capture = digest.content_digest, run_lock.CompletedRun.capture
    capturing: list[bool] = []

    def counting(path: Path) -> str:
        if capturing:
            read.append(Path(path))
        return real(path)

    def counted_capture(cls, *args, **kwargs):
        capturing.append(True)
        try:
            return capture.__func__(cls, *args, **kwargs)
        finally:
            capturing.pop()

    monkeypatch.setattr(digest, "content_digest", counting)
    monkeypatch.setattr(run_lock.CompletedRun, "capture", classmethod(counted_capture))
    task = _task(workspace, spec, "nextpnr", [done])
    outcome = run_node(task, canceled=lambda: False, emit=lambda event: None)
    assert outcome.result.outcome is Outcome.PASSED
    assert read, "the capture of the producer reads the files that nothing vouches for"
    assert not outputs & set(read), "an unchanged output is read in full again"
