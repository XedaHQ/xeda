"""A launch reports an outcome for every planned node, and a verdict for the whole launch.

A node that fails, is rejected or is blocked is a value in the `Result`, not an exception. A node
that a failed producer blocks does not run and writes nothing: its run directory keeps its last
record and trace.
"""

from __future__ import annotations

import dataclasses
import json
import os
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
from xeda.deliver import DeliveryError
from xeda.flow_runner import DefaultRunner, launch_identity, launch_report
from xeda.flow_runner import default_runner as default_runner_module
from xeda.flow_runner.launch_result import failed_result, launch_result
from xeda.frozen import thaw
from xeda.outcomes import (
    DeliveryRecord,
    Disposition,
    NodeResult,
    Outcome,
    Result,
    aggregate,
    result_from_dict,
)
from xeda.records import ErrorInfo, decode_record, read_record
from xeda.run_dir import RunDirectory, RunDirectoryError
from xeda.spec import Spec
from xeda.utils import ExecutableNotFound, replacing_file

from .flow_registry import preserved_flow_registry
from .record_documents import record_document
from .test_converted_designs import Events, consumer_flows, write_design
from .test_failure_results import _MayFail, _NeedsMayFail, producer_registered  # noqa: F401

pytestmark = pytest.mark.python_compat

MAY_FAIL = _MayFail.name


#: The clock of the collector in the timing test: `_Slow` advances it, not the wall clock.
_CLOCK = [0.0]


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

    class _Slow(Flow):
        """Takes a while to write a note, which another flow reads."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(Flow.Outputs):
            note: Path = Out(SourceType.Data, description="The note it writes.")

        def run(self) -> None:
            _CLOCK[0] += 100.0  # a long run, on the clock of the timing test
            note = self.run_path / "note.txt"
            self.run_directory.writable(note).write_text("note\n")
            self.outputs.note = note

    class _AfterSlow(Flow):
        """Reads the note of `_Slow`, and does nothing else."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            note: Path = In(
                SourceType.Data, producer=_Slow.name, output="note", description="The note."
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
            netlist = self.run_file(self.settings.netlist)
            netlist.parent.mkdir(parents=True, exist_ok=True)
            netlist.write_text("net\n")
            self.artifacts["netlist"] = str(netlist)
            self.outputs.netlist = netlist

    return {
        cls.__name__: cls
        for cls in (
            _Rejects,
            _Quiet,
            _NeedsBoth,
            _Slow,
            _AfterSlow,
            _Interrupts,
            _Missing,
            _Deliverer,
        )
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
    result = launch_result(
        runner,
        spec,
        lambda: runner.run_flow(flow, design, settings or {}, all_flows_settings=sections),
    )
    _skipped_means_never_attempted(runner, result)
    return result


def _skipped_means_never_attempted(runner: DefaultRunner, result: Result) -> None:
    """Check, for every launch of these tests, that a node is `skipped` only if the launch never
    acted on it: it reported no end for it, and built no flow in its run directory."""
    attempted = set(runner.collector.ended)
    built = {os.path.abspath(f.run_path) for f in runner.launched[runner._launched_before :]}
    for node in result.nodes.values():
        if node.outcome is Outcome.SKIPPED:
            assert node.name not in attempted, f"`{node.name}` was attempted and is skipped"
            assert os.path.abspath(node.run_dir) not in built, f"`{node.name}` has a flow"


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
    design.rtl.top = "changed"  # a caller's later change of the design does not reach the spec
    assert spec.design.rtl.top == "top"
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
        flow=name,
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


def _fail_removal(monkeypatch, purge: bool, failure: Exception) -> None:
    """Make the removal that the clean-up after the run makes raise `failure`: the deletion of the
    run directory (`purge`), or the pruning of its files. The run itself removes files too, so the
    removal fails only inside the clean-up."""
    real = DefaultRunner._clean_up

    def refuse(self, *args, **kwargs):
        raise failure

    def clean_up(self, *args, **kwargs):
        with monkeypatch.context() as inside:
            inside.setattr(RunDirectory, "delete" if purge else "remove", refuse)
            return real(self, *args, **kwargs)

    monkeypatch.setattr(DefaultRunner, "_clean_up", clean_up)


@pytest.mark.parametrize(
    "purge, failure",
    [
        (False, OSError(5, "Input/output error")),
        (True, OSError(13, "Permission denied")),
        (True, RunDirectoryError("refused for the test")),
    ],
    ids=["pruning", "deleting", "a refusal of the run directory"],
)
def test_a_failed_clean_up_after_a_passed_root_is_a_failed_launch(
    tmp_path, monkeypatch, flows, purge, failure
):
    _fail_removal(monkeypatch, purge, failure)
    runner = _runner(tmp_path, post_cleanup=True, post_cleanup_purge=purge)
    result = _launch(runner, _MayFail, _design(tmp_path))
    node = result.nodes[MAY_FAIL]
    assert result.outcome == "failed" and not result.passed
    assert result.error is not None and result.error.code == "cleanup_failed"
    assert result.error.node is None
    assert str(node.run_dir) in result.error.message and str(failure) in result.error.message
    # the node passed, and its outcome and record stay as they were
    assert (node.outcome, node.disposition) == ("passed", "ran")
    assert node.record is not None and node.record.outcome == "passed" and node.error is None


def test_a_clean_up_that_fails_in_another_way_is_a_failed_launch_too(tmp_path, monkeypatch, flows):
    def refuse(self, *args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(DefaultRunner, "_clean_up", refuse)
    result = _launch(_runner(tmp_path, post_cleanup=True), _MayFail, _design(tmp_path))
    node = result.nodes[MAY_FAIL]
    assert result.error is not None and result.error.code == "cleanup_failed"
    assert str(node.run_dir) in result.error.message and "bug" in result.error.message
    assert node.outcome == "passed"


@pytest.mark.parametrize("purge", [False, True], ids=["pruning", "deleting"])
def test_a_failed_clean_up_never_replaces_the_error_of_a_root_that_did_not_pass(
    tmp_path, monkeypatch, flows, purge
):
    _fail_removal(monkeypatch, purge, OSError(5, "Input/output error"))
    runner = _runner(tmp_path, post_cleanup=True, post_cleanup_purge=purge)
    result = _launch(runner, _MayFail, _design(tmp_path), {MAY_FAIL: {"report_failure": True}})
    node = result.nodes[MAY_FAIL]
    assert node.outcome == "failed" and node.error is not None
    assert node.error.code == "reported_failure"
    assert result.outcome == "failed" and result.error == node.error


def test_a_failed_delivery_keeps_its_error_when_the_clean_up_fails_too(
    tmp_path, monkeypatch, flows
):
    deliverer = flows["_Deliverer"]
    destination = tmp_path / "user" / "net.v"

    def disk_full(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(xeda.deliver, "copy_fd", disk_full)
    _fail_removal(monkeypatch, False, OSError(5, "Input/output error"))
    runner = _runner(tmp_path, post_cleanup=True)
    result = _launch(
        runner, deliverer, _design(tmp_path), {deliverer.name: {"netlist": str(destination)}}
    )
    assert result.error is not None and result.error.code == "delivery_failed"
    assert "No space left" in result.error.message


def _refuse_identity_of(monkeypatch, name, failure):
    """Make the identity stage fail for the node `name`, before it has a flow."""
    real = launch_identity.run_identity

    def refuse(self, flow_class, flow_name, *args, **kwargs):
        if flow_name == name:
            raise failure
        return real(self, flow_class, flow_name, *args, **kwargs)

    monkeypatch.setattr(launch_identity, "run_identity", refuse)


@pytest.mark.parametrize("failing", ["producer", "root"])
@pytest.mark.usefixtures("flows")
def test_a_node_that_fails_before_it_has_a_flow_is_failed_not_skipped(
    tmp_path, monkeypatch, failing
):
    name = MAY_FAIL if failing == "producer" else _NeedsMayFail.name
    _refuse_identity_of(monkeypatch, name, RunDirectoryError("refused for the test"))
    result = _launch(_runner(tmp_path), _NeedsMayFail, _design(tmp_path))
    node = result.nodes[name]
    assert (node.outcome, node.record) == ("failed", None)
    assert node.error is not None and node.error.code == "storage_error"
    assert node.error.node == name and "refused for the test" in node.error.message
    assert result.outcome == "failed" and result.error == node.error
    if failing == "producer":
        consumer = result.nodes[_NeedsMayFail.name]
        assert (consumer.outcome, consumer.blocked_by) == ("blocked", MAY_FAIL)


@pytest.mark.usefixtures("flows")
def test_a_failure_while_a_producer_ends_is_the_failure_of_the_producer(tmp_path, monkeypatch):
    """The hand-over token of a producer that passed cannot be made: the producer did not end
    well, and its consumer is blocked by it."""

    def refuse(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(default_runner_module.CompletedRun, "capture", refuse)
    result = _launch(_runner(tmp_path), _NeedsMayFail, _design(tmp_path))
    producer = result.nodes[MAY_FAIL]
    assert (producer.outcome, producer.record) == ("failed", None)
    assert producer.error is not None and producer.error.code == "storage_error"
    consumer = result.nodes[_NeedsMayFail.name]
    assert (consumer.outcome, consumer.blocked_by) == ("blocked", MAY_FAIL)
    assert result.outcome == "failed" and result.error == producer.error


@pytest.mark.usefixtures("flows")
def test_a_refusal_before_the_run_is_raised_not_reported(tmp_path, monkeypatch):
    refusal = DeliveryError("refused for the test", before_run=True)
    _refuse_identity_of(monkeypatch, MAY_FAIL, refusal)
    with pytest.raises(DeliveryError) as raised:
        _launch(_runner(tmp_path), _NeedsMayFail, _design(tmp_path))
    assert raised.value is refusal


def test_the_time_of_a_node_is_that_of_its_own_execution(tmp_path, flows, monkeypatch):
    from types import SimpleNamespace

    from xeda.flow_runner import launch_result

    def monotonic() -> float:
        _CLOCK[0] += 0.001  # each reading takes a little time
        return _CLOCK[0]

    _CLOCK[0] = 0.0
    monkeypatch.setattr(launch_result, "time", SimpleNamespace(monotonic=monotonic))
    result = _launch(_runner(tmp_path), flows["_AfterSlow"], _design(tmp_path))
    slow, after = result.nodes[flows["_Slow"].name], result.nodes[flows["_AfterSlow"].name]
    assert slow.elapsed > 100.0
    assert 0.0 < after.elapsed < 1.0, "the consumer's time includes its producer's"


def test_a_delivery_that_xeda_skipped_is_noted_as_not_delivered(tmp_path):
    """An artifact of `--outputs-to` that a link leads out of the run directory is skipped: Xeda
    writes nothing for it, so its delivery is in the node's deliveries, as not delivered."""
    from types import SimpleNamespace

    from xeda.deliver import Delivered
    from xeda.flow_runner.launch_result import LaunchCollector

    collector = LaunchCollector()
    flow = object()
    run, user = tmp_path / "run", tmp_path / "user"
    run.mkdir()
    user.mkdir()
    named = SimpleNamespace(key="netlist")
    collector.deliveries_noted(
        flow,  # type: ignore[arg-type]
        [(named, run / "d.v", user / "net.v", "digest")],
        [
            Delivered("netlist", run / "d.v", user / "net.v", "delivered"),
            Delivered("--outputs-to", run / "link.v", user / "got" / "link.v", "skipped"),
        ],
    )
    assert collector.deliveries_of(flow) == (  # type: ignore[arg-type]
        DeliveryRecord("netlist", user / "net.v", True),
        DeliveryRecord("--outputs-to", user / "got" / "link.v", False),
    )


# --- The document of a result --------------------------------------------------------------------


def _document_keys(document: dict[str, Any]) -> None:
    assert list(document) == [
        "schema_version",
        "outcome",
        "error",
        "launch_id",
        "spec",
        "roots",
        "nodes",
    ]
    for node in document["nodes"]:
        assert list(node) == [
            "name",
            "flow",
            "outcome",
            "disposition",
            "reason",
            "blocked_by",
            "elapsed",
            "run_dir",
            "deliveries",
            "record",
            "error",
        ]


@pytest.mark.usefixtures("flows")
def test_a_failed_result_round_trips_through_its_document(tmp_path):
    result = _launch(
        _runner(tmp_path),
        _NeedsMayFail,
        _design(tmp_path),
        {MAY_FAIL: {"fail": True}},
    )
    document = result.to_dict()
    _document_keys(document)
    assert document["outcome"] == "failed" and document["roots"] == [_NeedsMayFail.name]
    assert document["spec"]["design"] == "d" and document["spec"]["design_file"] is None
    assert document["spec"]["settings"] == {MAY_FAIL: {"fail": True}}
    producer, consumer = document["nodes"]
    assert producer["record"]["outcome"] == "failed" and producer["flow"] == MAY_FAIL
    assert consumer["outcome"] == "blocked" and consumer["record"] is None
    assert consumer["blocked_by"] == MAY_FAIL
    assert json.loads(json.dumps(document)) == document  # plain JSON data

    again = result_from_dict(document)
    assert again.to_dict() == document
    assert (again.outcome, again.error) == (result.outcome, result.error)
    assert again.nodes[MAY_FAIL].record == result.nodes[MAY_FAIL].record
    assert again.nodes == result.nodes
    blocked = again.nodes[_NeedsMayFail.name]
    assert blocked.error is not None and blocked.error.code == "producer_failed"
    assert blocked.error.node == _NeedsMayFail.name


def _mixed_result(tmp_path: Path) -> Result:
    """A result with a node of every kind that the document must carry: one with an error and
    deliveries, one with an error and no record, a blocked one and a skipped one."""
    failed = dataclasses.replace(
        _node("f", "failed"),
        flow="flow_f",
        run_dir=tmp_path / "run" / "d" / "flow_f",
        reason="settings changed: seed",
        elapsed=1.5,
        deliveries=(DeliveryRecord("netlist", tmp_path / "user" / "net.v", False),),
    )
    nodes = _nodes(
        _node("p", "passed"),
        failed,
        _node("b", "blocked", blocked_by="f", code="producer_failed"),
        _node("s", "skipped"),
    )
    return Result("id", Spec("b", _design(tmp_path)), ["b"], "failed", failed.error, nodes)  # type: ignore[arg-type]


def test_a_node_document_has_the_fields_of_the_node_result_and_their_meaning(tmp_path):
    result = _mixed_result(tmp_path)
    fields = [field.name for field in dataclasses.fields(NodeResult)]
    assert {"flow", "run_dir", "error"} <= set(fields)
    documents = result.to_dict()["nodes"]
    assert [document["name"] for document in documents] == list(result.nodes)
    for node, document in zip(result.nodes.values(), documents):
        assert set(document) == set(fields), node.name
        assert document["name"] == node.name and document["flow"] == node.flow
        assert document["outcome"] == node.outcome.value
        assert document["disposition"] == node.disposition.value
        assert document["reason"] == node.reason and document["blocked_by"] == node.blocked_by
        assert document["elapsed"] == node.elapsed and document["run_dir"] == str(node.run_dir)
        assert document["record"] is None and node.record is None
        assert document["error"] == (None if node.error is None else node.error.as_json_value())
        assert document["deliveries"] == [
            {"key": d.key, "destination": str(d.destination), "delivered": d.delivered}
            for d in node.deliveries
        ]


def test_the_document_of_a_result_has_the_fields_of_the_result(tmp_path):
    result = _mixed_result(tmp_path)
    document = result.to_dict()
    # The document adds its schema version. It holds `nodes` as a list in plan order and `spec` as
    # the table that `spec_document_of` builds.
    assert set(document) == {field.name for field in dataclasses.fields(Result)} | {
        "schema_version"
    }
    assert document["launch_id"] == result.launch_id and document["roots"] == list(result.roots)
    assert document["outcome"] == result.outcome.value
    assert document["error"] == result.error.as_json_value()  # type: ignore[union-attr]
    assert document["spec"]["flow"] == result.spec.flow


def test_a_document_gives_every_field_of_a_node_back(tmp_path):
    result = _mixed_result(tmp_path)
    again = result_from_dict(result.to_dict())
    assert again.nodes == result.nodes
    assert (again.launch_id, again.roots, again.outcome, again.error) == (
        result.launch_id,
        result.roots,
        result.outcome,
        result.error,
    )


@pytest.mark.usefixtures("flows")
def test_a_passed_result_round_trips_and_keeps_the_design_file(tmp_path):
    design = Design.from_file(_write_design_file(tmp_path))
    runner = _runner(tmp_path)
    result = launch_result(
        runner,
        Spec(_NeedsMayFail.name, design),
        lambda: runner.run_flow(_NeedsMayFail, design, {}),
    )
    document = result.to_dict()
    assert document["outcome"] == "passed" and document["error"] is None
    assert document["spec"]["design_file"] == str(tmp_path / "top.yaml")
    again = result_from_dict(document)
    assert again.to_dict() == document
    assert again.passed and again.record == result.record
    assert [node.flow for node in again.nodes.values()] == [MAY_FAIL, _NeedsMayFail.name]


def _write_design_file(tmp_path: Path) -> Path:
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    path = tmp_path / "top.yaml"
    path.write_text("name: d\nrtl:\n  sources: [top.v]\n  top: top\n")
    return path


def test_a_design_remembers_the_file_it_was_loaded_from_and_nothing_else(tmp_path):
    path = _write_design_file(tmp_path)
    design = Design.from_file(path)
    assert design._source_file == path
    assert "_source_file" not in design.model_dump()
    assert design.model_copy(deep=True)._source_file == path
    assert Design(name="d", design_root=tmp_path)._source_file is None


def test_result_from_dict_reads_a_detached_and_a_skipped_node(tmp_path):
    nodes = {
        "a": _node("a", "passed"),
        "b": _node("b", "detached"),
        "c": _node("c", "skipped"),
    }
    error = ErrorInfo("remote_detached", "keeps running", None, None, None)
    result = Result(
        "launch-1", Spec("a+b+c", _design(tmp_path)), ["c"], "detached", error, nodes  # type: ignore[arg-type]
    )
    document = result.to_dict()
    assert document["outcome"] == "detached" and document["error"]["code"] == "remote_detached"
    again = result_from_dict(document)
    assert again.to_dict() == document
    assert again.outcome is Outcome.DETACHED and again.error == error
    assert again.launch_id == "launch-1" and again.roots == ("c",)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"outcome": None},
        {"outcome": "done"},
        {"nodes": [{"name": "a"}]},
        {"roots": "a"},
        {"spec": None},
        {"spec": {"flow": "a", "design": "a/b", "target": None, "settings": {}}},
    ],
    ids=lambda change: next(iter(change)) + "=" + repr(next(iter(change.values()))),
)
def test_result_from_dict_refuses_a_document_that_is_no_result(tmp_path, change):
    result = Result(
        "id", Spec("a", _design(tmp_path)), ["a"], "passed", None, {"a": _node("a", "passed")}  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        result_from_dict({**result.to_dict(), **change})
    assert result_from_dict({**result.to_dict(), "future": 1}).to_dict() == result.to_dict()


def test_the_documented_run_document_has_the_keys_of_the_real_one(tmp_path):
    import re
    import textwrap

    text = (Path(__file__).parent.parent / "docs" / "machine-readable.rst").read_text()
    section = text.split("Running a flow\n==============\n", 1)[1]
    block = re.search(r"\.\. code-block:: json\n\n(    \{\n.*?\n    \})\n", section, re.S)
    assert block, "the section needs a JSON example"
    example = json.loads(textwrap.dedent(block.group(1)))
    result = Result(
        "id", Spec("a", _design(tmp_path)), ["a"], "passed", None, {"a": _node("a", "passed")}  # type: ignore[arg-type]
    )
    actual = result.to_dict()
    assert list(example) == list(actual)
    assert list(example["spec"]) == list(actual["spec"])
    assert list(example["nodes"][0]) == list(example["nodes"][1]) == list(actual["nodes"][0])
    assert list(example["nodes"][1]["deliveries"][0]) == list(
        {"key": 0, "destination": 0, "delivered": 0}
    )


@pytest.mark.usefixtures("flows")
def test_an_interrupted_launch_keeps_the_producer_that_passed_and_cancels_the_running_node(
    tmp_path, monkeypatch
):
    def interrupted(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(_NeedsMayFail, "run", interrupted)
    runner, design = _runner(tmp_path), _design(tmp_path)
    spec = Spec(_NeedsMayFail.name, design)
    with pytest.raises(KeyboardInterrupt):
        launch_result(runner, spec, lambda: runner.run_flow(_NeedsMayFail, design, {}))
    error = ErrorInfo("canceled", "The launch was interrupted.", None, None, None)
    result = failed_result(runner, spec, None, error, interrupted=True)
    assert result.outcome is Outcome.CANCELED and result.error == error
    producer, consumer = result.nodes.values()
    assert (producer.outcome, producer.record is not None) == (Outcome.PASSED, True)
    assert (consumer.outcome, consumer.record) == (Outcome.CANCELED, None)
    assert consumer.error is not None and consumer.error.code == "canceled"
    # without `interrupted`, the node that the interrupt left is a failure like any other
    failed = failed_result(runner, spec, None, ErrorInfo("internal_error", "x", None, None, None))
    assert failed.outcome is Outcome.FAILED
    assert failed.nodes[_NeedsMayFail.name].outcome is Outcome.FAILED


@pytest.mark.usefixtures("flows")
def test_a_node_that_ended_before_the_interrupt_keeps_its_outcome(tmp_path, monkeypatch):
    """Ctrl-C after the producer noted its end (its record and trace are written), in the last
    steps of its attempt, leaves the producer `passed`. The consumer, whose attempt the interrupt
    left, is `canceled`, and so is the launch."""

    def interrupted(*arguments, **keywords):
        raise KeyboardInterrupt

    monkeypatch.setattr(default_runner_module.CompletedRun, "capture", interrupted)
    runner, design = _runner(tmp_path), _design(tmp_path)
    spec = Spec(_NeedsMayFail.name, design)
    with pytest.raises(KeyboardInterrupt):
        launch_result(runner, spec, lambda: runner.run_flow(_NeedsMayFail, design, {}))
    error = ErrorInfo("canceled", "The launch was interrupted.", None, None, None)
    result = failed_result(runner, spec, None, error, interrupted=True)
    assert result.outcome is Outcome.CANCELED and result.error == error
    producer, consumer = result.nodes.values()
    assert (producer.outcome, producer.record is not None) == (Outcome.PASSED, True)
    assert consumer.outcome is Outcome.CANCELED


def test_a_design_file_is_not_part_of_the_equality_of_designs(tmp_path):
    path = _write_design_file(tmp_path)
    loaded = Design.from_file(path)
    again = Design.from_file(path)
    built = Design(**{**loaded.model_dump(), "design_root": loaded.design_root})
    assert loaded._source_file == path and built._source_file is None
    assert loaded == again == built
    assert loaded != built.model_copy(update={"name": "other"})


def test_the_design_file_of_a_document_is_absolute(tmp_path, monkeypatch):
    from xeda.outcomes import spec_document

    monkeypatch.chdir(tmp_path)
    document = spec_document(
        flow="a", design="d", design_file="sub/d.yaml", target=None, settings={}
    )
    assert document["design_file"] == str(tmp_path / "sub" / "d.yaml")
    assert (
        spec_document(flow="a", design=None, design_file=None, target=None, settings={})[
            "design_file"
        ]
        is None
    )
