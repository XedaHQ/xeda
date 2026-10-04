"""Chains and saved bindings over the open FPGA flows: `yosys_fpga`, `nextpnr`, `fpga_pack` and
the programming-only `openfpgaloader`. Planning here; execution is through the fake tools, and
the programmer is never a real one."""

from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.trace import as_recorded
from xeda.flows import FpgaPack, Nextpnr, Openfpgaloader
from xeda.introspect import plan_info

ECP5 = "LFE5U-25F-6BG381C"
ICE40 = "iCE40HX1K-TQ144"
A100T = "xc7a100tcsg324-1"
PINS = "set_property LOC E3 [get_ports clk]\n"


@pytest.fixture(autouse=True)
def isolate_registration():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


def _design(tmp_path: Path, part: str = ECP5, flows=None) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    sources: list = ["top.v"]
    if part == A100T:
        (root / "pins.xdc").write_text(PINS)
        sources.append("pins.xdc")
    return Design(
        name="top", design_root=root, rtl={"sources": sources, "top": "top"}, flow=flows or {}
    )


def _runner(tmp_path: Path, **kwargs) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _graph(plan):
    return [
        (
            node.name,
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


# ------------------------------------------------------------------- the default route, spelled


@pytest.mark.parametrize(
    "chain, requested",
    [
        ("yosys_fpga+nextpnr+fpga_pack+openfpgaloader", Openfpgaloader),
        ("nextpnr+fpga_pack+openfpgaloader", Openfpgaloader),
        ("fpga_pack+openfpgaloader", Openfpgaloader),
        ("yosys_fpga+nextpnr+fpga_pack", FpgaPack),
        ("nextpnr+fpga-pack", FpgaPack),
        ("YosysFpga+nextpnr", Nextpnr),
    ],
)
def test_the_default_route_spelled_as_a_chain_is_the_bare_plan(tmp_path, chain, requested):
    runner = _runner(tmp_path, hashed_run_dirs=True)
    design = _design(tmp_path, flows={"nextpnr": {"fpga": ECP5}})
    bare = runner.plan(requested, design)
    spelled = runner.plan(parse_request(chain), design)
    assert _graph(spelled) == _graph(bare)
    assert [e.node for e in spelled.request.elements] == [
        name for name in ("yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader") if name in spelled
    ][-len(spelled.request.elements) :]
    assert all(node.switched_on == () for node in spelled.nodes)


# -------------------------------------------------------------------- the programmer ends a chain


def test_the_programmer_is_an_action_that_can_only_end_a_chain():
    assert Openfpgaloader.action_reason == "it programs a device"
    for chain in ("openfpgaloader+nextpnr", "fpga_pack+openfpgaloader+nextpnr"):
        with pytest.raises(FlowSettingsException) as raised:
            parse_request(chain)
        assert "`openfpgaloader` programs a device and can only end a chain" in str(raised.value)


def test_a_chain_that_skips_the_packer_says_what_each_side_takes_and_makes():
    with pytest.raises(FlowSettingsException) as raised:
        parse_request("nextpnr+openfpgaloader")
    message = str(raised.value)
    assert "`openfpgaloader` takes bitstream (Bitstream)" in message
    assert "`nextpnr` makes config (EcpConfig/IceAsc/Fasm)" in message


# ------------------------------------------------------------- family selection along an edge


@pytest.mark.parametrize("part, config", [(ECP5, "EcpConfig"), (ICE40, "IceAsc"), (A100T, "Fasm")])
def test_a_chain_narrows_the_packer_s_input_to_the_family_nextpnr_selects(tmp_path, part, config):
    plan = _runner(tmp_path).plan(
        parse_request("nextpnr+fpga_pack"), _design(tmp_path, part), flow_settings={"fpga": part}
    )
    nodes = {node["name"]: node for node in plan_info(plan)["nodes"]}
    assert nodes["nextpnr"]["output_types"]["config"] == [config]
    assert nodes["fpga_pack"]["input_types"]["config"] == [config]
    bound = _input(plan, "fpga_pack", "config")
    assert (bound.producer, bound.output, bound.binding_origin) == ("nextpnr", "config", "chain")
    assert plan.node("yosys_fpga").settings.fpga.part == plan.node("nextpnr").settings.fpga.part


class _AscMaker(Flow):
    """Makes an iCE40 configuration, whatever device the packer targets."""

    results_description: ClassVar[dict[str, str]] = {}

    class Outputs(Flow.Outputs):
        config: Path = Out(SourceType.IceAsc, description="An iCE40 ASCII configuration.")

    def run(self):
        pass


def test_a_configuration_of_another_family_is_refused_for_the_packer(tmp_path):
    flows = {"fpga_pack": {"fpga": ECP5, "inputs": {"config": "__asc_maker.config"}}}
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).plan(FpgaPack, _design(tmp_path, flows=flows))
    message = str(raised.value)
    assert "fpga_pack.config: __asc_maker.config has no compatible output" in message
    assert "takes EcpConfig, makes IceAsc" in message
    ice = {"fpga_pack": {"fpga": ICE40, "inputs": {"config": "__asc_maker"}}}
    plan = _runner(tmp_path).plan(FpgaPack, _design(tmp_path, ICE40, flows=ice))
    assert [node.name for node in plan.nodes] == ["__asc_maker", "fpga_pack"]


# ------------------------------------------------------ shared leaves agree along explicit edges


@pytest.mark.parametrize("owner", ["fpga_pack", "nextpnr"])
def test_a_database_override_agrees_along_a_chained_edge(tmp_path, owner):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "db").mkdir()
    design = _design(tmp_path, A100T, flows={owner: {"prjxray_db": "db"}})
    plan = _runner(tmp_path).plan(
        parse_request("yosys_fpga+nextpnr+fpga_pack"), design, flow_settings={"fpga": A100T}
    )
    database = (tmp_path / "design" / "db").resolve()
    assert plan.node("fpga_pack").settings.prjxray_db == database
    assert plan.node("nextpnr").settings.prjxray_db == database
    flows = {"fpga_pack": {"prjxray_db": "db"}, "nextpnr": {"prjxray_db": "elsewhere"}}
    with pytest.raises(FlowSettingsError, match="prjxray_db.*disagrees"):
        _runner(tmp_path).plan(
            parse_request("nextpnr+fpga_pack"),
            _design(tmp_path, A100T, flows=flows),
            flow_settings={"fpga": A100T},
        )


@pytest.mark.parametrize("owner", ["openfpgaloader", "fpga_pack", "nextpnr", "yosys_fpga"])
def test_a_board_given_at_one_node_is_every_chained_node_s_device(tmp_path, owner):
    """The board, or the device, written once anywhere on the chain reaches every node that
    declares it, through the explicit edges."""
    setting = {"fpga": ECP5} if owner == "yosys_fpga" else {"board": "ULX3S_85F"}
    plan = _runner(tmp_path).plan(
        parse_request("yosys_fpga+nextpnr+fpga_pack+openfpgaloader"),
        _design(tmp_path, flows={owner: setting}),
    )
    parts = {node.name: node.settings.fpga.part for node in plan.nodes}
    assert len(set(parts.values())) == 1 and len(parts) == 4, parts
    if owner != "yosys_fpga":
        boards = {n.name: n.settings.board for n in plan.nodes if hasattr(n.settings, "board")}
        assert set(boards.values()) == {"ULX3S_85F"}, boards


def test_boards_that_differ_along_a_chain_are_an_error_naming_both_nodes(tmp_path):
    flows = {"fpga_pack": {"board": "ULX3S_85F"}, "nextpnr": {"board": "ULX3S_12F"}}
    with pytest.raises(FlowSettingsError) as raised:
        _runner(tmp_path).plan(parse_request("nextpnr+fpga_pack"), _design(tmp_path, flows=flows))
    message = str(raised.value)
    assert "fpga_pack" in message and "nextpnr" in message and "board" in message
