# uart: a UART PHY in Bluespec

A transmitter and a receiver with byte buffers, sticky error flags and one-cycle event pulses. It
is small (about 50 LUT and 60 FF with buffers on a 7-series FPGA, see [Area](#area)), runs at
12 Mbaud with a 100 MHz clock (8.33 clocks per bit) and has an exact average baud rate. The
architecture has no oversampling: one restartable fractional bit timer per direction, a hold
filter in front of a level-sensitive start detect, and one sample at the centre of every bit.

```
tx (Put) -> transmit buffer -> UartTx -> txd
rxd -> 2-FF synchronizer -> hold filter -> UartRx -> receive buffer -> rx (Get)
```

The interface is the user's: `tx` takes bytes to send, `rx` gives the bytes received, so a link
layer connects to it with `mkConnection`.

| file | content |
|---|---|
| `rtl/Uart.bsv` | `Uart#(nData)` interface, `mkUartCore` (polymorphic), `mkUart` (the synthesizable top, sized by macros) |
| `rtl/UartTx.bsv`, `rtl/UartRx.bsv` | the transmitter and the receiver |
| `rtl/BitTimer.bsv` | the fractional bit timer, `staticTiming` and `runtimeTiming` |
| `rtl/UartTypes.bsv` | frame format, receive events, flags, pins |
| `rtl/RingFifo.bsv` | `mkSmallFIFOF` (1, 2 or a power of two entries) and the LUTRAM ring behind it; generic |
| `tb/TbUartModels.bsv`, `tb/TbUart.bsv` | the testbench: models of a remote UART and of a line checker, and the tests |
| `uart.yaml` | the design file |

## Interface

```bsv
interface Uart#(numeric type nData);
   interface Put#(Bit#(nData)) tx;      // bytes to send
   interface Get#(Bit#(nData)) rx;      // bytes received
   method UartFlags flags;              // sticky: frameError, parityError, brk, overrun
   method Action clearFlags;
   method UartEvents events;            // one-cycle pulses, see below
   method Bool txBusy;                  // a frame is on the line or queued
   interface UartPins pins;             // rxd (input, no handshake), txd (output)
endinterface
```

* The receiver never stalls. A good byte that arrives while the receive buffer is full is
  dropped (the buffered bytes stay, in order) and raises the overrun flag and event. Full is
  judged at the start of the cycle: a buffer of 2 or more entries that is full does not take the
  byte in the cycle in which it is read either, only the one-entry buffer does (`RingFifo.bsv`
  says why).
* Frames with a framing error, a parity error or a break are reported and dropped, never
  delivered. Each received frame raises exactly one of the events `rxByte`, `frameError`,
  `parityError`, `brk`, `overrun`, in the cycle after its stop bit was sampled (`txByte` is
  separate: the transmitter took a byte). So a counter per event never counts twice.
* A break is one `brk` event, however long the line stays low, and no byte. After a framing error
  the receiver waits for the line to be high before it hunts for a start bit again, and so does
  it after reset: a line that is low at power-up (or a floating pin) creates no frames.
* The transmitter starts the next frame in the clock in which the stop bit ends if a byte is
  waiting: no gap, so 8N1 frames are 10 bit times long on average, exactly.

The pins are plain Verilog ports: `rxd` and `txd`, no ready or enable signals, and `rxd` is
asynchronous to `CLK`.

## Using it

`mkUart` is a ready-made module. Its size, baud rate and format come from preprocessor macros,
which Xeda sets with `rtl.defines` (see `uart.yaml`); all have defaults:

| macro | meaning | default |
|---|---|---|
| `CLK_HZ`, `BAUD` | clock and baud rate in Hz; the bit period is their ratio | 100000000, 12000000 |
| `DATA_BITS` | 5 to 9 | 8 |
| `PARITY` | 0 none, 1 even, 2 odd | 0 |
| `STOP_BITS` | stop bits sent, 1 or 2 (the receiver samples the first one) | 1 |
| `TX_DEPTH`, `RX_DEPTH` | buffer depth: 1, 2 or a power of two from 4 | 1, 4 |
| `HOLD` | receiver glitch filter in clocks, 1 is none; at most (D+2)/3 for D clocks in the shortest bit (3 at 12 Mbaud / 100 MHz), see [Glitches](#timing-and-tolerance) | 3 |
| `ACC_BITS` | most bits of the baud fraction accumulator, at most 8 | 6 |

A value outside what a macro allows stops the build with a message that says so: `PARITY` other
than 0, 1 or 2, `HOLD` above the limit for the bit period (or 0), `ACC_BITS` above 8 where the
period needs that many, a buffer depth that is not 1, 2 or a power of two, a period of fewer than 4
clocks per bit. `DATA_BITS` outside 5 to 9 and `STOP_BITS` outside 1 to 2 are type errors at
`uartFormat` (below), which bsc words as an unsatisfiable `Add` proviso.

`mkUartCore` is the same thing as a polymorphic module for a design that wants another format
or a baud rate that changes at run time:

```bsv
UartFormat#(7, 1, 2) fmt = uartFormat(EvenParity);        // 7E2: data, parity bits, stop bits
Depth#(2) txDepth = depth;
Depth#(8) rxDepth = depth;

// a bit period fixed at elaboration ...
BitTiming#(StaticCntBits#(100000000, 8000000, 1), 1) fixed =
   staticTiming(100000000, 8000000, defaultAdjust(100000000, 8000000));
// ... or in a register, to change it while the design runs (modulus 2**6)
Reg#(BitPeriod#(10, 6)) period <- mkReg(bitPeriod(100000000, 12000000));
let variable = runtimeTiming(period, 0, shortestBit(100000000, 12000000, 6));

Uart#(7) uart <- mkUartCore(variable, fmt, 3, txDepth, rxDepth);   // 3: hold filter depth
```

`uartFormat` is the only way to make a `UartFormat` (its constructor is not exported), and it
accepts 5 to 9 data bits, 0 or 1 parity bits and 1 or 2 stop bits only, so no module can be
given a format outside that. `mkUartCore` checks the hold filter against the period: with
`staticTiming` the elaboration stops if the depth is more than `maxHold` allows for the shortest
bit. `runtimeTiming` cannot know the shortest bit the period will take, so it is the third
argument, `shortestBit(clockHz, baud, f)` of the fastest rate: a precondition that the caller
keeps (the period register must never hold a shorter bit), checked against `hold` like the static
one. `mkUartRx`, which takes a `BitTimer` and so cannot see the period, has the same precondition.

Both timings feed the same timer, so a constant period costs what a static one does. How the
widths are chosen:

* `staticTiming(clockHz, baud, adjust)` reduces `clockHz/baud` to A/B. If B fits the
  accumulator (B <= 2**aw) the bit period is exact: bit widths D = floor(A/B) and D+1 clocks, with
  the carry of a modulus-B accumulator choosing which, so 12 Mbaud at 100 MHz is 8,8,9,8,8,9...
  clocks. Otherwise the period is rounded to aw fraction bits. A modulus that is not a power of two
  costs a compare and a conditional subtract by that constant, where a power of two needs only the
  adder's carry. The counter needs only as many bits as D-1: size it with
  `StaticCntBits#(clockHz, baud, aw)` from numeric macros, as `mkUart` does. A width that is too
  small is an elaboration error, not a silent truncation.
* `mkUart` also picks the accumulator width `aw` = ceil(log2 B) for the exact period, up to
  `ACC_BITS`. That is a choice of instance at elaboration, since a numeric type cannot be computed
  from a gcd. It saves 7 LUT and 4 FF in the receiver and 5 LUT and 4 FF in the transmitter (12
  Mbaud, 100 MHz: a 2 bit accumulator instead of 6).
* `runtimeTiming` takes a `BitPeriod`, D-1 and a fraction of f bits, from a register or a table
  built with `bitPeriod(clockHz, baud)`, and the clocks of the shortest bit it will hold (the
  hold filter limit above). Its rounding error is at most 2**-(f+1)/P: 0.09 % at 12 Mbaud and
  f = 6, so the baud rate is correct within the 0.16 % that an FTDI chip itself is off.
* `adjust` is the latency constant of the first receiver sample: 0 for periods of 8 clocks and
  more, -1 below (`defaultAdjust`).

## Pin constraints

The receiver's two synchronizer registers are named `rxSync1` and `rxSync2`; the cells in the
netlist are `<hierarchy>rxSync1_reg` and `<hierarchy>rxSync2_reg` whatever the hierarchy. Tell Vivado
that they are a synchronizer (it then places them in one slice) and that the pin has no timing
relation to the clock, in the board's XDC file:

```tcl
set sync_regs [get_cells -hierarchical -filter {NAME =~ *Sync1_reg* || NAME =~ *Sync2_reg*}]
set_property ASYNC_REG TRUE $sync_regs
set_false_path -from [get_ports {rxd}]
```

This is the form that the [uart_echo](../uart_echo/README.md) example uses, checked in a Vivado
2024.2 implementation of the Arty A7-100T design: `ASYNC_REG` is set on the cells, a pair is in one
slice, the false path applies to the paths from the pin and `report_cdc` finds no unsafe crossing.
Other tools have their own spelling: Quartus `SYNCHRONIZER_IDENTIFICATION`, a false path from the
pin for any of them. The two registers reset to 0 (a line not yet seen high), see the receiver
below.

## Timing and tolerance

At 12 Mbaud with a 100 MHz clock (the tolerances also come from a model of the receiver over all
edge phases, which the testbench and a bisection of the baud offset confirm):

* **Transmitter:** every bit edge of a frame, and of any number of back-to-back frames, lies within
  0.5 clocks of the ideal grid `k * 25/3` clocks (the testbench measures at most 0.35, or 3.5 ns),
  and the frame period is 10 bit times exactly on average (240 frames checked against one grid).
  The bit timer is restarted only when a frame starts from idle. Its accumulator starts at R + M/2
  (the fraction of the start bit plus one half), which is what keeps the first edges of a frame
  centred.
* **Receiver:** the first sample, at the centre of the start bit, rejects a false start (a low
  pulse shorter than 5 clocks, once past the filter). Then one sample per bit, at its centre, and
  the stop bit is sampled at its centre too, and the receiver hunts again one clock later. That is
  what keeps the baud tolerance at 100 % line utilization: **-5.0 % to +4.0 % measured** with the
  remote running back to back (model: -5.06 % / +4.17 %, ideal +-5.26 %). The testbench runs
  -4.5 % to +3.5 % at three phases each, and +7 % and -8 % must produce errors.
* **Glitches:** with the default filter of 3 clocks a pulse of 1 or 2 clocks is invisible on an
  idle line and at the centre of a bit (the testbench puts one at the centre of every bit of a
  frame, and at random bits). Near a bit edge it is not: a pulse right after an edge can delay the
  filtered edge by up to `pulse + HOLD - 1` clocks, which eats into the baud tolerance.
  The depth of the filter is limited by the bit period. A pulse of HOLD-1 clocks at the centre of
  a bit of D clocks (D is the shortest bit: 8 at 8.33 clocks per bit) leaves ceil((D-HOLD+1)/2)
  clocks of the bit's level before it, and the filter must have switched to that level before
  the pulse starts, which takes HOLD of them: **HOLD <= (D+2)/3** (`maxHold`). So 3 is the limit
  at 12 Mbaud / 100 MHz, 2 at 6 clocks per bit and 7 at 20. A filter of 4 there corrupted 9 of the
  22 glitch-matrix cases that the filter of 3 passes (all 22). With the testbench's glitch matrix
  (every bit of a frame, pulses of 1 to HOLD-1 clocks) the deepest filter that passes is exactly
  this bound for every D from 4 to 20, and one more fails. The filter delays every edge alike, so
  it does not move the sampling point: the baud tolerance is the same for every HOLD (measured at
  4 Mbaud with HOLD 1, 3, 6 and 9). R5's T4e limit, that the narrowest legal bit pulse must contain
  HOLD samples, is looser than this at these periods. The bound is an elaboration error in
  `mkUartCore` (see above for run-time periods).
* **Other rates:** the tolerance rises with the bit period, to about +-5.1 % from 33 clocks per bit
  (3 Mbaud at 100 MHz) on; the testbench runs 3 Mbaud and 921600 baud with the remote 3 % off.
  It falls below 8 clocks per bit: at 16 Mbaud (6.25 clocks) the testbench runs +-2 %.

## Area

Measured with **bsc 2026.07.1** (`bsc -u -verilog`, no other flags) and **Yosys 0.70+post**
(Homebrew), LUT6 on 7-series (LUTRAM is extra; Yosys re-encodes the receiver's state in one-hot,
which is one FF more than the source has), at 12 Mbaud and 100 MHz:

```bash
bsc -u -verilog -p +:rtl -bdir build -vdir build rtl/Uart.bsv        # the Verilog of mkUart
yosys -q -p "read_verilog -sv -DBSV_NO_INITIAL_BLOCKS mkUart.v <bsc>/lib/Verilog/RegFile.v;
             hierarchy -top mkUart; synth_xilinx -family xc7 -flatten -top mkUart; stat"
```

(`<bsc>/lib/Verilog` is bsc's library. The receiver, transmitter and buffers alone are small
`(* synthesize *)` wrappers of `mkUartRx`, `mkUartTx` and `mkSmallFIFOF`, through the same two
commands.) The Verilog from the Xeda `bsc` flow (`-lift -aggressive-conditions` and more) is 2 LUT
larger for `mkUart`: 62.

| block | LUT | FF | notes |
|---|---|---|---|
| receiver (8N1, hold 3, static) | 21 | 26 | hold 1: 20 / 23; 8E1: 26 / 27 |
| transmitter (8N1, static) | 24 | 17 | 8E1: 25 / 18 |
| receiver, run-time period (D-1: 10 bits, fraction: 6 bits) | 46 | 37 | |
| transmitter, run-time period | 42 | 28 | the period register itself is in the user's design |
| buffer, depth 1 / 2 / 4 | 2 / 13 / 11 | 9 / 18 / 8 | depth 4: plus 8 LUTRAM |
| `mkUart`, defaults (tx depth 1, rx depth 4) | 60 | 64 | plus 8 LUTRAM |

A hand-written Verilog model of the same architecture (the research notes, same Yosys command) is
22 LUT / 25 FF (receiver) and 25 / 17 (transmitter): within a LUT of the BSV, so the generated code
is not bigger. Other tools, `mkUart` with the defaults, through Xeda from the Verilog of the `bsc`
flow:

| tool and flow | result |
|---|---|
| Vivado 2024.2, `xeda run vivado_synth uart.yaml` (out of context, xc7a100tcsg324-1, 10 ns) | **49 LUT (43 logic, 6 LUTRAM), 59 FF, 19 slices, Fmax 238 MHz** |
| Yosys 0.69+286, `xeda run yosys_fpga uart.yaml --target artix7` (xc7a100t) | 76 LUT (68 logic, 8 LUTRAM), 64 FF |
| Yosys 0.69+286 and nextpnr-ecp5 0.11.1, `xeda run yosys_fpga+nextpnr uart.yaml --target ecp5` (LFE5U-85F, 50 MHz target) | 95 LUT4 (TRELLIS_COMB), 64 FF, Fmax 173 MHz |
| Yosys 0.69+286, `xeda run yosys uart.yaml` (nangate45, 1.5 ns) | 926 um2 |

The generated Verilog has no needless multiplexers: every register is written by one rule that
fires every cycle, `txd` is a register output, and the timers' reloads and compares are constants.

## Testbench

`tb/TbUart.bsv` runs five DUTs at the same time, each against `tb/TbUartModels.bsv`: a remote
UART model (a bit period in 1/65536 clock, so any baud offset and phase; glitches, bad frames,
breaks), a checker that holds the DUT's `txd` to the ideal grid (half a clock, and exact at the
end of a train of frames) and a consumer that compares every received byte.

| DUT | what it checks |
|---|---|
| A: `mkUart`, 8N1, 12 Mbaud | low line from power-up; loopback of 240 bytes at 100 % utilization (corner and random bytes: data, order, zero gap, exact baud); remote at -4.5 % to +3.5 % x 3 phases, and +7 %, -8 % that must fail; framing error and resync; breaks of 12 and 48 bits (one event, no byte); glitches of 1 and 2 clocks on an idle line, at the centre of 60 random bits and at the centre of every bit of a frame in turn (the glitch matrix: 2 widths x 10 bits); false starts of 3 and 4 clocks; overrun with a stalled consumer (`RX_DEPTH` bytes kept in order, 3 dropped), recovery, flags; `clearFlags` in the very cycle of a framing error, a break and an overrun: the event stays one cycle and its flag is set |
| B: `mkUartCore`, 7E2, 8 Mbaud, hold 2, tx depth 1, rx depth 2 | loopback with the checker, remote at +-3 % with frames of one and two stop bits mixed, a parity error (with `clearFlags` in its cycle), a framing error, a break |
| C: `mkUartCore`, 8O1, run-time period | 12 M, 3 M and 921600 baud in turn: loopback, remote at +-3 %; the same error cases at 12 M; then reset in the middle of a transmitted frame (two bytes queued, one on the line) and of a received frame, cut every 6 clocks from start bit to stop bit: `txd` high and the transmitter idle two clocks later, the queued byte never sent, a clean frame next; the receiver delivers nothing and raises nothing, and works again |
| D: `mkUartCore`, 8N1, 16 Mbaud, hold 2 | 6.25 clocks per bit, `adjust` = -1 (hold 2 is the most that 6 clocks in the shortest bit allow) |
| E: `mkUartCore`, 8N1, 921600 baud | the period does not fit the accumulator and is rounded |

`XEDA_INJECT_BUG` makes the receiver late to hunt for the next start bit after a frame (it samples
from the previous frame's phase); the baud offset runs catch it. `XEDA_INJECT_BUG_TX` opens a gap
between transmitted frames; the checker catches it. Defining `TB_TRACE` prints the stages.

All three simulators run 104270 cycles of the testbench to the same result: Bluesim 0.5 s,
Verilator 0.6 s, Icarus Verilog 20 s for the simulation itself. The whole `xeda run bsc_sim` takes
about 40 s on Bluesim and Verilator and 60 s on Icarus, most of it bsc elaborating the testbench.

The tests assume the default `HOLD` and an 8N1 `mkUart` at 12 Mbaud with 100 MHz. The buffer depths
of A follow `TX_DEPTH` and `RX_DEPTH`, and the testbench queues of expected bytes and frames are
sized from them: it passes with a transmit depth of 1, 2, 4 and 16 and a receive depth of 1, 4, 8
and 16 (`--design-overrides tb.defines.TX_DEPTH=16`; the defines of the testbench reach the
design too). `RX_DEPTH=2` does not build the testbench: bsc finds a scheduling cycle
between the bench's event counters and its consumer when the receive buffer is a `mkFIFOF` (it did
before the reset test was added too); depth 2 is covered by DUT B. `HOLD=4` stops the build with the
error of `mkUartCore`.

DUT C is reset by a `mkReset` around it, so the rules of its bench use two reset domains on purpose;
the design file suppresses bsc's warning about it (G0043) for `bsc_sim`.

```bash
cd examples/bluespec/uart
xeda run bsc_sim uart.yaml                          # Bluesim
xeda run bsc_sim uart.yaml -s simulator=verilator
xeda run bsc_sim uart.yaml -s simulator=iverilog
xeda run bsc_sim uart.yaml --design-overrides tb.defines.XEDA_INJECT_BUG=true   # must fail
xeda run bsc uart.yaml                              # Verilog of mkUart
xeda run vivado_synth uart.yaml                     # Vivado, out of context
xeda run yosys_fpga uart.yaml --target artix7       # yosys, xc7a100t
xeda run yosys_fpga+nextpnr uart.yaml --target ecp5
```

## The receiver in one paragraph

`rxd` goes through two registers (`rxSync1`, `rxSync2`) and a hold filter (`HOLD` equal samples
before its output changes). State `Hunt`: a low line restarts the bit timer to tick after half a
bit. State `Start`: at the first tick the line must still be low, else it is a false start and the
receiver hunts again; a marker bit is loaded into a shift register. State `Data`: at every tick the
line shifts in at the top until the marker reaches bit 0. The next tick samples the stop bit and
raises `valid` for a cycle; the receiver goes to `Hunt`, or to `WaitHigh` if the stop bit was low.
Everything else about the frame (break, parity error, data) is combinational on the shift register,
which stays untouched until the next start bit's first sample, so no further registers are needed.
