"""Canonical parsing and structural matching for flow-chain requests."""

import pytest

from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsException, registered_flows
from xeda.flow.io import is_declared
from xeda.flow_runner import FlowNotFoundError
from xeda.flow_runner.chains import ChainElement, FlowRequest, match_required_inputs, parse_request
from xeda.flow_runner.chains import validate_chain

from .io_flows import (
    _ChainAction,
    _ChainAmbiguousConsumer,
    _ChainAmbiguousDefault,
    _ChainOptionalConsumer,
    _ChainProducer,
    _ChainSimpleConsumer,
    _ChainSimpleProducer,
    _ChainUndeclared,
)


@pytest.fixture(autouse=True)
def isolate_test_flow_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def test_parse_normalizes_names_and_keeps_a_single_element_request():
    request = parse_request("_ChainProducer.json_a+_ChainAmbiguousDefault")

    assert isinstance(request, FlowRequest)
    assert request.elements == (
        ChainElement(_ChainProducer, "json_a"),
        ChainElement(_ChainAmbiguousDefault),
    )
    assert request.requested is _ChainAmbiguousDefault
    assert parse_request("Chain-Source").requested is _ChainProducer
    assert parse_request("_ChainSimpleConsumer").elements == (ChainElement(_ChainSimpleConsumer),)


@pytest.mark.parametrize(
    "text, message",
    [
        ("+_ChainSimpleConsumer", "element 1 is empty"),
        ("_ChainSimpleConsumer+", "element 2 is empty"),
        ("_ChainSimpleProducer++_ChainSimpleConsumer", "element 2 is empty"),
        ("_ChainSimpleProducer..data", "output qualifier"),
        ("_ChainSimpleProducer.data.extra", "output qualifier"),
        ("_ChainSimpleProducer+_ChainSimpleConsumer.netlist", "last element"),
    ],
)
def test_invalid_grammar_is_reported_with_position_or_qualifier(text, message):
    with pytest.raises(FlowSettingsException, match=message):
        parse_request(text)


def test_unknown_flow_and_output_have_suggestions():
    with pytest.raises(FlowNotFoundError, match="chain_simple_consumer"):
        parse_request("_ChainSimpleProducer+__chain_simple_consmer")

    with pytest.raises(FlowSettingsException, match="json_a.*json_b"):
        match_required_inputs(_ChainProducer, _ChainAmbiguousConsumer, output="jsn_a")


def test_repeated_flows_are_rejected_after_name_normalization():
    with pytest.raises(FlowSettingsException, match="__chain_simple_producer.*more than once"):
        parse_request("__chain_simple_producer+__chain-simple-producer")


def test_adjacency_binds_every_required_compatible_input_in_declaration_order():
    assert match_required_inputs(_ChainSimpleProducer, _ChainSimpleConsumer) == [
        ("netlist", "netlist"),
        ("data", "data"),
    ]


def test_adjacency_skips_optional_inputs_and_respects_a_default_output():
    assert match_required_inputs(_ChainProducer, _ChainOptionalConsumer) == []
    assert match_required_inputs(_ChainProducer, _ChainAmbiguousDefault) == [("netlist", "json_b")]


def test_ambiguous_and_qualified_outputs_are_checked():
    with pytest.raises(FlowSettingsException, match="ambiguous.*json_a.*json_b"):
        match_required_inputs(_ChainProducer, _ChainAmbiguousConsumer)

    assert match_required_inputs(_ChainProducer, _ChainAmbiguousDefault, output="json_a") == [
        ("netlist", "json_a")
    ]

    assert match_required_inputs(_ChainProducer, _ChainAmbiguousDefault, output="json_b") == [
        ("netlist", "json_b")
    ]


def test_many_output_cannot_feed_scalar_input():
    with pytest.raises(FlowSettingsException, match="cardinality"):
        match_required_inputs(_ChainProducer, _ChainAmbiguousConsumer, output="many_json")


def test_undeclared_flows_are_allowed_alone_but_not_in_a_chain():
    assert parse_request("_ChainUndeclared").requested is _ChainUndeclared
    with pytest.raises(FlowSettingsException, match="declared I/O"):
        validate_chain((ChainElement(_ChainUndeclared), ChainElement(_ChainAction)))


def test_actions_can_only_appear_at_the_end():
    assert parse_request("_ChainSimpleProducer+_ChainAction").requested is _ChainAction
    with pytest.raises(FlowSettingsException, match="only appear at the end"):
        validate_chain((ChainElement(_ChainAction), ChainElement(_ChainSimpleConsumer)))


def test_parsing_and_matching_do_not_construct_flows_or_touch_a_run_root(monkeypatch, tmp_path):
    def forbidden_init(*args, **kwargs):
        raise AssertionError("flow construction is not part of static chain validation")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)

    request = parse_request("_ChainSimpleProducer+_ChainSimpleConsumer")
    assert match_required_inputs(_ChainSimpleProducer, request.requested) == [
        ("netlist", "netlist"),
        ("data", "data"),
    ]
    assert not list(tmp_path.iterdir())


def test_selected_type_narrowing_requires_produced_types_to_fit_consumer():
    with pytest.raises(FlowSettingsException, match="compatible"):
        match_required_inputs(
            _ChainProducer,
            _ChainAmbiguousDefault,
            output="json_b",
            selected_output_types={"json_b": (SourceType.Data,)},
        )


def test_chain_fixture_flows_have_declarations():
    assert is_declared(_ChainProducer)
