"""Settings groups: the settings of one command or pass of a tool, as a nested model.

A flow with many tool options groups them by command (`global_placement`, `abc`). A group is a
`SettingsGroup`, a field of the settings of a flow or of another group. A user gives its fields as
nested YAML or as dotted keys (`-s global_placement.timing_driven.enabled=false`).

* A group lists the flags it prints in `FLAGS`, and `render_flags` prints them.
* A group that can be switched off has `enabled` as its first field.
* `extra_args` is an escape hatch for flags that have no setting. The group lists the flags it
  accepts there in `EXTRA_FLAGS`.
* `group_problems` finds what is wrong with all groups of some settings. The base
  `Flow.check_settings_supported` runs it, so planning refuses before any process runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import UnionType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Dict,
    Literal,
    Optional,
    Sequence,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic_core import InitErrorDetails

from ..dataclass import (
    ListLiteralText,
    ValidationError,
    XedaBaseModel,
    comma_separated_items,
    field_annotation,
    fold_dotted_keys,
    input_names,
    model_validator,
    opaque_key_declaration_problems,
    opaque_key_fields_of,
)
from ..units import Quantity
from .flow import _is_comma_separated_list, _is_optional_path, removed_setting_error

__all__ = ["Flag", "SettingsGroup", "group_problems", "render_flags"]

#: A word of `extra_args` that is a flag. A word such as `-0.5` is a value.
FLAG_WORD = re.compile(r"^-[A-Za-z_]")


FlagKind = Literal["switch", "skip", "value", "list", "words"]
#: The kinds a flag may have, from the one declaration of `FlagKind`.
FLAG_KINDS: tuple[str, ...] = get_args(FlagKind)


@dataclass(frozen=True)
class Flag:
    """One flag that a group prints for one of its fields.

    The kind says how the value of the field becomes words:

    * `switch`: a boolean. The flag, when the value is true.
    * `skip`: a boolean that is true by default for a `-skip_*` flag. The flag, when the value
      is false.
    * `value`: the flag and the value.
    * `list`: the flag and the list as one word, `[list "a" "b"]` in Tcl by default.
    * `words`: the flag and each item of the list as a word of its own.

    A field that is `None`, and a list that is empty, print nothing."""

    field: str
    flag: str
    kind: FlagKind


class SettingsGroup(XedaBaseModel):
    """The settings of one command or pass of a tool.

    A group is a field of a flow's `Settings`, or of another group, with
    `Field(default_factory=<Group>, description=...)`. Every group builds with no arguments. Each
    of its fields has a `description` and a tier (`xeda.dataclass.tier`).

    A subclass declares what it needs in these class variables:

    * `FLAGS` is the table of flags that `render_flags` prints.
    * `EXTRA_FLAGS` lists the flags that `extra_args` accepts. A group with one has the field
      `extra_args: list[str]`, and a group with that field has `EXTRA_FLAGS`.
    * `removed_settings` maps the name of a former field to what replaced it.

    The class refuses a wrong declaration with a `TypeError` when it is created."""

    FLAGS: ClassVar[tuple[Flag, ...]] = ()
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset()
    removed_settings: ClassVar[Dict[str, str]] = {}

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Return what is wrong with this group beyond the generic rules of `group_problems`.

        `key` is the dotted path of this group from the settings of the flow. Each problem is a
        `(dotted key of a leaf, message)` pair. The method is pure and reads only the fields of
        this group."""
        return []

    @classmethod
    def removed_setting_replacements(cls) -> Dict[str, str]:
        """Return every removed setting of this group with what replaced it. That is the
        `removed_settings` of the class and of all its bases, and the class itself wins."""
        merged: Dict[str, str] = {}
        for klass in reversed(cls.__mro__):
            merged.update(vars(klass).get("removed_settings", {}))
        return merged

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        problems = _declaration_problems(cls)
        problems += opaque_key_declaration_problems(cls)
        if problems:
            raise TypeError(f"{cls.__qualname__}: " + "; ".join(problems))

    @classmethod
    def _normalized_field(cls, key: str, value: Any) -> Any:
        """Return the value of the field `key` after the input conveniences of a setting.

        Text given to a list that accepts no text is a comma-separated list
        (`-s group.items=a,b`), and text spelled `[...]` is refused with a `ListLiteralText`, as
        for every setting (`comma_separated_items`). `""` given to an optional path is unset. A
        dotted key of a field in `opaque_key_fields` is one key (`fold_dotted_keys`)."""
        names = input_names(cls)
        if key not in names:
            return value
        if names[key] in opaque_key_fields_of(cls):
            return fold_dotted_keys(value)
        annotation = field_annotation(cls, names[key])
        if isinstance(value, str) and _is_comma_separated_list(annotation):
            return comma_separated_items(names[key], value)
        if value == "" and _is_optional_path(annotation):
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_group_input(cls, values: Any, info: Any) -> Any:
        """Refuse a removed setting and apply the input conveniences of a setting (see
        `_normalized_field`) to the fields of the input.

        An assignment arrives with the state of the group and the new value. pydantic reads the
        assigned value from the assignment, not from this state, so `__setattr__` normalizes
        the assigned value, and this validator changes nothing then."""
        for name, replacement in cls.removed_setting_replacements().items():
            if name in values:
                raise removed_setting_error(name, replacement, values[name])
        if info.field_name is not None and info.data is None:
            return values  # an assignment
        refused: list[InitErrorDetails] = []
        for key, value in values.items():
            try:
                values[key] = cls._normalized_field(key, value)
            except ListLiteralText as error:
                refused.append(
                    InitErrorDetails(type=error.validation_error(), loc=(key,), input=value)
                )
        if refused:
            raise ValidationError.from_exception_data(cls.__name__, refused)
        return values

    def __setattr__(self, name: str, value: Any) -> None:
        if name in type(self).model_fields:
            try:
                value = type(self)._normalized_field(name, value)
            except ListLiteralText as error:
                raise ValidationError.from_exception_data(
                    type(self).__name__,
                    [InitErrorDetails(type=error.validation_error(), loc=(name,), input=value)],
                ) from None
        super().__setattr__(name, value)


def _is_bool(annotation: Any) -> bool:
    """Return whether `annotation` is `bool` or `Optional[bool]`."""
    return annotation is bool or set(get_args(annotation)) == {bool, type(None)}


def _is_list(annotation: Any) -> bool:
    """Return whether `annotation` is a `list[...]` or an `Optional[list[...]]`."""
    members = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) in (Union, UnionType) and len(members) == 1:
        annotation = members[0]
    return get_origin(annotation) is list


def _is_quantity(annotation: Any) -> bool:
    """Return whether `annotation` is a quantity type, or an `Optional`, `Union` or `Annotated`
    of one."""
    if isinstance(annotation, type):
        return issubclass(annotation, Quantity)
    if get_origin(annotation) is Annotated:
        return _is_quantity(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        return any(_is_quantity(member) for member in get_args(annotation))
    return False


def _declaration_problems(cls: type[SettingsGroup]) -> list[str]:
    """Return what is wrong with the declaration of the group `cls`."""
    problems: list[str] = []
    fields = cls.model_fields
    seen_fields: set[str] = set()
    seen_flags: set[str] = set()
    for flag in cls.FLAGS:
        if flag.field not in fields:
            problems.append(f"the flag `{flag.flag}` names `{flag.field}`, which is no field")
        if flag.field in seen_fields:
            problems.append(f"`{flag.field}` has more than one flag in `FLAGS`")
        if flag.flag in seen_flags:
            problems.append(f"`{flag.flag}` is twice in `FLAGS`")
        if not flag.flag.startswith("-"):
            problems.append(f"the flag `{flag.flag}` of `{flag.field}` does not start with `-`")
        if flag.flag in cls.EXTRA_FLAGS:
            problems.append(f"`{flag.flag}` is in `FLAGS` and in `EXTRA_FLAGS`")
        annotation = field_annotation(cls, flag.field)
        if _is_quantity(annotation):
            problems.append(
                f"`{flag.field}` is a quantity and cannot be in `FLAGS`, which prints it with its "
                f"unit (`10 ns`): write the flag `{flag.flag}` in the template, with the value "
                f"rendered by `| to(unit)`"
            )
        if flag.kind not in FLAG_KINDS:
            problems.append(
                f"the flag `{flag.flag}` has the kind {flag.kind!r}: use one of {', '.join(FLAG_KINDS)}"
            )
        elif flag.kind in ("switch", "skip") and not _is_bool(annotation):
            problems.append(f"the {flag.kind} flag `{flag.flag}` needs `{flag.field}` to be a bool")
        elif flag.kind in ("list", "words") and not _is_list(annotation):
            problems.append(f"the {flag.kind} flag `{flag.flag}` needs `{flag.field}` to be a list")
        seen_fields.add(flag.field)
        seen_flags.add(flag.flag)
    for flag_text in sorted(cls.EXTRA_FLAGS):
        if not flag_text.startswith("-"):
            problems.append(f"the extra flag `{flag_text}` does not start with `-`")
    if "extra_args" in fields:
        if field_annotation(cls, "extra_args") != list[str]:
            problems.append("`extra_args` must be a `list[str]`")
        if not cls.EXTRA_FLAGS:
            problems.append("`extra_args` needs `EXTRA_FLAGS`, the flags it accepts")
    elif cls.EXTRA_FLAGS:
        problems.append("`EXTRA_FLAGS` needs the field `extra_args: list[str]`")
    if "enabled" in fields:
        if next(iter(fields)) != "enabled":
            problems.append("`enabled` must be the first field")
        if field_annotation(cls, "enabled") is not bool:
            problems.append("`enabled` must be a `bool`")
    for name in sorted(cls.removed_setting_replacements()):
        if name in fields:
            problems.append(f"`{name}` is in `removed_settings` and is a field")
    if not problems and cls.__pydantic_complete__:
        try:
            cls()
        except ValidationError as error:
            problems.append(f"it does not build with no arguments ({error.error_count()} errors)")
    return problems


def render_flags(
    group: SettingsGroup,
    quote: Callable[[Any], str],
    list_word: Optional[Callable[[Sequence[Any]], str]] = None,
) -> str:
    """Return the words that `group` prints: its `FLAGS` in table order, then its `extra_args`.

    `quote` turns one value into one word of the tool's language (`tcl_word`). `list_word` turns
    the items of a `list` flag into one word. By default that is the Tcl form `[list "a" "b"]`.

    The words of this group only: a sub-group prints with its own call. A group that has
    `enabled` and is disabled prints only the flag of `enabled`, if it has one."""
    cls = type(group)
    if list_word is None:

        def list_word(items: Sequence[Any]) -> str:
            return "[list " + " ".join(quote(item) for item in items) + "]"

    disabled = getattr(group, "enabled", True) is False and "enabled" in cls.model_fields
    words: list[str] = []
    for flag in cls.FLAGS:
        if disabled and flag.field != "enabled":
            continue
        value = getattr(group, flag.field)
        if value is None:
            continue
        if flag.kind == "switch":
            if value is True:
                words.append(flag.flag)
        elif flag.kind == "skip":
            if value is False:
                words.append(flag.flag)
        elif flag.kind == "value":
            words.append(f"{flag.flag} {quote(value)}")
        elif flag.kind == "list":
            if value:
                words.append(f"{flag.flag} {list_word(list(value))}")
        elif flag.kind == "words":
            if value:
                words.append(f"{flag.flag} " + " ".join(quote(item) for item in value))
        else:
            raise ValueError(f"the flag `{flag.flag}` has the unknown kind {flag.kind!r}")
    if not disabled:
        words.extend(quote(item) for item in getattr(group, "extra_args", ()))
    return " ".join(words)


def _changed_leaves(
    group: SettingsGroup, default: SettingsGroup, key: str, *, skip_enabled: bool = True
) -> list[str]:
    """Return the dotted keys of the leaves of `group` below `key` whose value differs from the
    same leaf of `default`, sub-groups included. The `enabled` of `group` itself is no leaf here,
    but the `enabled` of a sub-group is one."""
    changed: list[str] = []
    for name in type(group).model_fields:
        if skip_enabled and name == "enabled":
            continue
        value, original = getattr(group, name), getattr(default, name)
        if isinstance(value, SettingsGroup) and isinstance(original, SettingsGroup):
            changed += _changed_leaves(value, original, f"{key}.{name}", skip_enabled=False)
        elif value != original:
            changed.append(f"{key}.{name}")
    return changed


def _extra_args_problems(group: SettingsGroup, key: str) -> list[tuple[str, str]]:
    """Return what is wrong with the `extra_args` of `group`."""
    items: list[str] = getattr(group, "extra_args", [])
    if not items:
        return []
    where = f"{key}.extra_args"
    owners = {flag.flag: flag.field for flag in type(group).FLAGS}
    allowed = ", ".join(sorted(type(group).EXTRA_FLAGS))
    problems: list[tuple[str, str]] = []
    if not FLAG_WORD.match(items[0]):
        problems.append((where, f"`{where}` must start with a flag, not `{items[0]}`"))
    for item in items:
        if not FLAG_WORD.match(item):
            continue
        if item in owners:
            problems.append(
                (
                    where,
                    f"`{item}` in `{where}` is the setting `{key}.{owners[item]}`: "
                    "set that instead",
                )
            )
        elif item not in type(group).EXTRA_FLAGS:
            problems.append(
                (where, f"`{item}` in `{where}` is not allowed; the allowed flags are: {allowed}")
            )
    return problems


def _problems_of(group: SettingsGroup, key: str) -> list[tuple[str, str]]:
    """Return the problems of `group` and, depth first, of the groups inside it."""
    if "enabled" in type(group).model_fields and getattr(group, "enabled") is False:
        default = type(group)()
        return [
            (leaf, f"`{leaf}` has no effect while `{key}.enabled` is false")
            for leaf in _changed_leaves(group, default, key)
        ]
    problems = _extra_args_problems(group, key)
    problems += group.problems(key)
    for name in type(group).model_fields:
        value = getattr(group, name)
        if isinstance(value, SettingsGroup):
            problems += _problems_of(value, f"{key}.{name}")
    return problems


def group_problems(settings: BaseModel) -> list[tuple[str, str]]:
    """Return what is wrong with the groups in `settings`, as `(dotted key, message)` pairs.

    The keys are dotted paths from the root of `settings`. The groups come depth first in
    declaration order. For each group the rules are:

    * A disabled group (its `enabled` is false) holds only defaults. Each leaf that differs from
      the default of the group is a problem, and nothing else is judged below the group.
    * A word of `extra_args` that starts a flag is one of the `EXTRA_FLAGS` of the group, and it
      is no flag that a field of the group prints. The first word is a flag.
    * The `problems` of the group."""
    found: list[tuple[str, str]] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SettingsGroup):
            found += _problems_of(value, name)
    return found
