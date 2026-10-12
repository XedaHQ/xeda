"""`--dry-run` says what a launch would do: each planned node is `fresh`, `stale` with the reason
the launch would give, or `unknown` with why nothing can be said without running something.

The oracle is the launch itself. For a matrix of changes, on a graph of two toy flows and on the
graph `yosys_fpga` -> `nextpnr`, the dry run is made and then the same request is run: every node
the plan calls `fresh` is reused by the run, and every node it calls `stale` runs for exactly the
reason it gave. A node it calls `unknown` is not compared, but each case has a node that is
claimed, and the matrix holds all three states. The dry run is read-only: the tree, the run root
included, is as it was, the audit hook sees no write, and no process starts (the teeth: each of
the three mistakes that would break that is found).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, ClassVar

import pytest
from pydantic import Field

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, In, Out
from xeda.flow_runner import DefaultRunner, plan_freshness
from xeda.flow_runner.run_lock import run_dir_lock
from xeda.flow_runner.trace import locate_program
from xeda.proc_utils import DOCKER_IMAGE_PREFIX

from .flow_registry import preserved_flow_registry
from .test_declared_fpga_flows import PART, tools  # noqa: F401  (the stand-in tools)
from .test_isolation import _state
from .write_audit import writes_seen

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


# ------------------------------------------------------------------------------------ the graphs


@pytest.fixture
def toys():
    class ToyProducer(Flow):
        """Copies the first design source, plus `suffix`, to outputs/produced.txt."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            suffix: str = Field("", description="Appended to the copied text.")

        class Outputs(Flow.Outputs):
            produced: Path = Out(SourceType.Data, description="The copied text.")

        def run(self) -> None:
            out = self.run_path / "outputs" / "produced.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(self.design.rtl.sources[0].file.read_text() + self.settings.suffix)
            self.artifacts.produced = out
            self.outputs.produced = out

    class ToyConsumer(Flow):
        """Copies its producer's output to outputs/consumed.txt."""

        results_description: ClassVar[dict[str, str]] = {}
        design_source_types = frozenset()

        class Settings(Flow.Settings):
            shout: bool = Field(False, description="Upper-case the text.")
            always: bool = Field(False, description="Run every time, as a fresh seed does.")

        class Inputs(Flow.Inputs):
            produced: Path = In(
                SourceType.Data,
                producer="toy_producer",
                output="produced",
                description="The producer's text.",
            )

        def always_runs(self):
            assert isinstance(self.settings, self.Settings)
            return "it draws a new random seed" if self.settings.always else None

        def run(self) -> None:
            out = self.run_path / "outputs" / "consumed.txt"
            out.parent.mkdir(exist_ok=True)
            text = self.inputs.produced.read_text()
            out.write_text(text.upper() if self.settings.shout else text)
            self.artifacts.consumed = out

    return ToyProducer, ToyConsumer


class Graph:
    """A request, its design on disk, and the ways to change them."""

    flow: str
    #: the names of the node that produces and of the one that consumes
    producer: str
    consumer: str

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.run_root = root / "xeda_run"
        self.settings: list[str] = []
        monkeypatch.chdir(root)

    def design(self) -> Design:
        raise NotImplementedError

    def launcher(self, **options) -> DefaultRunner:
        return DefaultRunner(self.run_root, display_results=False, **options)

    def dry(self, **options):
        runner = self.launcher(**options)
        return runner.plan_with_freshness(self.flow, self.design(), flow_settings=self.settings)

    def run(self, **options) -> dict[str, Flow]:
        runner = self.launcher(**options)
        runner.run(self.flow, self.design(), flow_settings=self.settings)
        return {f.name: f for f in runner.launched}

    def run_dir(self, node: str) -> Path:
        return self.run_root / "d" / node

    def settle(self) -> None:
        self.run()
        assert all(f.reused for f in self.run().values())


class ToyGraph(Graph):
    flow, producer, consumer = "toy_consumer", "toy_producer", "toy_consumer"

    def __init__(self, root: Path, monkeypatch) -> None:
        super().__init__(root, monkeypatch)
        (root / "a.v").write_text("module a; endmodule\n")

    def design(self) -> Design:
        return Design(name="d", design_root=self.root, rtl={"sources": ["a.v"], "top": "a"})

    def edit_source(self) -> None:
        (self.root / "a.v").write_text("module a; wire edited; endmodule\n")

    def produced(self) -> Path:
        return self.run_dir("toy_producer") / "outputs" / "produced.txt"

    def consumer_setting(self) -> str:
        return "shout=true"

    def producer_setting(self) -> str:
        return "flows.toy_producer.suffix=!"


class FpgaGraph(Graph):
    flow, producer, consumer = "nextpnr", "yosys_fpga", "nextpnr"

    def __init__(self, root: Path, monkeypatch) -> None:
        super().__init__(root, monkeypatch)
        self.settings = [f"fpga.part={PART}"]
        from .test_declared_fpga_flows import BLINK

        (root / "blink.v").write_text(BLINK)

    def design(self) -> Design:
        return Design(name="d", design_root=self.root, rtl={"sources": ["blink.v"], "top": "blink"})

    def edit_source(self) -> None:
        path = self.root / "blink.v"
        path.write_text(path.read_text() + "// a comment\n")

    def produced(self) -> Path:
        return self.run_dir("yosys_fpga") / "netlist.json"

    def consumer_setting(self) -> str:
        return "seed=3"

    def producer_setting(self) -> str:
        return "flows.yosys_fpga.verbose=1"


# ------------------------------------------------------------------------------ the changes

Options = dict[str, Any]


def nothing(graph: Graph, options: Options) -> None:
    pass


def source_edit(graph: Graph, options: Options) -> None:
    graph.edit_source()


def consumer_setting(graph: Graph, options: Options) -> None:
    graph.settings.append(graph.consumer_setting())


def producer_setting(graph: Graph, options: Options) -> None:
    graph.settings.append(graph.producer_setting())


def producer_output_tampered(graph: Graph, options: Options) -> None:
    graph.produced().write_text("tampered")


def file_added(graph: Graph, options: Options) -> None:
    (graph.run_dir(graph.consumer) / "notes.txt").write_text("mine\n")


def missing_trace(graph: Graph, options: Options) -> None:
    (graph.run_dir(graph.consumer) / "trace.json").unlink()


def rebuild_all(graph: Graph, options: Options) -> None:
    options["rebuild_all"] = True


def clean(graph: Graph, options: Options) -> None:
    options["clean"] = True


CHANGES: dict[str, Callable[[Graph, Options], None]] = {
    "nothing": nothing,
    "source edit": source_edit,
    "consumer setting": consumer_setting,
    "producer setting": producer_setting,
    "producer output tampered": producer_output_tampered,
    "file added": file_added,
    "missing trace": missing_trace,
    "rebuild-all": rebuild_all,
    "clean": clean,
}
GRAPHS = {"toys": ToyGraph, "fpga": FpgaGraph}

#: the state of (producer, consumer) the plan gives for each change, on both graphs
EXPECTED = {
    "nothing": (FRESH, FRESH),
    "source edit": (STALE, UNKNOWN),
    "consumer setting": (FRESH, STALE),
    "producer setting": (STALE, STALE),
    "producer output tampered": (STALE, UNKNOWN),
    "file added": (FRESH, STALE),
    "missing trace": (FRESH, STALE),
    "rebuild-all": (STALE, STALE),
    "clean": (STALE, STALE),
}


def compare(graph: Graph, options: Options) -> dict[str, Any]:
    """Dry-run, then run: the states of the plan, after checking the run bears them out."""
    plan, states = graph.dry(**options)
    ran = graph.run(**options)
    claimed = 0
    for node in plan.nodes:
        said = states[node.name]
        flow = ran[node.name]
        if said.state == FRESH:
            claimed += 1
            assert flow.reused, f"{node.name}: said fresh, but it ran: {flow.stale_reason}"
        elif said.state == STALE:
            claimed += 1
            assert not flow.reused, f"{node.name}: said stale ({said.reason}), but it was reused"
            assert flow.stale_reason == said.reason, node.name
        else:
            assert said.state == UNKNOWN and said.reason, node.name
    assert claimed, "a case that claims nothing shows nothing"
    return states


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("graph_kind", sorted(GRAPHS))
def test_the_dry_run_and_the_run_agree_on_every_node_it_claims(
    graph_kind, change, tmp_path, monkeypatch, toys, tools  # noqa: F811
):
    graph = GRAPHS[graph_kind](tmp_path, monkeypatch)
    graph.settle()
    options: Options = {}
    CHANGES[change](graph, options)
    states = compare(graph, options)
    assert (states[graph.producer].state, states[graph.consumer].state) == EXPECTED[change]


def test_a_prepared_input_a_flow_registers_is_judged_as_the_launch_judges_it(
    tmp_path, monkeypatch, tools  # noqa: F811
):
    """The ABC script of `yosys_fpga` is registered in the flow's preparation, not named by the
    design: a dry run finds it by the flow's locating step, and an edit of it is the reason."""
    graph = FpgaGraph(tmp_path, monkeypatch)
    script = tmp_path / "map.abc"
    script.write_text("strash\n")
    graph.settings.append(f"flows.yosys_fpga.abc_script={script}")
    graph.settle()
    _, states = graph.dry()
    assert (states["yosys_fpga"].state, states["nextpnr"].state) == (FRESH, FRESH)
    script.write_text("strash\nmap\n")
    states = compare(graph, {})
    assert (states["yosys_fpga"].state, states["yosys_fpga"].reason) == (
        STALE,
        f"input changed: {script.resolve()}",
    )
    assert states["nextpnr"].state == UNKNOWN


def test_a_node_whose_settings_a_launch_refuses_in_the_design_s_context_is_unknown(
    tmp_path, monkeypatch, tools  # noqa: F811
):
    """A plan checks the request; a launch checks the settings against the design too (a clock the
    design has no port for). The dry run cannot say what a launch that fails would do."""
    graph = FpgaGraph(tmp_path, monkeypatch)
    graph.settings.append("clock.freq=100MHz")
    plan, states = graph.dry()
    assert states["nextpnr"].state == UNKNOWN
    assert states["nextpnr"].reason.startswith("a launch refuses its settings: ")
    assert "clock" in states["nextpnr"].reason


def test_an_input_a_producer_supplies_is_supplied_to_the_hook_that_asks(
    tmp_path, monkeypatch, toys  # noqa: F811
):
    """nextpnr takes the board's pin file only when no pin file is supplied: a hook that asks
    whether an input is supplied must be answered as the launch answers it, also when a
    producer, not the design, supplies it."""

    class ToyFallback(Flow):
        """Registers a fallback file unless its input is supplied."""

        results_description: ClassVar[dict[str, str]] = {}
        design_source_types = frozenset()

        class Inputs(Flow.Inputs):
            produced: Path = In(
                SourceType.Data,
                producer="toy_producer",
                output="produced",
                description="The producer's text.",
            )

        def locate_prepared_inputs(self):
            return [] if self.inputs.produced is not None else [self.design.root_path / "fallback"]

        def prepare_inputs(self) -> None:
            self.implicit_inputs += self.locate_prepared_inputs()

        def run(self) -> None:
            (self.run_path / "out.txt").write_text(self.inputs.produced.read_text())

    class FallbackGraph(ToyGraph):
        flow, consumer = "toy_fallback", "toy_fallback"

    (tmp_path / "fallback").write_text("a pin file of the board\n")
    graph = FallbackGraph(tmp_path, monkeypatch)
    graph.settle()
    for options in ({}, {"rebuild_all": True}):
        states = compare(graph, options)
        assert states["toy_fallback"].state == (STALE if options else FRESH), states
    graph.edit_source()
    states = compare(graph, {})
    assert states["toy_fallback"].state == UNKNOWN


def test_a_node_that_always_runs_is_stale_and_its_consumer_is_unknown(
    tmp_path, monkeypatch, tools  # noqa: F811
):
    """A fresh random seed: nextpnr runs every time, and says so; its producer is fresh."""
    graph = FpgaGraph(tmp_path, monkeypatch)
    graph.settings.append("randomize_seed=true")
    graph.run()
    options: Options = {}
    states = compare(graph, options)
    assert states["yosys_fpga"].state == FRESH
    assert (states["nextpnr"].state, states["nextpnr"].reason) == (
        STALE,
        "it draws a new random seed",
    )


def test_the_reasons_the_plan_gives_are_the_launch_s_words(tmp_path, monkeypatch, toys):
    graph = ToyGraph(tmp_path, monkeypatch)
    graph.settle()
    graph.edit_source()
    _, states = graph.dry()
    assert states["toy_producer"].reason.startswith("input changed: ")
    assert (states["toy_consumer"].state, states["toy_consumer"].reason) == (
        UNKNOWN,
        "depends on toy_producer",
    )
    _, states = graph.dry(rebuild_all=True)
    assert states["toy_consumer"].reason == "every flow runs (--rebuild-all)"
    _, states = graph.dry(clean=True)
    assert states["toy_consumer"].reason == "every flow runs (--clean)"


def test_a_launch_records_why_every_flow_runs(tmp_path, monkeypatch, toys):
    graph = ToyGraph(tmp_path, monkeypatch)
    graph.settle()
    for option, words in (("rebuild_all", "(--rebuild-all)"), ("clean", "(--clean)")):
        ran = graph.run(**{option: True})
        assert {f.stale_reason for f in ran.values()} == {f"every flow runs {words}"}


def test_a_node_whose_recorded_program_is_a_container_image_is_unknown(tmp_path, monkeypatch, toys):
    """Locating an image is a process (`docker image inspect`): not for a dry run."""
    graph = ToyGraph(tmp_path, monkeypatch)
    graph.settle()
    trace_file = graph.run_dir("toy_consumer") / "trace.json"
    trace = json.loads(trace_file.read_text())
    trace["programs"] = {f"{DOCKER_IMAGE_PREFIX}hdlc/ghdl:yosys": {"path": "sha256:0123"}}
    trace_file.write_text(json.dumps(trace))
    started: list[Any] = []
    real = subprocess.Popen.__init__

    def recording(self, *args, **kwargs):
        started.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", recording)
    _, states = graph.dry()
    assert not started, started
    assert (states["toy_consumer"].state, states["toy_consumer"].reason) == (
        UNKNOWN,
        "its container image is checked when it runs",
    )


# ------------------------------------------------------------------------------- read-only


class Watch:
    """What the dry run did to the world: the tree (the run root included), the writes the audit
    hook saw, the locks taken and the processes started."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.monkeypatch = pytest.MonkeyPatch()  # its own: undoing only what this patched
        self.writes: list[str] = []
        self.processes: list[Any] = []
        self._watching = writes_seen(self.writes)

    def __enter__(self):
        self.before = _state(self.graph.root, [])
        self._watching.__enter__()
        real = subprocess.Popen.__init__

        def recording(proc, *args, **kwargs):
            self.processes.append(args)
            return real(proc, *args, **kwargs)

        self.monkeypatch.setattr(subprocess.Popen, "__init__", recording)
        return self

    def __exit__(self, *exc) -> None:
        self._watching.__exit__(*exc)
        self.monkeypatch.undo()

    def problems(self) -> list[str]:
        found = []
        after = _state(self.graph.root, [])
        changed = sorted(
            k for k in self.before.keys() | after.keys() if self.before.get(k) != after.get(k)
        )
        if changed:
            found.append(f"changed: {changed}")
        if self.writes:
            found.append(f"writes: {self.writes}")
        if self.processes:
            found.append(f"processes: {self.processes}")
        return found


def _dry_run_problems(graph: Graph, monkeypatch, **options) -> list[str]:
    with Watch(graph) as watch:
        graph.dry(**options)
    return watch.problems()


@pytest.mark.parametrize("graph_kind", sorted(GRAPHS))
def test_a_dry_run_changes_nothing_and_starts_nothing(
    graph_kind, tmp_path, monkeypatch, toys, tools  # noqa: F811
):
    """After a full launch and a source edit: the tree is as it was, the run root included; the
    audit hook sees no write, no lock file is made and no process starts."""
    graph = GRAPHS[graph_kind](tmp_path, monkeypatch)
    graph.settle()
    graph.edit_source()
    assert _dry_run_problems(graph, monkeypatch) == []


MISTAKES = {
    "refreshes the trace": "refresh",
    "takes the run directory's lock": "lock",
    "locates a container image": "locate",
}


@pytest.mark.parametrize("mistake", sorted(MISTAKES))
def test_the_read_only_check_finds_each_mistake_that_would_break_it(
    mistake, tmp_path, monkeypatch, toys  # noqa: F811
):
    """Teeth: three ways the judgment could write, lock or start a process, each put into it."""
    from .tool_utils import check_after_the_racy_window

    graph = ToyGraph(tmp_path, monkeypatch)
    graph.settle()
    kind = MISTAKES[mistake]
    if kind == "refresh":
        check_after_the_racy_window(monkeypatch)  # a check that refreshes the trace now would
        real = plan_freshness.check_trace
        monkeypatch.setattr(
            plan_freshness, "check_trace", lambda *a, **k: real(*a, **{**k, "refresh": True})
        )
    elif kind == "lock":
        real = plan_freshness.check_trace

        def locking(run_dir, *args, **kwargs):
            with run_dir_lock(run_dir, graph.run_root):
                return real(run_dir, *args, **kwargs)

        monkeypatch.setattr(plan_freshness, "check_trace", locking)
    else:
        trace_file = graph.run_dir("toy_consumer") / "trace.json"
        trace = json.loads(trace_file.read_text())
        trace["programs"] = {f"{DOCKER_IMAGE_PREFIX}img:tag": {"path": "sha256:0123"}}
        trace_file.write_text(json.dumps(trace))
        monkeypatch.setattr(plan_freshness, "dry_locate", locate_program)
    problems = _dry_run_problems(graph, monkeypatch)
    assert problems, f"a dry run that {mistake} went unnoticed"


def test_the_cli_dry_run_json_carries_state_and_reason(tmp_path, monkeypatch, toys):

    from xeda.introspect import plan_info

    graph = ToyGraph(tmp_path, monkeypatch)
    graph.settle()
    graph.edit_source()
    plan, states = graph.dry()
    nodes = {n["name"]: n for n in plan_info(plan, states)["nodes"]}
    assert nodes["toy_producer"]["state"] == STALE
    assert nodes["toy_producer"]["reason"].startswith("input changed: ")
    assert nodes["toy_consumer"] | {} and nodes["toy_consumer"]["state"] == UNKNOWN
    assert nodes["toy_consumer"]["reason"] == "depends on toy_producer"
    assert "state" not in plan_info(plan)["nodes"][0]


def test_a_cocotb_simulation_is_planned_without_starting_a_process(tmp_path, monkeypatch):
    """A simulator's flow makes its cocotb support when it first uses it, not when it is built:
    making it asks for a version, which is a process."""
    from xeda.tool import Tool

    from .test_isolation import DESIGNS, SQRT_DESIGN, _world

    world = _world(tmp_path)
    rtl, tb = DESIGNS.get("ghdl_sim", SQRT_DESIGN)
    design = Design(
        name="sqrt",
        design_root=world.work,
        rtl=rtl,
        tb=tb,
        language={"vhdl": {"standard": "2008"}},
    )
    graph = Graph.__new__(Graph)
    graph.root, graph.run_root, graph.settings = world.work, world.root, []
    monkeypatch.chdir(world.work)

    def forbidden(*args, **kwargs):
        pytest.fail("a dry run probed a tool")

    monkeypatch.setattr(Tool, "probe_stdout", forbidden)
    graph.design = lambda: design  # type: ignore[method-assign]
    graph.flow = "ghdl_sim"
    with Watch(graph) as watch:
        plan, states = graph.dry()
    assert watch.problems() == []
    assert states["ghdl_sim"].state == STALE, states
    assert states["ghdl_sim"].reason == "no successful previous run"


def _forge_record(graph: Graph, node: str, edit: Callable[[dict], None]) -> None:
    """Rewrite one output record of `node`'s `results.json`, and the trace's record of that
    file to match, so that the node is still fresh: the record alone is what is wrong."""
    from xeda.digest import record_file

    run_dir = graph.run_dir(node)
    results = run_dir / "results.json"
    document = json.loads(results.read_text())
    edit(document["outputs"])
    results.write_text(json.dumps(document))
    trace_file = run_dir / "trace.json"
    trace = json.loads(trace_file.read_text())
    trace["outputs"][str(results.resolve())] = json.loads(record_file(results).model_dump_json())
    trace_file.write_text(json.dumps(trace))


def _as_a_list(outputs: dict) -> None:
    outputs["produced"] = [outputs["produced"]]


def _as_a_table(outputs: dict) -> None:
    outputs["produced"] = outputs["produced"][0]


@pytest.mark.parametrize("always", [False, True], ids=["reusable", "always runs"])
@pytest.mark.parametrize("shape", ["a scalar output as a list", "a list output as a table"])
def test_an_output_record_the_hand_over_refuses_is_unknown_to_a_dry_run(
    shape, always, tmp_path, monkeypatch, toys
):
    """The dry run reads a producer's recorded outputs as the hand-over reads them (one function,
    `outputs.recorded_entries`): a record of the wrong shape for the declared cardinality is not
    handed over, so the consumer cannot be said to be fresh."""
    ToyProducer, ToyConsumer = toys
    if shape == "a list output as a table":
        # a producer whose one output is a list of files, and a consumer that takes the list
        class ManyProducer(ToyProducer):
            """Writes its text to a list output of one file."""

            class Outputs(Flow.Outputs):
                produced: list[Path] = Out(SourceType.Data, description="The copied text.")

            def run(self) -> None:
                out = self.run_path / "outputs" / "produced.txt"
                out.parent.mkdir(exist_ok=True)
                out.write_text(self.design.rtl.sources[0].file.read_text() + self.settings.suffix)
                self.artifacts.produced = out
                self.outputs.produced = [out]

        class ManyConsumer(ToyConsumer):
            """Copies the first file of its producer's list."""

            class Inputs(Flow.Inputs):
                produced: list[Path] = In(
                    SourceType.Data,
                    producer="many_producer",
                    output="produced",
                    description="The producer's text.",
                )

            def run(self) -> None:
                out = self.run_path / "outputs" / "consumed.txt"
                out.parent.mkdir(exist_ok=True)
                out.write_text(self.inputs.produced[0].read_text())
                self.artifacts.consumed = out

        class ManyGraph(ToyGraph):
            flow, producer, consumer = "many_consumer", "many_producer", "many_consumer"

        graph, edit = ManyGraph(tmp_path, monkeypatch), _as_a_table
    else:
        graph, edit = ToyGraph(tmp_path, monkeypatch), _as_a_list
    if always:  # the launch fails to hand the output over before it asks the flow anything
        graph.settings.append(f"flows.{graph.consumer}.always=true")
    if always:
        graph.run()
        graph.run()
    else:
        graph.settle()
    _, states = graph.dry()
    assert states[graph.producer].state == FRESH
    assert states[graph.consumer].state == (STALE if always else FRESH)
    _forge_record(graph, graph.producer, edit)
    _, states = graph.dry()
    assert states[graph.producer].state == FRESH, "only the record is wrong"
    said = states[graph.consumer]
    assert said.state == UNKNOWN, said
    assert f"{graph.producer}'s output `produced` has an invalid record" in said.reason
    # and the launch refuses it
    with pytest.raises(Exception, match="has an invalid record"):
        graph.run()


def test_an_invalid_record_of_a_fresh_producer_is_judged_whatever_another_producer_does(
    tmp_path, monkeypatch, toys
):
    """Two producers: one is fresh and holds a record that the hand-over refuses, the other is
    not fresh. The launch runs the second producer and fails at the hand-over of the first. A
    consumer that would run for its own reasons is therefore unknown, not stale."""

    class ToyOther(toys[0]):
        """Writes its text to outputs/other.txt, as its output `other`."""

        class Outputs(Flow.Outputs):
            other: Path = Out(SourceType.Data, description="The text.")

        def run(self) -> None:
            out = self.run_path / "outputs" / "other.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text("other" + self.settings.suffix)
            self.artifacts.other = out
            self.outputs.other = out

    class ToyBoth(toys[1]):
        """Takes the texts of two producers."""

        class Inputs(Flow.Inputs):
            produced: Path = In(
                SourceType.Data,
                producer="toy_producer",
                output="produced",
                description="The first text.",
            )
            other: Path = In(
                SourceType.Data,
                producer="toy_other",
                output="other",
                description="The second text.",
            )

        def run(self) -> None:
            out = self.run_path / "outputs" / "consumed.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(self.inputs.produced.read_text() + self.inputs.other.read_text())
            self.artifacts.consumed = out

    class BothGraph(ToyGraph):
        flow, producer, consumer = "toy_both", "toy_producer", "toy_both"

    graph = BothGraph(tmp_path, monkeypatch)
    graph.settings.append("flows.toy_both.always=true")
    graph.run()
    graph.run()
    _forge_record(graph, "toy_producer", _as_a_list)
    graph.settings.append("flows.toy_other.suffix=!")  # the second producer is not fresh now
    _, states = graph.dry()
    assert states["toy_producer"].state == FRESH
    assert states["toy_other"].state == STALE
    said = states["toy_both"]
    assert said.state == UNKNOWN and "has an invalid record" in said.reason, said
    with pytest.raises(Exception, match="has an invalid record"):
        graph.run()
