"""FPGA netlists and checkpoints are typed by what they are, not only by their file format.

A Vivado netlist is made of FPGA primitives, so a flow that takes a JSON netlist cannot take it.
A checkpoint is written at several stages of a Vivado run, and power can only be reported on the
routed one. Each case is judged by one predicate, `chains.fitting_outputs`, whether it comes from a
chain, a saved binding or a default producer.
"""

import re
from dataclasses import replace
from itertools import product

import pytest

from xeda.design import (
    AMBIGUOUS_SUFFIXES,
    SOURCE_SUFFIXES,
    TYPE_ONLY,
    Source,
    SourceType,
)
from xeda.flow import FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import fitting_outputs, parse_request, validate_chain
from xeda.flows import (
    Nextpnr,
    VivadoAltSynth,
    VivadoImpl,
    VivadoPostsynthSim,
    VivadoPower,
    VivadoSynth,
    Yosys,
    YosysFpga,
)
from xeda.introspect import all_flow_classes

from .openroad_utils import openroad_settings
from .tiny_platform import tiny_store
from .test_tool_input_equivalence import (
    VIVADO_SETTINGS,
    write_vivado_design,
)

FPGA_NETLISTS = (SourceType.FpgaNetlist, SourceType.FpgaTimingNetlist)
CHECKPOINTS = (SourceType.SynthCheckpoint, SourceType.RoutedCheckpoint)


def refused(request: str) -> str:
    with pytest.raises(FlowSettingsException) as error:
        validate_chain(parse_request(request).elements)
    return str(error.value)


# ------------------------------------------------------------------------------ the two chains


def test_a_vivado_netlist_does_not_chain_into_an_asic_flow():
    message = refused("vivado_synth.netlist+openroad_place")
    assert "FpgaNetlist" in message and "VerilogNetlist" in message


def test_a_standard_cell_netlist_chains_into_the_placement():
    validate_chain(parse_request("yosys+openroad_place").elements)


def test_a_standard_cell_netlist_still_chains_into_the_last_openroad_flow():
    """The request passes through the flows between the two ends: the placement, the clock tree
    and the global route run in between."""
    validate_chain(parse_request("yosys+openroad").elements)


def test_an_explicit_binding_of_the_wrong_kind_to_an_openroad_flow_is_refused_naming_the_types(
    tmp_path,
):
    write_vivado_design(tmp_path)
    store = tiny_store(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "openroad_place",
            tmp_path / "design.yaml",
            flow_settings=[
                *openroad_settings(store),
                *VIVADO_SETTINGS,
                "inputs.netlist=vivado_synth.netlist",
            ],
        )
    assert "VerilogNetlist" in str(error.value) and "FpgaNetlist" in str(error.value)


def test_a_vivado_netlist_does_not_chain_into_a_json_netlist_flow():
    message = refused("vivado_synth.netlist+nextpnr")
    assert "FpgaNetlist" in message and "JsonNetlist" in message


def test_a_checkpoint_from_before_routing_does_not_chain_into_power():
    message = refused("vivado_alt_synth.checkpoint_synth+vivado_power")
    assert "SynthCheckpoint" in message and "RoutedCheckpoint" in message


def test_the_routed_checkpoint_chains_into_power():
    validate_chain(parse_request("vivado_synth.checkpoint_route+vivado_power").elements)
    validate_chain(parse_request("vivado_alt_synth.checkpoint_route+vivado_power").elements)


def test_an_explicit_binding_of_the_wrong_kind_is_refused_naming_the_types(tmp_path):
    write_vivado_design(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "nextpnr",
            tmp_path / "design.yaml",
            flow_settings=[
                *VIVADO_SETTINGS,
                "flows.nextpnr.fpga.part=LFE5U-25F-6BG381C",
                "inputs.netlist=vivado_synth.netlist",
            ],
        )
    assert "JsonNetlist" in str(error.value) and "FpgaNetlist" in str(error.value)


def test_power_refuses_a_checkpoint_bound_from_before_routing(tmp_path):
    write_vivado_design(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "vivado_power",
            tmp_path / "design.yaml",
            flow_settings=[*VIVADO_SETTINGS, "inputs.checkpoint=vivado_synth.checkpoint_synth"],
        )
    assert "RoutedCheckpoint" in str(error.value) and "SynthCheckpoint" in str(error.value)


def test_the_functional_netlist_of_vivado_still_binds_to_the_post_synthesis_simulation(tmp_path):
    write_vivado_design(tmp_path)
    plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
        "vivado_postsynth_sim",
        tmp_path / "design.yaml",
        flow_settings=[*VIVADO_SETTINGS, "inputs.netlist=vivado_synth.netlist"],
    )
    simulation = plan.node("vivado_postsynth_sim")
    bound = {item.name: item.references for item in simulation.inputs}
    assert [(ref.node, ref.output) for ref in bound["netlist"]] == [("vivado_synth", "netlist")]
    fitting, many = fitting_outputs(VivadoSynth, declared_inputs(VivadoPostsynthSim)["netlist"])
    assert fitting == ["netlist"] and many == []


def test_the_declarations_name_the_stage():
    for flow in (VivadoSynth, VivadoAltSynth):
        outputs = declared_outputs(flow)
        assert outputs["netlist"].types == (SourceType.FpgaNetlist,)
        assert outputs["netlist_timing"].types == (SourceType.FpgaTimingNetlist,)
        assert outputs["checkpoint_synth"].types == (SourceType.SynthCheckpoint,)
        assert outputs["checkpoint_route"].types == (SourceType.RoutedCheckpoint,)
    inputs = declared_inputs(VivadoPostsynthSim)
    assert inputs["netlist"].types == (SourceType.FpgaNetlist,)
    assert inputs["netlist_timing"].types == (SourceType.FpgaTimingNetlist,)
    assert declared_inputs(VivadoPower)["checkpoint"].types == (SourceType.RoutedCheckpoint,)


@pytest.mark.parametrize("producer", ["vivado_synth", "vivado_alt_synth"])
@pytest.mark.parametrize(
    "crossing, wanted, given",
    [
        ("netlist_timing", "FpgaTimingNetlist", "FpgaNetlist"),
        ("netlist", "FpgaNetlist", "FpgaTimingNetlist"),
    ],
)
def test_the_functional_and_the_timing_netlist_cannot_be_crossed(
    tmp_path, producer, crossing, wanted, given
):
    """Binding the functional netlist as the timing one (the simulation then elaborates cells of
    the wrong library, or simulates without delays) or the other way round is refused by type."""
    other = "netlist" if crossing == "netlist_timing" else "netlist_timing"
    write_vivado_design(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "vivado_postsynth_sim",
            tmp_path / "design.yaml",
            flow_settings=[
                *VIVADO_SETTINGS,
                *(
                    ["flows.vivado_alt_synth.fpga.part=xc7a12tcsg325-1"]
                    if producer == "vivado_alt_synth"
                    else []
                ),
                f"inputs.{crossing}={producer}.{other}",
            ],
        )
    message = " ".join(str(error.value).split())
    assert f"takes {wanted}" in message and f"makes {given}" in message
    assert f"vivado_postsynth_sim.{crossing}" in message


def test_the_chain_check_finds_no_edge_between_the_crossed_netlists():
    for output, taken in (("netlist", "netlist_timing"), ("netlist_timing", "netlist")):
        fitting, _many = fitting_outputs(
            VivadoSynth, declared_inputs(VivadoPostsynthSim)[taken], output=output
        )
        assert fitting == []


# ------------------------------------------------------------------------------ the new members


def test_the_stage_types_are_given_by_type_only_and_a_checkpoint_suffix_asks_for_one(tmp_path):
    for member in (*FPGA_NETLISTS, *CHECKPOINTS):
        assert member in TYPE_ONLY
        assert member not in set(SOURCE_SUFFIXES.values())
    assert "dcp" not in SOURCE_SUFFIXES
    assert AMBIGUOUS_SUFFIXES["dcp"] == CHECKPOINTS
    path = tmp_path / "top.dcp"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"`\.dcp` names several kinds of file") as error:
        Source(path)
    assert "SynthCheckpoint" in str(error.value) and "RoutedCheckpoint" in str(error.value)
    for member in CHECKPOINTS:
        assert Source({"file": str(path), "type": member.name}).type is member


def test_the_openroad_types_are_given_by_type_only_and_an_odb_suffix_asks_for_one(tmp_path):
    for member in (*OPENROAD_DBS, *OPENROAD_SDCS):
        assert member in TYPE_ONLY
        assert member not in set(SOURCE_SUFFIXES.values())
    assert "odb" not in SOURCE_SUFFIXES
    assert set(AMBIGUOUS_SUFFIXES["odb"]) == OPENROAD_DBS
    path = tmp_path / "top.odb"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"`\.odb` names several kinds of file"):
        Source(path)
    for member in OPENROAD_DBS:
        assert Source({"file": str(path), "type": member.name}).type is member


# ------------------------------------------------------------------------------ the sweep

NETLIST_KINDS = frozenset(
    {
        SourceType.JsonNetlist,
        SourceType.VerilogNetlist,
        SourceType.GenericVerilogNetlist,
        SourceType.VhdlNetlist,
        SourceType.Edif,
        SourceType.Blif,
        *FPGA_NETLISTS,
    }
)
CHECKPOINT_KINDS = frozenset({SourceType.Checkpoint, *CHECKPOINTS})
#: The OpenROAD databases and the SDC files that a stage writes with its database. Each stage has
#: its own member of both, so a database or an SDC file fits the next stage only.
OPENROAD_DBS = frozenset(
    {
        SourceType.OpenroadDbPlaced,
        SourceType.OpenroadDbCts,
        SourceType.OpenroadDbGlobalRouted,
        SourceType.OpenroadDbRouted,
    }
)
OPENROAD_SDCS = frozenset(
    {
        SourceType.OpenroadSdcPlaced,
        SourceType.OpenroadSdcCts,
        SourceType.OpenroadSdcGlobalRouted,
        SourceType.OpenroadSdcRouted,
    }
)

#: Edges between two netlists or two checkpoints that a consumer does not declare as its default
#: wiring but takes all the same, each with why it is right (or a known limit). The default
#: wiring (`In(producer=, output=)`) documents every other edge.
REVIEWED_EDGES = {
    # the alternative synthesis writes the same outputs as `vivado_synth`, so it feeds the same
    # consumers by an explicit binding or a chain
    ("vivado_alt_synth", "netlist", "vivado_postsynth_sim", "netlist"),
    ("vivado_alt_synth", "netlist_timing", "vivado_postsynth_sim", "netlist_timing"),
    ("vivado_alt_synth", "checkpoint_route", "vivado_power", "checkpoint"),
    # the implementation of an EDIF netlist writes them too, under the same names, switched on by
    # the same settings; a binding or a chain feeds the consumers from it
    ("vivado_impl", "netlist", "vivado_postsynth_sim", "netlist"),
    ("vivado_impl", "netlist_timing", "vivado_postsynth_sim", "netlist_timing"),
    ("vivado_impl", "checkpoint_route", "vivado_power", "checkpoint"),
    # the routed netlist is a standard-cell netlist, so the placement flow takes it too; placing
    # it again needs an explicit binding
    ("openroad", "netlist", "openroad_place", "netlist"),
}


def kind_of(types) -> str | None:
    members = set(types)
    if members and members <= NETLIST_KINDS:
        return "netlist"
    if members and members <= CHECKPOINT_KINDS:
        return "checkpoint"
    if members and members <= OPENROAD_DBS:
        return "database"
    if members and members <= OPENROAD_SDCS:
        return "stage_sdc"
    return None


def fitting_edges(outputs_of=declared_outputs):
    """Every (producer, output, consumer, input) the one edge predicate accepts where both ends
    are netlists or both are checkpoints, and whether the consumer's default wiring names it."""
    found = {}
    flows = [flow for flow in all_flow_classes() if flow.__module__.startswith("xeda.")]
    for producer, consumer in product(flows, repeat=2):
        if producer is consumer:
            continue
        for input_name, declaration in declared_inputs(consumer).items():
            for output_name, made in outputs_of(producer).items():
                kind = kind_of(made.types)
                if kind is None or kind != kind_of(declaration.types):
                    continue
                fitting, _many = fitting_outputs(
                    producer,
                    declaration,
                    output=output_name,
                    selected_output_types={output_name: made.types},
                )
                if output_name not in fitting:
                    continue
                key = (producer.name, output_name, consumer.name, input_name)
                found[key] = (declaration.producer, declaration.output) == (
                    producer.name,
                    output_name,
                )
    return found


def undocumented(edges) -> set:
    return {edge for edge, declared in edges.items() if not declared}


def test_every_netlist_or_checkpoint_edge_is_declared_or_reviewed():
    edges = fitting_edges()
    assert edges, "the sweep found no edge: its predicate is broken"
    assert undocumented(edges) == REVIEWED_EDGES, (
        "an edge between two netlists or two checkpoints that no consumer declares as its default "
        "wiring has to be reviewed (add it to REVIEWED_EDGES with the reason), and a reviewed edge "
        f"that no longer exists has to go: {sorted(undocumented(edges) ^ REVIEWED_EDGES)}"
    )


def test_the_openroad_stages_hand_each_other_their_own_database_and_sdc():
    edges = fitting_edges()
    assert {edge for edge in edges if "openroad" in edge[0] and edge[2].startswith("openroad")} == {
        ("openroad_place", "odb", "openroad_cts", "odb"),
        ("openroad_place", "sdc", "openroad_cts", "sdc"),
        ("openroad_cts", "odb", "openroad_grt", "odb"),
        ("openroad_cts", "sdc", "openroad_grt", "sdc"),
        ("openroad_grt", "odb", "openroad", "odb"),
        ("openroad_grt", "sdc", "openroad", "sdc"),
        ("openroad", "netlist", "openroad_place", "netlist"),
    }


def test_the_sweep_finds_an_edge_a_wrong_type_would_open_for_the_openroad_flows():
    """The oracle's teeth: with the netlist of `vivado_synth` typed as a standard-cell netlist,
    the sweep reports it as an undocumented input of `openroad_place`."""

    def typed_as_before(flow):
        outputs = declared_outputs(flow)
        if flow is VivadoSynth:
            outputs["netlist"] = replace(outputs["netlist"], types=(SourceType.VerilogNetlist,))
        return outputs

    opened = undocumented(fitting_edges(typed_as_before)) - REVIEWED_EDGES
    assert ("vivado_synth", "netlist", "openroad_place", "netlist") in opened


def test_the_sweep_finds_an_edge_a_wrong_type_would_open():
    """With the netlist typed as a JSON netlist, the sweep reports vivado_synth's netlist as an
    undocumented input of nextpnr, so the sweep does detect an edge a wrong type would open."""

    def typed_as_json(flow):
        outputs = declared_outputs(flow)
        if flow is VivadoSynth:
            outputs["netlist"] = replace(outputs["netlist"], types=(SourceType.JsonNetlist,))
        return outputs

    opened = undocumented(fitting_edges(typed_as_json)) - REVIEWED_EDGES
    assert ("vivado_synth", "netlist", "nextpnr", "netlist") in opened


def test_the_flows_that_take_a_stage_type_are_the_ones_that_can_use_it():
    consumers = {
        (flow.name, name)
        for flow in all_flow_classes()
        if flow.__module__.startswith("xeda.")
        for name, declaration in declared_inputs(flow).items()
        if set(declaration.types) & {*FPGA_NETLISTS, *CHECKPOINTS, SourceType.Checkpoint}
    }
    assert consumers == {
        ("vivado_postsynth_sim", "netlist"),
        ("vivado_postsynth_sim", "netlist_timing"),
        ("vivado_power", "checkpoint"),
    }


def test_the_other_netlists_keep_their_formats():
    assert declared_outputs(Yosys)["netlist"].types == (SourceType.VerilogNetlist,)
    assert declared_outputs(Yosys)["netlist_generic"].types == (SourceType.GenericVerilogNetlist,)
    assert declared_outputs(YosysFpga)["netlist"].types == (SourceType.JsonNetlist,)
    assert declared_outputs(YosysFpga)["netlist_edif"].types == (SourceType.Edif,)
    assert declared_inputs(Nextpnr)["netlist"].types == (SourceType.JsonNetlist,)
    assert declared_inputs(VivadoImpl)["netlist"].types == (SourceType.Edif,)


def test_a_generic_netlist_of_yosys_does_not_chain_into_the_placement():
    """No Liberty library holds the cells of yosys, so the generic netlist is a different type
    and the placement flow takes only the mapped one."""
    message = refused("yosys.netlist_generic+openroad_place")
    assert "GenericVerilogNetlist" in message and re.search(r"\bVerilogNetlist\b", message)


def test_an_explicit_binding_of_the_generic_netlist_to_the_placement_is_refused(tmp_path):
    from .openroad_utils import openroad_design, register_openroad_flows

    store = tiny_store(tmp_path)
    with register_openroad_flows(), pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "openroad_place",
            openroad_design(tmp_path / "design", rtl=True),
            flow_settings=[*openroad_settings(store), "inputs.netlist=yosys.netlist_generic"],
        )
    message = " ".join(str(error.value).split())
    assert "GenericVerilogNetlist" in message and re.search(r"\bVerilogNetlist\b", message)


def test_the_sweep_finds_an_edge_a_wrong_type_of_the_generic_netlist_would_open():
    """The oracle's teeth: with the generic netlist typed as a mapped one, the sweep reports it as
    an undocumented input of `openroad_place`."""

    def typed_as_mapped(flow):
        outputs = declared_outputs(flow)
        if flow is Yosys:
            outputs["netlist_generic"] = replace(
                outputs["netlist_generic"], types=(SourceType.VerilogNetlist,)
            )
        return outputs

    opened = undocumented(fitting_edges(typed_as_mapped)) - REVIEWED_EDGES
    assert ("yosys", "netlist_generic", "openroad_place", "netlist") in opened
