- Fix `out_of_context` losing options: `vivado_alt_synth` dropped it for an empty or list
  synthesis step, and `vivado_synth` replaced the `SYNTH_DESIGN.ARGS.MORE.OPTIONS` of the design.
  The Vivado flows no longer record the options they derive in their settings.
