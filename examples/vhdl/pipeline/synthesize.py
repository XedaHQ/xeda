#!/usr/bin/env python3
"""Synthesize the pipelined adder with Quartus, in Docker unless `--no-docker` is given.

Run it from any directory. Xeda writes the run directories to `./xeda_run`. The script exits with
0 only if the run passes.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from xeda import Design, Spec, Workspace

HERE = Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-docker", action="store_true", help="run the Quartus on this machine, not in Docker"
    )
    args = parser.parse_args(argv)
    spec = Spec(
        "quartus",
        Design.load(HERE / "pipelined_adder.yaml"),
        settings={
            "quartus.docker": not args.no_docker,
            "quartus.fpga.part": "10CL016YU256C6G",
            "quartus.clock.period": "15 ns",
        },
    )
    result = Workspace(tool_output="terminal").run(spec)
    print(f"{spec.flow}: {result.outcome}")
    if result.error is not None:
        print(f"{result.error.code}: {result.error.message}", file=sys.stderr)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
