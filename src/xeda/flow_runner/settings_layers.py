"""Where a flow's settings come from, and how those layers combine.

A flow's settings are assembled from these layers, lowest precedence first:

1. the flow's own defaults (supplied by validation, not here)
2. under the field holding a dependency's settings (``nextpnr.yosys``), that dependency's own
   sections (``[flows.yosys_fpga]``), in the order below (`flow_settings_from_sections`)
3. the project file's ``flows.<flow>`` section
4. the design file's ``[flows.<flow>]`` section
5. the command line (``-s KEY=VALUE``), then overrides given through the API

The layers are merged *deeply*: a nested section such as ``yosys = {...}`` combines key by key,
so ``-s yosys.flatten=true`` changes that one setting of the design's ``yosys`` section instead
of replacing the whole section. Any other value -- a list included -- is replaced whole by a
higher layer. Local and remote runs, and the settings a flow hands to a dependency, all go
through `merge_layers`, so they cannot disagree about precedence.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from ..dataclass import XedaBaseModel, input_names
from ..flow import Flow, registered_flows
from ..utils import hierarchical_merge, settings_to_dict

__all__ = ["flow_settings_from_sections", "merge_flow_sections", "merge_layers"]

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

    A dependency's section (``[flows.yosys_fpga]``) is the base of the field holding that
    dependency's settings (``nextpnr.yosys``), and the depender's section (``[flows.nextpnr]
    yosys.*``) refines it -- the precedence the dependency's launch uses (`dependency_settings`).
    Composed here, before the depender runs, because only then can the depender see it: a
    setting it shares with the dependency is resolved in its `init()` (`resolve_dependency`),
    long before the dependency launches -- so an `fpga` given only for `yosys_fpga` never
    reached `nextpnr`, which cannot run without one. Recursive: `openfpgaloader.nextpnr.yosys`
    sits on `[flows.yosys_fpga]` as well.
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

    A design file's ``[flows.nextpnr]`` refines the project's ``flows.nextpnr`` rather than
    replacing it. When `flow_class_for` is supplied, aliases such as ``ghdl`` are normalized to
    the canonical flow name; spelling the same flow twice in one section is an error.
    """
    normalized_sections: list[dict[str, dict[str, Any]]] = []
    classes: dict[str, Any] = {}
    for section in sections:
        normalized: dict[str, dict[str, Any]] = {}
        original_names: dict[str, str] = {}
        for name, values in (section or {}).items():
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
