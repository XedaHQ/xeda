- `openfpgaloader` now fails when the loader exits with status 0 but its output shows that the
  FPGA was not programmed, such as DONE staying low after a Xilinx load, and when a Xilinx flash
  write does not end with `Done`. The error says what happened and quotes the lines of the
  loader's output.
