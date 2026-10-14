- `yosys` maps to a standard cell library in the order of OpenROAD-flow-scripts, and passes the
  unusable cells to `dfflibmap` and `abc` as `-dont_use`. It no longer merges the Liberty files
  or passes `-dff` to `abc`, and it refuses `abc_dff: true`, `post_synth_opt: true`, `prep`,
  `nosynth` and `noabc` with such a mapping.
