"""The public surface is small, and the documents and examples stay inside it.

`xeda`, `xeda.errors` and `xeda.introspect` are the public modules. The launcher classes, the
flow base classes and the tool wrapper are internal. A snippet in the documents, the agent skill
or an example that imports anything else teaches a name that can change.
"""

import ast
import importlib
import re
import textwrap
from pathlib import Path

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
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and aliases.get(node.value.id)
            and node.attr not in exports(aliases[node.value.id])
            and not (aliases[node.value.id] == "xeda" and f"xeda.{node.attr}" in PUBLIC_MODULES)
        ):
            found.append(f"{node.value.id}.{node.attr}")
    return found


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
    )
    assert sorted(problems(tree)) == [
        "from xeda import DefaultRunner",
        "from xeda.flows import ...",
        "xeda.Tool",
    ]
