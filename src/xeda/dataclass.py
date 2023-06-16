"""Interchangable dataclass abstraction"""
from __future__ import annotations
import copy

import logging
from abc import ABCMeta
from functools import cached_property
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar

import attrs

# pylint: disable=no-name-in-module
from pydantic import (
    BaseConfig,
    BaseModel,
    Extra,
    Field,
    ValidationError,
    root_validator,
    validator,
)

if TYPE_CHECKING:
    from pydantic.error_wrappers import ErrorDict

__all__ = [
    "XedaBaseModel",
    "XedaBaseModelAllowExtra",
    "Field",
    "validator",
    "root_validator",
    "Extra",
    "asdict",
    "ValidationError",
    "validation_errors",
]

log = logging.getLogger(__name__)


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
        keep_untouched = (cached_property,)  # https://github.com/samuelcolvin/pydantic/issues/1241
        use_enum_values = True
        allow_population_by_field_name = True

    def invalidate_cached_properties(self):
        for key, value in self.__class__.__dict__.items():
            if isinstance(value, cached_property):
                log.debug("invalidating: %s", str(key))
                self.__dict__.pop(key, None)


class XedaBaseModelAllowExtra(XedaBaseModel, metaclass=ABCMeta):
    class Config(XedaBaseModel.Config):
        extra = Extra.allow


_XedaModelType = TypeVar("_XedaModelType", bound=XedaBaseModel)


def model_with_allow_extra(cls: Type[_XedaModelType]) -> Type[_XedaModelType]:
    cls_copy = copy.deepcopy(cls)
    cls_copy.Config.extra = Extra.allow
    return cls_copy


def validation_errors(
    errors: List[ErrorDict],
) -> List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]:
    return [
        (
            " -> ".join(str(loc) for loc in e.get("loc", [])),
            e.get("msg"),
            "".join(f"; {k}={v}" for k, v in e.get("ctx", {}).items()),
            e.get("type"),
        )
        for e in errors
    ]
