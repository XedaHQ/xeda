"""The four OpenROAD flows chain by type: a placed database, a clock tree, a global route.

`openroad_place` reads a gate-level netlist. Each later flow reads the database and the SDC file
that the flow before it wrote. Both come from one producer, or both from the design's sources
(a restart pair), and a plan never mixes the two.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner

from .bluespec_sources import COUNTER_BSV
from .launch_utils import empty_project
from .openroad_utils import (
    OPENROAD_FLOWS,
    openroad_design,
    openroad_settings,
    register_openroad_flows,
)
from .test_openroad_final import (
    GDS_FILE,
    KLAYOUT_TECH,
    KLAYOUT_TECH_TEXT,
    RCX_RULES,
    SUPPLY,
    fake_klayout,
    variant_residue,
)
from .tiny_platform import TINY_RESIDUE, tiny_store
from .tool_utils import (
    fake_returns,
    require_bsc,
    require_tclsh,
    use_fake_asic_tools,
    use_fake_openroad,
)

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
        "sdf": ((SourceType.Sdf,), None),
        "abstract_lef": ((SourceType.Data,), None),
        "cdl": ((SourceType.Cdl,), None),
        "timing_model": ((SourceType.Liberty,), "write_timing_model"),
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


# ============================================================================== the whole chain
#
# The four flows run one after the other under the fake OpenROAD.

RECORDED = Path(__file__).parent / "resources" / "openroad" / "metrics"
CLOCK = {"flows.openroad_place.clocks.clk.period": "5 ns"}
SLACK = "finish__timing__setup__ws"
HOLD = "finish__timing__hold__ws"
DRC = "detailedroute__route__drc_errors"
ANTENNA = "detailedroute__antenna__violating__nets"
NAMES = ("openroad_place", "openroad_cts", "openroad_grt", "openroad")


@pytest.fixture
def chain(monkeypatch):
    """The fake OpenROAD on the path."""
    require_tclsh()
    use_fake_openroad(monkeypatch)


def chain_settings(
    tmp_path: Path, *given: str, rcx: bool = False, layout: bool = False, **named
) -> list[str]:
    """The settings of a launch on the `tiny` platform. With `rcx`, the platform has the rules of
    OpenRCX and the voltages of its supply nets, and with `layout` it has the GDSII file and the
    KLayout technology file."""
    store = tmp_path / "store"
    if not store.exists():
        tiny_store(tmp_path)
    if rcx or layout:
        supply = SUPPLY if rcx else None
        residue = variant_residue(tmp_path, rules=rcx, supply=supply, layout=layout)
        if rcx:
            (store / "tiny" / RCX_RULES).write_text("rules\n")
        if layout:
            (store / "tiny" / GDS_FILE).write_bytes(b"gds\n")
            (store / "tiny" / KLAYOUT_TECH).write_text(KLAYOUT_TECH_TEXT)
        named = {"platform_file": str(residue), **named}
    return [*openroad_settings(store, **{**CLOCK, **named}), *given]


def metrics_dir(tmp_path: Path, **edits: dict) -> Path:
    """A directory with the five recorded metrics files, `edits` applied to the contents of the
    file of the driver they name (`openroad_final={key: value}`). A value `None` is a JSON null,
    and the marker `REMOVE` takes the key out."""
    target = tmp_path / "metrics"
    target.mkdir()
    for source in RECORDED.glob("*.json"):
        data = json.loads(source.read_text())
        for key, value in edits.get(source.stem, {}).items():
            if value is REMOVE:
                del data[key]
            else:
                data[key] = value
        (target / source.name).write_text(json.dumps(data))
    return target


REMOVE = object()


def launch(tmp_path: Path, monkeypatch, *settings: str, request: str = "openroad", **kwargs):
    """Launch `request` with the fake OpenROAD; return the runner, the design file, the settings
    and the flow that the launch returned (`None` when the launch failed)."""
    given = chain_settings(tmp_path, *settings, **kwargs)
    design = openroad_design(tmp_path / "design")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run(request, design, flow_settings=given)
    return runner, design, given, flow


def run_dir(tmp_path: Path, name: str = "openroad") -> Path:
    return tmp_path / "xeda_run" / "counter" / name


def xeda_run(tmp_path: Path, *settings: str) -> tuple[Any, dict]:
    """Run `xeda run openroad --json` and return the result and its document."""
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "openroad",
            str(openroad_design(tmp_path / "design")),
            "--run-root",
            str(tmp_path / "xeda_run"),
            "-s",
            *chain_settings(tmp_path, *settings),
            "--json",
        ],
    )
    return result, json.loads(result.stdout)


def states(document: dict) -> list[tuple[str, str]]:
    return [(node["flow"], node["state"]) for node in document["nodes"]]


# ------------------------------------------------------------------------------ the command line


@pytest.mark.parametrize("request_text", ["openroad", "yosys+openroad"])
def test_the_command_line_plans_the_five_nodes_of_the_whole_chain(tmp_path, request_text):
    """`xeda run openroad` and `xeda run yosys+openroad` plan synthesis and the four OpenROAD
    flows, each node on the declared edges, and run nothing."""
    store = tiny_store(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "run",
            request_text,
            str(openroad_design(tmp_path / "design", rtl=True)),
            "--run-root",
            str(tmp_path / "xeda_run"),
            "-s",
            *openroad_settings(store, **CLOCK),
            "--dry-run",
            "--json",
        ],
    )
    document = json.loads(result.stdout)
    assert result.exit_code == 0 and document["success"] is True, document.get("error")
    nodes = document["plan"]["nodes"]
    assert [node["flow"] for node in nodes] == ["yosys", *NAMES]
    assert [(node["name"], [item["name"] for item in node["inputs"]]) for node in nodes] == [
        ("yosys", []),
        ("openroad_place", ["netlist", "sdc"]),
        ("openroad_cts", ["odb", "sdc"]),
        ("openroad_grt", ["odb", "sdc"]),
        ("openroad", ["odb", "sdc"]),
    ]
    assert not (tmp_path / "xeda_run").exists()


# ------------------------------------------------------------------------------ the optional steps


def test_the_images_and_the_layout_are_written_only_when_asked_for(tmp_path, monkeypatch, chain):
    fake_klayout(tmp_path, monkeypatch)
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false", layout=True)
    assert flow is not None and flow.succeeded
    last = run_dir(tmp_path)
    for name in ("images", "env.sh", "openroad_images.tcl", "klayout.lyt", "klayout.args"):
        assert not (last / name).exists(), name
    assert "gds" not in flow.results["outputs"]


def test_the_images_are_saved_by_the_third_process_of_the_last_flow(tmp_path, monkeypatch, chain):
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false", "save_images=true")
    assert flow is not None and flow.succeeded
    last = run_dir(tmp_path)
    assert (last / "env.sh").read_text() == 'export QT_QPA_PLATFORM="offscreen"'
    assert sorted(path.name for path in (last / "images").iterdir()) == sorted(
        f"final_{view}.webp"
        for view in ("all", "routing", "placement", "clocks", "resizer", "congestion", "worst_path")
    )
    for earlier in ("openroad_place", "openroad_cts", "openroad_grt"):
        assert not (run_dir(tmp_path, earlier) / "env.sh").exists()


def test_a_build_without_the_gui_fails_the_run_and_leaves_no_trace(tmp_path, monkeypatch, chain):
    fake_returns(monkeypatch, {("ord::openroad_gui_compiled",): "0"})
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false", "save_images=true")
    failed_without_trace(tmp_path, flow)
    results = json.loads((run_dir(tmp_path) / "results.json").read_text())
    assert "this OpenROAD has no GUI; set save_images: false" in results["error"]["message"]
    assert (run_dir(tmp_path, "openroad_grt") / "trace.json").is_file()
    assert list((run_dir(tmp_path) / "images").glob("*.webp")) == []


def test_the_layout_is_an_output_when_write_gds_is_on(tmp_path, monkeypatch, chain):
    fake_klayout(tmp_path, monkeypatch)
    _, _, _, flow = launch(
        tmp_path, monkeypatch, "fail_timing=false", "write_gds=true", layout=True
    )
    assert flow is not None and flow.succeeded
    last = run_dir(tmp_path)
    assert flow.outputs.gds == last / "final.gds"
    assert (last / "final.gds").is_file()
    assert flow.results["outputs"]["gds"]["path"] == str(last / "final.gds")


def test_a_failed_merge_fails_the_run_with_no_trace_and_names_the_errors(
    tmp_path, monkeypatch, chain
):
    fake_klayout(tmp_path, monkeypatch)
    monkeypatch.setenv("KLAYOUT_FAKE_ERROR", "[ERROR] Found orphan cell 'X'")
    _, _, _, flow = launch(
        tmp_path, monkeypatch, "fail_timing=false", "write_gds=true", layout=True
    )
    failed_without_trace(tmp_path, flow)
    results = json.loads((run_dir(tmp_path) / "results.json").read_text())
    assert "[ERROR] Found orphan cell 'X'" in results["error"]["message"]


# ------------------------------------------------------------------------------ a failed stage


def test_a_failed_stage_reruns_alone(tmp_path, monkeypatch, chain):
    """A stage that fails in the middle of the chain leaves the database of that moment and no
    trace. The stages after it are blocked, and the stage before it stays fresh. The next launch,
    without the failure, runs the failed stage and the two after it again and not the first."""
    settings = ["flows.openroad.fail_timing=false"]
    with monkeypatch.context() as failing:
        failing.setenv("XEDA_FAKE_TOOL_FAIL", "clock_tree_synthesis")
        result, document = xeda_run(tmp_path, *settings)
    assert result.exit_code != 0 and document["success"] is False
    assert states(document) == [
        ("openroad_place", "ran"),
        ("openroad_cts", "failed"),
        ("openroad_grt", "blocked"),
        ("openroad", "blocked"),
    ]
    cts = run_dir(tmp_path, "openroad_cts")
    assert (cts / "openroad_cts-failed.odb").is_file()
    assert not (cts / "trace.json").exists() and not (cts / "cts.odb").exists()
    assert (run_dir(tmp_path, "openroad_place") / "trace.json").is_file()
    for blocked in ("openroad_grt", "openroad"):  # they never started
        assert not (run_dir(tmp_path, blocked) / "trace.json").exists()
        assert not (run_dir(tmp_path, blocked) / f"{blocked}.tcl").exists()

    result, document = xeda_run(tmp_path, *settings)
    assert result.exit_code == 0 and document["success"] is True, document.get("error")
    assert states(document) == [
        ("openroad_place", "fresh"),
        ("openroad_cts", "ran"),
        ("openroad_grt", "ran"),
        ("openroad", "ran"),
    ]
    assert not (cts / "openroad_cts-failed.odb").exists()
    assert (cts / "trace.json").is_file() and (cts / "cts.odb").is_file()
    assert (run_dir(tmp_path) / "final.odb").is_file()


@pytest.mark.parametrize("failing", ["detailed_route", "extract_parasitics"])
def test_a_failed_process_of_the_last_flow_leaves_its_database_and_no_trace(
    tmp_path, monkeypatch, chain, failing
):
    with monkeypatch.context() as patch:
        patch.setenv("XEDA_FAKE_TOOL_FAIL", failing)
        _, _, _, flow = launch(tmp_path, monkeypatch, rcx=True)
    assert flow is None or not flow.succeeded
    last = run_dir(tmp_path)
    assert (last / "openroad-failed.odb").is_file()
    assert not (last / "trace.json").exists()
    assert (run_dir(tmp_path, "openroad_grt") / "trace.json").is_file()


# ------------------------------------------------------------------------------ the results


def test_the_results_of_the_chain_are_the_recorded_metrics(tmp_path, monkeypatch, chain):
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false")
    assert flow is not None and flow.succeeded
    results = flow.results
    assert results["worst_slack"] == pytest.approx(-0.244411)
    assert results["wns"] == pytest.approx(-0.244411)
    assert results["tns"] == pytest.approx(-10.9686)
    assert results["whs"] == pytest.approx(0.0159159)
    assert results["fmax"] == pytest.approx(2022.61)
    assert results["area"] == pytest.approx(773.528)
    assert results["utilization"] == pytest.approx(0.887668)
    assert results["power"] == pytest.approx(0.00566387)
    assert results["drc_errors"] == 0 and results["antenna_violating_nets"] == 0
    assert results["instances"] == 576 and results["fill_cells"] == 186
    assert results["parasitics"] == "estimate" and results["platform"]["name"] == "tiny"
    persisted = json.loads((run_dir(tmp_path) / "results.json").read_text())
    assert persisted["metrics"]["worst_slack"] == results["worst_slack"]
    assert persisted["outcome"] == "passed"


def test_the_outputs_of_the_chain_are_the_files_of_the_last_flow(tmp_path, monkeypatch, chain):
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false", rcx=True)
    assert flow is not None and flow.succeeded
    last = run_dir(tmp_path)
    assert flow.outputs.odb == last / "final.odb"
    assert flow.outputs.sdc == last / "final.sdc"
    assert flow.outputs.def_file == last / "final.def"
    assert flow.outputs.netlist == last / "final.v"
    assert flow.outputs.spef == last / "final.spef"
    assert flow.inputs.odb == run_dir(tmp_path, "openroad_grt") / "grt.odb"
    for name in ("final.odb", "final.sdc", "final.def", "final.v", "final.spef", "route.odb"):
        assert (last / name).is_file(), name
    assert flow.results["parasitics"] == "rcx"


def test_the_file_of_the_extraction_does_not_stay_in_the_run_directory(
    tmp_path, monkeypatch, chain
):
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "counter")
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false", rcx=True)
    assert flow is not None and flow.succeeded
    last = run_dir(tmp_path)
    assert (last / "final.spef").is_file()
    assert not os.path.lexists(last / "counter.totCap")
    assert sorted(path.name for path in last.glob("*.totCap")) == []


def test_a_second_launch_is_fresh_and_a_dry_run_agrees(tmp_path, monkeypatch, chain):
    """The files that the flow opens are the same for a run and for the dry run, so the dry run
    after a run finds the flow up to date, with and without the rules of OpenRCX."""
    for rcx in (False, True):
        work = tmp_path / str(rcx)
        runner, design, settings, first = launch(work, monkeypatch, "fail_timing=false", rcx=rcx)
        assert first is not None and first.succeeded and not first.reused
        again = runner.run("openroad", design, flow_settings=settings)
        assert again is not None and again.succeeded and again.reused
        given = dict(text.split("=", 1) for text in settings)
        _, states = runner.plan_with_freshness(
            "openroad", Design.from_file(design), flow_settings=given
        )
        assert {name: state.state for name, state in states.items()} == dict.fromkeys(
            NAMES, "fresh"
        )


# ------------------------------------------------------------------------------ a rejection


def test_a_timing_miss_rejects_the_run_and_the_next_launch_reports_it_again(
    tmp_path, monkeypatch, chain
):
    runner, design, settings, first = launch(tmp_path, monkeypatch)
    assert first is not None and first.outcome == "rejected" and not first.succeeded
    assert "timing not met" in first.rejection and "-0.244411" in first.rejection
    last = run_dir(tmp_path)
    assert (last / "trace.json").is_file(), "a rejection keeps its trace"
    persisted = json.loads((last / "results.json").read_text())
    assert persisted["outcome"] == "rejected" and persisted["error"]["code"] == "rejected"
    written = (last / "openroad_final.log").stat().st_mtime_ns
    calls = (last / "fake_openroad.calls").read_text()

    again = runner.run("openroad", design, flow_settings=settings)
    assert again is not None and again.outcome == "rejected" and again.rejected_again
    assert first.rejection in again.rejection
    assert (last / "openroad_final.log").stat().st_mtime_ns == written
    assert (last / "fake_openroad.calls").read_text() == calls, "OpenROAD did not run again"


def test_the_rejection_is_reported_again_by_the_command_line(tmp_path, monkeypatch, chain):
    result, document = xeda_run(tmp_path)
    assert result.exit_code != 0 and document["error"]["type"] == "FlowFailed"
    assert states(document)[-1] == ("openroad", "failed")
    result, document = xeda_run(tmp_path)
    assert result.exit_code != 0 and document["error"]["type"] == "FlowFailed"
    assert states(document) == [
        ("openroad_place", "fresh"),
        ("openroad_cts", "fresh"),
        ("openroad_grt", "fresh"),
        ("openroad", "rejected again"),
    ]
    assert "timing not met" in document["nodes"][-1]["reason"]


def test_a_timing_miss_passes_when_the_check_is_off(tmp_path, monkeypatch, chain):
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false")
    assert flow is not None and flow.outcome == "passed" and flow.succeeded


def test_design_rule_violations_reject_the_run_unless_the_check_is_off(
    tmp_path, monkeypatch, chain
):
    monkeypatch.setenv(
        "XEDA_FAKE_OPENROAD_METRICS", str(metrics_dir(tmp_path, openroad_route={DRC: 4}))
    )
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false")
    assert flow is not None and flow.outcome == "rejected" and "design rule" in flow.rejection
    _, _, _, flow = launch(tmp_path / "off", monkeypatch, "fail_timing=false", "fail_drc=false")
    assert flow is not None and flow.outcome == "passed"
    assert flow.results["drc_errors"] == 4


def test_antenna_violations_reject_the_run_unless_the_check_is_off(tmp_path, monkeypatch, chain):
    monkeypatch.setenv(
        "XEDA_FAKE_OPENROAD_METRICS", str(metrics_dir(tmp_path, openroad_route={ANTENNA: 2}))
    )
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false")
    assert flow is not None and flow.outcome == "rejected" and "antenna" in flow.rejection
    _, _, _, flow = launch(tmp_path / "off", monkeypatch, "fail_timing=false", "fail_antenna=false")
    assert flow is not None and flow.outcome == "passed"


# ------------------------------------------------------------------------------ a parse failure


def failed_without_trace(tmp_path: Path, flow) -> None:
    assert flow is None or (not flow.succeeded and flow.outcome == "failed")
    assert not (run_dir(tmp_path) / "trace.json").exists()
    results = json.loads((run_dir(tmp_path) / "results.json").read_text())
    assert results["outcome"] == "failed"


@pytest.mark.parametrize(
    "key, file",
    [
        (DRC, "openroad_route"),
        (ANTENNA, "openroad_route"),
        (SLACK, "openroad_final"),
        (HOLD, "openroad_final"),
    ],
)
@pytest.mark.parametrize("checks", [True, False], ids=["checks on", "checks off"])
def test_a_metrics_file_without_a_verdict_key_fails_the_run_with_no_trace(
    tmp_path, monkeypatch, chain, key, file, checks
):
    monkeypatch.setenv(
        "XEDA_FAKE_OPENROAD_METRICS", str(metrics_dir(tmp_path, **{file: {key: REMOVE}}))
    )
    switches = [f"fail_{kind}={str(checks).lower()}" for kind in ("timing", "drc", "antenna")]
    _, _, _, flow = launch(tmp_path, monkeypatch, *switches)
    failed_without_trace(tmp_path, flow)


@pytest.mark.parametrize("checks", [True, False], ids=["checks on", "checks off"])
@pytest.mark.parametrize(
    "key, file, value",
    [
        *[
            (key, "openroad_final", value)
            for key in (SLACK, HOLD)
            for value in (None, "0", True, float("nan"), float("inf"), float("-inf"))
        ],
        *[(key, "openroad_route", value) for key in (DRC, ANTENNA) for value in (1.5, -1, True)],
    ],
    ids=lambda value: repr(value),
)
def test_an_invalid_verdict_value_fails_the_run_with_no_trace(
    tmp_path, monkeypatch, chain, key, file, value, checks
):
    monkeypatch.setenv(
        "XEDA_FAKE_OPENROAD_METRICS", str(metrics_dir(tmp_path, **{file: {key: value}}))
    )
    switches = [f"fail_{kind}={str(checks).lower()}" for kind in ("timing", "drc", "antenna")]
    _, _, _, flow = launch(tmp_path, monkeypatch, *switches)
    failed_without_trace(tmp_path, flow)


@pytest.mark.parametrize("which", ["openroad_route", "openroad_final"])
def test_a_missing_metrics_file_fails_the_run_with_no_trace(tmp_path, monkeypatch, chain, which):
    directory = metrics_dir(tmp_path)
    (directory / f"{which}.json").unlink()
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_METRICS", str(directory))
    _, _, _, flow = launch(tmp_path, monkeypatch, "fail_timing=false")
    failed_without_trace(tmp_path, flow)


# ------------------------------------------------------------------------------ a Bluespec design


@pytest.mark.real_tool
def test_a_comment_edit_of_a_bluespec_source_reruns_bsc_and_nothing_after_it(tmp_path, monkeypatch):
    """The chain `bsc+openroad` on a Bluespec design: a comment in the source runs `bsc` again,
    which writes the same Verilog, and every flow after it is reused. An edit that changes the
    Verilog runs `yosys` again. A dry run said the first before."""
    require_bsc()
    require_tclsh()
    monkeypatch.chdir(tmp_path)  # Xeda reads a project file in the current directory
    use_fake_openroad(monkeypatch)
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    monkeypatch.setenv("XEDA_FAKE_OPENROAD_TOP", "mkCounter")
    source = tmp_path / "design" / "Counter.bsv"
    source.parent.mkdir()
    source.write_text(COUNTER_BSV)
    (tmp_path / "design" / "design.yaml").write_text(
        "name: counter\nrtl:\n  sources: [Counter.bsv]\n  top: mkCounter\n  clock.port: CLK\n"
    )
    design = tmp_path / "design" / "design.yaml"
    given = dict(text.split("=", 1) for text in chain_settings(tmp_path, "fail_timing=false"))
    names = ["bsc", "yosys", *NAMES]
    project = empty_project(tmp_path)

    def launch_chain() -> dict[str, bool]:
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        flow = runner.run("openroad", design, project, flow_settings=given)
        assert flow is not None and flow.succeeded
        return {f.name: f.reused for f in runner.launched}

    def judged() -> dict[str, str]:
        runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
        _, states = runner.plan_with_freshness(
            "openroad", Design.from_file(design), project, flow_settings=given
        )
        return {name: state.state for name, state in states.items()}

    assert launch_chain() == dict.fromkeys(names, False)
    assert launch_chain() == dict.fromkeys(names, True)
    assert judged() == dict.fromkeys(names, "fresh")

    source.write_text(source.read_text().replace("// A counter.", "// A counter, edited."))
    states = judged()
    assert states["bsc"] == "stale" and set(states.values()) <= {"stale", "unknown"}
    assert launch_chain() == {"bsc": False, **dict.fromkeys(names[1:], True)}
    assert judged() == dict.fromkeys(names, "fresh")

    source.write_text(source.read_text().replace("count + 1", "count + 2"))
    ran = launch_chain()
    assert not ran["bsc"] and not ran["yosys"]
