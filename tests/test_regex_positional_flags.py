"""A count or a flag is never passed by position to `re.split`, `re.sub` or `re.subn`.

Their third (`split`) or fourth (`sub`, `subn`) positional parameter is a count, not the flags:
`re.split(pattern, text, re.MULTILINE)` splits at most 8 times and `re.sub(..., re.MULTILINE |
re.UNICODE)` replaces at most 40 matches. Python 3.13 deprecates the positional form. The scan
covers xeda's source, the tests and the tools.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

#: The number of positional parameters of each function before its count.
POSITIONAL_BEFORE_COUNT = {"split": 2, "sub": 3, "subn": 3}


def positional_counts(tree: ast.Module) -> list[int]:
    """The line of every `re.split`/`re.sub`/`re.subn` call in `tree` that passes the count by
    position."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.func.attr in POSITIONAL_BEFORE_COUNT
        and len(node.args) > POSITIONAL_BEFORE_COUNT[node.func.attr]
    ]


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
    ],
)
def test_the_scan_leaves_the_right_forms_alone(call):
    assert positional_counts(ast.parse(call)) == []
