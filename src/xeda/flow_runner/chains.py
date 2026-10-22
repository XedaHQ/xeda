"""Pure parsing of chain requests, and checks of chains against declared inputs and outputs.

The grammar of requests is separate from the resolution of the graph. Parsing canonicalizes the
names of flows and outputs. The edge predicate uses the types that flows declare, or that their
settings narrow them to, and it constructs no flow and probes no tool.

A link `a+b` is valid when `a` feeds a required input of `b` directly, or when the default
producers of `b` reach `a`: the link then passes through the flows between them, and binds each
required input that `a` feeds. Planning runs the flows between exactly as it does for `b` alone.

A converter flow (it defines `Flow.design_conversion`) feeds the `design` input of a flow that
cannot read what it converts. Those edges belong to the default graph (`converter_edges`). A link
from a converter binds the `design` input of every flow of the graph that the converter fits, the
second element's own included. An explicit binding of a `design` input follows the same rule
(`design_edge_refusal`).
"""

from __future__ import annotations

import difflib
from collections import deque
from functools import cache, lru_cache
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from ..design import SourceType
from ..flow import Flow, FlowSettingsException, registered_flows
from ..flow.derived import DesignConversion
from ..flow.io import declared_inputs, declared_outputs, kinds_text

__all__ = [
    "ChainEdge",
    "ChainElement",
    "ChainLink",
    "DefaultEdge",
    "DefaultGraph",
    "FlowRequest",
    "binding_points",
    "close_names",
    "complete_request",
    "converter_edges",
    "default_conversion",
    "default_graph",
    "design_edge_refusal",
    "edges",
    "fitting_outputs",
    "followers",
    "is_converter",
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
class ChainLink:
    """How one pair of adjacent elements of a request is bound.

    `through` lists the flows between the two elements, in chain order. It is empty when the
    first element feeds the second directly. `binds` lists the `(consumer flow, input, producer
    output)` triples that the link binds: the inputs of the second element for a direct link,
    and the inputs of the flows that the first element feeds for a link that passes through."""

    through: tuple[type[Flow], ...]
    binds: tuple[tuple[type[Flow], str, str], ...]


@dataclass(frozen=True)
class FlowRequest:
    """A normalized request for one flow or for adjacent flows.

    `links` has one entry for each pair of adjacent elements, so none for a single flow."""

    elements: tuple[ChainElement, ...]
    links: tuple[ChainLink, ...] = ()

    def __post_init__(self) -> None:
        if len(self.links) != max(len(self.elements) - 1, 0):
            raise ValueError(
                f"A request of {len(self.elements)} elements has {max(len(self.elements) - 1, 0)} "
                f"links, not {len(self.links)}."
            )

    @property
    def requested(self) -> type[Flow]:
        """The requested flow: the final element of the chain."""
        return self.elements[-1].flow_class

    def through(self, index: int) -> tuple[type[Flow], ...]:
        """Return the flows between element `index` and element `index + 1`, in chain order."""
        return self.links[index].through


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
            raise ChainRefusal(
                f"Flow chain element {position} is empty; use FLOW[.OUTPUT][+FLOW[.OUTPUT]...].",
                code="invalid_chain",
                chain=text,
            )
        if part.count(".") > 1:
            raise ChainRefusal(
                f"Flow chain element {position} has an invalid output qualifier {part!r}; "
                "use one exact output key after a single dot.",
                code="invalid_chain",
                chain=text,
            )
        flow_name, separator, output = part.partition(".")
        if separator and not output:
            raise ChainRefusal(
                f"Flow chain element {position} has an empty output qualifier; "
                "use an exact output key after the dot.",
                code="invalid_chain",
                chain=text,
            )
        if not flow_name:
            raise ChainRefusal(
                f"Flow chain element {position} has no flow name.",
                code="invalid_chain",
                chain=text,
            )
        try:
            flow_class = get_flow_class(flow_name)
        except FlowNotFoundError:
            raise
        canonical = flow_class.name
        if canonical in seen:
            raise ChainRefusal(
                f"Flow `{canonical}` appears more than once; a chain is a path and each flow "
                "may appear only once.",
                code="repeated_flow",
                chain=text,
            )
        seen.add(canonical)
        elements.append(ChainElement(flow_class, output if separator else None))

    if elements[-1].output is not None:
        raise ChainRefusal(
            f"The last element `{elements[-1].node}` cannot select an output because it has no "
            "following consumer.",
            code="invalid_chain",
            chain=text,
        )
    return FlowRequest(tuple(elements), validate_chain(elements))


class _EdgeFailure(Exception):
    """An adjacent pair that cannot be an edge.

    It holds the position, the reason, and whether another chain could correct the request.
    Another chain cannot correct a request whose output name does not exist: the message of
    that error already names the close matches."""

    def __init__(self, index: int, text: str, *, code: str, advise: bool = True) -> None:
        super().__init__(text)
        self.index = index
        self.text = text
        self.code = code
        self.advise = advise


def request_text(elements: Sequence[ChainElement]) -> str:
    """Return the canonical request text of `elements`: `flow[.output]` joined by `+`."""
    return "+".join(
        element.node + (f".{element.output}" if element.output else "") for element in elements
    )


def _restore_refusal(cls: type[Exception], args: tuple[Any, ...], state: dict[str, Any]) -> Any:
    """Rebuild a refusal that was pickled."""
    refusal = Exception.__new__(cls)
    Exception.__init__(refusal, *args)
    refusal.__dict__.update(state)
    return refusal


class ChainRefusal(FlowSettingsException):
    """A chain, or a link of one, that Xeda refuses.

    `code` is the code of the `xeda.errors.ChainError` that the refusal becomes. `chain` is the
    text of the chain, when the code that refuses it knows it."""

    def __init__(self, message: str, *, code: str, chain: str | None = None) -> None:
        super().__init__(message, code=code)
        self.chain = chain

    def __reduce__(self) -> tuple[Any, ...]:  # an exception crosses a process boundary by pickling
        return (_restore_refusal, (type(self), self.args, dict(self.__dict__)))


class _NoDirectEdge(ChainRefusal):
    """`match_required_inputs` found no input of the consumer that the producer can feed.

    A link that passes through other flows may still be valid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="no_edge")


def _repeated(flow: type[Flow]) -> str:
    return (
        f"Flow `{flow.name}` appears more than once; a chain is a path and each flow may appear "
        "only once."
    )


def _check_chain(elements: Sequence[ChainElement]) -> tuple[ChainLink, ...]:
    """Check a chain by the action, repeat and edge rules, without instantiation.

    This is the one chain validator. It returns the link of each pair of adjacent elements. A
    pair is a direct edge when the first element feeds a required input of the second. Else it
    passes through the default producers between them, when the default graph of the second
    element reaches the first. A pair that is neither raises `_EdgeFailure`. Every other
    refusal is a `ChainRefusal`."""
    if len(elements) <= 1:
        return ()
    seen: set[str] = set()
    for element in elements:
        if element.node in seen:
            raise ChainRefusal(
                _repeated(element.flow_class), code="repeated_flow", chain=request_text(elements)
            )
        seen.add(element.node)
    links: list[ChainLink] = []
    passed: set[type[Flow]] = set()
    for index, producer in enumerate(elements[:-1]):
        consumer = elements[index + 1]
        reason = getattr(producer.flow_class, "action_reason", None)
        if reason:
            raise ChainRefusal(
                f"Flow `{producer.node}` {reason.removeprefix('it ')} and can only end a chain.",
                code="action_not_last",
                chain=request_text(elements),
            )
        link = _link(index, producer, consumer)
        for flow in link.through:
            if flow.name in seen or flow in passed:
                raise ChainRefusal(
                    f"{_repeated(flow)} The link `{request_text(elements[index : index + 2])}` "
                    f"passes through `{flow.name}`.",
                    code="repeated_flow",
                    chain=request_text(elements),
                )
        passed.update(link.through)
        links.append(link)
    return tuple(links)


@cache
def is_converter(flow_class: type[Flow]) -> bool:
    """Whether the flow may convert a design: it defines `Flow.design_conversion`.

    A design output alone does not make a converter. Whether a converter converts anything is
    for its settings to decide (`default_conversion` is what the default settings say). The
    declarations of a class never change."""
    return getattr(flow_class.design_conversion, "__func__") is not getattr(
        Flow.design_conversion, "__func__"
    )


@cache
def default_conversion(flow_class: type[Flow]) -> DesignConversion | None:
    """Return the conversion of the flow under its default settings, or None.

    None for a flow that is no converter, and for a converter that converts nothing until a
    setting says so. Such a converter is never a default producer, and the declarations cannot
    judge it: a chain or a binding accepts it, and the final check under the agreed settings
    decides. The default settings of a class never change, and the checks of a chain ask for
    this thousands of times (building the settings is the cost), so the result is kept."""
    return flow_class.design_conversion(flow_class.Settings())


def _has_design_output(flow_class: type[Flow]) -> bool:
    return any(made.is_design for made in declared_outputs(flow_class).values())


def _link(index: int, producer: ChainElement, consumer: ChainElement) -> ChainLink:
    """Return how `producer` feeds `consumer`, or raise `_EdgeFailure` for the pair at `index`."""
    converter = is_converter(producer.flow_class)
    if converter:
        link = _converter_link(index, producer, consumer)
        if link is not None:
            return link
    try:
        matched = match_required_inputs(
            producer.flow_class, consumer.flow_class, output=producer.output
        )
        refusal = _no_edge(producer.flow_class, consumer.flow_class)
        if converter:
            # A converter feeds a `design` input through the default graph only: `_converter_link`
            # judges it, and what it refused is no direct link.
            inputs = declared_inputs(consumer.flow_class)
            matched = [pair for pair in matched if not inputs[pair[0]].is_design]
            if not matched:
                refusal = _no_conversion(producer.flow_class, consumer.flow_class)
    except _NoDirectEdge as e:
        matched, refusal = [], str(e)
    except ChainRefusal as e:
        unknown = producer.output is not None and producer.output not in declared_outputs(
            producer.flow_class
        )
        raise _EdgeFailure(index, str(e), code=e.code, advise=not unknown) from None
    if matched:
        return ChainLink((), tuple((consumer.flow_class, name, made) for name, made in matched))
    graph = default_graph(consumer.flow_class)
    through = graph.between(producer.flow_class)
    if not through or not _binding_edges(graph, producer.flow_class):
        if not converter and _has_design_output(producer.flow_class):
            refusal = _converts_no_design(producer.flow_class)
        raise _EdgeFailure(index, refusal, code="no_edge")
    try:
        points = binding_points(graph, producer.flow_class, producer.output)
    except ChainRefusal as e:
        raise _EdgeFailure(index, str(e), code=e.code, advise=True) from None
    return ChainLink(through, tuple((edge.consumer, edge.input, made) for edge, made in points))


def _converts_no_design(producer: type[Flow]) -> str:
    return f"Flow `{producer.name}` converts no design."


def _writes_and_converts(conversion: DesignConversion) -> tuple[str, str]:
    return tuple(  # type: ignore[return-value]
        "/".join(sorted(member.name for member in types))
        for types in (conversion.transforms, conversion.produces)
    )


def _no_conversion(producer: type[Flow], consumer: type[Flow]) -> str:
    """Return why the converter `producer` cannot precede `consumer`: `consumer` converts the
    design itself (a conversion is one hop), or no flow that `consumer` runs reads what the
    converter writes without reading what it converts."""
    if default_conversion(consumer) is not None:
        return (
            f"`{producer.name}+{consumer.name}`: {consumer.name} converts the design itself, "
            f"and a conversion is one hop."
        )
    conversion = default_conversion(producer)
    if conversion is None:
        if is_converter(producer):
            return (
                f"`{producer.name}+{consumer.name}`: no flow that {consumer.name} runs takes the "
                "design of a converter."
            )
        return _converts_no_design(producer)
    converts, writes = _writes_and_converts(conversion)
    return (
        f"`{producer.name}+{consumer.name}`: {producer.name} converts {converts} to {writes}, "
        f"and no flow that {consumer.name} runs reads {writes} without reading {converts} itself."
    )


def _own_refusal(
    producer: type[Flow],
    conversion: DesignConversion,
    consumer: type[Flow],
    consumer_conversion: DesignConversion | None,
) -> str | None:
    """Return why the converter `producer` (with its `conversion`) cannot supply the `design`
    input of `consumer` itself, or None. `consumer_conversion` is the conversion of `consumer`."""
    where = f"`{producer.name}` cannot supply `{consumer.name}.design`: "
    reads = consumer.reads_sources or frozenset()
    converts, writes = _writes_and_converts(conversion)
    if consumer_conversion is not None:
        return where + f"{consumer.name} converts the design itself, and a conversion is one hop."
    if not conversion.produces <= reads:
        return where + f"{producer.name} writes {writes}, and {consumer.name} does not read it."
    if not conversion.transforms - reads:
        return where + (
            f"{consumer.name} reads {converts} itself, so converting it with {producer.name} "
            "gains nothing."
        )
    return None


def design_edge_refusal(
    producer: type[Flow],
    consumer: type[Flow],
    *,
    producer_settings: Flow.Settings | None = None,
    consumer_settings: Flow.Settings | None = None,
) -> str | None:
    """Return why `producer` cannot supply the `design` input of `consumer`, or None.

    This is the one rule of the design edge, for a chain link (`consumer` the end of the chain,
    from the declarations under the default settings) and for an explicit binding of the input
    (`flows.<consumer>.inputs.design: <producer>`). A binding that a chain would refuse for lack
    of a conversion edge gets the text of the chain. The settings, when given, are the agreed
    settings of the two flows, and the rule asks for their conversions.

    A producer that declares no design output is judged by the fit of the outputs, not here.
    Rules, in order: the producer is no converter; the chain `producer+consumer` has no converter
    edge (the text of `_no_conversion`); the consumer itself cannot take it (it converts the
    design itself, which makes a conversion two hops; it does not read what the converter writes;
    it reads what the converter converts).

    A converter whose conversion is None under the default settings may convert under others, so
    the declarations accept it: its conversion, and the consumer's, are judged once the settings
    agree (the settings arguments)."""
    if not _has_design_output(producer):
        return None
    if not is_converter(producer):
        return _converts_no_design(producer)
    agreed = producer_settings is not None or consumer_settings is not None
    if not agreed and not _binding_edges(default_graph(consumer), producer):
        return _no_conversion(producer, consumer)
    conversion = producer.design_conversion(
        producer.Settings() if producer_settings is None else producer_settings
    )
    if conversion is None:
        return _converts_no_design(producer) if agreed else None
    return _own_refusal(
        producer,
        conversion,
        consumer,
        consumer.design_conversion(
            consumer.Settings() if consumer_settings is None else consumer_settings
        ),
    )


def _converter_link(index: int, producer: ChainElement, consumer: ChainElement) -> ChainLink | None:
    """Return the link of a converter to `consumer`, or None when it feeds no `design` input.

    The link binds the `design` input of every flow of the default graph of `consumer` that the
    converter fits, `consumer` itself included. It passes through the flows between the two.
    Where the default graph also uses the converter for a required file input, that input is bound
    by the ordinary chain rules."""
    graph = default_graph(consumer.flow_class)
    edges_from = [
        edge
        for edge in _binding_edges(graph, producer.flow_class)
        if declared_inputs(edge.consumer)[edge.input].is_design
    ]
    if not edges_from:
        return None
    try:
        points = binding_points(graph, producer.flow_class, producer.output)
    except ChainRefusal as e:
        raise _EdgeFailure(index, str(e), code=e.code, advise=True) from None
    return ChainLink(
        graph.between(producer.flow_class),
        tuple((edge.consumer, edge.input, made) for edge, made in points),
    )


def _valid(elements: Sequence[ChainElement]) -> bool:
    try:
        _check_chain(elements)
    except (FlowSettingsException, _EdgeFailure):
        return False
    return True


def validate_chain(elements: Sequence[ChainElement]) -> tuple[ChainLink, ...]:
    """Validate the action and repeat rules and every adjacent link, without instantiation.

    Return the link of each pair of adjacent elements. A pair that is no link is refused with
    the corrected requests that do validate, when there are any (`suggest_chains`)."""
    try:
        return _check_chain(elements)
    except _EdgeFailure as failure:
        advice = suggest_chains(elements, failure.index) if failure.advise else []
        chain = request_text(elements)
        if not advice:
            raise ChainRefusal(failure.text, code=failure.code, chain=chain) from None
        quoted = " or ".join(f"`{request_text(suggested)}`" for suggested in advice)
        raise ChainRefusal(
            f"{failure.text} Did you mean {quoted}?", code=failure.code, chain=chain
        ) from None


def suggest_chains(elements: Sequence[ChainElement], index: int) -> list[tuple[ChainElement, ...]]:
    """Return corrected requests for the pair at `index` and `index + 1` that is no link.

    Every correction is the whole request (its prefix, its suffix and the other qualifiers
    kept) and passes `_check_chain`, so no repeat, action or missing link is advertised. A
    correction is another output of the producer, for an ambiguous or unfitting qualifier. A
    missing stage needs no correction, since a link passes through the default producers
    between its flows. Nothing is executed.
    """
    producer, consumer = elements[index], elements[index + 1]
    head, tail = tuple(elements[:index]), tuple(elements[index + 2 :])
    found: list[tuple[ChainElement, ...]] = []
    for output in (None, *declared_outputs(producer.flow_class)):
        if output != producer.output:
            candidate = head + (ChainElement(producer.flow_class, output), consumer) + tail
            if candidate not in found and _valid(candidate):
                found.append(candidate)
    return found[:MAX_SUGGESTIONS]


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


def converter_edges(consumer: type[Flow]) -> list[DefaultEdge]:
    """Return the edges by which a converter flow can feed the `design` input of `consumer`.

    A registered converter (a flow that defines a conversion) has an edge when its static conversion
    (`Flow.design_conversion` under the default settings) writes only languages that `consumer`
    reads, and converts one that `consumer` cannot read. So there is no edge to a flow that reads
    the converted language itself (`bsc_sim`), or to a flow that reads none of the output
    (`ghdl_sim`), and none to a converter: a conversion is one hop. A converter that converts
    nothing under the default settings has an edge too (`default_conversion`): the declarations
    cannot tell what it converts, so it never becomes a default producer, and the final check under
    the agreed settings judges it. The edges are in the canonical order of the converter names.
    Declarations alone decide: no design is loaded."""
    declaration = declared_inputs(consumer).get("design")
    reads = consumer.reads_sources
    if (
        declaration is None
        or not declaration.is_design
        or reads is None
        or default_conversion(consumer) is not None
    ):
        return []
    found: list[DefaultEdge] = []
    registered = tuple(sorted({cls for _module, cls in registered_flows.values()}, key=_name))
    for converter, output in _converters(registered):
        if converter is consumer:
            continue  # a converter reads the design as it is
        conversion = default_conversion(converter)
        if conversion is None or (conversion.produces <= reads and conversion.transforms - reads):
            found.append(DefaultEdge(consumer, declaration.name, converter, output))
    return found


@lru_cache(maxsize=8)
def _converters(registered: tuple[type[Flow], ...]) -> tuple[tuple[type[Flow], str], ...]:
    """Return each converter of `registered` with the name of its design output."""
    return tuple(
        (flow_class, made.name)
        for flow_class in registered
        if is_converter(flow_class)
        for made in declared_outputs(flow_class).values()
        if made.is_design
    )


def _name(flow_class: type[Flow]) -> str:
    return flow_class.name


def default_graph(consumer: type[Flow]) -> DefaultGraph:
    """Return the default graph of `consumer`, from its declarations alone.

    This mirrors the discovery of implicit planning: a breadth-first walk from `consumer` over
    the inputs, in declaration order, that name a registered default producer. Each flow is
    expanded once. An edge to a flow that is already expanded is kept, so a cycle of defaults
    ends and hides no other path. The `design` input has the converters that fit it
    (`converter_edges`) in place of a default producer."""
    from .settings_layers import registered_flow

    found: list[DefaultEdge] = []
    expanded = {consumer}
    #: The flows that the walk reached as the converter of a `design` input.
    converting: set[type[Flow]] = set()
    queue = deque([consumer])
    while queue:
        current = queue.popleft()
        for declaration in declared_inputs(current).values():
            if declaration.is_design:
                # A flow that converts the design of another reads its own design as it is: one
                # hop. A converter that is reached as the producer of a file is no such flow.
                for edge in [] if current in converting else converter_edges(current):
                    found.append(edge)
                    if edge.producer not in expanded:
                        expanded.add(edge.producer)
                        converting.add(edge.producer)
                        queue.append(edge.producer)
                continue
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


def _binding_edges(graph: DefaultGraph, producer: type[Flow]) -> list[DefaultEdge]:
    """Return the edges of `graph` from `producer` to a required input, in graph order."""
    return [
        edge
        for edge in graph.edges
        if edge.producer is producer and declared_inputs(edge.consumer)[edge.input].required
    ]


def binding_points(
    graph: DefaultGraph, producer: type[Flow], output: str | None = None
) -> tuple[tuple[DefaultEdge, str], ...]:
    """Return the required inputs of the graph that a flow `producer` can feed, with the output.

    A binding point is an edge of the graph from `producer` to a required input: a chain never
    binds an optional input. The output is the one `fitting_outputs` selects for the input, as
    for the adjacent flows of a chain. `output` limits the choice to that output of `producer`.
    The result is in graph order and is empty when no edge comes from `producer`. A binding
    point that `producer` cannot feed is refused, with the point named as `consumer.input`."""
    points = _binding_edges(graph, producer)
    outputs = declared_outputs(producer)
    if points and output is not None and output not in outputs:
        raise ChainRefusal(
            f"Flow `{producer.name}` has no output `{output}`.{close_names(output, list(outputs))}",
            code="unknown_output",
        )
    chosen: list[tuple[DefaultEdge, str]] = []
    for edge in points:
        declaration = declared_inputs(edge.consumer)[edge.input]
        where = f"{edge.consumer.name}.{edge.input}"
        fitting, many = fitting_outputs(producer, declaration, output=output)
        made = f"`{producer.name}.{output}`" if output else f"`{producer.name}`"
        if len(fitting) > 1:
            raise ChainRefusal(
                f"Input `{where}` has ambiguous outputs from `{producer.name}`: "
                f"{', '.join(f'`{name}`' for name in fitting)}; qualify the producer output.",
                code="ambiguous_output",
            )
        if not fitting:
            if many:
                raise ChainRefusal(
                    f"Flow `{producer.name}` output `{many[0]}` has incompatible cardinality "
                    f"for input `{where}`, which takes one file.",
                    code="cardinality_mismatch",
                )
            made_types = {
                kind
                for name, o in outputs.items()
                if output in (None, name)
                for kind in ([kinds_text(o)] if o.is_design else [t.name for t in o.types])
            }
            raise ChainRefusal(
                f"Input `{where}` takes {kinds_text(declaration)}, "
                f"but {made} makes {'/'.join(sorted(made_types)) or 'nothing'}.",
                code="type_mismatch",
            )
        chosen.append((edge, fitting[0]))
    return tuple(chosen)


@dataclass(frozen=True)
class ChainEdge:
    """One way `producer` can precede `consumer`, directly or through the flows between them.

    It is exactly what the chain validator accepts for that pair, with the input and output
    pairs it binds."""

    producer: type[Flow]
    consumer: type[Flow]
    #: The output of the producer that a qualified request names; None when the unqualified
    #: request is valid.
    output: str | None
    #: The `(input, output)` pairs the link binds, in the order of `ChainLink.binds`.
    binds: tuple[tuple[str, str], ...]
    #: Whether a produced or accepted kind is a union that the settings narrow, so that the
    #: final plan decides.
    target_dependent: bool
    #: The flows between `producer` and `consumer`, in chain order; empty for a direct link.
    through: tuple[type[Flow], ...] = ()
    #: The flow that takes each input of `binds`: the consumer for a direct link.
    bound_flows: tuple[type[Flow], ...] = ()


def edges(producer: type[Flow], consumer: type[Flow]) -> list[ChainEdge]:
    """Return the ways `producer+consumer` is a valid link, direct ones before the others.

    That is the unqualified request, else one qualified request per output of the producer that
    makes it valid. The chain validator itself judges, on declarations alone. A link is direct
    when the producer feeds the consumer, else it passes through the flows between them."""
    outputs = declared_outputs(producer)
    found: list[ChainEdge] = []
    for output in (None, *outputs):
        try:
            (link,) = _check_chain((ChainElement(producer, output), ChainElement(consumer)))
        except (FlowSettingsException, _EdgeFailure):
            continue
        found.append(
            ChainEdge(
                producer,
                consumer,
                output,
                tuple((name, made) for _flow, name, made in link.binds),
                target_dependent=any(
                    len(outputs[made].types) > 1 or len(declared_inputs(flow)[taken].types) > 1
                    for flow, taken, made in link.binds
                ),
                through=link.through,
                bound_flows=tuple(flow for flow, _taken, _made in link.binds),
            )
        )
        if output is None:
            break
    return sorted(found, key=lambda edge: bool(edge.through))


def followers(producer: type[Flow], candidates: Sequence[type[Flow]]) -> list[ChainEdge]:
    """Return the links from `producer` to each of `candidates`: the direct links, then the
    ones that pass through other flows, each group in candidate order."""
    found = [edge for consumer in candidates for edge in edges(producer, consumer)]
    return sorted(found, key=lambda edge: bool(edge.through))


def predecessors(consumer: type[Flow], candidates: Sequence[type[Flow]]) -> list[ChainEdge]:
    """Return the links from each of `candidates` to `consumer`: the direct links, then the
    ones that pass through other flows, each group in candidate order."""
    found = [edge for producer in candidates for edge in edges(producer, consumer)]
    return sorted(found, key=lambda edge: bool(edge.through))


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
    takes a required input and has a compatible link from the flow before it (a direct edge, or a
    pass-through of the default producers between them) that does not pass through a flow of the
    request. The flows that follow directly come first, then the ones that follow through other
    flows, each group in candidate order. In a qualifier
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
    direct: list[str] = []
    passing: list[str] = []
    for flow_class in candidates:
        spellings = [
            n for n in (flow_class.name, *flow_class.aliases) if n.lower().startswith(wanted)
        ]
        if not spellings:
            continue
        passes = False
        if head:
            try:
                passes = bool(_check_chain((*head, ChainElement(flow_class)))[-1].through)
            except (FlowSettingsException, _EdgeFailure):
                continue
        spelling = flow_class.name if flow_class.name in spellings else spellings[0]
        (passing if passes else direct).append(typed + spelling)
    return direct + passing


def fitting_outputs(
    producer: type[Flow],
    declaration: Any,
    *,
    output: str | None = None,
    accepted: Sequence[SourceType] | None = None,
    selected_output_types: Mapping[str, Sequence[SourceType]] | None = None,
) -> tuple[list[str], list[str]]:
    """Return the outputs of `producer` that can supply the input `declaration`.

    This is the one edge predicate, for chain links and explicit bindings alike. The result
    is a pair of lists. The second holds the outputs of a fitting kind that cannot supply the
    input, because they are many-valued and the input takes one file.

    The kind of value decides first: the design input fits the design output of a converter and
    nothing else, and an input of files never fits a design output. An output of files fits when
    the types it can make are nonempty and a subset of the types the input accepts (an output that
    may be of a kind the input cannot take does not fit), and a many-valued output feeds only a
    many-valued input. `output` restricts the question to one key. When several outputs fit and the default
    producer of the input is `producer`, the declared default output decides.
    """
    takes = tuple(declaration.types if accepted is None else accepted)
    fitting: list[str] = []
    many: list[str] = []
    for name, made in declared_outputs(producer).items():
        if output is not None and name != output:
            continue
        if made.is_design != declaration.is_design:
            continue
        if declaration.is_design:
            if is_converter(producer):
                fitting.append(name)
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
    a chain link is not an opt-in for them.
    """
    inputs = declared_inputs(consumer)
    outputs = declared_outputs(producer)
    if output is not None and output not in outputs:
        raise ChainRefusal(
            f"Flow `{producer.name}` has no output `{output}`.{close_names(output, list(outputs))}",
            code="unknown_output",
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
            raise ChainRefusal(
                f"Flow `{consumer.name}` input `{input_name}` has ambiguous outputs from "
                f"`{producer.name}`: {', '.join(f'`{name}`' for name in candidates)}; "
                "qualify the producer output.",
                code="ambiguous_output",
            )
        if candidates:
            matches.append((input_name, candidates[0]))

    if not matches and any(declaration.required for declaration in inputs.values()):
        if cardinality_mismatches:
            input_name, output_name = cardinality_mismatches[0]
            raise _NoDirectEdge(
                f"Flow `{producer.name}` output `{output_name}` has incompatible cardinality "
                f"for `{consumer.name}` input `{input_name}`."
            )
        raise _NoDirectEdge(_no_edge(producer, consumer))
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
            ", ".join(f"{name} ({kinds_text(declared)})" for name, declared in declarations.items())
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
