"""Tests of the host library of the `uart_link` example (`host/leanlink`).

The library has no dependency outside the standard library. These tests load it from the example
by path, so they need no installation. They cover the codec, the host protocol state machine, the
device with its backends (pyserial and pyftdi are replaced by fakes), a frame-level model of the
FPGA, and the protocol simulation with fault injection.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import random
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.python_compat

HOST_DIR = Path(__file__).parent.parent / "examples" / "bluespec" / "uart_link" / "host"
PACKAGE = "uart_link_leanlink"


def _load_package() -> types.ModuleType:
    """Import `host/leanlink` under the name PACKAGE, without a change to `sys.path` and without
    bytecode files in the checkout."""
    directory = HOST_DIR / "leanlink"
    spec = importlib.util.spec_from_file_location(
        PACKAGE, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        for name in ("codec", "protocol", "device", "backends", "fpga_model", "sim"):
            importlib.import_module(f"{PACKAGE}.{name}")
    finally:
        sys.dont_write_bytecode = written
    return module


leanlink = _load_package()
lc = leanlink.codec
fm = leanlink.fpga_model
hp = leanlink.protocol
sim = leanlink.sim
BackendUnavailable = leanlink.BackendUnavailable
HostConfig = leanlink.HostConfig
HostProtocol = leanlink.HostProtocol
LinkDevice = leanlink.LinkDevice
LoopbackBackend = leanlink.LoopbackBackend
OpenFailed = leanlink.OpenFailed
ResponseTruncated = leanlink.ResponseTruncated
SerialBackend = leanlink.SerialBackend
SessionClosed = leanlink.SessionClosed
State = leanlink.State
Status = leanlink.Status
UnknownOutcome = leanlink.UnknownOutcome


# ---------------------------------------------------------------------------------------------
# Helpers


SID = 0x11223344


class Clock:
    """Manual clock for LinkDevice."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def log2(w: int) -> int:
    return w.bit_length() - 1


def status_wire(
    sid: int = SID,
    w: int = 4,
    max_len: int = 255,
    cursor: int = 12,
    seq: int = 0,
    exp: int = 0,
    trunc: bool = False,
) -> bytes:
    ctl = lc.fpga_ctl(trunc=trunc, log2w=log2(w))
    return lc.encode_fpga_wire(
        lc.build_fpga_frame(ctl, seq, exp, cursor, b"", sid, len_field=max_len)
    )


def response_wire(
    sid: int = SID,
    w: int = 4,
    seq: int = 0,
    exp: int = 1,
    cursor: int = 40,
    payload: bytes = b"",
    trunc: bool = False,
    len_field: int | None = None,
) -> bytes:
    ctl = lc.fpga_ctl(data=True, trunc=trunc, log2w=log2(w))
    return lc.encode_fpga_wire(
        lc.build_fpga_frame(ctl, seq, exp, cursor, payload, sid, len_field=len_field)
    )


def parse_host(wire: bytes, sid: int = SID, w: int = 4, max_len: int = 255) -> dict:
    """Parse a host wire frame (delimiter included). Fails the test if it is invalid."""
    assert wire[-1] == 0 and 0 not in wire[:-1]
    frame = fm.check_host(wire[:-1], sid, w, max_len)
    assert frame is not None, "host frame is invalid"
    return frame


def drain(proto: HostProtocol, now: float) -> list[bytes]:
    out = []
    while True:
        f = proto.next_frame(now)
        if f is None:
            return out
        out.append(f)


def config(**kw) -> HostConfig:
    base = dict(
        bridge_budget=1024,
        rsp_limit=255,
        rto=0.01,
        rewind_timeout=0.02,
        poll_interval=0.03,
        resync_quiet=0.05,
        max_retries=3,
        resync_retries=2,
        open_timeout=0.01,
        open_retries=3,
    )
    base.update(kw)
    return HostConfig(**base)


def started(w: int = 4, now: float = 0.0, cursor: int = 12, **kw) -> HostProtocol:
    """A protocol in the ACTIVE state (the OPEN reply is processed)."""
    proto = HostProtocol(config(**kw), sid=SID)
    proto.start(now)
    assert proto.next_frame(now) is not None  # OPEN
    proto.feed(status_wire(SID, w=w, cursor=cursor), now)
    assert proto.state.value == "active", proto.state
    return proto


class ModelHost:
    """A minimal host for FpgaModel tests: builds frames, feeds them, and reads the replies."""

    def __init__(self, model: fm.FpgaModel, sid: int = SID) -> None:
        self.m = model
        self.sid = sid
        self.t = 1e-3
        self.seen = 0
        self.W = model.W

    def send(self, decoded: bytes, gap: float = 0.0) -> None:
        wire = lc.encode_host_wire(decoded)
        self.t += gap
        self.m.rx_stream(wire, self.t)
        self.t += len(wire) * self.m.byte_time

    def open(
        self,
        sid: int | None = None,
        rsp_limit: int = 255,
        grant: int = 0,
        ctl_extra: int = 0,
        gap: float = 0.0,
    ) -> None:
        sid = self.sid if sid is None else sid
        body = bytearray(lc.build_open_frame(sid, rsp_limit, grant=grant))
        if ctl_extra:
            body[0] |= ctl_extra
            body[-4:] = lc.crc32c(bytes(body[:-4])).to_bytes(4, "little")
        self.send(bytes(body), gap)

    def frame(
        self,
        *,
        data: bool = False,
        payload: bytes = b"",
        seq: int = 0,
        ack: int = 0,
        grant: int = 1000,
        poll: bool = False,
        rewind: int = 0,
        resync: bool = False,
        sid: int | None = None,
    ) -> bytes:
        ctl = lc.host_ctl(data=data, poll=poll, rewind=rewind) | (fm.HOST_RESYNC if resync else 0)
        return lc.build_host_frame(ctl, seq, ack, grant, payload, self.sid if sid is None else sid)

    def req(self, seq: int, payload: bytes = b"", ack: int = 0, grant: int = 1000, **kw) -> None:
        self.send(self.frame(data=True, payload=payload, seq=seq, ack=ack, grant=grant, **kw))

    def hdr(self, ack: int = 0, grant: int = 1000, **kw) -> None:
        self.send(self.frame(ack=ack, grant=grant, **kw))

    def settle(self, dt: float = 5e-3) -> list[dict]:
        """Advance time and return the new FPGA frames, parsed (status/data)."""
        self.t += dt
        self.m.advance(self.t)
        out = []
        for em in self.m.pop_emissions():
            if em.kind == "delimiter":  # the byte after power-up belongs to no frame
                continue
            frame = None
            for sid in (self.m.sid, self.sid, 0):
                frame = lc.check_fpga_frame(em.wire[:-1], sid, self.W, None).frame
                if frame is not None:
                    break
            assert frame is not None, "FPGA frame is invalid"
            frame = dict(frame)
            frame["wire"] = em.wire
            out.append(frame)
        return out

    def start_session(self, **kw) -> list[dict]:
        self.open(**kw)
        out = self.settle()
        assert out and out[0]["kind"] == "status"
        return out


# ---------------------------------------------------------------------------------------------
# Codec


def test_codec_known_values() -> None:
    assert lc.crc32c(b"123456789") == 0xE3069283
    for data in (b"", b"\x00", b"\x00\x00", bytes(range(1, 255))[:253], b"\x01" * 253):
        assert lc.cobs_decode(lc.cobs_encode(data)) == data
        assert lc.rcobs_decode(lc.rcobs_encode(data)) == data


@pytest.mark.parametrize(
    "data, cobs, rcobs",
    [
        (b"", b"\x01", b"\x01"),
        (b"\x00", b"\x01\x01", b"\x01\x01"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33", b"\x11\x22\x03\x33\x02"),
        # a run of 254 nonzero bytes is one full block: the code byte 0xFF
        (b"\x01" * 254, b"\xff" + b"\x01" * 254, b"\x01" * 254 + b"\xff\x01"),
        (
            b"\x01" * 255,
            b"\xff" + b"\x01" * 254 + b"\x02\x01",
            b"\x01" * 254 + b"\xff\x01\x02",
        ),
        (b"\x01" * 254 + b"\x00", b"\xff" + b"\x01" * 254 + b"\x01\x01", None),
    ],
    ids=["empty", "zero", "mixed", "run-254", "run-255", "run-254-zero"],
)
def test_cobs_vectors_with_the_full_block_code(data, cobs, rcobs) -> None:
    assert lc.cobs_encode(data) == cobs
    assert lc.cobs_decode(cobs) == data
    if rcobs is not None:
        assert lc.rcobs_encode(data) == rcobs
    assert lc.rcobs_decode(lc.rcobs_encode(data)) == data


def test_the_codec_rejects_trunc_in_a_status_frame() -> None:
    ctl = lc.fpga_ctl(trunc=True, log2w=2)
    status = lc.encode_fpga_wire(lc.build_fpga_frame(ctl, 0, 0, 12, b"", SID, len_field=255))
    verdict = lc.check_fpga_frame(status[:-1], SID, 4, 255)
    assert verdict.frame is None and verdict.failures == ("header",)
    clean = lc.fpga_ctl(log2w=2)
    status = lc.encode_fpga_wire(lc.build_fpga_frame(clean, 0, 0, 12, b"", SID, len_field=255))
    assert lc.check_fpga_frame(status[:-1], SID, 4, 255).frame is not None
    # TRUNC is allowed in a DATA frame
    data = lc.fpga_ctl(data=True, trunc=True, log2w=2)
    wire = lc.encode_fpga_wire(lc.build_fpga_frame(data, 0, 1, 40, b"abc", SID))
    assert lc.check_fpga_frame(wire[:-1], SID, 4, 255).frame["trunc"] is True


def test_the_codec_accepts_the_resync_bit_and_judges_the_other_rules() -> None:
    # Host ctl bit 0 is RESYNC. check_host reports it and judges the rest with the codec.
    ctl = lc.host_ctl(poll=True, rewind=2) | 1
    dec = lc.build_host_frame(ctl, 1, 2, 300, b"", 0x01020304)
    frame = fm.check_host(lc.cobs_encode(dec), 0x01020304, 4, 255)
    assert frame is not None and frame["resync"] and frame["poll"] and frame["rewind"] == 2
    bad = bytearray(dec)
    bad[-1] ^= 1
    assert fm.check_host(lc.cobs_encode(bytes(bad)), 0x01020304, 4, 255) is None
    other = lc.build_host_frame(ctl ^ 0x40, 1, 2, 300, b"", 0x01020304)  # version broken
    assert fm.check_host(lc.cobs_encode(other), 0x01020304, 4, 255) is None
    plain = lc.build_host_frame(ctl & ~1, 1, 2, 300, b"", 0x01020304)
    assert fm.check_host(lc.cobs_encode(plain), 0x01020304, 4, 255)["resync"] is False


# ---------------------------------------------------------------------------------------------
# Device and backends


def make_device(w: int = 4, rsp_limit: int = 255, core_delay: float = 0.0, **kw):
    clock = Clock()
    model = fm.FpgaModel(w, 256, core_delay=core_delay)
    backend = LoopbackBackend(fm.ModelPeer(model), clock)
    cfg = HostConfig(
        bridge_budget=2048,
        rsp_limit=rsp_limit,
        byte_time=model.byte_time,
        open_timeout=0.01,
        rto=0.01,
        rewind_timeout=0.02,
        poll_interval=0.03,
        resync_quiet=0.05,
        **kw,
    )
    return LinkDevice(backend, cfg, clock=clock, sleep=clock.sleep), model, clock


def test_open_and_transact_return_the_response() -> None:
    dev, model, _clock = make_device()
    dev.open(timeout=1.0)
    assert dev.active and dev.protocol.W == 4
    assert dev.transact(b"hello") == (0).to_bytes(4, "little") + b"hello"
    assert dev.transact(b"") == (1).to_bytes(4, "little")
    assert model.executed == [b"hello", b""]


def test_submit_many_and_wait_in_any_order() -> None:
    dev, model, _clock = make_device(w=2)
    dev.open(timeout=1.0)
    tickets = [dev.submit(bytes([k]) * (k + 1)) for k in range(9)]
    for k in reversed(range(9)):
        result = dev.wait_result(tickets[k], timeout=1.0)
        assert result.status is Status.OK and result.data[4:] == bytes([k]) * (k + 1)
    assert len(model.executed) == 9


@pytest.mark.parametrize("w", [1, 2, 8])
def test_completed_requests_release_replay_storage_before_results_are_taken(w: int) -> None:
    """Completed input packets need no replay storage, even with results waiting for the caller."""
    p = started(w=w)
    tickets = []
    for index in range(257):
        now = index * 0.001
        tickets.append(p.submit(index.to_bytes(4, "little") + bytes(235)))
        sent = [parse_host(wire, w=w) for wire in drain(p, now)]
        assert [frame["seq"] for frame in sent if frame["kind"] == "data"] == [index % (2 * w)]
        p.feed(
            response_wire(
                w=w,
                seq=index % (2 * w),
                exp=(index + 1) % (2 * w),
                cursor=u16(12 + 16 * (index + 1)),
                payload=index.to_bytes(4, "little"),
                trunc=bool(index % 2),
            ),
            now,
        )
        assert p.outstanding == 0
        assert not p._reqs, "completed requests still retain their input packets"
    for index in reversed(range(257)):
        result = p.take_result(tickets[index])
        assert result is not None
        assert result.status is (Status.TRUNCATED if index % 2 else Status.OK)
        assert result.data == index.to_bytes(4, "little")


def test_pruned_request_history_keeps_retries_and_rewind_correct_across_sequence_wrap() -> None:
    p = started(w=4)
    cursor = 12
    for batch in range(12):
        base = 4 * batch
        now = batch * 0.1
        payloads = [(base + index).to_bytes(4, "little") for index in range(4)]
        tickets = [p.submit(payload) for payload in payloads]
        sent = [parse_host(wire) for wire in drain(p, now)]
        assert [frame["payload"] for frame in sent if frame["kind"] == "data"] == payloads
        for index in range(2):
            cursor += 16
            p.feed(
                response_wire(
                    seq=(base + index) % 8,
                    exp=(base + 2) % 8,
                    cursor=cursor,
                    payload=payloads[index],
                ),
                now + 0.001 * (index + 1),
            )
        assert len(p._reqs) == 2
        retry = [parse_host(wire) for wire in drain(p, now + 0.0121)]
        data = [frame for frame in retry if frame["kind"] == "data"]
        assert [frame["seq"] for frame in data] == [(base + 2) % 8, (base + 3) % 8]
        assert [frame["payload"] for frame in data] == payloads[2:]
        cursor += 12
        p.feed(status_wire(exp=(base + 4) % 8, cursor=cursor), now + 0.013)
        replay = [parse_host(wire) for wire in drain(p, now + 0.0331)]
        assert len(replay) == 1 and replay[0]["kind"] == "header"
        assert replay[0]["ack"] == (base + 2) % 8
        assert replay[0]["rewind"] == (batch + 1) % 4
        for index in range(2, 4):
            cursor += 16
            wire = response_wire(
                seq=(base + index) % 8,
                exp=(base + 4) % 8,
                cursor=cursor,
                payload=payloads[index],
            )
            p.feed(wire, now + 0.032 + index * 0.001)
        p.feed(wire, now + 0.036)  # A duplicate reply cannot remove another request.
        assert p.outstanding == 0 and not p._reqs
        for index in reversed(range(4)):
            result = p.take_result(tickets[index])
            assert result is not None and result.status is Status.OK
            assert result.data == payloads[index]


def test_terminal_session_releases_request_storage_and_keeps_ticket_results() -> None:
    p = started(w=2)
    tickets = [p.submit(bytes([index]) * 239) for index in range(3)]
    drain(p, 0.0)
    p.abort(0.001)
    assert not p._reqs, "a terminal session still retains request packets"
    assert not p._queue
    for index, ticket in enumerate(tickets):
        result = p.take_result(ticket)
        assert result is not None and result.status is Status.UNKNOWN_OUTCOME
        assert result.was_sent is (index < 2)


def test_abandon_ignores_inactive_tickets_and_releases_live_abandonment() -> None:
    p = started(w=4)
    completed = p.submit(b"completed")
    drain(p, 0.001)
    p.feed(response_wire(seq=0, exp=1, cursor=40, payload=b"done"), 0.002)
    assert p.peek_result(completed).data == b"done"

    # Abandoning a completed result still discards that result, as the API promises.
    p.abandon(completed)
    assert p.take_result(completed) is None and not p._abandoned

    stale = p.submit(b"already taken")
    drain(p, 0.003)
    p.feed(response_wire(seq=1, exp=2, cursor=60, payload=b"taken"), 0.004)
    assert p.take_result(stale).data == b"taken"

    # A stale completed ticket and an ID never returned by submit must not leave tombstones.
    p.abandon(stale)
    p.abandon(1000)
    assert not p._abandoned

    # A real outstanding ticket still suppresses its result and clears its tombstone on reply.
    live = p.submit(b"live")
    drain(p, 0.005)
    p.abandon(live)
    assert p._abandoned == {live}
    p.feed(response_wire(seq=2, exp=3, cursor=80, payload=b"ignored"), 0.006)
    assert p.take_result(live) is None
    assert not p._abandoned


def test_truncated_response_raises_with_the_partial_data() -> None:
    dev, _model, _clock = make_device(rsp_limit=20)  # responseMax 8
    dev.open(timeout=1.0)
    try:
        dev.transact(b"0123456789")
    except ResponseTruncated as exc:
        assert exc.partial == (0).to_bytes(4, "little") + b"0123"
    else:
        raise AssertionError("TRUNC must raise")


def test_wait_result_times_out_without_ending_the_session() -> None:
    dev, _model, _clock = make_device(core_delay=0.5)
    dev.open(timeout=1.0)
    ticket = dev.submit(b"slow")
    try:
        dev.wait_result(ticket, timeout=0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError
    assert dev.active
    assert dev.wait_result(ticket, timeout=2.0).status is Status.OK


def test_abort_gives_unknown_outcome_and_a_new_session_works() -> None:
    dev, model, _clock = make_device(core_delay=5e-3)
    dev.open(timeout=1.0)
    t = dev.submit(b"in flight")
    dev.pump()
    dev.abort()
    result = dev.take_result(t)
    assert result.status is Status.UNKNOWN_OUTCOME
    try:
        dev.submit(b"x")
    except Exception as exc:
        assert type(exc).__name__ == "SessionClosed"
    else:
        raise AssertionError
    dev.open(timeout=1.0)  # OPEN with a busy core: it waits for the request in progress
    assert dev.transact(b"fresh") == (1).to_bytes(4, "little") + b"fresh"
    assert model.executed == [b"in flight", b"fresh"]


def test_wait_result_returns_a_ready_result_before_it_pumps_the_device() -> None:
    dev, _model, _clock = make_device()
    dev.open(timeout=1.0)
    ticket = dev.submit(b"in flight")
    dev.abort()  # the first session ends: the ticket has the result UNKNOWN_OUTCOME
    dev.open(timeout=1.0)  # a new session; the old ticket belongs to the old protocol
    pumped = []
    real = dev.pump
    dev.pump = lambda wait=0.0: (pumped.append(wait), real(wait))[1]
    assert dev.wait_result(ticket, timeout=1.0).status is Status.UNKNOWN_OUTCOME
    assert not pumped


def test_a_transact_that_times_out_leaves_no_ticket_behind() -> None:
    dev, model, _clock = make_device(core_delay=0.5)
    dev.open(timeout=1.0)
    with pytest.raises(TimeoutError):
        dev.transact(b"slow", timeout=0.01)
    assert dev.active and not dev._tickets
    # the request still runs, and its result is dropped when it arrives
    for _ in range(2000):
        dev.pump(0.001)
        if model.executed:
            break
    assert model.executed == [b"slow"]
    assert not dev.protocol._results and not dev.protocol._abandoned
    assert dev.transact(b"next", timeout=2.0) == (1).to_bytes(4, "little") + b"next"


def test_lost_session_raises_unknown_outcome_from_transact() -> None:
    dev, model, _clock = make_device()
    dev.open(timeout=1.0)
    assert dev.transact(b"one")
    model.reboot(dev._clock(), downtime=0.0)
    try:
        dev.transact(b"two", timeout=5.0)
    except UnknownOutcome as exc:
        assert exc.result.status is Status.UNKNOWN_OUTCOME
        assert "unbound" in exc.result.reason
    else:
        raise AssertionError("an unbound FPGA must end the session")
    assert not dev.active


def test_open_fails_when_nobody_answers() -> None:
    clock = Clock()

    class Dead:
        def rx(self, data, now):
            pass

        def tx(self, now):
            return b""

    cfg = HostConfig(bridge_budget=2048, open_timeout=0.01, open_retries=3)
    dev = LinkDevice(LoopbackBackend(Dead(), clock), cfg, clock=clock, sleep=clock.sleep)
    try:
        dev.open(timeout=1.0)
    except OpenFailed as exc:
        assert "OPEN" in str(exc)
    else:
        raise AssertionError


def test_import_does_not_load_serial_libraries_and_missing_library_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "serial", None)  # makes "import serial" raise ImportError
    with pytest.raises(BackendUnavailable, match="pyserial"):
        SerialBackend("/dev/null")


def test_serial_backend_has_no_timeout_option() -> None:
    with pytest.raises(TypeError, match="timeout"):
        SerialBackend("/dev/null", timeout=1.0)


def test_serial_backend_maps_to_the_pyserial_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    pipe_read, pipe_write = os.pipe()
    os.set_blocking(pipe_read, False)

    class FakeSerial:
        def __init__(self, port, **kw):
            calls.append(("open", port, kw))

        def fileno(self):
            return pipe_read

        def write(self, data):
            calls.append(("write", bytes(data)))

        def reset_input_buffer(self):
            calls.append(("purge",))

        def close(self):
            calls.append(("close",))

    fake = types.ModuleType("serial")
    fake.Serial = FakeSerial
    monkeypatch.setitem(sys.modules, "serial", fake)
    try:
        be = SerialBackend("/dev/ttyUSB9", baudrate=3_000_000)
        be.write(b"xy")
        os.write(pipe_write, b"abcd")
        assert be.read(3, 0.5) == b"abc" and be.read(16, 0.0) == b"d"
        assert be.read(16, 0.0) == b""  # nothing waits: select returns at once
        be.purge()
        be.close()
    finally:
        os.close(pipe_read)
        os.close(pipe_write)
    assert calls[0][0] == "open" and calls[0][2]["baudrate"] == 3_000_000
    assert calls[1:] == [("write", b"xy"), ("purge",), ("close",)]


def test_ftdi_backend_strips_status_bytes_and_sets_chip_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _time

    log = []

    class FakeFtdi:
        VENDOR = None
        _max_packet_size = 8  # status bytes at the start of every 8-byte USB packet

        def __init__(self):
            self.reads = [
                bytes([1, 0, 10, 11, 12, 13, 14, 15]) + bytes([1, 0x02, 16, 17]),  # 2 packets
                bytes([1, 0]),  # status only
            ]

        def open_from_url(self, url):
            log.append(("open", url))

        def set_baudrate(self, b):
            log.append(("baud", b))
            return b

        def set_line_property(self, *a):
            pass

        def set_latency_timer(self, ms):
            log.append(("latency", ms))

        def set_event_char(self, ch, enable):
            log.append(("event", ch, enable))

        def read_data_set_chunksize(self, n):
            log.append(("chunk", n))

        def purge_buffers(self):
            pass

        def purge_rx_buffer(self):
            log.append(("purge",))

        def write_data(self, data):
            log.append(("write", bytes(data)))

        def close(self):
            log.append(("close",))

        def _read(self):
            if self.reads:
                return self.reads.pop(0)
            _time.sleep(0.001)
            return bytes([1, 0])

    pkg = types.ModuleType("pyftdi")
    mod = types.ModuleType("pyftdi.ftdi")
    mod.Ftdi = FakeFtdi
    monkeypatch.setitem(sys.modules, "pyftdi", pkg)
    monkeypatch.setitem(sys.modules, "pyftdi.ftdi", mod)
    be = leanlink.FtdiBackend("ftdi://x/2", 12_000_000, latency_ms=2, event_char=0, chunksize=64)
    got = b""
    end = _time.monotonic() + 2
    while len(got) < 8 and _time.monotonic() < end:
        got += be.read(100, 0.05)
    assert got == bytes([10, 11, 12, 13, 14, 15, 16, 17])
    assert be.overruns == 1
    assert be.read(10, 0.0) == b""
    be.write(b"ab")
    be.close()
    assert ("latency", 2) in log and ("event", 0, True) in log and ("chunk", 64) in log
    assert ("write", b"ab") in log and log[-1] == ("close",)


class FakeChip:
    """A pyftdi `Ftdi` with a receive buffer. `_read` waits up to 20 ms for bytes, like a read of
    the chip with a latency timer.

    A test sets `gate` to hold the next read. With `capture_first` the read takes the bytes of the
    buffer first and then waits at the gate, so it returns bytes that are older than the gate.
    Without it the read waits at the gate first and takes the bytes when the gate opens."""

    VENDOR = None
    _max_packet_size = 64

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.started_held = threading.Event()  # a read has started that the gate will hold
        self.blocked = threading.Event()  # a read waits at the gate
        self.gate: threading.Event | None = None
        self.capture_first = False
        self.calls: list[str] = []
        self.closed = False
        self.read_after_close = False
        self.backend = None  # set by the test: the backend that reads this chip
        self.read_after_stop = False  # a read started after the backend was told to stop
        self.on_read = None  # a function that the next read calls once

    def feed(self, data: bytes) -> None:
        with self.lock:
            self.buffer += data

    def purge_rx_buffer(self) -> None:
        self.calls.append("purge_rx_buffer")
        with self.lock:
            self.buffer.clear()

    def _take(self, seconds: float = 0.02) -> bytes:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.buffer:
            time.sleep(0.0005)
        with self.lock:
            data = bytes(self.buffer)
            self.buffer.clear()
        return data

    def _read(self) -> bytes:
        if self.closed:
            self.read_after_close = True
        if self.backend is not None and self.backend._stop:
            self.read_after_stop = True
        hook, self.on_read = self.on_read, None
        if hook is not None:
            hook()
        gate = self.gate
        held = gate is not None and not gate.is_set()
        if held:
            self.started_held.set()
        data = self._take(5.0 if held else 0.02) if self.capture_first else b""
        if held:
            self.blocked.set()
            gate.wait(5)
        if not self.capture_first:
            data = self._take()
        return bytes([1, 0]) + data

    def close(self):
        self.calls.append("close")
        self.closed = True

    def open_from_url(self, url):
        pass

    def set_baudrate(self, b):
        return b

    def set_line_property(self, *a):
        pass

    def set_latency_timer(self, ms):
        pass

    def set_event_char(self, ch, enable):
        pass

    def read_data_set_chunksize(self, n):
        pass

    def purge_buffers(self):
        pass

    def write_data(self, data):
        pass


def ftdi_backend_on_a_fake_chip(monkeypatch: pytest.MonkeyPatch):
    chip = FakeChip()
    module = types.ModuleType("pyftdi.ftdi")
    module.Ftdi = lambda: chip
    monkeypatch.setitem(sys.modules, "pyftdi", types.ModuleType("pyftdi"))
    monkeypatch.setitem(sys.modules, "pyftdi.ftdi", module)
    backend = leanlink.FtdiBackend("ftdi://x/2", 12_000_000)
    chip.backend = backend
    return chip, backend


class WatchedLock:
    """A lock that tells when a thread is about to wait for it: `waiting` is set right before the
    blocking acquire, so a test knows that the competing call waits and does not go on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.waiting.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


def wait_until(predicate, seconds: float = 5.0) -> None:
    """Wait for a state, not for a time: poll until the predicate holds."""
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "the state did not come"
        time.sleep(0.0002)


def purge_then_feed(be, chip, data: bytes) -> threading.Thread:
    """A thread that purges and then lets `data` arrive at the chip: the bytes come after the
    purge has returned, in the order of this thread."""

    def run() -> None:
        be.purge()
        chip.feed(data)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def purger_is_in_the_handshake_or_done(be, thread) -> bool:
    return getattr(be, "_pause", False) or not thread.is_alive()


def test_ftdi_purge_drops_the_bytes_that_a_read_brought_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read has taken the stale bytes `AB` and waits before it returns them. The purge starts, and
    the read returns `AB` after that. They are older than the purge and must go. The bytes `C`
    arrive after the purge returned and must stay."""
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    gate = threading.Event()
    try:
        chip.capture_first = True
        chip.gate = gate
        assert chip.started_held.wait(5)  # the next read is the one that the gate holds
        chip.feed(b"AB")  # it takes these bytes, and then it waits at the gate
        assert chip.blocked.wait(5)
        purger = purge_then_feed(be, chip, b"C")
        wait_until(lambda: purger_is_in_the_handshake_or_done(be, purger))
        gate.set()
        purger.join(5)
        assert not purger.is_alive()
        assert be.read(10, 1.0) == b"C"
    finally:
        gate.set()
        be.close()


def test_ftdi_purge_keeps_the_bytes_that_arrive_right_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read is under way when the purge starts and holds there. The reply arrives as soon as
    purge has returned. The read that was under way must not drop it."""
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    gate = threading.Event()
    try:
        chip.gate = gate
        assert chip.blocked.wait(5)  # a read is under way and holds at the gate
        purger = purge_then_feed(be, chip, b"reply")
        wait_until(lambda: purger_is_in_the_handshake_or_done(be, purger))
        gate.set()
        purger.join(5)
        assert not purger.is_alive()
        assert be.read(10, 1.0) == b"reply"
    finally:
        gate.set()
        be.close()


def test_ftdi_purge_fails_when_the_reader_does_not_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    gate = threading.Event()
    try:
        be.purge_timeout = 0.05
        chip.gate = gate
        assert chip.blocked.wait(5)
        with pytest.raises(RuntimeError, match="did not stop"):
            be.purge()
    finally:
        gate.set()
        be.close()


def test_two_ftdi_purges_at_once_both_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second purge waits for the first. It does not time out because the reader went on."""
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    errors: list[BaseException] = []

    def purge() -> None:
        try:
            be.purge()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        be.purge_timeout = 0.5
        for _ in range(30):
            lock = be._op_lock = WatchedLock()
            gate = threading.Event()
            chip.blocked.clear()
            chip.gate = gate
            assert chip.blocked.wait(5)  # a read holds at the gate
            first = threading.Thread(target=purge)
            first.start()
            wait_until(lambda: be._pause)  # the first purge is in its handshake
            second = threading.Thread(target=purge)
            second.start()
            assert lock.waiting.wait(5)  # the second purge waits for the lock of the first
            gate.set()
            first.join(5)
            second.join(5)
            assert not first.is_alive() and not second.is_alive()
            assert errors == []
    finally:
        be.close()


def test_ftdi_close_waits_for_a_purge_and_a_purge_after_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    lock = be._op_lock = WatchedLock()
    gate = threading.Event()
    errors: list[BaseException] = []

    def purge() -> None:
        try:
            be.purge()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        chip.gate = gate
        assert chip.blocked.wait(5)
        purger = threading.Thread(target=purge)
        purger.start()
        wait_until(lambda: be._pause)  # the purge is in its handshake
        closer = threading.Thread(target=be.close)
        closer.start()
        assert lock.waiting.wait(5)  # close waits for the lock of the purge
        assert not be._stop  # it has not stopped the reader
        gate.set()
        purger.join(5)
        closer.join(5)
        assert not purger.is_alive() and not closer.is_alive()
        assert errors == []
        assert chip.calls == ["purge_rx_buffer", "close"]  # the purge ran on an open chip
        assert not chip.read_after_close
        with pytest.raises(RuntimeError, match="closed"):
            be.purge()
        assert chip.calls == ["purge_rx_buffer", "close"]
    finally:
        gate.set()


def test_ftdi_close_stops_the_reader_before_it_starts_another_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read is under way when close starts. After close has set the stop flag, the reader ends
    when that read returns: it starts no read, and the chip is closed once."""
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    gate = threading.Event()
    try:
        chip.gate = gate
        assert chip.blocked.wait(5)
        closer = threading.Thread(target=be.close)
        closer.start()
        wait_until(lambda: be._stop)  # close has told the reader to stop and waits for it
        gate.set()
        closer.join(5)
        assert not closer.is_alive() and not be._thread.is_alive()
        assert not chip.read_after_stop and not chip.read_after_close
        assert chip.calls == ["close"]
    finally:
        gate.set()


def test_ftdi_close_on_the_reader_thread_still_closes_the_chip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader cannot join itself. Close skips the join and closes the chip, and the reader
    ends by itself."""
    chip, be = ftdi_backend_on_a_fake_chip(monkeypatch)
    errors: list[BaseException] = []

    def close_on_the_reader() -> None:
        try:
            be.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    chip.on_read = close_on_the_reader
    be._thread.join(5)  # the next read runs the hook; the reader then ends
    assert not be._thread.is_alive()
    assert errors == []
    assert chip.calls == ["close"]
    assert be._error is None


@pytest.mark.parametrize("purge_on_open", [True, False])
def test_ftdi_backend_purges_the_chip_buffer_on_open_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch, purge_on_open: bool
) -> None:
    log = []

    class FakeFtdi:
        VENDOR = None
        _max_packet_size = 64

        def open_from_url(self, url):
            log.append("open")

        def set_baudrate(self, b):
            return b

        def set_line_property(self, *a):
            pass

        def set_latency_timer(self, ms):
            pass

        def set_event_char(self, ch, enable):
            pass

        def read_data_set_chunksize(self, n):
            pass

        def purge_buffers(self):
            log.append("purge")

        def purge_rx_buffer(self):
            pass

        def write_data(self, data):
            pass

        def close(self):
            pass

        def _read(self):
            time.sleep(0.001)
            return bytes([1, 0])

    pkg = types.ModuleType("pyftdi")
    mod = types.ModuleType("pyftdi.ftdi")
    mod.Ftdi = FakeFtdi
    monkeypatch.setitem(sys.modules, "pyftdi", pkg)
    monkeypatch.setitem(sys.modules, "pyftdi.ftdi", mod)
    be = leanlink.FtdiBackend("ftdi://x/2", 12_000_000, purge_on_open=purge_on_open)
    be.close()
    assert ("purge" in log) is purge_on_open


# ---------------------------------------------------------------------------------------------
# Frame-level FPGA model


def make_model(w: int = 4, p: int = 256, **kw) -> tuple[fm.FpgaModel, ModelHost]:
    model = fm.FpgaModel(w, p, **kw)
    return model, ModelHost(model)


def response_of(count: int, payload: bytes) -> bytes:
    return count.to_bytes(4, "little") + payload


def test_open_reply_is_status_valid_under_new_session() -> None:
    m, h = make_model(w=8)
    out = h.start_session(rsp_limit=100)
    assert len(out) == 1
    f = out[0]
    assert f["kind"] == "status" and f["len"] == m.max_len == 255
    assert f["log2w"] == 3 and f["seq"] == 0 and f["exp"] == 0 and f["cursor"] == 12
    assert not f["trunc"]
    assert m.bound and m.sid == SID and m.rsp_limit == 100


def test_unbound_fpga_answers_an_invalid_frame_with_status_under_session_zero() -> None:
    m, h = make_model()
    h.send(h.frame(sid=0x99999999))  # invalid under S = 0
    out = h.settle()
    assert len(out) == 1 and out[0]["kind"] == "status"
    assert lc.check_fpga_frame(out[0]["wire"][:-1], 0, 4, None).frame is not None
    assert not m.bound


def test_bound_fpga_ignores_an_invalid_frame_silently() -> None:
    m, h = make_model()
    h.start_session()
    h.send(h.frame(sid=0x99999999))
    assert h.settle() == []


def test_request_executes_once_and_response_has_count_and_payload() -> None:
    m, h = make_model()
    h.start_session()
    h.req(0, b"abc")
    out = h.settle()
    assert [f["kind"] for f in out] == ["data"]
    assert out[0]["payload"] == response_of(0, b"abc") and out[0]["seq"] == 0
    assert out[0]["exp"] == 1 and not out[0]["trunc"]
    h.req(1, b"", ack=1)
    out = h.settle()
    assert out[0]["payload"] == response_of(1, b"") and out[0]["len"] == 16
    assert m.executed == [b"abc", b""]


def test_duplicate_is_not_executed_and_gets_a_status_reply() -> None:
    m, h = make_model()
    h.start_session()
    h.req(0, b"x")
    h.settle()
    h.req(0, b"x")  # duplicate: seq 0, exp is 1
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"] and out[0]["exp"] == 1
    assert len(m.executed) == 1


def test_gap_is_not_committed() -> None:
    m, h = make_model()
    h.start_session()
    h.req(1, b"y")  # exp is 0
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"] and out[0]["exp"] == 0
    assert m.executed == []


def test_ack_for_an_unsent_response_ignores_the_whole_frame() -> None:
    m, h = make_model()
    h.start_session()
    h.req(0, b"z", ack=2)  # no response was sent yet: hi = 0
    assert h.settle() == []
    assert m.executed == [] and m.exp == 0
    assert m.stats.get("rx_ignored_bad_ack") == 1


def test_poll_gets_a_status_frame() -> None:
    m, h = make_model()
    h.start_session()
    h.hdr(poll=True)
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"]


def test_open_ignores_poll_rewind_and_resync_bits() -> None:
    m, h = make_model()
    h.open(ctl_extra=0x08 | 0x06 | 0x01)  # POLL, REWIND epoch 3, RESYNC
    out = h.settle()
    assert len(out) == 1 and out[0]["kind"] == "status"  # only the OPEN reply
    assert m.rw_host == 0 and not m.permit and m.bound


def test_status_frame_carries_next_to_send_and_maxlen() -> None:
    m, h = make_model(w=2, p=64)
    h.start_session(rsp_limit=71)
    assert m.max_len == 71
    h.hdr(poll=True)
    f = h.settle()[0]
    assert f["len"] == 71 and f["cursor"] == 24 and f["log2w"] == 1


def test_credit_blocks_data_but_a_status_may_use_the_reserve() -> None:
    m, h = make_model()
    h.start_session()
    # Grant 12 + 20: a data frame of 16 bytes (4 + 0 payload + 12) needs cursor 12 + 16 = 28.
    h.req(0, b"", grant=20)  # grant edge 20 < 28: no data credit
    assert h.settle() == []
    assert m.tx != m.wr and m.cursor == 12
    # POLL makes it owe a status. Status needs grant - next >= -R: 20 - 24 = -4 >= -64.
    h.hdr(poll=True, grant=20)
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"] and out[0]["cursor"] == 24
    # Raise the grant: the data frame goes out.
    h.hdr(grant=100)
    out = h.settle()
    assert [f["kind"] for f in out] == ["data"] and out[0]["cursor"] == 40


def test_status_without_credit_and_without_permit_is_blocked() -> None:
    m, h = make_model()
    h.start_session()
    h.hdr(poll=True, grant=-200 & 0xFFFF)  # grant - 24 = -224 < -64
    assert h.settle() == []
    assert m.owe


def test_resync_permit_lets_one_status_start_without_credit() -> None:
    m, h = make_model()
    h.start_session()
    h.hdr(resync=True, grant=-200 & 0xFFFF)
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"] and out[0]["cursor"] == 24
    assert not m.permit
    h.hdr(poll=True, grant=-200 & 0xFFFF)  # the permit was used up
    assert h.settle() == []


def test_resync_in_an_ignored_frame_sets_no_permit() -> None:
    m, h = make_model()
    h.start_session()
    h.hdr(resync=True, ack=3)  # bad ack: ignored whole
    assert not m.permit
    assert h.settle() == []


def test_open_waits_for_the_core_and_discards_unstarted_requests() -> None:
    m, h = make_model(w=4, core_delay=2e-3)
    h.start_session()
    h.req(0, b"first")
    h.req(1, b"second", ack=0)
    h.settle(1e-4)  # request 0 is in the core, request 1 is committed but not started
    assert m.state()["core"] in ("read", "delay") and m.exp == 2
    h.open(sid=0x55667788)  # new OPEN
    assert m.open_pending
    out = h.settle(10e-3)
    assert [f["kind"] for f in out] == ["status"]  # the OPEN reply; no response of the old session
    assert m.sid == 0x55667788 and m.exp == 0 and m.cons == 0 and m.wr == 0
    assert m.executed == [b"first"]  # started request served, unstarted request discarded
    assert m.stats.get("responses_dropped_open") == 1


def test_frames_of_the_old_session_are_ignored_while_open_is_pending() -> None:
    m, h = make_model(core_delay=2e-3)
    h.start_session()
    h.req(0, b"first")
    h.settle(1e-4)
    h.open(sid=0x55667788)
    h.req(1, b"late")  # valid under the old session, arrives while openPending
    assert m.stats.get("rx_ignored_open_pending") == 1
    h.settle(10e-3)
    assert m.executed == [b"first"]


def test_session_reset_invalidates_a_frame_in_progress() -> None:
    m, h = make_model(w=4, core_delay=1e-3)
    h.start_session()
    h.req(0, b"busy")
    h.settle(1e-4)
    h.open(sid=SID)  # same session ID again: the reset waits for the core
    wire = lc.encode_host_wire(h.frame(data=True, seq=0, payload=b"during the reset", sid=SID))
    # The frame starts before the reset and ends after it.
    t0 = h.t
    m.rx(wire[:1], [t0 + 1e-6])
    m.advance(t0 + 3e-3)  # the core finishes: the reset happens
    assert m.stats.get("session_resets") == 2
    m.rx(wire[1:], [t0 + 3e-3 + (i + 1) * m.byte_time for i in range(len(wire) - 1)])
    assert m.stats.get("rx_invalidated_by_reset") == 1
    assert m.exp == 0 and m.executed == [b"busy"]


def test_truncated_response_carries_trunc_and_drops_the_excess() -> None:
    m, h = make_model()
    h.start_session(rsp_limit=24)  # responseMax = 12 payload bytes
    h.req(0, b"0123456789")  # response = 4 + 10 = 14 bytes
    f = h.settle()[0]
    assert f["trunc"] and f["len"] == 24 and f["payload"] == response_of(0, b"0123456789")[:12]
    assert m.stats.get("data_frames") == 1


def test_rewind_resends_unacknowledged_responses() -> None:
    m, h = make_model(w=4)
    h.start_session()
    for k in range(3):
        h.req(k, bytes([k]), ack=0)
    first = h.settle()
    assert [f["seq"] for f in first if f["kind"] == "data"] == [0, 1, 2]
    h.hdr(ack=1, rewind=1)  # the host received 0, lost the rest: epoch 1
    again = [f for f in h.settle() if f["kind"] == "data"]
    assert [f["seq"] for f in again] == [1, 2]
    assert m.executed == [b"\x00", b"\x01", b"\x02"]


def test_response_waits_for_an_acknowledgement_when_the_pages_are_full() -> None:
    m, h = make_model(w=1)
    h.start_session()
    h.req(0, b"a", ack=0)
    assert h.settle()[0]["kind"] == "data"
    h.req(1, b"b", ack=0)  # a host that breaks the window rule: request 1 before ack 1
    assert h.settle() == [] and m.executed == [b"a"]  # responseReady is false: no execution
    h.hdr(ack=1)
    out = h.settle()
    assert [f["seq"] for f in out if f["kind"] == "data"] == [1] and m.executed == [b"a", b"b"]


def test_a_host_that_breaks_the_window_rule_gets_a_drop_for_a_busy_page() -> None:
    m, h = make_model(w=2, core_delay=5e-3)
    h.start_session()
    for k in range(4):  # a compliant host sends at most 2 requests before the first response
        h.req(k, bytes([k]), ack=0)
    out = h.settle(1e-4)
    # Request 0 is in the core (cons = 1). Requests 1 and 2 wait. Request 3 has no free page.
    assert m.exp == 3 and m.stats.get("req_dropped_page_busy") == 1
    assert [f["kind"] for f in out] == ["status"] and out[0]["exp"] == 3


def test_reboot_makes_the_fpga_unbound() -> None:
    m, h = make_model()
    h.start_session()
    h.req(0, b"a")
    h.settle()
    m.reboot(h.t, downtime=0.0)
    h.t += 1e-3
    assert not m.bound and m.cursor == 0
    h.hdr(ack=1)
    out = h.settle()
    assert [f["kind"] for f in out] == ["status"]
    assert lc.check_fpga_frame(out[0]["wire"][:-1], 0, 4, None).frame is not None
    assert m.executed == [b"a"]  # the history survives, the next count restarts at 0
    h.start_session(sid=0x01020304)
    h.sid = 0x01020304
    h.req(0, b"b")
    assert h.settle()[0]["payload"] == response_of(0, b"b")


def test_data_frame_is_never_longer_than_the_page_allows() -> None:
    m, h = make_model(w=2, p=64)
    h.start_session(rsp_limit=71)
    h.req(0, bytes(59))
    f = h.settle()[0]
    assert f["len"] == 71 and f["trunc"] and len(f["payload"]) == 59


def test_cursor_wraps_modulo_65536_and_credit_follows() -> None:
    m, h = make_model()
    h.start_session()
    m.cursor = 0xFFF0
    h.req(0, b"", grant=0x0030)  # grant edge 0x30 is 0x40 ahead of the cursor
    f = h.settle()[0]
    assert f["kind"] == "data" and f["cursor"] == (0xFFF0 + 16) & 0xFFFF


def test_open_does_not_wait_for_a_core_that_has_not_taken_the_first_token() -> None:
    m, h = make_model()
    h.start_session()
    m.core_stall = True  # the core is idle and refuses new work
    h.req(0, b"never taken")
    h.settle(1e-4)
    assert m.exp == 1 and m.state()["core"] == "idle"
    h.open(sid=0x55667788)
    out = h.settle(5e-3)
    assert [f["kind"] for f in out] == ["status"] and m.sid == 0x55667788
    m.core_stall = False
    h.settle(5e-3)
    assert m.executed == []  # the unstarted request was abandoned, not executed later


def test_a_frame_that_a_reboot_cuts_is_cut_on_the_wire() -> None:
    model = fm.FpgaModel(4, 256)
    peer = fm.ModelPeer(model)
    byte_time = model.byte_time
    open_wire = lc.encode_host_wire(lc.build_open_frame(SID, 255, grant=0))
    peer.rx(open_wire, 0.0)
    now = len(open_wire) * byte_time + 1e-3
    assert len(peer.tx(now)) == 1 + 12  # the delimiter after power-up, then the OPEN reply
    request = lc.build_host_frame(lc.host_ctl(data=True), 0, 0, 2000, bytes(200), SID)
    wire = lc.encode_host_wire(request)
    peer.rx(wire, now)
    now += len(wire) * byte_time
    sent = peer.tx(now + 100 * byte_time)  # the response frame (216 bytes) is under way
    assert 0 < len(sent) < 216
    model.reboot(now + 100 * byte_time)
    later = peer.tx(now + 400 * byte_time)
    assert len(sent) + len(later) < 216


def test_the_model_sends_a_delimiter_after_power_up_and_after_a_reboot() -> None:
    model = fm.FpgaModel(4, 256)
    peer = fm.ModelPeer(model)
    byte_time = model.byte_time
    assert peer.tx(10 * byte_time) == b"\x00"
    assert model.state()["grant"] == 0xFFCC and model.state()["cursor"] == 0
    model.reboot(1e-3, downtime=2e-3)
    assert peer.tx(1e-3 + 10 * byte_time) == b""  # the FPGA is still in reset
    assert peer.tx(3e-3 + 10 * byte_time) == b"\x00"
    assert model.state()["grant"] == 0xFFCC and model.state()["cursor"] == 0


def test_the_power_up_grant_pays_for_exactly_one_unbound_status_frame() -> None:
    model, h = make_model()
    first = h.settle()  # the delimiter is no frame
    assert first == []
    h.req(0, b"x")
    frames = h.settle()
    assert [f["kind"] for f in frames] == ["status"] and frames[0]["cursor"] == 12
    h.req(0, b"y")
    h.hdr(poll=True)
    assert h.settle() == []  # the reserve is used up


@pytest.mark.parametrize("w, p", [(3, 256), (256, 256), (0, 256), (8, 10), (8, 100), (8, 512)])
def test_the_model_refuses_sizes_that_the_core_cannot_build(w: int, p: int) -> None:
    with pytest.raises(ValueError):
        fm.FpgaModel(w, p)


# ---------------------------------------------------------------------------------------------
# Host protocol


def u16(x: int) -> int:
    return x & 0xFFFF


# ---------------------------------------------------------------------------------------------
# Configuration and OPEN


def test_session_id_has_four_nonzero_bytes() -> None:
    rng = random.Random(1)
    for _ in range(500):
        p = HostProtocol(config(), rng=rng)
        assert 0 not in p.sid.to_bytes(4, "little")
    for bad in (0, 0x00112233, 0x11002233, 0x11220033, 0x11223300):
        try:
            HostProtocol(config(), sid=bad)
        except ValueError:
            continue
        raise AssertionError(f"sid {bad:#x} was accepted")
    HostProtocol(config(), sid=0x01010101)


def test_config_validates_the_budget_rules() -> None:
    def raises(**kw) -> bool:
        try:
            HostConfig(**kw)
        except ValueError:
            return True
        return False

    assert raises(bridge_budget=32768 - 64)  # B + R < 32768 fails at 32768
    HostConfig(bridge_budget=32768 - 65)
    assert raises(bridge_budget=300, rsp_limit=255)  # rspLimit <= B - R - 12
    HostConfig(bridge_budget=255 + 64 + 12, rsp_limit=255)
    assert raises(bridge_budget=255 + 64 + 11, rsp_limit=255)
    assert raises(bridge_budget=256, rsp_limit=181)  # the budget of a small bridge
    HostConfig(bridge_budget=256, rsp_limit=180)
    assert raises(bridge_budget=1024, rsp_limit=11)
    assert raises(bridge_budget=1024, rsp_limit=256)


def test_open_frame_is_a_valid_open_with_session_zero() -> None:
    p = HostProtocol(config(rsp_limit=100), sid=SID)
    p.start(0.0)
    wire = p.next_frame(0.0)
    f = parse_host(wire, sid=0)
    assert f["kind"] == "open" and f["session_id"] == SID and f["rsp_limit"] == 100
    assert not f["poll"] and f["rewind"] == 0 and not f["resync"] and f["grant"] == 0
    assert p.next_frame(0.0) is None  # no other frame before the session starts


def test_open_is_retried_and_then_the_session_is_lost() -> None:
    p = HostProtocol(config(open_timeout=0.01, open_retries=3), sid=SID)
    p.start(0.0)
    assert p.next_frame(0.0) is not None and p.next_frame(0.005) is None
    assert p.next_frame(0.0101) is not None  # second OPEN, same session ID
    assert p.next_frame(0.0202) is not None
    assert p.next_frame(0.0303) is None and p.state is State.LOST
    assert "OPEN" in p.lost_reason


def test_session_starts_at_the_first_status_valid_under_the_session() -> None:
    p = HostProtocol(config(max_payload=40), sid=SID)
    p.start(0.0)
    p.next_frame(0.0)
    p.feed(status_wire(sid=0x01010101), 0.001)  # another session
    assert p.state is State.OPENING
    p.feed(status_wire(SID, w=8, max_len=71, cursor=12), 0.002)
    assert p.state is State.ACTIVE and p.W == 8 and p.max_len == 71 and p.consumed == 12


def test_opening_ignores_a_reply_that_breaks_the_fpga_frame_rules() -> None:
    p = HostProtocol(config(), sid=SID)
    p.start(0.0)
    p.next_frame(0.0)
    p.feed(status_wire(SID, trunc=True), 0.001)  # status with TRUNC = 1
    assert p.state is State.OPENING
    p.feed(status_wire(SID, exp=1), 0.001)  # a fresh session has exp = 0
    assert p.state is State.OPENING


def test_unbound_status_in_the_open_phase_lowers_rsp_limit_and_resends() -> None:
    p = HostProtocol(config(rsp_limit=255, open_timeout=1.0), sid=SID)
    p.start(0.0)
    assert parse_host(p.next_frame(0.0), sid=0)["rsp_limit"] == 255
    assert p.next_frame(0.001) is None
    p.feed(status_wire(sid=0, w=2, max_len=71), 0.001)  # UNBOUND status frame: maxLen 71
    assert p.state is State.OPENING
    again = p.next_frame(0.001)
    assert again is not None and parse_host(again, sid=0, max_len=71)["rsp_limit"] == 71


def test_a_build_with_a_small_maxlen_ends_the_session_before_any_request() -> None:
    p = HostProtocol(config(max_payload=243), sid=SID)
    t = p.submit(b"x")
    p.start(0.0)
    p.next_frame(0.0)
    p.feed(status_wire(SID, max_len=71), 0.001)
    assert p.state is State.LOST
    r = p.take_result(t)
    assert r.status is Status.UNKNOWN_OUTCOME and not r.was_sent


# ---------------------------------------------------------------------------------------------
# Requests, window, ack and grant


def test_window_admits_w_requests_and_one_more_per_response() -> None:
    p = started(w=2)
    for k in range(5):
        p.submit(bytes([k]))
    frames = drain(p, 0.001)
    assert [parse_host(f)["seq"] for f in frames] == [0, 1]
    p.feed(response_wire(w=2, seq=0, exp=2, cursor=40, payload=b"r0"), 0.002)
    assert p.take_result(0).data == b"r0"
    frames = drain(p, 0.002)
    assert [parse_host(f, w=2)["seq"] for f in frames] == [2]
    p.feed(response_wire(w=2, seq=1, exp=3, cursor=70), 0.003)
    assert [parse_host(f, w=2)["seq"] for f in drain(p, 0.003)] == [3]


def test_sequence_numbers_wrap_modulo_2w() -> None:
    p = started(w=1)
    seqs = []
    for k in range(6):
        p.submit(b"x")
        frames = drain(p, 0.001 * (k + 1))
        assert len(frames) == 1
        seqs.append(parse_host(frames[0], w=1)["seq"])
        p.feed(
            response_wire(w=1, seq=k % 2, exp=(k + 1) % 2, cursor=12 + 20 * (k + 1)),
            0.001 * (k + 1),
        )
    assert seqs == [0, 1, 0, 1, 0, 1]


def frame_kinds(frames: list[bytes], w: int = 4) -> list[str]:
    return ["request" if parse_host(f, w=w)["data"] else "header" for f in frames]


def credit_first_session(budget: int, payload_a: int = 1) -> HostProtocol:
    """A session with two requests on the wire and the first response read: the FPGA has used
    the credit of the first grant, and a request is due."""
    p = started(w=4, bridge_budget=budget, rsp_limit=255)
    p.submit(b"a" * payload_a)
    p.submit(b"b")
    assert frame_kinds(drain(p, 0.001)) == ["request", "request"]
    return p


def test_credit_first_sends_a_header_frame_before_a_request_when_the_fpga_may_be_blocked() -> None:
    p = credit_first_session(400)  # grant = consumed + 324: the window holds one response frame
    p.feed(response_wire(seq=0, exp=2, cursor=12 + 255, payload=bytes(243)), 0.002)
    p.submit(b"c")
    frames = drain(p, 0.003)
    assert frame_kinds(frames) == ["header", "request"]
    header, request = (parse_host(f) for f in frames)
    assert header["grant"] == request["grant"] == u16(12 + 255 + 324)
    assert header["ack"] == request["ack"] == 1 and request["seq"] == 2
    assert p.stats["credit_first_frames"] == 1


@pytest.mark.parametrize(
    "payload, header",
    [(57, False), (58, True)],
    ids=["credit-for-one-response-left", "one-byte-short"],
)
def test_credit_first_fires_when_less_than_one_response_of_credit_is_left(
    payload: int, header: bool
) -> None:
    """The first grant was 12 + 324 = 336. After a response of 12 + payload bytes the cursor is
    c = 12 + 12 + payload, and the FPGA may have 336 - c bytes of credit: 255 or more is enough
    for one full response (rsp_limit 255)."""
    p = credit_first_session(400)
    p.feed(response_wire(seq=0, exp=2, cursor=24 + payload, payload=bytes(payload)), 0.002)
    p.submit(b"c")
    expected = ["header", "request"] if header else ["request"]
    assert frame_kinds(drain(p, 0.003)) == expected


def test_credit_first_never_fires_when_credit_is_ample() -> None:
    p = started(w=4, bridge_budget=4096, rsp_limit=255)  # the window holds seven response frames
    p.submit(b"a")
    p.submit(b"b")
    assert frame_kinds(drain(p, 0.001)) == ["request", "request"]
    cursor = 12
    kinds: list[str] = []
    for i in range(24):  # one response in, one request out, many times
        cursor += 12 + 200
        now = 0.002 + 0.0001 * i
        p.feed(response_wire(seq=i % 8, exp=(i + 2) % 8, cursor=cursor, payload=bytes(200)), now)
        p.submit(b"x")
        kinds += frame_kinds(drain(p, now))
    assert kinds and set(kinds) == {"request"}
    assert "credit_first_frames" not in p.stats


def test_credit_first_never_fires_when_no_response_is_outstanding() -> None:
    p = started(w=4, bridge_budget=400, rsp_limit=255)
    p.submit(b"a")
    assert frame_kinds(drain(p, 0.001)) == ["request"]
    p.feed(response_wire(seq=0, exp=1, cursor=12 + 255, payload=bytes(243)), 0.002)
    p.submit(b"b")  # the credit is short, but nothing waits for the FPGA
    assert frame_kinds(drain(p, 0.003)) == ["request"]
    assert "credit_first_frames" not in p.stats


def test_credit_first_cannot_starve_the_requests() -> None:
    """At most one credit-first frame goes before a request, however many FPGA frames arrive in
    between: each request frame follows its header, and all queued requests go out."""
    p = credit_first_session(400)
    p.feed(response_wire(seq=0, exp=2, cursor=12 + 255, payload=bytes(243)), 0.002)
    for name in (b"c", b"d"):
        p.submit(name)
    sent: list[str] = []
    cursor = 12 + 255
    for step in range(12):
        frame = p.next_frame(0.003 + 0.0001 * step)
        if frame is not None:
            sent += frame_kinds([frame])
        cursor += 12  # a status frame arrives after every frame: the grant moves again
        p.feed(status_wire(w=4, seq=1, exp=2 + sent.count("request") - 0, cursor=cursor), 0.0035)
        if sent.count("request") == 2:
            break
    assert sent.count("request") == 2, sent
    assert "header" not in [a for a, b in zip(sent, sent[1:]) if a == b == "header"], sent


def test_every_frame_carries_ack_and_grant_of_consumed_plus_budget_minus_76() -> None:
    p = started(w=4, bridge_budget=2048)
    for k in range(3):
        p.submit(b"a" * k)
    f0 = parse_host(drain(p, 0.001)[0])
    assert f0["ack"] == 0 and f0["grant"] == u16(12 + 2048 - 76)
    p.feed(response_wire(seq=0, exp=3, cursor=300, payload=b"x"), 0.002)
    p.submit(b"more")
    f = parse_host(drain(p, 0.002)[0])
    assert f["ack"] == 1 and f["grant"] == u16(300 + 2048 - 76)


def test_results_come_in_order_and_gaps_and_duplicates_are_ignored() -> None:
    p = started(w=4)
    for k in range(3):
        p.submit(bytes([k]))
    drain(p, 0.001)
    p.feed(response_wire(seq=1, exp=3, cursor=40, payload=b"one"), 0.002)  # gap: 0 is missing
    assert p.take_result(1) is None and p.take_result(0) is None
    p.feed(response_wire(seq=0, exp=3, cursor=60, payload=b"zero"), 0.003)
    assert p.take_result(0).data == b"zero"
    p.feed(response_wire(seq=0, exp=3, cursor=80, payload=b"zero"), 0.004)  # duplicate
    assert p.take_result(0) is None
    p.feed(response_wire(seq=1, exp=3, cursor=100, payload=b"one"), 0.005)
    p.feed(response_wire(seq=2, exp=3, cursor=120, payload=b"two"), 0.005)
    assert p.take_result(1).data == b"one" and p.take_result(2).data == b"two"
    assert p.stats["responses"] == 3


def test_trunc_is_an_error_result_and_not_a_lost_session() -> None:
    p = started(w=4)
    t0, t1 = p.submit(b"a"), p.submit(b"b")
    drain(p, 0.001)
    p.feed(response_wire(seq=0, exp=2, cursor=40, payload=b"par", trunc=True), 0.002)
    r = p.take_result(t0)
    assert r.status is Status.TRUNCATED and r.data == b"par"
    assert p.state is State.ACTIVE
    p.feed(response_wire(seq=1, exp=2, cursor=70, payload=b"ok"), 0.003)
    assert p.take_result(t1).status is Status.OK


def test_fpga_frames_that_break_the_frame_rules_are_ignored() -> None:
    p = started(w=4, rsp_limit=40, bridge_budget=1024)
    t = p.submit(b"a")
    drain(p, 0.001)
    p.feed(response_wire(seq=0, exp=1, cursor=100, payload=bytes(30)), 0.002)  # len 42 > 40
    assert p.take_result(t) is None and p.state is State.ACTIVE
    p.feed(status_wire(SID, w=4, trunc=True, cursor=112), 0.002)  # status with TRUNC
    assert p.consumed == 12
    p.feed(status_wire(SID, w=4, max_len=200, cursor=112), 0.002)  # len != maxLen
    assert p.consumed == 12
    p.feed(response_wire(seq=0, exp=1, cursor=100, payload=bytes(28)), 0.003)  # len 40: fine
    assert p.take_result(t).status is Status.OK


def test_garbage_split_frames_and_overflow_are_handled() -> None:
    p = started(w=4)
    t = p.submit(b"q")
    drain(p, 0.001)
    wire = response_wire(seq=0, exp=1, cursor=40, payload=b"split")
    junk = bytes([7, 8, 9, 0, 0, 0, 1, 2, 0])
    p.feed(junk + wire[:5], 0.002)
    assert p.take_result(t) is None
    p.feed(wire[5:], 0.002)
    assert p.take_result(t).data == b"split"
    p.feed(bytes([1]) * 600 + bytes([0]), 0.003)  # a run without a delimiter
    assert p.state is State.ACTIVE and p.stats["rx_overflow"] >= 1


def test_credit_updates_follow_the_cursor_of_valid_frames_only() -> None:
    p = started(w=4)
    p.submit(b"a")
    drain(p, 0.001)
    p.feed(response_wire(seq=0, exp=1, cursor=300, payload=b"x"), 0.002)
    assert p.consumed == 300
    p.feed(status_wire(SID, cursor=5000)[:-3] + b"\x01\x00", 0.003)  # damaged status
    assert p.consumed == 300
    p.feed(status_wire(SID, cursor=100), 0.004)  # an older cursor does not move consumed back
    assert p.consumed == 300


def test_header_only_frame_gives_new_credit_when_the_fpga_may_be_blocked() -> None:
    p = started(w=4, bridge_budget=336)  # window 336 - 64 - 12 = 260
    for k in range(3):
        p.submit(b"r")
    drain(p, 0.001)
    # The FPGA sent a 255-byte frame: little credit is left for it.
    p.feed(response_wire(seq=0, exp=3, cursor=12 + 255, payload=bytes(243)), 0.002)
    frames = drain(p, 0.002)
    assert len(frames) == 1
    f = parse_host(frames[0])
    assert f["kind"] == "header" and f["ack"] == 1
    assert f["grant"] == u16(12 + 255 + 336 - 64 - 12)
    assert drain(p, 0.002) == []  # nothing more until new credit piles up


# ---------------------------------------------------------------------------------------------
# Timers


def test_go_back_n_resends_all_unaccepted_requests_in_order_after_the_timer() -> None:
    p = started(w=4, rto=0.01)
    for k in range(3):
        p.submit(bytes([k]))
    drain(p, 0.0)
    assert drain(p, 0.005) == []
    again = drain(p, 0.0101)
    assert [parse_host(f)["seq"] for f in again] == [0, 1, 2]
    assert p.stats["go_back_n"] == 1 and p.stats["retransmissions"] == 3
    assert drain(p, 0.015) == []  # backoff: the next resend needs 2 * rto


def test_exp_progress_limits_the_retransmission_to_the_missing_requests() -> None:
    p = started(w=4, rto=0.01)
    for k in range(3):
        p.submit(bytes([k]))
    drain(p, 0.0)
    p.feed(status_wire(SID, exp=2, cursor=24), 0.002)  # requests 0 and 1 arrived
    again = drain(p, 0.0121)
    assert [parse_host(f)["seq"] for f in again if parse_host(f)["kind"] == "data"] == [2]


def test_retry_exhaustion_ends_the_session_with_unknown_outcomes() -> None:
    p = started(w=2, rto=0.01, max_retries=3)
    sent = [p.submit(bytes([k])) for k in range(2)]
    queued = p.submit(b"later")
    drain(p, 0.0)
    t = 0.0
    while p.state is State.ACTIVE and t < 5.0:
        t += 0.005
        drain(p, t)
    assert p.state is State.LOST and "retries" in p.lost_reason
    for ticket in sent:
        r = p.take_result(ticket)
        assert r.status is Status.UNKNOWN_OUTCOME and r.was_sent
    r = p.take_result(queued)
    assert r.status is Status.UNKNOWN_OUTCOME and not r.was_sent
    try:
        p.submit(b"x")
    except SessionClosed:
        pass
    else:
        raise AssertionError("submit after the end of the session must fail")
    assert p.next_frame(t) is None


def test_rewind_epoch_advances_by_one_modulo_four_when_a_response_is_missing() -> None:
    p = started(
        w=4, rewind_timeout=0.02, rto=10.0, poll_interval=10.0, resync_quiet=10.0, max_retries=10
    )
    p.submit(b"a")
    drain(p, 0.0)
    p.feed(status_wire(SID, exp=1, cursor=24), 0.001)  # accepted, no response
    assert drain(p, 0.015) == []
    epochs = []
    t = 0.0211
    for _ in range(5):
        frames = drain(p, t)
        assert len(frames) == 1
        f = parse_host(frames[0])
        assert f["kind"] == "header" and f["poll"]
        epochs.append(f["rewind"])
        t += 0.3  # beyond the backed-off timer (0.02 * 8)
    assert epochs == [1, 2, 3, 0, 1]


def test_response_retries_end_the_session() -> None:
    p = started(
        w=4, rewind_timeout=0.01, max_retries=2, rto=10.0, poll_interval=10.0, resync_quiet=10.0
    )
    p.submit(b"a")
    drain(p, 0.0)
    p.feed(status_wire(SID, exp=1, cursor=24), 0.001)
    t = 0.001
    while p.state is State.ACTIVE and t < 5:
        t += 0.01
        drain(p, t)
    assert p.state is State.LOST and "response retries" in p.lost_reason


def test_idle_timer_sends_poll_after_silence() -> None:
    p = started(w=4, poll_interval=0.03, rto=10.0, rewind_timeout=10.0, resync_quiet=10.0)
    p.submit(b"a")
    drain(p, 0.0)
    assert drain(p, 0.02) == []
    frames = drain(p, 0.0301)
    assert len(frames) == 1 and parse_host(frames[0])["poll"]
    assert drain(p, 0.04) == []


def test_resync_after_a_quiet_period_while_waiting() -> None:
    p = started(w=4, resync_quiet=0.05, rto=10.0, rewind_timeout=10.0, poll_interval=10.0)
    p.submit(b"a")
    drain(p, 0.0)
    assert drain(p, 0.049) == []
    frames = drain(p, 0.0501)
    assert len(frames) == 1
    f = parse_host(frames[0])
    assert f["kind"] == "header" and f["resync"] and f["ctl"] & 1
    assert p.stats["resyncs"] == 1


def test_a_byte_from_the_fpga_postpones_resync_and_valid_frames_reset_the_count() -> None:
    p = started(
        w=4, resync_quiet=0.05, resync_retries=2, rto=10.0, rewind_timeout=10.0, poll_interval=10.0
    )
    p.submit(b"a")
    drain(p, 0.0)
    p.feed(bytes([5, 6, 7, 0]), 0.04)  # garbage still counts as a byte
    assert drain(p, 0.06) == []
    assert len(drain(p, 0.0901)) == 1  # 0.05 after the last byte
    p.feed(status_wire(SID, exp=1, cursor=24), 0.1)  # a valid frame
    assert p._resync_tries == 0


def test_resync_retry_limit_ends_the_session() -> None:
    p = started(
        w=4,
        resync_quiet=0.05,
        resync_retries=2,
        rto=100.0,
        rewind_timeout=100.0,
        poll_interval=100.0,
    )
    t0 = p.submit(b"a")
    drain(p, 0.0)
    t = 0.0
    sent = 0
    while p.state is State.ACTIVE and t < 20:
        t += 0.05
        sent += sum(1 for f in drain(p, t) if parse_host(f)["resync"])
    assert p.state is State.LOST and "RESYNC" in p.lost_reason and sent == 2
    assert p.take_result(t0).status is Status.UNKNOWN_OUTCOME


def test_no_resync_when_nothing_is_awaited() -> None:
    p = started(w=4, resync_quiet=0.05)
    assert drain(p, 10.0) == [] and p.state is State.ACTIVE


def test_resync_can_be_switched_off_with_an_infinite_quiet_period() -> None:
    p = started(
        w=4, resync_quiet=float("inf"), rto=100.0, rewind_timeout=100.0, poll_interval=100.0
    )
    p.submit(b"a")
    drain(p, 0.0)
    assert drain(p, 50.0) == [] and p.stats.get("resyncs") is None


def test_a_frame_valid_under_session_zero_means_unbound() -> None:
    p = started(w=4)
    t0 = p.submit(b"a")
    t1 = p.submit(b"b")
    drain(p, 0.001)
    p.feed(status_wire(sid=0, w=4, cursor=12), 0.002)
    assert p.state is State.LOST and "unbound" in p.lost_reason
    for t in (t0, t1):
        assert p.take_result(t).status is Status.UNKNOWN_OUTCOME


def test_a_corrupted_trailer_byte_cannot_fake_unbound_with_four_nonzero_bytes() -> None:
    rng = random.Random(5)
    for _ in range(50):
        p = HostProtocol(config(), rng=rng)
        p.start(0.0)
        p.next_frame(0.0)
        p.feed(status_wire(p.sid, w=4), 0.001)
        assert p.state is State.ACTIVE
        wire = bytearray(status_wire(p.sid, w=4, cursor=24))
        dec = lc.rcobs_decode(bytes(wire[:-1]))
        for pos in range(-4, 0):
            for value in range(256):
                alt = bytearray(dec)
                alt[pos] = value
                alt_wire = lc.encode_fpga_wire(bytes(alt))
                p2 = HostProtocol(config(), sid=p.sid)
                p2.start(0.0)
                p2.next_frame(0.0)
                p2.feed(status_wire(p.sid, w=4), 0.001)
                p2.feed(alt_wire, 0.002)
                assert p2.state is State.ACTIVE
        break


def test_start_works_once_and_idle_protocols_send_nothing() -> None:
    p = HostProtocol(config(), sid=SID)
    assert p.next_frame(0.0) is None
    p.start(0.0)
    try:
        p.start(1.0)
    except Exception:
        pass
    else:
        raise AssertionError("second start() must fail")


def test_payload_longer_than_max_payload_is_refused_at_submit() -> None:
    p = HostProtocol(config(max_payload=20), sid=SID)
    try:
        p.submit(bytes(21))
    except ValueError:
        pass
    else:
        raise AssertionError
    assert p.submit(bytes(20)) == 0


def test_next_deadline_reports_the_next_timer() -> None:
    p = started(w=4, rto=0.01)
    assert p.next_deadline(0.0) is None
    p.submit(b"a")
    assert p.next_deadline(0.0) == 0.0  # a frame can go out now
    drain(p, 0.0)
    d = p.next_deadline(0.0)
    assert d is not None and abs(d - 0.01) < 1e-9


# ---------------------------------------------------------------------------------------------
# Protocol simulation


QUICK_MIXES = (
    "clean",
    "loss_hi",
    "insert",
    "subst",
    "trunc",
    "delim",
    "mixed_lo",
    "mixed_hi",
    "reboot",
    "abort",
    "overflow",
    "pause",
    "blackout",
    "burst",
    "burst5",
    "stale",
    "zombie",
    "heavy",
)


def test_invariants_hold_in_a_short_campaign() -> None:
    bad = []
    for mix in QUICK_MIXES:
        for seed in range(3):
            res = sim.run_one(mix, seed)
            if not res["ok"]:
                bad.append((mix, seed, res["fails"][:2]))
    assert not bad, bad


def test_liveness_below_the_fault_bound() -> None:
    for mix in ("loss_hi", "mixed_lo", "pause"):
        for seed in range(4):
            res = sim.run_one(mix, seed)
            assert res["liveness_expected"] and res["ok"], res["fails"]
            assert res["metrics"]["statuses"]["unknown_outcome"] == 0
            assert res["metrics"]["sessions"] == 1


# ---------------------------------------------------------------------------------------------
# The checks must be able to fail: mutate the model or the host and expect a violation.


def _failures(mixes, seeds, prefix: str) -> int:
    n = 0
    for mix in mixes:
        for seed in seeds:
            if any(f.startswith(prefix) for f in sim.run_one(mix, seed)["fails"]):
                n += 1
    return n


def test_mutation_model_commits_duplicates_is_caught_by_i1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig = fm.FpgaModel._decide
    real = fm.check_host

    def decide(self, now):
        def patched(wire, s, w, ml):
            frame = real(wire, s, w, ml)
            if frame and frame["data"]:
                frame["seq"] = self.exp  # accept every request as the next one
            return frame

        monkeypatch.setattr(fm, "check_host", patched)
        try:
            orig(self, now)
        finally:
            monkeypatch.setattr(fm, "check_host", real)

    monkeypatch.setattr(fm.FpgaModel, "_decide", decide)
    assert _failures(("loss_hi", "mixed_lo"), range(4), "I1") >= 4


def test_mutation_host_ignores_response_sequence_is_caught_by_i2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig = hp.HostProtocol._on_frame

    def on_frame(self, frame, now):
        if frame["kind"] == "data":
            frame = dict(frame)
            frame["seq"] = self._rsp % self.M
        orig(self, frame, now)

    monkeypatch.setattr(hp.HostProtocol, "_on_frame", on_frame)
    assert _failures(("loss_hi", "mixed_lo"), range(4), "I2") >= 4


def test_mutation_grant_without_reserve_is_caught_by_i3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hp.HostProtocol,
        "_grant",
        lambda self: (self._consumed + self.cfg.bridge_budget) & 0xFFFF,
    )
    assert _failures(("loss_hi", "mixed_lo", "clean"), range(12), "I3") >= 2


def test_mutation_host_forgets_was_sent_is_caught_by_i2(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = hp.HostProtocol._lose

    def lose(self, now, reason):
        orig(self, now, reason)
        for ticket, res in list(self._results.items()):
            if res.status is Status.UNKNOWN_OUTCOME:
                self._results[ticket] = hp.Result(ticket, res.status, b"", False, res.reason)

    monkeypatch.setattr(hp.HostProtocol, "_lose", lose)
    assert _failures(("abort",), range(6), "I2") >= 1


# ---------------------------------------------------------------------------------------------
# Burst loss: all frames lost until the FPGA cannot send even a status frame.


def _burst(resync: bool, seeds):
    lost = unknown = stuck = permit = 0
    for seed in seeds:
        cfg = sim.make_config("burst", seed)
        cfg.resync = resync
        s = sim.Sim(cfg, seed)
        s.run()
        fails = s.verify()
        m = s.metrics()
        if resync:
            assert not fails, fails
        lost += bool(m["lost_reasons"])
        unknown += m["statuses"]["unknown_outcome"]
        stuck += m["burst_stuck"]
        permit += bool(m["fpga"].get("status_by_permit"))
    return lost, unknown, stuck, permit


def test_burst_loss_recovers_through_resync_without_unknown_outcome() -> None:
    lost, unknown, stuck, permit = _burst(True, range(16))
    assert lost == 0 and unknown == 0
    assert stuck >= 6 and permit >= stuck  # the FPGA really was blocked, and the permit freed it


def test_burst_loss_without_resync_loses_the_session() -> None:
    lost, unknown, stuck, _permit = _burst(False, range(16))
    assert stuck >= 6 and lost >= stuck // 2 and unknown > 0


def test_throughput_is_limited_by_the_window_and_the_turnaround() -> None:
    r = sim.run_throughput(w=8, turnaround=1.5e-3, size=64, n=240)
    assert not r["fails"]
    # W requests per host tick: 8 * 64 bytes per 1.5 ms is 341 kB/s.
    assert 0.9 * 341e3 < r["req_goodput_Bps"] < 1.1 * 341e3
    r = sim.run_throughput(w=32, turnaround=0.5e-3, size=236, n=240)
    assert r["req_goodput_frac"] > 0.85
