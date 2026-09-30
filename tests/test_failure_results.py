"""A run that fails leaves a failure document, never an earlier run's success (C4)."""

import json
import sys
from typing import ClassVar

import pytest

from xeda import Design
from xeda.dataclass import Field
from xeda.flow import Flow, FlowDependencyFailure, FlowFatalError, registered_flows
from xeda.flow_runner import DefaultRunner


class _MayFail(Flow):
    """Writes a report, or raises in `run()` when told to."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        fail: bool = Field(False, description="Raise a FlowFatalError in run().")
        report_failure: bool = Field(False, description="Report a failure without raising.")

    def run(self) -> None:
        if self.settings.fail:
            raise FlowFatalError("boom")
        self.run_directory.writable(self.run_path / "report.txt").write_text("ok\n")

    def parse_reports(self) -> bool:
        return not self.settings.report_failure


class _NeedsMayFail(Flow):
    """Launches `_MayFail`, which fails when told to."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        dep_fails: bool = Field(False, description="Whether the dependency raises.")
        dep_reports_failure: bool = Field(False, description="Whether the dependency fails.")

    def init(self) -> None:
        self.add_dependency(
            _MayFail,
            _MayFail.Settings(
                fail=self.settings.dep_fails, report_failure=self.settings.dep_reports_failure
            ),
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
for _cls in (_MayFail, _NeedsMayFail, _ExitsNonZero):
    for _name in (_cls.name, _cls.__name__):
        registered_flows.pop(_name, None)


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


@pytest.mark.parametrize(
    "dep, error_type, raised",
    [
        ({"dep_reports_failure": True}, "FlowDependencyFailure", FlowDependencyFailure),
        ({"dep_fails": True}, "FlowFatalError", FlowFatalError),
    ],
)
def test_a_failing_dependency_replaces_the_depender_previous_success(
    tmp_path, dep, error_type, raised
):
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run_flow(_NeedsMayFail, design, {})
    results_json = first.run_path / "results.json"
    assert json.loads(results_json.read_text())["success"] is True
    assert (first.run_path / "trace.json").exists()

    with pytest.raises(raised):
        runner.run_flow(_NeedsMayFail, design, dep)
    document = json.loads(results_json.read_text())
    assert document["success"] is False
    assert document["error"]["type"] == error_type
    assert document["flow"] == first.name and document["design"] == "d"
    assert not (first.run_path / "trace.json").exists()
    message = document["error"]["message"]
    if raised is FlowDependencyFailure:
        # the depender's document says which dependency failed, and where its own document is
        (dependency,) = first.completed_dependencies
        assert dependency.name in message
        assert str(dependency.run_path / "results.json") in message
    else:  # what the dependency raised, as it raised it
        assert message == "boom"
