"""Reserved input wiring, captured per origin before ordinary settings composition.

These layers describe requests, not an executable graph. The resolver must call
``effective_bindings`` for each reached consumer before choosing sources or default producers;
target-selected type matching and edge discovery remain the resolver's responsibility.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..flow import Flow, FlowSettingsException
from ..flow.io import declared_inputs, declared_outputs
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
    """One origin's reserved keys and explicitly supplied ordinary settings."""

    location: str
    entries: tuple[BindingEntry, ...]
    settings: Mapping[str, Any]
    invalid_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputBinding:
    """The winning ordered references for one input, with its explanatory location."""

    references: tuple[ProducerRef, ...]
    location: str


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
    sections: Mapping[str, Any], *, location: str
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
        location, tuple(entries), _freeze(_explicit(normalized)), tuple(invalid_inputs)
    )


PENDING_INTEGRATION = "Input binding requests require resolver integration after P2b (PC3)."

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


def check_chain_collisions(layers: Sequence[BindingLayer], request: FlowRequest | None) -> None:
    """PC1: reject every raw chain/explicit collision before precedence or value validation."""
    if request is None:
        return
    for position, (producer, consumer) in enumerate(zip(request.elements, request.elements[1:]), 1):
        adjacent = dict(
            match_required_inputs(producer.flow_class, consumer.flow_class, output=producer.output)
        )
        for layer in layers:
            for entry in layer.entries:
                if entry.node == NodeKey(consumer.node) and entry.name in adjacent:
                    raise FlowSettingsException(
                        f"The chain position {position} ({producer.node}) -> {position + 1} "
                        f"({consumer.node}) binds input {entry.name!r}; {entry.location} also "
                        "binds that input. Give the binding in one place, even when equal."
                    )


def effective_bindings(
    layers: Sequence[BindingLayer],
    reached: Sequence[tuple[NodeKey, type[Flow]]],
    *,
    request: FlowRequest | None = None,
) -> Mapping[NodeKey, Mapping[str, InputBinding]]:
    """Validate reached input names in every layer, then only the winning reference values.

    Origins must be supplied in increasing precedence (project, design, CLI, API).
    Ordered lists replace whole; input names merge keywise. Nodes are told apart by
    ``NodeKey``, never by flow name. This does not select edges, append design sources,
    compose producer settings or validate target-dependent types.
    """
    check_chain_collisions(layers, request)
    classes: dict[NodeKey, type[Flow]] = {}
    for key, cls in reached:
        if key in classes:
            raise ValueError(f"The node {key.label!r} is reached twice.")
        classes[key] = cls
    winners: dict[tuple[NodeKey, str], BindingEntry] = {}
    for layer in layers:
        for flow in layer.invalid_inputs:
            if NodeKey(flow) in classes:
                raise FlowSettingsException(
                    f"{layer.location}: flows.{flow}.inputs must be a mapping."
                )
        for entry in layer.entries:
            if entry.node not in classes:
                continue
            if entry.name not in declared_inputs(classes[entry.node]):
                raise FlowSettingsException(
                    f"{entry.location}: unknown input {entry.name!r} of {entry.node.label!r}."
                )
            winners[entry.node, entry.name] = entry
    selected: dict[NodeKey, dict[str, InputBinding]] = {}
    for (node, name), entry in winners.items():
        cls = classes[node]
        declaration = declared_inputs(cls)[name]
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
        binding = InputBinding(
            tuple(_reference(value, entry.location) for value in values), entry.location
        )
        selected.setdefault(node, {})[name] = binding
    return _freeze(selected)


def default_nodes(classes: Sequence[type[Flow]]) -> list[tuple[NodeKey, type[Flow]]]:
    """The reached nodes of a request that names each flow once (instance ``None``)."""
    return [(NodeKey(cls.name), cls) for cls in classes]


def require_resolver_integration(
    layers: Sequence[BindingLayer],
    reached: Sequence[type[Flow]],
    request: FlowRequest | None = None,
) -> None:
    """Keep captured wiring from being silently ignored until Task 3 integrates the resolver."""
    bindings = effective_bindings(layers, default_nodes(reached), request=request)
    if bindings or (request is not None and len(request.elements) > 1):
        raise FlowSettingsException(PENDING_INTEGRATION)


def require_no_bindings(layers: Sequence[BindingLayer]) -> None:
    """Refuse any binding where the request is not yet wired to the resolver (``--remote``)."""
    if any(layer.entries or layer.invalid_inputs for layer in layers):
        raise FlowSettingsException(PENDING_INTEGRATION)
