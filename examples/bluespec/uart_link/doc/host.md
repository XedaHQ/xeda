# LeanLink host library

The Python package `leanlink` (in `host/leanlink/`) is a host for the protocol of
[protocol.md](protocol.md). It needs only the Python standard library. The serial backends need
`pyserial` or `pyftdi`, and they load these libraries only when you create the backend object.

The package has four layers:

| Module | Content |
| --- | --- |
| `host/leanlink/codec.py` | CRC-32C, COBS, rCOBS, the frame builders and the frame parsers. |
| `host/leanlink/protocol.py` | `HostProtocol`: the state machine of the host. It does no I/O and reads no clock. |
| `host/leanlink/device.py` | `LinkDevice`: a blocking API over a transport. |
| `host/leanlink/backends.py` | `SerialBackend` (pyserial), `FtdiBackend` (pyftdi), `LoopbackBackend` (tests). |

## 1. Device API

```python
from leanlink import HostConfig, LinkDevice, SerialBackend

dev = LinkDevice(SerialBackend("/dev/ttyUSB1"), HostConfig(bridge_budget=4096))
dev.open()                      # purge, send OPEN, wait for the OPEN reply
reply = dev.transact(b"hello")  # one request, one response
dev.close()
```

- `open(timeout=None)` starts a fresh session. It raises `OpenFailed` if the session does not
  start.
- `transact(payload, timeout=None)` sends one request and returns the response bytes. It raises
  `ResponseTruncated` if the FPGA set TRUNC (the exception has the bytes that the FPGA kept in
  `partial`). It raises `UnknownOutcome` if the session ended before the response arrived. The
  library never sends such a request again. After `TimeoutError` the request is still in flight and
  can run on the FPGA, but the device keeps no result for it. Use `submit` and `wait_result` to
  keep the ticket.
- `submit(payload)` queues a request and returns a ticket. `pump(wait)` reads the transport,
  runs the protocol and writes the frames that are due. `take_result(ticket)` returns the result
  or `None`. `wait_result(ticket, timeout)` pumps until the ticket has a result.
- `abort()` ends the session now. `close()` aborts and closes the transport.

### Tickets and results

Every request has a ticket. A result has a `status`:

| Status | Meaning |
| --- | --- |
| `OK` | The response arrived. `data` has its payload. |
| `TRUNCATED` | The FPGA flagged TRUNC. `data` is a prefix of the response. It is an error result, not a lost session. |
| `UNKNOWN_OUTCOME` | The session ended before the response arrived. `was_sent` is true if the request left the host (it may have executed). `was_sent` is false if it never left (it did not execute). `reason` says why the session ended. |

The result of a ticket stays available after the session is lost. A pipeline is possible: call
`submit` many times, then `wait_result` for each ticket. The window rule of the protocol limits
the requests in flight to W.

## 2. Configuration

`HostConfig` holds the parameters of the host. The main ones:

| Field | Default | Meaning |
| --- | --- | --- |
| `bridge_budget` | (required) | The bytes that the transport can hold in the FPGA-to-host direction. |
| `rsp_limit` | 255 | The largest response wire length that the host asks for. |
| `max_payload` | 243 | The largest request payload that the caller sends. |
| `byte_time` | 0 | Seconds per wire byte. Timers start when the last byte of a frame leaves the line. |
| `open_timeout`, `open_retries` | 0.05 s, 20 | The OPEN retry timer and its limit. |
| `rto` | 0.02 s | The request retransmission timer (go-back-N). |
| `rewind_timeout` | 0.05 s | The timer for an accepted request without a response (REWIND). |
| `poll_interval` | 0.05 s | The idle timer (POLL). |
| `max_retries` | 12 | Consecutive timer expiries without progress. Then the session is lost. |
| `resync_quiet`, `resync_retries` | 0.2 s, 8 | RESYNC after this much silence while a reply is awaited. At most 8 tries. |

The status reserve R (64) and the RESYNC margin (12) are constants of the protocol
(`RESERVE` and `RESYNC_MARGIN` in `protocol.py`). `HostConfig` refuses a configuration that breaks
the budget rules: `bridge_budget + 64` must be below 32768, and `rsp_limit` must be at most the data
window `bridge_budget - 76`.

## 3. Budget and grant

`bridge_budget` is B of the protocol: the number of FPGA-to-host bytes that the bridge can hold.
The library puts `grant = consumed + bridge_budget - 64 - 12` in every frame. The 64 bytes are the
status reserve R. The 12 bytes are for the one status frame that RESYNC can permit. The rules are
`bridge_budget + 64 < 32768` and `12 <= rsp_limit <= bridge_budget - 76`.

The OPEN frame carries grant 0. The OPEN reply needs `grant - 12 >= -R`, which 0
satisfies. The first frame of the session carries the full grant.

## 4. Backends

A backend has four methods: `write(data)`, `read(max_bytes, timeout)`, `purge()` and `close()`.

- `SerialBackend(port, baudrate=12_000_000)`: pyserial, 8N1. It waits for data with `select` on
  the port, so it does not poll.
- `FtdiBackend(url, baudrate=12_000_000, latency_ms=1, event_char=None, chunksize=512)`: pyftdi,
  for example `ftdi://ftdi:2232h:SERIAL/2`. A reader thread reads the chip and queues the data.
  It removes the two status bytes of each USB packet and counts chip receive overruns
  (`overruns`).
- `LoopbackBackend(peer, clock)`: in memory, for tests. The peer has `rx(data, now)` and
  `tx(now) -> bytes`. The Python FPGA model in `host/leanlink/fpga_model.py` is such a peer.

### 4.1 FTDI latency timer and event character

An FTDI chip holds received bytes until its latency timer runs out, its buffer is full, or the
event character arrives. The chip default for the timer is 16 ms. This delays a short reply by
up to 16 ms. A stop-and-wait loop that sends the next request right after a reply locks onto the
timer, and every request takes 16 ms.

Set the event character to `0x00` (the frame delimiter). Then the chip sends its buffer at once
when a frame ends. `FtdiBackend(url, event_char=0)` does this. It writes both settings every
time, so a setting that an earlier program left in the chip never stays.

Measured on an Arty A7-100T at 12 Mbaud with a 16-byte request (median of 1000 requests):

| Setting | Round trip |
| --- | --- |
| Chip default (16 ms timer, no event character) | 15.99 ms |
| Timer 1 ms only | 0.998 ms |
| Event character `0x00` only | 0.214 ms |
| Timer 1 ms and event character `0x00` | 0.215 ms |

The event character alone gives the full gain. The settings stay in the chip after the program
ends. A later session with another driver (for example the macOS serial driver) then also sees
the low latency. This is not durable: a power cycle or a USB reset clears it. The FT231X chip
(ULX3S board) did not keep the settings in the test.

### 4.2 Bridge budget

The bridge FIFO limits `bridge_budget`. A larger value costs retransmissions, but it never
gives wrong results. Measured limits:

| Bridge | Limit that lost no byte |
| --- | --- |
| FT2232H (Arty, Basys-3) | 4096 bytes (a window of 8 x 255 bytes fits the 4 KB FIFO) |
| FT231X (ULX3S, 512-byte FIFO) | up to 500 bytes (1024 gives chip overruns) |
| CH9102 (STLV7325, no flow control) | 256 and 512 lost no byte. 1024 lost frames in some stall tests, and 2048 in one of four. |

The data window of a budget is `bridge_budget - 76` bytes, and `rsp_limit` must fit in it. A budget
of 2048 allows `rsp_limit=255`. A budget of 256 allows `rsp_limit=180` at most.

Keep the number of bytes in flight well below the chip FIFO. A host that is busy can fall behind
the line rate and lose bytes in the chip.

## 5. Behavior of the host library

- It builds every frame at send time, so each frame has a fresh `ack`, `grant` and REWIND
  epoch.
- It picks a random session ID with four nonzero bytes. It uses the same ID for the repeats of
  one OPEN.
- It learns W and `maxLen` from the OPEN reply. It ends the session if `max_payload` exceeds
  `maxLen - 12`. If an unbound FPGA answers an OPEN, the host lowers `rsp_limit` to the `maxLen`
  that the reply carries and sends the OPEN again.
- It sends request `s` only after the response to `s - W` arrived. It sends a header-only frame
  with new credit when the FPGA might be blocked, but only while it waits for a reply.
- Go-back-N goes from `exp`, once per timer expiry (not at once on a status frame). The REWIND
  epoch advances on the response timer. An idle POLL runs on the idle timer. RESYNC goes in a
  header-only frame.
- Every timer has a retry limit with back-off (factor 2, at most 8). The counters reset on
  progress. A limit that runs out ends the session.
- An FPGA frame that is valid under S = 0 during the session ends the session (UNBOUND).
- After the session ends, every ticket without a result becomes `UNKNOWN_OUTCOME`. `submit`
  raises `SessionClosed`.

## 6. Python reference models

- `host/leanlink/codec.py` is the reference codec of the protocol. It checks frames in a fixed order
  (structure, length, header, CRC) and names the first check that fails.
- `host/leanlink/fpga_model.py` is a Python model of the FPGA side (`FpgaModel`), written from the
  specification and not from the RTL. `sim.py` runs the host library against it in a
  discrete-event simulation with random faults.
