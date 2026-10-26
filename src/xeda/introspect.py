"""Machine-readable introspection of Xeda's flows, settings, results and design schema.

The CLI output of `--json` and `--format`, the generated documentation and the reference files
of the agent skill all read from this module. Every function returns plain JSON-serializable
data and never touches the console, so other code can use it too:

    >>> from xeda.introspect import flow_info, settings_info
    >>> flow_info("vivado_synth")["category"]
    'fpga_synthesis'
"""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
import logging
import os
from collections.abc import Mapping
from enum import Enum, IntEnum
from pathlib import Path
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Type,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, PydanticSchemaGenerationError
from pydantic.errors import PydanticInvalidForJsonSchema

from .dataclass import (
    PydanticUndefined,
    field_annotation,
    field_shorthands_of,
    input_names,
    opaque_key_fields_of,
    single_item_fields_of,
    tier_of,
    written_role,
    XedaBaseModel,
)
from .design import (
    DESIGN_NAME,
    FLAT_RTL_KEYS,
    TARGET_FORBIDDEN_KEYS,
    Design,
    names_a_design_file,
)
from .flow import AsicSynthFlow, Flow, FpgaSynthFlow, SimFlow, SynthFlow, registered_flows
from .flow.io import declared_inputs, declared_outputs, selected_types
from .flow_runner import get_flow_class
from .flow_runner.chains import ChainEdge, followers, predecessors
from .flows import __builtin_flows__
from .units import QUANTITY_KEY
from .utils import json_encodable, unique, with_json_keys

if TYPE_CHECKING:
    from .flow_runner.resolver import Plan

log = logging.getLogger(__name__)

__all__ = [
    "all_flow_classes",
    "boards_info",
    "design_info",
    "design_schema",
    "flow_info",
    "flows_info",
    "json_safe",
    "optimizers_info",
    "plan_info",
    "platforms_info",
    "provider_status",
    "results_info",
    "settings_info",
    "settings_completion_info",
]


def json_safe(value: Any) -> Any:
    """Return `value` as plain JSON data, exactly as Xeda writes it to a file.

    The value is encoded by `json` with `utils.json_encodable`, the encoder behind every JSON
    file Xeda writes, and read back. A document that the CLI prints therefore agrees with
    `settings.json` or the `fmax_<design>_<flow>_<timestamp>.json` file of a search on the same
    value.

    The round trip replaces a walk over the value, because a walk re-derives the rules of `json`
    and gets some wrong: `json` writes the key `True` as `"true"`, `None` as `"null"` and a `str`
    enum member as its text, where `str(key)` would give `"True"`, `"None"` and `"E.A"`. The
    result holds only plain `dict`, `list`, `str`, number, `bool` and `None` data, which
    `--format yaml` also renders. Keys go through `utils.with_json_keys` first, as they do for a
    file, so a key such as a `Path` or a tuple never turns a document into an error."""
    return json.loads(json.dumps(with_json_keys(value), default=json_encodable))


def design_info(requested: Any, loaded: str | None = None) -> dict[str, Any]:
    """Return the `design` and `design_file` keys of a `run` or `dse` document.

    `requested` is the design that the command line gave: a design file (a `Path`, or text with
    a design file suffix, which the launcher reads as a design file) or the name of a design in
    a project. It is None when the command line gave none. `loaded` is the name of the design
    that the launcher loaded, if it got that far.

    `design` is always the name of the design: the loaded design's name, else the name that the
    request gave. A design file that was not read has no name to report, so `design` is None.
    `design_file` is the absolute path of the design file that the request named. It is None when
    the request named none (a design entry of a project, or no design). A failure to read the
    file still says which file it was.
    """
    file = None
    name = loaded
    if requested is not None:
        if isinstance(requested, Path) or names_a_design_file(requested):
            file = os.path.abspath(requested)
        elif name is None:
            name = str(requested)
    return {"design": name, "design_file": file}


def request_info(request: Any) -> list[dict[str, Any]]:
    """Return the elements of a request with canonical names: one for a single flow, several for
    a chain. `output` is the output that a chain element selects for its edge to the next
    element, else None. `through` lists the names of the flows between the element and the next
    one, in chain order. It is empty for a direct link and for the last element."""
    return [
        {
            "node": element.node,
            "flow": element.flow_class.name,
            "output": element.output,
            "through": (
                [flow.name for flow in request.through(index)] if index < len(request.links) else []
            ),
        }
        for index, element in enumerate(request.elements)
    ]


def inputs_info(node: Any) -> list[dict[str, Any]]:
    """Return the resolved inputs of a plan node, as `plan_info` and the nodes of `xeda run
    --json` give them."""
    return [
        {
            "name": resolved.name,
            "origin": resolved.origin,
            "producer": resolved.producer,
            "output": resolved.output,
            "sources": [str(path) for path in resolved.sources],
            "references": [{"node": ref.node, "output": ref.output} for ref in resolved.references],
            "binding_origin": resolved.binding_origin,
            "binding_location": resolved.binding_location,
            "overridden": list(resolved.overridden),
            "reason": resolved.reason,
        }
        for resolved in node.inputs
    ]


def plan_info(plan: Plan, freshness: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a resolved plan as plain data: the selected target of the design (or None) and the
    nodes, producers first, each with its run directory, input origins and the outputs switched
    on because a consumer reads them.

    With `freshness` (from `DefaultRunner.plan_with_freshness`), each node also has `state`
    (`fresh`, `stale` or `unknown`) and `reason`, which say what a launch would do and why. The
    reason of a stale node is the text that the launch logs. The reason of an unknown node says
    why nothing can be said. The reason of a fresh node is empty.
    """
    return json_safe(
        {
            "requested": plan.requested,
            "target": plan.context.target,
            "nodes": [
                {
                    "name": node.name,
                    "flow": node.flow_class.name,
                    "run_path": str(node.run_path),
                    "flowrun_hash": node.flowrun_hash,
                    "settings_hash": node.settings_hash,
                    "inputs": inputs_info(node),
                    "switched_on": list(node.switched_on),
                    **(
                        {
                            "state": freshness[node.name].state,
                            "reason": freshness[node.name].reason,
                        }
                        if freshness is not None
                        else {}
                    ),
                    # Static class metadata. `always_runs()` is never called here.
                    "action_reason": node.flow_class.action_reason,
                    # The types of files: the design kind has none.
                    "input_types": {
                        name: [t.name for t in selected_types(node.flow_class, node.settings, name)]
                        for name, declaration in declared_inputs(node.flow_class).items()
                        if not declaration.is_design
                    },
                    "output_types": {
                        name: [
                            t.name
                            for t in selected_types(
                                node.flow_class, node.settings, name, output=True
                            )
                        ]
                        for name, declaration in declared_outputs(node.flow_class).items()
                        if not declaration.is_design
                    },
                }
                for node in plan.nodes
            ],
        }
    )


def all_flow_classes() -> List[Type[Flow]]:
    """Return every registered flow class, ordered by canonical name."""
    classes = {cls for cls in __builtin_flows__} | {cls for _mod, cls in registered_flows.values()}
    return sorted(classes, key=lambda cls: cls.name)


def flow_executables() -> List[str]:
    """Return the programs of every registered flow (`Flow.executables`), without repeats, in
    the order of the flows."""
    return unique([name for cls in all_flow_classes() for name in cls.executables])


def _resolve(flow: Union[str, Type[Flow]]) -> Type[Flow]:
    if isinstance(flow, str):
        return get_flow_class(flow)
    return flow


def _own_docstring(cls: type) -> Optional[str]:
    """Return the docstring of the class itself, never one inherited from a base class.

    `inspect.getdoc` walks the MRO, so `list-flows` would describe a flow without a docstring as
    "Superclass of all FPGA synthesis flows".
    """
    doc = cls.__dict__.get("__doc__")
    return inspect.cleandoc(doc) if isinstance(doc, str) and doc.strip() else None


def _category(cls: Type[Flow]) -> str:
    if issubclass(cls, SimFlow):
        return "simulation"
    if issubclass(cls, FpgaSynthFlow):
        return "fpga_synthesis"
    if issubclass(cls, AsicSynthFlow):
        return "asic_synthesis"
    if issubclass(cls, SynthFlow):
        return "synthesis"
    return "other"


def _edge_info(edge: ChainEdge, other: type[Flow]) -> dict[str, Any]:
    """Return one follow relation as plain data, seen from the other end. `other` is the flow
    that the relation names. `output` is the producer output that a request must name, and None
    when the unqualified request is valid. `through` lists the flows that the link passes
    through, in chain order, and is empty for a direct link. `binds` lists the input and output
    pairs that the link binds, and each item names the flow whose input it binds (`flow`): the
    consumer of a direct link. `target_dependent` tells whether a type that the relation makes
    or takes is a union that the target settings narrow.

    Every relation has these keys, in this order."""
    return {
        "flow": other.name,
        "output": edge.output,
        "through": [flow.name for flow in edge.through],
        "binds": [
            {"flow": flow.name, "input": taken, "output": made}
            for flow, (taken, made) in zip(edge.bound_flows, edge.binds)
        ],
        "target_dependent": edge.target_dependent,
    }


def flow_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Return everything known about a flow without instantiating it.

    `dependencies` lists the default producers of the declared inputs. `can_precede` lists the
    flows that can follow this flow in a chain (`this+other`), and `can_follow` lists the flows
    that this flow can follow (`other+this`). The direct relations come first. A relation whose
    link passes through the flows between the two lists them in `through`, which is empty for a
    direct relation. The chain validator judges both on declarations alone (`chains.followers`
    and `chains.predecessors`), so a hint and a refusal cannot disagree. A relation is
    `target_dependent` when a union of types decides it and the target settings narrow that
    union. The resolved plan is authoritative.
    """
    cls = _resolve(flow)
    candidates = all_flow_classes()
    return {
        "name": cls.name,
        "aliases": list(cls.aliases),
        "class": cls.__name__,
        "module": cls.__module__,
        "qualified_name": f"{cls.__module__}.{cls.__name__}",
        "description": _own_docstring(cls),
        "category": _category(cls),
        "supports_cocotb": bool(getattr(cls, "cocotb_sim_name", None)),
        "dependencies": unique(
            [d.producer for d in declared_inputs(cls).values() if d.producer is not None]
        ),
        "action_reason": cls.action_reason,
        "inputs": [
            {
                "name": d.name,
                "value": d.value,
                "types": [t.name for t in d.types],
                "cardinality": d.cardinality,
                "required": d.required,
                "optional": d.optional,
                "equivalence": d.equivalence,
                "producer": d.producer,
                "output": d.output,
                "same_producer_as": d.same_producer_as,
                "paired": d.paired,
                "via": d.via,
                "description": d.description,
            }
            for d in declared_inputs(cls).values()
        ],
        "outputs": [
            {
                "name": d.name,
                "value": d.value,
                "types": [t.name for t in d.types],
                "cardinality": d.cardinality,
                "enabled_by": d.enabled_by,
                "description": d.description,
            }
            for d in declared_outputs(cls).values()
        ],
        "can_follow": [_edge_info(e, e.producer) for e in predecessors(cls, candidates)],
        "can_precede": [_edge_info(e, e.consumer) for e in followers(cls, candidates)],
        "settings_class": f"{cls.Settings.__module__}.{cls.Settings.__qualname__}",
    }


def flow_chain_cells(info: dict[str, Any]) -> tuple[str, str, str]:
    """Return the three chain columns of the `list-flows` table for one `flow_info` document:
    what the flow takes, what it makes and what can follow it."""

    def kinds(item: dict[str, Any]) -> str:
        return item["value"] if item["value"] == "design" else "/".join(item["types"])

    takes = []
    for item in info["inputs"]:
        name = item["name"] + ("..." if item["cardinality"] == "many" else "")
        name = f"[{name}]" if not item["required"] else name
        related = ""
        if item["same_producer_as"]:
            related = f"; same producer as {item['same_producer_as']}"
            related += f" via {item['via']}" if item["via"] else ""
            related += ", paired" if item["paired"] else ""
        takes.append(f"{name} ({kinds(item)}{related})")
    makes = []
    for item in info["outputs"]:
        name = item["name"] + ("..." if item["cardinality"] == "many" else "")
        name = f"[{name}]" if item["enabled_by"] else name
        makes.append(f"{name} ({kinds(item)})")
    if info["action_reason"]:
        makes.append(f"action: {info['action_reason']} (ends a chain)")
    following = []
    for edge in info["can_precede"]:
        text = (
            edge["flow"]
            if edge["output"] is None
            else f"{info['name']}.{edge['output']}+{edge['flow']}"
        )
        if edge["through"]:
            text += f" (through {', '.join(edge['through'])})"
        if edge["target_dependent"]:
            text += " (depends on the target)"
        following.append(text)
    return "\n".join(takes) or "-", "\n".join(makes) or "-", "\n".join(following) or "-"


def flows_info() -> List[Dict[str, Any]]:
    """Return `flow_info` for every registered flow, ordered by canonical name."""
    return [flow_info(cls) for cls in all_flow_classes()]


#: The JSON Schema dialect that pydantic 2 emits. The published document declares it, so that a
#: validator selects the matching draft. Draft 2020-12 uses the shapes `$defs` and `prefixItems`.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _is_null_branch(spec: Any) -> bool:
    """Return whether `spec` is the `{"type": "null"}` branch that pydantic 2 adds to every
    `Optional[...]` field."""
    return isinstance(spec, dict) and spec.get("type") == "null" and len(spec) == 1


def type_str(field_schema: Any, definitions: Optional[Dict[str, Any]] = None) -> str:
    """Render a JSON Schema fragment as a short type expression that people and agents can read."""
    definitions = definitions if definitions is not None else {}
    if isinstance(field_schema, (list, tuple)):
        return "|".join(type_str(f, definitions) for f in field_schema)
    if not isinstance(field_schema, dict):
        return "any"
    kind = field_schema.get(QUANTITY_KEY)
    if isinstance(kind, str):
        # A quantity is text with a unit in the schema. The listing names what it measures.
        return kind
    ref = field_schema.get("$ref")
    typ = field_schema.get("type")
    if typ is None and ref:
        return ref.split("/")[-1]
    if typ == "object":
        additional = field_schema.get("additionalProperties")
        if additional:
            return f"dict[string, {type_str(additional, definitions)}]"
        return "object"
    if typ == "array":
        # Draft 2020-12 describes a fixed-length tuple with `prefixItems`, one schema per
        # position.
        prefix_items = field_schema.get("prefixItems")
        if prefix_items:
            return "tuple[{}]".format(", ".join(type_str(i, definitions) for i in prefix_items))
        items = field_schema.get("items")
        if items:
            return f"array[{type_str(items, definitions)}]"
        return "array"
    for key, joiner in (("allOf", " & "), ("anyOf", " | "), ("oneOf", " | ")):
        variants = field_schema.get(key)
        if variants:
            # `Optional[X]` is `anyOf: [X, {"type": "null"}]`.
            # Nullability is already carried by `required`/`default`, so rendering it here would
            # turn every optional setting into "X | null" in `xeda list-settings` and the docs.
            non_null = [v for v in variants if not _is_null_branch(v)]
            if not non_null:
                return "null"
            if len(non_null) == 1:
                return type_str(non_null[0], definitions)
            # Remove duplicates and keep the order: several branches can render the same way (a
            # plain string and a `format: path` string both read as "string").
            rendered = list(dict.fromkeys(type_str(v, definitions) for v in non_null))
            return joiner.join(rendered)
    if typ is None:
        return "any"
    return str(typ)


def _default_of(model_field: Any) -> Any:
    """Return the default of a pydantic field as JSON-safe data, or None for a field with no
    default."""
    if model_field.is_required():
        return None
    default = model_field.default
    if default is None or default is PydanticUndefined:
        if model_field.default_factory is not None:
            try:
                return json_safe(model_field.default_factory())
            except TypeError:  # factory that takes the already-validated data
                return None
        return None
    return json_safe(default)


def _enum_of(field_schema: Any, definitions: Dict[str, Any]) -> Optional[List[Any]]:
    """Return choices from a field or its sole non-null schema branch."""
    if not isinstance(field_schema, dict):
        return None
    enum = field_schema.get("enum")
    if isinstance(enum, list):
        return json_safe(enum)
    if "const" in field_schema:
        # pydantic renders a single-value `Literal["rtl"]` as `const` rather than a one-element
        # `enum`. It is still the setting's only valid choice.
        return json_safe([field_schema["const"]])
    ref = field_schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return _enum_of(definitions.get(ref.removeprefix("#/$defs/")), definitions)
    for key in ("allOf", "anyOf", "oneOf"):
        variants = field_schema.get(key)
        if isinstance(variants, list):
            non_null = [variant for variant in variants if not _is_null_branch(variant)]
            if len(non_null) == 1:
                return _enum_of(non_null[0], definitions)
    return None


def _declaring_class(model: type[BaseModel], name: str) -> Optional[str]:
    """Return the qualified name of the class in the MRO of `model` that declares the field
    `name`."""
    return next(
        (
            f"{b.__module__}.{b.__qualname__}"
            for b in reversed(model.__mro__)
            if name in getattr(b, "__annotations__", {})
        ),
        None,
    )


def _group_class(annotation: Any) -> Optional[type]:
    """Return the settings group class that `annotation` is, or None."""
    from .flow.settings_group import SettingsGroup

    if isinstance(annotation, type) and issubclass(annotation, SettingsGroup):
        return annotation
    return None


def _group_entries(
    group_class: type[BaseModel], prefix: str, top: str, common: bool
) -> List[Dict[str, Any]]:
    """Return the entries of the fields of a settings group, depth first in declaration order.

    `prefix` is the dotted name of the group, and `top` the name of the top-level field that
    holds it. `common` is whether that field is accepted by every flow, and so are all the
    fields of the group. A field that is a group has an entry of its own, then the entries of its
    fields."""
    schema = group_class.model_json_schema(by_alias=True)
    properties = schema.get("properties", {})
    definitions = schema.get("$defs", {})
    entries: List[Dict[str, Any]] = []
    for name, info in group_class.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        if extra.get("hidden_from_schema") or name.endswith("_"):
            continue
        prop = properties.get(info.alias or name, properties.get(name, {}))
        alias = f"{prefix}.{info.alias}" if info.alias and info.alias != name else None
        entries.append(
            {
                "name": f"{prefix}.{name}",
                "alias": alias,
                "type": type_str(prop, definitions),
                "required": False,
                "default": _default_of(info),
                "description": info.description or None,
                "enum": _enum_of(prop, definitions),
                "common": common,
                "declared_by": _declaring_class(group_class, name),
                "writes": written_role(group_class, name),
                "opaque_keys": name in opaque_key_fields_of(group_class),
                "json_schema": json_safe(prop),
                "tier": tier_of(group_class, name) or "common",
                "group": top,
            }
        )
        sub_group = _group_class(info.annotation)
        if sub_group is not None:
            entries += _group_entries(sub_group, f"{prefix}.{name}", top, common)
    return entries


def _field_entries(cls: Type[Flow]) -> List[Dict[str, Any]]:
    schema = cls.Settings.model_json_schema(by_alias=True)
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    definitions = schema.get("$defs", {})
    base_field_names = set(Flow.Settings.model_fields)

    entries: List[Dict[str, Any]] = []
    represented_names: set[str] = set()
    for name, model_field in cls.Settings.model_fields.items():
        info = model_field  # the mapping's values are `FieldInfo`s
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        if extra.get("hidden_from_schema") or name.endswith("_"):
            continue
        alias = model_field.alias if model_field.alias and model_field.alias != name else None
        represented_names.add(name)
        represented_names.add(model_field.alias or name)
        prop = properties.get(model_field.alias or name, properties.get(name, {}))
        entries.append(
            {
                "name": name,
                "alias": alias,
                "type": type_str(prop, definitions),
                "required": model_field.is_required()
                or ((model_field.alias or name) in required)
                or (name in required),
                "default": _default_of(model_field),
                "description": info.description or None,
                "enum": _enum_of(prop, definitions),
                "common": name in base_field_names,
                "declared_by": _declaring_class(cls.Settings, name),
                # "working" or "deliverable": a path the flow writes; None: it is read
                "writes": written_role(cls.Settings, name),
                # true: the keys are the tool's names, and a dot in one is part of the name
                "opaque_keys": name in opaque_key_fields_of(cls.Settings),
                "json_schema": json_safe(prop),
                "tier": tier_of(cls.Settings, name) or "common",
                "group": None,
            }
        )
        group_class = _group_class(model_field.annotation)
        if group_class is not None:
            entries += _group_entries(group_class, name, name, name in base_field_names)
    for name, prop in properties.items():
        if name in represented_names or not prop.get("x-xeda-input-only"):
            continue
        entries.append(
            {
                "name": name,
                "alias": None,
                "type": type_str(prop, definitions),
                "required": name in required,
                "default": None,
                "description": prop.get("description"),
                "enum": _enum_of(prop, definitions),
                "common": False,
                "declared_by": f"{cls.Settings.__module__}.{cls.Settings.__qualname__}",
                "writes": None,
                "opaque_keys": False,
                "json_schema": json_safe(prop),
                "tier": "common",
                "group": None,
            }
        )
    return entries


def settings_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Return the settings of a flow, plus the raw JSON Schema.

    `fields` has one entry for each top-level setting, including the settings that every flow
    shares (`ncpus`, `dockerized`, ...). After a settings group, it has one entry for each field
    of the group, depth first in declaration order. Its `name` is the dotted path that `-s` and a
    design file accept, and its `group` is the top-level setting that holds the group.

    `tier` is how often a user needs the setting (`common`, `advanced` or `expert`). A setting
    that declares none is `common`. `group` is None outside a group. `definitions` describes the
    nested models.
    """
    cls = _resolve(flow)
    schema = cls.Settings.model_json_schema(by_alias=True)
    return {
        "flow": cls.name,
        "settings_class": f"{cls.Settings.__module__}.{cls.Settings.__qualname__}",
        "fields": _field_entries(cls),
        "definitions": json_safe(schema.get("$defs", {})),
        "json_schema": json_safe(schema),
    }


def settings_completion_info(flow: Union[str, Type[Flow]]) -> dict[str, dict[str, Any]]:
    """Return accepted settings paths and finite values for shell completion.

    The walk follows settings models and their validation schemas. It does not construct model
    instances or invoke validators. A mapping whose keys come from the user is marked
    ``dynamic``. Typed maps include ``*`` templates for their values.
    """
    cls = _resolve(flow)
    entries: dict[str, dict[str, Any]] = {}

    def ensure(path: str, *, children: bool = False, assignable: bool = False) -> dict[str, Any]:
        record = entries.setdefault(path, {"children": False, "assignable": False})
        record["children"] = record["children"] or children
        record["assignable"] = record["assignable"] or assignable
        return record

    def union_members(annotation: Any) -> list[Any]:
        while get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        origin = get_origin(annotation)
        if origin in (Union, UnionType):
            return [member for member in get_args(annotation) if member is not type(None)]
        return [annotation]

    def is_model(annotation: Any) -> bool:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)

    def is_mapping(annotation: Any) -> bool:
        origin = get_origin(annotation)
        return origin in (dict, Mapping) or annotation in (dict, Mapping)

    def mapping_values(annotation: Any) -> list[Any]:
        values = []
        for member in union_members(annotation):
            if is_mapping(member):
                args = get_args(member)
                values.append(args[1] if len(args) == 2 else Any)
        return values

    def finite_values(annotation: Any) -> list[str] | None:
        values: list[str] = []
        for member in union_members(annotation):
            origin = get_origin(member)
            if origin is Literal:
                literal_choices = get_args(member)
                if not all(isinstance(choice, str) for choice in literal_choices):
                    return None
                values.extend(literal_choices)
            elif member is bool:
                values.extend(("false", "true"))
            elif isinstance(member, type) and issubclass(member, IntEnum):
                values.extend(str(choice.value) for choice in member)
            elif isinstance(member, type) and issubclass(member, Enum):
                enum_choices = [choice.value for choice in member]
                if not all(isinstance(choice, str) for choice in enum_choices):
                    return None
                values.extend(enum_choices)
            else:
                return None
        return sorted(set(values)) if values else None

    scalar_schema_types = {"string", "integer", "number", "boolean"}

    def schema_variants(
        schema: Any, definitions: Mapping[str, Any], active_refs: frozenset[str]
    ) -> list[tuple[dict[str, Any], frozenset[str]]]:
        """Return this schema and its local union/ref branches, stopping recursive refs."""
        if not isinstance(schema, dict):
            return []
        branches: list[tuple[dict[str, Any], frozenset[str]]] = []
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in active_refs:
            key = ref.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
            target = definitions.get(key)
            if isinstance(target, dict):
                branches.extend(schema_variants(target, definitions, active_refs | {ref}))
        branches.append((schema, active_refs))
        for key in ("anyOf", "oneOf", "allOf"):
            for variant in schema.get(key, ()):
                branches.extend(schema_variants(variant, definitions, active_refs))
        return branches

    def schema_allows_scalar(schema: Any, definitions: Mapping[str, Any]) -> bool:
        return any(
            bool(
                scalar_schema_types.intersection(
                    variant.get("type", ())
                    if isinstance(variant.get("type"), list)
                    else [variant.get("type")]
                )
            )
            for variant, _ in schema_variants(schema, definitions, frozenset())
        )

    def schema_values(schema: Any, definitions: Mapping[str, Any]) -> list[str] | None:
        values: list[str] = []
        for variant, _ in schema_variants(schema, definitions, frozenset()):
            choices = variant.get("enum")
            if choices is None and "const" in variant:
                choices = [variant["const"]]
            if choices is not None:
                if not all(isinstance(choice, str) for choice in choices):
                    return None
                values.extend(choices)
        return sorted(set(values)) if values else None

    def walk_schema(
        schema: Any,
        prefix: str,
        definitions: Mapping[str, Any],
        active_refs: frozenset[str] = frozenset(),
    ) -> None:
        variants = schema_variants(schema, definitions, active_refs)
        if not variants:
            return
        properties: dict[str, list[tuple[dict[str, Any], frozenset[str]]]] = {}
        descriptions: list[str] = []
        for variant, refs in variants:
            if variant.get("description") and variant["description"] not in descriptions:
                descriptions.append(variant["description"])
            for name, prop in variant.get("properties", {}).items():
                if name.endswith("_") or prop.get("hidden_from_schema") or prop.get("readOnly"):
                    continue
                properties.setdefault(name, []).extend(schema_variants(prop, definitions, refs))
        record = ensure(
            prefix, children=bool(properties), assignable=schema_allows_scalar(schema, definitions)
        )
        values = schema_values(schema, definitions)
        if values is not None:
            record["values"] = sorted(set(record.get("values", ())) | set(values))
        if descriptions:
            record.setdefault("help", descriptions[0])
        for name, prop_variants in properties.items():
            path = f"{prefix}.{name}"
            for variant, refs in prop_variants:
                # Each schema branch is walked so accepted union properties are merged.
                walk_schema(variant, path, definitions, refs)

    def walk_value(
        annotation: Any,
        prefix: str,
        ancestors: frozenset[type],
        definitions: Mapping[str, Any],
        field_schema: Any = None,
        field_shorthand: bool = False,
    ) -> None:
        members = union_members(annotation)
        models = [member for member in members if is_model(member)]
        maps = mapping_values(annotation)
        schema_scalar = schema_allows_scalar(field_schema, definitions) if field_schema else False
        annotation_scalar = any(
            not is_model(member) and not is_mapping(member) for member in members
        )
        # Lists and tuples are accepted from CLI text by settings normalization. A JSON schema
        # reports them as arrays, so keep annotation-based leaf assignment alongside explicit
        # scalar validation-schema inputs.
        scalar_branch = schema_scalar or annotation_scalar
        base_as_mapping = getattr(XedaBaseModel.as_mapping, "__func__", XedaBaseModel.as_mapping)

        def has_model_shorthand(model: type[BaseModel]) -> bool:
            mapper = getattr(model, "as_mapping", None)
            return mapper is not None and getattr(mapper, "__func__", mapper) is not base_as_mapping

        shorthand = any(has_model_shorthand(model) for model in models) or field_shorthand
        has_children = bool(models or maps)
        record = ensure(prefix, children=has_children, assignable=scalar_branch or shorthand)
        values = finite_values(annotation)
        if values is not None:
            record["values"] = values
        if has_children:
            for model in models:
                walk_model(model, prefix, ancestors)
            for value_annotation in maps:
                record["dynamic"] = True
                # Arbitrary scalar values stop at the dynamic key boundary. Model values,
                # nested maps, and finite scalar values have useful templates.
                value_members = union_members(value_annotation)
                if any(member is Any or member is object for member in value_members):
                    continue
                if (
                    not any(is_model(member) or is_mapping(member) for member in value_members)
                    and finite_values(value_annotation) is None
                ):
                    continue
                walk_value(value_annotation, f"{prefix}.*", ancestors, definitions)

    def walk_model(model: type[BaseModel], prefix: str, ancestors: frozenset[type]) -> None:
        if model in ancestors:
            return
        branch = ancestors | {model}
        try:
            schema = model.model_json_schema(by_alias=True, mode="validation")
        except (PydanticInvalidForJsonSchema, PydanticSchemaGenerationError):
            schema = {}
        properties = schema.get("properties", {})
        definitions = schema.get("$defs", {})
        names = input_names(model)
        spellings_by_field: dict[str, list[str]] = {}
        for spelling, field_name in names.items():
            spellings_by_field.setdefault(field_name, []).append(spelling)
        shorthands = field_shorthands_of(model)
        opaque_fields = set(opaque_key_fields_of(model))
        for field_name, field in model.model_fields.items():
            extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
            if extra.get("hidden_from_schema") or field_name.endswith("_"):
                continue
            spellings = sorted(set(spellings_by_field.get(field_name, [field_name])))
            annotation = field_annotation(model, field_name)
            if annotation is None:
                annotation = field.annotation
            field_shorthand = field_name in shorthands
            field_schema = next(
                (properties[spelling] for spelling in spellings if spelling in properties),
                properties.get(field.alias or field_name, properties.get(field_name)),
            )
            for spelling in spellings:
                path = f"{prefix}.{spelling}" if prefix else spelling
                if field_name in opaque_fields:
                    record = ensure(
                        path,
                        children=True,
                        assignable=(
                            schema_allows_scalar(field_schema, definitions)
                            if field_schema is not None
                            else field_shorthand
                        )
                        or field_shorthand,
                    )
                    record["dynamic"] = True
                    record["opaque_keys"] = True
                    if field.description:
                        record.setdefault("help", field.description)
                    continue
                walk_value(
                    annotation,
                    path,
                    branch,
                    definitions,
                    field_schema=field_schema,
                    field_shorthand=field_shorthand,
                )
                if field.description:
                    entries[path].setdefault("help", field.description)

        # Validation schemas also declare accepted aliases and input-only spellings that have no
        # model field. Walk those properties generically, including inline child properties and
        # local definitions. Skip schema-only properties that are already ordinary inputs.
        for name, property_schema in properties.items():
            if (
                name in names
                or property_schema.get("hidden_from_schema")
                or property_schema.get("readOnly")
            ):
                continue
            path = f"{prefix}.{name}" if prefix else name
            walk_schema(property_schema, path, definitions)

    walk_model(cls.Settings, "", frozenset())
    return entries


def results_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Return the metrics that a flow can write to `results.json`.

    Each metric is `{path, description, type, unit}`. The other fields of the record (`outcome`,
    `runtime`, ...) are no metrics and are not listed. `artifacts` is empty until a flow
    describes the files it keeps.
    """
    cls = _resolve(flow)
    metrics = [
        {
            "path": path,
            "description": spec.description,
            "type": spec.type,
            "unit": spec.unit,
        }
        for path, spec in cls.results_description.items()
    ]
    return {
        "flow": cls.name,
        "metrics": metrics,
        "artifacts": [],
        "note": "Some metrics are only present when the relevant setting is enabled or the tool "
        "reports them. The authoritative record of a run is always the results.json in its run "
        "directory.",
    }


#: A list of `{name, value}` objects, which stands for the mapping they give. It is an accepted
#: input form of `parameters` and `generics`.
_PARAMETER_LIST = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "value": {"not": {"type": "null"}},  # `0` and `""` are values; null is none
        },
        "required": ["name", "value"],
    },
}
#: The input shapes that the validators of a field accept but the model schema does not describe.
#: The first key is the definition name ("" for the root Design model), the second is the property
#: name. A field with an alias lists both names, because the loader accepts either.
_EXTRA_INPUT_FORMS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    # `Design.authors` accepts a single "Name <email>" string
    "": {
        "author": [{"type": "string"}],
        "authors": [{"type": "string"}],
        "parameters": [_PARAMETER_LIST],
        "generics": [_PARAMETER_LIST],
    },
    "RtlSettings": {"parameters": [_PARAMETER_LIST], "generics": [_PARAMETER_LIST]},
    # `tb.top` accepts a bare module name, and `tb.cocotb` accepts `true`
    "TbSettings": {
        "top": [{"type": "string"}],
        "cocotb": [{"type": "boolean"}],
        "parameters": [_PARAMETER_LIST],
        "generics": [_PARAMETER_LIST],
    },
    # `language.vhdl: "2008"` and `language.vhdl: 2008` are both accepted
    "Language": {
        "vhdl": [{"type": "string"}, {"type": "integer"}],
        "verilog": [{"type": "string"}, {"type": "integer"}],
    },
    "LanguageSettings": {"version": [{"type": "integer"}], "standard": [{"type": "integer"}]},
    "VhdlSettings": {"version": [{"type": "integer"}], "standard": [{"type": "integer"}]},
    # `generator.args` is one list, and a single string is split on whitespace
    "Generator": {"args": [{"type": "string"}]},
}


def _add_flat_form(schema: Dict[str, Any]) -> None:
    """Describe the flat top-level form: `sources` at the root in place of `rtl.sources`, for
    example."""
    definitions = schema.get("$defs", {})
    rtl_properties = definitions.get("RtlSettings", {}).get("properties", {})
    root = schema.setdefault("properties", {})
    for name in FLAT_RTL_KEYS:
        if name in rtl_properties and name not in root:
            root[name] = {
                **rtl_properties[name],
                "description": f"Top-level shorthand for `rtl.{name}`.",
            }
    if "tb" in root:
        tb_schema = root["tb"]
        root.setdefault("test", {**tb_schema, "description": "Top-level shorthand for `tb`."})
        root.setdefault(
            "tests",
            {
                "type": "array",
                "items": tb_schema,
                "description": "Testbenches; only the first is used. Shorthand for `tb`.",
            },
        )
    # `rtl` is synthesized from the flat form when absent, and `name` defaults to the design
    # file's stem, so neither is required in a file.
    required = schema.get("required")
    if required:
        remaining = [name for name in required if name not in ("rtl", "name")]
        if remaining:
            schema["required"] = remaining
        else:
            schema.pop("required", None)


def _add_targets(schema: dict[str, Any]) -> None:
    """Describe `targets`. Each target is an overlay that takes the design's own keys, in every
    form the root takes them, except the keys that belong to the design alone
    (`TARGET_FORBIDDEN_KEYS`). This is input syntax only: the loader applies the selected target
    (`Design.select_target`) and records its name as `target`, so a design file does not write
    `target`."""
    root = schema.setdefault("properties", {})
    root.pop("target", None)
    definitions = schema.setdefault("$defs", {})
    # In an overlay `rtl` and `tb` are partial: the design supplies what a target leaves out.
    partial = {}
    for name in ("RtlSettings", "TbSettings"):
        if name in definitions:
            partial[f"#/$defs/{name}"] = f"#/$defs/{name}Overlay"
            definitions[f"{name}Overlay"] = {
                k: v for k, v in deepcopy(definitions[name]).items() if k != "required"
            }

    # `clock` names the design's first clock and changes it key by key, so a
    # target's `clock` may leave out the port; `clocks` replaces the list, so its clocks are whole.
    if "Clock" in definitions:
        definitions["ClockOverlay"] = {
            k: v for k, v in deepcopy(definitions["Clock"]).items() if k != "required"
        }

    def overlaid(node: Any, refs: Mapping[str, str] = partial) -> Any:
        if isinstance(node, dict):
            return {
                k: refs.get(v, v) if k == "$ref" and isinstance(v, str) else overlaid(v, refs)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [overlaid(item, refs) for item in node]
        return node

    clock_refs = {**partial, "#/$defs/Clock": "#/$defs/ClockOverlay"}
    rtl_overlay = definitions.get("RtlSettingsOverlay", {}).get("properties", {})
    if "clock" in rtl_overlay:
        rtl_overlay["clock"] = overlaid(rtl_overlay["clock"], clock_refs)
    properties: dict[str, Any] = {
        name: overlaid(spec, clock_refs if name == "clock" else partial)
        for name, spec in root.items()
        if name not in TARGET_FORBIDDEN_KEYS
    }
    properties.update({name: False for name in sorted(TARGET_FORBIDDEN_KEYS)})
    root["targets"] = {
        "type": "object",
        "description": "Named targets (for example, one per board), each an overlay on the "
        "design: it takes the design's own keys, merged over them. Mappings merge key by key; "
        "`sources` are appended after the design's; any other list replaces the design's. One "
        "is selected with `--target NAME`; without it, the design is used with no target. "
        "A target name cannot be the name of a flow.",
        "propertyNames": {"pattern": f"^{DESIGN_NAME.pattern}$"},
        "additionalProperties": {"type": "object", "properties": properties},
    }


def _open_property_sets(node: Any) -> None:
    """Drop `additionalProperties: false` throughout the input schema.

    The models set `extra = forbid`, so pydantic emits a closed property set. A design file is
    not closed in that sense: it may use dotted-key shorthand (`clock.port`, `hdl.vhdl.standard`,
    `fpga.part`) that `Design.from_file` expands before the model sees it. The closed set would
    reject files that load without error. The loader still rejects unknown keys once it has
    expanded the file.
    """
    if isinstance(node, dict):
        if node.get("title") == "DesignSource":
            return  # a source table is closed: no dotted-key shorthand applies inside it
        if node.get("additionalProperties") is False:
            node.pop("additionalProperties")
        for value in node.values():
            _open_property_sets(value)
    elif isinstance(node, list):
        for item in node:
            _open_property_sets(item)


def _widen(spec: Dict[str, Any], extra: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add accepted input shapes to one property schema, keeping its documentation."""
    documentation: Dict[str, Any] = {
        k: spec[k] for k in ("title", "description", "default") if k in spec
    }
    branches = spec.get("anyOf")
    if branches is None:
        rest = {k: v for k, v in spec.items() if k not in documentation}
        branches = [rest] if rest else []
    # Copy the forms: a returned schema must not share the module's tables of extra forms.
    return {**documentation, "anyOf": [*branches, *deepcopy(extra)]}


def _accept_field_names(by_alias: Dict[str, Any], by_name: Dict[str, Any]) -> None:
    """Accept a field's own name wherever the schema names its alias.

    Models set `populate_by_name`, so `language` and `hdl` (its alias) are equally valid in a
    design file. A schema generated with aliases alone would reject the field names.
    """

    def merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
        dst_props, src_props = dst.get("properties"), src.get("properties")
        if not dst_props or not src_props:
            return
        for name, spec in src_props.items():
            dst_props.setdefault(name, spec)
        dst_required, src_required = dst.get("required"), src.get("required")
        if dst_required and src_required and set(dst_required) != set(src_required):
            # A required field named differently in the two schemas may appear under either name.
            dst.setdefault("allOf", []).append(
                {"anyOf": [{"required": sorted(dst_required)}, {"required": sorted(src_required)}]}
            )
            dst.pop("required", None)

    merge(by_alias, by_name)
    definitions_by_name = by_name.get("$defs", {})
    for name, spec in by_alias.get("$defs", {}).items():
        counterpart = definitions_by_name.get(name)
        if counterpart:
            merge(spec, counterpart)


def _models_reachable_from(model: type[BaseModel]) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []

    def visit(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in found:
                found.append(annotation)
                for info in annotation.model_fields.values():
                    visit(info.annotation)
        for arg in get_args(annotation):
            visit(arg)

    visit(model)
    return found


#: The forms of an item of `clocks` that are not a whole clock but that the loader takes
#: (`clock_tables`): a port name, and `null` or an empty table, which both mean no clock.
_CLOCK_ITEM_FORMS: List[Dict[str, Any]] = [
    {"type": "string"},
    {"type": "null"},
    {"type": "object", "maxProperties": 0},
]


def _accept_clock_forms(schema: Dict[str, Any]) -> None:
    """Accept every form of a clock in the items of `clocks`, as `RtlSettings.clock_tables`
    reads them."""
    properties = schema.get("$defs", {}).get("RtlSettings", {}).get("properties", {})
    clocks = properties.get("clocks")
    if clocks and "items" in clocks:
        clocks["items"] = _widen(clocks["items"], _CLOCK_ITEM_FORMS)
    clock = properties.get("clock")  # one clock: the same forms of an item
    if clock and "anyOf" in clock:
        clock["anyOf"] = [*clock["anyOf"], deepcopy(_CLOCK_ITEM_FORMS[-1])]


def _accept_single_items(schema: Dict[str, Any]) -> None:
    """Accept one item where a list field takes a list (`single_item_fields`): the loader reads
    it as a list of one. The item's schema is the list's own, so the two cannot differ."""
    for model in _models_reachable_from(Design):
        target = schema if model is Design else schema.get("$defs", {}).get(model.__name__)
        properties = (target or {}).get("properties", {})
        for name in single_item_fields_of(model):
            spec = properties.get(name)
            if spec is not None and spec.get("type") == "array" and "items" in spec:
                properties[name] = _widen(spec, [spec["items"]])


def _accept_alias_choices(schema: dict[str, Any]) -> None:
    """Accept every name of a field with several (`AliasChoices`). The loader takes any of them,
    but the schema of pydantic names only the first. For example, `generics` is another name of
    `parameters`. The spellings come from `dataclass.input_names`, the one table of what the
    loader accepts."""
    for model in _models_reachable_from(Design):
        target = schema if model is Design else schema.get("$defs", {}).get(model.__name__)
        properties = (target or {}).get("properties")
        if not properties:
            continue
        spellings: Dict[str, List[str]] = {}
        for spelling, name in input_names(model).items():
            spellings.setdefault(name, []).append(spelling)
        for names in spellings.values():
            spec = next((properties[s] for s in names if s in properties), None)
            if spec is not None:
                for spelling in names:
                    properties.setdefault(spelling, spec)


def design_schema(input_syntax: bool = True) -> Dict[str, Any]:
    """Return the JSON Schema of a Xeda design file (YAML, TOML or JSON).

    By default the schema describes the input syntax: everything a design file may contain,
    including the shorthands that the validators of the model accept (a bare string for
    `tb.top`, `true` for `tb.cocotb`, the name of a field as well as its alias). A file is
    written against this syntax, so `xeda design-schema` emits it.

    Pass `input_syntax=False` for the schema of the model itself, which describes the validated
    object instead of the accepted input.

    The input schema is permissive about unknown keys, because a file may use dotted-key
    shorthand that is expanded before validation. Use it to check that a design file is well
    formed, not to catch typos. The loader still rejects unknown keys.
    """
    schema = json_safe(Design.model_json_schema(by_alias=True))
    # pydantic 2 emits draft 2020-12 shapes (`$defs`, `prefixItems` for fixed-length tuples).
    # Declare the draft that pydantic actually produced, so that validators pick the right one.
    # Do not stamp a draft that the document does not follow.
    schema["$schema"] = JSON_SCHEMA_DIALECT
    if not input_syntax:
        return schema
    _accept_field_names(schema, json_safe(Design.model_json_schema(by_alias=False)))
    _accept_alias_choices(schema)
    _accept_clock_forms(schema)
    _accept_single_items(schema)
    _add_flat_form(schema)
    _open_property_sets(schema)
    for definition, properties in _EXTRA_INPUT_FORMS.items():
        target = schema if not definition else schema.get("$defs", {}).get(definition)
        if not target:
            continue
        target_properties = target.get("properties", {})
        for name, extra in properties.items():
            if name in target_properties:
                target_properties[name] = _widen(target_properties[name], extra)
    # Last: a target takes the properties of the root, including those that the steps above added.
    _add_targets(schema)
    return schema


def boards_info() -> List[Dict[str, Any]]:
    """Return the FPGA boards that ship with Xeda (`xeda/data/boards.toml`), by their lower-case
    names."""
    from .board import bundled_boards

    try:
        data = bundled_boards()
    except (FileNotFoundError, ModuleNotFoundError) as e:  # pragma: no cover
        log.warning("Could not load bundled boards.toml: %s", e)
        return []
    return [{"board": name, **json_safe(kv)} for name, kv in sorted(data.items())]


def provider_status(residue: Any, provider: str, root: Path) -> Dict[str, Any]:
    """Return where one provider keeps the PDK files of a platform in the store at `root`, and
    whether they are there.

    The result has the `provider`, its `version` (the pin of the residue, or `unpinned` for a
    `directory`), `installed` and `directory`, the directory of the platform in the store. The
    platform is installed by the rule of `pdk install` (`providers.installed`): every file is
    there, the version marker names the pin, and a `ciel` store has its receipt. If the marker
    names another version than the pin, `problem` says so. The function only looks at the files:
    it starts no process, uses no network and writes nothing."""
    from .platforms import PdkNotInstalled, locate
    from .platforms.providers import (
        UNPINNED,
        PdkVersionMismatch,
        installed,
        platform_directory,
        verify_pin,
    )

    version = UNPINNED if provider == "directory" else residue.providers.entry(provider).version
    status: Dict[str, Any] = {"provider": provider, "version": version}
    status["installed"] = installed(residue, provider, None, root) is not None
    if not status["installed"]:
        try:
            verify_pin(locate(residue, provider, None, root, None), residue)
        except PdkNotInstalled:
            pass
        except PdkVersionMismatch as error:
            status["problem"] = str(error)
    status["directory"] = str(platform_directory(residue, provider, None, root))
    return status


def platforms_info() -> List[Dict[str, Any]]:
    """Return the ASIC platforms bundled with Xeda, which the `platform` setting of the ASIC
    flows (`yosys` today) takes by name.

    Each platform has its `description` and its default `provider`, the `version` that Xeda pins
    for it, and `installed`: whether the default store (`~/.ciel`) holds the PDK files of that
    version. An installed platform also has the `directory` that holds them. If the files are
    there but name another version, `problem` says so. The function starts no process, uses no
    network and writes nothing.
    """
    from .platforms import bundled_platform_names, load_residue
    from .platforms.providers import default_root

    root = default_root()
    platforms: List[Dict[str, Any]] = []
    for name in bundled_platform_names():
        residue = load_residue(name)
        provider = residue.providers.default
        assert provider is not None  # a bundled platform names its default provider
        status = provider_status(residue, provider, root)
        entry: Dict[str, Any] = {
            "platform": name,
            "description": residue.description,
            "provider": provider,
            "version": status["version"],
            "installed": status["installed"],
        }
        if status["installed"]:
            entry["directory"] = status["directory"]
        if "problem" in status:
            entry["problem"] = status["problem"]
        platforms.append(entry)
    return platforms


def optimizers_info() -> List[Dict[str, Any]]:
    """Return the optimizers that `xeda dse --optimizer <name>` accepts."""
    # Keep these imports local to avoid loading the DSE stack during ordinary CLI startup.
    from . import flow_runner
    from .flow_runner.dse import dse_runner

    optimizers: List[Dict[str, Any]] = []
    for name in dir(flow_runner.dse):
        cls = getattr(flow_runner.dse, name)
        if (
            not inspect.isclass(cls)
            or not issubclass(cls, dse_runner.Optimizer)
            or cls is dse_runner.Optimizer
        ):
            continue
        from .utils import camelcase_to_snakecase

        optimizers.append(
            {
                "name": camelcase_to_snakecase(cls.__name__),
                "class": cls.__name__,
                "module": cls.__module__,
                "description": _own_docstring(cls),
                "settings": [
                    {
                        "name": field_name,
                        "type": type_str(
                            cls.Settings.model_json_schema()
                            .get("properties", {})
                            .get(field_name, {}),
                            cls.Settings.model_json_schema().get("$defs", {}),
                        ),
                        "required": model_field.is_required(),
                        "default": _default_of(model_field),
                        "description": model_field.description or None,
                    }
                    for field_name, model_field in cls.Settings.model_fields.items()
                    if not field_name.endswith("_")
                ],
            }
        )
    return sorted(optimizers, key=lambda d: d["name"])
