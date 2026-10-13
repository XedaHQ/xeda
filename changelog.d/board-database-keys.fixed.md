- A dotted key in an entry of a custom board database, such as `fpga.part` in YAML, now means the
  table it stands for. Before, Xeda dropped it and the board had no part. An unknown key, or a pin
  file key (`lpf`, `pcf`, `pdc`, `xdc`) that is not text, is now an error.
