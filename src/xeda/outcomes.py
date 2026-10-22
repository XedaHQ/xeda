"""What a launch reports: an outcome for every planned node, and the verdict of the launch.

`Outcome` and `Disposition` are closed sets of values. A `NodeResult` tells what happened to one
node of the plan. A `Result` holds the `NodeResult` of every planned node, in plan order, and the
verdict of the whole launch: its aggregate outcome and the error that caused it (`aggregate`).

`Result.to_dict` is the document that `xeda run --json` prints, and `result_from_dict` reads such
a document again (the remote client sends it between hosts).

The module imports no flow module when it loads. `Result` and its parts are immutable and can be
pickled.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import RecordError, RunFailedError
from .frozen import FrozenDict, thaw

if TYPE_CHECKING:
    from .records import ErrorInfo, RunRecord
    from .spec import Spec

__all__ = [
    "DeliveryRecord",
    "Disposition",
    "NodeResult",
    "Outcome",
    "RUN_DOCUMENT_VERSION",
    "Result",
    "aggregate",
    "node_document",
    "result_from_dict",
    "run_document",
    "spec_document",
    "spec_document_of",
]

#: The `schema_version` of the document that `xeda run --json` prints.
RUN_DOCUMENT_VERSION = 1


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
    no `error`. `run_dir` is the run directory of the node, and `flow` is the name of its flow."""

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
    flow: str

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

    def to_dict(self) -> dict[str, Any]:
        """Return the document that `xeda run --json` prints (schema 1).

        It has the keys `schema_version`, `outcome`, `error`, `launch_id`, `spec`, `roots` and
        `nodes`. The `nodes` are in plan order. The document of a node has no `error`: the error
        of a node that ran is in its `record`, and the error that decided the launch is the
        top-level `error`."""
        return run_document(
            outcome=self.outcome,
            error=self.error,
            launch_id=self.launch_id,
            spec=spec_document_of(self.spec),
            roots=self.roots,
            nodes=[node_document(node) for node in self.nodes.values()],
        )


def _plain(value: Any) -> Any:
    """Return `value` as plain JSON data: tables, lists, text, numbers, booleans and null."""
    from .utils import json_encodable

    return json.loads(json.dumps(thaw(value), default=json_encodable))


def spec_document(
    *,
    flow: str,
    design: str | None,
    design_file: Path | str | None,
    target: str | None,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the `spec` of a run document: what the launch was asked to do."""
    return {
        "flow": flow,
        "design": design,
        "design_file": None if design_file is None else os.path.abspath(design_file),
        "target": target,
        "settings": _plain(settings),
    }


def spec_document_of(spec: Spec) -> dict[str, Any]:
    """Return the `spec` of a run document for the `Spec` `spec`."""
    design = spec.design
    return spec_document(
        flow=spec.flow,
        design=design.name,
        # Only the loader sets the file of a design: it is no field.
        design_file=design._source_file,
        target=design.target,
        settings=spec.settings,
    )


def run_document(
    *,
    outcome: Outcome | None,
    error: ErrorInfo | None,
    launch_id: str | None,
    spec: Mapping[str, Any] | None,
    roots: Sequence[str],
    nodes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a run document (schema 1) from its parts.

    `Result.to_dict` builds its document with this function. So does the report of a request that
    never started a launch: it has no `outcome`, no `launch_id` and no `nodes`, and its `error`
    says why."""
    return {
        "schema_version": RUN_DOCUMENT_VERSION,
        "outcome": None if outcome is None else Outcome(outcome).value,
        "error": None if error is None else error.as_json_value(),
        "launch_id": launch_id,
        "spec": None if spec is None else dict(spec),
        "roots": list(roots),
        "nodes": [dict(node) for node in nodes],
    }


def node_document(node: NodeResult) -> dict[str, Any]:
    """Return the entry of a node in a run document."""
    from .records import encode_record

    return {
        "name": node.name,
        "flow": node.flow,
        "outcome": node.outcome.value,
        "disposition": node.disposition.value,
        "reason": node.reason,
        "blocked_by": node.blocked_by,
        "elapsed": node.elapsed,
        "run_dir": str(node.run_dir),
        "deliveries": [
            {"key": d.key, "destination": str(d.destination), "delivered": d.delivered}
            for d in node.deliveries
        ],
        "record": None if node.record is None else encode_record(node.record),
    }


def _field(document: Mapping[str, Any], key: str, kinds: type | tuple[type, ...], what: str) -> Any:
    """Return `document[key]`, or raise `ValueError` if it is missing or not of a type in `kinds`.
    A boolean is never a number."""
    value = document.get(key)
    if isinstance(value, bool) and bool not in (kinds if isinstance(kinds, tuple) else (kinds,)):
        value = None
    if not isinstance(value, kinds):
        raise ValueError(f"{what} has no valid `{key}`: {value!r}")
    return value


def _run_root_of(run_dir: Path, target: str | None) -> Path:
    """Return the run root of the run directory `run_dir`: the layout is
    `<run root>/<design>[/<target>]/<flow>[_<hash>]`."""
    depth = 2 if target is None else 3
    if len(run_dir.parents) < depth:
        raise ValueError(f"the run directory {run_dir} is not in a run root")
    return run_dir.parents[depth - 1]


def _node_of(document: Any) -> NodeResult:
    from .records import PRODUCER_FAILED, ErrorInfo, decode_record

    if not isinstance(document, Mapping):
        raise ValueError(f"a node of the document is not an object: {document!r}")
    name = _field(document, "name", str, "a node")
    what = f"the node `{name}`"
    run_dir = Path(_field(document, "run_dir", str, what))
    blocked_by = document.get("blocked_by")
    if blocked_by is not None and not isinstance(blocked_by, str):
        raise ValueError(f"{what} has no valid `blocked_by`: {blocked_by!r}")
    record = None
    if document.get("record") is not None:
        raw = _field(document, "record", Mapping, what)
        try:
            record = decode_record(
                raw, run_dir=run_dir, run_root=_run_root_of(run_dir, raw.get("target"))
            )
        except RecordError as problem:
            raise ValueError(f"{what} has a bad record: {problem}") from None
    outcome = Outcome(_field(document, "outcome", str, what))
    error = None if record is None else record.error
    if error is None and outcome is Outcome.BLOCKED and blocked_by is not None:
        error = ErrorInfo(
            code=PRODUCER_FAILED,
            message=f"producer {blocked_by} did not pass",
            node=name,
            exit_status=None,
            log=None,
        )
    deliveries = []
    for item in _field(document, "deliveries", list, what):
        if not isinstance(item, Mapping):
            raise ValueError(f"{what} has a bad delivery: {item!r}")
        deliveries.append(
            DeliveryRecord(
                key=_field(item, "key", str, "a delivery"),
                destination=Path(_field(item, "destination", str, "a delivery")),
                delivered=_field(item, "delivered", bool, "a delivery"),
            )
        )
    return NodeResult(
        name=name,
        outcome=outcome,
        disposition=Disposition(_field(document, "disposition", str, what)),
        reason=_field(document, "reason", str, what),
        blocked_by=blocked_by,
        elapsed=float(_field(document, "elapsed", (int, float), what)),
        record=record,
        error=error,
        deliveries=tuple(deliveries),
        run_dir=run_dir,
        flow=_field(document, "flow", str, what),
    )


def _spec_of(document: Mapping[str, Any]) -> Spec:
    """Return the `Spec` that a `spec` of a run document stands for.

    The document holds the name of the design, not the design. The `Spec` gets an empty design
    of that name, target and file."""
    from .design import Design
    from .spec import Spec

    design_file = document.get("design_file")
    if design_file is not None and not isinstance(design_file, str):
        raise ValueError(f"the spec has no valid `design_file`: {design_file!r}")
    target = document.get("target")
    if target is not None and not isinstance(target, str):
        raise ValueError(f"the spec has no valid `target`: {target!r}")
    design = Design(
        name=_field(document, "design", str, "the spec"),
        target=target,
        design_root=Path(design_file).parent if design_file else None,
    )
    design._source_file = None if design_file is None else Path(design_file)
    return Spec(
        _field(document, "flow", str, "the spec"),
        design,
        settings=_field(document, "settings", Mapping, "the spec"),
    )


def result_from_dict(document: Mapping[str, Any]) -> Result:
    """Return the `Result` that the run document `document` (`Result.to_dict`) describes.

    A key that this version does not know is ignored. Raise `ValueError` if the document is of a
    request that never started a launch (its `outcome` is null), has another schema version, or
    is malformed.

    The document keeps less than the `Result` holds. The `spec` of the result has an empty design
    of the same name, target and file, and the settings of the document. A node has no `error` of
    its own in the document: the node takes the error of its record, and a blocked node gets
    the error `producer_failed`."""
    from .records import ErrorInfo

    if not isinstance(document, Mapping):
        raise ValueError("a run document is an object")
    version = document.get("schema_version")
    if version != RUN_DOCUMENT_VERSION:
        raise ValueError(f"the run document has the schema version {version!r}, not 1")
    if document.get("outcome") is None:
        raise ValueError("the document is of a request that never started a launch")
    error = document.get("error")
    nodes = [_node_of(item) for item in _field(document, "nodes", list, "the document")]
    launch_id = _field(document, "launch_id", str, "the document")
    roots = _field(document, "roots", list, "the document")
    if not all(isinstance(root, str) for root in roots):
        raise ValueError(f"the roots of the document are not all text: {roots!r}")
    try:
        parsed_error = None if error is None else ErrorInfo.from_json(error)
    except ValueError as problem:
        raise ValueError(f"the document has a bad error: {problem}") from None
    return Result(
        launch_id=launch_id,
        spec=_spec_of(_field(document, "spec", Mapping, "the document")),
        roots=tuple(roots),
        outcome=Outcome(_field(document, "outcome", str, "the document")),
        error=parsed_error,
        nodes={node.name: node for node in nodes},
    )
