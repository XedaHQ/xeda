"""Frame-level reference model of the FPGA side of the lean UART transport.

This model is an independent reading of doc/protocol.md (the wire format and the FPGA behavior).
It does not use any RTL source. It reads wire bytes (with arrival times) and produces wire
frames (with start times). It models state, decide rules, TX boundary priorities, credit with
the status reserve and the boot grant, pages and the length table, OPEN quiescence, TRUNC, and
UNBOUND status frames.

The core is an in-order endpoint (the "test top"). It reads a request to its last token,
waits ``core_delay`` seconds, and then writes one response. The response is the 32-bit
little-endian count of requests executed before it, followed by the request payload. The list
``executed`` holds the payload of every request that the core really executed, in order.

Time
----
All times are seconds (floats). The caller drives time in two ways:

* ``rx(data, times)`` gives wire bytes and the time at which each byte arrives. Bytes must be
  given in time order.
* ``advance(now)`` runs the internal events (core steps, end of a TX frame) up to ``now``.

A frame that the model starts is returned by ``pop_emissions()`` as an ``Emission``. Byte ``i``
of an emission is complete at ``start + (i + 1) * byte_time``. An FPGA reboot cuts the frame
that is in progress (``Emission.cut``).

Host ctl bit 0 is RESYNC (a one-shot permit for one status frame without credit). A session
reset invalidates the RX frame in progress (``sessGen``).

Where doc/protocol.md leaves a point open, the text says "Interpretation".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from . import codec as lc
from .protocol import RESERVE

BYTE_TIME_12M = 10.0 / 12e6  # 12 Mbaud, 8N1: 10 bit times per byte
STATUS_LEN = 12
_EPS = 1e-9


def _signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


HOST_RESYNC = 0x01


def check_host(wire: bytes, sid: int, w: int, max_len: int) -> dict | None:
    """Parse host wire bytes with the codec. Return the frame, with ``frame["resync"]``, or None."""
    frame = lc.check_host_frame(wire, sid, w, max_len).frame
    if frame is None:
        return None
    frame = dict(frame)
    frame["resync"] = bool(frame["ctl"] & HOST_RESYNC)
    return frame


@dataclass
class Emission:
    """One frame that the FPGA transmits (wire bytes, delimiter included)."""

    start: float
    wire: bytes
    kind: str  # "data" or "status"
    byte_time: float
    boot: int  # boot counter value when the frame started
    cut: float | None = None  # time of a reboot that cut the frame

    def available(self, now: float) -> int:
        """Number of bytes that are complete at ``now`` (respecting a reboot cut)."""
        limit = now if self.cut is None else min(now, self.cut)
        count = int((limit - self.start) / self.byte_time + _EPS)
        return max(0, min(len(self.wire), count))

    def byte_end(self, index: int) -> float:
        """Completion time of byte ``index``."""
        return self.start + (index + 1) * self.byte_time

    @property
    def end(self) -> float:
        return self.start + len(self.wire) * self.byte_time


@dataclass
class _RspEntry:
    payload: bytes
    trunc: bool

    @property
    def wire_len(self) -> int:
        return len(self.payload) + 12


class FpgaModel:
    """Frame-level model of the FPGA transport core with an in-order endpoint behind it."""

    def __init__(
        self,
        w: int = 8,
        p: int = 256,
        *,
        byte_time: float = BYTE_TIME_12M,
        token_time: float = 20e-9,
        core_delay: float | Callable[[bytes], float] = 0.0,
        boot_grant: int = 0,
    ) -> None:
        if w not in (1, 2, 4, 8, 16, 32, 64, 128):
            raise ValueError(f"W must be a power of two from 1 to 128, got {w}")
        if p not in (16, 32, 64, 128, 256):
            raise ValueError(f"P must be a power of two from 16 to 256, got {p}")
        self.W = w
        self.M = 2 * w
        self.P = p
        self.R = RESERVE
        self.max_len = min(255, p + 7)
        self.byte_time = byte_time
        self.token_time = token_time
        self.core_delay = core_delay
        self.boot_grant = boot_grant & 0xFFFF
        self.core_stall = False  # True: the core takes no new request (an idle core that refuses)
        self.log2w = w.bit_length() - 1
        # History that survives a reboot.
        self.executed: list[bytes] = []
        self.exec_counts: list[int] = []  # response count field of each executed request
        self.exec_boots: list[int] = []  # boot number of each executed request
        self.exec_times: list[float] = []  # time at which each request executed
        self.boot_times: list[float] = [0.0]
        self.emissions: list[Emission] = []
        self.stats: dict[str, int] = {}
        self._boot_until = -1.0
        self._power_on(0.0)

    # ------------------------------------------------------------------------------------
    # State

    def _count(self, name: str) -> None:
        self.stats[name] = self.stats.get(name, 0) + 1

    def _power_on(self, now: float) -> None:
        self._t = now
        # Session state.
        self.sid = 0
        self.bound = False
        self.rsp_limit = 0
        self.pend_sid = 0
        self.pend_limit = 0
        self.open_pending = False
        self.exp = 0
        self.dack = 0
        self.grant = self.boot_grant
        self.rw_host = 0
        self.owe = False
        self.cons = 0
        self.wr = 0
        self.tx = 0
        self.hi = 0
        self.cursor = 0
        self.rw_done = 0
        self.permit = False  # RESYNC: one status frame may start without credit
        self.sess_gen = False  # toggled by every session reset
        # Pages and length table.
        self.req_pages: list[bytes] = [b""] * self.W
        self.rsp_pages: list[_RspEntry] = [_RspEntry(b"", False)] * self.W
        # RX frame in progress.
        self._rx_buf = bytearray()
        self._rx_started = False
        self._rx_over = False
        self._rx_land = False
        self._rx_gen = False
        # TX frame in progress.
        self._tx_frame: Emission | None = None
        self._tx_kind = ""
        self._tx_index = 0
        self._tx_end: float | None = None
        # Core.
        self._phase = "idle"  # idle, read, delay, wait, write
        self._core_t: float | None = None
        self._cur_req = b""
        self._cur_rsp: _RspEntry | None = None
        self.exec_count = 0  # requests executed since the last reboot

    def reboot(self, now: float, downtime: float = 0.0) -> None:
        """Power-cycle the FPGA at ``now``. It ignores input until ``now + downtime``."""
        self.advance(now)
        if self._tx_frame is not None:
            self._tx_frame.cut = now
        self._power_on(now)
        self._boot_until = now + downtime
        self.boot_times.append(now)
        self._count("reboots")

    @property
    def boot(self) -> int:
        return len(self.boot_times) - 1

    def state(self) -> dict[str, int | bool | str]:
        """Return a snapshot of the doc/protocol.md state (for tests and traces)."""
        return {
            "bound": self.bound,
            "sid": self.sid,
            "rspLimit": self.rsp_limit,
            "openPending": self.open_pending,
            "exp": self.exp,
            "dack": self.dack,
            "grant": self.grant,
            "rwHost": self.rw_host,
            "owe": self.owe,
            "cons": self.cons,
            "wr": self.wr,
            "tx": self.tx,
            "hi": self.hi,
            "cursor": self.cursor,
            "rwDone": self.rw_done,
            "permit": self.permit,
            "core": self._phase,
            "tx_busy": self._tx_frame is not None,
        }

    # ------------------------------------------------------------------------------------
    # Derived conditions

    def _land_ok(self) -> bool:
        return (self.exp - self.cons) % self.M < self.W

    def _tx_behind(self) -> bool:
        """True if tx is not in [dack, hi] (dack is ahead of tx)."""
        return (self.tx - self.dack) % self.M > (self.hi - self.dack) % self.M

    def _quiescent(self) -> bool:
        return self._phase not in ("read", "write") and self.cons == self.wr

    def _page_busy(self) -> bool:
        return (
            self._tx_frame is not None
            and self._tx_kind == "data"
            and self._tx_index % self.W == self.wr % self.W
        )

    def response_ready(self) -> bool:
        return self.open_pending or (
            (self.wr - self.dack) % self.M < self.W and not self._page_busy()
        )

    def response_max(self) -> int:
        return max(0, self.rsp_limit - 12)

    # ------------------------------------------------------------------------------------
    # Input

    def rx_stream(self, data: bytes, t0: float, dt: float | None = None) -> None:
        """Feed ``data`` as a continuous stream: byte ``i`` arrives at ``t0 + (i + 1) * dt``."""
        dt = self.byte_time if dt is None else dt
        self.rx(data, [t0 + (i + 1) * dt for i in range(len(data))])

    def rx(self, data: bytes, times: Sequence[float]) -> None:
        """Feed wire bytes. ``times[i]`` is the arrival time of ``data[i]`` (non-decreasing)."""
        count = len(data)
        pos = 0
        if self._boot_until > 0:
            while pos < count and times[pos] < self._boot_until:
                pos += 1
            if pos < count:
                self._boot_until = -1.0
        while pos < count:
            zero = data.find(0, pos)
            end = count if zero < 0 else zero
            if end > pos:
                if not self._rx_started:
                    self.advance(times[pos])
                    self._rx_started = True
                    self._rx_land = self._land_ok()
                    self._rx_gen = self.sess_gen
                if len(self._rx_buf) + (end - pos) > self.max_len:
                    self._rx_over = True
                    del self._rx_buf[:]
                elif not self._rx_over:
                    self._rx_buf += data[pos:end]
            if zero < 0:
                break
            self.advance(times[zero])
            if self._rx_started:
                self._decide(times[zero])
            self._rx_buf = bytearray()
            self._rx_started = False
            self._rx_over = False
            pos = zero + 1

    # ------------------------------------------------------------------------------------
    # RX decide (doc/protocol.md)

    def _decide(self, now: float) -> None:
        self._count("rx_frames")
        s = self.sid if self.bound else 0
        frame = None
        if not self._rx_over:
            frame = check_host(bytes(self._rx_buf), s, self.W, self.max_len)
        if frame is not None and self._rx_gen != self.sess_gen:
            # doc/protocol.md: a session reset during the frame invalidates it.
            self._count("rx_invalidated_by_reset")
            frame = None
        if frame is None:
            self._count("rx_invalid")
            if not self.bound:
                self.owe = True
                self._count("unbound_replies_owed")
        elif frame["open"]:
            self._count("rx_open")
            self.pend_sid = frame["session_id"]
            self.pend_limit = frame["rsp_limit"]
            self.grant = frame["grant"]
            self.open_pending = True
        elif not self.bound:
            self._count("rx_valid_unbound")
            self.owe = True
        elif self.open_pending:
            self._count("rx_ignored_open_pending")
        elif not (frame["ack"] - self.dack) % self.M <= (self.hi - self.dack) % self.M:
            self._count("rx_ignored_bad_ack")
        else:
            self.dack = frame["ack"]
            self.grant = frame["grant"]
            self.rw_host = frame["rewind"]
            if frame["data"]:
                if frame["seq"] == self.exp and self._rx_land:
                    self.req_pages[self.exp % self.W] = frame["payload"]
                    self.exp = (self.exp + 1) % self.M
                    self._count("req_committed")
                else:
                    self.owe = True
                    if frame["seq"] == self.exp:
                        self._count("req_dropped_page_busy")
                    else:
                        self._count("req_dropped_dup_or_gap")
            if frame["poll"] or frame["resync"]:
                self.owe = True
            if frame["resync"]:
                self.permit = True
                self._count("rx_resync")
        self._settle(now)

    # ------------------------------------------------------------------------------------
    # Time

    def next_event_time(self) -> float | None:
        times = [t for t in (self._core_t, self._tx_end) if t is not None]
        return min(times) if times else None

    def advance(self, now: float) -> None:
        """Run internal events up to ``now``."""
        while True:
            core = self._core_t
            tx = self._tx_end
            core_due = core is not None and core <= now
            tx_due = tx is not None and tx <= now
            if tx is not None and tx_due and (not core_due or core is None or tx <= core):
                self._t = max(self._t, tx)
                self._tx_finish(tx)
            elif core is not None and core_due:
                self._t = max(self._t, core)
                self._core_event(core)
            else:
                break
        self._t = max(self._t, now)

    def pop_emissions(self) -> list[Emission]:
        out, self.emissions = self.emissions, []
        return out

    # ------------------------------------------------------------------------------------
    # Core (in-order endpoint)

    def _delay(self, payload: bytes) -> float:
        d = self.core_delay
        return float(d(payload)) if callable(d) else float(d)

    def _core_event(self, t: float) -> None:
        if self._phase == "read":
            self.cons = (self.cons + 1) % self.M  # cons advances at the last token
            delay = self._delay(self._cur_req)
            if delay > 0:
                self._phase = "delay"
                self._core_t = t + delay
            else:
                self._phase = "wait"
                self._core_t = None
        elif self._phase == "delay":
            self._phase = "wait"
            self._core_t = None
        elif self._phase == "write":
            entry = self._cur_rsp
            assert entry is not None
            if not self.open_pending:
                self.rsp_pages[self.wr % self.W] = entry
            else:
                self._count("responses_dropped_open")
            self.wr = (self.wr + 1) % self.M
            self._phase = "idle"
            self._core_t = None
            self._cur_rsp = None
        self._settle(t)

    def _core_step(self, now: float) -> bool:
        if self._phase == "idle":
            if (
                self.bound
                and not self.open_pending
                and not self.core_stall
                and (self.exp - self.cons) % self.M != 0
            ):
                self._cur_req = self.req_pages[self.cons % self.W]
                self._phase = "read"
                self._core_t = now + (len(self._cur_req) + 1) * self.token_time
                return True
        elif self._phase == "wait":
            if self.response_ready():
                self._execute(now)
                return True
        return False

    def _execute(self, now: float) -> None:
        payload = self._cur_req
        count = self.exec_count
        self.exec_count += 1
        self.executed.append(payload)
        self.exec_counts.append(count)
        self.exec_boots.append(self.boot)
        self.exec_times.append(now)
        full = count.to_bytes(4, "little") + payload
        limit = self.response_max()
        trunc = len(full) > limit
        self._cur_rsp = _RspEntry(full[:limit], trunc)
        self._phase = "write"
        self._core_t = now + (len(full[:limit]) + 1) * self.token_time

    # ------------------------------------------------------------------------------------
    # TX boundary step (doc/protocol.md)

    def _settle(self, now: float) -> None:
        changed = True
        while changed:
            changed = self._core_step(now)
            changed = self._tx_step(now) or changed

    def _session_reset(self) -> None:
        self.sid = self.pend_sid
        self.rsp_limit = self.pend_limit
        self.bound = True
        self.open_pending = False
        self.exp = self.dack = self.cons = self.wr = self.tx = self.hi = 0
        self.cursor = 0
        self.rw_done = self.rw_host
        self.permit = False
        self.sess_gen = not self.sess_gen
        self.owe = True
        self._count("session_resets")

    def _tx_step(self, now: float) -> bool:
        if self._tx_frame is not None:
            return False
        if self.open_pending:
            if self._quiescent():
                self._session_reset()
                return True
            return False
        if self.rw_host != self.rw_done or self._tx_behind():
            if self.rw_host != self.rw_done:
                self._count("rewinds")
            self.tx = self.dack
            self.rw_done = self.rw_host
            return True
        if self.tx != self.wr:
            entry = self.rsp_pages[self.tx % self.W]
            nxt = (self.cursor + entry.wire_len) & 0xFFFF
            if _signed16(self.grant - nxt) >= 0:
                self._start_data(now, entry, nxt)
                return True
        if self.owe:
            nxt = (self.cursor + STATUS_LEN) & 0xFFFF
            if _signed16(self.grant - nxt) >= -self.R or self.permit:
                if _signed16(self.grant - nxt) < -self.R:
                    self._count("status_by_permit")
                self._start_status(now, nxt)
                self.permit = False
                return True
        return False

    def _frame_sid(self) -> int:
        return self.sid if self.bound else 0

    def _start_data(self, now: float, entry: _RspEntry, nxt: int) -> None:
        ctl = lc.fpga_ctl(data=True, trunc=entry.trunc, log2w=self.log2w)
        decoded = lc.build_fpga_frame(ctl, self.tx, self.exp, nxt, entry.payload, self._frame_sid())
        self._begin(now, "data", decoded, self.tx)
        self.cursor = nxt
        self.owe = False
        self._count("data_frames")

    def _start_status(self, now: float, nxt: int) -> None:
        ctl = lc.fpga_ctl(log2w=self.log2w)
        decoded = lc.build_fpga_frame(
            ctl, self.tx, self.exp, nxt, b"", self._frame_sid(), len_field=self.max_len
        )
        self._begin(now, "status", decoded, self.tx)
        self.cursor = nxt
        self.owe = False
        self._count("status_frames" if self.bound else "unbound_status_frames")

    def _begin(self, now: float, kind: str, decoded: bytes, index: int) -> None:
        wire = lc.encode_fpga_wire(decoded)
        emission = Emission(now, wire, kind, self.byte_time, self.boot)
        self.emissions.append(emission)
        self._tx_frame = emission
        self._tx_kind = kind
        self._tx_index = index
        self._tx_end = now + len(wire) * self.byte_time

    def _tx_finish(self, t: float) -> None:
        if self._tx_kind == "data":
            if self.tx == self.hi:
                self.hi = (self.hi + 1) % self.M
            self.tx = (self.tx + 1) % self.M
        self._tx_frame = None
        self._tx_end = None
        self._tx_kind = ""
        self._settle(t)


class ModelPeer:
    """Adapter for ``leanlink.backends.LoopbackBackend``: a byte pipe to an ``FpgaModel``.

    The host writes at time ``now``. The bytes arrive at the model one byte time apart, after
    any bytes that are still on the wire. ``tx(now)`` returns the bytes that the FPGA has
    completely sent by ``now`` and that earlier calls did not return.
    """

    def __init__(self, model: FpgaModel) -> None:
        self.model = model
        self._h2f_free = 0.0
        # (completion time, byte, emission), in time order. A reboot sets the `cut` time of the
        # emission in progress, so the bytes that would complete after it are never delivered.
        self._pending: list[tuple[float, int, Emission]] = []

    def rx(self, data: bytes, now: float) -> None:
        if not data:
            return
        start = max(now, self._h2f_free)
        dt = self.model.byte_time
        self.model.rx(data, [start + (i + 1) * dt for i in range(len(data))])
        self._h2f_free = start + len(data) * dt

    def tx(self, now: float) -> bytes:
        self.model.advance(now)
        for em in self.model.pop_emissions():
            self._pending.extend((em.byte_end(i), em.wire[i], em) for i in range(len(em.wire)))
        self._pending.sort(key=lambda item: item[0])
        out = bytearray()
        due = 0
        for t, b, em in self._pending:
            if t > now + _EPS:
                break
            due += 1
            if em.cut is None or t <= em.cut + _EPS:
                out.append(b)
        del self._pending[:due]
        return bytes(out)
