#!/usr/bin/env python3
"""Use one command to access a board running the GCD or Ascon UART application.

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
from leanlink import ascon_cli, cli_common, gcd_cli  # noqa: E402

__all__ = [
    "AsconClient",
    "AuthenticationError",
    "BackendUnavailable",
    "FtdiBackend",
    "GcdClient",
    "HostConfig",
    "LinkDevice",
    "SerialBackend",
    "_execute",
    "_hex",
    "_operand",
    "discover",
    "main",
    "parser",
    "read_ascon_vectors",
    "selftest_ascon",
]

_hex = cli_common.hex_bytes
_operand = cli_common.operand


def discover() -> list[dict]:
    """List serial identities without opening ports or selecting a board by guesswork."""
    return cli_common.discover()


def parser() -> argparse.ArgumentParser:
    result = cli_common.new_parser(__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list exact serial identities without opening a board")
    gcd_cli.add_commands(commands)
    ascon_cli.add_commands(commands)
    check = commands.add_parser("selftest", help="check independent reference vectors on the board")
    check.add_argument("application", choices=("gcd", "ascon"))
    check.add_argument(
        "--variant", choices=("128", "128a"), default="128", help="must match the FPGA build"
    )
    return result


def selftest_ascon(client: AsconClient, variant_a: bool, timeout: float) -> dict[str, object]:
    """Retain the combined CLI's Ascon self-test helper."""
    return ascon_cli.selftest_ascon(client, variant_a, timeout)


def _execute(args: argparse.Namespace, device: LinkDevice) -> dict[str, object]:
    if args.command in ("gcd", "gcd-many"):
        return dict(gcd_cli.execute(args, device))
    if args.command in ("hash", "encrypt", "decrypt"):
        return dict(ascon_cli.execute(args, device))
    if args.command == "selftest":
        if args.application == "gcd":
            return dict(gcd_cli.execute(args, device))
        if args.application == "ascon":
            return dict(ascon_cli.execute(args, device))
        raise ValueError(f"unsupported application: {args.application}")
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return cli_common.run_cli(
        args,
        _execute,
        serial_backend=SerialBackend,
        ftdi_backend=FtdiBackend,
        device_type=LinkDevice,
        discover_fn=discover,
    )


if __name__ == "__main__":
    raise SystemExit(main())
