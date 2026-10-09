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


def _involved(graph: Graph, node: Node, declaration: InputDeclaration) -> list[tuple[Node, str]]:
    """Every input that has to agree for the relation of `declaration` to hold: the related
    inputs of `node` and, with `via`, that input of each node that makes the related input,
    with the inputs of that node tied to it."""
    anchor = declaration.same_producer_as
    assert anchor is not None
    members = _related(node, declaration)
    if declaration.via is None:
        return [(node, name) for name in (anchor, *members)]
    involved = [(node, name) for name in members]
    for made_by in sorted(_origin(node, anchor) or ()):
        maker = graph[made_by]
        tied = [
            other.name
            for other in declared_inputs(maker.cls).values()
            if other.same_producer_as == declaration.via
        ]
        involved += [(maker, name) for name in (declaration.via, *tied)]
    return involved


def _binding(node: Node, name: str, producer: str) -> str | None:
    """The `-s` item that binds input `name` of `node` to node `producer`, naming the output when
    it is clear: the only one that fits, or the one named like the input. None when the producer
    cannot supply the input."""
    producer_cls = registered_flow(producer)
    if producer_cls is None:
        return None
    fitting, _many = fitting_outputs(producer_cls, declared_inputs(node.cls)[name])
    if len(fitting) != 1 and name in fitting:
        fitting = [name]
    if len(fitting) != 1:
        return None
    return f"flows.{node.label}.inputs.{name}={producer}.{fitting[0]}"


def _advice(graph: Graph, node: Node, declaration: InputDeclaration) -> str:
    """What to do about a broken relation. The inputs the user bound keep their producer, and
    every other generated input that has to agree moves to it, so adding the bindings makes the
    plan pass. When the bound inputs themselves name different producers, no binding can
    help without overriding one of them, and the advice says so."""
    involved = _involved(graph, node, declaration)
    bound = [(n, name) for n, name in involved if _explicit(n, name) and _origin(n, name)]
    targets = {_origin(n, name) for n, name in bound}
    if len(targets) > 1:
        said = "; ".join(_said(n, name) for n, name in bound)
        return f" These inputs are bound to different producers ({said}): bind them to one."
    if len(targets) == 1:
        (target,) = targets
        if target is not None and len(target) == 1:
            (producer,) = target
            moves = []
            for n, name in involved:
                if _origin(n, name) in (target, None):  # agrees already, or is the user's file
                    continue
                move = _binding(n, name, producer)
                if move is None:
                    return " Take all of them from one producer."
                moves.append(move)
            return f" To use `{producer}` for all of them, add `-s {' '.join(moves)}`."
    return " Take all of them from one producer."


def _message(graph: Graph, node: Node, declaration: InputDeclaration, theirs: Origin) -> str:
    """The refusal. `theirs` is where the related input of a `via` relation is followed to."""
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
    return f"{head}, but " + "; ".join(states) + "." + _advice(graph, node, declaration)


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
                    raise FlowSettingsException(_message(graph, node, declaration, frozenset()))
                continue
            mine = _origin(node, declaration.name)
            theirs = _followed(graph, node, declaration)
            if mine is not None and theirs is not None and mine != theirs:
                raise FlowSettingsException(_message(graph, node, declaration, theirs))
