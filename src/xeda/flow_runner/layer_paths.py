"""Resolve the relative paths of one layer of settings against the file that gave the layer.

A relative path of a setting means a path below the directory of the file that wrote it. The
settings of a design resolve against the design root when a flow reads them. The `flows` table
of a project file is another file, in another directory when the project lists a design file
below it, so its relative paths are made absolute against the project directory before the
layers merge. A setting that names a file the flow writes stays as written.
"""

from __future__ import annotations

import os
import types
import typing
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..dataclass import WORKING, input_names

__all__ = ["anchored_sections"]

#: The key of the schema extra that marks a setting the flow writes (`dataclass.WORKING`).
WRITES_KEY = next(iter(WORKING))

#: The key of the schema extra of a text setting that is a file, or the content itself written
#: inline after the prefix that the key gives (`abc_script`).
INLINE_PREFIX_KEY = "x-xeda-inline-prefix"


def _members(annotation: Any) -> tuple[Any, ...]:
    """Return the types that `annotation` allows: the members of a union, or the type itself."""
    if typing.get_origin(annotation) is typing.Annotated:
        return _members(typing.get_args(annotation)[0])
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return tuple(member for arg in typing.get_args(annotation) for member in _members(arg))
    return (annotation,)


def _is_path_type(member: Any) -> bool:
    return isinstance(member, type) and issubclass(member, (os.PathLike, Path))


def _is_relative(value: str) -> bool:
    """Return whether `value` is a path to resolve: not absolute, and not a variable or a home
    directory, which the settings expand where they are read."""
    return bool(value) and not os.path.isabs(value) and not value.startswith(("$", "~"))


def _anchor(annotation: Any, value: Any, base: Path) -> Any:
    members = _members(annotation)
    if isinstance(value, Mapping):
        for member in members:
            if isinstance(member, type) and issubclass(member, BaseModel):
                return _anchored_table(member, value, base)
            origin = typing.get_origin(member)
            if isinstance(origin, type) and issubclass(origin, Mapping):
                arguments = typing.get_args(member)
                if len(arguments) == 2:
                    return {key: _anchor(arguments[1], item, base) for key, item in value.items()}
        return value
    if isinstance(value, (list, tuple)):
        for member in members:
            origin = typing.get_origin(member)
            if isinstance(origin, type) and issubclass(origin, (list, tuple, set, frozenset)):
                element_types = [a for a in typing.get_args(member) if a is not Ellipsis]
                if element_types:
                    anchored = [_anchor(element_types[0], item, base) for item in value]
                    return anchored if isinstance(value, list) else tuple(anchored)
        return value
    if isinstance(value, (str, os.PathLike)) and any(_is_path_type(m) for m in members):
        text = os.fspath(value)
        return str(base / text) if _is_relative(str(text)) else value
    return value


def _anchored_table(model: type[BaseModel], table: Mapping[str, Any], base: Path) -> dict[str, Any]:
    names = input_names(model)
    result: dict[str, Any] = {}
    for key, value in table.items():
        info = model.model_fields.get(names.get(key, key))
        extra = None if info is None else info.json_schema_extra
        if info is None or (isinstance(extra, Mapping) and WRITES_KEY in extra):
            # A file the flow writes is a name in the run directory, wherever it was written.
            result[key] = value
        elif isinstance(extra, Mapping) and INLINE_PREFIX_KEY in extra and isinstance(value, str):
            inline = value.startswith(str(extra[INLINE_PREFIX_KEY]))
            result[key] = str(base / value) if not inline and _is_relative(value) else value
        else:
            result[key] = _anchor(info.annotation, value, base)
    return result


def anchored_sections(
    sections: Mapping[str, Any],
    base: Path,
    settings_class_for: Callable[[str], Any],
) -> dict[str, Any]:
    """Return `sections`, the `flows` table of one file, with the relative path of each setting
    of a flow made absolute against `base`, the directory of that file.

    `settings_class_for(flow name)` returns the settings class of a flow, or None for a flow
    that is not known: its section stays as written. A value that is no path stays as it is."""
    result: dict[str, Any] = {}
    for name, section in sections.items():
        settings = settings_class_for(name)
        if settings is None or not isinstance(section, Mapping):
            result[name] = section
        else:
            result[name] = _anchored_table(settings, section, base)
    return result
