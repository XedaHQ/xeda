"""A run whose checks call `Flow.reject` is `rejected`: a judgment, not a failure of the run.

The outcome of a launch is `passed`, `rejected` or `failed`. A run is `rejected` only if its tool
steps completed, its reports were read, its declared outputs are valid and its checks called
`reject()`. A rejected run hands over no outputs. Any other failure, and a bare `False` from
`check_results()`, leaves the outcome `failed`.
"""

import json
from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.dataclass import Field
from xeda.design import SourceType
from xeda.flow import Flow, FlowFatalError, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.tool import NonZeroExitCode


class _Judged(Flow):
    """Writes a report and an output, and judges them as its settings say."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        reject: bool = Field(False, description="Reject the results in check_results().")
        bare_false: bool = Field(False, description="Return False from check_results().")
        bad_report: bool = Field(False, description="Fail to read the report.")
        skip_output: bool = Field(False, description="Do not write the declared output.")
        raise_in_run: bool = Field(False, description="Raise a FlowFatalError in run().")
        exit_in_run: bool = Field(False, description="Raise a NonZeroExitCode in run().")

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="The output it writes.")

    def run(self) -> None:
        self.run_directory.writable(self.run_path / "report.txt").write_text("report\n")
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


# Test-only flow, launched by class: out of the registry at once, so that no sweep over every
# registered flow collected after this module finds it.
_REGISTERED = {_name: registered_flows[_name] for _name in (_Judged.name, _Judged.__name__)}
for _name in _REGISTERED:
    registered_flows.pop(_name, None)


@pytest.fixture
def judged_registered():
    registered_flows.update(_REGISTERED)
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
        "type": "ReportedFailure",
        "message": f"`{flow.name}` reported failure: slack is negative",
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
    assert document["error"]["type"] == "ReportedFailure"
    assert "slack is negative" in document["error"]["message"]


def test_a_missing_output_is_the_error_of_a_run_that_would_pass(tmp_path, judged_registered):
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        _Judged, _design(tmp_path), {"skip_output": True}
    )
    document = json.loads((flow.run_path / "results.json").read_text())
    assert flow.outcome == "failed"
    assert document["error"]["type"] == "MissingOutput"


def test_a_raising_run_is_failed_not_rejected(tmp_path, judged_registered):
    with pytest.raises(FlowFatalError):
        DefaultRunner(tmp_path / "run", display_results=False).run_flow(
            _Judged, _design(tmp_path), {"raise_in_run": True, "reject": True}
        )


def test_a_rejection_keeps_the_error_of_the_tool():
    flow = Flow.__new__(_Judged)
    flow.__dict__.update(rejection=None, results={"error": {"type": "NonZeroExitCode"}})
    flow.reject("slack is negative")
    assert flow.rejection == "slack is negative"
    assert flow.results["error"] == {"type": "NonZeroExitCode"}
