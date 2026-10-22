"""A launch reports an outcome for every planned node, and a verdict for the whole launch.

A node that fails, is rejected or is blocked is a value in the `Result`, not an exception. A node
that a failed producer blocks does not run and writes nothing: its run directory keeps its last
record and trace.
"""

from __future__ import annotations

import dataclasses
import pickle
from pathlib import Path
from typing import Any, ClassVar

import pytest

import xeda.deliver
from xeda import Design
from xeda.dataclass import Field, deliverable
from xeda.design import SourceType
from xeda.errors import RecordError, RunFailedError
from xeda.flow import Flow, FlowSettingsError, In, MetricSpec, Out, registered_flows
from xeda.flow_runner import DefaultRunner, launch_report
from xeda.flow_runner.launch_result import launch_result
from xeda.frozen import thaw
from xeda.outcomes import DeliveryRecord, Disposition, NodeResult, Outcome, Result, aggregate
from xeda.records import ErrorInfo, decode_record, read_record
from xeda.run_dir import RunDirectory
from xeda.spec import Spec
from xeda.utils import ExecutableNotFound, replacing_file

from .flow_registry import preserved_flow_registry
from .record_documents import record_document
from .test_converted_designs import Events, consumer_flows, write_design
from .test_failure_results import _MayFail, _NeedsMayFail, producer_registered  # noqa: F401

pytestmark = pytest.mark.python_compat

MAY_FAIL = _MayFail.name


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _flows() -> dict[str, Any]:
    """Define the test flows that the failure tests do not have, and return them by name."""

    class _Rejects(Flow):
        """Passes its run and rejects the results."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            pass

        def check_results(self) -> bool:
            self.reject("the results are not good enough")
            return super().check_results()

    class _Quiet(Flow):
        """Writes a note, which another flow reads."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(Flow.Outputs):
            note: Path = Out(SourceType.Data, description="The note it writes.")

        def run(self) -> None:
            note = self.run_path / "note.txt"
            self.run_directory.writable(note).write_text("note\n")
            self.outputs.note = note

    class _NeedsBoth(Flow):
        """Reads the report of `_MayFail` first and the note of `_Quiet` second."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            report: Path = In(
                SourceType.Data,
                producer=MAY_FAIL,
                output="report",
                description="The report.",
            )
            note: Path = In(
                SourceType.Data, producer=_Quiet.name, output="note", description="The note."
            )

        def run(self) -> None:
            pass

    class _Interrupts(Flow):
        """Is interrupted by the user while it runs."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            raise KeyboardInterrupt

    class _Missing(Flow):
        """Starts a program that is not installed."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            raise ExecutableNotFound("xeda-no-such-program-for-this-test")

    class _Deliverer(Flow):
        """Writes the file its setting names, which a launch delivers where the setting says."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            netlist: Path | None = Field(
                None,
                description="The file it writes.",
                json_schema_extra=deliverable("outputs/{design}.v"),
            )

        class Outputs(Flow.Outputs):
            netlist: Path = Out(SourceType.Data, description="The file it writes.")

        def run(self) -> None:
            assert self.settings.netlist is not None
            netlist = Path(self.settings.netlist)
            netlist.parent.mkdir(parents=True, exist_ok=True)
            netlist.write_text("net\n")
            self.artifacts["netlist"] = str(netlist)
            self.outputs.netlist = netlist

    return {
        cls.__name__: cls
        for cls in (_Rejects, _Quiet, _NeedsBoth, _Interrupts, _Missing, _Deliverer)
    }


@pytest.fixture
def flows(producer_registered):  # noqa: F811
    """The failure flows and the flows above, registered for one test."""
    found = _flows()
    yield found
    for cls in found.values():
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


def _design(tmp_path: Path) -> Design:
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    return Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})


def _runner(tmp_path: Path, **options: Any) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False, **options)


def _launch(
    runner: DefaultRunner,
    flow: type[Flow],
    design: Design,
    sections: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> Result:
    """Launch `flow` on `runner` and return what the launch did."""
    spec = Spec(flow.name, design, settings=sections or {})
    return launch_result(
        runner,
        spec,
        lambda: runner.run_flow(flow, design, settings or {}, all_flows_settings=sections),
    )


@pytest.mark.usefixtures("flows")
def test_a_failed_producer_blocks_its_consumer_and_is_the_cause(tmp_path):
    result = _launch(
        _runner(tmp_path), _NeedsMayFail, _design(tmp_path), {MAY_FAIL: {"fail": True}}
    )
    assert result.outcome == "failed" and not result.passed
    assert result.error is not None and result.error.node == MAY_FAIL
    assert result.error.code == "reported_failure" and result.error.message == "boom"
    assert list(result.nodes) == [MAY_FAIL, _NeedsMayFail.name]
    producer = result.nodes[MAY_FAIL]
    assert (producer.outcome, producer.disposition) == ("failed", "ran")
    assert producer.record is not None and producer.record.outcome == "failed"
    assert producer.elapsed > 0
    consumer = result.nodes[_NeedsMayFail.name]
    assert (consumer.outcome, consumer.disposition) == ("blocked", "not_run")
    assert consumer.blocked_by == MAY_FAIL and consumer.record is None and consumer.elapsed == 0.0
    assert consumer.error is not None and consumer.error.code == "producer_failed"
    # the consumer's error names the producer, its code and its message
    assert f"producer {MAY_FAIL} failed (reported_failure): boom" in consumer.error.message
    with pytest.raises(RunFailedError) as raised:
        result.require_passed()
    assert raised.value.result is result and raised.value.code == "run_failed"


@pytest.mark.usefixtures("flows")
def test_a_blocked_consumer_keeps_its_earlier_record(tmp_path):
    design, runner = _design(tmp_path), _runner(tmp_path)
    first = _launch(runner, _NeedsMayFail, design)
    assert first.passed and first.require_passed() is first
    consumer = first.nodes[_NeedsMayFail.name]
    earlier = read_record(consumer.run_dir, run_root=runner.run_root)
    assert (consumer.run_dir / "trace.json").exists()

    second = _launch(runner, _NeedsMayFail, design, {MAY_FAIL: {"fail": True}})
    blocked = second.nodes[_NeedsMayFail.name]
    assert blocked.outcome == "blocked" and blocked.record is None
    kept = read_record(consumer.run_dir, run_root=runner.run_root)
    assert kept.run_id == earlier.run_id and kept.outcome == "passed"
    assert (consumer.run_dir / "trace.json").exists()


def test_a_rejected_root_aggregates_rejected_with_its_error(tmp_path, flows):
    rejects = flows["_Rejects"]
    design, runner = _design(tmp_path), _runner(tmp_path)
    result = _launch(runner, rejects, design)
    assert result.outcome == "rejected" and not result.passed
    assert result.error is not None and result.error.code == "rejected"
    assert result.error.node == rejects.name
    assert result.error.message.endswith("the results are not good enough")
    node = result.nodes[rejects.name]
    assert (node.outcome, node.disposition) == ("rejected", "ran")
    assert node.record is not None and node.record.outcome == "rejected"
    assert result.record is node.record

    # reported again from the trace: still rejected, reused, with its error unchanged
    again = _launch(runner, rejects, design)
    reused = again.nodes[rejects.name]
    assert (reused.outcome, reused.disposition, reused.elapsed) == ("rejected", "reused", 0.0)
    assert again.outcome == "rejected" and again.error == result.error


def test_a_sibling_not_reached_is_skipped_without_an_error(tmp_path, flows):
    result = _launch(
        _runner(tmp_path),
        flows["_NeedsBoth"],
        _design(tmp_path),
        {MAY_FAIL: {"fail": True}},
    )
    quiet = flows["_Quiet"].name
    assert result.outcome == "failed" and result.error is not None
    assert result.error.node == MAY_FAIL
    assert set(result.nodes) == {MAY_FAIL, quiet, flows["_NeedsBoth"].name}
    skipped = result.nodes[quiet]
    assert (skipped.outcome, skipped.disposition) == ("skipped", "not_run")
    assert skipped.error is None and skipped.record is None and skipped.blocked_by is None
    assert result.nodes[flows["_NeedsBoth"].name].outcome == "blocked"
    assert not (skipped.run_dir / "results.json").exists()


@pytest.mark.usefixtures("flows")
def test_a_reused_node_has_disposition_reused_and_zero_elapsed(tmp_path):
    design, runner = _design(tmp_path), _runner(tmp_path)
    first = _launch(runner, _NeedsMayFail, design)
    assert {n.disposition for n in first.nodes.values()} == {Disposition.RAN}
    assert all(n.elapsed > 0 for n in first.nodes.values())
    second = _launch(runner, _NeedsMayFail, design)
    assert second.passed
    for node in second.nodes.values():
        assert (node.outcome, node.disposition, node.elapsed) == ("passed", "reused", 0.0)
        assert node.record is not None
        assert node.record.run_id == first.nodes[node.name].record.run_id


@pytest.mark.parametrize("stage", ["before the run", "when the record is written"])
def test_a_storage_failure_is_a_failed_node_without_a_record(tmp_path, monkeypatch, stage):
    """A read-only run directory or a full disk: the launch returns, and the node says why."""
    if stage == "before the run":
        real = RunDirectory.writable

        def refuse(self, path):
            if Path(path).name == "results.json":
                raise PermissionError(13, "Permission denied", str(path))
            return real(self, path)

        monkeypatch.setattr(RunDirectory, "writable", refuse)
    else:
        real_dump = launch_report.dump_json

        def full(data, path, *args, **kwargs):
            if Path(path).name == "results.json":
                raise OSError(28, "No space left on device", str(path))
            return real_dump(data, path, *args, **kwargs)

        monkeypatch.setattr(launch_report, "dump_json", full)
    result = _launch(_runner(tmp_path), _MayFail, _design(tmp_path))
    node = result.nodes[MAY_FAIL]
    assert (node.outcome, node.disposition) == ("failed", "ran")
    assert node.record is None
    assert node.error is not None and node.error.code == "storage_error"
    assert node.error.node == MAY_FAIL
    assert result.outcome == "failed" and result.error == node.error
    with pytest.raises(RecordError) as raised:
        result.record  # noqa: B018
    assert raised.value.code == "missing_record"


@pytest.mark.parametrize("stage", ["during the run", "while the record is written"])
def test_an_interrupt_leaves_no_partial_record(tmp_path, monkeypatch, flows, stage):
    runner, design = _runner(tmp_path), _design(tmp_path)
    flow = flows["_Interrupts"] if stage == "during the run" else _MayFail
    if stage != "during the run":
        real_dump = launch_report.dump_json

        def interrupted(data, path, *args, **kwargs):
            if Path(path).name != "results.json":
                return real_dump(data, path, *args, **kwargs)
            with replacing_file(path) as partial:
                partial.write('{"schema_ver')
                raise KeyboardInterrupt

        monkeypatch.setattr(launch_report, "dump_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _launch(runner, flow, design)
    (node,) = runner.collector.plan.nodes[-1:]
    with pytest.raises(RecordError) as raised:
        read_record(node.run_path, run_root=runner.run_root)
    assert raised.value.code == "missing_record"
    assert not list(node.run_path.glob(".results.json*"))


def test_a_missing_executable_fails_the_node_with_its_code(tmp_path, flows):
    missing = flows["_Missing"]
    result = _launch(_runner(tmp_path), missing, _design(tmp_path))
    node = result.nodes[missing.name]
    assert node.outcome == "failed" and node.record is not None
    assert node.error is not None and node.error.code == "executable_not_found"
    assert result.error == node.error == node.record.error


@pytest.mark.usefixtures("flows")
def test_result_record_raises_for_a_root_without_a_record(tmp_path):
    result = _launch(
        _runner(tmp_path), _NeedsMayFail, _design(tmp_path), {MAY_FAIL: {"fail": True}}
    )
    consumer = result.nodes[_NeedsMayFail.name]
    assert consumer.record is None
    with pytest.raises(RecordError) as raised:
        result.record  # noqa: B018
    assert raised.value.code == "missing_record"
    assert raised.value.path == consumer.run_dir / "results.json"
    passing = _launch(_runner(tmp_path / "other"), _MayFail, _design(tmp_path))
    assert passing.record is passing.nodes[MAY_FAIL].record


@pytest.mark.usefixtures("flows")
def test_values_are_deeply_frozen_and_picklable(tmp_path):
    design = _design(tmp_path)
    settings: dict[str, Any] = {
        "nextpnr": {"seed": 7, "clocks": {"main_clock": {"period": "10 ns"}}, "list": [1, [2]]}
    }
    spec = Spec("nextpnr", design, settings=settings)
    settings["nextpnr"]["seed"] = 8
    settings["nextpnr"]["list"].append(3)
    assert spec.settings["nextpnr"]["seed"] == 7
    assert spec.settings["nextpnr"]["list"] == (1, (2,))
    with pytest.raises(TypeError):
        spec.settings["nextpnr"]["seed"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.settings["other"] = {}  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.flow = "other"  # type: ignore[misc]
    assert thaw(spec.settings) == {
        "nextpnr": {"seed": 7, "clocks": {"main_clock": {"period": "10 ns"}}, "list": [1, [2]]}
    }
    assert pickle.loads(pickle.dumps(spec)) == spec

    result = _launch(_runner(tmp_path / "work"), _NeedsMayFail, design)
    assert pickle.loads(pickle.dumps(result)) == result
    with pytest.raises(TypeError):
        result.nodes["x"] = result.nodes[MAY_FAIL]  # type: ignore[index]
    with pytest.raises(TypeError):
        result.record.metrics["count"] = 0  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.nodes[MAY_FAIL].outcome = Outcome.FAILED  # type: ignore[misc]


def test_a_record_does_not_alias_the_data_it_was_built_from(tmp_path):
    document = record_document("passed", metrics={"count": 1})
    base = decode_record(document, run_dir=tmp_path, run_root=tmp_path)
    metrics: dict[str, Any] = {"count": 1, "nested": {"values": [1, 2]}}
    artifacts = {"log": "log.txt"}
    settings: dict[str, Any] = {"seed": [1, {"deep": 2}]}
    record = dataclasses.replace(
        base, metrics=metrics, artifacts=artifacts, settings=settings, outcome="passed"
    )
    metrics["count"] = 2
    metrics["nested"]["values"].append(3)
    artifacts["other"] = "other.txt"
    settings["seed"][1]["deep"] = 3
    assert record.metrics["count"] == 1 and record.metrics["nested"]["values"] == (1, 2)
    assert dict(record.artifacts) == {"log": "log.txt"}
    assert thaw(record.settings) == {"seed": [1, {"deep": 2}]}
    assert record.outcome is Outcome.PASSED and record.to_dict()["outcome"] == "passed"


def _node(
    name: str,
    outcome: str,
    *,
    blocked_by: str | None = None,
    code: str | None = None,
    record: Any = None,
) -> NodeResult:
    ran = outcome in ("passed", "rejected", "failed", "canceled")
    error = None
    if outcome not in ("passed", "skipped"):
        error = ErrorInfo(code or outcome, f"{name} {outcome}", name, None, None)
    return NodeResult(
        name=name,
        outcome=outcome,  # type: ignore[arg-type]
        disposition="ran" if ran else "not_run",  # type: ignore[arg-type]
        reason="",
        blocked_by=blocked_by,
        elapsed=0.0,
        record=record,
        error=error,
        deliveries=[],  # type: ignore[arg-type]
        run_dir=Path(name),
    )


def test_a_result_does_not_alias_the_data_it_was_built_from(tmp_path):
    design = _design(tmp_path)
    nodes = {"a": _node("a", "passed")}
    roots = ["a"]
    result = Result("id", Spec("a", design), roots, "passed", None, nodes)  # type: ignore[arg-type]
    nodes["b"] = _node("b", "failed")
    roots.append("b")
    assert list(result.nodes) == ["a"] and result.roots == ("a",)
    with pytest.raises(TypeError):
        result.nodes["c"] = nodes["b"]  # type: ignore[index]
    assert pickle.loads(pickle.dumps(result)) == result


def _nodes(*nodes: NodeResult) -> dict[str, NodeResult]:
    return {node.name: node for node in nodes}


LAUNCH_ERROR = ErrorInfo("delivery_failed", "disk full", None, None, None)


def test_the_aggregate_follows_the_rules_in_order():
    passed, failed, rejected = _node("p", "passed"), _node("f", "failed"), _node("r", "rejected")
    skipped = _node("s", "skipped")

    # a passed root
    assert aggregate(_nodes(passed), ["p"], None) == ("passed", None)
    # a root that failed or was rejected has its own error
    assert aggregate(_nodes(failed), ["f"], None) == ("failed", failed.error)
    assert aggregate(_nodes(rejected), ["r"], None) == ("rejected", rejected.error)
    # a root that was never reached is skipped and has no error
    assert aggregate(_nodes(passed, skipped), ["s"], None) == ("skipped", None)
    # a blocked root: the cause is the node that failed, followed down the chain
    chain = _nodes(
        failed,
        _node("b", "blocked", blocked_by="f", code="producer_failed"),
        _node("c", "blocked", blocked_by="b", code="producer_failed"),
    )
    assert aggregate(chain, ["c"], None) == ("failed", failed.error)
    # a blocked root whose cause was rejected is rejected, with the error of the rejection
    behind = _nodes(rejected, _node("b", "blocked", blocked_by="r", code="producer_failed"))
    assert aggregate(behind, ["b"], None) == ("rejected", rejected.error)
    # a launch-level failure comes before every node
    assert aggregate(_nodes(passed), ["p"], LAUNCH_ERROR) == ("failed", LAUNCH_ERROR)
    assert aggregate(_nodes(failed), ["f"], LAUNCH_ERROR) == ("failed", LAUNCH_ERROR)
    # a canceled node, or the launch-level `canceled`, makes the aggregate canceled
    canceled = _node("c", "canceled")
    assert aggregate(_nodes(passed, canceled), ["p"], None) == ("canceled", canceled.error)
    stopped = ErrorInfo("canceled", "canceled", None, None, None)
    assert aggregate(_nodes(skipped), ["s"], stopped) == ("canceled", stopped)
    assert aggregate(_nodes(failed, canceled), ["f"], stopped) == ("canceled", stopped)
    # a detached node comes first of all
    detached = _node("d", "detached")
    note = ErrorInfo("remote_detached", "left running", None, None, None)
    assert aggregate(_nodes(canceled, detached), ["d"], note) == ("detached", note)
    assert aggregate(_nodes(failed, detached), ["f"], None) == ("detached", detached.error)


def test_the_worst_root_decides_the_aggregate():
    passed, failed, rejected = _node("p", "passed"), _node("f", "failed"), _node("r", "rejected")
    skipped = _node("s", "skipped")
    every = _nodes(passed, failed, rejected, skipped)
    assert aggregate(every, ["p", "s"], None)[0] == "skipped"
    assert aggregate(every, ["p", "r", "s"], None) == ("rejected", rejected.error)
    assert aggregate(every, ["r", "f", "p"], None) == ("failed", failed.error)
    other = _node("g", "failed")
    assert aggregate(_nodes(failed, other), ["g", "f"], None) == ("failed", other.error)


def test_a_blocked_node_that_nothing_blocks_it_is_an_error():
    lost = _nodes(_node("b", "blocked", blocked_by="missing"))
    with pytest.raises(ValueError, match="no node that blocks it"):
        aggregate(lost, ["b"], None)


@pytest.mark.parametrize("producer_kind", ["output", "design converter"])
@pytest.mark.usefixtures("flows")
def test_clean_does_not_remove_a_consumer_that_ends_up_blocked(
    tmp_path, monkeypatch, producer_kind
):
    """`clean` empties the run directory of a node only when the node is about to run. A failed
    producer blocks its consumer first, so the consumer keeps its record and trace."""
    if producer_kind == "output":
        design, root = _design(tmp_path), _NeedsMayFail
        sections: dict[str, Any] | None = None
        broken = {MAY_FAIL: {"fail": True}}

        def break_producer() -> None:
            pass

    else:
        events = Events()
        converter, root = consumer_flows(events)
        design = Design.from_file(
            write_design(tmp_path / "d", flows={root.name: {"inputs": {"design": converter.name}}})
        )
        sections, broken = None, None
        working = converter.run

        def fails(self) -> None:
            raise RuntimeError("the converter is out of order")

        def break_producer() -> None:
            monkeypatch.setattr(converter, "run", fails)

        def mend_producer() -> None:
            monkeypatch.setattr(converter, "run", working)

    first = _launch(_runner(tmp_path), root, design, sections)
    assert first.passed
    consumer_dir = first.nodes[root.name].run_dir
    earlier = read_record(consumer_dir, run_root=tmp_path / "run")
    kept = {p.name for p in consumer_dir.iterdir()}
    assert "trace.json" in kept

    break_producer()
    clean = _runner(tmp_path, clean=True)
    second = _launch(clean, root, design, broken)
    assert second.outcome == "failed"
    blocked = second.nodes[root.name]
    assert (blocked.outcome, blocked.record) == ("blocked", None)
    assert {p.name for p in consumer_dir.iterdir()} == kept
    assert read_record(consumer_dir, run_root=tmp_path / "run").run_id == earlier.run_id

    # with its producers well, the same clean launch empties the directory and runs the node
    if producer_kind == "design converter":
        mend_producer()
    third = _launch(_runner(tmp_path, clean=True), root, design, sections)
    assert third.passed and third.nodes[root.name].disposition == "ran"
    assert read_record(consumer_dir, run_root=tmp_path / "run").run_id != earlier.run_id


def test_a_failed_delivery_is_a_launch_error_and_the_node_records_stay(
    tmp_path, monkeypatch, flows
):
    deliverer = flows["_Deliverer"]
    design, runner = _design(tmp_path), _runner(tmp_path)
    destination = tmp_path / "user" / "net.v"
    sections = {deliverer.name: {"netlist": str(destination)}}

    copy = xeda.deliver.copy_fd

    def disk_full(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(xeda.deliver, "copy_fd", disk_full)
    result = _launch(runner, deliverer, design, sections)
    assert result.outcome == "failed" and not result.passed
    assert result.error is not None and result.error.code == "delivery_failed"
    assert result.error.node is None and "No space left" in result.error.message
    node = result.nodes[deliverer.name]
    assert (node.outcome, node.disposition) == ("passed", "ran")
    assert node.record is not None and node.record.outcome == "passed"
    assert node.deliveries == (DeliveryRecord("netlist", destination, False),)
    assert read_record(node.run_dir, run_root=runner.run_root).run_id == node.record.run_id
    assert not destination.exists()

    monkeypatch.setattr(xeda.deliver, "copy_fd", copy)
    delivered = _launch(_runner(tmp_path), deliverer, design, sections)
    assert delivered.passed and destination.read_text() == "net\n"
    assert delivered.nodes[deliverer.name].deliveries == (
        DeliveryRecord("netlist", destination, True),
    )


def test_a_failure_before_any_plan_is_raised_not_reported(tmp_path):
    """A request that is refused before the plan exists has no nodes: it is an exception."""
    runner = _runner(tmp_path)
    with pytest.raises(FlowSettingsError, match="no_such_setting"):
        _launch(runner, _MayFail, _design(tmp_path), None, {"no_such_setting": 1})
    assert not list((tmp_path / "run").glob("**/results.json"))
