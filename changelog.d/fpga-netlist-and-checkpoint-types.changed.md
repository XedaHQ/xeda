- Vivado netlists are now typed `FpgaNetlist` (functional) and `FpgaTimingNetlist` (timing), and
  checkpoints `SynthCheckpoint` or `RoutedCheckpoint`, so `vivado_synth.netlist+openroad`, a
  pre-route checkpoint into `vivado_power` and a crossed netlist binding are refused. A `.dcp` or
  Vivado netlist source now needs one of these as its `type`.
