"""Snapshot the public API, keep release baselines, and classify the difference between them.

    python tools/api_baseline.py                  write tests/snapshots/current/ for review
    python tools/api_baseline.py --check          fail when tests/snapshots/current/ is stale
    python tools/api_baseline.py --compare        classify the code against the newest baseline
    python tools/api_baseline.py --release X.Y    write tests/baselines/X.Y/ once, with a manifest

A baseline is the public contract of one release. Only `--release` writes it, and nothing edits it
after that. A breaking difference to the newest baseline needs a new major version and
`--accept-breaking`; `BREAKING.json` then lists every such difference.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import functools
import hashlib
import importlib
import inspect
import json
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
            p.name: {"names": p.opts + p.secondary_opts, "flag": p.is_flag, "hidden": p.hidden}
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


#: The snapshot files: name, kind (for `classify`) and producer. A later snapshot is one more row.
SNAPSHOTS: dict[str, tuple[SnapshotKind, Callable[[], dict[str, Any]]]] = {
    "public_api.json": ("api", public_api_snapshot),
    "builtin.json": ("contract", builtin_contract),
    "behavior.json": ("behavior", behavior_snapshot),
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


_CLASSIFIERS: dict[str, Callable[[dict[str, Any], dict[str, Any], list[str], list[str]], None]] = {
    "api": _classify_api,
    "contract": _classify_contract,
    "behavior": _classify_behavior,
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
    if stale:
        print(f"error: {', '.join(stale)} in {current} differ from the code.", file=sys.stderr)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
