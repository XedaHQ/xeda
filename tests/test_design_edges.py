"""The design kind of a declaration: the `design` input of a flow that reads design sources, the
design output of a converter flow, and the edges between them.

A design input fits a design output and nothing else, and an input of files never fits a design
output. A chain link from a converter binds the `design` input of every flow of the plan of the
second element that it fits. Which flow converts a design for a consumer that cannot read it is a
question of the resolver and the launcher, not of these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import DESIGN, Flow, FlowSettingsException, In, Out
from xeda.flow.derived import DerivedSources
from xeda.flow.io import (
    IO_MARKER,
    check_io_declarations,
    declared_inputs,
    declared_outputs,
)
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import (
    converter_edges,
    default_graph,
    fitting_outputs,
    followers,
    match_required_inputs,
    parse_request,
    predecessors,
)
from xeda.flow_runner.settings_layers import transitive_dependencies
from xeda.flows import Bsc, BscSim, GhdlSim, Nextpnr, OpenroadPlace, Verilator, Yosys
from xeda.introspect import flow_chain_cells, flow_info, plan_info

from .derived_flows import converter_flows
from .flow_registry import preserved_flow_registry
from .settings_samples import flow_classes

DESCRIPTION = "A declaration for a test."


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _verilog_design(tmp_path: Path, **fields) -> Design:
    """A design the Verilog tools read as it is. The planned design of a converted consumer (the
    check of its language on what the converter will write) is for the resolver stage that plans
    the conversion; these tests bind a converter by hand."""
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(); endmodule\n")
    return Design(name="d", design_root=root, rtl={"sources": ["top.v"], "top": "top"}, **fields)


def _bluespec_design(tmp_path: Path, **fields) -> Design:
    """A design for planning a Bluespec converter by chain or saved binding."""
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    (root / "Top.bsv").write_text("package Top; endpackage\n")
    return Design(
        name="d", design_root=root, rtl={"sources": ["Top.bsv"], "top": "mkTop"}, **fields
    )


# ------------------------------------------------------------------------ the declaration


def test_the_design_marker_is_a_value_kind_with_no_types() -> None:
    marker = In(DESIGN, description=DESCRIPTION).json_schema_extra[IO_MARKER]
    assert (marker["value"], marker["types"]) == ("design", [])
    marker = Out(DESIGN, description=DESCRIPTION).json_schema_extra[IO_MARKER]
    assert (marker["value"], marker["types"]) == ("design", [])
    assert (
        "value" not in In(SourceType.Verilog, description=DESCRIPTION).json_schema_extra[IO_MARKER]
    )


@pytest.mark.parametrize("declare", [In, Out])
@pytest.mark.parametrize(
    "types", [[DESIGN], [SourceType.Verilog, DESIGN], (DESIGN, SourceType.Vhdl), []]
)
def test_the_design_marker_stands_alone(declare, types) -> None:
    with pytest.raises(TypeError):
        declare(types, description=DESCRIPTION)


def test_a_flow_that_reads_design_sources_declares_the_design_input_first() -> None:
    reading = [(cls, name) for cls, name in flow_classes() if cls.reads_sources is not None]
    assert reading
    for cls, name in reading:
        inputs = declared_inputs(cls)
        design = inputs["design"]
        assert next(iter(inputs)) == "design", name
        assert (design.is_design, design.value, design.types) == (True, "design", ())
        assert (design.cardinality, design.required) == ("one", True)
        assert design.producer is None and design.output is None


def test_a_flow_that_reads_no_design_source_has_no_design_input() -> None:
    for cls, name in flow_classes():
        if cls.reads_sources is None:
            assert "design" not in declared_inputs(cls), name


def test_a_nonreader_can_inherit_the_hidden_base_design_input() -> None:
    class _NonReader(Flow):
        reads_sources = None

    assert "design" not in declared_inputs(_NonReader)


def test_the_design_input_is_the_only_input_a_flow_never_reads() -> None:
    """The field stays unset: the flow reads the design as `self.design`."""
    assert Yosys.Inputs().design is None
    assert not any(declaration.is_design for declaration in declared_outputs(Yosys).values())


def test_bsc_is_the_flow_with_a_design_output() -> None:
    converters = [
        name
        for cls, name in flow_classes()
        if any(declaration.is_design for declaration in declared_outputs(cls).values())
    ]
    assert converters == ["bsc"]
    made = declared_outputs(Bsc)["design"]
    assert (made.value, made.types, made.cardinality, made.enabled_by) == (
        "design",
        (),
        "one",
        None,
    )
    assert not declared_outputs(BscSim)


def test_a_declaration_of_files_stays_what_it_was() -> None:
    declaration = declared_inputs(Nextpnr)["netlist"]
    assert (declaration.value, declaration.is_design) == ("files", False)
    assert declared_outputs(Yosys)["netlist"].is_design is False


@pytest.mark.parametrize(
    "reads", [frozenset({SourceType.Verilog}), None], ids=["reader", "nonreader"]
)
@pytest.mark.parametrize(
    "argument",
    [
        {"producer": "bsc"},
        {"producer": "bsc", "output": "design"},
        {"optional": True},
        {"same_producer_as": "design"},
        {"paired": True},
        {"via": "design"},
        {"equivalence": True},
    ],
)
def test_a_design_input_takes_no_argument_of_a_file_input(argument, reads) -> None:
    with pytest.raises(TypeError, match="_Refused"):

        class _Refused(Flow):
            reads_sources = reads

            class Inputs(Flow.Inputs):
                other: Design | None = In(DESIGN, description=DESCRIPTION, **argument)


@pytest.mark.parametrize(
    "reads", [frozenset({SourceType.Verilog}), None], ids=["reader", "nonreader"]
)
def test_a_design_input_is_a_design_or_none(reads) -> None:
    with pytest.raises(TypeError, match="Design"):

        class _Refused(Flow):
            reads_sources = reads

            class Inputs(Flow.Inputs):
                other: Path | None = In(DESIGN, description=DESCRIPTION)


def test_a_design_output_is_always_written() -> None:
    with pytest.raises(TypeError, match="enabled_by"):

        class _Refused(Flow):
            reads_sources = frozenset({SourceType.Verilog})

            class Settings(Flow.Settings):
                switch: bool = False

            class Outputs(Flow.Outputs):
                design: DerivedSources | None = Out(
                    DESIGN, enabled_by="switch", description=DESCRIPTION
                )

            @classmethod
            def design_conversion(cls, settings):
                return None


def test_a_flow_derives_one_design() -> None:
    with pytest.raises(TypeError, match="two design outputs|2 design outputs"):

        class _Refused(Flow):
            reads_sources = frozenset({SourceType.Verilog})

            class Outputs(Flow.Outputs):
                first: DerivedSources | None = Out(DESIGN, description=DESCRIPTION)
                second: DerivedSources | None = Out(DESIGN, description=DESCRIPTION)

            @classmethod
            def design_conversion(cls, settings):
                return None


def test_a_design_output_is_derived_sources() -> None:
    with pytest.raises(TypeError, match="DerivedSources"):

        class _Refused(Flow):
            reads_sources = frozenset({SourceType.Verilog})

            class Outputs(Flow.Outputs):
                design: Path | None = Out(DESIGN, description=DESCRIPTION)

            @classmethod
            def design_conversion(cls, settings):
                return None


def test_a_flow_with_a_design_output_defines_its_conversion() -> None:
    with pytest.raises(TypeError, match="design_conversion"):

        class _Refused(Flow):
            reads_sources = frozenset({SourceType.Verilog})

            class Outputs(Flow.Outputs):
                design: DerivedSources | None = Out(DESIGN, description=DESCRIPTION)


def test_a_declaration_check_accepts_the_converters_of_the_tests() -> None:
    for converter in converter_flows():
        check_io_declarations(converter)
    assert Flow.design_conversion(Flow.Settings()) is None


def test_the_conversion_of_bsc_is_a_pure_declaration() -> None:
    conversion = Bsc.design_conversion(Bsc.Settings())
    assert conversion is not None
    assert conversion.transforms == {SourceType.Bluespec}
    assert conversion.produces == {SourceType.Verilog}
    assert BscSim.design_conversion(BscSim.Settings()) is None


# ------------------------------------------------------------------------ the edge predicate


def test_a_design_input_fits_a_design_output() -> None:
    design = declared_inputs(Yosys)["design"]
    assert fitting_outputs(Bsc, design) == (["design"], [])
    assert match_required_inputs(Bsc, Yosys) == [("design", "design")]
    assert match_required_inputs(Bsc, Verilator) == [("design", "design")]


def test_a_design_input_fits_no_output_of_files() -> None:
    design = declared_inputs(Verilator)["design"]
    assert fitting_outputs(Yosys, design) == ([], [])
    with pytest.raises(FlowSettingsException, match="design"):
        match_required_inputs(Yosys, Verilator)


def test_an_input_of_files_fits_no_design_output() -> None:
    netlist = declared_inputs(Nextpnr)["netlist"]
    assert fitting_outputs(Bsc, netlist) == ([], [])
    with pytest.raises(FlowSettingsException):
        match_required_inputs(Bsc, Nextpnr)
    place = declared_inputs(OpenroadPlace)["netlist"]
    assert fitting_outputs(Bsc, place) == ([], [])


def test_the_kind_decides_before_the_types() -> None:
    """A design output has no types, so the subset rule would take it for every input."""
    assert not declared_outputs(Bsc)["design"].types
    for cls, name in flow_classes():
        for declaration in declared_inputs(cls).values():
            if not declaration.is_design:
                assert fitting_outputs(Bsc, declaration) == ([], []), f"{name}.{declaration.name}"


# ------------------------------------------------------------------------ the default graph


def test_bsc_is_a_converter_edge_of_the_flows_that_read_verilog_and_not_bluespec() -> None:
    reached = {
        name for cls, name in flow_classes() if any(e.producer is Bsc for e in converter_edges(cls))
    }
    assert {"yosys", "yosys_fpga", "verilator", "vivado_synth", "vivado_postsynth_sim"} <= reached
    assert not reached & {"bsc", "bsc_sim", "ghdl_sim", "ghdl_synth", "nvc", "openroad"}
    edge = converter_edges(Yosys)[0]
    assert (edge.consumer, edge.input, edge.producer, edge.output) == (
        Yosys,
        "design",
        Bsc,
        "design",
    )


def test_the_default_graph_has_the_converter_edges() -> None:
    graph = default_graph(Yosys)
    assert [(e.consumer.name, e.input, e.producer.name) for e in graph.edges] == [
        ("yosys", "design", "bsc")
    ]
    flows = [flow.name for flow in default_graph(Nextpnr).flows()]
    assert flows == ["nextpnr", "yosys_fpga", "bsc"]
    assert "bsc" not in [flow.name for flow in default_graph(OpenroadPlace).flows()[:2]]
    assert "bsc" in [flow.name for flow in default_graph(OpenroadPlace).flows()]


def test_a_converter_does_not_fit_a_flow_that_reads_what_it_converts() -> None:
    assert converter_edges(BscSim) == []
    assert converter_edges(Bsc) == []


def test_a_converter_edge_follows_the_conversion_of_each_converter() -> None:
    fake, rival, to_vhdl = converter_flows()
    names = [edge.producer.name for edge in converter_edges(Verilator)]
    assert names == sorted(["bsc", fake.name, rival.name])  # the VHDL converter is no fit
    # a VHDL-only flow takes the VHDL converter and no other
    assert [edge.producer for edge in converter_edges(GhdlSim)] == [to_vhdl]


def test_a_flow_of_the_run_may_be_set_on_the_command_line_with_its_converter() -> None:
    """`-s flows.bsc.<setting>` names a flow of a run of `yosys`, as it does in the chain."""
    assert "bsc" in transitive_dependencies(Yosys)
    assert "bsc" in transitive_dependencies(Nextpnr)
    assert "bsc" in transitive_dependencies(Verilator)
    assert "bsc" not in transitive_dependencies(BscSim)


# ------------------------------------------------------------------------ chains


def _link(text: str):
    (link,) = parse_request(text).links
    return (
        [flow.name for flow in link.through],
        [(flow.name, name, made) for flow, name, made in link.binds],
    )


def test_a_chain_binds_the_design_input_of_the_flow_after_the_converter() -> None:
    assert _link("bsc+yosys") == ([], [("yosys", "design", "design")])
    assert _link("bsc+verilator") == ([], [("verilator", "design", "design")])


def test_a_chain_passes_through_to_the_design_input_it_binds() -> None:
    through, binds = _link("bsc+openroad")
    assert through == ["yosys", "openroad_place", "openroad_cts", "openroad_grt"]
    assert binds == [("yosys", "design", "design")]
    assert _link("bsc+fpga_pack") == (
        ["yosys_fpga", "nextpnr"],
        [("yosys_fpga", "design", "design")],
    )
    assert _link("bsc+nextpnr") == (["yosys_fpga"], [("yosys_fpga", "design", "design")])


def test_a_chain_from_a_converter_continues_as_it_did() -> None:
    request = parse_request("bsc+yosys_fpga+nextpnr+fpga_pack")
    binds = [[(flow.name, name) for flow, name, _ in link.binds] for link in request.links]
    assert binds == [
        [("yosys_fpga", "design")],
        [("nextpnr", "netlist")],
        [("fpga_pack", "config")],
    ]


def test_a_chain_from_a_converter_binds_every_design_input_it_fits() -> None:
    """`vivado_postsynth_sim` has a design input of its own, and so has the synthesis behind it."""
    through, binds = _link("bsc+vivado_postsynth_sim")
    assert through == ["vivado_synth"]
    assert binds == [
        ("vivado_postsynth_sim", "design", "design"),
        ("vivado_synth", "design", "design"),
    ]


def test_with_two_converters_a_chain_picks_one_for_every_design_input() -> None:
    fake, rival, _to_vhdl = converter_flows()
    for converter in ("bsc", fake.name, rival.name):
        through, binds = _link(f"{converter}+vivado_postsynth_sim")
        assert through == ["vivado_synth"]
        assert {(flow, name) for flow, name, _ in binds} == {
            ("vivado_postsynth_sim", "design"),
            ("vivado_synth", "design"),
        }


@pytest.mark.parametrize("consumer", ["ghdl_sim", "bsc_sim", "ghdl_synth"])
def test_a_converter_cannot_precede_a_flow_that_gains_nothing_from_it(consumer) -> None:
    with pytest.raises(
        FlowSettingsException,
        match=rf"`bsc\+{consumer}`: bsc converts Bluespec to Verilog, and no flow that {consumer} runs",
    ):
        parse_request(f"bsc+{consumer}")


def test_a_converter_cannot_precede_a_flow_that_reads_files_only() -> None:
    with pytest.raises(FlowSettingsException):
        parse_request("bsc+openfpgaloader+yosys")


def test_the_link_to_a_file_input_is_what_it_was() -> None:
    assert _link("yosys+openroad_place") == ([], [("openroad_place", "netlist", "netlist")])


def test_chains_list_the_converter_among_the_flows_that_follow_and_precede() -> None:
    candidates = [cls for cls, _name in flow_classes()]
    follow = {edge.consumer.name for edge in followers(Bsc, candidates)}
    assert {"yosys", "yosys_fpga", "verilator", "openroad", "fpga_pack"} <= follow
    assert not follow & {"ghdl_sim", "bsc_sim", "bsc"}
    before = {edge.producer.name for edge in predecessors(Yosys, candidates)}
    assert "bsc" in before


def test_flow_info_renders_the_design_kind() -> None:
    info = flow_info("yosys")
    design = next(item for item in info["inputs"] if item["name"] == "design")
    assert (design["value"], design["types"], design["required"]) == ("design", [], True)
    made = next(item for item in flow_info("bsc")["outputs"] if item["name"] == "design")
    assert (made["value"], made["types"], made["enabled_by"]) == ("design", [], None)
    takes, makes, following = flow_chain_cells(flow_info("bsc"))
    assert "design (design)" in takes and makes == "design (design)"
    assert "openroad (through yosys" in following
    netlist = next(item for item in flow_info("nextpnr")["inputs"] if item["name"] == "netlist")
    assert netlist["value"] == "files"


# ------------------------------------------------------------------------ the resolver


def test_an_unbound_design_input_changes_no_plan(tmp_path) -> None:
    """The plan of a design that Verilog tools read has no node and no input for the design."""
    design = _verilog_design(tmp_path)
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan("yosys", design)
    assert [node.name for node in plan.nodes] == ["yosys"]
    assert plan.node("yosys").inputs == ()
    assert [i["name"] for node in plan_info(plan)["nodes"] for i in node["inputs"]] == []
    assert "design" not in plan_info(plan)["nodes"][0]["input_types"]


def test_a_design_input_bound_to_bsc_is_planned_through_the_binding(tmp_path) -> None:
    design = _bluespec_design(tmp_path, flows={"yosys": {"inputs": {"design": "bsc"}}})
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan("yosys", design)
    assert [node.name for node in plan.nodes] == ["bsc", "yosys"]
    (bound,) = plan.node("yosys").inputs
    assert (bound.name, bound.origin, bound.producer, bound.output) == (
        "design",
        "producer",
        "bsc",
        "design",
    )
    assert bound.binding_origin == "file"
    assert "design" not in plan_info(plan)["nodes"][1]["input_types"]


def test_a_chain_plans_the_converter_before_the_flow_it_binds(tmp_path) -> None:
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    plan = runner.plan(parse_request("bsc+yosys"), _bluespec_design(tmp_path))
    assert [node.name for node in plan.nodes] == ["bsc", "yosys"]
    assert plan.node("yosys").inputs[0].binding_origin == "chain"


def test_a_chain_through_other_flows_binds_the_design_input_of_the_first(tmp_path) -> None:
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    design = _bluespec_design(tmp_path, flows={"openroad": {"platform": "nangate45"}})
    plan = runner.plan(parse_request("bsc+openroad"), design)
    assert [node.name for node in plan.nodes] == [
        "bsc",
        "yosys",
        "openroad_place",
        "openroad_cts",
        "openroad_grt",
        "openroad",
    ]
    assert plan.node("yosys").inputs[0].name == "design"
    assert plan.node("openroad_place").inputs[0].name == "netlist"


def test_a_binding_and_a_chain_plan_the_same_nodes(tmp_path) -> None:
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    chained = runner.plan(parse_request("bsc+yosys"), _bluespec_design(tmp_path))
    bound = runner.plan(
        "yosys", _bluespec_design(tmp_path, flows={"yosys": {"inputs": {"design": "bsc"}}})
    )
    assert [(n.name, n.flowrun_hash, n.run_path) for n in chained.nodes] == [
        (n.name, n.flowrun_hash, n.run_path) for n in bound.nodes
    ]


def test_a_design_input_is_refused_a_converter_that_writes_what_it_cannot_read(tmp_path) -> None:
    """The final check: the conversion of the producer, under its settings, writes only
    languages that the consumer reads."""
    _fake, _rival, to_vhdl = converter_flows()
    design = _verilog_design(tmp_path, flows={"verilator": {"inputs": {"design": to_vhdl.name}}})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException, match=r"verilator\.design.*no compatible output"):
        runner.plan("verilator", design)


@pytest.mark.parametrize(
    "producer, message",
    [
        ("verilator", r"verilator declares no outputs, so it cannot supply yosys\.design"),
        ("yosys_fpga", r"yosys_fpga has no output yosys\.design can take \(design\)"),
    ],
)
def test_a_design_input_is_refused_a_flow_without_a_design_output(
    producer, message, tmp_path
) -> None:
    design = _verilog_design(tmp_path, flows={"yosys": {"inputs": {"design": producer}}})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException, match=message):
        runner.plan("yosys", design)


def test_an_input_of_files_is_refused_the_design_output_of_a_converter(tmp_path) -> None:
    design = _verilog_design(tmp_path, flows={"openroad_place": {"inputs": {"netlist": "bsc"}}})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException, match=r"openroad_place\.netlist"):
        runner.plan("openroad_place", design)
