"""Performance at 83 clocks per byte in both directions (12 Mbaud at 100 MHz).

Every test logs lines that start with `METRIC` with the measured numbers.
"""

import random

import cocotb

from lean_tb import MAXLEN, W, Bench, Host, mod

GAP = 83
SID = 0x6A6A0101


async def open_session(dut, rx_gap=GAP, tx_gap=GAP):
    b = await Bench.create(dut, rx_gap=rx_gap, tx_gap=tx_gap)
    h = Host(b, SID)
    await h.open(limit=200000)
    return b, h


def metric(dut, name, value):
    dut._log.info("METRIC %s = %s", name, value)


def pay(rng, n):
    return bytes(rng.randrange(1, 256) for _ in range(n))


def long_payload():
    """The longest request whose response still fits: response = count + request."""
    return MAXLEN - 12 - 4


@cocotb.test()
async def rx_backlog_with_a_lost_request_and_a_long_response(dut):
    """Request 0 gets a long response. The next request is lost, so every later request is a gap
    and gets a status reply. The host sends without a pause while TX is busy with the long
    response. Measure the bytes that wait at the RTL input."""
    b, h = await open_session(dut)
    rng = random.Random(11)
    b.rx_max_waiting = b.rx_max_incl = b.rx_max_wait_clocks = 0
    acc0 = b.rx_accepted
    b.send(h.req(pay(rng, long_payload())))  # request 0, a long response (maxLen frame)
    bad_seq = mod(2)  # W = 8: one ahead of exp (the lost request 1); W = 1: a duplicate
    n_more = 6
    for i in range(n_more):
        b.send(h.req(pay(rng, rng.randrange(long_payload() // 2, long_payload() + 1)), seq=bad_seq))
    len(b.rx_q)
    await b.until(lambda: not b.rx_q and not b.rx_wait, 400000, "RX drained")
    await b.settle(limit=400000)
    fr = h.absorb(b.frames())
    kinds = [f.kind for f in fr]
    assert kinds.count("data") == 1, kinds
    assert kinds.count("status") >= 1, kinds
    data = [f for f in fr if f.kind == "data"][0]
    status = [f for f in fr if f.kind == "status"]
    # the long response was being sent while the later requests were received
    metric(dut, "rx_bytes_received", b.rx_accepted - acc0)
    metric(dut, "rx_backlog_max_bytes_waiting_after_accept", b.rx_max_waiting)
    metric(dut, "rx_backlog_max_bytes_including_arriving_byte", b.rx_max_incl)
    metric(dut, "rx_max_wait_clocks", b.rx_max_wait_clocks)
    metric(dut, "long_response_tx_clocks", data.t1 - data.t0)
    metric(dut, "status_frames", len(status))
    assert b.rx_max_waiting <= 2 and b.rx_max_incl <= 2, (b.rx_max_waiting, b.rx_max_incl)
    # not vacuous: the long frame was on the wire for its full length while the RX was busy
    assert data.t1 - data.t0 >= (data.wire_len - 1) * GAP
    assert b.sig("m_count") == 1


@cocotb.test()
async def rx_service_rate(dut):
    """Find the smallest byte gap that the RX sustains (no growing backlog). A frame of the
    maximum length is sent at gaps from 14 down to 6 clocks."""
    b, h = await open_session(dut, rx_gap=12, tx_gap=12)
    rng = random.Random(5)
    results = {}
    for gap in (14, 12, 11, 10, 9, 8, 7, 6):
        b.rx_gap = gap
        b.rx_max_waiting = b.rx_max_incl = b.rx_max_wait_clocks = 0
        seq = mod(2)  # a gap frame: rejected, so no TX traffic
        w = h.req(pay(rng, MAXLEN - 12), seq=seq)
        for _ in range(3):
            b.send(w)
        await b.until(lambda: not b.rx_q and not b.rx_wait, 100000, "RX drained")
        results[gap] = (b.rx_max_waiting, b.rx_max_wait_clocks)
        await b.settle()
        b.frames()
    metric(dut, "rx_gap_vs_(max_waiting,max_wait_clocks)", results)
    assert results[14][0] == 0 and results[12][0] == 0


@cocotb.test(skip=W == 1)
async def response_write_while_tx_is_busy(dut):
    """Request 0 gives a maximum-length response (TX busy for about 21,000 clocks). Request 1
    follows at once. Measure how long the core needs to write the next response while TX sends
    the first one."""
    b, h = await open_session(dut)
    rng = random.Random(12)
    sizes = [long_payload(), long_payload() - 4 - 39]  # 243 and 200 response bytes
    for name in ("link_exp", "m_count", "link_wActive", "link_txActive", "link_wr"):
        b.watch_signal(name)
    b.changes.clear()
    reqs = [pay(rng, n) for n in sizes]
    for p in reqs:
        b.send(h.req(p, ack=h.ack))
    await b.until(lambda: not b.rx_q and not b.rx_wait, 400000, "RX drained")
    await b.settle(limit=400000)
    fr = [f for f in h.absorb(b.frames()) if f.kind == "data"]
    assert len(fr) == 2 and b.sig("m_count") == 2
    ch = b.changes
    exp_changes = [c for c, n, v in ch if n == "link_exp"]
    t_commit1 = exp_changes[-1]  # the second request is committed
    t_exec = [c for c, n, v in ch if n == "m_count"]
    t_exec1 = t_exec[-1]
    wr_ch = [c for c, n, v in ch if n == "link_wr"]
    t_wr1 = wr_ch[-1]
    wact = [(c, v) for c, n, v in ch if n == "link_wActive"]
    t_wstart1 = [c for c, v in wact if v == 1 and c > t_commit1][0]
    tx_act = [(c, v) for c, n, v in ch if n == "link_txActive"]
    busy_at_commit = [v for c, v in tx_act if c <= t_commit1][-1]
    tx0_end = fr[0].t1
    resp1_len = fr[1].p["len"]
    metric(dut, "response1_wire_len", resp1_len)
    metric(dut, "tx_busy_with_response0_at_commit_of_request1", busy_at_commit)
    metric(dut, "commit_to_wr_increment_clocks", t_wr1 - t_commit1)
    metric(dut, "write_start_to_wr_increment_clocks", t_wr1 - t_wstart1)
    metric(dut, "commit_to_executed_clocks", t_exec1 - t_commit1)
    metric(dut, "response0_tx_end_minus_response1_written_clocks", tx0_end - t_wr1)
    metric(dut, "response0_tx_clocks", fr[0].t1 - fr[0].t0)
    assert busy_at_commit == 1, "TX was not busy when request 1 was committed"
    assert t_wr1 < tx0_end, "response 1 was written only after TX finished response 0"
    assert t_wr1 - t_commit1 <= 450, t_wr1 - t_commit1


@cocotb.test(skip=W == 1)
async def response_write_time_by_size(dut):
    """The core's write time for responses of several sizes, TX busy with a long frame."""
    b, h = await open_session(dut)
    rng = random.Random(13)
    for name in ("link_exp", "link_wr"):
        b.watch_signal(name)
    rows = []
    for n in (0, 16, 96, 196):
        b.changes.clear()
        b.send(h.req(pay(rng, long_payload()), ack=h.ack))  # a long response keeps TX busy
        await b.until(
            lambda: b.sig("link_txActive") == 1
            and b.sig("link_txIsData") == 1
            and not b.rx_q
            and not b.rx_wait,
            200000,
            "long frame in flight",
        )
        b.changes.clear()
        b.send(h.req(pay(rng, n), ack=h.ack))
        await b.until(lambda: not b.rx_q and not b.rx_wait, 100000, "RX drained")
        await b.run(20)
        await b.settle(limit=400000)
        fr = [f for f in h.absorb(b.frames()) if f.kind == "data"]
        assert len(fr) == 2
        h.ack = mod(h.ack + 2)
        t_commit = [c for c, nm, v in b.changes if nm == "link_exp"][-1]
        t_wr = [c for c, nm, v in b.changes if nm == "link_wr"][-1]
        rows.append((4 + n, t_wr - t_commit))
        h.count += 2
        assert t_wr < fr[0].t1, "written after TX finished"
    metric(dut, "response_payload_bytes_vs_commit_to_written_clocks", rows)


def idle_stats(b, frames):
    """Idle clocks between consecutive FPGA bytes of a burst of frames (a byte may go every 83
    clocks). Returns (bytes, total idle clocks, intervals with idle > 0, whole idle slots,
    max idle at a frame boundary)."""
    t0, t1 = frames[0].t0, frames[-1].t1
    bytes_ = [(c, v) for c, v in b.tx_bytes if t0 <= c <= t1]
    idle = []
    boundary = []
    for (c0, v0), (c1, v1) in zip(bytes_, bytes_[1:]):
        gap = c1 - c0 - b.tx_gap
        idle.append(max(0, gap))
        if v0 == 0:
            boundary.append(max(0, gap))
    return (
        len(bytes_),
        sum(idle),
        sum(1 for i in idle if i > 0),
        sum(i // b.tx_gap for i in idle),
        max(boundary) if boundary else 0,
    )


@cocotb.test()
async def tx_idle_with_ready_responses_fast_requests(dut):
    """Requests arrive fast (12 clocks per byte), so several responses are ready while TX sends
    one at 83 clocks per byte. Count the idle slots between frames."""
    b, h = await open_session(dut, rx_gap=12, tx_gap=GAP)
    rng = random.Random(14)
    n = min(W, 7)
    size = 60 if W > 1 else 40
    for i in range(n):
        b.send(h.req(pay(rng, size), ack=0))
        if W == 1:
            break
    n = n if W > 1 else 1
    await b.until(
        lambda: len([f for f in b.peek_frames() if f.kind == "data"]) >= n, 400000, "frames"
    )
    await b.settle(limit=400000)
    fr = [f for f in h.absorb(b.frames()) if f.kind == "data"]
    assert len(fr) == n
    nb, idle, parts, slots, bnd = idle_stats(b, fr)
    metric(
        dut,
        "tx_idle_fast_requests: frames, bytes, idle_clocks, intervals_with_idle, idle_slots, "
        "max_idle_at_frame_boundary",
        (n, nb, idle, parts, slots, bnd),
    )
    assert slots == 0 and bnd < GAP


@cocotb.test()
async def tx_idle_with_ready_responses_at_83(dut):
    """Both directions at 83 clocks per byte. Short requests (12-byte frames) give 16-byte
    responses, so TX falls behind and responses wait. Count idle slots between frames, from
    the first byte of the second response on (a response waits only if it was ready)."""
    b, h = await open_session(dut)
    n = min(W, 7)
    for i in range(n):
        b.send(h.req(b"", ack=0))
        if W == 1:
            break
    n = n if W > 1 else 1
    await b.until(
        lambda: len([f for f in b.peek_frames() if f.kind == "data"]) >= n, 600000, "frames"
    )
    await b.settle(limit=600000)
    fr = [f for f in h.absorb(b.frames()) if f.kind == "data"]
    assert len(fr) == n
    if n > 1:
        # ready times: the request k is committed after (k + 1) * 12 * 83 clocks of RX
        nb, idle, parts, slots, bnd = idle_stats(b, fr)
        metric(
            dut,
            "tx_idle_at_83: frames, bytes, idle_clocks, intervals_with_idle, idle_slots, "
            "max_idle_at_frame_boundary",
            (n, nb, idle, parts, slots, bnd),
        )
        gaps = [fr[i + 1].t0 - fr[i].t1 for i in range(n - 1)]
        metric(dut, "tx_gap_delimiter_to_next_first_byte_clocks", gaps)
        assert max(gaps) - GAP <= 2, gaps


@cocotb.test()
async def tx_idle_all_ready_before_tx_starts(dut):
    """All responses are ready before the PHY takes the first byte (the PHY is held off). Then TX
    runs at 83 clocks per byte. There must be no idle slot at all."""
    b, h = await open_session(dut, rx_gap=12, tx_gap=GAP)
    rng = random.Random(15)
    n = min(W, 7)
    b.tx_hold = True
    for i in range(n):
        b.send(h.req(pay(rng, 30 + 10 * i), ack=0))
        if W == 1:
            break
    n = n if W > 1 else 1
    await b.run(8000)
    assert b.sig("link_wr") == mod(n), b.snapshot()
    b.tx_hold = False
    await b.until(
        lambda: len([f for f in b.peek_frames() if f.kind == "data"]) >= n, 600000, "frames"
    )
    await b.settle(limit=600000)
    fr = [f for f in h.absorb(b.frames()) if f.kind == "data"]
    assert len(fr) == n
    nb, idle, parts, slots, bnd = idle_stats(b, fr)
    total = fr[-1].t1 - fr[0].t0
    metric(
        dut,
        "tx_idle_all_ready: frames, bytes, idle_clocks, intervals_with_idle, idle_slots, "
        "max_idle_at_frame_boundary, burst_clocks",
        (n, nb, idle, parts, slots, bnd, total),
    )
    assert slots == 0 and bnd < GAP
    assert idle <= 0.01 * total
