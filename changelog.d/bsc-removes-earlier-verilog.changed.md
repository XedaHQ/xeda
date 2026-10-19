- `bsc` and `bsc_sim` remove the Verilog modules and `.use` files of an earlier run before they
  compile, also with `cleanup_bobjs: false`. That setting now decides only whether they remove the
  `.bo` and `.ba` files.
