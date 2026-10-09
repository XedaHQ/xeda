"""Related inputs of one flow come from one producer.

`vivado_postsynth_sim` reads one routed design's netlists and its SDF; `vivado_power` reports a
routed checkpoint against the activity of a simulation of that same design. Binding one of these
inputs to another synthesis flow would pair one run's netlist with another run's delays, or one
run's checkpoint with another run's activity, and nothing in the files shows it. The consumer
declares the relation (`In(same_producer_as=...)`), and the resolver refuses a plan that breaks
it, naming the inputs and their producers.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsException, In, registered_flows
from xeda.flow.io import declared_inputs
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import registered_flow
from xeda.flows import VivadoPostsynthSim, VivadoPower
from xeda.introspect import all_flow_classes, flow_info

from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design

ALT = [
    "flows.vivado_alt_synth.fpga=xc7a12tcsg325-1",
    "flows.vivado_alt_synth.clock.period=5.0",
]
SIM = "flows.vivado_postsynth_sim.inputs"
POWER = "flows.vivado_power.inputs"


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def plan(tmp_path, flow, *settings, sources=()):
    write_vivado_design(tmp_path)
    if sources:
        listed = "".join(f', {{file: "{name}", type: {kind}}}' for name, kind in sources)
        for name, _kind in sources:
            (tmp_path / name).write_text("x\n")
        design = (tmp_path / "design.yaml").read_text()
        (tmp_path / "design.yaml").write_text(
            design.replace('sources: ["top.v"]', f'sources: ["top.v"{listed}]', 1)
        )
    alternative = ALT if any("vivado_alt_synth" in setting for setting in settings) else []
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        flow, tmp_path / "design.yaml", flow_settings=[*VIVADO_SETTINGS, *alternative, *settings]
    )


def refusal(tmp_path, flow, *settings, sources=()) -> str:
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, flow, *settings, sources=sources)
    return " ".join(str(error.value).split())


def producers(plan_) -> set[str]:
    return {node.name for node in plan_.nodes}


# ------------------------------------------------------------------------------ the simulation


@pytest.mark.parametrize("name", ["netlist", "netlist_timing", "sdf"])
def test_one_of_the_three_simulation_inputs_cannot_be_bound_alone(tmp_path, name):
    message = refusal(
        tmp_path,
        "vivado_postsynth_sim",
        f"{SIM}.{name}=vivado_alt_synth.{name}",
    )
    assert "vivado_postsynth_sim" in message
    assert "vivado_alt_synth" in message and "vivado_synth" in message
    assert "netlist_timing" in message and "sdf" in message
    assert "must come from the same producer" in message


def test_the_refusal_says_which_bindings_make_the_plan_valid(tmp_path):
    message = refusal(
        tmp_path,
        "vivado_postsynth_sim",
        f"{SIM}.netlist_timing=vivado_alt_synth.netlist_timing",
    )
    advice = message.partition("add `-s")[2]
    assert f"{SIM}.netlist=vivado_alt_synth.netlist" in advice
    assert f"{SIM}.sdf=vivado_alt_synth.sdf" in advice
    assert f"{SIM}.netlist_timing=" not in advice, "the input the user bound stays as it is"


def test_all_three_bound_to_one_producer_run_only_that_producer(tmp_path):
    bound = plan(
        tmp_path,
        "vivado_postsynth_sim",
        *(f"{SIM}.{name}=vivado_alt_synth.{name}" for name in ("netlist", "netlist_timing", "sdf")),
    )
    assert producers(bound) == {"vivado_alt_synth", "vivado_postsynth_sim"}


def test_the_default_wiring_is_one_producer(tmp_path):
    assert producers(plan(tmp_path, "vivado_postsynth_sim")) == {
        "vivado_synth",
        "vivado_postsynth_sim",
    }


# ------------------------------------------------------------------------------ the power report


def test_power_refuses_a_checkpoint_from_another_synthesis_than_its_activity(tmp_path):
    message = refusal(
        tmp_path, "vivado_power", f"{POWER}.checkpoint=vivado_alt_synth.checkpoint_route"
    )
    for text in ("checkpoint", "activity", "vivado_alt_synth", "vivado_synth", "netlist_timing"):
        assert text in message, text
    assert "vivado_postsynth_sim" in message
    # the fix that keeps the user's choice of synthesis: the simulation's three inputs
    for name in ("netlist", "netlist_timing", "sdf"):
        assert f"{SIM}.{name}=vivado_alt_synth.{name}" in message, name


def test_power_refuses_an_activity_from_another_synthesis_than_its_checkpoint(tmp_path):
    message = refusal(
        tmp_path,
        "vivado_power",
        *(f"{SIM}.{name}=vivado_alt_synth.{name}" for name in ("netlist", "netlist_timing", "sdf")),
    )
    assert "checkpoint" in message and "activity" in message
    assert f"{POWER}.checkpoint=vivado_alt_synth.checkpoint_route" in message


def test_the_alternative_synthesis_can_feed_the_simulation_and_the_checkpoint(tmp_path):
    bound = plan(
        tmp_path,
        "vivado_power",
        f"{POWER}.checkpoint=vivado_alt_synth.checkpoint_route",
        *(f"{SIM}.{name}=vivado_alt_synth.{name}" for name in ("netlist", "netlist_timing", "sdf")),
    )
    assert producers(bound) == {"vivado_alt_synth", "vivado_postsynth_sim", "vivado_power"}


def test_the_chains_the_old_error_suggested_are_refused_with_what_to_bind(tmp_path):
    """`vivado_alt_synth.checkpoint_synth+vivado_power` is refused by its types; the request it
    suggests passes the chain check, which sees declarations only, and the resolver then refuses
    it because the activity still comes from `vivado_synth`."""
    write_vivado_design(tmp_path)
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(tmp_path / "run", display_results=False).plan(
            "vivado_alt_synth.checkpoint_route+vivado_power",
            tmp_path / "design.yaml",
            flow_settings=[*VIVADO_SETTINGS, *ALT],
        )
    assert f"{SIM}.netlist_timing=vivado_alt_synth.netlist_timing" in " ".join(
        str(error.value).split()
    )


# ------------------------------------------------------------------------------ sources


def test_a_listed_file_is_the_users_choice_and_is_not_compared_with_a_producer(tmp_path):
    """A listed SDF with the netlist of `vivado_synth`, and a listed netlist (which fills both
    netlist inputs) with the SDF of `vivado_synth`: the files are the user's own."""
    delays = plan(tmp_path, "vivado_postsynth_sim", sources=[("delays.sdf", "Sdf")])
    assert producers(delays) == {"vivado_synth", "vivado_postsynth_sim"}
    other = tmp_path / "netlist"
    other.mkdir()
    routed = plan(other, "vivado_postsynth_sim", sources=[("routed.v", "FpgaNetlist")])
    assert producers(routed) == {"vivado_synth", "vivado_postsynth_sim"}


def test_a_listed_anchor_does_not_hide_a_mix_of_the_generated_inputs(tmp_path):
    """`netlist_timing` is a listed file, but `netlist` and `sdf` are generated by two flows."""
    message = refusal(
        tmp_path,
        "vivado_postsynth_sim",
        f"{SIM}.netlist=vivado_alt_synth.netlist",
        sources=[("routed.v", "FpgaNetlist")],
    )
    assert "netlist <- vivado_alt_synth.netlist" in message and "sdf <- vivado_synth" in message
    assert f"{SIM}.sdf=vivado_alt_synth.sdf" in message
    assert f"{SIM}.netlist_timing=" not in message, "the user's own file stays as it is"


def test_sources_for_all_related_inputs_run_no_synthesis(tmp_path):
    listed = plan(
        tmp_path,
        "vivado_postsynth_sim",
        sources=[("routed.v", "FpgaNetlist"), ("delays.sdf", "Sdf")],
    )
    assert producers(listed) == {"vivado_postsynth_sim"}


def test_power_on_a_listed_activity_is_not_compared_with_the_checkpoint(tmp_path):
    listed = plan(tmp_path, "vivado_power", sources=[("activity.saif", "Saif")])
    assert producers(listed) == {"vivado_synth", "vivado_power"}


def test_an_activity_simulated_from_listed_files_is_not_compared_with_the_checkpoint(tmp_path):
    """No synthesis made the netlist that the simulation of the activity reads, so the
    checkpoint of `vivado_synth` is not compared with one."""
    listed = plan(
        tmp_path,
        "vivado_power",
        sources=[("routed.v", "FpgaNetlist"), ("delays.sdf", "Sdf")],
    )
    assert producers(listed) == {"vivado_synth", "vivado_postsynth_sim", "vivado_power"}


# ------------------------------------------------------------------------------ the oracles


def declaring_flows():
    return [
        (flow, declaration)
        for flow in all_flow_classes()
        if flow.__module__.startswith("xeda.")
        for declaration in declared_inputs(flow).values()
        if declaration.same_producer_as is not None
    ]


def test_the_relations_exist_in_the_flows_that_declare_them():
    found = {(flow.name, declaration.name) for flow, declaration in declaring_flows()}
    assert found == {
        ("vivado_postsynth_sim", "netlist"),
        ("vivado_postsynth_sim", "sdf"),
        ("vivado_power", "checkpoint"),
    }


def test_every_declared_relation_names_an_input_that_can_be_compared():
    for flow, declaration in declaring_flows():
        inputs = declared_inputs(flow)
        where = f"{flow.name}.{declaration.name}"
        anchor = declaration.same_producer_as
        assert anchor in inputs and anchor != declaration.name, where
        assert inputs[anchor].same_producer_as is None, f"{where}: the anchor declares one too"
        if declaration.via is not None:
            producer = registered_flow(inputs[anchor].producer or "")
            assert producer is not None, f"{where}: {anchor} names no default producer"
            assert declaration.via in declared_inputs(producer), where


def test_power_follows_the_activity_to_the_netlist_it_was_simulated_from():
    checkpoint = declared_inputs(VivadoPower)["checkpoint"]
    assert (checkpoint.same_producer_as, checkpoint.via) == ("activity", "netlist_timing")
    assert declared_inputs(VivadoPower)["activity"].same_producer_as is None
    assert declared_inputs(VivadoPostsynthSim)["netlist_timing"].same_producer_as is None
    for name in ("netlist", "sdf"):
        assert declared_inputs(VivadoPostsynthSim)[name].same_producer_as == "netlist_timing"


def test_list_flows_shows_the_relation():
    inputs = {item["name"]: item for item in flow_info("vivado_postsynth_sim")["inputs"]}
    assert inputs["sdf"]["same_producer_as"] == "netlist_timing" and inputs["sdf"]["via"] is None
    assert inputs["netlist_timing"]["same_producer_as"] is None
    power = {item["name"]: item for item in flow_info("vivado_power")["inputs"]}
    assert power["checkpoint"]["same_producer_as"] == "activity"
    assert power["checkpoint"]["via"] == "netlist_timing"
    documents = json.loads(CliRunner().invoke(cli, ["list-flows", "--json"]).stdout)
    listed = {flow["name"]: flow for flow in documents}["vivado_power"]["inputs"]
    assert {item["name"]: item["same_producer_as"] for item in listed} == {
        "activity": None,
        "checkpoint": "activity",
    }


def test_without_the_declaration_the_resolver_accepts_the_mix(tmp_path, monkeypatch):
    """The refusal comes from the declaration alone: with the relation removed from the flow's
    inputs, the same plan passes."""
    bad = [f"{SIM}.netlist_timing=vivado_alt_synth.netlist_timing"]
    refusal(tmp_path, "vivado_postsynth_sim", *bad)
    inputs = VivadoPostsynthSim.Inputs
    for name in ("netlist", "sdf"):
        info = inputs.model_fields[name]
        extra = dict(info.json_schema_extra["x-xeda-io"], same_producer_as=None)
        monkeypatch.setitem(info.json_schema_extra, "x-xeda-io", extra)
    other = tmp_path / "other"
    other.mkdir()
    mixed = plan(other, "vivado_postsynth_sim", *bad)
    assert producers(mixed) == {"vivado_alt_synth", "vivado_synth", "vivado_postsynth_sim"}


# ------------------------------------------------------------------------------ bad declarations


def declare(**kwargs):
    return In(SourceType.Data, description="x", **kwargs)


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"a": declare(same_producer_as="nothing")}, "names no input"),
        ({"a": declare(same_producer_as="a")}, "itself"),
        (
            {"a": declare(same_producer_as="b"), "b": declare(same_producer_as="a")},
            "declares one too",
        ),
        ({"a": declare(via="b"), "b": declare()}, "needs `same_producer_as`"),
        (
            {"a": declare(same_producer_as="b", via="c"), "b": declare()},
            "names a default producer",
        ),
    ],
)
def test_a_malformed_relation_is_refused_when_the_flow_is_defined(inputs, message):
    annotations = {name: Path | None for name in inputs}
    model = type("Inputs", (Flow.Inputs,), {**inputs, "__annotations__": annotations})
    with pytest.raises(TypeError, match=message):
        type(
            "_Related",
            (Flow,),
            {"__doc__": "Refused.", "run": lambda self: None, "Inputs": model},
        )


def test_the_documented_alternative_synthesis_power_report_plans(tmp_path):
    from .test_chain_documentation import _stage

    design = _stage(tmp_path, "alt_power.yaml") / "alt_power.yaml"
    planned = DefaultRunner(tmp_path / "run", display_results=False).plan("vivado_power", design)
    assert producers(planned) == {"vivado_alt_synth", "vivado_postsynth_sim", "vivado_power"}
