"""The result of a launch: an outcome for every node of the plan.

A launch reports the end of each node to a `LaunchCollector` while it goes on. `launch_result`
runs a launch, and then builds the `Result` from the collector and the plan: the nodes that
ended, and, from the plan, the nodes that did not start. The module is made of free functions and
never imports `default_runner`: `LaunchRunner` types the part of the launcher that it reads.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from ..deliver import Delivered, Delivery, DeliveryError
from ..flow import Flow
from ..outcomes import DeliveryRecord, Disposition, NodeResult, Outcome, Result, aggregate
from ..records import INTERNAL_ERROR, PRODUCER_FAILED, ErrorInfo
from ..spec import Spec
from .error_codes import DELIVERY_FAILED, error_info
from .resolver import Plan, PlanNode

log = logging.getLogger(__name__)

__all__ = ["LaunchCollector", "LaunchRunner", "launch_result"]

#: The outcomes of a producer that stop its consumers.
_STOPS_CONSUMERS = frozenset(
    {Outcome.FAILED, Outcome.REJECTED, Outcome.BLOCKED, Outcome.CANCELED, Outcome.DETACHED}
)


@dataclass(frozen=True)
class _Ended:
    """A node that the launch attempted: its flow, the exception that ended it (if any), and the
    seconds the launch spent on it. The flow is `None` for a node that failed before it had one."""

    flow: Flow | None
    error: BaseException | None
    elapsed: float


class LaunchCollector:
    """What one launch reports while it goes on.

    The launcher makes a new collector at the start of each launch. It tells the collector the
    plan, the end of every node that it started, the deliveries it noted, and the failures of the
    end of the launch."""

    def __init__(self) -> None:
        #: The plan of the launch, once the launcher has resolved it.
        self.plan: Plan | None = None
        self.ended: dict[str, _Ended] = {}
        #: The nodes whose launch has begun and not yet closed, the innermost last.
        self._attempts: list[PlanNode] = []
        #: When each node finished its producers and began its own execution.
        self._own_start: dict[str, float] = {}
        #: The failures of the end of the launch, each as its stage (`delivery`, `clean-up`) and the
        #: exception.
        self.finish_failures: list[tuple[str, Exception]] = []
        #: The failure of the end of the launch that the launch raised: the first of them, when
        #: the requested flow passed.
        self.finish_raised: Exception | None = None
        self._deliveries: dict[int, tuple[DeliveryRecord, ...]] = {}

    def planned(self, plan: Plan) -> None:
        """Note the plan that the launch follows."""
        self.plan = plan

    def attempt_begun(self, node: PlanNode) -> None:
        """Note that the launch begins to act on `node`: from here, the node reports its end,
        whatever raises."""
        self._attempts.append(node)

    def producers_done(self, node: PlanNode) -> None:
        """Note that `node` has its producers and begins its own execution."""
        self._own_start[node.name] = time.monotonic()

    def depth(self) -> int:
        """Return how many nodes have begun and not closed, to hand to `attempt_closed`."""
        return len(self._attempts)

    def attempt_closed(self, depth: int, error: BaseException | None) -> None:
        """Close the attempt that began after `depth` attempts, and note how it ended.

        A node that raised and has no end yet failed before it had a flow. A node whose end has
        another error than `error` failed again while it ended: `error` is what left it. A
        refusal made before the run (`DeliveryError.before_run`) is no failure of a node, and an
        interrupt is no outcome: neither is noted."""
        node = self._attempts[depth] if len(self._attempts) > depth else None
        del self._attempts[depth:]
        if node is None or not isinstance(error, Exception):
            return
        if isinstance(error, DeliveryError) and error.before_run:
            return
        end = self.ended.get(node.name)
        if end is None:
            self.ended[node.name] = _Ended(None, error, 0.0)
        elif end.error is not error:
            self.ended[node.name] = replace(end, error=error)

    def node_ended(self, node: PlanNode, flow: Flow, error: BaseException | None) -> None:
        """Note that the launch started `node` as `flow`, and how it ended: `error` is the
        exception that left the node, or `None`. The time of the node is that of its own
        execution, after its producers."""
        started = self._own_start.get(node.name)
        elapsed = 0.0 if started is None else time.monotonic() - started
        self.ended[node.name] = _Ended(flow, error, elapsed)

    def deliveries_noted(
        self,
        flow: Flow,
        pending: Sequence[tuple[Delivery, Path, Path, str]],
        delivered: Sequence[Delivered],
    ) -> None:
        """Note the deliveries of `flow`: `pending` is what its launch had to deliver, and
        `delivered` is what reached its destination."""
        made = {(done.key, done.destination) for done in delivered}
        self._deliveries[id(flow)] = tuple(
            DeliveryRecord(
                key=named.key,
                destination=destination,
                delivered=(named.key, _located(destination)) in made,
            )
            for named, _source, destination, _digest in pending
        )

    def deliveries_of(self, flow: Flow) -> tuple[DeliveryRecord, ...]:
        """Return the deliveries noted for `flow`."""
        return self._deliveries.get(id(flow), ())

    def finish_failed(self, stage: str, error: Exception) -> None:
        """Note a failure of the end of the launch."""
        self.finish_failures.append((stage, error))

    def accounts_for(self, error: Exception) -> bool:
        """Return whether `error`, which left the launch, is an outcome of a node of the plan or
        a failure of the end of the launch.

        Any other exception left the launch before it could report: a refusal, or a bug."""
        if self.plan is None:
            return False
        if self.finish_raised is error:
            return True
        root = self.ended.get(self.plan.nodes[-1].name)
        return root is not None and root.error is error


class LaunchRunner(Protocol):
    """The part of the launcher that `launch_result` reads: the collector of the current launch."""

    collector: LaunchCollector


def _located(path: Path) -> Path:
    """Return `path` with its parent resolved and its last component as named."""
    return Path(os.path.realpath(path.parent)) / path.name


def _producer_names(plan: Plan, node: PlanNode) -> set[str]:
    """Return the names of the nodes that feed `node`."""
    return {
        plan.node(reference.node).name
        for resolved in node.inputs
        for reference in resolved.references
    }


def _blocked(node: PlanNode, blocker: str, message: str) -> NodeResult:
    return NodeResult(
        name=node.name,
        outcome=Outcome.BLOCKED,
        disposition=Disposition.NOT_RUN,
        reason=f"{blocker} did not pass",
        blocked_by=blocker,
        elapsed=0.0,
        record=None,
        error=ErrorInfo(
            code=PRODUCER_FAILED, message=message, node=node.name, exit_status=None, log=None
        ),
        deliveries=(),
        run_dir=node.run_path,
    )


def _ended_node(node: PlanNode, end: _Ended, collector: LaunchCollector) -> NodeResult:
    """Return the result of a node that the launch started."""
    flow, error = end.flow, end.error
    if flow is None:
        # The node failed before it had a flow: nothing of it ran, and the exception says why.
        assert error is not None
        return NodeResult(
            name=node.name,
            outcome=Outcome.FAILED,
            disposition=Disposition.RAN,
            reason="",
            blocked_by=None,
            elapsed=0.0,
            record=None,
            error=error_info(error, node=node.name),
            deliveries=(),
            run_dir=node.run_path,
        )
    if flow.blocked_by is not None:
        return _blocked(node, flow.blocked_by, str(error))
    deliveries = collector.deliveries_of(flow)
    record = flow.record
    if record is not None and (error is None or record.outcome not in ("passed", "rejected")):
        # The record says how the node ended: a failure that was recorded is the record's.
        reused = flow.reused
        if reused:
            reason = (flow.rejection or "") if flow.rejected_again else "up to date"
        else:
            reason = flow.stale_reason or ""
        return NodeResult(
            name=node.name,
            outcome=record.outcome,
            disposition=Disposition.REUSED if reused else Disposition.RAN,
            reason=reason,
            blocked_by=None,
            elapsed=0.0 if reused else end.elapsed,
            record=record,
            error=record.error,
            deliveries=deliveries,
            run_dir=node.run_path,
        )
    # An exception left the node after the run passed, or the run left no record: nothing vouches
    # for the node, and the exception says why.
    if error is not None:
        cause = error_info(error, node=node.name)
    else:
        cause = ErrorInfo(
            code=INTERNAL_ERROR,
            message=f"`{node.name}` ended without a record",
            node=node.name,
            exit_status=None,
            log=None,
        )
    return NodeResult(
        name=node.name,
        outcome=Outcome.FAILED,
        disposition=Disposition.RAN,
        reason=flow.stale_reason or "",
        blocked_by=None,
        elapsed=end.elapsed,
        record=None,
        error=cause,
        deliveries=deliveries,
        run_dir=node.run_path,
    )


def _not_reached(plan: Plan, node: PlanNode, done: Mapping[str, NodeResult]) -> NodeResult:
    """Return the result of a node that the launch never started.

    It is blocked when one of its producers did not pass: the first of them in plan order blocks
    it. Otherwise the launch stopped before its turn, and it is skipped."""
    producers = _producer_names(plan, node)
    for name in (n.name for n in plan.nodes):
        producer = done.get(name)
        if name in producers and producer is not None and producer.outcome in _STOPS_CONSUMERS:
            cause = producer.error
            return _blocked(
                node,
                name,
                f"producer {name} did not pass"
                + (f" ({cause.code}): {cause.message}" if cause is not None else ""),
            )
    return NodeResult(
        name=node.name,
        outcome=Outcome.SKIPPED,
        disposition=Disposition.NOT_RUN,
        reason="the launch stopped before its turn",
        blocked_by=None,
        elapsed=0.0,
        record=None,
        error=None,
        deliveries=(),
        run_dir=node.run_path,
    )


def _launch_error(
    collector: LaunchCollector, nodes: Mapping[str, NodeResult], roots: Sequence[str]
) -> ErrorInfo | None:
    """Return the error of the whole launch, from the failure at its end that it raised, or None.

    A failed delivery is a launch error. So is a failed clean-up, when every root passed. A
    clean-up that fails after a root did not pass never takes its place as the cause: the root's
    own error stays, and the clean-up failure is only logged."""
    found = next(
        (item for item in collector.finish_failures if item[1] is collector.finish_raised), None
    )
    if found is None:
        return None
    stage, error = found
    if stage == "delivery":
        return ErrorInfo(
            code=DELIVERY_FAILED, message=str(error), node=None, exit_status=None, log=None
        )
    if any(nodes[root].outcome is not Outcome.PASSED for root in roots):
        log.warning("The clean-up failed, after the launch did not pass: %s", error)
        return None
    return error_info(error, node=None)


def launch_result(runner: LaunchRunner, spec: Spec, launch: Callable[[], object]) -> Result:
    """Run `launch`, a launch on `runner`, and return what it did to every node of its plan.

    A node that fails, is rejected or is blocked is an outcome in the `Result`, not an exception.
    So is a failure to deliver the outputs at the end. An exception that left the launch before
    it reported anything propagates: a request that was refused before the plan, a refusal of a
    delivery before the run, a bug. So does `KeyboardInterrupt`: an interrupt is no outcome, and
    the node that it interrupted left no record to read (`results.json` is replaced as a whole)."""
    before = runner.collector
    try:
        launch()
    except Exception as raised:
        collector = runner.collector
        if collector is before or not collector.accounts_for(raised):
            raise
    collector = runner.collector
    plan = collector.plan
    if collector is before or plan is None:
        raise RuntimeError("the launch did not start a launch of the runner")
    nodes: dict[str, NodeResult] = {}
    for node in plan.nodes:
        end = collector.ended.get(node.name)
        nodes[node.name] = (
            _ended_node(node, end, collector)
            if end is not None
            else _not_reached(plan, node, nodes)
        )
    roots = (plan.nodes[-1].name,)
    launch_error = _launch_error(collector, nodes, roots)
    outcome, error = aggregate(nodes, roots, launch_error)
    return Result(
        launch_id=uuid.uuid4().hex,
        spec=spec,
        roots=roots,
        outcome=outcome,
        error=error,
        nodes=nodes,
    )
