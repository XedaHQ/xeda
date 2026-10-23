"""Bounded stream exchanges over one LeanLink session.

An exchange admits a confirmed input prefix and returns available output words. Only the
unaccepted suffix may be offered again. A timeout or a malformed reply requires RESET before
further input, because the application may already have consumed words from that exchange.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable

from .device import LinkDevice
from .protocol import LinkError


class StreamProtocolError(LinkError):
    """The stream state or a reply does not satisfy the exchange protocol."""


class StreamClient:
    """One ordered stream, bound to the session of the last successful RESET.

    Use a separate device for concurrent application streams. Call ``reset`` after opening or
    reopening the device. ``transfer`` uses one deadline for all admission attempts and polls.
    The optional clock makes deadlines testable without a physical board.
    """

    def __init__(
        self,
        device: LinkDevice,
        *,
        input_bytes: int,
        output_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if input_bytes <= 0 or output_bytes <= 0:
            raise ValueError("stream word sizes must be positive")
        self.device = device
        self.input_bytes = input_bytes
        self.output_bytes = output_bytes
        self._clock = clock
        self._session: object | None = None

    def _limits(self) -> tuple[int, int]:
        protocol = self.device.protocol
        if protocol is None or not self.device.active:
            raise StreamProtocolError("open a LeanLink session before using a stream")
        request_limit = min(protocol.max_len - 12, protocol.cfg.max_payload)
        response_limit = min(protocol.max_len, protocol.rsp_limit) - 12
        if request_limit < 1 + self.input_bytes:
            raise StreamProtocolError("the request limit cannot hold one input word")
        if response_limit < 2 + self.output_bytes:
            raise StreamProtocolError(
                "the response limit cannot hold the header and one output word"
            )
        return request_limit, response_limit

    def check_session(self) -> None:
        """Refuse input until RESET has bound this client to the active device session."""
        self._limits()
        if self._session is None or self.device.protocol is not self._session:
            raise StreamProtocolError("RESET is required before using this application stream")

    def invalidate(self) -> None:
        """Require RESET after an uncertain or invalid application operation."""
        self._session = None

    @staticmethod
    def _timeout(timeout: float) -> float:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        return timeout

    def reset(self, timeout: float = 5.0) -> None:
        """Reset the application and both queues, then bind to this device session."""
        self._timeout(timeout)
        self.invalidate()
        self._limits()
        session = self.device.protocol
        reply = self.device.transact(b"\x00", timeout=timeout)
        if self.device.protocol is not session or reply != b"\x00\x00":
            raise StreamProtocolError("invalid RESET reply or changed device session")
        self._session = session

    def _words(self, words: Iterable[bytes]) -> list[bytes]:
        result = []
        for word in words:
            if (
                not isinstance(word, (bytes, bytearray, memoryview))
                or len(word) != self.input_bytes
            ):
                raise ValueError(f"each input word must contain {self.input_bytes} bytes")
            result.append(bytes(word))
        return result

    def exchange(
        self, words: Iterable[bytes] = (), timeout: float = 5.0
    ) -> tuple[int, list[bytes]]:
        """Offer one packet of words and return its accepted count and output words."""
        self._timeout(timeout)
        self.check_session()
        request_limit, response_limit = self._limits()
        offered = self._words(words)
        if len(offered) > (request_limit - 1) // self.input_bytes:
            raise ValueError("the offered words exceed one request packet")
        try:
            reply = self.device.transact(b"\x01" + b"".join(offered), timeout=timeout)
            self.check_session()
            if (
                len(reply) < 2
                or len(reply) > response_limit
                or reply[0] != 0
                or reply[1] > len(offered)
                or (len(reply) - 2) % self.output_bytes
            ):
                raise StreamProtocolError("invalid stream exchange reply")
            output = [
                reply[index : index + self.output_bytes]
                for index in range(2, len(reply), self.output_bytes)
            ]
            return reply[1], output
        except BaseException:
            self.invalidate()
            raise

    def transfer(
        self,
        words: Iterable[bytes],
        *,
        output_words: int,
        timeout: float = 5.0,
        on_output: Callable[[bytes], None] | None = None,
    ) -> list[bytes]:
        """Admit all input and collect exactly ``output_words`` with one overall deadline.

        A full input queue produces a zero or partial accepted count. The next exchange offers
        the same unaccepted suffix while collecting output. After input admission, empty
        exchanges poll for remaining output. ``on_output`` can reject an application error
        immediately instead of waiting for more output that will never arrive.
        """
        self._timeout(timeout)
        self.check_session()
        if output_words < 0:
            raise ValueError("output_words must be nonnegative")
        pending = self._words(words)
        maximum = (self._limits()[0] - 1) // self.input_bytes
        deadline = self._clock() + timeout
        admitted = 0
        output: list[bytes] = []
        try:
            while admitted < len(pending) or len(output) < output_words:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("stream transfer did not finish before its deadline")
                accepted, received = self.exchange(
                    pending[admitted : admitted + maximum], timeout=remaining
                )
                if len(output) + len(received) > output_words:
                    raise StreamProtocolError("the application returned too many output words")
                admitted += accepted
                for word in received:
                    if on_output is not None:
                        on_output(word)
                    output.append(word)
            return output
        except BaseException:
            self.invalidate()
            raise
