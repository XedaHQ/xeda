from __future__ import annotations

import pytest
import sys
from types import SimpleNamespace
from pathlib import Path

_original_path = sys.path[:]
try:
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples/bluespec/uart_link/host"))
    from uartlink import (
        Frame,
        cobs_decode,
        cobs_encode,
        crc32c,
        rcobs_decode,
        rcobs_encode,
    )
finally:
    sys.path[:] = _original_path


def test_pyftdi_backend_uses_the_raw_ftdi_api(monkeypatch):
    from uartlink import backends

    calls = []

    class Ftdi:
        is_H_series = False

        def open_from_url(self, url):
            calls.append(("open", url))

        def set_line_property(self, *args):
            calls.append(("line", *args))

        def set_flowctrl(self, mode):
            calls.append(("flow", mode))

        def set_dtr_rts(self, dtr, rts):
            calls.append(("modem", dtr, rts))

        def set_baudrate(self, baud, constrain):
            assert constrain
            return baud

        def set_latency_timer(self, value):
            calls.append(("latency", value))

        def read_data_set_chunksize(self, size):
            calls.append(("chunk", size))

        def write_data(self, data):
            return len(data)

        def read_data_bytes(self, size, attempt):
            calls.append(("read", size, attempt))
            return bytearray(b"reply")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(backends, "import_module", lambda _: SimpleNamespace(Ftdi=Ftdi))
    transport = backends.PyFtdiBackend("ftdi://ftdi:ft232h:TEST/1", 3_000_000)
    assert transport.write(b"test") == 4
    assert transport.read(4096) == b"reply"
    transport.close()
    assert ("chunk", 64) in calls
    assert ("latency", 1) in calls
    assert ("modem", False, False) in calls
    assert ("read", 64, 1) in calls


def test_crc32c_check_value() -> None:
    assert crc32c(b"123456789") == 0xE3069283


def _crc32c_bitloop(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def test_crc32c_matches_independent_bitloop_across_lengths_and_patterns() -> None:
    import random

    rng = random.Random(0xC32C)
    lengths = (
        0,
        1,
        2,
        7,
        8,
        9,
        15,
        16,
        31,
        32,
        63,
        64,
        127,
        128,
        191,
        192,
        235,
        236,
        239,
        240,
        255,
        256,
        511,
        512,
    )
    for length in lengths:
        patterns = (
            bytes(length),
            b"\xff" * length,
            bytes(index & 0xFF for index in range(length)),
            bytes(rng.randrange(256) for _ in range(length)),
        )
        for data in patterns:
            assert crc32c(data) == _crc32c_bitloop(data), f"CRC mismatch for {length} bytes"


@pytest.mark.parametrize(
    ("plain", "encoded"),
    [
        (b"", b"\x01"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
        (bytes(range(1, 255)), b"\xff" + bytes(range(1, 255)) + b"\x01"),
    ],
)
def test_cobs_fixed_vectors(plain: bytes, encoded: bytes) -> None:
    assert cobs_encode(plain) == encoded
    assert cobs_decode(encoded) == plain


def test_rcobs_fixed_vector() -> None:
    plain = b"\x11\x22\x00\x33"
    framed = b"\x11\x22\x03\x33\x02\x00"
    assert rcobs_encode(plain) + b"\x00" == framed
    assert rcobs_decode(framed[:-1]) == plain
    full_block = bytes(range(1, 255))
    encoded_block = full_block + b"\xff\x01"
    assert rcobs_encode(full_block) == encoded_block
    assert rcobs_decode(encoded_block) == full_block


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (bytes(range(1, 254)), bytes(range(1, 254)) + b"\xfe"),
        (bytes(range(1, 255)), bytes(range(1, 255)) + b"\xff\x01"),
        (bytes(range(1, 256)), bytes(range(1, 255)) + b"\xff\xff\x02"),
        (
            bytes(range(1, 255)) * 2,
            bytes(range(1, 255)) + b"\xff" + bytes(range(1, 255)) + b"\xff\x01",
        ),
        (b"\x00" + bytes(range(1, 255)), b"\x01" + bytes(range(1, 255)) + b"\xff\x01"),
        (bytes(range(1, 255)) + b"\x00\x01", bytes(range(1, 255)) + b"\xff\x01\x01\x02"),
        (bytes(range(1, 255)) + b"\x00\x00", bytes(range(1, 255)) + b"\xff\x01\x01\x01"),
    ],
    ids=["253", "254", "255", "508", "zero-before-full", "zero-after-full", "trailing-zeros"],
)
def test_device_side_rcobs_vectors(payload: bytes, expected: bytes) -> None:
    assert rcobs_encode(payload) == expected
    assert rcobs_decode(expected) == payload


class StreamingRcobsOracle:
    """Byte-at-a-time model of the FPGA encoder, kept separate from host codecs."""

    def __init__(self) -> None:
        self.count = 0
        self.output = bytearray()

    def push(self, chunk: bytes, *, final: bool = False) -> None:
        for value in chunk:
            if value == 0:
                self.output.append(self.count + 1)
                self.count = 0
            else:
                self.output.append(value)
                self.count += 1
                if self.count == 254:
                    self.output.append(0xFF)
                    self.count = 0
        if final:
            self.output.append(self.count + 1)


@pytest.mark.parametrize(
    "payload",
    [
        bytes(range(1, 254)),
        bytes(range(1, 255)),
        bytes(range(1, 256)),
        bytes(range(1, 255)) * 2,
        b"\x00" + bytes(range(1, 255)) + b"\x00",
        bytes(range(1, 255)) + b"\x00\x00" + bytes(range(1, 255)),
    ],
    ids=["253", "254", "255", "508", "zeros-around-full", "zero-between-full-blocks"],
)
def test_host_rcobs_matches_streaming_model_across_chunk_boundaries(payload: bytes) -> None:
    oracle = StreamingRcobsOracle()
    widths = (1, 17, 253, 2, 64)
    offset = 0
    index = 0
    while offset < len(payload):
        width = widths[index % len(widths)]
        oracle.push(payload[offset : offset + width])
        offset += width
        index += 1
    oracle.push(b"", final=True)
    assert rcobs_encode(payload) == bytes(oracle.output)


@pytest.mark.parametrize("bad", [b"", b"\x00", b"\x03\x11", b"\x02\x11\x00"])
def test_cobs_rejects_malformed_input(bad: bytes) -> None:
    with pytest.raises(ValueError):
        cobs_decode(bad)


def test_frame_round_trip_and_crc_rejection() -> None:
    frame = Frame(
        kind="request",
        flags=0,
        seq=7,
        ack=3,
        byte_cursor=0x1234,
        session=0x12345678,
        payload=b"\x30\x02\x00\x00ab",
    )
    wire = frame.encode()
    assert wire.endswith(b"\x00")
    assert Frame.decode(wire) == frame
    damaged = bytearray(wire)
    damaged[3] ^= 0x01
    with pytest.raises(ValueError):
        Frame.decode(bytes(damaged))


class BehavioralPeer:
    """Independent endpoint model with bounded TX credit and response snapshots."""

    def __init__(self) -> None:
        self.session = 0
        self.expected = 0
        self.delivery_ack = 0
        self.grant = 0
        self.cursor = 0
        self.cache: dict[int, bytes] = {}
        self.dispatches: dict[int, int] = {}
        self.seen: dict[int, int] = {}
        self.reader_paused = False
        self.rx_queue: list[bytes] = []
        self.drop_next_data = False
        self.drop_next_open_reply = False
        self.drop_next_close_reply = False
        self.drop_close_replies = False
        self.drop_response_sequences: set[int] = set()
        self.drop_next_poll_reply = False
        self.insert_before_next = b""

    def accept(self, request: Frame) -> None:
        if request.kind == "control":
            self.grant = request.byte_cursor
            self._retire(request.ack)
            selector = request.flags
            if selector == 2:
                if self.session and self.session != request.session:
                    return
                if not self.session:
                    self.session = request.session
                    self.cursor = 0
                selected = min(int.from_bytes(request.payload[:2], "little"), 209)
                window = min(request.payload[2], 8)
                self._reply(
                    Frame(
                        "control_response",
                        selector,
                        request.seq,
                        self.expected,
                        self.cursor,
                        self.session,
                        selected.to_bytes(2, "little") + bytes((window, 0)),
                    )
                )
            elif request.session == self.session and selector in (0, 1, 3):
                payload = b"\xf0\x00\x08\x01" if selector == 1 else b""
                self._reply(
                    Frame(
                        "control_response",
                        selector,
                        request.seq,
                        self.expected,
                        self.cursor,
                        self.session,
                        payload,
                    )
                )
            return
        if request.kind != "request" or request.session != self.session:
            return
        self.grant = request.byte_cursor
        self._retire(request.ack)
        seq = request.seq
        self.seen[seq] = self.seen.get(seq, 0) + 1
        if seq not in self.cache and seq == self.expected:
            self.expected = (self.expected + 1) & 0xFF
            self.dispatches[seq] = self.dispatches.get(seq, 0) + 1
            payload = request.payload
            if payload[:1] == b"\x30" and len(payload) >= 3:
                size = int.from_bytes(payload[1:3], "little")
                self.cache[seq] = b"\xb0" + payload[3 : 3 + size]
            else:
                self.cache[seq] = b"\x80"
        if seq in self.cache:
            self._reply(
                Frame(
                    "response",
                    2 if self.seen[seq] > 1 else 0,
                    seq,
                    self.expected,
                    self.cursor,
                    self.session,
                    self.cache[seq],
                )
            )

    def _retire(self, ack: int) -> None:
        while self.delivery_ack != ack:
            self.cache.pop(self.delivery_ack, None)
            self.delivery_ack = (self.delivery_ack + 1) & 0xFF

    def _reply(self, response: Frame) -> None:
        wire = response.encode_reverse()
        if self.cursor + len(wire) > self.grant:
            raise AssertionError("peer emitted bytes beyond the absolute host grant")
        self.cursor = (self.cursor + len(wire)) & 0xFFFF
        if self.drop_next_data and response.kind == "response":
            self.drop_next_data = False
            return
        if (
            self.drop_next_open_reply
            and response.kind == "control_response"
            and response.flags == 2
        ):
            self.drop_next_open_reply = False
            return
        if (
            (self.drop_next_close_reply or self.drop_close_replies)
            and response.kind == "control_response"
            and response.flags == 3
        ):
            self.drop_next_close_reply = False
            return
        if (
            self.drop_next_poll_reply
            and response.kind == "control_response"
            and response.flags == 0
        ):
            self.drop_next_poll_reply = False
            return
        if response.kind == "response" and response.seq in self.drop_response_sequences:
            self.drop_response_sequences.remove(response.seq)
            return
        self.rx_queue.append(self.insert_before_next + wire)
        self.insert_before_next = b""

    def read(self) -> bytes:
        if self.reader_paused or not self.rx_queue:
            return b""
        return self.rx_queue.pop(0)


def test_missing_result_recovers_original_response_without_dispatch_again() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=0x12345678, byte_budget=256, window=4)
    peer = BehavioralPeer()
    peer.drop_next_data = True
    open_request = state.open_frame()
    peer.accept(open_request)
    opened = Frame.decode_reverse(peer.read())
    assert state.accept_control_response(opened, len(opened.encode_reverse())) == "open"
    assert state.active and state.max_frame == 209

    first = state.submit(b"\x30\x03\x00one")
    second = state.submit(b"\x30\x03\x00two")
    state.note_transmitted(first.seq, 0.0)
    state.note_transmitted(second.seq, 0.0)
    peer.accept(first)
    peer.accept(second)
    response_two_wire = peer.read()
    response_two = Frame.decode_reverse(response_two_wire)
    assert state.receive_response(response_two, len(response_two_wire)) == b"\xb0two"
    assert state.pending[0].accepted
    assert state.pending[0].response is None
    assert state.pending[1].response == b"\xb0two"

    status = state.retries_due(now=state.pending[0].sent_at + state.timeout)
    assert len(status) == 1 and status[0].kind == "control"
    peer.accept(status[0])
    status_wire = peer.read()
    status_response = Frame.decode_reverse(status_wire)
    assert state.accept_control_response(status_response, len(status_wire)) == "poll"
    retry = state.take_ready_frames()
    assert len(retry) == 1
    assert retry[0].payload == first.payload
    peer.accept(retry[0])
    original = Frame.decode_reverse(peer.read())
    assert state.receive_response(original, len(original.encode_reverse())) == b"\xb0one"
    assert state.completed == {0: b"\xb0one", 1: b"\xb0two"}
    assert peer.dispatches == {0: 1, 1: 1}
    assert peer.cursor <= peer.grant


def test_reader_pause_and_inserted_bytes_do_not_release_credit() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=0x1234, byte_budget=256)
    peer = BehavioralPeer()
    peer.reader_paused = True
    peer.insert_before_next = b"\x55"
    opening = state.open_frame()
    peer.accept(opening)
    assert peer.cursor <= peer.grant
    assert peer.read() == b""
    peer.reader_paused = False
    damaged = peer.read()
    prior = (state.cursor_anchor, state.grant_edge)
    with pytest.raises(ValueError):
        Frame.decode_reverse(damaged)
    assert (state.cursor_anchor, state.grant_edge) == prior


def test_sequence_ack_wrap_and_credit_exhaustion_are_bounded() -> None:
    from uartlink import ProtocolState, RetryExhausted
    from uartlink.link import Pending

    state = ProtocolState(session=9, byte_budget=80, window=4)
    state.active = True
    state.tx_next = 1
    state.accepted_next = 254
    state.delivery_ack = 254
    state.pending = {seq: Pending(seq, b"\x00", 0.0) for seq in (254, 255, 0)}
    state._advance_acceptance(1)
    assert state.accepted_next == 1
    assert all(item.accepted for item in state.pending.values())

    opening = ProtocolState(session=11, byte_budget=80)
    for _ in range(opening.open_limit):
        opening.open_frame()
    with pytest.raises(RetryExhausted):
        opening.open_frame()


def test_status_control_retries_keep_the_same_frame_and_stop_at_reserve_limit() -> None:
    from uartlink import CreditExhausted, ProtocolState

    state = ProtocolState(session=11, byte_budget=256)
    state.active = True
    poll = state.poll_frame()
    state.note_control_transmitted(poll.seq, 0.0)
    retry1 = state.control_retries_due(now=1.0)
    assert retry1 == [poll]
    state.note_control_transmitted(poll.seq, 1.0)
    retry2 = state.control_retries_due(now=2.0)
    assert retry2 == [poll]
    state.note_control_transmitted(poll.seq, 2.0)
    with pytest.raises(CreditExhausted):
        state.control_retries_due(now=3.0)


def test_delivery_acknowledgment_crosses_sequence_wrap_in_order() -> None:
    from uartlink import ProtocolState
    from uartlink.link import Pending

    state = ProtocolState(session=11, byte_budget=256, window=4)
    state.active = True
    state.delivery_ack = 254
    state.accepted_next = 254
    state.tx_next = 1
    state.cursor_anchor = 100
    state.grant_edge = 356
    state.pending = {seq: Pending(seq, b"\x00", 0.0) for seq in (254, 255, 0)}
    for seq, payload in ((254, b"a"), (255, b"b"), (0, b"c")):
        response = Frame("response", 0, seq, 1, state.cursor_anchor, 11, payload)
        encoded = response.encode_reverse()
        assert state.receive_response(response, len(encoded)) == payload
    assert state.delivery_ack == 1
    assert state.completed == {254: b"a", 255: b"b", 0: b"c"}


def test_device_echo_uses_the_pure_state_with_an_in_memory_transport() -> None:
    from uartlink import Device

    peer = BehavioralPeer()

    class MemoryTransport:
        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            pass

    device = Device(MemoryTransport(), byte_budget=256, window=4)
    device.open()
    sample = bytes(range(64))
    assert device.echo(sample) == sample
    assert peer.dispatches == {0: 1}


def test_rto_samples_only_first_transmissions_and_respects_minimum() -> None:
    from uartlink import ProtocolState
    from uartlink.link import Pending

    now = [1.0]
    state = ProtocolState(
        session=9,
        byte_budget=256,
        timeout=0.5,
        min_timeout=0.1,
        wire_time_per_byte=0.001,
        clock=lambda: now[0],
    )
    state.active = True
    state.tx_next = 1
    state.cursor_anchor = 10
    state.grant_edge = 266
    state.pending[0] = Pending(0, b"\x00", 1.0)
    now[0] = 2.0
    response = Frame("response", 0, 0, 1, 10, 9, b"ok")
    wire_length = len(response.encode_reverse())
    state.receive_response(response, wire_length)
    assert state.srtt == 1.0
    assert state.rttvar == 0.5
    assert state.timeout == 3.0 + wire_length * 0.001

    retry_state = ProtocolState(session=10, byte_budget=256, timeout=0.5, min_timeout=0.1)
    retry_state.active = True
    retry_state.tx_next = 1
    retry_state.cursor_anchor = 10
    retry_state.grant_edge = 266
    retry_state.pending[0] = Pending(0, b"\x00", 1.0, retransmitted=True)
    retry_state.receive_response(Frame("response", 0, 0, 1, 10, 10, b"ok"), 20)
    assert retry_state.srtt is None
    assert retry_state.timeout == 0.5


def test_device_reader_discards_oversize_input_through_next_delimiter() -> None:
    from uartlink import Device, Frame
    from uartlink.codec import MAX_WIRE_FRAME

    class QueueTransport:
        def __init__(self) -> None:
            self.data = b"\x11" * (MAX_WIRE_FRAME + 10)

        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            data, self.data = self.data, b""
            return data

        def close(self) -> None:
            pass

    transport = QueueTransport()
    device = Device(transport)
    assert device._receive() == 0
    assert len(device._rx) == 0
    assert device._discard_until_delimiter
    transport.data = b"\x22\x00" + Frame("response", 0, 1, 0, 0, 1).encode_reverse()
    assert device._receive() == 1
    assert not device._discard_until_delimiter
    assert len(device._rx) == 0


def test_reset_reports_unknown_operations_and_does_not_replay_them() -> None:
    from uartlink import ProtocolError, ProtocolState, UnknownOutcome
    from uartlink.link import Pending

    state = ProtocolState(session=5)
    state.active = True
    state.pending[0] = Pending(0, b"write", 0.0)
    failures = state.mark_reset()
    assert len(failures) == 1
    assert isinstance(failures[0], UnknownOutcome)
    assert state.pending == {}
    assert not state.active
    assert state.controls == {}
    assert state.control_waiting == {}
    assert state.recovery_seq is None
    assert state.ready_frames == []
    assert state.retries_due() == []
    with pytest.raises(ProtocolError):
        state.submit(b"write")


def test_pipelined_result_tickets_survive_sequence_wrap_until_consumed() -> None:
    from uartlink import CreditExhausted, Device

    peer = BehavioralPeer()

    class MemoryTransport:
        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            pass

    device = Device(MemoryTransport(), byte_budget=256, window=8)
    device.open()
    tickets = []
    for value in range(256):
        ticket = device.submit(b"\x30\x01\x00" + bytes((value,)))
        tickets.append(ticket)
        device.pump()

    assert tickets == list(range(256))
    with pytest.raises(CreditExhausted):
        device.submit(b"\x30\x01\x00\xff")
    assert device.take_result(tickets[0]) == b"\xb0\x00"
    next_ticket = device.submit(b"\x30\x01\x00\xff")
    assert next_ticket == 256


def test_replayed_open_keeps_token_limits_and_grant() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9, byte_budget=256, window=4, max_frame=200)
    first = state.open_frame()
    second = state.open_frame()
    assert first == second
    assert first.seq == second.seq
    assert first.payload == second.payload
    assert first.byte_cursor == second.byte_cursor == 256


def test_open_retry_accepts_fresh_cursor_after_first_reply_was_lost() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9, byte_budget=256, window=8)
    peer = BehavioralPeer()
    peer.drop_next_open_reply = True
    first = state.open_frame()
    peer.accept(first)
    assert peer.read() == b""
    second = state.open_frame()
    assert second == first
    peer.accept(second)
    retry_wire = peer.read()
    retry = Frame.decode_reverse(retry_wire)
    assert retry.byte_cursor == len(retry_wire)
    assert state.accept_control_response(retry, len(retry_wire)) == "open"
    assert state.active
    assert state.cursor_anchor == peer.cursor
    assert state.grant_edge == (peer.cursor + 256) & 0xFFFF


@pytest.mark.parametrize("failure", ["zero", "exception"])
def test_partial_write_failure_poison_device_and_reports_unknown_outcome(failure: str) -> None:
    from uartlink import Device, LinkReset, UnknownOutcome

    class BrokenTransport:
        calls = 0

        def write(self, data: bytes) -> int:
            self.calls += 1
            if self.calls == 1:
                return 1
            if failure == "exception":
                raise OSError("write failed")
            return 0

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(BrokenTransport())
    device.state.active = True
    with pytest.raises(UnknownOutcome):
        device.submit(b"uncertain")
    assert not device.state.active
    assert device.state.pending == {}
    with pytest.raises(LinkReset):
        device.submit(b"do not replay")


@pytest.mark.parametrize(
    ("purpose", "flags", "payload"),
    [
        ("poll", 0, b"x"),
        ("capabilities", 1, b"\xc8\x00\x04\x02"),
        ("capabilities", 1, b"\xc8\x00\x04"),
        ("close", 3, b"x"),
    ],
)
def test_malformed_control_reply_does_not_change_state(
    purpose: str, flags: int, payload: bytes
) -> None:
    from uartlink import ProtocolError, ProtocolState
    from uartlink.link import Pending

    state = ProtocolState(session=11, byte_budget=256)
    state.active = True
    state.cursor_anchor = 10
    state.grant_edge = 266
    state.tx_next = 1
    state.pending[0] = Pending(0, b"request", 0.0)
    frame = {
        "poll": state.poll_frame,
        "capabilities": state.capabilities_frame,
        "close": state.close_frame,
    }[purpose]()
    reply = Frame("control_response", flags, frame.seq, 1, 10, 11, payload)
    with pytest.raises(ProtocolError):
        state.accept_control_response(reply, 5)
    assert state.control_waiting[frame.seq] == purpose
    assert state.cursor_anchor == 10
    assert state.accepted_next == 0
    assert not state.pending[0].accepted
    assert state.active


def test_device_close_requires_confirmation_and_refuses_pending_operations() -> None:
    from uartlink import CloseUnconfirmed, Device, ProtocolError

    peer = BehavioralPeer()

    class MemoryTransport:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            self.closed = True

    transport = MemoryTransport()
    device = Device(transport, timeout=0.01)
    device.open()
    ticket = device.submit(b"pending")
    before = peer.cursor
    with pytest.raises(ProtocolError):
        device.close()
    assert peer.cursor == before
    assert device.take_result(ticket) is None
    device.pump()
    assert device.take_result(ticket) == b"\x80"

    peer.drop_close_replies = True
    with pytest.raises(CloseUnconfirmed, match="reset and drain"):
        device.close()
    assert not device.state.active
    assert transport.closed


def test_close_returns_only_after_valid_confirmation() -> None:
    from uartlink import Device

    peer = BehavioralPeer()

    class MemoryTransport:
        def __init__(self) -> None:
            self.closed = False

        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            self.closed = True

    transport = MemoryTransport()
    device = Device(transport)
    device.open()
    device.close()
    assert not device.state.active
    assert transport.closed


def test_close_write_failure_reports_unknown_close_and_closes_backend() -> None:
    from uartlink import CloseUnconfirmed, Device

    peer = BehavioralPeer()

    class MemoryTransport:
        def __init__(self) -> None:
            self.closed = False
            self.fail_close = False

        def write(self, data: bytes) -> int:
            if self.fail_close:
                return 0
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            self.closed = True

    transport = MemoryTransport()
    device = Device(transport)
    device.open()
    transport.fail_close = True
    with pytest.raises(CloseUnconfirmed, match="reset and drain"):
        device.close()
    assert not device.state.active
    assert transport.closed


def test_statistics_use_transport_byte_counter_names() -> None:
    from uartlink.device import STAT_NAMES

    assert STAT_NAMES[:2] == ("transport_rx_bytes", "transport_tx_bytes")


@pytest.mark.parametrize(
    "invalid_payload", [None, b"\x80", b"\x82\x00\x00\x00\x00\x00", b"\x83\x01\x00\x00\x00\x00"]
)
def test_statistics_requires_a_valid_response_for_every_counter(invalid_payload) -> None:
    from dataclasses import replace

    from uartlink import Device, LinkError
    from uartlink.device import STAT_NAMES

    class CounterPeer(BehavioralPeer):
        def _reply(self, response: Frame) -> None:
            if response.kind == "response":
                payload = (
                    invalid_payload
                    if invalid_payload is not None
                    else bytes((0x83, response.seq))
                    + (0x11223300 + response.seq).to_bytes(4, "little")
                )
                response = replace(response, payload=payload)
            super()._reply(response)

    peer = CounterPeer()

    class MemoryTransport:
        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            pass

    device = Device(MemoryTransport())
    device.open()
    if invalid_payload is not None:
        with pytest.raises(LinkError, match="invalid statistics response"):
            device.statistics()
    else:
        assert device.statistics() == {
            name: 0x11223300 + index for index, name in enumerate(STAT_NAMES)
        }


def test_open_first_anchor_must_fit_the_original_byte_budget() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9, byte_budget=256)
    request = state.open_frame()
    bad = Frame(
        "control_response", 2, request.seq, 0, 250, 9, (209).to_bytes(2, "little") + b"\x08\x00"
    )
    bad_length = len(bad.encode_reverse())
    assert state.accept_control_response(bad, bad_length) is None
    assert not state.active
    assert state.cursor_anchor is None
    assert state.grant_edge == 256

    valid = Frame(
        "control_response", 2, request.seq, 0, 19, 9, (209).to_bytes(2, "little") + b"\x08\x00"
    )
    assert state.accept_control_response(valid, len(valid.encode_reverse())) == "open"


def test_cursor_wrap_and_stale_anchor_do_not_move_credit_backwards() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9, byte_budget=256)
    state.active = True
    state.cursor_anchor = 0xFFF0
    state.grant_edge = (0xFFF0 + 256) & 0xFFFF
    valid = Frame("response", 0, 0, 0, 0xFFF8, 9, b"x")
    assert state._anchor(valid.byte_cursor, 12)
    anchor = state.cursor_anchor
    grant = state.grant_edge
    stale = Frame("response", 0, 0, 0, 0xFFF0, 9, b"x")
    assert not state._anchor(stale.byte_cursor, 12)
    assert state.cursor_anchor == anchor
    assert state.grant_edge == grant


def test_bad_session_and_inserted_bytes_do_not_change_peer_or_host_state() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=7, byte_budget=256)
    state.active = True
    state.cursor_anchor = 20
    state.grant_edge = 276
    state.tx_next = 1
    state.pending[0] = __import__("uartlink.link", fromlist=["Pending"]).Pending(0, b"\x00", 0.0)
    before = (state.accepted_next, state.cursor_anchor, state.grant_edge)
    wrong_session = Frame("response", 0, 0, 1, 20, 8, b"ok")
    assert state.receive_response(wrong_session, 20) is None
    assert (state.accepted_next, state.cursor_anchor, state.grant_edge) == before
    with pytest.raises(ValueError):
        Frame.decode_reverse(b"\x55\x00\x01\x00")


def test_ack_ahead_of_issued_requests_cannot_advance_any_host_state() -> None:
    from uartlink import ProtocolState
    from uartlink.link import Pending

    state = ProtocolState(session=7, byte_budget=256)
    state.active = True
    state.tx_next = 1
    state.pending[0] = Pending(0, b"\x00", 0.0)
    state.cursor_anchor = 20
    state.grant_edge = 276
    before = (state.accepted_next, state.cursor_anchor, state.grant_edge)
    invalid = Frame("response", 0, 0, 4, 20, 7, b"result")
    assert state.receive_response(invalid, len(invalid.encode_reverse())) is None
    assert (state.accepted_next, state.cursor_anchor, state.grant_edge) == before
    assert state.pending[0].response is None


def test_two_missing_accepted_responses_serialize_recovery_polls() -> None:
    from uartlink import Device

    now = [0.0]
    peer = BehavioralPeer()

    class MemoryTransport:
        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            pass

    device = Device(MemoryTransport(), timeout=1.0, min_timeout=0.1, clock=lambda: now[0])
    device.open()
    peer.drop_response_sequences.update((0, 1))
    tickets = [
        device.submit(b"\x30\x01\x00\x00"),
        device.submit(b"\x30\x01\x00\x01"),
    ]

    device._send_poll()
    device.pump()
    assert device.state.accepted_next == 2
    assert device.state.pending[0].response is None
    assert device.state.pending[1].response is None

    peer.drop_next_poll_reply = True
    now[0] = 2.0
    device.pump()
    assert device.state.recovery_seq == 0
    assert not peer.rx_queue

    now[0] = 4.0
    device.pump()
    assert device.state.recovery_seq == 0

    for _ in range(8):
        device.pump()
        if len(device._results) == 2:
            break
    assert device.take_result(tickets[0]) == b"\xb0\x00"
    assert device.take_result(tickets[1]) == b"\xb0\x01"
    assert peer.dispatches == {0: 1, 1: 1}
    assert peer.seen == {0: 2, 1: 2}


def test_backend_read_failure_poison_device_and_marks_all_results_unknown() -> None:
    from uartlink import Device, LinkReset, UnknownOutcome

    peer = BehavioralPeer()

    class ReadFailureTransport:
        fail_read = False

        def write(self, data: bytes) -> int:
            peer.accept(Frame.decode(data))
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            if self.fail_read:
                raise OSError("device disconnected")
            return peer.read()

        def close(self) -> None:
            pass

    transport = ReadFailureTransport()
    device = Device(transport)
    device.open()
    tickets = [device.submit(b"first"), device.submit(b"second")]
    transport.fail_read = True
    with pytest.raises(UnknownOutcome, match="read"):
        device.pump()
    assert not device.state.active
    assert device.state.pending == {}
    for ticket in tickets:
        with pytest.raises(UnknownOutcome):
            device.take_result(ticket)
    with pytest.raises(LinkReset):
        device.submit(b"must not retry")
    with pytest.raises(LinkReset):
        device.pump()


def test_pyserial_read_uses_available_bytes_without_changing_timeout(monkeypatch) -> None:
    from uartlink.backends import PySerialBackend

    class StrictSerial:
        def __init__(self, port, *, baudrate, timeout):
            self._locked = False
            self.timeout = timeout
            self.port = port
            self.baudrate = baudrate
            self._locked = True
            self._available = iter((3, 0))
            self.read_sizes = []

        def __setattr__(self, name, value):
            if name == "timeout" and getattr(self, "_locked", False):
                raise AssertionError("backend must not reassign serial timeout")
            object.__setattr__(self, name, value)

        @property
        def in_waiting(self):
            return next(self._available)

        def read(self, size):
            self.read_sizes.append(size)
            return bytes(size)

    fake_serial = SimpleNamespace(Serial=StrictSerial)
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    backend = PySerialBackend("fake", 3_000_000, timeout=0.02)

    assert backend.read(4096) == bytes(3)
    assert backend.read(4096) == b"\x00"
    assert backend._port.read_sizes == [3, 1]
    assert backend._port.timeout == 0.02


def test_diagnostic_cli_exposes_link_budget_and_window() -> None:
    import subprocess

    script = Path(__file__).parents[1] / "examples/bluespec/uart_link/host/uart_link_diag.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, check=True
    )
    assert "--byte-budget" in result.stdout
    assert "--window" in result.stdout


def test_diagnostic_cli_rejects_echo_that_does_not_fit_budget_before_opening() -> None:
    import subprocess

    script = Path(__file__).parents[1] / "examples/bluespec/uart_link/host/uart_link_diag.py"
    result = subprocess.run(
        [sys.executable, str(script), "--port", "unused", "--byte-budget", "80"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "does not fit" in result.stderr


def test_diagnostic_cli_aborts_on_stats_failure_and_preserves_original_error(monkeypatch) -> None:
    import importlib.util

    script = Path(__file__).parents[1] / "examples/bluespec/uart_link/host/uart_link_diag.py"
    spec = importlib.util.spec_from_file_location("uart_link_diag_test", script)
    assert spec and spec.loader
    diag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diag)
    closed = []

    class FakeBackend:
        def __init__(self, *args):
            pass

    class FakeDevice:
        def __init__(self, transport, **kwargs):
            pass

        def open(self):
            pass

        def statistics(self):
            raise RuntimeError("original stats timeout")

        def abort(self):
            closed.append("abort")

        def close(self):
            raise AssertionError("close must not mask stats failure")

    monkeypatch.setattr(diag, "PySerialBackend", FakeBackend)
    monkeypatch.setattr(diag, "Device", FakeDevice)
    monkeypatch.setattr(sys, "argv", [str(script), "--port", "unused", "--stats"])
    with pytest.raises(RuntimeError, match="original stats timeout"):
        diag.main()
    assert closed == ["abort"]


@pytest.mark.parametrize(
    ("terminal_error", "mode"),
    [("retry", "request"), ("credit", "control")],
)
def test_pump_terminal_retry_errors_poison_the_device(terminal_error: str, mode: str) -> None:
    from uartlink import CreditExhausted, Device, LinkReset, RetryExhausted
    from uartlink.codec import RESERVE
    from uartlink.link import Pending

    class EmptyTransport:
        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(EmptyTransport())
    device.state.active = True
    if mode == "request":
        device.state.tx_next = 1
        device.state.pending[0] = Pending(0, b"unknown", 0.0, attempts=device.state.retry_limit)
        expected_error = RetryExhausted
    else:
        frame = device.state.poll_frame()
        control = device.state.controls[frame.seq]
        control.sent_at = 0.0
        control.attempts = min(8, max(1, RESERVE // 15))
        expected_error = CreditExhausted

    assert terminal_error == ("retry" if mode == "request" else "credit")
    with pytest.raises(expected_error):
        device.pump()
    assert not device.state.active
    assert device.state.pending == {}
    with pytest.raises(LinkReset):
        device.submit(b"must not retry")


def test_transact_timeout_reports_unknown_outcome_and_poison_device() -> None:
    from uartlink import Device, LinkReset, UnknownOutcome

    class EmptyTransport:
        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(EmptyTransport())
    device.state.active = True
    with pytest.raises(UnknownOutcome, match="unknown"):
        device.transact(b"side effect", timeout=0)
    assert not device.state.active
    assert device.state.pending == {}
    with pytest.raises(LinkReset):
        device.submit(b"do not retry")


def test_transact_control_credit_exhaustion_reports_unknown_outcome() -> None:
    from uartlink import Device, UnknownOutcome
    from uartlink.codec import RESERVE

    class EmptyTransport:
        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(EmptyTransport())
    device.state.active = True
    device.submit(b"side effect")
    frame = device.state.poll_frame()
    control = device.state.controls[frame.seq]
    control.sent_at = 0.0
    control.attempts = min(8, max(1, RESERVE // 15))
    with pytest.raises(UnknownOutcome):
        device.transact(b"second operation", timeout=1)
    assert not device.state.active


def test_control_response_requires_actual_encoded_length() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9, byte_budget=256)
    state.active = True
    frame = state.poll_frame()
    response = Frame("control_response", 0, frame.seq, 0, 10, 9)
    with pytest.raises(TypeError):
        state.accept_control_response(response)


def test_mark_reset_clears_control_and_recovery_state() -> None:
    from uartlink import ProtocolState

    state = ProtocolState(session=9)
    state.active = True
    poll = state.poll_frame()
    state.recovery_seq = 3
    state.ready_frames.append(poll)
    state.mark_reset()
    assert not state.controls
    assert not state.control_waiting
    assert state.recovery_seq is None
    assert state.ready_frames == []


def test_reset_protocol_state_cannot_reopen_old_session() -> None:
    from uartlink import LinkReset, ProtocolState

    state = ProtocolState(session=9)
    state.open_frame()
    state.mark_reset()
    with pytest.raises(LinkReset, match="reset"):
        state.open_frame()


def test_transact_protocol_error_marks_result_unknown() -> None:
    from uartlink import Device, UnknownOutcome

    class QueueTransport:
        def __init__(self, incoming: bytes) -> None:
            self.incoming = incoming

        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            data, self.incoming = self.incoming, b""
            return data

        def close(self) -> None:
            pass

    state_session = 11
    malformed = Frame("control_response", 3, 0, 0, 0, state_session).encode_reverse()
    transport = QueueTransport(malformed)
    device = Device(transport)
    device.state.session = state_session
    device.state.active = True
    device.state.poll_frame()
    with pytest.raises(UnknownOutcome):
        device.transact(b"operation", timeout=1)
    assert not device.state.active
    assert device.state.pending == {}


def test_transact_control_retry_write_failure_marks_result_unknown() -> None:
    from uartlink import Device, UnknownOutcome

    now = [10.0]
    device_ref = []

    class BrokenRetryTransport:
        calls = 0

        def write(self, data: bytes) -> int:
            self.calls += 1
            if self.calls == 1:
                device_ref[0].state.pending[0].sent_at = now[0]
                return len(data)
            raise OSError("control retry write failed")

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(BrokenRetryTransport(), clock=lambda: now[0])
    device_ref.append(device)
    device.state.active = True
    poll = device.state.poll_frame()
    device.state.note_control_transmitted(poll.seq, 0.0)
    with pytest.raises(UnknownOutcome):
        device.transact(b"operation", timeout=1)
    assert not device.state.active
    assert device.state.pending == {}


@pytest.mark.parametrize("interrupted_stage", ["write", "read"])
def test_transact_keyboard_interrupt_terminates_link_and_propagates(
    interrupted_stage: str,
) -> None:
    from uartlink import Device

    class InterruptedTransport:
        def write(self, data: bytes) -> int:
            if interrupted_stage == "write":
                raise KeyboardInterrupt
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            if interrupted_stage == "read":
                raise KeyboardInterrupt
            return b""

        def close(self) -> None:
            pass

    device = Device(InterruptedTransport())
    device.state.active = True
    with pytest.raises(KeyboardInterrupt):
        device.transact(b"operation", timeout=1)
    assert not device.state.active
    assert device.state.pending == {}


def test_transact_returns_known_result_when_final_poll_write_fails() -> None:
    from uartlink import Device

    peer = BehavioralPeer()

    class PollFailureTransport:
        def write(self, data: bytes) -> int:
            frame = Frame.decode(data)
            if frame.kind == "control" and frame.flags == 0:
                raise OSError("final POLL write failed")
            peer.accept(frame)
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return peer.read()

        def close(self) -> None:
            pass

    device = Device(PollFailureTransport())
    device.open()
    assert device.transact(b"\x30\x01\x00x") == b"\xb0x"
    assert not device.state.active


def test_wait_result_timeout_keeps_explicit_ticket_resumable() -> None:
    from uartlink import Device, RetryExhausted

    class EmptyTransport:
        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, size: int = 4096) -> bytes:
            return b""

        def close(self) -> None:
            pass

    device = Device(EmptyTransport())
    device.state.active = True
    ticket = device.submit(b"side effect")
    with pytest.raises(RetryExhausted):
        device.wait_result(ticket, timeout=0)
    assert device.state.active
    assert device.take_result(ticket) is None
