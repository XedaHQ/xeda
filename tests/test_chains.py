"""Canonical parsing and structural matching for flow-chain requests."""

import re
from pathlib import Path

import pytest

from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsException, In, Out
from xeda.flow.io import declared_outputs
from xeda.flow_runner import FlowNotFoundError
from xeda.flow_runner.bindings import NodeKey, ProducerRef, chain_bindings
from xeda.flow_runner.chains import (
    ChainElement,
    ChainLink,
    FlowRequest,
    binding_points,
    default_graph,
    match_required_inputs,
    parse_request,
    validate_chain,
)

from .scoping import node_settings
from .flow_registry import preserved_flow_registry
from . import io_flows
from .io_flows import (
    TChainAction,
    TChainAliased,
    TChainAmbiguousConsumer,
    TChainAmbiguousDefault,
    TChainOptionalConsumer,
    TChainProducer,
    TChainSimpleConsumer,
    TChainSimpleProducer,
    TChainUndeclared,
)

# `io_flows` also registers the `t_route_*` and `t_loop_*` flows the suggestion tests name.


@pytest.fixture(autouse=True)
def isolate_test_flow_registration():
    with preserved_flow_registry():
        yield


def test_parse_names_the_flows_and_keeps_a_single_element_request():
    request = parse_request("t_chain_producer.json_a+t_chain_ambiguous_default")

    assert isinstance(request, FlowRequest)
    assert request.elements == (
        ChainElement(TChainProducer, "json_a"),
        ChainElement(TChainAmbiguousDefault),
    )
    assert request.requested is TChainAmbiguousDefault
    assert parse_request(TChainSimpleConsumer.name).elements == (
        ChainElement(TChainSimpleConsumer),
    )


@pytest.mark.parametrize(
    "text, message",
    [
        ("+t_chain_simple_consumer", "element 1 is empty"),
        ("t_chain_simple_consumer+", "element 2 is empty"),
        ("t_chain_simple_producer++t_chain_simple_consumer", "element 2 is empty"),
        ("t_chain_simple_producer..data", "output qualifier"),
        ("t_chain_simple_producer.data.extra", "output qualifier"),
        ("t_chain_simple_producer+t_chain_simple_consumer.netlist", "last element"),
    ],
)
def test_invalid_grammar_is_reported_with_position_or_qualifier(text, message):
    with pytest.raises(FlowSettingsException, match=message):
        parse_request(text)


def test_unknown_flow_and_output_have_suggestions():
    with pytest.raises(FlowNotFoundError, match="chain_simple_consumer"):
        parse_request("t_chain_simple_producer+t_chain_simple_consmer")

    with pytest.raises(FlowSettingsException, match="json_a.*json_b"):
        match_required_inputs(TChainProducer, TChainAmbiguousConsumer, output="jsn_a")


def test_repeated_flows_are_rejected():
    with pytest.raises(FlowSettingsException, match="t_chain_simple_producer.*more than once"):
        parse_request("t_chain_simple_producer+t_chain_simple_producer")


def test_adjacency_binds_every_required_compatible_input_in_declaration_order():
    assert match_required_inputs(TChainSimpleProducer, TChainSimpleConsumer) == [
        ("netlist", "netlist"),
        ("data", "data"),
    ]


def test_adjacency_skips_optional_inputs_and_respects_a_default_output():
    assert match_required_inputs(TChainProducer, TChainOptionalConsumer) == []
    assert match_required_inputs(TChainProducer, TChainAmbiguousDefault) == [("netlist", "json_b")]


def test_ambiguous_and_qualified_outputs_are_checked():
    with pytest.raises(FlowSettingsException, match="ambiguous.*json_a.*json_b"):
        match_required_inputs(TChainProducer, TChainAmbiguousConsumer)

    assert match_required_inputs(TChainProducer, TChainAmbiguousDefault, output="json_a") == [
        ("netlist", "json_a")
    ]

    assert match_required_inputs(TChainProducer, TChainAmbiguousDefault, output="json_b") == [
        ("netlist", "json_b")
    ]


def test_many_output_cannot_feed_scalar_input():
    with pytest.raises(FlowSettingsException, match="cardinality"):
        match_required_inputs(TChainProducer, TChainAmbiguousConsumer, output="many_json")


def test_a_flow_without_declarations_is_allowed_alone_but_makes_nothing_to_chain():
    assert parse_request(TChainUndeclared.name).requested is TChainUndeclared
    with pytest.raises(FlowSettingsException, match="no compatible output"):
        validate_chain((ChainElement(TChainUndeclared), ChainElement(TChainAction)))


def test_actions_can_only_appear_at_the_end():
    assert parse_request("t_chain_simple_producer+t_chain_action").requested is TChainAction
    with pytest.raises(
        FlowSettingsException, match="performs a test action and can only end a chain"
    ):
        validate_chain((ChainElement(TChainAction), ChainElement(TChainSimpleConsumer)))


def test_parsing_and_matching_do_not_construct_flows_or_touch_a_run_root(monkeypatch, tmp_path):
    def forbidden_init(*args, **kwargs):
        raise AssertionError("flow construction is not part of static chain validation")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)

    request = parse_request("t_chain_simple_producer+t_chain_simple_consumer")
    assert match_required_inputs(TChainSimpleProducer, request.requested) == [
        ("netlist", "netlist"),
        ("data", "data"),
    ]
    assert not list(tmp_path.iterdir())


def test_selected_type_narrowing_requires_produced_types_to_fit_consumer():
    with pytest.raises(FlowSettingsException, match="compatible"):
        match_required_inputs(
            TChainProducer,
            TChainAmbiguousDefault,
            output="json_b",
            selected_output_types={"json_b": (SourceType.Data,)},
        )


def test_chain_fixture_flows_have_declarations():
    assert declared_outputs(TChainProducer)


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
    "request_text, passes",
    [
        ("nextpnr+openfpgaloader", [["fpga_pack"]]),
        ("yosys_fpga+fpga_pack", [["nextpnr"]]),
        # a link that passes through sits beside a direct one, before or after it
        ("yosys_fpga+nextpnr+openfpgaloader", [[], ["fpga_pack"]]),
        ("yosys_fpga+fpga_pack+openfpgaloader", [["nextpnr"], []]),
        # so does the producer's output qualifier
        ("nextpnr.config+openfpgaloader", [["fpga_pack"]]),
        ("yosys_fpga+openfpgaloader", [["nextpnr", "fpga_pack"]]),
    ],
)
def test_a_link_across_a_missing_stage_is_valid_and_passes_through_it(request_text, passes):
    request = parse_request(request_text)
    assert [[flow.name for flow in link.through] for link in request.links] == passes
    assert request.requested.name == request_text.split("+")[-1]


def test_a_link_across_a_stage_in_the_middle_of_a_longer_request_passes_through_it():
    request = parse_request("t_route_a+t_route_c")
    assert [flow.name for flow in request.through(0)] == ["t_route_b"]
    assert _suggestions("t_chain_simple_producer+t_route_a+t_route_c") == []


def test_an_ambiguous_output_suggests_each_qualification_that_validates():
    assert _suggestions("t_chain_producer+t_chain_ambiguous_consumer") == [
        "t_chain_producer.json_a+t_chain_ambiguous_consumer",
        "t_chain_producer.json_b+t_chain_ambiguous_consumer",
    ]
    # the qualifiers of other elements and the rest of the request are kept
    longer = _suggestions("t_chain_producer+t_chain_ambiguous_consumer+t_chain_action")
    assert longer == []  # the action takes Data, which the consumer does not make: no stage fits


def test_a_qualifier_that_does_not_fit_suggests_the_one_that_does():
    # `data` is a Data file; the consumer takes JsonNetlist -- only the json_* outputs fit
    suggestions = _suggestions("t_chain_producer.data+t_chain_ambiguous_consumer")
    assert suggestions == [
        "t_chain_producer.json_a+t_chain_ambiguous_consumer",
        "t_chain_producer.json_b+t_chain_ambiguous_consumer",
    ]


@pytest.mark.parametrize(
    "request_text",
    [
        "t_chain_simple_consumer+t_chain_producer",  # the target takes nothing
        "t_chain_action+t_chain_simple_consumer",  # an action ends a chain
        "t_chain_undeclared+t_chain_action",  # an undeclared flow runs alone
        "t_chain_simple_producer+t_chain_ambiguous_default+t_chain_simple_consumer",
        "t_route_a+t_loop_a",  # the default producers of the target do not reach the producer
        "t_route_a+t_route_c+t_route_b",  # the link would pass through a flow of the request
    ],
)
def test_no_compatible_declared_path_means_no_fabricated_suggestion(request_text):
    assert _suggestions(request_text) == []


def test_an_action_before_the_end_is_refused_with_its_reason_and_no_suggestion():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("t_chain_action+t_chain_simple_consumer")
    assert "performs a test action and can only end a chain" in str(raised.value)
    assert "Did you mean" not in str(raised.value)


def test_validation_terminates_on_a_cycle_of_default_producers():
    # TLoopA and TLoopB are each other's default producer: the walk visits each once
    assert _suggestions("t_route_a+t_loop_a") == []
    # a cycle gives direct links in both directions, and passes through nothing
    assert parse_request("t_loop_b+t_loop_a").through(0) == ()
    assert parse_request("t_loop_a+t_loop_b").through(0) == ()


def test_every_suggestion_is_itself_a_valid_request_with_its_qualifiers():
    for text in (
        "yosys_fpga.netlist_edif+fpga_pack",
        "t_chain_producer+t_chain_ambiguous_consumer",
        "t_chain_producer.data+t_chain_ambiguous_consumer",
        "t_diamond_head.o2+t_diamond_tail",
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
    assert _suggestions("yosys_fpga.netlist_edif+fpga_pack") == [
        "yosys_fpga+fpga_pack",
        "yosys_fpga.netlist+fpga_pack",
    ]
    assert not list(tmp_path.iterdir())


def test_a_suggestion_resolves_with_a_valid_design(tmp_path):
    from xeda import Design
    from xeda.flow_runner import DefaultRunner

    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(name="top", root=root, rtl={"sources": ["top.v"], "top": "top"})
    suggestion, _qualified = _suggestions("yosys_fpga.netlist_edif+fpga_pack")
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        parse_request(suggestion),
        design,
        flow_settings=node_settings(
            parse_request(suggestion), {"fpga": {"part": "LFE5U-25F-6BG381C"}}
        ),
    )
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack"]
    assert plan.requested == "fpga_pack"


# ------------------------------------------------------------ links that pass through flows


def test_a_link_passes_through_the_default_producers_between_its_flows():
    from xeda.flows import Nextpnr

    request = parse_request("yosys_fpga+fpga_pack")
    validate_chain(request.elements)
    assert [cls.name for cls in request.through(0)] == ["nextpnr"]
    assert request.links[0].binds == ((Nextpnr, "netlist", "netlist"),)


def test_a_direct_link_passes_through_nothing_and_binds_the_inputs_of_its_consumer():
    from xeda.flows import FpgaPack

    request = parse_request("nextpnr+fpga_pack")
    assert request.links == (ChainLink((), ((FpgaPack, "config", "config"),)),)
    assert request.through(0) == ()
    assert parse_request("fpga_pack").links == ()


def test_a_direct_edge_still_wins_over_a_route():
    from .io_flows import TRouteBoth

    request = parse_request("t_route_a+t_route_both")
    assert request.through(0) == ()
    # `y` comes from `t_route_b` by default, and `t_route_a` is not its producer
    assert request.links[0].binds == ((TRouteBoth, "x", "x"),)
    assert [cls.name for cls in default_graph(TRouteBoth).between(io_flows.TRouteA)] == [
        "t_route_b"
    ]


def test_a_qualified_pass_through_link_binds_every_binding_point():
    from .io_flows import TDiamondHead, TDiamondLeft, TDiamondRight

    for text in ("t_diamond_head+t_diamond_tail", "t_diamond_head.o1+t_diamond_tail"):
        request = parse_request(text)
        assert request.through(0) == (TDiamondLeft, TDiamondRight)
        assert request.links[0].binds == (
            (TDiamondLeft, "i", "o1"),
            (TDiamondRight, "i", "o1"),
        )
        bound = chain_bindings(request)
        location = (
            "the chain position 1 (t_diamond_head) -> 2 (t_diamond_tail), "
            "through t_diamond_left, t_diamond_right"
        )
        for side in ("left", "right"):
            binding = bound[NodeKey(f"t_diamond_{side}"), "i"]
            assert binding.references == (ProducerRef(NodeKey(TDiamondHead.name), "o1"),)
            assert (binding.location, binding.origin) == (location, "chain")
        assert len(bound) == 2

    with pytest.raises(FlowSettingsException) as raised:
        parse_request("t_diamond_head.o2+t_diamond_tail")
    message = str(raised.value)
    assert "t_diamond_left.i" in message
    assert _suggestions("t_diamond_head.o2+t_diamond_tail") == [
        "t_diamond_head+t_diamond_tail",
        "t_diamond_head.o1+t_diamond_tail",
    ]


def test_a_link_cannot_pass_through_a_flow_the_request_names():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("t_route_a+t_route_c+t_route_b")
    message = str(raised.value)
    assert message == (
        "Flow `t_route_b` appears more than once; a chain is a path and each flow may appear "
        "only once. The link `t_route_a+t_route_c` passes through `t_route_b`."
    )
    assert _suggestions("t_route_a.x+t_route_c+t_route_b") == []
    with pytest.raises(FlowSettingsException, match="The link `t_route_a.x\\+t_route_c` passes"):
        parse_request("t_route_a.x+t_route_c+t_route_b")


def test_a_link_that_feeds_only_its_consumer_is_a_failed_edge_not_a_pass_through():
    class TPassMismatch(Flow):
        """Names `t_route_a` as the producer of a file it makes no output of."""

        results_description = {}

        class Inputs(Flow.Inputs):
            i: Path = In(SourceType.Sdc, producer="t_route_a", description="Not made.")

        def run(self) -> None:
            pass

    assert default_graph(TPassMismatch).between(io_flows.TRouteA) == ()
    with pytest.raises(FlowSettingsException, match="no compatible output") as raised:
        parse_request("t_route_a+t_pass_mismatch")
    assert "passes through" not in str(raised.value)
    with pytest.raises(FlowSettingsException, match="no compatible output"):
        parse_request("t_route_c+t_route_b")


def test_a_request_has_one_link_for_each_pair_of_elements():
    elements = (ChainElement(TChainSimpleProducer), ChainElement(TChainSimpleConsumer))
    with pytest.raises(ValueError, match="links"):
        FlowRequest(elements)
    with pytest.raises(ValueError, match="links"):
        FlowRequest(elements[:1], (ChainLink((), ()),))
    assert FlowRequest(elements[:1]).links == ()


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
    from xeda.flows import FpgaPack, Nextpnr, Openfpgaloader, VivadoImpl, YosysFpga

    classes = _product_classes()
    # the JSON netlist feeds nextpnr, and the EDIF netlist Vivado: each by its own output. The
    # packer and the loader come after them, through the flows between
    assert _pairs(followers(YosysFpga, classes), "consumer") == [
        ("nextpnr", None, False),
        ("vivado_impl", None, False),
        ("fpga_pack", None, False),
        ("openfpgaloader", None, False),
    ]
    assert [[f.name for f in e.through] for e in followers(YosysFpga, classes)] == [
        [],
        [],
        ["nextpnr"],
        ["nextpnr", "fpga_pack"],
    ]
    # nextpnr makes a configuration of whichever family the target selects: a hint that
    # depends on the target
    assert _pairs(followers(Nextpnr, classes), "consumer") == [
        ("fpga_pack", None, True),
        ("openfpgaloader", None, True),
    ]
    assert _pairs(followers(FpgaPack, classes), "consumer") == [("openfpgaloader", None, False)]
    assert followers(Openfpgaloader, classes) == []
    # the Vivado flows write a bitstream on request (`bitstream`), which the loader reads
    assert _pairs(predecessors(Openfpgaloader, classes), "producer") == [
        ("fpga_pack", None, False),
        ("vivado_alt_synth", None, False),
        ("vivado_impl", None, False),
        ("vivado_synth", None, False),
        ("bsc", None, False),
        ("nextpnr", None, True),
        ("yosys_fpga", None, False),
    ]
    # `bsc` converts Bluespec for `yosys_fpga`, so it comes before every flow after it, through
    # the flows between
    assert _pairs(predecessors(Nextpnr, classes), "producer") == [
        ("yosys_fpga", None, False),
        ("bsc", None, False),
    ]
    assert _pairs(predecessors(VivadoImpl, classes), "producer") == [
        ("yosys_fpga", None, False),
        ("bsc", None, False),
    ]
    assert _pairs(predecessors(FpgaPack, classes), "producer") == [
        ("nextpnr", None, True),
        ("bsc", None, False),
        ("yosys_fpga", None, False),
    ]
    # and its routed checkpoint (`write_checkpoint`) is what the power report reads
    assert _pairs(followers(VivadoImpl, classes), "consumer") == [
        ("openfpgaloader", None, False),
        ("vivado_power", None, False),
    ]
    # only the converter comes before it: nothing else makes a design
    assert _pairs(predecessors(YosysFpga, classes), "producer") == [("bsc", None, False)]


def test_follow_relations_come_from_declarations_not_from_a_name_table():
    from xeda.flow_runner.chains import followers, predecessors

    classes = _classes()
    by_consumer = {}
    for edge in followers(TChainProducer, classes):
        by_consumer.setdefault(edge.consumer, []).append(edge.output)
    # a consumer whose default names an output is followed unqualified; an ambiguous one only
    # through each output that fits
    assert by_consumer[TChainAmbiguousDefault] == [None]
    assert by_consumer[TChainAmbiguousConsumer] == ["json_a", "json_b"]
    assert by_consumer[TChainAliased] == ["json_a", "json_b"]
    assert by_consumer[TChainAction] == [None]
    assert TChainUndeclared not in by_consumer and TChainOptionalConsumer not in by_consumer
    assert followers(TChainUndeclared, classes) == []
    assert followers(TChainAction, classes) == []
    assert TChainProducer in [e.producer for e in predecessors(TChainAction, classes)]
    assert TChainAction not in [e.producer for e in predecessors(TChainSimpleConsumer, classes)]
    edge = next(
        e for e in followers(TChainSimpleProducer, classes) if e.consumer is TChainSimpleConsumer
    )
    assert edge.binds == (("netlist", "netlist"), ("data", "data"))


def _link_of(producer, output, consumer):
    """The link `producer.output+consumer` that the chain validator accepts, or None."""
    try:
        (link,) = validate_chain((ChainElement(producer, output), ChainElement(consumer)))
    except FlowSettingsException:
        return None
    return link


def test_a_follow_relation_is_exactly_a_link_the_chain_validator_accepts():
    """Every relation is a request that `xeda run` accepts: a direct link or one that passes
    through other flows. Nothing else is listed, and a direct link is listed before the others."""
    from xeda.flow_runner.chains import followers, predecessors

    classes = _classes()
    # once per producer: the last check below reads every list again for every consumer
    followed = {producer: followers(producer, classes) for producer in classes}
    for producer in classes:
        listed = followed[producer]
        assert [bool(e.through) for e in listed] == sorted(bool(e.through) for e in listed)
        for consumer in classes:
            found = [e for e in listed if e.consumer is consumer]
            if (link := _link_of(producer, None, consumer)) is not None:
                expected = [(None, link)]
            else:
                outputs = declared_outputs(producer)
                expected = [
                    (o, link)
                    for o in outputs
                    if (link := _link_of(producer, o, consumer)) is not None
                ]
            expected.sort(key=lambda pair: bool(pair[1].through))
            assert [(e.output, e.through) for e in found] == [
                (output, link.through) for output, link in expected
            ], (producer.name, consumer.name)
            for edge, (_output, link) in zip(found, expected):
                assert edge.bound_flows == tuple(flow for flow, _name, _made in link.binds)
                assert edge.binds == tuple((name, made) for _flow, name, made in link.binds)
    for consumer in classes:
        assert {(e.producer, e.output) for e in predecessors(consumer, classes)} == {
            (e.producer, e.output)
            for producer in classes
            for e in followed[producer]
            if e.consumer is consumer
        }


def test_a_link_that_passes_through_flows_is_listed_with_the_flows_it_binds():
    from xeda.flow_runner.chains import followers
    from xeda.flows import Nextpnr, YosysFpga

    classes = _product_classes()
    (edge,) = [e for e in followers(YosysFpga, classes) if e.consumer.name == "fpga_pack"]
    assert [flow.name for flow in edge.through] == ["nextpnr"]
    assert edge.bound_flows == (Nextpnr,)
    assert edge.binds == (("netlist", "netlist"),)
    # a direct link lists no flow in between and binds inputs of its consumer
    (direct,) = [e for e in followers(YosysFpga, classes) if e.consumer is Nextpnr]
    assert direct.through == () and direct.bound_flows == (Nextpnr,)


# ---------------------------------------------------------------------------- completion


@pytest.mark.parametrize(
    "incomplete, expected",
    [
        ("yosys_fpga+ne", ["yosys_fpga+nextpnr"]),
        (
            "yosys_fpga+nextpnr+",
            ["yosys_fpga+nextpnr+fpga_pack", "yosys_fpga+nextpnr+openfpgaloader"],
        ),
        (
            "yosys_fpga+nextpnr+fpga_pack+open",
            ["yosys_fpga+nextpnr+fpga_pack+openfpgaloader"],
        ),
        ("nextpnr+fp", ["nextpnr+fpga_pack"]),
        ("nextpnr+", ["nextpnr+fpga_pack", "nextpnr+openfpgaloader"]),
        ("yosys_fpga+fpga_pack+", ["yosys_fpga+fpga_pack+openfpgaloader"]),
        ("yosys_fpga+fpga_pack+o", ["yosys_fpga+fpga_pack+openfpgaloader"]),
        ("nextpnr+fpga_pack+", ["nextpnr+fpga_pack+openfpgaloader"]),
        ("yosys_fpga+fpga_pack.", ["yosys_fpga+fpga_pack.bitstream"]),
        ("yosys_fpga+nextpnr.", ["yosys_fpga+nextpnr.config"]),
        # a flow that a link passes through cannot come again, as an element or in another link
        ("yosys_fpga+fpga_pack+nextpnr", []),
        ("yosys_fpga+fpga_pack+ne", []),
        ("yosys_fpga+openfpgaloader+", []),
        ("yosys_fpga+openfpgaloader+fpga_pack", []),
        ("nextpnr+openfpgaloader+fpga_pack", []),
        ("ne", ["nextpnr"]),
        # no followers: an action ends a chain, nothing repeats, an undeclared flow is alone
        ("openfpgaloader+", []),
        ("fpga_pack+openfpgaloader+", []),
        ("yosys_fpga+nextpnr+n", []),
        ("nextpnr+yosys", []),
        ("t_chain_undeclared+", []),
        ("t_chain_simple_producer+t_chain_action+", []),
        # a qualifier completes to the outputs that lead somewhere
        ("nextpnr.", ["nextpnr.config"]),
        ("yosys_fpga.n", ["yosys_fpga.netlist", "yosys_fpga.netlist_edif"]),
        ("yosys_fpga.netlist_e", ["yosys_fpga.netlist_edif"]),
        ("yosys_fpga+nextpnr.c", ["yosys_fpga+nextpnr.config"]),
        ("openfpgaloader.", []),
        ("t_chain_producer.j", ["t_chain_producer.json_a", "t_chain_producer.json_b"]),
        # an output-qualified prefix filters the followers through that output
        (
            "t_chain_producer.json_a+t_chain_a",
            [
                "t_chain_producer.json_a+t_chain_aliased",
                "t_chain_producer.json_a+t_chain_ambiguous_consumer",
                "t_chain_producer.json_a+t_chain_ambiguous_default",
            ],
        ),
        # an unqualified one offers only what needs no qualification
        (
            "t_chain_producer+t_chain_a",
            ["t_chain_producer+t_chain_action", "t_chain_producer+t_chain_ambiguous_default"],
        ),
        # a flow completes by its name only
        ("t_chain_simple_producer+sink", []),
        ("t_chain_simple_producer+t_chain_ali", ["t_chain_simple_producer+t_chain_aliased"]),
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


def _product_classes():
    return [cls for cls in _classes() if cls.__module__.startswith("xeda.flows")]


def test_completion_offers_the_direct_followers_first_and_then_the_ones_a_link_passes_through():
    from xeda.flow_runner.chains import complete_request

    classes = _product_classes()
    assert complete_request("yosys_fpga+", classes) == [
        "yosys_fpga+nextpnr",
        "yosys_fpga+vivado_impl",
        "yosys_fpga+fpga_pack",
        "yosys_fpga+openfpgaloader",
    ]
    assert complete_request("yosys_fpga.netlist+", classes) == [
        "yosys_fpga.netlist+nextpnr",
        "yosys_fpga.netlist+fpga_pack",
        "yosys_fpga.netlist+openfpgaloader",
    ]
    assert complete_request("yosys_fpga.netlist_edif+", classes) == [
        "yosys_fpga.netlist_edif+vivado_impl"
    ]
    assert complete_request("yosys_fpga+o", classes) == ["yosys_fpga+openfpgaloader"]
    assert complete_request("yosys_fpga+vivado_impl+", classes) == [
        "yosys_fpga+vivado_impl+openfpgaloader",
        "yosys_fpga+vivado_impl+vivado_power",
    ]


def test_completion_offers_no_flow_that_a_link_of_the_prefix_passes_through():
    from xeda.flow_runner.chains import complete_request

    classes = _product_classes()
    # `yosys_fpga+fpga_pack` runs `nextpnr` in between, so `nextpnr` cannot come again
    assert complete_request("yosys_fpga+fpga_pack+", classes) == [
        "yosys_fpga+fpga_pack+openfpgaloader"
    ]
    assert complete_request("yosys_fpga+fpga_pack+ne", classes) == []
    assert complete_request("yosys_fpga+fpga_pack+nextpnr", classes) == []
    assert complete_request("yosys_fpga+fpga_pack.", classes) == ["yosys_fpga+fpga_pack.bitstream"]
    assert complete_request("yosys_fpga+openfpgaloader+", classes) == []
    assert complete_request("nextpnr+openfpgaloader+fpga_pack", classes) == []
    # a prefix that is no chain completes to nothing
    assert complete_request("yosys_fpga+openfpgaloader+fpga_pack+", classes) == []


def test_completion_does_not_offer_a_flow_that_an_earlier_link_skips():
    from xeda.flow_runner.chains import complete_request

    skip = [
        getattr(io_flows, name)
        for name in ("TSkipMake", "TSkipMid", "TSkipEnd", "TSkipSide", "TSkipSink")
    ]
    # `TSkipMake+TSkipEnd` skips `TSkipMid`, which could take the netlist of `TSkipEnd` directly
    assert complete_request("t_skip_end+", skip) == [
        "t_skip_end+t_skip_mid",
        "t_skip_end+t_skip_side",
    ]
    assert complete_request("t_skip_make+", skip) == [
        "t_skip_make+t_skip_mid",
        "t_skip_make+t_skip_side",
        "t_skip_make+t_skip_end",
        "t_skip_make+t_skip_sink",
    ]
    assert complete_request("t_skip_make+t_skip_end+", skip) == [
        "t_skip_make+t_skip_end+t_skip_side"
    ]
    assert complete_request("t_skip_make+t_skip_end+t_skip_m", skip) == []
    # a second link that would skip `TSkipMid` again is refused, as an element and as a route
    assert complete_request("t_skip_make+t_skip_end+t_skip_side+", skip) == []
    assert complete_request("t_skip_make+t_skip_end.", skip) == ["t_skip_make+t_skip_end.end"]
    assert complete_request("t_skip_make+t_skip_end+t_skip_side.", skip) == []


def test_completion_is_static(monkeypatch, tmp_path):
    from xeda.flow_runner.chains import complete_request

    def forbidden_init(*args, **kwargs):
        raise AssertionError("completion never constructs a flow")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)
    monkeypatch.chdir(tmp_path)
    assert complete_request("yosys_fpga+ne", _classes()) == ["yosys_fpga+nextpnr"]
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------------- default graph


def _toy(name: str, inputs=None, outputs=None) -> type[Flow]:
    """Register a flow named `name` (a class name; the flow name is its snake case) with the
    given inputs and outputs, each a mapping of a name to `(annotation, declaration)`."""
    parts: dict = {
        "__doc__": f"Toy flow {name}.",
        "results_description": {},
        "run": lambda self: None,
    }
    for kind, base, declared in (
        ("Inputs", Flow.Inputs, inputs),
        ("Outputs", Flow.Outputs, outputs),
    ):
        if declared:
            parts[kind] = type(
                kind,
                (base,),
                {
                    "__annotations__": {
                        key: annotation for key, (annotation, _) in declared.items()
                    },
                    **{key: declaration for key, (_, declaration) in declared.items()},
                },
            )
    return type(name, (Flow,), parts)


def _data_in(producer: str | None = None, output: str | None = None, **kwargs):
    return (
        Path,
        In(SourceType.Data, producer=producer, output=output, description="In.", **kwargs),
    )


def _data_out():
    return (Path, Out(SourceType.Data, description="Out."))


def _pairs_of(edges) -> list[tuple[str, str, str]]:
    return [(e.consumer.name, e.input, e.producer.name) for e in edges]


def test_the_default_graph_keeps_the_edges_of_converging_paths():
    a = _toy("TDgA", outputs={"made": _data_out()})
    z = _toy("TDgZ", {"src": _data_in("t_dg_a", "made")}, {"made": _data_out()})
    x = _toy("TDgX", {"src": _data_in("t_dg_z", "made")}, {"made": _data_out()})
    y = _toy("TDgY", {"src": _data_in("t_dg_z", "made")}, {"made": _data_out()})
    b = _toy("TDgB", {"left": _data_in("t_dg_x", "made"), "right": _data_in("t_dg_y", "made")})

    graph = default_graph(b)

    assert graph.root is b
    assert graph.flows() == (b, x, y, z, a)
    assert _pairs_of(graph.edges) == [
        ("t_dg_b", "left", "t_dg_x"),
        ("t_dg_b", "right", "t_dg_y"),
        ("t_dg_x", "src", "t_dg_z"),
        ("t_dg_y", "src", "t_dg_z"),
        ("t_dg_z", "src", "t_dg_a"),
    ]
    assert [(e.consumer, e.input, e.producer, e.output) for e in graph.edges][-1] == (
        z,
        "src",
        a,
        "made",
    )
    (point,) = binding_points(graph, a)
    assert (point[0].consumer, point[0].input, point[1]) == (z, "src", "made")
    assert graph.between(a) == (z, x, y)
    assert graph.between(z) == (x, y)
    assert graph.between(x) == ()  # adjacent to the root
    assert graph.between(b) == ()
    assert graph.between(_toy("TDgOther")) == ()  # not in the graph


def test_a_flow_that_takes_two_inputs_from_one_producer_has_two_edges():
    p = _toy("TDgP", outputs={"one": _data_out(), "two": _data_out()})
    c = _toy(
        "TDgC",
        {"first": _data_in("t_dg_p", "one"), "second": _data_in("t_dg_p", "two")},
    )

    graph = default_graph(c)

    assert graph.flows() == (c, p)
    assert _pairs_of(graph.edges) == [("t_dg_c", "first", "t_dg_p"), ("t_dg_c", "second", "t_dg_p")]
    points = binding_points(graph, p)
    assert [(edge.input, chosen) for edge, chosen in points] == [
        ("first", "one"),
        ("second", "two"),
    ]


def test_an_output_qualifier_selects_the_binding_points_that_it_fits():
    a = _toy(
        "TDgA",
        outputs={
            "o1": (Path, Out(SourceType.Data, description="First.")),
            "o2": (Path, Out(SourceType.JsonNetlist, description="Second.")),
        },
    )
    z = _toy("TDgZ", {"src": _data_in("t_dg_a")})
    graph = default_graph(z)

    assert [chosen for _, chosen in binding_points(graph, a)] == ["o1"]
    assert [chosen for _, chosen in binding_points(graph, a, "o1")] == ["o1"]
    with pytest.raises(FlowSettingsException, match=r"t_dg_z\.src"):
        binding_points(graph, a, "o2")
    with pytest.raises(FlowSettingsException, match=r"has no output `o3`\. Did you mean .*`o1`"):
        binding_points(graph, a, "o3")


def test_binding_points_refuse_a_producer_with_no_fitting_or_with_several_outputs():
    many = _toy("TDgMany", outputs={"files": (list[Path], Out(SourceType.Data, description="F."))})
    both = _toy("TDgBoth", outputs={"m": _data_out(), "n": _data_out()})
    none = _toy("TDgNone", outputs={"j": (Path, Out(SourceType.JsonNetlist, description="J."))})
    one_file = _toy(
        "TDgOneFile",
        {"a": _data_in("t_dg_many"), "b": _data_in("t_dg_both"), "c": _data_in("t_dg_none")},
    )
    graph = default_graph(one_file)

    with pytest.raises(FlowSettingsException, match=r"cardinality.*t_dg_one_file\.a"):
        binding_points(graph, many)
    with pytest.raises(FlowSettingsException, match=r"t_dg_one_file\.b.*ambiguous.*qualify"):
        binding_points(graph, both)
    with pytest.raises(
        FlowSettingsException, match=r"t_dg_one_file\.c` takes Data.*makes JsonNetlist"
    ):
        binding_points(graph, none)


def test_a_cycle_of_default_producers_ends_and_hides_no_other_path():
    a = _toy("TDgA", outputs={"made": _data_out()})
    m = _toy("TDgM", {"src": _data_in("t_dg_a", "made")}, {"made": _data_out()})
    l1 = _toy("TDgLone", {"src": _data_in("t_dg_ltwo", "made")}, {"made": _data_out()})
    l2 = _toy("TDgLtwo", {"src": _data_in("t_dg_lone", "made")}, {"made": _data_out()})
    b = _toy("TDgB", {"loop": _data_in("t_dg_lone", "made"), "line": _data_in("t_dg_m", "made")})

    graph = default_graph(b)

    assert graph.flows() == (b, l1, m, l2, a)
    assert _pairs_of(graph.edges) == [
        ("t_dg_b", "loop", "t_dg_lone"),
        ("t_dg_b", "line", "t_dg_m"),
        ("t_dg_lone", "src", "t_dg_ltwo"),
        ("t_dg_m", "src", "t_dg_a"),
        ("t_dg_ltwo", "src", "t_dg_lone"),
    ]
    assert _pairs_of(edge for edge, _ in binding_points(graph, a)) == [("t_dg_m", "src", "t_dg_a")]
    assert graph.between(a) == (m,)
    assert graph.between(l2) == (l1,)
    # the flows of a cycle lie between each other and the root
    assert graph.between(l1) == (l2,)


def test_an_optional_input_is_followed_but_binds_nothing():
    a = _toy("TDgA", outputs={"made": _data_out()})
    c = _toy(
        "TDgC",
        {"extra": (Path | None, In(SourceType.Data, producer="t_dg_a", description="Extra."))},
    )

    graph = default_graph(c)

    assert graph.flows() == (c, a)
    assert _pairs_of(graph.edges) == [("t_dg_c", "extra", "t_dg_a")]
    assert binding_points(graph, a) == ()


def test_a_producer_that_is_not_a_flow_is_skipped_and_a_flow_without_producers_is_alone():
    c = _toy("TDgC", {"src": _data_in("t_dg_missing")})

    graph = default_graph(c)

    assert graph.flows() == (c,)
    assert graph.edges == ()
    assert graph.between(c) == ()
    assert binding_points(graph, c) == ()


def test_the_default_graph_of_a_flow_reads_declarations_only(monkeypatch):
    def forbidden_init(*args, **kwargs):
        raise AssertionError("the graph is static")

    monkeypatch.setattr(Flow, "__init__", forbidden_init)
    from xeda.flow_runner import get_flow_class

    assert default_graph(get_flow_class("fpga_pack")).flows()


def test_the_graph_of_the_fpga_packer_passes_through_nextpnr():
    from xeda.flow_runner import get_flow_class

    fpga_pack, nextpnr, yosys_fpga = map(get_flow_class, ("fpga_pack", "nextpnr", "yosys_fpga"))

    graph = default_graph(fpga_pack)

    assert {nextpnr, yosys_fpga} <= set(graph.flows())
    assert graph.flows()[0] is fpga_pack
    assert graph.between(yosys_fpga) == (nextpnr,)
    ((edge, chosen),) = binding_points(graph, yosys_fpga)
    assert (edge.consumer, edge.input, edge.producer, chosen) == (
        nextpnr,
        "netlist",
        yosys_fpga,
        "netlist",
    )


# ---------------------------------------------------------------------------------------------
# The grammar of a request and of a flow name
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, code",
    [
        ("yosys_fpga,nextpnr", "several_roots_unsupported"),
        ("yosys_fpga+nextpnr,fpga_pack", "several_roots_unsupported"),
        ("yosys_fpga@x+nextpnr", "invalid_chain"),
        ("yosys_fpga+nextpnr@x", "invalid_chain"),
        ("yosys_fpga+", "invalid_chain"),
        ("+nextpnr", "invalid_chain"),
        ("yosys_fpga.Net-list+nextpnr", "invalid_chain"),
        ("yosys_fpga.1st+nextpnr", "invalid_chain"),
        ("yosys_fpga.nosuch+nextpnr", "unknown_output"),
    ],
)
def test_reserved_chain_syntax_and_a_malformed_output_are_refused_with_their_codes(text, code):
    from xeda.flow_runner.chains import ChainRefusal
    from xeda.flow_runner.error_codes import public_error

    with pytest.raises(ChainRefusal) as refused:
        parse_request(text)
    error = public_error(refused.value)
    assert (error.code, error.chain) == (code, text)


def test_a_qualified_output_parses():
    request = parse_request("yosys_fpga.netlist+nextpnr")
    assert [(e.node, e.output) for e in request.elements] == [
        ("yosys_fpga", "netlist"),
        ("nextpnr", None),
    ]


@pytest.mark.parametrize(
    "text, suggestion",
    [("vivado-synth", ("vivado_synth",)), ("Nextpnr", ("nextpnr",)), ("nosuchflow", ())],
)
def test_a_name_that_is_no_flow_is_flow_not_found_and_no_spelling_is_accepted(text, suggestion):
    with pytest.raises(FlowNotFoundError) as raised:
        parse_request(text)
    assert raised.value.suggestions[: len(suggestion)] == suggestion
    with pytest.raises(FlowNotFoundError):
        parse_request(f"yosys_fpga+{text}")


def test_every_builtin_flow_name_fits_the_grammar():
    from xeda.flow_runner.chains import FLOW_NAME, RESERVED_FLOW_NAMES

    names = [cls.name for cls in _product_classes()]
    assert len(names) >= 29
    assert all(FLOW_NAME.fullmatch(name) and name not in RESERVED_FLOW_NAMES for name in names)


@pytest.mark.parametrize("class_name", ["_Hidden", "Flows", "Design", "Settings"])
def test_a_flow_name_outside_the_grammar_or_reserved_is_refused_at_registration(class_name):
    from xeda.flow import registered_flows

    before = dict(registered_flows)
    with pytest.raises(TypeError, match="flow name"):
        type(class_name, (Flow,), {"__module__": __name__, "run": lambda self: None})
    assert registered_flows == before


def _action_with_a_receipt():
    class TProgram(Flow):
        """An action that declares the receipt of what it did."""

        action_reason = "programs a device"

        class Outputs(Flow.Outputs):
            receipt: Path = Out(SourceType.Data, description="What the action did.")

        def run(self) -> None:
            pass

    class TFlash(Flow):
        """An action that declares no output."""

        action_reason = "flashes a device"

        def run(self) -> None:
            pass

    class TCheck(Flow):
        """Reads the receipt of an action."""

        class Inputs(Flow.Inputs):
            receipt: Path = In(SourceType.Data, description="The receipt.")

        def run(self) -> None:
            pass

    return TProgram, TFlash, TCheck


def test_an_action_may_precede_a_reader_of_its_declared_output_and_no_other():
    from xeda.flow_runner.chains import ChainRefusal

    program, flash, check = _action_with_a_receipt()
    request = parse_request("t_program+t_check")
    assert request.requested is check
    assert request.links[0].binds == ((check, "receipt", "receipt"),)
    assert [e.node for e in parse_request("t_program.receipt+t_check").elements] == [
        program.name,
        check.name,
    ]
    with pytest.raises(ChainRefusal, match="can only end a chain") as refused:
        parse_request("t_flash+t_check")
    assert refused.value.code == "action_not_last"
    with pytest.raises(ChainRefusal) as refused:
        parse_request("t_program+t_flash")
    assert refused.value.code == "action_not_last"
