"""What planning returns: what a launch of a `Spec` would do, and nothing that it did.

`Workspace.plan` makes a `Plan`. It holds one `PlanNode` for each node of the run, in execution
order with the producers first. Each node says where it would run, what identifies it, whether a
launch would reuse its last run (`state`) and why, which inputs it reads and which files it would
deliver. The values are immutable and can be pickled. `Plan.to_dict` is the document of
`xeda run --dry-run --json`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .frozen import FrozenDict, freeze, thaw

if TYPE_CHECKING:
    from .flow_runner.launch_options import LaunchOptions, WorkspaceOptions
    from .flow_runner.plan_freshness import NodeFreshness
    from .flow_runner.resolver import Plan as ResolvedPlan
    from .flow_runner.resolver import PlanNode as ResolvedNode
    from .flow_runner.resolver import ResolvedInput
    from .spec import Spec

__all__ = [
    "PLAN_DOCUMENT_VERSION",
    "Plan",
    "PlanDelivery",
    "PlanInput",
    "PlanNode",
    "node_document",
    "plan_node",
    "plan_of",
    "spec_document",
]

#: The `schema_version` of the document that `xeda run --dry-run --json` prints.
PLAN_DOCUMENT_VERSION = 1

#: The key of a delivery that `outputs_to` asked for.
OUTPUTS_TO_KEY = "outputs_to"


@dataclass(frozen=True)
class PlanInput:
    """Where one input of a node comes from, and why that producer or source was chosen.

    An input that several producers supply has one `PlanInput` for each of them, with the same
    `name`."""

    name: str
    origin: Literal["source", "producer", "none"]
    producer: str | None
    output: str | None
    sources: tuple[Path, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class PlanDelivery:
    """A file that a launch would deliver, known before any tool runs.

    `key` is the deliverable setting, `outputs_to` or `generator`. `replaces` is true when a file
    is at `destination` that is not an unchanged earlier delivery of Xeda."""

    key: str
    destination: Path
    replaces: bool


@dataclass(frozen=True)
class PlanNode:
    """What a launch would do with one node.

    `state` is `fresh` (a launch would reuse the last run), `rejected` (it would reuse the last
    run, which the checks rejected, and report the rejection again), `stale` (it would run),
    `unknown` (it depends on a producer that would run, or on something that only a process can
    tell) or `blocked` (a producer is rejected or blocked, so it would not run). `reason` says
    why, and it is empty for `fresh`. `identity` is opaque text: compare it for equality only.
    `settings` are the effective settings of the node. `outputs_enabled` names the outputs that
    a consumer switches on."""

    name: str
    flow: str
    run_dir: Path
    identity: str
    state: Literal["fresh", "rejected", "stale", "unknown", "blocked"]
    reason: str
    action: bool
    settings: Mapping[str, Any]
    inputs: tuple[PlanInput, ...]
    outputs_enabled: tuple[str, ...]
    input_types: Mapping[str, tuple[str, ...]]
    output_types: Mapping[str, tuple[str, ...]]
    deliveries: tuple[PlanDelivery, ...]

    def __post_init__(self) -> None:
        for name in ("settings", "input_types", "output_types"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        for name in ("inputs", "outputs_enabled", "deliveries"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class Plan:
    """What a launch of `spec` would do: the nodes, and the options that the plan was made with.

    `roots` names the nodes that the spec asks for. `options` holds the seven launch options by
    name (`outputs_to` is an absolute path or `None`). `nodes` holds every node by name, in
    execution order."""

    spec: Spec
    roots: tuple[str, ...]
    options: Mapping[str, Any]
    nodes: Mapping[str, PlanNode]
    #: The options of the workspace that made the plan. Running the plan compares them.
    _workspace: WorkspaceOptions | None = field(default=None, repr=False, compare=False)
    #: The commit that each Git dependency resolved to, as `(repository, commit)`.
    _dependencies: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(self.roots))
        object.__setattr__(self, "options", FrozenDict(self.options))
        object.__setattr__(self, "nodes", FrozenDict(self.nodes))

    def to_dict(self) -> dict[str, Any]:
        """Return the document that `xeda run --dry-run --json` prints (schema 1).

        It has the keys `schema_version`, `spec`, `roots`, `options` and `nodes`. `nodes` is a
        list in execution order, and each entry has one key for each field of `PlanNode`."""
        options = dict(self.options)
        if options["outputs_to"] is not None:
            options["outputs_to"] = str(options["outputs_to"])
        return {
            "schema_version": PLAN_DOCUMENT_VERSION,
            "spec": spec_document(self.spec),
            "roots": list(self.roots),
            "options": options,
            "nodes": [node_document(node) for node in self.nodes.values()],
        }


def node_document(node: PlanNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "flow": node.flow,
        "run_dir": str(node.run_dir),
        "identity": node.identity,
        "state": node.state,
        "reason": node.reason,
        "action": node.action,
        "settings": thaw(node.settings),
        "inputs": [
            {
                "name": item.name,
                "origin": item.origin,
                "producer": item.producer,
                "output": item.output,
                "sources": [str(path) for path in item.sources],
                "reason": item.reason,
            }
            for item in node.inputs
        ],
        "outputs_enabled": list(node.outputs_enabled),
        "input_types": thaw(node.input_types),
        "output_types": thaw(node.output_types),
        "deliveries": [
            {"key": item.key, "destination": str(item.destination), "replaces": item.replaces}
            for item in node.deliveries
        ],
    }


def spec_document(spec: Spec) -> dict[str, Any]:
    """Return the `spec` of a plan document and of a run document: what the launch is asked to do.

    The keys are `flow`, `design` (the name of the design), `design_file` (the absolute path of
    the file that the design was loaded from, or `None`), `target` and `settings` (the table of
    the spec, each quantity in the unit of its field)."""
    from .outcomes import spec_document as document

    design = spec.design
    return document(
        flow=spec.flow,
        design=design.name,
        # Only the loader sets the file of a design: it is no field.
        design_file=design._source_file,  # pylint: disable=protected-access
        target=design.target,
        settings=spec.settings,
    )


def _input_reason(resolved: ResolvedInput) -> str:
    """Say why the resolver chose the producer or the source of an input."""
    if resolved.binding_origin is not None:
        where = resolved.binding_location or ""
        # A location names its place: "the chain position 1 (a) -> 2 (b)", or a file and a key.
        text = {
            "chain": "bound by the chain: " + where.removeprefix("the chain "),
            "file": f"bound by a saved binding in {where}",
            "cli": "bound by `-s` or `flow_settings`",
            "api": "bound by the API",
        }[resolved.binding_origin]
        if resolved.overridden:
            text += ", overriding " + ", ".join(resolved.overridden)
        return text
    if resolved.reason:
        return resolved.reason
    return {
        "producer": "the default producer of the input",
        "source": "a source of the design",
        "none": "nothing supplies the input",
    }[resolved.origin]


def _inputs_of(node: Any) -> tuple[PlanInput, ...]:
    found: list[PlanInput] = []
    for resolved in node.inputs:
        reason = _input_reason(resolved)
        if resolved.references:
            found += [
                PlanInput(resolved.name, resolved.origin, ref.node, ref.output, (), reason)
                for ref in resolved.references
            ]
        else:
            found.append(
                PlanInput(
                    resolved.name,
                    resolved.origin,
                    resolved.producer,
                    resolved.output,
                    resolved.sources,
                    reason,
                )
            )
    return tuple(found)


def _types_of(node: Any, *, output: bool) -> dict[str, tuple[str, ...]]:
    from .flow.io import declared_inputs, declared_outputs, selected_types

    declarations = declared_outputs(node.flow_class) if output else declared_inputs(node.flow_class)
    settings = node.settings
    return {
        name: tuple(t.name for t in selected_types(node.flow_class, settings, name, output=output))
        for name, declaration in declarations.items()
        if not declaration.is_design
    }


def plan_node(
    node: ResolvedNode, freshness: NodeFreshness, deliveries: Sequence[PlanDelivery]
) -> PlanNode:
    """Return the public `PlanNode` of a node that the launcher resolved."""
    from .utils import plain_json

    return PlanNode(
        name=node.name,
        flow=node.flow_class.name,
        run_dir=node.run_path,
        identity=node.flowrun_hash,
        state=freshness.state,
        reason=freshness.reason,
        action=node.flow_class.action_reason is not None,
        settings=plain_json(node.settings),
        inputs=_inputs_of(node),
        outputs_enabled=node.switched_on,
        input_types=_types_of(node, output=False),
        output_types=_types_of(node, output=True),
        deliveries=tuple(deliveries),
    )


def plan_of(
    internal: ResolvedPlan,
    states: Mapping[str, NodeFreshness],
    deliveries: Mapping[str, Sequence[PlanDelivery]],
    *,
    spec: Spec,
    options: LaunchOptions,
    workspace: WorkspaceOptions | None,
    dependencies: Sequence[tuple[str, str]] = (),
) -> Plan:
    """Return the public `Plan` of the plan that the launcher resolved.

    `states` holds what a launch would do with each node, and `deliveries` the files that each
    node would deliver, both by node name. The plan keeps `workspace` in a private field, which
    `to_dict` leaves out: running the plan compares it. So does it the `dependencies`, the commit
    of each Git dependency."""
    nodes = {
        node.name: plan_node(node, states[node.name], deliveries.get(node.name, ()))
        for node in internal.nodes
    }
    return Plan(
        spec=spec,
        roots=(internal.nodes[-1].name,),
        options=options.as_dict(),
        nodes=nodes,
        _workspace=workspace,
        _dependencies=tuple(dependencies),
    )


def delivery_key(key: str) -> str:
    """Return the key of a planned delivery: the setting that asked for it, or `outputs_to`."""
    from .deliver import OUTPUTS_TO

    return OUTPUTS_TO_KEY if key == OUTPUTS_TO else key


def planned_destination(path: Path) -> Path:
    """Return the absolute destination of a planned delivery."""
    return Path(os.path.abspath(path))


def dependency_commits(design: Any) -> tuple[tuple[str, str], ...]:
    """Return `(repository, commit)` for each Git dependency that planning resolved in `design`."""
    return tuple(
        (d.repository, d.commit) for d in design.resolved_dependencies if d.commit is not None
    )
