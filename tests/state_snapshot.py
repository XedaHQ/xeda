"""What a launch may change in the process, and how to see that it did not.

Three views of "state that outlives a launch", each a function of the running interpreter:

- `state()` is a deep, comparable picture of everything the package keeps at module and class
  level: every module variable, every class attribute (instances held there included), every
  function default and every pydantic field default of `xeda.*`. A launch that writes into a
  table, a default or a class-held object shows as a difference between two pictures.
- `reachable()` and `shared_objects()` find the mutable objects a finished flow shares with that
  state. Sharing is how a write into a flow's own settings becomes a write into a table.
- `process_state()` is what the process holds besides the package: the working directory, the
  environment, `sys.path`, the logging configuration and the warning filters.

Not seen: what a foreign library keeps (`rich`, `click`, `jinja2`, `pint`: named, not entered),
caches inside functions (`functools.cache`), what a child process does, and state held in a
closure. Pydantic keeps lazily built internals on its classes (`__pydantic_*`, `__slotnames__`);
every dunder class attribute is left out for that, except the fields' defaults.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import importlib
import logging
import os
import pkgutil
import re
import sys
import tempfile
import types
import warnings
from collections import deque
from pathlib import PurePath
from typing import Any

import click
from pydantic import BaseModel
from pydantic.fields import FieldInfo

import xeda

#: Module variables that are Python's own bookkeeping.
MODULE_BOOKKEEPING = frozenset(
    {
        "__builtins__",
        "__spec__",
        "__loader__",
        "__cached__",
        "__file__",
        "__package__",
        "__name__",
        "__doc__",
        "__path__",
        "__annotations__",
        "__all__",
        "__conditional_annotations__",
    }
)
#: The one dunder class attribute that is state: the defaults of a model's fields.
KEPT_DUNDERS = frozenset({"__pydantic_fields__"})
DESCRIPTORS = (property, staticmethod, classmethod, functools.cached_property)
FOREIGN_PACKAGES = frozenset({"click", "cloup", "click_extra", "rich", "jinja2", "pint", "yaml"})
MAX_DEPTH = 14


def import_package() -> None:
    """Import every module of `xeda`: lazily imported modules would otherwise appear in a picture
    taken after a launch."""
    for info in pkgutil.walk_packages(xeda.__path__, "xeda."):
        importlib.import_module(info.name)


def _modules() -> list[types.ModuleType]:
    names = sorted(name for name in list(sys.modules) if name == "xeda" or name.startswith("xeda."))
    return [sys.modules[name] for name in names if sys.modules[name] is not None]


class _Picture:
    """One walk over the objects: what it has entered (an object is entered once, so a cycle ends),
    and the temporary containers it made, kept alive so that no `id` is reused while it counts."""

    def __init__(self) -> None:
        self.entered: set[int] = set()
        self.keep: list[Any] = []

    def of(self, obj: Any, depth: int = 0) -> Any:  # noqa: C901 - one case per kind of object
        if obj is None or isinstance(obj, (bool, int, float, complex, str, bytes)):
            return obj
        if isinstance(obj, PurePath):
            return ("path", str(obj))
        if isinstance(obj, enum.Enum):
            return ("enum", type(obj).__qualname__, obj.name)
        if isinstance(obj, (types.ModuleType, types.FunctionType, types.BuiltinFunctionType, type)):
            return ("named", getattr(obj, "__module__", None), getattr(obj, "__qualname__", None))
        if isinstance(obj, types.MethodType):
            return ("method", getattr(obj.__func__, "__qualname__", None))
        if isinstance(obj, functools.partial):
            return ("partial", self.of(obj.func, depth + 1), self.of(obj.args, depth + 1))
        if isinstance(obj, re.Pattern):
            return ("re", obj.pattern, obj.flags)
        if isinstance(obj, logging.Logger):
            return ("logger", obj.name)  # the level of a logger is `process_state`'s
        if isinstance(obj, DESCRIPTORS) or hasattr(obj, "cache_clear"):
            return ("descriptor", type(obj).__qualname__)
        if id(obj) in self.entered:
            return ("seen",)
        self.entered.add(id(obj))
        self.keep.append(obj)  # its `id` is not reused while the picture is made
        package = (type(obj).__module__ or "").split(".")[0]
        if isinstance(obj, (click.Command, click.Parameter, click.Context)):
            commands = getattr(obj, "commands", None)
            return ("click", type(obj).__qualname__, sorted(commands) if commands else None)
        if package == "rich" and type(obj).__qualname__ == "Console":
            # where the console writes is state: `--json` redirects it for a command's length
            target = getattr(obj, "_file", None)
            return ("console", None if target is None else type(target).__qualname__)
        if package in FOREIGN_PACKAGES:
            return ("foreign", package, type(obj).__qualname__)
        if depth > MAX_DEPTH:
            return ("too deep", type(obj).__qualname__)
        return self._container(obj, depth + 1)

    def _container(self, obj: Any, depth: int) -> Any:
        if isinstance(obj, dict):
            return ("map", [(self.of(k, depth), self.of(v, depth)) for k, v in obj.items()])
        if isinstance(obj, (list, tuple, deque)):
            return ("seq", type(obj).__qualname__, [self.of(v, depth) for v in obj])
        if isinstance(obj, (set, frozenset)):
            # an order that does not depend on how the objects hash
            apart = sorted(obj, key=lambda v: repr(_Picture().of(v, depth)))
            return ("set", [self.of(v, depth) for v in apart])
        if type(obj).__qualname__ == "ModelPrivateAttr":
            return ("private", self.of(getattr(obj, "default", None), depth))
        if type(obj).__qualname__ == "ContextVar":
            return ("contextvar", obj.name, self.of(obj.get(None), depth))
        if isinstance(obj, dataclasses.Field):
            return ("field", obj.name, self.of(obj.default, depth))
        if isinstance(obj, FieldInfo):
            extra = obj.json_schema_extra
            return (
                "field",
                self.of(obj.default, depth),
                repr(obj.annotation),
                self.of(extra, depth),
            )
        if isinstance(obj, BaseModel):
            return self._attributes(
                "model", obj, dict(obj.__dict__), getattr(obj, "__pydantic_extra__", None), depth
            )
        if dataclasses.is_dataclass(obj):
            fields = {f.name: getattr(obj, f.name, None) for f in dataclasses.fields(obj)}
            return self._attributes("dataclass", obj, fields, None, depth)
        if (type(obj).__module__ or "").split(".")[0] == "xeda" and hasattr(obj, "__dict__"):
            return self._attributes("object", obj, dict(vars(obj)), None, depth)
        return ("opaque", type(obj).__module__, type(obj).__qualname__)

    def _attributes(self, kind: str, obj: Any, attributes: dict, extra: Any, depth: int) -> Any:
        self.keep.append(attributes)
        return (kind, type(obj).__qualname__, self.of(attributes, depth), self.of(extra, depth))


def _classes_of(module: types.ModuleType):
    """Every class the module defines, the ones nested in a class (`Flow.Settings`) included, as
    (name, class). A nested class is state too: the defaults of a flow's settings are in it."""
    pending = [
        (name, value)
        for name, value in list(vars(module).items())
        if isinstance(value, type) and value.__module__ == module.__name__
    ]
    while pending:
        name, cls = pending.pop(0)
        yield name, cls
        pending.extend(
            (f"{name}.{attribute}", value)
            for attribute, value in list(vars(cls).items())
            if isinstance(value, type)
            and value.__module__ == module.__name__
            and value.__qualname__.startswith(cls.__qualname__ + ".")
        )


def _class_state(cls: type, picture: _Picture) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for name, value in list(vars(cls).items()):
        if name.startswith("__") and name not in KEPT_DUNDERS:
            continue
        function = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
        if isinstance(function, types.FunctionType):
            state[f"{name}()"] = picture.of((function.__defaults__, function.__kwdefaults__))
        elif not isinstance(value, DESCRIPTORS):
            state[name] = picture.of(value)
    return state


def state() -> dict[str, Any]:
    """The package's module and class level state, by name."""
    picture = _Picture()
    out: dict[str, Any] = {}
    for module in _modules():
        for name, value in list(vars(module).items()):
            if name in MODULE_BOOKKEEPING or isinstance(value, (types.ModuleType, type)):
                continue
            where = f"{module.__name__}.{name}"
            if isinstance(value, types.FunctionType):
                if value.__module__ == module.__name__:
                    out[f"{where}()"] = picture.of((value.__defaults__, value.__kwdefaults__))
            else:
                out[where] = picture.of(value)
        for name, cls in _classes_of(module):
            for key, picture_of in _class_state(cls, picture).items():
                out[f"{module.__name__}.{name}.{key}"] = picture_of
    return out


def changes(before: Any, after: Any, where: str = "") -> list[str]:
    """What differs between two pictures, one line for each place, naming it."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        lines: list[str] = []
        for key in sorted(before.keys() | after.keys()):
            if key not in before:
                lines.append(f"{where}/{key}: added")
            elif key not in after:
                lines.append(f"{where}/{key}: removed")
            else:
                lines += changes(before[key], after[key], f"{where}/{key}")
        return lines
    if (
        isinstance(before, tuple)
        and isinstance(after, tuple)
        and before[:1] == after[:1] == ("map",)
    ):
        old, new = dict(before[1]), dict(after[1])
        lines = []
        for key in old.keys() | new.keys():
            name = f"{where}[{key!r}]"
            if key not in old:
                lines.append(f"{name}: added {_short(new[key])}")
            elif key not in new:
                lines.append(f"{name}: removed")
            else:
                lines += changes(old[key], new[key], name)
        return sorted(lines)
    if isinstance(before, (tuple, list)) and isinstance(after, (tuple, list)):
        if len(before) != len(after):
            return [f"{where}: {len(before)} items -> {len(after)}: {_short(after)}"]
        return [
            line
            for i, (x, y) in enumerate(zip(before, after))
            for line in changes(x, y, f"{where}.{i}")
        ]
    return [f"{where}: {_short(before)} -> {_short(after)}"]


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


# --- aliasing --------------------------------------------------------------------------------

_NOT_MUTABLE = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    PurePath,
    enum.Enum,
    re.Pattern,
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    type,
    logging.Logger,
    types.CodeType,
    functools.partial,
)


def _children(obj: Any):
    """The (label, child) pairs through which an object holds others."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"[{key!r}]", value
    elif isinstance(obj, (list, tuple, set, frozenset, deque)):
        for i, value in enumerate(obj):
            yield f"[{i}]", value
    elif isinstance(obj, BaseModel):
        for key, value in dict(obj.__dict__).items():
            yield f".{key}", value
        for key, value in (getattr(obj, "__pydantic_extra__", None) or {}).items():
            yield f".{key}", value
        for key, value in (getattr(obj, "__pydantic_private__", None) or {}).items():
            yield f"._{key}", value
    elif dataclasses.is_dataclass(obj):
        for field in dataclasses.fields(obj):
            yield f".{field.name}", getattr(obj, field.name, None)
    elif type(obj).__qualname__ == "Environment" and type(obj).__module__.startswith("jinja2"):
        for key in ("globals", "filters", "tests", "policies"):
            yield f".{key}", getattr(obj, key)
    elif (type(obj).__module__ or "").split(".")[0] == "xeda" and hasattr(obj, "__dict__"):
        for key, value in dict(vars(obj)).items():
            yield f".{key}", value


def reachable(root: Any, label: str, found: dict[int, tuple[str, str]] | None = None) -> dict:
    """Every mutable object reachable from `root`, by `id`, with the first path to it and its type."""
    found = {} if found is None else found
    entered: set[int] = set()
    stack = [(label, root)]
    while stack:
        path, obj = stack.pop()
        if isinstance(obj, _NOT_MUTABLE) or isinstance(obj, (click.Command, click.Parameter)):
            continue
        if id(obj) in entered:
            continue
        entered.add(id(obj))
        if not isinstance(obj, (tuple, frozenset)):
            found.setdefault(id(obj), (path, type(obj).__qualname__))
        stack.extend((path + sub, child) for sub, child in _children(obj))
    return found


def shared_objects() -> dict[int, tuple[str, str]]:
    """Every mutable object held by a module variable, a class attribute or a function default."""
    found: dict[int, tuple[str, str]] = {}
    for module in _modules():
        for name, value in list(vars(module).items()):
            if name in MODULE_BOOKKEEPING or isinstance(value, (types.ModuleType, type)):
                continue
            where = f"{module.__name__}.{name}"
            if isinstance(value, types.FunctionType):
                if value.__module__ == module.__name__:
                    reachable((value.__defaults__, value.__kwdefaults__), f"{where}()", found)
            else:
                reachable(value, where, found)
        for name, cls in _classes_of(module):
            for attribute, held in list(vars(cls).items()):
                if attribute.startswith("__") or isinstance(held, (type, *DESCRIPTORS)):
                    continue
                label = f"{module.__name__}.{name}.{attribute}"
                if isinstance(held, types.FunctionType):
                    reachable((held.__defaults__, held.__kwdefaults__), f"{label}()", found)
                else:
                    reachable(held, label, found)
    return found


# --- the process -----------------------------------------------------------------------------


def process_state() -> dict[str, Any]:
    """What the process holds apart from the package. The environment leaves out the variable
    that pytest sets for each test."""
    root = logging.getLogger()
    loggers = {
        name: (logger.level, tuple(map(id, logger.handlers)), logger.propagate, logger.disabled)
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    return {
        "working directory": os.getcwd(),
        "environment": {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"},
        "sys.path": list(sys.path),
        "root logger": (root.level, tuple(map(id, root.handlers))),
        "loggers": loggers,
        "warning filters": [repr(f) for f in warnings.filters],
        "temporary directory": tempfile.tempdir,
    }


def process_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in before:
        if key == "loggers":
            lines += [
                f"logger {name!r}: {before[key][name]} -> {after[key].get(name)}"
                for name in before[key]
                if before[key][name] != after[key].get(name)
            ]
        elif key == "environment":
            lines += [
                f"environment {name}: {before[key].get(name)!r} -> {after[key].get(name)!r}"
                for name in sorted(before[key].keys() | after[key].keys())
                if before[key].get(name) != after[key].get(name)
            ]
        elif before[key] != after[key]:
            lines.append(f"{key}: {_short(before[key])} -> {_short(after[key])}")
    return lines


def models_held_by(cls: type) -> list[str]:
    """The class attributes of a class and its bases that hold a pydantic model instance (a
    `Tool`, a `Docker`, a settings object): an instance is state, and a class is not an owner."""
    return [
        f"{klass.__qualname__}.{name}"
        for klass in cls.__mro__
        for name, value in vars(klass).items()
        if isinstance(value, BaseModel)
    ]
