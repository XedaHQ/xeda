"""Make-like launches, the default: a flow re-runs only when what it consumed or produced changed.

Two pure-Python flows stand in for tools: `toy_producer` copies the design's source (plus a
suffix setting) to an output; `toy_consumer` depends on it and copies that output onward. Each
records that it ran, so the tests see exactly what executed.
"""

import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import Field

from xeda import Design
from xeda.console import console
from xeda.flow import Flow, FlowFatalError, FlowSettingsError, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import scrub_runs
from xeda.flow_runner.run_lock import lock_file, run_dir_lock

from .tool_utils import require_yosys

RUNS: list[str] = []
#: why the consumer runs after its producer ran again: dependencies are named by flow and run
#: directory, relative to `xeda_run`
AGAIN = "toy_producer (toy/toy_producer) ran again"


@pytest.fixture(scope="module")
def toys():
    class ToyProducer(Flow):
        """Copies the first design source, plus `suffix`, to outputs/produced.txt."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            suffix: str = Field("", description="Appended to the copied text.")

        def run(self) -> None:
            RUNS.append(self.name)
            out = self.run_path / "outputs" / "produced.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(self.design.rtl.sources[0].file.read_text() + self.settings.suffix)
            self.artifacts.produced = out

    class ToyConsumer(Flow):
        """Copies its dependency's output to outputs/consumed.txt."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyProducer, ToyProducer.Settings())

        def run(self) -> None:
            RUNS.append(self.name)
            (producer,) = self.completed_dependencies
            out = self.run_path / "outputs" / "consumed.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(Path(producer.artifacts.produced).read_text())
            self.artifacts.consumed = out

    yield ToyProducer, ToyConsumer
    for cls in (ToyProducer, ToyConsumer):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


@pytest.fixture
def design(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    return Design(name="toy", rtl={"sources": ["a.v"], "top": "a"}, design_root=tmp_path)


def _run(tmp_path, cls, design, sections=None, settings=None, **launcher):
    RUNS.clear()
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, **launcher)
    flow = runner.launch_flow(cls, design, settings or {}, all_flows_settings=sections)
    assert flow.succeeded
    return flow, list(RUNS)


def _unregister(*classes):
    for cls in classes:
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


def test_an_unchanged_rerun_runs_nothing(tmp_path, toys, design):
    _, consumer = toys
    _run(tmp_path, consumer, design)
    flow, ran = _run(tmp_path, consumer, design)
    assert ran == [] and flow.reused
    assert flow.completed_dependencies[0].reused


def test_dependencies_are_siblings(tmp_path, toys, design):
    producer, consumer = toys
    flow, _ = _run(tmp_path, consumer, design)
    assert flow.completed_dependencies[0].run_path == flow.run_path.parent / producer.name


def test_a_source_edit_reruns_the_chain_and_says_why(tmp_path, toys, design):
    _, consumer = toys
    _run(tmp_path, consumer, design)
    (design.root_path / "a.v").write_text("module b; endmodule\n")
    flow, ran = _run(tmp_path, consumer, design)
    assert ran == ["toy_producer", "toy_consumer"]
    assert flow.completed_dependencies[0].stale_reason.startswith("input changed:")
    assert flow.stale_reason == AGAIN


def test_a_setting_of_the_dependency_alone_reruns_both(tmp_path, toys, design):
    _, consumer = toys
    _run(tmp_path, consumer, design)
    flow, ran = _run(tmp_path, consumer, design, sections={"toy_producer": {"suffix": "!"}})
    assert ran == ["toy_producer", "toy_consumer"]
    assert flow.completed_dependencies[0].stale_reason == "settings changed: suffix"
    assert flow.stale_reason == AGAIN


def test_a_dependency_that_ran_again_reruns_its_depender_even_with_an_identical_output(
    tmp_path, toys, design
):
    """A producer setting that does not affect its output re-runs the producer, and then the
    consumer, although the producer's declared output is byte-identical: a flow may read files
    its dependency wrote without declaring them, so a dependency's new run is a change. (Early
    cutoff across a dependency edge returns once flows read their dependencies' files only
    through declared outputs, in plan 2; cutoff on a flow's own inputs is below.)"""
    _, consumer = toys
    _run(tmp_path, consumer, design)
    flow, ran = _run(tmp_path, consumer, design, sections={"toy_producer": {"verbose": 1}})
    assert ran == ["toy_producer", "toy_consumer"]
    assert flow.stale_reason == AGAIN


def test_a_dependency_that_ran_again_since_is_a_change(tmp_path, toys, design):
    """Across invocations too: the producer re-ran on its own after the consumer last ran, so
    the consumer, finding the producer fresh, still sees a producer run it did not consume."""
    producer, consumer = toys
    _run(tmp_path, consumer, design)
    _, ran = _run(tmp_path, producer, design, rebuild_all=True)  # byte-identical output
    assert ran == ["toy_producer"]
    flow, ran = _run(tmp_path, consumer, design)
    assert flow.completed_dependencies[0].reused
    assert ran == ["toy_consumer"] and flow.stale_reason == AGAIN


def test_rewriting_a_source_with_the_same_content_runs_nothing(tmp_path, toys, design):
    _, consumer = toys
    _run(tmp_path, consumer, design)
    (design.root_path / "a.v").write_text("module a; endmodule\n")  # same content, new mtime
    flow, ran = _run(tmp_path, consumer, design)
    assert ran == [] and flow.reused


def test_an_interrupted_run_is_stale(tmp_path, toys, design):
    producer, _ = toys
    flow, _ = _run(tmp_path, producer, design)
    (flow.run_path / "trace.json").unlink()  # what a crash before the trace looks like
    _, ran = _run(tmp_path, producer, design)
    assert ran == ["toy_producer"]


def test_a_deleted_output_is_stale(tmp_path, toys, design):
    producer, _ = toys
    flow, _ = _run(tmp_path, producer, design)
    Path(flow.artifacts.produced).unlink()
    again, ran = _run(tmp_path, producer, design)
    assert ran == ["toy_producer"] and again.stale_reason.startswith("output missing:")


def test_rebuild_all_always_runs(tmp_path, toys, design):
    producer, _ = toys
    _run(tmp_path, producer, design)
    _, ran = _run(tmp_path, producer, design, rebuild_all=True)
    assert ran == ["toy_producer"]


def test_clean_empties_every_node_that_runs(tmp_path, toys, design):
    _, consumer = toys
    flow, _ = _run(tmp_path, consumer, design)
    leftover = flow.completed_dependencies[0].run_path / "leftover.txt"
    leftover.write_text("x")
    _, ran = _run(tmp_path, consumer, design, clean=True)
    assert ran == ["toy_producer", "toy_consumer"] and not leftover.exists()


def test_post_cleanup_waits_for_the_requested_flow(tmp_path, design):
    """A dependency's post-run clean-up waits until the flow the launch was asked for has
    completed: until then its depender may read any file it wrote, declared or not."""

    class ToyScratcher(Flow):
        """Writes an output it does not declare."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            RUNS.append(self.name)
            (self.run_path / "undeclared.txt").write_text("scratch")

    class ToyScratchReader(Flow):
        """Reads its dependency's undeclared output."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyScratcher, ToyScratcher.Settings())

        def run(self) -> None:
            RUNS.append(self.name)
            (scratcher,) = self.completed_dependencies
            assert (scratcher.run_path / "undeclared.txt").read_text() == "scratch"

    try:
        flow, ran = _run(tmp_path, ToyScratchReader, design, post_cleanup=True)
        (scratcher,) = flow.completed_dependencies
        assert ran == ["toy_scratcher", "toy_scratch_reader"]
        for run_path in (scratcher.run_path, flow.run_path):  # both cleaned up afterwards
            assert sorted(p.name for p in run_path.iterdir()) == ["results.json", "settings.json"]
        # Pruning removed the traces: a pruned directory may lack a file a depender reads (here
        # `undeclared.txt`), so it is never reused -- both run again.
        flow, ran = _run(tmp_path, ToyScratchReader, design, post_cleanup=True)
        assert ran == ["toy_scratcher", "toy_scratch_reader"]
        assert flow.completed_dependencies[0].stale_reason == "no successful previous run"
        _, ran = _run(
            tmp_path, ToyScratchReader, design, post_cleanup=True, post_cleanup_purge=True
        )
        assert ran == ["toy_scratcher", "toy_scratch_reader"]
        assert not scratcher.run_path.exists() and not flow.run_path.exists()
    finally:
        _unregister(ToyScratcher, ToyScratchReader)


def test_a_failed_launch_still_cleans_up_what_ran(tmp_path, toys, design):
    """The deferred clean-ups run when the requested flow fails, too."""
    producer, _ = toys

    class ToyFailingUser(Flow):
        """Depends on the producer, then fails."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(producer, producer.Settings())

        def run(self) -> None:
            raise FlowFatalError("boom")

    try:
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, post_cleanup=True)
        with pytest.raises(FlowFatalError):
            runner.launch_flow(ToyFailingUser, design, {})
        done, failed = runner.launched
        assert sorted(p.name for p in done.run_path.iterdir()) == [
            "outputs",
            "results.json",
            "settings.json",
        ]
        assert not failed.succeeded
    finally:
        _unregister(ToyFailingUser)


def test_a_file_a_depfile_named_is_an_input(tmp_path, design):
    """A file the flow's tool reported reading, in a depfile, is checked like any input."""
    included = tmp_path / "included.vh"
    included.write_text("`define A 1\n")

    class ToyIncluder(Flow):
        """Reports, in a depfile, that it read a file the design does not list."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            RUNS.append(self.name)
            depfile = self.run_path / "deps.d"
            depfile.write_text(f"out: {included}\n")
            self.depfiles.append(depfile)

    try:
        _run(tmp_path, ToyIncluder, design)
        flow, ran = _run(tmp_path, ToyIncluder, design)
        assert ran == [] and flow.reused
        included.write_text("`define A 2\n")
        flow, ran = _run(tmp_path, ToyIncluder, design)
        assert ran == ["toy_includer"]
        assert flow.stale_reason == f"input changed: {included.resolve()}"
    finally:
        _unregister(ToyIncluder)


def test_a_flow_reporting_its_whole_run_directory_can_be_fresh(tmp_path, design):
    """The run directory itself can be an artifact: its outputs are then everything in it,
    `results.json` included, which the trace must record as the run left it."""

    class ToyWhole(Flow):
        """Reports its whole run directory."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            RUNS.append(self.name)
            (self.run_path / "out.txt").write_text("out")
            self.artifacts["directory"] = "."

    try:
        _run(tmp_path, ToyWhole, design)
        _run(tmp_path, ToyWhole, design, rebuild_all=True)  # over the previous run's results.json
        flow, ran = _run(tmp_path, ToyWhole, design)
        assert ran == [] and flow.reused
    finally:
        _unregister(ToyWhole)


def test_an_action_runs_every_time_after_fresh_dependencies(tmp_path, toys, design):
    """A flow that changes the world (programs a board) always runs, and no trace records it;
    its dependencies are reused as usual."""
    producer, _ = toys

    class ToyAction(Flow):
        """Pretends to program a board with its dependency's output."""

        results_description: ClassVar[dict[str, str]] = {}

        def always_runs(self):
            return "it programs a device"

        def init(self) -> None:
            self.add_dependency(producer, producer.Settings())

        def run(self) -> None:
            RUNS.append(self.name)

    try:
        flow, ran = _run(tmp_path, ToyAction, design)
        assert ran == ["toy_producer", "toy_action"]
        assert not (flow.run_path / "trace.json").exists()
        (flow.run_path / "trace.json").write_text("{}")  # a trace from before it was an action
        flow, ran = _run(tmp_path, ToyAction, design)
        assert ran == ["toy_action"] and flow.completed_dependencies[0].reused
        assert not flow.reused and flow.stale_reason == "it programs a device"
        assert not (flow.run_path / "trace.json").exists()
    finally:
        _unregister(ToyAction)


RUN_PATH_REMOVED = (
    "`run_path` was removed: use run_root to choose where runs go (a flow always runs in a "
    "directory xeda creates under it), and outputs_to to receive its outputs elsewhere"
)


def test_run_path_was_removed_and_names_run_root(tmp_path):
    with pytest.raises(ValueError, match=re.escape(RUN_PATH_REMOVED)):
        DefaultRunner(tmp_path / "xeda_run", run_path=tmp_path / "mine")
    runner = DefaultRunner(tmp_path / "xeda_run")
    with pytest.raises(ValueError, match=re.escape(RUN_PATH_REMOVED)):
        runner.settings.run_path = tmp_path / "mine"


def test_a_launch_takes_no_run_path():
    import inspect

    from xeda.flow_runner.dse.dse_runner import Dse

    for method in (DefaultRunner.launch_flow, DefaultRunner.run_flow, Dse.run_flow):
        assert "run_path" not in inspect.signature(method).parameters, method


def test_a_flow_built_directly_names_its_run_directory(tmp_path, toys, design):
    producer, _ = toys
    with pytest.raises(TypeError):
        producer({}, design)  # type: ignore[call-arg]


def test_launched_lists_every_flow_in_completion_order(tmp_path, toys, design):
    """Dependencies before their dependers, reused flows as well as ones that ran."""
    _, consumer = toys
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    runner.launch_flow(consumer, design, {})
    runner.launch_flow(consumer, design, {})
    assert [(f.name, f.reused) for f in runner.launched] == [
        ("toy_producer", False),
        ("toy_consumer", False),
        ("toy_producer", True),
        ("toy_consumer", True),
    ]


def test_launched_lists_a_flow_that_raised_and_its_depender(tmp_path, design):
    class ToyFatal(Flow):
        """Its run raises."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            raise FlowFatalError("boom")

    class ToyFatalUser(Flow):
        """Depends on a flow whose run raises."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(ToyFatal, ToyFatal.Settings())

        def run(self) -> None:
            RUNS.append(self.name)

    try:
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        with pytest.raises(FlowFatalError):
            runner.launch_flow(ToyFatalUser, design, {})
        assert [(f.name, f.succeeded) for f in runner.launched] == [
            ("toy_fatal", False),
            ("toy_fatal_user", False),
        ]
    finally:
        _unregister(ToyFatal, ToyFatalUser)


def _two_producers(producer, first: dict, second: dict):
    class ToyTwoProducers(Flow):
        """Depends on two runs of one flow."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(producer, producer.Settings(**first))
            self.add_dependency(producer, producer.Settings(**second))

        def run(self) -> None:
            RUNS.append(self.name)

    return ToyTwoProducers


def test_one_directory_one_configuration_per_launch(tmp_path, toys, design):
    """Two dependencies of one flow with different settings resolve to one stable directory: the
    second would overwrite what the first produced, so the launch fails, naming both."""
    producer, _ = toys
    two = _two_producers(producer, {}, {"suffix": "!"})
    try:
        with pytest.raises(FlowSettingsError) as raised:
            _run(tmp_path, two, design)
        message = str(raised.value)
        assert str(tmp_path / "xeda_run" / "toy" / "toy_producer") in message
        assert "toy_producer would run twice" in message and "(differing in suffix)" in message
        assert "for toy_two_producers and for toy_two_producers" in message
    finally:
        _unregister(two)


def test_a_dependency_cannot_take_over_the_requested_flow_s_directory(tmp_path, design):
    class ToyNested(Flow):
        """Depends on itself, with other settings."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            depth: int = Field(0, description="How deep this launch is.")

        def init(self) -> None:
            if self.settings.depth == 0:
                self.add_dependency(ToyNested, ToyNested.Settings(depth=1))

        def run(self) -> None:
            RUNS.append(self.name)

    try:
        with pytest.raises(FlowSettingsError, match="for the requested flow and for toy_nested"):
            _run(tmp_path, ToyNested, design)
        assert RUNS == []
    finally:
        _unregister(ToyNested)


@pytest.mark.parametrize("launcher", [{}, {"rebuild_all": True}, {"clean": True}], ids=str)
def test_the_same_configuration_twice_in_a_launch_runs_once(tmp_path, toys, design, launcher):
    """Two dependencies with the same settings share one run, however the launch rebuilds."""
    producer, _ = toys
    two = _two_producers(producer, {"suffix": "!"}, {"suffix": "!"})
    try:
        flow, ran = _run(tmp_path, two, design, **launcher)
        assert ran == ["toy_producer", "toy_two_producers"]
        first, second = flow.completed_dependencies
        assert not first.reused and second.reused and first.run_id == second.run_id
    finally:
        _unregister(two)


def test_two_dependencies_of_one_flow_are_told_apart(tmp_path, toys, design):
    """In hashed directories two runs of one flow with different settings coexist, and a
    depender tracks each: the first running again makes it stale, although the second did not
    and the first's output is byte-identical."""
    producer, _ = toys
    two = _two_producers(producer, {}, {"suffix": "!"})
    try:
        flow, ran = _run(tmp_path, two, design, hashed_run_dirs=True)
        first, second = flow.completed_dependencies
        assert first.run_path != second.run_path
        runner = DefaultRunner(
            tmp_path / "xeda_run", display_results=False, hashed_run_dirs=True, rebuild_all=True
        )
        runner.launch_flow(producer, design, {})  # the first, again
        flow, ran = _run(tmp_path, two, design, hashed_run_dirs=True)
        assert ran == ["toy_two_producers"]
        assert flow.stale_reason == f"toy_producer (toy/{first.run_path.name}) ran again"
    finally:
        _unregister(two)


def test_a_failed_clean_up_does_not_stop_the_others(tmp_path, toys, design, monkeypatch, caplog):
    """Each deferred clean-up runs on its own: one that fails is logged, the next still runs, and
    the first failure is raised once all ran -- unless the launch itself failed, whose error then
    propagates unchanged."""
    producer, consumer = toys
    clean_up = DefaultRunner._clean_up

    def failing_for_the_producer(self, flow, *args):
        if flow.name == "toy_producer":
            raise OSError("cannot clean up")
        clean_up(self, flow, *args)

    monkeypatch.setattr(DefaultRunner, "_clean_up", failing_for_the_producer)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, post_cleanup=True)
    with pytest.raises(OSError, match="cannot clean up"):
        runner.launch_flow(consumer, design, {})
    producer_run, consumer_run = runner.launched
    assert "Cleaning up" in caplog.text and "cannot clean up" in caplog.text
    assert (producer_run.run_path / "trace.json").exists()  # not cleaned up
    assert sorted(p.name for p in consumer_run.run_path.iterdir()) == [
        "outputs",
        "results.json",
        "settings.json",
    ]

    class ToyFailingConsumer(Flow):
        """Depends on the producer, then fails."""

        results_description: ClassVar[dict[str, str]] = {}

        def init(self) -> None:
            self.add_dependency(producer, producer.Settings())

        def run(self) -> None:
            raise FlowFatalError("the launch's own error")

    try:
        runner = DefaultRunner(
            tmp_path / "xeda_run", display_results=False, post_cleanup=True, rebuild_all=True
        )
        with pytest.raises(FlowFatalError, match="the launch's own error"):
            runner.launch_flow(ToyFailingConsumer, design, {})
    finally:
        _unregister(ToyFailingConsumer)


def test_scrubbing_a_run_directory_removes_its_lock(tmp_path, toys, design, monkeypatch):
    """A lock file sits beside its run directory; scrubbing the directory removes both."""
    producer, _ = toys
    old, _ = _run(tmp_path, producer, design, hashed_run_dirs=True)
    lock = lock_file(old.run_path)
    assert lock.is_file()
    monkeypatch.setattr(console, "input", lambda *a, **kw: "yes")
    assert scrub_runs(producer.name, old.run_path.parent)
    assert not old.run_path.exists() and not lock.exists()
    kept, _ = _run(tmp_path, producer, design, hashed_run_dirs=True)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "no")  # declined: nothing removed
    assert not scrub_runs(producer.name, kept.run_path.parent)
    assert kept.run_path.exists() and lock_file(kept.run_path).is_file()


def test_hashed_directories_are_named_by_settings(tmp_path, toys, design):
    producer, _ = toys
    flow, _ = _run(tmp_path, producer, design, hashed_run_dirs=True)
    assert flow.run_path.name == f"{producer.name}_{flow.flow_hash[:16]}"
    assert flow.run_path.parent.name == "toy"  # no design-hash layer


@pytest.mark.parametrize(
    "old, replacement",
    [
        (
            "cached_dependencies",
            "the default, which reuses unchanged runs, and hashed_run_dirs=True to keep settings "
            "variants side by side",
        ),
        ("skip_if_previous_run_exists", "the default, which reuses unchanged runs"),
        (
            "incremental",
            "clean=True to empty run directories before running (they are otherwise always "
            "reused)",
        ),
        ("cleanup_before_run", "clean=True"),
    ],
)
def test_replaced_launcher_settings_name_their_replacement(tmp_path, old, replacement):
    with pytest.raises(ValueError, match=re.escape(f"`{old}` was removed: use {replacement}")):
        DefaultRunner(tmp_path / "xeda_run", **{old: True})


_HOLDER = """
import sys
from pathlib import Path
from xeda.flow_runner.run_lock import run_dir_lock

with run_dir_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()  # until the test closes our stdin
"""


def test_a_run_directory_lock_makes_another_process_wait(tmp_path):
    """Parallel launches of one run directory (DSE variants sharing a dependency) take turns."""
    run_path = tmp_path / "xeda_run" / "toy" / "toy_producer"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(run_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        acquired = threading.Event()

        def acquire() -> None:
            with run_dir_lock(run_path):
                acquired.set()

        waiter = threading.Thread(target=acquire, daemon=True)
        waiter.start()
        assert not acquired.wait(0.5), "the lock was taken while another process held it"
        assert holder.stdin is not None
        holder.stdin.close()  # the holder releases the lock and exits
        assert acquired.wait(10), "the lock was not taken once released"
        assert lock_file(run_path).is_file() and not run_path.exists()
    finally:
        holder.kill()
        holder.wait()


def test_an_edited_include_reruns_yosys(tmp_path):
    """The header is no source of the design: only yosys's depfile knows it was read."""
    require_yosys()
    (tmp_path / "defs.vh").write_text("`define W 4\n")
    (tmp_path / "top.v").write_text(
        '`include "defs.vh"\nmodule top(input [`W-1:0] a, output [`W-1:0] y); '
        "assign y = ~a; endmodule\n"
    )
    design = Design(name="inc", rtl={"sources": ["top.v"], "top": "top"}, design_root=tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    settings = {"fpga": {"part": "LFE5U-25F-6BG256C"}}
    first = runner.launch_flow("yosys_fpga", design, settings)
    assert first.succeeded
    header = (tmp_path / "defs.vh").resolve()
    trace = json.loads((first.run_path / "trace.json").read_text())
    assert str(header) in trace["implicit_inputs"]
    # yosys's own library files, which its depfile names too, are its installation's: not recorded
    assert not any("/share/yosys/" in name for name in trace["implicit_inputs"]), trace
    # a second launch is fresh, or runs once more where a file's first record cannot tell
    again = runner.launch_flow("yosys_fpga", design, settings)
    if not again.reused:
        assert again.stale_reason.startswith("input first read by the last run"), again
        assert runner.launch_flow("yosys_fpga", design, settings).reused
    header.write_text("`define W 8\n")
    second = runner.launch_flow("yosys_fpga", design, settings)
    assert not second.reused and second.stale_reason == f"input changed: {header}"


def test_pruning_removes_the_trace_before_anything_else(tmp_path, design, monkeypatch):
    """A prune that stops halfway must not leave a trace vouching for a directory it has
    already partly emptied: the trace is the first thing it deletes."""
    from xeda import run_dir

    class ToyScratchFiles(Flow):
        """Leaves undeclared files behind, named on both sides of `trace.json`."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            for name in ("0.txt", "a.txt", "u.txt", "z.txt"):
                (self.run_path / name).write_text("x")
            (self.run_path / "scratch").mkdir(exist_ok=True)

    deleted: list[str] = []
    unlink, rmtree, clean_up = Path.unlink, run_dir.rmtree, DefaultRunner._clean_up
    iterdir = Path.iterdir

    def trace_last(self):  # the worst order for a prune that deletes as it iterates
        return iter(sorted(iterdir(self), key=lambda p: p.name == "trace.json"))

    def recording_unlink(self, *args, **kwargs):
        deleted.append(self.name)
        return unlink(self, *args, **kwargs)

    def recording_rmtree(path):
        deleted.append(Path(path).name)
        return rmtree(path)

    def recording_clean_up(self, *args, **kwargs):
        deleted.clear()  # only what the clean-up deletes
        return clean_up(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", recording_unlink)
    monkeypatch.setattr(run_dir, "rmtree", recording_rmtree)
    monkeypatch.setattr(DefaultRunner, "_clean_up", recording_clean_up)
    monkeypatch.setattr(Path, "iterdir", trace_last)
    try:
        flow, _ = _run(tmp_path, ToyScratchFiles, design, post_cleanup=True)
        assert deleted[0] == "trace.json" and "trace.json" not in deleted[1:]
        assert {"0.txt", "a.txt", "u.txt", "z.txt", "scratch"} <= set(deleted)
        assert not (flow.run_path / "trace.json").exists()
    finally:
        _unregister(ToyScratchFiles)


def test_a_change_to_xeda_s_code_reruns_every_flow(tmp_path, toys, design, monkeypatch):
    from xeda.flow_runner import trace_inputs

    _, consumer = toys
    _run(tmp_path, consumer, design)
    monkeypatch.setattr(trace_inputs, "xeda_code_digest", lambda: "e" * 32)
    flow, ran = _run(tmp_path, consumer, design)
    assert ran == ["toy_producer", "toy_consumer"]
    assert flow.completed_dependencies[0].stale_reason == "xeda's code changed"
