# UART packet transport

This example connects a Bluespec core to a host through a UART byte stream. It adds CRC-32C,
request sequence numbers, cached responses, host-driven retries, and byte credit. A separate
diagnostic service provides ECHO, PRBS generation and checking, build information, and counters.

The packet link does not depend on a board, USB bridge, baud rate, or application protocol.
`mkUartLink` connects it to the [shared UART PHY](../uart/README.md) and diagnostic service.
Use `mkReliableLink` directly to connect another byte transport or application service.

## Build and simulate

Run these commands from the repository root. They need BSC 2026.07.1 or newer.

```sh
xeda run bsc examples/bluespec/uart_link/uart_link.yaml -s promote_warnings=ALL
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml -s simulator=bluesim
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml -s simulator=iverilog
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml -s simulator=verilator
xeda run yosys_fpga examples/bluespec/uart_link/uart_link.yaml --target artix7
```

The top uses the caller's clock and reset. It has no pin constraints or power-on reset circuit.
The default clock is 100 MHz and the default baud rate is 12 Mbaud. Set `CLK_HZ` and `BAUD`
together when the board clock differs. A board wrapper must connect the UART pins and provide
reset. See the UART PHY example for the supported baud rates and timing constraints.

The example testbench checks packet acceptance, CRC rejection, cached replay, delivery
acknowledgment, and the transmit grant bound. Additional tests check both codecs, the PHY fault
fence, and the real Python host state against the RTL through a bounded byte bridge:

```sh
pytest tests/test_uartlink_host.py tests/test_uartlink_example.py
pytest tests/test_bsc_examples.py -k uart_link
```

## Connect an application

Instantiate `mkReliableLink(frameLimit, windowLimit, slotBytes)`. The constructor returns
`ReliableLinkIfc`, defined in `rtl/UartLinkTypes.bsv`.

| Port | Direction at the application | Meaning |
| --- | --- | --- |
| `requestHeader` | Get | Verified sequence, request length, maximum response length |
| `requestPayload` | Get | Exactly the request length in bytes |
| `responseHeader` | Put | Request sequence and response length |
| `responsePayload` | Put | Exactly the response length in bytes |
| `rx` | Put | Encoded host bytes |
| `tx` | Get | Encoded FPGA bytes |

Read a complete request payload before reading another request header. A zero-length request
has no payload transfers. Check `maxResponse` before starting an operation with side effects.
Consume all request payload bytes before producing the response header.
Every accepted request must produce one response with its original sequence number. A
zero-length response is allowed. The application must honor all port readiness signals.

The link stores the original response until the host acknowledges delivery. A retry returns
those stored bytes. It does not send the operation to the application again. Acceptance of a
request and delivery of its result use separate acknowledgment state.
While a response or replay is in flight, the link rejects an incoming frame whose ACK covers
that response. It also ignores the frame's grant update. The host retries after transmission ends.

The current implementation supports frame limits from 32 through 240 bytes, one through eight
request slots, and slot sizes through 256 bytes. These are static constructor parameters.
Negotiation can reduce the active frame and window limits. The two payload stores each allocate
2048 bytes, including when a smaller profile is selected. Smaller profiles do not currently
reduce these memory allocations. The 240-byte limit includes the header and CRC.
The diagnostic top requires slot sizes of at least three bytes for its error responses.

`abortRx` cancels speculative receive data and discards input through the next delimiter. A
transport adapter must also discard older bytes in its own FIFO before selecting that delimiter.
`mkUartLinkAdapter` implements this fence for the shared UART PHY. It handles framing errors,
parity errors, breaks, and overruns, including faults during application backpressure.
The adapter requires the PHY's `events` and `rx.get` methods to be conflict-free: no path
from the dequeue to `events`, and no required order between these methods.
For the shared PHY, use a power of two from four upward; the default is four.
The example top rejects `RX_DEPTH=1` and `RX_DEPTH=2`, whose FIFO schedules conflict with fault tracking.

## Use the Python host

The host package is in `host/uartlink`. Its codecs and protocol state need only Python.
Install `pyserial` for an operating system serial port, or `pyftdi` for direct FTDI USB access.
The diagnostic command defaults to a conservative 256-byte receive budget:

```sh
python examples/bluespec/uart_link/host/uart_link_diag.py --port /dev/ttyUSB1 --baud 12000000
python examples/bluespec/uart_link/host/uart_link_diag.py --port COM5 --baud 12000000 --stats
python examples/bluespec/uart_link/host/uart_link_diag.py \
  --ftdi 'ftdi://ftdi:2232h:<serial>/2' --baud 12000000 --bytes 180
```

Find serial ports with `python -m serial.tools.list_ports -v`. Find FTDI URLs with
`ftdi_urls.py`. Replace the example port or URL with the connected device.

For application code, add this example's `host` directory to the Python module search path:

```python
from uartlink import Device
from uartlink.backends import PySerialBackend

transport = PySerialBackend("/dev/ttyUSB1", 12_000_000)
device = Device(transport, byte_budget=256)
try:
    device.open()
    assert device.echo(b"hello") == b"hello"
finally:
    device.close()
```

`transact(payload)` returns the known application response. If it cannot establish the result
after submission, it terminates the link and raises `UnknownOutcome`. Do not resubmit that
operation automatically. A failed final POLL cannot hide an already received response, but the
device cannot be reused. `KeyboardInterrupt` terminates the link and propagates.
For pipelined requests, `submit(payload)`
returns a monotonic ticket. Call `pump()` to process incoming bytes and retries, then call
`take_result(ticket)` to consume a result. It returns `None` while that ticket is pending.
`wait_result(ticket)` combines polling and waiting. Its timeout leaves the ticket pending, so
the caller can wait again. A sequence number remains reserved until
its result ticket is consumed. `ProtocolState` exposes the same reliability state without I/O
for asynchronous transports and custom event loops. Pass the actual encoded frame length to
its response handlers so that valid replies release byte credit. After `mark_reset()`, create a
new protocol state; the reset state cannot reopen its old session.
Call `abort()` after an application error to mark pending outcomes unknown and close the backend.
It does not send CLOSE. Reset the FPGA and drain the backend before starting a session after an abort.

Choose `byte_budget` from the receive storage that remains safe while the host reader pauses.
Count bridge, driver, and application limits conservatively. A larger operating system read
buffer alone does not protect a smaller USB-UART FIFO. The budget must stay below 32768 bytes.
The default 256-byte profile selects a 209-byte decoded frame and reserves 45 bytes for recovery.
The largest request or response payload in that profile is 196 bytes.
The diagnostic command accepts `--byte-budget` and `--window` for other bridge limits.
A larger safe budget and pipelined requests can reduce idle time between USB transfers.

## Wire format

All multi-byte fields are little-endian. Each decoded frame has this layout:

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 1 | Version, kind, flags |
| 1 | 1 | Application sequence or control token |
| 2 | 1 | Cumulative acknowledgment |
| 3 | 2 | Transmit byte cursor in replies; grant edge in requests |
| 5 | 4 | Nonzero session identifier |
| 9 | variable | Application or control payload |
| end minus 4 | 4 | CRC-32C |

Version 1 uses control byte bits 7:6 equal to `01`. Bits 5:4 select request, response, control,
or control response. Bits 3:2 are reserved and must be zero. Response flags are GAP in bit 0 and
replay in bit 1. Control selectors are POLL=0, CAPABILITIES=1, OPEN=2, and CLOSE=3.

CRC-32C covers the decoded header and payload. It uses initial state `FFFFFFFF`, reflected
polynomial `82F63B78`, and final XOR `FFFFFFFF`. The check value for `123456789` is `E3069283`.
The uncomplemented residue after the appended little-endian CRC is `B798B438`.

Host frames use COBS and a zero delimiter. FPGA frames use streaming reverse COBS and a zero
delimiter. Nonzero bytes pass through immediately. Each zero produces the preceding run length
plus one. A full 254-byte nonzero run produces `FF`, followed by a new run. Every frame ends with
its final run code and delimiter. Thus 254 nonzero bytes encode as `data254 FF 01 00`.

OPEN carries the requested frame limit as `u16`, the requested window as `u8`, and a zero reserved
byte. Its grant field carries the initial receive budget. The four-byte OPEN response returns
the selected limits and a zero reserved byte. POLL has an empty payload. CAPABILITIES returns
the compiled frame and window limits and feature flags. CLOSE has an empty payload.

Each physical reply, including a replay, has a fresh byte cursor and CRC. A valid reply lets the
host extend the grant from that frame's actual encoded end. Raw received byte counts and timeout
events cannot extend credit. Whole-frame bounds include stuffing, the CRC, and the delimiter.

## Diagnostics and recovery limits

The diagnostic service accepts one operation per request:

| Request | Successful response |
| --- | --- |
| `00` | `80` |
| `02` | `82`, clock Hz `u32`, baud `u32`, build ID `u32` |
| `03`, counter index `u8` | `83`, index `u8`, value `u32` |
| `30`, length `u16`, data | `B0`, data |
| `31`, seed `u32`, length `u16` | `B1`, generated PRBS bytes |
| `32`, seed `u32`, length `u16`, data | `B2`, mismatched byte count `u16` |

An error response is `FF`, error code, and operation. Error codes are BAD_OP=1, LENGTH=2,
RANGE=3, and INTERNAL=4. PRBS uses a nonzero 31-bit seed. Seed bit 31 must be zero. Output bit
7 comes from state bit 30; each shift inserts the XOR of state bits 30 and 27.

Counter indices 0 and 1 count encoded bytes accepted and produced by the packet transport.
They exclude bytes discarded in the UART adapter's quarantine. Indices 2 through 15 count
framing errors, parity errors, breaks, overruns, valid requests, COBS errors, CRC errors,
length errors, header errors, gaps, duplicates, receive drops, response transmissions, and
credit blocks. Counters saturate at `FFFFFFFF`.

Recovery uses detection and automatic repeat request (ARQ). There is no forward error correction.
Loss of all reserved recovery replies causes an explicit failure. A reset ends the session and
makes outstanding operation outcomes unknown. Do not automatically resubmit those operations
in a new session. Before reopening after reset or an uncertain close, reset the peer and drain
the transport. Protection from stale bytes across FPGA reboot needs that external contract or
a separate boot epoch mechanism.
