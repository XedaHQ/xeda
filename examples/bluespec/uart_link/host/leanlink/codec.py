"""Independent reference codec for the lean UART packet transport.

This module follows the wire format, the host frame validity rules and the FPGA frame kinds of
doc/protocol.md. It does not use any hardware source.

Terms
-----
decoded frame   header (6 bytes) + payload + trailer (4 bytes). At most 253 bytes.
wire bytes      the decoded frame after COBS (host to FPGA) or rCOBS (FPGA to host) coding.
                A zero byte never appears in it.
wire frame      the wire bytes followed by one zero delimiter byte.
len             the byte at offset 5 of the decoded frame. It is the wire frame length
                (payload + 12), except in an FPGA status frame, where it is a capability.
S               the session ID that is folded into the trailer.

Check order
-----------
Every ``check_*`` function runs these checks in this fixed order and names the first one that
fails:

1. ``structure``  COBS or rCOBS structure of the wire bytes.
2. ``length``     the decoded length against ``len`` and the allowed ``len`` range.
3. ``header``     version, reserved bits, sequence ranges and the frame kind rules.
4. ``crc``        the trailer against CRC-32C of header and payload, XOR S.

The CRC is last. A frame that the CRC alone rejects has passed every deterministic check.
The order changes only the reported reason, never the verdict.

Where doc/protocol.md leaves a point open, the text below says "Interpretation".
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------------------------
# Constants

HEADER_LEN = 6
TRAILER_LEN = 4
MIN_DECODED = HEADER_LEN + TRAILER_LEN  # 10: header-only frame, status frame
MAX_DECODED = 253  # largest decoded frame: wire length 255 is the largest that len can hold
MIN_LEN = 12  # smallest wire frame length: empty payload
MAX_LEN = 255
MAX_PAYLOAD = MAX_DECODED - MIN_DECODED  # 243
OPEN_LEN = 17  # OPEN frame wire length: 12 + 5 payload bytes
DELIMITER = 0

CHECKS = ("structure", "length", "header", "crc")

# Host control byte
HOST_VERSION = 0b10
HOST_OPEN = 0x20
HOST_DATA = 0x10
HOST_POLL = 0x08
HOST_RESYNC = 0x01

# FPGA control byte
FPGA_VERSION = 0b10
FPGA_RESERVED = 0x20
FPGA_DATA = 0x10
FPGA_TRUNC = 0x08

# ---------------------------------------------------------------------------------------------
# CRC-32C

_POLY_REFLECTED = 0x82F63B78


def _make_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY_REFLECTED if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _make_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli): reflected polynomial 0x82F63B78, init and final XOR 0xFFFFFFFF."""
    crc = 0xFFFFFFFF
    table = _TABLE
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def crc32c_bitwise(data: bytes) -> int:
    """Slow bit-by-bit CRC-32C. It is the cross-check for the table version."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY_REFLECTED if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------------------------
# COBS (host to FPGA)


def cobs_encode(data: bytes) -> bytes:
    """Standard COBS. The output has no zero byte and no delimiter.

    A run of 254 nonzero bytes is coded as 0xFF and 254 bytes. A final 0xFF block is not
    followed by an extra 0x01 code. This does not matter for a frame: a frame has at most 253
    decoded bytes, so a valid frame never contains a 0xFF code.
    """
    out = bytearray([0])
    code_index = 0
    code = 1
    for byte in data:
        if code == 0xFF:  # the block is full and more data follows
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(frame: bytes) -> bytes | None:
    """Decode COBS wire bytes (no delimiter). Return None if the structure is malformed.

    Malformed: empty input, a zero byte, or a code that points past the end (incomplete final
    block). The last block adds no implied zero. A final 0xFF block with or without a trailing
    0x01 code is accepted. This is the lenient reading. It cannot matter for a valid frame.
    """
    frame = bytes(frame)
    count = len(frame)
    if count == 0 or 0 in frame:
        return None
    out = bytearray()
    index = 0
    while index < count:
        code = frame[index]
        end = index + code
        if end > count:
            return None
        out += frame[index + 1 : end]
        index = end
        if code != 0xFF and index < count:
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------------------------------------
# rCOBS: streaming reverse COBS (FPGA to host)


def rcobs_encode(data: bytes) -> bytes:
    """Streaming reverse COBS. The output has no zero byte and no delimiter.

    A nonzero byte passes through. A zero byte is replaced by the length of the preceding run
    plus one. A run of 254 nonzero bytes emits 0xFF at once and starts a new run. The frame ends
    with the code of the final run (0x01 if that run is empty).
    """
    out = bytearray()
    run = 0
    for byte in data:
        if byte == 0:
            out.append(run + 1)
            run = 0
        else:
            out.append(byte)
            run += 1
            if run == 254:
                out.append(0xFF)
                run = 0
    out.append(run + 1)
    return bytes(out)


def rcobs_decode(frame: bytes) -> bytes | None:
    """Decode rCOBS wire bytes (no delimiter) from the end backward. None if malformed.

    The last byte is the code of the final run. A block is a run followed by its code. A code
    other than 0xFF stands for one zero between its run and the next run. The first run must
    start exactly at the first byte.
    """
    frame = bytes(frame)
    pos = len(frame)
    if pos == 0 or 0 in frame:
        return None
    reverse = bytearray()
    first = True
    while pos > 0:
        pos -= 1
        code = frame[pos]
        if not first and code != 0xFF:
            reverse.append(0)
        first = False
        run = code - 1
        if run > pos:
            return None
        reverse += frame[pos - run : pos][::-1]
        pos -= run
    reverse.reverse()
    return bytes(reverse)


def code_positions(wire: bytes, reverse: bool) -> list[int]:
    """Return the indexes of the code bytes in valid COBS (or rCOBS if ``reverse``) wire bytes."""
    positions = []
    if reverse:
        pos = len(wire)
        while pos > 0:
            pos -= 1
            positions.append(pos)
            pos -= wire[pos] - 1
        positions.reverse()
    else:
        pos = 0
        while pos < len(wire):
            positions.append(pos)
            pos += wire[pos]
    return positions


def encode_host_wire(decoded: bytes) -> bytes:
    """COBS-encode a decoded host frame and append the delimiter."""
    return cobs_encode(decoded) + bytes([DELIMITER])


def encode_fpga_wire(decoded: bytes) -> bytes:
    """rCOBS-encode a decoded FPGA frame and append the delimiter."""
    return rcobs_encode(decoded) + bytes([DELIMITER])


# ---------------------------------------------------------------------------------------------
# Control bytes and frame builders


def _u(name: str, value: int, bits: int) -> int:
    if not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer, got {value!r}")
    return value


def host_ctl(
    *,
    open_frame: bool = False,
    data: bool = False,
    poll: bool = False,
    rewind: int = 0,
    resync: bool = False,
) -> int:
    """Pack a host control byte: version 10, OPEN, DATA, POLL, REWIND epoch, RESYNC."""
    _u("rewind", rewind, 2)
    return (
        (HOST_VERSION << 6)
        | (HOST_OPEN if open_frame else 0)
        | (HOST_DATA if data else 0)
        | (HOST_POLL if poll else 0)
        | (rewind << 1)
        | (HOST_RESYNC if resync else 0)
    )


def fpga_ctl(*, data: bool = False, trunc: bool = False, log2w: int = 0) -> int:
    """Pack an FPGA control byte: version 10, reserved 0, DATA, TRUNC, log2(W)."""
    _u("log2w", log2w, 3)
    return (FPGA_VERSION << 6) | (FPGA_DATA if data else 0) | (FPGA_TRUNC if trunc else 0) | log2w


def _build(
    ctl: int, byte1: int, byte2: int, word: int, payload: bytes, sid: int, len_field: int | None
) -> bytes:
    payload = bytes(payload)
    if len_field is None:
        len_field = len(payload) + 12
    header = bytes(
        [
            _u("ctl", ctl, 8),
            _u("seq", byte1, 8),
            _u("ack/exp", byte2, 8),
            _u("grant/cursor", word, 16) & 0xFF,
            word >> 8,
            _u("len", len_field, 8),
        ]
    )
    body = header + payload
    trailer = (crc32c(body) ^ _u("sid", sid, 32)).to_bytes(4, "little")
    return body + trailer


def build_host_frame(
    ctl: int, seq: int, ack: int, grant: int, payload: bytes, sid: int, len_field: int | None = None
) -> bytes:
    """Return the decoded host frame. The trailer is CRC32C(header + payload) XOR ``sid``.

    ``len`` is len(payload) + 12 unless ``len_field`` is given. For an OPEN frame, pass
    ``sid=0``: the OPEN frame is checked with S = 0 (see ``build_open_frame``).
    """
    return _build(ctl, seq, ack, grant, payload, sid, len_field)


def build_fpga_frame(
    ctl: int,
    seq: int,
    exp: int,
    cursor: int,
    payload: bytes,
    sid: int,
    len_field: int | None = None,
) -> bytes:
    """Return the decoded FPGA frame. The trailer is CRC32C(header + payload) XOR ``sid``.

    ``len`` is len(payload) + 12 unless ``len_field`` is given. A status frame has an empty
    payload and a ``len_field`` that carries the capability (maxLen).
    """
    return _build(ctl, seq, exp, cursor, payload, sid, len_field)


def build_open_frame(
    session_id: int,
    rsp_limit: int,
    *,
    seq: int = 0,
    ack: int = 0,
    grant: int = 0,
    poll: bool = False,
    rewind: int = 0,
) -> bytes:
    """Return a decoded host OPEN frame. Its trailer uses S = 0."""
    payload = _u("session_id", session_id, 32).to_bytes(4, "little") + bytes(
        [_u("rsp_limit", rsp_limit, 8)]
    )
    ctl = host_ctl(open_frame=True, poll=poll, rewind=rewind)
    return build_host_frame(ctl, seq, ack, grant, payload, 0)


# ---------------------------------------------------------------------------------------------
# Parsers


@dataclass(frozen=True)
class Verdict:
    """Result of a check.

    ``frame``     the parsed fields (a dict) if the frame is valid, else None.
    ``failures``  names of the failed checks, in check order. With ``full=False`` it holds only
                  the first failure. It is empty for a valid frame.
    ``decoded``   the decoded bytes if the structure was valid, else None.
    """

    frame: dict | None
    failures: tuple[str, ...]
    decoded: bytes | None

    @property
    def reason(self) -> str:
        """``ok`` or the name of the first failed check."""
        return self.failures[0] if self.failures else "ok"


def _check_params(sid: int | None, w: int | None, max_len: int | None) -> None:
    if sid is not None:
        _u("sid", sid, 32)
    if w is not None and (not isinstance(w, int) or w not in (1, 2, 4, 8, 16, 32, 64, 128)):
        raise ValueError(f"W must be a power of two from 1 to 128, got {w!r}")
    if max_len is not None and (not isinstance(max_len, int) or not MIN_LEN <= max_len <= MAX_LEN):
        raise ValueError(f"max_len must be in {MIN_LEN}..{MAX_LEN}, got {max_len!r}")


def check_host_frame(wire: bytes, sid: int, w: int, max_len: int, full: bool = False) -> Verdict:
    """Judge host wire bytes (COBS, delimiter removed) against doc/protocol.md.

    ``sid`` is the session ID S of the receiver (0 if the FPGA is unbound). ``w`` is the window
    W. ``max_len`` is maxLen of the build. A frame is valid only if all of these hold:

    - COBS structure is valid;
    - decoded length = len - 2, and 12 <= len <= max_len;
    - version (ctl[7:6]) = 10 (ctl[0] is RESYNC);
    - seq < 2W and ack < 2W;
    - exactly one kind applies:
      OPEN   (OPEN = 1, DATA = 0, len = 17, payload = session ID u32 + rspLimit u8 with
             12 <= rspLimit; a value above max_len means max_len),
      header-only (OPEN = 0, DATA = 0, len = 12),
      DATA   (OPEN = 0, DATA = 1, any payload length that len allows);
    - trailer = CRC32C(header + payload) XOR S, little-endian, where S = 0 if OPEN = 1
      (the ``sid`` argument is then ignored), else ``sid``.

    Interpretation: doc/protocol.md puts no limit on POLL or REWIND in an OPEN frame, so they are
    accepted there. It also gives no rule for the grant field, so any u16 value is valid.

    With ``full=False`` the check stops at the first failure.
    """
    _check_params(sid, w, max_len)
    decoded = cobs_decode(wire)
    if decoded is None:
        return Verdict(None, ("structure",), None)
    count = len(decoded)
    if count < MIN_DECODED:
        return Verdict(None, ("length",), decoded)  # no complete header and trailer
    ctl, seq, ack = decoded[0], decoded[1], decoded[2]
    length = decoded[5]
    failures: list[str] = []

    if not MIN_LEN <= length <= max_len or count != length - 2:
        failures.append("length")
        if not full:
            return Verdict(None, tuple(failures), decoded)

    is_open = bool(ctl & HOST_OPEN)
    is_data = bool(ctl & HOST_DATA)
    payload = decoded[HEADER_LEN:-TRAILER_LEN]
    header_ok = ctl >> 6 == HOST_VERSION and seq < 2 * w and ack < 2 * w
    if is_open:
        header_ok = header_ok and not is_data and length == OPEN_LEN
        if header_ok and len(payload) == OPEN_LEN - 12:
            # An rspLimit above maxLen means maxLen (the FPGA clamps it).
            header_ok = MIN_LEN <= payload[4]
    elif not is_data:
        header_ok = header_ok and length == MIN_LEN
    if not header_ok:
        failures.append("header")
        if not full:
            return Verdict(None, tuple(failures), decoded)

    s_used = 0 if is_open else sid
    trailer = int.from_bytes(decoded[-TRAILER_LEN:], "little")
    if trailer != crc32c(decoded[:-TRAILER_LEN]) ^ s_used:
        failures.append("crc")
    if failures:
        return Verdict(None, tuple(failures), decoded)

    frame = {
        "kind": "open" if is_open else "data" if is_data else "header",
        "ctl": ctl,
        "open": is_open,
        "data": is_data,
        "poll": bool(ctl & HOST_POLL),
        "rewind": (ctl >> 1) & 3,
        "resync": bool(ctl & HOST_RESYNC),
        "seq": seq,
        "ack": ack,
        "grant": decoded[3] | decoded[4] << 8,
        "len": length,
        "payload": bytes(payload),
    }
    if is_open:
        frame["session_id"] = int.from_bytes(payload[:4], "little")
        frame["rsp_limit"] = payload[4]
    return Verdict(frame, (), decoded)


def check_fpga_frame(
    wire: bytes, sid: int, w: int | None, max_len: int | None, full: bool = False
) -> Verdict:
    """Judge FPGA wire bytes (rCOBS, delimiter removed) against doc/protocol.md.

    doc/protocol.md has no explicit validity list for FPGA frames, so this function applies its
    general frame rules and its field definitions. A frame is valid only if all of these hold:

    - rCOBS structure is valid;
    - DATA = 1: decoded length = len - 2 and 12 <= len <= max_len;
    - DATA = 0 (status frame): decoded length = 10 (wire length 12) and len is the capability.
      The capability is in 12..255, and equals ``max_len`` if ``max_len`` is known;
    - version (ctl[7:6]) = 10 and the reserved bit ctl[5] = 0;
    - seq < 2W and exp < 2W;
    - ctl[2:0] = log2(W);
    - trailer = CRC32C(header + payload) XOR ``sid``, little-endian.

    ``w`` and ``max_len`` may be None while the host has not yet learned them from the first
    status frame (the OPEN reply). The three W checks and the max_len checks are then skipped.
    ``sid`` is the session ID under which the frame is judged. The host library also parses
    with ``sid = 0`` to find an UNBOUND FPGA. That is a second call, outside this function.

    Interpretation: TRUNC in a status frame, and a DATA response longer than rspLimit, are
    not rejected. doc/protocol.md does not forbid them.
    """
    _check_params(sid, w, max_len)
    decoded = rcobs_decode(wire)
    if decoded is None:
        return Verdict(None, ("structure",), None)
    count = len(decoded)
    if count < MIN_DECODED:
        return Verdict(None, ("length",), decoded)
    ctl, seq, exp = decoded[0], decoded[1], decoded[2]
    length = decoded[5]
    is_data = bool(ctl & FPGA_DATA)
    failures: list[str] = []

    if is_data:
        length_ok = MIN_LEN <= length <= (MAX_LEN if max_len is None else max_len)
        length_ok = length_ok and count == length - 2
    else:
        length_ok = count == MIN_DECODED and MIN_LEN <= length
        length_ok = length_ok and (max_len is None or length == max_len)
    if not length_ok:
        failures.append("length")
        if not full:
            return Verdict(None, tuple(failures), decoded)

    header_ok = ctl >> 6 == FPGA_VERSION and not ctl & FPGA_RESERVED
    if w is not None:
        header_ok = header_ok and seq < 2 * w and exp < 2 * w and (ctl & 7) == w.bit_length() - 1
    if not header_ok:
        failures.append("header")
        if not full:
            return Verdict(None, tuple(failures), decoded)

    trailer = int.from_bytes(decoded[-TRAILER_LEN:], "little")
    if trailer != crc32c(decoded[:-TRAILER_LEN]) ^ sid:
        failures.append("crc")
    if failures:
        return Verdict(None, tuple(failures), decoded)

    frame = {
        "kind": "data" if is_data else "status",
        "ctl": ctl,
        "data": is_data,
        "trunc": bool(ctl & FPGA_TRUNC),
        "log2w": ctl & 7,
        "seq": seq,
        "exp": exp,
        "cursor": decoded[3] | decoded[4] << 8,
        "len": length,
        "payload": bytes(decoded[HEADER_LEN:-TRAILER_LEN]),
    }
    if not is_data:
        frame["capability"] = length
    return Verdict(frame, (), decoded)


def parse_host_frame(wire: bytes, sid: int, W: int, max_len: int) -> dict | None:
    """Return the parsed host frame, or None if the frame is invalid (see check_host_frame)."""
    return check_host_frame(wire, sid, W, max_len).frame


def parse_fpga_frame(wire: bytes, sid: int, W: int | None, max_len: int | None) -> dict | None:
    """Return the parsed FPGA frame, or None if the frame is invalid (see check_fpga_frame)."""
    return check_fpga_frame(wire, sid, W, max_len).frame
