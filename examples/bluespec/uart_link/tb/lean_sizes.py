"""Sequence wrap and sizes (zero length, maximum, TRUNC)."""

import random

import cocotb

import lean_tb as tb
from lean_tb import (
    MAXLEN,
    SEQ_MOD,
    W,
    Bench,
    Host,
    check_fields,
    mod,
    resp_payload,
    run_bridge,
)

SID = 0x59E50101


async def open_session(dut, rx_gap=12, tx_gap=12, rsp_limit=None, sid=SID):
    b = await Bench.create(dut, rx_gap=rx_gap, tx_gap=tx_gap)
    h = Host(b, sid)
    await h.open(rsp_limit=rsp_limit)
    return b, h


def blob(rng, n, zeros=0.15):
    return bytes(0 if rng.random() < zeros else rng.randrange(1, 256) for _ in range(n))


# ---------------------------------------------------------------------------------------------
# Sequence wrap


@cocotb.test()
async def sequence_wraps_one_request_at_a_time(dut):
    """More than 4W requests in one session. Every response has the exact seq, exp and count."""
    b, h = await open_session(dut)
    rng = random.Random(9)
    n = 4 * W + 9
    for i in range(n):
        await h.transact(blob(rng, rng.randrange(0, 40) if W > 1 else rng.randrange(0, 30)))
        # transact checked seq = i mod 2W, exp = (i + 1) mod 2W, payload and the executed count
    assert h.count == n
    s = b.snapshot()
    assert s["link_exp"] == mod(n) and s["link_dack"] == mod(n - 1) and s["link_wr"] == mod(n), s


@cocotb.test()
async def sequence_wraps_in_a_full_pipeline(dut):
    """A pipeline with W requests in flight, for 12 * 2W + 5 requests (many wraps)."""
    n = 12 * SEQ_MOD + 5
    b, h, br, total = await run_bridge(
        dut, B=4000, read_gap=12, pause_prob=0.0, pause_range=(1, 2), seed=99, n=n, max_len=60
    )
    assert b.sig("link_exp") == mod(n) and b.sig("m_count") == n
    dut._log.info(
        "pipeline: %d requests in one session, %d wraps of the %d-value sequence space",
        n,
        n // SEQ_MOD,
        SEQ_MOD,
    )


@cocotb.test()
async def sequence_wraps_with_a_lagging_ack(dut):
    """The host acknowledges every responses in groups (ack lags by up to W - 1). The window stays
    full most of the time, and the counters wrap more than 4 times."""
    b, h = await open_session(dut)
    rng = random.Random(10)
    n = 5 * SEQ_MOD
    sent = 0
    rsp = 0
    pays = [blob(rng, rng.randrange(0, 30)) for _ in range(n)]
    acked = 0
    while rsp < n:
        # send as many requests as the window allows
        while sent < n and sent < acked + W:
            b.send(h.req(pays[sent], ack=mod(acked)))
            sent += 1
        fr = [f for f in await h.collect(1) if f.kind == "data"]
        for f in fr:
            check_fields(f, seq=mod(rsp), payload=resp_payload(rsp, pays[rsp]))
            rsp += 1
        # acknowledge only when W responses are outstanding or at the end
        if rsp - acked >= max(1, W - 1) or rsp == n:
            acked = rsp
            if rsp < n:
                b.send(h.hdr(ack=mod(acked)))
    assert b.sig("m_count") == n


# ---------------------------------------------------------------------------------------------
# Sizes


@cocotb.test()
async def zero_length_request(dut):
    """A request without payload: the response is only the 4 count bytes (wire length 16)."""
    b, h = await open_session(dut)
    for _ in range(6):
        f = await h.transact(b"")
        assert f.p["len"] == 16 and len(f.p["payload"]) == 4
    # pipelined zero-length requests
    n = min(W, 5)
    for i in range(n):
        b.send(h.req(b"", ack=h.ack))
    fr = [f for f in await h.collect(n) if f.kind == "data"]
    assert [f.p["payload"] for f in fr] == [(6 + i).to_bytes(4, "little") for i in range(n)]
    assert all(f.p["len"] == 16 for f in fr)


@cocotb.test()
async def every_request_size(dut):
    """Every payload size from 0 to the maximum, with zero bytes inside the data. The response is
    the count and the request, cut at responseMax (TRUNC for the largest sizes)."""
    b, h = await open_session(dut)
    rng = random.Random(1)
    max_req = MAXLEN - 12  # 243 at W = 8, 59 at W = 1
    lim = h.rsp_limit - 12
    n_trunc = 0
    for n in range(0, max_req + 1):
        pay = blob(rng, n)
        f = await h.transact(pay)  # checks payload (cut to responseMax) and the TRUNC flag
        want_trunc = 4 + n > lim
        n_trunc += want_trunc
        assert f.p["trunc"] == want_trunc and f.p["len"] == 12 + min(4 + n, lim), (n, f)
    assert n_trunc == max_req + 1 - (lim - 4 + 1), n_trunc  # sizes lim-3 .. max_req
    # special contents at the largest size that fits and at the maximum
    for size in (lim - 4, max_req):
        for pat in (
            bytes(size),
            bytes([0xFF]) * size,
            bytes([1]) * size,
            bytes([0, 255]) * (size // 2) + bytes(size % 2),
        ):
            await h.transact(pat)


@cocotb.test()
async def maximum_request_payload_truncates_the_response(dut):
    """The test endpoint answers count + request, so the maximum request (maxLen - 12 bytes)
    gives a response that is 4 bytes too long: TRUNC, wire length maxLen, the first
    responseMax bytes."""
    b, h = await open_session(dut)
    rng = random.Random(2)
    pay = blob(rng, MAXLEN - 12)
    f = await h.transact(pay)
    lim = MAXLEN - 12
    assert f.p["trunc"] is True and f.p["len"] == MAXLEN and len(f.p["payload"]) == lim
    assert f.p["payload"] == resp_payload(0, pay)[:lim]
    # one byte less fits only if the response is 4 + n <= lim
    f = await h.transact(blob(rng, lim - 4))
    assert f.p["trunc"] is False and f.p["len"] == MAXLEN
    f = await h.transact(blob(rng, lim - 3))
    assert f.p["trunc"] is True and f.p["len"] == MAXLEN
    # the request after a TRUNC response is not affected
    f = await h.transact(b"after")
    assert f.p["trunc"] is False and f.p["payload"] == resp_payload(h.count - 1, b"after")


@cocotb.test()
async def small_response_limits(dut):
    """TRUNC with a small rspLimit from OPEN. responseMax = rspLimit - 12."""
    b = await Bench.create(dut)
    count = 0
    for limit in (12, 13, 14, 16, 17, 20, 24):
        h = Host(b, tb.mk_sid(limit))
        h.count = count
        await h.open(rsp_limit=limit)
        lim = limit - 12
        for n in (0, 1, 2, 4, 5, 10, 30):
            f = await h.transact(bytes(range(1, n + 1)))
            full = 4 + n
            assert f.p["trunc"] == (full > lim), (limit, n, f.p)
            assert f.p["len"] == 12 + min(full, lim), (limit, n, f.p)
            assert len(f.p["payload"]) == min(full, lim)
        count = h.count
    # a limit of 12 gives an empty DATA frame (wire length 12) with TRUNC set
    h = Host(b, tb.mk_sid(99))
    h.count = count
    await h.open(rsp_limit=12)
    f = await h.transact(b"x")
    assert f.p["len"] == 12 and f.p["payload"] == b"" and f.p["trunc"] is True and f.wire_len == 12


@cocotb.test()
async def trunc_does_not_leak_to_the_next_response(dut):
    b, h = await open_session(dut, rsp_limit=20)  # responseMax = 8
    for n in (10, 0, 10, 2, 40, 3, 4, 5):
        f = await h.transact(bytes([n]) * n)
        assert f.p["trunc"] == (4 + n > 8), (n, f.p)
    # pipelined: TRUNC and fitting responses in the window
    sizes = [10, 0, 20, 1, 9, 2]
    sizes = sizes[: min(W, len(sizes))]
    for n in sizes:
        b.send(h.req(bytes([7]) * n, ack=h.ack))
    fr = [f for f in await h.collect(len(sizes)) if f.kind == "data"]
    assert [f.p["trunc"] for f in fr] == [4 + n > 8 for n in sizes], fr
