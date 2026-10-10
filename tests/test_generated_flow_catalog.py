"""The generated lightweight flow catalog stays fresh and matches chain validation."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.python_compat

ROOT = Path(__file__).resolve().parents[1]


def test_generated_flow_catalog_is_current():
    result = subprocess.run(
        [sys.executable, "tools/generate_flow_catalog.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_catalog_generator_excludes_registered_extension_flows():
    code = r"""
import json
import runpy
from typing import ClassVar

from xeda.flow import Flow
from xeda.introspect import all_flow_classes

generator = runpy.run_path("tools/generate_flow_catalog.py")

class CatalogExtensionFlow(Flow):
    aliases: ClassVar[list[str]] = ["catalog_extension_alias"]

    def run(self) -> None:
        pass

catalog = json.loads(generator["render"]())
builtin_classes = {
    flow for flow in all_flow_classes()
    if flow.__module__.startswith("xeda.flows.")
}
builtin_names = {flow.name for flow in builtin_classes}
assert set(catalog) == builtin_names
assert "catalog_extension_flow" not in catalog
assert "vivado_synth" in catalog
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_builtin_completion_matches_the_runtime_chain_validator():
    """Compare exact candidate lists for builtin spellings, outputs, followers, and bad input."""
    code = r"""
from xeda.flow_runner.chains import complete_request
from xeda.flow.io import declared_outputs
from xeda.flow_catalog import complete_builtin_request
from xeda.introspect import all_flow_classes

classes = [
    cls for cls in all_flow_classes()
    if cls.__module__.startswith("xeda.flows")
]
cases = {
    "", "+", "nosuch+ne", "nextpnr++", "nextpnr.config.x+",
    "yosys_fpga+nextpnr+n", "openfpgaloader+", "nextpnr+openfpgaloader",
    " yosys_fpga+ne", "yosys_fpga +ne", " nextpnr .c",
    "yosys__fpga+", "yosys__fpga+ne", "_yosys_fpga_+ne",
    "g_h_d_l_synth+",
    # The final edge can be valid while an earlier edge makes the full request invalid.
    "openfpgaloader+nextpnr+fp",
    "yosys_fpga+nextpnr+yosys_fpga",
    "yosys_fpga+nextpnr+fpga_pack+open",
}

for cls in classes:
    spellings = {
        cls.name,
        cls.name.replace("_", "-"),
        cls.__name__,
        *cls.aliases,
    }
    for spelling in spellings:
        cases.update({spelling, spelling[:1], spelling[:3], spelling + "+"})
        for output in declared_outputs(cls):
            cases.update(
                spelling + "." + output[:length]
                for length in range(len(output) + 1)
            )
            qualified = spelling + "." + output + "+"
            cases.add(qualified)
            for candidate in complete_request(qualified, classes):
                child = candidate[len(qualified):]
                cases.update(qualified + child[:length] for length in range(1, len(child) + 1))

# Include each actual follower and a partial follower prefix for every builtin producer spelling.
for cls in classes:
    for spelling in {cls.name, *cls.aliases}:
        for candidate in complete_request(spelling + "+", classes):
            suffix = candidate[len(spelling) + 1:]
            cases.add(spelling + "+" + suffix[:1])
            cases.add(spelling + "+" + suffix)
            second = spelling + "+" + suffix + "+"
            cases.add(second)
            for third in complete_request(second, classes):
                cases.add(third)

for incomplete in sorted(cases):
    if "+" not in incomplete and "." not in incomplete:
        continue  # Plain flow-name completion remains a separate Click choice path.
    expected = complete_request(incomplete, classes)
    actual = complete_builtin_request(incomplete)
    assert actual == expected, (incomplete, expected, actual)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
