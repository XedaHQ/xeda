"""Snapshot the public API, keep release baselines, and classify the difference between them.

    python tools/api_baseline.py                  write tests/snapshots/current/ for review, and
                                                  the schemas in the data/schemas directory
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
import fractions
import functools
import hashlib
import importlib
import inspect
import itertools
import json
import pathlib
import re
import sys
import types
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from xeda._internal.package_paths import IMPL, impl_module, schemas_dir, source_root

ROOT = Path(__file__).resolve().parents[1]

#: The public modules, and the module that holds the six module-level verbs. A module maps to the
#: names it snapshots. `VERB_KEY` is the key of the verbs in the snapshot, and it stays the same
#: wherever the module is.
PUBLIC_MODULES = ("xeda", "xeda.errors", "xeda.introspect")
VERB_MODULE = impl_module("workspace_verbs")
VERB_KEY = "xeda.workspace_verbs"
VERBS = ("plan", "run", "sweep", "load_record", "records", "scrub")

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
    # An internal class has the same name wherever the implementation lives.
    elif module == IMPL or module.startswith(f"{IMPL}."):
        module = "xeda" + module[len(IMPL) :]
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


@functools.cache
def _exported() -> frozenset[type]:
    """The classes that a public module exports."""
    return frozenset(
        value
        for module_name in PUBLIC_MODULES
        for name in importlib.import_module(module_name).__all__
        if isinstance(value := getattr(importlib.import_module(module_name), name), type)
    )


def _internal_bases(cls: type) -> list[type]:
    """The Xeda base classes of `cls` that no public module exports: a user cannot import them."""
    return [k for k in cls.__mro__ if _is_xeda(k) and k not in _exported()]


def _public_bases(cls: type) -> list[type]:
    """The bases of `cls` as the API names them. An internal base gives way to its own bases, and
    pydantic is an implementation detail."""
    result: list[type] = []
    for base in cls.__bases__:
        found = _public_bases(base) if _is_xeda(base) and base not in _exported() else [base]
        result += [
            k
            for k in found
            if k is not object and not k.__module__.startswith("pydantic") and k not in result
        ]
    return result


def _member_entry(cls: type, name: str) -> dict[str, Any] | None:
    """A public method or property that a public Xeda class defines for `cls`, or `None`.

    A name that an internal base class also defines is machinery of the models (a loader hook, a
    cache reset), and the `model_` names are pydantic's: neither is API."""
    owner = next((k for k in cls.__mro__ if name in vars(k)), None)
    if owner is None or not _is_xeda(owner) or name.startswith("model_"):
        return None
    if any(name in vars(k) for k in _internal_bases(cls)):
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


def _is_working_state(cls: type, name: str) -> bool:
    """Whether `name` is a model field that a dump leaves out: state that planning sets."""
    field = getattr(cls, "model_fields", {}).get(name)
    return field is not None and bool(field.exclude)


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
        if _is_xeda(klass) and klass not in _internal_bases(cls):
            own = _evaluate(dict(inspect.get_annotations(klass)), klass.__module__)
            hints.update(own)
            names += [n for n in own if n not in names]
    result = []
    for name in names:
        # A class variable is class-level configuration, not part of an instance.
        if (
            name.startswith("_")
            or typing.get_origin(hints[name]) is typing.ClassVar
            or _is_working_state(cls, name)
        ):
            continue
        entry = {"name": name, "annotation": render_annotation(hints[name])}
        default = _attribute_default(cls, name)
        if default is not None:
            entry["default"] = default
        result.append(entry)
    return result


#: The classes that a loader builds: their `__init__` only refuses, so it is no API.
LOADED_CLASSES = ("Project",)


def _class_entry(cls: type) -> dict[str, Any]:
    is_enum = issubclass(cls, enum.Enum)
    entry: dict[str, Any] = {
        "kind": "enum" if is_enum else "class",
        "bases": [render_annotation(b) for b in _public_bases(cls)],
    }
    # A class that no Xeda code gives a constructor has the signature of its Python base, which
    # differs between Python versions.
    if (
        not is_enum
        and cls.__name__ not in LOADED_CLASSES
        and any(
            _is_xeda(k) and ({"__init__", "__new__", "__signature__"} & vars(k).keys())
            for k in cls.__mro__
        )
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
    modules[VERB_KEY] = {"names": {n: _entry(getattr(verbs, n)) for n in VERBS}}
    return {"modules": modules}


#: Keywords whose value is data, not a schema, and keywords whose value maps names to schemas.
_SCHEMA_DATA = ("default", "enum", "const", "examples")
_SCHEMA_NAME_MAPS = ("properties", "patternProperties", "$defs")


def _plain_schema(node: Any) -> Any:
    """`node` without the `title`, `description` and `examples` keywords, at every level."""
    if isinstance(node, list):
        return [_plain_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {
        k: (
            v
            if k in _SCHEMA_DATA
            else (
                {n: _plain_schema(s) for n, s in v.items()}
                if k in _SCHEMA_NAME_MAPS
                else _plain_schema(v)
            )
        )
        for k, v in node.items()
        if k not in ("title", "description", "examples")
    }


def _reachable_definitions(schemas: list[Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """The entries of `definitions` that `schemas` name with `$ref`, directly or through each other."""
    found: dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and ref[8:] not in found:
                found[ref[8:]] = definitions[ref[8:]]
                walk(found[ref[8:]])
            for key, value in node.items():
                if key not in _SCHEMA_DATA:
                    walk(value)

    walk(schemas)
    return {name: _plain_schema(found[name]) for name in sorted(found)}


def _flow_settings(cls: Any) -> dict[str, Any]:
    """The settings of a flow by name, and the `$defs` that their schemas name."""
    from xeda._internal.introspect import settings_class_info

    fields = settings_class_info(cls)["fields"]
    names = {f["name"] for f in fields}
    is_group = {f["name"] for f in fields if any(n.startswith(f["name"] + ".") for n in names)}
    settings: dict[str, Any] = {}
    for f in fields:
        schema = (
            None
            if f["name"] in is_group
            else {k: v for k, v in _plain_schema(f["json_schema"]).items() if k != "default"}
        )
        settings[f["name"]] = {
            **{k: f.get(k) for k in ("type", "unit")},
            **({"default": f.get("default")} if not f.get("required") else {}),
            **({"experimental": True} if f.get("experimental") else {}),
            **({"schema": schema} if schema is not None else {}),
        }
    definitions = _reachable_definitions(
        [s["schema"] for s in settings.values() if "schema" in s],
        cls.Settings.model_json_schema().get("$defs", {}),
    )
    return {"settings": settings, **({"$defs": definitions} if definitions else {})}


def builtin_contract() -> dict[str, Any]:
    """Return the declared built-in flow interfaces and error codes."""
    from xeda._internal.flow.io import declared_inputs, declared_outputs
    from xeda._internal.flow_runner.error_codes import ALL_CODES
    from xeda._internal.flows import __builtin_flows__

    flows = {}
    for cls in sorted(__builtin_flows__, key=lambda c: c.name):
        flows[cls.name] = {
            **({"experimental": True} if cls.experimental else {}),
            **_flow_settings(cls),
            "required_settings": sorted(cls.required_settings),
            "inputs": {
                name: {
                    "kinds": [t.name for t in d.types],
                    "cardinality": d.cardinality,
                    "required": d.required,
                }
                for name, d in declared_inputs(cls).items()
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
    """A literal default as itself, an omitted one (Click's sentinel) as `None`, and any other
    default (a callable) by its type name."""
    from click.core import UNSET

    if value is None or value is UNSET:
        return None
    if isinstance(value, (bool, int, float, str)):
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
    from xeda._internal.cli import cli
    from xeda._internal.flow.flow import FLOW_NAME
    from xeda._internal.flow.metrics import ARTIFACT_KEY, METRIC_PATH
    from xeda._internal.flow_runner.chains import OUTPUT_NAME
    from xeda._internal.generation import SESSION_VARIABLES
    from xeda._internal.local_settings import LOCAL_FILES
    from xeda._internal.project import PROJECT_KEYS
    from xeda._internal.cli_types import FlowChoice
    from xeda._internal.run_dir import DIR_NAME_HASH_LEN

    commands: dict[str, Any] = {}

    def parameter(p: click.Parameter, ctx: click.Context) -> dict[str, Any]:
        envvars = {p.envvar} if isinstance(p.envvar, str) else set(p.envvar or ())
        prefix = ctx.auto_envvar_prefix
        if isinstance(p, click.Option) and p.allow_from_autoenv and prefix and p.name:
            envvars.add(f"{prefix}_{p.name.upper()}")
        # The flow names are the flow registry, judged in the built-in contract: a plugin or a test
        # adds to them.
        choice = (
            p.type
            if isinstance(p.type, click.Choice) and not isinstance(p.type, FlowChoice)
            else None
        )
        ranged = p.type if isinstance(p.type, (click.IntRange, click.FloatRange)) else None
        return {
            "type": p.type.name,
            "required": p.required,
            "nargs": p.nargs,
            "default": _option_default(p.default),
            **(
                {"choices": list(choice.choices), "case_sensitive": choice.case_sensitive}
                if choice
                else {}
            ),
            **(
                {"range": [ranged.min, ranged.max, ranged.min_open, ranged.max_open]}
                if ranged
                else {}
            ),
            **({"environment": sorted(envvars)} if envvars else {}),
        }

    def visit(command: Any, path: str, ctx: click.Context) -> None:
        commands[path] = {
            "options": {
                p.name: {
                    "names": p.opts + p.secondary_opts,
                    "flag": p.is_flag,
                    "hidden": p.hidden,
                    "multiple": p.multiple,
                    "default": _option_default(p.default),
                    **parameter(p, ctx),
                }
                for p in command.params
                if isinstance(p, click.Option)
            },
            "arguments": [
                {"name": p.name, **parameter(p, ctx)}
                for p in command.params
                if isinstance(p, click.Argument)
            ],
        }
        if isinstance(command, click.Group):
            for name, child in sorted(command.commands.items()):
                visit(
                    child,
                    f"{path} {name}",
                    child.make_context(name, [], parent=ctx, resilient_parsing=True),
                )

    visit(cli, "xeda", cli.make_context("xeda", [], resilient_parsing=True))
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
        "project_keys": sorted(PROJECT_KEYS),
        "cli": commands,
    }


# ---------------------------------------------------------------------------------------------
# The document schemas
# ---------------------------------------------------------------------------------------------

DIALECT = "https://json-schema.org/draft/2020-12/schema"
#: Relative to the root of a checkout. The tool writes and checks the files of a checkout, wherever
#: the imported package is installed.
SCHEMA_DIRECTORY = Path("src") / schemas_dir().relative_to(source_root())

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
    from xeda._internal.flow_runner.dse.dse_runner import DsePoint
    from xeda._internal.flow_runner.launch_options import LaunchOptions
    from xeda._internal.flow_runner.scrub import ScrubResult
    from xeda.introspect import design_schema
    from xeda._internal.outcomes import NodeResult, Outcome
    from xeda._internal.plan import PlanNode
    from xeda._internal.records import OUTCOMES, ErrorInfo, MetricInfo, RunRecord

    simple = _Types()
    results = simple.cls(RunRecord)
    # A record never holds an outcome of a node that did not run.
    results["properties"]["outcome"] = {"type": "string", "enum": list(OUTCOMES)}
    error = simple.of(ErrorInfo)
    outcome = simple.of(Outcome)
    # `node_configuration` fills a plain `dict`; a part and `language` are there when the flow reads
    # them, and `sources` only when it reads the sources. The object is open: a release may add keys.
    source = _object(
        {"path": _STRING, "type": _STRING, "dependency": _INTEGER, "generator": _STRING},
        optional=("dependency", "generator"),
    )
    parameters = {"top": _nullable(_STRING), "parameters": _OBJECT, "defines": _OBJECT}
    rtl = _object(
        {
            **parameters,
            "clocks": _array(_object({"port": _STRING, "name": _STRING})),
            "attributes": _OBJECT,
            "sources": _array(source),
        },
        optional=("sources",),
    )
    tb = _object(
        {
            **parameters,
            "top": _array(_STRING),
            "uut": _nullable(_STRING),
            "cocotb": _nullable(_OBJECT),
            "sources": _array(source),
        },
        optional=("sources",),
    )
    configuration = _object(
        {
            "rtl": rtl,
            "tb": tb,
            "language": _OBJECT,
            "converted_from": _STRING,
            "transforms": _array(_STRING),
            "produces": _array(_STRING),
        },
        optional=("rtl", "tb", "language", "converted_from", "transforms", "produces"),
    )
    # `DependencyRecord.as_mapping` always writes these four keys.
    dependency = _object(
        {
            "repository": _nullable(_STRING),
            "reference": _nullable(_STRING),
            "commit": _nullable(_STRING),
            "design_file": _STRING,
        }
    )
    results["properties"]["configuration"] = configuration
    results["properties"]["dependencies"] = _array(dependency)
    # The record of a node, and `best` of an exploration, are a `results.json`.
    node = _Types({RunRecord: "results"}).cls(NodeResult)
    defs = {"results": results}

    # `spec_document` fills a plain `dict` from keyword arguments.
    spec = _object(
        {
            "flow": _STRING,
            "design_name": _nullable(_STRING),
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
            "dependencies": _array(dependency),
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
            "design_name": _nullable(_STRING),
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
            "design_name": _nullable(_STRING),
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
            "description": _STRING,
            "category": _STRING,
            "supports_cocotb": _BOOLEAN,
            "experimental": _BOOLEAN,
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
        },
        optional=("experimental",),  # only a flow marked experimental has it
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
            "writes": _nullable(_STRING),
            "opaque_keys": _BOOLEAN,
            "json_schema": _OBJECT,
            "tier": _STRING,
            "group": _nullable(_STRING),
            "experimental": _BOOLEAN,
        },
        optional=("unit", "experimental"),  # an input-only setting has no unit; see the flow
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
    from xeda._internal.board import BOARD_ENTRY_KEYS

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
    # `pdk_cli` fills plain `dict`s; `pdk path` adds `platform` to the fields of `Located`.
    from xeda._internal.platforms.model import Located
    from xeda._internal.remote.environment import RemotePlatform

    pdk_list = _array(
        _object(
            {
                "platform": _STRING,
                "provider": _STRING,
                "default": _BOOLEAN,
                "version": _STRING,
                "installed": _BOOLEAN,
                "directory": _STRING,
                "problem": _STRING,
                "other_versions": _array(
                    _object({"version": _STRING, "directory": _STRING, "installed": _BOOLEAN})
                ),
            },
            # `problem`: the files name another version. `other_versions`: a Git provider only.
            optional=("problem", "other_versions"),
        )
    )
    pdk_path = _object(
        {
            "platform": _STRING,
            **{k: simple.of(v) for k, v in _class_fields(Located).items()},
        }
    )
    pdk_install = _object(
        {
            "success": {"const": True},
            "platform": _STRING,
            "provider": _STRING,
            "version": _STRING,
            "root": _STRING,
            "directory": _STRING,
            "already_installed": _BOOLEAN,
            "receipt": _nullable(_STRING),
            "commands": _array(_array(_STRING)),
        }
    )
    # `describe_host`, `_clean_host` and `cancel_launch` fill plain `dict`s. A removed entry has
    # a `kind` and a `path`; a kept entry also has the `reason`.
    removed = _object({"kind": _STRING, "path": _STRING})
    kept = _object({"kind": _STRING, "path": _STRING, "reason": _STRING})
    remote_check = _object(
        {
            "success": {"const": True},
            "host": _STRING,
            "platform": simple.cls(RemotePlatform),
            "xeda": _STRING,
            "python": _object(
                {"version": _STRING, "implementation": _STRING, "executable": _STRING}
            ),
            "environment": _object(
                {
                    "key": _STRING,
                    "code": _STRING,
                    "prefix": _STRING,
                    "lock_file": _STRING,
                    "lock": _array(_STRING),
                }
            ),
            "disk": _object({"path": _STRING, "free": _INTEGER, "total": _INTEGER}),
            "kill_user_processes": {"type": "string", "enum": ["yes", "no", "unknown"]},
            "executables": _object(
                {
                    "found": {"type": "object", "additionalProperties": _STRING},
                    "missing": _array(_STRING),
                }
            ),
            "docker": _object({"found": _BOOLEAN, "path": _nullable(_STRING)}),
            "warnings": _array(_STRING),
        }
    )
    remote_clean = _object(
        {
            "success": {"const": True},
            "host": _STRING,
            "removed": _array(removed),
            "kept": _array(kept),
        }
    )
    remote_cancel = _object(
        {
            "success": {"const": True},
            "host": _STRING,
            "launch": _STRING,
            "survivors": {"type": "integer", "const": 0},
        }
    )
    listing_of_flow = {"schema_version": _INTEGER, "flow": _STRING}
    # `failure_document` fills a plain `dict`; the error has the keys of the error of a run.
    failure = _object({"success": {"const": False}, "error": error})

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
        "pdk-list": _document("xeda pdk list --json", pdk_list),
        "pdk-path": _document("xeda pdk path --json", pdk_path),
        "pdk-install": _document("xeda pdk install --json", pdk_install),
        "remote-check": _document("xeda remote check --json", remote_check),
        "remote-clean": _document("xeda remote clean --json", remote_clean),
        "remote-cancel": _document("xeda remote cancel --json", remote_cancel),
        "failure": _document("a failed query, pdk or remote command", failure),
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


def _without_leaf(default: Any, path: list[str]) -> Any:
    """A group default without the value at the nested key `path`."""
    if not (isinstance(default, dict) and path[0] in default):
        return default
    if len(path) == 1:
        return {k: v for k, v in default.items() if k != path[0]}
    return {**default, path[0]: _without_leaf(default[path[0]], path[1:])}


def _classify_default(
    path: str, old: Any, new: Any, grouped: bool, add: list[str], brk: list[str]
) -> None:
    """A changed default breaks. The default of a settings group is a table that must keep each of
    its keys; a new key is an addition, since it comes with a new leaf. Any other table must stay
    as it is."""
    if grouped and isinstance(old, dict) and isinstance(new, dict):
        brk.extend(f"{path}.{k}: default removed" for k in sorted(old.keys() - new.keys()))
        add.extend(f"{path}.{k}: default added" for k in sorted(new.keys() - old.keys()))
        for key in sorted(old.keys() & new.keys()):
            _classify_default(f"{path}.{key}", old[key], new[key], grouped, add, brk)
    elif old != new:
        brk.append(f"{path}: default changed from {old!r} to {new!r}")


def _classify_setting(
    path: str, old: dict[str, Any], new: dict[str, Any], roots: tuple[Any, Any],
    add: list[str], brk: list[str],
) -> None:  # fmt: skip
    """Compare a setting, which Xeda reads: its `type` is display text and is not judged."""
    if old.get("unit") != new.get("unit"):
        brk.append(f"{path}: unit changed from {old.get('unit')} to {new.get('unit')}")
    if ("default" in old) != ("default" in new):
        if "default" in new:
            add.append(f"{path}: no longer required")
        else:
            brk.append(f"{path}: now required")
    elif "default" in old:
        # A settings group records no schema.
        _classify_default(path, old["default"], new["default"], "schema" not in old, add, brk)
    if ("schema" in old) != ("schema" in new):
        brk.append(f"{path}: a setting and a settings group exchanged")
    elif "schema" in old:
        _schema_visitor(add, brk)(path, old["schema"], new["schema"], roots, True, frozenset())


def _classify_requirements(
    name: str, old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare what a user must give: the required settings and the declared inputs."""
    old_names, new_names = set(old.get("required_settings", ())), set(
        new.get("required_settings", ())
    )
    brk.extend(
        f"{name}.required_settings.{n}: newly required" for n in sorted(new_names - old_names)
    )
    add.extend(
        f"{name}.required_settings.{n}: no longer required" for n in sorted(old_names - new_names)
    )
    before, after = old.get("inputs", {}), new.get("inputs", {})
    _diff_names(f"{name}.inputs", "input", before.keys(), after.keys(), add, brk)
    for key in sorted(after.keys() - before.keys()):
        if after[key]["required"]:
            add.remove(f"{name}.inputs.{key}: new input")
            brk.append(f"{name}.inputs.{key}: new required input")
    for key in sorted(before.keys() & after.keys()):
        a, b, where = before[key], after[key], f"{name}.inputs.{key}"
        brk.extend(f"{where}: kind {k} removed" for k in sorted(set(a["kinds"]) - set(b["kinds"])))
        add.extend(f"{where}: kind {k} added" for k in sorted(set(b["kinds"]) - set(a["kinds"])))
        if a["cardinality"] != b["cardinality"] and (a["cardinality"], b["cardinality"]) != (
            "one",
            "optional",
        ):
            brk.append(
                f"{where}: cardinality changed from {a['cardinality']} to {b['cardinality']}"
            )
        if a["required"] != b["required"]:
            (brk if b["required"] else add).append(
                f"{where}: now " + ("required" if b["required"] else "optional")
            )


def _classify_contract(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare the built-in flows. What the old baseline marks `experimental` is not judged: a
    flow mark covers the flow, and a setting mark covers the setting, its subtree and its part of
    the default of each enclosing group. A new mark on a stable item breaks; a removed mark adds."""
    for contract in (old, new):  # a keyword that nothing judges is an error, marked or not
        for name, flow in contract["flows"].items():
            _check_keywords(f"{name}.$defs", {"$defs": flow.get("$defs", {})})
            for key, entry in flow["settings"].items():
                _check_keywords(f"{name}.settings.{key}", entry.get("schema"))
    experimental_flows = {n for n, f in old["flows"].items() if f.get("experimental")}
    kept = {n for n in old["flows"] if n in new["flows"] or n not in experimental_flows}
    _diff_names("flows", "flow", kept, new["flows"].keys(), add, brk)
    for name in old["flows"].keys() & new["flows"].keys():
        before, after = old["flows"][name], new["flows"][name]
        if before.get("experimental") != after.get("experimental"):
            if after.get("experimental"):
                brk.append(f"flows.{name}: newly marked experimental")
            else:
                add.append(f"flows.{name}: no longer experimental")
        if before.get("experimental"):
            continue
        for group in ("settings", "outputs", "metrics"):
            a, b = before[group], after[group]
            if group == "settings":
                marked = [k for k, v in a.items() if v.get("experimental")]
                for key in a.keys() & b.keys():
                    if any(key.startswith(mark + ".") for mark in marked):
                        continue  # the subtree of an old mark is not judged
                    if bool(a[key].get("experimental")) != bool(b[key].get("experimental")):
                        if b[key].get("experimental"):
                            brk.append(f"{name}.{group}.{key}: newly marked experimental")
                        else:
                            add.append(f"{name}.{group}.{key}: no longer experimental")
                a, b = dict(a), dict(b)
                for mark in marked:
                    for table in (a, b):
                        for key in [k for k in table if k == mark or k.startswith(mark + ".")]:
                            del table[key]
                        for key in table:
                            if mark.startswith(key + "."):
                                table[key] = {
                                    **table[key],
                                    "default": _without_leaf(
                                        table[key].get("default"), mark[len(key) + 1 :].split(".")
                                    ),
                                }
                for table in (a, b):  # the marks themselves were compared above
                    for key in table:
                        table[key] = {k: v for k, v in table[key].items() if k != "experimental"}
            _diff_names(f"{name}.{group}", group, a.keys(), b.keys(), add, brk)
            if group == "settings":
                for key in b.keys() - a.keys():
                    if "default" not in b[key]:
                        add.remove(f"{name}.{group}.{key}: new {group}")
                        brk.append(f"{name}.{group}.{key}: new required setting")
            for key in a.keys() & b.keys():
                if group == "settings":
                    _classify_setting(
                        f"{name}.{group}.{key}", a[key], b[key], (before, after), add, brk
                    )
                elif a[key] != b[key]:
                    brk.append(f"{name}.{group}.{key}: changed from {a[key]} to {b[key]}")
        _classify_requirements(name, before, after, add, brk)
        _diff_names(name, "artifact", set(before["artifacts"]), set(after["artifacts"]), add, brk)
    _diff_names("codes", "category", old["codes"].keys(), new["codes"].keys(), add, brk)
    for name in old["codes"].keys() & new["codes"].keys():
        _diff_names(name, "code", set(old["codes"][name]), set(new["codes"][name]), add, brk)


def _classify_set(path: str, what: str, old: Any, new: Any, add: list[str], brk: list[str]) -> None:
    """A user of the old set keeps working: a removed member breaks, an added one is additive."""
    brk.extend(f"{path}: {what} {m!r} removed" for m in sorted(set(old or ()) - set(new or ())))
    add.extend(f"{path}: {what} {m!r} added" for m in sorted(set(new or ()) - set(old or ())))


def _range_bound(bound: list[Any], low: bool) -> tuple[float, bool]:
    """The tightness of a range bound (see `_tightness`): the value, then whether it is open."""
    value, is_open = bound
    if value is None:
        return float("-inf"), False
    return (value if low else -value), is_open


def _classify_parameter(
    path: str, old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare an option or an argument, which a user gives to Xeda. `hidden` is not judged."""
    for key in ("names", "environment", "choices"):
        _classify_set(path, key, old.get(key), new.get(key), add, brk)
    if old["required"] != new["required"]:
        (brk if new["required"] else add).append(f"{path}: required changed")
    if old.get("range") != new.get("range"):
        if old.get("range") and new.get("range"):
            lows = [_range_bound([r[0], r[2]], True) for r in (old["range"], new["range"])]
            highs = [_range_bound([r[1], r[3]], False) for r in (old["range"], new["range"])]
            (brk if lows[1] > lows[0] or highs[1] > highs[0] else add).append(
                f"{path}: range changed"
            )
        else:
            brk.append(f"{path}: range changed")
    for key in sorted(old.keys() | new.keys()):
        if key not in ("names", "environment", "choices", "required", "range", "hidden"):
            if old.get(key) != new.get(key):
                brk.append(f"{path}: {key} changed from {old.get(key)!r} to {new.get(key)!r}")


def _classify_arguments(
    path: str, old: list[dict[str, Any]], new: list[dict[str, Any]], add: list[str], brk: list[str]
) -> None:
    """A removed, reordered or changed argument breaks. A new one is additive when it is optional
    and last, and no variadic argument comes before it."""
    for index, (before, after) in enumerate(zip(old, new)):
        if before["name"] != after["name"]:
            brk.append(f"{path}.arguments[{index}]: {before['name']} replaced by {after['name']}")
        else:
            _classify_parameter(f"{path}.{before['name']}", before, after, add, brk)
    brk.extend(f"{path}.{a['name']}: argument removed" for a in old[len(new) :])
    for extra in new[len(old) :]:
        fits = not extra["required"] and not any(a["nargs"] == -1 for a in old)
        (add if fits else brk).append(f"{path}.{extra['name']}: new argument")


def _classify_behavior(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    for key in old.keys() | new.keys():
        if key not in ("cli", "project_keys") and old.get(key) != new.get(key):
            brk.append(f"{key}: changed from {old.get(key)} to {new.get(key)}")
    # A project file is input: a new key is additive, a removed one breaks.
    keys = [set(snapshot.get("project_keys", ())) for snapshot in (old, new)]
    _diff_names("project", "key", keys[0], keys[1], add, brk)
    a, b = old["cli"], new["cli"]
    _diff_names("cli", "command", a.keys(), b.keys(), add, brk)
    for command in a.keys() & b.keys():
        before, after = a[command]["options"], b[command]["options"]
        _diff_names(command, "option", before.keys(), after.keys(), add, brk)
        for option in sorted(after.keys() - before.keys()):
            if after[option]["required"]:  # a new demand on every caller
                add.remove(f"{command}.{option}: new option")
                brk.append(f"{command}.{option}: new required option")
        for option in before.keys() & after.keys():
            _classify_parameter(f"{command}.{option}", before[option], after[option], add, brk)
        _classify_arguments(command, a[command]["arguments"], b[command]["arguments"], add, brk)


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


def _branches(node: dict[str, Any], spread: bool) -> list[Any]:
    """The alternatives of a schema. With `spread`, the keywords beside the combinator join each."""
    for key in ("anyOf", "oneOf"):
        if key in node:
            rest = _without_branches(node) if spread else {}
            return [{**rest, **b} if isinstance(b, dict) else b for b in node[key]]
    return [node]


def _has_branches(node: dict[str, Any]) -> bool:
    return "anyOf" in node or "oneOf" in node


def _without_branches(node: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if key not in ("anyOf", "oneOf")}


_JSON_NAMES = {
    bool: "boolean", int: "integer", float: "number", str: "string", type(None): "null",
    list: "array", dict: "object",
}  # fmt: skip


def _value_type(value: Any) -> str:
    """The narrowest JSON type of a value: `1.0` is an `integer`."""
    if isinstance(value, float) and value.is_integer():
        return "integer"
    return _JSON_NAMES[type(value)]


def _accepted_types(node: dict[str, Any]) -> frozenset[str] | None:
    """The JSON types that a schema accepts: its `type`, else the types of its `enum` or `const`
    values. `None` means any."""
    if "type" not in node and ("enum" in node or "const" in node):
        values = node["enum"] if "enum" in node else [node["const"]]
        return frozenset(_value_type(v) for v in values)
    return _type_set(node)


def _branch_types(node: dict[str, Any]) -> frozenset[str] | None:
    """What pairs a branch of an old schema with one of a new schema: its types, with `integer`
    as `number`."""
    kind = _accepted_types(node)
    return None if kind is None else frozenset("number" if t == "integer" else t for t in kind)


def _covers(wide: frozenset[str] | None, narrow: frozenset[str] | None) -> bool:
    """Whether the types `wide` accept every value of the types `narrow`; `None` is any."""
    return wide is None or (narrow is not None and _with_integers(narrow) <= _with_integers(wide))


def _canonical(value: Any) -> Any:
    """A JSON value as a hashable that compares as JSON does: `1` equals `1.0`, `true` is no `1`."""
    if isinstance(value, (bool, str)) or value is None:
        return type(value).__name__, value
    if isinstance(value, (int, float)):
        return "number", fractions.Fraction(str(value))
    if isinstance(value, list):
        return "array", tuple(_canonical(v) for v in value)
    return "object", frozenset((k, _canonical(v)) for k, v in value.items())


def _enum_values(node: dict[str, Any]) -> frozenset[Any] | None:
    """The values that `enum` and `const` allow together, or `None` for a node with neither."""
    found = [frozenset(_canonical(v) for v in node["enum"])] if "enum" in node else []
    if "const" in node:
        found.append(frozenset([_canonical(node["const"])]))
    return frozenset.intersection(*found) if found else None


def _may_match(pattern: str, name: str) -> bool:
    """Whether a `patternProperties` regular expression may match the key `name`."""
    try:
        return re.search(pattern, name) is not None
    except re.error:
        return True


#: Keywords that describe a schema and do not change what it accepts.
_SCHEMA_METADATA = frozenset(
    ("title", "description", "examples", "$schema", "$id", "$defs", "deprecated", "readOnly",
     "writeOnly", "$comment", "hidden_from_schema")
)  # fmt: skip
#: Each group is one bound; the value says if the endpoint is exclusive.
_SCHEMA_BOUNDS: tuple[dict[str, bool], ...] = (
    {"minimum": False, "exclusiveMinimum": True},
    {"maximum": False, "exclusiveMaximum": True},
    {"minLength": False}, {"maxLength": False}, {"minItems": False}, {"maxItems": False},
    {"minProperties": False}, {"maxProperties": False},
)  # fmt: skip


#: Keywords that `_classify_schema` judges. Any other keyword is an error of the tool.
_SCHEMA_JUDGED = frozenset(
    ("type", "enum", "const", "anyOf", "oneOf", "properties", "required", "items", "prefixItems",
     "additionalProperties", "patternProperties", "propertyNames", "default", "pattern", "format",
     "multipleOf", "uniqueItems")
)  # fmt: skip
#: Keywords with no order: any change breaks. `schema_version` is the version of the design file.
_SCHEMA_OPAQUE = ("not", "schema_version")
_SCHEMA_JUDGED |= set(_SCHEMA_OPAQUE)
_SCHEMA_JUDGED |= {key for group in _SCHEMA_BOUNDS for key in group}
#: The `x-xeda-*` keys that say what a value means (its unit, the file it names): any change breaks.
_SCHEMA_MEANING = (
    "x-xeda-unit", "x-xeda-quantity", "x-xeda-measure", "x-xeda-writes", "x-xeda-output",
    "x-xeda-always", "x-xeda-waveform", "x-xeda-locator", "x-xeda-inline-prefix",
)  # fmt: skip
#: The `x-xeda-*` keys that are not judged: `tier` and `input-only` only sort a listing, and the
#: built-in contract judges the mark `experimental` on the entry of the setting.
_SCHEMA_LISTING = ("x-xeda-tier", "x-xeda-input-only", "x-xeda-experimental")
#: Judged as `additionalProperties: false` (see `_closed_extra`): the design file closes the object.
_SCHEMA_CLOSED = "x-xeda-closed"
_SCHEMA_KNOWN = (
    _SCHEMA_JUDGED | _SCHEMA_METADATA | {"$ref", _SCHEMA_CLOSED, *_SCHEMA_MEANING, *_SCHEMA_LISTING}
)
#: The keywords whose value is a schema, a list of schemas or a table of schemas.
_SUBSCHEMA = ("items", "additionalProperties", "propertyNames", "not")
_SUBSCHEMAS = ("anyOf", "oneOf", "prefixItems")
_SUBSCHEMA_TABLES = ("properties", "patternProperties", "$defs")


def _check_keywords(path: str, node: Any) -> None:
    """Raise `ValueError` for a keyword anywhere in the schema `node` that nothing judges. Another
    tool's `x-*` key is metadata, and an `x-xeda-*` key must be listed above."""
    if not isinstance(node, dict):
        return
    for key in node:
        if key not in _SCHEMA_KNOWN and (not key.startswith("x-") or key.startswith("x-xeda-")):
            raise ValueError(f"{path}: the schema keyword {key!r} is not compared")
    for key in _SUBSCHEMA:
        _check_keywords(f"{path}.{key}", node.get(key))
    for key in _SUBSCHEMAS:
        for index, item in enumerate(node.get(key, ())):
            _check_keywords(f"{path}.{key}[{index}]", item)
    for key in _SUBSCHEMA_TABLES:
        for name, item in node.get(key, {}).items():
            _check_keywords(f"{path}.{key}.{name}", item)


def _with_integers(types: frozenset[str]) -> frozenset[str]:
    """The types with `integer`, if `number` is among them: a number holds every integer."""
    return types | {"integer"} if "number" in types else types


def _openness(extra: Any) -> int:
    """What `additionalProperties` accepts: 2 any key, 1 the keys that a schema accepts, 0 none."""
    return 0 if extra is False else 1 if isinstance(extra, dict) and extra else 2


def _extra_keys(node: dict[str, Any]) -> Any:
    """The `additionalProperties` of an object: `false` for an object that the loader closes."""
    extra = node.get("additionalProperties")
    return False if extra is None and node.get(_SCHEMA_CLOSED) else extra


def _tightness(node: dict[str, Any], group: dict[str, bool]) -> tuple[float, bool]:
    """How tight a bound is: a larger result is tighter. No bound is the loosest."""
    sign = -1 if next(iter(group)).startswith("max") else 1
    found = [(sign * node[key], exclusive) for key, exclusive in group.items() if key in node]
    return max(found, default=(float("-inf"), False))


def _schema_visitor(add: list[str], brk: list[str]) -> Callable[..., None]:
    """The visitor `visit(path, before, after, roots, reader, seen)` of two schemas that records
    the differences in `add` and `brk`. `roots` are the documents that a `$ref` resolves against;
    `reader` is true for an input, which Xeda reads. An object that says nothing about other keys
    (`additionalProperties` absent) is open, unless it has the mark `x-xeda-closed`: the loader of
    the design file refuses a key that no property names, and `design_schema` records that."""

    def judge(path: str, message: str, widened: bool, reader: bool) -> None:
        """Input accepts more or output promises more: additive. The reverse breaks."""
        (add if widened == reader else brk).append(f"{path}: {message}")

    def keywords(
        path: str, before: dict[str, Any], after: dict[str, Any], roots: tuple[Any, Any],
        reader: bool, seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        for group in _SCHEMA_BOUNDS:
            old_bound, new_bound = _tightness(before, group), _tightness(after, group)
            if old_bound != new_bound:
                judge(path, f"{'/'.join(group)} changed", new_bound < old_bound, reader)
        old_step, new_step = before.get("multipleOf"), after.get("multipleOf")
        if old_step != new_step:
            if old_step is None or new_step is None:
                judge(path, "multipleOf changed", new_step is None, reader)
            else:
                ratio = fractions.Fraction(str(old_step)) / fractions.Fraction(str(new_step))
                if ratio != 1:
                    if ratio.denominator == 1:
                        judge(path, "multipleOf changed", True, reader)
                    elif ratio.numerator == 1:
                        judge(path, "multipleOf changed", False, reader)
                    else:  # neither divides the other
                        brk.append(f"{path}: multipleOf changed")
        if bool(before.get("uniqueItems")) != bool(after.get("uniqueItems")):
            judge(path, "uniqueItems changed", not after.get("uniqueItems"), reader)
        for key in ("pattern", "format"):
            old_value, new_value = before.get(key), after.get(key)
            if old_value != new_value:
                dropped = new_value is None and reader
                added = old_value is None and not reader
                (add if dropped or added else brk).append(f"{path}: {key} changed")
        if "default" in before and "default" in after and before["default"] != after["default"]:
            brk.append(f"{path}: default changed")
        for key in (*_SCHEMA_OPAQUE, *_SCHEMA_MEANING):
            if before.get(key) != after.get(key):
                brk.append(f"{path}: {key} changed")
        old_names, new_names = before.get("propertyNames"), after.get("propertyNames")
        if old_names is not None or new_names is not None:
            visit(
                f"{path}.names", True if old_names is None else old_names,
                True if new_names is None else new_names, roots, reader, seen,
            )  # fmt: skip

    def visit(
        path: str, before: Any, after: Any, roots: tuple[Any, Any], reader: bool,
        seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        if isinstance(before, bool) or isinstance(after, bool):  # `true` is `{}`, `false` nothing
            before, after = ({} if node is True else node for node in (before, after))
            if before == after:
                return
            if before is False or after is False:
                judge(path, "schema changed", before is False, reader)
                return
        if not (isinstance(before, dict) and isinstance(after, dict)):
            if before != after:
                brk.append(f"{path}: schema changed")
            return
        pair = (str(before.get("$ref")), str(after.get("$ref")))
        if pair != ("None", "None"):
            if pair in seen:  # a recursive schema
                return
            seen = seen | {pair}
        before, after = _resolve(before, roots[0]), _resolve(after, roots[1])
        old_has, new_has = _has_branches(before), _has_branches(after)
        if old_has and new_has and (("anyOf" in before) != ("anyOf" in after)):
            brk.append(f"{path}: anyOf and oneOf exchanged")
        if old_has or new_has:
            spread = old_has != new_has  # the keywords beside one combinator hold for its branches
            alternatives(
                path, _branches(before, spread), _branches(after, spread), roots, reader, seen,
                "oneOf" in before or "oneOf" in after,
            )  # fmt: skip
            if spread:
                return
            # The keywords beside `anyOf` or `oneOf` hold for every branch: compare them too.
            before, after = _without_branches(before), _without_branches(after)
        keywords(path, before, after, roots, reader, seen)
        old_types, new_types = _accepted_types(before), _accepted_types(after)
        if old_types != new_types:
            wider, narrower = _covers(new_types, old_types), _covers(old_types, new_types)
            if wider != narrower:  # a narrower output type keeps the promise of the old one
                judge(path, "type changed", wider, reader)
            elif not wider:
                brk.append(f"{path}: type changed")
        old_enum, new_enum = _enum_values(before), _enum_values(after)
        if old_enum != new_enum:
            grew = new_enum is None or (old_enum is not None and old_enum < new_enum)
            # A document holds closed sets (an outcome, a disposition): a new member breaks a
            # reader.
            (add if grew and reader else brk).append(
                f"{path}: enum {'widened' if grew else 'narrowed'}"
            )
        properties(path, before, after, roots, reader, seen)
        if "items" in before or "items" in after:
            old_items, new_items = before.get("items", True), after.get("items", True)
            visit(f"{path}.items", old_items, new_items, roots, reader, seen)
        old_tuple, new_tuple = before.get("prefixItems", []), after.get("prefixItems", [])
        if len(old_tuple) != len(new_tuple):
            brk.append(f"{path}: tuple length changed")
        for index, (a, b) in enumerate(zip(old_tuple, new_tuple)):
            visit(f"{path}.prefixItems[{index}]", a, b, roots, reader, seen)

    def alternatives(
        path: str, left: list[Any], right: list[Any], roots: tuple[Any, Any], reader: bool,
        seen: frozenset[tuple[str, str]], exclusive: bool,
    ) -> None:  # fmt: skip
        """Pair identical alternatives first, then each old one with the new one of the same JSON
        types (else one that holds more or less), and compare the pairs. In a `oneOf`, branches
        that overlap, before or after, make a value match twice and so invalid: a change that
        adds there breaks."""

        def kinds(branches: list[Any], root: Any) -> list[frozenset[str] | None]:
            return [
                _branch_types(_resolve(b, root) if isinstance(b, dict) else {}) for b in branches
            ]

        def name(kind: frozenset[str] | None) -> str:
            return "any" if kind is None else "|".join(sorted(kind))

        mark = len(add)
        old_kinds, new_kinds = kinds(left, roots[0]), kinds(right, roots[1])
        used: set[int] = set()
        text = [json.dumps(b, sort_keys=True) for b in (*left, *right)]
        same = {}  # identical alternatives pair first, whatever their order
        for i, old_text in enumerate(text[: len(left)]):
            match = next(
                (
                    j
                    for j, new_text in enumerate(text[len(left) :])
                    if new_text == old_text and j not in used
                ),
                None,
            )
            if match is not None:
                used.add(match)
                same[i] = match
        for i, (branch, kind) in enumerate(zip(left, old_kinds)):
            match = same.get(i)
            if match is not None:
                visit(path, branch, right[match], roots, reader, seen)
                continue
            match = next((j for j, k in enumerate(new_kinds) if k == kind and j not in used), None)
            if match is None:
                match = next(
                    (
                        j
                        for j, k in enumerate(new_kinds)
                        if j not in used and (_covers(k, kind) or _covers(kind, k))
                    ),
                    None,
                )
            if match is None:
                judge(path, f"alternative {name(kind)} removed", False, reader)
                continue
            used.add(match)
            visit(path, branch, right[match], roots, reader, seen)
        for index, kind in enumerate(new_kinds):
            if index not in used:
                judge(path, f"alternative {name(kind)} added", True, reader)
        overlap = any(
            a is None or b is None or a & b
            for kinds_ in (old_kinds, new_kinds)
            for a, b in itertools.combinations(kinds_, 2)
        )
        if exclusive and overlap:  # a value may have matched two branches before or after
            brk.extend(add[mark:])
            del add[mark:]

    def properties(
        path: str, before: dict[str, Any], after: dict[str, Any], roots: tuple[Any, Any],
        reader: bool, seen: frozenset[tuple[str, str]],
    ) -> None:  # fmt: skip
        old_props, new_props = before.get("properties", {}), after.get("properties", {})
        old_patterns = before.get("patternProperties", {})
        new_patterns = after.get("patternProperties", {})
        old_extra, new_extra = _extra_keys(before), _extra_keys(after)

        def everything(node: Any) -> bool:
            node = _resolve(node, roots[1]) if isinstance(node, dict) else node
            return node is True or (
                isinstance(node, dict)
                and all(k in _SCHEMA_METADATA or k == "default" or k.startswith("x-") for k in node)
            )

        def appears(place: str, schema: Any, name: str, is_pattern: bool) -> None:
            """A new property or pattern of an input accepts less, unless it accepts everything or
            the keys that it names were refused. Where `additionalProperties` was a schema, compare
            it with the new one. Another pattern or property may hold the same keys: not provable.
            """
            what = "new pattern property" if is_pattern else "new property"
            if is_pattern:
                shared = bool(old_patterns) or any(_may_match(name, key) for key in old_props)
            else:
                shared = any(_may_match(pattern, name) for pattern in old_patterns)
            fallback = _openness(old_extra)
            if everything(schema) or (fallback == 0 and not shared):
                add.append(f"{place}: {what}")
            elif fallback == 1 and not shared:
                mark = len(brk)
                visit(place, old_extra, schema, roots, True, seen)
                (brk if len(brk) > mark else add).append(f"{place}: {what}")
            else:
                brk.append(f"{place}: {what} narrows the accepted keys")

        def vanishes(place: str, schema: Any, name: str, is_pattern: bool) -> None:
            """A removed property or pattern of an input hands its keys to a pattern or to
            `additionalProperties`: it accepts more if that takes them, and less if it refuses."""
            what = "pattern property" if is_pattern else "property"
            fallback = _openness(new_extra)
            if fallback == 2 or (not is_pattern and any(_may_match(p, name) for p in new_patterns)):
                add.append(f"{place}: {what} removed")
            elif fallback == 1:
                mark = len(brk)
                visit(place, schema, new_extra, roots, True, seen)
                (brk if len(brk) > mark else add).append(f"{place}: {what} removed")
            else:
                brk.append(f"{place}: {what} removed")

        # An output reader ignores a new key and needs an old one.
        for key in sorted(old_props.keys() - new_props.keys()):
            if reader:
                vanishes(f"{path}.{key}", old_props[key], key, False)
            else:
                brk.append(f"{path}.{key}: property removed")
        for key in sorted(new_props.keys() - old_props.keys()):
            if reader:
                appears(f"{path}.{key}", new_props[key], key, False)
            else:
                add.append(f"{path}.{key}: new property")
        for pattern in sorted(old_patterns.keys() - new_patterns.keys()):
            if reader:
                vanishes(f"{path}.{pattern}", old_patterns[pattern], pattern, True)
            else:
                brk.append(f"{path}.{pattern}: pattern property removed")
        for pattern in sorted(new_patterns.keys() - old_patterns.keys()):
            if reader:
                appears(f"{path}.{pattern}", new_patterns[pattern], pattern, True)
            else:
                add.append(f"{path}.{pattern}: new pattern property")
        old_required, new_required = set(before.get("required", ())), set(after.get("required", ()))
        for key in sorted(old_required ^ new_required):  # a required name needs no property
            now_required = key in new_required
            # A reader of a written document needs the key: it must not become optional.
            # Xeda, as the reader of the design file, must not get a new demand.
            breaking = now_required if reader else not now_required
            fresh = now_required and key in new_props.keys() - old_props.keys()
            state = (
                "new required property"
                if fresh
                else f"now {'required' if now_required else 'optional'}"
            )
            (brk if breaking else add).append(f"{path}.{key}: {state}")
        for key in sorted(old_props.keys() & new_props.keys()):
            visit(f"{path}.{key}", old_props[key], new_props[key], roots, reader, seen)
        for pattern in sorted(old_patterns.keys() & new_patterns.keys()):
            visit(
                f"{path}.{pattern}", old_patterns[pattern], new_patterns[pattern], roots, reader, seen
            )  # fmt: skip
        # From the most open to the most closed: no schema (or `true`), a schema, `false`.
        old_open, new_open = _openness(old_extra), _openness(new_extra)
        if old_open != new_open:
            # A reader of a document ignores keys it does not know, as for a new property: opening
            # a closed output object adds. Opening a schema-valued map breaks a reader of its values.
            if old_open == 0 and not reader:
                add.append(f"{path}: additional keys changed")
            else:
                judge(path, "additional keys changed", new_open > old_open, reader)
        elif old_open == 1:
            visit(f"{path}.*", old_extra, new_extra, roots, reader, seen)

    return visit


def _classify_schema(
    old: dict[str, Any], new: dict[str, Any], add: list[str], brk: list[str]
) -> None:
    """Compare the schemas of the documents.

    A document that Xeda writes keeps its types, its enums and its keys: a removed key, another
    type, another enum or a key that is no longer required breaks a reader of it, and a new
    optional key is an addition. The design file is what Xeda reads, so the direction reverses:
    it may accept more and never less, and a new key must be optional. A `$ref` is compared by
    what it names.

    Bounds compare by value and endpoint (`minimum` against `exclusiveMinimum` too), `multipleOf`
    by exact division, and `uniqueItems`, `const` and `additionalProperties` by what they accept.
    A looser input or a tighter output is additive; the reverse breaks. `pattern` and `format`
    have no order: any change breaks, but dropping one from an input or adding one to an output
    is additive. A changed default breaks, and so does a change of `not`, `schema_version` or the
    combinator.
    A keyword anywhere in either schema that is neither judged nor metadata raises `ValueError`: it
    would be unchecked."""
    _diff_names("schemas", "document", set(old), set(new), add, brk)
    for name, schema in [*old.items(), *new.items()]:
        _check_keywords(name, schema)

    visit = _schema_visitor(add, brk)
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
