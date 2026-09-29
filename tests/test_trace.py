"""Traces: written last and atomically; absent or unreadable means "no valid previous run"."""

import json
import os
from pathlib import Path

import pytest

from xeda.digest import MODIFIED_DURING_RUN, RACY_NS, FileRecord, record_file
from xeda.flow_runner.trace import (
    TRACE_FILE,
    TRACE_FORMAT,
    Expectation,
    ProgramRecord,
    Trace,
    as_recorded,
    check_trace,
    read_trace,
    remove_trace,
    write_trace,
)

OLD = 10**18  # a file time long before the trace is written


def _trace(**overrides) -> Trace:
    fields = dict(
        flow="toy",
        run_id="r" * 32,
        flowrun_hash="f" * 32,
        design_hash="d" * 32,
        xeda_version="0.5.0",
        xeda_code="x" * 32,
        flow_code="c" * 32,
        programs={"yosys": ProgramRecord(path="/opt/bin/yosys", size=1, mtime_ns=2)},
        inputs_recorded_ns=OLD + 10**12,
        inputs={"/d/a.v": FileRecord(size=1, mtime_ns=2, sha="s" * 32)},
        outputs={"/r/out.json": FileRecord(size=3, mtime_ns=4, sha="t" * 32)},
    )
    return Trace(**{**fields, **overrides})


def test_a_written_trace_reads_back_as_itself(tmp_path):
    write_trace(tmp_path, _trace())
    found = read_trace(tmp_path)
    assert found is not None
    trace, written_ns = found
    assert trace == _trace()
    assert written_ns == (tmp_path / TRACE_FILE).stat().st_mtime_ns


def test_writing_leaves_no_temporary_file(tmp_path):
    write_trace(tmp_path, _trace())
    assert sorted(p.name for p in tmp_path.iterdir()) == [TRACE_FILE]


def test_no_trace_and_an_unreadable_trace_both_mean_none(tmp_path):
    assert read_trace(tmp_path) is None
    (tmp_path / TRACE_FILE).write_text("{ not json")
    assert read_trace(tmp_path) is None
    (tmp_path / TRACE_FILE).write_text(json.dumps({"format": 1}))  # missing fields
    assert read_trace(tmp_path) is None


def test_remove_trace_is_idempotent(tmp_path):
    write_trace(tmp_path, _trace())
    remove_trace(tmp_path)
    remove_trace(tmp_path)
    assert not (tmp_path / TRACE_FILE).exists()


def test_as_recorded_matches_what_dump_json_writes(tmp_path):
    from xeda.utils import dump_json

    value = {"xdc": Path("/a/b.xdc"), "n": 3, "t": ("x", None)}
    dump_json(value, tmp_path / "v.json", backup=False)
    assert as_recorded(value) == json.loads((tmp_path / "v.json").read_text())


def _file(path: Path, text: str) -> Path:
    path.write_text(text)
    os.utime(path, ns=(OLD, OLD))
    return path


@pytest.fixture
def recorded(tmp_path):
    """A run directory whose trace records one input and one output, both unchanged since."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = _file(tmp_path / "a.v", "module a; endmodule\n")
    output = _file(run_dir / "out.json", "{}\n")
    write_trace(
        run_dir,
        _trace(
            programs={},
            inputs={str(source): record_file(source)},
            outputs={str(output): record_file(output)},
        ),
    )
    (run_dir / "settings.json").write_text(json.dumps({"flow_settings": {"seed": 1}}))
    expected = Expectation(
        flow="toy",
        flowrun_hash="f" * 32,
        design_hash="d" * 32,
        xeda_version="0.5.0",
        xeda_code="x" * 32,
        flow_code="c" * 32,
        inputs=(source,),
        settings={"seed": 1},
        dependency_runs={},
        dependency_flows={},
    )
    return run_dir, source, output, expected


def _check(run_dir, expected, **changes):
    return check_trace(run_dir, Expectation(**{**expected.__dict__, **changes}), lambda name: None)


def test_an_unchanged_run_is_fresh(recorded):
    run_dir, _, _, expected = recorded
    result = _check(run_dir, expected)
    assert result.fresh and result.run_id == "r" * 32


def test_a_trace_without_a_run_id_is_no_trace(recorded):
    run_dir, _, _, expected = recorded
    trace = json.loads((run_dir / "trace.json").read_text())
    del trace["run_id"]
    (run_dir / "trace.json").write_text(json.dumps(trace))
    assert _check(run_dir, expected).reason == "no successful previous run"


def _record_dependency_runs(run_dir: Path, runs: dict) -> None:
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["dependency_runs"] = runs
    (run_dir / "trace.json").write_text(json.dumps(trace))


def test_a_dependency_that_ran_again_is_named(recorded, tmp_path):
    """Whatever the dependency's declared outputs say, and before the input set is compared: a
    run may have read any file its dependency's run wrote. Dependencies are told apart by their
    run directories, so two of one flow are two dependencies."""
    run_dir, source, _, expected = recorded
    runs = {"toy/yosys_1": "a" * 32, "toy/yosys_2": "b" * 32}
    flows = {"toy/yosys_1": "yosys", "toy/yosys_2": "yosys"}
    _record_dependency_runs(run_dir, runs)
    assert _check(run_dir, expected, dependency_runs=runs, dependency_flows=flows).fresh
    extra = _file(tmp_path / "b.v", "module b; endmodule\n")
    again = _check(
        run_dir,
        expected,
        dependency_runs={**runs, "toy/yosys_1": "c" * 32},
        dependency_flows=flows,
        inputs=(source, extra),
    )
    assert again.reason == "yosys (toy/yosys_1) ran again"
    fewer = _check(
        run_dir, expected, dependency_runs={"toy/yosys_2": "b" * 32}, dependency_flows=flows
    )
    assert fewer.reason == "no longer depends on the run in toy/yosys_1"
    more = _check(
        run_dir,
        expected,
        dependency_runs={**runs, "toy/yosys_3": "d" * 32},
        dependency_flows={**flows, "toy/yosys_3": "yosys"},
    )
    assert more.reason == "new dependency: yosys (toy/yosys_3)"


def test_without_a_trace_nothing_is_fresh(tmp_path, recorded):
    _, _, _, expected = recorded
    assert _check(tmp_path, expected).reason == "no successful previous run"


def test_a_settings_change_names_the_setting(recorded):
    run_dir, _, _, expected = recorded
    result = _check(run_dir, expected, flowrun_hash="g" * 32, settings={"seed": 2})
    assert not result.fresh and result.reason == "settings changed: seed"


def test_an_edited_input_is_named(recorded):
    run_dir, source, _, expected = recorded
    _file(source, "module b; endmodule\n")
    os.utime(source, ns=(OLD + 1_000, OLD + 1_000))  # a real edit also advances mtime
    assert _check(run_dir, expected).reason == f"input changed: {source}"


def test_touch_alone_is_not_a_change(recorded):
    run_dir, source, _, expected = recorded
    os.utime(source, ns=(OLD + 7, OLD + 7))
    result = _check(run_dir, expected)
    assert result.fresh
    assert result.refreshed is not None
    assert result.refreshed.inputs[str(source)].mtime_ns == OLD + 7


def test_a_racy_input_is_hashed(recorded, tmp_path):
    """Inputs are racy relative to when they were recorded, before the run started -- not to
    when the trace was written, after it."""
    run_dir, source, _, expected = recorded
    recorded_ns = OLD + 10**12
    # same size and same mtime, but within the racy window of the record: must be hashed
    source.write_text("module z; endmodule\n")
    os.utime(source, ns=(recorded_ns - RACY_NS // 2, recorded_ns - RACY_NS // 2))
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["inputs"][str(source)]["mtime_ns"] = recorded_ns - RACY_NS // 2
    (run_dir / "trace.json").write_text(json.dumps(trace))
    assert _check(run_dir, expected).reason == f"input changed: {source}"


def test_an_input_modified_during_the_last_run_never_matches(recorded):
    run_dir, source, _, expected = recorded
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["inputs"][str(source)]["sha"] = MODIFIED_DURING_RUN
    (run_dir / "trace.json").write_text(json.dumps(trace))
    assert _check(run_dir, expected).reason == f"input modified during the last run: {source}"


def test_a_refreshed_trace_moves_its_inputs_racy_threshold_to_the_check(recorded):
    """A touched input is rehashed once; the refreshed record is then trusted from the check's
    time on, so it is not hashed again at every later check."""
    run_dir, source, _, expected = recorded
    os.utime(source, ns=(OLD + 7, OLD + 7))
    result = _check(run_dir, expected)
    assert result.fresh and result.refreshed is not None
    assert result.refreshed.inputs_recorded_ns > OLD + 10**12


def test_a_new_input_and_a_missing_input_are_named(recorded, tmp_path):
    run_dir, source, _, expected = recorded
    extra = _file(tmp_path / "b.v", "module b; endmodule\n")
    assert _check(run_dir, expected, inputs=(source, extra)).reason == f"new input: {extra}"
    source.unlink()
    assert _check(run_dir, expected).reason == f"input missing: {source}"


def test_a_deleted_or_edited_output_is_stale(recorded):
    run_dir, _, output, expected = recorded
    _file(output, '{"edited": true}\n')
    assert _check(run_dir, expected).reason == f"output changed: {output}"
    output.unlink()
    assert _check(run_dir, expected).reason == f"output missing: {output}"


def test_a_changed_program_is_named(recorded):
    run_dir, _, _, expected = recorded
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["programs"] = {"yosys": {"path": "/old/yosys", "size": 1, "mtime_ns": 2}}
    (run_dir / "trace.json").write_text(json.dumps(trace))
    reason = check_trace(run_dir, expected, lambda name: ProgramRecord(path="/new/yosys")).reason
    assert reason == "yosys changed"


def test_metadata_only_design_changes_come_last(recorded):
    run_dir, _, _, expected = recorded
    reason = _check(run_dir, expected, design_hash="e" * 32).reason
    assert reason == "design changed (top, parameters, defines or other metadata)"


def test_another_xeda_version_is_named(recorded):
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, xeda_version="0.6.0").reason == "xeda changed: 0.5.0 -> 0.6.0"


def test_a_change_to_xeda_s_code_is_named(recorded):
    """An editable install keeps its version string across edits: the package digest is what
    notices a changed helper."""
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, xeda_code="y" * 32).reason == "xeda's code changed"


def test_a_change_to_a_plugin_flow_s_code_is_named(recorded):
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, flow_code="e" * 32).reason == "the toy flow's code changed"


def test_an_input_no_longer_used_is_named(recorded):
    run_dir, source, _, expected = recorded
    assert _check(run_dir, expected, inputs=()).reason == f"input no longer used: {source}"


def test_a_trace_of_another_format_is_recorded_by_another_xeda(recorded):
    """A trace written by another xeda, whose fields may mean something else, is never trusted --
    whether or not it would still parse."""
    run_dir, _, _, expected = recorded
    trace = json.loads((run_dir / "trace.json").read_text())
    (run_dir / "trace.json").write_text(json.dumps({**trace, "format": TRACE_FORMAT + 1}))
    assert _check(run_dir, expected).reason == "recorded by another xeda"
    old = {k: v for k, v in trace.items() if k not in ("xeda_code", "inputs_recorded_ns")}
    (run_dir / "trace.json").write_text(json.dumps({**old, "format": 1}))  # plan 1's first
    assert _check(run_dir, expected).reason == "recorded by another xeda"


def test_a_trace_of_another_flow_is_named(recorded):
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, flow="other").reason == "recorded for another flow: toy"
