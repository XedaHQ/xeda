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


Ports = dict[str, tuple[str, str]]


def _bus(name: str, pins: str, standard: str) -> Ports:
    return {f"{name}[{index}]": (pin, standard) for index, pin in enumerate(pins.split())}


@dataclass(frozen=True)
class Board:
    id: str
    part: str
    loader: str
    xdc: str
    #: the port groups of the pin file, in order: group -> port -> (pin, I/O standard)
    groups: dict[str, Ports]
    #: the port documented as the reset button
    reset: str
    #: the level at which a button is pressed and an LED is lit
    level: str

    @property
    def ports(self) -> Ports:
        """Every port the pin file constrains."""
        return {port: pin for group in self.groups.values() for port, pin in group.items()}


BASYS3 = Board(
    "basys_3",
    "xc7a35tcpg236-1",
    "basys3",
    "boards/basys3/board.xdc",
    {
        "Clock": {"clk": ("W5", "LVCMOS33")},
        "LEDs": _bus("led", "U16 E19 U19 V19 W18 U15 U14 V14 V13 V3 W3 U3 P3 N3 P1 L1", "LVCMOS33"),
        "Buttons": {
            "btnC": ("U18", "LVCMOS33"),
            "btnU": ("T18", "LVCMOS33"),
            "btnL": ("W19", "LVCMOS33"),
            "btnR": ("T17", "LVCMOS33"),
            "btnD": ("U17", "LVCMOS33"),
        },
        "UART": {"RsRx": ("B18", "LVCMOS33"), "RsTx": ("A18", "LVCMOS33")},
    },
    reset="btnC",
    level="high",
)
STLV7325 = Board(
    "stlv7325_v2",
    "xc7k325tffg676-2",
    "stlv7325",
    "boards/stlv7325_v2/board.xdc",
    {
        "Clock": {"clk_p": ("AB11", "DIFF_SSTL15"), "clk_n": ("AC11", "DIFF_SSTL15")},
        "LEDs": _bus("led", "AA2 AD5 W10 Y10 AE10 W11 V11 Y12", "LVCMOS15"),
        "Buttons": {"btn[0]": ("AC16", "LVCMOS15"), "btn[1]": ("C24", "LVCMOS33")},
        "UART": {"uart_rx": ("K21", "LVCMOS33"), "uart_tx": ("L23", "LVCMOS33")},
    },
    reset="btn[0]",
    level="low",
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
    assert data["openfpgaloader_board"] == board.loader
    assert data["fpga"] == {"part": board.part}
    assert data["xdc"] == board.xdc
    (row,) = [row for row in boards_info() if row["board"] == board.id]
    assert row["openfpgaloader_board"] == board.loader and row["xdc"] == board.xdc
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
    assert {"basys_3", "stlv7325_v2"} <= names
    assert not {"basys3", "stlv7325", "stlv7325_v1"} & names
    with pytest.raises(FlowSettingsError, match="Unknown board"):
        WithFpgaBoardSettings.from_input({"board": "STLV7325"})


# ------------------------------------------------------------------------------------ pin files


def _text(board: Board) -> str:
    with WithFpgaBoardSettings.from_input({"board": board.id}).board_file(board.xdc) as path:
        return path.read_text()


def _groups(text: str) -> dict[str, tuple[list[str], list[str]]]:
    """The pin file's `## <group>` sections: each one's comments and its commands."""
    groups: dict[str, tuple[list[str], list[str]]] = {}
    comments: list[str] = []
    commands: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            comments, commands = [], []
            groups[line[3:].strip()] = (comments, commands)
        elif line.startswith("#"):
            comments.append(line)
        elif line.strip():
            assert commands is not None, f"a command before the first group: {line}"
            commands.append(line)
    return groups


@BY_BOARD
def test_the_pin_file_constrains_exactly_the_supported_ports(board):
    text = _text(board)
    pins = _pins(text)
    assert set(pins) == set(board.ports)
    for port, (pin, standard) in board.ports.items():
        assert pins[port] == {"LOC": pin, "IOSTANDARD": standard}, port
    # no pin is given to two ports
    assert len({pin for pin, _ in board.ports.values()}) == len(board.ports)
    # timing comes from the flow's clocks, never from a board file
    active = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "create_clock" not in active and "PACKAGE_PIN" not in active


@BY_BOARD
def test_the_pin_file_has_one_group_per_kind_of_port_in_order(board):
    groups = _groups(_text(board))
    assert list(groups) == list(board.groups)
    for name, (_comments, commands) in groups.items():
        assert set(_pins("\n".join(commands))) == set(board.groups[name]), name


@BY_BOARD
def test_the_vocabulary_has_every_led_and_button_and_the_uart(board):
    leds = [port for port in board.ports if port.startswith("led[")]
    assert leds == [f"led[{index}]" for index in range(len(leds))]
    assert len(leds) == {"basys_3": 16, "stlv7325_v2": 8}[board.id]
    assert len(board.groups["Buttons"]) == {"basys_3": 5, "stlv7325_v2": 2}[board.id]
    assert len(board.groups["UART"]) == 2
    assert board.reset in board.groups["Buttons"]


@BY_BOARD
def test_every_group_names_its_source_and_the_pin_file_its_license_and_limits(board):
    text = _text(board)
    groups = _groups(text)
    for name, (comments, _commands) in groups.items():
        assert any(line.startswith("# Source: ") for line in comments), name
    header = text.split("\n## ", 1)[0]
    assert "create_clock" in header and "Copyright" in header and "LICENSE.md" in header
    # what a design needs to know to use the ports it names
    leds = " ".join(groups["LEDs"][0])
    buttons = " ".join(groups["Buttons"][0])
    assert f"active {board.level}" in leds and f"active {board.level}" in buttons
    assert any(
        board.reset in line and "reset" in line.lower() for line in groups["Buttons"][0]
    ), "the reset button is documented"
    uart = " ".join(groups["UART"][0])
    assert "input" in uart and "output" in uart
    clock = " ".join(groups["Clock"][0])
    assert ("differential" in clock) == (board is STLV7325)


def test_each_pin_file_names_the_sources_its_pins_come_from():
    basys = _text(BASYS3)
    assert "Digilent" in basys and "Basys-3-Master.xdc" in basys and "MIT" in basys
    stlv = _text(STLV7325)
    assert "openXC7 demo-projects" in stlv and "BSD-3-Clause" in stlv
    assert "LiteX-Boards" in stlv and "BSD-2-Clause" in stlv
    for board in BOARDS:
        assert "LiteX-Boards" in _text(board)  # every pin is found there too


def test_the_licenses_shipped_with_the_pin_files_are_those_the_sources_carry():
    basys = (ROOT / "src/xeda/data/boards/basys3/LICENSE.md").read_text()
    assert basys.startswith("MIT License") and "Copyright (c) 2017 Digilent" in basys
    assert basys == (ROOT / "src/xeda/data/boards/arty/LICENSE.md").read_text()
    stlv = (ROOT / "src/xeda/data/boards/stlv7325_v2/LICENSE.md").read_text()
    assert "BSD 3-Clause License" in stlv and "Hans Baier" in stlv
    assert "BSD 2-Clause License" in stlv and "Enjoy-Digital" in stlv


def test_the_stlv_clock_is_differential_and_the_basys_clock_is_not():
    assert set(STLV7325.groups["Clock"]) == {"clk_p", "clk_n"}
    assert set(BASYS3.groups["Clock"]) == {"clk"}


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
def test_the_loader_is_given_the_board_name_alone(tmp_path, board):
    """It knows the board's part, and `--fpga-part` would replace it with one that names no
    bridge bitstream (a part with a speed grade)."""
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": board.id})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["tool"] == "openFPGALoader"
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--board",
        board.loader,
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
# reaches nextpnr. Each design has a port for every port of the board's pin file, so the tools
# see every pin and I/O standard. A bitstream shows the tools accept the part and the pins, not
# that the pins match a board's wiring.

BASYS3_RTL = """\
`default_nettype none
module top (
    input  wire clk, btnC, btnU, btnL, btnR, btnD, RsRx,
    output wire RsTx,
    output wire [15:0] led
);
    reg [27:0] count = 0;
    always @(posedge clk) count <= btnC ? 0 : count + 1;
    assign led = {count[27:16], btnU, btnD, btnL, btnR};
    assign RsTx = RsRx;
endmodule
"""

STLV7325_RTL = """\
`default_nettype none
module top (
    input  wire clk_p, clk_n, uart_rx,
    input  wire [1:0] btn,
    output wire uart_tx,
    output wire [7:0] led
);
    wire clk_ibufg, clk;
    IBUFDS ibuf_inst (.I(clk_p), .IB(clk_n), .O(clk_ibufg));
    BUFG bufg_inst (.I(clk_ibufg), .O(clk));
    reg [31:0] count = 0;
    always @(posedge clk) count <= ~btn[0] ? 0 : count + 1;
    assign led = {count[31:26], btn[1], btn[0]};
    assign uart_tx = uart_rx;
endmodule
"""
RTL = {BASYS3.id: BASYS3_RTL, STLV7325.id: STLV7325_RTL}


@BY_BOARD
def test_the_designs_built_for_real_use_every_port_of_the_pin_file(board):
    header = RTL[board.id].split(");", 1)[0]
    declared = set(re.findall(r"[A-Za-z_]\w*", header)) - {
        "module",
        "top",
        "input",
        "output",
        "wire",
        "default_nettype",
        "none",
    }
    assert {re.sub(r"\[\d+\]", "", port) for port in board.ports} == declared


@BY_BOARD
def test_the_board_builds_a_bitstream_with_openxc7(tmp_path, tmp_path_factory, board):
    tool_utils.require_openxc7()
    base = tmp_path_factory.getbasetemp()
    common = base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text(RTL[board.id])
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    design = Design(
        name=f"{board.id.lower()}_{worker}",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top"},
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
        assert re.search(rf"LOC {pin} \[get_ports \{{?{re.escape(port)}\}}?\]", constraints), port
