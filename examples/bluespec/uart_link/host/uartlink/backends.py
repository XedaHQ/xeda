"""Optional serial backends. Driver packages load only when a backend is opened."""

from __future__ import annotations

from importlib import import_module
from typing import Protocol


class ByteTransport(Protocol):
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 4096) -> bytes: ...
    def close(self) -> None: ...


class PySerialBackend:
    def __init__(self, port: str, baudrate: int, *, timeout: float = 0.02) -> None:
        import serial

        self._port = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self.baudrate = baudrate

    def write(self, data: bytes) -> int:
        return self._port.write(data)

    def read(self, size: int = 4096) -> bytes:
        available = min(size, self._port.in_waiting)
        return self._port.read(available if available else 1)

    def close(self) -> None:
        self._port.close()


class PyFtdiBackend:
    def __init__(
        self, url: str, baudrate: int, *, latency_ms: int = 1, chunk_size: int | None = None
    ) -> None:
        if not 1 <= latency_ms <= 255 or (chunk_size is not None and chunk_size < 1):
            raise ValueError("latency_ms must be in 1..255 and chunk_size must be positive")
        self._ftdi = import_module("pyftdi.ftdi").Ftdi()
        self._ftdi.open_from_url(url)
        try:
            self._ftdi.set_line_property(8, 1, "N")
            self._ftdi.set_flowctrl("")
            self._ftdi.set_dtr_rts(False, False)
            self.baudrate = self._ftdi.set_baudrate(baudrate, constrain=True)
            self._ftdi.set_latency_timer(latency_ms)
            self._chunk_size = chunk_size or (512 if self._ftdi.is_H_series else 64)
            self._ftdi.read_data_set_chunksize(self._chunk_size)
        except Exception:
            self._ftdi.close()
            raise

    def write(self, data: bytes) -> int:
        return self._ftdi.write_data(data)

    def read(self, size: int = 4096) -> bytes:
        return bytes(self._ftdi.read_data_bytes(min(size, self._chunk_size), attempt=1))

    def close(self) -> None:
        self._ftdi.close()
