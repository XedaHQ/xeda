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
from xeda.flow_runner.bindings import ProducerRef, effective_bindings, split_bindings
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.settings_layers import split_flow_sections

from .io_flows import (
    _ChainConsumer,
    _ChainOptionalConsumer,
    _ChainProducer,
    _ChainSimpleConsumer,
    _Maker,
    _Place,
    _Synth,
    _Taker,
)


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def layer(cls, inputs, location="the design file"):
    return split_bindings({cls.name: {"inputs": inputs}}, location=location)[1]


def test_split_copies_settings_and_captures_immutable_binding_data():
    sections = {_ChainConsumer.name: {"quiet": True, "inputs": {"files": ["chain_source.files"]}}}
    before = deepcopy(sections)
    settings, bindings = split_bindings(sections, location="project.yaml")
    sections[_ChainConsumer.name]["inputs"]["files"].append("__maker.made")

    assert settings == {_ChainConsumer.name: {"quiet": True}}
    assert before[_ChainConsumer.name]["inputs"]["files"] == ["chain_source.files"]
    selected = effective_bindings([bindings], [_ChainConsumer])
    binding = selected[_ChainConsumer.name]["files"]
    assert binding.references == (ProducerRef(_ChainProducer.name, "files"),)
    assert binding.location == f"project.yaml: flows.{_ChainConsumer.name}.inputs.files"
    with pytest.raises(FrozenInstanceError):
        binding.references[0].node = "other"
    with pytest.raises(TypeError):
        selected[_ChainConsumer.name]["files"] = binding


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
    bindings = effective_bindings(layers, [_ChainConsumer])[_ChainConsumer.name]
    assert bindings["left"].references == (ProducerRef(_ChainProducer.name, "json_b"),)
    assert bindings["right"].references == (ProducerRef(_ChainProducer.name, "json_b"),)
    assert bindings["files"].references == (
        ProducerRef(_ChainProducer.name, "data"),
        ProducerRef(_ChainProducer.name, "files"),
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
        effective_bindings([layer(_Taker, {"made": value}, "design.yaml")], [_Taker])


@pytest.mark.parametrize("value", [True, [], None, "__maker"])
def test_inputs_requires_a_mapping(value):
    _, bindings = split_bindings({_Taker.name: {"inputs": value}}, location="project.yaml")
    with pytest.raises(FlowSettingsException, match="project.yaml.*inputs.*mapping"):
        effective_bindings([bindings], [_Taker])


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
        effective_bindings([layer(_Taker, {"made": value})], [_Taker])


def test_replaced_bad_value_is_not_validated_but_lower_input_names_are():
    bindings = effective_bindings(
        [layer(_Taker, {"made": False}), layer(_Taker, {"made": "__maker.made"}, "API")],
        [_Taker],
    )
    assert bindings[_Taker.name]["made"].references == (ProducerRef(_Maker.name, "made"),)
    with pytest.raises(FlowSettingsException, match="the design file.*Made.*input"):
        effective_bindings(
            [layer(_Taker, {"Made": False}), layer(_Taker, {"made": "__maker.made"}, "API")],
            [_Taker],
        )


def test_required_and_optional_empty_many_bindings():
    with pytest.raises(FlowSettingsException, match="files.*empty"):
        effective_bindings([layer(_ChainConsumer, {"files": []})], [_ChainConsumer])
    selected = effective_bindings(
        [layer(_ChainOptionalConsumer, {"files": []})], [_ChainOptionalConsumer]
    )
    assert selected[_ChainOptionalConsumer.name]["files"].references == ()


def test_unreached_plugin_sections_remain_tolerant():
    settings, bindings = split_bindings(
        {"unavailable_plugin": {"inputs": {"unknown": False}, "custom": 8}}, location="design.yaml"
    )
    assert settings == {"unavailable_plugin": {"custom": 8}}
    assert effective_bindings([bindings], [_Taker]) == {}


def test_unreached_inputs_shape_is_tolerated_until_its_consumer_is_reached():
    settings, bindings = split_bindings({_Taker.name: {"inputs": False}}, location="design.yaml")
    assert settings == {_Taker.name: {}}
    assert effective_bindings([bindings], [_Maker]) == {}
    with pytest.raises(FlowSettingsException, match="design.yaml.*inputs.*mapping"):
        effective_bindings([bindings], [_Taker])


@pytest.mark.parametrize("origin", ["CLI", "design.yaml", "project.yaml", "API"])
@pytest.mark.parametrize("reference", ["__chain_simple_producer.netlist", "chain_source.json_a"])
def test_pc1_collisions_precede_precedence_for_all_origins(origin, reference):
    request = parse_request("__chain_simple_producer+__chain_simple_consumer")
    layers = [
        layer(_ChainSimpleConsumer, {"netlist": reference}, origin),
        layer(_ChainSimpleConsumer, {"netlist": False}, "higher API"),
    ]
    with pytest.raises(FlowSettingsException) as error:
        effective_bindings(layers, [_ChainSimpleConsumer], request=request)
    message = str(error.value)
    assert "chain position 1" in message and "2" in message
    assert origin in message and "inputs.netlist" in message


def test_aliases_are_canonicalized_and_duplicate_sections_still_fail():
    settings, bindings = split_bindings(
        {"_Taker": {"inputs": {"made": "__maker.made"}}}, location="API"
    )
    assert settings == {_Taker.name: {}}
    assert _Taker.name in effective_bindings([bindings], [_Taker])
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
    binding = effective_bindings(request.binding_layers, [_Taker])[_Taker.name]["made"]
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
    with pytest.raises(FlowSettingsException, match="chain position") as error:
        runner._request(
            parse_request("__chain_simple_producer+__chain_simple_consumer"),
            design,
            str(project),
            {"inputs.netlist": reference} if origin == "CLI" else {},
            {"inputs.netlist": reference} if origin == "API" else {},
        )
    assert {"CLI": "command line", "design": "design d", "project": str(project), "API": "API"}[
        origin
    ] in str(error.value)


@pytest.mark.parametrize("entry", ["run", "plan", "run_flow", "resolve"])
def test_reached_bindings_stop_before_the_pending_resolver_integration(tmp_path, entry):
    runner = DefaultRunner(tmp_path / "runs")
    design = Design(name="d", rtl={"sources": []})
    with pytest.raises(FlowSettingsException, match="binding.*resolver integration"):
        if entry in ("run_flow", "resolve"):
            getattr(runner, entry)(
                _Taker,
                design,
                {},
                all_flows_settings={_Taker.name: {"inputs": {"made": "__maker.made"}}},
            )
        else:
            getattr(runner, entry)(_Taker, design, flow_settings={"inputs.made": "__maker.made"})
    assert not (tmp_path / "runs").exists()


def test_nested_default_settings_are_allowed_only_for_a_default_equivalent_binding():
    settings, default = split_bindings(
        {_Place.name: {"inputs": {"netlist": "__synth.netlist"}, "synth": {"quiet": True}}},
        location="design.yaml",
    )
    assert settings[_Place.name]["synth"] == {"quiet": True}
    effective_bindings([default], [_Place])
    _, alternate = split_bindings(
        {
            _Place.name: {
                "inputs": {"netlist": "__chain_simple_producer.netlist"},
                "synth": {"quiet": True},
            }
        },
        location="design.yaml",
    )
    with pytest.raises(FlowSettingsException, match="design.yaml.*synth.*default producer"):
        effective_bindings([alternate], [_Place])


def test_model_defaults_and_separate_producer_sections_are_not_explicit_nested_settings():
    model = _Place.Settings()
    _, defaults = split_bindings({_Place.name: model, _Synth.name: {"quiet": True}}, location="API")
    alternate = layer(_Place, {"netlist": "__chain_simple_producer.netlist"})
    effective_bindings([defaults, alternate], [_Place])
    model.synth.quiet = True
    _, edited = split_bindings({_Place.name: model}, location="API")
    with pytest.raises(FlowSettingsException, match="API.*synth.*default producer"):
        effective_bindings([edited, alternate], [_Place])


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
    binding = effective_bindings(request.binding_layers, [_Taker])[_Taker.name]["made"]
    assert str(project) in binding.location and "design embedded" in binding.location


def test_binding_capture_keeps_sources_and_design_hash_unchanged(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("source data\n")
    design = Design(name="d", rtl={"sources": [{"file": str(source), "type": "Data"}]})
    before = design.rtl_hash
    request = DefaultRunner(tmp_path / "runs")._request(
        _Taker, design, flow_settings={"inputs.made": "__maker.made"}
    )
    assert effective_bindings(request.binding_layers, [_Taker])[_Taker.name]["made"].references == (
        ProducerRef(_Maker.name, "made"),
    )
    assert request.design.rtl.sources[0].path == source
    assert request.design.rtl_hash == before == design.rtl_hash


def test_request_checks_edited_nested_model_defaults_as_supplied_settings(tmp_path):
    model = _Place.Settings()
    runner = DefaultRunner(tmp_path / "runs")
    design = Design(name="d", rtl={"sources": []})
    runner._request(
        _Place,
        design,
        flow_settings=model,
        flow_overrides={"inputs.netlist": "__chain_simple_producer.netlist"},
    )
    model.synth.quiet = True
    with pytest.raises(FlowSettingsException, match="command line.*synth.*default producer"):
        runner._request(
            _Place,
            design,
            flow_settings=model,
            flow_overrides={"inputs.netlist": "__chain_simple_producer.netlist"},
        )
