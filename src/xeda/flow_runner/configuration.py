"""The design configuration of a node: what the node reads of its design, without content.

The configuration is a part of the identity of a node (`bindings.node_identity`), so two
designs that differ in what a flow reads (its top, a parameter, the places and types of its
sources) are two nodes with two run directories. The content of a source is no part of it: it
is evidence of freshness (`Design.parts_hash`, the trace).

This module is the one rule. The resolver freezes the configuration of every node in its plan,
the launcher recomputes it from the same view of the design, and the record of a run
(`records.record_of`) holds the one of its node.

The shape is that of a part of `Design.to_dict()`:

- for each part of `Flow.design_parts` (`rtl`, `tb`): its metadata (`Design.part_metadata`) and,
  for a node that reads the design's sources, its `sources`, a list of `{path, type}`;
- `language`, when the node reads `rtl`;
- for a design that a converter derives, the conversion (`converted_from`, `transforms`,
  `produces`) and no `sources`.

Nothing of the target, the design root or the history of the design is in it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from ..design import Design, Source, SourceType
from ..flow import Flow
from ..flow.derived import DesignConversion
from ..frozen import thaw
from ..utils import plain_json, semantic_hash

__all__ = ["configuration_hash", "conversion_descriptor", "node_configuration", "source_entries"]


def conversion_descriptor(producer: str, conversion: DesignConversion) -> dict[str, Any]:
    """Return what a node knows of the conversion that makes its design.

    `producer` is the name of the node of the converter. The types are sorted names, so the
    descriptor is the same whatever the order of their declaration."""
    return {
        "converted_from": producer,
        "transforms": sorted(member.name for member in conversion.transforms),
        "produces": sorted(member.name for member in conversion.produces),
    }


def source_entries(
    design: Design, sources: tuple[Source, ...], scope: Collection[SourceType] | None
) -> list[dict[str, str]]:
    """Return the `{path, type}` of each source in `scope` (None: all), in design order.

    The path is the place from the design root (`Design.source_place`), and the entry holds no
    content. A design generator runs when the design is loaded, so the files it wrote are known
    when a plan is made: the configuration lists them by their place under the design root, as
    it lists any other source."""
    return [
        {"path": design.source_place(source), "type": source.type.name}
        for source in sources
        if scope is None or source.type in scope
    ]


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
            entry["sources"] = source_entries(design, getattr(design, part).sources, scope)
        configuration[part] = entry
    if "rtl" in flow_class.design_parts:
        configuration["language"] = thaw(design.language)
    if conversion is not None:
        configuration.update(conversion)
    return plain_json(configuration)


def configuration_hash(configuration: Mapping[str, Any]) -> str:
    """Return the hash of a configuration, which `bindings.node_identity` takes."""
    return semantic_hash(thaw(configuration))
