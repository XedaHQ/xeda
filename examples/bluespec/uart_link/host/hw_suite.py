#!/usr/bin/env python3
"""Test the LeanLink transport on a real board.

The script uses the host library of this example (`LinkDevice`, `HostProtocol` and the
backends in `leanlink`) and no second protocol implementation. It needs a board that runs the
design of the example (`mkLeanLinkBoard`, built with one of the board targets of `uart_link.yaml`)
and a UART connection to it. Run it from the root directory of the repository:

    python3 examples/bluespec/uart_link/host/hw_suite.py BOARD COMMAND [options]

BOARD is arty, basys3, ulx3s or stlv7325. COMMAND is one of:

    program     load the bitstream into the FPGA (SRAM only)
    soak        correctness soak: random sizes, every count and echo byte checked
    goodput     goodput against budget B and payload size, per backend
    latency     round trip of a 16-byte request (median, p99)
    variants    goodput and latency for the four FTDI chip settings (default, latency timer
                1 ms, event character 0x00, both)
    fault       fault injection in the host backend (drop, corrupt, insert; 1e-4 and 1e-3)
    blackout    200 ms blackouts (both directions, one direction)
    reconfig    program the FPGA again during a session
    stall       host stalls: does the bridge lose bytes at budget B
    trunc       requests whose response does not fit rspLimit (TRUNC)
    powerup     what the FPGA sends right after configuration, and does the host accept it

A backend is `serial` (a serial port with the chip settings as they are; the host timers assume a
16 ms chip), `serial:fast` (the same, host timers for a chip that answers within 1 ms, such as the
CH9102), `serial:default` or `serial:tuned` (reset the FTDI chip settings with pyftdi first: 16 ms
and no event character, or 1 ms and event character 0x00), or `ftdi:LATENCY_MS:EVENT` (pyftdi;
EVENT is `-` or a byte value, for example `ftdi:1:0`). The `serial` backends need `--port`. The
`ftdi` backends and `serial:default` and `serial:tuned` need `--ftdi`. Install `pyserial` and
`pyftdi` as README.md of the example says. The library `pyftdi` loads only when a backend needs it.

Every result goes to stdout and to `<log directory>/<board>_<command>.json`.

Nothing in this script writes the flash of a board. `program` and `reconfig` load the SRAM with
`openFPGALoader`.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from leanlink import (  # noqa: E402
    HostConfig,
    HostProtocol,
    LinkDevice,
    OpenFailed,
    SessionClosed,
    State,
    Status,
)
from leanlink import device as device_module  # noqa: E402
from leanlink.backends import FtdiBackend, SerialBackend  # noqa: E402

R = 64  # protocol constant (doc/protocol.md)


# ------------------------------------------------------------------------------------------
# Boards


@dataclass
class Board:
    name: str
    baud: int
    loader: list[str]  # openFPGALoader arguments that select the board
    bit_name: str  # the bitstream, relative to the run directory of the target
    budgets: tuple[int, ...]  # goodput sweep
    soak_budget: int
    soak_backend: str
    window: int = 8
    chunk: int = 512  # pyftdi USB read size
    serial: str | None = None  # the serial port (--port)
    ftdi: str | None = None  # pyftdi URL of the UART interface (--ftdi)
    bit: Path = Path("missing.bit")  # the bitstream that `program` and `reconfig` load
    log_dir: Path = Path("hw_logs")

    @property
    def line_rate(self) -> float:
        """Bytes per second of one direction (8N1: 10 bits per byte)."""
        return self.baud / 10


# The bitstream names are those of the board targets of uart_link.yaml. `--run-root` names the
# run root of the Xeda build (default `xeda_run`).
BOARDS = {
    "arty": Board(
        "arty",
        12_000_000,
        ["--board", "arty_a7_100t"],
        "uart_link/arty/vivado_synth/uart_link.bit",
        (256, 512, 1024, 2048, 4096),
        2048,
        "ftdi:1:0",
    ),
    "basys3": Board(
        "basys3",
        12_000_000,
        ["--board", "basys3"],
        "uart_link/basys3/vivado_synth/uart_link.bit",
        (256, 512, 1024, 2048, 4096),
        2048,
        "ftdi:1:0",
    ),
    "ulx3s": Board(
        "ulx3s",
        3_000_000,
        ["--board", "ulx3s"],
        "uart_link/ulx3s/fpga_pack/outputs/uart_link.bit",
        (256, 512, 1024, 2048),
        512,  # the FIFO of the FT231X holds 512 bytes
        "ftdi:1:0",
        chunk=4096,
    ),
    "stlv7325": Board(
        "stlv7325",
        3_000_000,
        ["--board", "stlv7325"],
        "uart_link/stlv7325/fpga_pack/outputs/uart_link.bit",
        (256, 512, 1024, 2048),
        1024,  # the CH9102 datasheet gives no buffer size: keep margin on other host drivers
        "serial",
    ),
}


def set_qos() -> None:
    """On macOS, ask for the user-interactive scheduling class (a busy machine slows a test).
    On another system, do nothing."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        libc.pthread_set_qos_class_self_np(0x21, 0)  # QOS_CLASS_USER_INTERACTIVE
    except Exception as exc:  # noqa: BLE001
        print("qos not set:", exc, file=sys.stderr)


# ------------------------------------------------------------------------------------------
# Backends


@dataclass(frozen=True)
class BackendSpec:
    kind: str  # "serial" or "ftdi"
    latency_ms: int | None  # the chip latency timer in effect, None if unknown
    event: int | None
    prep: str | None = None  # serial only: "default" or "tuned"

    @property
    def name(self) -> str:
        if self.kind == "serial":
            return "serial" + (f":{self.prep}" if self.prep else "")
        ev = "-" if self.event is None else f"{self.event:#04x}"
        return f"ftdi:{self.latency_ms}ms:{ev}"

    @property
    def slow(self) -> bool:
        """True if the chip may hold data for about 16 ms (the host timers must allow it)."""
        if self.kind == "serial" and self.prep is None:
            return True
        return (self.latency_ms or 16) >= 8 and self.event is None


def parse_backend(text: str) -> BackendSpec:
    parts = text.split(":")
    if parts[0] == "serial":
        prep = parts[1] if len(parts) > 1 else None
        latency = {"default": 16, "tuned": 1, "fast": 1}.get(prep or "", None)
        event = {"default": None, "tuned": 0}.get(prep or "", None)
        return BackendSpec("serial", latency, event, prep)
    if parts[0] == "ftdi":
        lat = int(parts[1]) if len(parts) > 1 else 1
        ev_text = parts[2] if len(parts) > 2 else "-"
        ev = None if ev_text == "-" else int(ev_text, 0)
        return BackendSpec("ftdi", lat, ev)
    raise SystemExit(f"unknown backend {text!r}")


def ftdi_configure(board: Board, latency_ms: int, event: int | None) -> None:
    """Write latency timer and event character into the chip, then release it."""
    assert board.ftdi is not None
    be = FtdiBackend(board.ftdi, board.baud, latency_ms=latency_ms, event_char=event)
    be.close()


def make_transport(board: Board, spec: BackendSpec, purge_on_open: bool = True):
    """Open the transport. pyftdi lists every USB device first, and that fails for a moment
    when another process opens or closes a device at the same time, so retry."""
    last: Exception | None = None
    for attempt in range(40):
        try:
            return _make_transport(board, spec, purge_on_open)
        except Exception as exc:  # noqa: BLE001
            if (
                "No such device" not in str(exc)
                and "Access" not in str(exc)
                and "busy" not in str(exc).lower()
            ):
                raise
            last = exc
            time.sleep(0.5)
    raise last  # type: ignore[misc]


def _make_transport(board: Board, spec: BackendSpec, purge_on_open: bool = True):
    if spec.kind == "ftdi":
        if board.ftdi is None:
            raise SystemExit(f"backend {spec.name} needs --ftdi")
        return FtdiBackend(
            board.ftdi,
            board.baud,
            latency_ms=spec.latency_ms or 1,
            event_char=spec.event,
            chunksize=board.chunk,
            purge_on_open=purge_on_open,
        )
    if spec.prep in ("default", "tuned"):
        if board.ftdi is None:
            raise SystemExit(f"backend {spec.name} needs --ftdi")
        ftdi_configure(board, spec.latency_ms or 16, spec.event)
    if board.serial is None:
        raise SystemExit(f"backend {spec.name} needs --port")
    return SerialBackend(board.serial, board.baud)


class Tap:
    """Counts the bytes that pass a transport."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.tx = 0
        self.rx = 0
        self.trace: list | None = None  # set to a list to record (time, direction, bytes)

    def write(self, data: bytes) -> None:
        self.tx += len(data)
        if self.trace is not None:
            self.trace.append((time.perf_counter(), "tx", bytes(data)))
        self.inner.write(data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        data = self.inner.read(max_bytes, timeout)
        self.rx += len(data)
        if self.trace is not None and data:
            self.trace.append((time.perf_counter(), "rx", bytes(data)))
        return data

    def purge(self) -> None:
        self.inner.purge()

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class Detachable:
    """A transport that can be closed and replaced while the host session goes on.

    While detached, written bytes are dropped and reads return nothing (like a cable pulled).
    The ULX3S needs it: one FT231X interface carries both JTAG and UART, so the port must be
    closed during programming.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.dropped = 0

    def detach(self) -> None:
        inner, self.inner = self.inner, None
        if inner is not None:
            inner.close()

    def attach(self, inner) -> None:
        self.inner = inner

    def write(self, data: bytes) -> None:
        if self.inner is None:
            self.dropped += len(data)
        else:
            self.inner.write(data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if self.inner is None:
            time.sleep(min(timeout, 0.001))
            return b""
        return self.inner.read(max_bytes, timeout)

    def purge(self) -> None:
        if self.inner is not None:
            self.inner.purge()

    def close(self) -> None:
        self.detach()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class Injector:
    """Per-byte faults with a geometric gap, so a run costs one random number per fault."""

    def __init__(self, rng: random.Random, kind: str, rate: float) -> None:
        self.rng = rng
        self.kind = kind
        self.rate = rate
        self.count = 0
        self.seen = 0
        self.gap = self._next_gap()

    def _next_gap(self) -> float:
        if self.rate <= 0:
            return math.inf
        u = self.rng.random()
        return int(math.log(1.0 - u) / math.log(1.0 - self.rate))

    def apply(self, data: bytes) -> bytes:
        self.seen += len(data)
        if self.rate <= 0 or not data:
            return data
        out = bytearray()
        pos = 0
        n = len(data)
        while True:
            if self.gap >= n - pos:
                self.gap -= n - pos
                out += data[pos:]
                break
            k = pos + int(self.gap)
            out += data[pos:k]
            b = data[k]
            self.count += 1
            if self.kind == "drop":
                pass
            elif self.kind == "corrupt":
                out.append(b ^ self.rng.randrange(1, 256))
            elif self.kind == "insert":
                out.append(self.rng.randrange(256))
                out.append(b)
            else:
                raise ValueError(self.kind)
            pos = k + 1
            self.gap = self._next_gap()
        return bytes(out)


class FaultBackend:
    """Fault injection around a transport: drop, corrupt or insert bytes, and blackouts.

    ``tx`` faults hit the bytes that the host writes (host to FPGA), ``rx`` faults hit the bytes
    that the host reads (FPGA to host). A blackout drops every byte of the chosen directions
    for a time.
    """

    def __init__(
        self,
        inner,
        rng: random.Random,
        kind: str = "drop",
        tx_rate: float = 0.0,
        rx_rate: float = 0.0,
    ) -> None:
        self.inner = inner
        self.tx = Injector(rng, kind, tx_rate)
        self.rx = Injector(rng, kind, rx_rate)
        self.pending = bytearray()
        self.blackout_until = 0.0
        self.blackout_dirs: tuple[str, ...] = ()
        self.blackout_dropped = {"tx": 0, "rx": 0}
        self.blackouts = 0

    def blackout(self, seconds: float, dirs: tuple[str, ...] = ("tx", "rx")) -> None:
        self.blackout_until = time.monotonic() + seconds
        self.blackout_dirs = dirs
        self.blackouts += 1

    def _black(self, direction: str) -> bool:
        return direction in self.blackout_dirs and time.monotonic() < self.blackout_until

    def write(self, data: bytes) -> None:
        if self._black("tx"):
            self.blackout_dropped["tx"] += len(data)
            return
        out = self.tx.apply(data)
        if out:
            self.inner.write(out)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if not self.pending:
            raw = self.inner.read(max_bytes, timeout)
            if self._black("rx"):
                self.blackout_dropped["rx"] += len(raw)
                return b""
            self.pending += self.rx.apply(raw)
        out = bytes(self.pending[:max_bytes])
        del self.pending[:max_bytes]
        return out

    def purge(self) -> None:
        self.pending.clear()
        self.inner.purge()

    def close(self) -> None:
        self.inner.close()

    def counts(self) -> dict:
        return {
            "kind": self.tx.kind,
            "tx_faults": self.tx.count,
            "tx_bytes": self.tx.seen,
            "rx_faults": self.rx.count,
            "rx_bytes": self.rx.seen,
            "blackouts": self.blackouts,
            "blackout_dropped": dict(self.blackout_dropped),
        }


# ------------------------------------------------------------------------------------------
# Host protocol with byte accounting

PROTOCOLS: list["TracedProtocol"] = []


class TracedProtocol(HostProtocol):
    """HostProtocol plus a check of the FPGA's byte cursor.

    Every valid FPGA frame carries the cursor after that frame. Two valid frames in a row must
    differ by the wire length (frame plus delimiter) of the second one. A larger difference is
    the number of FPGA-to-host bytes that never arrived as a valid frame (bytes lost in the
    bridge or in the line, or frames that failed a check).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_gaps: list[int] = []
        self.lost_bytes = 0
        self._prev_cursor: int | None = None
        self._wire_len = 0
        PROTOCOLS.append(self)

    def _handle(self, wire: bytes, now: float) -> None:
        self._wire_len = len(wire) + 1
        super()._handle(wire, now)

    def _start_session(self, frame: dict, now: float) -> None:
        super()._start_session(frame, now)
        if self.state is State.ACTIVE:
            self._prev_cursor = frame["cursor"]

    def _on_frame(self, frame: dict, now: float) -> None:
        if self._prev_cursor is not None:
            delta = (frame["cursor"] - self._prev_cursor) & 0xFFFF
            if delta != self._wire_len:
                extra = (delta - self._wire_len) & 0xFFFF
                self.cursor_gaps.append(extra)
                self.lost_bytes += extra
        self._prev_cursor = frame["cursor"]
        super()._on_frame(frame, now)


device_module.HostProtocol = TracedProtocol  # LinkDevice.open() builds this class


# ------------------------------------------------------------------------------------------
# Configuration of a session


def host_config(board: Board, budget: int, spec: BackendSpec, **over) -> HostConfig:
    rsp_limit = min(255, budget - R - 12)
    max_payload = min(243, rsp_limit - 16)  # the response holds a 4-byte count, then the echo
    timing = dict(
        open_timeout=0.05, rto=0.02, rewind_timeout=0.05, poll_interval=0.05, resync_quiet=0.2
    )
    if spec.slow:
        timing = dict(
            open_timeout=0.2, rto=0.1, rewind_timeout=0.25, poll_interval=0.25, resync_quiet=0.6
        )
    kw = dict(
        bridge_budget=budget,
        rsp_limit=rsp_limit,
        max_payload=max_payload,
        byte_time=10.0 / board.baud,
        **timing,
    )
    kw.update(over)
    return HostConfig(**kw)


@dataclass
class Rig:
    board: Board
    spec: BackendSpec
    budget: int
    transport: object  # innermost transport
    tap: Tap
    detach: Detachable
    fault: FaultBackend | None
    dev: LinkDevice
    cfg: HostConfig

    def close(self) -> None:
        try:
            self.dev.abort()
        finally:
            self.dev.transport.close()

    @property
    def max_payload(self) -> int:
        return self.cfg.max_payload


def make_rig(
    board: Board,
    spec: BackendSpec,
    budget: int,
    *,
    fault: dict | None = None,
    seed: int = 1,
    cfg_over: dict | None = None,
) -> Rig:
    inner = make_transport(board, spec)
    tap = Tap(inner)
    transport: object = tap
    detach = Detachable(tap)
    transport = detach
    fb = None
    if fault:
        fb = FaultBackend(detach, random.Random(seed * 7919 + 1), **fault)
        transport = fb
    cfg = host_config(board, budget, spec, **(cfg_over or {}))
    dev = LinkDevice(transport, cfg, rng=random.Random(seed))
    return Rig(board, spec, budget, inner, tap, detach, fb, dev, cfg)


# ------------------------------------------------------------------------------------------
# Ledger: every ticket, with the invariants


def u32(b: bytes) -> int:
    return int.from_bytes(b[:4], "little")


@dataclass
class Row:
    ticket: int
    payload: bytes
    status: Status
    data: bytes
    was_sent: bool
    reason: str
    epoch: int


class Ledger:
    """Holds every ticket in submission order and checks these invariants:

    * OK: the response is the 32-bit execution count and then the request payload.
    * Within one FPGA configuration (epoch) the counts of the OK and TRUNCATED responses grow.
    * Between two such responses the count may jump only by the number of tickets in between that
      were sent and came back as UnknownOutcome (they may have executed). Any other jump is a
      double execution or an execution that nobody asked for.
    * An UnknownOutcome ticket exists only after a terminal event of the session (the reason
      names it).
    """

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.epoch = 0
        self.violations: list[str] = []
        self.terminal_events: collections.Counter = collections.Counter()

    def add(self, ticket: int, payload: bytes, result, session_lost: bool) -> None:
        row = Row(
            ticket, payload, result.status, result.data, result.was_sent, result.reason, self.epoch
        )
        self.rows.append(row)
        if result.status is Status.UNKNOWN_OUTCOME:
            if not session_lost or not result.reason:
                self.violations.append(f"ticket {ticket}: UnknownOutcome without a terminal event")
            self.terminal_events[result.reason] += 1

    def check(self, max_resp_payload: int | None = None) -> dict:
        v = list(self.violations)
        prev = None
        prev_epoch = None
        unknown_between = 0
        ok = trunc = unknown_sent = unknown_never = executed_unknown = 0
        first_count = last_count = None
        for row in self.rows:
            if row.status is Status.UNKNOWN_OUTCOME:
                if row.was_sent:
                    unknown_sent += 1
                    unknown_between += 1
                else:
                    unknown_never += 1
                continue
            if len(row.data) < 4:
                v.append(f"ticket {row.ticket}: response shorter than the count")
                continue
            count = u32(row.data)
            body = row.data[4:]
            if row.status is Status.OK:
                ok += 1
                if body != row.payload:
                    v.append(
                        f"ticket {row.ticket}: echo differs (len {len(body)} vs "
                        f"{len(row.payload)})"
                    )
            else:
                trunc += 1
                if row.payload[: len(body)] != body:
                    v.append(f"ticket {row.ticket}: truncated echo is not a prefix")
            if prev is not None and prev_epoch == row.epoch:
                if count <= prev:
                    v.append(f"ticket {row.ticket}: count {count} after {prev}")
                elif count - prev - 1 > unknown_between:
                    v.append(
                        f"ticket {row.ticket}: count {count} after {prev} with only "
                        f"{unknown_between} unknown tickets in between (double execution)"
                    )
                else:
                    executed_unknown += count - prev - 1
            if prev_epoch != row.epoch:
                first_count = count
            prev, prev_epoch, unknown_between = count, row.epoch, 0
            last_count = count
        return {
            "tickets": len(self.rows),
            "ok": ok,
            "truncated": trunc,
            "unknown_sent": unknown_sent,
            "unknown_never_sent": unknown_never,
            "executed_among_unknown": executed_unknown,
            "first_count": first_count,
            "last_count": last_count,
            "violations": len(v),
            "violation_samples": v[:5],
            "terminal_events": dict(self.terminal_events),
        }


# ------------------------------------------------------------------------------------------
# Driver


def sum_stats(protocols: list[TracedProtocol]) -> dict:
    total: collections.Counter = collections.Counter()
    for p in protocols:
        total.update(p.stats)
    return dict(total)


def lost_bytes(protocols: list[TracedProtocol]) -> int:
    return sum(p.lost_bytes for p in protocols)


def drain(dev: LinkDevice, pending: collections.deque, led: Ledger) -> int:
    done = 0
    while pending:
        ticket, payload = pending[0]
        result = dev.take_result(ticket)
        if result is None:
            break
        lost = dev.protocol is not None and dev.protocol.state is State.LOST
        led.add(ticket, payload, result, lost)
        pending.popleft()
        done += 1
    return done


def open_session(rig: Rig, tries: int = 20, timeout: float = 5.0) -> int:
    """Open a session. Returns the number of failed attempts."""
    failed = 0
    for _ in range(tries):
        try:
            rig.dev.open(timeout=timeout)
            return failed
        except OpenFailed:
            failed += 1
    raise SystemExit("OPEN failed too often")


def drive(
    rig: Rig,
    led: Ledger,
    n: int,
    gen: Callable[[int], bytes],
    *,
    cap: int | None = None,
    reopen: bool = True,
    hook: Callable[[int, int], bool] | None = None,
    limit_s: float = 3600.0,
    progress: str | None = None,
) -> dict:
    """Submit ``n`` requests (``gen(i)`` makes request i) with at most ``cap`` outstanding.

    On a lost session it drains the tickets and, if ``reopen``, opens a new session. ``hook(i,
    done)`` runs once per loop and may return True to stop. Returns timing and counters.
    """
    dev = rig.dev
    cap = cap or 2 * rig.board.window
    pending: collections.deque = collections.deque()
    submitted = done = sessions = open_failures = 0
    t0 = time.perf_counter()
    t_last = t0
    stopped = False
    while True:
        proto = dev.protocol
        active = proto is not None and proto.state is State.ACTIVE
        if not active:
            drain(dev, pending, led)  # a lost session gives every ticket its result
            if submitted >= n or not reopen:
                break
            open_failures += open_session(rig)
            sessions += 1
            continue
        if submitted >= n and not pending:
            break
        while submitted < n and len(pending) < cap:
            payload = gen(submitted)
            try:
                ticket = dev.submit(payload)
            except SessionClosed:
                break
            pending.append((ticket, payload))
            submitted += 1
        dev.pump(dev._slice(dev.protocol, None))
        done += drain(dev, pending, led)
        if hook is not None and hook(submitted, done):
            stopped = True
            break
        now = time.perf_counter()
        if progress and now - t_last > 15:
            print(
                f"   {progress}: {submitted}/{n} submitted, {done} done, " f"{now - t0:.0f} s",
                flush=True,
            )
            t_last = now
        if now - t0 > limit_s:
            raise SystemExit(f"time limit of {limit_s} s reached ({submitted}/{n})")
    return {
        "elapsed_s": time.perf_counter() - t0,
        "submitted": submitted,
        "done": done,
        "sessions_opened_in_drive": sessions,
        "open_failures": open_failures,
        "pending_left": len(pending),
        "stopped_by_hook": stopped,
        "_pending": pending,
    }


def size_gen(rng: random.Random, max_payload: int) -> Callable[[int], bytes]:
    """Random sizes 0..max; sizes 0 and max appear every 100 requests (and request 0, 50)."""

    def gen(i: int) -> bytes:
        k = i % 100
        n = 0 if k == 0 else max_payload if k == 50 else rng.randint(0, max_payload)
        return rng.randbytes(n)

    return gen


def fixed_gen(rng: random.Random, size: int) -> Callable[[int], bytes]:
    return lambda i: rng.randbytes(size)


def host_summary(protocols: list[TracedProtocol]) -> dict:
    keys = (
        "sessions_started",
        "requests_sent",
        "retransmissions",
        "go_back_n",
        "rewinds",
        "idle_polls",
        "polls",
        "resync_timers",
        "resyncs",
        "rx_frames",
        "rx_valid",
        "rx_invalid",
        "rx_overflow",
        "unbound_hint",
        "response_ignored",
        "exp_stale",
        "responses",
        "lost",
    )
    st = sum_stats(protocols)
    out = {k: st.get(k, 0) for k in keys}
    out["fpga_to_host_bytes_lost"] = lost_bytes(protocols)
    return out


def banner(rig: Rig) -> str:
    return (
        f"{rig.board.name} {rig.spec.name} B={rig.budget} rspLimit={rig.cfg.rsp_limit} "
        f"maxPayload={rig.max_payload} W=8"
    )


def save(board: Board, command: str, data: object) -> None:
    board.log_dir.mkdir(parents=True, exist_ok=True)
    path = board.log_dir / f"{board.name}_{command}.json"
    path.write_text(json.dumps(data, indent=1, default=str))
    print(f"-> {path}")


# ------------------------------------------------------------------------------------------
# Commands


def program(board: Board) -> float:
    """Load the bitstream into SRAM (never flash). Returns the time that it took."""
    bit = board.bit
    if not bit.exists():
        raise SystemExit(f"{bit} does not exist")
    cmd = ["openFPGALoader", *board.loader, "--bitstream", str(bit)]
    if any(a in cmd for a in ("-f", "--write-flash", "-b")):
        raise SystemExit("refusing to write flash")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        raise SystemExit(f"openFPGALoader failed:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}")
    print(f"programmed {board.name} from {bit.name} in {dt:.1f} s")
    time.sleep(0.3)
    return dt


def warmup(rig: Rig, rng: random.Random, count: int = 20) -> None:
    for _ in range(count):
        rig.dev.transact(rng.randbytes(16), timeout=5.0)


def cmd_soak(
    board: Board, spec: BackendSpec, budget: int, n: int, seed: int, window: int | None = None
) -> dict:
    rig = make_rig(board, spec, budget, seed=seed)
    led = Ledger()
    rng = random.Random(seed)
    mark = len(PROTOCOLS)
    try:
        print(banner(rig))
        failed = open_session(rig)
        # Request 0 is the first ticket: the count of the board is whatever earlier runs left.
        out = drive(rig, led, n, size_gen(rng, rig.max_payload), cap=window, progress="soak")
        out.pop("_pending")
        summary = led.check()
        res = {
            "board": board.name,
            "backend": spec.name,
            "budget": budget,
            "rsp_limit": rig.cfg.rsp_limit,
            "max_payload": rig.max_payload,
            **summary,
            **out,
            "open_failures": failed + out["open_failures"],
            "requests_per_s": summary["tickets"] / out["elapsed_s"],
            "tx_bytes": rig.tap.tx,
            "rx_bytes": rig.tap.rx,
            "host": host_summary(PROTOCOLS[mark:]),
            "chip_overruns": getattr(rig.transport, "overruns", None),
        }
        # All 100,000 counts: the count must advance by exactly one per OK response.
        if summary["first_count"] is not None and summary["unknown_sent"] == 0:
            res["count_span_ok"] = (
                summary["last_count"] - summary["first_count"] + 1
                == summary["ok"] + summary["truncated"]
            )
        print(json.dumps({k: v for k, v in res.items()}, default=str))
        return res
    finally:
        rig.close()


def measure_goodput(rig: Rig, size: int, seconds: float, seed: int) -> dict:
    led = Ledger()
    rng = random.Random(seed)
    gen = fixed_gen(rng, size)
    # Calibrate with a short run, then run for about `seconds`.
    drive(rig, Ledger(), 50, gen, limit_s=60)
    t = drive(rig, Ledger(), 100, gen, limit_s=60)
    rate = 100 / max(t["elapsed_s"], 1e-3)
    n = int(min(30000, max(200, rate * seconds)))
    # The session that is active now counts from here on: its counters so far are the baseline.
    first = len(PROTOCOLS) - 1
    base_stats = dict(PROTOCOLS[first].stats)
    base_lost = PROTOCOLS[first].lost_bytes
    tx0, rx0 = rig.tap.tx, rig.tap.rx
    cpu0 = time.process_time()
    out = drive(rig, led, n, gen, limit_s=120)
    cpu = time.process_time() - cpu0
    dt = out["elapsed_s"]
    chk = led.check()
    req_kBps = n * size / dt / 1e3
    rsp_kBps = n * (size + 4) / dt / 1e3
    line = rig.board.line_rate
    return {
        "size": size,
        "requests": n,
        "seconds": dt,
        "requests_per_s": n / dt,
        "req_kBps": req_kBps,
        "rsp_kBps": rsp_kBps,
        "req_pct_line": 100 * req_kBps * 1e3 / line,
        "rsp_pct_line": 100 * rsp_kBps * 1e3 / line,
        "wire_tx_pct_line": 100 * (rig.tap.tx - tx0) / dt / line,
        "wire_rx_pct_line": 100 * (rig.tap.rx - rx0) / dt / line,
        "host_cpu_fraction": cpu / dt,
        "violations": chk["violations"],
        "ok": chk["ok"],
        "unknown": chk["unknown_sent"],
        "lost_bytes": lost_bytes(PROTOCOLS[first:]) - base_lost,
        "retransmissions": sum_stats(PROTOCOLS[first:]).get("retransmissions", 0)
        - base_stats.get("retransmissions", 0),
    }


def cmd_goodput(
    board: Board,
    specs: list[BackendSpec],
    budgets: tuple[int, ...],
    sizes: tuple[object, ...],
    seconds: float,
    seed: int,
) -> list[dict]:
    rows = []
    for spec in specs:
        for budget in budgets:
            rig = make_rig(board, spec, budget, seed=seed)
            try:
                open_session(rig)
                for size in sizes:
                    sz = rig.max_payload if size == "max" else int(size)
                    sz = min(sz, rig.max_payload)
                    row = {
                        "board": board.name,
                        "backend": spec.name,
                        "budget": budget,
                        "size_label": str(size),
                        **measure_goodput(rig, sz, seconds, seed),
                    }
                    rows.append(row)
                    print(
                        f"{board.name:8s} {spec.name:14s} B={budget:5d} L={sz:3d}: "
                        f"req {row['req_kBps']:7.1f} kB/s ({row['req_pct_line']:5.1f}%), "
                        f"rsp {row['rsp_kBps']:7.1f} kB/s ({row['rsp_pct_line']:5.1f}%), "
                        f"{row['requests_per_s']:7.0f} req/s, cpu {row['host_cpu_fraction']:.2f}, "
                        f"lost {row['lost_bytes']}, retx {row['retransmissions']}, "
                        f"viol {row['violations']}",
                        flush=True,
                    )
            finally:
                rig.close()
    return rows


def measure_latency(rig: Rig, n: int, size: int, seed: int) -> dict:
    rng = random.Random(seed)
    dev = rig.dev
    for _ in range(20):
        dev.transact(rng.randbytes(size), timeout=5.0)
    times = []
    for _ in range(n):
        payload = rng.randbytes(size)
        t0 = time.perf_counter()
        data = dev.transact(payload, timeout=5.0)
        times.append((time.perf_counter() - t0) * 1e3)
        if data[4:] != payload:
            raise SystemExit("echo mismatch in the latency test")
    times.sort()
    return {
        "n": n,
        "size": size,
        "median_ms": statistics.median(times),
        "p99_ms": times[int(0.99 * (n - 1))],
        "min_ms": times[0],
        "max_ms": times[-1],
        "mean_ms": statistics.fmean(times),
        "p90_ms": times[int(0.9 * (n - 1))],
    }


def cmd_latency(
    board: Board, specs: list[BackendSpec], budget: int, n: int, seed: int, size: int = 16
) -> list[dict]:
    rows = []
    for spec in specs:
        rig = make_rig(board, spec, budget, seed=seed)
        try:
            open_session(rig)
            row = {
                "board": board.name,
                "backend": spec.name,
                "budget": budget,
                **measure_latency(rig, n, size, seed),
            }
            rows.append(row)
            print(
                f"{board.name:8s} {spec.name:14s} B={budget:5d} {size}-byte request: median "
                f"{row['median_ms']:.3f} ms, p99 {row['p99_ms']:.3f} ms, min {row['min_ms']:.3f}, "
                f"max {row['max_ms']:.3f} (n={n})",
                flush=True,
            )
        finally:
            rig.close()
    return rows


VARIANTS = ("ftdi:16:-", "ftdi:1:-", "ftdi:16:0", "ftdi:1:0")


def cmd_variants(
    board: Board, budget: int, sizes: tuple[object, ...], seconds: float, n_latency: int, seed: int
) -> list[dict]:
    rows = []
    for text in VARIANTS:
        spec = parse_backend(text)
        lat = cmd_latency(board, [spec], budget, n_latency, seed)
        gp = cmd_goodput(board, [spec], (budget,), sizes, seconds, seed)
        rows.append({"variant": spec.name, "latency": lat[0], "goodput": gp})
    return rows


FAULT_KINDS = ("drop", "corrupt", "insert")


def run_fault_case(
    board: Board,
    spec: BackendSpec,
    budget: int,
    kind: str,
    rate: float,
    dirs: str,
    n: int,
    seed: int,
    blackout: dict | None = None,
) -> dict:
    tx_rate = rate if dirs in ("both", "h2f") else 0.0
    rx_rate = rate if dirs in ("both", "f2h") else 0.0
    rig = make_rig(
        board, spec, budget, seed=seed, fault=dict(kind=kind, tx_rate=tx_rate, rx_rate=rx_rate)
    )
    led = Ledger()
    rng = random.Random(seed)
    mark = len(PROTOCOLS)
    fired = {"n": 0}
    sched = sorted(blackout["at"]) if blackout else []

    def hook(submitted: int, done: int) -> bool:
        if sched and submitted >= sched[0]:
            sched.pop(0)
            rig.fault.blackout(blackout["seconds"], blackout["dirs"])
            fired["n"] += 1
        return False

    try:
        failed = open_session(rig)
        out = drive(rig, led, n, size_gen(rng, rig.max_payload), hook=hook, limit_s=1800)
        out.pop("_pending")
        chk = led.check()
        host = host_summary(PROTOCOLS[mark:])
        res = {
            "board": board.name,
            "backend": spec.name,
            "budget": budget,
            "kind": kind,
            "rate": rate,
            "dirs": dirs,
            "blackout": blackout,
            "blackouts_fired": fired["n"],
            **chk,
            **out,
            "open_failures": failed + out["open_failures"],
            "fault": rig.fault.counts(),
            "host": host,
            "sessions": host["sessions_started"],
        }
        return res
    finally:
        rig.close()


def show_fault(res: dict) -> None:
    h = res["host"]
    f = res["fault"]
    print(
        f"{res['board']:8s} {res['kind']:8s} rate={res['rate']:.0e} {res['dirs']:5s} "
        f"B={res['budget']:4d} n={res['tickets']}: ok {res['ok']} trunc {res['truncated']} "
        f"unknown {res['unknown_sent']}+{res['unknown_never_sent']}(never sent) "
        f"sessions {res['sessions']} VIOL {res['violations']} | faults h2f {f['tx_faults']} "
        f"f2h {f['rx_faults']} bo {f['blackouts']} | retx {h['retransmissions']} "
        f"gbn {h['go_back_n']} rew {h['rewinds']} poll {h['polls']} resync {h['resyncs']} "
        f"inval {h['rx_invalid']} lostB {h['fpga_to_host_bytes_lost']} "
        f"events {res['terminal_events']}",
        flush=True,
    )


def cmd_fault(
    board: Board,
    spec: BackendSpec,
    budget: int,
    n: int,
    seed: int,
    rates: tuple[float, ...],
    dirs_list: tuple[str, ...],
    kinds: tuple[str, ...] = FAULT_KINDS,
) -> list[dict]:
    rows = []
    k = 0
    for kind in kinds:
        for rate in rates:
            for dirs in dirs_list:
                k += 1
                res = run_fault_case(board, spec, budget, kind, rate, dirs, n, seed + k)
                show_fault(res)
                rows.append(res)
    return rows


def cmd_blackout(
    board: Board,
    spec: BackendSpec,
    budgets: tuple[int, ...],
    n: int,
    seed: int,
    seconds: float = 0.2,
) -> list[dict]:
    rows = []
    k = 0
    for budget in budgets:
        for dirs in (("tx", "rx"), ("rx",), ("tx",)):
            k += 1
            bo = {"at": [n // 4, n // 2, 3 * n // 4], "seconds": seconds, "dirs": dirs}
            res = run_fault_case(board, spec, budget, "drop", 0.0, "both", n, seed + k, bo)
            res["blackout_dirs"] = "+".join(dirs)
            print(f"   blackout dirs={'+'.join(dirs)}", end=" ")
            show_fault(res)
            rows.append(res)
    return rows


def cmd_trunc(board: Board, spec: BackendSpec, budget: int, seed: int) -> dict:
    """Requests whose response is larger than rspLimit allows: TRUNC, with an exact prefix."""
    rig = make_rig(board, spec, budget, seed=seed, cfg_over={"max_payload": 243})
    rng = random.Random(seed)
    led = Ledger()
    try:
        open_session(rig)
        limit = rig.cfg.rsp_limit - 12  # response payload bytes the session allows
        sizes = [s for s in range(limit - 4 - 2, min(243, limit - 4 + 12) + 1)]
        seq = []
        for s in sizes:
            seq.append(rng.randbytes(s))
        pending: collections.deque = collections.deque()
        for p in seq:
            pending.append((rig.dev.submit(p), p))
        while pending:
            rig.dev.pump(0.001)
            drain(rig.dev, pending, led)
        kinds = collections.Counter()
        bad = 0
        for row in led.rows:
            want_trunc = 4 + len(row.payload) > limit
            kinds[row.status.value] += 1
            if want_trunc != (row.status is Status.TRUNCATED):
                bad += 1
            if row.status is Status.TRUNCATED and len(row.data) != limit:
                bad += 1
        chk = led.check()
        res = {
            "board": board.name,
            "backend": spec.name,
            "budget": budget,
            "limit": limit,
            "sizes": [min(sizes), max(sizes)],
            "statuses": dict(kinds),
            "mismatches": bad,
            **chk,
        }
        print(json.dumps(res, default=str))
        return res
    finally:
        rig.close()


def cmd_stall(
    board: Board,
    spec: BackendSpec,
    budgets: tuple[int, ...],
    seed: int,
    pauses_ms: tuple[int, ...] = (5, 20, 50, 100),
    rounds: int = 40,
) -> list[dict]:
    """Host stalls with a full window of maximum-size responses on the way.

    Each round submits a full window of maximum-size requests, stalls the host (no read, no
    write) for the pause, then reads everything. The bridge holds the bytes that the FPGA
    sends meanwhile. With no byte lost, the FPGA-to-host cursor gaps stay 0 and no frame fails.
    """
    rows = []
    for budget in budgets:
        for pause in pauses_ms:
            rig = make_rig(board, spec, budget, seed=seed)
            led = Ledger()
            rng = random.Random(seed)
            mark = len(PROTOCOLS)
            try:
                open_session(rig)
                dev = rig.dev
                size = rig.max_payload
                pending: collections.deque = collections.deque()
                t0 = time.perf_counter()
                for r in range(rounds):
                    for _ in range(board.window):
                        p = rng.randbytes(size)
                        pending.append((dev.submit(p), p))
                    # Send the window now, then stall without reading.
                    dev.pump(0.0)
                    if hasattr(rig.transport, "stall_reader"):
                        rig.transport.stall_reader(pause / 1e3)  # the USB reads stop too
                    time.sleep(pause / 1e3)
                    guard = time.perf_counter() + 30
                    while pending and time.perf_counter() < guard:
                        dev.pump(0.001)
                        drain(dev, pending, led)
                    if dev.protocol.state is not State.ACTIVE:
                        break
                chk = led.check()
                host = host_summary(PROTOCOLS[mark:])
                row = {
                    "board": board.name,
                    "backend": spec.name,
                    "budget": budget,
                    "pause_ms": pause,
                    "rounds": r + 1,
                    "size": size,
                    **chk,
                    "lost_bytes": host["fpga_to_host_bytes_lost"],
                    "rx_invalid": host["rx_invalid"],
                    "retransmissions": host["retransmissions"],
                    "rewinds": host["rewinds"],
                    "resyncs": host["resyncs"],
                    "session_lost": dev.protocol.state is State.LOST,
                    "chip_overruns": getattr(rig.transport, "overruns", None),
                    "seconds": time.perf_counter() - t0,
                }
                rows.append(row)
                print(
                    f"{board.name:8s} stall B={budget:5d} pause={pause:3d} ms: tickets "
                    f"{chk['tickets']} ok {chk['ok']} unknown {chk['unknown_sent']} lost bytes "
                    f"{row['lost_bytes']} invalid frames {row['rx_invalid']} retx "
                    f"{row['retransmissions']} viol {chk['violations']} "
                    f"overruns {row['chip_overruns']}",
                    flush=True,
                )
            finally:
                rig.close()
    return rows


def cmd_reconfig(
    board: Board,
    spec: BackendSpec,
    budget: int,
    seed: int,
    variants: tuple[str, ...] = ("busy", "idle"),
    reps: int = 1,
    default_limits: bool = False,
) -> dict:
    """Program the FPGA again during a session.

    A: busy. The window is full of requests when the programmer starts. The host must end the
       session (UNBOUND once the new FPGA answers), report UnknownOutcome for every ticket
       without a response, and execute nothing again. A new OPEN must work, and the first count
       of the new configuration must be 0.
    B: idle. The session is open and idle when the FPGA is programmed. The next request must end
       with UnknownOutcome and must not execute.
    """
    results = {}
    cfg_over = (
        {} if default_limits else dict(max_retries=2000, resync_retries=2000, open_retries=200)
    )
    for variant, rep in [(v, r) for r in range(reps) for v in variants]:
        rig = make_rig(board, spec, budget, seed=seed + rep, cfg_over=cfg_over)
        led = Ledger()
        rng = random.Random(seed)
        try:
            open_session(rig)
            # Warm-up: 30 requests with exact counts.
            drive(rig, led, 30, fixed_gen(rng, 32), limit_s=60)
            pre = led.check()
            before_count = pre["last_count"]
            proc_holder: dict = {}
            events: dict = {"t_start": None, "t_done": None}

            def launch() -> None:
                cmd = ["openFPGALoader", *board.loader, "--bitstream", str(board.bit)]
                events["t_start"] = time.perf_counter()
                proc_holder["p"] = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )

            swapped = False
            if variant == "busy":
                state = {"launched": False, "swap_back": False}

                def hook(submitted: int, done: int) -> bool:
                    nonlocal swapped
                    if not state["launched"] and done >= 20:
                        if board.name == "ulx3s":
                            # One interface serves JTAG and UART: release the port first.
                            rig.detach.detach()
                            swapped = True
                        launch()
                        state["launched"] = True
                    p = proc_holder.get("p")
                    if p is not None and p.poll() is not None and events["t_done"] is None:
                        events["t_done"] = time.perf_counter()
                        if swapped:
                            rig.detach.attach(Tap(make_transport(board, spec, purge_on_open=False)))
                    if rig.dev.protocol.state is State.LOST:
                        return True
                    # Give up 20 s after the programmer ended if the host never noticed.
                    return (
                        events["t_done"] is not None and time.perf_counter() - events["t_done"] > 20
                    )

                # Keep the pipeline full until the session is lost.
                rig.tap.trace = []
                out = drive(
                    rig, led, 1_000_000, fixed_gen(rng, 64), reopen=False, hook=hook, limit_s=300
                )
                detected = rig.dev.protocol.state is State.LOST
                if not detected:
                    tail_note = "host did not end the session within 20 s of the programmer"
                    rig.dev.abort()
                    drain(rig.dev, out["_pending"], led)
                else:
                    tail_note = ""
                lost_reason = rig.dev.protocol.lost_reason or tail_note
            else:
                if board.name == "ulx3s":
                    rig.detach.detach()
                    swapped = True
                launch()
                p = proc_holder["p"]
                p.wait(timeout=300)
                events["t_done"] = time.perf_counter()
                if swapped:
                    rig.detach.attach(Tap(make_transport(board, spec, purge_on_open=False)))
                out = {}
                # The FPGA is new. The host does not know. The next request ends the session.
                payload = rng.randbytes(40)
                ticket = rig.dev.submit(payload)
                pend = collections.deque([(ticket, payload)])
                guard = time.perf_counter() + 60
                while pend and time.perf_counter() < guard:
                    rig.dev.pump(0.001)
                    drain(rig.dev, pend, led)
                detected = rig.dev.protocol.state is State.LOST
                lost_reason = rig.dev.protocol.lost_reason
            proc = proc_holder["p"]
            proc.wait(timeout=300)
            if events["t_done"] is None:
                events["t_done"] = time.perf_counter()
            if swapped and rig.detach.inner is None:
                rig.detach.attach(Tap(make_transport(board, spec, purge_on_open=False)))
            prog_out = (proc.stdout.read() or "")[-200:]
            # After the loss: collect every ticket; none may be left unresolved.
            led.epoch += 1  # the new FPGA starts its count again
            chk_old = led.check()
            trace = rig.tap.trace or []
            t_ref = events["t_done"] or 0
            after = [
                (round(t - t_ref, 3), d, b.hex()[:80], len(b))
                for t, d, b in trace
                if t >= t_ref - 0.5 and d == "rx"
            ][:12]
            stream = b"".join(b for t, d, b in trace if d == "rx")
            tail = {
                "rx_stream_tail_hex": stream[-80:].hex(),
                "rx_after_programmer": after,
                "tx_frames_after_programmer": sum(
                    1 for t, d, b in trace if t >= t_ref and d == "tx"
                ),
                "variant": variant,
                "programmer_rc": proc.returncode,
                "programmer_seconds": (events["t_done"] or 0) - (events["t_start"] or 0),
                "transport_released_during_programming": swapped,
                "session_state": rig.dev.protocol.state.value,
                "reset_detected": detected,  # False: the host never noticed the new FPGA
                "lost_reason": lost_reason,
                "before_count": before_count,
                "tickets": chk_old["tickets"],
                "ok": chk_old["ok"],
                "unknown_sent": chk_old["unknown_sent"],
                "unknown_never_sent": chk_old["unknown_never_sent"],
                "violations": chk_old["violations"],
                "violation_samples": chk_old["violation_samples"],
                "terminal_events": chk_old["terminal_events"],
            }
            # A new OPEN must work, and the count restarts at 0 (a silent retry on the new
            # FPGA would show as a count above 0).
            open_session(rig)
            led2 = Ledger()
            drive(rig, led2, 25, fixed_gen(rng, 24), limit_s=60)
            chk_new = led2.check()
            tail["new_session_first_count"] = chk_new["first_count"]
            tail["new_session_ok"] = chk_new["ok"]
            tail["new_session_violations"] = chk_new["violations"]
            tail["programmer_tail"] = prog_out.strip().splitlines()[-1:] if prog_out else []
            results[f"{variant}{rep}"] = tail
            print(json.dumps(tail, default=str), flush=True)
        finally:
            rig.close()
    return results


def cmd_powerup(board: Board, spec: BackendSpec, budget: int, seed: int) -> dict:
    """What does the FPGA send right after configuration, and does the host accept it?

    The port is open while the programmer runs, so the bytes of the power-up are captured. Then
    the host library is fed a bare delimiter (an empty frame) in the OPENING and ACTIVE states.
    """
    transport = Tap(make_transport(board, spec))
    transport.trace = []
    try:
        transport.purge()
        shared = board.name != "ulx3s"  # one FT231X interface serves JTAG and UART
        if not shared:
            transport.close()
        proc = subprocess.Popen(
            ["openFPGALoader", *board.loader, "--bitstream", str(board.bit)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t_done = None
        end = None
        while end is None or time.perf_counter() < end:
            if shared or t_done is not None:
                transport.read(4096, 0.02)
            else:
                time.sleep(0.02)
            if t_done is None and proc.poll() is not None:
                t_done = time.perf_counter()
                end = t_done + 2.0
                if not shared:
                    transport = Tap(make_transport(board, spec, purge_on_open=False))
                    transport.trace = []
        rc = proc.returncode
        prog_out = (proc.stdout.read() or "")[-300:]
        events = [(round(t - t_done, 3), b.hex()) for t, d, b in transport.trace if d == "rx"]
        after = [(t, h) for t, h in events if t >= -0.5]
        stream_after = b"".join(bytes.fromhex(h) for t, h in events if t >= -0.5)
    finally:
        transport.close()
    # The host library must accept a bare delimiter in every state.
    cfg = host_config(board, budget, spec)
    proto = HostProtocol(cfg, rng=random.Random(seed))
    proto.start(0.0)
    proto.feed(b"\x00\x00\x00", 0.01)
    opening_ok = proto.state is State.OPENING
    st_open = dict(proto.stats)
    # A session needs a real FPGA: reopen on the board and feed bare delimiters then.
    rig = make_rig(board, spec, budget, seed=seed)
    try:
        open_session(rig)
        before = dict(rig.dev.protocol.stats)
        rig.dev.protocol.feed(b"\x00\x00\x00\x00", time.monotonic())
        after_stats = dict(rig.dev.protocol.stats)
        data = rig.dev.transact(b"after the bare delimiters", timeout=5.0)
        active_ok = rig.dev.active and data[4:] == b"after the bare delimiters"
        stats_equal = {k: v for k, v in after_stats.items() if k != "rx_overflow"} == {
            k: v for k, v in before.items() if k != "rx_overflow"
        }
    finally:
        rig.close()
    res = {
        "board": board.name,
        "programmer_rc": rc,
        "programmer_output": prog_out if rc else "",
        "rx_events_around_end_of_programming": after[:6],
        "first_byte_after_programming": stream_after[:1].hex() if stream_after else None,
        "stream_after_hex": stream_after[:32].hex(),
        "host_opening_state_kept": opening_ok,
        "host_stats_after_bare_delimiters_opening": st_open,
        "host_active_state_kept": bool(active_ok),
        "host_stats_unchanged_in_active": stats_equal,
    }
    print(json.dumps(res, default=str))
    return res


# ------------------------------------------------------------------------------------------
# CLI


#: Result keys that must be zero, true or absent in a passing run.
ZERO_KEYS = ("violations", "new_session_violations", "mismatches", "programmer_rc")
TRUE_KEYS = (
    "count_span_ok",
    "reset_detected",
    "host_opening_state_kept",
    "host_active_state_kept",
    "host_stats_unchanged_in_active",
)


def failures(result: object, where: str = "") -> list[str]:
    """The checks of a command result that failed: a violation, a mismatch, a programmer that
    failed, a reset that the host never noticed, a new session that does not start at count 0, or a
    host that lost its state."""
    found: list[str] = []
    if isinstance(result, dict):
        if "reset_detected" in result and result.get("new_session_first_count") != 0:
            # A reconfiguration run: the first count of the new session must be 0. A higher count
            # is a request that ran again on the new FPGA (or a count that is missing).
            found.append(
                f"{where or 'result'}.new_session_first_count = "
                f"{result.get('new_session_first_count')}"
            )
        for key, value in result.items():
            path = f"{where}.{key}" if where else str(key)
            if key in ZERO_KEYS and value:
                found.append(f"{path} = {value}")
            elif key in TRUE_KEYS and value is False:
                found.append(f"{path} = {value}")
            else:
                found += failures(value, path)
    elif isinstance(result, list):
        for index, value in enumerate(result):
            found += failures(value, f"{where}[{index}]")
    return found


def parse_sizes(text: str) -> tuple[object, ...]:
    return tuple(s if s == "max" else int(s) for s in text.split(","))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("board", choices=sorted(BOARDS))
    ap.add_argument(
        "command",
        choices=(
            "program",
            "soak",
            "goodput",
            "latency",
            "variants",
            "fault",
            "blackout",
            "reconfig",
            "stall",
            "trunc",
            "powerup",
        ),
    )
    ap.add_argument("--backend", help="backend(s), comma separated (default: the board's)")
    ap.add_argument("--budget", type=int, help="bridge budget B")
    ap.add_argument("--budgets", help="comma separated list of B")
    ap.add_argument("--n", type=int, help="number of requests")
    ap.add_argument("--sizes", default="16,64,max")
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rates", default="1e-4,1e-3")
    ap.add_argument("--dirs", default="both,h2f,f2h")
    ap.add_argument("--kinds", default=",".join(FAULT_KINDS))
    ap.add_argument("--variants", default="busy,idle")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--default-limits", action="store_true")
    ap.add_argument("--blackout-s", type=float, default=0.2)
    ap.add_argument("--tag", default="", help="suffix of the log file name")
    ap.add_argument("--port", help="the serial port of the board (the serial backends)")
    ap.add_argument(
        "--ftdi", help="pyftdi URL of the UART interface, such as ftdi://ftdi:2232h:SERIAL/2"
    )
    ap.add_argument("--ftdi-serial", help="serial number of the FTDI probe (for openFPGALoader)")
    ap.add_argument(
        "--run-root",
        type=Path,
        default=Path("xeda_run"),
        help="run root of the Xeda build (default: xeda_run)",
    )
    ap.add_argument("--bit", type=Path, help="bitstream (default: the one of the Xeda build)")
    ap.add_argument(
        "--log-dir",
        type=Path,
        default=Path("hw_logs"),
        help="directory of the JSON results (default: hw_logs)",
    )
    a = ap.parse_args()

    set_qos()
    loader = BOARDS[a.board].loader + (["--ftdi-serial", a.ftdi_serial] if a.ftdi_serial else [])
    board = replace(
        BOARDS[a.board],
        serial=a.port,
        ftdi=a.ftdi,
        loader=loader,
        bit=a.bit or a.run_root / BOARDS[a.board].bit_name,
        log_dir=a.log_dir,
    )
    specs = [parse_backend(t) for t in (a.backend or board.soak_backend).split(",")]
    budget = a.budget or board.soak_budget
    budgets = tuple(int(b) for b in a.budgets.split(",")) if a.budgets else board.budgets
    tag = f"_{a.tag}" if a.tag else ""
    name = a.command + tag

    if a.command == "program":
        program(board)
        return
    if a.command == "soak":
        res = cmd_soak(board, specs[0], budget, a.n or 100_000, a.seed)
    elif a.command == "goodput":
        res = cmd_goodput(board, specs, budgets, parse_sizes(a.sizes), a.seconds, a.seed)
    elif a.command == "latency":
        res = cmd_latency(board, specs, budget, a.n or 1000, a.seed)
    elif a.command == "variants":
        res = cmd_variants(board, budget, parse_sizes(a.sizes), a.seconds, a.n or 500, a.seed)
    elif a.command == "fault":
        res = cmd_fault(
            board,
            specs[0],
            budget,
            a.n or 5000,
            a.seed,
            tuple(float(r) for r in a.rates.split(",")),
            tuple(a.dirs.split(",")),
            tuple(a.kinds.split(",")),
        )
    elif a.command == "blackout":
        res = cmd_blackout(
            board, specs[0], budgets if a.budgets else (budget,), a.n or 4000, a.seed, a.blackout_s
        )
    elif a.command == "reconfig":
        res = cmd_reconfig(
            board, specs[0], budget, a.seed, tuple(a.variants.split(",")), a.reps, a.default_limits
        )
    elif a.command == "stall":
        res = cmd_stall(board, specs[0], budgets if a.budgets else (budget,), a.seed)
    elif a.command == "trunc":
        res = cmd_trunc(board, specs[0], budget, a.seed)
    elif a.command == "powerup":
        res = cmd_powerup(board, specs[0], budget, a.seed)
    else:
        raise SystemExit(a.command)
    save(board, name, res)
    failed = failures(res)
    if failed:
        print("FAILED checks:", *failed, sep="\n  ", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
