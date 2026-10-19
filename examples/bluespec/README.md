# Bluespec examples

Small, self-checking designs in Bluespec SystemVerilog (BSV, `.bsv`) and Bluespec Classic
(BH, `.bs`), each with an Xeda design file. They need bsc 2026.07.1 or newer.

Each example is in `examples/bluespec/<name>/`: the design file `<name>.yaml`, the design in
`rtl/` and the testbench in `tb/`. Compile the RTL of a design to Verilog with the `bsc` flow:

```bash
xeda run bsc examples/bluespec/gcd/gcd.yaml
```

Simulate its testbench with the `bsc_sim` flow, in Bluesim (the default) or in a Verilog
simulator through the link step of bsc:

```bash
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=verilator
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=iverilog
```

All the designs follow the same conventions:

- Each package is in a file of the same name. In `rtl.sources`, the file that defines `rtl.top`
  comes last among the Bluespec sources. In `tb.sources`, the file that defines `tb.top` comes
  last.
- The testbench top module has an `Empty` interface and the `(* synthesize *)` attribute. On
  success, it prints a line that starts with `PASS` and calls `$finish(0)`. On a mismatch, or
  when a watchdog runs out of cycles, it calls `$fatal`, which makes the simulation exit with a
  non-zero status. (The argument of `$finish` is a verbosity level, so `$finish(1)` does not set
  an exit status.)
- Defining the macro `XEDA_INJECT_BUG` plants a deliberate bug that the testbench must catch. Use
  it for a negative test: the run must fail.
- Bluesim, Verilator and Icarus Verilog run each testbench in the same number of cycles.

To run the negative test without editing a design file, define the macro on the command line:

```bash
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml --design-overrides tb.defines.XEDA_INJECT_BUG=true
```

The same setting in a design file is `defines.XEDA_INJECT_BUG: true` under `tb`.

## Other flows

The design files of `gcd` and `ascon` also hold the settings for the flows that read Verilog. Xeda
runs `bsc` first, to convert the design. These commands use `gcd`. For `ascon`, replace the path:

```bash
xeda run openroad examples/bluespec/gcd/gcd.yaml  # ASIC place and route on nangate45
xeda run vivado_synth examples/bluespec/gcd/gcd.yaml  # Vivado, out of context
xeda run yosys_fpga+nextpnr examples/bluespec/gcd/gcd.yaml --target ecp5  # ECP5 place and route
xeda run yosys_fpga examples/bluespec/gcd/gcd.yaml --target artix7  # Xilinx synthesis only
```

The FPGA configurations are resource and timing estimates without pins. A board build needs an I/O
wrapper and constraints, as the targets of [`uart_echo`](#uart_echo-a-uart-echo-demo-for-two-boards)
have.

## gcd: multi-package BSV

A GCD accelerator with a Get/Put (`Server`) interface. `mkGcdUnit` hands requests round-robin
to two binary-GCD engines and collects their results in the same order. The engine,
`mkGcdEngine`, is synthesized separately in another package (`GcdEngine`), so the Verilog of
the design comes from two packages, plus the bsc library FIFOs it instantiates (`SizedFIFO`,
`FIFO2`). The testbench sends directed corner cases and LFSR-driven requests and checks each
result against Euclid's algorithm, run by a `StmtFSM`.

## fir: a design sized by macros

A streaming transposed-form FIR filter. The core, `mkFir`, is polymorphic in its tap count and
widths, which provisos relate, and its accumulator grows by `TLog#(taps)` bits. The top,
`mkFirFilter`, sizes it with the preprocessor macros `TAPS` and `SAMPLE_WIDTH`. The design file
sets them in `rtl.defines`, and Xeda passes them to bsc as `-D NAME=value`. Without them, the
source uses 8 taps and 16-bit samples. The testbench checks every output against a direct-form
model. Its `PASS` line reports the tap count and the sample width.

## collatz: Bluespec Classic (BH)

A Collatz ("3n + 1") sequence unit and its testbench, written in BH. For each start value, the
unit counts the steps to 1 and the largest value reached, and flags sequences that leave the
32-bit range. The expected results of the testbench come from a golden model that bsc evaluates
at compile time on unbounded `Integer`s. The preprocessor of bsc reads BSV sources only, so `-D`
macros do not reach BH code. The testbench therefore imports the `XEDA_INJECT_BUG` switch from a
one-line BSV package, `TbCollatzOptions`.

## crc32: mixed BSV and BH

A CRC-32 unit whose BSV top, `mkCrcUnit`, imports a BH package, `CrcCore`. The BH package
holds the CRC datapath and a separately synthesized engine that folds a 32-bit word into the
CRC each cycle, and the BSV top frames messages around it. The testbench checks two
known-answer messages (including the standard check value of `"123456789"`, `CBF43926`) and
random messages against a table-driven reference.

## verilog_import: importing Verilog with `import "BVI"`

`mkMulUnit` wraps an existing Verilog module, the two-stage pipelined multiplier `pipe_mul.v`,
which is one of the design's sources. The `import "BVI"` declaration maps its clock, reset,
parameter and ports to BSV methods and declares how they may be scheduled. The pipeline cannot
stall, so the wrapper counts credits to never start a multiplication without room for its
result. The testbench drains results slowly to exercise that backpressure.

Bluesim cannot simulate imported Verilog, so the design file sets `simulator: verilator` for
`bsc_sim`. The option `-s simulator=iverilog` works too.

## ascon: standalone crypto core

The [Ascon v1.2 example](ascon/README.md) adds a standalone 32-bit Put/Get core and a
vector-driven testbench. Like `gcd`, its design file holds the settings for the ASIC chain, Vivado
and the two FPGA targets, so the commands in [Other flows](#other-flows) work on it too. Its ASIC
clock period is 2 ns.

## uart: a UART PHY

The [UART PHY](uart/README.md) is a transmitter and a receiver with byte buffers, sticky error flags
and event pulses, at up to 12 Mbaud with a 100 MHz clock. It is polymorphic in the frame format,
the buffer depths and the bit period, which is fixed at elaboration (exact rational) or read from a
register at run time, and `mkUart` is a synthesizable 8N1 top that the macros `CLK_HZ` and `BAUD`
size. The testbench runs five DUTs against models of a remote UART, with baud offsets, phases,
glitches, bad frames and breaks, and of a line checker. Its design file also has the FPGA flows
(Vivado out of context, `yosys_fpga` and nextpnr for ECP5).

## uart_echo: a UART echo demo for two boards

The [echo demo](uart_echo/README.md) sends everything it receives back, through a block RAM FIFO,
with the PHY of the `uart` example. Its source files come from `../uart/rtl` through relative paths
in the design file. `mkUartEcho` is board agnostic: a clock, the UART, four switches that select the
baud rate from a table made at elaboration, a button and four LEDs, and a power-on reset of its
own. The testbench is a behavioral host at the pins (echo at three baud rates, LEDs, errors,
overrun, a baud rate switched in the middle of traffic). The targets `arty` (Vivado),
`arty_nextpnr` (open-source tools) and `ulx3s` build bitstreams with pin files in `boards/`, and
`host/uart_echo_test.py` checks the echo from a computer at the full line rate (99.6 to 100 % at
12 Mbaud on the Arty, measured). The macro `FIXED_BAUD` gives a build with one baud rate and no
switches that needs 96 LUT in Vivado instead of 168.

```bash
xeda run bsc_sim examples/bluespec/uart_echo/uart_echo.yaml
xeda run vivado_synth examples/bluespec/uart_echo/uart_echo.yaml --target arty
xeda run yosys_fpga+nextpnr+fpga_pack examples/bluespec/uart_echo/uart_echo.yaml --target ulx3s
```
