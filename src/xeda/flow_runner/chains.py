"""Pure request parsing and declared-I/O checks for flow chains.

The request grammar is deliberately separate from graph resolution: parsing canonicalizes names
and outputs, while the edge predicate uses declared or target-selected types without constructing
flows or probing tools.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from ..design import SourceType
from ..flow import Flow, FlowSettingsException
from ..flow.io import declared_inputs, declared_outputs, is_declared

__all__ = [
    "ChainElement",
    "FlowRequest",
    "fitting_outputs",
    "match_required_inputs",
    "parse_request",
    "validate_chain",
]


@dataclass(frozen=True)
class ChainElement:
    """One canonical flow and, optionally, the output selected for its next edge."""

    flow_class: type[Flow]
    output: str | None = None

    @property
    def node(self) -> str:
        """The canonical request-node name (P3 currently has one node per flow)."""
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


def validate_chain(elements: Sequence[ChainElement]) -> None:
    """Validate declaration/action boundaries and every adjacent edge, without instantiation."""
    if len(elements) <= 1:
        return
    for element in elements:
        if not is_declared(element.flow_class):
            raise FlowSettingsException(
                f"Flow `{element.node}` has no declared I/O and can only be run alone."
            )
    for index, producer in enumerate(elements[:-1]):
        consumer = elements[index + 1]
        if getattr(producer.flow_class, "action_reason", None):
            raise FlowSettingsException(
                f"Flow `{producer.node}` is an action and can only appear at the end of a chain."
            )
        if not match_required_inputs(
            producer.flow_class, consumer.flow_class, output=producer.output
        ):
            raise FlowSettingsException(
                f"Flow `{producer.node}` has no compatible output for a required input of "
                f"`{consumer.node}`."
            )


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
        suggestions = difflib.get_close_matches(output, list(outputs), n=3, cutoff=0.5)
        hint = (
            f" Did you mean {', '.join(f'`{name}`' for name in suggestions)}?"
            if suggestions
            else ""
        )
        raise FlowSettingsException(f"Flow `{producer.name}` has no output `{output}`.{hint}")

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
        raise FlowSettingsException(
            f"Flow `{producer.name}` has no compatible output for a required input of "
            f"`{consumer.name}`."
        )
    return matches
