"""Shared command-line support for the LeanLink application examples."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping
from typing import Any

from . import BackendUnavailable, FtdiBackend, HostConfig, LinkDevice, SerialBackend

BOARD_BAUD = {"arty": 12_000_000, "basys3": 12_000_000, "ulx3s": 3_000_000, "stlv7325": 3_000_000}
BOARD_BUDGET = {"arty": 2048, "basys3": 2048, "ulx3s": 512, "stlv7325": 1024}
BOARDS = tuple(BOARD_BAUD)


def hex_bytes(value: str) -> bytes:
    """Parse hexadecimal command-line data."""
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an even number of hexadecimal digits") from exc


def operand(value: str) -> int:
    """Parse an unsigned 32-bit integer, allowing Python's 0x prefix."""
    try:
        number = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected an integer, optionally with a 0x prefix"
        ) from exc
    if not 0 <= number <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("operand must be in 0..0xffffffff")
    return number


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--port", help="exact serial device path from the list command")
    connection.add_argument("--serial-number", help="select exactly one enumerated serial number")
    connection.add_argument("--ftdi", help="exact pyftdi URL, including serial number and channel")
    parser.add_argument("--board", choices=BOARDS, default="arty")
    parser.add_argument(
        "--baud", type=int, help="UART rate; default 12 Mbaud on Arty/Basys-3, otherwise 3 Mbaud"
    )
    parser.add_argument(
        "--bridge-budget", type=int, help="bytes in flight; default 2048/512/1024 by board"
    )
    parser.add_argument(
        "--response-limit",
        type=int,
        help="response wire bytes; default min(255, bridge budget - 76)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="seconds per operation, across all exchanges"
    )


def new_parser(description: str | None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_connection_options(parser)
    return parser


def discover() -> list[dict[str, Any]]:
    """List serial identities without opening ports or selecting a board by guesswork."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise BackendUnavailable("serial discovery needs pyserial") from exc
    return [
        {
            "port": port.device,
            "serial_number": port.serial_number,
            "vendor_id": port.vid,
            "product_id": port.pid,
            "description": port.description,
            "location": port.location,
            "hardware_id": port.hwid,
        }
        for port in sorted(list_ports.comports(), key=lambda item: item.device)
    ]


def open_device(
    args: argparse.Namespace,
    *,
    serial_backend: type | None = None,
    ftdi_backend: type | None = None,
    device_type: type | None = None,
    discover_fn: Callable[[], list[dict[str, Any]]] | None = None,
) -> LinkDevice:
    """Validate settings, open one transport session, and close it if OPEN fails."""
    serial_backend = SerialBackend if serial_backend is None else serial_backend
    ftdi_backend = FtdiBackend if ftdi_backend is None else ftdi_backend
    device_type = LinkDevice if device_type is None else device_type
    discover_fn = discover if discover_fn is None else discover_fn

    port = args.port
    if args.serial_number:
        matches = [
            item["port"] for item in discover_fn() if item["serial_number"] == args.serial_number
        ]
        if len(matches) != 1:
            raise ValueError(f"serial number selected {len(matches)} ports; use an exact --port")
        port = matches[0]
    if not port and not args.ftdi:
        raise ValueError("select an exact --port, --serial-number, or --ftdi URL; use list first")

    baud = args.baud if args.baud is not None else BOARD_BAUD[args.board]
    if baud <= 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("baud and timeout must be finite and positive")
    budget = args.bridge_budget if args.bridge_budget is not None else BOARD_BUDGET[args.board]
    config = HostConfig(
        bridge_budget=budget,
        rsp_limit=args.response_limit if args.response_limit is not None else min(255, budget - 76),
        byte_time=10 / baud,
    )

    backend = None
    device = None
    try:
        backend = (
            ftdi_backend(args.ftdi, baudrate=baud, latency_ms=1, event_char=0)
            if args.ftdi
            else serial_backend(port, baudrate=baud)
        )
        device = device_type(backend, config)
        device.open(timeout=args.timeout)
        return device
    except BaseException:
        resource = device if device is not None else backend
        if resource is not None:
            try:
                resource.close()
            except Exception:
                pass
        raise


def emit_success(command: str, fields: Mapping[str, object]) -> None:
    print(json.dumps({"success": True, "command": command, **fields}))


def emit_error(exc: Exception) -> None:
    print(
        json.dumps({"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
    )


def run_cli(
    args: argparse.Namespace,
    execute: Callable[[argparse.Namespace, LinkDevice], Mapping[str, object]],
    *,
    serial_backend: type | None = None,
    ftdi_backend: type | None = None,
    device_type: type | None = None,
    discover_fn: Callable[[], list[dict[str, Any]]] | None = None,
) -> int:
    device = None
    try:
        if args.command == "list":
            print(
                json.dumps(
                    {"success": True, "ports": (discover if discover_fn is None else discover_fn)()}
                )
            )
            return 0
        device = open_device(
            args,
            serial_backend=serial_backend,
            ftdi_backend=ftdi_backend,
            device_type=device_type,
            discover_fn=discover_fn,
        )
        emit_success(args.command, execute(args, device))
        return 0
    except Exception as exc:
        emit_error(exc)
        return 1
    finally:
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                # Report cleanup failures without replacing the command's JSON result.
                if sys.stderr is not None:
                    try:
                        print(f"warning: device cleanup failed: {exc}", file=sys.stderr)
                    except Exception:
                        pass
