"""A machine for the fake `openFPGALoader` to describe: boards, cables, USB probes and devices.

The texts have the formats that openFPGALoader v1.2.0 prints for `--list-boards`,
`--list-cables`, `--scan-usb` (with the column widths of `libusb_ll.cpp`) and `--detect`, so the
flow's readers see what they see on a real machine. A test describes its
machine with `World`, and `World.install` writes it where the fake reads it
(`XEDA_FAKE_FPGA_LOADER_WORLD`) and returns the file in which the fake records every query.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

#: The cables and boards of the loader that the tests use, with what the real lists say about them.
CABLES = {
    "digilent": "0x0403:6010",
    "ft231X": "0x0403:6015",
    "ft2232": "0x0403:6010",
    "ft4232": "0x0403:6011",
    "usb-blaster": "0x09fb:6001",
    "ch347_jtag": "0x1a86:55dd",
    "dfu": "0x0000:0000",
    "xilinxPlatformCableUsb_alt": "0x03fd:000d",
}
BOARDS = {
    "arty_a7_100t": ("digilent", "xc7a100tcsg324"),
    "arty_a7_35t": ("digilent", "xc7a35tcsg324"),
    "basys3": ("digilent", "xc7a35tcpg236"),
    "ulx3s": ("ft231X", "LFE5U-85F-6BG381C"),
    "kc705": ("digilent", "Undefined"),
    "acornCle215": ("Undefined", "xc7a200tsbg484"),
}


def list_cables_text() -> str:
    """The output of `--list-cables`: a name, then the id. A long name runs into it."""
    rows = ["cable name               vid:pid"]
    for name, usb_id in CABLES.items():
        rows.append(f"{name:<25}{usb_id}")
    return "\n".join(rows) + "\n"


def list_boards_text() -> str:
    """The output of `--list-boards`."""
    rows = ["board name                 cable_name         fpga_part                "]
    for name, (cable, part) in BOARDS.items():
        rows.append(f"{name:<27}{cable:<19}{part:<25}")
    return "\n".join(rows) + "\n"


@dataclass(frozen=True)
class Usb:
    """A probe on the bus. `serial` of None prints as `none`, as the real loader does."""

    bus: int
    device: int
    serial: Optional[str]
    vid: int = 0x0403
    pid: int = 0x6010
    probe_type: str = "FTDI2232"
    manufacturer: str = "Digilent"
    product: str = "Digilent USB Device"


def scan_usb_text(probes: list[Usb]) -> str:
    """The output of `--scan-usb`, in the widths of `libusb_ll.cpp`."""
    serials = [p.serial or "none" for p in probes]
    probe_len = max([10, *(len(p.probe_type) for p in probes)]) + 1
    manufacturer_len = max([12, *(len(p.manufacturer) for p in probes)]) + 1
    # openFPGALoader reserves twelve characters for the serial and one for the separator.
    serial_len = max([12, *(len(s) for s in serials)]) + 1
    rows = [
        f"{'Bus':<4}{'device':<7}{'vid:pid':<14}{'probe_type':<{probe_len}}"
        f"{'manufacturer':<{manufacturer_len}}{'serial':<{serial_len}}product"
    ]
    for probe, serial in zip(probes, serials):
        usb_id = f"0x{probe.vid:04x}:0x{probe.pid:04x}"
        rows.append(
            f"{probe.bus:03d} {probe.device:03d}    {usb_id:<14}{probe.probe_type:<{probe_len}}"
            f"{probe.manufacturer:<{manufacturer_len}}{serial:<{serial_len}}{probe.product}"
        )
    return "\n".join(rows) + "\n"


#: The devices of the loader's `part.hpp` that the tests detect: idcode, manufacturer, family,
#: model, irlength.
DEVICES = {
    "xc7a100": (0x3631093, "xilinx", "artix a7 100t", "xc7a100", 6),
    "xc7a35": (0x362D093, "xilinx", "artix a7 35t", "xc7a35", 6),
    "xc7a200": (0x3636093, "xilinx", "artix a7 200t", "xc7a200", 6),
    "ecp5-85": (0x41113043, "lattice", "ECP5", "LFE5U-85", 8),
    "ecp5-25": (0x41111043, "lattice", "ECP5", "LFE5U-25", 8),
}


def detect_text(*devices: str, noise: bool = True) -> str:
    """The output of `--detect` for a chain of these devices, a name of `DEVICES` each. `arm` is
    a device that has a type and no model."""
    rows = ["Jtag frequency : requested 6.00MHz    -> real 6.00MHz   "] if noise else []
    for index, name in enumerate(devices):
        rows.append(f"index {index}:")
        if name == "arm":
            rows += ["\tidcode   0x4ba00477", "\ttype     ARM cortex A9", "\tirlength 4"]
            continue
        idcode, manufacturer, family, model, irlength = DEVICES[name]
        rows += [
            f"\tidcode 0x{idcode:x}",
            f"\tmanufacturer {manufacturer}",
            f"\tfamily {family}",
            f"\tmodel  {model}",
            f"\tirlength {irlength}",
        ]
    return "\n".join(rows) + "\n"


@dataclass
class World:
    """The machine: the probes on the bus and what `--detect` says for each probe.

    `detect` maps a serial number (or `<bus>:<device>`) to the output of `--detect` and its exit
    status; a probe that is not in it fails as the real loader does for a device that is gone."""

    probes: list[Usb] = field(default_factory=list)
    detect: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: The exact output of `--scan-usb`, in place of the table of `probes` (an old release's
    #: format, a failed scan).
    scan: Optional[str] = None

    def install(self, monkeypatch: pytest.MonkeyPatch, directory: Path) -> Path:
        """Describe this machine to the fake loader; return the file that records its queries."""
        queries = directory / "loader_queries.jsonl"
        world = directory / "loader_world.json"
        world.write_text(
            json.dumps(
                {
                    "list_boards": list_boards_text(),
                    "list_cables": list_cables_text(),
                    "scan_usb": scan_usb_text(self.probes) if self.scan is None else self.scan,
                    "detect": {
                        key: {"stdout": text, "status": status}
                        for key, (text, status) in self.detect.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_WORLD", str(world))
        monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_QUERIES", str(queries))
        return queries


def queries(record: Path) -> list[list[str]]:
    """The arguments of every query that the fake loader answered, in order."""
    if not record.exists():
        return []
    return [json.loads(line)["argv"] for line in record.read_text().splitlines()]
