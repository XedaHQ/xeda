"""Command-line Ascon client for the LeanLink streaming application."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from . import LinkDevice
from .applications import AsconClient, AuthenticationError, read_ascon_vectors
from . import cli_common


def add_commands(commands: argparse._SubParsersAction) -> None:
    for operation in ("hash", "encrypt", "decrypt"):
        command = commands.add_parser(operation)
        data = command.add_mutually_exclusive_group()
        data.add_argument(
            "--data-hex",
            type=cli_common.hex_bytes,
            default=b"",
            help="input bytes, encoded as hexadecimal",
        )
        data.add_argument("--input", type=Path, help="read binary input from this file")
        command.add_argument(
            "--output", type=Path, help="write the verified result as binary bytes"
        )
        if operation != "hash":
            command.add_argument(
                "--key-hex", type=cli_common.hex_bytes, required=True, help="16-byte key"
            )
            command.add_argument(
                "--nonce-hex", type=cli_common.hex_bytes, required=True, help="16-byte nonce"
            )
            command.add_argument(
                "--ad-hex", type=cli_common.hex_bytes, default=b"", help="associated data"
            )


def parser() -> argparse.ArgumentParser:
    result = cli_common.new_parser(__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list exact serial identities without opening a board")
    add_commands(commands)
    check = commands.add_parser("selftest", help="check independent reference vectors on the board")
    check.add_argument(
        "--variant", choices=("128", "128a"), default="128", help="must match the FPGA build"
    )
    return result


def long_vectors_path() -> Path:
    """Return the long-message fixture path without depending on the current directory."""
    repository_examples = Path(__file__).resolve().parents[3]
    return repository_examples / "ascon/tb/AsconStreamLongVectors.vectors"


def selftest_ascon(client: AsconClient, variant_a: bool, timeout: float) -> dict[str, object]:
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

    long_vectors = json.loads(long_vectors_path().read_text(encoding="utf-8"))
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


def execute(args: argparse.Namespace, device: LinkDevice) -> Mapping[str, object]:
    if args.command not in ("hash", "encrypt", "decrypt", "selftest"):
        raise ValueError(f"unsupported Ascon command: {args.command}")
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
    return cli_common.run_cli(args, execute)


if __name__ == "__main__":
    raise SystemExit(main())
