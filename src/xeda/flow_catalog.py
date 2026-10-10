"""Lightweight flow names and generated chain transitions for shell completion."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import TypedDict


class _FlowRecord(TypedDict):
    aliases: list[str]
    class_name: str
    followers: dict[str, list[str]]


@lru_cache(maxsize=1)
def _builtin_flows() -> list[tuple[str, _FlowRecord]]:
    """Read the generated built-in catalog without importing any flow models."""
    path = Path(__file__).with_name("flow_catalog.json")
    with path.open(encoding="utf-8") as stream:
        data: dict[str, _FlowRecord] = json.load(stream)
    return list(data.items())


def flow_names() -> list[str]:
    """Return canonical flow names and aliases without importing flow models."""
    names = {
        name for canonical, record in _builtin_flows() for name in (canonical, *record["aliases"])
    }

    flow_module = sys.modules.get("xeda.flow.flow")
    registered_flows = getattr(flow_module, "registered_flows", {})
    for _, flow_class in registered_flows.values():
        names.add(flow_class.name)
        names.update(flow_class.aliases)

    return sorted(names)


def _resolve_builtin(token: str) -> tuple[str, _FlowRecord] | None:
    """Resolve a complete token as the runtime flow resolver does for built-ins."""
    wanted = token.strip().replace("-", "_")
    lowered = wanted.lower()
    records = _builtin_flows()
    for canonical, record in records:
        if lowered in {
            name.lower() for name in (canonical, record["class_name"], *record["aliases"])
        }:
            return canonical, record
    class_name = "".join(word.title() for word in wanted.split("_"))
    return next(
        (
            (canonical, record)
            for canonical, record in records
            if record["class_name"] == class_name
        ),
        None,
    )


def _eligible_followers(
    records: dict[str, _FlowRecord], producer: str, output: str | None, used: set[str]
) -> list[str]:
    """Return declared followers that keep the path free of repeated flows."""
    return [
        follower
        for follower in records[producer]["followers"].get(output or "", [])
        if follower not in used
    ]


def complete_builtin_request(incomplete: str) -> list[str]:
    """Complete a built-in flow chain from the checked-in transition catalog."""
    records = dict(_builtin_flows())
    head_text, plus, partial = incomplete.rpartition("+")
    head: list[tuple[str, str | None]] = []
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

        seen: set[str] = set()
        for index, (canonical, output) in enumerate(head):
            if canonical in seen:
                return []
            if index + 1 < len(head):
                consumer = head[index + 1][0]
                if consumer not in _eligible_followers(
                    records, canonical, output, seen | {canonical}
                ):
                    return []
            seen.add(canonical)

    typed = head_text + plus
    name, dot, output = partial.partition(".")
    if dot:
        if "." in output:
            return []
        resolved = _resolve_builtin(name)
        if resolved is None:
            return []
        canonical, record = resolved
        seen = {flow for flow, _ in head}
        if canonical in seen:
            return []
        if head:
            producer, selected = head[-1]
            if canonical not in _eligible_followers(records, producer, selected, seen):
                return []
        used = seen | {canonical}
        return [
            f"{typed}{name}.{key}"
            for key in record["followers"]
            if key and key.startswith(output) and _eligible_followers(records, canonical, key, used)
        ]

    wanted = name.lower().replace("-", "_")
    found: list[str] = []
    for canonical, record in _builtin_flows():
        spellings = [
            spelling
            for spelling in (canonical, *record["aliases"])
            if spelling.lower().startswith(wanted)
        ]
        if not spellings or canonical in {flow for flow, _ in head}:
            continue
        if head:
            producer, selected = head[-1]
            if canonical not in _eligible_followers(
                records, producer, selected, {flow for flow, _ in head}
            ):
                continue
        spelling = canonical if canonical in spellings else spellings[0]
        found.append(typed + spelling)
    return found


__all__ = ["complete_builtin_request", "flow_names"]
