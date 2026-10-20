"""The base model of Xeda (`XedaBaseModel`), its validator decorators, and helpers for the fields
of settings models: shorthand forms, dotted keys, written paths and waveform settings."""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import cache, cached_property, wraps
from inspect import signature
from pathlib import PurePath
from types import UnionType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

# pylint: disable=no-name-in-module
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    ValidationError,
)
from pydantic import field_validator as _pydantic_field_validator
from pydantic import model_validator as _pydantic_model_validator
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, InitErrorDetails, PydanticCustomError, PydanticUndefined

__all__ = [
    "AliasChoices",
    "Code",
    "ConfigDict",
    "DELIVERABLE_ROLE",
    "LOCATOR",
    "TierName",
    "WORKING",
    "WORKING_ROLE",
    "ErrorDetails",
    "Field",
    "FieldInfo",
    "PrivateAttr",
    "PydanticUndefined",
    "SerializeAsAny",
    "ValidationError",
    "WHOLE",
    "BaseModel",
    "XedaBaseModel",
    "accepts_non_mapping",
    "annotation_args",
    "canonical_names",
    "canonical_tree",
    "appended_fields_of",
    "annotation_form",
    "annotation_mistake",
    "expand_forms",
    "field_shorthands_of",
    "is_locator",
    "is_mistake",
    "mapping_form",
    "nested_model",
    "opaque_key_fields_of",
    "opaque_key_declaration_problems",
    "opaque_key_paths",
    "escape_opaque_names",
    "escaped_names_accepted",
    "names_are_escaped_by_a_peer",
    "fold_dotted_keys",
    "atomic_keys",
    "AtomicName",
    "name_inside_another",
    "shape_problems",
    "single_item_fields_of",
    "table_entries",
    "tier",
    "tier_of",
    "unspecified",
    "conventional_output",
    "LIST_TEXT_MESSAGE",
    "ListLiteralText",
    "comma_separated_items",
    "deliverable",
    "extras",
    "field_validator",
    "input_names",
    "model_validator",
    "validation_errors",
    "written_always",
    "waveform",
    "waveform_fields",
    "waveform_format",
    "COMPRESSED_FORMATS",
    "WAVEFORM_SUFFIXES",
    "waveform_format_name",
    "format_of_waveform_name",
    "waveform_format_of",
    "waveform_value",
    "WaveformText",
    "written_role",
]

log = logging.getLogger(__name__)


# Validator decorators
#
# `field_validator` and `model_validator` wrap the decorators of pydantic. They give every
# validator two guarantees, without each validator having to remember them:
#
# * A mistake of the user is a validation error, never a traceback. pydantic lets a `TypeError`
#   raised inside a validator propagate, so `sources: 123` or `freq: []` would crash the CLI, and
#   `--json` would report it under the wrong class. The wrappers turn it into a validation error.
# * A `mode="before"` validator may normalize its input in place without touching the data of the
#   caller, because it receives a copy.
#
# Validators should still guard their own inputs and raise `ValueError` with a useful message.
# The wrappers are a safety net.


#: The message for a list given as text that is spelled as a list. `key` is the setting as the
#: command line writes it.
LIST_TEXT_MESSAGE = (
    "`{shown}` is text, not a list: write `{key}=` for the empty list and "
    "`{key}=a,b` for a list of items"
)


class ListLiteralText(ValueError):
    """A list given as text that is spelled as a list, such as `[]` or `[a,b]`. `shown` is the text
    (cut if long) and `name` is the field that it was given to."""

    def __init__(self, name: str, shown: str) -> None:
        self.name = name
        self.shown = shown
        super().__init__(LIST_TEXT_MESSAGE.format(shown=shown, key=name))

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.name, self.shown))

    def validation_error(self, key: Optional[str] = None) -> PydanticCustomError:
        """Return this refusal as the validation error `list_text` of the field `key` (default:
        `name`)."""
        return PydanticCustomError(
            "list_text", LIST_TEXT_MESSAGE, {"shown": self.shown, "key": key or self.name}
        )


def comma_separated_items(name: str, text: str) -> List[str]:
    """Return the items of the list setting `name` given as `text` (`a.xdc,b.xdc`). Spaces around
    an item and empty items are dropped, so `""` is the empty list. Text spelled `[...]` is
    refused with a `ListLiteralText`. It is text and not a list, and it would otherwise become
    the one item `"[]"` (`-s flags=[]`), which is not the empty list that its writer means. The
    refusal names the two ways to write the list.

    This is the one rule for a list given as text, whichever model takes it: every flow setting
    (`Flow.Settings._normalize_flow_setting`) and the lists of the models nested in them
    (`CocotbSettings`) use it. A caller reports the refusal with `validation_error`, and
    `validation_errors` words it with the key that a field of a nested model has from the top."""
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        shown = stripped if len(stripped) <= 40 else f"{stripped[:37]}..."
        raise ListLiteralText(name, shown)
    return [item.strip() for item in text.split(",") if item.strip()]


def _guarded(fn: Any, copy_input: bool) -> Any:
    """Wrap a validator body with the two guarantees of the validator decorators.

    `copy_input` copies a `dict` or `list` argument for `mode="before"` validators. pydantic
    passes the object of the caller straight through, so the common pattern "normalize by writing
    back into `values`" would otherwise rewrite data that the caller owns (the `flows` section of
    a design, a `Settings` kwargs dict). The copy must include nested containers, because several
    validators normalize nested clock, parameter and corner mappings before their child models
    are constructed.
    """
    unwrapped = fn.__func__ if isinstance(fn, classmethod) else fn

    @wraps(unwrapped)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            if copy_input and len(args) >= 2:
                value = args[1]
                if isinstance(value, (dict, list)):
                    args = (args[0], deepcopy(value), *args[2:])
            return unwrapped(*args, **kwargs)
        except TypeError as e:
            raise ValueError(str(e)) from e

    return classmethod(wrapper) if isinstance(fn, classmethod) else wrapper


def _guarded_before_model(fn: Any) -> Any:
    """Guard a before-model validator and isolate the input that it may normalize in place.

    pydantic supplies `field_name` both when a nested model is constructed and when an existing
    model is assignment-validated, so `field_name` alone cannot tell the two cases apart. `data`
    is the already-validated state of the containing model for nested construction, and `None`
    for assignment validation. Construction inputs therefore need a full defensive copy. An
    assignment needs a shallow copy of the state and a deep copy of only the newly assigned
    field. That protects the new value of the caller without detaching any unrelated established
    field.
    """
    is_classmethod = isinstance(fn, classmethod)
    unwrapped = fn.__func__ if is_classmethod else fn
    accepts_info = len(signature(unwrapped).parameters) == 3
    accepts_non_mapping = getattr(unwrapped, "__xeda_accepts_non_mapping__", False)

    def wrapper(cls: Any, value: Any, info: Any) -> Any:
        if not isinstance(value, dict) and not accepts_non_mapping:
            # pydantic hands a `mode="before"` model validator the raw input, whatever it is,
            # and these bodies are written against a mapping (`values.get(...)`): `fpga: ["x"]`
            # would crash them with `AttributeError`. Pass anything else through untouched, and
            # pydantic reports its own "valid dictionary" error. A validator that converts a
            # non-mapping shorthand itself opts in with `@accepts_non_mapping` (see `FPGA`).
            return value
        try:
            if isinstance(value, dict):
                is_assignment = info.field_name is not None and info.data is None
                if is_assignment:
                    # The mapping is the established state of the model with the new raw field
                    # value inserted. Copy the mapping itself so validators can write top-level
                    # keys, and isolate only the value being assigned, which the caller owns.
                    value = value.copy()
                    if info.field_name in value:
                        value[info.field_name] = deepcopy(value[info.field_name])
                else:
                    # Root and nested construction both receive mappings that the caller owns.
                    value = deepcopy(value)
            elif isinstance(value, list):
                value = deepcopy(value)
            if accepts_info:
                return unwrapped(cls, value, info)
            return unwrapped(cls, value)
        except TypeError as e:
            raise ValueError(str(e)) from e

    wrapper.__name__ = unwrapped.__name__
    wrapper.__qualname__ = unwrapped.__qualname__
    wrapper.__doc__ = unwrapped.__doc__
    wrapper.__module__ = unwrapped.__module__
    return classmethod(wrapper) if is_classmethod else wrapper


def accepts_non_mapping(fn: Any) -> Any:
    """Mark a `mode="before"` model validator that converts a non-mapping shorthand itself.

    Without it, the wrapper passes any input that is not a `dict` straight through to pydantic,
    which rejects it. Apply it under `@classmethod`.
    """
    fn.__xeda_accepts_non_mapping__ = True
    return fn


def _is_before(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> bool:
    return kwargs.get("mode", "after") == "before" or "before" in args


def field_validator(*args: Any, **kwargs: Any) -> Any:
    """Decorate a field validator like `pydantic.field_validator`, with the guarantees of the
    validators of Xeda (see `_guarded`)."""
    decorator = _pydantic_field_validator(*args, **kwargs)
    copy_input = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        return decorator(_guarded(fn, copy_input))

    return wrap


def model_validator(*args: Any, **kwargs: Any) -> Any:
    """Decorate a model validator like `pydantic.model_validator`, with the guarantees of the
    validators of Xeda and an isolation of the input that knows assignments (see `_guarded` and
    `_guarded_before_model`)."""
    decorator = _pydantic_model_validator(*args, **kwargs)
    before = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        guarded = _guarded_before_model(fn) if before else _guarded(fn, False)
        return decorator(guarded)

    return wrap


# Annotation introspection
#
# A pydantic field carries only its raw annotation. The functions below answer questions such as
# "which types does this Optional or Union accept" with `typing` introspection.


def _number_as_text(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


#: Text that is naturally written as a number, such as a speed grade (`speed: -1`) or a device
#: generation. A setting declares it field by field. A number is accepted for text nowhere else.
Code = Annotated[
    str,
    BeforeValidator(_number_as_text, json_schema_input_type=str | int | float),
]


def annotation_args(annotation: Any) -> Tuple[Any, ...]:
    """Return the members of a Union or Optional annotation, or the annotation itself."""
    if get_origin(annotation) in (Union, UnionType):
        return tuple(a for a in get_args(annotation) if a is not type(None))
    return (annotation,)


def field_annotation(model: Any, name: Optional[str]) -> Any:
    """Return the declared annotation of the field `name` of a model class, or None."""
    if not name:
        return None
    info = getattr(model, "model_fields", {}).get(name)
    return info.annotation if info is not None else None


@cache
def input_names(model: Type[BaseModel]) -> Dict[str, str]:
    """Return the field that each simple key in the input of a model stands for: every field
    name, every `alias` and every text choice of a `validation_alias`. The name of a field wins a
    clash."""
    names: Dict[str, str] = {}
    for name, info in model.model_fields.items():
        spellings = [
            info.alias,
            *getattr(info.validation_alias, "choices", [info.validation_alias]),
        ]
        for spelling in spellings:
            if isinstance(spelling, str):
                names.setdefault(spelling, name)
    names.update((name, name) for name in model.model_fields)
    return names


# Shorthand forms and the tables they stand for
#
# A key that takes a table often takes other forms too: a clock as a port name, `fpga` as a part
# number, `parameters` as a list of `{name, value}` objects. Each form means exactly one table.
# When two layers meet (a target over its design, a settings layer over the one below), both
# sides are read as the tables they stand for. Then the merge decides whether to merge them key
# by key or to replace one by the other. A model says what its forms mean in one place: its
# `as_mapping` classmethod or, for a field whose type is no model, its `field_shorthands` entry.
# The validator of the model calls the same function, so nothing converts a form twice.


class _Whole:
    """The type of `WHOLE`."""

    def __repr__(self) -> str:
        return "WHOLE"


#: What `as_mapping` returns for a form that is valid but is no table, so it cannot be merged
#: key by key: `cocotb: false` (no cocotb) and the name of a platform (a whole model, read from
#: a file). Such a form replaces what is below it and is replaced by what is above it.
WHOLE = _Whole()


def unspecified(value: Any) -> Any:
    """Return `WHOLE` for `None` and `None` for any other value.

    This is the `field_shorthands` entry of a field that takes `None` to say that nothing is
    given (`flows:` with no table). `WHOLE` replaces what is below it. A field that does not take
    `None` declares no such entry, so a `None` there is no form of a table and is reported."""
    return WHOLE if value is None else None


def field_shorthands_of(model: Type[BaseModel]) -> Dict[str, Callable[[Any], Any]]:
    """Return the `field_shorthands` of `model`: its own entries and those of its bases, along
    the MRO. A subclass adds to what its bases declare, and its entry for the same field wins."""
    table: Dict[str, Callable[[Any], Any]] = {}
    for klass in reversed(model.__mro__):
        table.update(vars(klass).get("field_shorthands", {}))
    return table


def appended_fields_of(model: Type[BaseModel]) -> Tuple[str, ...]:
    """Return the `appended_fields` of `model`: its own and those of its bases, in the order of
    the MRO from the base."""
    fields: Dict[str, None] = {}
    for klass in reversed(model.__mro__):
        fields.update(dict.fromkeys(vars(klass).get("appended_fields", ())))
    return tuple(fields)


def single_item_fields_of(model: Type[BaseModel]) -> Tuple[str, ...]:
    """Return the `single_item_fields` of `model`: its own and those of its bases, in the order
    of the MRO from the base."""
    fields: Dict[str, None] = {}
    for klass in reversed(model.__mro__):
        fields.update(dict.fromkeys(vars(klass).get("single_item_fields", ())))
    return tuple(fields)


def opaque_key_fields_of(model: Type[BaseModel]) -> Tuple[str, ...]:
    """Return the `opaque_key_fields` of `model`: its own and those of its bases, in the order
    of the MRO from the base."""
    fields: Dict[str, None] = {}
    for klass in reversed(model.__mro__):
        fields.update(dict.fromkeys(vars(klass).get("opaque_key_fields", ())))
    return tuple(fields)


def opaque_key_declaration_problems(model: Type[BaseModel]) -> List[str]:
    """Return what is wrong with the `opaque_key_fields` of `model`: a name that is no field, and a field that does not hold a dictionary of values (`Dict[str, int]`; the
    entries of a dictionary of tables are fields of their own model)."""
    problems: List[str] = []
    for name in opaque_key_fields_of(model):
        info = model.model_fields.get(name)
        if (
            info is None
            or annotation_kind(info.annotation)[0] != "dict"
            or table_entries(info.annotation) is not None
        ):
            problems.append(
                f"`{name}` is in `opaque_key_fields`, so it must be a setting that holds a "
                "dictionary of values (`Dict[str, int]`), not dictionaries"
            )
    return problems


def opaque_key_paths(model: Type[BaseModel]) -> Tuple[str, ...]:
    """Return the dotted path of every setting of `model` whose keys are names of a tool, at any
    depth: a field of `opaque_key_fields_of(model)`, and the same of each model nested in a field
    (`ir_drop.instance_power`)."""
    paths: List[str] = []

    def walk(current: Type[BaseModel], prefix: str, ancestors: Tuple[type, ...]) -> None:
        opaque = opaque_key_fields_of(current)
        for name, info in current.model_fields.items():
            path = prefix + name
            if name in opaque:
                paths.append(path)
                continue
            child = nested_model(info.annotation)
            if child is not None and child not in ancestors:
                walk(child, path + ".", (*ancestors, child))

    walk(model, "", (model,))
    return tuple(paths)


#: The sequence that `utils.set_hierarchy` keeps whole when it splits a dotted key. The names of
#: a setting in `opaque_key_fields` do not contain it (`utils.EscapedName`).
ESCAPED_DOT = "\\."


#: Whether the design that this code reads was written by the remote runner of a Xeda
#: (`escaped_names_accepted`).
_escaped_names_accepted: ContextVar[bool] = ContextVar("escaped_names_accepted", default=False)


@contextmanager
def escaped_names_accepted() -> Iterator[None]:
    """Read a name with `ESCAPED_DOT` in a table of `opaque_key_fields` as the name with plain
    dots, and accept `A` with `A.B` in one table, for the body of the `with`.

    A remote run uses this for its own load of the design that the local side shipped. The local
    side judged the settings, and it wrote the names of a table that holds `A` and `A.B` escaped
    (`escape_opaque_names`), since a design file is one origin, and it expands a dotted key into
    nested tables, where `A` is a value and `A.B` is a key inside it. A key of a user with a
    backslash before a dot is an error everywhere else."""
    token = _escaped_names_accepted.set(True)
    try:
        yield
    finally:
        _escaped_names_accepted.reset(token)


def names_are_escaped_by_a_peer() -> bool:
    """Return whether `escaped_names_accepted` is in force."""
    return _escaped_names_accepted.get()


def escape_opaque_names(model: Type[BaseModel], values: Mapping[str, Any]) -> Dict[str, Any]:
    """Return `values`, the settings of `model` as a table, with the dots of every name written as
    `ESCAPED_DOT` in each table of `opaque_key_fields` (at any depth) that holds a name inside
    another name (`A` and `A.B`). Nothing else changes, and `values` stays as it is."""
    names = input_names(model)
    opaque = opaque_key_fields_of(model)
    result: Dict[str, Any] = {}
    for key, value in values.items():
        target = names.get(key, key)
        info = model.model_fields.get(target)
        child = nested_model(info.annotation) if info is not None else None
        if target in opaque and isinstance(value, Mapping):
            # Only a table that holds a name inside another one (`A`, `A.B`) cannot be written
            # as nested tables; any other table goes as it is, and every reader takes it.
            if name_inside_another(map(str, value)) is None:
                result[key] = value
            else:
                result[key] = {str(AtomicName.of(name)): item for name, item in value.items()}
        elif child is not None and isinstance(value, Mapping):
            result[key] = escape_opaque_names(child, value)
        else:
            result[key] = value
    return result


class AtomicName(str):
    """A name of a setting in `opaque_key_fields` that a merged table keeps whole, dots included.

    Layers of one setting are merged as tables of these (`atomic_keys`): `A` and `A.B` are two
    names there. The text of an `AtomicName` is the name with each dot as `ESCAPED_DOT`, which
    `utils.set_hierarchy` does not split. The class tells it from text that a user wrote. Text
    that a user wrote is a name to fold (`fold_dotted_keys`), and a key of a user that contains
    `ESCAPED_DOT` is refused."""

    __slots__ = ()

    @classmethod
    def of(cls, name: str) -> "AtomicName":
        return cls(name.replace(".", ESCAPED_DOT))

    @property
    def name(self) -> str:
        return str.replace(self, ESCAPED_DOT, ".")


def fold_dotted_keys(value: Any) -> Any:
    """Fold a table given for a setting whose keys are names that contain dots
    (`STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`) into a table of those names.

    Every origin expands a dotted key into nested tables before it knows the model of the setting
    (`utils.set_hierarchy`). So `STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM: 0` arrives as
    `{"STEPS": {"SYNTH_DESIGN": {"ARGS": {"MAX_BRAM": 0}}}}`. A setting named in
    `Flow.Settings.opaque_key_fields` joins each path to a value back into one key. The dotted
    key and the nested tables are the same table, so they are one setting. An `AtomicName` is
    one name already.

    Only a table with entries is a path. An empty table stays a value, and the type of the
    setting judges it. What cannot be folded also stays as given, for the type to judge: a value
    that is no table, a key that is not text, and a name given twice, such as
    `{"a.b": 1, "a": {"b": 2}}` (no origin makes it). A key with `ESCAPED_DOT` is refused
    (`utils.EscapedName`).
    """
    if not isinstance(value, Mapping):
        return value
    folded: Dict[str, Any] = {}

    def fold(table: Mapping[Any, Any], prefix: str) -> bool:
        for key, entry in table.items():
            if isinstance(key, AtomicName):
                name = key.name
            elif isinstance(key, str):
                if ESCAPED_DOT in key and not names_are_escaped_by_a_peer():
                    from .utils import EscapedName

                    raise EscapedName(prefix + key)
                name = key.replace(ESCAPED_DOT, ".")
            else:
                return False
            path = prefix + name
            if isinstance(entry, Mapping) and entry:
                if not fold(entry, path + "."):
                    return False
                continue
            if path in folded:
                return False
            folded[path] = entry
        return True

    return folded if fold(value, "") else value


def atomic_keys(value: Any) -> Any:
    """Fold a table like `fold_dotted_keys` and make each name an `AtomicName`. This is the form
    in which layers of one setting are merged. The normalization of the setting folds the names
    back."""
    folded = fold_dotted_keys(value)
    if not isinstance(folded, Mapping) or folded is value:
        return value
    return {AtomicName.of(key): entry for key, entry in folded.items()}


def name_inside_another(names: Iterable[str]) -> Optional[Tuple[str, str]]:
    """Return `(name, other)` when `other` is `name` followed by a dot and more, else `None`. Of
    two such names, one is a value and the other sets a key inside it (`utils.set_hierarchy`)."""
    given = set(names)
    for name in sorted(given):
        for other in sorted(given):
            if other.startswith(name + "."):
                return name, other
    return None


def annotation_kind(annotation: Any) -> Tuple[str, Optional[Type["XedaBaseModel"]], bool]:
    """Return what the annotation of a field says about a value written as a table:
    `("model", M, optional)` for a field that holds the model `M`, `("dict", None, optional)` for
    one that holds a mapping, and `("other", None, optional)` for anything else. A union with
    another alternative (`str | list | M`) is "other": such a field may hold a value that is no
    table."""
    optional = False
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if origin in (Union, UnionType):
            members = [a for a in get_args(annotation) if a is not type(None)]
            optional = optional or len(members) < len(get_args(annotation))
            if len(members) != 1:
                return "other", None, optional
            annotation = members[0]
            continue
        break
    if isinstance(annotation, type) and issubclass(annotation, XedaBaseModel):
        return "model", annotation, optional
    if origin in (dict, Mapping) or annotation in (dict, Mapping):
        return "dict", None, optional
    return "other", None, optional


def nested_model(annotation: Any) -> Optional[Type["XedaBaseModel"]]:
    """Return the model that a table written for a field of this annotation is validated as, if
    the field holds nothing else."""
    kind, model, _ = annotation_kind(annotation)
    return model if kind == "model" else None


def table_entries(annotation: Any) -> Any:
    """Return the annotation of the entries of a field that holds a dictionary whose every entry
    is a table (`Dict[str, Dict[str, Any]]`, `Dict[str, PhysicalClock]`), or None. A field of any
    other kind, a dictionary of values that are no tables, and one whose entries may be other
    things (a union) are not judged entry by entry."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if origin in (Union, UnionType):
            members = [a for a in get_args(annotation) if a is not type(None)]
            if len(members) != 1:
                return None
            annotation = members[0]
            continue
        break
    arguments = get_args(annotation)
    if origin not in (dict, Mapping) or len(arguments) != 2:
        return None
    return arguments[1] if annotation_kind(arguments[1])[0] in ("model", "dict") else None


def canonical_names(values: Mapping[str, Any], model: Type[BaseModel]) -> Dict[str, Any]:
    """Return `values` with each key at one level of `model` spelled as the model spells the
    field (an alias and its field are one setting). A field given under two spellings keeps
    both, as written, so validation reports the ambiguity and one spelling does not win
    silently."""
    names = input_names(model)
    targets = [names.get(key, key) for key in values]
    duplicates = {target for target, count in Counter(targets).items() if count > 1}
    canonical: Dict[str, Any] = {}
    for (key, value), target in zip(values.items(), targets):
        canonical[key if target in duplicates else target] = value
    return canonical


def canonical_tree(model: Type[BaseModel], values: Mapping[str, Any]) -> Dict[str, Any]:
    """Return `values` with `canonical_names` applied at every level of nested models."""
    tree = canonical_names(values, model)
    for key, value in tree.items():
        child = nested_model(field_annotation(model, input_names(model).get(key)))
        if child is not None and isinstance(value, Mapping):
            tree[key] = canonical_tree(child, value)
    return tree


def annotation_form(
    annotation: Any, value: Any, shorthand: Optional[Callable[[Any], Any]] = None
) -> Any:
    """Return the table that `value`, written where `annotation` is expected, stands for: `value`
    when it is a table; the table of a shorthand form (the `shorthand` function, else the
    `as_mapping` of the nested model); `WHOLE` for a valid form that is no table; `None` for
    anything else (a mistake, which validation reports)."""
    if isinstance(value, Mapping):
        return value
    child = nested_model(annotation)
    if child is not None and isinstance(value, child):
        return WHOLE  # a model already built is a whole value
    hook = shorthand
    if hook is None:
        hook = child.as_mapping if child is not None else None
    if hook is None:
        return None
    try:
        return hook(value)
    except (ValueError, TypeError):
        return None


def annotation_mistake(annotation: Any, value: Any, form: Any) -> bool:
    """Return whether `value` is no table where `annotation` takes one: its `form`
    (`annotation_form`) is no table and not `WHOLE`, and the value is not `None` for an optional
    annotation. An annotation that takes values other than a table is never judged."""
    kind, _, optional = annotation_kind(annotation)
    if kind == "other":
        return False
    return not (isinstance(form, Mapping) or form is WHOLE or (value is None and optional))


def mapping_form(owner: Type[BaseModel], field: Optional[str], value: Any) -> Any:
    """Return the table that `value`, written for the field `field` of `owner`, stands for:
    `value` when it is a table; the table of a shorthand form; `WHOLE` for a valid form that is
    no table; `None` for anything else (a mistake, which validation reports)."""
    if isinstance(value, Mapping):
        return value
    if not field or field not in getattr(owner, "model_fields", {}):
        return None
    return annotation_form(
        owner.model_fields[field].annotation, value, field_shorthands_of(owner).get(field)
    )


def expand_forms(model: Type[BaseModel], values: Mapping[str, Any]) -> Dict[str, Any]:
    """Return `values` (a table written for `model`) with every shorthand form read as the table
    it stands for, through every level of nested models. Anything that is no form stays as it
    is."""
    names = input_names(model)
    expanded: Dict[str, Any] = {}
    for key, value in values.items():
        field = names.get(key)
        form = mapping_form(model, field, value)
        child = nested_model(field_annotation(model, field))
        if isinstance(form, Mapping):
            expanded[key] = expand_forms(child, form) if child is not None else dict(form)
        else:
            expanded[key] = value
    return expanded


def is_mistake(owner: Type[BaseModel], field: str, value: Any) -> bool:
    """Return whether `value` is no table where the `field` of `owner` takes one: not a table,
    not a shorthand form of one (`mapping_form`), not a valid form that is no table (`WHOLE`),
    and not `None` for an optional field. A field that takes values other than a table is never
    judged."""
    return annotation_mistake(
        owner.model_fields[field].annotation, value, mapping_form(owner, field, value)
    )


def shape_problems(
    model: Type[BaseModel], values: Mapping[str, Any]
) -> List[Tuple[Tuple[str, ...], Any]]:
    """Return where `values` (a table written for `model`) holds a mistake (`is_mistake`) at a
    key that takes a table, as `(key path, value)` pairs. The keys are as they are written,
    through every level of nested models. A key that is no field is judged only if the model
    says so (`XedaBaseModel.form_problems`). This checks the shape and does not validate."""
    problems: List[Tuple[Tuple[str, ...], Any]] = [
        ((key,), value) for key, value in model.form_problems(values)  # type: ignore[attr-defined]
    ]
    names = input_names(model)
    for key, value in values.items():
        field = names.get(key)
        if field is None or field not in model.model_fields:
            continue
        if is_mistake(model, field, value):
            problems.append(((key,), value))
            continue
        annotation = model.model_fields[field].annotation
        child = nested_model(annotation)
        form = mapping_form(model, field, value)
        if child is not None and isinstance(form, Mapping):
            problems += [((key, *path), v) for path, v in shape_problems(child, form)]
        entries = table_entries(annotation)
        if entries is not None and isinstance(form, Mapping):
            problems += [((key, *path), v) for path, v in _entry_problems(entries, form)]
    return problems


def _entry_problems(
    annotation: Any, entries: Mapping[Any, Any]
) -> List[Tuple[Tuple[str, ...], Any]]:
    """Return the `shape_problems` of a dictionary whose entries are tables (`annotation` is
    the one that `table_entries` gave): the entries that are mistakes, and the problems inside
    the entries that are models."""
    problems: List[Tuple[Tuple[str, ...], Any]] = []
    child = nested_model(annotation)
    for name, entry in entries.items():
        form = annotation_form(annotation, entry)
        if annotation_mistake(annotation, entry, form):
            problems.append(((str(name),), entry))
        elif child is not None and isinstance(form, Mapping):
            problems += [((str(name), *path), v) for path, v in shape_problems(child, form)]
    return problems


#: The role of a setting whose path a flow writes, as a `json_schema_extra` marker. A working
#: location (a build or report directory, a log) is a name inside the run directory. A
#: deliverable is a name there, or a location that it is delivered to after the run
#: (`xeda.deliver`). A path field without a role is read.
WORKING_ROLE = "working"
DELIVERABLE_ROLE = "deliverable"
WORKING: Dict[str, Any] = {"x-xeda-writes": WORKING_ROLE}


#: The marker of a setting that names where installed data lives on this machine (a PDK store),
#: as a `json_schema_extra` marker. Such a place is no part of the identity of a run, a launch
#: never lists or traverses what lies under it, and a remote run never receives it: the remote
#: host uses its own. The flow registers the exact files it opens (`Flow.implicit_inputs`), and
#: the trace records their content. Mark an `Optional` field only.
LOCATOR: Dict[str, Any] = {"x-xeda-locator": True}


def is_locator(model: type[BaseModel], field: str) -> bool:
    """Return whether the `field` of `model` is marked `LOCATOR`."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    return isinstance(extra, dict) and extra.get("x-xeda-locator") is True


#: The tier of a setting: how often a user needs it. It is metadata for documentation and
#: listings (`xeda list-settings`) and never changes what a setting accepts.
TierName = Literal["common", "advanced", "expert"]
TIER_KEY = "x-xeda-tier"
TIERS: Tuple[str, ...] = get_args(TierName)


def tier(name: TierName) -> Dict[str, Any]:
    """Return the `json_schema_extra` marker of a setting with the tier `name`."""
    if name not in TIERS:
        raise ValueError(f"unknown tier {name!r}: use one of {', '.join(TIERS)}")
    return {TIER_KEY: name}


def tier_of(model: type[BaseModel], field: str) -> Optional[TierName]:
    """Return the tier of the `field` of `model`, or None when it has none."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    name = extra.get(TIER_KEY) if isinstance(extra, dict) else None
    return name if name in TIERS else None  # type: ignore[return-value]


def extras(*markers: Mapping[str, Any]) -> Dict[str, Any]:
    """Return one `json_schema_extra` dictionary with the keys of all `markers` (`tier(...)`,
    `WORKING`, `deliverable(...)`). Two markers that give one key different values are an
    error."""
    merged: Dict[str, Any] = {}
    for marker in markers:
        for key, value in marker.items():
            if key in merged and merged[key] != value:
                raise ValueError(
                    f"two markers give `{key}` different values: {merged[key]!r} and {value!r}"
                )
            merged[key] = value
    return merged


def deliverable(conventional: Optional[str] = None, *, always: bool = False) -> Dict[str, Any]:
    """Return the marker of a setting whose path is an output that the user may want delivered.

    `conventional` is the name that the run writes the output as when the setting is given a
    location (`{design}` stands for the name of the design; by convention
    `outputs/<design>.<ext>`). With None, the name is the default of the setting.

    `always` marks an output that the flow writes whether or not the setting is given. When the
    setting is unset, the flow writes the conventional name, and the identity of the run counts
    that name as the value (`flow.identity_settings`). An unset setting and a location are then
    one run. A setting that switches its output on (unset: nothing is written) is not such an
    output.

    A deliverable that is no waveform (`waveform`) names a file. A model refuses `true` and
    `false` for it, since the text would become a file named `true`."""
    marker: Dict[str, Any] = {"x-xeda-writes": DELIVERABLE_ROLE}
    if conventional is not None:
        marker["x-xeda-output"] = conventional
    if always:
        if conventional is None:
            raise ValueError("an output written always needs a conventional name")
        marker["x-xeda-always"] = True
    return marker


#: The `json_schema_extra` key of a setting that names a waveform file, with the format of the
#: waveform. `Flow.Settings` reads a marked setting by one rule (`waveform_value`).
WAVEFORM_KEY = "x-xeda-waveform"


def waveform(extension: str, conventional: Optional[str] = None) -> Dict[str, Any]:
    """Return the marker of a setting that names a waveform file of the format `extension` (`vcd`,
    `fst`, `ghw`, `saif`, ...). It is a deliverable. `conventional` is the name that the run
    writes when the setting is given a location, `dump.<extension>` by default. `true` stands for
    that same name, so `true` and a location are one run (`flow.identity_settings`). The name may
    hold the name of the design (`outputs/{design}.saif`). The setting then keeps the text as it
    is, and the launcher writes the name of the design where the run writes the file
    (`deliver.split_deliveries`)."""
    marker = deliverable(conventional or f"dump.{extension}")
    marker[WAVEFORM_KEY] = {"format": extension}
    return marker


def waveform_format(model: type[BaseModel], field: str) -> Optional[Tuple[str, str]]:
    """Return `(extension, name)` for the waveform setting `field` of `model` (see `waveform`),
    where `name` is what `true` stands for. Return None if the field is no waveform setting."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    mark = extra.get(WAVEFORM_KEY) if isinstance(extra, dict) else None
    if not isinstance(extra, dict) or not isinstance(mark, dict):
        return None
    return str(mark["format"]), str(extra["x-xeda-output"])


#: Every extension that a tool writes a waveform format under, by format (without the dot,
#: compared in lower case, the longest match winning). The check of a name against the format of
#: its setting reads this table (`format_of_waveform_name`). A format may have more than one
#: extension. `vcdgz` is VCD compressed with gzip (`--vcdgz` of GHDL): a name that ends in `.gz`
#: is one, whatever comes before, and `vcd` is always plain.
WAVEFORM_SUFFIXES: Dict[str, Tuple[str, ...]] = {
    "vcd": ("vcd",),
    "vcdgz": ("vcd.gz", "vcdgz", "gz"),
    "fst": ("fst",),
    "ghw": ("ghw",),
    "saif": ("saif",),
    "vpd": ("vpd",),
    "fsdb": ("fsdb",),
    "evcd": ("evcd",),
}


#: A format that is another format compressed, and the plain format it is made of.
COMPRESSED_FORMATS: Dict[str, str] = {"vcdgz": "vcd"}


def waveform_format_name(extension: str) -> str:
    """Return how a message names the waveform format `extension`: `VCD`, `FST`, ... and, for a
    compressed format, `gzip-compressed VCD`."""
    plain = COMPRESSED_FORMATS.get(extension)
    return f"gzip-compressed {plain.upper()}" if plain else extension.upper()


def format_of_waveform_name(name: Any) -> Optional[Tuple[str, str]]:
    """Return `(format, suffix)` for a waveform file name that ends in an extension of
    `WAVEFORM_SUFFIXES`, the longest one, and None for any other name. The suffix is in lower
    case."""
    if not isinstance(name, (str, os.PathLike)):
        return None
    lowered = os.fspath(name).lower()
    found: Optional[Tuple[str, str]] = None
    for extension, suffixes in WAVEFORM_SUFFIXES.items():
        for suffix in suffixes:
            if lowered.endswith("." + suffix) and (found is None or len(suffix) > len(found[1])):
                found = (extension, suffix)
    return found


def waveform_format_of(model: type[BaseModel], field: str) -> Optional[str]:
    """Return the format of the waveform setting `field` of `model` (`vcd`, `fst`, `ghw`, ...),
    or None if it is no waveform setting. The marker is the one place that says a setting takes
    `true` as on."""
    found = waveform_format(model, field)
    return found[0] if found else None


def waveform_fields(model: type[BaseModel]) -> Dict[str, str]:
    """Return every waveform setting of `model` (see `waveform`), with its format."""
    return {
        name: found[0] for name in model.model_fields if (found := waveform_format(model, name))
    }


class WaveformText(ValueError):
    """Text that a waveform setting cannot take: a word that YAML 1.1 reads as a boolean (`yes`).
    `message` says what to write."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.message,))

    def validation_error(self, key: Optional[str] = None) -> PydanticCustomError:
        """Return this refusal as the validation error `boolean_text`."""
        return PydanticCustomError("boolean_text", self.message)


def waveform_value(value: Any, extension: str, true_name: str) -> Any:
    """Return a waveform setting as the name it stands for. This is the one rule for every
    simulator.

    `true` (the boolean, or the text in any letter case, which is all `-s` can write) stands for
    `true_name`, the name that the run writes for a location (`waveform`), which may hold
    `{design}`. `false` (likewise) and an empty name stand for none. A name without an extension
    gets `.<extension>`. A setting is on, off or a name, so the other words that YAML 1.1 reads
    as a boolean (`yes`, `on`, `n`, ...) are refused as they are for a boolean field
    (`boolean_text_message`). So is `true` or `false` with spaces around it, given as the whole
    value. Other text is a name. Anything else is left as it is, for the validation of the
    field."""
    if isinstance(value, str):
        word = value.strip().lower()
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        elif word in ("true", "false") or word in (_TRUE_WORDS | _FALSE_WORDS):
            # As for a boolean field, text with spaces around `true` is no boolean and no name.
            raise WaveformText(f"{boolean_text_message(value)} (or give a file name)")
    if value is True:
        return true_name
    if value is False or (isinstance(value, str) and value == ""):
        return None
    if isinstance(value, (str, PurePath)) and not PurePath(value).suffix:
        return f"{value}.{extension}"
    return value


def written_role(model: type[BaseModel], field: str) -> Optional[str]:
    """Return the role of the `field` of `model` (`WORKING_ROLE`, `DELIVERABLE_ROLE`), or None if
    it is read."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    role = extra.get("x-xeda-writes") if isinstance(extra, dict) else None
    return role if isinstance(role, str) else None


def written_always(model: type[BaseModel], field: str) -> bool:
    """Return whether the deliverable `field` of `model` is an output that the flow writes even
    when the setting is unset (`deliverable(always=True)`)."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    return isinstance(extra, dict) and extra.get("x-xeda-always") is True


def _deliverable_message(key: str, value: Any) -> str:
    """Return why `true` or `false` is no value of the deliverable `key`."""
    from .utils import LOCATION_FORMS

    return (
        f"`{str(value).lower()}` is a switch, and `{key}` names a file: give a name or a "
        f"location ({LOCATION_FORMS}), or leave it unset"
    )


def conventional_output(model: type[BaseModel], field: str, design: str) -> Optional[PurePath]:
    """Return the name under which the deliverable `field` of `model` is written in the run
    directory when the setting is given a location: the conventional name of its marker for the
    design `design`, else the default name of the field. Return None if the field is no
    deliverable or has neither."""
    if written_role(model, field) != DELIVERABLE_ROLE:
        return None
    info = model.model_fields[field]
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
    template = extra.get("x-xeda-output")
    if isinstance(template, str):
        return PurePath(template.format(design=design))
    default = info.default
    if isinstance(default, (str, os.PathLike)) and os.fspath(default):
        return PurePath(default)
    return None


#: The words that YAML 1.1 (and the lax booleans of pydantic) read as a boolean. Xeda reads
#: YAML 1.2, where they are text, and a setting accepts exactly its declared type, so they are
#: never a boolean.
_TRUE_WORDS = frozenset({"y", "yes", "on", "t"})
_FALSE_WORDS = frozenset({"n", "no", "off", "f"})


def _is_boolean_annotation(annotation: Any) -> bool:
    """Whether `annotation` is `bool` or `Optional[bool]`."""
    accepted = annotation_args(annotation) if annotation is not None else ()
    return bool(accepted) and all(a is bool for a in accepted)


def boolean_text_message(text: str) -> str:
    """Return what to say about `text` given where a boolean is required."""
    word = text.strip().lower()
    if word in _TRUE_WORDS:
        return f"`{text}` is text, not a boolean: write `true`"
    if word in _FALSE_WORDS:
        return f"`{text}` is text, not a boolean: write `false`"
    return f"`{text}` is text, not a boolean: write `true` or `false`"


def yaml11_hint(value: Any) -> Optional[str]:
    """Return what YAML 1.2 wants written for a value that does not fit a field, when the value
    is a word that YAML 1.1 read as a boolean (`on`, `no`). Return None otherwise."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return f"`{value}` is text in Xeda YAML (YAML 1.2): write `true`"
    if word in _FALSE_WORDS:
        return f"`{value}` is text in Xeda YAML (YAML 1.2): write `false`"
    return None


class XedaBaseModel(BaseModel):
    model_config = ConfigDict(
        validate_assignment=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        # https://github.com/samuelcolvin/pydantic/issues/1241
        ignored_types=(cached_property,),
        use_enum_values=True,
        populate_by_name=True,
        # A default goes through the same validators as a given value, so leaving a setting out
        # and writing its default explicitly (as a saved `settings.json` does) are the same.
        validate_default=True,
    )

    #: Fields whose value is no model but has forms that stand for a table (`parameters` as a
    #: list of `{name, value}`). The table maps the name of a field to a function from a form to
    #: its table. The function returns `None` when the value is not one of the forms of the
    #: field. A subclass adds its entries to those of its bases (`field_shorthands_of` walks the
    #: MRO), and an entry for the same field wins.
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}
    #: Fields that are lists to which a layer adds instead of replacing them, when the merge
    #: asks for it (`merge_layers(append=True)`, a target's merge over the `sources` of its
    #: design). A subclass adds to those of its bases (`appended_fields_of`).
    appended_fields: ClassVar[Tuple[str, ...]] = ()
    #: List fields that also take one item where a list is expected (`sources: rtl/a.v`). The
    #: validator of the field reads that form, and `design_schema()` describes it from this
    #: declaration. A subclass adds to those of its bases (`single_item_fields_of`).
    single_item_fields: ClassVar[Tuple[str, ...]] = ()
    #: Fields that hold a dictionary whose keys are names from a tool or a netlist (a Vivado run
    #: property such as `STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`, a flattened instance such as
    #: `u_core.reg_1`), which may contain dots. A dotted key of such a field is one key, whichever
    #: way it is given (`fold_dotted_keys`). A flow declares its own fields, and a group declares
    #: the fields of the group. A subclass adds to those of its bases (`opaque_key_fields_of`).
    opaque_key_fields: ClassVar[Tuple[str, ...]] = ()

    @classmethod
    def as_mapping(cls, value: Any) -> Any:
        """Return the table that `value`, a form other than a table, stands for as this model.
        The validator of the model reads the form by this function, and a merge of two layers
        reads both sides by it. Return `WHOLE` for a valid form that is no table, and `None` for
        a value that is no form of this model. A model without forms keeps this default."""
        return None

    @classmethod
    def form_problems(cls, values: Mapping[str, Any]) -> List[Tuple[str, Any]]:
        """Return the keys of `values` (a table written for this model) that hold a mistake though
        they are no fields, as `(key, value)` pairs. A model with input-only spellings (the
        `clock` of a design) overrides this, so that `shape_problems` judges them too. The
        default judges none."""
        return []

    @classmethod
    def merge_inputs(cls, merged: Dict[str, Any], values: Dict[str, Any]) -> None:
        """Merge the keys of `values` that this model merges by its own rule into `merged`
        (what is below them), and remove them from `values`. Both are canonical tables of this
        model. A model whose fields have several spellings with one meaning (the clock of a
        design) overrides this. The default merges nothing."""

    @_pydantic_model_validator(mode="before")
    @classmethod
    def _booleans_are_not_text(cls, values: Any) -> Any:
        """Refuse text or a number given to a `bool` or `Optional[bool]` field, unless the text is
        `true` or `false` (a command line has no other way to write a boolean). The lax booleans
        of pydantic would read `yes`, `on`, `y` and `1` as `True`, but a setting accepts exactly
        its declared type. There is one more exception: `true` and `false` given to a deliverable
        that is no waveform are refused too. Every model of Xeda shares this rule: flow settings,
        the design and nested models. The input is only read and never rewritten."""
        if not isinstance(values, dict):
            return values
        names = input_names(cls)
        problems = []
        for key, value in values.items():
            if not (isinstance(key, str) and key in names):
                continue
            if (
                written_role(cls, names[key]) == DELIVERABLE_ROLE
                and waveform_format_of(cls, names[key]) is None
                and (
                    isinstance(value, bool)
                    or (isinstance(value, str) and value.lower() in ("true", "false"))
                )
            ):
                problems.append(
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "deliverable_switch", _deliverable_message(key, value)
                        ),
                        loc=(key,),
                        input=value,
                    )
                )
                continue
            if isinstance(value, str):
                if value.lower() in ("true", "false"):  # how a command line writes a boolean
                    continue
                message = boolean_text_message(value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                message = f"`{value}` is a number, not a boolean: write `true` or `false`"
            else:
                continue
            if _is_boolean_annotation(field_annotation(cls, names[key])):
                problems.append(
                    InitErrorDetails(
                        type=PydanticCustomError("boolean_text", message), loc=(key,), input=value
                    )
                )
        if problems:
            raise ValidationError.from_exception_data(cls.__name__, problems)
        return values

    def invalidate_cached_properties(self):
        """Discard the cached properties, inherited ones included, after the state of the model
        changes."""
        # A `cached_property` caches in the instance `__dict__`, whichever class along the MRO
        # declares it, so every base must be searched: `VivadoTool` inherits `Tool.version`.
        for klass in type(self).__mro__:
            for key, value in vars(klass).items():
                if isinstance(value, cached_property) and key not in type(self).model_fields:
                    log.debug("invalidating: %s", str(key))
                    self.__dict__.pop(key, None)


_XedaModelType = TypeVar("_XedaModelType", bound=XedaBaseModel)


@cache
def model_with_allow_extra(cls: Type[_XedaModelType]) -> Type[_XedaModelType]:
    """Return a subclass of `cls` that tolerates unknown keys.

    pydantic v2 compiles the validator of a model when the class is created, so changing
    `model_config` on an existing class has no effect. Building a subclass is the only form that
    makes pydantic build the schema again, and it keeps the relaxation within the subclass.

    The result is cached, and its instances have a custom reducer that rebuilds the derived class
    in a child process. The DSE runner ships a `Design` across a process boundary with `pebble`,
    so relying on pickle to resolve this class, which exists only at run time, by `__module__`
    and `__qualname__` fails.
    """
    name = f"{cls.__name__}AllowExtra"

    def __reduce__(self):
        # The class is built at run time, so a child process (spawn) has no such attribute to
        # look up. Pickle the base class, which is an ordinary importable class, and rebuild
        # the derived class on the far side. The DSE runner ships designs to `pebble` workers
        # this way.
        return (_rebuild_with_allow_extra, (cls, self.__getstate__()))

    derived = type(
        name,
        (cls,),
        {
            "model_config": ConfigDict(**{**cls.model_config, "extra": "allow"}),
            "__module__": __name__,
            "__qualname__": name,
            "__reduce__": __reduce__,
        },
    )
    return derived  # type: ignore[return-value]


def _rebuild_with_allow_extra(base: Any, state: Any) -> Any:
    """Unpickle an instance of `model_with_allow_extra(base)`. A module-level function, so that
    it pickles."""
    cls: Any = model_with_allow_extra(base)
    obj = cls.__new__(cls)
    obj.__setstate__(state)
    return obj


def validation_errors(
    errors: List[ErrorDetails],
) -> List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]:
    return [
        (
            " -> ".join(str(loc) for loc in e.get("loc", [])),
            _list_text_message(e) or _with_yaml_hint(e),
            "".join(f"; {k}={v}" for k, v in (e.get("ctx") or {}).items()),
            e.get("type"),
        )
        for e in errors
    ]


def _list_text_message(error: ErrorDetails) -> Optional[str]:
    """Return the message for a list given as text that is spelled as a list (`ListLiteralText`),
    for a field of a nested model, with the key as the command line writes it from the top
    (`cocotb.testcase`, not `testcase`). The model that refused it knows only its own field."""
    loc = error.get("loc", ())
    context = error.get("ctx") or {}
    if error.get("type") == "list_text" and len(loc) > 1:
        key = ".".join(str(part) for part in loc)
        return LIST_TEXT_MESSAGE.format(shown=context.get("shown", ""), key=key)
    return None


def _with_yaml_hint(error: ErrorDetails) -> str:
    """Return the message of one error, with what to write when YAML 1.2 changed the input: a
    word that YAML 1.1 read as a boolean (`ncpus: on`), or a number where text is needed
    (`top: 010`)."""
    msg = str(error.get("msg"))
    if error.get("type") in ("boolean_text", "extra_forbidden"):
        return msg
    value = error.get("input")
    hint = yaml11_hint(value)
    if hint is None and error.get("type") == "string_type" and isinstance(value, (int, float)):
        shown = repr(value)
        hint = f'`{shown}` is a number in Xeda YAML (YAML 1.2): write `"{shown}"` for text'
    return f"{msg}; {hint}" if hint else msg
