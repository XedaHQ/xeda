"""Byte transports for ``leanlink.device.LinkDevice``.

A backend has four methods:

* ``write(data)``: send bytes to the FPGA.
* ``read(max_bytes, timeout)``: return the bytes that are available, waiting up to ``timeout``
  seconds for the first one. It returns ``b""`` if there is none.
* ``purge()``: drop the bytes that wait in the receive buffer.
* ``close()``.

``SerialBackend`` (pyserial) and ``FtdiBackend`` (pyftdi) import their library when you create
the object, so ``import leanlink`` needs neither. ``LoopbackBackend`` is an in-memory backend
for tests: its peer is any object with ``rx(data, now)`` and ``tx(now) -> bytes``.

Both hardware backends wait for data without a polling loop: ``SerialBackend`` uses ``select``
on the port's file descriptor, and ``FtdiBackend`` has a reader thread. Neither assigns a
pyserial ``timeout`` on an open port. That assignment makes pyserial re-apply the terminal
settings (and the custom baud rate) at every call.
"""

from __future__ import annotations

import os
import select
import threading
import time
from collections import deque
from typing import Callable, Protocol


class BackendUnavailable(RuntimeError):
    """The library that the backend needs is not installed."""


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, max_bytes: int, timeout: float) -> bytes: ...

    def purge(self) -> None: ...

    def close(self) -> None: ...


class SerialBackend:
    """pyserial backend. Default 12 Mbaud, 8N1.

    The backend is POSIX only: ``read`` waits with ``select`` on the file descriptor of the port.
    The port always has ``timeout=0``, so ``timeout`` is not an option here: the ``timeout``
    argument of ``read`` is the wait. Any other keyword goes to ``serial.Serial``.
    """

    def __init__(self, port: str, baudrate: int = 12_000_000, **kwargs: object) -> None:
        if "timeout" in kwargs:
            raise TypeError("SerialBackend has no timeout option: read(max_bytes, timeout) waits")
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise BackendUnavailable("pyserial is not installed (pip install pyserial)") from exc
        self._serial = serial.Serial(
            port, baudrate=baudrate, bytesize=8, parity="N", stopbits=1, timeout=0, **kwargs
        )

    def write(self, data: bytes) -> None:
        self._serial.write(data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        fd = self._serial.fileno()
        ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        try:
            return os.read(fd, max_bytes)
        except BlockingIOError:  # pragma: no cover - a spurious wake-up
            return b""

    def purge(self) -> None:
        self._serial.reset_input_buffer()

    def close(self) -> None:
        self._serial.close()


class FtdiBackend:
    """pyftdi backend, for example ``ftdi://ftdi:2232h:SERIAL/2``.

    The chip holds received bytes until its latency timer expires, its buffer fills, or the
    event character arrives. ``latency_ms`` (1 to 255, the chip default is 16) sets the timer.
    ``event_char`` (0 to 255, or None for off) makes the chip send its buffer at once when that
    byte arrives. Both settings are always written, so a setting that an earlier program left in
    the chip never stays. For a link whose frames end with a zero byte use ``event_char=0``.
    ``chunksize`` is the size of one USB read. ``purge_on_open=False`` keeps the bytes that the
    chip received while no program had the port open (a test aid for power-up captures).

    A reader thread reads the chip all the time and queues the data. ``read`` waits for that
    queue, so a slow chip timer delays data but never blocks a write. ``overruns`` counts the
    reads whose status byte had the chip's receive-overrun flag. It needs pyftdi 0.5x (it uses
    ``Ftdi._read``).

    ``purge`` and ``close`` take one lock and wait for the reader thread. Do not call them from a
    signal handler or from inside another call of the backend: that deadlocks. Let
    ``KeyboardInterrupt`` unwind the program and close the backend in a ``finally`` block.
    """

    OVERRUN_BIT = 0x02  # modem status byte 2: OE (the chip's receive FIFO overflowed)

    def __init__(
        self,
        url: str,
        baudrate: int = 12_000_000,
        latency_ms: int = 1,
        event_char: int | None = None,
        chunksize: int = 512,
        purge_on_open: bool = True,
    ) -> None:
        try:
            from pyftdi.ftdi import Ftdi  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise BackendUnavailable("pyftdi is not installed (pip install pyftdi)") from exc
        self._ftdi = Ftdi()
        self._ftdi.open_from_url(url)
        actual = self._ftdi.set_baudrate(baudrate)
        self.baudrate = actual
        self._ftdi.set_line_property(8, 1, "N")
        self._ftdi.set_latency_timer(latency_ms)
        self.latency_ms = latency_ms
        if event_char is None:
            self._ftdi.set_event_char(0, False)
        else:
            self._ftdi.set_event_char(event_char, True)
        self.event_char = event_char
        self._ftdi.read_data_set_chunksize(chunksize)
        self._packet = self._ftdi._max_packet_size  # each USB packet starts with 2 status bytes
        if purge_on_open:
            self._ftdi.purge_buffers()
        self.overruns = 0
        self._queue: deque[bytes] = deque()
        self._pause = False  # a purge is under way: the reader must park between reads
        self._parked = False  # the reader waits between two reads
        self.purge_timeout = 2.0  # seconds that purge waits for the reader to park
        self._cond = threading.Condition()
        self._op_lock = threading.Lock()  # one purge or close at a time
        self._stop = False
        self._hold_until = 0.0
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._reader, name="ftdi-reader", daemon=True)
        self._thread.start()

    def _strip(self, raw: bytes) -> bytes:
        """Remove the two status bytes at the start of each USB packet."""
        packet = self._packet
        if len(raw) <= packet:
            if len(raw) >= 2 and raw[1] & self.OVERRUN_BIT:
                self.overruns += 1
            return bytes(raw[2:])
        out = bytearray()
        for start in range(0, len(raw), packet):
            if len(raw) - start >= 2 and raw[start + 1] & self.OVERRUN_BIT:
                self.overruns += 1
            out += raw[start + 2 : start + packet]
        return bytes(out)

    def _park_while_paused(self) -> None:
        """Wait between two reads while a purge is under way."""
        with self._cond:
            while self._pause and not self._stop:
                self._parked = True
                self._cond.notify_all()
                self._cond.wait()
            self._parked = False

    def _reader(self) -> None:
        try:
            while not self._stop:
                self._park_while_paused()
                if self._stop:
                    break
                delay = self._hold_until - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 0.005))
                    continue
                raw = bytes(self._ftdi._read())
                data = self._strip(raw)
                if data:
                    with self._cond:
                        self._queue.append(data)
                        self._cond.notify_all()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller of read()
            with self._cond:
                if not self._stop:
                    self._error = exc
                self._cond.notify_all()

    def stall_reader(self, seconds: float) -> None:
        """Test aid: the reader thread does not read the chip for ``seconds``."""
        self._hold_until = time.monotonic() + seconds

    def write(self, data: bytes) -> None:
        self._ftdi.write_data(data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._queue:
                if self._error is not None:
                    raise RuntimeError(f"FTDI reader failed: {self._error!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            first = self._queue.popleft()
            if len(first) > max_bytes:
                self._queue.appendleft(first[max_bytes:])
                return first[:max_bytes]
            out = bytearray(first)
            while self._queue and len(out) < max_bytes:
                nxt = self._queue.popleft()
                room = max_bytes - len(out)
                if len(nxt) > room:
                    self._queue.appendleft(nxt[room:])
                    nxt = nxt[:room]
                out += nxt
            return bytes(out)

    def purge(self) -> None:
        """Drop the chip's receive buffer and everything that the reader has queued.

        The reader parks between two reads first, so no read is under way when the chip buffer is
        purged. Bytes that a read brought before that are older than the purge and are dropped.
        A byte that the chip receives after the chip purge is never dropped. Raise ``RuntimeError``
        if the reader does not park within ``purge_timeout`` seconds, or if the backend is closed.
        """
        with self._op_lock:
            if self._stop:
                raise RuntimeError("the FTDI backend is closed")
            with self._cond:
                self._pause = True
                try:
                    parked = self._cond.wait_for(
                        lambda: self._parked or self._error is not None, self.purge_timeout
                    )
                    if self._error is not None:
                        raise RuntimeError(f"FTDI reader failed: {self._error!r}")
                    if not parked:
                        raise RuntimeError("the FTDI reader did not stop for the purge")
                    self._ftdi.purge_rx_buffer()
                    self._queue.clear()
                finally:
                    self._pause = False
                    self._cond.notify_all()

    def close(self) -> None:
        with self._op_lock:  # wait for a purge that is under way
            with self._cond:
                self._stop = True
                self._cond.notify_all()
            if threading.current_thread() is not self._thread:  # the reader cannot join itself
                self._thread.join(timeout=1.0 + 2e-3 * self.latency_ms)
            self._ftdi.close()


class LoopbackBackend:
    """In-memory backend for tests.

    ``peer.rx(data, now)`` receives what the host writes at time ``now``. ``peer.tx(now)``
    returns the bytes that the peer has sent by ``now``. ``clock`` supplies ``now``. With a
    manual clock the test controls time. ``read`` does not wait: the device's ``sleep``
    function advances the clock.
    """

    def __init__(self, peer: object, clock: Callable[[], float]) -> None:
        self.peer = peer
        self.clock = clock
        self._buffer = bytearray()
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data
        self.peer.rx(bytes(data), self.clock())  # type: ignore[attr-defined]

    def _fill(self) -> None:
        self._buffer += self.peer.tx(self.clock())  # type: ignore[attr-defined]

    def read(self, max_bytes: int, timeout: float) -> bytes:
        self._fill()
        out = bytes(self._buffer[:max_bytes])
        del self._buffer[:max_bytes]
        return out

    def purge(self) -> None:
        self._fill()
        self._buffer.clear()

    def close(self) -> None:
        pass
