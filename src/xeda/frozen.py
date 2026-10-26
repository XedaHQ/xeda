"""Immutable copies of nested values.

A record holds tables and lists that a caller must not change by accident. `freeze` copies a value
into a form that nothing can change: a table becomes a `FrozenDict` and a list becomes a tuple.
`thaw` makes plain tables and lists again, for a JSON document.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

__all__ = ["FrozenDict", "freeze", "thaw"]


class FrozenDict(Mapping[str, Any]):
    """A mapping that nobody can change.

    It reads like a `dict`. It has no method that changes it, so an assignment into it raises
    `TypeError`. It has a hash when all its values have one, and it can be pickled. It equals
    every mapping that has the same items."""

    __slots__ = ("_data",)

    def __init__(
        self, data: Mapping[str, Any] | Iterator[tuple[str, Any]] | None = None, /
    ) -> None:
        self._data: dict[str, Any] = {} if data is None else dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))


def freeze(value: Any) -> Any:
    """Return a deep copy of `value` that nothing can change.

    A mapping becomes a `FrozenDict` and a list or tuple becomes a tuple, at every depth. Any
    other value is returned as it is. The copy shares nothing with `value`, so a later change of
    `value` never reaches it."""
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return a copy of `value` made of plain tables and lists: the inverse of `freeze`."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value
