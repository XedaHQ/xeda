- `xeda run --dry-run` now shows refusals that `bsc`, `bsc_sim`, `ise_synth`, `vcs`, `verilator`,
  `ghdl_sim`, `nvc`, `vivado_postsynth_sim`, `yosys`, `yosys_fpga` and `yosys_sim` made only after
  the run began, and a clock setting that matches no design clock. A flow in a chain is refused
  before the flows ahead of it run. `ise_synth` without `rtl.top` fails with a settings error.
