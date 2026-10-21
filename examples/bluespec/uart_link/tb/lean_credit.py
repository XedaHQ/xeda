"""Credit (grant edge, status reserve, bridge budget, cursor wrap) and windows.

Credit rule (doc/protocol.md): a frame of V bytes may start if `grant - (cursor + V) >= 0` (DATA) or
`>= -R` with R = 64 (status). The host sends `grant = consumed + B - R`.
"""

import cocotb

import lean_tb as tb
from lean_tb import (
    MAXLEN,
    W,
    Bench,
    Host,
    check_fields,
    lc,
    mod,
    resp_payload,
    run_bridge,
    status_ok,
)

SID = 0x4C4C0101
FRAME8 = 12 + 4 + 8  # wire length of the response to a request with an 8-byte payload


async def open_session(dut, rx_gap=12, tx_gap=12, grant=None, sid=SID):
    b = await Bench.create(dut, rx_gap=rx_gap, tx_gap=tx_gap)
    h = Host(b, sid)
    await h.open(grant=grant)
    return b, h


def data_frames(frames):
    return [f for f in frames if f.kind == "data"]


# ---------------------------------------------------------------------------------------------
# Credit


@cocotb.test()
async def data_frame_edge_and_status_reserve(dut):
    """One response is ready but has no credit. Count the status frames that the 64-byte reserve
    allows (exactly), then test the data edge to the byte."""
    b, h = await open_session(dut)
    c = 12  # cursor after the OPEN reply
    e1 = c + FRAME8 - 1  # one byte short of the response frame
    b.send(h.req(bytes(range(8)), grant=e1))
    await b.settle()
    await b.run(2000)
    assert b.sig("m_count") == 1 and b.sig("link_wr") == mod(1)
    assert not b.frames(), "a DATA frame was sent without credit"
    assert b.sig("link_grant") == e1
    # POLL one at a time: a status frame needs grant - (cursor + 12) >= -64
    k_max = (e1 - c + 64) // 12
    got = 0
    for i in range(k_max + 3):
        b.send(h.hdr(poll=True, grant=e1))
        await b.settle()
        await b.run(100)
        fr = h.absorb(b.frames())
        if i < k_max:
            assert len(fr) == 1 and fr[0].kind == "status", (i, fr)
            status_ok(fr[0], seq=0, exp=mod(1), cursor=c + 12 * (i + 1))
            got += 1
        else:
            assert not fr, (i, fr)
    assert got == k_max
    c = h.consumed
    assert c == 12 + 12 * k_max
    dut._log.info(
        "status reserve: edge %d, %d status frames sent, cursor %d (edge+%d)", e1, got, c, c - e1
    )
    assert c - e1 <= 64 and c + 12 - e1 > 64
    # The unanswered POLLs left `owe` set. A grant for exactly one DATA frame now arrives.
    # doc/protocol.md allows two orders (a race with the TX decision, which alternates
    # `tryStatus` while DATA waits with `owe` set): the DATA frame goes first and discharges
    # `owe`; or a status frame goes first (credit for it exists) and the DATA frame waits for the
    # next grant. Accept both.
    b.send(h.hdr(grant=c + FRAME8))
    await b.settle()
    await b.run(300)
    fr = h.absorb(b.frames())
    kinds = [f.kind for f in fr]
    assert kinds in (["data"], ["status"]), kinds
    if kinds == ["status"]:  # status first: the DATA frame needs a grant that includes it
        c = h.consumed
        b.send(h.hdr(grant=c + FRAME8))
        fr = await h.collect(1)
        assert [f.kind for f in fr] == ["data"], fr
    else:
        await b.run(500)
        assert not b.frames(), "owe was not discharged by the DATA frame"
    d = [f for f in h.frames_seen if f.kind == "data"]
    assert len(d) == 1
    check_fields(d[0], seq=0, payload=resp_payload(0, bytes(range(8))), cursor=h.consumed)


@cocotb.test(skip=W < 4)
async def data_frames_stop_at_the_edge(dut):
    """Four responses are ready. The edge allows 1.x of them. Each new byte of grant counts."""
    b, h = await open_session(dut)
    pays = [bytes([i]) * 8 for i in range(4)]
    edge = 12 + FRAME8 - 1
    b.send(h.req(pays[0], grant=edge))
    await b.settle()
    await b.run(500)
    assert not b.frames()
    for p in pays[1:]:
        b.send(h.req(p, grant=edge))
    await b.settle()
    await b.run(500)
    assert not b.frames() and b.sig("m_count") == 4
    # edge for exactly one frame, then for 1.99 frames, then for two
    for e, n_total in (
        (12 + FRAME8, 1),
        (12 + 2 * FRAME8 - 1, 1),
        (12 + 2 * FRAME8, 2),
        (12 + 4 * FRAME8 - 1, 3),
        (12 + 4 * FRAME8, 4),
    ):
        b.send(h.hdr(grant=e))
        await b.settle()
        await b.run(300)
        h.absorb(b.frames())
        have = sum(1 for f in h.frames_seen if f.kind == "data")
        assert have == n_total, (e, n_total, have)
        assert h.consumed <= e
    # all four, in order, exact
    seqs = [f.p["seq"] for f in h.frames_seen if f.kind == "data"]
    assert seqs == [0, 1, 2, 3], seqs
    for f in [f for f in h.frames_seen if f.kind == "data"]:
        assert f.p["payload"] == resp_payload(f.p["seq"], pays[f.p["seq"]])


@cocotb.test()
async def bridge_budget_with_a_slow_reader(dut):
    """Slow reader (150 clocks per byte, pauses): FPGA bytes in flight never exceed B."""
    B = 300 if W > 1 else 140  # B - R must hold the largest frame (71 bytes at W = 1)
    b, h, br, total = await run_bridge(
        dut, B=B, read_gap=150, pause_prob=0.02, pause_range=(300, 3000), seed=1, n=30, max_len=100
    )
    assert br.max_inflight <= B
    if W > 1:  # with W = 1 the window, not the credit, limits the bytes in flight
        assert br.max_inflight >= B - 64 - 120, "the budget never bound: the test is too weak"


@cocotb.test()
async def bridge_budget_credit_recovers(dut):
    """The reader stops for a long time with data waiting. The FPGA stops at the edge. When the
    reader resumes and sends a new grant, the FPGA continues. Run with a tight budget."""
    B = 200 if W > 1 else 140
    b, h, br, total = await run_bridge(
        dut, B=B, read_gap=40, pause_prob=0.15, pause_range=(2000, 6000), seed=2, n=40, max_len=60
    )
    assert br.max_inflight <= B
    if W > 1:
        assert br.max_inflight >= B - 64 - 80, "the budget never bound: the test is too weak"


@cocotb.test()
async def cursor_wraps_past_65535(dut):
    """More than 65536 FPGA bytes in one session: the 16-bit cursor and grant wrap; every frame,
    credit decision and response stays exact."""
    B = 600 if W > 1 else 300
    b, h, br, total = await run_bridge(
        dut,
        B=B,
        read_gap=12,
        pause_prob=0.01,
        pause_range=(200, 1500),
        seed=3,
        min_bytes=66000,
        max_len=140,
    )
    assert total > 65536 + 1000, total
    assert br.wraps >= 1, "the cursor did not wrap"
    assert br.max_inflight <= B


@cocotb.test()
async def credit_edges_across_the_cursor_wrap(dut):
    """Move the cursor to just below 65536, then test the status reserve and the data edge to the
    byte while the cursor and the grant wrap."""
    b, h, br, total = await run_bridge(
        dut,
        B=4000,
        read_gap=12,
        pause_prob=0.0,
        pause_range=(1, 2),
        seed=4,
        n=430 if MAXLEN > 100 else 900,
        max_len=240,
    )
    h.count = br.rsp
    h.B = 4000
    assert br.wraps == 0 and h.consumed < 65536 - 1000, (h.consumed, total)
    while h.consumed < 65536 - 20:  # status frames with a large grant: 12 bytes each
        b.send(h.hdr(poll=True))
        await b.settle()
        h.absorb(b.frames())
    c = h.consumed
    assert 65536 - 20 <= c < 65536 - 8, c
    # status reserve across zero: the edge is 5 bytes above the cursor
    edge = (c + 5) & 0xFFFF
    k_max = (5 + 64) // 12
    n_status = 0
    for i in range(k_max + 2):
        b.send(h.hdr(poll=True, grant=edge))
        await b.settle()
        await b.run(100)
        n_status += len(h.absorb(b.frames()))
    assert n_status == k_max, (n_status, k_max)
    assert h.consumed == (c + 12 * k_max) & 0xFFFF and h.consumed < c, "the cursor wrapped"
    # a large grant clears the owe flag with one status frame
    b.send(h.hdr(grant=(h.consumed + 3000) & 0xFFFF))
    await b.settle()
    h.absorb(b.frames())
    c = h.consumed
    # data edge: one byte short, then exact (the cursor is small, the edge is near it)
    pay = bytes(range(8))
    b.send(h.req(pay, grant=(c + FRAME8 - 1) & 0xFFFF, ack=h.ack))
    await b.settle()
    await b.run(500)
    assert not b.frames(), "DATA frame without credit"
    b.send(h.hdr(grant=(c + FRAME8) & 0xFFFF))
    fr = data_frames(await h.collect(1))
    assert len(fr) == 1 and fr[0].p["payload"] == resp_payload(h.count, pay)
    assert h.consumed == (c + FRAME8) & 0xFFFF


# ---------------------------------------------------------------------------------------------
# RESYNC (ctl bit 0): one status frame without credit


async def exhaust_status_reserve(b, h, edge):
    """One DATA response is blocked (the edge is one byte short). POLLs use up the 64-byte status
    reserve. Return the cursor. Afterwards a POLL gets no reply."""
    c = 12
    b.send(h.req(bytes(range(8)), grant=edge))
    await b.settle()
    await b.run(1000)
    assert not b.frames() and b.sig("m_count") == 1
    k_max = (edge - c + 64) // 12
    for i in range(k_max):
        b.send(h.hdr(poll=True, grant=edge))
        await b.settle()
        await b.run(100)
        fr = h.absorb(b.frames())
        assert len(fr) == 1 and fr[0].kind == "status", (i, fr)
    for i in range(3):  # the reserve is used up: POLL gets no reply
        b.send(h.hdr(poll=True, grant=edge))
        await b.settle()
        await b.run(300)
        assert not b.frames(), "POLL got a reply without credit"
        assert b.sig("link_owe") == 1 and b.sig("link_permit") == 0
    assert h.consumed == c + 12 * k_max
    return h.consumed


@cocotb.test()
async def resync_gets_one_status_frame_then_a_grant_restores_data(dut):
    b, h = await open_session(dut)
    edge = 12 + FRAME8 - 1
    c = await exhaust_status_reserve(b, h, edge)
    assert c - edge <= 64 and c + 12 - edge > 64
    # RESYNC: exactly one status frame, charged to the cursor
    b.send(h.hdr(resync=True, grant=edge))
    await b.settle()
    await b.run(300)
    fr = h.absorb(b.frames())
    assert len(fr) == 1 and fr[0].kind == "status", fr
    status_ok(fr[0], seq=0, exp=mod(1), cursor=c + 12)
    s = b.snapshot()
    assert s["link_permit"] == 0 and s["link_owe"] == 0 and s["link_cursor"] == c + 12, s
    # the permit was used: POLL gets nothing again, and the response is still blocked
    b.send(h.hdr(poll=True, grant=edge))
    await b.settle()
    await b.run(300)
    assert not b.frames()
    # every RESYNC gets one more status frame
    for i in range(2):
        b.send(h.hdr(resync=True, grant=edge))
        await b.settle()
        await b.run(300)
        fr = h.absorb(b.frames())
        assert len(fr) == 1 and fr[0].kind == "status", (i, fr)
        status_ok(fr[0], seq=0, exp=mod(1), cursor=c + 12 * (i + 2))
    c = h.consumed
    # a new grant restores DATA (exact edge)
    assert b.sig("link_owe") == 0
    b.send(h.hdr(grant=c + FRAME8 - 1))
    await b.settle()
    await b.run(300)
    assert not b.frames(), "DATA frame one byte short of the edge"
    b.send(h.hdr(grant=c + FRAME8))
    fr = await h.collect(1)
    assert [f.kind for f in fr] == ["data"], fr
    check_fields(fr[0], seq=0, payload=resp_payload(0, bytes(range(8))), cursor=c + FRAME8)


@cocotb.test()
async def resync_carried_by_a_request_and_other_cases(dut):
    """RESYNC in a DATA frame: the request commits and one status frame goes without credit. RESYNC
    does nothing in an invalid frame, in a frame with an ack for an unsent response, while unbound,
    in an OPEN frame, or from the old session while OPEN is pending."""
    b, h = await open_session(dut)
    edge = 12 + FRAME8 - 1
    c = await exhaust_status_reserve(b, h, edge)
    # DATA + RESYNC: commit, then one status frame
    b.send(h.req(b"second", grant=edge, resync=True))
    # (at W = 1 the core blocks on the full response window, so do not wait for it to settle)
    await b.run(1500)
    fr = h.absorb(b.frames())
    assert [f.kind for f in fr] == ["status"], fr
    status_ok(fr[0], seq=0, exp=mod(2), cursor=c + 12)
    assert b.sig("m_count") == (2 if W > 1 else 1) and b.sig("link_permit") == 0
    c = h.consumed
    # an ack for an unsent response: the whole frame is ignored, RESYNC too
    snap = b.snapshot()
    b.send(h.hdr(resync=True, grant=edge, ack=1))
    await b.run(600)  # W = 1: the core may be blocked, so no settle
    await b.run(300)
    assert not b.frames() and b.snapshot() == snap
    # a damaged frame and a frame of another session: nothing
    for w in (tb.flip_bit(h.hdr(resync=True, grant=edge), 3, 2), h.hdr(resync=True, sid=h.sid ^ 5)):
        b.send(w)
        await b.run(600)  # W = 1: the core may be blocked, so no settle
        await b.run(300)
        assert not b.frames() and b.snapshot() == snap
    # RESYNC in an OPEN frame is ignored: with no credit for the reply, no reply goes
    hn = Host(b, tb.mk_sid(7))
    b.sid = hn.sid
    b.send(hn.open_wire(grant=(-60) & 0xFFFF, rsp_limit=MAXLEN))
    await b.run(600)  # W = 1: the core may be blocked, so no settle
    await b.run(300)
    assert not b.frames()
    resync_open = lc.encode_host_wire(
        lc.build_host_frame(
            lc.host_ctl(open_frame=True) | 1,
            0,
            0,
            (-60) & 0xFFFF,
            hn.sid.to_bytes(4, "little") + bytes([MAXLEN]),
            0,
        )
    )
    b.send(resync_open)
    await b.run(600)  # W = 1: the core may be blocked, so no settle
    await b.run(300)
    assert not b.frames(), "RESYNC in an OPEN frame gave a status frame"
    s = b.snapshot()
    assert s["link_bound"] == 1 and s["link_sid"] == hn.sid and s["link_permit"] == 0, s
    # a header-only frame with credit releases the OPEN reply
    hn.consumed = 0
    b.send(hn.hdr(grant=0x1000))
    await b.run(600)  # W = 1: the core may be blocked, so no settle
    fr = hn.absorb(b.frames())
    assert len(fr) == 1
    status_ok(fr[0], seq=0, exp=0, cursor=12)


@cocotb.test()
async def resync_permit_survives_a_data_frame_that_discharges_owe(dut):
    """RESYNC (with a grant that pays for the blocked DATA frame) while credit and reserve are used
    up. doc/protocol.md allows two orders, and which one happens is a race with the TX decision
    (the TX alternates `tryStatus` while a DATA frame waits with `owe` set):
    - DATA first: the grant pays for it, the DATA frame clears `owe`, the permit stays; a later POLL
      then gets one status frame on the permit.
    - status first: the permit pays for a status frame that takes the credit the DATA frame
      needed; the DATA frame waits and goes with the next grant (and answers that POLL itself).
    The test sweeps the arrival of the RESYNC frame over 8 clock shifts and accepts either order. It
    requires: exactly one DATA frame for the request, at most one status frame, the permit used by
    exactly one status frame (now, or by the next POLL), one execution, and a session that goes on.
    It logs which order each shift gave."""
    orders = {}
    for shift in range(8):
        b, h = await open_session(dut, sid=tb.mk_sid(300 + shift))
        h.count = b.sig("m_count")
        edge = 12 + FRAME8 - 1
        c = await exhaust_status_reserve(b, h, edge)
        base_exec = b.sig("m_count")
        await b.run(shift)
        b.send(h.hdr(resync=True, grant=c + FRAME8))
        await b.settle()
        await b.run(300)
        fr = h.absorb(b.frames())
        kinds = [f.kind for f in fr]
        assert kinds in (["data"], ["status"]), (shift, kinds)
        status_first = kinds == ["status"]
        orders[shift] = kinds[0]
        s = b.snapshot()
        assert s["link_permit"] == (0 if status_first else 1), (shift, kinds, s)
        assert s["link_owe"] == 0, (shift, s)
        # a POLL with credit for everything: data first -> one status frame (the permit, the owe of
        # the POLL); status first -> the waiting DATA frame goes and answers the POLL
        b.send(h.hdr(poll=True, grant=h.consumed + 100))
        await b.settle()
        await b.run(200)
        fr = h.absorb(b.frames())
        kinds2 = [f.kind for f in fr]
        assert kinds2 == (["data"] if status_first else ["status"]), (shift, kinds, kinds2)
        assert b.sig("link_permit") == 0, shift
        data = [f for f in h.frames_seen if f.kind == "data"]
        assert len(data) == 1 and data[0].p["seq"] == 0, (shift, data)
        assert b.sig("m_count") == base_exec  # no request ran twice
        h.ack = mod(h.ack + 1)
        b.send(h.req(b"after"))
        fr = await h.collect(1)
        assert [f.kind for f in fr] == ["data"], (shift, fr)
        assert b.sig("m_count") == base_exec + 1
        b.close()
    dut._log.info("RESYNC race: first frame by arrival shift %s", orders)


@cocotb.test()
async def resync_while_unbound_gets_nothing(dut):
    """Unbound: the boot grant pays for one status frame. RESYNC adds no permit there."""
    b = await Bench.create(dut)
    h = Host(b, tb.mk_sid(3))
    b.sid = h.sid
    b.send(h.hdr(resync=True))
    await b.settle()
    await b.run(400)
    fr = b.frames()
    assert len(fr) == 1 and fr[0].kind == "unbound", fr  # the boot status frame
    for w in (h.hdr(resync=True), h.hdr(resync=True, sid=0), h.req(b"x", resync=True)):
        b.send(w)
        await b.settle()
        await b.run(400)
        assert not b.frames(), "RESYNC while unbound gave a frame"
    assert b.sig("link_permit") == 0 and b.sig("m_count") == 0


@cocotb.test()
async def resync_from_the_old_session_is_ignored_while_open_is_pending(dut):
    b, h = await open_session(dut)
    b.send(h.req(bytes(range(40))))
    await b.until(lambda: b.sig("link_wActive") == 1, 20000, "endpoint in the middle of a request")
    b.stall(True)
    hn = Host(b, tb.mk_sid(8))
    b.send(hn.open_wire())
    await b.until(lambda: b.sig("link_openPending") == 1, 5000, "openPending")
    b.send(h.hdr(resync=True, ack=0))
    await b.run(2000)
    assert b.sig("link_permit") == 0 and b.sig("link_owe") == 0
    b.stall(False)
    b.sid = hn.sid
    await b.settle()
    fr = hn.absorb(b.frames())
    assert len(fr) == 1 and fr[0].kind == "status", fr
    assert b.sig("link_permit") == 0


# ---------------------------------------------------------------------------------------------
# Windows


@cocotb.test()
async def request_pages_full_with_a_stalled_endpoint(dut):
    """The endpoint is stalled, so W requests fill the request pages. The next request is dropped
    with a status reply, the RX keeps draining, and POLL is still answered."""
    b, h = await open_session(dut)
    b.stall(True)
    pays = [bytes([0x20 + i]) * (10 + i) for i in range(W)]
    for p in pays:
        b.send(h.req(p))
    await b.run(len(pays) * 40 * 12 + 800)
    s = b.snapshot()
    assert s["link_exp"] == mod(W) and s["link_cons"] == 0 and s["m_count"] == 0, s
    assert not b.frames()
    # pages are full: a dropped request gets a status frame
    big = bytes([0x77]) * (MAXLEN - 12)
    n0 = b.rx_accepted
    b.rx_max_wait_clocks = 0
    b.rx_max_waiting = 0
    rounds = 6
    for r in range(rounds):
        b.send(h.req(big, seq=mod(W)))
    await b.until(lambda: not b.rx_q and not b.rx_wait, 200000, "RX drains")
    sent = b.rx_accepted - n0
    assert sent == rounds * len(h.req(big, seq=mod(W))), sent
    assert b.rx_max_wait_clocks <= 10 and b.rx_max_waiting <= 1, (
        b.rx_max_wait_clocks,
        b.rx_max_waiting,
    )
    await b.run(400)
    fr = h.absorb(b.frames())
    assert fr and all(f.kind == "status" for f in fr), fr
    for f in fr:
        status_ok(f, seq=0, exp=mod(W))
    s = b.snapshot()
    assert s["link_exp"] == mod(W) and s["m_count"] == 0, s
    # POLL is answered while the endpoint is stalled
    b.send(h.hdr(poll=True))
    await b.run(600)
    fr = h.absorb(b.frames())
    assert len(fr) == 1 and fr[0].kind == "status", fr
    status_ok(fr[0], seq=0, exp=mod(W))
    # release: the W requests run in order; the dropped request is retransmitted and accepted
    b.stall(False)
    got = data_frames(await h.collect(W))
    assert [f.p["seq"] for f in got] == [mod(i) for i in range(W)], got
    for i, f in enumerate(got):
        assert f.p["payload"] == resp_payload(i, pays[i]), (i, f)
    h.count = W
    # the response window is full (W responses, no ACK yet): the retransmitted request carries ack 1
    b.send(h.req(b"retransmit", seq=mod(W), ack=1))
    got = data_frames(await h.collect(1))
    assert got[0].p["seq"] == mod(W) and got[0].p["payload"] == resp_payload(W, b"retransmit")


@cocotb.test()
async def response_window_full_without_ack(dut):
    """W responses without an ACK: responseReady goes false when the last response is written, the
    next request blocks the core, and an ACK releases it."""
    b, h = await open_session(dut)
    ready = []
    b.hooks.append(
        lambda bb, clk: ready.append((clk, int(dut.responseReady.value), bb.sig("link_wr")))
    )
    assert int(dut.responseReady.value) == 1
    for i in range(W):
        b.send(h.req(bytes([i]) * 6, ack=0))
    await h.collect(W)
    h.count = W
    s = b.snapshot()
    assert s["link_wr"] == mod(W) and s["link_dack"] == 0, s
    assert int(dut.responseReady.value) == 0
    # it fell exactly when the last response was written (wr became W)
    next(c for c, r, wr in ready if wr == mod(W))
    assert all(
        r == 1 for c, r, wr in ready if wr != mod(W)
    ), "responseReady low before the window was full"
    assert all(
        r == 0 for c, r, wr in ready if wr == mod(W)
    ), "responseReady high with a full window"
    # the next request is committed (page free) but the core blocks on the full response window
    b.send(h.req(b"blocked", ack=0))
    await b.run(3000)
    assert b.sig("m_phase") != 0 and b.sig("m_count") == W and int(dut.responseReady.value) == 0
    assert not data_frames(b.frames()), "a response was sent without room"
    # ACK for response 0: room for one more response
    h.ack = 1
    b.send(h.hdr(ack=1))
    t0 = b.clk
    fr = data_frames(await h.collect(1))
    assert len(fr) == 1 and fr[0].p["seq"] == mod(W), fr
    assert fr[0].p["payload"] == resp_payload(W, b"blocked")
    t_ready = next(c for c, r, wr in ready if c > t0 and r == 1)
    dut._log.info("response window: ACK sent at clock %d, responseReady high at %d", t0, t_ready)
    b.hooks.pop()
    assert b.sig("m_count") == W + 1


@cocotb.test()
async def poll_is_answered_while_the_endpoint_is_stalled_mid_request(dut):
    b, h = await open_session(dut)
    b.send(h.req(bytes(range(40))))
    await b.until(lambda: b.sig("link_wActive") == 1, 20000, "endpoint in the middle of a request")
    b.stall(True)
    await b.run(50)
    for i in range(3):
        b.send(h.hdr(poll=True))
        await b.until(lambda: bool(b._new), 8000, "status frame")
        await b.run(200)
        fr = h.absorb(b.frames())
        assert len(fr) == 1 and fr[0].kind == "status", (i, fr)
        status_ok(fr[0], seq=0, exp=mod(1))
    b.stall(False)
    got = data_frames(await h.collect(1))
    assert got[0].p["payload"] == resp_payload(0, bytes(range(40)))
