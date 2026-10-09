"""Interchangeable dataclass abstraction"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Mapping
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
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

import attrs

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
    "asdict",
    "canonical_names",
    "canonical_tree",
    "appended_fields_of",
    "expand_forms",
    "field_shorthands_of",
    "is_mistake",
    "mapping_form",
    "nested_model",
    "shape_problems",
    "unspecified",
    "conventional_output",
    "LIST_TEXT_MESSAGE",
    "ListLiteralText",
    "comma_separated_items",
    "deliverable",
    "field_validator",
    "input_names",
    "model_validator",
    "validation_errors",
    "written_role",
]

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------
# validator decorators
#
# xeda's `field_validator` / `model_validator` wrap pydantic's with two guarantees for every
# validator, current or future, without each one having to remember them:
#
# * A user's mistake is a validation error, never a traceback. pydantic lets a `TypeError` raised
#   inside a validator propagate, so `sources = 123` or `freq = []` would crash the CLI and be
#   reported under the wrong class by `--json`; the wrappers turn it into a validation error.
# * A `mode="before"` validator may normalize its input in place without touching the caller's
#   data: it receives a copy.
#
# Validators should still guard their own inputs and raise `ValueError` with a useful message;
# the wrappers are the safety net.
# --------------------------------------------------------------------------------------------


#: What is said of a list given as text that is spelled as a list; `key` is the setting as the
#: command line writes it.
LIST_TEXT_MESSAGE = (
    "`{shown}` is text, not a list: write `{key}=` for the empty list and "
    "`{key}=a,b` for a list of items"
)


class ListLiteralText(ValueError):
    """A list given as text that is spelled as a list: `[]`, `[a,b]`. `shown` is the text (cut if
    long), `name` the field it was given to."""

    def __init__(self, name: str, shown: str) -> None:
        self.name = name
        self.shown = shown
        super().__init__(LIST_TEXT_MESSAGE.format(shown=shown, key=name))

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.name, self.shown))

    def validation_error(self, key: Optional[str] = None) -> PydanticCustomError:
        """This refusal as the validation error `list_text` of the field `key` (default: `name`)."""
        return PydanticCustomError(
            "list_text", LIST_TEXT_MESSAGE, {"shown": self.shown, "key": key or self.name}
        )


def comma_separated_items(name: str, text: str) -> List[str]:
    """The items of the list setting `name` given as `text` (`a.xdc,b.xdc`): spaces around an item
    and empty items are dropped, so `""` is the empty list. Text spelled `[...]` is refused with a
    `ListLiteralText`: it is text, never a list, and would otherwise become the one item `"[]"`
    (`-s flags=[]`), which is not the empty list its writer means. The two ways to write the list
    are named instead.

    This is the one rule for a list given as text, whichever model takes it: every flow setting
    (`Flow.Settings._normalize_flow_setting`) and the lists of the models nested in them
    (`CocotbSettings`) go through it. A caller reports the refusal with `validation_error`, and
    `validation_errors` words it with the key a nested model's field has from the top."""
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        shown = stripped if len(stripped) <= 40 else f"{stripped[:37]}..."
        raise ListLiteralText(name, shown)
    return [item.strip() for item in text.split(",") if item.strip()]


def _guarded(fn: Any, copy_input: bool) -> Any:
    """Wrap a validator body with the two guarantees described above.

    `copy_input` copies a `dict`/`list` argument for `mode="before"` validators. pydantic passes
    the caller's own object straight through, so the common "normalize by writing back into
    `values`" pattern would otherwise rewrite caller-owned data (a design's `flow[...]` section, a
    `Settings` kwargs dict). The copy must include nested
    containers: several validators intentionally normalize nested clock, parameter, and corner
    mappings before their child models are constructed.
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
    """Guard a before-model validator and isolate the input it may normalize in place.

    Pydantic supplies ``field_name`` both when a nested model is being constructed and when an
    existing model is assignment-validated, so it cannot distinguish those cases by itself.
    ``data`` is the containing model's already-validated state for nested construction, but is
    ``None`` for assignment validation.  Construction inputs therefore need a full defensive
    copy; assignment needs a shallow state copy plus a deep copy of only the newly assigned field.
    That protects the caller's new value without detaching any unrelated established fields.
    """
    is_classmethod = isinstance(fn, classmethod)
    unwrapped = fn.__func__ if is_classmethod else fn
    accepts_info = len(signature(unwrapped).parameters) == 3
    accepts_non_mapping = getattr(unwrapped, "__xeda_accepts_non_mapping__", False)

    def wrapper(cls: Any, value: Any, info: Any) -> Any:
        if not isinstance(value, dict) and not accepts_non_mapping:
            # pydantic hands a `mode="before"` model validator the raw input, whatever it is, and
            # these bodies are written against a mapping (`values.get(...)`): `fpga = ["x"]`
            # would crash them with `AttributeError`. Pass anything else through untouched, and
            # pydantic reports its own "valid dictionary" error.
            # A validator that converts a non-mapping shorthand itself opts in with
            # `@accepts_non_mapping` (see `FPGA`).
            return value
        try:
            if isinstance(value, dict):
                is_assignment = info.field_name is not None and info.data is None
                if is_assignment:
                    # The mapping is the model's established state with the new raw field value
                    # inserted.  Copy the mapping itself so validators can write top-level keys,
                    # and isolate only the caller-owned value being assigned.
                    value = value.copy()
                    if info.field_name in value:
                        value[info.field_name] = deepcopy(value[info.field_name])
                else:
                    # Root and nested construction both receive caller-owned input mappings.
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
    which rejects it. Apply it *under* `@classmethod`.
    """
    fn.__xeda_accepts_non_mapping__ = True
    return fn


def _is_before(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> bool:
    return kwargs.get("mode", "after") == "before" or "before" in args


def field_validator(*args: Any, **kwargs: Any) -> Any:
    """`pydantic.field_validator` with xeda's validator guarantees. See `_guarded`."""
    decorator = _pydantic_field_validator(*args, **kwargs)
    copy_input = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        return decorator(_guarded(fn, copy_input))

    return wrap


def model_validator(*args: Any, **kwargs: Any) -> Any:
    """`pydantic.model_validator` with xeda's validator guarantees and assignment-aware input
    isolation. See `_guarded` and `_guarded_before_model`."""
    decorator = _pydantic_model_validator(*args, **kwargs)
    before = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        guarded = _guarded_before_model(fn) if before else _guarded(fn, False)
        return decorator(guarded)

    return wrap


# --------------------------------------------------------------------------------------------
# annotation introspection
#
# A pydantic field carries only its raw annotation; these answer questions such as "which types
# does this Optional/Union accept" with `typing` introspection.
# --------------------------------------------------------------------------------------------


def _number_as_text(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


#: Text that is naturally written as a number -- a speed grade (`speed = -1`), a device generation.
#: Settings declare it field by field; nowhere else is a number accepted for text.
Code = Annotated[
    str,
    BeforeValidator(_number_as_text, json_schema_input_type=str | int | float),
]


def annotation_args(annotation: Any) -> Tuple[Any, ...]:
    """The members of a Union/Optional annotation, or the annotation itself."""
    if get_origin(annotation) in (Union, UnionType):
        return tuple(a for a in get_args(annotation) if a is not type(None))
    return (annotation,)


def field_annotation(model: Any, name: Optional[str]) -> Any:
    """The declared annotation of `name` on a model class, or None."""
    if not name:
        return None
    info = getattr(model, "model_fields", {}).get(name)
    return info.annotation if info is not None else None


@cache
def input_names(model: Type[BaseModel]) -> Dict[str, str]:
    """The field every simple key a model's input may use stands for: each field's name, its
    `alias` and every text choice of its `validation_alias`. A field's own name wins a clash."""
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


# --------------------------------------------------------------------------------------------
# shorthand forms and the tables they stand for
#
# A key that takes a table often takes other forms too: a clock as a port name, `fpga` as a part
# number, `parameters` as a list of `{name, value}` objects. Each form means exactly one table.
# When two layers meet (a target over its design, a settings layer over the one below), both
# sides are read as the tables they stand for before it is decided to merge them key by key or
# to replace one by the other. The one place a model says what its forms mean is its
# `as_mapping` classmethod (or, for a field whose type is no model, its `field_shorthands`
# entry), which the model's own validator calls too: nothing converts a form twice.
# --------------------------------------------------------------------------------------------


class _Whole:
    """The type of `WHOLE`."""

    def __repr__(self) -> str:
        return "WHOLE"


#: What `as_mapping` returns for a form that is valid but is no table, so it cannot be merged
#: key by key: `cocotb: false` (no cocotb) and a platform's name (a whole model, read from a
#: file). Such a form replaces what is below it, and is replaced by what is above it.
WHOLE = _Whole()


def unspecified(value: Any) -> Any:
    """A `field_shorthands` entry for a field that takes `None` to say that nothing is given
    (`flows:` with no table, `clocks: null`): the value is `WHOLE`, so it replaces what is below
    it as it always has."""
    return WHOLE if value is None else None


def field_shorthands_of(model: Type[BaseModel]) -> Dict[str, Callable[[Any], Any]]:
    """The `field_shorthands` of `model`: its own entries and its bases', walking the MRO. A
    subclass adds to what its bases declare, and its entry for the same field wins."""
    table: Dict[str, Callable[[Any], Any]] = {}
    for klass in reversed(model.__mro__):
        table.update(vars(klass).get("field_shorthands", {}))
    return table


def appended_fields_of(model: Type[BaseModel]) -> Tuple[str, ...]:
    """The `appended_fields` of `model`: its own and its bases', in the order of the MRO from
    the base."""
    fields: Dict[str, None] = {}
    for klass in reversed(model.__mro__):
        fields.update(dict.fromkeys(vars(klass).get("appended_fields", ())))
    return tuple(fields)


def annotation_kind(annotation: Any) -> Tuple[str, Optional[Type["XedaBaseModel"]], bool]:
    """What a field's annotation says about a value written as a table: `("model", M, optional)`
    for a field that holds the model `M`, `("dict", None, optional)` for one that holds a
    mapping, `("other", None, optional)` for anything else, a union with another alternative
    (`str | list | M`) included: such a field may legitimately hold a value that is no table."""
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
    """The model a table written for a field of this annotation is validated as, if the field
    holds nothing else."""
    kind, model, _ = annotation_kind(annotation)
    return model if kind == "model" else None


def canonical_names(values: Mapping[str, Any], model: Type[BaseModel]) -> Dict[str, Any]:
    """`values`' keys at one level of `model`, each as the model spells the field (an alias and
    its field are one setting). A field given under two spellings keeps both, as written, so
    validation reports the ambiguity instead of one spelling silently winning."""
    names = input_names(model)
    targets = [names.get(key, key) for key in values]
    duplicates = {target for target, count in Counter(targets).items() if count > 1}
    canonical: Dict[str, Any] = {}
    for (key, value), target in zip(values.items(), targets):
        canonical[key if target in duplicates else target] = value
    return canonical


def canonical_tree(model: Type[BaseModel], values: Mapping[str, Any]) -> Dict[str, Any]:
    """`canonical_names` at every level of nested models."""
    tree = canonical_names(values, model)
    for key, value in tree.items():
        child = nested_model(field_annotation(model, input_names(model).get(key)))
        if child is not None and isinstance(value, Mapping):
            tree[key] = canonical_tree(child, value)
    return tree


def mapping_form(owner: Type[BaseModel], field: Optional[str], value: Any) -> Any:
    """The table that `value`, written for `owner`'s field `field`, stands for: `value` when it
    is a table; the table of a shorthand form; `WHOLE` for a valid form that is no table; `None`
    for anything else (a mistake, which validation reports)."""
    if isinstance(value, Mapping):
        return value
    if not field or field not in getattr(owner, "model_fields", {}):
        return None
    child = nested_model(owner.model_fields[field].annotation)
    if child is not None and isinstance(value, child):
        return WHOLE  # a model already built is a whole value
    hook = field_shorthands_of(owner).get(field)
    if hook is None:
        hook = child.as_mapping if child is not None else None
    if hook is None:
        return None
    try:
        return hook(value)
    except (ValueError, TypeError):
        return None


def expand_forms(model: Type[BaseModel], values: Mapping[str, Any]) -> Dict[str, Any]:
    """`values` (a table written for `model`) with every shorthand form read as the table it
    stands for, through every level of nested models. Anything that is no form is kept as it is."""
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
    """Whether `value` is no table where `owner`'s `field` takes one: not a table, not a
    shorthand form of one (`mapping_form`), not a valid form that is no table (`WHOLE`), and not
    `None` for an optional field. A field that takes other values than a table is never judged."""
    kind, _, optional = annotation_kind(owner.model_fields[field].annotation)
    if kind == "other":
        return False
    form = mapping_form(owner, field, value)
    return not (isinstance(form, Mapping) or form is WHOLE or (value is None and optional))


def shape_problems(
    model: Type[BaseModel], values: Mapping[str, Any]
) -> List[Tuple[Tuple[str, ...], Any]]:
    """Where `values` (a table written for `model`) holds a mistake (`is_mistake`) at a key that
    takes a table: `(key path, value)`, the keys as they are written, through every level of
    nested models. A key that is no field is judged only if the model says so
    (`XedaBaseModel.form_problems`). This is a check of shape, not a validation."""
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
        child = nested_model(model.model_fields[field].annotation)
        form = mapping_form(model, field, value)
        if child is not None and isinstance(form, Mapping):
            problems += [((key, *path), v) for path, v in shape_problems(child, form)]
    return problems


#: The role of a setting whose paths a flow writes, as a `json_schema_extra` marker: a
#: *working* location (its build or report directory, its log) is a name inside the run
#: directory; a *deliverable* is a name there, or a location it is delivered to after the run
#: (`xeda.deliver`). A path field without a role is read.
WORKING_ROLE = "working"
DELIVERABLE_ROLE = "deliverable"
WORKING: Dict[str, Any] = {"x-xeda-writes": WORKING_ROLE}


def deliverable(conventional: Optional[str] = None) -> Dict[str, Any]:
    """The marker of a setting whose path is an output the user may want delivered, with its
    `conventional` name: what the run writes it as when the setting is given a location
    (`{design}` stands for the design's name; by convention `outputs/<design>.<ext>`); None: the
    setting's own default name."""
    marker: Dict[str, Any] = {"x-xeda-writes": DELIVERABLE_ROLE}
    if conventional is not None:
        marker["x-xeda-output"] = conventional
    return marker


def written_role(model: type[BaseModel], field: str) -> Optional[str]:
    """The role of `model`'s `field` (`WORKING_ROLE`, `DELIVERABLE_ROLE`), None if it is read."""
    info = model.model_fields.get(field)
    extra = info.json_schema_extra if info is not None else None
    role = extra.get("x-xeda-writes") if isinstance(extra, dict) else None
    return role if isinstance(role, str) else None


def conventional_output(model: type[BaseModel], field: str, design: str) -> Optional[PurePath]:
    """The name `model`'s deliverable `field` is written under, in the run directory, when the
    setting is given a location: its marker's conventional name for the design `design`, or else
    the field's default name; None if it is no deliverable or has neither."""
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


def field(
    default: Any = attrs.NOTHING,
    *,
    description: Optional[str] = None,
    validator_: Optional[Callable[..., None]] = None,
    converter: Optional[Callable[..., Any]] = None,
    factory: Optional[Callable[[], Any]] = None,
    on_setattr: Any = None,
    **kwargs: Any,
) -> Any:
    metadata = None
    if description is not None:
        metadata = {"description": description}
    return attrs.field(
        default=default,
        validator=validator_,
        converter=converter,
        factory=factory,
        on_setattr=on_setattr,
        metadata=metadata,
        **kwargs,
    )


def asdict(inst: Any, filter_: Optional[Callable[..., bool]] = None) -> Dict[str, Any]:
    if isinstance(inst, BaseModel):
        assert filter_ is None
        return inst.model_dump()
    return attrs.asdict(inst, filter=filter_)


#: The words YAML 1.1 (and pydantic's lax booleans) read as a boolean. xeda reads YAML 1.2, where
#: they are text, and a setting accepts exactly its declared type, so they are never a boolean.
_TRUE_WORDS = frozenset({"y", "yes", "on", "t"})
_FALSE_WORDS = frozenset({"n", "no", "off", "f"})


def _is_boolean_annotation(annotation: Any) -> bool:
    """Whether `annotation` is `bool` or `Optional[bool]`."""
    accepted = annotation_args(annotation) if annotation is not None else ()
    return bool(accepted) and all(a is bool for a in accepted)


def boolean_text_message(text: str) -> str:
    """What to say about `text` given where a boolean is required."""
    word = text.strip().lower()
    if word in _TRUE_WORDS:
        return f"`{text}` is text, not a boolean: write `true`"
    if word in _FALSE_WORDS:
        return f"`{text}` is text, not a boolean: write `false`"
    return f"`{text}` is text, not a boolean: write `true` or `false`"


def yaml11_hint(value: Any) -> Optional[str]:
    """For a value that does not fit a field, what YAML 1.2 wants written, when the value is a
    word YAML 1.1 read as a boolean (`on`, `no`)."""
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return f"`{value}` is text in xeda YAML (YAML 1.2): write `true`"
    if word in _FALSE_WORDS:
        return f"`{value}` is text in xeda YAML (YAML 1.2): write `false`"
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
    #: list of `{name, value}`): field name -> function from a form to its table, or `None`
    #: when the value is not one of the field's forms. A subclass adds its entries to its bases'
    #: (`field_shorthands_of` walks the MRO); an entry for the same field wins.
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}
    #: Fields that are lists which a layer adds to instead of replacing, when the merge asks for
    #: it (`merge_layers(append=True)`, a target's merge over its design's `sources`). A subclass
    #: adds to its bases' (`appended_fields_of`).
    appended_fields: ClassVar[Tuple[str, ...]] = ()

    @classmethod
    def as_mapping(cls, value: Any) -> Any:
        """The table that `value`, a form other than a table, stands for as this model: its
        validator reads the form by this function, and a merge of two layers reads both sides
        by it. `WHOLE` for a valid form that is no table, `None` for a value that is no form of
        this model. A model without forms keeps this default."""
        return None

    @classmethod
    def form_problems(cls, values: Mapping[str, Any]) -> List[Tuple[str, Any]]:
        """The keys of `values` (a table written for this model) that hold a mistake though they
        are no fields, as `(key, value)`: a model with input-only spellings (a design's `clock`)
        overrides this, so that `shape_problems` judges them too. The default judges none."""
        return []

    @classmethod
    def merge_inputs(cls, merged: Dict[str, Any], values: Dict[str, Any]) -> None:
        """Merge the keys of `values` that this model merges by its own rule into `merged`
        (what is below them), and remove them from `values`. Both are canonical tables of this
        model. A model whose fields have several spellings with one meaning (a design's clock)
        overrides this; the default merges nothing."""

    @_pydantic_model_validator(mode="before")
    @classmethod
    def _booleans_are_not_text(cls, values: Any) -> Any:
        """A `bool` or `Optional[bool]` field given text or a number is an error, unless the
        text is `true` or `false` (a command line has no other way to write a boolean):
        pydantic's lax booleans would read `yes`, `on`, `y` and `1` as `True`, but a setting
        accepts exactly its declared type. Every model of xeda -- flow settings, the design,
        nested models -- shares the rule. The input is only read, never rewritten."""
        if not isinstance(values, dict):
            return values
        names = input_names(cls)
        problems = []
        for key, value in values.items():
            if not (isinstance(key, str) and key in names):
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
        # A `cached_property` caches in the instance `__dict__` whichever class along the MRO
        # declares it, so every base must be searched: `VivadoTool` inherits `Tool.version`.
        """Discard inherited cached properties after model state changes."""
        for klass in type(self).__mro__:
            for key, value in vars(klass).items():
                if isinstance(value, cached_property) and key not in type(self).model_fields:
                    log.debug("invalidating: %s", str(key))
                    self.__dict__.pop(key, None)


_XedaModelType = TypeVar("_XedaModelType", bound=XedaBaseModel)


@cache
def model_with_allow_extra(cls: Type[_XedaModelType]) -> Type[_XedaModelType]:
    """A subclass of `cls` that tolerates unknown keys.

    pydantic v2 compiles a model's validator when the class is created, so mutating
    `model_config` on an existing class has no effect -- building a subclass is the only form
    that actually re-runs schema construction, and it keeps the relaxation scoped to the
    subclass.

    The result is cached, and instances provide a custom reducer that rebuilds the derived class
    in a child process. The DSE runner ships a `Design` across a process boundary via `pebble`, so
    relying on pickle to resolve this runtime-only class by `__module__` + `__qualname__` fails.
    """
    name = f"{cls.__name__}AllowExtra"

    def __reduce__(self):
        # The class is built at runtime, so a child process (spawn) has no such attribute to
        # look up. Pickle the *base* -- an ordinary importable class -- and rebuild on the
        # far side. The DSE runner ships designs to `pebble` workers this way.
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
    """Unpickle counterpart of `model_with_allow_extra` (module-level, so it pickles)."""
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
    """The message of a list given as text that is spelled as a list (`ListLiteralText`), for a
    field of a nested model, with the key as the command line writes it from the top
    (`cocotb.testcase`, not `testcase`): the model that refused it knows only its own field."""
    loc = error.get("loc", ())
    context = error.get("ctx") or {}
    if error.get("type") == "list_text" and len(loc) > 1:
        key = ".".join(str(part) for part in loc)
        return LIST_TEXT_MESSAGE.format(shown=context.get("shown", ""), key=key)
    return None


def _with_yaml_hint(error: ErrorDetails) -> str:
    """The message of one error, saying what to write when YAML 1.2 changed the input: a word
    YAML 1.1 read as a boolean (`ncpus: on`), or a number where text is needed (`top: 010`)."""
    msg = str(error.get("msg"))
    if error.get("type") in ("boolean_text", "extra_forbidden"):
        return msg
    value = error.get("input")
    hint = yaml11_hint(value)
    if hint is None and error.get("type") == "string_type" and isinstance(value, (int, float)):
        shown = repr(value)
        hint = f'`{shown}` is a number in xeda YAML (YAML 1.2): write `"{shown}"` for text'
    return f"{msg}; {hint}" if hint else msg
