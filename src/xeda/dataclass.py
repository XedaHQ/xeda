"""Interchangable dataclass abstraction"""

from __future__ import annotations

import logging
from copy import deepcopy
from functools import cache, cached_property, wraps
from inspect import signature
from types import UnionType
from typing import (
    Annotated,
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
from pydantic_core import ErrorDetails, PydanticUndefined

__all__ = [
    "AliasChoices",
    "Code",
    "ConfigDict",
    "ErrorDetails",
    "Field",
    "FieldInfo",
    "PrivateAttr",
    "PydanticUndefined",
    "SerializeAsAny",
    "ValidationError",
    "XedaBaseModel",
    "accepts_non_mapping",
    "annotation_args",
    "asdict",
    "field_validator",
    "model_validator",
    "validation_errors",
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
        # A default goes through the same validators as a given value, so leaving a setting out
        # and writing its default explicitly (as a saved `settings.json` does) are the same.
        validate_default=True,
    )

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
            e.get("msg"),
            "".join(f"; {k}={v}" for k, v in (e.get("ctx") or {}).items()),
            e.get("type"),
        )
        for e in errors
    ]
