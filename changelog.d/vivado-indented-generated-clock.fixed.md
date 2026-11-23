- `vivado_synth` counts a generated clock in `fmax_by_clock` and `fmax`. Vivado indents it in the
  clock summary, and Xeda skipped it, so `fmax` could be too high.
