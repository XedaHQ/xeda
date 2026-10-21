"""Harness check: OPEN, one request, a short pipeline. Exact fields on every frame."""

import cocotb

from lean_tb import *  # noqa: F401,F403
from lean_tb import Bench, Host, check_fields, resp_payload, W

SID = 0x1234ABCD


@cocotb.test()
async def basic(dut):
    b = await Bench.create(dut)
    h = Host(b, SID)
    await h.open()
    b.send(h.req(b"hello"))
    fr = await h.collect(1)
    assert len(fr) == 1, fr
    check_fields(
        fr[0],
        kind="data",
        seq=0,
        exp=1,
        cursor=12 + 12 + 9,
        len=21,
        payload=resp_payload(0, b"hello"),
        trunc=False,
        ctl=0x90 | (3 if W == 8 else 0),
    )
    assert int(dut.executed.value) == 1
    # pipeline
    n = min(W, 6)
    pays = [bytes([i]) * i for i in range(n)]
    for p in pays:
        b.send(h.req(p, ack=1))
    fr = await h.collect(n)
    for i, f in enumerate(fr):
        check_fields(f, kind="data", seq=mod_(1 + i), payload=resp_payload(1 + i, pays[i]))
    assert int(dut.executed.value) == 1 + n


def mod_(x):
    return x % (2 * W)
