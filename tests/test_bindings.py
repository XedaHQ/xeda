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

from .flow_registry import preserved_flow_registry
from .io_flows import (
    TChainConsumer,
    TChainOptionalConsumer,
    TChainProducer,
    TChainSimpleConsumer,
    TMaker,
    TTaker,
)


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def layer(cls, inputs, location="the design file"):
    kind = "cli" if "CLI" in location else "api" if "API" in location else "file"
    return split_bindings({cls.name: {"inputs": inputs}}, location=location, kind=kind)[1]


def test_split_copies_settings_and_captures_immutable_binding_data():
    sections = {
        TChainConsumer.name: {"nthreads": 2, "inputs": {"files": ["t_chain_producer.files"]}}
    }
    before = deepcopy(sections)
    settings, bindings = split_bindings(sections, location="project.yaml")
    sections[TChainConsumer.name]["inputs"]["files"].append("t_maker.made")

    assert settings == {TChainConsumer.name: {"nthreads": 2}}
    assert before[TChainConsumer.name]["inputs"]["files"] == ["t_chain_producer.files"]
    selected = effective_bindings([bindings], default_nodes([TChainConsumer]))
    binding = selected[NodeKey(TChainConsumer.name)]["files"]
    assert binding.references == (ProducerRef(NodeKey(TChainProducer.name), "files"),)
    assert binding.location == f"project.yaml: flows.{TChainConsumer.name}.inputs.files"
    with pytest.raises(FrozenInstanceError):
        binding.references[0].node = "other"
    with pytest.raises(TypeError):
        selected[NodeKey(TChainConsumer.name)]["files"] = binding


def test_binding_precedence_merges_input_names_and_replaces_ordered_lists():
    layers = [
        layer(
            TChainConsumer,
            {"left": "t_chain_producer.json_a", "files": ["t_maker.made"]},
            "project",
        ),
        layer(
            TChainConsumer,
            {"right": "t_chain_producer.json_b", "files": ["t_chain_producer.files"]},
            "design",
        ),
        layer(TChainConsumer, {"left": "t_chain_producer.json_b"}, "CLI"),
        layer(
            TChainConsumer, {"files": ["t_chain_producer.data", "t_chain_producer.files"]}, "API"
        ),
    ]
    bindings = effective_bindings(layers, default_nodes([TChainConsumer]))[
        NodeKey(TChainConsumer.name)
    ]
    assert bindings["left"].references == (ProducerRef(NodeKey(TChainProducer.name), "json_b"),)
    assert bindings["right"].references == (ProducerRef(NodeKey(TChainProducer.name), "json_b"),)
    assert bindings["files"].references == (
        ProducerRef(NodeKey(TChainProducer.name), "data"),
        ProducerRef(NodeKey(TChainProducer.name), "files"),
    )
    assert bindings["files"].location.startswith("API:")


@pytest.mark.parametrize(
    "value",
    [
        True,
        7,
        None,
        Path("made.txt"),
        {"node": "t_maker"},
        "a/b",
        "t_maker..made",
        "t_maker+t_taker",
        "",
    ],
)
def test_winning_invalid_reference_is_rejected_at_its_location(value):
    with pytest.raises(FlowSettingsException, match=r"design.yaml.*inputs.made"):
        effective_bindings([layer(TTaker, {"made": value}, "design.yaml")], default_nodes([TTaker]))


@pytest.mark.parametrize("value", [True, [], None, "t_maker"])
def test_inputs_requires_a_mapping(value):
    _, bindings = split_bindings({TTaker.name: {"inputs": value}}, location="project.yaml")
    with pytest.raises(FlowSettingsException, match="project.yaml.*inputs.*mapping"):
        effective_bindings([bindings], default_nodes([TTaker]))


@pytest.mark.parametrize(
    "value, message",
    [
        ("missing_producer.made", "producer"),
        ("t_maker.Made", "output.*Made"),
        (["t_maker.made"], "one.*list"),
    ],
)
def test_strict_reference_names_and_scalar_cardinality(value, message):
    with pytest.raises(FlowSettingsException, match=message):
        effective_bindings([layer(TTaker, {"made": value})], default_nodes([TTaker]))


def test_replaced_bad_value_is_not_validated_but_lower_input_names_are():
    bindings = effective_bindings(
        [layer(TTaker, {"made": False}), layer(TTaker, {"made": "t_maker.made"}, "API")],
        default_nodes([TTaker]),
    )
    assert bindings[NodeKey(TTaker.name)]["made"].references == (
        ProducerRef(NodeKey(TMaker.name), "made"),
    )
    with pytest.raises(FlowSettingsException, match="the design file.*Made.*input"):
        effective_bindings(
            [layer(TTaker, {"Made": False}), layer(TTaker, {"made": "t_maker.made"}, "API")],
            default_nodes([TTaker]),
        )


def test_required_and_optional_empty_many_bindings():
    with pytest.raises(FlowSettingsException, match="files.*empty"):
        effective_bindings([layer(TChainConsumer, {"files": []})], default_nodes([TChainConsumer]))
    selected = effective_bindings(
        [layer(TChainOptionalConsumer, {"files": []})], default_nodes([TChainOptionalConsumer])
    )
    assert selected[NodeKey(TChainOptionalConsumer.name)]["files"].references == ()


def test_unreached_plugin_sections_remain_tolerant():
    settings, bindings = split_bindings(
        {"unavailable_plugin": {"inputs": {"unknown": False}, "custom": 8}}, location="design.yaml"
    )
    assert settings == {"unavailable_plugin": {"custom": 8}}
    assert effective_bindings([bindings], default_nodes([TTaker])) == {}


def test_unreached_inputs_shape_is_tolerated_until_its_consumer_is_reached():
    settings, bindings = split_bindings({TTaker.name: {"inputs": False}}, location="design.yaml")
    assert settings == {TTaker.name: {}}
    assert effective_bindings([bindings], default_nodes([TMaker])) == {}
    with pytest.raises(FlowSettingsException, match="design.yaml.*inputs.*mapping"):
        effective_bindings([bindings], default_nodes([TTaker]))


CHAIN_EDGE = (ProducerRef(NodeKey("t_chain_simple_producer"), "netlist"),)


@pytest.mark.parametrize("origin", ["CLI", "API"])
@pytest.mark.parametrize(
    "reference", ["t_chain_simple_producer.netlist", "t_chain_producer.json_a"]
)
def test_command_line_and_api_collisions_precede_precedence(origin, reference):
    """A chain collides with a command-line or API binding of the same
    input, equal or not, before layer precedence or value validation."""
    request = parse_request("t_chain_simple_producer+t_chain_simple_consumer")
    layers = [
        layer(TChainSimpleConsumer, {"netlist": False}, "design.yaml"),
        layer(TChainSimpleConsumer, {"netlist": reference}, origin),
    ]
    with pytest.raises(FlowSettingsException) as error:
        effective_bindings(layers, default_nodes([TChainSimpleConsumer]), request=request)
    message = str(error.value)
    assert "chain position 1" in message and "2" in message
    assert origin in message and "inputs.netlist" in message


@pytest.mark.parametrize("origin", ["design.yaml", "project.yaml"])
@pytest.mark.parametrize(
    "reference", ["t_chain_simple_producer.netlist", "t_chain_producer.json_a"]
)
def test_pc1_a_chain_overrides_a_file_binding_of_the_same_input(origin, reference):
    """A chain is command-line data: it wins over a saved binding, which is reported as
    overridden; a file binding of another input still applies, and a replaced value is not
    validated."""
    request = parse_request("t_chain_simple_producer+t_chain_simple_consumer")
    layers = [
        layer(TChainSimpleConsumer, {"netlist": False}, "an earlier file"),
        layer(TChainSimpleConsumer, {"netlist": reference}, origin),
    ]
    bound = effective_bindings(layers, default_nodes([TChainSimpleConsumer]), request=request)
    netlist = bound[NodeKey(TChainSimpleConsumer.name)]["netlist"]
    assert netlist.references == CHAIN_EDGE and netlist.origin == "chain"
    assert "chain position 1" in netlist.location
    assert [origin in location for location in netlist.overridden] == [False, True]
    assert all("inputs.netlist" in location for location in netlist.overridden)
    data = bound[NodeKey(TChainSimpleConsumer.name)]["data"]
    assert data.origin == "chain" and data.overridden == ()


def test_same_cli_input_in_unqualified_and_qualified_forms_is_an_error_even_when_equal():
    with pytest.raises(FlowSettingsError, match="inputs.made.*flows.*inputs.made"):
        split_flow_sections(
            {"inputs.made": "t_maker.made", f"flows.{TTaker.name}.inputs.made": "t_maker.made"},
            TTaker.name,
            lambda name: registered_flows[name][1],
        )


def test_yaml_project_design_cli_and_api_layers_are_captured_before_composition(tmp_path):
    project = tmp_path / "xedaproject.yaml"
    design = tmp_path / "design.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "binding_design",
                "rtl": {"sources": []},
                "flows": {TTaker.name: {"inputs": {"made": "t_maker.made"}}},
            }
        )
    )
    project.write_text(
        yaml.safe_dump(
            {
                "designs": [{"file": "design.yaml"}],
                "flows": {TTaker.name: {"inputs": {"made": "t_maker"}, "nthreads": 3}},
            }
        )
    )
    runner = DefaultRunner(tmp_path / "runs")
    request = runner._request(
        TTaker,
        "binding_design",
        str(project),
        {"inputs.made": "t_maker"},
        {"inputs.made": "t_maker.made"},
    )
    assert "inputs" not in request.settings
    assert all("inputs" not in section for section in request.sections.values())
    assert len(request.binding_layers) == 4
    binding = effective_bindings(request.binding_layers, default_nodes([TTaker]))[
        NodeKey(TTaker.name)
    ]["made"]
    assert binding.location.startswith("the API:")
    assert str(project) in request.binding_layers[0].location
    assert str(design) in request.binding_layers[1].location
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("origin", ["CLI", "design", "project", "API"])
@pytest.mark.parametrize(
    "reference", ["t_chain_simple_producer.netlist", "t_chain_producer.json_a"]
)
def test_pc1_is_checked_at_the_request_boundary(tmp_path, origin, reference):
    bindings = {TChainSimpleConsumer.name: {"inputs": {"netlist": reference}}}
    design = Design(
        name="d", root=tmp_path, rtl={"sources": []}, flows=bindings if origin == "design" else {}
    )
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "designs": [{"name": "d", "rtl": {"sources": []}}],
                "flows": bindings if origin == "project" else {},
            }
        )
    )
    runner = DefaultRunner(tmp_path / "runs")
    label = {"CLI": "command line", "design": "design d", "project": str(project), "API": "API"}[
        origin
    ]

    def request():
        return runner._request(
            parse_request("t_chain_simple_producer+t_chain_simple_consumer"),
            "d" if origin == "project" else design,
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
        default_nodes([TChainSimpleConsumer]),
        request=captured.flow_request,
    )
    netlist = bound[NodeKey(TChainSimpleConsumer.name)]["netlist"]
    assert netlist.references == CHAIN_EDGE and netlist.origin == "chain"
    assert len(netlist.overridden) == 1 and label in netlist.overridden[0]


@pytest.mark.parametrize("entry", ["run", "plan", "run_flow", "resolve"])
def test_a_reached_binding_selects_the_edge_on_every_entry_path(tmp_path, entry):
    """Every local entry hands its captured binding layers to the resolver: the bound
    producer replaces the default one, and nothing is silently ignored."""
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    source = tmp_path / "in.txt"
    source.write_text("bound\n")
    design = Design(name="d", root=tmp_path, rtl={"sources": []})
    sections = {
        TTaker.name: {"inputs": {"made": "t_input_maker.made"}},
        "t_input_maker": {"input_file": str(source)},
    }
    if entry in ("run_flow", "resolve"):
        result = getattr(runner, entry)(TTaker, design, {}, all_flows_settings=sections)
    else:
        result = getattr(runner, entry)(
            TTaker,
            design,
            flow_settings={
                "inputs.made": "t_input_maker.made",
                "flows.t_input_maker.input_file": str(source),
            },
        )
    if entry in ("plan", "resolve"):
        assert [node.name for node in result.nodes] == ["t_input_maker", "t_taker"]
        assert not (tmp_path / "runs").exists()
    else:
        assert result.succeeded and result.results["read"] == "bound\n"
        assert [flow.name for flow in runner.launched] == ["t_input_maker", "t_taker"]


def test_captured_layers_can_be_transported_to_existing_dse_workers(tmp_path):
    runner = DefaultRunner(tmp_path / "runs")
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": []},
        flows={"unavailable_plugin": {"inputs": {"unknown": {"invalid": True}}}},
    )
    request = runner._request(TMaker, design, flow_settings={"nthreads": 2})
    restored = pickle.loads(pickle.dumps(request))
    assert restored.binding_layers == request.binding_layers
    with pytest.raises(TypeError):
        restored.binding_layers[2].settings[TMaker.name]["nthreads"] = 3


def test_embedded_yaml_design_binding_names_its_project_file_and_design(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump(
            {
                "designs": [
                    {
                        "name": "embedded",
                        "rtl": {"sources": []},
                        "flows": {TTaker.name: {"inputs": {"made": "t_maker.made"}}},
                    }
                ]
            }
        )
    )
    request = DefaultRunner(tmp_path / "runs")._request(TTaker, "embedded", str(project))
    binding = effective_bindings(request.binding_layers, default_nodes([TTaker]))[
        NodeKey(TTaker.name)
    ]["made"]
    assert str(project) in binding.location and "design embedded" in binding.location


def test_binding_capture_keeps_sources_and_design_hash_unchanged(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("source data\n")
    design = Design(
        name="d", root=tmp_path, rtl={"sources": [{"file": str(source), "type": "Data"}]}
    )
    before = design.rtl_hash
    request = DefaultRunner(tmp_path / "runs")._request(
        TTaker, design, flow_settings={"inputs.made": "t_maker.made"}
    )
    assert effective_bindings(request.binding_layers, default_nodes([TTaker]))[
        NodeKey(TTaker.name)
    ]["made"].references == (
        ProducerRef(NodeKey(TMaker.name), "made"),
    )
    assert request.design.rtl.sources[0].path == source
    assert request.design.rtl_hash == before == design.rtl_hash


def test_node_keys_are_immutable_hashable_and_pickle_for_dse_transport():
    plain = NodeKey(TTaker.name)
    assert plain.instance is None
    assert plain == NodeKey(TTaker.name) and hash(plain) == hash(NodeKey(TTaker.name))
    assert plain != NodeKey(TTaker.name, "second")
    assert len({plain, NodeKey(TTaker.name), NodeKey(TTaker.name, "second")}) == 2
    with pytest.raises(FrozenInstanceError):
        plain.instance = "other"
    keyed = NodeKey(TTaker.name, "second")
    assert pickle.loads(pickle.dumps(keyed)) == keyed
    ref = ProducerRef(NodeKey(TMaker.name, 1), "made")
    assert pickle.loads(pickle.dumps(ref)) == ref


def test_two_reached_instances_of_one_flow_keep_distinct_bindings():
    first, second = NodeKey(TTaker.name, "first"), NodeKey(TTaker.name, "second")

    def entry(node, reference, location):
        return BindingEntry(node, "made", reference, False, location)

    layers = [
        BindingLayer("design", (entry(first, "t_maker.made", "design: first"),), {}),
        BindingLayer("CLI", (entry(second, "t_maker.made", "CLI: second"),), {}),
        BindingLayer("API", (entry(second, "t_maker", "API: second"),), {}),
    ]
    selected = effective_bindings(layers, [(first, TTaker), (second, TTaker)])
    assert set(selected) == {first, second}
    assert selected[first]["made"].references == (ProducerRef(NodeKey(TMaker.name), "made"),)
    assert selected[first]["made"].location == "design: first"
    assert selected[second]["made"].references == (ProducerRef(NodeKey(TMaker.name), None),)
    assert selected[second]["made"].location == "API: second"

    # An entry for the default (None) node does not bind an instance of that flow.
    plain = BindingLayer("design", (entry(NodeKey(TTaker.name), "t_maker.made", "plain"),), {})
    assert effective_bindings([plain], [(first, TTaker), (second, TTaker)]) == {}
    with pytest.raises(ValueError, match="twice"):
        effective_bindings([plain], [(first, TTaker), (first, TTaker)])
