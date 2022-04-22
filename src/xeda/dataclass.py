"""Interchangable dataclass abstraction"""
from __future__ import annotations

import logging
from abc import ABCMeta
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar

import attrs

# pylint: disable=no-name-in-module
from pydantic import (
    BaseModel,
    BaseConfig,
    Extra,
    Field,
    ValidationError,
    root_validator,
    validator,
)

__all__ = [
    "XedaBaseModel",
    "XedaBaseModeAllowExtra",
    "xeda_model",  # decorator
    "Field",
    "validator",
    "root_validator",
    "Extra",
    "asdict",
    "define",
    "ValidationError",
    "validation_errors",
]


log = logging.getLogger(__name__)


# WIP: interchangable dataclass backend
def define(maybe_cls: Optional[Type[Any]], **kwargs: Any) -> Any:
    return attrs.define(maybe_cls, **kwargs)


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
        return inst.dict()
    return attrs.asdict(inst, filter=filter_)


class XedaBaseModel(BaseModel):
    class Config(BaseConfig):
        validate_assignment = True
        extra = Extra.forbid
        arbitrary_types_allowed = True
        use_enum_values = True

    # def validate(self):
    #     *_, validation_error = validate_model(self.__class__, self.__dict__)
    #     if validation_error:
    #         raise validation_error


class XedaBaseModeAllowExtra(XedaBaseModel, metaclass=ABCMeta):
    class Config(XedaBaseModel.Config):
        extra = Extra.allow


def error_type_and_context(error: Dict[str, Any]) -> str:
    ctx = error.get("ctx", {})
    return str(error.get("type")) + "".join(f"; {k}={v}" for k, v in ctx.items())


def validation_errors(errors: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    return [
        (
            " -> ".join(str(loc) for loc in e.get("loc", [])),
            e.get("msg", "???"),
            error_type_and_context(e),
        )
        for e in errors
    ]


T = TypeVar("T", bound=object)


def xeda_model(
    maybe_class: Optional[Type[T]] = None, /, *, allow_extra: bool = False
) -> Type[T]:
    """decorator replacement for dataclasses"""
    # This is a WIP
    # FIXME: does not work with classes with inheritance

    def wrap(clz: Type[T]) -> Type[T]:
        base_model: Type[BaseModel] = (
            XedaBaseModeAllowExtra if allow_extra else XedaBaseModel
        )
        bases = (clz,) if issubclass(clz, base_model) else (clz, base_model)
        eclz = type(clz.__name__, bases, dict(clz.__dict__))

        return eclz

    return wrap if maybe_class is None else wrap(maybe_class)  # type: ignore
