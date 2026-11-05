- `RunRecord.design` is now `design_name`, `started` and `finished` are `started_at` and
  `finished_at`, and `Event.time` is `occurred_at`. In JSON, `results.json`, the `spec` of the run
  and plan documents, and the `dse` and `scrub` documents use `design_name` for the design name.
  Run the flow again to write the new `results.json`.
