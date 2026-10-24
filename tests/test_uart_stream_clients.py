"""Host stream clients: packet bounds, partial admission, application words and authentication."""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import types
from types import SimpleNamespace
from collections import deque
from pathlib import Path

import pytest

pytestmark = pytest.mark.python_compat

HOST = Path(__file__).parent.parent / "examples/bluespec/uart_link/host/leanlink"
PACKAGE = "uart_stream_test_leanlink"


def modules():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, HOST / "__init__.py", submodule_search_locations=[str(HOST)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(package)
        finally:
            sys.dont_write_bytecode = previous
    return (
        importlib.import_module(f"{PACKAGE}.stream"),
        importlib.import_module(f"{PACKAGE}.applications"),
    )


class Device:
    """Packet-level device whose peer chooses admission and output independently."""

    def __init__(self, handler=None, payload_limit=243, response_limit=243):
        self.protocol = types.SimpleNamespace(
            max_len=payload_limit + 12,
            rsp_limit=response_limit + 12,
            cfg=types.SimpleNamespace(max_payload=payload_limit),
        )
        self.active = True
        self.handler = handler
        self.calls = []
        self.now = 0.0

    def clock(self):
        return self.now

    def transact(self, payload, timeout=None):
        assert timeout is not None and timeout > 0
        self.now += 0.01
        self.calls.append(payload)
        if payload == b"\x00":
            return b"\x00\x00"
        return self.handler(payload)


def output_word(kind, data=b"", *, last=False, end=False):
    keep = (1 << len(data)) - 1
    return bytes([(kind << 5) | keep | (int(last) << 4)]) + data.ljust(4, b"\0")


def output_words(text=b"", trailer=bytes(16), *, hash_mode=False):
    words = []
    for start in range(0, len(text), 4):
        chunk = text[start : start + 4]
        words.append(output_word(0, chunk, last=start + 4 >= len(text)))
    for start in range(0, len(trailer), 4):
        final = start + 4 >= len(trailer)
        words.append(
            output_word(2 if hash_mode else 1, trailer[start : start + 4], last=final, end=final)
        )
    return words


def app_device(outputs, *, capacity=3, payload_limit=243):
    pending = deque(outputs)
    admitted = []

    def handle(packet):
        assert packet[0] == 1 and (len(packet) - 1) % 5 == 0
        offered = [packet[index : index + 5] for index in range(1, len(packet), 5)]
        accepted = min(capacity, len(offered))
        admitted.extend(offered[:accepted])
        # A core can produce output before all of its input has been sent.
        out = [pending.popleft()] if pending else []
        return bytes([0, accepted]) + b"".join(out)

    device = Device(handle, payload_limit=payload_limit)
    return device, admitted


def test_partial_admission_resends_only_the_confirmed_suffix_and_polls_output():
    stream, _ = modules()
    packets = deque([b"\x00\x01aa", b"\x00\x00bb", b"\x00\x01", b"\x00\x00cc"])
    device = Device(lambda _packet: packets.popleft(), payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()
    assert client.transfer([b"11", b"22"], output_words=3, timeout=1) == [b"aa", b"bb", b"cc"]
    assert device.calls == [b"\x00", b"\x011122", b"\x0122", b"\x0122", b"\x01"]


def test_transfer_rejects_first_reply_overrun_before_running_output_callback():
    stream, _ = modules()
    device = Device(lambda _packet: b"\x00\x00aaBB")
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    seen = []

    with pytest.raises(stream.StreamProtocolError, match="too many output words"):
        client.transfer([], output_words=1, on_output=seen.append)

    assert seen == []
    with pytest.raises(stream.StreamProtocolError):
        client.check_session()


def test_transfer_overrun_reply_does_not_run_callbacks_after_a_valid_prefix():
    stream, _ = modules()
    replies = deque([b"\x00\x00aa", b"\x00\x00bbCC"])
    device = Device(lambda _packet: replies.popleft())
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    seen = []

    with pytest.raises(stream.StreamProtocolError, match="too many output words"):
        client.transfer([], output_words=2, on_output=seen.append)

    assert seen == [b"aa"]
    with pytest.raises(stream.StreamProtocolError):
        client.check_session()


@pytest.mark.parametrize("reply", [b"", b"\x00", b"\x01\x00", b"\x00\x02", b"\x00\x00x"])
def test_exchange_rejects_malformed_replies(reply):
    stream, _ = modules()
    device = Device(lambda _packet: reply)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    with pytest.raises(stream.StreamProtocolError):
        client.exchange([b"ab"])


def test_stream_timeout_does_not_resubmit_an_uncertain_exchange():
    stream, _ = modules()

    def uncertain(_packet):
        raise TimeoutError("exchange reply lost")

    device = Device(uncertain)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    with pytest.raises(TimeoutError):
        client.transfer([b"ab"], output_words=1, timeout=1)
    with pytest.raises(stream.StreamProtocolError):
        client.exchange([b"ab"])
    assert device.calls == [b"\x00", b"\x01ab"]


def test_stalled_stream_has_one_overall_deadline():
    stream, _ = modules()
    device = Device(lambda _packet: b"\x00\x00")
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2, clock=device.clock)
    client.reset()
    with pytest.raises(TimeoutError):
        client.transfer([b"ab"], output_words=1, timeout=0.05)
    assert len(device.calls) < 10


def test_reconnect_requires_reset_before_stream_admission():
    stream, _ = modules()
    device = Device(lambda _packet: b"\x00\x01")
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    device.protocol = types.SimpleNamespace(**vars(device.protocol))
    with pytest.raises(stream.StreamProtocolError):
        client.exchange([b"ab"])
    client.reset()
    assert client.exchange([b"ab"]) == (1, [])


@pytest.mark.parametrize("response_limit", [0, 1, 3])
def test_stream_refuses_a_session_that_cannot_return_a_whole_output(response_limit):
    stream, _ = modules()
    device = Device(response_limit=response_limit)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    with pytest.raises(stream.StreamProtocolError):
        client.reset()
    assert not device.calls


def test_stream_validates_input_sizes_before_any_exchange():
    stream, _ = modules()
    device = Device(payload_limit=5)
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    client.reset()
    for words in ([b"x"], [b"aa", b"bb", b"cc"]):
        with pytest.raises(ValueError):
            client.exchange(words)
    assert device.calls == [b"\x00"]


def test_gcd_batching_matches_integer_gcd_with_partial_admission():
    _, apps = modules()
    pairs = [(0, 0), (0, 7), (48, 18), (0xFFFFFFFF, 0xFFFFFFFF)] + [
        (i * 17, i * 13) for i in range(1, 50)
    ]
    admitted = []

    def gcd_peer(packet):
        words = [packet[index : index + 8] for index in range(1, len(packet), 8)]
        accepted = min(2, len(words))
        admitted.extend(words[:accepted])
        results = [
            math.gcd(int.from_bytes(word[:4], "little"), int.from_bytes(word[4:], "little"))
            for word in words[:accepted]
        ]
        return bytes([0, accepted]) + b"".join(result.to_bytes(4, "little") for result in results)

    device = Device(gcd_peer, payload_limit=33)
    client = apps.GcdClient(device, clock=device.clock)
    client.reset()
    assert client.gcd_many(pairs) == [math.gcd(a, b) for a, b in pairs]
    assert len(admitted) == len(pairs)
    assert client.gcd(12, 8) == 4
    with pytest.raises(ValueError):
        client.gcd(-1, 8)


def test_ascon_wire_words_have_exact_start_key_nonce_and_partial_segment_metadata():
    _, apps = modules()
    words = apps.ascon_words(
        "encrypt",
        key=bytes(range(16)),
        nonce=bytes(range(16, 32)),
        associated_data=b"",
        message=b"abcde",
    )
    assert words[0] == bytes.fromhex("000400000000")
    assert words[1:5] == [
        bytes.fromhex(value)
        for value in ("010000010203", "010004050607", "010008090a0b", "01000c0d0e0f")
    ]
    assert words[5] == bytes.fromhex("020f10111213")
    assert words[8] == bytes.fromhex("021f1c1d1e1f")
    assert words[9] == bytes.fromhex("023000000000")
    assert words[10:] == [bytes.fromhex("024f61626364"), bytes.fromhex("025165000000")]
    assert apps.ascon_words("hash", message=b"") == [
        bytes.fromhex("000200000000"),
        bytes.fromhex("027000000000"),
    ]


def test_ascon40_client_word_bytes_match_the_little_endian_exchange_contract():
    _, apps = modules()
    words = list(
        apps._ascon_stream_words40(
            "encrypt",
            key=bytes(range(16)),
            nonce=bytes(range(16, 32)),
            associated_data=b"",
            message=b"\x11\x22\x33\x44",
        )
    )
    assert words[0] == bytes.fromhex("1004000000")
    assert words[1] == bytes.fromhex("2f00010203")
    assert words[4] == bytes.fromhex("3f0c0d0e0f")
    assert words[5] == bytes.fromhex("4f10111213")
    assert words[8] == bytes.fromhex("5f1c1d1e1f")
    assert words[-1] == bytes.fromhex("9f11223344")
    tag = apps.decode_ascon_output(bytes.fromhex("3f11223344"))
    assert (tag.kind, tag.keep, tag.last, tag.end_of_operation, tag.data) == (
        1,
        15,
        True,
        True,
        b"\x11\x22\x33\x44",
    )


def test_ascon_encrypt_streams_large_input_and_reuses_a_key():
    _, apps = modules()
    plaintext = bytes(range(256)) * 5 + b"end"
    ciphertext = bytes(byte ^ 0xA5 for byte in plaintext)
    tag = bytes(range(16))
    device, admitted = app_device(output_words(ciphertext, tag), payload_limit=31)
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    assert client.encrypt(bytes(16), bytes(16), b"ad", plaintext, timeout=10) == ciphertext + tag
    assert sum(word[0] >> 5 == 1 for word in admitted) == 4
    reuse_device, reuse_admitted = app_device(output_words(b"", tag))
    device.handler = reuse_device.handler
    assert client.encrypt(None, bytes(16), b"", b"") == tag
    assert reuse_admitted[0] == bytes.fromhex("1000000000")
    assert all(word[0] >> 5 != 1 for word in reuse_admitted)


def test_ascon_decrypt_returns_plaintext_only_after_the_full_tag_matches():
    _, apps = modules()
    tag = bytes(range(16))
    device, _ = app_device(output_words(b"secret!", tag))
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    assert client.decrypt(bytes(16), bytes(16), b"", bytes(7) + tag) == b"secret!"
    device.handler = app_device(output_words(b"secret!", tag))[0].handler
    with pytest.raises(apps.AuthenticationError):
        client.decrypt(None, bytes(16), b"", bytes(7) + tag[:-1] + b"\xff")


def test_ascon_hash_needs_no_key_and_returns_the_digest():
    _, apps = modules()
    digest = bytes(range(32))
    device, admitted = app_device(output_words(trailer=digest, hash_mode=True))
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    assert client.hash(b"hash me") == digest
    assert admitted[0] == bytes.fromhex("1002000000")
    assert all(word[0] >> 5 != 1 for word in admitted)


def _fail_cli_operation(_args, _device):
    raise ValueError("operation failed")


@pytest.mark.parametrize(
    "word",
    [bytes.fromhex(value) for value in ("8000000000", "0000000000", "1111223344", "7e00000000")],
)
def test_ascon_output_decoder_rejects_reserved_or_empty_success_words(word):
    stream, apps = modules()
    with pytest.raises(stream.StreamProtocolError):
        apps.decode_ascon_output(word)


@pytest.mark.parametrize("keep", [1, 3, 7, 15])
def test_ascon_output_decoder_accepts_zero_padding_for_contiguous_keep_masks(keep):
    _, apps = modules()
    data_bytes = keep.bit_count()
    word = bytes([keep]) + bytes.fromhex("11223344")[:data_bytes].ljust(4, b"\0")

    decoded = apps.decode_ascon_output(word)

    assert decoded.keep == keep
    assert decoded.data == bytes.fromhex("11223344")[:data_bytes]


@pytest.mark.parametrize(
    "keep,padding_offset",
    [(keep, padding_offset) for keep in (1, 3, 7) for padding_offset in range(keep.bit_count(), 4)],
)
def test_ascon_output_decoder_rejects_nonzero_byte_at_each_padding_position(keep, padding_offset):
    _, apps = modules()
    data_bytes = keep.bit_count()
    word = bytearray(bytes([keep]) + bytes.fromhex("11223344")[:data_bytes].ljust(4, b"\0"))
    word[1 + padding_offset] = 0xA5

    with pytest.raises(apps.StreamProtocolError):
        apps.decode_ascon_output(bytes(word))


def test_ascon_error_output_fails_immediately_and_requires_reset():
    stream, apps = modules()
    device, _ = app_device([bytes.fromhex("7f04000000")])
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    with pytest.raises(apps.AsconError, match="4"):
        client.hash(bytes(2000), timeout=10)
    before = len(device.calls)
    with pytest.raises(stream.StreamProtocolError):
        client.hash(b"again")
    assert len(device.calls) == before


@pytest.mark.parametrize("change", ["kind", "last", "keep"])
def test_ascon_rejects_wrong_output_order_or_metadata(change):
    stream, apps = modules()
    words = output_words(b"hello", bytes(16))
    bad = bytearray(words[0])
    if change == "kind":
        bad[0] = (bad[0] & 0x1F) | 0x20
    elif change == "last":
        bad[0] |= 0x10
    else:
        bad[0] = (bad[0] & 0xF0) | 7
    words[0] = bytes(bad)
    device, _ = app_device(words)
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    with pytest.raises(stream.StreamProtocolError):
        client.encrypt(bytes(16), bytes(16), b"", b"hello")


def test_ascon_refuses_key_reuse_after_reset_or_reconnect():
    stream, apps = modules()
    device, _ = app_device(output_words())
    client = apps.AsconClient(device, clock=device.clock)
    client.reset()
    with pytest.raises(ValueError):
        client.encrypt(None, bytes(16), b"", b"")
    client.encrypt(bytes(16), bytes(16), b"", b"")
    client.reset()
    with pytest.raises(ValueError):
        client.encrypt(None, bytes(16), b"", b"")
    device.protocol = types.SimpleNamespace(**vars(device.protocol))
    with pytest.raises(stream.StreamProtocolError):
        client.hash(b"")


def cli_module(monkeypatch):
    monkeypatch.syspath_prepend(str(HOST.parent))
    path = HOST.parent / "app_suite.py"
    spec = importlib.util.spec_from_file_location("uart_application_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "script, expected",
    [
        ("app_suite.py", "decrypt"),
        ("gcd_cli.py", "gcd-many"),
        ("ascon_cli.py", "encrypt"),
    ],
)
def test_application_cli_help_needs_no_board_or_optional_backend_library(script, expected):
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(HOST.parent / script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.parametrize("script", ["app_suite.py", "gcd_cli.py", "ascon_cli.py"])
def test_application_cli_help_does_not_import_optional_backends(script):
    target = str(HOST.parent / script)
    code = """
import importlib.abc
import runpy
import sys

class BlockOptionalBackends(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "serial" or fullname.startswith("serial.") or fullname == "pyftdi" or fullname.startswith("pyftdi."):
            raise ImportError("optional backend blocked")
        return None

sys.meta_path.insert(0, BlockOptionalBackends())
sys.argv = [sys.argv[1], "--help"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    raise SystemExit(exc.code)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, target],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("module", ["gcd_cli", "ascon_cli"])
def test_application_cli_module_help_works_with_host_pythonpath(module):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HOST.parent)
    env.pop("NO_COLOR", None)
    result = subprocess.run(
        [sys.executable, "-B", "-m", f"leanlink.{module}", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_application_cli_rejects_an_unsupported_internal_command(monkeypatch):
    cli = cli_module(monkeypatch)
    with pytest.raises(ValueError, match="unsupported"):
        cli._execute(SimpleNamespace(command="unknown", timeout=1), object())


def package_cli_module(monkeypatch, name):
    monkeypatch.syspath_prepend(str(HOST.parent))
    return importlib.import_module(f"leanlink.{name}")


def test_focused_gcd_cli_reports_result_and_closes_device(monkeypatch, capsys):
    cli = package_cli_module(monkeypatch, "gcd_cli")
    common = importlib.import_module("leanlink.cli_common")
    device = Device(
        lambda packet: b"\x00\x01"
        + math.gcd(
            int.from_bytes(packet[1:5], "little"), int.from_bytes(packet[5:9], "little")
        ).to_bytes(4, "little")
    )
    device.open = lambda timeout: None
    closed = []
    device.close = lambda: closed.append(True)
    monkeypatch.setattr(common, "SerialBackend", lambda *args, **kwargs: object())
    monkeypatch.setattr(common, "LinkDevice", lambda backend, config: device)

    assert cli.main(["--port", "unused", "gcd", "48", "18"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {"success": True, "command": "gcd", "gcd": 6}
    assert closed == [True]


def test_focused_ascon_cli_hash_reports_digest_and_closes_device(monkeypatch, capsys, tmp_path):
    cli = package_cli_module(monkeypatch, "ascon_cli")
    common = importlib.import_module("leanlink.cli_common")
    digest = bytes(range(32))
    device, _ = app_device(output_words(trailer=digest, hash_mode=True))
    device.open = lambda timeout: None
    closed = []
    device.close = lambda: closed.append(True)
    monkeypatch.setattr(common, "SerialBackend", lambda *args, **kwargs: object())
    monkeypatch.setattr(common, "LinkDevice", lambda backend, config: device)

    output = tmp_path / "digest.bin"
    assert (
        cli.main(["--port", "unused", "hash", "--data-hex", "616263", "--output", str(output)]) == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["success"] and report["digest"] == digest.hex()
    assert report["bytes"] == len(digest)
    assert output.read_bytes() == digest
    assert closed == [True]


@pytest.mark.parametrize(
    "execute, expected, status",
    [
        (
            lambda _args, _device: {"answer": 42},
            {"success": True, "command": "gcd", "answer": 42},
            0,
        ),
        (
            _fail_cli_operation,
            {"success": False, "error": {"type": "ValueError", "message": "operation failed"}},
            1,
        ),
    ],
)
def test_cli_close_error_does_not_override_json_result(
    monkeypatch, capsys, execute, expected, status
):
    cli = package_cli_module(monkeypatch, "cli_common")

    class FailedCloseDevice:
        def close(self):
            raise OSError("close failed")

    device = FailedCloseDevice()
    monkeypatch.setattr(cli, "open_device", lambda *args, **kwargs: device)
    args = SimpleNamespace(command="gcd")

    assert cli.run_cli(args, execute) == status
    captured = capsys.readouterr()
    assert captured.out == json.dumps(expected) + "\n"
    assert captured.err == "warning: device cleanup failed: close failed\n"


@pytest.mark.parametrize(
    "execute, expected, expected_status",
    [
        (
            lambda _args, _device: {"answer": 42},
            {"success": True, "command": "gcd", "answer": 42},
            0,
        ),
        (
            _fail_cli_operation,
            {"success": False, "error": {"type": "ValueError", "message": "operation failed"}},
            1,
        ),
    ],
)
def test_cli_cleanup_warning_failure_does_not_override_result(
    monkeypatch, capsys, execute, expected, expected_status
):
    cli = package_cli_module(monkeypatch, "cli_common")

    class FailedCloseDevice:
        def close(self):
            raise OSError("close failed")

    class FailedStderr:
        def write(self, _value):
            raise BrokenPipeError("stderr closed")

    device = FailedCloseDevice()
    monkeypatch.setattr(cli, "open_device", lambda *args, **kwargs: device)
    args = SimpleNamespace(command="gcd")

    with monkeypatch.context() as context:
        context.setattr(cli.sys, "stderr", FailedStderr())
        status = cli.run_cli(args, execute)

    assert status == expected_status
    assert capsys.readouterr().out == json.dumps(expected) + "\n"


def test_cli_cleanup_warning_does_not_write_to_stdout_when_stderr_is_unavailable(
    monkeypatch, capsys
):
    cli = package_cli_module(monkeypatch, "cli_common")

    class FailedCloseDevice:
        def close(self):
            raise OSError("close failed")

    device = FailedCloseDevice()
    monkeypatch.setattr(cli, "open_device", lambda *args, **kwargs: device)
    args = SimpleNamespace(command="gcd")

    with monkeypatch.context() as context:
        context.setattr(cli.sys, "stderr", None)
        status = cli.run_cli(args, lambda _args, _device: {"answer": 42})

    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"success": True, "command": "gcd", "answer": 42}) + "\n"
    )


def test_combined_selftest_variant_is_only_available_for_ascon(monkeypatch):
    cli = cli_module(monkeypatch)

    ascon = cli.parser().parse_args(["selftest", "ascon", "--variant", "128a"])
    assert ascon.application == "ascon" and ascon.variant == "128a"

    gcd = cli.parser().parse_args(["selftest", "gcd"])
    assert gcd.application == "gcd" and not hasattr(gcd, "variant")
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["selftest", "gcd", "--variant", "128"])


def test_application_cli_reports_open_error_as_json_and_closes_backend(monkeypatch, capsys):
    cli = cli_module(monkeypatch)
    closed = []

    class Backend:
        def close(self):
            closed.append("backend")

    class FailedDevice:
        def __init__(self, backend, config):
            self.backend = backend

        def open(self, timeout):
            raise RuntimeError("open failed")

        def close(self):
            closed.append("device")
            self.backend.close()

    monkeypatch.setattr(cli, "SerialBackend", lambda *args, **kwargs: Backend())
    monkeypatch.setattr(cli, "LinkDevice", FailedDevice)

    assert cli.main(["--port", "unused", "gcd", "48", "18"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "success": False,
        "error": {"type": "RuntimeError", "message": "open failed"},
    }
    assert closed == ["device", "backend"]


def test_ascon_selftest_long_vectors_are_resolved_from_module_location(monkeypatch):
    cli = package_cli_module(monkeypatch, "ascon_cli")
    assert cli.long_vectors_path().is_file()


def test_application_cli_hash_uses_stream_clients_and_closes_the_device(monkeypatch, capsys):
    cli = cli_module(monkeypatch)
    digest = bytes(range(32))
    device, _ = app_device(output_words(trailer=digest, hash_mode=True))
    device.open = lambda timeout: None
    closed = []
    device.close = lambda: closed.append(True)
    monkeypatch.setattr(cli, "SerialBackend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "LinkDevice", lambda backend, config: device)
    assert cli.main(["--port", "unused", "hash", "--data-hex", "616263"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["success"] and report["digest"] == digest.hex()
    assert closed == [True]


def test_cli_independent_vectors_preserve_reference_bytes(monkeypatch):
    cli = cli_module(monkeypatch)
    vectors = cli.read_ascon_vectors()
    assert len(vectors) == 122
    first = vectors[0]
    assert first["key"] == bytes.fromhex("217dffa7756983c329b5673f3d61ab1b")
    assert first["nonce"] == bytes.fromhex("91ed6f17e5d9f3339925d7afadd11b8b")
    assert first["expected"] == bytes.fromhex("0c5eda22a66c7913bcac55c5417b75a2")
    assert {vector["operation"] for vector in vectors} == {"encrypt", "decrypt", "hash"}


@pytest.mark.parametrize("bad_length", [0, 1, 15])
def test_ascon_refuses_an_incomplete_supplied_tag_without_device_io(bad_length):
    _, apps = modules()
    device = Device()
    client = apps.AsconClient(device)
    with pytest.raises(ValueError):
        client.decrypt(bytes(16), bytes(16), b"", bytes(bad_length))
    assert not device.calls


def test_ascon_output_sequence_requires_a_complete_tag():
    stream, apps = modules()
    with pytest.raises(stream.StreamProtocolError):
        apps.decode_ascon_outputs(output_words()[:-1], operation="encrypt")


def test_malformed_reset_reply_keeps_the_stream_unbound():
    stream, _ = modules()
    device = Device(lambda _packet: b"\x00\x01")
    client = stream.StreamClient(device, input_bytes=2, output_bytes=2)
    device.transact = lambda *args, **kwargs: b"\x00\x00junk"
    with pytest.raises(stream.StreamProtocolError):
        client.reset()
    with pytest.raises(stream.StreamProtocolError):
        client.exchange([b"ab"])


@pytest.mark.parametrize("option", ["--baud", "--bridge-budget", "--response-limit", "--timeout"])
def test_cli_refuses_zero_configuration_before_opening_a_port(option, monkeypatch, capsys):
    cli = cli_module(monkeypatch)
    opened = []

    def unexpected_open(*args, **kwargs):
        opened.append(True)
        raise RuntimeError("invalid configuration reached the serial backend")

    monkeypatch.setattr(cli, "SerialBackend", unexpected_open)
    assert cli.main(["--port", "unused", option, "0", "gcd", "48", "18"]) == 1
    assert not opened
    assert not json.loads(capsys.readouterr().out)["success"]
