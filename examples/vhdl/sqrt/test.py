#!/usr/bin/env python3

import logging
import os
from pathlib import Path

from xeda import Design, Flow
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, YosysFpga
from xeda.frozen import FrozenDict
from xeda.tool import ExecutableNotFound

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()

design = Design.from_file(SCRIPT_DIR / "sqrt.yaml")
xeda_runner = DefaultRunner()


def with_tb_parameters(**parameters: int) -> Design:
    """Return the design with these testbench parameters. A design never changes in place."""
    tb = design.tb.model_copy(
        update={"parameters": FrozenDict({**design.tb.parameters, **parameters})}
    )
    changed = design.model_copy(update={"tb": tb})
    changed.invalidate_cached_properties()
    return changed


def test_sqrt() -> None:
    LONG_TEST = [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]
    QUICK_TEST = [8, 32]
    ws = QUICK_TEST
    if os.environ.get("LONG_TEST"):
        print("running long test!")
        ws = LONG_TEST
    for w in ws:
        os.environ["NUM_TV"] = str(50)
        f: Flow = xeda_runner.run_flow(GhdlSim, with_tb_parameters(G_IN_WIDTH=w))
        if not f.succeeded:
            raise AssertionError(f"test failed for w={w}")


def test_sqrt_yosys_synth() -> None:
    try:
        f = xeda_runner.run_flow(
            YosysFpga,
            with_tb_parameters(G_IN_WIDTH=32),
            {"fpga": {"part": "LFE5U-25F-6BG381C"}, "clock": {"period": "10 ns"}},
        )
        assert f.succeeded
    except ExecutableNotFound:
        log.critical("Yosys executable not found")


if __name__ == "__main__":
    test_sqrt()
    test_sqrt_yosys_synth()
