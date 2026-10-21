"""Random stress with faults in both directions.

For each seed a simple host (written here) runs a session against the RTL:
- OPEN with retries, then N requests with a window of W, go-back-N from the `exp` that the FPGA
  reports, REWIND (epoch + 1) when a response is missing, POLL on a timeout, RESYNC when no FPGA
  byte arrived for a long time, `grant = consumed + B - R`.
- Faults on the host to FPGA bytes: lost, inserted and substituted bytes, truncated, dropped and
  duplicated frames, lost delimiters. Faults on the FPGA to host bytes: lost, inserted and
  substituted bytes, and one blackout (every byte lost for a time).
- Byte gaps vary at random.

Checks: every request gets exactly its response (count of earlier executions, then the request);
no request is executed twice (final `executed` equals N, response counts are 0, 1, 2, ...);
the host sees no frame that is valid under S = 0; every request is done within the time limit.
Run a single seed with the environment variable `LEAN_SEED=<n>`. The environment variable
`LEAN_SEEDS` sets the number of seeds (default 8).
"""

import os
import random

import cocotb

from lean_tb import (
    SEQ_MOD,
    W,
    Bench,
    Frame,
    Host,
    TestFailure,
    mod,
    rand_sid,
    resp_payload,
)

NSEEDS = int(os.environ.get("LEAN_SEEDS", "8"))
ONLY = os.environ.get("LEAN_SEED")
LIMIT = 1_500_000  # clocks per seed


def mutate_bytes(rng, data: bytes, p: float) -> bytes:
    if p <= 0:
        return data
    out = bytearray()
    for byte in data:
        r = rng.random()
        if r < p / 3:
            continue  # lost
        if r < 2 * p / 3:
            out.append(rng.randrange(256))  # inserted
            out.append(byte)
        elif r < p:
            out.append(rng.randrange(256))  # substituted
        else:
            out.append(byte)
    return bytes(out)


class Stress:
    def __init__(self, b: Bench, rng: random.Random, prof: dict):
        self.b, self.rng, self.p = b, rng, prof
        self.sid = rand_sid(rng)
        self.h = Host(b, self.sid, B=prof["B"])
        self.reqs = [
            bytes(rng.randrange(256) for _ in range(rng.randrange(0, prof["maxp"] + 1)))
            for _ in range(prof["n"])
        ]
        self.n = prof["n"]
        self.opened = False
        self.last_open = -(10**9)
        self.next_to_send = 0
        self.sent_hi = 0
        self.rsp_next = 0  # absolute index of the next response needed
        self.exp_abs = 0  # absolute exp the FPGA reported
        self.last_valid = 0  # clock of the last valid FPGA frame
        self.last_rsp_event = 0  # clock of the last accepted response or exp advance
        self.last_byte = 0  # clock of the last FPGA byte of any kind
        self.last_progress = 0
        self.gap_seen = None
        self.poll_pending = False
        self.rewind_pending = False
        self.resync_pending = False
        self.need_update = False
        self.read_idx = 0
        self.cur = bytearray()
        self.done_time = None
        self.log = {
            "open_tx": 0,
            "req_tx": 0,
            "poll": 0,
            "rewind": 0,
            "resync": 0,
            "rto": 0,
            "fpga_frames": 0,
            "bad_frames": 0,
            "dup_resp": 0,
            "gap_resp": 0,
            "status": 0,
            "blackout_bytes": 0,
            "host_faults": 0,
        }
        self.errors = []

    # ---- sending with faults ----
    def emit(self, wire: bytes):
        b, rng, p = self.b, self.rng, self.p
        w = wire
        r = rng.random()
        if r < p["p_frame"] / 4:
            self.log["host_faults"] += 1
            return  # dropped
        if r < p["p_frame"] / 2:
            self.log["host_faults"] += 1
            w = w[: rng.randrange(1, len(w))] + b"\x00"  # truncated
        elif r < 3 * p["p_frame"] / 4:
            self.log["host_faults"] += 1
            w = w[:-1]  # lost delimiter
        elif r < p["p_frame"]:
            self.log["host_faults"] += 1
            w = w + w  # duplicated
        m = mutate_bytes(rng, w, p["p_byte"])
        if m != w:
            self.log["host_faults"] += 1
        b.send(m)

    # ---- the per-clock host ----
    def hook(self, b: Bench, clk: int):
        self.read_fpga(b, clk)
        if self.done_time is not None:
            return
        h = self.h
        # timers
        outstanding = self.rsp_next < self.n
        if self.opened and outstanding:
            if clk - self.last_progress > self.p["rto"]:
                self.log["rto"] += 1
                self.poll_pending = True
                self.next_to_send = self.exp_abs
                self.last_progress = clk
            gap_wait = self.gap_seen is not None and clk - self.gap_seen > self.p["gap_wait"]
            # status frames do not count as progress: only a response or a new exp does
            quiet_rsp = self.exp_abs > self.rsp_next and clk - self.last_rsp_event > self.p["rwto"]
            if gap_wait or quiet_rsp:
                h.rewind = (h.rewind + 1) % 4
                self.rewind_pending = True
                self.gap_seen = None
                self.last_rsp_event = clk
                self.log["rewind"] += 1
            if clk - self.last_byte > self.p["quiet"]:
                self.resync_pending = True
                self.last_byte = clk
        if b.rx_q or b.rx_wait:
            return
        if not self.opened:
            if clk - self.last_open > self.p["open_to"]:
                h.consumed = 0
                self.last_open = clk
                self.log["open_tx"] += 1
                self.emit(h.open_wire(grant=h.grant_for(0)))
            return
        g = h.grant_for()
        ack = mod(self.rsp_next)
        if self.poll_pending or self.rewind_pending or self.resync_pending:
            self.log["poll"] += self.poll_pending
            self.log["resync"] += self.resync_pending
            self.emit(h.hdr(ack=ack, grant=g, poll=self.poll_pending, resync=self.resync_pending))
            self.poll_pending = self.rewind_pending = self.resync_pending = False
            return
        s = self.next_to_send
        if s < self.n and s < self.rsp_next + W:
            self.log["req_tx"] += 1
            self.emit(h.wire(data=True, seq=mod(s), ack=ack, grant=g, payload=self.reqs[s]))
            self.next_to_send += 1
            self.sent_hi = max(self.sent_hi, self.next_to_send)
            self.need_update = False
            return
        if self.need_update:
            self.emit(h.hdr(ack=ack, grant=g))
            self.need_update = False

    # ---- the FPGA to host direction, with faults ----
    def read_fpga(self, b: Bench, clk: int):
        p, rng = self.p, self.rng
        while self.read_idx < len(b.tx_bytes):
            c, byte = b.tx_bytes[self.read_idx]
            self.read_idx += 1
            bo = p["blackout"]
            if bo and bo[0] <= c < bo[0] + bo[1]:
                self.log["blackout_bytes"] += 1
                continue
            seq = [byte]
            r = rng.random()
            if r < p["p_byte"] / 3:
                seq = []
            elif r < 2 * p["p_byte"] / 3:
                seq = [rng.randrange(256), byte]
            elif r < p["p_byte"]:
                seq = [rng.randrange(256)]
            for x in seq:
                self.last_byte = clk
                if x == 0:
                    if self.cur:
                        self.on_frame(bytes(self.cur), clk)
                        self.cur = bytearray()
                else:
                    self.cur.append(x)

    def on_frame(self, raw: bytes, clk: int):
        f = Frame(raw, 0, clk)
        h = self.h
        f.parse(self.sid)
        self.log["fpga_frames"] += 1
        if f.p is None:
            if self.opened and f.p0 is not None:
                self.errors.append(f"frame valid under S = 0 at clock {clk}: {f.p0}")
            self.log["bad_frames"] += 1
            return
        p = f.p
        if not self.opened:
            if p["kind"] == "status":  # the OPEN reply
                self.opened = True
                h.consumed = p["cursor"]
                self.last_valid = self.last_progress = self.last_rsp_event = clk
                self.need_update = True
            return
        self.last_valid = clk
        h.consumed = p["cursor"]
        # exp, unwrapped: it never goes back within a session
        e = self.exp_abs + ((p["exp"] - self.exp_abs) % SEQ_MOD)
        if e != self.exp_abs:
            self.last_progress = self.last_rsp_event = clk
            self.exp_abs = e
        if p["kind"] == "status":
            self.log["status"] += 1
        else:
            i = self.rsp_next
            if p["seq"] == mod(i) and i < self.n:
                want = resp_payload(i, self.reqs[i])
                if p["payload"] != want or p["trunc"]:
                    self.errors.append(
                        f"response {i}: payload {p['payload'][:12].hex()} want "
                        f"{want[:12].hex()} trunc={p['trunc']}"
                    )
                self.rsp_next += 1
                self.last_progress = self.last_rsp_event = clk
                self.gap_seen = None
                # an accepted response means the requests before it were accepted
                if self.exp_abs < self.rsp_next:
                    self.exp_abs = self.rsp_next
            elif (p["seq"] - mod(i)) % SEQ_MOD < W:
                self.log["gap_resp"] += 1  # a later response: an earlier one is missing
                if self.gap_seen is None:
                    self.gap_seen = clk
            else:
                self.log["dup_resp"] += 1
        self.need_update = True


def profile(rng: random.Random) -> dict:
    gap_lo = rng.choice([9, 12, 12, 16])
    return dict(
        n=rng.randrange(20, 60),
        maxp=rng.choice([0, 8, 40, 100]) if W > 1 else rng.choice([0, 8, 40, 55]),
        p_byte=rng.choice([0, 0, 0.001, 0.004, 0.01]),
        p_frame=rng.choice([0, 0, 0.05, 0.12, 0.25]),
        B=rng.choice([4096, 600, 400]) if W > 1 else rng.choice([4096, 300, 200]),
        gap=(gap_lo, rng.choice([gap_lo, 24, 40])),
        rto=rng.choice([4000, 6000]),
        rwto=rng.choice([3000, 5000]),
        gap_wait=rng.choice([1500, 2500]),
        quiet=rng.choice([5000, 8000]),
        open_to=3000,
        blackout=None,
    )


async def run_seed(dut, seed: int):
    rng = random.Random(seed)
    prof = profile(rng)
    # a blackout in the FPGA to host direction in some runs
    if rng.random() < 0.3:
        prof["blackout"] = (rng.randrange(5000, 30000), rng.choice([3000, 6000, 12000]))
    gl, gh = prof["gap"]
    b = await Bench.create(
        dut,
        rx_gap=lambda: rng.randrange(gl, gh + 1),
        tx_gap=lambda: rng.randrange(max(1, gl - 3), gh + 1),
    )
    st = Stress(b, rng, prof)
    b.sid = st.sid
    b.hooks.append(st.hook)
    start = b.clk
    while st.rsp_next < st.n or (st.done_time is None):
        await b.tick()
        if st.rsp_next >= st.n and st.done_time is None:
            st.done_time = b.clk
        if st.done_time is not None and b.clk > st.done_time + 3000:
            break
        if b.clk - start > LIMIT:
            raise TestFailure(
                f"seed {seed}: not finished: {st.rsp_next}/{st.n} responses, "
                f"profile {prof}, log {st.log}, {b.dump()}"
            )
        if b.clk % 4096 == 0:
            ex = b.sig("m_count")
            if ex > st.sent_hi:
                raise TestFailure(
                    f"seed {seed}: executed {ex} but only {st.sent_hi} requests were sent"
                )
    b.hooks.remove(st.hook)
    b.close()
    ex = b.sig("m_count")
    errs = list(st.errors)
    if ex != st.n:
        errs.append(f"executed {ex} but {st.n} requests were submitted")
    if errs:
        raise TestFailure(f"seed {seed}: {errs[:5]}\nprofile {prof}\nlog {st.log}")
    return b.clk - start, prof, st.log


@cocotb.test()
async def random_faults_both_directions(dut):
    seeds = [int(ONLY)] if ONLY else list(range(1, NSEEDS + 1))
    tot = dict(
        clocks=0,
        host_faults=0,
        rto=0,
        rewind=0,
        resync=0,
        poll=0,
        bad_frames=0,
        open_tx=0,
        blackouts=0,
    )
    for seed in seeds:
        clocks, prof, log = await run_seed(dut, seed)
        tot["clocks"] += clocks
        for k in ("host_faults", "rto", "rewind", "resync", "poll", "bad_frames", "open_tx"):
            tot[k] += log[k]
        tot["blackouts"] += 1 if prof["blackout"] else 0
    dut._log.info("METRIC stress: %d seeds passed, totals %s", len(seeds), tot)
