- `Workspace.run` runs a `Spec` or a `Plan` and returns a `Result`. Running a plan checks it
  against a new planning and raises `StalePlanError` when it is stale.
