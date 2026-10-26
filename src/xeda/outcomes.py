"""What a launch reports: an outcome for every planned node, and the verdict of the launch.

`Outcome` and `Disposition` are closed sets of values. A `NodeResult` tells what happened to one
node of the plan. A `Result` holds the `NodeResult` of every planned node, in plan order, and the
verdict of the whole launch: its aggregate outcome and the error that caused it (`aggregate`).

The module imports no flow module when it loads. `Result` and its parts are immutable and can be
pickled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import RecordError, RunFailedError
from .frozen import FrozenDict

if TYPE_CHECKING:
    from .records import ErrorInfo, RunRecord
    from .spec import Spec

__all__ = [
    "DeliveryRecord",
    "Disposition",
    "NodeResult",
    "Outcome",
    "Result",
    "aggregate",
]


class Outcome(StrEnum):
    """How a node, or a whole launch, ended. The set is closed.

    `str()` of a member is its text. A record holds only `passed`, `rejected`, `failed` or
    `canceled`: a node that did not run (`blocked`, `skipped`, `detached`) has no record."""

    PASSED = "passed"
    REJECTED = "rejected"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELED = "canceled"
    DETACHED = "detached"


class Disposition(StrEnum):
    """What this launch did with a node. The set is closed.

    `ran`: the launch executed it. `reused`: an earlier run was up to date. `shared`: a sweep
    executed it once for several points. `not_run`: it did not start."""

    RAN = "ran"
    SHARED = "shared"
    REUSED = "reused"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class DeliveryRecord:
    """One delivery of a node: where the file was to go, and whether it got there.

    `key` names the setting or option that asked for it. `delivered` is false when the delivery
    was refused or failed, or when the launch did not pass."""

    key: str
    destination: Path
    delivered: bool


@dataclass(frozen=True)
class NodeResult:
    """What happened to one planned node.

    `reason` says why the node ran, was reused, was blocked or was not reached. `blocked_by` names
    the producer whose outcome blocked the node. `elapsed` is the time in seconds that this launch
    spent on the node: it is 0.0 unless the node `ran`. `record` is the record of the run, or
    `None` when the node did not run or when its record could not be stored. A `skipped` node has
    no `error`. `run_dir` is the run directory of the node."""

    name: str
    outcome: Outcome
    disposition: Disposition
    reason: str
    blocked_by: str | None
    elapsed: float
    record: RunRecord | None
    error: ErrorInfo | None
    deliveries: tuple[DeliveryRecord, ...]
    run_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", Outcome(self.outcome))
        object.__setattr__(self, "disposition", Disposition(self.disposition))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))


#: The order of the verdicts of the roots: the first one decides.
_WORST_FIRST = (Outcome.FAILED, Outcome.REJECTED, Outcome.SKIPPED, Outcome.PASSED)


def _cause(nodes: Mapping[str, NodeResult], root: str) -> NodeResult:
    """Return the node that decides the verdict of `root`.

    That is the root itself, or, for a blocked root, the node that failed or was rejected: follow
    `blocked_by` down."""
    node = nodes[root]
    followed = {node.name}
    while node.outcome is Outcome.BLOCKED:
        blocker = node.blocked_by
        if blocker is None or blocker not in nodes or blocker in followed:
            raise ValueError(f"the blocked node `{node.name}` has no node that blocks it")
        followed.add(blocker)
        node = nodes[blocker]
    return node


def aggregate(
    nodes: Mapping[str, NodeResult], roots: Sequence[str], launch_error: ErrorInfo | None
) -> tuple[Outcome, ErrorInfo | None]:
    """Return the outcome of a launch and the error that caused it.

    The aggregate is the outcome of the cause of the verdict. The rules apply in this order.

    1. A detached node: `detached`.
    2. A canceled node, or the launch-level error `canceled`: `canceled`.
    3. A launch-level error (a failed generation, delivery or fetch): `failed`.
    4. Otherwise each root has a cause: the root itself, or, for a blocked root, the node that
       failed or was rejected, found by following `blocked_by`. The aggregate is the worst
       outcome of the causes, in the order `failed`, `rejected`, `skipped`, `passed`. The first
       root wins a tie.

    The error is the launch-level error if there is one, else the error of the node that decides
    the aggregate. A `passed` or `skipped` aggregate has no error."""
    detached = next((n for n in nodes.values() if n.outcome is Outcome.DETACHED), None)
    if detached is not None:
        return Outcome.DETACHED, launch_error or detached.error
    canceled = next((n for n in nodes.values() if n.outcome is Outcome.CANCELED), None)
    if canceled is not None or (launch_error is not None and launch_error.code == "canceled"):
        return Outcome.CANCELED, launch_error or (canceled.error if canceled else None)
    if launch_error is not None:
        return Outcome.FAILED, launch_error
    causes = [_cause(nodes, root) for root in roots]
    for outcome in _WORST_FIRST:
        decisive = next((c for c in causes if c.outcome is outcome), None)
        if decisive is not None:
            return outcome, decisive.error
    raise ValueError(f"a root ended with an outcome that decides nothing: {causes!r}")


@dataclass(frozen=True)
class Result:
    """What one launch did: the verdict, and an outcome for every planned node.

    `outcome` is the aggregate and `error` is the cause (`aggregate`). `nodes` holds every
    planned node, in plan order. `roots` are the nodes that the request asked for."""

    launch_id: str
    spec: Spec
    roots: tuple[str, ...]
    outcome: Outcome
    error: ErrorInfo | None
    nodes: Mapping[str, NodeResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(self.roots))
        object.__setattr__(self, "outcome", Outcome(self.outcome))
        object.__setattr__(self, "nodes", FrozenDict(self.nodes))

    @property
    def record(self) -> RunRecord:
        """The record of the single root. Raise `RecordError` (`missing_record`) if it has none.

        A root that was blocked, skipped or detached, or whose record could not be stored, has
        none. To test for one without an exception, ask `result.nodes[root].record is not None`."""
        node = self.nodes[self.roots[0]]
        if node.record is None:
            raise RecordError(
                f"`{node.name}` has no record: it ended {node.outcome}",
                code="missing_record",
                path=node.run_dir / "results.json",
            )
        return node.record

    @property
    def passed(self) -> bool:
        """Whether the launch passed."""
        return self.outcome is Outcome.PASSED

    def require_passed(self) -> Result:
        """Return the result if the launch passed. Raise `RunFailedError` if it did not."""
        if not self.passed:
            cause = f": {self.error.message}" if self.error is not None else ""
            raise RunFailedError(
                f"the launch ended {self.outcome}{cause}", code="run_failed", result=self
            )
        return self
