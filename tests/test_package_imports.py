"""Tests for the public package import behavior."""

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.python_compat


def run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        capture_output=True,
        text=True,
    )


def test_import_xeda_does_not_load_design_flows_or_heavy_dependencies() -> None:
    result = run_python("""
import json
import sys
import xeda

heavy_modules = ("xeda.design", "xeda.flow_runner", "xeda.flows", "pint", "pydantic")
print(json.dumps([name for name in heavy_modules if name in sys.modules]))
""")

    assert json.loads(result.stdout) == []


def test_package_dir_lists_lazy_exports_without_importing_them() -> None:
    result = run_python("""
import sys
import xeda

modules_before = set(sys.modules)
names = dir(xeda)
assert set(xeda.__all__) <= set(names)
assert set(sys.modules) == modules_before
""")

    assert result.stdout == ""


def test_extension_simulator_rejection_includes_builtin_cocotb_simulators() -> None:
    result = run_python("""
import sys, tempfile
from xeda import Design
from xeda.flow import FlowSettingsException, SimFlow

class CustomSimulator(SimFlow):
    def run(self):
        pass

assert "xeda.flows" not in sys.modules
try:
    design = Design(
        name="cocotb_design", root=tempfile.gettempdir(), rtl={"sources": []}, tb={"cocotb": True}
    )
    CustomSimulator.check_design_supported(design)
except FlowSettingsException as error:
    suggested = str(error).split("use one of: ")[1].split(", ")
    assert {"ghdl_sim", "nvc", "verilator"} <= set(suggested)
else:
    raise AssertionError("A simulator without Cocotb support must reject the design")
""")

    assert result.stdout == ""


@pytest.mark.parametrize("name", ["flow", "tool", "cocotb", "design", "flows"])
def test_internal_modules_import_as_submodules_after_a_bare_import(name: str) -> None:
    result = run_python(f"""
import importlib
import xeda

assert not hasattr(xeda, {name!r})
module = importlib.import_module("xeda." + {name!r})
assert xeda.{name} is module
""")

    assert result.stdout == ""


def test_builtin_flow_resolves_on_the_first_api_call() -> None:
    result = run_python("""
import sys
from xeda.flow_runner import get_flow_class

assert "xeda.flows" not in sys.modules
assert get_flow_class("ghdl_sim").name == "ghdl_sim"
""")

    assert result.stdout == ""


@pytest.mark.parametrize(
    "imports",
    [
        "import xeda",
        "import xeda.design; import xeda.flow_runner; import xeda.flows; import xeda",
        "from xeda import Design; import xeda",
        "from xeda import Workspace; import xeda.workspace; import xeda",
    ],
)
def test_package_exports_work_in_import_orders(imports: str) -> None:
    result = run_python(f"""
import importlib
{imports}
import xeda

expected = {{
    "Design": ("xeda.design", "Design"),
    "Plan": ("xeda.plan", "Plan"),
    "Result": ("xeda.outcomes", "Result"),
    "RunRecord": ("xeda.records", "RunRecord"),
    "Spec": ("xeda.spec", "Spec"),
    "Workspace": ("xeda.workspace", "Workspace"),
}}
assert isinstance(xeda.__version__, str)
assert set(expected) | {{"__version__"}} <= set(xeda.__all__)
for name, (module_name, attribute) in expected.items():
    assert getattr(xeda, name) is getattr(importlib.import_module(module_name), attribute)
for name in xeda.__all__:
    getattr(xeda, name)
""")

    assert result.stdout == ""
