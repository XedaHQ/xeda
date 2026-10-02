"""Preparation happens under input leases, before freshness, including reused launches."""

import json

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner, default_runner

from .io_flows import _Taker, _Wrapper
from .test_read_locks import _probe


@pytest.mark.parametrize("consumer", [_Taker, _Wrapper])
def test_preparation_sees_handed_over_inputs_before_freshness(tmp_path, monkeypatch, consumer):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    seen = []
    original = default_runner.expectation

    def prepare(self):
        producer = self.completed_dependencies[0]
        assert _probe(producer.run_path) == "blocked"
        if isinstance(self, _Taker):
            assert self.inputs.made.read_text() == "made\n"
        seen.append("prepare")

    def expectation(flow, *args):
        if isinstance(flow, consumer):
            seen.append("expectation")
        return original(flow, *args)

    monkeypatch.setattr(consumer, "prepare_inputs", prepare, raising=False)
    monkeypatch.setattr(default_runner, "expectation", expectation)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(consumer, design, {})
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in first.run_path.rglob("*") if p.is_file()
    }
    again = runner.launch_flow(consumer, design, {})
    assert again.reused
    assert seen == ["prepare", "expectation", "prepare", "expectation"]
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


def test_preparation_failure_invalidates_success_and_releases_lease(tmp_path, monkeypatch):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(_Taker, design, {})

    def fail(self):
        assert _probe(self.inputs.made.parent) == "blocked"
        raise FlowFatalError("broken preparation")

    monkeypatch.setattr(_Taker, "prepare_inputs", fail, raising=False)
    with pytest.raises(FlowFatalError, match="broken preparation"):
        runner.launch_flow(_Taker, design, {})
    recorded = json.loads((first.run_path / "results.json").read_text())
    assert recorded["success"] is False
    assert recorded["error"]["message"] == "broken preparation"
    for key in ("design", "flow", "design_hash", "flow_hash", "run_path", "timestamp"):
        assert recorded[key]
    assert not (first.run_path / "trace.json").exists()
    assert _probe(first.inputs.made.parent) == "free"


def test_planning_does_not_prepare_inputs(tmp_path, monkeypatch):
    def fail(self):
        pytest.fail("dry-run prepared inputs")

    monkeypatch.setattr(_Taker, "prepare_inputs", fail, raising=False)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    DefaultRunner(tmp_path / "run", display_results=False).plan(_Taker, design)
    assert not (tmp_path / "run").exists()


def test_execution_order_keeps_preparation_before_snapshot_and_start(tmp_path, monkeypatch):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    events = []
    from xeda.flow import Flow

    original_expectation = default_runner.expectation
    original_snapshot = default_runner.snapshot_inputs
    original_start = Flow.start_run
    original_run = _Taker.run

    def init(self):
        events.append("init")

    def prepare(self):
        assert self.inputs.made.read_text() == "made\n"
        assert _probe(self.inputs.made.parent) == "blocked"
        events.append("hand-over and prepare")

    def expectation(flow, *args):
        if isinstance(flow, _Taker):
            events.append("expectation")
        return original_expectation(flow, *args)

    def snapshot(expected, *args):
        if expected.flow == _Taker.name:
            events.append("snapshot")
        return original_snapshot(expected, *args)

    def start(self, *args, **kwargs):
        if isinstance(self, _Taker):
            events.append("start")
        return original_start(self, *args, **kwargs)

    def run(self):
        events.append("run")
        return original_run(self)

    monkeypatch.setattr(_Taker, "init", init)
    monkeypatch.setattr(_Taker, "prepare_inputs", prepare)
    monkeypatch.setattr(default_runner, "expectation", expectation)
    monkeypatch.setattr(default_runner, "snapshot_inputs", snapshot)
    monkeypatch.setattr(Flow, "start_run", start)
    monkeypatch.setattr(_Taker, "run", run)
    DefaultRunner(tmp_path / "run", display_results=False).launch_flow(_Taker, design, {})
    assert events == ["init", "hand-over and prepare", "expectation", "snapshot", "start", "run"]
