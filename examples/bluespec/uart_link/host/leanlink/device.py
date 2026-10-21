"""Blocking API over a byte transport.

``LinkDevice`` runs a ``HostProtocol`` on a transport (see ``backends.py``). The time source
and the sleep function are parameters, so a test can drive a manual clock.

Typical use::

    dev = LinkDevice(SerialBackend("/dev/ttyUSB1"), HostConfig(bridge_budget=4096))
    dev.open()
    reply = dev.transact(b"hello")

``transact`` returns the response bytes. It raises ``ResponseTruncated`` if the FPGA flagged
TRUNC, and ``UnknownOutcome`` if the session ended before the response arrived. After
``UnknownOutcome`` the request may or may not have executed. The library never resubmits it.
``open()`` starts a fresh session. Tickets of a lost session keep their results.
"""

from __future__ import annotations

import random
import time
from typing import Callable

from .backends import Transport
from .protocol import (
    HostConfig,
    HostProtocol,
    LinkError,
    Result,
    SessionClosed,
    State,
    Status,
)

__all__ = [
    "LinkDevice",
    "OpenFailed",
    "ResponseTruncated",
    "UnknownOutcome",
]


class OpenFailed(LinkError):
    """The session did not start."""


class UnknownOutcome(LinkError):
    """The session ended before the response arrived: the request may have executed."""

    def __init__(self, result: Result) -> None:
        super().__init__(
            f"ticket {result.ticket}: unknown outcome"
            f" ({'sent' if result.was_sent else 'never sent'}): {result.reason}"
        )
        self.result = result


class ResponseTruncated(LinkError):
    """The FPGA flagged TRUNC: ``partial`` holds the bytes that it kept."""

    def __init__(self, result: Result) -> None:
        super().__init__(f"ticket {result.ticket}: response truncated to {len(result.data)} bytes")
        self.result = result
        self.partial = result.data


class LinkDevice:
    """One transport, one session at a time."""

    def __init__(
        self,
        transport: Transport,
        config: HostConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        read_size: int = 4096,
    ) -> None:
        self.transport = transport
        self.config = config
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._read_size = read_size
        self.protocol: HostProtocol | None = None
        self._next_ticket = 0
        self._tickets: dict[int, tuple[HostProtocol, int]] = {}

    # ------------------------------------------------------------------------------------
    # Session

    def open(self, timeout: float | None = None) -> None:
        """Purge the transport, send OPEN, and wait for the session to start."""
        if self.protocol is not None and self.protocol.state in (State.OPENING, State.ACTIVE):
            self.abort()
        self.transport.purge()
        proto = HostProtocol(self.config, rng=self._rng)
        self.protocol = proto
        now = self._clock()
        proto.start(now)
        deadline = None if timeout is None else now + timeout
        while proto.state is State.OPENING:
            self.pump(self._slice(proto, deadline))
            if proto.state is not State.OPENING:
                break
            if deadline is not None and self._clock() >= deadline:
                proto.abort(self._clock(), "open timed out")
                raise OpenFailed("open timed out")
        if proto.state is not State.ACTIVE:
            raise OpenFailed(proto.lost_reason)

    @property
    def active(self) -> bool:
        return self.protocol is not None and self.protocol.state is State.ACTIVE

    def abort(self) -> None:
        """End the session now. Every ticket without a result becomes unknown."""
        if self.protocol is not None:
            self.protocol.abort(self._clock())

    def close(self) -> None:
        self.abort()
        self.transport.close()

    # ------------------------------------------------------------------------------------
    # Requests

    def submit(self, payload: bytes) -> int:
        """Queue a request and return its ticket. The frame goes out at the next ``pump``."""
        proto = self._require_session()
        local = proto.submit(payload)
        ticket = self._next_ticket
        self._next_ticket += 1
        self._tickets[ticket] = (proto, local)
        return ticket

    def pump(self, wait: float = 0.0) -> None:
        """Read what is available, run the protocol, and write the frames that are due.

        Wait up to ``wait`` seconds for the first byte. A backend that cannot block (the
        loopback) returns at once. Then ``pump`` calls the sleep function for the rest of
        the wait, so a manual clock moves.
        """
        proto = self.protocol
        if proto is None:
            return
        started = self._clock()
        data = self.transport.read(self._read_size, wait)
        if not data and wait > 0:
            elapsed = self._clock() - started
            if elapsed < wait:
                self._sleep(wait - elapsed)
                data = self.transport.read(self._read_size, 0.0)
        now = self._clock()
        if data:
            proto.feed(data, now)
        out = bytearray()
        while True:
            frame = proto.next_frame(now)
            if frame is None:
                break
            out += frame
        if out:
            self.transport.write(bytes(out))

    def take_result(self, ticket: int) -> Result | None:
        """Return and remove the result of ``ticket``, or None if it is not ready."""
        proto, local = self._tickets[ticket]
        result = proto.take_result(local)
        if result is None:
            return None
        del self._tickets[ticket]
        return Result(ticket, result.status, result.data, result.was_sent, result.reason)

    def wait_result(self, ticket: int, timeout: float | None = None) -> Result:
        """Pump until ``ticket`` has a result. Raise TimeoutError after ``timeout`` seconds."""
        proto, _local = self._tickets[ticket]
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            self.pump(self._slice(proto, deadline))
            result = self.take_result(ticket)
            if result is not None:
                return result
            if deadline is not None and self._clock() >= deadline:
                raise TimeoutError(f"no result for ticket {ticket}")

    def transact(self, payload: bytes, timeout: float | None = None) -> bytes:
        """Send one request and return its response.

        After ``TimeoutError`` the request is still in flight and can run on the FPGA, but the
        device keeps no result for it: use ``submit`` and ``wait_result`` to keep the ticket.
        """
        ticket = self.submit(payload)
        try:
            result = self.wait_result(ticket, timeout)
        except TimeoutError:
            proto, local = self._tickets.pop(ticket)
            proto.abandon(local)
            raise
        if result.status is Status.OK:
            return result.data
        if result.status is Status.TRUNCATED:
            raise ResponseTruncated(result)
        raise UnknownOutcome(result)

    # ------------------------------------------------------------------------------------

    def _require_session(self) -> HostProtocol:
        if self.protocol is None:
            raise SessionClosed("open() has not been called")
        return self.protocol

    def _slice(self, proto: HostProtocol, deadline: float | None) -> float:
        """How long to wait before the next pump."""
        now = self._clock()
        limit = 0.001
        due = proto.next_deadline(now)
        if due is not None:
            limit = min(limit, max(0.0, due - now))
        if deadline is not None:
            limit = min(limit, max(0.0, deadline - now))
        return limit
