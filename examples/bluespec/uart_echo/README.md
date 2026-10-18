# uart_echo: a UART echo demo for two boards

Everything the host sends comes back. It is the board demo of the [`uart`](../uart/README.md) PHY:
a 12 Mbaud echo on the Arty A7-100T, a 3 Mbaud echo on the ULX3S, built from the same board-agnostic
top `mkUartEcho`.

```
rxd -> UartCore (receiver) -> echo FIFO (block RAM) -> UartCore (transmitter) -> txd
sw[3:0] -> baud rate table ---^                       btn -> clear flags      led[3:0] <- activity, flags
```

| file | content |
|---|---|
| `rtl/UartEcho.bsv` | `mkUartEcho`, the top: UART core with a run-time baud rate, echo FIFO, switches, button, LEDs, power-on reset |
| `tb/TbUartEcho.bsv` | the testbench: a behavioural host at the pins and a receiver model (the line sender of `../uart/tb/TbUartModels.bsv` is reused) |
| `boards/arty_a7_100t.xdc`, `boards/arty_a7_100t_sync.xdc` | Arty A7-100T: pins (also for openXC7), and the synchronizer and timing constraints of Vivado |
| `boards/ulx3s.lpf` | ULX3S pins |
| `host/uart_echo_test.py` | the computer's side: sends random bytes, reads the echo, compares, reports throughput |
| `uart_echo.yaml` | the design file: simulation, Vivado out of context, the board targets, and `fixed_baud` (the `FIXED_BAUD` testbench) |

The design file lists the PHY sources of `../uart/rtl` by relative path, which Xeda handles like any
other source (bsc searches the directories of all its sources).

## The design

`mkUartEcho` has a clock `CLK` and no reset pin. Its ports:

| port | direction | meaning |
|---|---|---|
| `rxd`, `txd` | in, out | the UART, 8N1 |
| `sw[3:0]` | in | the baud rate (see below) |
| `btn` | in | clears the sticky LEDs |
| `led[3:0]` | out | `led[0]` frame received, `led[1]` byte sent (both stretched), `led[2]` sticky: framing error, parity error or break, `led[3]` sticky: overrun |

The activity LEDs stay on for about 60 to 100 ms after the last event (`ACTIVITY_US`), so a human can
see them even at 12 Mbaud. The sticky LEDs stay on until the button is pressed. An overrun means a
byte arrived while the PHY's receive buffer was full: the new byte is dropped, the ones in the buffer
stay in order. A full echo FIFO (a host sending faster than the echo can send back) is what keeps
that buffer from emptying and so what leads to an overrun.

**Baud rate.** The switches are an index into a table made at elaboration from `CLK_HZ`: index 0 is the
default `BAUD`, then the rates that a USB UART bridge can generate (12 M, 8 M, 6 M, 4 M, 3 M, 2 M, 1.5 M,
1 M, 921600, ... down to `MIN_BAUD`) that have at least 8 clocks per bit. An index past the end of the
table selects the last (slowest) rate. The period of the PHY has 6 fraction bits, so each baud rate
is within 0.07 % of the nominal one. Switch 0 is `sw[0]`, the lowest bit.

| `sw[3:0]` | Arty A7-100T, 100 MHz | ULX3S, 25 MHz |
|---|---|---|
| 0000 | 12 M (default) | 3 M (default) |
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

The FT2232H of the Arty can generate 12, 8, 6, 4, 3, 2, 1.5 and 1 Mbaud and the usual slower rates
exactly; the FT231X of the ULX3S is limited to 3 Mbaud.

A new setting takes effect only when the echo is idle: the transmitter has nothing left to send, the
receive line has been high for 11 bit times of the slowest rate (9550 clocks, 95 us at 100 MHz: 11 x
100e6 / 115200 rounded up, plus one) and is high now, so no frame is in progress. Changing a switch
in the middle of traffic cannot cut a frame; the new rate starts after the traffic has stopped. The
host must start at the new rate after that. The switches and the button go through two flops each.

**Reset.** The board needs no reset pin: `mkUartEcho` holds the reset of everything else for a few
clocks after power-up, from a 3-bit counter `held` that counts up to 7 (the library's `MakeReset0` is
the reset source; it registers the request, so the reset covers the clock after each count). The
counter has no reset itself, so its start value depends on where it runs:

| where | `held` starts at | reset lasts |
|---|---|---|
| FPGA (Vivado, openXC7, Yosys ECP5) | 0: every flip-flop is 0 after configuration, and bsc's initial blocks are between `synopsys translate_off` lines, so synthesis does not see them | 7 clocks |
| Bluesim, Verilator, Icarus (initial blocks) | 3'h2: bsc initializes every register to the pattern 'hAAAA, cut to its width | 5 clocks |
| four-state simulator with `BSV_NO_INITIAL_BLOCKS` | X, and X + 1 is X: `held != 7` never becomes false | no reset, ever |

(Counted in a simulation of the generated Verilog: the reset is asserted in 5 or 7 consecutive
clock edges, for either reset polarity; with the INIT value 0 forced onto `held` it is the
FPGA's 7. An active-low reset on an FPGA is one clock longer, since `MakeReset0`'s flip-flop is 0
at configuration, which is in reset for that polarity.) The last case is a limit of the mechanism: a
simulation that removes the initial blocks needs `held` forced to a known value (a Verilog
`initial` or the tool's INIT option); every other flow works. Any start value works, only
the length of the reset changes; a counter without an initial value cannot do better than this, and
a reset pin is the alternative for a board that has one.

`txd` is held high until the reset is over, because the flip-flop that drives it is 0 at
configuration and would show a start bit for a clock or two. The obvious `mkInitialReset` of the bsc
library does not work in hardware: its body is between `synopsys translate_off` lines, so the
synthesized design has no reset at all. The mechanism runs the same 107070 cycles in Bluesim,
Verilator and Icarus. For the hardware, the Yosys netlist of the design (`synth_xilinx`, every
flip-flop at 0) echoes two bytes in a simulation, with either reset polarity (Xeda's bsc flow makes
resets active high); Vivado and the other flows build it, and the boards power up with no stray
byte (see the hardware results). The sweep's Verilator lint (`tests/test_bsc_examples.py`) needs
`-Wno-STMTDLY` for the `#0` that `MakeReset0.v` has, and passes it only for a design that contains a
bsc reset generator.

**Echo FIFO.** `ECHO_DEPTH` bytes of block RAM (the library's `mkSizedBRAMFIFOF`): 2047 by default,
which is one 18 Kbit block RAM at 8 bits (the library FIFO holds 2**n - 1 bytes in 2**n entries). Why
so large: a host needs the line to stay busy during the USB round trip (1 to 2 ms with the FTDI
latency timer at 1 ms, which is 1.2 to 2.4 KB at 12 Mbaud), and the host sends only while fewer than
`window` bytes are unechoed; the FIFO must hold a whole window. The PHY's receive and transmit
buffers are one byte each, since the FIFO behind them does the buffering.

**Macros** (set them with `rtl.defines` in the design file, a target, or `-s`; all have defaults):

| macro | meaning | default |
|---|---|---|
| `CLK_HZ` | clock frequency in Hz | 100000000 |
| `BAUD` | the baud rate for switches 0000 | 12000000 |
| `MIN_BAUD` | the slowest rate in the table; sizes the period counters | 115200 |
| `ECHO_DEPTH` | the echo FIFO in bytes | 2047 |
| `ACTIVITY_US` | the longest time an activity LED stays on, in microseconds | 100000 |
| `HOLD` | the receiver's glitch filter depth in clocks; at most (D+2)/3 for D clocks in the shortest bit, which is that of the fastest rate in the table (3 at 12 Mbaud / 100 MHz); a larger value is an elaboration error | 3 |
| `FIXED_BAUD` | defined (any value): one baud rate, `BAUD`, and the switches are ignored; see below | undefined |

**Fixed baud rate (`FIXED_BAUD`).** When the macro is defined the design has the one rate `BAUD`:
the PHY gets the static timing of `BAUD` (an exact fraction, constants in the timers, counters of
just the bits they need) instead of the run-time period, and the table, the switch synchronizers
and the idle detector that waits to retune are not built. The ports are the same (`sw` is an
unused input), so the board files do not change. It is the choice when the baud rate is not a
selection, or the switches are not free: the DIP switches of the ULX3S are not all off, and the
default design then picks the rate they say (115200 on the board used for the measurements below).
The testbench covers it too (`--target fixed_baud`: the same checks with the switches flipped to
prove they are ignored). Area, same design and tools, 12 Mbaud at 100 MHz (the tool versions and
commands are under "Results" below):

| | LUT | FF | slices / timing |
|---|---|---|---|
| Vivado 2024.2, xc7a100t, switches (default) | 168 | 187 | 76 slices, WNS +3.26 ns |
| Vivado 2024.2, `FIXED_BAUD` | **96** | **139** | 54 slices, WNS +3.52 ns |
| Yosys 0.70+post `synth_xilinx -family xc7 -flatten`, default / `FIXED_BAUD` | 193 / 115 | 206 / 142 | |
| Yosys 0.69+286 and nextpnr-ecp5 0.11.1 (ULX3S, 25 MHz, 3 Mbaud), default / `FIXED_BAUD` | 352 / 231 | 225 / 169 | |

So `FIXED_BAUD` takes about 40 % of the logic away (72 LUT and 48 FF in Vivado): the run-time
period (counters one bit wider, the period mux and its table, an adder for the first sample) and
the retune logic (a 14-bit idle counter, two more synchronizers).

## Simulation

`tb/TbUartEcho.bsv` is a behavioural host at the pins of the real `mkUartEcho` (the design file sets
`ECHO_DEPTH` to 3 and `ACTIVITY_US` to 20 for the simulation only, so that an overrun and the end of
an LED stretch are reached in a few thousand clocks). The host drives `rxd`, the switches and the
button; a receiver model decodes `txd` frame by frame from its own start edge and compares every byte
with what was sent. It checks:

1. the power-up: the internal reset ends, the LEDs are off, `txd` is idle;
2. corner and random bytes, back to back, at 12 Mbaud;
3. the full utilization of the transmitter: with the host 0.5 % faster than the echo, the FIFO fills
   and at least 98 of 100 echoed frames start in the clock where the previous one ended;
4. the activity LEDs, on during the echo and off after the stretch;
5. a framing error and a break: nothing is echoed, the error LED is on and stays on, the button
   clears it, bytes after the error come back;
6. an overrun, with the host 3.5 % faster than the echo: the overrun LED is on, the bytes that came back
   are in order, every missing byte is counted, the button clears the LED;
7. the baud rate: a switch is changed in the middle of a burst (it comes back whole at the old rate),
   then the new rate takes effect when the echo is idle: 3 Mbaud, then 921600 baud.

All three simulators run 107070 cycles with the same result. `--target fixed_baud` runs the same
script on the `FIXED_BAUD` build (70761 cycles on all three; test 7 flips the switches and checks
that the echo does not care, at the one rate; `XEDA_INJECT_BUG` is caught there too, by the receiver bug).

```bash
xeda run bsc_sim uart_echo.yaml                       # Bluesim, 11 s with the elaboration
xeda run bsc_sim uart_echo.yaml -s simulator=verilator
xeda run bsc_sim uart_echo.yaml -s simulator=iverilog
xeda run bsc_sim uart_echo.yaml --design-overrides tb.defines.XEDA_INJECT_BUG=true   # must fail
xeda run bsc_sim uart_echo.yaml --target fixed_baud           # the FIXED_BAUD build
```

`XEDA_INJECT_BUG` makes the echo take a new baud rate at once instead of when it is idle (and plants
the receiver bug of the `uart` example too, since both packages read the macro). The burst in test
7 catches it. `XEDA_INJECT_BUG_ECHO` plants only the echo's bug.

## Building for a board

Commands run from this directory. Use `--run-root` for the run directory somewhere outside the
checkout, and an absolute path for the design file when you run Xeda with `uv run --directory`.

**Arty A7-100T with Vivado** (about 3 minutes; 100 MHz, 10 ns):

```bash
xeda run vivado_synth uart_echo.yaml --target arty --run-root /path/to/runs
```

The bitstream is `uart_echo.bit` in the run directory (`.../uart_echo/arty/vivado_synth/`); add
`-s bitstream=/path/to/uart_echo_arty.bit` to copy it somewhere. The XDC files give the pins, put
`ASYNC_REG` on the synchronizer flops (the receiver's, the idle detector's, the switches' and the
button's) and set a false path from `rxd`, `sw[*]` and `btn`. The clock is not in the XDC: `clock.period`
of the flow writes `create_clock`, and a clock with two constraints is an error. XDC is not full Tcl
(`if` is refused), so the `ASYNC_REG` line has no check: when no cell matches, Vivado warns. In the
Vivado log, the lines `Applied set_property ASYNC_REG = true for echo_lineSync1_reg` say that it landed.
(Vivado merges the receiver's `rxSync1/2` with `lineSync1/2`, two copies of the same synchronizer
on the same pin, and keeps one pair, which carries the property.)

**Arty A7-100T with the open-source openXC7 tools** (7 to 10 s; needs `/opt/openxc7/bin` first on
`PATH`; nextpnr of openXC7 reads only pin lines, which is why the pins have a file of their own):

```bash
PATH=/opt/openxc7/bin:$PATH xeda run yosys_fpga+nextpnr+fpga_pack uart_echo.yaml --target arty_nextpnr
```

**ULX3S 85F** (about 5 s; needs the oss-cad-suite `bin` directory first on `PATH`, 25 MHz, 3 Mbaud
default):

```bash
PATH=/path/to/oss-cad-suite/bin:$PATH xeda run yosys_fpga+nextpnr+fpga_pack uart_echo.yaml --target ulx3s
```

The LPF has no `FREQUENCY` line, because `nextpnr: clock.freq: 25 MHz` of the target gives the period.
The ULX3S button is FIRE1 (`R1`, active high); the DIP switches SW1 to SW4 are `sw[0]` to `sw[3]` (ON is
high).

**Programming** is the same chain with `openfpgaloader` at the end. It loads the SRAM of the FPGA
through the board's USB cable (the design is gone at power-off; there is no `write_flash`), so it
runs only when you ask for it. The targets carry the board name for it:

```bash
xeda run vivado_synth+openfpgaloader uart_echo.yaml --target arty
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader uart_echo.yaml --target arty_nextpnr   # 10 s
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader uart_echo.yaml --target ulx3s
openFPGALoader -b arty_a7_100t path/to/uart_echo.bit        # the same, from a bitstream
openFPGALoader -b ulx3s path/to/uart_echo.bit
```

**A fixed baud rate** is a macro, so it is a design override on any target (`FIXED_BAUD`
alone keeps the target's `BAUD`):

```bash
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader uart_echo.yaml --target arty_nextpnr \
    --design-overrides rtl.defines.FIXED_BAUD=true rtl.defines.BAUD=6000000
xeda run bsc_sim uart_echo.yaml --target fixed_baud       # the testbench of that build
```

**Results** (Xeda reports, the design as in the design file; `FIXED_BAUD` is in its section above):

| target and tools | LUT | FF | block RAM | timing |
|---|---|---|---|---|
| `arty`: Vivado 2024.2 (`xeda run vivado_synth`), xc7a100t | 168 | 187 | 1 RAMB18 | 100 MHz met, WNS +3.26 ns (Fmax 148 MHz), 76 slices |
| `arty_nextpnr`: Yosys 0.69, nextpnr-himbaechel 1.0.0 (openXC7) | 275 (222 in Yosys) | 206 | 1 RAMB18 | 100 MHz met, Fmax 176 MHz |
| `ulx3s`: Yosys 0.69+286, nextpnr-ecp5 0.11.1, LFE5U-85F | 352 (LUT4 with carry and mux) | 225 | 1 EBR | 25 MHz met, Fmax 110 MHz |
| Yosys 0.70+post, `synth_xilinx -family xc7 -flatten` | 193 | 206 | 1 RAMB18 | |

The first three rows are the chains of the commands above, from the Verilog of Xeda's `bsc` flow.
The last row, and the Yosys figures of the `FIXED_BAUD` table, are (in `uart_echo/`; `$BSV_VERILOG` is
bsc's Verilog library, `<bsc>/lib/Verilog`, or `<bsc>/libexec/lib/Verilog` with Homebrew):

```bash
mkdir -p build
bsc -u -verilog -p +:../uart/rtl:rtl -bdir build -vdir build rtl/UartEcho.bsv   # plus -D FIXED_BAUD
yosys -p "read_verilog -sv -DBSV_NO_INITIAL_BLOCKS build/mkUartEcho.v build/mkUart.v \
             $BSV_VERILOG/BRAM2.v $BSV_VERILOG/MakeReset0.v;
             hierarchy -top mkUartEcho; synth_xilinx -family xc7 -flatten -top mkUartEcho;
             stat" | tail -n 40
```

The Verilog that Xeda's `bsc` flow writes, with its own optimization flags, comes out at 193 / 116
LUT in the same Yosys command: the figure depends on the bsc flags by a LUT or two.

Almost half is the PHY with a run-time baud rate; the idle detector (14 bits), the LED prescaler (20
bits) and the 12-bit FIFO pointers are most of the rest.

## The host test

`host/uart_echo_test.py` (Python 3.11; `pyserial`, or `pyftdi` for libusb access with a 1 ms
latency timer) sends a pseudo-random payload and checks the echo as it arrives. Its docstring has
the options, the window limits, the notes (DTR and RTS, macOS, pyftdi) and the exit status. The
**pipelined** mode (default) writes from a thread while the main thread reads, with at most
`--window` bytes in flight, so both directions of the line are busy at once: this measures the
full-duplex line utilization. The **chunked** mode (`--mode chunked`) writes a chunk, reads it back,
and only then writes the next, so the line idles for a round trip after every chunk (79 % of the
line rate at 12 Mbaud with 4000-byte chunks).

The pipelined mode reaches 99.6 to 100 % of the line rate (an earlier version of the tool got 40 %)
because the reader does nothing but read and compare (no sleeps, no per-byte Python: each chunk is
compared with the payload in one memory compare, and the payload is made on the fly), the writer
uses 512-byte writes and sleeps 0.1 ms only when the window is full, and pyftdi reads one USB packet
at a time (512 bytes on the FT2232H; the notes say why not its 4 KiB default). `--ftdi` also counts
the packets in which the chip flags an overrun (`chip errors` in the report) and fails the run if
there is one, so a host that did not read in time can be told apart from an FPGA problem. With
`--ftdi` a `--window` or `--chunk` above the chip's receive buffer minus 64 bytes is refused.

```bash
python3 host/uart_echo_test.py --loopback-selftest        # no hardware: both modes, faults, overflow
python3 host/uart_echo_test.py --ftdi ftdi://ftdi:2232h:210319B9B557/2 --baud 12000000 --bytes 16777216
python3 host/uart_echo_test.py --port /dev/cu.usbserial-210319B9B5571 --baud 3000000
python3 host/uart_echo_test.py --ftdi ftdi://ftdi:ft-x:K00121/1 --baud 3000000       # ULX3S
python3 host/uart_echo_test.py --ftdi ... --baud 12000000 --bytes 536870912 --progress   # soak
```

Exit status 0 only for a perfect echo; a run that stops echoing reports the offset of the first
missing byte. On success it prints an upper bound of the bit error rate (95 % confidence, three
events over the bits tested, with zero errors seen).

## Measured on the boards

Boards: Arty A7-100T (FT2232H, USB high speed, channel B), ULX3S 85F (FT231X, USB full speed),
one Mac (macOS, Apple's DriverKit FTDI driver, pyserial 3.5, pyftdi 0.57, libusb 1.0.30), the host
not otherwise idle. All runs are the pipelined mode with the defaults, 100 % PASS means every
byte back in order, nothing extra, and no packet flagged by the chip. Throughput is per direction,
as a percentage of the line rate (baud / 10 bytes per second). The bitstreams are `FIXED_BAUD`
builds from openXC7 (Arty, 10 s each) and the open-source ECP5 flow (ULX3S, 5 s), except the
Vivado rows.

| board, build | baud | pyftdi: bytes, throughput | pyserial: bytes, throughput |
|---|---|---|---|
| Arty, Vivado, switches 0 | 12 M | 2 x 16 MiB: 100.0 %, 99.7 %; 512 MiB soak: 99.9 %; chunked 4000 B: 79.3 % | 16 MiB: 99.9 % |
| Arty, Vivado, `FIXED_BAUD` | 12 M | 16 MiB: 99.7 % | 4 MiB: 99.5 % |
| Arty, openXC7, `FIXED_BAUD` | 12 M | 16 MiB: 100.0 % | 3 x 4 MiB: 99.5 to 99.6 % |
| | 8 M | 16 MiB: 99.9 % | 3 x 4 MiB: 99.7 % |
| | 6 M | 16 MiB: 100.0 % | 4 MiB: 99.8 % |
| | 4 M | 8 MiB: 99.8 % | 4 MiB: 99.8 % |
| | 3 M | 8 MiB: 99.9 % | 4 MiB: 99.9 % |
| | 2 M | 4 MiB: 99.9 % | 4 MiB: 99.9 % |
| | 1 M | 4 MiB: 100.0 % | 4 MiB: 99.9 % |
| | 921600 | 4 MiB: 100.0 % | 4 MiB: 100.0 % |
| | 115200 | 1 MiB: 100.0 % | 1 MiB: 100.0 % |
| ULX3S, `FIXED_BAUD` | 3 M | 4 MiB, window 448: 56 to 74 % | 2 x 4 MiB: 99.5 %, 99.7 % |
| ULX3S, `FIXED_BAUD` | 115200 | 1 MiB: 97.2 % | 2 x 1 MiB: 100.0 % |

Every run in the table passed with 0 mismatched, 0 missing and 0 extra bytes. The 512 MiB soak
(`536870912` bytes, 447.8 s, 1,198,776 B/s, 0 chip errors, in flight at most 1536 bytes) is
4.3e9 data bits with no error: with 95 % confidence the bit error rate of the echo path (USB, FTDI
chip, cable, the FPGA's receiver and transmitter, at 12 Mbaud and 100 MHz) is below 7e-10. The
Vivado bitstream (`arty`) is the one in the soak; `FIXED_BAUD` and the open-source builds ran the
shorter runs of the table.

* **pyserial on macOS.** The first versions of the tool lost and corrupted bytes with pyserial at
  every rate from 1 Mbaud up, within the first kilobytes. The cause was the tool, not the rate:
  assigning `ser.timeout` on an open port makes pyserial rewrite the termios settings, and Apple's
  FTDI driver turns that into USB control transfers in the middle of the traffic (see the notes in
  the script). The timeout is now set once. Then pyserial echoed 4 MiB at every rate up to 12 Mbaud
  (tens of runs at 8 and 12 Mbaud, also with 20 busy loops competing for the CPU: 89 to 95 % of the
  line rate). Two early runs at 8 and 12 Mbaud still lost bytes (a 330-byte gap, and a stall after
  2 MiB); about 40 later runs did not reproduce them, so the cause is unknown. pyserial cannot tell
  that the driver dropped bytes, and it cannot set the latency timer (16 ms by default: with a
  window of 512 bytes it is down to 4 % at 8 Mbaud). So **on macOS use `--ftdi` from 8 Mbaud up**;
  pyserial passed everything up to 6 Mbaud.
* **Host load.** With 20 busy loops on 10 cores, pyftdi at 12 Mbaud still passes every byte but
  drops to 40 to 42 % (the window is small and the reader is late). The chip's 4 KiB buffer is
  never overrun because the window is below it. The FT231X of the ULX3S has a 512-byte buffer: a
  window above 512 (`--window 1792`) loses bytes on a loaded host (`chip errors`: 2 to 11 packets
  in the failing runs; one such run is in the measurements), so the tool's default there is 448,
  and with `--ftdi` it refuses a larger window.
  That costs speed (full-speed USB, a longer round trip): 56 to 74 % of the 300 kB/s, depending on
  the host load. pyserial on the same board did 99.5 % without loss, but has no protection against
  the same overrun.
* **Recovery from errors** (Arty with Vivado, both builds, and ULX3S at 3 Mbaud, pyftdi): the
  host sent 20 kB of random bytes and 20 kB of zeros at wrong baud rates (Arty: 6 M, 3 M, 1 M,
  115200, 57600; ULX3S: 1 M, 115200, 57600; 10 to 12 kB came back as garbage when the wrong rate
  was 6 M or 3 M on the Arty and 1 M on the ULX3S), then 5000 bytes of 0xFF, a 50 ms break, 1000
  bytes, a 200 ms break, and a 0x00/0x55 pattern at half the baud rate: framing errors, breaks
  and garbage frames at the receiver. After each, and a pause of 0.2 s, the next 1 MiB at the
  right rate came back exactly: 15 of 15 on the Arty (Vivado default and `FIXED_BAUD`, each) and
  11 of 11 on the ULX3S. A framing error does not wedge the echo.
* **Power-up without a reset.** After `openFPGALoader` (SRAM load) the port was opened at once,
  4 to 40 ms after the loader returned. The line was silent for 0.3 s (no byte from a glitch of
  `txd` or the receiver), and 256 KiB echoed exactly: 4 times for the Arty Vivado default, 3 times
  for each of the Vivado `FIXED_BAUD` and the two openXC7 builds, 4 times for the ULX3S at 3 Mbaud
  and twice at 115200. No reset button, no reset pin.
* **The switches on the ULX3S.** Its DIP switches are not all off. The default design (not
  `FIXED_BAUD`) echoed at 115200 and not at 3 Mbaud, so the run-time period of the PHY and the
  table work at 115200 on that board. On the Arty, the default design with switches 0 is the
  12 Mbaud rows above.

Reproduce one row (Arty, 6 Mbaud, from this directory; the paths are those of my machine):

```bash
export PATH=/opt/openxc7/bin:$PATH
xeda run yosys_fpga+nextpnr+fpga_pack+openfpgaloader $PWD/uart_echo.yaml --target arty_nextpnr \
    --run-root /path/to/runs --design-overrides rtl.defines.FIXED_BAUD=true rtl.defines.BAUD=6000000
DYLD_LIBRARY_PATH=/opt/homebrew/lib python3 host/uart_echo_test.py \
    --ftdi ftdi://ftdi:2232h:210319B9B557/2 --baud 6000000 --bytes 16777216
python3 host/uart_echo_test.py --port /dev/cu.usbserial-210319B9B5571 --baud 6000000 --bytes 4194304
```

(`DYLD_LIBRARY_PATH` makes pyusb find Homebrew's libusb on macOS. The device names are those of
my boards: `pyftdi-ftdi-urls` lists yours. pyftdi reads an all-digit serial number as an index.)
