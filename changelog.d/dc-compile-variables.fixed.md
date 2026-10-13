- The `dc` setting `compile` now sets the `compile_<key>` variables it holds, and a key there
  overrides the variable that `optimization` presets on DC-NXT. Nothing used `compile` before, and
  its two default entries did nothing, so the default is now empty.
