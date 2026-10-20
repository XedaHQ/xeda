"""Run the Python host state against the RTL through a bounded UART bridge."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from uartlink import Frame, ProtocolState, cobs_encode, crc32c  # noqa: E402


class Wire:
    """A UART byte every 83 clocks, with an independently paused host reader."""

    def __init__(self, dut, budget=256):
        self.dut = dut
        self.budget = budget
        self.ticks = 0
        self.bridge = bytearray()
        self.high_water = 0
        self.tx_enabled = True
        self.rx_fifo = deque()
        self.rx_high_water = 0

    async def step(self, rx=None):
        dut = self.dut
        dut.CLK.value = 0
        if rx is not None:
            self.rx_fifo.append(rx)
            self.rx_high_water = max(self.rx_high_water, len(self.rx_fifo))
            assert len(self.rx_fifo) <= 4, "RTL blocked the four-byte UART receive FIFO"
        dut.EN_rxData.value = 0
        dut.rxData_value.value = self.rx_fifo[0] if self.rx_fifo else 0
        transmit = self.tx_enabled and self.ticks % 83 == 0
        dut.EN_txData.value = 0
        await Timer(2, unit="ns")
        accepted = bool(self.rx_fifo) and int(dut.RDY_rxData.value)
        take_tx = transmit and int(dut.RDY_txData.value)
        dut.EN_rxData.value = int(accepted)
        dut.EN_txData.value = int(take_tx)
        await Timer(3, unit="ns")
        if take_tx:
            self.bridge.append(int(dut.txData.value))
            self.high_water = max(self.high_water, len(self.bridge))
            assert len(self.bridge) <= self.budget, "RTL exceeded the bridge byte budget"
        if accepted:
            self.rx_fifo.popleft()
        dut.CLK.value = 1
        await Timer(5, unit="ns")
        self.ticks += 1
        return accepted

    async def wait(self, clocks):
        for _ in range(clocks):
            await self.step()

    async def statistics(self):
        values = []
        for index in range(16):
            self.dut.linkStat_index.value = index
            await self.step()
            values.append(int(self.dut.linkStat.value))
        return values

    async def send(self, frame, corrupt=False):
        encoded = bytearray(frame.encode())
        if corrupt:
            # Change a nonzero encoded byte without changing COBS block lengths.
            encoded[-2] = 1 if encoded[-2] != 1 else 2
        await self.send_bytes(encoded)

    async def send_bytes(self, encoded):
        for value in encoded:
            await self.step(value)
            await self.wait(82)

    def read(self):
        frames = []
        while 0 in self.bridge:
            boundary = self.bridge.index(0) + 1
            encoded = bytes(self.bridge[:boundary])
            del self.bridge[:boundary]
            frames.append((Frame.decode_reverse(encoded), len(encoded)))
        return frames


def accept(state, received):
    for frame, wire_length in received:
        if frame.kind == "control_response":
            state.accept_control_response(frame, wire_length)
        else:
            state.receive_response(frame, wire_length)


@cocotb.test()
async def host_against_rtl(dut):
    wire = Wire(dut)
    dut.EN_abortRx.value = 0
    dut.EN_setResponseStall.value = 0
    dut.setResponseStall_value.value = 0
    dut.linkStat_index.value = 0
    dut.RST_N.value = 0
    await wire.wait(10)
    dut.RST_N.value = 1
    await wire.wait(10)
    state = ProtocolState(
        session=0x12345678,
        timeout=0.001,
        min_timeout=0.001,
        clock=lambda: wire.ticks * 1e-8,
    )

    # Lose the first OPEN reply. A repeated OPEN must keep the session and cursor.
    opening = state.open_frame()
    await wire.send(opening)
    await wire.wait(3000)
    first_open = wire.read()
    assert len(first_open) == 1
    assert first_open[0][0].byte_cursor == 0
    await wire.send(state.open_frame())
    await wire.wait(3000)
    repeated = wire.read()
    assert len(repeated) == 1
    assert repeated[0][0].byte_cursor == first_open[0][1]
    accept(state, repeated)
    assert state.active and state.max_frame == 209 and state.window == 8

    # An ACK beyond the completed interval invalidates the whole frame before any effect.
    bad_control = Frame("control", 0, 0x80, 1, state.grant_edge, state.session, b"")
    await wire.send(bad_control)
    await wire.wait(2000)
    assert wire.read() == [], "invalid control ACK received a success reply"
    bad_request = Frame("request", 0, 0, 1, state.grant_edge, state.session, b"bad-ack")
    await wire.send(bad_request)
    await wire.wait(2000)
    assert int(dut.dispatchCount.value) == 0, "invalid ACK dispatched an operation"
    assert wire.read() == []

    originals = [state.submit(bytes((n, 0, 0xFF))) for n in range(2)]
    for frame in originals:
        await wire.send(frame)
        state.note_transmitted(frame.seq, 0)
    await wire.wait(5000)
    replies = wire.read()
    assert len(replies) == 2
    missing = next(frame for frame, _ in replies if frame.seq == 0)
    accept(state, [(frame, size) for frame, size in replies if frame.seq != 0])
    assert state.accepted_next == 2 and state.delivery_ack == 0
    assert 0 in state.pending and int(dut.dispatchCount.value) == 2
    recovery = state.retries_due(10)
    assert len(recovery) == 1 and recovery[0].kind == "control"
    await wire.send(recovery[0])
    await wire.wait(3000)
    accept(state, wire.read())
    replay = state.take_ready_frames()
    assert len(replay) == 1 and replay[0].seq == 0
    await wire.send(replay[0])
    await wire.wait(3000)
    replayed = wire.read()
    if not replayed:
        dut._log.error("Replay stalled; link statistics: %s", await wire.statistics())
    assert len(replayed) == 1 and replayed[0][0].flags & 2
    assert replayed[0][0].payload == missing.payload
    accept(state, replayed)
    assert state.delivery_ack == 2 and int(dut.dispatchCount.value) == 2
    assert int.from_bytes(state.completed.pop(0)[:4], "little") == 1
    assert int.from_bytes(state.completed.pop(1)[:4], "little") == 2

    # Control recovery must remain live while the application pauses its response payload.
    dut.setResponseStall_value.value = 1
    dut.EN_setResponseStall.value = 1
    await wire.step()
    dut.EN_setResponseStall.value = 0
    paused = state.submit(b"pause")
    await wire.send(paused)
    await wire.wait(1000)
    await wire.send(state.poll_frame())
    await wire.wait(2000)
    polled = wire.read()
    if not polled:
        dut._log.error("Stalled-service POLL statistics: %s", await wire.statistics())
    assert len(polled) == 1 and polled[0][0].kind == "control_response"
    accept(state, polled)
    assert paused.seq in state.pending and state.pending[paused.seq].accepted
    dut.setResponseStall_value.value = 0
    dut.EN_setResponseStall.value = 1
    await wire.step()
    dut.EN_setResponseStall.value = 0
    await wire.wait(3000)
    accept(state, wire.read())
    assert state.completed.pop(paused.seq)[4:] == b"pause"
    assert int(dut.dispatchCount.value) == 3

    # The reader pauses while eight requests execute. The bridge must remain bounded.
    await wire.send(state.poll_frame())
    await wire.wait(3000)
    accept(state, wire.read())
    for n in range(8):
        frame = state.submit(bytes((n,)) * 24)
        await wire.send(frame)
    await wire.wait(30000)
    partial = wire.read()
    response_count = sum(frame.kind == "response" for frame, _ in partial)
    assert 0 < response_count < 8, "credit must pause a burst before bridge overflow"
    accept(state, partial)
    await wire.send(state.poll_frame())
    await wire.wait(30000)
    accept(state, wire.read())
    assert not state.pending and len(state.completed) == 8
    assert int(dut.dispatchCount.value) == 11
    state.completed.clear()

    # A bad CRC causes no dispatch. A later retry is accepted once.
    frame = state.submit(b"crc-loss")
    await wire.send(frame, corrupt=True)
    await wire.wait(3000)
    assert int(dut.dispatchCount.value) == 11
    await wire.send(frame)
    await wire.wait(4000)
    accept(state, wire.read())
    assert state.completed.pop(frame.seq)[4:] == b"crc-loss"
    assert int(dut.dispatchCount.value) == 12

    # More than 512 decoded bytes must not wrap the receive counter into a valid request.
    raw = (
        bytes((0x40, state.tx_next, state.delivery_ack))
        + state.grant_edge.to_bytes(2, "little")
        + state.session.to_bytes(4, "little")
        + b"x" * 600
    )
    await wire.send_bytes(cobs_encode(raw + crc32c(raw).to_bytes(4, "little")) + b"\x00")
    await wire.wait(1000)
    assert int(dut.dispatchCount.value) == 12
    frame = state.submit(b"")
    await wire.send(frame)
    await wire.wait(3000)
    accept(state, wire.read())
    assert len(state.completed.pop(frame.seq)) == 4
    assert int(dut.dispatchCount.value) == 13

    # Application headers must carry delivery ACK and new credit across sequence wrap.
    for n in range(260):
        frame = state.submit(bytes((n & 255,)))
        await wire.send(frame)
        await wire.wait(2500)
        accept(state, wire.read())
        assert state.completed.pop(frame.seq)[4:] == bytes((n & 255,))
    assert int(dut.dispatchCount.value) == 273
    assert wire.high_water <= 256

    # All eight delivered results still occupy FPGA slots until the next valid ACK arrives.
    await wire.send(state.poll_frame())
    await wire.wait(2000)
    accept(state, wire.read())
    batch = [state.submit(bytes((n,))) for n in range(8)]
    for frame in batch:
        await wire.send(frame)
    await wire.wait(5000)
    accept(state, wire.read())
    assert not state.pending and len(state.completed) == 8
    cached_results = {}
    for n, frame in enumerate(batch):
        cached_results[frame.seq] = state.completed.pop(frame.seq)
        assert cached_results[frame.seq][4:] == bytes((n,))
    # Speculative use of an occupied request store must preserve the cached result on rejection.
    malformed = Frame(
        "request", 0, state.tx_next, state.delivery_ack, state.grant_edge, state.session, b"bad-crc"
    )
    await wire.send(malformed, corrupt=True)
    await wire.wait(1000)
    malformed = Frame(
        "request",
        0,
        state.tx_next,
        (state.delivery_ack + 1) & 255,
        state.grant_edge,
        state.session,
        b"bad-ack",
    )
    await wire.send(malformed)
    await wire.wait(1000)
    assert wire.read() == [] and int(dut.dispatchCount.value) == 281
    await wire.send(batch[0])
    await wire.wait(3000)
    preserved = wire.read()
    assert len(preserved) == 1 and preserved[0][0].payload == cached_results[batch[0].seq]
    accept(state, preserved)
    # This request must use its own ACK to release a slot, without a POLL or timeout retry.
    ninth = state.submit(b"ninth")
    await wire.send(ninth)
    await wire.wait(4000)
    accept(state, wire.read())
    assert state.completed.pop(ninth.seq)[4:] == b"ninth"
    assert int(dut.dispatchCount.value) == 282

    # The endpoint must bound its result even for the largest legal request.
    maximum = bytes(range(state.max_frame - 13))
    boundary = state.submit(maximum)
    await wire.send(boundary)
    await wire.wait(20000)
    accept(state, wire.read())
    result = state.completed.pop(boundary.seq)
    assert len(result) == state.max_frame - 13
    assert int.from_bytes(result[:4], "little") == 283
    assert result[4:] == maximum[: len(result) - 4]
    following = state.submit(b"after-max")
    await wire.send(following)
    await wire.wait(4000)
    accept(state, wire.read())
    assert state.completed.pop(following.seq)[4:] == b"after-max"
    assert int(dut.dispatchCount.value) == 284

    # CLOSE carries the final delivery ACK. Confirmation must release the old session.
    await wire.send(state.close_frame())
    await wire.wait(3000)
    accept(state, wire.read())
    assert not state.active
    small = ProtocolState(session=0x24681357, byte_budget=80, window=2)
    opening_small = small.open_frame()
    await wire.send(opening_small)
    await wire.wait(3000)
    assert len(wire.read()) == 1  # Lose the first reply with the smallest supported budget.
    await wire.send(small.open_frame())
    await wire.wait(3000)
    accept(small, wire.read())
    assert small.active and small.max_frame == 33 and small.window == 2

    # A delayed OPEN retry must not change a CAPABILITIES reply already in the TX pipeline.
    wire.tx_enabled = False
    await wire.send(small.capabilities_frame())
    await wire.wait(20)
    await wire.send(opening_small)
    wire.tx_enabled = True
    await wire.wait(6000)
    controls = wire.read()
    capabilities = [frame for frame, _ in controls if frame.flags == 1]
    assert len(capabilities) == 1
    assert capabilities[0].payload == b"\xf0\x00\x08\x01"
    accept(small, controls)
    await wire.send(small.close_frame())
    await wire.wait(3000)
    accept(small, wire.read())
    assert not small.active
    dut._log.info(
        "PASS: host/RTL recovery, replay, CRC, credit, sequence wrap and session controls"
    )
