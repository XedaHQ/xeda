"""The `wire_rc` setting takes the same text on the command line as in a design file.

The platform model and the setting that overrides it read their numbers strictly. `-s` gives
text, and the text is read as numbers before the model sees it, as for every other number
setting. A pair (a layer's resistance and capacitance) is a comma-separated list, as every list
given as text.
"""

from pathlib import Path

import pytest

from xeda.flow import FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.openroad.base import OpenroadStage
from xeda.flow_runner.settings_layers import merge_layers

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    probe_flows,
    register_openroad_flows,
    render_driver,
)
from .tiny_platform import tiny_store
from .setting_texts import text_items
from .scoping import node_settings

TEXT = [
    "wire_rc.signal_layer=m3",
    "wire_rc.layers.m2=0.5,0.25",
    "wire_rc.layers.m5=0.1, 0.2",
    "wire_rc.vias.v2=0.75",
]
TYPED = {
    "wire_rc": {
        "signal_layer": "m3",
        "layers": {"m2": [0.5, 0.25], "m5": [0.1, 0.2]},
        "vias": {"v2": 0.75},
    }
}


def from_text(*texts: str):
    return OpenroadStage.Settings.from_input(
        merge_layers(list(texts), settings_cls=OpenroadStage.Settings)
    )


def test_text_from_the_command_line_gives_the_settings_of_the_design_file():
    assert from_text(*TEXT) == OpenroadStage.Settings.from_input(TYPED)
    rc = from_text(*TEXT).wire_rc
    assert rc.layers == {"m2": (0.5, 0.25), "m5": (0.1, 0.2)}
    assert rc.vias == {"v2": 0.75} and rc.signal_layer == "m3" and rc.clock_layer is None


def test_the_numbers_are_read_like_those_of_any_other_number_setting():
    """The command-line text of a number is a number for a setting and for a leaf of `wire_rc`."""
    from xeda.flow import Flow

    assert Flow.Settings.from_input(merge_layers(["nthreads=3"], settings_cls=Flow.Settings))
    with pytest.raises(FlowSettingsError):
        Flow.Settings.from_input({"nthreads": "3"})
    assert from_text("wire_rc.vias.v1=1e-3").wire_rc.vias == {"v1": 0.001}
    assert from_text("wire_rc.vias.v1=2").wire_rc.vias == {"v1": 2.0}


@pytest.mark.parametrize(
    "text, where",
    [
        ("wire_rc.vias.v1=abc", "wire_rc -> vias -> v1"),
        ("wire_rc.vias.v1=nan", "wire_rc -> vias -> v1"),
        ("wire_rc.layers.m1=0.1", "wire_rc -> layers -> m1"),
        ("wire_rc.layers.m1=0.1,0.2,0.3", "wire_rc -> layers -> m1"),
        ("wire_rc.layers.m1=a,b", "wire_rc -> layers -> m1"),
        ("wire_rc.layers.m1=inf,1", "wire_rc -> layers -> m1"),
        ("wire_rc.layers.m1=[0.1,0.2]", "wire_rc -> layers -> m1"),
    ],
)
def test_text_that_is_no_number_or_no_pair_is_refused_naming_the_leaf(text, where):
    with pytest.raises(FlowSettingsError) as error:
        from_text(text)
    assert where in str(error.value)


def test_a_typed_value_of_the_wrong_type_is_still_refused():
    for typed in ({"vias": {"v1": "abc"}}, {"layers": {"m1": [1.0]}}, {"layers": {"m1": 1.0}}):
        with pytest.raises(FlowSettingsError):
            OpenroadStage.Settings.from_input({"wire_rc": typed})


def test_the_header_is_the_same_for_the_text_and_the_file_form(tmp_path):
    with probe_flows() as probes:
        text = probes.flow(
            tmp_path / "text", settings=merge_layers(TEXT, settings_cls=OpenroadStage.Settings)
        )
        typed = probes.flow(tmp_path / "typed", settings=TYPED)
        first = render_driver(text, "openroad_probe")
        second = render_driver(typed, "openroad_probe")
    strip = lambda script, root: script.replace(str(root), "ROOT")  # noqa: E731
    assert strip(first, tmp_path / "text") == strip(second, tmp_path / "typed")
    assert 'set_layer_rc -layer "m5" -resistance 0.1 -capacitance 0.2' in first


def plan(tmp_path: Path, given):
    store = tiny_store(tmp_path)
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        "openroad",
        openroad_design(tmp_path / "design", rtl=True),
        flow_settings=node_settings("openroad", [*openroad_settings(store), *given]),
    )


def test_a_chain_planned_with_text_is_the_chain_planned_with_the_file_form(tmp_path):
    flat = {
        "flows.openroad_place.wire_rc.signal_layer": "m3",
        "flows.openroad_cts.wire_rc.layers.m2": [0.5, 0.25],
        "flows.openroad_grt.wire_rc.vias.v2": 0.75,
    }
    with register_openroad_flows():
        texts = [
            "flows.openroad_place.wire_rc.signal_layer=m3",
            "flows.openroad_cts.wire_rc.layers.m2=0.5,0.25",
            "flows.openroad_grt.wire_rc.vias.v2=0.75",
        ]
        by_text = plan(tmp_path / "text", texts)
        store = tiny_store(tmp_path / "typed")
        by_file = DefaultRunner(tmp_path / "typed" / "run", display_results=False).plan(
            "openroad",
            openroad_design(tmp_path / "typed" / "design", rtl=True),
            flow_settings=node_settings(
                "openroad",
                {
                    **text_items(openroad_settings(store)),
                    **flat,
                },
            ),
        )
    assert [n.flowrun_hash for n in by_text.nodes] == [n.flowrun_hash for n in by_file.nodes]
    for node in by_text.nodes:
        if node.name != "yosys":
            rc = node.settings.wire_rc
            assert rc.layers == {"m2": (0.5, 0.25)} and rc.vias == {"v2": 0.75}, node.name
            assert rc.signal_layer == "m3", node.name


def test_text_leaves_that_disagree_are_refused_naming_both_flows(tmp_path):
    with register_openroad_flows(), pytest.raises(FlowSettingsException) as error:
        plan(
            tmp_path,
            [
                "flows.openroad_place.wire_rc.layers.m2=0.5,0.25",
                "flows.openroad_grt.wire_rc.layers.m2=0.5,0.26",
            ],
        )
    message = str(error.value)
    assert "wire_rc.layers.m2" in message
    assert "openroad_place" in message and "openroad_grt" in message
