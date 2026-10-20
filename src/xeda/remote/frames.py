"""The frame protocol between the local xeda and the xeda agent on the remote host.

After the agent writes `MAGIC`, both directions carry frames. A frame is one byte for the kind,
four bytes (big-endian) for the payload length and the payload. This module only moves frames.
It does not know what a frame means.
"""

import io
import json
import struct
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

from ..utils import json_encodable
from .errors import RemoteProtocolError

#: The first bytes that the agent writes. Text that the login shell prints before them is noise.
MAGIC = b"XEDA-AGENT 1\n"

#: A JSON document.
JSON = b"J"
#: A piece of a binary stream. A stream is `DATA` frames and then one `END` frame.
DATA = b"D"
#: The end of a binary stream.
END = b"E"
#: A line of the log of the launch.
LOG = b"L"
#: A heartbeat.
BEAT = b"H"
#: A request to cancel the launch.
CANCEL = b"C"

KINDS = frozenset({JSON, DATA, END, LOG, BEAT, CANCEL})

#: The largest payload of one frame, in bytes.
MAX_PAYLOAD = 16 << 20

_HEADER = struct.Struct(">cI")


class FrameWriter:
    """Write frames to a binary stream. `send` is safe to call from more than one thread: the
    frames of different threads never mix. A write that fails or is interrupted may leave half a
    frame in the stream. The writer then refuses every later frame."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._broken = False

    def send(self, kind: bytes, payload: bytes = b"") -> None:
        """Write one frame and flush the stream."""
        if kind not in KINDS:
            raise ValueError(f"unknown frame kind {kind!r}")
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f"a frame payload of {len(payload)} bytes is over {MAX_PAYLOAD}")
        with self._lock:
            if self._broken:
                raise RemoteProtocolError("an earlier write failed inside a frame")
            try:
                self._stream.write(_HEADER.pack(kind, len(payload)))
                self._stream.write(payload)
                self._stream.flush()
            except BaseException:
                self._broken = True
                raise

    def send_json(self, obj: Any) -> None:
        """Write `obj` as one `JSON` frame."""
        text = json.dumps(obj, separators=(",", ":"), default=json_encodable)
        self.send(JSON, text.encode("utf-8"))

    def send_stream(self, src: BinaryIO, chunk: int = 1 << 20) -> None:
        """Write the rest of `src` as `DATA` frames and then an `END` frame. Frames of other
        threads can come between them. Only one thread at a time may send a stream."""
        if not 0 < chunk <= MAX_PAYLOAD:
            raise ValueError(f"the chunk size {chunk} is not between 1 and {MAX_PAYLOAD}")
        while data := src.read(chunk):
            self.send(DATA, data)
        self.send(END)


def _read_exactly(stream: BinaryIO, count: int, what: str) -> bytes:
    data = bytearray()
    while len(data) < count:
        piece = stream.read(count - len(data))
        if not piece:
            raise RemoteProtocolError(f"the stream ended inside {what}")
        data += piece
    return bytes(data)


def read_frame(stream: BinaryIO) -> tuple[bytes, bytes] | None:
    """Read one frame and return its kind and payload.

    Return `None` at the end of the stream before a frame starts. The end inside a frame, an
    unknown kind or a payload length over `MAX_PAYLOAD` raises `RemoteProtocolError`."""
    first = stream.read(1)
    if not first:
        return None
    kind, length = _HEADER.unpack(first + _read_exactly(stream, _HEADER.size - 1, "a frame header"))
    if kind not in KINDS:
        raise RemoteProtocolError(f"unknown frame kind {kind!r}")
    if length > MAX_PAYLOAD:
        raise RemoteProtocolError(f"a frame payload of {length} bytes is over {MAX_PAYLOAD}")
    payload = _read_exactly(stream, length, "a frame payload") if length else b""
    return kind, payload


def decode_json(payload: bytes) -> Any:
    """Return the value of the JSON document in the payload of a `JSON` frame."""
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError as error:  # UnicodeDecodeError and JSONDecodeError are both ValueError
        raise RemoteProtocolError(f"a JSON frame does not hold JSON: {error}") from error


def _preview(seen: bytes) -> str:
    """The count of the bytes and a short escaped start of them, for an error message. The text
    can come from a login banner, which may reach a `--json` document."""
    return f"{len(seen)} bytes, starting {seen[:40]!r}"


def skip_to_magic(stream: BinaryIO, limit: int = 1 << 20) -> bytes:
    """Read the stream up to and including `MAGIC` and return the bytes before it.

    A login shell can print text before the agent starts, for example from a `.bashrc`. This text
    is skipped. If the stream ends or has more than `limit` bytes before `MAGIC`, raise
    `RemoteProtocolError`."""
    seen = bytearray()
    while True:
        byte = stream.read(1)
        if not byte:
            raise RemoteProtocolError(
                f"the stream ended before the agent marker; it had {_preview(bytes(seen))}"
            )
        seen += byte
        if seen.endswith(MAGIC):
            return bytes(seen[: -len(MAGIC)])
        if len(seen) > limit + len(MAGIC):
            raise RemoteProtocolError(
                f"no agent marker in the first {limit} bytes; they are {_preview(bytes(seen))}"
            )


class DataReader(io.RawIOBase):
    """A file-like object over the `DATA` frames of one stream, up to its `END` frame. It
    reads zero bytes (the end of the file) after `END`. Wrap it in `io.BufferedReader` where a
    `BinaryIO` is needed.

    A frame of another kind inside the stream goes to `on_frame(kind, payload)`. Without
    `on_frame`, it raises `RemoteProtocolError`, as does the end of the connection inside the
    stream."""

    def __init__(
        self, stream: BinaryIO, on_frame: Callable[[bytes, bytes], None] | None = None
    ) -> None:
        super().__init__()
        self._stream = stream
        self._on_frame = on_frame
        self._pending = memoryview(b"")
        self._ended = False

    @property
    def ended(self) -> bool:
        """Whether the `END` frame has been read."""
        return self._ended

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        target = memoryview(buffer).cast("B")
        if not target:
            return 0  # not the end of the stream: read no frame for it
        while not self._pending and not self._ended:
            self._next_frame()
        count = min(len(target), len(self._pending))
        target[:count] = self._pending[:count]
        self._pending = self._pending[count:]
        return count

    def _next_frame(self) -> None:
        frame = read_frame(self._stream)
        if frame is None:
            raise RemoteProtocolError("the connection ended inside a data stream")
        kind, payload = frame
        if kind == DATA:
            self._pending = memoryview(payload)
        elif kind == END:
            if payload:
                raise RemoteProtocolError("the END frame of a data stream has a payload")
            self._ended = True
        elif self._on_frame is not None:
            self._on_frame(kind, payload)
        else:
            raise RemoteProtocolError(f"frame {kind!r} inside a data stream")

    def drain(self) -> None:
        """Read and drop the rest of the stream, so that the next frame is the one after `END`."""
        while self.read(1 << 16):
            pass
