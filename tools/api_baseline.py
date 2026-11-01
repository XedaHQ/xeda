"""Snapshot the public API, keep release baselines, and classify the difference between them.

    python tools/api_baseline.py                  write tests/snapshots/current/ for review, and
                                                  the schemas in src/xeda/data/schemas/
    python tools/api_baseline.py --check          fail when a snapshot or a shipped schema is stale
    python tools/api_baseline.py --compare        classify the code against the newest baseline
    python tools/api_baseline.py --release X.Y    write tests/baselines/X.Y/ once, with a manifest

A baseline is the public contract of one release. Only `--release` writes it, and nothing edits it
after that. A breaking difference to the newest baseline needs a new major version and
`--accept-breaking`; `BREAKING.json` then lists every such difference.
"""

from __future__ import annotations

import argparse
import ast
import collections.abc
import dataclasses
import datetime
import enum
import functools
import hashlib
import importlib
import inspect
import json
import pathlib
import re
import sys
import types
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]

#: The public modules, and `xeda.workspace_verbs`, which holds the six module-level verbs until
#: `xeda.__all__` exports all of them. A module maps to the names it snapshots.
PUBLIC_MODULES = ("xeda", "xeda.errors", "xeda.introspect")
VERB_MODULE = "xeda.workspace_verbs"
VERBS = ("plan", "run", "sweep", "load", "records", "scrub")

SPECIAL_METHODS = (
    "__iter__",
    "__len__",
    "__getitem__",
    "__contains__",
    "__enter__",
    "__exit__",
    "__eq__",
    "__hash__",
    "__bool__",
)

# Modules whose names a snapshot writes without the module.
_PLAIN_MODULES = {"builtins", "typing", "collections.abc", "typing_extensions"}

SnapshotKind = Literal["api", "contract", "behavior", "schema"]


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------


@functools.cache
def _public_names() -> dict[int, str]:
    """The public classes by identity, so an annotation names them as the user imports them."""
    names: dict[int, str] = {}
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for name in module.__all__:
            value = getattr(module, name)
            if isinstance(value, type):
                names.setdefault(id(value), f"{module_name}.{name}")
    return names


def _class_name(cls: type) -> str:
    public = _public_names().get(id(cls))
    if public:
        return public
    module = cls.__module__
    if module in _PLAIN_MODULES:
        return cls.__qualname__
    # `pathlib._local.Path` (3.13) and `pathlib.Path` (3.11) are one name.
    if not module.startswith("xeda"):
        module = module.split(".")[0]
    return f"{module}.{cls.__qualname__}"


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    return repr(value)


def render_annotation(tp: Any) -> str:
    """The text of an annotation, the same on every supported Python version."""
    if isinstance(tp, str):
        return tp
    if tp is None or tp is type(None):
        return "None"
    if tp is Ellipsis:
        return "..."
    if isinstance(tp, typing.TypeVar):
        return tp.__name__
    if isinstance(tp, typing.ForwardRef):
        return tp.__forward_arg__
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Annotated:
        return render_annotation(args[0])
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(render_annotation(a) for a in args)
    if origin is typing.Literal:
        return "Literal[" + ", ".join(_literal(a) for a in args) + "]"
    if origin is not None:
        name = getattr(origin, "_name", None) or origin.__name__
        parts = [
            (
                "[" + ", ".join(render_annotation(b) for b in a) + "]"
                if isinstance(a, list)
                else render_annotation(a)
            )
            for a in args
        ]
        return f"{name}[{', '.join(parts) or '()'}]"
    if isinstance(tp, type):
        return _class_name(tp)
    return getattr(tp, "_name", None) or getattr(tp, "__name__", None) or repr(tp)


def _render_default(value: Any) -> str:
    text = repr(value)
    return f"<{type(value).__name__}>" if " at 0x" in text else text


def _parameters(sig: inspect.Signature, *, skip_first: bool = False) -> list[dict[str, str]]:
    result = []
    for position, p in enumerate(sig.parameters.values()):
        if skip_first and position == 0:
            continue
        entry = {"name": p.name, "kind": p.kind.name.lower()}
        if p.default is not inspect.Parameter.empty:
            entry["default"] = _render_default(p.default)
        if p.annotation is not inspect.Parameter.empty:
            entry["annotation"] = render_annotation(p.annotation)
        result.append(entry)
    return result


@functools.cache
def _namespace(module_name: str) -> dict[str, Any]:
    """The globals of a module, plus the imports under `if TYPE_CHECKING:`.

    A public annotation may name a class that the module imports for type checkers only. An
    optional package that is not installed becomes a stub class of the same name, so that the
    snapshot does not depend on what is installed."""
    module = importlib.import_module(module_name)
    namespace = dict(vars(module))
    for node in ast.parse(inspect.getsource(module)).body:
        if not (isinstance(node, ast.If) and ast.unparse(node.test).endswith("TYPE_CHECKING")):
            continue
        for statement in node.body:
            try:
                exec(compile(ast.Module([statement], []), module.__file__ or "", "exec"), namespace)
            except ImportError:
                for alias in statement.names:  # type: ignore[attr-defined]
                    if isinstance(statement, ast.ImportFrom):
                        stub: Any = type(alias.name, (), {"__module__": statement.module})
                    else:
                        stub = _Stub(alias.name.split(".")[0])
                    namespace[alias.asname or alias.name.split(".")[0]] = stub
    return namespace


class _Stub:
    """A package that is not installed: an attribute is a class that names its package."""

    def __init__(self, package: str) -> None:
        self.package = package

    def __getattr__(self, name: str) -> type:
        return type(name, (), {"__module__": self.package})


def _evaluate(annotations: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Resolve annotations that are strings or hold forward references."""

    # A class, because `typing.get_type_hints` refuses `ClassVar` in the annotations of a function.
    holder = type("Holder", (), {"__annotations__": annotations})
    return typing.get_type_hints(holder, globalns=_namespace(module_name), include_extras=True)


def _signature(obj: Any) -> inspect.Signature:
    sig = inspect.signature(obj)
    given = {p.name: p.annotation for p in sig.parameters.values() if p.annotation is not p.empty}
    if sig.return_annotation is not sig.empty:
        given["return"] = sig.return_annotation
    hints = _evaluate(given, (obj if isinstance(obj, type) else inspect.unwrap(obj)).__module__)
    parameters = [p.replace(annotation=hints.get(p.name, p.empty)) for p in sig.parameters.values()]
    return sig.replace(parameters=parameters, return_annotation=hints.get("return", sig.empty))


def _signature_entry(
    fn: Callable[..., Any], *, skip_first: bool = False, returns: bool = True
) -> dict[str, Any]:
    sig = _signature(fn)
    entry: dict[str, Any] = {"parameters": _parameters(sig, skip_first=skip_first)}
    if returns and sig.return_annotation is not inspect.Signature.empty:
        entry["return"] = render_annotation(sig.return_annotation)
    return entry


# ---------------------------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------------------------


def _is_xeda(cls: type) -> bool:
    return cls.__module__ == "xeda" or cls.__module__.startswith("xeda.")


def _member_entry(cls: type, name: str) -> dict[str, Any] | None:
    """A public method or property that Xeda code defines for `cls`, or `None`."""
    owner = next((k for k in cls.__mro__ if name in vars(k)), None)
    if owner is None or not _is_xeda(owner):
        return None
    raw = inspect.getattr_static(cls, name)
    if isinstance(raw, classmethod):
        return {"kind": "classmethod", **_signature_entry(raw.__func__, skip_first=True)}
    if isinstance(raw, staticmethod):
        return {"kind": "staticmethod", **_signature_entry(raw.__func__)}
    if isinstance(raw, (property, functools.cached_property)):
        getter = raw.fget if isinstance(raw, property) else raw.func
        entry: dict[str, Any] = {"kind": "property"}
        if getter is not None:
            hint = _signature(getter).return_annotation
            if hint is not inspect.Signature.empty:
                entry["annotation"] = render_annotation(hint)
        return entry
    if inspect.isfunction(raw):
        return {"kind": "method", **_signature_entry(raw, skip_first=True)}
    return None


def _attribute_default(cls: type, name: str) -> str | None:
    """The default of an annotated attribute as text, or `None` when it is required."""
    model_fields = getattr(cls, "model_fields", None)
    if model_fields is not None and name in model_fields:
        field = model_fields[name]
        if field.default_factory is not None:
            return "<factory>"
        return None if field.is_required() else _render_default(field.default)
    if dataclasses.is_dataclass(cls):
        for field in dataclasses.fields(cls):
            if field.name == name:
                if field.default_factory is not dataclasses.MISSING:
                    return "<factory>"
                return (
                    None if field.default is dataclasses.MISSING else _render_default(field.default)
                )
    try:
        return _render_default(inspect.getattr_static(cls, name))
    except AttributeError:
        return None


def _attributes(cls: type) -> list[dict[str, str]]:
    """The public annotated attributes that Xeda classes declare, bases first."""
    hints: dict[str, Any] = {}
    names: list[str] = []
    for klass in reversed(cls.__mro__):
        if _is_xeda(klass):
            own = _evaluate(dict(inspect.get_annotations(klass)), klass.__module__)
            hints.update(own)
            names += [n for n in own if n not in names]
    result = []
    for name in names:
        # A class variable is class-level configuration, not part of an instance.
        if name.startswith("_") or typing.get_origin(hints[name]) is typing.ClassVar:
            continue
        entry = {"name": name, "annotation": render_annotation(hints[name])}
        default = _attribute_default(cls, name)
        if default is not None:
            entry["default"] = default
        result.append(entry)
    return result


def _class_entry(cls: type) -> dict[str, Any]:
    is_enum = issubclass(cls, enum.Enum)
    entry: dict[str, Any] = {
        "kind": "enum" if is_enum else "class",
        "bases": [render_annotation(b) for b in cls.__bases__ if b is not object],
    }
    # A class that no Xeda code gives a constructor has the signature of its Python base, which
    # differs between Python versions.
    if not is_enum and any(
        _is_xeda(k) and ({"__init__", "__new__", "__signature__"} & vars(k).keys())
        for k in cls.__mro__
    ):
        entry["constructor"] = _signature_entry(cls, returns=False)
    if is_enum:
        entry["members"] = {m.name: repr(m.value) for m in cls}
    methods = {}
    for name in dir(cls):
        if not name.startswith("_") and (member := _member_entry(cls, name)):
            methods[name] = member
    entry["methods"] = methods
    entry["attributes"] = [] if is_enum else _attributes(cls)
    entry["special_methods"] = [
        name
        for name in SPECIAL_METHODS
        if any(_is_xeda(k) and vars(k).get(name) is not None for k in cls.__mro__)
    ]
    return entry


def _entry(value: Any) -> dict[str, Any]:
    if isinstance(value, type):
        return _class_entry(value)
    if inspect.ismodule(value):
        return {"kind": "module"}
    if callable(value):
        return {"kind": "function", **_signature_entry(value)}
    entry = {"kind": "constant", "type": render_annotation(type(value))}
    if isinstance(value, enum.Enum):
        entry["value"] = repr(value)
    return entry


def public_api_snapshot() -> dict[str, Any]:
    """The public API as plain data: for each public module, each name with its structure."""
    modules: dict[str, Any] = {}
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        modules[module_name] = {"names": {n: _entry(getattr(module, n)) for n in module.__all__}}
    verbs = importlib.import_module(VERB_MODULE)
    modules[VERB_MODULE] = {"names": {n: _entry(getattr(verbs, n)) for n in VERBS}}
    return {"modules": modules}


def builtin_contract() -> dict[str, Any]:
    """Return the declared built-in flow interfaces and error codes."""
    from xeda.flow.io import declared_outputs
    from xeda.flow_runner.error_codes import ALL_CODES
    from xeda.flows import __builtin_flows__
    from xeda.introspect import settings_class_info

    flows = {}
    for cls in sorted(__builtin_flows__, key=lambda c: c.name):
        fields = settings_class_info(cls)["fields"]
        flows[cls.name] = {
            "settings": {
                f["name"]: {
                    **{k: f.get(k) for k in ("type", "unit")},
                    **({"default": f.get("default")} if not f.get("required") else {}),
                }
                for f in fields
            },
            "outputs": {
                name: {"kinds": [t.name for t in d.types], "cardinality": d.cardinality}
                for name, d in declared_outputs(cls).items()
            },
            "metrics": {
                name: {"type": spec.type, "unit": spec.unit}
                for name, spec in cls.results_description.items()
            },
            "artifacts": sorted(cls.artifacts_description),
        }
    return {"flows": flows, "codes": {name: sorted(codes) for name, codes in ALL_CODES.items()}}


def _option_default(value: Any) -> Any:
    """A literal default as itself; any other default (a callable, a sentinel) by its type name."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)) and all(
        isinstance(v, (bool, int, float, str)) for v in value
    ):
        return list(value)
    return type(value).__name__


def behavior_snapshot() -> dict[str, Any]:
    """Return the stable name grammars, private files, session variables, run-directory hash length
    and command-line interface."""
    import click
    from xeda.cli import cli
    from xeda.flow.flow import FLOW_NAME
    from xeda.flow.metrics import ARTIFACT_KEY, METRIC_PATH
    from xeda.flow_runner.chains import OUTPUT_NAME
    from xeda.generation import SESSION_VARIABLES
    from xeda.local_settings import LOCAL_FILES
    from xeda.run_dir import DIR_NAME_HASH_LEN

    commands: dict[str, Any] = {}

    def visit(command: Any, path: str) -> None:
        commands[path] = {
            p.name: {
                "names": p.opts + p.secondary_opts,
                "flag": p.is_flag,
                "hidden": p.hidden,
                "type": p.type.name,
                "required": p.required,
                "multiple": p.multiple,
                "default": _option_default(p.default),
            }
            for p in command.params
            if isinstance(p, click.Option)
        }
        if isinstance(command, click.Group):
            for name, child in sorted(command.commands.items()):
                visit(child, f"{path} {name}")

    visit(cli, "xeda")
    return {
        "grammars": {
            "flow": FLOW_NAME.pattern,
            "output": OUTPUT_NAME.pattern,
            "metric": METRIC_PATH.pattern,
            "artifact": ARTIFACT_KEY.pattern,
        },
        "private_files": [[name, place, list(keys)] for name, place, keys in LOCAL_FILES],
        "session_variables": sorted(SESSION_VARIABLES),
        "run_directory_hash_length": DIR_NAME_HASH_LEN,
        "cli": commands,
    }


# ---------------------------------------------------------------------------------------------
# The document schemas
# ---------------------------------------------------------------------------------------------

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_DIRECTORY = Path("src") / "xeda" / "data" / "schemas"

_STRING: dict[str, Any] = {"type": "string"}
_INTEGER: dict[str, Any] = {"type": "integer"}
_NUMBER: dict[str, Any] = {"type": "number"}
_BOOLEAN: dict[str, Any] = {"type": "boolean"}
_OBJECT: dict[str, Any] = {"type": "object"}
_ANY: dict[str, Any] = {}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema, or null."""
    kind = schema.get("type")
    if isinstance(kind, str) and "enum" not in schema:
        return {**schema, "type": [kind, "null"]}
    if "enum" in schema:
        return {**schema, "type": [kind, "null"], "enum": [*schema["enum"], None]}
    return {"anyOf": [schema, {"type": "null"}]}


def _object(
    properties: dict[str, dict[str, Any]], optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    """An object with these keys. The writer always emits each key that is not `optional`."""
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(properties) - set(optional)),
    }


#: A field that the codec writes under another name or as the key of a table, not as a field.
_NOT_WRITTEN = {
    "RunRecord": ("run_root", "run_dir"),
    "OutputRecord": ("name",),
    "MetricInfo": ("path",),
    "ScrubResult": ("reasons",),
}


def _class_fields(cls: type) -> dict[str, Any]:
    """The fields of a dataclass, an attrs class or a named tuple, with their types."""
    if dataclasses.is_dataclass(cls):
        raw = {f.name: f.type for f in dataclasses.fields(cls)}
    elif hasattr(cls, "__attrs_attrs__"):
        raw = {a.name: a.type for a in cls.__attrs_attrs__}
    else:
        raw = dict(cls.__annotations__)
    skipped = _NOT_WRITTEN.get(cls.__name__, ())
    return {k: v for k, v in _evaluate(raw, cls.__module__).items() if k not in skipped}


class _Types:
    """The JSON Schema of Python types. A class in `references` is a `$ref` to a definition."""

    def __init__(self, references: dict[type, str] | None = None) -> None:
        self.references = references or {}

    def of(self, tp: Any) -> dict[str, Any]:  # noqa: C901
        if tp is typing.Any:
            return dict(_ANY)
        if tp is type(None):
            return {"type": "null"}
        origin, args = typing.get_origin(tp), typing.get_args(tp)
        if origin is typing.Annotated:
            return self.of(args[0])
        if origin is typing.Literal:
            return {"type": "string", "enum": list(args)}
        if origin is typing.Union or origin is types.UnionType:
            plain = [a for a in args if a is not type(None)]
            schemas = [self.of(a) for a in plain]
            schema = schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
            return _nullable(schema) if len(plain) < len(args) else schema
        if origin in (tuple, list, set, frozenset) or origin is collections.abc.Sequence:
            return _array(self.of(args[0]))
        if origin in (dict, collections.abc.Mapping):
            return {"type": "object", **({} if args[1] is typing.Any else {"additionalProperties": self.of(args[1])})}  # fmt: skip
        if tp is bool:
            return dict(_BOOLEAN)
        if tp is int:
            return dict(_INTEGER)
        if tp is float:
            return dict(_NUMBER)
        if tp is str or issubclass(tp, pathlib.PurePath):
            return dict(_STRING)
        if issubclass(tp, datetime.datetime):
            return {"type": "string", "format": "date-time"}
        if issubclass(tp, enum.Enum):
            return {"type": "string", "enum": [member.value for member in tp]}
        if tp in self.references:
            return {"$ref": f"#/$defs/{self.references[tp]}"}
        return self.cls(tp)

    def cls(self, tp: type) -> dict[str, Any]:
        return _object({name: self.of(hint) for name, hint in _class_fields(tp).items()})


def _document(
    title: str, schema: dict[str, Any], definitions: dict[str, Any] | None = None
) -> dict[str, Any]:
    document = {"$schema": DIALECT, "title": title, **schema}
    if definitions:
        document["$defs"] = definitions
    return document


def _listing(title: str, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    return _document(title, _object({"schema_version": _INTEGER, key: _array(entry)}))


def document_schemas() -> dict[str, dict[str, Any]]:
    """One JSON Schema for each document that Xeda writes or reads, by the name of the document.

    A schema comes from the code where the code has a type for the document: the fields of the
    dataclasses (`RunRecord`, `NodeResult`, `PlanNode`, `LaunchOptions`, `ScrubResult`,
    `DsePoint`) and the schema of the design model. The rest is the table below, written by hand:
    the builders of the documents fill a plain `dict`, so no type says what its keys hold.
    """
    from xeda.flow_runner.dse.dse_runner import DsePoint
    from xeda.flow_runner.launch_options import LaunchOptions
    from xeda.flow_runner.scrub import ScrubResult
    from xeda.introspect import design_schema
    from xeda.outcomes import NodeResult, Outcome
    from xeda.plan import PlanNode
    from xeda.records import OUTCOMES, ErrorInfo, MetricInfo, RunRecord

    simple = _Types()
    results = simple.cls(RunRecord)
    # A record never holds an outcome of a node that did not run.
    results["properties"]["outcome"] = {"type": "string", "enum": list(OUTCOMES)}
    error = simple.of(ErrorInfo)
    outcome = simple.of(Outcome)
    # The record of a node, and `best` of an exploration, are a `results.json`.
    node = _Types({RunRecord: "results"}).cls(NodeResult)
    defs = {"results": results}

    # `spec_document` fills a plain `dict` from keyword arguments.
    spec = _object(
        {
            "flow": _STRING,
            "design": _nullable(_STRING),
            "design_file": _nullable(_STRING),
            "target": _nullable(_STRING),
            "settings": _OBJECT,
        }
    )
    # A launch that never started has no outcome, no launch id and no spec; its `error` says why.
    run = _object(
        {
            "schema_version": _INTEGER,
            "outcome": _nullable(outcome),
            "error": _nullable(error),
            "launch_id": _nullable(_STRING),
            "spec": _nullable(spec),
            "roots": _array(_STRING),
            "nodes": _array(node),
        }
    )
    dry_run = _object(
        {
            "schema_version": _INTEGER,
            "spec": spec,
            "roots": _array(_STRING),
            "options": simple.of(LaunchOptions),
            "dependencies": simple.of(typing.get_type_hints(RunRecord)["dependencies"]),
            "nodes": _array(simple.of(PlanNode)),
        }
    )
    scrub_lists = {k: simple.of(v) for k, v in _class_fields(ScrubResult).items()}
    # `scrub_document` and `dse_document` take their parts as `Any` and fill a plain `dict`.
    scrub = _object(
        {
            "schema_version": _INTEGER,
            "outcome": _nullable(outcome),
            "error": _nullable(error),
            "flow": _nullable(_STRING),
            "design": _nullable(_STRING),
            "target": _nullable(_STRING),
            "run_root": _nullable(_STRING),
            **scrub_lists,
        }
    )
    dse = _object(
        {
            "schema_version": _INTEGER,
            "outcome": _nullable(outcome),
            "error": _nullable(error),
            "flow": _nullable(_STRING),
            "design": _nullable(_STRING),
            "target": _nullable(_STRING),
            "optimizer": _nullable(_STRING),
            "best": _nullable({"$ref": "#/$defs/results"}),
            "best_run_dir": _nullable(_STRING),
            "points": _array(simple.of(DsePoint)),
        }
    )

    # The listings come from `introspect`, which builds each entry as a plain `dict`.
    edge = _object(
        {
            "flow": _STRING,
            "output": _nullable(_STRING),
            "through": _array(_STRING),
            "binds": _array(_object({"flow": _STRING, "input": _STRING, "output": _STRING})),
            "target_dependent": _BOOLEAN,
        }
    )
    declared = {"name": _STRING, "value": _STRING, "types": _array(_STRING)}
    flow = _object(
        {
            "name": _STRING,
            "class": _STRING,
            "module": _STRING,
            "qualified_name": _STRING,
            "description": _STRING,
            "category": _STRING,
            "supports_cocotb": _BOOLEAN,
            "dependencies": _array(_STRING),
            "action_reason": _nullable(_STRING),
            "inputs": _array(
                _object(
                    {
                        **declared,
                        "cardinality": _STRING,
                        "required": _BOOLEAN,
                        "optional": _BOOLEAN,
                        "equivalence": _BOOLEAN,
                        "producer": _nullable(_STRING),
                        "output": _nullable(_STRING),
                        "same_producer_as": _nullable(_STRING),
                        "paired": _BOOLEAN,
                        "via": _nullable(_STRING),
                        "description": _STRING,
                    }
                )
            ),
            "outputs": _array(
                _object(
                    {
                        **declared,
                        "cardinality": _STRING,
                        "enabled_by": _nullable(_STRING),
                        "description": _STRING,
                    }
                )
            ),
            "can_follow": _array(edge),
            "can_precede": _array(edge),
            "settings_class": _STRING,
        }
    )
    setting = _object(
        {
            "name": _STRING,
            "type": _STRING,
            "required": _BOOLEAN,
            "unit": _nullable(_STRING),
            "default": _ANY,
            "description": _nullable(_STRING),
            "enum": _nullable(_array(_ANY)),
            "common": _BOOLEAN,
            "declared_by": _STRING,
            "writes": _nullable(_STRING),
            "opaque_keys": _BOOLEAN,
            "json_schema": _OBJECT,
            "tier": _STRING,
            "group": _nullable(_STRING),
        },
        optional=("unit",),  # a setting that only the input accepts has none
    )
    metric = _object(
        {
            "path": _STRING,
            "description": _STRING,
            "type": simple.of(_class_fields(MetricInfo)["type"]),
            "unit": _nullable(_STRING),
        }
    )
    artifact = _object({"key": _STRING, "description": _STRING, "when": _STRING})
    # A board has the keys of its entry in `boards.toml`; only the name is always there.
    from xeda.board import BOARD_ENTRY_KEYS

    board = _object(
        {"board": _STRING, **{k: _OBJECT if k == "fpga" else _STRING for k in BOARD_ENTRY_KEYS}},
        optional=BOARD_ENTRY_KEYS,
    )
    platform = _object(
        {
            "platform": _STRING,
            "description": _nullable(_STRING),
            "provider": _STRING,
            "version": _STRING,
            "installed": _BOOLEAN,
            "directory": _STRING,
            "problem": _STRING,
        },
        optional=("directory", "problem"),  # the files are installed, or they name another version
    )
    optimizer = _object(
        {
            "name": _STRING,
            "class": _STRING,
            "module": _STRING,
            "description": _nullable(_STRING),
            "settings": _array(
                _object(
                    {
                        "name": _STRING,
                        "type": _STRING,
                        "required": _BOOLEAN,
                        "unit": _nullable(_STRING),
                        "default": _ANY,
                        "description": _nullable(_STRING),
                    }
                )
            ),
        }
    )
    listing_of_flow = {"schema_version": _INTEGER, "flow": _STRING}

    return {
        "results": _document("results.json", results),
        "run": _document("xeda run --json", run, defs),
        "dry-run": _document("xeda run --dry-run --json", dry_run),
        "list-flows": _listing("xeda list-flows --json", "flows", flow),
        "list-settings": _document(
            "xeda list-settings --json",
            _object(
                {
                    **listing_of_flow,
                    "settings_class": _STRING,
                    "fields": _array(setting),
                    "definitions": _OBJECT,
                    "json_schema": _OBJECT,
                }
            ),
        ),
        "list-results": _document(
            "xeda list-results --json",
            _object(
                {
                    **listing_of_flow,
                    "metrics": _array(metric),
                    "artifacts": _array(artifact),
                    "note": _STRING,
                }
            ),
        ),
        "list-boards": _listing("xeda list-boards --json", "boards", board),
        "list-platforms": _listing("xeda list-platforms --json", "platforms", platform),
        "list-optimizers": _listing("xeda list-optimizers --json", "optimizers", optimizer),
        "scrub": _document("xeda scrub --json", scrub),
        "dse": _document("xeda dse --json", dse, defs),
        "design": design_schema(),
    }


#: The snapshot files: name, kind (for `classify`) and producer. A later snapshot is one more row.
SNAPSHOTS: dict[str, tuple[SnapshotKind, Callable[[], dict[str, Any]]]] = {
    "public_api.json": ("api", public_api_snapshot),
    "builtin.json": ("contract", builtin_contract),
    "behavior.json": ("behavior", behavior_snapshot),
    "schemas.json": ("schema", document_schemas),
}


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------

_POSITIONAL = ("positional_only", "positional_or_keyword")


def _diff_signature(
    path: str,
    old: dict[str, Any],
    new: dict[str, Any],
    add: list[str],
    brk: list[str],
    *,
    constructor: bool = False,
) -> None:
    old_by_name = {p["name"]: p for p in old["parameters"]}
    new_by_name = {p["name"]: p for p in new["parameters"]}
    new_positional = [p["name"] for p in new["parameters"] if p["kind"] in _POSITIONAL]
    # A field that a constructor gains after every old positional parameter keeps old calls valid.
    appended = [n for n in new_positional if n not in old_by_name]
    at_end = new_positional[len(new_positional) - len(appended) :] == appended

    for name in old_by_name.keys() - new_by_name.keys():
        brk.append(f"{path}: parameter {name!r} removed")
    for p in new["parameters"]:
        name, before = p["name"], old_by_name.get(p["name"])
        if before is None:
            if p["kind"] in ("var_positional", "var_keyword"):
                add.append(f"{path}: new parameter {name!r}")
            elif "default" not in p:
                brk.append(f"{path}: new required parameter {name!r}")
            elif p["kind"] == "keyword_only" or (constructor and at_end):
                add.append(f"{path}: new optional parameter {name!r}")
            else:
                brk.append(f"{path}: new positional parameter {name!r}")
            continue
        if before["kind"] != p["kind"]:
            brk.append(f"{path}: parameter {name!r} changed from {before['kind']} to {p['kind']}")
        _diff_value(f"{path}: parameter {name!r}", before, p, add, brk)
    kept_positional = [n for n in new_positional if n in old_by_name]
    old_positional = [
        p["name"]
        for p in old["parameters"]
        if p["kind"] in _POSITIONAL and p["name"] in new_by_name
    ]
    if kept_positional != old_positional:
        brk.append(f"{path}: positional order changed from {old_positional} to {kept_positional}")
    if old.get("return") != new.get("return"):
        brk.append(
            f"{path}: return annotation changed from {old.get('return')} to {new.get('return')}"
        )


def _diff_value(
    what: str, old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare the annotation and the default of a parameter or an attribute."""
    if old.get("annotation") != new.get("annotation"):
        brk.append(
            f"{what} annotation changed from {old.get('annotation')} to {new.get('annotation')}"
        )
    if old.get("default") == new.get("default"):
        return
    if "default" not in old:
        add.append(f"{what} gained the default {new['default']}")
    else:
        brk.append(f"{what} default changed from {old['default']} to {new.get('default')}")


def _diff_names(
    path: str, what: str, old: set[str], new: set[str], add: list[str], brk: list[str]
) -> None:
    brk.extend(f"{path}.{n}: {what} removed" for n in sorted(old - new))
    add.extend(f"{path}.{n}: new {what}" for n in sorted(new - old))


def _diff_class(
    path: str, old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    _diff_names(path, "base", set(old["bases"]), set(new["bases"]), add, brk)
    if "constructor" in old and "constructor" not in new:
        brk.append(f"{path}: constructor removed")
    elif "constructor" in new and "constructor" not in old:
        add.append(f"{path}: new constructor")
    elif "constructor" in old:
        _diff_signature(
            f"{path}.__init__", old["constructor"], new["constructor"], add, brk, constructor=True
        )

    old_methods, new_methods = old["methods"], new["methods"]
    _diff_names(path, "method", old_methods.keys(), new_methods.keys(), add, brk)
    for name in old_methods.keys() & new_methods.keys():
        before, after = old_methods[name], new_methods[name]
        if before["kind"] != after["kind"]:
            brk.append(f"{path}.{name}: changed from {before['kind']} to {after['kind']}")
        elif before["kind"] == "property":
            if before.get("annotation") != after.get("annotation"):
                brk.append(
                    f"{path}.{name}: annotation changed from {before.get('annotation')} "
                    f"to {after.get('annotation')}"
                )
        else:
            _diff_signature(f"{path}.{name}", before, after, add, brk)

    old_attrs = {a["name"]: a for a in old["attributes"]}
    new_attrs = {a["name"]: a for a in new["attributes"]}
    _diff_names(path, "attribute", old_attrs.keys(), new_attrs.keys(), add, brk)
    for name in old_attrs.keys() & new_attrs.keys():
        _diff_value(f"{path}.{name}", old_attrs[name], new_attrs[name], add, brk)

    _diff_names(
        path, "special method", set(old["special_methods"]), set(new["special_methods"]), add, brk
    )

    if old["kind"] == "enum":
        for name, value in old["members"].items():
            if name not in new["members"]:
                brk.append(f"{path}.{name}: member removed")
            elif new["members"][name] != value:
                brk.append(f"{path}.{name}: value changed from {value} to {new['members'][name]}")
        # A reader of an older version could not judge a member that it does not know.
        brk.extend(
            f"{path}.{n}: new member of a closed set"
            for n in new["members"].keys() - old["members"].keys()
        )


def _diff_entry(
    path: str, old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    if old["kind"] != new["kind"]:
        brk.append(f"{path}: changed from {old['kind']} to {new['kind']}")
    elif old["kind"] == "function":
        _diff_signature(path, old, new, add, brk)
    elif old["kind"] in ("class", "enum"):
        _diff_class(path, old, new, add, brk)
    elif old != new:
        brk.append(f"{path}: changed from {old} to {new}")


def _classify_api(old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]) -> None:
    for module, before in old["modules"].items():
        after = new["modules"].get(module)
        if after is None:
            brk.append(f"{module}: module removed")
            continue
        _diff_names(module, "public name", before["names"].keys(), after["names"].keys(), add, brk)
        for name in before["names"].keys() & after["names"].keys():
            _diff_entry(f"{module}.{name}", before["names"][name], after["names"][name], add, brk)
    add.extend(f"{m}: new module" for m in sorted(new["modules"].keys() - old["modules"].keys()))


def _classify_contract(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    _diff_names("flows", "flow", old["flows"].keys(), new["flows"].keys(), add, brk)
    for name in old["flows"].keys() & new["flows"].keys():
        before, after = old["flows"][name], new["flows"][name]
        for group in ("settings", "outputs", "metrics"):
            a, b = before[group], after[group]
            _diff_names(f"{name}.{group}", group, a.keys(), b.keys(), add, brk)
            if group == "settings":
                for key in b.keys() - a.keys():
                    if "default" not in b[key]:
                        add.remove(f"{name}.{group}.{key}: new {group}")
                        brk.append(f"{name}.{group}.{key}: new required setting")
            for key in a.keys() & b.keys():
                if a[key] != b[key]:
                    brk.append(f"{name}.{group}.{key}: changed from {a[key]} to {b[key]}")
        _diff_names(name, "artifact", set(before["artifacts"]), set(after["artifacts"]), add, brk)
    _diff_names("codes", "category", old["codes"].keys(), new["codes"].keys(), add, brk)
    for name in old["codes"].keys() & new["codes"].keys():
        _diff_names(name, "code", set(old["codes"][name]), set(new["codes"][name]), add, brk)


def _classify_behavior(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    for key in old.keys() | new.keys():
        if key != "cli" and old.get(key) != new.get(key):
            brk.append(f"{key}: changed from {old.get(key)} to {new.get(key)}")
    a, b = old["cli"], new["cli"]
    _diff_names("cli", "command", a.keys(), b.keys(), add, brk)
    for command in a.keys() & b.keys():
        _diff_names(command, "option", a[command].keys(), b[command].keys(), add, brk)
        for option in a[command].keys() & b[command].keys():
            if a[command][option] != b[command][option]:
                brk.append(
                    f"{command}.{option}: changed from {a[command][option]} to {b[command][option]}"
                )


#: The document whose schema describes what Xeda reads. The others describe what it writes.
INPUT_DOCUMENTS = ("design",)


def _resolve(node: Any, root: dict[str, Any]) -> Any:
    """The schema that a local `$ref` names, with the keywords beside the reference."""
    while isinstance(node, dict) and "$ref" in node:
        target: Any = root
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def _type_set(node: dict[str, Any]) -> frozenset[str] | None:
    """The JSON types that a schema names, or `None` for a schema that names none."""
    kind = node.get("type")
    if kind is None:
        return None
    return frozenset([kind] if isinstance(kind, str) else kind)


def _branches(node: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("anyOf", "oneOf"):
        if key in node:
            return list(node[key])
    return [node]


def _branch_kind(node: dict[str, Any]) -> str:
    """What pairs a branch of an old schema with a branch of a new one."""
    if "enum" in node:
        return "enum"
    kind = _type_set(node)
    return "any" if kind is None else "|".join(sorted(kind))


def _classify_schema(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare the schemas of the documents.

    A document that Xeda writes keeps its types, its enums and its keys: a removed key, another
    type, a narrower enum or a key that is no longer required breaks a reader of it, and a new key
    or a wider enum is an addition. The design file is what Xeda reads, so the direction reverses:
    it may accept more and never less, and a new key must be optional. A `$ref` is compared by
    what it names. Other keywords, such as `minimum` or `pattern`, are not compared."""
    _diff_names("schemas", "document", set(old), set(new), add, brk)

    def visit(
        path: str, before: Any, after: Any, roots: tuple[Any, Any], reader: bool,
        seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        if not (isinstance(before, dict) and isinstance(after, dict)):  # `true` or `false`
            if before != after:
                brk.append(f"{path}: schema changed")
            return
        pair = (str(before.get("$ref")), str(after.get("$ref")))
        if pair != ("None", "None"):
            if pair in seen:  # a recursive schema
                return
            seen = seen | {pair}
        before, after = _resolve(before, roots[0]), _resolve(after, roots[1])
        old_branches, new_branches = _branches(before), _branches(after)
        if len(old_branches) > 1 or len(new_branches) > 1:
            alternatives(path, old_branches, new_branches, roots, reader, seen)
            return
        old_types, new_types = _type_set(before), _type_set(after)
        if old_types != new_types:
            grew = new_types is None or (old_types is not None and old_types < new_types)
            (add if grew and reader else brk).append(f"{path}: type changed")
        old_enum, new_enum = before.get("enum"), after.get("enum")
        if old_enum != new_enum:
            grew = new_enum is None or (
                old_enum is not None and {repr(v) for v in old_enum} < {repr(v) for v in new_enum}
            )
            (add if grew else brk).append(f"{path}: enum {'widened' if grew else 'narrowed'}")
        properties(path, before, after, roots, reader, seen)
        old_items, new_items = before.get("items"), after.get("items")
        if isinstance(old_items, dict) and isinstance(new_items, dict):
            visit(f"{path}.items", old_items, new_items, roots, reader, seen)
        elif bool(old_items) != bool(new_items):
            (add if reader and not new_items else brk).append(f"{path}: items changed")

    def alternatives(
        path: str, left: list[Any], right: list[Any], roots: tuple[Any, Any], reader: bool,
        seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        unmatched = list(right)
        for branch in left:
            kind = _branch_kind(_resolve(branch, roots[0]))
            same = [n for n in unmatched if _branch_kind(_resolve(n, roots[1])) == kind]
            if not same:
                brk.append(f"{path}: alternative {kind} removed")
                continue
            unmatched.remove(same[0])
            visit(path, branch, same[0], roots, reader, seen)
        for branch in unmatched:
            kind = _branch_kind(_resolve(branch, roots[1]))
            (add if reader else brk).append(f"{path}: alternative {kind} added")

    def properties(
        path: str, before: dict[str, Any], after: dict[str, Any], roots: tuple[Any, Any],
        reader: bool, seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        old_props, new_props = before.get("properties", {}), after.get("properties", {})
        old_req, new_req = set(before.get("required", ())), set(after.get("required", ()))
        for key in sorted(old_props.keys() - new_props.keys()):
            brk.append(f"{path}.{key}: property removed")
        for key in sorted(new_props.keys() - old_props.keys()):
            required = reader and key in new_req
            (brk if required else add).append(
                f"{path}.{key}: " + ("new required property" if required else "new property")
            )
        for key in sorted(old_props.keys() & new_props.keys()):
            if (key in old_req) != (key in new_req):
                now_required = key in new_req
                # A reader of a written document needs the key: it must not become optional.
                # Xeda, as the reader of the design file, must not get a new demand.
                breaking = now_required if reader else not now_required
                (brk if breaking else add).append(
                    f"{path}.{key}: now " + ("required" if now_required else "optional")
                )
            visit(f"{path}.{key}", old_props[key], new_props[key], roots, reader, seen)
        old_extra, new_extra = before.get("additionalProperties"), after.get("additionalProperties")
        if isinstance(old_extra, dict) and isinstance(new_extra, dict):
            visit(f"{path}.*", old_extra, new_extra, roots, reader, seen)
        elif reader and old_extra is not False and new_extra is False:
            brk.append(f"{path}: unknown keys refused")

    for name in sorted(old.keys() & new.keys()):
        both = (old[name], new[name])
        visit(name, both[0], both[1], both, name in INPUT_DOCUMENTS, frozenset())


_CLASSIFIERS: dict[str, Callable[[dict[str, Any], dict[str, Any], list[str], list[str]], None]] = {
    "api": _classify_api,
    "contract": _classify_contract,
    "behavior": _classify_behavior,
    "schema": _classify_schema,
}


def classify(
    old: dict[str, Any], new: dict[str, Any], *, kind: SnapshotKind
) -> tuple[list[str], list[str]]:
    """The differences from `old` to `new` as `(additive, breaking)` messages."""
    if kind not in _CLASSIFIERS:
        raise NotImplementedError(f"no classifier for the snapshot kind {kind!r}")
    additive: list[str] = []
    breaking: list[str] = []
    _CLASSIFIERS[kind](old, new, additive, breaking)
    return sorted(additive), sorted(breaking)


# ---------------------------------------------------------------------------------------------
# Files and commands
# ---------------------------------------------------------------------------------------------


def _text(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in path.name.split("."))


def newest_baseline(root: Path = ROOT) -> Path:
    """The directory of the highest `tests/baselines/X.Y/`."""
    found = [
        p for p in (root / "tests" / "baselines").glob("*") if re.fullmatch(r"\d+\.\d+", p.name)
    ]
    if not found:
        raise FileNotFoundError(f"no baseline in {root / 'tests' / 'baselines'}")
    return max(found, key=_version_key)


def compare(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """The classified differences between the newest baseline and the code."""
    baseline = newest_baseline(root)
    additive: list[str] = []
    breaking: list[str] = []
    for name, (kind, produce) in SNAPSHOTS.items():
        old = json.loads((baseline / name).read_text(encoding="utf-8"))
        more, worse = classify(old, produce(), kind=kind)
        additive += [f"{name}: {m}" for m in more]
        breaking += [f"{name}: {m}" for m in worse]
    return additive, breaking


def _write(directory: Path) -> None:
    for name, (_, produce) in SNAPSHOTS.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_text(produce()), encoding="utf-8")


def _shipped(root: Path) -> dict[Path, str]:
    """The schema files that the package ships, with their text."""
    return {
        root / SCHEMA_DIRECTORY / f"{name}.schema.json": _text(schema)
        for name, schema in document_schemas().items()
    }


def _write_shipped(root: Path) -> None:
    shipped = root / SCHEMA_DIRECTORY
    shipped.mkdir(parents=True, exist_ok=True)
    for stale in shipped.glob("*.schema.json"):
        stale.unlink()
    for path, text in _shipped(root).items():
        path.write_text(text, encoding="utf-8")


def _manifest(directory: Path) -> str:
    files = sorted(p for p in directory.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
    return "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(directory).as_posix()}\n"
        for p in files
    )


def _print_differences(additive: list[str], breaking: list[str]) -> None:
    for title, items in (("additive", additive), ("breaking", breaking)):
        if items:
            print(f"{title}:\n" + "\n".join(f"  {item}" for item in items))
    if not (additive or breaking):
        print("no difference to the newest baseline")


def _release(root: Path, version: str, accept_breaking: bool) -> int:
    if not re.fullmatch(r"\d+\.\d+", version):
        print(f"error: the release must be X.Y, not {version!r}", file=sys.stderr)
        return 2
    target = root / "tests" / "baselines" / version
    if target.exists():
        print(f"error: {target} exists; a baseline is written once", file=sys.stderr)
        return 1
    breaking: list[str] = []
    try:
        previous = newest_baseline(root)
    except FileNotFoundError:
        pass
    else:
        _, breaking = compare(root)
        new_major = int(version.split(".")[0]) > _version_key(previous)[0]
        if breaking and not (new_major and accept_breaking):
            print(f"error: breaking differences to the baseline {previous.name}:", file=sys.stderr)
            print("\n".join(f"  {item}" for item in breaking), file=sys.stderr)
            print("A break needs a new major version and --accept-breaking.", file=sys.stderr)
            return 1
    _write(target)
    if breaking:
        (target / "BREAKING.json").write_text(_text({"breaking": breaking}), encoding="utf-8")
    (target / "MANIFEST.sha256").write_text(_manifest(target), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def _check(root: Path) -> int:
    current = root / "tests" / "snapshots" / "current"
    stale = [
        name
        for name, (_, produce) in SNAPSHOTS.items()
        if not (current / name).is_file()
        or (current / name).read_text(encoding="utf-8") != _text(produce())
    ]
    shipped = root / SCHEMA_DIRECTORY
    stale += [
        str(path.relative_to(root))
        for path, text in _shipped(root).items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]
    stale += [
        str(path.relative_to(root))
        for path in sorted(shipped.glob("*.schema.json"))
        if path not in _shipped(root)
    ]
    if stale:
        print(f"error: {', '.join(stale)} differ from the code.", file=sys.stderr)
        print("Run `python tools/api_baseline.py` and commit the result.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true", help="fail when the current snapshots are stale"
    )
    group.add_argument(
        "--compare", action="store_true", help="classify the code against the newest baseline"
    )
    group.add_argument("--release", metavar="X.Y", help="write tests/baselines/X.Y/")
    parser.add_argument(
        "--accept-breaking",
        action="store_true",
        help="with --release: allow a break in a new major",
    )
    args = parser.parse_args(argv)
    if args.accept_breaking and not args.release:
        parser.error("--accept-breaking needs --release")
    if args.check:
        return _check(root)
    if args.compare:
        additive, breaking = compare(root)
        _print_differences(additive, breaking)
        return 1 if breaking else 0
    if args.release:
        return _release(root, args.release, args.accept_breaking)
    _write(root / "tests" / "snapshots" / "current")
    _write_shipped(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
