- A target, `--design-overrides` and each settings layer now read a short form such as
  `fpga: <part>` or `clock: CLK` as the table it stands for, and report a malformed value that a
  table above it used to hide. `vhdl: true` and `clock: 0` are refused.
