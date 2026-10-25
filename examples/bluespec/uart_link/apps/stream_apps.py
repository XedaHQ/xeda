"""Run the shipped blocking host clients through the complete application RTL.

The backend crosses cocotb's bridge/resume boundary. It drives the public byte methods or
8N1 UART pins and uses simulation time for every host deadline. Expected cryptographic bytes
come from independent checked-in Ascon v1.2 C-reference fixtures.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from collections import deque
from itertools import islice
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.task import bridge, resume
from cocotb.triggers import Event, FallingEdge, First, Timer
from cocotb.utils import get_sim_time

ROOT = Path(os.environ.get("STREAM_SOURCE_ROOT", Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(ROOT / "examples/bluespec/uart_link/host"))
from leanlink import codec  # noqa: E402
from leanlink.applications import (  # noqa: E402
    AsconClient,
    _ascon_stream_words40,
    AuthenticationError,
    GcdClient,
    decode_ascon_output,
    read_ascon_vectors,
)
from leanlink.device import LinkDevice  # noqa: E402
from leanlink.protocol import HostConfig  # noqa: E402
from leanlink.stream import StreamProtocolError  # noqa: E402


class RtlBackend:
    """A byte transport whose only peer is the RTL's public interface."""

    def __init__(self, dut, serial):
        self.dut = dut
        self.serial = serial
        self.pending = deque()
        self.received = bytearray()
        self.frame = bytearray()
        self.available = Event()
        self.device = None
        self.drop_next_response = False
        self.dropped_responses = 0
        self.frames = 0
        self.bit_ns = 1000 / 12  # 12 Mbaud, 100 MHz RTL clock

    async def start(self):
        self.dut.RST_N.value = 0
        if self.serial:
            self.dut.rxd.value = 1
            self.dut.sw.value = 0
            self.dut.btn.value = 0
        else:
            self.dut.EN_rx_put.value = 0
            self.dut.EN_tx_get.value = 0
            self.dut.rx_put.value = 0
        # The native driver avoids Python callbacks on every clock edge.
        Clock(self.dut.CLK, 10, unit="ns", impl="gpi").start()
        await Timer(100, unit="ns")
        # Release reset on the inactive clock edge before starting traffic.
        await FallingEdge(self.dut.CLK)
        self.dut.RST_N.value = 1
        if self.serial:
            cocotb.start_soon(self._uart_send())
            cocotb.start_soon(self._uart_receive())
        else:
            cocotb.start_soon(self._byte_wire())
        await Timer(200, unit="ns")

    def _receive_byte(self, value):
        if value:
            self.frame.append(value)
            return
        if not self.frame:
            return
        wire = bytes(self.frame)
        self.frame.clear()
        self.frames += 1
        sid = self.device.protocol.sid if self.device and self.device.protocol else 0
        parsed = codec.check_fpga_frame(wire, sid, 8, 255).frame
        if self.drop_next_response and parsed and parsed["kind"] == "data":
            self.drop_next_response = False
            self.dropped_responses += 1
            return
        self.received.extend(wire + b"\0")
        self.available.set()

    async def _byte_wire(self):
        cycle = 0
        while True:
            await FallingEdge(self.dut.CLK)
            cycle += 1
            self.dut.EN_rx_put.value = 0
            self.dut.EN_tx_get.value = 0
            if cycle % 12 == 0:
                if self.pending and int(self.dut.RDY_rx_put.value):
                    self.dut.rx_put.value = self.pending.popleft()
                    self.dut.EN_rx_put.value = 1
                if int(self.dut.RDY_tx_get.value):
                    self._receive_byte(int(self.dut.tx_get.value))
                    self.dut.EN_tx_get.value = 1

    async def _uart_send(self):
        while True:
            if not self.pending:
                await Timer(20, unit="ns")
                continue
            value = self.pending.popleft()
            for bit in [0, *((value >> i) & 1 for i in range(8)), 1]:
                self.dut.rxd.value = bit
                await Timer(self.bit_ns, unit="ns", round_mode="round")

    async def _uart_receive(self):
        while True:
            await FallingEdge(self.dut.txd)
            await Timer(self.bit_ns * 1.5, unit="ns", round_mode="round")
            value = 0
            for index in range(8):
                value |= int(self.dut.txd.value) << index
                await Timer(self.bit_ns, unit="ns", round_mode="round")
            assert int(self.dut.txd.value) == 1, "FPGA emitted a bad UART stop bit"
            self._receive_byte(value)
            await Timer(self.bit_ns * 0.25, unit="ns", round_mode="round")

    @resume
    async def write(self, data):
        self.pending.extend(data)

    @resume
    async def read(self, max_bytes, timeout):
        if not self.received and timeout > 0:
            self.available.clear()
            await First(self.available.wait(), Timer(timeout, unit="sec", round_mode="ceil"))
        result = bytes(self.received[:max_bytes])
        del self.received[:max_bytes]
        return result

    @resume
    async def purge(self):
        self.received.clear()
        self.frame.clear()
        self.pending.clear()

    def close(self):
        pass

    @resume
    async def clock(self):
        return get_sim_time(unit="sec")

    @resume
    async def sleep(self, seconds):
        if seconds > 0:
            await Timer(seconds, unit="sec", round_mode="ceil")


def check_gcd(device, backend):
    client = GcdClient(device, clock=backend.clock)
    client.reset()
    rng = random.Random(0x55415254)
    pairs = [(0, 0), (0, 19), (19, 0), (1, 0xFFFFFFFF), (0xFFFFFFFF, 0xFFFFFFFF)]
    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(256)]
    backend.drop_next_response = True
    assert client.gcd_many(pairs) == [math.gcd(a, b) for a, b in pairs]
    assert backend.dropped_responses == 1
    # The old client's session binding must fail after reconnect. RESET recovers it.
    device.open(timeout=0.1)
    try:
        client.gcd(12, 18)
    except StreamProtocolError:
        pass
    else:
        raise AssertionError("GCD accepted a new session without RESET")
    client.reset()
    assert client.gcd(12, 18) == 6


def check_ascon(device, backend, variant_a):
    client = AsconClient(device, clock=backend.clock)
    client.reset()
    completed = 0
    for row in read_ascon_vectors(ROOT / "examples/bluespec/ascon/tb/AsconVectors.bsv"):
        if row["variant_a"] != variant_a:
            continue
        operation = row["operation"]
        if operation == "encrypt":
            got = client.encrypt(
                row["key"] if row["new_key"] else None,
                row["nonce"],
                row["associated_data"],
                row["message"],
            )
            assert got == row["expected"], (completed, operation)
        elif operation == "decrypt":
            got = client.decrypt(
                row["key"] if row["new_key"] else None,
                row["nonce"],
                row["associated_data"],
                row["message"] + row["expected"][-16:],
            )
            assert got == row["expected"][:-16], (completed, operation)
        else:
            assert client.hash(row["message"]) == row["expected"], (completed, operation)
        completed += 1
    assert completed == 61
    fixture = json.loads(
        (ROOT / "examples/bluespec/ascon/tb/AsconStreamLongVectors.vectors").read_text()
    )
    assert fixture["reference_commit"] == "4b90d3d689969f9fd44919345d2a8a646a8e3639"
    row = fixture["cases"][int(variant_a)]
    key, nonce, ad, message, expected = (
        bytes.fromhex(row[name]) for name in ("key", "nonce", "ad", "message", "ciphertext_tag")
    )
    backend.drop_next_response = True
    assert client.encrypt(key, nonce, ad, message) == expected
    assert backend.dropped_responses == 1
    assert client.hash(bytes.fromhex(fixture["hash"]["message"])) == bytes.fromhex(
        fixture["hash"]["digest"]
    )
    # Hash must preserve the previously loaded key in both the client and the core.
    assert client.decrypt(None, nonce, ad, expected) == message
    damaged = expected[:-1] + bytes([expected[-1] ^ 1])
    try:
        client.decrypt(None, nonce, ad, damaged)
    except AuthenticationError:
        pass
    else:
        raise AssertionError("Ascon returned plaintext for an invalid authentication tag")
    # Incomplete and unknown exchange packets must not reach the application.
    client.reset()
    assert device.transact(b"\x01\x00") == b"\x01\x00"
    assert device.transact(b"\x02") == b"\x01\x00"
    # Malformed application words fail explicitly and remain drainable until RESET.
    output = client.stream.transfer([bytes.fromhex("ff00000000")], output_words=1)
    error = decode_ascon_output(output[0])
    assert error.kind == 3 and error.last and error.end_of_operation and error.keep == 15
    accepted, output = client.stream.exchange([bytes.fromhex("1002000000")] * 16)
    assert 0 < accepted <= 16 and not output
    client.reset()
    assert client.hash(b"") == bytes.fromhex(
        "7346bc14f036e87ae03d0997913088f5f68411434b3cf8b54fa796a80d251f91"
    )
    # Lose the session while the core waits for the remaining key words.
    partial = list(islice(_ascon_stream_words40("encrypt", key=key, nonce=nonce), 3))
    accepted, output = client.stream.exchange(partial)
    assert accepted == len(partial) and not output
    device.open(timeout=0.1)
    try:
        client.hash(b"")
    except StreamProtocolError:
        pass
    else:
        raise AssertionError("Ascon accepted a new session without RESET")
    client.reset()
    assert client.hash(b"") == bytes.fromhex(
        "7346bc14f036e87ae03d0997913088f5f68411434b3cf8b54fa796a80d251f91"
    )


@cocotb.test()
async def application_clients(dut):
    application = os.environ["STREAM_APP"]
    backend = RtlBackend(dut, bool(int(os.environ["STREAM_SERIAL"])))
    await backend.start()

    def run():
        config = HostConfig(
            bridge_budget=4096,
            byte_time=10 / 12_000_000 if backend.serial else 120e-9,
            rto=0.002,
            rewind_timeout=0.003,
            poll_interval=0.001,
            open_timeout=0.003,
            resync_quiet=0.02,
        )
        device = LinkDevice(
            backend, config, clock=backend.clock, sleep=backend.sleep, rng=random.Random(0x5354524D)
        )
        backend.device = device
        device.open(timeout=0.1)
        if application == "gcd":
            check_gcd(device, backend)
        else:
            check_ascon(device, backend, application == "ascon128a")

    await bridge(run)()
    dut._log.info(
        "Checked shipped %s client through %s in %d FPGA frames",
        application,
        "UART pins" if backend.serial else "byte ports",
        backend.frames,
    )
