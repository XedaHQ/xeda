"""Frame rejection. A bad frame changes no state and, when the FPGA is bound, gets no reply.

Every mutant is checked with the reference codec first (it must be invalid). Then the RTL must
send no byte, and the registers in `STATE_REGS` must be the same as before (grant, ack, REWIND
epoch, exp, owe, cursor, session). After every group, a good request must be accepted and answered
with the exact count.
"""

import random

import cocotb

import lean_tb as tb
from lean_tb import (
    MAXLEN,
    W,
    Bench,
    Host,
    TestFailure,
    body,
    delete_byte,
    flip_bit,
    insert_byte,
    lc,
    mod,
    truncate,
)

SID = 0x5EED1234  # all four bytes nonzero
PAY = bytes([7, 0, 200, 3, 0, 0, 9, 1, 2, 0, 255, 128, 64, 0, 5, 6, 7, 8, 9, 10])  # zeros inside


async def setup(dut, rx_gap=12):
    b = await Bench.create(dut, rx_gap=rx_gap, tx_gap=12)
    h = Host(b, SID)
    await h.open()
    await h.transact(b"warm-up")  # executed = 1, so a stray count would show
    return b, h


def base(h, **kw):
    """A valid DATA request (seq = next) that carries a changed grant, REWIND epoch and POLL."""
    args = dict(
        data=True,
        poll=True,
        rewind=(h.rewind + 1) % 4,
        seq=h.seq,
        ack=h.ack,
        grant=(h.grant_for() + 77) & 0xFFFF,
        payload=PAY,
    )
    args.update(kw)
    return h.wire(**args)


def check_codec_rejects(h, label, wire):
    """The reference codec must judge the mutant frame invalid."""
    v = lc.check_host_frame(body(wire), h.sid, W, MAXLEN)
    if v.frame is not None:
        raise TestFailure(f"{label}: the reference codec says the mutant is valid: {v.frame}")


async def expect_rejected(b, h, cases, burst=False):
    """Send each (label, wire); no reply and no state change."""
    for label, wire in cases:
        check_codec_rejects(h, label, wire)
    if burst:
        snap = b.snapshot()
        n0 = len(b.tx_bytes)
        for _, wire in cases:
            b.send(wire)
        await b.settle()
        await b.run(300)
        if len(b.tx_bytes) != n0:
            raise TestFailure(f"burst: {len(b.tx_bytes) - n0} bytes sent: {b.peek_frames()}")
        if b.snapshot() != snap:
            raise TestFailure(f"burst: state changed {snap} -> {b.snapshot()}")
        return
    for label, wire in cases:
        snap = b.snapshot()
        n0 = len(b.tx_bytes)
        b.send(wire)
        await b.settle()
        await b.run(250)
        if len(b.tx_bytes) != n0:
            raise TestFailure(f"{label}: {len(b.tx_bytes) - n0} bytes sent: {b.peek_frames()}")
        after = b.snapshot()
        if after != snap:
            diff = {k: (snap[k], after[k]) for k in snap if snap[k] != after[k]}
            raise TestFailure(f"{label}: state changed {diff}")


async def still_good(b, h, n=2):
    for i in range(n):
        await h.transact(bytes([i]) * (3 + i))


def corrupt_cases(h):
    w = base(h)
    n = len(body(w))
    cases = []
    for pos in range(n):
        cases.append((f"flip pos {pos}", flip_bit(w, pos, (pos * 3) % 8)))
    for pos in range(n - 5, n):  # the CRC bytes and the last code: all eight bits
        for bit in range(8):
            cases.append((f"flip pos {pos} bit {bit}", flip_bit(w, pos, bit)))
    return cases


@cocotb.test()
async def control_the_unmodified_frame_is_accepted(dut):
    """Positive control: the frame that the mutants start from is valid and changes the state."""
    b, h = await setup(dut)
    snap = b.snapshot()
    w = base(h)
    assert lc.check_host_frame(body(w), h.sid, W, MAXLEN).frame is not None
    h.rewind = (h.rewind + 1) % 4
    b.send(w)
    fr = await h.collect(1)  # POLL gives a status frame, the request gives a DATA frame
    after = b.snapshot()
    assert after["m_count"] == snap["m_count"] + 1, (snap, after)
    assert after["link_grant"] != snap["link_grant"], (snap, after)
    assert after["link_rwHost"] == h.rewind, (snap, after)
    assert after["link_exp"] == mod(snap["link_exp"] + 1), (snap, after)
    assert sorted(f.kind for f in fr) == ["data", "status"], fr
    data = [f for f in fr if f.kind == "data"][0]
    tb.check_fields(data, payload=tb.resp_payload(h.count, PAY))
    h.seq = mod(h.seq + 1)
    h.count += 1
    h.ack = mod(h.ack + 1)
    await still_good(b, h)


@cocotb.test()
async def corrupted_byte(dut):
    b, h = await setup(dut)
    await expect_rejected(b, h, corrupt_cases(h))
    await still_good(b, h)


@cocotb.test()
async def deleted_byte(dut):
    b, h = await setup(dut)
    w = base(h)
    cases = [(f"delete pos {p}", delete_byte(w, p)) for p in range(len(body(w)))]
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def inserted_byte(dut):
    b, h = await setup(dut)
    w = base(h)
    rng = random.Random(11)
    cases = []
    for p in range(len(body(w)) + 1):
        cases.append((f"insert pos {p}", insert_byte(w, p, rng.randrange(1, 256))))
    cases.append(("insert 0x01 at the end", insert_byte(w, len(body(w)), 1)))
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def truncated_frame(dut):
    b, h = await setup(dut)
    w = base(h)
    n = len(body(w))
    cases = [(f"keep {k} of {n}", truncate(w, k)) for k in range(1, n)]
    cases.append(("header-only frame cut", truncate(h.hdr(poll=True), 5)))
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def merged_frames(dut):
    """Two frames without the delimiter between them (a lost delimiter)."""
    b, h = await setup(dut)
    w1 = base(h)
    w2 = base(h, seq=mod(h.seq + 1), payload=b"second")
    hdr = h.hdr(poll=True)
    cases = [
        ("data + data", body(w1) + body(w2) + b"\x00"),
        ("data + header-only", body(w1) + body(hdr) + b"\x00"),
        ("header-only + data", body(hdr) + body(w1) + b"\x00"),
        ("header-only + header-only", body(hdr) + body(hdr) + b"\x00"),
        ("data x3", body(w1) + body(w2) + body(w1) + b"\x00"),
    ]
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def bad_header_fields(dut):
    b, h = await setup(dut)
    cases = []
    good_ctl = lc.host_ctl(data=True, poll=True, rewind=(h.rewind + 1) % 4)
    for ver in (0b00, 0b01, 0b11):  # bad version
        cases.append((f"version {ver:02b}", base(h, ctl=(good_ctl & 0x3F) | (ver << 6))))
        cases.append(
            (
                f"version {ver:02b}, header-only",
                h.hdr(ctl=(lc.host_ctl(poll=True) & 0x3F) | (ver << 6)),
            )
        )
    for v in (tb.SEQ_MOD, tb.SEQ_MOD + 1, 2 * tb.SEQ_MOD + h.seq, 128, 255):
        cases.append((f"seq {v}", base(h, seq=v)))
        cases.append((f"ack {v}", base(h, ack=v)))
        cases.append((f"ack {v}, header-only", h.hdr(poll=True, ack=v)))
    cases.append(("seq = 2W + exp", base(h, seq=tb.SEQ_MOD + h.seq)))
    # len field
    for lf in (0, 1, 11, 13, 255):
        cases.append((f"len {lf} with 20 payload bytes", base(h, len_field=lf)))
    cases.append(
        ("len 32 with 20 payload bytes (off by 0 from decoded - 0)", base(h, len_field=33))
    )
    cases.append(("len 31", base(h, len_field=31)))
    for lf in (0, 11, 13):
        cases.append((f"len {lf}, header-only", h.hdr(poll=True, len_field=lf)))
    cases.append(("len 12 with a data payload", base(h, len_field=12)))
    # header-only frame with a payload (len 13): DATA = 0 requires len = 12
    cases.append(("header-only with 1 payload byte", h.hdr(poll=True, payload=b"x")))
    # a frame longer than maxLen with a consistent len field (possible only when maxLen < 255)
    if MAXLEN < 255:
        for n in (MAXLEN - 12 + 1, 100, 243):
            cases.append((f"len {n + 12} > maxLen {MAXLEN}", base(h, payload=bytes([5]) * n)))
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def bad_open(dut):
    b, h = await setup(dut)
    new = 0x0BADCAFE
    g = (h.grant_for() + 77) & 0xFFFF
    sidb = new.to_bytes(4, "little")
    cases = [
        ("OPEN payload 4 bytes (len 16)", h.open_wire(payload=sidb, grant=g)),
        ("OPEN payload 6 bytes (len 18)", h.open_wire(payload=sidb + bytes([255, 0]), grant=g)),
        ("OPEN payload 0 bytes (len 12)", h.open_wire(payload=b"", grant=g)),
        ("OPEN payload 10 bytes", h.open_wire(payload=sidb + bytes([255]) * 6, grant=g)),
        ("OPEN rspLimit 0", h.open_wire(sid=new, rsp_limit=0, grant=g)),
        ("OPEN rspLimit 11", h.open_wire(sid=new, rsp_limit=11, grant=g)),
        ("OPEN rspLimit 1", h.open_wire(sid=new, rsp_limit=1, grant=g)),
        ("OPEN with DATA set", h.open_wire(sid=new, grant=g, data=True)),
        ("OPEN seq = 2W", h.open_wire(sid=new, grant=g, seq=tb.SEQ_MOD)),
        ("OPEN ack = 2W", h.open_wire(sid=new, grant=g, ack=tb.SEQ_MOD)),
        ("OPEN with trailer XOR the old session", h.open_wire(sid=new, grant=g, crc_sid=h.sid)),
        ("OPEN with trailer XOR the new session", h.open_wire(sid=new, grant=g, crc_sid=new)),
        ("OPEN len field 16 for 5 payload bytes", h.open_wire(sid=new, grant=g, len_field=16)),
        ("OPEN len field 18 for 5 payload bytes", h.open_wire(sid=new, grant=g, len_field=18)),
    ]
    # rspLimit above maxLen is valid (the FPGA clamps it): see lean_session
    # a bad version in an OPEN frame
    ctl = lc.host_ctl(open_frame=True)
    pl = sidb + bytes([MAXLEN])
    for label, c in (
        ("OPEN version 11", (ctl & 0x3F) | 0xC0),
        ("OPEN version 01", (ctl & 0x3F) | 0x40),
    ):
        dec = lc.build_host_frame(c, 0, 0, g, pl, 0)
        cases.append((label, lc.encode_host_wire(dec)))
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def wrong_session_in_trailer(dut):
    """The session ID is folded into the CRC: a frame under another ID is invalid."""
    b, h = await setup(dut)
    s = h.sid
    cases = [
        ("trailer XOR 0", base(h, sid=0)),
        ("trailer XOR sid^1", base(h, sid=s ^ 1)),
        ("trailer XOR sid^0x80000000", base(h, sid=s ^ 0x80000000)),
        ("trailer XOR other sid", base(h, sid=0x13572468)),
        ("header-only XOR 0", h.hdr(poll=True, sid=0)),
        ("header-only XOR other", h.hdr(poll=True, sid=s ^ 0x100)),
        ("header-only POLL XOR 0xFFFFFFFF", h.hdr(poll=True, sid=0xFFFFFFFF)),
    ]
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def overlong_and_tiny(dut):
    b, h = await setup(dut)
    rng = random.Random(5)
    cases = []
    for n in (300, 600, 1000):
        junk = bytes(rng.randrange(1, 256) for _ in range(n))
        cases.append((f"{n} nonzero bytes", junk + b"\x00"))
    # a long frame whose code bytes are consistent (valid COBS), decoded length far above len
    dec = lc.build_host_frame(
        lc.host_ctl(data=True), h.seq, h.ack, h.grant_for(), bytes(400), h.sid, 255
    )
    cases.append(("valid COBS, decoded 410 bytes, len 255", lc.encode_host_wire(dec)))
    dec = lc.build_host_frame(
        lc.host_ctl(data=True), h.seq, h.ack, h.grant_for(), bytes(250), h.sid, 255
    )
    cases.append(("decoded 260 bytes, len 255", lc.encode_host_wire(dec)))
    for k in (1, 2, 3, 5, 9, 0xFF):
        cases.append((f"single byte {k:#x}", bytes([k]) + b"\x00"))
    cases.append(("code 2 then data", bytes([2, 9]) + b"\x00"))
    cases.append(("ten bytes of 0x01", bytes([1] * 10) + b"\x00"))
    cases.append(("six zeros of data", bytes([1] * 6) + b"\x00"))
    await expect_rejected(b, h, cases)
    await still_good(b, h)


@cocotb.test()
async def empty_frames_are_ignored(dut):
    """Consecutive delimiters are not frames: no state change and no reply (bound)."""
    b, h = await setup(dut)
    snap = b.snapshot()
    n0 = len(b.tx_bytes)
    b.send(b"\x00" * 20)
    await b.settle()
    await b.run(250)
    assert len(b.tx_bytes) == n0 and b.snapshot() == snap
    # empty frames around a good frame do not disturb it
    seq = h.seq
    b.send(b"\x00\x00" + h.req(b"x") + b"\x00\x00\x00")
    fr = await h.collect(1)
    tb.check_fields(fr[0], seq=seq, payload=tb.resp_payload(h.count, b"x"))
    h.count += 1
    h.ack = mod(h.ack + 1)


@cocotb.test()
async def burst_of_bad_frames(dut):
    """All mutants in one stream at the fastest sustainable byte rate, then a good request."""
    b, h = await setup(dut, rx_gap=10)
    cases = corrupt_cases(h)
    w = base(h)
    cases += [(f"delete {p}", delete_byte(w, p)) for p in range(len(body(w)))]
    cases += [(f"truncate {k}", truncate(w, k)) for k in range(1, len(body(w)))]
    rng = random.Random(3)
    cases += [
        (f"insert {p}", insert_byte(w, p, rng.randrange(1, 256))) for p in range(len(body(w)))
    ]
    await expect_rejected(b, h, cases, burst=True)
    await still_good(b, h)
    dut._log.info("burst: %d bad frames, RX max waiting %d", len(cases), b.rx_max_waiting)


@cocotb.test()
async def random_garbage_streams(dut):
    """Random bytes (many zeros, so many short frames, some with 0xFF codes): no reply, no state
    change. Then a good request works."""
    b, h = await setup(dut, rx_gap=10)
    rng = random.Random(77)
    snap = b.snapshot()
    n0 = len(b.tx_bytes)
    total = 0
    for k in range(60):
        n = rng.randrange(1, 400)
        z = rng.choice([0.01, 0.05, 0.2])
        stream = (
            bytes(
                0 if rng.random() < z else (0xFF if rng.random() < 0.05 else rng.randrange(1, 255))
                for _ in range(n)
            )
            + b"\x00"
        )
        total += len(stream)
        b.send(stream)
    await b.settle()
    await b.run(500)
    assert len(b.tx_bytes) == n0, b.peek_frames()
    assert b.snapshot() == snap
    dut._log.info("garbage: %d bytes, RX max waiting %d", total, b.rx_max_waiting)
    await still_good(b, h)


@cocotb.test()
async def open_with_resync_poll_rewind_bits_is_valid_and_ignores_them(dut):
    """doc/protocol.md: OPEN ignores POLL and REWIND (and RESYNC): the frame is valid and there is
    one reply, no extra status frame, no replay, no permit."""
    b, h = await setup(dut)
    for ctl_bits in (1, 0x08, 0x06, 0x08 | 0x06 | 1):
        hn = Host(b, tb.mk_sid(50 + ctl_bits))
        b.sid = hn.sid
        b.frames()
        rw0 = b.sig("link_rwHost")
        ctl = lc.host_ctl(open_frame=True) | ctl_bits
        dec = lc.build_host_frame(ctl, 0, 0, 0, hn.sid.to_bytes(4, "little") + bytes([MAXLEN]), 0)
        b.send(lc.encode_host_wire(dec))
        await b.until(lambda: bool(b._new), 8000, "OPEN reply")
        await b.settle()
        await b.run(300)
        fr = hn.absorb(b.frames())
        assert len(fr) == 1 and fr[0].kind == "status", (ctl_bits, fr)
        tb.status_ok(fr[0], seq=0, exp=0, cursor=12)
        s = b.snapshot()
        assert s["link_sid"] == hn.sid and s["link_permit"] == 0 and s["link_rwHost"] == rw0, s
        hn.count = h.count
        await hn.transact(b"after")
        h = hn


@cocotb.test()
async def extra_decoded_bytes_after_the_trailer(dut):
    """A valid frame followed by decoded bytes that continue the trailer check pattern. After the
    four trailer bytes the CRC register is zero, so the next expected byte is 0xFF for S = 0
    (OPEN frames) and the complement of the session ID bytes for a session. Such a frame has a
    decoded length above len - 2, so it is invalid."""
    b, h = await setup(dut)
    new = 0x0BADCAFE
    g = (h.grant_for() + 77) & 0xFFFF
    open_dec = lc.build_host_frame(
        lc.host_ctl(open_frame=True), 0, 0, g, new.to_bytes(4, "little") + bytes([MAXLEN]), 0
    )
    cases = []
    for k in (1, 2, 3, 4, 5):
        cases.append((f"OPEN + {k} bytes 0xFF", lc.encode_host_wire(open_dec + b"\xff" * k)))
    sid_comp = bytes(~x & 0xFF for x in h.sid.to_bytes(4, "little"))
    data_dec = lc.build_host_frame(lc.host_ctl(data=True, poll=True), h.seq, h.ack, g, PAY, h.sid)
    for k in (1, 2, 3, 4, 5):
        cases.append(
            (f"DATA + {k} complement bytes", lc.encode_host_wire(data_dec + (sid_comp * 2)[:k]))
        )
    hdr_dec = lc.build_host_frame(lc.host_ctl(poll=True), h.seq, h.ack, g, b"", h.sid)
    cases.append(("header-only + 1 complement byte", lc.encode_host_wire(hdr_dec + sid_comp[:1])))
    cases.append(("header-only + 4 complement bytes", lc.encode_host_wire(hdr_dec + sid_comp)))
    # control: without the extra bytes the frames are valid under the codec
    assert (
        lc.check_host_frame(lc.encode_host_wire(open_dec)[:-1], h.sid, W, MAXLEN).frame is not None
    )
    assert (
        lc.check_host_frame(lc.encode_host_wire(data_dec)[:-1], h.sid, W, MAXLEN).frame is not None
    )
    await expect_rejected(b, h, cases)
    await still_good(b, h)
