"""The `openfpgaloader` flow chooses the probe whose FPGA matches the part.

openFPGALoader opens the first probe that fits the cable's USB id, before it reads any ID code, so
with two Digilent boards connected (both 0x0403:0x6010) it can open the wrong one, and
`--fpga-part` does not change that. The flow lists the probes, reads the device of each when two
or more fit, and names the one whose FPGA matches the part.

No test here may reach a real programmer. Every launch goes through the `fake_loader` fixture, and
the fake answers `--list-boards`, `--list-cables`, `--scan-usb` and `--detect` from the machine a
test describes (`tests/loader_world.py`, in the formats of the real loader). The samples of the
parser tests have the output format of openFPGALoader v1.2.0. Their serial numbers are made up.
"""

import logging

import pytest

from xeda.flow import FlowFatalError
from xeda.flows import Openfpgaloader
from xeda.flows.openfpgaloader import (
    PROBE_NOTE_CHOSEN,
    PROBE_NOTE_DEFAULT,
    PROBE_NOTE_NAMED,
    PROBE_NOTE_SINGLE,
    LoaderFailure,
    OpenfpgaloaderTool,
    loader_failure,
)
from xeda.flows.openfpgaloader_probes import (
    ChainDevice,
    Chosen,
    Detection,
    Reading,
    Refusal,
    UsbId,
    UsbProbe,
    changes_the_cable,
    choose_probe,
    model_matches_part,
    names_a_probe,
    parse_board_cables,
    parse_cable_ids,
    parse_detect,
    parse_scan_usb,
    part_is_matchable,
    probe_selector,
    probes_without_a_name,
    refusal_text,
    scan_failure,
    scan_open_errors,
    selectable_usb_id,
)
from xeda.utils import ToolException

from . import tool_utils
from .loader_world import Usb, World, detect_text, queries, scan_usb_text
from .test_openfpgaloader import _calls, _prebuilt, _runner, assert_fake_loader

ARTY = "AB00000001"  # an Arty A7-100T: xc7a100, IDCODE 0x3631093
BASYS = "AB00000002"  # a Basys 3: xc7a35, IDCODE 0x362d093
ARTY_PART = "xc7a100tcsg324-1"
LOGGER = "xeda.flows.openfpgaloader"


@pytest.fixture
def fake_loader(tmp_path, monkeypatch):
    """The fake toolchain first on `PATH`, and a check that `openFPGALoader` is the fake."""
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    assert assert_fake_loader() == prefix / "bin/openFPGALoader"
    return prefix


# ------------------------------------------------- samples of the loader's output (v1.2.0)

REAL_CABLES = """\
cable name               vid:pid
anlogicCable             0x0547:1002
dfu                      0x0000:0000
digilent                 0x0403:6010
digilent_ad              0x0403:6014
ft231X                   0x0403:6015
xilinxPlatformCableUsb   0x03fd:0013
xilinxPlatformCableUsb_alt0x03fd:000d
xvc-client               0x0000:0000
"""

REAL_BOARDS = """\
board name                 cable_name         fpga_part
LD-KONFEKT                 Undefined          LFE5U-12F-6BG256C
LD-KONFEKT-DFU             dfu                Undefined
arty_a7_100t               digilent           xc7a100tcsg324
ulx3s                      ft231X             LFE5U-85F-6BG381C
"""

REAL_SCAN = """\
Bus device vid:pid       probe_type manufacturer      serial       product
001 002    0x0403:0x6010 FTDI2232   Digilent          AB00000001   Digilent USB Device
001 003    0x0403:0x6015 ft231X     FER-RADIONA-EMARD FT00000001   ULX3S FPGA 85K v3.0.8
001 008    0x0403:0x6011 ft4232     Xilinx            XC00000001   Xilinx Debugger
"""

REAL_DETECT = """\
Jtag frequency : requested 6.00MHz    -> real 6.00MHz
index 0:
\tidcode 0x3631093
\tmanufacturer xilinx
\tfamily artix a7 100t
\tmodel  xc7a100
\tirlength 6
"""

REAL_DETECT_FAILURE = """\
unable to open ftdi device: -3 (device not found)
JTAG init failed with: unable to open ftdi device
"""

#: `--detect` on the ft231X cable of the ULX3S: no chain device is reported, and status 0.
REAL_DETECT_SILENT = """\
Jtag probe limited to 3MHz
Jtag frequency : requested 6000000Hz -> real 3000000Hz
ret 0
"""


# ---------------------------------------------------------------------------- the readers


def test_the_cable_list_gives_each_cable_its_usb_id():
    cables = parse_cable_ids(REAL_CABLES)
    assert cables["digilent"] == UsbId(0x0403, 0x6010)
    assert cables["ft231X"] == UsbId(0x0403, 0x6015)
    assert str(cables["digilent"]) == "0403:6010"
    # a long name runs into the id with no space
    assert cables["xilinxPlatformCableUsb_alt"] == UsbId(0x03FD, 0x000D)
    assert cables["xilinxPlatformCableUsb"] == UsbId(0x03FD, 0x0013)
    # no USB device (0000:0000), and the header, are no cable
    assert "dfu" not in cables and "xvc-client" not in cables and "cable" not in cables


@pytest.mark.parametrize(
    "row", ["digilent 0X0403:0X6010", "digilent 0403:6010", "digilent 403:6010"]
)
def test_the_cable_list_is_read_with_any_hex_spelling(row):
    assert parse_cable_ids(row) == {"digilent": UsbId(0x0403, 0x6010)}


def test_the_board_list_gives_each_board_its_cable():
    boards = parse_board_cables(REAL_BOARDS)
    assert boards == {
        "LD-KONFEKT-DFU": "dfu",
        "arty_a7_100t": "digilent",
        "ulx3s": "ft231X",
    }  # `Undefined` is no cable, the header is no board


def test_the_usb_scan_reads_the_probes_of_the_real_output():
    arty, ulx3s, debugger = parse_scan_usb(REAL_SCAN)
    assert arty == UsbProbe(
        1, 2, UsbId(0x0403, 0x6010), "FTDI2232", "Digilent", ARTY, "Digilent USB Device"
    )
    # a manufacturer and a product with spaces or dashes stay whole
    assert (ulx3s.manufacturer, ulx3s.serial) == ("FER-RADIONA-EMARD", "FT00000001")
    assert ulx3s.product == "ULX3S FPGA 85K v3.0.8"
    assert (debugger.usb_id, debugger.serial) == (UsbId(0x0403, 0x6011), "XC00000001")
    assert (debugger.manufacturer, debugger.product) == ("Xilinx", "Xilinx Debugger")


def test_a_probe_with_no_serial_has_none_and_a_manufacturer_with_spaces_stays_whole():
    scan = (
        "Bus device vid:pid       probe_type manufacturer      serial       product\n"
        "001 004    0x0403:0x6010 FTDI2232   Some Maker Inc    none         My Probe 2\n"
    )
    (probe,) = parse_scan_usb(scan)
    assert (probe.serial, probe.manufacturer, probe.product) == (
        None,
        "Some Maker Inc",
        "My Probe 2",
    )
    assert probe.serial_readable


@pytest.mark.parametrize("usb_id", ["0x0403:0x6010", "0403:6010", "0X0403:0X6010", "0x403:6010"])
def test_the_usb_scan_reads_any_hex_spelling_of_the_id(usb_id):
    scan = (
        "Bus device vid:pid       probe_type manufacturer serial product\n"
        f"001 002    {usb_id:<13} FTDI2232   Digilent     ABC123 Digilent USB Device\n"
    )
    assert [p.usb_id for p in parse_scan_usb(scan)] == [UsbId(0x0403, 0x6010)]


def _old_scan(rows):
    """`--scan-usb` as v0.13.1 prints it (`libusb_ll.cpp`): `probe type` with a space, and the
    columns by `%-13s %-15s %-12s %-20s`."""
    lines = [
        "%3s %3s %-13s %-15s %-12s %-20s %s"
        % ("Bus", "device", "vid:pid", "probe type", "manufacturer", "serial", "product")
    ]
    for bus, device, usb_id, probe, manufacturer, serial, product in rows:
        lines.append(
            "%03d %03d    %s %-15s %-12s %-20s %s"
            % (bus, device, usb_id, probe, manufacturer, serial, product)
        )
    return "\n".join(lines) + "\n"


def _current_scan(rows):
    """A current-style scan table with widths grown from the actual device strings."""
    probe_width = max([10, *(len(row[3]) for row in rows)]) + 1
    manufacturer_width = max([12, *(len(row[4]) for row in rows)]) + 1
    serial_width = max([6, *(len(row[5]) for row in rows)]) + 1
    lines = [
        f"{'Bus':<4}{'device':<7}{'vid:pid':<14}{'probe_type':<{probe_width}}"
        f"{'manufacturer':<{manufacturer_width}}{'serial':<{serial_width}}product"
    ]
    for bus, device, usb_id, probe, manufacturer, serial, product in rows:
        lines.append(
            f"{bus:03d} {device:03d}    {usb_id:<14}{probe:<{probe_width}}"
            f"{manufacturer:<{manufacturer_width}}{serial:<{serial_width}}{product}"
        )
    return "\n".join(lines) + "\n"


def test_the_scan_of_the_oldest_supported_loader_is_read_too():
    scan = _old_scan(
        [
            (1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", BASYS, "Digilent USB Device"),
            (1, 2, "0x0403:0x6010", "FTDI2232", "Some Maker", ARTY, "My Probe"),
        ]
    )
    first, second = parse_scan_usb(scan)
    assert (first.bus, first.device, first.serial) == (1, 3, BASYS)
    assert (second.manufacturer, second.serial, second.product) == ("Some Maker", ARTY, "My Probe")
    assert (first.probe_type, first.product) == ("FTDI2232", "Digilent USB Device")
    assert first.serial_readable and second.serial_readable
    assert scan_failure(scan) is None


def test_the_old_scan_does_not_accept_a_serial_from_a_shifted_column():
    scan = _old_scan(
        [
            (1, 3, "0x0403:0x6010", "FTDI2232", "ManufacturerTooLong", ARTY, "My Probe"),
            (1, 2, "0x0403:0x6010", "FTDI2232", "Digilent", BASYS, "Other Probe"),
        ]
    )
    first, second = parse_scan_usb(scan)
    assert first.serial is None
    assert second.serial == BASYS


def test_the_old_scan_keeps_unusual_serial_characters():
    scan = _old_scan([(1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", "A+B:09.2", "My Probe")])
    (probe,) = parse_scan_usb(scan)
    assert probe.serial == "A+B:09.2"


def test_the_old_scan_keeps_a_serial_longer_than_its_column():
    serial = "serial+with:punctuation-123456"
    scan = _old_scan([(1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", serial, "My Probe")])
    (probe,) = parse_scan_usb(scan)
    assert probe.serial == serial
    assert probe.serial_readable


def test_the_old_scan_rejects_an_ambiguous_manufacturer_tail():
    scan = _old_scan(
        [
            (
                1,
                3,
                "0x0403:0x6010",
                "FTDI2232",
                "Acme Devices " + "X" * 20,
                ARTY,
                "My Probe",
            )
        ]
    )
    (probe,) = parse_scan_usb(scan)
    assert probe.serial is None
    assert not probe.serial_readable


def test_the_current_scan_preserves_a_punctuated_serial_at_its_column_boundary():
    scan = _current_scan([(1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", "A+B:09.2", "My Probe")])
    (probe,) = parse_scan_usb(scan)
    assert probe.serial == "A+B:09.2"
    assert probe.serial_readable


def test_the_current_scan_preserves_an_overlong_serial_in_its_column():
    serial = "serial+with:punctuation-123456"
    scan = _current_scan([(1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", serial, "My Probe")])
    (probe,) = parse_scan_usb(scan)
    assert probe.serial == serial
    assert probe.serial_readable


def test_the_current_scan_preserves_a_long_product_word():
    product = "DigilentUSBDevice"
    scan = _current_scan([(1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", ARTY, product)])
    (probe,) = parse_scan_usb(scan)
    assert probe.serial == ARTY
    assert probe.product == product
    assert probe.serial_readable


def test_a_bus_or_device_number_above_127_is_read_as_the_loader_meant_it():
    """The loader prints the numbers with `std::to_string(char)`: 161 comes out as -95."""
    scan = (
        "Bus device vid:pid       probe_type manufacturer serial product\n"
        "-95 -23    0x0403:0x6010 FTDI2232   Digilent     ABC123 Digilent USB Device\n"
    )
    (probe,) = parse_scan_usb(scan)
    assert (probe.bus, probe.device) == (161, 233)


@pytest.mark.parametrize(
    "bus_text, bus, device_text, device",
    [
        ("-128", 128, "004", 4),
        ("0-1", 255, "004", 4),
        ("001", 1, "-128", 128),
        ("001", 1, "0-1", 255),
    ],
)
def test_the_current_scan_reads_signed_char_values_that_fill_their_columns(
    bus_text, bus, device_text, device
):
    """The loader's signed-char formatter can fill a numeric field and remove its separator."""
    scan = _current_scan([(1, 4, "0x0403:0x6010", "FTDI2232", "Digilent", "ABC123", "Probe")])
    scan = scan.replace("001 004    ", f"{bus_text:<4}{device_text:<7}")
    (probe,) = parse_scan_usb(scan)
    assert (probe.bus, probe.device, probe.serial) == (bus, device, "ABC123")


def test_a_scan_that_printed_no_table_is_a_failure_with_its_last_line():
    assert scan_failure("") == "it printed nothing"
    assert scan_failure("Error: failed to allocate buffer\n") == "Error: failed to allocate buffer"
    table = "Bus device vid:pid       probe_type manufacturer serial product\n"
    assert scan_failure(table) is None  # no probe, but a table


def test_a_partial_scan_header_is_not_treated_as_a_usb_table():
    partial_header = "probe_type manufacturer serial product\n"
    assert parse_scan_usb(partial_header) == []


def test_a_scan_header_with_unrecognized_column_case_is_not_treated_as_a_table():
    uppercase_header = "BUS DEVICE VID:PID PROBE_TYPE MANUFACTURER SERIAL PRODUCT\n"
    assert parse_scan_usb(uppercase_header) == []


def test_a_device_the_scan_could_not_open_is_reported_for_its_usb_id():
    scan = (
        "Error: can't open device with vid:vid = 0x0403:0x6010. Error code -3 Access denied\n"
        "Error: can't open device with vid:vid = 0x0403:0x6015. Error code -3 Access denied\n"
        "Bus device vid:pid       probe_type manufacturer serial product\n"
    )
    assert scan_open_errors(scan, UsbId(0x0403, 0x6010)) == [
        "Error: can't open device with vid:vid = 0x0403:0x6010. Error code -3 Access denied"
    ]
    assert scan_open_errors(scan, UsbId(0x09FB, 0x6001)) == []


def test_an_output_with_no_header_or_probe_gives_no_probe():
    assert parse_scan_usb("") == []
    assert parse_scan_usb("Error: can't open device\n") == []
    assert parse_scan_usb("Bus device vid:pid       probe_type manufacturer serial product\n") == []


def test_a_detect_reports_the_device_with_its_model():
    detection = parse_detect(REAL_DETECT)
    assert detection.readable
    (device,) = detection.devices
    assert device == ChainDevice(0, 0x3631093, "xilinx", "artix a7 100t", "xc7a100")


def test_a_detect_of_a_chain_reports_each_device_and_one_without_a_model():
    detection = parse_detect(detect_text("arm", "xc7a35", "xc7a100"))
    assert [(d.index, d.model) for d in detection.devices] == [
        (0, None),
        (1, "xc7a35"),
        (2, "xc7a100"),
    ]
    assert detection.devices[0].idcode == 0x4BA00477


def test_a_detect_that_failed_is_unreadable_and_says_why():
    detection = parse_detect(REAL_DETECT_FAILURE)
    assert not detection.readable
    assert detection.reason == "JTAG init failed with: unable to open ftdi device"


def test_a_detect_that_reports_nothing_is_unreadable_with_no_reason():
    detection = parse_detect(REAL_DETECT_SILENT)
    assert not detection.readable and detection.reason == ""
    assert parse_detect("") == Detection()


def test_the_readers_ignore_the_colors_of_a_terminal():
    colored = "\x1b[32m" + REAL_DETECT.replace("\n", "\x1b[0m\n")
    assert parse_detect(colored).devices[0].model == "xc7a100"


# ----------------------------------------------------------------------------- the matching

#: (model in the loader's part.hpp, part, whether it matches), by family. A test for each.
MATCHES = [
    # Xilinx 7 series: the model has no package and no `t`; a longer number is another device
    ("xc7a100", "xc7a100tcsg324-1", True),
    ("xc7a100", "XC7A100T-1CSG324C", True),
    ("xc7a35", "xc7a35tcpg236-1", True),
    ("xc7a35", "xc7a350t", False),
    ("xc7a35", "xc7a100tcsg324-1", False),
    ("xc7a35", "zzxc7a35t", False),  # the model is a prefix of the part, not any part of it
    ("xc7a100", "xc7a1000", False),
    ("xc7a15", "xc7a15tcpg236-1", True),
    ("xc7a12t", "xc7a12tcsg325-1", True),
    ("xc7a200", "xc7a200tfbg676-2", True),
    ("xc7k325t", "xc7k325tffg676-2", True),
    ("xc7k70t", "xc7k325tffg676-2", False),
    ("xc7z020", "xc7z020clg400-1", True),
    ("xc7z010", "xc7z0100", False),
    ("xc7s25", "xc7s25csga324-1", True),
    ("xc7s6", "xc7s60", False),
    # the model of the Spartan-7 15 names a package, which is no reason to refuse the others
    ("xc7s15ftgb196-1", "xc7s15cpga196-1", True),
    ("xc7s15ftgb196-1", "xc7s150", False),
    # Xilinx UltraScale and UltraScale+
    ("xcku040", "xcku040-ffva1156-2-e", True),
    ("xcku040", "xcku060-ffva1156-2-e", False),
    ("xcku5p", "xcku5p-ffvb676-2-e", True),
    ("xcku5p", "xcku15p-ffva1760-2-e", False),
    ("xcvu9p", "xcvu9p-flga2104-2l-e", True),
    ("xck26", "xck26-sfvc784-2lv-c", True),
    # Zynq UltraScale+: one IDCODE for CG, EG and EV
    ("xczu4cg/eg/ev", "xczu4ev-sfvc784-1-e", True),
    ("xczu4cg/eg/ev", "xczu4cg-sfvc784-1-e", True),
    ("xczu4cg/eg/ev", "xczu5ev-sfvc784-1-e", False),
    ("xczu9eg", "xczu9eg-ffvb1156-2-e", True),
    # Lattice ECP5
    ("LFE5U-85", "LFE5U-85F-6BG381C", True),
    ("LFE5U-25", "LFE5U-25F-6BG381C", True),
    ("LFE5U-25", "LFE5U-85F-6BG381C", False),
    ("LFE5UM-85", "LFE5UM5G-85F-8BG756C", False),
    ("LFE5UM5G-85", "LFE5UM5G-85F-8BG756C", True),
    ("LFE5U-12", "LFE5U-12F-6BG256C", True),
    # Lattice MachXO2: the speed grade follows a dash, and is no part of the device
    ("LCMXO2-7000ZE/LCMXO2-7000HE", "LCMXO2-7000HE-4TG144C", True),
    ("LCMXO2-7000HC/LCMXO2-4000UHC", "LCMXO2-7000HC-4TG144C", True),
    ("LCMXO2-1200HC/LCMXO2-640UHC", "LCMXO2-1200HC-4TG100C", True),
    ("LCMXO2-1200ZE", "LCMXO2-1200HC-4TG100C", False),
    ("LCMXO2-256HC", "LCMXO2-2560HC", False),
    # Lattice Nexus
    ("LIFCL-40", "LIFCL-40-9BG400C", True),
    ("LIFCL-17", "LIFCL-40-9BG400C", False),
    ("LFD2NX-40", "LFD2NX-40-7BG256C", True),
    ("LFCPNX-100", "LFCPNX-100-9LFG672C", True),
    # Gowin: the model and the part share no prefix; the series and the capacity decide
    ("GW1NR-9", "GW1NR-LV9QN88PC6/I5", True),
    ("GW1N(R)-9C", "GW1NR-LV9QN88PC6/I5", True),
    ("GW1N(R)-9C", "GW1N-LV9LQ144C6/I5", True),
    ("GW1N-1", "GW1N-LV1QN48C6/I5", True),
    ("GW1N-4", "GW1N-UV4LQ144C6/I5", True),
    ("GW1N-4", "GW1N-LV1QN48C6/I5", False),
    ("GW1NZ-1", "GW1N-LV1QN48C6/I5", False),
    ("GW1NZ-1", "GW1NZ-LV1QN48C6/I5", True),
    ("GW1NSR-4C", "GW1NSR-LV4CQN48PC6/I5", True),
    ("GW2A(R)-18(C)", "GW2AR-LV18QN88C8/I7", True),
    ("GW2A(R)-18(C)", "GW2A-LV18PG256C8/I7", True),
    ("GW2A(R)-18(C)", "GW2A-LV55PG484C8/I7", False),
    ("GW2A-55", "GW2A-LV55PG484C8/I7", True),
    ("GW5AST-138", "GW5AST-LV138FPG676AC1/I0", True),
    ("GW5AT-138", "GW5AST-LV138FPG676AC1/I0", False),
    ("GW5A-25", "GW5A-LV25MG121NC1/I0", True),
    ("GW1N-4", "xc7a100tcsg324-1", False),
    # Intel: Cyclone IV E, III, 10 LP, MAX II, MAX 10 are spelled as the ordering code is
    ("EP3C16/EP4CE15/10CL016", "EP4CE15F23C8", True),
    ("EP3C25/EP4CE22/10CL025", "EP4CE22F17C6N", True),
    ("EP3C25/EP4CE22/10CL025", "10CL025YU256C8G", True),
    ("EP3C25/EP4CE22/10CL025", "EP4CE115F29C7", False),
    ("EP3C120/EP4CE115/10CL120", "EP4CE115F29C7", True),
    ("EP3C5/EP3C10/EP4CE6/EP4CE10", "EP4CE6E22C8", True),
    ("EP3C5/EP3C10/EP4CE6/EP4CE10", "EP4CE115F29C7", False),
    ("EP4CGX150", "EP4CGX150DF27C7", True),
    ("EPM240T100C5N", "EPM240T100C5N", True),
    # the model is a whole ordering code, which no other package or grade of the device spells
    ("EPM240T100C5N", "EPM240T100I5N", True),
    ("EPM240T100C5N", "EPM240M100C4N", True),
    ("EPM240T100C5N", "EPM2210F324C3", False),
    ("EPM240T100C5N", "EPM2400F324C3", False),
    ("10M50D", "10M50DAF484C7G", True),
    ("10M50S", "10M50DAF484C7G", False),
    ("10M08S", "10M08SAU169C8G", True),
    # Intel Cyclone V: the `*` stands for the letters between the series and the density
    ("5CGX*3", "5CGXFC3B6F23C6", True),
    ("5CGX*3", "5CGXFC5C6F27C7N", False),
    ("5CGT*5/5CGX*5", "5CGXFC5C6F27C7N", True),
    ("5CGT*5/5CGX*5", "5CGTFD5C5F27C7", True),
    ("5CE*A4", "5CEBA4F23C7", True),
    ("5CE*A4", "5CEBA2F17C8", False),
    ("5CSE*A5/5CST*5/5CSX*5", "5CSEMA5F31C6", True),
    ("5CSE*A5/5CST*5/5CSX*5", "5CSTFD5D5F31I7N", True),
    ("5CSE*A5/5CST*5/5CSX*5", "5CSEMA4U23C6N", False),
    # Intel Agilex
    ("A3CY100", "A3CY100BM16AE7S", True),
    ("A5EC008B", "A5EC008BM16AE6S", True),
    # nothing else matches an empty model
    ("", "xc7a100tcsg324-1", False),
]


@pytest.mark.parametrize("model,part,expected", MATCHES)
def test_a_detected_model_matches_the_parts_that_have_its_name(model, part, expected):
    assert model_matches_part(model, part) is expected
    assert part_is_matchable(part) or not expected  # every part that matches is one it handles


@pytest.mark.parametrize(
    "part",
    [
        "xc7a100tcsg324-1",
        "xcku040-ffva1156-2-e",
        "xcvu9p-flga2104-2L-e",
        "xczu7ev-ffvc1156-2-e",
        "xck26-sfvc784-2LV-c",
        "LFE5U-85F-6BG381C",
        "LFE5UM5G-45F-8BG381C",
        "LCMXO2-7000HC-4TG144C",
        "LIFCL-40-9BG400C",
        "GW1NR-LV9QN88PC6/I5",
        "GW2A-LV18PG256C8/I7",
        "GW1NR-9C",
        "EP4CE22F17C6N",
        "10M50DAF484C7G",
        "10CL025YU256C8G",
        "5CSEMA5F31C6",
        "A3CY100BM16AE7S",
    ],
)
def test_the_parts_of_the_supported_families_can_be_matched(part):
    assert part_is_matchable(part)


@pytest.mark.parametrize(
    "part,why",
    [
        ("ICE40UP5K-SG48", "iCE40 has no JTAG"),
        ("iCE40HX8K-CT256", "iCE40 has no JTAG"),
        ("xc6slx25tcsg324-2", "Spartan-6: `xc6slx25` is a prefix of the T part"),
        ("xc6slx9-2tqg144", "Spartan-6: the package follows the number"),
        ("xc3s200-4tq144", "Spartan-3"),
        ("LCMXO3LF-6900C-5BG256C", "MachXO3: its models name another package spelling"),
        ("10AX115S2F45I1SG", "Arria 10: the model is `GX1150`"),
        ("5SGSMD5K2F40C2N", "Stratix V: the model is `5SGSD5`"),
        ("T20F256", "Efinix"),
        ("", "no part"),
    ],
)
def test_a_part_whose_models_cannot_be_matched_reliably_is_not_guessed(part, why):
    assert not part_is_matchable(part), why


def test_the_two_boards_of_the_report_are_told_apart_by_their_models():
    assert model_matches_part("xc7a100", ARTY_PART)
    assert not model_matches_part("xc7a35", ARTY_PART)
    assert model_matches_part("xc7a35", "xc7a35tcpg236-1")
    assert not model_matches_part("xc7a100", "xc7a35tcpg236-1")


# ------------------------------------------------------------------------------ the decision


def _probe(bus, device, serial, vid=0x0403, pid=0x6010):
    return UsbProbe(bus, device, UsbId(vid, pid), "FTDI2232", "Digilent", serial, "Digilent USB")


def _reading(probe, *models, reason=""):
    devices = tuple(ChainDevice(i, 1, "x", "f", m) for i, m in enumerate(models))
    return Reading(probe, Detection(devices, reason))


def test_the_one_probe_that_matches_is_chosen_and_the_others_are_kept():
    arty, basys = _probe(1, 2, ARTY), _probe(1, 3, BASYS)
    readings = [_reading(basys, "xc7a35"), _reading(arty, "xc7a100")]
    decision = choose_probe(readings, ARTY_PART)
    assert isinstance(decision, Chosen)
    assert decision.reading.probe is arty and decision.device.model == "xc7a100"
    assert [r.probe for r in decision.others] == [basys]


def test_a_probe_that_could_not_be_read_stops_the_choice_of_the_one_that_matches():
    """The unreadable probe is unknown, not excluded: it may be a second board with the same FPGA
    (an Arty-35T and a Basys-3 are both an xc7a35), so the readable match is not known to be the
    one intended."""
    arty, basys = _probe(1, 2, ARTY), _probe(1, 3, BASYS)
    decision = choose_probe([_reading(basys, reason="busy"), _reading(arty, "xc7a100")], ARTY_PART)
    assert isinstance(decision, Refusal)
    assert [r.probe for r in decision.matching] == [arty]
    assert [r.probe for r in decision.readings if not r.detection.readable] == [basys]


def test_a_probe_that_was_read_and_shows_another_device_is_excluded():
    arty, basys = _probe(1, 2, ARTY), _probe(1, 3, BASYS)
    decision = choose_probe([_reading(basys, "xc7a35"), _reading(arty, "xc7a100")], ARTY_PART)
    assert isinstance(decision, Chosen)
    # a chain with no FPGA the loader knows was read too: it cannot be programmed, so it is excluded
    decision = choose_probe([_reading(basys, None), _reading(arty, "xc7a100")], ARTY_PART)
    assert isinstance(decision, Chosen) and decision.reading.probe is arty


def test_no_match_and_several_matches_are_refused():
    first, second = _probe(1, 2, "A"), _probe(1, 3, "B")
    none = choose_probe([_reading(first, "xc7a35"), _reading(second, "xc7a35")], ARTY_PART)
    assert isinstance(none, Refusal) and none.matching == ()
    both = choose_probe([_reading(first, "xc7a100"), _reading(second, "xc7a100")], ARTY_PART)
    assert isinstance(both, Refusal) and [r.probe for r in both.matching] == [first, second]
    unreadable = choose_probe([_reading(first), _reading(second)], ARTY_PART)
    assert isinstance(unreadable, Refusal) and unreadable.matching == ()


def test_a_device_without_a_model_never_matches():
    first, second = _probe(1, 2, "A"), _probe(1, 3, "B")
    decision = choose_probe([_reading(first, None), _reading(second, "xc7a100")], ARTY_PART)
    assert isinstance(decision, Chosen) and decision.reading.probe is second


def test_the_device_at_the_index_in_the_chain_decides_when_one_is_given():
    first, second = _probe(1, 2, "A"), _probe(1, 3, "B")
    readings = [_reading(first, "xc7a100", "xc7a35"), _reading(second, "xc7a35", "xc7a35")]
    # no index: the loader takes the FPGA of the chain, so any device of it can be the one
    assert isinstance(choose_probe(readings, "xc7a35tcpg236-1"), Refusal)  # both chains have it
    decision = choose_probe(readings, ARTY_PART)
    assert isinstance(decision, Chosen) and decision.reading.probe is first
    # with an index, only that device: index 1 of the first probe is an xc7a35
    assert isinstance(choose_probe(readings, ARTY_PART, index_chain=1), Refusal)
    decision = choose_probe(readings, ARTY_PART, index_chain=0)
    assert isinstance(decision, Chosen) and decision.reading.probe is first
    decision = choose_probe(readings, "xc7a35tcpg236-1", index_chain=0)
    assert isinstance(decision, Chosen) and decision.reading.probe is second


def test_a_probe_is_named_by_its_serial_and_only_probes_that_have_one_are_chosen_among():
    unique, other = _probe(1, 2, "A"), _probe(1, 6, "B")
    shared_one, shared_two = _probe(1, 3, "S"), _probe(1, 4, "S")
    no_serial = _probe(1, 5, None)
    assert probe_selector(unique) == ["--ftdi-serial", "A"]
    assert probes_without_a_name([unique, other]) == []
    # `--busdev-num` is never used: it works only with libftdi 1.4 or newer
    probes = [unique, shared_one, shared_two, no_serial]
    assert probes_without_a_name(probes) == [shared_one, shared_two, no_serial]
    with pytest.raises(ValueError, match="no serial number"):
        probe_selector(no_serial)


@pytest.mark.parametrize(
    "vid,pid,selectable",
    [
        (0x0403, 0x6010, True),  # FTDI: by serial number
        (0x0403, 0x6015, True),
        # `--busdev-num` is the only option that could name these: the CH347 and the ESP32-S3
        # take it through libusb_ll, which compares the bus with the device number
        (0x1A86, 0x55DD, False),
        (0x303A, 0x1001, False),
        (0x09FB, 0x6001, False),  # USB-Blaster: opens the first device, ignores both options
        (0x0D28, 0x0204, False),  # CMSIS-DAP
        (0x1366, 0x0105, False),  # J-Link
        (0x1209, 0xC0CA, False),  # DirtyJTAG
        (0x03FD, 0x0013, False),  # Xilinx platform cable
    ],
)
def test_xeda_names_only_the_probes_of_ftdi_chips(vid, pid, selectable):
    assert selectable_usb_id(UsbId(vid, pid)) is selectable


@pytest.mark.parametrize(
    "arguments,named",
    [
        (["--ftdi-serial", "X"], True),
        (["--ftdi-serial=X"], True),
        (["--cable-index", "1"], True),
        (["--cable-index=1"], True),
        (["--busdev-num", "1:2"], True),
        (["--busdev-num=1:2"], True),
        (["--vid", "0x403"], True),
        (["--pid=0x6010"], True),
        # v1.2.0's own spelling of the serial option, and the probe by its tty
        (["--usb-serial-num", "X"], True),
        (["--usb-serial-num=X"], True),
        (["--device", "/dev/ttyUSB0"], True),
        (["--device=/dev/ttyUSB0"], True),
        (["-d", "/dev/ttyUSB0"], True),
        (["-dx"], False),  # `-d` is read as a token of its own
        (["--debug"], False),
        (["-f"], False),
        (["--scan-usb"], False),
        (["--ftdi-serialx"], False),
        (["--freq", "6000000"], False),
        ([], False),
    ],
)
def test_the_arguments_that_name_a_probe(arguments, named):
    assert names_a_probe(arguments) is named


@pytest.mark.parametrize(
    "arguments,changed",
    [
        (["-c", "ft2232"], True),
        (["--cable", "ft2232"], True),
        (["--cable=ft2232"], True),
        (["-b", "arty_a7_35t"], True),
        (["--board", "arty_a7_35t"], True),
        (["--board=arty_a7_35t"], True),
        (["--freq", "6000000"], False),
        (["--cable-index", "1"], False),
        (["--boards"], False),
        ([], False),
    ],
)
def test_the_arguments_that_give_the_loader_another_cable(arguments, changed):
    assert changes_the_cable(arguments) is changed


def test_the_refusal_lists_every_probe_and_names_the_settings():
    first, second = _probe(1, 2, ARTY), _probe(1, 3, BASYS)
    refusal = Refusal((_reading(first, "xc7a35"), _reading(second, reason="busy")), ())
    sentences, lines = refusal_text(
        refusal, cable="digilent", usb_id=UsbId(0x0403, 0x6010), part=ARTY_PART
    )
    assert sentences[0] == (
        "openFPGALoader has 2 probes on the cable digilent (USB 0403:6010), and none of them "
        f"has an FPGA that matches the part {ARTY_PART}."
    )
    assert lines == (
        f"serial {ARTY}, bus 1 device 2: xc7a35 (`usb_serial_num: {ARTY}`)",
        f"serial {BASYS}, bus 1 device 3: could not read its device (busy) "
        f"(`usb_serial_num: {BASYS}`)",
    )
    text = " ".join(sentences)
    assert "`usb_serial_num`" in text and "`cable_index`" in text
    assert "could not read the device of some probes" in text
    assert "A named probe is programmed without reading the others." in text
    assert sentences[-1] == "Nothing was programmed."


def test_the_refusal_for_one_match_beside_an_unreadable_probe_says_why():
    arty, basys, third = _probe(1, 2, ARTY), _probe(1, 3, BASYS), _probe(1, 7, "C")
    readings = (_reading(arty, "xc7a100"), _reading(basys, reason="busy"), _reading(third))
    sentences, lines = refusal_text(
        Refusal(readings, readings[:1]),
        cable="digilent",
        usb_id=UsbId(0x0403, 0x6010),
        part=ARTY_PART,
    )
    assert sentences[0] == (
        "openFPGALoader has 3 probes on the cable digilent (USB 0403:6010), and one of them has "
        f"an FPGA that matches the part {ARTY_PART}, but Xeda could not read the device of 2 "
        "other probes. That may be another board with the same FPGA, so Xeda cannot tell which "
        "one to program."
    )
    assert lines[0] == f"serial {ARTY}, bus 1 device 2: xc7a100 (`usb_serial_num: {ARTY}`)"
    assert lines[1].endswith(f"(`usb_serial_num: {BASYS}`)")


# ------------------------------------------------------------------- the flow, on the fake


def _two_boards(**changes) -> World:
    """Two boards on one kind of probe: an Arty A7-100T and a Basys 3, listed with the Basys 3
    first (the loader opens the first one)."""
    world = World(
        probes=[Usb(1, 3, BASYS), Usb(1, 2, ARTY)],
        detect={BASYS: (detect_text("xc7a35"), 0), ARTY: (detect_text("xc7a100"), 0)},
    )
    for name, value in changes.items():
        setattr(world, name, value)
    return world


def _argv(tmp_path) -> list[str]:
    (call,) = _calls(tmp_path, "openfpgaloader")
    return call["argv"]


def _arty_command(tmp_path, *rest) -> list[str]:
    return ["--bitstream", str(tmp_path / "design/given.bit"), "--board", "arty_a7_100t", *rest]


def _start(tmp_path, monkeypatch, world, settings, caplog=None):
    """Launch the flow on a prebuilt bitstream, on the machine of `world`; return the flow and
    the file that records the queries."""
    if caplog is not None:
        caplog.set_level(logging.INFO, logger=LOGGER)
    record = world.install(monkeypatch, tmp_path)
    flow = _runner(tmp_path).run("openfpgaloader", _prebuilt(tmp_path), flow_settings=settings)
    return flow, record


def test_two_probes_one_matches_the_flow_programs_that_one(
    tmp_path, fake_loader, monkeypatch, caplog
):
    """openFPGALoader opens the first Digilent probe, here the Basys 3, for an Arty board. The flow
    chooses the probe whose FPGA matches the board's part."""
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_STDOUT", "the loader's own output\n")
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"}, caplog)
    assert flow.succeeded
    assert _argv(tmp_path) == _arty_command(tmp_path, "--ftdi-serial", ARTY)
    assert queries(record) == [
        ["--list-boards"],
        ["--list-cables"],
        ["--scan-usb"],
        ["--board", "arty_a7_100t", "--ftdi-serial", BASYS, "--detect"],
        ["--board", "arty_a7_100t", "--ftdi-serial", ARTY, "--detect"],
    ]
    # the log of the run is the programming call's alone, which the verdict reads
    log = tmp_path / "run/top/openfpgaloader/openfpgaloader.log"
    assert log.read_text() == "the loader's own output\n"
    # nothing of the queries is left in the run directory
    assert not list((tmp_path / "run").rglob("loader_*"))
    (message,) = [r.getMessage() for r in caplog.records if "Programming through" in r.getMessage()]
    assert message == (
        f"Programming through the probe with serial {ARTY}, bus 1 device 2 on the cable "
        "digilent (USB 0403:6010): its FPGA is xc7a100 (artix a7 100t), which matches the part "
        f"{ARTY_PART}. The other probes: serial {BASYS}, bus 1 device 3: xc7a35."
    )
    assert [r.levelno for r in caplog.records if r.getMessage() == message] == [logging.INFO]


def test_the_probe_order_on_the_bus_does_not_change_the_choice(tmp_path, fake_loader, monkeypatch):
    world = _two_boards(probes=[Usb(1, 2, ARTY), Usb(1, 3, BASYS)])
    _start(tmp_path, monkeypatch, world, {"board": "basys_3"})
    assert _argv(tmp_path)[-2:] == ["--ftdi-serial", BASYS]


def test_one_probe_changes_nothing_and_reads_no_device(tmp_path, fake_loader, monkeypatch):
    world = World(probes=[Usb(1, 2, ARTY)], detect={ARTY: (detect_text("xc7a100"), 0)})
    flow, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert queries(record) == [["--list-boards"], ["--list-cables"], ["--scan-usb"]]
    assert flow.probe_note == PROBE_NOTE_SINGLE


def test_no_probe_changes_nothing(tmp_path, fake_loader, monkeypatch):
    flow, record = _start(tmp_path, monkeypatch, World(), {"board": "arty_a7_100t"})
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert ["--scan-usb"] in queries(record) and len(queries(record)) == 3


def test_probes_of_another_usb_id_are_not_counted(tmp_path, fake_loader, monkeypatch):
    """A FT4232H debugger and a ULX3S beside one Digilent board: one probe fits the cable."""
    world = World(
        probes=[
            Usb(1, 2, ARTY),
            Usb(1, 3, "FT00000001", pid=0x6015, probe_type="ft231X"),
            Usb(1, 8, "XC00000001", pid=0x6011, probe_type="ft4232", manufacturer="Xilinx"),
        ]
    )
    flow, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert _argv(tmp_path) == _arty_command(tmp_path)
    assert not any("--detect" in q for q in queries(record))


def test_a_probe_without_a_serial_is_not_chosen_among(tmp_path, fake_loader, monkeypatch, caplog):
    """Only `--busdev-num` could name it, and that works with libftdi 1.4 or newer only: Xeda
    does not detect, warns, and leaves the command line as it was."""
    world = World(
        probes=[Usb(1, 3, BASYS), Usb(1, 2, None)],
        detect={BASYS: (detect_text("xc7a35"), 0), "1:2": (detect_text("xc7a100"), 0)},
    )
    flow, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"}, caplog)
    assert flow.succeeded
    assert _argv(tmp_path) == _arty_command(tmp_path)
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning == (
        "2 probes fit the cable digilent (USB 0403:6010): "
        f"serial {BASYS}, bus 1 device 3; no serial, bus 1 device 2. Xeda names a probe by "
        "its serial number, and no serial, bus 1 device 2 has no serial number of its own, so "
        "openFPGALoader opens the first one. Name the probe with `cable_index`, or with "
        "`usb_serial_num` if its serial number is unique."
    )


def test_two_probes_with_one_serial_are_not_chosen_among(
    tmp_path, fake_loader, monkeypatch, caplog
):
    world = World(
        probes=[Usb(1, 3, "SAME"), Usb(1, 2, "SAME")],
        detect={"1:3": (detect_text("xc7a35"), 0), "1:2": (detect_text("xc7a100"), 0)},
    )
    flow, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"}, caplog)
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "serial SAME, bus 1 device 3; serial SAME, bus 1 device 2 have no serial" in warning


def test_one_probe_without_a_serial_among_several_stops_the_choice(
    tmp_path, fake_loader, monkeypatch
):
    """Every candidate has to be nameable, or the loader's pick could be the one not named."""
    world = World(
        probes=[Usb(1, 3, BASYS), Usb(1, 2, ARTY), Usb(1, 6, None)],
        detect={BASYS: (detect_text("xc7a35"), 0), ARTY: (detect_text("xc7a100"), 0)},
    )
    _, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert _argv(tmp_path) == _arty_command(tmp_path)
    assert not any("--detect" in q for q in queries(record))


def test_a_detect_that_fails_on_the_other_probe_stops_the_choice_and_lists_the_probes(
    tmp_path, fake_loader, monkeypatch
):
    """The Basys-3 gives no answer: it may be a second board with the Arty's FPGA, so the run
    fails before programming and says how to name the probe."""
    world = _two_boards(detect={ARTY: (detect_text("xc7a100"), 0)})  # the Basys-3: no answer
    with pytest.raises(FlowFatalError) as raised:
        _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert str(raised.value) == (
        "openFPGALoader has 2 probes on the cable digilent (USB 0403:6010), and one of them has "
        f"an FPGA that matches the part {ARTY_PART}, but Xeda could not read the device of 1 "
        "other probe. That may be another board with the same FPGA, so Xeda cannot tell which "
        "one to program.\n"
        "Name the probe to program with the `usb_serial_num` setting (its serial number, listed "
        "below) or the `cable_index` setting. A named probe is programmed without reading the "
        "others.\n"
        "Nothing was programmed.\n"
        "Probes:\n"
        f"    serial {BASYS}, bus 1 device 3: could not read its device (JTAG init failed with: "
        f"unable to open ftdi device) (`usb_serial_num: {BASYS}`)\n"
        f"    serial {ARTY}, bus 1 device 2: xc7a100 (`usb_serial_num: {ARTY}`)"
    )
    assert not _calls(tmp_path, "openfpgaloader")


def test_a_detect_that_exits_with_an_error_is_an_unknown_probe_too(
    tmp_path, fake_loader, monkeypatch
):
    world = _two_boards(
        detect={BASYS: ("unable to open ftdi device\n", 1), ARTY: (detect_text("xc7a100"), 0)}
    )
    with pytest.raises(FlowFatalError, match="could not read the device"):
        _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert not _calls(tmp_path, "openfpgaloader")


def test_a_named_probe_is_programmed_without_reading_the_unreadable_one(
    tmp_path, fake_loader, monkeypatch
):
    """The way out of the refusal: the user names the probe, and nothing is asked."""
    world = _two_boards(detect={ARTY: (detect_text("xc7a100"), 0)})
    flow, record = _start(
        tmp_path, monkeypatch, world, {"board": "arty_a7_100t", "usb_serial_num": ARTY}
    )
    assert flow.succeeded and queries(record) == []
    assert _argv(tmp_path)[-2:] == ["--ftdi-serial", ARTY]


def test_a_detect_that_fails_on_the_matching_probe_fails_the_run_with_the_list(
    tmp_path, fake_loader, monkeypatch
):
    world = _two_boards(detect={BASYS: (detect_text("xc7a35"), 0)})  # the Arty: no answer
    with pytest.raises(FlowFatalError) as raised:
        _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    message = str(raised.value)
    assert "none of them has an FPGA that matches the part xc7a100tcsg324-1" in message
    assert "could not read the device of some probes" in message
    assert f"serial {ARTY}, bus 1 device 2: could not read its device" in message
    assert not _calls(tmp_path, "openfpgaloader")  # nothing was programmed


def test_no_probe_matches_the_run_fails_before_programming(tmp_path, fake_loader, monkeypatch):
    world = _two_boards(
        detect={BASYS: (detect_text("xc7a35"), 0), ARTY: (detect_text("xc7a200"), 0)}
    )
    with pytest.raises(FlowFatalError) as raised:
        _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert str(raised.value) == (
        "openFPGALoader has 2 probes on the cable digilent (USB 0403:6010), and none of them "
        f"has an FPGA that matches the part {ARTY_PART}.\n"
        "Name the probe to program with the `usb_serial_num` setting (its serial number, listed "
        "below) or the `cable_index` setting. A named probe is programmed without reading the "
        "others.\n"
        "Nothing was programmed.\n"
        "Probes:\n"
        f"    serial {BASYS}, bus 1 device 3: xc7a35 (`usb_serial_num: {BASYS}`)\n"
        f"    serial {ARTY}, bus 1 device 2: xc7a200 (`usb_serial_num: {ARTY}`)"
    )
    assert not _calls(tmp_path, "openfpgaloader")
    results = (tmp_path / "run/top/openfpgaloader/results.json").read_text()
    assert '"outcome": "failed"' in results and '"code": "reported_failure"' in results


def test_two_probes_match_the_run_fails_before_programming(tmp_path, fake_loader, monkeypatch):
    world = _two_boards(
        detect={BASYS: (detect_text("xc7a100"), 0), ARTY: (detect_text("xc7a100"), 0)}
    )
    with pytest.raises(FlowFatalError) as raised:
        _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    message = str(raised.value)
    assert message.splitlines()[0] == (
        "openFPGALoader has 2 probes on the cable digilent (USB 0403:6010), and 2 of them have "
        f"an FPGA that matches the part {ARTY_PART}, so Xeda cannot tell which one to program."
    )
    assert f"serial {BASYS}, bus 1 device 3: xc7a100" in message
    assert f"serial {ARTY}, bus 1 device 2: xc7a100" in message
    assert not _calls(tmp_path, "openfpgaloader")


def test_the_cable_setting_chooses_the_probe_without_asking_for_the_boards(
    tmp_path, fake_loader, monkeypatch
):
    flow, record = _start(
        tmp_path, monkeypatch, _two_boards(), {"cable": "digilent", "fpga": ARTY_PART}
    )
    assert flow.succeeded
    assert _argv(tmp_path) == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "digilent",
        "--fpga-part",
        "xc7a100tcsg324",
        "--ftdi-serial",
        ARTY,
    ]
    assert ["--list-boards"] not in queries(record)


def test_the_cable_setting_takes_precedence_over_the_board_s_cable(
    tmp_path, fake_loader, monkeypatch
):
    """`ft2232` and `digilent` both have 0x0403:0x6010 here: the cable the flow passes decides."""
    flow, record = _start(
        tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t", "cable": "ft2232"}
    )
    assert ["--cable", "ft2232", "--ftdi-serial", BASYS, "--detect"] in queries(record)
    assert _argv(tmp_path)[-2:] == ["--ftdi-serial", ARTY]


@pytest.mark.parametrize(
    "settings",
    [
        {"usb_serial_num": BASYS},
        {"cable_index": 1},
        {"extra_args": ["--ftdi-serial", BASYS]},
        {"extra_args": [f"--ftdi-serial={BASYS}"]},
        {"extra_args": ["--cable-index", "0"]},
        {"extra_args": ["--cable-index=0"]},
        {"extra_args": ["--busdev-num", "1:3"]},
        {"extra_args": ["--busdev-num=1:3"]},
        # v1.2.0's own spelling: the flow must not add `--ftdi-serial` beside it, which the
        # loader refuses ("can't be used at the same time")
        {"extra_args": ["--usb-serial-num", BASYS]},
        {"extra_args": [f"--usb-serial-num={BASYS}"]},
        {"extra_args": ["-d", "/dev/ttyUSB0"]},
        {"extra_args": ["--device=/dev/ttyUSB0"]},
    ],
)
def test_a_probe_the_run_names_is_never_second_guessed(
    tmp_path, fake_loader, monkeypatch, settings
):
    """The user's choice stands, even where it looks wrong: no query, the arguments as given."""
    flow, record = _start(
        tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t", **settings}
    )
    assert flow.succeeded
    assert queries(record) == []
    argv = _argv(tmp_path)
    assert argv[:4] == _arty_command(tmp_path)
    assert not {"--ftdi-serial", ARTY} <= set(argv)
    assert flow.probe_note == PROBE_NOTE_NAMED


def test_an_unknown_part_warns_that_the_loader_opens_the_first_probe(
    tmp_path, fake_loader, monkeypatch, caplog
):
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), {"cable": "digilent"}, caplog)
    assert flow.succeeded
    assert _argv(tmp_path) == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "digilent",
    ]
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        "2 probes fit the cable digilent (USB 0403:6010): serial "
        f"{BASYS}, bus 1 device 3; serial {ARTY}, bus 1 device 2. The flow has no part to "
        "compare with their FPGAs, so openFPGALoader opens the first one. Set `usb_serial_num` "
        "to the serial number of the probe to program, or give the part (`fpga.part`, or a "
        "`board` that has one)."
    )


def test_an_unknown_part_with_one_probe_is_silent(tmp_path, fake_loader, monkeypatch, caplog):
    world = World(probes=[Usb(1, 2, ARTY)])
    _start(tmp_path, monkeypatch, world, {"cable": "digilent"}, caplog)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_part_that_cannot_be_matched_warns_and_changes_nothing(
    tmp_path, fake_loader, monkeypatch, caplog
):
    """iCE40 has no JTAG ID code: `--detect` shows no model, so Xeda does not guess."""
    world = _two_boards()
    flow, record = _start(
        tmp_path, monkeypatch, world, {"cable": "digilent", "fpga": "iCE40UP5K-SG48"}, caplog
    )
    assert flow.succeeded
    assert "--ftdi-serial" not in _argv(tmp_path)
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Xeda cannot match the part iCE40UP5K-SG48" in warning
    assert "so openFPGALoader opens the first one" in warning and "`usb_serial_num`" in warning


def test_a_board_whose_cable_the_loader_does_not_know_gets_no_selection(
    tmp_path, fake_loader, monkeypatch
):
    """`acornCle215` has the cable `Undefined` in the loader's list: nothing to choose among."""
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text(
        '[MY_BOARD]\nopenfpgaloader_board = "acornCle215"\nfpga.part = "xc7a200tsbg484-1"\n'
    )
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), settings)
    assert flow.succeeded
    assert queries(record) == [["--list-boards"]]


def test_a_board_the_loader_does_not_list_gets_no_selection(tmp_path, fake_loader, monkeypatch):
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text(
        '[MY_BOARD]\nopenfpgaloader_board = "no_such_board"\nfpga.part = "xc7a200tsbg484-1"\n'
    )
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), settings)
    assert flow.succeeded and queries(record) == [["--list-boards"]]


def test_a_cable_with_no_usb_id_gets_no_selection(tmp_path, fake_loader, monkeypatch):
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), {"cable": "dfu", "fpga": ARTY_PART})
    assert flow.succeeded
    assert queries(record) == [["--list-cables"], ["--scan-usb"]]


def test_no_cable_and_no_board_name_asks_nothing(tmp_path, fake_loader, monkeypatch):
    flow, record = _start(tmp_path, monkeypatch, _two_boards(), {"fpga": ARTY_PART})
    assert flow.succeeded and queries(record) == []


def test_the_index_in_the_chain_is_the_device_that_decides(tmp_path, fake_loader, monkeypatch):
    """Both chains hold an xc7a100, but only the Arty's is at index 1, where the loader looks."""
    world = World(
        probes=[Usb(1, 3, BASYS), Usb(1, 2, ARTY)],
        detect={
            BASYS: (detect_text("xc7a100", "xc7a35"), 0),
            ARTY: (detect_text("arm", "xc7a100"), 0),
        },
    )
    settings = {"board": "arty_a7_100t", "index_chain": 1}
    flow, _ = _start(tmp_path, monkeypatch, world, settings)
    assert flow.succeeded
    assert _argv(tmp_path) == _arty_command(tmp_path, "--index-chain", "1", "--ftdi-serial", ARTY)


def test_a_query_that_cannot_run_is_no_reason_to_stop(tmp_path, fake_loader, monkeypatch, caplog):
    """When the loader cannot be asked (not found, over its time limit), the flow still programs
    and the loader chooses the probe."""
    original = OpenfpgaloaderTool.probe_stdout

    def refuse(self, *args, **kwargs):
        if args and args[0] in ("--list-cables", "--scan-usb", "--list-boards", "--cable"):
            raise ToolException("openFPGALoader ran past its time limit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(OpenfpgaloaderTool, "probe_stdout", refuse)
    caplog.set_level(logging.INFO, logger=LOGGER)
    flow, _ = _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"})
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert any("Could not ask openFPGALoader" in r.getMessage() for r in caplog.records)


def test_the_queries_have_a_time_limit(tmp_path, fake_loader, monkeypatch):
    seen = []
    original = OpenfpgaloaderTool.probe_stdout

    def watch(self, *args, **kwargs):
        if args and args[0].startswith("--") and args[0] != "--Version":
            seen.append((args, kwargs.get("timeout")))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(OpenfpgaloaderTool, "probe_stdout", watch)
    _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"})
    assert len(seen) == 5 and all(limit and 0 < limit <= 120 for _, limit in seen)


def test_planning_a_dry_run_asks_the_loader_nothing(tmp_path, fake_loader, monkeypatch):
    record = _two_boards().install(monkeypatch, tmp_path)
    plan = _runner(tmp_path).plan(
        Openfpgaloader, _prebuilt(tmp_path), flow_settings={"board": "arty_a7_100t"}
    )
    assert [n.name for n in plan.nodes] == ["openfpgaloader"]
    assert queries(record) == []


def test_the_queries_start_in_a_scratch_directory_and_leave_the_run_directory_alone(
    tmp_path, fake_loader, monkeypatch
):
    """The scan and the detects run outside the run directory (`Tool.probe_stdout`), so the files
    a run directory holds are the programming call's alone."""
    _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"})
    run_directory = tmp_path / "run/top/openfpgaloader"
    names = {p.name for p in run_directory.iterdir()}
    assert "openfpgaloader.log" in names
    assert not [n for n in names if "scan" in n or "detect" in n or "world" in n]
    assert len(_calls(tmp_path, "openfpgaloader")) == 1


ULX3S_PART = "LFE5U-85F-6BG381C"


def _ft231x(serial, device):
    return Usb(1, device, serial, pid=0x6015, probe_type="ft231X", manufacturer="FER-RADIONA-EMARD")


def test_a_bit_bang_board_is_read_through_its_board_not_through_its_cable(
    tmp_path, fake_loader, monkeypatch
):
    """The ULX3S is `ft231X`, a bit-bang cable whose pins only the board gives. With a second
    0403:6015 probe (here another ULX3S, with a 25F) beside it, the flow reads each probe with
    `--board ulx3s`, finds the board's FPGA on one, and does not refuse."""
    world = World(
        probes=[_ft231x("FT00000002", 7), _ft231x("FT00000001", 3)],
        detect={
            "FT00000001": (detect_text("ecp5-85"), 0),
            "FT00000002": (detect_text("ecp5-25"), 0),
        },
    )
    flow, record = _start(tmp_path, monkeypatch, world, {"board": "ulx3s_85f"})
    assert flow.succeeded
    assert _argv(tmp_path) == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--board",
        "ulx3s",
        "--ftdi-serial",
        "FT00000001",
    ]
    detects = [q for q in queries(record) if "--detect" in q]
    assert detects == [
        ["--board", "ulx3s", "--ftdi-serial", "FT00000002", "--detect"],
        ["--board", "ulx3s", "--ftdi-serial", "FT00000001", "--detect"],
    ]
    assert ULX3S_PART  # the board's part, which the choice matched against `LFE5U-85`


def test_a_uart_adapter_beside_a_ulx3s_is_unknown_and_the_run_asks_for_the_serial(
    tmp_path, fake_loader, monkeypatch
):
    """An FT231X UART adapter has no JTAG device, so its detect prints none: the probe is
    unknown, not excluded, and the run fails before programming with the `usb_serial_num` that
    names the ULX3S."""
    world = World(
        probes=[_ft231x("FT00000002", 7), _ft231x("FT00000001", 3)],
        detect={"FT00000001": (detect_text("ecp5-85"), 0)},
    )
    with pytest.raises(FlowFatalError) as raised:
        _start(tmp_path, monkeypatch, world, {"board": "ulx3s_85f"})
    assert "(`usb_serial_num: FT00000001`)" in str(raised.value)
    assert "(`usb_serial_num: FT00000002`)" in str(raised.value)
    assert not _calls(tmp_path, "openfpgaloader")
    flow, _ = _start(
        tmp_path, monkeypatch, world, {"board": "ulx3s_85f", "usb_serial_num": "FT00000001"}
    )
    assert flow.succeeded and _argv(tmp_path)[-2:] == ["--ftdi-serial", "FT00000001"]


def test_a_bit_bang_cable_without_its_board_is_not_read(tmp_path, fake_loader, monkeypatch, caplog):
    """`cable: ft231X` has no pins: `--detect` would read nothing and drive the pins of an
    unrelated FTDI chip. Xeda says it cannot tell, and the command line has no probe option."""
    world = World(probes=[_ft231x("FT00000002", 7), _ft231x("FT00000001", 3)])
    flow, record = _start(
        tmp_path, monkeypatch, world, {"cable": "ft231X", "fpga": ULX3S_PART}, caplog
    )
    assert flow.succeeded
    assert _argv(tmp_path) == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "ft231X",
        "--fpga-part",
        ULX3S_PART,
    ]
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "A bit-bang cable has its pins only from a board" in warning
    assert "`usb_serial_num`" in warning


def test_a_cable_whose_probes_cannot_be_named_is_not_chosen_among(
    tmp_path, fake_loader, monkeypatch, caplog
):
    """A USB-Blaster ignores `--ftdi-serial` and `--busdev-num`: a choice would name a probe
    that the loader does not open. The CH347 takes `--busdev-num` only through a filter that
    swaps the bus and the device number, so it is not chosen among either."""
    world = World(
        probes=[
            Usb(1, 2, "A", vid=0x09FB, pid=0x6001, probe_type="usb-blaster", manufacturer="Altera"),
            Usb(1, 3, "B", vid=0x09FB, pid=0x6001, probe_type="usb-blaster", manufacturer="Altera"),
        ]
    )
    flow, record = _start(
        tmp_path, monkeypatch, world, {"cable": "usb-blaster", "fpga": ARTY_PART}, caplog
    )
    assert flow.succeeded and "--busdev-num" not in _argv(tmp_path)
    assert not any("--detect" in q for q in queries(record))
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Xeda names only FTDI probes, by serial number" in warning
    assert warning.endswith("so openFPGALoader opens the first one.")


def test_a_ch347_cable_is_not_chosen_among(tmp_path, fake_loader, monkeypatch, caplog):
    world = World(
        probes=[
            Usb(1, 2, "A", vid=0x1A86, pid=0x55DD, probe_type="ch347_jtag", manufacturer="WCH"),
            Usb(1, 3, "B", vid=0x1A86, pid=0x55DD, probe_type="ch347_jtag", manufacturer="WCH"),
        ]
    )
    flow, record = _start(
        tmp_path, monkeypatch, world, {"cable": "ch347_jtag", "fpga": ARTY_PART}, caplog
    )
    assert flow.succeeded and "--busdev-num" not in _argv(tmp_path)
    assert not any("--detect" in q for q in queries(record))
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize(
    "extra",
    [
        ["-c", "ft2232"],
        ["--cable", "ft2232"],
        ["--cable=ft2232"],
        ["-b", "basys3"],
        ["--board=basys3"],
    ],
)
def test_a_cable_or_board_in_extra_args_replaces_the_cable_the_flow_knows(
    tmp_path, fake_loader, monkeypatch, extra
):
    """The loader lets them override the flow's cable, whose USB id is then not known."""
    flow, record = _start(
        tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t", "extra_args": extra}
    )
    assert flow.succeeded and queries(record) == []
    assert _argv(tmp_path)[-len(extra) :] == extra and "--ftdi-serial" not in _argv(tmp_path)


def test_the_choice_comes_before_extra_args(tmp_path, fake_loader, monkeypatch):
    """The user's arguments go last: they can still override what comes first."""
    flow, _ = _start(
        tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t", "extra_args": ["--verbose"]}
    )
    assert _argv(tmp_path) == _arty_command(tmp_path, "--ftdi-serial", ARTY, "--verbose")


def test_a_scan_with_no_table_warns_and_the_loader_chooses(
    tmp_path, fake_loader, monkeypatch, caplog
):
    world = World(scan="Error: failed to allocate buffer\n")
    flow, _ = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"}, caplog)
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning == (
        "openFPGALoader --scan-usb printed no table that Xeda can read (Error: failed to "
        "allocate buffer). The loader chooses the probe."
    )


def test_a_loader_of_the_oldest_supported_release_gets_the_choice_too(
    tmp_path, fake_loader, monkeypatch
):
    """v0.13.1 prints `probe type` in its header: the protection must not vanish silently."""
    world = _two_boards(
        scan=_old_scan(
            [
                (1, 3, "0x0403:0x6010", "FTDI2232", "Digilent", BASYS, "Digilent USB Device"),
                (1, 2, "0x0403:0x6010", "FTDI2232", "Digilent", ARTY, "Digilent USB Device"),
            ]
        )
    )
    _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"})
    assert _argv(tmp_path)[-2:] == ["--ftdi-serial", ARTY]


@pytest.mark.parametrize("manufacturer", ["ManufacturerTooLong", "Acme Devices " + "X" * 20])
def test_a_shifted_old_scan_warns_and_does_not_query_detect(
    manufacturer, tmp_path, fake_loader, monkeypatch, caplog
):
    world = _two_boards(
        scan=_old_scan(
            [
                (1, 3, "0x0403:0x6010", "FTDI2232", manufacturer, ARTY, "My Probe"),
                (1, 2, "0x0403:0x6010", "FTDI2232", "Digilent", BASYS, "Other Probe"),
            ]
        )
    )
    _, record = _start(tmp_path, monkeypatch, world, {"board": "arty_a7_100t"}, caplog)
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "could not read the serial number" in warning
    assert "serial number for bus 1 device 3" in warning
    assert "unreadable serial" in warning
    assert "has no serial number of its own" not in warning
    assert not any("--detect" in query for query in queries(record))


def test_a_probe_the_scan_could_not_open_is_a_warning_and_no_claim_of_one_probe(
    tmp_path, fake_loader, monkeypatch, caplog
):
    """The scan skips a device it cannot open (a missing permission) and prints only an error:
    the table then shows one probe, and "only one probe fits" would be wrong."""
    scan = (
        "Error: can't open device with vid:vid = 0x0403:0x6010. Error code -3 Access denied\n"
        + scan_usb_text([Usb(1, 2, ARTY)])
    )
    flow, record = _start(
        tmp_path, monkeypatch, World(scan=scan), {"board": "arty_a7_100t"}, caplog
    )
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert flow.probe_note is None
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "could not open a probe of the cable digilent (USB 0403:6010)" in warning
    assert "the 1 it lists" in warning and "Access denied" in warning
    assert not any("--detect" in q for q in queries(record))


def test_a_query_that_hangs_is_stopped_and_the_loader_chooses(
    tmp_path, fake_loader, monkeypatch, caplog
):
    """A real hang, not a refusal made by a stub: the fake sleeps in its answer, the flow's time
    limit stops the process, and the loader chooses the probe."""
    monkeypatch.setattr("xeda.flows.openfpgaloader.PROBE_QUERY_TIMEOUT", 0.5)
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_QUERY_DELAY", "60")
    flow, _ = _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"}, caplog)
    assert flow.succeeded and _argv(tmp_path) == _arty_command(tmp_path)
    assert any("Could not ask openFPGALoader" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------- the ID error's hint

ID_ERROR_LOG = """\
ir: 1 isc_done 0 isc_ena 0 init 1 done 0
Done            0x0
ID Error        ID error
"""


def _hint(**keywords) -> str:
    failure = loader_failure(ID_ERROR_LOG, target="board arty_a7_100t", **keywords)
    assert isinstance(failure, LoaderFailure)
    return "\n".join(failure.summary)


def test_the_id_error_hint_says_when_the_loader_chooses_and_when_xeda_does():
    text = _hint()
    assert PROBE_NOTE_DEFAULT in text
    assert PROBE_NOTE_DEFAULT == (
        "The loader opens the first cable that matches. Xeda chooses the probe only when the run "
        "names none, the part is known, several probes fit the cable, and the part matches "
        "exactly one of them. In any other case the loader can program the wrong board: name its "
        "probe with `usb_serial_num` (the serial number) or `cable_index`."
    )
    assert "The loader uses the first cable that matches unless" not in text


def test_the_id_error_hint_takes_the_flow_s_note_in_place_of_the_general_one():
    chosen = PROBE_NOTE_CHOSEN.format(
        probe=f"serial {ARTY}, bus 1 device 2", model="xc7a100", part=ARTY_PART
    )
    text = _hint(probe_note=chosen)
    assert chosen in text and PROBE_NOTE_DEFAULT not in text
    assert chosen == (
        f"Xeda chose the probe with serial {ARTY}, bus 1 device 2, because its FPGA (xc7a100) "
        f"matches the part {ARTY_PART}. So the board on the cable is the one this flow is set up "
        "to program. Check which device the bitstream was built for."
    )
    assert PROBE_NOTE_NAMED in _hint(probe_note=PROBE_NOTE_NAMED)


def test_an_id_error_after_the_flow_chose_a_probe_says_so(tmp_path, fake_loader, monkeypatch):
    """The chosen board's FPGA matched the part and still reported an ID error: the hint does not
    blame the cable."""
    monkeypatch.setenv(
        "XEDA_FAKE_FPGA_LOADER_STDOUT",
        "\n".join(["ir: 1 isc_done 0 isc_ena 0 init 1 done 0", "ID Error        ID error", ""]),
    )
    flow, _ = _start(tmp_path, monkeypatch, _two_boards(), {"board": "arty_a7_100t"})
    assert not flow.succeeded
    message = flow.results["error"]["message"]
    assert f"Xeda chose the probe with serial {ARTY}, bus 1 device 2" in message
    assert PROBE_NOTE_DEFAULT not in message
