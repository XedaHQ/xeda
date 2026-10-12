"""A count or a flag is never passed by position to `re.split`, `re.sub` or `re.subn`.

The third positional parameter of `re.split` and the fourth of `re.sub` and `re.subn` is a
count. A flag given there becomes a count: `re.split(pattern, text, re.MULTILINE)` splits at most
8 times, and `re.sub(..., re.MULTILINE | re.UNICODE)` replaces at most 40 matches. Python 3.13
deprecates the positional form. The scan covers the source, the tests and the tools.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

#: The number of positional parameters of each function before its count.
POSITIONAL_BEFORE_COUNT = {"split": 2, "sub": 3, "subn": 3}


def _is_flag(node: ast.expr) -> bool:
    """`re.I`, `re.MULTILINE | re.UNICODE` and the like."""
    if isinstance(node, ast.BinOp):
        return _is_flag(node.left) and _is_flag(node.right)
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "re"
        and node.attr.isupper()
    )


def positional_counts(tree: ast.Module) -> list[int]:
    """The line of every call in `tree` that passes the count of `re.split`, `re.sub` or
    `re.subn` by position, and of every call of a compiled pattern's `split`, `sub` or `subn`
    that passes a flag there."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        function = node.func
        if function.attr not in POSITIONAL_BEFORE_COUNT:
            continue
        module_function = isinstance(function.value, ast.Name) and function.value.id == "re"
        if module_function:
            if len(node.args) > POSITIONAL_BEFORE_COUNT[function.attr]:
                found.append(node.lineno)
        elif any(_is_flag(arg) for arg in node.args):
            found.append(node.lineno)
    return found


def test_no_flag_is_passed_where_a_count_goes():
    found = [
        f"{path.relative_to(ROOT)}:{line}"
        for directory in ("src", "tests", "tools")
        for path in sorted((ROOT / directory).rglob("*.py"))
        for line in positional_counts(ast.parse(path.read_text(), str(path)))
    ]
    assert not found, "pass `flags=`, `count=` or `maxsplit=` by keyword: " + ", ".join(found)


@pytest.mark.parametrize(
    "call",
    [
        "re.split(p, s, re.MULTILINE)",
        "re.split(p, s, 1)",
        "re.sub(p, r, s, re.MULTILINE | re.UNICODE)",
        "re.subn(p, r, s, re.I)",
        "pattern.sub(r, s, re.I)",
        "pattern.split(s, re.MULTILINE | re.UNICODE)",
    ],
)
def test_the_scan_sees_a_positional_count(call):
    assert positional_counts(ast.parse(call)) == [1]


@pytest.mark.parametrize(
    "call",
    [
        "re.split(p, s)",
        "re.split(p, s, flags=re.I)",
        "re.split(p, s, maxsplit=1)",
        "re.sub(p, r, s, flags=re.I)",
        "re.findall(p, s, re.I)",
        "re.search(p, s, re.I)",
        "pattern.sub(r, s, 1)",
        "text.split(',', 1)",
    ],
)
def test_the_scan_leaves_the_right_forms_alone(call):
    assert positional_counts(ast.parse(call)) == []
