"""Each run leaves one record, `results.json`, in one versioned schema, and every reader of a
stored run reads it through the codec of `xeda.records`.

The flows here are small test doubles: they need no tool. The sections follow the questions that
the schema has to answer: what the file holds, whether a fresh, a reused and a loaded record agree,
and what happens with a value that is no metric or a file that an older Xeda wrote.
"""

from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import pytest

from xeda import Design
from xeda.digest import content_digest
from xeda.design import SourceType
from xeda.errors import RecordError
from xeda.flow import Flow, MetricSpec, Out
from xeda.flow.sim import SimFlow, judge_evidence
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace import previous_trace
from xeda.frozen import FrozenDict, freeze, thaw
from xeda.records import (
    ErrorInfo,
    InvalidMetric,
    RunRecord,
    decode_record,
    encode_record,
    nest_metrics,
    read_record,
    stored_outputs,
)
from xeda.dataclass import Field

from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .io_flows import _Maker, _Reader, _Taker

KEYS = {
    "schema_version",
    "run_id",
    "xeda_version",
    "flow",
    "design",
    "target",
    "identity",
    "configuration",
    "outcome",
    "error",
    "metrics",
    "metric_info",
    "outputs",
    "artifacts",
    "settings",
    "inputs",
    "started",
    "finished",
    "runtime",
    "dependencies",
    "inherited_variables",
}


class _Counts(Flow):
    """Writes metrics, and fails, is rejected or writes a bad value when its `mode` says so."""

    results_description: ClassVar[dict[str, MetricSpec]] = {
        "count": MetricSpec("A count.", "integer"),
        "group.size": MetricSpec("A size, in a group.", "integer"),
        "group.ratio": MetricSpec("A ratio, in the same group.", "number"),
        "names": MetricSpec("Some names.", "text_list"),
        "delays": MetricSpec("Delays by name.", "numbers", "ns"),
        "levels": MetricSpec("Levels by name.", "counts"),
        "never": MetricSpec("A metric that no run writes.", "integer"),
        "count.inner": MetricSpec("A metric below another metric.", "integer"),
    }

    class Settings(Flow.Settings):
        mode: str = Field("ok", description="What the run does.")

    def run(self) -> None:
        mode = self.settings.mode
        results = self.results
        results["count"] = 3
        results["group.size"] = 2
        results["group.ratio"] = 0.5
        results["names"] = ["a", "b"]
        results["delays"] = {"x": 1.5, "y": 2}
        results["levels"] = {"x": 1}
        results["never"] = None
        results["_internal"] = 7
        results["undescribed"] = 9
        if mode == "inf":
            results["group.ratio"] = float("inf")
        elif mode == "nan_in_list_table":
            results["delays"] = {"x": float("nan")}
        elif mode == "wrong_type":
            results["count"] = "3"
        elif mode == "bool_for_integer":
            results["count"] = True
        elif mode == "clash":
            results["count.inner"] = 1

    def parse_reports(self) -> bool:
        return self.settings.mode != "fail"

    def check_results(self) -> bool:
        if self.settings.mode == "reject":
            self.reject("not good enough")
        return True


class _Lists(Flow):
    """Writes a list output."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        made: list[Path] = Out(SourceType.Data, description="The ordered files.")

    def run(self) -> None:
        paths = [self.run_path / name for name in ("second.txt", "first.txt")]
        for path in paths:
            path.write_text(path.name)
        self.outputs.made = paths


class _Simulator(SimFlow):
    """A simulator that leaves no end record."""

    reads_sources = frozenset({SourceType.SystemVerilog})
    results_description: ClassVar[dict[str, MetricSpec]] = {}

    def run(self) -> None:
        pass

    def check_results(self) -> bool:
        return judge_evidence(self, None, "error")


@pytest.fixture(autouse=True)
def private_registry():
    with preserved_flow_registry():
        yield


@pytest.fixture
def design(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    (root / "top.sv").write_text("module top; endmodule\n")
    return Design(name="d", design_root=root, rtl={"sources": ["top.sv"], "top": "top"})


def _runner(tmp_path: Path, **options: Any) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **options)


def _document(flow: Flow) -> dict[str, Any]:
    return json.loads((flow.run_path / "results.json").read_text())


def _no_constant(name: str) -> Any:
    pytest.fail(f"the document holds {name}")


# --- What the file holds -------------------------------------------------------------------------


def test_a_passed_run_writes_schema_1(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {})
    document = json.loads((flow.run_path / "results.json").read_text(), parse_constant=_no_constant)
    assert set(document) == KEYS
    assert document["schema_version"] == 1
    assert (document["outcome"], document["error"]) == ("passed", None)
    assert (document["flow"], document["design"], document["target"]) == (flow.name, "d", None)
    assert document["identity"] == flow.flow_hash
    # a dotted key is nested; an undescribed, an internal and an unknown value are no metrics
    assert document["metrics"] == {
        "count": 3,
        "group": {"size": 2, "ratio": 0.5},
        "names": ["a", "b"],
        "delays": {"x": 1.5, "y": 2},
        "levels": {"x": 1},
    }
    assert document["metric_info"]["group.size"] == {
        "description": "A size, in a group.",
        "type": "integer",
        "unit": None,
    }
    assert document["metric_info"]["delays"]["unit"] == "ns"
    assert set(document["metric_info"]) == {
        "count",
        "group.size",
        "group.ratio",
        "names",
        "delays",
        "levels",
    }
    for gone in ("success", "run_path", "design_hash", "flow_hash", "settings_hash", "timestamp"):
        assert gone not in document
    assert document["settings"]["mode"] == "ok"
    assert document["configuration"]["rtl"]["sources"] == [
        {"path": "top.sv", "type": "SystemVerilog"}
    ]
    assert document["configuration"]["rtl"]["top"] == "top"
    for name in ("started", "finished"):
        assert datetime.fromisoformat(document[name]).utcoffset() == timezone.utc.utcoffset(None)
    assert document["runtime"] >= 0
    assert document["inherited_variables"] == [] and document["dependencies"] == []
    assert document["artifacts"] == {} and document["outputs"] == {} and document["inputs"] == []


def test_the_record_has_the_values_of_the_document(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {})
    record = flow.record
    assert isinstance(record, RunRecord) and record.passed
    assert record.metric("group.size") == 2 and record.metric("count") == 3
    assert record.metric("group") == {"size": 2, "ratio": 0.5}
    for path in ("nothing", "group.nothing", "count.inner", "group.size.deep"):
        with pytest.raises(KeyError):
            record.metric(path)
    assert record.to_dict() == encode_record(record) == _document(flow)
    for path, info in record.metric_info.items():
        assert record.metric(path) is not None, path
        assert info.path == path
    assert record.run_dir == flow.run_path and record.run_root == tmp_path / "xeda_run"


# --- Fresh, reused and loaded records ------------------------------------------------------------


def test_fresh_reused_and_loaded_records_are_equal(tmp_path, design, monkeypatch):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    again = _runner(tmp_path).launch_flow(_Taker, design, {})
    assert not first.reused and again.reused
    # reading a record needs no flow: the flows are not imported
    monkeypatch.setitem(sys.modules, "xeda.flows", None)
    loaded = read_record(first.run_path, run_root=first.record.run_root)
    assert first.record == again.record == loaded
    assert hash(loaded) == hash(first.record)
    assert loaded.run_id == first.run_id == again.run_id
    (maker,) = [flow for flow in (first,)]
    producer = read_record(tmp_path / "xeda_run" / "d" / "__maker", run_root=loaded.run_root)
    assert loaded.inputs[0].producer_run_id == producer.run_id
    assert loaded.inputs[0].producer_run_dir == producer.run_dir
    assert maker is first


def test_a_loaded_record_is_the_record_of_its_document(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {})
    document = _document(flow)
    again = decode_record(document, run_dir=flow.run_path, run_root=flow.record.run_root)
    assert again == flow.record and encode_record(again) == document


# --- A value that is no metric -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("inf", "group.ratio"),
        ("nan_in_list_table", "delays"),
        ("wrong_type", "count"),
        ("bool_for_integer", "count"),
        ("clash", "count.inner"),
    ],
)
def test_a_non_finite_metric_fails_the_node_with_invalid_metric(tmp_path, design, mode, path):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {"mode": mode})
    text = (flow.run_path / "results.json").read_text()
    document = json.loads(text, parse_constant=_no_constant)  # no NaN, no Infinity
    assert document["outcome"] == "failed" and not flow.succeeded
    assert document["error"]["code"] == "invalid_metric"
    assert document["error"]["node"] == flow.name
    assert f"`{path}`" in document["error"]["message"]
    # the metrics that are valid stay
    assert document["metrics"]["names"] == ["a", "b"]
    assert path not in document["metric_info"]
    assert not (flow.run_path / "trace.json").exists()
    assert "NaN" not in text and "Infinity" not in text


def test_nest_metrics_applies_one_rule_and_names_the_metric():
    specs = _Counts.results_description
    nested, info = nest_metrics({"count": 1, "group.size": 2, "_x": 1, "other": 2}, specs)
    assert nested == {"count": 1, "group": {"size": 2}}
    assert list(info) == ["count", "group.size"]
    for flat, path in (
        ({"count": float("inf")}, "count"),
        ({"count": 1.0}, "count"),
        ({"delays": {"x": float("-inf")}}, "delays"),
        ({"names": ["a", float("nan")]}, "names"),
        ({"group.size": float("nan")}, "group.size"),
    ):
        with pytest.raises(InvalidMetric) as caught:
            nest_metrics(flat, specs)
        assert caught.value.path == path


# --- A run directory that an older Xeda wrote ----------------------------------------------------


def test_an_old_results_document_makes_the_node_run_again(tmp_path, design):
    first = _runner(tmp_path).launch_flow(_Counts, design, {})
    results_json = first.run_path / "results.json"
    run_root = first.record.run_root
    # the document of an older Xeda: no `schema_version`, a `success` instead of an outcome
    old = {"success": True, "design": "d", "flow": first.name, "run_path": str(first.run_path)}
    results_json.write_text(json.dumps(old))
    with pytest.raises(RecordError) as caught:
        read_record(first.run_path, run_root=run_root)
    assert caught.value.code == "unsupported_schema" and caught.value.path == results_json

    again = _runner(tmp_path).launch_flow(_Counts, design, {})
    assert not again.reused and again.succeeded
    assert again.run_id != first.run_id
    assert read_record(again.run_path, run_root=run_root).passed
    assert _document(again)["schema_version"] == 1


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (None, "missing_record"),
        ("not json", "corrupt_record"),
        ("[]", "corrupt_record"),
        ('{"schema_version": true}', "corrupt_record"),
        ('{"schema_version": 2}', "unsupported_schema"),
        ('{"schema_version": 1}', "corrupt_record"),
        ('{"schema_version": 1, "runtime": NaN}', "corrupt_record"),
    ],
)
def test_a_record_that_cannot_be_read_says_why(tmp_path, text, code):
    if text is not None:
        (tmp_path / "results.json").write_text(text)
    with pytest.raises(RecordError) as caught:
        read_record(tmp_path, run_root=tmp_path.parent)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.update(outcome="blocked"),
        lambda d: d.update(error=None, outcome="failed"),
        lambda d: d.update(
            error={"code": "x", "message": "m", "node": None, "exit_status": 1.5, "log": None}
        ),
        lambda d: d["artifacts"].update(log="/etc/passwd"),
        lambda d: d["artifacts"].update(log="../outside"),
        lambda d: d.update(
            metric_info={"ghost": {"description": "", "type": "integer", "unit": None}}
        ),
        lambda d: d.update(started="yesterday"),
        lambda d: d.update(started="2026-01-01T00:00:00"),
        lambda d: d.update(runtime=True),
        lambda d: d.pop("outputs"),
    ],
)
def test_a_document_of_the_wrong_form_is_a_corrupt_record(tmp_path, design, damage):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {})
    document = _document(flow)
    damage(document)
    with pytest.raises(RecordError) as caught:
        decode_record(document, run_dir=flow.run_path, run_root=flow.record.run_root)
    assert caught.value.code == "corrupt_record"


def test_an_unknown_field_and_an_unknown_error_code_are_kept_apart(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "fail"})
    document = _document(flow)
    document["added_later"] = {"x": 1}
    document["error"]["code"] = "a_code_of_the_future"
    record = decode_record(document, run_dir=flow.run_path, run_root=flow.record.run_root)
    assert record.error is not None and record.error.code == "a_code_of_the_future"


# --- Failed and rejected records -----------------------------------------------------------------


def test_a_failed_and_a_rejected_record_keep_their_metrics(tmp_path, design):
    failed = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "fail"})
    document = _document(failed)
    assert document["outcome"] == "failed"
    assert document["error"]["code"] == "reported_failure"
    assert document["metrics"]["count"] == 3 and document["metrics"]["group"] == {
        "size": 2,
        "ratio": 0.5,
    }
    assert not (failed.run_path / "trace.json").exists()

    rejected = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "reject"})
    document = _document(rejected)
    assert document["outcome"] == "rejected"
    assert (
        document["error"]["code"] == "rejected"
        and "not good enough" in document["error"]["message"]
    )
    assert document["metrics"]["names"] == ["a", "b"]
    assert previous_trace(rejected.run_path, rejected.name).verdict == "rejected"

    again = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "reject"})
    assert again.reused and again.rejected_again
    assert again.record == rejected.record
    assert again.record.error == rejected.record.error


def test_a_simulation_failure_error_survives_the_record(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Simulator, design, {})
    assert not flow.succeeded
    written = ErrorInfo.from_json(flow.results["error"])
    assert written.code == "reported_failure"
    loaded = read_record(flow.run_path, run_root=flow.record.run_root)
    assert loaded.outcome == "failed"
    assert loaded.error == written == flow.record.error
    assert loaded.error.node == flow.name and "no end record" in loaded.error.message


# --- Outputs, artifacts and paths ----------------------------------------------------------------


def test_a_list_output_and_a_bsc_design_output_have_names_and_digests(tmp_path, design):
    lister = _runner(tmp_path).launch_flow(_Lists, design, {})
    output = _document(lister)["outputs"]["made"]
    assert output == {
        "kind": "Data",
        "cardinality": "many",
        "names": ["second.txt", "first.txt"],
        "digests": [content_digest(lister.run_path / name) for name in ("second.txt", "first.txt")],
        "design": None,
    }
    stored = stored_outputs(lister.record)
    assert [path for path, _ in stored["made"]] == [
        lister.run_path / "second.txt",
        lister.run_path / "first.txt",
    ]

    fake_bsc, _rival, _to_vhdl = converter_flows()
    root = design.root_path
    (root / "A.bsv").write_text("package A; endpackage\n")
    mixed = Design(name="d", design_root=root, rtl={"sources": ["A.bsv", "top.sv"], "top": "mkA"})
    converter = _runner(tmp_path).launch_flow(fake_bsc, mixed, {})
    assert converter.succeeded
    output = _document(converter)["outputs"]["design"]
    assert output["kind"] == "design" and output["cardinality"] == "one"
    assert output["names"] == ["A.v"]
    assert output["digests"] == [content_digest(converter.run_path / "A.v")]
    assert output["design"] == {
        "top": "mkA",
        "input_design": mixed.parts_hash({"rtl", "tb"}),
        "sources": [
            {"generated": "A.v", "type": "Verilog"},
            {"source": 1, "file": "top.sv", "type": "SystemVerilog"},
        ],
    }
    again = _runner(tmp_path).launch_flow(fake_bsc, mixed, {})
    assert again.reused and again.record == converter.record


def test_a_record_holds_no_absolute_path_to_a_file_of_the_run(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Taker, design, {})
    document = _document(flow)
    producer = _document_of(tmp_path, "__maker")
    for found in (document, producer):
        strings = list(_strings(found))
        assert not [text for text in strings if str(tmp_path) in text], strings
    assert document["inputs"][0]["producer_run_dir"] == "d/__maker"
    assert producer["artifacts"] == {} and producer["outputs"]["made"]["names"] == ["made.txt"]


def _document_of(tmp_path: Path, name: str) -> dict[str, Any]:
    return json.loads((tmp_path / "xeda_run" / "d" / name / "results.json").read_text())


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_a_source_input_lists_its_file_with_its_digest(tmp_path, design):
    (design.root_path / "given.dat").write_text("given\n")
    with_data = Design(
        name="d",
        design_root=design.root_path,
        rtl={"sources": [{"file": "given.dat", "type": "Data"}], "top": "t"},
    )
    flow = _runner(tmp_path).launch_flow(_Reader, with_data, {})
    (item,) = _document(flow)["inputs"]
    assert item == {
        "name": "data",
        "origin": "source",
        "producer": None,
        "output": None,
        "producer_run_id": None,
        "producer_run_dir": None,
        "generation": None,
        "sources": [
            {
                "path": "given.dat",
                "generated": False,
                "digest": content_digest(design.root_path / "given.dat"),
                "dependency": None,
            }
        ],
    }
    assert flow.record.inputs[0].sources[0].path == "given.dat"


def test_an_artifact_is_a_name_in_the_run_directory(tmp_path, design):
    class Writes(Flow):
        """Registers a file, a list of files, a file outside the run and a value that is none."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            for name in ("one.log", "a.rpt", "b.rpt"):
                (self.run_path / name).write_text(name)
            outside = tmp_path / "outside.txt"
            outside.write_text("x")
            self.artifacts.log = self.run_path / "one.log"
            self.artifacts.reports = [self.run_path / "a.rpt", "b.rpt"]
            self.artifacts.elsewhere = outside
            self.artifacts.nothing = None

    flow = _runner(tmp_path).launch_flow(Writes, design, {})
    assert dict(flow.record.artifacts) == {
        "log": "one.log",
        "reports[0]": "a.rpt",
        "reports[1]": "b.rpt",
    }
    assert _document(flow)["artifacts"] == dict(flow.record.artifacts)


# --- Reuse, hand-over, delivery and the dry run -------------------------------------------------


def test_a_reused_producer_hands_over_and_delivers_from_schema_1(tmp_path, design):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    assert first.results["read"] == "made\n"

    out = tmp_path / "out"
    maker = _runner(tmp_path, outputs_to=out).launch_flow(_Maker, design, {})
    assert maker.reused and maker.record.passed
    assert (out / "made.txt").read_text() == "made\n"

    second = _runner(tmp_path)
    plan = second.plan_with_freshness(_Taker, design)[1]
    assert {state.state for state in plan.values()} == {"fresh"}
    taker = second.launch_flow(_Taker, design, {})
    assert taker.reused and taker.inputs.made.read_text() == "made\n"
    assert taker.inputs.made == maker.run_path / "made.txt"


@pytest.mark.parametrize("reuse", [False, True])
def test_record_trace_and_consumer_provenance_share_one_run_id(tmp_path, design, reuse):
    first = _runner(tmp_path).launch_flow(_Taker, design, {})
    flow = first
    if reuse:
        flow = _runner(tmp_path).launch_flow(_Taker, design, {})
        assert flow.reused
    run_root = flow.record.run_root
    producer_dir = run_root / "d" / "__maker"
    producer_record = read_record(producer_dir, run_root=run_root)
    assert producer_record.run_id == previous_trace(producer_dir, "__maker").run_id
    consumer_record = read_record(flow.run_path, run_root=run_root)
    assert consumer_record.run_id == previous_trace(flow.run_path, flow.name).run_id == flow.run_id
    (item,) = consumer_record.inputs
    assert item.producer_run_id == producer_record.run_id
    assert item.producer_run_dir == producer_dir
    assert (item.origin, item.producer, item.output) == ("producer", "__maker", "made")
    assert previous_trace(flow.run_path, flow.name).dependency_runs == {
        "d/__maker": producer_record.run_id
    }


def test_a_failed_execution_has_its_own_run_id(tmp_path, design):
    passed = _runner(tmp_path).launch_flow(_Counts, design, {})
    failed = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "fail"})
    assert failed.run_path == passed.run_path
    record = read_record(failed.run_path, run_root=failed.record.run_root)
    assert record.outcome == "failed"
    assert record.run_id == failed.run_id and record.run_id != passed.run_id
    other = _runner(tmp_path).launch_flow(_Counts, design, {"mode": "fail"})
    assert other.run_id != failed.run_id


# --- Frozen values --------------------------------------------------------------------------------


def test_nested_values_of_a_record_cannot_be_changed(tmp_path, design):
    record = _runner(tmp_path).launch_flow(_Counts, design, {}).record
    with pytest.raises(TypeError):
        record.metrics["group"]["size"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        record.configuration["rtl"]["top"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        record.metric_info["count"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        record.artifacts["x"] = "y"  # type: ignore[index]
    with pytest.raises(AttributeError):
        record.outcome = "failed"  # type: ignore[misc]
    assert isinstance(record.metrics["group"], FrozenDict)
    assert isinstance(record.metrics["names"], tuple)


def test_a_record_is_a_copy_of_what_it_was_built_from(tmp_path, design):
    flow = _runner(tmp_path).launch_flow(_Counts, design, {})
    document = _document(flow)
    record = decode_record(document, run_dir=flow.run_path, run_root=flow.record.run_root)
    document["metrics"]["group"]["size"] = 99
    document["metrics"]["names"].append("c")
    document["settings"]["mode"] = "changed"
    assert record.metric("group.size") == 2
    assert record.metric("names") == ("a", "b")
    assert record.settings["mode"] == "ok"


def test_a_record_can_be_pickled(tmp_path, design):
    record = _runner(tmp_path).launch_flow(_Taker, design, {}).record
    again = pickle.loads(pickle.dumps(record))
    assert again == record and hash(again) == hash(record)
    assert again.to_dict() == record.to_dict()


def test_a_frozen_dict_reads_like_a_dict_and_cannot_change():
    value = freeze({"a": {"b": [1, {"c": 2}]}, "d": (3,)})
    assert isinstance(value, FrozenDict) and isinstance(value["a"], FrozenDict)
    assert value["a"]["b"] == (1, FrozenDict({"c": 2})) and value["d"] == (3,)
    assert thaw(value) == {"a": {"b": [1, {"c": 2}]}, "d": [3]}
    assert value == FrozenDict(value) == dict(value)
    assert hash(value) == hash(freeze({"a": {"b": [1, {"c": 2}]}, "d": [3]}))
    assert pickle.loads(pickle.dumps(value)) == value
    for change in (
        lambda: value.__setitem__("x", 1),
        lambda: value["a"].__setitem__("b", 1),
        lambda: value.update(x=1),  # type: ignore[attr-defined]
        lambda: value.pop("a"),  # type: ignore[attr-defined]
        lambda: value.__delitem__("a"),  # type: ignore[attr-defined]
    ):
        with pytest.raises((TypeError, AttributeError)):
            change()
    unhashable = freeze({"a": {1, 2}})
    assert unhashable["a"] == {1, 2}


def test_freeze_copies_so_a_later_change_of_the_source_is_not_seen():
    source = {"a": {"b": [1, 2]}}
    value = freeze(source)
    source["a"]["b"].append(3)
    source["a"]["new"] = 1
    assert thaw(value) == {"a": {"b": [1, 2]}}
