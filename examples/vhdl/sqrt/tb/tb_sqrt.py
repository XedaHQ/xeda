import os
import cocotb
import random
from box import Box
from cocolight import InPort, OutPort, TB

in_bus = InPort(data_signal="radicand")
out_bus = OutPort(data_signal=["root", "root_remainder"])


class SqrtTb(TB):
    async def verify(self, rad: int):
        await self.send_input(in_bus, rad)
        out = await self.receive_output(out_bus)
        # print(out)
        assert isinstance(out, Box)
        root = int(out.root)
        remainder = int(out.root_remainder)

        assert rad == root**2 + remainder, f"{rad} !=  {root} ** 2 + {remainder}"
        assert rad < (root + 1) ** 2, f"root was too small!"


@cocotb.test
async def test_sqrt_corners(dut):
    tb = SqrtTb(dut, in_bus, out_bus)
    await tb.reset()
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


NUM_TV = int(os.environ.get("NUM_TV", 2000))


@cocotb.test
async def test_sqrt(dut, num_tests: int = NUM_TV):
    tb = SqrtTb(dut, in_bus, out_bus)
    await tb.reset()
    # get bound parameters/generics from the simulator
    G_IN_WIDTH = tb.get_int_value("G_IN_WIDTH")
    assert G_IN_WIDTH
    print(f"G_IN_WIDTH:{G_IN_WIDTH}")
    # await tb.clock_edge
    testcases = [random.getrandbits(G_IN_WIDTH) for _ in range(num_tests)]
    testcases = list(set(testcases))
    for rad in testcases:
        await tb.verify(rad)
