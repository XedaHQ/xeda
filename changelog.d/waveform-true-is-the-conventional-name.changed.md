- A waveform setting given a location is written as `dump.<format>` in the run directory, and
  `true` writes the same file. `nvc` with `wave: true` writes `dump.fst`, not `<top>.fst`, and
  `true` is refused for the `saif` of `vivado_sim`.
