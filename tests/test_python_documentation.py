"""The Python documentation is complete and runs.

`docs/api/` is the reference of the three public modules: every name in their `__all__` lists has
an `auto*` entry there, and the build (`tox -e docs`) fails on a name that has no target.

A Python snippet is a fenced `python` block (a `code-block:: python` in reStructuredText) whose
first line is a comment naming its file, `# quickstart.py`, as a YAML fixture is named in
`tests/test_chain_documentation.py`. It runs as a script, with the fake tools, in a temporary
directory that holds a copy of each example design it names. A block without that line is a
fragment: the import audit of `tests/test_public_imports.py` still reads it.
"""

import ast
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import xeda
import xeda.errors
import xeda.introspect

from . import tool_utils
from .source_layout import AGENT_DIR
from .test_chain_documentation import _blocks
from .test_public_imports import problems

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".claude" / "skills" / "xeda"
API = ROOT / "docs" / "api"
PUBLIC_MODULES = (xeda, xeda.errors, xeda.introspect)

ENTRY = re.compile(r"^\.\. auto(?:class|exception|function|data):: (\S+)\s*$", re.M)


def documented_names() -> set[str]:
    return {name for path in sorted(API.rglob("*.rst")) for name in ENTRY.findall(path.read_text())}


@pytest.mark.parametrize("module", PUBLIC_MODULES, ids=lambda module: module.__name__)
def test_the_reference_documents_every_public_name(module) -> None:
    documented = documented_names()
    missing = [
        name
        for name in module.__all__
        if name != "__version__" and f"{module.__name__}.{name}" not in documented
    ]
    assert missing == []


def test_the_reference_documents_only_public_names() -> None:
    public = {f"{module.__name__}.{name}" for module in PUBLIC_MODULES for name in module.__all__}
    assert documented_names() <= public


def test_no_public_docstring_names_the_implementation() -> None:
    """The reference shows these docstrings; a reader cannot import what they would name."""
    leaks = []
    for module in PUBLIC_MODULES:
        for name in module.__all__:
            value = getattr(module, name)
            members = vars(value).items() if isinstance(value, type) else ()
            shown = [(f"{name}.{key}", v) for key, v in members if not key.startswith("_")]
            for where, item in [(name, value), *shown]:
                if "_internal" in (getattr(item, "__doc__", None) or ""):
                    leaks.append(f"{module.__name__}.{where}")
    assert leaks == []


def test_no_nitpick_ignore_names_xeda() -> None:
    """A missing target of a Xeda name must fail the documentation build."""
    config = runpy.run_path(str(ROOT / "docs" / "conf.py"))
    assert config["nitpick_ignore_regex"]
    assert not [pattern for _, pattern in config["nitpick_ignore_regex"] if "xeda" in pattern]
    assert not config.get("nitpick_ignore")


# ---------------------------------------------------------------------------- the runnable snippets

NAMED = re.compile(r"# (\w[\w-]*\.py)\s*$")
EXAMPLE_PATH = re.compile(r"examples/[\w./-]+")
DOCUMENTS = [
    *sorted((ROOT / "docs").rglob("*.rst")),
    *sorted(SKILL_DIR.rglob("*.md")),
    *sorted(AGENT_DIR.rglob("*.md")),
]


def python_blocks(path: Path) -> list[tuple[str, str]]:
    """The named Python snippets of a document as (name, body)."""
    found = []
    for language, body in _blocks(path):
        named = NAMED.match(body.splitlines()[0]) if language == "python" else None
        if named:
            found.append((named[1], body))
    return found


SNIPPETS = [
    pytest.param(name, body, id=f"{path.relative_to(ROOT).as_posix()}:{name}")
    for path in DOCUMENTS
    for name, body in python_blocks(path)
]


def run_snippet(tmp_path: Path, name: str, body: str) -> str:
    """Run a snippet as a script in `tmp_path` and return what is wrong with it, or ''."""
    wrong = problems(ast.parse(body))
    if wrong:
        return f"the snippet uses names that are not public: {wrong}"
    for named in EXAMPLE_PATH.findall(body):
        source = ROOT / named
        directory = source if source.is_dir() else source.parent
        shutil.copytree(directory, tmp_path / directory.relative_to(ROOT), dirs_exist_ok=True)
    (tmp_path / name).write_text(body + "\n", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, name], cwd=tmp_path, capture_output=True, text=True, timeout=600
    )
    return "" if process.returncode == 0 else process.stderr[-2000:]


def test_the_documents_carry_runnable_python_snippets() -> None:
    for path in (ROOT / "docs" / "python-api.rst", SKILL_DIR / "SKILL.md", AGENT_DIR / "SKILL.md"):
        assert any(python_blocks(path)), path
    names = [(path, name) for path in DOCUMENTS for name, _ in python_blocks(path)]
    assert len(names) == len(set(names)), "a document gives a snippet name twice"


@pytest.mark.parametrize("name, body", SNIPPETS)
def test_every_named_python_snippet_runs(tmp_path, monkeypatch, name, body) -> None:
    tool_utils.require_tclsh()
    tool_utils.use_fake_tools(monkeypatch)
    assert run_snippet(tmp_path, name, body) == ""


@pytest.mark.parametrize(
    "body",
    [
        "from xeda import DefaultRunner\n",
        "import xeda\nxeda.Tool\n",
        "from xeda._internal.design import Design\nprint(Design)\n",
    ],
)
def test_a_snippet_with_a_removed_or_internal_name_fails(tmp_path, body) -> None:
    assert run_snippet(tmp_path, "bad.py", "# bad.py\n" + body) != ""


def test_a_snippet_that_exits_with_an_error_fails(tmp_path) -> None:
    assert "ZeroDivisionError" in run_snippet(tmp_path, "bad.py", "# bad.py\n1 / 0\n")
