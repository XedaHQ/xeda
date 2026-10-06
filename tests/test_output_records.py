"""A successful run records each declared output it wrote -- inside its run directory, written by
this very run -- with its content digest, in `results.json`. A consumer is
handed an output only as that record names it, and only while the file still holds what the
record says: for a producer that just ran and one reused from its `results.json` alike."""

import json
from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.digest import content_digest
from xeda.flow import FlowDependencyFailure, Out
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner import default_runner as runner_module
from xeda.flow_runner import outputs as output_module
from xeda.flow_runner.outputs import handed_over

from .io_flows import _Maker


@pytest.fixture
def design(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"})


def _launch(tmp_path: Path, design: Design, settings: dict, **launcher):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, **launcher)
    return runner.launch_flow(_Maker, design, settings)


def test_a_run_records_each_declared_output_with_its_digest(tmp_path, design):
    maker = _launch(tmp_path, design, {})
    assert maker.succeeded
    record = maker.results["outputs"]["made"]
    path = Path(record["path"])
    assert path == (maker.run_path / "made.txt").resolve()
    assert record["sha"] == content_digest(path)
    written = json.loads((maker.run_path / "results.json").read_text())
    assert written["outputs"] == {"made": {"path": str(path), "sha": record["sha"]}}


def test_an_enabled_output_the_run_did_not_produce_fails_the_run(tmp_path, design, monkeypatch):
    monkeypatch.setattr(_Maker, "run", lambda self: None)
    maker = _launch(tmp_path, design, {})
    assert not maker.succeeded
    assert maker.results["error"]["type"] == "MissingOutput"
    assert "did not produce its output `made`" in maker.results["error"]["message"]


def test_an_output_an_earlier_run_left_is_not_this_run_s(tmp_path, design, monkeypatch):
    assert _launch(tmp_path, design, {}).succeeded

    def claims_without_writing(self):
        self.outputs.made = self.run_path / "made.txt"

    monkeypatch.setattr(_Maker, "run", claims_without_writing)
    again = _launch(tmp_path, design, {}, rebuild_all=True)
    assert not again.succeeded
    assert "was not written by this run" in again.results["error"]["message"]


def test_an_output_outside_the_run_directory_fails_the_run(tmp_path, design, monkeypatch):
    elsewhere = tmp_path / "elsewhere.txt"

    def writes_elsewhere(self):
        elsewhere.write_text("x\n")
        self.outputs.made = elsewhere

    monkeypatch.setattr(_Maker, "run", writes_elsewhere)
    maker = _launch(tmp_path, design, {})
    assert not maker.succeeded
    assert "is not inside its run directory" in maker.results["error"]["message"]


def test_an_optional_output_switched_off_is_neither_required_nor_recorded(tmp_path, design):
    maker = _launch(tmp_path, design, {"write": False})
    assert maker.succeeded and maker.results["outputs"] == {}


def test_the_hand_over_gives_the_recorded_file_only_while_it_is_unchanged(tmp_path, design):
    maker = _launch(tmp_path, design, {})
    (path,) = handed_over(maker, "made")
    assert path.read_text() == "made\n"
    path.write_text("rewritten by another process\n")
    with pytest.raises(FlowDependencyFailure, match="changed since its run recorded it"):
        handed_over(maker, "made")


def test_a_reused_producer_hands_over_what_its_results_json_records(tmp_path, design):
    _launch(tmp_path, design, {})
    again = _launch(tmp_path, design, {})
    assert again.reused
    (path,) = handed_over(again, "made")
    assert path.read_text() == "made\n"


def test_an_output_nothing_recorded_is_never_handed_over(tmp_path, design):
    maker = _launch(tmp_path, design, {"write": False})
    with pytest.raises(FlowDependencyFailure, match="recorded no output `made`"):
        handed_over(maker, "made")


class _ManyMaker(_Maker):
    """Writes an ordered list of declared files."""

    results_description: ClassVar[dict[str, str]] = {}

    class Outputs(_Maker.Outputs):
        made: list[Path] = Out(SourceType.Data, description="The ordered files.")

    def run(self):
        paths = [self.run_path / name for name in ("second.txt", "first.txt")]
        for path in paths:
            path.write_text(path.name)
        self.outputs.made = paths


class _RequiredMaker(_Maker):
    """Requires exactly one declared file."""

    results_description: ClassVar[dict[str, str]] = {}

    class Outputs(_Maker.Outputs):
        made: Path = Out(SourceType.Data, description="The required file.")


class _OptionalMaker(_Maker):
    """May omit a declared file without a switch."""

    results_description: ClassVar[dict[str, str]] = {}

    class Outputs(_Maker.Outputs):
        made: Path | None = Out(SourceType.Data, description="An optional file.")


def _launch_class(tmp_path, design, cls, **launcher):
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **launcher).launch_flow(
        cls, design, {}
    )


def test_many_outputs_preserve_order_in_records_and_hand_over(tmp_path, design):
    maker = _launch_class(tmp_path, design, _ManyMaker)
    assert maker.succeeded
    entries = maker.results["outputs"]["made"]
    assert [Path(entry["path"]).name for entry in entries] == ["second.txt", "first.txt"]
    assert handed_over(maker, "made") == [Path(entry["path"]) for entry in entries]
    assert [entry["sha"] for entry in entries] == [
        content_digest(Path(entry["path"])) for entry in entries
    ]


@pytest.mark.parametrize("cls", [_ManyMaker, _RequiredMaker])
def test_required_outputs_cannot_be_absent(tmp_path, design, monkeypatch, cls):
    def no_output(self):
        if cls is _ManyMaker:
            self.outputs.made = []

    monkeypatch.setattr(cls, "run", no_output)
    maker = _launch_class(tmp_path, design, cls)
    assert not maker.succeeded
    assert maker.results["outputs"] == {}
    assert maker.results["error"]["type"] == "MissingOutput"


def test_an_unswitched_optional_output_may_be_absent(tmp_path, design, monkeypatch):
    monkeypatch.setattr(_OptionalMaker, "run", lambda self: None)
    maker = _launch_class(tmp_path, design, _OptionalMaker)
    assert maker.succeeded and maker.results["outputs"] == {}


@pytest.mark.parametrize(
    "cls, value",
    [(_Maker, []), (_Maker, [Path("a"), Path("b")]), (_ManyMaker, Path("a")), (_Maker, 42)],
)
def test_invalid_output_values_fail_usefully(tmp_path, design, monkeypatch, cls, value):
    def invalid(self):
        self.outputs = cls.Outputs.model_construct(made=value)

    monkeypatch.setattr(cls, "run", invalid)
    maker = _launch_class(tmp_path, design, cls)
    assert not maker.succeeded
    assert maker.results["error"]["type"] == "MissingOutput"
    assert "invalid" in maker.results["error"]["message"]


@pytest.mark.parametrize(
    "records",
    [
        None,
        [],
        "bad",
        42,
        {"made": []},
        {"made": "bad"},
        {"made": {}},
        {"made": {"path": 42, "sha": "bad"}},
        {"made": {"path": "made.txt", "sha": None}},
        {"made": {"path": "made.txt", "sha": ""}},
    ],
)
def test_malformed_records_are_never_handed_over(tmp_path, design, records):
    maker = _launch(tmp_path, design, {})
    maker.results["outputs"] = records
    with pytest.raises(FlowDependencyFailure, match="output `made`"):
        handed_over(maker, "made")


@pytest.mark.parametrize("record", [[], {}, [None], [{"path": 42, "sha": "bad"}]])
def test_many_output_records_require_a_nonempty_list_of_entries(tmp_path, design, record):
    maker = _launch_class(tmp_path, design, _ManyMaker)
    maker.results["outputs"]["made"] = record
    with pytest.raises(FlowDependencyFailure, match="output `made`"):
        handed_over(maker, "made")


@pytest.mark.parametrize("kind", ["missing", "directory", "external", "external_link", "relative"])
def test_hand_over_rejects_missing_and_uncontained_files(tmp_path, design, kind):
    maker = _launch(tmp_path, design, {})
    path = maker.run_path / "made.txt"
    if kind == "missing":
        path.unlink()
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind in ("external", "external_link"):
        outside = tmp_path / "outside.txt"
        outside.write_text("made\n")
        if kind == "external":
            maker.results["outputs"]["made"]["path"] = str(outside)
        else:
            path.unlink()
            path.symlink_to(outside)
    else:
        maker.results["outputs"]["made"]["path"] = "made.txt"
    with pytest.raises(FlowDependencyFailure, match="output `made`"):
        handed_over(maker, "made")


@pytest.mark.parametrize("external", [False, True])
def test_recording_checks_the_target_of_a_final_symlink(tmp_path, design, monkeypatch, external):
    def writes_link(self):
        target = (tmp_path if external else self.run_path) / "target.txt"
        target.write_text("made\n")
        link = self.run_path / "made.txt"
        link.symlink_to(target)
        self.outputs.made = link

    monkeypatch.setattr(_Maker, "run", writes_link)
    maker = _launch(tmp_path, design, {})
    if external:
        assert not maker.succeeded
        assert "is not inside its run directory" in maker.results["error"]["message"]
        assert maker.results["outputs"] == {}
    else:
        assert maker.succeeded
        (path,) = handed_over(maker, "made")
        assert path.is_symlink() and path.read_text() == "made\n"


def test_a_new_link_to_an_old_internal_file_is_not_a_new_output(tmp_path, design, monkeypatch):
    first = _launch(tmp_path, design, {})

    def old_target(self):
        link = self.run_path / "alias.txt"
        link.symlink_to(first.run_path / "made.txt")
        self.outputs.made = link

    monkeypatch.setattr(_Maker, "run", old_target)
    maker = _launch(tmp_path, design, {}, rebuild_all=True)
    assert not maker.succeeded
    assert "was not written by this run" in maker.results["error"]["message"]


def test_a_disabled_output_is_ignored_even_when_assigned(tmp_path, design, monkeypatch):
    monkeypatch.setattr(_Maker, "run", lambda self: setattr(self.outputs, "made", tmp_path / "bad"))
    maker = _launch(tmp_path, design, {"write": False})
    assert maker.succeeded and maker.results["outputs"] == {}
    maker.results["outputs"]["made"] = {"path": str(tmp_path / "bad"), "sha": "0" * 32}
    with pytest.raises(FlowDependencyFailure, match="recorded no output `made`"):
        handed_over(maker, "made")


def test_unreadable_outputs_fail_recording_and_hand_over(tmp_path, design, monkeypatch):
    maker = _launch(tmp_path, design, {})

    def unreadable(path):
        raise PermissionError(f"cannot read {path}")

    monkeypatch.setattr(output_module, "content_digest", unreadable)
    with pytest.raises(FlowDependencyFailure, match="cannot be read"):
        handed_over(maker, "made")
    again = _launch(tmp_path, design, {}, rebuild_all=True)
    assert not again.succeeded
    assert "cannot be read" in again.results["error"]["message"]
    assert again.results["outputs"] == {}


def test_a_missing_output_replaces_old_success_and_keeps_failure_identity(
    tmp_path, design, monkeypatch
):
    first = _launch(tmp_path, design, {})
    monkeypatch.setattr(_Maker, "run", lambda self: None)
    again = _launch(tmp_path, design, {}, rebuild_all=True)
    assert not again.succeeded
    document = json.loads((again.run_path / "results.json").read_text())
    assert document["success"] is False and document["outputs"] == {}
    assert document["error"]["type"] == "MissingOutput"
    assert document["design"] == "d" and document["flow"] == first.name
    for name in ("run_path", "timestamp", "flow_hash", "design_hash"):
        assert document[name]
    assert not (again.run_path / "trace.json").exists()
    with pytest.raises(FlowDependencyFailure, match="output `made`"):
        handed_over(again, "made")


def test_hand_over_of_reused_outputs_does_not_test_current_run_writes(
    tmp_path, design, monkeypatch
):
    _launch(tmp_path, design, {})
    again = _launch(tmp_path, design, {})
    assert again.reused

    def forbidden(path):
        pytest.fail("hand-over must use recorded integrity, not wrote_output")

    monkeypatch.setattr(again, "wrote_output", forbidden)
    assert handed_over(again, "made")[0].read_text() == "made\n"


def test_output_records_are_skipped_in_the_results_table(tmp_path, design, monkeypatch):
    maker = _launch(tmp_path, design, {})
    printed = []
    monkeypatch.setattr(runner_module.console, "print", lambda *args: printed.extend(args))
    runner_module.print_results(maker)
    table = printed[0]
    assert "outputs:" not in table.columns[0]._cells
    assert "Status" in table.columns[0]._cells


def test_a_failed_rerun_cannot_retain_a_handoff_record(tmp_path, design, monkeypatch):
    first = _launch(tmp_path, design, {})
    record = first.results["outputs"]["made"]
    monkeypatch.setattr(_Maker, "parse_reports", lambda self: False)
    again = _launch(tmp_path, design, {}, rebuild_all=True)
    assert not again.succeeded
    document = json.loads((again.run_path / "results.json").read_text())
    assert document["success"] is False and "outputs" not in document
    assert not (again.run_path / "trace.json").exists()
    # Even a retained record of an existing, unchanged file cannot authorize a failed producer.
    again.results["outputs"] = {"made": record}
    with pytest.raises(FlowDependencyFailure, match="recorded no output `made`"):
        handed_over(again, "made")


def test_a_partly_written_required_list_is_not_recorded(tmp_path, design, monkeypatch):
    def partial(self):
        written = self.run_path / "first.txt"
        written.write_text("first\n")
        self.outputs.made = [written, self.run_path / "missing.txt"]

    monkeypatch.setattr(_ManyMaker, "run", partial)
    maker = _launch_class(tmp_path, design, _ManyMaker)
    assert not maker.succeeded
    assert maker.results["outputs"] == {}
    assert "missing.txt" in maker.results["error"]["message"]


def test_relative_output_paths_are_recorded_absolute(tmp_path, design, monkeypatch):
    original = _Maker.run

    def relative(self):
        original(self)
        self.outputs.made = Path("made.txt")

    monkeypatch.setattr(_Maker, "run", relative)
    maker = _launch(tmp_path, design, {})
    assert maker.succeeded
    assert handed_over(maker, "made") == [maker.run_path / "made.txt"]
