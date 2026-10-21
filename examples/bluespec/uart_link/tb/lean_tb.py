"""Shared test bench for the LeanLink directed tests (cocotb + Verilator).

Parts
-----
- `Bench`      clock, reset, a UART-like byte wire in each direction, frame capture, state snapshot.
- `Frame`      one FPGA frame as captured from the wire (raw bytes, clock of the
               first byte and of the delimiter).
- `Host`       frame builder and bookkeeping for one session (seq, ack, grant, rewind, cursor).
- helpers      expected response bytes, exact frame checks, wire mutations.

Frames are built and parsed only with `host/leanlink/codec.py`.

Time unit: one clock. The RX side of the RTL is the host-to-FPGA byte wire. The TX side is the
FPGA-to-host byte wire. Each wire moves at most one byte per `rx_gap` (`tx_gap`) clocks.
The RTL input needs about 9 clocks per decoded byte, so a gap of 12 or more never backs up.
"""

from __future__ import annotations

import os
import random
import sys
from collections import deque
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

HOST = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST))
from leanlink import codec as lc  # noqa: E402

TOP = os.environ.get("COCOTB_TOPLEVEL", "mkLeanLinkCosimW8")
CONFIGS = {
    "mkLeanLinkCosimW8": dict(W=8, P=256, maxlen=255, log2w=3),
    "mkLeanLinkCosimW1": dict(W=1, P=64, maxlen=71, log2w=0),
    # raw tops (rtl/LeanLinkTops.bsv): the core ports are pins, the test drives the core
    "mkLeanLinkW8P256": dict(W=8, P=256, maxlen=255, log2w=3),
    "mkLeanLinkW1P64": dict(W=1, P=64, maxlen=71, log2w=0),
}
RAW = TOP in ("mkLeanLinkW8P256", "mkLeanLinkW1P64")
CFG = CONFIGS[TOP]
W = CFG["W"]
MAXLEN = CFG["maxlen"]
LOG2W = CFG["log2w"]
SEQ_MOD = 2 * W
R = 64  # status reserve in bytes
B_BIG = 4096  # a bridge budget that never limits a short test

# Internal registers that the tests read (public build). `m_` signals belong to the test top.
STATE_REGS = (
    "link_exp",
    "link_dack",
    "link_grant",
    "link_cons",
    "link_wr",
    "link_txq",
    "link_hi",
    "link_cursor",
    "link_rwHost",
    "link_rwDone",
    "link_sid",
    "link_bound",
    "link_openPending",
    "link_owe",
    "link_permit",
    "link_rspLimit",
    "m_count",
)
BUSY_REGS = (
    "link_rdState",
    "m_phase",
    "link_wActive",
    "link_txActive",
    "link_obValid",
    "link_tbFull",
    "link_crcRun",
    "link_rxAny",
)  # `link_runT` is optional (below)


class TestFailure(AssertionError):
    pass


# ---------------------------------------------------------------------------------------------
# Frames from the FPGA


class Frame:
    """An FPGA frame as seen on the wire."""

    def __init__(self, raw: bytes, t0: int, t1: int):
        self.raw = raw  # wire bytes without the delimiter
        self.t0 = t0  # clock of the first byte
        self.t1 = t1  # clock of the delimiter
        self.p: dict | None = None  # parsed under the session ID
        self.p0: dict | None = None  # parsed under S = 0 (UNBOUND status frame)

    @property
    def wire_len(self) -> int:
        return len(self.raw) + 1

    def parse(self, sid: int):
        self.p = lc.check_fpga_frame(self.raw, sid, W, MAXLEN).frame if sid else None
        self.p0 = lc.check_fpga_frame(self.raw, 0, W, MAXLEN).frame
        return self

    @property
    def kind(self) -> str:
        if self.p is not None:
            return self.p["kind"]
        if self.p0 is not None:
            return "unbound"
        return "bad"

    def __repr__(self):
        d = self.p or self.p0
        return f"Frame({self.kind} t={self.t0}..{self.t1} {d if d else self.raw.hex()})"


# ---------------------------------------------------------------------------------------------
# Bench


class Bench:
    def __init__(self, dut, rx_gap=12, tx_gap=12):
        self.dut = dut
        self.rx_gap = rx_gap  # int, or a callable that returns the next gap
        self.tx_gap = tx_gap
        self.clk = 0
        self.sid = 0  # session ID used to parse FPGA frames
        self.rx_q: deque[int] = deque()
        self.rx_next = 0
        self.rx_wait: deque[tuple[int, int]] = deque()
        self.rx_accepted = 0
        self.rx_max_waiting = 0  # bytes still waiting at the end of a clock
        self.rx_max_incl = 0  # same, counting a byte that arrives in that clock
        self.rx_max_wait_clocks = 0
        self.tx_next = 0
        self.tx_hold = False
        self.tx_bytes: list[tuple[int, int]] = []  # (clock, byte)
        self._cur = bytearray()
        self._cur_t0 = 0
        self._new: list[Frame] = []
        self.all_frames: list[Frame] = []
        self.watch: dict[str, object] = {}
        self.last: dict[str, int] = {}
        self.changes: list[tuple[int, str, int]] = []
        self.hooks: list = []
        self._en_rx = 0
        self._en_tx = 0
        self._has_runT = hasattr(dut, self._resolve("link_runT") or "link_runT")  # optional signal

    # ---- setup ----
    @classmethod
    async def create(cls, dut, rx_gap=12, tx_gap=12):
        # LEAN_GAP_ADD=n adds n clocks to the default gap of 12 (to sweep the phase of the wires)
        add = int(os.environ.get("LEAN_GAP_ADD", "0"))
        if add:
            rx_gap = rx_gap + add if rx_gap == 12 else rx_gap
            tx_gap = tx_gap + add if tx_gap == 12 else tx_gap
        self = cls(dut, rx_gap, tx_gap)
        for name in STATE_REGS + BUSY_REGS:
            r = cls._resolve(name)
            if name == "link_sid" and hasattr(dut, r.replace("sid", "sidLive")):
                continue  # variant with the session ID in a 32x1 RegFile table
            if r is not None and not hasattr(dut, r):
                raise TestFailure(f"internal signal {r} not visible: build with --public-flat-rw")
        self.clock_task = cocotb.start_soon(Clock(dut.CLK, 10, unit="ns").start())
        dut.RST_N.value = 0
        if not RAW:
            dut.stall_s.value = 0
        else:  # the core ports of a raw top: a previous test may have left an enable high
            dut.EN_request_get.value = 0
            dut.EN_response_put.value = 0
            dut.response_put.value = 0
        dut.EN_rx_put.value = 0
        dut.EN_tx_get.value = 0
        dut.rx_put.value = 0
        for _ in range(6):
            await self._edge()
        dut.RST_N.value = 1
        for _ in range(3):
            await self._edge()
        self.clk = 0
        return self

    @staticmethod
    def _resolve(name):
        """Name of an internal signal in the Verilog of this top (None: not in this top)."""
        if RAW:
            return None if name.startswith("m_") else "m_" + name[5:]
        return name

    def close(self):
        """Stop the clock. A test that creates several benches must close each one."""
        self.clock_task.cancel()

    async def _edge(self):
        await FallingEdge(self.dut.CLK)

    # ---- the clock step ----
    async def tick(self):
        dut = self.dut
        await FallingEdge(dut.CLK)
        clk = self.clk = self.clk + 1
        for name, h in self.watch.items():
            v = int(h.value)
            if v != self.last.get(name):
                self.last[name] = v
                self.changes.append((clk, name, v))
        # host to FPGA wire: a byte arrives every rx_gap clocks while the host sends
        if self.rx_q and clk >= self.rx_next:
            self.rx_wait.append((clk, self.rx_q.popleft()))
            g = self.rx_gap() if callable(self.rx_gap) else self.rx_gap
            self.rx_next = clk + g
        put = 0
        if self.rx_wait:
            incl = len(self.rx_wait)
            if incl > self.rx_max_incl:
                self.rx_max_incl = incl
            if int(dut.RDY_rx_put.value):
                arr, b = self.rx_wait.popleft()
                put = 1
                dut.rx_put.value = b
                self.rx_accepted += 1
                w = clk - arr
                if w > self.rx_max_wait_clocks:
                    self.rx_max_wait_clocks = w
            if len(self.rx_wait) > self.rx_max_waiting:
                self.rx_max_waiting = len(self.rx_wait)
        if put != self._en_rx:
            dut.EN_rx_put.value = put
            self._en_rx = put
        # FPGA to host wire: the PHY takes one byte per tx_gap clocks when it is free
        get = 0
        if clk >= self.tx_next and not self.tx_hold and int(dut.RDY_tx_get.value):
            b = int(dut.tx_get.value)
            get = 1
            g = self.tx_gap() if callable(self.tx_gap) else self.tx_gap
            self.tx_next = clk + g
            self.tx_bytes.append((clk, b))
            self._collect(clk, b)
        if get != self._en_tx:
            dut.EN_tx_get.value = get
            self._en_tx = get
        for h in self.hooks:
            h(self, clk)

    def _collect(self, clk, b):
        if b == 0:
            if self._cur:
                f = Frame(bytes(self._cur), self._cur_t0, clk)
                self._cur = bytearray()
                self._new.append(f)
                self.all_frames.append(f)
        else:
            if not self._cur:
                self._cur_t0 = clk
            self._cur.append(b)

    # ---- running ----
    async def run(self, n):
        for _ in range(n):
            await self.tick()

    async def until(self, pred, limit=20000, what="condition"):
        for _ in range(limit):
            if pred():
                return
            await self.tick()
        if pred():
            return
        raise TestFailure(f"timeout after {limit} clocks waiting for {what}\n{self.dump()}")

    def sig(self, name) -> int:
        r = self._resolve(name)
        if name == "link_sid" and not hasattr(self.dut, r):
            # area variant: the live session ID is the table `sidLive` (bit i at index i)
            arr = getattr(self.dut, r.replace("sid", "sidLive")).arr
            return sum(int(arr[i].value) << i for i in range(32))
        return int(getattr(self.dut, r).value)  # `m_count` and `m_phase`: test top only

    def busy(self) -> bool:
        s = self.sig
        return bool(
            self.rx_q
            or self.rx_wait
            or s("link_crcRun")
            or s("link_rdState")
            or (s("m_phase") if not RAW else 0)
            or s("link_wActive")
            or s("link_txActive")
            or (s("link_runT") if self._has_runT else 0)
            or s("link_obValid")
            or s("link_tbFull")
            or (s("link_cons") != s("link_exp") and not s("link_openPending"))
        )

    async def settle(self, n=10, limit=400000):
        """Run until the RTL has done nothing for n consecutive clocks."""
        quiet = 0
        for _ in range(limit):
            await self.tick()
            quiet = 0 if self.busy() else quiet + 1
            if quiet >= n:
                return
        raise TestFailure(f"no settle after {limit} clocks\n{self.dump()}")

    def stall(self, on: bool):
        self.dut.stall_s.value = int(on)

    # ---- bytes in and frames out ----
    def send(self, wire: bytes):
        self.rx_q.extend(wire)

    def frames(self) -> list[Frame]:
        out, self._new = self._new, []
        for f in out:
            f.parse(self.sid)
        return out

    def peek_frames(self) -> list[Frame]:
        for f in self._new:
            f.parse(self.sid)
        return list(self._new)

    def snapshot(self) -> dict:
        return {n: self.sig(n) for n in STATE_REGS if self._resolve(n) is not None}

    def dump(self) -> str:
        s = {n: self.sig(n) for n in STATE_REGS + BUSY_REGS if self._resolve(n) is not None}
        if self._has_runT:
            s["link_runT"] = self.sig("link_runT")
        return f"clk={self.clk} rx_q={len(self.rx_q)} rx_wait={len(self.rx_wait)} state={s}"

    def watch_signal(self, name):
        self.watch[name] = getattr(self.dut, name)

    def changes_of(self, name):
        return [(c, v) for c, n, v in self.changes if n == name]


# ---------------------------------------------------------------------------------------------
# Expected values


def resp_payload(count: int, req: bytes, limit: int | None = None) -> bytes:
    """Response of the test endpoint: 32-bit little-endian count, then the request payload."""
    p = count.to_bytes(4, "little") + bytes(req)
    return p if limit is None else p[:limit]


def mod(x: int) -> int:
    return x % SEQ_MOD


def mk_sid(seed: int) -> int:
    """A session ID with all four bytes nonzero (doc/protocol.md), the same for the same seed."""
    return int.from_bytes(bytes(1 + ((seed * 37 + i * 91 + 13) % 255) for i in range(4)), "little")


def rand_sid(rng) -> int:
    return int.from_bytes(bytes(rng.randrange(1, 256) for _ in range(4)), "little")


def check_fields(f: Frame, **want):
    """Assert exact fields of a valid frame (under the session ID)."""
    if f.p is None:
        raise TestFailure(f"frame is not valid under the session ID: {f}")
    for k, v in want.items():
        if f.p.get(k) != v:
            raise TestFailure(f"field {k}: got {f.p.get(k)!r}, want {v!r} in {f.p}")


def status_ok(f: Frame, *, seq=None, exp=None, cursor=None, sid_ok=True):
    """Check a status frame (decoded length 10, wire length 12) under the session ID."""
    check_fields(f, kind="status", ctl=0x80 | LOG2W, len=MAXLEN, payload=b"")
    if f.wire_len != 12:
        raise TestFailure(f"status frame wire length {f.wire_len}")
    w = {}
    if seq is not None:
        w["seq"] = seq
    if exp is not None:
        w["exp"] = exp
    if cursor is not None:
        w["cursor"] = cursor
    check_fields(f, **w)


# ---------------------------------------------------------------------------------------------
# Wire mutations (on the COBS wire bytes of one host frame, delimiter included)


def body(wire: bytes) -> bytes:
    assert wire[-1] == 0
    return wire[:-1]


def flip_bit(wire: bytes, pos: int, bit: int) -> bytes:
    b = bytearray(body(wire))
    b[pos] ^= 1 << bit
    if b[pos] == 0:
        b[pos] ^= 1 << ((bit + 1) % 8)
    return bytes(b) + b"\x00"


def delete_byte(wire: bytes, pos: int) -> bytes:
    b = bytearray(body(wire))
    del b[pos]
    return bytes(b) + b"\x00"


def insert_byte(wire: bytes, pos: int, val: int) -> bytes:
    assert val != 0
    b = bytearray(body(wire))
    b.insert(pos, val)
    return bytes(b) + b"\x00"


def truncate(wire: bytes, keep: int) -> bytes:
    return body(wire)[:keep] + b"\x00"


# ---------------------------------------------------------------------------------------------
# Host


class Host:
    """Frame builder and bookkeeping for one session. It does not hide values: a test passes
    `seq`, `ack`, `grant` and `rewind` explicitly when it needs an exact frame."""

    def __init__(self, bench: Bench, sid: int, rsp_limit: int | None = None, B: int = B_BIG):
        self.b = bench
        self.sid = sid
        self.rsp_limit = MAXLEN if rsp_limit is None else rsp_limit
        self.B = B
        self.seq = 0  # next request sequence number (modulo 2W)
        self.ack = 0  # next response the host needs (modulo 2W)
        self.rewind = 0  # REWIND epoch
        self.consumed = 0  # cursor of the last valid FPGA frame
        self.count = 0  # requests the endpoint executed so far (the `executed` output)
        self.frames_seen: list[Frame] = []

    # ---- builders ----
    def grant_for(self, consumed: int | None = None) -> int:
        c = self.consumed if consumed is None else consumed
        return (c + self.B - R) & 0xFFFF

    def open_wire(
        self,
        *,
        rsp_limit=None,
        sid=None,
        grant=None,
        len_field=None,
        payload=None,
        data=False,
        poll=False,
        rewind=0,
        seq=0,
        ack=0,
        crc_sid=0,
    ) -> bytes:
        sid = self.sid if sid is None else sid
        lim = self.rsp_limit if rsp_limit is None else rsp_limit
        g = 0 if grant is None else grant  # the grant of an OPEN frame is 0 (doc/protocol.md)
        pl = sid.to_bytes(4, "little") + bytes([lim]) if payload is None else payload
        ctl = lc.host_ctl(open_frame=True, data=data, poll=poll, rewind=rewind)
        dec = lc.build_host_frame(ctl, seq, ack, g, pl, crc_sid, len_field)
        return lc.encode_host_wire(dec)

    def wire(
        self,
        *,
        data=False,
        poll=False,
        rewind=None,
        seq=None,
        ack=None,
        grant=None,
        payload=b"",
        sid=None,
        ctl=None,
        len_field=None,
        open_=False,
        resync=False,
    ) -> bytes:
        if ctl is None:
            ctl = lc.host_ctl(
                open_frame=open_,
                data=data,
                poll=poll,
                rewind=self.rewind if rewind is None else rewind,
            ) | int(resync)
        s = self.seq if seq is None else seq
        a = self.ack if ack is None else ack
        g = self.grant_for() if grant is None else grant
        dec = lc.build_host_frame(
            ctl, s, a, g, payload, self.sid if sid is None else sid, len_field
        )
        return lc.encode_host_wire(dec)

    def req(self, payload=b"", *, seq=None, advance=True, **kw) -> bytes:
        """A DATA request. With `seq` None it uses and advances the next sequence number."""
        if seq is None:
            seq = self.seq
            if advance:
                self.seq = mod(self.seq + 1)
        return self.wire(data=True, seq=seq, payload=payload, **kw)

    def hdr(self, **kw) -> bytes:
        """A header-only frame (carries ack, grant, REWIND and POLL)."""
        return self.wire(**kw)

    # ---- receive bookkeeping ----
    def absorb(self, frames: list[Frame], check_cursor=True) -> list[Frame]:
        """Check the invariants of every frame that is valid under the session ID, keep the
        cursor, and return those frames. Frames that are not valid under the ID are returned
        unchecked in `self.other` for the test."""
        mine = []
        self.other = []
        for f in frames:
            self.frames_seen.append(f)
            if f.p is None:
                self.other.append(f)
                continue
            p = f.p
            want = (self.consumed + f.wire_len) & 0xFFFF
            if check_cursor and p["cursor"] != want:
                raise TestFailure(f"cursor {p['cursor']} but {want} expected: {f}")
            if p["kind"] == "data" and p["len"] != f.wire_len:
                raise TestFailure(f"len field {p['len']} != wire length {f.wire_len}: {f}")
            self.consumed = p["cursor"]
            mine.append(f)
        return mine

    async def open(self, *, rsp_limit=None, grant=None, expect_reply=True, limit=60000):
        """Send OPEN and take the reply. Checks the exact reply fields."""
        b = self.b
        if rsp_limit is not None:
            self.rsp_limit = rsp_limit
        b.sid = self.sid
        b.frames()
        b.send(self.open_wire(grant=grant))
        self.rsp_limit = min(
            self.rsp_limit, MAXLEN
        )  # the FPGA clamps rspLimit to maxLen (doc/protocol.md)
        self.seq = 0
        self.ack = 0
        self.consumed = 0
        if not expect_reply:
            return None
        await b.until(lambda: bool(b._new), limit, "OPEN reply")
        await b.settle()
        fr = self.absorb(b.frames())
        if len(fr) != 1 or fr[0].kind != "status":
            raise TestFailure(f"OPEN: expected exactly one status frame, got {fr} / {self.other}")
        status_ok(fr[0], seq=0, exp=0, cursor=12)
        return fr[0]

    async def collect(self, n_data=None, *, limit=60000, settle=True) -> list[Frame]:
        """Run until n_data DATA frames arrived (or until settled), absorb, and return the
        frames valid under the session ID."""
        b = self.b
        got: list[Frame] = []
        if n_data is not None:

            def enough():
                return sum(1 for f in b.peek_frames() if f.kind == "data") >= n_data

            await b.until(enough, limit, f"{n_data} DATA frames")
        if settle:
            await b.settle()
        got = self.absorb(b.frames())
        return got

    async def transact(self, payload=b"", *, limit=60000, **kw) -> Frame:
        """Send one request with the next sequence number and check its response exactly:
        sequence, `exp`, TRUNC, payload (the count of earlier executions, then the request)."""
        b = self.b
        seq = self.seq
        b.send(self.req(payload, **kw))
        fr = await self.collect(1, limit=limit)
        if len(fr) != 1 or fr[0].kind != "data":
            raise TestFailure(f"expected one DATA frame, got {fr} / {self.other}")
        full = resp_payload(self.count, payload)
        lim = self.rsp_limit - 12
        check_fields(fr[0], seq=seq, exp=self.seq, payload=full[:lim], trunc=len(full) > lim)
        self.count += 1
        self.ack = mod(self.ack + 1)
        if b.sig("m_count") != self.count:
            raise TestFailure(f"executed {b.sig('m_count')} but {self.count} expected")
        return fr[0]


# ---------------------------------------------------------------------------------------------
# A fault-free host behind a bridge of B bytes (used for credit, wrap and long runs)


class Bridge:
    """A fault-free host behind a bridge of B bytes. The host reads FPGA bytes at its own pace.
    It sends `grant = consumed + B - R`, where `consumed` is the cursor of the last frame that it
    read completely. A request goes out only after the response to request s - W was read."""

    def __init__(self, b: Bench, h: Host, reqs, B, read_gap, pause_prob, pause_range, rng):
        self.b, self.h, self.reqs, self.B = b, h, reqs, B
        self.read_gap, self.pause_prob, self.pause_range, self.rng = (
            read_gap,
            pause_prob,
            pause_range,
            rng,
        )
        self.read_idx = len(b.tx_bytes)  # earlier bytes (the OPEN reply) were taken already
        self.cur = bytearray()
        self.next_read = 0
        self.pause_until = 0
        self.sent = 0
        self.rsp = 0  # responses taken
        self.need_update = False
        self.max_inflight = 0
        self.wraps = 0
        self.pauses = 0
        self.status_frames = 0
        self.blocked_clocks = 0
        self.done_time = None

    def on_frame(self, f: Frame):
        h, _b = self.h, self.b
        f.parse(h.sid)
        if f.p is None:
            raise TestFailure(f"bad FPGA frame {f}")
        want = (h.consumed + f.wire_len) & 0xFFFF
        if f.p["cursor"] != want:
            raise TestFailure(f"cursor {f.p['cursor']} but {want} expected: {f}")
        if f.p["cursor"] < h.consumed:
            self.wraps += 1
        h.consumed = f.p["cursor"]
        if f.kind == "status":
            self.status_frames += 1
        else:
            i = self.rsp
            if f.p["seq"] != mod(i):
                raise TestFailure(f"response {i}: seq {f.p['seq']}")
            want_pay = resp_payload(i, self.reqs[i])
            if f.p["payload"] != want_pay:
                raise TestFailure(f"response {i}: payload differs {f.p['payload'][:8]!r}")
            self.rsp += 1
            h.ack = mod(self.rsp)
        self.need_update = True

    def hook(self, b: Bench, clk: int):
        # the reader
        if clk >= self.pause_until and clk >= self.next_read and self.read_idx < len(b.tx_bytes):
            byte = b.tx_bytes[self.read_idx][1]
            self.read_idx += 1
            self.next_read = clk + self.read_gap
            if byte == 0:
                if self.cur:
                    f = Frame(bytes(self.cur), 0, clk)
                    self.cur = bytearray()
                    self.on_frame(f)
            else:
                self.cur.append(byte)
            if self.rng.random() < self.pause_prob:
                self.pause_until = clk + self.rng.randrange(*self.pause_range)
                self.pauses += 1
        inflight = len(b.tx_bytes) - self.read_idx
        if inflight > self.max_inflight:
            self.max_inflight = inflight
        if inflight > self.B:
            raise TestFailure(f"bridge overflow: {inflight} bytes in flight, budget {self.B}")
        # the sender: one frame at a time
        if not b.rx_q and not b.rx_wait:
            h = self.h
            if self.sent < len(self.reqs) and self.sent < self.rsp + W:
                b.send(h.req(self.reqs[self.sent], ack=h.ack, grant=h.grant_for()))
                self.sent += 1
                self.need_update = False
            elif self.need_update:
                b.send(h.hdr(ack=h.ack, grant=h.grant_for()))
                self.need_update = False
        if self.rsp == len(self.reqs) and self.done_time is None:
            self.done_time = clk


async def run_bridge(
    dut,
    B,
    read_gap,
    pause_prob,
    pause_range,
    seed,
    n=None,
    min_bytes=0,
    max_len=100,
    limit=4_000_000,
):
    rng = random.Random(seed)
    b = await Bench.create(dut, rx_gap=12, tx_gap=12)
    h = Host(b, 0x4C4C0101)
    await h.open()
    h.B = B
    maxp = min(max_len, MAXLEN - 12 - 4)
    # the number of requests: fixed, or enough for min_bytes of FPGA traffic
    mean = 12 + 4 + maxp / 2
    count = n if n is not None else int(min_bytes / mean * 1.12) + 20
    reqs = [
        bytes(rng.randrange(256) for _ in range(rng.randrange(0, maxp + 1))) for _ in range(count)
    ]
    br = Bridge(b, h, reqs, B, read_gap, pause_prob, pause_range, rng)
    b.hooks.append(br.hook)
    br.need_update = True
    for _ in range(limit):
        await b.tick()
        if br.done_time is not None and br.done_time < b.clk - 3000:
            break
    else:
        raise TestFailure(f"bridge run did not finish: {br.rsp}/{len(reqs)} responses, {b.dump()}")
    b.hooks.remove(br.hook)
    b.frames()  # the bridge reader took these frames; drop the bench copies
    assert br.rsp == len(reqs) and b.sig("m_count") == len(reqs)
    total = len(b.tx_bytes)
    dut._log.info(
        "bridge: B=%d read_gap=%d: %d requests, %d FPGA bytes, max in flight %d, %d status "
        "frames, %d pauses, %d cursor wraps, %d clocks",
        B,
        read_gap,
        len(reqs),
        total,
        br.max_inflight,
        br.status_frames,
        br.pauses,
        br.wraps,
        b.clk,
    )
    return b, h, br, total


# ---------------------------------------------------------------------------------------------
# The core side of a raw top (rtl/LeanLinkTops.bsv): the test plays the core


class RawCore:
    """Drives `request.get` and `response.put` of a raw top, one token per clock at most.

    `take`: take request tokens when the RTL offers them (up to `take_limit` tokens in all).
    `out`: response tokens (data, last, empty) to put when `RDY_response_put` is high.
    Logs: `req_tokens` (clock, data, last, empty) and `put_log` (clock, data, last, empty)."""

    def __init__(self, bench: Bench):
        self.b = bench
        self.dut = bench.dut
        self.take = False
        self.take_limit = None
        self.out: deque = deque()
        self.req_tokens: list = []
        self.put_log: list = []
        self.put_enable = True
        self._g = 0
        self._p = 0
        for sig in ("EN_request_get", "EN_response_put", "response_put"):
            getattr(self.dut, sig).value = 0
        bench.hooks.append(self.hook)

    def hook(self, b: Bench, clk: int):
        dut = self.dut
        g = p = 0
        if (
            self.take
            and (self.take_limit is None or len(self.req_tokens) < self.take_limit)
            and int(dut.RDY_request_get.value)
        ):
            v = int(dut.request_get.value)
            g = 1
            self.req_tokens.append((clk, (v >> 2) & 0xFF, (v >> 1) & 1, v & 1))
        if self.out and self.put_enable and int(dut.RDY_response_put.value):
            d, last, emp = self.out.popleft()
            p = 1
            dut.response_put.value = (d << 2) | (int(last) << 1) | int(emp)
            self.put_log.append((clk, d, int(last), int(emp)))
        if g != self._g:
            dut.EN_request_get.value = g
            self._g = g
        if p != self._p:
            dut.EN_response_put.value = p
            self._p = p

    def respond(self, payload: bytes):
        """Queue a whole response: one token per byte (`last` on the final one), or one empty
        token for a zero-length response."""
        if not payload:
            self.out.append((0, True, True))
        else:
            for i, byte in enumerate(payload):
                self.out.append((byte, i == len(payload) - 1, False))

    def request_packets(self) -> list:
        """The logged request tokens grouped into packets: (bytes, empty_flag) per packet."""
        pk, cur = [], bytearray()
        for _, d, last, emp in self.req_tokens:
            if emp:
                assert last, "empty token that is not last"
                pk.append((b"", True))
                continue
            cur.append(d)
            if last:
                pk.append((bytes(cur), False))
                cur = bytearray()
        assert not cur, "a request packet is not finished"
        return pk
