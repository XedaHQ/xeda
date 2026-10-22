"""Where the settings of a flow come from, and how those layers combine.

The settings of a flow are assembled from these layers, lowest precedence first:

1. the defaults of the flow (supplied by validation, not here)
2. with `--remote`, the `flows` table of the host profile (`FlowLauncher.prepare_request` only)
3. the `flows.<flow>` section of the project file
4. the `flows.<flow>` section of the design file
5. the `flows.<flow>` section of the selected target (`--target`), key by key. The target is
   the author of the design saying "for this build, these values", so what it writes wins over
   the design. The loader merges it into the design before anything else sees the design, so it
   is part of the origin of the design, and below the command line and the API
6. the command line (`-s KEY=VALUE`, or the `flow_settings` of `run` and `plan`), then the
   overrides given through the API (`flow_overrides`)

The layers are merged deeply. A nested section such as `clock` combines key by key, so
`-s clock.freq=100MHz` changes that one setting of the `clock` section of the design instead of
replacing the whole section. Any other value, a list included, is replaced whole by a higher
layer. Local and remote runs, and the settings of every producer, all go through
`merge_layers`, so they cannot disagree about precedence.

Every way to launch a flow takes the same layers. `run`, `plan` and the remote runner load the
files and hand them to `FlowLauncher.resolve` as origins. A launch that is given a built design
instead (`run_flow`, `launch_flow`) hands none, and `resolve` takes layers 4 and 5 from the
design itself. Only `run`, `plan` and the remote runner read a project file. Only `prepare_request` takes
layer 2.
"""

import difflib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..dataclass import (
    AtomicName,
    XedaBaseModel,
    annotation_form,
    annotation_mistake,
    appended_fields_of,
    atomic_keys,
    canonical_names,
    fold_dotted_keys,
    input_names,
    is_mistake,
    mapping_form,
    name_inside_another,
    nested_model,
    opaque_key_fields_of,
    opaque_key_paths,
    table_entries,
)
from ..errors import FlowNotFoundError as PublicFlowNotFoundError
from ..flow import Flow, FlowSettingsError, registered_flows
from ..flow.io import declared_inputs
from ..utils import (
    ConflictingKeys,
    EscapedName,
    as_list,
    flows_table_problems,
    hierarchical_merge,
    settings_to_dict,
)

__all__ = [
    "API_ORIGIN",
    "COMMAND_LINE_ORIGIN",
    "HOST_PROFILE_ORIGIN",
    "REMOVED_FLOWS",
    "SUPPLIED_SECTIONS_ORIGIN",
    "FlowNotFoundError",
    "FlowRemovedError",
    "check_not_removed",
    "host_profile_origin",
    "carry_diagnostics",
    "settings_in_context",
    "suggest_dependency_node",
    "registered_flow",
    "compose_flow_settings",
    "merge_flow_sections",
    "merge_layers",
    "check_run_flows",
    "command_line_sections",
    "split_flow_sections",
    "transitive_dependencies",
]


#: How messages name the command-line layer. `run` and `plan` take the command line's `-s` items
#: and the `flow_settings` an API caller gives them as one layer, so the label names both.
COMMAND_LINE_ORIGIN = "the command line (`-s`) or `flow_settings`"
#: The layer that the API's overrides, and the settings a launch is handed directly, form.
API_ORIGIN = "the API"
#: How messages name the `flows` table of a host profile, which `--remote` adds as the lowest
#: layer. `host_profile_origin` adds the name of the host.
HOST_PROFILE_ORIGIN = "the host profile"
#: The `flows` sections that a launch with a built design is handed (`all_flows_settings`).
SUPPLIED_SECTIONS_ORIGIN = "the supplied flow sections"


def host_profile_origin(host: str | None) -> str:
    """Return the label of the host profile of `host`, as messages name that layer."""
    return f"{HOST_PROFILE_ORIGIN} of {host}" if host else HOST_PROFILE_ORIGIN


class FlowNotFoundError(PublicFlowNotFoundError):
    """The name of a flow is no flow. `suggestions` holds the close names."""

    def __init__(self, flow_name: str | None = None, suggestions: Iterable[str] = ()) -> None:
        self.flow_name = flow_name
        close = tuple(suggestions)
        msg = f"Flow '{flow_name}' was not found." if flow_name else "Flow was not found."
        if close:
            msg += " Did you mean: " + ", ".join(close) + "?"
        msg += " Run `xeda list-flows` to see all available flows."
        super().__init__(msg, code="flow_not_found", name=flow_name or "", suggestions=close)


#: Flow names that Xeda refuses, by every normalized spelling (lower case, `-` as `_`) of each
#: name, with the flow's canonical name and what to use instead. `get_flow_class` says
#: so, in the words a removed setting or option uses.
REMOVED_FLOWS = {
    name: ("open_xc7", "fpga_pack to build, openfpgaloader to program")
    for name in ("open_xc7", "openxc7")
}


class FlowRemovedError(FlowNotFoundError):
    """A flow name that Xeda refuses. The message names what to use instead."""

    def __init__(self, flow_name: str, replacement: str) -> None:
        PublicFlowNotFoundError.__init__(
            self,
            f"`{flow_name}` was removed: use {replacement}",
            code="flow_not_found",
            name=flow_name,
        )
        self.flow_name = flow_name


def check_not_removed(flow_name: str) -> None:
    """Refuse the name of a removed flow, in any spelling, wherever a flow is named.

    That is when the user runs the flow and when its name is a section of a `flows` table. A
    removed flow is not an unknown one (a plugin that is not installed, whose section is left
    alone): its settings configure nothing any more, and saying nothing would leave them
    silently unused."""
    removed = REMOVED_FLOWS.get(str(flow_name).strip().replace("-", "_").lower())
    if removed is not None:
        raise FlowRemovedError(*removed)


#: One layer: a (possibly nested, possibly dotted-key) mapping, or `KEY=VALUE` strings.
Layer = None | Mapping[str, Any] | Sequence[str]


def _has_clock_inputs(settings_cls: type[XedaBaseModel]) -> bool:
    """Return whether this settings model has the clock contract of `SynthFlow`.

    `clocks` is a table of named clocks, and `clock` and `main_clock` are properties of it.
    (`RtlSettings` of the design has `clocks` and `clock` too, but as a list of ports, merged by
    `RtlSettings.merge_inputs`.)"""
    return (
        "clocks" in settings_cls.model_fields
        and isinstance(getattr(settings_cls, "clock", None), property)
        and isinstance(getattr(settings_cls, "main_clock", None), property)
    )


def _clock_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, XedaBaseModel):
        return value.model_dump()
    return None


def _canonicalize_clock_input(
    values: dict[str, Any], settings_cls: type[XedaBaseModel]
) -> tuple[dict[str, Any], str | None]:
    """Normalize the single-clock spelling of one layer to `clocks`.

    The returned name marks a singular override. It lets the merge apply `clock` to an existing
    sole or named main clock, rather than adding a second clock. Giving both spellings in the
    same layer is left unchanged on purpose, so validation still rejects the ambiguity.
    """
    if not _has_clock_inputs(settings_cls):
        return values, None
    present = [name for name in ("clock", "clocks") if name in values]
    if len(present) != 1 or present[0] == "clocks":
        return values, None

    spelling = present[0]
    raw = values.pop(spelling)
    if raw is None:
        # `None` means "not specified". It must not erase the clocks of a lower layer.
        return values, None
    mapped = _clock_mapping(raw)
    if mapped is None:
        # Preserve invalid input under its original name for pydantic's field-specific error.
        values[spelling] = raw
        return values, None
    clock = dict(mapped)
    name = str(clock.get("name") or "main_clock")
    values["clocks"] = {name: dict(clock)}
    return values, name


def _merge_clock_values(base: Any, override: Any) -> Any:
    """Merge a `clocks` mapping, treating `period` and `freq` as alternate spellings."""
    if not isinstance(base, Mapping) or not isinstance(override, Mapping):
        return deepcopy(override)
    merged = deepcopy(dict(base))
    for name, raw_clock in override.items():
        new_clock = _clock_mapping(raw_clock)
        old_clock = _clock_mapping(merged.get(name))
        if name in merged and old_clock is None:
            # What is below is no clock: it stays, so that validation reports it.
            continue
        if new_clock is not None and old_clock is not None:
            old_clock = deepcopy(dict(old_clock))
            # A timing constraint from the higher layer replaces the lower layer's alternate
            # spelling. Other attributes (port, uncertainty, duty cycle, ...) merge key by key.
            if "period" in new_clock or "freq" in new_clock:
                old_clock.pop("period", None)
                old_clock.pop("freq", None)
            merged[name] = hierarchical_merge(old_clock, dict(new_clock))
        else:
            merged[name] = deepcopy(raw_clock)
    return merged


def _merge_entries(
    base: Mapping[Any, Any], values: Mapping[Any, Any], entries: Any, append: bool
) -> dict[Any, Any]:
    """Merge two dictionaries whose entries are tables, entry by entry, as a field is merged.

    `entries` is the annotation of the entries that `table_entries` gave. An entry of `base`
    that is no table where one is expected is kept with nothing merged over it, so that
    validation reports it as it would with no layer above. Any other entry written twice merges
    key by key, as the model of the entry merges, and one written once is kept."""
    merged = deepcopy(dict(base))
    child = nested_model(entries)
    for name, value in values.items():
        old_form = annotation_form(entries, merged.get(name))
        if name in merged and annotation_mistake(entries, merged[name], old_form):
            continue
        new_form = annotation_form(entries, value)
        if isinstance(old_form, Mapping) and isinstance(new_form, Mapping):
            merged[name] = (
                _merge_settings_layer(old_form, new_form, child, append)
                if child is not None
                else hierarchical_merge(dict(old_form), dict(new_form))
            )
        else:
            merged[name] = deepcopy(value)
    return merged


def _merge_settings_layer(
    base: Mapping[str, Any],
    values: Mapping[str, Any],
    settings_cls: type[XedaBaseModel],
    append: bool = False,
) -> dict[str, Any]:
    """Merge one parsed layer into `base`, with model-aware aliases and nested settings.

    A value that is a shorthand form of a table (`fpga` as a part number) means exactly that
    table. Where it meets a table, from below or above, the two merge key by key. Where it
    meets nothing, it stays as written. A form that is no table (`WHOLE`), and a value that is
    no form at all, replace what is below them. So does a list, unless `append` is set and the
    model names the field in its `appended_fields`: the items of the layer then follow those
    below."""
    canonical = canonical_names(values, settings_cls)
    canonical, singular_clock = _canonicalize_clock_input(canonical, settings_cls)
    merged = deepcopy(dict(base))
    settings_cls.merge_inputs(merged, canonical)

    for key, value in canonical.items():
        target = input_names(settings_cls).get(key, key)
        info = settings_cls.model_fields.get(target)
        child_cls = nested_model(info.annotation) if info is not None else None
        old = merged.get(key)
        old_form = mapping_form(settings_cls, target, old)
        new_form = mapping_form(settings_cls, target, value)
        if key in merged and info is not None and is_mistake(settings_cls, target, old):
            # What is below is no table where one is expected: it stays, with nothing merged
            # over it, so that validation reports it as it would with no layer above.
            continue
        if key in appended_fields_of(settings_cls):
            adding = append and key in merged
            merged[key] = [*as_list(old), *as_list(value)] if adding else deepcopy(value)
        elif child_cls is not None and isinstance(value, Mapping):
            merged[key] = _merge_settings_layer(
                old_form if isinstance(old_form, Mapping) else {}, value, child_cls, append
            )
        elif (
            child_cls is not None
            and isinstance(new_form, Mapping)
            and isinstance(old_form, Mapping)
        ):
            merged[key] = _merge_settings_layer(old_form, new_form, child_cls, append)
        elif key == "clocks" and _has_clock_inputs(settings_cls) and isinstance(value, Mapping):
            clock_values = dict(value)
            existing_before = merged.get("clocks")
            existing_before = existing_before if isinstance(existing_before, Mapping) else {}
            if singular_clock is not None:
                existing = merged.get("clocks")
                if isinstance(existing, Mapping) and singular_clock not in existing:
                    if singular_clock == "main_clock" and existing:
                        # An unnamed single-clock shorthand targets the same deterministic
                        # main clock that SynthFlow.Settings.main_clock uses: a clock named
                        # `main_clock` first, otherwise the first declared clock.
                        old_name = next(iter(existing))
                        clock_values = {old_name: next(iter(clock_values.values()))}
                    elif len(existing) == 1:
                        # An explicitly named higher `clock` replaces the identity of the sole
                        # lower clock instead of leaving two clocks behind.
                        old_name = next(iter(existing))
                        merged["clocks"] = {singular_clock: existing[old_name]}
                # A singular `clock` without an explicit `name` names its clock `"main_clock"`,
                # matching the default of direct construction
                # (`SynthFlow.Settings._synthflow_settings_root_validator`), but only when it
                # creates a genuinely new clock entry. When it instead refines an existing
                # clock (the redirects above, or a layer that already has a clock of the same
                # name), the identity of the existing clock, its own `name` whatever it is,
                # must win and not be overwritten by the synthesized default.
                for cname, cval in clock_values.items():
                    if (
                        cname not in existing_before
                        and isinstance(cval, Mapping)
                        and not cval.get("name")
                    ):
                        clock_values[cname] = {**cval, "name": cname}
            merged[key] = _merge_clock_values(merged.get(key, {}), clock_values)
        elif isinstance(old_form, Mapping) and isinstance(new_form, Mapping):
            entries = table_entries(info.annotation) if info is not None else None
            merged[key] = (
                _merge_entries(old_form, new_form, entries, append)
                if entries is not None
                else hierarchical_merge(dict(old_form), dict(new_form))
            )
        else:
            merged[key] = deepcopy(value)
    return merged


def _with_atomic_names(
    values: dict[str, Any], settings_cls: type[XedaBaseModel], where: str = ""
) -> dict[str, Any]:
    """Return `values` with each table whose keys are names from a tool as a table of those names.

    These are the settings in `opaque_key_fields_of`, in `settings_cls` and in the groups nested
    in it (`where` is the dotted path of `settings_cls`). The names are kept whole. Every layer
    has expanded the dots of such a name into nested tables, where `A` and `A.B` of two layers
    would meet as a value and a table, and one would replace the other. As names, they merge key
    by key, and a higher layer replaces only a name that it gives."""
    if not opaque_key_paths(settings_cls):
        return values
    names = input_names(settings_cls)
    opaque = opaque_key_fields_of(settings_cls)
    result: dict[str, Any] = {}
    for key, value in values.items():
        target = names.get(key, key)
        info = settings_cls.model_fields.get(target)
        child_cls = nested_model(info.annotation) if info is not None else None
        path = f"{where}{key}"
        if target in opaque:
            result[key] = _atomic_table(path, value)
        elif child_cls is not None and isinstance(value, Mapping):
            result[key] = _with_atomic_names(dict(value), child_cls, path + ".")
        else:
            result[key] = value
    return result


def _atomic_table(key: str, value: Any) -> Any:
    """Return `atomic_keys(value)`, once a table that the user gave has been judged.

    One origin cannot give a name and a name that starts with it and a dot (`A`, `A.B`). A table
    of names already (`AtomicName` keys) is a merge of origins, which may. `key` is the dotted
    path of the setting."""
    atoms = atomic_keys(value)
    if isinstance(atoms, Mapping) and not any(isinstance(k, AtomicName) for k in value):
        pair = name_inside_another(k.name for k in atoms)
        if pair is not None:
            raise ConflictingKeys(f"{key}.{pair[0]}", f"{key}.{pair[1]}")
    return atoms


def merge_layers(
    *layers: Layer, settings_cls: type[XedaBaseModel] | None = None, append: bool = False
) -> dict[str, Any]:
    """Deep-merge `layers`, each one taking precedence over those before it.

    When `settings_cls` is known, accepted aliases are normalized within each layer before the
    merge. So differently spelled names for one setting still obey layer precedence, while two
    names in the same layer remain a validation error.

    A list replaces the list below it, as every layer does. `append` makes the lists that
    `settings_cls` names in its `appended_fields` (the `sources` of a design) the one
    exception. A target sets it, since its job is to add the files of a board.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            values = settings_to_dict(layer)  # type: ignore[arg-type]
            if settings_cls is not None:
                values = _with_atomic_names(values, settings_cls)
                merged = _merge_settings_layer(merged, values, settings_cls, append)
            else:
                merged = hierarchical_merge(merged, values)
    return merged


def compose_flow_settings(
    flow_cls: type[Flow],
    origins: Sequence[Mapping[str, Any] | None],
    *layers: Layer,
) -> dict[str, Any]:
    """Return the settings of `flow_cls` from `flows` sections given by origin.

    The origins come lowest precedence first (project, design, command line, API). Then come
    `layers`: the own settings of the requested flow (the `-s` items of the command line, then
    API overrides). Each origin contributes its own `flows.<flow>` section, and the origins are
    then stacked, so a leaf is decided by where it was given. Composing a result again with the
    merged sections below it changes nothing, so `run()` and `run_flow` may both compose.
    """
    per_origin = [(sections or {}).get(flow_cls.name) for sections in origins]
    return merge_layers(*per_origin, *layers, settings_cls=flow_cls.Settings)


def _folded(value: Any) -> Any:
    """Return `fold_dotted_keys(value)`, or `None` when it holds a name that the setting refuses.

    The refusal is an `EscapedName`. Validation reports it, and this function only looks for
    conflicts."""
    try:
        return fold_dotted_keys(value)
    except EscapedName:
        return None


def _leaves(
    mapping: Mapping[str, Any],
    settings_cls: type[XedaBaseModel] | None = None,
    prefix: tuple[str, ...] = (),
    written: tuple[str, ...] = (),
) -> dict[tuple[str, ...], tuple[str, Any]]:
    """Return every leaf of a nested mapping.

    The result maps the canonical path of a leaf (each name as the model spells it, so an alias
    and its setting are one leaf) to a pair: the path as written, and the value. The names of a
    setting in `opaque_key_fields` are split at their dots, as the expansion of a dotted key
    would split them."""
    leaves: dict[tuple[str, ...], tuple[str, Any]] = {}
    for key, value in mapping.items():
        target = key
        child_cls = None
        if settings_cls is not None:
            target = input_names(settings_cls).get(key, key)
            info = settings_cls.model_fields.get(target)
            child_cls = nested_model(info.annotation) if info is not None else None
        if (
            settings_cls is not None
            and target in opaque_key_fields_of(settings_cls)
            and isinstance(value, Mapping)
            and value
            and isinstance(names := _folded(value), Mapping)
            and all(isinstance(name, str) for name in names)
        ):
            # The keys are names from a tool: make a leaf for each name, at the path of its dots,
            # so that `A` and `A.B` meet as a value and a key inside it, as in one spelling.
            for name, entry in names.items():
                leaves[(*prefix, target, *name.split("."))] = (
                    ".".join((*written, key, name)),
                    entry,
                )
        elif isinstance(value, Mapping) and value:
            leaves.update(_leaves(value, child_cls, (*prefix, target), (*written, key)))
        else:
            leaves[(*prefix, target)] = (".".join((*written, key)), value)
    return leaves


def _is_inside(path: tuple[str, ...], other: tuple[str, ...]) -> bool:
    """Return whether `other` is a key inside the key `path`."""
    return len(other) > len(path) and other[: len(path)] == path


def _sets_value(value: Any) -> bool:
    """Return whether a leaf gives its key a value.

    `None` and an empty table leave the key free for keys inside it (`utils.set_hierarchy`)."""
    return value is not None and not (isinstance(value, Mapping) and not value)


def split_flow_sections(
    layer: Mapping[str, Any],
    requested: str,
    flow_class_for: Callable[[str], Any | None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Split a command-line layer into `flows.<node>.*` sections and the requested flow's own.

    `-s key` and `-s flows.<requested>.key` name one leaf (an alias of a setting is that
    setting). The same value twice is accepted. Two different values are an error that names
    both spellings. The two spellings are one origin for the rule of `utils.set_hierarchy` as
    well: a key is a value or a table, so a value in one spelling and a key inside it in the
    other (`A` and `A.B`) are an error that names both."""
    own = settings_to_dict(layer)  # type: ignore[arg-type]
    sections = merge_flow_sections(
        own.pop("flows", None), flow_class_for=flow_class_for, location=COMMAND_LINE_ORIGIN
    )
    flow_cls = flow_class_for(requested) if flow_class_for is not None else None
    settings_cls = flow_cls.Settings if flow_cls is not None else None
    own_inputs = own.get("inputs")
    section_inputs = sections.get(requested, {}).get("inputs")
    if isinstance(own_inputs, Mapping) and isinstance(section_inputs, Mapping):
        duplicate_inputs = own_inputs.keys() & section_inputs.keys()
        if duplicate_inputs:
            raise FlowSettingsError(
                [
                    (
                        f"inputs.{name}",
                        f"`-s inputs.{name}` and `-s flows.{requested}.inputs.{name}` "
                        "bind one input twice; give one, even when equal",
                        None,
                        "conflicting_bindings",
                    )
                    for name in sorted(duplicate_inputs)
                ],
                settings_cls if settings_cls is not None else requested,
            )
    requested_leaves = _leaves(sections.get(requested, {}), settings_cls)
    problems: list[tuple[str | None, str | None, str | None, str | None]] = []
    for path, (written, value) in _leaves(own, settings_cls).items():
        spelled = f"`-s {written}={value}`"
        for other_path, (other, other_value) in requested_leaves.items():
            qualified = f"`-s flows.{requested}.{other}={other_value}`"
            if other_path == path:
                if other_value != value:
                    message = f"{spelled} and {qualified} set one setting to two values; give one"
                else:
                    continue
            elif _is_inside(path, other_path) and _sets_value(value):
                message = f"{spelled} sets a value, and {qualified} sets a key inside it; give one"
            elif _is_inside(other_path, path) and _sets_value(other_value):
                message = f"{qualified} sets a value, and {spelled} sets a key inside it; give one"
            else:
                continue
            problems.append((written, message, None, "conflicting_spellings"))
    if problems:
        raise FlowSettingsError(problems, settings_cls if settings_cls is not None else requested)
    return sections, own


def command_line_table(requested: str, items: Iterable[str]) -> dict[str, Any]:
    """Return the `-s` items of a command line as a `flows` table: the settings by flow name.

    An item without a flow name is a setting of the `requested` flow, the last flow of the
    chain. Nothing is checked or canonicalized: the table says what the command line gave. Items
    that cannot be read give an empty table."""
    try:
        given = settings_to_dict(list(items))
    except Exception:  # pylint: disable=broad-except
        return {}
    flows = given.pop("flows", None)
    table: dict[str, Any] = dict(flows) if isinstance(flows, Mapping) else {}
    if given:
        own = table.get(requested)
        table[requested] = {**own, **given} if isinstance(own, Mapping) else given
    return table


def check_run_flows(
    sections: Mapping[str, Any],
    flow_cls: type[Flow],
    run_flows: Collection[str] | None = None,
) -> None:
    """Refuse a `flows.<name>` section of the command line that names a flow outside this run.

    The sections may name only the flows of this run: the requested flow and its transitive
    declared producers, or `run_flows` when the resolver knows the graph (an explicit binding
    can reach a flow outside the default one). Any other name is an error with the close matches
    among them."""
    run_flows = sorted({flow_cls.name, *(run_flows or transitive_dependencies(flow_cls))})
    unknown = [name for name in sections if name not in run_flows]
    if unknown:
        raise FlowSettingsError(
            [
                (
                    f"flows.{name}",
                    f"`-s flows.{name}.*` names no flow of this run ({', '.join(run_flows)})"
                    + "".join(
                        f"; did you mean `flows.{m}`?"
                        for m in difflib.get_close_matches(name, run_flows, n=3)
                    ),
                    None,
                    "unknown_flow",
                )
                for name in unknown
            ],
            flow_cls.Settings,
        )


def command_line_sections(
    layer: Mapping[str, Any],
    flow_cls: type[Flow],
    flow_class_for: Callable[[str], Any | None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return `(sections, own)` of the command line for a run of `flow_cls`.

    That is `split_flow_sections`, then `check_run_flows`. Local and remote runs both take the
    command line through this function."""
    sections, own = split_flow_sections(layer, flow_cls.name, flow_class_for)
    check_run_flows(sections, flow_cls)
    return sections, own


def registered_flow(name: str) -> type[Flow] | None:
    """Resolve a registered canonical name, class name or alias, without importing the runner."""
    normalized = name.strip().replace("-", "_").lower()
    return next(
        (cls for key, (_module, cls) in registered_flows.items() if key.lower() == normalized),
        None,
    )


def transitive_dependencies(flow_cls: type[Flow]) -> dict[str, type[Flow]]:
    """Return the flows of the declared default producers, transitively, in one traversal.

    A converter flow that can feed the `design` input of a flow is a producer of it too
    (`chains.converter_edges`), so `-s flows.bsc.<setting>` names a flow of a run of `yosys`."""
    from .chains import converter_edges

    found: dict[str, type[Flow]] = {}
    visited = {flow_cls.name}

    def visit(cls: type[Flow]) -> None:
        dependencies = [
            registered_flow(declaration.producer)
            for declaration in declared_inputs(cls).values()
            if declaration.producer is not None
        ] + [edge.producer for edge in converter_edges(cls)]
        for dependency in dependencies:
            if dependency is not None and dependency.name not in visited:
                visited.add(dependency.name)
                found[dependency.name] = dependency
                visit(dependency)

    visit(flow_cls)
    return found


def _flows_error(
    problems: Sequence[tuple[str, str]], location: str | None, kind: str
) -> FlowSettingsError:
    """Return the error for `problems` found in the `flows` table of one origin.

    Each problem is a `(key path, problem)` pair, and all are reported at once. `location` names
    the origin. `kind` is the type of the error."""
    where = f"{location}: " if location else ""
    return FlowSettingsError(
        [(path, f"{where}`{path}` {problem}", None, kind) for path, problem in problems], "flows"
    )


def merge_flow_sections(
    *sections: Mapping[str, Any] | None,
    flow_class_for: Callable[[str], Any | None] | None = None,
    location: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge `flows` sections (flow name to settings) flow by flow, later sections winning.

    The `flows.nextpnr` section of a design file refines the `flows.nextpnr` section of the
    project instead of replacing it. When `flow_class_for` is supplied, aliases such as `ghdl`
    are normalized to the canonical flow name. Spelling the same flow twice in one section is
    an error. So is a section for a removed flow (`check_not_removed`), wherever it was
    written: every `flows` table (a design's, a project's, the command line's, the API's) is
    merged here.

    Every table is also judged for its shape here (`flows_table_problems`): the table and the
    section of each flow are mappings, or a section is `KEY=VALUE` text, which is how code
    gives a layer, and each key is the name of a flow, as text. A table that is not so is a
    `FlowSettingsError` that names it and, in `location`, the origin it came from. Nothing in a
    table is read before it passes.
    """
    normalized_sections: list[dict[str, dict[str, Any]]] = []
    classes: dict[str, Any] = {}
    for section in sections:
        problems = flows_table_problems(section, layers=True)
        if problems:
            raise _flows_error(problems, location, "dict_type")
        normalized: dict[str, dict[str, Any]] = {}
        original_names: dict[str, str] = {}
        for name, values in (section or {}).items():
            check_not_removed(name)
            flow_cls = flow_class_for(name) if flow_class_for is not None else None
            canonical_name = flow_cls.name if flow_cls is not None else name
            if canonical_name in normalized:
                raise _flows_error(
                    [
                        (
                            f"flows.{canonical_name}",
                            f"is given twice in one `flows` section, as "
                            f"{original_names[canonical_name]!r} and {name!r}: keep one",
                        )
                    ],
                    location,
                    "duplicate_flow",
                )
            # Keep this section as one distinct precedence layer. Canonicalizing it here would
            # erase the fact that `clock` was a single-clock shorthand before it
            # is compared with the lower section (and could incorrectly add a second clock
            # instead of refining the lower section's sole named clock).
            normalized[canonical_name] = settings_to_dict(values)
            original_names[canonical_name] = name
            if flow_cls is not None:
                classes[canonical_name] = flow_cls
        normalized_sections.append(normalized)

    names = [name for section in normalized_sections for name in section]
    return {
        name: merge_layers(
            *(section.get(name) for section in normalized_sections),
            settings_cls=(classes[name].Settings if name in classes else None),
        )
        for name in dict.fromkeys(names)
    }


def carry_diagnostics(settings: Flow.Settings, depender: Flow.Settings) -> None:
    """Carry `debug` and an inherited verbose level into a producer, before hashing."""
    settings.debug |= depender.debug
    if not settings.verbose and depender.verbose > 1:
        settings.verbose = depender.verbose


def settings_in_context(
    flow_cls: type[Flow],
    given: Mapping[str, Any] | Flow.Settings | None,
    design_root: Path | None,
    runner_cwd: Path | None,
) -> Flow.Settings:
    """Validate mappings in the launch context. Copy models and keep their original context."""
    if given is None or isinstance(given, Mapping):
        return flow_cls.Settings.from_input(
            given or {}, design_root=design_root, runner_cwd=runner_cwd
        )
    if not given.context:
        return flow_cls.Settings.from_input(
            given.model_dump(), design_root=design_root, runner_cwd=runner_cwd
        )
    return given.model_copy(deep=True)


def suggest_dependency_node(flow_cls: type[Flow], error: FlowSettingsError) -> None:
    """Suggest `-s flows.<node>.key` when an unknown root setting belongs to a producer."""
    dependencies = transitive_dependencies(flow_cls)
    suggested = []
    for location, message, context, kind in error.errors:
        # A hint for a name in another case already names the setting this flow has.
        if (
            kind == "extra_forbidden"
            and location
            and " -> " not in location
            and ("did you mean" not in (message or ""))
        ):
            owner = next(
                (d for d in dependencies.values() if location in d.Settings.model_fields), None
            )
            if owner is not None:
                message = f"{message}; did you mean -s flows.{owner.name}.{location}=...?"
        suggested.append((location, message, context, kind))
    error.errors = suggested
