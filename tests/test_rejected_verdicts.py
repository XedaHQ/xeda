"""A run whose checks call `Flow.reject` is `rejected`: a judgment, not a failure of the run.

The outcome of a launch is `passed`, `rejected` or `failed`. A run is `rejected` only if its tool
steps completed, its reports were read, its declared outputs are valid and its checks called
`reject()`. A rejected run hands over no outputs. Any other failure, and a bare `False` from
`check_results()`, leaves the outcome `failed`.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import ClassVar, Optional

import pytest

from xeda import Design
from xeda.dataclass import Field, deliverable
from xeda.design import SourceType
from xeda.flow import Flow, FlowDependencyFailure, FlowFatalError, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.tool import NonZeroExitCode


class _Judged(Flow):
    """Writes a report and an output, and judges them as its settings say."""

    results_description: ClassVar[dict[str, str]] = {}
    #: The number of calls of `run()` since the fixture reset it.
    runs: ClassVar[int] = 0

    class Settings(Flow.Settings):
        tag: str = Field("a", description="A setting that changes the identity of the run.")
        always: bool = Field(False, description="Run at every launch.")
        report_copy: Optional[Path] = Field(
            None,
            description="Where to copy the report.",
            json_schema_extra=deliverable("{design}.txt"),
        )
        reject: bool = Field(False, description="Reject the results in check_results().")
        bare_false: bool = Field(False, description="Return False from check_results().")
        bad_report: bool = Field(False, description="Fail to read the report.")
        skip_output: bool = Field(False, description="Do not write the declared output.")
        raise_in_run: bool = Field(False, description="Raise a FlowFatalError in run().")
        exit_in_run: bool = Field(False, description="Raise a NonZeroExitCode in run().")

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="The output it writes.")

    def always_runs(self) -> Optional[str]:
        return "told to" if self.settings.always else super().always_runs()

    def run(self) -> None:
        type(self).runs += 1
        self.run_directory.writable(self.run_path / "report.txt").write_text("report\n")
        if self.settings.report_copy is not None:
            copy = Path(self.settings.report_copy)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.run_path / "report.txt", copy)
        if self.settings.raise_in_run:
            raise FlowFatalError("boom")
        if not self.settings.skip_output:
            out = self.run_path / "out.txt"
            self.run_directory.writable(out).write_text("out\n")
            self.outputs.out = out
        if self.settings.exit_in_run:
            raise NonZeroExitCode("tool", 3)

    def parse_reports(self) -> bool:
        return not self.settings.bad_report

    def check_results(self) -> bool:
        if self.settings.reject:
            self.reject("slack is negative")
            return False
        return not self.settings.bare_false


class _NeedsJudged(Flow):
    """Reads the output of `_Judged`."""

    results_description: ClassVar[dict[str, str]] = {}

    class Inputs(Flow.Inputs):
        out: Path = In(
            SourceType.Data,
            producer=_Judged.name,
            output="out",
            description="The output of the judged flow.",
        )

    def run(self) -> None:
        pass


# Test-only flows, launched by class: out of the registry at once, so that no sweep over every
# registered flow collected after this module finds them.
_REGISTERED = {
    _name: registered_flows[_name]
    for _cls in (_Judged, _NeedsJudged)
    for _name in (_cls.name, _cls.__name__)
}
for _name in _REGISTERED:
    registered_flows.pop(_name, None)


@pytest.fixture
def judged_registered():
    registered_flows.update(_REGISTERED)
    _Judged.runs = 0
    yield
    for name in _REGISTERED:
        registered_flows.pop(name, None)


def _design(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    return Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})


@pytest.mark.parametrize(
    "settings, outcome",
    [
        ({}, "passed"),
        ({"reject": True}, "rejected"),
        ({"bad_report": True}, "failed"),
        ({"reject": True, "bad_report": True}, "failed"),
        ({"reject": True, "skip_output": True}, "failed"),
        ({"bare_false": True}, "failed"),
        ({"reject": True, "exit_in_run": True}, "failed"),
    ],
    ids=[
        "passes",
        "rejects",
        "unreadable report",
        "rejects an unreadable report",
        "rejects with a missing output",
        "bare False without reject()",
        "tool exits non-zero",
    ],
)
def test_the_outcome_names_what_failed(tmp_path, judged_registered, settings, outcome):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), settings
    )
    assert flow.outcome == outcome
    assert flow.succeeded is (outcome == "passed")


def test_a_rejection_is_the_error_of_the_document(tmp_path, judged_registered):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), {"reject": True}
    )
    assert flow.rejection == "slack is negative"
    document = json.loads((flow.run_path / "results.json").read_text())
    assert document["success"] is False
    assert document["error"] == {
        "code": "rejected",
        "message": f"`{flow.name}` reported failure: slack is negative",
        "node": flow.name,
        "exit_status": None,
        "log": None,
    }
    assert "outputs" not in document  # a rejected run hands nothing over


def test_a_passed_run_records_its_outputs(tmp_path, judged_registered):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), {}
    )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert set(document["outputs"]) == {"out"}
    assert flow.rejection is None


def test_a_rejection_with_a_missing_output_keeps_the_rejection_as_its_error(
    tmp_path, judged_registered
):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), {"reject": True, "skip_output": True}
    )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert document["error"]["code"] == "rejected"
    assert "slack is negative" in document["error"]["message"]


def test_a_missing_output_is_the_error_of_a_run_that_would_pass(tmp_path, judged_registered):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), {"skip_output": True}
    )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert flow.outcome == "failed"
    assert document["error"]["code"] == "missing_output"


def test_a_raising_run_is_failed_not_rejected(tmp_path, judged_registered):
    with pytest.raises(FlowFatalError):
        DefaultRunner(tmp_path / "run", display_results=False).run_flow(
            _Judged, _design(tmp_path), {"raise_in_run": True, "reject": True}
        )


def test_a_rejection_keeps_the_error_of_the_tool():
    flow = Flow.__new__(_Judged)
    flow.__dict__.update(rejection=None, results={"error": {"code": "nonzero_exit"}})
    flow.reject("slack is negative")
    assert flow.rejection == "slack is negative"
    assert flow.results["error"] == {"code": "nonzero_exit"}


def test_an_unchanged_launch_reports_the_rejection_again_without_running(
    tmp_path, judged_registered
):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    document = json.loads((first.run_path / "results.json").read_text())
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert trace["verdict"] == "rejected" and trace["error"] == document["error"]
    calls = _Judged.runs
    again = runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    assert _Judged.runs == calls  # the tool did not run
    assert again.reused and again.rejected_again and not again.succeeded
    assert again.outcome == "rejected" and again.rejection == document["error"]["message"]
    assert again.results["error"] == document["error"]
    assert again.results["timestamp"] == document["timestamp"]  # the original run's
    assert again.run_id == trace["run_id"]


@pytest.mark.parametrize(
    "change, reason",
    [
        ("settings", "settings changed: tag"),
        ("edited results.json", "output changed:"),
        ("removed output", "output missing:"),
        ("rebuild_all", "every flow runs (--rebuild-all)"),
    ],
)
def test_a_rejection_is_reported_again_only_while_everything_is_unchanged(
    tmp_path, judged_registered, change, reason
):
    design = _design(tmp_path)
    first = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, design, {"reject": True}
    )
    settings = {"reject": True}
    if change == "settings":
        settings["tag"] = "b"
    elif change == "edited results.json":
        path = first.run_path / "results.json"
        document = json.loads(path.read_text())
        document["timestamp"] = "edited"
        path.write_text(json.dumps(document))
    elif change == "removed output":
        (first.run_path / "out.txt").unlink()
    runner = DefaultRunner(
        tmp_path / "run", display_results=False, rebuild_all=change == "rebuild_all"
    )
    calls = _Judged.runs
    again = runner.run_flow(_Judged, design, settings)
    assert _Judged.runs == calls + 1 and not again.reused
    assert again.stale_reason.startswith(reason)


def test_a_rejected_run_that_always_runs_keeps_no_trace(tmp_path, judged_registered):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_Judged, _design(tmp_path), {"reject": True, "always": True})
    assert first.outcome == "rejected" and not (first.run_path / "trace.json").exists()
    calls = _Judged.runs
    runner.run_flow(_Judged, _design(tmp_path), {"reject": True, "always": True})
    assert _Judged.runs == calls + 1


def test_without_a_results_document_a_rejection_keeps_no_trace(tmp_path, judged_registered):
    runner = DefaultRunner(tmp_path / "run", display_results=False, dump_results_json=False)
    first = runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    assert first.outcome == "rejected" and not (first.run_path / "trace.json").exists()


def test_a_consumer_of_a_rejected_producer_is_blocked_and_handed_nothing(
    tmp_path, judged_registered
):
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for _launch in range(2):  # the first run, then the replay
        with pytest.raises(FlowDependencyFailure) as error:
            runner.run_flow(
                _NeedsJudged, design, {}, all_flows_settings={_Judged.name: {"reject": True}}
            )
        assert error.value.producer == _Judged.name
        consumer = runner.launched[-1]
        assert consumer.name == _NeedsJudged.name and consumer.blocked_by == _Judged.name
        assert consumer.inputs.out is None  # nothing was handed over
        document = json.loads((consumer.run_path / "results.json").read_text())
        assert document["error"]["code"] == "producer_failed"
        assert not (consumer.run_path / "trace.json").exists()
    producers = [f for f in runner.launched if f.name == _Judged.name]
    assert not producers[0].rejected_again and producers[-1].rejected_again  # the second replayed


def test_a_rejected_requested_flow_delivers_nothing_first_or_again(tmp_path, judged_registered):
    target = tmp_path / "out" / "x.txt"
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    for _launch in range(2):
        flow = runner.run_flow(_Judged, _design(tmp_path), {"reject": True, "report_copy": target})
        assert not flow.succeeded
        assert not target.exists()


def test_a_trace_that_disagrees_with_its_document_is_not_reused(tmp_path, judged_registered):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    path = first.run_path / "trace.json"
    trace = json.loads(path.read_text())
    trace["verdict"] = "passed"
    path.write_text(json.dumps(trace))
    calls = _Judged.runs
    again = runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    assert _Judged.runs == calls + 1 and not again.reused


def test_replayed_failure_names_rebuild_all(tmp_path, judged_registered, caplog):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        runner.run_flow(_Judged, _design(tmp_path), {"reject": True})
    (record,) = [r for r in caplog.records if "rejected" in r.getMessage()]
    assert "--rebuild-all" in record.getMessage() and "declare" in record.getMessage()
    assert "slack is negative" in record.getMessage()


def test_a_passed_run_is_reused_as_before(tmp_path, judged_registered):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_Judged, _design(tmp_path), {})
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert trace["verdict"] == "passed" and trace["error"] is None
    calls = _Judged.runs
    again = runner.run_flow(_Judged, _design(tmp_path), {})
    assert _Judged.runs == calls
    assert again.reused and again.succeeded and not again.rejected_again
    assert again.outcome == "passed"
    assert again.run_id == trace["run_id"]
    assert set(again.results["outputs"]) == {"out"}
