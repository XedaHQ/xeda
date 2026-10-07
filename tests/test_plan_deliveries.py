"""A launch refuses a delivery it can already tell it will refuse before any tool of the plan
runs, and leaves the requested flow's directory as it was.

The requested flow registers what every flow of the plan reads, and makes the checks every
producer would make of its own deliveries when its turn comes. So a producer's destination in a
directory a sibling producer reads, or on a user's file, is refused before the sibling's tool has
run for minutes. What no answer could allow is refused for every flow of the plan before the first
question is asked.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, ClassVar, List, Optional

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.dataclass import Field, deliverable
from xeda.deliver import Deliveries, DeliveryError, OutputExistsError
from xeda.design import SourceType
from xeda.flow import Flow, FlowDependencyFailure, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner

RUNS: List[str] = []
#: what a test does while the first producer's tool runs (a user editing a file meanwhile)
DURING_RUN: List[Callable[[], object]] = []


class _ReadsDir(Flow):
    """A producer that reads a directory and writes `a.txt`."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        reads: Optional[Path] = Field(None, description="A directory it reads.")
        copy: Optional[Path] = Field(
            None,
            description="A second file it writes.",
            json_schema_extra=deliverable("outputs/{design}.a"),
        )

    class Outputs(Flow.Outputs):
        a: Path = Out(SourceType.Data, description="What it writes.")

    def run(self) -> None:
        RUNS.append(self.name)
        path = self.run_path / "a.txt"
        path.write_text("a\n")
        self.outputs.a = path
        if self.settings.copy is not None:
            copy = Path(self.settings.copy)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text("a\n")
            self.artifacts["copy"] = str(copy)
        for action in DURING_RUN:
            action()


class _Delivers(Flow):
    """A producer that delivers a netlist to the location its setting names."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        netlist: Optional[Path] = Field(
            None,
            description="The netlist it writes.",
            json_schema_extra=deliverable("outputs/{design}.v"),
        )

    class Outputs(Flow.Outputs):
        b: Path = Out(SourceType.Data, description="What it writes.")

    def run(self) -> None:
        RUNS.append(self.name)
        assert self.settings.netlist is not None
        netlist = Path(self.settings.netlist)
        netlist.parent.mkdir(parents=True, exist_ok=True)
        netlist.write_text("net\n")
        self.outputs.b = netlist


class _Both(Flow):
    """Reads what both producers write: two producers launched before it, in plan order."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        report: Optional[Path] = Field(
            None,
            description="A file it writes.",
            json_schema_extra=deliverable("outputs/{design}.rpt"),
        )

    class Inputs(Flow.Inputs):
        a: Path = In(SourceType.Data, producer="__reads_dir", output="a", description="A.")
        b: Path = In(SourceType.Data, producer="__delivers", output="b", description="B.")

    def run(self) -> None:
        RUNS.append(self.name)
        if self.settings.report is not None:
            report = Path(self.settings.report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("report\n")
            self.artifacts["report"] = str(report)


# Test-only flows, found by name while a test runs and out of the registry otherwise, so that no
# sweep over every registered flow collected after this module finds them.
_REGISTERED = {
    name: registered_flows[name]
    for cls in (_ReadsDir, _Delivers, _Both)
    for name in (cls.name, cls.__name__)
    if name in registered_flows
}
for _name in _REGISTERED:
    registered_flows.pop(_name, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "design").mkdir()
    user = tmp_path / "user"
    user.mkdir()
    lib = user / "lib"
    lib.mkdir()
    (lib / "cells.v").write_text("module cell; endmodule\n")
    monkeypatch.chdir(user)
    RUNS.clear()
    DURING_RUN.clear()
    registered_flows.update(_REGISTERED)
    design = Design(name="d", design_root=tmp_path / "design", rtl={"sources": [], "top": "t"})
    yield SimpleNamespace(
        user=user, lib=lib, design=design, root=tmp_path / "xeda_run", runner=_runner(tmp_path)
    )
    for name in _REGISTERED:
        registered_flows.pop(name, None)


def _runner(tmp_path: Path, **settings) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)


def _launch(world, runner=None, own=None, **sections):
    """Launch `_Both` with `own` as its settings, and the settings the design says for each
    producer."""
    runner = runner or world.runner
    return runner.launch_flow(
        _Both,
        world.design,
        own or {},
        all_flows_settings={f"__{name}": values for name, values in sections.items()},
    )


def _directory(world) -> Path:
    return world.root / "d" / "__both"


def test_a_destination_in_a_directory_a_sibling_producer_reads_is_refused_before_any_tool(world):
    with pytest.raises(DeliveryError, match=r"`netlist` names .*`reads` names") as refused:
        _launch(
            world, reads_dir={"reads": str(world.lib)}, delivers={"netlist": str(world.lib / "n.v")}
        )
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert RUNS == [], "a tool ran before the refusal"
    assert sorted(p.name for p in world.lib.iterdir()) == ["cells.v"]
    assert not _directory(world).exists(), "the requested flow's directory was not even made"


def test_a_user_s_file_in_the_way_of_a_producer_is_refused_before_any_tool(world):
    (world.user / "n.v").write_text("the user's file\n")
    with pytest.raises(OutputExistsError, match="n.v") as refused:
        _launch(world, delivers={"netlist": "$PWD/n.v"})
    assert refused.value.before_run
    assert RUNS == []
    assert (world.user / "n.v").read_text() == "the user's file\n"


def test_a_replacement_is_asked_about_once_and_before_any_tool_runs(world):
    (world.user / "n.v").write_text("the user's file\n")
    asked: list[tuple[int, str]] = []

    def confirm(conflicts):
        asked.extend((len(RUNS), conflict.destination.name) for conflict in conflicts)
        return True

    world.runner.confirm_overwrite = confirm
    flow = _launch(world, delivers={"netlist": "$PWD/n.v"})
    assert flow.succeeded and (world.user / "n.v").read_text() == "net\n"
    assert asked == [(0, "n.v")], "asked once, before the first tool ran"


def test_a_refusal_before_a_producer_runs_leaves_the_requested_flow_s_directory_alone(
    world, tmp_path
):
    first = _launch(world, delivers={"netlist": "$PWD/one.v"})
    assert first.succeeded
    kept = {p.name: p.read_bytes() for p in _directory(world).iterdir() if p.is_file()}
    assert "trace.json" in kept and json.loads(kept["results.json"])["success"] is True
    (world.user / "two.v").write_text("the user's file\n")
    RUNS.clear()
    with pytest.raises(OutputExistsError):
        _launch(world, _runner(tmp_path), delivers={"netlist": "$PWD/two.v"})
    assert RUNS == []
    now = {p.name: p.read_bytes() for p in _directory(world).iterdir() if p.is_file()}
    assert now == kept


def test_a_refusal_a_producer_makes_at_its_turn_is_its_own_and_the_requested_flow_is_left_alone(
    world, tmp_path, monkeypatch
):
    """What the checks made ahead found fine can change before the producer's turn: the
    producer's refusal then goes through as it is, and is no failure of the flow after it."""
    first = _launch(world, delivers={"netlist": "$PWD/one.v"})
    assert first.succeeded
    kept = {p.name: p.read_bytes() for p in _directory(world).iterdir() if p.is_file()}
    check = Deliveries.check
    seen: list[Path] = []

    def check_again(self, predicted=()):
        if self.run_path.name == "__delivers":
            seen.append(self.run_path)
            if len(seen) == 2:  # the producer's own turn, after the one made ahead
                raise DeliveryError("changed since it was first checked", before_run=True)
        return check(self, predicted)

    monkeypatch.setattr(Deliveries, "check", check_again)
    RUNS.clear()
    with pytest.raises(DeliveryError, match="changed since it was first checked"):
        _launch(world, _runner(tmp_path, rebuild_all=True), delivers={"netlist": "$PWD/one.v"})
    assert len(seen) == 2
    now = {p.name: p.read_bytes() for p in _directory(world).iterdir() if p.is_file()}
    assert now == kept, "the requested flow's trace and results are as the last launch left them"


@pytest.mark.parametrize("second_answer", [False, True])
def test_a_file_changed_after_the_question_was_answered_is_asked_about_again(world, second_answer):
    """A yes is for the file the user was asked about. The checks made ahead are made again at
    the producer's turn, after the tools of the producers before it: a file edited meanwhile is
    another question."""
    destination = world.user / "n.v"
    destination.write_text("the user's file\n")
    edited = "the user's file, edited while the first tool ran\n"
    DURING_RUN.append(lambda: destination.write_text(edited))
    asked: list[tuple[list[str], str]] = []

    def confirm(conflicts):
        asked.append((list(RUNS), destination.read_text()))
        return len(asked) == 1 or second_answer

    world.runner.confirm_overwrite = confirm
    if second_answer:
        assert _launch(world, delivers={"netlist": "$PWD/n.v"}).succeeded
        assert destination.read_text() == "net\n"
    else:
        with pytest.raises(OutputExistsError):
            _launch(world, delivers={"netlist": "$PWD/n.v"})
        assert destination.read_text() == edited, "not replaced"
        assert RUNS == [_ReadsDir.name], "the producer that would replace it did not run"
    assert asked == [([], "the user's file\n"), ([_ReadsDir.name], edited)]


def _delivers_meanwhile(world, tmp_path, destination: Path) -> None:
    """While the first producer's tool runs, another launch of the second producer alone delivers
    its file to `destination`: two launches of one run directory, as two terminals would make."""
    DURING_RUN.append(
        lambda: _runner(tmp_path).launch_flow(
            _Delivers, world.design, {"netlist": str(destination)}
        )
    )


def test_a_file_another_launch_delivered_meanwhile_is_still_xeda_s_own_at_its_turn(world, tmp_path):
    """The checks made ahead for a producer are made again at its turn with the same object. What
    that object read of the delivery record when the launch started is no longer what the record
    says: the other launch has written its delivery there since."""
    destination = world.user / "n.v"
    _delivers_meanwhile(world, tmp_path, destination)
    flow = _launch(world, delivers={"netlist": str(destination)})
    assert flow.succeeded and destination.read_text() == "net\n"
    assert RUNS.count(_Delivers.name) == 1, "the other launch ran it, and this one found it fresh"


def test_a_record_another_launch_wrote_meanwhile_is_not_lost_when_this_launch_writes_it(
    world, tmp_path
):
    """What this launch writes of the record is what it read under the producer's lock plus its
    own deliveries: an entry the other launch made in between stays."""
    theirs, ours = world.user / "two.v", world.user / "three.v"
    _delivers_meanwhile(world, tmp_path, theirs)
    assert _launch(world, delivers={"netlist": str(ours)}).succeeded
    RUNS.clear()
    again = _runner(tmp_path).launch_flow(_Delivers, world.design, {"netlist": str(theirs)})
    assert again.succeeded, "its file is xeda's own: no question, no refusal"
    assert theirs.read_text() == "net\n" and ours.read_text() == "net\n"


def test_the_question_about_the_requested_flow_s_file_comes_before_any_tool_runs(world):
    """The requested flow's own file is asked about with the producers', before a tool of the
    plan runs: its check is the first thing its launch does, and the producers are launched after
    it."""
    (world.user / "n.v").write_text("the user's file\n")
    (world.user / "r.rpt").write_text("the user's report\n")
    asked: list[tuple[list[str], list[str]]] = []

    def confirm(conflicts):
        asked.append((list(RUNS), [conflict.destination.name for conflict in conflicts]))
        return True

    world.runner.confirm_overwrite = confirm
    flow = _launch(
        world, own={"report": str(world.user / "r.rpt")}, delivers={"netlist": "$PWD/n.v"}
    )
    assert flow.succeeded
    assert asked == [([], ["n.v"]), ([], ["r.rpt"])], "each asked once, with no tool run yet"
    assert (world.user / "r.rpt").read_text() == "report\n"
    assert (world.user / "n.v").read_text() == "net\n"


def test_declining_the_replacement_of_the_requested_flow_s_file_runs_no_tool(world):
    (world.user / "r.rpt").write_text("the user's report\n")
    asked: list[list[str]] = []

    def decline(conflicts):
        asked.append([conflict.destination.name for conflict in conflicts])
        return False

    world.runner.confirm_overwrite = decline
    with pytest.raises(OutputExistsError, match="r.rpt") as refused:
        _launch(world, own={"report": str(world.user / "r.rpt")}, delivers={"netlist": "$PWD/n.v"})
    assert refused.value.before_run and asked == [["r.rpt"]]
    assert RUNS == [], "no producer ran for a launch that was declined"
    assert (world.user / "r.rpt").read_text() == "the user's report\n"


def _asking(runner) -> list[str]:
    """Answer yes to every question of `runner`; the names of the files asked about."""
    asked: list[str] = []

    def confirm(conflicts):
        asked.extend(conflict.destination.name for conflict in conflicts)
        return True

    runner.confirm_overwrite = confirm
    return asked


def test_a_refused_outputs_to_comes_before_the_question_about_a_producer_s_file(world, tmp_path):
    (world.user / "n.v").write_text("the user's file\n")
    (world.user / "out.txt").write_text("a file, not a directory\n")
    runner = _runner(tmp_path, outputs_to=world.user / "out.txt")
    asked = _asking(runner)
    with pytest.raises(DeliveryError, match="--outputs-to") as refused:
        _launch(world, runner, delivers={"netlist": "$PWD/n.v"})
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert asked == [] and RUNS == []
    assert (world.user / "n.v").read_text() == "the user's file\n"


def test_a_refusal_of_the_requested_flow_comes_before_the_question_about_a_producer_s_file(world):
    (world.user / "n.v").write_text("the user's file\n")
    (world.user / "reports").mkdir()
    asked = _asking(world.runner)
    with pytest.raises(DeliveryError, match="a directory") as refused:
        _launch(
            world,
            own={"report": str(world.user / "reports")},
            delivers={"netlist": "$PWD/n.v"},
        )
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert asked == [] and RUNS == []


def test_a_refusal_of_a_later_producer_comes_before_the_question_about_an_earlier_one(world):
    (world.user / "c.a").write_text("the user's file\n")
    (world.user / "n").mkdir()
    asked = _asking(world.runner)
    with pytest.raises(DeliveryError, match="a directory") as refused:
        _launch(world, reads_dir={"copy": "$PWD/c.a"}, delivers={"netlist": str(world.user / "n")})
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert asked == [] and RUNS == []
    assert (world.user / "c.a").read_text() == "the user's file\n"


@pytest.mark.parametrize("sharers", ["two producers", "a producer and the requested flow"])
def test_a_destination_two_flows_of_the_plan_name_is_refused_before_any_tool(world, sharers):
    """Every delivery of a launch is known from the plan, so a destination named twice is refused
    before the first tool runs, naming both settings, instead of delivering one output and
    refusing the other as "changed while the run went on" after all the tools had run."""
    same = world.user / "same.out"
    if sharers == "two producers":
        own, sections = {}, dict(reads_dir={"copy": str(same)}, delivers={"netlist": str(same)})
        first, second = "__reads_dir.copy", "__delivers.netlist"
    else:
        own, sections = {"report": str(same)}, dict(delivers={"netlist": str(same)})
        first, second = "__delivers.netlist", "__both.report"
    with pytest.raises(DeliveryError) as refused:
        _launch(world, own=own, **sections)
    # in the order the flows run
    assert f"`flows.{first}` and `flows.{second}` both name {same}" in str(refused.value)
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert RUNS == [] and not same.exists()
    assert not _directory(world).exists(), "the requested flow's directory was not even made"


def _two_directories_one_a_link_later(world):
    a, b = world.user / "a", world.user / "b"
    a.mkdir()
    b.mkdir()

    def link() -> None:
        b.rmdir()
        b.symlink_to(a, target_is_directory=True)

    return a, b, link


def test_a_directory_that_becomes_a_link_during_the_run_is_caught_before_anything_is_copied(world):
    """The names of two deliveries are compared again when the flow that makes them has run
    (`refuse_shared_destinations`), as they are then: the directory `b` is `a` by now, so the
    second producer's destination is the first's, and no file is copied."""
    a, b, link = _two_directories_one_a_link_later(world)
    DURING_RUN.append(link)
    with pytest.raises(FlowDependencyFailure, match="both name") as refused:
        _launch(world, reads_dir={"copy": str(a / "x.out")}, delivers={"netlist": str(b / "x.out")})
    assert "`flows.__reads_dir.copy` and `flows.__delivers.netlist`" in str(refused.value)
    assert not (a / "x.out").exists(), "nothing was delivered"


def test_two_names_of_one_file_that_only_delivery_can_tell_are_reported_as_two_deliveries(
    world, monkeypatch
):
    """What no comparison of names sees -- here a link made between two deliveries, as a file
    system that takes two Unicode forms for one name would do -- delivery finds in the file: the
    one the other delivery made. It says that two deliveries name it, never that something
    changed it while the run went on."""
    a, b, link = _two_directories_one_a_link_later(world)
    deliver = Deliveries.deliver

    def deliver_then_link(self):
        made = deliver(self)
        if self.run_path.name == "__reads_dir":
            link()
        return made

    monkeypatch.setattr(Deliveries, "deliver", deliver_then_link)
    with pytest.raises(DeliveryError) as refused:
        _launch(world, reads_dir={"copy": str(a / "x.out")}, delivers={"netlist": str(b / "x.out")})
    message = str(refused.value)
    assert "`flows.__delivers.netlist` names" in message
    assert "which `flows.__reads_dir.copy` delivered" in message
    assert "changed while the run went on" not in message
    assert not refused.value.before_run, "only delivery could tell"
    assert (a / "x.out").read_text() == "a\n", "what the first delivered stays"


def test_a_destination_two_flows_name_is_refused_before_the_question_about_a_file_in_the_way(world):
    same = world.user / "same.out"
    same.write_text("the user's file\n")
    asked = _asking(world.runner)
    with pytest.raises(DeliveryError, match="both name") as refused:
        _launch(world, reads_dir={"copy": str(same)}, delivers={"netlist": str(same)})
    assert type(refused.value) is DeliveryError and refused.value.before_run
    assert asked == [] and RUNS == []
    assert same.read_text() == "the user's file\n"


def test_the_requested_flow_reads_not_run_after_a_refusal_at_a_producer_s_turn(world, monkeypatch):
    """The refusal comes before the producer's tool, so the flow that asked for it did not run
    either: the document says so, as it says of the producer."""
    check = Deliveries.check
    seen: list[Path] = []

    def check_again(self, predicted=()):
        if self.run_path.name == "__delivers":
            seen.append(self.run_path)
            if len(seen) == 2:  # the producer's own turn, after the one made ahead
                raise DeliveryError("changed since it was first checked", before_run=True)
        return check(self, predicted)

    monkeypatch.setattr(Deliveries, "check", check_again)
    design = world.design.root_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": [], "top": "t"},
                "flows": {"__delivers": {"netlist": str(world.user / "n.v")}},
            }
        )
    )
    result = CliRunner().invoke(
        cli, ["run", "__both", str(design), "--run-root", str(world.root), "--json"]
    )
    document = json.loads(result.stdout)
    assert result.exit_code != 0 and document["success"] is False
    assert RUNS == [_ReadsDir.name]
    states = {node["node"]: node["state"] for node in document["nodes"]}
    assert states == {"__reads_dir": "ran", "__delivers": "not run", "__both": "not run"}


@pytest.mark.parametrize("error_class", [DeliveryError, OutputExistsError])
@pytest.mark.parametrize("before_run", [False, True])
def test_a_refusal_keeps_what_it_says_of_the_run_when_it_crosses_a_process(error_class, before_run):
    """An exception comes back from a worker process of an exploration by being pickled."""
    import pickle

    error = pickle.loads(pickle.dumps(error_class("refused", before_run=before_run)))
    assert type(error) is error_class and error.before_run is before_run
    assert str(error) == "refused"
