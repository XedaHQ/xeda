"""Immutable copies of nested values.

A record holds tables and lists that a caller must not change by accident. `freeze` copies a value
into a form that nothing can change: a table becomes a `FrozenDict` and a list becomes a tuple.
`thaw` makes plain tables and lists again, for a JSON document.
"""

from __future__ import annotations

import copy
import numbers
from collections.abc import Iterator, Mapping
from typing import Any, TypeVar

import pint
from pydantic import BaseModel
from pydantic_core import core_schema

__all__ = ["Frozen", "FrozenDict", "FrozenModel", "freeze", "quantity_texts", "thaw"]


_K = TypeVar("_K")
_V = TypeVar("_V")


class FrozenDict(Mapping[_K, _V]):
    """A mapping that nobody can change.

    It reads like a `dict`. It has no method that changes it, so an assignment into it raises
    `TypeError`. It has a hash when all its values have one, and it can be pickled. It equals
    every mapping that has the same items."""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[_K, _V] | Iterator[tuple[_K, _V]] | None = None, /) -> None:
        self._data: dict[_K, _V] = {} if data is None else dict(data)

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))


_FROZEN_QUANTITY_CLASSES: dict[type, type] = {}
_IN_PLACE_METHODS = (
    "ito",
    "ito_base_units",
    "ito_reduced_units",
    "ito_root_units",
    "ito_preferred",
    "ito_unprefixed",
    "__setitem__",
)
_CONVERSIONS = (
    "to",
    "to_base_units",
    "to_reduced_units",
    "to_root_units",
    "to_compact",
    "to_preferred",
    "to_unprefixed",
    "__neg__",
    "__pos__",
    "__abs__",
    "__round__",
)
_OPERATORS = ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow", "divmod")


def _build(kind: type, magnitude: Any, units: Any, registry: Any) -> Any:
    """Return a quantity of class `kind` that holds exactly these parts.

    It does not run the constructor of the class, which converts the magnitude as the current
    application registry says (`force_ndarray` makes an array) and attaches the quantity to that
    registry. The parts are those of a quantity that already exists, so there is nothing to
    convert."""
    quantity: Any = object.__new__(kind)
    object.__setattr__(quantity, "_magnitude", magnitude)
    object.__setattr__(quantity, "_units", units)
    if not hasattr(kind, "_REGISTRY"):  # a quantity of the application registry holds it itself
        object.__setattr__(quantity, "_REGISTRY", registry)
    return quantity


def _plain_quantity(quantity: Any) -> Any:
    """Return a copy of a pint quantity that can change in place, of the class and the registry
    that it came from."""
    kind = type(quantity)
    kind = getattr(kind, "_unfrozen", kind)
    return _build(kind, copy.copy(quantity.magnitude), quantity._units, quantity._REGISTRY)


def _unpickle_frozen_quantity(magnitude: Any, units: Any) -> Any:
    """Make a frozen quantity of the application registry again, as a pickle holds it."""
    registry = pint.get_application_registry().get()
    return _build(_frozen_class(pint.Quantity), magnitude, units, registry)


def _frozen_class(base: type) -> type:
    """Return the class of the frozen quantities that derive from `base`."""
    kind = _FROZEN_QUANTITY_CLASSES.get(base)
    if kind is None:
        namespace: dict[str, Any] = {
            "_unfrozen": base,
            "__reduce__": lambda self: (_unpickle_frozen_quantity, (self.magnitude, self._units)),
            "__copy__": lambda self: _frozen_quantity(self),
            "__deepcopy__": lambda self, memo: _frozen_quantity(
                self, copy.deepcopy(self.magnitude, memo)
            ),
            # pint builds a new quantity with `self.__class__(...)` (`real`, `imag`, `T`, ...): a
            # new quantity is a plain one
            "__new__": lambda cls, *args, **kwargs: base.__new__(base, *args, **kwargs),
            "__setattr__": _refuse_assignment,
            "__delattr__": _refuse_assignment,
            # pint caches the dimensionality in the quantity on first use
            "dimensionality": property(lambda self: _plain_quantity(self).dimensionality),
        }
        for name in _IN_PLACE_METHODS:
            if hasattr(base, name):
                namespace[name] = _forbidden(name)
        for name in _CONVERSIONS:
            if hasattr(base, name):
                namespace[name] = _on_plain(getattr(base, name))
        for name in _OPERATORS:
            if hasattr(base, f"__{name}__"):
                namespace[f"__{name}__"] = _on_plain(getattr(base, f"__{name}__"))
            if hasattr(base, f"__r{name}__"):  # `2 * x` and `y * x` reach `x` first
                namespace[f"__r{name}__"] = _reflected(name, getattr(base, f"__r{name}__"))
            if hasattr(base, f"__i{name}__"):  # `x += y` makes a new quantity
                namespace[f"__i{name}__"] = _on_plain(getattr(base, f"__{name}__"))
        kind = type(f"Frozen{base.__name__}", (base,), namespace)
        _FROZEN_QUANTITY_CLASSES[base] = kind
    return kind


def _frozen_quantity(quantity: Any, magnitude: Any = None) -> Any:
    """Return a quantity equal to `quantity` that nothing changes in place.

    Its class derives from the class of `quantity`, so it is a quantity of the same registry and
    it reads, compares and hashes as one. It refuses every assignment to and deletion of an
    attribute, so no method of pint, present or future, can change it: `ito` and its relatives
    raise `TypeError` with a message that says what to use. Every operator and conversion works
    on a plain copy and returns a plain quantity, which the caller may change: it is a new value,
    and the stored one stays as it is. A copy is frozen too. The magnitude must be a number: a
    setting is a scalar, and an array could change inside the quantity."""
    base = getattr(type(quantity), "_unfrozen", type(quantity))  # freezing twice is no change
    if magnitude is None:
        magnitude = quantity.magnitude
        if not isinstance(magnitude, numbers.Number):
            raise ValueError(
                f"a quantity must hold a number, not {type(magnitude).__name__}: "
                f"{quantity!r} is not allowed"
            )
        magnitude = copy.copy(magnitude)
    return _build(_frozen_class(base), magnitude, quantity._units, quantity._REGISTRY)


def _refuse_assignment(self: Any, name: str, *value: Any) -> None:
    raise TypeError(
        f"a quantity in a frozen value cannot change: `{name}` is not allowed. "
        "Use a method that returns a new quantity, such as `to`"
    )


def _forbidden(name: str) -> Any:
    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(
            f"a quantity in a frozen value cannot change in place: `{name}` is not allowed. "
            "Use the method that returns a new quantity, such as `to`"
        )

    return method


def _reflected(name: str, operation: Any) -> Any:
    """Return the reflected method `__r<name>__` of a frozen quantity.

    Python tries it before the method of a left operand of the base class, so it does what that
    method does, on plain copies."""

    def method(self: Any, other: Any, *args: Any) -> Any:
        if isinstance(other, pint.Quantity):
            plain = _plain_quantity(other)
            return getattr(type(plain), f"__{name}__")(plain, _plain_quantity(self), *args)
        return operation(_plain_quantity(self), other, *args)

    return method


def _on_plain(operation: Any) -> Any:
    """Return a method that runs `operation` on plain copies of the quantity and of a frozen
    argument. Pint changes a quantity in place inside some operators (`auto_reduce_dimensions`,
    offset units), and a frozen quantity must neither raise there nor change."""

    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        plain = [_plain_quantity(arg) if hasattr(type(arg), "_unfrozen") else arg for arg in args]
        return operation(_plain_quantity(self), *plain, **kwargs)

    return method


def freeze(value: Any) -> Any:
    """Return a deep copy of `value` that nothing can change.

    A mapping becomes a `FrozenDict` and a list or tuple becomes a tuple, at every depth. A pint
    quantity becomes a quantity that no method changes in place. Any other value is returned as it
    is. The copy shares nothing with `value`, so a later change of
    `value` never reaches it."""
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, pint.Quantity):
        return _frozen_quantity(value)  # `ito` changes a quantity in place
    return value


def thaw(value: Any) -> Any:
    """Return a copy of `value` made of plain tables, lists and quantities: the inverse of `freeze`."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    if isinstance(value, pint.Quantity):
        return _plain_quantity(value)
    return value


def quantity_texts(value: Any) -> Any:
    """Return `value` with every pint quantity written as quantity text (`10 ns`)."""
    from .units import pint_quantity_text

    if isinstance(value, Mapping):
        return {key: quantity_texts(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [quantity_texts(item) for item in value]
    return pint_quantity_text(value) if isinstance(value, pint.Quantity) else value


def _dump(value: Any, info: core_schema.SerializationInfo) -> Any:
    """Serialize a frozen value: plain tables and lists. A JSON dump also writes a pint quantity,
    which a raw settings table can hold before validation, as text."""
    return quantity_texts(value) if info.mode_is_json() else thaw(value)


def _serialization() -> Any:
    """Return the serializer of a frozen value: a dump holds plain tables and lists."""
    return core_schema.plain_serializer_function_ser_schema(
        _dump, info_arg=True, return_schema=core_schema.any_schema()
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
