"""The node stage of a sweep: execute one node of a plan, and read its producers as they are.

A sweep executes each node once, however many of its points have it. The launch of one node is
the launch of a plan that ends at that node. Its producers are not launched: the sweep completed
them already, and the node reads their records under a shared lease (`adopt_completed`).

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher (or a worker) gives `execute_node` the call that runs the node.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from box import Box

from ..cancel import CancelToken
from ..design import Design
from ..errors import RecordError, StalePlanError
from ..events import Event
from ..flow import Flow
from ..outcomes import DeliveryRecord, Disposition, NodeResult, Outcome, Result
from ..records import CANCELED, INTERNAL_ERROR, ErrorInfo, read_record
from ..run_dir import RunDirectory
from .launch_report import results_of
from .launch_run import timestamp_text
from .resolver import Plan, PlanNode, planned_design_of
from .run_lock import CompletedRun
from .trace import output_equivalences

if TYPE_CHECKING:
    from ..sweep import CompletedNode, NodeOutcome, NodeTask

log = logging.getLogger(__name__)

__all__ = [
    "NodeLaunch",
    "NodeRun",
    "PointDelivery",
    "adopt_completed",
    "check_completed",
    "execute_node",
    "plan_up_to",
]


@dataclass(frozen=True)
class NodeRun:
    """What the launcher knows while it launches one node of a sweep: the name of the node, and
    the producers that the sweep completed (the direct producers of the node), by name."""

    node: str
    completed: Mapping[str, CompletedNode]
    #: A point of the sweep delivers: every node of its plan is read from its record, and the
    #: launch delivers for all of them (`FlowLauncher.deliver_point`).
    delivering: bool = False


@dataclass(frozen=True)
class PointDelivery:
    """What the delivery of a point did: the deliveries of each node by name, and the error of the
    launch (`delivery_failed`, or `canceled`), if there is one."""

    deliveries: Mapping[str, tuple[DeliveryRecord, ...]]
    error: ErrorInfo | None


class PredicateToken(CancelToken):
    """A `CancelToken` that is canceled when `predicate` says so."""

    def __init__(self, predicate: Callable[[], bool]) -> None:
        super().__init__()
        self._predicate = predicate

    @property
    def canceled(self) -> bool:
        return self._predicate() or super().canceled


def producers_of(node: PlanNode) -> list[str]:
    """Return the names of the nodes that `node` reads, in the order of its inputs."""
    return list(dict.fromkeys(ref.node for item in node.inputs for ref in item.references))


def plan_up_to(
    plan: Plan, name: str, completed: Mapping[str, CompletedNode], identity: str
) -> Plan:
    """Return the plan that ends at the node `name`: that node and every node it needs.

    The node must have the identity `identity` that the sweep planned. The producers of the node must be the ones that the sweep completed, each in the run
    directory that the sweep planned. Otherwise the files that the sweep planned from are not
    the files that planning finds now, and `StalePlanError` says so."""
    if name not in plan:
        raise StalePlanError(
            f"The plan is stale: the node {name} is not in the plan any more. Plan again.",
            code="stale_plan",
            difference=f"the node {name} is not in the plan",
        )
    target = plan.node(name)
    if target.flowrun_hash != identity:
        raise StalePlanError(
            f"The plan is stale: the identity of {name} changed. Plan again.",
            code="stale_plan",
            difference=f"the identity of {name} changed",
        )
    needed = {target.name}
    for node in reversed(plan.nodes):
        if node.name in needed:
            needed |= {ref.node for item in node.inputs for ref in item.references}
    kept = tuple(node for node in plan.nodes if node.name in needed)
    direct = producers_of(target)
    if sorted(direct) != sorted(completed):
        difference = (
            f"the producers of {name} are {direct}, and the sweep completed {sorted(completed)}"
        )
        raise StalePlanError(
            f"The plan is stale: {difference}. Plan again.",
            code="stale_plan",
            difference=difference,
        )
    for done in completed.values():
        planned = plan.node(done.name).run_path
        if planned != done.run_dir:
            difference = f"the run directory of {done.name} is {planned}, not {done.run_dir}"
            raise StalePlanError(
                f"The plan is stale: {difference}. Plan again.",
                code="stale_plan",
                difference=difference,
            )
    return replace(plan, requested=name, nodes=kept)


def check_completed(plan: Plan, completed: Mapping[str, CompletedNode]) -> None:
    """Refuse a plan whose nodes are not the nodes that the sweep completed, each in the run
    directory that the sweep planned (`StalePlanError`)."""
    names = [node.name for node in plan.nodes]
    if sorted(names) != sorted(completed):
        difference = f"the nodes are {names}, and the sweep completed {sorted(completed)}"
    else:
        moved = [n for n in plan.nodes if n.run_path != completed[n.name].run_dir]
        if not moved:
            return
        difference = f"the run directory of {moved[0].name} is {moved[0].run_path}"
    raise StalePlanError(
        f"The plan is stale: {difference}. Plan again.", code="stale_plan", difference=difference
    )


def adopt_completed(
    node: PlanNode,
    plan: Plan,
    design: Design,
    done: CompletedNode,
    *,
    run_root: Path,
    runner_cwd: Path,
    run_path_of: Callable[..., Path],
    environment: object,
) -> tuple[Flow, CompletedRun]:
    """Return a flow that holds the run that the directory of the producer `node` holds now.

    The caller holds the shared lease of the directory. The record is read as it is: the run that
    the sweep completed (`done.run_id`), or a later run of the same node that another launch made
    meanwhile. A run that passed is handed over to the consumer, and its `run_id` is the one the
    consumer records. Any other record, or none, is a producer that did not pass: the flow
    reports `success` false with the cause, and the consumer is blocked by it. The producer is
    never run here."""
    run_path = node.run_path
    view = planned_design_of(plan, node, design, run_path_of)
    flow = node.flow_class(
        node.settings,
        view.model_copy(deep=True),
        run_path,
        runner_cwd=runner_cwd,
        run_directory=RunDirectory.claimed(run_path, run_root),
        environment=environment,  # type: ignore[arg-type]
    )
    flow.flow_hash = node.flowrun_hash
    flow.settings_hash = node.settings_hash
    flow.configuration = node.configuration
    flow.reused = True
    cause: ErrorInfo | None = None
    try:
        record = read_record(run_path, run_root=run_root)
    except RecordError as problem:
        cause = ErrorInfo(problem.code, problem.message, node.name, None, None)
    else:
        if record.identity != node.flowrun_hash:
            cause = ErrorInfo(
                "missing_record",
                f"{run_path / 'results.json'} holds the run of another configuration of "
                f"{node.name}",
                node.name,
                None,
                None,
            )
        else:
            if record.run_id != done.run_id:
                log.info(
                    "%s was run again by another launch since the sweep completed it: "
                    "using run %s",
                    node.name,
                    record.run_id,
                )
            flow.record = record
            flow.run_id = record.run_id
            flow.started = record.started
            flow.timestamp = timestamp_text(record.started)
            flow.results.update(**results_of(record, output_equivalences(run_path, node.name)))
            flow.artifacts = Box(flow.results["artifacts"])
            flow.outcome = str(record.outcome)  # type: ignore[assignment]
    if cause is not None:
        flow.run_id = done.run_id
        flow.outcome = "failed"
        flow.results.success = False
        flow.results["error"] = cause.as_json_value()
    return flow, CompletedRun.capture(run_path)


class NodeLaunch(Protocol):
    """The call that launches one node of the plan of a task and returns the `Result` of that
    launch. `FlowLauncher.run_node` is behind it. It lives outside this module, so that this
    module never imports the launcher."""

    def __call__(
        self,
        task: NodeTask,
        *,
        canceled: Callable[[], bool],
        emit: Callable[[Event], None],
    ) -> Result: ...


def execute_node(
    task: NodeTask,
    *,
    canceled: Callable[[], bool],
    emit: Callable[[Event], None],
    launch: NodeLaunch,
) -> NodeOutcome:
    """Execute the node `task.node` once, and return how it ended.

    `launch` plans `task.spec` again and checks, before it takes any lock, that the node has the
    identity `task.identity` and that its producers are the ones that the sweep completed
    (`StalePlanError`). It takes, in one acquisition, the lock of the node and the shared
    lease of each completed producer, reads each producer as it is (`adopt_completed`), runs the
    node alone and releases its lock when the node ends. A producer is never run again, and
    `task.rebuild` applies to the node only. `emit` gets the events of that launch."""
    from ..sweep import NodeOutcome

    result = launch(task, canceled=canceled, emit=emit)
    node = result.nodes[task.node]
    error = result.error
    if error is not None and error.node is None and error.code != CANCELED:
        # A failure of the launch itself (the generator): no node of it ran. The node is `skipped`,
        # as it is in the result of a launch, and carries the error of the launch, which has no
        # node. The sweep reads it and gives its points that error.
        return NodeOutcome(task.node, replace(node, error=error), None)
    if node.outcome is Outcome.SKIPPED and not canceled():
        node = replace(
            node,
            outcome=Outcome.FAILED,
            disposition=Disposition.RAN,
            error=ErrorInfo(
                INTERNAL_ERROR, f"`{node.name}` was not reached", node.name, None, None
            ),
        )
    run_id = node.record.run_id if node.record is not None and _executed(node) else None
    return NodeOutcome(task.node, node, run_id)


def _executed(node: NodeResult) -> bool:
    return node.disposition in (Disposition.RAN, Disposition.REUSED)
