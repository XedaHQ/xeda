"""Keeping the flow registry as a test found it.

`xeda.flows` registers the built-in flows when it is first imported, and nothing imports it before
a test needs it. A test that snapshots `registered_flows` before that import and restores the
snapshot afterwards removes the built-ins from the registry, while Python keeps the module as
imported: nothing registers them again, and every later test in the process sees a registry
without them. So a snapshot is taken after the built-ins are in.
"""

from contextlib import contextmanager
from importlib import import_module
from typing import Iterator

from xeda.flow import registered_flows


def load_builtin_flows() -> None:
    """Import the built-in flows now, so a registry snapshot holds them."""
    import_module("xeda.flows")


@contextmanager
def preserved_flow_registry() -> Iterator[None]:
    """Whatever the body registers or removes, the registry is as it was on the way in, built-in
    flows included (they are loaded first)."""
    load_builtin_flows()
    before = registered_flows.copy()
    try:
        yield
    finally:
        registered_flows.clear()
        registered_flows.update(before)
