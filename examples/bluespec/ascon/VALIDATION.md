# Ascon port validation

This file lists what the checked-in tests and commands cover, and the results of one run of the
synthesis commands. The tool versions were bsc 2026.07.1 (build 63665af1d1), Yosys 0.69+286,
nextpnr-ecp5 0.11.1, Verilator 5.053, Icarus Verilog 14.0 and OpenROAD with the Nangate45 platform.

## Simulation

The test vectors come from the C references vendored in BlueLight at the revision in NOTICE.md.
They include 129-byte streams with a 100-cycle output stall and key reuse across a Hash operation.

`tests/test_ascon_example.py` and the Ascon cases of `tests/test_bsc_examples.py` run the vectors
in Bluesim for Ascon-128 at unroll factors 1, 2, 3 and 6 and for Ascon-128a at 1, 2 and 4. They
run Ascon-128a at unroll factor 4 with Icarus Verilog and Verilator, check that the testbench
catches an injected bug, and check that bsc refuses an unroll factor that does not divide the
round counts. The focused command passed all 16 tests:

```bash
pytest tests/test_bsc_examples.py tests/test_ascon_example.py -k ascon
```

## Synthesis and physical implementation

These commands ran from the root directory of the repository with the settings of `ascon.yaml`.
Each of them ran `bsc` first, to convert the design to Verilog.

| Command | Result |
| --- | --- |
| `xeda run bsc_sim examples/bluespec/ascon/ascon.yaml` | passed in Bluesim, Verilator (`-s simulator=verilator`) and Icarus Verilog (`-s simulator=iverilog`) |
| `xeda run yosys_fpga examples/bluespec/ascon/ascon.yaml --target artix7` | 2239 LUT, 1106 FF, 13 CARRY4 (`xc7a100tcsg324-1`, a synthesis estimate) |
| `xeda run yosys_fpga+nextpnr examples/bluespec/ascon/ascon.yaml --target ecp5` | placed and routed on `LFE5U-85F-6BG381C` without pins: 3170 TRELLIS_COMB, 1106 TRELLIS_FF, Fmax 63.2 MHz at a 20 ns target |
| `xeda run openroad examples/bluespec/ascon/ascon.yaml` | the whole chain passed on Nangate45 at a 2 ns clock period |

For the OpenROAD chain, placement, clock-tree synthesis, global routing, detailed routing and the
final checks succeeded. The final reported cell area was 18820 square micrometers, the worst slack
was 0.353 ns and the estimated Fmax was 607 MHz. There were no DRC errors and no antenna
violations, and the default timing, DRC and antenna failure checks stayed on. These figures include
the stream adapter. They apply only to this tool and platform run.

The FPGA results are resource and timing estimates for the core alone. A board build needs an I/O
wrapper, pin constraints and a test on hardware.
