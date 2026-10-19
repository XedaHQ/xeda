- `yosys` now refuses `liberty` together with `gates` or `lut`, as it already does for `platform`.
  Mapping to library cells and mapping to generic gates or LUTs contradict each other.
