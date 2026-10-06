"""Chains and saved bindings over the open FPGA flows: `yosys_fpga`, `nextpnr`, `fpga_pack` and
the programming-only `openfpgaloader`. Planning first; then execution through the process fakes
of `tests/fake_tools`, with the fake programmer.

NOTHING HERE MAY REACH A REAL PROGRAMMER. Every launch goes through the `toolchain` fixture,
which puts the fake `openFPGALoader` first on PATH and checks that it is the fake dispatcher,
byte for byte; every programming test then reads the recorded call (`_programmed`), which
carries the file that ran, rather than trusting PATH order. `conftest.programmer_guard` stands
behind both."""

import json
import os
import shutil
from pathlib import Path
from typing import ClassVar, NamedTuple

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, Out, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.trace import as_recorded
from xeda.flows import FpgaPack, Nextpnr, Openfpgaloader
from xeda.introspect import plan_info
from xeda.utils import replacing_file

from . import tool_utils
from .test_openfpgaloader import assert_fake_loader

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


class _AscWriter(Flow):
    """Makes an iCE40 configuration and really writes it, whatever device the packer targets."""

    results_description: ClassVar[dict[str, str]] = {}

    class Outputs(Flow.Outputs):
        config: Path = Out(SourceType.IceAsc, description="An iCE40 ASCII configuration.")

    def run(self):
        path = self.run_path / "config.asc"
        with replacing_file(self.run_directory.writable(path)) as stream:
            stream.write("configuration\n")
        self.outputs.config = path


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


# ================================================================================ execution
#
# The whole chain, launched through `xeda run --json` against the process fakes: the document's
# nodes and what each fake recorded (`fake_fpga.calls.jsonl` in each run directory).

RESOURCES = Path(__file__).parent / "resources" / "chains"
DISPATCHER = tool_utils.FAKE_TOOLS_DIR / "fake_fpga_tool.py"
BITSTREAM = b"\x00\xffXEDA bitstream\x00"
CHAIN = "yosys_fpga+nextpnr+fpga_pack+openfpgaloader"
STAGES = ["yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"]
ICE40_PART = "iCE40HX1K-TQ144"


class Board(NamedTuple):
    """One board's design file and what its family's stages are."""

    file: str
    design: str
    part: str
    config_type: str
    config: str
    placer: str
    packer: str
    bitstream: str
    programmer: str  # `--board`


BOARDS = {
    "ulx3s": Board(
        "ulx3s.yaml",
        "chain_ulx3s",
        "LFE5U-85F-6BG381C",
        "EcpConfig",
        "config.txt",
        "nextpnr-ecp5",
        "ecppack",
        "outputs/chain_ulx3s.bit",
        "ulx3s",
    ),
    "arty": Board(
        "arty.yaml",
        "chain_arty",
        "xc7a100tcsg324-1",
        "Fasm",
        "config.fasm",
        "nextpnr-himbaechel",
        "fpga-as",
        "outputs/chain_arty.bit",
        "arty_a7_100t",
    ),
}
BY_BOARD = pytest.mark.parametrize("board", BOARDS.values(), ids=list(BOARDS))


@pytest.fixture
def toolchain(tmp_path, monkeypatch) -> Path:
    """The fake FPGA toolchain first on PATH, its `openFPGALoader` checked to be the fake."""
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    assert assert_fake_loader() == prefix / "bin/openFPGALoader"
    return prefix


def _stage(tmp_path: Path, board: Board) -> Path:
    """A copy of the board's design file and sources: nothing runs inside the checkout."""
    shutil.copytree(RESOURCES, tmp_path / "design", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path / "design" / board.file


def _write_design(tmp_path: Path, sources=("top.v",), flows=None, name="top") -> Path:
    """A design of one Verilog module and the prebuilt files the sources may name."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    (root / "given.bit").write_bytes(b"a bitstream built elsewhere")
    (root / "given.json").write_text('{"modules": {"top": {"ports": {}, "cells": {}}}}\n')
    document = {"name": name, "rtl": {"sources": list(sources), "top": "top"}, "flows": flows or {}}
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def _xeda(*args) -> tuple:
    """`xeda <args> --json`: the result and its one document."""
    result = CliRunner().invoke(cli, [str(arg) for arg in (*args, "--json")])
    return result, json.loads(result.stdout)


def _states(document: dict) -> dict[str, str]:
    return {node["node"]: node["state"] for node in document["nodes"]}


def _calls(tmp_path: Path, design: str, flow: str) -> list[dict]:
    record = tmp_path / "xeda_run" / design / flow / "fake_fpga.calls.jsonl"
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def _every_call(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "xeda_run").rglob("fake_fpga.calls.jsonl"))


def _settings(tmp_path: Path, design: str, flow: str) -> dict:
    path = tmp_path / "xeda_run" / design / flow / "settings.json"
    return json.loads(path.read_text())["effective_flow_settings"]


def _results(tmp_path: Path, design: str, flow: str) -> dict:
    return json.loads((tmp_path / "xeda_run" / design / flow / "results.json").read_text())


def _programmed(tmp_path: Path, toolchain: Path, design: str, bitstream: Path) -> dict:
    """The one call the fake programmer recorded, checked to be the fake and to have read
    `bitstream`: the recorded executable is the toolchain's copy of the dispatcher, byte for
    byte, and not the suite's sentinel nor any `openFPGALoader` of the machine."""
    calls = _calls(tmp_path, design, "openfpgaloader")
    assert calls, "the programmer was not started"
    return _the_fake_loader(toolchain, calls[-1], tmp_path / "xeda_run" / design, bitstream)


def _the_fake_loader(toolchain: Path, call: dict, base: Path, bitstream: Path) -> dict:
    loader = toolchain / "bin/openFPGALoader"
    assert call["tool"] == "openFPGALoader"
    assert Path(call["executable"]) == loader.resolve()
    assert Path(call["executable"]).read_bytes() == DISPATCHER.read_bytes()
    assert Path(call["cwd"]).resolve() == (base / "openfpgaloader").resolve()
    assert call["argv"][:2] == ["--bitstream", str(bitstream)]
    assert call["inputs"] == [str(bitstream)]
    assert call["input_bytes"] == [len(BITSTREAM)]
    return call


# ------------------------------------------------------------------ the chain, per board family


@BY_BOARD
def test_a_dry_run_of_the_whole_chain_selects_the_family_s_stages_and_runs_nothing(
    tmp_path, toolchain, board
):
    result, planned = _xeda("run", CHAIN, _stage(tmp_path, board), "--dry-run")
    assert result.exit_code == 0, result.output
    assert [e["flow"] for e in planned["request"]] == STAGES
    nodes = {node["name"]: node for node in planned["plan"]["nodes"]}
    assert list(nodes) == STAGES
    # the family selects the configuration both stages speak: textcfg for ECP5, FASM for Xilinx
    assert nodes["nextpnr"]["output_types"]["config"] == [board.config_type]
    assert nodes["fpga_pack"]["input_types"]["config"] == [board.config_type]
    assert nodes["nextpnr"]["input_types"]["netlist"] == ["JsonNetlist"]
    assert nodes["openfpgaloader"]["input_types"]["bitstream"] == ["Bitstream"]
    # nextpnr always writes its configuration, so no output is switched on by a demand
    assert all(node["switched_on"] == [] for node in nodes.values())
    assert {name: node["action_reason"] for name, node in nodes.items()} == {
        **dict.fromkeys(STAGES[:3]),
        "openfpgaloader": "it programs a device",
    }
    bound = {
        (node["name"], i["name"]): (i["producer"], i["output"], i["binding_origin"])
        for node in planned["plan"]["nodes"]
        for i in node["inputs"]
        if i["producer"]
    }
    assert bound == {
        ("nextpnr", "netlist"): ("yosys_fpga", "netlist", "chain"),
        ("fpga_pack", "config"): ("nextpnr", "config", "chain"),
        ("openfpgaloader", "bitstream"): ("fpga_pack", "bitstream", "chain"),
    }
    assert not (tmp_path / "xeda_run").exists() and not list(tmp_path.rglob("fake_fpga.calls*"))


@BY_BOARD
def test_the_whole_chain_builds_and_programs_the_board_through_the_fakes(
    tmp_path, toolchain, board
):
    result, document = _xeda("run", CHAIN, _stage(tmp_path, board))
    assert result.exit_code == 0, result.output
    assert document["success"] and document["flow"] == "openfpgaloader"
    assert [e["flow"] for e in document["request"]] == STAGES
    # all four nodes are in the document, in plan order, each in its own sibling directory
    nodes = document["nodes"]
    assert [(n["node"], n["state"]) for n in nodes] == [(name, "ran") for name in STAGES]
    root = tmp_path / "xeda_run" / board.design
    assert [Path(n["run_path"]) for n in nodes] == [root / name for name in STAGES]
    assert nodes[3]["reason"] == "it programs a device"
    # and the last node's results are the document's
    assert document["results"]["flow"] == "openfpgaloader" and document["results"]["success"]
    assert [tool["executable"] for tool in document["results"]["tools"]] == ["openFPGALoader"]
    # the stages each ran the family's tool, on the file the stage before it made
    (yosys,) = _calls(tmp_path, board.design, "yosys_fpga")
    (placer,) = _calls(tmp_path, board.design, "nextpnr")
    (packer,) = _calls(tmp_path, board.design, "fpga_pack")
    netlist, config = root / "yosys_fpga/netlist.json", root / "nextpnr" / board.config
    assert yosys["tool"] == "yosys"
    assert (placer["tool"], packer["tool"]) == (board.placer, board.packer)
    assert str(netlist) in placer["inputs"] and str(config) in packer["inputs"]
    assert config.is_file() and (root / "fpga_pack" / board.bitstream).read_bytes() == BITSTREAM
    # the declared outputs are checked records: path and digest, the config switched on
    assert _results(tmp_path, board.design, "nextpnr")["outputs"]["config"]["path"] == str(config)
    assert len(_results(tmp_path, board.design, "nextpnr")["outputs"]["config"]["sha"]) == 32
    outputs = _results(tmp_path, board.design, "fpga_pack")["outputs"]["bitstream"]
    assert outputs["path"] == str(root / "fpga_pack" / board.bitstream)
    # the programmer is the fake, by the file that ran, and read the packed bitstream only
    bitstream = root / "fpga_pack" / board.bitstream
    call = _programmed(tmp_path, toolchain, board.design, bitstream)
    assert call["argv"][2:] == ["--board", board.programmer, "--fpga-part", board.part]


def _chipdb_generator(tmp_path: Path) -> dict:
    """The one call that generated the chip database nextpnr was handed, under the run root."""
    (record,) = (tmp_path / "xeda_run/.cache/xilinx-chipdb").glob("*/fake_fpga.calls.jsonl")
    return next(
        call
        for call in map(json.loads, record.read_text().splitlines())
        if call["tool"] == "xilinx_gen.py"
    )


@BY_BOARD
def test_the_stages_share_one_device_and_the_family_s_database(tmp_path, toolchain, board):
    result, _ = _xeda("run", CHAIN, _stage(tmp_path, board))
    assert result.exit_code == 0, result.output
    parts = {name: _settings(tmp_path, board.design, name)["fpga"]["part"] for name in STAGES}
    assert set(parts.values()) == {board.part}, parts
    (placer,) = _calls(tmp_path, board.design, "nextpnr")
    (packer,) = _calls(tmp_path, board.design, "fpga_pack")
    if board.packer == "fpga-as":
        # nextpnr's chip database and the assembler's bitstream come from one X-Ray database
        database = toolchain / "share/nextpnr/prjxray-db/artix7"
        generator = _chipdb_generator(tmp_path)
        assert Path(generator["argv"][generator["argv"].index("--xray") + 1]) == database
        assert f"--prjxray_db_path={database}" in packer["argv"]
        assert f"--part={board.part}" in packer["argv"]
        chipdb = Path(placer["argv"][placer["argv"].index("--chipdb") + 1])
        assert chipdb.is_relative_to(tmp_path / "xeda_run/.cache/xilinx-chipdb")
        assert not any(arg.startswith("--textcfg") for arg in placer["argv"])
    else:
        assert not any(arg.startswith("--prjxray_db_path") for arg in packer["argv"])
        assert "--textcfg=config.txt" in placer["argv"]
        assert not any("fasm" in arg for arg in placer["argv"])


@pytest.mark.parametrize("owner", ["fpga_pack", "nextpnr"])
def test_a_database_given_at_one_stage_is_the_chip_database_s_and_the_assembler_s(
    tmp_path, toolchain, owner
):
    """The override reaches both stages along the chained edge: the chip database is generated
    from it and the assembler reads it, wherever in the chain it was written."""
    board = BOARDS["arty"]
    design = _stage(tmp_path, board)
    other = tmp_path / "design" / "db"
    shutil.copytree(toolchain / "share/nextpnr/prjxray-db", other)
    result, document = _xeda("run", CHAIN, design, "-s", f"flows.{owner}.prjxray_db={other}")
    assert result.exit_code == 0, result.output
    (packer,) = _calls(tmp_path, board.design, "fpga_pack")
    generator = _chipdb_generator(tmp_path)
    assert packer["argv"][0] == f"--prjxray_db_path={other / 'artix7'}"
    assert generator["argv"][generator["argv"].index("--xray") + 1] == str(other / "artix7")
    assert _states(document) == dict.fromkeys(STAGES, "ran")


# -------------------------------------------------- one executable graph, however it is spelled


@BY_BOARD
def test_every_shorter_head_and_the_bare_action_are_one_executable_graph(
    tmp_path, toolchain, board
):
    """By identity, not by comparing plans: after the full chain ran, each shorter spelling finds
    every build stage fresh in the very same directories -- only the programmer runs again."""
    design = _stage(tmp_path, board)
    first_result, first = _xeda("run", CHAIN, design)
    assert first_result.exit_code == 0 and _states(first) == dict.fromkeys(STAGES, "ran")
    built = [(n["node"], n["run_path"]) for n in first["nodes"]]
    spellings = ["nextpnr+fpga_pack+openfpgaloader", "fpga_pack+openfpgaloader", "openfpgaloader"]
    for runs, spelling in enumerate(spellings, start=2):
        result, document = _xeda("run", spelling, design)
        assert result.exit_code == 0, (spelling, result.output)
        assert [(n["node"], n["run_path"]) for n in document["nodes"]] == built, spelling
        assert list(_states(document).values()) == ["fresh"] * 3 + ["ran"], spelling
        assert len(_calls(tmp_path, board.design, "openfpgaloader")) == runs
        for stage in STAGES[:3]:
            assert len(_calls(tmp_path, board.design, stage)) == 1, (spelling, stage)
    # and as plans: one executable graph -- nodes, directories, identities, switched-on outputs
    # and each input's producer and output -- though the input's binding origin (a chain's
    # adjacency, or the default producer) and the request itself are spelled differently
    graphs = [
        [
            (
                node["name"],
                node["run_path"],
                node["flowrun_hash"],
                node["settings_hash"],
                node["switched_on"],
                [(i["name"], i["producer"], i["output"]) for i in node["inputs"]],
            )
            for node in _xeda("run", spelling, design, "--dry-run")[1]["plan"]["nodes"]
        ]
        for spelling in (CHAIN, *spellings)
    ]
    assert all(graph == graphs[0] for graph in graphs) and len(graphs[0]) == 4


@pytest.mark.parametrize("late_check", [False, True], ids=["same moment", "after the racy window"])
@BY_BOARD
def test_a_build_only_chain_run_twice_reuses_every_stage(
    tmp_path, toolchain, board, late_check, monkeypatch
):
    design = _stage(tmp_path, board)
    build = "yosys_fpga+nextpnr+fpga_pack"
    result, first = _xeda("run", build, design)
    assert result.exit_code == 0 and first["flow"] == "fpga_pack"
    assert _states(first) == dict.fromkeys(STAGES[:3], "ran")
    bitstream = tmp_path / "xeda_run" / board.design / "fpga_pack" / board.bitstream
    assert bitstream.read_bytes() == BITSTREAM
    run_dirs = [tmp_path / "xeda_run" / board.design / stage for stage in STAGES[:3]]
    state = tool_utils.run_outputs_state(*run_dirs)
    traces = {path: path.stat().st_mtime_ns for path in (d / "trace.json" for d in run_dirs)}
    if late_check:
        tool_utils.check_after_the_racy_window(monkeypatch)
    result, again = _xeda("run", build, design)
    assert result.exit_code == 0 and _states(again) == dict.fromkeys(STAGES[:3], "fresh")
    assert again["results"]["success"] and again["flow"] == "fpga_pack"
    for stage in STAGES[:3]:
        assert len(_calls(tmp_path, board.design, stage)) == 1, f"{stage} ran again"
    assert tool_utils.run_outputs_state(*run_dirs) == state
    if late_check:  # the scenario the check makes: the traces were refreshed, no output was
        assert {path: path.stat().st_mtime_ns for path in traces} != traces
    # a build that programs nothing started no programmer, however it was reached
    assert not (tmp_path / "xeda_run" / board.design / "openfpgaloader").exists()
    assert all(record.parent.name != "openfpgaloader" for record in _every_call(tmp_path))


@BY_BOARD
def test_a_programming_chain_run_twice_programs_again_while_the_build_stays_fresh(
    tmp_path, toolchain, board
):
    design = _stage(tmp_path, board)
    bitstream = tmp_path / "xeda_run" / board.design / "fpga_pack" / board.bitstream
    first_result, first = _xeda("run", CHAIN, design)
    assert first_result.exit_code == 0 and _states(first)["openfpgaloader"] == "ran"
    _programmed(tmp_path, toolchain, board.design, bitstream)
    result, again = _xeda("run", CHAIN, design)
    assert result.exit_code == 0 and again["success"]
    assert list(_states(again).values()) == ["fresh", "fresh", "fresh", "ran"]
    assert again["nodes"][3]["reason"] == "it programs a device"
    # exactly one more call of the programmer, and it is the fake again
    calls = _calls(tmp_path, board.design, "openfpgaloader")
    assert len(calls) == 2
    for call in calls:
        _the_fake_loader(toolchain, call, tmp_path / "xeda_run" / board.design, bitstream)
    for stage in STAGES[:3]:
        assert len(_calls(tmp_path, board.design, stage)) == 1, f"{stage} ran again"
    # the programmer keeps no trace: nothing vouches for "programmed"
    assert not (tmp_path / "xeda_run" / board.design / "openfpgaloader/trace.json").exists()


# ------------------------------------------------- nothing unbuilt or mismatched is programmed
#
# Each refusal below is paired with a control that differs from it in the one thing under test
# and DOES reach the fake programmer: a refusal proves something only if the same fixture,
# made right, would have programmed.


def _assert_never_programmed(tmp_path: Path, design: str) -> None:
    assert not (tmp_path / "xeda_run" / design / "openfpgaloader/fake_fpga.calls.jsonl").exists()
    assert not [r for r in _every_call(tmp_path) if r.parent.name == "openfpgaloader"]


def test_a_configuration_of_the_wrong_family_never_reaches_the_programmer(tmp_path, toolchain):
    chain = "__asc_writer+fpga_pack+openfpgaloader"
    design = _write_design(tmp_path)
    # the control: an iCE40 configuration for an iCE40 device is packed, and programmed
    result, document = _xeda("run", chain, design, "-s", f"fpga={ICE40_PART}")
    assert result.exit_code == 0, result.output
    assert _states(document) == {
        "__asc_writer": "ran",
        "fpga_pack": "ran",
        "openfpgaloader": "ran",
    }
    (packer,) = _calls(tmp_path, "top", "fpga_pack")
    assert packer["tool"] == "icepack"
    bitstream = tmp_path / "xeda_run/top/fpga_pack/outputs/top.bin"
    assert _programmed(tmp_path, toolchain, "top", bitstream)["argv"][-2:] == [
        "--fpga-part",
        ICE40_PART,
    ]
    # the same chain for an ECP5 device: refused in planning, before any tool starts
    shutil.rmtree(tmp_path / "xeda_run")
    result, refused = _xeda("run", chain, design, "-s", f"fpga={ECP5}")
    assert result.exit_code == 1 and refused["success"] is False
    assert refused["error"]["type"] == "FlowSettingsException"
    assert "takes EcpConfig, makes IceAsc" in refused["error"]["message"]
    assert refused["nodes"] == []
    assert not (tmp_path / "xeda_run").exists() and not _every_call(tmp_path)


def test_an_upstream_output_that_was_never_written_never_reaches_the_programmer(
    tmp_path, toolchain
):
    """`__asc_maker` declares a configuration and writes none: its output record is refused, the
    packer is not entered and the programmer never starts."""
    design = _write_design(tmp_path)
    result, document = _xeda(
        "run", "__asc_maker+fpga_pack+openfpgaloader", design, "-s", f"fpga={ICE40_PART}"
    )
    assert result.exit_code == 1 and document["success"] is False
    # the failure is the producer's and reaches each consumer as a failed dependency
    assert _states(document) == dict.fromkeys(
        ["__asc_maker", "fpga_pack", "openfpgaloader"], "failed"
    )
    maker = _results(tmp_path, "top", "__asc_maker")
    assert maker["success"] is False and maker["error"]["type"] == "MissingOutput"
    assert "did not produce its output `config`" in maker["error"]["message"]
    for consumer in ("fpga_pack", "openfpgaloader"):
        assert _results(tmp_path, "top", consumer)["error"]["type"] == "FlowDependencyFailure"
    assert not _every_call(tmp_path), "a tool started"
    _assert_never_programmed(tmp_path, "top")


@BY_BOARD
@pytest.mark.parametrize("mode", ["no-output", "partial", "fail"])
def test_a_packer_that_makes_no_whole_bitstream_never_reaches_the_programmer(
    tmp_path, toolchain, monkeypatch, board, mode
):
    design = _stage(tmp_path, board)
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", board.packer)
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", mode)
    result, document = _xeda("run", CHAIN, design)
    assert result.exit_code == 1 and document["success"] is False
    # the refusal is the packer's: everything before it ran, and the packer itself was started
    states = _states(document)
    assert [states[name] for name in STAGES[:3]] == ["ran", "ran", "failed"]
    assert states["openfpgaloader"] in ("failed", "not run")
    assert [call["tool"] for call in _calls(tmp_path, board.design, "fpga_pack")] == [board.packer]
    pack = _results(tmp_path, board.design, "fpga_pack")
    assert pack["success"] is False and "bitstream" not in pack.get("outputs", {})
    if mode == "no-output":
        assert "wrote no bitstream" in pack["error"]["message"]
    published = tmp_path / "xeda_run" / board.design / "fpga_pack" / board.bitstream
    assert not published.exists()
    loader = _results(tmp_path, board.design, "openfpgaloader")
    assert loader["success"] is False and loader["error"]["type"] == "FlowDependencyFailure"
    _assert_never_programmed(tmp_path, board.design)
    # the control: the same root, the same chain, the packer well -- programs, with the fake
    monkeypatch.delenv("XEDA_FAKE_FPGA_TOOL")
    monkeypatch.delenv("XEDA_FAKE_FPGA_MODE")
    result, good = _xeda("run", CHAIN, design)
    assert result.exit_code == 0, result.output
    # the control's own states: the two stages the failing run completed are reused as they
    # are, the packer that failed runs again and the programmer, never entered before, runs
    assert _states(good) == dict(zip(STAGES, ["fresh", "fresh", "ran", "ran"]))
    assert published.is_file() and published.read_bytes() == BITSTREAM
    _programmed(tmp_path, toolchain, board.design, published)


@BY_BOARD
@pytest.mark.parametrize("failing", ["yosys", "placer"])
@pytest.mark.parametrize("mode", ["fail", "no-output"])
def test_an_upstream_failure_stops_the_chain_before_the_programmer(
    tmp_path, toolchain, monkeypatch, board, failing, mode
):
    tool = "yosys" if failing == "yosys" else board.placer
    stage = "yosys_fpga" if failing == "yosys" else "nextpnr"
    design = _stage(tmp_path, board)
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", tool)
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", mode)
    result, document = _xeda("run", CHAIN, design)
    assert result.exit_code == 1 and document["success"] is False
    states = _states(document)
    downstream = STAGES[STAGES.index(stage) + 1 :]
    assert states[stage] == "failed"
    assert all(states.get(name, "not run") in ("failed", "not run") for name in downstream)
    # the failing tool was reached, and nothing after it was ever started
    assert [call["tool"] for call in _calls(tmp_path, board.design, stage)] == [tool]
    for name in downstream:
        assert _calls(tmp_path, board.design, name) == [], name
    failed = _results(tmp_path, board.design, stage)
    assert failed["success"] is False
    # the stage's own failure is named in its document: a tool's exit status, or the output a
    # tool that exited with zero did not write -- and then the consumers quote it
    expected = {
        ("yosys_fpga", "fail"): "NonZeroExitCode",
        ("yosys_fpga", "no-output"): "ReportedFailure",
        ("nextpnr", "fail"): "NonZeroExitCode",
        ("nextpnr", "no-output"): "FlowFatalError",
    }[stage, mode]
    assert failed["error"]["type"] == expected and failed["error"]["message"]
    quoted = {
        ("yosys_fpga", "fail"): "exited with code 1",
        ("yosys_fpga", "no-output"): "reported failure",
        ("nextpnr", "fail"): "exited with code 1",
        ("nextpnr", "no-output"): "did not write enabled",
    }
    for name in downstream:
        error = _results(tmp_path, board.design, name)["error"]
        assert error["type"] == "FlowDependencyFailure", (name, error)
        assert f"dependency {stage} failed" in error["message"], (name, error)
        assert quoted[stage, mode] in error["message"], (name, error)
    _assert_never_programmed(tmp_path, board.design)


@BY_BOARD
@pytest.mark.parametrize("mode", ["no-output", "partial"])
def test_an_earlier_bitstream_is_never_programmed_in_place_of_a_failed_rebuild(
    tmp_path, toolchain, monkeypatch, board, mode
):
    design = _stage(tmp_path, board)
    published = tmp_path / "xeda_run" / board.design / "fpga_pack" / board.bitstream
    assert _xeda("run", CHAIN, design)[0].exit_code == 0
    assert published.read_bytes() == BITSTREAM
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", board.packer)
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", mode)
    result, document = _xeda("run", CHAIN, design, "--rebuild-all")
    assert result.exit_code == 1 and document["success"] is False
    assert _states(document)["fpga_pack"] == "failed"
    assert len(_calls(tmp_path, board.design, "fpga_pack")) == 2  # the rebuild did start
    assert len(_calls(tmp_path, board.design, "openfpgaloader")) == 1  # the first launch's only


# ------------------------------------------------ typed prebuilt sources and explicit bindings

BITSTREAM_SOURCE = ("top.v", {"file": "given.bit", "type": "Bitstream"})
NETLIST_SOURCE = ("top.v", {"file": "given.json", "type": "JsonNetlist"})


def test_a_prebuilt_bitstream_is_programmed_alone_until_a_chain_names_the_packer(
    tmp_path, toolchain
):
    design = _write_design(tmp_path, BITSTREAM_SOURCE)
    given = tmp_path / "design/given.bit"
    result, document = _xeda("run", "openfpgaloader", design, "-s", f"fpga={ECP5}")
    assert result.exit_code == 0, result.output
    assert _states(document) == {"openfpgaloader": "ran"}  # no producer is run for it
    (call,) = _calls(tmp_path, "top", "openfpgaloader")
    assert call["argv"][:2] == ["--bitstream", str(given)]
    assert call["input_bytes"] == [len(given.read_bytes())]
    assert Path(call["executable"]) == (toolchain / "bin/openFPGALoader").resolve()
    assert not [r for r in _every_call(tmp_path) if r.parent.name != "openfpgaloader"]
    # the chain's own adjacency is an explicit binding: it selects the packer, whose bitstream
    # is what the programmer then reads, not the file the design lists
    result, document = _xeda("run", "fpga_pack+openfpgaloader", design, "-s", f"fpga={ECP5}")
    assert result.exit_code == 0, result.output
    assert list(_states(document).values()) == ["ran"] * 4
    packed = tmp_path / "xeda_run/top/fpga_pack/outputs/top.bit"
    assert packed.read_bytes() == BITSTREAM != given.read_bytes()
    _programmed(tmp_path, toolchain, "top", packed)
    assert len(_calls(tmp_path, "top", "openfpgaloader")) == 2


@pytest.mark.parametrize("route", ["chain", "design file"])
def test_a_prebuilt_netlist_bypasses_synthesis_until_its_producer_is_bound(
    tmp_path, toolchain, route
):
    """`nextpnr+fpga_pack` takes the design's own netlist; naming `yosys_fpga` -- as a chain
    position, or as the input's binding in the design file -- selects that producer instead."""
    flows = {"nextpnr": {"inputs": {"netlist": "yosys_fpga"}}} if route == "design file" else {}
    given = tmp_path / "design/given.json"
    design = _write_design(tmp_path, NETLIST_SOURCE, flows=flows)
    settings = ("-s", f"fpga={ECP5}")
    head = "yosys_fpga+nextpnr+fpga_pack" if route == "chain" else "nextpnr+fpga_pack"
    if route == "chain":  # the control: unbound, the typed source stands for the producer
        result, document = _xeda("run", "nextpnr+fpga_pack", design, *settings)
        assert result.exit_code == 0, result.output
        assert _states(document) == {"nextpnr": "ran", "fpga_pack": "ran"}
        assert not (tmp_path / "xeda_run/top/yosys_fpga").exists()
        (placer,) = _calls(tmp_path, "top", "nextpnr")
        assert str(given) in placer["inputs"]
        shutil.rmtree(tmp_path / "xeda_run")
    result, document = _xeda("run", head, design, *settings)
    assert result.exit_code == 0, result.output
    assert _states(document) == dict.fromkeys(STAGES[:3], "ran")
    (placer,) = _calls(tmp_path, "top", "nextpnr")
    netlist = tmp_path / "xeda_run/top/yosys_fpga/netlist.json"
    assert str(netlist) in placer["inputs"] and str(given) not in placer["inputs"]
    (nextpnr_node,) = [n for n in document["nodes"] if n["node"] == "nextpnr"]
    (bound,) = [i for i in nextpnr_node["inputs"] if i["name"] == "netlist"]
    assert (bound["producer"], bound["binding_origin"]) == (
        "yosys_fpga",
        "chain" if route == "chain" else "file",
    )


# ------------------------------------------------------------------------------ the P4 boundary
#
# Bluespec and most Vivado flows declare no inputs or outputs yet (P4 derives a design from `bsc`;
# P5 chains Bluespec to simulation and synthesis; `vivado_synth` and `vivado_alt_synth` declare
# their outputs). Until then a chain through one is refused, and a binding for one is refused for
# the missing declaration -- before any tool runs.

UNDECLARED = ["bsc", "bsc_sim", "vivado_project"]


def _nothing_started(tmp_path: Path) -> None:
    assert not (tmp_path / "xeda_run").exists(), "a run root was made"
    assert not [p for p in tmp_path.rglob("*") if p.name.startswith(("fake_", "vivado"))]


@pytest.mark.parametrize(
    "chain, flow",
    [
        ("bsc+yosys_fpga", "bsc"),
        ("bsc+vivado_synth", "bsc"),
        ("bsc_sim+openfpgaloader", "bsc_sim"),
        ("vivado_project+openfpgaloader", "vivado_project"),
        ("yosys_fpga+vivado_project", "vivado_project"),
        ("yosys_fpga+nextpnr+vivado_project", "vivado_project"),
    ],
)
def test_a_chain_through_an_undeclared_flow_is_refused_before_any_tool_runs(
    tmp_path, monkeypatch, chain, flow
):
    tool_utils.use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = _write_design(tmp_path)
    result, document = _xeda("run", chain, design)
    assert result.exit_code == 2 and document["success"] is False
    message = document["error"]["message"]
    assert f"Flow `{flow}` has no declared I/O and can only be run alone." in message
    assert not {"plan", "nodes", "request"} & set(document)
    _nothing_started(tmp_path)
    dry, refused = _xeda("run", chain, design, "--dry-run")
    assert dry.exit_code == 2 and refused["error"]["message"] == message
    with pytest.raises(FlowSettingsException, match="can only be run alone"):
        parse_request(chain)


@pytest.mark.parametrize("flow", ["vivado_synth", "vivado_alt_synth"])
def test_a_vivado_synthesis_precedes_the_loader_and_has_its_bitstream_switched_on(
    tmp_path, monkeypatch, flow
):
    """The Vivado synthesis flows declare their outputs: `bitstream` is the loader's input, and
    the loader's demand names one (`enable_output`). Only planned: the loader programs hardware."""
    tool_utils.use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    design = _write_design(
        tmp_path, flows={flow: {"fpga": A100T, "clock": {"period": 5.0}}}, name="top"
    )
    result, document = _xeda(
        "run", f"{flow}+openfpgaloader", design, "--dry-run", "-s", f"fpga.part={A100T}"
    )
    assert result.exit_code == 0, result.output
    nodes = {node["name"]: node for node in document["plan"]["nodes"]}
    assert list(nodes) == [flow, "openfpgaloader"]
    assert nodes[flow]["switched_on"] == ["bitstream"]
    (bitstream,) = [i for i in nodes["openfpgaloader"]["inputs"] if i["name"] == "bitstream"]
    assert (bitstream["producer"], bitstream["output"]) == (flow, "bitstream")
    _nothing_started(tmp_path)


def _unbound_design(tmp_path: Path, flow: str, bound: bool) -> Path:
    """A design that plans `flow` (the Vivado flows need a device), `inputs.design` or not."""
    section = {"fpga": A100T} if flow.startswith("vivado") else {}
    if bound:
        section["inputs"] = {"design": "bsc"}
    return _write_design(tmp_path, flows={flow: section}, name="bound" if bound else "top")


@pytest.mark.parametrize("flow", UNDECLARED)
@pytest.mark.parametrize("origin", ["design file", "command line", "command line, qualified"])
def test_a_design_binding_for_an_undeclared_flow_is_refused_for_the_missing_declaration(
    tmp_path, monkeypatch, flow, origin
):
    """`inputs.design: bsc` is a binding in every origin -- not erased, not a setting -- and the
    flow has no declared input to bind it to."""
    tool_utils.use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    plain = _unbound_design(tmp_path, flow, bound=False)
    args = {
        "design file": (_unbound_design(tmp_path, flow, bound=True),),
        "command line": (plain, "-s", "inputs.design=bsc"),
        "command line, qualified": (plain, "-s", f"flows.{flow}.inputs.design=bsc"),
    }[origin]
    result, document = _xeda("run", flow, *args)
    assert result.exit_code == 1 and document["success"] is False
    error = document["error"]
    # a binding refusal, not a settings refusal (`extra fields not permitted`)
    assert error["type"] == "FlowSettingsException", error
    message = error["message"]
    assert f"flows.{flow}.inputs.design" in message
    assert f"`{flow}` declares no inputs" in message and "can only be run alone" in message
    assert "not permitted" not in message and "extra_forbidden" not in message
    assert document["nodes"] == []
    _nothing_started(tmp_path)
    # the control: without the binding the same request plans (and nothing is refused)
    result, planned = _xeda("run", flow, plain, "--dry-run")
    assert result.exit_code == 0, result.output
    assert [n["name"] for n in planned["plan"]["nodes"]] == [flow]


def test_an_unknown_setting_is_a_settings_error_and_a_binding_is_not(tmp_path, monkeypatch):
    """The two refusals differ in kind: `bogus` would be a setting; `inputs` is reserved."""
    tool_utils.use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    plain = _write_design(tmp_path)
    result, document = _xeda("run", "vivado_project", plain, "-s", "bogus=1")
    assert result.exit_code == 1 and document["error"]["type"] == "FlowSettingsError"
    result, document = _xeda("run", "vivado_project", plain, "-s", "inputs.design=bsc")
    assert result.exit_code == 1 and document["error"]["type"] == "FlowSettingsException"
    _nothing_started(tmp_path)


# ------------------------------------------------------------ the real tools, ending at the packer
#
# One chain per family, from copies of the same design files: yosys, the family's placer and
# its packer, for real. No programmer: neither chain names one, and a bitstream is not loaded.
# The Xilinx one is opt-in (`XEDA_TESTS_OPENXC7=1`, openXC7 first on PATH), as in
# `test_openxc7_real.py`, whose run root and chip-database cache it shares.


def _real_build(run_root: Path, design: Path, board: Board) -> None:
    runner = DefaultRunner(run_root, display_results=False)
    flow = runner.run("yosys_fpga+nextpnr+fpga_pack", design)
    assert flow is not None and flow.succeeded
    root = run_root / board.design
    assert [(f.name, f.run_path) for f in runner.launched] == [
        (name, root / name) for name in STAGES[:3]
    ]
    assert flow.inputs.config == root / "nextpnr" / board.config
    assert flow.inputs.config.stat().st_size > 0
    assert flow.outputs.bitstream == root / "fpga_pack" / board.bitstream
    assert flow.outputs.bitstream.stat().st_size > 1000
    assert not (root / "openfpgaloader").exists()
    again = DefaultRunner(run_root, display_results=False).run(
        "yosys_fpga+nextpnr+fpga_pack", design
    )
    assert again is not None and again.reused


def test_the_ulx3s_chain_ending_at_the_packer_builds_a_bitstream_with_the_real_tools(tmp_path):
    tool_utils.require_nextpnr_ecp5()
    tool_utils._require_command("ecppack", ["ecppack", "--help"])
    _real_build(tmp_path / "run", _stage(tmp_path, BOARDS["ulx3s"]), BOARDS["ulx3s"])


def test_the_arty_chain_ending_at_the_packer_builds_a_bitstream_with_openxc7(
    tmp_path, tmp_path_factory
):
    tool_utils.require_openxc7()
    base = tmp_path_factory.getbasetemp()
    common = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    _real_build(common / "openxc7-run-root", _stage(tmp_path, BOARDS["arty"]), BOARDS["arty"])
