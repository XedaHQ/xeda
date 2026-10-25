from __future__ import annotations

import subprocess
import sys


import pytest

pytestmark = pytest.mark.python_compat


def test_flow_names_are_available_without_loading_flow_models() -> None:
    code = """
import sys
from xeda.flow_catalog import flow_names

names = flow_names()
assert 'ghdl_sim' in names
assert 'ghdl' not in names
assert 'xeda.flow.flow' not in sys.modules
assert not any(name.startswith('xeda.flows.') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_flow_names_match_runtime_builtin_catalog() -> None:
    code = """
from xeda.flow_catalog import flow_names

catalog_names = flow_names()

from xeda.introspect import all_flow_classes

runtime_names = sorted(
    {cls.name for cls in all_flow_classes()}
)
assert catalog_names == runtime_names
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_flow_names_include_already_registered_extension_flows() -> None:
    from xeda.flow import Flow, registered_flows
    from xeda.flow_catalog import flow_names

    class CatalogExtensionFlow(Flow):
        def run(self) -> None:
            pass

    try:
        names = flow_names()
        assert "catalog_extension_flow" in names
        assert "CatalogExtensionFlow" not in names
    finally:
        registered_flows.pop("catalog_extension_flow", None)
        registered_flows.pop("CatalogExtensionFlow", None)
