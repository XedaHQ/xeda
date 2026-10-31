"""A sweep: many specs, each node executed once, and a table of what came out.

`Workspace.sweep` plans every point first. The nodes of all points form one graph, keyed by the
identity of a node: a node that several points have is one node, and it executes once. The
scheduler starts a node when every producer of it has completed. A producer that did not pass
blocks its consumers in every point. Each node executes through `execute_node`, in this process
(`run_node`), as a launch of that node alone whose producers are read from their records.

A point gets a `Result` as a launch does. A node that another point executed is `shared` there.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cancel import CancelToken, never_canceled
from .errors import RecordError, SpecError, StalePlanError
from .events import Event
from .flow_runner import launch_run
from .flow_runner.environment import EnvironmentSnapshot
from .flow_runner.launch_identity import RunDirPolicy
from .flow_runner.launch_node import NodeLaunch, execute_node
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
from .records import read_record
from .run_dir import RunDirectory
from .spec import Spec

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
    "node_key",
    "run_node",
    "run_sweep",
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
                    f"`{text}` is not `<node>.<metric>` of a node of this sweep. "
                    f"The nodes are: {', '.join(sorted(names))}"
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
            updates = _merged(updates, thaw(update))
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

    `node` is the name of the node in the plan of `spec`. `completed` are its producers. `rebuild`
    applies to the node only, never to a producer. `workspace.confirm` is `None`, since a task
    asks nothing."""

    spec: Spec
    node: str
    workspace: WorkspaceOptions
    launch: LaunchOptions
    snapshot: EnvironmentSnapshot
    completed: tuple[CompletedNode, ...]
    rebuild: bool


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
        run_root_start=options.run_root.parent,
    )
    token = TOOL_OUTPUT.set(options.tool_output)  # type: ignore[arg-type]
    try:
        return runner.run_node(
            task.spec,
            node=task.node,
            completed={done.name: done for done in task.completed},
            local_settings=options.local_settings,
            options=task.launch,
            rebuild=task.rebuild,
            environment=task.snapshot,
            canceled=canceled,
            on_event=emit,
        )
    finally:
        TOOL_OUTPUT.reset(token)


_LAUNCH: NodeLaunch = launch_node_result


def run_node(
    task: NodeTask, *, canceled: Callable[[], bool], emit: Callable[[Event], None]
) -> NodeOutcome:
    """Execute the node of `task` in this process (`launch_node.execute_node`)."""
    return execute_node(task, canceled=canceled, emit=emit, launch=_LAUNCH)


# ------------------------------------------------------------------------------------ the sweep


def node_key(point: int, node: PlanNode) -> tuple[Any, ...]:
    """Return the key of `node`, a node of the plan of the point `point`, in the graph of a sweep.

    Nodes of equal identity are one node, and executes once. A node that acts (it programs a
    device) is never shared: its key holds its point."""
    return (point, node.name) if node.action else ("", node.identity)


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
    ) -> None:
        self.workspace = workspace
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
                key = keys[name] = node_key(point.index, node)
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
        outcome, error = aggregate(nodes, roots, CANCELED_ERROR if stopped else None)
        point.finished = Result(
            launch_id=point.launch_id,
            spec=self.specs[point.index],
            roots=roots,
            outcome=outcome,
            error=error,
            nodes=nodes,
        )
        self._begin(point)
        self._emit(point, "launch_finished", None, outcome)

    # ----------------------------------------------------------------------------- scheduling

    def _complete(self, unit: _Unit, result: NodeResult, run_id: str | None) -> None:
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
            workspace=replace(self.workspace._options(), confirm=None),
            launch=self.launch,
            snapshot=self.snapshot,
            completed=tuple(completed),
            rebuild=self.rebuild,
        )

    def _execute(self, unit: _Unit) -> None:
        from .flow_runner.error_codes import error_info

        point = self.points[unit.point]
        self._begin(point)
        self._emit(point, "node_started", unit.node.name)
        try:
            outcome = run_node(self._task(unit), canceled=self.canceled, emit=lambda event: None)
        except (StalePlanError, KeyboardInterrupt):
            raise
        except Exception as error:  # noqa: BLE001 - the failure of a node is a value
            node = unit.node
            result = NodeResult(
                name=node.name,
                outcome=Outcome.FAILED,
                disposition=Disposition.RAN,
                reason="",
                blocked_by=None,
                elapsed=0.0,
                record=None,
                error=error_info(error, node=node.name),
                deliveries=(),
                run_dir=node.run_dir,
                flow=node.flow,
            )
            self._complete(unit, result, None)
            return
        record = outcome.result.record
        if record is not None and record.identity != unit.node.identity:
            raise StalePlanError(
                f"The plan is stale: {unit.node.name} ran as another configuration than the "
                "sweep planned. Plan again.",
                code="stale_plan",
                difference=f"the identity of {unit.node.name} changed",
            )
        self._complete(unit, outcome.result, outcome.run_id)

    def run(self) -> SweepResult:
        for unit in self.order:
            if self.canceled():
                break
            blocked = self._blocked(unit)
            if blocked is not None:
                self._complete(unit, blocked, None)
            else:
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


def _check_points(plans: Sequence[Plan]) -> None:
    """Refuse what no sweep can do, before any node runs: two points that are one run, two
    configurations on one run directory, and two outputs for one destination."""
    roots: dict[str, list[int]] = {}
    for index, plan in enumerate(plans):
        roots.setdefault(plan.nodes[plan.roots[0]].identity, []).append(index)
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
    destinations: dict[Path, dict[tuple[Any, ...], list[int]]] = {}
    for index, plan in enumerate(plans):
        for node in plan.nodes.values():
            key = node_key(index, node)
            directories.setdefault(node.run_dir, {}).setdefault(key, []).append(index)
            for delivery in node.deliveries:
                where = Path(os.path.realpath(delivery.destination))
                destinations.setdefault(where, {}).setdefault(key, []).append(index)
    for path, keys in directories.items():
        if len(keys) > 1:
            _refuse(
                "colliding_run_dirs",
                f"points with different configurations use one run directory, {path}. "
                "Create the workspace with `hashed_run_dirs=True` to give each configuration "
                "its own directory.",
                (i for points in keys.values() for i in points),
            )
    for path, keys in destinations.items():
        if len(keys) > 1:
            _refuse(
                "shared_destination",
                f"points deliver different files to one destination, {path}. "
                "Give each point its own destination.",
                (i for points in keys.values() for i in points),
            )


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
    _check_points(plans)
    sweep = _Sweep(
        workspace,
        plans,
        given,
        LaunchOptions(clean=options.clean),
        options,
        cancel,
        on_event,
        snapshot,
    )
    return sweep.run()
