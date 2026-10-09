- A plan that takes related inputs from different producers is refused, naming the bindings that
  fix it. Before, binding only `netlist_timing` of `vivado_postsynth_sim` to `vivado_alt_synth`
  paired its netlist with the delays of `vivado_synth`; the checkpoint and activity of
  `vivado_power` had the same gap.
