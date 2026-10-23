"""Bounded input staging for the UART stream clients."""

from __future__ import annotations

import pytest
from math import gcd

from .test_uart_stream_clients import Device, modules

pytestmark = pytest.mark.python_compat


def test_transfer_exchanges_one_packet_before_consuming_more_input():
    stream, _ = modules()
    device = Device(lambda packet: bytes([0, (len(packet) - 1) // 2]), payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()

    def words():
        yield b"aa"
        yield b"bb"
        assert device.calls == [b"\x00", b"\x01aabb"]
        yield b"cc"

    assert client.transfer(words(), output_words=0, timeout=1) == []
    assert device.calls == [b"\x00", b"\x01aabb", b"\x01cc"]


def test_transfer_keeps_the_exact_unaccepted_suffix_through_zero_acceptance():
    stream, _ = modules()
    accepted = iter([1, 0, 2, 1])
    device = Device(lambda _packet: bytes([0, next(accepted)]), payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()

    assert client.transfer([b"aa", b"bb", b"cc", b"dd"], output_words=0, timeout=1) == []
    assert device.calls == [
        b"\x00",
        b"\x01aabb",
        b"\x01bbcc",
        b"\x01bbcc",
        b"\x01dd",
    ]


def test_invalid_late_word_after_exchange_invalidates_the_stream_session():
    stream, _ = modules()
    device = Device(lambda packet: bytes([0, (len(packet) - 1) // 2]), payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()

    with pytest.raises(ValueError, match="each input word"):
        client.transfer(iter([b"aa", b"bb", b"x"]), output_words=0, timeout=1)

    assert device.calls == [b"\x00", b"\x01aabb"]
    with pytest.raises(stream.StreamProtocolError, match="RESET is required"):
        client.check_session()


@pytest.mark.parametrize("failure", ["malformed_word", "iterator_exception"])
def test_input_failure_before_first_exchange_keeps_stream_session(failure):
    stream, _ = modules()
    device = Device(payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()

    def words():
        if failure == "iterator_exception":
            raise RuntimeError("input iterator failed")
        yield b"x"

    expected = ValueError if failure == "malformed_word" else RuntimeError
    with pytest.raises(expected):
        client.transfer(words(), output_words=0, timeout=1)

    assert device.calls == [b"\x00"]
    client.check_session()


def test_gcd_consumes_pairs_in_bounded_chunks():
    _, apps = modules()

    def gcd_peer(packet):
        results = []
        for offset in range(1, len(packet), 8):
            a = int.from_bytes(packet[offset : offset + 4], "little")
            b = int.from_bytes(packet[offset + 4 : offset + 8], "little")
            results.append(gcd(a, b))
        return bytes([0, len(results)]) + b"".join(
            result.to_bytes(4, "little") for result in results
        )

    device = Device(gcd_peer, payload_limit=17)
    client = apps.GcdClient(device, clock=device.clock)
    client.reset()

    def pairs():
        yield (12, 8)
        yield (18, 12)
        assert len(device.calls) >= 2
        assert device.calls[1] == (
            b"\x01"
            + (12).to_bytes(4, "little")
            + (8).to_bytes(4, "little")
            + (18).to_bytes(4, "little")
            + (12).to_bytes(4, "little")
        )
        yield (21, 14)

    assert client.gcd_many(pairs()) == [4, 6, 7]
    assert len(device.calls) >= 3


def test_ascon_top_level_arguments_are_validated_before_device_io():
    _, apps = modules()
    device = Device()
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()

    with pytest.raises(ValueError, match="nonce must contain 16 bytes"):
        client.encrypt(bytes(16), b"short", b"", b"message")

    assert device.calls == [b"\x00"]
