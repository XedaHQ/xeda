"""Pure request parsing and declared-I/O checks for flow chains.

The request grammar is deliberately separate from graph resolution: parsing canonicalizes names
and outputs, while the edge predicate uses declared or target-selected types without constructing
flows or probing tools.
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
    "FlowRequest",
    "close_names",
    "complete_request",
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
        """The canonical request-node name (a request has one node per flow)."""
        return self.flow_class.name


@dataclass(frozen=True)
class FlowRequest:
    """A normalized single-flow or adjacent-flow request."""

    elements: tuple[ChainElement, ...]

    @property
    def requested(self) -> type[Flow]:
        """The requested flow is the final chain element."""
        return self.elements[-1].flow_class


def parse_request(text: str) -> FlowRequest:
    """Parse ``flow[.output][+flow[.output]...]`` and canonicalize every flow name.

    Flow classes are looked up lazily here to keep the request module independent of launcher
    initialization. The parser does not construct a flow instance.
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
    """An adjacent pair that cannot be an edge: its position, the reason, and whether the
    request can be corrected by another chain (not when an output name does not exist: its
    own message already names the close matches)."""

    def __init__(self, index: int, text: str, *, advise: bool = True) -> None:
        super().__init__(text)
        self.index = index
        self.text = text
        self.advise = advise


def request_text(elements: Sequence[ChainElement]) -> str:
    """The canonical request text of `elements`: `flow[.output]` joined by `+`."""
    return "+".join(
        element.node + (f".{element.output}" if element.output else "") for element in elements
    )


def _check_chain(elements: Sequence[ChainElement]) -> None:
    """The one chain validator: action, repeat and edge rules, without instantiation.
    A pair that is no edge is an `_EdgeFailure`; every other refusal a `FlowSettingsException`."""
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
    """Validate declaration/action/repeat boundaries and every adjacent edge, without
    instantiation. A pair that is no edge is refused with the corrected requests that do
    validate, when there are any (`suggest_chains`)."""
    try:
        _check_chain(elements)
    except _EdgeFailure as failure:
        advice = suggest_chains(elements, failure.index) if failure.advise else []
        if not advice:
            raise FlowSettingsException(failure.text) from None
        quoted = " or ".join(f"`{request_text(chain)}`" for chain in advice)
        raise FlowSettingsException(f"{failure.text} Did you mean {quoted}?") from None


def suggest_chains(elements: Sequence[ChainElement], index: int) -> list[tuple[ChainElement, ...]]:
    """Corrected requests for the pair at `index` and `index + 1` that is no edge. Every one is
    the whole request -- its prefix, its suffix and the other qualifiers kept -- and passes
    `_check_chain`, so no repeat, action or missing edge is ever advertised.

    Two corrections, nothing else: another output of the producer (an ambiguous or unfitting
    qualifier), and flows inserted between the pair along **required default-producer edges**
    only (`_default_routes`: a bounded walk back from the consumer through the producers its
    inputs name). There is no search over all flows and nothing is executed.
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
            # the producer's own qualifier first; unqualified only when that one cannot work
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
    """The flows between `producer` and `consumer`, in chain order, for each way of reaching
    `producer` by walking back from `consumer` over the default producers its **required**
    inputs name. Breadth-first with a visited set (so a cycle of defaults ends), shortest first;
    only a hint, since the whole candidate chain is validated afterwards."""
    from .settings_layers import registered_flow

    routes: list[tuple[type[Flow], ...]] = []
    visited = {consumer}
    queue: deque[tuple[type[Flow], tuple[type[Flow], ...]]] = deque([(consumer, ())])
    while queue:
        current, between = queue.popleft()
        for declaration in declared_inputs(current).values():
            if not declaration.required or declaration.producer is None:
                continue
            upstream = registered_flow(declaration.producer)
            if upstream is None:
                continue
            if upstream is producer:
                if between and between not in routes:
                    routes.append(between)
            elif upstream not in visited:
                visited.add(upstream)
                queue.append((upstream, (upstream, *between)))
    return routes


@dataclass(frozen=True)
class ChainEdge:
    """One way `producer` can directly precede `consumer`: exactly what the chain validator
    accepts for that adjacency, with the input/output pairs it binds."""

    producer: type[Flow]
    consumer: type[Flow]
    #: the producer's output a qualified request names; None when the unqualified one is valid
    output: str | None
    binds: tuple[tuple[str, str], ...]
    #: a produced or accepted kind is a union the target narrows, so the final plan decides
    target_dependent: bool


def edges(producer: type[Flow], consumer: type[Flow]) -> list[ChainEdge]:
    """The ways `producer+consumer` is a valid adjacency: the unqualified one, else one
    qualified request per output of the producer that makes it valid. Judged by the chain
    validator itself, on declarations alone."""
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
    """The edges from `producer` to each of `candidates`, in candidate order."""
    return [edge for consumer in candidates for edge in edges(producer, consumer)]


def predecessors(consumer: type[Flow], candidates: Sequence[type[Flow]]) -> list[ChainEdge]:
    """The edges from each of `candidates` to `consumer`, in candidate order."""
    return [edge for producer in candidates for edge in edges(producer, consumer)]


def _lenient_element(token: str) -> ChainElement | None:
    """`flow[.output]` as typed so far, or None when it names no flow or is malformed."""
    from .default_runner import FlowNotFoundError, get_flow_class

    name, dot, output = token.partition(".")
    if not name or "." in output or (dot and not output):
        return None
    try:
        return ChainElement(get_flow_class(name), output if dot else None)
    except (FlowNotFoundError, FlowSettingsException):
        return None


def complete_request(incomplete: str, candidates: Sequence[type[Flow]]) -> list[str]:
    """Completions of an unfinished request, each the **whole current token**: what was typed
    before the last `+`, exactly as typed, followed by the completed element.

    After a `+` only flows that the validator accepts as the next element are offered (so none
    repeats, none follows an action, none is undeclared and none lacks a compatible edge); in a
    qualifier (`nextpnr.`) the producer's outputs that lead to some follower. Static: it reads
    declarations only, loads no design, constructs no flow and probes no tool.
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
    """The one edge predicate (PD3), for chain adjacency and explicit bindings alike: the
    outputs of `producer` that can supply the input `declaration`, and those of a fitting kind
    that cannot because they are many-valued and the input takes one file.

    An output fits when the types it can make are nonempty and a **subset** of the types the
    input accepts (an output that may be of a kind the input cannot take does not fit), and a
    many-valued output feeds only a many-valued input. `output` restricts the question to one
    key. When several fit and the input's own default producer is `producer`, its declared
    default output decides.
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
    """Return all required compatible ``(input, output)`` pairs in consumer declaration order.

    A produced type set must be nonempty and a subset of the accepted input types. A many-valued
    output can feed only a many-valued input. Optional inputs are intentionally excluded because
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
    """` Did you mean `x`?` for the declared names close to `name`, else what is declared (for
    an input or output name that is no name of the flow), else nothing."""
    close = difflib.get_close_matches(name, list(declared), n=3, cutoff=0.5)
    if close:
        return f" Did you mean {', '.join(f'`{n}`' for n in close)}?"
    return f" It declares: {', '.join(f'`{n}`' for n in declared)}." if declared else ""


def _no_edge(producer: type[Flow], consumer: type[Flow]) -> str:
    """Why `producer` cannot be chained before `consumer`: what each takes and makes."""

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
    return (
        f"Flow `{producer.name}` has no compatible output for a required input of "
        f"`{consumer.name}`: `{consumer.name}` takes {listed(required)}; `{producer.name}` "
        f"makes {listed(declared_outputs(producer))}."
    )
