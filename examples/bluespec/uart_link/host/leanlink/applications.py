"""GCD and Ascon v1.2 clients for the typed UART stream endpoints.

The pure Ascon word helpers also work with asynchronous test drivers. Decryption keeps all
plaintext private until the complete computed tag matches the supplied tag.
"""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .device import LinkDevice
from .stream import StreamClient, StreamProtocolError


class AsconError(StreamProtocolError):
    """The Ascon endpoint rejected an input word or operation."""


class AuthenticationError(StreamProtocolError):
    """The complete computed authentication tag did not match the supplied tag."""


def _bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{name} must be bytes")
    return bytes(value)


def _segment_words(data: bytes, segment: int) -> list[bytes]:
    words = []
    for offset in range(0, max(1, len(data)), 4):
        chunk = data[offset : offset + 4]
        metadata = ((1 << len(chunk)) - 1) | (int(offset + 4 >= len(data)) << 4)
        words.append(bytes([2, metadata | (segment << 5)]) + chunk.ljust(4, b"\0"))
    return words


def ascon_words(
    operation: str,
    *,
    key: bytes | None = None,
    nonce: bytes | None = None,
    associated_data: bytes = b"",
    message: bytes = b"",
) -> list[bytes]:
    """Encode the legacy six-byte Ascon word format for existing callers.

    ``key=None`` requests a previously loaded key for encryption or decryption. The caller
    must know that such a key exists in the current application state. Hash uses no key or
    nonce. Decryption's ``message`` is ciphertext without the authentication tag.
    """
    if operation not in ("encrypt", "decrypt", "hash"):
        raise ValueError("operation must be encrypt, decrypt, or hash")
    message = _bytes(message, "message")
    associated_data = _bytes(associated_data, "associated_data")
    if operation == "hash":
        if key is not None or nonce is not None or associated_data:
            raise ValueError("hash uses no key, nonce, or associated data")
        return [bytes.fromhex("000200000000"), *_segment_words(message, 3)]
    if nonce is None or len(_bytes(nonce, "nonce")) != 16:
        raise ValueError("nonce must contain 16 bytes")
    nonce = bytes(nonce)
    if key is not None:
        key = _bytes(key, "key")
        if len(key) != 16:
            raise ValueError("key must contain 16 bytes")
    metadata = int(operation == "decrypt") | (int(key is not None) << 2)
    words = [bytes([0, metadata, 0, 0, 0, 0])]
    if key is not None:
        words += [b"\x01\x00" + key[offset : offset + 4] for offset in range(0, 16, 4)]
    words += _segment_words(nonce, 0)
    words += _segment_words(associated_data, 1)
    words += _segment_words(message, 2)
    return words


def _ascon_stream_words40(
    operation: str,
    *,
    key: bytes | None = None,
    nonce: bytes | None = None,
    associated_data: bytes = b"",
    message: bytes = b"",
) -> Iterator[bytes]:
    """Encode the 40-bit stream endpoint format: metadata, then four little-endian data bytes."""
    if operation not in ("encrypt", "decrypt", "hash"):
        raise ValueError("operation must be encrypt, decrypt, or hash")
    message = _bytes(message, "message")
    associated_data = _bytes(associated_data, "associated_data")
    if operation == "hash":
        if key is not None or nonce is not None or associated_data:
            raise ValueError("hash uses no key, nonce, or associated data")

        def hash_words() -> Iterator[bytes]:
            yield _ascon_start40(2)
            yield from _ascon_segment_words40(message, 5)

        return hash_words()
    if nonce is None or len(_bytes(nonce, "nonce")) != 16:
        raise ValueError("nonce must contain 16 bytes")
    nonce = bytes(nonce)
    if key is not None:
        key = _bytes(key, "key")
        if len(key) != 16:
            raise ValueError("key must contain 16 bytes")
    operation_code = int(operation == "decrypt")
    start_data = operation_code | (int(key is not None) << 2)

    def words() -> Iterator[bytes]:
        yield _ascon_start40(start_data)
        if key is not None:
            yield from _ascon_full_words40(key, 1)
        yield from _ascon_full_words40(nonce, 2)
        yield from _ascon_segment_words40(associated_data, 3)
        yield from _ascon_segment_words40(message, 4)

    return words()


def _ascon_start40(data: int) -> bytes:
    return bytes([0x10]) + data.to_bytes(4, "little")


def _ascon_full_words40(data: bytes, kind: int) -> Iterator[bytes]:
    count = len(data) // 4
    for index, offset in enumerate(range(0, len(data), 4)):
        chunk = data[offset : offset + 4]
        yield bytes([(kind << 5) | 0x0F | (int(index == count - 1) << 4)]) + chunk


def _ascon_segment_words40(data: bytes, kind: int) -> Iterator[bytes]:
    count = (len(data) + 3) // 4 or 1
    for index in range(count):
        chunk = data[index * 4 : index * 4 + 4]
        last = index == count - 1
        keep = (1 << len(chunk)) - 1
        metadata = (kind << 5) | (int(last) << 4) | keep
        yield bytes([metadata]) + chunk.ljust(4, b"\0")


@dataclass(frozen=True)
class AsconOutput:
    """One decoded output word; ``data`` contains only bytes selected by ``keep``."""

    kind: int
    keep: int
    last: bool
    end_of_operation: bool
    data: bytes


def decode_ascon_output(word: bytes) -> AsconOutput:
    """Validate and decode one five-byte output word without performing device I/O."""
    word = _bytes(word, "output word")
    if len(word) != 5:
        raise StreamProtocolError("an Ascon output word must contain five bytes")
    metadata = word[0]
    kind = metadata >> 5
    keep = metadata & 15
    if kind > 3 or keep not in (1, 3, 7, 15):
        raise StreamProtocolError("invalid Ascon output kind, padding, or keep mask")
    if any(word[1 + keep :]):
        raise StreamProtocolError("Ascon output has nonzero bytes outside its keep mask")
    if kind == 3 and metadata != 0x7F:
        raise StreamProtocolError(
            "an Ascon error must end its operation with a complete error code"
        )
    return AsconOutput(
        kind,
        keep,
        bool(metadata & 16),
        kind in (1, 2, 3) and bool(metadata & 16),
        word[1 : 1 + keep.bit_count()],
    )


class _AsconOutputCollector:
    def __init__(self, operation: str, text_bytes: int) -> None:
        if operation not in ("encrypt", "decrypt", "hash") or text_bytes < 0:
            raise ValueError("invalid Ascon operation or text length")
        if operation == "hash" and text_bytes:
            raise ValueError("hash has no text output")
        self.text_bytes = text_bytes
        self.text_words = (text_bytes + 3) // 4
        self.trailer_words = 8 if operation == "hash" else 4
        self.trailer_kind = 2 if operation == "hash" else 1
        self.count = self.text_words + self.trailer_words
        self.index = 0
        self.data = bytearray()

    def add(self, raw: bytes) -> None:
        word = decode_ascon_output(raw)
        if word.kind == 3:
            raise AsconError(f"Ascon endpoint error {int.from_bytes(word.data, 'little')}")
        if self.index >= self.count:
            raise StreamProtocolError("Ascon returned output after the end of the operation")
        text = self.index < self.text_words
        expected_bytes = min(4, self.text_bytes - self.index * 4) if text else 4
        expected_last = self.index == self.text_words - 1 if text else self.index == self.count - 1
        if (
            word.kind != (0 if text else self.trailer_kind)
            or len(word.data) != expected_bytes
            or word.last != expected_last
            or word.end_of_operation != (self.index == self.count - 1)
        ):
            raise StreamProtocolError("Ascon output order, length, or boundary metadata is invalid")
        self.data.extend(word.data)
        self.index += 1

    def finish(self) -> bytes:
        if self.index != self.count:
            raise StreamProtocolError("Ascon output ended before the complete result arrived")
        return bytes(self.data)


def decode_ascon_outputs(words: Iterable[bytes], *, operation: str, text_bytes: int = 0) -> bytes:
    """Validate complete output order and return text plus tag, or the hash digest."""
    collector = _AsconOutputCollector(operation, text_bytes)
    for word in words:
        collector.add(word)
    return collector.finish()


def read_ascon_vectors(path: Path | None = None) -> list[dict]:
    """Read the independent C-reference vectors used by the Bluespec testbench.

    Packed BSV values store the first byte in the low bits. ``message`` contains plaintext,
    ciphertext without its tag, or hash input. ``expected`` contains text plus the computed
    tag for AEAD, or the digest for Hash. This loader never derives a cipher result.
    """
    if path is None:
        path = Path(__file__).resolve().parents[3] / "ascon/tb/AsconVectors.bsv"
    vectors = []
    for record in re.findall(r"AsconTest \{(.*?)\}", path.read_text(encoding="utf-8")):

        def field(name: str, pattern: str) -> str:
            match = re.search(rf"\b{name}:\s*{pattern}", record)
            if match is None:
                raise ValueError(f"reference vector lacks field {name}")
            return match.group(1)

        def packed(name: str, length: int) -> bytes:
            value = field(name, r"(?:unpack\()?\d+'h([0-9a-fA-F]+)")
            return int(value, 16).to_bytes(len(value) // 2, "little")[:length]

        ad_length = int(field("adLength", r"\d+'d(\d+)"))
        data_length = int(field("dataLength", r"\d+'d(\d+)"))
        output_length = int(field("outputLength", r"\d+'d(\d+)"))
        vectors.append(
            {
                "variant_a": field("variantA", r"(True|False)") == "True",
                "operation": field("operation", r"(Encrypt|Decrypt|Hash)").lower(),
                "new_key": field("newKey", r"(True|False)") == "True",
                "key": packed("key", 16),
                "nonce": packed("nonce", 16),
                "associated_data": packed("ad", ad_length),
                "message": packed("inputData", data_length),
                "expected": packed("expected", output_length),
            }
        )
    if not vectors:
        raise ValueError("the reference file contains no Ascon test vectors")
    return vectors


class GcdClient:
    """Integer GCD requests, with bounded batching and partial input admission."""

    def __init__(self, device: LinkDevice, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.stream = StreamClient(device, input_bytes=8, output_bytes=4, clock=clock)

    def reset(self, timeout: float = 5.0) -> None:
        self.stream.reset(timeout)

    def gcd_many(self, pairs: Iterable[tuple[int, int]], timeout: float = 5.0) -> list[int]:
        self.stream._timeout(timeout)
        self.stream.check_session()
        maximum = (self.stream._limits()[0] - 1) // self.stream.input_bytes
        deadline = self.stream._clock() + timeout
        pending_pairs = iter(pairs)
        output: list[int] = []
        while batch := list(islice(pending_pairs, maximum)):
            words = []
            for a, b in batch:
                if (
                    not isinstance(a, int)
                    or not isinstance(b, int)
                    or not 0 <= a <= 0xFFFFFFFF
                    or not 0 <= b <= 0xFFFFFFFF
                ):
                    raise ValueError("GCD operands must be unsigned 32-bit integers")
                words.append(a.to_bytes(4, "little") + b.to_bytes(4, "little"))
            remaining = deadline - self.stream._clock()
            if remaining <= 0:
                self.stream.invalidate()
                raise TimeoutError("stream transfer did not finish before its deadline")
            result = self.stream.transfer(words, output_words=len(words), timeout=remaining)
            output.extend(int.from_bytes(word, "little") for word in result)
        return output

    def gcd(self, a: int, b: int, timeout: float = 5.0) -> int:
        return self.gcd_many([(a, b)], timeout)[0]


class AsconClient:
    """Ascon-128 or Ascon-128a v1.2, according to the FPGA build.

    Call ``reset`` after opening a device. A non-None key loads a new 128-bit key. ``key=None``
    reuses a key from a successful operation since RESET in the same device session.
    """

    def __init__(self, device: LinkDevice, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.stream = StreamClient(device, input_bytes=5, output_bytes=5, clock=clock)
        self._key_available = False

    def reset(self, timeout: float = 5.0) -> None:
        self._key_available = False
        self.stream.reset(timeout)

    def _run(
        self,
        operation: str,
        *,
        key: bytes | None = None,
        nonce: bytes | None = None,
        associated_data: bytes = b"",
        message: bytes = b"",
        timeout: float,
    ) -> bytes:
        self.stream.check_session()
        if operation != "hash" and key is None and not self._key_available:
            raise ValueError("key reuse requires a loaded key since RESET")
        words = _ascon_stream_words40(
            operation, key=key, nonce=nonce, associated_data=associated_data, message=message
        )
        collector = _AsconOutputCollector(operation, 0 if operation == "hash" else len(message))
        try:
            self.stream.transfer(
                words, output_words=collector.count, timeout=timeout, on_output=collector.add
            )
            result = collector.finish()
        except BaseException:
            self._key_available = False
            self.stream.invalidate()
            raise
        if key is not None:
            self._key_available = True
        return result

    def encrypt(
        self, key: bytes | None, nonce: bytes, ad: bytes, plaintext: bytes, timeout: float = 5.0
    ) -> bytes:
        """Return ciphertext followed by the 16-byte authentication tag."""
        return self._run(
            "encrypt", key=key, nonce=nonce, associated_data=ad, message=plaintext, timeout=timeout
        )

    def decrypt(
        self,
        key: bytes | None,
        nonce: bytes,
        ad: bytes,
        ciphertext_and_tag: bytes,
        timeout: float = 5.0,
    ) -> bytes:
        """Return plaintext only after comparing the complete 16-byte authentication tag."""
        data = _bytes(ciphertext_and_tag, "ciphertext_and_tag")
        if len(data) < 16:
            raise ValueError("ciphertext_and_tag must include a complete 16-byte tag")
        output = self._run(
            "decrypt", key=key, nonce=nonce, associated_data=ad, message=data[:-16], timeout=timeout
        )
        if not hmac.compare_digest(output[-16:], data[-16:]):
            raise AuthenticationError("Ascon authentication tag does not match")
        return output[:-16]

    def hash(self, message: bytes, timeout: float = 5.0) -> bytes:
        """Return the 32-byte Ascon-Hash v1.2 digest."""
        return self._run("hash", message=message, timeout=timeout)
