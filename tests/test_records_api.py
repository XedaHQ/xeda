"""`Workspace.load`, `records` and `scrub`, and `RunRecord.open()`: read a stored run under leases.

The oracles are the files and the locks. A record opens only while its `results.json` holds its
`run_id`, and the leases keep every later launch, clean-up or scrub out until the block ends: a
test that waits for such a launch waits on the hook of the lock, never on time. A source that a
generator or a converter wrote is handed out only while its file holds the bytes that the run
read.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.dataclass import Field
from xeda.design import SourceType
from xeda.errors import (
    FlowNotFoundError,
    LeaseHeldError,
    RecordError,
    RunReplacedError,
    RunRootError,
)
from xeda.events import Event
from xeda.flow import Flow, In, MetricSpec, Out
from xeda.flow_runner import run_lock
from xeda.flow_runner.run_lock import lock_file
from xeda.flow_runner.scrub import scrub_generations
from xeda.records import InputRecord, OpenRun, RecordProblem, RunListing, RunRecord
from xeda.spec import Spec
from xeda.utils import replacing_file
from xeda.workspace import Workspace

from .cancel_flows import cut_design, cut_flows
from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .generation_world import World, input_reader_flow, reader_flow
from .io_flows import TMaker, TTaker  # noqa: F401  (flows that the tests run by name)
from .test_dependency_generation import Chain

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

#: A launch or a lock that nothing holds up ends within this many seconds, or a test fails.
PROMPT = 60.0


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def stamp_flow() -> type[Flow]:
    """Define a flow that writes its run id into `made.txt`, and optionally an `extra.txt`.

    It has an output of each cardinality: `made` (one), `extra` (optional, written with
    `write_extra`) and `many` (two files). Call it inside `preserved_flow_registry`."""

    class TStamp(Flow):
        """Writes the id of its run into its outputs, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            write_extra: bool = Field(False, description="Whether the run writes `extra.txt`.")

        class Outputs(Flow.Outputs):
            made: Path = Out(SourceType.Data, description="The file with the run id.")
            extra: Path | None = Out(SourceType.Data, description="An optional file.")
            many: list[Path] = Out(SourceType.Data, description="Two files.")

        def run(self) -> None:
            names = ["made.txt", "extra.txt", "one.txt", "two.txt"]
            for name in names:
                with replacing_file(self.run_directory.writable(self.run_path / name)) as f:
                    f.write(f"{self.run_id}:{name}\n")
            self.outputs.made = self.run_path / "made.txt"
            if self.settings.write_extra:
                self.outputs.extra = self.run_path / "extra.txt"
            self.outputs.many = [self.run_path / "one.txt", self.run_path / "two.txt"]
            self.artifacts.note = self.run_path / "extra.txt"

    return TStamp


def stamped(tmp_path: Path, **settings) -> tuple[Workspace, Spec]:
    """Return a workspace, and the spec of `t_stamp` with `settings`, in `tmp_path`."""
    stamp_flow()
    design = cut_design(tmp_path / "design")
    spec = Spec("t_stamp", design, settings={"t_stamp": settings} if settings else {})
    return Workspace(tmp_path / "xeda_run"), spec


def target_design(tmp_path: Path, target: str | None) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "d.yaml").write_text('name: d\nrtl: {sources: [], top: "t"}\ntargets: {a: {}, b: {}}\n')
    return Design.load(root / "d.yaml", target=target)


def rewrite_run_id(run_dir: Path, run_id: str) -> None:
    """Replace `results.json` of `run_dir` by a file of another run."""
    path = run_dir / "results.json"
    document = json.loads(path.read_text())
    document["run_id"] = run_id
    replacement = path.with_name("results.new")
    replacement.write_text(json.dumps(document))
    os.replace(replacement, path)


# ------------------------------------------------------------------------------------- load


def test_load_reads_the_record_that_a_run_directory_holds(tmp_path):
    workspace, spec = stamped(tmp_path)
    result = workspace.run(spec)
    assert result.passed
    run_dir = result.record.run_dir
    record = workspace.load(run_dir)
    assert record.run_id == result.record.run_id and record.flow == "t_stamp"
    assert record.run_dir == run_dir and record.run_root == workspace.run_root
    assert workspace.load(str(run_dir)) == record


def test_load_finds_the_run_root_of_a_run_directory_of_a_target(tmp_path):
    workspace = Workspace(tmp_path / "xeda_run")
    for target in ("a", None):
        assert workspace.run(Spec("t_maker", target_design(tmp_path, target))).passed
    with_target = workspace.load(workspace.run_root / "d" / "a" / "t_maker")
    without = workspace.load(workspace.run_root / "d" / "t_maker")
    assert (with_target.target, without.target) == ("a", None)
    assert with_target.run_root == without.run_root == workspace.run_root


@pytest.mark.parametrize(
    "content, code",
    [
        (None, "missing_record"),
        ("{", "corrupt_record"),
        ("[]", "corrupt_record"),
        ('{"success": true}', "unsupported_schema"),
        ('{"schema_version": 99}', "unsupported_schema"),
    ],
)
def test_load_names_why_a_directory_holds_no_record(tmp_path, content, code):
    workspace = Workspace(tmp_path / "xeda_run")
    run_dir = workspace.run_root / "d" / "t_stamp"
    run_dir.mkdir(parents=True)
    if content is not None:
        (run_dir / "results.json").write_text(content)
    with pytest.raises(RecordError) as raised:
        workspace.load(run_dir)
    assert raised.value.code == code and raised.value.path == run_dir / "results.json"


# --------------------------------------------------------------------------------- records


def test_records_lists_problems_without_raising(tmp_path):
    workspace, spec = stamped(tmp_path)
    assert workspace.run(spec).passed
    root = workspace.run_root / "d"
    (root / "a_corrupt").mkdir()
    (root / "a_corrupt" / "results.json").write_text("{")
    (root / "b_old").mkdir()
    (root / "b_old" / "results.json").write_text('{"success": true}')
    (root / "c_unfinished").mkdir()
    (root / "c_unfinished" / "settings.json").write_text("{}")
    listing = workspace.records()
    assert isinstance(listing, RunListing)
    assert [record.run_dir for record in listing] == [root / "t_stamp"] and len(listing) == 1
    assert all(isinstance(problem, RecordProblem) for problem in listing.problems)
    assert [(problem.path.parent.name, problem.code) for problem in listing.problems] == [
        ("a_corrupt", "corrupt_record"),
        ("b_old", "unsupported_schema"),
        ("c_unfinished", "missing_record"),
    ]
    assert all(problem.message for problem in listing.problems)


def test_records_selects_by_design_flow_and_target(tmp_path):
    workspace = Workspace(tmp_path / "xeda_run")
    for target in ("a", "b", None):
        assert workspace.run(Spec("t_taker", target_design(tmp_path, target))).passed
    other = cut_design(tmp_path / "other")
    other = Design(name="e", root=other.root, rtl={"sources": ["a.v"], "top": "a"})
    assert workspace.run(Spec("t_maker", other)).passed
    (workspace.run_root / ".generated" / "d" / "key").mkdir(parents=True)

    def found(**selection) -> list[tuple[str, str | None, str]]:
        listing = workspace.records(**selection)
        assert listing.problems == ()
        return [(r.design, r.target, r.flow) for r in listing]

    everything = [  # sorted by run directory: a target's below the design, then the design's own
        ("d", "a", "t_maker"),
        ("d", "a", "t_taker"),
        ("d", "b", "t_maker"),
        ("d", "b", "t_taker"),
        ("d", None, "t_maker"),
        ("d", None, "t_taker"),
        ("e", None, "t_maker"),
    ]
    assert found() == everything
    assert found(design_name="d") == [row for row in everything if row[0] == "d"]
    assert found(design_name="d", flow="t_taker") == [
        row for row in everything if row[0] == "d" and row[2] == "t_taker"
    ]
    assert found(flow="t_maker") == [row for row in everything if row[2] == "t_maker"]
    assert found(design_name="d", target=None) == [("d", None, "t_maker"), ("d", None, "t_taker")]
    assert found(design_name="d", target="b") == [("d", "b", "t_maker"), ("d", "b", "t_taker")]
    assert found(design_name="missing") == []
    assert found(design_name="d", target="missing") == []


def test_records_of_a_run_root_that_does_not_exist_is_empty(tmp_path):
    listing = Workspace(tmp_path / "xeda_run").records()
    assert (listing.records, listing.problems) == ((), ())


def test_records_lists_a_run_root_that_cannot_be_read_as_an_error(tmp_path):
    workspace, spec = stamped(tmp_path)
    assert workspace.run(spec).passed
    workspace.run_root.chmod(0o000)
    try:
        if os.access(workspace.run_root, os.R_OK):
            pytest.skip("the process reads anything (privileged)")
        with pytest.raises(RunRootError) as raised:
            workspace.records()
        assert raised.value.code == "invalid_run_root"
    finally:
        workspace.run_root.chmod(0o755)


# ------------------------------------------------------------------------------------- open


def test_open_hands_out_the_files_of_the_run_by_absolute_path(tmp_path):
    workspace, spec = stamped(tmp_path, write_extra=True)
    record = workspace.run(spec).record
    with record.open() as run:
        assert isinstance(run, OpenRun)
        assert run.record == record and run.run_dir == record.run_dir
        assert run.output("made").read_text() == f"{record.run_id}:made.txt\n"
        assert run.output("extra") == record.run_dir / "extra.txt"
        assert run.outputs["many"] == (record.run_dir / "one.txt", record.run_dir / "two.txt")
        assert run.artifacts == {"note": record.run_dir / "extra.txt"}
        assert all(path.is_absolute() for paths in run.outputs.values() for path in paths)
        assert run.generated_sources == {} and run.missing_generated == ()


def test_output_raises_for_an_unwritten_optional_and_a_many_output(tmp_path):
    workspace, spec = stamped(tmp_path)
    record = workspace.run(spec).record
    with record.open() as run:
        assert run.outputs["extra"] == ()
        with pytest.raises(RecordError) as unwritten:
            run.output("extra")
        assert unwritten.value.code == "not_written"
        with pytest.raises(RecordError) as several:
            run.output("many")
        assert several.value.code == "several_files"
        with pytest.raises(KeyError):
            run.output("nothing")
        assert run.output("made") == record.run_dir / "made.txt"


def test_opening_a_replaced_or_purged_record_raises(tmp_path):
    workspace, spec = stamped(tmp_path)
    record = workspace.run(spec).record
    assert workspace.run(spec, rebuild_all=True).passed
    replacement = workspace.load(record.run_dir)
    assert replacement.run_id != record.run_id
    with pytest.raises(RunReplacedError) as raised:
        with record.open():
            pytest.fail("the record of an earlier run must not open")
    assert raised.value.expected_run_id == record.run_id
    assert raised.value.found_run_id == replacement.run_id
    assert raised.value.path == record.run_dir / "results.json"
    with replacement.open() as run:
        assert run.record == replacement

    assert workspace.run(spec, rebuild_all=True, post_cleanup_purge=True).passed
    assert not record.run_dir.exists()
    before = sorted(workspace.run_root.rglob("*"))
    with pytest.raises(RecordError) as purged:
        with replacement.open():
            pytest.fail("the record of a purged run must not open")
    assert purged.value.code == "missing_record"
    assert sorted(workspace.run_root.rglob("*")) == before, "nothing is made for a lost record"


def test_a_rebuild_waits_while_a_record_is_open(tmp_path, monkeypatch):
    workspace, spec = stamped(tmp_path)
    record = workspace.run(spec).record
    waits, outcome = threading.Event(), {}

    def hook(path: Path) -> None:
        if path == lock_file(record.run_dir):
            waits.set()

    monkeypatch.setattr(run_lock, "wait_hook", hook)

    def rebuild() -> None:
        outcome["result"] = workspace.run(spec, rebuild_all=True)

    thread = threading.Thread(target=rebuild, daemon=True)
    with record.open() as run:
        before = run.output("made").read_bytes()
        thread.start()
        assert waits.wait(PROMPT), "the rebuild must wait for the lease"
        assert workspace.load(record.run_dir).run_id == record.run_id
        assert run.output("made").read_bytes() == before
        assert "result" not in outcome
    thread.join(PROMPT)
    assert not thread.is_alive() and outcome["result"].passed
    assert workspace.load(record.run_dir).run_id != record.run_id


def test_open_checks_the_run_id_after_it_holds_the_lease(tmp_path, monkeypatch):
    workspace, spec = stamped(tmp_path)
    record = workspace.run(spec).record
    taken = []

    def hook(path: Path) -> None:
        taken.append(path)
        if path == lock_file(record.run_dir):
            rewrite_run_id(record.run_dir, "f" * 32)

    monkeypatch.setattr(run_lock, "acquired_hook", hook)
    with pytest.raises(RunReplacedError) as raised:
        with record.open():
            pytest.fail("a record that was replaced after the lease must not open")
    assert raised.value.found_run_id == "f" * 32 and taken == [lock_file(record.run_dir)]


def test_a_callback_under_locks_reopens_a_held_record_and_nothing_else(tmp_path):
    cut_flows()
    design = cut_design(tmp_path / "design")
    other_design = Design(name="e", root=design.root, rtl={"sources": ["a.v"], "top": "a"})
    seen: list[str] = []
    workspace = Workspace(tmp_path / "xeda_run")
    other = workspace.run(Spec("cut_producer", other_design)).record
    run_dirs = {name: workspace.run_root / "d" / name for name in ("cut_producer", "cut_consumer")}

    def probe(where: str, node: str) -> None:
        held = workspace.load(run_dirs[node])
        with held.open() as run:
            assert run.run_dir == run_dirs[node] and run.artifacts
        with pytest.raises(LeaseHeldError) as record_error:
            with other.open():
                pytest.fail("a record that the launch does not hold must not open")
        with pytest.raises(LeaseHeldError) as scrub_error:
            workspace.scrub(design_name="e")
        assert (record_error.value.code, scrub_error.value.code) == ("lease_held", "lease_held")
        seen.append(where)

    def on_event(event: Event) -> None:
        if event.kind == "node_finished" and event.node == "cut_producer":
            probe("event", "cut_producer")

    spec = Spec("cut_consumer", design)
    assert workspace.run(spec, on_event=on_event).passed
    assert seen == ["event"]

    # the same at delivery, where the launch holds the lock of the node that delivers and asks
    # `confirm` about a file that appeared in the way while the node ran
    out = tmp_path / "out"

    class TLate(Flow):
        """Writes a file, and puts a file of the user where its delivery goes."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        def run(self) -> None:
            made = self.run_path / "outputs" / "late.txt"
            made.parent.mkdir(exist_ok=True)
            made.write_text("late\n")
            self.artifacts.late = made
            (out / "outputs").mkdir(parents=True, exist_ok=True)
            (out / "outputs" / "late.txt").write_text("yours\n")

    run_dirs["t_late"] = workspace.run_root / "d" / "t_late"

    def confirm(question: str) -> bool:
        probe("confirm", "t_late")
        return True

    asking = Workspace(workspace.run_root, confirm=confirm)
    delivered = asking.run(Spec("t_late", design), outputs_to=out)
    assert delivered.passed, delivered.error
    assert seen == ["event", "confirm"]
    assert (out / "outputs" / "late.txt").read_text() == "late\n", "the answer was yes"
    assert asking.records().problems == ()  # nothing stays locked or half written
    with other.open():
        pass


# --------------------------------------------------------------- generated sources of a run


def generated_world(tmp_path: Path) -> tuple[World, Workspace, RunRecord]:
    world = World(tmp_path)
    workspace = Workspace(world.run_root)
    result = workspace.run(Spec(input_reader_flow().name, world.load()))
    assert result.passed
    return world, workspace, result.record


def test_a_storage_that_was_gone_at_the_lease_hands_out_nothing(tmp_path, monkeypatch):
    world, _workspace, record = generated_world(tmp_path)
    (storage,) = world.storages()
    aside = storage.with_name(storage.name + ".aside")
    storage.rename(aside)  # as while another launch generates it again

    def hook(path: Path) -> None:
        if path == lock_file(record.run_dir) and aside.exists():
            aside.rename(storage)  # back, with the same bytes, but after the leases were chosen

    monkeypatch.setattr(run_lock, "acquired_hook", hook)
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (
            "$XEDA_GENERATED/gen/top.v",
        )


def test_the_record_names_the_generated_source_and_its_producer(tmp_path):
    world, _workspace, record = generated_world(tmp_path)
    (row,) = record.inputs
    (source,) = row.sources
    assert (source.path, source.generated, source.producer) == (
        "$XEDA_GENERATED/gen/top.v",
        True,
        None,
    )
    assert source.generation == world.storages()[0].name
    assert source.digest and source.dependency is None
    document = record.to_dict()["inputs"][0]["sources"][0]
    assert document["producer"] is None and document["path"] == "$XEDA_GENERATED/gen/top.v"


def test_open_hands_out_a_generated_source_that_holds_the_bytes_of_the_run(tmp_path):
    world, _workspace, record = generated_world(tmp_path)
    (storage,) = world.storages()
    with record.open() as run:
        assert run.generated_sources == {"$XEDA_GENERATED/gen/top.v": storage / "gen" / "top.v"}
        assert run.missing_generated == ()


def test_a_regenerated_source_is_missing_not_handed_out(tmp_path):
    world, _workspace, record = generated_world(tmp_path)
    (storage,) = world.storages()
    (storage / "gen" / "top.v").write_text("// regenerated\n")
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (
            "$XEDA_GENERATED/gen/top.v",
        )
        assert run.record == record


def test_a_scrubbed_generation_storage_makes_its_sources_missing(tmp_path):
    world, workspace, record = generated_world(tmp_path)
    shutil.rmtree(world.storages()[0])
    before = sorted(workspace.run_root.rglob("*"))
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (
            "$XEDA_GENERATED/gen/top.v",
        )
    assert sorted(workspace.run_root.rglob("*")) == before


def test_an_open_record_keeps_a_scrub_from_removing_its_generation_storage(tmp_path, monkeypatch):
    world, workspace, record = generated_world(tmp_path)
    (storage,) = world.storages()
    waits, outcome = threading.Event(), {}
    monkeypatch.setattr(
        run_lock, "wait_hook", lambda path: waits.set() if path == lock_file(storage) else None
    )
    thread = threading.Thread(
        target=lambda: outcome.update(
            result=scrub_generations("generated", run_root=world.run_root, confirm=lambda c: True)
        ),
        daemon=True,
    )
    with record.open() as run:
        thread.start()
        assert waits.wait(PROMPT), "the scrub must wait for the lease of the storage"
        assert run.generated_sources["$XEDA_GENERATED/gen/top.v"].read_text().startswith("// one")
        assert storage.is_dir()
    thread.join(PROMPT)
    assert not thread.is_alive() and storage in outcome["result"].removed
    assert not storage.exists()


def test_one_input_hands_out_the_sources_of_two_generators(tmp_path):
    chain = Chain(tmp_path, own=True)

    class TListReader(Flow):
        """Takes all its Verilog sources through one input, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            files: list[Path] = In(SourceType.Verilog, description="The Verilog sources.")

        def run(self) -> None:
            pass

    result = Workspace(chain.run_root).run(Spec("t_list_reader", chain.load()))
    assert result.passed, result.error
    (own,), (library,) = chain.storages("top"), chain.storages("lib1")
    with result.record.open() as run:
        assert run.generated_sources == {
            "$XEDA_GENERATED/gen/top.v": own / "gen" / "top.v",
            "lib1:$XEDA_GENERATED/gen/lib1.v": library / "gen" / "lib1.v",
        }
        assert run.missing_generated == ()


def test_a_dependency_source_is_found_in_its_storage_beside_a_converter_of_its_name(tmp_path):
    chain = Chain(tmp_path)

    class TListReader(Flow):
        """Takes all its Verilog sources through one input, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            files: list[Path] = In(SourceType.Verilog, description="The Verilog sources.")

        def run(self) -> None:
            pass

    result = Workspace(chain.run_root).run(Spec("t_list_reader", chain.load()))
    assert result.passed, result.error
    record = result.record
    (row,) = record.inputs
    converter = dataclasses.replace(
        row,
        name="design",
        origin="producer",
        producer="lib1",
        output="design",
        producer_run_dir=record.run_dir.parent / "lib1",
        sources=(),
    )  # a converter node that has the name of the dependency's design
    named = dataclasses.replace(record, inputs=(converter, *record.inputs))
    (storage,) = chain.storages("lib1")
    with named.open() as run:
        assert run.generated_sources == {
            "lib1:$XEDA_GENERATED/gen/lib1.v": storage / "gen" / "lib1.v"
        }


def test_a_dependency_generator_source_is_named_by_its_design(tmp_path):
    chain = Chain(tmp_path)

    class TListReader(Flow):
        """Takes all its Verilog sources through one input, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            files: list[Path] = In(SourceType.Verilog, description="The Verilog sources.")

        def run(self) -> None:
            pass

    workspace = Workspace(chain.run_root)
    result = workspace.run(Spec("t_list_reader", chain.load()))
    assert result.passed, result.error
    record = result.record
    (row,) = record.inputs
    assert [(s.path, s.generated, s.producer) for s in row.sources] == [
        ("$XEDA_GENERATED/gen/lib1.v", True, "lib1"),
        ("top.v", False, None),
    ]
    (storage,) = chain.storages("lib1")
    assert row.sources[0].generation == storage.name
    with record.open() as run:
        assert run.generated_sources == {
            "lib1:$XEDA_GENERATED/gen/lib1.v": storage / "gen" / "lib1.v"
        }
        assert run.missing_generated == ()
    (storage / "gen" / "lib1.v").write_text("// changed\n")
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (
            "lib1:$XEDA_GENERATED/gen/lib1.v",
        )


def test_a_converter_source_is_named_by_its_node_and_read_from_its_run_directory(tmp_path):
    fake, *_ = converter_flows()

    class TConvConsumer(Flow):
        """Reads the Verilog sources of its design, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        def run(self) -> None:
            pass

    root = tmp_path / "design"
    root.mkdir()
    (root / "A.bsv").write_text("package A; endpackage\n")
    design = Design(name="d", root=root, rtl={"sources": ["A.bsv"], "top": "mkA"})
    workspace = Workspace(tmp_path / "xeda_run")
    result = workspace.run(Spec(f"{fake.name}+{TConvConsumer.name}", design))
    assert result.passed, result.error
    record = result.record
    converter = result.nodes[fake.name].record
    (row,) = record.inputs
    assert isinstance(row, InputRecord) and row.producer == fake.name
    (source,) = row.sources
    assert (source.path, source.generated, source.producer) == ("A.v", True, fake.name)
    assert source.digest == converter.outputs["design"].digests[0]
    with record.open() as run:
        assert run.generated_sources == {f"{fake.name}:A.v": converter.run_dir / "A.v"}
        assert run.missing_generated == ()
    (converter.run_dir / "A.v").write_text("module changed(); endmodule\n")
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (f"{fake.name}:A.v",)


def test_a_flow_that_reads_its_design_records_the_sources_it_read(tmp_path):
    reader = reader_flow()
    world = World(
        tmp_path,
        sources=["$XEDA_GENERATED/gen/top.v", "extra.v"],
        generator={"generated_sources": ["$XEDA_GENERATED/gen/top.v"]},
    )
    (world.root / "extra.v").write_text("module extra; endmodule\n")
    workspace = Workspace(world.run_root)
    result = workspace.run(Spec(reader.name, world.load()))
    assert result.passed, result.error
    record = result.record
    (row,) = record.inputs
    assert (row.name, row.origin, row.producer, row.producer_run_dir) == (
        "design",
        "source",
        None,
        None,
    )
    (storage,) = world.storages()
    generated, written = row.sources
    assert (generated.path, generated.generated, generated.producer) == (
        "$XEDA_GENERATED/gen/top.v",
        True,
        None,
    )
    assert generated.generation == storage.name and generated.digest
    assert (written.path, written.generated, written.producer) == ("extra.v", False, None)
    assert written.generation is None and written.digest and written.dependency is None
    with record.open() as run:
        assert run.generated_sources == {"$XEDA_GENERATED/gen/top.v": storage / "gen" / "top.v"}
        assert run.missing_generated == ()
    (storage / "gen" / "top.v").write_text("// changed\n")
    with record.open() as run:
        assert run.generated_sources == {} and run.missing_generated == (
            "$XEDA_GENERATED/gen/top.v",
        )


def test_a_converter_row_lists_the_sources_that_pass_through(tmp_path):
    fake, *_ = converter_flows()

    class TConvConsumer(Flow):
        """Reads the Verilog sources of its design, for the tests of records."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        def run(self) -> None:
            pass

    world = World(
        tmp_path,
        sources=["A.bsv", "$XEDA_GENERATED/gen/top.v", "plain.v"],
        generator={"generated_sources": ["$XEDA_GENERATED/gen/top.v"]},
    )
    (world.root / "A.bsv").write_text("package A; endpackage\n")
    (world.root / "plain.v").write_text("module plain; endmodule\n")
    result = Workspace(world.run_root).run(Spec(f"{fake.name}+{TConvConsumer.name}", world.load()))
    assert result.passed, result.error
    record = result.record
    converter = result.nodes[fake.name].record
    (row,) = record.inputs
    assert (row.name, row.producer, row.output) == ("design", fake.name, "design")
    (storage,) = world.storages()
    assert [(s.path, s.generated, s.producer) for s in row.sources] == [
        ("A.v", True, fake.name),
        ("$XEDA_GENERATED/gen/top.v", True, None),
        ("plain.v", False, None),
    ]
    made, passed, written = row.sources
    assert made.digest == converter.outputs["design"].digests[0] and made.generation is None
    assert passed.generation == storage.name and written.generation is None
    with record.open() as run:
        assert run.generated_sources == {
            f"{fake.name}:A.v": converter.run_dir / "A.v",
            "$XEDA_GENERATED/gen/top.v": storage / "gen" / "top.v",
        }
        assert run.missing_generated == ()
    (storage / "gen" / "top.v").write_text("// changed\n")
    with record.open() as run:
        assert set(run.generated_sources) == {f"{fake.name}:A.v"}
        assert run.missing_generated == ("$XEDA_GENERATED/gen/top.v",)


# ---------------------------------------------------------------------- what a record holds


def _paths(value, where="record"):
    """Yield every `Path` that a value holds, with where it is."""
    if isinstance(value, Path):
        yield where, value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _paths(getattr(value, field.name), f"{type(value).__name__}.{field.name}")
    elif isinstance(value, dict) or hasattr(value, "items"):
        for key, item in value.items():
            yield from _paths(item, where)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _paths(item, where)


def test_no_public_record_field_names_a_file_by_an_absolute_path(tmp_path):
    _world, _workspace, record = generated_world(tmp_path)
    stamp, spec = stamped(tmp_path / "other", write_extra=True)
    made = stamp.run(spec).record
    locations = {"RunRecord.run_root", "RunRecord.run_dir", "InputRecord.producer_run_dir"}
    for held in (record, made):
        for where, path in _paths(held):
            assert where in locations and path.is_absolute(), (where, path)
        for output in held.outputs.values():
            assert not any(os.path.isabs(name) for name in output.names)
        assert not any(os.path.isabs(name) for name in held.artifacts.values())
        for row in held.inputs:
            assert not any(os.path.isabs(source.path) for source in row.sources)
    with made.open() as run:
        assert all(path.is_absolute() for _where, path in _paths(run.outputs))


def test_loading_imports_no_flow_module(tmp_path):
    workspace, spec = stamped(tmp_path, write_extra=True)
    run_dir = workspace.run(spec).record.run_dir
    program = textwrap.dedent("""
        import sys
        from pathlib import Path
        from xeda.workspace import Workspace
        workspace = Workspace(Path(sys.argv[1]))
        record = workspace.load(Path(sys.argv[2]))
        assert [r.run_id for r in workspace.records()] == [record.run_id]
        with record.open() as run:
            assert run.output("made").is_file()
        assert not [name for name in sys.modules if name.startswith("xeda.flows")], "flows"
        print("ok")
        """)
    found = subprocess.run(
        [sys.executable, "-I", "-c", program, str(workspace.run_root), str(run_dir)],
        capture_output=True,
        text=True,
        timeout=PROMPT,
        check=False,
    )
    assert found.stdout.strip() == "ok", found.stderr


# ------------------------------------------------------------------------------------ scrub


def test_scrub_without_confirm_removes_nothing_and_says_so(tmp_path, caplog):
    workspace, spec = stamped(tmp_path)
    run_dir = workspace.run(spec).record.run_dir
    with caplog.at_level(logging.WARNING, logger="xeda"):
        result = workspace.scrub(design_name="d")
    assert result.removed == [] and result.kept == [] and result.gone == []
    assert result.scanned and run_dir.is_dir()
    assert "nothing confirmed the removal" in caplog.text


def test_scrub_reports_removed_kept_and_gone(tmp_path):
    cut_flows()
    stamp_flow()
    design = cut_design(tmp_path / "design")
    questions: list[str] = []
    root = tmp_path / "xeda_run"
    producer, consumer, stamp = (
        root / "d" / name for name in ("cut_producer", "cut_consumer", "t_stamp")
    )

    def confirm(question: str) -> bool:
        questions.append(question)
        shutil.rmtree(producer)  # gone when its turn comes
        rewrite_run_id(consumer, "e" * 32)  # a run record that changed after the listing
        return True

    workspace = Workspace(root, confirm=confirm)
    assert workspace.run(Spec("cut_consumer", design)).passed
    assert workspace.run(Spec("t_stamp", design)).passed
    result = workspace.scrub(design_name="d")
    assert len(questions) == 1
    assert all(str(path) in questions[0] for path in (producer, consumer, stamp))
    assert (result.removed, result.kept, result.gone) == ([stamp], [consumer], [producer])
    assert not stamp.exists() and consumer.is_dir() and not producer.exists()
    assert result.skipped == [] and result.scanned


def test_scrub_selects_by_flow_and_target(tmp_path):
    workspace = Workspace(tmp_path / "xeda_run", confirm=lambda question: True)
    for target in ("a", "b", None):
        assert workspace.run(Spec("t_taker", target_design(tmp_path, target))).passed
    root = workspace.run_root / "d"
    assert workspace.scrub(design_name="d", flow="t_taker", target="a").removed == [
        root / "a" / "t_taker"
    ]
    assert workspace.scrub(design_name="d", flow="t_taker", target=None).removed == [
        root / "t_taker"
    ]
    assert sorted(workspace.scrub(design_name="d", flow="t_taker").removed) == [
        root / "b" / "t_taker"
    ]
    assert {r.flow for r in workspace.records()} == {"t_maker"}
    assert sorted(workspace.scrub(design_name="d").removed) == sorted(
        [root / "a" / "t_maker", root / "b" / "t_maker", root / "t_maker"]
    )
    assert workspace.records().records == ()


def test_scrub_of_a_name_that_is_no_flow_raises(tmp_path):
    workspace = Workspace(tmp_path / "xeda_run", confirm=lambda question: True)
    with pytest.raises(FlowNotFoundError) as raised:
        workspace.scrub(design_name="d", flow="t_stamp_typo")
    assert raised.value.name == "t_stamp_typo"


def test_a_design_level_scrub_removes_the_generation_storages_a_flow_scrub_keeps(tmp_path):
    world, workspace, record = generated_world(tmp_path)
    flow = record.flow
    (storage,) = world.storages()
    questions: list[str] = []
    scrubbing = Workspace(world.run_root, confirm=lambda q: questions.append(q) or True)
    only_flow = scrubbing.scrub(design_name="generated", flow=flow)
    assert only_flow.removed == [record.run_dir] and storage.is_dir()
    assert workspace.run(Spec(flow, world.load())).passed
    everything = scrubbing.scrub(design_name="generated")
    assert sorted(everything.removed) == sorted([record.run_dir, storage])
    assert not storage.exists() and not record.run_dir.exists()
    assert len(questions) == 2 and str(storage) in questions[1], "one question for all"
    assert scrubbing.scrub(design_name="generated").removed == []


def test_a_target_scrub_keeps_the_generation_storages_that_every_target_reads(tmp_path):
    world, _workspace, record = generated_world(tmp_path)
    (storage,) = world.storages()
    scrubbing = Workspace(world.run_root, confirm=lambda _question: True)
    result = scrubbing.scrub(design_name="generated", target="a")
    assert storage.is_dir() and record.run_dir.is_dir()
    assert storage not in result.removed and storage.parent not in result.scanned


def test_a_design_level_scrub_refuses_a_link_where_the_storages_lie(tmp_path):
    world, _workspace, record = generated_world(tmp_path)
    base = world.run_root / ".generated" / "generated"
    elsewhere = tmp_path / "elsewhere"
    base.rename(elsewhere)
    base.symlink_to(elsewhere)
    scrubbing = Workspace(world.run_root, confirm=lambda question: True)
    with pytest.raises(RunRootError) as raised:
        scrubbing.scrub(design_name="generated")
    assert raised.value.code == "invalid_run_root"
    assert any(elsewhere.iterdir()) and record.run_dir.is_dir(), "nothing was removed"
