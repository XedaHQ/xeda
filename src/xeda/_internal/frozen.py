"""Immutable copies of nested values.

A record holds tables and lists that a caller must not change by accident. `freeze` copies a value
into a form that nothing can change: a table becomes a `FrozenDict` and a list becomes a tuple.
`thaw` makes plain tables and lists again, for a JSON document.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from pydantic import BaseModel
from pydantic_core import core_schema

__all__ = ["Frozen", "FrozenDict", "FrozenModel", "freeze", "thaw"]


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


def _serialization() -> Any:
    """Return the serializer of a frozen value: a dump holds plain tables and lists."""
    return core_schema.plain_serializer_function_ser_schema(
        thaw, return_schema=core_schema.any_schema()
    )


class Frozen:
    """A field annotation: the field is validated as its type says, then frozen.

    `Annotated[Mapping[str, Any], Frozen()]` is a field that takes a table and holds a
    `FrozenDict`. Every table and list inside it is frozen too. The JSON schema of the field is
    that of the type. A dump of the model holds plain tables and lists."""

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> Any:
        return core_schema.no_info_after_validator_function(
            freeze, handler(source), serialization=_serialization()
        )

    def __hash__(self) -> int:
        return hash(type(self))

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self)


class FrozenModel:
    """A field annotation: a model validates the value, and the field holds the model's fields
    as a `FrozenDict`.

    `Annotated[Mapping[str, Any], FrozenModel(Clock)]` takes anything that `Clock` takes (a table,
    a clock, or a form that `Clock` reads), reports a mistake at the place that `Clock` does, and
    holds `freeze(clock.model_dump())`. The JSON schema of the field is that of the model."""

    def __init__(self, model: type[BaseModel]) -> None:
        self.model = model

    @property
    def models(self) -> tuple[type[BaseModel], ...]:
        """The models that the annotation validates a value with."""
        return (self.model,)

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> Any:
        return core_schema.no_info_after_validator_function(
            lambda value: freeze(value.model_dump()),
            handler(self.model),
            serialization=_serialization(),
        )

    def __hash__(self) -> int:
        return hash(self.model)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FrozenModel) and other.model is self.model
