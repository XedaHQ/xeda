"""The Python documentation is complete and clean.

`docs/api/` is the reference of the three public modules: every name in their `__all__` lists has
an `auto*` entry there, and the build (`tox -e docs`) fails on a name that has no target.
"""

import re
import runpy
from pathlib import Path

import pytest

import xeda
import xeda.errors
import xeda.introspect

ROOT = Path(__file__).resolve().parents[1]
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


def test_no_nitpick_ignore_names_xeda() -> None:
    """A missing target of a Xeda name must fail the documentation build."""
    config = runpy.run_path(str(ROOT / "docs" / "conf.py"))
    assert config["nitpick_ignore_regex"]
    assert not [pattern for _, pattern in config["nitpick_ignore_regex"] if "xeda" in pattern]
    assert not config.get("nitpick_ignore")
