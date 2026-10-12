- A waveform setting given a location is written as `dump.<format>` in the run directory, and
  `true` writes the same file. The `saif` of `vivado_sim` and `vivado_postsynth_sim` keeps
  `outputs/<design>.saif`, so `true` is refused there. `nvc` with `wave: true` writes `dump.fst`,
  not `<top>.fst`. A name that ends in the extension of another format with its own setting is
  refused: use `vcd` instead of `wave: x.vcd`.
