- `Workspace.run` runs a `Spec` or a `Plan` and returns a `Result`. A stale `Plan` raises
  `StalePlanError` before the launch prunes, asks or generates anything, and `prune_variants`
  removes the other run directories of every node of the plan, producers included.
