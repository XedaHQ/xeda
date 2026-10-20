"""Which probe carries the FPGA that a flow is set up to program.

openFPGALoader opens the first probe that fits the cable's USB id, before it reads any ID code.
With two boards of one kind connected (two Digilent boards share 0x0403:0x6010) it can open the
wrong one, and `--fpga-part` does not change that. This module holds what the `openfpgaloader`
flow needs to choose the probe itself: the readers of the four outputs it asks the loader for
(`--list-boards`, `--list-cables`, `--scan-usb`, `--detect`), the rule that matches a detected
device to a part, and the decision.

Nothing here starts a process or reads a file. The flow starts the loader and passes the text in.
The formats are those of openFPGALoader 0.13.1 to 1.2.0. The files named in comments are the
loader's sources: `libusb_ll.cpp` (`--scan-usb`), `main.cpp` (`--detect`), `part.hpp` (the models).
"""

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Union

__all__ = [
    "ChainDevice",
    "Chosen",
    "Detection",
    "BITBANG_CABLES",
    "FTDI_VENDOR",
    "Reading",
    "Refusal",
    "UsbId",
    "UsbProbe",
    "choose_probe",
    "describe_probe",
    "describe_reading",
    "choice_text",
    "model_matches_part",
    "names_a_probe",
    "parse_board_cables",
    "parse_cable_ids",
    "parse_detect",
    "parse_scan_usb",
    "part_is_matchable",
    "probe_selector",
    "probes_without_a_name",
    "refusal_text",
    "scan_failure",
    "scan_open_errors",
    "selectable_usb_id",
]

#: The vendor id of FTDI. Only an FTDI probe has a serial number that `--ftdi-serial` selects.
FTDI_VENDOR = 0x0403

#: The cables that `--cable` alone cannot use: a bit-bang cable (`FTDI_BB` in `cable.hpp`) drives
#: the pins of an FTDI chip that only a board's pin list names (`main.cpp:207-214`). `--detect` on
#: such a cable without its board reads nothing, and it drives the pins of whatever FTDI chip is
#: there.
BITBANG_CABLES = frozenset({"ft231X", "ft232RL"})

_ESCAPE_CODES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: The cable of a board for which the loader knows none, in `--list-boards`.
_NO_CABLE = "Undefined"


def _plain(text: str) -> list[str]:
    """Return the lines of the output without the escape codes of a terminal's colors and without
    the carriage returns that a redrawn line leaves, with the line ends removed."""
    return _ESCAPE_CODES.sub("", text).replace("\r", "\n").split("\n")


@dataclass(frozen=True)
class UsbId:
    """The vendor and product id of a USB device."""

    vid: int
    pid: int

    def __str__(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


# ------------------------------------------------------------------------------------ the lists

_CABLE_ROW = re.compile(
    r"^(?P<name>\S.*?)(?:\s+|(?=0x))(?:0x)?(?P<vid>[0-9a-f]{1,4}):(?:0x)?(?P<pid>[0-9a-f]{1,4})\s*$",
    re.IGNORECASE,
)


def parse_cable_ids(text: str) -> dict[str, UsbId]:
    """Return the USB id of each cable in the output of `openFPGALoader --list-cables`.

    A row is the name of the cable and its id, as `0x0403:6010` (the product id has no `0x`). The
    reader takes any hex spelling. A long name runs into the id with no space
    (`xilinxPlatformCableUsb_alt0x03fd:000d`), so the row is read from the id at its end, not by
    column. A cable with the id `0000:0000` (`dfu`, `xvc-client`) is no USB device
    and is left out. The header and any line that is no row are skipped."""
    cables: dict[str, UsbId] = {}
    for line in _plain(text):
        found = _CABLE_ROW.match(line.strip())
        if not found:
            continue
        usb_id = UsbId(int(found["vid"], 16), int(found["pid"], 16))
        if usb_id.vid or usb_id.pid:
            cables[found["name"]] = usb_id
    return cables


def parse_board_cables(text: str) -> dict[str, str]:
    """Return the cable of each board in the output of `openFPGALoader --list-boards`.

    A row is the name of the board, the name of its cable and its part, in columns that white
    space separates (a board name has no space). A board whose cable is `Undefined` has none and
    is left out. The header and any row with fewer than two fields are skipped."""
    boards: dict[str, str] = {}
    for line in _plain(text):
        fields = line.split()
        if len(fields) < 2 or fields[:2] == ["board", "name"] or fields[1] == _NO_CABLE:
            continue
        boards[fields[0]] = fields[1]
    return boards


@dataclass(frozen=True)
class UsbProbe:
    """A probe in the output of `openFPGALoader --scan-usb`.

    `serial` is None when the scan says it has none. `serial_readable` is false when the row does
    not identify a trustworthy serial field.
    """

    bus: int
    device: int
    usb_id: UsbId
    probe_type: str
    manufacturer: str
    serial: Optional[str]
    product: str
    serial_readable: bool = True


_SCAN_ROW = re.compile(
    r"^\s*(?P<bus>-?\d+)\s+(?P<device>-?\d+)\s+(?:0x)?(?P<vid>[0-9a-f]{1,4}):(?:0x)?(?P<pid>[0-9a-f]{1,4})"
    r"(?=\s|$)",
    re.IGNORECASE,
)
#: The words of the header. v0.13.1 prints `probe type` with a space; later releases `probe_type`.
_SCAN_COLUMNS = (r"probe[_ ]type", "manufacturer", "serial", "product")
_SCAN_OPEN_ERROR = re.compile(
    r"can't open device with vid:vid = 0x(?P<vid>[0-9a-f]+):0x(?P<pid>[0-9a-f]+)", re.IGNORECASE
)


def _scan_header(lines: list[str]) -> Optional[str]:
    return next(
        (line for line in lines if all(re.search(name, line) for name in _SCAN_COLUMNS)), None
    )


def _scan_serial_candidates(
    line: str, start: int, width: int, separator_after: bool
) -> list[tuple[int, str]]:
    """Return tokens whose following padding fits a serial column of at least `width` chars.

    The loader left-aligns the serial in a fixed-width field and adds one separator before the
    product. The oldest format has a separator after the fixed-width field; newer headers include
    that space in the column width. The loader permits values wider than the field. An overlong
    preceding column can move its text into the advertised serial position, so more than one
    candidate makes the row ambiguous.
    """
    candidates = []
    for token in re.finditer(r"\S+", line[start:]):
        token_start = start + token.start()
        if token_start and not line[token_start - 1].isspace():
            continue
        value = token.group()
        token_end = token_start + len(value)
        field_end = token_start + max(width, len(value))
        if field_end > len(line) or (
            separator_after and (field_end >= len(line) or not line[field_end].isspace())
        ):
            continue
        if any(not char.isspace() for char in line[token_end:field_end]):
            continue
        candidates.append((token_start, value))
    return candidates


def scan_failure(text: str) -> Optional[str]:
    """Return why `--scan-usb` gave nothing Xeda can read, or None when its table is there.

    The table has a header even with no probe. Without one, the scan failed or the format is a
    new one; the reason is the last line that the loader printed, or says that there was none."""
    lines = _plain(text)
    if _scan_header(lines) is not None:
        return None
    printed = [line.strip() for line in lines if line.strip()]
    return printed[-1] if printed else "it printed nothing"


def scan_open_errors(text: str, usb_id: UsbId) -> list[str]:
    """Return the lines where the scan says that it could not open a device of this USB id.

    The scan skips such a device (a missing permission, a device in use; `libusb_ll.cpp:200-209`),
    so it is a probe that the table does not show."""
    errors = []
    for line in _plain(text):
        found = _SCAN_OPEN_ERROR.search(line)
        if found and (int(found["vid"], 16), int(found["pid"], 16)) == (usb_id.vid, usb_id.pid):
            errors.append(line.strip())
    return errors


def parse_scan_usb(text: str) -> list[UsbProbe]:
    """Return the probes in the output of `openFPGALoader --scan-usb`, in the order it lists them.

    The loader prints a header, `Bus device vid:pid probe_type manufacturer serial product`
    (`probe type` in v0.13.1), and one row for each probe, in columns of the header's widths
    (`libusb_ll.cpp`). The manufacturer and the product can hold spaces (`Digilent USB Device`),
    so the reader reads the bus, the device and the USB id, then cuts the rest of the row at the
    offsets of the header's words. A probe with no serial prints `none`; that and an empty field
    both mean no serial. Ambiguous column alignment leaves the serial unreadable. A row that is not
    a probe is skipped. Without the header the result is empty."""
    lines = _plain(text)
    header = _scan_header(lines)
    if header is None:
        return []
    probe_at, manufacturer_at, serial_at, product_at = (
        re.search(name, header).start() for name in _SCAN_COLUMNS  # type: ignore[union-attr]
    )
    old_scan = re.search(r"probe\s+type", header, re.IGNORECASE) is not None
    separator_after = old_scan
    serial_width = max(1, product_at - serial_at - int(separator_after))
    probes = []
    for line in lines:
        found = _SCAN_ROW.match(line)
        if not found:
            continue
        probe_type = line[probe_at:manufacturer_at].strip()
        serial_field = line[serial_at:product_at]
        candidate_line = line if separator_after else line[:product_at]
        candidates = _scan_serial_candidates(
            candidate_line, serial_at, serial_width, separator_after
        )
        serial_readable = len(line) >= product_at and (
            (not serial_field.strip() and not candidates)
            or (len(candidates) == 1 and candidates[0][0] == serial_at)
        )
        serial = candidates[0][1] if serial_readable and candidates else ""
        probes.append(
            UsbProbe(
                # the loader prints the numbers with `std::to_string(char)`: above 127, negative
                bus=int(found["bus"]) % 256,
                device=int(found["device"]) % 256,
                usb_id=UsbId(int(found["vid"], 16), int(found["pid"], 16)),
                probe_type=probe_type,
                manufacturer=line[manufacturer_at:serial_at].strip(),
                serial=None if serial in ("", "none") else serial,
                product=line[product_at:].strip(),
                serial_readable=serial_readable,
            )
        )
    return probes


# ----------------------------------------------------------------------------------- the detect


@dataclass(frozen=True)
class ChainDevice:
    """A device of a JTAG chain, as `--detect` prints it.

    `model` is None for a device that the loader knows only by its type (an ARM DAP) or not at
    all: it cannot program such a device."""

    index: int
    idcode: Optional[int]
    manufacturer: str
    family: str
    model: Optional[str]


@dataclass(frozen=True)
class Detection:
    """What `--detect` reported for one probe: the devices of the chain, in chain order.

    With no device, `reason` is the last line that the loader printed beside its frequency
    messages (why it could not read the chain), or empty when it printed none."""

    devices: tuple[ChainDevice, ...] = ()
    reason: str = ""

    @property
    def readable(self) -> bool:
        """Tell whether the loader reported the chain. When it did not (the probe is busy, a
        permission is missing, nothing answers), the probe is unknown, not unmatched."""
        return bool(self.devices)


_INDEX_LINE = re.compile(r"^index\s+(\d+):$")
#: Lines that `--detect` prints about the cable and not about a device or a failure.
_DETECT_NOISE = re.compile(r"^(?:jtag frequency|jtag probe limited|ret \d+)", re.IGNORECASE)


def parse_detect(text: str) -> Detection:
    """Read the output of `openFPGALoader --cable <cable> <probe> --detect`.

    The loader prints one block for each device of the chain (`main.cpp:445-470`): `index N:` and
    then the indented lines `idcode 0x3631093`, `manufacturer xilinx`, `family artix a7 100t`,
    `model  xc7a100` and `irlength 6`. A device of another kind has `type` in place of the model,
    and an idcode that the loader does not know has no line at all. A line that is not indented
    ends the block. Anything else the loader prints is a message about the cable."""
    devices: list[ChainDevice] = []
    block: Optional[dict[str, str]] = None
    index = 0
    others: list[str] = []

    def close() -> None:
        if block is not None:
            idcode = block.get("idcode", "").split()
            try:
                code: Optional[int] = int(idcode[0], 16) if idcode else None
            except ValueError:
                code = None
            devices.append(
                ChainDevice(
                    index=index,
                    idcode=code,
                    manufacturer=block.get("manufacturer", ""),
                    family=block.get("family", ""),
                    model=block.get("model") or None,
                )
            )

    for raw in _plain(text):
        line = raw.strip()
        if not line:
            continue
        opening = _INDEX_LINE.match(line)
        if opening:
            close()
            block, index = {}, int(opening[1])
        elif block is not None and raw[:1].isspace():
            key, _, value = line.partition(" ")
            block[key.lower()] = value.strip()
        else:
            close()
            block = None
            if not _DETECT_NOISE.match(line):
                others.append(line)
    close()
    return Detection(tuple(devices), others[-1] if others and not devices else "")


# --------------------------------------------------------------------------------- the matching

#: Models of `part.hpp` that carry more than the device (a package, a speed grade), by their
#: lower-case name, and the device they stand for. Without this the model would fail every other
#: package of the device (the MAX II `EPM240T100C5N` is no prefix of `EPM240T100I5N`).
_MODEL_ALIASES = {"xc7s15ftgb196-1": "xc7s15", "epm240t100c5n": "epm240"}

#: The parts that `model_matches_part` can match to the models of `part.hpp`, by the start of the
#: normalized part: the families whose models are spelled as their part numbers are (see
#: `model_matches_part`). Any other part is "cannot tell". iCE40 has no JTAG. The Spartan-6 and
#: Spartan-3 models and parts differ in the `T` variants and in where the package goes. The
#: MachXO3 models name a package in other letters than the part number does. The Arria 10 and
#: Stratix V models (`GX160`, `5SGSD5`) are no prefix of an ordering code.
_MATCHABLE_PART = re.compile(
    r"xc7[a-z]"  # Xilinx 7 series
    r"|xc[kvasz]u\d"  # Xilinx UltraScale and UltraScale+
    r"|xck\d"  # Xilinx Kria
    r"|lfe5u"  # Lattice ECP5 (LFE5U, LFE5UM, LFE5UM5G)
    r"|lcmxo2"  # Lattice MachXO2
    r"|lifcl|lfd2nx|lfcpnx"  # Lattice Nexus
    r"|ep[234]c|epm\d|10m\d|10cl\d"  # Intel MAX II, Cyclone II, III, IV, 10 LP, MAX 10
    r"|5c(?:e|g[tx]|s[ext])"  # Intel Cyclone V
    r"|a3c|a5e"  # Intel Agilex 3 and 5
)

#: A Gowin part as the ordering code writes it: the series, a code for the voltage and then the
#: capacity (`GW1NR-LV9QN88PC6/I5` is series `GW1NR`, capacity 9). Names without the voltage code
#: (`GW1NR-9C`, as nextpnr writes them) are read too.
_GOWIN_PART = re.compile(r"^(?P<series>gw\d[a-z]*?)-(?:[a-z]{2})?(?P<capacity>\d+)")
_GOWIN_MODEL = re.compile(r"^(?P<series>gw\d[a-z]*)-(?P<capacity>\d+)")


def _normalize(text: str) -> str:
    """Return the text in lower case with only its letters and digits."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _gowin_part_key(part: str) -> Optional[tuple[str, str]]:
    found = _GOWIN_PART.match(part.strip().lower())
    return (found["series"], found["capacity"]) if found else None


def part_is_matchable(part: str) -> bool:
    """Tell whether `model_matches_part` can compare a model with this part reliably.

    When it cannot (see `_MATCHABLE_PART`), the flow does not guess: it falls back to what the
    loader does without a choice, and warns."""
    return bool(_MATCHABLE_PART.match(_normalize(part))) or _gowin_part_key(part) is not None


def _expand_optional(name: str) -> list[str]:
    """Return the spellings that a name with optional parts stands for: `GW1N(R)-9C` is `GW1N-9C`
    and `GW1NR-9C`."""
    found = re.search(r"\(([^)]*)\)", name)
    if not found:
        return [name]
    return _expand_optional(name[: found.start()] + found[1] + name[found.end() :]) + (
        _expand_optional(name[: found.start()] + name[found.end() :])
    )


def _alternatives(model: str) -> list[str]:
    """Return the names that a model of `part.hpp` stands for.

    The loader joins devices that share an IDCODE with `/`. After a Xilinx `xczu4cg/eg/ev`, the
    later names are the endings of the first (`xczu4cg`, `xczu4eg`, `xczu4ev`); anywhere else each
    name is a whole one (`EP3C16/EP4CE15/10CL016`). A name can hold optional letters in
    parentheses (`GW2A(R)-18(C)`)."""
    names = [name.strip() for name in model.strip().split("/") if name.strip()]
    if len(names) > 1 and names[0].lower().startswith("xc"):
        first = names[0]
        names = [first] + [first[: max(0, len(first) - len(tail))] + tail for tail in names[1:]]
    return [spelling for name in names for spelling in _expand_optional(name)]


def _ends_at_a_name_boundary(part: str, consumed: int) -> bool:
    """Tell whether the written part goes on with something other than a digit after its first
    `consumed` letters and digits. The end of the text and any separator count as the end of a
    name: after the separator of `LCMXO2-7000HC-4TG144C` comes the speed grade `4`, which is no
    part of the device `7000HC`."""
    seen = 0
    for position, char in enumerate(part):
        if re.fullmatch("[a-z0-9]", char.lower()):
            seen += 1
            if seen == consumed:
                return not part[position + 1 : position + 2].isdigit()
    return False


def model_matches_part(model: str, part: str) -> bool:
    """Tell whether the `model` that `--detect` printed for a device fits the flow's `part`.

    The models are the strings of openFPGALoader's `part.hpp`. Each names the device and no
    package (`xc7a100`), so the part (`xc7a100tcsg324-1`) is the model and more. The rule:
    normalize both (lower case, letters and digits only). The normalized model must be a prefix of
    the normalized part, and the written part must not continue with a digit: `xc7a100` matches
    `xc7a100tcsg324-1`, and `xc7a35` does not match `xc7a350t`. A separator ends the name (see
    `_ends_at_a_name_boundary`).

    The families, and the spellings that need care:

    * Xilinx 7 series, UltraScale and UltraScale+: plain (`xc7a35`, `xcku040`, `xcvu9p`). `xck26`
      is the Kria module. `xc7s15ftgb196-1` names a package, so it stands for `xc7s15`. The
      Zynq UltraScale+ models join the variants of one IDCODE, `xczu4cg/eg/ev`: any of them
      matches, because the IDCODE does not tell them apart. Spartan-6, Spartan-3 and the CPLDs are
      not matched (`part_is_matchable`): `xc6slx25` is a prefix of the `T` part `xc6slx25tcsg324`.
    * Lattice ECP5 (`LFE5U-85`, `LFE5UM5G-25`), MachXO2 (`LCMXO2-7000ZE/LCMXO2-7000HE`) and Nexus
      (`LIFCL-40`, `LFD2NX-40`, `LFCPNX-100`): plain, with every name of a slash list. iCE40 has no
      JTAG, and the MachXO3 models name a package in other letters than the part number does
      (`CSFBGA121` and `MG121`), so these are not matched.
    * Gowin: the model (`GW1N(R)-9C`, `GW1NR-9`, `GW2A(R)-18(C)`) and the part
      (`GW1NR-LV9QN88PC6/I5`) share no prefix. The series and the capacity are compared; the `C`
      of a model and the letters after the capacity of a part number are not told apart and are
      ignored. `GW1N-9` and `GW1N-9C` therefore both match either spelling.
    * Intel MAX II, Cyclone II to IV, Cyclone 10 LP and MAX 10 (`EP4CE22`, `EP3C16/EP4CE15/
      10CL016`, `10M50D`): plain, with every name of a slash list. The loader's list names
      `10CL025` for two IDCODEs (`EP3C25/EP4CE22/10CL025` and
      `EP3C40/EP4CE30/EP4C40/10CL025`), so a `10CL025` part matches both devices. `EP2C15` and
      `EP2C20` share an IDCODE and the loader prints the first, so an `EP2C20` reads as `EP2C15`
      (a refusal for a correct board, never a wrong one). The MAX II model `EPM240T100C5N` is an
      alias of `epm240`.
    * Intel Cyclone V (`5CGX*3`, `5CE*A4`, `5CSE*A5/5CST*5/5CSX*5`): the `*` stands for the
      letters between the series and the density of an ordering code (`5CGXFC3B6F23C6`).
    * Intel Agilex 3 and 5 (`A3CY100`, `A5EC008B`): plain. Arria 10 and Stratix V (`GX160`,
      `5SGSD5`) are not matched: their models are no prefix of an ordering code.
    """
    wanted = _normalize(part)
    spelled = model.strip().lower()
    model = _MODEL_ALIASES.get(spelled, model)
    for name in _alternatives(model):
        if name.lower().startswith("gw"):
            have = _GOWIN_MODEL.match(name.lower())
            if have and (have["series"], have["capacity"]) == _gowin_part_key(part):
                return True
            continue
        pattern = "[a-z]*?".join(re.escape(_normalize(piece)) for piece in name.split("*"))
        found = re.match(pattern, wanted) if _normalize(name) else None
        if found and _ends_at_a_name_boundary(part, found.end()):
            return True
    return False


# ------------------------------------------------------------------------------- the decision


@dataclass(frozen=True)
class Reading:
    """A probe with what `--detect` reported for it."""

    probe: UsbProbe
    detection: Detection


@dataclass(frozen=True)
class Chosen:
    """The one probe whose FPGA matches the part. `device` is that FPGA."""

    reading: Reading
    device: ChainDevice
    others: tuple[Reading, ...]


@dataclass(frozen=True)
class Refusal:
    """No probe is chosen. Three cases: none has an FPGA that matches the part (`matching` is
    empty), several do, or exactly one does and another could not be read, so it may match too
    (`matching` has one probe and some reading is unreadable)."""

    readings: tuple[Reading, ...]
    matching: tuple[Reading, ...]


def _candidates(detection: Detection, index_chain: Optional[int]) -> list[ChainDevice]:
    """Return the devices of the chain that the loader could program.

    With an `index_chain` it programs the device at that index. Without one it takes the one
    device that it knows (`main.cpp:478-509`; it stops with an error when two FPGAs are on the
    chain), so every device with a model is a candidate."""
    if index_chain is not None:
        return [d for d in detection.devices if d.index == index_chain and d.model]
    return [d for d in detection.devices if d.model]


def choose_probe(
    readings: Sequence[Reading], part: str, index_chain: Optional[int] = None
) -> Union[Chosen, Refusal]:
    """Choose the probe whose FPGA matches `part`.

    A probe is chosen only when exactly one matches and every other probe was read and shows a
    different device. A probe whose detect failed or printed no device is unknown, not excluded:
    it may be a second board with the same FPGA (two boards of one FPGA cannot be told apart by
    the part), so choosing the readable match could program the wrong one. With none, with more
    than one, or with one match beside an unknown probe, the result is a `Refusal`: Xeda does not
    guess, and the user names the probe with `usb_serial_num`."""
    matches = []
    for reading in readings:
        device = next(
            (
                d
                for d in _candidates(reading.detection, index_chain)
                if d.model and model_matches_part(d.model, part)
            ),
            None,
        )
        if device is not None:
            matches.append((reading, device))
    if len(matches) == 1 and all(r.detection.readable for r in readings):
        reading, device = matches[0]
        return Chosen(reading, device, tuple(r for r in readings if r is not reading))
    return Refusal(tuple(readings), tuple(reading for reading, _ in matches))


def selectable_usb_id(usb_id: UsbId) -> bool:
    """Tell whether Xeda can name a probe of this USB id to the loader: an FTDI chip, by its
    serial number (`--ftdi-serial`, which every supported release has).

    Xeda names no other probe. `--busdev-num` reaches the FTDI cables only when the loader was
    built with libftdi 1.4 or newer (`ftdipp_mpsse.cpp`: `ftdi_usb_open_bus_addr` under
    `FTDI_VERSION >= 104`, else the probe is opened by USB id and the address is ignored), and
    Xeda cannot detect the build. The CH347 and ESP32-S3 take the address through `libusb_ll`,
    whose filter compares the bus with the device number and the device with the bus
    (`libusb_ll.cpp:75-77`). The other cables (USB-Blaster, CMSIS-DAP, J-Link, DirtyJTAG, XPC)
    ignore it."""
    return usb_id.vid == FTDI_VENDOR


def probes_without_a_name(probes: Sequence[UsbProbe]) -> list[UsbProbe]:
    """Return the probes that no serial number names: those with none, and those whose serial
    another of `probes` has too (two probes of one serial would still be one to the loader).

    Xeda chooses among `probes` only when this is empty, because a probe that needs
    `--busdev-num` cannot be named reliably (see `selectable_usb_id`)."""
    return [
        probe
        for probe in probes
        if not probe.serial or sum(1 for other in probes if other.serial == probe.serial) > 1
    ]


def probe_selector(probe: UsbProbe) -> list[str]:
    """Return the loader's arguments that name this probe: its serial number.

    The option is `--ftdi-serial`, which every supported loader has: v0.13.1 to v1.1.1 have only
    that one, and v1.2.0 keeps it as a deprecated spelling of `--usb-serial-num` (`main.cpp:
    1185-1190`). The `usb_serial_num` setting passes the same option. The probe must not be one
    that `probes_without_a_name` lists."""
    if not probe.serial:
        raise ValueError("a probe with no serial number cannot be named")
    return ["--ftdi-serial", probe.serial]


# ------------------------------------------------------------------------------- the messages

#: The options of openFPGALoader that name a probe, which a user can put in `extra_args`.
#: `--usb-serial-num` is the serial option of v1.2.0 and later, where `--ftdi-serial` is
#: deprecated (`main.cpp:966-969`); `--device` (`-d`) opens a probe by its tty (`ftdipp_mpsse.cpp`).
_PROBE_OPTIONS = (
    "--ftdi-serial",
    "--usb-serial-num",
    "--cable-index",
    "--busdev-num",
    "--vid",
    "--pid",
    "--device",
)
#: The options that change the cable the flow gave the loader, so its USB id is no longer known.
_CABLE_OPTIONS = ("--cable", "--board")


def _has_option(arguments: Sequence[str], options: Sequence[str], short: Sequence[str]) -> bool:
    """Tell whether the arguments hold a long option, alone or as `--option=value`, or a short
    one as a token of its own (`-d /dev/ttyUSB0`)."""
    return any(argument in short for argument in arguments) or any(
        argument == option or argument.startswith(option + "=")
        for argument in arguments
        for option in options
    )


def names_a_probe(arguments: Sequence[str]) -> bool:
    """Tell whether the arguments name a probe themselves, as `--ftdi-serial X` or
    `--ftdi-serial=X` do (also `--usb-serial-num`, by index, by bus and device, by USB id or by
    `-d`/`--device`). The flow then leaves the choice to the user."""
    return _has_option(arguments, _PROBE_OPTIONS, ("-d",))


def changes_the_cable(arguments: Sequence[str]) -> bool:
    """Tell whether the arguments give the loader another cable or board (`-c`, `--cable`, `-b`,
    `--board`), which replaces the cable the flow passed (`main.cpp:217-224`)."""
    return _has_option(arguments, _CABLE_OPTIONS, ("-c", "-b"))


def describe_probe(probe: UsbProbe) -> str:
    """Return how a message names a probe: `serial AB00000001, bus 1 device 2`."""
    serial = (
        f"serial {probe.serial}"
        if probe.serial
        else "no serial" if probe.serial_readable else "unreadable serial"
    )
    return f"{serial}, bus {probe.bus} device {probe.device}"


def describe_reading(detection: Detection) -> str:
    """Return the device(s) that `--detect` found, as a message gives them: `xc7a100`; with
    several, `xc7a100, xc7a35`; a chain with no FPGA that the loader knows, or no answer."""
    if not detection.readable:
        why = f" ({detection.reason})" if detection.reason else ""
        return f"could not read its device{why}"
    models = [d.model for d in detection.devices if d.model]
    return ", ".join(models) if models else "no FPGA that the loader knows"


def refusal_text(
    refusal: Refusal, *, cable: str, usb_id: UsbId, part: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the sentences and the lines of the message for a probe that cannot be chosen.

    The sentences say which of the three cases of `Refusal` happened and name the settings that
    pick a probe. The lines list every probe with what was detected on it, or why it could not be
    read, and the `usb_serial_num` setting that names it. Every probe listed has a serial number
    (`probes_without_a_name`)."""
    count = len(refusal.readings)
    unknown = [r for r in refusal.readings if not r.detection.readable]
    start = f"openFPGALoader has {count} probes on the cable {cable} (USB {usb_id}), and"
    if len(refusal.matching) > 1:
        sentences = [
            f"{start} {len(refusal.matching)} of them have an FPGA that matches the part {part}, "
            "so Xeda cannot tell which one to program."
        ]
    elif refusal.matching:
        sentences = [
            f"{start} one of them has an FPGA that matches the part {part}, but Xeda could not "
            f"read the device of {len(unknown)} other probe{'s' if len(unknown) > 1 else ''}. "
            "That may be another board with the same FPGA, so Xeda cannot tell which one to "
            "program."
        ]
    else:
        sentences = [f"{start} none of them has an FPGA that matches the part {part}."]
        if unknown:
            sentences.append(
                "Xeda could not read the device of some probes, and one of them may be the board "
                "you want. A probe can be busy, or a permission can be missing."
            )
    sentences.append(
        "Name the probe to program with the `usb_serial_num` setting (its serial number, listed "
        "below) or the `cable_index` setting. A named probe is programmed without reading the "
        "others."
    )
    sentences.append("Nothing was programmed.")
    lines = tuple(
        f"{describe_probe(r.probe)}: {describe_reading(r.detection)}"
        + (f" (`usb_serial_num: {r.probe.serial}`)" if r.probe.serial else "")
        for r in refusal.readings
    )
    return tuple(sentences), lines


def choice_text(chosen: Chosen, *, cable: str, usb_id: UsbId, part: str) -> str:
    """Return the message that says which probe was chosen and why, and what the others hold."""
    reading = chosen.reading
    device = chosen.device
    family = f" ({device.family})" if device.family else ""
    text = (
        f"Programming through the probe with {describe_probe(reading.probe)} on the cable "
        f"{cable} (USB {usb_id}): its FPGA is {device.model}{family}, which matches the part "
        f"{part}."
    )
    if chosen.others:
        others = "; ".join(
            f"{describe_probe(r.probe)}: {describe_reading(r.detection)}" for r in chosen.others
        )
        text += f" The other probes: {others}."
    return text
