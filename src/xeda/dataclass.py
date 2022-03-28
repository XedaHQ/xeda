from abc import ABCMeta
from typing import Any, Callable, Dict, List, Optional, Type, Union, Tuple
import pydantic
import attrs
from pydantic import Field, validator, Extra, ValidationError
from pydantic.error_wrappers import display_errors
from pathlib import Path
from datetime import datetime
import logging


__all__ = [
    "XedaBaseModel",
    "XedaBaseModeAllowExtra",
    "Field",
    "validator",
    "Extra",
    "asdict",
    "define",
    "ValidationError",
    "validation_errors",
]


log = logging.getLogger(__name__)


def define(maybe_cls: Optional[Type[Any]], **kwargs: Any) -> Any:
    return attrs.define(maybe_cls, **kwargs)


def field(
    default: Any = attrs.NOTHING,
    *,
    description: Optional[str] = None,
    validator: Optional[Callable[..., None]] = None,
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
        validator=validator,
        converter=converter,
        factory=factory,
        on_setattr=on_setattr,
        metadata=metadata,
        **kwargs,
    )


def asdict(inst: Any, filter: Optional[Callable[..., bool]] = None) -> Dict[str, Any]:
    if isinstance(inst, pydantic.BaseModel):
        assert filter is None
        return inst.dict()
    elif True: # FIXME
        return attrs.asdict(inst, filter=filter)


class XedaBaseModel(pydantic.BaseModel):
    class Config(pydantic.BaseConfig):
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
