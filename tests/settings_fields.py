"""Walk the fields of settings models, for the tests that cover every number or every quantity.

A settings model nests other models, in a field of its own, in a `dict`, in a `list`, behind an
`Optional` or a `Union`. These functions find the fields that hold numbers and quantities
wherever they are, and they build and follow the input that reaches one.
"""

import numbers as number_types
import typing
from collections.abc import Iterable, Mapping
from enum import Enum
from inspect import get_annotations
from pathlib import PurePath
from types import UnionType
from typing import Annotated, Any, Literal, NamedTuple, Union

from pydantic import BaseModel

from xeda.units import Quantity

#: A field is named by the class that defines it and its name in that class.
FieldKey = tuple[str, str]


class Shape(NamedTuple):
    """What an annotation holds, after `Optional`, `Union`, `Annotated`, `Literal` and the
    arguments of collections are unwrapped."""

    numbers: bool  # a bare number (not `bool`), or a `Literal` of such numbers
    quantity_types: tuple[type[Quantity], ...]  # the quantity classes, in order of appearance
    models: tuple[type[BaseModel], ...]  # nested settings models, in order of appearance
    unresolved: tuple[str, ...]  # an annotation that is still text

    @property
    def quantities(self) -> bool:
        return bool(self.quantity_types)


def _is_number(value: Any) -> bool:
    return isinstance(value, number_types.Number) and not isinstance(value, bool)


def shape_of(annotation: Any) -> Shape:
    numbers = False
    quantity_types: list[type[Quantity]] = []
    models: list[type[BaseModel]] = []
    unresolved: list[str] = []
    pending = [annotation]
    while pending:
        item = pending.pop(0)
        origin = typing.get_origin(item)
        if origin is Annotated:
            pending.append(typing.get_args(item)[0])
        elif origin is Literal:
            numbers = numbers or any(_is_number(v) for v in typing.get_args(item))
        elif origin is not None:
            pending.extend(typing.get_args(item))
        elif isinstance(item, (str, typing.ForwardRef)):
            unresolved.append(str(item))
        elif isinstance(item, type):
            if issubclass(item, Quantity):
                if item not in quantity_types:
                    quantity_types.append(item)
            elif issubclass(item, BaseModel):
                if item not in models:
                    models.append(item)
            elif issubclass(item, (bool, Enum)):
                pass  # a switch, or a choice among names (even an int enum)
            elif issubclass(item, number_types.Number):
                numbers = True  # int, float, Decimal, Fraction, ... even a subclass of one
    return Shape(numbers, tuple(quantity_types), tuple(models), tuple(unresolved))


def defining_class(model: type[BaseModel], name: str) -> type[BaseModel]:
    """Return the class that declares the field `name` of `model` (the most derived one)."""
    for klass in model.__mro__:
        if (
            isinstance(klass, type)
            and issubclass(klass, BaseModel)
            and name in get_annotations(klass)
        ):
            return klass
    return model


def key_of(model: type[BaseModel], name: str) -> FieldKey:
    return (defining_class(model, name).__qualname__, name)


def reachable_models(roots: Iterable[type[BaseModel]]) -> list[type[BaseModel]]:
    """Return the roots and every model that a field of one of them holds, at any depth."""
    found: list[type[BaseModel]] = []
    pending = list(roots)
    while pending:
        model = pending.pop(0)
        if model in found:
            continue
        found.append(model)
        for info in model.model_fields.values():
            pending.extend(shape_of(info.annotation).models)
    return found


def fields_of(models: Iterable[type[BaseModel]]) -> dict[FieldKey, tuple[type[BaseModel], Any]]:
    """Return each field of `models` once, by the class that defines it, with a model that has it
    and its `FieldInfo`."""
    fields: dict[FieldKey, tuple[type[BaseModel], Any]] = {}
    for model in models:
        for name, info in model.model_fields.items():
            fields.setdefault(key_of(model, name), (model, info))
    return fields


# ------------------------------------------------------------------------------ input paths


class Step(NamedTuple):
    """One step from a settings model to a field below it."""

    key: Union[str, int]  # a field, a key of a `dict` or the index in a `list` or a `tuple`
    model: Union[type[BaseModel], None]  # the model that has the field `key`, else None
    slots: tuple[Any, ...] = ()  # for a fixed `tuple`, a valid value for each of its slots


FieldPath = tuple[Step, ...]

#: A valid value for each required field of a model that a path goes through. A model with a
#: required field that is not here fails loudly, so that a sweep cannot pass by being refused.
REQUIRED_VALUES: dict[FieldKey, Any] = {("PhysicalClock", "period"): "5 ns"}

#: The key that stands for any key of a `dict`.
ANY_KEY = "key"


def _valid_value(annotation: Any) -> Any:
    """Return a valid value for the type `annotation`, for the slots of a tuple that a path does
    not go through. A type without one fails loudly: give it a case here."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Annotated:
        return _valid_value(args[0])
    if origin is Literal:
        return args[0]
    if origin is Union or origin is UnionType:
        return _valid_value(next(arg for arg in args if arg is not type(None)))
    if isinstance(annotation, type):
        if issubclass(annotation, Quantity):
            return annotation.example
        for kind, value in ((bool, False), (int, 0), (float, 0.0), (str, "x"), (PurePath, "x")):
            if issubclass(annotation, kind):
                return value
    raise TypeError(f"give {annotation!r} a valid value in settings_fields._valid_value")


def _paths(annotation: Any, trail: tuple[type[BaseModel], ...]) -> Iterable[FieldPath]:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Annotated:
        yield from _paths(args[0], trail)
    elif origin is Literal:
        return
    elif origin in (dict, Mapping):
        for path in _paths(args[1], trail):
            yield (Step(ANY_KEY, None), *path)
    elif origin in (list, set, frozenset):
        for path in _paths(args[0], trail):
            yield (Step(0, None), *path)
    elif origin is tuple and args and args[-1] is Ellipsis:  # any number of one type
        for path in _paths(args[0], trail):
            yield (Step(0, None), *path)
    elif origin is tuple:  # a fixed number of slots, each with its own type
        found = [(index, list(_paths(arg, trail))) for index, arg in enumerate(args)]
        if any(paths for _, paths in found):
            slots = tuple(_valid_value(arg) for arg in args)
            for index, paths in found:
                for path in paths:
                    yield (Step(index, None, slots), *path)
    elif origin is not None:
        for arg in args:
            yield from _paths(arg, trail)
    elif isinstance(annotation, type) and issubclass(annotation, Quantity):
        yield ()
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in trail:
            return
        for name, info in annotation.model_fields.items():
            for path in _paths(info.annotation, (*trail, annotation)):
                yield (Step(name, annotation), *path)


def quantity_paths(model: type[BaseModel]) -> list[FieldPath]:
    """Return the path from `model` to each field that holds a quantity, at any depth."""
    return list(_paths(model, ()))


def dotted(path: FieldPath) -> str:
    return ".".join(str(step.key) for step in path)


def nest(path: FieldPath, value: Any) -> Any:
    """Return the input that puts `value` at `path`. A model on the way gets a valid value for
    each of its required fields."""
    if not path:
        return value
    step, *rest = path
    inner = nest(tuple(rest), value)
    if isinstance(step.key, int):
        slots = list(step.slots) or [None]
        slots[step.key if step.slots else 0] = inner
        return slots
    level = {step.key: inner}
    if step.model is not None:
        for name, info in step.model.model_fields.items():
            if info.is_required() and name != step.key:
                level[name] = REQUIRED_VALUES[key_of(step.model, name)]
    return level


def merged(base: Mapping[str, Any], more: Mapping[str, Any]) -> dict[str, Any]:
    """Return `base` with the nested mapping `more` laid over it."""
    result = dict(base)
    for key, value in more.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


def follow(settings: Any, path: FieldPath) -> Any:
    """Return what `path` leads to in built `settings`."""
    node = settings
    for step in path:
        node = getattr(node, step.key) if step.model is not None else node[step.key]
    return node


def assign(settings: Any, path: FieldPath, value: Any) -> None:
    """Assign `value` at `path` in built `settings`, as a user of the Python API does."""
    parent = follow(settings, path[:-1])
    last = path[-1]
    if last.model is not None:
        setattr(parent, last.key, value)
    elif isinstance(parent, tuple):  # a tuple holds no assignment: replace it whole
        items = list(parent)
        items[last.key] = value
        assign(settings, path[:-1], tuple(items))
    else:
        parent[last.key] = value
