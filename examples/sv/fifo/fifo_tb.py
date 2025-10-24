from logging import Logger
import random
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, First, ReadOnly, ReadWrite

from cocotb_coverage.coverage import CoverPoint, coverage_db


@CoverPoint(
    "fifo.enq_data", xf=lambda dut: int(dut.enq_data.value), bins=list(range(0, 256)), at_least=1
)
@CoverPoint(
    "fifo.deq_data", xf=lambda dut: int(dut.deq_data.value), bins=list(range(0, 256)), at_least=1
)
# coverage of full and empty conditions
@CoverPoint("fifo.full", xf=lambda dut: int(not dut.enq_ready.value), bins=[0, 1], at_least=1)
@CoverPoint("fifo.empty", xf=lambda dut: int(not dut.deq_valid.value), bins=[0, 1], at_least=1)
# coverage of enq when full and deq when empty
@CoverPoint(
    "fifo.enq_when_full",
    xf=lambda dut: int(dut.enq_valid.value and not dut.enq_ready.value),
    bins=[0, 1],
    at_least=1,
)
@CoverPoint(
    "fifo.deq_when_empty",
    xf=lambda dut: int(dut.deq_ready.value and not dut.deq_valid.value),
    bins=[0, 1],
    at_least=1,
)
def fifo_coverage(dut):
    pass


async def reset_dut(dut, cycles=5):
    dut.reset.value = 1
    # Initialize driven signals
    dut.enq_valid.value = 0
    dut.enq_data.value = 0
    dut.deq_ready.value = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    dut.reset.value = 0
    await RisingEdge(dut.clk)


async def push_one(dut, data, timeout_cycles=100_000):
    dut.enq_data.value = data
    dut.enq_valid.value = 1
    for _ in range(timeout_cycles):
        fifo_coverage(dut)
        await RisingEdge(dut.clk)
        if dut.enq_ready.value:
            break
    else:
        assert False, "Timeout waiting for enq_ready"
    dut.enq_valid.value = 0


async def pop_one(dut, timeout_cycles=1000):
    # Wait until data is available (valid)
    dut.deq_ready.value = 1
    for _ in range(timeout_cycles):
        await ReadOnly()  # Ensure sampled after updates
        fifo_coverage(dut)
        if dut.deq_valid.value:
            break
        await RisingEdge(dut.clk)
    else:
        assert False, "Timeout waiting for deq_valid"
    data = int(dut.deq_data.value)
    await RisingEdge(dut.clk)
    dut.deq_ready.value = 0
    return data


@cocotb.test()
async def test_basic_functionality(dut):
    # Create and start clock
    clk = Clock(dut.clk, 10)
    clk.start()
    await reset_dut(dut)

    test_data = [0xA5, 0x5A, 0x3C, 0xC3, 0x00, 0xFF, 0x12, 0x34]
    for d in test_data:
        await push_one(dut, d)

    for exp in test_data:
        got = await pop_one(dut)
        assert got == exp, f"Mismatch: expected 0x{exp:02X}, got 0x{got:02X}"


@cocotb.test()
async def test_randomized_with_backpressure(dut):
    clk = Clock(dut.clk, 5, unit="ns")
    cocotb.start_soon(clk.start())

    logger: Logger = dut._log

    await reset_dut(dut)

    # Randomized traffic
    # rng = random.Random(0xC0C01)
    rng = random.Random()
    try:
        depth = 1 << int(dut.LOG2_DEPTH.value)  # Access parameter via hierarchy if supported
        logger.info(f"* Detected FIFO depth of {depth}")
    # Fallback if parameter not visible
    except Exception:
        depth = 16
        logger.warning(f"* Assuming FIFO depth of {depth}")

    exp_q = deque()
    cycles = 10000

    async def writer():
        for _ in range(cycles):
            # Randomly decide to try to write
            if rng.random() < 0.6:
                data = rng.randrange(0, 256)
                exp_q.append(data)
                await push_one(dut, data)
            else:
                # Idle cycle
                await RisingEdge(dut.clk)

    async def reader():
        for _ in range(cycles + 200):
            # Random backpressure
            if rng.random() < 0.62 and exp_q:
                got = await pop_one(dut)
                exp = exp_q.popleft()
                assert got == exp, f"Mismatch: expected 0x{exp:02X}, got 0x{got:02X}"
            else:
                # Idle cycle
                await RisingEdge(dut.clk)

    # Run concurrently with a timeout
    writer_task = cocotb.start_soon(writer())
    reader_task = cocotb.start_soon(reader())

    # Global timeout
    await First(writer_task, Timer(clk.period * 1_000_000, unit=clk.unit))
    await reader_task

    # Drain remaining expected data
    while exp_q:
        # Ensure DUT still has data if scoreboard says so
        if not bool(dut.deq_valid.value):
            # Allow valid to propagate
            for _ in range(10):
                await RisingEdge(dut.clk)
                if bool(dut.deq_valid.value):
                    break
        got = await pop_one(dut)
        exp = exp_q.popleft()
        assert got == exp, f"Mismatch at drain: expected 0x{exp:02X}, got 0x{got:02X}"

    coverage_db.report_coverage(logger.info, bins=False)


@cocotb.test()
async def test_fill_and_drain(dut):
    clk = Clock(dut.clk, 8, unit="ns")
    cocotb.start_soon(clk.start())

    await reset_dut(dut)

    # Estimate depth from LOG2_DEPTH if accessible, else assume 16
    try:
        log2_depth = int(dut.dut.LOG2_DEPTH)
        print(f"Detected LOG2_DEPTH={log2_depth}")
        depth = 1 << log2_depth
    except Exception:
        depth = 16

    # Fill until enq_ready deasserts or we hit 'depth'
    filled = 0
    for i in range(depth * 2):  # guard
        dut.enq_data.value = i & 0xFF
        dut.enq_valid.value = 1
        await RisingEdge(dut.clk)
        if dut.enq_ready.value:
            filled += 1
        else:
            # FIFO is full (no more writes accepted)
            break

    dut.enq_valid.value = 0
    assert filled > 0, "No writes were accepted"
    assert filled == depth, f"Expected to fill {depth} entries, but only filled {filled}"
    assert not bool(
        dut.enq_ready.value
    ), f"Expected FIFO to be full (enq_ready=={dut.enq_ready.value})"

    # Drain all entries
    drained = 0
    while True:
        dut.deq_ready.value = 1
        # If no data, break when valid stays low for a couple cycles
        idle = 0
        while idle < 4:
            await ReadOnly()
            if bool(dut.deq_valid.value):
                break
            idle += 1
            await RisingEdge(dut.clk)
        else:
            break

        # Pop one if available
        await RisingEdge(dut.clk)
        drained += 1
        dut.deq_ready.value = 0

    assert drained == filled, f"Drained {drained} entries, expected {filled}"
