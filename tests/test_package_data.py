"""Every tracked file under `src/xeda/` reaches the wheel: it is a module of a package, a file that
a glob of `[tool.setuptools.package-data]` matches, or an entry of `UNSHIPPED` with a reason.
`tools/check_wheel.py` builds the wheel and compares its real contents."""

import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_wheel.py"

pytestmark = pytest.mark.python_compat


def _load_tool() -> types.ModuleType:
    """The script as a module. `tools/` is no package, and importing the file would write its
    bytecode into the checkout, so its source runs in a module of its own."""
    module = types.ModuleType("check_wheel")
    module.__file__ = str(TOOL)
    exec(compile(TOOL.read_text(encoding="utf-8"), str(TOOL), "exec"), module.__dict__)
    return module


tool = _load_tool()


def test_every_data_file_is_shipped():
    files = tool.tracked_files()
    missing = [
        name
        for name in files
        if name not in tool.UNSHIPPED and not tool.shipped_by_setuptools(name, files)
    ]
    assert not missing, missing


def test_the_data_script_of_klayout_is_not_a_module_and_needs_its_glob():
    files = tool.tracked_files()
    name = "xeda/flows/openroad/data/def2stream.py"
    assert name in files
    assert "xeda/flows/openroad/data" not in tool.package_directories(files)
    assert tool.shipped_by_setuptools(name, files)


def test_an_unlisted_file_is_not_shipped():
    files = [*tool.tracked_files(), "xeda/flows/vivado/templates/new.unknown"]
    assert not tool.shipped_by_setuptools("xeda/flows/vivado/templates/new.unknown", files)


def test_every_unshipped_entry_names_a_tracked_file_and_a_reason():
    files = set(tool.tracked_files())
    for name, reason in tool.UNSHIPPED.items():
        assert name in files and reason.strip(), name
