- `bsc` accepts a design with VHDL or other sources beside the Bluespec sources, and passes them
  through unchanged. Before it runs a tool, `bsc` and `bsc_sim` refuse a design that defines
  `BSV_POSITIVE_RESET` while `positive_reset` is on, and `bsc` refuses a design with no Bluespec RTL
  sources.
