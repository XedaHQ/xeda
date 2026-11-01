"""The public surface is small, and the documents and examples stay inside it.

`xeda`, `xeda.errors` and `xeda.introspect` are the public modules. The launcher classes, the
flow base classes and the tool wrapper are internal. A snippet in the documents, the agent skill
or an example that imports anything else teaches a name that can change.
"""

import ast
import collections.abc
import enum
import importlib
import inspect
import pathlib
import re
import sys
import textwrap
import types
import typing
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import pytest

import xeda

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULES = ("xeda", "xeda.errors", "xeda.introspect")
RETIRED = {
    "DefaultRunner",
    "FlowRunner",
    "FlowLauncher",
    "Dse",
    "Flow",
    "SimFlow",
    "SynthFlow",
    "Tool",
    "FPGA",
    "Cocotb",
}
DOCUMENTS = [
    *sorted((ROOT / "docs").glob("*.rst")),
    ROOT / ".claude/skills/xeda/SKILL.md",
    ROOT / "src/xeda/data/agent/SKILL.md",
]
EXAMPLES = sorted((ROOT / "examples").rglob("*.py"))

RST_BLOCK = re.compile(r"^(?P<indent> *)\.\. code-block:: *python3? *$")
MD_FENCE = re.compile(r"^```(?:python3?|py)\s*$")


def exports(module: str) -> set[str]:
    return set(importlib.import_module(module).__all__)


def python_blocks(path: Path) -> list[tuple[int, str]]:
    """Return the Python code blocks of a document as (first line number, source)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        rst = RST_BLOCK.match(lines[index])
        if path.suffix == ".rst" and rst:
            indent = len(rst["indent"])
            start = index + 1
            end = start
            while end < len(lines) and (
                not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
            ):
                end += 1
            blocks.append((start + 1, textwrap.dedent("\n".join(lines[start:end]))))
            index = end
        elif path.suffix == ".md" and MD_FENCE.match(lines[index]):
            start = index + 1
            end = start
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            blocks.append((start + 1, "\n".join(lines[start:end])))
            index = end
        else:
            index += 1
    return blocks


def problems(tree: ast.AST) -> list[str]:
    """Return what the code imports or reads from `xeda` that is not public."""
    found: list[str] = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] != "xeda":
                    continue
                if item.name not in PUBLIC_MODULES:
                    found.append(f"import {item.name}")
                else:
                    aliases[item.asname or item.name.split(".")[0]] = (
                        item.name if item.asname else "xeda"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] != "xeda":
                continue
            if node.module not in PUBLIC_MODULES:
                found.append(f"from {node.module} import ...")
                continue
            found += [
                f"from {node.module} import {item.name}"
                for item in node.names
                if item.name not in exports(node.module)
            ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        path = _module_path(node, aliases)
        if path is None:
            continue
        # The longest public module the path starts with, and the name read from it.
        size = max(n for n in range(1, len(path) + 1) if ".".join(path[:n]) in PUBLIC_MODULES)
        if size < len(path) and path[size] not in exports(".".join(path[:size])):
            name = ".".join(path[: size + 1])
            if name not in found:
                found.append(name)
    return found


def _module_path(node: ast.Attribute, aliases: dict[str, str]) -> list[str] | None:
    """Return `node` as the dotted path of what it reads, starting at the module that its name
    is bound to, or `None` when the name is no `xeda` import."""
    parts: list[str] = []
    value: ast.expr = node
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name) or value.id not in aliases:
        return None
    return [*aliases[value.id].split("."), *reversed(parts)]


def test_the_old_entry_points_are_not_exported() -> None:
    assert not RETIRED & set(dir(xeda))
    assert not RETIRED & set(xeda.__all__)


def test_the_package_exports_exist() -> None:
    assert [name for name in xeda.__all__ if not hasattr(xeda, name)] == []


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_documents_import_only_public_names(path: Path) -> None:
    for line, source in python_blocks(path):
        assert problems(ast.parse(source)) == [], f"{path.relative_to(ROOT)}:{line}"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: str(path.relative_to(ROOT)))
def test_examples_import_only_public_names(path: Path) -> None:
    assert problems(ast.parse(path.read_text(encoding="utf-8"))) == []


def test_the_checker_finds_a_name_that_is_not_public() -> None:
    tree = ast.parse(
        "import xeda\nfrom xeda import DefaultRunner\nfrom xeda.flows import GhdlSim\n"
        "xeda.Tool\nfrom xeda.errors import SpecError\n"
        "import xeda.introspect\nxeda.introspect.Flow\nxeda.introspect.flows_info()\n"
        "import xeda.introspect as xi\nxi.Tool\nxeda.Workspace.run\n"
    )
    assert sorted(problems(tree)) == [
        "from xeda import DefaultRunner",
        "from xeda.flows import ...",
        "xeda.Tool",
        "xeda.introspect.Flow",
        "xeda.introspect.Tool",
    ]


# --- The annotations of the public names -------------------------------------------------------

STANDARD_MODULES = {
    "builtins",
    "typing",
    "typing_extensions",
    "collections.abc",
    "os",
    "pathlib",
    "datetime",
    "enum",
    "dataclasses",
    "types",
}


def _public_objects() -> dict[str, object]:
    """Return every public name of the three public modules and `xeda.workspace_verbs`."""
    found: dict[str, object] = {}
    for module in (*PUBLIC_MODULES, "xeda.workspace_verbs"):
        loaded = importlib.import_module(module)
        names = loaded.__all__ if module != "xeda.workspace_verbs" else _VERB_NAMES
        found.update({f"{module}.{name}": getattr(loaded, name) for name in names})
    return found


_VERB_NAMES = ("plan", "run", "sweep", "load", "records", "scrub")
PUBLIC_OBJECTS = _public_objects()
PUBLIC_NAMES = sorted(PUBLIC_OBJECTS)
_PUBLIC_IDS = {id(value) for value in PUBLIC_OBJECTS.values()}
# The names that a string annotation of a public module may use under `TYPE_CHECKING`.
_NAMESPACE = {
    name: value
    for qualified, value in PUBLIC_OBJECTS.items()
    for name in [qualified.rpartition(".")[2]]
}


try:
    import pandas as _pandas
except ImportError:  # pandas is optional: a stand-in with the same module name
    _frame = type("DataFrame", (), {"__module__": "pandas.core.frame"})
    _pandas = types.SimpleNamespace(DataFrame=_frame)  # type: ignore[assignment]


def _leaves(annotation: object) -> list[object]:
    """Return the classes and aliases that an annotation names, without the arguments of `Literal`."""
    if annotation is None or annotation is Ellipsis or annotation is type(None):
        return []
    if isinstance(annotation, (TypeVar, ParamSpec)):
        return []
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return []
    if origin is typing.Annotated:
        return _leaves(typing.get_args(annotation)[0])
    if origin is not None:
        found = [] if origin in (typing.Union, types.UnionType) else [origin]
        for argument in typing.get_args(annotation):
            found += _leaves(list(argument) if isinstance(argument, list) else argument)
        return found
    if isinstance(annotation, (list, tuple)):
        return [leaf for item in annotation for leaf in _leaves(item)]
    return [annotation]


def _allowed(leaf: object) -> bool:
    if id(leaf) in _PUBLIC_IDS:
        return True
    module = getattr(leaf, "__module__", None)
    return (
        module in STANDARD_MODULES
        or str(module).split(".")[0] == "pandas"
        or (isinstance(leaf, type) and leaf.__module__ == "xeda.errors")
    )


def _annotation_problems_of(owner: str, hints: dict[str, object]) -> list[str]:
    return [
        f"{owner} `{name}` names {getattr(leaf, '__module__', '?')}.{getattr(leaf, '__qualname__', leaf)}"
        for name, annotation in hints.items()
        for leaf in _leaves(annotation)
        if not _allowed(leaf)
    ]


def _hints(target: object) -> dict[str, object]:
    """Return the public annotations that `target` itself declares, each one evaluated alone.

    A name that is not defined (a private type imported under `TYPE_CHECKING`) evaluates to a
    marker that `_allowed` refuses."""
    namespace = {**_NAMESPACE, "pandas": _pandas}
    module = sys.modules.get(getattr(target, "__module__", ""), None)
    scope = {**vars(module), **namespace} if module else namespace
    if hasattr(inspect, "get_annotations") and sys.version_info >= (3, 14):
        import annotationlib

        raw = inspect.get_annotations(target, format=annotationlib.Format.FORWARDREF)  # type: ignore[arg-type]
    else:
        raw = inspect.get_annotations(target)
    found: dict[str, object] = {}
    for name, hint in raw.items():
        if name.startswith("_") or name in ("self", "cls"):
            continue
        text = hint if isinstance(hint, str) else getattr(hint, "__forward_arg__", None)
        while text is not None:
            try:
                hint = eval(text, scope)  # noqa: S307 - the annotations of the package itself
            except NameError as error:
                hint = _Undefined(str(error))
            text = hint if isinstance(hint, str) else getattr(hint, "__forward_arg__", None)
        found[name] = hint
    return found


class _Undefined:
    def __init__(self, text: str) -> None:
        self.__module__ = "undefined"
        self.__qualname__ = text


def annotation_problems(obj: Any) -> list[str]:
    """Return the annotations of a public name that name something not public.

    A public signature, return or attribute annotation may name a public name, a builtin, or a
    type of `typing`, `collections.abc`, `os`, `pathlib`, `datetime`, `enum`, `dataclasses`
    or `types`. An empty list means the annotations are fine."""
    found: list[str] = []
    qualname = getattr(obj, "__qualname__", repr(obj))
    if not inspect.isclass(obj):
        return _annotation_problems_of(qualname, _hints(obj)) if callable(obj) else []
    if not issubclass(obj, enum.Enum):
        for base in obj.__mro__:
            if base.__module__.startswith("xeda"):
                found += _annotation_problems_of(qualname, _hints(base))
    for name in dir(obj):
        if name.startswith("_") and name != "__init__":
            continue
        member = inspect.getattr_static(obj, name)
        defining = next((c for c in obj.__mro__ if name in vars(c)), None)
        if defining is None or not defining.__module__.startswith("xeda"):
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        elif isinstance(member, property):
            member = member.fget
        if inspect.isfunction(member):
            found += _annotation_problems_of(f"{qualname}.{name}", _hints(member))
    return found


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_annotations_name_public_or_standard_types(name: str) -> None:
    found = set(annotation_problems(PUBLIC_OBJECTS[name]))
    assert found == set()


class _Internal:
    pass


def test_the_annotation_check_finds_an_internal_type() -> None:
    def fine(value: pathlib.Path | None = None) -> collections.abc.Mapping[str, xeda.Spec]: ...

    def bad(value: _Internal) -> list[int]: ...

    class Holder:
        __module__ = "xeda.holder"  # the check reads the classes of the package only
        field: dict[str, _Internal]

    assert annotation_problems(fine) == []
    assert len(annotation_problems(bad)) == 1
    assert len(annotation_problems(Holder)) == 1
