"""Outputs are delivered where the user named them: copied after the run, never onto an
input, never deleting, never through a link, and never over a file of the user's without their
say; where an output goes is never part of what the run is."""

import errno
import json
import logging
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Callable, ClassVar, List, Optional

import pytest
from click.testing import CliRunner

import xeda.deliver as deliver
import xeda.digest as digest
from xeda import Design
from xeda.cli import cli
from xeda.dataclass import Field, deliverable
from xeda.design import SourceType
from xeda.deliver import (
    Conflict,
    Delivery,
    DeliveryError,
    OutputExistsError,
    delivery_record,
)
from xeda.digest import RACY_NS
from xeda.flow import Flow, FlowDependencyFailure, FlowSettingsError, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.run_root import ensure_run_root

from .tool_utils import producers_of

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
FAKE_TOOLS = Path(__file__).parent / "fake_tools"

RUNS: List[str] = []
#: what a test does while the deliverer runs (a stand-in for a user editing a file meanwhile)
DURING_RUN: List[Callable[[], object]] = []
#: what a test does while the wrapper runs, its dependency completed
DURING_WRAPPER: List[Callable[["_Wrapper"], object]] = []
_registration_before = registered_flows.copy()


def test_declared_output_collision_is_refused_before_rebuilding(tmp_path, monkeypatch):
    from .io_flows import _Maker

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})
    root = tmp_path / "runs"
    first = DefaultRunner(root, display_results=False).launch_flow(_Maker, design, {})
    assert first.succeeded and first.results["outputs"] and not first.results.get("artifacts")
    destination = tmp_path / "out"
    destination.mkdir()
    conflict = destination / "made.txt"
    conflict.write_text("user's file\n")
    monkeypatch.setattr(_Maker, "run", lambda self: pytest.fail("it ran"))

    with pytest.raises(OutputExistsError, match="made.txt"):
        DefaultRunner(
            root, display_results=False, outputs_to=destination, rebuild_all=True
        ).launch_flow(_Maker, design, {})
    assert conflict.read_text() == "user's file\n"


class _Deliverer(Flow):
    """Writes the netlist its setting names, and reports it."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        netlist: Optional[Path] = Field(
            None,
            description="The netlist it writes.",
            json_schema_extra=deliverable("outputs/{design}.v"),
        )
        report: Optional[Path] = Field(
            None,
            description="A second file it writes.",
            json_schema_extra=deliverable("outputs/{design}.rpt"),
        )
        text: str = Field("net\n", description="What it writes.")
        fail: bool = Field(False, description="Whether its reports say it failed.")
        reads: Optional[Path] = Field(None, description="A file it reads: an input.")

    class Outputs(Flow.Outputs):
        netlist: Path = Out(SourceType.Data, description="The netlist it writes.")

    def run(self) -> None:
        RUNS.append(self.name)
        assert self.settings.netlist is not None
        netlist = Path(self.settings.netlist)
        netlist.parent.mkdir(parents=True, exist_ok=True)
        netlist.write_text(self.settings.text)
        self.artifacts["netlist"] = str(netlist)
        self.outputs.netlist = netlist
        if self.settings.report is not None:
            report = Path(self.settings.report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(self.settings.text)
            self.artifacts["report"] = str(report)
        for action in DURING_RUN:
            action()

    def parse_reports(self) -> bool:
        return not self.settings.fail


class _Wrapper(Flow):
    """Reads the deliverer's netlist: the deliverer is its producer."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        reads: Optional[Path] = Field(None, description="A file it reads: an input.")
        fail: bool = Field(False, description="Whether its reports say it failed.")

    class Inputs(Flow.Inputs):
        netlist: Path = In(
            SourceType.Data,
            producer="__deliverer",
            output="netlist",
            description="The deliverer's netlist.",
        )

    def run(self) -> None:
        for action in DURING_WRAPPER:
            action(self)
        (self.run_path / "summary.txt").write_text("ok\n")
        self.artifacts["summary"] = "summary.txt"

    def parse_reports(self) -> bool:
        return not self.settings.fail


# Test-only flows, launched by class: out of the registry at once, so that no sweep over every
# registered flow (`test_documentation`, `test_flow_registry`) collected after this module finds
# them.
_REGISTERED = {
    _name: registered_flows[_name]
    for _cls in (_Deliverer, _Wrapper)
    for _name in (_cls.name, _cls.__name__)
    if _name in registered_flows
}
for _cls in (_Deliverer, _Wrapper):
    for _name in (_cls.name, _cls.__name__):
        if _name in _registration_before:
            registered_flows[_name] = _registration_before[_name]
        else:
            registered_flows.pop(_name, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A design, the user's directory xeda is started in, and where runs go."""
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "top.v").write_text("module top; endmodule\n")
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.chdir(user)
    RUNS.clear()
    DURING_RUN.clear()
    DURING_WRAPPER.clear()
    design = Design(
        name="d", design_root=tmp_path / "design", rtl={"sources": ["top.v"], "top": "top"}
    )
    # the wrapper's producer is found by name
    registered_flows.update(_REGISTERED)
    yield SimpleNamespace(user=user, design=design, root=tmp_path / "xeda_run")
    for name in _REGISTERED:
        registered_flows.pop(name, None)


def _launch(world, launcher=None, flow=_Deliverer, deliverer=None, **settings):
    """Launch `flow`; `deliverer` is what the design says of the deliverer's own settings."""
    runner = launcher or DefaultRunner(world.root, display_results=False)
    sections = {_Deliverer.name: deliverer} if deliverer else None
    flow = runner.launch_flow(flow, world.design, settings, all_flows_settings=sections)
    flow.producers = producers_of(runner, flow) if flow.declared_input_records else []
    return flow


def test_a_location_is_delivered_and_the_run_writes_the_conventional_name(world):
    flow = _launch(world, netlist="$PWD/out/net.v")
    destination = world.user / "out" / "net.v"
    assert flow.succeeded and (flow.run_path / "outputs" / "d.v").read_text() == "net\n"
    assert destination.read_text() == "net\n"
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["flow_settings"]["netlist"] == "outputs/d.v"
    assert recorded["deliveries"] == [
        {"setting": "netlist", "name": "outputs/d.v", "to": str(destination)}
    ]
    assert [(d.destination, d.state) for d in flow.deliveries] == [(destination, "delivered")]


def test_renaming_or_moving_a_destination_never_re_runs(world):
    """Delivery targets are not identity, and never what the tool is told to write."""
    first = _launch(world, netlist="$PWD/a/net.v")
    second = _launch(world, netlist="$PWD/b/renamed.txt")
    assert second.reused and second.flow_hash == first.flow_hash and len(RUNS) == 1
    assert (world.user / "b" / "renamed.txt").read_text() == "net\n"
    assert _launch(world, netlist="outputs/d.v").reused, "the conventional name, delivered nowhere"


def test_a_dependency_delivers_its_output_and_where_is_no_one_s_identity(world):
    _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/a/net.v"})
    second = _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/b/other.v"})
    assert second.reused and second.producers[0].reused
    assert (world.user / "a" / "net.v").is_file() and (world.user / "b" / "other.v").is_file()


def test_another_extension_is_copied_as_it_is_and_says_so(world, caplog):
    with caplog.at_level(logging.WARNING, logger="xeda.deliver"):
        _launch(world, netlist="$PWD/net.v.gz")
    assert (world.user / "net.v.gz").read_text() == "net\n"
    assert "outputs/d.v" in caplog.text


def test_an_up_to_date_run_delivers_again(world):
    _launch(world, netlist="$PWD/net.v")
    (world.user / "net.v").unlink()
    flow = _launch(world, netlist="$PWD/net.v")
    assert flow.reused and (world.user / "net.v").read_text() == "net\n"
    assert [d.state for d in _launch(world, netlist="$PWD/net.v").deliveries] == ["unchanged"]


def test_xeda_s_own_unchanged_copy_is_replaced(world):
    _launch(world, netlist="$PWD/net.v", text="a\n")
    flow = _launch(world, netlist="$PWD/net.v", text="b\n")
    assert not flow.reused and (world.user / "net.v").read_text() == "b\n"


def test_the_delivery_record_lies_beside_the_run_directory_and_survives_clean(world):
    flow = _launch(world, netlist="$PWD/net.v", text="a\n")
    record = delivery_record(flow.run_path)
    assert record.parent == flow.run_path.parent and record.is_file()
    cleaning = DefaultRunner(world.root, display_results=False, clean=True)
    _launch(world, cleaning, netlist="$PWD/net.v", text="b\n")
    assert (world.user / "net.v").read_text() == "b\n", "still xeda's own copy after --clean"


@pytest.fixture
def hashed(monkeypatch):
    """Every file whose content was read to digest it, in order: delivery's own reads and those
    of the records it takes (`digest.record_file`), both patched -- `deliver` imported the name,
    so patching one module catches only half the calls."""
    seen: List[Path] = []
    for module in (deliver, digest):
        original = module.content_digest

        def counted(path, _original=original):
            seen.append(Path(path))
            return _original(path)

        monkeypatch.setattr(module, "content_digest", counted)
    return seen


@pytest.fixture
def later_clock(monkeypatch):
    """The destination's file-system clock as a launch made more than the racy window after the
    delivery reads it. The marker is really made in the destination's own directory and its
    device really checked; only the time it reports is the one a later launch would see. Nothing
    is done to the record or to the file: an anchor is only ever a clock read at a moment the
    content was verified, never arithmetic on a record already held."""
    original = deliver._destination_clock

    def later(destination):
        reading = original(destination)
        return None if reading is None else replace(reading, ns=reading.ns + RACY_NS + 1)

    monkeypatch.setattr(deliver, "_destination_clock", later)


@pytest.fixture
def early_clock(monkeypatch):
    """The destination's file-system clock as a launch made while the delivery is still inside the
    racy window reads it: the real reading, never advanced, but clamped down to the latest time at
    which a record of a file changed that recently is still not settled -- `settled_before(t)` is
    `max(mtime, ctime) + RACY_NS < t`, so that boundary is false either way. The marker is really
    made and its device really checked. This makes a test assert the behavior at a chosen moment
    instead of racing a real clock, which on a shared runner under `-n auto` it would lose."""
    original = deliver._destination_clock

    def early(destination):
        reading = original(destination)
        if reading is None:
            return None
        st = os.lstat(destination)
        boundary = max(st.st_mtime_ns, st.st_ctime_ns) + RACY_NS
        return replace(reading, ns=min(reading.ns, boundary))

    monkeypatch.setattr(deliver, "_destination_clock", early)


def _reads(hashed, destination) -> int:
    """How many times the destination's content was read since the count was last cleared."""
    return sum(1 for path in hashed if path == destination)


def test_an_unchanged_delivery_reads_the_destination_once_not_at_every_launch(
    world, hashed, later_clock
):
    """A check that reads a destination anchors the record it takes to that file system's own
    clock, read just before: the next check recognizes the unchanged file by its metadata
    and reads nothing, and the delivery copies nothing either."""
    destination = world.user / "net.v"
    first = _launch(world, netlist="$PWD/net.v")
    assert _reads(hashed, destination) == 0, "nothing was there to read"
    record = json.loads(delivery_record(first.run_path).read_text())["files"]
    assert "anchor_ns" not in record[str(destination)], "just written: racy, fail-closed"

    hashed.clear()
    second = _launch(world, netlist="$PWD/net.v")
    assert [d.state for d in second.deliveries] == ["unchanged"]
    assert _reads(hashed, destination) == 1, "read once, to anchor its record"
    entry = json.loads(delivery_record(second.run_path).read_text())["files"][str(destination)]
    assert entry["anchor_ns"] > entry["recorded_ns"], "anchored by a clock, not by its own times"

    for _ in range(2):
        hashed.clear()
        again = _launch(world, netlist="$PWD/net.v")
        assert [d.state for d in again.deliveries] == ["unchanged"]
        assert _reads(hashed, destination) == 0, "its metadata vouches for it"
    assert destination.read_text() == "net\n"


def test_an_unchanged_delivery_of_a_producer_reads_the_destination_once_a_launch(
    world, hashed, later_clock, monkeypatch
):
    """A producer's deliveries are checked ahead, and again at its turn. The two checks are one
    pass over the destination: the first anchors the record it takes, and the second finds it
    anchored. The clock of the destination's file system is read, and its marker made, once."""
    markers: List[Path] = []
    original = deliver._destination_clock
    monkeypatch.setattr(deliver, "_destination_clock", lambda d: markers.append(d) or original(d))
    destination = world.user / "net.v"
    sections = {"netlist": "$PWD/net.v"}
    _launch(world, flow=_Wrapper, deliverer=sections)
    assert _reads(hashed, destination) == 0 and markers == [], "nothing was there to read"

    hashed.clear()
    second = _launch(world, flow=_Wrapper, deliverer=sections)
    assert [d.state for d in second.producers[0].deliveries] == ["unchanged"]
    assert _reads(hashed, destination) == 1, "read once, to anchor its record"
    assert len(markers) == 1, "its file system's clock was read once"

    for _ in range(2):
        hashed.clear()
        markers.clear()
        again = _launch(world, flow=_Wrapper, deliverer=sections)
        assert [d.state for d in again.producers[0].deliveries] == ["unchanged"]
        assert _reads(hashed, destination) == 0 and markers == [], "its metadata vouches for it"


def test_the_anchor_is_the_clock_of_the_destination_read_before_its_content(world, monkeypatch):
    """What anchors a record is a time the destination's own file system reported, read just
    before that content was, and nothing else. Arithmetic on the record already held -- its own
    change time plus the racy window -- would make every record look settled the moment it was
    first read, which is precisely the guarantee the window exists for: on a file system whose
    timestamps are coarse, a write during the read falls in the same tick, and the destination
    would then be trusted for ever to hold bytes it does not. So this pins the mechanism: the
    stored anchor is the clock's own answer, and the clock is read before the content."""
    destination = world.user / "net.v"
    events: List[str] = []
    answers: List[int] = []
    real_clock = deliver._destination_clock
    #: unmistakably that clock's answer: no sum of the file's own times lands on it
    far = 10**15

    def clock(path):
        reading = real_clock(path)
        if reading is None:
            return None
        events.append("clock")
        answers.append(reading.ns + far)
        return replace(reading, ns=answers[-1])

    original = digest.content_digest

    def counted(path):
        if Path(path) == destination:
            events.append("read")
        return original(path)

    monkeypatch.setattr(deliver, "_destination_clock", clock)
    monkeypatch.setattr(digest, "content_digest", counted)
    _launch(world, netlist="$PWD/net.v")
    events.clear()
    flow = _launch(world, netlist="$PWD/net.v")

    assert events == ["clock", "read"], "the clock is read before the content, never after"
    entry = json.loads(delivery_record(flow.run_path).read_text())["files"][str(destination)]
    assert entry["anchor_ns"] == answers[-1], "the anchor is what that clock reported"
    st = os.lstat(destination)
    assert entry["anchor_ns"] != max(st.st_mtime_ns, st.st_ctime_ns) + RACY_NS + 1


def test_a_delivery_still_inside_the_racy_window_is_read_at_every_launch(
    world, hashed, early_clock
):
    """While the window has not passed, the record of a file written a moment ago is settled
    before no clock that can be read, so nothing anchors it and every check reads it: an anchor
    is never manufactured from the record already held. `early_clock` pins the reading at the
    boundary, so this holds however long the launches really take."""
    destination = world.user / "net.v"
    _launch(world, netlist="$PWD/net.v")
    for _ in range(2):
        hashed.clear()
        assert [d.state for d in _launch(world, netlist="$PWD/net.v").deliveries] == ["unchanged"]
        assert _reads(hashed, destination) == 2, "the check's read, and the copy's"


def test_an_edit_with_its_mtime_restored_is_found_even_after_the_record_was_anchored(
    world, later_clock
):
    """The inode change time is what no one can set: an edit given its old mtime back still
    moves it, so the anchored record stops vouching and the content is read -- and refused."""
    destination = world.user / "net.v"
    _launch(world, netlist="$PWD/net.v")
    _launch(world, netlist="$PWD/net.v")  # reads it once, and anchors its record
    mtime = destination.stat().st_mtime_ns
    destination.write_text("theirs\n")
    os.utime(destination, ns=(destination.stat().st_atime_ns, mtime))
    assert destination.stat().st_mtime_ns == mtime

    with pytest.raises(OutputExistsError, match="changed since xeda wrote it"):
        _launch(world, netlist="$PWD/net.v")
    assert destination.read_text() == "theirs\n"


def test_another_file_of_the_same_bytes_and_mtime_in_its_place_is_refused_when_anchored(
    world, later_clock
):
    """A file put in the destination's place is another inode, which is part of what a record is
    trusted by: its metadata can never vouch, and the inode decides once the content is read."""
    destination = world.user / "net.v"
    _launch(world, netlist="$PWD/net.v")
    _launch(world, netlist="$PWD/net.v")  # anchors its record
    recorded = destination.stat()
    impostor = world.user / "theirs.v"
    impostor.write_text(destination.read_text())  # the very bytes xeda delivered
    destination.unlink()
    impostor.rename(destination)
    os.utime(destination, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
    assert destination.stat().st_size == recorded.st_size
    assert destination.stat().st_ino != recorded.st_ino

    with pytest.raises(OutputExistsError, match="changed since xeda wrote it"):
        _launch(world, netlist="$PWD/net.v")


def test_a_destination_whose_file_system_clock_cannot_be_read_is_read_at_every_launch(
    world, hashed, later_clock, monkeypatch
):
    """A marker that cannot be made where the output goes (a directory that is read only, a file
    system that refuses) anchors nothing: the check reads the content, as it always did, and
    delivery is otherwise exactly the same."""

    def refuse(directory):
        raise OSError("no marker here")

    monkeypatch.setattr(deliver, "filesystem_time_ns", refuse)
    destination = world.user / "net.v"
    _launch(world, netlist="$PWD/net.v")
    for _ in range(2):
        hashed.clear()
        assert [d.state for d in _launch(world, netlist="$PWD/net.v").deliveries] == ["unchanged"]
        assert _reads(hashed, destination) == 2
    assert destination.read_text() == "net\n"


def test_an_anchor_read_on_another_file_system_is_not_used(world, hashed, later_clock):
    """An anchor is a time on one file system's clock: a destination that moved to another is
    read again rather than judged by times that mean nothing there."""
    destination = world.user / "net.v"
    _launch(world, netlist="$PWD/net.v")
    flow = _launch(world, netlist="$PWD/net.v")  # anchors its record
    record_path = delivery_record(flow.run_path)
    record = json.loads(record_path.read_text())
    entry = record["files"][str(destination)]
    assert entry["anchor_device"] == os.lstat(destination).st_dev
    entry["anchor_device"] += 1  # as if it had been read where the file no longer is
    record_path.write_text(json.dumps(record))

    hashed.clear()
    again = _launch(world, netlist="$PWD/net.v")
    assert [d.state for d in again.deliveries] == ["unchanged"]
    assert _reads(hashed, destination) == 1, "read once, and anchored afresh"
    assert destination.read_text() == "net\n"


def test_an_anchored_destination_is_still_replaced_when_the_output_changes(world, later_clock):
    """Metadata trust says what the destination holds, never that it need not be replaced."""
    _launch(world, netlist="$PWD/net.v", text="a\n")
    _launch(world, netlist="$PWD/net.v", text="a\n")  # anchors its record
    flow = _launch(world, netlist="$PWD/net.v", text="b\n")
    assert not flow.reused and (world.user / "net.v").read_text() == "b\n"
    assert [d.state for d in flow.deliveries] == ["delivered"]


@pytest.mark.parametrize("theirs", ["edited", "foreign", "a link"])
def test_a_file_that_is_not_xeda_s_unchanged_copy_is_refused_before_the_tool_runs(
    world, tmp_path, theirs
):
    destination = world.user / "net.v"
    if theirs == "edited":
        _launch(world, netlist="$PWD/net.v", text="a\n")
        destination.write_text("my edit\n")
    elif theirs == "foreign":
        destination.write_text("mine\n")
    else:
        (tmp_path / "target.txt").write_text("target\n")
        destination.symlink_to(tmp_path / "target.txt")
    before = destination.read_text()
    RUNS.clear()
    with pytest.raises(OutputExistsError, match="--overwrite-outputs"):
        _launch(world, netlist="$PWD/net.v", text="b\n")
    assert RUNS == [] and destination.read_text() == before


@pytest.mark.parametrize("how", ["flag", "yes"])
def test_a_confirmed_replacement_replaces_a_link_as_itself(world, tmp_path, how):
    (tmp_path / "target.txt").write_text("target\n")
    destination = world.user / "net.v"
    destination.symlink_to(tmp_path / "target.txt")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=how == "flag")
    asked: List[Conflict] = []
    if how == "yes":
        launcher.confirm_overwrite = lambda conflicts: asked.extend(conflicts) or True
    _launch(world, launcher, netlist="$PWD/net.v")
    assert not destination.is_symlink() and destination.read_text() == "net\n"
    assert (tmp_path / "target.txt").read_text() == "target\n"
    assert how == "flag" or [c.why for c in asked] == ["a symbolic link xeda did not make"]


def test_a_no_at_the_prompt_keeps_the_file(world):
    (world.user / "net.v").write_text("mine\n")
    launcher = DefaultRunner(world.root, display_results=False)
    launcher.confirm_overwrite = lambda conflicts: False
    with pytest.raises(OutputExistsError):
        _launch(world, launcher, netlist="$PWD/net.v")
    assert (world.user / "net.v").read_text() == "mine\n"


def test_a_yes_is_remembered_for_the_file_as_it_was_and_only_for_that(tmp_path):
    first, second = tmp_path / "first.v", tmp_path / "second.v"
    first.write_text("one\n")
    second.write_text("two\n")
    conflicts = [
        Conflict(Delivery("k", PurePath(file.name), file), file, "yours")
        for file in (first, second)
    ]
    asked: List[List[str]] = []
    answers = iter([True, False, True])

    def ask(asking):
        asked.append([c.destination.name for c in asking])
        return next(answers)

    confirmed = deliver.ConfirmedReplacements()
    assert confirmed.confirm(ask, conflicts) and asked == [["first.v", "second.v"]]
    assert confirmed.confirm(ask, conflicts) and len(asked) == 1, "as they were: asked once"
    second.write_text("two, edited\n")
    assert not confirmed.confirm(ask, conflicts), "a no for one is a no for all"
    assert asked[1:] == [["second.v"]], "only the file that changed is asked about again"
    assert confirmed.confirm(ask, conflicts) and asked[2:] == [["second.v"]], "no was no yes"


def test_a_file_edited_while_the_question_is_open_is_not_replaced(world):
    """A yes is for the file the user was asked about, as it was when the question was asked: a
    file edited while the answer is awaited is not delivered over."""
    destination = world.user / "net.v"
    destination.write_text("mine\n")
    edited = "mine, edited while xeda waited for the answer\n"

    def yes_after_an_edit(conflicts):
        destination.write_text(edited)
        return True

    launcher = DefaultRunner(world.root, display_results=False)
    launcher.confirm_overwrite = yes_after_an_edit
    with pytest.raises(DeliveryError, match="changed while the run went on"):
        _launch(world, launcher, netlist="$PWD/net.v")
    assert destination.read_text() == edited


@pytest.mark.parametrize("spelling", ["the source", "a hard link", "a symbolic link", "a case"])
def test_an_input_is_never_a_destination_even_with_overwrite_outputs(world, spelling):
    source = world.design.root_path / "top.v"
    destination = world.user / "net.v"
    if spelling == "the source":
        destination = source
    elif spelling == "a hard link":
        os.link(source, destination)
    elif spelling == "a symbolic link":
        destination.symlink_to(source)
    else:
        destination = source.with_name("TOP.v")
        if not destination.exists():
            pytest.skip("a case-sensitive file system: TOP.v would be another file")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(world, launcher, netlist=str(destination))
    assert source.read_text() == "module top; endmodule\n" and RUNS == []


@pytest.mark.parametrize("overwrite", [False, True])
def test_an_earlier_delivery_into_a_directory_a_setting_now_reads_is_never_replaced(
    world, overwrite
):
    """An output delivered into a library directory, which a
    later launch reads through a setting naming the directory (`lib_paths`, an include
    directory), is an input of that launch -- the trace lists every file there -- so the later
    launch refuses to replace it, before its tool runs, even though the delivery record says it
    is xeda's own unchanged copy, and even with --overwrite-outputs."""
    (world.user / "lib").mkdir()
    _launch(world, netlist="$PWD/lib/net.v", text="a\n")
    assert (world.user / "lib" / "net.v").read_text() == "a\n"
    RUNS.clear()
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=overwrite)
    with pytest.raises(DeliveryError, match="`reads` names"):
        _launch(world, launcher, reads="$PWD/lib", netlist="$PWD/lib/net.v", text="b\n")
    assert (world.user / "lib" / "net.v").read_text() == "a\n" and RUNS == []


@pytest.mark.parametrize("where", ["a new file", "a new subdirectory", "a dependency's"])
def test_a_destination_inside_a_directory_a_setting_reads_is_refused_before_the_tool_runs(
    world, where
):
    """No file need be there yet: whether a tool of the launch would read it -- every file under
    the directory is an input of the run -- cannot be known before it runs, so a delivery into a
    read directory is refused outright, naming the setting and the directory."""
    lib = world.user / "lib"
    lib.mkdir()
    (lib / "cells.v").write_text("module cell; endmodule\n")
    if where == "a dependency's":
        launch = dict(flow=_Wrapper, deliverer={"reads": "$PWD/lib", "netlist": "$PWD/lib/net.v"})
        refusal = DeliveryError  # the producer's own refusal, as it is
    else:
        name = "net.v" if where == "a new file" else "sub/net.v"
        launch = dict(reads="$PWD/lib", netlist=f"$PWD/lib/{name}")
        refusal = DeliveryError
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(refusal, match=f"`reads` names {lib.resolve()}") as refused:
        _launch(world, launcher, **launch)
    assert "an input of the run" in str(refused.value)
    assert RUNS == [] and sorted(p.name for p in lib.iterdir()) == ["cells.v"]


def test_a_file_a_read_directory_reaches_through_a_link_is_never_a_destination(world, tmp_path):
    """The trace lists a read directory following its links (`listing.directory_files`), so a
    file reached through a link in it is an input wherever it lies: its own path, outside the
    directory, is no way around the guard -- and neither is a new file beside it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "cells.v").write_text("module cell; endmodule\n")
    (world.user / "lib").mkdir()
    (world.user / "lib" / "shared").symlink_to(elsewhere, target_is_directory=True)
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    for destination in (elsewhere / "cells.v", elsewhere / "new.v"):
        with pytest.raises(DeliveryError, match="an input of the run"):
            _launch(world, launcher, reads="$PWD/lib", netlist=str(destination))
    assert (elsewhere / "cells.v").read_text() == "module cell; endmodule\n"
    assert not (elsewhere / "new.v").exists() and RUNS == []


@pytest.mark.parametrize("owner", ["the requested flow", "a producer"])
@pytest.mark.parametrize("named", ["the directory", "inside it", "a link to it"])
def test_outputs_to_into_a_directory_a_setting_reads_is_refused_before_the_first_tool_runs(
    world, named, owner
):
    """Every flow of the plan registers its reads before any delivery is checked."""
    lib = world.user / "lib"
    lib.mkdir()
    (lib / "cells.v").write_text("module cell; endmodule\n")
    outputs_to = {"the directory": lib, "inside it": lib / "got", "a link to it": world.user / "l"}
    if named == "a link to it":
        outputs_to[named].symlink_to(lib, target_is_directory=True)
    launcher = DefaultRunner(
        world.root, display_results=False, outputs_to=outputs_to[named], overwrite_outputs=True
    )
    if owner == "the requested flow":
        launch = dict(reads=str(lib), deliverer={"netlist": "b/n.v"})
    else:
        launch = dict(deliverer={"netlist": "b/n.v", "reads": str(lib)})
    with pytest.raises(DeliveryError, match=r"--outputs-to names .*`reads` names"):
        _launch(world, launcher, flow=_Wrapper, **launch)
    assert RUNS == [] and sorted(p.name for p in lib.iterdir()) == ["cells.v"]


def test_a_directory_is_not_a_file_s_destination(world):
    (world.user / "net.v").mkdir()
    with pytest.raises(DeliveryError, match="a directory"):
        _launch(world, netlist="$PWD/net.v")
    assert RUNS == []


def test_a_destination_in_the_run_root_is_refused(world):
    with pytest.raises(DeliveryError, match="run root"):
        _launch(world, netlist=str(world.root / "elsewhere" / "net.v"))


def test_a_destination_in_any_run_root_is_refused(world, tmp_path):
    """Another project's `xeda_run`, a DSE root: every marked run root is xeda's."""
    other = ensure_run_root(tmp_path / "another" / "xeda_run")
    assert other is not None
    with pytest.raises(DeliveryError, match="run root"):
        _launch(world, netlist=str(other / "d" / "net.v"))
    assert RUNS == []


def test_a_name_xeda_keeps_is_refused(world):
    with pytest.raises(FlowSettingsError, match="a name xeda keeps"):
        _launch(world, netlist="results.json")  # a bare name: a location never is one


def test_a_failed_run_delivers_nothing(world):
    _launch(world, netlist="$PWD/net.v", text="a\n")
    flow = _launch(world, netlist="$PWD/net.v", text="b\n", fail=True)
    assert not flow.succeeded and (world.user / "net.v").read_text() == "a\n"


def test_a_failed_requested_flow_delivers_nothing_not_even_its_dependency_s(world):
    """Delivery is gated on the requested flow's success: its dependency succeeded and noted
    what it delivers, but the flow the launch was asked for reports failure (it returns, it does
    not raise) -- nothing is delivered, and the user's files are as they were."""
    (world.user / "net.v").write_text("mine\n")
    launcher = DefaultRunner(
        world.root, display_results=False, overwrite_outputs=True, outputs_to=world.user / "got"
    )
    flow = _launch(world, launcher, flow=_Wrapper, fail=True, deliverer={"netlist": "$PWD/net.v"})
    assert not flow.succeeded and flow.producers[0].succeeded
    assert RUNS == [_Deliverer.name], "the dependency ran, and wrote its output in its run dir"
    assert (world.user / "net.v").read_text() == "mine\n"
    assert not (world.user / "got").exists()
    assert flow.deliveries == [] and flow.producers[0].deliveries == []


def test_a_destination_changed_while_the_run_went_on_is_not_replaced(world):
    (world.user / "net.v").write_text("mine\n")
    DURING_RUN.append(lambda: (world.user / "net.v").write_text("edited while it ran\n"))
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="changed while the run went on"):
        _launch(world, launcher, netlist="$PWD/net.v")
    assert (world.user / "net.v").read_text() == "edited while it ran\n"


@pytest.mark.parametrize("swap", ["destination", "parent"])
def test_a_destination_swapped_during_the_copy_is_not_replaced(world, monkeypatch, swap):
    """Checked again right before the rename: a file put there, or its directory swapped for a
    link to the design's, while the copy was made, is left as it is -- the input untouched."""
    import xeda.deliver

    out = world.user / "out"
    out.mkdir()
    copy = xeda.deliver.copy_fd

    def copy_then_swap(source, target):
        copy(source, target)
        if swap == "destination":
            (out / "net.v").write_text("put there meanwhile\n")
        else:
            out.rename(world.user / "moved")
            out.symlink_to(world.design.root_path, target_is_directory=True)

    monkeypatch.setattr(xeda.deliver, "copy_fd", copy_then_swap)
    with pytest.raises(DeliveryError, match="changed while the run went on"):
        _launch(world, netlist="$PWD/out/net.v")
    if swap == "destination":
        assert (out / "net.v").read_text() == "put there meanwhile\n"
    else:
        assert not (world.design.root_path / "net.v").exists()
        assert (world.design.root_path / "top.v").read_text() == "module top; endmodule\n"


def test_an_oserror_mid_delivery_still_records_and_reports_the_copies_already_made(
    world, monkeypatch
):
    """One node delivering two files: an `OSError` (a full disk) copying the second must not lose
    track of the first -- its record is still written (`try`/`finally`), and `flow.deliveries`
    (reported in `--json`'s `nodes[].deliveries`) still lists it, before the error is raised."""
    import xeda.deliver

    copy = xeda.deliver.copy_fd
    made: List[bool] = []

    def copy_then_fail_second(source, target):
        copy(source, target)
        made.append(True)
        if len(made) == 2:
            raise OSError("disk full")

    monkeypatch.setattr(xeda.deliver, "copy_fd", copy_then_fail_second)
    launcher = DefaultRunner(world.root, display_results=False)
    with pytest.raises(OSError, match="disk full"):
        _launch(world, launcher, netlist="$PWD/a.v", report="$PWD/b.v")
    flow = launcher.launched[-1]
    assert (world.user / "a.v").read_text() == "net\n" and not (world.user / "b.v").exists()
    assert [d.destination for d in flow.deliveries] == [world.user / "a.v"]
    record = json.loads(delivery_record(flow.run_path).read_text())
    assert str(world.user / "a.v") in record["files"]
    assert str(world.user / "b.v") not in record["files"]


@pytest.mark.parametrize("size", [0, 1, (3 << 20) + 12345])
def test_a_delivered_file_is_byte_identical_with_its_permission_bits(world, size):
    """Larger than any copy chunk, and of a size no chunk divides, or empty: what is delivered is
    the run's file, byte for byte, with the mode the descriptor-based copy takes from it."""
    content = bytes(range(251)) * (size // 251 + 1)
    content = content[:size]

    def rewrite_output():
        (written,) = world.root.glob("**/outputs/d.v")
        written.write_bytes(content)
        written.chmod(0o751)

    DURING_RUN.append(rewrite_output)
    flow = _launch(world, netlist="$PWD/out/net.v")
    delivered = world.user / "out" / "net.v"
    assert flow.succeeded and delivered.read_bytes() == content
    assert stat.S_IMODE(delivered.stat().st_mode) == 0o751


@pytest.mark.parametrize("written_first", [1, 4096, 1 << 20])
def test_a_fast_copy_failing_after_writing_falls_back_to_a_complete_copy(
    world, monkeypatch, written_first
):
    """The kernel primitive writes some bytes, then fails: the plain loop must start from an empty
    destination at offset 0, or the delivery would be the half copy with the whole appended."""
    import xeda.utils

    content = bytes(range(251)) * 10_000  # 2.5 MB, an odd multiple of nothing here
    attempts: List[int] = []

    def half_then_fail(src_fd, dst_fd, size):
        attempts.append(os.write(dst_fd, os.pread(src_fd, written_first, 0)))
        raise OSError(errno.EIO, "failed after writing some bytes")

    monkeypatch.setattr(xeda.utils, "_fast_copies", lambda *args: [half_then_fail])

    def rewrite_output():
        (written,) = world.root.glob("**/outputs/d.v")
        written.write_bytes(content)

    DURING_RUN.append(rewrite_output)
    flow = _launch(world, netlist="$PWD/out/net.v")
    assert attempts == [written_first]  # the fast path ran, wrote bytes, and failed
    assert flow.succeeded and (world.user / "out" / "net.v").read_bytes() == content
    assert not [p for p in (world.user / "out").iterdir() if p.name != "net.v"]  # no temporary


def test_the_same_bytes_in_another_file_are_not_xeda_s_copy(world):
    """A record is a `FileRecord`: a user's file of the same content put in place of xeda's copy
    (another inode) is not xeda's, and is not replaced without a yes."""
    _launch(world, netlist="$PWD/net.v", text="a\n")
    theirs = world.user / "theirs.v"
    theirs.write_text("a\n")
    os.replace(theirs, world.user / "net.v")  # made while xeda's copy existed: another inode
    RUNS.clear()
    with pytest.raises(OutputExistsError, match="changed since xeda wrote it"):
        _launch(world, netlist="$PWD/net.v", text="b\n")
    assert RUNS == [] and (world.user / "net.v").read_text() == "a\n"


def test_outputs_to_copies_the_requested_flow_s_artifacts_only(world):
    launcher = DefaultRunner(world.root, display_results=False, outputs_to=world.user / "got")
    flow = _launch(world, launcher, flow=_Wrapper, deliverer={"netlist": "build/net.v"})
    assert (world.user / "got" / "summary.txt").read_text() == "ok\n"
    assert not (world.user / "got" / "build").exists(), "a dependency's artifacts stay put"
    assert [d.key for d in flow.deliveries] == ["--outputs-to"]


def test_outputs_to_into_the_run_root_is_refused_before_the_first_tool_runs(world):
    """A location only becomes a concrete `Delivery` -- and so reaches `check`'s
    per-destination loop -- once its flow's tool has run and reported an artifact, so without
    `check_outputs_to` this was refused only in `deliver()`, after both the dependency's and the
    depending flow's tools had already run."""
    launcher = DefaultRunner(world.root, display_results=False, outputs_to=world.root / "grab")
    with pytest.raises(DeliveryError, match="run root"):
        _launch(world, launcher, flow=_Wrapper, deliverer={"netlist": "build/net.v"})
    assert RUNS == []


def test_a_dependency_s_read_input_is_never_a_destination(world):
    """The guard covers every read input of the launched graph -- here a
    file only the dependency's settings, nested in the wrapper's, name -- flag or not."""
    kept = world.user / "summary.txt"
    kept.write_text("the dependency reads this\n")
    launcher = DefaultRunner(
        world.root, display_results=False, outputs_to=world.user, overwrite_outputs=True
    )
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(
            world,
            launcher,
            flow=_Wrapper,
            deliverer={"netlist": "build/net.v", "reads": str(kept)},
        )
    assert kept.read_text() == "the dependency reads this\n"


def test_a_dependency_never_delivers_onto_what_its_depender_reads(world):
    """Refused before the dependency's tool runs: the depender's inputs are registered first."""
    (world.user / "in.v").write_text("the wrapper reads this\n")
    launcher = DefaultRunner(world.root, display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="an input of the run"):
        _launch(
            world, launcher, flow=_Wrapper, reads="$PWD/in.v", deliverer={"netlist": "$PWD/in.v"}
        )
    assert (world.user / "in.v").read_text() == "the wrapper reads this\n" and RUNS == []


def test_a_dependency_s_output_is_delivered_once_the_launch_has_finished(world):
    seen: List[bool] = []
    DURING_WRAPPER.append(lambda wrapper: seen.append((world.user / "net.v").exists()))
    _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/net.v"})
    assert seen == [False] and (world.user / "net.v").read_text() == "net\n"


def test_a_producer_s_refused_destination_is_the_launch_s_own_error(world):
    """A refusal made before the producer's tool runs is not a failed dependency."""
    (world.user / "net.v").write_text("the user's file\n")
    with pytest.raises(OutputExistsError, match="net.v"):
        _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/net.v"})
    assert RUNS == []


def test_a_producer_whose_run_wrote_no_named_output_is_a_failed_dependency(world):
    """`collect` refuses once the producer's tool ran: the producer failed, and the launch says so
    as it does of any failed producer, naming its results.json."""
    DURING_RUN.append(lambda: next(world.root.rglob("outputs/d.rpt")).unlink())
    with pytest.raises(
        FlowDependencyFailure, match=r"dependency __deliverer failed: .*results.json"
    ) as failed:
        _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/net.v", "report": "$PWD/r.rpt"})
    cause = failed.value.__cause__
    assert type(cause) is DeliveryError and not cause.before_run
    assert "wrote no outputs/d.rpt" in str(cause)
    assert RUNS == [_Deliverer.name]


def test_a_producer_refused_before_it_is_entered_names_no_results_of_an_earlier_launch(world):
    """The results.json of an earlier launch is not this launch's: a failure message that names
    one would send the reader to the wrong document."""
    assert _launch(world, flow=_Wrapper, deliverer={"netlist": "b/n.v"}).succeeded
    producer = world.root / "d" / _Deliverer.name
    earlier = producer / "b" / "n.v"
    assert earlier.is_file() and (producer / "results.json").is_file()
    with pytest.raises(FlowDependencyFailure, match="own run directory") as refused:
        _launch(
            world,
            DefaultRunner(world.root, display_results=False),
            flow=_Wrapper,
            deliverer={"netlist": "b/n.v", "reads": str(earlier)},
        )
    assert "results.json" not in str(refused.value)


def test_a_launcher_that_ran_a_producer_before_names_no_results_of_that_run(world):
    """The same launcher launches twice. What its first launch left of the producer is not what
    the second launch's failure may point to: only a flow this launch built counts."""
    launcher = DefaultRunner(world.root, display_results=False)
    assert _launch(world, launcher, flow=_Wrapper, deliverer={"netlist": "b/n.v"}).succeeded
    producer = world.root / "d" / _Deliverer.name
    earlier = producer / "b" / "n.v"
    assert earlier.is_file() and (producer / "results.json").is_file()
    assert len(launcher.launched) == 2, "the launcher remembers the producer it ran"
    with pytest.raises(FlowDependencyFailure, match="own run directory") as refused:
        _launch(
            world,
            launcher,
            flow=_Wrapper,
            deliverer={"netlist": "b/n.v", "reads": str(earlier)},
        )
    assert "results.json" not in str(refused.value)


def test_an_output_changed_after_its_run_is_not_delivered(world):
    """A deferred delivery copies what its node's run left (the digest `collect` noted), not a
    file written into that run directory since -- as another launch there would."""

    def rewrite(wrapper):
        wrapper.inputs.netlist.write_text("other\n")

    DURING_WRAPPER.append(rewrite)
    with pytest.raises(DeliveryError, match="changed after its run"):
        _launch(world, flow=_Wrapper, deliverer={"netlist": "$PWD/net.v"})
    assert not (world.user / "net.v").exists()


def test_an_exploration_delivers_nothing(world, tmp_path):
    from xeda.flow_runner.dse.dse_runner import Dse, Optimizer

    class _NoBatch(Optimizer):
        def next_batch(self):
            return None

    dse = Dse(_NoBatch, {}, tmp_path / "dse", variations={})
    with pytest.raises(FlowSettingsError, match="nothing is delivered"):
        dse.run_flow(_Deliverer, world.design, {"netlist": str(world.user / "net.v")})


def test_a_remote_run_refuses_a_located_output_before_connecting(world, monkeypatch):
    from xeda.flow_runner import remote

    monkeypatch.setattr(remote, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(DeliveryError, match="--outputs-to"):
        remote.RemoteRunner(world.root).run_remote(
            world.design,
            "vivado_synth",
            "host",
            flow_settings=["fpga.part=xc7a12tcsg325-1", "bitstream=$PWD/top.bit"],
        )


def test_a_remote_run_checks_its_output_names_before_connecting(world, monkeypatch):
    """`results.json` would overwrite the remote's own record."""
    from xeda.flow_runner import remote

    monkeypatch.setattr(remote, "Connection", lambda *a, **k: pytest.fail("it connected"))
    with pytest.raises(FlowSettingsError, match="a name xeda keeps"):
        remote.RemoteRunner(world.root).run_remote(
            world.design,
            "yosys_fpga",
            "host",
            flow_settings=["fpga.part=LFE5U-25F-6BG381C", "netlist_json=results.json"],
        )


def test_a_remote_run_protects_every_read_input_it_can_name(world):
    """Remote delivery protects requested flow settings and every producer's own section,
    as well as the design's source files."""
    from xeda.flow_runner.remote import remote_read_inputs
    from xeda.flows import VivadoPostsynthSim

    for name in ("nested.sdf", "section.xdc", "nested_lib/a.v", "section_lib/b.v"):
        (world.user / name).parent.mkdir(exist_ok=True)
        (world.user / name).write_text("")
    settings = VivadoPostsynthSim.Settings.from_input(
        {
            "fpga": {"part": "xc7a12tcsg325-1"},
            "sdf": {"max": "$PWD/nested.sdf"},
            "lib_paths": [["work", "$PWD/nested_lib"]],
        },
        design_root=world.design.root_path,
        runner_cwd=world.user,
    )
    sections = {
        "vivado_synth": {
            "xdc_files": [str(world.user / "section.xdc")],
            "lib_paths": [["work", str(world.user / "section_lib")]],
        }
    }
    root = ensure_run_root(world.root)
    assert root is not None
    inputs = remote_read_inputs(
        world.design,
        settings,
        sections,
        [],
        flow_class=VivadoPostsynthSim,
        run_path=root / "d" / "f",
        run_root=root,
    )
    named = [world.user / "nested.sdf", world.user / "section.xdc"]
    listed = [world.user / "nested_lib" / "a.v", world.user / "section_lib" / "b.v"]
    for path in [*named, *listed, world.design.root_path / "top.v"]:
        assert inputs.find(path) is not None, path
    # and every destination inside a directory they name, a file there or not
    assert inputs.directory_of(world.user / "nested_lib" / "new.v") == (
        "lib_paths[0][1]",
        (world.user / "nested_lib").resolve(),
    )
    assert inputs.directory_of(world.user / "section_lib" / "new.v") == (
        "lib_paths[0][1]",
        (world.user / "section_lib").resolve(),
    )
    assert inputs.directory_of(world.user / "new.v") is None


def test_a_remote_run_registers_its_own_flow_s_section_as_the_launch_uses_it(world):
    """The requested flow's section is registered only as merged into its
    settings (the command line's over it), never as written -- `lib_paths` given on the command
    line replaces the section's, and the run reads only that; another flow's section is
    registered as written, since which dependencies the remote launches is not known here."""
    from xeda.flow_runner.remote import remote_read_inputs
    from xeda.flows import GhdlSim, VivadoSynth

    for name in ("old", "new", "other"):
        (world.user / name).mkdir()
    settings = VivadoSynth.Settings.from_input(
        {"fpga": {"part": "xc7a12tcsg325-1"}, "lib_paths": [["work", "$PWD/new"]]},
        design_root=world.design.root_path,
        runner_cwd=world.user,
    )
    sections = {
        VivadoSynth.name: {"lib_paths": [["work", str(world.user / "old")]]},
        GhdlSim.name: {"lib_paths": [["work", str(world.user / "other")]]},
    }
    root = ensure_run_root(world.root)
    assert root is not None
    inputs = remote_read_inputs(
        world.design,
        settings,
        sections,
        [],
        flow_class=VivadoSynth,
        run_path=root / "d" / "f",
        run_root=root,
    )
    assert inputs.directory_of(world.user / "old" / "x.v") is None
    assert inputs.directory_of(world.user / "new" / "x.v") is not None
    assert inputs.directory_of(world.user / "other" / "x.v") is not None


def test_a_failed_remote_run_delivers_nothing(world):
    """`--outputs-to` delivers a remote run's fetched artifacts only when it succeeded:
    a failed run's files never replace xeda's earlier copies."""
    from xeda.deliver import Deliveries, ReadInputs
    from xeda.flow_runner.remote import RemoteRunner

    root = ensure_run_root(world.root)
    assert root is not None
    mirror = root / "d" / "flow_0123456789abcdef"
    fetched = mirror / "artifacts"
    fetched.mkdir(parents=True)
    (fetched / "a.txt").write_text("fetched\n")
    runner = RemoteRunner(root, outputs_to=world.user / "got")
    for success in (False, True):
        results = {"success": success}
        delivery = Deliveries(mirror, root, inputs=ReadInputs())
        artifacts = {"a": "/remote/run/a.txt"}  # as the remote reported them
        runner._deliver_fetched(delivery, results, artifacts, "/remote/run", fetched)
        assert (world.user / "got" / "a.txt").exists() is success
        assert ("deliveries" in results) is success


@pytest.fixture
def sqrt_copy(tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(SQRT, work, ignore=shutil.ignore_patterns("xeda_run*"))
    monkeypatch.setenv("PATH", str(FAKE_TOOLS) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(work)
    return work


def test_outputs_to_and_overwrite_outputs_on_the_command_line(sqrt_copy):
    args = [
        "run",
        "vivado_synth",
        "sqrt.yaml",
        "-s",
        "fpga.part=xc7a12tcsg325-1",
        "--outputs-to",
        "got",
        "--json",
    ]
    first = json.loads(CliRunner().invoke(cli, args).stdout)
    delivered = first["nodes"][0]["deliveries"]
    assert first["success"] and delivered
    assert all(Path(d["to"]).is_relative_to(sqrt_copy / "got") for d in delivered)
    edited = Path(delivered[0]["to"])
    edited.write_text("my edit\n")
    refused = CliRunner().invoke(cli, args)
    document = json.loads(refused.stdout)
    assert refused.exit_code != 0 and document["error"]["type"] == "OutputExistsError"
    assert edited.read_text() == "my edit\n", "no terminal, no --overwrite-outputs: kept"
    replaced = json.loads(CliRunner().invoke(cli, [*args, "--overwrite-outputs"]).stdout)
    assert replaced["success"] and edited.read_text() != "my edit\n"


def test_the_prompt_lists_what_it_would_replace_and_defaults_to_no(monkeypatch, capsys):
    from xeda.cli import _prompt_overwrite

    destination = Path("/u/sqrt.bit")
    conflict = Conflict(
        Delivery("bitstream", PurePath("sqrt.bit"), destination),
        destination,
        "not recorded as delivered from sqrt: yours, or another run's",
    )
    monkeypatch.setattr("click.confirm", lambda text, default, err: default)
    assert _prompt_overwrite([conflict]) is False
    assert "/u/sqrt.bit" in capsys.readouterr().err
