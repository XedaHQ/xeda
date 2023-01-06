from typing import Type, TypeVar

from ._flow import Flow
from .sim import SimFlow
from .synth import SynthFlow

__all__ = [
    "define_flow",
    "synth_flow",
    "sim_flow",
]

FlowClass = TypeVar("FlowClass", bound=Type[Flow])


# add base class
def _define_flow(cls, base_class: FlowClass) -> FlowClass:
    assert issubclass(base_class, Flow)
    if base_class not in cls.__bases__:
        # FIXME do we need to add existing __bases__?
        cls = type(cls.__name__, (cls, base_class), {})
        cls.__init_subclass__()
    assert issubclass(cls, base_class)
    assert hasattr(cls, "Settings")
    if base_class.Settings not in cls.Settings.__bases__:
        cls.Settings = type(cls.__name__, (cls.Settings, base_class.Settings), {})
    return cls  # pyright: ignore


# decorator to auto-inherit from Flow
def define_flow(maybe_cls=None, *, base: FlowClass):  # pyright: ignore
    def wrap(cls):
        return _define_flow(cls, base)

    if maybe_cls is None:
        return wrap
    else:
        return wrap(maybe_cls)


def synth_flow(cls):
    return define_flow(cls, base=SynthFlow)


def sim_flow(cls):
    return define_flow(cls, base=SimFlow)
