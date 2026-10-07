"""An exception that is built and dropped does nothing.

`ValueError("...")` on a line of its own is a check that never fails: `GitReference.fetch_design`
dropped one this way and went on with no repository, and `run_bloop` dropped another. Ruff's
`PLW0133` (enforced by `tox -e ruff`) finds this for the built-in exceptions only, so this scan
covers the rest: every exception class xeda defines, found through the bases the classes name.
"""

import ast
import builtins
from collections.abc import Mapping
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "xeda"


def _called_name(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def dropped_exceptions(trees: Mapping[str, ast.Module]) -> list[str]:
    """Where an exception is built as a statement of its own, in `trees` (path -> parsed
    module): the built-in exceptions and every class that derives from one, by the names the
    class statements give their bases."""
    exceptions = {
        name
        for name, value in vars(builtins).items()
        if isinstance(value, type) and issubclass(value, BaseException)
    }
    bases: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases.setdefault(node.name, set()).update(
                    base.id if isinstance(base, ast.Name) else base.attr
                    for base in node.bases
                    if isinstance(base, (ast.Name, ast.Attribute))
                )
    grown = True
    while grown:
        grown = False
        for name, named in bases.items():
            if name not in exceptions and named & exceptions:
                exceptions.add(name)
                grown = True
    found = [
        (path, node.lineno, name)
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and (name := _called_name(node.value)) in exceptions
    ]
    return [f"{path}:{line}: {name}(...)" for path, line, name in sorted(found)]


def test_no_exception_is_built_and_dropped() -> None:
    trees = {
        str(path.relative_to(SRC)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
    }
    assert dropped_exceptions(trees) == []


def test_the_scan_sees_an_exception_of_xedas_own_built_and_dropped() -> None:
    source = (
        "class Failed(Exception):\n"
        "    pass\n"
        "class Refused(Failed):\n"
        "    pass\n"
        "def check(value):\n"
        "    if value:\n"
        "        Refused('dropped')\n"
        "    if not value:\n"
        "        raise Refused('raised')\n"
        "    errors.Refused('dropped, by attribute')\n"
        "    kept = Refused('kept')\n"
        "    log(Refused('passed on'))\n"
        "    return Failed('returned')\n"
    )
    found = dropped_exceptions({"m.py": ast.parse(source)})
    assert found == ["m.py:7: Refused(...)", "m.py:10: Refused(...)"]
