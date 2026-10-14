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

from .flow_registry import preserved_flow_registry
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
    with preserved_flow_registry():
        yield


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


def test_a_flow_without_declarations_is_allowed_alone_but_makes_nothing_to_chain():
    assert parse_request("_ChainUndeclared").requested is _ChainUndeclared
    with pytest.raises(FlowSettingsException, match="no compatible output"):
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
    assert declared_outputs(_ChainProducer)


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
    request = parse_request("__route_a+__route_c")
    assert [flow.name for flow in request.through(0)] == ["__route_b"]
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
        "__route_a+__loop_a",  # the default producers of the target do not reach the producer
        "__route_a+__route_c+__route_b",  # the link would pass through a flow of the request
    ],
)
def test_no_compatible_declared_path_means_no_fabricated_suggestion(request_text):
    assert _suggestions(request_text) == []


def test_an_action_before_the_end_is_refused_with_its_reason_and_no_suggestion():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("__chain_action+__chain_simple_consumer")
    assert "performs a test action and can only end a chain" in str(raised.value)
    assert "Did you mean" not in str(raised.value)


def test_validation_terminates_on_a_cycle_of_default_producers():
    # _LoopA and _LoopB are each other's default producer: the walk visits each once
    assert _suggestions("__route_a+__loop_a") == []
    # a cycle gives direct links in both directions, and passes through nothing
    assert parse_request("__loop_b+__loop_a").through(0) == ()
    assert parse_request("__loop_a+__loop_b").through(0) == ()


def test_every_suggestion_is_itself_a_valid_request_with_its_qualifiers():
    for text in (
        "yosys_fpga.netlist_edif+fpga_pack",
        "__chain_producer+__chain_ambiguous_consumer",
        "__chain_producer.data+__chain_ambiguous_consumer",
        "__diamond_head.o2+__diamond_tail",
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
    design = Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})
    suggestion, _qualified = _suggestions("yosys_fpga.netlist_edif+fpga_pack")
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        parse_request(suggestion), design, flow_settings={"fpga": "LFE5U-25F-6BG381C"}
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
    from .io_flows import _RouteBoth

    request = parse_request("__route_a+__route_both")
    assert request.through(0) == ()
    # `y` comes from `__route_b` by default, and `__route_a` is not its producer
    assert request.links[0].binds == ((_RouteBoth, "x", "x"),)
    assert [cls.name for cls in default_graph(_RouteBoth).between(io_flows._RouteA)] == [
        "__route_b"
    ]


def test_a_qualified_pass_through_link_binds_every_binding_point():
    from .io_flows import _DiamondHead, _DiamondLeft, _DiamondRight

    for text in ("__diamond_head+__diamond_tail", "__diamond_head.o1+__diamond_tail"):
        request = parse_request(text)
        assert request.through(0) == (_DiamondLeft, _DiamondRight)
        assert request.links[0].binds == (
            (_DiamondLeft, "i", "o1"),
            (_DiamondRight, "i", "o1"),
        )
        bound = chain_bindings(request)
        location = (
            "the chain position 1 (__diamond_head) -> 2 (__diamond_tail), "
            "through __diamond_left, __diamond_right"
        )
        for side in ("left", "right"):
            binding = bound[NodeKey(f"__diamond_{side}"), "i"]
            assert binding.references == (ProducerRef(NodeKey(_DiamondHead.name), "o1"),)
            assert (binding.location, binding.origin) == (location, "chain")
        assert len(bound) == 2

    with pytest.raises(FlowSettingsException) as raised:
        parse_request("__diamond_head.o2+__diamond_tail")
    message = str(raised.value)
    assert "__diamond_left.i" in message
    assert _suggestions("__diamond_head.o2+__diamond_tail") == [
        "__diamond_head+__diamond_tail",
        "__diamond_head.o1+__diamond_tail",
    ]


def test_a_link_cannot_pass_through_a_flow_the_request_names():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("__route_a+__route_c+__route_b")
    message = str(raised.value)
    assert message == (
        "Flow `__route_b` appears more than once; a chain is a path and each flow may appear "
        "only once. The link `__route_a+__route_c` passes through `__route_b`."
    )
    assert _suggestions("__route_a.x+__route_c+__route_b") == []
    with pytest.raises(FlowSettingsException, match="The link `__route_a.x\\+__route_c` passes"):
        parse_request("__route_a.x+__route_c+__route_b")


def test_a_link_that_feeds_only_its_consumer_is_a_failed_edge_not_a_pass_through():
    class _PassMismatch(Flow):
        """Names `__route_a` as the producer of a file it makes no output of."""

        results_description = {}

        class Inputs(Flow.Inputs):
            i: Path = In(SourceType.Sdc, producer="__route_a", description="Not made.")

        def run(self) -> None:
            pass

    assert default_graph(_PassMismatch).between(io_flows._RouteA) == ()
    with pytest.raises(FlowSettingsException, match="no compatible output") as raised:
        parse_request("__route_a+__pass_mismatch")
    assert "passes through" not in str(raised.value)
    with pytest.raises(FlowSettingsException, match="no compatible output"):
        parse_request("__route_c+__route_b")


def test_a_request_has_one_link_for_each_pair_of_elements():
    elements = (ChainElement(_ChainSimpleProducer), ChainElement(_ChainSimpleConsumer))
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

    classes = [c for c in _classes() if c.__module__.startswith("xeda.flows")]
    # the JSON netlist feeds nextpnr, and the EDIF netlist Vivado: each by its own output
    assert _pairs(followers(YosysFpga, classes), "consumer") == [
        ("nextpnr", None, False),
        ("vivado_impl", None, False),
    ]
    # nextpnr makes a configuration of whichever family the target selects: a hint that
    # depends on the target
    assert _pairs(followers(Nextpnr, classes), "consumer") == [("fpga_pack", None, True)]
    assert _pairs(followers(FpgaPack, classes), "consumer") == [("openfpgaloader", None, False)]
    assert followers(Openfpgaloader, classes) == []
    # the Vivado flows write a bitstream on request (`bitstream`), which the loader reads
    assert _pairs(predecessors(Openfpgaloader, classes), "producer") == [
        ("fpga_pack", None, False),
        ("vivado_alt_synth", None, False),
        ("vivado_impl", None, False),
        ("vivado_synth", None, False),
    ]
    assert _pairs(predecessors(Nextpnr, classes), "producer") == [("yosys_fpga", None, False)]
    assert _pairs(predecessors(VivadoImpl, classes), "producer") == [("yosys_fpga", None, False)]
    # and its routed checkpoint (`write_checkpoint`) is what the power report reads
    assert _pairs(followers(VivadoImpl, classes), "consumer") == [
        ("openfpgaloader", None, False),
        ("vivado_power", None, False),
    ]
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
    """Whether `producer.output+consumer` is a direct link: follow relations list only those."""
    try:
        (link,) = validate_chain((ChainElement(producer, output), ChainElement(consumer)))
    except FlowSettingsException:
        return False
    return not link.through


def test_a_follow_relation_is_exactly_the_direct_links_the_chain_validator_accepts():
    from xeda.flow_runner.chains import followers

    classes = _classes()
    for producer in classes:
        for consumer in classes:
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
        (
            "yosys_fpga+nextpnr+",
            ["yosys_fpga+nextpnr+fpga_pack", "yosys_fpga+nextpnr+openfpgaloader"],
        ),
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
        ("yosys_fpga.n", ["yosys_fpga.netlist", "yosys_fpga.netlist_edif"]),
        ("yosys_fpga.netlist_e", ["yosys_fpga.netlist_edif"]),
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
    a = _toy("_DgA", outputs={"made": _data_out()})
    z = _toy("_DgZ", {"src": _data_in("__dg_a", "made")}, {"made": _data_out()})
    x = _toy("_DgX", {"src": _data_in("__dg_z", "made")}, {"made": _data_out()})
    y = _toy("_DgY", {"src": _data_in("__dg_z", "made")}, {"made": _data_out()})
    b = _toy("_DgB", {"left": _data_in("__dg_x", "made"), "right": _data_in("__dg_y", "made")})

    graph = default_graph(b)

    assert graph.root is b
    assert graph.flows() == (b, x, y, z, a)
    assert _pairs_of(graph.edges) == [
        ("__dg_b", "left", "__dg_x"),
        ("__dg_b", "right", "__dg_y"),
        ("__dg_x", "src", "__dg_z"),
        ("__dg_y", "src", "__dg_z"),
        ("__dg_z", "src", "__dg_a"),
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
    assert graph.between(_toy("_DgOther")) == ()  # not in the graph


def test_a_flow_that_takes_two_inputs_from_one_producer_has_two_edges():
    p = _toy("_DgP", outputs={"one": _data_out(), "two": _data_out()})
    c = _toy(
        "_DgC",
        {"first": _data_in("__dg_p", "one"), "second": _data_in("__dg_p", "two")},
    )

    graph = default_graph(c)

    assert graph.flows() == (c, p)
    assert _pairs_of(graph.edges) == [("__dg_c", "first", "__dg_p"), ("__dg_c", "second", "__dg_p")]
    points = binding_points(graph, p)
    assert [(edge.input, chosen) for edge, chosen in points] == [
        ("first", "one"),
        ("second", "two"),
    ]


def test_an_output_qualifier_selects_the_binding_points_that_it_fits():
    a = _toy(
        "_DgA",
        outputs={
            "o1": (Path, Out(SourceType.Data, description="First.")),
            "o2": (Path, Out(SourceType.JsonNetlist, description="Second.")),
        },
    )
    z = _toy("_DgZ", {"src": _data_in("__dg_a")})
    graph = default_graph(z)

    assert [chosen for _, chosen in binding_points(graph, a)] == ["o1"]
    assert [chosen for _, chosen in binding_points(graph, a, "o1")] == ["o1"]
    with pytest.raises(FlowSettingsException, match=r"__dg_z\.src"):
        binding_points(graph, a, "o2")
    with pytest.raises(FlowSettingsException, match=r"has no output `o3`\. Did you mean .*`o1`"):
        binding_points(graph, a, "o3")


def test_binding_points_refuse_a_producer_with_no_fitting_or_with_several_outputs():
    many = _toy("_DgMany", outputs={"files": (list[Path], Out(SourceType.Data, description="F."))})
    both = _toy("_DgBoth", outputs={"m": _data_out(), "n": _data_out()})
    none = _toy("_DgNone", outputs={"j": (Path, Out(SourceType.JsonNetlist, description="J."))})
    one_file = _toy(
        "_DgOneFile",
        {"a": _data_in("__dg_many"), "b": _data_in("__dg_both"), "c": _data_in("__dg_none")},
    )
    graph = default_graph(one_file)

    with pytest.raises(FlowSettingsException, match=r"cardinality.*__dg_one_file\.a"):
        binding_points(graph, many)
    with pytest.raises(FlowSettingsException, match=r"__dg_one_file\.b.*ambiguous.*qualify"):
        binding_points(graph, both)
    with pytest.raises(
        FlowSettingsException, match=r"__dg_one_file\.c` takes Data.*makes JsonNetlist"
    ):
        binding_points(graph, none)


def test_a_cycle_of_default_producers_ends_and_hides_no_other_path():
    a = _toy("_DgA", outputs={"made": _data_out()})
    m = _toy("_DgM", {"src": _data_in("__dg_a", "made")}, {"made": _data_out()})
    l1 = _toy("_DgLone", {"src": _data_in("__dg_ltwo", "made")}, {"made": _data_out()})
    l2 = _toy("_DgLtwo", {"src": _data_in("__dg_lone", "made")}, {"made": _data_out()})
    b = _toy("_DgB", {"loop": _data_in("__dg_lone", "made"), "line": _data_in("__dg_m", "made")})

    graph = default_graph(b)

    assert graph.flows() == (b, l1, m, l2, a)
    assert _pairs_of(graph.edges) == [
        ("__dg_b", "loop", "__dg_lone"),
        ("__dg_b", "line", "__dg_m"),
        ("__dg_lone", "src", "__dg_ltwo"),
        ("__dg_m", "src", "__dg_a"),
        ("__dg_ltwo", "src", "__dg_lone"),
    ]
    assert _pairs_of(edge for edge, _ in binding_points(graph, a)) == [("__dg_m", "src", "__dg_a")]
    assert graph.between(a) == (m,)
    assert graph.between(l2) == (l1,)
    # the flows of a cycle lie between each other and the root
    assert graph.between(l1) == (l2,)


def test_an_optional_input_is_followed_but_binds_nothing():
    a = _toy("_DgA", outputs={"made": _data_out()})
    c = _toy(
        "_DgC",
        {"extra": (Path | None, In(SourceType.Data, producer="__dg_a", description="Extra."))},
    )

    graph = default_graph(c)

    assert graph.flows() == (c, a)
    assert _pairs_of(graph.edges) == [("__dg_c", "extra", "__dg_a")]
    assert binding_points(graph, a) == ()


def test_a_producer_that_is_not_a_flow_is_skipped_and_a_flow_without_producers_is_alone():
    c = _toy("_DgC", {"src": _data_in("__dg_missing")})

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
