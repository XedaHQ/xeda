"""The frame protocol between the local xeda and the remote agent."""

import io
import os
import struct
import threading

import pytest

from xeda.remote.errors import RemoteProtocolError
from xeda.remote.frames import (
    BEAT,
    CANCEL,
    DATA,
    END,
    JSON,
    LOG,
    MAGIC,
    MAX_PAYLOAD,
    DataReader,
    FrameWriter,
    decode_json,
    read_frame,
    skip_to_magic,
)

pytestmark = pytest.mark.python_compat


def frame_bytes(kind: bytes, payload: bytes = b"") -> bytes:
    out = io.BytesIO()
    FrameWriter(out).send(kind, payload)
    return out.getvalue()


def test_frames_round_trip_and_eof():
    out = io.BytesIO()
    writer = FrameWriter(out)
    writer.send_json({"hello": {"pid": 7, "name": "café"}})
    writer.send_stream(io.BytesIO(b"abcdefgh"), chunk=3)
    writer.send(LOG, b"line\n")
    writer.send(BEAT)
    writer.send(CANCEL)
    stream = io.BytesIO(out.getvalue())
    frames = []
    while (frame := read_frame(stream)) is not None:
        frames.append(frame)
    assert [kind for kind, _ in frames] == [JSON, DATA, DATA, DATA, END, LOG, BEAT, CANCEL]
    assert decode_json(frames[0][1]) == {"hello": {"pid": 7, "name": "café"}}
    assert b"".join(payload for kind, payload in frames if kind == DATA) == b"abcdefgh"
    assert frames[-1] == (CANCEL, b"")
    assert read_frame(stream) is None


def test_frame_layout_is_kind_length_payload():
    assert frame_bytes(LOG, b"xy") == b"L" + struct.pack(">I", 2) + b"xy"


@pytest.mark.parametrize("cut", [1, 3, 5, 6])
def test_truncated_frame_is_a_protocol_error(cut):
    data = frame_bytes(LOG, b"abcdef")
    assert len(data) > cut
    with pytest.raises(RemoteProtocolError, match="ended inside"):
        read_frame(io.BytesIO(data[:cut]))


def test_unknown_kind_and_oversize_length_are_protocol_errors():
    with pytest.raises(RemoteProtocolError, match="unknown frame kind"):
        read_frame(io.BytesIO(b"X" + struct.pack(">I", 0)))
    huge = b"D" + struct.pack(">I", MAX_PAYLOAD + 1)
    with pytest.raises(RemoteProtocolError, match="over"):
        read_frame(io.BytesIO(huge))  # refused before the payload is read


def test_writer_refuses_a_bad_kind_and_an_oversize_payload():
    writer = FrameWriter(io.BytesIO())
    with pytest.raises(ValueError):
        writer.send(b"X", b"")
    with pytest.raises(ValueError):
        writer.send(DATA, bytes(MAX_PAYLOAD + 1))
    with pytest.raises(ValueError):
        writer.send_stream(io.BytesIO(b"a"), chunk=0)


def test_read_frame_survives_short_reads():
    class Trickle(io.RawIOBase):
        def __init__(self, data):
            self.data = io.BytesIO(data)

        def readable(self):
            return True

        def readinto(self, buffer):
            chunk = self.data.read(min(len(buffer), 2))
            buffer[: len(chunk)] = chunk
            return len(chunk)

    stream = Trickle(frame_bytes(JSON, b'{"a": 1}') + frame_bytes(END))
    assert read_frame(stream) == (JSON, b'{"a": 1}')  # type: ignore[arg-type]
    assert read_frame(stream) == (END, b"")  # type: ignore[arg-type]
    assert read_frame(stream) is None  # type: ignore[arg-type]


def test_invalid_json_is_a_protocol_error():
    for payload in (b"{", b"\xff\xfe"):
        with pytest.raises(RemoteProtocolError):
            decode_json(payload)


def test_noise_before_the_magic_is_skipped():
    frame = frame_bytes(JSON, b"{}")
    stream = io.BytesIO(b"Welcome!\n" + MAGIC + frame)
    assert skip_to_magic(stream) == b"Welcome!\n"
    assert read_frame(stream) == (JSON, b"{}")
    assert skip_to_magic(io.BytesIO(MAGIC)) == b""
    assert skip_to_magic(io.BytesIO(b"XEDA-AGENT " + MAGIC)) == b"XEDA-AGENT "


def test_magic_errors_carry_only_a_short_preview_of_the_skipped_text():
    banner = b"A" * 40 + b"SECRET" + b"\xff\n" * 500
    for data, limit, count in ((banner, 1 << 20, len(banner)), (banner + MAGIC, 100, 100 + 14)):
        with pytest.raises(RemoteProtocolError) as caught:
            skip_to_magic(io.BytesIO(data), limit=limit)
        message = str(caught.value)
        assert f"{count} bytes" in message
        assert "A" * 40 in message
        assert "SECRET" not in message
        assert len(message) < 250


def test_missing_magic_is_a_protocol_error():
    with pytest.raises(RemoteProtocolError, match="ended before the agent marker"):
        skip_to_magic(io.BytesIO(b"bash: python: command not found\n"))
    with pytest.raises(RemoteProtocolError, match="no agent marker in the first 10 bytes"):
        skip_to_magic(io.BytesIO(b"x" * 100 + MAGIC), limit=10)
    assert skip_to_magic(io.BytesIO(b"x" * 10 + MAGIC), limit=10) == b"x" * 10


def test_data_reader_reads_until_end_and_leaves_the_next_frame():
    out = io.BytesIO()
    writer = FrameWriter(out)
    payload = bytes(range(256)) * 40
    writer.send_stream(io.BytesIO(payload), chunk=1000)
    writer.send(LOG, b"after")
    stream = io.BytesIO(out.getvalue())
    reader = DataReader(stream)
    assert reader.read(10) == payload[:10]
    assert reader.read() == payload[10:]
    assert reader.ended
    assert reader.read(5) == b""
    assert read_frame(stream) == (LOG, b"after")


def test_data_reader_reads_no_frame_for_an_empty_buffer():
    stream = io.BytesIO(frame_bytes(DATA, b"ab") + frame_bytes(END))
    reader = DataReader(stream)
    assert reader.readinto(bytearray()) == 0
    assert stream.tell() == 0 and not reader.ended
    assert reader.read(0) == b""
    assert reader.read() == b"ab"


def test_data_reader_drain_skips_to_the_end_frame():
    out = io.BytesIO()
    writer = FrameWriter(out)
    writer.send_stream(io.BytesIO(b"x" * 5000), chunk=700)
    writer.send(LOG, b"after")
    stream = io.BytesIO(out.getvalue())
    reader = DataReader(stream)
    reader.read(3)
    reader.drain()
    assert read_frame(stream) == (LOG, b"after")


def test_data_reader_refuses_the_end_of_the_connection_and_foreign_frames():
    with pytest.raises(RemoteProtocolError, match="ended inside a data stream"):
        DataReader(io.BytesIO(frame_bytes(DATA, b"abc"))).read()
    with pytest.raises(RemoteProtocolError, match="inside a data stream"):
        DataReader(io.BytesIO(frame_bytes(LOG, b"x") + frame_bytes(END))).read()
    with pytest.raises(RemoteProtocolError, match="has a payload"):
        DataReader(io.BytesIO(frame_bytes(END, b"x"))).read()


def test_data_reader_passes_frames_of_other_kinds_to_the_callback():
    seen = []
    data = (
        frame_bytes(DATA, b"ab")
        + frame_bytes(BEAT)
        + frame_bytes(DATA, b"cd")
        + frame_bytes(CANCEL)
        + frame_bytes(END)
    )
    reader = DataReader(io.BytesIO(data), on_frame=lambda kind, payload: seen.append(kind))
    assert reader.read() == b"abcd"
    assert seen == [BEAT, CANCEL]


def test_writer_is_safe_for_two_threads():
    pipe_read, pipe_write = os.pipe()
    count = 400
    payloads = {b"A": b"a" * 70_000, b"B": b"b" * 3}
    barrier = threading.Barrier(len(payloads))
    errors: list[BaseException] = []
    received = []
    with os.fdopen(pipe_read, "rb") as reader, os.fdopen(pipe_write, "wb") as sink:
        writer = FrameWriter(sink)  # type: ignore[arg-type]

        def produce(name: bytes) -> None:
            try:
                barrier.wait(timeout=10)  # both threads start sending together
                for _ in range(count):
                    writer.send(LOG, name + payloads[name])
            except BaseException as error:  # reported by the main thread
                errors.append(error)
                barrier.abort()

        threads = [threading.Thread(target=produce, args=(name,)) for name in payloads]
        for thread in threads:
            thread.start()
        try:
            for _ in range(2 * count):
                frame = read_frame(reader)  # type: ignore[arg-type]
                assert frame is not None
                received.append(frame[1])
        finally:
            for thread in threads:
                thread.join(timeout=30)
    assert errors == []
    for name in payloads:
        assert received.count(name + payloads[name]) == count


def test_writer_refuses_frames_after_a_failed_write():
    class Failing(io.BytesIO):
        def write(self, data):
            if len(self.getvalue()) >= 5:  # the header is in, the payload fails
                raise OSError("pipe is gone")
            return super().write(data)

    writer = FrameWriter(Failing())
    with pytest.raises(OSError):
        writer.send(LOG, b"ok")
    with pytest.raises(RemoteProtocolError, match="earlier write failed"):
        writer.send(BEAT)
