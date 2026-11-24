- The fixed mappings of the API have `TypedDict` types: `Plan.options`, `PlanNode.executable_check`,
  the entries of `RunRecord.dependencies`, `OutputRecord.design` and each listing of
  `xeda.introspect`. A type checker now knows their keys. A record with no `design_file` in a
  dependency is refused.
