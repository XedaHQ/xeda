#!/usr/bin/env python3
"""Write LeanLinkFrames.bsv: the host frames and the expected link bytes of the Bluespec bench.

Every byte comes from the reference codec of the host package (`host/leanlink/codec.py`): the
bench has no frame that was computed by hand. The script plays one session against the W = 8
profile (pages of 256 bytes) with a request budget of B bytes, and writes a table of steps that
`TbLeanLink.bsv` runs:

  send    the bench puts the bytes of a frame into the link, one byte every 12 clocks
  expect  the link must send exactly these bytes next
  quiet   the link must send nothing for this many clocks
  count   the endpoint must have executed this many requests

Usage: python3 gen_frames.py [OUTPUT]     (default: LeanLinkFrames.bsv beside this script)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "host"))

from leanlink import codec as lc  # noqa: E402

SID = 0x44332211  # the session ID of the script: all four bytes nonzero
LOG2W = 3  # W = 8
MAX_LEN = 255  # the largest frame of the profile (pages of 256 bytes)
B = 2048  # the budget of the bridge, in bytes
R = 64  # the status reserve, in bytes (a constant of the protocol)
RESYNC_MARGIN = 12  # bytes of B for the status frame that RESYNC permits (a constant too)
QUIET_CLOCKS = 600  # a rejected frame must stay silent this long
SEND, EXPECT, QUIET, COUNT = 0, 1, 2, 3

# The payloads of the requests. They have zeros at the start, in the middle and at the end (so
# that the COBS code bytes sit in every place), and one is as long as a frame may be.
HELLO = b"hello"
ZEROS = bytes([0, 0, 1, 0, 255, 0, 2, 3, 0, 0])
EMPTY = b""
LARGEST = bytes((i % 255) + 1 for i in range(243))  # no zero byte


class Script:
    """The steps, and the state of the host that builds them."""

    def __init__(self) -> None:
        self.blob = bytearray()
        self.steps: list[tuple[int, int, int]] = []
        self.consumed = 0  # the cursor of the last valid frame from the link
        self.seq = 0  # the sequence number of the next request
        self.count = 0  # requests executed so far

    def _add(self, kind: int, data: bytes = b"", value: int = 0) -> None:
        if data:
            self.steps.append((kind, len(self.blob), len(data)))
            self.blob += data
        else:
            self.steps.append((kind, 0, value))

    def send(self, wire: bytes) -> None:
        assert wire[-1] == 0 and 0 not in wire[:-1]
        self._add(SEND, wire)

    def expect(self, wire: bytes) -> None:
        assert wire[-1] == 0 and 0 not in wire[:-1]
        self._add(EXPECT, wire)

    def quiet(self) -> None:
        self._add(QUIET, value=QUIET_CLOCKS)

    def check_count(self, count: int) -> None:
        self._add(COUNT, value=count)

    # ---- frames ----
    def grant(self) -> int:
        return (self.consumed + B - R - RESYNC_MARGIN) & 0xFFFF

    def request(self, payload: bytes, *, seq: int | None = None, ack: int | None = None) -> bytes:
        """The wire bytes of a request frame (the next one, unless `seq` says otherwise)."""
        decoded = lc.build_host_frame(
            lc.host_ctl(data=True),
            self.seq if seq is None else seq,
            self.seq if ack is None else ack,  # the host has all the responses before this one
            self.grant(),
            payload,
            SID,
        )
        return lc.encode_host_wire(decoded)

    def fpga(
        self, ctl: int, seq: int, exp: int, payload: bytes, sid: int, len_field: int | None = None
    ) -> bytes:
        """The wire bytes of a frame from the link; it moves the cursor by its length."""
        wire_len = len(
            lc.encode_fpga_wire(lc.build_fpga_frame(ctl, seq, exp, 0, payload, sid, len_field))
        )
        cursor = (self.consumed + wire_len) & 0xFFFF
        decoded = lc.build_fpga_frame(ctl, seq, exp, cursor, payload, sid, len_field)
        self.consumed = cursor
        return lc.encode_fpga_wire(decoded)

    def response(self, payload: bytes) -> bytes:
        """The response to the request in sequence: the count of earlier executions (four
        bytes, little endian), then the request payload, cut to the response limit."""
        full = self.count.to_bytes(4, "little") + payload
        limit = MAX_LEN - 12
        trunc = len(full) > limit
        ctl = lc.fpga_ctl(data=True, trunc=trunc, log2w=LOG2W)
        wire = self.fpga(ctl, self.seq, self.seq + 1, full[:limit], SID)
        self.seq += 1
        self.count += 1
        return wire

    def status(self, seq: int, exp: int, sid: int) -> bytes:
        ctl = lc.fpga_ctl(log2w=LOG2W)
        return self.fpga(ctl, seq, exp, b"", sid, len_field=MAX_LEN)

    def exchange(self, payload: bytes) -> None:
        self.send(self.request(payload))
        self.expect(self.response(payload))
        self.check_count(self.count)


def build() -> Script:
    s = Script()

    # Power-up: the first byte out is a delimiter. It is not part of any frame.
    s.expect(bytes([0]))

    # A frame of an unknown session reaches an unbound link: one status frame, valid under S = 0
    # (the UNBOUND status frame), and nothing is executed.
    s.send(s.request(HELLO))
    s.expect(s.status(0, 0, 0))
    s.check_count(0)

    # OPEN: the reply is a status frame under the new session. The cursor starts again.
    s.consumed = 0
    open_frame = lc.build_open_frame(SID, MAX_LEN, grant=0)
    s.send(lc.encode_host_wire(open_frame))
    s.expect(s.status(0, 0, SID))

    # A request and its response, byte for byte.
    s.exchange(HELLO)

    # Two frames that must change nothing: the CRC differs in one bit, and the COBS structure is
    # wrong (a code byte points past the end of the frame).
    bad_crc = bytearray(s.request(ZEROS)[:-1])
    bad_crc[-3] ^= 0x10
    assert 0 not in bad_crc
    assert lc.check_host_frame(bytes(bad_crc), SID, 8, MAX_LEN).failures == ("crc",)
    s.send(bytes(bad_crc) + b"\x00")
    s.quiet()
    s.check_count(1)
    bad_cobs = bytearray(s.request(ZEROS)[:-1])
    bad_cobs[0] = 0xF0
    assert lc.cobs_decode(bytes(bad_cobs)) is None
    s.send(bytes(bad_cobs) + b"\x00")
    s.quiet()
    s.check_count(1)

    # The same request, now intact: it runs once.
    s.exchange(ZEROS)

    # A duplicate of that request gets a status frame and no second execution. It names the
    # next response to send and the next request to accept.
    s.send(s.request(ZEROS, seq=s.seq - 1, ack=s.seq))
    s.expect(s.status(s.seq, s.seq, SID))
    s.check_count(2)

    # A request with no payload, then one as long as a frame may be: the response is cut to the
    # response limit and carries TRUNC.
    s.exchange(EMPTY)
    s.exchange(LARGEST)
    s.exchange(HELLO)  # the sequence goes on after the largest frame
    return s


def check(script: Script) -> None:
    """Check the table against the codec once more, from the bytes alone."""
    for kind, off, length in script.steps:
        if kind == EXPECT:
            wire = bytes(script.blob[off : off + length])
            if wire == b"\x00":
                continue
            frame = lc.parse_fpga_frame(wire[:-1], SID, 8, MAX_LEN) or lc.parse_fpga_frame(
                wire[:-1], 0, 8, MAX_LEN
            )
            assert frame is not None, wire.hex()


def render(script: Script) -> str:
    rows = [
        f"   Step {{ kind: {('Send', 'Expect', 'Quiet', 'Count')[k]}, off: {o}, len: {n} }}"
        for k, o, n in script.steps
    ]
    rom = []
    for i in range(0, len(script.blob), 16):
        rom.append("   " + ", ".join(f"8'h{b:02x}" for b in script.blob[i : i + 16]))
    return (
        "// Generated by gen_frames.py from the codec of the host package. Do not edit.\n"
        "// The host frames and the bytes the link must answer, for TbLeanLink.bsv.\n"
        "package LeanLinkFrames;\n"
        "\n"
        "typedef enum { Send, Expect, Quiet, Count } StepKind deriving (Bits, Eq);\n"
        "\n"
        "// Send, Expect: `len` bytes of `frameRom` from `off`. Quiet: `len` clocks.\n"
        "// Count: the endpoint has executed `len` requests.\n"
        "typedef struct {\n"
        "   StepKind kind;\n"
        "   UInt#(16) off;\n"
        "   UInt#(16) len;\n"
        "} Step deriving (Bits, Eq);\n"
        "\n"
        f"Integer stepCount = {len(script.steps)};\n"
        f"Integer romSize = {len(script.blob)};\n"
        "\n"
        f"Step steps[{len(script.steps)}] = {{\n" + ",\n".join(rows) + "\n};\n"
        "\n"
        f"Bit#(8) frameRom[{len(script.blob)}] = {{\n" + ",\n".join(rom) + "\n};\n"
        "\n"
        "endpackage\n"
    )


def main() -> None:
    script = build()
    check(script)
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "LeanLinkFrames.bsv"
    out.write_text(render(script), encoding="utf-8")


if __name__ == "__main__":
    main()
