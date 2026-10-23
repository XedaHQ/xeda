- `xeda list-results --json` now lists `metrics`, each with a type and a unit, in place of `keys`,
  `documented` and `canonical_aliases`. A flow's `results_description` takes `MetricSpec` values,
  and `vivado_power` writes its power and temperature values as numbers.
