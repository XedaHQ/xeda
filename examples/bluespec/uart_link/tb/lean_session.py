"""OPEN, the session reset, and the UNBOUND state.

UNBOUND: after reset, a frame of an unknown session gets exactly one status frame valid
under S = 0 (the boot grant pays for exactly one 12-byte frame), and no more until OPEN.

OPEN: exact reply; a second OPEN resets the window; frames of the old session are
ignored; an OPEN that arrives while the endpoint is in the middle of a request waits until the
endpoint finishes, discards the requests the endpoint did not start, and then replies.
"""

import random

import cocotb

import lean_tb as tb
from lean_tb import (
    LOG2W,
    MAXLEN,
    W,
    Bench,
    Host,
    check_fields,
    mod,
    resp_payload,
    status_ok,
)

SID_A = 0xA0A1A2A3
SID_B = 0xB0B1B2B3


# ---------------------------------------------------------------------------------------------
# UNBOUND


@cocotb.test()
async def unbound_one_status_frame_then_silence(dut):
    b = await Bench.create(dut)
    s = b.snapshot()
    assert s["link_bound"] == 0 and s["link_grant"] == 0xFFCC and s["link_cursor"] == 0, s
    h = Host(b, SID_A)
    b.sid = SID_A
    # a frame of an unknown session: valid in format, wrong session
    b.send(h.req(b"hello", advance=False))
    await b.settle()
    await b.run(400)
    fr = b.frames()
    assert len(fr) == 1, fr
    f = fr[0]
    assert f.kind == "unbound", f
    assert f.p0 is not None and f.p is None
    # valid under S = 0: exact fields
    d = f.p0
    exp = dict(
        kind="status",
        ctl=0x80 | LOG2W,
        seq=0,
        exp=0,
        cursor=12,
        len=MAXLEN,
        payload=b"",
        trunc=False,
        log2w=LOG2W,
        capability=MAXLEN,
        data=False,
    )
    assert d == exp, (d, exp)
    assert f.wire_len == 12
    s = b.snapshot()
    assert s["link_cursor"] == 12 and s["m_count"] == 0 and s["link_exp"] == 0, s
    # more frames of unknown sessions of every kind: no further frame (the credit is used up)
    n0 = len(b.tx_bytes)
    junk = [
        h.req(b"x", advance=False),
        h.hdr(poll=True),
        h.hdr(),
        h.req(b"", advance=False),
        h.req(b"y", advance=False, sid=0),  # valid under S = 0 but the FPGA is unbound
        h.hdr(poll=True, sid=0),
        bytes([5, 5, 5, 5, 0]),
        bytes([1, 0]),
        h.open_wire(sid=SID_B, rsp_limit=5),
    ]  # a bad OPEN
    for j in junk:
        b.send(j)
        await b.settle()
        await b.run(300)
    assert len(b.tx_bytes) == n0, b.peek_frames()
    s = b.snapshot()
    assert s["m_count"] == 0 and s["link_exp"] == 0 and s["link_bound"] == 0, s
    # OPEN gives the OPEN reply, valid under the new session
    h2 = Host(b, SID_B)
    fr = await h2.open()
    assert fr.p0 is None
    s = b.snapshot()
    assert s["link_bound"] == 1 and s["link_sid"] == SID_B and s["link_cursor"] == 12, s
    await h2.transact(b"first")


@cocotb.test()
async def unbound_s0_frames_do_not_execute(dut):
    """A frame that is fully valid under S = 0 is still not executed while unbound."""
    b = await Bench.create(dut)
    h = Host(b, 0)
    b.sid = SID_A
    b.send(h.req(b"must not run", advance=False))
    await b.settle()
    await b.run(400)
    fr = b.frames()
    assert len(fr) == 1 and fr[0].kind == "unbound", fr
    s = b.snapshot()
    assert s["m_count"] == 0 and s["link_exp"] == 0 and s["link_bound"] == 0, s


@cocotb.test()
async def unbound_burst_gives_one_frame(dut):
    """Three bad frames in a row (the first reply is still waiting): still one status frame."""
    b = await Bench.create(dut, rx_gap=10)
    b.sid = SID_A
    h = Host(b, SID_A)
    for _ in range(3):
        b.send(h.req(b"z", advance=False))
    await b.settle()
    await b.run(500)
    fr = b.frames()
    assert len(fr) == 1 and fr[0].kind == "unbound", fr


@cocotb.test()
async def unbound_open_with_tiny_grant(dut):
    """The boot grant, then an OPEN. The OPEN reply needs credit like every status frame."""
    b = await Bench.create(dut)
    h = Host(b, SID_A)
    b.sid = SID_A
    # no credit for the OPEN reply: grant = -60 (needs >= -52 after the 12 reply bytes)
    b.send(h.open_wire(grant=(-60) & 0xFFFF))
    await b.settle()
    await b.run(500)
    assert not b.frames(), "OPEN reply sent without credit"
    s = b.snapshot()
    assert s["link_bound"] == 1 and s["link_owe"] == 1, s
    # a header-only frame with credit releases the reply
    b.send(h.hdr(grant=0x1000, ack=0, seq=0))
    await b.settle()
    fr = h.absorb(b.frames())
    assert len(fr) == 1, fr
    status_ok(fr[0], seq=0, exp=0, cursor=12)


# ---------------------------------------------------------------------------------------------
# OPEN


@cocotb.test()
async def open_reply_is_exact(dut):
    b = await Bench.create(dut)
    count = 0
    for rsp_limit in sorted({MAXLEN, 12, 20, 100 if MAXLEN >= 100 else 40}):
        h = Host(b, tb.mk_sid(rsp_limit))
        h.count = count
        fr = await h.open(rsp_limit=rsp_limit, grant=0x0ABC)
        # open() checked kind, seq 0, exp 0, cursor 12, ctl, len = maxLen, wire length 12
        assert fr.p0 is None
        s = b.snapshot()
        want = dict(
            link_bound=1,
            link_sid=tb.mk_sid(rsp_limit),
            link_rspLimit=rsp_limit,
            link_grant=0x0ABC,
            link_exp=0,
            link_dack=0,
            link_cons=0,
            link_wr=0,
            link_txq=0,
            link_hi=0,
            link_cursor=12,
            link_openPending=0,
            link_owe=0,
        )
        got = {k: s[k] for k in want}
        assert got == want, (got, want)
        # the session works with the new limit
        h.consumed = 12
        h.B = 4096
        await h.transact(b"ok")
        await h.transact(b"")
        count = h.count


@cocotb.test()
async def open_clamps_a_response_limit_above_maxlen(dut):
    """An OPEN with rspLimit above maxLen is valid; the FPGA uses maxLen. Only
    rspLimit below 12 is invalid. (At W = 8, maxLen is 255 and the limit cannot exceed it.)"""
    b = await Bench.create(dut)
    count = 0
    for n, lim in enumerate((255, 200, 128, min(MAXLEN + 1, 255), MAXLEN)):
        h = Host(b, tb.mk_sid(20 + n))
        h.count = count
        await h.open(rsp_limit=lim)
        s = b.snapshot()
        assert s["link_rspLimit"] == min(lim, MAXLEN) and s["link_bound"] == 1, (lim, s)
        # the response is cut at the clamped limit
        top = min(lim, MAXLEN) - 12
        f = await h.transact(bytes([9]) * (top - 4))  # fits exactly
        check_fields(f, trunc=False, len=min(lim, MAXLEN))
        f = await h.transact(bytes([9]) * (top - 3))  # one byte too long
        check_fields(f, trunc=True, len=min(lim, MAXLEN))
        count = h.count


@cocotb.test()
async def second_open_resets_the_window(dut):
    b = await Bench.create(dut)
    hA = Host(b, SID_A)
    await hA.open()
    for i in range(3):
        await hA.transact(bytes([i + 1]) * i)
    # more requests; the host does not acknowledge their responses (two, or one when W = 1)
    m = min(2, W)
    for i in range(m):
        b.send(hA.req(b"more%d" % i, ack=hA.ack))
    fr = await hA.collect(m)
    assert [f.p["seq"] for f in fr if f.kind == "data"] == [mod(3 + i) for i in range(m)], fr
    hA.count += m
    s = b.snapshot()
    assert s["link_dack"] == mod(3) and s["link_wr"] == mod(3 + m) and s["link_hi"] == mod(3 + m), s
    assert s["m_count"] == 3 + m
    # OPEN with a new session: everything restarts, the endpoint keeps its count
    hB = Host(b, SID_B)
    hB.count = hA.count
    await hB.open(grant=0x0777)
    s = b.snapshot()
    for k in ("link_exp", "link_dack", "link_cons", "link_wr", "link_txq", "link_hi"):
        assert s[k] == 0, (k, s)
    assert s["link_sid"] == SID_B and s["link_cursor"] == 12 and s["link_grant"] == 0x0777, s
    # frames of the old session are ignored: no reply, no state change
    snap = b.snapshot()
    n0 = len(b.tx_bytes)
    old = [
        hA.req(b"old", seq=0, ack=0),
        hA.hdr(poll=True, seq=0, ack=0),
        hA.req(b"old2", seq=1, ack=0),
    ]
    for w in old:
        b.send(w)
        await b.settle()
        await b.run(300)
    assert len(b.tx_bytes) == n0 and b.snapshot() == snap, (b.peek_frames(), snap, b.snapshot())
    # the new session starts at sequence 0 with the count of the endpoint
    hB.consumed = 12
    f = await hB.transact(b"new session")
    check_fields(f, seq=0, payload=resp_payload(3 + m, b"new session"))
    # OPEN again with the same ID resets too
    await hB.transact(b"two")
    await hB.open()
    s = b.snapshot()
    assert s["link_exp"] == 0 and s["link_dack"] == 0 and s["link_cursor"] == 12, s
    f = await hB.transact(b"after reopen")
    check_fields(f, seq=0, payload=resp_payload(3 + m + 2, b"after reopen"))


@cocotb.test()
async def open_discards_unsent_responses_and_waits_for_the_frame_in_flight(dut):
    """The PHY holds back FPGA bytes, so response frames wait. A frame in flight finishes under
    the old session. Unsent responses are discarded. The OPEN reply is the next frame."""
    b = await Bench.create(dut)
    hA = Host(b, SID_A)
    await hA.open()
    await hA.transact(b"warm")
    b.tx_hold = True
    n = min(3, W)
    for i in range(n):
        b.send(hA.req(bytes([0x40 + i]) * 30, ack=hA.ack))
    await b.run(3000)
    s = b.snapshot()
    assert s["m_count"] == 1 + n and s["link_wr"] == mod(1 + n), s
    hB = Host(b, SID_B)
    b.send(hB.open_wire())
    await b.run(3000)
    s = b.snapshot()
    assert s["link_openPending"] == 1 and s["link_sid"] == SID_A, s  # waits for the frame
    b.tx_hold = False
    await b.settle()
    allf = b.frames()
    # parse by hand: first the frame(s) under A, then the OPEN reply under B
    for f in allf:
        f.parse(SID_A)
    under_a = [f for f in allf if f.p is not None]
    for f in allf:
        f.parse(SID_B)
    under_b = [f for f in allf if f.p is not None]
    assert len(allf) == len(under_a) + len(under_b), allf
    assert len(under_b) == 1 and under_b[0] is allf[-1], allf
    status_ok(under_b[0], seq=0, exp=0, cursor=12)
    assert len(under_a) == 1, under_a  # only the frame that was in flight
    for f in under_a:
        f.parse(SID_A)
        assert f.p["kind"] == "data" and f.p["seq"] == mod(1), f
    s = b.snapshot()
    assert s["link_wr"] == 0 and s["link_txq"] == 0 and s["m_count"] == 1 + n, s


async def start_long_request_and_stall(b, h, size=100):
    """Send a request, wait until the endpoint is in the middle of it, stall the endpoint."""
    size = min(size, MAXLEN - 12 - 4 - 4)  # the response (count + request) must fit
    b.send(h.req(bytes(range(1, 1 + size))))
    await b.until(lambda: b.sig("link_wActive") == 1, 20000, "endpoint in the middle of a request")
    b.stall(True)
    await b.run(20)
    assert b.sig("m_phase") != 0 and b.sig("link_wActive") == 1


@cocotb.test()
async def open_waits_for_the_endpoint_and_discards_unstarted_requests(dut):
    b = await Bench.create(dut)
    hA = Host(b, SID_A)
    await hA.open()
    await hA.transact(b"warm")  # executed = 1
    await start_long_request_and_stall(b, hA)
    assert b.sig("m_count") == 1
    # more requests are committed but the endpoint has not started them (none when W = 1)
    nun = min(2, W - 1)
    for i in range(nun):
        b.send(hA.req(b"unstarted-%d" % i))
    await b.run(1500)
    s = b.snapshot()
    assert s["link_exp"] == mod(2 + nun) and s["link_cons"] == mod(1) and s["m_count"] == 1, s
    # OPEN arrives; then two more old-session frames, which are ignored
    hB = Host(b, SID_B)
    b.send(hB.open_wire())
    await b.run(1500)
    assert b.sig("link_openPending") == 1
    assert int(dut.responseReady.value) == 1  # OPEN pending: the endpoint may finish
    b.send(hA.req(b"after-open-1"))
    b.send(hA.req(b"after-open-2"))
    n0 = len(b.tx_bytes)
    await b.run(4000)
    s = b.snapshot()
    assert s["link_exp"] == mod(2 + nun) and s["m_count"] == 1 and s["link_sid"] == SID_A, s
    assert (
        len(b.tx_bytes) == n0
    ), "no reply while the endpoint is stalled in the middle of a request"
    b.stall(False)
    await b.settle()
    # the request in progress finished (its response was dropped), the unstarted ones were not run
    s = b.snapshot()
    assert s["m_count"] == 2, ("executed", s)
    assert s["link_sid"] == SID_B and s["link_openPending"] == 0, s
    b.sid = SID_B
    fr = hB.absorb(b.frames())
    assert len(fr) == 1, fr
    status_ok(fr[0], seq=0, exp=0, cursor=12)
    hB.count = 2
    hB.consumed = 12
    f = await hB.transact(b"fresh")
    check_fields(f, seq=0, payload=resp_payload(2, b"fresh"))


@cocotb.test()
async def open_pending_with_a_full_response_window(dut):
    """The core waits for response space (no ACK). OPEN frees it: it can finish and the OPEN
    reply follows."""
    b = await Bench.create(dut)
    hA = Host(b, SID_A)
    await hA.open()
    for i in range(W):
        b.send(hA.req(bytes([i]) * 5, ack=0))
    await hA.collect(W)
    hA.count = W
    assert b.sig("link_wr") == mod(W) and b.sig("link_dack") == 0
    assert int(dut.responseReady.value) == 0  # window full
    b.send(hA.req(b"blocked", ack=0))
    await b.run(1500)
    assert b.sig("m_phase") != 0 and b.sig("m_count") == W and int(dut.responseReady.value) == 0
    hB = Host(b, SID_B)
    b.send(hB.open_wire())
    await b.settle()
    s = b.snapshot()
    assert s["m_count"] == W + 1 and s["link_sid"] == SID_B, s
    b.sid = SID_B
    fr = hB.absorb(b.frames())
    assert len(fr) == 1, fr
    status_ok(fr[0], seq=0, exp=0, cursor=12)
    hB.count = W + 1
    hB.consumed = 12
    await hB.transact(b"go")


@cocotb.test()
async def open_abandons_a_prefetched_request(dut):
    """doc/protocol.md: during OPEN, a request that the reader prefetched but the core has not taken
    (first token not read) is abandoned and discarded. The endpoint is stalled while idle, a
    request is committed (the reader stages it), then OPEN arrives. The OPEN reply must come
    while the endpoint is still stalled, and `executed` must not change after the stall ends.
    Repeated for a zero-length request (the reader stages an empty token)."""
    b = await Bench.create(dut)
    h = Host(b, tb.mk_sid(1))
    await h.open()
    await h.transact(b"warm")
    for n, payload in enumerate((b"staged", b"")):
        b.stall(True)
        b.send(h.req(payload))
        if W > 1:
            b.send(h.req(b"behind"))
        await b.run(1500)
        s = b.snapshot()
        # h has run one request in this session (exp = 1 after it, or after the warm-up)
        assert s["m_count"] == h.count and s["link_exp"] == mod(2 + (W > 1)), s
        assert s["link_cons"] == mod(1), s
        assert b.sig("link_rdState") != 0, "the reader staged the oldest request"
        hn = Host(b, tb.mk_sid(2 + n))
        hn.count = h.count
        b.sid = hn.sid
        b.frames()
        b.send(hn.open_wire())
        await b.until(lambda: bool(b._new), 8000, "OPEN reply while the endpoint is stalled")
        await b.run(300)
        s = b.snapshot()
        assert s["link_sid"] == hn.sid and s["link_openPending"] == 0 and s["m_count"] == h.count, s
        fr = hn.absorb(b.frames())
        assert len(fr) == 1, fr
        status_ok(fr[0], seq=0, exp=0, cursor=12)
        b.stall(False)
        await b.run(3000)
        s = b.snapshot()
        assert s["m_count"] == h.count, ("executed changed after the stall ended", s)
        assert not b.frames(), "unexpected frame after the stall ended"
        h = hn
        await h.transact(b"fresh %d" % n)  # the next request of the new session runs normally


@cocotb.test()
async def open_reply_follows_old_traffic_in_order(dut):
    """Random sizes: after any amount of old traffic the OPEN reply is exactly one status frame."""
    b = await Bench.create(dut)
    rng = random.Random(8)
    sid = 0x01010101
    h = Host(b, sid)
    await h.open()
    for rounds in range(6):
        for _ in range(rng.randrange(0, 5)):
            await h.transact(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 50))))
        new = Host(b, tb.rand_sid(rng))
        new.count = h.count
        await new.open(rsp_limit=rng.choice([MAXLEN, 40, 20]))
        new.consumed = 12
        h = new


# ---------------------------------------------------------------------------------------------
# A frame of the old session that straddles the session reset


async def stale_straddle_iteration(b, off, rx_gap):
    """One try. A stale frame (valid under the old session, seq 0, ack 0) arrives while OPEN is
    pending. The endpoint is released `off` clocks after the first byte of the stale frame.
    Return (executed before, executed after, sid before, frames)."""
    rng = random.Random(off)
    sa = tb.rand_sid(rng)
    sb = tb.rand_sid(rng)
    hA = Host(b, sa)
    hA.count = b.sig("m_count")
    b.rx_gap = 12
    await hA.open()
    await hA.transact(b"warm")
    base = b.sig("m_count")
    await start_long_request_and_stall(b, hA, size=40)
    hB = Host(b, sb)
    b.send(hB.open_wire())
    await b.until(lambda: b.sig("link_openPending") == 1, 5000, "openPending")
    b.rx_gap = rx_gap
    n_before = b.rx_accepted
    stale = hA.req(b"STALE", seq=0, ack=0, grant=0x4444, rewind=2)
    b.send(stale)
    await b.until(lambda: b.rx_accepted > n_before, 3000, "first byte of the stale frame")
    await b.run(off)
    b.stall(False)
    # run until the stale frame is fully received and the endpoint and TX are done
    await b.until(lambda: not b.rx_q and not b.rx_wait and b.sig("link_crcRun") == 0, 5000, "rx")
    await b.run(400)
    await b.settle()
    b.rx_gap = 12
    snap = b.snapshot()
    b.sid = sb
    frames = b.frames()
    return base, snap, frames, hB, sb


@cocotb.test()
async def stale_frame_straddling_the_session_reset(dut):
    """A frame that was valid under the old session and is still being received when the
    session reset happens must not be accepted by the new session. The release time of the
    endpoint is swept over the whole reception of the stale frame.

    A link without the session generation decides the stale frame with the new session's state
    for the release offsets in a window of about one byte time before the delimiter: it executes
    the frame, or at least takes its grant and REWIND epoch."""
    b = await Bench.create(dut)
    rx_gap = 40
    nbytes = len(Host(b, 1).req(b"STALE", seq=0, ack=0))
    bad = []
    for off in range(0, nbytes * rx_gap + 60, 3):
        base, snap, frames, hB, sb = await stale_straddle_iteration(b, off, rx_gap)
        # The long request completes (+1). The stale frame must have no effect at all: it must not
        # run, change exp, the grant (the OPEN grant stays), or the REWIND epoch, and it must get
        # no reply (the OPEN reply is the only frame).
        kinds = [f.kind for f in frames]
        if (
            snap["m_count"] != base + 1
            or snap["link_exp"] != 0
            or snap["link_dack"] != 0
            or snap["link_grant"] != hB.grant_for(0)
            or snap["link_rwHost"] != 0
            or kinds != ["status"]
        ):
            bad.append(
                (
                    off,
                    snap["m_count"] - base - 1,
                    snap["link_exp"],
                    hex(snap["link_grant"]),
                    snap["link_rwHost"],
                    kinds,
                )
            )
    dut._log.info(
        "stale straddle: %d release offsets tried, %d let the stale frame act: "
        "(offset, extra executions, exp, grant, rwHost, frames) %s",
        len(range(0, nbytes * rx_gap + 60, 3)),
        len(bad),
        bad[:6],
    )
    assert not bad, f"the stale frame acted in the new session at release offsets {bad[:6]}"


@cocotb.test()
async def stale_frame_straddling_the_session_reset_minimal(dut):
    """Minimal reproduction of the stale-frame problem that the session generation prevents,
    without a sweep.

    1. Session A, one warm-up request. A long request is in progress, the endpoint is stalled.
    2. OPEN for session B (the reset waits for the endpoint).
    3. A stale frame of session A (DATA, seq 0, ack 0, payload "STALE") arrives slowly (600 clocks
       per byte). Its trailer is valid under A.
    4. When the RTL has checked the last trailer byte (`link_done` is set) and the delimiter is
       still 200 clocks away, the endpoint is released. The session reset happens inside that gap.
    5. At the delimiter `decide` sees `bound = 1` and `openPending = 0`, so it accepts the frame.

    Expected (doc/protocol.md): the stale frame is not executed in session B and has no effect.
    Without the session generation it executes the old content of request page 0 ("warm") again.
    """
    b = await Bench.create(dut)
    hA = Host(b, SID_A)
    await hA.open()
    await hA.transact(b"warm")
    await start_long_request_and_stall(b, hA, size=40)
    base = b.sig("m_count")  # 1
    hB = Host(b, SID_B)
    b.send(hB.open_wire())
    await b.until(lambda: b.sig("link_openPending") == 1, 5000, "openPending")
    b.rx_gap = 600
    stale = hA.req(b"STALE", seq=0, ack=0, grant=0x4444, rewind=2)
    n_before = b.rx_accepted
    b.send(stale)
    # the last trailer byte has been checked, only the delimiter is missing
    await b.until(
        lambda: b.rx_accepted == n_before + len(stale) - 1
        and b.sig("link_done") == 1
        and b.sig("link_crcRun") == 0,
        20000,
        "trailer checked",
    )
    assert b.sig("link_openPending") == 1
    b.stall(False)
    await b.until(lambda: not b.rx_q and not b.rx_wait and b.sig("link_crcRun") == 0, 5000, "rx")
    await b.settle()
    after = b.sig("m_count")
    b.sid = SID_B
    dut._log.info(
        "stale minimal: executed %d -> %d, exp=%d, frames under B: %s",
        base,
        after,
        b.sig("link_exp"),
        b.peek_frames(),
    )
    snap = b.snapshot()
    assert snap["link_grant"] == hB.grant_for(0), f"stale grant {snap['link_grant']:#x} took effect"
    assert snap["link_rwHost"] == 0, "stale REWIND epoch took effect"
    assert len(b.peek_frames()) == 1, "the stale frame got a reply"
    assert after == base + 1, f"the stale frame of session A was executed in session B ({after})"
    assert snap["link_exp"] == 0, "the stale frame was committed in session B"


@cocotb.test()
async def second_open_straddling_the_reset(dut):
    """OPEN 1 is pending (the core is stalled in a request). OPEN 2 (another session) arrives and
    the core is released at a swept time, so the reset falls before, inside or after OPEN 2.
    The outcome must be consistent: the session that is bound gets exactly one reply under its ID.
    If OPEN 2 straddles the reset it is dropped (doc/protocol.md, session generation), and a retry
    works.
    """
    b = await Bench.create(dut)
    rx_gap = 40
    nbytes = len(Host(b, 1).open_wire())
    outcomes = {"s1": 0, "s2": 0}
    for off in range(0, nbytes * rx_gap + 60, 7):
        rng = random.Random(off)
        sa, s1, s2 = tb.rand_sid(rng), tb.rand_sid(rng), tb.rand_sid(rng)
        hA = Host(b, sa)
        hA.count = b.sig("m_count")
        b.rx_gap = 12
        await hA.open()
        await hA.transact(b"warm")
        await start_long_request_and_stall(b, hA, size=40)
        h1, h2 = Host(b, s1), Host(b, s2)
        b.send(h1.open_wire())
        await b.until(lambda: b.sig("link_openPending") == 1, 5000, "openPending")
        b.rx_gap = rx_gap
        n_before = b.rx_accepted
        b.send(h2.open_wire())
        await b.until(lambda: b.rx_accepted > n_before, 3000, "first byte of OPEN 2")
        await b.run(off)
        b.stall(False)
        await b.until(
            lambda: not b.rx_q and not b.rx_wait and b.sig("link_crcRun") == 0, 5000, "rx"
        )
        await b.run(400)
        await b.settle()
        b.rx_gap = 12
        sid_now = b.sig("link_sid")
        frames = b.frames()
        assert sid_now in (s1, s2), (off, hex(sid_now))
        who = h1 if sid_now == s1 else h2
        outcomes["s1" if sid_now == s1 else "s2"] += 1
        for f in frames:
            f.parse(who.sid)
        assert [f.kind for f in frames] == ["status"], (off, sid_now == s1, frames)
        assert b.sig("link_openPending") == 0 and b.sig("link_exp") == 0
        # the bound session works; if OPEN 2 was dropped, its retry works too
        b.sid = who.sid
        who.count = b.sig("m_count")
        who.consumed = 12
        await who.transact(b"check")
        if who is h1:
            h2.count = who.count
            await h2.open()
            await h2.transact(b"retry")
    dut._log.info(
        "OPEN 2 straddle sweep: session 1 stayed %d times, session 2 took over %d times",
        outcomes["s1"],
        outcomes["s2"],
    )
    assert outcomes["s1"] > 0 and outcomes["s2"] > 0, "the sweep did not cover both outcomes"


@cocotb.test()
async def open_replies_and_new_session_after_old_frames_in_flight(dut):
    """Old-session requests keep arriving around OPEN (the host retransmits during OPEN). After
    the OPEN reply nothing of the old session is executed in the new one."""
    b = await Bench.create(dut)
    rng = random.Random(33)
    hA = Host(b, tb.rand_sid(rng))
    await hA.open()
    await hA.transact(b"warm")
    base = b.sig("m_count")
    hB = Host(b, tb.rand_sid(rng))
    # old-session requests, OPEN, more old-session requests (in flight), all back to back
    old_req = [hA.req(b"old-%d" % i, ack=hA.ack) for i in range(min(W, 3))]
    for w in old_req:
        b.send(w)
    b.send(hB.open_wire())
    for i in range(3):
        b.send(hA.req(b"late-%d" % i, seq=mod(i), ack=0))
    await b.settle()
    n_exec = b.sig("m_count") - base
    assert n_exec == len(old_req), "only the requests committed before OPEN run"
    assert b.sig("link_sid") == hB.sid and b.sig("link_exp") == 0
    b.sid = hB.sid
    fr = b.frames()
    # the replies to the old requests may come first (old session), then the OPEN reply
    for f in fr:
        f.parse(hB.sid)
    mine = [f for f in fr if f.p is not None]
    assert len(mine) == 1 and mine[0].kind == "status" and mine[0] is fr[-1], fr
    hB.count = b.sig("m_count")
    hB.consumed = mine[0].p["cursor"]
    await hB.transact(b"new")
