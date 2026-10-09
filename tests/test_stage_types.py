"""FPGA netlists and checkpoints are typed by what they are, not only by their file format.

A Vivado netlist is made of FPGA primitives, so a standard-cell flow (`openroad`) cannot take it.
A checkpoint is written at several stages of a Vivado run, and power can only be reported on the
routed one. Each case is judged by one predicate, `chains.fitting_outputs`, whether it comes from a
chain, a saved binding or a default producer.
"""

from dataclasses import replace
from itertools import product

import pytest

from xeda.design import (
    AMBIGUOUS_SUFFIXES,
    SOURCE_SUFFIXES,
    TYPE_ONLY,
    DesignSource,
    SourceType,
)
from xeda.flow import FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import fitting_outputs, parse_request, validate_chain
from xeda.flows import (
    Nextpnr,
    Openroad,
    VivadoAltSynth,
    VivadoImpl,
    VivadoPostsynthSim,
    VivadoPower,
    VivadoSynth,
    Yosys,
    YosysFpga,
)
from xeda.introspect import all_flow_classes

from .test_tool_input_equivalence import (
    OPENROAD_SETTINGS,
    VIVADO_SETTINGS,
    write_asic_design,
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
    message = refused("vivado_synth.netlist+openroad")
    assert "FpgaNetlist" in message and "VerilogNetlist" in message


def test_a_checkpoint_from_before_routing_does_not_chain_into_power():
    message = refused("vivado_alt_synth.checkpoint_synth+vivado_power")
    assert "SynthCheckpoint" in message and "RoutedCheckpoint" in message


def test_the_routed_checkpoint_chains_into_power():
    validate_chain(parse_request("vivado_synth.checkpoint_route+vivado_power").elements)
    validate_chain(parse_request("vivado_alt_synth.checkpoint_route+vivado_power").elements)


def test_a_standard_cell_netlist_still_chains_into_openroad():
    validate_chain(parse_request("yosys+openroad").elements)


def test_an_explicit_binding_of_the_wrong_kind_is_refused_naming_the_types(tmp_path):
    write_asic_design(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "openroad",
            tmp_path / "design.yaml",
            flow_settings=[
                *OPENROAD_SETTINGS,
                "flows.vivado_synth.fpga=xc7a12tcsg325-1",
                "flows.vivado_synth.clock.period=5.0",
                "inputs.netlist=vivado_synth.netlist",
            ],
        )
    assert "VerilogNetlist" in str(error.value) and "FpgaNetlist" in str(error.value)


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
                    ["flows.vivado_alt_synth.fpga=xc7a12tcsg325-1"]
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
        assert member not in {typed for typed, _variant in SOURCE_SUFFIXES.values()}
    assert "dcp" not in SOURCE_SUFFIXES
    assert AMBIGUOUS_SUFFIXES["dcp"] == CHECKPOINTS
    path = tmp_path / "top.dcp"
    path.write_text("x\n")
    with pytest.raises(ValueError, match=r"`\.dcp` names several kinds of file") as error:
        DesignSource(path)
    assert "SynthCheckpoint" in str(error.value) and "RoutedCheckpoint" in str(error.value)
    for member in CHECKPOINTS:
        assert DesignSource({"file": str(path), "type": member.name}).type is member


# ------------------------------------------------------------------------------ the sweep

NETLIST_KINDS = frozenset(
    {
        SourceType.JsonNetlist,
        SourceType.VerilogNetlist,
        SourceType.VhdlNetlist,
        SourceType.Edif,
        SourceType.Blif,
        *FPGA_NETLISTS,
    }
)
CHECKPOINT_KINDS = frozenset({SourceType.Checkpoint, *CHECKPOINTS})

#: Edges between two netlists or two checkpoints that a consumer does not declare as its default
#: wiring but takes all the same, each with why it is right (or a known limit). The default
#: wiring (`In(producer=, output=)`) documents every other edge.
REVIEWED_EDGES = {
    # the alternative synthesis writes the same outputs as `vivado_synth`, so it feeds the same
    # consumers by an explicit binding or a chain
    ("vivado_alt_synth", "netlist", "vivado_postsynth_sim", "netlist"),
    ("vivado_alt_synth", "netlist_timing", "vivado_postsynth_sim", "netlist_timing"),
    ("vivado_alt_synth", "checkpoint_route", "vivado_power", "checkpoint"),
}


def kind_of(types) -> str | None:
    members = set(types)
    if members and members <= NETLIST_KINDS:
        return "netlist"
    if members and members <= CHECKPOINT_KINDS:
        return "checkpoint"
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


def test_the_sweep_finds_an_edge_a_wrong_type_would_open():
    """The oracle's teeth: with the netlist typed as before, the sweep reports vivado_synth's
    netlist as an undocumented input of openroad."""

    def typed_as_before(flow):
        outputs = declared_outputs(flow)
        if flow is VivadoSynth:
            outputs["netlist"] = replace(outputs["netlist"], types=(SourceType.VerilogNetlist,))
        return outputs

    opened = undocumented(fitting_edges(typed_as_before)) - REVIEWED_EDGES
    assert ("vivado_synth", "netlist", "openroad", "netlist") in opened


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
    assert declared_inputs(Openroad)["netlist"].types == (SourceType.VerilogNetlist,)
    assert declared_outputs(YosysFpga)["netlist"].types == (SourceType.JsonNetlist,)
    assert declared_outputs(YosysFpga)["netlist_edif"].types == (SourceType.Edif,)
    assert declared_inputs(Nextpnr)["netlist"].types == (SourceType.JsonNetlist,)
    assert declared_inputs(VivadoImpl)["netlist"].types == (SourceType.Edif,)
