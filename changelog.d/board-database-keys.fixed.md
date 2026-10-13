- A dotted key in an entry of a custom board database, such as `fpga.part` in YAML, now means the
  table it stands for. Before, Xeda dropped it and the board had no part. An unknown key in an
  entry is now an error that names the board, the key and the file.
