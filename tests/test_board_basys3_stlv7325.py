"""The bundled Digilent Basys 3 and STLV7325 v2 boards: database entries, pin files, wheel
resources, the programmer's arguments (through the fake loader only) and, opt-in, a real build.

NOTHING HERE MAY REACH A REAL PROGRAMMER: loader tests use `test_openfpgaloader`'s `fake_loader`
fixture, which proves `openFPGALoader` resolves to the fake before a test runs.
"""

import glob
import os
import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import pytest

from xeda import Design
from xeda.board import WithFpgaBoardSettings, get_board_data
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import FpgaPack
from xeda.flows.nextpnr_constraints import merge_constraints
from xeda.introspect import boards_info

from . import tool_utils
from .test_nextpnr_constraints import make_flow
from .test_openfpgaloader import _calls, _prebuilt, _program, fake_loader  # noqa: F401

ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Board:
    id: str
    part: str
    loader: str
    xdc: str
    #: every port the pin file constrains: port -> (pin, I/O standard)
    ports: dict[str, tuple[str, str]]


BASYS3 = Board(
    "BASYS_3",
    "xc7a35tcpg236-1",
    "basys3",
    "boards/basys3/board.xdc",
    {"clk": ("W5", "LVCMOS33"), "led": ("U16", "LVCMOS33")},
)
STLV7325 = Board(
    "STLV7325_V2",
    "xc7k325tffg676-2",
    "stlv7325",
    "boards/stlv7325_v2/board.xdc",
    {
        "clk_p": ("AB11", "DIFF_SSTL15"),
        "clk_n": ("AC11", "DIFF_SSTL15"),
        "led": ("AA2", "LVCMOS15"),
    },
)
BOARDS = [BASYS3, STLV7325]
BY_BOARD = pytest.mark.parametrize("board", BOARDS, ids=lambda board: board.id)


def _pins(text: str) -> dict[str, dict[str, str]]:
    """The properties each port of a pin file is given: `set_property <name> <value> [...]`."""
    found: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"set_property (\w+) (\S+) \[get_ports \{?([\w\[\]]+)\}?\]", line)
        assert match, f"not a pin command: {line}"
        name, value, port = match.groups()
        found.setdefault(port, {})[name] = value
    return found


# ----------------------------------------------------------------------------------- the database


@BY_BOARD
def test_the_board_is_in_the_bundled_database(board):
    data = get_board_data(board.id)
    assert data["name"] == board.loader
    assert data["fpga"] == {"part": board.part}
    assert data["xdc"] == board.xdc
    (row,) = [row for row in boards_info() if row["board"] == board.id]
    assert row["name"] == board.loader and row["xdc"] == board.xdc
    assert row["fpga"] == {"part": board.part}


@BY_BOARD
def test_the_board_gives_a_complete_xilinx_part(board):
    settings = WithFpgaBoardSettings.from_input({"board": board.id})
    assert settings.fpga is not None and settings.fpga.part == board.part
    assert (settings.fpga.vendor, settings.fpga.family) == (
        "xilinx",
        "artix-7" if board is BASYS3 else "kintex-7",
    )
    assert settings.fpga.package and settings.fpga.pins and settings.fpga.speed


def test_the_database_has_no_alias_and_no_unversioned_stlv_entry():
    names = {row["board"] for row in boards_info()}
    assert {"BASYS_3", "STLV7325_V2"} <= names
    assert not {"BASYS3", "STLV7325", "STLV7325_V1"} & names
    with pytest.raises(FlowSettingsError, match="Unknown board"):
        WithFpgaBoardSettings.from_input({"board": "STLV7325"})


# ------------------------------------------------------------------------------------ pin files


@BY_BOARD
def test_the_pin_file_constrains_exactly_the_supported_ports(board):
    with WithFpgaBoardSettings.from_input({"board": board.id}).board_file(board.xdc) as path:
        text = path.read_text()
    pins = _pins(text)
    assert set(pins) == set(board.ports)
    for port, (pin, standard) in board.ports.items():
        assert pins[port] == {"LOC": pin, "IOSTANDARD": standard}, port
    # timing comes from the flow's clocks, never from a board file
    active = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "create_clock" not in active and "PACKAGE_PIN" not in active


@BY_BOARD
def test_the_pin_file_records_its_source_license_and_limits(board):
    comments = "\n".join(
        line
        for line in (ROOT / "src/xeda/data" / board.xdc).read_text().splitlines()
        if line.startswith("#")
    )
    for needle in (
        "openXC7 demo-projects",
        "BSD-3-Clause",
        "LiteX-Boards",
        "polarity",
        "create_clock",
    ):
        assert needle in comments, needle
    license_text = (ROOT / "src/xeda/data" / Path(board.xdc).parent / "LICENSE.md").read_text()
    assert "BSD 3-Clause License" in license_text and "Hans Baier" in license_text


def test_the_stlv_clock_is_differential_and_the_basys_clock_is_not():
    assert {"clk_p", "clk_n"} <= set(STLV7325.ports) and "clk" not in STLV7325.ports
    assert "clk" in BASYS3.ports and not {"clk_p", "clk_n"} & set(BASYS3.ports)


# ------------------------------------------------------------------------------ wheel resources


@BY_BOARD
def test_the_board_files_are_package_data_so_a_wheel_ships_them(board):
    """Every file a database entry names, and its license, matches a `package-data` pattern."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = project["tool"]["setuptools"]["package-data"]["xeda.data"]
    data = ROOT / "src/xeda/data"
    shipped = {
        path for pattern in patterns for path in glob.glob(pattern, root_dir=data, recursive=True)
    }
    assert board.xdc in shipped
    assert f"{Path(board.xdc).parent}/LICENSE.md" in shipped


@BY_BOARD
def test_the_board_files_are_readable_as_package_resources(board):
    resource = files("xeda.data").joinpath(board.xdc)
    assert resource.is_file() and "set_property" in resource.read_text()


# ---------------------------------------------------------------------------- the programmer


@BY_BOARD
@pytest.mark.usefixtures("fake_loader")
def test_the_loader_is_given_the_board_name_and_the_full_part(tmp_path, board):
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": board.id})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["tool"] == "openFPGALoader"
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--board",
        board.loader,
        "--fpga-part",
        board.part,
    ]


# ----------------------------------------------------------------------- the pin file's use


@BY_BOARD
def test_nextpnr_falls_back_to_the_board_pin_file_and_typed_pins_displace_it(
    tmp_path, monkeypatch, board
):
    flow, _ = make_flow(tmp_path, monkeypatch, settings={"board": board.id})
    flow._prepare_board_inputs()
    (path,) = flow._pin_inputs
    assert path.read_text() == (ROOT / "src/xeda/data" / board.xdc).read_text()
    assert flow.implicit_inputs == [path]
    merged = merge_constraints(flow._pin_inputs).text
    for port, (pin, _) in board.ports.items():
        assert f"set_property LOC {pin} [get_ports {{{port}}}]" in merged or (
            f"set_property LOC {pin} [get_ports {port}]" in merged
        )
    (tmp_path / "mine.xdc").write_text("# mine\n")
    typed, _ = make_flow(tmp_path, monkeypatch, sources=["mine.xdc"], settings={"board": board.id})
    typed._prepare_board_inputs()
    assert typed._pin_inputs == [tmp_path / "mine.xdc"] and typed.implicit_inputs == []


# -------------------------------------------------------------------------- the real toolchain
#
# Opt-in (`XEDA_TESTS_OPENXC7=1`, openXC7 first on PATH), as in `test_openxc7_real.py`, whose
# run root and chip-database cache these share. The design names no pin file: the board's
# reaches nextpnr. A bitstream shows the tools accept the part and the pins, not that the pins
# match a board's wiring.

BASYS3_RTL = """\
`default_nettype none
module blinky (input wire clk, output wire led);
    reg [24:0] r_count = 0;
    always @(posedge clk) r_count <= r_count + 1;
    assign led = r_count[24];
endmodule
"""

STLV7325_RTL = """\
`default_nettype none
module blinky (input wire clk_p, input wire clk_n, output wire led);
    wire clk_ibufg, clk;
    IBUFDS ibuf_inst (.I(clk_p), .IB(clk_n), .O(clk_ibufg));
    BUFG bufg_inst (.I(clk_ibufg), .O(clk));
    reg [26:0] r_count = 0;
    always @(posedge clk) r_count <= r_count + 1;
    assign led = r_count[26];
endmodule
"""
RTL = {BASYS3.id: BASYS3_RTL, STLV7325.id: STLV7325_RTL}


@BY_BOARD
def test_the_board_builds_a_bitstream_with_openxc7(tmp_path, tmp_path_factory, board):
    tool_utils.require_openxc7()
    base = tmp_path_factory.getbasetemp()
    common = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    root = tmp_path / "design"
    root.mkdir()
    (root / "blinky.v").write_text(RTL[board.id])
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    design = Design(
        name=f"{board.id.lower()}_{worker}",
        design_root=root,
        rtl={"sources": ["blinky.v"], "top": "blinky"},
        flow={"fpga_pack": {"board": board.id}, "yosys_fpga": {"flatten": True}},
    )
    run_root = common / "openxc7-run-root"
    flow = DefaultRunner(run_root, display_results=False).run(FpgaPack, design)
    assert flow is not None and flow.succeeded
    assert flow.outputs.bitstream.stat().st_size > 1_000_000
    routed = (run_root / design.name / "nextpnr/results.json").read_text()
    assert f'"{board.part}"' in routed
    constraints = (run_root / design.name / "nextpnr/constraints.xdc").read_text()
    for port, (pin, _) in board.ports.items():
        assert re.search(rf"LOC {pin} \[get_ports \{{?{port}\}}?\]", constraints), port
