"""Pure host protocol state for sessions, retries, acknowledgments, and byte credit."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .codec import Frame, RESERVE

MASK8 = 0xFF
MASK16 = 0xFFFF


class LinkError(Exception):
    """Base class for terminal link errors."""


class RetryExhausted(LinkError):
    pass


class CreditExhausted(LinkError):
    pass


class LinkReset(LinkError):
    pass


class UnknownOutcome(LinkReset):
    pass


class CloseUnconfirmed(LinkReset):
    """CLOSE may have reached the peer, but no valid confirmation arrived."""


class ProtocolError(LinkError):
    pass


def _distance8(a: int, b: int) -> int:
    return (a - b) & MASK8


def _distance16(a: int, b: int) -> int:
    return (a - b) & MASK16


def wire_bound(decoded_size: int) -> int:
    if decoded_size <= 0:
        return 0
    return decoded_size + decoded_size // 254 + 2


def fit_frame(byte_budget: int, compiled_limit: int = 240) -> int:
    usable = byte_budget - RESERVE
    size = 0
    for n in range(1, compiled_limit + 1):
        if wire_bound(n) > usable:
            break
        size = n
    return size


@dataclass
class Pending:
    seq: int
    payload: bytes
    sent_at: float
    attempts: int = 1
    accepted: bool = False
    response: bytes | None = None
    retransmitted: bool = False


@dataclass
class ControlPending:
    frame: Frame
    sent_at: float = float("inf")
    attempts: int = 1


class ProtocolState:
    """Protocol state with no serial dependency.

    A reset ends delivery guarantees for outstanding operations. The caller must report their
    outcomes as unknown and must not submit them automatically in a new session.
    """

    def __init__(
        self,
        *,
        byte_budget: int = 256,
        window: int = 8,
        max_frame: int = 240,
        session: int | None = None,
        retry_limit: int = 8,
        timeout: float = 0.25,
        min_timeout: float = 0.02,
        wire_time_per_byte: float = 0.0,
        clock=time.monotonic,
    ) -> None:
        if not 80 <= byte_budget < 32768:
            raise ValueError("byte_budget must be in 80..32767")
        if not 1 <= window <= 64:
            raise ValueError("window must be in 1..64")
        if not 32 <= max_frame <= 240:
            raise ValueError("max_frame must be in 32..240")
        if retry_limit < 1 or timeout <= 0 or min_timeout <= 0 or wire_time_per_byte < 0:
            raise ValueError("retry limits and timeouts must be positive")
        self.byte_budget = byte_budget
        self.window = window
        self.requested_frame = max_frame
        self.max_frame = max_frame
        self.session = session if session is not None else secrets.randbits(32) or 1
        if self.session == 0:
            raise ValueError("session zero is reserved")
        self.retry_limit = retry_limit
        self.initial_timeout = timeout
        self.min_timeout = min_timeout
        self.timeout = max(timeout, min_timeout)
        self.wire_time_per_byte = wire_time_per_byte
        self.srtt: float | None = None
        self.rttvar: float | None = None
        self.clock = clock
        self.active = False
        self.tx_next = 0
        self.accepted_next = 0
        self.delivery_ack = 0
        self.grant_edge = byte_budget
        self.cursor_anchor: int | None = None
        self.pending: dict[int, Pending] = {}
        self.control_token = 0
        self.open_payload = b""
        self.open_attempts = 0
        self.open_limit = min(8, byte_budget // 19)
        self.control_waiting: dict[int, str] = {}
        self.controls: dict[int, ControlPending] = {}
        self.recovery_seq: int | None = None
        self.ready_frames: list[Frame] = []
        self._open_frame: Frame | None = None
        self._reset = False
        self.completed: dict[int, bytes] = {}

    def open_frame(self) -> Frame:
        if self._reset:
            raise LinkReset("protocol state was reset; create a new state before reopening")
        if self.active:
            raise ProtocolError("session is already open")
        if fit_frame(self.byte_budget) < 32:
            raise ProtocolError("byte budget cannot support a 32-byte frame")
        if self._open_frame is None:
            self.open_payload = min(self.requested_frame, 240).to_bytes(2, "little") + bytes(
                (min(self.window, 8), 0)
            )
            self._open_frame = self._control(2, self.open_payload, self.grant_edge)
        self.open_attempts += 1
        if self.open_attempts > self.open_limit:
            raise RetryExhausted("OPEN reply budget exhausted; reset and drain the backend")
        return self._open_frame

    def _control(self, selector: int, payload: bytes, grant: int | None = None) -> Frame:
        token = self.control_token
        self.control_token = (self.control_token + 1) & MASK8
        self.control_waiting[token] = ("poll", "capabilities", "open", "close")[selector]
        frame = Frame(
            "control",
            selector,
            token,
            self.delivery_ack,
            self.grant_edge if grant is None else grant,
            self.session,
            payload,
        )
        self.controls[token] = ControlPending(frame)
        return frame

    def poll_frame(self) -> Frame:
        if not self.active:
            raise ProtocolError("POLL requires an active session")
        return self._control(0, b"")

    def capabilities_frame(self) -> Frame:
        if not self.active:
            raise ProtocolError("CAPABILITIES requires an active session")
        return self._control(1, b"")

    def close_frame(self) -> Frame:
        if not self.active:
            raise ProtocolError("CLOSE requires an active session")
        return self._control(3, b"")

    def accept_control_response(self, frame: Frame, encoded_length: int) -> str | None:
        if frame.kind != "control_response" or frame.session != self.session:
            return None
        purpose = self.control_waiting.get(frame.seq)
        if purpose is None:
            return None
        selected_frame = self.max_frame
        selected_window = self.window
        if purpose == "open":
            if frame.flags != 2 or len(frame.payload) != 4:
                raise ProtocolError("invalid OPEN response")
            if frame.payload[3] != 0:
                raise ProtocolError("OPEN response reserved byte must be zero")
            selected_frame = int.from_bytes(frame.payload[:2], "little")
            selected_window = frame.payload[2]
            if (
                selected_frame < 32
                or selected_frame > fit_frame(self.byte_budget)
                or selected_frame > int.from_bytes(self.open_payload[:2], "little")
                or not 1 <= selected_window <= min(self.window, 8)
            ):
                raise ProtocolError("OPEN response selected incompatible limits")
        else:
            if (
                frame.flags
                != (0, 1, 2, 3)[("poll", "capabilities", "open", "close").index(purpose)]
            ):
                raise ProtocolError("control response selector mismatch")
            if purpose == "poll" and frame.payload:
                raise ProtocolError("POLL response payload must be empty")
            if purpose == "capabilities":
                if len(frame.payload) != 4:
                    raise ProtocolError("CAPABILITIES response must contain four bytes")
                max_frame = int.from_bytes(frame.payload[:2], "little")
                window = frame.payload[2]
                features = frame.payload[3]
                if not 32 <= max_frame <= 240 or not 1 <= window <= 64 or features & ~1:
                    raise ProtocolError("CAPABILITIES response contains invalid limits or features")
            if purpose == "close" and frame.payload:
                raise ProtocolError("CLOSE response payload must be empty")
        if not self._acceptance_ack_valid(frame.ack):
            return None
        if not self._anchor(frame.byte_cursor, encoded_length):
            return None
        self.control_waiting.pop(frame.seq, None)
        self.controls.pop(frame.seq, None)
        if purpose == "open":
            self.max_frame = selected_frame
            self.window = selected_window
            self.active = True
            self.tx_next = self.accepted_next = self.delivery_ack = 0
        elif purpose == "close":
            self.active = False
        self._advance_acceptance(frame.ack)
        if purpose == "poll" and self.recovery_seq is not None:
            item = self.pending.get(self.recovery_seq)
            if item is not None and item.response is None:
                self.ready_frames.append(self._request(item))
            self.recovery_seq = None
        return purpose

    def submit(self, payload: bytes) -> Frame:
        if not self.active:
            raise ProtocolError("OPEN must complete before application traffic")
        if len(self.pending) >= self.window:
            raise CreditExhausted("request window is full")
        if len(payload) + 13 > self.max_frame:
            raise ProtocolError("request exceeds negotiated frame limit")
        seq = self.tx_next
        self.tx_next = (self.tx_next + 1) & MASK8
        item = Pending(seq, bytes(payload), float("inf"))
        self.pending[seq] = item
        return self._request(item)

    def note_transmitted(self, seq: int, now: float | None = None) -> None:
        item = self.pending.get(seq)
        if item is not None:
            item.sent_at = self.clock() if now is None else now

    def note_control_transmitted(self, token: int, now: float | None = None) -> None:
        item = self.controls.get(token)
        if item is not None:
            item.sent_at = self.clock() if now is None else now

    def _request(self, item: Pending) -> Frame:
        return Frame(
            "request", 0, item.seq, self.delivery_ack, self.grant_edge, self.session, item.payload
        )

    def receive_response(self, frame: Frame, encoded_length: int) -> bytes | None:
        if not self.active or frame.kind != "response" or frame.session != self.session:
            return None
        if len(frame.payload) + 13 > self.max_frame:
            return None
        if frame.flags & 1 and frame.payload:
            return None
        if frame.flags & ~3:
            return None
        if not self._acceptance_ack_valid(frame.ack):
            return None
        if not self._anchor(frame.byte_cursor, encoded_length):
            return None
        self._advance_acceptance(frame.ack)
        item = self.pending.get(frame.seq)
        if item is None:
            return None
        if frame.flags & 1:
            return None
        if item.response is None:
            if not item.retransmitted and item.sent_at != float("inf"):
                self._sample_rtt(max(0.0, self.clock() - item.sent_at), encoded_length)
            item.response = frame.payload
        self._advance_delivery()
        return item.response

    def _sample_rtt(self, sample: float, encoded_length: int) -> None:
        if self.srtt is None or self.rttvar is None:
            self.srtt = sample
            self.rttvar = sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample
        self.timeout = max(
            self.min_timeout,
            self.srtt + 4 * self.rttvar + encoded_length * self.wire_time_per_byte,
        )

    def _advance_acceptance(self, ack: int) -> None:
        forward = _distance8(ack, self.accepted_next)
        if forward == 0:
            return
        live = _distance8(self.tx_next, self.accepted_next)
        if forward >= 128 or forward > live:
            return
        for offset in range(forward):
            seq = (self.accepted_next + offset) & MASK8
            pending = self.pending.get(seq)
            if pending is not None:
                pending.accepted = True
        self.accepted_next = ack

    def _acceptance_ack_valid(self, ack: int) -> bool:
        forward = _distance8(ack, self.accepted_next)
        if forward == 0:
            return True
        if forward < 128:
            return forward <= _distance8(self.tx_next, self.accepted_next)
        return _distance8(self.accepted_next, ack) <= self.window

    def _advance_delivery(self) -> None:
        while True:
            item = self.pending.get(self.delivery_ack)
            if item is None or item.response is None:
                break
            self.completed[self.delivery_ack] = item.response
            del self.pending[self.delivery_ack]
            self.delivery_ack = (self.delivery_ack + 1) & MASK8

    def _anchor(self, cursor: int, encoded_length: int = 0) -> bool:
        if self.cursor_anchor is None:
            start = cursor
            if start >= 0x8000 or start > self.byte_budget:
                return False
            if encoded_length and start + encoded_length > self.byte_budget:
                return False
            end = (cursor + encoded_length) & MASK16
            self.cursor_anchor = end
            self.grant_edge = (end + self.byte_budget) & MASK16
            return True
        advance = _distance16(cursor, self.cursor_anchor)
        grant_span = _distance16(self.grant_edge, self.cursor_anchor)
        if advance >= 0x8000 or advance > grant_span:
            return False
        if encoded_length:
            end = (cursor + encoded_length) & MASK16
            consumed = _distance16(end, self.cursor_anchor)
            if consumed >= 0x8000 or consumed > grant_span:
                return False
            self.cursor_anchor = end
            self.grant_edge = (end + self.byte_budget) & MASK16
        elif advance:
            self.cursor_anchor = cursor
        return True

    def retries_due(self, now: float | None = None) -> list[Frame]:
        if not self.active:
            return []
        if self.recovery_seq is not None:
            return []
        now = self.clock() if now is None else now
        due = [
            item
            for item in self.pending.values()
            if item.response is None and now - item.sent_at >= self.timeout
        ]
        if not due:
            return []
        oldest = min(due, key=lambda item: _distance8(item.seq, self.accepted_next))
        if oldest.attempts >= self.retry_limit:
            raise RetryExhausted(f"request {oldest.seq} exceeded retry limit")
        self.timeout = max(self.min_timeout, min(self.timeout * 2, 8.0))
        if oldest.accepted:
            oldest.attempts += 1
            oldest.retransmitted = True
            self.recovery_seq = oldest.seq
            oldest.sent_at = float("inf")
            return [self.poll_frame()]
        burst = []
        seq = self.accepted_next
        for _ in range(_distance8(self.tx_next, self.accepted_next)):
            item = self.pending.get(seq)
            if item is not None and item.response is None:
                if item.attempts >= self.retry_limit:
                    raise RetryExhausted(f"request {item.seq} exceeded retry limit")
                item.attempts += 1
                item.retransmitted = True
                burst.append(self._request(item))
            seq = (seq + 1) & MASK8
        return burst

    def control_retries_due(self, now: float | None = None) -> list[Frame]:
        now = self.clock() if now is None else now
        due: list[Frame] = []
        limit = min(8, max(1, RESERVE // 15))
        for pending in self.controls.values():
            if now - pending.sent_at < self.timeout:
                continue
            if pending.attempts >= limit:
                raise CreditExhausted("reserved control reply credit is exhausted")
            pending.attempts += 1
            pending.sent_at = float("inf")
            due.append(pending.frame)
        return due

    def take_ready_frames(self) -> list[Frame]:
        frames, self.ready_frames = self.ready_frames, []
        return frames

    def mark_reset(self) -> list[UnknownOutcome]:
        self.active = False
        self._reset = True
        errors = [
            UnknownOutcome(f"request {seq} has unknown outcome after reset") for seq in self.pending
        ]
        self.pending.clear()
        self.controls.clear()
        self.control_waiting.clear()
        self.recovery_seq = None
        self.ready_frames.clear()
        return errors
