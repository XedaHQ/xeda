"""The `Flow` base class, its settings model and errors, and the registry of flows."""

from __future__ import annotations

import inspect
import logging
import os
import re
from abc import ABCMeta, abstractmethod
from datetime import datetime
from pathlib import Path, PurePath
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
)

import jinja2
from box import Box
from jinja2 import ChoiceLoader, PackageLoader, StrictUndefined
from pydantic_core import ErrorDetails, InitErrorDetails

from ..dataclass import (
    DELIVERABLE_ROLE,
    WORKING,
    WORKING_ROLE,
    BaseModel,
    Field,
    ListLiteralText,
    NoReplacement,
    PrivateAttr,
    SecondSpelling,
    ValidationError,
    WaveformText,
    XedaBaseModel,
    annotation_args,
    comma_separated_items,
    conventional_output,
    field_annotation,
    fold_dotted_keys,
    input_names,
    is_locator,
    model_validator,
    nested_model,
    opaque_key_declaration_problems,
    opaque_key_fields_of,
    removed_setting_error,
    validation_errors,
    written_always,
    waveform_format,
    waveform_value,
    written_role,
)
from ..design import LANGUAGE_TYPES, Design, Source, SourceType
from ..frozen import FrozenDict
from ..records import REJECTED, ErrorInfo, RunRecord
from ..run_dir import OutputSnapshot, RunDirectory, record_output_state, resolved_inside
from ..units import count, finalize_template_value, to_filter
from ..utils import (
    LOCATION_FORMS,
    PATH_VARIABLES,
    XedaException,
    camelcase_to_snakecase,
    expand_env_vars,
    location_free,
    location_roots,
    parse_patterns_in_file,
    path_identity,
    path_variables,
    rebuild_like,
    regex_match,
    replacing_file,
    semantic_hash,
    tcl_list,
    tcl_quote,
    tcl_word,
    unique,
)

from .derived import DesignConversion
from .source_file import without_source_file
from .metrics import (
    ArtifactSpec,
    MetricSpec,
    describe_results,
    log_artifact_key,
    log_stem,
)
from .io import (
    DESIGN,
    FlowInputs,
    FlowOutputs,
    In,
    check_io_declarations,
    declared_inputs,
    declared_outputs,
    output_enabled,
    switch_on,
)

if TYPE_CHECKING:
    from ..deliver import Delivered

log = logging.getLogger(__name__)


__all__ = [
    "Flow",
    "FlowFatalError",
    "FlowSettingsError",
    "FlowSettingsException",
    "WrittenLeaf",
    "describe_results",
    "flowrun_hash",
    "identity_settings",
    "is_conventional_template",
    "resolve_conventional_templates",
    "resolved_template",
    "is_unset",
    "map_written_leaves",
    "output_name",
    "written_path_problems",
]

registered_flows: Dict[str, Tuple[str, Type[Flow]]] = {}

DictStrPath = Dict[str, Union[str, os.PathLike]]


def _is_path_annotation(annotation: Any) -> bool:
    """Return whether one annotation leaf stands for a path."""
    return annotation in (Path, os.PathLike) or get_origin(annotation) is os.PathLike


def _annotation_contains_path(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _annotation_contains_path(get_args(annotation)[0])
    if _is_path_annotation(annotation):
        return True
    return any(_annotation_contains_path(arg) for arg in get_args(annotation))


def _is_optional_path(annotation: Any) -> bool:
    """Return whether `annotation` is an optional path that accepts no plain text.

    Then an empty string can only mean that the setting is unset."""
    if get_origin(annotation) not in (Union, UnionType):
        return False
    members = get_args(annotation)
    return (
        type(None) in members
        and str not in members
        and any(_is_path_annotation(member) for member in members)
    )


def _annotation_matches_value(annotation: Any, value: Any) -> bool:
    """Return whether `value` fits `annotation`, as a best effort.

    The check picks the branch of a union before validation."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _annotation_matches_value(args[0], value)
    if origin in (Union, UnionType):
        return any(_annotation_matches_value(arg, value) for arg in args)
    if annotation is Any:
        return True
    if annotation is type(None):
        return value is None
    if _is_path_annotation(annotation):
        return isinstance(value, (str, os.PathLike))
    if origin is list:
        return isinstance(value, list)
    if origin in (set, frozenset):
        return isinstance(value, (list, set, frozenset))
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_annotation_matches_value(args[0], item) for item in value)
        return len(value) == len(args) and all(
            _annotation_matches_value(item_annotation, item)
            for item_annotation, item in zip(args, value)
        )
    if origin is dict:
        return isinstance(value, dict)
    try:
        return isinstance(value, annotation)
    except TypeError:
        return False


def _is_comma_separated_list(annotation: Any) -> bool:
    """Return whether `annotation` is a list that accepts no plain string.

    Text given for such a setting is a comma-separated list."""
    accepted = annotation_args(annotation)
    return str not in accepted and any((get_origin(a) or a) is list for a in accepted)


def _list_literal_error(
    model: type, refused: Sequence[Tuple[str, ListLiteralText | WaveformText, Any]]
) -> ValidationError:
    """Return the validation error of `model` for the list settings in `refused`.

    Each item of `refused` is a `(key, error, value)`. The error holds one entry per setting, so
    that every refusal is reported at once."""
    return ValidationError.from_exception_data(
        model.__name__,
        [
            InitErrorDetails(type=error.validation_error(key), loc=(key,), input=value)
            for key, error, value in refused
        ],
    )


def map_path_leaves(value: Any, annotation: Any, fn: Callable[[Any], Any]) -> Any:
    """Apply `fn` at every path-typed leaf of `value`, walked by its annotation.

    A container must be walked by its annotation and not by value alone. In `lib_paths`, for
    example, the first tuple member is a library name and only the second is a path. Treating
    every string as a path would corrupt names that contain `$` (path expansion) and could match
    a library name against a file of the same name (a trace's inputs).
    """
    return map_keyed_path_leaves(value, annotation, lambda _key, leaf: fn(leaf))


def map_keyed_path_leaves(
    value: Any, annotation: Any, fn: Callable[[str, Any], Any], key: str = ""
) -> Any:
    """Apply `fn` at every path-typed leaf of `value`, and give it the key path of the leaf.

    This is the one traversal; `map_path_leaves` calls it. The key path of a leaf lies below
    `key`: `[i]` is an item of a list or tuple, and `[k]` is the value under the key `k` of a
    dict (`lib_paths[0][1]` is the path half of the first library)."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return map_keyed_path_leaves(value, args[0], fn, key)
    if _is_path_annotation(annotation):
        return fn(key, value)
    if origin in (Union, UnionType):
        # Follow the first branch that holds a path and fits the value. A raw string fits both
        # branches of `str | Path`; the presence of the Path branch is what makes it a path.
        for choice in args:
            if _annotation_contains_path(choice) and _annotation_matches_value(choice, value):
                return map_keyed_path_leaves(value, choice, fn, key)
        return value
    if origin in (list, set, frozenset) and isinstance(value, (list, set, frozenset)):
        item_annotation = args[0] if args else Any
        mapped = [
            map_keyed_path_leaves(item, item_annotation, fn, f"{key}[{index}]")
            for index, item in enumerate(value)
        ]
        return rebuild_like(value, mapped)
    if origin is tuple and isinstance(value, (list, tuple)):
        if len(args) == 2 and args[1] is Ellipsis:
            item_annotations = [args[0]] * len(value)
        else:
            item_annotations = [args[i] if i < len(args) else Any for i in range(len(value))]
        mapped = [
            map_keyed_path_leaves(item, item_annotation, fn, f"{key}[{index}]")
            for index, (item, item_annotation) in enumerate(zip(value, item_annotations))
        ]
        return rebuild_like(value, mapped)
    if origin is dict and isinstance(value, dict):
        key_annotation, value_annotation = args if len(args) == 2 else (Any, Any)
        return {
            map_keyed_path_leaves(k, key_annotation, fn, f"{key}[{k}](key)"): (
                map_keyed_path_leaves(item, value_annotation, fn, f"{key}[{k}]")
            )
            for k, item in value.items()
        }
    return value


def _nested_models(value: Any, key: str) -> list[tuple[str, BaseModel]]:
    """Return the models inside `value`, by key path.

    The models are `value` itself, the values of a mapping, or the items of a list."""
    if isinstance(value, BaseModel):
        return [(key, value)]
    if isinstance(value, Mapping):
        return [m for name, item in value.items() for m in _nested_models(item, f"{key}[{name}]")]
    if isinstance(value, (list, tuple)):
        return [m for i, item in enumerate(value) for m in _nested_models(item, f"{key}[{i}]")]
    return []


class WrittenLeaf(NamedTuple):
    """A path leaf of a setting that the flow writes.

    It holds the key path (`cocotb.results_xml`), the role, the value, and the model and field
    that the leaf belongs to."""

    key: str
    role: str
    value: Any
    owner: BaseModel
    field: str


def map_written_leaves(
    model: BaseModel,
    fn: Callable[[WrittenLeaf], Any],
    *,
    prefix: str = "",
) -> None:
    """Call `fn` at every path leaf of every field whose paths the flow writes (`written_role`).

    The walk covers `model` and the models nested in it, by declared shape
    (`map_keyed_path_leaves`). A field whose leaves `fn` changed is assigned back."""
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        role = written_role(type(model), name)
        if role is not None:
            if value is not None:
                changed = map_keyed_path_leaves(
                    value,
                    info.annotation,
                    lambda key, leaf: fn(WrittenLeaf(key, role, leaf, model, name)),
                    prefix + name,
                )
                if changed != value:
                    setattr(model, name, changed)
            continue
        for key, nested in _nested_models(value, prefix + name):
            map_written_leaves(nested, fn, prefix=key + ".")


def output_name(leaf: WrittenLeaf, design: str) -> Optional[PurePath]:
    """Return the conventional name of the deliverable that `leaf` belongs to, for `design`.

    The name comes from the settings of the flow (`Flow.Settings.conventional_output`) or from
    the marker of the field."""
    if isinstance(leaf.owner, Flow.Settings):
        return leaf.owner.conventional_output(leaf.field, design)
    return conventional_output(type(leaf.owner), leaf.field, design)


def is_conventional_template(leaf: WrittenLeaf) -> bool:
    """Return whether `leaf` is the conventional name of its deliverable, still holding `{design}`.

    For the waveform `saif`, the value `true` stands for `outputs/{design}.saif`. Such a value
    stands for the output that the run writes, as a location does. The name of the design goes
    into it where the run writes the file (`resolve_conventional_templates`). The rule is the
    same for every deliverable."""
    name = output_name(leaf, "{design}")
    return (
        name is not None
        and "{design}" in str(name)
        and isinstance(leaf.value, (str, os.PathLike))
        and PurePath(leaf.value) == name
    )


def resolved_template(leaf: WrittenLeaf, design: str) -> Optional[PurePath]:
    """Return the name for `design` that `leaf` stands for when it is the conventional template.

    Return None for any other value (`is_conventional_template`)."""
    return output_name(leaf, design) if is_conventional_template(leaf) else None


def resolve_conventional_templates(settings: "Flow.Settings", design: str) -> None:
    """Replace, in place, each deliverable that is its conventional template by its name.

    The name is the one for `design`.

    The launcher does this with the deliveries (`deliver.split_deliveries`). A flow built
    directly does it when it is made, so the path that a flow writes is the same."""

    def fill(written: WrittenLeaf) -> Any:
        name = resolved_template(written, design)
        return Path(name) if name is not None else written.value

    map_written_leaves(settings, fill)


def written_path_problems(settings: "Flow.Settings") -> list[tuple[str, str]]:
    """Return what is wrong with the paths that `settings` says the flow writes.

    Each problem is a `(key path, message)` pair. The problems are a working location that is
    not a name inside the run directory, a deliverable name that leaves it, and a variable that
    is not expanded (`$CWD`: the start directory is `$PWD`)."""
    problems: list[tuple[str, str]] = []

    def check(written: WrittenLeaf) -> Any:
        key, role, leaf = written.key, written.role, written.value
        if not isinstance(leaf, (str, os.PathLike)) or not os.fspath(leaf):
            return leaf
        text, path = os.fspath(leaf), Path(leaf)
        if "$" in text:
            variable = re.search(r"\$\{?(\w+)", text)
            name = variable.group(1) if variable else "$"
            if name in PATH_VARIABLES:
                message = (
                    f"`{key}` = {text}: ${name} was not expanded here. The value was assigned to "
                    "a nested model after the settings were made. Give an absolute path, or give "
                    "it with the settings (-s, the design file, the API's flow settings)"
                )
            else:
                names = [f"${variable}" for variable in PATH_VARIABLES]
                hint = (
                    "use $PWD, the directory Xeda was started from"
                    if name == "CWD"
                    else f"the variables are {', '.join(names[:-1])} and {names[-1]}"
                )
                message = f"`{key}` = {text}: ${name} is not a variable Xeda knows: {hint}"
            problems.append((key, message))
        elif role == WORKING_ROLE and (
            path.is_absolute() or ".." in path.parts or text.startswith("~")
        ):
            problems.append(
                (
                    key,
                    f"`{key}` = {text}: it is where the flow works, inside its run directory: "
                    "give a name, not a location (to put runs elsewhere, use --run-root)",
                )
            )
        elif text.startswith("~"):
            problems.append(
                (
                    key,
                    f"`{key}` = {text}: `~` is not expanded: give a name in the run directory, "
                    f"or a location ({LOCATION_FORMS})",
                )
            )
        elif not path.is_absolute() and ".." in path.parts:
            problems.append(
                (
                    key,
                    f"`{key}` = {text} leaves the run directory: give a name inside it; to put "
                    f"it elsewhere, name a location ({LOCATION_FORMS}) or use --outputs-to",
                )
            )
        return leaf

    map_written_leaves(settings, check)
    return problems


def _item_annotation(annotation: Any) -> tuple[Any, bool]:
    """Return the annotation of the items of a list or a dictionary, and whether the location of
    an item has a key after the field name. Any other annotation gives `(None, False)`."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        elif origin in (Union, UnionType):
            members = [a for a in get_args(annotation) if a is not type(None)]
            if len(members) != 1:
                return None, False
            annotation = members[0]
        else:
            break
    arguments = get_args(annotation)
    if origin is list and len(arguments) == 1:
        return arguments[0], False
    if origin is dict and len(arguments) == 2:
        return arguments[1], True
    return None, False


def _model_at(model: type[BaseModel], path: Sequence[Any]) -> Optional[type[BaseModel]]:
    """Return the model that holds the setting at `path` in `model`, from the location of a
    validation error, or None when `path` leads through anything that is no nested model."""
    current: Optional[type[BaseModel]] = model
    skip = False
    for part in path:
        if current is None:
            return None
        if skip or isinstance(part, int):  # the index or key of an item
            skip = False
            continue
        field = input_names(current).get(part)
        if field is None:
            return None
        annotation = current.model_fields[field].annotation
        current = nested_model(annotation)
        if current is None:
            item, skip = _item_annotation(annotation)
            current = nested_model(item)
    return current


def _spelling_by_case(model: type[BaseModel], key: str) -> Optional[str]:
    """Return the one name of a field of `model` that is `key` in another case or with `-` for
    `_`, or None when there is none or there are several fields like that."""

    def plain(name: str) -> str:
        return name.lower().replace("-", "_")

    names = input_names(model)
    matches = {name: field for name, field in names.items() if plain(name) == plain(key)}
    if len(set(matches.values())) != 1:
        return None
    return next((name for name, field in matches.items() if name == field), next(iter(matches)))


def _with_case_hints(
    model: type[BaseModel], errors: List[ErrorDetails]
) -> List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """Return `validation_errors(errors)`, with a hint on each unknown key that is a known name in
    another case: "Extra inputs are not permitted; did you mean `knob`?". The key may be in a
    nested model, such as a settings group."""
    shown = validation_errors(errors)
    hinted = []
    for error, (location, message, context, kind) in zip(errors, shown):
        loc = error.get("loc", ())
        if kind == "extra_forbidden" and loc and isinstance(loc[-1], str):
            owner = _model_at(model, loc[:-1])
            spelling = _spelling_by_case(owner, loc[-1]) if owner is not None else None
            if spelling is not None and spelling != loc[-1]:
                message = f"{message}; did you mean `{spelling}`?"
        hinted.append((location, message, context, kind))
    return hinted


def _plain_model(annotation: Any) -> Optional[type[BaseModel]]:
    """Return the plain model that `annotation` names, directly or as `Optional[...]`.

    Examples are `CocotbSettings` and `CxxRtl`. The settings of a flow are no plain model: they
    normalize their own input."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _plain_model(get_args(annotation)[0])
    for choice in get_args(annotation) if origin in (Union, UnionType) else (annotation,):
        if get_origin(choice) is Annotated:
            choice = get_args(choice)[0]
        if (
            isinstance(choice, type)
            and issubclass(choice, BaseModel)
            and not issubclass(choice, Flow.Settings)
        ):
            return choice
    return None


def _expand_model_paths(value: Any, model: type[BaseModel], overrides: Dict[str, Any]) -> Any:
    """Return `value` with the path variables of a plain nested model expanded.

    In a mapping, the field of each key decides (`input_names`). A field in `opaque_key_fields`
    has its dotted keys joined first (`fold_dotted_keys`), since the path variables are at its
    values. An instance is copied deeply, and only when a field changes, so the caller's
    instance stays as it is."""
    if isinstance(value, Mapping):
        names = input_names(model)
        opaque = opaque_key_fields_of(model)

        def expanded(key: Any, item: Any) -> Any:
            if key not in names:
                return item
            if names[key] in opaque:
                item = fold_dotted_keys(item)
            return _expand_path_values(item, model.model_fields[names[key]].annotation, overrides)

        return {key: expanded(key, item) for key, item in value.items()}
    if isinstance(value, model):
        changed = {}
        for name, info in type(value).model_fields.items():
            current = getattr(value, name)
            expanded = _expand_path_values(current, info.annotation, overrides)
            if expanded is not current and expanded != current:
                changed[name] = expanded
        if not changed:
            return value
        copy = value.model_copy(deep=True)
        for name, expanded in changed.items():
            setattr(copy, name, expanded)
        return copy
    return value


def _expand_path_values(value: Any, annotation: Any, overrides: Dict[str, Any]) -> Any:
    """Expand variables at the path-typed leaves of `value` only.

    The leaves include those of nested containers and the path fields of a plain model nested in
    a setting (`_plain_model`: `cocotb.results_xml`).

    A container must be walked by its annotation and not by value alone. In `lib_paths`, for
    example, the first tuple member is a library name and only the second is a path. Treating
    every string as a path would corrupt names that contain `$`.
    """

    def expand(leaf: Any) -> Any:
        if isinstance(leaf, (str, os.PathLike)) and "$" in str(leaf):
            return expand_env_vars(Path(leaf), overrides)
        return leaf

    expanded = map_path_leaves(value, annotation, expand)
    model = _plain_model(annotation)
    if model is None or expanded is None:
        # A mapping or a list of plain models: each model expands its own paths.
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is dict and len(args) == 2 and _plain_model(args[1]) is not None:
            if isinstance(expanded, Mapping):
                return {
                    key: _expand_path_values(item, args[1], overrides)
                    for key, item in expanded.items()
                }
        if origin is list and len(args) == 1 and _plain_model(args[0]) is not None:
            if isinstance(expanded, list):
                return [_expand_path_values(item, args[0], overrides) for item in expanded]
        return expanded
    return _expand_model_paths(expanded, model, overrides)


def identity_settings(
    settings: "Flow.Settings", design_name: Optional[str] = None
) -> "Flow.Settings":
    """Return a copy of `settings` as the identity of a run sees them.

    In the copy, every deliverable has the name that the run writes it under in its run
    directory (`output_name`). That holds for a deliverable given as a location, and for one that
    the flow always writes (`deliverable(always=True)`) and that is unset. Where an output is
    delivered, and what it is called there, is no part of the run. A locator (`LOCATOR`) is None
    in the copy: where installed data lives is no part of the run either. Without `design_name`,
    `{design}` stays as written."""
    copy = settings.model_copy(deep=True)
    design = design_name if design_name is not None else "{design}"

    def conventional(written: WrittenLeaf) -> Any:
        leaf = written.value
        if written.role == DELIVERABLE_ROLE and isinstance(leaf, (str, os.PathLike)):
            if (os.fspath(leaf) and Path(leaf).is_absolute()) or is_conventional_template(written):
                name = output_name(written, design)
                if name is not None:
                    return Path(name)
        return leaf

    map_written_leaves(copy, conventional)
    _name_unset_outputs(copy, design)
    _clear_locators(copy)
    return copy


def _clear_locators(model: BaseModel) -> None:
    """Set, in place, each locator setting (`LOCATOR`) of `model` and of the models nested in it
    to None. A place on this machine is no part of the identity of a run."""
    for name in type(model).model_fields:
        value = getattr(model, name)
        if is_locator(type(model), name):
            if value is not None:
                setattr(model, name, None)
            continue
        for _key, nested in _nested_models(value, name):
            _clear_locators(nested)


def _name_unset_outputs(model: BaseModel, design: str, prefix: str = "") -> None:
    """Set, in place, each unset deliverable that the flow always writes to its conventional name.

    The deliverables are those of `model` and of the models nested in it."""
    for name in type(model).model_fields:
        value = getattr(model, name)
        if written_role(type(model), name) == DELIVERABLE_ROLE:
            if value is None and written_always(type(model), name):
                conventional = output_name(
                    WrittenLeaf(prefix + name, DELIVERABLE_ROLE, None, model, name), design
                )
                assert conventional is not None, f"{prefix}{name}: written always, so named"
                setattr(model, name, Path(conventional))
            continue
        for key, nested in _nested_models(value, prefix + name):
            _name_unset_outputs(nested, design, key + ".")


def _with_path_identities(value: Any) -> Any:
    """Return `value` with each path that a remote launch moved written as the client counts it
    (`path_identity`, role `setting`)."""
    if isinstance(value, dict):
        return {key: _with_path_identities(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return rebuild_like(value, [_with_path_identities(item) for item in value])
    if isinstance(value, (str, os.PathLike)):
        found = path_identity(value, "setting")
        return value if found is None else found
    return value


def identity_values(
    flow_name: str, settings: Flow.Settings, design_name: Optional[str] = None
) -> dict[str, Any]:
    """Return what `flowrun_hash` hashes.

    That is the name of the flow and its settings as the identity of a run sees them
    (`identity_settings`). Every path under the design root, the start directory or the
    installation of Xeda is written relative to it (`utils.location_roots`)."""
    roots = location_roots(settings.context.get("design_root"), settings.context.get("runner_cwd"))
    identity = identity_settings(settings, design_name)
    values = identity.model_dump()
    values = _with_path_identities(values)
    return dict(flow_name=flow_name, flow_settings=location_free(values, roots))


def flowrun_hash(flow_name: str, settings: Flow.Settings, design_name: Optional[str] = None) -> str:
    """Return the hash of the settings of a run of `flow_name`.

    This is the settings-only part of the identity of a run. `bindings.node_identity` adds the
    origins of the declared inputs, and the result names a hashed run directory.

    The hash depends on what the settings mean, not on where anything is. A path under the
    design root or under the directory where Xeda was started counts relative to it
    (`$DESIGN_ROOT/c.xdc`), so moving a design, or starting Xeda elsewhere, keeps the hash. A
    path under the installation of Xeda (the files of a bundled platform) counts relative to
    that (`$XEDA/platforms/...`), so two installations agree on it. Paths count as their text:
    only the source files of a design are hashed by content (`Design.rtl_hash`), and no
    directory's content is ever read. Where an output is delivered is no part of the hash
    (`identity_settings`, for the design `design_name`): a deliverable given as a location counts
    as its conventional name.
    """
    return semantic_hash(identity_values(flow_name, settings, design_name))


def is_unset(value: Any) -> bool:
    """Return whether `value` says nothing: `None`, or an empty string or container.

    A required setting that is unset is missing. An empty `clocks` is how `SynthFlow` spells "not
    given". `False` and `0` are values.
    """
    if value is None:
        return True
    return isinstance(value, (str, list, tuple, set, frozenset, dict)) and len(value) == 0


class Flow(metaclass=ABCMeta):
    """A flow runs one or more tools for one design with one set of settings.

    All tool executables should be available on the host or in the same Docker image."""

    name: str  # set automatically

    #: The metrics this flow writes to `results` (and therefore to `results.json`), each with a
    #: description, a type and a unit. Reported by `xeda list-results <flow>`. Build it with
    #: `describe_results`.
    results_description: ClassVar[Dict[str, MetricSpec]] = {}

    #: The artifacts this flow keeps, each with a description and the condition under which a run
    #: has it. An artifact is one file in the run directory. The key is lower snake case
    #: (`ARTIFACT_KEY`). `log` is the log of the main tool (`main_executable`), and `<name>_log`
    #: the log of each other program that the flow starts (`log_artifact_key`). A declared output
    #: is never an artifact. Reported by `xeda list-results <flow>`. Build it with
    #: `describe_artifacts`.
    artifacts_description: ClassVar[Dict[str, ArtifactSpec]] = {}

    #: Settings this flow cannot run without, each with what it is and how to give it (the text
    #: may name the flow as `{flow}`). Checked when the flow is launched, by
    #: `check_required_settings`, not when its settings are validated: a layer of settings, such
    #: as one section of a design file, holds only some of them, and a model that insisted on all
    #: of them could not validate it. So the model never requires them: `Flow.Settings` refuses a
    #: field without a default when its class is defined. Give the field a default (`None`, or an
    #: empty value), and name the setting here. A flow that needs a setting only for what its
    #: other settings ask of it says so in `required_settings_for`, which is checked instead.
    required_settings: Dict[str, str] = {}

    #: The types of the design's sources this flow hands its tools, in the parts it reads
    #: (`design_parts`). A flow that reads the design's sources declares them here and selects
    #: what it reads with `sources_read()`, the one selection, so the declaration is what the tool
    #: gets. `check_design_supported` refuses a language (`LANGUAGE_TYPES`) the flow does not
    #: read, and a design with no source it reads; a source of another type belongs to another
    #: flow. None: the flow reads none of the design's sources, only its declared `Inputs`
    #: (`tests/test_source_contracts.py` checks the code and the templates of every flow).
    reads_sources: ClassVar[frozenset[SourceType] | None] = None

    #: The parts of the design this flow reads (`design.DESIGN_PARTS`: `rtl`, `tb`). They are
    #: what identifies its run (`Design.parts_hash`, the `design_hash` of its trace and
    #: `results.json`) and the design files that its trace records as inputs. An edit to a part
    #: it does not read leaves the flow up to date. This scopes only the flow's own freshness:
    #: a flow whose producer reads another part still runs again with its producer. A part
    #: wrongly left out is a stale reuse of a result built from other sources, and one wrongly
    #: left in is only a run for nothing. So a flow that reads `tb` anywhere, in code or in a
    #: template, declares it (`tests/test_design_parts.py`).
    design_parts: ClassVar[frozenset[str]] = frozenset({"rtl"})

    #: The types of the design's sources this flow is judged on, within `design_parts`: its
    #: `design_hash` and the design files of its trace cover the sources of these types, in their
    #: order, and the design's metadata (top, parameters, defines, clocks, language), but not a
    #: source of another type. None: every source of the parts. A flow that takes its files through
    #: declared inputs (`nextpnr`, `fpga_pack`, `openfpgaloader`, `vivado_power`) names the types
    #: its inputs take, so an edit to a file only its producer reads leaves it alone. A type
    #: wrongly left out is a stale reuse: the declaration holds every type the flow can take or
    #: hand its tool, and its code and templates read no source of their own
    #: (`tests/test_design_source_types.py`). Only the flow's own freshness is scoped; the guards
    #: that protect the user's files keep the whole design.
    design_source_types: ClassVar[frozenset[SourceType] | None] = None

    @classmethod
    def design_scope(cls, settings: Flow.Settings) -> frozenset[SourceType] | None:
        """Return the source types this flow is judged on under `settings`. Pure.

        That is `design_source_types`, unless the settings can make the flow read other sources
        (the Python hooks of nextpnr). Then it is None: every source of the parts."""
        return cls.design_source_types

    #: A static reason why a flow can only end a chain, such as a programmer. `always_runs()` is
    #: a separate, dynamic decision: the launcher asks it at run time whether the flow must run.
    action_reason: ClassVar[str | None] = None

    #: The names of every program that the flow may start, under any setting: its tools and the
    #: helpers that only some settings need (a compiler, `make`). `xeda remote check` looks for
    #: them on the login `PATH` of a remote host. A program that a run builds (`simv`) is not
    #: one. `tests/test_flow_executables.py` finds the programs that the code names.
    executables: ClassVar[tuple[str, ...]] = ()

    def main_executable(self) -> str:
        """Return the program of the main tool of this flow. Its log is the artifact `log`.

        That is the first of `executables`, unless the settings select another one. A flow that
        names no program has no main tool: the empty text."""
        executables = type(self).executables
        return executables[0] if executables else ""

    def failure_log_key(self) -> str | None:
        """Return the artifact key of the log to read for a failure of this run, or None.

        The default is None: the log of the program that failed, or else `log`, is the one
        (`error_codes.failure_log`). A flow whose tool starts child runs, each with a log of its
        own, names the log of the run that failed: the error is there and not in the log of the
        tool. A key the run did not register is ignored."""
        return None

    def note_tool_log(self, program: str, key: str | None = None) -> None:
        """Register the log `logs/<program>.log` of a run of `program` as an artifact, if this run
        wrote it.

        The key is `key`, if the tool has one. Else it is `log` for the main tool
        (`main_executable`) and `log_artifact_key(program)` for another program. A log that a program wrote before the run started (`start_run`), or that
        an earlier run left, is no artifact."""
        name = (Path("logs") / f"{log_stem(program)}.log").as_posix()
        if not self.wrote_output(name):
            return
        if key is None:
            main = log_stem(program) == log_stem(self.main_executable())
            key = "log" if main else log_artifact_key(program)
        self.artifacts[key] = name
        self.tool_log_keys[log_stem(program)] = key

    @classmethod
    def required_settings_for(cls, settings: Flow.Settings) -> Mapping[str, str]:
        """Return the settings this flow cannot run without, given `settings`.

        That is `required_settings`, unless the flow needs a setting only for what its other
        settings ask of it (`openfpgaloader` needs its device only to program the flash)."""
        return cls.required_settings

    @classmethod
    def check_required_settings(
        cls, settings: "Flow.Settings", unreached: Mapping[str, str] | None = None
    ) -> None:
        """Fail a launch that lacks a `required_settings` entry, before anything is set up.

        The error names each missing setting and how to give it. `settings` are the final,
        agreed values. `unreached` maps a setting to where it is given without reaching this
        flow (the resolver knows: the section of a flow that is not part of the run). The error
        adds that text, so a value the user did write is not left unexplained."""
        missing = []
        notes = []
        for name, how in cls.required_settings_for(settings).items():
            if is_unset(getattr(settings, name, None)):
                missing.append(f"`{name}`, {how.format(flow=cls.name)}")
                if unreached and unreached.get(name):
                    notes.append(unreached[name])
        if missing:
            raise FlowSettingsException(
                f"{cls.name} needs " + "; and ".join(missing) + "".join(f". {n}" for n in notes),
                code="required_setting",
                node=cls.name,
            )

    @classmethod
    def check_settings_supported(cls, settings: "Flow.Settings") -> None:
        """Refuse settings that this flow cannot support. Pure.

        The check constructs no flow and probes no tool. A flow overrides it to refuse what it
        cannot do, and calls this method first: it refuses what is wrong with the settings
        groups (`settings_group.group_problems`) in one error that lists every problem."""
        from .settings_group import group_problems  # settings_group imports this module

        problems = group_problems(settings)
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "settings_group") for key, message in problems],
                cls.Settings,
            )

    @classmethod
    def check_run_directory(cls, settings: "Flow.Settings", run_path: Path) -> None:
        """Refuse a run directory this flow cannot work in. Pure, like `check_settings_supported`.

        `run_path` is the absolute path decided for this run, not yet created. The launcher calls
        this where it first knows the path, before it creates anything for the run (the run
        root, the directory and its lock). A plan calls it too."""

    @classmethod
    def input_equivalence(cls, settings: Flow.Settings, name: str) -> bool:
        """Return whether input `name` compares the equivalence digest of what it is handed.

        The answer is the `In(..., equivalence=True)` declaration, unless `settings` make the
        flow depend on what the digest leaves out. Pure, like `input_types`."""
        return declared_inputs(cls)[name].equivalence

    @classmethod
    def input_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        """Return the source types that input `name` accepts under `settings`.

        A flow may narrow the declared types here. It uses the settings alone, without probes or
        writes."""
        return declared_inputs(cls)[name].types

    @classmethod
    def output_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        """Return the source types that output `name` may have under `settings`.

        A flow may narrow the declared types here. It uses the settings alone, without probes or
        writes."""
        return declared_outputs(cls)[name].types

    @classmethod
    def enable_output(cls, settings: Flow.Settings, name: str, *, design_name: str) -> None:
        """Enable an output that a consumer demands, in the settings the resolver proposes.

        `design_name` is the name of the design, which the conventional name of a deliverable
        holds (`Flow.Settings.conventional_output`)."""
        declaration = declared_outputs(cls)[name]
        if declaration.cardinality == "optional" and declaration.enabled_by is None:
            raise ValueError(f"{cls.name}.{name} cannot be switched on")
        if not output_enabled(settings, declaration):
            switch_on(settings, declaration)

    @classmethod
    def check_design_supported(
        cls, design: Design, settings: "Flow.Settings | None" = None
    ) -> None:
        """Fail a launch on a design this flow cannot run, before anything is set up or shipped.

        The check runs wherever `check_required_settings` does. A flow that reads the sources of
        the design itself (`reads_sources`) refuses a source in a language it cannot read,
        naming it. A converter (a flow that defines `design_conversion`) refuses none: it passes
        the sources it does not convert through. The check also refuses a design none of whose
        sources the flow reads, naming what the design lists. A flow may refuse more by
        overriding this method and calling it."""
        if cls.reads_sources is None:
            return
        by_part = {part: getattr(design, part).sources for part in sorted(cls.design_parts)}
        listed = [src for sources in by_part.values() for src in sources]
        reads = ", ".join(sorted(member.name for member in cls.reads_sources))
        converter = (
            cls.design_conversion(cls.Settings() if settings is None else settings) is not None
        )
        unread = (
            []
            if converter
            else [
                src
                for src in listed
                if src.type in LANGUAGE_TYPES and src.type not in cls.reads_sources
            ]
        )
        if unread:
            kinds = sorted({src.type.name for src in unread})
            raise FlowSettingsException(
                f"{cls.name} cannot read the design's {' and '.join(kinds)} source(s) "
                f"{', '.join(str(src.path) for src in unread)}; it reads {reads}"
                + cls._testbench_hint(unread, by_part)
            )
        if not any(src.type in cls.reads_sources for src in listed):
            has = ", ".join(
                f"{part}.sources has "
                + (", ".join(f"{src.path} ({src.type.name})" for src in sources) or "none")
                for part, sources in by_part.items()
            )
            raise NoReadableSource(
                f"{cls.name} reads none of the design's sources: {has}; {cls.name} reads {reads}"
            )

    @classmethod
    def _testbench_hint(cls, unread: list[Source], by_part: dict[str, Any]) -> str:
        """Return `; simulate a <language> testbench with <flows>` for a simulator that is given a
        testbench in a language it cannot read, when other simulators read that language; else "".

        The names come from the registry: the simulators that read every unread language."""
        from .sim import SimFlow

        rtl = by_part.get("rtl", [])
        if not issubclass(cls, SimFlow) or any(src is own for src in unread for own in rtl):
            return ""
        languages = {src.type for src in unread}
        names = sorted(
            {
                flow_class.name
                for _module, flow_class in registered_flows.values()
                if issubclass(flow_class, SimFlow)
                and flow_class is not cls
                and flow_class.reads_sources is not None
                and languages <= flow_class.reads_sources
            }
        )
        if not names:
            return ""
        kinds = " and ".join(sorted(member.name for member in languages))
        return f"; simulate a {kinds} testbench with {' or '.join(names)}"

    @classmethod
    def design_conversion(cls, settings: Flow.Settings) -> DesignConversion | None:
        """Return which languages this flow converts and writes, or None when it converts none.

        A flow that defines this method is a converter. It declares exactly one design output
        (`Out(DESIGN, ...)`) that carries the design it derives (the class is refused
        otherwise). It returns None for settings under which it converts nothing. A design
        output without a conversion is allowed: such a flow is no converter and no flow takes
        its design. Pure, like `input_types`.

        The declarations (chains, bindings, default producers) judge a converter by its
        conversion under the default settings: the languages it converts and writes there decide
        which flows it may precede. A converter that returns None under the default settings
        leaves the whole decision to the agreed settings, which the final check applies."""
        return None

    class Inputs(FlowInputs):
        """The declared inputs of the flow: files it reads besides those of its design and settings.

        There are none, unless the flow declares them (`xeda.flow.io.In`). Every flow that reads
        design sources has the input `design` (listed by `declared_inputs`): the design it reads,
        or the design that a converter flow derives from it. The flow reads it as `self.design`;
        the field stays unset."""

        design: Design | None = In(
            DESIGN,
            description="The design the flow reads: the loaded design, or the design that a "
            "converter flow derives from it, such as bsc's Verilog. A flow reads it as "
            "`self.design`.",
        )

    class Outputs(FlowOutputs):
        """The declared outputs of the flow: files it writes for another flow to read.

        There are none, unless the flow declares them (`xeda.flow.io.Out`)."""

    class Settings(XedaBaseModel):
        """The settings that affect the behavior of a flow."""

        verbose: int = Field(
            0,
            description="Verbosity level. Higher values make the tools more talkative.",
            json_schema_extra=count(),
        )
        debug: bool = Field(
            False,
            description="Run the flow in debug mode: verbose logging, and exceptions are re-raised "
            "instead of being reported as a failure.",
        )
        quiet: bool = Field(
            False, description="Run the flow quietly, unless `verbose` or `debug` is set."
        )
        redirect_stdout: bool = Field(
            False, description="Redirect stdout from execution of tools to files."
        )
        nthreads: Optional[int] = Field(
            None,
            description="Maximum number of threads.",
            json_schema_extra=count(),
        )
        reports_dir: Path = Field(
            Path("reports"), json_schema_extra={"hidden_from_schema": True, **WORKING}
        )
        checkpoints_dir: Path = Field(
            Path("checkpoints"), json_schema_extra={"hidden_from_schema": True, **WORKING}
        )
        outputs_dir: Path = Field(
            Path("outputs"), json_schema_extra={"hidden_from_schema": True, **WORKING}
        )
        lib_paths: List[
            Union[
                Tuple[
                    str,  # library name/identifier
                    Union[str, Path, None],  # optional library path
                ],
                Tuple[None, Union[str, Path]],  # or just the path
                # Both name and path can't be None.
            ]
        ] = Field(
            [],
            description="Additional libraries, as a list of (name, path) tuples. The name or the "
            "path can be none, but not both. GHDL and NVC take a relative path as relative to "
            "the design root.",
        )
        docker: Optional[str] = Field(
            None,
            description="Run the tools in this Docker image, instead of the default image of the "
            "flow.",
        )
        dockerized: bool = Field(False, description="Run the tools in Docker.")
        print_commands: bool = Field(True, description="Print the commands that the flow runs.")
        console_colors: bool = Field(True, description="Colorize tool output on the console.")

        @classmethod
        def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
            """Refuse a settings field that has no default, when its class is defined.

            A setting a flow cannot run without is declared in `Flow.required_settings` and
            checked when the flow is launched. The model must not require it: one section of a
            design file holds only some of a flow's settings, and `Optional[X]` without
            `= None` is such a field by mistake.
            """
            super().__pydantic_init_subclass__(**kwargs)
            required = [name for name, info in cls.model_fields.items() if info.is_required()]
            if required:
                names = ", ".join(f"`{name}`" for name in required)
                raise TypeError(
                    f"{cls.__qualname__}: {names} {'has' if len(required) == 1 else 'have'} no "
                    "default. A setting a flow cannot run without goes in "
                    "`Flow.required_settings`, which the launch checks, never in a field "
                    "without a default: give the field a default (`None`, or an empty value) "
                    "and name it in `required_settings`."
                )
            problems = opaque_key_declaration_problems(cls)
            if problems:
                raise TypeError(f"{cls.__qualname__}: " + "; ".join(problems))

        #: Settings that Xeda refuses, with what to use instead (`NoReplacement(reason)` when
        #: nothing replaces one). Giving one is an error that says so, instead of pydantic's
        #: "extra inputs are not permitted". Only a name that was a setting of this class belongs
        #: here.
        removed_settings: ClassVar[Dict[str, str]] = {
            "clean": "the --clean option (clean=True on the launcher)",
            "timeout_seconds": "`timeout`, on the flows that have one (the simulators); no other "
            "flow limits a tool's run time",
            "no_console": NoReplacement("no flow read it"),
            "ncpus": SecondSpelling("`nthreads`"),
        }

        @classmethod
        def removed_setting_replacements(cls) -> Dict[str, str]:
            """Return every removed setting of this class with what replaced it.

            That is the `removed_settings` of the class and of all its bases, and the entry of
            the class itself takes precedence. A flow whose settings have several bases (those of
            `GhdlSynth` come from a simulator and a synthesis flow) is refused what each base
            removed, whether or not its own table repeats it."""
            merged: Dict[str, str] = {}
            for klass in reversed(cls.__mro__):
                merged.update(vars(klass).get("removed_settings", {}))
            return merged

        # Every flow setting gets four input conveniences from `_normalize_flow_setting`. They
        # apply before any field validator runs, so the validators of a flow always receive the
        # normalized value: on construction and on reload of `settings.json` through
        # `_normalize_flow_settings`, on assignment through `__setattr__`. (pydantic runs the
        # validators of a subclass before those of a base class, so a field validator of the base
        # class could not guarantee this. Only the few `before` model validators of subclasses
        # see raw input.)

        @classmethod
        def _normalize_flow_setting(cls, name: str, value: Any, roots: Dict[str, Any]) -> Any:
            """Return the value of one flow setting after its input conveniences:

            1. A list setting given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`).
               Spaces around items and empty items are dropped, so `""` is an empty list, and
               text spelled `[...]` is refused (`dataclass.comma_separated_items`, the one rule,
               which the lists of nested models take text by too). A setting that also accepts
               plain text keeps the text whole. Likewise an optional path given as `""` is
               unset (`-s textcfg=`): as a `Path` it would name the current directory.
            2. `$DESIGN_ROOT`, `$DESIGN_DIR` and `$PWD` (`roots`) are expanded at every `Path`,
               the path fields of a plain model nested in the setting included
               (`cocotb.results_xml`).
            3. A setting whose keys are opaque names (`opaque_key_fields`) takes a dotted key as
               one key: the nested tables that every origin makes of it are joined again.
            4. A waveform setting (`dataclass.waveform`) is read by one rule for every simulator
               (`dataclass.waveform_value`): the text `true` or `false` is the boolean, as it is
               everywhere else, `true` stands for the name the run writes, a name gets the
               format's extension, and a word such as `yes` is refused.
            """
            annotation = field_annotation(cls, name)
            if annotation is None:
                return value
            if name in opaque_key_fields_of(cls):
                value = fold_dotted_keys(value)
            waveform = waveform_format(cls, name)
            if waveform is not None:
                value = waveform_value(value, *waveform)
            if isinstance(value, str) and _is_comma_separated_list(annotation):
                value = comma_separated_items(name, value)
            if value == "" and _is_optional_path(annotation):
                return None
            if value is None or not (
                _annotation_contains_path(annotation) or _plain_model(annotation) is not None
            ):
                return value
            return _expand_path_values(value, annotation, roots)

        #: Where the settings were given: `design_root` and `runner_cwd` (the directory where
        #: Xeda was started). It is the validation context of pydantic (`from_input`), never a
        #: setting, so it is neither written to `settings.json` nor hashed: the same settings
        #: mean the same thing wherever the design lives and wherever Xeda is started.
        _context: dict[str, Any] = PrivateAttr(default_factory=dict)

        @classmethod
        def from_input(
            cls,
            data: Mapping[str, Any],
            *,
            design_root: Path | None = None,
            runner_cwd: Path | None = None,
        ):
            """Validate settings as a user gave them.

            Path variables are resolved against `design_root` and `runner_cwd`, and invalid input
            is a `FlowSettingsError` that lists every problem. The context stays with the
            settings, so an assignment resolves paths the same way.

            This is not an `__init__` override: pydantic validates a model with a custom
            `__init__` through that `__init__`, which loses the validation context.
            """
            context = {"design_root": design_root, "runner_cwd": runner_cwd}
            try:
                return cls.model_validate(dict(data), context=context)
            except ValidationError as e:
                debug = data.get("debug")
                if debug is True or (isinstance(debug, str) and debug.lower() == "true"):
                    raise e
                raise FlowSettingsError(_with_case_hints(cls, e.errors()), cls, e.json()) from e

        @property
        def context(self) -> dict[str, Any]:
            """The validation context these settings were given in; see `from_input`."""
            return self._context

        @staticmethod
        def _path_roots(context: Mapping[str, Any] | None) -> dict[str, Any]:
            return path_variables(context or {})

        def model_post_init(self, context: Any, /) -> None:
            super().model_post_init(context)
            if isinstance(context, Mapping):
                self._attach_context(context)

        def _attach_context(self, context: Mapping[str, Any]) -> None:
            """Attach the input location context to these settings.

            The context is the design root and the start directory. A later assignment then
            resolves paths as the input did."""
            self._context = {k: context.get(k) for k in ("design_root", "runner_cwd")}

        @model_validator(mode="before")
        @classmethod
        def _normalize_flow_settings(cls, values, info):
            """Normalize each supplied flow setting before model validation."""
            if isinstance(values, dict):
                for name, replacement in cls.removed_setting_replacements().items():
                    if name in values:
                        raise removed_setting_error(name, replacement, values[name])
            if info.field_name is not None and info.data is None:
                return values  # an assignment: `__setattr__` has normalized the assigned value
            roots = cls._path_roots(info.context)
            names = input_names(cls)
            refused: List[Tuple[str, ListLiteralText | WaveformText, Any]] = []
            for key, value in values.items():
                if key in names:
                    try:
                        values[key] = cls._normalize_flow_setting(names[key], value, roots)
                    except (ListLiteralText, WaveformText) as error:
                        refused.append((key, error, value))
            if refused:
                raise _list_literal_error(cls, refused)
            return values

        def __setattr__(self, name: str, value: Any) -> None:
            if name in type(self).model_fields:
                try:
                    value = type(self)._normalize_flow_setting(
                        name, value, self._path_roots(self.context)
                    )
                except (ListLiteralText, WaveformText) as error:
                    raise _list_literal_error(type(self), [(name, error, value)]) from None
            super().__setattr__(name, value)

        def conventional_output(self, field: str, design: str) -> Optional[PurePath]:
            """Return the name that the run directory holds the deliverable `field` under.

            This is the name for a deliverable that is given a location
            (`xeda.dataclass.conventional_output`). A flow whose other settings name its outputs
            overrides this method."""
            return conventional_output(type(self), field, design)

        @property
        def is_quiet(self) -> bool:
            """Whether the flow is quiet: `quiet`, unless `verbose` or `debug` asks for more output.

            This is decided where it is read, so `quiet` keeps the value that was written and the
            order of the three settings does not matter."""
            return self.quiet and not (self.verbose or self.debug)

    class Results(Box):
        """The results of a flow, written to `results.json`."""

        def __init__(
            self,
            *args,
            **kwargs,
        ) -> None:
            kwargs |= {
                "box_intact_types": [Box],
                "box_class": Box,
            }
            self.success: bool = False
            self.tools: List[Any] = []
            self.artifacts: Box = Box()
            # Time of the execution of run() in fractional seconds. It is None until the
            # execution has finished.
            self.runtime: Optional[float] = None
            super().__init__(*args, **kwargs)

    @property
    def succeeded(self) -> bool:
        return self.results.success  # pyright: ignore

    def __init_subclass__(cls) -> None:
        check_io_declarations(cls)
        if getattr(cls.design_conversion, "__func__") is not getattr(
            Flow.design_conversion, "__func__"
        ) and not (sum(d.is_design for d in declared_outputs(cls).values()) == 1):
            raise TypeError(
                f"{cls.__qualname__} defines `design_conversion` and must declare exactly one "
                "design output (`Out(DESIGN, ...)`) for the design it derives"
            )
        cls_name = cls.__name__
        mod_name = cls.__module__
        log.info("registering flow %s from %s", cls_name, mod_name)
        cls.name = camelcase_to_snakecase(cls_name)
        if not inspect.isabstract(cls):
            # A flow has one name, `cls.name`: the only spelling that the CLI, the files and the
            # docs accept.
            if cls.name in registered_flows:
                log.warning("Duplicate name: %s while registering flow %s", cls.name, cls_name)
            registered_flows[cls.name] = (mod_name, cls)

    def init(self) -> None:
        """Run the custom initialization of the flow.

        This runs after `__init__`, when more properties are set, and is the usual place for
        initialization. It adds no producer: a flow declares them with `Flow.Inputs`.
        """

    def prepare_inputs(self) -> None:
        """Prepare managed inputs after hand-over, before the check whether the run is up to date.

        Producer inputs are filled and held for reading. This stage runs even when the launch
        finds the run up to date, so it must not write the run directory of the flow. Register
        resolved files in `implicit_inputs`, so that the freshness check and the delivery
        reservations see them before the run. `locate_prepared_inputs` tells what this method
        registers, without making anything.
        """

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the files that `init()` and `prepare_inputs()` register in `implicit_inputs`.

        The answer comes from the settings, the design and the declared inputs that are design
        sources alone. This method writes nothing, starts no process and reads no file of a
        producer. It returns None when one of the files is not made yet (a chip database that no
        run has generated); then the files of a launch do not exist to be compared. A dry run
        uses it to judge whether the run is up to date, on a flow that was not initialized. The
        launch calls it after `prepare_inputs()` and refuses a flow whose registrations differ
        from the answer (`tests/test_prepare_inputs.py`)."""
        return []

    def input_directories(self) -> List[Path]:
        """Return the existing directories that hold the files this flow reads.

        A dockerized tool mounts them read-only (`Tool.execute`). They are:

        - the directory of each declared input, once the launcher has filled it, and of each file
          in `implicit_inputs`;
        - the directory of each existing file that a path-typed setting names, and each existing
          directory that one names, as the trace records them
          (`trace_inputs.setting_read_paths`). A setting that names what the flow writes is no
          input.

        Each path is absolute, as the tool receives it. When a path is a link, the directory of
        its target counts too. Each directory appears once, in the order of the paths."""
        # The trace modules import `Flow`, so this import waits until the method runs.
        from ..flow_runner.trace_inputs import setting_read_paths

        files: List[Path] = []
        for name in declared_inputs(type(self)):
            value = getattr(self.inputs, name, None)
            files.extend(value if isinstance(value, list) else [] if value is None else [value])
        files.extend(self.implicit_inputs)
        named = [Path(os.path.abspath(f)) for f in setting_read_paths(self.settings, self.run_path)]
        found: Dict[Path, None] = {}

        def add(directory: Path) -> None:
            if directory.is_dir():
                found[directory] = None

        for file in files:
            path = Path(os.path.abspath(self.run_path / file))
            add(path.parent)
            add(path.resolve().parent)
        for path in named:
            for holder in (path, path.resolve()):
                add(holder if holder.is_dir() else holder.parent)
        return list(found)

    @classmethod
    def _create_jinja_env(
        cls,
        extra_modules: Optional[List[str]] = None,
        trim_blocks=False,
        lstrip_blocks=False,
    ) -> jinja2.Environment:
        """Create the template environment with Xeda filters and helpers."""
        if extra_modules is None:
            extra_modules = []
        loaderChoices = []
        mod_paths = []
        modules = unique(
            extra_modules + [cls.__module__] + [clz.__module__ for clz in cls.__bases__]
        )
        for mpx in modules:
            for mp in [mpx, mpx.rsplit(".", 1)[0]]:
                if mp not in mod_paths:
                    mod_paths.append(mp)
        for mp in mod_paths:
            try:  # TODO better/cleaner way
                loaderChoices.append(PackageLoader(mp))
            except (ValueError, ImportError):
                # no templates directory, or a module that cannot be imported (a class defined by
                # `exec`, whose module is no module)
                pass
        env = jinja2.Environment(
            loader=ChoiceLoader(loaderChoices),
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=trim_blocks,
            lstrip_blocks=lstrip_blocks,
            finalize=finalize_template_value,
        )
        # A quantity reaches a script through `to`, which names the unit: `{{ period | to("ns") }}`.
        env.filters["to"] = to_filter
        # A path that a user named goes into a TCL script as a literal word, inside quotes, or as
        # a list.
        env.filters.update(tcl_word=tcl_word, tcl_quote=tcl_quote, tcl_list=tcl_list)
        return env

    @property
    def design_root(self):
        return self.design.design_root

    @property
    def run_directory(self) -> RunDirectory:
        """The `RunDirectory` of the flow.

        It is the one way that anything in the run directory is deleted (`RunDirectory.remove`,
        `clear`) or written by Xeda (`writable`). It is decided once, when the flow is
        constructed, and cannot be replaced."""
        return self._run_directory

    def __init__(
        self,
        settings: Union[Settings, Dict],
        design: Union[Design, Dict],
        run_path: Path,
        runner_cwd: Optional[Path] = None,
        run_directory: Optional[RunDirectory] = None,
    ):
        """Construct the flow. A subclass should not override this unless it must.

        `run_directory` is `run_path` as the launcher chose it (`RunDirectory.claimed`), handed
        over here, once. A flow constructed without one holds an `unlaunched` directory, in which
        Xeda deletes nothing.
        """
        # Normalized once, here, to absolute: a relative `run_path` given directly (bypassing the
        # launcher, which always hands one down already absolute) must join, and look up the
        # snapshot below, the same way wherever the current directory later is.
        run_path = Path(os.path.abspath(run_path))
        self.run_path = run_path
        if run_directory is None:
            run_directory = RunDirectory.unlaunched(run_path)
        elif run_directory.path != Path(os.path.realpath(run_path)):
            raise ValueError(f"{run_directory.path} is not the run directory {run_path}")
        self._run_directory = run_directory
        # The state of every existing file and directory under the run directory, by identity.
        # `wrote_output` tells this run's own output from one that an earlier run left by it.
        # No clock takes part, so it is sound across file systems and coarse timestamps alike.
        # A launcher takes it right before the run (`start_run`), once its own records are
        # written. A flow built directly takes it now, before anything it runs can write to the
        # directory.
        self._output_snapshot: Optional[OutputSnapshot] = (
            OutputSnapshot(run_path) if run_directory.run_root is None else None
        )

        if isinstance(design, dict):
            design = dict(design)
            if design.get("design_root") is None:
                design["design_root"] = run_path
            design = Design(**design)
        assert isinstance(design, Design), "design is not a Design object"

        self.design: Design = design
        # The directory where the launcher was invoked, e.g., where the `xeda` command was started.
        self.runner_cwd: Optional[Path] = runner_cwd
        self.init_time: Optional[float] = None
        #: Set by the launcher right before `run()` (`start_run`): from then on, a file in the run
        #: directory that the run did not write is a previous run's (`report_file`).
        self.run_started: bool = False
        self.timestamp: Optional[str] = None
        #: The identity of the run (its settings, its design configuration and where its inputs
        #: come from) and the hash of its settings alone, which the identity is made of
        #: (`bindings.node_identity`). The `design_hash` is the content of the design it reads,
        #: evidence of freshness and no part of the identity.
        self.flow_hash: Optional[str] = None
        self.settings_hash: Optional[str] = None
        self.design_hash: Optional[str] = None
        #: What the run reads of its design (`flow_runner.configuration`): the configuration
        #: that the plan froze for its node.
        self.configuration: Mapping[str, Any] = FrozenDict()
        #: The key of the storage of each generator whose output the run reads (the launcher sets
        #: it), by owner: None for the generator of the design, and the root of the dependency
        #: for the generator of a dependency.
        self.generation_keys: Mapping[Optional[str], str] = {}
        self.declared_input_records: tuple[Any, ...] = ()

        if isinstance(settings, dict):
            settings = self.Settings.from_input(
                settings, design_root=design.root_path, runner_cwd=runner_cwd
            )
        elif isinstance(settings, Flow.Settings):
            # The flow completes its settings (paths, clocks): the caller's are not its to change.
            settings = settings.model_copy(deep=True)

        assert isinstance(settings, self.Settings)
        if runner_cwd is None:
            runner_cwd = settings.context.get("runner_cwd")
            self.runner_cwd = runner_cwd
        if not settings.context or any(value is None for value in settings.context.values()):
            # Attaching context alone would affect only later assignments: path placeholders
            # already present in a directly constructed Settings object would remain unresolved.
            # Revalidate the complete effective value so nested default settings that the caller
            # edited are preserved too.
            settings = self.Settings.from_input(
                settings.model_dump(),
                design_root=settings.context.get("design_root") or design.root_path,
                runner_cwd=settings.context.get("runner_cwd") or runner_cwd,
            )
        assert isinstance(settings, self.Settings)
        settings.outputs_dir = self.process_path(settings.outputs_dir)
        settings.reports_dir = self.process_path(settings.reports_dir)
        settings.checkpoints_dir = self.process_path(settings.checkpoints_dir)
        resolve_conventional_templates(settings, design.name)
        self.settings = settings
        assert isinstance(self.settings, self.Settings)

        # An artifact key maps to one path: the file, relative to run_path or absolute. A flow that
        # registers a list, a table or a directory has a bug (`records.artifact_problem`): the
        # launcher drops that artifact and fails the node.
        self.artifacts = Box()
        #: The artifact key of the log of each program that wrote one in this run, by the name of
        #: the program (`log_stem`).
        self.tool_log_keys: dict[str, str] = {}
        self.results = self.Results()
        self.jinja_env = self._create_jinja_env(extra_modules=[self.__module__])
        self.jinja_env.globals["sources_read"] = self.sources_read
        self.add_template_filter("quote", lambda x: f'"{x}"')
        self.add_template_test("match", regex_match)
        #: Declared inputs. The launcher fills them before it judges whether the run is up to
        #: date; `init()` never reads them.
        self.inputs = type(self).Inputs.model_construct()
        #: Declared outputs, set by the flow and recorded by the launcher after a successful run.
        self.outputs = type(self).Outputs.model_construct()

        #: Makefile-style dependency files the flow's tools wrote (`yosys -E`); read after
        #: `run()` into the trace's implicit inputs.
        self.depfiles: List[Path] = []
        #: Files the flow reads on its own, that no setting or design field names (a board's pin
        #: constraints from its boards file): recorded after `run()` like a depfile's.
        self.implicit_inputs: List[Path] = []
        #: Every report the flow asked for (`report_file`), as an absolute path: the trace
        #: records those inside the run directory, which the launcher removes before the next
        #: run executes, so a previous run's report is gone whatever its mtime.
        self.reports_read: List[Path] = []
        self.reports_missing: List[Path] = []
        #: Set by the launcher: the previous run was still up to date and its results are reused.
        self.reused: bool = False
        #: Set by `reject()`: why the checks of this run rejected its results. A rejection that
        #: the launcher reports again holds the message of the recorded error. On a first run it
        #: is the text given to `reject()`.
        self.rejection: Optional[str] = None
        #: Set by the launcher after the run: "passed", "rejected" (the tool steps completed, the
        #: reports were read, the declared outputs are valid and `reject()` was called) or
        #: "failed" (anything else). Only a "passed" or a "rejected" run keeps a trace. It is
        #: None for a run that raised out of the launcher and for a flow that never started.
        self.outcome: Optional[Literal["passed", "rejected", "failed"]] = None
        #: Set by the launcher: the recorded rejection of the last run was reported again.
        self.rejected_again: bool = False
        #: Set by the launcher: the producer whose failure or rejection stopped this flow.
        self.blocked_by: Optional[str] = None
        #: Set by the launcher when the flow ran although a previous run existed: why.
        self.stale_reason: Optional[str] = None
        #: Set by the launcher when an execution starts, or from the record of a reused run: the
        #: id of the run whose results the flow holds. The record and the trace of the run take it.
        self.run_id: Optional[str] = None
        #: Set by the launcher when an execution starts: the moment, in UTC.
        self.started: Optional[datetime] = None
        #: Set by the launcher: the record of the run whose results the flow holds, once the flow
        #: is complete (it ran, or it was reused). Every reader of the run goes through it.
        self.record: Optional[RunRecord] = None
        #: Set by the launcher: the `run_id` of each producer that handed this flow a file, by
        #: the run directory of the producer.
        self.producer_run_ids: Dict[str, str] = {}
        #: Set by the launcher once the launch has finished: the outputs delivered where the user
        #: named them (`xeda.deliver`).
        self.deliveries: List["Delivered"] = []

    def always_runs(self) -> Optional[str]:
        """Return why this run can never be reused, or None for a run that its trace can vouch for.

        A flow that changes the world outside its run directory (programs a device), whose
        settings ask for a new random seed, or that reads something no trace can verify, returns
        a reason. The launcher then runs the flow every time, keeps no trace of it, and reports
        the reason. It still brings the producers of the flow up to date as usual. The launcher
        asks this once the `init()` of the flow has run and its producers have completed.

        This answers for a flow class that has no source file on disk (defined in a notebook cell,
        for example): Xeda cannot tell whether its code changed, so it computes no code digest for
        it. Every override calls this method."""
        return without_source_file(
            cls for cls in type(self).__mro__ if isinstance(cls, type) and issubclass(cls, Flow)
        )

    @abstractmethod
    def run(self) -> None:
        """Run the tools of the flow.

        Raise an exception on failure, or let `parse_reports()` and `check_results()` report it.
        The launcher ignores the return value."""

    def purge_run_path(self) -> None:
        """Empty the run directory (`RunDirectory.clear`); do nothing without a launcher."""
        self.run_directory.clear()

    def start_run(self) -> None:
        """Mark the start of the run.

        The launcher calls this right before `run()`, once it has written its own records into
        the run directory. From then on, what is written there is this run's (`wrote_output`),
        and a report that is not is a previous run's (`report_file`)."""
        self._output_snapshot = OutputSnapshot(self.run_path)
        self.run_started = True

    def wrote_output(self, path: Union[str, os.PathLike]) -> bool:
        """Return whether this run wrote the output at `path`, a file or a directory.

        The path is relative to the run directory unless it is absolute. The output must be
        there now, and its state (identity, size, mtime or ctime) must differ from what the
        snapshot of the run directory recorded for it before the run. That covers a new file,
        an earlier copy that the tool really overwrote, and one that was merely touched. No
        clock takes part: an output on a file system whose clock is behind, or one read within a
        coarse tick, is told apart by its recorded identity and metadata.

        The snapshot (`run_dir.OutputSnapshot`) is looked up by identity, so every name of a
        file from an earlier run finds the state of that file. A file whose identity the
        snapshot did not record was created (or moved there) since, unless it lies in a
        directory the snapshot could not read; that proves nothing. An output outside the run
        directory, or one asked about before any snapshot was taken, has no known prior state.
        An unknown prior state is never proof that this run wrote it: the results of a failed
        run omit such an artifact rather than risk listing one that an earlier run left."""
        if self._output_snapshot is None:
            return False
        target = Path(os.path.abspath(self.run_path / path))
        current = record_output_state(target)
        if current is None:
            return False  # not there now: this run cannot have written it
        inside = resolved_inside(target, self.run_path)
        if inside is None:
            return False  # outside the run directory: unknown, not proven
        return self._output_snapshot.changed(inside, current) is True

    def parse_reports(self) -> bool:
        log.debug("No parse_reports action for %s", self.name)
        return True

    def check_results(self) -> bool:
        """Check results shared by a family of flows after tool-specific report parsing."""
        return True

    def reject(self, message: str) -> None:
        """Record that the checks of this run rejected its results, and why.

        Call it from `check_results()` when the reports were read and the judgment is negative:
        timing not met, a failed test, a forbidden resource. The run keeps its trace, and a
        launch with the same inputs and settings reports the same rejection again without
        running. A call to `reject()` makes the run rejected, whatever `check_results()`
        returns. A report that is missing or unreadable is no rejection: `parse_reports()`
        returns False for it, and the next launch runs again."""
        self.rejection = message
        if not self.results.get("error"):
            self.results["error"] = ErrorInfo(
                code=REJECTED,
                message=f"`{self.name}` reported failure: {message}",
                node=self.name,
                exit_status=None,
                log=None,
            ).as_json_value()

    def sources_read(self, rtl: bool = True, tb: bool = False) -> list[Source]:
        """Return the sources of the design that this flow reads (`reads_sources`), in design order.

        These are the sources that its code and scripts hand to the tool. `rtl` and `tb` select
        the parts (`Design.sources_of_type`). A flow that declares no `reads_sources` reads no
        source, and asking it is an error."""
        if self.reads_sources is None:
            raise TypeError(
                f"{self.name} declares no `reads_sources`, so it reads none of the design's "
                "sources: declare the types it reads"
            )
        sources = self.design.sources_of_type("*", rtl=rtl, tb=tb)
        return [src for src in sources if src.type in self.reads_sources]

    def copy_from_template(
        self, resource_name, lstrip_blocks=False, trim_blocks=False, script_filename=None, **kwargs
    ) -> Path:
        template = self.jinja_env.get_template(resource_name)
        template.environment.lstrip_blocks = lstrip_blocks
        template.environment.trim_blocks = trim_blocks
        script_path = self.run_file(script_filename or resource_name)

        log.debug("generating %s from template.", str(script_path.resolve()))
        rendered_content = template.render(
            settings=self.settings,
            design=self.design,
            artifacts=self.artifacts,
            **kwargs,
        )
        # Inside the run directory (`RunDirectory.writable`), and only as a complete file, which
        # replaces a link at its name rather than writing through it (`replacing_file`).
        located = self.run_directory.writable(script_path)
        with replacing_file(located) as f:
            f.write(rendered_content)
        # Relative to the run directory as resolved: `run_path` may be a link into the run root.
        return located.relative_to(self.run_directory.path)

    def add_template_filter(self, filter_name: str, func, replace_existing=False) -> None:
        assert filter_name
        if filter_name in self.jinja_env.filters:
            if not replace_existing:
                raise ValueError(f"Template filter with name {filter_name} already exists!")
        self.jinja_env.filters[filter_name] = func

    def add_template_filter_func(self, func) -> None:
        self.add_template_filter(func.__name__, func)

    def add_template_global_func(self, func, filter_name=None) -> None:
        if not filter_name:
            filter_name = func.__name__
        assert filter_name
        if filter_name in self.jinja_env.globals:
            raise ValueError(f"Template global with name {filter_name} already exists!")
        self.jinja_env.globals[filter_name] = func

    def add_template_test(self, filter_name: str, func) -> None:
        self.jinja_env.tests[filter_name] = func

    def parse_report_regex(
        self,
        reportfile_path: Union[str, os.PathLike],
        re_pattern: Union[str, List[str]],
        *other_re_patterns: Union[str, List[str]],
        dotall: bool = True,
        required: bool = False,
        sequential: bool = False,
        optional: bool = False,
    ) -> bool:
        res = self.parse_regex(
            reportfile_path,
            re_pattern,
            *other_re_patterns,
            dotall=dotall,
            required=required,
            sequential=sequential,
            optional=optional,
        )
        if not res:
            return False
        if "success" not in res:
            res["success"] = True
        self.results.update(**res)
        return self.results.success

    def run_file(self, path: Union[str, os.PathLike]) -> Path:
        """Return `path` as an absolute path: a relative one is relative to the run directory.

        A tool runs in the run directory, so the flow names its files relative to it. Python code
        does not depend on the working directory of the process: it reads and writes such a file
        through this method (the file helpers of `Flow` do), never through a relative path."""
        return self.run_path / path

    def written_by_this_run(self, path: Union[str, os.PathLike]) -> bool:
        """Return whether the file `path` exists and is this run's.

        The run must have written it (`wrote_output`, judged by the identity and state of the
        file, never by a clock). For a flow that no launcher started (`run_started` is false),
        any file that exists counts. A relative `path` is relative to the run directory."""
        path = self.run_file(path)
        if not os.path.lexists(path):
            return False
        return not self.run_started or self.wrote_output(path)

    def report_file(
        self, path: Union[str, os.PathLike], *, optional: bool = False
    ) -> Optional[Path]:
        """Return `path`, a report (or any file) that the tools write, if this run wrote it.

        Return None, with a warning, if the file does not exist or is not this run's
        (`written_by_this_run`: its identity and state are what they were before the run). A
        previous run's report, left in a reused run directory, is never taken for this run's.
        Every report a flow reads goes through here (`parse_regex` does). Every `path` asked for
        is noted in `reports_read`, and each one returned as None in `reports_missing`, unless
        `optional` is true. A read is optional when the flow does not fail for the lack of the
        file: the lack is then no cause of a failure, and the launcher never names it as one. A
        relative `path` is relative to the run directory, and the returned path is absolute."""
        path = self.run_file(path)
        self.reports_read.append(Path(os.path.abspath(path)))
        if not path.exists():
            log.warning(
                "File %s does not exist. Check the console output and the log files in %s",
                path.absolute(),
                self.run_path,
            )
            if not optional:
                self.reports_missing.append(Path(os.path.abspath(path)))
            return None
        if not self.written_by_this_run(path):
            log.warning(
                "File %s was not written by this run: it is a previous run's, and is not read. "
                "Check the console output and the log files in %s",
                path.absolute(),
                self.run_path,
            )
            if not optional:
                self.reports_missing.append(Path(os.path.abspath(path)))
            return None
        return path

    def parse_regex(
        self,
        reportfile_path: Union[str, os.PathLike],
        re_pattern: Union[str, List[str]],
        *other_re_patterns: Union[str, List[str]],
        dotall: bool = True,
        required: bool = False,
        sequential: bool = False,
        optional: bool = False,
    ) -> Optional[dict]:
        """Return the values that the patterns find in the report, or None without a report.

        `optional` is the argument of `report_file`."""
        report = self.report_file(reportfile_path, optional=optional)
        if report is None:
            return None
        reportfile_path = report
        return parse_patterns_in_file(
            reportfile_path,
            re_pattern,
            *other_re_patterns,
            dotall=dotall,
            required=required,
            sequential=sequential,
        )

    def resolve_paths_to_design_or_cwd(self, paths: list) -> List[Path]:
        """Expand the variables in `paths` and resolve the relative ones against the design root."""
        return [
            self.process_path(path, subs_vars=True, resolve_to=self.design.design_root)
            for path in paths
        ]

    def resolve_lib_paths(self) -> None:
        """Resolve each relative path of `lib_paths` against the design root, as other path
        settings are. A flow whose tool takes these paths calls this in `init()`, because the tool
        runs in the run directory."""
        resolved: List[Any] = [
            (name, None if path is None else self.normalize_path_to_design_root(path))
            for name, path in self.settings.lib_paths
        ]
        self.settings.lib_paths = resolved

    def process_path(
        self,
        path: Union[str, Path],
        *,
        subs_vars: bool = True,
        resolve_to: Optional[Path] = None,
    ) -> Path:
        if subs_vars and isinstance(path, (str, Path)):
            path = expand_env_vars(
                path,
                overrides=path_variables(
                    {"design_root": self.design.design_root, "runner_cwd": self.runner_cwd}
                ),
            )
        if not isinstance(path, Path):
            path = Path(path)
        if resolve_to is not None and not path.is_absolute() and not str(path).startswith("$"):
            return resolve_to / path
        return path

    def normalize_path_to_design_root(self, path: Union[str, Path]) -> Path:
        return self.process_path(
            path,
            subs_vars=False,
            resolve_to=self.design.design_root,
        )


class FlowException(XedaException):
    """The base class of all flow exceptions."""


class FlowDependencyFailure(FlowException):
    """A producer of a flow failed, or what the producers hand over cannot be used.

    `producer` is the name of the producer that failed or was rejected, when the failure is
    that; it is None for a problem with what the producers hand over."""

    def __init__(self, *args: object, producer: str | None = None) -> None:
        super().__init__(*args)
        self.producer = producer


class FlowSettingsException(FlowException):
    """The settings of a flow failed validation.

    This is a fatal error: the flow must not run. `code` is the stable code of the refusal
    (`xeda.errors.SettingsError.code`). A refusal that names no code is an unsupported setting.
    """

    code: str = "unsupported_setting"
    #: The flow that refused, when the refusal knows it.
    node: str | None = None

    def __init__(self, *args: object, code: str | None = None, node: str | None = None) -> None:
        super().__init__(*args)
        if code is not None:
            self.code = code
        if node is not None:
            self.node = node


class NoReadableSource(FlowSettingsException):
    """A flow that reads the sources of a design (`Flow.reads_sources`) finds none it reads.

    For a producer that a consumer reached by default, the resolver adds the source that would
    have replaced it."""


class FlowSettingsError(FlowSettingsException):
    """The settings of a flow failed validation.

    The constructor takes a list of tuples made from the validation errors of pydantic.
    """

    def __init__(
        self,
        errors: List[
            Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]
        ],  # (location, message, context, type)
        model,
        *args: Any,
    ) -> None:
        super().__init__(*args)
        self.errors = errors
        self.model = model
        #: True for the refusal of settings that the flow cannot support
        #: (`Flow.check_settings_supported`): its findings that name no other kind are
        #: unsupported settings, not invalid values.
        self.refuses_support = False

    def __str__(self) -> str:
        return "{}: {} error{} validating {}\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            getattr(self.model, "__qualname__", self.model),
            "\n".join(f"   {msg}: {loc} ({typ})" for loc, msg, _, typ in self.errors),
        )


class FlowFatalError(FlowException):
    """A fatal error that no other flow exception covers."""
