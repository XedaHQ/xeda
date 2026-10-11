"""Freshness in a dry run: what a launch of a plan would do with each node, without doing it.

Each node is `fresh` (a launch reuses it), `stale` (a launch runs it, for the reason given, the
words the launch logs) or `unknown` (nothing can be said without making something: a producer
that would run, a container image to look up, a prepared input that does not exist yet).

It judges as the launch does, because it uses what the launch uses: `trace_inputs.expected_run`
for what the node would consume, `trace.check_trace` for the comparison, and the pure hooks of
the flows (`Flow.design_scope`, `input_equivalence`, `locate_prepared_inputs`, `always_runs`).
It is read-only: `check_trace(refresh=False)` makes no clock marker and refreshes no trace, no
lock is taken, no process starts, and nothing is created. A flow is constructed as the launch
constructs it, with a run directory that is not launched (nothing is written there), and is not
initialized.

The order of judgment is the launch's: a flow that always runs; `--rebuild-all` and `--clean`;
then the trace, up to what the producers handed over; then that, if every producer is fresh and
so its recorded results are the ones a launch would hand over.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..deliver import split_deliveries
from ..design import Design
from ..flow import (
    Flow,
    FlowDependencyFailure,
    FlowFatalError,
    FlowSettingsError,
    FlowSettingsException,
)
from ..flow.io import declared_inputs, declared_outputs
from ..proc_utils import DOCKER_IMAGE_PREFIX
from ..run_dir import RunDirectory, RunDirectoryError
from . import launch_input
from .outputs import recorded_entries
from .resolver import Plan, PlanNode
from .trace import (
    BoundProducer,
    DeclaredInputRecord,
    Expectation,
    OutputEntry,
    check_trace,
    locate_program,
)
from .trace_inputs import expected_run

log = logging.getLogger(__name__)

State = Literal["fresh", "stale", "unknown"]

#: why a node runs under `--rebuild-all` and `--clean`: the launch records these too
EVERY_FLOW_RUNS_REBUILD_ALL = "every flow runs (--rebuild-all)"
EVERY_FLOW_RUNS_CLEAN = "every flow runs (--clean)"


@dataclass(frozen=True)
class NodeFreshness:
    """What a launch would do with one node. `reason` is the launch's own for a stale node,
    and says why nothing can be said for an unknown one; empty for a fresh node."""

    state: State
    reason: str = ""


class NeedsAProcess(Exception):
    """Judging the node needs something only a process can tell."""


def dry_locate(name: str) -> str | None:
    """Where the program `name` is now (`trace.locate_program`), except that a container image
    is not looked up: `docker image inspect` is a process (`NeedsAProcess`)."""
    if name.startswith(DOCKER_IMAGE_PREFIX):
        raise NeedsAProcess("its container image is checked when it runs")
    return locate_program(name)


def _results(node: PlanNode) -> dict | None:
    """The recorded results of the node's last run, if they are a success a launch would reuse
    (`launch_report.reuse_results`)."""
    try:
        document = json.loads((node.run_path / "results.json").read_text())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("success") else None


def _entries(results: dict, producer: PlanNode, output: str) -> tuple[OutputEntry, ...] | str:
    """The files of `output` as the recorded results hand them over, read as the hand-over reads
    them (`outputs.recorded_entries`); else why not, as words for an unknown node: no record, or
    one that is not valid."""
    declaration = declared_outputs(producer.flow_class).get(output)
    recorded = (results.get("outputs") or {}).get(output)
    if declaration is None or recorded is None:
        return f"{producer.name} recorded no output `{output}` to hand over"
    try:
        return recorded_entries(
            recorded,
            declaration.cardinality == "many",
            f"{producer.name}'s output `{output}`",
        )
    except FlowDependencyFailure as error:
        return str(error)


def _bare_flow(
    plan: Plan,
    node: PlanNode,
    settings: Flow.Settings,
    design: Design,
    run_root: Path,
    runner_cwd: Path,
    design_hash: str,
    handed: dict[tuple[str, str], tuple[OutputEntry, ...]],
) -> Flow:
    """The flow as the launch constructs it, before `init()`: its run directory is claimed, and
    nothing writes there. Its declared inputs are what the plan wires: the design sources, and the
    files the producers hand over where those are known (`handed`), else a path of the
    producer's run directory standing for the file, so that a hook that asks whether an input
    is supplied (nextpnr's board fallback) answers as the launch does."""
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
    for selected in node.inputs:
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
    return flow


def _records(
    plan: Plan,
    node: PlanNode,
    settings: Flow.Settings,
    entries: dict[tuple[str, str], tuple[OutputEntry, ...]],
) -> tuple[DeclaredInputRecord, ...]:
    """The node's input records as a launch would build them (`launch_producers.run_producers`),
    with the files the producers hand over in `entries` (by node and output) where known."""
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
) -> NodeFreshness:
    flow_class = node.flow_class
    # what the producers hand over, where their recorded results can say (a fresh producer's
    # results are the ones a launch hands over; any other's are about to be made)
    producers = [plan.node(ref.node) for item in node.inputs for ref in item.references]
    waiting = next((p for p in producers if states[p.name].state != "fresh"), None)
    entries: dict[tuple[str, str], tuple[OutputEntry, ...]] = {}
    lacks: NodeFreshness | None = None
    if waiting is None:
        for item in node.inputs:
            for ref in item.references:
                results = _results(plan.node(ref.node))
                found = (
                    f"{ref.node} recorded no output `{ref.output}` to hand over"
                    if results is None
                    else _entries(results, plan.node(ref.node), ref.output)
                )
                if isinstance(found, str):
                    lacks = NodeFreshness("unknown", found)
                else:
                    entries[(ref.node, ref.output)] = found
    try:
        settings = launch_input.input_settings(
            flow_class, node.settings, design, runner_cwd, debug=debug
        )
        split_deliveries(settings, design.name)
        design_hash = design.parts_hash(flow_class.design_parts, flow_class.design_scope(settings))
        flow = _bare_flow(plan, node, settings, design, run_root, runner_cwd, design_hash, entries)
    except (FlowSettingsError, FlowSettingsException, FlowFatalError) as error:
        # a plan checks no more than the request; a launch checks the settings in the design's
        # context, as it constructs the flow, and would refuse these
        return NodeFreshness("unknown", f"a launch refuses its settings: {error}")
    # the launch prepares inputs before it asks whether the flow always runs: what preparation
    # refuses it refuses whatever else is true, and one it can still make does not stop a run
    try:
        registered = flow.locate_prepared_inputs()
    except (FlowFatalError, RunDirectoryError, ValueError, OSError) as error:
        return NodeFreshness("unknown", f"its prepared inputs cannot be located: {error}")
    always = flow.always_runs()
    if always is not None:
        return NodeFreshness("stale", always)
    if clean:
        return NodeFreshness("stale", EVERY_FLOW_RUNS_CLEAN)
    if rebuild_all:
        return NodeFreshness("stale", EVERY_FLOW_RUNS_REBUILD_ALL)
    if registered is None:
        return NodeFreshness(
            "unknown", "a prepared input is not made yet: a launch makes it before judging"
        )
    if lacks is not None:
        return lacks
    expected: Expectation = expected_run(
        flow_class=flow_class,
        name=flow.name,
        flowrun_hash=node.flowrun_hash,
        settings_hash=node.settings_hash,
        design_hash=design_hash,
        run_path=node.run_path,
        design=design,
        input_settings=settings,
        run_root=run_root,
        registered=registered,
        declared=_records(plan, node, settings, entries),
        dependency_run_ids={},
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
    if _results(node) is None:
        return NodeFreshness("stale", f"no successful results in {node.run_path / 'results.json'}")
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
) -> dict[str, NodeFreshness]:
    """What a launch of `plan` would do with each node, by node name, producers first."""
    states: dict[str, NodeFreshness] = {}
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
        )
    return states
