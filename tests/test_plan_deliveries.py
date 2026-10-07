"""A launch refuses a delivery it can already tell it will refuse before any tool of the plan
runs, and leaves the requested flow's directory as it was.

The requested flow registers what every flow of the plan reads, and makes the checks every
producer would make of its own deliveries when its turn comes. So a producer's destination in a
directory a sibling producer reads, or on a user's file, is refused before the sibling's tool has
run for minutes.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, List, Optional

import pytest

from xeda import Design
from xeda.dataclass import Field, deliverable
from xeda.deliver import Deliveries, DeliveryError, OutputExistsError
from xeda.design import SourceType
from xeda.flow import Flow, In, Out, registered_flows
from xeda.flow_runner import DefaultRunner

RUNS: List[str] = []


class _ReadsDir(Flow):
    """A producer that reads a directory and writes `a.txt`."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        reads: Optional[Path] = Field(None, description="A directory it reads.")

    class Outputs(Flow.Outputs):
        a: Path = Out(SourceType.Data, description="What it writes.")

    def run(self) -> None:
        RUNS.append(self.name)
        path = self.run_path / "a.txt"
        path.write_text("a\n")
        self.outputs.a = path


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

    class Inputs(Flow.Inputs):
        a: Path = In(SourceType.Data, producer="__reads_dir", output="a", description="A.")
        b: Path = In(SourceType.Data, producer="__delivers", output="b", description="B.")

    def run(self) -> None:
        RUNS.append(self.name)


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
    registered_flows.update(_REGISTERED)
    design = Design(name="d", design_root=tmp_path / "design", rtl={"sources": [], "top": "t"})
    yield SimpleNamespace(
        user=user, lib=lib, design=design, root=tmp_path / "xeda_run", runner=_runner(tmp_path)
    )
    for name in _REGISTERED:
        registered_flows.pop(name, None)


def _runner(tmp_path: Path, **settings) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **settings)


def _launch(world, runner=None, **sections):
    """Launch `_Both` with the settings the design says for each producer."""
    runner = runner or world.runner
    return runner.launch_flow(
        _Both,
        world.design,
        {},
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


@pytest.mark.parametrize("error_class", [DeliveryError, OutputExistsError])
@pytest.mark.parametrize("before_run", [False, True])
def test_a_refusal_keeps_what_it_says_of_the_run_when_it_crosses_a_process(error_class, before_run):
    """An exception comes back from a worker process of an exploration by being pickled."""
    import pickle

    error = pickle.loads(pickle.dumps(error_class("refused", before_run=before_run)))
    assert type(error) is error_class and error.before_run is before_run
    assert str(error) == "refused"
