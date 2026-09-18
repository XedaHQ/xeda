"""Where a flow's settings come from, and how those layers combine.

A flow's settings are assembled from these layers, lowest precedence first:

1. the flow's own defaults (supplied by validation, not here)
2. the project file's ``flows.<flow>`` section
3. the design file's ``[flows.<flow>]`` section
4. the command line (``-s KEY=VALUE``), then overrides given through the API

The layers are merged *deeply*: a nested section such as ``yosys = {...}`` combines key by key,
so ``-s yosys.flatten=true`` changes that one setting of the design's ``yosys`` section instead
of replacing the whole section. Any other value -- a list included -- is replaced whole by a
higher layer. Local and remote runs, and the settings a flow hands to a dependency, all go
through `merge_layers`, so they cannot disagree about precedence.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from ..dataclass import XedaBaseModel
from ..utils import hierarchical_merge, settings_to_dict

__all__ = ["merge_flow_sections", "merge_layers"]

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


def _input_names(settings_cls: type[XedaBaseModel]) -> dict[str, str]:
    """Every accepted simple input name -> the one stored field name."""
    names: dict[str, str] = {}
    for name, info in settings_cls.model_fields.items():
        names[name] = name
        if info.alias:
            names[info.alias] = name
        validation_alias = info.validation_alias
        for choice in getattr(validation_alias, "choices", ()):
            if isinstance(choice, str):
                names[choice] = name
    return names


def _canonicalize_setting_names(
    values: Mapping[str, Any], settings_cls: type[XedaBaseModel]
) -> dict[str, Any]:
    """Canonicalize aliases in one precedence layer, recursively.

    This happens *per layer*, so a higher layer's `nthreads` overrides a lower layer's `ncpus`.
    If one layer gives both spellings, preserve both and let pydantic reject the ambiguity rather
    than silently choosing one.
    """
    input_names = _input_names(settings_cls)
    targets = [input_names.get(key, key) for key in values]
    duplicates = {target for target, count in Counter(targets).items() if count > 1}
    canonical: dict[str, Any] = {}
    for (key, value), target in zip(values.items(), targets):
        output_key = key if target in duplicates else target
        info = settings_cls.model_fields.get(target)
        child_cls = _nested_model(info.annotation) if info is not None else None
        if child_cls is not None and isinstance(value, Mapping):
            value = _canonicalize_setting_names(value, child_cls)
        canonical[output_key] = value
    return canonical


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
                values = _canonicalize_setting_names(values, settings_cls)
            merged = hierarchical_merge(merged, values)
    return merged


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
            settings_cls = flow_cls.Settings if flow_cls is not None else None
            normalized[canonical_name] = merge_layers(values, settings_cls=settings_cls)
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
