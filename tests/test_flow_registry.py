"""Every advertised flow must be resolvable by the name the CLI advertises.

`xeda list-flows` used to list flows (`open_xc7`, `yosys_sim`) that `xeda run` and
`xeda list-settings` could not resolve, because the registry was keyed by class name and the
snake_case lookup fell back to a lossy `snakecase_to_camelcase` round-trip.
"""

from inspect import isabstract, isclass

import pytest

from xeda import flows
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import get_flow_class

ALL_FLOW_CLASSES = sorted(
    {cls for cls in flows.__builtin_flows__} | {cls for _mod, cls in registered_flows.values()},
    key=lambda cls: cls.name,
)


def test_flows_all_lists_only_concrete_flow_classes():
    for name in flows.__all__:
        if name.startswith("__"):
            continue
        cls = getattr(flows, name)
        assert isclass(cls), f"xeda.flows.__all__ exports non-class {name!r}"
        assert issubclass(cls, Flow), f"xeda.flows.__all__ exports non-Flow {name!r}"
        assert not isabstract(cls), f"xeda.flows.__all__ exports abstract flow {name!r}"


def test_every_builtin_flow_is_exported():
    exported = {getattr(flows, n) for n in flows.__all__ if not n.startswith("__")}
    missing = sorted(cls.__name__ for cls in flows.__builtin_flows__ if cls not in exported)
    assert not missing, f"builtin flows missing from xeda.flows.__all__: {missing}"


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_resolvable_by_canonical_name(flow_class):
    assert get_flow_class(flow_class.name) is flow_class


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_resolvable_by_class_name_and_aliases(flow_class):
    assert get_flow_class(flow_class.__name__) is flow_class
    for alias in flow_class.aliases:
        assert get_flow_class(alias) is flow_class


@pytest.mark.parametrize("flow_class", ALL_FLOW_CLASSES, ids=lambda cls: cls.name)
def test_flow_settings_schema_is_generatable(flow_class):
    """`xeda list-settings <flow>` and the JSON output both depend on this."""
    schema = flow_class.Settings.model_json_schema(by_alias=True)
    assert "properties" in schema


def test_flow_name_lookup_is_dash_and_case_insensitive():
    assert get_flow_class("vivado-synth") is get_flow_class("vivado_synth")
    assert get_flow_class("VIVADO_SYNTH") is get_flow_class("vivado_synth")
