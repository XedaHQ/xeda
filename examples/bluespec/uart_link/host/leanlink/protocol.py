"""Host protocol state machine for the lean UART transport (doc/protocol.md).

``HostProtocol`` does no I/O and reads no clock. The caller passes the time ``now`` (seconds)
to every call that can depend on time. A caller moves bytes like this:

* ``start(now)`` begins the session. The caller purges its transport first.
* ``feed(data, now)`` gives the bytes that came from the FPGA.
* ``next_frame(now)`` returns the next wire frame to send (COBS bytes and the delimiter), or
  None. Call it until it returns None. Each frame is built when you call this method, so its
  ``ack`` and ``grant`` fields are current.
* ``next_deadline(now)`` returns the time of the next timer, or None.
* ``submit(payload)`` queues a request and returns a ticket. ``take_result(ticket)`` returns
  the result when it is ready.

Session life: IDLE, OPENING, ACTIVE, LOST. LOST is terminal. A protocol object is never
reused. After the session is lost, every ticket that has no result gets the result
UNKNOWN_OUTCOME, and ``submit`` raises ``SessionClosed``.

Counters (``ack``, ``exp``, ``seq`` fields) are absolute Python integers inside this class. The
wire carries them modulo 2W. W is learned from the first status frame that is valid under the
session ID.

RESYNC: host ctl bit 0. If no FPGA byte arrives for ``resync_quiet`` seconds
while the host waits for a reply, the host sends a header-only frame with RESYNC. The FPGA
then sends one status frame even without credit. That frame re-anchors ``consumed``. The host
keeps ``RESYNC_MARGIN`` (12) bytes of B free for it. After ``resync_retries`` tries without any
valid FPGA frame the session is lost.

Rules that doc/protocol.md does not state, and that this class adds, are marked "Host rule" below.
"""

from __future__ import annotations

import enum
import random
from collections import deque
from dataclasses import dataclass
from typing import Deque

from . import codec as lc

U16 = 0xFFFF
HOST_RESYNC = 0x01  # ctl bit 0
RESERVE = 64  # R: the status reserve of the FPGA, a constant of the protocol
RESYNC_MARGIN = 12  # bytes of B kept free for the status frame that RESYNC permits
OPEN_GRANT = 0  # the grant edge of every OPEN frame (doc/protocol.md)


class LinkError(Exception):
    """Base class of the errors of this package."""


class SessionClosed(LinkError):
    """The session is lost or aborted. The object cannot be used any more."""


class State(enum.Enum):
    IDLE = "idle"
    OPENING = "opening"
    ACTIVE = "active"
    LOST = "lost"


class Status(enum.Enum):
    OK = "ok"
    TRUNCATED = "truncated"  # the FPGA flagged TRUNC: an error result, the data is a prefix
    UNKNOWN_OUTCOME = "unknown_outcome"  # the session ended before the response arrived


@dataclass(frozen=True)
class Result:
    ticket: int
    status: Status
    data: bytes = b""
    was_sent: bool = True  # False: the request never left the host, so it did not execute
    reason: str = ""


@dataclass
class HostConfig:
    """Explicit parameters of the host protocol.

    ``bridge_budget`` (B) is the number of FPGA-to-host bytes that the transport can hold
    between the FPGA and the host program. The status reserve R (``RESERVE``, 64) and the RESYNC
    margin (``RESYNC_MARGIN``, 12) are constants of the protocol. The timeouts are seconds.
    ``byte_time`` is the time of one wire byte on the host-to-FPGA line. If it is not 0, the
    timers start when the last byte of a frame leaves the line, not when the frame is handed to
    the transport.
    """

    bridge_budget: int
    rsp_limit: int = 255  # wire length limit of a response frame (12 <= rspLimit <= maxLen)
    max_payload: int = 243  # largest request payload that the caller will submit
    byte_time: float = 0.0
    open_timeout: float = 0.05
    open_retries: int = 20
    rto: float = 0.02  # request retransmission timer
    rewind_timeout: float = 0.05  # accepted request without a response
    poll_interval: float = 0.05  # idle timer
    max_retries: int = 12  # consecutive timer expiries without progress
    backoff: float = 2.0
    max_backoff: float = 8.0
    grant_step: int | None = None  # bytes of new credit that justify a header-only frame
    resync_quiet: float = 0.2  # no FPGA byte for this long while waiting: send RESYNC
    resync_retries: int = 8  # RESYNC frames without any valid FPGA frame before the session ends

    def __post_init__(self) -> None:
        if not 12 <= self.rsp_limit <= lc.MAX_LEN:
            raise ValueError(f"rsp_limit must be in 12..{lc.MAX_LEN}")
        if not 0 <= self.max_payload <= lc.MAX_PAYLOAD:
            raise ValueError(f"max_payload must be in 0..{lc.MAX_PAYLOAD}")
        # The credit compare is a signed 16-bit one (doc/protocol.md): B + R < 32768.
        if self.bridge_budget + RESERVE >= 0x8000:
            raise ValueError(f"bridge_budget + {RESERVE} must stay below 32768")
        # Every allowed response must fit the data window. The grant leaves R bytes for the
        # status reserve and RESYNC_MARGIN bytes for the status frame that RESYNC permits.
        window = self.bridge_budget - RESERVE - RESYNC_MARGIN
        if self.rsp_limit > window:
            raise ValueError(
                f"rsp_limit {self.rsp_limit} exceeds the data window "
                f"B - {RESERVE} - {RESYNC_MARGIN} = {window}: a full response would "
                "never get credit"
            )
        if self.grant_step is None:
            self.grant_step = self.rsp_limit


@dataclass
class _Req:
    ticket: int
    payload: bytes
    sent_end: float = 0.0  # time at which the last copy left the line


class HostProtocol:
    """Host side of one session."""

    def __init__(
        self,
        config: HostConfig,
        *,
        sid: int | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = config
        self._rng = rng if rng is not None else random.SystemRandom()
        if sid is None:
            # doc/protocol.md: all four bytes nonzero, so that one corrupted trailer byte cannot
            # make a frame of this session valid under S = 0 (a false UNBOUND).
            sid = int.from_bytes(bytes(self._rng.randrange(1, 256) for _ in range(4)), "little")
        if not 0 <= sid < (1 << 32) or 0 in sid.to_bytes(4, "little"):
            raise ValueError("session ID must be a 32-bit value with four nonzero bytes")
        self.sid = sid
        self.rsp_limit = config.rsp_limit
        self.state = State.IDLE
        self.lost_reason = ""
        self.W = 0
        self.M = 0
        self.max_len = 0
        self.stats: dict[str, int] = {}
        # Tickets and requests.
        self._next_ticket = 0
        self._queue: Deque[tuple[int, bytes]] = deque()
        self._reqs: list[_Req] = []
        self._results: dict[int, Result] = {}
        self._abandoned: set[int] = set()
        # Counters (absolute).
        self._snd = 0  # next request to put on the wire (rewound by go-back-N)
        self._exp = 0  # requests that the FPGA reported as accepted
        self._rsp = 0  # responses delivered in order
        self._consumed = 0  # cursor of the last valid FPGA frame (u16)
        self._last_grant = 0
        self._epoch = 0
        self._need_poll = False
        self._need_resync = False
        # Timers.
        self._wire_free = 0.0
        self._open_due = 0.0
        self._open_tries = 0
        self._last_rx = 0.0
        self._last_poll = 0.0
        self._req_progress = 0.0
        self._req_tries = 0
        self._req_backoff = 1.0
        self._rsp_clock = 0.0
        self._rsp_tries = 0
        self._rsp_backoff = 1.0
        self._sil_tries = 0
        self._sil_backoff = 1.0
        self._last_byte = 0.0  # last time an FPGA byte arrived (valid or not)
        self._last_resync = 0.0
        self._resync_tries = 0
        self._resync_backoff = 1.0
        # Receive buffer.
        self._rxbuf = bytearray()
        self._rx_overflow = False

    # ------------------------------------------------------------------------------------
    # Introspection

    def _count(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n

    @property
    def outstanding(self) -> int:
        """Requests that were sent at least once and have no response yet."""
        return len(self._reqs) - self._rsp

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def terminal(self) -> bool:
        return self.state is State.LOST

    def _grant(self) -> int:
        cfg = self.cfg
        return (self._consumed + cfg.bridge_budget - RESERVE - RESYNC_MARGIN) & U16

    # ------------------------------------------------------------------------------------
    # Tickets

    def submit(self, payload: bytes) -> int:
        """Queue a request and return its ticket."""
        if self.state is State.LOST:
            raise SessionClosed(self.lost_reason)
        payload = bytes(payload)
        if len(payload) > self.cfg.max_payload:
            raise ValueError(
                f"payload of {len(payload)} bytes exceeds max_payload {self.cfg.max_payload}"
            )
        ticket = self._next_ticket
        self._next_ticket += 1
        self._queue.append((ticket, payload))
        return ticket

    def take_result(self, ticket: int) -> Result | None:
        """Return and remove the result of ``ticket``, or None if it is not ready."""
        return self._results.pop(ticket, None)

    def peek_result(self, ticket: int) -> Result | None:
        return self._results.get(ticket)

    def abandon(self, ticket: int) -> None:
        """Give up the result of ``ticket``. The request stays in flight, and it can still run on
        the FPGA, but the protocol keeps no result for it."""
        if self._results.pop(ticket, None) is None:
            self._abandoned.add(ticket)

    def _finish(self, ticket: int, result: Result) -> None:
        if ticket in self._abandoned:
            self._abandoned.discard(ticket)
        else:
            self._results[ticket] = result

    # ------------------------------------------------------------------------------------
    # Session start and end

    def start(self, now: float) -> None:
        """Begin the session: the first OPEN frame is due now."""
        if self.state is not State.IDLE:
            raise LinkError("start() works only once")
        self.state = State.OPENING
        self._open_due = now
        self._last_rx = now
        self._last_byte = now

    def abort(self, now: float, reason: str = "aborted by the caller") -> None:
        """End the session by the caller's choice. Tickets without a result become unknown."""
        if self.state is not State.LOST:
            self._lose(now, reason)

    def _lose(self, now: float, reason: str) -> None:
        if self.state is State.LOST:
            return
        self.state = State.LOST
        self.lost_reason = reason
        self._count("lost")
        for index in range(self._rsp, len(self._reqs)):
            req = self._reqs[index]
            self._finish(
                req.ticket,
                Result(req.ticket, Status.UNKNOWN_OUTCOME, was_sent=True, reason=reason),
            )
        for ticket, _payload in self._queue:
            self._finish(
                ticket,
                Result(ticket, Status.UNKNOWN_OUTCOME, was_sent=False, reason=reason),
            )
        self._queue.clear()
        self._rxbuf.clear()

    # ------------------------------------------------------------------------------------
    # Input

    def feed(self, data: bytes, now: float) -> None:
        """Process bytes that came from the FPGA."""
        if self.state not in (State.OPENING, State.ACTIVE):
            return
        if data:
            self._last_byte = now
        buf = self._rxbuf
        pos = 0
        while pos < len(data):
            zero = data.find(0, pos)
            end = len(data) if zero < 0 else zero
            if end > pos and not self._rx_overflow:
                buf += data[pos:end]
                if len(buf) > lc.MAX_LEN:
                    self._rx_overflow = True
                    buf.clear()
            if zero < 0:
                break
            if buf and not self._rx_overflow:
                wire = bytes(buf)
                buf.clear()
                self._count("rx_frames")
                self._handle(wire, now)
                if self.state not in (State.OPENING, State.ACTIVE):
                    return
            else:
                if self._rx_overflow:
                    self._count("rx_overflow")
                buf.clear()
            self._rx_overflow = False
            pos = zero + 1

    def _parse(self, wire: bytes, sid: int, w: int | None, max_len: int | None) -> dict | None:
        """Parse an FPGA frame by the rules of doc/protocol.md.

        The codec checks everything except two rules: a status frame has TRUNC = 0, and a DATA
        frame has len <= rspLimit. This method adds them.
        """
        frame = lc.check_fpga_frame(wire, sid, w, max_len).frame
        if frame is None:
            return None
        if frame["kind"] == "status":
            if frame["trunc"]:
                return None
        elif frame["len"] > self.rsp_limit:
            return None
        return frame

    def _handle(self, wire: bytes, now: float) -> None:
        if self.state is State.OPENING:
            frame = self._parse(wire, self.sid, None, None)
            if frame is not None:
                if frame["kind"] == "status":
                    self._start_session(frame, now)
                return
            frame = self._parse(wire, 0, None, None)
            if frame is not None and frame["kind"] == "status":
                self._count("unbound_hint")
                self._learn_from_unbound(frame, now)
            else:
                self._count("rx_invalid")
            return
        frame = self._parse(wire, self.sid, self.W, self.max_len)
        if frame is None:
            other = self._parse(wire, 0, self.W, self.max_len)
            if other is not None:
                self._lose(now, "the FPGA is unbound (frame valid under session 0)")
            else:
                self._count("rx_invalid")
            return
        self._on_frame(frame, now)

    def _learn_from_unbound(self, frame: dict, now: float) -> None:
        """Host rule: an unbound FPGA in the OPEN phase tells maxLen. Fix rspLimit and resend."""
        capability = frame["capability"]
        if self.rsp_limit > capability:
            self.rsp_limit = capability
            self._open_due = now  # the old OPEN was invalid: send the corrected one at once

    def _start_session(self, frame: dict, now: float) -> None:
        width = 1 << frame["log2w"]
        if frame["seq"] >= 2 * width or frame["exp"] != 0:
            # The codec skips the W-dependent checks while W is unknown. A status frame of a
            # fresh session has seq = 0 and exp = 0, because the host sent nothing yet.
            self._count("rx_invalid")
            return
        self.W = width
        self.M = 2 * width
        self.max_len = frame["capability"]
        if self.cfg.max_payload > self.max_len - 12:
            self._lose(
                now,
                f"the build accepts {self.max_len - 12} payload bytes, "
                f"max_payload is {self.cfg.max_payload}",
            )
            return
        self.state = State.ACTIVE
        self._consumed = frame["cursor"]
        self._last_grant = OPEN_GRANT
        self._last_rx = now
        self._last_poll = now
        self._last_resync = now
        self._req_progress = now
        self._rsp_clock = now
        self._count("sessions_started")

    def _on_frame(self, frame: dict, now: float) -> None:
        self._count("rx_valid")
        self._last_rx = now
        self._sil_tries = 0
        self._sil_backoff = 1.0
        self._resync_tries = 0
        self._resync_backoff = 1.0
        # Credit: the cursor is cumulative, so it also covers frames that were lost.
        if (frame["cursor"] - self._consumed) & U16 < 0x8000:
            self._consumed = frame["cursor"]
        # Accepted requests (exp). A value that is behind or beyond what we sent is stale.
        sent = len(self._reqs)
        step = (frame["exp"] - self._exp) % self.M
        if 0 < step <= sent - self._exp:
            if self._rsp >= self._exp:
                self._rsp_clock = now  # a request just became accepted with nothing before it
            self._exp += step
            self._req_tries = 0
            self._req_backoff = 1.0
            self._req_progress = now
            if self._snd < self._exp:
                self._snd = self._exp
        elif step > sent - self._exp:
            self._count("exp_stale")
        # Response.
        if frame["kind"] == "data":
            if (frame["seq"] - self._rsp) % self.M == 0 and self._rsp < self._exp:
                req = self._reqs[self._rsp]
                status = Status.TRUNCATED if frame["trunc"] else Status.OK
                self._finish(req.ticket, Result(req.ticket, status, frame["payload"]))
                self._rsp += 1
                self._rsp_tries = 0
                self._rsp_backoff = 1.0
                self._rsp_clock = now
                self._count("responses")
                if frame["trunc"]:
                    self._count("responses_truncated")
            else:
                self._count("response_ignored")

    # ------------------------------------------------------------------------------------
    # Timers

    def _req_deadline(self) -> float | None:
        if self._exp >= len(self._reqs):
            return None
        base = max(self._reqs[self._exp].sent_end, self._req_progress)
        return base + self.cfg.rto * self._req_backoff

    def _rsp_deadline(self) -> float | None:
        if self._rsp >= self._exp:
            return None
        return self._rsp_clock + self.cfg.rewind_timeout * self._rsp_backoff

    def _sil_deadline(self) -> float | None:
        if self._rsp >= len(self._reqs):
            return None
        base = max(self._last_rx, self._last_poll)
        return base + self.cfg.poll_interval * self._sil_backoff

    def _resync_deadline(self) -> float | None:
        if self._rsp >= len(self._reqs):
            return None  # nothing is awaited
        base = max(self._last_byte, self._last_resync)
        return base + self.cfg.resync_quiet * self._resync_backoff

    def _bump(self, value: float) -> float:
        return min(value * self.cfg.backoff, self.cfg.max_backoff)

    def _check_timers(self, now: float) -> None:
        deadline = self._req_deadline()
        if deadline is not None and now >= deadline:
            # Host rule: go-back-N from exp, once per timer expiry, never at once on a status.
            self._req_tries += 1
            if self._req_tries > self.cfg.max_retries:
                self._lose(now, "request retries exhausted")
                return
            self._snd = self._exp
            self._req_backoff = self._bump(self._req_backoff)
            self._req_progress = now
            self._count("go_back_n")
        deadline = self._rsp_deadline()
        if deadline is not None and now >= deadline:
            self._rsp_tries += 1
            if self._rsp_tries > self.cfg.max_retries:
                self._lose(now, "response retries exhausted")
                return
            self._epoch = (self._epoch + 1) & 3
            self._need_poll = True
            self._rsp_backoff = self._bump(self._rsp_backoff)
            self._rsp_clock = now
            self._count("rewinds")
        deadline = self._sil_deadline()
        if deadline is not None and now >= deadline:
            self._sil_tries += 1
            if self._sil_tries > self.cfg.max_retries:
                self._lose(now, "the FPGA is silent")
                return
            self._need_poll = True
            self._sil_backoff = self._bump(self._sil_backoff)
            self._last_poll = now
            self._count("idle_polls")
        deadline = self._resync_deadline()
        if deadline is not None and now >= deadline:
            self._resync_tries += 1
            if self._resync_tries > self.cfg.resync_retries:
                self._lose(now, "RESYNC retries exhausted: no FPGA byte arrived")
                return
            self._need_resync = True
            self._resync_backoff = self._bump(self._resync_backoff)
            self._last_resync = now
            self._count("resync_timers")

    def next_deadline(self, now: float) -> float | None:
        """Time of the next timer, or ``now`` if a frame can go out now. None if idle."""
        if self.state is State.OPENING:
            return self._open_due
        if self.state is not State.ACTIVE:
            return None
        if self._can_send_request() or self._need_poll or self._need_resync or self._update_due():
            return now
        times = [
            t
            for t in (
                self._req_deadline(),
                self._rsp_deadline(),
                self._sil_deadline(),
                self._resync_deadline(),
            )
            if t is not None
        ]
        return min(times) if times else None

    # ------------------------------------------------------------------------------------
    # Output

    def _put_on_wire(self, now: float, decoded: bytes) -> tuple[bytes, float]:
        wire = lc.encode_host_wire(decoded)
        start = max(now, self._wire_free)
        end = start + len(wire) * self.cfg.byte_time
        self._wire_free = end
        self._count("frames_sent")
        return wire, end

    def _open_frame(self, now: float) -> bytes | None:
        if now < self._open_due:
            return None
        if self._open_tries >= self.cfg.open_retries:
            self._lose(now, "OPEN was not answered")
            return None
        self._open_tries += 1
        # Host rule: the OPEN grant edge is 0 (cursor restarts at 0 in the new session). Only
        # the OPEN reply (a status frame, which may use the reserve) can use it. Data credit
        # starts with the first frame that follows the reply.
        decoded = lc.build_open_frame(self.sid, self.rsp_limit, grant=OPEN_GRANT)
        wire, end = self._put_on_wire(now, decoded)
        self._open_due = end + self.cfg.open_timeout
        self._count("open_sent")
        return wire

    def _can_send_request(self) -> bool:
        if self._snd < len(self._reqs):
            return True  # retransmission: the window rule held at the first send
        if not self._queue:
            return False
        return self._snd - self._rsp < self.W

    def _update_due(self) -> bool:
        """Host rule: send a header-only frame when new credit is worth sending.

        The host knows the last grant it sent and the cursor of the last frame it read. The
        difference is the credit that the FPGA may still have. If it is below one full response
        frame, the FPGA may be blocked, so any new credit is worth a frame. Otherwise the host
        waits until ``grant_step`` bytes have piled up. The host sends such a frame only if it
        waits for responses.
        """
        if self._rsp >= len(self._reqs):
            return False
        step = (self._grant() - self._last_grant) & U16
        if not 0 < step < 0x8000:
            return False
        left = (self._last_grant - self._consumed) & U16
        if left >= 0x8000:
            left = 0
        grant_step = self.cfg.grant_step if self.cfg.grant_step is not None else self.rsp_limit
        return left < self.rsp_limit or step >= grant_step

    def next_frame(self, now: float) -> bytes | None:
        """Return the next wire frame to send, or None."""
        if self.state is State.OPENING:
            return self._open_frame(now)
        if self.state is not State.ACTIVE:
            return None
        self._check_timers(now)
        if self.state is not State.ACTIVE:
            return None
        if self._need_resync:
            return self._send_header(now)  # RESYNC goes in a header-only frame
        if self._can_send_request():
            return self._send_request(now)
        if self._need_poll or self._update_due():
            return self._send_header(now)
        return None

    def _frame_fields(self) -> tuple[int, int]:
        return self._rsp % self.M, self._grant()

    def _sent(self, now: float, decoded: bytes, grant: int) -> tuple[bytes, float]:
        wire, end = self._put_on_wire(now, decoded)
        self._last_grant = grant
        self._need_poll = False
        return wire, end

    def _send_request(self, now: float) -> bytes:
        if self._snd == len(self._reqs):
            ticket, payload = self._queue.popleft()
            self._reqs.append(_Req(ticket, payload))
        else:
            self._count("retransmissions")
        index = self._snd
        req = self._reqs[index]
        ack, grant = self._frame_fields()
        ctl = lc.host_ctl(data=True, poll=self._need_poll, rewind=self._epoch)
        decoded = lc.build_host_frame(ctl, index % self.M, ack, grant, req.payload, self.sid)
        wire, end = self._sent(now, decoded, grant)
        req.sent_end = end
        self._snd += 1
        self._count("requests_sent")
        return wire

    def _send_header(self, now: float) -> bytes:
        ack, grant = self._frame_fields()
        poll = self._need_poll
        resync = self._need_resync
        ctl = lc.host_ctl(poll=poll, rewind=self._epoch) | (HOST_RESYNC if resync else 0)
        decoded = lc.build_host_frame(ctl, 0, ack, grant, b"", self.sid)
        wire, _end = self._sent(now, decoded, grant)
        self._need_resync = False
        self._count("header_frames")
        if poll:
            self._count("polls")
        if resync:
            self._count("resyncs")
        return wire
