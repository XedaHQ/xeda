"""Core-port tests on the raw top (rtl/LeanLinkTops.bsv): the test plays the core.

Run on the bare cores only (rtl/LeanLinkTops.bsv), with the targets `core_w8` and `core_w1` of
cosim.yaml.

What the test endpoint cannot show:
- every byte of a request, also of the maximum request, and the token flags;
- a non-last empty response token is a complete no-op (doc/protocol.md);
- `responseMax`, a zero-length response;
- OPEN: a request that the core started is served to its end, an unstarted one is abandoned.
"""

import random

import cocotb

import lean_tb as tb
from lean_tb import (
    LOG2W,
    MAXLEN,
    RAW,
    W,
    Bench,
    Host,
    RawCore,
    TestFailure,
    check_fields,
    mod,
    status_ok,
)

assert RAW, "run this module on a raw top (--src rtl/LeanLinkTops.bsv)"


async def setup(dut, rsp_limit=None, sid_seed=1):
    b = await Bench.create(dut, rx_gap=12, tx_gap=12)
    core = RawCore(b)
    h = Host(b, tb.mk_sid(sid_seed))
    await h.open(rsp_limit=rsp_limit)
    return b, core, h


def blob(rng, n):
    return bytes(0 if rng.random() < 0.15 else rng.randrange(1, 256) for _ in range(n))


async def one_request(b, core, h, payload, respond=None):
    """Send a request, let the core take it, and answer. Return the response frame."""
    n_before = len(core.req_tokens)
    core.take = True
    b.send(h.req(payload))
    seq = mod(h.seq - 1)
    await b.until(
        lambda: len(core.req_tokens) > n_before and core.req_tokens[-1][2] == 1,
        60000,
        "request tokens",
    )
    core.take = False
    core.respond(payload if respond is None else respond)
    fr = await h.collect(1)
    h.ack = mod(h.ack + 1)
    d = [f for f in fr if f.kind == "data"]
    assert len(d) == 1 and d[0].p["seq"] == seq, fr
    return d[0]


@cocotb.test()
async def every_request_byte_reaches_the_core(dut):
    """All sizes from 0 to the maximum: the core sees exactly the payload bytes, `last` on the
    final token only, one empty token for a zero-length request."""
    b, core, h = await setup(dut)
    rng = random.Random(21)
    sizes = list(range(0, MAXLEN - 12 + 1))
    for n in sizes:
        pay = blob(rng, n)
        k0 = len(core.req_tokens)
        f = await one_request(b, core, h, pay, respond=b"ok")
        toks = core.req_tokens[k0:]
        if n == 0:
            assert len(toks) == 1 and toks[0][2] == 1 and toks[0][3] == 1, toks
        else:
            assert len(toks) == n, (n, len(toks))
            assert bytes(t[1] for t in toks) == pay, n
            assert [t[2] for t in toks] == [0] * (n - 1) + [1], n
            assert all(t[3] == 0 for t in toks)
        assert f.p["payload"] == b"ok" and not f.p["trunc"]
    # the maximum request again with special contents (all zero, all 0xFF)
    for pat in (bytes(MAXLEN - 12), bytes([0xFF]) * (MAXLEN - 12)):
        k0 = len(core.req_tokens)
        await one_request(b, core, h, pat, respond=b"")
        assert bytes(t[1] for t in core.req_tokens[k0:]) == pat


@cocotb.test()
async def non_last_empty_token_is_a_no_op(dut):
    b, core, h = await setup(dut)
    # 1. with the core idle: the token is accepted and changes nothing
    snap = b.snapshot()
    core.out.append((0, False, True))
    await b.run(20)
    assert not core.out and len(core.put_log) == 1
    assert b.sig("link_wActive") == 0 and b.sig("link_wo") == 12 and b.sig("link_wTrunc") == 0
    assert b.snapshot() == snap
    # the core is idle in the sense of OPEN: a new OPEN completes at once, nothing waits for it
    h2 = Host(b, tb.mk_sid(2))
    b.sid = h2.sid
    b.frames()
    b.send(h2.open_wire())
    await b.until(lambda: bool(b._new), 4000, "OPEN reply (no wait for the core)")
    await b.settle()
    status_ok(h2.absorb(b.frames())[0], seq=0, exp=0, cursor=12)
    h = h2
    # 2. inside a response: empty tokens between the bytes change nothing
    core.take = True
    b.send(h.req(b"abc"))
    await b.until(lambda: len(core.req_tokens) >= 3, 20000, "request tokens")
    core.take = False
    toks = [
        (0, False, True),
        (0x11, False, False),
        (0, False, True),
        (0, False, True),
        (0x22, False, False),
        (0, False, True),
        (0x33, True, False),
    ]
    core.out.extend(toks)
    fr = await h.collect(1)
    d = [f for f in fr if f.kind == "data"][0]
    check_fields(d, payload=bytes([0x11, 0x22, 0x33]), trunc=False, len=15)
    h.ack = mod(h.ack + 1)


@cocotb.test()
async def non_last_empty_token_never_sets_trunc(dut):
    """responseMax = 2. Two bytes with empty tokens around them fit (no TRUNC). A third byte
    sets TRUNC. Empty tokens at the limit do not."""
    b, core, h = await setup(dut, rsp_limit=14)
    assert int(dut.responseMax.value) == 2
    core.take = True
    b.send(h.req(b"x"))
    await b.until(lambda: len(core.req_tokens) >= 1, 20000, "request token")
    core.take = False
    core.out.extend([(0, False, True), (7, False, False), (0, False, True), (8, True, False)])
    fr = await h.collect(1)
    d = [f for f in fr if f.kind == "data"][0]
    check_fields(d, payload=bytes([7, 8]), trunc=False, len=14)
    h.ack = mod(h.ack + 1)
    core.take = True
    b.send(h.req(b"y"))
    await b.until(lambda: len(core.req_tokens) >= 2, 20000, "request token")
    core.take = False
    core.out.extend(
        [(7, False, False), (8, False, False), (0, False, True), (0, False, True), (9, True, False)]
    )
    fr = await h.collect(1)
    d = [f for f in fr if f.kind == "data"][0]
    check_fields(d, payload=bytes([7, 8]), trunc=True, len=14)


@cocotb.test()
async def response_max_and_zero_length_response(dut):
    b = await Bench.create(dut, rx_gap=12, tx_gap=12)
    core = RawCore(b)
    for n, limit in enumerate((12, 13, 20, 100 if MAXLEN > 100 else 40, MAXLEN)):
        h = Host(b, tb.mk_sid(10 + n))
        await h.open(rsp_limit=limit)
        assert int(dut.responseMax.value) == limit - 12, (limit, int(dut.responseMax.value))
        # a zero-length response: one empty token with last: wire length 12, no TRUNC
        f = await one_request(b, core, h, b"req", respond=b"")
        check_fields(f, payload=b"", len=12, trunc=False)
        assert f.wire_len == 12
        # a response of limit - 12 + 1 bytes is cut and flagged
        too_long = bytes([5]) * (limit - 12 + 1)
        f = await one_request(b, core, h, b"req2", respond=too_long)
        check_fields(f, payload=too_long[: limit - 12], len=limit, trunc=True)
        # exactly responseMax bytes: no TRUNC
        if limit > 12:
            f = await one_request(b, core, h, b"req3", respond=too_long[:-1])
            check_fields(f, payload=too_long[:-1], len=limit, trunc=False)


@cocotb.test()
async def open_serves_a_started_request_and_abandons_an_unstarted_one(dut):
    """doc/protocol.md. The core takes the first token of request 1, then stops. OPEN arrives: the
    rest of the request stays available and the OPEN reply waits for the core. The core finishes
    and responds (the response is dropped). A request committed behind it is never offered."""
    b, core, h = await setup(dut)
    await one_request(b, core, h, b"warm", respond=b"w")
    pay = bytes(range(1, 31))
    k0 = len(core.req_tokens)
    core.take = True
    core.take_limit = k0 + 1
    b.send(h.req(pay))  # request 1
    if W > 1:
        b.send(h.req(b"behind"))  # request 2, unstarted
    await b.until(lambda: len(core.req_tokens) == k0 + 1, 20000, "first token")
    await b.run(1500)
    assert len(core.req_tokens) == k0 + 1
    h2 = Host(b, tb.mk_sid(3))
    b.send(h2.open_wire())
    await b.until(lambda: b.sig("link_openPending") == 1, 8000, "openPending")
    await b.run(4000)
    # no reply: the core is in the middle of a request
    assert not b.frames() and b.sig("link_sid") == h.sid
    assert int(dut.RDY_request_get.value) == 1, "the started request must stay available"
    # the core reads the rest of the started request and answers; OPEN completes
    core.take_limit = None
    await b.until(lambda: len(core.req_tokens) == k0 + len(pay), 20000, "rest of the request")
    assert bytes(t[1] for t in core.req_tokens[k0:]) == pay
    assert core.req_tokens[-1][2] == 1
    core.take = False
    core.respond(b"dropped")
    b.sid = h2.sid
    await b.until(lambda: bool(b._new), 20000, "OPEN reply")
    await b.run(2000)
    fr = h2.absorb(b.frames())
    assert len(fr) == 1 and fr[0].kind == "status", fr
    assert int(dut.RDY_request_get.value) == 0, "the unstarted request must not be offered"
    assert len(core.req_tokens) == k0 + len(pay), "an unstarted request was offered to the core"
    # the new session starts clean
    core.take = True
    h2.consumed = 12
    b.send(h2.req(b"new"))
    await b.until(
        lambda: core.req_tokens[-1][2] == 1 and len(core.req_tokens) > k0 + len(pay),
        20000,
        "request of the new session",
    )
    assert bytes(t[1] for t in core.req_tokens[k0 + len(pay) :]) == b"new"


@cocotb.test()
async def open_abandons_the_offered_request_before_the_first_token(dut):
    """The reader offers request 1 (RDY_request_get high) but the core does not take it. OPEN
    abandons it at once: RDY_request_get falls, the OPEN reply comes without the core."""
    b, core, h = await setup(dut)
    b.send(h.req(b"offered"))
    await b.until(lambda: int(dut.RDY_request_get.value) == 1, 10000, "request offered")
    assert not core.req_tokens
    h2 = Host(b, tb.mk_sid(4))
    b.sid = h2.sid
    b.frames()
    b.send(h2.open_wire())
    await b.until(lambda: bool(b._new), 8000, "OPEN reply")
    await b.run(500)
    assert int(dut.RDY_request_get.value) == 0 and not core.req_tokens
    fr = h2.absorb(b.frames())
    assert len(fr) == 1 and fr[0].kind == "status", fr
    s = b.snapshot()
    assert s["link_exp"] == 0 and s["link_cons"] == 0 and s["link_wr"] == 0, s


@cocotb.test(skip=W == 1)
async def longest_zero_free_response_frame(dut):
    """A DATA frame whose whole decoded content (header, payload, trailer) has no zero byte:
    MAXLEN - 2 = 253 bytes, one reverse-COBS run. Not possible at W1: with 2W = 2, seq and exp of a
    response cannot both be nonzero (seq 1 means exp 0), so the test is skipped there. The wire has
    one code byte at the end (decoded length + 1, 0xFE at W8) and no other special byte. Header
    fields are chosen nonzero: seq 1, exp 2, a cursor with two nonzero bytes, and a payload for
    which the CRC bytes are nonzero."""
    b, core, h = await setup(dut)
    lc = tb.lc
    await one_request(b, core, h, b"warm", respond=b"")  # response 0, a 12-byte frame: cursor 24
    rng = random.Random(9)
    n = MAXLEN - 12
    cursor = (h.consumed + MAXLEN) & 0xFFFF
    assert 256 <= cursor, cursor  # two nonzero cursor bytes
    seq = h.seq
    for _ in range(5000):
        pay = bytes(rng.randrange(1, 256) for _ in range(n))
        dec = lc.build_fpga_frame(
            lc.fpga_ctl(data=True, log2w=LOG2W), mod(seq), mod(seq + 1), cursor, pay, h.sid
        )
        if 0 not in dec:
            break
    else:
        raise TestFailure("no zero-free frame found")
    assert len(dec) == MAXLEN - 2
    # the core answers request `seq` with `pay`; exp is 'seq + 1' after the commit
    k0 = len(core.req_tokens)
    core.take = True
    b.send(h.req(b"zf"))
    await b.until(lambda: len(core.req_tokens) >= k0 + 2, 20000, "request tokens")
    core.take = False
    core.respond(pay)
    await b.until(lambda: bool(b._new), 60000, "response frame")
    await b.settle()
    f = [x for x in b.frames() if x.raw][-1]
    assert 0 not in f.raw
    assert f.raw[-1] == len(dec) + 1, f.raw[-1]  # the single reverse-COBS code byte
    assert f.raw[:-1] == dec, "payload bytes pass through unchanged"
    f.parse(h.sid)
    check_fields(f, payload=pay, seq=mod(seq), len=MAXLEN, trunc=False)


@cocotb.test(skip=W > 1)
async def trunc_bit_of_a_frame_on_the_wire_is_not_changed_by_a_response_dropped_in_open(dut):
    """W1, rspLimit 12. Responses 0 and 1 are held by the core. Response 0 is empty (no TRUNC).
    When TX starts it, an OPEN delimiter arrives within a few clocks (openPending), and then the
    core puts response 1 as one nonempty last token. With rspLimit = wo the writer computes TRUNC
    and `lenTbl.upd(pg(wr), ...)` runs for the dropped response. At W1 pg(wr) == pg(txq), so it
    writes the table entry of the frame that is on the wire. The control byte of that frame must
    still be 0x90 (TRUNC = 0). The offsets are swept: d = OPEN delimiter relative to the put of
    response 0, e = clocks between openPending and the put of response 1."""
    b = await Bench.create(dut, rx_gap=12, tx_gap=12)
    core = RawCore(b)
    bad = []
    seen = 0
    total = 0
    for d in range(
        0, 14
    ):  # d < 0 (OPEN before the put) would only show the mod-2 alias of cons and wr
        for e in range(0, 9):
            total += 1
            core.out.clear()
            core.req_tokens.clear()
            core.put_log.clear()
            core.take = False
            h = Host(b, tb.mk_sid(100 + d * 20 + e))
            await h.open(rsp_limit=12)
            b.frames()
            core.take = True
            b.send(h.req(b"r0"))
            await b.until(lambda: len(core.req_tokens) >= 2, 20000, "request 0 read")
            b.send(h.req(b"r1", ack=0))
            await b.until(lambda: len(core.req_tokens) >= 4, 20000, "request 1 read")
            core.take = False
            hn = Host(b, tb.mk_sid(5000 + d * 20 + e))
            ow = hn.open_wire()
            b.send(ow[:-1])  # OPEN without its delimiter
            await b.until(
                lambda: not b.rx_q and not b.rx_wait and b.sig("link_crcRun") == 0,
                5000,
                "OPEN body",
            )
            await b.run(20)
            t_put0 = b.clk + 1 + max(0, -d)
            t_del = b.clk + 1 + max(0, d)
            t_put1 = None
            done0 = done_del = False
            for _ in range(400):
                await b.tick()
                if not done0 and b.clk >= t_put0:
                    core.out.append((0, True, True))
                    done0 = True
                if not done_del and b.clk >= t_del:
                    b.rx_next = b.clk
                    b.send(b"\x00")
                    done_del = True
                if t_put1 is None and b.sig("link_openPending") == 1:
                    t_put1 = b.clk + e
                if t_put1 is not None and b.clk >= t_put1:
                    core.out.append((0x55, True, False))  # response 1: one nonempty last token
                    t_put1 = 10**12
            await b.run(3000)
            fr = b.frames()
            for f in fr:
                f.parse(h.sid)
            old = [f for f in fr if f.p is not None and f.kind == "data"]
            if old:
                seen += 1
                f = old[0]
                if f.p["ctl"] != 0x90 or f.p["trunc"] or f.p["payload"] != b"" or f.p["len"] != 12:
                    bad.append((d, e, hex(f.p["ctl"]), f.p["len"]))
    dut._log.info(
        "TRUNC sweep: %d offset pairs, response 0 on the wire in %d, changed control byte "
        "in %d: %s",
        total,
        seen,
        len(bad),
        bad[:8],
    )
    assert seen > 0, "response 0 never went out: the sweep did not reach the window"
    assert not bad, f"TRUNC of the frame on the wire was changed by a dropped response: {bad[:8]}"
