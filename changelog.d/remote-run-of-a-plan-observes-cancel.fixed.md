- A `CancelToken` now stops a remote run during its first steps (connect, provision, agent start,
  sync, plan comparison) and returns a `canceled` result. A canceled or interrupted provisioning
  also stops on the host and frees its lock.
