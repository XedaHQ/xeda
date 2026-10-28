#!/usr/bin/env python3
"""Echo test for the uart_echo design: send random bytes, read the echo back, compare.

Two modes:

* pipelined (default): a writer thread sends while the main thread reads in a tight loop, with at
  most --window bytes in flight (sent and not yet read back). Both directions of the line are busy
  at the same time, so this mode measures full-duplex line utilization. The window must cover the
  line rate times the round trip (USB, the FTDI latency timer, the OS: about 1 KB at 12 Mbaud).
  It must stay below the echo FIFO of the design (2047 bytes, ECHO_DEPTH), so that the FIFO cannot
  overflow. It must also stay below the receive buffer of the USB bridge, so that the bridge cannot
  overflow when the host stalls: 4 KiB on the FT2232H, 512 bytes on the FT231X. The default window
  is 1792 (448 for the FT231X with --ftdi or --port) and the limit is 2040.
* chunked (--mode chunked): the host writes a chunk, reads back exactly that many bytes, then sends
  the next chunk. The line is idle for a round trip after every chunk, so the throughput is chunk /
  (chunk time + round trip): 79 % of the line at 12 Mbaud with 4000 bytes. A chunk must stay below
  the receive buffer of the bridge. The default is 2048 bytes (448 for the FT231X).

The bridge sets the window. A WCH CH9102 (USB 1a86:55d4, full speed) has a receive FIFO of only
about 320 bytes and no flow control towards the FPGA. If the host reads late, more than about 256
bytes in flight can overflow the FIFO and lose 64-byte packets. With --port the script reads the
USB ids of the port (pyserial's `list_ports`, matched on the device name) and takes the window and
chunk of a known bridge from BRIDGES: 256 for the WCH CH9102, CH343 and CH34x, 448 for the FT231X,
1792 and 2048 for the FT2232H, FT4232H and FT232H, and the defaults above for any other port. The
script prints the choice. A --window or --chunk on the command line always wins, but a value
above the bridge's default gets a warning.

The payload is pseudo-random and made on the fly from the seed. Every chunk of the echo is compared
with it as it arrives (a C-speed compare, no per-byte Python), so a run of any length needs no
memory: --bytes 268435456 is a 256 MiB soak.

Usage examples
--------------
Find the port first: `ls /dev/cu.usbserial-*` (macOS), `ls /dev/ttyUSB*` (Linux), Device Manager
(Windows), or `python -m serial.tools.list_ports -v` on any system. `ftdi_urls.py`, which comes
with pyftdi, lists the pyftdi URLs.

Arty A7-100T (FT2232H channel B, 12 Mbaud, 100 MHz design) through pyftdi (libusb; channel B is
interface 2). The latency timer is set to 1 ms:

    python3 examples/bluespec/uart_echo/host/uart_echo_test.py \\
        --ftdi ftdi://ftdi:2232h:<serial>/2 --baud 12000000 --bytes 16777216

The same board through pyserial:

    python3 examples/bluespec/uart_echo/host/uart_echo_test.py \\
        --port /dev/cu.usbserial-<serial>1 --baud 3000000

ULX3S (FT231X, 3 Mbaud at most, 25 MHz design):

    python3 examples/bluespec/uart_echo/host/uart_echo_test.py \\
        --port /dev/cu.usbserial-<serial> --baud 3000000

Proving that the FPGA is in the loop (--transform): a plain echo cannot tell a design from a wire.
A design built with the macro ECHO_TRANSFORM returns f(b) = reverseBits(b) ^ 0xB1 for every byte b,
and --transform makes this tool expect that. f(b) != b for every byte (see ECHO_KEY), so a wire
loopback fails on the first byte and the report says so. Build the design with
`--design-updates rtl.defines.ECHO_TRANSFORM=1`:

    python3 examples/bluespec/uart_echo/host/uart_echo_test.py \\
        --port /dev/cu.usbserial-<serial> --baud 3000000 --transform

The report has an "echo:" line that says which mode ran. With --transform, a run in which every
received byte equals the byte that was sent says "the bytes came back unchanged" (a wire loopback,
or a design built without ECHO_TRANSFORM). Without --transform, a run in which every byte is the
transformed one says that the design was built with ECHO_TRANSFORM.

No hardware: an in-memory echo with a latency runs the same compare and report path, in both
modes, plain and transformed, with planted faults, a FIFO overflow and a wire loopback that the
check must catch:

    python3 examples/bluespec/uart_echo/host/uart_echo_test.py --loopback-selftest

Exit status: 0 only for a perfect echo (every byte back, in order, nothing extra, and with --ftdi no
packet flagged by the chip for an overrun). 1 for an echo failure. 2 for a usage or setup error.

Notes
-----
* The baud rate of the host and the one of the design (its switches, or FIXED_BAUD) must agree.
  With the switches all off it is 12 Mbaud on the Arty and 3 Mbaud on the ULX3S. The design changes
  its baud rate only when it is idle: after you flip a switch, wait a moment and start the host at
  the new rate.
* DTR and RTS stay low before and after open. On the Arty with jumper JP2 shorted, DTR is wired to
  the FPGA reset (CK_RST), so asserting DTR resets the design. pyserial asserts DTR on open by
  default, so this tool clears it first. With pyftdi the lines are cleared right after open.
* pyserial: never assign `ser.timeout` (or any other attribute) of an open port while data flows.
  pyserial then rewrites the terminal settings. Apple's FTDI driver turns that into USB control
  transfers in the middle of the traffic, and bytes are lost or corrupted at any baud rate. This
  tool sets the timeout once. pyserial cannot tell whether a driver dropped bytes, so on macOS use
  --ftdi at 8 Mbaud and above: its window cannot overflow the chip, and it counts the chip's own
  overrun flags.
* pyftdi reads an all-digit field of the URL as a device index, not as a serial number. Use a
  serial number that has a letter in it.
* Flow control is off. The Arty does not wire RTS/CTS.
* Latency timer: the FTDI default is 16 ms, which delays each partial USB packet by that much.
  With pyftdi this tool sets 1 ms. pyserial has no latency control on macOS, so use --ftdi there. On
  Linux, `setserial /dev/ttyUSB1 low_latency` sets it for pyserial.
* pyftdi reads one USB packet (512 bytes on the FT2232H, 64 on the FT231X) per call. Its default
  is 4 KiB: the chip completes such a transfer only when it is full, the data waits, and the
  pipelined throughput drops to 40 % of the line rate at 12 Mbaud. This tool sets the packet size.
* The reader and the writer use the port from two threads at the same time. pyserial allows that,
  and pyftdi uses one USB endpoint for each direction. If it misbehaves on your system, use
  --mode chunked.
* The FT2232H cannot generate 7, 9, 10 or 11 Mbaud. Use 12, 8, 6, 4 or 3 Mbaud on the Arty.
* Throughput is the whole run (round trips included), per direction. The line rate is baud / 10,
  because each byte is 10 bits on the wire (start, 8 data, stop).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import math
import random
import sys
import threading
import time
import types
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import serial
    from pyftdi.ftdi import Ftdi

DEFAULT_BAUD = 12_000_000
DEFAULT_BYTES = 1 << 20
DEFAULT_CHUNK = 2048
MAX_CHUNK = 4095  # below the 4 KiB FTDI buffers
DEFAULT_WINDOW = 1792  # bytes in flight, pipelined mode
MAX_WINDOW = 2040  # below the 2047 bytes of the design's echo FIFO
RX_MARGIN = 64  # the window stays this far below the receive buffer of the FTDI chip
DEFAULT_BLOCK = 512  # bytes per write, pipelined mode
READ_SIZE = 4096  # what the reader asks for at a time (pyftdi hands out one USB packet)
POLL = 0.02  # how long a read waits for data before the run looks at its state, in seconds
WINDOW_WAIT = 0.0001  # how long the writer sleeps when the window is full, in seconds
DEFAULT_TIMEOUT = 2.0
DRAIN_TIMEOUT = 0.1  # how long to wait for stray bytes after the last byte
PROGRESS_SECONDS = 10.0
BITS_PER_BYTE = 10  # start bit, 8 data bits, stop bit
BAUD_TOLERANCE = 0.005  # the accepted baud error of the FTDI chip, as a fraction
FTDI_LATENCY_MS = 1
HS_PACKET = 512  # USB bulk packet of the high-speed FTDI chips (FT2232H, FT4232H, FT232H)
FS_PACKET = 64  # and of the full-speed ones (FT231X, FT232R)
PREVIEW_BYTES = 8  # bytes shown on each side of the first mismatch
PAYLOAD_BLOCK = 1 << 16  # the payload is generated in blocks of this size
CONFIDENCE_FACTOR = 3.0  # 95 % upper bound on a rate after zero events: about 3 / trials
# The key of the transformed echo (ECHO_TRANSFORM): the design sends back reverseBits(b) ^ ECHO_KEY.
# This is the one definition here, and it is `echoKey` in rtl/UartEcho.bsv: keep the two equal.
# reverseBits(b) ^ b is a bit palindrome for every b, so f(b) == b needs a palindromic key; this
# one (1011_0001, reversed 1000_1101) is not, and f(b) != b for all 256 bytes (the self-test
# checks it).
ECHO_KEY = 0xB1


def echo_transform(value: int) -> int:
    """What a design built with ECHO_TRANSFORM sends back for the byte `value`."""
    return int(f"{value:08b}"[::-1], 2) ^ ECHO_KEY


# the transform of every byte, for bytes.translate (a C-speed map of a whole block)
TRANSFORM_TABLE = bytes(echo_transform(v) for v in range(256))


@dataclass(frozen=True)
class Bridge:
    """A USB-serial bridge known by its USB ids, with the window and chunk that are safe for it."""

    name: str
    window: int  # bytes in flight, pipelined mode
    chunk: int  # bytes written before the echo is read back, chunked mode
    why: str  # the basis of the numbers, for the report


# USB (vendor, product) ids. The CH9102 is the bridge of the STLV7325. The CH343 id is in the Linux
# cdc-acm driver (drivers/usb/class/cdc-acm.c, 1a86:55d3). The CH340/CH341 ids are in the Linux
# ch341 driver (drivers/usb/serial/ch341.c, 1a86:7523, 1a86:7522, 1a86:5523). Their buffers are
# not known: 256 is assumed, the window that suits the CH9102. The FTDI numbers are the ones of
# --ftdi above.
BRIDGES: dict[tuple[int, int], Bridge] = {
    (0x1A86, 0x55D4): Bridge("WCH CH9102", 256, 256, "~320 B receive FIFO, no flow control"),
    (0x1A86, 0x55D3): Bridge("WCH CH343", 256, 256, "CH9102 family, FIFO not measured"),
    (0x1A86, 0x7523): Bridge("WCH CH340", 256, 256, "FIFO not measured, 256 B assumed"),
    (0x1A86, 0x7522): Bridge("WCH CH340", 256, 256, "FIFO not measured, 256 B assumed"),
    (0x1A86, 0x5523): Bridge("WCH CH341", 256, 256, "FIFO not measured, 256 B assumed"),
    (0x0403, 0x6015): Bridge("FTDI FT231X", 448, 448, "512 B receive buffer"),
    (0x0403, 0x6010): Bridge("FTDI FT2232H", 1792, 2048, "4 KiB receive buffer"),
    (0x0403, 0x6011): Bridge("FTDI FT4232H", 1792, 2048, "as the FT2232H"),
    (0x0403, 0x6014): Bridge("FTDI FT232H", 1792, 2048, "as the FT2232H"),
}


def port_bridge(device: str, listing: Sequence[Any] | None = None) -> Bridge | None:
    """The known bridge behind the serial port `device`, by its USB ids. `listing` is the output
    of pyserial's comports(), read when it is None. None when the port is not listed, has no USB
    ids, or its bridge is not in BRIDGES."""
    if listing is None:
        try:
            from serial.tools import list_ports
        except ImportError:
            return None
        listing = list_ports.comports()
    for info in listing:
        if info.device == device and info.vid is not None and info.pid is not None:
            return BRIDGES.get((info.vid, info.pid))
    return None


class LinkError(Exception):
    """The link cannot be opened or configured as asked."""


class Link(Protocol):
    """A byte pipe to the design. Its modem lines are low and its flow control is off.

    `read` and `write` may be called from two threads at the same time."""

    def write(self, data: bytes | memoryview) -> None: ...

    def read(self, size: int, timeout: float) -> bytes:
        """Return up to size bytes as soon as some are there, or b"" after timeout seconds."""
        ...

    def close(self) -> None: ...


class SerialLink:
    """pyserial backend. DTR and RTS are False before open, so the port never opens high."""

    def __init__(self, port: str, baud: int, timeout: float) -> None:
        try:
            import serial
        except ImportError as exc:
            raise LinkError("pyserial is not installed (pip install pyserial)") from exc
        ser: serial.Serial = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.xonxoff = False
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = False
        ser.rts = False
        ser.timeout = POLL  # fixed: assigning it later makes pyserial rewrite the termios settings
        ser.write_timeout = timeout
        ser.open()
        ser.dtr = False  # pyserial applies its stored state on open; make sure it is low now
        ser.rts = False
        ser.reset_input_buffer()
        self._ser = ser

    def write(self, data: bytes | memoryview) -> None:
        self._ser.write(data)

    def read(self, size: int, timeout: float) -> bytes:
        # read(n) waits for n bytes or for the (fixed) timeout: wait for one, then take what is
        # there. Never assign `ser.timeout` here: pyserial then reconfigures the port, a
        # tcsetattr that Apple's FTDI driver turns into USB control transfers in the middle of
        # the traffic, and bytes are lost or corrupted at any rate.
        deadline = time.monotonic() + timeout
        while True:
            first = bytes(self._ser.read(1))
            if first:
                return first + bytes(self._ser.read(min(size - 1, self._ser.in_waiting)))
            if time.monotonic() >= deadline:
                return first

    def close(self) -> None:
        self._ser.close()


class _CountErrors(logging.Handler):
    """Counts the packets in which the FTDI chip reports an error (pyftdi logs each one)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("FTDI error"):
            self.count += 1


class FtdiLink:
    """pyftdi backend (libusb). Sets the baud rate, the latency timer and the modem lines itself.

    `chip_errors` counts the USB packets whose status byte flags an error of the chip: an overrun
    means that the host did not read fast enough and the chip dropped bytes (the FT231X of the
    ULX3S has a 512-byte buffer, the FT2232H 4 KiB)."""

    def __init__(self, url: str, baud: int) -> None:
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError as exc:
            raise LinkError("pyftdi is not installed (pip install pyftdi)") from exc
        ftdi: Ftdi = Ftdi()
        ftdi.open_from_url(url)
        self._ftdi = ftdi
        self._errors = _CountErrors()
        logging.getLogger("pyftdi.ftdi").addHandler(self._errors)
        self._packet = FS_PACKET
        try:
            ftdi.set_line_property(8, 1, "N")
            ftdi.set_flowctrl("")
            ftdi.set_dtr_rts(False, False)
            actual = ftdi.set_baudrate(baud, constrain=True)
            if abs(actual - baud) > baud * BAUD_TOLERANCE:
                raise LinkError(f"FTDI cannot generate {baud} baud (it set {actual})")
            ftdi.set_latency_timer(FTDI_LATENCY_MS)
            # One USB packet per read. pyftdi's default is 4 KiB, a transfer that the chip completes
            # only when it is full (8 packets): the data waits for the last packet, the window
            # runs dry, and the throughput falls to 40 %.
            self._packet = HS_PACKET if ftdi.is_H_series else FS_PACKET
            ftdi.read_data_set_chunksize(self._packet)
            ftdi.purge_buffers()
        except BaseException:
            logging.getLogger("pyftdi.ftdi").removeHandler(self._errors)
            ftdi.close()
            raise

    def write(self, data: bytes | memoryview) -> None:
        # one call writes the whole block, in USB transfers of the chip's TX buffer size
        self._ftdi.write_data(data)  # type: ignore[arg-type]

    def read(self, size: int, timeout: float) -> bytes:
        # pyftdi's read returns after at most one latency period when nothing arrives (the chip
        # sends a status packet), so this loop does not spin, and needs no sleep.
        deadline = time.monotonic() + timeout
        while True:
            data = bytes(self._ftdi.read_data_bytes(min(size, self._packet)))
            if data or time.monotonic() >= deadline:
                return data

    @property
    def rx_buffer(self) -> int:
        """The receive buffer of the chip in bytes: 4096 on the FT2232H, 512 on the FT231X."""
        return int(self._ftdi.fifo_sizes[1])

    @property
    def chip_errors(self) -> int:
        return self._errors.count

    def close(self) -> None:
        self._ftdi.close()
        logging.getLogger("pyftdi.ftdi").removeHandler(self._errors)


@dataclass(frozen=True)
class Fault:
    """Planted errors in the in-memory echo, by the index of the byte the host sent."""

    flip_at: int | None = None  # this byte comes back with one bit inverted
    drop_at: int | None = None  # this byte never comes back


class LoopbackLink:
    """In-memory echo, thread safe. A byte comes back `latency` seconds after it was written, unless
    the fault plan says otherwise.

    `capacity` models the echo FIFO of the design: a byte that arrives while that many are waiting
    to be read back is lost, as in the overrun of the real design."""

    def __init__(
        self,
        fault: Fault | None = None,
        latency: float = 0.0,
        capacity: int | None = None,
        echo: str = "plain",
    ) -> None:
        """`echo` is "plain" (b comes back as b), "transform" (f(b), a design with ECHO_TRANSFORM)
        or "wire" (b, which is what a hardwired TXD to RXD loopback does: the name only says
        what the test is about)."""
        self._table = TRANSFORM_TABLE if echo == "transform" else None
        self._fault = fault or Fault()
        self._latency = latency
        self._capacity = capacity
        self._lock = threading.Lock()
        self._pending: deque[tuple[float, bytes]] = deque()  # (time it can be read, bytes)
        self._waiting = 0
        self._sent = 0

    def write(self, data: bytes | memoryview) -> None:
        ready = time.monotonic() + self._latency
        kept = bytearray()
        with self._lock:
            for value in data:
                index = self._sent
                self._sent += 1
                if index == self._fault.drop_at:
                    continue
                if self._capacity is not None and self._waiting + len(kept) >= self._capacity:
                    continue
                if self._table is not None:
                    value = self._table[value]
                if index == self._fault.flip_at:
                    value ^= 0x40
                kept.append(value)
            if kept:
                self._pending.append((ready, bytes(kept)))
                self._waiting += len(kept)

    def read(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                out = bytearray()
                while self._pending and self._pending[0][0] <= time.monotonic() and len(out) < size:
                    when, block = self._pending.popleft()
                    take, rest = block[: size - len(out)], block[size - len(out) :]
                    out += take
                    if rest:
                        self._pending.appendleft((when, rest))
                self._waiting -= len(out)
            if out or time.monotonic() >= deadline:
                return bytes(out)
            time.sleep(0.0002)

    def close(self) -> None:
        pass


class Payload:
    """Pseudo-random bytes by offset, made block by block from the seed.

    Block k is `Random(seed * 2**32 + k)`, so any offset can be had without the bytes before it,
    and a run of any length needs memory for one block only. Not thread safe: each thread makes
    its own."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._index = -1
        self._block = b""

    def get(self, offset: int, size: int) -> bytes:
        parts: list[bytes] = []
        while size > 0:
            index, inside = divmod(offset, PAYLOAD_BLOCK)
            if index != self._index:
                self._block = random.Random((self._seed << 32) + index).randbytes(PAYLOAD_BLOCK)
                self._index = index
            part = self._block[inside : inside + size]
            parts.append(part)
            offset += len(part)
            size -= len(part)
        return parts[0] if len(parts) == 1 else b"".join(parts)


@dataclass(frozen=True)
class Preview:
    """The bytes around the first mismatch: what was sent and what came back, from `start`."""

    start: int  # offset of the first byte shown
    sent: bytes
    echoed: bytes  # may be shorter than `sent`, when the echo ended in this chunk
    expected: bytes | None = None  # what should have come back, when that is not `sent`


@dataclass(frozen=True)
class Comparison:
    """The echo against the payload."""

    sent: int
    received: int
    ok: int
    mismatched: int
    missing: int
    first_mismatch: int | None
    preview: Preview | None
    # Received bytes equal to the other mode's answer: the byte that was sent when a transformed
    # echo is expected, its transform when a plain one is. Counted in the chunks with errors only.
    other_mode: int = 0


class Checker:
    """Compares the echo with the payload, chunk by chunk, as it arrives.

    A chunk that is equal costs one memory compare. Only a chunk with an error is looked at byte by
    byte, so a clean run has no per-byte Python, and a failed one is slow only in proportion to
    the damage."""

    def __init__(self, total: int, seed: int, transform: bool = False) -> None:
        self._total = total
        self._transform = transform
        self._payload = Payload(seed)
        self._other_mode = 0
        self.received = 0  # bytes of the echo inside the payload range
        self.extra = 0  # bytes beyond the payload range
        self._mismatched = 0
        self._first: int | None = None
        self._preview: Preview | None = None

    def feed(self, data: bytes) -> None:
        offset = self.received
        inside = data[: max(0, self._total - offset)]
        self.extra += len(data) - len(inside)
        self.received += len(inside)
        if not inside:
            return
        sent = self._payload.get(offset, len(inside))
        want = sent.translate(TRANSFORM_TABLE) if self._transform else sent
        if want == inside:
            return
        bad = [i for i in range(len(inside)) if want[i] != inside[i]]
        self._mismatched += len(bad)
        other = sent if self._transform else sent.translate(TRANSFORM_TABLE)
        self._other_mode += sum(1 for i in range(len(inside)) if other[i] == inside[i])
        if self._first is None:
            lo = max(0, bad[0] - PREVIEW_BYTES)
            hi = min(len(inside), bad[0] + PREVIEW_BYTES + 1)
            self._first = offset + bad[0]
            self._preview = Preview(
                offset + lo, sent[lo:hi], inside[lo:hi], want[lo:hi] if self._transform else None
            )

    def result(self) -> Comparison:
        return Comparison(
            sent=self._total,
            received=self.received,
            ok=self.received - self._mismatched,
            mismatched=self._mismatched,
            missing=self._total - self.received,
            first_mismatch=self._first,
            preview=self._preview,
            other_mode=self._other_mode,
        )


@dataclass(frozen=True)
class Run:
    """The outcome of one echo run."""

    comp: Comparison
    elapsed: float  # seconds, from the first write to the last byte of the echo
    timed_out_at: int | None  # offset of the first byte that did not come back in time
    extra: int  # bytes that arrived after the last byte
    peak_in_flight: int = 0  # most bytes sent and not yet echoed (pipelined mode)
    chip_errors: int | None = None  # packets with an error flag from the FTDI chip (pyftdi only)


def _chip_errors(link: Link) -> int | None:
    return link.chip_errors if isinstance(link, FtdiLink) else None


def read_exactly(link: Link, size: int, timeout: float) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        part = link.read(size - len(buf), timeout)
        if not part:
            break
        buf += part
    return bytes(buf)


def drain(link: Link) -> int:
    count = 0
    while True:
        stray = link.read(READ_SIZE, DRAIN_TIMEOUT)
        if not stray:
            return count
        count += len(stray)


def run_chunked(
    link: Link, total: int, seed: int, chunk: int, timeout: float, transform: bool = False
) -> Run:
    """Write a chunk, read it back, repeat."""
    payload = Payload(seed)
    checker = Checker(total, seed, transform)
    timed_out_at: int | None = None
    start = time.perf_counter()
    for offset in range(0, total, chunk):
        size = min(chunk, total - offset)
        link.write(payload.get(offset, size))
        got = read_exactly(link, size, timeout)
        checker.feed(got)
        if len(got) < size:
            timed_out_at = offset + len(got)
            break
    elapsed = time.perf_counter() - start
    return Run(
        checker.result(),
        elapsed,
        timed_out_at,
        checker.extra + drain(link),
        chip_errors=_chip_errors(link),
    )


class _Pipe:
    """What the writer thread and the reader share. Plain attributes: each has one writer, and a
    read of an int is atomic."""

    def __init__(self) -> None:
        self.sent = 0  # bytes handed to the link, or about to be (writer)
        self.received = 0  # bytes of the echo read so far (reader)
        self.peak = 0  # most bytes in flight (writer)
        self.stop = False  # the reader has finished or given up (reader)
        self.error: BaseException | None = None  # the writer's exception


def run_pipelined(
    link: Link,
    total: int,
    seed: int,
    window: int,
    block: int,
    timeout: float,
    progress: Callable[[int, float], None] | None = None,
    transform: bool = False,
) -> Run:
    """Send from a thread while this one reads, with at most `window` bytes in flight.

    In flight means sent and not yet read back. The writer waits for room for a whole block, so
    the window is the most that the design and the FTDI chip ever hold (and it is checked here:
    `peak_in_flight` is what the run really reached). The reader does nothing but read and
    compare, in a loop without sleeps; the writer sleeps 0.1 ms when the window is full."""
    pipe = _Pipe()
    checker = Checker(total, seed, transform)

    def writer() -> None:
        payload = Payload(seed)
        try:
            while pipe.sent < total and not pipe.stop:
                size = min(block, total - pipe.sent)
                if pipe.sent + size - pipe.received > window:
                    time.sleep(WINDOW_WAIT)
                    continue
                data = payload.get(pipe.sent, size)
                pipe.sent += size
                pipe.peak = max(pipe.peak, pipe.sent - pipe.received)
                link.write(data)
        except BaseException as exc:  # reported by the reader, on its own thread
            pipe.error = exc

    thread = threading.Thread(target=writer, name="echo-writer", daemon=True)
    started = last = last_report = time.monotonic()
    end = started
    thread.start()
    try:
        while checker.received < total and pipe.error is None:
            data = link.read(READ_SIZE, POLL)
            now = time.monotonic()
            if data:
                checker.feed(data)
                pipe.received = checker.received
                end = last = now
                if progress is not None and now - last_report >= PROGRESS_SECONDS:
                    last_report = now
                    progress(pipe.received, now - started)
            elif pipe.sent > pipe.received and now - last > timeout:
                break  # bytes are in flight but none has come back for a timeout
            elif pipe.sent == pipe.received:
                last = now  # nothing in flight: nothing to wait for
    finally:
        pipe.stop = True
        thread.join(timeout + 1.0)
    if pipe.error is not None:
        raise pipe.error
    timed_out_at = None if checker.received >= total else checker.received
    return Run(
        checker.result(),
        end - started,
        timed_out_at,
        checker.extra + drain(link),
        pipe.peak,
        _chip_errors(link),
    )


def _hex_row(data: bytes, width: int) -> str:
    return " ".join(f"{data[i]:02x}" if i < len(data) else "--" for i in range(width))


def echo_mode(transform: bool) -> str:
    """The line of the report that says which echo the run expected."""
    if transform:
        return (
            "transformed (--transform): every byte b must come back as "
            f"reverseBits(b) ^ 0x{ECHO_KEY:02X}"
        )
    return "plain: every byte must come back as sent (a design built without ECHO_TRANSFORM)"


def unchanged_note(comp: Comparison, transform: bool) -> str | None:
    """A plain sentence when every received byte is the other mode's answer, else None."""
    if not comp.received or comp.other_mode != comp.received:
        return None
    if transform:
        return (
            "the bytes came back unchanged: a wire loopback, or a design built without "
            "ECHO_TRANSFORM"
        )
    return (
        "the bytes came back transformed (reverseBits(b) ^ "
        f"0x{ECHO_KEY:02X}): the design was built with ECHO_TRANSFORM, run with --transform"
    )


def report(run: Run, *, baud: int, how: str, target: str, transform: bool = False) -> bool:
    """Print the result. Return True only for a perfect echo. `how` describes the mode and
    `transform` says whether the echo of ECHO_TRANSFORM was expected."""
    comp = run.comp
    line_rate = baud / BITS_PER_BYTE
    # A packet flagged by the FTDI chip (overrun) is a run that was not clean, whatever the compare
    # says: the chip lost data once, and may have done it where the compare cannot see.
    perfect = comp.ok == comp.sent == comp.received and run.extra == 0 and not run.chip_errors
    rate = comp.received / run.elapsed if run.elapsed > 0 else 0.0
    print(f"target:      {target}")
    print(f"line:        {baud} baud, {BITS_PER_BYTE} bits per byte, {line_rate:,.0f} B/s")
    print(f"mode:        {how}")
    print(f"echo:        {echo_mode(transform)}")
    print(f"sent:        {comp.sent} bytes")
    print(f"echoed:      {comp.received} bytes")
    print(f"bytes OK:    {comp.ok}")
    print(f"mismatched:  {comp.mismatched}")
    print(f"missing:     {comp.missing}")
    print(f"extra:       {run.extra}")
    if run.peak_in_flight:
        print(f"in flight:   at most {run.peak_in_flight} bytes")
    if run.chip_errors is not None:
        print(f"chip errors: {run.chip_errors} packets flagged by the FTDI chip (overrun)")
        if run.chip_errors:
            print("             the chip lost data or reported an error: the run fails")
    if run.timed_out_at is not None:
        print(f"timeout:     no echo at offset {run.timed_out_at}")
    if comp.first_mismatch is not None and comp.preview is not None:
        pv = comp.preview
        print(f"first mismatch at offset {comp.first_mismatch} (0x{comp.first_mismatch:x})")
        span = f"[{pv.start}:{pv.start + len(pv.sent)}]"
        print(f"  sent     {span}  {_hex_row(pv.sent, len(pv.sent))}")
        if pv.expected is not None:
            print(f"  expected {span}  {_hex_row(pv.expected, len(pv.sent))}")
        print(f"  echoed   {span}  {_hex_row(pv.echoed, len(pv.sent))}")
    note = unchanged_note(comp, transform)
    if note is not None:
        print(f"NOTE:        {note}")
    print(
        f"throughput:  {rate:,.0f} B/s each way = {100 * rate / line_rate:.1f} % of the line rate"
    )
    print(f"elapsed:     {run.elapsed:.3f} s")
    if perfect and comp.received:
        bits = comp.received * 8
        print(
            f"error rate:  0 errors in {bits:.4g} data bits; 95 % bound on the bit error rate "
            f"{CONFIDENCE_FACTOR / bits:.2g} (echo path)"
        )
    print("RESULT: PASS" if perfect else "RESULT: FAIL")
    return perfect


def open_link(args: argparse.Namespace) -> Link:
    if args.ftdi:
        return FtdiLink(args.ftdi, args.baud)
    return SerialLink(args.port, args.baud, args.timeout)


def over_default_warning(flag: str, value: int, bridge: Bridge) -> str:
    """The warning for an explicit --window or --chunk above the default of a known bridge."""
    default = bridge.window if flag == "--window" else bridge.chunk
    return (
        f"{flag} {value} is above the {default} B default for the {bridge.name} bridge "
        f"({bridge.why}). The bridge may lose data if the host stalls. Keep {flag} at or below "
        f"{default} with this bridge."
    )


def choose_window(
    args: argparse.Namespace, link: Link | None, listing: Sequence[Any] | None = None
) -> tuple[list[str], list[str]]:
    """Fill in --window, --block and --chunk when they were not given, and check them. Return two
    lists: the lines that say which rule chose the window (or the chunk), for the report, and the
    warnings.

    With pyftdi the window and the chunk stay below the chip's receive buffer (RX_MARGIN under
    it), so even a host that stalls cannot overflow the chip, whatever the line does: 1792 and
    2048 bytes on the FT2232H (4 KiB buffer), 448 on the FT231X of the ULX3S (512 bytes). The
    defaults are clamped to that limit, and a --window (pipelined mode) or --chunk (chunked mode)
    above it is refused with a LinkError.

    With --port the defaults come from the bridge of the port (port_bridge, `listing` as there):
    256 on the WCH CH9102 of the STLV7325, which has a receive FIFO of about 320 bytes and no flow
    control. An explicit --window or --chunk always wins over the defaults and is not refused. With
    a known bridge, a larger explicit value gets a warning. Otherwise the limits are those of the
    design and the defaults are 1792 and 2048."""
    window, chunk = DEFAULT_WINDOW, DEFAULT_CHUNK
    notes: list[str] = []
    warnings: list[str] = []
    if args.port and not isinstance(link, FtdiLink):
        bridge = port_bridge(args.port, listing)
        if bridge is not None:
            window, chunk = bridge.window, bridge.chunk
            rule = f"{bridge.name} bridge: {bridge.why}"
            if args.window is None and args.mode == "pipelined":
                notes.append(f"window: {window} B ({rule})")
            if args.chunk is None and args.mode == "chunked":
                notes.append(f"chunk: {chunk} B ({rule})")
            if args.mode == "pipelined" and args.window is not None and args.window > window:
                warnings.append(over_default_warning("--window", args.window, bridge))
            if args.mode == "chunked" and args.chunk is not None and args.chunk > chunk:
                warnings.append(over_default_warning("--chunk", args.chunk, bridge))
    if isinstance(link, FtdiLink):
        room = max(1, link.rx_buffer - RX_MARGIN)
        if args.mode == "pipelined" and args.window is not None and args.window > room:
            raise LinkError(
                f"--window {args.window} is above {room} bytes, the receive buffer of this FTDI "
                f"chip ({link.rx_buffer} bytes) minus {RX_MARGIN}: the chip could overflow"
            )
        if args.mode == "chunked" and args.chunk is not None and args.chunk > room:
            raise LinkError(
                f"--chunk {args.chunk} is above {room} bytes, the receive buffer of this FTDI "
                f"chip ({link.rx_buffer} bytes) minus {RX_MARGIN}: the chip could overflow"
            )
        window, chunk = min(window, room), min(chunk, room)
    if args.window is None:
        args.window = window
    if args.chunk is None:
        args.chunk = chunk
    if args.block is None:
        args.block = min(DEFAULT_BLOCK, args.window)
    elif args.block > args.window:
        raise LinkError(f"--block {args.block} is larger than the window of {args.window} bytes")
    return notes, warnings


def run_echo(link: Link, args: argparse.Namespace) -> tuple[Run, str]:
    """Run the mode that the arguments ask for. Return the run and a description of the mode."""
    if args.mode == "chunked":
        run = run_chunked(link, args.total, args.seed, args.chunk, args.timeout, args.transform)
        return run, f"chunked, {args.chunk} B"

    def progress(done: int, seconds: float) -> None:
        print(
            f"  {done / (1 << 20):.0f} MiB in {seconds:.0f} s, {done / seconds:,.0f} B/s",
            file=sys.stderr,
            flush=True,
        )

    how = f"pipelined, window {args.window} B, writes of {args.block} B"
    run = run_pipelined(
        link,
        args.total,
        args.seed,
        args.window,
        args.block,
        args.timeout,
        progress if args.progress else None,
        args.transform,
    )
    return run, how


def _check_window_choice() -> int:
    """The defaults and limits of --window and --chunk, with and without an FTDI chip (a stand-in
    object with the chip's buffer size). Return the number of wrong outcomes."""

    def chip(buffer: int) -> FtdiLink:
        link = FtdiLink.__new__(FtdiLink)  # no USB: only rx_buffer is read
        link._ftdi = types.SimpleNamespace(fifo_sizes=(0, buffer))  # type: ignore[assignment]
        return link

    def choose(
        link: Link | None,
        mode: str,
        window: int | None = None,
        chunk: int | None = None,
        port: str | None = None,
        listing: Sequence[Any] | None = None,
    ) -> tuple[int, int] | str:
        ns = argparse.Namespace(mode=mode, window=window, chunk=chunk, block=None, port=port)
        try:
            choose_window(ns, link, listing)
        except LinkError:
            return "refused"
        return ns.window, ns.chunk

    def ports(vid: int, pid: int) -> list[types.SimpleNamespace]:
        """A fake comports() list with one port, /dev/cu.fake, of the given USB ids."""
        return [types.SimpleNamespace(device="/dev/cu.fake", vid=vid, pid=pid)]

    def choice_of(
        mode: str, window: int | None, listing: Sequence[Any], chunk: int | None = None
    ) -> tuple[list[str], list[str]]:
        ns = argparse.Namespace(
            mode=mode, window=window, chunk=chunk, block=None, port="/dev/cu.fake"
        )
        return choose_window(ns, None, listing)

    def notes_of(mode: str, window: int | None, listing: Sequence[Any]) -> list[str]:
        return choice_of(mode, window, listing)[0]

    cases = [
        ("no chip", choose(None, "pipelined"), (1792, 2048)),
        ("no chip, chunked", choose(None, "chunked"), (1792, 2048)),
        ("FT2232H", choose(chip(4096), "pipelined"), (1792, 2048)),
        ("FT231X", choose(chip(512), "pipelined"), (448, 448)),
        ("FT231X, window 448", choose(chip(512), "pipelined", window=448), (448, 448)),
        ("FT231X, window 1792", choose(chip(512), "pipelined", window=1792), "refused"),
        ("FT231X, chunk 2048", choose(chip(512), "chunked", chunk=2048), "refused"),
        ("FT231X, chunk 448", choose(chip(512), "chunked", chunk=448), (448, 448)),
        ("FT2232H, chunk 4000", choose(chip(4096), "chunked", chunk=4000), (1792, 4000)),
        ("FT2232H, chunk 4095", choose(chip(4096), "chunked", chunk=4095), "refused"),
        ("no chip, window 2040", choose(None, "pipelined", window=2040), (2040, 2048)),
        # --port: the bridge decides, by USB id, and an explicit value always wins
        (
            "CH9102 port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x1A86, 0x55D4)),
            (256, 256),
        ),
        (
            "CH9102 port, chunked",
            choose(None, "chunked", port="/dev/cu.fake", listing=ports(0x1A86, 0x55D4)),
            (256, 256),
        ),
        (
            "CH9102 port, window 1024",
            choose(None, "pipelined", 1024, port="/dev/cu.fake", listing=ports(0x1A86, 0x55D4)),
            (1024, 256),
        ),
        (
            "CH9102 port, chunk 2048",
            choose(None, "chunked", chunk=2048, port="/dev/cu.fake", listing=ports(0x1A86, 0x55D4)),
            (256, 2048),
        ),
        (
            "CH343 port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x1A86, 0x55D3)),
            (256, 256),
        ),
        (
            "CH340 port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x1A86, 0x7523)),
            (256, 256),
        ),
        (
            "FT231X port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x0403, 0x6015)),
            (448, 448),
        ),
        (
            "FT231X port, window 1792",
            choose(None, "pipelined", 1792, port="/dev/cu.fake", listing=ports(0x0403, 0x6015)),
            (1792, 448),
        ),
        (
            "FT2232H port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x0403, 0x6010)),
            (1792, 2048),
        ),
        (
            "FT4232H port",
            choose(None, "chunked", port="/dev/cu.fake", listing=ports(0x0403, 0x6011)),
            (1792, 2048),
        ),
        (
            "FT232H port",
            choose(None, "chunked", port="/dev/cu.fake", listing=ports(0x0403, 0x6014)),
            (1792, 2048),
        ),
        (
            "unknown bridge port",
            choose(None, "pipelined", port="/dev/cu.fake", listing=ports(0x10C4, 0xEA60)),
            (1792, 2048),
        ),
        (
            "port not listed",
            choose(None, "pipelined", port="/dev/cu.other", listing=ports(0x1A86, 0x55D4)),
            (1792, 2048),
        ),
        (
            "port without USB ids",
            choose(
                None,
                "pipelined",
                port="/dev/cu.fake",
                listing=[types.SimpleNamespace(device="/dev/cu.fake", vid=None, pid=None)],
            ),
            (1792, 2048),
        ),
    ]
    wrong = 0
    for title, got, want in cases:
        if got != want:
            wrong += 1
            print(f"SELFTEST: window choice, {title}: got {got}, want {want}", file=sys.stderr)
    # the lines that the report prints about the choice: present for a rule, absent for a value
    # given on the command line or an unknown port
    notes_cases = [
        (
            "CH9102 port names its rule",
            notes_of("pipelined", None, ports(0x1A86, 0x55D4)),
            ("window: 256 B (WCH CH9102 bridge: ~320 B receive FIFO, no flow control)",),
        ),
        (
            "CH9102 port, explicit window: no line",
            notes_of("pipelined", 1024, ports(0x1A86, 0x55D4)),
            (),
        ),
        ("unknown port: no line", notes_of("pipelined", None, ports(0x10C4, 0xEA60)), ()),
    ]
    for title, got, want in notes_cases:
        if got != list(want):
            wrong += 1
            print(
                f"SELFTEST: window choice, {title}: got {got}, want {list(want)}", file=sys.stderr
            )
    # the warnings: one for an explicit value above the bridge's default, none otherwise. Each
    # wanted entry is a fragment that the warning must contain.
    ch9102 = ports(0x1A86, 0x55D4)
    warning_cases = [
        ("CH9102 port, window 256: no warning", choice_of("pipelined", 256, ch9102)[1], ()),
        (
            "CH9102 port, window 1024: warns",
            choice_of("pipelined", 1024, ch9102)[1],
            ("--window 1024 is above the 256 B default for the WCH CH9102",),
        ),
        (
            "CH9102 port, chunked, window 2040 and chunk 2048: warns for the chunk only",
            choice_of("chunked", 2040, ch9102, 2048)[1],
            ("--chunk 2048 is above the 256 B default",),
        ),
        (
            "CH9102 port, chunk 2048 (chunked): warns",
            choice_of("chunked", None, ch9102, 2048)[1],
            ("--chunk 2048 is above the 256 B default",),
        ),
        (
            "CH9102 port, chunked, window 1024: no warning, the window is not used",
            choice_of("chunked", 1024, ch9102)[1],
            (),
        ),
        (
            "FT231X port, window 1792: warns",
            choice_of("pipelined", 1792, ports(0x0403, 0x6015))[1],
            ("--window 1792 is above the 448 B default for the FTDI FT231X",),
        ),
        (
            "FT2232H port, window 2040: warns",
            choice_of("pipelined", 2040, ports(0x0403, 0x6010))[1],
            ("--window 2040 is above the 1792 B default for the FTDI FT2232H",),
        ),
        (
            "unknown port, window 2040: no warning",
            choice_of("pipelined", 2040, ports(0x10C4, 0xEA60))[1],
            (),
        ),
        ("port not listed, window 2040: no warning", choice_of("pipelined", 2040, [])[1], ()),
    ]
    for title, got, want in warning_cases:
        if len(got) != len(want) or not all(f in g for f, g in zip(want, got, strict=True)):
            wrong += 1
            print(
                f"SELFTEST: window warning, {title}: got {got}, want {list(want)}", file=sys.stderr
            )
    print(f"== window and chunk limits: {len(cases) - wrong} of {len(cases)} as expected\n")
    return wrong


def _check_transform_table() -> int:
    """f(b) != b for all 256 bytes, f is a bijection, and a few values that the RTL and its
    testbench have as well (f(0) = ECHO_KEY, f(1) = 0x31: the byte reversed to 0x80, then xor).
    Return the number of wrong outcomes."""
    wrong = 0
    if any(echo_transform(v) == v for v in range(256)):
        wrong += 1
        print("SELFTEST: some byte is its own echo", file=sys.stderr)
    if sorted(TRANSFORM_TABLE) != list(range(256)):
        wrong += 1
        print("SELFTEST: the transform is not a bijection", file=sys.stderr)
    if (echo_transform(0x00), echo_transform(0x01), echo_transform(0xFF)) != (0xB1, 0x31, 0x4E):
        wrong += 1
        print("SELFTEST: the transform differs from the RTL's", file=sys.stderr)
    verdict = "ok" if not wrong else "WRONG"
    print(f"== echo transform, key 0x{ECHO_KEY:02X}: {verdict} for all 256 bytes\n")
    return wrong


def selftest(args: argparse.Namespace) -> int:
    """Run both modes over the in-memory link: clean, with planted faults, and with an overflow."""
    total = args.total
    flip_at = total // 3
    drop_at = 2 * total // 3
    timeout = min(args.timeout, 0.5)  # a dropped byte makes a run wait for this long
    choose_window(args, None)
    window, block = min(args.window, total // 4), min(args.block, total // 8)
    capacity = window + 64  # an echo FIFO that is big enough for the window
    latency = 0.002  # a round trip that the window has to cover
    pipelined = f"pipelined, window {window} B, writes of {block} B"
    failures = 0

    def check(
        title: str,
        run: Run,
        how: str,
        want_pass: bool,
        run_ok: Callable[[Run], bool] | None = None,
        *,
        transform: bool = False,
        says: str | None = None,
        silent_about: str | None = None,
    ) -> None:
        """`says` must be in the report, `silent_about` must not."""
        nonlocal failures
        print(f"== {title} (must {'PASS' if want_pass else 'FAIL'})")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = report(run, baud=DEFAULT_BAUD, how=how, target="loopback", transform=transform)
        text = out.getvalue()
        print(text, end="")
        wrong = ok != want_pass or (run_ok is not None and not run_ok(run))
        wrong = wrong or (says is not None and says not in text)
        wrong = wrong or (silent_about is not None and silent_about in text)
        if wrong:
            failures += 1
            print(f"SELFTEST: unexpected outcome in: {title}", file=sys.stderr)
        print()

    def chunked(link: LoopbackLink, transform: bool = False) -> Run:
        return run_chunked(link, total, args.seed, args.chunk, timeout, transform)

    def pipe(link: LoopbackLink, transform: bool = False) -> Run:
        return run_pipelined(link, total, args.seed, window, block, timeout, transform=transform)

    check("chunked, no faults", chunked(LoopbackLink()), f"chunked, {args.chunk} B", True)

    link = LoopbackLink(latency=latency, capacity=capacity)
    check(
        "pipelined, no faults",
        pipe(link),
        pipelined,
        True,
        lambda r: 0 < r.peak_in_flight <= window,
    )

    fault = Fault(flip_at=flip_at, drop_at=drop_at)
    # The drop shifts the rest of its chunk, so later bytes may mismatch too. The first mismatch
    # must be the flipped byte, and the run must stop short of the payload.
    check(
        "chunked, one byte flipped and one dropped",
        chunked(LoopbackLink(fault)),
        f"chunked, {args.chunk} B",
        False,
        lambda r: r.comp.first_mismatch == flip_at and r.comp.missing > 0,
    )

    check(
        "pipelined, one byte flipped and one dropped",
        pipe(LoopbackLink(fault, latency=latency, capacity=capacity)),
        pipelined,
        False,
        lambda r: (
            r.comp.first_mismatch == flip_at and r.comp.missing > 0 and r.timed_out_at is not None
        ),
    )

    check(
        "pipelined, window twice the echo FIFO (overflow)",
        pipe(LoopbackLink(latency=latency, capacity=window // 2)),
        f"pipelined, window {window} B, FIFO {window // 2} B",
        False,
        lambda r: r.comp.missing > 0,
    )

    # a packet flagged by the FTDI chip fails an otherwise perfect run
    clean = pipe(LoopbackLink(latency=latency, capacity=capacity))
    check(
        "pipelined, perfect echo but the FTDI chip flagged an overrun",
        replace(clean, chip_errors=1),
        pipelined,
        False,
    )
    check("pipelined, perfect echo, no chip errors", replace(clean, chip_errors=0), pipelined, True)

    # The transformed echo (ECHO_TRANSFORM): the checker expects f(sent). A design that really
    # transforms passes, a plain echo and a wire loopback (the same thing to this tool) fail with
    # the plain sentence, and so does the reverse.
    unchanged = "the bytes came back unchanged: a wire loopback, or a design built without"
    chunk_how = f"chunked, {args.chunk} B"

    def all_unchanged(r: Run) -> bool:
        return r.comp.mismatched == r.comp.received == total and r.comp.other_mode == total

    check(
        "transform on, chunked, echo of f(b)",
        chunked(LoopbackLink(echo="transform"), True),
        chunk_how,
        True,
        transform=True,
        says="transformed (--transform)",
        silent_about=unchanged,
    )
    check(
        "transform on, pipelined, echo of f(b)",
        pipe(LoopbackLink(latency=latency, capacity=capacity, echo="transform"), True),
        pipelined,
        True,
        lambda r: 0 < r.peak_in_flight <= window,
        transform=True,
        says="transformed (--transform)",
        silent_about=unchanged,
    )
    check(
        "transform on, chunked, plain echo (a wire loopback)",
        chunked(LoopbackLink(echo="wire"), True),
        chunk_how,
        False,
        all_unchanged,
        transform=True,
        says=unchanged,
    )
    check(
        "transform on, pipelined, plain echo (a wire loopback)",
        pipe(LoopbackLink(latency=latency, capacity=capacity, echo="wire"), True),
        pipelined,
        False,
        all_unchanged,
        transform=True,
        says=unchanged,
    )
    check(
        "transform on, one byte flipped and one dropped in the f(b) echo",
        chunked(LoopbackLink(fault, echo="transform"), True),
        chunk_how,
        False,
        lambda r: r.comp.first_mismatch == flip_at and r.comp.missing > 0,
        transform=True,
        silent_about=unchanged,
    )
    check(
        "transform off, a design that transforms (built with ECHO_TRANSFORM)",
        chunked(LoopbackLink(echo="transform")),
        chunk_how,
        False,
        transform=False,
        says="built with ECHO_TRANSFORM, run with --transform",
    )
    check(
        "transform off, plain echo",
        chunked(LoopbackLink()),
        chunk_how,
        True,
        silent_about="came back transformed",
    )

    failures += _check_transform_table()
    failures += _check_window_choice()

    # the payload does not depend on how it is cut into reads
    payload = Payload(args.seed)
    at = PAYLOAD_BLOCK - 3
    if payload.get(at, 6) != payload.get(at, 3) + payload.get(at + 3, 3):
        failures += 1
        print("SELFTEST: the payload changes across a block boundary", file=sys.stderr)

    if failures:
        print(f"SELFTEST FAIL ({failures} unexpected outcomes)", file=sys.stderr)
        return 1
    print("SELFTEST PASS")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send random bytes to the uart_echo design and check the echo.",
        epilog="See the module docstring for examples and notes on DTR, RTS and latency.",
    )
    link = parser.add_mutually_exclusive_group()
    link.add_argument(
        "--port",
        metavar="DEVICE",
        help="pyserial device, e.g. /dev/cu.usbserial-<serial>1, /dev/ttyUSB1 or COM5",
    )
    link.add_argument("--ftdi", metavar="URL", help="pyftdi URL, e.g. ftdi://ftdi:2232h:<serial>/2")
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default: %(default)s)"
    )
    parser.add_argument(
        "--bytes",
        dest="total",
        type=int,
        default=DEFAULT_BYTES,
        help="total bytes to send (default: 1 MiB)",
    )
    parser.add_argument(
        "--mode",
        choices=("pipelined", "chunked"),
        default="pipelined",
        help="pipelined: write while this program reads, at most --window bytes in flight; "
        "chunked: write a chunk, read it back (default: %(default)s)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help=f"pipelined: bytes in flight, 1..{MAX_WINDOW}, below the design's echo FIFO "
        f"(default: {DEFAULT_WINDOW}; with --ftdi the default is clamped to the chip's receive "
        f"buffer minus {RX_MARGIN} (448 for the FT231X), and a larger value is refused; with "
        "--port the default is the bridge's: 256 on the CH9102, and a larger value warns)",
    )
    parser.add_argument(
        "--block",
        type=int,
        default=None,
        help=f"pipelined: bytes per write, at most --window (default: {DEFAULT_BLOCK}, or the "
        "window if that is smaller)",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=None,
        help=f"chunked: bytes written before the echo is read back, 1..{MAX_CHUNK} "
        f"(default: {DEFAULT_CHUNK}; with --ftdi the default is clamped to the chip's receive "
        f"buffer minus {RX_MARGIN}, and a larger value is refused)",
    )
    parser.add_argument(
        "--transform",
        action="store_true",
        help="expect the echo of a design built with ECHO_TRANSFORM: each byte b comes back as "
        f"reverseBits(b) ^ 0x{ECHO_KEY:02X}, so a wire loopback from TXD to RXD fails "
        "(default: a plain echo)",
    )
    parser.add_argument("--seed", type=int, default=1, help="random seed (default: %(default)s)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds without any echo before the run gives up (default: %(default)s)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help=f"pipelined: print the progress every {PROGRESS_SECONDS:.0f} s on stderr",
    )
    parser.add_argument(
        "--loopback-selftest",
        action="store_true",
        help="run the compare and report path on an in-memory echo; no hardware is used",
    )
    args = parser.parse_args(argv)
    if not args.loopback_selftest and not (args.port or args.ftdi):
        parser.error("give --port, --ftdi, or --loopback-selftest")
    if args.baud <= 0:
        parser.error("--baud must be positive")
    if args.chunk is not None and not 1 <= args.chunk <= MAX_CHUNK:
        parser.error(f"--chunk must be between 1 and {MAX_CHUNK} (the FTDI buffers are 4 KiB)")
    if args.window is not None and not 1 <= args.window <= MAX_WINDOW:
        parser.error(f"--window must be between 1 and {MAX_WINDOW} (the echo FIFO is 2047 bytes)")
    if args.block is not None and args.block < 1:
        parser.error("--block must be positive")
    if args.total < BITS_PER_BYTE:
        parser.error("--bytes must be at least 10")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive, finite number of seconds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.loopback_selftest:
        return selftest(args)

    target = args.ftdi or args.port
    try:
        link = open_link(args)
    except (LinkError, OSError) as exc:
        print(f"error: cannot open {target}: {exc}", file=sys.stderr)
        return 2
    try:
        notes, warnings = choose_window(args, link)
    except LinkError as exc:
        link.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(note)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    try:
        run, how = run_echo(link, args)
    except OSError as exc:
        print(f"error: I/O failed on {target}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        link.close()

    return 0 if report(run, baud=args.baud, how=how, target=target, transform=args.transform) else 1


if __name__ == "__main__":
    sys.exit(main())
