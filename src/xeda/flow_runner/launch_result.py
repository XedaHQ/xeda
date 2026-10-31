"""The result of a launch: an outcome for every node of the plan.

A launch reports the end of each node to a `LaunchCollector` while it goes on. `launch_result`
runs a launch, and then builds the `Result` from the collector and the plan: the nodes that
ended, and, from the plan, the nodes that did not start. The module is made of free functions and
never imports `default_runner`: `LaunchRunner` types the part of the launcher that it reads.

The same call carries the two requests of the caller: a `CancelToken` that stops the launch, and
`on_event`, which gets the events of the launch in the calling thread. The collector emits the
events as it hears of the steps. A callback that raises cancels the launch, and the call raises
that exception again once the launch has released everything.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

from ..cancel import CancelToken, observing
from ..events import Event

from ..deliver import Delivered, Delivery, DeliveryError
from ..flow import Flow
from ..generation import GenerationError
from ..log_context import launch_context, tag_launch_id
from ..plan import delivery_key
from ..outcomes import DeliveryRecord, Disposition, NodeResult, Outcome, Result, aggregate
from ..records import CANCELED, INTERNAL_ERROR, PRODUCER_FAILED, ErrorInfo
from ..spec import Spec
from ..utils import Canceled
from .error_codes import CLEANUP_FAILED, DELIVERY_FAILED, GENERATOR_FAILED, error_info
from .resolver import Plan, PlanNode
from .run_lock import callback_guard

log = logging.getLogger(__name__)

__all__ = [
    "CANCELED_ERROR",
    "LaunchCollector",
    "LaunchRunner",
    "blocked_result",
    "failed_result",
    "launch_result",
    "shared_result",
    "skipped_result",
    "stops_consumers",
]

#: The outcomes of a producer that stop its consumers. A canceled producer is not one of them: the
#: launch stopped, and a consumer that it never started is skipped.
_STOPS_CONSUMERS = frozenset({Outcome.FAILED, Outcome.REJECTED, Outcome.BLOCKED, Outcome.DETACHED})


class _Progress:
    """The requests of the caller for one launch, and what the launch told it.

    It answers whether the launch must stop (the token, or a callback that raised) and calls
    `on_event`. An event that a restart of the acquisition would repeat is told once: a node
    starts and finishes once in a launch."""

    def __init__(
        self, launch_id: str, cancel: CancelToken | None, on_event: Callable[[Event], None] | None
    ) -> None:
        self.launch_id = launch_id
        self._cancel = cancel
        self._on_event = on_event
        #: The exception that a callback raised. The launch stops, and the call raises it.
        self.raised: Exception | None = None
        self._told: set[tuple[str, str | None]] = set()

    def canceled(self) -> bool:
        return self.raised is not None or (self._cancel is not None and self._cancel.canceled)

    def emit(
        self,
        kind: str,
        node: str | None = None,
        outcome: Outcome | None = None,
        disposition: Disposition | None = None,
        *,
        locked: bool = False,
    ) -> None:
        """Tell `on_event` about a step. `locked` says that the launch holds locks, so the callback
        may reuse them and take no other (`run_lock.callback_guard`)."""
        if self._on_event is None or self.raised is not None or (kind, node) in self._told:
            return
        self._told.add((kind, node))
        event = Event(kind, self.launch_id, node, outcome, disposition, datetime.now(timezone.utc))
        try:
            if locked:
                with callback_guard():
                    self._on_event(event)
            else:
                self._on_event(event)
        except Exception as error:  # noqa: BLE001 - the launch stops, and the call raises it
            self.raised = error


#: The requests of the launch that runs in this context. The collector of the launch reads it.
_PROGRESS: ContextVar[_Progress | None] = ContextVar("xeda_launch_progress", default=None)


@contextmanager
def _watched(progress: _Progress) -> Iterator[None]:
    """Make `progress` the requests of the launch that runs in this context, for the block."""
    token = _PROGRESS.set(progress)
    try:
        with observing(progress.canceled):
            yield
    finally:
        _PROGRESS.reset(token)


@dataclass(frozen=True)
class _Ended:
    """A node that the launch attempted: its flow, the exception that ended it (if any), and the
    seconds the launch spent on it. The flow is `None` for a node that failed before it had one."""

    flow: Flow | None
    error: BaseException | None
    elapsed: float


@dataclass(frozen=True)
class _FinishFailure:
    """A failure of the end of the launch: its stage (`delivery` or `clean-up`), the exception,
    and the run directory of the flow that the stage acted on."""

    stage: str
    error: Exception
    run_dir: Path


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
        #: The node a refusal made before its run (`DeliveryError.before_run`) stopped, and the
        #: refusal. It is the innermost node that the refusal left.
        self.refused: tuple[PlanNode, Exception] | None = None
        #: The node that was running when an interrupt left the launch (the innermost one).
        self.interrupted: PlanNode | None = None
        #: When each node finished its producers and began its own execution.
        self._own_start: dict[str, float] = {}
        #: The failure of the generator of the design, which ended the launch before any node ran.
        self.generation_failed: GenerationError | None = None
        #: The failures of the end of the launch.
        self.finish_failures: list[_FinishFailure] = []
        #: The failure of the end of the launch that the launch raised: the first of them, when
        #: the requested flow passed.
        self.finish_raised: Exception | None = None
        self._deliveries: dict[int, tuple[DeliveryRecord, ...]] = {}
        #: Whether the launch stopped because it was canceled (`Canceled`).
        self.canceled = False
        self._progress = _PROGRESS.get()

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
        if self._progress is not None:
            self._progress.emit("node_started", node.name, locked=True)

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
        if isinstance(error, Canceled):
            # The launch stopped. A node that has no end yet did not run: it stays unreached.
            self.canceled = True
            return
        if isinstance(error, GenerationError):
            # A failure of the whole launch, before any node ran: no node ended with it.
            if self.generation_failed is None:
                self.generation_failed = error
            return
        if node is None:
            return
        if not isinstance(error, Exception):
            if isinstance(error, KeyboardInterrupt) and self.interrupted is None:
                self.interrupted = node
            return
        if isinstance(error, DeliveryError) and error.before_run:
            if self.refused is None:
                self.refused = (node, error)
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
        end = self.ended[node.name] = _Ended(flow, error, elapsed)
        if isinstance(error, Canceled):
            self.canceled = True
        progress = self._progress
        if progress is not None and (error is None or isinstance(error, Exception)):
            # An exception that is no `Exception` (an interrupt, the restart of the acquisition)
            # is no end of the node.
            progress.emit("node_started", node.name, locked=True)
            result = _ended_node(node, end, self)
            progress.emit(
                "node_finished", node.name, result.outcome, result.disposition, locked=True
            )

    def deliveries_noted(
        self,
        flow: Flow,
        pending: Sequence[tuple[Delivery, Path, Path, str]],
        delivered: Sequence[Delivered],
    ) -> None:
        """Note the deliveries of `flow` (`delivery_records`)."""
        self._deliveries[id(flow)] = delivery_records(pending, delivered)

    def deliveries_of(self, flow: Flow) -> tuple[DeliveryRecord, ...]:
        """Return the deliveries noted for `flow`."""
        return self._deliveries.get(id(flow), ())

    def finish_failed(self, stage: str, error: Exception, run_dir: Path) -> None:
        """Note a failure of the end of the launch, in the stage `stage`, for the flow whose run
        directory is `run_dir`."""
        self.finish_failures.append(_FinishFailure(stage, error, run_dir))
        if isinstance(error, Canceled):
            self.canceled = True

    def accounts_for(self, error: Exception) -> bool:
        """Return whether `error`, which left the launch, is an outcome of a node of the plan or
        a failure of the end of the launch.

        Any other exception left the launch before it could report: a refusal, or a bug."""
        if self.plan is None:
            return False
        if self.finish_raised is error or self.generation_failed is error:
            return True
        root = self.ended.get(self.plan.nodes[-1].name)
        return root is not None and root.error is error

    def settle(self, error: Exception) -> bool:
        """Count `error`, which left the launch with no outcome of a node, as the failure of the
        node it was refused at, if the launch had started. Return whether it did.

        A refusal made before the run of a node, once an earlier node has ended, is a failure of
        that node in a launch that started. A refusal before any node ended is a request that
        was refused: it stays an exception."""
        if self.refused is None or self.refused[1] is not error or not self.ended:
            return False
        self.ended[self.refused[0].name] = _Ended(None, error, 0.0)
        return True


class LaunchRunner(Protocol):
    """The part of the launcher that `launch_result` reads: the collector of the current launch."""

    collector: LaunchCollector


def delivery_records(
    pending: Sequence[tuple[Delivery, Path, Path, str]], delivered: Sequence[Delivered]
) -> tuple[DeliveryRecord, ...]:
    """Return the deliveries of a node. `pending` is what its launch had to deliver, and
    `delivered` is what reached its destination, with the deliveries that Xeda skipped (an
    artifact of `--outputs-to` that a link leads out of the run directory). A pending delivery
    that did not reach its destination, and a skipped one, are not delivered."""
    made = {(done.key, done.destination) for done in delivered if done.state != "skipped"}
    return tuple(
        DeliveryRecord(
            key=delivery_key(named.key),
            destination=destination,
            delivered=(named.key, _located(destination)) in made,
        )
        for named, _source, destination, _digest in pending
    ) + tuple(
        DeliveryRecord(key=delivery_key(done.key), destination=done.destination, delivered=False)
        for done in delivered
        if done.state == "skipped"
    )


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


def stops_consumers(outcome: Outcome) -> bool:
    """Return whether a producer that ended with `outcome` stops its consumers."""
    return outcome in _STOPS_CONSUMERS


def blocked_result(
    name: str, flow: str, run_dir: Path, blocker: str, cause: ErrorInfo | None
) -> NodeResult:
    """Return the result of the node `name` that its producer `blocker` stopped.

    `cause` is the error of the producer. A sweep builds the same result from a public plan."""
    message = f"producer {blocker} did not pass" + (
        f" ({cause.code}): {cause.message}" if cause is not None else ""
    )
    return _blocked(name, flow, run_dir, blocker, message)


def _blocked(name: str, flow: str, run_dir: Path, blocker: str, message: str) -> NodeResult:
    return NodeResult(
        name=name,
        outcome=Outcome.BLOCKED,
        disposition=Disposition.NOT_RUN,
        reason=f"{blocker} did not pass",
        blocked_by=blocker,
        elapsed=0.0,
        record=None,
        error=ErrorInfo(
            code=PRODUCER_FAILED, message=message, node=name, exit_status=None, log=None
        ),
        deliveries=(),
        run_dir=run_dir,
        flow=flow,
    )


def skipped_result(name: str, flow: str, run_dir: Path) -> NodeResult:
    """Return the result of the node `name` that the launch never started."""
    return NodeResult(
        name=name,
        outcome=Outcome.SKIPPED,
        disposition=Disposition.NOT_RUN,
        reason="the launch stopped before its turn",
        blocked_by=None,
        elapsed=0.0,
        record=None,
        error=None,
        deliveries=(),
        run_dir=run_dir,
        flow=flow,
    )


def shared_result(result: NodeResult, point: int) -> NodeResult:
    """Return `result`, the result of a node that a sweep executed for the point `point`, as
    another point of the sweep sees it.

    Only an execution is shared. A node that was reused, blocked or not started is the same in
    every point."""
    if result.disposition is not Disposition.RAN:
        return result
    return replace(
        result, disposition=Disposition.SHARED, elapsed=0.0, reason=f"shared with point {point}"
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
            flow=node.flow_class.name,
        )
    if flow.blocked_by is not None:
        return _blocked(node.name, node.flow_class.name, node.run_path, flow.blocked_by, str(error))
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
            flow=node.flow_class.name,
        )
    if isinstance(error, Canceled):
        # The launch was canceled after the node began and before its run wrote a record.
        return NodeResult(
            name=node.name,
            outcome=Outcome.CANCELED,
            disposition=Disposition.NOT_RUN,
            reason="",
            blocked_by=None,
            elapsed=end.elapsed,
            record=None,
            error=error_info(error, node=node.name),
            deliveries=(),
            run_dir=node.run_path,
            flow=node.flow_class.name,
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
        flow=node.flow_class.name,
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
            return blocked_result(node.name, node.flow_class.name, node.run_path, name, cause)
    return skipped_result(node.name, node.flow_class.name, node.run_path)


CANCELED_ERROR = ErrorInfo(
    code=CANCELED, message="The launch was canceled.", node=None, exit_status=None, log=None
)


def _launch_error(
    collector: LaunchCollector, nodes: Mapping[str, NodeResult], roots: Sequence[str]
) -> ErrorInfo | None:
    """Return the error of the whole launch, or None: a failed generator, or the failure at the
    end of the launch that it raised.

    A failed delivery is a launch error (`delivery_failed`). So is a failed clean-up
    (`cleanup_failed`), when every root passed. The nodes stay as they were. A failure of the end
    of the launch never takes the place of an earlier cause: when a root did not pass, its own
    error stays, and the clean-up failure is only logged. When a delivery failed first, the
    delivery is the cause. A launch that was canceled has the error `canceled`, whatever stopped
    it."""
    if collector.canceled:
        return CANCELED_ERROR
    if collector.generation_failed is not None:
        return ErrorInfo(
            code=GENERATOR_FAILED,
            message=str(collector.generation_failed),
            node=None,
            exit_status=None,
            log=None,
        )
    found = next(
        (item for item in collector.finish_failures if item.error is collector.finish_raised), None
    )
    if found is None:
        return None
    if found.stage == "delivery":
        return ErrorInfo(
            code=DELIVERY_FAILED, message=str(found.error), node=None, exit_status=None, log=None
        )
    if any(nodes[root].outcome is not Outcome.PASSED for root in roots):
        log.warning("The clean-up failed, after the launch did not pass: %s", found.error)
        return None
    return ErrorInfo(
        code=CLEANUP_FAILED,
        message=f"The clean-up of {found.run_dir} failed: {found.error}",
        node=None,
        exit_status=None,
        log=None,
    )


def launch_result(
    runner: LaunchRunner,
    spec: Spec,
    launch: Callable[[], object],
    *,
    cancel: CancelToken | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> Result:
    """Run `launch`, a launch on `runner`, and return what it did to every node of its plan.

    A node that fails, is rejected or is blocked is an outcome in the `Result`, not an exception.
    So is a failure to deliver the outputs at the end, and so is a cancel: when `cancel` says so
    the launch stops what it does (a wait for a lock, a generator, a tool), and the nodes that did
    not run are `skipped`. An exception that left the launch before
    it reported anything propagates: a request that was refused before the plan, a refusal of a
    delivery before the run of its first node, a bug. A refusal at the turn of a later node is the
    failure of that node (`LaunchCollector.settle`). So does `KeyboardInterrupt`: an interrupt is
    no outcome, and the node that it interrupted left no record to read (`results.json` is
    replaced as a whole).

    `on_event` gets the events of the launch, in this thread. An exception of `on_event` cancels
    the launch, which releases everything first. The call then raises that exception, unchanged.

    The launch id is made here, once, as the launch starts; a call made inside another launch of the
    same thread keeps the id of the outer one. An exception that leaves the launch carries it
    (`log_context.launch_id_of`), so that `failed_result` names the same launch."""
    before = runner.collector
    # The launch runs under an id of its own, and each log record of the launch carries it
    # (`xeda_launch_id`). The `Result` names the same id.
    with launch_context() as launch_id:
        progress = _Progress(launch_id, cancel, on_event)
        #: Whether the launch was canceled before it had a plan: it has no node.
        unplanned = False
        try:
            with _watched(progress):
                progress.emit("launch_started")
                try:
                    launch()
                except Exception as raised:
                    collector = runner.collector
                    if isinstance(raised, Canceled) and (
                        collector is before or collector.plan is None
                    ):
                        unplanned = True
                    elif collector is before or not (
                        collector.accounts_for(raised)
                        or collector.settle(raised)
                        or (isinstance(raised, Canceled) and collector.plan is not None)
                    ):
                        tag_launch_id(raised, launch_id)
                        raise
                except BaseException as raised:
                    tag_launch_id(raised, launch_id)
                    raise
        except Exception:
            if progress.raised is None:
                raise
    if progress.raised is not None:
        raise progress.raised
    if unplanned:
        result = Result(
            launch_id=launch_id,
            spec=spec,
            roots=(),
            outcome=Outcome.CANCELED,
            error=CANCELED_ERROR,
            nodes={},
        )
        progress.emit("launch_finished", None, result.outcome)
        if progress.raised is not None:
            raise progress.raised
        return result
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
    result = Result(
        launch_id=launch_id,
        spec=spec,
        roots=roots,
        outcome=outcome,
        error=error,
        nodes=nodes,
    )
    progress.emit("launch_finished", None, outcome)
    if progress.raised is not None:
        raise progress.raised
    return result


def failed_result(
    runner: LaunchRunner,
    spec: Spec,
    plan: Plan | None,
    launch_error: ErrorInfo,
    *,
    interrupted: bool = False,
    launch_id: str | None = None,
) -> Result:
    """Return the result of a launch that an exception ended outside the nodes.

    The nodes are those of the plan of the launch, or of `plan` if the launch reported none: a node
    that ended keeps its outcome, and a node that did not start is `skipped` or `blocked`. With
    `interrupted`, the node that was running is `canceled`. `launch_error` is the error of the
    launch, and it decides the outcome. `launch_id` is the id of the launch that the exception left
    (`log_context.launch_id_of`), so that the result names the launch of its log records."""
    collector = runner.collector
    ended = collector.ended if collector.plan is not None else {}
    plan = collector.plan or plan
    nodes: dict[str, NodeResult] = {}
    for node in plan.nodes if plan is not None else ():
        assert plan is not None
        end = ended.get(node.name)
        # A node that ended before the interrupt left its attempt keeps its end.
        running = interrupted and (
            (end is None and collector.interrupted is node)
            or (end is not None and isinstance(end.error, KeyboardInterrupt))
        )
        if end is not None and not running:
            nodes[node.name] = _ended_node(node, end, collector)
        elif running:
            nodes[node.name] = NodeResult(
                name=node.name,
                outcome=Outcome.CANCELED,
                disposition=Disposition.RAN,
                reason="",
                blocked_by=None,
                elapsed=0.0,
                record=None,
                error=ErrorInfo(
                    code=CANCELED,
                    message=f"`{node.name}` was running when the launch was interrupted",
                    node=node.name,
                    exit_status=None,
                    log=None,
                ),
                deliveries=(),
                run_dir=node.run_path,
                flow=node.flow_class.name,
            )
        else:
            nodes[node.name] = _not_reached(plan, node, nodes)
    roots = (plan.nodes[-1].name,) if plan is not None else ()
    outcome, error = aggregate(nodes, roots, launch_error)
    return Result(
        launch_id=launch_id or uuid.uuid4().hex,
        spec=spec,
        roots=roots,
        outcome=outcome,
        error=error,
        nodes=nodes,
    )
