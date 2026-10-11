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


def _restoring_by_hand(path: Path) -> list[int]:
    """The lines of a test file that empty the registry, or swap the module's own table."""
    lines = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "registered_flows"
        ):
            lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and any(
                isinstance(arg, ast.Constant) and arg.value == "registered_flows"
                for arg in node.args
            )
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
        "monkeypatch.setattr(flow_module, 'registered_flows', {})",
    ],
)
def test_the_scan_sees_each_way_to_restore_by_hand(tmp_path, source):
    path = tmp_path / "t.py"
    path.write_text(source + "\n")
    assert _restoring_by_hand(path) == [1]
