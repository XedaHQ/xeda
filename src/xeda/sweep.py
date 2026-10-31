"""A sweep: many specs, each node executed once, and a table of what came out.

`Workspace.sweep` plans every point first. The nodes of all points form one graph, keyed by the
identity of a node: a node that several points have is one node, and it executes once. The
scheduler starts a node when every producer of it has completed. A producer that did not pass
blocks its consumers in every point. Each node executes through `execute_node` (`run_node`), as a
launch of that node alone whose producers are read from their records. With `max_workers` of 1 it
runs in this process. With more, it runs in a worker process (`worker_main`), and the nodes that
do not wait for each other run at once.

A point gets a `Result` as a launch does. A node that another point executed is `shared` there.
"""

from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import signal
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cancel import CancelToken, never_canceled, observing
from .design import Design
from .errors import RecordError, SpecError, StalePlanError
from .events import Event
from .flow import registered_flows
from .flow_runner import launch_run
from .flow_runner.environment import EnvironmentSnapshot
from .flow_runner.launch_identity import RunDirPolicy
from .flow_runner.launch_node import NodeLaunch, PointDelivery, execute_node
from .flow_runner.launch_options import LaunchOptions, WorkspaceOptions
from .flow_runner.launch_report import results_of
from .flow_runner.launch_result import (
    CANCELED_ERROR,
    blocked_result,
    shared_result,
    skipped_result,
    stops_consumers,
)
from .flow_runner.run_lock import LockRequest, acquire_all
from .frozen import thaw
from .outcomes import Disposition, NodeResult, Outcome, Result, aggregate
from .plan import Plan, PlanNode
from .proc_utils import CANCEL_POLL
from .records import ErrorInfo, read_record
from .run_dir import RunDirectory
from .spec import Spec
from .utils import Canceled

if TYPE_CHECKING:
    import pandas

    from .workspace import Workspace

log = logging.getLogger(__name__)

__all__ = [
    "CompletedNode",
    "NodeOutcome",
    "NodeTask",
    "SweepPoint",
    "SweepResult",
    "WorkerChannel",
    "node_key",
    "run_node",
    "run_sweep",
    "worker_main",
]


@dataclass(frozen=True)
class SweepPoint:
    """One spec of a sweep: its position in the input, the spec and what its launch did."""

    index: int
    spec: Spec
    result: Result


@dataclass(frozen=True)
class SweepResult:
    """What a sweep did: one `SweepPoint` for each spec, in input order.

    `canceled` is true when the sweep stopped before every point finished. A point that did not
    start has the outcome `canceled`, the error `canceled` and only `skipped` nodes."""

    points: tuple[SweepPoint, ...]
    canceled: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))

    def __iter__(self) -> Iterator[SweepPoint]:
        return iter(self.points)

    def __len__(self) -> int:
        return len(self.points)

    def counts(self) -> Mapping[Outcome, int]:
        """Return how many points ended with each outcome. An outcome that no point has is absent."""
        found = Counter(point.result.outcome for point in self.points)
        return {outcome: found[outcome] for outcome in Outcome if found[outcome]}

    def rows(self, *, metrics: Sequence[str] = ()) -> list[dict[str, Any]]:
        """Return one table row for each point, as a dict.

        The keys are `index`, `flow` (the flow or chain of the spec), `design`, `target`,
        `outcome`, `error_code` and `run_dir` (of the root node); then `design.<path>` for each
        leaf of the updates of the design (`Design.updates`, merged), then `settings.<node>.<path>`
        for each leaf of the settings of the spec, then one key for each of `metrics`. A metric is
        named `<node>.<path>`, as `nextpnr.fmax`, and its key is that text. A setting of a node that
        has a record is the value that the node ran with, in the canonical text of a quantity.

        A metric reads the record of its node in that point, whatever the outcome of the point. Its
        value is `None` when the node has no record there or the record has no such metric. A
        `KeyError` names every node of the sweep when a metric names a node that no point has."""
        names = {name for point in self.points for name in point.result.nodes}
        for text in metrics:
            if text.partition(".")[0] not in names or "." not in text:
                raise KeyError(
                    f"`{text}` names no node of this sweep. Write `<node>.<metric>`, such as "
                    f"`nextpnr.fmax`. The nodes are: {', '.join(sorted(names))}."
                )
        rows = [self._row(point, metrics) for point in self.points]
        keys = list(dict.fromkeys(key for row in rows for key in row))
        return [{key: row.get(key) for key in keys} for row in rows]

    @staticmethod
    def _row(point: SweepPoint, metrics: Sequence[str]) -> dict[str, Any]:
        result, spec = point.result, point.spec
        root = result.nodes[result.roots[0]] if result.roots else None
        row: dict[str, Any] = {
            "index": point.index,
            "flow": spec.flow,
            "design": spec.design.name,
            "target": spec.design.target,
            "outcome": str(result.outcome),
            "error_code": None if result.error is None else result.error.code,
            "run_dir": None if root is None else root.run_dir,
        }
        updates: dict[str, Any] = {}
        for update in spec.design.updates:
            updates = _merged(updates, Design.expanded(thaw(update)))
        for path, value in _leaves(updates):
            row[f"design.{path}"] = value
        for path, value in _leaves(thaw(spec.settings)):
            node, _, rest = path.partition(".")
            record = result.nodes[node].record if node in result.nodes else None
            effective = _found(thaw(record.settings), rest) if record is not None else None
            row[f"settings.{path}"] = value if effective is None else effective
        for text in metrics:
            node, _, metric = text.partition(".")
            record = result.nodes[node].record if node in result.nodes else None
            has = record is not None and metric in record.metric_info
            row[text] = thaw(record.metric(metric)) if has and record is not None else None
        return row

    def to_pandas(self, *, metrics: Sequence[str] = ()) -> "pandas.DataFrame":
        """Return `rows(metrics=...)` as a pandas `DataFrame`. Raise `SpecError`
        (`missing_dependency`) when pandas is not installed."""
        try:
            import pandas
        except ImportError:
            raise SpecError(
                "`to_pandas` needs pandas: install it, or use `rows`", code="missing_dependency"
            ) from None
        return pandas.DataFrame(self.rows(metrics=metrics))


def _merged(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        old = merged.get(key)
        merged[key] = (
            _merged(old, value) if isinstance(old, dict) and isinstance(value, Mapping) else value
        )
    return merged


def _leaves(table: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield the dotted path and the value of each leaf of `table`. A list is a leaf."""
    for key, value in table.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            yield from _leaves(value, path)
        else:
            yield path, value


def _found(table: Mapping[str, Any], path: str) -> Any:
    """Return the value at the dotted `path` of `table`, or `None`."""
    value: Any = table
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return None if isinstance(value, Mapping) else value


# ---------------------------------------------------------------------------- the node interface


@dataclass(frozen=True)
class CompletedNode:
    """A producer that the sweep completed: its run directory, the run that the sweep saw there,
    and how it ended."""

    name: str
    run_dir: Path
    run_id: str
    outcome: Outcome


@dataclass(frozen=True)
class NodeTask:
    """What `execute_node` needs to execute one node: a value that crosses a process boundary.

    `node` is the name of the node in the plan of `spec`, and `identity` is the identity that the
    sweep planned for it. `completed` are its producers. `rebuild`
    applies to the node only, never to a producer. `workspace.confirm` is `None`, since a task
    asks nothing."""

    spec: Spec
    node: str
    identity: str
    workspace: WorkspaceOptions
    launch: LaunchOptions
    snapshot: EnvironmentSnapshot
    completed: tuple[CompletedNode, ...]
    rebuild: bool
    #: The generation that the sweep settled for the point (`launch_generation.fingerprints`),
    #: which the launch verifies under its leases. Empty: nothing to verify.
    generation: tuple[tuple[str, str], ...] = ()
    #: The modules, by name, that define the flows of the point that Xeda does not have built in. A
    #: worker imports them before it plans, so that the flows are registered there.
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeOutcome:
    """How a node ended: its result, and the `run_id` of the run that it holds (`None` when it
    did not execute or was not reused)."""

    node: str
    result: NodeResult
    run_id: str | None


def launch_node_result(
    task: NodeTask, *, canceled: Callable[[], bool], emit: Callable[[Event], None]
) -> Result:
    """Launch the node of `task` in this process, and return the `Result` of that launch."""
    from .flow_runner import DefaultRunner
    from .proc_utils import TOOL_OUTPUT

    options = task.workspace
    runner = DefaultRunner(
        options.run_root,
        hashed_run_dirs=options.hashed_run_dirs,
        display_results=False,
        run_root_start=options.start,
    )
    token = TOOL_OUTPUT.set(options.tool_output)  # type: ignore[arg-type]
    try:
        return runner.run_node(
            task.spec,
            node=task.node,
            identity=task.identity,
            completed={done.name: done for done in task.completed},
            local_settings=options.local_settings,
            options=task.launch,
            rebuild=task.rebuild,
            generation=task.generation,
            environment=task.snapshot,
            canceled=canceled,
            on_event=emit,
        )
    finally:
        TOOL_OUTPUT.reset(token)


def generate_points(
    specs: Sequence[Spec],
    options: WorkspaceOptions,
    launch: LaunchOptions,
    snapshot: EnvironmentSnapshot,
    canceled: Callable[[], bool],
) -> list[Any]:
    """Settle the generation of every spec in this process: see `FlowLauncher.generate_points`."""
    from .flow_runner import DefaultRunner
    from .proc_utils import TOOL_OUTPUT

    runner = DefaultRunner(
        options.run_root,
        hashed_run_dirs=options.hashed_run_dirs,
        display_results=False,
        run_root_start=options.start,
    )
    token = TOOL_OUTPUT.set(options.tool_output)  # type: ignore[arg-type]
    try:
        return runner.generate_points(
            specs,
            local_settings=options.local_settings,
            options=launch,
            environment=snapshot,
            canceled=canceled,
        )
    finally:
        TOOL_OUTPUT.reset(token)


def deliver_point(
    spec: Spec,
    completed: Mapping[str, CompletedNode],
    generation: tuple[tuple[str, str], ...],
    options: WorkspaceOptions,
    snapshot: EnvironmentSnapshot,
    canceled: Callable[[], bool],
) -> PointDelivery:
    """Deliver a point of the sweep in this process: see `FlowLauncher.deliver_point`."""
    from .flow_runner import DefaultRunner
    from .proc_utils import TOOL_OUTPUT

    runner = DefaultRunner(
        options.run_root,
        hashed_run_dirs=options.hashed_run_dirs,
        display_results=False,
        run_root_start=options.start,
    )
    token = TOOL_OUTPUT.set(options.tool_output)  # type: ignore[arg-type]
    try:
        return runner.deliver_point(
            spec,
            completed=completed,
            generation=generation,
            local_settings=options.local_settings,
            environment=snapshot,
            canceled=canceled,
        )
    finally:
        TOOL_OUTPUT.reset(token)


_LAUNCH: NodeLaunch = launch_node_result


def run_node(
    task: NodeTask, *, canceled: Callable[[], bool], emit: Callable[[Event], None]
) -> NodeOutcome:
    """Execute the node of `task` in this process (`launch_node.execute_node`)."""
    return execute_node(task, canceled=canceled, emit=emit, launch=_LAUNCH)


# ------------------------------------------------------------------------------ worker processes


@dataclass(frozen=True)
class WorkerChannel:
    """What the calling process shares with its workers: the request to stop.

    `cancel` is an event of a `multiprocessing.Manager`, so that it crosses the process boundary.
    The events of the sweep never cross it: the calling process tells them from the results of the
    nodes, so a worker never waits for the callback."""

    cancel: Any


def worker_main(task: NodeTask, channel: WorkerChannel) -> NodeOutcome:
    """Execute the node of `task` in a worker process, and return how it ended.

    The node stops when `channel.cancel` is set, or when the calling process is gone. No exception
    leaves this function: an exception that the calling process cannot unpickle would lose the
    result. A failure is plain data, an outcome with an error code."""
    from .flow_runner.error_codes import error_info

    def canceled() -> bool:
        try:
            return bool(channel.cancel.is_set())
        except (OSError, EOFError):  # the manager of the calling process is gone
            return True

    try:
        for name in task.modules:
            importlib.import_module(name)
        return run_node(task, canceled=canceled, emit=lambda event: None)
    except StalePlanError as error:
        info = ErrorInfo(error.code or "stale_plan", str(error), task.node, None, None)
    except Exception as error:  # noqa: BLE001 - the failure of a node is a value
        info = error_info(error, node=task.node)
    # The calling process knows the flow and the run directory of the node: it fills them in.
    result = NodeResult(
        name=task.node,
        outcome=Outcome.FAILED,
        disposition=Disposition.RAN,
        reason="",
        blocked_by=None,
        elapsed=0.0,
        record=None,
        error=info,
        deliveries=(),
        run_dir=Path(),
        flow="",
    )
    return NodeOutcome(task.node, result, None)


def _ignore_interrupt() -> None:
    """Start a worker that leaves Ctrl-C to the calling process, which stops it through the
    channel."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


# ------------------------------------------------------------------------------------ the sweep


def node_key(point: int, node: PlanNode, design_root: Path) -> tuple[Any, ...]:
    """Return the key of `node`, a node of the plan of the point `point`, in the graph of a sweep.

    Nodes of equal identity that read the same design are one node, and it executes once. The
    identity holds no content and no place, so the key holds the root of the design too: files
    of one relative name in two roots are two inputs. A node that acts (it programs a device) is
    never shared: its key holds its point."""
    return (point, node.name) if node.action else (design_root, node.identity)


def _root_of(spec: Spec) -> Path:
    return Path(os.path.realpath(spec.design.root))


@dataclass
class _Unit:
    """One node of the graph of the sweep."""

    key: tuple[Any, ...]
    #: The first point that has the node, and the name that the node has in it.
    point: int
    node: PlanNode
    producers: list[tuple[Any, ...]] = field(default_factory=list)
    #: Every point that has it, with the name.
    users: list[tuple[int, str]] = field(default_factory=list)
    result: NodeResult | None = None
    run_id: str | None = None
    #: The error of a failure of the launch itself (the generator): no node of it ran.
    launch_error: ErrorInfo | None = None


class _Point:
    """The state of one point: its plan, the results that it has so far, and its events."""

    def __init__(self, index: int, plan: Plan) -> None:
        self.index = index
        self.plan = plan
        self.names = list(plan.nodes)
        self.results: dict[str, NodeResult] = {}
        self.launch_id = os.urandom(16).hex()
        self.told: set[tuple[str, str | None]] = set()
        self.finished: Result | None = None
        #: The error of the launch that a node reported (`_Unit.launch_error`).
        self.launch_error: ErrorInfo | None = None
        #: What the sweep generated for the point, which its launches verify.
        self.generation: tuple[tuple[str, str], ...] = ()


class _Sweep:
    def __init__(
        self,
        workspace: Workspace,
        plans: list[Plan],
        specs: list[Spec],
        launch: LaunchOptions,
        options: LaunchOptions,
        cancel: CancelToken | None,
        on_event: Callable[[Event], None] | None,
        snapshot: EnvironmentSnapshot,
        max_workers: int = 1,
    ) -> None:
        self.workspace = workspace
        self.max_workers = max_workers
        self.specs = specs
        #: The options of each node launch, and those of the sweep (the clean-up at its end).
        self.launch = launch
        self.options = options
        self.rebuild = options.rebuild_all or options.clean
        self.cancel = cancel
        self.on_event = on_event
        self.snapshot = snapshot
        self.points = [_Point(index, plan) for index, plan in enumerate(plans)]
        self.units: dict[tuple[Any, ...], _Unit] = {}
        self.order: list[_Unit] = []
        self.keys: list[dict[str, tuple[Any, ...]]] = []
        for point in self.points:
            keys: dict[str, tuple[Any, ...]] = {}
            for name, node in point.plan.nodes.items():
                key = keys[name] = node_key(point.index, node, _root_of(specs[point.index]))
                unit = self.units.get(key)
                if unit is None:
                    unit = self.units[key] = _Unit(key, point.index, node)
                    self.order.append(unit)
                    for item in node.inputs:
                        if item.producer is not None and keys[item.producer] not in unit.producers:
                            unit.producers.append(keys[item.producer])
                unit.users.append((point.index, name))
            self.keys.append(keys)

    # ------------------------------------------------------------------------------- events

    def canceled(self) -> bool:
        return self.cancel is not None and self.cancel.canceled

    def _emit(
        self,
        point: _Point,
        kind: str,
        node: str | None = None,
        outcome: Outcome | None = None,
        disposition: Disposition | None = None,
    ) -> None:
        if self.on_event is None or (kind, node) in point.told:
            return
        point.told.add((kind, node))
        self.on_event(
            Event(kind, point.launch_id, node, outcome, disposition, datetime.now(timezone.utc))
        )

    def _begin(self, point: _Point) -> None:
        self._emit(point, "launch_started")

    def _advance(self, point: _Point) -> None:
        """Tell the events of the nodes of `point` that have a result, in plan order."""
        for name in point.names:
            result = point.results.get(name)
            if result is None:
                return
            self._begin(point)
            self._emit(point, "node_started", name)
            self._emit(point, "node_finished", name, result.outcome, result.disposition)
        self._finish(point)

    def _finish(self, point: _Point) -> None:
        if point.finished is not None:
            return
        nodes: dict[str, NodeResult] = {}
        stopped = False
        for name, node in point.plan.nodes.items():
            result = point.results.get(name)
            if result is None:
                stopped = True
                result = skipped_result(name, node.flow, node.run_dir)
            nodes[name] = result
        roots = tuple(point.plan.roots)
        error = point.launch_error
        if error is None and stopped:
            error = CANCELED_ERROR
        elif error is None and nodes[roots[0]].outcome is Outcome.PASSED:
            # As a launch does: a request that came after the last node delivers nothing.
            error = CANCELED_ERROR if self.canceled() else self._deliver(point, nodes)
        outcome, cause = aggregate(nodes, roots, error)
        point.finished = Result(
            launch_id=point.launch_id,
            spec=self.specs[point.index],
            roots=roots,
            outcome=outcome,
            error=cause,
            nodes=nodes,
        )
        self._begin(point)
        self._emit(point, "launch_finished", None, outcome)

    def _deliver(self, point: _Point, nodes: dict[str, NodeResult]) -> ErrorInfo | None:
        """Deliver the outputs of `point`, which ended with its root passed, as a launch of the
        point alone would. Put the deliveries in `nodes`, and return the error of the launch."""
        if not any(node.deliveries for node in point.plan.nodes.values()):
            return None
        completed = {}
        for name, result in nodes.items():
            unit = self.units[self.keys[point.index][name]]
            assert unit.run_id is not None, f"{name} passed with no run"
            completed[name] = CompletedNode(name, result.run_dir, unit.run_id, result.outcome)
        done = deliver_point(
            self.specs[point.index],
            completed,
            point.generation,
            self.workspace._options(),
            self.snapshot,
            self.canceled,
        )
        for name in nodes:
            nodes[name] = replace(nodes[name], deliveries=done.deliveries.get(name, ()))
        return done.error

    # ----------------------------------------------------------------------------- scheduling

    def _complete(self, unit: _Unit, result: NodeResult, run_id: str | None) -> None:
        if result.outcome is Outcome.SKIPPED and result.error is not None:
            # A failure of the launch: its error has no node, and the node is skipped.
            unit.launch_error, result = result.error, replace(result, error=None)
            for index, _ in unit.users:
                self.points[index].launch_error = unit.launch_error
        unit.result, unit.run_id = result, run_id
        for index, name in unit.users:
            point = self.points[index]
            point.results[name] = (
                result if index == unit.point else shared_result(result, unit.point)
            )
        for index, _ in unit.users:
            self._advance(self.points[index])

    def _blocked(self, unit: _Unit) -> NodeResult | None:
        for key in unit.producers:
            done = self.units[key].result
            if done is not None and stops_consumers(done.outcome):
                node = unit.node
                return blocked_result(node.name, node.flow, node.run_dir, done.name, done.error)
        return None

    def _task(self, unit: _Unit) -> NodeTask:
        completed = []
        for key in unit.producers:
            producer = self.units[key]
            assert producer.result is not None and producer.run_id is not None
            completed.append(
                CompletedNode(
                    producer.node.name,
                    producer.node.run_dir,
                    producer.run_id,
                    producer.result.outcome,
                )
            )
        return NodeTask(
            spec=self.specs[unit.point],
            node=unit.node.name,
            identity=unit.node.identity,
            workspace=replace(self.workspace._options(), confirm=None),
            launch=self.launch,
            snapshot=self.snapshot,
            completed=tuple(completed),
            rebuild=self.rebuild,
            generation=self.points[unit.point].generation,
            modules=_flow_modules(self.points[unit.point].plan) if self.max_workers > 1 else (),
        )

    def _execute(self, unit: _Unit) -> None:
        """Execute `unit` in this process."""
        from .flow_runner.error_codes import error_info

        point = self.points[unit.point]
        self._begin(point)
        self._emit(point, "node_started", unit.node.name)
        try:
            outcome = run_node(self._task(unit), canceled=self.canceled, emit=lambda event: None)
        except (StalePlanError, KeyboardInterrupt):
            raise
        except Exception as error:  # noqa: BLE001 - the failure of a node is a value
            self._failed(unit, error_info(error, node=unit.node.name))
            return
        self._ran(unit, outcome)

    def _failed(self, unit: _Unit, error: ErrorInfo) -> None:
        node = unit.node
        result = NodeResult(
            name=node.name,
            outcome=Outcome.FAILED,
            disposition=Disposition.RAN,
            reason="",
            blocked_by=None,
            elapsed=0.0,
            record=None,
            error=error,
            deliveries=(),
            run_dir=node.run_dir,
            flow=node.flow,
        )
        self._complete(unit, result, None)

    def _ran(self, unit: _Unit, outcome: NodeOutcome) -> None:
        result = outcome.result
        if result.outcome is Outcome.BLOCKED and result.blocked_by is not None:
            blocker = next(
                (
                    self.units[k].result
                    for k in unit.producers
                    if self.units[k].node.name == result.blocked_by
                ),
                None,
            )
            if blocker is not None and not stops_consumers(blocker.outcome):
                # Another launch replaced the producer after the sweep completed it, and the node
                # read the run that is there now. The result of the producer stays what the sweep
                # saw, so the cause is the node itself: it did not pass.
                result = replace(result, outcome=Outcome.FAILED, disposition=Disposition.RAN)
        self._complete(unit, result, outcome.run_id)

    def _settle(self, unit: _Unit) -> bool:
        """Complete `unit` with no execution when a producer of it, or its point, failed before
        it. Return whether it did. Every producer of `unit` has completed."""
        inherited = self.points[unit.point].launch_error or next(
            (self.units[k].launch_error for k in unit.producers if self.units[k].launch_error),
            None,
        )
        node = unit.node
        if inherited is not None:
            skipped = skipped_result(node.name, node.flow, node.run_dir)
            self._complete(unit, replace(skipped, error=inherited), None)
            return True
        blocked = self._blocked(unit)
        if blocked is not None:
            self._complete(unit, blocked, None)
            return True
        return False

    # ----------------------------------------------------------------------------- the workers

    def _run_in_workers(self) -> None:
        """Execute the nodes in worker processes, at most `max_workers` at once.

        A node starts when every producer of it has completed. The loop tells the events, and
        completes a node that a worker finished, in this thread. When the token says stop, the
        workers get the request, and nothing else starts. When anything raises (a callback, a
        stale plan, Ctrl-C), the workers get the request too, the loop waits until each has
        released its locks, and then the exception goes on."""
        context = multiprocessing.get_context("spawn")
        running: dict[Future[NodeOutcome], _Unit] = {}
        with (
            context.Manager() as manager,
            ProcessPoolExecutor(
                self.max_workers, mp_context=context, initializer=_ignore_interrupt
            ) as pool,
        ):
            channel = WorkerChannel(manager.Event())
            try:
                self._schedule(pool, channel, running)
            except BaseException:
                channel.cancel.set()
                wait(running)
                raise

    def _schedule(
        self,
        pool: ProcessPoolExecutor,
        channel: WorkerChannel,
        running: dict[Future[NodeOutcome], _Unit],
    ) -> None:
        # `order` has every producer before its consumers, so one pass sees what a pass settles.
        pending = list(self.order)
        while pending or running:
            if self.canceled():
                channel.cancel.set()
                pending.clear()
            waiting: list[_Unit] = []
            for unit in pending:
                if self.canceled() or any(self.units[key].result is None for key in unit.producers):
                    waiting.append(unit)
                elif self._settle(unit):
                    continue
                elif len(running) < self.max_workers:
                    self._start(unit, pool, channel, running)
                else:
                    waiting.append(unit)
            pending = waiting
            if running:
                done, _ = wait(running, timeout=CANCEL_POLL, return_when=FIRST_COMPLETED)
                for future in done:
                    self._collect(running.pop(future), future)

    def _start(
        self,
        unit: _Unit,
        pool: ProcessPoolExecutor,
        channel: WorkerChannel,
        running: dict[Future[NodeOutcome], _Unit],
    ) -> None:
        from .flow_runner.error_codes import error_info

        point = self.points[unit.point]
        self._begin(point)
        try:
            future = pool.submit(worker_main, self._task(unit), channel)
        except Exception as error:  # noqa: BLE001 - a broken pool is the failure of the node
            self._failed(unit, error_info(error, node=unit.node.name))
            return
        # The node is in `running` before its start is told: a callback that raises waits for it.
        running[future] = unit
        self._emit(point, "node_started", unit.node.name)

    def _collect(self, unit: _Unit, future: Future[NodeOutcome]) -> None:
        from .flow_runner.error_codes import error_info

        try:
            outcome = future.result()
        except Exception as error:  # noqa: BLE001 - a worker that died is the failure of the node
            self._failed(unit, error_info(error, node=unit.node.name))
            return
        node = unit.node
        result = replace(outcome.result, run_dir=node.run_dir, flow=node.flow)
        problem = result.error
        if problem is not None and problem.code == "stale_plan":
            raise StalePlanError(problem.message, code=problem.code, difference=problem.message)
        self._ran(unit, replace(outcome, result=result))

    def _generate(self) -> None:
        """Settle the generation of every point once, before the first node, as a launch does.
        The nodes and the deliveries read what it leaves. A point whose generator failed ends as a
        launch does that the generator failed."""
        from .flow_runner.error_codes import GENERATOR_FAILED

        try:
            errors = generate_points(
                self.specs,
                replace(self.workspace._options(), confirm=None),
                LaunchOptions(rebuild_all=self.options.rebuild_all, clean=self.options.clean),
                self.snapshot,
                self.canceled,
            )
        except Canceled:
            return  # the token says so, and no node starts
        for point, (error, generation) in zip(self.points, errors):
            point.generation = generation
            if error is not None:
                point.launch_error = ErrorInfo(GENERATOR_FAILED, str(error), None, None, None)

    def run(self) -> SweepResult:
        # As in a launch, the start is told before the generators run, while no lock is held: a
        # callback that cancels or raises stops the sweep before any generator.
        for point in self.points:
            self._begin(point)
        if not self.canceled():
            self._generate()
        if self.max_workers > 1:
            self._run_in_workers()
        else:
            for unit in self.order:
                if self.canceled():
                    break
                if not self._settle(unit):
                    self._execute(unit)
        for point in self.points:
            self._finish(point)
        stopped = any(
            point.finished is not None and point.finished.outcome is Outcome.CANCELED
            for point in self.points
        )
        self._clean_up()
        results = [p.finished for p in self.points]
        assert all(result is not None for result in results)
        return SweepResult(
            tuple(
                SweepPoint(point.index, self.specs[point.index], point.finished)  # type: ignore[arg-type]
                for point in self.points
            ),
            stopped,
        )

    # ------------------------------------------------------------------------------- clean-up

    def _clean_up(self) -> None:
        """Clean up each node that the sweep ran, now that every point has finished.

        A node is cleaned under its own exclusive lock, taken again, and only when its
        `results.json` still holds the run that the sweep made. Another run, or none, is left as
        it is."""
        if not (self.options.post_cleanup or self.options.post_cleanup_purge):
            return
        policy = RunDirPolicy(False, False, True, self.options.post_cleanup_purge)
        run_root = self.workspace.run_root
        for unit in self.order:
            result = unit.result
            if (
                result is None
                or result.disposition is not Disposition.RAN
                or result.outcome not in (Outcome.PASSED, Outcome.REJECTED)
                or unit.run_id is None
            ):
                continue
            directory = result.run_dir
            try:
                with acquire_all(
                    [LockRequest("run_dir", directory, True)],
                    run_root=run_root,
                    canceled=never_canceled,
                ):
                    try:
                        record = read_record(directory, run_root=run_root)
                    except RecordError as problem:
                        log.warning(
                            "Not cleaning up %s, which has no record now: %s", directory, problem
                        )
                        continue
                    if record.run_id != unit.run_id:
                        log.warning(
                            "Not cleaning up %s: another run replaced the run of the sweep",
                            directory,
                        )
                        continue
                    launch_run.clean_up_run(
                        directory,
                        RunDirectory.claimed(directory, run_root),
                        result.flow,
                        results_of(record),
                        directory / "settings.json",
                        directory / "results.json",
                        run_root,
                        policy,
                    )
            except Exception as error:  # noqa: BLE001 - every node gets its turn
                log.error("Cleaning up %s failed: %s", directory, error, exc_info=True)


def _refuse(code: str, message: str, points: Iterable[int]) -> None:
    raise SpecError(message, code=code, points=sorted(set(points)))


def _listed(points: Sequence[int]) -> str:
    """Return the points as text: `0 and 1`, `0, 2 and 5`."""
    names = [str(point) for point in points]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _check_points(plans: Sequence[Plan], specs: Sequence[Spec]) -> None:
    """Refuse what no sweep can do, before any node runs: two points that are one run, two
    configurations on one run directory, and two outputs for one destination."""
    roots: dict[tuple[Path, str], list[int]] = {}
    for index, plan in enumerate(plans):
        roots.setdefault((_root_of(specs[index]), plan.nodes[plan.roots[0]].identity), []).append(
            index
        )
    equal = [points for points in roots.values() if len(points) > 1]
    if equal:
        _refuse(
            "equal_specs",
            "these points are the same run: "
            + "; ".join(", ".join(map(str, points)) for points in equal)
            + ". They have equal settings once units are compared. Remove the repeated spec.",
            (i for points in equal for i in points),
        )
    directories: dict[Path, dict[tuple[Any, ...], list[int]]] = {}
    identities: dict[Path, set[str]] = {}
    destinations: dict[Path, dict[tuple[Any, ...], list[int]]] = {}
    for index, plan in enumerate(plans):
        for node in plan.nodes.values():
            key = node_key(index, node, _root_of(specs[index]))
            directories.setdefault(node.run_dir, {}).setdefault(key, []).append(index)
            identities.setdefault(node.run_dir, set()).add(node.identity)
            for delivery in node.deliveries:
                where = Path(os.path.realpath(delivery.destination))
                destinations.setdefault(where, {}).setdefault(key, []).append(index)
    for path, keys in directories.items():
        if len(keys) > 1:
            involved = sorted({i for points in keys.values() for i in points})
            if len(identities[path]) == 1:
                # One configuration of one design name, read from more than one design root.
                _refuse(
                    "colliding_run_dirs",
                    f"points {_listed(involved)} use one run directory, {path}, for designs with "
                    "different sources. Give each design its own name, or sweep them separately.",
                    involved,
                )
            _refuse(
                "colliding_run_dirs",
                f"points with different configurations use one run directory, {path}. "
                "Create the workspace with `hashed_run_dirs=True` to give each configuration "
                "its own directory.",
                involved,
            )
    for path, keys in destinations.items():
        if len(keys) > 1:
            _refuse(
                "shared_destination",
                f"points deliver different files to one destination, {path}. "
                "Give each point its own destination.",
                (i for points in keys.values() for i in points),
            )


def _flow_modules(plan: Plan) -> tuple[str, ...]:
    """Return the names of the modules that define the flows of `plan` and are not part of Xeda."""
    modules = {registered_flows[node.flow][0] for node in plan.nodes.values()}
    return tuple(sorted(name for name in modules if name.partition(".")[0] != "xeda"))


def _check_importable(plans: Sequence[Plan]) -> None:
    """Refuse a flow that a worker process cannot import: one that a script or a notebook defines.
    A worker has no `__main__` of the caller."""
    for plan in plans:
        for node in plan.nodes.values():
            if registered_flows[node.flow][0] == "__main__":
                _refuse(
                    "flow_not_importable",
                    f"the flow `{node.flow}` is defined in `__main__`, so a worker process cannot "
                    "import it. Move it into a module, or sweep with max_workers=1.",
                    (
                        index
                        for index, other in enumerate(plans)
                        if any(n.flow == node.flow for n in other.nodes.values())
                    ),
                )


def _check_deliveries(plans: Sequence[Plan]) -> None:
    """Refuse a delivery that would replace a file that Xeda did not deliver. A sweep asks no
    question, so the answer is no."""
    for index, plan in enumerate(plans):
        for node in plan.nodes.values():
            for delivery in node.deliveries:
                if delivery.replaces:
                    raise SpecError(
                        f"point {index} would replace {delivery.destination}, a file that Xeda "
                        "did not deliver. A sweep never replaces such a file: remove it, or "
                        "name another path.",
                        code="delivery_refused",
                        points=(index,),
                    )


def _unplanned(specs: Sequence[Spec], on_event: Callable[[Event], None] | None) -> SweepResult:
    """Return the result of a sweep that was canceled while it planned: every point ended as a
    launch does that was canceled before it had a plan, with no node."""
    points = []
    for index, spec in enumerate(specs):
        launch_id = os.urandom(16).hex()
        result = Result(launch_id, spec, (), Outcome.CANCELED, CANCELED_ERROR, {})
        for kind, outcome in (("launch_started", None), ("launch_finished", Outcome.CANCELED)):
            if on_event is not None:
                on_event(Event(kind, launch_id, None, outcome, None, datetime.now(timezone.utc)))
        points.append(SweepPoint(index, spec, result))
    return SweepResult(tuple(points), True)


def run_sweep(
    workspace: Workspace,
    specs: Iterable[Spec],
    *,
    max_workers: int,
    options: LaunchOptions,
    cancel: CancelToken | None,
    on_event: Callable[[Event], None] | None,
) -> SweepResult:
    """Plan every spec, refuse what no sweep can do, and execute the nodes once.

    See `Workspace.sweep`."""
    given = list(specs)
    private = workspace.local_settings.env if workspace.local_settings is not None else {}
    snapshot = EnvironmentSnapshot.take(private)
    plans: list[Plan] = []
    try:
        # Planning waits for the lock of a Git dependency and runs `git`: it observes the token.
        with observing(lambda: cancel is not None and cancel.canceled):
            for index, spec in enumerate(given):
                try:
                    plans.append(
                        workspace.plan(
                            spec,
                            rebuild_all=options.rebuild_all,
                            clean=options.clean,
                            post_cleanup=options.post_cleanup,
                            post_cleanup_purge=options.post_cleanup_purge,
                        )
                    )
                except SpecError as error:
                    error.points = (index,)
                    raise
    except Exception as error:
        if not isinstance(error, Canceled) and not isinstance(error.__cause__, Canceled):
            raise
        return _unplanned(given, on_event)
    _check_deliveries(plans)
    _check_points(plans, given)
    if max_workers > 1:
        _check_importable(plans)
    sweep = _Sweep(
        workspace,
        plans,
        given,
        LaunchOptions(clean=options.clean),
        options,
        cancel,
        on_event,
        snapshot,
        max_workers,
    )
    return sweep.run()
