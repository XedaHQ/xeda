# Ascon port validation

Local validation used BSC 2026.07.1 (build 63665af1d1), Yosys 0.70+post,
Verilator 5.052, and Icarus Verilog 13.0.

The standalone core passed BSC elaboration with all warnings promoted to errors for every
supported variant and unroll factor: Ascon-128 at 1, 2, 3, and 6; Ascon-128a at 1, 2, and 4.
The test vectors come from the C references vendored in BlueLight at the revision in NOTICE.md.
They include 129-byte streams with a 100-cycle output stall and key reuse across a Hash operation.

The focused acceptance command passed all 20 tests:

```bash
pytest tests/test_bsc_examples.py tests/test_ascon_example.py tests/test_ascon_showcase.py \
  -k ascon -n auto
```

It covers the supported unroll factors in Bluesim, both AEAD variants with Icarus and Verilator,
fault injection, invalid-factor rejection, Verilog elaboration and lint, synthesis, and the helper.
The changelog and documentation checks passed 407 tests. The repository's Black, Ruff, and
`mypy src` checks also passed.

## Synthesis and physical implementation

The example helper passed Xilinx FPGA synthesis through Yosys for `xc7a100tftg256-2L`.
This validates synthesis of the generic core; board pin constraints and hardware testing need a
board-specific wrapper.

The ASIC helper passed Nangate45 cell mapping and the full OpenROAD flow at a 10 ns clock period.
Placement, clock-tree synthesis, global routing, detailed routing, and final checks succeeded.
The final reported cell area was 18826.4 square micrometers and the estimated Fmax was
372.871 MHz. These figures include the stream adapter and apply only to this tool/platform run.
The OpenROAD run kept its default timing, DRC, and antenna failure checks enabled.
