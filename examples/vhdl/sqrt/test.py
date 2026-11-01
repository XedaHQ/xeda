#!/usr/bin/env python3
"""Simulate the sqrt design at several input widths with GHDL, then synthesize it with Yosys.

Run it from any directory. Xeda writes the run directories to `./xeda_run`. By default, the
script simulates the widths 8 and 32. With `--long`, it simulates 12 widths. It prints one line
for each point and exits with 0 only if every point passes.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from xeda import Design, Spec, Workspace

HERE = Path(__file__).resolve().parent
QUICK_WIDTHS = [8, 32]
LONG_WIDTHS = [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--long", action="store_true", help="simulate 12 widths, not 2")
    args = parser.parse_args(argv)
    design = Design.load(HERE / "sqrt.yaml")
    widths = LONG_WIDTHS if args.long else QUICK_WIDTHS
    simulations = [
        Spec(
            "ghdl_sim",
            design.with_updates({"tb.parameters.G_IN_WIDTH": width}),
            settings={"ghdl_sim.env.NUM_TV": "50"},
        )
        for width in widths
    ]
    synthesis = Spec(
        "yosys_fpga",
        design,
        settings={
            "yosys_fpga.fpga.part": "LFE5U-25F-6BG381C",
            "yosys_fpga.clock.period": "10 ns",
        },
    )
    # The points differ in what a node does, so each run directory has the hash of its run.
    workspace = Workspace(hashed_run_dirs=True, tool_output="terminal")
    labels = [f"ghdl_sim, width {width}" for width in widths] + ["yosys_fpga"]
    failed = 0
    for label, point in zip(labels, workspace.sweep([*simulations, synthesis])):
        error = point.result.error
        print(
            f"{label:20} {point.result.outcome}"
            + (f": {error.code}, {error.message}" if error else "")
        )
        failed += not point.result.passed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
