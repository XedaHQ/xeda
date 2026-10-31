#!/usr/bin/env python3

import logging
import os
from pathlib import Path

from xeda import Design, Result, Spec, Workspace

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()

design = Design.load(SCRIPT_DIR / "sqrt.yaml")
workspace = Workspace()


def test_sqrt() -> None:
    LONG_TEST = [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]
    QUICK_TEST = [8, 32]
    ws = QUICK_TEST
    if os.environ.get("LONG_TEST"):
        print("running long test!")
        ws = LONG_TEST
    for w in ws:
        spec = Spec(
            "ghdl_sim",
            design.with_updates({"tb.parameters.G_IN_WIDTH": w}),
            settings={"ghdl_sim.env.NUM_TV": "50"},
        )
        if not workspace.run(spec).passed:
            raise AssertionError(f"test failed for w={w}")


def test_sqrt_yosys_synth() -> None:
    spec = Spec(
        "yosys_fpga",
        design.with_updates({"tb.parameters.G_IN_WIDTH": 32}),
        settings={
            "yosys_fpga.fpga.part": "LFE5U-25F-6BG381C",
            "yosys_fpga.clocks.main_clock.period": "10 ns",
        },
    )
    result: Result = workspace.run(spec)
    if result.error is not None and result.error.code == "executable_not_found":
        log.critical("Yosys executable not found")
    else:
        assert result.passed


if __name__ == "__main__":
    test_sqrt()
    test_sqrt_yosys_synth()
