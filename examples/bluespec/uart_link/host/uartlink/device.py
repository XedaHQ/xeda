"""Synchronous application client for the generic UART link."""

from __future__ import annotations

import time

from .backends import ByteTransport
from .codec import MAX_WIRE_FRAME, Frame, rcobs_decode
from .link import (
    CloseUnconfirmed,
    CreditExhausted,
    LinkError,
    LinkReset,
    ProtocolError,
    ProtocolState,
    RetryExhausted,
    UnknownOutcome,
)

STAT_NAMES = (
    "transport_rx_bytes",
    "transport_tx_bytes",
    "framing_errors",
    "parity_errors",
    "breaks",
    "overruns",
    "request_frames",
    "cobs_errors",
    "crc_errors",
    "length_errors",
    "header_session_errors",
    "gaps",
    "duplicates",
    "receive_drops",
    "response_transmissions",
    "credit_blocked",
)


class Device:
    def __init__(
        self,
        transport: ByteTransport,
        *,
        byte_budget: int = 256,
        window: int = 8,
        max_frame: int = 240,
        timeout: float = 0.25,
        min_timeout: float = 0.02,
        retry_limit: int = 8,
        wire_time_per_byte: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.transport = transport
        if wire_time_per_byte is None:
            baudrate = getattr(transport, "baudrate", None)
            wire_time_per_byte = 10.0 / baudrate if baudrate else 0.0
        self.state = ProtocolState(
            byte_budget=byte_budget,
            window=window,
            max_frame=max_frame,
            timeout=timeout,
            min_timeout=min_timeout,
            retry_limit=retry_limit,
            wire_time_per_byte=wire_time_per_byte,
            clock=clock,
        )
        self._rx = bytearray()
        self._discard_until_delimiter = False
        self._results: dict[int, bytes] = {}
        self._failures: dict[int, Exception] = {}
        self._next_ticket = 0
        self._ticket_by_seq: dict[int, int] = {}
        self._seq_by_ticket: dict[int, int] = {}
        self._stats: dict[int, int] = {}
        self._failed = False
        self._closed = False

    def _ensure_usable(self) -> None:
        if self._failed:
            raise LinkReset("link failed; reset and drain the backend before reuse")
        if self._closed:
            raise LinkReset("device transport is closed")

    def _terminate_link(self) -> None:
        self._failed = True
        pending_sequences = list(self.state.pending)
        unknown = self.state.mark_reset()
        for seq, failure in zip(pending_sequences, unknown):
            ticket = self._ticket_by_seq.get(seq)
            if ticket is not None:
                self._failures[ticket] = failure

    def _write(self, wire: bytes, *, unknown_outcome: bool = False) -> None:
        self._ensure_usable()
        offset = 0
        while offset < len(wire):
            try:
                accepted = self.transport.write(wire[offset:])
            except Exception as exc:
                self._terminate_link()
                error = (
                    UnknownOutcome("request write failed; outcome is unknown")
                    if unknown_outcome
                    else LinkReset("backend write failed; reset and drain the backend before reuse")
                )
                raise error from exc
            if not isinstance(accepted, int) or accepted <= 0 or accepted > len(wire) - offset:
                self._terminate_link()
                if unknown_outcome:
                    raise UnknownOutcome("request write failed; outcome is unknown")
                raise LinkReset("backend write failed; reset and drain the backend before reuse")
            offset += accepted

    def open(self) -> None:
        self._ensure_usable()
        last_error: Exception | None = None
        while self.state.open_attempts < self.state.open_limit:
            frame = self.state.open_frame()
            self._write(frame.encode())
            self.state.note_control_transmitted(frame.seq)
            try:
                deadline = self.state.clock() + self.state.timeout
                while self.state.clock() < deadline:
                    self._receive()
                    if self.state.active:
                        return
            except (ValueError, LinkError) as exc:
                last_error = exc
        if last_error:
            raise RetryExhausted(f"OPEN failed: {last_error}") from last_error
        raise RetryExhausted("OPEN reply budget exhausted; reset and drain the backend")

    def submit(self, payload: bytes) -> int:
        self._ensure_usable()
        seq = self.state.tx_next
        if seq in self._ticket_by_seq:
            raise CreditExhausted(
                f"result ticket {self._ticket_by_seq[seq]} for sequence {seq} is not consumed"
            )
        frame = self.state.submit(payload)
        ticket = self._next_ticket
        self._next_ticket += 1
        self._ticket_by_seq[frame.seq] = ticket
        self._seq_by_ticket[ticket] = frame.seq
        self._write(frame.encode(), unknown_outcome=True)
        self.state.note_transmitted(frame.seq)
        return ticket

    def take_result(self, ticket: int) -> bytes | None:
        """Return and consume a completed result, or None while it is still pending."""
        if ticket in self._failures:
            failure = self._failures.pop(ticket)
            self._release_ticket(ticket)
            raise failure
        if ticket not in self._seq_by_ticket:
            raise KeyError(f"unknown or consumed result ticket {ticket}")
        if ticket not in self._results:
            return None
        result = self._results.pop(ticket)
        self._release_ticket(ticket)
        return result

    def _release_ticket(self, ticket: int) -> None:
        seq = self._seq_by_ticket.pop(ticket, None)
        if seq is not None:
            self._ticket_by_seq.pop(seq, None)

    def wait_result(self, ticket: int, *, timeout: float = 30.0) -> bytes:
        deadline = self.state.clock() + timeout
        while self.state.clock() < deadline:
            result = self.take_result(ticket)
            if result is not None:
                return result
            self.pump()
        raise RetryExhausted(f"result ticket {ticket} timed out")

    def _receive(self) -> int:
        self._ensure_usable()
        try:
            data = self.transport.read(4096)
        except Exception as exc:
            has_pending = bool(self.state.pending)
            self._terminate_link()
            if has_pending:
                raise UnknownOutcome(
                    "backend read failed; pending request outcomes are unknown"
                ) from exc
            raise LinkReset(
                "backend read failed; reset and drain the backend before reuse"
            ) from exc
        if data:
            if self._discard_until_delimiter:
                boundary = data.find(b"\x00")
                if boundary < 0:
                    return 0
                data = data[boundary + 1 :]
                self._discard_until_delimiter = False
            self._rx.extend(data)
        count = 0
        while 0 in self._rx:
            end = self._rx.index(0)
            packet = bytes(self._rx[:end])
            del self._rx[: end + 1]
            if end + 1 > MAX_WIRE_FRAME:
                continue
            if not packet:
                continue
            try:
                decoded = rcobs_decode(packet)
                frame = Frame.from_decoded(decoded)
            except ValueError:
                continue
            if frame.kind == "control_response":
                purpose = self.state.accept_control_response(frame, len(packet) + 1)
                if purpose == "capabilities" and len(frame.payload) == 4:
                    self._stats[-1] = int.from_bytes(frame.payload[:2], "little")
                    self._stats[-2] = frame.payload[2]
                    self._stats[-3] = frame.payload[3]
            elif frame.kind == "response":
                self.state.receive_response(frame, len(packet) + 1)
                for seq, result in self.state.completed.items():
                    ticket = self._ticket_by_seq.get(seq)
                    if ticket is not None:
                        self._results[ticket] = result
                self.state.completed.clear()
            count += 1
        if len(self._rx) > MAX_WIRE_FRAME:
            self._rx.clear()
            self._discard_until_delimiter = True
        return count

    def pump(self) -> int:
        self._ensure_usable()
        try:
            self._receive()
            for frame in self.state.retries_due():
                self._write(frame.encode(), unknown_outcome=frame.kind == "request")
                if frame.kind == "control":
                    self.state.note_control_transmitted(frame.seq)
                else:
                    self.state.note_transmitted(frame.seq)
            for frame in self.state.take_ready_frames():
                self._write(frame.encode(), unknown_outcome=frame.kind == "request")
                self.state.note_transmitted(frame.seq)
            for frame in self.state.control_retries_due():
                self._write(frame.encode())
                self.state.note_control_transmitted(frame.seq)
            return len(self._results)
        except (RetryExhausted, CreditExhausted):
            self._terminate_link()
            raise

    def transact(self, payload: bytes, *, timeout: float | None = None) -> bytes:
        try:
            ticket = self.submit(payload)
        except KeyboardInterrupt:
            self._terminate_link()
            raise
        try:
            result = self.wait_result(ticket, timeout=timeout if timeout is not None else 30.0)
        except KeyboardInterrupt:
            self._terminate_link()
            raise
        except Exception as exc:
            if isinstance(exc, UnknownOutcome):
                raise
            self._terminate_link()
            try:
                result = self.take_result(ticket)
            except UnknownOutcome as failure:
                raise failure from exc
            except KeyError:
                raise UnknownOutcome(f"request result ticket {ticket} has unknown outcome") from exc
        try:
            self._send_poll()
        except KeyboardInterrupt:
            self._terminate_link()
            raise
        except Exception:
            # The application result is known. A failed housekeeping POLL must not hide it.
            self._terminate_link()
        return result

    def _send_poll(self) -> None:
        self._ensure_usable()
        self._write(self.state.poll_frame().encode())
        token = (self.state.control_token - 1) & 0xFF
        self.state.note_control_transmitted(token)

    def echo(self, data: bytes) -> bytes:
        response = self.transact(b"\x30" + len(data).to_bytes(2, "little") + data)
        if len(response) != len(data) + 1 or response[:1] != b"\xb0":
            raise LinkError("invalid ECHO response")
        return response[1:]

    def statistics(self) -> dict[str, int]:
        self._ensure_usable()
        capabilities = self.state.capabilities_frame()
        self._write(capabilities.encode())
        self.state.note_control_transmitted(capabilities.seq)
        deadline = self.state.clock() + self.state.timeout
        while self.state.clock() < deadline:
            self.pump()
            if -1 in self._stats:
                break
        if -1 not in self._stats:
            raise LinkError("CAPABILITIES request timed out")
        values: dict[str, int] = {}
        for index, name in enumerate(STAT_NAMES):
            ticket = self.submit(bytes((3, index)))
            payload = self.wait_result(ticket, timeout=self.state.timeout * 4)
            if len(payload) == 6 and payload[0] == 0x83 and payload[1] == index:
                values[name] = int.from_bytes(payload[2:], "little")
            else:
                raise LinkError(f"invalid statistics response for {name}")
        return values

    def close(self) -> None:
        if self._closed:
            return
        if self._failed:
            self.transport.close()
            self._closed = True
            return
        if self.state.pending:
            raise ProtocolError("CLOSE requires all application requests to complete")
        if self.state.active:
            frame = self.state.close_frame()
            try:
                self._write(frame.encode())
                self.state.note_control_transmitted(frame.seq)
                deadline = self.state.clock() + self.state.timeout * 4
                confirmed = False
                while self.state.clock() < deadline:
                    try:
                        self._receive()
                    except ProtocolError:
                        # Keep waiting. Invalid replies do not consume the CLOSE token.
                        pass
                    if not self.state.active:
                        confirmed = True
                        break
                    try:
                        retries = self.state.control_retries_due()
                    except CreditExhausted:
                        break
                    for retry in retries:
                        self._write(retry.encode())
                        self.state.note_control_transmitted(retry.seq)
                if not confirmed:
                    self._terminate_link()
                    self.transport.close()
                    self._closed = True
                    raise CloseUnconfirmed(
                        "CLOSE confirmation is unknown; reset and drain the backend before reuse"
                    )
            except LinkReset as exc:
                self._terminate_link()
                if isinstance(exc, CloseUnconfirmed):
                    raise
                self.transport.close()
                self._closed = True
                raise CloseUnconfirmed(
                    "CLOSE confirmation is unknown; reset and drain the backend before reuse"
                ) from exc
        else:
            # Closing a device that was never opened needs no protocol exchange.
            self.state.mark_reset()
        self.transport.close()
        self._closed = True

    def abort(self) -> None:
        """Stop the session and close the backend after an application error."""
        if self._closed:
            return
        self._terminate_link()
        try:
            self.transport.close()
        finally:
            self._closed = True
