"""Where a flow's settings come from, and how those layers combine.

A flow's settings are assembled from these layers, lowest precedence first:

1. the flow's own defaults (supplied by validation, not here)
2. for a flow that still launches a dependency itself, under the field holding that
   dependency's settings (``vivado_postsynth_sim.synth``), the dependency's own sections
   (``flows.vivado_synth``), in the order below (`flow_settings_from_sections`); a declared
   producer's settings are written under its own ``flows.<flow>`` only
3. the project file's ``flows.<flow>`` section
4. the design file's ``flows.<flow>`` section
5. the command line (``-s KEY=VALUE``), then overrides given through the API

The layers are merged *deeply*: a nested section such as ``clock = {...}`` combines key by key,
so ``-s clock.freq=100MHz`` changes that one setting of the design's ``clock`` section instead
of replacing the whole section. Any other value -- a list included -- is replaced whole by a
higher layer. Local and remote runs, and the settings a flow hands to a dependency, all go
through `merge_layers`, so they cannot disagree about precedence.
"""

import difflib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from ..dataclass import XedaBaseModel, input_names
from ..flow import Flow, FlowSettingsError, registered_flows
from ..flow.io import declared_inputs
from ..utils import XedaException, hierarchical_merge, settings_to_dict

__all__ = [
    "REMOVED_FLOWS",
    "FlowNotFoundError",
    "FlowRemovedError",
    "check_not_removed",
    "carry_diagnostics",
    "dependency_settings",
    "settings_in_context",
    "suggest_dependency_node",
    "registered_flow",
    "compose_flow_settings",
    "flow_settings_from_sections",
    "merge_flow_sections",
    "merge_layers",
    "check_run_flows",
    "command_line_sections",
    "split_flow_sections",
    "transitive_dependencies",
]


class FlowNotFoundError(XedaException):
    def __init__(self, flow_name: str | None = None, suggestions: Iterable[str] = ()) -> None:
        self.flow_name = flow_name
        self.suggestions = list(suggestions)
        msg = f"Flow '{flow_name}' was not found." if flow_name else "Flow was not found."
        if self.suggestions:
            msg += " Did you mean: " + ", ".join(self.suggestions) + "?"
        msg += " Run `xeda list-flows` to see all available flows."
        super().__init__(msg)


#: Flows that no longer exist, by every normalized spelling (lower case, `-` as `_`) of the
#: names they had, with the flow's canonical name and what replaced it. `get_flow_class` says
#: so, in the words a removed setting or option uses.
REMOVED_FLOWS = {
    name: ("open_xc7", "fpga_pack to build, openfpgaloader to program")
    for name in ("open_xc7", "openxc7")
}


class FlowRemovedError(FlowNotFoundError):
    """A flow that was removed: the message names what to use instead."""

    def __init__(self, flow_name: str, replacement: str) -> None:
        self.flow_name = flow_name
        self.suggestions = []
        XedaException.__init__(self, f"`{flow_name}` was removed: use {replacement}")


def check_not_removed(flow_name: str) -> None:
    """Refuse a removed flow's name, in any spelling, wherever a flow is named: to run it, or
    as a section of a `flows` table. A removed flow is not an unknown one (a plugin that is not
    installed, whose section is left alone): its settings configure nothing any more, and
    saying nothing would leave them silently unused."""
    removed = REMOVED_FLOWS.get(str(flow_name).strip().replace("-", "_").lower())
    if removed is not None:
        raise FlowRemovedError(*removed)


#: One layer: a (possibly nested, possibly dotted-key) mapping, or `KEY=VALUE` strings.
Layer = None | Mapping[str, Any] | Sequence[str]


def _nested_model(annotation: Any) -> type[XedaBaseModel] | None:
    """The model a mapping value of `annotation` is validated as, if it has one."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _nested_model(get_args(annotation)[0])
    if origin in (Union, UnionType):
        for choice in get_args(annotation):
            model = _nested_model(choice)
            if model is not None:
                return model
        return None
    if isinstance(annotation, type) and issubclass(annotation, XedaBaseModel):
        return annotation
    return None


def _canonicalize_setting_names(
    values: Mapping[str, Any], settings_cls: type[XedaBaseModel]
) -> dict[str, Any]:
    """Canonicalize aliases at one model level in one precedence layer.

    This happens *per layer*, so a higher layer's `nthreads` overrides a lower layer's `ncpus`.
    If one layer gives both spellings, preserve both and let pydantic reject the ambiguity rather
    than silently choosing one.
    """
    names = input_names(settings_cls)
    targets = [names.get(key, key) for key in values]
    duplicates = {target for target, count in Counter(targets).items() if count > 1}
    canonical: dict[str, Any] = {}
    for (key, value), target in zip(values.items(), targets):
        output_key = key if target in duplicates else target
        canonical[output_key] = value
    return canonical


def _has_clock_inputs(settings_cls: type[XedaBaseModel]) -> bool:
    """Whether this settings model has SynthFlow's canonical clock contract."""
    return (
        "clocks" in settings_cls.model_fields
        and isinstance(getattr(settings_cls, "clock", None), property)
        and isinstance(getattr(settings_cls, "clock_period", None), property)
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
    """Normalize one layer's single-clock spelling to ``clocks``.

    The returned name marks a singular override. It lets the merge apply `clock_period` or
    `clock` to an existing sole/named main clock rather than adding a second clock. Giving more
    than one spelling in this *same* layer is intentionally left unchanged, so validation still
    rejects the ambiguity.
    """
    if not _has_clock_inputs(settings_cls):
        return values, None
    present = [name for name in ("clock", "clock_period", "clocks") if name in values]
    if len(present) != 1 or present[0] == "clocks":
        return values, None

    spelling = present[0]
    raw = values.pop(spelling)
    if raw is None:
        # `None` historically meant "not specified". It must not erase a lower layer's clocks.
        return values, None
    if spelling == "clock_period":
        clock: Mapping[str, Any] = {"period": raw}
        name = "main_clock"
    else:
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
    """Merge a `clocks` mapping while treating `period` and `freq` as alternate spellings."""
    if not isinstance(base, Mapping) or not isinstance(override, Mapping):
        return deepcopy(override)
    merged = deepcopy(dict(base))
    for name, raw_clock in override.items():
        new_clock = _clock_mapping(raw_clock)
        old_clock = _clock_mapping(merged.get(name))
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


def _merge_settings_layer(
    base: Mapping[str, Any], values: Mapping[str, Any], settings_cls: type[XedaBaseModel]
) -> dict[str, Any]:
    """Merge one already-parsed layer with model-aware aliases and nested settings."""
    canonical = _canonicalize_setting_names(values, settings_cls)
    canonical, singular_clock = _canonicalize_clock_input(canonical, settings_cls)
    merged = deepcopy(dict(base))

    for key, value in canonical.items():
        target = input_names(settings_cls).get(key, key)
        info = settings_cls.model_fields.get(target)
        child_cls = _nested_model(info.annotation) if info is not None else None
        old = merged.get(key)
        if child_cls is not None and isinstance(value, Mapping):
            merged[key] = _merge_settings_layer(
                old if isinstance(old, Mapping) else {}, value, child_cls
            )
        elif key == "clocks" and _has_clock_inputs(settings_cls) and isinstance(value, Mapping):
            clock_values = dict(value)
            existing_before = merged.get("clocks")
            existing_before = existing_before if isinstance(existing_before, Mapping) else {}
            if singular_clock is not None:
                existing = merged.get("clocks")
                if isinstance(existing, Mapping) and singular_clock not in existing:
                    if singular_clock == "main_clock" and existing:
                        # An unnamed single-clock shorthand targets the same deterministic
                        # legacy main used by SynthFlow.Settings.main_clock: explicit
                        # ``main_clock`` first, otherwise the first declared clock.
                        old_name = next(iter(existing))
                        clock_values = {old_name: next(iter(clock_values.values()))}
                    elif len(existing) == 1:
                        # An explicitly named higher `clock` replaces the identity of the sole
                        # lower clock instead of leaving two clocks behind.
                        old_name = next(iter(existing))
                        merged["clocks"] = {singular_clock: existing[old_name]}
                # A singular spelling without an explicit name (`clock_period`, or `clock` with
                # no `name`) names its clock `"main_clock"`, matching
                # `SynthFlow.Settings._synthflow_settings_root_validator`'s direct-construction
                # default -- but only when it creates a genuinely new clock entry. When it
                # instead refines an already-existing clock (the redirects above, or a layer
                # that already has a same-named clock), the existing clock's identity -- its own
                # `name`, whatever it is -- must win, not be overwritten by the synthesized
                # default.
                for cname, cval in clock_values.items():
                    if (
                        cname not in existing_before
                        and isinstance(cval, Mapping)
                        and not cval.get("name")
                    ):
                        clock_values[cname] = {**cval, "name": cname}
            merged[key] = _merge_clock_values(merged.get(key, {}), clock_values)
        elif isinstance(old, Mapping) and isinstance(value, Mapping):
            merged[key] = hierarchical_merge(dict(old), dict(value))
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_layers(*layers: Layer, settings_cls: type[XedaBaseModel] | None = None) -> dict[str, Any]:
    """Deep-merge `layers`, each one taking precedence over those before it.

    When `settings_cls` is known, accepted aliases are normalized within each layer before the
    merge. Thus differently-spelled names for one setting still obey layer precedence, while two
    names in the *same* layer remain an explicit validation error.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            values = settings_to_dict(layer)  # type: ignore[arg-type]
            if settings_cls is not None:
                merged = _merge_settings_layer(merged, values, settings_cls)
            else:
                merged = hierarchical_merge(merged, values)
    return merged


def flow_settings_from_sections(
    flow_cls: type[Flow], sections: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What merged `flows` sections (`merge_flow_sections`) say for `flow_cls`: its own
    section, over each of its declared dependencies' own sections.

    A dependency's section (``flows.vivado_synth``) is the base of the field holding that
    dependency's settings (``vivado_postsynth_sim.synth``), and the depender's section
    (``flows.vivado_postsynth_sim.synth.*``) refines it -- the precedence the dependency's
    launch uses (`dependency_settings`). Composed here, before the depender runs, because only
    then can the depender see it: a setting it shares with the dependency is resolved in its
    `init()` (`resolve_dependency`), long before the dependency launches. Recursive. Only
    flows that still launch a dependency themselves have such a field; a declared producer's
    settings are written under its own ``flows.<flow>`` (D-10).
    """
    sections = sections or {}
    dependencies: dict[str, Any] = {}
    for field in flow_cls.Settings.dependency_settings:
        dependency = _flow_of(flow_cls.Settings._dependency_settings_class(field))
        if dependency is not None:
            section = flow_settings_from_sections(dependency, sections)
            if section:
                dependencies[field] = section
    return merge_layers(dependencies, sections.get(flow_cls.name), settings_cls=flow_cls.Settings)


def compose_flow_settings(
    flow_cls: type[Flow],
    origins: Sequence[Mapping[str, Any] | None],
    *layers: Layer,
) -> dict[str, Any]:
    """`flow_cls`'s settings from `flows` sections given by origin, lowest precedence first
    (project, design, command line, API), then `layers` -- the requested flow's own settings
    (the command line's `-s`, then API overrides).

    Each origin is composed on its own first -- a dependency's own section under the depender's
    nested value for it (`flow_settings_from_sections`) -- and the composed origins are then
    stacked. So a leaf is decided by where it was given first, and by nesting only within one
    origin: a design file's ``flows.vivado_synth.fail_timing`` beats a project file's
    ``flows.vivado_postsynth_sim.synth.fail_timing``, while within the design file the nested
    value beats ``flows.vivado_synth.fail_timing``. Composing a result again with the
    merged sections below it changes nothing, so `run()` and `run_flow` may both compose.
    """
    per_origin = [flow_settings_from_sections(flow_cls, sections or {}) for sections in origins]
    return merge_layers(*per_origin, *layers, settings_cls=flow_cls.Settings)


def _leaves(
    mapping: Mapping[str, Any],
    settings_cls: type[XedaBaseModel] | None = None,
    prefix: tuple[str, ...] = (),
    written: tuple[str, ...] = (),
) -> dict[tuple[str, ...], tuple[str, Any]]:
    """Every leaf of a nested mapping: its canonical path (each name as the model spells it, so
    an alias and its setting are one leaf) -> (the path as written, its value)."""
    leaves: dict[tuple[str, ...], tuple[str, Any]] = {}
    for key, value in mapping.items():
        target = key
        child_cls = None
        if settings_cls is not None:
            target = input_names(settings_cls).get(key, key)
            info = settings_cls.model_fields.get(target)
            child_cls = _nested_model(info.annotation) if info is not None else None
        if isinstance(value, Mapping) and value:
            leaves.update(_leaves(value, child_cls, (*prefix, target), (*written, key)))
        else:
            leaves[(*prefix, target)] = (".".join((*written, key)), value)
    return leaves


def split_flow_sections(
    layer: Mapping[str, Any],
    requested: str,
    flow_class_for: Callable[[str], Any | None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Split a command-line layer into its ``flows.<node>.*`` sections and the requested flow's
    own settings. ``-s key`` and ``-s flows.<requested>.key`` name one leaf (an alias of a
    setting is that setting): the same value twice is accepted, two different values are an
    error naming both spellings."""
    own = settings_to_dict(layer)  # type: ignore[arg-type]
    sections = merge_flow_sections(own.pop("flows", None) or {}, flow_class_for=flow_class_for)
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
    conflicts = [
        (written, value, requested_leaves[path])
        for path, (written, value) in _leaves(own, settings_cls).items()
        if path in requested_leaves and requested_leaves[path][1] != value
    ]
    if conflicts:
        raise FlowSettingsError(
            [
                (
                    key,
                    f"`-s {key}={a}` and `-s flows.{requested}.{other}={b}` set one setting to "
                    "two values; give one",
                    None,
                    "conflicting_spellings",
                )
                for key, a, (other, b) in conflicts
            ],
            settings_cls if settings_cls is not None else requested,
        )
    return sections, own


def check_run_flows(
    sections: Mapping[str, Any],
    flow_cls: type[Flow],
    run_flows: Collection[str] | None = None,
) -> None:
    """The command line's `flows.<name>` sections may name only the flows of this run: the
    requested flow and its transitive declared dependencies, or `run_flows` when the resolver
    knows the graph (an explicit binding can reach a flow outside the default one). Any other
    name is an error with the close matches among them."""
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
    """`(sections, own)` of the command line for a run of `flow_cls`: `split_flow_sections`,
    then `check_run_flows`. Local and remote runs both take the command line through this."""
    sections, own = split_flow_sections(layer, flow_cls.name, flow_class_for)
    check_run_flows(sections, flow_cls)
    return sections, own


def registered_flow(name: str) -> type[Flow] | None:
    """Resolve a registered canonical name, class name or alias without importing the runner."""
    normalized = name.strip().replace("-", "_").lower()
    return next(
        (cls for key, (_module, cls) in registered_flows.items() if key.lower() == normalized),
        None,
    )


def transitive_dependencies(flow_cls: type[Flow]) -> dict[str, type[Flow]]:
    """Nested settings dependencies and declared default producers, with one traversal state."""
    found: dict[str, type[Flow]] = {}
    visited = {flow_cls.name}

    def visit(cls: type[Flow]) -> None:
        dependencies = [
            _flow_of(cls.Settings._dependency_settings_class(field))
            for field in cls.Settings.dependency_settings
        ]
        dependencies.extend(
            registered_flow(declaration.producer)
            for declaration in declared_inputs(cls).values()
            if declaration.producer is not None
        )
        for dependency in dependencies:
            if dependency is not None and dependency.name not in visited:
                visited.add(dependency.name)
                found[dependency.name] = dependency
                visit(dependency)

    visit(flow_cls)
    return found


def _flow_of(settings_cls: type[Flow.Settings] | None) -> type[Flow] | None:
    """The registered flow whose settings class `settings_cls` is."""
    if settings_cls is None:
        return None
    return next(
        (cls for _module, cls in registered_flows.values() if cls.Settings is settings_cls), None
    )


def merge_flow_sections(
    *sections: Mapping[str, Any] | None,
    flow_class_for: Callable[[str], Any | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge `flows` sections (flow name -> settings) flow by flow, later sections winning.

    A design file's ``flows.nextpnr`` refines the project's ``flows.nextpnr`` rather than
    replacing it. When `flow_class_for` is supplied, aliases such as ``ghdl`` are normalized to
    the canonical flow name; spelling the same flow twice in one section is an error. So is a
    section for a removed flow (`check_not_removed`), wherever it was written: every `flows`
    table -- a design's, a project's, the command line's, the API's -- is merged here.
    """
    normalized_sections: list[dict[str, dict[str, Any]]] = []
    classes: dict[str, Any] = {}
    for section in sections:
        normalized: dict[str, dict[str, Any]] = {}
        original_names: dict[str, str] = {}
        for name, values in (section or {}).items():
            check_not_removed(name)
            flow_cls = flow_class_for(name) if flow_class_for is not None else None
            canonical_name = flow_cls.name if flow_cls is not None else name
            if canonical_name in normalized:
                raise ValueError(
                    f"Flow settings for {canonical_name!r} are given twice in one `flows` "
                    f"section, as {original_names[canonical_name]!r} and {name!r}. Keep one."
                )
            # Keep this section as one distinct precedence layer. Canonicalizing it here would
            # erase the fact that `clock_period`/`clock` was a single-clock shorthand before it
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
    """Carry debug and an inherited verbose level into a dependency before hashing."""
    settings.debug |= depender.debug
    if not settings.verbose and depender.verbose > 1:
        settings.verbose = depender.verbose


def dependency_settings(
    dep_cls: type[Flow],
    given: Flow.Settings | None,
    depender_settings: Flow.Settings,
    all_flows_settings: Mapping[str, Any] | None = None,
) -> Flow.Settings:
    """The settings a dependency is launched with -- composed here, and only here.

    `given` is what the depending flow passed to `add_dependency` (for a declared dependency,
    already `resolve_dependency`-d). Layers, lowest precedence first:

    1. the design's / project's own section for the dependency's flow (``flows.yosys_fpga``),
       merged deeply like any settings layer (`settings_layers.merge_layers`);
    2. `given`, which is more specific.

    The depending flow's diagnostics then carry over: `debug` if it is on, and a `verbose` level
    above 1 when the dependency has none of its own.
    """
    section = (all_flows_settings or {}).get(dep_cls.name)
    if section or given is None or not given.context:
        own = given.model_dump(exclude_unset=True) if given is not None else {}
        settings = dep_cls.Settings.from_input(
            merge_layers(section, own, settings_cls=dep_cls.Settings),
            **depender_settings.context,
        )
    else:
        settings = given
    carry_diagnostics(settings, depender_settings)
    return settings


def settings_in_context(
    flow_cls: type[Flow],
    given: Mapping[str, Any] | Flow.Settings | None,
    design_root: Path | None,
    runner_cwd: Path | None,
) -> Flow.Settings:
    """Validate mappings in launch context; copy models, preserving their original context."""
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
    """Suggest -s flows.<node>.key when an unknown root setting belongs to a dependency."""
    dependencies = transitive_dependencies(flow_cls)
    suggested = []
    for location, message, context, kind in error.errors:
        if kind == "extra_forbidden" and location and " -> " not in location:
            owner = next(
                (d for d in dependencies.values() if location in d.Settings.model_fields), None
            )
            if owner is not None:
                message = f"{message}; did you mean -s flows.{owner.name}.{location}=...?"
        suggested.append((location, message, context, kind))
    error.errors = suggested
