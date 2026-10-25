"""Reserved input wiring, captured per origin before ordinary settings composition.

These layers describe requests, not an executable graph. The resolver calls `node_bindings`
for each node it reaches, before it chooses sources or default producers. Type matching that
depends on the settings of a node, and edge discovery, belong to the resolver.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..design import Design
from ..flow import Flow, FlowSettingsException
from ..flow.io import declared_inputs, declared_outputs
from ..utils import semantic_hash
from .chains import ChainLink, FlowRequest, close_names
from .settings_layers import merge_flow_sections, registered_flow

#: The key of a flow section that holds input bindings. It is wiring, never a setting:
#: `split_bindings` takes it out of a section before the settings model sees it.
INPUTS_KEY = "inputs"


@dataclass(frozen=True)
class NodeKey:
    """The identity of one node of a request: a canonical flow name and an optional instance.

    `instance` tells apart several nodes of one flow. Chains, design files and the command
    line each name a flow once, so they all produce the default `None` instance. A key with
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
    """A canonical producer node and an optional exact output key."""

    node: NodeKey
    output: str | None = None


@dataclass(frozen=True)
class BindingEntry:
    """One unvalidated contribution.

    A lower value that another value overrides need not be a valid reference."""

    node: NodeKey
    name: str
    value: Any
    is_list: bool
    location: str


@dataclass(frozen=True)
class BindingLayer:
    """One origin's reserved keys and explicitly supplied ordinary settings.

    `kind` is the rank of the origin: a `"file"` (design or project), the command line
    (`"cli"`) or the API (`"api"`). A chain link is command-line data: it overrides the
    binding of the same input in a file and collides with one on the command line or in the API.
    """

    location: str
    entries: tuple[BindingEntry, ...]
    settings: Mapping[str, Any]
    invalid_inputs: tuple[str, ...] = ()
    kind: str = "file"


@dataclass(frozen=True)
class InputBinding:
    """The winning ordered references for one input, with a location that explains them.

    `origin` is `"chain"` or the kind of the winning layer. `overridden` lists the locations
    of file bindings that a chain link replaced. Neither is part of the identity of a run.
    """

    references: tuple[ProducerRef, ...]
    location: str
    origin: str = "file"
    overridden: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)
class _FrozenMapping(Mapping[Any, Any]):
    """An immutable, pickleable snapshot of a request, for the transport to workers."""

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
    """Copy and canonicalize an origin, removing `inputs` before the settings model sees it.

    Unknown plugin sections stay opaque until reached. Each origin is
    normalized on its own.
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
        location=location,
    )
    entries = []
    invalid_inputs = []
    for node, values in normalized.items():
        if INPUTS_KEY not in values:
            continue
        inputs = values.pop(INPUTS_KEY)
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
    "Flow chains and input bindings are local `xeda run` requests: dse does not take them."
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
            + close_names(output, list(declared_outputs(producer)))
        )
    return ProducerRef(NodeKey(producer.name), output)


def _link_bindings(
    request: FlowRequest | None,
) -> Iterator[tuple[tuple[NodeKey, str], InputBinding, ChainLink]]:
    """Yield each input that a link of the request binds, with its binding and the link."""
    if request is None:
        return
    pairs = zip(request.elements, request.elements[1:], request.links)
    for position, (producer, consumer, link) in enumerate(pairs, 1):
        location = (
            f"the chain position {position} ({producer.node}) -> {position + 1} ({consumer.node})"
        )
        if link.through:
            location += ", through " + ", ".join(flow.name for flow in link.through)
        for flow, name, output in link.binds:
            reference = ProducerRef(NodeKey(producer.node), output)
            yield (NodeKey(flow.name), name), InputBinding((reference,), location, "chain"), link


def chain_bindings(request: FlowRequest | None) -> dict[tuple[NodeKey, str], InputBinding]:
    """Return the input that each link of a chain binds.

    The key is `(consumer, input)` and the value is the one reference. A link that passes
    through other flows binds the inputs of those flows that its first element feeds."""
    return {key: binding for key, binding, _link in _link_bindings(request)}


def check_chain_collisions(layers: Sequence[BindingLayer], request: FlowRequest | None) -> None:
    """Refuse a chain link and a command-line or API binding of the same input.

    This is an error even when the two are equal, and the check comes before precedence and
    value validation. A binding in a design or project file is no collision: the chain, which
    is command-line data, overrides it."""
    chain = {key: (binding, link) for key, binding, link in _link_bindings(request)}
    for layer in layers:
        if layer.kind == "file":
            continue
        for entry in layer.entries:
            found = chain.get((entry.node, entry.name))
            if found is not None:
                binding, link = found
                # The location of a link that passes through ends in the flows between.
                comma, where = (",", f" of {entry.node.flow}") if link.through else ("", "")
                raise FlowSettingsException(
                    f"{binding.location[0].upper()}{binding.location[1:]}{comma} binds input "
                    f"{entry.name!r}{where}; {entry.location} also binds that input. Give the "
                    "binding in one place, even when equal."
                )


def _winning_entries(
    layers: Sequence[BindingLayer], key: NodeKey | None = None
) -> dict[tuple[NodeKey, str], tuple[BindingEntry, str]]:
    """Return the entry that wins each `(node, input)` and the kind of the layer it came from.

    This is the one precedence rule: a later layer replaces the whole value of an earlier one.
    `key` limits the answer to one node. Nothing is validated."""
    winners: dict[tuple[NodeKey, str], tuple[BindingEntry, str]] = {}
    for layer in layers:
        for entry in layer.entries:
            if key is None or entry.node == key:
                winners[(entry.node, entry.name)] = (entry, layer.kind)
    return winners


def _thaw(value: Any) -> Any:
    """Return a frozen binding value as plain data (a mapping, a list or a scalar)."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def written_bindings(
    layers: Sequence[BindingLayer], request: FlowRequest | None = None
) -> dict[str, dict[str, Any]]:
    """Return the bindings of a request as one table: flow name, then input name, then the value.

    The value is the text of the winning entry, as the user wrote it (a name, or a list of
    names), and it is not validated. A table in this shape is the `inputs` of a flow section,
    which a host that gets the request without its origins (`--remote`) plans again. The
    bindings that a chain link makes are left out: the chain replaces them there as it does
    here."""
    chained = set(chain_bindings(request))
    table: dict[str, dict[str, Any]] = {}
    for (node, name), (entry, _kind) in _winning_entries(layers).items():
        if node.instance is not None:
            raise ValueError(f"The binding of {node.label!r} has no place in a flow section.")
        if (node, name) not in chained:
            table.setdefault(node.flow, {})[name] = _thaw(entry.value)
    return table


def node_bindings(
    layers: Sequence[BindingLayer],
    key: NodeKey,
    cls: type[Flow],
    *,
    request: FlowRequest | None = None,
) -> dict[str, InputBinding]:
    """Return the winning binding of each explicitly bound input of one reached node.

    Origins must be supplied in increasing precedence (project, design, CLI, API). The input
    names of every layer are checked, but only a winning value is validated as a reference. A
    chain link replaces a file binding of its input and notes the location of that binding.
    """
    declarations = declared_inputs(cls)
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
                    + (
                        close_names(entry.name, list(declarations))
                        if declarations
                        else f" `{entry.node.label}` declares no inputs;"
                        " only a flow's declared inputs can be bound."
                    )
                )
    winners = {name: won for (_node, name), won in _winning_entries(layers, key).items()}
    chain = {
        name: adjacency
        for (node, name), adjacency in chain_bindings(request).items()
        if node == key
    }
    selected: dict[str, InputBinding] = {}
    for name, (entry, kind) in winners.items():
        if name in chain:
            continue  # replaced by the chain: a replaced value is never validated
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
    for name, adjacency in chain.items():
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
    """Return `node_bindings` of every reached node that has any, after the chain collision check.

    Nodes are told apart by `NodeKey`, never by flow name. This function does not select edges,
    append design sources, compose producer settings or validate types that depend on the
    settings.
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
    inputs: Sequence[Any], identity_of: Callable[[str], str], design: Design
) -> tuple[tuple[str, Any], ...]:
    """Return where each resolved input of a node comes from, in declaration order.

    The origin of an input that sources of the design supply is `("source", entries)`, where the
    entries are the `(path, type)` of each file it takes, in order. The path is the place from
    the design root (`Design.source_place`) and the entry holds no content, so two netlists are
    two origins, and the same netlist in two checkouts is one. The origin of an input that
    producers supply is its ordered `(producer identity, output key)` references, and none for an
    absent input. `inputs` are the `ResolvedInput`s of a plan node. `identity_of` gives the
    identity of the plan node that a reference names, so producers are identified before their
    consumers. The order of the references and of the files counts. Where a binding was written
    (chain, file, command line, API) does not.
    """
    listed = {source.path: source for source in design.rtl.sources}

    def origin(resolved: Any) -> Any:
        if resolved.origin == "source":
            return (
                "source",
                tuple(
                    (design.source_place(listed[path]), listed[path].type.name)
                    for path in resolved.sources
                ),
            )
        return tuple((identity_of(ref.node), ref.output) for ref in resolved.references)

    return tuple((resolved.name, origin(resolved)) for resolved in inputs)


def node_identity(settings_hash: str, configuration_hash: str, origins: Sequence[Any] = ()) -> str:
    """Return the identity of a node: its settings, its design configuration and its ordered
    resolved input origins.

    This is the one identity rule (`input_origins`), for default and explicit edges alike.
    `settings_hash` is the settings-only `flowrun_hash`. `configuration_hash` is the hash of what
    the node reads of its design (`configuration.configuration_hash`). A flow that declares no
    inputs has no origins.

    The freeze of the resolver, the plan validation and run identity of the launcher, the
    run-directory claims, `results.json` and the trace all take the hash of a run from here.
    """
    return semantic_hash(
        {"settings": settings_hash, "configuration": configuration_hash, "inputs": tuple(origins)}
    )


def default_nodes(classes: Sequence[type[Flow]]) -> list[tuple[NodeKey, type[Flow]]]:
    """Return the reached nodes of a request that names each flow once (instance `None`)."""
    return [(NodeKey(cls.name), cls) for cls in classes]


def require_no_bindings(layers: Sequence[BindingLayer], reached: Sequence[NodeKey]) -> None:
    """Refuse a binding of a node that the request reaches, where requests are not resolved locally.

    That is the case for `--remote`. `reached` are the nodes of the request's graph, without
    bindings. Any other node could be reached only through a binding of one of these. A binding
    saved for a flow that the request never runs is no refusal."""
    nodes = set(reached)
    for layer in layers:
        if any(entry.node in nodes for entry in layer.entries) or any(
            NodeKey(flow) in nodes for flow in layer.invalid_inputs
        ):
            raise FlowSettingsException(LOCAL_REQUESTS_ONLY)
