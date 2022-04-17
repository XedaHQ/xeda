#!/usr/bin/env python3

import logging
import os
from pathlib import Path

from xeda import Design, Flow
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Yosys
from xeda.tool import ExecutableNotFound

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()

design = Design.from_toml(SCRIPT_DIR / "sqrt.toml")
xeda_runner = DefaultRunner()


def test_sqrt() -> None:
    LONG_TEST = [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]
    QUICK_TEST = [8, 32]
    ws = QUICK_TEST
    if os.environ.get("LONG_TEST"):
        print("runing long test!")
        ws = LONG_TEST
    for w in ws:
        assert design.tb
        design.tb.parameters = {**design.tb.parameters, "G_IN_WIDTH": w}
        os.environ["NUM_TV"] = str(100)
        f: Flow = xeda_runner.run_flow(GhdlSim, design)
        if not f.succeeded:
            raise AssertionError(f"test failed for w={w}")


def test_sqrt_yosys_synth() -> None:
    design.tb.parameters["G_IN_WIDTH"] = 32
    try:
        f = xeda_runner.run_flow(
            Yosys, design, {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock_period": 10.0}
        )
        assert f.succeeded
    except ExecutableNotFound:
        log.critical("Yosys executable not found")


if __name__ == "__main__":
    test_sqrt()
    test_sqrt_yosys_synth()
