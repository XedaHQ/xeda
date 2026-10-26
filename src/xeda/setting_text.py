"""Read the text of a setting from the command line as the type that the setting declares.

A setting takes exactly its declared type from Python and from files, and Xeda converts nothing
(`dataclass.scalar_problems`). The command line has only text (`-s seed=4`), so its text goes
through the parser of this module first, chosen by the annotation of each setting. Only text
that `utils.settings_to_dict` marked as such (`dataclass.SettingText`) is read this way.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from .dataclass import (
    LIST_TEXT_MESSAGE,
    ListLiteralText,
    SettingText,
    XedaBaseModel,
    annotation_kind,
    boolean_text_message,
    comma_separated_items,
    field_type,
    input_names,
    nested_model,
)

__all__ = ["SettingTextError", "parse_setting_text", "parse_text_leaves"]

#: One problem with a setting given as text: the address of the setting, what is wrong, and the
#: type of the error (`boolean_text`, `invalid_value` or `list_text`).
Problem = tuple[str, str, str]


class SettingTextError(ValueError):
    """Text on the command line is no value of the type of its setting.

    `problems` holds one `(address, message, type)` triple for each setting."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(f"{address}: {message}" for address, message, _ in problems))
        self.problems = problems

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.problems,))


def _problem(address: str, message: str, kind: str = "invalid_value") -> SettingTextError:
    return SettingTextError([(address, message, kind)])


def _members(annotation: Any) -> tuple[Any, ...]:
    """Return the branches of a union (`None` removed), or the annotation alone."""
    if get_origin(annotation) in (Union, types.UnionType):
        return tuple(a for a in get_args(annotation) if a is not type(None))
    return (annotation,)


def _plain(annotation: Any) -> Any:
    """Return `annotation` without its `Annotated` metadata and without `None` in a union."""
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
            continue
        members = _members(annotation)
        if len(members) == 1 and members[0] is not annotation:
            annotation = members[0]
            continue
        return annotation


def _is_text(annotation: Any) -> bool:
    """Whether the text of this annotation is its value: a type that validates the text itself
    (`str`, a path, an enum, a quantity, a model) or that Xeda has no parser for."""
    annotation = _plain(annotation)
    if get_origin(annotation) in (Union, types.UnionType):
        return False
    if annotation in (bool, int, float):
        return False
    return not (get_origin(annotation) is Literal or _is_sequence(annotation))


def _is_sequence(annotation: Any) -> bool:
    return get_origin(annotation) in (list, set, frozenset, tuple)


def _is_fixed_tuple(annotation: Any) -> bool:
    args = get_args(annotation)
    return get_origin(annotation) is tuple and not (len(args) == 2 and args[1] is Ellipsis)


def _items_type(annotation: Any) -> Any:
    args = get_args(annotation)
    return args[0] if args else Any


def parse_setting_text(annotation: Any, text: str, *, address: str) -> Any:
    """Return the value that the command-line `text` of the setting at `address` stands for.

    The annotation of the setting decides, after `Annotated` metadata and `None` are removed:

    - `bool`: `true` or `false` in any case (`boolean_text` otherwise);
    - `int` and `float`: the number (`invalid_value` otherwise);
    - `str`, a path, `Code`, a quantity, an enum, a model and any type without a parser: the
      text, for the validation of the setting to judge;
    - `Literal[...]`: the text when it is a member, else the text read as the type of the members
      (`Literal[1, 2]`);
    - a list or `tuple[X, ...]`: the items at the commas, each read as `X`. A tuple of fixed
      length takes that many items, each read as its own type. Text spelled `[...]` is refused
      (`list_text`);
    - a union: the first branch, in the order of the declaration, that reads the text. Branches
      that take any text come last.

    The result of a text type is a plain `str`."""
    return _parse(_plain(annotation), str(text), address, in_union=False)


def _parse(annotation: Any, text: str, address: str, *, in_union: bool) -> Any:
    annotation = _plain(annotation)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return _parse_union(annotation, text, address)
    if annotation is bool:
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        raise _problem(address, boolean_text_message(text), "boolean_text")
    if annotation is int:
        try:
            return int(text)
        except ValueError:
            raise _problem(
                address, f"`{text}` is not a whole number: write digits, such as `4`"
            ) from None
    if annotation is float:
        try:
            return float(text)
        except ValueError:
            raise _problem(address, f"`{text}` is not a number: write one, such as `4.5`") from None
    if origin is Literal:
        return _parse_literal(get_args(annotation), text, address, in_union)
    if _is_sequence(annotation):
        try:
            items = comma_separated_items(address, text)
        except ListLiteralText as error:
            raise _problem(
                address,
                LIST_TEXT_MESSAGE.format(shown=error.shown, key=address),
                "list_text",
            ) from None
        if _is_fixed_tuple(annotation):
            kinds = get_args(annotation)
            if len(items) != len(kinds):
                raise _problem(address, f"`{text}` needs {len(kinds)} values, separated by commas")
            return tuple(_parse(k, one, address, in_union=False) for k, one in zip(kinds, items))
        item = _items_type(annotation)
        values = [_parse(item, one, address, in_union=False) for one in items]
        return tuple(values) if origin is tuple else values
    return str(text)


def _parse_literal(members: tuple[Any, ...], text: str, address: str, in_union: bool) -> Any:
    """Return the member that `text` names, reading it as the type of the members when that
    type is not text. Text that is no member is the text itself, for the setting to refuse,
    unless a union is trying this branch."""
    if text in members:
        return text
    for kind in dict.fromkeys(type(member) for member in members if not isinstance(member, str)):
        if kind in (bool, int, float):
            try:
                value = _parse(kind, text, address, in_union=False)
            except SettingTextError:
                continue
            if value in members:
                return value
    shown = ", ".join(repr(member) for member in members)
    if in_union or any(not isinstance(member, str) for member in members):
        raise _problem(address, f"`{text}` is none of {shown}")
    return str(text)


def _parse_union(annotation: Any, text: str, address: str) -> Any:
    branches = _members(annotation)
    reading = [b for b in branches if not _is_text(b)]
    taking_any_text = [b for b in branches if _is_text(b)]
    refusals: list[str] = []
    for branch in reading:
        try:
            return _parse(branch, text, address, in_union=True)
        except SettingTextError as error:
            refusals.extend(message for _, message, _ in error.problems)
    if taking_any_text:
        return str(text)
    raise SettingTextError(
        [
            (
                address,
                "; ".join(dict.fromkeys(refusals)) or f"`{text}` is not accepted",
                "invalid_value",
            )
        ]
    )


# ---------------------------------------------------------------------------------------------
# A table of settings
# ---------------------------------------------------------------------------------------------


def parse_text_leaves(
    values: Mapping[str, Any], settings_cls: type[XedaBaseModel], prefix: str = ""
) -> dict[str, Any]:
    """Return `values`, a table written for `settings_cls`, with each leaf of command-line text
    (`SettingText`) read by the annotation of its setting.

    The table is walked through nested models and through the values of a dictionary. A leaf that
    names no setting, or sits where the model takes a shorthand form, stays text for the
    validation of the model. Any other value is not text from the command line and stays as it
    is. Every problem is reported at once, as one `SettingTextError`."""
    problems: list[Problem] = []
    result = _read_table(values, settings_cls, prefix, problems)
    if problems:
        raise SettingTextError(problems)
    return result


def _read_table(
    values: Mapping[str, Any],
    settings_cls: type[XedaBaseModel],
    prefix: str,
    problems: list[Problem],
) -> dict[str, Any]:
    names = input_names(settings_cls)
    result: dict[str, Any] = {}
    for key, value in values.items():
        info = settings_cls.model_fields.get(names.get(key, key)) if isinstance(key, str) else None
        if info is None:
            result[key] = value
            continue
        result[key] = _read(field_type(info), value, f"{prefix}{key}", problems)
    return result


def _read(annotation: Any, value: Any, address: str, problems: list[Problem]) -> Any:
    if isinstance(value, SettingText):
        try:
            return parse_setting_text(annotation, value, address=address)
        except SettingTextError as error:
            problems.extend(error.problems)
            return value
    if not isinstance(value, Mapping):
        return value
    model = nested_model(annotation)
    if model is not None:
        return _read_table(value, model, address + ".", problems)
    if annotation_kind(annotation)[0] != "dict":
        return value
    entries = _plain(annotation)
    arguments = get_args(entries)
    entry = arguments[1] if len(arguments) == 2 else Any
    table_like = nested_model(entry) is not None or annotation_kind(entry)[0] == "dict"
    result: dict[Any, Any] = {}
    for name, item in value.items():
        where = f"{address}.{name}"
        # A name that has dots arrives as nested tables (`utils.set_hierarchy`): look inside
        # for the entry, until the leaf.
        inside = isinstance(item, Mapping) and not table_like
        result[name] = _read(annotation if inside else entry, item, where, problems)
    return result
