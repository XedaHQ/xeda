"""Traces: written last and atomically; absent or unreadable means "no valid previous run"."""

import json
import os
from pathlib import Path

import pytest

from xeda.digest import MODIFIED_DURING_RUN, RACY_NS, FileRecord, record_file
from xeda.flow_runner.trace import (
    TRACE_FILE,
    TRACE_FORMAT,
    BoundProducer,
    DeclaredInputRecord,
    Expectation,
    OutputEntry,
    ProgramRecord,
    Trace,
    as_recorded,
    check_trace,
    read_trace,
    remove_trace,
    write_trace,
)

pytestmark = pytest.mark.python_compat


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
        programs={
            "yosys": ProgramRecord(
                path="/opt/bin/yosys",
                file=FileRecord(size=1, mtime_ns=2, ctime_ns=2, inode=7, sha="y" * 32),
            )
        },
        inputs_recorded_ns=OLD + 10**12,
        outputs_recorded_ns=OLD + 10**12,
        inputs={"/d/a.v": FileRecord(size=1, mtime_ns=2, ctime_ns=2, inode=5, sha="s" * 32)},
        outputs={"/r/out.json": FileRecord(size=3, mtime_ns=4, ctime_ns=4, inode=6, sha="t" * 32)},
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


def _entry(sha: str, equivalence: str | None = None, path: str = "/r/yosys_1/out.json"):
    return OutputEntry(path=path, sha=sha * 32, equivalence=equivalence)


def _wired(*entries: OutputEntry, equivalence: bool = False) -> DeclaredInputRecord:
    """The input `netlist`, made by `yosys` and handed over as `entries`."""
    return DeclaredInputRecord(
        name="netlist",
        origin="producer",
        producer="yosys",
        output="netlist",
        producer_hash="p" * 32,
        producer_path="/r/yosys_1",
        paths=tuple(entry.path for entry in entries),
        references=(
            BoundProducer(
                producer="yosys",
                output="netlist",
                producer_hash="p" * 32,
                producer_path="/r/yosys_1",
                entries=entries,
            ),
        ),
        equivalence=equivalence,
    )


@pytest.fixture
def recorded(tmp_path):
    """A run directory whose trace records one input and its files as outputs, all unchanged
    since."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = _file(tmp_path / "a.v", "module a; endmodule\n")
    output = _file(run_dir / "out.json", "{}\n")
    settings = _file(run_dir / "settings.json", json.dumps({"flow_settings": {"seed": 1}}))
    write_trace(
        run_dir,
        _trace(
            programs={},
            inputs={str(source): record_file(source)},
            outputs={str(path): record_file(path) for path in (output, settings)},
            declared_inputs=[_wired(_entry("a"))],
            dependency_runs={"toy/yosys_1": "a" * 32},
        ),
    )
    expected = Expectation(
        flow="toy",
        flowrun_hash="f" * 32,
        design_hash="d" * 32,
        xeda_version="0.5.0",
        xeda_code="x" * 32,
        flow_code="c" * 32,
        inputs=(source,),
        settings={"seed": 1},
        setting_locations={},
        dependency_runs={"toy/yosys_1": "a" * 32},
        declared_inputs=(_wired(_entry("a")),),
    )
    return run_dir, source, output, expected


def _check(run_dir, expected, **changes):
    expectation = Expectation(**{**expected.__dict__, **changes})
    return check_trace(run_dir, expectation, lambda name: None)


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


def test_a_dependency_that_ran_again_is_no_change_in_itself(recorded):
    """What the flow consumed is what it declared: the producer's run is recorded for the record,
    and is not compared."""
    run_dir, _, _, expected = recorded
    ran_again = {"toy/yosys_1": "c" * 32, "toy/yosys_2": "d" * 32}
    assert _check(run_dir, expected, dependency_runs=ran_again).fresh
    assert _check(run_dir, expected, dependency_runs={}).fresh


def test_a_changed_output_is_named_by_its_input_and_its_producer(recorded):
    run_dir, _, _, expected = recorded
    changed = _check(run_dir, expected, declared_inputs=(_wired(_entry("b")),))
    assert changed.reason == "`netlist` from `yosys` changed"


def test_an_output_that_moved_is_a_change(recorded):
    run_dir, _, _, expected = recorded
    moved = _wired(_entry("a", path="/r/yosys_2/out.json"))
    assert _check(run_dir, expected, declared_inputs=(moved,)).reason == (
        "`netlist` from `yosys` changed"
    )


def test_an_output_that_changed_while_the_flow_ran_never_matches(recorded):
    run_dir, _, _, expected = recorded
    entry = _entry("a")
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["declared_inputs"][0]["references"][0]["entries"][0]["unknown"] = True
    (run_dir / "trace.json").write_text(json.dumps(trace))
    stale = _check(run_dir, expected, declared_inputs=(_wired(entry),))
    assert stale.reason == "`netlist` from `yosys` changed while toy ran"


def test_the_edge_is_judged_after_everything_else(recorded):
    """A flow stale for its own reasons says so, whatever its producer did."""
    run_dir, source, _, expected = recorded
    _file(source, "module b; endmodule\n")
    os.utime(source, ns=(OLD + 1_000, OLD + 1_000))
    result = _check(run_dir, expected, declared_inputs=(_wired(_entry("b")),))
    assert result.reason == f"input changed: {source}"
    result = _check(run_dir, expected, declared_inputs=(_wired(_entry("b")),), design_hash="e" * 32)
    assert result.reason == f"input changed: {source}"
    source.unlink()
    result = _check(run_dir, expected, declared_inputs=(_wired(_entry("b")),))
    assert result.reason == f"input missing: {source}"


def test_an_equivalent_output_is_no_change_for_an_input_that_opts_in(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = _file(run_dir / "out.json", "{}\n")
    recorded_input = _wired(_entry("a", "e" * 32), equivalence=True)
    write_trace(
        run_dir,
        _trace(
            programs={},
            inputs={},
            outputs={str(out): record_file(out)},
            declared_inputs=[recorded_input],
        ),
    )

    def check(*entries: OutputEntry, equivalence: bool):
        expected = Expectation(
            flow="toy",
            flowrun_hash="f" * 32,
            design_hash="d" * 32,
            xeda_version="0.5.0",
            xeda_code="x" * 32,
            flow_code="c" * 32,
            inputs=(),
            settings={},
            setting_locations={},
            dependency_runs={},
            declared_inputs=(_wired(*entries, equivalence=equivalence),),
        )
        return check_trace(run_dir, expected, lambda name: None)

    same = check(_entry("b", "e" * 32), equivalence=True)
    assert same.fresh and same.equivalent == ("netlist",)
    identical = check(_entry("a", "e" * 32), equivalence=True)
    assert identical.fresh and identical.equivalent == ()
    assert not check(_entry("b", "e" * 32), equivalence=False).fresh  # does not opt in
    assert not check(_entry("b", "f" * 32), equivalence=True).fresh  # another digest
    assert not check(_entry("b"), equivalence=True).fresh  # no digest now


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


def test_a_symbolic_link_output_is_checked_by_its_target_and_its_content(recorded, tmp_path):
    """An output link is recorded by its target and, for a file, by that file's content: an edit
    of the file it points to, another target with the same content, and a file whose content is
    the link's target text are each a change."""
    run_dir, _, _, expected = recorded
    target = _file(tmp_path / "lib.v", "module lib; endmodule\n")
    link = run_dir / "lib.v"
    link.symlink_to(target)
    trace = json.loads((run_dir / "trace.json").read_text())
    record = record_file(link, follow_symlinks=False)
    assert record.sha.startswith("symlink:") and record != record_file(link)
    trace["outputs"][str(link)] = record.model_dump()
    (run_dir / "trace.json").write_text(json.dumps(trace))
    assert _check(run_dir, expected).fresh
    target.write_text("module lib2; endmodule\n")
    assert _check(run_dir, expected).reason == f"output changed: {link}"
    target.write_text("module lib; endmodule\n")
    assert _check(run_dir, expected).fresh
    same = _file(tmp_path / "same.v", target.read_text())
    link.unlink()
    link.symlink_to(same)
    assert _check(run_dir, expected).reason == f"output changed: {link}"
    link.unlink()
    link.write_text(str(target))
    assert _check(run_dir, expected).reason == f"output changed: {link}"


def test_a_file_that_appeared_in_a_managed_run_directory_is_a_change(recorded):
    """The directory is xeda's: a file there that the run did not leave is a change."""
    run_dir, _, _, expected = recorded
    (run_dir / "sub").mkdir()
    (run_dir / "sub" / "later.txt").write_text("mine\n")
    reason = _check(run_dir, expected).reason
    # the directory is an entry of its own, listed before what it holds
    assert reason == f"new file in the run directory: {(run_dir / 'sub').resolve()}"


def test_a_changed_program_is_named(recorded):
    run_dir, _, _, expected = recorded
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["programs"] = {"yosys": {"path": "/old/yosys", "file": None}}
    (run_dir / "trace.json").write_text(json.dumps(trace))
    reason = check_trace(run_dir, expected, lambda name: "/new/yosys").reason
    assert reason == "yosys changed"


def _record_program(run_dir: Path, name: str, program: ProgramRecord) -> None:
    trace = json.loads((run_dir / "trace.json").read_text())
    trace["programs"] = {name: program.model_dump(mode="json")}
    (run_dir / "trace.json").write_text(json.dumps(trace))


def test_a_program_is_checked_as_a_file_under_the_trust_rule(recorded, tmp_path):
    """A program is its location and its file's record -- an edit given back its size
    and mtime is a change (its inode change time moved, so its content is read); a `touch` is
    not."""
    run_dir, _, _, expected = recorded
    program = _file(tmp_path / "yosys", "#!/bin/sh\necho one\n")
    _record_program(run_dir, "yosys", ProgramRecord(path=str(program), file=record_file(program)))
    locate = {"yosys": str(program)}.get
    assert check_trace(run_dir, expected, locate).fresh
    os.utime(program, None)
    assert check_trace(run_dir, expected, locate).fresh
    program.write_text("#!/bin/sh\necho two\n")
    os.utime(program, ns=(OLD, OLD))
    assert check_trace(run_dir, expected, locate).reason == "yosys changed"


def test_a_program_replaced_during_its_run_or_an_image_is_checked_by_what_it_is(recorded):
    run_dir, _, _, expected = recorded
    unknown = FileRecord(size=1, mtime_ns=2, ctime_ns=2, inode=7, sha=MODIFIED_DURING_RUN)
    _record_program(run_dir, "yosys", ProgramRecord(path="/opt/bin/yosys", file=unknown))
    reason = check_trace(run_dir, expected, lambda name: "/opt/bin/yosys").reason
    assert reason == "yosys changed during the last run"
    image = "docker-image:hdlc/ghdl:yosys"
    _record_program(run_dir, image, ProgramRecord(path="sha256:0123"))
    assert check_trace(run_dir, expected, lambda name: "sha256:0123").fresh
    reason = check_trace(run_dir, expected, lambda name: "sha256:4567").reason
    assert reason == f"{image} changed"


def test_metadata_only_design_changes_come_last(recorded):
    run_dir, _, _, expected = recorded
    reason = _check(run_dir, expected, design_hash="e" * 32).reason
    assert reason == "design changed (top, parameters, defines or other metadata)"


def test_another_xeda_version_is_named(recorded):
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, xeda_version="0.6.0").reason == "Xeda changed: 0.5.0 -> 0.6.0"


def test_a_change_to_xeda_s_code_is_named(recorded):
    """An editable install keeps its version string across edits: the package digest is what
    notices a changed helper."""
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, xeda_code="y" * 32).reason == "Xeda's code changed"


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
    assert _check(run_dir, expected).reason == "recorded by another Xeda"
    old = {k: v for k, v in trace.items() if k not in ("xeda_code", "inputs_recorded_ns")}
    (run_dir / "trace.json").write_text(json.dumps({**old, "format": 1}))  # the first trace format
    assert _check(run_dir, expected).reason == "recorded by another Xeda"


def test_a_trace_of_another_flow_is_named(recorded):
    run_dir, _, _, expected = recorded
    assert _check(run_dir, expected, flow="other").reason == "recorded for another flow: toy"
