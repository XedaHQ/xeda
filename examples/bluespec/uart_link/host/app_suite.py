#!/usr/bin/env python3
"""Use a board running the GCD or Ascon UART application.

Examples, from the repository root:

    python3 examples/bluespec/uart_link/host/app_suite.py list
    python3 examples/bluespec/uart_link/host/app_suite.py --port /dev/cu.usbserial-XXXX gcd 48 18
    python3 examples/bluespec/uart_link/host/app_suite.py --board ulx3s --ftdi URL selftest ascon
    python3 examples/bluespec/uart_link/host/app_suite.py --port PORT hash --input message.bin

Each command opens a fresh session and resets the application. Use the Python clients for key
reuse or multiple operations in one session. This script does not program a board. Results are
JSON on stdout. Encryption returns ciphertext followed by its tag. Decryption releases no
plaintext until the complete tag matches. A failed or uncertain operation exits with status 1;
it is never retried in a new session.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from leanlink import HostConfig, LinkDevice  # noqa: E402
from leanlink.applications import (  # noqa: E402
    AsconClient,
    AuthenticationError,
    GcdClient,
    read_ascon_vectors,
)
from leanlink.backends import BackendUnavailable, FtdiBackend, SerialBackend  # noqa: E402


def discover() -> list[dict]:
    """List serial identities without opening ports or selecting a board by guesswork."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise BackendUnavailable("serial discovery needs pyserial") from exc
    ports = []
    for port in sorted(list_ports.comports(), key=lambda item: item.device):
        ports.append(
            {
                "port": port.device,
                "serial_number": port.serial_number,
                "vendor_id": port.vid,
                "product_id": port.pid,
                "description": port.description,
                "location": port.location,
                "hardware_id": port.hwid,
            }
        )
    return ports


def _hex(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an even number of hexadecimal digits") from exc


def _operand(value: str) -> int:
    try:
        number = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected an integer, optionally with a 0x prefix"
        ) from exc
    if not 0 <= number <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("operand must be in 0..0xffffffff")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    connection = result.add_mutually_exclusive_group()
    connection.add_argument("--port", help="exact serial device path from the list command")
    connection.add_argument("--serial-number", help="select exactly one enumerated serial number")
    connection.add_argument("--ftdi", help="exact pyftdi URL, including serial number and channel")
    result.add_argument("--board", choices=("arty", "basys3", "ulx3s", "stlv7325"), default="arty")
    result.add_argument(
        "--baud", type=int, help="UART rate; default 12 Mbaud on Arty/Basys-3, otherwise 3 Mbaud"
    )
    result.add_argument(
        "--bridge-budget", type=int, help="bytes in flight; default 2048/512/1024 by board"
    )
    result.add_argument(
        "--response-limit",
        type=int,
        help="response wire bytes; default min(255, bridge budget - 76)",
    )
    result.add_argument(
        "--timeout", type=float, default=30.0, help="seconds per operation, across all exchanges"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list exact serial identities without opening a board")
    gcd = commands.add_parser("gcd", help="compute the GCD of two unsigned 32-bit integers")
    gcd.add_argument("a", type=_operand)
    gcd.add_argument("b", type=_operand)
    batch = commands.add_parser("gcd-many", help="compute a batch of A:B operand pairs")
    batch.add_argument("pairs", nargs="+", help="for example 48:18 0:7 0xffffffff:12")
    for operation in ("hash", "encrypt", "decrypt"):
        command = commands.add_parser(operation)
        data = command.add_mutually_exclusive_group()
        data.add_argument(
            "--data-hex", type=_hex, default=b"", help="input bytes, encoded as hexadecimal"
        )
        data.add_argument("--input", type=Path, help="read binary input from this file")
        command.add_argument(
            "--output", type=Path, help="write the verified result as binary bytes"
        )
        if operation != "hash":
            command.add_argument("--key-hex", type=_hex, required=True, help="16-byte key")
            command.add_argument("--nonce-hex", type=_hex, required=True, help="16-byte nonce")
            command.add_argument("--ad-hex", type=_hex, default=b"", help="associated data")
    check = commands.add_parser("selftest", help="check independent reference vectors on the board")
    check.add_argument("application", choices=("gcd", "ascon"))
    check.add_argument(
        "--variant", choices=("128", "128a"), default="128", help="must match the FPGA build"
    )
    return result


def selftest_ascon(client: AsconClient, variant_a: bool, timeout: float) -> dict:
    """Check both directions, hash, saved keys, long messages, and tag rejection."""
    checked = 0
    for vector in read_ascon_vectors():
        if vector["variant_a"] != variant_a:
            continue
        operation = vector["operation"]
        key = vector["key"] if vector["new_key"] else None
        expected = vector["expected"]
        if operation == "hash":
            actual = client.hash(vector["message"], timeout)
        elif operation == "encrypt":
            actual = client.encrypt(
                key, vector["nonce"], vector["associated_data"], vector["message"], timeout
            )
        else:
            actual = client.decrypt(
                key,
                vector["nonce"],
                vector["associated_data"],
                vector["message"] + expected[-16:],
                timeout,
            )
            expected = expected[:-16]
        if actual != expected:
            raise ValueError(f"independent Ascon reference mismatch at vector {checked}")
        checked += 1
    reference = HERE.parent.parent / "ascon/tb/AsconStreamLongVectors.vectors"
    long_vectors = json.loads(reference.read_text(encoding="utf-8"))
    case = next(item for item in long_vectors["cases"] if item["variant_a"] == variant_a)
    key, nonce, ad, message, ciphertext = (
        bytes.fromhex(case[field]) for field in ("key", "nonce", "ad", "message", "ciphertext_tag")
    )
    if client.encrypt(key, nonce, ad, message, timeout) != ciphertext:
        raise ValueError("independent long encryption reference mismatch")
    digest_case = long_vectors["hash"]
    if client.hash(bytes.fromhex(digest_case["message"]), timeout) != bytes.fromhex(
        digest_case["digest"]
    ):
        raise ValueError("independent long hash reference mismatch")
    if client.decrypt(None, nonce, ad, ciphertext, timeout) != message:
        raise ValueError("independent long decryption or key reuse mismatch")
    damaged = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    try:
        client.decrypt(None, nonce, ad, damaged, timeout)
    except AuthenticationError:
        pass
    else:
        raise ValueError("decryption accepted an incorrect authentication tag")
    return {
        "vectors": checked,
        "long_encrypt_decrypt": True,
        "long_hash": True,
        "key_reuse_after_hash": True,
        "bad_tag_rejected": True,
    }


def _execute(args, device: LinkDevice) -> dict:
    if args.command in ("gcd", "gcd-many") or (
        args.command == "selftest" and args.application == "gcd"
    ):
        client = GcdClient(device)
        client.reset(args.timeout)
        if args.command == "selftest":
            rng = random.Random(0x58454441)
            pairs = [(0, 0), (0, 7), (48, 18), (0xFFFFFFFF, 0xFFFFFFFF)]
            pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(1000)]
            actual = client.gcd_many(pairs, args.timeout)
            if actual != [math.gcd(a, b) for a, b in pairs]:
                raise ValueError("GCD batch does not match integer reference results")
            return {"checked": len(pairs)}
        if args.command == "gcd":
            return {"gcd": client.gcd(args.a, args.b, args.timeout)}
        pairs = []
        for pair in args.pairs:
            parts = pair.split(":")
            if len(parts) != 2:
                raise ValueError("each GCD pair must have the form A:B")
            pairs.append((_operand(parts[0]), _operand(parts[1])))
        return {"gcd": client.gcd_many(pairs, args.timeout)}
    client = AsconClient(device)
    client.reset(args.timeout)
    if args.command == "selftest":
        return selftest_ascon(client, args.variant == "128a", args.timeout)
    data = args.input.read_bytes() if args.input is not None else args.data_hex
    if args.command == "hash":
        output = client.hash(data, args.timeout)
        label = "digest"
    elif args.command == "encrypt":
        output = client.encrypt(args.key_hex, args.nonce_hex, args.ad_hex, data, args.timeout)
        label = "ciphertext_and_tag"
    else:
        output = client.decrypt(args.key_hex, args.nonce_hex, args.ad_hex, data, args.timeout)
        label = "plaintext"
    if args.output is not None:
        args.output.write_bytes(output)
    return {label: output.hex(), "bytes": len(output)}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    device = None
    backend = None
    try:
        if args.command == "list":
            print(json.dumps({"success": True, "ports": discover()}))
            return 0
        port = args.port
        if args.serial_number:
            matches = [
                item["port"] for item in discover() if item["serial_number"] == args.serial_number
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"serial number selected {len(matches)} ports; use an exact --port"
                )
            port = matches[0]
        if not port and not args.ftdi:
            raise ValueError(
                "select an exact --port, --serial-number, or --ftdi URL; use list first"
            )
        baud = (
            args.baud
            if args.baud is not None
            else (12_000_000 if args.board in ("arty", "basys3") else 3_000_000)
        )
        if baud <= 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("baud and timeout must be finite and positive")
        budget = (
            args.bridge_budget
            if args.bridge_budget is not None
            else {"arty": 2048, "basys3": 2048, "ulx3s": 512, "stlv7325": 1024}[args.board]
        )
        config = HostConfig(
            bridge_budget=budget,
            rsp_limit=(
                args.response_limit if args.response_limit is not None else min(255, budget - 76)
            ),
            byte_time=10 / baud,
        )
        backend = (
            FtdiBackend(args.ftdi, baudrate=baud, latency_ms=1, event_char=0)
            if args.ftdi
            else SerialBackend(port, baudrate=baud)
        )
        device = LinkDevice(backend, config)
        device.open(timeout=args.timeout)
        report = _execute(args, device)
        print(json.dumps({"success": True, "command": args.command, **report}))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
            )
        )
        return 1
    finally:
        if device is not None:
            device.close()
        elif backend is not None:
            backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
