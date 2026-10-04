"""Reserved input wiring, captured per origin before ordinary settings composition.

These layers describe requests, not an executable graph. The resolver calls ``node_bindings``
for each node it reaches, before choosing sources or default producers; target-selected type
matching and edge discovery are the resolver's.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..flow import Flow, FlowSettingsException
from ..flow.io import declared_inputs, declared_outputs
from ..utils import semantic_hash
from .chains import FlowRequest, match_required_inputs
from .settings_layers import merge_flow_sections, registered_flow


@dataclass(frozen=True)
class NodeKey:
    """The identity of one node of a request: a canonical flow name and an optional instance.

    ``instance`` tells apart several nodes of one flow. Chains, design files and the command
    line each name a flow once, so they all produce the default ``None`` instance; a key with
    an instance only matches entries made for that same instance.
    """

    flow: str
    instance: Hashable | None = None

    @property
    def label(self) -> str:
        """The flow name, with the instance when there is one (for messages)."""
        return self.flow if self.instance is None else f"{self.flow}[{self.instance}]"


@dataclass(frozen=True)
class ProducerRef:
    """A canonical producer node and an exact, optional output key."""

    node: NodeKey
    output: str | None = None


@dataclass(frozen=True)
class BindingEntry:
    """One unvalidated contribution; lower overridden values need not be valid references."""

    node: NodeKey
    name: str
    value: Any
    is_list: bool
    location: str


@dataclass(frozen=True)
class BindingLayer:
    """One origin's reserved keys and explicitly supplied ordinary settings.

    ``kind`` is the origin's rank: a ``"file"`` (design or project), the command line
    (``"cli"``) or the API (``"api"``). A chain adjacency is command-line data: it overrides a
    file's binding of the same input and collides with a command-line or API one (PC1).
    """

    location: str
    entries: tuple[BindingEntry, ...]
    settings: Mapping[str, Any]
    invalid_inputs: tuple[str, ...] = ()
    kind: str = "file"


@dataclass(frozen=True)
class InputBinding:
    """The winning ordered references for one input, with its explanatory location.

    ``origin`` is ``"chain"`` or the winning layer's kind; ``overridden`` lists the locations
    of file bindings a chain adjacency replaced. Neither is part of a run's identity.
    """

    references: tuple[ProducerRef, ...]
    location: str
    origin: str = "file"
    overridden: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)
class _FrozenMapping(Mapping[Any, Any]):
    """An immutable, pickleable request snapshot for existing worker transport."""

    _items: tuple[tuple[Any, Any], ...]

    def __getitem__(self, key: Any) -> Any:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[Any]:
        return (name for name, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenMapping(tuple((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def split_bindings(
    sections: Mapping[str, Any], *, location: str, kind: str = "file"
) -> tuple[dict[str, Any], BindingLayer]:
    """Copy/canonicalize an origin, removing ``inputs`` before Settings sees it.

    Unknown plugin sections remain opaque until reached. Normalize flow aliases per origin,
    retaining the existing same-origin duplicate checks and ordinary settings spellings.
    """
    # Reuse the resolver's contribution recovery: exclude model defaults but retain edits
    # made to a default-created nested Settings instance, even under an unset parent field.
    from .resolver import _explicit

    normalized = merge_flow_sections(
        {
            name: _explicit(values) if isinstance(values, Flow.Settings) else values
            for name, values in sections.items()
        },
        flow_class_for=registered_flow,
    )
    entries = []
    invalid_inputs = []
    for node, values in normalized.items():
        if "inputs" not in values:
            continue
        inputs = values.pop("inputs")
        if not isinstance(inputs, Mapping):
            invalid_inputs.append(node)
            continue
        for name, value in inputs.items():
            entries.append(
                BindingEntry(
                    NodeKey(node),
                    name,
                    _freeze(value),
                    isinstance(value, list),
                    f"{location}: flows.{node}.inputs.{name}",
                )
            )
    return normalized, BindingLayer(
        location, tuple(entries), _freeze(_explicit(normalized)), tuple(invalid_inputs), kind
    )


LOCAL_REQUESTS_ONLY = (
    "Flow chains and input bindings are local `xeda run` requests: --remote and dse do not "
    "take them."
)

_REFERENCE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\Z")


def _reference(value: Any, location: str) -> ProducerRef:
    from .default_runner import FlowNotFoundError, get_flow_class

    match = _REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise FlowSettingsException(
            f"{location}: a binding reference must be a producer or producer.output name; "
            "external files belong in typed rtl.sources."
        )
    name, output = match.groups()
    try:
        producer = get_flow_class(name)
    except FlowNotFoundError as error:
        raise FlowSettingsException(f"{location}: unknown producer {name!r}. {error}") from error
    if output is not None and output not in declared_outputs(producer):
        raise FlowSettingsException(
            f"{location}: producer {producer.name!r} has no output {output!r}."
        )
    return ProducerRef(NodeKey(producer.name), output)


def chain_bindings(request: FlowRequest | None) -> dict[tuple[NodeKey, str], InputBinding]:
    """The input each chain adjacency binds: ``(consumer, input)`` -> its one reference."""
    bound: dict[tuple[NodeKey, str], InputBinding] = {}
    if request is None:
        return bound
    for position, (producer, consumer) in enumerate(zip(request.elements, request.elements[1:]), 1):
        location = (
            f"the chain position {position} ({producer.node}) -> {position + 1} ({consumer.node})"
        )
        for name, output in match_required_inputs(
            producer.flow_class, consumer.flow_class, output=producer.output
        ):
            bound[NodeKey(consumer.node), name] = InputBinding(
                (ProducerRef(NodeKey(producer.node), output),), location, "chain"
            )
    return bound


def check_chain_collisions(layers: Sequence[BindingLayer], request: FlowRequest | None) -> None:
    """PC1: a chain adjacency and a command-line or API binding of the same input are always
    an error, even when equal, before precedence or value validation. A design or project
    file's binding is not a collision: the chain, command-line data, overrides it."""
    chain = chain_bindings(request)
    for layer in layers:
        if layer.kind == "file":
            continue
        for entry in layer.entries:
            adjacency = chain.get((entry.node, entry.name))
            if adjacency is not None:
                raise FlowSettingsException(
                    f"{adjacency.location[0].upper()}{adjacency.location[1:]} binds input "
                    f"{entry.name!r}; {entry.location} also binds that input. Give the "
                    "binding in one place, even when equal."
                )


def node_bindings(
    layers: Sequence[BindingLayer],
    key: NodeKey,
    cls: type[Flow],
    *,
    request: FlowRequest | None = None,
) -> dict[str, InputBinding]:
    """The winning binding of each explicitly bound input of one reached node.

    Origins must be supplied in increasing precedence (project, design, CLI, API). Every
    layer's input names are checked; only a winning value is validated as a reference. A chain
    adjacency replaces a file binding of its input and notes that binding's location.
    """
    declarations = declared_inputs(cls)
    winners: dict[str, tuple[BindingEntry, str]] = {}
    for layer in layers:
        if key.instance is None and key.flow in layer.invalid_inputs:
            raise FlowSettingsException(
                f"{layer.location}: flows.{key.flow}.inputs must be a mapping."
            )
        for entry in layer.entries:
            if entry.node != key:
                continue
            if entry.name not in declarations:
                raise FlowSettingsException(
                    f"{entry.location}: unknown input {entry.name!r} of {entry.node.label!r}."
                )
            winners[entry.name] = (entry, layer.kind)
    selected: dict[str, InputBinding] = {}
    for name, (entry, kind) in winners.items():
        declaration = declarations[name]
        if entry.is_list:
            if declaration.cardinality != "many":
                raise FlowSettingsException(f"{entry.location}: takes one reference, not a list.")
            if not entry.value and declaration.required:
                raise FlowSettingsException(
                    f"{entry.location}: a required binding cannot be empty."
                )
            values = entry.value
        else:
            values = (entry.value,)
        selected[name] = InputBinding(
            tuple(_reference(value, entry.location) for value in values), entry.location, kind
        )
    for (node, name), adjacency in chain_bindings(request).items():
        if node != key:
            continue
        overridden = tuple(
            entry.location
            for layer in layers
            if layer.kind == "file"
            for entry in layer.entries
            if entry.node == key and entry.name == name
        )
        selected[name] = InputBinding(adjacency.references, adjacency.location, "chain", overridden)
    return selected


def effective_bindings(
    layers: Sequence[BindingLayer],
    reached: Sequence[tuple[NodeKey, type[Flow]]],
    *,
    request: FlowRequest | None = None,
) -> Mapping[NodeKey, Mapping[str, InputBinding]]:
    """`node_bindings` of every reached node that has any, after the PC1 collision check.

    Nodes are told apart by ``NodeKey``, never by flow name. This does not select edges,
    append design sources, compose producer settings or validate target-dependent types.
    """
    check_chain_collisions(layers, request)
    classes: dict[NodeKey, type[Flow]] = {}
    for key, cls in reached:
        if key in classes:
            raise ValueError(f"The node {key.label!r} is reached twice.")
        classes[key] = cls
    selected = {
        key: bindings
        for key, cls in classes.items()
        if (bindings := node_bindings(layers, key, cls, request=request))
    }
    return _freeze(selected)


def input_origins(
    inputs: Sequence[Any], identity_of: Callable[[str], str]
) -> tuple[tuple[str, Any], ...]:
    """Where each resolved input of a node comes from, in declaration order: ``"source"``, or
    its ordered ``(producer identity, output key)`` references (none for an absent input).

    `inputs` are a plan node's `ResolvedInput`s; `identity_of` gives the identity of the plan
    node a reference names, so producers are identified before their consumers. Reference
    order counts. Where a binding was written (chain, file, command line, API) does not.
    """
    return tuple(
        (
            resolved.name,
            (
                "source"
                if resolved.origin == "source"
                else tuple((identity_of(ref.node), ref.output) for ref in resolved.references)
            ),
        )
        for resolved in inputs
    )


def node_identity(settings_hash: str, origins: Sequence[Any] = ()) -> str:
    """D-9, the one identity rule: a node is its settings plus its ordered resolved input
    origins (`input_origins`), for default and explicit edges alike. `settings_hash` is the
    settings-only `flowrun_hash`; a flow that declares no inputs has no origins.

    The resolver's freeze, the launcher's plan validation and run identity, run-directory
    claims, `results.json` and the trace all take a run's hash from here.
    """
    return semantic_hash({"settings": settings_hash, "inputs": tuple(origins)})


def default_nodes(classes: Sequence[type[Flow]]) -> list[tuple[NodeKey, type[Flow]]]:
    """The reached nodes of a request that names each flow once (instance ``None``)."""
    return [(NodeKey(cls.name), cls) for cls in classes]


def require_no_bindings(layers: Sequence[BindingLayer]) -> None:
    """Refuse any binding where requests are not resolved locally (``--remote``)."""
    if any(layer.entries or layer.invalid_inputs for layer in layers):
        raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
