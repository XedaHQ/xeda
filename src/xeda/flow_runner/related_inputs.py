"""Related inputs of one flow come from one producer.

A flow declares that an input is related to another (`In(same_producer_as=...)`): the netlist a
simulation reads and the SDF that annotates it, or the checkpoint that power is reported on and
the activity of a simulation of that same synthesis. A plan that takes two such inputs from
different producers pairs one run's files with another's, and nothing in the files shows it.
The resolver calls `check_related_inputs` once, on the final graph. It refuses a plan that breaks
a relation, naming the inputs, where each comes from and the bindings that would repair it.

The rule compares the files that xeda generates. An input comes from a set of producer nodes,
and two related inputs agree when they come from the same nodes. A file the user lists in the
design's sources is the user's own choice, so an input that comes from a source, or from nothing
(an optional input that is absent), is not compared.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, NamedTuple

from ..flow import Flow, FlowSettingsException
from ..flow.io import InputDeclaration, declared_inputs
from .chains import fitting_outputs
from .settings_layers import registered_flow

if TYPE_CHECKING:
    from .resolver import ResolvedInput

__all__ = ["Node", "check_related_inputs"]

Origin = frozenset[str]


class Node(NamedTuple):
    """A node of the graph, as the rule sees it."""

    label: str
    cls: type[Flow]
    inputs: Mapping[str, ResolvedInput]


Graph = Mapping[str, Node]


def _origin(node: Node, name: str) -> Origin | None:
    """The producer nodes input `name` of `node` comes from; None for an input that comes from
    the design's sources or is absent."""
    item = node.inputs.get(name)
    if item is None or item.origin != "producer":
        return None
    return frozenset(reference.node for reference in item.references)


def _said(node: Node, name: str) -> str:
    item = node.inputs.get(name)
    return item.describe() if item is not None else f"{name} <- none"


def _explicit(node: Node, name: str) -> bool:
    """Whether the user bound input `name` (a chain, a saved binding, `-s` or the API)."""
    item = node.inputs.get(name)
    return item is not None and item.binding_origin is not None


def _related(node: Node, declaration: InputDeclaration) -> list[str]:
    """The inputs of `node` related in the same way as `declaration`: itself and every other that
    names the same input, with the same `via`."""
    return [
        other.name
        for other in declared_inputs(node.cls).values()
        if (other.same_producer_as, other.via) == (declaration.same_producer_as, declaration.via)
    ]


def _followed(graph: Graph, node: Node, declaration: InputDeclaration) -> Origin | None:
    """The producers the input that `declaration` is related to comes from. With `via`, one step
    on: where that input of each node that makes the related input comes from."""
    assert declaration.same_producer_as is not None
    anchor = _origin(node, declaration.same_producer_as)
    if declaration.via is None or anchor is None:
        return anchor
    followed: set[str] = set()
    for made_by in anchor:
        maker = graph[made_by]
        if declaration.via not in declared_inputs(maker.cls):
            raise FlowSettingsException(
                f"{node.cls.name}.{declaration.name} has to come from the producer of "
                f"`{declaration.via}` of the flow that makes `{declaration.same_producer_as}`, "
                f"and `{made_by}` has no input `{declaration.via}`: xeda cannot tell what its "
                "output was made from"
            )
        followed |= _origin(maker, declaration.via) or frozenset()
    return frozenset(followed) or None


def _moves(graph: Graph, node: Node, names: Iterable[str], target: Origin) -> list[str] | None:
    """The `-s` items that bind each of `names` (inputs of `node`) to the one producer in
    `target`, naming the output when it is clear: the only one that fits, or the one named like
    the input. None when `target` is no single producer or one of them cannot supply an input."""
    if len(target) != 1:
        return None
    (producer,) = target
    producer_cls = registered_flow(producer)
    if producer_cls is None:
        return None
    items = []
    for name in names:
        if _origin(node, name) in (target, None):  # agrees already, or is the user's own file
            continue
        fitting, _many = fitting_outputs(producer_cls, declared_inputs(node.cls)[name])
        if len(fitting) != 1 and name in fitting:
            fitting = [name]
        if len(fitting) != 1:
            return None
        items.append(f"flows.{node.label}.inputs.{name}={producer}.{fitting[0]}")
    return items


def _repair(
    graph: Graph, node: Node, declaration: InputDeclaration, mine: Origin, theirs: Origin
) -> tuple[str, list[str]] | None:
    """The producer to take the related inputs from and the bindings that do it. The side the
    user bound keeps its producer, and the other side moves to it; None when no side is clear."""
    assert declaration.same_producer_as is not None
    members = _related(node, declaration)
    if declaration.via is None:
        everyone = [declaration.same_producer_as, *members]
        chosen = next((name for name in everyone if _explicit(node, name)), None)
        target = _origin(node, chosen) if chosen is not None else None
        moves = None if target is None else _moves(graph, node, everyone, target)
        return (next(iter(target)), moves) if target and moves else None
    if any(_explicit(node, name) for name in members):
        # this input keeps its producer: the flow that makes the related input follows it
        moves = []
        for made_by in sorted(_origin(node, declaration.same_producer_as) or ()):
            maker = graph[made_by]
            dependents = [
                other.name
                for other in declared_inputs(maker.cls).values()
                if other.same_producer_as == declaration.via
            ]
            more = _moves(graph, maker, [declaration.via, *dependents], mine)
            if more is None:
                return None
            moves += more
        return (next(iter(mine)), moves) if moves and len(mine) == 1 else None
    moves = _moves(graph, node, members, theirs)
    return (next(iter(theirs)), moves) if moves else None


def _message(
    graph: Graph, node: Node, declaration: InputDeclaration, mine: Origin, theirs: Origin
) -> str:
    """The refusal. `mine` and `theirs` are the two sides of a `via` relation (empty otherwise)."""
    anchor = declaration.same_producer_as
    assert anchor is not None
    members = _related(node, declaration)
    if declaration.via is None:
        names = [anchor, *members]
        head = f"{node.cls.name}: " + ", ".join(f"`{name}`" for name in names)
        head += " must come from the same producer"
        states = [_said(node, name) for name in names]
    else:
        head = (
            f"{node.cls.name}: " + ", ".join(f"`{name}`" for name in members) + " must come "
            f"from the producer of the `{declaration.via}` that the flow making `{anchor}` reads"
        )
        states = [_said(node, name) for name in (*members, anchor)]
        states.append(f"that `{declaration.via}` comes from " + ", ".join(sorted(theirs)))
    text = f"{head}, but " + "; ".join(states) + "."
    repair = _repair(graph, node, declaration, mine, theirs)
    if repair is not None:
        producer, moves = repair
        return text + f" To use `{producer}` for all of them, add `-s {' '.join(moves)}`."
    return text + " Take all of them from one producer."


def check_related_inputs(nodes: Iterable[Node]) -> None:
    """Refuse a graph in which two related inputs of a node come from different producers (a
    `FlowSettingsException`). `nodes` are the nodes of the final graph; the first whose relation
    breaks, in the order given, is reported."""
    graph = {node.label: node for node in nodes}
    for node in graph.values():
        for declaration in declared_inputs(node.cls).values():
            if declaration.same_producer_as is None:
                continue
            if declaration.via is None:
                names = [declaration.same_producer_as, *_related(node, declaration)]
                if len({origin for name in names if (origin := _origin(node, name))}) > 1:
                    raise FlowSettingsException(
                        _message(graph, node, declaration, frozenset(), frozenset())
                    )
                continue
            mine = _origin(node, declaration.name)
            theirs = _followed(graph, node, declaration)
            if mine is not None and theirs is not None and mine != theirs:
                raise FlowSettingsException(_message(graph, node, declaration, mine, theirs))
