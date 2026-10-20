"""Wire codecs for the uncoded UART link profile."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

HEADER_SIZE = 9
CRC_SIZE = 4
VERSION = 1
MAX_DECODED_FRAME = 240
MAX_WIRE_FRAME = MAX_DECODED_FRAME + MAX_DECODED_FRAME // 254 + 2
RESERVE = 45


def _make_crc32c_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _make_crc32c_table()


def crc32c(data: bytes) -> int:
    """Return the reflected CRC-32C value for data."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(encoded: bytes) -> bytes:
    if not encoded or 0 in encoded:
        raise ValueError("invalid COBS frame")
    out = bytearray()
    index = 0
    while index < len(encoded):
        code = encoded[index]
        if code == 0:
            raise ValueError("zero COBS code")
        index += 1
        end = index + code - 1
        if end > len(encoded):
            raise ValueError("truncated COBS block")
        out.extend(encoded[index:end])
        index = end
        if code < 0xFF and index < len(encoded):
            out.append(0)
    return bytes(out)


def rcobs_encode(data: bytes) -> bytes:
    """Encode the FPGA streaming form of reverse COBS.

    Data bytes pass through in order. Each block code follows its data, and a full 254-byte
    block emits ``FF`` before the final block code.
    """
    out = bytearray()
    count = 0
    for value in data:
        if value == 0:
            out.append(count + 1)
            count = 0
        else:
            out.append(value)
            count += 1
            if count == 254:
                out.append(0xFF)
                count = 0
    out.append(count + 1)
    return bytes(out)


def rcobs_decode(encoded: bytes) -> bytes:
    if not encoded or 0 in encoded:
        raise ValueError("invalid reverse COBS frame")
    out = bytearray()
    index = len(encoded)
    while index:
        code = encoded[index - 1]
        index -= 1
        block_size = code - 1
        if block_size > index:
            raise ValueError("truncated reverse COBS block")
        block_start = index - block_size
        out.extend(encoded[block_start:index][::-1])
        index = block_start
        if index and encoded[index - 1] < 0xFF:
            out.append(0)
    return bytes(out[::-1])


class Kind(IntEnum):
    REQUEST = 0
    RESPONSE = 1
    CONTROL = 2
    CONTROL_RESPONSE = 3


_KIND_NAMES = {
    "request": Kind.REQUEST,
    "response": Kind.RESPONSE,
    "control": Kind.CONTROL,
    "control_response": Kind.CONTROL_RESPONSE,
}


@dataclass(frozen=True)
class Frame:
    kind: str
    flags: int
    seq: int
    ack: int
    byte_cursor: int
    session: int
    payload: bytes = b""

    def decoded(self) -> bytes:
        try:
            kind = _KIND_NAMES[self.kind]
        except KeyError as exc:
            raise ValueError(f"unknown frame kind {self.kind!r}") from exc
        if not 0 <= self.flags <= 3 or not 0 <= self.seq <= 255 or not 0 <= self.ack <= 255:
            raise ValueError("frame byte field out of range")
        if not 0 <= self.byte_cursor <= 0xFFFF or not 0 <= self.session <= 0xFFFFFFFF:
            raise ValueError("frame cursor or session out of range")
        if self.session == 0:
            raise ValueError("session zero is reserved")
        if kind is Kind.REQUEST and self.flags != 0:
            raise ValueError("request flags must be zero")
        if len(self.payload) + HEADER_SIZE + CRC_SIZE > MAX_DECODED_FRAME:
            raise ValueError("decoded frame exceeds compiled limit")
        ctl = (VERSION << 6) | (int(kind) << 4) | self.flags
        header = bytes((ctl, self.seq, self.ack))
        header += self.byte_cursor.to_bytes(2, "little") + self.session.to_bytes(4, "little")
        body = header + self.payload
        return body + crc32c(body).to_bytes(4, "little")

    def encode(self) -> bytes:
        """Encode a host-to-FPGA COBS frame with its delimiter."""
        return cobs_encode(self.decoded()) + b"\x00"

    def encode_reverse(self) -> bytes:
        """Encode an FPGA-to-host reverse COBS frame with its delimiter."""
        return rcobs_encode(self.decoded()) + b"\x00"

    @classmethod
    def from_decoded(cls, data: bytes) -> Frame:
        if len(data) < HEADER_SIZE + CRC_SIZE:
            raise ValueError("frame is shorter than header and CRC")
        if len(data) > MAX_DECODED_FRAME:
            raise ValueError("decoded frame exceeds compiled limit")
        body, crc_bytes = data[:-CRC_SIZE], data[-CRC_SIZE:]
        if crc32c(body).to_bytes(4, "little") != crc_bytes:
            raise ValueError("CRC-32C mismatch")
        ctl = body[0]
        if ctl >> 6 != VERSION or (ctl & 0x0C) != 0:
            raise ValueError("unsupported version or coding profile")
        try:
            kind = Kind((ctl >> 4) & 3)
        except ValueError as exc:
            raise ValueError("unknown frame kind") from exc
        session = int.from_bytes(body[5:9], "little")
        if session == 0:
            raise ValueError("session zero is reserved")
        flags = ctl & 3
        if kind is Kind.REQUEST and flags != 0:
            raise ValueError("request flags must be zero")
        if kind is Kind.RESPONSE and flags & 1 and len(body) != HEADER_SIZE:
            raise ValueError("GAP responses must not include an application result")
        return cls(
            kind=kind.name.lower(),
            flags=flags,
            seq=body[1],
            ack=body[2],
            byte_cursor=int.from_bytes(body[3:5], "little"),
            session=session,
            payload=body[HEADER_SIZE:],
        )

    @classmethod
    def decode(cls, wire: bytes) -> Frame:
        if wire.endswith(b"\x00"):
            wire = wire[:-1]
        return cls.from_decoded(cobs_decode(wire))

    @classmethod
    def decode_reverse(cls, wire: bytes) -> Frame:
        if wire.endswith(b"\x00"):
            wire = wire[:-1]
        return cls.from_decoded(rcobs_decode(wire))
