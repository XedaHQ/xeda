"""Whether each node is up to date in a dry run: what a launch would do with each node of a plan.

Each node is in one of five states:

- `fresh`: a launch reuses the node.
- `stale`: a launch runs the node, for the reason the launch logs.
- `unknown`: nothing can be said without making something first. Examples are a producer that
  would run, a container image to look up and a prepared input that does not exist yet.
- `rejected again`: the last run of the node was rejected by its own checks, and nothing it
  consumes changed. A launch reports that rejection again and does not run the tool.
- `blocked`: a producer of the node is `rejected again` or `blocked`. A launch stops at that
  producer, before it prepares anything for the node.

The dry run uses what the launch uses. `trace_inputs.expected_run` says what a node would
consume, `trace.check_trace` compares it with the last run, and the pure hooks of the flows answer
for their settings (`Flow.design_scope`, `input_equivalence`, `locate_prepared_inputs`,
`always_runs`). It reads files and writes nothing: `check_trace(refresh=False)` makes no clock
marker and does not refresh the trace, no lock is taken, no process starts, and nothing is
created. Each flow is constructed as the launch constructs it, with a run directory that no
launch has claimed, and is not initialized.

A node is judged in the order the launch uses:

1. The design it reads. A flow whose `design` input comes from a converter reads the design that
   the converter derives. If the converter is `fresh`, the dry run derives that design from its
   record, after it verified the record read-only (`outputs.verified_design`): the files, the
   sources that pass through, and the design the record was made for. A record that does not
   verify gives the node `unknown`, with the reason of the hand-over. Otherwise the node reads
   the design that the converter is planned to derive (`resolver.planned_design_of`).
2. The settings, which a launch checks against that design. If it refuses them, the node is
   `unknown`.
3. The producers, in the order in which a launch runs them (the converter of the design first).
   A producer that is `rejected again` or `blocked` makes the node `blocked`. The recorded
   outputs of every producer that is `fresh`, whatever the other producers do, must be handed
   over. If one cannot be, the node is `unknown`. A producer that is not `fresh` is not read,
   because it will be rewritten. Of the two problems, the one with the earlier producer decides.
4. The prepared inputs, located. If preparation refuses them, the node is `unknown`.
5. `always_runs()`: the node is `stale`.
6. `--clean`, then `--rebuild-all`: the node is `stale`.
7. A converter that is not `fresh`: the node is `unknown`, with the reason `depends on <converter>`.
   Its trace cannot be judged without the hash of the derived design.
8. A prepared input that is not made yet: the node is `unknown`.
9. The trace: the node is `stale` with the reason of `check_trace`. What the producers handed
   over is compared last, and only when every producer is `fresh`. Otherwise a node that
   nothing above decided is `unknown`, with the reason `depends on <producer>`.
10. `results.json`, only when the trace is current and every producer is `fresh`. The record
   must agree with the verdict of the trace (`launch_report.adoptable_document`). If it does
   not, the node is `stale`. If it does, the node is `fresh` for a run that passed, and
   `rejected again` for a run that was rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Literal

from ..deliver import split_deliveries
from ..design import Design
from ..errors import RecordError
from ..flow import (
    Flow,
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
)
from ..flow.io import declared_inputs, declared_outputs
from ..proc_utils import DOCKER_IMAGE_PREFIX
from ..records import RunRecord, read_record
from ..run_dir import RunDirectory, RunDirectoryError
from . import launch_input
from .outputs import recorded_entries, recorded_outputs, verified_design
from .launch_report import adoptable_document
from .resolver import Plan, PlanNode, planned_design_of
from .trace import (
    BoundProducer,
    DeclaredInputRecord,
    Expectation,
    OutputEntry,
    check_trace,
    locate_program,
)
from .code_digest import CodeDigests
from .trace_inputs import expected_run

log = logging.getLogger(__name__)

State = Literal["fresh", "stale", "unknown", "rejected again", "blocked"]

#: Why a node runs under `--rebuild-all` and `--clean`. The launch records these too.
EVERY_FLOW_RUNS_REBUILD_ALL = "every flow runs (--rebuild-all)"
EVERY_FLOW_RUNS_CLEAN = "every flow runs (--clean)"


@dataclass(frozen=True)
class NodeFreshness:
    """What a launch would do with one node.

    `reason` is the reason that the launch itself gives for a `stale` node. For an `unknown`
    node it says why nothing can be said. For a `rejected again` node it is the message of the
    rejection. For a `blocked` node it names the producer that blocks it. It is empty for a
    `fresh` node."""

    state: State
    reason: str = ""


class NeedsAProcess(Exception):
    """Judging the node needs something only a process can tell."""


def dry_locate(name: str) -> str | None:
    """Return where the program `name` is now (`trace.locate_program`).

    A container image is not looked up: `docker image inspect` is a process
    (`NeedsAProcess`)."""
    if name.startswith(DOCKER_IMAGE_PREFIX):
        raise NeedsAProcess("its container image is checked when it runs")
    return locate_program(name)


def _record(node: PlanNode, run_root: Path) -> RunRecord | None:
    """Return the record of the last run of the node, or None if it cannot be read.

    Whether a launch adopts it depends on the trace (`launch_report.adoptable_document`)."""
    try:
        return read_record(node.run_path, run_root=run_root)
    except RecordError:
        return None


def _entries(record: RunRecord, producer: PlanNode, output: str) -> tuple[OutputEntry, ...] | str:
    """Return the files of `output` as the record of the producer hands them over.

    They are read as the hand-over reads them (`outputs.recorded_entries`). Otherwise return why
    not, as words for an unknown node: no record, or a record that is not valid."""
    declaration = declared_outputs(producer.flow_class).get(output)
    recorded = recorded_outputs(record).get(output)
    if declaration is None or recorded is None:
        return f"{producer.name} recorded no output `{output}` to hand over"
    try:
        return recorded_entries(
            recorded,
            declaration.cardinality == "many",
            f"{producer.name}'s output `{output}`",
            design=declaration.is_design,
        )
    except FlowDependencyFailure as error:
        return str(error)


def _bare_flow(
    node: PlanNode,
    settings: Flow.Settings,
    design: Design,
    run_root: Path,
    runner_cwd: Path,
    design_hash: str,
) -> Flow:
    """Return the flow as the launch constructs it, before `init()`.

    Its run directory is claimed, and nothing writes there. Construction checks the settings
    against the design, as in a launch."""
    flow = node.flow_class(
        settings.model_copy(deep=True),
        design.model_copy(deep=True),
        node.run_path,
        runner_cwd=runner_cwd,
        run_directory=RunDirectory.claimed(node.run_path, run_root),
    )
    flow.flow_hash, flow.settings_hash, flow.design_hash = (
        node.flowrun_hash,
        node.settings_hash,
        design_hash,
    )
    return flow


def _wire_inputs(
    flow: Flow,
    plan: Plan,
    node: PlanNode,
    handed: dict[tuple[str, str], tuple[OutputEntry, ...]],
) -> None:
    """Set the declared inputs of the flow to what the plan wires.

    That is the design sources and, where known (`handed`), the files that the producers hand
    over. Otherwise a path in the run directory of the producer stands for the file. A hook that
    asks whether an input is supplied (the board fallback of nextpnr) then answers as the launch
    does."""
    for selected in node.inputs:
        if declared_inputs(node.flow_class)[selected.name].is_design:
            continue  # the flow reads the design as `self.design`; the input stays unset
        paths = list(selected.sources)
        for ref in selected.references:
            found = handed.get((ref.node, ref.output))
            paths += (
                [Path(entry.path) for entry in found]
                if found
                else [plan.node(ref.node).run_path / ref.output]
            )
        if paths:
            many = declared_inputs(node.flow_class)[selected.name].cardinality == "many"
            setattr(flow.inputs, selected.name, paths if many else paths[0])


def _records(
    plan: Plan,
    node: PlanNode,
    settings: Flow.Settings,
    entries: dict[tuple[str, str], tuple[OutputEntry, ...]],
) -> tuple[DeclaredInputRecord, ...]:
    """Return the input records of the node as a launch would build them.

    The launch builds them in `launch_producers.run_producers`. `entries` holds the files that
    the producers hand over, by node and output, where known."""
    records = []
    for selected in node.inputs:
        references = tuple(
            BoundProducer(
                producer=ref.node,
                output=ref.output,
                producer_hash=plan.node(ref.node).flowrun_hash,
                producer_path=str(plan.node(ref.node).run_path),
                entries=entries.get((ref.node, ref.output), ()),
            )
            for ref in selected.references
        )
        handed = [entry.path for ref in references for entry in ref.entries]
        first = plan.node(selected.references[0].node) if selected.references else None
        records.append(
            DeclaredInputRecord(
                name=selected.name,
                origin=selected.origin,
                producer=selected.producer,
                output=selected.output,
                producer_hash=first.flowrun_hash if first else None,
                producer_path=str(first.run_path) if first else None,
                paths=(*map(str, selected.sources), *handed),
                references=references,
                binding_origin=selected.binding_origin,
                binding_location=selected.binding_location,
                equivalence=node.flow_class.input_equivalence(settings, selected.name),
            )
        )
    return tuple(records)


def _derived_design(
    plan: Plan,
    node: PlanNode,
    design: Design,
    states: dict[str, NodeFreshness],
    run_root: Path,
    run_path: Callable[..., Path],
) -> tuple[Design, tuple[PlanNode, str] | None, tuple[OutputEntry, ...], NodeFreshness | None]:
    """Return the design that `node` reads, as far as a dry run can tell.

    The result is the design, the converter of it (its node and output) or None, the files the
    converter generated, and why the record of a converter that is `fresh` cannot be handed over
    (None if it can, or if there is no such record to read). With a converter that is `fresh`,
    the design is the one derived from its record, which is verified read-only
    (`outputs.verified_design`). Otherwise it is the design the converter is planned to derive,
    or the loaded one if the node has no converter."""
    planned = planned_design_of(plan, node, design, run_path)
    selected = next((item for item in node.inputs if item.name == "design"), None)
    if selected is None or not selected.references:
        return planned, None, (), None
    reference = selected.references[0]
    converter = plan.node(reference.node)
    if states[converter.name].state != "fresh":
        return planned, (converter, reference.output), (), None
    converted = _record(converter, run_root)
    recorded = None if converted is None else recorded_outputs(converted).get(reference.output)
    label = f"{converter.name}'s output `{reference.output}`"
    if recorded is None:
        return (
            planned,
            (converter, reference.output),
            (),
            NodeFreshness("unknown", f"{converter.name} recorded no output `{reference.output}`"),
        )
    try:
        derived, entries = verified_design(
            recorded, RunDirectory.claimed(converter.run_path, run_root), label, design
        )
    except (FlowDependencyFailure, RunDirectoryError) as error:
        return planned, (converter, reference.output), (), NodeFreshness("unknown", str(error))
    return derived, (converter, reference.output), entries, None


def judge_node(
    plan: Plan,
    node: PlanNode,
    design: Design,
    states: dict[str, NodeFreshness],
    *,
    run_root: Path,
    runner_cwd: Path,
    rebuild_all: bool,
    clean: bool,
    debug: bool,
    run_path: Callable[..., Path],
    digests: CodeDigests,
) -> NodeFreshness:
    flow_class = node.flow_class
    # The design the node reads: the one its converter derived when that is `fresh`, else the one
    # it is planned to derive (`_derived_design`).
    view, converter, design_entries, lacks = _derived_design(
        plan, node, design, states, run_root, run_path
    )
    try:
        settings = launch_input.input_settings(
            flow_class, node.settings, view, runner_cwd, debug=debug
        )
        split_deliveries(settings, design.name)
        # The planned design names a file that need not exist yet, so it has no hash. The node is
        # judged on its trace only when its converter is `fresh` (below), and the hash is then
        # that of the derived design.
        known = converter is None or (states[converter[0].name].state == "fresh" and lacks is None)
        design_hash = (
            view.parts_hash(flow_class.design_parts, flow_class.design_scope(settings))
            if known
            else ""
        )
        flow = _bare_flow(node, settings, view, run_root, runner_cwd, design_hash)
    except (FlowSettingsError, FlowSettingsException, FlowFatalError) as error:
        # The plan made the checks of the hooks of the flow. A launch also checks the settings in
        # the context of the design, as it constructs the flow, and would refuse these.
        return NodeFreshness("unknown", f"a launch refuses its settings: {error}")
    # A launch takes the producers in order. One that was rejected in its last run, or is blocked
    # itself, stops the launch before the consumer prepares anything. The recorded results of a
    # producer that is up to date are the ones a launch hands over, whatever the other producers
    # do. A producer that is not up to date runs first and its results are rewritten.
    producers = [plan.node(ref.node) for item in node.inputs for ref in item.references]
    waiting = next((p for p in producers if states[p.name].state != "fresh"), None)
    entries: dict[tuple[str, str], tuple[OutputEntry, ...]] = {}
    if converter is not None and design_entries:
        entries[(converter[0].name, converter[1])] = design_entries
    for item in node.inputs:
        for ref in item.references:
            producer = plan.node(ref.node)
            if states[ref.node].state == "rejected again":
                return lacks or NodeFreshness(
                    "blocked", f"{producer.name} was rejected in its last run"
                )
            if states[ref.node].state == "blocked":
                return lacks or NodeFreshness("blocked", f"{producer.name} is blocked")
            if states[ref.node].state != "fresh":
                continue
            if converter is not None and (ref.node, ref.output) == (
                converter[0].name,
                converter[1],
            ):
                continue  # derived above, with the record verified
            handing = _record(producer, run_root)
            found = (
                f"{ref.node} recorded no output `{ref.output}` to hand over"
                if handing is None
                else _entries(handing, producer, ref.output)
            )
            if isinstance(found, str):
                lacks = lacks or NodeFreshness("unknown", found)
            else:
                entries[(ref.node, ref.output)] = found
    _wire_inputs(flow, plan, node, entries)
    # A launch hands over the outputs before it prepares anything or asks the flow anything. One
    # it cannot hand over stops the launch.
    if lacks is not None:
        return lacks
    # The launch prepares inputs before it asks whether the flow always runs. What preparation
    # refuses, the launch refuses whatever else is true. An input that it can still make does
    # not stop a run.
    try:
        registered = flow.locate_prepared_inputs()
    except (
        FlowFatalError,
        FlowSettingsException,
        RunDirectoryError,
        ValueError,
        OSError,
    ) as error:
        return NodeFreshness("unknown", f"its prepared inputs cannot be located: {error}")
    always = flow.always_runs()
    if always is not None:
        return NodeFreshness("stale", always)
    if clean:
        return NodeFreshness("stale", EVERY_FLOW_RUNS_CLEAN)
    if rebuild_all:
        return NodeFreshness("stale", EVERY_FLOW_RUNS_REBUILD_ALL)
    if converter is not None and states[converter[0].name].state != "fresh":
        # The trace of the node holds the hash of the design its converter derives.
        return NodeFreshness("unknown", f"depends on {converter[0].name}")
    if registered is None:
        return NodeFreshness(
            "unknown", "a prepared input is not made yet: a launch makes it before judging"
        )
    expected: Expectation = expected_run(
        flow_class=flow_class,
        name=flow.name,
        flowrun_hash=node.flowrun_hash,
        settings_hash=node.settings_hash,
        design_hash=design_hash,
        run_path=node.run_path,
        design=view,
        input_settings=settings,
        run_root=run_root,
        registered=registered,
        declared=_records(plan, node, settings, entries),
        dependency_run_ids={},
        digests=digests,
    )
    try:
        freshness = check_trace(
            node.run_path, expected, dry_locate, refresh=False, edge=waiting is None
        )
    except NeedsAProcess as why:
        return NodeFreshness("unknown", str(why))
    if not freshness.fresh:
        return NodeFreshness("stale", freshness.reason)
    if waiting is not None:
        return NodeFreshness("unknown", f"depends on {waiting.name}")
    recorded_run = _record(node, run_root)
    if recorded_run is None or not adoptable_document(
        recorded_run, freshness.verdict, freshness.error, freshness.run_id
    ):
        return NodeFreshness("stale", f"no completed results in {node.run_path / 'results.json'}")
    if freshness.verdict == "rejected":
        assert freshness.error is not None
        return NodeFreshness("rejected again", freshness.error["message"])
    return NodeFreshness("fresh")


def judge_plan(
    plan: Plan,
    design: Design,
    *,
    run_root: Path,
    runner_cwd: Path,
    rebuild_all: bool = False,
    clean: bool = False,
    debug: bool = False,
    run_path: Callable[..., Path],
) -> dict[str, NodeFreshness]:
    """Return what a launch of `plan` would do with each node, by node name, producers first.

    `run_path` is the `run_path_of` of the launcher (`resolver.planned_design_of`)."""
    states: dict[str, NodeFreshness] = {}
    digests = CodeDigests()  # this judgment's: the next one reads the files again
    for node in plan.nodes:
        states[node.name] = judge_node(
            plan,
            node,
            design,
            states,
            run_root=run_root,
            runner_cwd=runner_cwd,
            rebuild_all=rebuild_all,
            clean=clean,
            debug=debug,
            run_path=run_path,
            digests=digests,
        )
    return states
