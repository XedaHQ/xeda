"""A run that fails leaves a failure document, never an earlier run's success."""

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.dataclass import Field
from xeda.design import SourceType
from xeda.flow import Flow, FlowDependencyFailure, FlowFatalError, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner

from .tool_utils import producers_of


class _MayFail(Flow):
    """Writes a report, or raises in `run()` when told to."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        fail: bool = Field(False, description="Raise a FlowFatalError in run().")
        report_failure: bool = Field(False, description="Report a failure without raising.")

    class Outputs(Flow.Outputs):
        report: Path = Out(SourceType.Data, description="The report it writes.")

    def run(self) -> None:
        if self.settings.fail:
            raise FlowFatalError("boom")
        report = self.run_path / "report.txt"
        self.run_directory.writable(report).write_text("ok\n")
        self.outputs.report = report

    def parse_reports(self) -> bool:
        return not self.settings.report_failure


class _NeedsMayFail(Flow):
    """Reads the report `_MayFail` writes, which fails when told to."""

    results_description: ClassVar[dict[str, str]] = {}

    class Inputs(Flow.Inputs):
        report: Path = In(
            SourceType.Data,
            producer="__may_fail",
            output="report",
            description="The producer's report.",
        )

    def run(self) -> None:
        pass


class _ExitsNonZero(Flow):
    """Runs a program that exits with status 3."""

    results_description: ClassVar[dict[str, str]] = {}

    def run(self) -> None:
        from xeda.proc_utils import run_process

        run_process(sys.executable, ["-c", "raise SystemExit(3)"])


# Test-only flows, launched by class: out of the registry at once, so that no sweep over every
# registered flow collected after this module finds them.
_REGISTERED = {
    _name: registered_flows[_name]
    for _cls in (_MayFail, _NeedsMayFail, _ExitsNonZero)
    for _name in (_cls.name, _cls.__name__)
}
for _name in _REGISTERED:
    registered_flows.pop(_name, None)


@pytest.fixture
def producer_registered():
    """`_NeedsMayFail` finds its producer by name."""
    registered_flows.update(_REGISTERED)
    yield
    for name in _REGISTERED:
        registered_flows.pop(name, None)


def _design(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    return Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})


def test_a_raising_run_replaces_the_previous_success_with_a_failure_document(tmp_path):
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_MayFail, design, {})
    results_json = first.run_path / "results.json"
    assert json.loads(results_json.read_text())["success"] is True

    with pytest.raises(FlowFatalError, match="boom"):
        runner.run_flow(_MayFail, design, {"fail": True})
    document = json.loads(results_json.read_text())
    assert document["success"] is False
    assert document["error"] == {"type": "FlowFatalError", "message": "boom"}
    assert document["design"] == "d" and document["flow"] == first.name
    assert document["run_path"] and document["timestamp"]
    assert not (first.run_path / "trace.json").exists()


def test_a_non_zero_exit_is_recorded_in_the_failure_document(tmp_path):
    design = _design(tmp_path)
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _ExitsNonZero, design, {}
    )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert document["success"] is False
    assert document["error"]["type"] == "NonZeroExitCode"


def test_a_failure_the_reports_show_is_a_failure_document_with_an_error(tmp_path):
    """No exception, no nonzero exit: the flow's reports or checks say it failed. The document
    still names the error."""
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(_MayFail, design, {"report_failure": True})
    document = json.loads((flow.run_path / "results.json").read_text())
    assert document["success"] is False
    assert document["error"]["type"] == "ReportedFailure"
    assert f"`{flow.name}` reported failure" in document["error"]["message"]
    assert "reports or checks" in document["error"]["message"]


@pytest.mark.parametrize(
    "producer, reason",
    [
        ({"report_failure": True}, "reported failure"),
        ({"fail": True}, "boom"),
    ],
    ids=["reports failure", "raises"],
)
def test_a_failing_producer_replaces_the_consumer_s_previous_success(
    tmp_path, producer_registered, producer, reason
):
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_NeedsMayFail, design, {})
    results_json = first.run_path / "results.json"
    assert json.loads(results_json.read_text())["success"] is True
    assert (first.run_path / "trace.json").exists()
    (done,) = producers_of(runner, first)

    with pytest.raises(FlowDependencyFailure, match=reason):
        runner.run_flow(_NeedsMayFail, design, {}, all_flows_settings={"__may_fail": producer})
    document = json.loads(results_json.read_text())
    assert document["success"] is False
    assert document["error"]["type"] == "FlowDependencyFailure"
    assert document["flow"] == first.name and document["design"] == "d"
    assert not (first.run_path / "trace.json").exists()
    # the consumer's document says which producer failed, and where its own document is
    message = document["error"]["message"]
    assert done.name in message
    assert str(done.run_path / "results.json") in message
