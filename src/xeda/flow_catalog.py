"""Lightweight flow names, settings metadata, and chain transitions for shell completion."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, NotRequired, TypedDict


class _FlowRecord(TypedDict):
    #: The flows that can follow the flow, by the output the request names ("" for none), in the
    #: order the runtime offers them: the direct followers, then the ones reached through a route.
    followers: dict[str, list[str]]
    #: For a follower reached through other flows, the flows between, in chain order. A catalog
    #: without the key holds direct followers alone.
    routes: NotRequired[dict[str, dict[str, list[str]]]]
    #: Accepted settings paths and their compact completion metadata.
    settings: NotRequired[dict[str, dict[str, Any]]]
    #: Canonical flows that can appear as potential transitive default producers.
    default_producers: NotRequired[list[str]]


@lru_cache(maxsize=1)
def _builtin_flows() -> list[tuple[str, _FlowRecord]]:
    """Read the generated built-in catalog without importing any flow models."""
    path = Path(__file__).with_name("flow_catalog.json")
    with path.open(encoding="utf-8") as stream:
        data: dict[str, _FlowRecord] = json.load(stream)
    return list(data.items())


def flow_names() -> list[str]:
    """Return the names of the flows without importing flow models."""
    names = {canonical for canonical, _ in _builtin_flows()}

    flow_module = sys.modules.get("xeda.flow.flow")
    registered_flows = getattr(flow_module, "registered_flows", {})
    for _, flow_class in registered_flows.values():
        names.add(flow_class.name)

    return sorted(names)


def _resolve_builtin(token: str) -> tuple[str, _FlowRecord] | None:
    """Resolve a complete token as the runtime flow resolver does for built-ins: by the name of
    the flow."""
    return next(
        ((canonical, record) for canonical, record in _builtin_flows() if canonical == token), None
    )


def _eligible_followers(
    records: dict[str, _FlowRecord], producer: str, output: str | None, used: set[str]
) -> list[tuple[str, tuple[str, ...]]]:
    """Return the followers that keep the chain free of repeated flows, each with its route.

    `used` holds the flows of the chain so far and the flows its links pass through. A follower
    is not eligible if it is in `used`, or if its route meets `used`."""
    record = records[producer]
    routes = record.get("routes", {}).get(output or "", {})
    eligible = []
    for follower in record["followers"].get(output or "", []):
        route = tuple(routes.get(follower, ()))
        if follower not in used and used.isdisjoint(route):
            eligible.append((follower, route))
    return eligible


def _walk_head(
    records: dict[str, _FlowRecord], head: list[tuple[str, str | None]]
) -> set[str] | None:
    """Return the flows that a typed chain uses: its elements and the flows its links pass
    through. Return None when the chain is no valid request."""
    elements = {flow for flow, _ in head}
    if len(elements) != len(head):
        return None
    used = set(elements)
    for (producer, output), (consumer, _) in zip(head, head[1:]):
        # a route meets no element of the chain and no route of another link
        known = dict(_eligible_followers(records, producer, output, used - {consumer}))
        if consumer not in known:
            return None
        used.update(known[consumer])
    return used


def complete_builtin_request(incomplete: str) -> list[str]:
    """Complete a built-in flow chain from the checked-in transition catalog."""
    records = dict(_builtin_flows())
    head_text, plus, partial = incomplete.rpartition("+")
    head: list[tuple[str, str | None]] = []
    used: set[str] = set()
    if plus:
        for token in head_text.split("+"):
            name, dot, qualifier = token.partition(".")
            if not name or (dot and (not qualifier or "." in qualifier)):
                return []
            resolved = _resolve_builtin(name)
            if resolved is None:
                return []
            canonical, _ = resolved
            head.append((canonical, qualifier if dot else None))
        walked = _walk_head(records, head)
        if walked is None:
            return []
        used = walked

    typed = head_text + plus
    name, dot, output = partial.partition(".")
    if dot:
        if "." in output:
            return []
        resolved = _resolve_builtin(name)
        if resolved is None:
            return []
        canonical, record = resolved
        if canonical in used:
            return []
        if head:
            producer, selected = head[-1]
            linked = dict(_eligible_followers(records, producer, selected, used - {canonical}))
            if canonical not in linked:
                return []
            used = used | set(linked[canonical])
        used = used | {canonical}
        return [
            f"{typed}{name}.{key}"
            for key in record["followers"]
            if key and key.startswith(output) and _eligible_followers(records, canonical, key, used)
        ]

    def spelled(canonical: str) -> str | None:
        return typed + canonical if canonical.startswith(name) else None

    if head:
        producer, selected = head[-1]
        found = [
            (spelling, bool(route))
            for follower, route in _eligible_followers(records, producer, selected, used)
            if (spelling := spelled(follower)) is not None
        ]
        return [s for s, passes in found if not passes] + [s for s, passes in found if passes]
    return [
        spelling
        for canonical, _ in _builtin_flows()
        if (spelling := spelled(canonical)) is not None
    ]


__all__ = ["complete_builtin_request", "flow_names"]
