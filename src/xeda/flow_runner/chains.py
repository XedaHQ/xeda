"""Pure parsing of chain requests, and checks of chains against declared inputs and outputs.

The grammar of requests is separate from the resolution of the graph. Parsing canonicalizes the
names of flows and outputs. The edge predicate uses the types that flows declare, or that their
settings narrow them to, and it constructs no flow and probes no tool.
"""

from __future__ import annotations

import difflib
from collections import deque
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from ..design import SourceType
from ..flow import Flow, FlowSettingsException
from ..flow.io import declared_inputs, declared_outputs

__all__ = [
    "ChainEdge",
    "ChainElement",
    "DefaultEdge",
    "DefaultGraph",
    "FlowRequest",
    "binding_points",
    "close_names",
    "complete_request",
    "default_graph",
    "edges",
    "fitting_outputs",
    "followers",
    "match_required_inputs",
    "parse_request",
    "predecessors",
    "request_text",
    "suggest_chains",
    "validate_chain",
]

#: The most corrected requests an error advertises.
MAX_SUGGESTIONS = 4


@dataclass(frozen=True)
class ChainElement:
    """One canonical flow and, optionally, the output selected for its next edge."""

    flow_class: type[Flow]
    output: str | None = None

    @property
    def node(self) -> str:
        """The canonical name of the request node (a request has one node per flow)."""
        return self.flow_class.name


@dataclass(frozen=True)
class FlowRequest:
    """A normalized request for one flow or for adjacent flows."""

    elements: tuple[ChainElement, ...]

    @property
    def requested(self) -> type[Flow]:
        """The requested flow: the final element of the chain."""
        return self.elements[-1].flow_class


def parse_request(text: str) -> FlowRequest:
    """Parse `flow[.output][+flow[.output]...]` and canonicalize every flow name.

    Flow classes are looked up lazily here, to keep the request module independent of the
    initialization of the launcher. The parser does not construct a flow instance.
    """
    from .default_runner import FlowNotFoundError, get_flow_class

    parts = text.split("+")
    elements: list[ChainElement] = []
    seen: set[str] = set()
    for position, raw_part in enumerate(parts, 1):
        part = raw_part.strip()
        if not part:
            raise FlowSettingsException(
                f"Flow chain element {position} is empty; use FLOW[.OUTPUT][+FLOW[.OUTPUT]...]."
            )
        if part.count(".") > 1:
            raise FlowSettingsException(
                f"Flow chain element {position} has an invalid output qualifier {part!r}; "
                "use one exact output key after a single dot."
            )
        flow_name, separator, output = part.partition(".")
        if separator and not output:
            raise FlowSettingsException(
                f"Flow chain element {position} has an empty output qualifier; "
                "use an exact output key after the dot."
            )
        if not flow_name:
            raise FlowSettingsException(f"Flow chain element {position} has no flow name.")
        try:
            flow_class = get_flow_class(flow_name)
        except FlowNotFoundError:
            raise
        canonical = flow_class.name
        if canonical in seen:
            raise FlowSettingsException(
                f"Flow `{canonical}` appears more than once; a chain is a path and each flow "
                "may appear only once."
            )
        seen.add(canonical)
        elements.append(ChainElement(flow_class, output if separator else None))

    if elements[-1].output is not None:
        raise FlowSettingsException(
            f"The last element `{elements[-1].node}` cannot select an output because it has no "
            "following consumer."
        )
    request = FlowRequest(tuple(elements))
    validate_chain(request.elements)
    return request


class _EdgeFailure(Exception):
    """An adjacent pair that cannot be an edge.

    It holds the position, the reason, and whether another chain could correct the request.
    Another chain cannot correct a request whose output name does not exist: the message of
    that error already names the close matches."""

    def __init__(self, index: int, text: str, *, advise: bool = True) -> None:
        super().__init__(text)
        self.index = index
        self.text = text
        self.advise = advise


def request_text(elements: Sequence[ChainElement]) -> str:
    """Return the canonical request text of `elements`: `flow[.output]` joined by `+`."""
    return "+".join(
        element.node + (f".{element.output}" if element.output else "") for element in elements
    )


def _check_chain(elements: Sequence[ChainElement]) -> None:
    """Check a chain by the action, repeat and edge rules, without instantiation.

    This is the one chain validator. A pair that is no edge raises `_EdgeFailure`. Every other
    refusal is a `FlowSettingsException`."""
    if len(elements) <= 1:
        return
    seen: set[str] = set()
    for element in elements:
        if element.node in seen:
            raise FlowSettingsException(
                f"Flow `{element.node}` appears more than once; a chain is a path and each flow "
                "may appear only once."
            )
        seen.add(element.node)
    for index, producer in enumerate(elements[:-1]):
        consumer = elements[index + 1]
        reason = getattr(producer.flow_class, "action_reason", None)
        if reason:
            raise FlowSettingsException(
                f"Flow `{producer.node}` {reason.removeprefix('it ')} and can only end a chain."
            )
        try:
            matched = match_required_inputs(
                producer.flow_class, consumer.flow_class, output=producer.output
            )
        except FlowSettingsException as e:
            unknown = producer.output is not None and producer.output not in declared_outputs(
                producer.flow_class
            )
            raise _EdgeFailure(index, str(e), advise=not unknown) from None
        if not matched:
            raise _EdgeFailure(index, _no_edge(producer.flow_class, consumer.flow_class))


def _valid(elements: Sequence[ChainElement]) -> bool:
    try:
        _check_chain(elements)
    except (FlowSettingsException, _EdgeFailure):
        return False
    return True


def validate_chain(elements: Sequence[ChainElement]) -> None:
    """Validate the action and repeat rules and every adjacent edge, without instantiation.

    A pair that is no edge is refused with the corrected requests that do validate, when there
    are any (`suggest_chains`)."""
    try:
        _check_chain(elements)
    except _EdgeFailure as failure:
        advice = suggest_chains(elements, failure.index) if failure.advise else []
        if not advice:
            raise FlowSettingsException(failure.text) from None
        quoted = " or ".join(f"`{request_text(chain)}`" for chain in advice)
        raise FlowSettingsException(f"{failure.text} Did you mean {quoted}?") from None


def suggest_chains(elements: Sequence[ChainElement], index: int) -> list[tuple[ChainElement, ...]]:
    """Return corrected requests for the pair at `index` and `index + 1` that is no edge.

    Every correction is the whole request (its prefix, its suffix and the other qualifiers
    kept) and passes `_check_chain`, so no repeat, action or missing edge is advertised.

    There are two kinds of correction. One is another output of the producer (for an ambiguous
    or unfitting qualifier). The other inserts flows between the pair along required
    default-producer edges only (`_default_routes`: the flows between the pair in the default
    graph of the consumer). There is no search over all flows, and nothing is executed.
    """
    producer, consumer = elements[index], elements[index + 1]
    head, tail = tuple(elements[:index]), tuple(elements[index + 2 :])
    found: list[tuple[ChainElement, ...]] = []

    def offer(candidate: tuple[ChainElement, ...]) -> None:
        if candidate not in found and _valid(candidate):
            found.append(candidate)

    for output in (None, *declared_outputs(producer.flow_class)):
        if output != producer.output:
            offer(head + (ChainElement(producer.flow_class, output), consumer) + tail)
    if not found:
        for route in _default_routes(producer.flow_class, consumer.flow_class):
            between = tuple(ChainElement(cls) for cls in route)
            # The producer's own qualifier first; unqualified only when that one cannot work.
            for output in dict.fromkeys((producer.output, None)):
                before = len(found)
                offer(
                    head
                    + (ChainElement(producer.flow_class, output),)
                    + between
                    + (consumer,)
                    + tail
                )
                if len(found) > before:
                    break
    return found[:MAX_SUGGESTIONS]


def _default_routes(producer: type[Flow], consumer: type[Flow]) -> list[tuple[type[Flow], ...]]:
    """Return the flows between `producer` and `consumer`, in chain order, as one route.

    The route is `DefaultGraph.between` in the default graph of `consumer`. It is empty, and so
    is the list, when `producer` is not in that graph or is next to `consumer`. The result is
    only a hint, since the whole candidate chain is validated afterwards."""
    between = default_graph(consumer).between(producer)
    return [between] if between else []


@dataclass(frozen=True)
class DefaultEdge:
    """One edge of a default graph: `consumer.input` takes `producer.output` by default."""

    consumer: type[Flow]
    input: str
    producer: type[Flow]
    #: The output that implicit planning takes: the one the input declares, else the only output
    #: of the producer with a type the input accepts. None when it can choose no output.
    output: str | None


@dataclass(frozen=True)
class DefaultGraph:
    """The flows that implicit planning of `root` runs, and the edges between them.

    The graph follows every input that names a default producer, required or not, over the
    declarations alone. It has one node per flow and every edge met: edges that converge on a
    flow, and two inputs of one flow that name the same producer, are all there."""

    root: type[Flow]
    #: Every edge, in breadth-first order of the consumers and in declaration order of the
    #: inputs of one consumer.
    edges: tuple[DefaultEdge, ...]

    def flows(self) -> tuple[type[Flow], ...]:
        """Return the root, then every other flow once, in the order the walk meets them."""
        return tuple(dict.fromkeys((self.root, *(edge.producer for edge in self.edges))))

    def between(self, producer: type[Flow]) -> tuple[type[Flow], ...]:
        """Return the flows on any path from `producer` to the root, in chain order.

        Neither `producer` nor the root is in the result. It is empty when `producer` is not in
        the graph or is next to the root. A producer comes before its consumer: the order is the
        depth-first post-order from the root over the edges, as the resolver lists plan nodes."""
        feeds: dict[type[Flow], list[type[Flow]]] = {}
        for edge in self.edges:
            feeds.setdefault(edge.consumer, []).append(edge.producer)
        # The flows that reach `producer`: the walk back over the edges, from `producer`.
        reaching = {producer}
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if edge.producer in reaching and edge.consumer not in reaching:
                    reaching.add(edge.consumer)
                    changed = True
        visited: set[type[Flow]] = set()
        order: list[type[Flow]] = []

        def visit(flow: type[Flow]) -> None:
            visited.add(flow)
            for upstream in feeds.get(flow, ()):
                if upstream not in visited:
                    visit(upstream)
            order.append(flow)

        visit(self.root)
        # Every flow of the graph is reachable from the root, so reaching `producer` decides.
        return tuple(
            flow
            for flow in order
            if flow in reaching and flow is not producer and flow is not self.root
        )


def default_graph(consumer: type[Flow]) -> DefaultGraph:
    """Return the default graph of `consumer`, from its declarations alone.

    This mirrors the discovery of implicit planning: a breadth-first walk from `consumer` over
    the inputs, in declaration order, that name a registered default producer. Each flow is
    expanded once. An edge to a flow that is already expanded is kept, so a cycle of defaults
    ends and hides no other path."""
    from .settings_layers import registered_flow

    found: list[DefaultEdge] = []
    expanded = {consumer}
    queue = deque([consumer])
    while queue:
        current = queue.popleft()
        for declaration in declared_inputs(current).values():
            if declaration.producer is None:
                continue
            upstream = registered_flow(declaration.producer)
            if upstream is None:
                continue
            outputs = declared_outputs(upstream)
            if declaration.output:
                output: str | None = declaration.output
            else:
                matching = [
                    made.name
                    for made in outputs.values()
                    if set(made.types) & set(declaration.types)
                ]
                output = matching[0] if len(matching) == 1 else None
            found.append(DefaultEdge(current, declaration.name, upstream, output))
            if upstream not in expanded:
                expanded.add(upstream)
                queue.append(upstream)
    return DefaultGraph(consumer, tuple(found))


def binding_points(
    graph: DefaultGraph, producer: type[Flow], output: str | None = None
) -> tuple[tuple[DefaultEdge, str], ...]:
    """Return the required inputs of the graph that a flow `producer` can feed, with the output.

    A binding point is an edge of the graph from `producer` to a required input: a chain never
    binds an optional input. The output is the one `fitting_outputs` selects for the input, as
    for the adjacent flows of a chain. `output` limits the choice to that output of `producer`.
    The result is in graph order and is empty when no edge comes from `producer`. A binding
    point that `producer` cannot feed is refused, with the point named as `consumer.input`."""
    points = [
        edge
        for edge in graph.edges
        if edge.producer is producer and declared_inputs(edge.consumer)[edge.input].required
    ]
    outputs = declared_outputs(producer)
    if points and output is not None and output not in outputs:
        raise FlowSettingsException(
            f"Flow `{producer.name}` has no output `{output}`.{close_names(output, list(outputs))}"
        )
    chosen: list[tuple[DefaultEdge, str]] = []
    for edge in points:
        declaration = declared_inputs(edge.consumer)[edge.input]
        where = f"{edge.consumer.name}.{edge.input}"
        fitting, many = fitting_outputs(producer, declaration, output=output)
        made = f"`{producer.name}.{output}`" if output else f"`{producer.name}`"
        if len(fitting) > 1:
            raise FlowSettingsException(
                f"Input `{where}` has ambiguous outputs from `{producer.name}`: "
                f"{', '.join(f'`{name}`' for name in fitting)}; qualify the producer output."
            )
        if not fitting:
            if many:
                raise FlowSettingsException(
                    f"Flow `{producer.name}` output `{many[0]}` has incompatible cardinality "
                    f"for input `{where}`, which takes one file."
                )
            made_types = {
                t.name for name, o in outputs.items() if output in (None, name) for t in o.types
            }
            raise FlowSettingsException(
                f"Input `{where}` takes {'/'.join(t.name for t in declaration.types)}, "
                f"but {made} makes {'/'.join(sorted(made_types)) or 'nothing'}."
            )
        chosen.append((edge, fitting[0]))
    return tuple(chosen)


@dataclass(frozen=True)
class ChainEdge:
    """One way `producer` can directly precede `consumer`.

    It is exactly what the chain validator accepts for that adjacency, with the input and
    output pairs it binds."""

    producer: type[Flow]
    consumer: type[Flow]
    #: The output of the producer that a qualified request names; None when the unqualified
    #: request is valid.
    output: str | None
    binds: tuple[tuple[str, str], ...]
    #: Whether a produced or accepted kind is a union that the settings narrow, so that the
    #: final plan decides.
    target_dependent: bool


def edges(producer: type[Flow], consumer: type[Flow]) -> list[ChainEdge]:
    """Return the ways `producer+consumer` is a valid adjacency.

    That is the unqualified request, else one qualified request per output of the producer that
    makes it valid. The chain validator itself judges, on declarations alone."""
    outputs = declared_outputs(producer)
    found: list[ChainEdge] = []
    for output in (None, *outputs):
        if not _valid((ChainElement(producer, output), ChainElement(consumer))):
            continue
        binds = tuple(match_required_inputs(producer, consumer, output=output))
        inputs = declared_inputs(consumer)
        found.append(
            ChainEdge(
                producer,
                consumer,
                output,
                binds,
                target_dependent=any(
                    len(outputs[made].types) > 1 or len(inputs[taken].types) > 1
                    for taken, made in binds
                ),
            )
        )
        if output is None:
            break
    return found


def followers(producer: type[Flow], candidates: Sequence[type[Flow]]) -> list[ChainEdge]:
    """Return the edges from `producer` to each of `candidates`, in candidate order."""
    return [edge for consumer in candidates for edge in edges(producer, consumer)]


def predecessors(consumer: type[Flow], candidates: Sequence[type[Flow]]) -> list[ChainEdge]:
    """Return the edges from each of `candidates` to `consumer`, in candidate order."""
    return [edge for producer in candidates for edge in edges(producer, consumer)]


def _lenient_element(token: str) -> ChainElement | None:
    """Return `flow[.output]` as typed so far, or None when it names no flow or is malformed."""
    from .default_runner import FlowNotFoundError, get_flow_class

    name, dot, output = token.partition(".")
    if not name or "." in output or (dot and not output):
        return None
    try:
        return ChainElement(get_flow_class(name), output if dot else None)
    except (FlowNotFoundError, FlowSettingsException):
        return None


def complete_request(incomplete: str, candidates: Sequence[type[Flow]]) -> list[str]:
    """Return completions of an unfinished request, each the whole current token.

    A completion is what was typed before the last `+`, exactly as typed, followed by the
    completed element. After a `+`, only flows that the validator accepts as the next element
    are offered. A flow is offered only if it does not repeat one, does not follow an action,
    takes a required input and has a compatible edge from the flow before it. In a qualifier
    (`nextpnr.`), the outputs of the producer that lead to some follower are offered. The
    function is static: it reads declarations only, loads no design, constructs no flow and
    probes no tool.
    """
    head_text, plus, partial = incomplete.rpartition("+")
    head: list[ChainElement] = []
    for token in head_text.split("+") if plus else []:
        element = _lenient_element(token)
        if element is None:
            return []
        head.append(element)
    typed = head_text + plus
    name, dot, output = partial.partition(".")
    if dot:
        element = _lenient_element(name)
        if element is None or "." in output:
            return []
        found = []
        for made in declared_outputs(element.flow_class):
            if not made.startswith(output):
                continue
            producer = (*head, ChainElement(element.flow_class, made))
            if any(
                _valid((*producer, ChainElement(follower)))
                for follower in candidates
                if follower is not element.flow_class
            ):
                found.append(f"{typed}{name}.{made}")
        return found
    wanted = name.lower().replace("-", "_")
    found = []
    for flow_class in candidates:
        spellings = [
            n for n in (flow_class.name, *flow_class.aliases) if n.lower().startswith(wanted)
        ]
        if not spellings:
            continue
        if head and not _valid((*head, ChainElement(flow_class))):
            continue
        spelling = flow_class.name if flow_class.name in spellings else spellings[0]
        found.append(typed + spelling)
    return found


def fitting_outputs(
    producer: type[Flow],
    declaration: Any,
    *,
    output: str | None = None,
    accepted: Sequence[SourceType] | None = None,
    selected_output_types: Mapping[str, Sequence[SourceType]] | None = None,
) -> tuple[list[str], list[str]]:
    """Return the outputs of `producer` that can supply the input `declaration`.

    This is the one edge predicate, for chain adjacency and explicit bindings alike. The result
    is a pair of lists. The second holds the outputs of a fitting kind that cannot supply the
    input, because they are many-valued and the input takes one file.

    An output fits when the types it can make are nonempty and a subset of the types the input
    accepts (an output that may be of a kind the input cannot take does not fit), and a
    many-valued output feeds only a many-valued input. `output` restricts the question to one
    key. When several outputs fit and the default producer of the input is `producer`, the
    declared default output decides.
    """
    takes = tuple(declaration.types if accepted is None else accepted)
    fitting: list[str] = []
    many: list[str] = []
    for name, made in declared_outputs(producer).items():
        if output is not None and name != output:
            continue
        produced = tuple((selected_output_types or {}).get(name, made.types))
        if not produced or not set(produced).issubset(takes):
            continue
        if made.cardinality == "many" and declaration.cardinality != "many":
            many.append(name)
            continue
        fitting.append(name)
    if len(fitting) > 1 and declaration.producer is not None and declaration.output in fitting:
        from .settings_layers import registered_flow

        if registered_flow(declaration.producer) is producer:
            fitting = [declaration.output]
    return fitting, many


def match_required_inputs(
    producer: type[Flow],
    consumer: type[Flow],
    *,
    output: str | None = None,
    selected_input_types: Mapping[str, Sequence[SourceType]] | None = None,
    selected_output_types: Mapping[str, Sequence[SourceType]] | None = None,
) -> list[tuple[str, str]]:
    """Return all required compatible `(input, output)` pairs in consumer declaration order.

    A produced type set must be nonempty and a subset of the accepted input types. A many-valued
    output can feed only a many-valued input. Optional inputs are excluded on purpose, because
    adjacency is not an opt-in for them.
    """
    inputs = declared_inputs(consumer)
    outputs = declared_outputs(producer)
    if output is not None and output not in outputs:
        raise FlowSettingsException(
            f"Flow `{producer.name}` has no output `{output}`.{close_names(output, list(outputs))}"
        )

    matches: list[tuple[str, str]] = []
    cardinality_mismatches: list[tuple[str, str]] = []
    for input_name, declaration in inputs.items():
        if not declaration.required:
            continue
        candidates, many = fitting_outputs(
            producer,
            declaration,
            output=output,
            accepted=(selected_input_types or {}).get(input_name),
            selected_output_types=selected_output_types,
        )
        cardinality_mismatches += [(input_name, name) for name in many]
        if len(candidates) > 1:
            raise FlowSettingsException(
                f"Flow `{consumer.name}` input `{input_name}` has ambiguous outputs from "
                f"`{producer.name}`: {', '.join(f'`{name}`' for name in candidates)}; "
                "qualify the producer output."
            )
        if candidates:
            matches.append((input_name, candidates[0]))

    if not matches and any(declaration.required for declaration in inputs.values()):
        if cardinality_mismatches:
            input_name, output_name = cardinality_mismatches[0]
            raise FlowSettingsException(
                f"Flow `{producer.name}` output `{output_name}` has incompatible cardinality "
                f"for `{consumer.name}` input `{input_name}`."
            )
        raise FlowSettingsException(_no_edge(producer, consumer))
    return matches


def close_names(name: str, declared: Sequence[str]) -> str:
    """Return a hint for a name that is no name of the flow, for an input or an output.

    The hint is ` Did you mean `x`?` for the declared names close to `name`, else the list of
    what is declared, else nothing."""
    close = difflib.get_close_matches(name, list(declared), n=3, cutoff=0.5)
    if close:
        return f" Did you mean {', '.join(f'`{n}`' for n in close)}?"
    return f" It declares: {', '.join(f'`{n}`' for n in declared)}." if declared else ""


def _no_edge(producer: type[Flow], consumer: type[Flow]) -> str:
    """Return why `producer` cannot be chained before `consumer`: what each takes and makes."""

    def listed(declarations: Mapping[str, Any]) -> str:
        return (
            ", ".join(
                f"{name} ({'/'.join(t.name for t in declared.types)})"
                for name, declared in declarations.items()
            )
            or "nothing"
        )

    required = {
        name: declared for name, declared in declared_inputs(consumer).items() if declared.required
    }
    if not required:
        return (
            f"Flow `{producer.name}` cannot precede `{consumer.name}`: `{consumer.name}` takes no "
            "required input, and a chain feeds only required inputs."
        )
    return (
        f"Flow `{producer.name}` has no compatible output for a required input of "
        f"`{consumer.name}`: `{consumer.name}` takes {listed(required)}; `{producer.name}` "
        f"makes {listed(declared_outputs(producer))}."
    )
