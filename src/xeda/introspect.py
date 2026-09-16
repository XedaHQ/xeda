# © 2022-2025 [Kamyar Mohajerani](mailto:kammoh@gmail.com)
"""Machine-readable introspection of xeda's flows, settings, results, and design schema.

This is the single source of truth behind the CLI's ``--json``/``--format`` output, the
generated documentation, and the reference files of the agent skill. Everything here returns
plain JSON-serializable data and never touches the console, so it can be consumed
programmatically:

    >>> from xeda.introspect import flow_info, settings_info
    >>> flow_info("vivado_synth")["category"]
    'fpga_synthesis'
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import re
import textwrap
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

from importlib_resources import as_file, files
from pydantic import BaseModel

from .design import Design
from .flow import AsicSynthFlow, Flow, FpgaSynthFlow, SimFlow, SynthFlow, registered_flows
from .flow_runner import get_flow_class
from .flows import __builtin_flows__
from .utils import toml_load, unique

log = logging.getLogger(__name__)

__all__ = [
    "all_flow_classes",
    "boards_info",
    "design_schema",
    "flow_info",
    "flows_info",
    "json_safe",
    "optimizers_info",
    "platforms_info",
    "results_info",
    "settings_info",
]


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """Best-effort conversion of `value` into something `json.dump`-able."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if isinstance(value, (Path, os.PathLike)):
        return str(value)
    if isinstance(value, BaseModel):
        return json_safe(value.dict())
    if isinstance(value, Enum):
        return json_safe(value.value)
    return str(value)


def all_flow_classes() -> List[Type[Flow]]:
    """Every concrete, registered flow class, ordered by canonical name."""
    classes = {cls for cls in __builtin_flows__} | {cls for _mod, cls in registered_flows.values()}
    return sorted(classes, key=lambda cls: cls.name)


def _resolve(flow: Union[str, Type[Flow]]) -> Type[Flow]:
    if isinstance(flow, str):
        return get_flow_class(flow)
    return flow


def _own_docstring(cls: type) -> Optional[str]:
    """The class's *own* docstring, never one inherited from a base class.

    `inspect.getdoc` walks the MRO, which is how `list-flows` came to advertise flows as
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


# --------------------------------------------------------------------------------------------
# flows
# --------------------------------------------------------------------------------------------


def _declared_dependencies(cls: Type[Flow]) -> List[str]:
    """Flow names passed to `self.add_dependency(...)` in any `init()` along the MRO.

    Statically detected: dependencies are registered at run time and may be conditional, so
    this is a reliable *superset* hint rather than a guarantee.
    """
    found: List[str] = []
    for klass in cls.__mro__:
        init = klass.__dict__.get("init")
        if init is None:
            continue
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
        except (OSError, TypeError, SyntaxError):  # pragma: no cover - source not available
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_dependency"):
                continue
            target = node.args[0]
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr if isinstance(target, ast.Attribute) else None
            )
            if not name:
                continue
            try:
                found.append(get_flow_class(name).name)
            except Exception:  # noqa: BLE001 - a non-flow argument is simply not a dependency
                log.debug("add_dependency argument %s in %s is not a known flow", name, cls.name)
    return unique(found)


def flow_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Everything known about a flow that does not require instantiating it."""
    cls = _resolve(flow)
    return {
        "name": cls.name,
        "aliases": list(cls.aliases),
        "class": cls.__name__,
        "module": cls.__module__,
        "qualified_name": f"{cls.__module__}.{cls.__name__}",
        "description": _own_docstring(cls),
        "category": _category(cls),
        "supports_cocotb": bool(getattr(cls, "cocotb_sim_name", None)),
        "dependencies": _declared_dependencies(cls),
        "settings_class": f"{cls.Settings.__module__}.{cls.Settings.__qualname__}",
    }


def flows_info() -> List[Dict[str, Any]]:
    """`flow_info` for every registered flow, ordered by canonical name."""
    return [flow_info(cls) for cls in all_flow_classes()]


# --------------------------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------------------------


def type_str(field_schema: Any, definitions: Optional[Dict[str, Any]] = None) -> str:
    """Render a JSON-Schema fragment as a short, human- and agent-readable type expression."""
    definitions = definitions if definitions is not None else {}
    if isinstance(field_schema, (list, tuple)):
        return "|".join(type_str(f, definitions) for f in field_schema)
    if not isinstance(field_schema, dict):
        return "any"
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
        items = field_schema.get("items")
        if items:
            return f"array[{type_str(items, definitions)}]"
        return "array"
    for key, joiner in (("allOf", " & "), ("anyOf", " | "), ("oneOf", " | ")):
        variants = field_schema.get(key)
        if variants:
            return joiner.join(type_str(v, definitions) for v in variants)
    if typ is None:
        return "any"
    return str(typ)


def _default_of(model_field: Any) -> Any:
    """JSON-safe default of a pydantic v1 field; `None` for fields that have no default."""
    if model_field.required:
        return None
    default = model_field.default
    if default is None or repr(default) == "PydanticUndefined":
        return None
    return json_safe(default)


def _field_entries(cls: Type[Flow]) -> List[Dict[str, Any]]:
    schema = cls.Settings.schema(by_alias=True)
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    definitions = schema.get("definitions", {})
    base_field_names = set(Flow.Settings.__fields__)

    entries: List[Dict[str, Any]] = []
    for name, model_field in cls.Settings.__fields__.items():
        info = model_field.field_info
        if info.extra.get("hidden_from_schema") or name.endswith("_"):
            continue
        alias = model_field.alias if model_field.alias != name else None
        prop = properties.get(model_field.alias, properties.get(name, {}))
        declared_by = next(
            (
                f"{b.__module__}.{b.__qualname__}"
                for b in reversed(cls.Settings.__mro__)
                if name in getattr(b, "__annotations__", {})
            ),
            None,
        )
        entries.append(
            {
                "name": name,
                "alias": alias,
                "type": type_str(prop, definitions),
                "required": bool(model_field.required)
                or (model_field.alias in required)
                or (name in required),
                "default": _default_of(model_field),
                "description": info.description or None,
                "enum": prop.get("enum"),
                "common": name in base_field_names,
                "declared_by": declared_by,
                "json_schema": json_safe(prop),
            }
        )
    return entries


def settings_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Flattened, fully-named settings of a flow, plus the raw JSON Schema.

    Every field is reported under the name that can be typed as ``-s <name>=<value>``, including
    the fields shared by all flows (``ncpus``, ``dockerized``, ...) which the settings table
    used to hide.
    """
    cls = _resolve(flow)
    schema = cls.Settings.schema(by_alias=True)
    return {
        "flow": cls.name,
        "settings_class": f"{cls.Settings.__module__}.{cls.Settings.__qualname__}",
        "fields": _field_entries(cls),
        "definitions": json_safe(schema.get("definitions", {})),
        "json_schema": json_safe(schema),
    }


# --------------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------------


_NAMED_GROUP_RE = re.compile(r"\(\?P<([A-Za-z_]\w*)>")


def _detected_result_keys(cls: Type[Flow]) -> List[str]:
    """Result keys a flow's own source shows it reports, from three complementary signals.

    1. assignments to ``self.results[...]`` / ``self.results....``
    2. named groups of the regexes handed to ``parse_report_regex`` / ``parse_regex``, which is
       how most flows populate results
    3. keys the flow reads back via ``self.results[...]`` / ``.get(...)``

    Statically detected, so dynamically-computed keys (e.g. per-resource FPGA utilization) are
    not listed. By convention, keys starting with ``_`` are internal and excluded.
    """
    keys: List[str] = []
    for klass in cls.__mro__:
        if klass is object:
            continue
        try:
            source = textwrap.dedent(inspect.getsource(klass))
        except (OSError, TypeError):  # pragma: no cover - source not available
            continue
        if "parse_regex" in source or "parse_report_regex" in source:
            keys += _NAMED_GROUP_RE.findall(source)
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            # self.results["key"] anywhere (assigned, augmented, or read back)
            if isinstance(node, ast.Subscript):
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "results"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    keys.append(node.slice.value)
            # self.results.key = ... (Box attribute assignment)
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "results"
                    ):
                        keys.append(target.attr)
            # self.results.get("key")
            elif isinstance(node, ast.Call) and node.args:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "results"
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    keys.append(node.args[0].value)
    return sorted(unique([k for k in keys if not k.startswith("_")]))


#: Reported by every cocotb-driven simulation flow, via `Cocotb.add_results`.
COCOTB_RESULTS: Dict[str, str] = {
    "cocotb.tests": "Number of cocotb tests that ran.",
    "cocotb.errors": "Number of cocotb tests that errored.",
    "cocotb.failures": "Number of cocotb tests that failed.",
    "cocotb.skipped": "Number of cocotb tests that were skipped.",
    "cocotb.time": "Wall-clock duration of the cocotb regression, in seconds.",
    "cocotb.sim_time_ns": "Total simulated time of the cocotb regression, in nanoseconds.",
}


def results_info(flow: Union[str, Type[Flow]]) -> Dict[str, Any]:
    """Result keys a flow writes to `results.json`, with descriptions where documented.

    A flow that declares `results_description` is trusted: only its documented keys (plus the
    ones every flow reports) are listed. For a flow that has not been documented yet, keys are
    detected from its source as a best effort and the `note` says so.
    """
    cls = _resolve(flow)
    documented: Dict[str, str] = dict(getattr(cls, "results_description", {}) or {})
    if getattr(cls, "cocotb_sim_name", None):
        documented = {**COCOTB_RESULTS, **documented}
    # A flow counts as documented once it declares `results_description` itself -- including as an
    # empty dict, which is how a flow states that it reports nothing beyond the common keys.
    curated = bool(documented) or any(
        "results_description" in klass.__dict__ for klass in cls.__mro__ if klass is not Flow
    )
    common = {
        "success": "Whether the flow completed successfully. This decides the process exit status.",
        "runtime": "Wall-clock duration of run() in fractional seconds.",
        "tools": "Executables invoked by the flow, with their detected versions.",
        "artifacts": "Files produced by the flow, by name.",
        "design": "Name of the design that was run.",
        "design_hash": "Hash of the design's RTL and testbench fingerprints.",
        "flow": "Canonical flow name.",
        "flow_hash": "Hash of the flow name and its effective settings.",
        "run_path": "Absolute path of the run directory holding reports/, outputs/ and checkpoints/.",
        "timestamp": "Local time the run finished, as YYYY-MM-DD-HHMMSS.",
    }
    extra = [] if curated else [k for k in _detected_result_keys(cls) if k not in documented]
    keys = [
        {
            "name": key,
            "description": common[key],
            "common": True,
            "documented": True,
        }
        for key in common
    ] + [
        {
            "name": key,
            "description": documented.get(key),
            "common": False,
            "documented": key in documented,
        }
        for key in list(documented) + extra
        if key not in common
    ]
    note = (
        "Keys starting with '_' are internal detail and are omitted. Some keys are only present "
        "when the relevant setting is enabled or the tool reports them. The authoritative record "
        "of a run is always the results.json in its run directory."
        if curated
        else "This flow's results are not documented yet: the keys below were detected from its "
        "source as a best effort and may be incomplete or over-inclusive. Read the results.json "
        "of an actual run for the definitive list."
    )
    aliases = {
        canonical: list(candidates)
        for canonical, candidates in getattr(cls, "results_canonical_aliases", {}).items()
    }
    return {
        "flow": cls.name,
        "documented": curated,
        "keys": keys,
        "canonical_aliases": aliases,
        "note": note,
    }


# --------------------------------------------------------------------------------------------
# designs, boards, platforms, optimizers
# --------------------------------------------------------------------------------------------


#: Input shapes a field's validators accept but the model schema does not describe. Keyed by
#: definition name ("" for the root Design model), then by property name. Where a field has an
#: alias, both names are listed, because the loader accepts either.
_EXTRA_INPUT_FORMS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    # Design.authors accepts a single "Name <email>" string
    "": {"author": [{"type": "string"}], "authors": [{"type": "string"}]},
    # tb.top accepts a bare module name; tb.cocotb accepts `true`
    "TbSettings": {"top": [{"type": "string"}], "cocotb": [{"type": "boolean"}]},
    # language.vhdl = "2008" and language.vhdl = 2008 are both accepted
    "Language": {
        "vhdl": [{"type": "string"}, {"type": "integer"}],
        "verilog": [{"type": "string"}, {"type": "integer"}],
    },
    "LanguageSettings": {"version": [{"type": "integer"}], "standard": [{"type": "integer"}]},
    "VhdlSettings": {"version": [{"type": "integer"}], "standard": [{"type": "integer"}]},
}


#: Top-level shorthands that `Design.from_file` folds into `rtl` before validation, via
#: `Design.process_compatibility`. A design file may use either form, so both are described.
_FLAT_RTL_PROPERTIES = ("sources", "top", "clock", "clocks", "parameters", "defines", "generator")


def _add_flat_form(schema: Dict[str, Any]) -> None:
    """Describe the flat top-level form, e.g. `sources` at the root instead of `rtl.sources`."""
    definitions = schema.get("definitions", {})
    rtl_properties = definitions.get("RtlSettings", {}).get("properties", {})
    root = schema.setdefault("properties", {})
    for name in _FLAT_RTL_PROPERTIES:
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


def _open_property_sets(node: Any) -> None:
    """Drop `additionalProperties: false` throughout the input schema.

    The models set `extra = forbid`, so pydantic emits a closed property set. A design *file* is
    not closed in that sense: it may use dotted-key shorthand (`clock.port`, `hdl.vhdl.standard`,
    `fpga.part`) that `Design.from_file` expands before the model ever sees it. Keeping the
    closed set would reject files that load perfectly well. The loader still rejects genuinely
    unknown keys once the file has been expanded.
    """
    if isinstance(node, dict):
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
    return {**documentation, "anyOf": [*branches, *extra]}


def _accept_field_names(by_alias: Dict[str, Any], by_name: Dict[str, Any]) -> None:
    """Accept a field's own name wherever the schema names its alias.

    Models set `allow_population_by_field_name`, so `language` and `hdl` (its alias) are equally
    valid in a design file. A schema generated with aliases alone would reject half of that.
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
    definitions_by_name = by_name.get("definitions", {})
    for name, spec in by_alias.get("definitions", {}).items():
        counterpart = definitions_by_name.get(name)
        if counterpart:
            merge(spec, counterpart)


def design_schema(input_syntax: bool = True) -> Dict[str, Any]:
    """JSON Schema of a xeda design description (a design TOML/YAML/JSON file).

    By default this describes the *input* syntax: everything a design file may contain, including
    the shorthands the model's validators accept (a bare string for `tb.top`, `true` for
    `tb.cocotb`, a field's own name as well as its alias). That is what a file is written
    against, so it is what `xeda design-schema` emits.

    Pass `input_syntax=False` for the model's own schema, which describes the validated object
    rather than the accepted input.

    The input schema is deliberately permissive about unknown keys, because a file may use
    dotted-key shorthand that is expanded before validation. Use it to check that a design file
    is well formed, not to catch typos; the loader still rejects unknown keys.
    """
    schema = json_safe(Design.schema(by_alias=True))
    # pydantic v1 emits draft-07 shapes (notably array-form `items` for fixed-length tuples),
    # which later drafts reject. Declare the draft so validators pick the right one.
    schema.setdefault("$schema", "http://json-schema.org/draft-07/schema#")
    if not input_syntax:
        return schema
    _accept_field_names(schema, json_safe(Design.schema(by_alias=False)))
    _add_flat_form(schema)
    _open_property_sets(schema)
    for definition, properties in _EXTRA_INPUT_FORMS.items():
        target = schema if not definition else schema.get("definitions", {}).get(definition)
        if not target:
            continue
        target_properties = target.get("properties", {})
        for name, extra in properties.items():
            if name in target_properties:
                target_properties[name] = _widen(target_properties[name], extra)
    return schema


def boards_info() -> List[Dict[str, Any]]:
    """FPGA boards shipped with xeda (`xeda/data/boards.toml`)."""
    try:
        res = files("xeda.data").joinpath("boards.toml")
        with as_file(res) as path:
            data = toml_load(path)
    except (FileNotFoundError, ModuleNotFoundError) as e:  # pragma: no cover
        log.warning("Could not load bundled boards.toml: %s", e)
        return []
    return [{"board": name, **json_safe(kv)} for name, kv in sorted(data.items())]


def platforms_info() -> List[Dict[str, Any]]:
    """ASIC PDK platforms bundled with xeda (used by the OpenROAD and DC flows).

    Platform data is excluded from the wheel, so an installed xeda usually reports none.
    """
    platforms: List[Dict[str, Any]] = []
    try:
        root = Path(str(files("xeda.platforms")))
    except (ModuleNotFoundError, TypeError) as e:  # pragma: no cover
        log.debug("xeda.platforms is not importable: %s", e)
        return platforms
    if not root.is_dir():  # pragma: no cover
        return platforms
    for config in sorted(root.glob("*/config.toml")):
        entry: Dict[str, Any] = {"platform": config.parent.name, "config": str(config)}
        try:
            data = toml_load(config)
            for key in ("name", "version", "description", "process"):
                if key in data:
                    entry[key] = json_safe(data[key])
        except Exception as e:  # noqa: BLE001 - a malformed PDK must not break listing
            entry["error"] = str(e)
        platforms.append(entry)
    return platforms


def optimizers_info() -> List[Dict[str, Any]]:
    """DSE optimizers usable via `xeda dse --optimizer <name>`."""
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
                            cls.Settings.schema().get("properties", {}).get(field_name, {}),
                            cls.Settings.schema().get("definitions", {}),
                        ),
                        "required": bool(model_field.required),
                        "default": _default_of(model_field),
                        "description": model_field.field_info.description or None,
                    }
                    for field_name, model_field in cls.Settings.__fields__.items()
                    if not field_name.endswith("_")
                ],
            }
        )
    return sorted(optimizers, key=lambda d: d["name"])
