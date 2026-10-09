- Every flow that reads the design's sources now refuses, before it runs, a source in a language
  it cannot read (VHDL for `verilator`, SystemVerilog for `ghdl_sim` or `nvc`) and a design with no
  source it reads.
