"""The design configuration of a node: what the node reads of its design, without content.

The configuration is a part of the identity of a node (`bindings.node_identity`), so two
designs that differ in what a flow reads (its top, a parameter, the places and types of its
sources) are two nodes with two run directories. The content of a source is no part of it: it
is evidence of freshness (`Design._parts_hash`, the trace).

This module is the one rule. The resolver freezes the configuration of every node in its plan,
the launcher recomputes it from the same view of the design, and the record of a run
(`records.record_of`) holds the one of its node.

The shape is that of a part of `Design.to_dict()`:

- for each part of `Flow.design_parts` (`rtl`, `tb`): its metadata (`Design.part_metadata`) and,
  for a node that reads the design's sources, its `sources`, a list of `{path, type}` (a file of a
  Git dependency also says which dependency, and its path is in the checkout; a generated source
  also holds the recipe of its generator as written, `generated_by`);
- `language`, when the node reads `rtl`;
- for a design that a converter derives, the conversion (`converted_from`, `transforms`,
  `produces`) and no `sources`.

Nothing of the target, the design root or the history of the design is in it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from ..design import Design, Source, SourceType, generator_of
from ..flow import Flow
from ..flow.derived import DesignConversion
from ..frozen import thaw
from ..generation import recipe_identity
from ..utils import plain_json, semantic_hash

__all__ = [
    "configuration_hash",
    "conversion_descriptor",
    "design_configuration",
    "design_configuration_hash",
    "generated_by",
    "node_configuration",
    "source_entries",
]


def conversion_descriptor(producer: str, conversion: DesignConversion) -> dict[str, Any]:
    """Return what a node knows of the conversion that makes its design.

    `producer` is the name of the node of the converter. The types are sorted names, so the
    descriptor is the same whatever the order of their declaration."""
    return {
        "converted_from": producer,
        "transforms": sorted(member.name for member in conversion.transforms),
        "produces": sorted(member.name for member in conversion.produces),
    }


def generated_by(design: Design, source: Source) -> dict[str, Any] | None:
    """Return the recipe of the generator that writes `source` (`generation.recipe_identity`),
    or None for a source that no generator writes.

    The generator is that of `design`, or that of the unit of generation that owns the source
    (`Source.owner`, `Design.generation_units`). The recipe is as written: it holds no content
    and no digest, so two designs whose generators differ only in their arguments read two
    configurations, and an edit of a generator's script is evidence of freshness only."""
    if not source.generated:
        return None
    if source.owner is None:
        unit = design
    else:
        units = {unit.generation_label: unit for unit in design.generation_units}
        if source.owner not in units:
            raise ValueError(f"no unit of generation of {design.name} owns {source.path}")
        unit = units[source.owner]
    generator = generator_of(unit.rtl, root=unit.root)
    return None if generator is None else recipe_identity(generator)


def source_entries(
    design: Design,
    sources: tuple[Source, ...],
    scope: Collection[SourceType] | None,
    *,
    ownership: bool = False,
    recipes: bool = False,
) -> list[dict[str, Any]]:
    """Return the `{path, type}` of each source in `scope` (None: all), in design order.

    The path is the place from the design root (`Design.source_place`), and the entry holds no
    content. A file of a Git dependency is `{dependency, path, type}`: the index of the
    dependency, and the path in its checkout. The commit is no part of it. A generated source is
    its declaration, as the design states it: a name or a pattern in the output of the generator
    (`gen/*.v`), never the files that it matches, so the entry is the same before and after the
    generator has run. With `ownership`, an entry of a generated source also says
    `generated: true`, so that a written file and a generated one of one name
    differ."""
    entries: list[dict[str, Any]] = []
    for source in sources:
        if scope is not None and source.type not in scope:
            continue
        entry: dict[str, Any] = {
            "path": design.source_place(source),
            "type": SourceType(source.type).name,
        }
        if source.dependency is not None:
            entry["dependency"] = source.dependency
        if ownership:
            entry["generated"] = source.generated
        if recipes and (recipe := generated_by(design, source)) is not None:
            entry["generator"] = recipe
        entries.append(entry)
    return entries


def design_configuration(design: Design) -> dict[str, Any]:
    """Return the whole configuration of a design, without content: the key of a generation.

    It has `rtl` and `tb` (each with its metadata and its ordered `sources`, whose entries say
    who writes them: the design's author or its generator) and `language`. It is not the
    projection of one node (`node_configuration`): a generator may use anything of the design, so
    its storage is named by all of it. Nothing of the target, the design root, the destinations of
    deliveries or the history of the design is in it. The helpers are those of the node
    configuration."""
    configuration: dict[str, Any] = {}
    for part in ("rtl", "tb"):
        entry = design.part_metadata(part)
        entry["sources"] = source_entries(
            design, getattr(design, part).sources, None, ownership=True
        )
        configuration[part] = entry
    configuration["language"] = thaw(design.language)
    return plain_json(configuration)


def node_configuration(
    design: Design,
    flow_class: type[Flow],
    settings: Flow.Settings,
    *,
    conversion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the design configuration of a node of `flow_class` with `settings`.

    `design` is the design that the node is planned to read. `conversion` is None when the node
    reads the design's sources, or has no design input. Otherwise it describes the converter
    that makes the design (`conversion_descriptor`), and the node gets the planned design's
    metadata and that description, and never a list of files: the converter has not run yet.

    The result is plain data (`utils.plain_json`)."""
    configuration: dict[str, Any] = {}
    reads = flow_class.reads_sources is not None and conversion is None
    scope = flow_class.design_scope(settings)
    for part in ("rtl", "tb"):
        if part not in flow_class.design_parts:
            continue
        entry = design.part_metadata(part)
        if reads:
            entry["sources"] = source_entries(
                design, getattr(design, part).sources, scope, recipes=True
            )
        configuration[part] = entry
    if "rtl" in flow_class.design_parts:
        configuration["language"] = thaw(design.language)
    if conversion is not None:
        configuration.update(conversion)
    return plain_json(configuration)


def configuration_hash(configuration: Mapping[str, Any]) -> str:
    """Return the hash of a configuration, which `bindings.node_identity` takes."""
    return semantic_hash(thaw(configuration))


def design_configuration_hash(design: Design) -> str:
    """Return the hash of the whole configuration of a design (`design_configuration`).

    A plan holds it to know the design it was made for. It reads no file, so it exists for a
    design whose generated sources are not generated yet."""
    return semantic_hash(design_configuration(design))
