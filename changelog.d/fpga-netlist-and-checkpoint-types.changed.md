- Vivado netlists are now typed `FpgaNetlist` and Vivado checkpoints `SynthCheckpoint` or
  `RoutedCheckpoint`, so `vivado_synth.netlist+openroad` and a pre-route checkpoint into
  `vivado_power` are refused. A `.dcp` source now needs `type: RoutedCheckpoint` (or
  `SynthCheckpoint`), and a Vivado netlist source needs `type: FpgaNetlist`.
