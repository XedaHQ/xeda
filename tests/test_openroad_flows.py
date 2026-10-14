"""The four OpenROAD flows chain by type: a placed database, a clock tree, a global route.

`openroad_place` reads a gate-level netlist. Each later flow reads the database and the SDC file
that the flow before it wrote. Both come from one producer, or both from the design's sources
(a restart pair), and a plan never mixes the two.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner

from .openroad_utils import (
    OPENROAD_FLOWS,
    openroad_design,
    openroad_settings,
    register_openroad_flows,
)
from .tiny_platform import TINY_RESIDUE, tiny_store

CTS_PAIR = [("cts.odb", "OpenroadDbCts"), ("cts.sdc", "OpenroadSdcCts")]


@pytest.fixture(autouse=True)
def openroad_flows():
    with register_openroad_flows():
        yield


def plan(tmp_path: Path, flow: str, *settings: str, typed: dict | None = None, **design):
    """Plan `flow`. `settings` are `KEY=VALUE` texts, as the command line gives them. A leaf that
    is a number (`wire_rc`) cannot come as text, so `typed` gives such leaves by dotted key."""
    store = tiny_store(tmp_path)
    texts = [*openroad_settings(store), *settings]
    given = dict(text.split("=", 1) for text in texts) | (typed or {})
    return DefaultRunner(tmp_path / "run", display_results=False).plan(
        flow, openroad_design(tmp_path / "design", **design), flow_settings=given
    )


def refusal(tmp_path: Path, flow: str, *settings: str, **design) -> str:
    typed = design.pop("typed", None)
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, flow, *settings, typed=typed, **design)
    return " ".join(str(error.value).split())


def origins(planned) -> dict[str, dict[str, str]]:
    return {
        node.name: {item.name: item.describe() for item in node.inputs} for node in planned.nodes
    }


# ------------------------------------------------------------------------------ the declarations


def test_the_flows_declare_the_database_and_sdc_of_their_stage():
    stages = {
        "openroad_place": (None, "OpenroadDbPlaced", "OpenroadSdcPlaced"),
        "openroad_cts": ("Placed", "OpenroadDbCts", "OpenroadSdcCts"),
        "openroad_grt": ("Cts", "OpenroadDbGlobalRouted", "OpenroadSdcGlobalRouted"),
        "openroad": ("GlobalRouted", "OpenroadDbRouted", "OpenroadSdcRouted"),
    }
    for flow in OPENROAD_FLOWS:
        taken, made_db, made_sdc = stages[flow.name]
        outputs = declared_outputs(flow)
        assert outputs["odb"].types == (SourceType[made_db],)
        assert outputs["sdc"].types == (SourceType[made_sdc],)
        inputs = declared_inputs(flow)
        if taken is None:
            assert inputs["netlist"].types == (SourceType.VerilogNetlist,)
            assert (inputs["netlist"].producer, inputs["netlist"].output) == ("yosys", "netlist")
            assert inputs["sdc"].types == (SourceType.Sdc,) and inputs["sdc"].optional
            continue
        assert inputs["odb"].types == (SourceType[f"OpenroadDb{taken}"],)
        assert inputs["sdc"].types == (SourceType[f"OpenroadSdc{taken}"],)
        assert (inputs["sdc"].same_producer_as, inputs["sdc"].paired) == ("odb", True)
        assert not inputs["odb"].paired and inputs["odb"].same_producer_as is None


def test_the_last_flow_declares_its_files():
    outputs = declared_outputs(OPENROAD_FLOWS[-1])
    assert {
        name: (declaration.types, declaration.enabled_by) for name, declaration in outputs.items()
    } == {
        "odb": ((SourceType.OpenroadDbRouted,), None),
        "sdc": ((SourceType.OpenroadSdcRouted,), None),
        "def_file": ((SourceType.Def,), None),
        "netlist": ((SourceType.VerilogNetlist,), None),
        "spef": ((SourceType.Spef,), None),
        "gds": ((SourceType.Gds,), "write_gds"),
    }
    assert declared_outputs(OPENROAD_FLOWS[2])["guides"].types == (SourceType.Data,)


def test_every_declaration_has_a_description():
    for flow in OPENROAD_FLOWS:
        for declaration in (*declared_inputs(flow).values(), *declared_outputs(flow).values()):
            assert declaration.description.strip(), f"{flow.name}.{declaration.name}"


def test_the_flows_read_no_source_but_their_declared_inputs():
    for flow in OPENROAD_FLOWS:
        assert flow.reads_sources is None


def test_a_flow_that_is_not_complete_is_not_registered_by_importing_the_flows():
    """The skeletons stay unregistered until each flow can run, whatever imports `xeda.flows`.
    `openroad_place` runs, so it is registered; the other three are not."""
    code = (
        "import xeda.flows, xeda.flow as f; "
        "print(sorted(name for name in f.registered_flows if 'openroad' in name.lower()))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "['OpenroadPlace', 'openroad_place']"


# ------------------------------------------------------------------------------ the default edges


def test_the_default_wiring_plans_the_whole_chain_from_the_rtl(tmp_path):
    planned = plan(tmp_path, "openroad", rtl=True)
    assert [node.name for node in planned.nodes] == [
        "yosys",
        "openroad_place",
        "openroad_cts",
        "openroad_grt",
        "openroad",
    ]
    assert origins(planned) == {
        "yosys": {},
        "openroad_place": {"netlist": "netlist <- yosys.netlist", "sdc": "sdc <- none"},
        "openroad_cts": {"odb": "odb <- openroad_place.odb", "sdc": "sdc <- openroad_place.sdc"},
        "openroad_grt": {"odb": "odb <- openroad_cts.odb", "sdc": "sdc <- openroad_cts.sdc"},
        "openroad": {"odb": "odb <- openroad_grt.odb", "sdc": "sdc <- openroad_grt.sdc"},
    }


def test_a_typed_netlist_displaces_synthesis(tmp_path):
    planned = plan(tmp_path, "openroad")
    assert [node.name for node in planned.nodes] == [
        "openroad_place",
        "openroad_cts",
        "openroad_grt",
        "openroad",
    ]
    assert origins(planned)["openroad_place"]["netlist"].startswith("netlist <- source ")


def test_a_design_sdc_never_replaces_a_stage_sdc(tmp_path):
    """Clock tree synthesis changes the SDC (the clocks become propagated), so only the first
    flow takes the design's `.sdc`; every later flow takes the SDC that the flow before wrote."""
    planned = plan(tmp_path, "openroad", sources=[("constraints.sdc", None)])
    taken = origins(planned)
    assert taken["openroad_place"]["sdc"].startswith("sdc <- source ")
    assert taken["openroad_place"]["sdc"].endswith("constraints.sdc")
    assert taken["openroad_cts"]["sdc"] == "sdc <- openroad_place.sdc"
    assert taken["openroad_grt"]["sdc"] == "sdc <- openroad_cts.sdc"
    assert taken["openroad"]["sdc"] == "sdc <- openroad_grt.sdc"


# ------------------------------------------------------------------------------ a restart pair


@pytest.mark.parametrize(
    ("flow", "listed", "expected"),
    [
        ("openroad_grt", CTS_PAIR, ["openroad_grt"]),
        ("openroad", CTS_PAIR, ["openroad_grt", "openroad"]),
    ],
)
def test_a_typed_pair_of_files_starts_the_chain_at_its_stage(tmp_path, flow, listed, expected):
    planned = plan(tmp_path, flow, sources=listed)
    assert [node.name for node in planned.nodes] == expected
    taken = origins(planned)["openroad_grt"]
    assert taken["odb"].startswith("odb <- source ") and taken["odb"].endswith("cts.odb")
    assert taken["sdc"].startswith("sdc <- source ") and taken["sdc"].endswith("cts.sdc")


def test_a_pair_of_producers_passes(tmp_path):
    planned = plan(
        tmp_path,
        "openroad_grt",
        "flows.openroad_grt.inputs.odb=openroad_cts.odb",
        "flows.openroad_grt.inputs.sdc=openroad_cts.sdc",
    )
    assert "openroad_cts" in {node.name for node in planned.nodes}


def test_a_database_that_is_listed_does_not_take_the_sdc_of_a_producer(tmp_path):
    message = refusal(tmp_path, "openroad_grt", sources=CTS_PAIR[:1])
    assert "`odb` and `sdc` are written together" in message
    assert "odb <- source" in message and "cts.odb" in message
    assert "sdc <- openroad_cts.sdc" in message
    assert "-s flows.openroad_grt.inputs.odb=openroad_cts.odb" in message
    assert "list `sdc` as a typed source of the design too" in message


def test_an_sdc_that_is_listed_does_not_take_the_database_of_a_producer(tmp_path):
    message = refusal(tmp_path, "openroad_grt", sources=CTS_PAIR[1:])
    assert "`odb` and `sdc` are written together" in message
    assert "odb <- openroad_cts.odb" in message
    assert "sdc <- source" in message and "cts.sdc" in message
    assert "-s flows.openroad_grt.inputs.sdc=openroad_cts.sdc" in message


def test_a_binding_that_replaces_one_listed_file_is_refused(tmp_path):
    message = refusal(
        tmp_path,
        "openroad_grt",
        "flows.openroad_grt.inputs.odb=openroad_cts.odb",
        sources=CTS_PAIR,
    )
    assert "`odb` and `sdc` are written together" in message
    assert "odb <- openroad_cts.odb" in message and "sdc <- source" in message
    assert "remove the binding of `odb` and give it as a typed source" in message


def test_the_binding_the_refusal_suggests_makes_the_plan_pass(tmp_path):
    message = refusal(tmp_path, "openroad_grt", sources=CTS_PAIR[:1])
    advice = message.partition(" add `-s ")[2].partition("`")[0]
    assert advice.startswith("flows.openroad_grt.inputs.odb=")
    other = tmp_path / "other"
    other.mkdir()
    planned = plan(other, "openroad_grt", advice, sources=CTS_PAIR[:1])
    assert "openroad_cts" in {node.name for node in planned.nodes}


def test_a_binding_of_the_sdc_of_the_wrong_stage_is_refused(tmp_path):
    """The stage types keep the SDC of one stage from standing in for the SDC of another."""
    with pytest.raises(FlowSettingsException) as error:
        plan(tmp_path, "openroad_cts", "flows.openroad_cts.inputs.sdc=openroad_cts.sdc")
    message = str(error.value)
    assert "takes OpenroadSdcPlaced" in message and "makes OpenroadSdcCts" in message


# ------------------------------------------------------------------------------ shared leaves


def stage_nodes(planned):
    """The OpenROAD nodes of a plan, by name."""
    return {node.name: node for node in planned.nodes if node.name != "yosys"}


def test_the_hierarchy_given_to_one_flow_reaches_the_others(tmp_path):
    planned = plan(tmp_path, "openroad", "flows.openroad_cts.hierarchical=true", rtl=True)
    nodes = stage_nodes(planned)
    assert list(nodes) == ["openroad_place", "openroad_cts", "openroad_grt", "openroad"]
    assert {name: node.settings.hierarchical for name, node in nodes.items()} == dict.fromkeys(
        nodes, True
    )


def test_flows_that_disagree_on_the_hierarchy_are_refused_naming_both(tmp_path):
    message = refusal(
        tmp_path,
        "openroad",
        "flows.openroad_place.hierarchical=true",
        "flows.openroad_cts.hierarchical=false",
        rtl=True,
    )
    assert "hierarchical" in message
    assert "openroad_place" in message and "openroad_cts" in message


def test_leaves_of_the_wire_rc_given_to_different_flows_combine(tmp_path):
    planned = plan(
        tmp_path,
        "openroad",
        "flows.openroad_place.wire_rc.signal_layer=m3",
        "flows.openroad_grt.wire_rc.clock_layer=m4",
        typed={"flows.openroad.wire_rc.vias.v1": 0.5},
        rtl=True,
    )
    for name, node in stage_nodes(planned).items():
        rc = node.settings.wire_rc
        assert (rc.signal_layer, rc.clock_layer, rc.vias) == ("m3", "m4", {"v1": 0.5}), name


def test_flows_that_disagree_on_a_leaf_of_the_wire_rc_are_refused_naming_both(tmp_path):
    message = refusal(
        tmp_path,
        "openroad",
        typed={
            "flows.openroad_place.wire_rc.vias.v1": 0.5,
            "flows.openroad_grt.wire_rc.vias.v1": 0.6,
        },
        rtl=True,
    )
    assert "wire_rc.vias.v1" in message and "0.5" in message and "0.6" in message
    assert "openroad_place" in message and "openroad_grt" in message


def test_a_leaf_of_the_wire_rc_of_the_wrong_type_is_refused_naming_the_leaf(tmp_path):
    message = refusal(
        tmp_path,
        "openroad",
        "flows.openroad_place.wire_rc.vias.v1=abc",
        "flows.openroad_cts.wire_rc.signal_layer=m2",
        rtl=True,
    )
    assert "wire_rc -> vias -> v1" in message


def test_a_hierarchy_that_is_no_boolean_is_refused(tmp_path):
    message = refusal(
        tmp_path,
        "openroad_cts",
        "flows.openroad_cts.hierarchical=maybe",
    )
    assert "hierarchical" in message


@pytest.mark.parametrize(
    "name, value",
    [("hierarchical", True), ("wire_rc.signal_layer", "m3"), ("wire_rc.vias.v1", 0.5)],
)
def test_the_identity_of_every_node_changes_with_a_shared_leaf(tmp_path, name, value):
    base = plan(tmp_path / "a", "openroad", rtl=True)
    changed = plan(
        tmp_path / "b", "openroad", typed={f"flows.openroad_place.{name}": value}, rtl=True
    )
    same = plan(tmp_path / "c", "openroad", rtl=True)
    hashes = {node.name: node.flowrun_hash for node in base.nodes}
    assert hashes == {node.name: node.flowrun_hash for node in same.nodes}
    for node in changed.nodes:
        if node.name == "yosys":
            assert node.flowrun_hash == hashes["yosys"]
        else:
            assert node.flowrun_hash != hashes[node.name], node.name


def test_a_platform_without_lef_files_is_refused_at_planning(tmp_path):
    residue = tmp_path / "residue.yaml"
    text = TINY_RESIDUE.read_text()
    residue.write_text(
        "\n".join(
            line
            for line in text.splitlines()
            if not line.lstrip().startswith(("tech_lef:", "cell_lef:"))
        )
    )
    message = refusal(tmp_path, "openroad_cts", f"platform_file={residue}")
    assert "no tech_lef and no cell_lef" in message and "openroad_cts" in message
