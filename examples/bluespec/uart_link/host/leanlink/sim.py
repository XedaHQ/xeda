"""Discrete-event protocol simulation: HostProtocol <-> line <-> FpgaModel <-> bridge <-> host.

Put the `host` directory of the example on `PYTHONPATH` and run it as a module::

    python3 -m leanlink.sim campaign --seeds 60 --jobs 4     many fault mixes, invariant checks
    python3 -m leanlink.sim throughput                       goodput table for W and turnaround
    python3 -m leanlink.sim one --mix mixed_hi --seed 7 -v   one run with a trace
    python3 -m leanlink.sim burst --seeds 60                 burst loss with and without RESYNC

Model of the world
------------------
* The line carries 1.2 MB/s (12 Mbaud 8N1) in each direction. The host writes a frame to the
  host-to-FPGA line at the time of a host service. Frames queue on the line.
* The host runs once every ``turnaround`` seconds (a service tick). At a tick it reads the
  bridge FIFO, feeds the protocol, and writes the frames that the protocol returns. A host
  pause skips several ticks: the bridge fills and the host sends nothing.
* The bridge is a FIFO of ``capacity`` bytes between the FPGA-to-host line and the host. The
  host drains it completely at each tick. A byte that arrives when the FIFO is full is lost.
* Line faults (both directions): per byte, loss, insertion of a random byte before the byte,
  and substitution; per frame, truncation (the tail goes, the delimiter stays) and loss of the
  delimiter. An FPGA reboot clears all FPGA state at a random time.

Fault mixes (``MIXES``, one run per seed; the seed also picks W, P, B, turnaround and delay)
-----------------------------------------------------------------------------------------
clean, loss_lo, loss_hi, insert, subst, trunc, delim, mixed_lo: line faults below 1e-3
(liveness is required); mixed_hi, heavy: line faults above the bound; reboot: FPGA reboot;
abort: the host aborts and re-opens while requests are in flight (OPEN with a busy core);
overflow: bridge smaller than B; pause: host reader pauses; blackout: the FPGA-to-host line
drops everything for 0.5 to 40 ms; burst: the FPGA-to-host line drops every frame until the
FPGA cannot send even a status frame (all credit and reserve used), then it is clean; burst5:
the same until five status frames are lost; stale: frames of the dead session reach the FPGA
after the new session started; zombie: the dead session keeps sending timer frames;
stale_open: like stale, with the old OPEN replayed (a replay is outside the channel model of
doc/protocol.md; double execution is a known hazard there and is counted, not failed).

Checks in every run (see ``Sim.verify``)
----------------------------------------
I1  no request executes twice, and the executed requests are submitted requests in order;
I2  every ticket ends with its exact result, a TRUNC error result, or UnknownOutcome after a
    terminal event of its session; a request that never left the host never executes;
I3  the FPGA-to-host bytes in the bridge never exceed B (the origin count, without bytes that
    the line inserted); after an FPGA reboot the bound is B + R;
I4  liveness: with fault rates below 1e-3, no reboot, no abort, no blackout or burst, and no
    overflow fault, every ticket gets its result and no session is lost;
R   recovery: after a blackout or a burst loss the session survives (RESYNC) and no ticket
    ends as UnknownOutcome.
"""

from __future__ import annotations

import argparse
import copy
import heapq
import json
import math
import os
import random
import sys
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from . import codec as lc
from .fpga_model import BYTE_TIME_12M, Emission, FpgaModel
from .protocol import RESERVE, HostConfig, HostProtocol, State, Status

INF = float("inf")
LINE_RATE = 1.0 / BYTE_TIME_12M  # bytes per second, each way
_EPS = 1e-9

# ---------------------------------------------------------------------------------------------
# Line with faults


@dataclass
class Faults:
    p_loss: float = 0.0
    p_insert: float = 0.0
    p_subst: float = 0.0
    p_trunc: float = 0.0  # per frame
    p_delim: float = 0.0  # per frame

    @property
    def p_byte(self) -> float:
        return self.p_loss + self.p_insert + self.p_subst

    @property
    def any(self) -> bool:
        return self.p_byte > 0 or self.p_trunc > 0 or self.p_delim > 0

    @property
    def below_liveness_bound(self) -> bool:
        """True if every rate is below 1e-3 (the bound of invariant I4)."""
        return all(
            rate < 1e-3
            for rate in (self.p_loss, self.p_insert, self.p_subst, self.p_trunc, self.p_delim)
        )


class Segment:
    """Bytes on a line, after faults. ``idx[k]`` is the original byte index of ``data[k]``."""

    __slots__ = ("start", "bt", "data", "idx", "ins", "pos", "em")

    def __init__(self, start, bt, data, idx=None, ins=None, em=None):
        self.start = start
        self.bt = bt
        self.data = data
        self.idx = idx
        self.ins = ins  # sorted positions in ``data`` of inserted bytes, or None
        self.pos = 0  # bytes already extracted
        self.em = em  # the Emission (to read a reboot cut), or None

    def _limit(self, t: float) -> float:
        if self.em is not None and self.em.cut is not None and self.em.cut < t:
            return self.em.cut
        return t

    def upto(self, t: float) -> int:
        """Number of leading bytes of ``data`` that are complete at time ``t``."""
        limit = self._limit(t)
        if limit == INF:
            count = len(self.data) + 1
        else:
            count = int((limit - self.start) / self.bt + _EPS)
        if count <= 0:
            return 0
        if self.idx is None:
            return min(count, len(self.data))
        return bisect_right(self.idx, count - 1)

    def time_of(self, k: int) -> float:
        return self.start + ((self.idx[k] if self.idx is not None else k) + 1) * self.bt

    def inserted_in(self, a: int, b: int) -> int:
        if not self.ins:
            return 0
        return bisect_left(self.ins, b) - bisect_left(self.ins, a)


class Line:
    """One direction of the serial line. ``transform`` applies the faults to one frame."""

    def __init__(self, rng: random.Random, faults: Faults, byte_time: float) -> None:
        self.rng = rng
        self.faults = faults
        self.bt = byte_time
        self.stats = {
            "lost": 0,
            "inserted": 0,
            "substituted": 0,
            "truncated": 0,
            "delim_lost": 0,
            "blackout_lost": 0,
        }
        self.blackout: tuple[float, float] | None = None  # bytes completing in it are lost
        self.countdown = self._geom() if faults.p_byte > 0 else INF

    def _geom(self) -> int:
        p = self.faults.p_byte
        if p >= 1.0:
            return 0
        return int(math.log(1.0 - self.rng.random()) / math.log(1.0 - p))

    def _positions(self, n: int) -> list[int]:
        out: list[int] = []
        c = self.countdown
        while c < n:
            out.append(int(c))
            c += 1 + self._geom()
        self.countdown = c - n
        return out

    def transform(self, start: float, wire: bytes, em: Emission | None = None) -> Segment:
        f = self.faults
        rng = self.rng
        n = len(wire)
        positions = self._positions(n) if f.p_byte > 0 else []
        trunc_at = None
        drop_delim = False
        if f.p_trunc and n > 2 and rng.random() < f.p_trunc:
            trunc_at = rng.randrange(1, n - 1)
            self.stats["truncated"] += 1
        if f.p_delim and rng.random() < f.p_delim:
            drop_delim = True
            self.stats["delim_lost"] += 1
        dark = self.blackout
        if dark is not None and (start + n * self.bt < dark[0] or start >= dark[1]):
            dark = None
        if not positions and trunc_at is None and not drop_delim and dark is None:
            return Segment(start, self.bt, wire, None, None, em)
        kinds: dict[int, str] = {}
        total = f.p_byte
        for j in positions:
            r = rng.random() * total
            if r < f.p_loss:
                kinds[j] = "loss"
            elif r < f.p_loss + f.p_insert:
                kinds[j] = "insert"
            else:
                kinds[j] = "subst"
        out = bytearray()
        idx: list[int] = []
        ins: list[int] = []
        for j in range(n):
            if trunc_at is not None and trunc_at <= j < n - 1:
                continue
            if drop_delim and j == n - 1:
                continue
            if dark is not None and dark[0] <= start + (j + 1) * self.bt < dark[1]:
                self.stats["blackout_lost"] += 1
                continue
            kind = kinds.get(j)
            if kind == "loss":
                self.stats["lost"] += 1
                continue
            if kind == "insert":
                self.stats["inserted"] += 1
                ins.append(len(out))
                out.append(rng.randrange(256))
                idx.append(j)
                out.append(wire[j])
                idx.append(j)
            elif kind == "subst":
                self.stats["substituted"] += 1
                value = rng.randrange(255)
                out.append(value + 1 if value >= wire[j] else value)
                idx.append(j)
            else:
                out.append(wire[j])
                idx.append(j)
        return Segment(start, self.bt, bytes(out), idx, ins, em)


# ---------------------------------------------------------------------------------------------
# Configuration

# name -> Faults. Rates below 1e-3 qualify for the liveness invariant.
MIXES: dict[str, Faults] = {
    "clean": Faults(),
    "loss_lo": Faults(p_loss=1e-4),
    "loss_hi": Faults(p_loss=9e-4),
    "insert": Faults(p_insert=9e-4),
    "subst": Faults(p_subst=9e-4),
    "trunc": Faults(p_trunc=9e-4),
    "delim": Faults(p_delim=9e-4),
    "mixed_lo": Faults(9e-4, 9e-4, 9e-4, 9e-4, 9e-4),
    "mixed_hi": Faults(5e-3, 2e-3, 3e-3, 2e-2, 2e-2),
    "heavy": Faults(2e-2, 1e-2, 1e-2, 1e-1, 1e-1),
    "reboot": Faults(p_loss=3e-4, p_trunc=3e-4),
    "abort": Faults(p_loss=3e-4, p_delim=3e-4),
    "overflow": Faults(p_loss=1e-4),
    "pause": Faults(p_loss=3e-4, p_subst=3e-4),
    "blackout": Faults(p_loss=1e-4),
    "burst": Faults(),
    "burst5": Faults(),
    "stale": Faults(p_loss=2e-4),
    "stale_open": Faults(p_loss=2e-4),
    "zombie": Faults(p_loss=2e-4),
}


@dataclass
class SimConfig:
    mix: str = "clean"
    w: int = 8
    p: int = 256
    bridge_b: int = 4096  # B: the budget the host assumes
    bridge_capacity: int | None = None  # physical FIFO size (None: equal to B)
    rsp_limit: int = 255
    turnaround: float = 1.5e-3
    pause_prob: float = 0.0  # per tick: chance that a pause starts
    pause_ticks: tuple[int, int] = (3, 40)
    core_delay: float = 0.0
    n_requests: int = 100
    sizes: str | int = "mixed"
    faults: Faults = field(default_factory=Faults)
    reboot_at: float | None = None  # seconds, None: no reboot
    blackout: tuple[float, float] | None = None  # FPGA-to-host line drops all bytes (start, end)
    burst_t0: float | None = None  # burst loss: from this time the FPGA-to-host line drops every
    burst_mode: str = "stuck"  # frame. "stuck": until the FPGA cannot send even a status frame;
    burst_status_drop: int = 5  # "status5": until this many status frames were dropped. Then clean.
    burst_max: float = 0.06  # "stuck": give up the burst after this many seconds
    resync: bool = True  # False: the host never sends RESYNC (negative control)
    abort_at: float | None = None  # seconds, None: no host abort and re-OPEN
    max_sessions: int = 8
    pause_after_abort: int = 0  # host pauses this many ticks after the abort and re-OPEN
    stale: bool = False  # after the abort, old frames of the dead session reach the FPGA again
    stale_open: bool = (
        False  # ... including the old OPEN (replay: outside the channel model of doc/protocol.md)
    )
    zombie: bool = False  # the aborted session keeps sending (blind) while the new one opens
    submit_mode: str = "paced"  # "paced" (keep a depth) or "burst" (all at once)
    depth: int = 0  # paced mode: tickets in flight (0: 2 * W)
    max_time: float = 20.0
    drain: float = 0.05
    trace: bool = False

    @property
    def max_len(self) -> int:
        return min(255, self.p + 7)

    @property
    def recovery_expected(self) -> bool:
        """Burst loss or blackout with RESYNC on: no session loss, no UnknownOutcome."""
        return (self.burst_t0 is not None or self.blackout is not None) and self.resync

    @property
    def liveness_expected(self) -> bool:
        return (
            self.faults.below_liveness_bound
            and self.reboot_at is None
            and self.abort_at is None
            and self.blackout is None
            and self.burst_t0 is None
            and (self.bridge_capacity is None or self.bridge_capacity >= self.bridge_b)
        )


def make_config(mix: str, seed: int) -> SimConfig:
    """Derive a configuration from the mix name and the seed."""
    rng = random.Random(f"cfg:{mix}:{seed}")
    w = rng.choice([1, 2, 4, 8, 8, 16, 32, 64, 128])
    p = rng.choice([64, 256, 256, 256])
    max_len = min(255, p + 7)
    rsp_limit = rng.choice([max_len, max_len, max_len, min(max_len, 128), min(max_len, 40), 12])
    b = rng.choice([336, 512, 1024, 2048, 4096])
    b = max(b, 64 + 12 + rsp_limit)
    turnaround = rng.choice([0.5e-3, 1.5e-3, 3e-3])
    core_delay = rng.choice([0.0, 0.0, 20e-6, 200e-6, 2e-3])
    n = rng.randrange(60, 161) if w < 64 else rng.randrange(200, 301)
    cfg = SimConfig(
        mix=mix,
        w=w,
        p=p,
        bridge_b=b,
        rsp_limit=rsp_limit,
        turnaround=turnaround,
        core_delay=core_delay,
        n_requests=n,
        faults=MIXES[mix],
    )
    if mix == "reboot":
        cfg.reboot_at = rng.uniform(0.02, 0.15)
    elif mix == "abort":
        cfg.abort_at = rng.uniform(0.01, 0.1)
    elif mix == "zombie":
        cfg.abort_at = rng.uniform(0.005, 0.06)
        cfg.zombie = True
        cfg.core_delay = rng.choice([200e-6, 2e-3, 5e-3])
    elif mix in ("stale", "stale_open"):
        cfg.abort_at = rng.uniform(0.005, 0.06)
        cfg.stale = True
        cfg.stale_open = mix == "stale_open"
        cfg.core_delay = rng.choice([200e-6, 2e-3, 5e-3])
    elif mix == "overflow":
        cfg.bridge_capacity = max(64, b // rng.choice([2, 3, 4]))
    elif mix == "pause":
        cfg.pause_prob = 0.03
    elif mix in ("burst", "burst5"):
        # A small B and a deep window, so that the lost frames use up all of the data credit.
        cfg.w = rng.choice([8, 16, 32])
        cfg.p = 256
        cfg.rsp_limit = 255
        cfg.bridge_b = rng.choice([336, 512, 1024])
        cfg.turnaround = rng.choice([0.5e-3, 1.5e-3])
        cfg.core_delay = 0.0
        cfg.burst_t0 = rng.uniform(0.003, 0.02)
        cfg.sizes = rng.choice([100, 236, "mixed"])
        cfg.n_requests = 150
        cfg.depth = 2 * cfg.w
        if mix == "burst5":
            cfg.burst_mode = "status5"
    elif mix == "blackout":
        start = rng.uniform(0.01, 0.08)
        cfg.blackout = (start, start + rng.choice([0.5e-3, 1e-3, 2e-3, 4e-3, 8e-3, 16e-3, 40e-3]))
    return cfg


# ---------------------------------------------------------------------------------------------
# Simulation


@dataclass
class _Ticket:
    ident: int
    payload: bytes
    session: int
    local: int
    t_submit: float
    result: object = None
    t_result: float = 0.0


class Sim:
    def __init__(self, cfg: SimConfig, seed: int) -> None:
        self.cfg = cfg
        self.seed = seed
        self.rng = random.Random(f"sim:{cfg.mix}:{seed}")
        bt = BYTE_TIME_12M
        self.bt = bt
        self.fpga = FpgaModel(cfg.w, cfg.p, byte_time=bt, core_delay=cfg.core_delay)
        self.line_h2f = Line(random.Random(f"h2f:{cfg.mix}:{seed}"), cfg.faults, bt)
        self.line_f2h = Line(random.Random(f"f2h:{cfg.mix}:{seed}"), cfg.faults, bt)
        self.line_f2h.blackout = cfg.blackout
        self.hcfg = HostConfig(
            bridge_budget=cfg.bridge_b,
            rsp_limit=cfg.rsp_limit,
            max_payload=min(243, cfg.max_len - 12),
            byte_time=bt,
            **self._timers(),
        )
        self.capacity = cfg.bridge_capacity if cfg.bridge_capacity is not None else cfg.bridge_b
        # Event queue.
        self.heap: list[tuple[float, int, str, object]] = []
        self._seq = 0
        self.now = 0.0
        # Host.
        self.proto: HostProtocol | None = None
        self.sessions: list[HostProtocol] = []
        self.session_active_at: list[float | None] = []
        self.sids: set[int] = set()
        self.tickets: dict[int, _Ticket] = {}
        self.pending: list[_Ticket] = []
        self.next_id = 0
        self.short_used: dict[int, int] = {}
        self.h2f_free = 0.0
        self.aborted = False
        self.rebooted = False
        self.abandoned = False
        self.timed_out = False
        self.burst_left = cfg.burst_status_drop
        self.sent_log: list[bytes] = []
        self.stale_pending: list[bytes] = []
        self.zombies: list[HostProtocol] = []
        self.burst_end: float | None = None
        self.burst_stuck = False
        self.burst_dropped: dict[str, int] = {}
        self.pause_left = 0
        # FPGA-to-host side.
        self.f2h_segments: list[Segment] = []
        self.peak_origin = 0
        self.peak_physical = 0
        self.dropped_bytes = 0
        self.dropped_origin = 0
        self.violations: list[str] = []
        self.finish_time: float | None = None
        self.trace_lines: list[str] = []
        self.bytes_h2f = 0
        self.bytes_f2h = 0
        self.t_first_active: float | None = None
        self.t_last_result = 0.0

    def _timers(self) -> dict[str, float]:
        t = self.cfg.turnaround
        d = self.cfg.core_delay
        return {
            "rto": 3 * t + 4e-3 + 2 * d,
            "rewind_timeout": 4 * t + 6e-3 + 4 * d,
            "poll_interval": 6 * t + 8e-3 + 4 * d,
            "open_timeout": 4 * t + 6e-3,
            "open_retries": 40,
            "max_retries": 12,
            "resync_quiet": (5 * t + 10e-3 + 4 * d) if self.cfg.resync else INF,
            "resync_retries": 8,
        }

    # -- events ----------------------------------------------------------------------------

    def _push(self, t: float, kind: str, payload: object = None) -> None:
        self._seq += 1
        heapq.heappush(self.heap, (t, self._seq, kind, payload))

    def log(self, text: str) -> None:
        if self.cfg.trace:
            self.trace_lines.append(f"{self.now * 1e3:10.4f} ms  {text}")

    # -- workload --------------------------------------------------------------------------

    def _size(self) -> int:
        mode = self.cfg.sizes
        top = self.hcfg.max_payload
        if isinstance(mode, int):
            return min(mode, top)
        r = self.rng.random()
        if r < 0.40:
            return self.rng.randrange(4, min(33, top + 1))
        if r < 0.70:
            return self.rng.randrange(min(33, top), min(121, top + 1))
        if r < 0.85:
            return self.rng.randrange(min(121, top), top + 1)
        if r < 0.90:
            return self.rng.choice(sorted({top, max(0, top - 1), min(top, 239), min(top, 240)}))
        if r < 0.95:
            return self.rng.randrange(0, 4)
        return self.rng.randrange(4, top + 1)

    def make_payload(self, ident: int) -> bytes:
        size = self._size()
        if size == 0:
            if self.short_used.get(0):
                size = 4
            self.short_used[0] = 1
        elif size < 4:
            used = self.short_used.get(size, 0)
            if used >= 200:
                size = 4
            else:
                self.short_used[size] = used + 1
                return (
                    (used + 1).to_bytes(size, "little") if used + 1 < 256**size else b"\x00" * size
                )
        if size == 0:
            return b""
        head = ident.to_bytes(4, "little")
        body = size - 4
        r = self.rng.random()
        if r < 0.04:
            tail = bytes(body)  # zeros
        elif r < 0.08:
            tail = b"\xff" * body
        elif r < 0.12:
            tail = bytes((i % 254) + 1 for i in range(body))  # long run without a zero
        else:
            tail = self.rng.randbytes(body) if body else b""
        return (head + tail)[:size] if size >= 4 else head[:size]

    def submit_more(self, t: float) -> None:
        proto = self.proto
        if proto is None or proto.state not in (State.OPENING, State.ACTIVE):
            return
        if self.cfg.submit_mode == "burst":
            depth = self.cfg.n_requests
        else:
            depth = self.cfg.depth or 2 * self.cfg.w
        while self.next_id < self.cfg.n_requests and len(self.pending) < depth:
            ident = self.next_id
            self.next_id += 1
            payload = self.make_payload(ident)
            local = proto.submit(payload)
            rec = _Ticket(ident, payload, len(self.sessions) - 1, local, t)
            self.tickets[ident] = rec
            self.pending.append(rec)

    def collect_results(self, t: float) -> None:
        still: list[_Ticket] = []
        for rec in self.pending:
            proto = self.sessions[rec.session]
            result = proto.take_result(rec.local)
            if result is None:
                still.append(rec)
            else:
                rec.result = result
                rec.t_result = t
                self.t_last_result = t
        self.pending = still

    # -- sessions --------------------------------------------------------------------------

    def open_session(self, t: float) -> None:
        self.discard_bridge(t)
        proto = HostProtocol(self.hcfg, rng=self.rng)
        while proto.sid in self.sids:
            proto = HostProtocol(self.hcfg, rng=self.rng)
        self.sids.add(proto.sid)
        self.sessions.append(proto)
        self.session_active_at.append(None)
        self.proto = proto
        proto.start(t)
        self.log(f"open session {len(self.sessions) - 1} sid={proto.sid:08x}")

    def manage_session(self, t: float) -> None:
        proto = self.proto
        assert proto is not None
        if proto.state is State.ACTIVE and self.session_active_at[-1] is None:
            self.session_active_at[-1] = t
            if self.t_first_active is None:
                self.t_first_active = t
            self.log(f"session started W={proto.W} maxLen={proto.max_len}")
            for k, frame in enumerate(self.stale_pending):
                # Old frames of the dead session reach the FPGA after the new session started.
                delay = [0.0002, 0.001, 0.003, 0.01, 0.02, 0.05, 0.1, 0.2][k % 8]
                self._push(t + delay, "inject", frame)
            self.stale_pending = []
        if proto.state is State.LOST:
            self.log(f"session lost: {proto.lost_reason}")
            work_left = self.next_id < self.cfg.n_requests or bool(self.pending)
            if work_left or self.next_id < self.cfg.n_requests:
                if len(self.sessions) < self.cfg.max_sessions:
                    self.open_session(t)
                else:
                    self.abandoned = True

    def done(self) -> bool:
        return (self.next_id >= self.cfg.n_requests or self.abandoned) and not self.pending

    # -- the FPGA-to-host path -------------------------------------------------------------

    def pull_emissions(self) -> None:
        for em in self.fpga.pop_emissions():
            if self.burst_drops(em):
                # Burst loss: the frame never reaches the bridge.
                self.burst_dropped[em.kind] = self.burst_dropped.get(em.kind, 0) + 1
                if em.kind == "status":
                    self.burst_left -= 1
                continue
            self.f2h_segments.append(self.line_f2h.transform(em.start, em.wire, em))

    def burst_drops(self, em: Emission) -> bool:
        cfg = self.cfg
        if cfg.burst_t0 is None or em.start < cfg.burst_t0:
            return False
        if cfg.burst_mode == "status5":
            return self.burst_left > 0
        return self.burst_end is None or em.start <= self.burst_end

    def update_burst(self, t: float) -> None:
        """Mode "stuck": the burst ends at the first tick at which the FPGA is credit-blocked."""
        cfg = self.cfg
        if cfg.burst_t0 is None or cfg.burst_mode != "stuck" or self.burst_end is not None:
            return
        if t < cfg.burst_t0:
            return
        f = self.fpga
        nxt = (f.cursor + 12) & 0xFFFF
        gap = (f.grant - nxt) & 0xFFFF
        gap = gap - 0x10000 if gap & 0x8000 else gap
        stuck = f.bound and gap < -f.R and not f.permit
        if stuck or t >= cfg.burst_t0 + cfg.burst_max:
            self.burst_end = t
            self.burst_stuck = stuck
            self.log(f"burst loss ends (FPGA credit-blocked: {stuck})")

    def extract(self, t: float) -> tuple[bytes, int]:
        """Return the bytes that complete by ``t`` on the FPGA-to-host line and their origin
        count (bytes that are not insertions)."""
        data = bytearray()
        origin = 0
        keep: list[Segment] = []
        for seg in self.f2h_segments:
            n = seg.upto(t)
            if n > seg.pos:
                data += seg.data[seg.pos : n]
                origin += (n - seg.pos) - seg.inserted_in(seg.pos, n)
                seg.pos = n
            if seg.pos < seg.upto(INF):
                keep.append(seg)
        self.f2h_segments = keep
        return bytes(data), origin

    def read_bridge(self, t: float) -> bytes:
        data, origin = self.extract(t)
        self.bytes_f2h += len(data)
        total = len(data)
        self.peak_physical = max(self.peak_physical, min(total, self.capacity))
        self.peak_origin = max(self.peak_origin, origin)
        if total > self.capacity:
            self.dropped_bytes += total - self.capacity
            data = data[: self.capacity]
        return data

    def discard_bridge(self, t: float) -> None:
        self.pull_emissions()
        self.read_bridge(t)

    # -- host service ----------------------------------------------------------------------

    def host_service(self, t: float) -> None:
        proto = self.proto
        assert proto is not None
        cfg_stale = self.cfg.stale
        self.fpga.advance(t)
        self.update_burst(t)
        self.pull_emissions()
        data = self.read_bridge(t)
        if data:
            self.log(f"host reads {len(data)} bytes")
            proto.feed(data, t)
        self.collect_results(t)
        self.manage_session(t)
        if (
            self.cfg.abort_at is not None
            and not self.aborted
            and t >= self.cfg.abort_at
            and self.proto.state is State.ACTIVE
        ):
            self.aborted = True
            self.log("host aborts the session and opens a new one")
            stale = []
            if cfg_stale:
                for frame in self.sent_log[-16:]:
                    dec = lc.cobs_decode(frame[:-1])
                    is_open = dec is not None and bool(dec[0] & lc.HOST_OPEN)
                    if self.cfg.stale_open or not is_open:
                        stale.append(frame)
                stale = stale[-8:]
            if self.cfg.zombie:
                # A forgotten sender of the old session: it sends its timer frames, hears nothing.
                zombie = copy.deepcopy(self.proto)
                zombie._queue.clear()  # it sends only requests that already left the host
                self.zombies.append(zombie)
            self.proto.abort(t)
            self.collect_results(t)
            self.open_session(t)
            self.pause_left = self.cfg.pause_after_abort
            self.sent_log = []
            self.stale_pending = stale
        self.submit_more(t)
        self.send_frames(t)
        self.collect_results(t)
        self.manage_session(t)
        self.submit_more(t)

    def put_on_line(self, t: float, frame: bytes) -> None:
        """Write one host frame to the host-to-FPGA line (it queues behind earlier frames)."""
        start = max(t, self.h2f_free)
        self.h2f_free = start + len(frame) * self.bt
        self.bytes_h2f += len(frame)
        seg = self.line_h2f.transform(start, frame)
        if not seg.data:
            return
        n = len(seg.data)
        times = [seg.time_of(k) for k in range(n)]
        if n == 1:
            self._push(times[0], "deliver", (seg.data, times))
        else:
            self._push(times[0], "deliver", (seg.data[:1], times[:1]))
            self._push(times[-1], "deliver", (seg.data[1:], times[1:]))

    def send_frames(self, t: float) -> None:
        proto = self.proto
        assert proto is not None
        while True:
            frame = proto.next_frame(t)
            if frame is None:
                break
            self.sent_log.append(frame)
            self.put_on_line(t, frame)
            self.log(f"host sends frame of {len(frame)} bytes")
        for zombie in self.zombies:
            while True:
                frame = zombie.next_frame(t)
                if frame is None:
                    break
                self.put_on_line(t, frame)
                self.log("zombie of the old session sends a frame")

    def next_tick(self, t: float) -> float:
        return t + self.cfg.turnaround

    # -- main loop -------------------------------------------------------------------------

    def run(self) -> None:
        cfg = self.cfg
        phase = self.rng.random() * cfg.turnaround
        self._push(phase, "tick")
        if cfg.reboot_at is not None:
            self._push(cfg.reboot_at, "reboot")
        self.open_session(0.0)
        self.submit_more(0.0)
        fpga = self.fpga
        while True:
            t_model = fpga.next_event_time()
            t_heap = self.heap[0][0] if self.heap else INF
            if t_model is not None and t_model <= t_heap:
                self.now = t_model
                fpga.advance(t_model)
                continue
            if not self.heap:
                break
            t, _seq, kind, payload = heapq.heappop(self.heap)
            self.now = t
            if kind == "deliver":
                data, times = payload  # type: ignore[misc]
                fpga.rx(data, times)
            elif kind == "inject":
                self.put_on_line(t, payload)  # type: ignore[arg-type]
                self.log("stale frame of the old session reaches the line")
            elif kind == "reboot":
                self.rebooted = True
                downtime = self.rng.uniform(0.0002, 0.003)
                self.log(f"FPGA reboots (down {downtime * 1e3:.2f} ms)")
                fpga.reboot(t, downtime)
            elif kind == "tick":
                if self.pause_left > 0:
                    self.pause_left -= 1
                elif cfg.pause_prob and self.rng.random() < cfg.pause_prob:
                    self.pause_left = self.rng.randrange(*cfg.pause_ticks)
                    self.log(f"host pauses for {self.pause_left} ticks")
                else:
                    self.host_service(t)
                if self.done():
                    self.finish_time = t
                    break
                if t > cfg.max_time:
                    # The caller gives up: every ticket without a result ends as unknown.
                    self.timed_out = True
                    self.log("max_time reached: the host aborts the session")
                    self.proto.abort(t, "simulation time limit")
                    self.collect_results(t)
                    break
                self._push(self.next_tick(t), "tick")
        # Let the FPGA finish what it started.
        end = self.now + cfg.drain
        while True:
            t_model = fpga.next_event_time()
            heap_t = self.heap[0][0] if self.heap else INF
            nxt = min(t for t in (t_model, heap_t) if t is not None)
            if nxt > end:
                break
            if t_model is not None and t_model <= heap_t:
                fpga.advance(t_model)
                continue
            _t, _s, kind, payload = heapq.heappop(self.heap)
            if kind == "deliver":
                data, times = payload  # type: ignore[misc]
                fpga.rx(data, times)
            elif kind == "inject":
                self.put_on_line(_t, payload)  # type: ignore[arg-type]
            elif kind == "reboot":
                fpga.reboot(_t, self.rng.uniform(0.0002, 0.003))
        fpga.advance(end)

    # -- verification ----------------------------------------------------------------------

    def verify(self) -> list[str]:
        cfg = self.cfg
        fails: list[str] = []
        fpga = self.fpga
        by_payload = {rec.payload: rec for rec in self.tickets.values()}
        if len(by_payload) != len(self.tickets):
            fails.append("internal: payloads are not unique")
        # I1: executed requests.
        last = -1
        seen: set[int] = set()
        executed_ident: dict[int, int] = {}
        for k, payload in enumerate(fpga.executed):
            rec = by_payload.get(payload)
            if rec is None:
                fails.append(f"I1: executed request #{k} is no submitted request")
                continue
            if rec.ident in seen:
                fails.append(f"I1: ticket {rec.ident} executed twice")
            seen.add(rec.ident)
            if rec.ident <= last:
                fails.append(f"I1: ticket {rec.ident} executed out of order (after {last})")
            last = max(last, rec.ident)
            executed_ident[rec.ident] = k
        # I2: results.
        rsp_max = max(0, cfg.rsp_limit - 12)
        counts: dict[int, int] = {}
        for rec in self.tickets.values():
            res = rec.result
            if res is None:
                fails.append(f"I2: ticket {rec.ident} has no result at the end of the run")
                continue
            k = executed_ident.get(rec.ident)
            if res.status is Status.UNKNOWN_OUTCOME:
                proto = self.sessions[rec.session]
                if proto.state is not State.LOST:
                    fails.append(f"I2: ticket {rec.ident} unknown, session not terminal")
                if not res.was_sent and k is not None:
                    fails.append(f"I2: ticket {rec.ident} never left the host but executed")
                continue
            if k is None:
                fails.append(f"I2: ticket {rec.ident} has a result but was not executed")
                continue
            full = fpga.exec_counts[k].to_bytes(4, "little") + rec.payload
            if res.status is Status.OK:
                if res.data != full:
                    fails.append(f"I2: ticket {rec.ident} wrong result")
                if len(full) > rsp_max:
                    fails.append(f"I2: ticket {rec.ident} OK but the response is too long")
            else:
                if res.data != full[:rsp_max] or len(full) <= rsp_max:
                    fails.append(f"I2: ticket {rec.ident} wrong TRUNC result")
            counts[rec.ident] = k
        # I3: bridge.
        bound = cfg.bridge_b + (RESERVE if self.rebooted else 0)
        if cfg.bridge_capacity is None and self.peak_origin > bound:
            fails.append(f"I3: bridge held {self.peak_origin} bytes, bound {bound}")
        if self.dropped_bytes and cfg.bridge_capacity is None and not self.rebooted:
            if self.line_f2h.stats["inserted"] == 0:
                fails.append(f"I3: {self.dropped_bytes} bytes overflowed the bridge")
        # I4: liveness.
        if cfg.liveness_expected:
            if self.timed_out or self.finish_time is None:
                fails.append("I4: the run did not finish before max_time (stuck)")
            for rec in self.tickets.values():
                if rec.result is not None and rec.result.status is Status.UNKNOWN_OUTCOME:
                    fails.append(
                        f"I4: ticket {rec.ident} unknown ({rec.result.reason}) at fault rates "
                        "below 1e-3"
                    )
                    break
            if len(self.sessions) > 1:
                fails.append("I4: the session was lost and reopened")
        if cfg.recovery_expected:
            if self.timed_out or self.finish_time is None:
                fails.append("R: the run did not finish after the burst loss")
            if len(self.sessions) > 1:
                fails.append("R: the session was lost after the burst loss")
            if any(
                rec.result is not None and rec.result.status is Status.UNKNOWN_OUTCOME
                for rec in self.tickets.values()
            ):
                fails.append("R: UnknownOutcome after the burst loss")
        return fails

    def metrics(self) -> dict:
        stats: dict[str, int] = {}
        for proto in self.sessions:
            for key, value in proto.stats.items():
                stats[key] = stats.get(key, 0) + value
        statuses = {s.value: 0 for s in Status}
        for rec in self.tickets.values():
            if rec.result is not None:
                statuses[rec.result.status.value] += 1
        return {
            "tickets": len(self.tickets),
            "statuses": statuses,
            "sessions": len(self.sessions),
            "lost_reasons": [p.lost_reason for p in self.sessions if p.lost_reason],
            "executed": len(self.fpga.executed),
            "peak_origin": self.peak_origin,
            "peak_physical": self.peak_physical,
            "dropped_bytes": self.dropped_bytes,
            "host": stats,
            "fpga": dict(self.fpga.stats),
            "line_h2f": dict(self.line_h2f.stats),
            "line_f2h": dict(self.line_f2h.stats),
            "finish_time": self.finish_time,
            "timed_out": self.timed_out,
            "burst_dropped": dict(self.burst_dropped),
            "burst_stuck": self.burst_stuck,
            "bytes_h2f": self.bytes_h2f,
            "bytes_f2h": self.bytes_f2h,
        }


def run_one(mix: str, seed: int, cfg: SimConfig | None = None) -> dict:
    cfg = cfg if cfg is not None else make_config(mix, seed)
    sim = Sim(cfg, seed)
    started = time.monotonic()
    sim.run()
    fails = sim.verify()
    out = {
        "mix": mix,
        "seed": seed,
        "ok": not fails,
        "fails": fails,
        "liveness_expected": cfg.liveness_expected,
        "known_hazard": cfg.stale_open,
        "w": cfg.w,
        "p": cfg.p,
        "b": cfg.bridge_b,
        "turnaround": cfg.turnaround,
        "core_delay": cfg.core_delay,
        "rsp_limit": cfg.rsp_limit,
        "wall": time.monotonic() - started,
        "metrics": sim.metrics(),
    }
    if cfg.trace:
        out["trace"] = sim.trace_lines
    return out


# ---------------------------------------------------------------------------------------------
# Throughput


def run_throughput(w: int, turnaround: float, size: int, n: int = 600, b: int = 4096) -> dict:
    cfg = SimConfig(
        mix="clean",
        w=w,
        p=256,
        bridge_b=b,
        rsp_limit=255,
        turnaround=turnaround,
        n_requests=n,
        sizes=size,
        submit_mode="burst",
        faults=Faults(),
    )
    sim = Sim(cfg, 1)
    sim.run()
    fails = sim.verify()
    start = sim.t_first_active
    stop = sim.t_last_result
    duration = stop - start
    req_rate = n * size / duration
    rsp_rate = n * (size + 4) / duration
    return {
        "w": w,
        "turnaround_ms": turnaround * 1e3,
        "size": size,
        "n": n,
        "b": b,
        "duration_ms": duration * 1e3,
        "req_goodput_Bps": req_rate,
        "req_goodput_frac": req_rate / LINE_RATE,
        "rsp_goodput_frac": rsp_rate / LINE_RATE,
        "ideal_frac": size / (size + 16),
        "fails": fails,
    }


# ---------------------------------------------------------------------------------------------
# Command line


def _campaign_job(args: tuple[str, int]) -> dict:
    mix, seed = args
    try:
        return run_one(mix, seed)
    except Exception as exc:  # report a crash as a failed run
        import traceback

        return {
            "mix": mix,
            "seed": seed,
            "ok": False,
            "liveness_expected": False,
            "fails": [f"crash: {exc!r}", traceback.format_exc()],
            "metrics": {},
        }


def cmd_campaign(ns: argparse.Namespace) -> int:
    mixes = ns.mixes.split(",") if ns.mixes else list(MIXES)
    jobs = [(mix, seed) for mix in mixes for seed in range(ns.first, ns.first + ns.seeds)]
    started = time.monotonic()
    results: list[dict] = []
    if ns.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=ns.jobs) as pool:
            for res in pool.map(_campaign_job, jobs, chunksize=4):
                results.append(res)
    else:
        for job in jobs:
            results.append(_campaign_job(job))
    wall = time.monotonic() - started
    summary = summarize(results)
    summary["wall_s"] = wall
    print(format_summary(summary))
    if ns.out:
        os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
        with open(ns.out, "w") as fh:
            json.dump({"summary": summary, "runs": results}, fh, indent=1, default=str)
        print(f"wrote {ns.out}")
    failed = [r for r in results if not r["ok"] and not r.get("known_hazard")]
    for r in failed[:20]:
        print(f"FAIL {r['mix']} seed {r['seed']}: {r['fails'][:3]}")
    return 1 if failed else 0


def summarize(results: list[dict]) -> dict:
    per_mix: dict[str, dict] = {}
    for r in results:
        m = per_mix.setdefault(
            r["mix"],
            {
                "runs": 0,
                "ok": 0,
                "live_runs": 0,
                "live_ok": 0,
                "tickets": 0,
                "ok_results": 0,
                "trunc": 0,
                "unknown": 0,
                "executed": 0,
                "sessions_lost": 0,
                "peak_origin_max": 0,
                "retrans": 0,
                "rewinds": 0,
                "polls": 0,
            },
        )
        m["runs"] += 1
        m["ok"] += int(r["ok"] or bool(r.get("known_hazard")))
        if r.get("known_hazard"):
            m["hazard_runs"] = m.get("hazard_runs", 0) + 1
            m["hazard_i1"] = m.get("hazard_i1", 0) + int(
                any(f.startswith("I1") for f in r["fails"])
            )
        if r.get("liveness_expected"):
            m["live_runs"] += 1
            m["live_ok"] += int(r["ok"])
        met = r.get("metrics") or {}
        if met:
            m["tickets"] += met["tickets"]
            m["ok_results"] += met["statuses"]["ok"]
            m["trunc"] += met["statuses"]["truncated"]
            m["unknown"] += met["statuses"]["unknown_outcome"]
            m["executed"] += met["executed"]
            m["sessions_lost"] += len(met["lost_reasons"])
            m["peak_origin_max"] = max(m["peak_origin_max"], met["peak_origin"])
            m["retrans"] += met["host"].get("retransmissions", 0)
            m["rewinds"] += met["host"].get("rewinds", 0)
            m["polls"] += met["host"].get("polls", 0)
    total = {
        "runs": len(results),
        "ok": sum(1 for r in results if r["ok"] or r.get("known_hazard")),
        "live_runs": sum(1 for r in results if r.get("liveness_expected")),
        "live_ok": sum(1 for r in results if r.get("liveness_expected") and r["ok"]),
        "tickets": sum(m["tickets"] for m in per_mix.values()),
        "executed": sum(m["executed"] for m in per_mix.values()),
    }
    return {"total": total, "per_mix": per_mix}


def format_summary(summary: dict) -> str:
    lines = [
        f"{'mix':10s} {'runs':>5s} {'ok':>5s} {'live':>5s} {'liveok':>6s} {'tickets':>8s} "
        f"{'OK':>7s} {'TRUNC':>6s} {'UNKN':>6s} {'exec':>7s} {'lost':>5s} {'peakB':>6s} "
        f"{'retx':>6s} {'rew':>5s} {'poll':>5s}"
    ]
    for name, m in summary["per_mix"].items():
        lines.append(
            f"{name:10s} {m['runs']:5d} {m['ok']:5d} {m['live_runs']:5d} {m['live_ok']:6d} "
            f"{m['tickets']:8d} {m['ok_results']:7d} {m['trunc']:6d} {m['unknown']:6d} "
            f"{m['executed']:7d} {m['sessions_lost']:5d} {m['peak_origin_max']:6d} "
            f"{m['retrans']:6d} {m['rewinds']:5d} {m['polls']:5d}"
        )
    t = summary["total"]
    lines.append(
        f"TOTAL runs={t['runs']} ok={t['ok']} live_runs={t['live_runs']} live_ok={t['live_ok']} "
        f"tickets={t['tickets']} executed={t['executed']}"
    )
    return "\n".join(lines)


def cmd_throughput(ns: argparse.Namespace) -> int:
    rows = []
    sizes = [int(s) for s in ns.sizes.split(",")]
    print(
        f"{'size':>5s} {'W':>4s} {'turn ms':>8s} {'dur ms':>9s} {'req kB/s':>9s} "
        f"{'req %':>7s} {'rsp %':>7s} {'ideal %':>8s}"
    )
    for size in sizes:
        for w in (1, 8, 32):
            for turn in (0.5e-3, 1.5e-3, 3e-3):
                r = run_throughput(w, turn, size, n=ns.n, b=ns.b)
                rows.append(r)
                flag = "  FAIL" if r["fails"] else ""
                print(
                    f"{size:5d} {w:4d} {turn * 1e3:8.1f} {r['duration_ms']:9.2f} "
                    f"{r['req_goodput_Bps'] / 1e3:9.1f} {r['req_goodput_frac'] * 100:7.1f} "
                    f"{r['rsp_goodput_frac'] * 100:7.1f} {r['ideal_frac'] * 100:8.1f}{flag}"
                )
    if ns.sweep_b:
        print()
        print("Bridge budget sweep: W = 32, size 236")
        print(f"{'B':>6s} " + " ".join(f"{'T=' + str(t) + ' ms':>12s}" for t in (0.5, 1.5, 3.0)))
        for b in (336, 512, 1024, 2048, 4096):
            cells = []
            for turn in (0.5e-3, 1.5e-3, 3e-3):
                r = run_throughput(32, turn, 236, n=ns.n, b=b)
                rows.append(r)
                cells.append(f"{r['req_goodput_frac'] * 100:11.1f}%")
            print(f"{b:6d} " + " ".join(cells))
    if ns.out:
        os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
        with open(ns.out, "w") as fh:
            json.dump(rows, fh, indent=1)
        print(f"wrote {ns.out}")
    return 1 if any(r["fails"] for r in rows) else 0


def cmd_burst(ns: argparse.Namespace) -> int:
    """Burst loss with RESYNC and without it (the control)."""
    print(
        f"{'mode':8s} {'resync':>6s} {'runs':>5s} {'blocked':>8s} {'permit':>7s} "
        f"{'lost':>5s} {'unknown':>8s} {'verify fails':>12s}"
    )
    bad = 0
    for mode in ("stuck", "status5"):
        for resync in (True, False):
            lost = unknown = blocked = permit = fails = 0
            for seed in range(ns.first, ns.first + ns.seeds):
                cfg = make_config("burst", seed)
                cfg.burst_mode = mode
                cfg.resync = resync
                sim = Sim(cfg, seed)
                sim.run()
                f = sim.verify()
                m = sim.metrics()
                lost += bool(m["lost_reasons"])
                unknown += m["statuses"]["unknown_outcome"]
                blocked += m["burst_stuck"]
                permit += bool(m["fpga"].get("status_by_permit"))
                fails += bool(f)
            if resync:
                bad += fails
            print(
                f"{mode:8s} {str(resync):>6s} {ns.seeds:5d} {blocked:8d} {permit:7d} "
                f"{lost:5d} {unknown:8d} {fails:12d}"
            )
    return 1 if bad else 0


def cmd_one(ns: argparse.Namespace) -> int:
    cfg = make_config(ns.mix, ns.seed)
    cfg.trace = ns.verbose
    res = run_one(ns.mix, ns.seed, cfg)
    for line in res.pop("trace", [])[: ns.trace_lines]:
        print(line)
    print(json.dumps({k: v for k, v in res.items()}, indent=1, default=str))
    return 0 if res["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("campaign")
    c.add_argument("--seeds", type=int, default=60)
    c.add_argument("--first", type=int, default=0)
    c.add_argument("--jobs", type=int, default=1)
    c.add_argument("--mixes", default="")
    c.add_argument("--out", default="")
    c.set_defaults(func=cmd_campaign)
    t = sub.add_parser("throughput")
    t.add_argument("--n", type=int, default=600)
    t.add_argument("--b", type=int, default=4096)
    t.add_argument("--sizes", default="16,64,236")
    t.add_argument("--sweep-b", action="store_true", help="also sweep B at W=32, size 236")
    t.add_argument("--out", default="")
    t.set_defaults(func=cmd_throughput)
    b = sub.add_parser("burst")
    b.add_argument("--seeds", type=int, default=60)
    b.add_argument("--first", type=int, default=0)
    b.set_defaults(func=cmd_burst)
    o = sub.add_parser("one")
    o.add_argument("--mix", default="clean")
    o.add_argument("--seed", type=int, default=0)
    o.add_argument("-v", "--verbose", action="store_true")
    o.add_argument("--trace-lines", type=int, default=200)
    o.set_defaults(func=cmd_one)
    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
