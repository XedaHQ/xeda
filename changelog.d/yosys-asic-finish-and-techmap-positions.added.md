- `yosys` takes more finishing options for a standard-cell mapping, in the group `finish`:
  `drop_formal`, `names`, `normalize_single_bit`, `check_pre`, `allow_tristate`,
  `insert_tie_cells` and `insert_buffers`. Each entry of `techmap_files` can take a `when`
  position (`pre_synth`, `post_coarse`, `pre_dff` or `post_dff`); a plain path keeps its place.
