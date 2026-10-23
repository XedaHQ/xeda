"""Command-line GCD client for the LeanLink streaming application."""

from __future__ import annotations

import argparse
import math
import random
from collections.abc import Mapping

from . import LinkDevice
from .applications import GcdClient
from . import cli_common


def add_commands(commands: argparse._SubParsersAction) -> None:
    gcd = commands.add_parser("gcd", help="compute the GCD of two unsigned 32-bit integers")
    gcd.add_argument("a", type=cli_common.operand)
    gcd.add_argument("b", type=cli_common.operand)
    batch = commands.add_parser("gcd-many", help="compute a batch of A:B operand pairs")
    batch.add_argument("pairs", nargs="+", help="for example 48:18 0:7 0xffffffff:12")


def parser() -> argparse.ArgumentParser:
    result = cli_common.new_parser(__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list exact serial identities without opening a board")
    add_commands(commands)
    commands.add_parser("selftest", help="check independent GCD reference results on the board")
    return result


def _selftest(client: GcdClient, timeout: float) -> dict[str, int]:
    rng = random.Random(0x58454441)
    pairs = [(0, 0), (0, 7), (48, 18), (0xFFFFFFFF, 0xFFFFFFFF)]
    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(1000)]
    actual = client.gcd_many(pairs, timeout)
    if actual != [math.gcd(a, b) for a, b in pairs]:
        raise ValueError("GCD batch does not match integer reference results")
    return {"checked": len(pairs)}


def execute(args: argparse.Namespace, device: LinkDevice) -> Mapping[str, object]:
    if args.command not in ("gcd", "gcd-many", "selftest"):
        raise ValueError(f"unsupported GCD command: {args.command}")
    client = GcdClient(device)
    client.reset(args.timeout)
    if args.command == "selftest":
        return _selftest(client, args.timeout)
    if args.command == "gcd":
        return {"gcd": client.gcd(args.a, args.b, args.timeout)}

    pairs = []
    for pair in args.pairs:
        parts = pair.split(":")
        if len(parts) != 2:
            raise ValueError("each GCD pair must have the form A:B")
        pairs.append((cli_common.operand(parts[0]), cli_common.operand(parts[1])))
    return {"gcd": client.gcd_many(pairs, args.timeout)}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return cli_common.run_cli(args, execute)


if __name__ == "__main__":
    raise SystemExit(main())
