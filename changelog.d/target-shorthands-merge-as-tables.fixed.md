- A target, `--design-overrides` and each settings layer now read a short form such as
  `fpga: <part>` or `clock: CLK` as the table it stands for, and report a malformed value that a
  table above it used to hide. `--design-overrides` now adds its `sources` after the design's, as a
  target does; `vhdl: true` and `clock: 0` are refused.
