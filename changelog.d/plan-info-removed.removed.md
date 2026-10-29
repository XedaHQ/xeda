- `xeda.introspect.plan_info` and `request_info` are gone. Use `Workspace(...).plan(spec)` and
  `Plan.to_dict()`, which is the document of `xeda run --dry-run --json`.
