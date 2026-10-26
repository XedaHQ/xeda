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
assert set(xeda.__all__) | {"cocotb", "flow", "tool"} <= set(names)
assert set(sys.modules) == modules_before
""")

    assert result.stdout == ""


def test_extension_simulator_rejection_includes_builtin_cocotb_simulators() -> None:
    result = run_python("""
import sys, tempfile
from xeda import Design, SimFlow
from xeda.flow import FlowSettingsException

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


@pytest.mark.parametrize("name", ["flow", "tool", "cocotb", "Tool", "Cocotb"])
def test_package_tool_and_flow_exports_work_after_a_bare_import(name: str) -> None:
    result = run_python(f"""
import importlib
import xeda

module = getattr(xeda, {name!r})
if {name!r} in ("Tool", "Cocotb"):
    expected = getattr(importlib.import_module("xeda." + {name.lower()!r}), {name!r})
else:
    expected = importlib.import_module("xeda." + {name!r})
assert module is expected
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
    ],
)
def test_package_exports_work_in_import_orders(imports: str) -> None:
    result = run_python(f"""
import importlib
{imports}
import xeda

expected = {{
    "FPGA": ("xeda.flow", "FPGA"),
    "Cocotb": ("xeda.cocotb", "Cocotb"),
    "DefaultRunner": ("xeda.flow_runner", "DefaultRunner"),
    "Design": ("xeda.design", "Design"),
    "Dse": ("xeda.flow_runner", "Dse"),
    "Flow": ("xeda.flow", "Flow"),
    "FlowRunner": ("xeda.flow_runner", "FlowRunner"),
    "SimFlow": ("xeda.flow", "SimFlow"),
    "SynthFlow": ("xeda.flow", "SynthFlow"),
    "Tool": ("xeda.tool", "Tool"),
}}
assert isinstance(xeda.__version__, str)
assert set(xeda.__all__) == set(expected) | {{"__version__", "design", "flow_runner", "flows"}}
for name, (module_name, attribute) in expected.items():
    assert getattr(xeda, name) is getattr(importlib.import_module(module_name), attribute)
for name in ("design", "flow_runner", "flows"):
    assert getattr(xeda, name) is importlib.import_module(f"xeda.{{name}}")
""")

    assert result.stdout == ""
