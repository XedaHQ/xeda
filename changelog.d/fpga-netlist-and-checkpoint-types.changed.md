- Vivado netlists are now `FpgaNetlist` and Vivado checkpoints `SynthCheckpoint` or
  `RoutedCheckpoint`. So `vivado_synth.netlist+openroad` and a pre-route checkpoint into
  `vivado_power` are refused. A `.dcp` source now needs its `type`, and a Vivado netlist source
  for `vivado_postsynth_sim` needs `type: FpgaNetlist`.
