"""Reserved input bindings retain their origins before settings composition."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import pickle

import pytest
import yaml

from xeda import Design
from xeda.flow import FlowSettingsError, FlowSettingsException, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.bindings import (
    BindingEntry,
    BindingLayer,
    NodeKey,
    ProducerRef,
    default_nodes,
    effective_bindings,
    split_bindings,
)
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.settings_layers import split_flow_sections

from .io_flows import (
    _ChainConsumer,
    _ChainOptionalConsumer,
    _ChainProducer,
    _ChainSimpleConsumer,
    _Maker,
    _Taker,
)


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def layer(cls, inputs, location="the design file"):
    kind = "cli" if "CLI" in location else "api" if "API" in location else "file"
    return split_bindings({cls.name: {"inputs": inputs}}, location=location, kind=kind)[1]


def test_split_copies_settings_and_captures_immutable_binding_data():
    sections = {_ChainConsumer.name: {"quiet": True, "inputs": {"files": ["chain_source.files"]}}}
    before = deepcopy(sections)
    settings, bindings = split_bindings(sections, location="project.yaml")
    sections[_ChainConsumer.name]["inputs"]["files"].append("__maker.made")

    assert settings == {_ChainConsumer.name: {"quiet": True}}
    assert before[_ChainConsumer.name]["inputs"]["files"] == ["chain_source.files"]
    selected = effective_bindings([bindings], default_nodes([_ChainConsumer]))
    binding = selected[NodeKey(_ChainConsumer.name)]["files"]
    assert binding.references == (ProducerRef(NodeKey(_ChainProducer.name), "files"),)
    assert binding.location == f"project.yaml: flows.{_ChainConsumer.name}.inputs.files"
    with pytest.raises(FrozenInstanceError):
        binding.references[0].node = "other"
    with pytest.raises(TypeError):
        selected[NodeKey(_ChainConsumer.name)]["files"] = binding


def test_binding_precedence_merges_input_names_and_replaces_ordered_lists():
    layers = [
        layer(
            _ChainConsumer, {"left": "chain_source.json_a", "files": ["__maker.made"]}, "project"
        ),
        layer(
            _ChainConsumer,
            {"right": "chain_source.json_b", "files": ["chain_source.files"]},
            "design",
        ),
        layer(_ChainConsumer, {"left": "chain_source.json_b"}, "CLI"),
        layer(_ChainConsumer, {"files": ["chain_source.data", "chain_source.files"]}, "API"),
    ]
    bindings = effective_bindings(layers, default_nodes([_ChainConsumer]))[
        NodeKey(_ChainConsumer.name)
    ]
    assert bindings["left"].references == (ProducerRef(NodeKey(_ChainProducer.name), "json_b"),)
    assert bindings["right"].references == (ProducerRef(NodeKey(_ChainProducer.name), "json_b"),)
    assert bindings["files"].references == (
        ProducerRef(NodeKey(_ChainProducer.name), "data"),
        ProducerRef(NodeKey(_ChainProducer.name), "files"),
    )
    assert bindings["files"].location.startswith("API:")


@pytest.mark.parametrize(
    "value",
    [
        True,
        7,
        None,
        Path("made.txt"),
        {"node": "__maker"},
        "a/b",
        "__maker..made",
        "__maker+__taker",
        "",
    ],
)
def test_winning_invalid_reference_is_rejected_at_its_location(value):
    with pytest.raises(FlowSettingsException, match=r"design.yaml.*inputs.made"):
        effective_bindings([layer(_Taker, {"made": value}, "design.yaml")], default_nodes([_Taker]))


@pytest.mark.parametrize("value", [True, [], None, "__maker"])
def test_inputs_requires_a_mapping(value):
    _, bindings = split_bindings({_Taker.name: {"inputs": value}}, location="project.yaml")
    with pytest.raises(FlowSettingsException, match="project.yaml.*inputs.*mapping"):
        effective_bindings([bindings], default_nodes([_Taker]))


@pytest.mark.parametrize(
    "value, message",
    [
        ("missing_producer.made", "producer"),
        ("__maker.Made", "output.*Made"),
        (["__maker.made"], "one.*list"),
    ],
)
def test_strict_reference_names_and_scalar_cardinality(value, message):
    with pytest.raises(FlowSettingsException, match=message):
        effective_bindings([layer(_Taker, {"made": value})], default_nodes([_Taker]))


def test_replaced_bad_value_is_not_validated_but_lower_input_names_are():
    bindings = effective_bindings(
        [layer(_Taker, {"made": False}), layer(_Taker, {"made": "__maker.made"}, "API")],
        default_nodes([_Taker]),
    )
    assert bindings[NodeKey(_Taker.name)]["made"].references == (
        ProducerRef(NodeKey(_Maker.name), "made"),
    )
    with pytest.raises(FlowSettingsException, match="the design file.*Made.*input"):
        effective_bindings(
            [layer(_Taker, {"Made": False}), layer(_Taker, {"made": "__maker.made"}, "API")],
            default_nodes([_Taker]),
        )


def test_required_and_optional_empty_many_bindings():
    with pytest.raises(FlowSettingsException, match="files.*empty"):
        effective_bindings([layer(_ChainConsumer, {"files": []})], default_nodes([_ChainConsumer]))
    selected = effective_bindings(
        [layer(_ChainOptionalConsumer, {"files": []})], default_nodes([_ChainOptionalConsumer])
    )
    assert selected[NodeKey(_ChainOptionalConsumer.name)]["files"].references == ()


def test_unreached_plugin_sections_remain_tolerant():
    settings, bindings = split_bindings(
        {"unavailable_plugin": {"inputs": {"unknown": False}, "custom": 8}}, location="design.yaml"
    )
    assert settings == {"unavailable_plugin": {"custom": 8}}
    assert effective_bindings([bindings], default_nodes([_Taker])) == {}


def test_unreached_inputs_shape_is_tolerated_until_its_consumer_is_reached():
    settings, bindings = split_bindings({_Taker.name: {"inputs": False}}, location="design.yaml")
    assert settings == {_Taker.name: {}}
    assert effective_bindings([bindings], default_nodes([_Maker])) == {}
    with pytest.raises(FlowSettingsException, match="design.yaml.*inputs.*mapping"):
        effective_bindings([bindings], default_nodes([_Taker]))


CHAIN_EDGE = (ProducerRef(NodeKey("__chain_simple_producer"), "netlist"),)


@pytest.mark.parametrize("origin", ["CLI", "API"])
@pytest.mark.parametrize("reference", ["__chain_simple_producer.netlist", "chain_source.json_a"])
def test_command_line_and_api_collisions_precede_precedence(origin, reference):
    """A chain collides with a command-line or API binding of the same
    input, equal or not, before layer precedence or value validation."""
    request = parse_request("__chain_simple_producer+__chain_simple_consumer")
    layers = [
        layer(_ChainSimpleConsumer, {"netlist": False}, "design.yaml"),
        layer(_ChainSimpleConsumer, {"netlist": reference}, origin),
    ]
    with pytest.raises(FlowSettingsException) as error:
        effective_bindings(layers, default_nodes([_ChainSimpleConsumer]), request=request)
    message = str(error.value)
    assert "chain position 1" in message and "2" in message
    assert origin in message and "inputs.netlist" in message


@pytest.mark.parametrize("origin", ["design.yaml", "project.yaml"])
@pytest.mark.parametrize("reference", ["__chain_simple_producer.netlist", "chain_source.json_a"])
def test_pc1_a_chain_overrides_a_file_binding_of_the_same_input(origin, reference):
    """A chain is command-line data: it wins over a saved binding, which is reported as
    overridden; a file binding of another input still applies, and a replaced value is not
    validated."""
    request = parse_request("__chain_simple_producer+__chain_simple_consumer")
    layers = [
        layer(_ChainSimpleConsumer, {"netlist": False}, "an earlier file"),
        layer(_ChainSimpleConsumer, {"netlist": reference}, origin),
    ]
    bound = effective_bindings(layers, default_nodes([_ChainSimpleConsumer]), request=request)
    netlist = bound[NodeKey(_ChainSimpleConsumer.name)]["netlist"]
    assert netlist.references == CHAIN_EDGE and netlist.origin == "chain"
    assert "chain position 1" in netlist.location
    assert [origin in location for location in netlist.overridden] == [False, True]
    assert all("inputs.netlist" in location for location in netlist.overridden)
    data = bound[NodeKey(_ChainSimpleConsumer.name)]["data"]
    assert data.origin == "chain" and data.overridden == ()


def test_aliases_are_canonicalized_and_duplicate_sections_still_fail():
    settings, bindings = split_bindings(
        {"_Taker": {"inputs": {"made": "__maker.made"}}}, location="API"
    )
    assert settings == {_Taker.name: {}}
    assert NodeKey(_Taker.name) in effective_bindings([bindings], default_nodes([_Taker]))
    with pytest.raises(ValueError, match="twice"):
        split_bindings({"chain_source": {}, "_ChainProducer": {}}, location="design.yaml")


def test_same_cli_input_in_unqualified_and_qualified_forms_is_an_error_even_when_equal():
    with pytest.raises(FlowSettingsError, match="inputs.made.*flows.*inputs.made"):
        split_flow_sections(
            {"inputs.made": "__maker.made", f"flows.{_Taker.name}.inputs.made": "__maker.made"},
            _Taker.name,
            lambda name: registered_flows[name][1],
        )


def test_yaml_project_design_cli_and_api_layers_are_captured_before_composition(tmp_path):
    project = tmp_path / "xedaproject.yaml"
    project.write_text(
        yaml.safe_dump({"flows": {_Taker.name: {"inputs": {"made": "__maker"}, "quiet": False}}})
    )
    design = tmp_path / "design.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "binding_design",
                "rtl": {"sources": []},
                "flows": {_Taker.name: {"inputs": {"made": "__maker.made"}}},
            }
        )
    )
    runner = DefaultRunner(tmp_path / "runs")
    request = runner._request(
        _Taker, design, str(project), {"inputs.made": "__maker"}, {"inputs.made": "__maker.made"}
    )
    assert "inputs" not in request.settings
    assert all("inputs" not in section for section in request.sections.values())
    assert len(request.binding_layers) == 4
    binding = effective_bindings(request.binding_layers, default_nodes([_Taker]))[
        NodeKey(_Taker.name)
    ]["made"]
    assert binding.location.startswith("the API:")
    assert str(project) in request.binding_layers[0].location
    assert str(design) in request.binding_layers[1].location
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("origin", ["CLI", "design", "project", "API"])
@pytest.mark.parametrize("reference", ["__chain_simple_producer.netlist", "chain_source.json_a"])
def test_pc1_is_checked_at_the_request_boundary(tmp_path, origin, reference):
    bindings = {_ChainSimpleConsumer.name: {"inputs": {"netlist": reference}}}
    design = Design(name="d", rtl={"sources": []}, flows=bindings if origin == "design" else {})
    project = tmp_path / "project.yaml"
    project.write_text(yaml.safe_dump({"flows": bindings if origin == "project" else {}}))
    runner = DefaultRunner(tmp_path / "runs")
    label = {"CLI": "command line", "design": "design d", "project": str(project), "API": "API"}[
        origin
    ]

    def request():
        return runner._request(
            parse_request("__chain_simple_producer+__chain_simple_consumer"),
            design,
            str(project),
            {"inputs.netlist": reference} if origin == "CLI" else {},
            {"inputs.netlist": reference} if origin == "API" else {},
        )

    if origin in ("CLI", "API"):
        with pytest.raises(FlowSettingsException, match="chain position") as error:
            request()
        assert label in str(error.value)
        return
    captured = request()
    bound = effective_bindings(
        captured.binding_layers,
        default_nodes([_ChainSimpleConsumer]),
        request=captured.flow_request,
    )
    netlist = bound[NodeKey(_ChainSimpleConsumer.name)]["netlist"]
    assert netlist.references == CHAIN_EDGE and netlist.origin == "chain"
    assert len(netlist.overridden) == 1 and label in netlist.overridden[0]


@pytest.mark.parametrize("entry", ["run", "plan", "run_flow", "resolve"])
def test_a_reached_binding_selects_the_edge_on_every_entry_path(tmp_path, entry):
    """Every local entry hands its captured binding layers to the resolver: the bound
    producer replaces the default one, and nothing is silently ignored."""
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    source = tmp_path / "in.txt"
    source.write_text("bound\n")
    design = Design(name="d", rtl={"sources": []})
    sections = {
        _Taker.name: {"inputs": {"made": "__input_maker.made"}},
        "__input_maker": {"input_file": str(source)},
    }
    if entry in ("run_flow", "resolve"):
        result = getattr(runner, entry)(_Taker, design, {}, all_flows_settings=sections)
    else:
        result = getattr(runner, entry)(
            _Taker,
            design,
            flow_settings={
                "inputs.made": "__input_maker.made",
                "flows.__input_maker.input_file": str(source),
            },
        )
    if entry in ("plan", "resolve"):
        assert [node.name for node in result.nodes] == ["__input_maker", "__taker"]
        assert not (tmp_path / "runs").exists()
    else:
        assert result.succeeded and result.results["read"] == "bound\n"
        assert [flow.name for flow in runner.launched] == ["__input_maker", "__taker"]


def test_captured_layers_can_be_transported_to_existing_dse_workers(tmp_path):
    runner = DefaultRunner(tmp_path / "runs")
    design = Design(
        name="d",
        rtl={"sources": []},
        flows={"unavailable_plugin": {"inputs": {"unknown": {"invalid": True}}}},
    )
    request = runner._request(_Maker, design, flow_settings={"quiet": True})
    restored = pickle.loads(pickle.dumps(request))
    assert restored.binding_layers == request.binding_layers
    with pytest.raises(TypeError):
        restored.binding_layers[2].settings[_Maker.name]["quiet"] = False


def test_embedded_yaml_design_binding_names_its_project_file_and_design(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "designs": [
                    {
                        "name": "embedded",
                        "rtl": {"sources": []},
                        "flows": {_Taker.name: {"inputs": {"made": "__maker.made"}}},
                    }
                ]
            }
        )
    )
    request = DefaultRunner(tmp_path / "runs")._request(_Taker, "embedded", str(project))
    binding = effective_bindings(request.binding_layers, default_nodes([_Taker]))[
        NodeKey(_Taker.name)
    ]["made"]
    assert str(project) in binding.location and "design embedded" in binding.location


def test_binding_capture_keeps_sources_and_design_hash_unchanged(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("source data\n")
    design = Design(name="d", rtl={"sources": [{"file": str(source), "type": "Data"}]})
    before = design.rtl_hash
    request = DefaultRunner(tmp_path / "runs")._request(
        _Taker, design, flow_settings={"inputs.made": "__maker.made"}
    )
    assert effective_bindings(request.binding_layers, default_nodes([_Taker]))[
        NodeKey(_Taker.name)
    ]["made"].references == (
        ProducerRef(NodeKey(_Maker.name), "made"),
    )
    assert request.design.rtl.sources[0].path == source
    assert request.design.rtl_hash == before == design.rtl_hash


def test_node_keys_are_immutable_hashable_and_pickle_for_dse_transport():
    plain = NodeKey(_Taker.name)
    assert plain.instance is None
    assert plain == NodeKey(_Taker.name) and hash(plain) == hash(NodeKey(_Taker.name))
    assert plain != NodeKey(_Taker.name, "second")
    assert len({plain, NodeKey(_Taker.name), NodeKey(_Taker.name, "second")}) == 2
    with pytest.raises(FrozenInstanceError):
        plain.instance = "other"
    keyed = NodeKey(_Taker.name, "second")
    assert pickle.loads(pickle.dumps(keyed)) == keyed
    ref = ProducerRef(NodeKey(_Maker.name, 1), "made")
    assert pickle.loads(pickle.dumps(ref)) == ref


def test_two_reached_instances_of_one_flow_keep_distinct_bindings():
    first, second = NodeKey(_Taker.name, "first"), NodeKey(_Taker.name, "second")

    def entry(node, reference, location):
        return BindingEntry(node, "made", reference, False, location)

    layers = [
        BindingLayer("design", (entry(first, "__maker.made", "design: first"),), {}),
        BindingLayer("CLI", (entry(second, "__maker.made", "CLI: second"),), {}),
        BindingLayer("API", (entry(second, "__maker", "API: second"),), {}),
    ]
    selected = effective_bindings(layers, [(first, _Taker), (second, _Taker)])
    assert set(selected) == {first, second}
    assert selected[first]["made"].references == (ProducerRef(NodeKey(_Maker.name), "made"),)
    assert selected[first]["made"].location == "design: first"
    assert selected[second]["made"].references == (ProducerRef(NodeKey(_Maker.name), None),)
    assert selected[second]["made"].location == "API: second"

    # An entry for the default (None) node does not bind an instance of that flow.
    plain = BindingLayer("design", (entry(NodeKey(_Taker.name), "__maker.made", "plain"),), {})
    assert effective_bindings([plain], [(first, _Taker), (second, _Taker)]) == {}
    with pytest.raises(ValueError, match="twice"):
        effective_bindings([plain], [(first, _Taker), (first, _Taker)])
