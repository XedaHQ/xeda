"""Explicit edges in the resolver: a chain adjacency or a saved `inputs` binding selects an
input's producer before the design's sources and before its default producer, in one immutable
plan that is also what runs."""

from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, In, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.bindings import NodeKey
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.resolver import ResolvedReference
from xeda.flow_runner.trace import as_recorded
from xeda.flows import Nextpnr
from xeda.introspect import plan_info

from .io_flows import (
    _ChainAmbiguousConsumer,
    _ChainConsumer,
    _Join,
    _Left,
    _Taker,
)

PART = "LFE5U-25F-6BG381C"
ICE40 = "iCE40HX1K-TQ144"
BLINK = "module blink(input clk, output q); assign q = clk; endmodule\n"


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def _design(tmp_path, sources=(), flows=None):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    entries = []
    for name, kind in sources:
        (root / name).write_text(BLINK if kind is SourceType.Verilog else "{}\n")
        entries.append({"file": name, "type": kind.name})
    return Design(
        name="d", design_root=root, rtl={"sources": entries, "top": "blink"}, flows=flows or {}
    )


def _runner(tmp_path, **kwargs):
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _graph(plan):
    """What executes: nodes in order with settings, identity, directory, enabled outputs and
    ordered input origins -- not the request's spelling or where a binding was written."""
    return [
        (
            node.name,
            node.flow_class,
            as_recorded(node.settings),
            node.flowrun_hash,
            node.run_path,
            node.switched_on,
            [(i.name, i.origin, i.references, i.sources) for i in node.inputs],
        )
        for node in plan.nodes
    ]


def _input(plan, node, name):
    return next(i for i in plan.node(node).inputs if i.name == name)


# ---------------------------------------------------------------------------------------------
# Selection: explicit binding > design source > default producer
# ---------------------------------------------------------------------------------------------


def test_an_unbound_input_takes_a_source_before_its_default_producer(tmp_path):
    plan = _runner(tmp_path).plan(_Taker, _design(tmp_path, [("x.dat", SourceType.Data)]))
    assert [node.name for node in plan.nodes] == ["__taker"]
    assert _input(plan, "__taker", "made").origin == "source"


def test_a_bound_alternate_producer_displaces_both_the_source_and_the_default(tmp_path):
    design = _design(
        tmp_path,
        [("x.dat", SourceType.Data)],
        {"__taker": {"inputs": {"made": "__input_maker.made"}}},
    )
    plan = _runner(tmp_path).plan(_Taker, design)
    assert [node.name for node in plan.nodes] == ["__input_maker", "__taker"]
    made = _input(plan, "__taker", "made")
    assert (made.origin, made.producer, made.output, made.sources) == (
        "producer",
        "__input_maker",
        "made",
        (),
    )
    assert made.references == (ResolvedReference("__input_maker", "made"),)
    assert made.binding_origin == "file" and "flows.__taker.inputs.made" in made.binding_location


def test_an_explicit_default_binding_selects_the_producer_over_a_source(tmp_path):
    design = _design(
        tmp_path, [("x.dat", SourceType.Data)], {"__taker": {"inputs": {"made": "__maker"}}}
    )
    plan = _runner(tmp_path).plan(_Taker, design)
    assert [node.name for node in plan.nodes] == ["__maker", "__taker"]


def test_a_chain_a_file_binding_and_an_api_binding_plan_the_same_graph(tmp_path):
    runner = _runner(tmp_path)
    chain = runner.plan(parse_request("__input_maker+__taker"), _design(tmp_path))
    saved = runner.plan(
        _Taker, _design(tmp_path, flows={"__taker": {"inputs": {"made": "__input_maker.made"}}})
    )
    project = tmp_path / "project.yaml"
    project.write_text(
        yaml.safe_dump({"flows": {"__taker": {"inputs": {"made": "InputMaker"}}}}).replace(
            "InputMaker", "__input_maker"
        )
    )
    from_project = runner.plan(_Taker, _design(tmp_path), xedaproject=str(project))
    api = runner.plan(
        _Taker, _design(tmp_path), flow_overrides={"inputs.made": "__input_maker.made"}
    )
    cli = runner.plan(_Taker, _design(tmp_path), flow_settings=["inputs.made=__input_maker"])
    assert _graph(chain) == _graph(saved) == _graph(from_project) == _graph(api) == _graph(cli)
    origins = [_input(p, "__taker", "made").binding_origin for p in (chain, saved, api, cli)]
    assert origins == ["chain", "file", "api", "cli"]
    assert chain.request.elements[0].node == "__input_maker" and len(saved.request.elements) == 1


def test_a_default_only_chain_plans_what_the_bare_request_plans(tmp_path):
    runner = _runner(tmp_path, hashed_run_dirs=True)
    bare = runner.plan(_Taker, _design(tmp_path))
    chain = runner.plan(parse_request("__maker+__taker"), _design(tmp_path))
    assert _graph(chain) == _graph(bare)
    assert _input(bare, "__taker", "made").binding_origin is None
    assert _input(chain, "__taker", "made").binding_origin == "chain"


def test_a_chain_overrides_a_saved_binding_and_the_plan_says_so(tmp_path):
    design = _design(tmp_path, flows={"__taker": {"inputs": {"made": "__maker.made"}}})
    plan = _runner(tmp_path).plan(parse_request("__input_maker+__taker"), design)
    made = _input(plan, "__taker", "made")
    assert made.producer == "__input_maker" and made.binding_origin == "chain"
    assert len(made.overridden) == 1 and "flows.__taker.inputs.made" in made.overridden[0]
    assert "(chain) overriding" in made.describe()
    document = plan_info(plan)["nodes"][-1]["inputs"][0]
    assert document["binding_origin"] == "chain" and document["overridden"] == list(made.overridden)
    assert document["references"] == [{"node": "__input_maker", "output": "made"}]


def test_a_chain_and_a_command_line_binding_of_one_input_are_an_error_even_when_equal(tmp_path):
    with pytest.raises(FlowSettingsException, match="chain position 1.*command line"):
        _runner(tmp_path).plan(
            parse_request("__input_maker+__taker"),
            _design(tmp_path),
            flow_settings=["inputs.made=__input_maker.made"],
        )


# ---------------------------------------------------------------------------------------------
# Judging an explicit reference
# ---------------------------------------------------------------------------------------------


def test_a_binding_of_the_wrong_kind_names_both_ends_and_what_the_producer_makes(tmp_path):
    design = _design(
        tmp_path, flows={"__taker": {"inputs": {"made": "__chain_simple_producer.netlist"}}}
    )
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).plan(_Taker, design)
    message = str(raised.value)
    for text in (
        "flows.__taker.inputs.made",
        "__taker.made takes Data",
        "__chain_simple_producer.netlist makes JsonNetlist",
        "data (Data)",
    ):
        assert text in message, message


def test_an_unqualified_binding_takes_the_one_output_that_fits(tmp_path):
    """`__chain_producer` makes two `Data` outputs; only the scalar one fits a scalar input."""
    design = _design(tmp_path, flows={"__taker": {"inputs": {"made": "__chain_producer"}}})
    assert _input(_runner(tmp_path).plan(_Taker, design), "__taker", "made").output == "data"


def test_an_ambiguous_binding_names_the_outputs_to_choose_from(tmp_path):
    design = _design(
        tmp_path,
        flows={"__chain_ambiguous_consumer": {"inputs": {"netlist": "__chain_producer"}}},
    )
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).plan(_ChainAmbiguousConsumer, design)
    assert "__chain_producer.json_a, __chain_producer.json_b; name one" in str(raised.value)


def test_a_many_output_cannot_be_bound_to_a_scalar_input(tmp_path):
    design = _design(tmp_path, flows={"__taker": {"inputs": {"made": "__chain_producer.files"}}})
    with pytest.raises(FlowSettingsException, match="takes one file.*produces many"):
        _runner(tmp_path).plan(_Taker, design)


def test_a_flow_without_outputs_cannot_be_bound_as_a_producer(tmp_path):
    design = _design(tmp_path, flows={"__taker": {"inputs": {"made": "__taker"}}})
    with pytest.raises(FlowSettingsException, match="__taker declares no outputs"):
        _runner(tmp_path).plan(_Taker, design)


# ---------------------------------------------------------------------------------------------
# One node per producer: demands unioned before hashing, order kept, cycles reported
# ---------------------------------------------------------------------------------------------


def test_two_inputs_bound_to_one_producer_enable_its_outputs_together(tmp_path):
    bindings = {
        "left": "__chain_producer.json_a",
        "right": "__chain_producer.json_b",
        "files": [
            "__chain_producer.files",
            "__chain_simple_producer.data",
            "__chain_producer.files",
        ],
    }
    design = _design(tmp_path, flows={"__chain_consumer": {"inputs": bindings}})
    plan = _runner(tmp_path).plan(_ChainConsumer, design)
    assert [node.name for node in plan.nodes] == [
        "__chain_producer",
        "__chain_simple_producer",
        "__chain_consumer",
    ]
    producer = plan.node("__chain_producer")
    assert producer.settings.first and producer.settings.second
    assert producer.switched_on == ("json_a", "json_b")
    # an ordered many binding keeps its reference order and its duplicates
    assert _input(plan, "__chain_consumer", "files").references == (
        ResolvedReference("__chain_producer", "files"),
        ResolvedReference("__chain_simple_producer", "data"),
        ResolvedReference("__chain_producer", "files"),
    )


def test_one_producer_reached_through_two_branches_is_one_node_one_identity_one_run(tmp_path):
    """`_Left` demands `_Fork.a`, `_Right` demands `_Fork.b`; both outputs are switched on
    before `_Fork` is hashed, and it runs once."""
    runner = _runner(tmp_path, hashed_run_dirs=True)
    design = _design(tmp_path)
    plan = runner.plan(_Join, design)
    assert [node.name for node in plan.nodes] == ["__fork", "__left", "__right", "__join"]
    assert [node.node_key for node in plan.nodes] == [NodeKey(node.name) for node in plan.nodes]
    fork = plan.node(NodeKey("__fork"))
    assert fork.switched_on == ("a", "b") and fork.settings.first and fork.settings.second
    flow = runner.run(_Join, design)
    assert flow.succeeded and flow.results["read"] == "a\nb\n"
    launched = [f.name for f in runner.launched]
    assert launched.count("__fork") == 1, launched
    (ran,) = [f for f in runner.launched if f.name == "__fork"]
    assert ran.flow_hash == fork.flowrun_hash and ran.run_path == fork.run_path
    assert [(f.name, f.run_path) for f in runner.launched] == [
        (node.name, node.run_path) for node in plan.nodes
    ]


def test_a_binding_cycle_is_reported_in_order(tmp_path):
    flows = {
        "__left": {"inputs": {"json": "__relay.netlist"}},
        "__relay": {"inputs": {"data": "__left.out"}},
    }
    with pytest.raises(FlowSettingsException, match="cycle: __left -> __relay -> __left"):
        _runner(tmp_path).plan(_Left, _design(tmp_path, flows=flows))


def test_an_empty_binding_of_an_optional_list_is_an_explicit_nothing(tmp_path):
    design = _design(tmp_path, [("x.dat", SourceType.Data)], {"__join": {"inputs": {"more": []}}})
    bare = _runner(tmp_path).plan(_Join, _design(tmp_path, [("x.dat", SourceType.Data)]))
    assert _input(bare, "__join", "left").origin == "source"
    more = _input(_runner(tmp_path).plan(_Join, design), "__join", "more")
    assert (more.origin, more.references, more.sources) == ("none", (), ())
    assert more.binding_origin == "file"


# ---------------------------------------------------------------------------------------------
# Settings of the nodes an explicit edge reaches
# ---------------------------------------------------------------------------------------------


def test_command_line_settings_reach_an_alternate_producer(tmp_path):
    plan = _runner(tmp_path).plan(
        _Taker,
        _design(tmp_path),
        flow_settings=["inputs.made=__input_maker", "flows.__input_maker.text=given"],
    )
    assert plan.node("__input_maker").settings.text == "given"


def test_command_line_settings_for_a_flow_outside_the_graph_name_its_nodes(tmp_path):
    with pytest.raises(FlowSettingsError) as raised:
        _runner(tmp_path).plan(
            _Taker,
            _design(tmp_path),
            flow_settings=["inputs.made=__input_maker", "flows.__fork.label=x"],
        )
    message = str(raised.value)
    assert "`-s flows.__fork.*` names no flow of this run" in message
    assert "__input_maker" in message and "__taker" in message


def test_a_displaced_default_producer_is_still_addressable_and_unused(tmp_path):
    plan = _runner(tmp_path).plan(
        _Taker,
        _design(tmp_path),
        flow_settings=["inputs.made=__input_maker", "flows.__maker.text=unused"],
    )
    assert "__maker" not in plan


def test_file_sections_for_unrelated_flows_stay_tolerated(tmp_path):
    design = _design(
        tmp_path,
        flows={
            "unavailable_plugin": {"inputs": {"x": {"not": "a reference"}}},
            "__chain_consumer": {"inputs": {"no_such_input": "__maker"}},
        },
    )
    assert [n.name for n in _runner(tmp_path).plan(_Taker, design).nodes] == ["__maker", "__taker"]


# ---------------------------------------------------------------------------------------------
# The FPGA flows of this base: family-selected types, source pruning, shared leaves
# ---------------------------------------------------------------------------------------------

NETLIST_AND_RTL = [("blink.v", SourceType.Verilog), ("net.json", SourceType.JsonNetlist)]


def test_a_netlist_source_skips_yosys_unless_a_binding_or_chain_selects_it(tmp_path):
    runner = _runner(tmp_path)
    settings = [f"fpga={PART}"]
    bare = runner.plan(Nextpnr, _design(tmp_path, NETLIST_AND_RTL), flow_settings=settings)
    assert [node.name for node in bare.nodes] == ["nextpnr"]
    saved = runner.plan(
        Nextpnr,
        _design(tmp_path, NETLIST_AND_RTL, {"nextpnr": {"inputs": {"netlist": "yosys_fpga"}}}),
        flow_settings=settings,
    )
    chain = runner.plan(
        parse_request("yosys-fpga+nextpnr"),
        _design(tmp_path, NETLIST_AND_RTL),
        flow_settings=settings,
    )
    for plan in (saved, chain):
        assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr"]
        netlist = _input(plan, "nextpnr", "netlist")
        assert netlist.references == (ResolvedReference("yosys_fpga", "netlist"),)
        assert netlist.sources == ()
        assert plan.node("yosys_fpga").settings.fpga.part == PART
    assert _graph(saved) == _graph(chain)


def test_an_alternate_synthesis_agrees_its_device_through_the_new_edge(tmp_path):
    """`__synth` replaces `yosys_fpga`: its `fpga` reaches `nextpnr` along the bound edge,
    and the displaced default's conflicting device takes no part in the agreement."""
    flows = {
        "nextpnr": {"inputs": {"netlist": "__synth.netlist"}},
        "__synth": {"fpga": {"part": PART}},
        "yosys_fpga": {"fpga": {"part": ICE40}},
    }
    plan = _runner(tmp_path).plan(
        Nextpnr, _design(tmp_path, [("blink.v", SourceType.Verilog)], flows)
    )
    assert [node.name for node in plan.nodes] == ["__synth", "nextpnr"]
    assert plan.node("nextpnr").settings.fpga.part == PART
    conflicting = dict(flows, nextpnr={**flows["nextpnr"], "fpga": {"part": ICE40}})
    with pytest.raises(FlowSettingsError) as raised:
        _runner(tmp_path).plan(
            Nextpnr, _design(tmp_path, [("blink.v", SourceType.Verilog)], conflicting)
        )
    assert "__synth" in str(raised.value) and "nextpnr" in str(raised.value)


class _EcpTaker(Flow):
    """Reads an ECP5 configuration only: `nextpnr.config` may be another family's."""

    results_description = {}

    class Inputs(Flow.Inputs):
        config: Path = In(SourceType.EcpConfig, description="An ECP5 text configuration.")

    def run(self):
        pass


def test_a_binding_and_a_chain_judge_an_edge_by_the_same_predicate(tmp_path):
    """What an output can make must be a subset of what the input takes. A chain and
    a saved binding of the same edge are accepted or refused alike."""
    from xeda.flow_runner.chains import fitting_outputs

    with pytest.raises(FlowSettingsException, match="no compatible output"):
        parse_request("nextpnr+__ecp_taker")
    design = _design(
        tmp_path,
        [("blink.v", SourceType.Verilog)],
        {"__ecp_taker": {"inputs": {"config": "nextpnr.config"}}, "nextpnr": {"fpga": PART}},
    )
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).plan(_EcpTaker, design)
    message = str(raised.value)
    assert "__ecp_taker.config takes EcpConfig" in message
    assert "nextpnr.config makes EcpConfig/IceAsc/Fasm" in message
    from xeda.flow.io import declared_inputs

    assert fitting_outputs(Nextpnr, declared_inputs(_EcpTaker)["config"]) == ([], [])


def test_a_chain_over_an_invalid_saved_binding_succeeds(tmp_path):
    """A saved binding the chain replaces is not validated, as one a command-line binding
    replaces is not."""
    for bad in (False, "no such flow", "__maker.no_such_output", ["__maker"]):
        design = _design(tmp_path, flows={"__taker": {"inputs": {"made": bad}}})
        plan = _runner(tmp_path).plan(parse_request("__input_maker+__taker"), design)
        made = _input(plan, "__taker", "made")
        assert made.producer == "__input_maker" and len(made.overridden) == 1
        replaced = _runner(tmp_path).plan(
            _Taker, design, flow_settings=["inputs.made=__input_maker"]
        )
        assert _input(replaced, "__taker", "made").producer == "__input_maker"
        with pytest.raises(FlowSettingsException):
            _runner(tmp_path).plan(_Taker, design)


def test_a_producer_s_settings_do_not_depend_on_who_asks(tmp_path):
    """No consumer gives its producer defaults. `yosys_fpga` requested alone and as
    `nextpnr`'s producer is one configuration, one identity, one directory."""
    from xeda.flows import YosysFpga

    assert not hasattr(Flow, "producer_defaults")
    assert YosysFpga.Settings().netlist_src_attrs is True
    design = _design(
        tmp_path, [("blink.v", SourceType.Verilog)], {"yosys_fpga": {"fpga": {"part": PART}}}
    )
    runner = _runner(tmp_path, hashed_run_dirs=True)
    alone = runner.plan(YosysFpga, design).node("yosys_fpga")
    produced = runner.plan(Nextpnr, design).node("yosys_fpga")
    assert as_recorded(alone.settings) == as_recorded(produced.settings)
    assert (alone.flowrun_hash, alone.run_path) == (produced.flowrun_hash, produced.run_path)


# ---------------------------------------------------------------------------------------------
# One immutable plan: what a dry run prints is what runs; remote and DSE refuse
# ---------------------------------------------------------------------------------------------


def test_the_plan_keeps_its_request_and_bindings_immutably(tmp_path):
    flows = {"__taker": {"inputs": {"made": "__input_maker.made"}}}
    plan = _runner(tmp_path).plan(_Taker, _design(tmp_path, flows=flows))
    before = _graph(plan)
    flows["__taker"]["inputs"]["made"] = "__maker.made"
    (entry,) = [e for layer in plan.bindings for e in layer.entries]
    assert entry.value == "__input_maker.made"
    for target, name, value in (
        (plan, "nodes", ()),
        (plan.nodes[-1], "inputs", ()),
        (plan.nodes[-1].inputs[0], "producer", "__maker"),
        (plan.nodes[-1].inputs[0].references[0], "node", "__maker"),
        (entry, "value", "__maker.made"),
    ):
        with pytest.raises(AttributeError):
            setattr(target, name, value)
    assert _graph(plan) == before


def test_a_dry_run_of_a_saved_binding_shows_the_graph_that_then_runs(tmp_path, monkeypatch):
    import json

    from click.testing import CliRunner

    from xeda.cli import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.txt").write_text("bound\n")
    design = tmp_path / "design.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "bound_demo",
                "rtl": {"sources": []},
                "flows": {
                    "__taker": {"inputs": {"made": "__input_maker.made"}},
                    "__input_maker": {"input_file": "$DESIGN_ROOT/in.txt"},
                },
            }
        )
    )
    dry = CliRunner().invoke(cli, ["run", "__taker", str(design), "--dry-run", "--json"])
    assert dry.exit_code == 0, dry.output
    planned = json.loads(dry.stdout)["plan"]["nodes"]
    assert [node["name"] for node in planned] == ["__input_maker", "__taker"]
    assert planned[1]["inputs"][0]["binding_origin"] == "file"
    assert not (tmp_path / "xeda_run").exists()
    text = CliRunner().invoke(cli, ["run", "__taker", str(design), "--dry-run"])
    assert "made <- __input_maker.made (saved binding)" in text.output
    ran = CliRunner().invoke(cli, ["run", "__taker", str(design), "--json"])
    assert ran.exit_code == 0, ran.output
    document = json.loads(ran.stdout)
    assert document["success"] and document["results"]["read"] == "bound\n"
    assert [(n["flow"], n["run_path"]) for n in document["nodes"]] == [
        (n["flow"], n["run_path"]) for n in planned
    ]


def test_dse_refuses_a_chain_or_a_reached_binding_before_any_worker(tmp_path):
    from xeda.flow_runner.dse import Dse

    from .test_dse_run import _DeclaredOptimizer

    bound = _design(tmp_path, flows={"__taker": {"inputs": {"made": "__input_maker.made"}}})
    for flow, design in (
        (_Taker, bound),
        (parse_request("__maker+__taker"), _design(tmp_path)),
    ):
        runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
        with pytest.raises(FlowSettingsException, match="local `xeda run` requests"):
            runner.run(flow, design)
    assert not (tmp_path / "run").exists()
