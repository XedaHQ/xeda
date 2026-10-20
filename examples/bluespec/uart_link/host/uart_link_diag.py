#!/usr/bin/env python3
"""Run ECHO or read link counters from an FPGA UART link."""

from __future__ import annotations

import argparse
import json

from uartlink import Device
from uartlink.backends import PyFtdiBackend, PySerialBackend


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--port", help="serial port, for example /dev/ttyUSB1 or COM5")
    backend.add_argument("--ftdi", help="pyftdi URL, for example ftdi://ftdi:2232h:<serial>/2")
    parser.add_argument("--baud", type=int, default=3_000_000)
    parser.add_argument("--bytes", type=int, default=64, dest="byte_count")
    parser.add_argument("--byte-budget", type=int, default=256, help="link byte budget (80..32767)")
    parser.add_argument("--window", type=int, default=8, help="request window (1..8)")
    parser.add_argument("--stats", action="store_true", help="print all 16 link counters")
    args = parser.parse_args()

    if args.byte_count < 0 or args.byte_count > 180:
        parser.error("--bytes must be in 0..180")
    if not 80 <= args.byte_budget <= 32767:
        parser.error("--byte-budget must be in 80..32767")
    if not 1 <= args.window <= 8:
        parser.error("--window must be in 1..8")
    transport = (
        PyFtdiBackend(args.ftdi, args.baud) if args.ftdi else PySerialBackend(args.port, args.baud)
    )
    device = Device(transport, byte_budget=args.byte_budget, window=args.window)
    try:
        device.open()
        if args.stats:
            print(json.dumps(device.statistics(), indent=2, sort_keys=True))
        else:
            payload = bytes(range(args.byte_count))
            echoed = device.echo(payload)
            if echoed != payload:
                raise RuntimeError("ECHO response differs from the request")
            print(f"ECHO passed for {len(payload)} bytes")
    finally:
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
