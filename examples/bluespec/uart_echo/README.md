# uart_echo: a UART echo demo for FPGA boards

Everything the host sends comes back. The design is the board demo of the
[`uart`](../uart/README.md) PHY. One board-agnostic top, `mkUartEcho`, builds for three boards: the
Arty A7-100T, the ULX3S and the STLV7325 v2.

```
rxd -> UartCore (receiver) -> echo FIFO (block RAM) -> UartCore (transmitter) -> txd
sw[3:0] -> baud rate table ---^                       btn -> clear flags      led[3:0] <- activity, flags
```

| file | content |
|---|---|
| `rtl/UartEcho.bsv` | `mkUartEcho` (the top) and `mkEcho` (the core it wraps) |
| `tb/TbUartEcho.bsv` | the testbench: a behavioral host at the pins |
| `boards/` | pin and timing constraints of the Arty and the ULX3S; the Bluespec wrapper `Stlv7325V2.bsv` |
| `host/uart_echo_test.py` | the host side: sends random bytes, checks the echo, reports throughput |
| `uart_echo.yaml` | the design file: simulation, board targets |

## The design

`mkUartEcho` has the clock `CLK` and no reset pin: it makes its own power-on reset.

| port | direction | meaning |
|---|---|---|
| `rxd`, `txd` | in, out | the UART, 8N1 |
| `sw[3:0]` | in | the baud rate, with `FIXED_BAUD=0` (`sw[0]` is the lowest bit) |
| `btn` | in | clears the sticky LEDs |
| `led[0]` | out | a frame was received |
| `led[1]` | out | a byte was sent |
| `led[2]` | out | sticky: framing error, parity error or break |
| `led[3]` | out | sticky: overrun (the PHY dropped a byte because its receive buffer was full) |

The activity LEDs stay on for 0.4 to 1 times `ACTIVITY_US` after the last event, so a person can
see them at 12 Mbaud. The echo FIFO is `ECHO_DEPTH` bytes of block RAM. The PHY buffers hold one
byte each.

Set the macros with `rtl.defines` in the design file, in a target, or with `--design-updates`.
All have defaults.

| macro | meaning | default |
|---|---|---|
| `CLK_HZ` | clock frequency in Hz | 100000000 |
| `BAUD` | the baud rate (with `FIXED_BAUD=0`: the rate for `sw` = 0000) | 12000000 |
| `FIXED_BAUD` | 1: one baud rate, `BAUD`, and `sw` is ignored. 0: the switches select the rate | 1 |
| `ECHO_TRANSFORM` | 1: the echo returns `reverseBits(b) ^ 8'hB1`, see [the transformed echo](#the-transformed-echo) | 0 |
| `MIN_BAUD` | the slowest rate of the switch table (not used with `FIXED_BAUD=1`) | 115200 |
| `ECHO_DEPTH` | echo FIFO size in bytes (one 18 Kbit block RAM) | 2047 |
| `ACTIVITY_US` | the longest time an activity LED stays on, in microseconds | 100000 |
| `HOLD` | the receiver's glitch filter depth in clocks; see the [`uart`](../uart/README.md) README | 3 |

`FIXED_BAUD` and `ECHO_TRANSFORM` must be `0` or `1`. Xeda writes a YAML `true` as a bare `-D NAME`
and leaves a YAML `false` out, so a number is the only value that can switch a macro off again in a
target or an override. Any other input does not build: `false` gives the default, `true` and text
such as `yes` are bsc errors, and `2` stops with `FIXED_BAUD is 2: write 0 or 1`.

```bash
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml --design-updates rtl.defines.FIXED_BAUD=0
xeda run yosys_fpga+nextpnr+fpga_pack examples/bluespec/uart_echo/uart_echo.yaml --target stlv7325 \
    --design-updates rtl.defines.ECHO_TRANSFORM=1 rtl.defines.BAUD=115200
```

### Switches (`FIXED_BAUD=0`)

The switches index a table that the design computes at elaboration from `CLK_HZ`: index 0 is `BAUD`,
then the rates a USB bridge can generate (12 M, 8 M, 6 M, 4 M, 3 M, 2 M, 1.5 M, 1 M, 921600, ...
down to `MIN_BAUD`) that have at least 8 clocks per bit. An index past the end selects the slowest
rate. Each rate is within 0.07 % of the nominal one.

| `sw[3:0]` | Arty A7-100T, 100 MHz | ULX3S, 25 MHz |
|---|---|---|
| 0000 | 12 M | 3 M |
| 0001 | 8 M | 2 M |
| 0010 | 6 M | 1.5 M |
| 0011 | 4 M | 1 M |
| 0100 | 3 M | 921600 |
| 0101 | 2 M | 460800 |
| 0110 | 1.5 M | 230400 |
| 0111 | 1 M | 115200 |
| 1000 | 921600 | 115200 |
| 1001 | 460800 | 115200 |
| 1010 | 230400 | 115200 |
| 1011 to 1111 | 115200 | 115200 |

A new setting takes effect only when the echo is idle: nothing is left to send and the receive line
has been high for 11 bit times of the slowest rate. A frame in progress is never cut. After you flip
a switch, wait a moment and restart the host at the new rate.

With `FIXED_BAUD=1` the design has no table, no switch synchronizers and no idle detector. In Vivado
2024.2 at 12 Mbaud and 100 MHz that is 96 LUT and 139 FF instead of 168 LUT and 187 FF.

## Boards

| target | board | tools | clock | default rate and mode | UART bridge |
|---|---|---|---|---|---|
| `arty` | Arty A7-100T | Vivado | 100 MHz | 12 Mbaud, switches | FT2232H, channel B |
| `arty_nextpnr` | Arty A7-100T | openXC7 | 100 MHz | 12 Mbaud, switches | FT2232H, channel B |
| `ulx3s` | ULX3S 85F | Yosys, nextpnr-ecp5 | 25 MHz | 3 Mbaud, switches | FT231X |
| `stlv7325` | SITLINV STLV7325 v2 (Kintex-7) | openXC7 | 200 MHz | 3 Mbaud, fixed | WCH CH9102 |

The ULX3S has four DIP switches, so the target uses them: it runs at 3 Mbaud only with all four off.
For a rate that no switch changes, add `--design-updates rtl.defines.FIXED_BAUD=1`.

The STLV7325 v2 has no switches. Its target builds `boards/Stlv7325V2.bsv`, a Bluespec wrapper that
adds the `IBUFDS` and `BUFG` of the differential clock and inverts the active-low LEDs and buttons
(`led[3:0]` show the Arty's LEDs, `btn[1]` clears the flags). The pins come from Xeda's bundled
board file (`nextpnr: board: stlv7325_v2`). Its UART goes through the board's CH9102 USB bridge, not
through the FT4232H JTAG probe that programs the board.

## Build and program

Run the commands from the root directory of the repository. Use `--run-root` for a run directory
outside the checkout, and an absolute path for the design file when you run Xeda with
`uv run --directory`. The `bin` directory of openXC7 (for `arty_nextpnr` and `stlv7325`) or of
oss-cad-suite (for `ulx3s`) must be first on `PATH`.

```bash
xeda run vivado_synth examples/bluespec/uart_echo/uart_echo.yaml --target arty                              # about 3 minutes
xeda run yosys_fpga+nextpnr+fpga_pack examples/bluespec/uart_echo/uart_echo.yaml --target arty_nextpnr      # openXC7
xeda run yosys_fpga+nextpnr+fpga_pack examples/bluespec/uart_echo/uart_echo.yaml --target ulx3s
xeda run yosys_fpga+nextpnr+fpga_pack examples/bluespec/uart_echo/uart_echo.yaml --target stlv7325
```

The first openXC7 build of a chip makes its chip database, which takes minutes. Xeda keeps it under
the run root (`--run-root DIR`).

To program the FPGA, add `+openfpgaloader` to the chain. It loads the SRAM through the board's USB
cable: the design is gone at power-off.

```bash
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader examples/bluespec/uart_echo/uart_echo.yaml --target stlv7325
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_echo/uart_echo.yaml --target arty
openFPGALoader -b arty_a7_100t uart_echo.bit        # from a bitstream; -b ulx3s, -b stlv7325
```

With several FTDI probes attached, select one with its serial number:
`-s openfpgaloader.usb_serial_num=<serial>` (`--ftdi-serial` for `openFPGALoader`).

## Run the host test

`host/uart_echo_test.py` sends a pseudo-random payload and compares the echo as it arrives. It
exits with 0 only for a perfect echo: every byte back, in order, nothing extra.

1. Install the packages. From the root of the repository, `uv sync` or
   `pip install --group examples` installs `pyserial`. For direct USB access to an FTDI chip, also
   install `pyftdi` (and libusb: `brew install libusb` on macOS). Homebrew's Python finds
   Homebrew's libusb. A uv-managed Python does not: if `--ftdi` reports `No backend available`,
   set `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`. `--port` does not use libusb.
2. Find the port.
   * macOS: `ls /dev/cu.usbserial-*`
   * Linux: `ls /dev/ttyUSB*` (or `/dev/ttyACM*`)
   * Windows: the port in Device Manager, for example `COM5`
   * any system: `python -m serial.tools.list_ports -v` shows the USB ids, and for pyftdi
     `ftdi_urls.py` lists the URLs, such as `ftdi://ftdi:2232h:<serial>/2`. Channel B of the
     FT2232H is interface 2. Give a serial number that has a letter in it: pyftdi reads an all-digit
     field as a device index.
3. Run it at the baud rate of the design.

```bash
python3 examples/bluespec/uart_echo/host/uart_echo_test.py --loopback-selftest                    # no hardware
python3 examples/bluespec/uart_echo/host/uart_echo_test.py --port /dev/cu.usbserial-<serial> --baud 3000000
python3 examples/bluespec/uart_echo/host/uart_echo_test.py --port COM5 --baud 3000000 --bytes 16777216
python3 examples/bluespec/uart_echo/host/uart_echo_test.py --ftdi ftdi://ftdi:2232h:<serial>/2 --baud 12000000 --bytes 16777216
```

Add `--transform` for a design built with `ECHO_TRANSFORM=1`, `--progress` for a long run, and
`--mode chunked` for a simpler test that writes a chunk and reads it back (79 % of the line rate at
12 Mbaud with 4000-byte chunks). Run `--help` for all options.

### Window and latency

The default pipelined mode writes from one thread and reads from another, with at most `--window`
bytes in flight (sent and not yet echoed). The window must be large enough to keep the line busy
during the USB round trip: line rate in bytes per second times the round trip. It must be small
enough that no buffer on the way overflows. The window must stay below `ECHO_DEPTH` (the script's
limit is 2040) and below the receive buffer of the bridge. The script picks a default for
`--ftdi` from the chip and for `--port` from the USB ids of the port.

The listed defaults stay below the known bridge buffers. A manual `--window` (or `--chunk`, in
chunked mode) overrides the default. With `--port` and a known bridge, a larger value makes the
script print a `WARNING` and still run, so keep it at or below the listed default. With `--ftdi`,
the script refuses a value above the chip's limit.

| bridge | what limits it | default window | notes |
|---|---|---|---|
| FT2232H (Arty) | 4 KiB receive buffer | 1792 | up to 12 Mbaud |
| FT231X (ULX3S) | 512-byte receive buffer, full-speed USB | 448 | up to 3 Mbaud. With `--ftdi`, a larger window is refused |
| CH9102 (STLV7325) | about 320-byte receive FIFO, no flow control | 256 | see [Limits](#limits) |

The FTDI latency timer defaults to 16 ms, which delays every partial USB packet. `--ftdi` sets it
to 1 ms. With `--port` you set it yourself: on Linux,
`echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB1/latency_timer` (or
`setserial /dev/ttyUSB1 low_latency`), on Windows in the advanced properties of the port in Device
Manager. macOS has no setting, so use `--ftdi` from 8 Mbaud up. The CH9102 has no latency timer.

Two rules for host programs:

* With pyserial, never assign `ser.timeout` (or another attribute) on an open port while data flows.
  pyserial then rewrites the terminal settings. Apple's FTDI driver turns that into USB control
  transfers in the middle of the traffic, and bytes are lost. The script sets the timeout once.
* The script clears DTR and RTS on open. On the Arty with jumper JP2 shorted, DTR is wired to the
  FPGA reset.

## The transformed echo

A plain echo test can pass through a wire loopback without testing the FPGA: a loopback plug, a
wrong port, or a programmer that ties TXD to RXD on a spare channel. Build with `ECHO_TRANSFORM=1`
and run with `--transform` to check that the FPGA is in the data path.

With `ECHO_TRANSFORM=1` the design returns `f(b) = reverseBits(b) ^ 8'hB1`, and `--transform` makes
the host expect that. The key is not a bit palindrome, so `f(b) != b` for all 256 bytes: a wire
returns `b` and fails at the first byte. The transform is wiring and four inverters. It adds no
state and no latency, and the line stays fully used. The key is `echoKey` in `rtl/UartEcho.bsv` and
`ECHO_KEY` in the host script.

```bash
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader examples/bluespec/uart_echo/uart_echo.yaml --target stlv7325 \
    --design-updates rtl.defines.ECHO_TRANSFORM=1
python3 examples/bluespec/uart_echo/host/uart_echo_test.py --port /dev/cu.usbserial-<serial> --baud 3000000 --transform
```

The report has an `echo:` line with the mode that ran. A mismatch of mode fails with a note: `the
bytes came back unchanged: a wire loopback, or a design built without ECHO_TRANSFORM`, or `the bytes
came back transformed ... run with --transform`. The `stlv7325` target builds a plain
echo, because a terminal wants plain bytes.

## Simulation

The testbench is a behavioral host at the pins of the echo. A receiver model decodes `txd` frame
by frame and compares every byte. It checks:

* the power-up: the reset ends, the LEDs are off, `txd` is idle;
* corner and random bytes back to back at 12 Mbaud;
* full use of the transmitter: with the host 0.5 % faster than the echo, at least 98 of 100 echoed
  frames start in the clock where the previous one ended;
* the activity LEDs;
* a framing error and a break: nothing is echoed, the error LED stays on until the button clears it;
* an overrun with the host 3.5 % faster than the echo: the bytes that come back are in order and
  every missing byte is counted;
* a baud rate switched in the middle of a burst (the burst comes back whole), then at 3 Mbaud and
  921600 baud when the echo is idle. With `FIXED_BAUD=1` the same flips change nothing.

One simulation runs all four combinations of `FIXED_BAUD` and `ECHO_TRANSFORM`, each with its own
host and receiver model. The testbench shrinks `ECHO_DEPTH` to 3 and `ACTIVITY_US` to 20 so that an
overrun and the end of an LED stretch come within a few thousand clocks. The target `switches`
and `echo_transform` choose which combination uses the real top `mkUartEcho`.

```bash
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml                              # Bluesim, about 45 s
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml -s bsc_sim.simulator=verilator       # or iverilog
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml --target switches
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml --design-updates tb.defines.XEDA_INJECT_BUG=true   # must fail
```

`XEDA_INJECT_BUG` plants a bug (the echo takes a new baud rate at once, even in the middle of a
frame); `XEDA_INJECT_BUG_ECHO` plants only that one, and `XEDA_INJECT_BUG_PLAIN` sends every byte
back unchanged, which the transformed benches catch at the first byte.

## Measured results

Echo of random data, pipelined mode, with the default window of each bridge. Throughput is per
direction, as a share of the line rate (baud / 10 bytes per second). A run passes when every byte
comes back in order with no extra byte. With `--ftdi`, it also fails if the chip reports an overrun.

| board | baud | host | result |
|---|---|---|---|
| Arty A7-100T | 12 M | pyftdi | 16 MiB echoed without error at 99.7 to 100 % |
| Arty A7-100T | 12 M | pyftdi | 512 MiB without error (bit error rate below 7e-10 at 95 % confidence) |
| Arty A7-100T | 115200 to 6 M | pyserial | 4 MiB without error at 99.5 to 100 % |
| ULX3S | 3 M | pyserial | 4 MiB without error at 99.5 %; with pyftdi, 56 to 74 % |
| STLV7325 v2 | 3 M | pyserial | 16 MiB at about 45 %; a run can still fail, see [Limits](#limits) |

On the Arty and the ULX3S, the echo recovers from wrong baud rates, breaks and framing errors: the
next run at the right rate comes back exactly. After programming, the line stays silent: no stray
byte, and no reset pin.

Area and timing (`arty`, `arty_nextpnr` and `ulx3s` are built with the switches, `stlv7325` with a
fixed rate):

| target | tools | LUT | FF | block RAM | timing |
|---|---|---|---|---|---|
| `arty` | Vivado 2024.2 | 168 | 187 | 1 RAMB18 | 100 MHz met, WNS +3.26 ns |
| `arty_nextpnr` | openXC7 | 275 | 206 | 1 RAMB18 | 100 MHz met, Fmax 176 MHz |
| `ulx3s` | Yosys, nextpnr-ecp5 | 352 | 225 | 1 EBR | 25 MHz met, Fmax 110 MHz |
| `stlv7325` | openXC7 | 197 | 151 | 1 RAMB18 | 200 MHz met, Fmax 242 MHz |

## Limits

* The CH9102 has a receive FIFO of about 320 bytes and no flow control. Keep at most 256 bytes in
  flight (the default window). If the host does not read for longer than the FIFO takes to fill,
  the bridge loses or repeats 64-byte USB packets. The STLV7325 target runs at 3 Mbaud. In testing
  on macOS, the CH9102 worked at 3 Mbaud but not at 921600 baud; other hosts and drivers may differ.
* The FT231X of the ULX3S generates at most 3 Mbaud. The FT2232H generates 12, 8, 6, 4, 3, 2, 1.5
  and 1 Mbaud and the usual slower rates exactly.
* pyserial cannot see that a driver dropped bytes. A USB bridge can lose data, so a link that must
  not lose any needs a layer with a CRC and retransmission.
* In a four-state simulator with `BSV_NO_INITIAL_BLOCKS`, the 3-bit counter of the power-on reset
  has no start value and the reset never ends. Give it a known value (an `initial` block or the
  tool's INIT option). FPGAs and the other simulators need nothing.
