"""A test that restores the flow registry must keep the built-in flows.

`xeda.flows` registers the built-ins when it is first imported, and a test file run alone imports
it late. A restore of an earlier snapshot then removes the built-ins for good. The shared helper
loads them before it takes the snapshot, and no test restores the registry by any other means.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
HELPER = "flow_registry.py"


def _run(script: str) -> str:
    """Run `script` in a fresh interpreter, in which nothing has imported `xeda.flows` yet."""
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=TESTS.parent,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_the_helper_keeps_the_built_in_flows_of_a_registry_that_was_not_loaded():
    out = _run("""
        import sys
        from xeda.flow import Flow, registered_flows
        from tests.flow_registry import preserved_flow_registry

        assert "xeda.flows" not in sys.modules and not registered_flows
        with preserved_flow_registry():
            class _Probe(Flow):
                '''A flow that only this block registers.'''
                results_description = {}
                def run(self): pass
                def parse_reports(self): return True
            assert "_Probe" in registered_flows
        print("_Probe" in registered_flows, "nextpnr" in registered_flows,
              "openfpgaloader" in registered_flows)
        """)
    assert out == "False True True"


def test_a_snapshot_taken_before_the_built_ins_load_would_lose_them():
    """The mutation: the restore this helper replaces, so the test above has teeth."""
    out = _run("""
        from importlib import import_module
        from xeda.flow import registered_flows

        before = registered_flows.copy()
        import_module("xeda.flows")
        registered_flows.clear()
        registered_flows.update(before)
        print("nextpnr" in registered_flows)
        """)
    assert out == "False"


def _is_registry(node: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    """Whether `node` names the registry: `registered_flows`, any `<module>.registered_flows`
    (`flow_module.registered_flows`, `xeda.flow.flow.registered_flows`), or a name bound to one."""
    if isinstance(node, ast.Name):
        return node.id == "registered_flows" or node.id in aliases
    return isinstance(node, ast.Attribute) and node.attr == "registered_flows"


def _registry_aliases(tree: ast.AST) -> frozenset[str]:
    """The names a file binds to the registry: `table = flow_module.registered_flows`, and
    `from xeda.flow import registered_flows as table`."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_registry(node.value):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ImportFrom):
            aliases.update(
                a.asname for a in node.names if a.name == "registered_flows" and a.asname
            )
    return frozenset(aliases)


#: The methods that take entries out or drop them wholesale. A test removes the flow it defined by
#: name (`pop(name, None)`, `del registered_flows[name]`) and adds it back the same way (`update`,
#: `registered_flows[name] = ...`): these do not depend on what the registry held before.
WHOLESALE = ("clear", "popitem")


def _restoring_by_hand(path: Path) -> list[int]:
    """The lines of a test file that empty the registry or swap the table itself."""
    lines = []
    tree = ast.parse(path.read_text(), str(path))
    aliases = _registry_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            emptied = (
                isinstance(func, ast.Attribute)
                and func.attr in WHOLESALE
                and _is_registry(func.value, aliases)
            )
            # `setattr(module, "registered_flows", ...)`, `monkeypatch.setattr(module,
            # "registered_flows", ...)` and `monkeypatch.setattr("xeda.flow.flow.registered_flows",
            # ...)`
            swapped = (
                (isinstance(func, ast.Attribute) and func.attr == "setattr")
                or (isinstance(func, ast.Name) and func.id == "setattr")
            ) and any(
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.rsplit(".", 1)[-1] == "registered_flows"
                for arg in node.args
            )
            if emptied or swapped:
                lines.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr == "registered_flows"
                for target in targets
            ):
                lines.append(node.lineno)
    return lines


def test_no_test_restores_the_registry_by_hand():
    found = {
        path.name: lines
        for path in sorted(TESTS.glob("*.py"))
        if path.name not in (HELPER, Path(__file__).name) and (lines := _restoring_by_hand(path))
    }
    assert not found, (
        "restore the flow registry with `preserved_flow_registry` (tests/flow_registry.py); "
        f"found by hand: {found}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "registered_flows.clear()",
        "flow_module.registered_flows.clear()",
        "xeda.flow.flow.registered_flows.clear()",
        "registered_flows.popitem()",
        "flow_module.registered_flows.popitem()",
        "monkeypatch.setattr(flow_module, 'registered_flows', {})",
        "monkeypatch.setattr('xeda.flow.flow.registered_flows', {})",
        "setattr(flow_module, 'registered_flows', {})",
        "flow_module.registered_flows = {}",
        "flow_module.registered_flows |= {}",
        "table = flow_module.registered_flows\ntable.clear()",
        "table = registered_flows\ntable.popitem()",
        "from xeda.flow import registered_flows as table\ntable.clear()",
    ],
)
def test_the_scan_sees_each_way_to_restore_by_hand(tmp_path, source):
    path = tmp_path / "t.py"
    path.write_text(source + "\n")
    assert _restoring_by_hand(path) == [source.count("\n") + 1]


@pytest.mark.parametrize(
    "source",
    [
        "registered_flows.pop(name, None)",
        "flow_module.registered_flows.pop(name, None)",
        "del registered_flows[name]",
        "registered_flows[name] = entry",
        "registered_flows.update(own)",
        "registered_flows.setdefault(name, entry)",
        "before = registered_flows.copy()",
        "monkeypatch.setitem(registered_flows, name, entry)",
    ],
)
def test_the_scan_leaves_the_registration_of_a_flow_by_name_alone(tmp_path, source):
    path = tmp_path / "t.py"
    path.write_text(source + "\n")
    assert _restoring_by_hand(path) == []
