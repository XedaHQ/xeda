"""Canonical parsing and structural matching for flow-chain requests."""

import re

import pytest

from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsException, registered_flows
from xeda.flow.io import is_declared
from xeda.flow_runner import FlowNotFoundError
from xeda.flow_runner.chains import (
    ChainElement,
    FlowRequest,
    match_required_inputs,
    parse_request,
    validate_chain,
)

from . import io_flows
from .io_flows import (
    _ChainAction,
    _ChainAliased,
    _ChainAmbiguousConsumer,
    _ChainAmbiguousDefault,
    _ChainOptionalConsumer,
    _ChainProducer,
    _ChainSimpleConsumer,
    _ChainSimpleProducer,
    _ChainUndeclared,
)

# `io_flows` also registers the `__route_*` and `__loop_*` flows the suggestion tests name.


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
    with pytest.raises(
        FlowSettingsException, match="performs a test action and can only end a chain"
    ):
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


# ------------------------------------------------------------------ validated suggestions


def _suggestions(text: str) -> list[str]:
    """The chains `parse_request` advertises when it refuses `text`, none if it advertises none."""
    with pytest.raises(FlowSettingsException) as raised:
        parse_request(text)
    message = str(raised.value)
    if "Did you mean" not in message:
        return []
    return re.findall(r"`([^`]+)`", message.partition("Did you mean")[2])


@pytest.mark.parametrize(
    "request_text, suggested",
    [
        ("nextpnr+openfpgaloader", "nextpnr+fpga_pack+openfpgaloader"),
        ("yosys_fpga+fpga_pack", "yosys_fpga+nextpnr+fpga_pack"),
        # the prefix and the suffix of the request are kept around the inserted stage
        ("yosys_fpga+nextpnr+openfpgaloader", "yosys_fpga+nextpnr+fpga_pack+openfpgaloader"),
        ("yosys_fpga+fpga_pack+openfpgaloader", "yosys_fpga+nextpnr+fpga_pack+openfpgaloader"),
        # so is the producer's output qualifier
        ("nextpnr.config+openfpgaloader", "nextpnr.config+fpga_pack+openfpgaloader"),
        ("yosys_fpga+openfpgaloader", "yosys_fpga+nextpnr+fpga_pack+openfpgaloader"),
    ],
)
def test_a_missing_stage_suggests_the_full_corrected_request(request_text, suggested):
    assert _suggestions(request_text) == [suggested]
    assert parse_request(suggested).requested.name == suggested.split("+")[-1]


def test_a_missing_stage_is_found_in_the_middle_of_a_longer_request():
    assert _suggestions("__route_a+__route_c") == ["__route_a+__route_b+__route_c"]
    assert _suggestions("__chain_simple_producer+__route_a+__route_c") == []


def test_an_ambiguous_output_suggests_each_qualification_that_validates():
    assert _suggestions("__chain_producer+__chain_ambiguous_consumer") == [
        "__chain_producer.json_a+__chain_ambiguous_consumer",
        "__chain_producer.json_b+__chain_ambiguous_consumer",
    ]
    # the qualifiers of other elements and the rest of the request are kept
    longer = _suggestions("__chain_producer+__chain_ambiguous_consumer+__chain_action")
    assert longer == []  # the action takes Data, which the consumer does not make: no stage fits


def test_a_qualifier_that_does_not_fit_suggests_the_one_that_does():
    # `data` is a Data file; the consumer takes JsonNetlist -- only the json_* outputs fit
    suggestions = _suggestions("__chain_producer.data+__chain_ambiguous_consumer")
    assert suggestions == [
        "__chain_producer.json_a+__chain_ambiguous_consumer",
        "__chain_producer.json_b+__chain_ambiguous_consumer",
    ]


@pytest.mark.parametrize(
    "request_text",
    [
        "__chain_simple_consumer+__chain_producer",  # the target takes nothing
        "__chain_action+__chain_simple_consumer",  # an action ends a chain
        "__chain_undeclared+__chain_action",  # an undeclared flow runs alone
        "__chain_simple_producer+__chain_ambiguous_default+__chain_simple_consumer",
        "__route_a+__loop_a",  # no default route reaches the target
        "__route_a+__route_c+__route_b",  # the only route would repeat a flow of the request
    ],
)
def test_no_compatible_declared_path_means_no_fabricated_suggestion(request_text):
    assert _suggestions(request_text) == []


def test_an_action_before_the_end_is_refused_with_its_reason_and_no_suggestion():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("__chain_action+__chain_simple_consumer")
    assert "performs a test action and can only end a chain" in str(raised.value)
    assert "Did you mean" not in str(raised.value)


def test_default_route_search_terminates_on_a_cycle_of_default_producers():
    # _LoopA and _LoopB are each other's default producer: the search visits each once
    assert _suggestions("__route_a+__loop_a") == []


def test_every_suggestion_is_itself_a_valid_request_with_its_qualifiers():
    for text in (
        "nextpnr+openfpgaloader",
        "nextpnr.config+openfpgaloader",
        "__chain_producer+__chain_ambiguous_consumer",
        "__chain_producer.data+__chain_ambiguous_consumer",
        "__route_a+__route_c",
    ):
        for suggestion in _suggestions(text):
            request = parse_request(suggestion)  # the same validator, and it accepts
            assert [e.output for e in request.elements[:1]] == [
                suggestion.split("+")[0].partition(".")[2] or None
            ]


def test_suggestions_construct_no_flow_and_touch_no_run_root(monkeypatch, tmp_path):
    def forbidden_init(*args, **kwargs):
        raise AssertionError("suggestions are static")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)
    assert _suggestions("nextpnr+openfpgaloader") == ["nextpnr+fpga_pack+openfpgaloader"]
    assert not list(tmp_path.iterdir())


def test_a_suggestion_resolves_with_a_valid_design(tmp_path):
    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    (suggestion,) = _suggestions("nextpnr+openfpgaloader")
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        parse_request(suggestion), design, flow_settings={"fpga": "LFE5U-25F-6BG381C"}
    )
    assert [node.name for node in plan.nodes] == [
        "yosys_fpga",
        "nextpnr",
        "fpga_pack",
        "openfpgaloader",
    ]
    assert plan.requested == "openfpgaloader"


# ------------------------------------------------------- follow relations (shared with listing)


def _classes():
    """The product's flows and this suite's chain fixtures: other test modules register flows of
    their own in the same process, which a relation under test must not depend on."""
    from xeda.introspect import all_flow_classes

    return [
        cls
        for cls in all_flow_classes()
        if cls.__module__.startswith("xeda.flows") or cls.__module__ == io_flows.__name__
    ]


def _pairs(edges, attribute):
    return [(getattr(e, attribute).name, e.output, e.target_dependent) for e in edges]


def test_the_open_fpga_flows_follow_each_other_by_their_declarations():
    from xeda.flow_runner.chains import followers, predecessors
    from xeda.flows import FpgaPack, Nextpnr, Openfpgaloader, YosysFpga

    classes = [c for c in _classes() if c.__module__.startswith("xeda.flows")]
    assert _pairs(followers(YosysFpga, classes), "consumer") == [("nextpnr", None, False)]
    # nextpnr makes a configuration of whichever family the target selects: a hint that
    # depends on the target
    assert _pairs(followers(Nextpnr, classes), "consumer") == [("fpga_pack", None, True)]
    assert _pairs(followers(FpgaPack, classes), "consumer") == [("openfpgaloader", None, False)]
    assert followers(Openfpgaloader, classes) == []
    assert _pairs(predecessors(Openfpgaloader, classes), "producer") == [("fpga_pack", None, False)]
    assert _pairs(predecessors(Nextpnr, classes), "producer") == [("yosys_fpga", None, False)]
    assert predecessors(YosysFpga, classes) == []


def test_follow_relations_come_from_declarations_not_from_a_name_table():
    from xeda.flow_runner.chains import followers, predecessors

    classes = _classes()
    by_consumer = {}
    for edge in followers(_ChainProducer, classes):
        by_consumer.setdefault(edge.consumer, []).append(edge.output)
    # a consumer whose default names an output is followed unqualified; an ambiguous one only
    # through each output that fits
    assert by_consumer[_ChainAmbiguousDefault] == [None]
    assert by_consumer[_ChainAmbiguousConsumer] == ["json_a", "json_b"]
    assert by_consumer[_ChainAliased] == ["json_a", "json_b"]
    assert by_consumer[_ChainAction] == [None]
    assert _ChainUndeclared not in by_consumer and _ChainOptionalConsumer not in by_consumer
    assert followers(_ChainUndeclared, classes) == []
    assert followers(_ChainAction, classes) == []
    assert _ChainProducer in [e.producer for e in predecessors(_ChainAction, classes)]
    assert _ChainAction not in [e.producer for e in predecessors(_ChainSimpleConsumer, classes)]
    edge = next(
        e for e in followers(_ChainSimpleProducer, classes) if e.consumer is _ChainSimpleConsumer
    )
    assert edge.binds == (("netlist", "netlist"), ("data", "data"))


def _accepts(producer, output, consumer):
    try:
        validate_chain((ChainElement(producer, output), ChainElement(consumer)))
    except FlowSettingsException:
        return False
    return True


def test_a_follow_relation_is_exactly_what_the_chain_validator_accepts():
    from xeda.flow.io import declared_outputs
    from xeda.flow_runner.chains import followers

    classes = _classes()
    declared = [c for c in classes if is_declared(c)]
    for producer in declared:
        for consumer in declared:
            edges = [e.output for e in followers(producer, classes) if e.consumer is consumer]
            if _accepts(producer, None, consumer):
                assert edges == [None], (producer.name, consumer.name)
                continue
            qualified = [o for o in declared_outputs(producer) if _accepts(producer, o, consumer)]
            assert edges == qualified, (producer.name, consumer.name)


# ---------------------------------------------------------------------------- completion


@pytest.mark.parametrize(
    "incomplete, expected",
    [
        ("yosys_fpga+ne", ["yosys_fpga+nextpnr"]),
        ("yosys_fpga+nextpnr+", ["yosys_fpga+nextpnr+fpga_pack"]),
        (
            "yosys_fpga+nextpnr+fpga_pack+open",
            ["yosys_fpga+nextpnr+fpga_pack+openfpgaloader"],
        ),
        ("nextpnr+fp", ["nextpnr+fpga_pack"]),
        ("ne", ["nextpnr"]),
        # the prefix is returned as it was typed, whatever spelling it used
        ("yosys-fpga+ne", ["yosys-fpga+nextpnr"]),
        ("YosysFpga+ne", ["YosysFpga+nextpnr"]),
        # no followers: an action ends a chain, nothing repeats, an undeclared flow is alone
        ("openfpgaloader+", []),
        ("fpga_pack+openfpgaloader+", []),
        ("yosys_fpga+nextpnr+n", []),
        ("nextpnr+yosys", []),
        ("__chain_undeclared+", []),
        ("__chain_simple_producer+__chain_action+", []),
        # a qualifier completes to the outputs that lead somewhere
        ("nextpnr.", ["nextpnr.config"]),
        ("yosys_fpga.n", ["yosys_fpga.netlist"]),
        ("yosys_fpga+nextpnr.c", ["yosys_fpga+nextpnr.config"]),
        ("openfpgaloader.", []),
        ("__chain_producer.j", ["__chain_producer.json_a", "__chain_producer.json_b"]),
        # an output-qualified prefix filters the followers through that output
        (
            "__chain_producer.json_a+__chain_a",
            [
                "__chain_producer.json_a+__chain_aliased",
                "__chain_producer.json_a+__chain_ambiguous_consumer",
                "__chain_producer.json_a+__chain_ambiguous_default",
            ],
        ),
        # an unqualified one offers only what needs no qualification
        (
            "__chain_producer+__chain_a",
            ["__chain_producer+__chain_action", "__chain_producer+__chain_ambiguous_default"],
        ),
        # an alias prefix completes to the alias; a canonical match wins over an alias
        ("__chain_simple_producer+sink", ["__chain_simple_producer+sink_alias"]),
        ("__chain_simple_producer+__chain_ali", ["__chain_simple_producer+__chain_aliased"]),
        # malformed or unknown text completes to nothing, never raises
        ("nosuch+ne", []),
        ("open_xc7+ne", []),  # a removed flow completes to nothing, never raises
        ("open_xc7.", []),
        ("nextpnr+open_xc7+", []),
        ("+ne", []),
        ("nextpnr++", []),
        ("nextpnr.config.x+", []),
        ("nextpnr.+", []),
    ],
)
def test_completion_of_an_unfinished_request_returns_the_full_token(incomplete, expected):
    from xeda.flow_runner.chains import complete_request

    assert complete_request(incomplete, _classes()) == expected


def test_completion_is_static(monkeypatch, tmp_path):
    from xeda.flow_runner.chains import complete_request

    def forbidden_init(*args, **kwargs):
        raise AssertionError("completion never constructs a flow")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)
    monkeypatch.chdir(tmp_path)
    assert complete_request("yosys_fpga+ne", _classes()) == ["yosys_fpga+nextpnr"]
    assert not list(tmp_path.iterdir())
