# uart_link: a reliable packet link over a UART

LeanLink is a small transport for a byte stream, such as a UART behind a USB bridge. A host sends
requests, an FPGA core runs them, and the responses come back. The link detects damaged frames with
a CRC-32C, repeats lost frames, and never runs a request twice. If a session is lost, the host gets
a terminal "unknown outcome" for the requests in flight. It does not send them again by itself.

The core is written in Bluespec with library components only. It needs about 370 LUT on an
Artix-7. Read [doc/protocol.md](doc/protocol.md) for the protocol and
[doc/architecture.md](doc/architecture.md) for the design of the core.

```
host <-> USB bridge <-> UART PHY (uart example) <-> LeanLink <-> core (request, response)
                                                    |  CRC-32C, COBS, window, ARQ, session
                                                    +-- request pages, response pages (block RAM)
```

| path | content |
|---|---|
| `rtl/LeanLink.bsv` | `mkLeanLink`, the core: a byte stream on one side, `request` and `response` streams of tokens on the other |
| `rtl/LeanLinkTestTop.bsv` | the test endpoint: the response is the number of earlier requests, then the request payload |
| `rtl/LeanLinkBoard.bsv` | `mkLeanLinkBoard`, the board top: PHY, link and endpoint, with the pins of `mkUartEcho` |
| `rtl/LeanLinkTops.bsv` | the bare core as a top, for area measurements |
| `boards/` | the Basys-3 pins and the STLV7325 v2 wrapper (the other pin files are in `../uart_echo/boards/`) |
| `tb/TbLeanLink.bsv`, `tb/LeanLinkFrames.bsv` | the self-checking bench and the frames it plays |
| `tb/gen_frames.py` | writes `tb/LeanLinkFrames.bsv` from the Python codec |
| `tb/cosim.yaml`, `tb/lean_*.py` | the cocotb tests against the Python codec |
| `tb/run_cosim.py` | runs the full cocotb suite (several minutes) |
| `host/leanlink/` | the host library: codec, protocol state machine, device, backends, a model of the FPGA |
| `host/hw_suite.py` | tests a real board |
| `uart_link.yaml` | the design file: bench, FPGA flows, board targets |

## The protocol in short

A frame is one packet: a 6-byte header, a payload of up to 243 bytes, and a 4-byte trailer that is
the CRC-32C of the frame XOR the session ID. Host frames use COBS and FPGA frames use streaming
reverse COBS, and a zero byte ends each frame. Requests and responses are numbered. The FPGA
accepts the next `W` requests in pages of RAM, so the window needs a few counters. The host
repeats a request that the FPGA did not accept (go-back-N), and it asks for a response again with a
REWIND epoch. A credit scheme (`grant` and `cursor`) keeps the bytes in the USB bridge below the
size that the host chose. An OPEN frame starts a session. Read [doc/protocol.md](doc/protocol.md)
for the details.

## Area and performance

The area is the output of the Xeda flow `yosys_fpga` (Yosys 0.70+179, bsc 2026.07.1, default
settings, part `xc7a100tcsg324-1`, 10 ns clock) for the bare core, without the PHY and the test
endpoint. Run `xeda run yosys_fpga examples/bluespec/uart_link/uart_link.yaml --target core_w8`:

| profile | target | logic LUT | LUT RAM | FF | CARRY4 | RAMB18 |
|---|---|---|---|---|---|---|
| W = 8 pages of 256 bytes | `core_w8` | 370 | 8 | 461 | 36 | 2 |
| W = 1 page of 64 bytes | `core_w1` | 332 | 24 | 462 | 26 | 0 |

The target `artix7` (PHY, link and endpoint) needs 476 logic LUT, 16 LUT RAM, 578 FF and 2 RAMB18.

The board builds of the design with PHY and endpoint (W = 8) met their clocks:

| board | flow | LUT | FF | block RAM | timing |
|---|---|---|---|---|---|
| Arty A7-100T, 100 MHz | `vivado_synth` (Vivado 2025.2) | 454 (434 logic, 20 LUT RAM) | 560 | 2 RAMB18 | WNS +2.917 ns, Fmax 141.2 MHz |
| Basys-3, 100 MHz | `vivado_synth` (Vivado 2025.2) | 453 (433 logic, 20 LUT RAM) | 560 | 2 RAMB18 | WNS +2.788 ns, Fmax 138.7 MHz |
| ULX3S, 25 MHz | `yosys_fpga`, `nextpnr` | 846 TRELLIS_COMB | 635 | 2 DP16KD | Fmax 79.0 MHz |
| STLV7325 v2, 200 MHz | `yosys_fpga`, `nextpnr` (openXC7) | 560 | 586 | 2 RAMB18 | Fmax 214.0 MHz, WNS +0.328 ns |

The hardware results come from `host/hw_suite.py` on a Mac, with the Arty and the Basys-3 at
12 Mbaud, and the ULX3S and the STLV7325 at 3 Mbaud:

- Soak: 100,000 requests of random sizes on every board, with every echo byte and every execution
  count checked. No request ran twice and no byte was wrong.
- Faults: with a host backend that drops, corrupts or inserts bytes at a rate of 1e-3 per byte in
  both directions, 60,000 requests per board all got the exact response. No session was lost.
- Blackouts of 200 ms (both directions, or one direction): 4000 requests per run, all answered, the
  session kept.
- Reconfiguration of the FPGA during a busy session: the host saw the UNBOUND status frame in every
  run (40 runs on the four boards) and no request ran again.
- Goodput on the Arty with the largest requests (239 bytes), pyftdi, latency timer 1 ms and event
  character `0x00` (see the end of this file): 1119 kB/s, 93.3 % of the line rate at 12 Mbaud.
- Latency of a 16-byte request on the Arty with the same settings: 0.22 ms (median). With the chip
  as shipped it is 16 ms.

## Simulation

The bench is a behavioral host at the byte interface of the link. It plays the frames of
`tb/LeanLinkFrames.bsv` and compares each byte that the link sends: the delimiter after reset, the
UNBOUND status frame, the OPEN reply, a request and its response, a frame with a wrong CRC and a
frame with a wrong COBS structure (neither runs), a duplicate request (a status frame, no second
execution), a request without payload and the largest request (the response is cut and has TRUNC).
The frames come from the Python codec, so the bench checks CRC-32C and COBS against it. After a
change of `tb/gen_frames.py`, write the table again:

```bash
python3 examples/bluespec/uart_link/tb/gen_frames.py
```

```bash
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml -s simulator=verilator
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml -s simulator=iverilog
```

Defining `XEDA_INJECT_BUG` makes the test endpoint count every request twice. The bench must catch
it:

```bash
xeda run bsc_sim examples/bluespec/uart_link/uart_link.yaml \
    --design-overrides tb.defines.XEDA_INJECT_BUG=true
```

The cocotb tests run on the Verilog that bsc makes, with Verilator. They are in `tb/lean_*.py` and
use the Python codec of `host/` for every frame. They check, among others, that a damaged frame
changes no state, every OPEN and session case, duplicates and gaps, the replay of a lost response,
credit and the status reserve, the wrap of the counters, every request size, performance at
12 Mbaud and 100 MHz, and random faults in both directions.

```bash
xeda run verilator examples/bluespec/uart_link/tb/cosim.yaml
xeda run verilator examples/bluespec/uart_link/tb/cosim.yaml --target w1
```

The first command runs all the tests for W = 8 (a few minutes). The test suite of Xeda runs a
subset of them. `tb/run_cosim.py` runs every test on four targets (W = 8, W = 1 and the two bare
cores) with a number of random seeds of your choice:

```bash
python3 examples/bluespec/uart_link/tb/run_cosim.py --seeds 100
```

The tests read some registers of the core by name, so they need the Verilog of `bsc` with the
register names that `mkLeanLink` has.

## Synthesis and boards

```bash
xeda run yosys_fpga examples/bluespec/uart_link/uart_link.yaml --target core_w8
xeda run yosys_fpga examples/bluespec/uart_link/uart_link.yaml --target core_w1
xeda run yosys_fpga examples/bluespec/uart_link/uart_link.yaml --target artix7
xeda run yosys_fpga+nextpnr examples/bluespec/uart_link/uart_link.yaml --target ecp5
xeda run vivado_synth examples/bluespec/uart_link/uart_link.yaml
```

The macros `LL_LOGW` and `LL_LOGP` set the window and the pages: `W = 2^LL_LOGW` pages of
`2^LL_LOGP` bytes. `LL_LOGW` is at most 7, and `LL_LOGP` is at least 4. The link refuses other
values at elaboration. Set them with `rtl.defines` or with `--design-overrides`. `CLK_HZ` and
`BAUD` set the PHY.

| target | board | tools | clock | rate | UART bridge |
|---|---|---|---|---|---|
| `arty` | Arty A7-100T | Vivado | 100 MHz | 12 Mbaud | FT2232H, channel B |
| `basys3` | Basys-3 | Vivado | 100 MHz | 12 Mbaud | FT2232H, channel B |
| `ulx3s` | ULX3S 85F | Yosys, nextpnr-ecp5 | 25 MHz | 3 Mbaud | FT231X |
| `stlv7325` | SITLINV STLV7325 v2 | openXC7 | 200 MHz | 3 Mbaud | WCH CH9102 |

Run the commands from the root directory of the repository. The `bin` directory of openXC7 (for
`stlv7325`) or of oss-cad-suite (for `ulx3s`) must be first on `PATH`. Add `+openfpgaloader` to
program the SRAM of the board through its USB cable. The design is gone at power-off. The pin
files are those of the [`uart_echo`](../uart_echo/README.md) example, and the board top has the
same pins. Switch `sw[0]` of the board does nothing here, unless you define `LL_STALL_SW=1`: then
it stalls the test endpoint. The LEDs show the state of the endpoint and the PHY (`led[0]`
`responseReady`, `led[1]` the lowest bit of the execution count, `led[2]` a framing, parity or
break error, `led[3]` a receive overrun).

```bash
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/uart_link.yaml --target arty
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/uart_link.yaml --target basys3
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader examples/bluespec/uart_link/uart_link.yaml \
    --target ulx3s
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader examples/bluespec/uart_link/uart_link.yaml \
    --target stlv7325
```

## The host library

`host/leanlink/` has no dependency. A serial port needs `pyserial` and a direct FTDI connection
needs `pyftdi`. Each loads when you create the backend that needs it. `uv sync` or
`pip install --group examples` installs `pyserial`. Install `pyftdi` yourself, and libusb
(`brew install libusb` on macOS; a uv-managed Python needs
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`). `SerialBackend` works on POSIX systems only (Linux
and macOS).

```python
import sys
sys.path.insert(0, "examples/bluespec/uart_link/host")
from leanlink import HostConfig, LinkDevice, SerialBackend

config = HostConfig(bridge_budget=2048, rsp_limit=255)
device = LinkDevice(SerialBackend("/dev/cu.usbserial-XXXX", baudrate=12_000_000), config)
device.open()
print(device.transact(b"hello"))    # 4-byte count of earlier requests, then b"hello"
```

`bridge_budget` (B) is the number of bytes that the USB bridge can hold for you. The FPGA never
sends more than B bytes that the host has not read. Use a value below the receive buffer of the
bridge. The data window is `bridge_budget - 76` bytes (64 for the status reserve, 12 for RESYNC),
and `rsp_limit` must fit in it. For the FT2232H (4 KiB buffer) use `bridge_budget=2048` and
`rsp_limit=255`. For the CH9102 and the FT231X use `bridge_budget=256` and `rsp_limit=180`.
`device.submit(payload)` and `device.wait_result(ticket)` keep several requests in flight.
`device.transact` raises `UnknownOutcome` if the session is lost while the request is in flight.

`host/leanlink/fpga_model.py` is a model of the FPGA at the frame level, and
`host/leanlink/sim.py` runs the
host protocol against it with injected faults. Put the `host` directory on `PYTHONPATH`:

```bash
PYTHONPATH=examples/bluespec/uart_link/host python3 -m leanlink.sim campaign --seeds 60 --jobs 4
```

## The hardware script

`host/hw_suite.py` tests a board that runs `mkLeanLinkBoard`. It uses the host library only. The
commands are `program`, `soak`, `goodput`, `latency`, `variants`, `fault`, `blackout`, `reconfig`,
`stall`, `trunc` and `powerup`. Run `--help` for the options. It loads a bitstream into the SRAM
only. It never writes the flash.

```bash
python3 examples/bluespec/uart_link/host/hw_suite.py arty soak \
    --ftdi ftdi://ftdi:2232h:SERIAL/2 --n 100000
python3 examples/bluespec/uart_link/host/hw_suite.py stlv7325 soak \
    --backend serial:fast --port /dev/cu.usbserial-XXXX
python3 examples/bluespec/uart_link/host/hw_suite.py ulx3s latency --ftdi ftdi://ftdi:ft-x:SERIAL/1
```

The script reads the bitstream of a Xeda build from the run root `./xeda_run` (`--run-root`), or you
give it with `--bit`. Results go to `hw_logs/*.json` (`--log-dir`).

### The latency of the FTDI chips

A FTDI chip holds the bytes that it receives until a USB packet is full, or until its latency
timer ends. The timer is 16 ms by default, so a stop-and-wait host sees 16 ms per request. Two
settings remove it. A latency timer of 1 ms limits the wait. An event character makes the chip send
the packet at once when it sees the character. The frames of LeanLink end with a zero byte, so
`0x00` is the event character to use: every frame leaves the chip as soon as it has arrived. The
settings are in the chip until it is reset or powered off. With `pyftdi`, the backends of the
script set both (`ftdi:1:0` is a latency timer of 1 ms and the event character `0x00`), and
`FtdiBackend(..., latency_ms=1, event_char=0)` does the same in your own program.

The FT231X of the ULX3S does not keep the setting after pyftdi closes the chip, so use `--ftdi`
there. The
CH9102 of the STLV7325 has no latency timer: use the backend `serial:fast`, which tells the host
timers that the chip answers within 1 ms.

Two rules for host programs that use pyserial: never assign `ser.timeout` (or another attribute) of
an open port while data flows, because pyserial then rewrites the terminal settings and some
drivers turn that into USB control transfers in the middle of the traffic. `SerialBackend` waits
with `select` on the file descriptor for that reason. And keep the bytes in flight well below the
receive buffer of the bridge, with `bridge_budget`.
