"""A test changes the environment only through `monkeypatch` (`tests/conftest.py` holds the
runtime half of this: what a test or an import leaves behind fails it).

The static half is here, because it names the line. A direct write to `os.environ` outlives the
test and every one after it in the worker; `PATH` is the dangerous one -- the fake toolchain
appended at import made a missing real `nextpnr-ecp5` resolve to the fake, so the real-tool tests
passed their probes and failed only in a full run, on a machine without the tools on `PATH`.
"""

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).parent

#: Methods that write the mapping.
WRITING_METHODS = {"update", "setdefault", "pop", "popitem", "clear"}
#: Functions that write the process environment behind the mapping's back.
WRITING_FUNCTIONS = {"putenv", "unsetenv"}

#: Reviewed writes: ``(file, line text)``, with why each is safe.
REVIEWED: dict[tuple[str, str], str] = {
    ("conftest.py", "os.environ.clear()"): "the guards put back what a test or an import changed",
    ("conftest.py", "os.environ.update(ENVIRONMENT_AT_START)"): "the same",
    ("conftest.py", "os.environ.update(before)"): "the same",
    (
        "test_external_cache.py",
        'os.environ["XEDA_TESTS_EXTERNAL_CACHE"] = cache',
    ): "runs only in a `spawn`ed pool process of its own, never in the test process",
}


def _is_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _writes_target(target: ast.AST) -> bool:
    """Whether assigning to (or deleting) `target` writes `os.environ`: the mapping itself
    (`os.environ = {}`, `os.environ |= ...`), one of its items, or either inside an unpacking."""
    if _is_environ(target):
        return True
    if isinstance(target, ast.Subscript):
        return _is_environ(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_writes_target(element) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _writes_target(target.value)
    return False


def writes(tree: ast.AST) -> list[ast.AST]:
    """Every node of `tree` that writes `os.environ`."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = (
                node.targets
                if isinstance(node, (ast.Assign, ast.Delete))
                else [node.target]  # type: ignore[union-attr]
            )
            if any(_writes_target(t) for t in targets):
                found.append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if _is_environ(node.func.value) and node.func.attr in WRITING_METHODS:
                found.append(node)
            elif (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in WRITING_FUNCTIONS
            ):
                found.append(node)
    return found


def offenders(path: Path) -> list[tuple[str, str]]:
    source = path.read_text()
    lines = source.splitlines()
    return [
        (f"{path.name}:{node.lineno}", lines[node.lineno - 1].strip())
        for node in writes(ast.parse(source))
    ]


def test_no_test_writes_the_environment_directly() -> None:
    found = [
        site
        for path in sorted(TESTS.glob("*.py"))
        if path.name != Path(__file__).name
        for site in offenders(path)
        if (site[0].split(":")[0], site[1]) not in REVIEWED
    ]
    assert not found, (
        "write the environment through `monkeypatch.setenv`/`delenv`/`setitem` (undone with the "
        f"test), or on a copy of `os.environ` handed to a child process: {found}"
    )


@pytest.mark.parametrize(
    "source",
    [
        'os.environ["PATH"] = "x"',
        'os.environ["PATH"] += ":x"',
        'del os.environ["PATH"]',
        "os.environ = {}",
        'os.environ |= {"A": "b"}',
        "del os.environ",
        'os.environ["PATH"], other = values',
        'other, *os.environ["PATH"] = values',
        '[os.environ["PATH"]] = values',
        'os.environ.update({"A": "b"})',
        'os.environ.setdefault("A", "b")',
        'os.environ.pop("A", None)',
        'os.putenv("A", "b")',
        'os.unsetenv("A")',
    ],
)
def test_the_scan_sees_every_way_of_writing_the_environment(source: str) -> None:
    assert len(writes(ast.parse(source))) == 1


@pytest.mark.parametrize(
    "source",
    [
        'x = os.environ["PATH"]',
        'env = dict(os.environ); env["PATH"] = "x"',
        'env = os.environ.copy(); env.update({"A": "b"})',
        'monkeypatch.setitem(os.environ, "A", "b")',
        'os.environ.get("A")',
    ],
)
def test_the_scan_leaves_reads_and_copies_alone(source: str) -> None:
    assert not writes(ast.parse(source))
