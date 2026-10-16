- `bsc` accepts a design with VHDL or other sources beside the Bluespec sources, and passes them
  through unchanged. It refuses a design that defines `BSV_POSITIVE_RESET` while `positive_reset`
  is on.
