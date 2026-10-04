# Bluespec examples

Small, self-checking designs in Bluespec SystemVerilog (BSV, `.bsv`) and Bluespec Classic
(BH, `.bs`), each with an Xeda design file. They need bsc 2026.07.1 or newer.

Each example lives in `examples/bluespec/<name>/`, with its design file `<name>.yaml`, the
design in `rtl/` and the testbench in `tb/`. Compile a design's RTL to Verilog with the `bsc`
flow:

```bash
xeda run bsc examples/bluespec/gcd/gcd.yaml
```

Simulate its testbench with the `bsc_sim` flow, in Bluesim (the default) or in a Verilog
simulator through bsc's own link step:

```bash
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=verilator
xeda run bsc_sim examples/bluespec/gcd/gcd.yaml -s simulator=iverilog
```

All the designs follow the same conventions:

- Each package lives in a file of the same name. In `rtl.sources` the file defining `rtl.top`
  comes last among the Bluespec sources, and in `tb.sources` the file defining `tb.top`.
- The testbench top module is `(* synthesize *)`d with an `Empty` interface. It prints a line
  starting with `PASS` and calls `$finish(0)` on success, and fails with `$fatal`, which
  makes the simulation exit with a non-zero status, on a mismatch or when a watchdog runs
  out of cycles. (`$finish(1)` would not do: its argument is a verbosity level, not an
  exit status.)
- Defining the macro `XEDA_INJECT_BUG` (`defines = { XEDA_INJECT_BUG = true }` under
  `[tb]`) plants a deliberate bug that the testbench must catch, for negative tests.
- Bluesim, Verilator and Icarus Verilog run each testbench in the same number of cycles.

## gcd: multi-package BSV

A GCD accelerator with a Get/Put (`Server`) interface. `mkGcdUnit` hands requests round-robin
to two binary-GCD engines and collects their results in the same order. The engine,
`mkGcdEngine`, is synthesized separately in another package (`GcdEngine`), so the Verilog of
the design comes from two packages, plus the bsc library FIFOs it instantiates (`SizedFIFO`,
`FIFO2`). The testbench sends directed corner cases and LFSR-driven requests and checks each
result against Euclid's algorithm, run by a `StmtFSM`.

## fir: a design sized by macros

A streaming transposed-form FIR filter. The core, `mkFir`, is polymorphic in its tap count and
widths, related by provisos, with an accumulator that grows by `TLog#(taps)` bits. The top,
`mkFirFilter`, sizes it with the preprocessor macros `TAPS` and `SAMPLE_WIDTH`, which the
design file sets through `rtl.defines` (Xeda passes them to bsc as `-D NAME=value`).
Without them, the source falls back to 8 taps and 16-bit samples. The testbench checks every
output against a direct-form model, and its `PASS` line reports the tap count and sample width
used.

## collatz: Bluespec Classic (BH)

A Collatz ("3n + 1") sequence unit and its testbench, written in BH. For each start value it
counts the steps to 1 and the largest value reached, and flags sequences that leave the 32-bit
range. The testbench's expected results come from a golden model that bsc evaluates at compile
time on unbounded `Integer`s. bsc's preprocessor handles BSV sources only, so `-D` macros never
reach BH code: the testbench imports the `XEDA_INJECT_BUG` switch from a one-line BSV package,
`TbCollatzOptions`.

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

Bluesim cannot simulate imported Verilog, so this design sets `simulator = "verilator"` for
`bsc_sim` in its design file, and `-s simulator=iverilog` works too.
