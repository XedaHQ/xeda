- A `CancelToken` now stops a remote run during its first steps, while it connects, provisions,
  syncs and compares the plan with the host. The run returns a `canceled` result.
