"""What a module exports is what it defines, and no module replaces a builtin.

`all = [...]` in a module that meant `__all__` exported nothing and replaced the builtin `all` for
the module's own code; the list it held named a class that no longer existed.
"""

import builtins
import importlib
import pkgutil

import pytest

import xeda
import xeda.flows

MODULES = [info.name for info in pkgutil.walk_packages(xeda.__path__, "xeda.")]
BUILTINS = [name for name in vars(builtins) if not name.startswith("_")]


@pytest.mark.parametrize("name", MODULES)
def test_every_name_a_module_exports_is_defined_in_it(name) -> None:
    module = importlib.import_module(name)
    missing = [n for n in getattr(module, "__all__", ()) if not hasattr(module, n)]
    assert not missing, f"{name}.__all__ names {missing}, which the module does not have"


@pytest.mark.parametrize("name", MODULES)
def test_no_module_replaces_a_builtin(name) -> None:
    module = importlib.import_module(name)
    replaced = [
        n for n in BUILTINS if n in vars(module) and vars(module)[n] is not getattr(builtins, n)
    ]
    assert not replaced, f"{name} defines {replaced}, which the builtins of that name are not"


def test_the_vivado_package_exports_its_flow_base_and_tool() -> None:
    from xeda.flows import vivado

    assert sorted(vivado.__all__) == ["Vivado", "VivadoTool"]
