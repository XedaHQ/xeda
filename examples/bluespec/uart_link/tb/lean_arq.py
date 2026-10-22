"""Duplicates and gaps, lost responses (REWIND), and ACK validation."""

import cocotb

from lean_tb import (
    MAXLEN,
    SEQ_MOD,
    W,
    Bench,
    Host,
    TestFailure,
    check_fields,
    mod,
    resp_payload,
    status_ok,
)

SID = 0x3C3C0101


async def open_session(dut, rx_gap=12, tx_gap=12, rsp_limit=None, sid=SID):
    b = await Bench.create(dut, rx_gap=rx_gap, tx_gap=tx_gap)
    h = Host(b, sid)
    await h.open(rsp_limit=rsp_limit)
    return b, h


def data_frames(frames):
    return [f for f in frames if f.kind == "data"]


async def quiet_check(b, snap=None, clocks=300):
    """Settle, then no byte was sent and (if given) the registers equal `snap`."""
    n0 = len(b.tx_bytes)
    await b.settle()
    await b.run(clocks)
    if len(b.tx_bytes) != n0:
        raise TestFailure(f"unexpected FPGA bytes: {b.peek_frames()}")
    if snap is not None and b.snapshot() != snap:
        after = b.snapshot()
        raise TestFailure(
            f"state changed: {({k: (snap[k], after[k]) for k in snap if snap[k] != after[k]})}"
        )


# ---------------------------------------------------------------------------------------------
# Duplicates and gaps


@cocotb.test()
async def duplicate_request_gets_status_and_no_execution(dut):
    b, h = await open_session(dut)
    # With W = 1 the sequence numbers are 0 and 1, so only one request is a duplicate.
    done = 2 if W > 1 else 1
    await h.transact(b"alpha")
    if done == 2:
        await h.transact(b"beta")
    assert b.sig("m_count") == done
    # each duplicate (the old request, or the same sequence number with other bytes) gets one
    # status frame and is not executed
    dups = [(b"alpha", 0), (b"beta", 1), (b"changed bytes", 1), (b"", 0)]
    if W == 1:
        dups = [(b"alpha", 0), (b"changed bytes", 0)]
    for payload, seq in dups:
        b.send(h.req(payload, seq=seq))
        fr = await h.collect()
        assert len(fr) == 1 and fr[0].kind == "status", fr
        status_ok(fr[0], seq=mod(done), exp=mod(done), cursor=h.consumed)
        after = b.snapshot()
        assert after["m_count"] == done and after["link_exp"] == mod(done), after
        # a duplicate still carries a valid ack and grant, which take effect
        assert after["link_dack"] == h.ack and after["link_grant"] == h.grant_for(
            h.consumed - 12
        ), after


@cocotb.test()
async def duplicate_carries_ack_and_grant(dut):
    """The ack, grant and REWIND epoch of a frame count even when its request is a duplicate."""
    b, h = await open_session(dut)
    b.send(h.req(b"one", ack=0))
    await h.collect(1)
    h.count = 1
    # duplicate of request 0 that acknowledges response 0 and moves the grant
    b.send(h.req(b"one", seq=0, ack=1, grant=0x2222))
    fr = await h.collect()
    assert [f.kind for f in fr] == ["status"], fr
    s = b.snapshot()
    assert s["link_dack"] == 1 and s["link_grant"] == 0x2222 and s["m_count"] == 1, s


@cocotb.test()
async def request_ahead_of_exp_is_not_committed(dut):
    b, h = await open_session(dut)
    await h.transact(b"warm")  # exp = 1
    exp = mod(1)
    for k in range(1, SEQ_MOD):  # every sequence number that is not exp
        seq = mod(exp + k)
        b.send(h.req(bytes([k]) * (1 + k % 7), seq=seq))
        fr = await h.collect()
        assert len(fr) == 1 and fr[0].kind == "status", (k, fr)
        status_ok(fr[0], seq=mod(1), exp=exp, cursor=h.consumed)
        s = b.snapshot()
        assert s["m_count"] == 1 and s["link_exp"] == exp, (k, s)
    # go-back-N: the right request and its successors now succeed, the earlier bytes are gone
    n = min(W, 5)
    pays = [bytes([0xA0 + i]) * (3 + i) for i in range(n)]
    for p in pays:
        b.send(h.req(p))
    fr = data_frames(await h.collect(n))
    assert len(fr) == n, fr
    for i, f in enumerate(fr):
        check_fields(f, seq=mod(1 + i), payload=resp_payload(1 + i, pays[i]))
    assert b.sig("m_count") == 1 + n


@cocotb.test(skip=W < 4)
async def lost_request_in_a_pipeline(dut):
    """Requests e, (e+1 lost), e+2, e+3: only e is committed. Go-back-N from e+1 then runs all.
    Needs W >= 4."""
    b, h = await open_session(dut)
    p = [bytes([0x50 + i]) * (4 + i) for i in range(4)]
    b.send(h.req(p[0], seq=0))
    b.send(h.req(p[2], seq=2))
    b.send(h.req(p[3], seq=3))
    fr = await h.collect(1)
    assert b.sig("m_count") == 1 and b.sig("link_exp") == 1, b.snapshot()
    assert len(data_frames(fr)) == 1 and data_frames(fr)[0].p["seq"] == 0
    assert all(f.kind in ("data", "status") for f in fr)
    assert 1 <= len([f for f in fr if f.kind == "status"]) <= 2, fr  # one owe flag may merge two
    # retransmit from exp (the status frames told the host exp = 1)
    for i in (1, 2, 3):
        b.send(h.req(p[i], seq=i, ack=1))
    fr = data_frames(await h.collect(3))
    assert [f.p["seq"] for f in fr] == [1, 2, 3], fr
    for f, i in zip(fr, (1, 2, 3)):
        assert f.p["payload"] == resp_payload(i, p[i]), (i, f)
    assert b.sig("m_count") == 4


@cocotb.test()
async def duplicates_in_a_burst_never_execute(dut):
    b, h = await open_session(dut, rx_gap=10)
    await h.transact(b"x")
    for _ in range(4):
        b.send(h.req(b"dup", seq=0))
    fr = await h.collect()
    assert 1 <= len(fr) <= 4 and all(f.kind == "status" for f in fr), fr
    assert b.sig("m_count") == 1


# ---------------------------------------------------------------------------------------------
# Lost response, REWIND


async def send_requests(b, h, n, size=5, ack=0):
    pays = [bytes([0x10 + i]) * (size + i) for i in range(n)]
    for p in pays:
        b.send(h.req(p, ack=ack))
    return pays


def same_response(a, orig, exp):
    """A replay carries the same seq, ctl, len and payload. The exp field is the current exp."""
    for k in ("kind", "ctl", "seq", "len", "payload", "trunc"):
        if a.p[k] != orig.p[k]:
            raise TestFailure(f"replay differs in {k}: {a.p} vs {orig.p}")
    if a.p["exp"] != exp:
        raise TestFailure(f"replay exp {a.p['exp']} != {exp}")


async def rewind_scenario(dut, rsp_limit=None, size=5):
    b, h = await open_session(dut, rsp_limit=rsp_limit)
    lim = h.rsp_limit - 12
    n = min(W, 4)
    pays = await send_requests(b, h, n, size)
    first = data_frames(await h.collect(n))
    assert [f.p["seq"] for f in first] == list(range(n)), first
    h.count = n
    for i, f in enumerate(first):
        full = resp_payload(i, pays[i])
        assert f.p["payload"] == full[:lim] and f.p["trunc"] == (len(full) > lim), f
    consumed_before = h.consumed
    # --- REWIND: the host did not see the responses and sends epoch 1 with ack 0
    h.rewind = 1
    b.send(h.hdr(ack=0))
    rep = await h.collect(n)
    assert [f.kind for f in rep] == ["data"] * n, rep
    for a, o in zip(rep, first):
        same_response(a, o, exp=mod(n))
    # the replays are charged to the cursor (absorb checked every frame's cursor)
    assert h.consumed == (consumed_before + sum(f.wire_len for f in rep)) & 0xFFFF
    s = b.snapshot()
    assert s["link_rwHost"] == 1 and s["link_rwDone"] == 1 and s["m_count"] == n, s
    # --- the same epoch again: no replay (a POLL gets one status frame)
    b.send(h.hdr(ack=0))
    await quiet_check(b)
    b.send(h.hdr(ack=0, poll=True))
    fr = await h.collect()
    assert [f.kind for f in fr] == ["status"], fr
    if W == 1:
        # one response outstanding is the whole window: epochs replay it again and again
        for epoch in (2, 3, 0, 1):
            h.rewind = epoch
            b.send(h.hdr(ack=0))
            rep = await h.collect(1)
            assert len(rep) == 1 and rep[0].p["seq"] == 0, (epoch, rep)
            same_response(rep[0], first[0], exp=mod(n))
        # acknowledge it: a new epoch has nothing to replay
        h.rewind = 2
        b.send(h.hdr(ack=1))
        await quiet_check(b)
        # a request that carries a new epoch: nothing to replay, only the new response
        h.rewind = 3
        b.send(h.req(b"carried", ack=1))
        rep = data_frames(await h.collect(1))
        assert [f.p["seq"] for f in rep] == [1], rep
        assert rep[0].p["payload"] == resp_payload(n, b"carried")[:lim]
        return
    b.send(h.req(b"same epoch", ack=0))
    fr = await h.collect(1)
    assert [f.kind for f in fr].count("data") == 1, fr  # only the new response, no replay
    d = data_frames(fr)[0]
    assert d.p["seq"] == mod(n) and d.p["payload"] == resp_payload(n, b"same epoch")[:lim]
    h.count += 1
    # --- epoch 2 with ack = 2 (n >= 3): replay only the responses from 2 on
    k = 2 if n >= 3 else 0
    h.rewind = 2
    b.send(h.hdr(ack=k))
    rep = data_frames(await h.collect())
    assert [f.p["seq"] for f in rep] == [mod(i) for i in range(k, n + 1)], rep
    for a in rep[: n - k]:
        same_response(a, first[a.p["seq"]], exp=mod(n + 1))
    # --- epochs 3, 0, 1 (modulo 4), each with the same ack, replay again each time
    for epoch in (3, 0, 1):
        h.rewind = epoch
        b.send(h.hdr(ack=k))
        rep = data_frames(await h.collect())
        assert [f.p["seq"] for f in rep] == [mod(i) for i in range(k, n + 1)], (epoch, rep)
    # --- REWIND carried by a request: the replays come first, then the new response
    h.rewind = 2
    b.send(h.req(b"carried", ack=k))
    rep = data_frames(await h.collect(n - k + 2))
    assert [f.p["seq"] for f in rep] == [mod(i) for i in range(k, n + 2)], rep
    assert rep[-1].p["payload"] == resp_payload(n + 1, b"carried")[:lim]
    # --- everything acknowledged: a new epoch has nothing to send
    h.rewind = 3
    b.send(h.hdr(ack=mod(n + 2)))
    await quiet_check(b)


@cocotb.test()
async def lost_response_is_replayed_by_rewind(dut):
    await rewind_scenario(dut)


@cocotb.test()
async def replayed_truncated_responses_stay_truncated(dut):
    await rewind_scenario(dut, rsp_limit=20, size=10)


@cocotb.test()
async def rewind_while_a_frame_is_in_flight(dut):
    """REWIND arrives while the first response frame is being sent. The frame in flight ends,
    then the replay starts from the delivery ACK."""
    b, h = await open_session(dut, tx_gap=40)
    n = min(W, 3)
    tx_before = len(b.tx_bytes)  # the OPEN reply is already on the wire
    pays = await send_requests(b, h, n, size=30)
    await b.until(lambda: len(b.tx_bytes) >= tx_before + 8, 5000, "first response bytes")
    h.rewind = 1
    b.send(h.hdr(ack=0))
    await b.until(
        lambda: len([f for f in b.peek_frames() if f.kind == "data"]) >= 1 + n, 80000, "frames"
    )
    await b.settle()
    fr = data_frames(h.absorb(b.frames()))
    seqs = [f.p["seq"] for f in fr]
    assert seqs == [0] + list(range(n)), seqs
    same_response(fr[1], fr[0], exp=mod(n))
    for f in fr[1:]:
        assert f.p["payload"] == resp_payload(f.p["seq"], pays[f.p["seq"]]), f
    # the first frame of each response was a first transmission: hi advanced once per response
    assert b.sig("link_hi") == mod(n)


# ---------------------------------------------------------------------------------------------
# ACK validation


@cocotb.test()
async def ack_for_an_unsent_response_ignores_the_whole_frame(dut):
    b, h = await open_session(dut)
    # nothing sent yet (hi = 0, dack = 0): ack = 1 is beyond the first-transmission frontier
    snap = b.snapshot()
    bad = dict(seq=0, ack=1, grant=0x5555, rewind=1, poll=True, data=True)
    b.send(h.wire(payload=b"must not run", **bad))
    await quiet_check(b, snap)
    assert b.sig("m_count") == 0
    # the same frame with a valid ack is accepted: exp, grant, REWIND epoch, a status reply
    ok = dict(bad, ack=0)
    b.send(h.wire(payload=b"runs", **ok))
    fr = await h.collect(1)
    s = b.snapshot()
    assert s["m_count"] == 1 and s["link_grant"] == 0x5555 and s["link_rwHost"] == 1, s
    assert sorted(f.kind for f in fr) == ["data", "status"], fr
    h.count = 1
    h.rewind = 1


@cocotb.test()
async def ack_beyond_frontier_after_traffic(dut):
    """Responses 0..n-1 sent (hi = n): ack n is valid, n+1.. is not; an ack that goes backwards
    is not valid either. Invalid frames change nothing and get no reply."""
    b, h = await open_session(dut)
    n = min(W, 4)
    await send_requests(b, h, n)
    await h.collect(n)
    h.count = n
    s0 = b.snapshot()
    assert s0["link_hi"] == mod(n) and s0["link_dack"] == 0
    for ack in range(n + 1, SEQ_MOD):
        snap = b.snapshot()
        b.send(
            h.wire(data=True, seq=mod(n), ack=ack, grant=0x1357, rewind=2, poll=True, payload=b"no")
        )
        await quiet_check(b, snap)
    # ack = n (the frontier) is valid
    b.send(h.wire(ack=n, grant=0x1357, rewind=0))
    await b.settle()
    s = b.snapshot()
    assert s["link_dack"] == mod(n) and s["link_grant"] == 0x1357, s
    h.ack = mod(n)
    # now backwards: every ack except n is invalid (the frontier equals dack)
    for ack in range(SEQ_MOD):
        if ack == mod(n):
            continue
        snap = b.snapshot()
        b.send(h.wire(data=True, seq=mod(n), ack=ack, grant=0x2468, poll=True, payload=b"no"))
        await quiet_check(b, snap)
    assert b.sig("m_count") == n


@cocotb.test(skip=W == 1)
async def ack_window_with_wrapped_counters(dut):
    """dack = 2W-2, hi = 3: the valid acks are the modular range from dack to hi (needs W > 1)."""
    b, h = await open_session(dut)
    for i in range(SEQ_MOD - 2):  # acknowledge every response: dack = 2W - 2
        await h.transact(bytes([i]))
    for i in range(5):  # five more responses, none acknowledged
        b.send(h.req(bytes([100 + i]), ack=h.ack))
    await h.collect(5)
    h.count += 5
    s = b.snapshot()
    dack, hi = s["link_dack"], s["link_hi"]
    assert dack == SEQ_MOD - 2 and hi == mod(SEQ_MOD - 2 + 5), s
    valid = {mod(dack + i) for i in range(6)}
    # invalid values first (they change nothing), then the valid ones in increasing order
    for ack in range(SEQ_MOD):
        if ack in valid:
            continue
        snap = b.snapshot()
        b.send(h.wire(ack=ack, grant=0x0BAD))
        await quiet_check(b, snap, clocks=100)
    for i in range(6):
        ack = mod(dack + i)
        b.send(h.wire(ack=ack, grant=0x1000 + i))
        await b.settle()
        s = b.snapshot()
        assert s["link_dack"] == ack and s["link_grant"] == 0x1000 + i, (ack, s)


@cocotb.test()
async def ack_covering_a_replayed_response_waits_for_the_replay(dut):
    """The core's next write to a page waits until the replay of that page ends.

    The window is full (W responses, one long). REWIND replays response 0. During that replay,
    an ACK for response 0 arrives with request W. The ACK is valid (it is not beyond the
    frontier), so dack moves and the window has room. But TX still reads page 0, the page that
    the core needs for response W. `responseReady` stays false until the replay frame ends.
    """
    b, h = await open_session(dut, tx_gap=40)
    long_n = 100 if MAXLEN > 100 else 50
    b.send(h.req(bytes([7]) * long_n, ack=0))
    for i in range(1, W):
        b.send(h.req(bytes([i]) * 4, ack=0))
    await h.collect(W)
    h.count = W
    s = b.snapshot()
    assert s["link_wr"] == mod(W) and s["link_hi"] == mod(W) and s["link_dack"] == 0, s
    assert int(dut.responseReady.value) == 0, "the response window is full"
    # REWIND: replay response 0 (long)
    h.rewind = 1
    b.send(h.hdr(ack=0))
    await b.until(lambda: b.sig("link_txActive") == 1, 3000, "replay starts")
    assert b.sig("link_txq") == 0 and b.sig("link_txIsData") == 1
    await b.run(300)
    assert b.sig("link_txActive") == 1 and b.sig("link_txq") == 0, "replay of response 0 in flight"
    # record per-clock state from here on
    rec = []
    b.hooks.append(
        lambda bb, clk: rec.append(
            (
                clk,
                int(dut.responseReady.value),
                bb.sig("link_txActive"),
                bb.sig("link_txq"),
                bb.sig("m_count"),
            )
        )
    )
    # ACK for response 0 and request W
    b.send(h.req(b"late", seq=mod(W), ack=1, rewind=1))
    await b.until(lambda: b.sig("m_phase") != 0, 5000, "core takes request W")
    t_block = b.clk
    assert b.sig("link_dack") == 1
    await b.until(lambda: b.sig("m_count") == W + 1, 60000, "request W executed")
    b.hooks.pop()
    # when did the replay frame of response 0 end? (txq leaves 0 while txActive falls)
    end = next(c for c, rr, act, q, cnt in rec if c > t_block and q != 0)
    in_window = [(c, rr, cnt) for c, rr, act, q, cnt in rec if t_block <= c < end]
    assert in_window, "the replay ended before the core was blocked"
    assert all(
        rr == 0 and cnt == W for c, rr, cnt in in_window
    ), "responseReady rose during the replay"
    after = [(c, rr) for c, rr, act, q, cnt in rec if c >= end]
    first_ready = next(c for c, rr in after if rr == 1)
    first_exec = next(c for c, rr, act, q, cnt in rec if cnt == W + 1)
    dut._log.info(
        "replay-page wait: core blocked at clock %d, replay ended at %d, responseReady at "
        "%d, executed at %d (blocked %d clocks)",
        t_block,
        end,
        first_ready,
        first_exec,
        first_ready - t_block,
    )
    assert first_ready - end <= 3 and first_exec >= first_ready
    fr = data_frames(await h.collect(W + 1, limit=200000))  # replays of 0..W-1, then response W
    assert [f.p["seq"] for f in fr] == [mod(i) for i in range(W + 1)], fr
    assert fr[-1].p["payload"] == resp_payload(W, b"late")
    assert b.sig("m_count") == W + 1
