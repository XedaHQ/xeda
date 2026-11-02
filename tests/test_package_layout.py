"""The package layout has one source (`xeda.package_paths`): no module path, file depth or source
path is written out anywhere else, so a change of the layout touches that module only."""

import ast
import importlib
import json
import re
from collections.abc import Iterator
from pathlib import Path


from xeda import package_paths
from xeda.introspect import flows_info, optimizers_info, results_info, settings_info
from xeda.package_paths import impl_module

from .source_layout import AGENT_DIR, IMPL_DIR, ROOT, SCHEMAS_DIR, SRC_XEDA

TOOLS = ROOT / "tools"
TESTS = ROOT / "tests"
#: The modules that the public surface keeps at their own path.
PUBLIC = ("errors", "introspect")


def _internal_names() -> list[str]:
    """The names of the modules and packages that are not public: next to `xeda/__init__.py`
    and in the implementation package."""
    names = set()
    for directory in (SRC_XEDA, IMPL_DIR):
        for entry in directory.iterdir():
            name = entry.stem if entry.suffix == ".py" else entry.name
            if (entry.suffix == ".py" or (entry / "__init__.py").is_file()) and not (
                name.startswith("__") or name in PUBLIC
            ):
                names.add(name)
    return sorted(names)


#: A dotted path of an internal module inside a string. A file name (`xeda.json`) and a path
#: (`~/.config/xeda.fish`) do not match. `xeda.local_settings(` is the function that `xeda`
#: exports.
#: Strings that name a module and are no path to import: the key of the verb module in the API
#: snapshot, which stays the same wherever the module is.
KEYS = {("api_baseline.py", "xeda.workspace_verbs")}
MODULE_PATH = re.compile(r"(?<![\w/.~-])xeda\.(?:" + "|".join(_internal_names()) + r")\b(?!\()")


def _python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for parent in ast.walk(tree):
        if (
            isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and parent.body
            and isinstance(parent.body[0], ast.Expr)
            and isinstance(parent.body[0].value, ast.Constant)
        ):
            found.add(id(parent.body[0].value))
    return found


def _literals(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


def test_no_literal_internal_module_path():
    found = [
        f"{path.relative_to(ROOT)}:{line}: {text!r}"
        for directory in (SRC_XEDA, TOOLS)
        for path in _python_files(directory)
        if path.name != "package_paths.py"
        for line, text in _literals(path)
        if MODULE_PATH.search(text) and (path.name, text) not in KEYS
    ]
    assert not found, "\n".join(found)


def test_no_module_path_in_the_bootstrap_script():
    text = (IMPL_DIR / "remote" / "bootstrap.sh").read_text(encoding="utf-8")
    assert not re.findall(r"-m xeda\.", text)


def _imported_modules(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def test_no_absolute_internal_import_in_product_code():
    allowed = {"xeda", *(f"xeda.{name}" for name in PUBLIC)}
    found = []
    for path in _python_files(SRC_XEDA):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for line, module in _imported_modules(tree):
            if (module == "xeda" or module.startswith("xeda.")) and module not in allowed:
                found.append(f"{path.relative_to(ROOT)}:{line}: {module}")
    assert not found, "\n".join(found)


def test_no_source_layout_path_outside_one_helper():
    # The patterns are built here, so that this file does not match them.
    patterns = ['"src" ' + '/ "xeda"', "src" + "/xeda/", '"src", ' + '"xeda"']
    found = []
    for directory in (TESTS, TOOLS):
        for path in _python_files(directory):
            if path.name == "source_layout.py":
                continue
            text = path.read_text(encoding="utf-8")
            found += [f"{path.relative_to(ROOT)}: {p}" for p in patterns if p in text]
    assert not found, "\n".join(found)


def _mentions_file(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(node))


def test_no_file_depth_arithmetic():
    found = []
    for path in _python_files(SRC_XEDA):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            deep = (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and _mentions_file(node.value.value)
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr == "parent"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parent"
                and _mentions_file(node.value.value)
            )
            if deep:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not found, "\n".join(found)


def test_remote_module_names_come_from_one_place():
    from xeda.remote.provision import boot_script

    assert "@XEDA_AGENT_MODULE@" not in boot_script()
    assert f"-m {impl_module('remote.agent')}" in boot_script()


def test_the_directories_agree_with_the_installed_modules():
    import xeda

    assert package_paths.package_root() == Path(xeda.__file__).absolute().parent
    assert package_paths.source_root() == package_paths.package_root().parent
    assert package_paths.impl_root().resolve() == IMPL_DIR.resolve()
    assert (package_paths.impl_root() / "package_paths.py").is_file()
    assert importlib.import_module(impl_module("flows")).__file__
    assert (package_paths.schemas_dir() / "design.schema.json").is_file()
    assert package_paths.schemas_dir().resolve() == SCHEMAS_DIR.resolve()
    assert (AGENT_DIR / "SKILL.md").is_file()


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


PYTHON_NAMES = ("class", "module", "qualified_name", "settings_class", "declared_by")


def test_no_document_names_a_python_module():
    documents: list[tuple[str, object]] = [("list-flows", flows_info())]
    documents.append(("list-optimizers", optimizers_info()))
    names = [flow["name"] for flow in flows_info()]
    for name in names:
        documents.append((f"list-settings {name}", settings_info(name)))
        documents.append((f"list-results {name}", results_info(name)))
    found = []
    for label, document in documents:
        text = json.dumps(document, default=str)
        found += [
            f"{label}: {value!r}"
            for value in _strings(json.loads(text))
            if value.startswith("xeda.")
        ]
    assert not found, "\n".join(found[:20])
    for entry in [*flows_info(), *optimizers_info()]:
        assert not set(PYTHON_NAMES) & set(entry), entry["name"]
    for name in names:
        info = settings_info(name)
        assert "settings_class" not in info
        assert all("declared_by" not in field for field in info["fields"])
