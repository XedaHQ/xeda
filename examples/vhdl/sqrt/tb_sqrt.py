import os
import random

import cocotb
from cocolight import DUT, DutClock, DutReset, ValidReadyTb, cocotest

NUM_TV = int(os.environ.get("NUM_TV", 2000))
DEBUG = bool(os.environ.get("DEBUG", False))


class SqrtTb(ValidReadyTb):
    def __init__(self, dut: DUT, debug: bool = DEBUG):
        super().__init__(dut, DutClock("clk"), DutReset("rst"), debug)
        self.in_bus = self.driver("in", data_suffix="data")
        self.out_bus = self.monitor("out", data_suffix=["data_root", "data_rem"])

    async def verify(self, rad: int):
        stimulus = cocotb.start_soon(self.in_bus.enqueue(rad))
        out = await self.out_bus.dequeue()
        await stimulus

        root = int(out.data_root)
        remainder = int(out.data_rem)

        assert rad == root**2 + remainder, f"{rad} !=  {root} ** 2 + {remainder}"
        assert rad < (root + 1) ** 2, "returned root was smaller than expected"

        self.log.debug("radicand=%d got root=%d remainder=%d", rad, root, remainder)


@cocotest()
async def test_sqrt_corners(dut: DUT, debug=False):
    tb = SqrtTb(dut, debug=debug)
    await tb.reset()
    tb.log.info("DUT: %s", str(dut))
    # get bound parameters/generics from the simulator
    G_IN_WIDTH = tb.get_int_value("G_IN_WIDTH")
    tb.log.info(f"G_IN_WIDTH:{G_IN_WIDTH}")
    # await tb.clock_edge
    # TODO make sure ranges are within G_IN_WIDTH bits
    assert G_IN_WIDTH and G_IN_WIDTH >= 6
    testcases = list(range(30)) + list(range(2**G_IN_WIDTH - 70, 2**G_IN_WIDTH))
    testcases += list(range(2**G_IN_WIDTH // 2 - 30, 2**G_IN_WIDTH // 2 + 30))
    for rad in testcases:
        await tb.verify(rad)


@cocotest
async def test_sqrt(dut: DUT, num_tests: int = NUM_TV, debug=DEBUG):
    tb = SqrtTb(dut, debug=debug)
    await tb.reset()
    # get bound parameters/generics from the simulator
    G_IN_WIDTH = tb.get_value("G_IN_WIDTH", int)
    tb.log.info("G_IN_WIDTH:%d num_tests:%d", G_IN_WIDTH, num_tests)
    # await tb.clock_edge
    testcases = [random.getrandbits(G_IN_WIDTH) for _ in range(num_tests)]
    testcases = list(set(testcases))
    for rad in testcases:
        await tb.verify(rad)
