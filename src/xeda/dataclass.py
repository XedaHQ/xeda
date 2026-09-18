"""Interchangable dataclass abstraction"""

from __future__ import annotations

import logging
from copy import deepcopy
from functools import cache, cached_property, wraps
from inspect import signature
from types import UnionType
from typing import (
    Any,
    Callable,
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
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, ValidationError
from pydantic import field_validator as _pydantic_field_validator
from pydantic import model_validator as _pydantic_model_validator
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, PydanticUndefined

__all__ = [
    "ConfigDict",
    "ErrorDetails",
    "Field",
    "FieldInfo",
    "PydanticUndefined",
    "SerializeAsAny",
    "ValidationError",
    "XedaBaseModel",
    "accepts_non_mapping",
    "annotation_accepts",
    "annotation_args",
    "annotation_is_list",
    "asdict",
    "field_validator",
    "model_validator",
    "validation_errors",
]

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------
# validator decorators
#
# pydantic v1 treated a `TypeError` raised inside a validator as a validation failure, exactly
# like `ValueError`/`AssertionError`. v2 does not: a `TypeError` propagates out of
# `model_validate` untouched, so a plain user mistake (`sources = 123`, `freq = []`) escapes as a
# raw traceback on the CLI and is reported under the wrong class by `--json`.
#
# Validators should still guard their own inputs and raise `ValueError` with a useful message --
# these wrappers are the safety net that keeps a missed guard from turning into a crash, and they
# apply to every current and future xeda validator without each one having to remember.
# --------------------------------------------------------------------------------------------


def _guarded(fn: Any, copy_input: bool) -> Any:
    """Wrap a validator body with the two behaviours pydantic v1 provided implicitly.

    `copy_input` copies a `dict`/`list` argument for `mode="before"` validators. v1 handed these
    fresh input state; v2 passes the caller's own object straight through, so the very common
    "normalize by writing back into `values`" pattern silently rewrites caller-owned data (a
    design's `flow[...]` section, a `Settings` kwargs dict). The copy must include nested
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
    """Guard a before-model validator while preserving v1 input-isolation semantics.

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
            # v1 only ever ran a `pre=True` root validator on a mapping: `BaseModel.validate`
            # rejected anything else ("value is not a valid dict") first. v2 hands the raw input
            # to a `mode="before"` model validator, so every v1-era body written against
            # `values.get(...)` crashed with `AttributeError` on, say, `fpga = ["x"]`. Pass the
            # input through untouched and pydantic reports its own "valid dictionary" error.
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

    Without it, the shim passes any input that is not a `dict` straight through to pydantic
    (v1's contract for `pre=True` root validators). Apply it *under* `@classmethod`.
    """
    fn.__xeda_accepts_non_mapping__ = True
    return fn


def _is_before(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> bool:
    return kwargs.get("mode", "after") == "before" or "before" in args


def field_validator(*args: Any, **kwargs: Any) -> Any:
    """`pydantic.field_validator` with xeda's v1-compatibility guards. See `_guarded`."""
    decorator = _pydantic_field_validator(*args, **kwargs)
    copy_input = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        return decorator(_guarded(fn, copy_input))

    return wrap


def model_validator(*args: Any, **kwargs: Any) -> Any:
    """A v1-compatible model validator with assignment-aware input isolation."""
    decorator = _pydantic_model_validator(*args, **kwargs)
    before = _is_before(args, kwargs)

    def wrap(fn: Any) -> Any:
        guarded = _guarded_before_model(fn) if before else _guarded(fn, False)
        return decorator(guarded)

    return wrap


# --------------------------------------------------------------------------------------------
# annotation introspection
#
# pydantic v1 exposed a field's "shape" (`SHAPE_LIST`, `SHAPE_SINGLETON`, ...) and its unwrapped
# inner type (`ModelField.type_`). v2 has neither: a field carries only its raw annotation, so the
# same questions are answered with `typing` introspection.
# --------------------------------------------------------------------------------------------


def annotation_args(annotation: Any) -> Tuple[Any, ...]:
    """The members of a Union/Optional annotation, or the annotation itself."""
    if get_origin(annotation) in (Union, UnionType):
        return tuple(a for a in get_args(annotation) if a is not type(None))
    return (annotation,)


def annotation_is_list(annotation: Any) -> bool:
    """True for `list`/`List[...]`, including inside an Optional."""
    return any(a is list or get_origin(a) is list for a in annotation_args(annotation))


def annotation_accepts(annotation: Any, typ: type) -> bool:
    """True if `typ` is one of the types the annotation accepts directly."""
    return any(a is typ for a in annotation_args(annotation))


def _accepts_only_str(annotation: Any) -> bool:
    accepted = annotation_args(annotation)
    return str in accepted and not any(a in (int, float) for a in accepted)


def str_only_element(annotation: Any) -> Optional[str]:
    """`"items"` / `"values"` when the annotation is a container of strings and nothing else.

    Returns which part of the container carries the string, so a caller knows whether to coerce
    a sequence's items or a mapping's values. `None` when the element type would also accept a
    number, so a genuine `List[Union[str, int]]` keeps discriminating normally.
    """
    for a in annotation_args(annotation):
        origin, args = get_origin(a), get_args(a)
        if origin in (list, set, frozenset, tuple) and args:
            elements = [e for e in args if e is not Ellipsis]
            if elements and all(_accepts_only_str(e) for e in elements):
                return "items"
        if origin is dict and len(args) == 2 and _accepts_only_str(args[1]):
            return "values"
    return None


def field_annotation(model: Any, name: Optional[str]) -> Any:
    """The declared annotation of `name` on a model class, or None."""
    if not name:
        return None
    info = getattr(model, "model_fields", {}).get(name)
    return info.annotation if info is not None else None


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


class XedaBaseModel(BaseModel):
    model_config = ConfigDict(
        validate_assignment=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        # https://github.com/samuelcolvin/pydantic/issues/1241
        ignored_types=(cached_property,),
        use_enum_values=True,
        populate_by_name=True,
        # pydantic v1 validators were overwhelmingly declared `always=True`, i.e. they ran even
        # when the field was left at its default. v2 skips defaults unless told otherwise, so
        # restore the v1 behavior model-wide rather than annotating every field.
        validate_default=True,
    )

    @_pydantic_field_validator("*", mode="before")
    @classmethod
    def _coerce_number_to_str(cls, value: Any, info: Any) -> Any:
        """Accept a number where only a string is declared, as pydantic v1 did.

        TOML cannot mark a number as text, so real design and platform files spell string-valued
        settings numerically -- an FPGA `speed = 2` grade, a `name = 2024`, a Vivado
        `set_synth_properties = {{ MAX_BRAM = 0 }}`, a `compile_args = ["-j", 8]`. v1 coerced all
        of those; v2 rejects them, which would break files that have always loaded.

        This applies to a container's elements too, not just scalar fields. Only annotations whose
        (element) type accepts `str` and *not* a numeric type are touched, so a genuine
        `Union[str, int]` still discriminates normally, and `bool` is left alone -- it is an `int`
        subclass, and `True` is not a meaningful name.
        """
        annotation = field_annotation(cls, info.field_name)
        if annotation is None:
            return value
        if type(value) in (int, float):
            return str(value) if _accepts_only_str(annotation) else value
        element = str_only_element(annotation)
        if element == "items" and isinstance(value, (list, tuple, set, frozenset)):
            return type(value)(str(v) if type(v) in (int, float) else v for v in value)
        if element == "values" and isinstance(value, dict):
            return {k: str(v) if type(v) in (int, float) else v for k, v in value.items()}
        return value

    def invalidate_cached_properties(self):
        for key, value in self.__class__.__dict__.items():
            if isinstance(value, cached_property):
                log.debug("invalidating: %s", str(key))
                self.__dict__.pop(key, None)


_XedaModelType = TypeVar("_XedaModelType", bound=XedaBaseModel)


@cache
def model_with_allow_extra(cls: Type[_XedaModelType]) -> Type[_XedaModelType]:
    """A subclass of `cls` that tolerates unknown keys.

    pydantic v2 compiles a model's validator when the class is created, so mutating
    `model_config` on an existing class has no effect -- building a subclass is the only form
    that actually re-runs schema construction. (Under v1 this function called
    `copy.deepcopy(cls)`, which returns the *same* class object for a class, so it silently
    mutated the original globally; the subclass keeps the relaxation scoped.)

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
            e.get("msg"),
            "".join(f"; {k}={v}" for k, v in (e.get("ctx") or {}).items()),
            e.get("type"),
        )
        for e in errors
    ]
