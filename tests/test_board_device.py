"""A board supplies the FPGA device: for every flow that takes a `board`, wherever the board is
written, and wherever that flow sits in the run's graph.

The resolver derives each board-aware node's `fpga` from its agreed board before the shared
`fpga` leaf is agreed along the run's edges and before the required settings are checked, so a
bundled board alone gives every node that shares `fpga` with it the board's part. A device
written for a flow that is not part of the run reaches no node: the error that a node lacks one
says where it is written and why it does not count, and planning reports the unused section
rather than a device "detected" for it.

Planning only: nothing here runs a tool, and no programmer is ever started.
"""

import logging
from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.board import get_board_data
from xeda.flow import FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.chains import ChainElement, parse_request, predecessors, request_text
from xeda.flow_runner.settings_layers import transitive_dependencies
from xeda.flows import Openfpgaloader
from xeda.introspect import boards_info

from .project_files import PROJECT_FILE
from .settings_samples import flow_classes

PRODUCT_FLOWS = [cls for cls, _name in flow_classes()]
#: every product flow that takes a board, from the registry: one that gains `board` is swept too
BOARD_FLOWS = [cls for cls in PRODUCT_FLOWS if "board" in cls.Settings.model_fields]
#: every bundled board whose entry names its part: a board added to the database is swept too
BUNDLED_BOARDS = [
    entry["board"]
    for entry in boards_info()
    if isinstance(entry.get("fpga"), str)
    or (isinstance(entry.get("fpga"), dict) and entry["fpga"].get("part"))
]
CUSTOM_PART = "LFE5U-25F-6BG381C"
#: where a board can be written for one node of a run
ORIGINS = (
    "design",
    "project",
    "target",
    "command line",
    "command line node",
    "API",
    "API settings",
    "custom",
)

VERILOG = "module blinky(input clk, output led); assign led = clk; endmodule\n"


def _part(board: str) -> str:
    data = get_board_data(board)
    assert data is not None
    fpga = data["fpga"]
    return fpga if isinstance(fpga, str) else fpga["part"]


def _write_design(root: Path, flows: dict, targets: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "blinky.v").write_text(VERILOG)
    (root / "blinky.xdc").write_text("# pins\n")
    data: dict = {"name": "blinky", "rtl": {"top": "blinky", "sources": ["blinky.v", "blinky.xdc"]}}
    if flows:
        data["flows"] = flows
    if targets:
        data["targets"] = targets
    path = root / "blinky.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _plan(tmp_path: Path, request: str, design: Path, **kwargs):
    """Plan `request` as `xeda run --dry-run` does: nothing is created, not even the run root."""
    run_root = tmp_path / "run"
    try:
        return DefaultRunner(run_root, display_results=False).plan(
            request, design, xedaproject=kwargs.pop("xedaproject", ""), **kwargs
        )
    finally:
        assert not run_root.exists(), "planning created the run root"


def _requests() -> list[tuple[str, list[str]]]:
    """Each board-aware flow alone (its default graph) and after each flow that can feed it,
    with the board-aware flows of the request's graph a board may be written for."""
    found = []
    for cls in BOARD_FLOWS:
        found.append(cls.name)
        for edge in predecessors(cls, PRODUCT_FLOWS):
            found.append(
                request_text((ChainElement(edge.producer, edge.output), ChainElement(cls)))
            )
    return [(request, _board_nodes(request)) for request in found]


def _board_nodes(request: str) -> list[str]:
    """The board-aware flows of `request`'s graph: its own flows and the default producers of
    the first, the only one whose input the chain does not bind."""
    elements = parse_request(request).elements
    flows = [*transitive_dependencies(elements[0].flow_class).values()]
    flows += [element.flow_class for element in elements]
    return [cls.name for cls in flows if "board" in cls.Settings.model_fields]


REQUESTS = _requests()


def _cases():
    for request, nodes in REQUESTS:
        requested = parse_request(request).requested.name
        for node in nodes:
            for origin in ORIGINS:
                if origin in ("command line", "API", "API settings") and node != requested:
                    continue  # `-s board=` and the API's settings are the requested flow's own
                yield pytest.param(request, node, origin, id=f"{request}-{node}-{origin}")


def test_the_sweep_covers_every_board_aware_flow_and_the_vivado_chains():
    assert {"nextpnr", "fpga_pack", "openfpgaloader"} <= {cls.name for cls in BOARD_FLOWS}
    assert {"ulx3s_85f", "arty_a7_100t", "arty_a7_35t"} <= set(BUNDLED_BOARDS)
    requests = [request for request, _nodes in REQUESTS]
    assert {"vivado_synth+openfpgaloader", "vivado_alt_synth+openfpgaloader"} <= set(requests)
    for cls in BOARD_FLOWS:
        assert any(cls.name in nodes for _request, nodes in REQUESTS), cls.name
    assert len(list(_cases())) >= 50


@pytest.mark.parametrize("request_, node, origin", list(_cases()))
def test_a_board_alone_gives_every_node_that_shares_the_device_its_part(
    tmp_path, monkeypatch, request_, node, origin
):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    board, part = "arty_a7_35t", _part("arty_a7_35t")
    flows: dict = {}
    targets = None
    kwargs: dict = {}
    if origin == "design":
        flows = {node: {"board": board}}
    elif origin == "project":
        project = root / PROJECT_FILE
        root.mkdir()
        project.write_text(yaml.safe_dump({"flows": {node: {"board": board}}}))
        kwargs["xedaproject"] = str(project)
    elif origin == "target":
        targets = {"arty": {"flows": {node: {"board": board}}}}
    elif origin == "command line":
        kwargs["flow_settings"] = [f"board={board}"]
    elif origin == "command line node":
        kwargs["flow_settings"] = [f"flows.{node}.board={board}"]
    elif origin == "API":
        kwargs["flow_overrides"] = {"board": board}
    elif origin == "API settings":
        requested = parse_request(request_).requested
        kwargs["flow_settings"] = requested.Settings.from_input({"board": board}, design_root=root)
    else:  # a board of a custom database, beside the design
        root.mkdir()
        (root / "boards.yaml").write_text(
            f"MY_BOARD:\n  openfpgaloader_board: mine\n  fpga:\n    part: {CUSTOM_PART}\n"
        )
        board, part = "MY_BOARD", CUSTOM_PART
        flows = {node: {"board": board, "custom_boards_file": "boards.yaml"}}
    design = _write_design(root, flows, targets)

    plan = _plan(tmp_path, request_, design, **kwargs)

    devices = {
        planned.name: planned.settings.fpga.part
        for planned in plan.nodes
        if "fpga" in type(planned.settings).model_fields
    }
    assert node in devices and set(devices.values()) == {part}, devices
    for planned in plan.nodes:
        if "board" in type(planned.settings).model_fields:
            assert planned.settings.board == board, planned.name


@pytest.mark.parametrize("board", BUNDLED_BOARDS)
@pytest.mark.parametrize("flow", [cls.name for cls in BOARD_FLOWS])
def test_every_bundled_board_alone_is_the_device_of_every_board_aware_flow(
    tmp_path, monkeypatch, flow, board
):
    monkeypatch.chdir(tmp_path)
    design = _write_design(tmp_path / "d", {flow: {"board": board}})
    plan = _plan(tmp_path, flow, design)
    devices = {n.name: n.settings.fpga.part for n in plan.nodes if hasattr(n.settings, "fpga")}
    assert set(devices.values()) == {_part(board)}, devices


# --------------------------------------------------------------- a board against a device elsewhere


def test_a_board_and_the_same_part_written_for_another_node_agree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    part = _part("arty_a7_100t")
    flows = {"openfpgaloader": {"board": "arty_a7_100t"}, "vivado_synth": {"fpga": {"part": part}}}
    plan = _plan(tmp_path, "vivado_synth+openfpgaloader", _write_design(tmp_path / "d", flows))
    assert {n.name: n.settings.fpga.part for n in plan.nodes} == {
        "vivado_synth": part,
        "openfpgaloader": part,
    }


def test_a_board_and_another_part_conflict_naming_where_the_board_is_written(tmp_path, monkeypatch):
    """The board reaches the loader and the packer by agreement along their edges: the conflict
    names the section the board is written in, not a board without an origin."""
    monkeypatch.chdir(tmp_path)
    flows = {"nextpnr": {"board": "ulx3s_85f"}, "yosys_fpga": {"fpga": {"part": CUSTOM_PART}}}
    design = _write_design(tmp_path / "d", flows)
    with pytest.raises(FlowSettingsError, match="disagrees") as raised:
        _plan(tmp_path, "openfpgaloader", design)
    message = str(raised.value)
    assert f"[flows.nextpnr] in {design}" in message, message
    assert f"[flows.yosys_fpga] in {design}" in message, message
    assert "the shared board" not in message


def test_a_board_and_another_part_on_one_node_name_both_values_and_origins(tmp_path, monkeypatch):
    """A board and a device written together for the loader, naming different parts."""
    monkeypatch.chdir(tmp_path)
    board, other = _part("arty_a7_100t"), _part("arty_a7_35t")
    flows = {"openfpgaloader": {"board": "arty_a7_100t", "fpga": {"part": other}}}
    design = _write_design(tmp_path / "d", flows)
    with pytest.raises(FlowSettingsError, match="disagree") as raised:
        _plan(tmp_path, "vivado_synth+openfpgaloader", design)
    message = str(raised.value)
    assert repr(board) in message and repr(other) in message, message
    assert f"[flows.openfpgaloader] in {design} (board)" in message, message
    assert f"[flows.openfpgaloader] in {design} (fpga.part)" in message, message


# ---------------------------------------------- the demo repositories' two designs, on stand-ins


def test_a_board_on_the_loader_gives_a_vivado_chain_its_device(tmp_path, monkeypatch):
    """`vivado_synth+openfpgaloader` with `board` written for the loader only, or with the same
    part also written for vivado_synth: the board's part is both nodes' device."""
    monkeypatch.chdir(tmp_path)
    part = _part("arty_a7_100t")
    for flows in (
        {"yosys_fpga": {"fpga.part": part}, "openfpgaloader": {"board": "arty_a7_100t"}},
        {
            "vivado_synth": {"fpga.part": part},
            "yosys_fpga": {"fpga.part": part},
            "openfpgaloader": {"board": "arty_a7_100t"},
        },
    ):
        design = _write_design(tmp_path / "d", flows)
        plan = _plan(tmp_path, "vivado_synth+openfpgaloader", design)
        assert [n.name for n in plan.nodes] == ["vivado_synth", "openfpgaloader"]
        assert {n.settings.fpga.part for n in plan.nodes} == {part}
        assert plan.node("openfpgaloader").settings.board == "arty_a7_100t"


def test_a_device_written_for_a_flow_outside_the_run_is_named_in_the_error(tmp_path, monkeypatch):
    """The chain displaces the loader's default producers, `yosys_fpga` among them: the device
    written for it reaches neither node of the run, and the error of the node that needs one
    (Vivado: the loader needs it only to program the flash) says so."""
    monkeypatch.chdir(tmp_path)
    flows = {"yosys_fpga": {"fpga": {"part": "xc7a35tcpg236-1"}}}
    design = _write_design(tmp_path / "d", flows)
    with pytest.raises(FlowSettingsException, match="vivado_synth needs `fpga`") as raised:
        _plan(tmp_path, "vivado_synth+openfpgaloader", design)
    message = str(raised.value)
    assert (
        f"The `fpga` in [flows.yosys_fpga] in {design} does not reach vivado_synth: "
        "yosys_fpga is not part of this run" in message
    ), message


def test_a_board_written_for_a_flow_outside_the_run_is_named_in_the_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _write_design(tmp_path / "d", {"openfpgaloader": {"board": "arty_a7_35t"}})
    with pytest.raises(FlowSettingsException, match="fpga_pack needs `fpga`") as raised:
        _plan(tmp_path, "fpga_pack", design)
    message = str(raised.value)
    assert (
        f"The `board` in [flows.openfpgaloader] in {design} does not reach fpga_pack: "
        "openfpgaloader is not part of this run" in message
    ), message


def test_a_device_written_on_the_command_line_for_a_displaced_flow_is_named(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    design = _write_design(tmp_path / "d", {})
    with pytest.raises(FlowSettingsException, match="vivado_synth needs `fpga`") as raised:
        _plan(
            tmp_path,
            "vivado_synth+openfpgaloader",
            design,
            flow_settings=["flows.yosys_fpga.fpga.part=xc7a35tcpg236-1"],
        )
    assert (
        "The `fpga` in [flows.yosys_fpga] in the command line does not reach vivado_synth"
        in str(raised.value)
    )


def test_a_device_the_loader_needs_for_the_flash_is_named_where_it_does_not_reach(
    tmp_path, monkeypatch
):
    """The loader needs the device only to program the flash (`required_settings_for`): with
    `write_flash`, the part written only for yosys_fpga, outside the run, is named in its error."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    (root / "given.bit").write_bytes(b"bits")
    flows = {"yosys_fpga": {"fpga": {"part": "xc7a35tcpg236-1"}}}
    design = _write_design(root, flows)
    data = yaml.safe_load(design.read_text())
    data["rtl"]["sources"].append({"file": "given.bit", "type": "Bitstream"})
    design.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(FlowSettingsException, match="openfpgaloader needs `fpga`") as raised:
        _plan(tmp_path, "openfpgaloader", design, flow_settings=["write_flash=true"])
    assert (
        f"The `fpga` in [flows.yosys_fpga] in {design} does not reach openfpgaloader: "
        "yosys_fpga is not part of this run" in str(raised.value)
    )


@pytest.mark.parametrize("origin", ["design file", "command line", "API"])
def test_a_device_written_as_one_dotted_key_is_named_where_it_does_not_reach(
    tmp_path, monkeypatch, origin
):
    """`fpga.part` written as one dotted key is the `fpga` it names, in each origin: the note on
    a section that does not reach the flow finds it there as it finds `fpga: {part: ...}`."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    (root / "given.bit").write_bytes(b"bits")
    dotted = {"yosys_fpga": {"fpga.part": "xc7a35tcpg236-1"}}
    design = _write_design(root, dotted if origin == "design file" else {})
    data = yaml.safe_load(design.read_text())
    data["rtl"]["sources"].append({"file": "given.bit", "type": "Bitstream"})
    design.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(FlowSettingsException, match="openfpgaloader needs `fpga`") as raised:
        if origin == "API":
            DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
                Openfpgaloader,
                Design.from_file(design),
                {"write_flash": True},
                all_flows_settings=dotted,
            )
        else:
            given = ["write_flash=true"]
            if origin == "command line":
                given.append("flows.yosys_fpga.fpga.part=xc7a35tcpg236-1")
            _plan(tmp_path, "openfpgaloader", design, flow_settings=given)
    message = str(raised.value)
    assert "The `fpga` in [flows.yosys_fpga] in " in message, message
    assert "does not reach openfpgaloader: yosys_fpga is not part of this run" in message, message
    if origin != "API":
        label = str(design) if origin == "design file" else "the command line"
        assert f"The `fpga` in [flows.yosys_fpga] in {label} does not reach" in message, message


def test_planning_reports_an_unused_section_and_no_device_for_it(tmp_path, monkeypatch, caplog):
    """Planning a chain that leaves out flows written in the design: one INFO line for each
    unused section, and nothing else about their settings -- no device "detected" for a flow
    that does not run, and no board looked up for one."""
    monkeypatch.chdir(tmp_path)
    part = _part("arty_a7_100t")
    flows = {
        "yosys_fpga": {"fpga": {"part": "xc7a35tcpg236-1"}},
        "nextpnr": {"board": "arty_a7_35t", "seed": 3},
        "vivado_synth": {"fpga": {"part": part}},
    }
    design = _write_design(tmp_path / "d", flows)
    with caplog.at_level(logging.INFO):
        _plan(tmp_path, "vivado_synth+openfpgaloader", design)
    reported = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert "yosys_fpga is not part of this run: its settings are unused" in reported, reported
    assert "nextpnr is not part of this run: its settings are unused" in reported, reported
    assert not any("fpga_pack" in message for message in reported), "it has no settings"
    from_settings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.INFO and r.name in ("xeda.flow.fpga", "xeda.board")
    ]
    assert from_settings == []
